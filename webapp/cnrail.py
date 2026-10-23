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
_STATIONS_RAW: Optional[Dict[str, dict]] = None                  # 档案原值，用户新增的车站在副本上合并
_LINES: Optional[List[dict]] = None
_LINES_RAW: Optional[List[dict]] = None                          # 档案原值，人工里程在副本上生效
_BY_LINE: Optional[Dict[str, dict]] = None
_POS: Optional[Dict[str, List[Tuple[int, int, float]]]] = None   # 站名 -> [(线路序号, 站位, 里程)]
_POSALL: Optional[Dict[str, List[Tuple[int, int]]]] = None       # 站名 -> [(线路序号, 站位)]，不限里程可信
_CATALOG: Dict[str, dict] = {}                                   # 线路名 -> 线路资料（含站表）
_CATALOG_ALL: Dict[str, dict] = {}                               # 资料 id -> 线路资料（含未收录线路）
_NAMES: Optional[set] = None                                     # 线路库里出现过的站名（原样），station_name 用
_LOADED = False

# 人工里程：{线路名: {"segKm": {"0": 12.3}, "totalKm": 240.0}}
# 键是区间序号（第 i 站 → 第 i+1 站），与站名解耦
_LINE_OVR: Dict[str, dict] = {}

# 用户新增的线路 / 车站（档案里没有的）
_CUSTOM_LINES: Dict[str, dict] = {}
_CUSTOM_STATIONS: Dict[str, dict] = {}


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


def station_name(name: str) -> str:
    """
    站名规范化，存储 / 显示都用它：

      「天津西站」→「天津西」（线路库里另有「天津西」这种不带「站」的写法）
      「十八站」  →「十八站」（库里的站名本身带「站」，是地名的一部分，不能截）

    判据：去掉尾部「站」之后，若线路库里存在**不带「站」的同名站**，说明裸名才是
    规范写法，取短名；否则保持原样（「十八站」「新九站」这类不会被截坏）。
    """
    n = _norm(name)
    if not n or not n.endswith("站") or len(n) <= 1:
        return n
    short = n[:-1]
    return short if short in _line_names() else n


def _line_names() -> set:
    """线路库里出现过的站名（原样，用于判断「XX站」是不是后缀）"""
    global _NAMES
    if _NAMES is None:
        s = set()
        for ln in (_LINES or []):
            for p in ln.get("stations") or []:
                nm = _norm(p.get("name") or "")
                if nm:
                    s.add(nm)
        _NAMES = s
    return _NAMES


def load(force: bool = False) -> bool:
    global _STATIONS, _STATIONS_RAW, _LINES_RAW, _LOADED
    if _LOADED and not force:
        return bool(_STATIONS)

    sp, lp = _find("cnrail_stations.json"), _find("cnrail_lines.json")
    if not sp or not lp:
        _LOADED, _STATIONS, _STATIONS_RAW, _LINES_RAW = True, {}, {}, []
        rebuild_lines()
        return False

    with open(sp, encoding="utf-8") as f:
        _STATIONS = json.load(f)
    _STATIONS_RAW = dict(_STATIONS)
    with open(lp, encoding="utf-8") as f:
        _LINES_RAW = json.load(f)

    rebuild_lines()
    _LOADED = True
    return True


_NA_SEG = ("-", "--", "—", "——", "–", "~", "～", "?", "？", "不详", "未知", "无", "n/a", "na", "unknown")


def _is_na(v) -> bool:
    return v is None or (isinstance(v, str) and v.strip().lower() in _NA_SEG)


