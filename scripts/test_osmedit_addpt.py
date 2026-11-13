# -*- coding: utf-8 -*-
"""回归：走向编辑器「添加点」的点序 —— 第 3 点必须接在第 2 点后面

现象（2026-11-11 用户报）：添加点模式下点第 3 个点，线「直连第 1 点」，不是
接着第 2 点往后画。

根因：insertPoint() 只把「点数 < 2」当画线，第 3 点就掉进「最近线段插入」分支；
那时只有一段（第 1→2 点），于是新点被插到两者中间，画出来是 1→3→2，
看着像新点连回了第 1 点。载入已有走向后也是这样（用户是清空后重画遇到的）。

现在 S.drawing 记「正在按点序画线」：新建线 / 刚起头 / 连续往下画都追加到末尾；
载入已有走向才按最近线段插；另外点落在线尾延长线上也往后接，不再回折。

跑法：python scripts/test_osmedit_addpt.py
（离线自绘模式，不需要地图 Key；临时草稿跑完删掉）
"""
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
TEST = "test_加点回归线"
PASS, FAIL = [], []


def ck(label, cond, extra=""):
    (PASS if cond else FAIL).append(label)
    print(("  ok   " if cond else "  FAIL ") + label +
          ("" if cond else "  ← " + str(extra)[:220]))


