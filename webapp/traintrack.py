# -*- coding: utf-8 -*-
"""
traintrack.py — 列车「行车路线」折线：已整理确定的线路贴真实走向，其余用停站间直线

口径（2026-11-06 用户定）
------------------------
* 「已整理确定」= 库里已经有连续真实走向的线路：
    - `line_base`      整理台「定为基本线路」/ 批量接管的规范线路（rank 0）
    - `line_geom`      导入 / 合并好的走向，有连续 spine 的（rank 1）
    - `line_route`     来源包自己的走向（rg / hdx / cnrail，有连续 spine 的，rank 2）
  只有碎段、串不成连续走向的线不算「整理确定」，不拿来画。
* 逐站间区间独立判定：哪个区间能贴上某条已整理线路，就用那条线路在该区间的
  真实折线；贴不上（没几何 / 站离得太远 / 弧长明显不合理）就老老实实连直线。
  所以一趟车常常是「实线 + 直线」混合，前端会分别画出来并报覆盖率。
* 不读经由（train_override / infer_via）：列车地图只关心画出来的形状，
  按坐标贴线比按自动推断的经由稳，也不依赖车次详情缓存。

几何统一 WGS-84，内部点格式 (lat, lon)，对外返回 [[lat, lon], ...]。
"""
from __future__ import annotations

import gzip
import json
import math
import os
import sqlite3
import threading
import time
from collections import defaultdict

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "rail.db")

# 索引里每条折线最多保留多少点（spine 本身已是抽稀过的骨架）
MAX_LINE_PTS = 1400
# 每个站间区间返回给前端的点数上限（避免一条京沪线把地图接口撑爆）
MAX_PIECE_PTS = 180
# 太短的站间区间贴线收益极小、却最容易贴到旁边岔线上，直接连直线
MIN_REAL_KM = 12.0
# 站坐标与线路几何的允许偏差：短区间给 4 km，长区间按比例放宽，最多 18 km
NEAR_MIN_KM, NEAR_MAX_KM = 4.0, 18.0

_LINES: list | None = None
_LINES_LOCK = threading.RLock()
_STOPS: dict | None = None            # 线名 -> 规范站名集合（用来排掉「贴到隔壁线」）
_TRACK: dict = {}                     # (code, date, 站序指纹) -> 结果
_TRACK_LOCK = threading.Lock()
_TRACK_MAX = 400
_BUILD_INFO: dict = {}


# --------------------------------------------------------------------------
# 基础几何工具
# --------------------------------------------------------------------------
def _hav(a, b) -> float:
    """两点球面距离 km，a/b = (lat, lon)"""
    la1, lo1 = math.radians(a[0]), math.radians(a[1])
    la2, lo2 = math.radians(b[0]), math.radians(b[1])
    dp, dl = la2 - la1, lo2 - lo1
    h = math.sin(dp / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(h)))


def _unzip(blob):
    if not blob:
        return []
    try:
        return json.loads(gzip.decompress(blob).decode("utf-8"))
    except (OSError, ValueError, TypeError, EOFError):
        return []


def _to_ll(arr) -> list:
    """[[lon,lat], ...] → [(lat,lon), ...]，滤掉坏点"""
    out = []
    for p in arr or []:
        if isinstance(p, (list, tuple)) and len(p) >= 2:
            try:
                lo, la = float(p[0]), float(p[1])
            except (TypeError, ValueError):
                continue
            if -180 <= lo <= 180 and -90 <= la <= 90:
                out.append((la, lo))
    return out


def _thin(pts: list, cap: int) -> list:
    """按弧长等距抽稀，端点必留（不用递归 Douglas-Peucker，避免深递归）"""
    n = len(pts)
    if n <= cap or cap < 3:
        return list(pts)
    cum = [0.0]
    for i in range(1, n):
        cum.append(cum[-1] + _hav(pts[i - 1], pts[i]))
    total = cum[-1]
    if total <= 0:
        return [pts[0], pts[-1]]
    step = total / (cap - 1)
    out, target = [pts[0]], step
    for i in range(1, n - 1):
        if cum[i] >= target:
            out.append(pts[i])
            target += step
            if len(out) >= cap - 1:
                break
    out.append(pts[-1])
    return out