def _rebuild_km(stations: List[dict], seg_km: Optional[dict] = None,
                total_km: Optional[float] = None,
                sta_km: Optional[dict] = None) -> Tuple[List[Optional[float]], List[Optional[float]]]:
    """
    由「站间里程 / 站累计里程」重建累计里程。返回 (累计, 区间)。

    区间：档案有前后两站里程时取差；人工填的段覆盖。
    累计：站点被人工指定了累计里程就直接采用（None = 这一站明确不详）；
          否则按上一站累加，累加不出来（前一段不详）时用档案累计里程续上，
          后面的段照常累加——一站不详不连累后面的车站。
    给了总里程时，只把**没手填**的段等比缩放到 total_km − 手填段之和。
    seg_km 里值为 None 的段表示「这一段区间不明」：即使档案里有值也不当站间距用，不参与缩放。
    """
    n = len(stations)
    base = [p.get("km") for p in stations]
    seg: List[Optional[float]] = [None] * max(n - 1, 0)
    for i in range(n - 1):
        a, b = base[i], base[i + 1]
        if a is not None and b is not None and b >= a:
            seg[i] = round(float(b) - float(a), 1)

    manual: Dict[int, float] = {}
    unknown: set = set()
    for k, v in (seg_km or {}).items():
        try:
            i = int(k)
        except (TypeError, ValueError):
            continue
        if not (0 <= i < n - 1):
            continue
        if _is_na(v):
            seg[i] = None
            unknown.add(i)
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f >= 0:
            seg[i] = round(f, 1)
            manual[i] = seg[i]

    if total_km:
        rest = [i for i in range(n - 1) if i not in manual and i not in unknown]
        rest_sum = sum(seg[i] for i in rest if seg[i] is not None)
        target = float(total_km) - sum(manual.values())
        if rest and rest_sum > 0 and target > 0:
            r = target / rest_sum
            for i in rest:
                if seg[i] is not None:
                    seg[i] = round(seg[i] * r, 1)

    # 人工指定的「站累计里程」：这一站就是这么多（None = 这一站里程明确不详）
    sta: Dict[int, Optional[float]] = {}
    for k, v in (sta_km or {}).items():
        try:
            i = int(k)
        except (TypeError, ValueError):
            continue
        if not (0 <= i < n):
            continue
        if _is_na(v):
            sta[i] = None
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if 0 <= f <= 20000:
            sta[i] = round(f, 1)

    # 累计：先看有没有人工指定这一站，其次按上一站累加，累加不出来再退回档案累计里程。
    cum: List[Optional[float]] = [None] * n
    if n:
        if 0 in sta:
            cum[0] = sta[0]
        elif base[0] is not None:
            cum[0] = round(float(base[0]), 1)
        else:
            cum[0] = 0.0
        for i in range(n - 1):
            if (i + 1) in sta:
                val: Optional[float] = sta[i + 1]
            elif cum[i] is not None and seg[i] is not None:
                val = round(cum[i] + seg[i], 1)
            elif base[i + 1] is not None:
                val = round(float(base[i + 1]), 1)
            else:
                val = None
            cum[i + 1] = val
    # 最终累计定下来后，区间按累计差回填（两端都确定且不倒挂时）。
    # 人工标过「这一段不明」的保持不明；站累计被人工改过的段，以累计为准。
    for i in range(n - 1):
        if i in unknown:
            seg[i] = None
        elif cum[i] is not None and cum[i + 1] is not None and cum[i + 1] >= cum[i]:
            seg[i] = round(cum[i + 1] - cum[i], 1)
        elif i in sta or (i + 1) in sta:
            seg[i] = None
    return cum, seg


def _line_with_override(raw: dict, ov: Optional[dict]) -> dict:
    """档案线路 + 人工里程 → 生效线路（不动原字典，累积里程全部重算）"""
    ln = {k: v for k, v in raw.items() if k != "stations"}
    stations = [dict(p) for p in (raw.get("stations") or [])]
    ln["stations"] = stations
    ln["totalKmBase"] = raw.get("totalKm")
    ln["kmManual"] = False
    if not ov:
        return ln

    cum, seg = _rebuild_km(stations, ov.get("segKm"), ov.get("totalKm"), ov.get("staKm"))
    for p, c in zip(stations, cum):
        p["kmBase"] = p.get("km")
        p["km"] = c
    total = cum[-1] if cum and cum[-1] is not None else (
        float(ov["totalKm"]) if ov.get("totalKm") else raw.get("totalKm"))
    ln["totalKm"] = total
    ln["kmComplete"] = bool(seg) and all(s is not None for s in seg) and all(c is not None for c in cum)
    ln["kmReliable"] = True if ln["kmComplete"] else bool(raw.get("kmReliable"))
    ln["kmManual"] = True
    ln["segKm"] = list(seg)     # 按下标给，含 None（该段站间距明确不详）
    return ln


