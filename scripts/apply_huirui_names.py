#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把资料库（辉睿）介绍里的「中文名称」定为整理台所有线路的正式名称。

口径（2026-11-04 用户定）：
  · 规范线名 = 资料库介绍里的「中文名称」（不是 OSM 名、也不是底表的按段名）
  · 起讫站点 = 介绍里的「起止站点」（已经有了就不覆盖，人工改过的优先）
  · **只改名字和起讫，不动站序、不动别名、不合并线路**

重名怎么办：两条线都想叫同一个名字（典型是「京沪线（底表）」和「京沪线（OSM）」
都想叫「京沪线」），默认**只让一条用这个名字，其余跳过并报出来**，由人工在整理台里
合并 —— 脚本不替用户选谁对谁错。

改名分两步走（先全部挪到临时名，再落到正式名），避免 A→B、B→C 这种链式改名
中途撞上 UNIQUE 约束（第一次跑就撞了）。

用法：
    python scripts/apply_huirui_names.py --dry-run     # 先看会改哪些、多少冲突
    python scripts/apply_huirui_names.py               # 落库
    python scripts/apply_huirui_names.py --only-lines  # 只处理「有站序」的线
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "webapp"))

import linedesk  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="用资料库中文名称统一规范线名")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写库")
    ap.add_argument("--only-lines", action="store_true", help="只处理有站序的线")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少条（调试用）")
    a = ap.parse_args()

    n_ref = linedesk.ensure_ref()
    print(f"资料库介绍：{n_ref} 条")

    with linedesk.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT id,name,from_sta,to_sta,status FROM ld_line ORDER BY id")]
        alias = {}
        for r in c.execute("SELECT line_id,name FROM ld_alias"):
            alias.setdefault(r["line_id"], []).append(r["name"])
        current = {r["name"]: r["id"] for r in rows}
        id_name = {r["id"]: r["name"] for r in rows}

        want = {}            # lid -> 目标名
        same, no_ref = [], []
        for r in rows:
            if a.only_lines:
                n = c.execute("SELECT COUNT(*) FROM ld_stop WHERE line_id=?",
                              (r["id"],)).fetchone()[0]
                if n < 2:
                    continue
            ref = linedesk._ref_row(c, [r["name"]] + alias.get(r["id"], []))
            target = (ref.get("name") or "").strip() if ref else ""
            if not target:
                no_ref.append(r["name"])
                continue
            if target == r["name"]:
                same.append(r["name"])
            else:
                want[r["id"]] = target

        # 名字归属：先看谁会腾出来，再定谁用得上
        by_target = {}
        for lid, t in want.items():
            by_target.setdefault(t, []).append(lid)
        renamed, conflicts = [], []
        for t, lids in by_target.items():
            owner = current.get(t)
            if owner is not None and owner not in want:      # 现在占着、且自己不挪
                for lid in lids:
                    conflicts.append((id_name[lid], t, f"已被「{id_name[owner]}」占着"))
                continue
            # 已经在用这个名字的排第一优先，其余按 id
            lids.sort(key=lambda i: (id_name[i] != t, i))
            renamed.append((lids[0], id_name[lids[0]], t))
            for lid in lids[1:]:
                conflicts.append((id_name[lid], t, "两条线都想叫这个名，先人工合并"))

        fromto = []
        for r in rows:
            lid = r["id"]
            if not (lid in want or r["name"] in same):
                continue
            ref = linedesk._ref_row(c, [want.get(lid) or r["name"]] + alias.get(lid, []))
            if not ref:
                continue
            fa, fb = (ref.get("from_sta") or "").strip(), (ref.get("to_sta") or "").strip()
            if (fa and not r["from_sta"]) or (fb and not r["to_sta"]):
                fromto.append((lid, r["name"], fa or r["from_sta"] or "", fb or r["to_sta"] or ""))

        print(f"已经是资料库名称：{len(same)} 条")
        print(f"要改名：{len(renamed)} 条")
        print(f"要补起讫：{len(fromto)} 条")
        print(f"资料库里找不到：{len(no_ref)} 条")
        print(f"重名跳过（需人工合并）：{len(conflicts)} 条")
        for old, new, why in conflicts[:25]:
            print(f"  「{old}」→「{new}」：{why}")
        if len(conflicts) > 25:
            print(f"  …还有 {len(conflicts) - 25} 条")

        if a.dry_run:
            for _i, old, new in renamed[:40]:
                print(f"  改名  {old} → {new}")
            if len(renamed) > 40:
                print(f"  …还有 {len(renamed) - 40} 条")
            print("\n【预演，没有写库】去掉 --dry-run 才落库")
            return 0

        # 两步改名：先全部挪成临时名，再落正式名，绕开链式改名撞 UNIQUE
        for lid, _old, _new in renamed:
            c.execute("UPDATE ld_line SET name=? WHERE id=?", (f"__tmp_{lid}", lid))
        for lid, _old, new in renamed:
            c.execute("UPDATE ld_line SET name=?, family=?, updated_at=? WHERE id=?",
                      (new, linedesk.family_of(new), linedesk._now(), lid))
        for lid, _nm, fa, fb in fromto:
            c.execute("UPDATE ld_line SET from_sta=COALESCE(NULLIF(from_sta,''),?),"
                      " to_sta=COALESCE(NULLIF(to_sta,''),?), updated_at=? WHERE id=?",
                      (fa, fb, linedesk._now(), lid))
    linedesk.log("ref-names", "", f"改名 {len(renamed)} / 起讫 {len(fromto)} / 冲突 {len(conflicts)}")
    print(f"\n完成：改名 {len(renamed)} 条，补起讫 {len(fromto)} 条，重名跳过 {len(conflicts)} 条。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
