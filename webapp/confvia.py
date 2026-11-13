# -*- coding: utf-8 -*-
"""confvia.py — 数据校对的线路口径：只用整理台「全部确认」的线路

2026-11-11 用户定：**数据校对里出现的线路，一律是整理台完成全部确认的那些**
（`confirmed.names()`，随整理台确认多少条动态变，别写死名单）。底表 / 资料库 /
china-emu / OSM / 自建 这些「来源版本」（京广线（OSM）、沪昆线（底表）…）以及
还没确认的线，既不参与自动识别，也不进线路下拉和缺口候选 —— 认不出来的区间就
当缺口（未识别）留给人工补，等整理台确认了那条线它自己会出现。

两个要点：

1. **站序取整理台的，不是线路库的。** 两边同名不同货：整理台确认的「京广线」
   212 站 / 2284 km，线路库里那条同名版本 193 站 / 4221 km。所以这里读
   `linedesk.db` 的 `ld_stop`（确认站序 + 里程），完全不碰 `rail.db` 的线路表。
   线路库里有、整理台没确认的线（京哈线、沈大线…）一律认不出来，这是有意的。

2. **缓存跟整理台走。** 用户在整理台确认/改站序，2 秒内这里就能看到，不用重启。

只给数据校对用（`curate.summarize` / `curate.detail` / 缺口候选 / 线路下拉）。
列车地图、主站等其它页面仍按各自原来的口径取数，不受影响。
"""
from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional, Sequence

import linedesk

_LOCK = threading.RLock()
_CACHE: dict = {"ts": 0.0, "data": None}
#: 站→线反查索引跟着 _CACHE 的版本走：线路 / 车站以后会一直增加，
#: 别每个请求都把整库重扫一遍，也别把它整份下发给前端
_SIDX: dict = {"ts": -1.0, "data": None}
#: 整理台是独立库，改完不通知本模块；2 秒 TTL 和 confirmed 的缓存节拍一致
_TTL = 2.0


def _key(name: str) -> str:
    """站名比对口径：直接借 cnrail 的（去空白、去「站 / 所」后缀）"""
    try:
        import cnrail
        return cnrail._key(name)
    except Exception:                          # noqa: BLE001
        s = "".join(str(name or "").split())
        for suf in ("线路所", "站", "所"):
            if len(s) > len(suf) and s.endswith(suf):
                return s[: -len(suf)]
        return s


def _build() -> dict:
    """{线名: {pos: {站名键: 下标}, km: [...], n: 站数, totalKm: 总里程}}"""
    import confirmed
    want = None
    try:
        want = set(confirmed.names())          # 已经按「线路/站点/走向都确认」筛过
    except Exception:                          # noqa: BLE001
        want = set()
    if not want:
        return {}
    # 总里程：ld_stop 的 km 有的线没填（整理台只确认了站序），退回清单里那个口径
    totals: Dict[str, object] = {}
    try:
        totals = {b["name"]: b.get("totalKm") for b in confirmed.list_all()}
    except Exception:                          # noqa: BLE001
        totals = {}

    c = linedesk.conn()
    try:
        rows = [dict(x) for x in c.execute(
            """SELECT l.name AS line, s.seq AS seq, s.name AS sta, s.km AS km
                 FROM ld_line l JOIN ld_stop s ON s.line_id = l.id
                WHERE l.status = 'confirmed' AND l.line_ok = 1 AND l.stop_ok = 1
                ORDER BY l.id, s.seq""")]
    except Exception:                          # noqa: BLE001
        rows = []
    finally:
        c.close()

    out: Dict[str, dict] = {}
    for r in rows:
        nm = (r.get("line") or "").strip()
        if not nm or nm not in want:
            continue
        sta = (r.get("sta") or "").strip()
        k = _key(sta)
        if not k:
            continue
        ent = out.setdefault(nm, {"pos": {}, "km": [], "stops": []})
        idx = len(ent["stops"])
        ent["stops"].append(sta)
        ent["pos"].setdefault(k, idx)           # 同名站（含线路所截断撞名）取第一个
        ent["km"].append(r.get("km"))
    for nm, ent in out.items():
        ent["n"] = len(ent["stops"])
        last = next((k for k in reversed(ent["km"]) if k is not None), None)
        ent["totalKm"] = last if last is not None else totals.get(nm)
    return out


