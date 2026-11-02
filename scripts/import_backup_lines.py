#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
import_backup_lines.py — 把备份里的线路**批量**导回线路库，并在名字上标清来源

为什么不能一条条点 `/api/backup/restore`：
  * 每条都 `save_custom_line` 会触发一次全库重建（`_apply_custom`），3470 条要跑几小时；
  * 备份里同名线很多（京沪线 底表 150 站 / OSM 166 站 / 资料库 82 站 / 自建 156 站），
    按原名导入会互相覆盖 —— 备份面板的「导回来」一条条点也会踩这个坑。

所以这里：先分组分配唯一名字 → 一个事务批量写 → 最后统一重建一次。

命名规则（用户 2026-11-02 要求「标注清楚来源」）：
  * 这个名字在全部备份里只出现一次     → 用原名，不加后缀
  * 出现多次                           → 「原名（来源）」，例：京广线（OSM）
  * 同来源同名的还有多条（资料库内部） → 「原名（来源·N站）」，例：沪昆线（资料库·211站）
  * 还撞（同来源同站数的重复）         → 再加 #2 #3
  * 后缀会撑破 30 字上限时，原名截短     （`_build_custom_line` 的限制）

来源另外写进 `note`（详情页能看到）：`备份导入：来源 OSM · 批次 xxx · 218站 2256.4km`。

用法：
  python scripts/import_backup_lines.py                 # 只看会导什么（dry-run，不写库）
  python scripts/import_backup_lines.py --yes           # 真写库
  python scripts/import_backup_lines.py --yes --source osm,base   # 只导某几个来源
  python scripts/import_backup_lines.py --yes --replace # 库里已有同名的自建线也覆盖
  python scripts/import_backup_lines.py --yes --no-stations       # 不补站坐标

写库后要重启服务（内存里的线路库还是旧的）。
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "webapp"))

import backup_lines as B          # noqa: E402
import railcore                   # noqa: E402

SRC_CN = B.SOURCES                       # custom/base/osm/ce/catalog → 中文
# 同组排序（决定谁先挑走短名字、以及 #2 的编号）：资料库 > china-emu > 底表 > 自建 > OSM
SRC_ORDER = {"catalog": 0, "ce": 1, "base": 2, "custom": 3, "osm": 4}
NAME_MAX = 30


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", str(s or ""))


def _gather(tag: str = "", sources=()) -> list:
    """把备份里所有线摊平成一维清单"""
    rows = []
    for b in B.batches():
        tg = b["tag"]
        if tag and tg != tag:
            continue
        data = B._load(tg)
        for src in B.SOURCES:
            if sources and src not in sources:
                continue
            for ln in data.get(src) or []:
                rows.append({
                    "tag": tg, "src": src, "srcCn": SRC_CN.get(src, src),
                    "name": ln["name"], "n": ln["n"], "km": ln["km"],
                    "stops": [{"name": s["name"], "km": s["km"]} for s in ln["stops"]],
                })
    return rows


def _dedupe(rows: list) -> tuple:
    """同批次 + 同来源 + 同名 + 同站数 + 同站序 的完全重复只留一条"""
    seen = {}
    for r in rows:
        sig = (r["tag"], r["src"], _norm(r["name"]), r["n"],
               tuple(s["name"] for s in r["stops"]))
        if sig in seen:
            seen[sig]["_dup"] += 1
            continue
        r["_dup"] = 0
        seen[sig] = r
    return list(seen.values())


def _fit(nm: str, base: str) -> str:
    """后缀可能撑破 30 字：把原名截短，保住后缀"""
    if len(nm) <= NAME_MAX:
        return nm
    tail = nm[len(base):]
    keep = max(2, NAME_MAX - len(tail))
    return base[:keep] + tail


def _uniq(nm: str, used: set) -> str:
    k = 2
    while True:
        cand = _fit(nm + "#%d" % k, nm)
        if cand not in used:
            return cand
        k += 1


def assign_names(rows: list) -> None:
    """给每条线分配唯一名字，写进 r["newname"]"""
    groups = collections.defaultdict(list)
    for r in rows:
        groups[_norm(r["name"])].append(r)
    used = set()
    for base, g in groups.items():
        g.sort(key=lambda r: (SRC_ORDER.get(r["src"], 9), -r["n"], r["tag"]))
        if len(g) == 1:
            cands = [base]
        else:
            cnt = collections.Counter(r["src"] for r in g)
            cands = []
            for r in g:
                cn = r["srcCn"]
                cands.append("%s（%s）" % (base, cn) if cnt[r["src"]] == 1
                             else "%s（%s·%d站）" % (base, cn, r["n"]))
        for r, nm in zip(g, cands):
            nm = _fit(nm, base)
            if nm in used:
                nm = _uniq(nm, used)
            used.add(nm)
            r["newname"] = nm


