"""回归：站序与 OSM 走向互相补全（分段线 ↔ 整条线）。

线名在两套数据里对不齐：
  · 线路库按段命名，OSM 只有整条 —— 成昆线成攀段 / 成昆线元昆段 ↔ 成昆线
  · OSM 有整条几何，线路库里没有这条线 —— 成昆线、京哈高速线

这个脚本盯住四件事：
  1. 分段线借来的几何要**按首末站裁掉两头**，里程和档案对得上（不是把整条画上去）
  2. 裁不出来的时候要如实退回整条并给原因，不许硬裁出一条假走向
  3. 只有 OSM 的线名要能补出站序（资料库同名 → 同前缀分段拼接）
  4. 分段之间真的接不上（京港高速线中间隔着 407 km 的合安、昌赣）要拒绝，不能硬拼
"""
import json
import os
import tempfile
import urllib.parse
import urllib.request

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8686/"
TMP = os.path.join(tempfile.gettempdir(), "railcn-lineseg")
os.makedirs(TMP, exist_ok=True)


def get(path):
    return json.load(urllib.request.urlopen(BASE.rstrip("/") + path, timeout=180))


def q(path, name, **kw):
    args = {"q": name}
    args.update(kw)
    return get(path + "?" + urllib.parse.urlencode(args))


def check_split(bad):
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "webapp"))
    import lineseg

    cases = {"成昆线元昆段": ("成昆线", "元昆"),
             "京哈高速线京沈段": ("京哈高速线", "京沈"),
             "龙龙高速线龙岩至武平段": ("龙龙高速线", "龙岩至武平"),
             "成昆线": None, "京九线": None, "沪昆高速线": None}
    for nm, want in cases.items():
        got = lineseg.split(nm)
        print("  %-24s -> %s" % (nm, got))
        if got != want:
            bad.append("split(%s) = %s，应为 %s" % (nm, got, want))


def check_borrow(bad):
    """方向一：库里只有站序 → 借整条线的几何再裁"""
    want = {"成昆线元昆段": 260, "成昆线成攀段": 734,
            "京哈高速线京沈段": 696, "京包客专线张包段": 468, "吉玉线铜玉段": 48}
    for nm, expect in want.items():
        d = q("/api/line", nm)
        o = d.get("osm") or {}
        clip = o.get("clip") or {}
        got = clip.get("km")
        print("  %-16s 站 %3d 档案 %4s → 裁出 %s km（%s，缺口 %s）"
              % (nm, d["n"], d.get("totalKm"), got, o.get("name"), clip.get("gaps")))
        if not o.get("borrowed"):
            bad.append("%s 没借到整条线的几何" % nm)
        if not o.get("clipped"):
            bad.append("%s 没裁出本段（%s）" % (nm, o.get("why")))
            continue
        if abs((got or 0) - expect) > max(15, expect * 0.06):
            bad.append("%s 裁出 %s km，和档案 %s km 差太多" % (nm, got, expect))
        if not (clip.get("from") and clip.get("to")):
            bad.append("%s 的裁剪区间没写首末站" % nm)

    # 裁不出来时必须如实退回：宁西线的几何断成 7 截，硬连通要横跨 294 km 空白
    d = q("/api/line", "宁西线西合段")
    o = d.get("osm") or {}
    print("  宁西线西合段 站 %3d → caught clipped=%s why=%s"
          % (d["n"], o.get("clipped"), (o.get("why") or "")[:38]))
    if o.get("clipped"):
        bad.append("宁西线西合段几何断裂，不该裁出一条假走向")
    if not o.get("why"):
        bad.append("宁西线西合段退整条时没说明原因")
    if not (o.get("nPart") or 0) > 1:
        bad.append("宁西线西合段退回整条后应保留原始分段数")


def check_compose(bad):
    """方向二：只有 OSM 的线名 → 补站序"""
    d = q("/api/line", "成昆线")
    print("  成昆线 %d 站 %s km（站序 %s / %s）"
          % (d["n"], d.get("totalKm"), d.get("stopsFrom"), d.get("stopsSource")))
    if d["n"] < 100:
        bad.append("成昆线只补到 %d 站（资料库应给 133 站）" % d["n"])
    names = [s["name"] for s in d["stations"]]
    if names[0] != "成都" or names[-1] != "昆明":
        bad.append("成昆线首末站不对：%s → %s" % (names[0], names[-1]))
    if d.get("stopsFrom") != "catalog":
        bad.append("成昆线的站序来源应标 catalog，实际 %r" % d.get("stopsFrom"))
    o = d.get("osm") or {}
    if o.get("name") != "成昆线" or o.get("borrowed"):
        bad.append("成昆线的走向应是 OSM 自己的整条几何，实际 %r" % o.get("name"))
    if (o.get("km") or 0) < 900:
        bad.append("成昆线的几何里程异常：%r" % o.get("km"))

    d = q("/api/line", "京哈高速线")
    parts = d.get("stopsParts") or []
    print("  京哈高速线 %d 站（拼自 %s）" % (d["n"], " + ".join(parts)))
    if d["n"] < 30:
        bad.append("京哈高速线只拼到 %d 站" % d["n"])
    if len(parts) != 2:
        bad.append("京哈高速线应由京沈段 + 沈哈段两段拼成，实际 %r" % parts)

    # 京港高速线：商合段止于合肥北城、赣深段起于赣州西，中间 407 km 没有站序
    st = q("/api/line/stops", "京港高速线")
    print("  京港高速线站序接口：found=%s count=%s" % (st.get("found"), st.get("count")))
    if st.get("found") and st.get("count", 0) < 25:
        bad.append("京港高速线两段隔着 407 km，被硬拼成了 %d 站" % st.get("count"))