def rebuild_lines() -> None:
    """把人工里程套回线路表，并重建「站名 → 线路里程」索引"""
    global _LINES, _BY_LINE, _POS, _POSALL, _NAMES
    custom = _CUSTOM_LINES or {}
    raw_lines = list(custom.values())
    raw_lines += [r for r in (_LINES_RAW or []) if r.get("name") not in custom]
    _fill_km_from_catalog(raw_lines)             # 口径一致的线路上，用资料库补空白站里程
    _LINES = [_line_with_override(r, _LINE_OVR.get(r.get("name"))) for r in raw_lines]
    _BY_LINE = {ln["name"]: ln for ln in _LINES}

    pos: Dict[str, List[Tuple[int, int, float]]] = {}
    posall: Dict[str, List[Tuple[int, int]]] = {}
    for li, ln in enumerate(_LINES):
        # 人工填过里程的线路也进索引（只取已知的站，缺口段本来也不会有值），
        # 否则用户改完里程，车次里程还是按老数据估算
        reliable = bool(ln.get("kmReliable") or ln.get("kmComplete") or ln.get("kmManual"))
        for pi, p in enumerate(ln.get("stations") or []):
            k = _key(p.get("name", ""))
            if not k:
                continue
            posall.setdefault(k, []).append((li, pi))
            if reliable and p.get("km") is not None:
                pos.setdefault(k, []).append((li, pi, p["km"]))
    _POS = pos
    _POSALL = posall
    _NAMES = None          # 站名集合随线路表变化，下次用到时重建


def set_line_overrides(mapping: Optional[Dict[str, dict]]) -> None:
    """设置全部人工线路里程（railcore 启动与保存时调用）；已加载则立即重建"""
    global _LINE_OVR
    _LINE_OVR = {k: v for k, v in (mapping or {}).items() if v}
    if _LOADED:
        rebuild_lines()


def set_custom(lines: Optional[Dict[str, dict]] = None,
               stations: Optional[Dict[str, dict]] = None) -> None:
    """
    设置用户新增的线路 / 车站（railcore 启动与保存时调用）。
    lines    {线路名: {name, n, totalKm, kmComplete, kmReliable, stations:[{name, km}], custom:True}}
    stations {站名键: {name, lat, lon, lines:[], tele, bureau, custom:True}}
    """
    global _CUSTOM_LINES, _CUSTOM_STATIONS, _STATIONS
    _ensure()
    if lines is not None:
        _CUSTOM_LINES = {k: v for k, v in lines.items() if v}
    if stations is not None:
        _CUSTOM_STATIONS = {k: v for k, v in stations.items() if v}
        merged: Dict[str, dict] = dict(_STATIONS_RAW or {})
        for key, st in _CUSTOM_STATIONS.items():
            base = merged.get(key)
            if base:
                cur = dict(base)                     # 别改到档案原值
                for f in ("lat", "lon"):
                    if cur.get(f) is None and st.get(f) is not None:
                        cur[f] = st[f]
                for f in ("tele", "bureau"):
                    if not cur.get(f) and st.get(f):
                        cur[f] = st[f]
                if st.get("lines"):
                    cur["lines"] = list(dict.fromkeys(
                        list(cur.get("lines") or []) + list(st["lines"])))
                cur["custom"] = True
                merged[key] = cur
            else:
                merged[key] = dict(st)
        _STATIONS = merged
    if _LOADED:
        rebuild_lines()



def set_catalog(entries: Optional[Sequence[dict]] = None) -> None:
    """
    载入「线路资料库」（外部整理来的线路参数 / 介绍 / 逐站里程）。

    entries: [{id, name, title, line_name, type, level, service, alias, foreign,
               design_speed, run_speed, length_km, open_date, from_sta, to_sta,
               intro, url, stops: [{name, km}]}]
    line_name 为 None 表示线路库里没有这条线（只在资料库里）。
    """
    global _CATALOG, _CATALOG_ALL, _CATALOG_STOPS
    by_line: Dict[str, dict] = {}
    by_id: Dict[str, dict] = {}
    stops_by_id: Dict[str, List[dict]] = {}
    for e in entries or []:
        if not e:
            continue
        lid = str(e.get("id") or "")
        if not lid:
            continue
        stops_by_id[lid] = [{"name": s.get("name"), "km": s.get("km")}
                            for s in (e.get("stops") or []) if s.get("name")]
        rec = {k: v for k, v in e.items() if k != "stops"}
        rec["stops"] = stops_by_id[lid]
        by_id[lid] = rec
        ln = _norm(e.get("line_name") or "")
        if ln:
            by_line.setdefault(ln, rec)
    _CATALOG, _CATALOG_ALL, _CATALOG_STOPS = by_line, by_id, stops_by_id
    if _LOADED:
        rebuild_lines()