def index() -> dict:
    with _LOCK:
        now = time.time()
        if _CACHE["data"] is not None and now - _CACHE["ts"] < _TTL:
            return _CACHE["data"]
        data = _build()
        _CACHE["data"] = data
        _CACHE["ts"] = now
        return data


def invalidate() -> None:
    """整理台刚改完站序 / 刚确认一条线，想立刻生效就调它"""
    with _LOCK:
        _CACHE["data"] = None
        _CACHE["ts"] = 0.0
        _SIDX["data"] = None
        _SIDX["ts"] = -1.0
        _JOINT["data"] = None            # 线路所索引 / 链路缓存也跟着换版
        _JOINT["ts"] = -1.0
        _BRIDGE["data"] = {}
        _BRIDGE["ver"] = -1.0
        _BRIDGE["trs"] = None


def names() -> set:
    return set(index().keys())


def station_index() -> Dict[str, List[str]]:
    """{站名键: [含这个站的确认线路名…]}，**只留在服务端**，别整份下发前端。

    键用 `_key()`（去空白、去「站/所」后缀），和自动识别 / 缺口候选用的是同一套，
    所以「北京南站」「北京南」都能命中整理台站序里的「北京南」；线路所也照样在里。
    只收 `confirmed.names()` 里的线路，口径与 `lineNames` 完全一致：整理台每确认
    一条新线 / 补一批站，索引随 `_CACHE` 换版自动重建，不用维护任何名单。
    """
    with _LOCK:
        idx = index()                       # RLock 可重入
        ver = _CACHE["ts"]
        if _SIDX["data"] is not None and _SIDX["ts"] == ver:
            return _SIDX["data"]
        out: Dict[str, List[str]] = {}
        for nm, ent in idx.items():
            for k in ent.get("pos", {}):
                out.setdefault(k, []).append(nm)
        for v in out.values():
            v.sort()
        _SIDX["data"] = out
        _SIDX["ts"] = ver
        return out


def lines_for(stas: Sequence[str]) -> Dict[str, List[str]]:
    """{原样站名: [含这个站的确认线路名…]}，只问传入的这几个站。

    数据校对只要「本趟车这些站」的候选，返回量随车次站数走，不随全库线路 /
    车站数量膨胀；新增的线路 / 车站只要整理台确认过就自动进来。
    """
    idx = station_index()
    out: Dict[str, List[str]] = {}
    for s in stas or []:
        nm = str(s or "").strip()
        if not nm:
            continue
        out[nm] = list(idx.get(_key(nm)) or [])
    return out


def normalize(name: str) -> str:
    """把任何写法的线名对到确认线路的规范名；对不上返回空字符串

    认三种：完全同名、「某某线（OSM）/（底表）」这类去掉来源后缀后同名、
    整理台里已经确认过的别名（`ld_alias.ok=1`）。
    """
    nm = str(name or "").strip()
    if not nm:
        return ""
    idx = index()
    if nm in idx:
        return nm
    try:
        import linegroup
        base = linegroup.split(nm)[0]
    except Exception:                          # noqa: BLE001
        base = nm
    if base in idx:
        return base
    try:
        c = linedesk.conn()
        try:
            rows = c.execute(
                "SELECT l.name FROM ld_alias a JOIN ld_line l ON l.id = a.line_id"
                " WHERE a.ok = 1 AND a.name = ? AND l.status = 'confirmed'", (nm,)).fetchall()
        finally:
            c.close()
    except Exception:                          # noqa: BLE001
        rows = []
    for r in rows:
        if r[0] in idx:
            return r[0]
    return ""


