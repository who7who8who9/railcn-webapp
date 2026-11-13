# -*- coding: utf-8 -*-
"""
railosm.py — 车次路线的「OSM 真实走向」版

数据源（2026-11-11 起，按优先级）：
  1. 整理台「全部确认」线路的定稿走向（`confirmed.geom()` → `ld_geom` 多段）；
  2. 主站共用的走向入口 `lineosm.geom()`（基本线路 / 导入 GeoJSON / OSM）；
  3. 老 OSM 包 `data/crtrain/geoms.json.gz`（按线名切好的线路几何，WGS-84）。

老包里没有「太中线」「石获线」，Z75 的 太原→中卫 700 km 只能画成一根大直线，
用户看到的「按真实走向」和「按站点直连」几乎一样不对 —— 整理台明明已经把
这 7 段全部确认过了，车次详情却完全没用那份走向。

做法：
  1. 把每个几何段（part）逐点投影到车次现有折线上，按「离走廊 ≤ off_km」
     切成连续子段 —— 整条线的几何（京广线 28 段 / 2344 km）里只留本趟车走的
     那一截，别把广州以南的段混进来，也别因为段端点在远处就把长段整段丢掉；
  2. 子段两端投影得到「里程 + 偏离」，段内里程按弧长线性插值；
  3. 各段按里程区间贪心覆盖，复线上下行只留一条；
  4. 覆盖不到的地方，只用**本趟车实际停车站**直线补过去。别用 `path_stations()`
     从线路库展开的通过站：那些坐标不少是仿射假值 / 站序错乱，补进去就是锯齿。

已知局限：这份几何是按线名切碎的，跨线车次和普速车次仍可能有小缺口（见 README）。
"""
from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple

import railgeom

EARTH_R = 6371.0088
#: 走廊宽度（km）。长距离车次放宽一点才留得住绕弯的线段，短途收窄免得蹭到平行线
OFF_MIN, OFF_MAX = 12.0, 45.0
#: 「本段站序」二次裁剪的宽度（km）：线路几何只留贴着这一段站序的部分，
#: 免得把同一条线往别处延伸的那截（滨洲线过昂昂溪继续往满洲里）也拼进车次里
SEG_OFF = 18.0
#: 全局不退化闸：按本段站序裁剪后，覆盖率比「不裁」低超过 2 个百分点、
#: 或终点比「不裁」多偏 5 km，就整趟退回不裁的版本。规则要对所有车次生效，
#: 但绝不能因为推广它让某趟车比原来更差。
COVER_DROP = 0.02
END_WORSE_KM = 5.0


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


def _geom_of(name: str) -> Optional[dict]:
    """一条经由线路的走向：整理台定稿 > 主站共用入口 > 老 OSM 包。

    以前只查老包 `railgeom.geom()`，整理台确认过的「太中线」「石获线」在老包里
    压根没有，Z75 的 太原→中卫 700 km 就只能画一根大直线。
    """
    try:
        import confirmed
        g = confirmed.geom(name)                 # 只认「全部确认」白名单，没确认返回 None
    except Exception:                            # noqa: BLE001  库被占用 / 旧表结构
        g = None
    if not (g and g.get("parts")):
        try:
            import lineosm
            g = lineosm.geom(name)
        except Exception:                        # noqa: BLE001
            g = None
    if not (g and g.get("parts")):
        try:
            g = railgeom.geom(name)
        except Exception:                        # noqa: BLE001
            g = None
    return g if (g and g.get("parts")) else None


def _parts_of(route: dict) -> Tuple[List[tuple], List[str]]:
    """收集这条车次经由线路的几何段，按线名分组（同一个线名只取一次）。

    分组是为了后面能按「这条线本趟车只走 from→to 那一截」做二次裁剪；
    以前直接摊平，滨洲线往满洲里那一大截和进齐齐哈尔的平齐线短段混在一起，
    贪心拼段就挑了长的那截、把正确的尾巴丢掉。
    """
    groups: List[tuple] = []
    names: List[str] = []
    seen = set()
    for seg in route.get("viaSegs") or []:
        nm = seg.get("line")
        if not nm or nm in seen:
            continue
        seen.add(nm)
        g = _geom_of(nm)
        if g:
            names.append(g.get("name") or nm)
            groups.append((nm, g["parts"]))
    return groups, names