def catalog_of(name: str) -> Optional[dict]:
    """某条线路对应的资料（没有返回 None）"""
    _ensure()
    return _CATALOG.get(_norm(name))


def catalog_get(lid_or_name: str) -> Optional[dict]:
    """按资料 id 或线路名取一条完整资料（含站表）"""
    _ensure()
    key = _norm(lid_or_name)
    if key in _CATALOG_ALL:
        return _CATALOG_ALL[key]
    for name, rec in _CATALOG.items():
        if key in (name, _norm(rec.get("name") or ""), _norm(rec.get("title") or "")):
            return rec
    for rec in _CATALOG_ALL.values():
        if key in (_norm(rec.get("name") or ""), _norm(rec.get("title") or "")):
            return rec
    return None


def catalog_search(q: str, limit: int = 20, only_unmatched: bool = True) -> List[dict]:
    """搜线路资料库；默认只给「线路库里还没有」的那些"""
    _ensure()
    q = _norm(q)
    out = []
    for rec in _CATALOG_ALL.values():
        if only_unmatched and rec.get("line_name"):
            continue
        if len(rec.get("stops") or []) < 2:       # 没有站表的条目搜出来也没用
            continue
        if q:
            hay = _norm(rec.get("name") or "") + _norm(rec.get("title") or "") \
                + _norm(rec.get("alias") or "")
            if q not in hay:
                continue
        out.append(rec)
    out.sort(key=lambda r: (-int(r.get("n_km") or 0), -int(r.get("n_stop") or 0)))
    return [{"id": r.get("id"), "name": r.get("name"), "title": r.get("title"),
             "type": r.get("type"), "level": r.get("level"), "length_km": r.get("length_km"),
             "n_stop": len(r.get("stops") or []),
             "n_km": sum(1 for s in (r.get("stops") or []) if s.get("km") is not None),
             "line_name": r.get("line_name"), "url": r.get("url"),
             "open_date": r.get("open_date")} for r in out[:limit]]


def _km_check(ln: dict, rec: dict) -> dict:
    """
    比对档案里程与资料库里程：
      pairs   两套都有里程的共同站数
      agree   两套是否同一口径（最大偏差 ≤2 km）
      diff    偏差最大的几个站，给前端提示用
      fill    档案缺里程、资料库有值的站数（口径一致才能补）
    """
    hint = {_key(s["name"]): s["km"] for s in (rec.get("stops") or [])
            if s.get("km") is not None}
    pairs, fill = [], 0
    for p in ln.get("stations") or []:
        k = _key(p.get("name") or "")
        h = hint.get(k)
        if p.get("km") is None:
            if h is not None:
                fill += 1
            continue
        if h is not None:
            pairs.append((p["name"], float(p["km"]), float(h)))
    if not pairs:
        return {"pairs": 0, "fill": fill, "agree": None, "diff": []}
    diffs = sorted(h - o for _, o, h in pairs)
    med = diffs[len(diffs) // 2]
    worst = max(abs(d) for d in diffs)
    samples = sorted(pairs, key=lambda t: -abs(t[2] - t[1]))[:3]
    return {"pairs": len(pairs), "fill": fill, "agree": worst <= 2.0,
            "median": round(med, 1), "worst": round(worst, 1),
            "diff": [{"name": n, "ours": o, "theirs": h} for n, o, h in samples]}


def _fill_km_from_catalog(raw_lines: List[dict]) -> int:
    """
    口径一致的线路上，用资料库里多出来的里程点补档案的空白站（只补 None，不改已有值）。

    返回补了多少站。资料库和档案不是同一套里程口径时一律不补 —— 宁缺勿错。
    """
    if not _CATALOG:
        return 0
    filled = 0
    for ln in raw_lines:
        rec = _CATALOG.get(_norm(ln.get("name") or ""))
        if not rec:
            continue
        chk = _km_check(ln, rec)
        if not chk["fill"] or chk.get("agree") is not True:
            continue
        hint = {_key(s["name"]): s["km"] for s in (rec.get("stops") or [])
                if s.get("km") is not None}
        offset = chk.get("median") or 0.0
        stations = ln.get("stations") or []
        for i, p in enumerate(stations):
            if p.get("km") is not None:
                continue
            h = hint.get(_key(p.get("name") or ""))
            if h is None:
                continue
            val = round(h - offset, 1)
            prev = next((q["km"] for q in reversed(stations[:i]) if q.get("km") is not None), None)
            nxt = next((q["km"] for q in stations[i + 1:] if q.get("km") is not None), None)
            if prev is not None and val <= prev:
                continue                          # 补进去会倒挂，不如留空
            if nxt is not None and val >= nxt:
                continue
            p["km"] = val
            p["kmSrc"] = "catalog"
            filled += 1
    return filled


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
                 "color": l.get("color"), "kmComplete": l.get("kmComplete"),
                 "kmManual": l.get("kmManual", False)} for l in _LINES[:limit]]
    hits = [l for l in _LINES if q in _norm(l["name"])]
    hits.sort(key=lambda l: (_norm(l["name"]).index(q), len(l["name"])))
    return [{"name": l["name"], "n": l["n"], "totalKm": l["totalKm"],
             "color": l.get("color"), "kmComplete": l.get("kmComplete"),
             "kmManual": l.get("kmManual", False)} for l in hits[:limit]]


