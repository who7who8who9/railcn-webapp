# -*- coding: utf-8 -*-
"""线路里程校正：复现并验证「填 - / 指定累计」与「坐标估算」两件事。

用法：
    python scripts/test_line_km.py           # 累计模式：曹庄 -、杨柳青 163
    python scripts/test_line_km.py --seg     # 段距模式：标 - 后点 ≈ 估算值

**会写真实库，所以开场先备份京沪线的人工站序、跑完原样还原**（以前不还原，
每跑一次就把当次界面上那份站序（常常是 OSM 的 166 站）落成「人工站序」，
京沪线的默认视图就被测试改掉了）。
"""
import json
import os
import sys
import tempfile
import urllib.parse
import urllib.request

from playwright.sync_api import sync_playwright

MODE = "seg" if "--seg" in sys.argv else "cum"
URL = "http://127.0.0.1:8686/"
LINE = "京沪线"
# 调试截图放临时目录，不要落进仓库的 docs/
SHOT = os.path.join(tempfile.gettempdir(), "railcn-line-km")


def _api(path, body=None):
    url = URL.rstrip("/") + path
    req = urllib.request.Request(
        url, data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
        method="POST" if body is not None else "GET")
    return json.load(urllib.request.urlopen(req, timeout=180))


def snap_override():
    """跑之前这条线有没有人工站序；有就整份记下来"""
    try:
        d = _api("/api/line?q=" + urllib.parse.quote(LINE))
    except Exception:                        # noqa: BLE001
        return None
    ov = d.get("override") or {}
    if not ov:
        return None
    return {"stops": [{"name": s["name"], "km": s.get("km")} for s in d.get("stations") or []],
            "note": ov.get("note") or ""}


def restore_override(snap):
    """还原：本来没有 → 清掉这次写进去的；本来有 → 按备份写回"""
    try:
        if snap:
            _api("/api/line/stations", {"name": LINE, "stations": snap["stops"],
                                       "note": snap["note"]})
            print("  已还原京沪线原来的人工站序（%d 站）" % len(snap["stops"]))
        else:
            _api("/api/line/stations/clear", {"name": LINE})
            print("  已清掉本次测试写下的京沪线人工站序")
    except Exception as e:                   # noqa: BLE001
        print("  ⚠ 还原失败：%s" % e)


def rows(pg, tag, names="曹庄|杨柳青|天津西|周李庄"):
    out = pg.evaluate("""(pat) => [...document.querySelectorAll('#lnSegs .seg-row')]
        .filter(r => new RegExp(pat).test(r.querySelector('.od').textContent))
        .map(r => ({i: r.dataset.i, cls: r.className.trim(),
                    text: r.querySelector('.od').textContent.trim().replace(/\\s+/g, ''),
                    val: r.querySelector('input.km').value,
                    ph: r.querySelector('input.km').placeholder,
                    auto: (r.querySelector('.auto').textContent || '').trim()}))""", names)
    print(f"--- {tag}")
    for r in out:
        print("   ", r)
    return out


def open_line(pg):
    pg.goto(URL, wait_until="load")
    pg.click('.tab[data-tab="net"]')
    pg.click('.chipsel[data-kind="line"]')
    pg.fill("#netQ", "京沪线")
    pg.click("#btnNet")
    pg.wait_for_timeout(1200)
    pg.evaluate("""() => { const r=[...document.querySelectorAll('#netList > *')]
        .find(e => e.textContent.includes('京沪线')); r && r.click(); }""")
    pg.wait_for_selector("#lnEditBtn", timeout=30000)
    pg.click("#lnEditBtn")
    pg.wait_for_timeout(600)


def focus_row(pg, name):
    idx = pg.evaluate("""(nm) => {
        const r = [...document.querySelectorAll('#lnSegs .seg-row')]
          .find(x => x.querySelector('.od').textContent.trim().startsWith(nm));
        if (!r) return null;
        r.querySelector('input.km').focus();
        return r.dataset.i;
    }""", name)
    return idx


def main():
    snap = snap_override()
    try:
        run()
    finally:
        restore_override(snap)


def run():
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1440, "height": 900})
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        open_line(pg)

        # 先恢复档案，保证起点干净
        pg.click("#lnResetAll")
        pg.wait_for_timeout(1200)
        if pg.query_selector("#lnMode") is None:      # 重置后编辑器可能收起
            pg.click("#lnEditBtn")
            pg.wait_for_timeout(600)

        if MODE == "cum":
            pg.click('#lnMode button[data-mode="cum"]')
            pg.wait_for_timeout(400)
        rows(pg, f"初始（{MODE}）")

        i1 = focus_row(pg, "曹庄")
        print("曹庄 行 =", i1)
        pg.keyboard.type("-")
        pg.wait_for_timeout(200)

        i2 = focus_row(pg, "杨柳青")
        print("杨柳青 行 =", i2)
        pg.keyboard.type("163")
        pg.wait_for_timeout(300)
        rows(pg, "填完之后")
        print("提示:", (pg.text_content("#lnMsg") or "").strip())
        print("保存按钮 bad:", pg.evaluate("() => document.querySelector('#lnSave').classList.contains('bad')"))

        if MODE == "seg":
            # 曹庄→杨柳青 现在标了 -，行尾应有 ≈ 估算值，点一下填入
            est = pg.evaluate("""() => {
                const r = [...document.querySelectorAll('#lnSegs .seg-row')]
                  .find(x => x.querySelector('.od').textContent.trim().startsWith('曹庄'));
                const a = r.querySelector('.auto');
                return {txt: a.textContent.trim(), cls: a.className, title: a.title};
            }""")
            print("估算:", est)
            pg.evaluate("""() => {
                const r = [...document.querySelectorAll('#lnSegs .seg-row')]
                  .find(x => x.querySelector('.od').textContent.trim().startsWith('曹庄'));
                r.querySelector('.auto').click();
            }""")
            pg.wait_for_timeout(300)
            rows(pg, "点估算之后")
            print("提示:", (pg.text_content("#lnMsg") or "").strip())

        pg.click("#lnSave")
        pg.wait_for_timeout(2500)
        print("保存提示:", (pg.text_content("#lnMsg") or "").strip())
        rows(pg, "保存之后")
        print("侧栏站点列表:", pg.evaluate("""() => [...document.querySelectorAll('#lnStops .stop')]
            .filter(li => /曹庄|杨柳青|周李庄/.test(li.querySelector('.name').textContent))
            .map(li => li.querySelector('.name').textContent.trim() + ' = ' + li.querySelector('.km').textContent.trim())"""))
        os.makedirs(SHOT, exist_ok=True)
        shot = os.path.join(SHOT, f"{MODE}.png")
        pg.screenshot(path=shot, clip={"x": 0, "y": 58, "width": 620, "height": 820})
        print("截图:", shot)
        print("err:", errs)
        b.close()


main()