_SEGPOLY: dict = {}
_SEGPOLY_TS = 0.0


def _seg_polyline(name: str, frm: str, to: str) -> Optional[dict]:
    """这条经由线路 `from→to` 那一截的站序折线，**走全项目唯一站序入口**。

    `cnrail.via_segment_polyline()` 与 `path_stations()` 的跨线链同源：
    整理台确认站序优先、线路库（含「（来源）」后缀版本）兜底。未确认的线
    也能拿到站序，只是 `confirmed=False`，裁剪门槛相应抬高。
    返回 `{"pts":[(lon,lat),…], "confirmed":bool, "label":str}`，
    认不出端点 / 点不足两个就 None（调用方退回「不裁剪」）。
    20 秒缓存：整理台改了站序会自动跟上，又不会每趟车都开一次库。
    """
    global _SEGPOLY_TS
    import time
    now = time.time()
    if now - _SEGPOLY_TS > 20:
        _SEGPOLY.clear()
        _SEGPOLY_TS = now
    ck = (name, frm, to)
    if ck in _SEGPOLY:
        return _SEGPOLY[ck]
    out = None
    try:
        import cnrail
        out = cnrail.via_segment_polyline(name, frm, to)
    except Exception:                                # noqa: BLE001 库忙 / 表结构旧
        out = None
    if not out or len(out.get("pts") or []) < 2:
        out = None
    _SEGPOLY[ck] = out
    return out


def _poly_dist(lon: float, lat: float, poly: Sequence[tuple]) -> float:
    """点到折线的最短距离（km）。裁剪切阈值用等距近似就够，别对几千点逐段算球面三角"""
    kx = 111.32 * max(0.2, math.cos(math.radians(lat)))
    ky = 111.32
    best2 = 1e36
    for i in range(1, len(poly)):
        ax, ay = poly[i - 1]
        bx, by = poly[i]
        dx, dy = (bx - ax) * kx, (by - ay) * ky
        l2 = dx * dx + dy * dy
        px, py = (lon - ax) * kx, (lat - ay) * ky
        t = 0.0 if l2 <= 0 else max(0.0, min(1.0, (px * dx + py * dy) / l2))
        ex, ey = px - t * dx, py - t * dy
        d2 = ex * ex + ey * ey
        if d2 < best2:
            best2 = d2
    return math.sqrt(best2)


def _seg_hug(parts: Sequence[Sequence], polys: Sequence[Sequence[tuple]]) -> float:
    """这份「本段站序」到底贴不贴线路几何。

    整理台站序的坐标并非都准（Z75 石太线那 111 个点中位偏离 25.8 km），
    拿它硬裁会把真实走向切光。只有大多数贴线点都在 8 km 内才敢裁；
    坐标不可靠的线退回原来的「只按车次走廊裁」。
    """
    near = hug = 0
    for part in parts:
        for p in part:
            if len(p) < 2:
                continue
            d = min(_poly_dist(float(p[0]), float(p[1]), poly) for poly in polys)
            if d < 45:
                near += 1
                if d < 8:
                    hug += 1
    return hug / max(1, near)


def _clip_to_seg(part: Sequence, polys: Sequence[Sequence[tuple]]) -> List[list]:
    """按本段站序把几何段切成连续子段，跑出这一截的点直接丢掉"""
    cur: List[tuple] = []
    out: List[list] = []
    for p in part:
        if len(p) < 2:
            continue
        lon, lat = float(p[0]), float(p[1])
        if min(_poly_dist(lon, lat, poly) for poly in polys) <= SEG_OFF:
            cur.append((lon, lat))
        else:
            if len(cur) >= 2:
                out.append(cur)
            cur = []
    if len(cur) >= 2:
        out.append(cur)
    return out


