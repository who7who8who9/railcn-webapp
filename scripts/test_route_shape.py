# -*- coding: utf-8 -*-
"""
回归：车次详情「按真实走向」= 按整理台确认线路站序画（2026-11-12 用户口径）

用户三条规则：
  1. 线路 / 车站 / 公里数全部按「车站/线路」页能查到的确认线路站序与累计 km；
  2. 跨线全在中转站转线、断裂自动连接最近两点、长出截至起讫、
     不足用首/末点连接起讫；
  3. 上下行分线只选一条，含停站的那条优先。
原画法 `railosm.build()` 一行不删，`mode=legacy` / 前端下拉随时切回；
未提及的（非确认线路段、经由空隙）沿用旧画法同一区间的几何。

跑法：python scripts/test_route_shape.py   （先起 server --port 8686）
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
DATE = "2026-11-25"

ok = fail = 0
bad: list = []


def check(label: str, cond: bool, val="") -> None:
    global ok, fail
    if cond:
        ok += 1
        print("  ok   %s%s" % (label, ("  " + str(val))[:90] if val != "" else ""))
    else:
        fail += 1
        bad.append(label)
        print("  失败 %s%s" % (label, ("  " + str(val))[:90] if val != "" else ""))


def get(path: str, **kw) -> dict:
    qs = ("?" + urllib.parse.urlencode(kw)) if kw else ""
    with urllib.request.urlopen(BASE + path + qs, timeout=240) as r:
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


def ll(p: dict):
    return (p["lon"], p["lat"])


# ---------------------------------------------------------------------------
print("== 1) 静态护栏：新画法落地、旧画法原样保留")
# ---------------------------------------------------------------------------
shape = src("routeshape.py")
osm = src("railosm.py")
core = src("railcore.py")
srv = src("server.py")
app = src(os.path.join("static", "app.js"))

check("新画法独立模块存在", os.path.exists(os.path.join(ROOT, "webapp", "routeshape.py")))
check("站序统一走 cnrail._via_chain（确认优先）", "_via_chain(" in shape
      and "confirmed.detail_payload" in src("cnrail.py"))
check("确认线路白名单判定", "confirmed.names()" in shape)
check("规则 2a 每段独立、中转站转线", "def _seg_points(" in shape
      and "_key(prev_to) != _key(seg.get(\"from\")" in shape)
check("规则 2b 断裂连接最近端点", "def _chain_parts(" in shape
      and "d_tail < d_head" in shape)
check("规则 2c 按站序投影裁到 from→to", "def _seg_candidates(" in shape
      and "-tol <= s <= span + tol" in shape)
check("规则 2d 不足用首/末点连接起讫", "chain_pts.insert(0, start)" in shape
      and "chain_pts.append(end)" in shape)
check("规则 3 分线优先含停站", "def _pick_branches(" in shape
      and "_stop_hits(c, seg_stops)" in shape)
check("规则 1 公里数按站序累加", "def _remap_km(" in shape
      and "def _km_span(" in shape)
check("没提及的沿用旧画法几何", "def _legacy_slice(" in shape
      and "def _fill_gap(" in shape and "railosm._geom_of(line)" in shape)
check("旧画法原封不动保留", "def _clipped_worse(" in osm
      and "COVER_DROP" in osm and "def _assemble(" in osm
      and "confirmed.geom(name)" in osm)
check("railcore 按 mode 分流 + 缓存按 mode 分键",
      "mode == \"confirmed\"" in core and "routeshape.build(payload" in core
      and "railosm.build(payload)" in core)
check("server 透传 mode", "mode=one(\"mode\")" in srv)
check("前端有画法下拉与 mode 参数", "id=\"geoMode\"" in app
      and "function geoMode()" in app and "mode: geoMode()" in app)
check("备注标明当前画法", "按确认线路" in app and "原画法" in app)

# ---------------------------------------------------------------------------
print("== 2) 后端：确认线路站序 / km 累加 / 中转站 / 裁切 / 分线")
# ---------------------------------------------------------------------------
import railcore                                            # noqa: E402
import railosm                                             # noqa: E402
import routeshape                                          # noqa: E402
import confirmed                                           # noqa: E402

R17 = railcore.build_route_payload("N17", DATE, cache_only=True)
R75 = railcore.build_route_payload("Z75", DATE, cache_only=True)

seg0 = R17["viaSegs"][0]
ch = routeshape._seg_chain(seg0)
check("确认线路段取到整理台站序", bool(ch) and ch["confirmed"] is True,
      None if not ch else (ch["label"], len(ch["stations"])))
kms = [s.get("km") for s in ch["stations"] if s.get("km") is not None]
check("站序 km 单调递增（公里数累加基准）",
      len(kms) > 10 and all(b >= a for a, b in zip(kms, kms[1:])),
      (kms[0], kms[-1], len(kms)))
check("站序 km 跨度≈车次该段里程",
      abs((kms[-1] - kms[0]) - 1249) < 1, kms[-1] - kms[0])

# 规则 2a：相邻两段在同一中转站首尾相接
prev = None
joints = []
for seg in R17["viaSegs"]:
    pts, meta = routeshape._seg_points(seg, R17, prev or 0.0)
    if not pts:
        continue
    if prev is not None:
        pass
    joints.append((seg["from"], ll(pts[0]), seg["to"], ll(pts[-1])))
    prev = pts[-1]["km"]
check("每段首点严格落在段起点站", all(
    routeshape._dist(a, routeshape._coord_of(n, R17["stops"], None, True)) < 0.7
    for n, a, _, _ in joints if routeshape._coord_of(n, R17["stops"], None, True)))
check("相邻段在同一中转站转线", all(
    routeshape._dist(joints[i][3], joints[i + 1][1]) < 0.7
    for i in range(len(joints) - 1)),
    [round(routeshape._dist(joints[i][3], joints[i + 1][1]), 3)
     for i in range(len(joints) - 1)])

# 规则 2c：整条京广线 5827 点，裁到「良乡→石家庄」后应远少于全量
seg_g = next(s for s in R75["viaSegs"] if s["line"] == "京广线")
cg = routeshape._seg_chain(seg_g)
poly = cg["poly"]
cum = routeshape._poly_cum(poly)
span = cum[-1]
cands = routeshape._seg_candidates("京广线", poly, cum, span)
n_cand = sum(len(c["pts"]) for c in cands)
full = sum(len(p) for p in (railosm._geom_of("京广线") or {}).get("parts") or [])
check("长出起讫的几何被截至本段", 0 < n_cand < full * 0.5, (n_cand, full))
check("分线候选被合并成较少段", len(routeshape._pick_branches(
    cands, [], span)) <= len(cands), (len(cands),
                                     len(routeshape._pick_branches(cands, [], span))))

# 规则 2b：构造两段断裂，验证自动连接最近的一对端点
poly2 = [(116.0, 39.0), (116.5, 39.0), (117.0, 39.0)]
cum2 = routeshape._poly_cum(poly2)
c1 = routeshape._mk_cand([(116.0, 39.0), (116.2, 39.0)], [0, 20], 0.5)
c2 = routeshape._mk_cand([(116.25, 39.0), (116.5, 39.0)], [25, 50], 0.5)
chain2, links2, broken2 = routeshape._chain_parts([c1, c2], poly2, cum2)
check("断裂自动连成一条折线", len(chain2) == 4 and len(links2) == 1
      and broken2 == 0, (len(chain2), links2, broken2))
check("断点取的是最近的一对端点",
      chain2[1] == (116.2, 39.0) and chain2[2] == (116.25, 39.0), chain2)

# 规则 3：两条重叠分线，选含停站的那条（停站贴 A，B 在 0.3° 外 ≈33km）
A = routeshape._mk_cand([(116.0, 39.0), (116.3, 39.02), (116.5, 39.0)],
                        [0, 25, 50], 0.5)
B = routeshape._mk_cand([(116.0, 39.3), (116.3, 39.32), (116.5, 39.3)],
                        [0, 25, 50], 0.5)
pick = routeshape._pick_branches([A, B], [(116.3, 39.02)], 50)
check("上下行分线只留一条", len(pick) == 1, len(pick))
check("含停站的那条被选中", bool(pick) and pick[0]["pts"] == A["pts"],
      None if not pick else pick[0]["pts"][:2])
pick0 = routeshape._pick_branches([A, B], [], 50)
check("没停站时按覆盖/贴合度选一条", len(pick0) == 1, len(pick0))

# ---------------------------------------------------------------------------
print("== 3) 整趟车：新画法结果")
# ---------------------------------------------------------------------------
def evaluate(code: str, r: dict):
    lp, li = railosm.build(r)
    np_, ni = routeshape.build(r, legacy=lp)
    if not np_:
        return None, ni
    js = [hav(a, b) for a, b in zip(np_, np_[1:])]
    return {"pts": np_, "info": ni, "maxjump": max(js),
            "end": hav(np_[-1], r["stops"][-1]),
            "mono": all(b["km"] >= a["km"] - 1e-6 for a, b in zip(np_, np_[1:])),
            "k0": np_[0]["km"], "k1": np_[-1]["km"]}, ni


for code, r in (("N17", R17), ("Z75", R75)):
    e, ni = evaluate(code, r)
    check("%s 新画法拼得出" % code, bool(e), None if not e else len(e["pts"]))
    if not e:
        continue
    check("%s 按确认线路段生效" % code, (ni.get("shapeSegs") or 0) >= 1,
          (ni.get("confirmedSegs"), ni.get("shapeSegs"), ni.get("segments")))
    check("%s 里程单调递增" % code, e["mono"])
    check("%s 首尾 = 0 / 全程" % code,
          abs(e["k0"]) < 0.01 and abs(e["k1"] - r["totalKm"]) < 0.5,
          (e["k0"], e["k1"], r["totalKm"]))
    check("%s 终点落在终到站" % code, e["end"] < 1.0, round(e["end"], 2))
    check("%s 无 >60km 假长直线" % code, e["maxjump"] < 60,
          round(e["maxjump"], 1))
    check("%s 上千几何点" % code, len(e["pts"]) > 1000, len(e["pts"]))

e17, _ = evaluate("N17", R17)
check("N17 尾段不再往满洲里西绕",
      min(q["lon"] for q in e17["pts"] if q["km"] >= 1440) >= 123.6,
      round(min(q["lon"] for q in e17["pts"] if q["km"] >= 1440), 3))

# D40：经由空隙「洛阳→新乡」+ 尾部「保定→北京西」沿用旧画法几何，不拉大直线
R40 = railcore.build_route_payload("D40", DATE, cache_only=True)
e40, i40 = evaluate("D40", R40)
check("D40 空隙/尾部没有 >60km 直线", bool(e40) and e40["maxjump"] < 60,
      None if not e40 else round(e40["maxjump"], 1))

# G1 全是非确认线路：新画法主动让位，route_osm 自动回退原画法
RG1 = railcore.build_route_payload("G1", DATE, cache_only=True)
lp1, _ = railosm.build(RG1)
np1, ni1 = routeshape.build(RG1, legacy=lp1)
check("没有确认线路时新画法让位", np1 is None, ni1.get("err"))

# ---------------------------------------------------------------------------
print("== 4) HTTP：默认新画法 / mode=legacy 原画法 / 回退")
# ---------------------------------------------------------------------------
n = get("/api/route/osm", code="N17", date=DATE)
o = get("/api/route/osm", code="N17", date=DATE, mode="legacy")
check("默认按确认线路画法", n.get("ok") is True and n.get("mode") == "confirmed"
      and (n.get("shapeSegs") or 0) >= 1, (n.get("mode"), n.get("shapeSegs")))
check("mode=legacy 返回原画法", o.get("ok") is True and o.get("mode") == "legacy"
      and "clipped" in o, (o.get("mode"), o.get("cover")))
check("两种画法结果不同（不是同一份缓存）",
      len(n.get("path") or []) != len(o.get("path") or []),
      (len(n.get("path") or []), len(o.get("path") or [])))
check("新画法终点在齐齐哈尔",
      hav(n["path"][-1], R17["stops"][-1]) < 1.0)
g1 = get("/api/route/osm", code="G1", date=DATE)
check("无确认线路的车次自动回退并标明",
      g1.get("ok") is True and g1.get("fallback") == "legacy"
      and g1.get("mode") == "legacy", (g1.get("fallback"), g1.get("cover")))

# ---------------------------------------------------------------------------
print("== 5) 浏览器：画法下拉可来回切、页面无 JS 错误")
# ---------------------------------------------------------------------------
from playwright.sync_api import sync_playwright                 # noqa: E402

with sync_playwright() as pw:
    br = pw.chromium.launch(args=["--no-proxy-server"])
    pg = br.new_context(viewport={"width": 1500, "height": 960}).new_page()
    errs: list = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(BASE + "/?" + urllib.parse.urlencode({"code": "Z75", "date": DATE}),
            wait_until="load")
    pg.wait_for_selector("#geoMode", timeout=60000)
    check("默认选中「按确认线路」", pg.input_value("#geoMode") == "confirmed")
    pg.click("#geoBtn")
    pg.wait_for_function("S.geoOn === true", timeout=90000)
    check("备注标明当前画法", "按确认线路" in pg.inner_text("#geoNote"),
          pg.inner_text("#geoNote"))
    pg.select_option("#geoMode", "legacy")
    pg.wait_for_function(
        "document.querySelector('#geoNote').textContent.includes('原画法')",
        timeout=90000)
    check("切到原画法后仍是真实走向且备注正确",
          pg.evaluate("S.geoOn === true") and "原画法" in pg.inner_text("#geoNote"),
          pg.inner_text("#geoNote"))
    pg.select_option("#geoMode", "confirmed")
    pg.wait_for_function(
        "document.querySelector('#geoNote').textContent.includes('按确认线路')",
        timeout=90000)
    check("切回按确认线路可用", pg.input_value("#geoMode") == "confirmed"
          and "按确认线路" in pg.inner_text("#geoNote"))
    pg.click("#geoBtn")
    pg.wait_for_timeout(700)
    check("切回站点直连后备注清空", pg.inner_text("#geoNote") == "")
    check("页面无 JS 错误", not errs, errs[:2])
    br.close()

print("\n通过 %d 项，失败 %d 项" % (ok, fail))
if bad:
    print("失败：" + " / ".join(bad))
sys.exit(1 if fail else 0)
