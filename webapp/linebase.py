# -*- coding: utf-8 -*-
"""linebase.py —— 「基本线路」：整理台定稿、全项目生效的那一条

流程（2026-11-03 与用户确认）：

    认亲（收来源别名） → 合并完善（调站序） → 匹配 OSM 走向 → 定稿
    → **这里落库**，成为「基本线路」

生效范围：**全面接管** —— 数据校对的线路选择、地图画走向、选经由、车次地图
都用这一份。备份导回来的那 2647 条**原样保留**，只是取数时让位；版本条里仍
能点开比对。

为什么不塞进 `cnrail._LINES`：基本线路的规范名（京沪线）常和用户 GeoJSON 导的
那条同名，塞进去会把人家覆盖掉；而且备份那批线要能随时切回比对。所以接管点
放在**取数出口**（linegroup / server 的 /api/line、/api/line/stops、
/api/curate/lineNames），底层五套来源一个字节不动，想撤销就删这张表的行。
"""
from __future__ import annotations

import sqlite3
import time
from typing import Dict, List, Optional, Tuple

import railcore

_CACHE: Optional[Dict[str, dict]] = None
_CACHE_SIG: Optional[tuple] = None
_SIG_TTL = 2.0
_SIG_AT = 0.0
_SIG_VAL: Optional[tuple] = None


def _ensure(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS line_base (
               id         INTEGER PRIMARY KEY AUTOINCREMENT,
               name       TEXT NOT NULL UNIQUE,  -- 规范线名（全项目唯一）
               ld_id      INTEGER,               -- 来自整理台哪条 ld_line
               total_km   REAL,
               from_sta   TEXT, to_sta TEXT,
               geom_src   TEXT DEFAULT '',       -- osm / geojson / manual / none
               geom_name  TEXT DEFAULT '',       -- OSM 线名 / GeoJSON 文件名
               covers     TEXT DEFAULT '',       -- 整合了哪些来源版本（逗号分隔，仅展示）
               note       TEXT DEFAULT '',
               created_at REAL, updated_at REAL
           )""")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS line_base_stop (
               base_id INTEGER NOT NULL,
               seq     INTEGER NOT NULL,
               name    TEXT NOT NULL,
               km      REAL, lon REAL, lat REAL,
               PRIMARY KEY (base_id, seq)
           )""")


def _sig() -> Optional[tuple]:
    """指纹变了就重建缓存。和 linegroup 一个思路：各写入口不一定记得通知。"""
    global _SIG_AT, _SIG_VAL
    now = time.time()
    if _SIG_VAL is not None and now - _SIG_AT < _SIG_TTL:
        return _SIG_VAL
    try:
        conn = railcore.db()
        try:
            _ensure(conn)
            r = conn.execute(
                "SELECT COUNT(*) AS c, MAX(updated_at) AS u FROM line_base").fetchone()
            _SIG_VAL = (r["c"], r["u"])
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        _SIG_VAL = None
    _SIG_AT = now
    return _SIG_VAL


def invalidate() -> None:
    global _CACHE, _CACHE_SIG, _SIG_VAL, _SIG_AT
    _CACHE = _CACHE_SIG = None
    _SIG_VAL, _SIG_AT = None, 0.0


def _load() -> Dict[str, dict]:
    global _CACHE, _CACHE_SIG
    sig = _sig()
    if _CACHE is not None and _CACHE_SIG == sig:
        return _CACHE
    out: Dict[str, dict] = {}
    try:
        conn = railcore.db()
        try:
            _ensure(conn)
            for r in conn.execute("SELECT * FROM line_base"):
                row = dict(r)
                row["stops"] = [
                    {"seq": s["seq"], "name": s["name"], "km": s["km"],
                     "lon": s["lon"], "lat": s["lat"]}
                    for s in conn.execute(
                        "SELECT * FROM line_base_stop WHERE base_id=? ORDER BY seq",
                        (row["id"],))]
                out[row["name"]] = row
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        out = {}
    _CACHE, _CACHE_SIG = out, sig
    return out


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------
def names() -> set:
    return set(_load().keys())


