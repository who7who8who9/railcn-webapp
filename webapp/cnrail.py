# -*- coding: utf-8 -*-
"""
cnrail_data_v2 静态知识库（由 D:/railcn/tools/xlsx_to_json.py 生成）

提供：
  station(name)        车站档案（所属路局 / 电报码 / 连接线路 / 服务类型 / 坐标）
  coord(name)          (lat, lon)，查不到返回 None
  lines_of(name)       该站连接的线路名列表
  search_line(q)       按名称搜铁路线
  line(name)           线路档案（含途经站点与累计里程）
  refine_km(stops)     用线路里程表修正一串站点的累计里程，并给出经由线路
"""
from __future__ import annotations

import json
import os
import re
import statistics
from typing import Dict, List, Optional, Sequence, Tuple

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SEARCH_DIRS = [
    os.path.join(os.path.dirname(BASE_DIR), "data"),   # D:/railcn/data
    os.path.join(BASE_DIR, "data"),                      # D:/railcn/webapp/data
]

_STATIONS: Optional[Dict[str, dict]] = None
_LINES: Optional[List[dict]] = None
_BY_LINE: Optional[Dict[str, dict]] = None
_POS: Optional[Dict[str, List[Tuple[int, int, float]]]] = None   # 站名 -> [(线路序号, 站位, 里程)]
_POSALL: Optional[Dict[str, List[Tuple[int, int]]]] = None       # 站名 -> [(线路序号, 站位)]，不限里程可信
_LOADED = False


def _find(fn: str) -> Optional[str]:
    for d in SEARCH_DIRS:
        p = os.path.join(d, fn)
        if os.path.exists(p):
            return p
    return None


def _norm(name: str) -> str:
    if not name:
        return ""
    s = re.sub(r"\s+", "", str(name))
    return s


def _key(name: str) -> str:
    """12306 风格站名（无「站/所」后缀）"""
    n = _norm(name)
    for suf in ("线路所", "站", "所"):
        if len(n) > len(suf) and n.endswith(suf):
            return n[: -len(suf)]
    return n


def load(force: bool = False) -> bool:
    global _STATIONS, _LINES, _BY_LINE, _POS, _POSALL, _LOADED
    if _LOADED and not force:
        return bool(_STATIONS)

    sp, lp = _find("cnrail_stations.json"), _find("cnrail_lines.json")
    if not sp or not lp:
        _LOADED, _STATIONS, _LINES = True, {}, []
        return False

    with open(sp, encoding="utf-8") as f:
        _STATIONS = json.load(f)
    with open(lp, encoding="utf-8") as f:
        _LINES = json.load(f)

    _BY_LINE = {ln["name"]: ln for ln in _LINES}

    # 反向索引：站名 -> [(线路序号, 站位, 里程)]
    pos: Dict[str, List[Tuple[int, int, float]]] = {}
    posall: Dict[str, List[Tuple[int, int]]] = {}
    for li, ln in enumerate(_LINES):
        reliable = bool(ln.get("kmReliable", ln.get("kmComplete")))
        for pi, p in enumerate(ln.get("stations") or []):
            k = _key(p.get("name", ""))
            if k:
                posall.setdefault(k, []).append((li, pi))
            if reliable and p.get("km") is not None and k:
                pos.setdefault(k, []).append((li, pi, p["km"]))
    _POS = pos
    _POSALL = posall
    _LOADED = True
    return True


def _ensure() -> None:
    if not _LOADED:
        load()


def available() -> bool:
    _ensure()
    return bool(_STATIONS)


# --------------------------------------------------------------------------
# 查询
# --------------------------------------------------------------------------

def station(name: str) -> Optional[dict]:
    _ensure()
    k = _key(name)
    if not _STATIONS:
        return None
    return _STATIONS.get(k) or _STATIONS.get(_norm(name))


def coord(name: str) -> Optional[Tuple[float, float]]:
    st = station(name)
    if st and st.get("lat") is not None and st.get("lon") is not None:
        return (float(st["lat"]), float(st["lon"]))
    return None


def lines_of(name: str) -> List[str]:
    st = station(name)
    return list(st.get("lines") or []) if st else []


