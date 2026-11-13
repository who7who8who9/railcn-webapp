# -*- coding: utf-8 -*-
"""
回归：车次详情「按真实走向」的**全局规则**（不许变回只对个别车次生效）

2026-11-12 用户要求「把这次的修改规则固定下来，用到所有的线路车次详情的
真实线路上」。N17 那次修的两条规则本来写在通用函数里，但实测只覆盖到
943 个经由段里的 53 个（5.1%，只有整理台确认线路才取得到本段站序），
另有 7 趟车被二次裁剪后覆盖率反而下降。现在固化为三条：

1. **唯一站序入口**：`cnrail.via_segment_polyline()` 与 `path_stations()`
   的跨线链同源于 `_via_chain()`（确认站序优先 / 线路库兜底 / 线路所就近落点），
   `railosm._seg_polyline()` 只做 20 秒缓存，不再另写一份「只认确认线路」的解析。
2. **可靠性门槛**：`_seg_hug()` < 0.60 不裁；兜底站序（非确认线）抬到 0.75。
3. **全局不退化闸**：裁完和「不裁」各拼一次，覆盖率掉 >2 个百分点或终点
   多偏 >5 km 就整趟退回不裁；覆盖率相同则保留裁剪（N17 未裁会西绕一圈）。
   另外 `railcore.route_osm()` 的进程缓存按两库 mtime 指纹失效，
   用户改了站序 / 几何 / 人工经由后，已经看过的车次也会用新规则重拼。

跑法：python scripts/test_route_osm_global.py   （先起 server --port 8686）
只读，不写库。
"""
from __future__ import annotations

import io
import json
import math
import os
import re
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
        print("  ok   %s%s" % (label, ("  " + str(val))[:110] if val != "" else ""))
    else:
        fail += 1
        bad.append(label)
        print("  失败 %s%s" % (label, ("  " + str(val))[:110] if val != "" else ""))


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


def end_off(pts, R) -> float:
    return hav(pts[-1], R["stops"][-1]) if pts else 1e9


# ---------------------------------------------------------------------------
print("== 1) 静态护栏：三条规则不许被改回去")
# ---------------------------------------------------------------------------
cn = src("cnrail.py")
osm = src("railosm.py")
core = src("railcore.py")

check("唯一站序入口存在", "def via_segment_polyline(" in cn)
check("入口与跨线链同源", '_via_chain([{"line": name, "from": frm, "to": to}])' in cn)
check("入口标出是否确认站序", '"confirmed": ("整理台" in label)' in cn)
check("点序是 [lon,lat] 且不足两点返回 None",
      'p = (float(lon), float(lat))' in cn and "len(pts) < 2" in cn)
check("railosm 委托给唯一入口（不再自己读 confirmed）",
      "cnrail.via_segment_polyline" in osm and "confirmed.detail_payload" not in osm)
check("站序缓存仍是 20 秒", "now - _SEGPOLY_TS > 20" in osm)
check("可靠性门槛在", "hug >= 0.60 and hug >= need" in osm)
check("非确认线抬高门槛",
      '0.60 if all(m["confirmed"] for m in metas) else 0.75' in osm)
check("全局不退化闸在", "def _clipped_worse(" in osm and "COVER_DROP" in osm
      and "END_WORSE_KM" in osm and "_assemble(base_parts)" in osm)
check("裁过的线名回报出来", 'info["clipped"]' in osm)
check("真实走向缓存按两库 mtime 失效",
      "_osm_data_epoch" in core and "_OSM_EPOCH" in core
      and "_OSM_CACHE.clear()" in core and "linedesk.db-wal" in core)
for nm, s in (("cnrail", cn), ("railosm", osm), ("railcore", core)):
    check("%s 没有按车次号写死的分支" % nm,
          not re.search(r"(?:if|elif|==|!=)[^\n]{0,60}[\"']N17[\"']", s))

# ---------------------------------------------------------------------------
print("== 2) 唯一站序入口：确认线路 / 非确认线路都能出折线")
# ---------------------------------------------------------------------------
import cnrail              # noqa: E402
import confirmed           # noqa: E402
import railcore            # noqa: E402
import railosm             # noqa: E402

