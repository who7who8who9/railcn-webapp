"""回归：线路编辑能在两站之间插一站（自建线路 / 档案线路都要能插）

以前只有「＋ 加一站」往末尾追加，中间补一站得把后面的行删掉重敲。
现在每行行首有「＋」，点它就在这一行前面插一空行；插进去那行的里程格会提示
「在 12.3–48.0 之间」，写完保存：

  · 自建线路 → 走 /api/line/new（整份站表替换）
  · 档案线路 → 走 /api/line/stations（人工站序覆盖，原始档案不动，可「恢复档案站序」）

用法：python scripts/test_nm_insert.py [线路名]
"""
import json
import os
import sys
import tempfile
import urllib.parse
import urllib.request

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8686"
SHOT = os.path.join(tempfile.gettempdir(), "railcn-nmins")

#: 只有 OSM 坐标、没有车站档案的测试站（淮南矿区支线站），落库后可以删干净
POOL = ("蔡家岗", "望峰岗", "毕家岗", "田家庵", "潘一矿", "田集", "戴楼")


def get(path):
    return json.load(urllib.request.urlopen(BASE + path, timeout=60))


def post(path, body):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    return json.load(urllib.request.urlopen(req, timeout=90))


def pick(n):
    """挑 n 个还没挂在线路上的测试站（挂过的别动）"""
    used = {s["name"] for s in get("/api/custom")["stations"]}
    return [x for x in POOL if x not in used][:n]


def cleanup(name, stations):
    post("/api/line/delete", {"name": name})
    for s in stations:
        post("/api/station/delete", {"name": s})


def page(browser):
    pg = browser.new_context(viewport={"width": 1440, "height": 900}).new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(BASE + "/", wait_until="load")
    pg.wait_for_timeout(1200)
    return pg, errs


def open_line(pg, name):
    pg.click('[data-tab="net"]')
    pg.wait_for_timeout(300)
    pg.click('.net-toggle .chipsel[data-kind="line"]')
    pg.wait_for_timeout(200)
    pg.fill("#netQ", name)
    with pg.expect_response(lambda r: "/api/line?" in r.url, timeout=30000):
        pg.press("#netQ", "Enter")
    pg.wait_for_selector("#netDetail.show", timeout=20000)
    pg.wait_for_timeout(800)