# ---------------------------------------------------------------------------
# 自动识别：区间 → 确认线路
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 线路所：自动推断里的换线接续点
# ---------------------------------------------------------------------------
_JOINT: dict = {"ts": -1.0, "data": None}
_BRIDGE: dict = {"ver": -1.0, "data": {}}
_MAX_LEG = 4          # 一条缺口最多拆 4 段，别把全路网搜一遍
_MAX_STATE = 1500


def _is_joint(name: str) -> bool:
    """线路所 / 中继站：不办客，但相邻线路在这里接续（头道河所、获鹿所…）"""
    s = "".join(str(name or "").split())
    return ("线路所" in s) or s.endswith("所") or ("中继站" in s)


def joint_index() -> Dict[str, Dict[str, tuple]]:
    """{线路所键: {线名: (下标, 原站名)}}，只收确认线路站序里的线路所。

    整理台以后新增 / 改名线路所，这里跟着 index() 的版本自动重建，不写死名单。
    """
    with _LOCK:
        idx = index()
        ver = _CACHE["ts"]
        if _JOINT["data"] is not None and _JOINT["ts"] == ver:
            return _JOINT["data"]
        out: Dict[str, Dict[str, tuple]] = {}
        for nm, ent in idx.items():
            for p, sta in enumerate(ent.get("stops") or []):
                if _is_joint(sta):
                    out.setdefault(_key(sta), {})[nm] = (p, sta)
        _JOINT["data"] = out
        _JOINT["ts"] = ver
        return out


def _build_transfers(idx: dict) -> dict:
    """{线名: [(本线站序下标, 是否线路所, [(可换到的线, 那边的下标)…])]}

    关键点：**线路所只按精确站名换线**。`_key()` 会把「获鹿所」砍成「获鹿」，
    和同名普通站撞键；沿用旧口径 BFS 会拼出「石太线 获鹿→获鹿」这种自环。
    普通站仍按 `_key()` 口径共线换乘。
    """
    exact = {}
    for nm, ent in idx.items():
        m = {}
        for p, s in enumerate(ent["stops"]):
            m.setdefault(s, p)
        exact[nm] = m
    out = {}
    for nm, ent in idx.items():
        rows = []
        for p, s in enumerate(ent["stops"]):
            joint = _is_joint(s)
            k = _key(s)
            dests = []
            for nm2, ent2 in idx.items():
                if nm2 == nm:
                    continue
                p2 = exact[nm2].get(s) if joint else ent2["pos"].get(k)
                if p2 is not None:
                    dests.append((nm2, p2))
            if dests:
                rows.append((p, joint, dests[:12]))
        rows.sort(key=lambda r: (0 if r[1] else 1, r[0]))   # 线路所优先
        out[nm] = rows[:60]
    return out


