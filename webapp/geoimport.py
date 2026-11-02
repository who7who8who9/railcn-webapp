# -*- coding: utf-8 -*-
"""
geoimport.py —— 手工导入 GeoJSON 线路

认的文件形态（本项目导出的那种，也是 GeoJSON 里最自然的写法）：

    {"type": "FeatureCollection",
     "properties": {"线路名称": "京沪线", "编号": "0002", "起点": "北京", "终点": "上海"},
     "features": [
        {"type": "Feature", "geometry": {"type": "MultiLineString", "coordinates": [...]}},
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [116.24, 39.83]},
         "properties": {"kind": "station", "站名": "丰台西", "类型": "站"}}
     ]}

做的事：

  1. 走向（LineString / MultiLineString，多层嵌套也认）用 `railgeom.chain_parts`
     串成一条连续折线 —— OSM 那种正线被站内无名股道切断的碎段，端点 3 km 内会接上，
     上下行复线（接上去只是原路折回）丢掉。
  2. 车站点投影到折线上，按**沿线距离**排序（不看文件里点的先后，也不看站名），
     每站里程 = 沿线距离 − 首站沿线距离，所以首站恒为 0。
  3. 落成「自建线路」（line_custom），站序编辑 / 倒排 / 改里程 / 删除全都能用。
  4. 走向另存 `line_geom`（压缩后的原始折线），地图画的就是这份；`/api/line/osm`
     在 OSM 几何库里找不到这条线时会回退到这里。
  5. 文件里带的站坐标补进 `station_custom`（只补档案里没有坐标的站，
     档案坐标是校对过的，不覆盖）。

不做的事：不顶档案线路（同名档案线直接报错），不碰线路资料库。
"""
from __future__ import annotations

import gzip
import json
import math
import os
import re
import sqlite3
import time
from typing import Dict, List, Optional, Sequence, Tuple

import cnrail
import railcore
import railgeom

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "rail.db")

# 站名 / 线路名可能出现的属性键（中文优先，兼容 OSM 风格的 name）
_NAME_KEYS = ("线路名称", "线名", "name", "NAME", "Name", "title")
_STA_KEYS = ("站名", "name", "车站名", "NAME", "Name")
_TYPE_KEYS = ("类型", "type", "类别")
# 车站要素里自带的里程（有就用文件的，没有才按走向量）
_KM_KEYS = ("里程", "km", "KM", "Km", "公里", "累计里程", "里数", "mileage")
# 站点离走向超过这个距离，仍然按投影入序列，但会在结果里报出来（多半是坐标错了）
OFF_GEOM_M = 3000.0
_CELL = 0.02          # 节点网格边长（度 ≈ 2 km）：建图补边和车站吸附共用


