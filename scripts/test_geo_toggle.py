"""回归：车次详情地图「站点直连 ⇄ 沿真实线路」切换。"""
import os
import sys
import tempfile

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8686/"
SHOT = os.path.join(tempfile.gettempdir(), "railcn-geo")


def main():
    codes = sys.argv[1:] or ["G1", "K8361", "D2290"]
    os.makedirs(SHOT, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--no-proxy-server"])
        ctx = b.new_context(viewport={"width": 1440, "height": 900})
        pg = ctx.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(BASE, wait_until="load")
        pg.wait_for_timeout(1800)

        for spec in codes:
            code, _, date = spec.partition("|")
            pg.fill("#q", code)
            if date:
                pg.fill("#date", date)
            with pg.expect_response(
                    lambda r: "/api/route" in r.url and "code=" + code in r.url,
                    timeout=60000):
                pg.click("#btnSearch")
            pg.wait_for_selector("#geoBtn", timeout=40000)
            pg.wait_for_timeout(1200)

            js_d = """() => {
              const ps = [...document.querySelectorAll('#fallback path')];
              const d = ps.length ? (ps[0].getAttribute('d') || '') : '';
              return {n: ps.length, len: d.length, pts: (d.match(/L/g) || []).length + (d ? 1 : 0)};
            }"""
            before = pg.evaluate(js_d)
            pg.screenshot(path=os.path.join(SHOT, f"{code}-1-straight.png"))

            with pg.expect_response(lambda r: "/api/route/osm" in r.url, timeout=60000):
                pg.click("#geoBtn")
            pg.wait_for_timeout(1500)
            note = pg.inner_text("#geoNote")
            label = pg.inner_text("#geoBtn")
            after = pg.evaluate(js_d)
            print(f"--- {code} 切换后按钮「{label}」 备注「{note}」")
            print(f"    直连 d 长 {before['len']} / 折点 {before['pts']}"
                  f"  →  真实 d 长 {after['len']} / 折点 {after['pts']}")
            assert after['len'] != before['len'], "切换后路线没有变化"
            pg.screenshot(path=os.path.join(SHOT, f"{code}-2-osm.png"))

            pg.click("#geoBtn")
            pg.wait_for_timeout(900)
            back = pg.inner_text("#geoBtn")
            rev = pg.evaluate(js_d)
            note2 = pg.inner_text("#geoNote")
            print(f"    切回：「{back}」 折点 {rev['pts']}  备注「{note2}」")
            assert rev['len'] == before['len'], "切回后没有恢复到站点直连"
            assert note2 == '', "切回后备注未清空"

        print("页面错误:", errs or "无")
        b.close()


if __name__ == "__main__":
    main()
