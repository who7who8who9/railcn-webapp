"""回归：自建线路的站点要么有坐标、要么说清怎么补

手工「+ 线路」只存了线路和站序，档案里没有的站（唐海南 / 曹妃甸港这类）就
没有坐标 —— 线路页标「缺坐标」、地图上一个点也画不出来。落库时要顺手把
沿线站补进 station_custom（source=line-fill，坐标取档案 → OSM → 人工的合并值）。

用法：python scripts/test_line_stations.py [线路名]
"""
import json
import os
import sys
import tempfile
import urllib.parse
import urllib.request

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8686"
SHOT = os.path.join(tempfile.gettempdir(), "railcn-linest")


def get(path):
    return json.load(urllib.request.urlopen(BASE + path, timeout=60))


def post(path, body):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    return json.load(urllib.request.urlopen(req, timeout=90))


#: 挑测试站用的候选池：只有 OSM 坐标、既没有车站档案也没有 12306 站表条目的
#: 站名（淮南矿区的支线站），line-fill 才会真的补它们
POOL = ("蔡家岗", "望峰岗", "毕家岗", "田家庵", "潘一矿", "田集", "戴楼")


def pick_pair():
    """找两个「没有档案、但有坐标」的站：只有这种站才会被 line-fill 补进去。
    已经挂在别的线路上的站不能动，所以先排掉 station_custom 里有的。"""
    custom = {s["name"] for s in get("/api/custom")["stations"]}
    return [n for n in POOL if n not in custom][:2]


def check_save_path():
    """存一条临时线路，看沿线站有没有当场补进车站库；跑完全部撤掉"""
    pair = pick_pair()
    if len(pair) < 2:
        print("  跳过：找不到「档案里没有但坐标有」的测试站（库里的这类站已被线路占用）")
        return
    a, b = pair
    name = "回归测试疏港线"
    # 先把可能残留的同名线路 / 车站清掉，保证测的是这次保存的效果
    post("/api/line/delete", {"name": name})
    made = [n for n in (a, b)
            if not any(s["name"] == n for s in get("/api/custom")["stations"])]
    for n in made:
        post("/api/station/delete", {"name": n})
    try:
        r = post("/api/line/new", {"name": name, "note": "回归测试，会自动删掉",
                                  "stations": [{"name": a, "km": 0}, {"name": b, "km": 30}]})
        filled = r["line"].get("filledStations") or []
        print(f"  新建「{name}」（{a} / {b}）→ 补进车站库 {filled}")
        assert set(filled) == {a, b}, f"没把沿线站补进车站库：{filled}"
        d = get("/api/line?q=" + urllib.parse.quote(name))
        assert d.get("coordCoverage") == 100.0, "新建线路的坐标覆盖不是 100%"
        for p in d["stations"]:
            print(f"    {p['name']:8} ({p['lat']}, {p['lon']}) {p.get('coordSrc')}")
            assert p["lat"] is not None, f"{p['name']} 建线后仍缺坐标"
            st = get("/api/station?q=" + urllib.parse.quote(p["name"]))
            assert name in (st.get("lines") or []), f"{p['name']} 没挂上「{name}」"
    finally:
        post("/api/line/delete", {"name": name})
        for n in made:                 # 只删这次新建的站，别碰原有档案
            post("/api/station/delete", {"name": n})
        left = [s["name"] for s in get("/api/custom")["stations"] if s["name"] in (a, b)]
        print(f"  清理完成，残留 {left or '无'}")


def check_api(name):
    d = get("/api/line?q=" + urllib.parse.quote(name))
    print(f"  {d.get('name')}：{d.get('n')} 站，坐标覆盖 {d.get('coordCoverage')}%")
    for p in d.get("stations", []):
        print(f"    {p['name']:8} km={p['km']} ({p['lat']}, {p['lon']}) {p.get('coordSrc')}")
        assert p["lat"] is not None and p["lon"] is not None, f"{p['name']} 还是没有坐标"
    assert d.get("coordCoverage") == 100.0, "坐标覆盖不是 100%"
    # 这些站应当真的进了车站库（不是只在线路详情里临时补的）
    for p in d["stations"]:
        st = get("/api/station?q=" + urllib.parse.quote(p["name"]))
        assert st.get("found"), f"{p['name']} 车站库里查不到"
        assert st.get("lat") is not None, f"{p['name']} 车站档案里没有坐标"
        print(f"    站档：{st.get('name')} 电报码 {st.get('tele')} 线路 {st.get('lines')}")
        assert name in (st.get("lines") or []), f"{p['name']} 的连接线路里没有「{name}」"


def check_ui(name):
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
        pg.click('.net-toggle .chipsel[data-kind="line"]')   # 默认是搜车站
        pg.wait_for_timeout(300)
        pg.fill("#netQ", name)
        with pg.expect_response(lambda r: "/api/line?" in r.url, timeout=30000):
            pg.press("#netQ", "Enter")
        pg.wait_for_selector("#netDetail.show", timeout=20000)
        pg.wait_for_timeout(1500)
        stops = pg.query_selector_all("#netDetail .stop")
        miss = [s for s in stops if s.query_selector(".s")]
        print(f"  站点 {len(stops)} 个，标「缺坐标」的 {len(miss)} 个")
        assert len(stops) >= 2, "站点没渲染出来"
        assert not miss, "还有站点标着缺坐标"
        l2 = pg.inner_text("#netDetail .dhead .l2").replace("\n", " ")
        print(f"  头部：{l2[:80]}")
        assert "缺坐标" not in l2, "头部还提示缺坐标"
        # 地图上真的有点：SVG 示意模式数 <circle>，腾讯地图取 TMapView.pts
        # （TMapView / SvgView 都是 app.js 顶层 const，不在 window 上，只能用裸名字）
        n = pg.evaluate("""() => {
          const c = document.querySelectorAll('#fallback circle').length;
          const t = (typeof TMapView !== 'undefined' && TMapView.pts) ? TMapView.pts.length : 0;
          return Math.max(c, t);
        }""")
        print(f"  地图点数：{n}")
        assert n >= 1, "地图上没有画出站点"
        pg.screenshot(path=os.path.join(SHOT, "line.png"))
        print("  页面错误:", errs or "无")
        assert not errs, "页面有 JS 报错"
        b.close()


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "曹妃甸港线"
    print("== 保存路径")
    check_save_path()
    print("== 已有线路")
    check_api(name)
    print("== 页面")
    check_ui(name)
    print("全部通过")


if __name__ == "__main__":
    main()
