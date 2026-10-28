"""回归：「新增线路」弹窗里的站名下拉，要和车站搜索用同一套站名

唐海南 / 曹妃甸港 / 南堡北 这批站只在 12306 站表里，线路档案还没收；
搜索框（/api/stations）早就并了两个来源，新增线路的站名下拉（/api/stations/names）
却只读档案那一半，于是搜得到、下拉里没有。这里两头一起盯。
"""
import json
import os
import sys
import tempfile
import urllib.parse
import urllib.request

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8686"
SHOT = os.path.join(tempfile.gettempdir(), "railcn-nm")


def get(path):
    return json.load(urllib.request.urlopen(BASE + path, timeout=60))


def check_names(loose):
    d = get("/api/stations/names")
    names = d["items"]
    print(f"  站名库 {d['count']} 条")
    for name in loose:
        assert name in names, f"{name} 不在站名库里"
        # 「XX」和「XX站」只该留一个写法
        dup = [n for n in (name, name + "站") if n in names]
        print(f"  {name:6} 在库 · 写法 {dup}")
        assert len(dup) == 1, f"{name} 在库里有两种写法"
    # 带「站」的地名不能被截坏
    assert "十八站" in names, "「十八站」被当成后缀截掉了"
    # 联想和下拉要对得上：搜得到的站，站名库里也得有
    for name in loose:
        items = get("/api/stations?q=" + urllib.parse.quote(name + "站"))["items"]
        assert items and items[0]["name"] in names, f"{name} 联想有、站名库没有"


def check_ui(loose):
    os.makedirs(SHOT, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--no-proxy-server"])
        pg = b.new_context(viewport={"width": 1440, "height": 900}).new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(BASE + "/", wait_until="load")
        pg.wait_for_timeout(1200)
        pg.click('[data-tab="net"]')
        pg.wait_for_timeout(400)
        # 站名库是异步拉的，等它到货再打字（NM 是 app.js 顶层 const，不在 window 上）
        pg.click("#btnNewLine")
        pg.wait_for_selector("#newModal:not(.hidden)", timeout=10000)
        pg.wait_for_function(
            "() => typeof NM !== 'undefined' && NM.stations && NM.stations.length > 0",
            timeout=30000)
        print(f"  站名库就绪 {pg.evaluate('() => NM.stations.length')} 条")

        row = pg.query_selector("#nmRows .nm-row")
        inp = row.query_selector("input.line")
        name = loose[0]
        inp.click()
        inp.type(name, delay=40)
        pg.wait_for_timeout(500)
        items = [el.inner_text().strip() for el in pg.query_selector_all("#nmRows .cb-pop .cb-item")]
        print(f"  输入「{name}」→ {items[:5]}")
        assert any(i.startswith(name) for i in items), f"下拉里没有 {name}"
        # 点第一条（就是它自己）
        target = next(el for el in pg.query_selector_all("#nmRows .cb-pop .cb-item")
                      if el.inner_text().strip().startswith(name))
        target.click()
        pg.wait_for_timeout(400)
        print(f"  选中后第一行站名：{pg.input_value('#nmRows .nm-row input.line')}")
        assert pg.input_value("#nmRows .nm-row input.line") == name, "选中的站名不对"

        # 带「站」的输入：不能多出一条「用这个名称」的库外条目
        row2 = pg.query_selector_all("#nmRows .nm-row")[1]
        inp2 = row2.query_selector("input.line")
        inp2.click()
        inp2.type(name + "站", delay=40)
        pg.wait_for_timeout(500)
        rows = [el.inner_text().strip() for el in row2.query_selector_all(".cb-pop .cb-item")]
        print(f"  输入「{name}站」→ {rows[:5]}")
        assert not any("用这个名称" in t for t in rows), "带「站」的输入被当成库外站名"
        assert any(t.startswith(name) for t in rows), f"带「站」搜不到 {name}"
        pg.screenshot(path=os.path.join(SHOT, f"{name}.png"))

        # 另外两个站也得能在下拉里出现
        for other in loose[1:]:
            inp2.fill("")
            inp2.type(other, delay=40)
            pg.wait_for_timeout(500)
            rows = [el.inner_text().strip() for el in row2.query_selector_all(".cb-pop .cb-item")]
            print(f"  输入「{other}」→ {rows[:4]}")
            assert any(t.startswith(other) for t in rows), f"下拉里没有 {other}"
        print("  页面错误:", errs or "无")
        assert not errs, "页面有 JS 报错"
        b.close()


def main():
    loose = sys.argv[1:] or ["唐海南", "曹妃甸港", "南堡北"]
    print("== 站名库")
    check_names(loose)
    print("== 页面下拉")
    check_ui(loose)
    print("全部通过")


if __name__ == "__main__":
    main()
