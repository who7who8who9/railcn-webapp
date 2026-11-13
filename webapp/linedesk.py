# -*- coding: utf-8 -*-
"""
linedesk.py — 线路整理台：一座独立的整理库 + 跨来源线名归并

rail.db 里五套来源各写各的名字，同一条线最多四种写法：

    底表按段命名   京哈高速线京沈段 / 京哈高速线沈哈段
    辉睿资料库     京沪高铁
    china-emu      京沪高速铁路
    OSM            京沪高速线
    自建           沪苏湖高铁

所以另开一个库 **webapp/data/linedesk.db**，整理结果只写这里，rail.db 一个字节都不碰：

    ld_line      规范线路（一条线一行，name 是人工定的规范名）
    ld_alias     每个来源的原始写法（source + name 唯一）
    ld_stop      整理后的站序（含坐标、里程）
    ld_relation  分段 ↔ 全线 的父子关系
    ld_log       每次改动的流水（可回溯）

归并只做「名字层面」的确定事：后缀族归一（高铁 = 高速线 = 高速铁路 = 客运专线）、
去掉括号备注、认出「X线Y段」。站序对不对、父子关系对不对，留给页面里人工确认。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "linedesk.db")
RAIL_DB = os.path.join(BASE_DIR, "data", "rail.db")
LIB_JSON = os.path.join(os.path.dirname(BASE_DIR), "data", "cnrail_lines.json")

# 来源优先级：谁的名字当规范名、谁的站序先信
SRC_ORDER = ["lib", "custom", "huirui", "ce", "osm", "rg", "hdx", "cnrail"]
SRC_LABEL = {
    "lib": "底表",
    "custom": "自建",
    "huirui": "资料库",
    "ce": "china-emu",
    "osm": "OSM",
    # 2026-11-05 新加的一批线路走向数据（见 scripts/merge_line_geoms.py）
    "rg": "rail-globe 走向",
    "hdx": "OSM 走向",
    "cnrail": "cnrail 走向",
}
ROUTE_SRC = ("rg", "hdx", "cnrail")
_ROUTE_PT_MAX = 30000           # 预览来源走向时的点数上限（超了抽稀，只为画得动）
_PRIO = {s: i for i, s in enumerate(SRC_ORDER)}

# 后缀 → 族。长后缀必须先匹配（「高速铁路」不能被「铁路」抢走）
_KINDS: Tuple[Tuple[str, str], ...] = (
    ("高速铁路线", "gs"), ("高速铁路", "gs"), ("高速线", "gs"), ("高铁线", "gs"),
    ("客运专线", "gs"), ("客专线", "gs"),
    ("客专", "gs"), ("高铁", "gs"), ("高速", "gs"),
    ("城际铁路线", "cj"), ("城际铁路", "cj"), ("城际线", "cj"), ("城际", "cj"),
    ("市域铁路", "sj"), ("市域线", "sj"), ("市域", "sj"),
    ("联络线", "lj"),
    ("支线", "zx"), ("专线", "zx"),
    ("铁路", "px"), ("线", "px"), ("通道", "px"),
)
KIND_LABEL = {"gs": "高速", "cj": "城际", "sj": "市域",
              "lj": "联络线", "zx": "支线", "px": "普速", "": "—"}

_SEG_RE = re.compile(r"^(.+?(?:铁路|线))([\u4e00-\u9fa5]{1,8})段$")


# ---------------------------------------------------------------------------
# 库
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ld_line (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,     -- 规范线名（人工可改）
    family      TEXT NOT NULL DEFAULT '', -- 归一键：前缀|族（同族 = 同一条线的不同写法）
    kind        TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'draft',   -- draft / confirmed
    total_km    REAL,
    from_sta    TEXT, to_sta TEXT,
    note        TEXT,
    src_main    TEXT,                     -- 站序初稿取自哪个来源
    created_at  TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS ld_alias (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    line_id INTEGER NOT NULL,
    source  TEXT NOT NULL,
    name    TEXT NOT NULL,
    src_id  TEXT,
    UNIQUE(source, name)
);
CREATE TABLE IF NOT EXISTS ld_stop (
    line_id INTEGER NOT NULL,
    seq     INTEGER NOT NULL,
    name    TEXT NOT NULL,
    km      REAL, lon REAL, lat REAL,
    src     TEXT,
    note    TEXT,
    PRIMARY KEY (line_id, seq)
);
CREATE TABLE IF NOT EXISTS ld_relation (
    parent_id INTEGER NOT NULL,
    child_id  INTEGER NOT NULL,
    kind      TEXT NOT NULL DEFAULT 'seg',   -- seg=分段建成 / fork=支线岔出
    note      TEXT,
    PRIMARY KEY (parent_id, child_id)
);
CREATE TABLE IF NOT EXISTS ld_log (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT, op TEXT, target TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS ld_meta (
    k TEXT PRIMARY KEY, v TEXT
);
CREATE INDEX IF NOT EXISTS ix_alias_line ON ld_alias(line_id);
CREATE INDEX IF NOT EXISTS ix_alias_name ON ld_alias(name);
CREATE INDEX IF NOT EXISTS ix_stop_line  ON ld_stop(line_id, seq);
CREATE INDEX IF NOT EXISTS ix_line_fam   ON ld_line(family);
"""

