# -*- coding: utf-8 -*-
"""备份里的线路：列出来看、挑一条再导回线路库

备份由 `scripts/reset_lines.py` 生成，目录 `data/backup/lines-reset-<tag>/`：

  lines_export.json  逐表行级导出（首选：自建 / 底表 / OSM / china-emu / 资料库 全在里面）
  rail.db            整库副本（批次没有 export 时从这儿读）
  cnrail_lines.json  底表副本

这里只干两件事：**看**（`lines()` / `detail()`）和 **挑**（`restore()`）。
不挑就不动库里一个字节 —— 用户要的是「先查看和选择，选了才算正式导入」。
"""
from __future__ import annotations

import base64
import gzip
import json
import os
import sqlite3
import time
from typing import Dict, List, Optional

import railcore

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKUP_ROOT = os.path.join(BASE, "data", "backup")

# 来源 → 人话（顺序也是列表里的展示顺序）
SOURCES = {
    "custom": "自建",
    "base": "底表",
    "osm": "OSM",
    "ce": "china-emu",
    "catalog": "线路资料库",
}

_CACHE: Dict[str, tuple] = {}      # tag → (mtime, {source: [line...]})


# ---------------------------------------------------------------------------
# 读备份
# ---------------------------------------------------------------------------

def _batch_dir(tag: str) -> str:
    tag = os.path.basename(str(tag or "").strip())
    if not tag:
        raise ValueError("没给备份批次")
    p = os.path.join(BACKUP_ROOT, tag)
    if not os.path.isdir(p):
        raise ValueError("没有这个备份批次：" + tag)
    return p


def batches() -> List[dict]:
    """所有备份批次（新的在前）"""
    out = []
    if not os.path.isdir(BACKUP_ROOT):
        return out
    for nm in os.listdir(BACKUP_ROOT):
        p = os.path.join(BACKUP_ROOT, nm)
        if not os.path.isdir(p):
            continue
        exp = os.path.join(p, "lines_export.json")
        db = os.path.join(p, "rail.db")
        has_exp = os.path.exists(exp)
        if not (has_exp or os.path.exists(db)):
            continue
        taken = ""
        if has_exp:
            try:
                taken = str(_read_json(exp).get("takenAt") or "")
            except (OSError, ValueError):
                taken = ""
        if not taken:
            taken = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(p)))
        data = _load(nm)
        n = sum(len(v) for k, v in data.items() if not k.startswith("_"))
        out.append({"tag": nm, "takenAt": taken, "hasExport": has_exp,
                    "n": n, "bytes": _dir_size(p)})
    out.sort(key=lambda x: x["tag"], reverse=True)
    return out


def _dir_size(p: str) -> int:
    try:
        return sum(os.path.getsize(os.path.join(p, f)) for f in os.listdir(p)
                   if os.path.isfile(os.path.join(p, f)))
    except OSError:
        return 0


def _read_json(path: str):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _load(tag: str) -> Dict[str, List[dict]]:
    """解析一个批次，按来源分组；同一个批次只解析一次（按 mtime 失效）"""
    p = _batch_dir(tag)
    exp = os.path.join(p, "lines_export.json")
    stamp = os.path.getmtime(exp if os.path.exists(exp) else p)
    hit = _CACHE.get(tag)
    if hit and hit[0] == stamp:
        return hit[1]
    if os.path.exists(exp):
        data = _index_json(_read_json(exp))
    else:
        data = _index_db(os.path.join(p, "rail.db"))
    _CACHE[tag] = (stamp, data)
    return data


def _stops(rows: List[dict], order_key: str = "seq") -> List[dict]:
    out = []
    for r in sorted(rows, key=lambda x: x.get(order_key) or 0):
        nm = str(r.get("name") or "").strip()
        if not nm:
            continue
        out.append({"name": nm, "km": _num(r.get("km"))})
    return out


def _num(v):
    try:
        return round(float(v), 1) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _group_stops(rows: List[dict], key: str = "lid") -> Dict[str, List[dict]]:
    by: Dict[str, List[dict]] = {}
    for r in rows or []:
        by.setdefault(str(r.get(key)), []).append(r)
    return by