def line_station_names(name: str) -> Optional[List[str]]:
    """某条线路按站序排好的站名（只取名字，供校对页「经由」选站用）

    站名按 12306 口径输出（去掉「站」后缀），这样自建线路和库内线路、
    车次站名三者对得上（「天津站」和「天津」不该是两个站）。
    """
    ln = line(name)
    if not ln:
        return None
    names = [station_name(p.get("name")) for p in (ln.get("stations") or []) if p.get("name")]
    return list(dict.fromkeys(n for n in names if n))


def line_detail(name: str) -> Optional[dict]:
    """线路 + 站点坐标（供地图绘制）、站间里程（供编辑）"""
    ln = line(name)
    if not ln:
        return None
    pts = []
    for p in ln["stations"]:
        c = coord(p["name"])
        pts.append({"name": _key(p["name"]), "km": p["km"],
                    "kmBase": p.get("kmBase", p["km"]),
                    "kmSrc": p.get("kmSrc"),
                    "lat": c[0] if c else None, "lon": c[1] if c else None})
    n = len(pts)
    seg = ln.get("segKm")
    if not isinstance(seg, list) or len(seg) != max(n - 1, 0):
        # 没人工改过：直接由档案累计里程取差
        seg = []
        for i in range(n - 1):
            a, b = pts[i]["km"], pts[i + 1]["km"]
            seg.append(round(float(b) - float(a), 1) if (a is not None and b is not None) else None)
    ov = _LINE_OVR.get(ln["name"]) or {}
    rec = _CATALOG.get(_norm(ln["name"]))
    info = None
    km_check = None
    if rec:
        info = {k: rec.get(k) for k in (
            "id", "name", "title", "type", "level", "service", "alias", "foreign",
            "design_speed", "run_speed", "length_km", "open_date", "from_sta", "to_sta",
            "intro", "url")}
        info["matchHow"] = rec.get("match_how")
        km_check = _km_check(ln, rec)
    return {**ln, "stations": pts, "segKm": seg,
            "override": {"segKm": ov.get("segKm") or {}, "staKm": ov.get("staKm") or {},
                         "totalKm": ov.get("totalKm"),
                         "note": ov.get("note") or ""} if ov else None,
            "info": info, "kmCheck": km_check,
            "kmFromCatalog": sum(1 for p in pts if p.get("kmSrc") == "catalog"),
            "coordCoverage": round(sum(1 for p in pts if p["lat"] is not None) / max(n, 1) * 100, 1)}


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


def _pick_segment(ka_: str, kb_: str, ka: float, kb: float):
    """在「相邻经停站对」之间找一条能贴合的线路：返回 (线路序号, 起站位, 止站位, 起里程, 跨度, 比例)"""
    best = None
    if not _POS:
        return None
    for li, pa, lka in _POS.get(ka_, ()):
        for lj, pb, lkb in _POS.get(kb_, ()):
            if li != lj or lka is None or lkb is None or pb <= pa:
                continue
            span = lkb - lka
            if span < 1:
                continue
            ratio = (kb - ka) / span
            if 0.7 <= ratio <= 1.4:                  # 里程口径要一致，否则不是同一段
                if best is None or pb - pa > best[2] - best[1]:
                    best = (li, pa, pb, lka, span, ratio)
    return best


