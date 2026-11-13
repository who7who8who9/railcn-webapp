# -*- coding: utf-8 -*-
"""车站「多个坐标」候选库。

背景（2026-11-09）：`station_master` 是 17 套来源合并出来的**一个**规范实体，
只选了一个坐标生效，剩下那些散在 note 里变成一句「同名 12 处坐标，最远相距
12.7 km」—— 用户看得到「有分歧」，却看不到分歧在哪、也没法挑一个。

这里把**每一套来源、每一个坐标**都落成 `rail.db / station_coord_cand`：
  * 来源 A：`merge_station_sources.load_sources()` 的 10 套带坐标来源（WGS84）
  * 来源 B：`railway-data/unified/stations_all_sources.csv` 六源原始表
    （GCJ-02 / BD-09 由 `coordcrs` 换算成 WGS84，原值 + 坐标系都留着）
  * 归一键与 `station_master.key` 同口径（merge_station_sources.key()）

查询时把彼此相差不到 60 m 的坐标并成「一处」，列出有哪些来源认同这个点，
以及它离当前生效坐标多远 —— 这样「确认」才有意义，而不是让人在 12 个
几乎相同的 OSM 点里挑。

**只读 + 幂等重建**：这张表不参与任何既有取数，确认走项目原有的人工坐标通道
（`coord_override` / `railcore.set_coord`），不在本模块里另开一套写库口径。
"""
from __future__ import annotations

import io
import math
import os
import re
import sqlite3
import sys
import threading
import time
from collections import defaultdict
from contextlib import redirect_stdout

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(BASE, "webapp", "data", "rail.db")
SIX_CSV = os.path.join(BASE, "railway-data", "unified", "stations_all_sources.csv")

_LOCK = threading.Lock()
_BUILT = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS station_coord_cand (
    id        INTEGER PRIMARY KEY,
    key       TEXT NOT NULL,        -- 归一键（同 station_master.key）
    src       TEXT NOT NULL,        -- 来源 id
    src_label TEXT,                 -- 中文来源名
    lat       REAL NOT NULL,        -- WGS84
    lon       REAL NOT NULL,
    raw_lat   REAL,                 -- 换算前的原始纬度
    raw_lon   REAL,                 -- 换算前的原始经度
    crs       TEXT,                 -- WGS84 / GCJ-02 / BD-09 / 未标注…
    converted INTEGER DEFAULT 0,    -- 是否做过坐标系换算
    n         INTEGER DEFAULT 1,    -- 该来源在这个坐标上的原始点数
    raw_name  TEXT,                 -- 来源里的原始站名（含「站」后缀 / 多语言）
    note      TEXT
);
CREATE INDEX IF NOT EXISTS idx_scc_key ON station_coord_cand(key);
CREATE TABLE IF NOT EXISTS station_coord_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# 六源 CSV 的「来源」列 → 项目里的来源 id（与 merge_station_sources.SRC_LABEL 对齐）
SIX_SRC = {
    "rail-globe(OSM)": "railglobe",
    "OSM-HDX": "osmhdx",
    "chinese-railway-gtfs": "gtfs",
    "station-database": "undefi",
    "railway-panorama(12306)": "panorama",
    "TrainVis": "trainvis",
}

# 坐标可信顺序（与 merge_station_sources.build_entities 挑生效坐标的口径一致）
PRIO = ["manual", "archive", "coordjson", "st12306", "crtrain", "custom",
        "chnsta", "pomerun", "undefi", "gtfs", "panorama", "trainvis",
        "railglobe", "osmhdx", "master", "osmdb", "osmraw", "cngeo"]
_PRIO_IDX = {k: i for i, k in enumerate(PRIO)}

SRC_LABEL = {
    "manual": "人工补录", "archive": "车站档案", "coordjson": "OSM派生坐标",
    "st12306": "12306站表", "crtrain": "12306资料", "custom": "自建车站库",
    "chnsta": "站名册CHN", "pomerun": "站名册pomerun", "undefi": "车站属性库",
    "gtfs": "GTFS坐标表", "panorama": "railway-panorama", "trainvis": "TrainVis",
    "railglobe": "rail-globe", "osmhdx": "OSM-HDX", "master": "六源合并总表",
    "osmdb": "OSM车站表", "osmraw": "OSM原始站", "cngeo": "cnrail OSM点",
}


# ---------------------------------------------------------------------------
# 建表
# ---------------------------------------------------------------------------
def _connect() -> sqlite3.Connection:
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def _key(s: str) -> str:
    """与 station_master.key / merge_station_sources.key 完全同口径。"""
    n = re.sub(r"\s+", "", str(s or ""))
    n = n.split("/")[0].split("／")[0]
    m = re.match(r"^[\u4e00-\u9fff0-9A-Za-z·\-—#()（）.、]+", n)
    if m:
        n = m.group(0)
    n = re.sub(r"[\(（](预留|废弃|旧址|在建|规划|已拆除|停用)[\)）]", "", n).strip("-—·")
    for suf in ("线路所", "站", "所"):
        if len(n) > len(suf) and n.endswith(suf):
            return n[: -len(suf)]
    return n


