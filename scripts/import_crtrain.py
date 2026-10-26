#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
import_crtrain.py — 把 CRTrainRoute（Laide140/CRTrainRoute）的 OSM 线路数据整理进来

数据源（MIT）：
  data/crtrain/geojson/<省代码>.geojson   OSM 铁路线几何，要素带 name = 线路名
  data/crtrain/data_stations.json         12306 车站坐标（GCJ-02）+ 城市 / 地址
  data/crtrain/data_station_names.json    电报码 -> 站名
  data/crtrain/data_station_lanes.json    同址异名车站标注表

产出：
  data/crtrain/lines.json   每条线路的元信息（里程 / 段落 / 桥隧 / 要素数）
  data/crtrain/geoms.json   每条线路的简化折线（已把上下行并成一条）

做法：
  1. 只收 fclass=rail（排除地铁 / 有轨电车 / 轻轨，它们和国铁交织会带偏几何）
  2. 按 name 分组，节点量化到 1e-4°（约 11 m）后建图 —— 上下行两条平行轨
     在这个精度下并成一条，避免「京沪线 2994 km」这种翻倍
  3. 每个连通段算直径（两次 Dijkstra）当作该段走行里程；各段相加即线路总长
  4. 图里抽出度数 != 2 的节点之间的链，Douglas-Peucker 抽稀到 25 m
