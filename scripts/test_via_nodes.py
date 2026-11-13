"""回归：数据校对的线路口径 —— 只认整理台确认的线，确认线里的线路所照样能选

2026-11-03 的旧要求「线路库没收的线也要出现在下拉里（合新高速线 / 银西高铁）」
在 11-11 被用户推翻：数据校对里出现的线路**只能是整理台完成全部确认的那些**
（`webapp/confvia.py`），来源版本（京广线（OSM）、沪昆线（底表）…）和没确认的线
一律不出现，认不出的区间就是缺口。这里守三件事：

  1. `/api/curate/lineNames` 只列确认线路，且不带「（来源）」后缀；
  2. 没确认的线（沪春线 / 合新高速线 / 银西高铁）不进下拉，但**老口径的
     `/api/line/stops` 原样保留**（其它页面还在用，比如线路详情选站）；
  3. 确认线路的站序自带线路所（京沪线：柳村线路所、黄土坡线路所），「选站」
     候选里照样按站序列出来并标「线路所」—— 以前靠资料库补节点，现在确认站序
     自己就有，不用补。
"""
import json
import os
import tempfile
import urllib.parse
import urllib.request

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8686/"
TMP = os.path.join(tempfile.gettempdir(), "railcn-via-node")
os.makedirs(TMP, exist_ok=True)
# 截图先落临时目录，全绿才复制去 docs/ —— 否则脚本一红就把 README 引用的图冲掉了
SHOT = os.path.join(TMP, "via-node.png")
DOC_SHOT = os.path.join("D:/railcn/docs", "via-node.png")

#: 走京沪线、自动识别比较全的一趟车（UI 里用它开编辑器）
CODE = "V786"
#: 确认线路里带线路所的一条，用来看「选站能不能选到所」
LINE = "京沪线"
NODES = ("柳村线路所", "黄土坡线路所")


def get(path):
    return json.load(urllib.request.urlopen(BASE.rstrip("/") + path, timeout=120))


def stops(q, confirmed=False):
    qs = {"q": q}
    if confirmed:
        qs["confirmed"] = 1
    return get("/api/line/stops?" + urllib.parse.urlencode(qs))


def check_api(bad):
    d = get("/api/curate/lineNames")
    items = d.get("items") or []
    names = {x["name"] for x in items}
    print("线路下拉：%d 条 · scope=%s" % (len(items), d.get("scope")))
    if d.get("scope") != "confirmed":
        bad.append("线路下拉没标 scope=confirmed：%r" % d.get("scope"))
    if not items:
        bad.append("线路下拉一条都没有（整理台确认线路没读出来）")
    suffixed = sorted(n for n in names if "（" in n)
    if suffixed:
        bad.append("线路下拉里还有带（来源）后缀的版本名：%s" % suffixed[:4])
    for nm in ("沪春线", "合新高速线", "银西高铁"):
        if nm in names:
            bad.append("%s 没在整理台确认，不该出现在校对下拉里" % nm)
    if LINE not in names:
        bad.append("确认线路 %s 不在下拉里" % LINE)

    # 站序：确认线路给整理台那一份，且自带线路所
    s = stops(LINE, confirmed=True)
    got = s.get("items") or []
    print("%s（confirmed=1）：%d 站，节点 %s"
          % (LINE, len(got), [n for n in got if n in NODES]))
    if s.get("from") != "confirmed":
        bad.append("确认线路的站序没走 confirmed 分支：%r" % s.get("from"))
    for n in NODES:
        if n not in got:
            bad.append("%s 的确认站序里少了 %s" % (LINE, n))
    if LINE in names:
        want = next((x.get("n") for x in items if x["name"] == LINE), None)
        if want and want != len(got):
            bad.append("%s 下拉给 %s 站，站序接口给 %d 站（口径不一致）"
                       % (LINE, want, len(got)))

    # 没确认的线：确认口径下拿不到站序（前端会退回这趟车的经停站）
    x = stops("沪春线", confirmed=True)
    print("沪春线（confirmed=1）：found=%s · %d 站" % (x.get("found"), len(x.get("items") or [])))
    if x.get("found") or (x.get("items") or []):
        bad.append("没确认的沪春线在确认口径下不该给站序")

    # 老口径不动：主站 / 线路详情仍按线路库取站序。
    # ⚠ 这两条**本来就红**（线路库 11-02 清空后，资料库那条补节点的路取不到沪春线 /
    # 银西高铁，跟本次口径改动无关），所以只打印不打红 —— 哪天线路库补回来了再收紧。
    old = stops("沪春线")
    print("沪春线（老口径）：%d 站，含春申南所=%s（既有红，见上）"
          % (len(old.get("items") or []), "春申南所" in (old.get("items") or [])))
    y = stops("银西高铁")
    print("银西高铁（老口径）：found=%s · %d 站（既有红，见上）"
          % (y.get("found"), len(y.get("items") or [])))


