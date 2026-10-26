# -*- coding: utf-8 -*-
"""
railgeom.py — 线路的真实走向与几何里程

数据来自 CRTrainRoute（Laide140/CRTrainRoute，MIT）：32 个省级 OSM 铁路线要素，
按 `name`（线路名）归集后由 scripts/import_crtrain.py 整理成

  data/crtrain/lines.json   每条线路的里程 / 段落 / 桥隧 / 要素数
  data/crtrain/geoms.json   每条线路的简化折线（上下行已并成一条）

这里只做三件事：按线路名找几何（精确 → 同族）、给前端折线、做统计。
坐标系：OSM 原始数据是 WGS-84，前端统一转 GCJ-02 再画。
"""
from __future__ import annotations

import gzip
import json
import os
import re
import threading
from typing import Dict, List, Optional

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIR = os.path.join(BASE, "data", "crtrain")
LINES_JSON = os.path.join(DIR, "lines.json")
GEOMS_GZ = os.path.join(DIR, "geoms.json.gz")
GEOMS_JSON = os.path.join(DIR, "geoms.json")
STATIONS_JSON = os.path.join(DIR, "data_stations.json")

_TAIL = re.compile(r"(高速铁路|高速线|高铁|客运专线|客专线|城际铁路|城际线|普速线|铁路|线)$")
MIN_KM = 0.5          # 短于这个里程的线名当碎屑，不参与匹配和搜索

_lock = threading.Lock()
_META: Dict[str, dict] = {}       # 线路名 -> 元信息
_GEOMS: Optional[Dict[str, list]] = None   # 线路名 -> [[[lon,lat],…], …]（懒加载）
_FAM: Dict[str, List[str]] = {}   # 同族键 -> [线路名]
_LOADED = False

def _family(name: str) -> str:
    """去掉「线 / 铁路 / 高速线 / 客专线」等尾缀后的同族键"""
    n = re.sub(r"\s+", "", str(name or ""))
    return _TAIL.sub("", n) or n


def _suffix_kind(name: str) -> str:
    """
    口径：高铁 / 高速 / 客专 / 城际 都算「客运专线」，其余算普速。

    同一条线在不同资料里叫法很乱（柳南城际线 = 柳南客专线、贵广客运专线 = 贵广高铁），
    所以客运专线内部不再细分；但客运专线不能拿普速线的几何来套（包西高速线 ≠ 包西铁路）。
    """
    n = str(name or "")
    if re.search(r"(高速|客专|高铁|城际|客运专线)", n):
        return "hsr"
    return "normal"


def _load() -> None:
    global _LOADED
    if _LOADED:
        return
    with _lock:
        if _LOADED:
            return
        meta: Dict[str, dict] = {}
        try:
            with open(LINES_JSON, encoding="utf-8") as f:
                d = json.load(f)
            for rec in d.get("lines") or []:
                nm = rec.get("name")
                if nm:
                    meta[nm] = rec
        except (OSError, ValueError):
            meta = {}
        fam: Dict[str, List[str]] = {}
        for nm, rec in meta.items():
            if (rec.get("km") or 0) < MIN_KM:      # 只剩碎屑的线名不参与匹配/搜索
                continue
            fam.setdefault(_family(nm), []).append(nm)
        for v in fam.values():
            v.sort(key=lambda n: -meta[n].get("km", 0))
        _META.clear()
        _META.update(meta)
        _FAM.clear()
        _FAM.update(fam)
        _LOADED = True


def ready() -> bool:
    _load()
    return bool(_META)


def _geoms() -> Dict[str, list]:
    global _GEOMS
    if _GEOMS is None:
        with _lock:
            if _GEOMS is None:
                data: Dict[str, list] = {}
                try:
                    if os.path.exists(GEOMS_GZ):
                        with gzip.open(GEOMS_GZ, "rt", encoding="utf-8") as f:
                            data = json.load(f)
                    elif os.path.exists(GEOMS_JSON):
                        with open(GEOMS_JSON, encoding="utf-8") as f:
                            data = json.load(f)
                except (OSError, ValueError):
                    data = {}
                _GEOMS = data
    return _GEOMS


