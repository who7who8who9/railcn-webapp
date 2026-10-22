# -*- coding: utf-8 -*-
"""
railgraph.py — 用 OSM 铁路线几何把「站点直连」变成「沿真实线路」

数据源：data/railways.geojson（HOT 官方中国铁路导出，41 万条 railway=rail 线）
做法：
  1. 预处理成稀疏网格索引（cell 0.3° -> way 序号），落盘 pickle，避免每次读 326MB JSON
  2. 查询时只把 A→B 包围盒内的 way 建成局部图，A* 求最短路径
  3. 结果按距离阈值抽稀，缓存到 SQLite（routes.geom）

效果：路线贴合实际走向，里程接近真实运营里程。
"""
from __future__ import annotations

import heapq
import json
import math
import os
import pickle
import time
from typing import Dict, List, Optional, Tuple

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
GEOJSON = os.path.join(DATA_DIR, "railways.geojson")
GRAPH_PKL = os.path.join(DATA_DIR, "rail_graph.pkl")

CELL = 0.3          # 网格大小（度）
PREC = 1e5          # 坐标量化精度（1e-5 ≈ 1m）


def haversine(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _cell(lon: float, lat: float) -> Tuple[int, int]:
    return int(math.floor(lon / CELL)), int(math.floor(lat / CELL))


def build_index(force: bool = False) -> dict:
    """把 geojson 转成 {ways, grid} 的紧凑结构并落盘"""
    if os.path.exists(GRAPH_PKL) and not force:
        with open(GRAPH_PKL, "rb") as f:
            return pickle.load(f)

    t0 = time.time()
    print("[graph] 读取 geojson …", flush=True)
    with open(GEOJSON, encoding="utf-8") as f:
        data = json.load(f)

    ways: List[List[Tuple[float, float]]] = []
    for ft in data.get("features", []):
        props = ft.get("properties") or {}
        if props.get("railway") != "rail":
            continue
        geo = ft.get("geometry") or {}
        if geo.get("type") != "LineString":
            continue
        coords = geo.get("coordinates") or []
        if len(coords) < 2:
            continue
        ways.append([(float(c[0]), float(c[1])) for c in coords])
    print(f"[graph] 线路 {len(ways)} 条，建网格索引 …", flush=True)

    grid: Dict[Tuple[int, int], List[int]] = {}
    for wi, pts in enumerate(ways):
        lons = [p[0] for p in pts]
        lats = [p[1] for p in pts]
        x0, y0 = _cell(min(lons), min(lats))
        x1, y1 = _cell(max(lons), max(lats))
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                grid.setdefault((x, y), []).append(wi)

    payload = {"ways": ways, "grid": grid, "cell": CELL}
    with open(GRAPH_PKL, "wb") as f:
        pickle.dump(payload, f, protocol=4)
    print(f"[graph] 完成 {time.time()-t0:.0f}s，索引 {len(grid)} 格 -> {GRAPH_PKL}", flush=True)
    return payload


class RailRouter:
    def __init__(self, data: Optional[dict] = None):
        self.data = data or build_index()
        self.ways: List[List[Tuple[float, float]]] = self.data["ways"]
        self.grid: Dict[Tuple[int, int], List[int]] = self.data["grid"]

    # ---- 局部图 ----
    def _local_graph(self, a: Tuple[float, float], b: Tuple[float, float]):
        """a/b = (lon, lat)。返回 邻接表 {node:(lat,lon)}"""
        pad = max(0.25, abs(a[0] - b[0]) * 0.15, abs(a[1] - b[1]) * 0.15)
        x0, y0 = _cell(min(a[0], b[0]) - pad, min(a[1], b[1]) - pad)
        x1, y1 = _cell(max(a[0], b[0]) + pad, max(a[1], b[1]) + pad)
        seen = set()
        adj: Dict[Tuple[int, int], List[Tuple[Tuple[int, int], float]]] = {}
        pos: Dict[Tuple[int, int], Tuple[float, float]] = {}
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                for wi in self.grid.get((x, y), ()):  # type: ignore[arg-type]
                    if wi in seen:
                        continue
                    seen.add(wi)
                    pts = self.ways[wi]
                    prev = None
                    for lon, lat in pts:
                        k = (int(round(lon * PREC)), int(round(lat * PREC)))
                        if k not in pos:
                            pos[k] = (lat, lon)
                            adj.setdefault(k, [])
                        if prev is not None and prev != k:
                            d = haversine(pos[prev][0], pos[prev][1], lat, lon)
                            adj[prev].append((k, d))
                            adj[k].append((prev, d))
                        prev = k
        return adj, pos

    @staticmethod
    def _merge_nodes(adj, pos, tol_m: float = 60.0):
        """
        OSM 里不少线路端点相差几十米没有共享节点，直接建图会碎成一堆连通分量。
        这里按容差做并查集合并，把断点接上。
        """
        parent = {k: k for k in adj}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        bucket: Dict[Tuple[int, int], List[Tuple[int, int]]] = {}
        cell = 0.0015                       # ≈ 150m
        for k, (lat, lon) in pos.items():
            bx, by = int(lon // cell), int(lat // cell)
            bucket.setdefault((bx, by), []).append(k)

        tol_deg = tol_m / 111000.0
        for (bx, by), ks in bucket.items():
            neigh = []
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    neigh.extend(bucket.get((bx + dx, by + dy), ()))
            for k in ks:
                lat, lon = pos[k]
                for m in neigh:
                    if m <= k:
                        continue
                    lat2, lon2 = pos[m]
                    if abs(lat - lat2) < tol_deg and abs(lon - lon2) < tol_deg:
                        union(k, m)

        groups: Dict[Any, List[Any]] = {}
        for k in adj:
            groups.setdefault(find(k), []).append(k)

        newpos: Dict[Any, Tuple[float, float]] = {}
        rep: Dict[Any, Any] = {}
        for root, members in groups.items():
            lat = sum(pos[m][0] for m in members) / len(members)
            lon = sum(pos[m][1] for m in members) / len(members)
            newpos[root] = (lat, lon)
            for m in members:
                rep[m] = root

        newadj: Dict[Any, List[Tuple[Any, float]]] = {r: [] for r in groups}
        for u, edges in adj.items():
            ru = rep[u]
            for v, w in edges:
                rv = rep[v]
                if rv != ru:
                    newadj[ru].append((rv, w))
        return newadj, newpos

    @staticmethod
    def _nearest(pos, lat: float, lon: float):
        best, bd = None, 1e9
        for k, (plat, plon) in pos.items():
            d = abs(plat - lat) + abs(plon - lon)
            if d < bd:
                bd, best = d, k
        return best

    def route(self, lat1, lon1, lat2, lon2, simplify_km: float = 0.15):
        """返回 (polyline[(lat,lon)], 距离km)，失败返回 (None, 0)"""
        adj, pos = self._local_graph((lon1, lat1), (lon2, lat2))
        if not adj:
            return None, 0.0
        adj, pos = self._merge_nodes(adj, pos)
        start = self._nearest(pos, lat1, lon1)
        goal = self._nearest(pos, lat2, lon2)
        if start is None or goal is None:
            return None, 0.0

        def h(k):
            p = pos[k]
            return haversine(p[0], p[1], lat2, lon2)

        dist = {start: 0.0}
        prev: Dict[Tuple[int, int], Tuple[int, int]] = {}
        pq = [(h(start), 0.0, start)]
        visited = set()
        while pq:
            _, g, u = heapq.heappop(pq)
            if u in visited:
                continue
            visited.add(u)
            if u == goal:
                break
            for v, w in adj.get(u, ()):
                ng = g + w
                if ng < dist.get(v, 1e18):
                    dist[v] = ng
                    prev[v] = u
                    heapq.heappush(pq, (ng + h(v), ng, v))
        if goal not in dist:
            return None, 0.0

        # 回溯
        path = [goal]
        while path[-1] in prev:
            path.append(prev[path[-1]])
        path.reverse()
        pts = [pos[k] for k in path]

        # 抽稀（Douglas-Peucker 简化版：按垂直距离）
        pts = self._dp(pts, 0.0008)
        dist_km = dist[goal]
        return pts, dist_km

    @staticmethod
    def _dp(pts, eps):
        if len(pts) < 3:
            return pts
        # 迭代式 RDP，避免深递归
        keep = [False] * len(pts)
        keep[0] = keep[-1] = True
        stack = [(0, len(pts) - 1)]
        while stack:
            i, j = stack.pop()
            if j <= i + 1:
                continue
            ax, ay = pts[i][1], pts[i][0]
            bx, by = pts[j][1], pts[j][0]
            dx, dy = bx - ax, by - ay
            norm = math.hypot(dx, dy) or 1e-12
            dmax, idx = -1.0, -1
            for k in range(i + 1, j):
                px, py = pts[k][1], pts[k][0]
                d = abs(dy * px - dx * py + bx * ay - by * ax) / norm
                if d > dmax:
                    dmax, idx = d, k
            if dmax > eps and idx > 0:
                keep[idx] = True
                stack.append((i, idx))
                stack.append((idx, j))
        return [p for p, k in zip(pts, keep) if k]


if __name__ == "__main__":
    t = time.time()
    r = RailRouter()
    print(f"load {time.time()-t:.1f}s")
    tests = [
        ("北京南→沧州西", 39.865, 116.378, 38.306, 116.762),
        ("南京南→上海虹桥", 31.973, 118.792, 31.196, 121.316),
    ]
    for name, la1, lo1, la2, lo2 in tests:
        t = time.time()
        pts, km = r.route(la1, lo1, la2, lo2)
        print(f"{name}: {km:.1f} km, {len(pts) if pts else 0} pts, {time.time()-t:.2f}s")
        if pts:
            print("   head", [(round(p[0], 3), round(p[1], 3)) for p in pts[:4]])