# 2026-11 改造：来源分「线路来源」和「站点来源」，两者分别确认后才合并定稿；
# 导入的 GeoJSON 走向存 ld_geom（**不写 rail.db**）；资料库介绍存 ld_ref。
_SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS ld_geom (
    name      TEXT PRIMARY KEY,          -- 走向名（一般 = 线路规范名）
    line_id   INTEGER,
    filename  TEXT,
    total_km  REAL,
    n_part    INTEGER, n_pt INTEGER,
    props     TEXT,
    spine_gz  BLOB,                      -- 串好的连续走向 [[lon,lat],…]
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS ld_ref (
    hid         TEXT PRIMARY KEY,        -- 资料库 id
    name        TEXT,                    -- 中文名称（**正式名称口径**）
    title       TEXT, full_name TEXT,
    ltype       TEXT, service TEXT, level TEXT,
    from_sta    TEXT, to_sta TEXT,
    length_km   REAL, total_km REAL,
    design_speed TEXT, run_speed TEXT, open_date TEXT,
    alias       TEXT, foreign_name TEXT,
    intro       TEXT, url TEXT,
    stops_json  TEXT, junctions_json TEXT,
    updated_at  TEXT
);
CREATE INDEX IF NOT EXISTS ix_ref_name ON ld_ref(name);
CREATE INDEX IF NOT EXISTS ix_ref_full ON ld_ref(full_name);
"""

# ld_line / ld_alias 的新列（老库 ALTER 上去，默认空 = 「最初来源为空」）
_MIGRATE_COLS = (
    ("ld_line", "src_line", "TEXT NOT NULL DEFAULT ''"),   # 已确认的线路来源：src:name|src:name
    ("ld_line", "src_stop", "TEXT NOT NULL DEFAULT ''"),   # 已确认的站点来源：src:name
    ("ld_line", "line_ok", "INTEGER NOT NULL DEFAULT 0"),  # 线路来源已确认
    ("ld_line", "stop_ok", "INTEGER NOT NULL DEFAULT 0"),  # 站点来源已确认
    ("ld_alias", "ok", "INTEGER NOT NULL DEFAULT 0"),      # 该写法已确认为线路来源
    # 合并保存时定下的走向：route:hdx / route:rg / route:cnrail / osm / geojson
    # 定基本线路时没另选就照它来，地图和主站都跟着用这一份
    ("ld_line", "geom_src", "TEXT NOT NULL DEFAULT ''"),
    ("ld_line", "geom_name", "TEXT NOT NULL DEFAULT ''"),
    ("ld_geom", "parts_gz", "BLOB"),                       # 原始多段（不抽稀，画图用）
    # 站序被人工增删 / 改名 / 反向过：这时②「用它并确认」默认应确认「本线现在的站序」，
    # 不能再按 src_stop 去把来源那一份（没删过的）站序灌回来
    ("ld_line", "stops_edited", "INTEGER NOT NULL DEFAULT 0"),
)

REF_JSON = os.path.join(os.path.dirname(BASE_DIR), "data", "huirui", "lines.json")


def conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=8000")
    return c


def init() -> None:
    with conn() as c:
        c.executescript(_SCHEMA)
        c.executescript(_SCHEMA_V2)
        # 老库补列（幂等；默认空/0 = 新口径下「来源未设置」）
        for table, col, decl in _MIGRATE_COLS:
            have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
            if col not in have:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def log(op: str, target: str = "", detail: str = "") -> None:
    with conn() as c:
        c.execute("INSERT INTO ld_log(ts,op,target,detail) VALUES(?,?,?,?)",
                  (_now(), op, str(target), str(detail)))


# ---------------------------------------------------------------------------
# 线名归一
# ---------------------------------------------------------------------------

def split_seg(name: str) -> Optional[Tuple[str, str]]:
    """「京哈高速线沈哈段」→ ('京哈高速线', '沈哈')；不是分段名返回 None"""
    n = re.sub(r"\s+", "", str(name or ""))
    m = _SEG_RE.match(n)
    if not m:
        return None
    parent, seg = m.group(1), m.group(2)
    return (parent, seg) if parent and seg and parent != seg else None


def normalize(name: str) -> dict:
    """把一条来源线名拆成 (base, kind, seg, note)

    京沪高铁 / 京沪高速线 / 京沪高速铁路 / 京沪高铁（京沪段） → 同一个 base=京沪, kind=gs
    """
    raw = str(name or "").strip()
    n = re.sub(r"\s+", "", raw)
    note = ""
    m = re.search(r"[（(]([^)）]*)[)）]", n)
    if m:
        note = m.group(1).strip()
        n = (n[:m.start()] + n[m.end():]).strip()
    seg = ""
    sp = split_seg(n)
    if sp:
        n, seg = sp
    for suf, kind in _KINDS:
        if n.endswith(suf) and len(n) > len(suf):
            return {"raw": raw, "base": n[: -len(suf)], "kind": kind,
                    "seg": seg, "note": note}
    return {"raw": raw, "base": n, "kind": "", "seg": seg, "note": note}


def family_of(name: str) -> str:
    """归一键：同一族 = 同一条线的不同写法（不分段时）"""
    d = normalize(name)
    return f"{d['base']}|{d['kind']}"


def family_key(d: dict) -> str:
    """用 normalize() 的结果直接算归一键（分段名的族 = 父线的族）"""
    return f"{d['base']}|{d['kind']}"


# OSM 把桥、隧道单独存成一条 element，名字写成「图佳线（第一五林河桥）」
# 有 150 个。它们不是线路的另一种叫法，收进别名只会把「写法数」灌水
_WORK_RE = re.compile(r"[（(][^）)]*(桥|隧道|涵洞|立交)[^）)]*[）)]")


def is_work_name(name: str) -> bool:
    return bool(_WORK_RE.search(str(name or "")))


def sort_key(src: str, n_stop: int) -> Tuple[int, int]:
    return (_PRIO.get(src, 99), -int(n_stop or 0))


# ---------------------------------------------------------------------------
# 收来源
# ---------------------------------------------------------------------------

def _ro(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def _lib_lines() -> List[dict]:
    """底表 417 条（按段命名的原始档案）"""
    if not os.path.exists(LIB_JSON):
        return []
    with open(LIB_JSON, encoding="utf-8") as f:
        raw = json.load(f)
    out = []
    for x in raw:
        stops = [{"name": s.get("name"), "km": s.get("km")}
                 for s in (x.get("stations") or []) if s.get("name")]
        out.append({"source": "lib", "name": x.get("name"), "src_id": x.get("eid") or "",
                    "stops": stops, "total_km": x.get("totalKm")})
    return out


def _huirui_lines() -> List[dict]:
    """资料库的线名 + 站序

    line_catalog 有数据就用它（旧口径）；库被清空过就只有 ld_ref 里的介绍表，
    这时从 stops_json 取（844 条里有 610 条带站序），别让「资料库」来源整个消失。
    """
    with _ro(RAIL_DB) as c:
        try:
            rows = c.execute("SELECT id,name,length_km FROM line_catalog").fetchall()
        except sqlite3.Error:
            rows = []
        byid: Dict[str, List[dict]] = {}
        for r in c.execute("SELECT lid,seq,name,km FROM line_catalog_stop ORDER BY lid,seq"):
            byid.setdefault(r["lid"], []).append({"name": r["name"], "km": r["km"]})
    if rows:
        return [{"source": "huirui", "name": r["name"], "src_id": r["id"],
                 "stops": byid.get(r["id"], []), "total_km": r["length_km"]} for r in rows]
    ensure_ref()
    out = []
    with conn() as c:
        for r in c.execute("SELECT hid,name,stops_json,length_km,total_km FROM ld_ref"):
            try:
                stops = [{"name": s.get("name"), "km": s.get("km")}
                         for s in json.loads(r["stops_json"] or "[]")
                         if isinstance(s, dict) and s.get("name")]
            except (TypeError, ValueError):
                stops = []
            out.append({"source": "huirui", "name": r["name"], "src_id": r["hid"],
                        "stops": stops, "total_km": r["length_km"] or r["total_km"]})
    return out


def _ce_lines() -> List[dict]:
    with _ro(RAIL_DB) as c:
        rows = c.execute("SELECT name,total_km FROM line_ce").fetchall()
        byid: Dict[str, List[dict]] = {}
        for r in c.execute("SELECT lid,seq,name,km,kind FROM line_ce_stop ORDER BY lid,seq"):
            if (r["kind"] or "") == "work":       # 桥隧行不是站
                continue
            byid.setdefault(r["lid"], []).append({"name": r["name"], "km": r["km"]})
    return [{"source": "ce", "name": r["name"], "src_id": r["name"],
             "stops": byid.get(r["name"], []), "total_km": r["total_km"]} for r in rows]


def _custom_lines() -> List[dict]:
    with _ro(RAIL_DB) as c:
        rows = c.execute("SELECT name,stations_json,note FROM line_custom").fetchall()
    out = []
    for r in rows:
        try:
            stops = json.loads(r["stations_json"] or "[]")
        except Exception:                          # noqa: BLE001
            stops = []
        stops = [{"name": s.get("name"), "km": s.get("km")}
                 for s in stops if isinstance(s, dict) and s.get("name")]
        total = stops[-1].get("km") if stops else None
        out.append({"source": "custom", "name": r["name"], "src_id": "",
                    "stops": stops, "total_km": total})
    return out


def _osm_names() -> List[dict]:
    try:
        import railgeom
        if not railgeom.ready():
            return []
        names = [n for n in railgeom._geoms().keys() if not is_work_name(n)]
    except Exception:                              # noqa: BLE001
        return []
    return [{"source": "osm", "name": n, "src_id": n, "stops": [], "total_km": None}
            for n in names]


_ROUTE_CACHE: dict = {"at": 0.0, "items": []}


def _route_lines() -> List[dict]:
    """2026-11-05 新加的线路走向数据（rail.db / line_route）

    每个 (线名, 来源包) 一份独立记录，各自带自己的走向和站序：整理台「① 线路来源」
    可以多选，「② 站点来源」可以单独挑其中一家的站序。几何不在这里读，
    要看走向时按 (name, source) 去 line_route 取，避免把 3 万多个点塞进候选缓存。
    """
    now = time.time()
    if _ROUTE_CACHE["items"] and now - float(_ROUTE_CACHE["at"] or 0) < 300:
        return _ROUTE_CACHE["items"]           # type: ignore[return-value]
    out: List[dict] = []
    if os.path.exists(RAIL_DB):
        try:
            with _ro(RAIL_DB) as c:
                rows = c.execute(
                    "SELECT name,source,src_id,total_km,stops_json FROM line_route"
                ).fetchall()
            for r in rows:
                try:
                    stops = json.loads(r["stops_json"] or "[]")
                except (TypeError, ValueError):
                    stops = []
                out.append({
                    "source": r["source"], "name": r["name"],
                    "src_id": r["src_id"] or "", "total_km": r["total_km"],
                    "stops": [{"name": s.get("name"), "km": s.get("km")}
                              for s in stops
                              if isinstance(s, dict) and s.get("name")],
                })
        except sqlite3.DatabaseError:
            out = []
    _ROUTE_CACHE["items"] = out
    _ROUTE_CACHE["at"] = now
    return out


def _norm_chain(chain) -> List[List[float]]:
    """把一条走向折线统一成 `[[lon,lat], ...]`。

    2026-11-06 坑：chain 有两种点序 —— `geoimport.assemble()` 给的是
    (lat, lon)，而 `line_route.spine_gz` / GeoJSON 原生给的是 [lon, lat]。
    `_save_geom()` 原来一律按 `for lat, lon in chain` 解包，于是③「合并保存」
    搬来源包走向时写出的 spine 整个反了：主站把沪昆线画成左上角一条竖线，
    停站却挤在右下角。这里按**整条折线多数点**判一次、整体交换，不逐点判断
    （个别点单独翻转会在折线上撕出假跳变）。中国境内经度 70~140、纬度 15~55。
    """
    pts: List[List[float]] = []
    for p in (chain or []):
        if not isinstance(p, (list, tuple)) or len(p) < 2:
            continue
        try:
            a, b = float(p[0]), float(p[1])
        except (TypeError, ValueError):
            continue
        pts.append([a, b])
    if not pts:
        return []
    sample = pts[:80]
    rev = sum(1 for a, b in sample if 15 <= a <= 55 and 70 <= b <= 140)
    fwd = sum(1 for a, b in sample if 70 <= a <= 140 and 15 <= b <= 55)
    swap = rev > fwd
    return [[round(b, 6), round(a, 6)] if swap else [round(a, 6), round(b, 6)]
            for a, b in pts]


def _norm_parts(data, single: bool = False) -> List[List[List[float]]]:
    """ld_geom 的 parts_gz（多段）/ spine_gz（单条）→ [[[lon,lat],...], ...]"""
    if not isinstance(data, list) or not data:
        return []
    segs = [data] if single else data
    out = []
    for seg in segs:
        line = _norm_chain(seg)
        if len(line) >= 2:
            out.append(line)
    return out


def _gz_paths(blob) -> List[List[List[float]]]:
    """line_route 里的走向（gzip JSON）→ [[[lon,lat],...], ...]

    spine_gz 是**一条**主路径（[[lon,lat],...]），parts_gz 是**多段**
    （[[[lon,lat],...],...]）。两种都认，统一成多段返回。
    """
    if not blob:
        return []
    try:
        import gzip
        data = json.loads(gzip.decompress(blob).decode("utf-8"))
    except (TypeError, ValueError, OSError, EOFError):
        return []
    if not isinstance(data, list) or not data:
        return []
    if isinstance(data[0], list) and data[0] and isinstance(data[0][0], (int, float)):
        data = [data]                       # 单条主路径 → 包成一段
    return [p for p in data
            if isinstance(p, list) and len(p) >= 2
            and isinstance(p[0], list) and len(p[0]) >= 2]


def _thin(paths: List[List[List[float]]], limit: int) -> List[List[List[float]]]:
    """点数超了按比例抽稀（保留每段首尾），只为在浏览器里画得动，不写库"""
    total = sum(len(p) for p in paths)
    if total <= limit or not total:
        return paths
    step = total / float(limit)
    out: List[List[List[float]]] = []
    for p in paths:
        if len(p) <= 3:
            out.append(p)
            continue
        keep = [p[0]]
        i = step
        while i < len(p) - 1:
            keep.append(p[int(i)])
            i += step
        keep.append(p[-1])
        out.append(keep)
    return out


def _route_stops(source: str, name: str) -> dict:
    """某个新来源包自己的站序 + 它自己的走向（搬过来就能上图）"""
    empty = {"source": source, "name": name, "stops": [], "coordN": 0, "parts": []}
    if not os.path.exists(RAIL_DB):
        return empty
    try:
        with _ro(RAIL_DB) as c:
            r = c.execute(
                "SELECT stops_json,total_km,n_part,spine_gz,parts_gz FROM line_route"
                " WHERE name=? AND source=?", (name, source)).fetchone()
    except sqlite3.DatabaseError:
        return empty
    if not r:
        return empty
    try:
        stops = json.loads(r["stops_json"] or "[]")
    except (TypeError, ValueError):
        stops = []
    out = [{"name": s.get("name"), "km": s.get("km"),
            "lat": s.get("lat"), "lon": s.get("lon")}
           for s in stops if isinstance(s, dict) and s.get("name")]
    # 走向：**优先这个来源自己的原始多段**（就是 OSM / rail-globe 里画的线），
    # 只有原始段缺失才退回 spine（spine 是合并脚本补齐断口拼的主轴，长直线会失真）
    raw = _gz_paths(r["parts_gz"])
    thinned = False
    spine_only = False
    if raw:
        total = sum(len(p) for p in raw)
        if total > _ROUTE_PT_MAX:
            raw = _thin(raw, _ROUTE_PT_MAX)
            thinned = True
    else:
        raw = _gz_paths(r["spine_gz"])
        spine_only = bool(raw)
    n_pt = sum(len(p) for p in raw)
    return {"source": source, "name": name, "stops": out,
            "totalKm": r["total_km"],
            "parts": raw,
            "nPart": r["n_part"] or len(raw),
            "geomKm": r["total_km"],
            "nPt": n_pt,
            "thinned": thinned,
            "spineOnly": spine_only,
            "coordN": sum(1 for s in out if s.get("lat") is not None)}


def collect_sources() -> List[dict]:
    """全部来源（新加的三套走向数据来源在最后，不改变原有名字优先级）"""
    return (_lib_lines() + _custom_lines() + _huirui_lines()
            + _ce_lines() + _osm_names() + _route_lines())


def _linegeom_raw(name: str) -> Optional[dict]:
    """rail.db 里按线名存的那份合并走向（line_geom，1 行 1 个线名）"""
    if not os.path.exists(RAIL_DB):
        return None
    try:
        with _ro(RAIL_DB) as c:
            r = c.execute("SELECT parts_gz,spine_gz,total_km,n_part,n_pt,file,props"
                          " FROM line_geom WHERE name=?", (name,)).fetchone()
    except sqlite3.DatabaseError:
        return None
    if not r:
        return None
    parts = _gz_paths(r["parts_gz"])
    spine = _gz_paths(r["spine_gz"])
    chain = (spine[0] if spine else (max(parts, key=len) if parts else []))
    return {"parts": parts, "chain": chain, "km": r["total_km"],
            "nPart": r["n_part"] or len(parts),
            "nPt": r["n_pt"] or sum(len(p) for p in parts),
            "file": r["file"] or "", "props": r["props"] or ""}


def _alias_stats(aliases: List[dict]) -> Dict[Tuple[str, str], dict]:
    """每条「已收」写法各有多少站、几个有定位、有没有走向

    面板上要一眼看出哪条能当**线路来源**（有走向：`hasGeom` / `km` / `nPart`），
    哪条能当**站点来源**（有站序：`nStop` / `coordN`）。同一来源的线名一次查完，
    别在循环里反复读 pool（`_lib_lines()` 每次都重读 JSON）。
    """
    out: Dict[Tuple[str, str], dict] = {}
    by_src: Dict[str, List[str]] = {}
    for a in aliases or []:
        by_src.setdefault(str(a.get("source") or ""), []).append(str(a.get("name") or ""))

    # 1) 三套走向来源：line_route 一次查完（顺带数一下有几站带定位）
    names = [n for s in ROUTE_SRC for n in by_src.get(s, [])]
    route: Dict[Tuple[str, str], dict] = {}
    if names and os.path.exists(RAIL_DB):
        try:
            with _ro(RAIL_DB) as c:
                qs = ",".join("?" * len(names))
                rows = c.execute(
                    f"SELECT name,source,n_stop,n_part,n_pt,total_km,stops_json,"
                    f" LENGTH(parts_gz) lp, LENGTH(spine_gz) ls FROM line_route"
                    f" WHERE name IN ({qs})", names).fetchall()
            for r in rows:
                try:
                    sst = json.loads(r["stops_json"] or "[]")
                except (TypeError, ValueError):
                    sst = []
                route[(r["source"], r["name"])] = {
                    "nStop": r["n_stop"] or len(sst),
                    "coordN": sum(1 for s in sst if isinstance(s, dict)
                                  and s.get("lat") is not None),
                    "hasGeom": bool(r["lp"] or r["ls"]),
                    "nPart": r["n_part"] or 0, "nPt": r["n_pt"] or 0,
                    "km": r["total_km"]}
        except sqlite3.DatabaseError:
            route = {}
    for src in ROUTE_SRC:
        for nm in by_src.get(src, []):
            out[(src, nm)] = (route.get((src, nm))
                              or {"nStop": 0, "coordN": 0, "hasGeom": False,
                                  "nPart": 0, "nPt": 0, "km": None})

    # 2) 站序来源：底表 / 资料库 / china-emu / 自建（站名反查定位；走向看 line_geom 有没有）
    pools = {"lib": _lib_lines, "huirui": _huirui_lines,
             "ce": _ce_lines, "custom": _custom_lines}
    other = [n for s, nms in by_src.items() if s in pools for n in nms]
    geoms: Dict[str, dict] = {}
    if other and os.path.exists(RAIL_DB):
        try:
            with _ro(RAIL_DB) as c:
                qs = ",".join("?" * len(other))
                for r in c.execute(f"SELECT name,total_km,n_part,n_pt FROM line_geom"
                                   f" WHERE name IN ({qs})", other):
                    geoms[r["name"]] = {"hasGeom": True, "km": r["total_km"],
                                        "nPart": r["n_part"] or 0,
                                        "nPt": r["n_pt"] or 0}
        except sqlite3.DatabaseError:
            geoms = {}
    for src, nms in by_src.items():
        if src not in pools:
            continue
        try:
            items = {it["name"]: it for it in pools[src]()}
        except sqlite3.DatabaseError:
            items = {}
        co = _coords()
        for nm in nms:
            it = items.get(nm) or {}
            stops = it.get("stops") or []
            g = geoms.get(nm) or {}
            out[(src, nm)] = {
                "nStop": len(stops),
                "coordN": sum(1 for s in stops if co.get(s.get("name"))),
                "hasGeom": bool(g.get("hasGeom")), "nPart": g.get("nPart", 0),
                "nPt": g.get("nPt", 0), "km": g.get("km") or it.get("total_km")}

    # 3) OSM 线名：只有走向、没有站序
    for nm in by_src.get("osm", []):
        out[("osm", nm)] = {"nStop": 0, "coordN": 0, "hasGeom": True,
                            "nPart": 0, "nPt": 0, "km": None}
    return out


def _route_raw(source: str, name: str) -> Optional[dict]:
    """来源包原始走向（保存用：**不抽稀**，和预览用的 `_route_stops` 分开）"""
    if not os.path.exists(RAIL_DB):
        return None
    try:
        with _ro(RAIL_DB) as c:
            r = c.execute("SELECT parts_gz,spine_gz,total_km,n_part,n_pt,note"
                          " FROM line_route WHERE name=? AND source=?",
                          (name, source)).fetchone()
    except sqlite3.DatabaseError:
        return None
    if not r:
        return None
    parts = _gz_paths(r["parts_gz"])
    spine = _gz_paths(r["spine_gz"])
    chain = (spine[0] if spine else
             (max(parts, key=len) if parts else []))
    return {"parts": parts, "chain": chain, "km": r["total_km"],
            "nPart": r["n_part"] or len(parts),
            "nPt": r["n_pt"] or sum(len(p) for p in parts),
            "note": r["note"] or ""}



# ---------------------------------------------------------------------------
# 资料库介绍（辉睿 https://www.huiruisoft.com/train/railway/）—— 正式名称口径
# ---------------------------------------------------------------------------

_KM_RE = re.compile(r"([\d,]+(?:\.\d+)?)")


def _num(s) -> Optional[float]:
    m = _KM_RE.search(str(s or ""))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _split_termini(s: str) -> Tuple[str, str]:
    t = str(s or "").strip()
    if not t:
        return "", ""
    for sep in (" - ", "－", "—", "~", "～", "至", "→"):
        if sep in t:
            a, b = t.split(sep, 1)
            return a.strip(), b.strip()
    return "", ""


def ensure_ref(force: bool = False) -> int:
    """把 data/huirui/lines.json（844 条：中文名称 / 全称 / 起止站点 / 介绍）灌进 ld_ref

    幂等：已经有数据就跳过，除非 force。**这是「正式名称」的唯一来源** ——
    介绍页里的「中文名称」就是规范线名，起止站点也照它。
    """
    init()
    if not os.path.exists(REF_JSON):
        return 0
    with conn() as c:
        n = c.execute("SELECT COUNT(*) FROM ld_ref").fetchone()[0]
        if n and not force:
            return n
        with open(REF_JSON, encoding="utf-8") as f:
            data = json.load(f)
        rows = []
        now = _now()
        for x in data:
            meta = x.get("meta") or {}
            name = str(meta.get("中文名称") or x.get("name") or "").strip()
            if not name:
                continue
            a, b = _split_termini(meta.get("起止站点") or "")
            rows.append((
                str(x.get("id") or ""), name, str(x.get("title") or ""),
                str(meta.get("中文全称") or ""),
                str(meta.get("铁路类型") or ""), str(meta.get("服务类型") or ""),
                str(meta.get("铁路等级") or ""), a, b,
                _num(meta.get("线路长度")), x.get("totalKm"),
                str(meta.get("设计速度") or ""), str(meta.get("运营速度") or ""),
                str(meta.get("开通日期") or ""), str(meta.get("别名") or ""),
                str(meta.get("外文名称") or ""), str(x.get("intro") or ""),
                str(x.get("url") or ""),
                json.dumps(x.get("stops") or [], ensure_ascii=False),
                json.dumps(x.get("junctions") or [], ensure_ascii=False), now,
            ))
        c.executemany(
            "INSERT OR REPLACE INTO ld_ref(hid,name,title,full_name,ltype,service,level,"
            "from_sta,to_sta,length_km,total_km,design_speed,run_speed,open_date,alias,"
            "foreign_name,intro,url,stops_json,junctions_json,updated_at)"
            " VALUES(" + ",".join("?" * 21) + ")", rows)
        c.execute("INSERT OR REPLACE INTO ld_meta(k,v) VALUES('ref_at',?)", (now,))
    log("ref", "", f"资料库介绍 {len(rows)} 条")
    return len(rows)


def ref_stats() -> dict:
    n = ensure_ref()
    with conn() as c:
        at = c.execute("SELECT v FROM ld_meta WHERE k='ref_at'").fetchone()
        # linked = 能挂上资料库介绍的线：面板点过的（src_line='huirui:*'）或名字直接对得上的
        # 两个 EXISTS 分开写（各自能走索引），合成一个 OR 会退化成全表笛卡尔积，5 s 起步
        linked = c.execute(
            "SELECT COUNT(*) FROM ld_line l WHERE l.src_line LIKE 'huirui:%' "
            " OR EXISTS(SELECT 1 FROM ld_ref r WHERE r.name=l.name)"
            " OR EXISTS(SELECT 1 FROM ld_ref r WHERE r.full_name=l.name)"
        ).fetchone()[0]
    return {"count": n, "at": (at["v"] if at else ""), "linked": linked}


_ALIAS_SEP = re.compile(r"[、，,;；/|]+")


def _alias_tokens(s: str) -> List[str]:
    return [x.strip() for x in _ALIAS_SEP.split(str(s or "")) if x.strip()]


def _ref_row(c: sqlite3.Connection, names: Iterable[str]) -> Optional[dict]:
    """按若干候选名找资料库记录：中文名称 → 中文全称 → 别名（**逐个别名整串比**）

    别名里常写「沪昆高速铁路沪杭段」这种带区段后缀的长写法，
    用 LIKE/Instr 做子串匹配会把「沪昆高速铁路」认成「沪杭高铁」——必须整串相等。
    """
    cand = [n for n in {str(x or "").strip() for x in names} if n]
    if not cand:
        return None
    for n in cand:
        r = c.execute("SELECT * FROM ld_ref WHERE name=? LIMIT 1", (n,)).fetchone()
        if r:
            return dict(r)
    for n in cand:
        r = c.execute("SELECT * FROM ld_ref WHERE full_name=? ORDER BY length(name) LIMIT 1",
                      (n,)).fetchone()
        if r:
            return dict(r)
    wanted = set(cand)
    best = None
    for r in c.execute("SELECT * FROM ld_ref WHERE alias<>''"):
        for tok in _alias_tokens(r["alias"]):
            if tok in wanted:
                if best is None or len(r["name"]) < len(best["name"]):
                    best = dict(r)
                break
    return best


def ref_get(name: str = "", hid: str = "") -> Optional[dict]:
    """一条线路的资料库介绍（整理台右侧「资料库介绍」块）"""
    ensure_ref()
    with conn() as c:
        if hid:
            r = c.execute("SELECT * FROM ld_ref WHERE hid=?", (hid,)).fetchone()
            return dict(r) if r else None
        nm = str(name or "").strip()
        if not nm:
            return None
        r = c.execute("SELECT * FROM ld_ref WHERE name=?", (nm,)).fetchone()
        if not r:                       # 规范名不在资料库里 → 用它的别名再试
            al = [x["name"] for x in c.execute(
                "SELECT a.name FROM ld_alias a JOIN ld_line l ON l.id=a.line_id"
                " WHERE l.name=?", (nm,))]
            return _ref_row(c, [nm] + al)
        return dict(r)


def ref_search(q: str = "", limit: int = 40) -> List[dict]:
    ensure_ref()
    like = f"%{q.strip()}%"
    with conn() as c:
        rows = c.execute(
            "SELECT hid,name,full_name,ltype,length_km,from_sta,to_sta FROM ld_ref"
            " WHERE name LIKE ? OR full_name LIKE ? OR instr(alias, ?)>0"
            " ORDER BY length(name), name LIMIT ?", (like, like, q.strip(), limit))
        return [dict(r) for r in rows]


def apply_ref(lid: int, what: str = "all") -> dict:
    """把资料库的中文名称 / 起止站点应用到这条线（what: name|fromto|all）"""
    init()
    d = line_detail(int(lid))
    if not d:
        raise ValueError("没有这条线路")
    ref = ref_get(d["name"])
    if not ref:
        al = [a["name"] for a in d.get("aliases") or []]
        ref = ref_get(next(iter(al), "")) if al else None
    if not ref:
        raise ValueError("资料库里找不到这条线（换个别名或先收一条资料库写法）")
    out = {"name": d["name"], "from": d["from_sta"], "to": d["to_sta"],
           "applied": [], "conflict": ""}
    with conn() as c:
        if what in ("name", "all") and ref.get("name") and ref["name"] != d["name"]:
            dup = c.execute("SELECT id,name FROM ld_line WHERE name=? AND id<>?",
                            (ref["name"], lid)).fetchone()
            if dup:
                out["conflict"] = f"「{ref['name']}」已经是另一条线的名字（id={dup['id']}）"
            else:
                nm = ref["name"]
                c.execute("UPDATE ld_line SET name=?, family=?, updated_at=? WHERE id=?",
                          (nm, family_of(nm), _now(), lid))
                out["name"] = nm
                out["applied"].append("名称")
        if what in ("fromto", "all"):
            a = (ref.get("from_sta") or "").strip()
            b = (ref.get("to_sta") or "").strip()
            if a or b:
                c.execute("UPDATE ld_line SET from_sta=COALESCE(?,from_sta),"
                          " to_sta=COALESCE(?,to_sta), updated_at=? WHERE id=?",
                          (a or None, b or None, _now(), lid))
                out["from"], out["to"] = a or d["from_sta"], b or d["to_sta"]
                out["applied"].append("起讫站点")
    log("ref-apply", str(lid), f"{what} {ref.get('name')} {out['applied']}")
    out["line"] = line_detail(lid)
    out["ref"] = ref
    return out



# ---------------------------------------------------------------------------
# 建初稿
# ---------------------------------------------------------------------------

STOP_SRC_ORDER = ["lib", "custom", "huirui", "ce"]


def _pick_stops(items: List[dict]) -> Tuple[List[dict], str]:
    for s in STOP_SRC_ORDER:
        cand = [it for it in items if it["source"] == s and it["stops"]]
        if cand:
            best = max(cand, key=lambda x: len(x["stops"]))
            return best["stops"], s
    return [], ""


class _Coords:
    """站名 → (lat, lon)：档案 + 人工坐标，railcore 已经合并好两种来源"""

    def __init__(self) -> None:
        self._c: Dict[str, Optional[Tuple[float, float]]] = {}

    def get(self, name: str) -> Optional[Tuple[float, float]]:
        if not name:
            return None
        if name not in self._c:
            import railcore
            try:
                self._c[name] = railcore.stations.coord(name)
            except Exception:                      # noqa: BLE001
                self._c[name] = None
        return self._c[name]


def _coords() -> "_Coords":
    return _Coords()


def _norm_stop_name(name: str) -> str:
    import cnrail
    try:
        return cnrail.station_name(name) or name
    except Exception:                              # noqa: BLE001
        return name


def build_seed(force: bool = False) -> dict:
    """把五套来源灌成整理库初稿。

    幂等：默认只补「rail.db 里有、整理库里还没被任何别名收下」的新线名，
    已经整理过（改过名、确认过、合并过）的一律不动。force=True 才清空重建。
    """
    init()
    items = collect_sources()
    stat = {"sources": {}, "groups": 0, "lines": 0, "aliases": 0,
            "segged": 0, "relations": 0, "stopped": 0, "skipped": 0}

    c = conn()
    try:
        if force:
            for t in ("ld_stop", "ld_relation", "ld_alias", "ld_line"):
                c.execute(f"DELETE FROM {t}")
        known = {r["name"] for r in c.execute("SELECT name FROM ld_alias")}
        _seed(c, items, stat, known, force)
        c.execute("INSERT OR REPLACE INTO ld_meta(k,v) VALUES('seeded_at',?)", (_now(),))
        c.execute("INSERT OR REPLACE INTO ld_meta(k,v) VALUES('seed_stat',?)",
                  (json.dumps(stat, ensure_ascii=False),))
        c.commit()
    finally:
        c.close()
    log("seed", "", json.dumps(stat, ensure_ascii=False))
    return stat


def _seed(c: sqlite3.Connection, items: List[dict], stat: dict,
          known: set, force: bool) -> None:
    # 按 family 分组（只分「不分段」的名；分段名单独成线）
    groups: Dict[str, List[dict]] = {}
    segged: List[dict] = []
    for it in items:
        if not it.get("name"):
            continue
        it["n"] = normalize(it["name"])
        stat["sources"][it["source"]] = stat["sources"].get(it["source"], 0) + 1
        if it["n"]["seg"]:
            segged.append(it)
        else:
            groups.setdefault(family_of(it["name"]), []).append(it)
    stat["groups"] = len(groups)
    stat["segged"] = len(segged)

    # 分段名自己也会有两种写法（京哈高速线京沈段 / 京哈高速铁路京沈段）：
    # 按「父线族 + 段名」再归并一次，别让同一段挂两遍
    seg_groups: Dict[str, List[dict]] = {}
    for it in segged:
        seg_groups.setdefault(f"{family_key(it['n'])}|{it['n']['seg']}", []).append(it)

    coords = _coords()
    fam_id: Dict[str, int] = {}
    name_id: Dict[str, int] = {}

    def add_line(name: str, fam: str, kind: str, items_: List[dict],
                 src_main: str = "", note: str = "") -> Optional[int]:
        row = c.execute("SELECT id FROM ld_line WHERE name=?", (name,)).fetchone()
        if row:
            lid = row["id"]
        else:
            taken = [it for it in items_ if it["name"] in known]
            if taken and not force:
                # 这条线的某个写法已经被整理过了，别再建一条新的
                r2 = c.execute("SELECT line_id FROM ld_alias WHERE source=? AND name=?",
                               (taken[0]["source"], taken[0]["name"])).fetchone()
                if r2:
                    name_id[name] = r2["line_id"]
                    stat["skipped"] += 1
                    return None
            km = next((it["total_km"] for it in items_
                       if it["source"] == src_main and it.get("total_km")), None)
            cur = c.execute(
                "INSERT INTO ld_line(name,family,kind,status,created_at,updated_at,src_main,note,"
                "total_km) VALUES(?,?,?,'draft',?,?,?,?,?)",
                (name, fam, kind, _now(), _now(), src_main, note, km))
            lid = cur.lastrowid
            stat["lines"] += 1
        name_id[name] = lid
        for it in items_:
            if it["name"] in known:
                continue
            c.execute("INSERT OR IGNORE INTO ld_alias(line_id,source,name,src_id)"
                      " VALUES(?,?,?,?)", (lid, it["source"], it["name"], it["src_id"]))
            stat["aliases"] += 1
        return lid

    def add_stops(lid: int, items_: List[dict]) -> None:
        if c.execute("SELECT 1 FROM ld_stop WHERE line_id=? LIMIT 1", (lid,)).fetchone():
            return
        stops, src = _pick_stops(items_)
        if not stops:
            return
        names = []
        for i, s in enumerate(stops, 1):
            nm = _norm_stop_name(s.get("name") or "")
            names.append(nm)
            ll = coords.get(nm)
            c.execute("INSERT OR REPLACE INTO ld_stop(line_id,seq,name,km,lon,lat,src)"
                      " VALUES(?,?,?,?,?,?,?)",
                      (lid, i, nm, s.get("km"),
                       ll[1] if ll else None, ll[0] if ll else None, src))
        c.execute("UPDATE ld_line SET from_sta=?, to_sta=? WHERE id=?",
                  (names[0], names[-1], lid))
        stat["stopped"] += 1

    for fam, its in groups.items():
        its.sort(key=lambda x: sort_key(x["source"], len(x["stops"])))
        head = its[0]
        n = head["n"]
        lid = add_line(head["name"], fam, n["kind"], its, head["source"],
                       note=n["note"])
        if lid:
            fam_id[fam] = lid
            add_stops(lid, its)

    for _key, its in seg_groups.items():
        its.sort(key=lambda x: sort_key(x["source"], len(x["stops"])))
        head = its[0]
        n = head["n"]
        lid = add_line(head["name"], family_key(n), n["kind"], its,
                       head["source"], note=n["note"])
        if lid:
            for x in its:
                x["_id"] = lid
            add_stops(lid, its)
    # 分段名的父线如果五套来源里都没有整条的名字（海南环岛高铁线只剩东西两段），
    # 补一条只有名字的整线挂上去，免得这两段永远悬空；站序留给人工补
    for _key, its in seg_groups.items():
        n = its[0]["n"]
        fam = family_key(n)
        if fam in fam_id:
            continue
        sp = split_seg(its[0]["name"])
        if not sp:
            continue
        parent_name = sp[0]
        if c.execute("SELECT 1 FROM ld_line WHERE name=?", (parent_name,)).fetchone():
            continue
        cur = c.execute(
            "INSERT INTO ld_line(name,family,kind,status,created_at,updated_at,src_main,note)"
            " VALUES(?,?,?,'draft',?,?,'inferred','由分段名反推出来的整线名，站序待补')",
            (parent_name, fam, n["kind"], _now(), _now()))
        fam_id[fam] = cur.lastrowid
        stat["lines"] += 1
        stat["inferred"] = stat.get("inferred", 0) + 1

    # 父子：分段名挂到整线名的规范线上
    for _key, its in seg_groups.items():
        lid_child = its[0].get("_id")
        if not lid_child:
            continue
        n = its[0]["n"]
        pid = fam_id.get(family_key(n))
        if not pid or pid == lid_child:
            continue
        cur = c.execute("INSERT OR IGNORE INTO ld_relation(parent_id,child_id,kind,note)"
                        " VALUES(?,?,'seg',?)", (pid, lid_child, n["seg"]))
        stat["relations"] += cur.rowcount


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def stats() -> dict:
    init()
    with conn() as c:
        g = lambda sql: c.execute(sql).fetchone()[0]        # noqa: E731
        return {
            "lines": g("SELECT COUNT(*) FROM ld_line"),
            "confirmed": g("SELECT COUNT(*) FROM ld_line WHERE status='confirmed'"),
            "aliases": g("SELECT COUNT(*) FROM ld_alias"),
            "stops": g("SELECT COUNT(*) FROM ld_stop"),
            "withStops": g("SELECT COUNT(DISTINCT line_id) FROM ld_stop"),
            "relations": g("SELECT COUNT(*) FROM ld_relation"),
            "families": g("SELECT COUNT(DISTINCT family) FROM ld_line WHERE family<>''"),
            "bySource": {r["source"]: r["n"] for r in
                         c.execute("SELECT source,COUNT(*) n FROM ld_alias GROUP BY source")},
            "seededAt": (c.execute("SELECT v FROM ld_meta WHERE k='seeded_at'").fetchone()
                         or ["—"])[0],
        }


def list_lines(q: str = "", kind: str = "", source: str = "", status: str = "",
               has_stops: bool = False, limit: int = 200, offset: int = 0) -> dict:
    init()
    where, args = [], []
    if q:
        where.append("(l.name LIKE ? OR EXISTS(SELECT 1 FROM ld_alias a WHERE a.line_id=l.id"
                     " AND a.name LIKE ?))")
        args += [f"%{q}%", f"%{q}%"]
    if kind:
        where.append("l.kind=?")
        args.append(kind)
    if status:
        where.append("l.status=?")
        args.append(status)
    if source:
        where.append("EXISTS(SELECT 1 FROM ld_alias a WHERE a.line_id=l.id AND a.source=?)")
        args.append(source)
    if has_stops:
        where.append("EXISTS(SELECT 1 FROM ld_stop s WHERE s.line_id=l.id)")
    w = ("WHERE " + " AND ".join(where)) if where else ""
    # 有站序的排前面（没站序的多数是只在 OSM 里出现的线名），同档按站数多少
    sql = (f"SELECT l.*, (SELECT COUNT(*) FROM ld_stop s WHERE s.line_id=l.id) n_stop,"
           f" (SELECT COUNT(*) FROM ld_alias a WHERE a.line_id=l.id) n_alias,"
           f" (SELECT COUNT(*) FROM ld_relation r WHERE r.parent_id=l.id) n_child"
           f" FROM ld_line l {w}"
           f" ORDER BY (SELECT COUNT(*) FROM ld_stop s WHERE s.line_id=l.id) DESC,"
           f" l.total_km DESC, l.id LIMIT ? OFFSET ?")
    with conn() as c:
        rows = [dict(r) for r in c.execute(sql, args + [limit, offset])]
        total = c.execute(f"SELECT COUNT(*) FROM ld_line l {w}", args).fetchone()[0]
        ids = [r["id"] for r in rows]
        al: Dict[int, List[dict]] = {}
        if ids:
            qs = ",".join("?" * len(ids))
            for r in c.execute(f"SELECT line_id,source,name FROM ld_alias"
                               f" WHERE line_id IN ({qs})", ids):
                al.setdefault(r["line_id"], []).append(
                    {"source": r["source"], "name": r["name"]})
    for r in rows:
        r["aliases"] = al.get(r["id"], [])
        r["kindLabel"] = KIND_LABEL.get(r["kind"], r["kind"])
        r["srcFlags"] = ("线路✓" if r.get("line_ok") else "") + \
                        ("站点✓" if r.get("stop_ok") else "")
    return {"items": rows, "total": total}


def line_detail(lid: int) -> Optional[dict]:
    init()
    with conn() as c:
        row = c.execute("SELECT * FROM ld_line WHERE id=?", (lid,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["kindLabel"] = KIND_LABEL.get(d["kind"], d["kind"])
        d["aliases"] = [dict(r) for r in c.execute(
            "SELECT id,source,name,src_id,ok FROM ld_alias WHERE line_id=?"
            " ORDER BY ok DESC, source", (lid,))]
        # 每条写法带体量：有没有走向 / 多少站 / 几个有定位（面板据此标「可当走向」「可当站点」）
        try:
            st = _alias_stats(d["aliases"])
            for a in d["aliases"]:
                a.update(st.get((a["source"], a["name"])) or {})
        except sqlite3.DatabaseError:
            pass
        d["srcLineItems"] = [{"source": a["source"], "name": a["name"]}
                             for a in d["aliases"] if a.get("ok")]
        d["stops"] = [dict(r) for r in c.execute(
            "SELECT seq,name,km,lon,lat,src,note FROM ld_stop WHERE line_id=? ORDER BY seq", (lid,))]
        d["parents"] = [dict(r) for r in c.execute(
            "SELECT r.parent_id id, r.kind, r.note, l.name FROM ld_relation r"
            " JOIN ld_line l ON l.id=r.parent_id WHERE r.child_id=?", (lid,))]
        d["children"] = [dict(r) for r in c.execute(
            "SELECT r.child_id id, r.kind, r.note, l.name,"
            " (SELECT COUNT(*) FROM ld_stop s WHERE s.line_id=l.id) n_stop"
            " FROM ld_relation r JOIN ld_line l ON l.id=r.child_id"
            " WHERE r.parent_id=? ORDER BY l.name", (lid,))]
        d["coordDone"] = sum(1 for s in d["stops"] if s["lat"] is not None)
        # 站序被人工改过 → 前端②默认选「本线现在的站序」，别再自动选回来源行
        d["stopsEdited"] = bool(d.get("stops_edited"))
        # 站点来源标签（geojson:文件名 / lib:线名 …）拆成好看的两段
        sp = (d.get("src_stop") or "").split(":", 1)
        d["stopSrc"] = ({"source": sp[0], "name": sp[1] if len(sp) > 1 else "",
                         "label": "GeoJSON" if sp[0] == "geojson"
                                  else ("人工" if sp[0] == "manual"
                                        else SRC_LABEL.get(sp[0], sp[0]))}
                        if sp and sp[0] else None)
        try:
            d["deskGeom"] = bool(c.execute("SELECT 1 FROM ld_geom WHERE line_id=?"
                                           " LIMIT 1", (lid,)).fetchone())
        except sqlite3.Error:
            d["deskGeom"] = False
        # 这条线有没有已经定为「基本线路」（全项目生效的那条）
        try:
            import linebase
            _b = linebase.get_by_ld(lid)
        except Exception:                          # noqa: BLE001
            _b = None
        d["baseInfo"] = ({"id": _b["id"], "name": _b["name"],
                          "nStop": len(_b["stops"]),
                          "geomSrc": _b.get("geom_src") or "",
                          "geomName": _b.get("geom_name") or "",
                          "covers": [c for c in (_b.get("covers") or "").split(",") if c]}
                         if _b else None)
    return d


def find_line(name: str) -> Optional[dict]:
    """按规范名 / 任意来源写法找一条线"""
    init()
    with conn() as c:
        r = c.execute("SELECT id FROM ld_line WHERE name=?", (name,)).fetchone()
        if not r:
            r = c.execute("SELECT line_id id FROM ld_alias WHERE name=?"
                          " ORDER BY id LIMIT 1", (name,)).fetchone()
        return line_detail(r["id"]) if r else None


def source_stops(source: str, name: str) -> dict:
    """某个来源里这条线自己的原始站序（整理台做「多方对比」用）

    每站补 lat/lon（站表反查），界面点来源写法时能直接画到地图上。
    ① 只看走向：所以「0 站但有走向」的来源也要把 parts 带出去给地图画。
    """
    if source in ROUTE_SRC:
        return _route_stops(source, name)
    if source == "osm":
        # OSM 线名只有走向、没有站序：按线名解析几何，① 选它时地图照样画得出线形
        try:
            import railgeom
            g = railgeom.geom(name, max_parts=400) or {}
        except Exception:                          # noqa: BLE001
            g = {}
        parts = _norm_parts(g.get("parts") or [])
        return {"source": source, "name": name, "stops": [], "coordN": 0,
                "parts": parts, "nPart": len(parts),
                "nPt": sum(len(p) for p in parts),
                "totalKm": g.get("km"), "geomKm": g.get("km")}
    pool = {"lib": _lib_lines, "huirui": _huirui_lines,
            "ce": _ce_lines, "custom": _custom_lines}.get(source)
    if not pool:
        return {"source": source, "name": name, "stops": [], "coordN": 0, "parts": []}
    for it in pool():
        if it["name"] == name:
            co = _Coords()
            stops = []
            for s in it["stops"]:
                ll = co.get(s.get("name"))
                stops.append({"name": s.get("name"), "km": s.get("km"),
                              "lat": ll[0] if ll else None,
                              "lon": ll[1] if ll else None})
            # 底表 / 自建 / 资料库 / china-emu 里有些线在 line_geom 存了走向：
            # 一起带上，点候选行才能画出真线形（没有就还是站点直连）
            g = _linegeom_raw(name) or {}
            parts = g.get("parts") or []
            thinned = False
            if parts and sum(len(p) for p in parts) > _ROUTE_PT_MAX:
                parts = _thin(parts, _ROUTE_PT_MAX)
                thinned = True
            return {"source": source, "name": name, "stops": stops,
                    "totalKm": it["total_km"],
                    "parts": parts, "nPart": g.get("nPart", 0),
                    "nPt": sum(len(p) for p in parts),
                    "geomKm": g.get("km"), "thinned": thinned,
                    "coordN": sum(1 for s in stops if s["lat"] is not None)}
    return {"source": source, "name": name, "stops": [], "coordN": 0, "parts": []}


_GEOM_KEYS: dict = {"at": 0.0, "route": set(), "geom": set()}


def _geom_name_sets() -> Tuple[set, set]:
    """哪些 (来源,线名) 自带走向、哪些线名在 line_geom 里有走向（60s 缓存）

    ① 线路来源的判据是「有没有走向」，**不是「有没有站点」**：line_route 里
    rg / hdx / cnrail 有大批 n_stop=0 但 parts_gz 完整的线路（京沪高铁就是），
    照样要能选来当这条线的走向。
    """
    now = time.time()
    if now - float(_GEOM_KEYS["at"] or 0) < 60 and _GEOM_KEYS["route"]:
        return _GEOM_KEYS["route"], _GEOM_KEYS["geom"]
    route, geom_names = set(), set()
    if os.path.exists(RAIL_DB):
        try:
            with _ro(RAIL_DB) as c:
                route = {(r["source"], r["name"]) for r in c.execute(
                    "SELECT source,name FROM line_route"
                    " WHERE parts_gz IS NOT NULL OR spine_gz IS NOT NULL")}
                geom_names = {r["name"] for r in c.execute("SELECT name FROM line_geom")}
        except sqlite3.DatabaseError:
            pass
    _GEOM_KEYS.update({"at": now, "route": route, "geom": geom_names})
    return route, geom_names


def search_source(q: str, limit: int = 40) -> List[dict]:
    """在各来源里找线名（整理台「+ 收一条」时挑来源用）

    ``has_geom`` 一起带出去：① 线路来源只要「有走向」就能选，哪怕 0 站；
    ② 站点来源才要求至少 2 站。
    """
    out, seen = [], set()
    ql = (q or "").strip()
    route_keys, geom_names = _geom_name_sets()
    for it in collect_sources():
        if ql and ql not in (it["name"] or ""):
            continue
        k = (it["source"], it["name"])
        if k in seen:
            continue
        seen.add(k)
        has_geom = (k in route_keys or it["source"] == "osm"
                    or (it["source"] not in ROUTE_SRC and it["name"] in geom_names))
        out.append({"source": it["source"], "name": it["name"],
                    "n_stop": len(it["stops"]), "totalKm": it["total_km"],
                    "has_geom": bool(has_geom)})
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# 编辑
# ---------------------------------------------------------------------------

_LINE_FIELDS = ("name", "kind", "status", "total_km", "from_sta", "to_sta", "note")

def save_line(lid: int, payload: dict) -> dict:
    init()
    with conn() as c:
        row = c.execute("SELECT * FROM ld_line WHERE id=?", (lid,)).fetchone()
        if not row:
            raise ValueError("没有这条线路")
        sets, args = [], []
        for k in _LINE_FIELDS:
            if k not in payload:
                continue
            v = payload[k]
            if k == "name":
                v = str(v or "").strip()
                if not v:
                    raise ValueError("线名不能为空")
                dup = c.execute("SELECT id FROM ld_line WHERE name=? AND id<>?",
                                (v, lid)).fetchone()
                if dup:
                    raise ValueError(f"已经有叫「{v}」的线路了")
            if k == "total_km" and v in ("", None):
                v = None
            if k == "kind":
                v = str(v or "")
            sets.append(f"{k}=?")
            args.append(v)
        if sets:
            sets.append("updated_at=?")
            args.append(_now())
            args.append(lid)
            c.execute(f"UPDATE ld_line SET {', '.join(sets)} WHERE id=?", args)
        # 改名时重算归一键；新名字认不出后缀（改成「京沪高速线①」这种）就保留原族，
        # 否则一改名就把自己和同族写法拆散了
        if "name" in payload:
            nm = c.execute("SELECT name,family FROM ld_line WHERE id=?", (lid,)).fetchone()
            d = normalize(nm["name"])
            fam = f"{d['base']}|{d['kind']}" if d["kind"] else (nm["family"] or "")
            c.execute("UPDATE ld_line SET family=? WHERE id=?", (fam, lid))
    log("save", str(lid), json.dumps(payload, ensure_ascii=False)[:400])
    out = line_detail(lid) or {}
    # 「基本信息 保存后要正式保存」（2026-11-06 用户口径）：接口就在这里落库，
    # 明确回一个 saved/savedAt 给前端，别让用户以为还停在草稿里。
    out["saved"] = True
    out["savedAt"] = _now()
    return out


def create_line(name: str, kind: str = "", note: str = "") -> dict:
    init()
    nm = str(name or "").strip()
    if not nm:
        raise ValueError("线名不能为空")
    with conn() as c:
        if c.execute("SELECT 1 FROM ld_line WHERE name=?", (nm,)).fetchone():
            raise ValueError(f"已经有叫「{nm}」的线路了")
        d = normalize(nm)
        cur = c.execute(
            "INSERT INTO ld_line(name,family,kind,status,created_at,updated_at,note,src_main)"
            " VALUES(?,?,?,'draft',?,?,?,'manual')",
            (nm, family_of(nm), kind or d["kind"], _now(), _now(), note))
        lid = cur.lastrowid
    log("new", str(lid), nm)
    return line_detail(lid) or {}


def delete_line(lid: int) -> None:
    init()
    with conn() as c:
        c.execute("DELETE FROM ld_relation WHERE parent_id=? OR child_id=?", (lid, lid))
        c.execute("DELETE FROM ld_stop WHERE line_id=?", (lid,))
        c.execute("DELETE FROM ld_alias WHERE line_id=?", (lid,))
        c.execute("DELETE FROM ld_geom WHERE line_id=?", (lid,))     # 导入的走向一起删
        c.execute("DELETE FROM ld_line WHERE id=?", (lid,))
    log("delete", str(lid))


def add_alias(lid: int, source: str, name: str, src_id: str = "") -> dict:
    init()
    nm = str(name or "").strip()
    src = str(source or "").strip()
    if not nm or src not in SRC_LABEL:
        raise ValueError("来源或线名不对")
    with conn() as c:
        dup = c.execute("SELECT line_id FROM ld_alias WHERE source=? AND name=?",
                        (src, nm)).fetchone()
        if dup and dup["line_id"] != lid:
            raise ValueError(f"「{nm}」已经被另一条线收作 {SRC_LABEL[src]} 别名")
        if not dup:
            c.execute("INSERT INTO ld_alias(line_id,source,name,src_id) VALUES(?,?,?,?)",
                      (lid, src, nm, src_id))
        c.execute("UPDATE ld_line SET updated_at=? WHERE id=?", (_now(), lid))
    log("alias+", str(lid), f"{src}:{nm}")
    return line_detail(lid) or {}


def del_alias(alias_id: int) -> dict:
    init()
    with conn() as c:
        r = c.execute("SELECT line_id FROM ld_alias WHERE id=?", (alias_id,)).fetchone()
        if not r:
            raise ValueError("没有这个别名")
        lid = r["line_id"]
        c.execute("DELETE FROM ld_alias WHERE id=?", (alias_id,))
        c.execute("UPDATE ld_line SET updated_at=? WHERE id=?", (_now(), lid))
    log("alias-", str(lid), str(alias_id))
    return line_detail(lid) or {}


def save_stops(lid: int, stops: List[dict]) -> dict:
    """整份覆盖站序（页面上拖动 / 增删之后一次性保存）

    没带坐标的站按站名查档案补上——手打一个站名也不该在地图上是个洞。
    """
    init()
    coords = _coords()
    with conn() as c:
        if not c.execute("SELECT 1 FROM ld_line WHERE id=?", (lid,)).fetchone():
            raise ValueError("没有这条线路")
        c.execute("DELETE FROM ld_stop WHERE line_id=?", (lid,))
        for i, s in enumerate(stops, 1):
            nm = _norm_stop_name(str((s or {}).get("name") or "").strip())
            if not nm:
                continue
            km = s.get("km")
            lat, lon = s.get("lat"), s.get("lon")
            if lat is None or lon is None:
                ll = coords.get(nm)
                if ll:
                    lat, lon = ll[0], ll[1]
            c.execute("INSERT INTO ld_stop(line_id,seq,name,km,lon,lat,src,note)"
                      " VALUES(?,?,?,?,?,?,?,?)",
                      (lid, i, nm, km, lon, lat,
                       s.get("src") or "manual", s.get("note") or ""))
        n = c.execute("SELECT COUNT(*) FROM ld_stop WHERE line_id=?", (lid,)).fetchone()[0]
        first = c.execute("SELECT name FROM ld_stop WHERE line_id=? ORDER BY seq LIMIT 1",
                          (lid,)).fetchone()
        last = c.execute("SELECT name FROM ld_stop WHERE line_id=? ORDER BY seq DESC LIMIT 1",
                         (lid,)).fetchone()
        # 手工改过站序（删站 / 加站 / 改名 / 上下移）就记一笔：② 再确认时
        # 要确认「这份站序」，而不是按 src_stop 把没改过的来源站序灌回来。
        c.execute("UPDATE ld_line SET total_km=COALESCE(?,total_km), from_sta=?, to_sta=?,"
                  " stop_ok=0, stops_edited=1, updated_at=? WHERE id=?",
                  (stops[-1].get("km") if stops else None,
                   first["name"] if first else None, last["name"] if last else None,
                   _now(), lid))
    log("stops", str(lid), f"{len(stops)} 站")
    return line_detail(lid) or {}


def _split_stop_names(names) -> List[str]:
    """「新增车站」弹窗一次可以填多个站名。

    字符串按换行 / 逗号 / 顿号 / 分号 / 制表符切；已经是数组就只逐项去空白
    （不再切，站名本身可能带这些符号）。不按空格切：站名里偶尔会有空格。
    """
    if names is None:
        return []
    raw = names if isinstance(names, (list, tuple)) else re.split(
        r"[,，;；、\t\r\n]+", str(names))
    return [str(x).strip() for x in raw if str(x).strip()]


def insert_stops(lid: int, names, pos: int = -1) -> dict:
    """在站序的指定位置插入车站（2026-11-09：新增车站要能指定插入位置）

    ``pos`` 是**新站插入后的 0-based 起始下标**：
      ``0`` → 插到最前面；``i+1`` → 插在「第 i+1 站」之后；
      ``-1`` 或 ``>= 站数`` → 追加到末尾。
    越界一律夹到 ``[0, n]``、不报错（页面下拉和库里的站序可能差一拍）。

    - 新站 ``km`` 留空、**不插值**（口径同 ``reverse_stops`` 和项目一贯做法）；
      原有各站的里程 / 坐标 / src / note 一个字不动；
    - 坐标交给 ``save_stops()`` 按站名查档案补，查不到就留空（页面标「没定位」）；
    - 落库走 ``save_stops()`` ⇒ 和手工「↑↓ / 改名 / 删站」完全同口径：
      ``stop_ok=0``、``stops_edited=1``、起讫站跟着更新，② 默认回到「现在的站序」。
    """
    init()
    lid = int(lid)
    with conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT seq,name,km,lon,lat,src,note FROM ld_stop"
            " WHERE line_id=? ORDER BY seq", (lid,))]
    add = [{"name": nm, "km": None, "lat": None, "lon": None,
            "src": "manual", "note": ""} for nm in _split_stop_names(names)]
    if not add:
        raise ValueError("先填要插入的站名")
    n = len(rows)
    try:
        p = int(pos)
    except (TypeError, ValueError):
        p = -1
    if p < 0:
        p = n
    p = max(0, min(p, n))
    out = rows[:p] + add + rows[p:]
    d = save_stops(lid, out)          # 顺带补坐标 / 重排 seq / 更新起讫与标记
    d["inserted"] = len(add)
    d["insertPos"] = p
    log("stops-insert", str(lid),
        "%d 站在第 %d 位：%s" % (len(add), p + 1,
                                "、".join(x["name"] for x in add)))
    return d


def reverse_stops(lid: int) -> dict:
    """站序反向 + 里程倒排重排（2026-11-06 用户口径：站序提前到 ② 下面，可选反向）

    和主站 ``lineseg._reverse_stops()`` 完全同口径：
      ``里程 = 原末站里程 − 原里程``，新起点强制 0，原本缺里程的站仍留空、不插值。
    只改 `ld_stop` 的顺序与 `km`，坐标 / 站名 / src / note 原样搬；不动 `total_km`
    （反向后末站里程本来就回到原总里程），也不动 `stop_ok`（只是换方向，站点来源没变）。
    """
    init()
    lid = int(lid)
    with conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT seq,name,km,lon,lat,src,note FROM ld_stop"
            " WHERE line_id=? ORDER BY seq", (lid,))]
        if len(rows) < 2:
            raise ValueError("至少要有 2 个站才能反向")
        kms = [r["km"] for r in rows if r["km"] is not None]
        base = max(kms) if kms else None
        out: List[dict] = []
        for i, s in enumerate(reversed(rows)):
            km = s["km"]
            if base is None:
                v = None
            elif i == 0:
                v = 0.0                      # 原来的终点成了起点，里程归零
            elif km is not None:
                v = round(base - float(km), 1)
            else:
                v = None                     # 缺的仍然缺，不插值
            out.append({**s, "km": v})
        c.execute("DELETE FROM ld_stop WHERE line_id=?", (lid,))
        for i, s in enumerate(out, 1):
            c.execute("INSERT INTO ld_stop(line_id,seq,name,km,lon,lat,src,note)"
                      " VALUES(?,?,?,?,?,?,?,?)",
                      (lid, i, s["name"], s["km"], s["lon"], s["lat"],
                       s["src"] or "manual", s["note"] or ""))
        # 反向后站序也和来源那份不一样了，② 的默认选中要回到「本线现在的站序」；
        # stop_ok 不动（只是换方向，来源没变），但方向变了仍建议再点一次③。
        c.execute("UPDATE ld_line SET from_sta=?, to_sta=?, stops_edited=1,"
                  " updated_at=? WHERE id=?",
                  (out[0]["name"], out[-1]["name"], _now(), lid))
    log("stops-reverse", str(lid),
        "%d 站，首站 %s / 末站 %s %s km" % (len(out), out[0]["name"],
                                          out[-1]["name"], out[-1]["km"]))
    d = line_detail(lid) or {}
    d["reversed"] = True
    return d


def fill_stops(lid: int, source: str, name: str) -> dict:
    """从某个来源把站序搬过来（页面「用 XX 来源的站序」按钮）"""
    d = source_stops(source, name)
    stops = d.get("stops") or []
    if not stops:
        raise ValueError(f"{SRC_LABEL.get(source, source)} 里没有「{name}」的站序")
    coords = _coords()
    # china-emu 记的是 K 里程（全线累计），陇海西宝段是 K1073→K1256：
    # 搬过来时统一减掉首站，换成段内里程，否则总里程会被算成 1256
    base = stops[0].get("km") or 0
    out = []
    for s in stops:
        nm = _norm_stop_name(s.get("name") or "")
        # 来源包自带的定位优先（line_route 的站序带 lat/lon，是这条线自己的位置），
        # 只有它没有才去站名档案里反查
        lat, lon = s.get("lat"), s.get("lon")
        if lat is None or lon is None:
            ll = coords.get(nm)
            lat, lon = (ll[0], ll[1]) if ll else (None, None)
        km = s.get("km")
        out.append({"name": nm, "km": (km - base) if km is not None else None,
                    "lat": lat, "lon": lon, "src": source})
    d = save_stops(lid, out)
    # 记下「站序来自哪个来源」（还没确认，等人工点「确认站点来源」）
    # 这份站序是刚从来源搬来的、没有人工改动，清掉 stops_edited。
    with conn() as c:
        c.execute("UPDATE ld_line SET src_stop=?, stop_ok=0, stops_edited=0,"
                  " updated_at=? WHERE id=?",
                  (f"{source}:{name}", _now(), lid))
    return line_detail(lid) or d


def merge_lines(ids: List[int], into: int = 0, name: str = "") -> dict:
    """把几条线并成一条：别名、站序、父子关系全部转到目标线上

    站序默认保留目标线自己的；目标线没有站序时，借用站数最多的那一条。
    """
    init()
    ids = [int(x) for x in ids if int(x)]
    if len(ids) < 2:
        raise ValueError("至少选两条线才能合并")
    into = int(into or ids[0])
    if into not in ids:
        ids.append(into)
    with conn() as c:
        rows = {r["id"]: dict(r) for r in
                c.execute(f"SELECT * FROM ld_line WHERE id IN ({','.join('?' * len(ids))})",
                          ids)}
        if into not in rows:
            raise ValueError("目标线路不存在")
        keep = rows[into]
        has = c.execute("SELECT COUNT(*) FROM ld_stop WHERE line_id=?", (into,)).fetchone()[0]
        if not has:
            best, bn = 0, 0
            for i in ids:
                n = c.execute("SELECT COUNT(*) FROM ld_stop WHERE line_id=?", (i,)).fetchone()[0]
                if n > bn:
                    best, bn = i, n
            if best:
                for s in c.execute("SELECT * FROM ld_stop WHERE line_id=? ORDER BY seq", (best,)):
                    c.execute("INSERT INTO ld_stop(line_id,seq,name,km,lon,lat,src,note)"
                              " VALUES(?,?,?,?,?,?,?,?)",
                              (into, s["seq"], s["name"], s["km"], s["lon"], s["lat"],
                               s["src"], s["note"]))
        for i in ids:
            if i == into:
                continue
            # 别名全转过来；重名的丢掉。
            # 先把这批别名读出来再改（边查边改同一张表会漏行）
            rows_al = c.execute("SELECT id,source,name FROM ld_alias WHERE line_id=?",
                                (i,)).fetchall()
            for a in rows_al:
                dup = c.execute("SELECT 1 FROM ld_alias WHERE source=? AND name=?",
                                (a["source"], a["name"])).fetchone()
                if dup:
                    c.execute("DELETE FROM ld_alias WHERE id=?", (a["id"],))
                else:
                    c.execute("UPDATE ld_alias SET line_id=? WHERE id=?", (into, a["id"]))
            # 被并掉的那条线的规范名本身也得留个写法，否则搜索里就再也找不到它了
            old = rows[i]
            if old["name"] and old["name"] != keep["name"]:
                hit = c.execute("SELECT 1 FROM ld_alias WHERE name=?", (old["name"],)).fetchone()
                if not hit:
                    c.execute("INSERT OR IGNORE INTO ld_alias(line_id,source,name,src_id)"
                              " VALUES(?,?,?,'')",
                              (into, old["src_main"] or "custom", old["name"]))
            # 父子关系转过来，指向自己的丢掉
            for r in c.execute("SELECT parent_id,child_id,kind,note FROM ld_relation"
                               " WHERE parent_id=? OR child_id=?", (i, i)).fetchall():
                p, ch = (into, r["child_id"]) if r["parent_id"] == i else (r["parent_id"], into)
                if p == ch or p == i or ch == i:
                    continue
                c.execute("INSERT OR IGNORE INTO ld_relation(parent_id,child_id,kind,note)"
                          " VALUES(?,?,?,?)", (p, ch, r["kind"], r["note"]))
            c.execute("DELETE FROM ld_relation WHERE parent_id=? OR child_id=?", (i, i))
            c.execute("DELETE FROM ld_stop WHERE line_id=?", (i,))
            c.execute("DELETE FROM ld_line WHERE id=?", (i,))
        if name:
            nm = str(name).strip()
            if nm and nm != keep["name"]:
                dup = c.execute("SELECT id FROM ld_line WHERE name=? AND id<>?",
                                (nm, into)).fetchone()
                if dup:
                    raise ValueError(f"已经有叫「{nm}」的线路了")
                c.execute("UPDATE ld_line SET name=?, family=? WHERE id=?",
                          (nm, family_of(nm), into))
        c.execute("UPDATE ld_line SET updated_at=?, status='confirmed' WHERE id=?",
                  (_now(), into))
    log("merge", str(into), f"{len(ids)} 条合并")
    return line_detail(into) or {}


def merged_geom(ld_id: int, name: str = "", with_parts: bool = True,
                max_parts: int = 400) -> Optional[dict]:
    """整理台「合并保存」落下的真实走向（ld_geom），形状对齐 geoimport.geom()

    基本线路定稿后 `line_base.ld_id` 指着整理台那条线。主站以前只能按线名去
    rail.db / line_geom 找走向，找不到就退到同名的旧版本 —— 整理台里刚合并
    好的那份等于没跟过来，用户会以为「整合完到车站/线路就没了」。
    这里只读 linedesk.db，**不往 rail.db 写任何东西**。
    """
    if not ld_id:
        return None
    init()
    with conn() as c:
        row = c.execute(
            "SELECT name,filename,total_km,n_part,n_pt,props,spine_gz,parts_gz"
            " FROM ld_geom WHERE line_id=? ORDER BY created_at DESC LIMIT 1",
            (int(ld_id),)).fetchone()
    if not row:
        return None
    nm = (name or row["name"] or "").strip()
    try:
        props = json.loads(row["props"] or "{}")
    except (TypeError, ValueError):
        props = {}
    out = {
        "name": nm, "ask": nm,
        "km": row["total_km"], "kmMain": row["total_km"],
        "nPart": row["n_part"], "nFeat": row["n_part"], "nPt": row["n_pt"],
        "match": "geojson", "source": "geojson",
        "file": (row["filename"] or props.get("source") or "整理台合并走向"),
        "ldGeom": True,
    }
    if not with_parts:
        return out
    import gzip
    parts = None
    # 原始多段优先：spine 是抽稀 + 补断口的主轴，画地图会失真；而且 2026-11-06
    # 之前③合并保存写出的 spine 点序是反的（见 _norm_chain）。这一步和整理台
    # desk_geom() 对齐，两边画同一份走向。spine 只在老数据没有多段时兜底。
    for blob, single in ((row["parts_gz"], False), (row["spine_gz"], True)):
        if not blob:
            continue
        try:
            got = json.loads(gzip.decompress(blob).decode("utf-8"))
        except (OSError, ValueError, TypeError, EOFError):
            continue
        norm = _norm_parts(got, single=single)
        if norm:
            parts = norm
            break
    if not parts:
        return None
    if max_parts and len(parts) > max_parts:
        parts = sorted(parts, key=len, reverse=True)[:max_parts]
    out["parts"] = parts
    out["crs"] = "WGS84"
    return out


def publish(lid: int, geom_src: str = "", geom_name: str = "",
            name: str = "", replace_id: int = 0,
            geom_name_given: bool = False) -> dict:
    """把整理台这条线定为「基本线路」，写进 rail.db 的 line_base（全面接管）

    geom_src / geom_name：走向配的哪条 OSM 线（osm）或哪份 GeoJSON（geojson）。
    geom_name_given：调用方**明确传了** geomName（哪怕是空串）—— 空串是有意义的
    （留空 = 按规范名找导入走向），这时不要拿整理台的旧值回填它。
    同名的基本线路要重定稿时传 replace_id。
    """
    d = line_detail(int(lid))
    if not d:
        raise ValueError("整理台里没有这条线")
    if d["status"] != "confirmed":
        raise ValueError("先「确认这条」，再定为基本线路")
    # 定基本线路时没另选走向：用「合并保存」定下的那一份（来源包走向 / OSM）
    saved_src = (d.get("geom_src") or "").strip()
    saved_name = (d.get("geom_name") or "").strip()
    if not geom_src:
        geom_src = saved_src
    # 脚本 / 旧版页面只给了走向类型、没给线名时，补上合并时定下的那份，
    # 免得重新定稿把 geom_name 冲空、主站退回按线名找旧走向。
    if not geom_name and not geom_name_given and geom_src == saved_src:
        geom_name = saved_name
    stops = [s for s in d["stops"] if (s.get("name") or "").strip()]
    if len(stops) < 2:
        raise ValueError("站序至少要有两站")
    covers = ["%s:%s" % (a["source"], a["name"]) for a in d["aliases"]]
    nm = (name or d["name"]).strip()
    if not replace_id:
        # 同名基本线路已经存在时：
        #   · 就是本条整理线自己发过的（③「重新合并保存」）→ 直接覆盖自己那份；
        #   · 合并脚本写的「临时基本线路」→ 自动让位；
        #   · 别的线人工定稿的绝不覆盖，还是要求显式传 replaceId。
        import linebase
        old = linebase.get(nm)
        if old:
            same_ld = old.get("ld_id") and int(old["ld_id"]) == int(d["id"])
            if same_ld or "临时基本线路" in (old.get("note") or ""):
                replace_id = int(old.get("id") or 0)
    import linebase
    out = linebase.publish({
        "name": nm,
        "ldId": d["id"],
        "totalKm": d["total_km"],
        "from": d["from_sta"], "to": d["to_sta"],
        "geomSrc": geom_src or "",
        "geomName": geom_name or "",
        "covers": covers,
        "note": d.get("note") or "",
        "replaceId": int(replace_id or 0),
        "stops": [{"name": s["name"], "km": s["km"], "lon": s["lon"], "lat": s["lat"]}
                  for s in stops],
    })
    log("publish", d["name"], "基本线路：站点 %d，走向 %s %s"
        % (len(stops), geom_src or "-", geom_name or "-"))
    return out


# ---------------------------------------------------------------------------
# 来源：线路来源 / 站点来源，分别确认
# ---------------------------------------------------------------------------

def _alias_ensure(c: sqlite3.Connection, lid: int, source: str,
                  name: str, src_id: str = "") -> int:
    """把某个来源写法挂到这条线下（已经有了就复用；被别人收着就报错）"""
    src = str(source or "").strip()
    nm = str(name or "").strip()
    if src not in SRC_LABEL:
        raise ValueError(f"不认识的来源：{src}")
    if not nm:
        raise ValueError("来源线名不能为空")
    r = c.execute("SELECT id,line_id FROM ld_alias WHERE source=? AND name=?",
                  (src, nm)).fetchone()
    if r:
        if r["line_id"] != lid:
            raise ValueError(f"「{nm}」已经被另一条线收作 {SRC_LABEL[src]} 写法")
        return r["id"]
    cur = c.execute("INSERT INTO ld_alias(line_id,source,name,src_id,ok)"
                    " VALUES(?,?,?,?,0)", (lid, src, nm, src_id))
    return cur.lastrowid


def set_line_sources(lid: int, items: List[dict], rename: bool = False) -> dict:
    """确认「① 线路来源」：只登记这条线用哪一家的走向（OSM 值）

    2026-11-06 用户口径：① 用来确认铁路线的 osm 值，和线路停靠的车站无关，
    保存只保存铁路线的 osm 值。所以这里只写 `ld_alias.ok` / `ld_line.src_line` /
    `line_ok`，**不碰 `ld_stop`（站序）、不碰 `ld_geom`（走向几何）**，也不再顺手
    改规范名 / 起讫站点 —— 那些是「基本信息」和资料库按钮的职责。
    `rename=True` 是给老脚本留的兼容开关（按资料库中文名称定名 + 补起讫）。
    """
    init()
    lid = int(lid)
    picks = []
    for it in items or []:
        src = str((it or {}).get("source") or "").strip()
        nm = str((it or {}).get("name") or "").strip()
        if src and nm and (src, nm) not in picks:
            picks.append((src, nm))
    if not picks:
        raise ValueError("先选至少一条线路来源")
    out = {"renamed": "", "conflict": "", "fromto": "", "warnings": []}
    with conn() as c:
        row = c.execute("SELECT * FROM ld_line WHERE id=?", (lid,)).fetchone()
        if not row:
            raise ValueError("没有这条线路")
        for src, nm in picks:
            _alias_ensure(c, lid, src, nm)
        c.execute("UPDATE ld_alias SET ok=0 WHERE line_id=?", (lid,))
        for src, nm in picks:
            c.execute("UPDATE ld_alias SET ok=1 WHERE line_id=? AND source=? AND name=?",
                      (lid, src, nm))
        # ① 只保存「用哪一家的走向」：选了来源就把之前「① 导入走向」留下的
        # GeoJSON 标记作废，免得 ③ 误以为还该用那份。
        # 但 geom_src=route:xxx 是「③ 合并保存」定下的成果，**不能清**：
        # 清掉后主站会退回按线名找 OSM 几何，用户只是重新点了一次①就把
        # 合并好的走向弄丢了（2026-11-06 实测）。
        c.execute("UPDATE ld_line SET src_line=?, line_ok=1, updated_at=? WHERE id=?",
                  ("|".join(f"{s}:{n}" for s, n in picks), _now(), lid))
        c.execute("UPDATE ld_line SET geom_src='', geom_name=''"
                  " WHERE id=? AND geom_src='geojson'", (lid,))

        # 规范名 = 资料库的「中文名称」（用户口径：介绍里的中文名称就是正式名称）
        if rename:
            names = [nm for _s, nm in picks] + [row["name"]]
            ref = _ref_row(c, names)
            if ref and ref.get("name") and ref["name"] != row["name"]:
                dup = c.execute("SELECT id FROM ld_line WHERE name=? AND id<>?",
                                (ref["name"], lid)).fetchone()
                if dup:
                    out["conflict"] = (f"资料库名称「{ref['name']}」已被另一条线占用"
                                       f"（id={dup['id']}），名字先没动")
                    out["warnings"].append(out["conflict"])
                else:
                    c.execute("UPDATE ld_line SET name=?, family=?, updated_at=? WHERE id=?",
                              (ref["name"], family_of(ref["name"]), _now(), lid))
                    out["renamed"] = ref["name"]
            # 起讫站点也照资料库补（已经有了就不覆盖，人工改过的优先）
            if ref:
                a = (ref.get("from_sta") or "").strip()
                b = (ref.get("to_sta") or "").strip()
                if (a and not row["from_sta"]) or (b and not row["to_sta"]):
                    c.execute("UPDATE ld_line SET from_sta=COALESCE(from_sta,?),"
                              " to_sta=COALESCE(to_sta,?), updated_at=? WHERE id=?",
                              (a or None, b or None, _now(), lid))
                    out["fromto"] = f"{a or row['from_sta']} → {b or row['to_sta']}"
    log("src-line", str(lid), " ".join(f"{s}:{n}" for s, n in picks))
    out["line"] = line_detail(lid)
    return out


def confirm_stop_source(lid: int, source: str, name: str, fill: bool = False) -> dict:
    """确认「站点来源」：站序来自哪个来源（geojson / lib / huirui / ce / custom / manual）

    - 外部来源 + ``fill=True``：先把那一家的站序搬过来再确认（② 里点了来源行）。
    - ``source='manual'``（或空）= 确认**「本线现在的站序」**：只置 ``stop_ok=1``，
      **不搬站序、不改 src_stop、不改每站 src**。站序被人工删站 / 加站 / 改名 /
      反向之后必须走这条，否则刚删掉的那一站会被来源那份完整站序灌回来
      （2026-11-06 用户报「删一站再用它并确认，没保存成功」）。
    """
    init()
    lid = int(lid)
    src = str(source or "").strip()
    nm = str(name or "").strip()
    current_only = src in ("", "manual")
    if not current_only and src not in SRC_LABEL and src != "geojson":
        raise ValueError("不认识的来源")
    with conn() as c:
        row = c.execute("SELECT name,src_stop FROM ld_line WHERE id=?", (lid,)).fetchone()
        if not row:
            raise ValueError("没有这条线路")
    if not nm and src != "geojson" and not current_only:
        nm = row["name"]
    if fill and not current_only and src != "geojson":
        fill_stops(lid, src, nm)
    with conn() as c:
        n = c.execute("SELECT COUNT(*) FROM ld_stop WHERE line_id=?", (lid,)).fetchone()[0]
        if n < 2:
            raise ValueError("这条线还没有站序（至少两站），先导入再确认站点来源")
        if current_only:
            # 只确认当前这份站序。来源标记保持原样（还是那一家，只是站序被人工修订过）
            tag = row["src_stop"] or f"manual:{row['name']}"
            c.execute("UPDATE ld_line SET stop_ok=1, stops_edited=0, updated_at=? WHERE id=?",
                      (_now(), lid))
        else:
            tag = f"{src}:{nm}"
            c.execute("UPDATE ld_line SET src_stop=?, stop_ok=1, stops_edited=0,"
                      " updated_at=? WHERE id=?", (tag, _now(), lid))
            c.execute("UPDATE ld_stop SET src=? WHERE line_id=?", (src, lid))
    log("src-stop", str(lid), tag)
    d = line_detail(lid)
    d["ok"] = bool(d.get("line_ok") and d.get("stop_ok"))
    return d


def clear_source(lid: int, kind: str) -> dict:
    """取消确认：kind = line / stop / both"""
    init()
    lid = int(lid)
    kind = kind if kind in ("line", "stop", "both") else "both"
    with conn() as c:
        if kind in ("line", "both"):
            c.execute("UPDATE ld_alias SET ok=0 WHERE line_id=?", (lid,))
            c.execute("UPDATE ld_line SET src_line='', line_ok=0 WHERE id=?", (lid,))
        if kind in ("stop", "both"):
            c.execute("UPDATE ld_line SET src_stop='', stop_ok=0 WHERE id=?", (lid,))
    log("src-clear", str(lid), kind)
    return line_detail(lid) or {}


def _line_src_pick(d: dict) -> Tuple[str, str]:
    """① 里选中的那一条线路来源（单选；老数据多选时取第一条）"""
    for part in str(d.get("src_line") or "").split("|"):
        if ":" not in part:
            continue
        s, n = part.split(":", 1)
        if s.strip() and n.strip():
            return s.strip(), n.strip()
    return "", ""


def _stop_src_pick(d: dict) -> Tuple[str, str]:
    sp = str(d.get("src_stop") or "").split(":", 1)
    return (sp[0].strip(), sp[1].strip() if len(sp) > 1 else "")


def finalize(lid: int, name: str = "") -> dict:
    """线路来源（走向）+ 站点来源（站点和定位）**合成一条确认线路**

    - 走向：取 ① 里选中的那一家。rg / hdx / cnrail 把它的**原始多段**搬进 ld_geom
      （真实线形，不抽稀、不用补断口的主轴），并在 ld_line 记 `geom_src=route:xxx`，
      之后地图 / 定为基本线路 / 主站都跟着这一份走。osm 只记 `osm`。
      底表 / 资料库 / china-emu / 自建只有站序、没有走向，保留已有走向并提示。
    - 站点：② 确认时已把那一家的站名和定位落到 ld_stop，这里只补仍然缺的定位。
    """
    init()
    lid = int(lid)
    d = line_detail(lid)
    if not d:
        raise ValueError("没有这条线路")
    if not (d.get("line_ok") and d.get("stop_ok")):
        raise ValueError("线路来源和站点来源都要先分别确认")
    # 「全部确认」= 两边来源都 ✓ + 有停站（含定位）+ 有线路路线位置（真实走向）。
    # 主站「车站/线路」页只显示这样的线（2026-11-06 用户要求），所以确认这一步
    # 就把住关，别让用户确认完却在主站找不到，或者确认出一条只有名字的线。
    if len(d.get("stops") or []) < 2:
        raise ValueError("至少要 2 个停站才能确认（现在 %d 个）；先在②选好站点来源"
                         % len(d.get("stops") or []))

    lsrc, lname = _line_src_pick(d)
    ssrc, sname = _stop_src_pick(d)
    final_name = (name or d["name"]).strip() or d["name"]
    if final_name != d["name"]:
        with conn() as c:
            dup = c.execute("SELECT id FROM ld_line WHERE name=? AND id<>?",
                            (final_name, lid)).fetchone()
            if dup:
                raise ValueError(f"已经有叫「{final_name}」的线路了")

    # ③ 现在一步到位「合并保存 + 定为基本线路」（2026-11-06 用户取消 ④），
    # 规范名必须是正名。原来这个拦截只写在 ④ 的前端，脚本 / 老页面能绕过，
    # 挪到后端一起把住，免得确认完却定不成基本线路。
    import re as _re
    clean = _re.sub(r"[（(](?:底表|OSM|自建|china-emu|GeoJSON|备份|线路资料库)"
                    r"[^）)]*[）)]", "", final_name, flags=_re.I)
    clean = _re.sub(r"#\d+$", "", clean).strip()
    if clean and clean != final_name:
        raise ValueError("规范名「%s」还带来源后缀，先在基本信息里改成正名「%s」再合并保存"
                         % (final_name, clean))

    warn: List[str] = []
    geom: Optional[dict] = None
    # ① 已经用「导入走向（GeoJSON）」存过一份时，③ 不能再按所选来源重搬一次盖掉它：
    # ① 只管走向、② 只管站点，③ 只把它们合起来。
    already_geojson = ((d.get("geom_src") or "").strip() == "geojson"
                       and bool(d.get("deskGeom")) and not lsrc)
    if already_geojson:
        geom = {"src": "geojson", "label": "GeoJSON（① 导入）",
                "name": (d.get("geom_name") or final_name),
                "km": None, "nPart": 0, "nPt": 0}
    elif lsrc in ROUTE_SRC:
        g = _route_raw(lsrc, lname)
        if not g or not g["parts"]:
            warn.append(f"「{SRC_LABEL.get(lsrc, lsrc)}」里读不到「{lname}」的走向")
        else:
            label = SRC_LABEL.get(lsrc, lsrc)
            stat = (f"合并：{label} ·「{lname}」的走向 + "
                    f"{('「' + sname + '」') if sname else '本线'}的站点定位")
            if g.get("note"):
                stat += f"（{g['note']}）"
            _save_geom(final_name, lid, f"{label} · {lname}", g["parts"], g["chain"],
                       g["km"], {"merge": "line+stop", "geomSrc": f"route:{lsrc}",
                                 "geomName": lname, "source": stat,
                                 "nPart": g["nPart"], "nPt": g["nPt"]},
                       raw_parts=g["parts"])
            geom = {"src": f"route:{lsrc}", "label": label, "name": lname,
                    "km": g["km"], "nPart": g["nPart"], "nPt": g["nPt"]}
    elif lsrc == "osm":
        geom = {"src": "osm", "label": "OSM", "name": lname or final_name,
                "km": None, "nPart": 0, "nPt": 0}
    elif lsrc:
        # 底表 / 自建 / 资料库 / china-emu：线路库里按线名存了走向就一起搬过来
        g = _linegeom_raw(lname)
        if g and g["parts"]:
            label = SRC_LABEL.get(lsrc, lsrc)
            _save_geom(final_name, lid, g["file"] or f"{label} · {lname}",
                       g["parts"], g["chain"], g["km"],
                       {"merge": "line+stop", "geomSrc": "geojson",
                        "geomName": lname,
                        "source": f"合并：{label} ·「{lname}」的走向 + "
                                  f"{('「' + sname + '」') if sname else '本线'}的站点定位",
                        "nPart": g["nPart"], "nPt": g["nPt"]},
                       raw_parts=g["parts"])
            geom = {"src": "geojson", "label": label, "name": lname,
                    "km": g["km"], "nPart": g["nPart"], "nPt": g["nPt"]}
        elif d.get("deskGeom"):
            warn.append(f"「{SRC_LABEL.get(lsrc, lsrc)}」这条线没有走向，沿用已经导入的那份")
        else:
            warn.append(f"「{SRC_LABEL.get(lsrc, lsrc)}」这条线没有走向，"
                        "这条线暂时只能按站点直连示意")

    # 站点定位：还缺的按②那家的站序位置补，再退一步查站名档案
    src_latlon: Dict[str, Tuple] = {}
    if ssrc and sname:
        try:
            for s in (source_stops(ssrc, sname).get("stops") or []):
                nm = _norm_stop_name(s.get("name") or "")
                if nm and s.get("lat") is not None and s.get("lon") is not None:
                    src_latlon[nm] = (s.get("lat"), s.get("lon"))
        except Exception:                          # noqa: BLE001
            src_latlon = {}
    co = _coords()
    fixed = 0
    with conn() as c:
        for s in d["stops"]:
            if s.get("lat") is not None and s.get("lon") is not None:
                continue
            ll = src_latlon.get(_norm_stop_name(s["name"])) or co.get(s["name"])
            if not ll or ll[0] is None:
                continue
            c.execute("UPDATE ld_stop SET lat=?,lon=? WHERE line_id=? AND seq=?",
                      (ll[0], ll[1], lid, s["seq"]))
            fixed += 1

    # 走向与定位：确认线路必须自带「线路路线位置」，否则主站画不出真实走向
    with conn() as c:
        has_geom = bool(c.execute(
            "SELECT 1 FROM ld_geom WHERE line_id=?"
            " AND (parts_gz IS NOT NULL OR spine_gz IS NOT NULL) LIMIT 1",
            (lid,)).fetchone())
        n_coord = c.execute(
            "SELECT COUNT(*) FROM ld_stop WHERE line_id=?"
            " AND lat IS NOT NULL AND lon IS NOT NULL", (lid,)).fetchone()[0]
    # ① 选了「OSM 线」但没落地走向时，得真的能按线名解析出几何才算数，
    #   否则确认完主站会画出一条只有站点直连的线（用户以为整合好了其实没有）
    geom_ok = bool(has_geom or d.get("deskGeom"))
    if not geom_ok and geom and geom.get("src") not in ("osm",):
        geom_ok = True                       # route:xxx / geojson 已经写进 ld_geom
    if not geom_ok and geom and geom.get("src") == "osm":
        gname = geom.get("name") or final_name
        try:
            import geoimport
            import railgeom
            geom_ok = bool(geoimport.meta(gname)
                           or railgeom.meta(gname) or railgeom.meta(final_name))
        except Exception:                    # noqa: BLE001
            geom_ok = False
    if not geom_ok:
        raise ValueError("还没有线路路线位置（走向）：先在①选一个带走向的线路来源，"
                         "或用「导入 GeoJSON」把走向导进整理台")
    if n_coord < 2:
        raise ValueError("停站缺定位：至少要有 2 个站带经纬度（现在 %d 个），"
                         "先在②重新确认站点来源或补坐标" % n_coord)

    with conn() as c:
        sets = ["name=?", "family=?", "status='confirmed'", "updated_at=?"]
        args: List = [final_name, family_of(final_name), _now()]
        if geom:
            sets += ["geom_src=?", "geom_name=?"]
            args += [geom["src"], geom["name"] or ""]
            if d.get("total_km") is None and geom.get("km"):
                sets.append("total_km=?")
                args.append(geom["km"])
        args.append(lid)
        c.execute(f"UPDATE ld_line SET {', '.join(sets)} WHERE id=?", args)

    out = line_detail(lid) or {}
    out["merged"] = {
        "name": final_name, "geom": geom, "warn": warn,
        "stops": len(out.get("stops") or []),
        "coordN": out.get("coordDone") or 0, "filled": fixed,
        "lineSrc": f"{lsrc}:{lname}" if lsrc else "",
        "stopSrc": f"{ssrc}:{sname}" if ssrc else "",
        # 确认成果必须同时含停站与线路走向，面板据此标「完整 / 还差什么」
        "complete": True, "nCoord": n_coord, "hasGeom": geom_ok,
    }
    log("finalize", final_name,
        "合并：走向 %s %s / 站点 %s（补定位 %d）"
        % (geom["src"] if geom else "-", (geom or {}).get("name") or "-",
           ssrc or "-", fixed))
    return out


# ---------------------------------------------------------------------------
# GeoJSON 导入（整理台自己存走向，不写 rail.db）
# ---------------------------------------------------------------------------

def _save_geom(name: str, lid: int, filename: str, parts: List[list],
               chain: Sequence[Tuple[float, float]], km: float, props: dict,
               raw_parts: Optional[list] = None) -> None:
    """存一份走向：`spine_gz` = 串好的连续主轴，`parts_gz` = 原始多段（画真线形用）

    导入 GeoJSON 时 raw_parts 传文件里的段；「合并保存」搬来源包走向时也传它的原始段，
    地图就不会只看到主轴（主轴缺口是直线补齐的，长线会失真）。
    """
    import gzip
    # 两种来源的 chain 点序不同，统一成 [lon,lat] 再落库（见 _norm_chain）
    line = _norm_chain(chain)
    blob = (gzip.compress(json.dumps(line, separators=(",", ":")).encode("utf-8"), 6)
            if line else None)
    raw = _norm_parts(raw_parts)
    rblob = (gzip.compress(json.dumps(raw, separators=(",", ":")).encode("utf-8"), 6)
             if raw else None)
    with conn() as c:
        c.execute(
            "INSERT INTO ld_geom(name,line_id,filename,total_km,n_part,n_pt,props,"
            "spine_gz,parts_gz,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(name) DO UPDATE SET line_id=excluded.line_id,"
            " filename=excluded.filename, total_km=excluded.total_km,"
            " n_part=excluded.n_part, n_pt=excluded.n_pt, props=excluded.props,"
            " spine_gz=excluded.spine_gz, parts_gz=excluded.parts_gz,"
            " created_at=excluded.created_at",
            (name, lid, filename, km, len(raw) or len(parts),
             sum(len(p) for p in raw) or sum(len(p) for p in (parts or [])),
             json.dumps(props or {}, ensure_ascii=False), blob, rblob, _now()))


def desk_geom(name: str = "", lid: int = 0) -> Optional[dict]:
    """整理台自己存的走向（导入的 GeoJSON）"""
    init()
    with conn() as c:
        r = None
        if lid:
            r = c.execute("SELECT * FROM ld_geom WHERE line_id=? ORDER BY created_at DESC"
                          " LIMIT 1", (int(lid),)).fetchone()
        if not r and name:
            r = c.execute("SELECT * FROM ld_geom WHERE name=?", (name,)).fetchone()
    if not r:
        return None
    parts = []
    # 原始多段优先（合并保存搬过来的来源包走向 / 导入的 GeoJSON），
    # 老数据只有 spine 时才退回单条主轴
    try:
        parts = _gz_paths(r["parts_gz"]) if "parts_gz" in r.keys() else []
    except sqlite3.DatabaseError:
        parts = []
    if not parts and r["spine_gz"]:
        import gzip
        try:
            chain = json.loads(gzip.decompress(r["spine_gz"]).decode("utf-8"))
            parts = _norm_parts(chain, single=True)   # 兼容历史反序 spine
        except Exception:                          # noqa: BLE001
            parts = []
    try:
        props = json.loads(r["props"] or "{}")
    except (TypeError, ValueError):
        props = {}
    # 这份走向是怎么来的：导入的 GeoJSON / 合并时搬的来源包走向
    gs = str(props.get("geomSrc") or "")
    src = "route" if gs.startswith("route:") else ("osm" if gs == "osm" else "geojson")
    return {"name": r["name"], "file": r["filename"], "km": r["total_km"],
            "nPart": r["n_part"], "nPt": r["n_pt"], "props": props,
            "parts": parts, "src": src, "geomSrc": gs,
            "routeSrc": gs.split(":", 1)[1] if gs.startswith("route:") else "",
            "label": (props.get("source") or ""), 
            "ask": r["filename"] or r["name"]}


def import_geojson(lid: int, obj, filename: str = "", name: str = "",
                   only: str = "") -> dict:
    """把一份 GeoJSON 导进整理台的某条线

    站名/站序/里程**全部以文件为准**（和主程序导入同一套算法：端点建图 → 车站投影
    切段 → PCA 主轴 + 局部搜索 → Dijkstra 累里程）。

    2026-11-06 用户口径 —— ① 只存走向、② 只存站点，两边不许互相覆盖：
      * `only='stops'`（②「导入站点」）：只写 `ld_stop` + `src_stop`，**不写 ld_geom**；
      * `only='geom'` （①「导入走向」）：只写 `ld_geom` + `geom_src`，**不写 ld_stop**；
      * `only=''`（顶栏「导入 GeoJSON」建新线）：两边都写，保持老行为。
    站点来源记为 `geojson:<文件名>`，但**要人工点「确认站点来源」**才算定稿。
    """
    import geoimport
    init()
    only = (only or "").strip().lower()
    if only not in ("", "stops", "geom"):
        raise ValueError("only 只能是 stops / geom / 空")
    lid = int(lid)
    d = line_detail(lid)
    if not d:
        raise ValueError("没有这条线路")
    p = geoimport.parse(obj, filename)
    if not p.get("parts"):
        raise ValueError("文件里没有线状几何（LineString / MultiLineString）")
    if len(p.get("stations") or []) < 2:
        raise ValueError("车站点少于 2 个（properties.kind=station 的 Point）")
    props = p.get("props") or {}
    seq, chain, stat = geoimport.assemble(p["parts"], p["stations"],
                                          str(props.get("起点") or ""),
                                          str(props.get("终点") or ""))
    if len(seq) < 2:
        raise ValueError("能定位到走向上的车站少于 2 个")
    if not chain:
        raise ValueError("走向串不成一条折线（碎段之间离得太远）")

    stops = [{"name": s["name"], "km": s["km"], "lat": s["lat"], "lon": s["lon"],
              "src": "geojson"} for s in seq]
    gname = name or p.get("name") or d["name"]
    if only != "geom":
        # ② 站点来源：只搬站点 + 定位，走向一个字节都不动
        save_stops(lid, stops)
        with conn() as c:
            c.execute("UPDATE ld_line SET src_stop=?, stop_ok=0, updated_at=? WHERE id=?",
                      (f"geojson:{p.get('file') or filename}", _now(), lid))
    if only != "stops":
        # ① 线路来源：只存这份走向，站序一个字节都不动
        _save_geom(gname, lid, p.get("file") or filename, p["parts"], chain,
                   stat.get("geomKm") or stat["totalKm"], props,
                   raw_parts=p["parts"])
        if only == "geom":
            with conn() as c:
                # ① 的两个入口互斥：直接导入走向就算完成了 ①，清掉之前选的来源
                c.execute("UPDATE ld_line SET geom_src=?, geom_name=?,"
                          " src_line='', line_ok=1, updated_at=? WHERE id=?",
                          ("geojson", gname, _now(), lid))
    log("geojson", str(lid), f"{p.get('file') or filename} {len(stops)} 站 only={only or 'all'}")
    out = {"ok": True, "n": len(stops), "totalKm": stops[-1]["km"],
           "geomKm": stat.get("geomKm"), "kmFrom": stat.get("kmFrom"),
           "first": stops[0]["name"], "last": stops[-1]["name"],
           "parts": len(p["parts"]), "chainPoints": len(chain),
           "broken": stat.get("broken") or 0,
           "stationsInFile": len(p["stations"]),
           "dropped": len(p["stations"]) - len(stops),
           "file": p.get("file") or filename,
           "only": only,
           "line": line_detail(lid)}
    return out


def set_relation(parent: int, child: int, kind: str = "seg", note: str = "") -> dict:
    init()
    parent, child = int(parent), int(child)
    if parent == child:
        raise ValueError("不能自己当自己的父线")
    with conn() as c:
        for i in (parent, child):
            if not c.execute("SELECT 1 FROM ld_line WHERE id=?", (i,)).fetchone():
                raise ValueError("线路不存在")
        # 防成环：child 的祖先里不能有 parent
        seen, stack = set(), [parent]
        while stack:
            cur = stack.pop()
            if cur == child:
                raise ValueError("会成环，换个父线")
            if cur in seen:
                continue
            seen.add(cur)
            stack += [r[0] for r in c.execute(
                "SELECT parent_id FROM ld_relation WHERE child_id=?", (cur,))]
        c.execute("INSERT OR REPLACE INTO ld_relation(parent_id,child_id,kind,note)"
                  " VALUES(?,?,?,?)", (parent, child, kind or "seg", note))
    log("rel+", f"{parent}->{child}", kind)
    return line_detail(parent) or {}


def del_relation(parent: int, child: int) -> dict:
    init()
    with conn() as c:
        c.execute("DELETE FROM ld_relation WHERE parent_id=? AND child_id=?",
                  (int(parent), int(child)))
    log("rel-", f"{parent}->{child}")
    return line_detail(int(parent)) or {}


# ---------------------------------------------------------------------------
# 同族未归属的写法（「这条线还有别的叫法没收进来」）
# ---------------------------------------------------------------------------

_SRC_CACHE: Dict[str, object] = {"at": 0.0, "items": []}
_SRC_TTL = 300.0


def _sources_cached() -> List[dict]:
    now = time.time()
    if now - float(_SRC_CACHE["at"] or 0) > _SRC_TTL or not _SRC_CACHE["items"]:
        _SRC_CACHE["items"] = collect_sources()
        _SRC_CACHE["at"] = now
    return _SRC_CACHE["items"]                        # type: ignore[return-value]


def clear_cache() -> None:
    _SRC_CACHE["at"] = 0.0
    _SRC_CACHE["items"] = []
    _ROUTE_CACHE["at"] = 0.0
    _ROUTE_CACHE["items"] = []


def family_candidates(lid: int, limit: int = 60) -> List[dict]:
    """同族但还没被任何线收下的来源写法"""
    init()
    with conn() as c:
        row = c.execute("SELECT name,family FROM ld_line WHERE id=?", (lid,)).fetchone()
        if not row:
            return []
        fam = row["family"] or family_of(row["name"])
        taken = {(r["source"], r["name"]) for r in
                 c.execute("SELECT source,name FROM ld_alias")}
        own = {r["name"] for r in c.execute(
            "SELECT name FROM ld_alias WHERE line_id=?", (lid,))}
    out = []
    for it in _sources_cached():
        if (it["source"], it["name"]) in taken:
            continue
        if family_of(it["name"]) != fam:
            continue
        out.append({"source": it["source"], "name": it["name"],
                    "n_stop": len(it["stops"]), "totalKm": it["total_km"],
                    "sameName": it["name"] in own})
    out.sort(key=lambda x: (sort_key(x["source"], x["n_stop"])))
    return out[:limit]


# ---------------------------------------------------------------------------
# 几何 / 导出
# ---------------------------------------------------------------------------

def geom(lid: int) -> dict:
    """这条线在地图上的走向：先看整理台自己导入的 GeoJSON，再看是不是基本线路
    指定了「某个来源包自己的走向」，最后拿 OSM 别名去取几何，取不到就只给站间直连"""
    d = line_detail(lid)
    if not d:
        raise ValueError("没有这条线路")
    g = desk_geom(d["name"], lid)          # 导入的 GeoJSON 走向优先（人工核对过的）
    if not g:
        try:
            import linebase
            rf = linebase.route_of(d["name"])
            if rf:
                g = linebase.route_geom(rf[1], rf[0])
        except Exception:                          # noqa: BLE001
            g = None
    if not g:
        import railgeom
        names = [a["name"] for a in d["aliases"] if a["source"] == "osm"]
        names += [d["name"]]
        for nm in names:
            try:
                g = railgeom.geom(nm, expect_km=d.get("total_km")) if railgeom.ready() else None
            except Exception:                          # noqa: BLE001
                g = None
            if g:
                g["ask"] = nm
                break
    return {"line": {"id": d["id"], "name": d["name"], "totalKm": d["total_km"]},
            "stops": [{"name": s["name"], "km": s["km"], "lat": s["lat"], "lon": s["lon"]}
                      for s in d["stops"]],
            "geom": g}


def export_map() -> dict:
    """整理成果导出：规范线名 ← 各来源写法，外加父子关系与站序

    将来要把整理结果回灌 rail.db 时，照这个结构生成 alias 映射即可。
    """
    init()
    with conn() as c:
        lines = []
        for r in c.execute("SELECT * FROM ld_line ORDER BY id"):
            lid = r["id"]
            al = {}
            for a in c.execute("SELECT source,name FROM ld_alias WHERE line_id=? ORDER BY source",
                               (lid,)):
                al.setdefault(a["source"], []).append(a["name"])
            stops = [{"name": s["name"], "km": s["km"], "lon": s["lon"], "lat": s["lat"]}
                     for s in c.execute("SELECT name,km,lon,lat FROM ld_stop"
                                        " WHERE line_id=? ORDER BY seq", (lid,))]
            lines.append({"id": lid, "name": r["name"], "kind": r["kind"],
                          "status": r["status"], "totalKm": r["total_km"],
                          "from": r["from_sta"], "to": r["to_sta"],
                          "family": r["family"], "alias": al, "stops": stops})
        rel = [{"parent": r["parent_id"], "child": r["child_id"],
                "kind": r["kind"], "note": r["note"]}
               for r in c.execute("SELECT * FROM ld_relation")]
    return {"exportedAt": _now(),
            "counts": {"lines": len(lines), "relations": len(rel)},
            "lines": lines, "relations": rel}


def recent_log(limit: int = 60) -> List[dict]:
    init()
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT ts,op,target,detail FROM ld_log ORDER BY id DESC LIMIT ?", (limit,))]
