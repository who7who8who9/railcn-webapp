# -*- coding: utf-8 -*-
"""
railosm.py — 车次路线的「OSM 真实走向」版

数据源：data/crtrain/geoms.json.gz（按线名切好的线路几何，WGS-84）

做法：
  1. 把每个几何段（part）的两端投影到车次现有折线（站点 + 沿途通过站）上，
     得到「里程 + 偏离」；
  2. 两端都贴着走廊的段整段保留，段内里程按弧长线性插值（不逐顶点筛，否则会把段切碎）；
  3. 各段按里程区间贪心覆盖，复线上下行只留一条；
  4. 覆盖不到的地方，用沿途通过站一站一站地直线补过去（比一根大斜线好看得多），
     缺口位置与里程仍旧如实报出来。

已知局限：这份几何是按线名切碎的，跨线车次和普速车次常有缺口（见 README）。
"""
from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple

import railgeom

EARTH_R = 6371.0088


def _hav(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    """两点球面距离（km），a/b = (lat, lon)"""
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dl = math.radians(b[1] - a[1])
    dp = p2 - p1
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(h))


def _proj(path: Sequence[dict], lon: float, lat: float) -> Tuple[float, float]:
    """把点投影到走廊折线上，返回 (里程 km, 偏离 km)"""
    best_km, best_d = 0.0, 1e18
    for i in range(1, len(path)):
        a, b = path[i - 1], path[i]
        ax, ay, bx, by = a["lon"], a["lat"], b["lon"], b["lat"]
        dx, dy = bx - ax, by - ay
        l2 = dx * dx + dy * dy
        t = 0.0 if l2 <= 0 else max(0.0, min(1.0, ((lon - ax) * dx + (lat - ay) * dy) / l2))
        d = _hav((ay + dy * t, ax + dx * t), (lat, lon))
        if d < best_d:
            best_d = d
            best_km = a["km"] + (b["km"] - a["km"]) * t
    return best_km, best_d


def _parts_of(route: dict) -> Tuple[List[list], List[str]]:
    """收集这条车次经由线路的几何段（同一个线名只取一次）"""
    parts: List[list] = []
    names: List[str] = []
    seen = set()
    for seg in route.get("viaSegs") or []:
        nm = seg.get("line")
        if not nm or nm in seen:
            continue
        seen.add(nm)
        g = railgeom.geom(nm)
        if g and g.get("parts"):
            names.append(g.get("name") or nm)
            parts.extend(g["parts"])
    return parts, names


def _between(path: Sequence[dict], a: float, b: float) -> List[dict]:
    """走廊上里程落在 (a, b) 之间的站点/通过站 —— 缺口就靠它们一站一站地直线补"""
    return [p for p in path if a + 0.3 < (p.get("km") or 0) < b - 0.3]