def get(name: str) -> Optional[dict]:
    """按规范线名取；带「（来源）」后缀的不是基本线路，返回 None"""
    if not name:
        return None
    from linegroup import split as _split
    if _split(str(name))[1]:
        return None                       # 点名要看某个来源版本，不接管
    return _load().get(str(name).strip())


def has(name: str) -> bool:
    return get(name) is not None


def get_by_ld(ld_id: int) -> Optional[dict]:
    try:
        lid = int(ld_id or 0)
    except (TypeError, ValueError):
        return None
    for r in _load().values():
        if r.get("ld_id") == lid:
            return r
    return None


def stop_names(name: str) -> Optional[List[str]]:
    b = get(name)
    if not b:
        return None
    return [railcore.canon_station(s["name"]) for s in b["stops"] if s.get("name")]


def list_all() -> List[dict]:
    out = []
    for r in _load().values():
        out.append({k: r[k] for k in
                    ("id", "name", "ld_id", "total_km", "from_sta", "to_sta",
                     "geom_src", "geom_name", "covers", "note", "updated_at")
                    } | {"nStop": len(r["stops"])})
    out.sort(key=lambda x: x["name"])
    return out


# ---------------------------------------------------------------------------
# 定稿 / 撤销
# ---------------------------------------------------------------------------
def publish(payload: dict) -> dict:
    """把整理台的成果定为基本线路

    payload: {name, ldId, totalKm, from, to, geomSrc, geomName, covers, note,
              stops: [{name, km, lon, lat}]}
    """
    name = str((payload or {}).get("name") or "").strip()
    if not name:
        raise ValueError("要给基本线路起个规范线名")
    from linegroup import split as _split
    if _split(name)[1]:
        raise ValueError("规范线名不能带「（来源）」后缀")
    stops = [s for s in (payload.get("stops") or []) if (s.get("name") or "").strip()]
    if len(stops) < 2:
        raise ValueError("至少要有两个车站")
    now = time.time()
    try:
        ld_id = int(payload.get("ldId") or 0) or None
    except (TypeError, ValueError):
        ld_id = None
    with railcore._db_lock, railcore.db() as conn:
        _ensure(conn)
        # 规范名唯一：占用着别的基本线路的名字要报错，让用户先改名或先撤销
        dup = conn.execute("SELECT id FROM line_base WHERE name=? AND (? IS NULL OR id<>?)",
                           (name, payload.get("replaceId"), payload.get("replaceId"))).fetchone()
        if dup and not payload.get("replaceId"):
            raise ValueError("已经有一条叫「%s」的基本线路了，先改名或先撤销" % name)
        if payload.get("replaceId"):
            bid = int(payload["replaceId"])
            conn.execute("DELETE FROM line_base_stop WHERE base_id=?", (bid,))
            conn.execute(
                "UPDATE line_base SET name=?, ld_id=?, total_km=?, from_sta=?, to_sta=?,"
                " geom_src=?, geom_name=?, covers=?, note=?, updated_at=? WHERE id=?",
                (name, ld_id, payload.get("totalKm"), payload.get("from"),
                 payload.get("to"), payload.get("geomSrc") or "",
                 payload.get("geomName") or "",
                 ",".join(payload.get("covers") or []),
                 payload.get("note") or "", now, bid))
        else:
            cur = conn.execute(
                "INSERT INTO line_base(name, ld_id, total_km, from_sta, to_sta,"
                " geom_src, geom_name, covers, note, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (name, ld_id, payload.get("totalKm"), payload.get("from"),
                 payload.get("to"), payload.get("geomSrc") or "",
                 payload.get("geomName") or "",
                 ",".join(payload.get("covers") or []),
                 payload.get("note") or "", now, now))
            bid = cur.lastrowid
        for i, s in enumerate(stops, 1):
            conn.execute(
                "INSERT INTO line_base_stop(base_id, seq, name, km, lon, lat)"
                " VALUES(?,?,?,?,?,?)",
                (bid, i, str(s["name"]).strip(), s.get("km"), s.get("lon"), s.get("lat")))
    invalidate()
    return get(name) or {}


