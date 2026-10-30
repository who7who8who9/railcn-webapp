# -*- coding: utf-8 -*-
"""
lineseg.py — 站序与 OSM 走向互相补全

线名在两个数据源里对不齐是常态：

  · 线路库按「段」命名，OSM 只有整条：成昆线成攀段 / 成昆线元昆段 ↔ 成昆线
  · OSM 有整条几何，线路库里压根没有这条线：成昆线、瓦日铁路、格库线

于是「有站没走向」「有走向没站」各占一批。这里给两边补上缺的那一半：

  borrow_geometry(段名)   借父线几何，按本段首末站裁出那一截（裁不出就退整条并说明）
  stations_for(OSM 线名)  从线路资料库取同名站序；资料库没有再按同名前缀的分段拼

**只在查询时补，不往库里写**：几何是站序的派生物，站序还会被人工改；
缓存也只放在进程里，服务一重启就按最新的站序重算。

裁剪为什么不用 railgeom._chain：那条链化按「接完这段会不会离起点更近」判复线，
长干线（京哈 1210 km）上会把它该接的段全当复线丢掉，成昆线只串出 313 km。
这里改成「复线去重 + 端点缝合成图 + 走最短路」，断口按直线补并如实记数。
"""
from __future__ import annotations

import heapq
import math
import re
import threading
from typing import Dict, List, Optional, Sequence, Tuple

import cnrail
import railcore
import railgeom

EARTH_R = 6371.0088
SNAP_KM = 0.08          # 相邻折点合并半径（80 m）
DUP_KM = 0.35           # 判上下行复线的端点容差
LINK_STEPS = (8.0, 25.0, 60.0, 200.0)   # 断口缝合距离，由紧到松逐级试

_SEG_RE = re.compile(r"^(.+?(?:铁路|线))([\u4e00-\u9fa5]{1,8})段$")
_NET_CACHE: Dict[str, "_Net"] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 24


# ---------------------------------------------------------------------------
# 线名拆解
# ---------------------------------------------------------------------------

def split(name: str) -> Optional[Tuple[str, str]]:
    """「成昆线元昆段」→ ('成昆线', '元昆')；不是分段名返回 None

    段名限定 1-4 个汉字、并以「线 / 铁路」收尾，才不会把「京九线」这种
    名字本身以「段」结尾的怪名拆错。
    """
    n = re.sub(r"\s+", "", str(name or ""))
    m = _SEG_RE.match(n)
    if not m:
        return None
    parent, seg = m.group(1), m.group(2)
    return (parent, seg) if parent and seg else None


def _hav(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp = p2 - p1
    dl = math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(max(0.0, min(1.0, h))))


# ---------------------------------------------------------------------------
# OSM 几何 → 可走网络
# ---------------------------------------------------------------------------