def _via_hits(ka_: str, kb_: str, ka: float, kb: float, prefs) -> List[tuple]:
    """
    人工 / 自动推断已经指明了经由线路时，只在这些线的站序里找区间。

    与 _pick_segment 的两点不同：
      - 不看线路里程是否可信。京沪线 150 站里 92 站没有里程（kmReliable=False），
        于是整条线进不了 _POS，人工写「京沪线」也匹配不上，中间站全丢；
        但它的**站序**是准的，只要知道 a、b 在这条线上就能把中间站补出来。
      - 里程口径对不上（ratio 超出 0.7~1.4）时不丢弃，而是把 ratio 置 None，
        让调用方退回「按站序均分」来估每站的公里数。
    """
    if not prefs:
        return []
    hits = []
    for li, pa in (_POSALL or {}).get(ka_, ()):
        if _norm(_LINES[li]["name"]) not in prefs:
            continue
        lks = _LINES[li]["stations"]
        for lj, pb in (_POSALL or {}).get(kb_, ()):
            if lj != li or pb <= pa:
                continue
            lka, lkb = lks[pa].get("km"), lks[pb].get("km")
            span, ratio = None, None
            if lka is not None and lkb is not None and lkb - lka >= 1:
                span = lkb - lka
                r = (kb - ka) / span
                if 0.7 <= r <= 1.4:
                    ratio = r
            hits.append((li, pa, pb, lka, span, ratio))
    # 里程能对上的优先，其次取最直接的一段（避免把环线绕一圈的站序塞进来）
    hits.sort(key=lambda c: (0 if c[5] else 1, c[2] - c[1]))
    return hits


def _via_chain(segs: Optional[Sequence[dict]]) -> List[tuple]:
    """
    把经由此番的线段解析成 [(线路序号, 起点下标, 终点下标)]，方向即运行方向。

    线路库里没有的线名直接跳过（不影响其它段）；端站点在线路站序里找不到时也跳过。
    """
    chain: List[tuple] = []
    idx = {ln["name"]: i for i, ln in enumerate(_LINES or [])}
    for s in segs or []:
        name = (s or {}).get("line") or ""
        L = line(name)
        if not L or L["name"] not in idx:
            continue
        st = L["stations"]
        kf, kt = _key(s.get("from") or ""), _key(s.get("to") or "")
        if not kf or not kt:
            continue
        pf = pt = None
        for p, x in enumerate(st):
            kx = _key(x.get("name") or "")
            if pf is None and kx == kf:
                pf = p
            if pt is None and kx == kt:
                pt = p
        if pf is None or pt is None or pf == pt:
            continue
        chain.append((idx[L["name"]], pf, pt))
    return chain


def _chain_mids(chain: List[tuple], ka_: str, kb_: str) -> Optional[List[str]]:
    """
    a 到 b 跨越多条经由线段时（例如 通化 —梅集线→ 鸭园 —鸭大线→ 白山市，
    而鸭园不是本趟车的经停站），把沿途中间站按运行顺序串出来。

    找不到「a 起、b 止」的完整链路时返回 None，交给原来的单线展开逻辑。
    """
    for k, (li, pf, pt) in enumerate(chain):
        lks = _LINES[li]["stations"]
        stp = 1 if pt >= pf else -1
        pa = next((p for p in range(pf, pt + stp, stp)
                   if _key(lks[p].get("name") or "") == ka_), None)
        if pa is None:
            continue
        mids: List[str] = []
        for p in range(pa + stp, pt + stp, stp):
            if _key(lks[p].get("name") or "") == kb_:
                return mids
            mids.append(lks[p].get("name"))
        for li2, pf2, pt2 in chain[k + 1:]:
            lks2 = _LINES[li2]["stations"]
            st2 = 1 if pt2 >= pf2 else -1
            for p in range(pf2, pt2 + st2, st2):
                if _key(lks2[p].get("name") or "") == kb_:
                    return mids
                mids.append(lks2[p].get("name"))
        return None
    return None


