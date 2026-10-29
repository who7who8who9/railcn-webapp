# -*- coding: utf-8 -*-
"""
复核 line_catalog.line_name —— 资料库那条线到底对应线路库里的哪条线。

原来的 match_lines() 里，按站序匹配用的是
    len(交集) / min(资料库站数, 库内线站数)
分母取两边的较小值，于是**库内的小联络线一定能拿满分**：
侯西线（42 站 / 288.5 km）就这样被匹配到了「陇海西成联络线」（2 站 / 9 km），
因为后者那两站恰好都在侯西线的站序里，2/2 = 1.0。

改成：
  1. 覆盖率 = 交集 / 资料库站数 ≥ 0.6  （资料库那些站，这条库内线走过多少）
  2. 量级下界：库内线站数、里程都不能只有资料库的一半以下
     —— 只卡「小线冒充大线」这一个方向。

方向是不对称的，这点很关键：库内线比资料库**大**是正常且常见的
（「京石高铁」7 站 283.7 km 就是「京广高速线」42 站 2291 km 的一段，
资料库那条线往往只是一整条干线的某个区段，成昆线元昆段那种）。
所以绝不能加上界，否则半张表都会被误判。

用法：
    python scripts/refix_catalog_match.py            # 只出报告
    python scripts/refix_catalog_match.py --apply    # 写库（先备份 rail.db）
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import time
from typing import Dict, List, Optional, Set, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "webapp"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import cnrail                                                    # noqa: E402
import railcore                                                  # noqa: E402
from import_huirui import core, is_hs                            # noqa: E402

DB = railcore.DB_PATH
COVER_MIN = 0.5          # 资料库站序的覆盖率下限
SIZE_RATIO = 0.5         # 库内线站数 / 资料库站数 的下限（只卡「小线冒充大线」）
KM_RATIO = 0.5           # 库内线里程 / 资料库里程 的下限（同样只卡下界）


def _keys(stops: List[dict]) -> Set[str]:
    return {cnrail._key(s.get("name")) for s in stops if s.get("name")}


def stops_score(cat_keys: Set[str], lib_keys: Set[str],
                cat_km: Optional[float], lib_km: Optional[float]) -> float:
    """覆盖率打分，过不了量级下界就是 0"""
    if not cat_keys or not lib_keys:
        return 0.0
    inter = len(cat_keys & lib_keys)
    if not inter:
        return 0.0
    # 量级：站数和里程**同时**不到一半，才算「小线冒充大线」
    # （「陇海西成联络线」2 站 9 km 之于「侯西线」42 站 288.5 km）
    # 只小一头的不动：分段关系里库内线常常站少但里程不短，
    # 「宁西线 → 宁西线西合段」「益湛线 → 益湛线永电段」都是这种
    small_n = len(lib_keys) < len(cat_keys) * SIZE_RATIO
    small_km = bool(cat_km and lib_km and float(lib_km) < float(cat_km) * KM_RATIO)
    if small_n and small_km:
        return 0.0
    return inter / len(cat_keys)


def best_match(rec: dict, our: Dict[str, dict], our_keys: Dict[str, Set[str]],
               by_core: Dict[str, List[str]], exclude: Optional[str] = None
               ) -> Tuple[Optional[str], Optional[str], float]:
    """只认线名完全相同的：同名 → 同族（唯一）→ 放弃

    故意不做站序覆盖率的新增匹配。实测下来 cover 0.5~0.7 那一档全是噪音
    （广佛环线→南广线、汉宜高铁→襄荆高铁、杭衢高铁→沪昆高速线），
    挂错了比不挂更糟：不挂的话 /api/line 会走 compose_line，
    用资料库站序 + OSM 走向拼出一条完整线路，反而更准。
    """
    names = [x for x in (rec["name"], rec["title"]) if x]
    hit = next((x for x in names if x in our and x != exclude), None)
    if hit:
        return hit, "exact", 1.0
    c = core(rec["name"]) or core(rec["title"] or "")
    hs = "高速" in (rec.get("type") or "")
    cands = [n for n in by_core.get(c, []) if n != exclude and is_hs(n) == hs]
    if len(cands) == 1:
        return cands[0], "family", 0.95
    return None, None, 0.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="写库（先备份 rail.db）")
    ap.add_argument("--show", type=int, default=40, help="最多打印多少条变更")
    args = ap.parse_args()

    our = {it["name"]: it for it in cnrail.search_line("", 5000)}
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    our_keys = {}
    for n, it in our.items():
        d = cnrail.line(n) or {}
        our_keys[n] = _keys(d.get("stations") or [])
    by_core: Dict[str, List[str]] = {}
    for n in our:
        by_core.setdefault(core(n), []).append(n)

    recs = []
    for r in conn.execute("""SELECT id, name, title, type, line_name, match_how,
                                    match_score, n_stop, length_km
                             FROM line_catalog"""):
        rec = dict(r)
        rec["keys"] = _keys([{"name": x[0]} for x in conn.execute(
            "SELECT name FROM line_catalog_stop WHERE lid=? ORDER BY seq", (rec["id"],))])
        recs.append(rec)

    changes: List[dict] = []
    kept = 0
    for rec in recs:
        old_name, old_how, old_score = rec["line_name"], rec["match_how"], rec["match_score"] or 0.0
        # added 是「线路库没有、我们把资料 append 成了新线」，它指的就是自己
        if old_how == "added":
            kept += 1
            continue
        # 先验证现在这条还站不站得住，站不住再全库另找
        still = False
        if old_name and old_name in our:
            if old_how in ("exact", "family"):
                still = True          # 同名/同族是按线名认的，不经站序，不动
            else:
                v = stops_score(rec["keys"], our_keys[old_name],
                                rec.get("length_km"), our[old_name].get("totalKm"))
                still = v >= COVER_MIN
        if still:
            kept += 1
            continue
        new_name, new_how, new_score = best_match(rec, our, our_keys, by_core)
        if (new_name, new_how) == (old_name, old_how):
            kept += 1
            continue
        rec.update(new_name=new_name, new_how=new_how, new_score=new_score,
                   old_name=old_name, old_how=old_how, old_score=old_score)
        changes.append(rec)

    loose = [c for c in changes if c["new_name"] is None]
    retarget = [c for c in changes if c["new_name"]]
    print("待复核 %d 条：原匹配保留 %d 条，需改 %d 条（清空 %d、换目标 %d）\n"
          % (len(recs), kept, len(changes), len(loose), len(retarget)))

    # 清空的：多半是「小联络线冒充整线」，重点看
    def _fmt(c: dict) -> str:
        lib = our.get(c["old_name"] or "") or {}
        return ("  %-20s → %-18s %-6s %.2f | 资料 %3d站 %7s km，库 %3d站 %6s km"
                % (c["name"][:20], (c["old_name"] or "—")[:18], c["old_how"] or "—",
                   c["old_score"], c["n_stop"] or 0, c["length_km"],
                   lib.get("n") or 0, lib.get("totalKm")))

    if loose:
        print("【清空】原来的对应线路经复核不成立（按站序覆盖率 + 站数/里程量级）")
        for c in loose[: args.show]:
            print(_fmt(c))
        if len(loose) > args.show:
            print("  …… 其余 %d 条" % (len(loose) - args.show))
        print()
    if retarget:
        print("【换目标】改挂到另一条线")
        for c in retarget[: args.show]:
            print(_fmt(c))
            print("      ↳ 改挂 %s（%s，%.2f）" % (c["new_name"], c["new_how"], c["new_score"]))
        print()

    if not changes:
        print("没有需要改的。")
        return
    if not args.apply:
        print("未写库。确认无误后加 --apply。")
        return

    bak = DB + ".bak-match-" + time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(DB, bak)
    print("已备份 →", bak)
    with conn:
        for c in changes:
            conn.execute("""UPDATE line_catalog
                            SET line_name=?, match_how=?, match_score=?
                            WHERE id=?""",
                         (c["new_name"], c["new_how"], c["new_score"], c["id"]))
    print("已更新 %d 条。" % len(changes))


if __name__ == "__main__":
    main()