class _Net:
    """一条 OSM 线的可走网络：段内折线是实边，端点缝合是断口虚拟边

    一次构图、多次查询。缝合边不预先加进邻接，`route()` 按传入的 link_km
    现场取用 —— 同一张图既能走「只接 8 km 内断口」的严格模式，也能放宽到 60 km。
    """

    def __init__(self, parts: Sequence[Sequence[Sequence[float]]]):
        self.pts: List[Tuple[float, float]] = []      # (lat, lon)
        self.adj: List[List[Tuple[int, float]]] = []  # 段内邻接
        self.links: List[Tuple[float, int, int]] = []  # (km, i, j) 断口
        self.n_raw = 0
        self.dup_drop = 0

        segs: List[List[Tuple[float, float]]] = []
        for p in parts or []:
            pts = [(float(q[1]), float(q[0])) for q in p]
            if len(pts) >= 2:
                segs.append(pts)
        self.n_raw = len(segs)
        segs = self._dedup(segs)
        self.dup_drop = self.n_raw - len(segs)

        grid: Dict[Tuple[int, int], List[int]] = {}

        def nid(pt: Tuple[float, float]) -> int:
            cy, cx = int(math.floor(pt[0] / 0.01)), int(math.floor(pt[1] / 0.01))
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    for i in grid.get((cy + dy, cx + dx), ()):
                        if _hav(self.pts[i], pt) <= SNAP_KM:
                            return i
            self.pts.append(pt)
            self.adj.append([])
            grid.setdefault((cy, cx), []).append(len(self.pts) - 1)
            return len(self.pts) - 1

        for line in segs:
            prev = None
            for p in line:
                cur = nid(p)
                if prev is not None and cur != prev:
                    w = _hav(self.pts[prev], self.pts[cur])
                    self.adj[prev].append((cur, w))
                    self.adj[cur].append((prev, w))
                prev = cur

        # 端点缝合：断口是真实存在的轨道没被映射（隧道群、桥），按直线连上，
        # 里程照算 —— 反正这一段本来就没有几何可画。
        #
        # 不能「每个端点就近连 6 个」：一条线常被切成十来个互不相连的分量，
        # 小分量的最近邻全在同一条大分量上，它自己永远连不出去（宁西线 7 个
        # 分量、断口都在 52 km 内却走不通，就是这么卡的）。改成先算分量，
        # 分量之间取最近的一对端点做候选边，再跑最小生成树 —— 分量图一定连通。
        ends = [i for i in range(len(self.pts)) if len(self.adj[i]) == 1]
        self.links = self._stitch(ends)

    def _stitch(self, ends: List[int]) -> List[Tuple[float, int, int]]:
        """分量间最近端点对 → 最小生成树；返回 [(km, i, j)]，按距离排序"""
        n = len(self.pts)
        if n == 0:
            return []
        comp = [-1] * n
        cid = 0
        for i in range(n):
            if comp[i] >= 0:
                continue
            stack = [i]
            comp[i] = cid
            while stack:
                u = stack.pop()
                for v, _ in self.adj[u]:
                    if comp[v] < 0:
                        comp[v] = cid
                        stack.append(v)
            cid += 1
        if cid <= 1:
            return []
        # 每个分量取端点；没有端点（闭环）就拿全体点当候选
        pts_of: Dict[int, List[int]] = {}
        for c in range(cid):
            pts_of[c] = []
        for i in ends:
            pts_of[comp[i]].append(i)
        for c in range(cid):
            if not pts_of[c]:
                pts_of[c] = [i for i in range(n) if comp[i] == c]

        # 分量两两之间最近的一对
        pair: List[Tuple[float, int, int]] = []
        for a in range(cid):
            for b in range(a + 1, cid):
                best = None
                for i in pts_of[a]:
                    for j in pts_of[b]:
                        d = _hav(self.pts[i], self.pts[j])
                        if best is None or d < best[0]:
                            best = (d, i, j)
                if best:
                    pair.append(best)
        pair.sort()

        # Kruskal：先接最近的，接完跳过分量已连通的那些
        parent = list(range(cid))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        out: List[Tuple[float, int, int]] = []
        for d, i, j in pair:
            ra, rb = find(comp[i]), find(comp[j])
            if ra == rb:
                continue
            parent[ra] = rb
            out.append((d, i, j))
        out.sort()
        return out

    @staticmethod
    def _dedup(segs: List[List[Tuple[float, float]]]) -> List[List[Tuple[float, float]]]:
        """上下行复线：起终点都对得上（正向或反向）就只留长的那条"""
        def length(s: List[Tuple[float, float]]) -> float:
            return sum(_hav(s[i - 1], s[i]) for i in range(1, len(s)))
        kept: List[List[Tuple[float, float]]] = []
        for s in sorted(segs, key=length, reverse=True):
            dup = False
            for k in kept:
                for rev in (False, True):
                    q = k[::-1] if rev else k
                    if _hav(s[0], q[0]) <= DUP_KM and _hav(s[-1], q[-1]) <= DUP_KM:
                        dup = True
                        break
                if dup:
                    break
            if not dup:
                kept.append(s)
        return kept

    def near(self, pt: Tuple[float, float]) -> Tuple[int, float]:
        """离这个坐标最近的折点（下标, 距离 km）"""
        best, bd = 0, 1e18
        for i, q in enumerate(self.pts):
            d = _hav(q, pt)
            if d < bd:
                best, bd = i, d
        return best, bd

    def route(self, s: int, t: int, link_km: float) -> Optional[Tuple[float, List[int], int]]:
        """s → t 的最短路，返回 (里程, 点序, 断口数)"""
        dist: Dict[int, float] = {s: 0.0}
        prev: Dict[int, Tuple[int, int]] = {}
        pq: List[Tuple[float, int]] = [(0.0, s)]
        seen = set()
        links = [x for x in self.links if x[0] <= link_km]
        while pq:
            d, u = heapq.heappop(pq)
            if u in seen:
                continue
            seen.add(u)
            if u == t:
                break
            for v, w in self.adj[u]:
                nd = d + w
                if nd < dist.get(v, 1e18):
                    dist[v] = nd
                    prev[v] = (u, 0)
                    heapq.heappush(pq, (nd, v))
            for w, a, b in links:
                if a == u or b == u:
                    v = b if a == u else a
                    nd = d + w
                    if nd < dist.get(v, 1e18):
                        dist[v] = nd
                        prev[v] = (u, 1)
                        heapq.heappush(pq, (nd, v))
        if t not in dist:
            return None
        path = [t]
        cur, gaps = t, 0
        while cur != s:
            cur, gap = prev[cur]
            gaps += gap
            path.append(cur)
        path.reverse()
        return dist[t], path, gaps


def _net(osm_name: str) -> Optional[_Net]:
    with _CACHE_LOCK:
        hit = _NET_CACHE.get(osm_name)
    if hit is not None:
        return hit
    parts = railgeom._geoms().get(osm_name) or []
    if not parts:
        return None
    net = _Net(parts)
    with _CACHE_LOCK:
        if len(_NET_CACHE) >= _CACHE_MAX:
            _NET_CACHE.pop(next(iter(_NET_CACHE)), None)
        _NET_CACHE[osm_name] = net
    return net