def _chain_parts(parts) -> list:
    """没有连续 spine 时的兜底：按端点把碎段贪心接成一条链，取最长的一条。

    段端点通常精确相接（OSM way 切分），用 3 位小数（约 100 m）建端点索引；
    接不上就停，不硬连（硬连会画出横穿城市的假线）。
    """
    segs = [s for s in (_to_ll(x) for x in (parts or [])) if len(s) >= 2]
    if not segs:
        return []
    segs.sort(key=len, reverse=True)
    segs = segs[:600]                    # 只处理最长的 600 段，控制构建耗时
    idx = defaultdict(list)
    for i, s in enumerate(segs):
        idx[(round(s[0][1], 3), round(s[0][0], 3))].append((i, 0))
        idx[(round(s[-1][1], 3), round(s[-1][0], 3))].append((i, 1))
    best: list = []
    for st in range(min(6, len(segs))):
        chain, used = list(segs[st]), {st}
        # 先往尾端接，再往首端接
        for tail in (True, False):
            for _ in range(600):
                end = chain[-1] if tail else chain[0]
                key = (round(end[1], 3), round(end[0], 3))
                nxt = None
                for i, side in idx.get(key, ()):
                    if i not in used:
                        nxt = (i, side)
                        break
                if nxt is None:
                    break
                i, side = nxt
                used.add(i)
                s = segs[i] if side == 0 else segs[i][::-1]
                if tail:
                    chain.extend(s[1:])
                else:
                    chain[:0] = s[:-1][::-1]
        if len(chain) > len(best):
            best = chain
    return best


def _path_of(spine_blob, parts_blob) -> list:
    """一条候选折线：优先串好的 spine，太短才去接碎段"""
    spine = _unzip(spine_blob)
    pts = []
    if (isinstance(spine, list) and spine and isinstance(spine[0], (list, tuple))
            and len(spine[0]) >= 2 and isinstance(spine[0][0], (int, float))):
        pts = _to_ll(spine)
    if len(pts) < 8:
        parts = _unzip(parts_blob)
        if isinstance(parts, list) and parts:
            chained = _chain_parts(parts)
            if len(chained) > len(pts):
                pts = chained
    if len(pts) < 8:
        return []
    return _thin(pts, MAX_LINE_PTS)


# --------------------------------------------------------------------------
# 线路走向索引
# --------------------------------------------------------------------------
class Line:
    __slots__ = ("name", "src", "rank", "pts", "cum", "bbox")

    def __init__(self, name: str, src: str, rank: int, pts: list):
        self.name, self.src, self.rank = name, src, rank
        self.pts = pts
        cum = [0.0]
        for i in range(1, len(pts)):
            cum.append(cum[-1] + _hav(pts[i - 1], pts[i]))
        self.cum = cum
        lats = [p[0] for p in pts]
        lons = [p[1] for p in pts]
        self.bbox = (min(lats), min(lons), max(lats), max(lons))


_ROUTE_LABEL = {"hdx": "OSM 走向", "rg": "rail-globe 走向", "cnrail": "cnrail 走向"}
_ROUTE_PRIORITY = {"hdx": 0, "rg": 1, "cnrail": 2}


def _name_keys(s) -> set:
    """站名比较用的几个写法：原名 + 去掉「站」后缀（「十八站」这种靠白名单保护，
    这里两个写法都放进集合，不会误伤，只是匹配时稍微宽一点）"""
    s = str(s or "").strip()
    out = {s}
    if s.endswith("站") and len(s) > 2:
        out.add(s[:-1])
    return out


def _build_stops() -> dict:
    """线名 → 站名集合。整理台 / 线路库里存过站序的线，用来排掉「贴到隔壁线」。"""
    out = defaultdict(set)
    with sqlite3.connect(DB_PATH, timeout=30) as c:
        for r in c.execute("SELECT b.name, s.name FROM line_base b "
                           "JOIN line_base_stop s ON s.base_id=b.id"):
            out[r[0]].update(_name_keys(r[1]))
        for r in c.execute("SELECT name, stations_json FROM line_custom"):
            try:
                st = json.loads(r[1] or "[]")
            except (ValueError, TypeError):
                continue
            if not isinstance(st, list):
                continue
            for x in st:
                n = x.get("name") if isinstance(x, dict) else x
                if n:
                    out[r[0]].update(_name_keys(n))
        for r in c.execute("SELECT name, stops_json FROM line_route"):
            try:
                st = json.loads(r[1] or "[]")
            except (ValueError, TypeError):
                continue
            if not isinstance(st, list):
                continue
            for x in st:
                n = x.get("name") if isinstance(x, dict) else x
                if n:
                    out[r[0]].update(_name_keys(n))
    return {k: v for k, v in out.items() if len(v) >= 2}


def line_stops() -> dict:
    global _STOPS
    if _STOPS is not None:
        return _STOPS
    with _LINES_LOCK:
        if _STOPS is None:
            try:
                _STOPS = _build_stops()
            except sqlite3.DatabaseError:
                _STOPS = {}
        return _STOPS