def path_stations(stops: Sequence[dict], max_points: int = 2000,
                  prefer: Optional[Sequence[str]] = None,
                  via: Optional[Sequence[dict]] = None) -> List[dict]:
    """
    把「经停站序列」展开成「经由铁路线上的全部车站」：
    相邻两站若在同一条线路上，就把该线中间的站补进来，按里程比例映射到本趟车的里程坐标。

    prefer 是优先采用的线路名（人工 / 自动推断出来的经由），命中时优先按它展开。
    via 是完整的经由区间 [{'line','from','to'}]，用于：
      - 人工指定了线路但该线档案里程不可信时也能按站序展开；
      - 相邻两站不在同一条线上时，按经由链路跨线串出中间站。

    返回 [{name, lat, lon, km, stop, idx}]：
      stop=True  本趟车停车（含始发、终到），idx 为在 stops 里的下标
      stop=False 列车通过但不办客的中间站
    """
    _ensure()
    pts: List[dict] = []
    n = len(stops)
    prefs = [_norm(x) for x in (prefer or []) if x]
    chain = _via_chain(via)

    def push(name, lat, lon, km, stop=False, idx=None):
        if lat is None or lon is None:
            return
        if not stop:
            # 线路所 / 中继站不是车站，不往地图上堆
            nk = _norm(name)
            if nk.endswith("线路所") or nk.endswith("所") or nk.endswith("中继站") or "线路所" in nk:
                return
            # 线路库里常写成「董家庄站」，按档案口径对齐成「董家庄」（「十八站」这种不动）
            name = station_name(name)
        if pts and _key(pts[-1]["name"]) == _key(name):
            pts[-1]["km"] = round(km, 1)
            if stop:
                pts[-1]["stop"] = True
                pts[-1]["idx"] = idx
            return
        pts.append({"name": name, "lat": round(float(lat), 5), "lon": round(float(lon), 5),
                    "km": round(float(km), 1), "stop": bool(stop), "idx": idx})

    for i in range(n):
        s = stops[i]
        push(s.get("name"), s.get("lat"), s.get("lon"), s.get("km") or 0.0, True, i)
        if i == n - 1:
            break
        a, b = s, stops[i + 1]
        ka, kb = float(a.get("km") or 0.0), float(b.get("km") or 0.0)
        ka_, kb_ = _key(a["name"]), _key(b["name"])

        # 1) 人工/自动推断点过名的线路，优先于按里程相似度猜出来的线
        vh = _via_hits(ka_, kb_, ka, kb, prefs)
        if not vh and chain:
            # 2) 跨线：a、b 不同线，但经由里给了 a→…→b 的链路（中间还夹着非经停站）
            names = _chain_mids(chain, ka_, kb_)
            if names:
                step = (kb - ka) / (len(names) + 1)
                for k, nm in enumerate(names, start=1):
                    c = coord(nm)
                    if c:
                        push(nm, c[0], c[1], ka + step * k)
                if len(pts) > max_points:
                    return pts
                continue
        best = vh[0] if vh else _pick_segment(ka_, kb_, ka, kb)
        if not best:
            continue
        li, pa, pb, lka, span, ratio = best
        line_pts = _LINES[li]["stations"]
        mids = line_pts[pa + 1:pb]
        if ratio is None:
            # 这条线的档案里程不全或口径不同：只能按站序均分，首尾仍钉在真实里程上
            step = (kb - ka) / max(1, pb - pa)
            for k, mid in enumerate(mids, start=1):
                c = coord(mid["name"])
                if not c:
                    continue
                push(mid["name"], c[0], c[1], ka + step * k)
        else:
            for mid in mids:
                c = coord(mid["name"])
                if not c or mid["km"] is None:
                    continue
                push(mid["name"], c[0], c[1], ka + (mid["km"] - lka) * ratio)
        if len(pts) > max_points:
            return pts
    return pts


def expand_path(stops: Sequence[dict], max_points: int = 1500,
                prefer: Optional[Sequence[str]] = None,
                via: Optional[Sequence[dict]] = None) -> List[dict]:
    """同 path_stations，但只取画线需要的 {lat, lon, km}（km 单调递增）"""
    return [{"lat": p["lat"], "lon": p["lon"], "km": p["km"]}
            for p in path_stations(stops, max_points, prefer, via)]


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
