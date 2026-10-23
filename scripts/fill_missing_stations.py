#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
fill_missing_stations.py — 把「线路里有、车站表里没有」的车站补进 station_custom

站名的来源（三处都算「线路里有的」）：
  1. data/cnrail_lines.json         档案线路的逐站表
  2. line_custom                    自建 / 资料库带进来的线路
  3. line_catalog_stop              线路资料库（辉睿「中国铁路线路一览」）的逐站表

补进去的车站：
  - name     按档案口径去掉尾部「站」（「上虞站」→「上虞」），原写法记在 alias
  - type     车站 / 线路所 / 乘降所
  - lines    所属线路（只写线路库里真有的线路名，免得点进去是 404）
  - lat/lon  能从 OSM 派生坐标（webapp/data/station_coords.json）对上就填，对不上留空
  - source   line-fill（区别于手工自建 manual）

用法：
  python scripts/fill_missing_stations.py            # 只看统计，不写库
  python scripts/fill_missing_stations.py --apply    # 真的写库
  python scripts/fill_missing_stations.py --apply --report docs/station-fill.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
DB_PATH = os.path.join(ROOT, "webapp", "data", "rail.db")
OSM_PATH = os.path.join(ROOT, "webapp", "data", "station_coords.json")

SUFFIXES = ("线路所", "站", "所")
HEAD_KEEP = 8          # 报告里只列前 N 个站名


def norm(s) -> str:
    return re.sub(r"\s+", "", str(s or ""))


def key(s) -> str:
    """12306 口径站名：去掉「线路所 / 站 / 所」后缀（与 cnrail._key 一致）"""
    n = norm(s)
    for suf in SUFFIXES:
        if len(n) > len(suf) and n.endswith(suf):
            return n[: -len(suf)]
    return n


def canon(name: str) -> tuple[str, str]:
    """
    规范站名 + 原写法。

    档案里的站名基本不带「站」，线路资料里却常写成「上虞站」，统一截掉；
    「十八站」「新九站」这类地名自带的「站」不能截，靠「截完只剩一两个字」兜住。
    """
    n = norm(name)
    if not n or not n.endswith("站") or n.endswith("线路所"):
        return n, ""
    bare = n[:-1]
    if len(bare) >= 2 and not bare.endswith("乘降所"):
        return bare, n
    return n, ""


def kind(name: str) -> str:
    n = norm(name)
    if n.endswith("线路所") or n.endswith("所"):
        return "线路所"
    return "车站"


