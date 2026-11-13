# -*- coding: utf-8 -*-
"""
routeshape.py — 车次详情「按真实走向」**用户口径画法**（2026-11-12）

用户要求（逐条对应下面代码）：

1. 所有线路和车站都以**整理台已确认的线路 / 停站次序**为准，也就是
   「车站/线路」页能查到的那份（`confirmed.detail_payload()` 的 `stations`，
   带累计 `km`）；公里数同样按站序一段一段累加，不再按「站点直连的直线
   走廊」投影估算。站序解析统一复用 `cnrail._via_chain()`（确认站序优先、
   线路库含「（来源）」后缀版本兜底、线路所按坐标就近落点），与
   `path_stations()` / `railosm` 同源。
2. 确认线路本身可能有毛病，四条补救原则：
   · **跨线中转全部在中转站转线**：每段独立拼装，段首/段末强制落在
     该段的 from/to 站坐标上；相邻段只在中转站首尾相接，绝不跨段贪心拼。
   · **线段断裂自动连接最近两点**：一条确认线路的几何是多段乱序碎段
     （京广线 28 段），按站序投影排序后，相邻段取最近的一对端点直接连。
   · **长出起讫点的截至起点终点**：几何按站序投影裁到 from→to 区间，
     区间外的点一律丢掉。
   · **不足覆盖起讫点的用最初/最终点连接**：段折线首点离起点远就把
     起点插到最前，末点离终点远就把终点追加到最后。
3. **上下行分线只选一条**：投影区间重叠的多段视为分线候选，优先选
   「包含本趟车停站」的那条（停站坐标贴近该段），否则选最贴站序 / 最长的。

几何来源没改：仍走 `railosm._geom_of()` 的三级顺序（整理台定稿 →
主站共用 `lineosm.geom()` → 老 OSM 包），**没提及的部分沿用原方式**：
无经由段 / 段完全拼不出时退回停站直连；`info` 字段与旧画法保持同名，
前端 `geoNoteText()` 不必改。

旧画法 `railosm.build()` 原封不动保留，`railcore.route_osm()` 用 `mode`
分流（默认 `confirmed` = 本文件，`legacy` = 旧画法），随时可以切回去。
"""
from __future__ import annotations

import math
import time
from typing import List, Optional, Sequence, Tuple

import railosm

#: 几何段与「本段确认站序折线」的贴合阈值（km）：中位偏离超过它就认为这段
#: 是这条线别的区间（京广线里在广州的那些段），整段丢掉 —— 即「长出截至」。
PART_OFF = 12.0
#: 逐点裁切阈值：站序折线本身也可能略有偏差，比 PART_OFF 稍宽
CLIP_OFF = 16.0
#: 断裂自动连接允许的最大跨距；超过就如实记成缺口，不硬连出一条假长线
LINK_MAX = 80.0
#: 段端点与站序起讫的对齐容差（km）
END_TOL = 0.6
#: 缓存秒数（与 railosm._seg_polyline 一致；改过库由 railcore 的 epoch 兜底）
CACHE_TTL = 20.0

_SEG_CACHE: dict = {}
_SEG_TS = 0.0


