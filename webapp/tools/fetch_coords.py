# -*- coding: utf-8 -*-
"""
fetch_coords.py — 按 12306 车站名单分块向 Overpass 取坐标

相比"整块 bbox 拉全国"，按站名精确匹配的分块查询返回量小、速度快得多：
  每 200 个站名拼成正则 ^(站A|站B|...)站?$，14 次左右请求即可覆盖全部 3365 个站。

输出 data/osm_stations.json（与整块查询同构，build_coords.py 可直接消费）。

用法：
    python tools/fetch_coords.py
    python tools/fetch_coords.py --chunk 150 --sleep 3
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
STATIONS_PATH = os.path.join(DATA_DIR, "stations.json")
OUT_PATH = os.path.join(DATA_DIR, "osm_stations.json")

ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
HEADERS = {"User-Agent": "railcn-local/1.0 (personal use; one-off geocoding)"}


def fetch(query: str, timeout: int = 240) -> dict | None:
    import requests

    for url in ENDPOINTS:
        for attempt in range(2):
            try:
                r = requests.post(url, data={"data": query}, headers=HEADERS, timeout=timeout)
                if r.status_code == 200:
                    return r.json()
                print(f"    ! {url} -> HTTP {r.status_code}")
            except Exception as exc:            # noqa: BLE001
                print(f"    ! {url} 失败: {type(exc).__name__}")
            time.sleep(2)
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunk", type=int, default=200)
    ap.add_argument("--sleep", type=float, default=2.0)
    ap.add_argument("--start", type=int, default=0)
    args = ap.parse_args()

    with open(STATIONS_PATH, encoding="utf-8") as f:
        st = json.load(f)
    names = sorted({(v.get("station_name") or "").replace(" ", "") for v in st.values()})
    names = [n for n in names if n]
    print(f"[names] 12306 车站 {len(names)} 个，分 {args.chunk} 个/批")

    # 支持中断续跑
    merged: dict[str, dict] = {}
    if os.path.exists(OUT_PATH):
        try:
            with open(OUT_PATH, encoding="utf-8") as f:
                for el in json.load(f).get("elements", []):
                    merged[str(el.get("id"))] = el
            print(f"[resume] 已有 {len(merged)} 个 OSM 元素")
        except Exception:                       # noqa: BLE001
            pass

    batches = [names[i:i + args.chunk] for i in range(0, len(names), args.chunk)]
    for bi, batch in enumerate(batches):
        if bi < args.start:
            continue
        # 站点名里可能有正则特殊字符（极少），做最小转义
        pattern = "^(" + "|".join(n.replace("(", "\\(").replace(")", "\\)") for n in batch) + ")站?$"
        query = (
            "[out:json][timeout:180];"
            f'node["railway"~"station|halt"]["name"~"{pattern}"](18,73,54,135);'
            "out body;"
        )
        t0 = time.time()
        data = fetch(query)
        if not data:
            print(f"[{bi+1}/{len(batches)}] 失败，跳过")
            continue
        els = data.get("elements", [])
        new = 0
        for el in els:
            k = str(el.get("id"))
            if k not in merged:
                merged[k] = el
                new += 1
        print(f"[{bi+1}/{len(batches)}] 返回 {len(els):4d}  新增 {new:4d}  累计 {len(merged):5d}  "
              f"{time.time()-t0:5.1f}s  样例: {batch[0]}")
        # 每批落盘
        with open(OUT_PATH, "w", encoding="utf-8") as f:
            json.dump({"elements": list(merged.values())}, f, ensure_ascii=False)
        time.sleep(args.sleep)

    print(f"[done] 共 {len(merged)} 个 OSM 站点 -> {OUT_PATH}")


if __name__ == "__main__":
    main()