def check_stops_api(bad):
    for nm, least in (("成昆线", 100), ("京哈高速线", 30), ("成昆线元昆段", 20)):
        s = q("/api/line/stops", nm)
        print("  %-14s %3d 站（from=%s）" % (nm, s.get("count", 0), s.get("from")))
        if s.get("count", 0) < least:
            bad.append("%s 的站序只有 %d 站" % (nm, s.get("count")))

    g = q("/api/line/osm", "成昆线元昆段")
    pts = sum(len(p) for p in (g.get("parts") or []))
    print("  元昆段几何：%s km，%d 段 %d 点，clipped=%s"
          % (g.get("km"), len(g.get("parts") or []), pts, g.get("clipped")))
    if not (240 <= (g.get("km") or 0) <= 275):
        bad.append("元昆段几何里程 %r 不在 240–275" % g.get("km"))
    if pts < 100:
        bad.append("元昆段几何只有 %d 个点，像是没裁出来" % pts)

    # 列表里要标出「这条 OSM 线能补站序」
    lst = q("/api/line/osm/list", "成昆")
    hit = [x for x in lst["items"] if x["name"] == "成昆线"]
    print("  OSM 列表里的成昆线：stopsN=%s" % (hit[0].get("stopsN") if hit else None))
    if not hit or not hit[0].get("stopsN"):
        bad.append("OSM 列表没标出成昆线能补站序")


def check_ui(bad):
    with sync_playwright() as p:
        br = p.chromium.launch(args=["--no-proxy-server"])
        pg = br.new_context(viewport={"width": 1680, "height": 1000},
                            device_scale_factor=1).new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(BASE, wait_until="load")
        pg.wait_for_timeout(1200)
        pg.click('[data-tab="net"]')
        pg.wait_for_timeout(500)
        pg.evaluate("localStorage.setItem('railcn.lineGeom','1')")   # 默认按真实走向画

        # 元昆段：站表来自线路库，走向借整条成昆线再裁
        pg.evaluate("n => showLine(n)", "成昆线元昆段")
        pg.wait_for_selector("#lnHead .l1", timeout=30000)
        pg.wait_for_timeout(2200)
        head = pg.inner_text("#lnHead")
        n = pg.eval_on_selector_all("#lnStops .stop", "els => els.length")
        osm = pg.inner_text("#lnInfo .ln-osm") if pg.query_selector("#lnInfo .ln-osm") else ""
        g = pg.evaluate("() => LN.gdata ? {ask: LN.gdata.ask, km: LN.gdata.km} : null")
        print("  元昆段：%d 站，几何 ask=%s km=%s" % (n, (g or {}).get("ask"), (g or {}).get("km")))
        if n != 31:
            bad.append("元昆段页面上是 %d 站，应为 31" % n)
        if "借 成昆线 裁出本段" not in osm.replace("\n", " "):
            bad.append("元昆段没标出「借 成昆线 裁出本段」：%s" % osm[:60])
        if not g or g.get("ask") != "成昆线元昆段":
            bad.append("元昆段画的是整条几何（ask=%r），没走裁剪" % (g or {}).get("ask"))
        if g and (g.get("km") or 0) > 400:
            bad.append("元昆段几何里程 %s，说明画了整条线" % g.get("km"))
        pg.query_selector("#netDetail").screenshot(path=os.path.join(TMP, "yuankun.png"))

        # 成昆线：站序从资料库补，走向用 OSM 自己的整条几何
        pg.evaluate("n => showLine(n)", "成昆线")
        pg.wait_for_selector("#lnHead .l1", timeout=30000)
        pg.wait_for_timeout(2200)
        head = pg.inner_text("#lnHead")
        n = pg.eval_on_selector_all("#lnStops .stop", "els => els.length")
        print("  成昆线：%d 站" % n)
        if n < 100:
            bad.append("成昆线页面上只有 %d 站" % n)
        if "站序来自资料库" not in head:
            bad.append("成昆线没标出「站序来自资料库」")
        pg.query_selector("#netDetail").screenshot(path=os.path.join(TMP, "chengkun.png"))

        if errs:
            bad.append("页面 JS 报错：" + "; ".join(errs[:3]))
        br.close()


def main():
    bad = []
    print("1) 线名拆解")
    check_split(bad)
    print("2) 借整条线几何并裁出本段")
    check_borrow(bad)
    print("3) 只有 OSM 的线名补站序")
    check_compose(bad)
    print("4) 接口")
    check_stops_api(bad)
    print("5) 界面")
    check_ui(bad)
    print("截图：", TMP)
    if bad:
        print("\n失败：")
        for b in bad:
            print("  -", b)
        raise SystemExit(1)
    print("\nOK：分段线拿到走向、整线补到站序，裁不出的如实退回并说明")


if __name__ == "__main__":
    main()
