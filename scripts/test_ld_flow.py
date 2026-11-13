# -*- coding: utf-8 -*-
"""test_ld_flow.py —— 线路整理台新流程回归（2026-11-06 用户口径）

守这些事：
  1. 基本信息「保存」正式落库，回 saved/savedAt
  2. ① 线路来源：只保存走向（OSM 值）——不碰 ld_stop、不改规范名 / 起讫、
     清掉旧的 geom_src；② 站点来源：只保存站点 + 定位——不碰 ld_geom
  3. 站序反向：倒排 + 里程 = 原末站里程 − 原里程、新起点 0、缺的不插值；
     坐标 / 总里程 / stop_ok 都不变；再反一次回到原样
  4. GeoJSON 导入 only='stops' / only='geom' 各只写一边
  5. ③ 合并保存 = 合并 ①② + **自动定为基本线路**（HTTP，④ 已取消）
  6. 前端静态检查：④ 区块与走向下拉已移除、站序排在 ② 下、有反向按钮

跑法：python scripts/test_ld_flow.py
后端 1~4 全在临时 linedesk.db 里跑，不碰正式库；第 5 项要服务（8686），
没起就跳过，起了也只用一条临时线，跑完 unpublish + delete 清理干净。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "webapp"))

import linedesk  # noqa: E402

OK = 0
FAIL = 0
SKIP = 0


def ck(cond, msg: str) -> None:
    global OK, FAIL
    if cond:
        OK += 1
        print("  ok   " + msg)
    else:
        FAIL += 1
        print("  FAIL " + msg)


def skip(msg: str) -> None:
    global SKIP
    SKIP += 1
    print("  skip " + msg)


def snap(lid: int) -> dict:
    """把这条线在整理库里的完整状态拍下来，用来证明「某一步没碰另一部分」"""
    with linedesk.conn() as c:
        c.row_factory = sqlite3.Row
        line = dict(c.execute("SELECT * FROM ld_line WHERE id=?", (lid,)).fetchone())
        stops = [dict(x) for x in c.execute(
            "SELECT seq,name,km,lon,lat,src,note FROM ld_stop"
            " WHERE line_id=? ORDER BY seq", (lid,))]
        geom = [dict(x) for x in c.execute(
            "SELECT name,filename,total_km,n_part,n_pt,props,parts_gz,spine_gz"
            " FROM ld_geom WHERE line_id=? ORDER BY created_at", (lid,))]
    return {"line": line, "stops": stops, "geom": geom}


# ---------------------------------------------------------------------------
# 1~4：临时库，测后端职责分离与站序反向
# ---------------------------------------------------------------------------
def test_backend() -> None:
    print("[1] ① 线路来源只存走向；② 站点来源只存站点；站序反向；GeoJSON 分离导入")
    tmp = os.path.join(tempfile.gettempdir(), "test_ld_flow_%d.db" % os.getpid())
    for suf in ("", "-wal", "-shm"):
        try:
            os.remove(tmp + suf)
        except OSError:
            pass
    old_db = linedesk.DB_PATH
    linedesk.DB_PATH = tmp
    linedesk.clear_cache()
    try:
        linedesk.init()
        lid = linedesk.create_line("测试流程线", "", "回归用")["id"]
        stops = [
            {"name": "甲", "km": 0, "lat": 30.0, "lon": 120.0},
            {"name": "乙", "km": 30, "lat": 30.1, "lon": 120.1},
            {"name": "丙", "km": None, "lat": 30.2, "lon": 120.2},
            {"name": "丁", "km": 100, "lat": 30.3, "lon": 120.3},
        ]
        linedesk.save_stops(lid, stops)
        linedesk.add_alias(lid, "rg", "测试走向线")
        with linedesk.conn() as c:
            c.execute("UPDATE ld_line SET geom_src='route:rg', geom_name='旧走向',"
                      " from_sta='甲', to_sta='丁', total_km=100 WHERE id=?", (lid,))

        # --- 基本信息：保存正式落库 ---
        r = linedesk.save_line(lid, {"note": "正式保存"})
        ck(r.get("saved") is True and bool(r.get("savedAt")),
           "基本信息保存回 saved/savedAt")
        ck(r.get("note") == "正式保存", "基本信息内容真的写进 ld_line")

        # --- ① 只存走向来源 ---
        before = snap(lid)
        r = linedesk.set_line_sources(lid, [{"source": "rg", "name": "测试走向线"}])
        after = snap(lid)
        ck(after["stops"] == before["stops"], "① 没动任何一站（站序 / 里程 / 坐标全等）")
        ck(after["geom"] == before["geom"], "① 没写 ld_geom（走向几何留给 ③）")
        ln = after["line"]
        ck(ln["name"] == "测试流程线" and ln["from_sta"] == "甲"
           and ln["to_sta"] == "丁" and ln["total_km"] == 100,
           "① 没顺带改规范名 / 起讫 / 总里程")
        ck(ln["line_ok"] == 1 and ln["src_line"] == "rg:测试走向线",
           "① 只记下走向来源并把 line_ok 置 1")
        ck(ln["geom_src"] == "route:rg",
           "① 不抹掉 ③ 合并保存定下的 route:rg 走向标记（只清 geojson 导入走向）")

        # --- ② 只存站点 ---
        with linedesk.conn() as c:
            c.execute("INSERT INTO ld_geom(name,line_id,filename,total_km,n_part,n_pt,"
                      "props,created_at) VALUES(?,?,?,?,?,?,?,?)",
                      ("测试走向线", lid, "旧走向.json", 100, 1, 4, "{}", "2026-11-06"))
        before = snap(lid)
        linedesk.confirm_stop_source(lid, "manual", "", fill=False)
        after = snap(lid)
        ck(after["geom"] == before["geom"], "② 一个字节都没写 ld_geom（不保存 OSM 路线）")
        ck(after["line"]["stop_ok"] == 1, "② 把 stop_ok 置 1")
        ck(after["stops"] == before["stops"], "② 没顺手重排站序")

        # --- 站序反向 ---
        r = linedesk.reverse_stops(lid)
        st = r["stops"]
        ck([s["name"] for s in st] == ["丁", "丙", "乙", "甲"],
           "反向后站序倒过来：%s" % [s["name"] for s in st])
        ck([s["km"] for s in st] == [0.0, None, 70.0, 100.0],
           "里程 = 原末站 100 − 原里程，新起点 0，缺的仍留空：%s"
           % [s["km"] for s in st])
        ck([(s["lat"], s["lon"]) for s in st]
           == [(30.3, 120.3), (30.2, 120.2), (30.1, 120.1), (30.0, 120.0)],
           "坐标跟着站走，本身没有被改动")
        ck(r["from_sta"] == "丁" and r["to_sta"] == "甲", "起讫站点对调")
        ck(r["total_km"] == 100, "总里程不变")
        ck(r["stop_ok"] == 1, "反向只是换方向，不取消 ② 的确认")
        r2 = linedesk.reverse_stops(lid)
        ck([s["name"] for s in r2["stops"]] == ["甲", "乙", "丙", "丁"]
           and [s["km"] for s in r2["stops"]] == [0.0, 30.0, None, 100.0],
           "再反一次回到原站序与原里程")

        # --- GeoJSON 导入：only='stops' / 'geom' 各只写一边（打桩，不依赖文件格式）---
        import geoimport
        orig_parse, orig_assemble = geoimport.parse, geoimport.assemble
        seq = [{"name": "A", "km": 0, "lat": 30.0, "lon": 120.0},
               {"name": "B", "km": 50, "lat": 30.1, "lon": 120.1},
               {"name": "C", "km": 100, "lat": 30.2, "lon": 120.2}]
        chain = [[120.0, 30.0], [120.1, 30.1], [120.2, 30.2]]
        geoimport.parse = lambda obj, fn: {
            "name": "测试导入走向", "props": {}, "file": fn,
            "parts": [chain], "stations": [{"name": x["name"]} for x in seq]}
        geoimport.assemble = lambda parts, stations, a, b: (
            seq, parts[0], {"totalKm": 100, "geomKm": 100,
                            "kmFrom": "geom", "broken": 0})
        try:
            before = snap(lid)
            linedesk.import_geojson(lid, "{}", "只导站点.json", only="stops")
            after = snap(lid)
            ck(len(after["stops"]) == 3 and after["stops"][0]["name"] == "A",
               "② 导入站点：站序变成文件里的 3 站")
            ck(len(after["geom"]) == len(before["geom"]),
               "② 导入站点：ld_geom 行数没变（走向没被写）")

            before = snap(lid)
            linedesk.import_geojson(lid, "{}", "只导走向.json", only="geom")
            after = snap(lid)
            ck(after["stops"] == before["stops"],
               "① 导入走向：站序一个字节没动")
            ck(len(after["geom"]) == len(before["geom"]) + 1,
               "① 导入走向：ld_geom 多了一份")
            ck(after["line"]["geom_src"] == "geojson"
               and after["line"]["src_line"] == "" and after["line"]["line_ok"] == 1,
               "① 导入走向：记成 geom_src=geojson、清掉来源、算完成 ①")
        finally:
            geoimport.parse, geoimport.assemble = orig_parse, orig_assemble
    finally:
        linedesk.DB_PATH = old_db
        linedesk.clear_cache()
        for suf in ("", "-wal", "-shm"):
            try:
                os.remove(tmp + suf)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# 5：前端 / 路由静态检查
# ---------------------------------------------------------------------------
def test_static() -> None:
    print("[2] ④ 与走向配置已取消、站序提前、反向接口已接")
    js = open(os.path.join(ROOT, "webapp", "static", "linedesk.js"),
              encoding="utf-8").read()
    ck('id="ldPublish"' not in js, "④「定为基本线路」按钮已移除")
    ck('id="ldGeomSrc"' not in js and 'id="ldGeomName"' not in js,
       "④ 的走向下拉 / 走向线名输入已移除")
    ck('id="ldReverse"' in js, "站序区块有「反向站序」按钮")
    ck('id="ldGeoIn"' in js and 'id="ldGeoGeom"' in js,
       "① 导入走向 / ② 导入站点 各有一个入口")
    i2 = js.index("<h4>② 站点来源")
    ist = js.index("<h4>站序", i2)
    i3 = js.index("<h4>③ 合并保存为确认线路", ist)
    ck(i2 < ist < i3, "面板顺序：② → 站序 → ③")
    ck("pickGeojson(d.id, 'stops')" in js
       and "pickGeojson(d.id, 'geom')" in js,
       "② 只导站点 / ① 只导走向 分别传 only")

    sv = open(os.path.join(ROOT, "webapp", "server.py"), encoding="utf-8").read()
    ck('linedesk.publish(lid, "", ""' in sv, "③ HTTP 自动调用 publish（定为基本线路）")
    ck('"/api/ld/stops/reverse"' in sv, "站序反向接口已接")
    ck('payload.get("only")' in sv, "GeoJSON 导入透传 only")
    ck('bool(payload.get("rename", False))' in sv,
       "① 默认不再顺带按资料库改名")

    py = open(os.path.join(ROOT, "webapp", "linedesk.py"), encoding="utf-8").read()
    ck("def reverse_stops(" in py, "后端有 reverse_stops")
    ck('rename: bool = False' in py, "set_line_sources 默认不改名")


# ---------------------------------------------------------------------------
# 6：HTTP 走一遍 ③，验证自动定为基本线路
# ---------------------------------------------------------------------------
BASE = "http://127.0.0.1:8686"


def post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"status": e.code, "error": e.read().decode("utf-8")[:200]}


def get(path: str, **kw) -> dict:
    u = BASE + path + ("?" + urllib.parse.urlencode(kw) if kw else "")
    with urllib.request.urlopen(u, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def test_http() -> None:
    print("[3] ③ 合并保存 = 确认 + 自动定为基本线路（HTTP，临时线，跑完清理）")
    try:
        get("/api/line/confirmed")
    except Exception:                                        # noqa: BLE001
        skip("服务没起，HTTP 部分跳过")
        return
    import linebase
    import railcore
    name = "测试流程自动定稿"

    def base_count() -> int:
        # 直接查库，别用 linebase 的进程内缓存（服务进程写的，本进程指纹可能还没变）
        linebase.invalidate()
        with railcore.db() as c:
            return c.execute("SELECT COUNT(*) FROM line_base").fetchone()[0]

    before = base_count()
    with railcore.db() as c:
        old = c.execute("SELECT id FROM line_base WHERE name=?", (name,)).fetchone()
    if old:
        post("/api/ld/unpublish", {"name": name})
    # 同名草稿残留也清掉
    ex = linedesk.find_line(name)
    if ex:
        linedesk.delete_line(ex["id"])

    geo = {
        "type": "FeatureCollection",
        "properties": {"名称": name, "起点": "甲", "终点": "丙"},
        "features": [
            {"type": "Feature",
             "geometry": {"type": "LineString",
                          "coordinates": [[120.0, 30.0], [120.05, 30.05],
                                          [120.1, 30.1], [120.15, 30.15],
                                          [120.2, 30.2]]},
             "properties": {}},
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [120.0, 30.0]},
             "properties": {"站名": "甲", "类别": "station"}},
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [120.1, 30.1]},
             "properties": {"站名": "乙", "类别": "station"}},
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [120.2, 30.2]},
             "properties": {"站名": "丙", "类别": "station"}},
        ],
    }
    lid = 0
    try:
        r = post("/api/ld/new", {"name": name, "kind": "line"})
        lid = (r.get("line") or {}).get("id") or 0
        ck(lid > 0, "建了临时线")
        if not lid:
            return
        post("/api/ld/stops/save", {"id": lid, "stops": [
            {"name": "甲", "km": 0, "lat": 30.0, "lon": 120.0},
            {"name": "乙", "km": 15, "lat": 30.1, "lon": 120.1},
            {"name": "丙", "km": 30, "lat": 30.2, "lon": 120.2}]})
        s = post("/api/ld/src/stop", {"id": lid, "source": "manual",
                                      "name": "", "fill": False})
        ck((s.get("line") or {}).get("stop_ok") == 1, "② 已确认")
        g = post("/api/ld/import-geojson", {
            "id": lid, "data": json.dumps(geo, ensure_ascii=False),
            "fileName": "测试流程.json", "only": "geom"})
        ck(g.get("ok") is True and g.get("only") == "geom",
           "① 导入走向成功（只写走向）：%s" % (g.get("error") or ""))

        f = post("/api/ld/finalize", {"id": lid})
        nl = f.get("line") or {}
        ck(nl.get("status") == "confirmed", "③ 合并成确认线路")
        ck(nl.get("published") is True,
           "③ 同时定为基本线路（没有 publishError）：%s" % (nl.get("publishError") or ""))
        ck(bool(nl.get("baseInfo")), "③ 返回的详情里带 baseInfo")
        with railcore.db() as c:
            brow = c.execute("SELECT id FROM line_base WHERE name=?", (name,)).fetchone()
            nst = (c.execute("SELECT COUNT(*) FROM line_base_stop WHERE base_id=?",
                             (brow[0],)).fetchone()[0] if brow else 0)
        ck(bool(brow) and nst == 3, "line_base 里真的落了这条线的 3 站（实际 %d）" % nst)
        ck((f.get("line") or {}).get("geom_src") == "geojson",
           "走向沿用 ① 定下的那份，没有被 ③ 重搬")
    finally:
        post("/api/ld/unpublish", {"name": name})
        if lid:
            post("/api/ld/delete", {"id": lid})
        ck(base_count() == before, "清理干净：基本线路数量回到原样")
        ck(linedesk.find_line(name) is None, "清理干净：临时线已删")


def main() -> int:
    test_backend()
    test_static()
    test_http()
    print("\n绿 %d / 红 %d / 跳过 %d" % (OK, FAIL, SKIP))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
