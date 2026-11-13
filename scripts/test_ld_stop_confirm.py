# -*- coding: utf-8 -*-
"""回归：整理台 ②「用它并确认」不回灌站序 + ①「0 站有走向」的来源可用

2026-11-06 用户报两个问题：

A. 「站序 删除一站，然后选择用它并确认后，并没有保存成功」。
   ld_log 实测：删站 29→28，紧接着 src-stop 又写回 29 站，反复 5 次。
   根因：手工改站序后 renderPanel() 按 src_stop 自动选回来源行，
   「用它并确认」带 fill=true → fill_stops() 把来源那份完整站序灌回 ld_stop。
B. 「线路来源中选择的线路，即使没有站点只要有走向就可以被我选中使用」。
   line_route 里 rg / hdx / cnrail 有大把 n_stop=0 但 parts_gz 完整的线路
   （rg:京沪高铁 就是 0 站 / 2 段 / 1549 点），previewSource() 原来一见
   stops 为空就 toast「这个来源只有线名，没有站序」并 return，地图不画走向。

契约：
  1. 手工改站序（save_stops / reverse_stops）→ ld_line.stops_edited=1，
     line_detail() 输出 stopsEdited。
  2. confirm_stop_source(source='manual', fill=False) 只置 stop_ok=1，
     **不搬站序、不改 src_stop、不改 ld_stop.src**。
  3. 明确选外部来源 + fill=True 仍然搬站序（旧能力不能丢）。
  4. source_stops() 对 0 站有走向的来源仍返回 parts；search_source() 给 has_geom。
  5. 前端：默认选「现在的站序」、manual 通道、previewSource 允许空站序 + 有走向。
"""
import gc
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "webapp"))
import linedesk  # noqa: E402

OK = FAIL = SKIP = 0


def ck(cond, msg):
    global OK, FAIL
    if cond:
        OK += 1
        print("  OK   " + msg)
    else:
        FAIL += 1
        print("  FAIL " + msg)


def skip(msg):
    global SKIP
    SKIP += 1
    print("  SKIP " + msg)


STOPS3 = [
    {"name": "甲站", "km": 0, "lat": 31.0, "lon": 121.0, "src": "manual"},
    {"name": "乙站", "km": 20, "lat": 31.2, "lon": 120.5, "src": "manual"},
    {"name": "丙站", "km": 50, "lat": 31.4, "lon": 120.0, "src": "manual"},
]
SOURCE_STOPS = [
    {"name": "甲站", "km": 0, "lat": 31.0, "lon": 121.0},
    {"name": "乙站", "km": 20, "lat": 31.2, "lon": 120.5},
    {"name": "丁站", "km": 35, "lat": 31.3, "lon": 120.2},
    {"name": "丙站", "km": 50, "lat": 31.4, "lon": 120.0},
]


def names(d):
    return [s["name"] for s in (d.get("stops") or [])]


def raw(lid):
    with linedesk.conn() as c:
        return dict(c.execute("SELECT * FROM ld_line WHERE id=?", (lid,)).fetchone())


print("=== 整理台 ②「用它并确认」不回灌站序 ===")
orig_db = linedesk.DB_PATH
tmpdir = tempfile.TemporaryDirectory(prefix="railcn-ld-stop-",
                                     ignore_cleanup_errors=True)
