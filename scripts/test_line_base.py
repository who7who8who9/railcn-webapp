# -*- coding: utf-8 -*-
"""test_line_base.py —— 基本线路：整理台定稿 → 全项目接管

跑法：python scripts/test_line_base.py   （要先起 webapp/server.py --port 8686）
收尾：自己撤销基本线路、删掉整理台那条草稿，不留残渣。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8686"
ok = fail = 0


def check(label: str, cond: bool, val=""):
    global ok, fail
    if cond:
        ok += 1
        print("  ok   %s%s" % (label, ("  " + str(val))[:60] if val != "" else ""))
    else:
        fail += 1
        print("  失败 %s%s" % (label, ("  " + str(val))[:60] if val != "" else ""))


def get(path: str) -> dict:
    try:
        with urllib.request.urlopen(BASE + path) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"status": e.code, "error": e.read().decode("utf-8")[:120]}


def post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"status": e.code, "body": e.read().decode("utf-8")}


def main() -> int:
    global ok, fail
    print("== 基本线路（整理台定稿 → 全项目接管）==")

    NAME = "测试基本线路"                 # 用自己的名字，别和真实数据里的线撞名
    ld_id = 0
    # 幂等：上一轮要是崩在半路，可能还留着同名的基本线路 / 整理台草稿
    post("/api/ld/unpublish", {"name": NAME})
    base_before = get("/api/base/lines")["count"]

    # --- 0) 整理台里搭一条草稿（名字故意带「（OSM）」后缀，模拟初稿）---
    r = post("/api/ld/new", {"name": NAME + "（OSM）", "kind": "line",
                             "note": "回归测试用"})
    ld_id = (r.get("line") or {}).get("id")
    check("整理台新建草稿", bool(ld_id), ld_id)
    stops = [{"name": "北京", "km": 0}, {"name": "天津", "km": 120},
             {"name": "济南", "km": 400}, {"name": "南京", "km": 900},
             {"name": "上海", "km": 1300}]
    r = post("/api/ld/stops/save", {"id": ld_id, "stops": stops})
    check("站序写入", r.get("ok") is True, (r.get("line") or {}).get("stops") and len(
        (r.get("line") or {}).get("stops")))
    r = post("/api/ld/save", {"id": ld_id, "name": NAME, "status": "confirmed",
                              "totalKm": 1300, "from": "北京", "to": "上海"})
    check("改规范名 + 确认", r.get("ok") is True)

    # --- 1) 定为基本线路 ---
    r = post("/api/ld/publish", {"id": ld_id, "geomSrc": "osm", "geomName": "京沪线",
                                 "name": NAME})
    check("publish 成功", r.get("ok") is True, (r.get("line") or {}).get("name"))
    check("基本线路 +1", get("/api/base/lines")["count"] == base_before + 1)
    _pl = r.get("line") or {}
    check("linebase.get 命中", len(_pl.get("stops") or []) == 5,
          len(_pl.get("stops") or []))

    # --- 2) 详情：全面接管 ---
    d = get("/api/line?q=%s" % urllib.request.quote(NAME))
    check("详情 base=true", d.get("base") is True)
    check("详情站序是定稿那份", [s["name"] for s in d.get("stations", [])] ==
          ["北京", "天津", "济南", "南京", "上海"],
          [s["name"] for s in d.get("stations", [])][:3])
    check("详情 n=5", d.get("n") == 5, d.get("n"))
    check("group.why=linebase", (d.get("group") or {}).get("effectiveWhy") == "linebase",
          (d.get("group") or {}).get("effectiveWhy"))
    check("group.hasBase", (d.get("group") or {}).get("hasBase") is True)

    # --- 3) 点名看某个来源版本：不接管 ---
    d2 = get("/api/line?q=%s" % urllib.request.quote(NAME + "（底表）"))
    check("点名（底表）不接管", d2.get("base") is not True)
    check("（底表）不是定稿那份", d2.get("n") != 5, d2.get("n"))

    # --- 4) 选经由 ---
    s = get("/api/line/stops?q=%s" % urllib.request.quote(NAME))
    check("选经由 from=base", s.get("from") == "base", s.get("from"))
    check("选经由站序", s.get("items") == ["北京", "天津", "济南", "南京", "上海"],
          s.get("count"))

    # --- 5) 校对下拉：2026-11-11 起只列整理台「全部确认」的线路 ---
    # 基本线路（整理台定稿）不等于确认线路：它没走 ③ 合并确认那一套的话，
    # 数据校对里就不出现（见 confvia.py）。基本线路本身仍全面接管 /api/line、
    # /api/line/stops（上面 1~4 已经验过），这里只守「校对下拉不列没确认的线」。
    ln = get("/api/curate/lineNames")
    check("校对下拉标 scope=confirmed", ln.get("scope") == "confirmed", ln.get("scope"))
    hit = [x for x in ln["items"] if x["name"] == NAME]
    check("校对下拉不列没确认的基本线路", not hit, [x["name"] for x in hit])
    check("校对下拉里的线名都不带（来源）后缀",
          all("（" not in x["name"] for x in ln["items"]),
          [x["name"] for x in ln["items"] if "（" in x["name"]][:3])

    # --- 6) effective 优先级 ---
    sys.path.insert(0, "webapp")
    import linegroup
    eff = linegroup.effective(NAME)
    check("effective.why=linebase", eff["why"] == "linebase", eff["why"])
    check("effective.name=规范名", eff["name"] == NAME, eff["name"])
    eff2 = linegroup.effective(NAME + "（OSM）")
    check("带后缀不接管", eff2["why"] != "linebase", eff2["why"])

    # --- 7) 同名冲突：再 publish 一条同名要报错，replaceId 才行 ---
    r = post("/api/ld/new", {"name": "测试基本线路·重复", "kind": "line"})
    ld2 = (r.get("line") or {}).get("id")
    post("/api/ld/stops/save", {"id": ld2,
                                "stops": [{"name": "A", "km": 0}, {"name": "B", "km": 10}]})
    # 新线默认是「待整理」（来源要人工逐个确认），这里只把它确认掉；
    # 名字不改成 NAME —— NAME 已经被上面那条线占着，publish 用传参的 name 就行
    post("/api/ld/save", {"id": ld2, "status": "confirmed"})
    r = post("/api/ld/publish", {"id": ld2, "name": NAME})
    check("同名 publish 报错", r.get("status") == 400 or r.get("ok") is not True,
          r.get("status") or r.get("body") or "ok:false")
    r = post("/api/ld/publish", {"id": ld2, "name": NAME,
                                 "replaceId": (d.get("baseId") or 0)})
    check("replaceId 可覆盖", r.get("ok") is True,
          (r.get("line") or {}).get("n"))
    d3 = get("/api/line?q=%s" % urllib.request.quote(NAME))
    check("覆盖后站序变了", d3.get("n") == 2, d3.get("n"))

    # --- 8) 撤销：退回原样 ---
    r = post("/api/ld/unpublish", {"name": NAME})
    check("unpublish 成功", r.get("ok") is True)
    check("基本线路归零", get("/api/base/lines")["count"] == base_before)
    d4 = get("/api/line?q=%s" % urllib.request.quote(NAME))
    check("撤销后不再是 base", d4.get("base") is not True)

    # --- 9) 清理整理台草稿 ---
    for _id in (ld_id, ld2):
        if _id:
            post("/api/ld/delete", {"id": _id})
    check("整理台草稿已删", get("/api/base/lines")["count"] == base_before)

    print("\n通过 %d / 失败 %d" % (ok, fail))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