def _trim_parts(parts: Sequence[Sequence], path: Sequence[dict],
                off_km: float) -> List[list]:
    """整条线的几何里只留贴着本趟车走廊的连续子段。

    老逻辑只看段的两端：京广线确认几何有个 820 km 的长段，一端在广州、
    一端在北京，两端都离 Z75 走廊几百公里，整段丢掉 —— 可良乡→石家庄
    那一截明明就在段里。改成逐点切，远处的点断开，留下的子段各自成段，
    广州以南那些段也自然全部被切掉，不会混进车次地图。
    """
    out: List[list] = []
    for part in parts:
        if len(part) < 2:
            continue
        cur: List[Tuple[float, float]] = []
        for p in part:
            if len(p) < 2:
                continue
            lon, lat = float(p[0]), float(p[1])
            _, d = _proj(path, lon, lat)
            if d <= off_km:
                cur.append((lon, lat))
            else:
                if len(cur) >= 2:
                    out.append(cur)
                cur = []
        if len(cur) >= 2:
            out.append(cur)
    return out


def _between(path: Sequence[dict], a: float, b: float) -> List[dict]:
    """走廊上里程落在 (a, b) 之间的点 —— 缺口就靠它们一站一站地直线补"""
    return [p for p in path if a + 0.3 < (p.get("km") or 0) < b - 0.3]


def _end_off(pts: Sequence[dict], stop: dict) -> float:
    """拼出来的终点离终到站多远（km）"""
    if not pts or not stop:
        return 1e9
    return _hav((pts[-1]["lat"], pts[-1]["lon"]), (stop["lat"], stop["lon"]))


def _clipped_worse(cur: dict, cur_pts: Optional[List[dict]],
                   raw: dict, raw_pts: Optional[List[dict]],
                   stop_path: Sequence[dict]) -> bool:
    """「按本段站序裁剪」是否明显劣于「不裁」。

    覆盖率相同（N17：裁不裁都是 1.0）时**保留裁剪** —— 未裁那版会往西绕到
    满洲里方向再折回，光看覆盖率发现不了；只有明显变差才退回。
    """
    if not raw_pts:
        return False
    if not cur_pts:
        return True
    if (raw.get("cover") or 0) - (cur.get("cover") or 0) > COVER_DROP:
        return True
    end = stop_path[-1] if stop_path else None
    if end and _end_off(cur_pts, end) - _end_off(raw_pts, end) > END_WORSE_KM:
        return True
    return False