orig_source_stops = linedesk.source_stops
linedesk.DB_PATH = os.path.join(tmpdir.name, "linedesk.db")
try:
    linedesk.init()
    with linedesk.conn() as c:
        lid = c.execute(
            "INSERT INTO ld_line(name,family,kind,status) VALUES(?,?,?,?)",
            ("__回归站序确认线__", "__回归站序确认线__", "gs", "draft")).lastrowid

    # 方便断言：外部来源用假数据，不依赖正式 rail.db 里有没有这条线
    linedesk.source_stops = lambda s, n: (
        {"source": s, "name": n, "stops": SOURCE_STOPS, "coordN": 4,
         "parts": [], "totalKm": 50.0} if s == "custom" and n == "假来源"
        else orig_source_stops(s, n))

    d = linedesk.save_stops(lid, STOPS3)
    ck(names(d) == ["甲站", "乙站", "丙站"], "save_stops 落下 3 站")
    ck(raw(lid)["stops_edited"] == 1, "save_stops 标记 stops_edited=1")
    ck(d["stopsEdited"] is True, "line_detail 输出 stopsEdited")
    ck(raw(lid)["stop_ok"] == 0, "手工改站序会把 stop_ok 打回 0")

    d = linedesk.confirm_stop_source(lid, "manual", "", False)
    ck(names(d) == ["甲站", "乙站", "丙站"], "确认本线站序后站序一个字没变")
    ck(raw(lid)["stop_ok"] == 1, "确认本线站序 → stop_ok=1")
    ck(raw(lid)["stops_edited"] == 0, "确认后清掉 stops_edited")
    ck(raw(lid)["src_stop"] in ("", "manual:__回归站序确认线__"),
       "确认本线站序不会凭空写一个新的来源标记")
    with linedesk.conn() as c:
        srcs = [r[0] for r in c.execute(
            "SELECT src FROM ld_stop WHERE line_id=? ORDER BY seq", (lid,))]
    ck(srcs == ["manual"] * 3, "确认本线站序不会把每站 src 改成来源")

    # 主回归：模拟「从来源搬 4 站 → 手工删 1 站 → 点用它并确认」
    linedesk.fill_stops(lid, "custom", "假来源")
    ck(names(linedesk.line_detail(lid)) == ["甲站", "乙站", "丁站", "丙站"],
       "fill_stops 从来源搬来 4 站")
    ck(raw(lid)["stops_edited"] == 0, "刚从来源搬来的站序不算人工改动")
    ck(raw(lid)["src_stop"] == "custom:假来源", "fill_stops 记下来源标记")
    ck(raw(lid)["stop_ok"] == 0, "fill_stops 后仍要人工确认")

    kept = [s for s in SOURCE_STOPS if s["name"] != "丁站"]
    linedesk.save_stops(lid, kept)
    ck(names(linedesk.line_detail(lid)) == ["甲站", "乙站", "丙站"],
       "手工删掉「丁站」后只剩 3 站")
    ck(raw(lid)["stops_edited"] == 1, "删站后 stops_edited=1（前端据此默认选本线站序）")

    d = linedesk.confirm_stop_source(lid, "manual", "", False)
    ck(names(d) == ["甲站", "乙站", "丙站"],
       "★ 删站后「用它并确认」不再把 4 站灌回来（本次 bug）")
    ck("丁站" not in names(d), "★ 删掉的「丁站」没有复活")
    ck(raw(lid)["stop_ok"] == 1, "删站后确认成功：stop_ok=1")
    ck(raw(lid)["src_stop"] == "custom:假来源", "来源标记保持不变")
    ck(raw(lid)["stops_edited"] == 0, "确认后 stops_edited 归零")

    # 旧能力：明确点了外部来源行，要能覆盖回来源那份
    d = linedesk.confirm_stop_source(lid, "custom", "假来源", True)
    ck(names(d) == ["甲站", "乙站", "丁站", "丙站"],
       "明确选来源行 + fill=true 仍能覆盖回 4 站")
    ck(raw(lid)["stop_ok"] == 1 and raw(lid)["stops_edited"] == 0,
       "来源覆盖后状态正常")

    # 反向站序也算人工改动，但 stop_ok 不变（口径同主站）
    d = linedesk.reverse_stops(lid)
    ck(names(d) == ["丙站", "丁站", "乙站", "甲站"], "反向后站序整条倒过来")
    ck(raw(lid)["stops_edited"] == 1, "反向也标记 stops_edited=1")
    ck(raw(lid)["stop_ok"] == 1, "反向不动 stop_ok")
    ck(names(linedesk.confirm_stop_source(lid, "manual", "", False))
       == ["丙站", "丁站", "乙站", "甲站"], "反向后确认本线站序不会翻回去")

    # 0 站 / 1 站不能确认
    linedesk.save_stops(lid, [STOPS3[0]])
    try:
        linedesk.confirm_stop_source(lid, "manual", "", False)
        ck(False, "少于 2 站应拒绝确认")
    except ValueError as e:
        ck("至少两站" in str(e) or "至少 2" in str(e), "少于 2 站拒绝确认：" + str(e))

    # 未知来源仍要拦
    linedesk.save_stops(lid, STOPS3)
    try:
        linedesk.confirm_stop_source(lid, "nope", "x", False)
        ck(False, "不认识的来源应拒绝")
    except ValueError as e:
        ck("不认识" in str(e), "不认识的来源被拦住")

    # ① 重新确认不能抹掉 ③ 合并保存定下的走向标记（15:05 用户实测踩到）
    linedesk.save_stops(lid, STOPS3)
    with linedesk.conn() as c:
        c.execute("UPDATE ld_line SET geom_src=?, geom_name=? WHERE id=?",
                  ("route:rg", "京沪高铁", lid))
    linedesk.set_line_sources(lid, [{"source": "rg", "name": "京沪高铁"}])
    ck(raw(lid)["geom_src"] == "route:rg" and raw(lid)["geom_name"] == "京沪高铁",
       "★ ① 重新确认不会清掉 ③ 合并保存的 route:rg 走向")
    with linedesk.conn() as c:
        c.execute("UPDATE ld_line SET geom_src='geojson', geom_name='导入' WHERE id=?",
                  (lid,))
    linedesk.set_line_sources(lid, [{"source": "rg", "name": "京沪高铁"}])
    ck(raw(lid)["geom_src"] == "" and raw(lid)["geom_name"] == "",
       "① 重新确认仍会作废「导入走向」留下的 geojson 标记")
