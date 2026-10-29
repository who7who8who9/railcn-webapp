# -*- coding: utf-8 -*-
"""
线路资料库 ↔ 线路库 的匹配不能串线。

原来的 match_lines() 按站序打分时，分母取了 min(资料库站数, 库内线站数)，
于是库内那条小联络线只要几站都落在资料库站序里，就必然拿 1.0：

    侯西线（42 站 / 288.5 km）→ 陇海西成联络线（2 站 / 9 km）  score=1.0

结果搜「侯西线」出来的是 9 公里的联络线，站点只有三桥、西安西。

这个测试守四件事：
  1. stops_score 对「小线冒充大线」返回 0（站数和里程同时不到一半）
  2. 分段关系不能误杀：资料库那条线只是整条干线的一段（京石高铁 / 京广高速线）
  3. 库里没有残留的荒谬匹配（资料库 10 站以上，却挂在 1/3 站数以下的库内线上）
  4. 接口上搜「侯西线」拿到的是侯西线：42 站、288.5 km、OSM 走向 237.7 km
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "webapp"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import cnrail                                                    # noqa: E402
import railcore                                                  # noqa: E402
from refix_catalog_match import stops_score                      # noqa: E402

BASE = "http://127.0.0.1:8686"
OK = "\u2713"


def api(path: str, **q) -> dict:
    url = BASE + path
    if q:
        url += "?" + urllib.parse.urlencode(q)
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def keys(names):
    return {cnrail._key(n) for n in names if n}


def check_small_line_parasite() -> None:
    """小联络线不能冒充整条干线"""
    # 侯西线 42 站 288.5 km vs 陇海西成联络线 2 站 9 km
    cat = keys(["侯马北", "侯马", "西安西", "西安"] + [f"站{i}" for i in range(38)])
    lib = keys(["三桥", "西安西"])
    assert stops_score(cat, lib, 288.5, 9.0) == 0.0, "2 站的联络线不该匹配 42 站的侯西线"
    # 昆玉河线 25 站 369 km vs 河口北联络线 2 站 5 km
    cat2 = keys([f"k{i}" for i in range(25)])
    assert stops_score(cat2, keys(["k0", "k1"]), 369.0, 5.0) == 0.0, "河口北联络线不该匹配昆玉河线"
    print("  %s 小线冒充大线被判死（侯西线 → 陇海西成联络线、昆玉河线 → 河口北联络线）" % OK)


def check_segment_kept() -> None:
    """资料库那条线只是整条干线的一段，必须留着"""
    # 京石高铁 7 站 283.7 km 是京广高速线 42 站 2291 km 的一段
    cat = keys(["北京西", "涿州东", "保定东", "石家庄", "高碑店东", "定州东", "正定机场"])
    lib = cat | keys([f"x{i}" for i in range(35)])
    v = stops_score(cat, lib, 283.7, 2291.0)
    assert v >= 0.99, f"京石高铁 → 京广高速线 应该保留，实际 {v}"
    # 宁西线 104 站 1030.2 km vs 宁西线西合段 52 站 953 km：站数过半、里程相近
    cat3 = keys([f"n{i}" for i in range(104)])
    lib3 = keys([f"n{i}" for i in range(52)])
    assert stops_score(cat3, lib3, 1030.2, 953.0) > 0, "宁西线 → 宁西线西合段 是分段关系，不该判死"
    print("  %s 分段关系没被误杀（京石高铁 → 京广高速线 100%%、宁西线 → 西合段 50%%）" % OK)


def check_no_residual() -> None:
    """库里不能再有荒谬匹配（只查 stops 这一档）

    exact / family 是按线名认的，不归这里管：「粤海铁路」这条资料的
    title 就叫「粤海轮渡线」，22 站覆盖整个粤海铁路，挂轮渡线是对的。
    """
    lib = {it["name"]: it for it in cnrail.search_line("", 5000)}
    conn = sqlite3.connect(railcore.DB_PATH)
    conn.row_factory = sqlite3.Row
    bad = []
    for r in conn.execute("""SELECT name, line_name, n_stop
                             FROM line_catalog
                             WHERE match_how = 'stops'
                               AND line_name IS NOT NULL AND line_name <> ''"""):
        tgt = lib.get(r["line_name"])
        if not tgt:
            continue
        cat_n, lib_n = r["n_stop"] or 0, tgt.get("n") or 0
        if cat_n >= 10 and lib_n * 3 < cat_n:
            bad.append("%s → %s（%d 站 vs %d 站）" % (r["name"], r["line_name"], cat_n, lib_n))
    assert not bad, "还有荒谬匹配：\n    " + "\n    ".join(bad[:10])
    print("  %s 库里没有「小线冒充大线」的匹配残留" % OK)


def check_ui() -> None:
    """搜侯西线，出来得是侯西线"""
    d = api("/api/line", q="侯西线")
    assert d.get("name") == "侯西线", f"搜侯西线出来的是「{d.get('name')}」"
    assert (d.get("n") or 0) == 42, f"侯西线应是 42 站，实际 {d.get('n')}"
    assert abs((d.get("totalKm") or 0) - 288.5) < 1, f"侯西线应是 288.5 km，实际 {d.get('totalKm')}"
    st = [s["name"] for s in d.get("stations") or []]
    assert st[0] == "侯马北" and st[-1] == "西安", f"首末站不对：{st[0]} → {st[-1]}"
    assert "阎良" in st, "侯西线站序里应该有阎良"
    osm = d.get("osm") or {}
    assert osm.get("name") == "侯西线" and abs((osm.get("km") or 0) - 237.7) < 1, \
        f"OSM 走向不对：{osm.get('name')} {osm.get('km')}"
    info = d.get("info") or {}
    assert info.get("title") == "侯西铁路", f"资料卡丢了：{info.get('title')}"
    print("  %s 搜侯西线 → 侯西线 42 站 288.5 km，OSM 237.7 km，资料卡在" % OK)

    # 顺手查一下原来同样挂错的几条
    for nm, n_min in (("宁西线", 100), ("同蒲线", 100), ("沪杭线", 25)):
        e = api("/api/line", q=nm)
        assert e.get("name") == nm, f"搜{nm}出来的是「{e.get('name')}」"
        assert (e.get("n") or 0) >= n_min, f"{nm} 站数偏少：{e.get('n')} < {n_min}"
    print("  %s 宁西线 104 站 / 同蒲线 101 站 / 沪杭线 29 站，不再是小联络线" % OK)


def main() -> None:
    print("资料库 ↔ 线路库 匹配")
    check_small_line_parasite()
    check_segment_kept()
    check_no_residual()
    check_ui()
    print("OK：资料库不会再挂到 9 公里的联络线上，分段关系也没被误杀")


if __name__ == "__main__":
    main()
