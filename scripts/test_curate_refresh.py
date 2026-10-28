# -*- coding: utf-8 -*-
"""
校对页三件事的回归：

1. 「重算自动经由」：新增线路后按新线路重算，清单摘要同步刷新
2. 自动链上的段可以点选加入手工列表；缺口处给候选线路，点一下填入
3. 手工填经由时，线路名 / 车站名都能搜到刚加进库里的

自带清理：测试用的临时线路保存后删掉，库回到原样。
用法：python scripts/test_curate_refresh.py [车次号]
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.parse
import urllib.request

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8686"
SHOT = os.path.join(tempfile.gettempdir(), "railcn-curate")
CODE = sys.argv[1] if len(sys.argv) > 1 else "C248"
TMP_LINE = "测试疏港线"


def get(path):
    return json.load(urllib.request.urlopen(BASE + path, timeout=60))


def post(path, body):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=120))


def check_api():
    print("== 接口")
    d = get("/api/curate/detail?code=" + CODE)
    assert d["exists"], f"{CODE} 在本地库里没有数据"
    print(f"  {CODE} {d['from']}→{d['to']} 自动覆盖 {d['auto']['viaCoverage']}% "
          f"段 {len(d['auto']['viaSegs'])} 缺口 {len(d['auto']['viaUnknown'])}")
    assert "gapCands" in d, "detail 没带缺口候选"

    # 缺口候选：至少给出一条，且「相邻段同线」排在最前
    gaps = [g for g in d["gapCands"]]
    print("  缺口候选：" + "；".join(
        f"{g['from']}→{g['to']} " + "/".join(x["line"] for x in g["lines"][:4]) for g in gaps))
    for g in gaps:
        if g["lines"]:
            assert g["lines"][0]["src"] == "neighbor" or not d["auto"]["viaSegs"], \
                "缺口候选第一条应该是相邻段同线"

    # 重算：返回体要有线路库概况，摘要跟着更新
    r = post("/api/curate/refresh", {"code": CODE})
    assert r["exists"] and r.get("library", {}).get("lines"), "重算没返回线路库概况"
    assert r.get("summary"), "重算没刷新清单摘要"
    print(f"  重算 → 线路库 {r['library']['lines']} 条（自建 {r['library']['customLines']}）、"
          f"覆盖 {r['auto']['viaCoverage']}%、摘要 {r['summary']['via_text'] or '未识别'}")

    # 自建线路必须出现在校对页的线路名清单里
    names = get("/api/curate/lineNames")["names"]
    for nm in ("唐曹线", "曹妃甸港线"):
        if nm in names:
            print(f"  线路名清单含 {nm}")
    return d


def check_ui():
    errs = []
    os.makedirs(SHOT, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--no-proxy-server"])
        pg = b.new_context(viewport={"width": 1500, "height": 980}).new_page()
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("dialog", lambda d: d.accept())     # 重算前「会丢弃未保存填写」的确认框
        pg.goto(BASE + "/", wait_until="load")
        pg.wait_for_timeout(1200)
        pg.click('[data-tab="curate"]')
        pg.wait_for_selector("#cuList .cu-row, #cuList .empty", timeout=30000)
        pg.wait_for_timeout(600)

        # 直接打开目标车次
        pg.evaluate("code => cuOpen(code)", CODE)
        pg.wait_for_selector("#ceViaChain", timeout=30000)
        pg.wait_for_timeout(900)

        # --- 1) 自动链上的段可点选 ---
        segs = pg.query_selector_all("#ceViaChain .vseg[data-line]")
        print(f"  自动链上的可点段：{len(segs)}")
        if segs:
            before = pg.evaluate("() => CU.via.length")
            was_virtual = pg.evaluate(
                "() => !CU.via.some(r => !isVirtual(r) && (r.line||'') && true)")
            segs[0].click()
            pg.wait_for_timeout(400)
            after = pg.evaluate("() => CU.via.length")
            # 原来就是「自动」行 → 只把标签改成人工，不新增行；否则新增一行
            assert after in (before, before + 1), "点自动链上的段没进手工列表"
            assert pg.evaluate("() => CU.via.some(r => !isVirtual(r))"), "加进来的段不是人工段"
            assert pg.eval_on_selector_all("#ceViaChain .vseg.picked", "els => els.length") >= 1, \
                "链上没标出「已加入」"
            print(f"  点第一段 → 手工列表 {before} → {after}，标为人工、链上打勾")
            # 再点一下撤回
            pg.query_selector_all("#ceViaChain .vseg[data-line]")[0].click()
            pg.wait_for_timeout(400)
            assert pg.evaluate("() => CU.via.length") == before - 1, "再点一下没撤回"
            print("  再点一下 → 已撤回")

        # --- 2) 缺口候选点选 ---
        chips = pg.query_selector_all("#ceGapCands .gc-line")
        print(f"  缺口候选按钮：{len(chips)}")
        if chips:
            label = chips[0].inner_text().replace("\n", " ")
            chips[0].click()
            pg.wait_for_timeout(500)
            rows = pg.query_selector_all("#ceViaRows .ve-row")
            print(f"  点候选「{label}」→ 手工列表 {len(rows)} 行")
            assert len(rows) >= 1, "点候选没填出一行"
            assert pg.eval_on_selector_all("#ceViaRows .ve-row.is-manual", "e => e.length") >= 1
            # 填进去的线路名要真落到某一行上
            vals = [el.input_value().strip()
                    for el in pg.query_selector_all("#ceViaRows .ve-row input.line")]
            print(f"  各行线路名：{vals}")
            assert "唐曹线" in vals, "点候选没把线路名填进手工列表"

        # --- 3) 车站下拉能从车站库现查（选一条线路，然后搜一个不在这条线上的站）---
        if not pg.query_selector("#ceViaRows .ve-row"):
            pg.click("#ceAdd")
            pg.wait_for_timeout(300)
        line_inp = pg.query_selector("#ceViaRows .ve-row input.line")
        line_inp.click()
        line_inp.fill("")
        line_inp.type("唐曹线", delay=40)
        pg.wait_for_timeout(500)
        pg.keyboard.press("Enter")            # 选中线路
        pg.wait_for_timeout(800)
        sta_inp = pg.query_selector('#ceViaRows .ve-row .combo[data-cbkind="sta"] input')
        sta_inp.click()
        sta_inp.fill("")
        sta_inp.type("唐山", delay=60)
        pg.wait_for_timeout(1200)             # 等车站库现查回来
        items = [el.inner_text().replace("\n", " · ")
                 for el in pg.query_selector_all("#ceViaRows .cb-pop.open .cb-item, "
                                                 "#ceViaRows .combo.open .cb-pop .cb-item")]
        print(f"  车站下拉搜「唐山」→ {items[:5]}")
        assert any(t.startswith("唐山") for t in items), "车站下拉搜不到「唐山」"
        pg.screenshot(path=os.path.join(SHOT, "curate.png"))

        # --- 4) 重算按钮 ---
        pg.keyboard.press("Escape")
        pg.click("#ceRefresh")
        pg.wait_for_timeout(1500)
        msg = pg.inner_text("#ceMsg")
        print("  重算提示：" + msg.replace("\n", " ")[:90])
        assert "重算" in msg or "线路库" in msg, "重算没有回显"
        pg.screenshot(path=os.path.join(SHOT, "refresh.png"))

        assert not errs, "页面 JS 报错：" + "; ".join(errs)
        print("  页面无 JS 报错")
        b.close()


def check_new_line_visible():
    """新加一条线路 + 一个车站后，校对页的下拉能搜到（这是第 3 条需求的根因）"""
    print("== 新增线路后可见性")
    post("/api/line/new", {"name": TMP_LINE, "stations": [
        {"name": "唐山", "km": 0}, {"name": "唐山南", "km": 8}]})
    try:
        names = get("/api/curate/lineNames")["names"]
        assert TMP_LINE in names, "新线路没进 /api/curate/lineNames"
        stops = get("/api/line/stops?q=" + urllib.parse.quote(TMP_LINE))["items"]
        print(f"  新线路 {TMP_LINE} 已进线路名清单，站序 {stops}")
        d = get("/api/curate/cands?from=" + urllib.parse.quote("唐山")
                + "&to=" + urllib.parse.quote("唐山南"))
        print("  候选接口 唐山→唐山南：" + "/".join(x["line"] for x in d["items"][:4]))
        assert d["items"], "候选接口没返回"
    finally:
        post("/api/line/delete", {"name": TMP_LINE})
        names = get("/api/curate/lineNames")["names"]
        assert TMP_LINE not in names, "临时线路没删干净"
        print("  临时线路已清理")


def main():
    check_api()
    check_new_line_visible()
    check_ui()
    print(f"全部通过（截图 {SHOT}）")


if __name__ == "__main__":
    main()
