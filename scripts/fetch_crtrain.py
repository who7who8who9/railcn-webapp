#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_crtrain.py — 从 GitHub 拉 CRTrainRoute 的 OSM 铁路数据（MIT）

下载内容（约 142 MB，落在 data/crtrain/，geojson 已在 .gitignore 里）：
  geojson/<省代码>.geojson    32 个省级铁路线几何，要素带 name = 线路名
  data_stations.json          12306 车站坐标（GCJ-02）+ 城市 / 地址
  data_station_names.json     电报码 -> 站名
  data_station_lanes.json     同址异名车站标注表

用法：
  python scripts/fetch_crtrain.py            # 缺什么补什么
  python scripts/fetch_crtrain.py --force    # 全部重下
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import sys
import time
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "data", "crtrain")
RAW = "https://raw.githubusercontent.com/Laide140/CRTrainRoute/main/"

CODES = ["110000", "120000", "130000", "140000", "150000", "210000", "220000", "230000",
         "310000", "320000", "330000", "340000", "350000", "360000", "370000",
         "410000", "420000", "430000", "440000", "450000", "460000",
         "500000", "510000", "520000", "530000", "540000",
         "610000", "620000", "630000", "640000", "650000",
         "810000", "820000"]
EXTRA = ["data/stations.json", "data/station_names.json", "data/station_lanes.json"]


def grab(url: str, dst: str, tries: int = 3) -> tuple:
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "railcn/1.0"})
            with urllib.request.urlopen(req, timeout=180) as r:
                blob = r.read()
            if dst.endswith(".json"):
                json.loads(blob.decode("utf-8"))       # 校验，防止半截文件
            with open(dst, "wb") as f:
                f.write(blob)
            return True, len(blob)
        except Exception as e:                          # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
            time.sleep(1.2 * (i + 1))
    return False, err


def main() -> int:
    force = "--force" in sys.argv
    os.makedirs(os.path.join(OUT, "geojson"), exist_ok=True)
    jobs = []
    for c in CODES:
        jobs.append((RAW + f"data/geojson/{c}.geojson",
                     os.path.join(OUT, "geojson", f"{c}.geojson")))
    for p in EXTRA:
        jobs.append((RAW + p, os.path.join(OUT, p.replace("/", "_"))))

    todo = [(u, d) for u, d in jobs
            if force or not os.path.exists(d) or os.path.getsize(d) < 1000]
    if not todo:
        print("都是最新的，无需下载（--force 可强刷）")
        return 0
    print(f"要下载 {len(todo)} 个文件 …")
    ok = 0
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(grab, u, d): d for u, d in todo}
        for fut in cf.as_completed(futs):
            good, info = fut.result()
            name = os.path.basename(futs[fut])
            if good:
                ok += 1
                print(f"  ✓ {name:24s} {info/1e6:6.2f} MB", flush=True)
            else:
                print(f"  ✗ {name:24s} {info}", flush=True)
    print(f"完成 {ok}/{len(todo)}，用时 {time.time()-t0:.1f}s")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
