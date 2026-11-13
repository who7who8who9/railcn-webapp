# -*- coding: utf-8 -*-
"""回归：确认线路地图（/linemap.html，2026-11-13 用户口径）

左侧只列整理台「全部确认」的线路、可单选可复选、默认全选；
右侧腾讯地图画选中线路，右栏占屏尽量大。无 Key 时自绘 Canvas 示意图，
不画国界、不引入任何第三方瓦片，代码里不含 Key。

跑法：python scripts/test_linemap.py   （先起 server --port 8686）
只读，不写库。
"""
from __future__ import annotations

import io
import json
import os
import sys
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "webapp"))
BASE = "http://127.0.0.1:8686"

ok = fail = 0
bad: list = []


def check(label: str, cond: bool, val="") -> None:
    global ok, fail
    if cond:
        ok += 1
        print("  ok   %s%s" % (label, ("  " + str(val))[:100] if val != "" else ""))
    else:
        fail += 1
        bad.append(label)
        print("  失败 %s%s" % (label, ("  " + str(val))[:100] if val != "" else ""))


def get(path: str, **kw) -> dict:
    qs = ("?" + urllib.parse.urlencode(kw)) if kw else ""
    with urllib.request.urlopen(BASE + path + qs, timeout=240) as r:
        return json.loads(r.read().decode("utf-8"))


def read(rel: str) -> str:
    with io.open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
print("== 1) 静态护栏：页面 / 地图合规 / 不内嵌 Key")
# ---------------------------------------------------------------------------
for p in ("/linemap.html", "/linemap.css", "/linemap.js"):
    with urllib.request.urlopen(BASE + p, timeout=20) as r:
        check("%s 可访问" % p, r.status == 200, r.status)

html = read("webapp/static/linemap.html")
js = read("webapp/static/linemap.js")
css = read("webapp/static/linemap.css")
server = read("webapp/server.py")
mod = read("webapp/linemap.py")

main_html = read("webapp/static/index.html")
check("独立页面（主站顶栏有入口，但不占页签）",
      'href="/linemap.html"' in main_html
      and 'data-tab="linemap"' not in main_html)
check("有独立的静态三件套", all(x in html for x in
      ("linemap.css", "linemap.js", "id=\"lmMap\"", "id=\"lmList\"")))
check("SDK 只来自腾讯地图 GL JS",
      "https://map.qq.com/api/gljs?v=1.exp" in js
      and "map.qq.com/api/gljs" in js)
for banned in ("leaflet", "openstreetmap", "mapbox", "googleapis.com/maps",
               "tile.openstreetmap", "unpkg.com/leaflet"):
    check("没有非合规底图：%s" % banned,
          banned not in (html + js + css).lower())
check("代码里没有写死 Key（只在运行时拼 localStorage 的值）",
      "localStorage.getItem('railcn.mapkey')" in js
      and "encodeURIComponent(key)" in js
      and "&key=" + "ABCDEFG" not in js)
check("代理模式占位符保留原样",
      "__WB_HTTP_PORT__" in js and "__WB_TMAP_SECRET__" in js)
check("WGS-84 → GCJ-02 后再喂给 TMap", "function GCJ" in js or "const GCJ" in js)
check("无 Key 走自绘 Canvas 且注明不画国界",
      "initCanvas" in js and "不画国界" in js)
check("服务端有 linemap 路由",
      "/api/linemap/index" in server and "/api/linemap/geom" in server)
check("后端只读（没有写库语句）",
      not any(w in mod for w in ("INSERT ", "UPDATE ", "DELETE ", "commit()")))
check("几何走确认线路唯一口径",
      "confirmed_only=True" in mod and "confirmed.recent()" in mod)
check("默认全选", "S.sel = new Set(S.items.map(x => x.name))" in js)

# ---------------------------------------------------------------------------
print("== 2) 后端：清单口径与批量几何")
# ---------------------------------------------------------------------------
import confirmed  # noqa: E402

idx = get("/api/linemap/index")
names_all = [x["name"] for x in idx["items"]]
check("清单只认整理台全部确认的线路",
      idx.get("scope") == "confirmed"
      and set(names_all) == confirmed.names()
      and idx["total"] == len(confirmed.recent()),
      (idx["total"], len(confirmed.names())))
check("清单字段齐全（站数 / 里程 / 起讫 / 确认时间）",
      all(all(k in x for k in ("name", "nStop", "totalKm", "fromSta", "toSta",
                               "confirmedAt", "geomSrc")) for x in idx["items"]))
check("清单没有来源版本名 / 草稿名",
      all("（" not in n and "(" not in n for n in names_all))

g = get("/api/linemap/geom", names="|".join(names_all), detail=400, stops=0)
check("批量一次返回全部选中线路", len(g["items"]) == len(names_all),
      (len(g["items"]), len(names_all)))
