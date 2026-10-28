# -*- coding: utf-8 -*-
"""把老 routes 表折叠成「route_tpl + route_day + routes 视图」。

老结构每天存一整份 12306 经停站 JSON；同一趟车停站不变时每天抓到的内容逐字节
相同，25 天下来 24.6 万行里有 95% 是重复的。新结构只存不同版本，每天留一条指针。

用法：
    python scripts/migrate_routes_dedup.py            # 只看统计（不写库）
    python scripts/migrate_routes_dedup.py --apply    # 真正迁移
    python scripts/migrate_routes_dedup.py --apply --vacuum   # 迁移后回收文件空间

幂等：中途失败可重跑；已经是新结构就直接退出。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(BASE, "webapp")
if WEB not in sys.path:
    sys.path.insert(0, WEB)

import routestore  # noqa: E402

DB_PATH = os.path.join(WEB, "data", "rail.db")
BATCH = 2000


def conn_open() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=60)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=60000")
    return c


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def survey(conn) -> dict:
    """统计老表：行数 / 字节 / 唯一内容 / 有停站变化的车次"""
    rows = conn.execute("SELECT train_no, date, code, raw FROM routes").fetchall()
    total = len(rows)
    total_bytes = sum(len(r["raw"] or "") for r in rows)
    seen: dict = {}
    per_train: dict = {}
    for r in rows:
        fp = routestore.stop_key(r["raw"] or "")
        tn = r["train_no"]
        seen.setdefault((tn, fp), len(r["raw"] or ""))
        per_train.setdefault(tn, set()).add(fp)
    changed = {tn: f for tn, f in per_train.items() if len(f) > 1}
    dates = len({r["date"] for r in rows})
    return {
        "rows": total,
        "bytes": total_bytes,
        "tpls": len(seen),
        "tplBytes": sum(seen.values()),
        "trains": len(per_train),
        "changed": changed,
        "dates": dates,
    }


def show(s: dict) -> None:
    print("== 现状 ==")
    print(f"  routes 行数       {s['rows']:,}（{s['dates']} 个日期、{s['trains']:,} 趟车）")
    print(f"  经停站 JSON 体积   {human(s['bytes'])}")
    print(f"  不同内容份数       {s['tpls']:,}")
    print(f"  去重后体积         {human(s['tplBytes'])}"
          f"（省 {human(s['bytes'] - s['tplBytes'])}，"
          f"{(1 - s['tplBytes'] / s['bytes']) * 100 if s['bytes'] else 0:.1f}%）")
    print(f"  每天指针开销       ≈ {human(s['rows'] * 60)}（一条几十字节，可忽略）")
    print(f"  停站变过的车次     {len(s['changed']):,}")
    if s["changed"]:
        print("    （同一趟车在不同日期有不同停站，保存时会各留一份）")


def collapse(conn, verbose: bool = True) -> dict:
    kind = routestore.table_kind(conn, "routes")
    if kind == "view":
        print("已经是新结构（routes 是视图），不用迁移。")
        return {"skipped": True}
    if kind != "table":
        print("库里没有 routes 表，按全新库处理。")
        routestore.ensure_schema(conn)
        conn.commit()
        return {"skipped": True}

    t0 = time.time()
    conn.execute("ALTER TABLE routes RENAME TO routes_old")
    conn.commit()
    routestore.create_schema(conn)
    conn.commit()

    cache: dict = {}
    last = -1
    done = 0
    while True:
        rows = conn.execute(
            "SELECT rowid AS rid, train_no, date, code, raw, updated_at"
            "  FROM routes_old WHERE rowid > ? ORDER BY rowid LIMIT ?",
            (last, BATCH),
        ).fetchall()
        if not rows:
            break
        for r in rows:
            if not r["train_no"] or not r["raw"]:
                continue
            routestore.put_route(conn, r["train_no"], r["date"], r["code"], r["raw"],
                                 updated_at=r["updated_at"], cache=cache)
        conn.commit()
        done += len(rows)
        last = rows[-1]["rid"]
        if verbose:
            print(f"  折叠 {done:,} / 行 …", end="\r", flush=True)

    days = conn.execute("SELECT COUNT(*) FROM route_day").fetchone()[0]
    tpls = conn.execute("SELECT COUNT(*) FROM route_tpl").fetchone()[0]
    conn.execute("DROP TABLE routes_old")
    routestore.create_view(conn)
    conn.commit()
    print(f"\n  折叠完成：{done:,} 行 → {days:,} 条日期指针 + {tpls:,} 份内容"
          f"，用时 {time.time() - t0:.1f}s")
    return {"days": days, "tpls": tpls, "rows": done}


def vacuum(conn) -> None:
    print("  回收文件空间（VACUUM）…", end="", flush=True)
    t0 = time.time()
    conn.execute("VACUUM")
    print(f" 完成 {time.time() - t0:.1f}s")


def report_versions(conn, limit: int = 10) -> None:
    """迁移后：哪些车次的停站真的变过（版本时间线）"""
    rows = conn.execute(
        """SELECT train_no, COUNT(DISTINCT tpl_id) n FROM route_day
            GROUP BY train_no HAVING n > 1 ORDER BY n DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    total = conn.execute(
        """SELECT COUNT(*) FROM (
               SELECT train_no FROM route_day GROUP BY train_no HAVING COUNT(DISTINCT tpl_id) > 1
           )"""
    ).fetchone()[0]
    print(f"\n== 停站变过的车次：{total:,} 趟 ==")
    for r in rows:
        vs = routestore.versions(conn, r["train_no"])
        seg = " → ".join(f"{v['from']}~{v['to']}({v['nStops']}站)" for v in vs[:4])
        code = vs[0]["code"] if vs else r["train_no"]
        print(f"  {code:<8} {len(vs)} 个版本：{seg}")


def main() -> None:
    global DB_PATH
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正迁移（默认只看统计）")
    ap.add_argument("--vacuum", action="store_true", help="迁移后 VACUUM 回收文件空间")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--skip-survey", action="store_true", help="跳过迁移前的全表统计（省时间）")
    args = ap.parse_args()

    DB_PATH = args.db
    if not os.path.exists(DB_PATH):
        print(f"找不到库：{DB_PATH}")
        return

    conn = conn_open()
    try:
        if not args.skip_survey:
            show(survey(conn))
        if not args.apply:
            print("\n（只是预览。加 --apply 才真正迁移）")
            return
        before = os.path.getsize(DB_PATH)
        print("\n== 迁移 ==")
        collapse(conn)
        if args.vacuum:
            vacuum(conn)
        after = os.path.getsize(DB_PATH)
        print(f"  文件大小 {human(before)} → {human(after)}")
        report_versions(conn)
        print("\n完成。服务重启后生效（routes 现在是只读视图，写入走 routestore）。")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
