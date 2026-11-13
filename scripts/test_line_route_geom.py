# -*- coding: utf-8 -*-
"""test_line_route_geom.py —— 基本线路「走向 = 某个来源包自己的线」

跑法：python scripts/test_line_route_geom.py   （要先起 webapp/server.py --port 8686）
验的是 2026-11-05 新加的那条路：整理台定基本线路时 geomSrc 传 `route:hdx`
（rail-globe / OSM / cnrail 三套走向数据之一），定稿后
`/api/line` 和 `/api/line/osm` 都要用这个来源包自己的几何，而不是 line_geom 里的合并走向。

收尾：撤销基本线路、删掉整理台草稿，不留残渣。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8686"
ok = fail = 0


def check(label: str, cond: bool, val=""):
    global ok, fail
    if cond:
        ok += 1
        print("  ok   %s%s" % (label, ("  " + str(val))[:70] if val != "" else ""))
    else:
        fail += 1
        print("  失败 %s%s" % (label, ("  " + str(val))[:70] if val != "" else ""))


def get(path: str) -> dict:
    try:
        with urllib.request.urlopen(BASE + path) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"status": e.code, "error": e.read().decode("utf-8")[:150]}


def post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"status": e.code, "body": e.read().decode("utf-8")[:150]}


def main() -> int:
    global ok, fail
    print("== 基本线路走向 = 某个来源包自己那条线 ==")

    NAME = "测试走向来源"
    SRC = "hdx"                       # OSM 走向
    RNAME = "京沪线"                   # line_route 里的线名
    post("/api/ld/unpublish", {"name": NAME})
    base_before = get("/api/base/lines")["count"]

    # --- 0) 整理台搭草稿 ---
    r = post("/api/ld/new", {"name": NAME + "（OSM）", "note": "走向来源回归"})
    ld_id = (r.get("line") or {}).get("id")
    check("新建草稿", bool(ld_id), ld_id)
    post("/api/ld/stops/save", {"id": ld_id, "stops": [
        {"name": "北京", "km": 0}, {"name": "天津", "km": 120},
        {"name": "济南", "km": 400}, {"name": "南京", "km": 900},
        {"name": "上海", "km": 1300}]})
    r = post("/api/ld/save", {"id": ld_id, "name": NAME, "status": "confirmed",
                              "totalKm": 1300, "from": "北京", "to": "上海"})
    check("改规范名 + 确认", r.get("ok") is True)

    # --- 1) 定稿时把走向指到 hdx 这个来源包 ---
    r = post("/api/ld/publish", {"id": ld_id, "geomSrc": "route:" + SRC,
                                 "geomName": RNAME, "name": NAME})
    check("publish 成功", r.get("ok") is True, (r.get("line") or {}).get("name"))
    check("基本线路 +1", get("/api/base/lines")["count"] == base_before + 1)

    # --- 2) 详情：osm 要走 route 分支，里程是来源包自己的 ---
    d = get("/api/line?q=" + urllib.parse.quote(NAME))
    o = d.get("osm") or {}
    check("详情带 base", d.get("base") is True)
    check("osm.source = route", o.get("source") == "route", o.get("source"))
    check("routeSrc = hdx", o.get("routeSrc") == SRC, o.get("routeSrc"))
    check("路标写「OSM 走向」", o.get("routeLabel") == "OSM 走向", o.get("routeLabel"))
    check("几何里程 = 来源包里程", abs((o.get("km") or 0) - 1527.9) < 0.3, o.get("km"))
    check("段数 = 来源包段数", int(o.get("nPart") or 0) > 3000, o.get("nPart"))

    # --- 3) 真正画图那一步：/api/line/osm 必须给这个来源包的 parts ---
    g = get("/api/line/osm?q=" + urllib.parse.quote(NAME))
    parts = g.get("parts") or []
    npt = sum(len(p) for p in parts)
    check("走向来自来源包", g.get("source") == "route", g.get("source"))
    check("段数 3214", len(parts) == 3214, len(parts))
    check("点数 20436", npt == 20436, npt)
    check("不是 line_geom 的合并走向（合并 4224 段）", len(parts) != 4224, len(parts))
    if parts:
        p0 = parts[0][0]
        check("坐标是 WGS-84 京沪走廊",
              115.5 < p0[0] < 122.5 and 30.5 < p0[1] < 40.5, p0)

    # --- 4) 整理台自己的地图也认这条走向 ---
    lg = get("/api/ld/geom?id=%d" % ld_id)
    gg = lg.get("geom") or {}
    check("整理台走向也来自来源包", gg.get("source") == "route", gg.get("source"))

    # --- 5) 换回默认（不指定来源包）就不该再走 route ---
    #     人工定稿过的基本线路要显式带 replaceId 才覆盖（和 publish 的口径一致）
    r = post("/api/ld/publish", {"id": ld_id, "geomSrc": "osm", "geomName": "",
                                 "name": NAME, "replaceId": d.get("baseId") or 0})
    check("改回 osm 成功（replaceId 覆盖）", r.get("ok") is True,
          r.get("status") or r.get("body"))
    d2 = get("/api/line?q=" + urllib.parse.quote(NAME))
    check("不再是 route", (d2.get("osm") or {}).get("source") != "route",
          (d2.get("osm") or {}).get("source"))

    # --- 6) 收尾 ---
    post("/api/ld/unpublish", {"name": NAME})
    if ld_id:
        post("/api/ld/delete", {"id": ld_id})
    check("基本线路归零", get("/api/base/lines")["count"] == base_before)
    d3 = get("/api/line?q=" + urllib.parse.quote(NAME))
    check("撤销后不再是 base", d3.get("base") is not True)

    print("\n通过 %d / 失败 %d" % (ok, fail))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
