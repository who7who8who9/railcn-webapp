# -*- coding: utf-8 -*-
"""回归：手工导入 GeoJSON 线路

口径（这几条是这个功能存在的全部理由）：

  1. **排序看走向，不看文件**：文件里车站点的先后可能是乱的（京沪线的导出把
     白家店排在南驿前面、天津枢纽几个站前后颠倒），导入要按「在走向上的位置」排。
  2. **里程是沿线距离**：首站 0，单调递增，末站就是这条线的里程；两站之间算
     轨道上的距离，不是直线距离。
  3. 落成**自建线路**（line_custom）：改站序、倒排、改里程、删除全都照旧能用。
  4. 走向另存一份（line_geom），`/api/line/osm` 优先给这一份；删线时一起删掉。
  5. 同名线路已存在 → 先拒绝，并明确说「可以覆盖」；带 replace 才覆盖，
     档案线路（底表 / 资料库带进来的）永远不覆盖。
  6. 坏文件给一句人话（不是 JSON / 没有走向 / 站不到 2 个），不要 500。

用法：python scripts/test_geo_import.py        （需要服务在 8686 跑着）
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8686"
LINE = "测试导入线（回归用）"
OK = FAIL = 0


def ck(name, cond, msg=""):
    global OK, FAIL
    if cond:
        OK += 1
        print("  ok   %s" % name)
    else:
        FAIL += 1
        print("  FAIL %s%s" % (name, ("  → " + msg) if msg else ""))


def get(path, **qs):
    url = BASE + path + (("?" + urllib.parse.urlencode(qs)) if qs else "")
    try:
        return json.load(urllib.request.urlopen(url, timeout=120))
    except urllib.error.HTTPError as e:
        return {"error": json.loads(e.read() or b"{}").get("error") or str(e), "_code": e.code}


def post(path, body, expect_error=False):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        return json.load(urllib.request.urlopen(req, timeout=300))
    except urllib.error.HTTPError as e:
        if not expect_error:
            raise
        raw = e.read()
        try:
            return {"error": json.loads(raw).get("error") or str(e), "_code": e.code}
        except ValueError:
            return {"error": str(e), "_code": e.code}


def sample():
    """一条南北向的假线：3 段走向（中间留 1.6 km 断口）、3 个站，站点故意乱序给

    甲站在最南、丙站在最北，文件里按「乙、甲、丙」的顺序写点 —— 导入要能自己
    排回 甲 → 乙 → 丙。
    """
    return {
        "type": "FeatureCollection",
        "properties": {"线路名称": LINE, "编号": "TEST-1", "起点": "甲站", "终点": "丙站",
                       "way段数": 3, "车站数": 3},
        "features": [
            {"type": "Feature",
             "geometry": {"type": "MultiLineString", "coordinates": [
                 [[120.0, 30.0], [120.0, 30.03]],
                 [[120.0, 30.045], [120.0, 30.09]],
                 [[120.0, 30.10], [120.0, 30.1264]],
             ]}},
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [120.0, 30.0632]},
             "properties": {"kind": "station", "站名": "乙站", "类型": "站"}},
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [120.0, 30.0]},
             "properties": {"kind": "station", "站名": "甲站", "类型": "站"}},
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [120.0, 30.1264]},
             "properties": {"kind": "station", "站名": "丙站", "类型": "站"}},
        ],
    }


def drop():
    try:
        post("/api/line/delete", {"name": LINE})
    except urllib.error.HTTPError:
        pass


def test_import():
    print("\n== 导入：按走向排序、沿线算里程")
    drop()
    fn = "测试导入线.geojson"
    r = post("/api/line/import-geojson", {"fileName": fn, "data": sample()})
    ck(r.get("ok") is True, "导入成功", r.get("error"))
    ck(r.get("n") == 3, "3 个站都进来了", str(r.get("n")))
    ck(r.get("first") == "甲站" and r.get("last") == "丙站",
       "站序按走向排（文件里乙在最前）", "%s → %s" % (r.get("first"), r.get("last")))
    ck(r.get("broken") == 0, "断口补边接上了，没有按直线跳", str(r.get("broken")))

    d = get("/api/line", q=LINE)
    kms = [s["km"] for s in d["stations"]]
    ck(d.get("custom") is True, "落成自建线路", str(d.get("custom")))
    ck(kms[0] == 0.0, "首站里程 0", str(kms[0]))
    ck(all(kms[i] < kms[i + 1] for i in range(len(kms) - 1)), "里程单调递增", str(kms))
    # 沿走向：甲→乙 7.0 km，甲乙之间还有 1.6 km 断口要算进去，所以 7 站间… 这里三站
    ck(6.0 < kms[1] < 8.5, "乙站里程 ≈7 km（沿线，不是直线）", str(kms[1]))
    ck(13.0 < kms[2] < 15.5, "末站里程 ≈14 km", str(kms[2]))
    ck(abs((d.get("totalKm") or 0) - kms[2]) < 0.2, "总里程 = 末站里程", str(d.get("totalKm")))
    ck((d.get("info") or {}).get("title") is None, "这条假线不挂资料卡", str(d.get("info")))

    print("\n== 走向：存进 line_geom，接口优先给这一份")
    g = get("/api/line/osm", q=LINE)
    ck(g.get("source") == "geojson", "走向来自 GeoJSON", str(g.get("source")))
    ck(g.get("file") == fn, "记着文件名", str(g.get("file")))
    ck(bool(g.get("parts")), "有折线可画", str(len(g.get("parts") or [])))
    pts = sum(len(p) for p in (g.get("parts") or []))
    ck(pts >= 3, "折线点数够", str(pts))
    ck(abs((g.get("km") or 0) - kms[2]) < 0.2, "几何里程 = 站序里程", str(g.get("km")))

    print("\n== 站坐标：文件里带的坐标补进车站库")
    st = get("/api/station", q="甲站") or {}
    ck(st.get("lat") is not None or st.get("error") is not None, "车站接口没炸")

    print("\n== 同名再导：先拒绝，说清能不能覆盖")
    r2 = post("/api/line/import-geojson", {"fileName": fn, "data": sample()},
              expect_error=True)
    ck(r2.get("_code") == 400, "重复导入被拒（400）", str(r2.get("_code")))
    ck("可以覆盖" in (r2.get("error") or ""), "提示里说清可以覆盖", r2.get("error"))
    r3 = post("/api/line/import-geojson",
              {"fileName": fn, "data": sample(), "replace": True})
    ck(r3.get("ok") is True, "带 replace 能覆盖", r3.get("error"))
    ck(r3.get("n") == 3, "覆盖后还是 3 站", str(r3.get("n")))

    print("\n== 坏文件：给人话，不要 500")
    e1 = post("/api/line/import-geojson", {"fileName": "x.json", "data": "{不是 JSON"},
              expect_error=True)
    ck(e1.get("_code") == 400 and "JSON" in (e1.get("error") or ""),
       "不是 JSON", e1.get("error"))
    no_geom = {"type": "FeatureCollection", "properties": {"线路名称": LINE},
               "features": [{"type": "Feature", "geometry": {"type": "Point",
                                                             "coordinates": [120, 30]},
                             "properties": {"站名": "孤站"}}]}
    e2 = post("/api/line/import-geojson", {"fileName": "y.json", "data": no_geom},
              expect_error=True)
    ck(e2.get("_code") == 400 and "线状几何" in (e2.get("error") or ""),
       "没有走向", e2.get("error"))
    one_st = {"type": "FeatureCollection", "properties": {"线路名称": LINE},
              "features": [
                  {"type": "Feature",
                   "geometry": {"type": "LineString",
                                "coordinates": [[120, 30], [120, 30.05]]}},
                  {"type": "Feature", "geometry": {"type": "Point",
                                                   "coordinates": [120, 30]},
                   "properties": {"站名": "孤站"}}]}
    e3 = post("/api/line/import-geojson", {"fileName": "z.json", "data": one_st},
              expect_error=True)
    ck(e3.get("_code") == 400 and "2 个" in (e3.get("error") or ""),
       "站不够 2 个", e3.get("error"))


def test_cleanup():
    print("\n== 删线：走向跟着删，接口回到 404")
    post("/api/line/delete", {"name": LINE})
    d = get("/api/line", q=LINE)
    ck(d.get("error") is not None or d.get("name") != LINE, "线路库没有了", str(d.get("name")))
    g = get("/api/line/osm", q=LINE)
    ck(g.get("_code") == 404 or g.get("source") != "geojson", "走向没了",
       str(g.get("source")))


def main():
    try:
        test_import()
        test_cleanup()
    finally:
        drop()
    print()
    if FAIL:
        print("失败 %d 项，通过 %d 项" % (FAIL, OK))
        return 1
    print("全部通过（%d 项）" % OK)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