def _hav(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    """两点球面距离（km），a/b = (lat, lon)"""
    return railosm._hav(a, b)


def _dist(p: Tuple[float, float], q: Tuple[float, float]) -> float:
    """(lon,lat) 两点的 km"""
    return _hav((p[1], p[0]), (q[1], q[0]))


def _key(name: str) -> str:
    """站名比对键：全项目统一走 cnrail._key（线路所 / 去「站」后缀口径）"""
    import cnrail
    return cnrail._key(name)


def _poly_cum(poly: Sequence[Tuple[float, float]]) -> List[float]:
    cum = [0.0]
    for i in range(1, len(poly)):
        cum.append(cum[-1] + _dist(poly[i - 1], poly[i]))
    return cum


def _proj(poly: Sequence[Tuple[float, float]], cum: Sequence[float],
          p: Tuple[float, float]) -> Tuple[float, float]:
    """点到站序折线的 (沿折线弧长 km, 偏离 km)。

    等距近似（不做球面三角），用于排序 / 裁切足够；长折线逐点扫描单次
    只是几个乘加，比另建网格索引更省心。
    """
    best_s, best_d2 = 0.0, 1e36
    lat0 = p[1]
    kx = 111.32 * max(0.2, math.cos(math.radians(lat0)))
    ky = 111.32
    for i in range(1, len(poly)):
        ax, ay = poly[i - 1]
        bx, by = poly[i]
        dx, dy = (bx - ax) * kx, (by - ay) * ky
        l2 = dx * dx + dy * dy
        px, py = (p[0] - ax) * kx, (p[1] - ay) * ky
        t = 0.0 if l2 <= 0 else max(0.0, min(1.0, (px * dx + py * dy) / l2))
        ex, ey = px - t * dx, py - t * dy
        d2 = ex * ex + ey * ey
        if d2 < best_d2:
            best_d2 = d2
            best_s = cum[i - 1] + (cum[i] - cum[i - 1]) * t
    return best_s, math.sqrt(best_d2)


# ---------------------------------------------------------------------------
# 段数据：确认线路站序（含累计 km）+ 起讫站坐标 + 几何碎段
# ---------------------------------------------------------------------------
def _seg_chain(seg: dict) -> Optional[dict]:
    """经由段 → 该段的确认线路站序。

    直接复用 `cnrail._via_chain()`：整理台「全部确认」站序优先，线路库
    （含「（来源）」后缀版本）兜底，端点认不出时按坐标就近落点。返回的
    stations 自带线路累计 km —— 这就是「公里数一样累加」的基准。
    """
    import cnrail
    chain = cnrail._via_chain([{
        "line": seg.get("line") or "",
        "from": seg.get("from") or "",
        "to": seg.get("to") or "",
    }])
    if not chain:
        return None
    label, st, pf, pt = chain[0]
    step = 1 if pt >= pf else -1
    seq = [dict(x) for x in st[pf:pt + step:step]]
    pts = []
    for s in seq:
        lat, lon = s.get("lat"), s.get("lon")
        if lat is None or lon is None:
            c = cnrail.coord(s.get("name") or "")
            if not c:
                continue
            lat, lon = c
        q = (float(lon), float(lat))
        if not pts or q != pts[-1]:
            pts.append(q)
    if len(pts) < 2:
        return None
    return {"label": label, "confirmed": "整理台" in label,
            "stations": seq, "poly": pts}


def _coord_of(name: str, stops: Sequence[dict],
              chain: Optional[dict], at_start: bool) -> Optional[Tuple[float, float]]:
    """段端点坐标：车次停站优先（车次口径），其次确认站序端点，最后站表。

    车次写「太原」而线路站序写「太原南」时，靠 `cnrail.coord()` 补；
    段首/段末强制用这份坐标，保证「跨线全部在中转站转线」。
    """
    k = _key(name)
    for s in stops or []:
        if _key(s.get("name") or "") == k and s.get("lat") is not None:
            return (float(s["lon"]), float(s["lat"]))
    if chain and chain["poly"]:
        seq = chain["stations"]
        s = seq[0] if at_start else seq[-1]
        if s.get("lat") is not None and s.get("lon") is not None:
            return (float(s["lon"]), float(s["lat"]))
    try:
        import cnrail
        c = cnrail.coord(name)
        if c:
            return (float(c[1]), float(c[0]))      # coord() = (lat, lon)
    except Exception:                              # noqa: BLE001
        pass
    return None


def _anchor_km(name: str, stops: Sequence[dict]) -> Optional[float]:
    """这个站在车次停站里的累计 km（中转站与车次显示里程对齐用）"""
    k = _key(name)
    for s in stops or []:
        if _key(s.get("name") or "") == k:
            try:
                return float(s.get("km") or 0)
            except (TypeError, ValueError):
                return None
    return None


def _km_span(chain: Optional[dict], poly: Sequence[Tuple[float, float]]) -> float:
    """本段在线路站序里的里程跨度；站序缺 km 时退化为站序折线弧长"""
    if chain:
        seq = chain["stations"]
        a, b = seq[0].get("km"), seq[-1].get("km")
        try:
            if a is not None and b is not None and abs(float(b) - float(a)) > 0.5:
                return abs(float(b) - float(a))
        except (TypeError, ValueError):
            pass
    return sum(_dist(poly[i - 1], poly[i]) for i in range(1, len(poly)))


# ---------------------------------------------------------------------------
# 规则 2c：按站序投影把整条线的碎段裁到 from→to；规则 3：分线只选一条
# ---------------------------------------------------------------------------
def _seg_candidates(line: str, poly: Sequence[Tuple[float, float]],
                    cum: Sequence[float], span: float):
    """把几何 parts 逐点投影到本段站序，返回可串接的候选子段。

    每个候选 = `{"pts": [(lon,lat)], "lo": 起点 s, "hi": 终点 s,
                 "hug": 中位偏离,""}`。偏离大的整段丢弃（那些是这条线
    别的区间），区间外的点裁掉（长出截至起点终点）。
    """
    g = railosm._geom_of(line)
    if not (g and g.get("parts")):
        return []
    out: List[dict] = []
    tol = min(20.0, max(2.0, span * 0.05))
    for raw in g["parts"]:
        part = [(float(q[0]), float(q[1])) for q in raw
                if isinstance(q, (list, tuple)) and len(q) >= 2]
        if len(part) < 2:
            continue
        # 抽样只用来给「贴合度」打分，不做早退：一条长段常常一半在本段、
        # 一半伸到线外（平齐线 720 点里只有进齐齐哈尔那截是本趟走的），
        # 早退会把整段丢掉。逐点裁才能留下贴着站序的那一部分。
        step = max(1, len(part) // 40)
        sample = [part[i] for i in range(0, len(part), step)]
        ds = sorted(_proj(poly, cum, q)[1] for q in sample)
        med = ds[len(ds) // 2]
        cur: List[Tuple[float, float]] = []
        ss: List[float] = []
        for q in part:
            s, d = _proj(poly, cum, q)
            if d <= CLIP_OFF and -tol <= s <= span + tol:
                cur.append(q)
                ss.append(s)
            else:
                if len(cur) >= 2:
                    out.append(_mk_cand(cur, ss, med))
                cur, ss = [], []
        if len(cur) >= 2:
            out.append(_mk_cand(cur, ss, med))
    return [c for c in out if c["hug"] <= PART_OFF * 3
            or c["span"] >= max(2.0, span * 0.02)]


def _mk_cand(pts: List[Tuple[float, float]], ss: List[float], hug: float) -> dict:
    # 按运行方向摆正：站序投影递增
    if ss[0] > ss[-1]:
        pts, ss = pts[::-1], ss[::-1]
    return {"pts": pts, "lo": min(ss), "hi": max(ss), "hug": hug,
            "span": max(0.1, max(ss) - min(ss))}


def _stop_hits(cand: dict, seg_stops: Sequence[Tuple[float, float]]) -> int:
    """规则 3：这条分线上「包含」本趟车多少个停站（3 km 内算命中）"""
    pts = cand["pts"]
    step = max(1, len(pts) // 200)
    thin = pts[::step]
    if pts[-1] not in thin:
        thin = thin + [pts[-1]]
    hit = 0
    for s in seg_stops:
        d = min(_dist(s, q) for q in thin)
        if d <= 3.0:
            hit += 1
    return hit


def _pick_branches(cands: List[dict], seg_stops: Sequence[Tuple[float, float]],
                   span: float) -> List[dict]:
    """上下行分线只留一条：投影区间重叠 >60% 视为同一组分线候选。

    优先级严格按用户口径：① **包含本趟车停站**最多的那条；② 覆盖本段
    站序区间比例最大的那条（长段里的小碎段 / 岔线别把主体挤掉 —— 太中线
    898 点的主段曾被 8 点小段挤掉，画成 551 km 直线）；③ 贴合站序折线
    最好；④ 点最多。
    """
    if not cands:
        return []
    cands = sorted(cands, key=lambda c: (c["lo"], -c["span"]))
    groups: List[List[dict]] = []
    for c in cands:
        placed = False
        for grp in groups:
            ref = grp[0]
            a0, a1, b0, b1 = ref["lo"], ref["hi"], c["lo"], c["hi"]
            overlap = max(0.0, min(a1, b1) - max(a0, b0))
            denom = max(0.1, min(a1 - a0, b1 - b0))
            if overlap / denom > 0.60:
                grp.append(c)
                placed = True
                break
        if not placed:
            groups.append([c])
    picked = []
    for grp in groups:
        scored = [(_stop_hits(c, seg_stops),
                   round(min(1.0, c["span"] / max(1.0, span)), 4),
                   -c["hug"], len(c["pts"]), i)
                  for i, c in enumerate(grp)]
        picked.append(grp[max(scored)[4]])
    return sorted(picked, key=lambda c: c["lo"])


def _poly_between(poly: Sequence[Tuple[float, float]], cum: Sequence[float],
                  a: float, b: float) -> List[Tuple[float, float]]:
    """站序折线上 (a, b) 弧长区间内的点 —— 大断口按确认站序补，不画假长直线"""
    if b <= a + 0.5:
        return []
    return [poly[i] for i in range(len(poly))
            if a + 0.3 < cum[i] < b - 0.3]


def _chain_parts(picked: List[dict], poly: Sequence[Tuple[float, float]],
                 cum: Sequence[float]) -> Tuple[List[Tuple[float, float]], List[float]]:
    """规则 2b：按站序顺序串接碎段，相邻段自动连接**最近的一对端点**。

    断口很小就直接连（画出来是那条直线）；断口超过 LINK_MAX 说明中间整截
    几何缺失，按规则 1 用**确认线路站序点**补上，而不是画一根几百公里的
    假线（京哈线秦皇岛→沈阳北曾因此出现 376 km 直线）。
    """
    pts: List[Tuple[float, float]] = []
    links: List[float] = []
    broken = 0.0
    cur_s = 0.0
    for c in picked:
        part = list(c["pts"])
        if pts:
            d_head = _dist(pts[-1], part[0])
            d_tail = _dist(pts[-1], part[-1])
            if d_tail < d_head:
                part = part[::-1]                       # 最近的那一点在末端
            d = min(d_head, d_tail)
            links.append(d)
            if d > LINK_MAX:
                broken += d
                pts.extend(_poly_between(poly, cum, cur_s, c["lo"]))
            if part and pts[-1] == part[0]:
                part = part[1:]
        pts.extend(part)
        cur_s = c["hi"]
    return pts, links, broken


# ---------------------------------------------------------------------------
# 单段：返回 (lat,lon,km) 点列 + 元信息
# ---------------------------------------------------------------------------
def _legacy_slice(legacy: Sequence[dict], k0: float,
                  k1: float) -> List[Tuple[float, float]]:
    """旧画法里落在这段里程区间的几何点 —— 「没提及的按原来的方式继续」"""
    return [(float(p["lon"]), float(p["lat"])) for p in (legacy or [])
            if p.get("lat") is not None and p.get("lon") is not None
            and k0 - 0.5 <= float(p.get("km") or 0) <= k1 + 0.5]


def _seg_points(seg: dict, route: dict, km_cursor: float,
                legacy: Optional[Sequence[dict]] = None) -> Tuple[List[dict], dict]:
    """一段经由线路的全部画线点（首尾严格落在中转站上）。

    段本身是**整理台确认线路且有几何**才用新规则；非确认线路段沿用旧画法
    在这一段的几何（`legacy`）—— 用户只规定了确认线路怎么画，底表线 /
    来源版本线没提及，按原来的方式继续。
    """
    stops = route.get("stops") or []
    line = seg.get("line") or ""
    frm, to = seg.get("from") or "", seg.get("to") or ""
    chain = _seg_chain(seg)
    start = _coord_of(frm, stops, chain, True)
    end = _coord_of(to, stops, chain, False)
    meta = {"line": line, "from": frm, "to": to,
            "confirmed": bool(chain and chain["confirmed"]),
            "source": "geom", "links": 0, "linkKm": 0.0, "gapKm": 0.0,
            "kmStart": km_cursor, "kmEnd": km_cursor}
    if start is None or end is None:
        meta["source"] = "straight"
        meta["kmEnd"] = km_cursor + _guess_span(stops, frm, to)
        return _straight(start, end, km_cursor, meta["kmEnd"]), meta

    poly = chain["poly"] if chain else [start, end]
    cum = _poly_cum(poly)
    span_s = cum[-1] if cum[-1] > 0.1 else _dist(start, end)
    # 车次停站锚点：中转站对齐车次自己的累计 km，段内按线路 km 累加
    k0 = _anchor_km(frm, stops)
    k1 = _anchor_km(to, stops)
    line_span = _km_span(chain, poly) or span_s
    km_start = k0 if k0 is not None else km_cursor
    km_end = k1 if k1 is not None else km_start + line_span
    if km_end <= km_start:
        km_end = km_start + max(1.0, line_span)
    meta["kmStart"], meta["kmEnd"] = km_start, km_end

    # 本段内车次停站（分线「含停站」判定用）
    seg_stops = [(float(s["lon"]), float(s["lat"])) for s in stops
                 if s.get("lat") is not None
                 and km_start - 1 <= float(s.get("km") or 0) <= km_end + 1]

    cands = _seg_candidates(line, poly, cum, span_s)
    picked = _pick_branches(cands, seg_stops, span_s)
    chain_pts, links, broken = _chain_parts(picked, poly, cum)

    # 一个候选都没留下（站序坐标不可靠 / 这条线没有几何）：
    # 退回「按停站次序」的站序折线 —— 仍是规则 1 的口径，不是站点直连
    if len(chain_pts) < 2:
        chain_pts = list(poly)
        meta["source"] = "stations" if chain else "straight"
    else:
        meta["links"] = len(links)
        meta["linkKm"] = round(sum(x for x in links if x <= LINK_MAX), 1)
    meta["gapKm"] = round(broken, 1)

    # 「没提及的按原来的方式继续」：非确认线路 / 没有几何的段，取旧画法在
    # 同一里程区间的点。这样 G1 那种全底表线的车次不会硬按确认线路拼。
    if legacy and not (meta["confirmed"] and meta["source"] == "geom"):
        sl = _legacy_slice(legacy, km_start, km_end)
        if len(sl) >= 2:
            chain_pts = sl
            meta["source"] = "legacy"
            # 这一整段已经换成旧画法的点，新画法自己数出来的断点不能算进去
            meta["links"] = 0
            meta["linkKm"] = 0.0
            meta["gapKm"] = 0.0

    # 规则 2c/d：「长出截至起点终点」已经由 _seg_candidates 的站序投影
    # 区间裁掉了（s 只留 from→to）；这里只做「不足用最初/最终点连接起讫」。
    # 别再按「离起讫最近的点」二次截取 —— 那会把折线中段整截裁掉。
    if not chain_pts:
        chain_pts = [start, end]
    if _dist(chain_pts[0], start) > END_TOL:
        chain_pts.insert(0, start)
    else:
        chain_pts[0] = start
    if _dist(chain_pts[-1], end) > END_TOL:
        chain_pts.append(end)
    else:
        chain_pts[-1] = end

    # 规则 1：km 按站序一段一段累加（段内按沿折线弧长比例落位）
    return _remap_km(chain_pts, km_start, km_end), meta


def _remap_km(pts: List[Tuple[float, float]], k0: float, k1: float) -> List[dict]:
    """把 (lon,lat) 点列按弧长比例映射到 [k0, k1] 的累计里程"""
    out: List[dict] = []
    acc = 0.0
    total_len = sum(_dist(pts[i - 1], pts[i]) for i in range(1, len(pts)))
    for i, q in enumerate(pts):
        if i:
            acc += _dist(pts[i - 1], q)
        ratio = acc / total_len if total_len > 0.01 else i / max(1, len(pts) - 1)
        out.append({"lat": q[1], "lon": q[0],
                    "km": round(k0 + (k1 - k0) * ratio, 3)})
    return out


def _guess_span(stops: Sequence[dict], frm: str, to: str) -> float:
    a, b = _anchor_km(frm, stops), _anchor_km(to, stops)
    if a is not None and b is not None and b > a:
        return b - a
    return 30.0


def _straight(start, end, k0, k1):
    if start is None or end is None:
        return []
    return [{"lat": start[1], "lon": start[0], "km": round(k0, 3)},
            {"lat": end[1], "lon": end[0], "km": round(k1, 3)}]


def _trim_ends(pts: List[Tuple[float, float]], start,
               end) -> List[Tuple[float, float]]:
    """长出起讫点的部分截至起点终点：取折线上离起/终点最近的两个位置，
    只留中间那一截；方向不对就整体反转。"""
    if len(pts) < 2:
        return list(pts)
    ia = min(range(len(pts)), key=lambda i: _dist(pts[i], start))
    ib = min(range(len(pts)), key=lambda i: _dist(pts[i], end))
    if ib < ia:
        pts, ia, ib = pts[::-1], len(pts) - 1 - ia, len(pts) - 1 - ib
    core = pts[ia:ib + 1]
    return core if len(core) >= 2 else list(pts)


# ---------------------------------------------------------------------------
# 对外：整趟车
# ---------------------------------------------------------------------------
def build(route: dict, legacy: Optional[Sequence[dict]] = None
          ) -> Tuple[Optional[List[dict]], dict]:
    """按用户口径拼出「真实走向」折线，返回 `(pts, info)`，形状与旧画法一致。

    `legacy` 是旧画法 `railosm.build()` 的结果：非确认线路段、经由段之间的
    空隙，都从它里面按里程区间取点 —— **没提及的按原来的方式继续**。
    整趟车没有任何确认线路段时返回 None，调用方直接退回旧画法。
    """
    global _SEG_TS
    now = time.time()
    if now - _SEG_TS > CACHE_TTL:
        _SEG_CACHE.clear()
        _SEG_TS = now

    segs = [s for s in (route.get("viaSegs") or []) if (s or {}).get("line")]
    if not segs:
        return None, {"err": "这趟车没有经由线路，沿用原画法"}
    try:
        import confirmed
        conf = confirmed.names()
    except Exception:                                   # noqa: BLE001
        conf = set()
    if legacy and not any((s.get("line") or "") in conf for s in segs):
        return None, {"err": "这趟车没有整理台确认线路，沿用原画法"}
    stops = route.get("stops") or []
    # 总里程以车次自己的显示口径为准：`path` 末点 km 是线路里程修正后的
    # 展开折线，个别车次（N1534）会比 totalKm 长一截，不能拿它当全程。
    total = float(route.get("totalKm") or 0)
    if total <= 0:
        total = float((route.get("path") or [{}])[-1].get("km") or 0)

    out: List[dict] = []
    metas: List[dict] = []
    km_cursor = 0.0
    prev_to = None
    for seg in segs:
        # 规则 2a：相邻段若不是同一个中转站，中间那截是经由空隙 ——
        # 沿用旧画法在这一段的几何，实在没有才退到停站直连。
        if prev_to and _key(prev_to) != _key(seg.get("from") or ""):
            gap = _fill_gap(stops, prev_to, seg.get("from") or "", km_cursor, legacy)
            for p in gap:
                if not out or p["km"] > out[-1]["km"] + 1e-6:
                    out.append(p)
            if gap:
                km_cursor = gap[-1]["km"]
        pts, meta = _seg_points(seg, route, km_cursor, legacy)
        if not pts:
            meta["source"] = "straight"
            pts = _straight(
                _coord_of(seg.get("from") or "", stops, None, True),
                _coord_of(seg.get("to") or "", stops, None, False),
                km_cursor, km_cursor + _guess_span(stops, seg.get("from") or "",
                                                   seg.get("to") or ""))
        # 中转站严格首尾相接：上一段末点就是这一段首点，重复点丢掉
        if out and pts:
            a, b = out[-1], pts[0]
            if abs(a["km"] - b["km"]) < 0.01 and _dist(
                    (a["lon"], a["lat"]), (b["lon"], b["lat"])) < 1.0:
                pts = pts[1:]
        out.extend(pts)
        if pts:
            km_cursor = pts[-1]["km"]
        metas.append(meta)
        prev_to = seg.get("to") or ""

    if len(out) < 2:
        return None, {"err": "按确认线路拼不出折线，沿用原画法"}

    # 规则 2d 的整趟版：经由段没盖到的首 / 尾，优先沿用旧画法在同一区间的
    # 几何，别只从末点拉一根上百公里的直线到终到站（D40 保定→北京西）。
    # 旧画法也没有几何时，才按用户说的「以最初/最终点连接起讫」直连。
    first, last = stops[0] if stops else None, stops[-1] if stops else None
    if legacy and first and first.get("lat") is not None and out[0]["km"] > 1.0:
        k_first = out[0]["km"]
        head = _legacy_slice(legacy, 0, k_first)
        if len(head) >= 2:
            hp = _remap_km([(float(first["lon"]), float(first["lat"]))] + head,
                           0.0, k_first)
            out = hp + out[1:]
    if legacy and last and last.get("lat") is not None \
            and total - out[-1]["km"] > 1.0:
        k_last = out[-1]["km"]
        tail = _legacy_slice(legacy, k_last, total)
        if len(tail) >= 2:
            tp = _remap_km(tail + [(float(last["lon"]), float(last["lat"]))],
                           k_last, total)
            out = out + tp[1:]

    # 段 km 按线路里程累加时，个别中转站认不出会让累计膨胀（N1534 到
    # 1059 km、车次只有 793 km）。整体线性压回 [0, totalKm]，各段相对
    # 长度与单调性不变，且与车次显示的全程对齐。
    hi = out[-1]["km"]
    if total > 0 and hi > total * 1.001:
        f = total / hi
        for p in out:
            p["km"] = round(p["km"] * f, 3)

    # 首尾对齐车次始发 / 终到（规则 2d 的整趟版）
    if first and first.get("lat") is not None:
        out[0] = {"lat": first["lat"], "lon": first["lon"], "km": 0.0}
    if last and last.get("lat") is not None and total > 0:
        out[-1] = {"lat": last["lat"], "lon": last["lon"], "km": round(total, 3)}

    # 里程单调递增（站序累加可能被锚点微调打乱，统一拉平）
    for i in range(1, len(out)):
        if out[i]["km"] < out[i - 1]["km"]:
            out[i]["km"] = out[i - 1]["km"]
    # 去重
    clean: List[dict] = []
    for p in out:
        q = clean[-1] if clean else None
        if q and abs(p["km"] - q["km"]) < 1e-6 \
                and abs(p["lat"] - q["lat"]) < 1e-9 and abs(p["lon"] - q["lon"]) < 1e-9:
            continue
        clean.append(p)
    out = clean
    if len(out) < 2:
        return None, {"err": "按确认线路拼不出折线，沿用原画法"}

    gap_km = round(sum(m["gapKm"] for m in metas) + sum(m["linkKm"] for m in metas), 1)
    link_km = round(sum(m["linkKm"] for m in metas), 1)
    gaps = []
    cursor = 0.0
    for i, m in enumerate(metas):
        g = m["gapKm"] + m["linkKm"]
        if g > 0.1:
            # kind=link：确认线路碎段之间的断口，规则 2b 用最近两点自动接上；
            # kind=gap：这段线压根没有几何 / 不是确认线路，按原方式走站间直线。
            # 分开报是为了前端文案不说反（断口连接不是「缺口」）。
            gaps.append({"from": round(cursor, 1), "to": round(cursor + g, 1),
                         "km": round(g, 1),
                         "kind": "link" if m["linkKm"] >= m["gapKm"] else "gap"})
        cursor += g
    covered = max(0.0, total - gap_km)
    info = {
        "lines": list(dict.fromkeys(m["line"] for m in metas)),
        "mode": "confirmed",
        "parts": sum(1 for m in metas if m["source"] == "geom"),
        "segments": len(segs),
        "total": round(total, 1),
        "covered": round(covered, 1),
        "gapKm": gap_km,
        "cover": round(covered / total, 3) if total > 0 else 0,
        "gaps": gaps,
        "fill": 0,
        "from": round(out[0]["km"], 1),
        "to": round(out[-1]["km"], 1),
        "confirmedSegs": sum(1 for m in metas if m["confirmed"]),
        "shapeSegs": sum(1 for m in metas if m["source"] == "geom"),
        "legacySegs": sum(1 for m in metas if m["source"] == "legacy"),
        "nonConfirmedSegs": sum(1 for m in metas if not m["confirmed"]),
        "links": sum(m["links"] for m in metas),
        "linkKm": link_km,
        "segInfo": [{"line": m["line"], "from": m["from"], "to": m["to"],
                     "confirmed": m["confirmed"], "source": m["source"],
                     "links": m["links"], "linkKm": m["linkKm"]} for m in metas],
    }
    return out, info


def _fill_gap(stops: Sequence[dict], a: str, b: str, km_cursor: float,
              legacy: Optional[Sequence[dict]]) -> List[dict]:
    """经由段之间的空隙：优先取旧画法在同一里程区间的几何（原方式）。

    两端仍强制落在中转站坐标上，保证「跨线全部在中转站转线」不被空隙破坏。
    """
    k1 = _anchor_km(b, stops)
    ca = _coord_of(a, stops, None, True)
    cb = _coord_of(b, stops, None, False)
    if k1 is not None and k1 > km_cursor and ca and cb:
        sl = _legacy_slice(legacy, km_cursor, k1) if legacy else []
        if len(sl) >= 2:
            return _remap_km([ca] + sl + [cb], km_cursor, k1)
    return _missing_stops(stops, a, b, km_cursor)


def _missing_stops(stops: Sequence[dict], a: str, b: str,
                   km_cursor: float) -> List[dict]:
    """两段经由之间没接上的部分：按车次停站直连补（沿用原方式）"""
    ka, kb = _key(a), _key(b)
    ia = next((i for i, s in enumerate(stops) if _key(s.get("name") or "") == ka), None)
    ib = next((i for i, s in enumerate(stops) if _key(s.get("name") or "") == kb), None)
    if ia is None or ib is None or ib <= ia:
        return []
    out = []
    for s in stops[ia:ib + 1]:
        if s.get("lat") is None:
            continue
        km = float(s.get("km") or km_cursor)
        out.append({"lat": s["lat"], "lon": s["lon"], "km": round(max(km, km_cursor), 3)})
    return out
