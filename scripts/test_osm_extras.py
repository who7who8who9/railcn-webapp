# -*- coding: utf-8 -*-
"""
test_osm_extras.py — 线路查询里「OSM 有、线路库没收录」的线能不能看见

要防的是这类：线路库里只有「津霸客运专线」（客专，73 km），OSM 里还有一条
「津霸线」（普速既有线，83 km）。以前搜「津霸」时库里的那条直接命中，页面就停在
它身上，另一条永远看不到 —— 只能靠「查不到才会去 OSM 找」那条兜底路径，
而它压根不会走到。

现在两处都要能看见：
  * 联想下拉：库里命中之后，再接上 OSM 未收录的
  * 线路详情：头部下方列出「OSM 里另有 N 条相关线」，点开看走向

用法：python scripts/test_osm_extras.py [关键字] [--keep]
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = os.environ.get("RAILCN_BASE", "http://127.0.0.1:8686")
SHOT = os.path.join(tempfile.gettempdir(), "railcn-osmextra")


def get(path: str):
    return json.load(urllib.request.urlopen(BASE + path, timeout=60))


def q(s: str) -> str:
    return urllib.parse.quote(s)


def check_api(kw: str, lib_name: str, osm_name: str) -> None:
    d = get("/api/lines?q=" + q(kw))
    lib = [x["name"] for x in d.get("items") or []]
    osm = [(x["name"], x["km"]) for x in d.get("osm") or []]
    print(f"  联想「{kw}」→ 库 {lib} / OSM {osm}")
    assert lib_name in lib, f"线路库里没有「{lib_name}」"
    assert any(n == osm_name for n, _ in osm), f"OSM 未收录列表里没有「{osm_name}」"

    det = get("/api/line?q=" + q(lib_name))
    other = det.get("osmOther") or []
    print(f"  详情「{lib_name}」→ osmOther {[(x['name'], x['km'], x['libSame']) for x in other]}")
    hit = next((x for x in other if x["name"] == osm_name), None)
    assert hit, f"详情里没有提示「{osm_name}」"
    assert hit["libSame"], "没说明它和库里哪条线同名不同口径"
    assert lib_name in hit["libSame"], f"libSame 应包含「{lib_name}」"


def check_negative() -> None:
    """已经收录的线不该出现在差集里（京沪线是库里的，别把它的残段当新线）"""
    for kw, nm in (("京沪线", "京沪线"), ("津蓟线", "津蓟线")):
        d = get("/api/lines?q=" + q(kw))
        names = [x["name"] for x in d.get("osm") or []]
        assert nm not in names, f"{nm} 已在线路库里，却算进了未收录"
    print("  已收录的线不在差集里")


def check_ui(kw: str, lib_name: str, osm_name: str) -> None:
    from playwright.sync_api import sync_playwright
    os.makedirs(SHOT, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--no-proxy-server"])
        pg = b.new_context(viewport={"width": 1500, "height": 980}).new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(BASE + "/", wait_until="load")
        pg.wait_for_timeout(1200)
        pg.click('[data-tab="net"]')
        pg.wait_for_timeout(400)
        pg.click('.chipsel[data-kind="line"]')     # 默认是车站查询，先切到线路
        pg.wait_for_timeout(300)
        pg.click("#netQ")
        pg.type("#netQ", kw, delay=90)
        pg.wait_for_timeout(1200)
        rows = [x.inner_text().replace("\n", " ") for x in pg.query_selector_all("#sugNet .sug-item")]
        print(f"  下拉：{rows}")
        assert any(r.startswith(lib_name) for r in rows), "下拉里没有库内那条"
        assert any(r.startswith(osm_name) for r in rows), "下拉里没有 OSM 那条"
        assert any("OSM" in r for r in rows), "OSM 那条没标出来"
        pg.screenshot(path=os.path.join(SHOT, "suggest.png"))

        pg.query_selector_all("#sugNet .sug-item")[0].click()
        pg.wait_for_timeout(2200)
        blk = pg.query_selector("#lnOsmOther")
        assert blk, "详情里没有 OSM 提示块"
        assert blk.get_attribute("hidden") is None, "详情里的 OSM 提示块被藏起来了"
        txt = blk.inner_text().replace("\n", " | ")
        print(f"  详情提示块：{txt[:120]}")
        assert osm_name in txt, "提示块里没列这条线"
        pg.screenshot(path=os.path.join(SHOT, "detail.png"))

        # 点进去要能打开 OSM 走向
        pg.query_selector("#lnOsmOther .lo-item").click()
        pg.wait_for_timeout(2000)
        head = pg.inner_text("#netDetail .dhead .l1").replace("\n", " ")
        print(f"  点开后：{head}")
        assert osm_name in head, "没打开对应的 OSM 线路"
        assert errs == [], f"页面报错：{errs}"
        print("  页面无 JS 报错")
        b.close()


def find_pair():
    """自动找一组「库里一条线 + OSM 里同核心名的另一条」当测试对象

    原来写死的是 津霸客运专线 / 津霸线，后来 津霸线 按 OSM 走向补进了线路库，
    这组就不再是「OSM 未收录」，写死的用例会误报。改成按当前库现找。
    """
    for kw in ("津霸", "京哈", "京广", "陇海", "胶济", "沪宁", "京沪", "津山"):
        d = get("/api/lines?q=" + q(kw))
        lib = [x["name"] for x in d.get("items") or []]
        if not lib:
            continue
        for ln in lib[:3]:
            try:
                det = get("/api/line?q=" + q(ln))
            except Exception:                     # noqa: BLE001
                continue
            for o in det.get("osmOther") or []:
                if o.get("libSame") and any(s in lib for s in o["libSame"]):
                    return kw, ln, o["name"]
    return None


def main() -> None:
    kw = "津霸"
    lib_name, osm_name = "津霸客运专线", "津霸线"
    args = [x for x in sys.argv[1:] if not x.startswith("--")]
    if args:
        if len(args) < 3:
            print("用法：test_osm_extras.py [关键字 库内线名 OSM线名]")
            return
        kw, lib_name, osm_name = args[:3]
    else:
        pair = find_pair()
        if not pair:
            print("跳过：当前线路库里找不到「库内一条、OSM 另一条同核心名」的线对")
            return
        kw, lib_name, osm_name = pair
        print(f"  自动选线：{lib_name}（库）/ {osm_name}（OSM 未收录）")
    print("== 接口")
    check_api(kw, lib_name, osm_name)
    check_negative()
    print("== 页面")
    check_ui(kw, lib_name, osm_name)
    print("全部通过（截图在 %s）" % SHOT)


if __name__ == "__main__":
    main()
