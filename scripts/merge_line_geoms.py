# -*- coding: utf-8 -*-
"""把新加的一批线路走向数据合并入库（只增不覆盖）。

来源包（一个线名最多 3 个包，各自独立保留「路线 + 站序」）：
  rg      rail-globe 走向  railway-data/unified/lines_geojson/*.geojson 里 数据来源=rail-globe 的要素
  hdx     OSM 走向         同文件里 数据来源=OSM 的要素
  cnrail  cnrail 走向     cnrail_geojson_corrected/*.geojson（WGS84 坐标修正版，自带车站点）

同名归组：>=2 个来源包 = 「合并」（note 以「合并」开头，走向互补去重后拼成尽量完整的一条）；
只有 1 个来源包 = 单源（note 以「来源」开头）。**所有来源只写 note / props，线名站名一个字不带来源。**

落点（只增不覆盖，line_custom 里已有的行一个字节都不动）：
  rail.db      line_route（新表）  每个来源包自己的走向 + 站序，供整理台分别选择
               line_geom          每线一条合并走向（会刷新已有的 4 条同名，已备份）
               line_custom        只 INSERT OR IGNORE 库里没有的线名
               line_base(+stop)  合并线路且站序>=2 → 临时接管全项目，跳过已定稿的基本线路
  linedesk.db  ld_line（复用/新建，来源保持空）/ ld_alias / ld_geom
  审阅表       railway-data/unified/lines_merged_db.csv

用法：
  python scripts/merge_line_geoms.py                 # dry-run：只算 + 出审阅表
  python scripts/merge_line_geoms.py --apply        # 写库
  python scripts/merge_line_geoms.py --limit 100    # 只处理前 100 个文件（调试）
"""
from __future__ import annotations

import argparse
import collections
import csv
import gzip
import json
import math
import os
import sqlite3
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "webapp"))

import geoimport          # noqa: E402  解析 / 串走向 / 排站序
import linedesk           # noqa: E402  family_of / normalize（只读）
import railcore           # noqa: E402  _apply_custom
import linebase           # noqa: E402  invalidate

RAIL_DB = os.path.join(BASE, "webapp", "data", "rail.db")
DESK_DB = os.path.join(BASE, "webapp", "data", "linedesk.db")
UNIFIED = os.path.join(BASE, "railway-data", "unified")
LG_DIR = os.path.join(UNIFIED, "lines_geojson")
CN_DIR = os.path.join(BASE, "cnrail_geojson_corrected")
REVIEW = os.path.join(UNIFIED, "lines_merged_db.csv")

SRC_LABEL = {
    "rg": "rail-globe 走向",
    "hdx": "OSM 走向",
    "cnrail": "cnrail 走向（坐标修正）",
}
SRC_ORDER = ["rg", "hdx", "cnrail"]
GRID_PREC = 4            # 约 11 m，判断一段几何是否已被主来源覆盖
COVER_THRESH = 0.6       # 采样点命中率 >= 60% 视为重复段，不再补进来


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _jload(path):
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def _hav(lon1, lat1, lon2, lat2):
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _geom_km(parts):
    km = 0.0
    for p in parts:
        for a, b in zip(p, p[1:]):
            km += _hav(a[0], a[1], b[0], b[1])
    return round(km, 1)


def _gz(obj):
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode("utf-8"), 6)


def _grid(parts):
    out = set()
    for p in parts:
        for q in p:
            out.add((round(q[0], GRID_PREC), round(q[1], GRID_PREC)))
    return out


