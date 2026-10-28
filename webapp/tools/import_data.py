# -*- coding: utf-8 -*-
"""
import_data.py — 把已采集的 Excel 灌进本地 SQLite

支持两类文件（可放在任意目录，自动递归查找）：
  车次列表_两阶段版_*.xlsx       -> trains 表（车次基础信息）
  列车经停站_*.xlsx              -> routes 表（经停站缓存）

用法：
    python tools/import_data.py                     # 默认扫描 D:\\railcn 和 D:\\ceshi\\tranclaw
    python tools/import_data.py --dirs D:\\railcn
    python tools/import_data.py --stations D:\\railcn\\cache\\stations.json
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sqlite3
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "rail.db")
STATIONS_OUT = os.path.join(DATA_DIR, "stations.json")
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import routestore  # noqa: E402  经停站存储（一天一条指针）

DEFAULT_DIRS = [r"D:\railcn", r"D:\ceshi\tranclaw"]

DAY_DIFF = {"当日": 0, "次日": 1, "第三日": 2, "第四日": 3, "第五日": 4}


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS trains (
            code TEXT NOT NULL, train_no TEXT, type TEXT,
            start_sta TEXT, end_sta TEXT, stop_count INTEGER, date TEXT,
            PRIMARY KEY (code, date)
        );
        CREATE INDEX IF NOT EXISTS idx_trains_code ON trains(code);
        CREATE INDEX IF NOT EXISTS idx_trains_sta  ON trains(start_sta, end_sta);
        -- 经停站表由 routestore 建（route_tpl / route_day / routes 视图）
        CREATE TABLE IF NOT EXISTS coord_override (
            name TEXT PRIMARY KEY, lat REAL, lon REAL
        );
        """
    )
    routestore.ensure_schema(conn)


def ymd(raw) -> str:
    s = str(raw or "").strip()
    m = re.match(r"^(\d{4})(\d{2})(\d{2})$", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return s[:10]
    return s


def import_train_lists(conn: sqlite3.Connection, dirs: list[str]) -> None:
    import openpyxl

    files = sorted({f for d in dirs if os.path.isdir(d) for f in glob.glob(os.path.join(d, "车次列表_*.xlsx"))})
    total = 0
    for path in files:
        try:
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        except Exception as exc:          # noqa: BLE001
            print(f"  ! 跳过 {os.path.basename(path)}: {exc}")
            continue
        rows = 0
        for ws in wb.worksheets:
            header_seen = False
            for row in ws.iter_rows(values_only=True):
                if not row:
                    continue
                if not header_seen:
                    header_seen = True
                    continue
                date, start_sta, code, end_sta, stop_count, train_no = (list(row) + [None] * 6)[:6]
                if not code:
                    continue
                code = str(code).strip().upper()
                try:
                    n = int(stop_count) if stop_count not in (None, "") else None
                except (TypeError, ValueError):
                    n = None
                conn.execute(
                    "INSERT OR REPLACE INTO trains (code, train_no, type, start_sta, end_sta, stop_count, date)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (code, str(train_no or "").strip(), code[0], str(start_sta or "").strip(),
                     str(end_sta or "").strip(), n, ymd(date)),
                )
                rows += 1
        wb.close()
        conn.commit()
        total += rows
        print(f"  + {os.path.basename(path)}: {rows} 条车次")
    print(f"[trains] 合计写入 {total} 条")


def import_routes(conn: sqlite3.Connection, dirs: list[str]) -> None:
    import openpyxl

    files = sorted({f for d in dirs if os.path.isdir(d) for f in glob.glob(os.path.join(d, "列车经停站_*.xlsx"))})
    total_trains = 0
    for path in files:
        try:
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        except Exception as exc:          # noqa: BLE001
            print(f"  ! 跳过 {os.path.basename(path)}: {exc}")
            continue
        groups: dict[tuple[str, str], list[dict]] = {}
        for ws in wb.worksheets:
            header_seen = False
            for row in ws.iter_rows(values_only=True):
                if not row:
                    continue
                if not header_seen:
                    header_seen = True
                    continue
                v = (list(row) + [None] * 13)[:13]
                ttype, code, train_no, date, start_sta, end_sta, seq, name, arr, dep, stay, run, daystr = v
                if not train_no or not name:
                    continue
                day = 0
                for k, val in DAY_DIFF.items():
                    if str(daystr or "").startswith(k):
                        day = val
                        break
                stop = {
                    "station_name": str(name).strip(),
                    "station_train_code": str(code or "").strip(),
                    "arrive_time": str(arr or "").strip(),
                    "start_time": str(dep or "").strip(),
                    "running_time": str(run or "00:00").strip(),
                    "arrive_day_diff": str(day),
                    "arrive_day_str": str(daystr or "").strip(),
                    "train_class_name": "",
                }
                groups.setdefault((str(train_no).strip(), ymd(date)), []).append(
                    (int(seq or 999), stop)
                )
        wb.close()

        for (train_no, date), items in groups.items():
            items.sort(key=lambda x: x[0])
            raw = [s for _, s in items]
            code = raw[0]["station_train_code"] if raw else ""
            routestore.put_route(conn, train_no, date, code,
                                 json.dumps(raw, ensure_ascii=False))
            total_trains += 1
        conn.commit()
        print(f"  + {os.path.basename(path)}: {len(groups)} 趟列车的经停站")
    print(f"[routes] 合计写入 {total_trains} 趟")


def copy_stations(conn: sqlite3.Connection, explicit: str | None, dirs: list[str]) -> None:
    cands = []
    if explicit:
        cands.append(explicit)
    cands += [os.path.join(d, "cache", "stations.json") for d in dirs]
    for p in cands:
        if p and os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            with open(STATIONS_OUT, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            print(f"[stations] 已复制 {len(data)} 个车站 -> {STATIONS_OUT}")
            return
    print("[stations] 未找到 stations.json，运行时将由 trainclaw 自动下载")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dirs", nargs="*", default=DEFAULT_DIRS)
    ap.add_argument("--stations", default=None)
    args = ap.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    print(f"[db] {DB_PATH}")
    copy_stations(conn, args.stations, args.dirs)
    print("[trains] 导入车次清单 ...")
    import_train_lists(conn, args.dirs)
    print("[routes] 导入经停站 ...")
    import_routes(conn, args.dirs)
    conn.close()
    print("[done]")


if __name__ == "__main__":
    main()