# ---------------------------------------------------------------------------
# 方向一：库里只有站序 → 借 OSM 的走向
# ---------------------------------------------------------------------------

def borrow_geometry(name: str, stops: Optional[Sequence[dict]] = None,
                    expect_km: Optional[float] = None) -> Optional[dict]:
    """分段线借父线的 OSM 几何，按本段首末站裁出那一截

    stops 给了坐标就裁；裁不出来（几何断成好几截、站名坐标明显不对）就退整条，
    并写清原因 —— 有整条总比没有强，前端照旧画得出来。
    """
    sp = split(name)
    if not sp:
        return None
    parent = sp[0]
    g = railgeom.geom(parent)
    if not g or not g.get("parts"):
        return None

    # 父线名和 OSM 里的线名常常不是同一个（中老昆万铁路 ↔ 中老昆万线、
    # 银西高速线 ↔ 银西高铁）：后面建网、取几何都得用命中后的那个名字
    osm_name = g.get("name") or parent

    out: Dict[str, object] = {
        "name": osm_name, "ask": name, "parent": parent, "segment": sp[1],
        "km": g.get("km"), "kmMain": g.get("kmMain"),
        "nPart": g.get("nPart"), "nFrag": g.get("nFrag"),
        "nBridge": g.get("nBridge"), "nTunnel": g.get("nTunnel"),
        "kmChain": g.get("kmChain"), "match": g.get("match", "exact"),
        "borrowed": True, "clipped": False,
        "parts": g["parts"], "crs": "WGS84",
    }

    pts = [s for s in (stops or []) if s.get("lat") is not None and s.get("lon") is not None]
    if len(pts) < 2:
        out["why"] = "这条段没有首末站坐标，借用整条走向"
        return out

    net = _net(osm_name)
    if net is None:
        out["why"] = "OSM 里有这条线的元信息，但取不到折线"
        return out
    a, b = (float(pts[0]["lat"]), float(pts[0]["lon"])), (float(pts[-1]["lat"]), float(pts[-1]["lon"]))
    s, ds = net.near(a)
    t, dt = net.near(b)
    far = max(ds, dt)
    if far > 30:
        gkm = g.get("km") or 0
        if expect_km and gkm and gkm < expect_km * 0.6:
            # 父线在 OSM 里只有一小截（川藏铁路 40 km），本段根本画不进去
            out["why"] = ("「%s」在 OSM 里只画了 %.0f km，本段却 %.0f km，覆盖不到；"
                          "首末站离它 %.0f km" % (osm_name, gkm, expect_km, far))
        else:
            out["why"] = "首末站离这条 OSM 线 %.0f km，站名坐标可能指错了地方" % far
        return out

    best = None
    for link in LINK_STEPS:
        r = net.route(s, t, link)
        if not r:
            continue
        km, path, gaps = r
        score = abs(km - expect_km) / expect_km if expect_km else 0.0
        if best is None or score < best[0]:
            best = (score, km, path, gaps, link)
        if not expect_km or score <= 0.25:
            break
    if not best:
        out["why"] = "这条 OSM 线在几何上断成了互不相连的几截，走不通"
        return out

    score, km, path, gaps, link = best
    if expect_km and score > 0.25:
        out["why"] = ("沿几何走一程要 %.0f km，比档案的 %.0f km 差得太多，"
                      "大概借错了线" % (km, expect_km))
        return out

    line = [[net.pts[i][1], net.pts[i][0]] for i in path]   # (lat,lon) → [lon,lat]
    gkm = g.get("km") or 0
    out.update({
        "parts": [line], "clipped": True,
        "km": round(km, 1), "kmMain": round(km, 1),
        "nPart": 1, "nFrag": 1, "nPt": len(path),
        "clip": {"from": pts[0].get("name"), "to": pts[-1].get("name"),
                 "km": round(km, 1), "expectKm": expect_km,
                 "gaps": gaps, "linkKm": link,
                 "offFrom": round(ds, 1), "offTo": round(dt, 1),
                 # 裁出来几乎就是整条（酒额线酒东段 240.9 / 整条 241.9）：没有可裁的余量
                 "full": bool(gkm and km >= gkm * 0.97)},
    })
    # 桥隧数按里程比例折算：整条线的统计值直接挂在半条线上会误导
    if expect_km and g.get("km"):
        ratio = min(1.0, km / float(g["km"]))
        for k in ("nBridge", "nTunnel"):
            if g.get(k):
                out[k] = int(round(g[k] * ratio))
    return out


# ---------------------------------------------------------------------------
# 方向二：OSM 有走向、库里没有站序 → 补站序
# ---------------------------------------------------------------------------

def _canon(name: str) -> str:
    """资料库站名统一成 12306 口径：「碧鸡关站」→「碧鸡关」

    cnrail.station_name 只在「短名出现在线路库」时截后缀，资料库这批站名
    常常不在里面（碧鸡关、读书铺），这里再用车站档案兜一次。
    像「十八站」「新九站」这种地名本身带「站」的，截短后档案里查不到，不会被动。
    """
    nm = cnrail.station_name(name)
    if nm.endswith("站") and len(nm) > 1 and cnrail.station(nm[:-1]):
        return nm[:-1]
    return nm