def _bridge_chain(a: str, b: str, cover: Dict[str, int], prev: Optional[str]):
    """两站不共线时，沿确认线路站序找一条多段链路把它接起来。

    **只有经过至少一个线路所的链路才采用** —— 用户要的是「把线路所纳入自动
    计算经由」，普通的多线绕行仍留缺口给人工判断，免得自动认错一大片。
    换乘点在 `_build_transfers()` 里按精确下标算好，最多 4 段；结果按
    (a, b, 上一段线) 缓存，全库扫描时同一站对不会反复 BFS。
    """
    idx = index()
    if not idx or len(idx) > 80:
        return None
    jidx = joint_index()
    if not any(len(v) > 1 for v in jidx.values()):
        return None                          # 没有跨线线路所，不用搜
    sidx = station_index()
    la = [nm for nm in (sidx.get(a) or []) if nm in idx]
    lb = {nm for nm in (sidx.get(b) or []) if nm in idx}
    if not la or not lb:
        return None
    # 只在「起点 / 终点所在的线里真有跨线线路所」时才搜：
    # 全库扫描时绝大多数站对在这一步就被挡掉，不会每个缺口都跑 BFS
    jlines = {nm for v in jidx.values() if len(v) > 1 for nm in v}
    if not (jlines & (set(la) | lb)):
        return None
    if _BRIDGE["ver"] != _CACHE["ts"]:
        _BRIDGE["ver"] = _CACHE["ts"]
        _BRIDGE["data"] = {}
        _BRIDGE["trs"] = None
    cache = _BRIDGE["data"]
    ck = (a, b, prev)
    if ck in cache:
        return cache[ck]
    if _BRIDGE.get("trs") is None:
        _BRIDGE["trs"] = _build_transfers(idx)
    trs = _BRIDGE["trs"]
    from collections import deque
    q = deque()
    for nm in la:
        pa = idx[nm]["pos"].get(a)
        if pa is not None:
            q.append((nm, pa, (), 0))
    seen = set()
    best = None
    steps = 0
    while q and steps < _MAX_STATE:
        steps += 1
        line, p, legs, nj = q.popleft()
        if len(legs) >= _MAX_LEG:
            continue
        ent = idx[line]
        if line in lb:                       # 能直接到 b 就收尾，短链路优先
            pb = ent["pos"].get(b)
            if pb is not None and pb != p:
                cand = legs + ((line, p, pb),)
                if nj > 0:
                    score = (len(cand), 0 if cand[0][0] == prev else 1,
                             sum(abs(t - f) for _l, f, t in cand),
                             -sum(cover.get(l, 0) for l, _f, _t in cand),
                             tuple(l for l, _f, _t in cand))
                    if best is None or score < best[0]:
                        best = (score, cand)
                continue
        for q0, isj, dests in trs[line]:
            if q0 == p:
                continue
            nleg = legs + ((line, p, q0),)
            for nm2, p2 in dests:
                st = (nm2, p2)
                if st in seen:
                    continue
                seen.add(st)
                q.append((nm2, p2, nleg, nj + (1 if isj else 0)))
    out = None
    if best:
        out = [{"line": l, "from": idx[l]["stops"][f], "to": idx[l]["stops"][t],
                "src": "confirmed", "joint": True}
               for l, f, t in best[1]]
        if any(s["from"] == s["to"] for s in out):    # 撞名自环，宁可留缺口
            out = None
    if len(cache) > 4000:
        cache.clear()
    cache[ck] = out
    return out


