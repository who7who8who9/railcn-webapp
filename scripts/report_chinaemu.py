# -*- coding: utf-8 -*-
"""
对账：china-emu 抓下来的线路 vs 现有四套来源，只出报告不写库。

现有四套：
  ① cnrail 底表       data/cnrail_lines.json          417 条（按「X线Y段」命名）
  ② 线路资料库        line_catalog / _stop            844 条（辉睿，610 条有站序）
  ③ OSM / CRTrainRoute data/crtrain/lines.json        3007 线名（只有走向）
  ④ 自建线路          line_custom                     62 条

重点回答三件事：
  A. china-emu 的线名能对上现有哪套、对不上多少
  B. 里程缺口能补多少 —— 资料库有 285 条线路站序完整但里程全空，
     china-emu 每站都有计价里程，站名能对上的就能补
  C. china-emu 独家（四套都没有）的线路有多少

用法：
    python scripts/report_chinaemu.py
    python scripts/report_chinaemu.py --top 30
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from typing import Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "webapp"))

import cnrail                                                    # noqa: E402
import railcore                                                  # noqa: E402
import railgeom                                                  # noqa: E402
import lineseg                                                   # noqa: E402

SRC = os.path.join(ROOT, "data", "chinaemu", "lines.json")

HS_RE = re.compile(r"高速|客专|客运专线|城际|高铁")
_TAILS = ("高速铁路", "客运专线", "城际铁路", "高速线", "客专线", "铁路", "高铁", "线")


def core(name: str) -> str:
    """去掉「铁路 / 线 / 高铁 …」这类不影响线路身份的尾巴"""
    s = re.sub(r"\s+", "", name or "")
    changed = True
    while changed:
        changed = False
        for t in _TAILS:
            if len(s) > len(t) and s.endswith(t):
                s = s[: -len(t)]
                changed = True
                break
    return s


def is_hs(name: str) -> bool:
    return bool(HS_RE.search(name or ""))


def nkey(name: str) -> str:
    """线名 → 可比的键。高铁/普速分开，分段线拆成「父线|段名」。"""
    hs = "H" if is_hs(name) else "P"
    sp = lineseg.split(name)
    if sp:
        return hs + core(sp[0]) + "|" + sp[1]
    return hs + core(name)


def skey(name: str) -> str:
    """站名键，与 cnrail._key 一致"""
    return cnrail._key(name or "")


def load_chinaemu() -> List[dict]:
    if not os.path.exists(SRC):
        print("缺少", SRC, "——先跑 python scripts/fetch_chinaemu.py")
        sys.exit(1)
    with open(SRC, encoding="utf-8") as f:
        return json.load(f)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=15, help="每节列前几条")
    args = ap.parse_args()

    ce = load_chinaemu()
    print("=" * 78)
    print("china-emu 抓取概览")
    print("=" * 78)
    n_stop = sum(r["n_stop"] for r in ce)
    n_km = sum(r["n_km"] for r in ce)
    n_st = sum(r["n_station"] for r in ce)
    print("  线路 %d 条 · 站行 %d（车站 %d，有里程 %d，覆盖 %.1f%%）"
          % (len(ce), n_stop, n_st, n_km, 100.0 * n_km / max(1, n_stop)))
    cats: Dict[str, int] = {}
    for r in ce:
        for c in r.get("cats") or ["?"]:
            cats[c] = cats.get(c, 0) + 1
    print("  分类:", cats)

    # ---------------------------------------------------------------- 索引
    lib = {it["name"]: it for it in cnrail.search_line("", 5000)}
    lib_by_key = {nkey(n): n for n in lib}

    conn = sqlite3.connect(railcore.DB_PATH)
    conn.row_factory = sqlite3.Row
    cat_rows = list(conn.execute("SELECT id,name,title,length_km,n_stop FROM line_catalog"))
    cat_by_key: Dict[str, dict] = {}
    for r in cat_rows:
        for nm in (r["name"], r["title"]):
            if nm:
                cat_by_key.setdefault(nkey(nm), r)

    osm_names = set()
    p = os.path.join(ROOT, "data", "crtrain", "lines.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            osm_names = {m["name"] for m in json.load(f)["lines"] if m.get("name")}

    custom = {r["name"] for r in conn.execute("SELECT name FROM line_custom") if r["name"]}

    # ---------------------------------------------------------------- A 覆盖
    print()
    print("=" * 78)
    print("A. china-emu 的线名能对上谁")
    print("=" * 78)
    hit_cn = hit_cat = hit_osm = hit_cus = 0
    only_ce: List[dict] = []
    for r in ce:
        nm = r["name"]
        k = nkey(nm)
        hc = nm in lib or k in lib_by_key
        hk = k in cat_by_key
        ho = nm in osm_names
        hu = nm in custom
        hit_cn += hc
        hit_cat += hk
        hit_osm += ho
        hit_cus += hu
        if not (hc or hk or ho or hu):
            only_ce.append(r)
    n = len(ce)
    print("  对上 cnrail 底表   %3d / %d  (%.0f%%)" % (hit_cn, n, 100.0 * hit_cn / n))
    print("  对上 线路资料库     %3d / %d  (%.0f%%)" % (hit_cat, n, 100.0 * hit_cat / n))
    print("  对上 OSM 线名       %3d / %d  (%.0f%%)" % (hit_osm, n, 100.0 * hit_osm / n))
    print("  对上 自建线路       %3d / %d  (%.0f%%)" % (hit_cus, n, 100.0 * hit_cus / n))
    print("  四套全无（独家）    %3d / %d  (%.0f%%)" % (len(only_ce), n, 100.0 * len(only_ce) / n))
    only_ce.sort(key=lambda r: -(r.get("totalKm") or 0))
    print("\n  独家且 ≥50 km 的前 %d 条：" % args.top)
    shown = 0
    for r in only_ce:
        if (r.get("totalKm") or 0) < 50:
            continue
        print("    %-22s %7.1f km  %3d 站行  %s"
              % (r["name"], r["totalKm"] or 0, r["n_stop"], "/".join(r.get("cats") or [])))
        shown += 1
        if shown >= args.top:
            break

    # ------------------------------------------------------------ B 里程缺口
    print()
    print("=" * 78)
    print("B. 里程缺口能补多少（资料库站序完整但里程全空）")
    print("=" * 78)
    empty = list(conn.execute("""
        SELECT c.id, c.name, c.title, c.n_stop, c.length_km
          FROM line_catalog c
          JOIN (SELECT lid FROM line_catalog_stop
                 GROUP BY lid
                HAVING SUM(CASE WHEN km IS NOT NULL THEN 1 ELSE 0 END) = 0
                   AND COUNT(*) >= 2) e ON e.lid = c.id
    """))
    print("  资料库里程全空的线路：%d 条" % len(empty))
    # name/title → china-emu 记录
    ce_by_key = {nkey(r["name"]): r for r in ce}

    fill_lines = 0
    fill_stops = 0
    detail = []
    for row in empty:
        hit = None
        for nm in (row["name"], row["title"]):
            if not nm:
                continue
            hit = ce_by_key.get(nkey(nm))
            if hit:
                break
        if not hit:
            continue
        # 站名比对：资料库站序 vs china-emu 站序
        lib_stops = [skey(x["name"]) for x in conn.execute(
            "SELECT name FROM line_catalog_stop WHERE lid=? ORDER BY seq", (row["id"],))]
        ce_stops = {skey(s["name"]): s["km"] for s in hit["stops"] if s["km"] is not None}
        common = [s for s in lib_stops if s in ce_stops]
        if len(common) < 2:
            continue
        cov = len(common) / max(1, len(lib_stops))
        if cov < 0.5:
            continue
        fill_lines += 1
        fill_stops += len(common)
        detail.append((row["name"], row["n_stop"], len(hit["stops"]),
                       len(common), cov, hit["name"], hit["totalKm"]))

    print("  china-emu 能补的：%d 条线路，%d 个站的里程" % (fill_lines, fill_stops))
    detail.sort(key=lambda x: -x[2])
    print("\n  前 %d 条：" % args.top)
    print("    %-20s %5s %6s %6s %6s  %s" % ("资料库线名", "站数", "对方行", "可补站", "覆盖", "china-emu 线名"))
    for nm, ns, nc, ncm, cov, cen, ckm in detail[: args.top]:
        print("    %-20s %5d %6d %6d %5.0f%%  %s (%.0f km)"
              % (nm[:20], ns or 0, nc, ncm, cov * 100, cen[:24], ckm or 0))

    # ------------------------------------------------------------ C 站序互补
    print()
    print("=" * 78)
    print("C. 站序互补：china-emu 有站序、现有来源没有的")
    print("=" * 78)
    print("  （cnrail 底表 417 条站序齐全，资料库 610 条有站序；")
    print("    这里看 china-emu 能否给「有走向但没站序」的 OSM 线名补站）")
    osm_no_stop = [m for m in (json.load(open(p, encoding="utf-8"))["lines"]
                               if os.path.exists(p) else []) if (m.get("km") or 0) >= 30]
    can_fill = 0
    sample = []
    for m in osm_no_stop:
        k = nkey(m["name"])
        r = ce_by_key.get(k)
        if r and r["n_station"] >= 2:
            can_fill += 1
            sample.append((m["name"], m.get("km") or 0, r["name"], r["n_station"]))
    print("  OSM ≥30 km 线名 %d 条，china-emu 能补站序的 %d 条" % (len(osm_no_stop), can_fill))
    sample.sort(key=lambda x: -x[1])
    for nm, km, cen, ns in sample[: args.top]:
        print("    %-20s %7.1f km → %s（%d 站）" % (nm, km, cen[:22], ns))


if __name__ == "__main__":
    main()
