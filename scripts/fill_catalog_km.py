# -*- coding: utf-8 -*-
"""
用 china-emu 的逐站计价里程，补 line_catalog_stop 里空着的 km。

背景：线路资料库（辉睿）10377 行站序里只有 5420 行有里程，610 条有站序的线路里
有 277 条**里程全空**（侯西线 42 站一个里程都没有）。china-emu 每站都有到起点的
计价里程，两者口径一致（都从本线起点算），所以可以补。

三条硬约束：
  1. **只填空值**，已有里程一个不动（那是辉睿的数据，可能是权威值）
  2. **口径对齐**：两边共有的站算出偏移量（中位数），china-emu 的里程整体平移后再填，
     不假设两边 0 点一定一致
  3. **单调校验**：填完该线路的里程必须单调不减，否则整条不填（宁可空着也不写坏）

用法：
    python scripts/fill_catalog_km.py                 # 只报告
    python scripts/fill_catalog_km.py --apply         # 写库（先备份）
    python scripts/fill_catalog_km.py --min-cover 0.8 # 只处理站名重合度更高的
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import statistics
import sys
import time
from typing import Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "webapp"))

import cnrail                                                    # noqa: E402
import railcore                                                  # noqa: E402
import lineseg                                                   # noqa: E402


def _skey(name: str) -> str:
    return cnrail._key(name or "")


def plan(conn: sqlite3.Connection, min_cover: float) -> List[dict]:
    """算出每条线路要填哪些站的里程"""
    out: List[dict] = []
    rows = list(conn.execute("""
        SELECT id, name, title, length_km, n_stop
          FROM line_catalog
         WHERE id IN (SELECT lid FROM line_catalog_stop GROUP BY lid HAVING COUNT(*) >= 2)
         ORDER BY name
    """))
    for rec in rows:
        stops = list(conn.execute(
            "SELECT seq,name,km FROM line_catalog_stop WHERE lid=? ORDER BY seq",
            (rec["id"],)))
        if not stops:
            continue
        blanks = [s for s in stops if s["km"] is None]
        if not blanks:
            continue          # 里程已齐，不动

        ce = railcore.ce_stops(rec["name"]) or railcore.ce_stops(rec["title"] or "")
        if not ce:
            continue
        if (ce.get("kmDesc") or 0) > 0:
            continue          # 页面里程本身就乱，不信

        ce_map: Dict[str, float] = {}
        for s in ce["stops"]:
            nm = _skey(lineseg._ce_name(s["name"]))
            if nm and s["km"] is not None and nm not in ce_map:
                ce_map[nm] = float(s["km"])

        # 两边共有的、且资料库已有里程的站 → 算偏移
        pairs: List[Tuple[float, float]] = []
        for s in stops:
            k = _skey(s["name"])
            if k in ce_map and s["km"] is not None:
                pairs.append((float(s["km"]), ce_map[k]))
        offset = statistics.median(a - b for a, b in pairs) if pairs else 0.0

        # 能填的空站
        fills: List[Tuple[str, int, float]] = []
        for s in blanks:
            k = _skey(s["name"])
            if k in ce_map:
                fills.append((k, s["seq"], round(ce_map[k] + offset, 1)))
        if not fills:
            continue
        cover = len(fills) / max(1, len(blanks))
        if cover < min_cover:
            continue

        # 单调校验：把填后的完整序列拉出来看
        filled = {seq: km for _, seq, km in fills}
        seq_kms = [(s["seq"], filled.get(s["seq"], s["km"])) for s in stops]
        seq_kms = [(q, v) for q, v in seq_kms if v is not None]
        bad = sum(1 for a, b in zip(seq_kms, seq_kms[1:]) if b[1] < a[1] - 1.0)
        if bad:
            continue

        out.append({"id": rec["id"], "name": rec["name"], "title": rec["title"],
                    "n_stop": len(stops), "n_blank": len(blanks), "n_fill": len(fills),
                    "cover": cover, "offset": round(offset, 1),
                    "ceName": ce["name"], "fills": fills})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="写库（会先备份）")
    ap.add_argument("--min-cover", type=float, default=0.5,
                    help="空站里能填上的比例下限，默认 0.5")
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args()

    conn = sqlite3.connect(railcore.DB_PATH)
    conn.row_factory = sqlite3.Row
    todo = plan(conn, args.min_cover)
    todo.sort(key=lambda r: -r["n_fill"])

    total_stops = sum(r["n_fill"] for r in todo)
    print("可补的线路 %d 条，共 %d 个站的里程" % (len(todo), total_stops))
    print("\n%-22s %5s %6s %6s %6s %8s  %s"
          % ("资料库线名", "站数", "空里程", "可补", "覆盖", "偏移km", "china-emu 线名"))
    for r in todo[: args.top]:
        print("%-22s %5d %6d %6d %5.0f%% %8s  %s"
              % (r["name"][:22], r["n_stop"], r["n_blank"], r["n_fill"],
                 r["cover"] * 100, r["offset"], r["ceName"][:20]))

    if not args.apply:
        print("\n（只报告，未写库。加 --apply 才落库）")
        return

    bak = railcore.DB_PATH + ".bak-km-" + time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(railcore.DB_PATH, bak)
    print("\n已备份:", bak)

    n_updated = 0
    with conn:
        for r in todo:
            for _k, seq, km in r["fills"]:
                cur = conn.execute(
                    "UPDATE line_catalog_stop SET km=? WHERE lid=? AND seq=? AND km IS NULL",
                    (km, r["id"], seq))
                n_updated += cur.rowcount
            n_km = conn.execute(
                "SELECT COUNT(*) FROM line_catalog_stop WHERE lid=? AND km IS NOT NULL",
                (r["id"],)).fetchone()[0]
            conn.execute("UPDATE line_catalog SET n_km=? WHERE id=?", (n_km, r["id"]))
    print("已更新 %d 个站的里程，涉及 %d 条线路" % (n_updated, len(todo)))

    # 复核
    left = conn.execute("""
        SELECT COUNT(*) FROM (SELECT lid FROM line_catalog_stop
                               GROUP BY lid
                              HAVING SUM(CASE WHEN km IS NOT NULL THEN 1 ELSE 0 END)=0
                                 AND COUNT(*)>=2)
    """).fetchone()[0]
    print("里程仍全空的线路：%d 条（原 277）" % left)


if __name__ == "__main__":
    main()
