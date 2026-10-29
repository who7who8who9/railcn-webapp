# -*- coding: utf-8 -*-
"""
抓取 china-emu.cn 的中国铁路线路（china-emu 的 RailRoads 栏目），解析成结构化 JSON。

数据来源：https://www.china-emu.cn/RailRoads/
四个目录页给出全部线名，详情页按线名查询：
    https://www.china-emu.cn/RailRoads/Line/?LineName=<线名>

它的价值在于**逐站计价里程**——辉睿资料库只有线路总长，站点里程大面积为空，
这里每站都有到起点的计价里程，可以直接补 line_catalog_stop.km。

输出 data/chinaemu/lines.json
  [{
     "name": "京哈高速铁路京沈段",
     "cats": ["骨干线路"],
     "kind": "高速铁路线路",
     "sections": [{"name": "京承段", "stops": [...]}, ...],
     "stops": [{"seq":1,"name":"北京朝阳","km":0.0,"kind":"station","cls":"A"}, ...],
     "totalKm": 696.0, "n_stop": 25, "n_km": 20
   }, ...]

stops[].kind: station（/RailRoads/Station/ 车站）| work（/RailRoads/Construction/ 桥隧）
stops[].cls : A=起点站 B=中间站 C=线路所 Q*=无里程点，用于区分是不是客运站

页面 HTML 缓存在 data/chinaemu/html/<线名>.html，重复运行不会重复下载。

用法：
    python scripts/fetch_chinaemu.py                # 增量抓取（缺的才下）
    python scripts/fetch_chinaemu.py --force        # 全部重下
    python scripts/fetch_chinaemu.py --only 骨干线路 # 只抓某个目录
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

BASE = "https://www.china-emu.cn"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "data", "chinaemu")
HTML_DIR = os.path.join(OUT_DIR, "html")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")

# 目录页 → 分类名
CATS = [
    ("Mainlines", "骨干线路"),
    ("Area", "区域线路"),
    ("Intercity", "市域与都市圈"),
    ("Hub", "枢纽与联络线"),
]

_SESS = requests.Session()
_SESS.headers["User-Agent"] = UA


def _get(url: str, retry: int = 3) -> Optional[str]:
    """带重试的 GET，返回文本。

    该站对不存在的线名返回 **HTTP 200 的 404 跳转页**（约 5 KB，内含
    window.parent.parent.location.href='/RailRoads/404/'），所以不能只看状态码，
    还要把这种软 404 判成 None。
    """
    for k in range(retry):
        try:
            r = _SESS.get(url, timeout=45)
            if r.status_code == 200:
                r.encoding = r.apparent_encoding or "utf-8"
                if "/RailRoads/404/" in r.text:
                    return None
                return r.text
            if r.status_code == 404:
                return None
            time.sleep(1.2 * (k + 1))
        except Exception:
            time.sleep(1.2 * (k + 1))
    return None


def safe(name: str) -> str:
    """线名 → 安全文件名"""
    return re.sub(r'[\\/:*?"<>|\s]+', "_", name)[:120]


def fetch_index(force: bool = False) -> Dict[str, List[str]]:
    """四个目录页 → {线名: [分类]}"""
    if not os.path.isdir(OUT_DIR):
        os.makedirs(OUT_DIR)
    out: Dict[str, List[str]] = {}
    for slug, cat in CATS:
        path = os.path.join(OUT_DIR, "index_%s.html" % slug)
        html = None
        if os.path.exists(path) and not force:
            with open(path, encoding="utf-8") as f:
                html = f.read()
        if not html:
            html = _get("%s/RailRoads/%s/" % (BASE, slug))
            if not html:
                print("  [warn] 目录页抓取失败:", slug)
                continue
            with open(path, "w", encoding="utf-8") as f:
                f.write(html)
        names = re.findall(r'href="/RailRoads/Line/\?LineName=([^"]+)"', html)
        names = [re.sub(r"<[^>]+>", "", n).strip() for n in names]
        for n in names:
            if n:
                out.setdefault(n, [])
                if cat not in out[n]:
                    out[n].append(cat)
        print("  %-8s %4d 条（去重后累计 %d）" % (cat, len(set(names)), len(out)))
    return out


# ---------------------------------------------------------------- 详情页解析

_MILE_RE = re.compile(r"station-mileage([A-Z0-9]+)")
_NAME_RE = re.compile(r"station-name([A-Z0-9]+)")
_TYPE_RE = re.compile(r"station-type([A-Z0-9]+)")


def _num(s: str) -> Optional[float]:
    m = re.search(r"-?\d+(?:\.\d+)?", (s or "").replace(",", ""))
    return round(float(m.group(0)), 1) if m else None


def parse_page(html: str) -> dict:
    """解析一个线路详情页"""
    soup = BeautifulSoup(html, "html.parser")

    # 线名与类型：<div class="text-muted font-mid">高速铁路线路</div> + <h2>京哈高速铁路京沈段</h2>
    kind = ""
    name = ""
    head = soup.find("div", class_="position-r")
    if head:
        t = head.find("div", class_="text-muted")
        kind = t.get_text(strip=True) if t else ""
        h = head.find("h2")
        name = h.get_text(strip=True) if h else ""

    sections: List[dict] = []
    # 注意：站点块的 class 有时是 lines-box，有时带后缀 lines-box-100，
    # 站点行本身也可能挂别的 class，所以这里一律用前缀匹配。
    for box in soup.find_all("div", class_=re.compile(r"^d-flex\b")):
        # 段名在 section-name 里的 marquee
        sec_name = ""
        sn = box.find("div", class_=re.compile(r"^section-name\b"))
        if sn:
            mq = sn.find("div", class_="marquee")
            sec_name = mq.get_text(strip=True) if mq else ""
        body = box.find("div", class_=re.compile(r"^lines-box"))
        if not body:
            continue

        stops = []
        # recursive=False 会漏：有些页面的站行外面多包了一层 div
        # （池黄高速铁路、黎湛铁路就是这样），直接子节点里找不到 keep-all。
        for row in body.find_all("div", class_=re.compile(r"\bkeep-all\b")):
            # 站名
            nm_div = row.find("div", class_=_NAME_RE)
            if not nm_div:
                continue
            sname = nm_div.get_text(strip=True)
            if not sname:
                continue
            cls = (_NAME_RE.search(" ".join(nm_div.get("class") or [])) or [None, ""])[1]

            # 链接类型：车站 / 桥隧 / 其它
            a = row.find("a", href=True)
            href = a["href"] if a else ""
            if "/RailRoads/Construction/" in href:
                st_kind = "work"
            elif "/RailRoads/Station/" in href:
                st_kind = "station"
            else:
                st_kind = "other"

            # 里程：**桥隧行不能取**——那些行里写的是桥隧自身的长度（米），
            # 丹昆特大桥 163763 并不是里程。只留车站/线路所行。
            km = None
            if st_kind != "work":
                km_div = row.find("div", class_=_MILE_RE)
                km = _num(km_div.get_text(" ", strip=True)) if km_div else None

            # 图形标记（ABZg+l / CONTfq 之类，用来看是不是分叉点）
            types = [m for m in _TYPE_RE.findall(" ".join(
                c for d in row.find_all("div") for c in (d.get("class") or [])))]

            stops.append({"name": sname, "km": km, "kind": st_kind,
                          "cls": cls, "href": href, "marks": types})

        if stops:
            sections.append({"name": sec_name, "stops": stops})

    # 汇总：把所有段的站按出现顺序拼起来
    flat = []
    for sec in sections:
        for s in sec["stops"]:
            flat.append(dict(s, section=sec["name"]))
    for i, s in enumerate(flat, 1):
        s["seq"] = i

    # 里程语义：这是**K 里程（全线累计）**，不是段内里程。
    # 陇海铁路西宝段是 K1073 → K1256，所以本段长度 = 1256-1073 = 183 km，
    # 用末站值会算成 1256 km（那是整条陇海线到宝鸡的里程）。
    kms = [s["km"] for s in flat if s["km"] is not None]
    kmin = min(kms) if kms else None
    kmax = max(kms) if kms else None
    total = round(kmax - kmin, 1) if (kmin is not None and kmax is not None) else None

    # 里程单调性自检：逆序次数。非 0 说明页面里混了可疑数字，标出来别当真
    desc = sum(1 for a1, b1 in zip(kms, kms[1:]) if b1 < a1 - 1)
    return {
        "name": name,
        "kind": kind,
        "sections": sections,
        "stops": flat,
        "kmMin": kmin,
        "kmMax": kmax,
        "totalKm": total,
        "kmDesc": desc,
        "n_stop": len(flat),
        "n_km": len(kms),
        "n_station": sum(1 for s in flat if s["kind"] == "station"),
    }


def parse_file(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return parse_page(f.read())


def _looks_empty(html: str) -> bool:
    """空壳页判定。

    该站偶尔会返回一个 5 KB 的「只有标题、没有站点列表」的页面（不是 404，
    状态码 200、也没有 /RailRoads/404/ 标记），同一条线路重抓一次就正常了。
    池黄高速铁路、黎湛铁路、南汇线等 17 条第一次抓都踩到了。
    """
    return "lines-box" not in html or "station-name" not in html


def fetch_detail(name: str, force: bool = False) -> Optional[dict]:
    path = os.path.join(HTML_DIR, safe(name) + ".html")
    if os.path.exists(path) and not force:
        try:
            with open(path, encoding="utf-8") as f:
                cached = f.read()
            if "/RailRoads/404/" not in cached and not _looks_empty(cached):
                return parse_page(cached)
        except Exception:
            pass
    # 最多试 3 次：空壳页要重抓，不然会留下「有这条线但 0 站」的假结果
    for attempt in range(3):
        html = _get("%s/RailRoads/Line/?LineName=%s" % (BASE, requests.utils.quote(name)))
        if not html:
            return None
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        if _looks_empty(html):
            time.sleep(0.6 * (attempt + 1))
            continue
        time.sleep(0.08)
        try:
            return parse_page(html)
        except Exception as e:
            print("  [warn] 解析失败 %s: %s" % (name, e))
            return None
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="忽略缓存重新下载")
    ap.add_argument("--only", default="", help="只抓某个分类，如 骨干线路")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--reparse", action="store_true",
                    help="不联网，只用 data/chinaemu/html 缓存重新解析（改了 parser 后用）")
    args = ap.parse_args()

    if not os.path.isdir(HTML_DIR):
        os.makedirs(HTML_DIR)

    if args.reparse:
        print("一、读目录页")
        idx = fetch_index(False)
        print("二、重新解析缓存")
        recs = []
        for fn in sorted(os.listdir(HTML_DIR)):
            if not fn.endswith(".html"):
                continue
            nm = fn[:-5].replace("_", " ")
            try:
                r = parse_file(os.path.join(HTML_DIR, fn))
            except Exception as e:
                print("  [warn] %s: %s" % (fn, e))
                continue
            if not r.get("n_stop"):
                continue
            # 缓存文件名被 safe() 改写过，用页面里的标题反查原线名
            r["cats"] = idx.get(r["name"]) or idx.get(nm) or []
            r["ask"] = r["name"] if r["name"] in idx else nm
            recs.append(r)
        return _write(recs)

    print("一、抓目录页")
    idx = fetch_index(args.force)
    todo = [n for n, cs in idx.items() if not args.only or args.only in cs]
    print("   待抓详情页 %d 条" % len(todo))

    print("二、抓详情页")
    recs: List[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(fetch_detail, n, args.force): n for n in todo}
        for i, (fu, n) in enumerate(futs.items(), 1):
            try:
                r = fu.result()
            except Exception as e:
                print("  [warn] %s: %s" % (n, e))
                r = None
            if r and r.get("n_stop"):
                r["cats"] = idx.get(n, [])
                r["name"] = r.get("name") or n
                r["ask"] = n
                recs.append(r)
                done += 1
            elif r is not None:
                print("  [空站序] %s" % n)
            if i % 50 == 0:
                print("    %d/%d …" % (i, len(todo)))

    recs.sort(key=lambda r: (-(r.get("totalKm") or 0), r["name"]))
    return _write(recs)


def _write(recs: List[dict]) -> None:
    out = os.path.join(OUT_DIR, "lines.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(recs, f, ensure_ascii=False, indent=1)

    n_st = sum(r["n_station"] for r in recs)
    n_km = sum(r["n_km"] for r in recs)
    n_all = sum(r["n_stop"] for r in recs)
    n_bad = sum(1 for r in recs if r.get("kmDesc"))
    print("\n结果")
    print("   线路 %d 条，站行 %d 行（车站 %d 行，有里程 %d 行）" % (len(recs), n_all, n_st, n_km))
    print("   里程覆盖 %d/%d = %.1f%%" % (n_km, n_all, 100.0 * n_km / max(1, n_all)))
    print("   里程非单调（页面混了可疑数字）%d 条，已用 kmDesc 标出" % n_bad)
    print("   输出", out)


if __name__ == "__main__":
    main()
