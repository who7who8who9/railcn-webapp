# -*- coding: utf-8 -*-
"""回归：走向编辑器「连接两点」与「接着末尾画」

现象（2026-11-11 用户报）：「OSM 走向编辑器 可以直接连接原有的 2 点，现在不行」。

根因：编辑器按主站多段改造后，段与段之间**不连线**；点已有红点只做「选中」，
载入已有线路后加点又一律走「最近线段插入」，于是既没有把两个已有点连起来的
入口，也没法接着最后一个点往后画。

现在：
- 「⇄ 连接两点」/ 按住 Shift：依次点两个已有红点 → 直接连一条直线；
  两点正好在断点两侧时改为去掉断点，两段合成一段；再点同一对可断开。
- 「↳ 接着末尾画」：载入已有线路后显式切回按点序追加（默认插点不变）。
- 连线段端点引用 pts 下标，拖点 / 插点 / 删点 / 撤销都会同步维护。

跑法：python scripts/test_osmedit_link.py
（离线自绘模式，不需要地图 Key；临时草稿跑完删掉）
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request

from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "webapp"))

import osmedit                                              # noqa: E402

BASE = "http://127.0.0.1:8686"
TEST = "test_连接两点回归线"
PASS, FAIL = [], []


def ck(label, cond, extra=""):
    (PASS if cond else FAIL).append(label)
    print(("  ok   " if cond else "  FAIL ") + label +
          ("" if cond else "  ← " + str(extra)[:220]))


def http(path):
    return json.loads(urllib.request.urlopen(BASE + path, timeout=60).read())


def post(path, body):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=60).read())


def stat(pg):
    t = pg.inner_text("#oeStat")
    m = re.search(r"可编辑点数：(\d+)", t)
    s = re.search(r"(\d+)\s*段", t)
    lk = re.search(r"直连\s*(\d+)\s*段", t)
    sel = re.search(r"已选中第\s*(\d+)\s*点", t)
    return {"n": int(m.group(1)) if m else 0,
            "seg": int(s.group(1)) if s else 0,
            "link": int(lk.group(1)) if lk else 0,
            "sel": int(sel.group(1)) if sel else 0,
            # 线名里也可能含「连接两点」（本用例就是），必须用状态条的「· 连接两点」判
            "linkMode": bool(re.search(r"·\s*连接两点", t)),
            "draw": "画线追加" in t,
            "txt": t.replace("\n", " | ")}


def add_line(pg, q):
    pg.fill("#oeQ", q)
    pg.wait_for_timeout(650)
    items = pg.query_selector_all("#oeList .oe-item")
    return items


def screen_points(pg, parts, bb):
    """按 drawCanvas() 的自适应公式，把每个 [lon,lat] 反算成画布内坐标"""
    g = http("/api/osmedit/geom?name=" + urllib.parse.quote(TEST) + "&n=600")
    pts = [p for seg in (g.get("parts") or []) for p in seg]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    w = (maxx - minx) or 1
    h = (maxy - miny) or 1
    sc = min((bb["width"] - 80) / w, (bb["height"] - 80) / h)
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    return [[bb["x"] + bb["width"] / 2 + (p[0] - cx) * sc,
             bb["y"] + bb["height"] / 2 - (p[1] - cy) * sc] for p in pts]


def click_pt(pg, xy):
    pg.mouse.click(xy[0], xy[1])
    pg.wait_for_timeout(260)


def center_pixel(pg):
    """画布正中央一带的像素：直连段（两点对角连线）必然穿过这里"""
    return pg.evaluate("""() => {
      const c = document.querySelector('#oeCanvas'), x = c.getContext('2d');
      const r = c.getBoundingClientRect(), d = window.devicePixelRatio || 1;
      const dat = x.getImageData(Math.round((r.width/2 - 3) * d),
                                 Math.round((r.height/2 - 3) * d),
                                 Math.round(6 * d), Math.round(6 * d)).data;
      let red = false, maxA = 0;
      for (let k = 0; k < dat.length; k += 4) {
        if (dat[k+3] > maxA) maxA = dat[k+3];
        if (dat[k] > 160 && dat[k+1] < 110 && dat[k+2] < 110 && dat[k+3] > 60) red = true;
      }
      return { red, alpha: maxA };
    }""")


def check_link_mode(pg, bb, sp):
    print("[1] 连接两点：断点两侧合并成一段")
    st0 = stat(pg)
    ck("初始载入 6 点 / 2 段", st0["n"] == 6 and st0["seg"] == 2, st0["txt"])
    # 两段是两条水平线，画布正中央（对角连线中点）此时应该是空白
    px0 = center_pixel(pg)
    ck("连接前画布中心没有线（像素基线）", not px0["red"], px0)

    pg.click("#oeLink")
    pg.wait_for_timeout(220)
    st = stat(pg)
    ck("按钮进入连接两点模式", st["linkMode"], st["txt"])
    ck("连接模式关掉添加点模式", "on" not in (pg.get_attribute("#oeAdd", "class") or ""))

    # 第 3 点（上一段末点）→ 第 4 点（下一段起点）：断点两侧，应合并两段
    click_pt(pg, sp[2])
    st1 = stat(pg)
    ck("点第一个红点进入「已选第 3 点」", "已选第 3 点" in st1["txt"], st1["txt"])
    ck("选点不新增点", st1["n"] == 6, st1["txt"])
    click_pt(pg, sp[3])
    st2 = stat(pg)
    print("  断点两侧连接 →", st2["txt"])
    ck("★ 两段合成一段（段数 2→1）", st2["seg"] == 1, st2["txt"])
    ck("断点合并没有新增点", st2["n"] == 6, st2["txt"])
    ck("连接后仍停在连接模式（可继续连）", st2["linkMode"], st2["txt"])

    # 撤销应恢复 2 段
    pg.click("#oeUndo")
    pg.wait_for_timeout(220)
    st3 = stat(pg)
    ck("撤销恢复 2 段（断点回来了）", st3["seg"] == 2, st3["txt"])

    print("[2] 连接两点：任意两个已有点直连成一条直线")
    # 第 1 点 ↔ 第 6 点（同属不同段、不相邻）→ 新增一条直连段
    click_pt(pg, sp[0])
    click_pt(pg, sp[5])
    st4 = stat(pg)
    print("  跨点直连 →", st4["txt"])
    ck("★ 出现 1 条直连段", st4["link"] == 1, st4["txt"])
    ck("直连不改动原有 6 点", st4["n"] == 6, st4["txt"])
    px1 = center_pixel(pg)
    ck("★ 画布上真的画出了这条对角直线", px1["red"] and px1["alpha"] > px0["alpha"], px1)

    # 再点同一对 → 断开
    click_pt(pg, sp[0])
    click_pt(pg, sp[5])
    st5 = stat(pg)
    ck("再点同一对断开直连", st5["link"] == 0, st5["txt"])
    ck("断开后画布中心的直线也消失", not center_pixel(pg)["red"], center_pixel(pg))

    # 撤销两次回到「直连 1 段」→ 连线段也进撤销栈
    pg.click("#oeUndo")
    pg.wait_for_timeout(200)
    st6 = stat(pg)
    ck("撤销把直连段找回来", st6["link"] == 1, st6["txt"])

    # Esc 退出连接模式
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(200)
    st7 = stat(pg)
    ck("Esc 退出连接模式", not st7["linkMode"], st7["txt"])
    ck("退出连接模式后直连段仍在", st7["link"] == 1, st7["txt"])
    return st7


def check_shift(pg, bb, sp):
    print("[3] Shift 快捷：不进模式也能直连两点")
    pg.click("#oeUndo")          # 撤掉刚才那条直连，回到纯 2 段
    pg.wait_for_timeout(200)
    st0 = stat(pg)
    ck("准备：直连段为 0", st0["link"] == 0, st0["txt"])
    pg.keyboard.down("Shift")
    click_pt(pg, sp[1])
    click_pt(pg, sp[4])
    pg.keyboard.up("Shift")
    st1 = stat(pg)
    print("  Shift 直连 →", st1["txt"])
    ck("★ Shift 点两点直连成功", st1["link"] == 1, st1["txt"])
    ck("Shift 不会误入连接模式", not st1["linkMode"], st1["txt"])
    pg.click("#oeUndo")
    pg.wait_for_timeout(200)


def check_append(pg, bb, sp):
    print("[4] 接着末尾画：载入已有线路后按点序往后追加")
    st0 = stat(pg)
    ck("默认是插点模式（不是画线追加）", not st0["draw"], st0["txt"])
    pg.click("#oeAppend")
    pg.wait_for_timeout(220)
    st1 = stat(pg)
    ck("★ 切到画线追加", st1["draw"], st1["txt"])
    ck("接着末尾画自动打开添加点模式",
       "on" in (pg.get_attribute("#oeAdd", "class") or ""))

    # 点一个远离所有红点的空白处（右下角），不能命中 nearestVert 的 10px
    pg.mouse.click(bb["x"] + bb["width"] * 0.93, bb["y"] + bb["height"] * 0.90)
    pg.wait_for_timeout(260)
    st2 = stat(pg)
    print("  空白处加点 →", st2["txt"])
    ck("★ 新点接在末尾（选中第 7 点）",
       st2["n"] == 7 and st2["sel"] == 7, st2["txt"])
    ck("追加后仍是 2 段（没插进中间）", st2["seg"] == 2, st2["txt"])
    pg.click("#oeUndo")
    pg.wait_for_timeout(200)
    ck("撤销回到 6 点", stat(pg)["n"] == 6, stat(pg)["txt"])


def check_save(pg, bb, sp):
    print("[5] 直连段随保存写回（parts 里多一条两点段）")
    pg.keyboard.down("Shift")
    click_pt(pg, sp[0])
    click_pt(pg, sp[5])
    pg.keyboard.up("Shift")
    ck("保存前有直连段", stat(pg)["link"] == 1, stat(pg)["txt"])
    pg.click("#oeSave")
    parts = []
    for _ in range(12):                 # POST 是异步的，等写盘完成再读
        pg.wait_for_timeout(250)
        g = http("/api/osmedit/geom?name=" + urllib.parse.quote(TEST) + "&n=600")
        parts = g.get("parts") or []
        if any(len(s) == 2 for s in parts):
            break
    two = [s for s in parts if len(s) == 2]
    ck("★ 保存后 parts 里出现两点直连段", len(two) >= 1,
       [len(s) for s in parts])
    ck("原有两段仍在", len(parts) >= 3, [len(s) for s in parts])


def check_static():
    print("[6] 静态护栏")
    js = open(os.path.join(ROOT, "webapp", "static", "osmedit.js"),
              encoding="utf-8").read()
    html = open(os.path.join(ROOT, "webapp", "static", "osmedit.html"),
                encoding="utf-8").read()
    ck("S 里有 links / linkMode", "links: []" in js and "linkMode: false" in js)
    ck("splitParts 把直连段并入 parts",
       "for (const [i, j] of S.links)" in js)
    ck("连接两点按钮存在", 'id="oeLink"' in html and "#oeLink" in js)
    ck("接着末尾画按钮存在", 'id="oeAppend"' in html and "#oeAppend" in js)
    ck("断点两侧走合并、其余新增直连段",
       "if (hi === lo + 1 && isBreak(hi))" in js and "S.links.push([lo, hi])" in js)
    ck("再点同一对断开", "S.links.splice(k, 1)" in js)
    ck("撤销同时保存 / 恢复直连段",
       "links: S.links.map(l => l.slice())" in js and "st.links || []" in js)
    ck("删点维护直连段下标", "a > i ? a - 1 : a" in js)
    ck("插点平移直连段下标", "shiftLinks(best, 1)" in js)
    ck("里程统计含直连段", "for (const [i, j] of S.links)" in js)
    ck("腾讯地图 / Canvas 点红点都走 onVertClick",
       "onVertClick(+e.geometry.id.slice(1))" in js and "onVertClick(i, e.shiftKey)" in js)
    ck("连接模式下点空白处不插点",
       js.count("连接两点：请点") >= 2, js.count("连接两点：请点"))
    ck("Esc 退出连接模式", "e.key === 'Escape'" in js)
    ck("帮助文案写明连接两点", "连接两点" in html and "Shift" in html)
    ck("帮助文案写明接着末尾画", "接着末尾画" in html)


def main():
    parts = [
        [[116.0, 39.0], [116.5, 39.0], [117.0, 39.0]],
        [[117.5, 39.5], [118.0, 39.5], [118.5, 39.5]],
    ]
    try:
        r = post("/api/osmedit/geom", {"name": TEST, "apply": False, "parts": parts})
        ck("临时两段草稿已写入", bool(r.get("ok")), str(r)[:120])
        with sync_playwright() as p:
            br = p.chromium.launch(args=["--no-proxy-server"])
            pg = br.new_context(viewport={"width": 1440, "height": 900}).new_page()
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.goto(BASE + "/osmedit.html", wait_until="load")
            pg.wait_for_timeout(1400)
            mode = pg.inner_text("#oeMapMode")
            ck("降级到离线自绘（没 Key 也能测）", "离线自绘" in mode, mode)

            items = add_line(pg, TEST)
            ck("列表里能搜到草稿", len(items) >= 1, len(items))
            items[0].click()
            pg.wait_for_timeout(900)
            st = stat(pg)
            ck("载入草稿 6 点 / 2 段", st["n"] == 6 and st["seg"] == 2, st["txt"])

            bb = pg.locator("#oeCanvas").bounding_box()
            sp = screen_points(pg, parts, bb)
            ck("6 个顶点的屏幕坐标互不重叠",
               all(abs(sp[i][0] - sp[j][0]) > 20 or abs(sp[i][1] - sp[j][1]) > 20
                   for i in range(6) for j in range(i + 1, 6)), sp)

            check_link_mode(pg, bb, sp)
            check_shift(pg, bb, sp)
            check_append(pg, bb, sp)
            check_save(pg, bb, sp)
            check_static()
            print("  页面错误:", errs or "无")
            ck("页面没有 JS 报错", not errs, errs[:2])
            br.close()
    finally:
        try:
            osmedit.drop_draft(TEST)
        except Exception as exc:                            # noqa: BLE001
            print("  清理临时草稿失败:", exc)
    print("\n通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        for x in FAIL:
            print("  × " + x)
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