def line(name: str) -> Optional[dict]:
    _ensure()
    if not _BY_LINE:
        return None
    n = _norm(name)
    if n in _BY_LINE:
        return _BY_LINE[n]
    for cand in (n + "线", n[:-1] if n.endswith("线") else n):
        if cand in _BY_LINE:
            return _BY_LINE[cand]
    return None


def search_line(q: str, limit: int = 20) -> List[dict]:
    _ensure()
    q = _norm(q)
    if not _LINES:
        return []
    if not q:
        return [{"name": l["name"], "n": l["n"], "totalKm": l["totalKm"],
                 "color": l.get("color")} for l in _LINES[:limit]]
    hits = [l for l in _LINES if q in _norm(l["name"])]
    hits.sort(key=lambda l: (_norm(l["name"]).index(q), len(l["name"])))
    return [{"name": l["name"], "n": l["n"], "totalKm": l["totalKm"],
             "color": l.get("color"), "kmComplete": l["kmComplete"]} for l in hits[:limit]]


def line_detail(name: str) -> Optional[dict]:
    """线路 + 站点坐标（供地图绘制）"""
    ln = line(name)
    if not ln:
        return None
    pts = []
    for p in ln["stations"]:
        c = coord(p["name"])
        pts.append({"name": _key(p["name"]), "km": p["km"],
                    "lat": c[0] if c else None, "lon": c[1] if c else None})
    return {**ln, "stations": pts,
            "coordCoverage": round(sum(1 for p in pts if p["lat"] is not None) / max(len(pts), 1) * 100, 1)}


# --------------------------------------------------------------------------
# 经由推断（这趟车走了哪几条线路）
# --------------------------------------------------------------------------

_LINE_TAIL = re.compile(r"(高速线|客运专线|高速铁路|城际铁路|普速线|铁路|线)$")


def _family(name: str) -> str:
    """线路族：京广高速线 / 京广线 归为同族，用于跨段连续性判断"""
    n = _norm(name)
    return _LINE_TAIL.sub("", n) or n


def _cand_lines(a: str, b: str) -> List[Tuple[str, str]]:
    """
    a→b 区间可能经由的线路，按可信度排序，返回 [(线路名, 依据)]

    依据三档，从强到弱：
      km    线路里程表能算出该段里程（最可靠，说明两站确在同一条线上且顺序正确）
      pos   两站都出现在该线的站序里，且 a 在 b 之前
      lines 两站的车站档案都标注了这条线（交集，最弱，只作兜底）
    """
    _ensure()
    ka, kb = _key(a), _key(b)
    out: List[Tuple[str, str]] = []
    seen = set()

    _, lname = _pair_km_raw(a, b)
    if lname:
        out.append((lname, "km"))
        seen.add(lname)

    pa_list = (_POSALL or {}).get(ka) or []
    pb_list = (_POSALL or {}).get(kb) or []
    for li, pa in pa_list:
        for li2, pb in pb_list:
            if li != li2 or pb <= pa:
                continue
            nm = _LINES[li]["name"]
            if nm not in seen:
                seen.add(nm)
                out.append((nm, "pos"))

    la = set((station(a) or {}).get("lines") or [])
    lb = set((station(b) or {}).get("lines") or [])
    for nm in sorted(la & lb):
        ln = line(nm)
        name = ln["name"] if ln else _norm(nm)
        if name not in seen:
            seen.add(name)
            out.append((name, "lines"))
    return out


