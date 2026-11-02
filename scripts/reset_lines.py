# -*- coding: utf-8 -*-
"""清空所有线路（保留线路资料库），清之前做完整备份

清的是「线路」这一层：

  rail.db   line_custom        自建线路（含资料库带进来的）
            line_override      人工站序 / 人工里程
            line_src_pick      站序默认来源勾选
            line_src_fix       来源级修正（倒排）
            line_osm / line_osm_stop        OSM 导入的线路与站序
            line_ce / line_ce_stop          china-emu 导入的线路与站序
            station_osm_line               车站→所属线路 的归属关系
            line_geom          GeoJSON 导入的走向
  底表      data/cnrail_lines.json        417 条带站序的线路

留的：

  line_catalog / line_catalog_stop   线路资料库（844 条资料 + 站表）—— 只作参考
  station_osm / station_custom / coord_override   车站与坐标
  curate / train_override / route_* / trains      车次与校对结果
  整理台 linedesk.db 同样清空（清前整个库文件一并备份）

用法：

  python scripts/reset_lines.py                     # 只看会清什么（不写库）
  python scripts/reset_lines.py --yes               # 备份后清空
  python scripts/reset_lines.py --yes --keep-linedesk   # 整理台保留
  python scripts/reset_lines.py --restore data/backup/lines-reset-20261102-101500

备份目录里放：rail.db / linedesk.db / cnrail_lines.json / lines_export.json / README.md，
rail.db 是清空前的整库副本，还原直接覆盖即可；lines_export.json 是逐表的行级导出，
万一只想捞回某一条线也能在 JSON 里找。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(BASE, "webapp", "data", "rail.db")
DESK = os.path.join(BASE, "webapp", "data", "linedesk.db")
LINES_JSON = os.path.join(BASE, "data", "cnrail_lines.json")
BACKUP_ROOT = os.path.join(BASE, "data", "backup")

# rail.db 里要清的线路表（顺序无关，全在一个事务里）
RAIL_TABLES = [
    "line_custom", "line_override", "line_src_pick", "line_src_fix",
    "line_osm", "line_osm_stop", "line_ce", "line_ce_stop",
    "station_osm_line", "line_geom",
]
# 要保留的（写进说明，顺便自检用）
KEPT_TABLES = ["line_catalog", "line_catalog_stop", "station_osm",
               "station_custom", "coord_override", "curate", "trains"]
DESK_TABLES = ["ld_alias", "ld_line", "ld_relation", "ld_stop", "ld_log", "ld_meta"]


def _tables(conn: sqlite3.Connection) -> set:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _count(conn: sqlite3.Connection, table: str) -> int:
    try:
        return conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
    except sqlite3.DatabaseError:
        return -1


def survey() -> dict:
    """清空前的家底"""
    out = {"rail": {}, "desk": {}, "base": 0}
    if os.path.exists(DB):
        with sqlite3.connect(DB) as c:
            have = _tables(c)
            for t in RAIL_TABLES + KEPT_TABLES:
                if t in have:
                    out["rail"][t] = _count(c, t)
    if os.path.exists(DESK):
        with sqlite3.connect(DESK) as c:
            have = _tables(c)
            for t in DESK_TABLES:
                if t in have:
                    out["desk"][t] = _count(c, t)
    if os.path.exists(LINES_JSON):
        try:
            with open(LINES_JSON, encoding="utf-8") as f:
                out["base"] = len(json.load(f) or [])
        except (OSError, ValueError):
            out["base"] = -1
    return out


def _dump_rows(conn: sqlite3.Connection, table: str) -> list:
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM %s" % table)]
    except sqlite3.DatabaseError:
        return []


def backup(tag: str = "") -> str:
    """把库、底表、整理台按行导出到 data/backup/lines-reset-<时间>/"""
    tag = tag or time.strftime("%Y%m%d-%H%M%S")
    dst = os.path.join(BACKUP_ROOT, "lines-reset-" + tag)
    os.makedirs(dst, exist_ok=True)

    if os.path.exists(DB):
        # 先把 WAL 落进主库，复制才是完整的一份（服务在跑也能做，checkpoint 只是排队等锁）
        try:
            with sqlite3.connect(DB, timeout=30) as c:
                c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.DatabaseError:
            pass
        shutil.copy2(DB, os.path.join(dst, "rail.db"))
        # checkpoint(TRUNCATE) 之后 WAL 是空的；-shm 是共享内存索引，不用备份
        wal = DB + "-wal"
        if os.path.exists(wal) and os.path.getsize(wal) > 0:
            shutil.copy2(wal, os.path.join(dst, "rail.db-wal"))

    if os.path.exists(DESK):
        try:
            with sqlite3.connect(DESK, timeout=30) as c:
                c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.DatabaseError:
            pass
        shutil.copy2(DESK, os.path.join(dst, "linedesk.db"))

    if os.path.exists(LINES_JSON):
        shutil.copy2(LINES_JSON, os.path.join(dst, "cnrail_lines.json"))

    export = {"takenAt": time.strftime("%Y-%m-%d %H:%M:%S"), "rail": {}, "desk": {},
              "baseLines": []}
    if os.path.exists(DB):
        with sqlite3.connect(DB) as c:
            have = _tables(c)
            for t in RAIL_TABLES:
                if t in have:
                    export["rail"][t] = _dump_rows(c, t)
    if os.path.exists(DESK):
        with sqlite3.connect(DESK) as c:
            have = _tables(c)
            for t in DESK_TABLES:
                if t in have:
                    export["desk"][t] = _dump_rows(c, t)
    if os.path.exists(LINES_JSON):
        try:
            with open(LINES_JSON, encoding="utf-8") as f:
                export["baseLines"] = json.load(f) or []
        except (OSError, ValueError):
            export["baseLines"] = []
    with open(os.path.join(dst, "lines_export.json"), "w", encoding="utf-8") as f:
        json.dump(export, f, ensure_ascii=False)

    with open(os.path.join(dst, "README.md"), "w", encoding="utf-8") as f:
        f.write(_readme(export, dst))
    return dst


def _readme(export: dict, dst: str) -> str:
    rows = []
    for t, recs in export.get("rail", {}).items():
        rows.append("| %s | %d |" % (t, len(recs)))
    for t, recs in export.get("desk", {}).items():
        rows.append("| 整理台 %s | %d |" % (t, len(recs)))
    rows.append("| 底表 cnrail_lines.json | %d |" % len(export.get("baseLines") or []))
    return """# 线路清空前的备份（%s）

