# -*- coding: utf-8 -*-
"""
回归：数据校对 ① 线路下拉按发车站筛 ② 页面内「线路走向 / 停站」地图

2026-11-12 用户提两点，并特别补了一句「线路和车站之后都会增加哦」——
所以这两处都**不能写死名单 / 一次性下发全库索引**：

  · 服务端只把「本趟车这些站 → 含该站的确认线路」这一小份给前端
    （`/api/curate/linesForStops`），候选随 `confvia.index()` 动态重建；
    整理台确认一条新线 / 补一批站，重开编辑器就自动出现。
  · 地图一次只取一条确认线路的几何 + 带坐标站序（`/api/curate/lineMap`），
    有腾讯地图 Key 用 TMap，没 Key 用 Canvas 自绘（只画网格 / 线路 / 站点，
    不画国界、不搬瓦片）；顶部按线名搜索也只让服务端回 20 条。

跑法：python scripts/test_curate_linefilter_map.py   （先起 server --port 8686）
只读，不写库；Playwright 部分只操作编辑器 UI，不点保存。
"""
from __future__ import annotations

import io
import json
import math
import sys
import time
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8686"
ok = fail = 0
bad: list = []


def check(label: str, cond: bool, val="") -> None:
    global ok, fail
    if cond:
        ok += 1
        print("  ok   %s%s" % (label, ("  " + str(val))[:80] if val != "" else ""))
    else:
        fail += 1
        bad.append(label)
        print("  失败 %s%s" % (label, ("  " + str(val))[:80] if val != "" else ""))


def get(path: str, **kw) -> dict:
    qs = ("?" + urllib.parse.urlencode(kw)) if kw else ""
    with urllib.request.urlopen(BASE + path + qs, timeout=180) as r:
        return json.loads(r.read().decode("utf-8"))


def src(rel: str) -> str:
    return io.open(rel, encoding="utf-8").read()


