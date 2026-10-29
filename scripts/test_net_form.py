# -*- coding: utf-8 -*-
"""回归：车站 / 线路查询的输入行、站站查询的输入行没被别的样式挤扁。

背景：同起讫点车次列表最初也用了 .od-row，它的六列 grid 覆盖了查询表单的
.od-row{display:flex}，输入框被压成 24px。这里盯住三件事：
  1) 车站/线路 输入框和「查询」按钮同一行、输入框够宽；
  2) 站站查询 出发/到达 等宽、交换按钮夹在中间；
  3) 车站查询是直接出详情，空的 #netList 不能和详情平分高度。
用法：python scripts/test_net_form.py
"""
import os
import tempfile

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8686/"
SHOT = os.path.join(tempfile.gettempdir(), "railcn-net-form")


def box(pg, sel):
    r = pg.eval_on_selector(sel, "e => { const b = e.getBoundingClientRect();"
                                 "return [b.x, b.y, b.width, b.height]; }")
    return [round(v) for v in r]


def main():
    os.makedirs(SHOT, exist_ok=True)
    bad = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_context(viewport={"width": 1440, "height": 900}).new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(BASE, wait_until="load")

        # ---------- 车站 / 线路 ----------
        pg.click('[data-tab="net"]')
        pg.wait_for_timeout(300)
        q = box(pg, "#netQ")
        btn = box(pg, "#btnNet")
        print("车站线路 输入框:", q, "查询按钮:", btn)
        if q[2] < 200:
            bad.append("车站/线路 输入框宽度只有 %dpx" % q[2])
        if q[1] != btn[1]:
            bad.append("输入框和查询按钮不在同一行（y %d / %d）" % (q[1], btn[1]))
        if btn[0] < q[0] + q[2]:
            bad.append("查询按钮压到了输入框上")

        # 输入联想框：贴着输入框、行高有内边距、能关掉
        pg.click("#netQ")
        pg.type("#netQ", "北京南", delay=30)
        pg.wait_for_timeout(600)
        sug = box(pg, "#sugNet")
        item = box(pg, "#sugNet .si")
        print("联想框:", sug, "首条:", item)
        if sug[2] < q[2] - 2:
            bad.append("联想框比输入框窄（%d < %d）" % (sug[2], q[2]))
        if item[3] < 28:
            bad.append("联想条目只有 %dpx 高，没有内边距（.si 样式没生效？）" % item[3])
        pg.keyboard.press("Escape")
        if not pg.eval_on_selector("#sugNet", "e => e.classList.contains('hidden')"):
            bad.append("按 Esc 联想框没关掉")
        pg.click("#netQ")
        pg.type("#netQ", "南", delay=30)
        pg.wait_for_timeout(600)
        pg.click("#paneNet", position={"x": 300, "y": 400})
        pg.wait_for_timeout(200)
        if not pg.eval_on_selector("#sugNet", "e => e.classList.contains('hidden')"):
            bad.append("点空白处联想框没关掉")

        # 车站查询：直接出详情，列表必须是空的、且不占高度
        pg.fill("#netQ", "北京南")
        pg.click("#btnNet")
        pg.wait_for_timeout(1200)
        lst = box(pg, "#netList")
        det = box(pg, "#netDetail")
        pane = box(pg, "#paneNet")
        print("列表:", lst, "详情:", det, "面板:", pane)
        if lst[3] != 0:
            bad.append("车站查询后空列表仍占 %dpx 高" % lst[3])
        if det[3] < pane[3] * 0.8:
            bad.append("详情只占 %dpx（面板 %dpx），被空列表挤到下半屏" % (det[3], pane[3]))
        pg.locator("#paneNet").screenshot(path=os.path.join(SHOT, "station.png"))

        # ---------- 站站查询 ----------
        pg.click('[data-tab="od"]')
        pg.wait_for_timeout(300)
        f, t, sw = box(pg, "#odFrom"), box(pg, "#odTo"), box(pg, "#btnSwap")
        print("出发:", f, "到达:", t, "交换:", sw)
        if abs(f[2] - t[2]) > 2:
            bad.append("出发/到达 不等宽（%d / %d）" % (f[2], t[2]))
        if f[2] < 100:
            bad.append("出发输入框被挤到 %dpx" % f[2])
        if not (f[0] + f[2] <= sw[0] and sw[0] + sw[2] <= t[0] + 1):
            bad.append("交换按钮没夹在出发/到达中间")
        pg.locator("#paneOD").screenshot(path=os.path.join(SHOT, "od.png"))

        # 表单行仍然是 flex（不是同起讫点列表那套六列 grid）
        disp = pg.evaluate("() => getComputedStyle(document.querySelector('.od-form .od-row')).display")
        print("表单行 display:", disp)
        if disp != "flex":
            bad.append("查询表单的 .od-row 变成了 %s" % disp)

        if errs:
            bad.append("页面报错：%s" % errs)
        b.close()

    for m in bad:
        print("  ✗", m)
    print("全部通过（截图 %s）" % SHOT if not bad else "失败 %d 项" % len(bad))


main()
