# -*- coding: utf-8 -*-
"""
抓取 tool.huiruisoft.com 的中国铁路线路一览（辉睿软件），解析成结构化 JSON。

输出 data/huirui/lines.json
  [{
     "id": "1506", "url": "...", "name": "京沪线", "title": "京沪铁路",
     "meta": {"中文全称": "京沪铁路", "铁路类型": "普速铁路", ...},
     "intro": "……",                       # 纯文本，段落用 \n\n 分隔
     "stops": [{"name":"北京", "km":0.0, "note":"", "sid":"10001"}, ...],
     "junctions": [{"name":"京哈铁路", "id":"1510"}],   # 图上提到的接轨线路
     "totalKm": 1463.0
   }, ...]

页面原始 HTML 缓存在 data/huirui/html/<id>.html，重复运行不会重复下载。
用法：
    python scripts/fetch_huirui.py            # 增量抓取（缺的才下）
    python scripts/fetch_huirui.py --force    # 全部重下
    python scripts/fetch_huirui.py --ids 1506,1001   # 只处理几条
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

import requests
from bs4 import BeautifulSoup

BASE = "https://tool.huiruisoft.com/train/railway/"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "data", "huirui")
HTML_DIR = os.path.join(OUT_DIR, "html")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

_session = requests.Session()
_session.headers.update({"User-Agent": UA, "Referer": BASE + "index.html"})


def fetch(url: str, cache: str, force: bool = False, tries: int = 3) -> Optional[str]:
    if not force and os.path.exists(cache) and os.path.getsize(cache) > 2000:
        with open(cache, encoding="utf-8") as f:
            return f.read()
    last = None
    for k in range(tries):
        try:
            r = _session.get(url, timeout=30)
            r.raise_for_status()
            r.encoding = "utf-8"
            os.makedirs(os.path.dirname(cache), exist_ok=True)
            with open(cache, "w", encoding="utf-8") as f:
                f.write(r.text)
            time.sleep(0.12)
            return r.text
        except Exception as e:                                  # noqa: BLE001
            last = e
            time.sleep(1.2 * (k + 1))
    print("  ! 失败", url, last, file=sys.stderr)
    return None


def index_items(force: bool = False) -> List[Dict[str, str]]:
    html = fetch(BASE + "index.html", os.path.join(OUT_DIR, "index.html"), force)
    if not html:
        raise SystemExit("索引页抓取失败")
    soup = BeautifulSoup(html, "lxml")
    seen, out = set(), []
    for a in soup.select("#nationContent li a[href]"):
        href = a.get("href") or ""
        m = re.fullmatch(r"(\d{3,5})\.html", href)
        if not m:
            continue
        lid = m.group(1)
        name = a.get_text(strip=True)
        if not name or lid in seen:
            continue
        seen.add(lid)
        out.append({"id": lid, "name": name})
    return out


def _cells(tr) -> List[str]:
    return [td.get_text(" ", strip=True).replace("\u00a0", " ") for td in tr.find_all("td")]


def _num(s: str) -> Optional[float]:
    s = (s or "").replace(",", "").strip()
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return round(float(m.group(0)), 1)
    except ValueError:
        return None


def parse(lid: str, html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    title = (soup.title.get_text(strip=True) if soup.title else "").replace("中国铁路网-", "").strip()

    meta: Dict[str, str] = {}
    intro_parts: List[str] = []
    info = soup.select_one("table.cc-table")
    if info:
        for tr in info.find_all("tr"):
            tds = tr.find_all("td", recursive=False)
            if len(tds) <= 1:                                # 介绍段落（colspan=4）
                if not tds:
                    continue
                # 按 <p> 分段；段内不加分隔符，免得行内链接把句子切碎
                ps = tds[0].find_all("p")
                if ps:
                    for el in ps:
                        txt = el.get_text("", strip=True)
                        if txt:
                            intro_parts.append(txt)
                else:
                    txt = tds[0].get_text("", strip=True)
                    if txt:
                        intro_parts.append(txt)
                continue
            for i in range(0, len(tds) - 1, 2):
                label = re.sub(r"\s+", "",
                               tds[i].get_text(" ", strip=True).replace("\u00a0", " "))
                val = tds[i + 1].get_text(" ", strip=True).replace("\u00a0", " ").strip()
                if label and val:
                    meta.setdefault(label, val)

    # 线路图：km | 站名 | 备注，逐行
    stops: List[dict] = []
    junctions: Dict[str, str] = {}
    route = soup.select_one("table.table-route")
    if route:
        for tr in route.find_all("tr"):
            tds = tr.find_all("td", recursive=False)
            if len(tds) < 3:
                continue
            kmtxt = tds[1].get_text(" ", strip=True)
            name_td = tds[2]
            link = name_td.find("a")
            sname = name_td.get_text(" ", strip=True)
            note = tds[3].get_text(" ", strip=True) if len(tds) > 3 else ""
            for a in (tds[3].find_all("a") if len(tds) > 3 else []):
                nm = a.get_text(strip=True)
                m = re.fullmatch(r"(\d{3,5})\.html", a.get("href") or "")
                if nm and m:
                    junctions.setdefault(nm, m.group(1))
            if not sname:
                continue
            km = None
            if kmtxt and re.search(r"\d", kmtxt):
                km = _num(kmtxt)
            sid = None
            if link:
                m = re.search(r"station/(\d+)\.html", link.get("href") or "")
                if m:
                    sid = m.group(1)
            # 没有里程也没有车站链接的行，是纯粹的线路接轨示意，不算车站
            if km is None and sid is None:
                continue
            stops.append({"name": sname, "km": km, "note": note, "sid": sid})

    out_parts = []
    for p in intro_parts:
        p = re.sub(r"\s+", " ", p or "").strip()
        if p:
            out_parts.append(p)
    intro = "\n\n".join(out_parts)
    total = _num(meta.get("线路长度") or "") if meta.get("线路长度") else None
    if total is None and stops:
        known = [s["km"] for s in stops if s["km"] is not None]
        if known:
            total = max(known)
    return {
        "id": lid, "url": BASE + lid + ".html", "name": meta.get("中文名称") or title,
        "title": title, "meta": meta, "intro": intro, "stops": stops,
        "junctions": [{"name": k, "id": v} for k, v in junctions.items()],
        "totalKm": total,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="忽略缓存重新下载")
    ap.add_argument("--ids", default="", help="只处理这些 id，逗号分隔")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    os.makedirs(HTML_DIR, exist_ok=True)
    items = index_items(args.force)
    if args.ids:
        want = {s.strip() for s in args.ids.split(",") if s.strip()}
        items = [it for it in items if it["id"] in want]
    print(f"索引 {len(items)} 条线路")

    def work(it):
        html = fetch(BASE + it["id"] + ".html",
                     os.path.join(HTML_DIR, it["id"] + ".html"), args.force)
        if not html:
            return None
        try:
            rec = parse(it["id"], html)
        except Exception as e:                                  # noqa: BLE001
            print("  ! 解析失败", it["id"], it["name"], e, file=sys.stderr)
            return None
        if not rec.get("name"):
            rec["name"] = it["name"]
        rec["indexName"] = it["name"]
        return rec

    out: List[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        for k, rec in enumerate(ex.map(work, items), 1):
            if rec:
                out.append(rec)
            if k % 100 == 0:
                print(f"  {k}/{len(items)}")

    out.sort(key=lambda r: int(r["id"]))
    path = os.path.join(OUT_DIR, "lines.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    n_stop = sum(len(r["stops"]) for r in out)
    n_km = sum(1 for r in out for s in r["stops"] if s["km"] is not None)
    n_intro = sum(1 for r in out if len(r["intro"]) > 30)
    print(f"解析 {len(out)} 条线路 · 站点行 {n_stop}（带里程 {n_km}）· 有介绍 {n_intro}")
    print("写入", path)


if __name__ == "__main__":
    main()