def build(route: dict, max_off_km: Optional[float] = None) -> Tuple[Optional[List[dict]], dict]:
    """
    route: railcore.build_route_payload 的返回值（要带 path / stops / viaSegs）
    返回 (pts, info)；pts 为 [{lat, lon, km}]，沿车次里程排好的一条折线
    """
    path = [p for p in (route.get("path") or []) if p.get("lat") is not None]
    if len(path) < 2:
        return None, {"err": "这条车次没有可用的折线"}
    total = float(path[-1].get("km") or 0)
    if max_off_km is None:
        # 长途车绕弯多，走廊放宽些才留得住线段；短途收窄免得蹭到平行线
        max_off_km = min(OFF_MAX, max(OFF_MIN, total * 0.03))

    # 缺口只拿本趟车实际停车站补：线路库展开出来的通过站坐标不少是假值、
    # 站序也乱，补进去就是一串锯齿（Z75 北京西→保定段最明显）
    stop_path = [{"lat": s["lat"], "lon": s["lon"], "km": float(s.get("km") or 0)}
                 for s in (route.get("stops") or []) if s.get("lat") is not None]
    if len(stop_path) < 2:
        stop_path = path

    groups, names = _parts_of(route)
    if not groups:
        return None, {"err": "经由线路在 OSM 里没有几何"}

    # 每条线只留「本趟车走的 from→to 那一截」：线路几何常常是整条线
    # （滨洲线一路到满洲里、平齐线一路到四平），光按车次走廊裁会把
    # 同方向伸出去的岔线也留下来，最后拼出往西绕一圈再折回的假走向。
    #
    # **全项目统一规则，不是某趟车的特例**：站序统一走
    # cnrail.via_segment_polyline()（确认站序优先、线路库兜底，与
    # path_stations() 的跨线链同源），再用 _seg_hug() 判这份站序能不能信。
    base_parts: List[list] = []          # 不裁剪的整条线几何（保底 / 对照）
    clip_parts: List[list] = []          # 按线名 + 本段站序裁过的几何
    clipped: List[str] = []
    for nm, ps in groups:
        base_parts.extend(ps)
        metas = [m for s in (route.get("viaSegs") or [])
                 if s.get("line") == nm
                 for m in (_seg_polyline(nm, s.get("from") or "", s.get("to") or ""),)
                 if m]
        polys = [m["pts"] for m in metas]
        if not polys:
            clip_parts.extend(ps)                    # 认不出这一截：沿用老逻辑
            continue
        # 站序坐标不可靠就别裁（Z75 石太线中位偏离 25 km，硬裁会切光整段）。
        # 底表兜底的站序比整理台定稿粗，门槛再抬一档：宁可不裁也别裁错。
        hug = _seg_hug(ps, polys)
        need = 0.60 if all(m["confirmed"] for m in metas) else 0.75
        cut = ([c for part in ps for c in _clip_to_seg(part, polys)]
               if hug >= 0.60 and hug >= need else [])
        if cut:
            clip_parts.extend(cut)
            clipped.append(nm)
        else:
            clip_parts.extend(ps)

    def _assemble(src_parts: List[list]) -> Tuple[Optional[List[dict]], dict]:
        parts = _trim_parts(src_parts, path, max_off_km)
        if not parts:
            return None, {"err": "线路几何都离这条车次太远", "lines": names}

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
            run_pts = part[::-1] if k1 < k0 else list(part)
            lo, hi = min(k0, k1), max(k0, k1)
            out, acc = [], 0.0
            for i, (lon, lat) in enumerate(run_pts):
                if i:
                    acc += _hav(run_pts[i - 1], run_pts[i])
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
        fill = 0                                # 用来补缺口的停车站个数
        prev_end: Optional[float] = None
        for r in kept:
            if prev_end is not None and r[0][0] - prev_end > 0.3:
                for q in _between(stop_path, prev_end, r[0][0]):  # 缺口：只按实际停车站直线补
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
            head = _between(stop_path, -0.3, pts[0]["km"])
            pts[:0] = [{"lat": q["lat"], "lon": q["lon"], "km": q["km"]} for q in head]
            pts.insert(0, {"lat": stop_path[0]["lat"], "lon": stop_path[0]["lon"], "km": 0.0})
        if pts[-1]["km"] < total - 0.3:
            pts.extend({"lat": q["lat"], "lon": q["lon"], "km": q["km"]}
                       for q in _between(stop_path, pts[-1]["km"], total))
            pts.append({"lat": stop_path[-1]["lat"], "lon": stop_path[-1]["lon"], "km": total})

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

    pts, info = _assemble(clip_parts)
    # 全局不退化闸：规则对所有车次生效，但任何一趟都不许比「不裁」更差。
    # 裁不裁覆盖率相同则保留裁剪（N17 未裁那版会往满洲里方向西绕一圈）。
    if clipped:
        raw_pts, raw_info = _assemble(base_parts)
        if _clipped_worse(info, pts, raw_info, raw_pts, stop_path):
            pts, info = raw_pts, raw_info
            info["clipped"] = []
        else:
            info["clipped"] = clipped
    else:
        info["clipped"] = []
    return pts, info