def _catalog_stops(name: str) -> Optional[dict]:
    """线路资料库里同名（或同标题）那条的站序"""
    rec = cnrail.catalog_get(name)
    if not rec:
        return None
    raw = rec.get("stops") or []
    stops: List[dict] = []
    for i, s in enumerate(raw):
        nm = _canon(s.get("name") or "")
        if not nm:
            continue
        c = cnrail.coord(nm)
        stops.append({"name": nm, "km": s.get("km"),
                      "lat": c[0] if c else None, "lon": c[1] if c else None,
                      "seq": i + 1})
    if len(stops) < 2:
        return None
    return {"stops": stops, "source": "catalog",
            "sourceName": rec.get("name"), "sourceId": rec.get("id"),
            "totalKm": rec.get("length_km")}


# 「铁路 / 线 / 高铁 …」这些尾巴不影响线路身份
_TAILS = ("高速铁路", "客运专线", "城际铁路", "高速线", "客专线", "铁路", "高铁", "线")
_HS_RE = re.compile(r"高速|客专|客运专线|城际|高铁")


def _core_name(name: str) -> str:
    """去掉线名的速度/制式尾缀：京沪高速铁路 → 京沪，京沪线 → 京沪"""
    s = re.sub(r"\s+", "", name or "")
    changed = True
    while changed:
        changed = False
        for t in _TAILS:
            if len(s) > len(t) and s.endswith(t):
                s = s[: -len(t)]
                changed = True
                break
    return s


def match_key(name: str) -> str:
    """跨来源的线名比对键：高铁/普速分开，分段线拆成「父线|段名」

    京哈高速铁路京沈段 与 京哈高速线京沈段 → 都是 H京哈|京沈
    京沪高速铁路 与 京沪高铁 → 都是 H京沪；京沪铁路 → P京沪（不混）
    与 scripts/import_chinaemu.py 的 match_key 必须保持一致，否则 join 不上。
    """
    hs = "H" if _HS_RE.search(name or "") else "P"
    sp = split(name)
    if sp:
        return hs + _core_name(sp[0]) + "|" + sp[1]
    return hs + _core_name(name)


_CE_BRACKET = re.compile(r"\[[^\]]*\]\s*$")


def _ce_name(name: str) -> str:
    """china-emu 站名清洗：「小井村1#线路所[上行]」→「小井村1#线路所」

    [上行] / [下行] 是它把上下行分列成两行时加的标记，[南车场] 是车场说明。
    洗完仍可能带「线路所」，那是真实节点，_canon 会按档案决定要不要截。
    """
    s = _CE_BRACKET.sub("", str(name or "")).strip()
    if not s:
        return ""
    s = re.sub(r"\s+", "", s)
    return _canon(s)


def _ce_stops(name: str) -> Optional[dict]:
    """china-emu（第五套来源）的站序。

    它在「有走向没站序」的线名上补得比资料库广：兰新线 118 站、京沪线 160 站、
    沪昆高速线 67 站都是资料库没有的。所以排在资料库之后、分段拼接之前。
    """
    try:
        got = railcore.ce_stops(name, match_key(name))
    except Exception:                        # noqa: BLE001  新表缺失时静默降级
        return None
    if not got:
        return None
    stops: List[dict] = []
    for i, s in enumerate(got.get("stops") or []):
        nm = _ce_name(s.get("name"))
        if not nm:
            continue
        c = cnrail.coord(nm)
        stops.append({"name": nm, "km": s.get("km"), "kind": s.get("kind"),
                      "lat": c[0] if c else None, "lon": c[1] if c else None,
                      "seq": len(stops) + 1})
    if len(stops) < 2:
        return None
    return {"stops": stops, "source": "chinaemu",
            "sourceName": got.get("name"), "totalKm": got.get("totalKm"),
            "kmDesc": got.get("kmDesc") or 0}


# 长度判不出高下时按这个顺序要：资料库最权威，china-emu 次之（含线路所与计价里程），
# OSM 最后（覆盖最广但站是从几何插值出来的）
_SRC_RANK = {"catalog": 0, "chinaemu": 1, "osm": 2}


