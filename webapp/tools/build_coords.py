# -*- coding: utf-8 -*-
"""
build_coords.py — 生成车站坐标库 data/station_coords.json

12306 的 station_name.js 只有站名和电报码，没有经纬度。这里用 OSM 的铁路站点数据
（HOT 官方导出 railways.geojson，或 Overpass 结果）做站名匹配，一次性落成本地坐标库。

坐标系统一存 WGS-84，前端渲染前再转 GCJ-02。

用法：
    python tools/build_coords.py                       # 用 data/railways.geojson
    python tools/build_coords.py --src data/osm_stations.json
"""
from __future__ import annotations

import argparse
import json
import os
import re

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
GEOJSON = os.path.join(DATA_DIR, "railways.geojson")
OUT_PATH = os.path.join(DATA_DIR, "station_coords.json")
STATIONS_PATH = os.path.join(DATA_DIR, "stations.json")
REPORT_PATH = os.path.join(DATA_DIR, "coord_report.json")


def norm(name: str) -> str:
    s = re.sub(r"\s+", "", str(name or ""))
    if len(s) > 2 and s.endswith("站"):
        s = s[:-1]
    return s


def from_geojson(path: str) -> list[tuple[str, float, float, int]]:
    """返回 [(站名, lon, lat, 权重)]"""
    print(f"[load] 读取 {os.path.basename(path)} …")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    out = []
    for ft in data.get("features", []):
        props = ft.get("properties") or {}
        if props.get("railway") != "station":
            continue
        geo = ft.get("geometry") or {}
        gtype, coords = geo.get("type"), geo.get("coordinates")
        if not coords:
            continue
        if gtype == "Point":
            lon, lat = coords[0], coords[1]
            weight = 3
        elif gtype == "Polygon":
            ring = coords[0]
            lon = sum(p[0] for p in ring) / len(ring)
            lat = sum(p[1] for p in ring) / len(ring)
            weight = 2
        elif gtype == "MultiPolygon":
            ring = coords[0][0]
            lon = sum(p[0] for p in ring) / len(ring)
            lat = sum(p[1] for p in ring) / len(ring)
            weight = 2
        else:
            continue
        for key in ("name", "name_zh"):
            n = norm(props.get(key))
            if n:
                out.append((n, lon, lat, weight))
    print(f"[load] 车站要素 {len(out)} 条")
    return out


def from_elements(path: str) -> list[tuple[str, float, float, int]]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    out = []
    for el in data.get("elements", []):
        tags = el.get("tags") or {}
        if el.get("lat") is None:
            continue
        rank = {"station": 3, "halt": 2}.get(tags.get("railway"), 1)
        for key in ("name", "name:zh", "alt_name"):
            v = tags.get(key)
            if not v:
                continue
            for part in str(v).split(";"):
                n = norm(part)
                if n:
                    out.append((n, el["lon"], el["lat"], rank))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=None)
    args = ap.parse_args()

    src = args.src or (GEOJSON if os.path.exists(GEOJSON) else os.path.join(DATA_DIR, "osm_stations.json"))
    if not os.path.exists(src):
        raise SystemExit("找不到数据源：先运行 tools/fetch_geojson.py 或 tools/fetch_coords.py")
    rows = from_geojson(src) if src.endswith(".geojson") else from_elements(src)

    # 同名多候选：取权重最高；权重相同取第一个
    best: dict[str, tuple[float, float, int]] = {}
    for name, lon, lat, w in rows:
        cur = best.get(name)
        if cur is None or w > cur[2]:
            best[name] = (lon, lat, w)

    coords = {n: [round(v[1], 6), round(v[0], 6)] for n, v in best.items()}   # -> [lat, lon]
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(coords, f, ensure_ascii=False, separators=(",", ":"))

    # 覆盖率
    if os.path.exists(STATIONS_PATH):
        with open(STATIONS_PATH, encoding="utf-8") as f:
            st = json.load(f)
        names = {norm(v.get("station_name")) for v in st.values()}
        names.discard("")
        hit = [n for n in names if n in coords]
        miss = sorted(n for n in names if n not in coords)
        report = {
            "total": len(names),
            "hit": len(hit),
            "missing": len(miss),
            "rate": round(len(hit) / max(len(names), 1) * 100, 1),
            "missingSample": miss[:500],
        }
        with open(REPORT_PATH, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
        print(f"[coverage] 12306 车站 {report['total']}，命中 {report['hit']}（{report['rate']}%），缺 {report['missing']}")
        print(f"[coverage] 缺失清单：{REPORT_PATH}")

    print(f"[done] {len(coords)} 条坐标 -> {OUT_PATH}")


if __name__ == "__main__":
    main()
