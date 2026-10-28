# -*- coding: utf-8 -*-
"""回归：校对页「同类车次沿用经由」——保存一趟车的经由后，同起讫站同车种的其它车次自动套用。

跑法（服务需已启动在 8686）：
    python scripts/test_via_class.py D7603 D7607
测试完会把来源车次的校正撤销，库回到原样。
"""
import json
import os
import sys
import tempfile
import urllib.request

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8686"
SHOT = os.path.join(tempfile.gettempdir(), "railcn-viaclass")


def get(path):
    return json.load(urllib.request.urlopen(BASE + path, timeout=120))


def post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=180))


def main():
    src = (sys.argv[1] if len(sys.argv) > 1 else "D7603").upper()
    sib = (sys.argv[2] if len(sys.argv) > 2 else "D7607").upper()
    os.makedirs(SHOT, exist_ok=True)

    d = get(f"/api/curate/detail?code={src}")
    print(f"{src}: {d['from']} → {d['to']} · 同类 {d['classCount']} 趟")
    via = [{"line": d["auto"]["viaSegs"][-1]["line"] if d["auto"]["viaSegs"] else "沈丹客运专线",
            "from": d["stations"][0], "to": d["stations"][-1]}]

    with sync_playwright() as p:
        b = p.chromium.launch(args=["--no-proxy-server"])
        pg = b.new_context(viewport={"width": 1440, "height": 950}).new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(BASE + "/", wait_until="load")
        pg.wait_for_timeout(1500)

        # 打开第 1 趟车的编辑器，确认「同类 N 趟一并沿用」复选框
        pg.click('[data-tab="curate"]')
        pg.wait_for_timeout(1200)
        pg.click('#cuFilters .cu-filter[data-f="all"]')      # 默认只看「待补全」，要切到全部
        pg.wait_for_timeout(600)
        pg.fill("#cuQ", src)
        pg.press("#cuQ", "Enter")                              # 搜索框要回车才查
        pg.wait_for_selector(f'#cuList .cu-row[data-code="{src}"]', timeout=30000)
        pg.click(f'#cuList .cu-row[data-code="{src}"]')
        pg.wait_for_selector("#ceViaRows", timeout=20000)
        pg.wait_for_timeout(800)
        prop = pg.query_selector("#ceProp")
        print("  复选框:", (pg.inner_text(".ce-prop") if prop else "没有"))
        pg.screenshot(path=os.path.join(SHOT, "1-editor.png"))

        # 直接走接口保存（编辑器里的线路下拉点起来太脆，这里只验后端链路 + 面板显示）
        r = post("/api/override", {"code": src, "date": d["date"], "via": via, "propagate": True})
        cls = r.get("viaClass") or {}
        print(f"  保存 → 同类副本 {cls.get('ok')} · 刷新 {r.get('classRefreshed')} 趟")

        # 兄弟车次：面板应显示「沿用同类 · 学自 XXX」，经由行为「同类」
        pg.fill("#cuQ", sib)
        pg.press("#cuQ", "Enter")
        pg.wait_for_selector(f'#cuList .cu-row[data-code="{sib}"]', timeout=30000)
        pg.click(f'#cuList .cu-row[data-code="{sib}"]')
        pg.wait_for_selector("#ceViaRows", timeout=20000)
        pg.wait_for_timeout(800)
        badge = pg.query_selector(".ce-badge.class")
        tags = [e.inner_text() for e in pg.query_selector_all("#ceViaRows .ve-src")]
        print(f"  {sib} 头部代码: {pg.inner_text('.ce-code')}")
        print(f"  {sib} 头部标记: {badge.inner_text() if badge else '无'}")
        print(f"  {sib} 经由行标签: {tags}")
        pg.screenshot(path=os.path.join(SHOT, "2-sibling.png"))

        # 车次详情页的经由后面应带「同类沿用」
        pg.click('[data-tab="train"]')
        pg.fill("#q", sib)
        with pg.expect_response(lambda r: "/api/route" in r.url and "code=" + sib in r.url, timeout=60000):
            pg.click("#btnSearch")
        pg.wait_for_timeout(1500)
        tag = pg.query_selector("#detail .tag.cls")
        print("  详情页标记:", tag.inner_text() if tag else "无")
        pg.screenshot(path=os.path.join(SHOT, "3-detail.png"))

        print("页面错误:", errs or "无")
        b.close()

    # 收尾：撤销来源车次的校正，同类副本随之删除
    post("/api/override", {"code": src, "via": [], "totalKm": None, "note": ""})
    left = get(f"/api/route?code={sib}&date={d['date']}").get("viaSource")
    print(f"  撤销后 {sib} 回到: {via_source_cn(left)}")


def via_source_cn(v):
    return {"manual": "人工校正", "class": "同类沿用", "auto": "自动推断", "none": "未识别"}.get(v, v)


if __name__ == "__main__":
    main()