def _build_lines() -> list:
    """一次读全库，按「基本线路 > 合并走向 > 来源包」建索引（同名可有多个版本）"""
    with sqlite3.connect(DB_PATH, timeout=30) as c:
        base = {r[0]: (r[1] or "", r[2] or "")
                for r in c.execute("SELECT name,geom_src,geom_name FROM line_base")}
        geoms = {r[0]: (r[1], r[2]) for r in
                 c.execute("SELECT name,spine_gz,parts_gz FROM line_geom")}
        routes = defaultdict(list)
        for r in c.execute("SELECT name,source,spine_gz,parts_gz FROM line_route"):
            routes[r[0]].append((r[1], r[2], r[3]))

    lines: list[Line] = []
    seen: set = set()

    def add(name, src, rank, sp, pa):
        key = (name, src)
        if key in seen:
            return
        seen.add(key)
        pts = _path_of(sp, pa)
        if pts:
            lines.append(Line(name, src, rank, pts))

    # rank 0：整理台定稿 / 全项目接管的基本线路。同名多版本都留着，匹配时挑最贴的。
    for name, (gs, gn) in base.items():
        g = geoms.get(name)
        if g:
            add(name, "基本线路·合并走向", 0, g[0], g[1])
        if gs.startswith("route:"):
            key = gs.split(":", 1)[1].strip()
            for src, sp, pa in routes.get(gn or name, ()):
                if src == key:
                    add(name, f"基本线路·{_ROUTE_LABEL.get(key, key)}", 0, sp, pa)
        # hdx 那份往往点更密，补一个版本提高贴线成功率
        for src, sp, pa in sorted(routes.get(gn or name, ()),
                                  key=lambda x: _ROUTE_PRIORITY.get(x[0], 9))[:1]:
            add(name, f"基本线路·{_ROUTE_LABEL.get(src, src)}", 0, sp, pa)

    # rank 1：不在基本线路里、但已合并好连续走向的线路
    for name, (sp, pa) in geoms.items():
        if name in base:
            continue
        add(name, "合并走向", 1, sp, pa)

    # rank 2：只有来源包走向、且上面没覆盖到的线名，每个名字取一份最完整的
    for name, lst in routes.items():
        if name in base or name in geoms:
            continue
        for src, sp, pa in sorted(lst, key=lambda x: _ROUTE_PRIORITY.get(x[0], 9))[:1]:
            add(name, _ROUTE_LABEL.get(src, src), 2, sp, pa)
    return lines