# ---------------------------------------------------------------- 自建线路
def check_new(name, a, b, c):
    """新增线路弹窗里插中间站，保存后站序 / 里程按行序落库"""
    os.makedirs(SHOT, exist_ok=True)
    with sync_playwright() as p:
        br = p.chromium.launch(args=["--no-proxy-server"])
        pg, errs = page(br)
        pg.click('[data-tab="net"]')
        pg.wait_for_timeout(300)
        pg.click("#btnNewLine")
        pg.wait_for_selector("#newModal:not(.hidden)", timeout=10000)
        pg.wait_for_function(
            "() => typeof NM !== 'undefined' && NM.stations && NM.stations.length > 0",
            timeout=30000)

        print(f"  打开时 {len(pg.query_selector_all('#nmRows .nm-row'))} 行空站表")
        pg.fill("#nmName", name)
        for i, km in ((0, "0"), (1, "100")):
            inp = pg.query_selector_all("#nmRows .nm-row")[i].query_selector("input.line")
            inp.fill("")
            inp.type((a, b)[i], delay=20)
            pg.query_selector_all("#nmRows .nm-row")[i].query_selector("input.km").fill(km)
        pg.wait_for_timeout(300)
        pg.evaluate("() => cbClose()")

        # 在第 2 行（b 站）前面插一站
        pg.query_selector_all("#nmRows .nm-row")[1].query_selector(".ins").click()
        pg.wait_for_timeout(250)
        names = [el.input_value() for el in pg.query_selector_all("#nmRows .nm-row input.line")]
        print(f"  插一行后站表：{names}")
        assert names == [a, "", b], f"没插在中间：{names}"
        newrow = pg.query_selector_all("#nmRows .nm-row")[1]
        ph = newrow.query_selector("input.km").get_attribute("placeholder")
        print(f"  新行里程提示：{ph}")
        assert ph == "在 0–100 之间", f"里程区间提示不对：{ph}"
        assert pg.evaluate(
            "() => document.activeElement === document.querySelectorAll('#nmRows .nm-row input.line')[1]"
        ), "插入后没聚焦到新行"
        idx = [el.inner_text().strip() for el in pg.query_selector_all("#nmRows .nm-row .i")]
        print(f"  行号：{idx}")
        assert idx == ["1", "2", "3"], f"行号没重排：{idx}"

        # 首站前面也能插
        pg.query_selector_all("#nmRows .nm-row")[0].query_selector(".ins").click()
        pg.wait_for_timeout(200)
        head = [el.input_value() for el in pg.query_selector_all("#nmRows .nm-row input.line")]
        print(f"  首行前面插入：{head}")
        assert head == ["", a, "", b], f"没插到最前面：{head}"
        pg.query_selector_all("#nmRows .nm-row")[0].query_selector(".del").click()
        pg.wait_for_timeout(200)

        newrow = pg.query_selector_all("#nmRows .nm-row")[1]
        inp = newrow.query_selector("input.line")
        inp.fill("")
        inp.type(c, delay=20)
        newrow.query_selector("input.km").fill("50")
        pg.screenshot(path=os.path.join(SHOT, "insert.png"))
        with pg.expect_response(lambda r: "/api/line/new" in r.url, timeout=30000):
            pg.click("#nmSave")
        pg.wait_for_timeout(600)

        d = get("/api/line?q=" + urllib.parse.quote(name))
        stops = [(s["name"], s["km"]) for s in d["stations"]]
        print(f"  落库站序：{stops}")
        assert [s[0] for s in stops] == [a, c, b], f"站序不对：{stops}"
        assert [s[1] for s in stops] == [0, 50, 100], f"里程不对：{stops}"
        print("  页面错误:", errs or "无")
        assert not errs, "页面有 JS 报错"
        br.close()