def build(rows: list, replace: bool, min_stops: int) -> tuple:
    """校验 + 转成待写库的行；返回 (可写, 跳过清单)"""
    ok, skipped = [], []
    lib = {_norm(it["name"]) for it in railcore.list_custom_lines()}
    for r in rows:
        if r["n"] < min_stops:
            skipped.append((r, "只有 %d 站" % r["n"]))
            continue
        if r["newname"] in lib and not replace:
            skipped.append((r, "库里已有同名线"))
            continue
        note = "备份导入：来源 %s · 批次 %s · %d 站 %s" % (
            r["srcCn"], r["tag"], r["n"],
            ("%.1f km" % r["km"]) if r["km"] is not None else "里程不全")
        try:
            ln = railcore._build_custom_line(r["newname"], r["stops"], note, raw=True)
        except ValueError as e:
            skipped.append((r, str(e)))
            continue
        r["note"] = note
        r["built"] = ln
        ok.append(r)
    return ok, skipped


def write(ok: list, replace: bool) -> int:
    """一个事务批量落库（绕开 save_custom_line 的逐条全库重建）"""
    now = time.time()
    with railcore._db_lock, railcore.db() as conn:
        for r in ok:
            ln = r["built"]
            conn.execute(
                """INSERT INTO line_custom(name, stations_json, note, created_at, updated_at, source)
                   VALUES(?,?,?,?,?,'backup')
                   ON CONFLICT(name) DO UPDATE SET stations_json=excluded.stations_json,
                     note=excluded.note, updated_at=excluded.updated_at, source=excluded.source""",
                (ln["name"], json.dumps(ln["stations"], ensure_ascii=False),
                 ln["note"], now, now),
            )
    return len(ok)


def write_geom(ok: list) -> int:
    """备份里有走向的（只有 GeoJSON 导入的那批自建线）连走向一起写回 line_geom

    只认 `custom` 来源：备份的 `_geom` 是按**线名**存的一行，而同名的线可能有好几条
    （京沪线 底表 150 站 / OSM 166 站 / 资料库 82 站），不加限制会把同一份走向
    往 3 个名字上各写一遍 —— 2026-11-02 第一次跑就写重了 2 条。
    再加一道里程核对（走向总里程和这条线的里程差 > 1 km 就不写），防同批次同名错配。
    """
    n = 0
    with railcore._db_lock, railcore.db() as conn:
        for r in ok:
            if r["src"] != "custom":
                continue
            g = B._geom_row(r["tag"], r["name"])
            if not g:
                continue
            gkm = B._num(g.get("total_km"))
            if gkm and r["km"] and abs(gkm - r["km"]) > 1:
                continue
            try:
                parts = B._blob(g.get("parts_gz"))
                spine = B._blob(g.get("spine_gz")) if g.get("spine_gz") else None
            except (ValueError, TypeError):
                continue
            if not parts:
                continue
            conn.execute(
                """INSERT INTO line_geom(name, file, total_km, n_part, n_pt, props, parts_gz,
                                         spine_gz, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(name) DO UPDATE SET file=excluded.file,
                     total_km=excluded.total_km, n_part=excluded.n_part, n_pt=excluded.n_pt,
                     props=excluded.props, parts_gz=excluded.parts_gz,
                     spine_gz=excluded.spine_gz, created_at=excluded.created_at""",
                (r["newname"], g.get("file") or "", gkm,
                 g.get("n_part"), g.get("n_pt"), g.get("props") or "{}", parts, spine, now_s()),
            )
            n += 1
    return n


def now_s() -> float:
    return time.time()