def _ensure(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS line_geom (
               name       TEXT PRIMARY KEY,   -- 线名（与 line_custom 同名）
               file       TEXT,               -- 导入时的文件名
               total_km   REAL,               -- 串成单线后的沿线里程
               n_part     INTEGER,            -- 原始折线段数
               n_pt       INTEGER,            -- 原始点数
               props      TEXT,               -- 文件顶层 properties（编号 / 起讫 / 车站数…）
               parts_gz   BLOB,               -- gzip(JSON)：[[ [lon,lat], … ], …] 原始碎段
               spine_gz   BLOB,               -- gzip(JSON)：[[lon,lat], …] 串好的连续走向
               created_at REAL
           )""")
    # 老库补一列（第一版没有 spine_gz）
    cols = {r[1] for r in conn.execute("PRAGMA table_info(line_geom)")}
    if "spine_gz" not in cols:
        conn.execute("ALTER TABLE line_geom ADD COLUMN spine_gz BLOB")


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    _ensure(conn)
    return conn


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

def _props_name(props: dict) -> str:
    for k in _NAME_KEYS:
        v = (props or {}).get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _line_parts(geom: dict, out: List[list]) -> None:
    """把任意层级的线状几何收进 out（Polygon 的外环当走向，环内洞忽略）"""
    t = geom.get("type")
    c = geom.get("coordinates")
    if t == "LineString" and isinstance(c, list):
        out.append([[float(p[0]), float(p[1])] for p in c if len(p) >= 2])
    elif t == "MultiLineString" and isinstance(c, list):
        for seg in c:
            out.append([[float(p[0]), float(p[1])] for p in seg if len(p) >= 2])
    elif t == "Polygon" and isinstance(c, list) and c:
        out.append([[float(p[0]), float(p[1])] for p in c[0] if len(p) >= 2])
    elif t == "GeometryCollection":
        for g in geom.get("geometries") or []:
            _line_parts(g, out)


def parse(obj, filename: str = "") -> dict:
    """GeoJSON → {name, props, parts, stations}；坏文件抛 ValueError（人话）"""
    if isinstance(obj, (bytes, bytearray)):
        obj = obj.decode("utf-8-sig")
    if isinstance(obj, str):
        try:
            obj = json.loads(obj)
        except ValueError as e:
            raise ValueError("不是合法 JSON：%s" % e) from e
    if not isinstance(obj, dict):
        raise ValueError("GeoJSON 根节点应该是对象")

    t = obj.get("type")
    if t == "FeatureCollection":
        feats = obj.get("features") or []
        props = obj.get("properties") or {}
    elif t == "Feature":
        feats = [obj]
        props = obj.get("properties") or {}
    elif t in ("MultiLineString", "LineString", "GeometryCollection"):
        feats = [{"type": "Feature", "geometry": obj, "properties": {}}]
        props = {}
    else:
        raise ValueError("不认识的 GeoJSON 类型：%s" % t)

    parts: List[list] = []
    stations: List[dict] = []
    for f in feats:
        if not isinstance(f, dict):
            continue
        g = f.get("geometry") or {}
        pr = f.get("properties") or {}
        if g.get("type") == "Point":
            c = g.get("coordinates") or []
            if len(c) < 2:
                continue
            nm = ""
            for k in _STA_KEYS:
                v = pr.get(k)
                if isinstance(v, str) and v.strip():
                    nm = v.strip()
                    break
            if not nm:
                continue                       # 没名字的点没法当站
            kind = ""
            for k in _TYPE_KEYS:
                v = pr.get(k)
                if isinstance(v, str):
                    kind = v.strip()
                    break
            km = None
            for k in _KM_KEYS:
                v = pr.get(k)
                if isinstance(v, bool) or v in (None, ""):
                    continue
                try:
                    km = float(str(v).replace("km", "").replace("公里", "").strip())
                except (TypeError, ValueError):
                    km = None
                if km is not None:
                    break
            stations.append({"name": nm, "lon": float(c[0]), "lat": float(c[1]),
                             "kind": kind, "km": km,
                             "osmId": str(pr.get("OSM_ID") or pr.get("osm_id") or ""),
                             "from": "file"})
        else:
            _line_parts(g, parts)

    parts = [p for p in parts if len(p) >= 2]
    name = _props_name(props)
    if not name:
        # 顶层没写线名：看那条线状要素有没有名字，再退回文件名
        for f in feats:
            if (f.get("geometry") or {}).get("type") != "Point":
                name = _props_name(f.get("properties") or {})
                if name:
                    break
    if not name and filename:
        name = re.sub(r"\.(geo)?json$", "", os.path.basename(filename), flags=re.I)
    return {"name": re.sub(r"\s+", "", name or ""), "props": props,
            "parts": parts, "stations": stations, "file": os.path.basename(filename or "")}


# ---------------------------------------------------------------------------
# 沿线排序与里程
# ---------------------------------------------------------------------------

def _dist_m(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    """两点平面距离（米）。lat/lon 顺序，和 railgeom 一致"""
    dy = (b[0] - a[0]) * railgeom._M_LAT
    dx = (b[1] - a[1]) * railgeom._M_LON * math.cos(math.radians(a[0]))
    return math.hypot(dx, dy)


def _proj(a: Tuple[float, float], b: Tuple[float, float],
          p: Tuple[float, float]) -> Tuple[float, float]:
    """点到线段的参数 t（0~1）与垂距（米）"""
    lat0 = a[0]
    sx = railgeom._M_LON * math.cos(math.radians(lat0))
    sy = railgeom._M_LAT
    bx, by = (b[1] - a[1]) * sx, (b[0] - a[0]) * sy
    px, py = (p[1] - a[1]) * sx, (p[0] - a[0]) * sy
    l2 = bx * bx + by * by
    if l2 <= 0:
        return 0.0, math.hypot(px, py)
    t = (px * bx + py * by) / l2
    t = 0.0 if t < 0 else (1.0 if t > 1 else t)
    return t, math.hypot(px - t * bx, py - t * by)



class Track:
    """把一条线的 way 段建成图，并把车站的投影点切进所在段里

    为什么要建图：OSM 里一条线是几千段（过一座桥、进一次站就被切断），按端点
    一段段往上接（`railgeom.chain_parts`）碰到一个缺口就停 —— 京沪线 2755 段
    只串出 967 km。这里改成：

      · 段端点作节点（坐标相同的自动合并），段作边，权重是沿线米数；
      · 段与段之间 2 km 内的断口补一条「直线边」，当真实轨道计入里程；
      · 车站投影到最近的一段上，在投影位置把段切开，站点成为正式节点。

    于是两站之间的最短路就是实打实的沿线距离；站落在长段中间（夹沟、西寺坡
    这种两站相距二十公里、中间没有 way 断点）也不会被拉到几公里外的端点上去。
    """

    def __init__(self, parts: Sequence[Sequence[Sequence[float]]],
                 gap_m: float = 2000.0):
        self.gap_m = gap_m
        self.parts: List[List[Tuple[float, float]]] = []      # 每段 (lat, lon)
        self.cum: List[List[float]] = []                     # 每段逐点累计米数
        self.nodes: List[Tuple[float, float]] = []           # (lat, lon)
        self.edges: List[Tuple[int, int, float, int]] = []   # (u, v, 米, 段号|-1)
        self.adj: List[List[Tuple[int, float, int]]] = []
        self.start: List[int] = []
        self.end: List[int] = []
        self.cuts: Dict[int, List[Tuple[float, int]]] = {}   # 段号 → [(段内米数, 节点)]
        self._shared: Dict[Tuple[float, float], int] = {}
        self._seg: Dict[Tuple[int, int], List[Tuple[int, int]]] = {}
        self._load(parts)

    # -- 段、端点、段索引 --------------------------------------------------
    def _node(self, lat: float, lon: float, share: bool = False) -> int:
        if share:
            key = (round(lon, 6), round(lat, 6))
            i = self._shared.get(key)
            if i is not None:
                return i
        i = len(self.nodes)
        self.nodes.append((lat, lon))
        if share:
            self._shared[key] = i
        return i

    def _load(self, parts: Sequence[Sequence[Sequence[float]]]) -> None:
        for raw in parts:
            pts = [(float(q[1]), float(q[0])) for q in raw if len(q) >= 2]
            if len(pts) < 2:
                continue
            if _dist_m(pts[0], pts[-1]) < 1.0:
                continue                                     # 闭合小环，没走向
            cum = [0.0]
            for i in range(1, len(pts)):
                cum.append(cum[-1] + _dist_m(pts[i - 1], pts[i]))
            if cum[-1] < 50.0:
                continue                                     # 太短的碎屑
            pi = len(self.parts)
            self.parts.append(pts)
            self.cum.append(cum)
            self.start.append(self._node(*pts[0], share=True))
            self.end.append(self._node(*pts[-1], share=True))
            for si in range(len(pts) - 1):
                mid = ((pts[si][0] + pts[si + 1][0]) / 2, (pts[si][1] + pts[si + 1][1]) / 2)
                self._seg.setdefault((int(mid[0] / _CELL), int(mid[1] / _CELL)),
                                     []).append((pi, si))

    # -- 车站落到哪一段 ----------------------------------------------------
    def _seg_proj(self, pi: int, si: int, p: Tuple[float, float]) -> Tuple[float, float]:
        a, b = self.parts[pi][si], self.parts[pi][si + 1]
        t, d = _proj(a, b, p)
        return d, self.cum[pi][si] + t * _dist_m(a, b)

    def locate(self, lat: float, lon: float) -> Tuple[int, float, float]:
        """最近的段：返回 (段号, 段内沿线米数, 垂距米)"""
        best: Tuple[float, int, float] = (float("inf"), -1, 0.0)
        ci, cj = int(lat / _CELL), int(lon / _CELL)
        for rad in (0, 1, 2, 4):
            for di in range(-rad, rad + 1):
                for dj in range(-rad, rad + 1):
                    for pi, si in self._seg.get((ci + di, cj + dj), ()):
                        d, s = self._seg_proj(pi, si, (lat, lon))
                        if d < best[0]:
                            best = (d, pi, s)
            if best[0] <= 1500:
                break
        if best[1] < 0:                     # 附近一格都没有：全量找（站坐标偏得离谱时）
            for pi in range(len(self.parts)):
                for si in range(len(self.parts[pi]) - 1):
                    d, s = self._seg_proj(pi, si, (lat, lon))
                    if d < best[0]:
                        best = (d, pi, s)
        return best[1], best[2], best[0]

    def add_station(self, lat: float, lon: float) -> Tuple[int, float]:
        """把车站切进图里，返回 (节点号, 垂距米)"""
        pi, s, d = self.locate(lat, lon)
        if pi < 0:
            return -1, d
        total = self.cum[pi][-1]
        if s <= 0.5:
            return self.start[pi], d
        if total - s <= 0.5:
            return self.end[pi], d
        node = self._node(lat, lon)
        self.cuts.setdefault(pi, []).append((s, node))
        return node, d

    # -- 建邻接表 ----------------------------------------------------------
    def finalize(self) -> None:
        self.edges = []
        adj: List[List[Tuple[int, float, int]]] = [[] for _ in self.nodes]

        def add(u: int, v: int, w: float, kind: int) -> None:
            if u == v or u < 0 or v < 0 or w <= 0.05:
                return
            k = len(self.edges)
            self.edges.append((u, v, w, kind))
            adj[u].append((v, w, k))
            adj[v].append((u, w, k))

        # 每段按切点顺序拆成若干小边
        for pi in range(len(self.parts)):
            total = self.cum[pi][-1]
            prev, prev_s = self.start[pi], 0.0
            for s, node in sorted(self.cuts.get(pi, [])):
                add(prev, node, s - prev_s, pi)
                prev, prev_s = node, s
            add(prev, self.end[pi], total - prev_s, pi)

        # 断口补边：只连「段端点」，每个最多连 3 条（够接上被股道切开的正线）
        ends = sorted(set(self.start) | set(self.end))
        grid: Dict[Tuple[int, int], List[int]] = {}
        for i in ends:
            lat, lon = self.nodes[i]
            grid.setdefault((int(lat / _CELL), int(lon / _CELL)), []).append(i)
        for i in ends:
            lat, lon = self.nodes[i]
            ci, cj = int(lat / _CELL), int(lon / _CELL)
            best = []
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    for j in grid.get((ci + di, cj + dj), ()):
                        if j <= i:
                            continue
                        dd = _dist_m((lat, lon), self.nodes[j])
                        if dd <= self.gap_m:
                            best.append((dd, j))
            best.sort()
            for dd, j in best[:3]:
                add(i, j, dd, -1)
        self.adj = adj

    # -- 查询 --------------------------------------------------------------
    def dijkstra(self, src: int):
        import heapq
        n = len(self.nodes)
        dist = [float("inf")] * n
        prev = [-1] * n
        if src < 0:
            return dist, prev
        dist[src] = 0.0
        pq = [(0.0, src)]
        while pq:
            dd, u = heapq.heappop(pq)
            if dd > dist[u] + 1e-9:
                continue
            for v, w, k in self.adj[u]:
                nd = dd + w
                if nd < dist[v] - 1e-9:
                    dist[v] = nd
                    prev[v] = k
                    heapq.heappush(pq, (nd, v))
        return dist, prev

    def path_nodes(self, b: int, prev: Sequence[int]) -> List[Tuple[float, float]]:
        """回溯到 b 的节点序列（起点由 prev 链决定），用于拼「干线」折线"""
        out: List[Tuple[float, float]] = []
        cur = b
        guard = 0
        while cur >= 0 and guard < 100000:
            out.append(self.nodes[cur])
            k = prev[cur]
            if k < 0:
                break
            u, v, _w, _i = self.edges[k]
            cur = v if cur == u else u
            guard += 1
        out.reverse()
        return out


def _pca_order(pts: Sequence[dict]) -> List[int]:
    """按「线路主方向」排序：坐标做主成分分析，投影到第一主轴上从小到大

    为什么不用文件里的点序：导出顺序可能有局部错位（京沪线把白家店排到了南驿
    前面、天津枢纽几个站前后颠倒），照文件顺序连会来回折返、里程虚高 90 km。
    也不用「最近邻逐个走」：图上有并行轨道和断口补边，贪心会顺着邻线跑偏。
    主轴投影对一条线状的站群足够稳，剩下的错位交给下面的局部优化。
    """
    if len(pts) < 3:
        return list(range(len(pts)))
    lat0 = sum(p["lat"] for p in pts) / len(pts)
    kx = math.cos(math.radians(lat0))
    xy = [(p["lon"] * kx, p["lat"]) for p in pts]
    mx = sum(x for x, _ in xy) / len(xy)
    my = sum(y for _, y in xy) / len(xy)
    sxx = sum((x - mx) ** 2 for x, _ in xy) / len(xy)
    syy = sum((y - my) ** 2 for _, y in xy) / len(xy)
    sxy = sum((x - mx) * (y - my) for x, y in xy) / len(xy)
    ang = 0.5 * math.atan2(2 * sxy, sxx - syy)          # 第一主轴方向
    vx, vy = math.cos(ang), math.sin(ang)
    return sorted(range(len(pts)),
                  key=lambda i: (xy[i][0] - mx) * vx + (xy[i][1] - my) * vy)


def _path_cost(order: Sequence[int], G: Sequence[Sequence[float]]) -> float:
    return sum(G[order[i]][order[i + 1]] for i in range(len(order) - 1))


def _local_search(order: List[int], G: Sequence[Sequence[float]],
                  window: int = 6, rounds: int = 12) -> List[int]:
    """局部优化：把某站插到前后 window 名之内，能缩短总里程就换

    主轴投影只定大方向，站场附近的几个站（货场、线路所）经常前后差一位，
    这种错位会让里程多算十几公里，插一位就回来了。
    """
    n = len(order)
    if n < 4:
        return order
    best = _path_cost(order, G)
    for _ in range(rounds):
        moved = False
        for i in range(n):
            for j in range(max(0, i - window), min(n, i + window + 1)):
                if j == i:
                    continue
                cand = list(order)
                v = cand.pop(i)
                cand.insert(j, v)
                c = _path_cost(cand, G)
                if c < best - 1.0:                  # 至少省 1 米才认
                    order, best, moved = cand, c, True
                    break
            if moved:
                break
        if not moved:
            break
    return order


def assemble(parts: Sequence[Sequence[Sequence[float]]],
             stations: Sequence[dict],
             start_hint: str = "", end_hint: str = ""):
    """串走向 + 排站序 + 算里程。返回 (seq, chain, stat)

    seq    [{name, lat, lon, kind, km, snap}]，首站 0，里程是沿线累计
    chain  节点序列（lat, lon），干线预览用，不落库
    stat   {nodes, edges, broken, totalKm}
    """
    track = Track(parts)

    # 站名原样照搬（不按 12306 口径去「站」后缀），同名站也照原样留着 ——
    # 导入以文件为准，系统不替用户改名、不替他合并
    pts: List[dict] = []
    for s in stations:
        nm = str(s["name"] or "").strip()
        if not nm:
            continue
        lat, lon = float(s["lat"]), float(s["lon"])
        node, snap = track.add_station(lat, lon)
        pts.append({"name": nm, "lat": lat, "lon": lon, "kind": s.get("kind") or "",
                    "node": node, "snap": snap, "raw": s["name"],
                    "fileKm": s.get("km") if isinstance(s.get("km"), (int, float)) else None})
    if not pts:
        return [], [], {"nodes": 0, "edges": 0, "broken": 0, "totalKm": 0.0, "kmFrom": "geom"}
    track.finalize()

    # 两两图上距离（不连通的退化成直线 ×1.2，免得整表失效）
    n = len(pts)
    cache: Dict[int, Sequence[float]] = {}
    G = [[0.0] * n for _ in range(n)]
    for i, p in enumerate(pts):
        node = p["node"]
        if node not in cache:
            cache[node] = track.dijkstra(node)[0]
        dist = cache[node]
        for j, q in enumerate(pts):
            if i == j:
                continue
            v = dist[q["node"]] if (node >= 0 and q["node"] >= 0) else float("inf")
            if v == float("inf"):
                v = _dist_m((p["lat"], p["lon"]), (q["lat"], q["lon"])) * 1.2
            G[i][j] = v

    order = _local_search(_pca_order(pts), G)

    # 方向：props 的起点/终点能对上站名就用它，否则跟文件里第一个点走
    idx_of: Dict[str, int] = {}
    for i, p in enumerate(pts):
        idx_of.setdefault(p["name"], i)       # 同名站取第一个
    rev = False
    hi, ti = idx_of.get(start_hint or ""), idx_of.get(end_hint or "")
    if hi is not None and ti is not None and hi != ti:
        rev = order.index(hi) > order.index(ti)
    elif stations:
        head = str(stations[0]["name"] or "").strip()
        if head in idx_of:
            rev = order.index(idx_of[head]) > len(order) / 2
    if rev:
        order.reverse()

    # 文件里自带里程（每站都有、且按排好的站序不递减）就按文件来 —— 导入以文件为准；
    # 否则退回按走向量出来的里程
    kms = [pts[i].get("fileKm") for i in order]
    use_file_km = (bool(kms) and all(k is not None for k in kms)
                   and all(kms[i] <= kms[i + 1] for i in range(len(kms) - 1)))

    seq: List[dict] = []
    chain: List[Tuple[float, float]] = []
    total = 0.0
    broken = 0
    prev = None
    for pos in order:
        p = pts[pos]
        if prev is None:
            seq.append({**p, "km": 0.0})
            prev = p
            continue
        if prev["node"] < 0 or p["node"] < 0:
            total += _dist_m((prev["lat"], prev["lon"]), (p["lat"], p["lon"]))
            broken += 1
        else:
            dist, pv = track.dijkstra(prev["node"])
            if dist[p["node"]] == float("inf"):
                total += _dist_m((prev["lat"], prev["lon"]), (p["lat"], p["lon"]))
                broken += 1
            else:
                total += dist[p["node"]]
                for q in track.path_nodes(p["node"], pv):
                    if not chain or _dist_m(chain[-1], q) > 0.5:
                        chain.append(q)
        seq.append({**p, "km": round(total / 1000.0, 1)})
        prev = p

    geom_km = round(total / 1000.0, 1)
    if use_file_km:
        for i, rec in enumerate(seq):
            rec["km"] = round(float(pts[order[i]]["fileKm"]), 1)
        seq[0]["km"] = seq[0]["km"] or 0.0
    out_km = seq[-1]["km"] if seq else 0.0
    return seq, chain, {"nodes": len(track.nodes), "edges": len(track.edges),
                        "broken": broken, "totalKm": out_km, "geomKm": geom_km,
                        "kmFrom": "file" if use_file_km else "geom"}



# ---------------------------------------------------------------------------
# 落库
# ---------------------------------------------------------------------------

def _put_coords(seq: Sequence[dict], exact: bool = False) -> List[str]:
    """把文件里的站坐标补进车站库（档案里查不到坐标的站才写），返回补了哪些

    站名按文件里怎么写就怎么存（exact 只是不再二次去重，保留参数给调用方语义）。
    档案已有的坐标一律不动 —— 车站坐标以档案 / 人工为准，导入只补空缺。
    """
    rows = []
    now = time.time()
    seen = set()
    for s in seq:
        # 档案里已经有这个站（按 12306 口径能查到）且有坐标：以档案为准，不再写
        st = cnrail.station(s["name"]) or {}
        if st.get("lat") is not None and st.get("lon") is not None:
            continue
        if s["name"] in seen:
            continue
        seen.add(s["name"])
        typ = s.get("kind") or ("线路所" if s["name"].endswith("所") else "车站")
        rows.append((s["name"], s["lat"], s["lon"],
                     json.dumps([], ensure_ascii=False), typ, now))
    if not rows:
        return []
    with railcore._db_lock, railcore.db() as conn:
        for nm, lat, lon, lines_json, typ, ts in rows:
            conn.execute(
                """INSERT INTO station_custom(name, lat, lon, lines_json, tele, bureau, note,
                                              updated_at, type, source, alias, coord_src)
                   VALUES(?,?,?,?,?,'','',?,?,'geojson','','geojson')
                   ON CONFLICT(name) DO UPDATE SET
                     lat=COALESCE(station_custom.lat, excluded.lat),
                     lon=COALESCE(station_custom.lon, excluded.lon),
                     type=COALESCE(station_custom.type, excluded.type),
                     coord_src=COALESCE(NULLIF(station_custom.coord_src,''), 'geojson'),
                     updated_at=excluded.updated_at""",
                (nm, lat, lon, lines_json, "", ts, typ),
            )
    return [r[0] for r in rows]


def _save_geom(name: str, filename: str, parts: List[list], km: float,
               props: dict, spine: Sequence[Tuple[float, float]] = ()) -> None:
    """存原始碎段 + 串好的连续走向（地图画后者，一条线画到底）"""
    blob = gzip.compress(json.dumps(parts, separators=(",", ":")).encode("utf-8"), 6)
    n_pt = sum(len(p) for p in parts)
    line = [[round(lon, 6), round(lat, 6)] for lat, lon in spine]
    sblob = gzip.compress(json.dumps(line, separators=(",", ":")).encode("utf-8"), 6) if line else None
    with railcore._db_lock, railcore.db() as conn:
        _ensure(conn)
        conn.execute(
            """INSERT INTO line_geom(name, file, total_km, n_part, n_pt, props, parts_gz,
                                     spine_gz, created_at)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET
                 file=excluded.file, total_km=excluded.total_km, n_part=excluded.n_part,
                 n_pt=excluded.n_pt, props=excluded.props, parts_gz=excluded.parts_gz,
                 spine_gz=excluded.spine_gz, created_at=excluded.created_at""",
            (name, filename, km, len(parts), n_pt,
             json.dumps(props or {}, ensure_ascii=False), blob, sblob, time.time()),
        )


def _geom_row(name: str) -> Optional[dict]:
    nm = cnrail._norm(name)
    try:
        with _db() as conn:
            r = conn.execute("SELECT * FROM line_geom WHERE name=?", (nm,)).fetchone()
            if r:
                return dict(r)
    except sqlite3.DatabaseError:
        return None
    return None


def has_geom(name: str) -> bool:
    return _geom_row(name) is not None


def geom(name: str, max_parts: int = 400) -> Optional[dict]:
    """导入的走向，形状跟 railgeom.geom() 对齐，前端不用改"""
    row = _geom_row(name)
    if not row:
        return None
    parts = None
    # 有拼好的连续走向就用它（一条线一笔画完）；没有就退回原始碎段
    spine = row.get("spine_gz") if isinstance(row, dict) else None
    for blob, wrap in ((spine, True), (row.get("parts_gz"), False)):
        if not blob:
            continue
        try:
            got = json.loads(gzip.decompress(blob).decode("utf-8"))
        except (OSError, ValueError):
            continue
        if got:
            parts = [got] if wrap else got
            break
    if not parts:
        return None
    if max_parts and len(parts) > max_parts:
        parts = sorted(parts, key=len, reverse=True)[:max_parts]
    rec = meta(name) or {}
    rec["parts"] = parts
    rec["crs"] = "WGS84"
    return rec


def meta(name: str) -> Optional[dict]:
    row = _geom_row(name)
    if not row:
        return None
    km = row.get("total_km")
    return {"name": row["name"], "km": km, "kmMain": km,
            "nPart": row.get("n_part"), "nFeat": row.get("n_part"),
            "nPt": row.get("n_pt"),
            "match": "geojson", "source": "geojson", "file": row.get("file") or ""}


def list_imported() -> List[dict]:
    """导入过的线路一览（给接口和「这份走向从哪来」用）"""
    try:
        with _db() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT name, file, total_km, n_part, n_pt, props, created_at"
                " FROM line_geom ORDER BY created_at DESC")]
    except sqlite3.DatabaseError:
        return []
    out = []
    for r in rows:
        try:
            r["props"] = json.loads(r.get("props") or "{}")
        except ValueError:
            r["props"] = {}
        out.append(r)
    return out


def drop_geom(name: str) -> bool:
    nm = cnrail._norm(name)
    try:
        with railcore._db_lock, railcore.db() as conn:
            _ensure(conn)
            return conn.execute("DELETE FROM line_geom WHERE name=?", (nm,)).rowcount > 0
    except sqlite3.DatabaseError:
        return False


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def import_line(obj, filename: str = "", replace: bool = False,
                note: str = "") -> dict:
    """导一条线进来，返回一份人话摘要

    replace=True 时允许覆盖**自己建的**同名线路（重复导入同一份文件就走这个）；
    档案线路 / 资料库挂靠的线路一律不动，报错让人自己决定。
    """
    d = parse(obj, filename)
    name = d["name"]
    if not name:
        raise ValueError("文件里没写线路名称（properties.线路名称），文件名也推不出来")
    if not d["parts"]:
        raise ValueError("没有线状几何（LineString / MultiLineString），画不出走向")
    if len(d["stations"]) < 2:
        raise ValueError("车站点少于 2 个（properties.kind=station 的 Point），成不了一条线")

    cur = cnrail.line(name)
    if cur and not (replace and cur.get("custom")):
        if cur.get("custom"):
            raise ValueError("线路库里已经有「%s」（%s 站，自建），可以覆盖"
                             % (name, cur.get("n")))
        raise ValueError("线路库里已经有「%s」（%s 站，%s），不能覆盖 —— "
                         "要按这份 GeoJSON 重来，先在线路页把这条线删掉，或者换个线名"
                         % (name, cur.get("n"),
                            "资料库带进来的" if cur.get("stopsFrom") == "catalog" else "底表线路"))

    props = d["props"] or {}
    seq, chain, stat = assemble(d["parts"], d["stations"],
                                str(props.get("起点") or ""), str(props.get("终点") or ""))
    if len(seq) < 2:
        raise ValueError("能定位到走向上的车站少于 2 个")
    if not chain:
        raise ValueError("走向串不成一条折线（碎段之间离得太远）")

    stops = [{"name": s["name"], "km": s["km"]} for s in seq]
    names = [s["name"] for s in stops]
    dup = list(dict.fromkeys(n for i, n in enumerate(names) if n in names[:i]))
    bits = ["GeoJSON 导入 · 按文件原样"]
    if props.get("编号"):
        bits.append("编号 %s" % props["编号"])
    if props.get("起点") or props.get("终点"):
        bits.append("%s→%s" % (props.get("起点") or "?", props.get("终点") or "?"))
    bits.append("%d 站 / %.1f km" % (len(stops), stops[-1]["km"]))
    bits.append("里程按文件" if stat.get("kmFrom") == "file" else "里程按走向量")
    if d["file"]:
        bits.insert(1, d["file"])
    auto_note = " · ".join(bits)

    filled = _put_coords(seq, exact=True)
    if filled:
        railcore._apply_custom()             # 新补的坐标要并进 cnrail，详情页才有坐标
        railcore._reload_coord_cache()
    detail = railcore.save_custom_line(name, stops, note=note or auto_note,
                                       replace=replace, raw=True)
    _save_geom(name, d["file"], d["parts"], stat.get("geomKm") or stat["totalKm"],
               props, chain)

    off = [s for s in seq if s["snap"] > OFF_GEOM_M]
    cat = cnrail.catalog_get(name) or {}
    return {
        "ok": True, "name": detail.get("name") or name,
        "n": len(stops), "totalKm": stops[-1]["km"],
        "geomKm": stat.get("geomKm") or stat["totalKm"],
        "kmFrom": stat.get("kmFrom") or "geom",
        "first": stops[0]["name"], "last": stops[-1]["name"],
        "points": sum(len(p) for p in d["parts"]), "parts": len(d["parts"]),
        "chainPoints": len(chain), "broken": stat["broken"],
        "stationsInFile": len(d["stations"]), "dropped": len(d["stations"]) - len(stops),
        "dupNames": dup[:8], "dupCount": len(dup),
        "offGeom": [{"name": s["name"], "dist": (round(s["snap"]) if s["snap"] < 1e9 else None)}
                    for s in off[:6]],
        "filledStations": len(filled), "note": detail.get("note") or auto_note,
        "catalog": {"name": cat.get("name"), "title": cat.get("title"),
                    "lengthKm": cat.get("length_km")} if cat else None,
        "line": detail,
    }