cnrail._ensure()
conf_names = confirmed.names()
check("整理台确认线路非空", len(conf_names) > 10, len(conf_names))

conf_hit = None
for nm in sorted(conf_names):
    d = confirmed.detail_payload(nm)
    st = [s for s in ((d or {}).get("stations") or []) if s.get("lat") is not None]
    if len(st) >= 3:
        m = cnrail.via_segment_polyline(nm, st[0]["name"], st[-1]["name"])
        if m:
            conf_hit = (nm, m)
            break
check("确认线路能取到本段站序", bool(conf_hit),
      None if not conf_hit else (conf_hit[0], len(conf_hit[1]["pts"])))
if conf_hit:
    check("确认线路标 confirmed=True", conf_hit[1]["confirmed"] is True,
          conf_hit[1]["label"])
    check("确认线路点序 [lon,lat]", abs(conf_hit[1]["pts"][0][0]) > 90
          and abs(conf_hit[1]["pts"][0][1]) < 60, conf_hit[1]["pts"][0])

# 非确认线路：从线路档案里现挑一条带来源后缀 / 库里独有的线，别写死线名
lib_hit = None
lib_args = None
for ln in (cnrail._LINES or []):
    nm = ln.get("name") or ""
    st = ln.get("stations") or []
    if nm in conf_names or len(st) < 3:
        continue
    args = (nm, st[0]["name"], st[-1]["name"])
    m = cnrail.via_segment_polyline(*args)
    if m and not m["confirmed"]:
        lib_hit, lib_args = (nm, m), args
        break
if lib_hit is None:
    # 底表清空后 _LINES 可能为空，但来源版本名仍然能经 line() 兜底解析
    for nm in ("京沪高速线（底表）", "大河沿联络线（china-emu）", "石太线（底表）"):
        args = (nm, "北京南", "上海虹桥")
        m = cnrail.via_segment_polyline(*args)
        if m and not m["confirmed"]:
            lib_hit, lib_args = (nm, m), args
            break
check("非确认线路也能取到本段站序（规则不是只给确认线路）", bool(lib_hit),
      None if not lib_hit else (lib_hit[0], len(lib_hit[1]["pts"]), lib_hit[1]["label"]))
if lib_hit:
    check("非确认线路标 confirmed=False", lib_hit[1]["confirmed"] is False)
    a = railosm._seg_polyline(*lib_args)
    check("railosm 委托结果与入口一致", bool(a) and a["pts"] == lib_hit[1]["pts"],
          None if not a else len(a["pts"]))

check("认不出的端点返回 None（调用方必须退回不裁剪）",
      cnrail.via_segment_polyline("京广线", "不存在的站甲", "不存在的站乙") is None)

# ---------------------------------------------------------------------------
print("== 3) 逐趟车：推广规则后不许比「不裁」更差")
# ---------------------------------------------------------------------------
CODES = ("N17", "Z75", "N1534", "N1531", "G1", "V6061",
         "D40", "V1118", "V1524", "V1283", "V717", "C201", "D1035")
orig_poly = railosm._seg_polyline
clipped_all = []
for code in CODES:
    R = railcore.build_route_payload(code, DATE, cache_only=True, with_path=True)
    cur, ci = railosm.build(R)
    clipped_all.extend(ci.get("clipped") or [])
    railosm._seg_polyline = lambda *a, **k: None
    try:
        raw, ri = railosm.build(R)
    finally:
        railosm._seg_polyline = orig_poly
    check("%s 裁后仍拼得出来" % code, bool(cur) or not raw,
          ci.get("err") if not cur else "")
    check("%s 覆盖率不退步 >2 个点" % code,
          (ci.get("cover") or 0) >= (ri.get("cover") or 0) - 0.021,
          (ci.get("cover"), ri.get("cover")))
    check("%s 终点不多偏 >5km" % code,
          end_off(cur, R) <= end_off(raw, R) + 5.1,
          (round(end_off(cur, R), 2), round(end_off(raw, R), 2)))
    check("%s 回报 clipped 名单" % code, isinstance(ci.get("clipped"), list),
          ci.get("clipped"))