def _osm_stops(name: str) -> Optional[dict]:
    """OSM（第六套来源）的线路站序。

    三个来源里它补得最广：1727 条线、11788 个站次，专治「资料库和 china-emu
    都没有」的线（瓦日线 44 站、嫩林线 43 站、红淖线 23 站）。

    它的站序是从几何图上算出来的 —— 车站插值到最近的边上，所以会带进并线枢纽站
    （role=secondary，京广线上能看到白云湖、江高镇），也没有 china-emu 那种
    线路所节点。好在**每行自带坐标**，补出来的线不用依赖别人反查。
    """
    try:
        got = railcore.osm_stops(name, match_key(name))
    except Exception:                        # noqa: BLE001  新表缺失时静默降级
        return None
    if not got:
        return None
    stops: List[dict] = []
    seen = set()
    for s in got.get("stops") or []:
        nm = _canon(s.get("name"))
        if not nm or nm in seen:
            continue
        seen.add(nm)
        c = cnrail.coord(nm)
        stops.append({"name": nm, "km": s.get("km"), "role": s.get("role"),
                      "lat": c[0] if c else s.get("lat"),
                      "lon": c[1] if c else s.get("lon"),
                      "seq": len(stops) + 1})
    if len(stops) < 2:
        return None
    return {"stops": stops, "source": "osm", "sourceName": got.get("name"),
            "totalKm": got.get("totalKm"), "lineType": got.get("lineType"),
            "bridges": got.get("bridges") or 0}


def _segments_of(parent: str) -> List[dict]:
    """库里以这个父线名开头的分段线（成昆线 → 成攀段、元昆段）"""
    out = []
    for it in cnrail.search_line("", 2000):
        sp = split(it["name"])
        if sp and sp[0] == parent:
            out.append(it)
    return out


def _chain_segments(parent: str, segs: List[dict]) -> Optional[dict]:
    """把同前缀的分段按接缝串成一条站序

    只认「上一段末站 = 下一段首站（或相距 ≤ 25 km）」的硬接缝：京港高速线
    的商合段（合肥北城止）与赣深段（赣州西起）中间隔着 407 km 的合安、昌赣，
    拼起来是假的，宁可报「中间缺一段」也不硬接。
    """
    info = []
    for it in segs:
        names = cnrail.line_station_names(it["name"]) or []
        if len(names) < 2:
            continue
        c0, c1 = cnrail.coord(names[0]), cnrail.coord(names[-1])
        info.append({"line": it["name"], "names": names, "totalKm": it.get("totalKm"),
                     "head": c0, "tail": c1})
    if not info:
        return None

    def join_km(a: dict, b: dict) -> Optional[float]:
        if not a["tail"] or not b["head"]:
            return None
        d = _hav(a["tail"], b["head"])
        return d if d <= 25 else None

    # 起段：没有别的段能接到它前面
    starts = [x for x in info if not any(join_km(y, x) is not None for y in info if y is not x)]
    if len(starts) != 1:
        return None
    order = [starts[0]]
    left = [x for x in info if x is not starts[0]]
    while left:
        nxt = None
        for x in left:
            if join_km(order[-1], x) is not None:
                nxt = x
                break
        if nxt is None:
            break
        order.append(nxt)
        left.remove(nxt)
    if left:
        return {"stops": [], "source": "parts", "parts": [o["line"] for o in order],
                "missing": [o["line"] for o in left],
                "why": "「%s」和「%s」中间隔着 %.0f km，接不上"
                       % (order[-1]["line"], left[0]["line"],
                          _hav(order[-1]["tail"], left[0]["head"]) if order[-1]["tail"] and left[0]["head"] else 0)}

    stops: List[dict] = []
    for seg in order:
        for nm in seg["names"]:
            if stops and stops[-1]["name"] == nm:      # 接缝站只留一个
                continue
            c = cnrail.coord(nm)
            stops.append({"name": nm, "km": None,
                          "lat": c[0] if c else None, "lon": c[1] if c else None,
                          "seg": seg["line"]})
    total = sum(float(o.get("totalKm") or 0) for o in order) or None
    return {"stops": stops, "source": "parts",
            "parts": [o["line"] for o in order], "totalKm": total}


def _lib_stops(name: str, source: str = "") -> Optional[dict]:
    """线路库里这条线自己的站序（含人工 override / 自建站表）"""
    ln = cnrail.line(name)
    st = (ln or {}).get("stations") or []
    if len(st) < 2:
        return None
    kind = source or ("custom" if ln.get("custom") else "lib")
    return {"stops": [{"name": s.get("name"), "km": s.get("km"),
                       "seq": i + 1, "lat": s.get("lat"), "lon": s.get("lon")}
                      for i, s in enumerate(st)],
            "source": kind, "sourceName": ln.get("name"),
            "totalKm": ln.get("totalKm")}


def _reverse_stops(stops: List[dict]) -> List[dict]:
    """站序倒过来，里程按「原末站里程 - 原里程」重排，新起点归零

    不能只把数组反过来：那样末站里程会是 0，中间站全在倒着走。里程缺失的
    站仍然是缺失，不插值（资料库/OSM 里本来就有没填里程的站）。
    """
    kms = [s.get("km") for s in stops if s.get("km") is not None]
    base = max(kms) if kms else None
    out: List[dict] = []
    for i, s in enumerate(reversed(stops)):
        km = s.get("km")
        if base is None:
            v = None
        elif i == 0:
            # 倒过来以后原来的终点就是起点，里程必然是 0
            v = 0.0
        elif km is not None:
            v = round(base - float(km), 1)
        else:
            v = None
        out.append({**s, "km": v})
    return out


