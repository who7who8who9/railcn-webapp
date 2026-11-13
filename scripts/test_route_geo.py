# -*- coding: utf-8 -*-
"""
回归：车次详情「站点直连 / 按真实走向」两种画法（2026-11-11 Z75 11-25）

用户反馈：Z75（2026-11-25）两种模式显示都不对。查下来是两件事：

1. 「站点直连」画的其实不是经停站直连，而是后端 `cnrail.path_stations()`
   从**线路库**展开的 68 个通过站折线 —— 那些通过站坐标不少是仿射假值、
   站序还乱（北京西→保定段来回折返、中卫→兰州段一串锯齿）。
   现在前端只连本趟车实际停车站（7 点），通过站一律不画。
2. 「按真实走向」`railosm` 只查老 OSM 包 `railgeom.geom()`，老包里没有
   整理台确认过的「太中线」「石获线」，Z75 的 太原→中卫 700 km 只能画成
   一根大直线，缺口 750 km，跟站点直连看着几乎一样。现在几何来源改成
   整理台定稿 > 主站共用入口 > 老包，并把整条线的几何逐点裁成贴着本趟车
   走廊的子段，缺口只拿停车站直线补。

跑法：python scripts/test_route_geo.py   （先起 server --port 8686）
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
CODE, DATE = "Z75", "2026-11-25"

ok = fail = 0
bad: list = []


def check(label: str, cond: bool, val="") -> None:
    global ok, fail
    if cond:
        ok += 1
        print("  ok   %s%s" % (label, ("  " + str(val))[:78] if val != "" else ""))
    else:
        fail += 1
        bad.append(label)
        print("  失败 %s%s" % (label, ("  " + str(val))[:78] if val != "" else ""))


def get(path: str, **kw) -> dict:
    qs = ("?" + urllib.parse.urlencode(kw)) if kw else ""
    with urllib.request.urlopen(BASE + path + qs, timeout=180) as r:
        return json.loads(r.read().decode("utf-8"))


def hav(a: dict, b: dict) -> float:
    R = 6371.0088
    p1, p2 = math.radians(a["lat"]), math.radians(b["lat"])
    dl, dp = math.radians(b["lon"] - a["lon"]), p2 - p1
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def src(name: str) -> str:
    with io.open(os.path.join(ROOT, "webapp", name), encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
print("== 1) 静态护栏")
# ---------------------------------------------------------------------------
osm = src("railosm.py")
app = src(os.path.join("static", "app.js"))
check("railosm 用整理台确认线路几何", "confirmed.geom(name)" in osm)
check("railosm 有主站共用入口兜底", "lineosm.geom(name)" in osm)
check("railosm 逐点裁整线几何", "_trim_parts(src_parts, path" in osm)
check("railosm 缺口只按停车站补", "_between(stop_path" in osm
      and osm.count("_between(stop_path") >= 3)
check("railosm 不再只用老 OSM 包", "railgeom.geom(nm)" not in osm)
check("app.js 有站点直连折线", "function directPath(route)" in app)
check("app.js 有停车站点集", "function directStops(route)" in app)
check("app.js 默认就切成直连", "route.path = route.directPath;" in app)
check("app.js 不再拿 pathBase 当直连", "route.pathBase" not in app)
check("app.js 两种模式只画停车站", "route.lineStops = route.directStops;" in app)
check("app.js 相邻标签错开", "lastLabel.y + 16" in app)

# ---------------------------------------------------------------------------
print("== 2) 后端：几何来源与拼接")
# ---------------------------------------------------------------------------
import railcore                                            # noqa: E402
import railgeom                                            # noqa: E402
import railosm                                             # noqa: E402

P = railcore.build_route_payload(CODE, DATE, cache_only=True)
check("Z75 有 7 个经停站", len(P["stops"]) == 7, [s["name"] for s in P["stops"]])
check("后端 path 仍是展开的通过站折线（前端才切直连）",
      len(P["path"]) > 30, len(P["path"]))
check("老 OSM 包确实没有太中线", not railgeom.geom("太中线"))
check("老 OSM 包确实没有石获线", not railgeom.geom("石获线"))
check("新来源能取到太中线", bool((railosm._geom_of("太中线") or {}).get("parts")))
check("新来源能取到石获线", bool((railosm._geom_of("石获线") or {}).get("parts")))
check("新来源对确认线路返回整理台那份",
      (railosm._geom_of("太中线") or {}).get("name") == "太中线")

# 逐点裁：一条「一半贴走廊、一半在广州」的长段，要被切成只剩贴着的那一截
fake = [[116.30, 39.88], [116.20, 39.80], [113.30, 23.15], [113.25, 23.10]]
cut = railosm._trim_parts([fake], P["path"], 20.0)
check("逐点裁掉远离走廊的半段", len(cut) == 1 and len(cut[0]) == 2, cut)

pts, info = railosm.build(P)
check("Z75 拼得出真实走向", bool(pts) and len(pts) > 1000, len(pts or []))
check("7 段确认线路全部用上", len(info.get("lines") or []) == 7, info.get("lines"))
check("太中线在用到的线路里", "太中线" in (info.get("lines") or []))
check("石获线在用到的线路里", "石获线" in (info.get("lines") or []))
check("覆盖率 ≥ 90%", (info.get("cover") or 0) >= 0.90, info.get("cover"))
check("缺口 ≤ 150 km（原来 750 km）", (info.get("gapKm") or 9999) <= 150, info.get("gapKm"))
check("里程单调递增", all(b["km"] >= a["km"] - 1e-6 for a, b in zip(pts, pts[1:])))
check("首尾就是全程", abs(pts[0]["km"]) < 0.01
      and abs(pts[-1]["km"] - P["totalKm"]) < 0.5, (pts[0]["km"], pts[-1]["km"]))
jumps = [hav(a, b) for a, b in zip(pts, pts[1:])]
check("没有 >100 km 的超长直线（原来 625 km）", max(jumps) < 100, round(max(jumps), 1))
check("折线没跑到华南（没混入整条京广线南段）",
      min(p["lat"] for p in pts) > 35.0,
      round(min(p["lat"] for p in pts), 2))
check("缺口补点只用停车站（fill 不再是通过站）",
      (info.get("fill") or 0) <= len(P["stops"]), info.get("fill"))

# 别的车次不能被改坏：G1 全程京沪高铁，改前就是零缺口
G1 = railcore.build_route_payload("G1", DATE, cache_only=True)
gpts, ginfo = railosm.build(G1)
check("G1 仍是零缺口", bool(gpts) and (ginfo.get("gapKm") or 0) < 1,
      ginfo.get("gapKm"))
check("G1 没有超大跳", bool(gpts) and max(hav(a, b) for a, b in zip(gpts, gpts[1:])) < 100,
      round(max(hav(a, b) for a, b in zip(gpts, gpts[1:])), 1) if gpts else None)

# ---------------------------------------------------------------------------
print("== 3) HTTP 接口")
# ---------------------------------------------------------------------------
r = get("/api/route", code=CODE, date=DATE)
check("/api/route 经停站 7 个", len(r.get("stops") or []) == 7)
check("/api/route 后端 path 未变（前端切直连）", len(r.get("path") or []) > 30,
      len(r.get("path") or []))
g = get("/api/route/osm", code=CODE, date=DATE)
check("/api/route/osm ok", g.get("ok") is True, g.get("error"))
check("默认走「按确认线路」新画法", g.get("mode") == "confirmed"
      and (g.get("shapeSegs") or 0) > 0, (g.get("mode"), g.get("shapeSegs")))
# 原画法一行没删，mode=legacy 随时可切回（详见 test_route_shape.py）
old = get("/api/route/osm", code=CODE, date=DATE, mode="legacy")
check("mode=legacy 原画法仍可用", old.get("ok") is True
      and old.get("mode") == "legacy" and "clipped" in old,
      (old.get("mode"), old.get("cover")))
check("/api/route/osm 用上太中线", "太中线" in (g.get("lines") or []))
check("/api/route/osm 覆盖 ≥ 90%", (g.get("cover") or 0) >= 0.90, g.get("cover"))
check("/api/route/osm 缺口 ≤ 150 km", (g.get("gapKm") or 9999) <= 150, g.get("gapKm"))
check("/api/route/osm 点数 > 1000", len(g.get("path") or []) > 1000,
      len(g.get("path") or []))

# ---------------------------------------------------------------------------
print("== 4) 浏览器：两种模式确实不同且可来回切")
# ---------------------------------------------------------------------------
from playwright.sync_api import sync_playwright                 # noqa: E402

with sync_playwright() as p:
    br = p.chromium.launch(args=["--no-proxy-server"])
    pg = br.new_context(viewport={"width": 1500, "height": 960}).new_page()
    errs: list = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(BASE + "/?" + urllib.parse.urlencode({"code": CODE, "date": DATE}),
            wait_until="load")
    pg.wait_for_selector("#geoBtn", timeout=60000)
    pg.wait_for_timeout(1200)

    def state() -> dict:
        return pg.evaluate("""() => { const r = S.route;
          const ds = [...document.querySelectorAll('#fallback path')].map(e => e.getAttribute('d') || '');
          const d = ds.find(x => x.length > 20) || '';
          return {path: r.path.length, lineStops: r.lineStops.length,
                  stops: r.stops.length, direct: r.directPath.length,
                  expanded: r.expandedPath.length, geo: S.geoOn,
                  note: document.querySelector('#geoNote').textContent,
                  btn: document.querySelector('#geoBtn').textContent,
                  d: d.length, corners: (d.match(/L/g) || []).length + (d ? 1 : 0),
                  labels: [...document.querySelectorAll('#fallback text')].map(e => e.textContent)}; }""")

    a = state()
    check("默认就是站点直连", a["geo"] is False and a["btn"] == "按真实走向", a["btn"])
    check("直连 path = 7 个停车站", a["path"] == 7 and a["direct"] == 7, a["path"])
    check("直连只画 7 个停车站点", a["lineStops"] == 7, a["lineStops"])
    check("展开折线仍留着备用（没丢数据）", a["expanded"] > 30, a["expanded"])
    check("直连画出来 7 个折点", a["corners"] == 7, a["corners"])
    names = [s["name"] for s in P["stops"]]
    check("7 个站名标签都在", all(any(n in x for x in a["labels"]) for n in names),
          a["labels"])
    check("直连备注为空", a["note"] == "")

    pg.click("#geoBtn")
    pg.wait_for_timeout(3500)
    b = state()
    check("切到真实走向", b["geo"] is True and b["btn"] == "回到站点直连", b["btn"])
    check("真实走向 path 上千点", b["path"] > 1000, b["path"])
    check("真实走向仍只画 7 个停车站点", b["lineStops"] == 7, b["lineStops"])
    check("两种模式画出来的线明显不同", b["d"] > a["d"] * 3 and b["corners"] > 100,
          (a["corners"], b["corners"]))
    # 备注必须如实报出「这一趟的线不是全程贴线」：按确认线路画法里碎段断口
    # 会标「断口自动连接」，真没几何的段才标「缺口」，两种都算如实。
    check("备注如实报出断口 / 缺口",
          ("缺口" in b["note"] or "断口自动连接" in b["note"]), b["note"])
    check("备注标明当前画法",
          "按确认线路" in b["note"] or "原画法" in b["note"], b["note"])
    check("切过去时 7 个站名标签仍在",
          all(any(n in x for x in b["labels"]) for n in names), b["labels"])

    pg.click("#geoBtn")
    pg.wait_for_timeout(900)
    c = state()
    check("切回站点直连", c["geo"] is False and c["btn"] == "按真实走向", c["btn"])
    check("切回后折线完全恢复", c["path"] == 7 and c["d"] == a["d"]
          and c["corners"] == 7, (c["path"], c["corners"]))
    check("切回后备注清空", c["note"] == "")
    check("页面无 JS 错误", not errs, errs[:2])
    br.close()

print("\n通过 %d 项，失败 %d 项" % (ok, fail))
if bad:
    print("失败：" + " / ".join(bad))
sys.exit(1 if fail else 0)