def _covered(seg, grid):
    if not seg:
        return True
    step = max(1, len(seg) // 40)
    pts = seg[::step]
    hit = sum(1 for q in pts
              if (round(q[0], GRID_PREC), round(q[1], GRID_PREC)) in grid)
    return bool(pts) and hit / len(pts) >= COVER_THRESH


def _spine_of(chain):
    """chain 是 [(lat,lon)]，落库统一成 [[lon,lat]]"""
    return [[round(lon, 6), round(lat, 6)] for lat, lon in (chain or [])]


def _stops_of(seq):
    out = []
    for s in seq or []:
        out.append({
            "name": str(s.get("name") or "").strip(),
            "km": round(s["km"], 1) if isinstance(s.get("km"), (int, float)) else None,
            "lon": round(s["lon"], 6) if isinstance(s.get("lon"), (int, float)) else None,
            "lat": round(s["lat"], 6) if isinstance(s.get("lat"), (int, float)) else None,
        })
    return [s for s in out if s["name"]]


def _match_stop(names, hint):
    """资料库起讫站常写「北京站」，文件里是裸名「北京」；再退一步做包含匹配"""
    h = str(hint or "").strip()
    if not h:
        return ""
    h2 = h[:-1] if h.endswith("站") and len(h) > 1 else h
    for n in names:
        if n == h or n == h2:
            return n
    for n in names:
        if h2 and (h2 in n or (len(n) >= 2 and n in h2)):
            return n
    return ""


def _hints(name, ref, stations):
    """从资料库介绍里找起讫站，能对上文件里的站名就交给 assemble 定方向"""
    got = ref.get(name)
    if not got:
        return "", ""
    names = [str(s.get("name") or "").strip() for s in stations]
    return _match_stop(names, got[0]), _match_stop(names, got[1])


def _rev_stops(stops, spine):
    """整条倒排：里程 = 原末站里程 − 原里程，新起点强制 0（与页面「倒排站序」同口径）"""
    total = stops[-1]["km"] or 0 if stops else 0
    out = []
    for i, s in enumerate(reversed(stops)):
        x = dict(s)
        x["km"] = (0.0 if i == 0 else
                   round(total - s["km"], 1)
                   if isinstance(s.get("km"), (int, float)) else None)
        out.append(x)
    return out, list(reversed(spine))


def _proj_km(spine, lon, lat):
    """把车站坐标投影到走向线上，返回 (沿线累计公里, 偏离公里)

    spine 是 [[lon,lat]]，按段做平面投影（段都很短，误差可忽略）。
    """
    best = (1e9, 0.0)
    cum = 0.0
    for a, b in zip(spine, spine[1:]):
        seg = _hav(a[0], a[1], b[0], b[1])
        dx, dy = b[0] - a[0], b[1] - a[1]
        l2 = dx * dx + dy * dy
        if l2 <= 0:
            cum += seg
            continue
        t = ((lon - a[0]) * dx + (lat - a[1]) * dy) / l2
        t = 0.0 if t < 0 else (1.0 if t > 1 else t)
        d = _hav(lon, lat, a[0] + t * dx, a[1] + t * dy)
        if d < best[0]:
            best = (d, cum + seg * t)
        cum += seg
    return best[1], best[0]


def _complete_termini(L, ref, coords, tol_km=45.0, end_km=6.0):
    """cnrail 把北京/上海这类枢纽站归给了别的线，站序两头常常缺。

    资料库给了规范起讫站名 → 到车站库取坐标 → 投影到走向线上：
    落在两头 6 km 以内、偏离走向 45 km 以内，就补成首站 / 末站。
    末站里程沿用「前站里程 + 两点直线距离」，保证整条站序单调递增。
    返回补了几个。
    """
    got = ref.get(L["name"])
    stops, spine = L["stops"], L["spine"]
    if not got or not stops or len(spine) < 2:
        return 0
    total = _geom_km([spine])
    win = max(end_km, total * 0.012)      # 长线两头放宽到 1.2%，短段仍按 6 km
    have = {s["name"] for s in stops}
    added = 0
    for hint in got:
        nm = str(hint or "").strip()
        if not nm:
            continue
        bare = nm[:-1] if nm.endswith("站") and len(nm) > 2 else nm
        if nm in have or bare in have:
            continue
        xy = coords.get(nm) or coords.get(bare)
        if not xy:
            continue
        km, off = _proj_km(spine, xy[0], xy[1])
        if off > tol_km:
            continue
        if km <= win:
            stops.insert(0, {"name": bare, "km": 0.0,
                             "lon": round(xy[0], 6), "lat": round(xy[1], 6)})
        elif km >= total - win:
            last = stops[-1]
            km2 = total
            if all(isinstance(last.get(k), (int, float)) for k in ("km", "lon", "lat")):
                km2 = (last["km"] or 0) + _hav(last["lon"], last["lat"], xy[0], xy[1])
            stops.append({"name": bare, "km": round(km2, 1),
                          "lon": round(xy[0], 6), "lat": round(xy[1], 6)})
        else:
            continue
        have.add(bare)
        added += 1
    return added


def _orient(L, ref, coords=None):
    """按资料库的规范起讫站校方向；反了就整条倒排，返回是否倒过

    名字对得上就按站序位置判；对不上时用起讫站坐标比「首站离终点 + 末站离起点」，
    明显更近才翻（+1 km 容差），免得被同名站带偏。
    """
    got = ref.get(L["name"])
    stops = L["stops"]
    if not got or len(stops) < 2:
        return False
    names = [s["name"] for s in stops]
    a = _match_stop(names, got[0])
    b = _match_stop(names, got[1])
    ia = names.index(a) if a else -1
    ib = names.index(b) if b else -1
    n = len(names)
    if ia >= 0 and ib >= 0 and ia != ib:
        rev = ia > ib
    elif ia >= 0:
        rev = ia > n * 0.6
    elif ib >= 0:
        rev = ib < n * 0.4
    else:
        rev = False
    if (ia < 0 or ib < 0) and coords:
        def _c(h):
            h2 = str(h or "").strip()
            return coords.get(h2) or (coords.get(h2[:-1]) if h2.endswith("站") else None)
        axy, bxy = _c(got[0]), _c(got[1])
        f, t = stops[0], stops[-1]
        ok = all(isinstance(x, (int, float))
                 for x in (f.get("lon"), f.get("lat"), t.get("lon"), t.get("lat")))
        if axy and bxy and ok:
            back = (_hav(f["lon"], f["lat"], bxy[0], bxy[1]) +
                    _hav(t["lon"], t["lat"], axy[0], axy[1]))
            fwd = (_hav(f["lon"], f["lat"], axy[0], axy[1]) +
                   _hav(t["lon"], t["lat"], bxy[0], bxy[1]))
            rev = back + 1 < fwd
    if not rev:
        return False
    L["stops"], L["spine"] = _rev_stops(stops, L["spine"])
    L["from"] = L["stops"][0]["name"]
    L["to"] = L["stops"][-1]["name"]
    return True


def _load_ref():
    """{线名: (起点, 终点)}，资料库中文名称 + 全称都认"""
    out = {}
    if not os.path.exists(DESK_DB):
        return out
    try:
        c = sqlite3.connect("file:" + DESK_DB.replace("\\", "/") + "?mode=ro", uri=True)
        for nm, full, a, b in c.execute(
                "SELECT name, full_name, from_sta, to_sta FROM ld_ref"):
            if not (a or b):
                continue
            for k in (nm, full):
                if k and k not in out:
                    out[str(k).strip()] = (str(a or "").strip(), str(b or "").strip())
        c.close()
    except sqlite3.DatabaseError:
        pass
    return out


def _load_sta_coords():
    """{站名: (lon,lat)}，为方向校正提供地理兜底（名字对不上时比距离）"""
    out = {}
    if not os.path.exists(RAIL_DB):
        return out
    try:
        c = sqlite3.connect("file:" + RAIL_DB.replace("\\", "/") + "?mode=ro", uri=True)
        for nm, lon, lat in c.execute(
                "SELECT name, lon, lat FROM station_master WHERE lon IS NOT NULL"):
            if not nm or lon is None or lat is None:
                continue
            out.setdefault(str(nm).strip(), (float(lon), float(lat)))
            k = str(nm).strip()
            if k.endswith("站") and len(k) > 1:
                out.setdefault(k[:-1], (float(lon), float(lat)))
        c.close()
    except sqlite3.DatabaseError:
        pass
    return out


# ---------------------------------------------------------------------------
# 1. 读三套来源，拆成 (线名 -> {source: 来源包})
# ---------------------------------------------------------------------------
def load_packages(limit=0):
    packs = collections.defaultdict(dict)
    stat = collections.Counter()

    man_path = os.path.join(LG_DIR, "manifest.csv")
    with open(man_path, encoding="utf-8-sig") as f:
        man = list(csv.DictReader(f))
    if limit:
        man = man[:limit]
    for row in man:
        fn, nm = row["文件名"], str(row["线路名"] or "").strip()
        if not nm:
            continue
        fp = os.path.join(LG_DIR, fn)
        if not os.path.exists(fp):
            stat["lines_geojson 缺文件"] += 1
            continue
        d = _jload(fp)
        buckets = {"rg": [], "hdx": []}
        for feat in d.get("features", []):
            v = str((feat.get("properties") or {}).get("数据来源") or "")
            key = "rg" if "rail-globe" in v.lower() else "hdx"
            geoimport._line_parts(feat.get("geometry") or {}, buckets[key])
        for key, parts in buckets.items():
            parts = [p for p in parts if len(p) >= 2]
            if parts:
                packs[nm][key] = {
                    "parts": parts, "stations": [], "src_id": fn,
                    "n_part": len(parts), "n_pt": sum(len(p) for p in parts),
                }
                stat["包 " + key] += 1
        stat["lines_geojson 文件"] += 1

    for fn in sorted(os.listdir(CN_DIR)):
        if not fn.endswith(".geojson") or fn.startswith("_"):
            continue
        nm = fn[:-8]
        if limit and nm not in packs:
            continue
        d = geoimport.parse(_jload(os.path.join(CN_DIR, fn)), fn)
        parts = [p for p in d["parts"] if len(p) >= 2]
        if not parts:
            continue
        packs[nm]["cnrail"] = {
            "parts": parts,
            "stations": d["stations"],
            "src_id": fn,
            "n_part": len(parts),
            "n_pt": sum(len(p) for p in parts),
        }
        stat["包 cnrail"] += 1
        stat["cnrail 文件"] += 1
    return packs, stat


# ---------------------------------------------------------------------------
# 2. 归组合并
# ---------------------------------------------------------------------------
def build_lines(packs, ref, limit=0, coords=None):
    if coords is None:
        coords = _load_sta_coords()
    out = []
    names = sorted(packs)
    if limit:
        names = names[:limit]
    t0 = time.time()
    for i, name in enumerate(names):
        ps = packs[name]
        sources = [s for s in SRC_ORDER if s in ps]
        is_merge = len(sources) >= 2

        # 主来源 = 段数最多的那份；其余来源只补主来源没覆盖到的段
        main = max(sources, key=lambda s: len(ps[s]["parts"]))
        merged_parts = list(ps[main]["parts"])
        grid = _grid(merged_parts)
        added = 0
        for s in sources:
            if s == main:
                continue
            for seg in ps[s]["parts"]:
                if not _covered(seg, grid):
                    merged_parts.append(seg)
                    grid.update((round(q[0], GRID_PREC), round(q[1], GRID_PREC))
                                for q in seg)
                    added += 1

        # 站点池：cnrail 的点优先（它是唯一自带车站点的来源）
        st, seen = [], set()
        for s in ("cnrail", "rg", "hdx"):
            if s not in ps:
                continue
            for x in ps[s]["stations"]:
                nm = str(x.get("name") or "").strip()
                if not nm or nm in seen:
                    continue
                seen.add(nm)
                st.append(x)
        hs, he = _hints(name, ref, st)

        def run(parts):
            if not st or not parts:
                return [], [], 0.0
            seq, chain, stat = geoimport.assemble(parts, st, hs, he)
            return _stops_of(seq), _spine_of(chain), float(stat.get("totalKm") or 0)

        stops, spine, km = run(merged_parts)
        geom_km = _geom_km(merged_parts)
        if not km:
            km = geom_km

        # 每个来源包单独跑一遍：整理台里才能真正「路线 / 站序分别选」
        for s in sources:
            p = ps[s]
            p_stops, p_spine, p_km = run(p["parts"])
            p["stops"] = p_stops
            p["spine"] = p_spine
            p["total_km"] = p_km or _geom_km(p["parts"])
            p["note"] = ("合并（来源之一）：" if is_merge else "来源：") + \
                "%s · %s" % (SRC_LABEL[s], p["src_id"])

        src_txt = " + ".join(SRC_LABEL[s] for s in sources)
        if is_merge:
            note = ("合并：%s｜共 %d 个来源｜%d 段 / %d 点 / %d 站｜补入 %d 段｜"
                    "几何里程含复线重复、非营业里程，待整理核正｜临时基本线路") % (
                        src_txt, len(sources), len(merged_parts),
                        sum(len(p) for p in merged_parts), len(stops), added)
        else:
            note = "来源：%s · %s｜%d 段 / %d 站｜几何里程含复线重复，待整理核正" % (
                src_txt, ps[sources[0]]["src_id"], len(merged_parts), len(stops))

        L = {
            "name": name, "sources": sources, "is_merge": is_merge,
            "parts": merged_parts, "spine": spine, "stops": stops,
            "total_km": round(km, 1), "n_part": len(merged_parts),
            "n_pt": sum(len(p) for p in merged_parts),
            "added": added, "note": note,
            "from": stops[0]["name"] if stops else "",
            "to": stops[-1]["name"] if stops else "",
            "oriented": False,
        }
        # 补起讫站：cnrail 的线路归属常把枢纽站漏给别人，先用资料库 + 坐标补回来，
        # 再判方向（补完才能按精确站名对齐规范起讫）
        L["added_sta"] = _complete_termini(L, ref, coords)
        if L["stops"] and isinstance(L["stops"][-1].get("km"), (int, float)):
            L["total_km"] = max(L["total_km"], L["stops"][-1]["km"])
        L["from"] = L["stops"][0]["name"] if L["stops"] else ""
        L["to"] = L["stops"][-1]["name"] if L["stops"] else ""
        # 方向跟资料库的规范起讫站对齐；反了连每个来源包的站序一起翻过来
        L["oriented"] = _orient(L, ref, coords)
        L["from"] = L["stops"][0]["name"] if L["stops"] else ""
        L["to"] = L["stops"][-1]["name"] if L["stops"] else ""
        if L["oriented"]:
            for s in sources:
                p = ps[s]
                if p["stops"]:
                    p["stops"], p["spine"] = _rev_stops(p["stops"], p["spine"])
        if L.get("added_sta"):
            L["note"] += "｜补起讫 %d 站" % L["added_sta"]
        out.append(L)
        if (i + 1) % 500 == 0:
            print("  ... %d/%d 条，用时 %.1fs" % (i + 1, len(names), time.time() - t0),
                  flush=True)
    return out


# ---------------------------------------------------------------------------
# 3. 审阅表
# ---------------------------------------------------------------------------
def write_review(lines, packs):
    os.makedirs(UNIFIED, exist_ok=True)
    cols = ["线路名", "是否合并", "来源", "来源包数", "段数", "点数", "站数",
            "补起讫站", "里程km(几何)", "补齐段数", "起点", "终点", "方向已校正", "备注"]
    with open(REVIEW, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for L in lines:
            w.writerow([
                L["name"], "合并" if L["is_merge"] else "单源",
                " + ".join(SRC_LABEL[s] for s in L["sources"]),
                len(L["sources"]), L["n_part"], L["n_pt"], len(L["stops"]),
                L.get("added_sta", 0), L["total_km"], L["added"], L["from"], L["to"],
                "是" if L["oriented"] else "", L["note"],
            ])
    return REVIEW


# ---------------------------------------------------------------------------
# 4. 写库
# ---------------------------------------------------------------------------
DDL_ROUTE = """
CREATE TABLE IF NOT EXISTS line_route (
    name       TEXT NOT NULL,
    source     TEXT NOT NULL,
    src_id     TEXT,
    n_part     INTEGER, n_pt INTEGER, n_stop INTEGER,
    total_km   REAL,
    parts_gz   BLOB,
    spine_gz   BLOB,
    stops_json TEXT,
    note       TEXT,
    created_at REAL,
    PRIMARY KEY (name, source)
);
CREATE INDEX IF NOT EXISTS ix_route_source ON line_route(source);
"""


def apply(lines, packs, refresh_base=False):
    now = time.time()
    rail = sqlite3.connect(RAIL_DB, timeout=60)
    rail.execute("PRAGMA journal_mode=WAL")
    rail.executescript(DDL_ROUTE)

    n_route = n_geom = n_custom = n_base = n_basestop = 0
    try:
        for L in lines:
            nm = L["name"]
            props = {
                "线路名": nm, "来源": [SRC_LABEL[s] for s in L["sources"]],
                "来源包": [{"source": s, "文件": packs[nm][s]["src_id"]}
                        for s in L["sources"]],
                "标记": "合并" if L["is_merge"] else "单源",
                "备注": L["note"],
            }
            # 每个来源包独立一行：整理台「线路来源 / 站点来源」分别选
            for s in L["sources"]:
                p = packs[nm][s]
                rail.execute(
                    "INSERT INTO line_route(name,source,src_id,n_part,n_pt,n_stop,"
                    "total_km,parts_gz,spine_gz,stops_json,note,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(name,source) DO UPDATE SET"
                    " src_id=excluded.src_id, n_part=excluded.n_part,"
                    " n_pt=excluded.n_pt, n_stop=excluded.n_stop,"
                    " total_km=excluded.total_km, parts_gz=excluded.parts_gz,"
                    " spine_gz=excluded.spine_gz, stops_json=excluded.stops_json,"
                    " note=excluded.note, created_at=excluded.created_at",
                    (nm, s, p["src_id"], p["n_part"], p["n_pt"], len(p["stops"]),
                     p["total_km"], _gz(p["parts"]),
                     _gz(p["spine"]) if p["spine"] else None,
                     json.dumps(p["stops"], ensure_ascii=False), p["note"], now))
                n_route += 1

            # 合并走向：地图优先画 spine，没有就画原始多段
            rail.execute(
                "INSERT INTO line_geom(name,file,total_km,n_part,n_pt,props,parts_gz,"
                "spine_gz,created_at) VALUES(?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(name) DO UPDATE SET file=excluded.file,"
                " total_km=excluded.total_km, n_part=excluded.n_part,"
                " n_pt=excluded.n_pt, props=excluded.props,"
                " parts_gz=excluded.parts_gz, spine_gz=excluded.spine_gz,"
                " created_at=excluded.created_at",
                (nm, "合并：" + " + ".join(L["sources"]), L["total_km"],
                 L["n_part"], L["n_pt"],
                 json.dumps(props, ensure_ascii=False), _gz(L["parts"]),
                 _gz(L["spine"]) if L["spine"] else None, now))
            n_geom += 1

            # 只新增库里没有的线名，已有的备份线路一个字节都不动；
            # 自己上一轮写进去的 geojson 行（补完起讫站后）跟着刷新站序
            if len(L["stops"]) >= 2:
                row = rail.execute(
                    "SELECT source FROM line_custom WHERE name=?", (nm,)).fetchone()
                if row is None:
                    rail.execute(
                        "INSERT OR IGNORE INTO line_custom(name,stations_json,note,"
                        "created_at,updated_at,source) VALUES(?,?,?,?,?,'geojson')",
                        (nm, json.dumps([{"name": x["name"], "km": x["km"]}
                                         for x in L["stops"]], ensure_ascii=False),
                         L["note"], now, now))
                    n_custom += 1
                elif str(row[0]) == "geojson":
                    rail.execute(
                        "UPDATE line_custom SET stations_json=?, note=?, updated_at=?"
                        " WHERE name=? AND source='geojson'",
                        (json.dumps([{"name": x["name"], "km": x["km"]}
                                     for x in L["stops"]], ensure_ascii=False),
                         L["note"], now, nm))
                    n_custom += 1

            # 合并线路 = 目前全项目临时接管的基本线路；已定稿的基本线路让位不动
            # （refresh_base 只覆盖本脚本自己写的「临时基本线路」，动不到人工定稿的）
            if L["is_merge"] and len(L["stops"]) >= 2:
                old = rail.execute(
                    "SELECT id, note FROM line_base WHERE name=?", (nm,)).fetchone()
                temporary = bool(old) and "临时基本线路" in (old[1] or "")
                if old is None or (refresh_base and temporary):
                    if old is not None:
                        rail.execute("DELETE FROM line_base_stop WHERE base_id=?", (old[0],))
                        rail.execute("DELETE FROM line_base WHERE id=?", (old[0],))
                    covers = ",".join("%s:%s" % (s, nm) for s in L["sources"])
                    cur = rail.execute(
                        "INSERT INTO line_base(name,ld_id,total_km,from_sta,to_sta,"
                        "geom_src,geom_name,covers,note,created_at,updated_at)"
                        " VALUES(?,NULL,?,?,?,'geojson',?,?,?,?,?)",
                        (nm, L["total_km"], L["from"], L["to"], nm, covers,
                         L["note"], now, now))
                    bid = cur.lastrowid
                    rail.executemany(
                        "INSERT INTO line_base_stop(base_id,seq,name,km,lon,lat)"
                        " VALUES(?,?,?,?,?,?)",
                        [(bid, i, x["name"], x["km"], x["lon"], x["lat"])
                         for i, x in enumerate(L["stops"], 1)])
                    n_base += 1
                    n_basestop += len(L["stops"])
        rail.commit()
    except Exception:
        rail.rollback()
        raise
    finally:
        rail.close()

    # 内存线路库重新合并一次（服务重启也会自动做，这里保证当前进程一致）
    try:
        railcore._apply_custom()
    except Exception as e:                                  # noqa: BLE001
        print("  ! _apply_custom 跳过：%s" % e)
    try:
        linebase.invalidate()
    except Exception:                                       # noqa: BLE001
        pass

    # ---------------- 整理台 ----------------
    desk = sqlite3.connect(DESK_DB, timeout=60)
    desk.execute("PRAGMA journal_mode=WAL")
    desk.row_factory = sqlite3.Row
    existing = {r["name"]: r["id"] for r in desk.execute("SELECT id,name FROM ld_line")}
    n_ldline = n_alias = n_ldgeom = n_ldstop = 0
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        for L in lines:
            nm = L["name"]
            lid = existing.get(nm)
            if lid is None:
                d = linedesk.normalize(nm)
                cur = desk.execute(
                    "INSERT INTO ld_line(name,family,kind,status,created_at,updated_at,"
                    "note,src_main,src_line,src_stop,line_ok,stop_ok)"
                    " VALUES(?,?,?,'draft',?,?,?,'manual','','',0,0)",
                    (nm, linedesk.family_of(nm), d.get("kind") or "", ts, ts, L["note"]))
                lid = cur.lastrowid
                existing[nm] = lid
                n_ldline += 1
            # 来源别名：ok=0、src_line / src_stop 保持空 —— 等用户自己分别确认
            for s in L["sources"]:
                p = packs[nm][s]
                cur = desk.execute(
                    "INSERT OR IGNORE INTO ld_alias(line_id,source,name,src_id,ok)"
                    " VALUES(?,?,?,?,0)", (lid, s, nm, p["src_id"]))
                n_alias += cur.rowcount
            # 站序也摆进来，但 src_line / src_stop / 两个确认位全留空 ——
            # 用户仍要自己分别挑「路线来源」和「站点来源」再确认
            if L["stops"]:
                n_all = desk.execute(
                    "SELECT COUNT(*) FROM ld_stop WHERE line_id=?", (lid,)).fetchone()[0]
                n_own = desk.execute(
                    "SELECT COUNT(*) FROM ld_stop WHERE line_id=? AND src='merged'",
                    (lid,)).fetchone()[0]
                # 只在「还没站序」或「全是本脚本摆的、用户没动过」时写，人工改过的绝不覆盖
                if n_all == 0 or (refresh_base and n_all == n_own):
                    desk.execute("DELETE FROM ld_stop WHERE line_id=? AND src='merged'",
                                 (lid,))
                    desk.executemany(
                        "INSERT INTO ld_stop(line_id,seq,name,km,lon,lat,src,note)"
                        " VALUES(?,?,?,?,?,?,?,?)",
                        [(lid, i, x["name"], x["km"], x["lon"], x["lat"],
                          "merged", L["note"])
                         for i, x in enumerate(L["stops"], 1)])
                    n_ldstop += len(L["stops"])
            # 合并走向先摆进整理台（只是可看的几何，不代表已确认）
            if L["spine"]:
                desk.execute(
                    "INSERT INTO ld_geom(name,line_id,filename,total_km,n_part,n_pt,"
                    "props,spine_gz,created_at) VALUES(?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(name) DO UPDATE SET line_id=excluded.line_id,"
                    " filename=excluded.filename, total_km=excluded.total_km,"
                    " n_part=excluded.n_part, n_pt=excluded.n_pt,"
                    " props=excluded.props, spine_gz=excluded.spine_gz,"
                    " created_at=excluded.created_at",
                    (nm, lid, "合并：" + " + ".join(L["sources"]), L["total_km"],
                     L["n_part"], L["n_pt"],
                     json.dumps({"合并": L["is_merge"], "来源": L["sources"],
                                 "备注": L["note"]}, ensure_ascii=False),
                     _gz(L["spine"]), ts))
                n_ldgeom += 1
        desk.commit()
    except Exception:
        desk.rollback()
        raise
    finally:
        desk.close()

    return {
        "line_route": n_route, "line_geom": n_geom, "line_custom 新增": n_custom,
        "line_base 新增": n_base, "line_base_stop 新增": n_basestop,
        "ld_line 新增": n_ldline, "ld_alias 新增": n_alias,
        "ld_stop 新增": n_ldstop, "ld_geom 新增": n_ldgeom,
    }


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的写库（默认只算 + 出审阅表）")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个文件/线名（调试）")
    ap.add_argument("--refresh-base", action="store_true",
                    help="重写本脚本自己写的「临时基本线路」（人工定稿的动不到）")
    args = ap.parse_args()

    print("== 读来源 ==")
    t = time.time()
    packs, stat = load_packages(args.limit)
    for k, v in stat.items():
        print("  %-22s %d" % (k, v))
    print("  线名合计 %d，用时 %.1fs" % (len(packs), time.time() - t))

    ref = _load_ref()
    coords = _load_sta_coords()
    print("  资料库起讫提示 %d 条，车站坐标 %d 个" % (len(ref), len(coords)))

    print("== 合并 ==")
    lines = build_lines(packs, ref, args.limit, coords)
    print("  方向按资料库校正 %d 条" % sum(1 for L in lines if L["oriented"]))
    mg = sum(1 for L in lines if L["is_merge"])
    withst = sum(1 for L in lines if L["stops"])
    print("  线路 %d 条：合并 %d / 单源 %d" % (len(lines), mg, len(lines) - mg))
    print("  有站序 %d 条（站数合计 %d）" % (withst, sum(len(L["stops"]) for L in lines)))
    print("  合并且有 >=2 站 %d 条 → 可作基本线路" %
          sum(1 for L in lines if L["is_merge"] and len(L["stops"]) >= 2))
    print("  几何段合计 %d / 点 %d" %
          (sum(L["n_part"] for L in lines), sum(L["n_pt"] for L in lines)))
    print("  名称含来源字样: %d" %
          sum(1 for L in lines if any(w in L["name"] for w in
                                      ("rail-globe", "OSM", "cnrail", "来源", "合并"))))
    print("  例：")
    for L in lines[:3] + [x for x in lines if x["name"] in ("京沪线", "京广线")][:2]:
        print("    %-14s %s / %d 段 / %d 站 / %.1f km" %
              (L["name"], "合并" if L["is_merge"] else "单源",
               L["n_part"], len(L["stops"]), L["total_km"]))
        print("      " + L["note"])

    p = write_review(lines, packs)
    print("== 审阅表 ==\n  %s" % p)

    if not args.apply:
        print("\n（dry-run，未写库。加 --apply 正式执行）")
        return
    print("== 写库 ==")
    t = time.time()
    res = apply(lines, packs, refresh_base=args.refresh_base)
    for k, v in res.items():
        print("  %-22s %d" % (k, v))
    print("  用时 %.1fs" % (time.time() - t))


if __name__ == "__main__":
    main()