def _line(name: str, stops: List[dict], extra: Optional[dict] = None) -> dict:
    km = None
    for s in stops:
        if s["km"] is not None:
            km = s["km"]
    rec = {"name": str(name or "").strip(), "n": len(stops), "stops": stops,
           "km": (extra or {}).get("km", km),
           "first": stops[0]["name"] if stops else "",
           "last": stops[-1]["name"] if stops else ""}
    if extra:
        rec.update({k: v for k, v in extra.items() if v is not None})
    return rec


def _index_json(d: dict) -> Dict[str, List[dict]]:
    rail = d.get("rail") or {}
    out: Dict[str, List[dict]] = {}

    # 自建：stations_json
    rows = []
    for r in rail.get("line_custom") or []:
        try:
            sts = json.loads(r.get("stations_json") or "[]")
        except (TypeError, ValueError):
            sts = []
        stops = [{"name": str(s.get("name") or "").strip(), "km": _num(s.get("km"))}
                 for s in sts if isinstance(s, dict) and str(s.get("name") or "").strip()]
        rows.append(_line(r.get("name"), stops, {"note": r.get("note") or ""}))
    out["custom"] = [x for x in rows if x["n"] >= 1]

    # 底表：stations
    rows = []
    for r in d.get("baseLines") or []:
        sts = r.get("stations") or []
        stops = [{"name": str(s.get("name") or "").strip(), "km": _num(s.get("km"))}
                 for s in sts if isinstance(s, dict) and str(s.get("name") or "").strip()]
        rows.append(_line(r.get("name"), stops, {"km": _num(r.get("totalKm"))}))
    out["base"] = [x for x in rows if x["n"] >= 1]

    # OSM / china-emu：主表 + 站表（lid = 线名）
    for src, main, stop in (("osm", "line_osm", "line_osm_stop"),
                            ("ce", "line_ce", "line_ce_stop"),
                            ("catalog", "line_catalog", "line_catalog_stop")):
        by = _group_stops(rail.get(stop) or [])
        rows = []
        for r in rail.get(main) or []:
            if src == "catalog":
                key = str(r.get("id"))
                nm = r.get("line_name") or r.get("name")
            else:
                key = str(r.get("name"))
                nm = r.get("name")
            stops = _stops(by.get(key) or [])
            if not nm:
                continue
            extra = {"km": _num(r.get("total_km") if src != "catalog" else r.get("length_km"))}
            if src == "catalog":
                extra["title"] = r.get("title") or ""
            rows.append(_line(nm, stops, extra))
        out[src] = [x for x in rows if x["n"] >= 1]
    # 走向单独挂在 _geom 上（不算来源，不参与列表）
    out["_geom"] = rail.get("line_geom") or []
    return out


def _index_db(path: str) -> Dict[str, List[dict]]:
    """批次里没有 lines_export.json 时，直接从整库副本读"""
    out: Dict[str, List[dict]] = {k: [] for k in SOURCES}
    if not os.path.exists(path):
        return out
    with sqlite3.connect("file:%s?mode=ro" % path, uri=True) as c:
        c.row_factory = sqlite3.Row
        have = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}

        def rows(t):
            try:
                return [dict(r) for r in c.execute("SELECT * FROM %s" % t)]
            except sqlite3.DatabaseError:
                return []

        for r in rows("line_custom"):
            try:
                sts = json.loads(r.get("stations_json") or "[]")
            except (TypeError, ValueError):
                sts = []
            stops = [{"name": str(s.get("name") or "").strip(), "km": _num(s.get("km"))}
                     for s in sts if isinstance(s, dict) and str(s.get("name") or "").strip()]
            out["custom"].append(_line(r.get("name"), stops, {"note": r.get("note") or ""}))
        for src, main, stop in (("osm", "line_osm", "line_osm_stop"),
                                ("ce", "line_ce", "line_ce_stop"),
                                ("catalog", "line_catalog", "line_catalog_stop")):
            by = _group_stops(rows(stop))
            for r in rows(main):
                if src == "catalog":
                    nm = r.get("line_name") or r.get("name")
                    stops = _stops(by.get(str(r.get("id"))) or [])
                    extra = {"km": _num(r.get("length_km")), "title": r.get("title") or ""}
                else:
                    nm = r.get("name")
                    stops = _stops(by.get(str(r.get("name"))) or [])
                    extra = {"km": _num(r.get("total_km"))}
                if nm and stops:
                    out[src].append(_line(nm, stops, extra))
    out["base"] = []                     # 底表在 cnrail_lines.json，整库副本里没有
    base = os.path.join(os.path.dirname(path), "cnrail_lines.json")
    if os.path.exists(base):
        try:
            for r in _read_json(base) or []:
                stops = [{"name": str(s.get("name") or "").strip(), "km": _num(s.get("km"))}
                         for s in (r.get("stations") or [])
                         if isinstance(s, dict) and str(s.get("name") or "").strip()]
                if stops:
                    out["base"].append(_line(r.get("name"), stops,
                                             {"km": _num(r.get("totalKm"))}))
        except (OSError, ValueError):
            pass
    return out