def infer_via(stops: Sequence[dict]) -> dict:
    """
    推断一列车经由哪些线路，输出**结构化区间**，供界面展示与人工校对。

    返回::

        {
          'segs':      [{'line': '峰福线', 'from': '福州', 'to': '武夷山', 'src': 'lines'}],
          'unknown':   [{'from': '武夷山', 'to': '鹰潭'}],   # 识别不出的区间
          'pairs': 20, 'resolved': 18, 'coverage': 90.0, 'sources': ['lines'],
        }

    区间按运行顺序首尾相接；中间有识别不出的区间时**不合并**，
    否则会把两段不同线路连成一条，看不出缺在哪儿。
    """
    _ensure()
    names = [_key(s["name"]) for s in stops]
    pairs = list(zip(names, names[1:]))
    if not pairs:
        return {"segs": [], "unknown": [], "pairs": 0, "resolved": 0,
                "coverage": 0.0, "sources": []}

    chosen: List[Optional[Tuple[str, str]]] = []
    prev: Optional[str] = None
    for a, b in pairs:
        cands = _cand_lines(a, b)
        if not cands:
            chosen.append(None)
            prev = None                      # 断链后不再要求与前面连续
            continue
        # 先取可信度最高的一档，再在这一档里挑与上一段同线/同族的
        tier = [c for c in cands if c[1] == cands[0][1]]
        same = [c for c in tier if prev and (c[0] == prev or _family(c[0]) == _family(prev))]
        pick = (same or tier)[0]
        chosen.append(pick)
        prev = pick[0]

    segs: List[dict] = []
    for i, c in enumerate(chosen):
        a, b = pairs[i]
        if not c:
            continue
        nm, src = c
        if segs and segs[-1]["line"] == nm and segs[-1]["to"] == a:
            segs[-1]["to"] = b
        else:
            segs.append({"line": nm, "from": a, "to": b, "src": src, "i0": i})

    unknown: List[dict] = []
    for i, c in enumerate(chosen):
        if c:
            continue
        a, b = pairs[i]
        if unknown and unknown[-1]["to"] == a:
            unknown[-1]["to"] = b
        else:
            unknown.append({"from": a, "to": b, "i0": i})

    resolved = sum(1 for c in chosen if c)
    sources: List[str] = []
    for c in chosen:
        if c and c[1] not in sources:
            sources.append(c[1])
    return {
        "segs": segs,
        "unknown": unknown,
        "pairs": len(pairs),
        "resolved": resolved,
        "coverage": round(resolved / len(pairs) * 100, 1),
        "sources": sources,
    }


# --------------------------------------------------------------------------
# 里程修正
# --------------------------------------------------------------------------

