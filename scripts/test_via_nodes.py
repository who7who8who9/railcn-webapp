"""回归：经由选站要能选到线路所（沪春线的春申南所、宝成线的灭火沟线路所）。

以前 `cuNotStation` 把「XX所 / 中继站」从候选里一律剔掉，线路所根本选不到 ——
而经由恰恰经常要在这些所转线。现在候选按站序列出、右侧标「线路所」；
命名线路站序里没有的，用资料库的同名站序补回来（只补节点，客运站顺序不动）。

顺带守「线路下拉要能选到线路库没收的线」：合新高速线只在 OSM（6 站）/
china-emu（10 站）里有站序，线路库没收录。以前下拉只列 `cnrail.search_line()`，
用户搜遍都找不到它；现在库外的也并进来并标「未收进库 · OSM」，
`_line_span_names` 也认它（否则选了也报「线路库里没有这条线」）。
"""
import json
import os
import tempfile
import urllib.parse
import urllib.request

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8686/"
SHOT = os.path.join("D:/railcn/docs", "via-node.png")
TMP = os.path.join(tempfile.gettempdir(), "railcn-via-node")
os.makedirs(TMP, exist_ok=True)


def get(path):
    return json.load(urllib.request.urlopen(BASE.rstrip("/") + path, timeout=120))


def stops(q):
    return get("/api/line/stops?q=" + urllib.parse.quote(q))


def check_api(bad):
    s = stops("沪春线")
    print("沪春线：", s["items"])
    if "春申南所" not in s["items"]:
        bad.append("沪春线的候选里没有春申南所")

    b = stops("宝成线")["items"]
    hit = [i for i, n in enumerate(b) if n == "灭火沟线路所"]
    if not hit:
        bad.append("宝成线没有补上灭火沟线路所（%d 站）" % len(b))
    else:
        i = hit[0]
        near = " → ".join(b[max(0, i - 1):i + 2])
        print("宝成线：%d 站，灭火沟线路所第 %d 站（%s）" % (len(b), i + 1, near))
        if not (i > 0 and b[i - 1] == "巨亭" and i + 1 < len(b) and b[i + 1] == "阳平关"):
            bad.append("灭火沟线路所插错了位置：%s" % near)

    x = stops("银西高铁")          # 只在资料库、认不出对应自建线路的那类
    nodes = [n for n in x["items"] if n.endswith(("所", "中继站"))]
    print("银西高铁：found=%s，%d 站，节点 %s" % (x["found"], len(x["items"]), nodes))
    if len(x["items"]) < 5:
        bad.append("只在资料库里的线路拿不到站序（银西高铁只有 %d 站）" % len(x["items"]))


def check_line_names(bad):
    """线路下拉：线路库没收、但别的来源有站序的线也要能选到（合新高速线）"""
    d = get("/api/curate/lineNames")
    items = d.get("items") or []
    extra = [x for x in items if x.get("inLib") is False]
    print("线路下拉：%d 条（库内 %d / 库外 %d）"
          % (len(items), len(items) - len(extra), len(extra)))
    if not extra:
        bad.append("线路下拉里一条库外线路都没有（OSM / china-emu 独有的选不到）")

    hit = [x for x in items if x["name"] == "合新高速线"]
    if not hit:
        bad.append("合新高速线没进线路下拉")
    else:
        x = hit[0]
        print("合新高速线：%s 站 · %s · %s%s"
              % (x.get("n"), x.get("totalKm"), x.get("src"),
                 "" if x.get("inLib") else "（未收进库）"))
        if not x.get("n"):
            bad.append("合新高速线条目没带站数：%s" % x)
        if not x.get("inLib") and x.get("src") not in ("osm", "chinaemu", "catalog"):
            bad.append("合新高速线的来源没标对：%s" % x)

    s = stops("合新高速线")
    if len(s.get("items") or []) < 5:
        bad.append("合新高速线的站序拿不到（选了也用不了）：%s"
                   % (s.get("items") or s))