check("每条都有多段几何", all(x["found"] and x["nPart"] >= 1 and x["nPt"] >= 2
                       for x in g["items"]))
check("抽稀保住每段的点（段内 ≥2 点）",
      all(len(p) >= 2 for x in g["items"] for p in x["parts"]))
check("抽稀没有超过预算太多（逐段保首尾）",
      all(x["nPt"] <= 400 + x["nPart"] * 2 + 4 for x in g["items"]),
      max(x["nPt"] for x in g["items"]))
bads = [x["name"] for x in g["items"]
        if not x["bbox"] or not (73 <= x["bbox"][0] <= 136 and 3 <= x["bbox"][1] <= 54)]
check("点序 [lon,lat] 且 bbox 在中国范围", not bads, bads[:3])
check("stops=0 不下发站点", all(not x["stations"] for x in g["items"]))
gs = get("/api/linemap/geom", names="|".join(names_all[:3]), detail=200, stops=1)
check("stops=1 按确认站序带回车站",
      all(x["stations"] for x in gs["items"])
      and all(all(k in s for k in ("name", "lon", "lat", "km"))
              for x in gs["items"] for s in x["stations"]))
bad_in = get("/api/linemap/geom", names="不是确认线路测试名|" + names_all[0])
check("非确认线路名被静默跳过（不画草稿 / 来源版本）",
      len(bad_in["items"]) == 1 and bad_in["items"][0]["name"] == names_all[0],
      [x["name"] for x in bad_in["items"]])
check("空清单不报错", get("/api/linemap/geom", names="")["items"] == [])

# ---------------------------------------------------------------------------
print("== 3) 浏览器：默认全选 / 单选复选 / 右栏占比 / Key 不刷新")
# ---------------------------------------------------------------------------
from playwright.sync_api import sync_playwright  # noqa: E402

