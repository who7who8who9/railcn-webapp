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
import math
import os
import re
import threading
from typing import Dict, List, Optional, Sequence, Tuple

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


# --------------------------------------------------------------------------
# OSM-only 线路补站：按几何反查车站库，找出压在线上的站
#
# 线路库里没有、只有 OSM 几何的线（联络线、支线最常见），cnrail 那边没有站表，
# 也就没法回车次经由 / 里程。这里给出「候选站 + 沿线里程」，让新增线路的弹窗
# 一打开就是填好的，人只需要删减补漏。
# --------------------------------------------------------------------------
_EARTH_M = 6371008.8
_M_LAT = 110574.0        # 每度纬度折合米
_M_LON = 111320.0        # 赤道上每度经度折合米


def _dist_m(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    """两点球面距离（米）；a / b 都是 (lat, lon)"""
    lat1, lon1 = a
    lat2, lon2 = b
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    h = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return 2 * _EARTH_M * math.asin(min(1.0, math.sqrt(h)))


def _norm_parts(parts: Sequence[Sequence[Sequence[float]]]) -> List[List[Tuple[float, float]]]:
    """OSM 的 [[lon,lat],…] 折线转成 (lat,lon)，丢掉不足两点的碎段"""
    out: List[List[Tuple[float, float]]] = []
    for part in parts or []:
        pts: List[Tuple[float, float]] = []
        for p in (part or []):
            if not p or len(p) < 2 or p[0] is None or p[1] is None:
                continue
            try:
                pts.append((float(p[1]), float(p[0])))
            except (TypeError, ValueError):
                continue
        if len(pts) >= 2:
            out.append(pts)
    return out


def _poly_km(pts: Sequence[Tuple[float, float]]) -> float:
    """折线长度（公里）"""
    total = sum(_dist_m(pts[i - 1], pts[i]) for i in range(1, len(pts)))
    return total / 1000.0


def _chain(parts: Sequence[Sequence[Sequence[float]]],
           gap_m: float = 3000.0) -> Tuple[List[Tuple[float, float]], float, float]:
    """串线主流程：返回 (链, 判为复线丢掉的公里数, 因接不上丢掉的公里数)"""
    segs = _norm_parts(parts)
    if not segs:
        return [], 0.0, 0.0
    segs.sort(key=len, reverse=True)
    chain = list(segs.pop(0))
    dup_km = 0.0

    while segs:
        tail, head = chain[-1], chain[0]
        span = _dist_m(head, tail)
        best = None
        for i, pts in enumerate(segs):
            for rev in (False, True):
                seq = pts[::-1] if rev else pts
                dt = _dist_m(tail, seq[0])          # 接在尾部
                if best is None or dt < best[0]:
                    best = (dt, i, rev, "tail")
                dh = _dist_m(head, seq[-1])         # 接在头部
                if dh < best[0]:
                    best = (dh, i, rev, "head")
        if best is None or best[0] > gap_m:
            break                       # 剩下的段离得太远，不硬接
        _, i, rev, where = best
        seq = segs[i][::-1] if rev else list(segs[i])
        if where == "tail":
            # 上下行复线：接完只是原路折回、离起点更近 —— 判为复线，丢掉这条
            if _dist_m(seq[-1], head) <= span + 200:
                dup_km += _poly_km(seq)
                segs.pop(i)
                continue
            segs.pop(i)
            chain.extend(seq)           # 段间缺口也计入里程（那是真实存在的轨道）
        else:
            if _dist_m(seq[0], tail) <= span + 200:
                dup_km += _poly_km(seq)
                segs.pop(i)
                continue
            segs.pop(i)
            chain = seq + chain

    drop_km = sum(_poly_km(pts) for pts in segs)
    return chain, dup_km, drop_km


def chain_parts(parts: Sequence[Sequence[Sequence[float]]],
                gap_m: float = 3000.0) -> List[Tuple[float, float]]:
    """
    把多段折线串成一条连续折线（同一条线常被切成上下行 / 桥隧 / 跨江碎段）。

    从最长的一段起头，往两头挑「端点最近」的段接上（必要时反向），端点间距超过
    gap_m 就停 —— 剩下的段宁可丢掉，也别把两条不相干的线接起来；接上去只是原路
    折回的段（上下行复线）也直接丢。入参点序是 OSM 原样的 [lon, lat]，出参 (lat, lon)。
    """
    return _chain(parts, gap_m)[0]


def chain_km(parts: Sequence[Sequence[Sequence[float]]],
             gap_m: float = 3000.0) -> float:
    """串成单线后的实际里程（公里）。OSM 源数据的 km 常把上下行复线各算一遍"""
    chain = _chain(parts, gap_m)[0]
    return round(_poly_km(chain), 1) if len(chain) > 1 else 0.0


def chain_info(parts: Sequence[Sequence[Sequence[float]]],
               gap_m: float = 3000.0) -> dict:
    """
    单线里程 + 拼接情况：

    dupKm   判为上下行复线丢掉的长度（正常，源数据常把两条各算一遍）
    dropKm  接不上丢掉的长度（走向有缺口 / 同名线其实是两条不同的线）
    complete  dropKm < 0.5 时才算「整条线都串上了」，这时单线里程才可信
    """
    chain, dup, drop = _chain(parts, gap_m)
    km = round(_poly_km(chain), 1) if len(chain) > 1 else 0.0
    return {"km": km, "dupKm": round(dup, 1), "dropKm": round(drop, 1),
            "complete": drop < 0.5}


def _hit_seg(a: Tuple[float, float], b: Tuple[float, float],
             p: Tuple[float, float]) -> Tuple[float, Tuple[float, float]]:
    """点到线段的垂距（米）与垂足经纬度；以线段起点为原点做等距投影"""
    lat0, lon0 = a
    sx = _M_LON * math.cos(math.radians(lat0))
    sy = _M_LAT

    def xy(q: Tuple[float, float]) -> Tuple[float, float]:
        return (q[1] - lon0) * sx, (q[0] - lat0) * sy

    bx, by = xy(b)
    px, py = xy(p)
    l2 = bx * bx + by * by
    if l2 <= 0:
        return math.hypot(px, py), a
    t = (px * bx + py * by) / l2
    t = 0.0 if t < 0 else (1.0 if t > 1 else t)
    fx, fy = t * bx, t * by
    return (math.hypot(px - fx, py - fy),
            (lat0 + fy / sy, lon0 + fx / sx))


def _cell(pt: Tuple[float, float], cell: float = 0.01) -> Tuple[int, int]:
    """0.01°（≈1 km）网格坐标"""
    return int(math.floor(pt[0] / cell)), int(math.floor(pt[1] / cell))


def stations_along(parts: Sequence[Sequence[Sequence[float]]],
                   stations: Sequence[dict],
                   radius_m: float = 1200.0,
                   extend_m: float = 3000.0,
                   gap_m: float = 3000.0,
                   must: Optional[Sequence[str]] = None) -> List[dict]:
    """
    车站库里哪些站压在这条 OSM 线上。

    stations: [{name, lat, lon, type…}]
    must:     站名列表。这些站即使离几何很远也返回（side='table'），
              用于「车站档案里已注明属于这条线」的站，免得几何截断就漏掉终点站
    返回按沿线顺序排好的 [{name, lat, lon, km, dist, side}]：
      side='on'    轨面 radius_m 以内，km 是沿线累计里程（公里，一位小数）
      side='start' 起点外 extend_m 以内（几何被截断，实际起点在更远处），km 为空
      side='end'   终点外 extend_m 以内
      side='table' 车站档案注明属于这条线，km 按最近点折算
    几何抽稀后每段只有几十米，所以按段起点 0.01°（≈1 km）分桶索引，
    7579 个站也只要几万次判断。
    """
    segs = _norm_parts(parts)          # 判「压在线附近」用全部段（含被链丢掉的复线 / 碎段）
    chain = chain_parts(parts, gap_m)  # 折算里程用串好的单线
    if len(chain) < 2 or not segs:
        return []
    cum = [0.0]
    for i in range(1, len(chain)):
        cum.append(cum[-1] + _dist_m(chain[i - 1], chain[i]))
    total_m = cum[-1]
    must_set = {m for m in (must or []) if m}

    cell = 0.01
    flat: List[Tuple[Tuple[float, float], Tuple[float, float]]] = []
    grid: Dict[Tuple[int, int], List[int]] = {}
    for pts in segs:
        for i in range(len(pts) - 1):
            si = len(flat)
            flat.append((pts[i], pts[i + 1]))
            for pt in (pts[i], pts[i + 1]):
                grid.setdefault(_cell(pt, cell), []).append(si)
    ch_grid: Dict[Tuple[int, int], List[int]] = {}
    for i, pt in enumerate(chain):
        ch_grid.setdefault(_cell(pt, cell), []).append(i)

    def near_chain(p: Tuple[float, float], full: bool = False) -> Tuple[int, float]:
        """最近的链上折点（下标, 距离）；网格里没有就退化为全表扫"""
        idxs: List[int] = []
        cy, cx = _cell(p, cell)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                idxs.extend(ch_grid.get((cy + dy, cx + dx)) or [])
        if not idxs or full:
            idxs = list(range(len(chain)))
        bi, bd = 0, None
        for i in idxs:
            d = _dist_m(chain[i], p)
            if bd is None or d < bd:
                bd, bi = d, i
        return bi, float(bd or 0.0)

    out: List[dict] = []
    for st in stations or []:
        lat, lon = st.get("lat"), st.get("lon")
        if lat is None or lon is None:
            continue
        lat, lon = float(lat), float(lon)
        idxs = []
        cy, cx = _cell((lat, lon), cell)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                idxs.extend(grid.get((cy + dy, cx + dx)) or [])
        best_d, foot = None, (lat, lon)
        for si in set(idxs):
            a, b = flat[si]
            d, fp = _hit_seg(a, b, (lat, lon))
            if best_d is None or d < best_d:
                best_d, foot = d, fp
        side, km = None, None
        if best_d is not None and best_d <= radius_m:
            ci, cd = near_chain(foot)
            if cd > 800 and ci in (0, len(chain) - 1):
                # 压在被丢掉的碎段上，而那段在链的端点之外（几何被截断 / 另一条走法）：
                # 位置落在两端之外，里程留空，避免编一个数出来
                side = "start" if ci == 0 else "end"
                best_d = _dist_m(foot, chain[0] if ci == 0 else chain[-1])
            else:
                side, km = "on", cum[ci]
        else:
            ds = _dist_m((lat, lon), chain[0])
            de = _dist_m((lat, lon), chain[-1])
            if ds <= extend_m and ds <= de:
                side, best_d = "start", ds
            elif de <= extend_m:
                side, best_d = "end", de
            elif st.get("name") in must_set:
                # 档案里注明属于这条线，但几何没扫到：按最近折点折算里程
                ci, cd = near_chain((lat, lon), full=True)
                side, best_d, km = "table", cd, cum[ci]
        if not side:
            continue
        out.append({"name": st.get("name") or "", "lat": lat, "lon": lon,
                    "km": km, "dist": float(best_d or 0.0), "side": side,
                    "type": st.get("type") or "", "tele": st.get("tele") or ""})

    # 排序：起点外的（远→近）→ 轨面上按里程 → 终点外的（近→远）
    for x in out:
        if x["side"] == "start":
            x["pos"] = -x["dist"]
        elif x["side"] == "end":
            x["pos"] = total_m + x["dist"]
        else:
            x["pos"] = x["km"] if x["km"] is not None else 0.0
    seen, rows = set(), []
    for x in sorted(out, key=lambda x: (x["pos"], x["dist"])):
        nm = x["name"]
        if not nm or nm in seen:
            continue
        seen.add(nm)
        rows.append(x)

    # 同一位置压着几个站时，里程会被 round 成同一个数，保存时会被判「不递增」；
    # 这里先错开 0.1 km，用户在弹窗里还能再改
    last = None
    for x in rows:
        if x["km"] is None:
            continue
        km = round(x["km"] / 1000.0, 1)
        if last is not None and km <= last:
            km = round(last + 0.1, 1)
        x["km"] = km
        last = km
    for x in rows:
        x["dist"] = round(x["dist"])
    return rows


def spine_km(name: str, expect_km: Optional[float] = None) -> Optional[float]:
    """
    单线里程：把上下行复线并掉之后的实际长度。

    OSM 源数据里的 km 是「各要素里程相加」，同一段上下行分开建要素时会各算一遍
    （赵甸联络线 10.5 km，实际单线 5.9 km）。补站时里程按单线折算，
    所以和档案里程比差异也该用这个值。
    """
    rec = meta(name, expect_km)
    if not rec:
        return None
    parts = _geoms().get(rec["name"]) or []
    if not parts:
        return None
    info = chain_info(parts)
    # 走向有缺口时单线里程不可信（少算一大段），宁可不给，让前端退回源数据里程
    return info["km"] or None if info["complete"] else None


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
