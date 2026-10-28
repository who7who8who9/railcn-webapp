# -*- coding: utf-8 -*-
"""
fetch_geojson.py — 下载 HOT 官方中国铁路导出（含车站点 + 线路几何）

数据来源：Humanitarian Data Exchange / OSM，OpenStreetMap ODbL 授权，仅用于数据匹配与绘图。
下载后解压得到 data/railways.geojson（约 326MB），供 build_coords.py 建车站坐标库使用。
（本项目的线路走向只用 data/crtrain/geoms.json.gz，不需要这份全量 geojson。）

用法：
    python tools/fetch_geojson.py
"""
from __future__ import annotations

import json
import os
import zipfile

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
ZIP_PATH = os.path.join(DATA_DIR, "railways_geojson.zip")
URL = ("https://production-raw-data-api.s3.amazonaws.com/ISO3/CHN/railways/"
       "hotosm_chn_railways_osm_geojson.zip")


def main() -> None:
    import requests

    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(ZIP_PATH):
        print("[download]", URL)
        r = requests.get(URL, headers={"User-Agent": "railcn-local/1.0"}, timeout=900, stream=True)
        r.raise_for_status()
        with open(ZIP_PATH, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
        print(f"[download] {os.path.getsize(ZIP_PATH)/1024/1024:.0f} MB")

    print("[unzip]")
    with zipfile.ZipFile(ZIP_PATH) as z:
        z.extract("railways.geojson", DATA_DIR)
    for extra in ("README.txt", "config.yaml", "metadata.json"):
        p = os.path.join(DATA_DIR, extra)
        if os.path.exists(p):
            os.remove(p)

    path = os.path.join(DATA_DIR, "railways.geojson")
    with open(path, encoding="utf-8") as f:
        n = len(json.load(f).get("features", []))
    print(f"[done] {n} 个要素 -> {path}")


if __name__ == "__main__":
    main()
