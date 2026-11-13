# -*- coding: utf-8 -*-
"""
回归：跨线经由链 / N1534 11-25 的「和硕→吐鲁番→大河沿联络线」

用户反馈：车次详情里 N1534/1（2026-11-25，阿克苏→乌鲁木齐）按真实走向，
中间一段没画出来，应该是「和硕往吐鲁番走，然后走大河沿联络线」。

根因不在几何库：南疆线 / 大河沿联络线 / 兰新客专的定稿几何都在，
吐鲁番方向的点一直都在。是 `cnrail._via_chain()` 只拼出 1 段 —— 人工经由
写的「大河沿联络线」在库里叫「大河沿联络线（china-emu）」、「头道河所」
是线路所（南疆线站序从「珍珠泉」起算，没收这个所），两段都被跳过，
`_chain_mids('库尔勒','乌鲁木齐')` 返回 None。于是 `path_stations()` 在
库尔勒→乌鲁木齐之间一个中间站都补不出来，`path` 末段成了 270 km 大直线；
`railosm.build()` 拿这根直线当走廊（宽 24 km），把绕向吐鲁番 150 km 的
真实几何全裁掉了 —— 只剩一根直线，cover 0.781 / 缺口 174 km。

修法：`_via_line()` 认「（来源）」后缀版本，`_via_sta_pos()` 用同键正式名
（头道河线路所）的坐标，在线路站序里就近落点（60 km 内）。链补齐三段后，
path 展开出 和硕 / 托克逊 / 珍珠泉 / 大河沿 / 盐湖西… 走廊贴着真实线路，
几何自然全部留下。**`railosm` 一行没改**，缺口仍只按停车站补。
另外 `railcore.build_route_payload()` 让换向别名 `N1531` 继承主号 `N1534` 的人工校正
（先查自己那条、没有才查主号），两个车次号的接口与页面显示一致。

跑法：python scripts/test_route_via_chain.py   （先起 server --port 8686）
只读，不写库。
"""
from __future__ import annotations

import io
import json
import math
import os
import sys
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "webapp"))
BASE = "http://127.0.0.1:8686"
CODE, DATE = "N1534", "2026-11-25"
VIA = [
    {"line": "南疆线", "from": "阿克苏", "to": "头道河所"},
    {"line": "大河沿联络线", "from": "头道河所", "to": "大河沿"},
    {"line": "兰新客专", "from": "大河沿", "to": "乌鲁木齐"},
]
MID_STOPS = ["焉耆", "和硕", "托克逊", "珍珠泉", "大河沿", "盐湖西"]

ok = fail = 0
bad: list = []


def check(label: str, cond: bool, val="") -> None:
    global ok, fail
    if cond:
        ok += 1
        print("  ok   %s%s" % (label, ("  " + str(val))[:92] if val != "" else ""))
    else:
        fail += 1
        bad.append(label)
        print("  失败 %s%s" % (label, ("  " + str(val))[:92] if val != "" else ""))


def get(path: str, **kw) -> dict:
    qs = ("?" + urllib.parse.urlencode(kw)) if kw else ""
    with urllib.request.urlopen(BASE + path + qs, timeout=180) as r:
        return json.loads(r.read().decode("utf-8"))


def src(name: str) -> str:
    with io.open(os.path.join(ROOT, "webapp", name), encoding="utf-8") as f:
        return f.read()


def hav(a: dict, b: dict) -> float:
    R = 6371.0088
    p1, p2 = math.radians(a["lat"]), math.radians(b["lat"])
    dl, dp = math.radians(b["lon"] - a["lon"]), p2 - p1
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


# ---------------------------------------------------------------------------
print("== 1) 静态护栏")
# ---------------------------------------------------------------------------
cn = src("cnrail.py")
app = src(os.path.join("static", "app.js"))
osm = src("railosm.py")
rc = src("railcore.py")
check("有来源后缀线名解析", "def _via_line(" in cn)
check("有线路所端站就近兜底", "def _via_sta_pos(" in cn)
check("有坐标距离函数", "def _geo_km(" in cn)
check("就近兜底只认 60km 内", "best, bd = None, 60.0" in cn)
check("后缀用正则剥掉", "r\"[（(][^）)]*[）)]\\s*$\"" in cn)
check("_via_chain 走两个新解析", "L = _via_line(name)" in cn
      and cn.count("_via_sta_pos(st,") == 2)
