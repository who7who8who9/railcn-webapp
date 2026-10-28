"""回归：车站查询的两个坑

1. 站名带「站」字也要能搜到（唐海南站 → 唐海南）；
2. 线路库里没有档案、但 12306 站表有的站（唐海南 / 曹妃甸港 / 南堡北），
   点进去不能再说「车站库里没有」，要给出电报码、坐标和当天经停的车次。
"""
import json
import os
import sys
import tempfile
import urllib.parse
import urllib.request

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8686"
SHOT = os.path.join(tempfile.gettempdir(), "railcn-station")


def get(path):
    return json.load(urllib.request.urlopen(BASE + path, timeout=60))


def check_api(loose, archived):
    """loose：还没有档案的站（走 12306 兜底）；archived：档案里有、但要能查到的站

    自建线路会把沿线站补进 station_custom（source=line-fill），所以「唐海南」
    「曹妃甸港」在有人建了曹妃甸港线之后就有档案了 —— 这里两种都认，
    只要求「没有档案 → 必须有当天经停车次」「有档案 → 必须有连接线路」。
    """
    for name in loose:
        d = get("/api/station?q=" + urllib.parse.quote(name + "站"))
        assert d.get("found"), f"{name} 查不到"
        print(f"  {name:6} 电报码 {d.get('tele')} 坐标 {d.get('lat')},{d.get('lon')} "
              f"{'档案' if d.get('archive') else '12306 兜底'} "
              f"经停 {len(d.get('trainsVia') or [])} 趟 ({d.get('viaDate')})")
        assert d.get("lat") is not None, f"{name} 没有坐标"
        assert d.get("tele"), f"{name} 没有电报码"
        if d.get("archive") is False:
            assert d.get("trainsVia"), f"{name} 没有档案却也没列经停车次"
        else:
            assert d.get("lines"), f"{name} 有档案却没有连接线路"
        # 带不带「站」字结果要一致
        bare = get("/api/station?q=" + urllib.parse.quote(name))
        assert bare.get("name") == d.get("name"), f"{name} 带/不带「站」结果不一致"
        # 联想：带「站」也能搜到，且同名的两种写法只出一条
        items = get("/api/stations?q=" + urllib.parse.quote(name + "站") + "&limit=6")["items"]
        names = [x["name"] for x in items]
        print(f"       联想 → {names}")
        assert d["name"] in names, f"{name} 联想搜不到"

    for name in archived:
        items = get("/api/stations?q=" + urllib.parse.quote(name + "站") + "&limit=8")["items"]
        names = [x["name"] for x in items]
        dup = [n for n in names if n != name and n == name + "站"]
        print(f"  {name:6} 联想 → {names[:6]}")
        assert name in names, f"{name} 联想里没有主条目"
        assert not dup, f"{name} 与「{name}站」重复出现"
        d = get("/api/station?q=" + urllib.parse.quote(name))
        assert d.get("found") and d.get("lines"), f"{name} 档案丢了连接线路"


def check_ui(name):
    os.makedirs(SHOT, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--no-proxy-server"])
        pg = b.new_context(viewport={"width": 1440, "height": 900}).new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(BASE + "/", wait_until="load")
        pg.wait_for_timeout(1500)
        pg.click('[data-tab="net"]')
        pg.wait_for_timeout(500)
        pg.fill("#netQ", name + "站")
        with pg.expect_response(lambda r: "/api/station?" in r.url, timeout=60000):
            pg.press("#netQ", "Enter")
        pg.wait_for_selector("#netDetail.show", timeout=30000)
        pg.wait_for_timeout(800)
        l1 = pg.inner_text("#netDetail .dhead .l1").replace("\n", " | ")
        note = pg.inner_text("#netDetail .st-note") if pg.query_selector("#netDetail .st-note") else ""
        via = pg.inner_text("#netDetail .st-via") if pg.query_selector("#netDetail .st-via") else ""
        print(f"  {name} 头部：{l1}")
        print(f"       备注：{note}")
        print(f"       经停：{via.replace(chr(10), ' / ')[:90]}")
        assert "仅 12306 站表" in l1, "头部没有「仅 12306 站表」标记"
        assert "线路库" in note, "没有说明线路库缺档案"
        assert via, "没有列经停车次"
        pg.evaluate("""() => {const n = document.querySelector('#netDetail');
                          n.scrollTop = n.scrollHeight;}""")
        pg.wait_for_timeout(400)
        pg.screenshot(path=os.path.join(SHOT, f"{name}.png"))
        # 点经停的车次 → 应当跳到车次详情
        chip = pg.query_selector("#netDetail .st-via .chip")
        if chip:
            code = chip.inner_text()
            with pg.expect_response(lambda r: "/api/route" in r.url and "code=" + code in r.url,
                                    timeout=60000):
                chip.click()
            pg.wait_for_timeout(1200)
            head = pg.inner_text("#detail .dhead .l1").replace("\n", " | ")
            print(f"       点 {code} → {head[:60]}")
            assert code in head, "点车次没打开对应详情"
        print("  页面错误:", errs or "无")
        b.close()


def main():
    loose = sys.argv[1:] or ["唐海南", "曹妃甸港", "南堡北"]
    print("== 接口")
    check_api(loose, ["唐山", "上海虹桥"])
    print("== 页面")
    # 「仅 12306 站表」的界面只对还没档案的站有意义（自建线路会把这些站补进车站库）
    still = [n for n in loose
             if get("/api/station?q=" + urllib.parse.quote(n)).get("archive") is False]
    if still:
        check_ui(still[0])
    else:
        print(f"  {'、'.join(loose)} 都已有档案，跳过「仅 12306 站表」的界面检查")
    print("全部通过")


if __name__ == "__main__":
    main()