def fill_stations(ok: list) -> int:
    """给导入的线补站坐标（档案里没有的站才补，source='line-fill'）

    照 `railcore._fill_line_stations` 的口径，但**只读一次自建车站表**，
    3470 条线逐个调那边会 O(n²)。
    """
    have = {_norm(it["name"]) for it in railcore.list_custom_stations()}
    rows = []
    for r in ok:
        for p in r["built"]["stations"]:
            nm = _norm(p["name"])
            if not nm or nm in have:
                continue
            if railcore.cnrail.station(nm):
                continue
            c = railcore.stations.coord(nm)
            src = (railcore.stations.coord_info(nm) or {}).get("source") or ""
            typ = "线路所" if nm.endswith("所") else "车站"
            rows.append((p["name"], c[0] if c else None, c[1] if c else None,
                         json.dumps([r["newname"]], ensure_ascii=False),
                         railcore.stations.telecode(nm) or "", typ,
                         "manual" if (c and src == "manual") else ("osm" if c else "")))
            have.add(nm)                     # 同一条线的重复站名别写两遍
    if not rows:
        return 0
    now = time.time()
    with railcore._db_lock, railcore.db() as conn:
        for nm, lat, lon, lines_json, tele, typ, csrc in rows:
            conn.execute(
                """INSERT INTO station_custom(name, lat, lon, lines_json, tele, bureau, note,
                                              updated_at, type, source, alias, coord_src)
                   VALUES(?,?,?,?,?,'','',?,?,'line-fill','',?)
                   ON CONFLICT(name) DO UPDATE SET
                     lat=COALESCE(station_custom.lat, excluded.lat),
                     lon=COALESCE(station_custom.lon, excluded.lon),
                     updated_at=excluded.updated_at""",
                (nm, lat, lon, lines_json, tele, now, typ, csrc),
            )
    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="真的写库（默认只看）")
    ap.add_argument("--tag", default="", help="只导某个备份批次")
    ap.add_argument("--source", default="", help="只导某些来源：osm,base,ce,catalog,custom")
    ap.add_argument("--min-stops", type=int, default=2, help="少于几站的线跳过（默认 2）")
    ap.add_argument("--replace", action="store_true", help="库里已有同名自建线时覆盖")
    ap.add_argument("--no-stations", action="store_true", help="不补站坐标")
    ap.add_argument("--geom-only", action="store_true", help="只回写走向，不碰站序")
    ap.add_argument("--show", type=int, default=12, help="样例打印几条")
    a = ap.parse_args()

    sources = tuple(x.strip() for x in a.source.split(",") if x.strip())
    rows = _gather(a.tag, sources)
    if not rows:
        print("备份里没有线路（看 data/backup/*/lines_export.json）")
        return 1
    raw_n = len(rows)
    rows = _dedupe(rows)
    dup_n = raw_n - len(rows)
    assign_names(rows)

    ok, skipped = build(rows, a.replace or a.geom_only, max(2, a.min_stops))
    by_src = collections.Counter(r["src"] for r in ok)
    skip_reason = collections.Counter(s[1] for s in skipped)
    renamed = [r for r in ok if _norm(r["name"]) != r["newname"]]

    print("备份里的线：%d 条（去重掉 %d 条完全重复的）" % (raw_n, dup_n))
    print("可以导入：%d 条 | 跳过：%d 条" % (len(ok), len(skipped)))
    print("  按来源：" + " / ".join("%s %d" % (SRC_CN.get(k, k), v)
                                   for k, v in sorted(by_src.items(), key=lambda kv: -kv[1])))
    if skip_reason:
        print("  跳过原因：" + " / ".join("%s %d" % (k, v) for k, v in skip_reason.most_common(6)))
    print("  改了名字（带来源后缀）的：%d 条" % len(renamed))
    for r in renamed[:a.show]:
        print("     %s → %s" % (r["name"], r["newname"]))
    if len(renamed) > a.show:
        print("     …… 还有 %d 条" % (len(renamed) - a.show))

    if not a.yes:
        print("\n（dry-run：一个字节都没写。确认无误后加 --yes）")
        return 0

    if a.geom_only:
        print("\n只回写走向：%d 条" % write_geom(ok))
        return 0

    t0 = time.time()
    n = write(ok, a.replace)
    print("\n写入 line_custom：%d 条（%.1fs）" % (n, time.time() - t0))
    t0 = time.time()
    g = write_geom(ok)
    print("写入 line_geom：%d 条走向" % g)
    t0 = time.time()
    railcore._apply_custom()
    print("重建线路库：%d 条（%.1fs）" % (len(railcore.list_custom_lines()), time.time() - t0))
    if not a.no_stations:
        t0 = time.time()
        filled = fill_stations(ok)
        print("补站坐标：%d 个新车站（%.1fs）" % (filled, time.time() - t0))
        if filled:
            railcore._apply_custom()
            railcore._reload_coord_cache()
            railcore._apply_coord_overrides()
    print("\n完成。**重启服务**后界面才会按新库显示。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