check("_via_chain 不再直接 line(name)", "L = line(name)" not in
      cn[cn.index("def _via_chain("):])
check("跨线展开仍按 chain 串中间站", "names = _chain_mids(chain, ka_, kb_)" in cn)
check("站点直连仍只画停车站", "function directStops(route)" in app
      and "function directPath(route)" in app)
check("railosm 缺口仍只按停车站补", "_between(stop_path" in osm)
check("换向车次号继承主号的人工校正", "get_override(main)" in rc)
check("自己那条校正优先，不被主号覆盖",
      "ov = get_override(code)" in rc
      and rc.index("ov = get_override(code)") < rc.index("get_override(main)"))

# ---------------------------------------------------------------------------
print("== 2) 后端：三段经由链与中间站")
# ---------------------------------------------------------------------------
import railcore, cnrail, railosm                                # noqa: E402

chain = cnrail._via_chain(VIA)
check("三段经由全部解析出来", len(chain) == 3, len(chain))
desc = []
for label, st_all, pf, pt in chain:
    st = [x["name"] for x in st_all]
    desc.append((label, st[pf], st[pt]))
check("段1 南疆线 阿克苏→头道河所",
      any("南疆线" in d[0] and d[1] == "阿克苏" for d in desc), desc)
check("段2 认出「（china-emu）」那份联络线",
      any("大河沿联络线" in d[0] and d[2] == "大河沿" for d in desc), desc)
check("段3 兰新客专 大河沿→乌鲁木齐",
      any("兰新客专" in d[0] and d[1] == "大河沿" and d[2] == "乌鲁木齐"
          for d in desc), desc)

mids = cnrail._chain_mids(chain, "库尔勒", "乌鲁木齐") or []
check("跨线中间站串得出来", len(mids) >= 12, len(mids))
for nm in ["和硕", "托克逊", "珍珠泉", "大河沿"]:
    check("中间站含 " + nm, nm in mids, nm)
expanded = cnrail.path_stations(
    [{"name": "库尔勒", "km": 523.3, "lat": 41.7458, "lon": 86.1909},
     {"name": "乌鲁木齐", "km": 793.2, "lat": 43.8402, "lon": 87.5244}],
    prefer=[v["line"] for v in VIA], via=VIA)
check("线路所不进地图点", not any("头道河" in x["name"] for x in expanded),
      [x["name"] for x in expanded][:20])

P = railcore.build_route_payload(CODE, DATE, cache_only=True)
names = [x["name"] for x in (P.get("lineStops") or [])]
check("车次有 4 个停车站", len(P.get("stops") or []) == 4, len(P.get("stops") or []))
check("path 末段不再是 270km 直线", len(P.get("path") or []) >= 30,
      len(P.get("path") or []))
for nm in MID_STOPS:
    check("lineStops 补出 " + nm, nm in names, nm)
check("线路所不进 lineStops", "头道河所" not in names
      and "头道河线路所" not in names)
mx_path = max((hav(P["path"][i - 1], P["path"][i])
               for i in range(1, len(P["path"]))), default=0)
check("path 最大相邻跳 < 100km", mx_path < 100, round(mx_path, 1))

# ---------------------------------------------------------------------------
print("== 3) 后端：真实走向真的画到吐鲁番 / 大河沿")
# ---------------------------------------------------------------------------
pts, info = railosm.build(P)
check("真实走向拼得出来", bool(pts), info.get("err"))
check("三条经由线几何都参与", len(info.get("lines") or []) == 3, info.get("lines"))
check("覆盖率 ≥ 0.98", (info.get("cover") or 0) >= 0.98, info.get("cover"))
check("缺口 ≤ 20km", (info.get("gapKm") or 999) <= 20, info.get("gapKm"))
check("几何点上千", len(pts) >= 1000, len(pts))
# 别把「画到吐鲁番」写死成 88.9：走廊宽度 / 裁剪边界稍有变化，最东点会在
# 88.87~89.03 之间摆（cover 0.995 / 缺口 3.6km 都正常）。真正要防的是
# 「几何被整段裁掉、只剩库尔勒→乌鲁木齐直线」（最东只有乌鲁木齐 87.52）。
stop_lon_max = max(s["lon"] for s in P["stops"])
LON_TURPAN = 88.4
lon_max = max(q["lon"] for q in pts)
check("画到吐鲁番方向（经度 ≥ %.1f）" % LON_TURPAN, lon_max >= LON_TURPAN, round(lon_max, 3))
check("最东点明显越过本趟车停车站范围（+0.8°）",
      lon_max >= stop_lon_max + 0.8, (round(stop_lon_max, 3), round(lon_max, 3)))