check("样本里确实有车次被按本段站序裁过", len(clipped_all) >= 3, clipped_all)
check("规则覆盖到非确认线路",
      any(x not in conf_names for x in clipped_all),
      sorted(set(clipped_all) - set(conf_names))[:6])

# ---------------------------------------------------------------------------
print("== 4) N17 原修复不能被推广改坏")
# ---------------------------------------------------------------------------
R17 = railcore.build_route_payload("N17", DATE, cache_only=True, with_path=True)
p17, i17 = railosm.build(R17)
check("N17 cover ≥0.98", (i17.get("cover") or 0) >= 0.98, i17.get("cover"))
check("N17 缺口 ≤10km", i17.get("gapKm", 999) <= 10, i17.get("gapKm"))
check("N17 终点在齐齐哈尔 5km 内", end_off(p17, R17) < 5, round(end_off(p17, R17), 2))
tail = [x for x in p17 if x["km"] >= 1440]
check("N17 尾段不西绕到 123.6°E 以西",
      min(x["lon"] for x in tail) >= 123.6,
      round(min(x["lon"] for x in tail), 3))
jumps = sorted((hav(p17[i - 1], p17[i]) for i in range(1, len(p17))), reverse=True)
check("N17 无 48km 西绕折返", jumps[0] <= 42, round(jumps[0], 1))

# ---------------------------------------------------------------------------
print("== 5) 真实走向缓存：改了库就重拼")
# ---------------------------------------------------------------------------
with railcore._OSM_LOCK:
    railcore._OSM_CACHE.clear()
    railcore._OSM_CACHE[("SENTINEL", DATE)] = {"ok": True, "path": [], "sentinel": True}
    railcore._OSM_EPOCH = None
first = railcore.route_osm("N17", DATE)
with railcore._OSM_LOCK:
    gone = ("SENTINEL", DATE) not in railcore._OSM_CACHE
check("epoch 变化会清掉旧缓存（旧规则不会赖着不走）", gone)
check("route_osm 仍返回 N17 真实走向", first.get("ok") is True, first.get("error"))
# 2026-11-12 起默认走「按确认线路」画法：逐段来源在 segInfo 里，
# clipped 只有旧画法（mode=legacy）才给。
check("默认结果是按确认线路画法", first.get("mode") == "confirmed", first.get("mode"))
check("结果逐段带来源（segInfo）", isinstance(first.get("segInfo"), list) and first["segInfo"],
      (first.get("segInfo") or [])[:2])
check("legacy 画法仍带 clipped 名单",
      "clipped" in railcore.route_osm("N17", DATE, mode="legacy"))
with railcore._OSM_LOCK:
    railcore._OSM_CACHE.clear()
    railcore._OSM_EPOCH = None
a = railcore.route_osm("N17", DATE)
b = railcore.route_osm("N17", DATE)
check("同一 epoch 下命中缓存", a is b)
check("缓存命中时 cover 不变", a.get("cover") == b.get("cover"), a.get("cover"))

# ---------------------------------------------------------------------------
print("== 6) HTTP：所有车次走同一个出口")
# ---------------------------------------------------------------------------
for code in ("N17", "D40", "G1"):
    # 本脚本验证的是贪心覆盖「原画法」；默认 mode 已切成「按确认线路」，
    # 用 mode=legacy 显式走旧入口，保证旧逻辑一行没删、结果完全一致。
    o = get("/api/route/osm", code=code, date=DATE, mode="legacy")
    R = railcore.build_route_payload(code, DATE, cache_only=True, with_path=True)
    q, qi = railosm.build(R)
    check("%s /api/route/osm ok（原画法）" % code, o.get("ok") is True, o.get("error"))
    check("%s 接口 cover 与本地一致" % code,
          abs(o.get("cover", 0) - (qi.get("cover") or 0)) < 0.002,
          (o.get("cover"), qi.get("cover")))
    check("%s 接口带 clipped 名单" % code, "clipped" in o, o.get("clipped"))

print("\n通过 %d / 失败 %d" % (ok, fail))
if fail:
    print("失败项：", "；".join(bad))
    raise SystemExit(1)
raise SystemExit(0)