def check_ui(bad):
    with sync_playwright() as p:
        br = p.chromium.launch(args=["--no-proxy-server"])
        pg = br.new_context(viewport={"width": 1500, "height": 980},
                            device_scale_factor=2).new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("dialog", lambda d: d.accept())
        pg.goto(BASE, wait_until="load")
        pg.wait_for_timeout(1200)
        pg.click('[data-tab="curate"]')
        pg.wait_for_selector("#cuList .cu-row, #cuList .empty", timeout=30000)

        pg.evaluate("c => cuOpen(c)", "C2371")       # 上海南 → 合肥南，首段沪春线
        pg.wait_for_selector("#ceViaRows .ve-row", timeout=30000)
        row = pg.locator("#ceViaRows .ve-row").first

        # 线路框里打「合新」：库外那条也要出现在下拉里，并标出来源
        lsel = '#ceViaRows .ve-row:first-child .combo[data-cbkind="line"]'
        row.locator('.combo[data-cbkind="line"] input').fill("合新")
        pg.wait_for_timeout(500)
        pg.click(lsel + " .cb-toggle")
        pg.wait_for_timeout(600)
        litems = pg.evaluate(
            """sel => [...document.querySelectorAll(sel + ' .cb-item')].map(e => ({
                   n: e.querySelector('.nm').textContent.trim(),
                   mt: e.querySelector('.mt').textContent.trim()}))""", arg=lsel)
        print("线路下拉（合新）：", [(i["n"], i["mt"]) for i in litems])
        if not any(i["n"] == "合新高速线" for i in litems):
            bad.append("线路下拉里搜不到合新高速线：%s" % [i["n"] for i in litems])
        elif not any("未收进库" in i["mt"] for i in litems if i["n"] == "合新高速线"):
            bad.append("合新高速线没标「未收进库」：%s" % litems)
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(200)

        row.locator('.combo[data-cbkind="line"] input').fill("沪春线")
        pg.wait_for_timeout(1400)

        frm = '#ceViaRows .ve-row:first-child .combo[data-field="from"]'
        pg.click(frm + " .cb-toggle")
        pg.wait_for_function(
            """sel => document.querySelectorAll(sel + ' .cb-item').length >= 2""",
            arg=frm, timeout=15000)
        items = pg.evaluate(
            """sel => [...document.querySelectorAll(sel + ' .cb-item')].map(e => ({
                   n: e.querySelector('.nm').textContent.trim(),
                   mt: e.querySelector('.mt').textContent.trim(),
                   node: e.classList.contains('node')}))""", arg=frm)
        print("下拉候选：", [(i["n"], i["mt"], i["node"]) for i in items])
        hit = [i for i in items if i["n"] == "春申南所"]
        if not hit:
            bad.append("下拉里没有春申南所：%s" % [i["n"] for i in items])
        elif not hit[0]["node"] or hit[0]["mt"] != "线路所":
            bad.append("春申南所没标成「线路所」：%s" % hit[0])

        # 截图（「手工填写经由」整块 + 展开的下拉）
        card = pg.locator(".ce-card:has(#ceViaRows)").first
        bb = card.bounding_box()
        pg.screenshot(path=SHOT, clip={"x": max(0, bb["x"] - 10), "y": max(0, bb["y"] - 8),
                                       "width": min(1500 - max(0, bb["x"] - 10), bb["width"] + 20),
                                       "height": bb["height"] + 14})

        if hit:
            pg.click(frm + " .cb-item >> text=春申南所")
            pg.wait_for_timeout(300)
            val = pg.eval_on_selector(frm + " input", "e => e.value")
            tag = pg.eval_on_selector(
                "#ceViaRows .ve-row:first-child .ve-src", "e => e.textContent.trim()")
            print("选中后：值=%r，段标签=%r" % (val, tag))
            if val != "春申南所":
                bad.append("点了春申南所，输入框却是 %r" % val)
            if tag != "人工":
                bad.append("选完车站这一段没变成「人工」：%r" % tag)

        if errs:
            bad.append("页面 JS 报错：" + "; ".join(errs))
        br.close()


def main():
    bad = []
    check_api(bad)
    check_line_names(bad)
    check_ui(bad)
    print("截图：", SHOT)
    if bad:
        print("\n失败：")
        for b in bad:
            print("  -", b)
        raise SystemExit(1)
    print("\nOK：线路所能在候选里选到，节点位置正确，库外线路也能选，UI 无报错")


if __name__ == "__main__":
    main()