turpan = sum(1 for q in pts if q["lat"] > 43.0 and q["lon"] > 88.6)
check("吐鲁番 / 大河沿一带有几何点", turpan >= 30, turpan)
mx_geo = max((hav(pts[i - 1], pts[i]) for i in range(1, len(pts))), default=0)
check("真实走向没有超大跳（≤ 60km）", mx_geo <= 60, round(mx_geo, 1))
check("缺口段不再拿停车站硬补", (info.get("fill") or 0) <= 2, info.get("fill"))

# ---------------------------------------------------------------------------
print("== 4) 不退化：其它人工经由车次 / 换向别名")
# ---------------------------------------------------------------------------
alias = railcore.build_route_payload("N1531", DATE, cache_only=True)
check("N1531 继承主号的人工经由", alias.get("viaSource") == "manual",
      alias.get("viaSource"))
check("N1531 的 lineStops 也补出和硕",
      "和硕" in [x["name"] for x in (alias.get("lineStops") or [])],
      len(alias.get("lineStops") or []))
check("N1531 与主号同一份站序", len(alias.get("path") or []) ==
      len(P.get("path") or []), len(alias.get("path") or []))

for code, baseline in [("Z75", 0.92), ("G1", 1.0), ("V6061", 1.0)]:
    p = railcore.build_route_payload(code, DATE, cache_only=True)
    if not p.get("stops"):
        check(code + " 有本地缓存", False, "缺缓存")
        continue
    q, inf = railosm.build(p)
    check(code + " 仍能拼出真实走向", bool(q), inf.get("err"))
    check(code + " 覆盖率 ≥ %.2f" % baseline,
          (inf.get("cover") or 0) >= baseline, inf.get("cover"))
    if code == "G1":
        check("G1 仍零缺口", (inf.get("gapKm") or 0) == 0, inf.get("gapKm"))

# ---------------------------------------------------------------------------
print("== 5) HTTP 接口")
# ---------------------------------------------------------------------------
R = get("/api/route", code=CODE, date=DATE)
O = get("/api/route/osm", code=CODE, date=DATE, mode="legacy")
check("/api/route 停车站仍 4 个", len(R.get("stops") or []) == 4)
check("/api/route lineStops 含和硕", "和硕" in
      [x["name"] for x in (R.get("lineStops") or [])])
check("/api/route/osm ok", O.get("ok") is True, O.get("error"))
check("/api/route/osm 覆盖率一致", abs(O.get("cover", 0) - info["cover"]) < 0.002,
      (O.get("cover"), info["cover"]))
check("/api/route/osm 点上千", len(O.get("path") or []) >= 1000,
      len(O.get("path") or []))
check("接口路径也画到吐鲁番", max(q["lon"] for q in O["path"]) >= LON_TURPAN,
      round(max(q["lon"] for q in O["path"]), 3))
RA = get("/api/route", code="N1531", date=DATE)
OA = get("/api/route/osm", code="N1531", date=DATE, mode="legacy")
check("N1531 接口也走人工经由", RA.get("viaSource") == "manual",
      RA.get("viaSource"))
check("N1531 接口站序与主号一致",
      len(RA.get("lineStops") or []) == len(R.get("lineStops") or []),
      len(RA.get("lineStops") or []))
check("N1531 真实走向与主号完全一致",
      OA.get("cover") == O.get("cover") and OA.get("gapKm") == O.get("gapKm")
      and len(OA.get("path") or []) == len(O.get("path") or []),
      (OA.get("cover"), OA.get("gapKm")))
check("N1531 接口也画到吐鲁番",
      max(q["lon"] for q in OA["path"]) >= LON_TURPAN,
      round(max(q["lon"] for q in OA["path"]), 3))

# ---------------------------------------------------------------------------
print("== 6) 浏览器：车次详情两种走向")
# ---------------------------------------------------------------------------
from playwright.sync_api import sync_playwright                 # noqa: E402