这个目录是 `scripts/reset_lines.py` 在清空线路之前做的完整备份。

## 里面有什么

| 内容 | 行数 |
|---|---|
%s

- `rail.db` —— 清空前的**整库副本**（含车次、经停、校对、资料库、所有线路）
- `linedesk.db` —— 整理台整库副本
- `cnrail_lines.json` —— 清空前的底表（417 条带站序的线路）
- `lines_export.json` —— 线路相关表的行级导出（JSON），只想捞某一条线时在这里找

## 怎么还原

```
python scripts/reset_lines.py --restore %s
```

会把 rail.db / linedesk.db / cnrail_lines.json 原样拷回去（覆盖当前库）。
只想捞一条线：在 `lines_export.json` 里按线名搜 `line_custom` / `line_osm_stop` 那些数组，
再手工导回去，或者直接用 GeoJSON 重新导入。
""" % (export.get("takenAt") or "", "\n".join(rows), dst.replace(BASE + os.sep, ""))


def clear(keep_linedesk: bool = False) -> dict:
    done = {"rail": {}, "desk": {}, "base": 0}
    if os.path.exists(DB):
        with sqlite3.connect(DB, timeout=30) as c:
            have = _tables(c)
            for t in RAIL_TABLES:
                if t not in have:
                    continue
                done["rail"][t] = c.execute("DELETE FROM %s" % t).rowcount
            if "station_osm" in have:
                # 车站本身留着（站名 + 坐标还有用），但「属于哪条线」已经不存在了
                try:
                    c.execute("UPDATE station_osm SET n_lines=0")
                except sqlite3.DatabaseError:
                    pass
            c.commit()
    if os.path.exists(LINES_JSON):
        with open(LINES_JSON, encoding="utf-8") as f:
            done["base"] = len(json.load(f) or [])
        with open(LINES_JSON, "w", encoding="utf-8") as f:
            f.write("[]")
    if not keep_linedesk and os.path.exists(DESK):
        with sqlite3.connect(DESK, timeout=30) as c:
            have = _tables(c)
            for t in DESK_TABLES:
                if t in have:
                    done["desk"][t] = c.execute("DELETE FROM %s" % t).rowcount
            c.commit()
    return done


def restore(src: str) -> None:
    if not os.path.isdir(src):
        raise SystemExit("没有这个备份目录：" + src)
    pairs = [("rail.db", DB), ("linedesk.db", DESK), ("cnrail_lines.json", LINES_JSON)]
    for name, target in pairs:
        p = os.path.join(src, name)
        if not os.path.exists(p):
            continue
        for ext in ("-wal", "-shm"):        # 旧的 WAL 不删干净，还原出来的库会串
            if os.path.exists(target + ext):
                os.remove(target + ext)
        shutil.copy2(p, target)
        print("  还原", name, "→", target.replace(BASE + os.sep, ""))


def main() -> int:
    ap = argparse.ArgumentParser(description="清空所有线路（保留线路资料库）")
    ap.add_argument("--yes", action="store_true", help="真的清（不给就只打印计划）")
    ap.add_argument("--keep-linedesk", action="store_true", help="整理台不动")
    ap.add_argument("--restore", default="", help="从备份目录还原")
    ap.add_argument("--tag", default="", help="备份目录后缀，默认时间戳")
    args = ap.parse_args()

    if args.restore:
        print("从备份还原：", args.restore)
        restore(args.restore)
        print("完事。**重启服务**（改库不重启，内存里还是老数据）")
        return 0

    st = survey()
    print("清空前：")
    for t, n in st["rail"].items():
        if t in RAIL_TABLES:
            print("  清  %-20s %6d" % (t, n))
    print("  清  底表 cnrail_lines.json %4d" % st["base"])
    for t, n in st["rail"].items():
        if t in KEPT_TABLES:
            print("  留  %-20s %6d" % (t, n))
    for t, n in st["desk"].items():
        print("  %s 整理台 %-14s %6d" % ("留" if args.keep_linedesk else "清", t, n))

    if not args.yes:
        print("\n这只是预演，没有动数据。确认无误加 --yes：")
        print("  python scripts/reset_lines.py --yes")
        return 0

    dst = backup(args.tag)
    print("\n已备份到：", dst.replace(BASE + os.sep, ""))
    done = clear(args.keep_linedesk)
    print("已清空：")
    for t, n in done["rail"].items():
        print("  %-20s -%d" % (t, n))
    if done["base"]:
        print("  底表 cnrail_lines.json -%d" % done["base"])
    for t, n in done["desk"].items():
        print("  整理台 %-14s -%d" % (t, n))
    print("\n线路资料库（line_catalog）没有动。**重启服务**才生效：")
    print("  taskkill /F /IM python.exe  然后 python webapp/server.py --port 8686")
    return 0


if __name__ == "__main__":
    sys.exit(main())