def load_json(path: str):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的写库（默认只统计）")
    ap.add_argument("--report", default="", help="把补全清单写成 JSON")
    args = ap.parse_args()

    archive = load_json(os.path.join(DATA_DIR, "cnrail_stations.json")) or {}
    lines = load_json(os.path.join(DATA_DIR, "cnrail_lines.json")) or []
    osm = load_json(OSM_PATH) or {}

    archive_raw = {norm(k) for k in archive}

    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    # 老库没有这几个列（railcore.init_db 也会补，脚本自己也来一遍，省得先起服务）
    cols = {r[1] for r in conn.execute("PRAGMA table_info(station_custom)")}
    for col in ("type", "source", "alias", "coord_src"):
        if col not in cols:
            conn.execute(f"ALTER TABLE station_custom ADD COLUMN {col} TEXT")
    conn.commit()
    custom_rows = conn.execute(
        "SELECT name FROM station_custom").fetchall()
    custom_names = {norm(r["name"]) for r in custom_rows}

    def known(name: str) -> bool:
        """车站表里有没有这个站（只认「XX」↔「XX站」两种写法，别把「XX所」当成「XX」）"""
        n = norm(name)
        if not n:
            return True
        bare = n[:-1] if n.endswith("站") else n
        pair = {n, bare, bare + "站"}
        return bool(pair & archive_raw) or bool(pair & custom_names)

    # ---- 1. 收集「线路名 → 站名」 -------------------------------------------------
    lib_lines: dict[str, list[str]] = {}          # 线路库里真有的线路
    for ln in lines:
        nm = norm(ln.get("name"))
        if nm:
            lib_lines[nm] = [norm(p.get("name")) for p in (ln.get("stations") or []) if p.get("name")]
    for r in conn.execute("SELECT name, stations_json, source FROM line_custom"):
        nm = norm(r["name"])
        if not nm:
            continue
        try:
            sts = json.loads(r["stations_json"] or "[]")
        except ValueError:
            sts = []
        lib_lines[nm] = [norm(p.get("name")) for p in sts if p.get("name")]

    cat_lines: dict[str, list[str]] = {}          # 资料库里没挂到线路库的线路名（只进备注）
    cat_known: dict[str, set] = {}                # 已挂靠的线路名
    stops_by_lid: dict[str, list[str]] = {}
    for r in conn.execute("SELECT lid, name FROM line_catalog_stop ORDER BY lid, seq"):
        stops_by_lid.setdefault(r["lid"], []).append(norm(r["name"]))
    for r in conn.execute("SELECT id, name, line_name FROM line_catalog"):
        nm = norm(r["name"])
        stops = stops_by_lid.get(r["id"]) or []
        if r["line_name"]:
            cat_known.setdefault(norm(r["line_name"]), set()).update(stops)
        elif nm:
            cat_lines.setdefault(nm, []).extend(stops)

    # ---- 2. 找出缺的站 -----------------------------------------------------------
    want: dict[str, dict] = {}

    def touch(name: str, line: str = "", src: str = "") -> None:
        n = norm(name)
        if not n or known(n):
            return
        cname, alias = canon(n)
        rec = want.setdefault(cname, {"name": cname, "alias": alias,
                                      "lines": [], "cat": [], "src": set(), "raw": set()})
        rec["raw"].add(n)
        if line:
            rec["lines"].append(line)
        if src:
            rec["src"].add(src)

    for ln, sts in lib_lines.items():
        for s in sts:
            touch(s, ln, "line")
    for ln, sts in cat_known.items():
        for s in sts:
            touch(s, ln, "catalog")
    for ln, sts in cat_lines.items():
        for s in sts:
            touch(s, "", "catalog-line")          # 线路库里没有这条线，只记备注
            if norm(s) in want or known(s):
                cname, _ = canon(norm(s))
                if cname in want:
                    want[cname]["cat"].append(ln)

    print(f"档案车站 {len(archive)} 条 · 自建/已补 {len(custom_names)} 条")
    print(f"线路库线路 {len(lib_lines)} 条 · 资料库线路 {len(cat_known) + len(cat_lines)} 条")
    print(f"线路里出现、车站表里没有的站：{len(want)} 个")

    # ---- 3. 坐标 / 字段整理 -------------------------------------------------------
    def look_coord(name: str):
        for cand in (name, name + "站", key(name)):
            v = osm.get(cand)
            if v and isinstance(v, (list, tuple)) and len(v) >= 2:
                return round(float(v[0]), 6), round(float(v[1]), 6)
        return None, None

    rows, with_coord, no_coord = [], 0, []
    for cname in sorted(want):
        rec = want[cname]
        lat, lon = look_coord(cname)
        if lat is None:
            # 原写法（带「站」）也可能在 OSM 里
            for raw in sorted(rec["raw"]):
                lat, lon = look_coord(raw)
                if lat is not None:
                    break
        if lat is not None:
            with_coord += 1
        else:
            no_coord.append(cname)
        uniq_lines = list(dict.fromkeys(rec["lines"]))
        uniq_cat = [x for x in dict.fromkeys(rec["cat"]) if x]
        note = []
        if rec["alias"]:
            note.append(f"原写法 {rec['alias']}")
        if uniq_cat:
            note.append("资料线路 " + "、".join(uniq_cat[:4]) + ("…" if len(uniq_cat) > 4 else ""))
        if not uniq_lines and uniq_cat:
            note.append("线路库里暂未收录该线路")
        rows.append({
            "name": cname,
            "lat": lat, "lon": lon,
            "lines": uniq_lines,
            "type": kind(rec["alias"] or cname),
            "alias": rec["alias"],
            "coord_src": "osm" if lat is not None else "",
            "note": "；".join(note),
        })

    print(f"其中能对上 OSM 坐标的 {with_coord} 个，没坐标的 {len(no_coord)} 个")
    print("样例：")
    for r in rows[:12]:
        c = f"{r['lat']:.6f},{r['lon']:.6f}" if r["lat"] is not None else "——"
        print(f"  {r['name']:<10} {r['type']:<4} {c:<22} "
              f"{'/'.join(r['lines'][:2]) or '(无线)'} {r['note']}")

    if args.report:
        path = args.report if os.path.isabs(args.report) else os.path.join(ROOT, args.report)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"total": len(rows), "withCoord": with_coord,
                       "noCoord": no_coord, "stations": rows}, f, ensure_ascii=False, indent=1)
        print(f"清单已写到 {path}")

    if not args.apply:
        print("（只是统计，没写库；加 --apply 才写）")
        return 0

    now = time.time()
    conn.executemany(
        """INSERT INTO station_custom(name, lat, lon, lines_json, tele, bureau, note,
                                      updated_at, type, source, alias, coord_src)
           VALUES(?,?,?,?,'','',?,?,?,?,?,?)
           ON CONFLICT(name) DO UPDATE SET
             lat=COALESCE(station_custom.lat, excluded.lat),
             lon=COALESCE(station_custom.lon, excluded.lon),
             lines_json=excluded.lines_json,
             type=COALESCE(station_custom.type, excluded.type),
             alias=excluded.alias,
             coord_src=COALESCE(station_custom.coord_src, excluded.coord_src),
             note=excluded.note,
             updated_at=excluded.updated_at""",
        [(r["name"], r["lat"], r["lon"],
          json.dumps(r["lines"], ensure_ascii=False), r["note"], now,
          r["type"], "line-fill", r["alias"], r["coord_src"]) for r in rows],
    )
    conn.commit()
    total = conn.execute("SELECT COUNT(*) c FROM station_custom").fetchone()["c"]
    conn.close()
    print(f"已写入 {len(rows)} 条；station_custom 现有 {total} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
