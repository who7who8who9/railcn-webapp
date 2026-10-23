"""回归：地图事件层不再抢走 dock 命中 + 倍速自选可用。"""
import json

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8686/"
WITH_MAP = "--with-map" in __import__("sys").argv


def run(p, with_map):
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": 1440, "height": 900})
    if with_map:
        ctx.add_init_script("localStorage.setItem('railcn.mapkey','TESTKEY123')")
    pg = ctx.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(BASE, wait_until="load")
    pg.fill("#q", "G1")
    pg.click("#btnSearch")
    pg.wait_for_selector("#dock:not(.hidden)", timeout=30000)
    pg.wait_for_timeout(2500)

    hit = pg.evaluate(
        """() => {
      const tl = document.querySelector('#tl'), r = tl.getBoundingClientRect();
      const el = document.elementFromPoint(r.left + r.width/2, r.top + r.height/2);
      return {tag: el.tagName, id: el.id, cls: String(el.className)};
    }"""
    )
    print(f"[map={with_map}] tl 命中:", hit)

    box = pg.locator("#tl").bounding_box()
    y = box["y"] + box["height"] / 2
    before = pg.text_content("#labNow")
    pg.mouse.move(box["x"] + box["width"] * 0.75, y)
    pg.mouse.down()
    pg.mouse.move(box["x"] + box["width"] * 0.25, y, steps=15)
    pg.mouse.up()
    pg.wait_for_timeout(300)
    after = pg.text_content("#labNow")
    print(f"  拖动前 {before} → 拖动后 {after}  拖动{'成功' if before != after else '失败'}")

    # 倍速面板
    pg.click("#btnSpeedMore")
    pg.wait_for_timeout(200)
    print("  面板可见:", pg.is_visible("#spdPop"))
    pg.click('#spdPop .spd-grid button[data-speed="1200"]')
    pg.wait_for_timeout(250)
    print("  选 1200× 后：按钮 =", pg.text_content("#btnSpeedMore"),
          "| S.speed =", pg.evaluate("() => S.speed"),
          "| 面板关:", not pg.is_visible("#spdPop"),
          "| 记忆 =", pg.evaluate("() => localStorage.getItem('railcn.speed')"))

    pg.click("#btnSpeedMore")
    pg.fill("#spdInput", "900")
    pg.press("#spdInput", "Enter")
    pg.wait_for_timeout(250)
    print("  自定义 900：按钮 =", pg.text_content("#btnSpeedMore"),
          "| S.speed =", pg.evaluate("() => S.speed"),
          "| 面板关:", not pg.is_visible("#spdPop"))

    # 点外部关闭
    pg.click("#btnSpeedMore")
    pg.mouse.click(700, 300)
    pg.wait_for_timeout(200)
    print("  点外部关闭:", not pg.is_visible("#spdPop"))

    # 1× 仍然可用
    pg.click('.btn.speed[data-speed="1"]')
    print("  1× :", pg.evaluate("() => S.speed"), "| 入口文本 =", pg.text_content("#btnSpeedMore"))

    pg.screenshot(path=f"D:/railcn/docs/dock-speed-{'map' if with_map else 'nomap'}.png")
    print("  pageerror:", errs)
    b.close()


with sync_playwright() as p:
    run(p, WITH_MAP)