def lines() -> list:
    global _LINES
    if _LINES is not None:
        return _LINES
    with _LINES_LOCK:
        if _LINES is not None:
            return _LINES
        t0 = time.time()
        got = _build_lines()
        _BUILD_INFO.update({
            "lines": len(got),
            "points": sum(len(x.pts) for x in got),
            "buildSec": round(time.time() - t0, 2),
            "builtAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        _LINES = got
        return _LINES


def invalidate() -> None:
    """库里新导入了走向 / 重新定稿基本线路后调用"""
    global _LINES, _STOPS
    with _LINES_LOCK:
        _LINES = None
        _STOPS = None
    with _TRACK_LOCK:
        _TRACK.clear()


def stats() -> dict:
    return dict(_BUILD_INFO, cached=len(_TRACK))


# --------------------------------------------------------------------------
# 逐区间贴线
# --------------------------------------------------------------------------
def _nearest(L: Line, lat: float, lon: float):
    """站内最快的近似距离（平面化，不开方），返回 (点下标, 距离 km)"""
    k = math.cos(math.radians(lat)) * 111.32
    best_i, best_d2 = 0, 1e30
    for i, (la, lo) in enumerate(L.pts):
        dx, dy = (lo - lon) * k, (la - lat) * 110.57
        d2 = dx * dx + dy * dy
        if d2 < best_d2:
            best_d2, best_i = d2, i
    return best_i, math.sqrt(best_d2)


def _match_segment(a, b, na: str, nb: str, Ls: list, stops_idx: dict) -> dict | None:
    """找一条能覆盖 A→B 的已整理线路，返回该区间的真实折线"""
    d = _hav(a, b)
    if d < MIN_REAL_KM:
        return None
    ka, kb = _name_keys(na), _name_keys(nb)
    tol = min(NEAR_MAX_KM, max(NEAR_MIN_KM, d * 0.07))
    # 「合并走向」的 spine 偶尔会把不相连的碎段用一根大直线连上（实测有 90~150 km
    # 的假跳变）。贴线区间内不允许有这种跳点，宁可退回停站间直线。
    jump_limit = min(30.0, max(10.0, d * 0.5))
    pad = 0.22
    lo1, la1 = min(a[1], b[1]) - pad, min(a[0], b[0]) - pad
    lo2, la2 = max(a[1], b[1]) + pad, max(a[0], b[0]) + pad
    best = None
    for L in Ls:
        m1, n1, m2, n2 = L.bbox
        if m2 < la1 or m1 > la2 or n2 < lo1 or n1 > lo2:
            continue
        # 这条线在整理库里存过站序，却一个都不认得 → 多半是贴到旁边的岔线了
        names = stops_idx.get(L.name)
        if names and not ((ka | kb) & names):
            continue
        ia, da = _nearest(L, *a)
        ib, db = _nearest(L, *b)
        if da > tol or db > tol or abs(ib - ia) < 2:
            continue
        arc = abs(L.cum[ib] - L.cum[ia])
        ratio = arc / max(d, 0.2)
        # 弧长明显短于直线（贴错线）或绕得太离谱（取到整条线）都不要
        if ratio < 0.65 or ratio > 2.6 or arc > d * 2.6 + 40:
            continue
        lo_i, hi_i = min(ia, ib), max(ia, ib)
        mx = 0.0
        for k in range(lo_i + 1, hi_i + 1):
            j = _hav(L.pts[k - 1], L.pts[k])
            if j > mx:
                mx = j
                if mx > jump_limit:
                    break
        if mx > jump_limit:
            continue
        score = L.rank * 3.0 + (da + db) / 2 + abs(ratio - 1.15) * 2
        if best is None or score < best[0]:
            best = (score, L, ia, ib, arc)
    if best is None:
        return None
    _, L, ia, ib, arc = best
    sub = L.pts[ia:ib + 1] if ib > ia else L.pts[ib:ia + 1][::-1]
    sub = _thin(sub, MAX_PIECE_PTS)
    pts = [a] + sub[1:-1] + [b] if len(sub) >= 2 else [a, b]
    return {"line": L.name, "src": L.src, "km": round(arc, 1), "pts": pts}


def track(code: str, date: str = "", t=None, detail: dict | None = None) -> dict:
    """一趟车的行车路线折线：能贴真实走向的区间贴线，其余停站间直线"""
    import trainmap

    d = detail if detail is not None else trainmap.train_detail(code, date, t)
    if d.get("error"):
        return {"ok": False, "error": d["error"], "code": code, "date": d.get("date", "")}
    stops = [s for s in (d.get("stops") or [])
             if s.get("lat") is not None and s.get("lon") is not None]
    code, date = d.get("code", code), d.get("date", date)
    fp = (code, date, len(stops), stops[0]["name"] if stops else "",
          stops[-1]["name"] if stops else "")
    with _TRACK_LOCK:
        hit = _TRACK.get(fp)
    if hit:
        return hit

    t0 = time.time()
    Ls = lines()
    stops_idx = line_stops()
    pieces, real_km, straight_km, used = [], 0.0, 0.0, []
    for i in range(len(stops) - 1):
        a = (stops[i]["lat"], stops[i]["lon"])
        b = (stops[i + 1]["lat"], stops[i + 1]["lon"])
        km = _hav(a, b)
        got = _match_segment(a, b, stops[i]["name"], stops[i + 1]["name"],
                             Ls, stops_idx)
        if got:
            pieces.append({"mode": "real", "from": stops[i]["name"],
                           "to": stops[i + 1]["name"], **got})
            real_km += km
            if got["line"] not in used:
                used.append(got["line"])
        else:
            pieces.append({"mode": "straight", "from": stops[i]["name"],
                           "to": stops[i + 1]["name"], "line": "", "src": "",
                           "km": round(km, 1), "pts": [a, b]})
            straight_km += km

    # 整条 path（相邻 piece 共享端点，去掉重复的那个）
    path: list = []
    for p in pieces:
        pts = p["pts"]
        path.extend(pts[1:] if path and path[-1] == pts[0] else pts)
    real = sum(1 for p in pieces if p["mode"] == "real")
    total = len(pieces)
    out = {
        "ok": True, "code": code, "date": date,
        "pieces": pieces, "path": [[round(x, 5), round(y, 5)] for x, y in path],
        "nStops": len(stops), "nPts": len(path),
        "realSegs": real, "straightSegs": total - real, "totalSegs": total,
        "realKm": round(real_km, 1), "straightKm": round(straight_km, 1),
        "coverage": round(real / total, 3) if total else 0.0,
        "lines": used,
        "scope": {"lines": len(Ls), "points": _BUILD_INFO.get("points", 0)},
        "elapsed": round(time.time() - t0, 3),
    }
    with _TRACK_LOCK:
        if len(_TRACK) > _TRACK_MAX:
            _TRACK.clear()
        _TRACK[fp] = out
    return out