finally:
    linedesk.source_stops = orig_source_stops
    linedesk.DB_PATH = orig_db
    gc.collect()
    tmpdir.cleanup()


print()
print("=== ①「0 站但有走向」的来源可用 ===")
if not os.path.exists(linedesk.RAIL_DB):
    skip("正式 rail.db 不存在，跳过 line_route 检查")
else:
    d = orig_source_stops("rg", "京沪高铁")
    ck(not (d.get("stops") or []), "rg:京沪高铁 本来就是 0 站")
    ck(bool(d.get("parts")), "★ 0 站的来源照样带得出走向 parts")
    ck(all(len(p) >= 2 for p in d["parts"]), "走向每段至少 2 个点")
    ck(d.get("source") == "rg", "来源标记仍是 rg")

    items = [x for x in linedesk.search_source("京沪高铁")
             if x["source"] == "rg" and x["name"] == "京沪高铁"]
    if items:
        ck(items[0]["n_stop"] == 0, "search_source 如实给出 0 站")
        ck(items[0]["has_geom"] is True, "★ search_source 标出 has_geom（① 据此可挑）")
    else:
        skip("来源清单里没有 rg:京沪高铁")

    osm = orig_source_stops("osm", "京沪高铁")
    ck(not (osm.get("stops") or []), "OSM 线名没有站序")
    ck(bool(osm.get("parts")), "OSM 线名也能按名字解析出走向给①预览")


print()
print("=== 前端 / 路由静态检查 ===")
js = open(os.path.join(ROOT, "webapp", "static", "linedesk.js"),
          encoding="utf-8").read()
py = open(os.path.join(ROOT, "webapp", "linedesk.py"), encoding="utf-8").read()
sv = open(os.path.join(ROOT, "webapp", "server.py"), encoding="utf-8").read()
ck("kind = 'line'" in js and "kind === 'stop' && !stops.length" in js,
   "previewSource 区分①/②：只有走向的来源在①不报错")
ck("!stops.length && !parts.length" in js, "既无站序也无走向才提示用不了")
ck("src = 'manual'; nm = ''; fill = false;" in js,
   "「本线现在的站序」走 manual + fill=false")
ck("S.pick.stop = { self: true };" in js, "站序编辑后默认选中本线站序")
ck("!d.stopsEdited" in js, "stopsEdited 时不自动选回来源行")
ck("0 站 · 仅走向" in js, "① 候选行标注「0 站 · 仅走向」")
ck("x.has_geom" in js, "+ 收一条 的搜索结果显示有无走向")
ck("confirm_stop_source" in py and "current_only = src in" in py,
   "后端有「确认本线当前站序」通道")
ck("stops_edited" in py and "stopsEdited" in py, "后端有 stops_edited 标记与输出")
ck("AND geom_src='geojson'" in py, "① 只清 geojson 标记、不碰 route:xxx 合并走向")
ck('"/api/ld/src/stop"' in sv and "payload.get(\"fill\")" in sv,
   "HTTP 路由透传 fill 开关")

print()
print("绿 %d / 红 %d / 跳过 %d" % (OK, FAIL, SKIP))
sys.exit(1 if FAIL else 0)