def build(route: dict, max_off_km: float = 12.0) -> Tuple[Optional[List[dict]], dict]:
    """
    route: railcore.build_route_payload 的返回值（要带 path / stops / viaSegs）
    返回 (pts, info)；pts 为 [{lat, lon, km}]，沿车次里程排好的一条折线
    """
    path = [p for p in (route.get("path") or []) if p.get("lat") is not None]
    if len(path) < 2:
        return None, {"err": "这条车次没有可用的折线"}
    total = float(path[-1].get("km") or 0)

    parts, names = _parts_of(route)
    if not parts:
        return None, {"err": "经由线路在 OSM 里没有几何"}

    runs: List[List[Tuple[float, float, float]]] = []
    for part in parts:
        if len(part) < 2:
            continue
        k0, d0 = _proj(path, part[0][0], part[0][1])
        k1, d1 = _proj(path, part[-1][0], part[-1][1])
        if d0 > max_off_km or d1 > max_off_km:
            continue                        # 这一整段不贴着车次走
        length = sum(_hav(part[i - 1], part[i]) for i in range(1, len(part)))
        if length < 0.2 or abs(k1 - k0) < 0.3:
            continue                        # 碎屑 / 岔线
        pts = part[::-1] if k1 < k0 else list(part)
        lo, hi = min(k0, k1), max(k0, k1)
        out, acc = [], 0.0
        for i, (lon, lat) in enumerate(pts):
            if i:
                acc += _hav(pts[i - 1], pts[i])
            out.append((lo + (hi - lo) * acc / length, lon, lat))
        runs.append(out)

    if not runs:
        return None, {"err": "线路几何都离这条车次太远", "lines": names}

    # 区间覆盖：从最小里程起，每次挑「接得上且走得最远」的段（复线自然只留一条）
    runs.sort(key=lambda r: r[0][0])
    kept: List[List[Tuple[float, float, float]]] = []
    gaps: List[Tuple[float, float]] = []
    pos = runs[0][0][0]
    while runs:
        best = None
        for r in runs:
            if r[0][0] <= pos + 3.0 and r[-1][0] > pos + 0.3:
                if best is None or r[-1][0] > best[-1][0]:
                    best = r
        if best is None:                    # 断了：跳到下一段，中间那段留给直线
            best = min(runs, key=lambda r: r[0][0])
            if best[0][0] > pos + 0.3:
                gaps.append((round(pos, 1), round(best[0][0], 1)))
        kept.append(best)
        pos = best[-1][0]
        runs = [r for r in runs if r is not best and r[-1][0] > pos + 0.3]

    pts: List[dict] = []
    fill = 0                                # 用来补缺口的通过站个数
    prev_end: Optional[float] = None
    for r in kept:
        if prev_end is not None and r[0][0] - prev_end > 0.3:
            for q in _between(path, prev_end, r[0][0]):   # 缺口：按沿途通过站补过去
                if pts and q["km"] - pts[-1]["km"] < 0.05:
                    continue
                pts.append({"lat": q["lat"], "lon": q["lon"], "km": q["km"]})
                fill += 1
        for km, lon, lat in r:
            if pts and km - pts[-1]["km"] < 0.001:
                continue
            pts.append({"lat": lat, "lon": lon, "km": km})
        prev_end = r[-1][0]

    if len(pts) < 2:
        return None, {"err": "拼出来的点太少"}

    # 首尾补上始发/终到站，保证列车从 0 走到全程
    if pts[0]["km"] > 0.3:
        head = _between(path, -0.3, pts[0]["km"])
        pts[:0] = [{"lat": q["lat"], "lon": q["lon"], "km": q["km"]} for q in head]
        pts.insert(0, {"lat": path[0]["lat"], "lon": path[0]["lon"], "km": 0.0})
    if pts[-1]["km"] < total - 0.3:
        pts.extend({"lat": q["lat"], "lon": q["lon"], "km": q["km"]}
                   for q in _between(path, pts[-1]["km"], total))
        pts.append({"lat": path[-1]["lat"], "lon": path[-1]["lon"], "km": total})

    # 首尾拼接处可能落进同一个点，去个重
    clean: List[dict] = []
    for p in pts:
        q = clean[-1] if clean else None
        if q and abs(p["km"] - q["km"]) < 1e-6 and abs(p["lat"] - q["lat"]) < 1e-9 and abs(p["lon"] - q["lon"]) < 1e-9:
            continue
        clean.append(p)
    pts = clean

    geo = sum(_hav((pts[i - 1]["lat"], pts[i - 1]["lon"]), (pts[i]["lat"], pts[i]["lon"]))
              for i in range(1, len(pts)))
    # 覆盖里程 = 全程 − 缺口总长（缺口就是那些只能拿直线跳过去的地方）
    gap_km = sum(b - a for a, b in gaps)
    covered = max(0.0, min(total, total - gap_km))
    info = {
        "lines": names,
        "parts": len(parts),
        "segments": len(kept),
        "total": round(total, 1),
        "geo": round(geo, 1),
        "covered": round(covered, 1),
        "gapKm": round(gap_km, 1),
        "cover": round(covered / total, 3) if total > 0 else 0,
        "gaps": [{"from": a, "to": b} for a, b in gaps],
        "fill": fill,
        "from": round(pts[0]["km"], 1),
        "to": round(pts[-1]["km"], 1),
    }
    return pts, info