def post(path, body):
    req = urllib.request.Request(
        BASE + path, data=__import__("json").dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    return __import__("json").loads(urllib.request.urlopen(req, timeout=60).read())


# ---------------------------------------------------------------- 页面工具
def stat(pg):
    """把状态条读成结构化数据：点数 / 段数 / 选中第几点 / 里程 / 画线追加标记"""
    t = pg.inner_text("#oeStat")
    m = re.search(r"可编辑点数：(\d+)", t)
    s = re.search(r"(\d+)\s*段", t)
    sel = re.search(r"已选中第\s*(\d+)\s*点", t)
    km = re.search(r"估算里程：([\d.]+)", t)
    return {"n": int(m.group(1)) if m else 0,
            "seg": int(s.group(1)) if s else 0,
            "sel": int(sel.group(1)) if sel else 0,
            "km": float(km.group(1)) if km else 0.0,
            "draw": "画线追加" in t,
            "txt": t.replace("\n", " | ")}


def click_at(pg, bb, fx, fy):
    pg.mouse.click(bb["x"] + bb["width"] * fx, bb["y"] + bb["height"] * fy)
    pg.wait_for_timeout(280)


def draw(pg, bb, pts, tag=""):
    """按给定画布比例依次点，返回每次点完的状态"""
    out = []
    for i, (fx, fy) in enumerate(pts):
        click_at(pg, bb, fx, fy)
        st = stat(pg)
        out.append(st)
        print("  %s点%d → %s" % (tag, i + 1, st["txt"]))
    return out


def canvas_box(pg):
    return pg.locator("#oeCanvas").bounding_box()


# ---------------------------------------------------------------- 用例
def check_draw(pg, label):
    """起画阶段：第 3、4、5 点都要接在末尾（sel=点数），不许插回前面"""
    bb = canvas_box(pg)
    seq = [(0.20, 0.80), (0.85, 0.85), (0.50, 0.50), (0.30, 0.20), (0.80, 0.15)]
    res = draw(pg, bb, seq, label + " ")
    ck(label + "：第 1 点起头", res[0]["n"] == 1 and res[0]["sel"] == 1, res[0]["txt"])
    ck(label + "：第 2 点接在后面", res[1]["n"] == 2 and res[1]["sel"] == 2, res[1]["txt"])
    ck(label + "：★ 第 3 点接在第 2 点后面（不是插回第 1、2 点之间）",
       res[2]["n"] == 3 and res[2]["sel"] == 3, res[2]["txt"])
    ck(label + "：第 4 点继续往后接", res[3]["n"] == 4 and res[3]["sel"] == 4, res[3]["txt"])
    ck(label + "：第 5 点继续往后接", res[4]["n"] == 5 and res[4]["sel"] == 5, res[4]["txt"])
    ck(label + "：全程一条线（1 段）", all(r["seg"] == 1 for r in res),
       [r["seg"] for r in res])
    ck(label + "：里程一路加长（点序没打乱）",
       all(res[i]["km"] <= res[i + 1]["km"] + 0.2 for i in range(4)),
       [r["km"] for r in res])
    ck(label + "：状态条标明「画线追加」", all(r["draw"] for r in res))


def check_edit(pg):
    """载入已有走向：仍是「最近线段插入」；点线尾延长线则往后接"""
    print("  -- 先建一条临时草稿（3 点直线），当作已有走向")
    r = post("/api/osmedit/geom", {"name": TEST, "apply": False,
                                   "parts": [[[116.0, 39.0], [116.4, 39.0], [116.8, 39.0]]]})
    ck("临时草稿已写入", bool(r.get("ok")), str(r)[:120])

    pg.fill("#oeQ", TEST)
    pg.wait_for_timeout(600)
    items = pg.query_selector_all("#oeList .oe-item")
    ck("列表里能搜到这条草稿", len(items) >= 1, len(items))
    items[0].click()
    pg.wait_for_timeout(900)
    st = stat(pg)
    ck("已载入 3 点草稿", st["n"] == 3, st["txt"])
    ck("载入已有走向后状态条不再是「画线追加」", not st["draw"], st["txt"])

    bb = canvas_box(pg)
    # 中间插一点：落点在第 1、2 点之间（画布横向前 1/3、纵向居中）
    click_at(pg, bb, 0.30, 0.50)
    st2 = stat(pg)
    print("  中间插点 →", st2["txt"])
    ck("编辑模式点空白处仍然插入（点数 +1）", st2["n"] == 4, st2["txt"])
    ck("插进去的点排在第 2 位（不是加到末尾）", st2["sel"] == 2, st2["txt"])

    # 线尾延长线：点画布最右侧、和线同一高度 → 应该接成第 5 点，不是插到倒数第二点前
    click_at(pg, bb, 0.98, 0.50)
    st3 = stat(pg)
    print("  线尾延长线加点 →", st3["txt"])
    ck("点线尾延长线往后接（点数 +1）", st3["n"] == 5, st3["txt"])
    ck("★ 接在末尾第 5 点（不是插回倒数第二点）", st3["sel"] == 5, st3["txt"])
    return bb


def check_clear_redraw(pg):
    """清空重画（用户实际遇到的路径）：清空后照样按点序追加"""
    pg.once("dialog", lambda d: d.accept())
    pg.click("#oeClear")
    pg.wait_for_timeout(400)
    st = stat(pg)
    ck("清空后没有点", st["n"] == 0, st["txt"])
    check_draw(pg, "清空重画")


def check_static():
    print("[4] 静态护栏：判据写在 insertPoint / 载入时复位")
    js = open(os.path.join(ROOT, "webapp", "static", "osmedit.js"),
              encoding="utf-8").read()
    html = open(os.path.join(ROOT, "webapp", "static", "osmedit.html"),
                encoding="utf-8").read()
    ck("S 里有 drawing 状态", "drawing: false" in js)
    ck("insertPoint 把「正在画线」算作追加",
       "S.isNew || S.drawing || S.pts.length < 2" in js)
    ck("追加时标记 drawing", "S.drawing = true" in js)
    ck("载入线路 / 建线完成复位 drawing（≥2 处）",
       js.count("S.drawing = false") >= 3, js.count("S.drawing = false"))
    ck("线尾延长线往后接（投影参数判尾）", "bt > 0.999" in js)
    ck("segProj 同时给距离与投影参数", "function segProj(" in js and "return { d:" in js)
    ck("帮助文案写明第 3 点接末尾", "第 3 点也接在末尾" in html)


def main():
    try:
        with sync_playwright() as p:
            br = p.chromium.launch(args=["--no-proxy-server"])
            pg = br.new_context(viewport={"width": 1440, "height": 900}).new_page()
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.goto(BASE + "/osmedit.html", wait_until="load")
            pg.wait_for_timeout(1500)
            mode = pg.inner_text("#oeMapMode")
            print("底图模式：", mode)
            ck("降级到离线自绘（没 Key 也能测）", "离线自绘" in mode, mode)

            pg.click("#oeAdd")
            pg.wait_for_timeout(250)
            print("[1] 起画：连续点 5 个点，点序就是走向")
            check_draw(pg, "起画")

            print("[2] 编辑已有走向：插点规则不变")
            check_edit(pg)

            print("[3] 清空重画：照样按点序追加")
            check_clear_redraw(pg)

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