def main() -> int:
    sys.path.insert(0, "webapp")
    import confvia

    # ------------------------------------------------------------------
    print("== 1) 静态护栏：动态口径 / 合规")
    cf = src("webapp/confvia.py")
    srv = src("webapp/server.py")
    app = src("webapp/static/app.js")
    html = src("webapp/static/index.html")
    css = src("webapp/static/style.css")
    osmjs = src("webapp/static/osmedit.js")

    check("confvia 有站点反查索引", "def station_index(" in cf)
    check("confvia 有 lines_for 按站查", "def lines_for(" in cf)
    check("索引带缓存且按 index() 动态重建",
          "_SIDX" in cf and "for nm, ent in idx.items()" in cf and "invalidate()" in cf)
    for key in ('if path == "/api/curate/linesForStops"',
                'if path == "/api/curate/lineMap"',
                "confvia.lines_for("):
        check("server 有 %s" % key[:42], key in srv)
    seg = srv[srv.index('if path == "/api/curate/lineNames"'):]
    seg = seg[:seg.index("if path ==", 10)]
    check("lineNames 不下发全库 staIndex", "staIndex" not in seg, "segment %d chars" % len(seg))
    check("lineNames 仍只列 confirmed.recent()", "confirmed.recent()" in srv)
    check("lineNames 支持 sta / q / limit 按需过滤",
          all(k in seg for k in ('one("sta")', 'one("q")', 'one("limit")')))
    check("lineMap 几何走 lineosm 且只认确认线路",
          "lineosm.geom(nm, confirmed_only=True)" in srv and "confvia.names()" in srv)
    check("lineMap 站序走 confirmed.detail_payload", "confirmed.detail_payload(nm)" in srv)
    check("lineMap 抽稀上限 2000 点", "2000" in srv)
    check("lineMap 未确认线给 200+found=false，不报 404",
          '"found": False' in srv and "scope" in srv)

    for key in ("cuLoadLineScope", "cuLineScope", "cuStaKey", "cuFetchStaLines",
                "cuMapShow", "cuMapCanvasDraw", "cuMapTMapDraw", "cuMapReset",
                "cuMapEnsure", "cuMapWire"):
        check("app.js 有 %s" % key, ("function %s(" % key) in app)
    check("线路下拉走按发车站过滤", "cbLineList(v, box)" in app and "cuLineScope(box)" in app)
    check("cuOpen 每次重取候选（新增线路自动出现）",
          "await cuLoadLineScope();" in app)
    check("候选按需单查兜底", "cuFetchStaLines" in app)
    check("线路多了也限制 DOM 条数", "all.slice(0, 300)" in app)
    check("地图搜索只回 20 条", "limit: 20" in app)
    check("地图 Key 缺失时降级 Canvas", "cuMapBackend" in app and "window.TMap" in app)
    check("Canvas 示意不画国界 / 不搬瓦片",
          "不画国界" in app and "Leaflet" not in app and "tile.openstreetmap" not in app)
    check("TMap 坐标统一过 GCJ", "GCJ(lon, lat)" in app)
    check("index.html 有地图面板", all(k in html for k in
          ("cuMapPane", "cuMapCanvas", "cuTMap", "cuMapQ", "cuMapFit", "cuMapSugg")))
    check("style.css 有地图布局", ".cu-map-pane" in css and ".curate-stage:not(.with-map)" in css)
    check("走向编辑器支持 ?line= 直达", "get('line')" in osmjs)

    # ------------------------------------------------------------------
    print("== 2) lineNames：按发车站 / 线名按需过滤")
    idx = confvia.index()
    allnames = set(idx)
    a = get("/api/curate/lineNames")
    check("全量 scope=confirmed", a.get("scope") == "confirmed", a.get("scope"))
    check("全量不带全库索引", "staIndex" not in a, list(a.keys()))
    check("全量只含确认线路", set(x["name"] for x in a["items"]) <= allnames,
          "%d 条" % a["count"])
    sidx = confvia.station_index()
    check("反查索引的值都是确认线路",
          all(n in allnames for v in sidx.values() for n in v), len(sidx))
    check("反查索引键数 = 各线 pos 键并集",
          set(sidx) == set(k for d in idx.values() for k in d["pos"]))

    for sta in ("北京南", "石家庄", "获鹿所", "中卫"):
        d = get("/api/curate/lineNames", sta=sta)
        want = set(sidx.get(confvia._key(sta)) or [])
        check("?sta=%s 只回含该站的线" % sta,
              set(x["name"] for x in d["items"]) == want and d.get("sta") == sta,
              "%d 条 %s" % (d["count"], sorted(want)[:5]))
        check("?sta=%s 不混入未确认线" % sta,
              all(x["name"] in allnames for x in d["items"]))
    d0 = get("/api/curate/lineNames", sta="__不存在的站ZZZ__")
    check("不存在的站给 0 条", d0["count"] == 0, d0["count"])
    dq = get("/api/curate/lineNames", q="兰新", limit=3)
    check("?q= + limit 生效", dq["count"] <= 3 and all("兰新" in x["name"] for x in dq["items"]),
          [x["name"] for x in dq["items"]])

    # ------------------------------------------------------------------
    print("== 3) linesForStops：规模随车次站数，新增线路自动出现")
    stas = ["北京南", "北京南站", "石家庄", "吐鲁番", "__新站未录入__"]
    r = get("/api/curate/linesForStops", stas="|".join(stas))
    m = r.get("staMap") or {}
    check("返回映射覆盖每个传入站", set(m) == set(stas), sorted(m))
    check("「北京南站」与「北京南」同候选", m.get("北京南站") == m.get("北京南"),
          m.get("北京南"))
    check("未录站给空数组", m.get("__新站未录入__") == [])
    check("候选都是确认线路", all(n in allnames for v in m.values() for n in v))
    check("scope=confirmed", r.get("scope") == "confirmed")

    # 模拟整理台新确认一条线、新补一个站：只改内存缓存，不碰库
    saved = dict(confvia._CACHE)
    try:
        fake = {"__测试新线__": {"pos": {"__将来新站__": 0, "北京南": 1},
                                "stops": ["__将来新站__", "北京南"],
                                "km": [0, 10], "n": 2, "totalKm": 10}}
        confvia._CACHE["data"] = fake
        confvia._CACHE["ts"] = time.time() + 100
        confvia._SIDX["data"] = None
        confvia._SIDX["ts"] = -1.0
        got = confvia.lines_for(["__将来新站__", "北京南"])
        check("新增线 / 新站后候选立刻出现（不写库）",
              got.get("__将来新站__") == ["__测试新线__"]
              and "__测试新线__" in got.get("北京南", []), got)
    finally:
        confvia._CACHE.update(saved)
        confvia.invalidate()
    check("恢复后索引仍是真实确认线路", set(confvia.index()) == allnames)

    # ------------------------------------------------------------------
    print("== 4) lineMap：几何 + 带坐标站序")
    g = get("/api/curate/lineMap", name="京沪线")
    check("found / scope", g.get("found") is True and g.get("scope") == "confirmed")
    check("parts 是多段且每段 ≥2 点", g["nPart"] >= 1
          and all(len(p) >= 2 for p in g["parts"]), "%d 段" % g["nPart"])
    check("抽稀后点数 ≤2000", g["nPt"] <= 2000, g["nPt"])
    check("站点带 WGS-84 坐标", g["nStop"] > 0
          and all(-90 < s["lat"] < 90 and -180 < s["lon"] < 180 for s in g["stations"]),
          g["nStop"])
    st = get("/api/line/stops", q="京沪线", confirmed=1)
    check("站序与确认站序一致", [s["name"] for s in g["stations"]][:5] == (st.get("items") or [])[:5],
          [s["name"] for s in g["stations"]][:5])
    # 抽稀不能改变线路范围
    import lineosm
    raw = lineosm.geom("京沪线", confirmed_only=True) or {}
    rp = [q for part in (raw.get("parts") or []) for q in part]
    gp = [q for part in g["parts"] for q in part]
    for axis, nm in ((0, "经度"), (1, "纬度")):
        a0, a1 = min(q[axis] for q in rp), max(q[axis] for q in rp)
        b0, b1 = min(q[axis] for q in gp), max(q[axis] for q in gp)
        check("抽稀基本不改变%s四至" % nm, abs(a0 - b0) < 0.05 and abs(a1 - b1) < 0.05,
              (round(a0, 3), round(a1, 3), round(b0, 3), round(b1, 3)))
    bad_line = get("/api/curate/lineMap", name="__测试不存在的线ZZZ__")
    check("未确认 / 不存在的线 200 + found=false",
          bad_line.get("found") is False and bad_line.get("parts") == []
          and bad_line.get("scope") == "confirmed", bad_line.get("reason"))
    g2 = get("/api/curate/lineMap", name="京广线")
    check("另一条线也独立可取", g2.get("found") and g2["nPart"] >= 1 and g2["nStop"] > 0,
          "%d 段 / %d 站" % (g2["nPart"], g2["nStop"]))

    # ------------------------------------------------------------------
    print("== 5) 浏览器：下拉过滤 / 地图绘制 / 交互")
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        br = p.chromium.launch(args=["--no-proxy-server"])
        pg = br.new_context(viewport={"width": 1600, "height": 950}).new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(BASE + "/", wait_until="load")
        pg.wait_for_timeout(900)
        pg.click('.tab[data-tab="curate"]')
        pg.wait_for_timeout(600)
        pg.evaluate("cuOpen('Z75')")
        pg.wait_for_timeout(2200)

        n = pg.evaluate("document.querySelectorAll('#ceViaRows .ve-row').length")
        check("Z75 手工经由多段", n >= 3, "%d 行" % n)
        check("地图面板可见", pg.locator("#cuMapPane").is_visible())
        check("校对台带 with-map",
              pg.evaluate("document.querySelector('#curateStage').classList.contains('with-map')"))
        check("默认 Canvas 后端（未配 Key）", pg.evaluate("CUMAP.backend") == "canvas",
              pg.evaluate("CUMAP.backend"))
        check("地图默认显示本趟车停站", "本趟车停站" in pg.inner_text("#cuMapInfo"),
              pg.inner_text("#cuMapInfo"))
        check("前端站名键与后端一致",
              pg.evaluate("cuStaKey('北京南站')===cuStaKey('北京南') && cuStaKey('获鹿所')==='获鹿'"))

        full = pg.evaluate("CU.lines.length")
        seen_sta = []
        for i in range(n):
            inp = pg.query_selector_all('#ceViaRows .ve-row .combo[data-cbkind="line"] input')[i]
            inp.click()
            pg.wait_for_timeout(200)
            s = pg.evaluate("""(i)=>{const b=document.querySelectorAll(
              '#ceViaRows .ve-row .combo[data-cbkind="line"]')[i];
              const sc=b.querySelector('.cb-scope');
              return {txt:sc?sc.innerText:'', items:b.querySelectorAll('.cb-item').length,
                      names:[...b.querySelectorAll('.cb-item .nm')].map(x=>x.innerText)};}""", i)
            sta = pg.evaluate("""(i)=>cuScopeSta(document.querySelectorAll(
              '#ceViaRows .ve-row .combo[data-cbkind="line"]')[i])""", i)
            seen_sta.append(sta)
            check("第 %d 行按发车站「%s」筛" % (i + 1, sta),
                  ("发车站" in s["txt"] and sta in s["txt"])
                  and 0 < s["items"] < full, "%d/%d 条" % (s["items"], full))
            check("第 %d 行当前线路仍在候选里" % (i + 1), inp.input_value() in s["names"],
                  inp.input_value())
            pg.keyboard.press("Escape")
        check("各段发车站确实不同", len(set(seen_sta)) >= 5, sorted(set(seen_sta)))

        inp = pg.query_selector_all('#ceViaRows .ve-row .combo[data-cbkind="line"] input')[0]
        inp.click(); pg.wait_for_timeout(250)
        btn = pg.query_selector('#ceViaRows .ve-row .combo[data-cbkind="line"] .cb-scope-all')
        check("下拉里有「显示全部」入口", btn is not None)
        if btn:
            btn.click(); pg.wait_for_timeout(300)
            cnt = pg.evaluate("""()=>document.querySelectorAll(
              '#ceViaRows .ve-row .combo[data-cbkind="line"]')[0]
              .querySelectorAll('.cb-item').length""")
            check("一键显示全部线路", cnt == full and pg.locator("#ceAllLines").is_checked(),
                  "%d/%d" % (cnt, full))
            pg.locator("#ceAllLines").uncheck(); pg.wait_for_timeout(200)
            # 关复选框会让下拉失焦关闭，重新点开才算新口径的候选数
            inp.click(); pg.wait_for_timeout(250)
            cnt2 = pg.evaluate("""()=>document.querySelectorAll(
              '#ceViaRows .ve-row .combo[data-cbkind="line"]')[0]
              .querySelectorAll('.cb-item').length""")
            check("关掉开关后恢复按发车站筛", 0 < cnt2 < full, cnt2)
        pg.keyboard.press("Escape")

        # 点第 2 行线路框 → 地图跟随
        pg.query_selector_all('#ceViaRows .ve-row .combo[data-cbkind="line"] input')[1].click()
        pg.wait_for_timeout(1500)
        st8 = pg.evaluate("""()=>({name:CUMAP.name, found:CUMAP.data&&CUMAP.data.found,
              parts:CUMAP.data&&CUMAP.data.parts.length,
              sta:CUMAP.data&&CUMAP.data.stations.length,
              focus:CUMAP.focus})""")
        check("地图跟随选中的线路", st8["name"] == "京广线" and st8["found"] is True, st8["name"])
        check("几何段与停站都到位", st8["parts"] > 0 and st8["sta"] > 20,
              "%d 段 / %d 站" % (st8["parts"], st8["sta"]))
        check("当前段起讫站传给地图高亮", bool(st8["focus"] and st8["focus"]["from"]),
              st8["focus"])
        px = pg.evaluate("""()=>{const c=document.querySelector('#cuMapCanvas');
          const im=c.getContext('2d').getImageData(0,0,c.width,c.height).data;
          let blue=0,orange=0;
          for(let i=0;i<im.length;i+=4){const r=im[i],g=im[i+1],b=im[i+2];
            if(b>170&&r<90&&g<160)blue++;
            if(r>190&&g>60&&g<140&&b<80)orange++;}
          return {blue,orange};}""")
        check("Canvas 真的画出蓝线", px["blue"] > 100, px)
        check("Canvas 真的画出本趟车停站", px["orange"] > 20, px)

        bb = pg.locator("#cuMapCanvas").bounding_box()
        pg.mouse.move(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
        pg.mouse.wheel(0, -300); pg.wait_for_timeout(220)
        check("滚轮缩放", pg.evaluate("CUMAP.view.zoom") > 1.05,
              round(pg.evaluate("CUMAP.view.zoom"), 3))
        pg.mouse.down()
        pg.mouse.move(bb["x"] + bb["width"] / 2 + 40, bb["y"] + bb["height"] / 2 + 25)
        pg.mouse.up(); pg.wait_for_timeout(220)
        check("拖拽平移", pg.evaluate("CUMAP.view.dx") == 40, pg.evaluate("[CUMAP.view.dx,CUMAP.view.dy]"))
        pg.click("#cuMapFit"); pg.wait_for_timeout(220)
        check("「适应」复位视野",
              pg.evaluate("CUMAP.view.zoom===1 && CUMAP.view.dx===0 && CUMAP.view.dy===0"))
        xy = pg.evaluate("""()=>{const s=cuMapTrainStops()[0],b=cuMapBBox(),
          c=document.querySelector('#cuMapCanvas');
          const q=cuMapProj(s.lon,s.lat,b,c.clientWidth,c.clientHeight);
          return {x:q[0],y:q[1],name:s.name};}""")
        pg.mouse.move(bb["x"] + xy["x"], bb["y"] + xy["y"]); pg.wait_for_timeout(220)
        check("悬停显示站名", pg.locator("#cuMapTip").is_visible()
              and xy["name"] in pg.inner_text("#cuMapTip"), pg.inner_text("#cuMapTip"))

        pg.fill("#cuMapQ", "京沪线"); pg.wait_for_timeout(650)
        sug = pg.query_selector_all("#cuMapSugg .cu-map-sugg-i[data-nm]")
        check("地图搜索按线名给建议", len(sug) >= 1 and sug[0].get_attribute("data-nm") == "京沪线",
              [x.get_attribute("data-nm") for x in sug])
        if sug:
            sug[0].click(); pg.wait_for_timeout(1500)
            check("点建议切换地图线路", "京沪线" in pg.inner_text("#cuMapInfo"),
                  pg.inner_text("#cuMapInfo"))

        pg.click("#ceMapToggle"); pg.wait_for_timeout(280)
        check("可收起地图",
              not pg.evaluate("document.querySelector('#curateStage').classList.contains('with-map')")
              and not pg.locator("#cuMapPane").is_visible())
        pg.click("#ceMapToggle"); pg.wait_for_timeout(400)
        check("可重新展开地图",
              pg.evaluate("document.querySelector('#curateStage').classList.contains('with-map')"))

        # 切走再切回，地图仍可绘制
        pg.click('.tab[data-tab="train"]'); pg.wait_for_timeout(320)
        pg.click('.tab[data-tab="curate"]'); pg.wait_for_timeout(600)
        check("切页签回来地图还在", pg.locator("#cuMapPane").is_visible()
              and pg.evaluate("CUMAP.backend") == "canvas")

        # ---- 中间段选线 → 右侧地图实时跟随（聚焦 / 打字 / 预览 / 起讫站 / 回退） ----
        via_bak = pg.evaluate("()=>CU.via.map(r=>({line:r.line,from:r.from,to:r.to,src:r.src}))")
        lns = pg.query_selector_all('#ceViaRows .ve-row .combo[data-cbkind="line"] input')
        r0 = pg.evaluate("()=>CU.via[0].line")
        lns[0].click(); pg.wait_for_timeout(900)
        check("聚焦段落 → 地图切到该段线路", pg.evaluate("CUMAP.name") == r0,
              [pg.evaluate("CUMAP.name"), r0])
        check("聚焦时红圈落在该段起讫站",
              pg.evaluate("CUMAP.focus && CUMAP.focus.from") == pg.evaluate("()=>CU.via[0].from"),
              pg.evaluate("CUMAP.focus"))

        pg.keyboard.press("Escape")
        lns[5].click()                             # 聚焦即切到这一段自己的线路
        pg.wait_for_timeout(700)
        keep = pg.evaluate("CUMAP.name")
        check("聚焦第 6 段切到该段线路", keep == pg.evaluate("()=>CU.via[5].line"),
              [keep, pg.evaluate("()=>CU.via[5].line")])
        pg.keyboard.press("Control+a")
        pg.keyboard.type("石太", delay=70); pg.wait_for_timeout(700)
        check("半截线名（前缀）不把地图闪空", pg.evaluate("CUMAP.name") == keep,
              [pg.evaluate("CUMAP.name"), keep])
        pg.keyboard.type("线", delay=70); pg.wait_for_timeout(900)
        check("补完线名后地图跟进", pg.evaluate("CUMAP.name") == "石太线",
              pg.evaluate("CUMAP.name"))

        pg.keyboard.press("Escape")
        lns[1].click(); pg.wait_for_timeout(500)
        pg.keyboard.press("ArrowDown"); pg.wait_for_timeout(650)
        act = pg.evaluate("""()=>{const b=document.querySelectorAll(
          '#ceViaRows .ve-row .combo[data-cbkind="line"]')[1].querySelector('.cb-item.active');
          return b?b.querySelector('.nm').textContent:'';}""")
        check("方向键浏览 → 地图预览高亮线路",
              bool(act) and pg.evaluate("CUMAP.name") == act,
              [pg.evaluate("CUMAP.name"), act])
        pg.keyboard.press("Escape"); pg.wait_for_timeout(700)
        check("Esc 取消预览 → 地图退回该段实际线路",
              pg.evaluate("CUMAP.name") == pg.evaluate("()=>CU.via[1].line"),
              [pg.evaluate("CUMAP.name"), pg.evaluate("()=>CU.via[1].line")])

        fst = pg.query_selector_all('#ceViaRows .ve-row .combo[data-field="from"] input')[1]
        fst.click(); pg.keyboard.press("Control+a")
        pg.keyboard.type("保定", delay=70); pg.wait_for_timeout(700)
        check("改起始站 → 地图红圈实时跟随",
              pg.evaluate("CUMAP.focus && CUMAP.focus.from") == "保定"
              and pg.evaluate("()=>CU.via[1].from") == "保定",
              [pg.evaluate("CUMAP.focus"), pg.evaluate("()=>CU.via[1].from")])

        # 点段落行的空白处（不是输入框）也算选中这一段
        pg.evaluate("()=>document.querySelectorAll('#ceViaRows .ve-row')[2].click()")
        pg.wait_for_timeout(800)
        check("点段落行 → 地图切到这一段线路",
              pg.evaluate("CUMAP.name") == pg.evaluate("()=>CU.via[2].line"),
              [pg.evaluate("CUMAP.name"), pg.evaluate("()=>CU.via[2].line")])

        # 还原经由，别把改过的线名 / 站名留给后面的检查
        pg.evaluate("""(bak)=>{CU.via=bak.map(r=>Object.assign({},r));cuRenderViaRows();}""", via_bak)
        pg.wait_for_timeout(500)

        # TMap 后端：注入一个最小假 SDK，验证有 Key 时那条路径不抛错、能建图层
        tres = pg.evaluate("""()=>{
          if (window.TMap) return {skip:true};
          const made=[];
          window.TMap={
            Map:function(){this.resize=()=>{};this.fitBounds=()=>{};this.easeTo=()=>{};},
            LatLng:function(a,b){this.lat=a;this.lng=b;},
            LatLngBounds:function(a,b){this.sw=a;this.ne=b;},
            PolylineStyle:function(o){this.o=o;}, MarkerStyle:function(o){this.o=o;},
            MultiPolyline:function(o){this.o=o;this.setMap=()=>{};made.push('line');},
            MultiMarker:function(o){this.o=o;this.setMap=()=>{};made.push('mark');},
          };
          CUMAP.tmap=new TMap.Map();
          try { cuMapTMapDraw(); } catch(e) { return {err:String(e)}; }
          const r={layers:CUMAP.tLayers.length,made:made,
                   paths:CUMAP.tLayers[0]?CUMAP.tLayers[0].o.geometries[0].paths.length:0};
          delete window.TMap; CUMAP.tmap=null; CUMAP.tLayers=[];
          return r;}""")
        check("TMap 后端路径可用（假 SDK）",
              tres.get("skip") or (not tres.get("err") and tres.get("layers", 0) >= 2
                                   and tres.get("paths", 0) > 0), tres)

        check("页面无 JS 错误", not errs, errs[:2])
        br.close()

    print("\n通过 %d / 失败 %d" % (ok, fail))
    if fail:
        print("失败项：", "；".join(bad))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
