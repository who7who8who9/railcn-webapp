# -*- coding: utf-8 -*-
"""经停站存储回归：一天一条指针 + 内容按趟去重。

覆盖：
  1. 停站指纹（只认「哪一站、几点到、几点开、车次号」，余票数之类的变动不算变）
  2. put_route 去重：21 天一模一样 → 1 份内容 + 21 条日期指针
  3. 停站真变了 → 新版本，versions() 给出时间线
  4. 老库迁移：整表折叠后逐行比对 raw，一条不差；视图只读、字段与老表一致
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(BASE, "webapp")
if WEB not in sys.path:
    sys.path.insert(0, WEB)

import routestore  # noqa: E402


def stop(name, no, arr, dep, code="G1"):
    return {
        "station_name": name, "station_no": f"{no:02d}",
        "arrive_time": arr, "start_time": dep, "station_train_code": code,
        "arrive_day_diff": "0", "train_class_name": "高速",
        "is_start": "N", "start_station_name": "北京南", "end_station_name": "上海虹桥",
        "wz_num": "--", "running_time": "00:00", "service_type": "1",
        "arrive_day_str": "当日到达",
    }


BASE_STOPS = [stop("北京南", 1, "----", "06:30"), stop("济南西", 2, "07:50", "07:53"),
              stop("南京南", 3, "10:10", "10:15"), stop("上海虹桥", 4, "11:30", "----")]


def new_db() -> sqlite3.Connection:
    fd, path = tempfile.mkstemp(suffix=".db", prefix="rt-")
    os.close(fd)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    routestore.ensure_schema(c)
    return c


def test_fingerprint() -> None:
    a = json.dumps(BASE_STOPS, ensure_ascii=False)
    shuffled = json.dumps([dict(reversed(list(s.items()))) for s in BASE_STOPS], ensure_ascii=False)
    assert routestore.stop_key(a) == routestore.stop_key(shuffled), "键序不同不该算变"
    dirty = [dict(s) for s in BASE_STOPS]
    dirty[1]["wz_num"] = "12"
    assert routestore.stop_key(a) == routestore.stop_key(dirty), "余票数变化不该算变"
    retimed = [dict(s) for s in BASE_STOPS]
    retimed[1]["start_time"] = "07:55"
    assert routestore.stop_key(a) != routestore.stop_key(retimed), "时刻变了该算变"
    assert routestore.stop_key(a) != routestore.stop_key(BASE_STOPS[:3]), "少一站该算变"
    assert routestore.stop_key(None) == "" and routestore.stop_key("not json").startswith("raw:")
    print("  指纹：键序/余票不算变，时刻/站数算变 ✓")


def test_dedup() -> None:
    c = new_db()
    raw = json.dumps(BASE_STOPS, ensure_ascii=False)
    for day in range(1, 22):
        date = f"2026-11-{day:02d}"
        c.execute("BEGIN")
        routestore.put_route(c, "24000000G10L", date, "G1", raw)
        c.commit()
    days = c.execute("SELECT COUNT(*) FROM route_day").fetchone()[0]
    tpls = c.execute("SELECT COUNT(*) FROM route_tpl").fetchone()[0]
    hits = c.execute("SELECT hits FROM route_tpl").fetchone()[0]
    assert days == 21, f"日期指针该有 21 条，实际 {days}"
    assert tpls == 1, f"内容相同只该存 1 份，实际 {tpls}"
    assert hits == 20, f"21 天里 20 次命中已有版本，实际 {hits}"
    # 视图读出来还是那份 raw
    got = c.execute("SELECT raw FROM routes WHERE train_no=? AND date=?",
                    ("24000000G10L", "2026-11-05")).fetchone()
    assert json.loads(got["raw"]) == BASE_STOPS
    print(f"  {days} 天 → {tpls} 份内容 + {days} 条指针，视图读回原文 ✓")

    # 第 22 天改点：南京南多停 2 分钟
    changed = [dict(s) for s in BASE_STOPS]
    changed[2]["start_time"] = "10:17"
    routestore.put_route(c, "24000000G10L", "2026-11-22", "G1",
                         json.dumps(changed, ensure_ascii=False))
    tpls = c.execute("SELECT COUNT(*) FROM route_tpl").fetchone()[0]
    assert tpls == 2, f"改了停站该多一份版本，实际 {tpls}"
    vs = routestore.versions(c, "24000000G10L")
    assert len(vs) == 2, f"版本时间线该 2 段，实际 {vs}"
    assert vs[0]["from"] == "2026-11-01" and vs[0]["to"] == "2026-11-21" and vs[0]["days"] == 21
    assert vs[1]["from"] == "2026-11-22" and vs[1]["days"] == 1
    print(f"  改点后：{len(vs)} 个版本，{vs[0]['from']}~{vs[0]['to']} / {vs[1]['from']} 起 ✓")

    # 同一天重抓（内容回到老版本）应覆盖指针，不多存版本
    routestore.put_route(c, "24000000G10L", "2026-11-22", "G1", raw)
    tpls = c.execute("SELECT COUNT(*) FROM route_tpl").fetchone()[0]
    days = c.execute("SELECT COUNT(*) FROM route_day").fetchone()[0]
    assert tpls == 2 and days == 22, (tpls, days)
    print("  同日重抓只改指针，不新增日期行 ✓")
    c.close()


def make_legacy(path: str) -> None:
    """造一个老结构的库：3 趟车 × 若干天，其中一趟中途改点"""
    c = sqlite3.connect(path)
    c.executescript(
        """
        CREATE TABLE routes (
            train_no TEXT NOT NULL, date TEXT NOT NULL, code TEXT,
            raw TEXT, updated_at REAL, PRIMARY KEY (train_no, date)
        );
        """
    )
    rows = []
    for day in range(1, 8):
        date = f"2026-11-{day:02d}"
        rows.append(("24000000G10L", date, "G1", json.dumps(BASE_STOPS, ensure_ascii=False), 1.0))
        rows.append(("24000000D20L", date, "D2",
                     json.dumps([stop("北京南", 1, "----", "08:00", "D2"),
                                 stop("天津南", 2, "08:30", "08:32", "D2"),
                                 stop("青岛北", 3, "11:40", "----", "D2")], ensure_ascii=False), 1.0))
        # 这趟车从 11-05 起站序不同
        st = list(BASE_STOPS)
        if day >= 5:
            st = st + [stop("杭州东", 5, "12:30", "----")]
        rows.append(("24000000K30L", date, "K3", json.dumps(st, ensure_ascii=False), 1.0))
    c.executemany("INSERT INTO routes (train_no,date,code,raw,updated_at) VALUES (?,?,?,?,?)", rows)
    c.commit()
    c.close()


def test_migrate() -> None:
    fd, path = tempfile.mkstemp(suffix=".db", prefix="rt-mig-")
    os.close(fd)
    os.unlink(path)
    make_legacy(path)
    before = {}
    c0 = sqlite3.connect(path)
    for tn, d, code, raw in c0.execute("SELECT train_no,date,code,raw FROM routes"):
        before[(tn, d)] = (code, raw)
    c0.close()

    import subprocess
    r = subprocess.run(
        [sys.executable, os.path.join(BASE, "scripts", "migrate_routes_dedup.py"),
         "--apply", "--db", path, "--skip-survey"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stdout + r.stderr

    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    kind = c.execute("SELECT type FROM sqlite_master WHERE name='routes'").fetchone()[0]
    assert kind == "view", f"迁移后 routes 该是视图，实际 {kind}"
    got = {(r["train_no"], r["date"]): (r["code"], r["raw"])
           for r in c.execute("SELECT train_no,date,code,raw FROM routes")}
    assert got == before, "折叠前后每一行的 code / raw 必须一致"
    days = c.execute("SELECT COUNT(*) FROM route_day").fetchone()[0]
    tpls = c.execute("SELECT COUNT(*) FROM route_tpl").fetchone()[0]
    assert days == len(before), (days, len(before))
    # 21 行：G1 7 份重复 → 1 个版本；D2 7 份重复 → 1；K3 两段 → 2
    assert tpls == 4, f"该有 4 份内容（G1/D2 各 1、K3 2 份），实际 {tpls}"
    vs = routestore.versions(c, "24000000K30L")
    assert [v["days"] for v in vs] == [4, 3], vs
    print(f"  老库迁移：{days} 行 → {days} 条指针 + {tpls} 份内容，"
          f"逐行 raw 一致；K3 版本段 {[v['days'] for v in vs]} ✓")

    # 视图只读
    try:
        c.execute("INSERT INTO routes (train_no,date,code,raw) VALUES ('x','2026-01-01','X1','[]')")
        raise AssertionError("routes 是视图，不该能直接 INSERT")
    except sqlite3.OperationalError:
        c.rollback()
    # 迁移幂等
    r = subprocess.run(
        [sys.executable, os.path.join(BASE, "scripts", "migrate_routes_dedup.py"),
         "--apply", "--db", path, "--skip-survey"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0 and "不用迁移" in r.stdout, r.stdout
    print("  视图只读、重复迁移直接跳过 ✓")
    c.close()


def test_collect_path() -> None:
    """采集的批量写入同一套逻辑：隔天重抓同一份内容不新增版本"""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="rt-col-")
    os.close(fd)
    os.unlink(path)
    import railcore
    old = railcore.DB_PATH
    railcore.DB_PATH = path
    try:
        railcore.init_db()
        import collector
        rows = [
            ("T1", "G1", "G", "北京南", "上海虹桥", 4, json.dumps(BASE_STOPS, ensure_ascii=False)),
            ("T2", "D2", "D", "北京南", "青岛北", 3, json.dumps(
                [stop("北京南", 1, "----", "08:00", "D2"),
                 stop("天津南", 2, "08:30", "08:32", "D2"),
                 stop("青岛北", 3, "11:40", "----", "D2")], ensure_ascii=False)),
        ]
        collector._save_batch("2026-11-20", rows)
        collector._save_batch("2026-11-21", rows)        # 第二天照抓，内容一样
        c = sqlite3.connect(path)
        c.row_factory = sqlite3.Row
        nd = c.execute("SELECT COUNT(*) FROM route_day").fetchone()[0]
        nt = c.execute("SELECT COUNT(*) FROM route_tpl").fetchone()[0]
        ntr = c.execute("SELECT COUNT(*) FROM trains").fetchone()[0]
        assert (nd, nt, ntr) == (4, 2, 4), (nd, nt, ntr)
        got = c.execute("SELECT raw FROM routes WHERE train_no='T1' AND date='2026-11-21'").fetchone()
        assert json.loads(got["raw"]) == BASE_STOPS
        c.close()
        print(f"  采集写两天：{nd} 条指针 / {nt} 份内容 / trains {ntr} 行 ✓")
    finally:
        railcore.DB_PATH = old


if __name__ == "__main__":
    print("== 指纹 =="); test_fingerprint()
    print("== 去重 =="); test_dedup()
    print("== 迁移 =="); test_migrate()
    print("== 采集写入 =="); test_collect_path()
    print("\n全部通过")