def unpublish(name: str) -> bool:
    """撤销基本线路，退回「默认版本 > 底表 > 站数最多」"""
    name = str(name or "").strip()
    if not name:
        raise ValueError("要撤销哪条")
    with railcore._db_lock, railcore.db() as conn:
        _ensure(conn)
        row = conn.execute("SELECT id FROM line_base WHERE name=?", (name,)).fetchone()
        if not row:
            return False
        conn.execute("DELETE FROM line_base_stop WHERE base_id=?", (row["id"],))
        conn.execute("DELETE FROM line_base WHERE id=?", (row["id"],))
    invalidate()
    return True


# ---------------------------------------------------------------------------
# 给 /api/line 用的返回结构
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 走向：基本线路可以指定「某个来源包自己的走向」（rail.db / line_route）
# ---------------------------------------------------------------------------
# 2026-11-05 用户要求：整理台定稿时，除了「OSM 线 / GeoJSON / 站间直连」，
# 还要能直接用某一套走向数据（rail-globe / OSM / cnrail）自己的几何 ——
# 这些包存在 line_route(name, source) 里，跟 line_geom 那条「合并走向」不是一回事。

_ROUTE_LABEL = {"rg": "rail-globe 走向", "hdx": "OSM 走向", "cnrail": "cnrail 走向"}


def _route_paths(blob) -> List[list]:
    """line_route 的 parts_gz / spine_gz（gzip JSON）→ [[[lon,lat],...], ...]"""
    if not blob:
        return []
    try:
        import gzip
        import json
        data = json.loads(gzip.decompress(blob).decode("utf-8"))
    except (OSError, ValueError, TypeError, EOFError):
        return []
    if not isinstance(data, list) or not data:
        return []
    if isinstance(data[0], list) and data[0] and isinstance(data[0][0], (int, float)):
        data = [data]                      # 单条主路径 → 包成一段
    return [p for p in data if isinstance(p, list) and len(p) >= 2
            and isinstance(p[0], list) and len(p[0]) >= 2]


def route_of(name: str) -> Optional[Tuple[str, str]]:
    """基本线路配了「来源包走向」时返回 (来源 key, 来源里的线名)，否则 None

    `geom_src` 存成 `route:hdx` 这种形式，`geom_name` 是来源包里的线名
    （通常就是规范名，但同一条线各来源写法可能不同，所以不硬按规范名取）。
    """
    name = str(name or "").strip()
    if not name:
        return None
    b = get(name)
    if not b:
        return None
    src = str(b.get("geom_src") or "")
    if not src.startswith("route:"):
        return None
    key = src.split(":", 1)[1].strip()
    if not key:
        return None
    return key, (str(b.get("geom_name") or "").strip() or name)


def route_geom(name: str, source: str, with_parts: bool = True) -> Optional[dict]:
    """某个来源包自己的走向，字段形状对齐 `railgeom.geom()`，前端不用改"""
    name = str(name or "").strip()
    source = str(source or "").strip()
    if not name or not source:
        return None
    try:
        with railcore._db_lock, railcore.db() as conn:
            row = conn.execute(
                "SELECT src_id,total_km,n_part,n_pt,parts_gz,spine_gz FROM line_route"
                " WHERE name=? AND source=?", (name, source)).fetchone()
    except sqlite3.DatabaseError:          # 旧库还没这张表
        return None
    if not row:
        return None
    parts = _route_paths(row["parts_gz"]) or _route_paths(row["spine_gz"])
    n_pt = sum(len(p) for p in parts)
    return {
        "name": name, "ask": name, "match": "route",
        "source": "route", "routeSrc": source,
        "routeLabel": _ROUTE_LABEL.get(source, source),
        "km": round(float(row["total_km"] or 0), 1),
        "nPart": row["n_part"] or len(parts), "nPt": n_pt,
        "file": row["src_id"] or "",
        "parts": parts if with_parts else [], "crs": "WGS84",
    }