def _pair_km_raw(a: str, b: str) -> Tuple[Optional[float], Optional[str]]:
    """查找 a→b 在共线铁路上的里程差，返回 (km, 线路名)；多候选取中位数"""
    _ensure()
    if not _POS:
        return None, None
    ka, kb = _key(a), _key(b)
    ea, eb = _POS.get(ka), _POS.get(kb)
    if not ea or not eb:
        return None, None
    cands: List[Tuple[float, str]] = []
    for li_a, pa, kma in ea:
        for li_b, pb, kmb in eb:
            if li_a != li_b or kma is None or kmb is None or pb <= pa:
                continue
            d = kmb - kma
            if 0 <= d < 3000:
                cands.append((d, _LINES[li_a]["name"]))
    if not cands:
        return None, None
    cands.sort()
    return cands[len(cands) // 2][0], cands[len(cands) // 2][1]


def refine_km(stops: Sequence[dict], fallback_haversine=None) -> dict:
    """
    用线路里程表修正一串站点（[{'name':..,'lat':..,'lon':..}]）的累计里程。
    返回 {'km': [...], 'viaLines': [...], 'matched': n, 'total': distance}
    未能匹配到的区间用大圆距离兜底（并标记）。
    """
    _ensure()
    n = len(stops)
    segs = n - 1
    if segs <= 0:
        return {"km": [0.0] * n, "viaLines": [], "matched": 0, "segments": 0,
                "total": 0.0, "mode": "estimate", "factor": 1.0}

    def straight(a, b) -> Optional[float]:
        if fallback_haversine and a.get("lat") and b.get("lat"):
            return fallback_haversine(a["lat"], a["lon"], b["lat"], b["lon"])
        return None

    # 第一轮：取线路里程，并用「里程/直线距离」比值做合理性校验（铁路不可能短于直线）
    raw: List[Optional[float]] = [None] * segs
    hav: List[Optional[float]] = [straight(stops[i], stops[i + 1]) for i in range(segs)]
    lines_of_seg: List[Optional[str]] = [None] * segs
    ratios: List[float] = []          # (比值, 直线距离)
    for i in range(segs):
        km, lname = _pair_km_raw(stops[i]["name"], stops[i + 1]["name"])
        if km is None or km <= 0:
            continue
        h = hav[i]
        if h and h > 3:
            r = km / h
            if not (0.95 <= r <= 1.45):       # 明显不是同一段线路
                continue
            ratios.append((r, h))
        raw[i] = km
        lines_of_seg[i] = lname

    matched = sum(1 for v in raw if v is not None)

    # 弯曲系数：短区间比值噪声大，只用 100 km 以上的匹配区间，且至少 2 段
    long_ratios = [r for r, h in ratios if h >= 100]
    if len(long_ratios) >= 2:
        factor = statistics.median(long_ratios)
    else:
        factor = 1.12                     # 稀疏停站下铁路相对直线距离的经验放大系数
    factor = min(max(factor, 1.0), 1.3)

    deltas: List[float] = []
    for i in range(segs):
        if raw[i] is not None:
            deltas.append(raw[i])
        elif hav[i] is not None:
            deltas.append(hav[i] * factor)
        else:
            deltas.append(0.0)

    seg_lines: List[str] = []
    for ln in lines_of_seg:
        if ln and (not seg_lines or seg_lines[-1] != ln):
            seg_lines.append(ln)

    cum, acc = [], 0.0
    for i in range(n):
        if i:
            acc += deltas[i - 1]
        cum.append(round(acc, 1))

    mode = "real" if matched == segs else ("hybrid" if matched else "estimate")
    return {"km": cum, "viaLines": seg_lines, "matched": matched,
            "total": round(acc, 1), "segments": segs,
            "mode": mode, "factor": round(factor, 3)}


def expand_path(stops: Sequence[dict], max_points: int = 1500) -> List[dict]:
    """
    把「经停站序列」展开成贴合线路的多段折线：
    对每对相邻经停站，若两者同在一条有里程的线路上，就把该线中间的站补进来，
    再按累计里程比例映射到本趟车的里程坐标。返回 [{lat, lon, km}]（km 单调递增）。
    """
    _ensure()
    pts: List[dict] = []
    n = len(stops)

    def push(lat, lon, km):
        if lat is None or lon is None:
            return
        if pts and abs(pts[-1]["lat"] - lat) < 1e-7 and abs(pts[-1]["lon"] - lon) < 1e-7:
            pts[-1]["km"] = km
            return
        pts.append({"lat": round(lat, 5), "lon": round(lon, 5), "km": round(km, 1)})

    for i in range(n):
        s = stops[i]
        push(s.get("lat"), s.get("lon"), s.get("km") or 0.0)
        if i == n - 1:
            break
        a, b = s, stops[i + 1]
        ka, kb = a.get("km") or 0.0, b.get("km") or 0.0
        ka_, kb_ = _key(a["name"]), _key(b["name"])
        best = None
        if _POS:
            for li, pa, lka in _POS.get(ka_, ()):
                for lj, pb, lkb in _POS.get(kb_, ()):
                    if li != lj or lka is None or lkb is None or pb <= pa:
                        continue
                    span = lkb - lka
                    if span < 1:
                        continue
                    ratio = (kb - ka) / span
                    if 0.7 <= ratio <= 1.4:          # 里程口径要一致，否则不是同一段
                        if best is None or pb - pa > best[2]:
                            best = (li, pa, pb, lka, span, ratio)
        if not best:
            continue
        li, pa, pb, lka, span, ratio = best
        line_pts = _LINES[li]["stations"]
        for mid in line_pts[pa + 1:pb]:
            c = coord(mid["name"])
            if not c or mid["km"] is None:
                continue
            push(c[0], c[1], ka + (mid["km"] - lka) * ratio)
            if len(pts) > max_points:
                return pts
    return pts


def stats() -> dict:
    _ensure()
    if not _STATIONS:
        return {"available": False}
    return {
        "available": True,
        "stations": len(_STATIONS),
        "lines": len(_LINES or []),
        "linesWithKm": sum(1 for l in (_LINES or []) if l.get("kmComplete")),
        "links": sum(len(s.get("lines") or []) for s in _STATIONS.values()),
    }