def _rows_from_sources() -> list:
    """17 套来源里所有带坐标的记录 → (key, src, lat, lon, raw, crs, converted, name, note)"""
    import csv

    # 服务进程里 cwd 未必是项目根；两个辅助模块都不在默认 sys.path 上
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (here, os.path.join(BASE, "scripts")):
        if p not in sys.path:
            sys.path.insert(0, p)
    import coordcrs                              # noqa: E402
    import merge_station_sources as m            # noqa: E402

    out: list = []
    buf = io.StringIO()
    with redirect_stdout(buf):                 # load_sources 会打各来源统计
        raw = m.load_sources()
    ref = defaultdict(list)                    # key -> 该站所有 WGS84 基准点
    for r in sorted(raw, key=lambda x: _PRIO_IDX.get(x["src"], 99)):
        if not m.valid_ll(r.get("lat"), r.get("lon")):
            continue
        k = m.key(r.get("name") or "")
        if not k:
            continue
        src = r["src"]
        # load_sources 里带坐标的来源全部在 WGS84_OK 白名单里
        lat, lon = float(r["lat"]), float(r["lon"])
        out.append((k, src, lat, lon, lat, lon, "WGS84", 0,
                    r.get("name") or "", ""))
        pt = (round(lon, 5), round(lat, 5))
        if len(ref[k]) < 12 and pt not in ref[k]:
            ref[k].append(pt)

    # 六源原始表：补上 gtfs / panorama / trainvis / station-database 的坐标。
    # 坐标系备注说的是「原始仓库」，CSV 里的值已被上游不同程度归一，
    # 所以拿上面的 WGS84 基准点自动校准，别按标签盲换（详见 coordcrs.py）。
    if os.path.exists(SIX_CSV):
        with open(SIX_CSV, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                sid = SIX_SRC.get(r.get("来源") or "")
                nm = r.get("车站名") or ""
                k = _key(nm)
                if not sid or not k:
                    continue
                try:
                    rlat, rlon = float(r["纬度"]), float(r["经度"])
                except (TypeError, ValueError):
                    continue
                if not (-90 <= rlat <= 90 and -180 <= rlon <= 180 and (rlat or rlon)):
                    continue
                lon, lat, crs, conv = coordcrs.calibrate(
                    rlon, rlat, r.get("坐标系备注"), ref.get(k))
                out.append((k, sid, lat, lon, rlat, rlon, crs, int(conv), nm,
                            r.get("其他信息") or ""))
    return out


def build(force: bool = False) -> dict:
    """重建候选表（幂等，只动自己的两张表）。"""
    global _BUILT
    with _LOCK:
        c = _connect()
        c.executescript(SCHEMA)
        n = c.execute("SELECT COUNT(*) FROM station_coord_cand").fetchone()[0]
        if n and not force:
            meta = dict(c.execute("SELECT key,value FROM station_coord_meta").fetchall())
            c.close()
            _BUILT = True
            return {"rows": n, "rebuilt": False, **meta}
        rows = _rows_from_sources()

        # 同一来源里坐标完全一样的点（OSM 同名多节点）并成一行，n 记点数
        agg = {}
        for k, src, lat, lon, rlat, rlon, crs, conv, nm, note in rows:
            sig = (k, src, round(lat, 5), round(lon, 5), crs)
            cur = agg.get(sig)
            if cur is None:
                agg[sig] = [k, src, lat, lon, rlat, rlon, crs, conv, 1, nm, note]
            else:
                cur[8] += 1
        data = [(v[0], v[1], SRC_LABEL.get(v[1], v[1]), v[2], v[3],
                 v[4], v[5], v[6], v[7], v[8], v[9], v[10])
                for v in agg.values()]

        c.execute("DELETE FROM station_coord_cand")
        c.executemany(
            """INSERT INTO station_coord_cand
               (key,src,src_label,lat,lon,raw_lat,raw_lon,crs,converted,n,raw_name,note)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", data)
        meta = {
            "builtAt": time.strftime("%Y-%m-%d %H:%M:%S"),
            "rawRows": str(len(rows)),
            "rows": str(len(data)),
            "sources": str(len({r[1] for r in data})),
        }
        c.executemany("INSERT OR REPLACE INTO station_coord_meta(key,value) VALUES(?,?)",
                      list(meta.items()))
        c.commit()
        c.close()
        _BUILT = True
        return {"rebuilt": True, **meta}


def ensure() -> None:
    """服务里第一次查车站时才建（表已存在就直接返回，不重复读 150k 行源数据）。"""
    global _BUILT
    if _BUILT:
        return
    try:
        build()
    except Exception:                        # noqa: BLE001 源数据缺失不能拖垮车站页
        pass


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------
def _hav_m(lat1, lon1, lat2, lon2) -> float:
    r = math.radians
    a = (math.sin(r(lat2 - lat1) / 2) ** 2 +
         math.cos(r(lat1)) * math.cos(r(lat2)) * math.sin(r(lon2 - lon1) / 2) ** 2)
    return 2 * 6371008.8 * math.asin(min(1.0, math.sqrt(a)))


def resolve_keys(*names: str) -> list:
    """查询名 → 候选表里的归一键（可能多个：档案名 / 别名 / 带「站」后缀）。"""
    keys = []
    for n in names:
        if not n:
            continue
        s = re.sub(r"\s+", "", str(n))
        k = _key(s)
        for x in (s, k):
            if x and x not in keys:
                keys.append(x)
    try:
        c = _connect()
        extra = []
        for k in list(keys):
            row = c.execute("SELECT key, name, aliases FROM station_master "
                            "WHERE key=? OR name=? LIMIT 1", (k, k)).fetchone()
            if row:
                for x in [row["key"], row["name"]] + (row["aliases"] or "").split("|"):
                    x = _key(x)
                    if x and x not in keys and x not in extra:
                        extra.append(x)
        c.close()
        keys.extend(extra)
    except sqlite3.Error:
        pass
    return keys


def candidates(*names: str, cur_lat=None, cur_lon=None, limit: int = 80) -> dict:
    """一个站的全部坐标（按位置聚类）→ 给「车站/线路」卡片直接渲染。

    返回 {items:[{lat,lon,crs,sources,n,distM,isCurrent,rawNames}], n, nRaw,
          maxDistM, confirmedSrc, current}
    """
    keys = resolve_keys(*names)
    empty = {"items": [], "n": 0, "nRaw": 0, "maxDistM": 0,
             "confirmedSrc": "", "current": None}
    if not keys:
        return empty
    ensure()
    try:
        c = _connect()
        ph = ",".join("?" for _ in keys)
        rows = c.execute(
            f"SELECT * FROM station_coord_cand WHERE key IN ({ph})", keys).fetchall()
        # coord_override.src 是后加的列；老库 / 迁移前没有就当「没确认过」
        try:
            conf = c.execute(
                "SELECT src FROM coord_override WHERE name IN (" + ph + ")", keys).fetchone()
            confirmed_src = (conf["src"] if conf else "") or ""
        except sqlite3.Error:
            confirmed_src = ""
        c.close()
    except sqlite3.Error:
        return empty
    if not rows:
        return empty

    have_lat = cur_lat is not None and cur_lon is not None
    # 权威来源排在前面：聚类时第一个落进来的点就是这一处的代表坐标
    rows = sorted(rows, key=lambda r: (_PRIO_IDX.get(r["src"], 99), -(r["n"] or 1)))
    clusters = []
    for r in rows:
        hit = None
        for cl in clusters:
            if _hav_m(cl["lat"], cl["lon"], r["lat"], r["lon"]) <= 60:
                hit = cl
                break
        if hit is None:
            hit = {"lat": r["lat"], "lon": r["lon"], "crs": r["crs"],
                   "converted": bool(r["converted"]), "sources": [],
                   "n": 0, "rawNames": []}
            clusters.append(hit)
        hit["n"] += r["n"] or 1
        hit["sources"].append({"src": r["src"],
                               "label": r["src_label"] or SRC_LABEL.get(r["src"], r["src"]),
                               "crs": r["crs"], "n": r["n"] or 1,
                               "converted": bool(r["converted"])})
        if r["raw_name"] and r["raw_name"] not in hit["rawNames"]:
            hit["rawNames"].append(r["raw_name"])
        if have_lat:
            d = _hav_m(cur_lat, cur_lon, hit["lat"], hit["lon"])
            hit["distM"] = round(d)
            hit["isCurrent"] = d <= 60
        else:
            hit["distM"] = None
            hit["isCurrent"] = False

    clusters.sort(key=lambda x: (x["distM"] if x["distM"] is not None else 1e12))
    maxd = max([x["distM"] for x in clusters if x["distM"] is not None] or [0])
    # 同一处里同一个来源可能有多行（不同坐标系标注），合并计数
    for cl in clusters:
        merged = {}
        for s in cl["sources"]:
            k = (s["src"], s["crs"])
            if k in merged:
                merged[k]["n"] += s["n"]
            else:
                merged[k] = dict(s)
        cl["sources"] = list(merged.values())
        cl.pop("converted", None)
    return {"items": clusters[:limit], "n": len(clusters),
            "nRaw": len(rows), "maxDistM": round(maxd),
            "confirmedSrc": confirmed_src,
            "current": {"lat": cur_lat, "lon": cur_lon} if have_lat else None}


# ---------------------------------------------------------------------------
# CLI：python webapp/stationcoords.py [--force] [--stat]
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.join(BASE, "scripts"))
    force = "--force" in sys.argv
    print("重建车站坐标候选表…", flush=True)
    st = build(force=force)
    print(st)
    if "--stat" in sys.argv or True:
        c = _connect()
        print("按来源:", c.execute(
            "SELECT src, COUNT(*), SUM(n) FROM station_coord_cand "
            "GROUP BY src ORDER BY COUNT(*) DESC").fetchall()[:20])
        print("候选处 >1 的站名数:", c.execute(
            "SELECT COUNT(*) FROM (SELECT key FROM station_coord_cand "
            "GROUP BY key HAVING COUNT(*)>1)").fetchone()[0])
        c.close()