def _apply_src_fix(name: str, got: Optional[dict]) -> Optional[dict]:
    """把「这个来源被人工倒排过」的标记应用到刚取到的那份站序上

    关键是**只在读的时候翻转**：`line_osm_stop` / 资料库 / china-emu 里的原始
    站序一个字节都不改，所以「只调 OSM 京沪线」不会波及其它来源，取消标记就
    原样回去。
    """
    if not got or not got.get("stops"):
        return got
    src = got.get("source") or ""
    try:
        fix = railcore.src_fix(name, src)
    except Exception:                        # noqa: BLE001
        fix = []
    if "reverse" not in fix:
        return got
    out = dict(got)
    out["stops"] = _reverse_stops(list(got["stops"]))
    out["reversed"] = True
    out["srcFix"] = ["reverse"]
    return out


def stations_for(name: str, source: str = "") -> Optional[dict]:
    """OSM 线名 → 站序

    **source 指定来源时只取那一份，取不到就返回 None** —— 界面上同一条线有几个
    来源的说法（线路库 89 站 / china-emu 154 站 / OSM 220 站），让用户自己点，
    系统不再替他挑。source 为空才是「给个默认的」。

    默认规则：
      1. 资料库和 china-emu 都用走向长度当裁判，取总长更接近的那个：
           兰新线      OSM 2362 km：资料库给它 兰中城际线（49 km，错配）→ china-emu 兰新铁路（2423 km）✓
           沪昆高速线  OSM 2054 km：资料库给它 沪杭高铁（158 km，错配）→ china-emu 沪昆高速铁路（2252 km）✓
           成昆线      OSM 1022 km：资料库 成昆线（1096 km）✓
      2. 这两份都没有，才用 OSM 的（1727 条线里 1116 条只有它有站序）。
      3. 还不行就拿同前缀的分段线拼。

    **OSM 不参加长度裁判**：它的线名是按 way 的 name 聚合的，拆段命名的线只算
    自己名下那截（成昆线 661 km 对实际 1096，另有一条「成昆复线」），拿它比长度
    会把资料库对的整条判成错；railgeom 的老走向长度同样偏短（大秦线 463 对 653）。
    """
    name = re.sub(r"\s+", "", str(name or ""))
    if not name:
        return None

    # 用户在界面上勾过的默认来源优先（「这条线用哪一份站序」是他自己定的，
    # 系统规则不该再抢）。source 明确指定时以参数为准，只当这一次查看用。
    picked = ""
    if not source:
        picked = railcore.src_pick(name)
        if picked:
            source = picked

    if source:
        fn = {"catalog": _catalog_stops, "chinaemu": _ce_stops,
              "osm": _osm_stops}.get(source)
        # lib / custom 本来就是库内线路，直接从线路库那条取，口径与 line_sources
        # 完全对称 —— 界面上点哪个来源的徽标都能按那个来源给。
        if source in ("lib", "custom"):
            got = _lib_stops(name, source)
        elif source == "parts":
            # 「分段拼」也是一种来源，勾成默认以后要能原样取回来
            segs = _segments_of(name)
            got = _chain_segments(name, segs) if segs else None
        else:
            got = fn(name) if fn else None
        if got:
            got["line"] = name
            got["pickWhy"] = "default" if picked else "explicit"
            got["pickedByUser"] = bool(picked)
            got["pickFrom"] = [{"source": source, "name": got.get("sourceName"),
                                "km": got.get("totalKm")}]
            return _apply_src_fix(name, got)
        # 勾的默认来源现在取不到了（线名改了、来源数据重建过）：退回系统规则，
        # 别因为一个过期的勾选把整条线判成「查不到」
        if picked:
            source = ""
        else:
            return got

    cands: List[dict] = []
    a = _catalog_stops(name)
    if a:
        cands.append(a)
    b = _ce_stops(name)
    if b:
        cands.append(b)

    if not cands:
        # 顺序是有讲究的：**先拿自有线段拼，最后才用 OSM**。
        # 京哈高速线在项目里是「京沈段 + 沈哈段」两段人工整理过的线，接起来 37 站、
        # 口径和线路库其余部分一致；OSM 那份是几何上插值出来的，站数差别很大。
        # 既然定了 OSM 是兜底来源，就得排在自分段拼接之后。
        segs = _segments_of(name)
        if segs:
            got = _chain_segments(name, segs)
            if got:
                got["line"] = name
                return _apply_src_fix(name, got)
        c = _osm_stops(name)
        if c:
            c["line"] = name
            return _apply_src_fix(name, c)
        return None

    if len(cands) > 1:
        ref = (railgeom.meta(name) or {}).get("km") or 0
        if ref > 20:
            # 相对偏差：谁的总长更贴 OSM 走向就选谁
            cands.sort(key=lambda c: abs((c.get("totalKm") or 0) - ref) / ref)
            best, other = cands[0], cands[1]
            d0 = abs((best.get("totalKm") or 0) - ref) / ref
            d1 = abs((other.get("totalKm") or 0) - ref) / ref
            # 差距不明显（<15%）就按老规矩要资料库那份，别无事生非
            if d1 - d0 < 0.15:
                best = a or best
            best["pickWhy"] = "osmKm"
            best["pickFrom"] = [{"source": c.get("source"), "name": c.get("sourceName"),
                                 "km": c.get("totalKm")} for c in cands]
            cands = [best] + [c for c in cands if c is not best]

    got = cands[0]
    got["line"] = name
    return _apply_src_fix(name, got)