def match(name: str, expect_km: Optional[float] = None) -> Optional[tuple]:
    """
    线路名 -> (OSM 线路名, 'exact' / 'family')

    同族里可能有多个 OSM 线名（京沪高铁 / 京沪高速铁路 / 京沪高速线），
    优先「口径一致（客运专线 / 普速）+ 里程最长」的那条：
    OSM 里常有一条同名的短岔线（京沪高速铁路只有 9 km），字面精确反而指错。

    expect_km 给了已知里程时，几何里程不到它 35% 的候选直接排除
    （潍烟高速铁路在 OSM 只有 2 km 的残段，对不上 585 km 的潍烟高铁）。
    """
    _load()
    if not name:
        return None
    n = re.sub(r"\s+", "", str(name))
    cands = list(_FAM.get(_family(n)) or [])
    if not cands:
        return None
    want = _suffix_kind(n)
    same = [c for c in cands if _suffix_kind(c) == want]
    if not same:
        return None                      # 只有口径不同的同名线（包西高速线 vs 包西铁路），宁可没有
    if expect_km:
        try:
            floor = float(expect_km) * 0.35
        except (TypeError, ValueError):
            floor = 0.0
        if floor > 0:
            keep = [c for c in same if (_META[c].get("km") or 0) >= floor]
            if not keep:
                return None
            same = keep
    pick = max(same, key=lambda c: _META[c].get("km") or 0)
    return pick, ("exact" if pick == n else "family")


def meta(name: str, expect_km: Optional[float] = None) -> Optional[dict]:
    """线路元信息（不含几何）；expect_km 用来排除对不上的残段"""
    hit = match(name, expect_km)
    if not hit:
        return None
    nm, how = hit
    rec = dict(_META[nm])
    rec["match"] = how
    rec["ask"] = name
    return rec


def users_of(osm_name: str) -> List[str]:
    """哪些名字会匹配到这条 OSM 线路（同族的都算）"""
    _load()
    out = [osm_name]
    for nm in _FAM.get(_family(osm_name), []):
        if nm not in out:
            out.append(nm)
    return out


def geom(name: str, max_parts: int = 400, expect_km: Optional[float] = None) -> Optional[dict]:
    """线路几何：meta + 折线（WGS-84）"""
    rec = meta(name, expect_km)
    if not rec:
        return None
    g = _geoms().get(rec["name"]) or []
    if max_parts and len(g) > max_parts:
        # 碎段太多时按点数取前 max_parts 段，避免一次吐几 MB
        g = sorted(g, key=len, reverse=True)[:max_parts]
    rec["parts"] = g
    rec["crs"] = "WGS84"
    return rec


def search(q: str = "", limit: int = 30) -> List[dict]:
    """按名字搜 OSM 线路（元信息）"""
    _load()
    q = re.sub(r"\s+", "", str(q or ""))
    out = []
    for nm, rec in _META.items():
        if (rec.get("km") or 0) < MIN_KM:
            continue
        if q and q not in nm:
            continue
        out.append({k: rec.get(k) for k in
                    ("name", "km", "kmMain", "nPart", "nFeat", "nBridge", "nTunnel", "bbox")}
                   | {"match": "exact"})
    out.sort(key=lambda r: -r["km"])
    return out[:limit]


# --------------------------------------------------------------------------
# 12306 车站资料（同一份源数据里的 data/stations.json）
# 只做补充：缺电报码 / 想显示街道地址时用，不覆盖车站档案和人工坐标
# --------------------------------------------------------------------------
_ST: Optional[Dict[str, dict]] = None


def stations() -> Dict[str, dict]:
    """{站名(不带/带「站」两种键): {code, city, addr, lat, lon}}"""
    global _ST
    if _ST is None:
        with _lock:
            if _ST is None:
                out: Dict[str, dict] = {}
                try:
                    with open(STATIONS_JSON, encoding="utf-8") as f:
                        raw = json.load(f)
                    for code, v in (raw or {}).items():
                        nm = str(v.get("name") or "").strip()
                        if not nm:
                            continue
                        rec = {"code": code, "city": v.get("city") or "",
                               "addr": v.get("addr") or "",
                               "lat": v.get("lat"), "lon": v.get("lon"), "name": nm}
                        out.setdefault(nm, rec)
                        bare = nm[:-1] if nm.endswith("站") else nm
                        out.setdefault(bare, rec)
                except (OSError, ValueError):
                    out = {}
                _ST = out
    return _ST


def station_extra(name: str) -> Optional[dict]:
    """某个站在 12306 那边的补充资料（没有返回 None）"""
    n = re.sub(r"\s+", "", str(name or ""))
    if not n:
        return None
    st = stations()
    return st.get(n) or st.get(n[:-1] if n.endswith("站") else n + "站")


def stats() -> dict:
    _load()
    if not _META:
        return {"lines": 0, "km": 0}
    return {
        "lines": len(_META),
        "km": round(sum(r.get("km") or 0 for r in _META.values())),
        "bridge": sum(r.get("nBridge") or 0 for r in _META.values()),
        "tunnel": sum(r.get("nTunnel") or 0 for r in _META.values()),
        "feat": sum(r.get("nFeat") or 0 for r in _META.values()),
        "source": "CRTrainRoute / OSM",
    }
