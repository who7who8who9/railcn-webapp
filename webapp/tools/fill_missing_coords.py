# -*- coding: utf-8 -*-
"""
fill_missing_coords.py — 补齐 OSM 导出里缺失的车站坐标

data/coord_report.json 记录了 12306 有、但 HOT 导出里没匹配上的站名（约 230 个）。
这里按站名分小块向 Overpass 精确查询，命中后合并进 station_coords.json。

用法：
    python tools/fill_missing_coords.py --chunk 60
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
COORDS = os.path.join(DATA_DIR, "station_coords.json")
REPORT = os.path.join(DATA_DIR, "coord_report.json")

ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
HEADERS = {"User-Agent": "railcn-local/1.0 (personal use)"}


def norm(s: str) -> str:
    s = re.sub(r"\s+", "", str(s or ""))
    if len(s) > 2 and s.endswith("站"):
        s = s[:-1]
    return s


def query(names: list[str]) -> list[dict] | None:
    import requests

    pat = "^(" + "|".join(re.escape(n) for n in names) + ")站?$"
    q = ('[out:json][timeout:120];'
         f'node["railway"~"station|halt"]["name"~"{pat}"](18,73,54,135);out body;')
    for url in ENDPOINTS:
        try:
            r = requests.post(url, data={"data": q}, headers=HEADERS, timeout=180)
            if r.status_code == 200:
                return r.json().get("elements", [])
            print(f"  ! {url} HTTP {r.status_code}", flush=True)
        except Exception as exc:            # noqa: BLE001
            print(f"  ! {url} {type(exc).__name__}", flush=True)
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunk", type=int, default=60)
    ap.add_argument("--sleep", type=float, default=2.0)
    args = ap.parse_args()

    with open(REPORT, encoding="utf-8") as f:
        rep = json.load(f)
    missing = [norm(n) for n in rep.get("missingSample", [])]
    with open(COORDS, encoding="utf-8") as f:
        coords = json.load(f)

    todo = [n for n in missing if n and n not in coords]
    print(f"[todo] 待补 {len(todo)} 个站", flush=True)

    got = 0
    for i in range(0, len(todo), args.chunk):
        batch = todo[i:i + args.chunk]
        els = query(batch)
        if els is None:
            print(f"[{i//args.chunk+1}] 失败", flush=True)
            continue
        add = 0
        for el in els:
            tags = el.get("tags") or {}
            for key in ("name", "name:zh", "alt_name"):
                for part in str(tags.get(key) or "").split(";"):
                    n = norm(part)
                    if n in batch and n not in coords and el.get("lat") is not None:
                        coords[n] = [round(el["lat"], 6), round(el["lon"], 6)]
                        add += 1
        got += add
        print(f"[{i//args.chunk+1}/{(len(todo)+args.chunk-1)//args.chunk}] 返回 {len(els)} 新增 {add} 累计 {got}", flush=True)
        with open(COORDS, "w", encoding="utf-8") as f:
            json.dump(coords, f, ensure_ascii=False, separators=(",", ":"))
        time.sleep(args.sleep)

    print(f"[done] 补充 {got} 个，坐标库共 {len(coords)} 条", flush=True)
    # 更新报告
    still = [n for n in todo if n not in coords]
    rep["hit"] = rep.get("hit", 0) + got
    rep["missing"] = len(still)
    rep["rate"] = round(rep["hit"] / max(rep.get("total", 1), 1) * 100, 1)
    rep["missingSample"] = still
    with open(REPORT, "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