# ---------------------------------------------------------------------------
# 拼成一条能查的线路：站序（补来的）+ 走向（OSM 的）
# ---------------------------------------------------------------------------

def compose_line(name: str, source: str = "") -> Optional[dict]:
    """给一个「OSM 有几何、线路库里没有」的线名，拼出一条完整的线路

    站序从线路资料库取（成昆线 133 站），取不到拿 china-emu、再不行拿 OSM 的
    （瓦日线 44 站），最后才拿同前缀的分段站序拼。
    `source` 指定时只用那一份 —— 界面上同一条线有几个来源的说法，用户点了哪个就给哪个。
    返回结构与 cnrail.line_detail 对齐，前端不用另写一套渲染。
    补不到站序就返回 None —— 只有一条光走向，画成线路会是一列空站。
    """
    got = stations_for(name, source)
    if not got or not got.get("stops"):
        return None
    raw = got["stops"]
    stations: List[dict] = []
    seg: List[Optional[float]] = []
    for i, s in enumerate(raw):
        nm = _canon(s.get("name") or "")
        c = (s.get("lat"), s.get("lon"))
        if c[0] is None:
            cc = cnrail.coord(nm)
            c = cc if cc else (None, None)
        st = cnrail.station(nm) or {}
        stations.append({
            "name": nm, "km": s.get("km"), "kmBase": s.get("km"),
            "kmSrc": got.get("source") or "catalog",
            "lat": c[0], "lon": c[1], "coordSrc": st.get("coordSrc") or "",
        })
        if i:
            a, b = raw[i - 1].get("km"), s.get("km")
            seg.append(round(float(b) - float(a), 1) if a is not None and b is not None else None)
    kms = [x["km"] for x in stations if x["km"] is not None]
    total = got.get("totalKm") or (max(kms) if kms else None)
    # 线名用资料库那条的规范名：搜「侯西铁路」（资料库的 title）拼出来的线不能
    # 自己也叫「侯西铁路」，否则同一条线在库里长出第二个名字，保存站序时还会
    # 报「线路库里没有这条线」。原始输入挂在 aliasOf 上，前端会说明是同一回事。
    canon = name
    if got.get("source") == "catalog" and got.get("sourceName"):
        canon = got["sourceName"]
    return {
        "name": canon, "n": len(stations), "totalKm": total,
        "aliasOf": name if canon != name else None,
        "kmComplete": len(kms) == len(stations), "kmReliable": len(kms) == len(stations),
        "custom": False, "stopsManual": False,
        "source": got.get("source") or "catalog",
        "stopsFrom": got.get("source"), "stopsSource": got.get("sourceName"),
        "stopsParts": got.get("parts") or [],
        "coordCoverage": round(sum(1 for x in stations if x["lat"] is not None)
                               / max(1, len(stations)) * 100, 1),
        "stations": stations, "segKm": seg, "override": None,
        # 这份站序被人为倒排过（来源级修正），前端据此把按钮显示成「恢复站序」
        "reversed": bool(got.get("reversed")), "srcFix": got.get("srcFix") or [],
        "lineName": got.get("sourceName") or ",".join(got.get("parts") or []),
    }


# ---------------------------------------------------------------------------
# 站序倒排：**按来源**倒过来，起点/终点对调，里程跟着重算
# ---------------------------------------------------------------------------