# ---------------------------------------------------------------------------
# 查
# ---------------------------------------------------------------------------

def _lib_names() -> set:
    """当前线路库里已经有哪些线（用来标「已在库」）"""
    try:
        with railcore.db() as conn:
            return {r[0] for r in conn.execute("SELECT name FROM line_custom")}
    except sqlite3.DatabaseError:
        return set()


def lines(tag: str = "", source: str = "", q: str = "",
          limit: int = 200, offset: int = 0) -> dict:
    """备份里的线路清单（tag 空 = 所有批次合起来看）"""
    names = _lib_names()
    tags = [b["tag"] for b in batches()] if not tag else [os.path.basename(tag)]
    q = (q or "").strip()
    items = []
    counts = {k: 0 for k in SOURCES}
    for tg in tags:
        try:
            data = _load(tg)
        except (OSError, ValueError):
            continue
        for src in SOURCES:
            for ln in data.get(src) or []:
                if q and q not in ln["name"] and q not in (ln.get("title") or ""):
                    continue
                counts[src] += 1
                if source and src != source:
                    continue
                items.append({"tag": tg, "source": src, "srcCn": SOURCES.get(src, src),
                              "name": ln["name"], "n": ln["n"], "km": ln["km"],
                              "first": ln["first"], "last": ln["last"],
                              "inLib": ln["name"] in names})
    items.sort(key=lambda x: (-x["n"], x["name"]))
    total = len(items)
    lo = max(0, int(offset or 0))
    hi = lo + max(1, min(int(limit or 200), 500))
    return {"items": items[lo:hi], "total": total, "counts": counts,
            "sources": SOURCES, "batches": batches()}


def _find(tag: str, source: str, name: str) -> Optional[dict]:
    data = _load(os.path.basename(tag or ""))
    for ln in data.get(source) or []:
        if ln["name"] == name:
            return ln
    return None


def detail(tag: str, source: str, name: str) -> dict:
    """一条线在备份里的站序（只读）"""
    if source not in SOURCES:
        raise ValueError("不认识的来源：" + str(source))
    ln = _find(tag, source, name)
    if not ln:
        raise ValueError("这个备份里没有「%s」（来源 %s）" % (name, SOURCES.get(source, source)))
    geom = _geom_info(tag, ln["name"])
    return {"tag": os.path.basename(tag), "source": source, "srcCn": SOURCES.get(source, source),
            "name": ln["name"], "n": ln["n"], "km": ln["km"],
            "first": ln["first"], "last": ln["last"], "note": ln.get("note") or "",
            "title": ln.get("title") or "", "stops": ln["stops"],
            "inLib": ln["name"] in _lib_names(), "geom": geom}


def _geom_info(tag: str, name: str) -> Optional[dict]:
    """备份里带不带这条线的走向（GeoJSON 导入的那批才有）"""
    try:
        data = _load(os.path.basename(tag or ""))
    except (OSError, ValueError):
        return None
    for g in data.get("_geom") or []:
        if g.get("name") == name:
            return {"km": _num(g.get("total_km")), "parts": g.get("n_part"),
                    "points": g.get("n_pt"), "file": g.get("file") or ""}
    return None


def _geom_row(tag: str, name: str) -> Optional[dict]:
    try:
        data = _load(os.path.basename(tag or ""))
    except (OSError, ValueError):
        return None
    for g in data.get("_geom") or []:
        if g.get("name") == name:
            return g
    return None