def detail_payload(name: str) -> Optional[dict]:
    """把基本线路整理成 /api/line 认识的样子（stations / totalKm / …）"""
    b = get(name)
    if not b:
        return None
    # kmBase 是定稿那天的档案值：编辑里程时前端拿它当重算基准、按段距/累计重算，
    # 一定要给（档案线路的 cnrail.line_detail 也一样给），否则填一个值整条线就从头算起。
    stations = [{"name": railcore.canon_station(s["name"]), "km": s["km"],
                 "kmBase": s["km"], "lon": s["lon"], "lat": s["lat"],
                 "order": s["seq"]} for s in b["stops"]]
    # 坐标以「当前生效值」为准：人工补录（coord_override）优先于定稿时写进
    # line_base_stop 的快照 —— 否则在车站页改了坐标，基本线路的地图/详情还是旧位置
    # （2026-11-05 用户报「杭州东坐标保存不下来」，站点表里一直是定稿那天的旧值）
    for s in stations:
        try:
            ci = railcore.stations.coord_info(s["name"]) or {}
        except Exception:            # 车站表还没加载好时别把详情带崩
            ci = {}
        if ci.get("lat") is not None and ci.get("lon") is not None:
            s["lat"], s["lon"] = ci["lat"], ci["lon"]
            if ci.get("source") == "manual":
                s["coordSrc"] = "manual"
    # 人工里程校正（「编辑里程」存的 line_override）：基本线路也要认。
    # 不认的话，在这条线上改完里程一保存，详情又按 line_base_stop 的定稿快照显示，
    # 看着就像「保存不正确」（2026-11-05 用户报的就是这个）。
    ov = (railcore.line_override_map() or {}).get(b["name"]) or {}
    total_km = b["total_km"]
    if ov.get("segKm") or ov.get("staKm") or ov.get("totalKm"):
        import cnrail as _cnrail
        cum, _seg = _cnrail._rebuild_km(
            [{"km": s.get("km")} for s in b["stops"]],
            ov.get("segKm"), ov.get("totalKm"), ov.get("staKm"))
        for s, c in zip(stations, cum):
            s["km"] = c
        # 总里程口径与 cnrail._rebuild_km 的应用方式一致：末站有累计就用末站，
        # 末站缺里程（沪昆线后面 82 站底表就没给）时才用人工填的总里程 / 定稿总里程。
        # 别拿「最后一个非空累计」当总里程 —— 缺里程的线会把总里程抬到半路那站的数。
        total_km = cum[-1] if (cum and cum[-1] is not None) else (
            ov.get("totalKm") or b["total_km"])
    seg_out = []
    for i in range(len(stations) - 1):
        a, c = stations[i].get("km"), stations[i + 1].get("km")
        seg_out.append(round(float(c) - float(a), 1)
                       if (a is not None and c is not None) else None)
    n_coord = sum(1 for s in stations if s.get("lat") is not None)
    return {
        "name": b["name"],
        "found": True,
        "from": "base",
        "base": True,
        "baseId": b["id"],
        "ldId": b.get("ld_id"),          # 整理台那条线的 id，主站用它取合并好的真实走向
        "stations": stations,
        "n": len(stations),
        "totalKm": total_km,
        "override": {"segKm": ov.get("segKm") or {}, "staKm": ov.get("staKm") or {},
                     "stops": [], "totalKm": ov.get("totalKm"),
                     "note": ov.get("note") or ""} if ov else None,
        "fromSta": b["from_sta"],
        "toSta": b["to_sta"],
        "geomSrc": b["geom_src"],
        "geomName": b["geom_name"],
        "covers": [c for c in (b["covers"] or "").split(",") if c],
        "note": b["note"] or "",
        "coordCoverage": round(100.0 * n_coord / len(stations)) if stations else 0,
        "segKm": seg_out,
    }