def reverse_line(name: str, source: str = "", mode: str = "") -> dict:
    """把「某一份来源的站序」倒过来

    同一条线名有好几份站序（线路库 / 资料库 / china-emu / OSM / 分段拼），
    方向不一定一致 —— OSM 是按 way 的绘制方向拼的，可能正好从上海往北京，
    而资料库那份是对的。所以这里只动**指定的那一份**：

      · 资料库 / china-emu / OSM / 分段拼：记一条来源级修正（`line_src_fix`），
        读的时候当场翻转，原始数据不被改写；再点一次是取消，原样回去。
      · 线路库 / 自建：这份就是线路自己的站序，只能按整条线改（人工站序），
        走 `save_line_stations`，仍可用「恢复档案站序」退回。

    `mode`：""/"toggle" 切换；"on" 强制倒排；"off" 强制恢复。
    """
    name = re.sub(r"\s+", "", str(name or ""))
    if not name:
        raise ValueError("要指定是哪个线名")
    # 倒排的是「他现在看着的那份」：指定了来源就用那份；没指定先看线路库自己
    # 的站序（那是详情页默认显示的那份），库内没有再按系统规则找
    got = stations_for(name, source) if source else None
    if not got:
        got = _lib_stops(name) or stations_for(name)
    if not got or len(got.get("stops") or []) < 2:
        raise ValueError("这条线没有可倒排的站序")
    src = got.get("source") or ""

    # ---- 库内线路：这份站序就是线路本身，按整条线改 ----
    if src in ("lib", "custom"):
        stops = [s for s in got["stops"] if (s.get("name") or "").strip()]
        if len(stops) < 2:
            raise ValueError("这条线没有可倒排的站序")
        rev = _reverse_stops(stops)
        miss = sum(1 for s in rev if s.get("km") is None)
        detail = railcore.save_line_stations(
            name, [{"name": s["name"], "km": s["km"]} for s in rev])
        return {"ok": True, "name": detail.get("name"), "asked": name,
                "n": len(rev), "from": src, "inPlace": False,
                "missing": miss, "totalKm": detail.get("totalKm"),
                "reversed": False, "srcFix": [], "line": detail}

    # ---- 外部来源：只挂修正标记，别的一概不动 ----
    if src not in ("catalog", "chinaemu", "osm", "parts"):
        raise ValueError("不认识的来源：" + src)
    cur = "reverse" in railcore.src_fix(name, src)
    want = (mode == "on") if mode in ("on", "off") else (not cur)
    if want != cur:
        railcore.set_src_fix(name, src, "reverse", want)

    after = stations_for(name, src) or {}
    st = after.get("stops") or []
    return {"ok": True, "name": name, "asked": name, "n": len(st),
            "from": src, "inPlace": True, "reversed": bool(want),
            "srcFix": ["reverse"] if want else [],
            "first": st[0].get("name") if st else "",
            "last": st[-1].get("name") if st else "",
            "missing": sum(1 for s in st if s.get("km") is None),
            "totalKm": after.get("totalKm"), "line": None}


# ---------------------------------------------------------------------------
# 盘点
# ---------------------------------------------------------------------------

# 来源在界面上的叫法
_SRC_LABEL = {"lib": "线路库", "custom": "自建", "catalog": "线路资料库",
              "chinaemu": "china-emu", "osm": "OSM", "parts": "分段拼接"}


def line_sources(name: str) -> List[dict]:
    """这条线名在各来源里各是什么样（站数 / 里程 / 首末站）

    界面拿它做「同名多来源」的选项条：搜「京广线」时**不替用户挑** ——
    线路库 89 站、china-emu 154 站、OSM 220 站全摆出来，点哪个用哪个。
    站序口径不同不是错误：线路库是人工整理的客运站顺序，china-emu 带线路所和
    计价里程，OSM 是几何上插值出来的（会带并线枢纽站，但站点最全）。
    """
    name = re.sub(r"\s+", "", str(name or ""))
    out: List[dict] = []
    if not name:
        return out

    ln = cnrail.line(name)
    if ln and len(ln.get("stations") or []) >= 2:
        st = ln["stations"]
        out.append({"source": "custom" if ln.get("custom") else "lib",
                    "label": _SRC_LABEL["custom" if ln.get("custom") else "lib"],
                    "sourceName": ln.get("name"), "n": len(st),
                    "km": ln.get("totalKm"),
                    "first": st[0].get("name"), "last": st[-1].get("name"),
                    "reversed": False,
                    "kmDone": sum(1 for x in st if x.get("km") is not None)})

    for src, fn in (("catalog", _catalog_stops), ("chinaemu", _ce_stops),
                    ("osm", _osm_stops)):
        try:
            got = fn(name)
        except Exception:                    # noqa: BLE001
            got = None
        got = _apply_src_fix(name, got)      # 倒排过的那份，来源条上也要反过来
        if not got or len(got.get("stops") or []) < 2:
            continue
        st = got["stops"]
        out.append({"source": src, "label": _SRC_LABEL[src],
                    "sourceName": got.get("sourceName"), "n": len(st),
                    "km": got.get("totalKm"),
                    "first": st[0].get("name"), "last": st[-1].get("name"),
                    "reversed": bool(got.get("reversed")),
                    "kmDone": sum(1 for x in st if x.get("km") is not None)})
    return out


def survey(names: Optional[Sequence[str]] = None) -> List[dict]:
    """盘点线名两侧的缺口（给界面上的「补全」清单用）

    names 为空就扫全部 OSM 线名。
    """
    out: List[dict] = []
    pool = list(names or [])
    if not pool:
        pool = [k for k, v in (railgeom._geoms() or {}).items()]
    for nm in pool:
        g = railgeom.meta(nm)
        if not g:
            continue
        sp = split(nm)
        s = stations_for(nm)
        out.append({"name": nm, "km": g.get("km"), "hasOsm": True,
                    "hasStops": bool(s and s.get("stops")),
                    "stopsFrom": (s or {}).get("source"),
                    "stopsN": len((s or {}).get("stops") or []),
                    "borrowFrom": sp[0] if sp and railgeom.meta(sp[0]) else None})
    return out