with sync_playwright() as pw:
    br = pw.chromium.launch(args=["--no-proxy-server"])
    ctx = br.new_context(viewport={"width": 1600, "height": 900})
    ctx.add_init_script("try{localStorage.removeItem('railcn.mapkey')}catch(e){};window.__lmMarker=1;")
    pg = ctx.new_page()
    errs: list = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    # 拦截外部 SDK：无 Key 本来就不加载；测「保存假 Key」时让它立刻失败，
    # 免得测试依赖外网
    pg.route("**/map.qq.com/**", lambda route: route.abort())
    pg.goto(BASE + "/linemap.html", wait_until="load")
    pg.wait_for_selector(".lm-row", timeout=60000)
    pg.wait_for_function(
        "document.querySelector('#lmStatus').textContent.includes('已显示')",
        timeout=120000)

    total = idx["total"]
    check("左栏列出全部确认线路", pg.locator(".lm-row").count() == total,
          pg.locator(".lm-row").count())
    check("默认全部选中", pg.locator(".lm-row.on").count() == total,
          pg.locator(".lm-row.on").count())
    check("状态报出线路数与点数",
          "已显示" in pg.inner_text("#lmStatus") and str(total) in pg.inner_text("#lmStatus"),
          pg.inner_text("#lmStatus")[:70])
    check("无 Key 时降级成自绘示意图",
          not pg.locator("#lmCanvas").is_hidden()
          and "示意图" in pg.inner_text("#lmKeyState"))
    check("Canvas 真的画了东西（不是空白）", pg.evaluate("""() => {
      const c = document.querySelector('#lmCanvas');
      const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
      let n = 0;
      for (let i = 0; i < d.length; i += 4 * 97)
        if (d[i] < 230 || d[i+1] < 230 || d[i+2] < 230) n++;
      return n > 20;
    }"""))

    box_map = pg.locator("#lmMain").bounding_box()
    check("右栏占屏 ≥ 78%", box_map["width"] / 1600 >= 0.78,
          round(box_map["width"] / 1600, 3))
    check("右栏铺满整屏高度", abs(box_map["height"] - 900) < 2, box_map["height"])

    pg.click("#lmCollapse")
    pg.wait_for_timeout(320)
    box2 = pg.locator("#lmMain").bounding_box()
    check("收起左栏后地图几乎占满全宽", box2["width"] / 1600 >= 0.99,
          round(box2["width"] / 1600, 3))
    check("展开按钮出现", not pg.locator("#lmExpand").is_hidden())
    pg.click("#lmExpand")
    pg.wait_for_timeout(320)
    check("展开后恢复", pg.locator("#lmSide").bounding_box()["width"] > 200)

    pg.click("#lmNone")
    pg.wait_for_timeout(500)
    check("清空：0 条选中", pg.locator(".lm-row.on").count() == 0)
    check("清空时状态如实提示", "没有选中线路" in pg.inner_text("#lmStatus"))
    pg.click("#lmAll")
    pg.wait_for_timeout(900)
    check("全选：全部回来", pg.locator(".lm-row.on").count() == total)
    pg.click("#lmInvert")
    pg.wait_for_timeout(500)
    check("反选：全部取消", pg.locator(".lm-row.on").count() == 0)
    pg.click("#lmAll")
    pg.wait_for_timeout(900)

    # 复选：勾掉一条，仍剩 N-1 条
    first = pg.locator(".lm-row").first
    pg.locator(".lm-row input").first.uncheck()
    pg.wait_for_timeout(350)
    check("复选模式可增减", pg.locator(".lm-row.on").count() == total - 1,
          pg.locator(".lm-row.on").count())

    # 单选模式：radio + 点一行只留一条
    pg.check("input[name='lmMode'][value='single']")
    pg.wait_for_timeout(350)
    check("切到单选模式只剩一条", pg.locator(".lm-row.on").count() == 1,
          pg.locator(".lm-row.on").count())
    check("单选模式渲染成 radio",
          pg.locator(".lm-row input[type=radio]").count() == total)
    pg.locator(".lm-row").nth(3).click(position={"x": 70, "y": 10})
    pg.wait_for_timeout(350)
    check("单选点另一行会换线", pg.locator(".lm-row.on").count() == 1
          and pg.locator(".lm-row").nth(3).get_attribute("class").find("on") >= 0)
    pg.check("input[name='lmMode'][value='multi']")
    pg.click("#lmAll")
    pg.wait_for_timeout(900)

    # 点名单选（复选模式下的快捷单选）
    pg.check("#lmSolo")
    pg.locator(".lm-row").nth(2).click(position={"x": 70, "y": 10})
    pg.wait_for_timeout(350)
    check("复选模式下「点名单选」只看一条", pg.locator(".lm-row.on").count() == 1)
    pg.uncheck("#lmSolo")
    pg.click("#lmAll")
    pg.wait_for_timeout(900)

    # 搜索 + 开关
    kw = names_all[0][:2]
    pg.fill("#lmQ", kw)
    pg.wait_for_timeout(250)
    check("搜索按线名 / 起讫过滤",
          0 < pg.locator(".lm-row").count() < total,
          pg.locator(".lm-row").count())
    pg.fill("#lmQ", "")
    pg.check("#lmStops")
    # 42 条线的确认站序要逐条读，实测约 5s，等结果回来再断言
    pg.wait_for_function(
        "document.querySelector('#lmStatus').textContent.includes('个车站')",
        timeout=120000)
    check("显示车站开关可用（按确认站序带出站点）",
          "个车站" in pg.inner_text("#lmStatus"),
          pg.inner_text("#lmStatus")[:80])
    pg.uncheck("#lmStops")
    pg.select_option("#lmDetail", "400")
    pg.wait_for_function("""() => { const t = document.querySelector('#lmStatus').textContent;
      return t.includes('已显示') && !t.includes('个车站'); }""", timeout=120000)
    check("精度切换可用", "已显示" in pg.inner_text("#lmStatus")
          and "个车站" not in pg.inner_text("#lmStatus"))

    # Key：保存后原地生效，绝不整页刷新
    pg.click("#lmKey")
    check("Key 弹窗打开", not pg.locator("#lmKeyModal").is_hidden())
    check("弹窗标明当前站点与 origin 隔离提示",
          "127.0.0.1:8686" in pg.inner_text("#lmSite")
          and "localhost" in pg.inner_text("#lmKeyModal"),
          pg.inner_text("#lmSite"))
    pg.fill("#lmKeyInput", "TEST-KEY-FOR-REGRESSION")
    pg.click("#lmKeySave")
    pg.wait_for_timeout(900)
    check("保存 Key 后仍在同一页（没有 reload）",
          pg.evaluate("window.__lmMarker === 1"))
    check("Key 存进 localStorage（与主站共用）",
          pg.evaluate("localStorage.getItem('railcn.mapkey')") == "TEST-KEY-FOR-REGRESSION")
    pg.click("#lmKey")
    pg.click("#lmKeyClear")
    pg.wait_for_timeout(400)
    check("清空 Key 后回到自绘示意图",
          pg.evaluate("!localStorage.getItem('railcn.mapkey')")
          and "示意图" in pg.inner_text("#lmKeyState"))
    check("页面无 JS 错误", not errs, errs[:3])

    # ---- 腾讯地图分支：注入 mock SDK，验证真的按腾讯 GL JS 的 API 画了 ----
    # 测试机没有真 Key，用一段假的 TMap 实现拦下 CDN 请求，
    # 检查 MultiPolyline / setGeometries / fitBounds / InfoWindow 的实参。
    MOCK = r"""(function(){
      function LatLng(lat,lng){this.lat=lat;this.lng=lng;}
      function Map(id,o){this.o=o;this.center=o.center;window.__TM=this;}
      Map.prototype.destroy=function(){};
      Map.prototype.getCenter=function(){return this.center;};
      Map.prototype.fitBounds=function(b){window.__TFit=b;};
      Map.prototype.resize=function(){};
      function Bounds(sw,ne){this.sw=sw;this.ne=ne;}
      function PStyle(o){this.o=o;}
      function MStyle(o){this.o=o;}
      function Poly(o){window.__TStyles=o.styles;window.__TPoly=this;this.setGeometries(o.geometries||[]);}
      Poly.prototype.setGeometries=function(g){this.g=g;window.__TGeoms=g;};
      Poly.prototype.on=function(e,cb){this['on_'+e]=cb;};
      function Mark(o){window.__TMarkStyles=o.styles;this.setGeometries(o.geometries||[]);}
      Mark.prototype.setGeometries=function(g){this.g=g;window.__TMarks=g;};
      function Win(o){this.o=o;}
      Win.prototype.setPosition=function(p){this.p=p;};
      Win.prototype.setContent=function(c){window.__TIW=c;};
      Win.prototype.open=function(){this.opened=true;};
      Win.prototype.close=function(){this.opened=false;};
      window.TMap={LatLng:LatLng,Map:Map,LatLngBounds:Bounds,PolylineStyle:PStyle,
        MarkerStyle:MStyle,MultiPolyline:Poly,MultiMarker:Mark,InfoWindow:Win};
    })();"""
    ctx2 = br.new_context(viewport={"width": 1600, "height": 900})
    ctx2.add_init_script("localStorage.setItem('railcn.mapkey','MOCK-KEY-FOR-TEST');")
    p2 = ctx2.new_page()
    errs2: list = []
    p2.on("pageerror", lambda e: errs2.append(str(e)))
    p2.route("**/map.qq.com/api/gljs*", lambda r: r.fulfill(
        status=200, content_type="application/javascript", body=MOCK))
    p2.goto(BASE + "/linemap.html", wait_until="load")
    p2.wait_for_function("window.__TGeoms && window.__TGeoms.length > 0", timeout=120000)
    check("腾讯地图：底图状态标成腾讯", "腾讯地图" in p2.inner_text("#lmKeyState"),
          p2.inner_text("#lmKeyState"))
    ng = p2.evaluate("window.__TGeoms.length")
    check("腾讯地图：每条选中线路一条 MultiPolyline geometry", ng == total, ng)
    check("腾讯地图：几何是多段 paths（不是硬串成一条）", p2.evaluate("""() => {
      const g = window.__TGeoms;
      return g.every(x => x.paths && x.paths.length >= 1) &&
             g.some(x => x.paths.length > 1) &&
             g.every(x => x.paths.every(p => p.length >= 2));
    }"""))
    check("腾讯地图：点已过 GCJ-02 且落在中国范围", p2.evaluate("""() => {
      let n = 0, bad = 0;
      for (const g of window.__TGeoms) for (const p of g.paths) for (const q of p) {
        n++; if (!(q.lng > 73 && q.lng < 136 && q.lat > 3 && q.lat < 54)) bad++;
      }
      return n > 1000 && bad === 0;
    }"""))
    check("腾讯地图：每条线一种颜色（样式一次建全）", p2.evaluate("""() => {
      const s = Object.values(window.__TStyles || {});
      return s.length === window.__TGeoms.length &&
             new Set(s.map(x => x.o.color)).size > 20;
    }"""))
    check("腾讯地图：自动缩放到选中线路 bbox", p2.evaluate("!!window.__TFit"))
    p2.evaluate("""() => window.__TPoly.on_click({
      geometry: {id: window.__TGeoms[0].id}, latLng: window.__TM.center})""")
    check("腾讯地图：点线路弹出线路信息窗",
          names_all[0] in (p2.evaluate("window.__TIW") or ""),
          (p2.evaluate("window.__TIW") or "")[:60])
    p2.check("#lmStops")
    p2.wait_for_function("window.__TMarks && window.__TMarks.length > 0", timeout=120000)
    check("腾讯地图：显示车站用 MultiMarker 圆点",
          p2.evaluate("window.__TMarks.length") > 100,
          p2.evaluate("window.__TMarks.length"))
    check("腾讯地图分支无 JS 错误", not errs2, errs2[:3])
    ctx2.close()
    br.close()

print("\n通过 %d 项，失败 %d 项" % (ok, fail))
if bad:
    print("失败：" + " / ".join(bad))
sys.exit(1 if fail else 0)