"""
from __future__ import annotations

import glob
import gzip
import heapq
import json
import math
import os
import sys
import time
from collections import defaultdict
from typing import Dict, List, Tuple

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE, "data", "crtrain", "geojson")
OUT_DIR = os.path.join(BASE, "data", "crtrain")

QUANT = 1e-4          # 节点量化（约 11 m）：上下行并行轨在这里合并
SIMPLIFY_M = 25.0     # 折线抽稀容差（米）
MIN_COMP_KM = 0.5     # 短于此的连通段当断链碎屑，不参与里程
DROP_NAMES = {"", "-", "未命名"}


def hav(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """大圆距离（公里）"""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(a))


def _seg_dist_m(lon: float, lat: float, a: Tuple[float, float], b: Tuple[float, float]) -> float:
    """点到线段的距离（米，局部平面近似，纬度 30° 一带误差可忽略）"""
    kx = 111320.0 * math.cos(math.radians(lat))
    ky = 110540.0
    px, py = (lon - a[0]) * kx, (lat - a[1]) * ky
    bx, by = (b[0] - a[0]) * kx, (b[1] - a[1]) * ky
    den = bx * bx + by * by
    t = 0.0 if den <= 0 else max(0.0, min(1.0, (px * bx + py * by) / den))
    dx, dy = px - t * bx, py - t * by
    return math.hypot(dx, dy)


def simplify(pts: List[Tuple[float, float]], tol_m: float = SIMPLIFY_M) -> List[Tuple[float, float]]:
    """Douglas-Peucker：容差按米给，首尾必留"""
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        mid_lat = (pts[i][1] + pts[j][1]) / 2
        best, bestd = -1, tol_m
        for k in range(i + 1, j):
            d = _seg_dist_m(pts[k][0], pts[k][1], pts[i], pts[j]) if mid_lat is not None else 0
            if d > bestd:
                best, bestd = k, d
        if best > 0:
            keep[best] = True
            stack.append((i, best))
            stack.append((best, j))
    return [p for p, k in zip(pts, keep) if k]


def load_features() -> Dict[str, List[dict]]:
    """按线路名归集 OSM 要素：name -> [{osmId, bridge, tunnel, layer, lines}]"""
    by_name: Dict[str, List[dict]] = defaultdict(list)
    files = sorted(glob.glob(os.path.join(SRC, "*.geojson")))
    if not files:
        print(f"没找到 geojson：{SRC}（先跑 scripts/fetch_crtrain.py）", file=sys.stderr)
        return {}
    total = kept = 0
    for path in files:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            print(f"  跳过 {os.path.basename(path)}：{e}", file=sys.stderr)
            continue
        for ft in data.get("features") or []:
            total += 1
            pr = ft.get("properties") or {}
            if pr.get("fclass") != "rail":          # 地铁 / 有轨 / 轻轨统统不要
                continue
            nm = str(pr.get("name") or "").strip()
            if nm in DROP_NAMES:
                continue
            geo = ft.get("geometry") or {}
            gtype = geo.get("type")
            if gtype == "LineString":
                lines = [geo.get("coordinates") or []]
            elif gtype == "MultiLineString":
                lines = geo.get("coordinates") or []
            else:
                continue
            good = []
            for ln in lines:
                pts = []
                for c in ln:
                    if len(c) < 2:
                        continue
                    try:
                        pts.append((float(c[0]), float(c[1])))
                    except (TypeError, ValueError):
                        continue
                if len(pts) >= 2:
                    good.append(pts)
            if not good:
                continue
            kept += 1
            by_name[nm].append({
                "osmId": str(pr.get("osm_id") or ""),
                "bridge": pr.get("bridge") == "T",
                "tunnel": pr.get("tunnel") == "T",
                "layer": pr.get("layer"),
                "lines": good,
            })
    print(f"要素 {total} 个，收下铁路要素 {kept} 个，线路名 {len(by_name)} 个")
    return by_name


def join_parts(parts: List[List[Tuple[float, float]]], tol_m: float = 40.0) -> List[List[Tuple[float, float]]]:
    """
    把 OSM 的碎段接成长折线（OSM 在这份数据里一条线被切成几千段 2~3 点的 way）。

    只有「端点相距 < 40 m 且转向 < 90°」才接，避免上下行在渡线处来回跳成锯齿。
    """
    if len(parts) < 2:
        return parts
    key = lambda p: (round(p[0], 5), round(p[1], 5))          # noqa: E731

    def near(a, b):
        return hav(a[0], a[1], b[0], b[1]) * 1000 <= tol_m

    def turn_ok(tail_from, tail_to, head_to):
        """延续方向夹角不超过 90°"""
        import math as _m
        a1 = _m.atan2(tail_to[1] - tail_from[1], tail_to[0] - tail_from[0])
        a2 = _m.atan2(head_to[1] - tail_to[1], head_to[0] - tail_to[0])
        d = abs((a2 - a1 + _m.pi) % (2 * _m.pi) - _m.pi)
        return d < _m.pi / 2

    used = [False] * len(parts)
    at_start: Dict[tuple, List[int]] = defaultdict(list)      # 端点键 -> 段序号
    at_end: Dict[tuple, List[int]] = defaultdict(list)
    for i, pts in enumerate(parts):
        at_start[key(pts[0])].append(i)
        at_end[key(pts[-1])].append(i)

    def find_next(pos, dir_pt):
        """找一个端点贴在 pos 上的未用段，返回 (序号, 是否需要反向)"""
        k = key(pos)
        for cand in at_start.get(k, []) + at_end.get(k, []):
            if used[cand]:
                continue
            pts = parts[cand]
            if near(pts[0], pos):
                rev = False
                other = pts[-1]
            else:
                rev = True
                other = pts[0]
            if len(pts) >= 2 and not turn_ok(dir_pt, pos, other):
                continue
            return cand, rev
        return None, False

    out: List[List[Tuple[float, float]]] = []
    for i, pts in enumerate(parts):
        if used[i]:
            continue
        used[i] = True
        chain = list(pts)
        # 往后接
        while True:
            if len(chain) < 2:
                break
            cand, rev = find_next(chain[-1], chain[-2])
            if cand is None:
                break
            used[cand] = True
            seg = parts[cand]
            chain.extend(reversed(seg) if rev else seg[1:])
        # 往前接
        while True:
            if len(chain) < 2:
                break
            cand, rev = find_next(chain[0], chain[1])
            if cand is None:
                break
            used[cand] = True
            seg = parts[cand]
            add = seg if rev else list(reversed(seg))
            chain[:0] = add[:-1]
        out.append(chain)
    return out


def build_line(nm: str, feats: List[dict]) -> dict:
    """把一条线的所有要素并成一张图：算里程、段落、桥隧；再抽稀成折线"""
    adj: Dict[Tuple[int, int], Dict[Tuple[int, int], float]] = defaultdict(dict)
    n_bridge = n_tunnel = 0
    for ft in feats:
        if ft["bridge"]:
            n_bridge += 1
        if ft["tunnel"]:
            n_tunnel += 1
        for ln in ft["lines"]:
            prev = None
            for lon, lat in ln:
                q = (int(round(lon / QUANT)), int(round(lat / QUANT)))
                if prev is not None and q != prev:
                    w = hav(prev[0] * QUANT, prev[1] * QUANT, q[0] * QUANT, q[1] * QUANT)
                    if w <= 0:
                        w = 1e-6
                    old = adj[prev].get(q)
                    if old is None or w < old:
                        adj[prev][q] = w
                        adj[q][prev] = w
                prev = q

    if not adj:
        return {}

    # ---- 连通段 + 每段直径（两次 Dijkstra）----
    seen = set()
    comps: List[List[Tuple[int, int]]] = []
    for s in adj:
        if s in seen:
            continue
        stack = [s]
        seen.add(s)
        mem = []
        while stack:
            u = stack.pop()
            mem.append(u)
            for v in adj[u]:
                if v not in seen:
                    seen.add(v)
                    stack.append(v)
        comps.append(mem)

    def far(src):
        dist = {src: 0.0}
        pq = [(0.0, src)]
        best = (0.0, src)
        while pq:
            d, u = heapq.heappop(pq)
            if d > dist.get(u, 1e18):
                continue
            if d > best[0]:
                best = (d, u)
            for v, w in adj[u].items():
                nd = d + w
                if nd < dist.get(v, 1e18):
                    dist[v] = nd
                    heapq.heappush(pq, (nd, v))
        return best

    comp_km = []
    for mem in comps:
        if len(mem) < 2:
            continue
        _, p1 = far(mem[0])
        d2, _ = far(p1)
        comp_km.append(d2)
    comp_km.sort(reverse=True)
    km_parts = [round(x, 2) for x in comp_km if x >= MIN_COMP_KM]
    km = round(sum(km_parts), 1)
    km_main = km_parts[0] if km_parts else 0.0

    # ---- 折线：OSM 原始要素 → 接长 → 抽稀 ----
    # 里程用上面的量化图算（上下行已合并，不会翻倍）；画线保留原始走向，
    # 先把 2~3 点一段的碎 way 接成整条，再 Douglas-Peucker 抽稀到 25 m。
    raw_parts: List[List[Tuple[float, float]]] = []
    n_raw = n_keep = 0
    seen_ids = set()
    for ft in feats:
        oid = ft["osmId"]
        if oid and oid in seen_ids:      # 同一条要素可能被邻省文件重复收录
            continue
        if oid:
            seen_ids.add(oid)
        for pts in ft["lines"]:
            if len(pts) >= 2:
                n_raw += len(pts)
                raw_parts.append(pts)
    simplified: List[List[float]] = []
    for pts in join_parts(raw_parts):
        sp = simplify(pts)
        n_keep += len(sp)
        if len(sp) >= 2:
            simplified.append([[round(x, 5), round(y, 5)] for x, y in sp])
    if not simplified:
        return {}
    simplified.sort(key=len, reverse=True)      # 大段优先，前端截断时先丢碎段

    lons = [p[0] for part in simplified for p in part]
    lats = [p[1] for part in simplified for p in part]
    return {
        "name": nm,
        "km": km,
        "kmMain": round(km_main, 1),
        "nPart": len(km_parts),
        "nFrag": len(simplified),
        "nFeat": len(feats),
        "nBridge": n_bridge,
        "nTunnel": n_tunnel,
        "nPt": n_keep,
        "bbox": [round(min(lons), 5), round(min(lats), 5),
                 round(max(lons), 5), round(max(lats), 5)],
        "geom": simplified,
    }


def main() -> int:
    t0 = time.time()
    by_name = load_features()
    if not by_name:
        return 1
    os.makedirs(OUT_DIR, exist_ok=True)

    metas: List[dict] = []
    geoms: Dict[str, list] = {}
    names = sorted(by_name, key=lambda n: -len(by_name[n]))
    for i, nm in enumerate(names, 1):
        rec = build_line(nm, by_name[nm])
        if not rec:
            continue
        geoms[nm] = rec.pop("geom")
        metas.append(rec)
        if i % 200 == 0:
            print(f"  {i}/{len(names)} …", flush=True)

    metas.sort(key=lambda r: -r["km"])
    with open(os.path.join(OUT_DIR, "lines.json"), "w", encoding="utf-8") as f:
        json.dump({"source": "Laide140/CRTrainRoute (OSM)",
                   "updated": time.strftime("%Y-%m-%d"),
                   "count": len(metas), "lines": metas}, f, ensure_ascii=False)
    # 几何落成 .json.gz：4 MB 的裸 JSON 压完 1.4 MB，进仓库不心疼
    raw = json.dumps(geoms, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with open(os.path.join(OUT_DIR, "geoms.json.gz"), "wb") as f:
        f.write(gzip.compress(raw, 9))
    plain = os.path.join(OUT_DIR, "geoms.json")
    if os.path.exists(plain):
        os.remove(plain)                 # 老版本留下的裸文件，别再占地方

    sz1 = os.path.getsize(os.path.join(OUT_DIR, "lines.json")) / 1e6
    sz2 = os.path.getsize(os.path.join(OUT_DIR, "geoms.json.gz")) / 1e6
    print(f"\n线路 {len(metas)} 条，总里程 {sum(r['km'] for r in metas):,.0f} km")
    print(f"lines.json {sz1:.2f} MB，geoms.json.gz {sz2:.2f} MB，用时 {time.time()-t0:.1f}s")
    for r in metas[:10]:
        print(f"  {r['name']:14s} {r['km']:8.1f} km 主干 {r['kmMain']:8.1f} "
              f"段 {r['nPart']} 要素 {r['nFeat']} 桥 {r['nBridge']} 隧 {r['nTunnel']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