def infer(stops: Sequence[dict]) -> dict:
    """按确认线路的站序推断整趟车的经由，输出结构和 `cnrail.infer_via` 一致

    对每一对相邻站，先看有没有确认线路同时含这两站（站序里位置不同），
    再在候选里挑：

      1. 和上一段同一条线（连续性，缺口多半是同一趟车换线才断的）
      2. 覆盖这趟车经停站最多的线（京广线覆盖 20 站，某条支线只覆盖 2 站）
      3. 两站在站序里隔得最近的线
      4. 站数多的线

    都不共线时，再试「经线路所换线」的多段链路（`_bridge_chain`）：
    Z75 的 石获线→获鹿所→石太客专线、N1534 的 南疆线→头道河所→大河沿联络线，
    以前只能人工填，现在自动推断也能拆出来。链路里没有线路所的仍算缺口。
    """
    idx = index()
    empty = {"segs": [], "unknown": [], "pairs": 0, "resolved": 0,
             "coverage": 0.0, "sources": [], "bridged": 0, "joints": []}
    if not idx or len(stops or []) < 2:
        return empty

    keys = [_key((s or {}).get("name") or "") for s in stops]
    # 出门用 12306 的站名（原样），只有比对时才用 _key —— 否则「春申南所」会被
    # 砍成「春申南」，人工列表按站名排序时就对不上这趟车的站表了
    names = [str((s or {}).get("name") or "") for s in stops]
    pairs = list(zip(keys, keys[1:]))
    cover = {nm: sum(1 for k in keys if k in d["pos"]) for nm, d in idx.items()}

    chosen: List[Optional[str]] = []
    bridges: List[Optional[List[dict]]] = []
    prev: Optional[str] = None
    for a, b in pairs:
        best = None
        for nm, d in idx.items():
            ia = d["pos"].get(a)
            ib = d["pos"].get(b)
            if ia is None or ib is None or ia == ib:
                continue
            rank = (0 if nm == prev else 1, -cover[nm], abs(ib - ia), -d["n"], nm)
            if best is None or rank < best[0]:
                best = (rank, nm)
        if best:
            chosen.append(best[1]); bridges.append(None); prev = best[1]
        else:
            link = _bridge_chain(a, b, cover, prev)
            chosen.append(None); bridges.append(link)
            prev = link[-1]["line"] if link else None

    segs: List[dict] = []
    for i, nm in enumerate(chosen):
        if nm:
            parts = [{"line": nm, "from": names[i], "to": names[i + 1],
                      "src": "confirmed", "i0": i}]
        elif bridges[i]:
            parts = [dict(x, i0=i) for x in bridges[i]]
        else:
            parts = []
        for p in parts:
            if segs and segs[-1]["line"] == p["line"] and segs[-1]["to"] == p["from"]:
                segs[-1]["to"] = p["to"]
            else:
                segs.append(dict(p))

    unknown: List[dict] = []
    for i, nm in enumerate(chosen):
        if nm or bridges[i]:
            continue
        a, b = names[i], names[i + 1]
        if unknown and unknown[-1]["to"] == a:
            unknown[-1]["to"] = b
        else:
            unknown.append({"from": a, "to": b, "i0": i})

    resolved = sum(1 for c, l in zip(chosen, bridges) if c or l)
    joints = sorted({s["to"] for l in bridges if l for s in l
                     if _is_joint(s.get("to") or "")})
    return {
        "segs": segs,
        "unknown": unknown,
        "pairs": len(pairs),
        "resolved": resolved,
        "coverage": round(100.0 * resolved / len(pairs), 1) if pairs else 0.0,
        "sources": ["confirmed"] if resolved else [],
        "bridged": sum(1 for l in bridges if l),
        "joints": joints,
    }


def cover_of(name: str, stops: Sequence[str]) -> int:
    """这条确认线路覆盖了这趟车多少个经停站（缺口候选排序用）"""
    ent = index().get(str(name or "").strip())
    if not ent:
        return 0
    pos = ent["pos"]
    return sum(1 for s in stops or [] if _key(s) in pos)


def cands(a: str, b: str, limit: int = 8) -> List[dict]:
    """缺口两端的候选线路：只给确认线路

    「两站都在」的排前面（真正的答案通常在这一档），只剩一头的垫后并标明，
    方便人工判断是不是线路站序缺了站。
    """
    idx = index()
    ka, kb = _key(a), _key(b)
    both: List[dict] = []
    one: List[dict] = []
    for nm, d in idx.items():
        ia = d["pos"].get(ka)
        ib = d["pos"].get(kb)
        item = {"line": nm, "n": d["n"], "totalKm": d.get("totalKm"), "custom": False}
        if ia is not None and ib is not None and ia != ib:
            both.append(dict(item, src="confirmed", srcCn="确认线路（两站都在）",
                             gap=abs(ib - ia)))
        elif ia is not None or ib is not None:
            one.append(dict(item, src="confirmed", srcCn="确认线路（只含一端）",
                            gap=None))
    both.sort(key=lambda x: (x.get("gap") or 0, -(x.get("n") or 0), x["line"]))
    one.sort(key=lambda x: (-(x.get("n") or 0), x["line"]))
    return (both + one)[:max(1, int(limit))]