# ---------------------------------------------------------------------------
# 挑好了再导
# ---------------------------------------------------------------------------

def restore(tag: str, source: str, name: str, replace: bool = False) -> dict:
    """把选中的这条线正式写进线路库（自建线），站名 / 里程照备份原样"""
    if source not in SOURCES:
        raise ValueError("不认识的来源：" + str(source))
    ln = _find(tag, source, name)
    if not ln:
        raise ValueError("这个备份里没有「%s」" % name)
    stops = [{"name": s["name"], "km": s["km"]} for s in ln["stops"]]
    if len(stops) < 2:
        raise ValueError("这条线在备份里只有 %d 站（%s），至少要有 2 站才能作为线路导入"
                         % (len(stops), ln["stops"][0]["name"] if stops else "无"))
    note = "从备份 %s 导入 · %s · %d 站" % (os.path.basename(tag), SOURCES[source], len(stops))
    detail = railcore.save_custom_line(name, stops, note=note,
                                       replace=replace, raw=True, src="backup")
    geom = _restore_geom(tag, name)
    return {"ok": True, "name": detail.get("name") or name, "n": len(stops),
            "km": stops[-1]["km"], "note": note, "geom": geom,
            "line": detail}


def _restore_geom(tag: str, name: str) -> Optional[dict]:
    """备份里有这条线的走向（GeoJSON 那批）就连同走向一起写回去"""
    g = _geom_row(tag, name)
    if not g:
        return None
    try:
        parts = _blob(g.get("parts_gz"))
        spine = _blob(g.get("spine_gz")) if g.get("spine_gz") else None
    except (ValueError, TypeError):
        return None
    if not parts:
        return None
    with railcore._db_lock, railcore.db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS line_geom(
                 name TEXT PRIMARY KEY, file TEXT, total_km REAL, n_part INTEGER, n_pt INTEGER,
                 props TEXT, parts_gz BLOB, spine_gz BLOB, created_at REAL)""")
        try:
            conn.execute("ALTER TABLE line_geom ADD COLUMN spine_gz BLOB")
        except sqlite3.DatabaseError:
            pass
        conn.execute(
            """INSERT INTO line_geom(name, file, total_km, n_part, n_pt, props, parts_gz,
                                     spine_gz, created_at)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET file=excluded.file, total_km=excluded.total_km,
                 n_part=excluded.n_part, n_pt=excluded.n_pt, props=excluded.props,
                 parts_gz=excluded.parts_gz, spine_gz=excluded.spine_gz,
                 created_at=excluded.created_at""",
            (name, g.get("file") or "", _num(g.get("total_km")), g.get("n_part"),
             g.get("n_pt"), g.get("props") or "{}", parts, spine, time.time()))
    return {"km": _num(g.get("total_km")), "parts": g.get("n_part"),
            "points": g.get("n_pt"), "file": g.get("file") or ""}


def _blob(v) -> Optional[bytes]:
    """line_geom 的走向在 JSON 里是 {"__b64__": "..."}"""
    if isinstance(v, dict) and v.get("__b64__"):
        return base64.b64decode(v["__b64__"])
    if isinstance(v, str) and v:
        return base64.b64decode(v)
    return None


def geom_preview(tag: str, name: str) -> Optional[dict]:
    """备份里那条线的连续走向（给前端画小地图），没有就 None"""
    g = _geom_row(tag, name)
    if not g:
        return None
    raw = None
    try:
        raw = _blob(g.get("spine_gz")) or _blob(g.get("parts_gz"))
    except (ValueError, TypeError):
        return None
    if not raw:
        return None
    try:
        data = json.loads(gzip.decompress(raw).decode("utf-8"))
    except (OSError, ValueError, EOFError):
        return None
    if data and isinstance(data[0], list) and len(data[0]) >= 2:
        # spine 存的是 [[lon,lat],...]，parts 是 [[[lon,lat],...],...]
        if isinstance(data[0][0], list):
            data = [pt for seg in data for pt in seg]
        return {"parts": [data], "km": _num(g.get("total_km")),
                "points": len(data), "file": g.get("file") or ""}
    return None