with sync_playwright() as pw:
    br = pw.chromium.launch(args=["--no-proxy-server"])
    pg = br.new_context(viewport={"width": 1500, "height": 960}).new_page()
    errs: list = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(BASE + "/?" + urllib.parse.urlencode({"code": CODE, "date": DATE}),
            wait_until="load")
    pg.wait_for_selector("#geoBtn", timeout=60000)
    pg.wait_for_timeout(1500)

    def state() -> dict:
        return pg.evaluate("""() => { const r = S.route;
          const ds = [...document.querySelectorAll('#fallback path')]
            .map(e => e.getAttribute('d') || '');
          const d = ds.find(x => x.length > 20) || '';
          return {path: r.path.length, lineStops: r.lineStops.length,
                  stops: r.stops.length, direct: r.directPath.length,
                  expanded: r.expandedPath.length, geo: S.geoOn,
                  lonMax: Math.max.apply(null, r.path.map(q => q.lon)),
                  note: document.querySelector('#geoNote').textContent,
                  btn: document.querySelector('#geoBtn').textContent,
                  d: d.length, corners: (d.match(/L/g) || []).length + (d ? 1 : 0),
                  labels: [...document.querySelectorAll('#fallback text')]
                    .map(e => e.textContent)}; }""")

    a = state()
    check("默认是站点直连", a["geo"] is False and a["btn"] == "按真实走向", a["btn"])
    check("直连只连 4 个停车站", a["path"] == 4 and a["direct"] == 4, a["path"])
    check("直连只画 4 个站点", a["lineStops"] == 4, a["lineStops"])
    check("展开折线留着备用", a["expanded"] >= 30, a["expanded"])
    check("直连 4 个折点", a["corners"] == 4, a["corners"])
    st_names = [s["name"] for s in P["stops"]]
    check("4 个站名标签都在", all(any(n in x for x in a["labels"]) for n in st_names),
          a["labels"])

    pg.click("#geoBtn")
    pg.wait_for_timeout(3500)
    b = state()
    check("切到真实走向", b["geo"] is True and b["btn"] == "回到站点直连", b["btn"])
    check("真实走向上千点", b["path"] >= 1000, b["path"])
    check("★ 前端折线画到吐鲁番方向", b["lonMax"] >= LON_TURPAN, round(b["lonMax"], 3))
    check("真实走向仍只标 4 个停车站", b["lineStops"] == 4, b["lineStops"])
    check("折线明显比直连复杂", b["corners"] > 100, b["corners"])
    check("4 个站名标签仍在",
          all(any(n in x for x in b["labels"]) for n in st_names), b["labels"])

    pg.click("#geoBtn")
    pg.wait_for_timeout(900)
    c = state()
    check("切回站点直连", c["geo"] is False and c["btn"] == "按真实走向", c["btn"])
    check("切回后折线完全恢复", c["path"] == 4 and c["corners"] == 4,
          (c["path"], c["corners"]))
    check("页面无 JS 错误", not errs, errs[:2])

    # 换向车次号 N1531 打开，也必须吃到同一份真实走向
    pg2 = br.new_context(viewport={"width": 1500, "height": 960}).new_page()
    errs2: list = []
    pg2.on("pageerror", lambda e: errs2.append(str(e)))
    pg2.goto(BASE + "/?" + urllib.parse.urlencode({"code": "N1531", "date": DATE}),
             wait_until="load")
    pg2.wait_for_selector("#geoBtn", timeout=60000)
    pg2.wait_for_timeout(1500)
    pg2.click("#geoBtn")
    pg2.wait_for_timeout(3500)
    b2 = pg2.evaluate("""() => ({code: S.route.code, path: S.route.path.length,
      lonMax: Math.max.apply(null, S.route.path.map(q => q.lon)),
      note: document.querySelector('#geoNote').textContent})""")
    check("N1531 打开时归属到主号", b2["code"] == CODE, b2["code"])
    check("★ N1531 打开同样画到吐鲁番", b2["lonMax"] >= LON_TURPAN,
          round(b2["lonMax"], 3))
    check("N1531 真实走向点数一致", b2["path"] >= 1000, b2["path"])
    check("N1531 页面无 JS 错误", not errs2, errs2[:2])
    br.close()

print("\n通过 %d 项，失败 %d 项" % (ok, fail))
if bad:
    print("失败：" + " / ".join(bad))
sys.exit(1 if fail else 0)