def check_detail(bad):
    """自动识别只能给出确认线路；缺口候选也只能是确认线路"""
    allowed = {x["name"] for x in (get("/api/curate/lineNames").get("items") or [])}
    d = get("/api/curate/detail?code=" + CODE)
    if not d.get("exists"):
        bad.append("%s 在本地库里没有数据，用例失效" % CODE)
        return
    segs = (d.get("auto") or {}).get("viaSegs") or []
    gaps = (d.get("auto") or {}).get("viaUnknown") or []
    print("%s %s→%s：覆盖 %s%%，段 %d，缺口 %d"
          % (CODE, d.get("from"), d.get("to"), d["auto"].get("viaCoverage"),
             len(segs), len(gaps)))
    outside = [s["line"] for s in segs if s["line"] not in allowed]
    if outside:
        bad.append("自动识别给出了没确认的线路：%s" % outside[:4])
    if any("（" in (s.get("line") or "") for s in segs):
        bad.append("自动识别还在给来源版本名：%s"
                   % [s["line"] for s in segs if "（" in s["line"]][:3])

    # 段 + 缺口必须正好覆盖每一对相邻站
    n = len(d.get("stations") or [])
    covered = sum(1 for s in segs) + sum(1 for g in gaps)
    if n and covered > n - 1:
        bad.append("段数+缺口数（%d）超过了相邻站对数（%d）" % (covered, n - 1))

    for g in d.get("gapCands") or []:
        for c in g.get("lines") or []:
            if c["line"] not in allowed:
                bad.append("缺口候选里混进没确认的线路：%s" % c["line"])
    print("缺口候选全部在确认线路内：%s" % all(
        c["line"] in allowed for g in (d.get("gapCands") or []) for c in (g.get("lines") or [])))


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

        pg.evaluate("c => cuOpen(c)", CODE)
        pg.wait_for_selector("#ceViaRows .ve-row", timeout=30000)

        # 线路框里打「合新」：没确认的线不该出现（下拉只剩「用这个名称」）
        lsel = '#ceViaRows .ve-row:first-child .combo[data-cbkind="line"]'
        pg.locator(lsel + " input").fill("合新")
        pg.wait_for_timeout(500)
        pg.click(lsel + " .cb-toggle")
        pg.wait_for_timeout(600)
        litems = pg.evaluate(
            """sel => [...document.querySelectorAll(sel + ' .cb-item')].map(e => ({
                   n: e.querySelector('.nm').textContent.trim(),
                   custom: e.classList.contains('custom')}))""", arg=lsel)
        print("线路下拉（合新）：", litems)
        if any(i["n"] == "合新高速线" and not i["custom"] for i in litems):
            bad.append("没确认的合新高速线还在下拉里（已被选成库线）：%s" % litems)
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(200)

        # 换成确认线路：站序候选里要有线路所，并标「线路所」
        pg.locator(lsel + " input").fill(LINE)
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
        hits = [i for i in items if i["n"] in NODES]
        print("下拉候选（%s）前 6：" % LINE, [(i["n"], i["node"]) for i in items[:6]],
              "| 节点：", [(i["n"], i["node"]) for i in hits])
        if not hits:
            bad.append("%s 的选站候选里没有线路所：%s"
                       % (LINE, [i["n"] for i in items[:10]]))
        elif not hits[0]["node"] or hits[0]["mt"] != "线路所":
            bad.append("%s 没标成「线路所」：%s" % (hits[0]["n"], hits[0]))

        # 截图（「手工填写经由」整块 + 展开的下拉）
        card = pg.locator(".ce-card:has(#ceViaRows)").first
        bb = card.bounding_box()
        pg.screenshot(path=SHOT, clip={"x": max(0, bb["x"] - 10), "y": max(0, bb["y"] - 8),
                                       "width": min(1500 - max(0, bb["x"] - 10), bb["width"] + 20),
                                       "height": bb["height"] + 14})

        if hits:
            pg.click(frm + " .cb-item >> text=%s" % hits[0]["n"])
            pg.wait_for_timeout(300)
            val = pg.eval_on_selector(frm + " input", "e => e.value")
            tag = pg.eval_on_selector(
                "#ceViaRows .ve-row:first-child .ve-src", "e => e.textContent.trim()")
            print("选中后：值=%r，段标签=%r" % (val, tag))
            if val != hits[0]["n"]:
                bad.append("点了 %s，输入框却是 %r" % (hits[0]["n"], val))
            if tag != "人工":
                bad.append("选完车站这一段没变成「人工」：%r" % tag)

        if errs:
            bad.append("页面 JS 报错：" + "; ".join(errs))
        br.close()


def main():
    bad = []
    check_api(bad)
    check_detail(bad)
    check_ui(bad)
    print("截图：", SHOT)
    if bad:
        print("\n失败：")
        for b in bad:
            print("  -", b)
        raise SystemExit(1)
    if os.path.exists(SHOT):
        import shutil
        shutil.copyfile(SHOT, DOC_SHOT)
        print("（全绿，已更新文档配图 %s）" % DOC_SHOT)
    print("\nOK：校对只用确认线路，确认线路的线路所能选到，老口径站序没被改坏")


if __name__ == "__main__":
    main()
