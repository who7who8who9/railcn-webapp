"""回归：时间条上的停站轨（站名 + 时刻，拥挤时自动分层）。"""
import os
import sys
import tempfile

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8686/"
SHOT = os.path.join(tempfile.gettempdir(), "railcn-tl-stops")

PROBE = """() => {
  const el = document.querySelector('#tlStops');
  const bs = [...el.querySelectorAll('.tl-st')];
  const boxes = bs.filter(b => b.querySelector('.nm'))
    .map(b => {
      const n = b.querySelector('.nm').getBoundingClientRect();
      return { nm: b.querySelector('.nm').textContent, l: n.left, r: n.right, t: n.top, b: n.bottom };
    });
  let overlap = 0;
  for (let i = 0; i < boxes.length; i++)
    for (let j = i + 1; j < boxes.length; j++)
      if (boxes[i].r > boxes[j].l + 0.5 && boxes[j].r > boxes[i].l + 0.5
          && boxes[i].b > boxes[j].t + 0.5 && boxes[j].b > boxes[i].t + 0.5) overlap++;
  const r = el.getBoundingClientRect();
  const dock = document.querySelector('#dock').getBoundingClientRect();
  return {
    stops: bs.length, labeled: boxes.length, overlap,
    elH: Math.round(r.height), elW: Math.round(r.width),
    dockH: Math.round(dock.height),
    first: boxes[0] && boxes[0].nm, last: boxes[boxes.length - 1] && boxes[boxes.length - 1].nm,
    texts: boxes.map(b => b.nm).join(',')
  };
}"""


def main():
    codes = sys.argv[1:] or ["G1", "G1033", "D2290|2026-11-03"]
    os.makedirs(SHOT, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--no-proxy-server"])
        ctx = b.new_context(viewport={"width": 1440, "height": 900})
        pg = ctx.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(BASE, wait_until="load")
        pg.wait_for_timeout(1800)          # 等页面自己那一次初始查询落定
        for spec in codes:
            code, _, date = spec.partition("|")
            pg.fill("#q", code)
            if date:
                pg.fill("#date", date)
            with pg.expect_response(
                    lambda r: "/api/route" in r.url and f"code={code}" in r.url,
                    timeout=60000) as ri:
                pg.click("#btnSearch")
            pg.click("#btnSearch")
            pg.wait_for_selector("#dock:not(.hidden)", timeout=40000)
            pg.wait_for_timeout(1200)
            got = pg.inner_text("#hudRoute").strip()
            print(f"    [页面] {pg.inner_text('#hudCode').strip()} {got}")
            info = pg.evaluate(PROBE)
            print(f"--- {code} {date} ---")
            for k, v in info.items():
                print(f"  {k}: {v}")
            pg.locator("#dock").screenshot(path=os.path.join(SHOT, f"{code}.png"))

            # 点第一个有标签的站，看是否跳过去
            btn = pg.locator("#tlStops .tl-st .nm").first
            if btn.count():
                want = btn.inner_text()
                try:
                    btn.click(timeout=5000)
                    pg.wait_for_timeout(400)
                    print(f"  点击 {want} → hud: {pg.inner_text('#hudState')}")
                except Exception as e:
                    print(f"  点击 {want} 失败: {type(e).__name__}")

        # 开关按钮
        pg.click("#btnStops")
        pg.wait_for_timeout(200)
        off = pg.evaluate("() => document.querySelector('#tlStops').getBoundingClientRect().height")
        print(f"--- 关闭停站轨后高度: {off}")
        pg.click("#btnStops")
        pg.wait_for_timeout(200)
        on = pg.evaluate("() => document.querySelector('#dock').getBoundingClientRect().height")
        print(f"--- 重新打开后 dock 高: {on}")
        print("页面错误:", errs or "无")
        b.close()


if __name__ == "__main__":
    main()