# ---------------------------------------------------------------- 档案线路
def check_lib(name, mid):
    """档案线路：改站序按钮 → 插中间站 → 保存成人工站序 → 恢复档案站序"""
    os.makedirs(SHOT, exist_ok=True)
    origin = get("/api/line?q=" + urllib.parse.quote(name))
    assert not origin.get("stopsManual"), f"「{name}」本来就有站序覆盖，测不了（先恢复档案站序）"
    names0 = [s["name"] for s in origin["stations"]]
    kms0 = [s["km"] for s in origin["stations"]]
    assert len(names0) >= 3, f"「{name}」只有 {len(names0)} 站，挑条长点的线"
    print(f"  档案站序：{list(zip(names0, kms0))}")

    with sync_playwright() as p:
        br = p.chromium.launch(args=["--no-proxy-server"])
        pg, errs = page(br)
        open_line(pg, name)
        btn = pg.query_selector("#lnStopsBtn")
        assert btn and btn.inner_text().strip() == "改站序", "档案线路没有「改站序」入口"
        btn.click()
        pg.wait_for_selector("#newModal:not(.hidden)", timeout=10000)
        pg.wait_for_function(
            "() => typeof NM !== 'undefined' && NM.stations && NM.stations.length > 0",
            timeout=30000)
        got = [el.input_value() for el in pg.query_selector_all("#nmRows .nm-row input.line")]
        print(f"  弹窗预填：{got}")
        assert got == names0, f"预填的不是现有站序：{got}"

        # 在第 2 站前面插
        pg.query_selector_all("#nmRows .nm-row")[1].query_selector(".ins").click()
        pg.wait_for_timeout(250)
        rows = pg.query_selector_all("#nmRows .nm-row")
        assert len(rows) == len(names0) + 1, "没插出一行"
        assert rows[1].query_selector("input.line").input_value() == "", "插进来的行不空"
        km_new = round((kms0[0] + kms0[1]) / 2, 1)
        rows[1].query_selector("input.line").fill("")
        rows[1].query_selector("input.line").type(mid, delay=20)
        rows[1].query_selector("input.km").fill(str(km_new))
        pg.evaluate("() => cbClose()")
        pg.screenshot(path=os.path.join(SHOT, "lib-insert.png"))

        with pg.expect_response(lambda r: "/api/line/stations" in r.url, timeout=30000):
            pg.click("#nmSave")
        pg.wait_for_timeout(1000)

        d = get("/api/line?q=" + urllib.parse.quote(name))
        stops = [(s["name"], s["km"]) for s in d["stations"]]
        print(f"  覆盖后站序：{stops}")
        assert d.get("stopsManual"), "档案线路没标成「站序人工」"
        assert [s[0] for s in stops] == [names0[0], mid] + names0[1:], f"站序不对：{stops}"
        assert stops[1][1] == km_new, f"插入站里程不对：{stops[1]}"
        # 详情页要能看到「站序人工」标记和恢复按钮
        pg.wait_for_selector("#lnStopsReset", timeout=20000)
        print("  详情页标记：", pg.inner_text("#lnHead .l1").replace("\n", " ")[:90])

        # 恢复档案站序：点两下（第二下确认）
        pg.click("#lnStopsReset")
        pg.wait_for_timeout(200)
        with pg.expect_response(lambda r: "/api/line/stations/clear" in r.url, timeout=30000):
            pg.click("#lnStopsReset")
        pg.wait_for_timeout(1200)
        d = get("/api/line?q=" + urllib.parse.quote(name))
        back = [(s["name"], s["km"]) for s in d["stations"]]
        print(f"  恢复后站序：{back}")
        assert not d.get("stopsManual"), "恢复后还标着站序人工"
        assert [s[0] for s in back] == names0, f"没恢复成档案站序：{back}"
        print("  页面错误:", errs or "无")
        assert not errs, "页面有 JS 报错"
        br.close()


# ---------------------------------------------------------------- 自建线路改站表
def check_edit_custom(name, stops):
    with sync_playwright() as p:
        br = p.chromium.launch(args=["--no-proxy-server"])
        pg, _errs = page(br)
        open_line(pg, name)
        pg.click("#lnStopsBtn")
        pg.wait_for_selector("#newModal:not(.hidden)", timeout=10000)
        pg.wait_for_function(
            "() => typeof NM !== 'undefined' && NM.stations && NM.stations.length > 0",
            timeout=30000)
        n = len(pg.query_selector_all("#nmRows .nm-row"))
        print(f"  自建线路改站表：{n} 行（现有 {len(stops)} 站）")
        assert n == len(stops), f"改站表行数不对：{n} vs {len(stops)}"
        pg.click("#nmCancel")
        br.close()


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "回归测试插站线"
    picks = pick(3)
    if len(picks) < 3:
        print("跳过自建线路部分：测试站都被别的线路占用了（" + "、".join(POOL) + "）")
        picks = []
    else:
        a, b, c = picks
        print(f"== 自建线路：{a} / 插入 {c} / {b}")
        cleanup(name, [])
        try:
            check_new(name, a, b, c)
            check_edit_custom(name, [a, c, b])
        finally:
            cleanup(name, picks)
            left = [s["name"] for s in get("/api/custom")["stations"] if s["name"] in picks]
            print(f"  清理完成，残留 {left or '无'}")

    print("== 档案线路：插中间站再恢复")
    try:
        check_lib("津霸客运专线", "安次")
    finally:
        post("/api/line/stations/clear", {"name": "津霸客运专线"})
    print("全部通过")


if __name__ == "__main__":
    main()
