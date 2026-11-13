# -*- coding: utf-8 -*-
"""
回归：N17（2026-11-25，北京→齐齐哈尔）车次详情「按真实走向」

用户反馈「N17 在 11/25 在车次详情的真实线路还是不正确」。停站只有
北京 / 秦皇岛 / 沈阳北 / 哈尔滨 / 大庆西 / 齐齐哈尔，人工经由 4 段：
京哈线 北京→哈尔滨、滨洲线 哈尔滨→红旗营、榆红线 红旗营→榆树屯、
平齐线 榆树屯→齐齐哈尔。

两个根因：
1. `cnrail._via_chain()` 优先用整理台确认线路站序，但以前只认线路库那份：
   滨洲线在库里只有 44 站，「对青山」之后直接跳到 367 km 外，
   哈尔滨—大庆西—红旗营 整段缺失，`红旗营` 认不出 ⇒ 这一段被丢掉 ⇒
   跨线链断在大庆西，`path` 末段是一根 107 km 直线；平齐线那份站序
   方向还是乱的（榆树屯之后往南绕到大兴再跳回齐齐哈尔）。
   修法：确认站序优先、底表兜底（未确认的线 / 来源后缀名仍走底表）。
2. `railosm.build()` 把 4 条线的整条几何混在一起按里程贪心拼：滨洲线
   过昂昂溪继续往满洲里那一截也在走廊 45 km 内，因为「走得最远」被选中，
   真正进齐齐哈尔的平齐线短段反而被丢掉，终点落在 41 km 外，还多出
   一段 48.5 km 的西绕折返。
   修法：按每条经由线路自己的 from→to 站序二次裁剪几何；站序坐标不可靠
   （Z75 石太线中位偏离 25.8 km）时用贴合度判定跳过裁剪，别把真走向切光。

跑法：python scripts/test_route_n17_via.py   （先起 server --port 8686）
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
CODE, DATE = "N17", "2026-11-25"

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
osm = src("railosm.py")
check("经由链有确认站序兜底", "def _via_confirmed_stations(" in cn)
check("兜底只认全部确认线路", "d.get(\"confirmed\")" in cn)
check("chain 自带站序、不按下标回查 _LINES",
      "chain.append((label, st, pf, pt))" in cn
      and "_LINES[li][\"stations\"]" not in
      cn[cn.index("def _chain_mids("):cn.index("def path_stations(")])
check("确认站序优先、底表兜底",
      "cands.append((name + \"（整理台）\", conf))" in cn
      and "cands.append((L[\"name\"], L[\"stations\"]))" in cn
      and cn.index("cands.append((name + \"（整理台）\", conf))")
      < cn.index("cands.append((L[\"name\"], L[\"stations\"]))"))
check("几何按线名分组", "groups.append((nm, g[\"parts\"]))" in osm)
check("按本段站序二次裁剪", "def _seg_polyline(" in osm
      and "def _clip_to_seg(" in osm and "SEG_OFF" in osm)
check("站序不可靠时跳过裁剪", "def _seg_hug(" in osm
      and "hug >= 0.60 and hug >= need" in osm)
check("确认站序有 20 秒缓存", "now - _SEGPOLY_TS > 20" in osm)

# ---------------------------------------------------------------------------
print("== 2) 后端：四段经由链补齐")
# ---------------------------------------------------------------------------
import railcore, cnrail, railosm, confirmed          # noqa: E402

R = railcore.build_route_payload(CODE, DATE, cache_only=True, with_path=True)
check("N17 有 6 个停车站", len(R["stops"]) == 6,
      [s["name"] for s in R["stops"]])
check("人工经由 4 段", len(R["viaSegs"]) == 4,
      [(s["line"], s["from"], s["to"]) for s in R["viaSegs"]])

chain = cnrail._via_chain(R["viaSegs"])
check("四段全部解析出来", len(chain) == 4, len(chain))
desc = [(lab, st[pf]["name"], st[pt]["name"]) for lab, st, pf, pt in chain]
check("京哈线 北京→哈尔滨", "京哈线" in desc[0][0]
      and desc[0][1] == "北京" and desc[0][2] == "哈尔滨", desc[0])
check("滨洲线端点解析出来了（整理台站序）",
      "整理台" in desc[1][0] and desc[1][1] == "哈尔滨" and desc[1][2] == "红旗营",
      desc[1])
check("榆红线 红旗营→榆树屯", desc[2][1] == "红旗营" and desc[2][2] == "榆树屯",
      desc[2])
check("平齐线 榆树屯→齐齐哈尔", desc[3][1] == "榆树屯" and desc[3][2] == "齐齐哈尔",
      desc[3])

conf_st = confirmed.detail_payload("滨洲线")["stations"]
check("确认版滨洲线含红旗营",
      any(s["name"] == "红旗营" for s in conf_st), len(conf_st))
lib_st = cnrail._via_line("滨洲线")["stations"]
check("底表版滨洲线不含红旗营",
      not any(s["name"] == "红旗营" for s in lib_st), len(lib_st))

mids = cnrail._chain_mids(chain, "大庆西", "齐齐哈尔") or []
check("跨线中间站串得出来", len(mids) >= 8, len(mids))
for nm in ("红旗营", "榆树屯", "大民屯", "齐齐哈尔南"):
    check("中间站含 " + nm, nm in mids, nm)
check("中间站不往南绕到三间房/昂昂溪",
      "三间房" not in mids and "汤池" not in mids, mids)

ln = [x["name"] for x in R["lineStops"]]
check("通过站折线补出红旗营", "红旗营" in ln, ln[-12:])
check("通过站折线补出榆树屯", "榆树屯" in ln, ln[-12:])
check("通过站折线补出大民屯/齐齐哈尔南",
      "大民屯" in ln and "齐齐哈尔南" in ln, ln[-12:])
check("尾段不再往南绕到三间房",
      "三间房" not in ln[-12:] and "大兴" not in ln[-12:], ln[-12:])
tail = R["lineStops"][-12:]
gap = max(hav(tail[i - 1], tail[i]) for i in range(1, len(tail)))
check("末段不再是 107km 一根直线", gap < 50, round(gap, 1))
check("折线终点就是齐齐哈尔", hav(R["path"][-1], R["stops"][-1]) < 1,
      (R["path"][-1], R["stops"][-1]))

# ---------------------------------------------------------------------------
print("== 3) 后端：真实走向不再西绕 / 终点落在齐齐哈尔")
# ---------------------------------------------------------------------------
pts, info = railosm.build(R)
check("拼得出真实走向", bool(pts) and len(pts) > 1000, len(pts or []))
end_off = hav(pts[-1], R["stops"][-1])
check("★ 终点在齐齐哈尔 5km 内", end_off < 5, round(end_off, 2))
check("★ 覆盖率 ≥ 0.98", (info.get("cover") or 0) >= 0.98, info.get("cover"))
check("★ 缺口 ≤ 10km", info.get("gapKm", 999) <= 10, info.get("gapKm"))
tailgeo = [x for x in pts if x["km"] >= 1440]
west = min(x["lon"] for x in tailgeo)
check("★ 尾段不再绕到 123.3°E（富拉尔基以西）", west >= 123.6, round(west, 3))
jumps = sorted((hav(pts[i - 1], pts[i]) for i in range(1, len(pts))), reverse=True)
check("★ 没有 48km 的西绕折返", jumps[0] <= 42, round(jumps[0], 1))
check("四条经由线路都在", len(info.get("lines") or []) == 4, info.get("lines"))

# ---------------------------------------------------------------------------
print("== 4) 其它车次不退化")
# ---------------------------------------------------------------------------
for code, mincover, maxgap in (("Z75", 0.90, 150), ("N1534", 0.98, 20),
                              ("N1531", 0.98, 20), ("G1", 0.99, 1)):
    r = railcore.build_route_payload(code, DATE, cache_only=True, with_path=True)
    q, inf = railosm.build(r)
    check("%s 覆盖率 ≥ %.2f" % (code, mincover),
          bool(q) and (inf.get("cover") or 0) >= mincover, inf.get("cover"))
    check("%s 缺口 ≤ %dkm" % (code, maxgap),
          inf.get("gapKm", 999) <= maxgap, inf.get("gapKm"))
    if q:
        check("%s 终点仍贴终到站" % code, hav(q[-1], r["stops"][-1]) < 5,
              round(hav(q[-1], r["stops"][-1]), 2))

# ---------------------------------------------------------------------------
print("== 5) HTTP 接口")
# ---------------------------------------------------------------------------
# 本脚本验证的是「原画法」契约（与上面直接调 railosm.build 的 info 对齐）；
# 默认 mode 现在是用户口径的「按确认线路」画法，见 test_route_shape.py。
O = get("/api/route/osm", code=CODE, date=DATE, mode="legacy")
check("/api/route/osm ok（原画法仍可用）", O.get("ok") is True, O.get("error"))
check("接口覆盖率与本地一致", abs(O.get("cover", 0) - info["cover"]) < 0.002,
      (O.get("cover"), info["cover"]))
check("接口终点在齐齐哈尔", hav(O["path"][-1], R["stops"][-1]) < 5,
      round(hav(O["path"][-1], R["stops"][-1]), 2))
check("接口尾段不再西绕",
      min(x["lon"] for x in O["path"] if x["km"] >= 1440) >= 123.6,
      round(min(x["lon"] for x in O["path"] if x["km"] >= 1440), 3))
A = get("/api/route", code=CODE, date=DATE, cache=1)
check("/api/route 通过站含红旗营",
      any(x["name"] == "红旗营" for x in A["lineStops"]),
      [x["name"] for x in A["lineStops"]][-12:])

# ---------------------------------------------------------------------------
print("== 6) 浏览器：切到真实走向")
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
    st = pg.evaluate("""()=>({geo:S.geoOn, path:S.route.path.length,
        stops:S.route.stops.length, expanded:S.route.expandedPath.length})""")
    check("默认是站点直连", st["geo"] is False and st["path"] == 6, st)
    check("展开折线已带跨线中间站", st["expanded"] >= 70, st["expanded"])
    pg.click("#geoBtn")
    pg.wait_for_timeout(4500)
    b = pg.evaluate("""()=>{const r=S.route;
        const p=r.path; const t=p.filter(q=>q.km>=1440);
        return {geo:S.geoOn, n:p.length, btn:document.querySelector('#geoBtn').textContent,
                note:document.querySelector('#geoNote').textContent,
                end:p[p.length-1], west:Math.min.apply(null,t.map(q=>q.lon)),
                stops:r.lineStops.length};}""")
    check("切到真实走向", b["geo"] is True and "站点直连" in b["btn"], b["btn"])
    check("真实走向上千点", b["n"] >= 1000, b["n"])
    check("前端末点在齐齐哈尔", hav(b["end"], R["stops"][-1]) < 5, b["end"])
    check("前端尾段不再西绕", b["west"] >= 123.6, round(b["west"], 3))
    check("真实走向仍只标 6 个停车站", b["stops"] == 6, b["stops"])
    check("页面无 JS 错误", not errs, errs[:2])
    br.close()

print("\n通过 %d / 失败 %d" % (ok, fail))
if fail:
    print("失败项：", "；".join(bad))
    raise SystemExit(1)
raise SystemExit(0)
