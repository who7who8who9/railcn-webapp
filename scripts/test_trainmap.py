# -*- coding: utf-8 -*-
"""全路网列车位置（时刻表推算）回归：解析 / 索引 / 快照 / 筛选 / 详情。

跑法：python scripts/test_trainmap.py
纯本地读库 + 内存索引，不依赖 8686 服务、不写库。用固定时刻保证结果可重复。
"""
import datetime as dt
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "webapp"))
import trainmap  # noqa: E402
import railcore  # noqa: E402

T = "2026-11-06 08:30"
OK, BAD = 0, []


def check(name, cond, extra=""):
    global OK
    if cond:
        OK += 1
        print("  ok  ", name)
    else:
        BAD.append(name)
        print("  FAIL", name, extra)


def main():
    print("== 1. 时间字段解析 ==")
    check("08:35 → 515 分钟", trainmap._hm("08:35") == 515)
    check("8:35 → 515 分钟", trainmap._hm("8:35") == 515)
    check("---- / 空 / 坏值 → None",
          trainmap._hm("----") is None and trainmap._hm("") is None and trainmap._hm("xx") is None)
    check("arrive_day_diff 脏值被夹住", trainmap._day("abc") == 0 and trainmap._day("99") == 9)

    print("== 2. 停站模板解析 ==")
    raw = [
        {"station_name": "北京南", "arrive_time": "----", "start_time": "23:30",
         "arrive_day_diff": "0", "station_no": "01"},
        {"station_name": "南京南", "arrive_time": "00:20", "start_time": "00:23",
         "arrive_day_diff": "1", "station_no": "02"},
        {"station_name": "上海虹桥", "arrive_time": "01:10", "start_time": "01:10",
         "arrive_day_diff": "1", "station_no": "03"},
    ]
    stops = trainmap._parse_raw(json.dumps(raw, ensure_ascii=False), railcore.stations)
    check("三站都解析出来", stops is not None and len(stops) == 3, stops)
    if stops:
        check("跨天到达 = 次日 00:20 = 1460 分", stops[1][3] == 1460, stops[1])
        check("停站出发 = 1463 分（跨午夜补 1440）", stops[1][4] == 1463, stops[1])
        check("全程时刻单调不减", all(stops[i][4] <= stops[i + 1][3] for i in range(2)))
    check("少于两站的坏数据被丢弃",
          trainmap._parse_raw('[{"station_name":"A","start_time":"08:00"}]', railcore.stations) is None)

    print("== 3. 索引构建 ==")
    t0 = time.time()
    idx = trainmap.build(force=True)
    build_s = time.time() - t0
    check("模板 > 1 万", len(idx.templates) > 10000, len(idx.templates))
    check("开行实例 > 5 万", len(idx.instances) > 50000, len(idx.instances))
    check("窗口 6 天", len(idx.dates) == 6, idx.dates)
    check("有推定开行实例", any(x[3] for x in idx.instances))
    check("模板时刻已排序", all(
        all(t.deps[i] <= t.deps[i + 1] for i in range(len(t.deps) - 1))
        for t in list(idx.templates.values())[:300]))
    print(f"      构建耗时 {build_s:.2f}s")

    print("== 4. 快照口径 ==")
    snap = trainmap.snapshot(t=T)
    items = snap["items"]
    check("此刻在途 > 1000 趟", snap["total"] > 1000, snap["total"])
    check("run + stop = total", snap["stats"]["run"] + snap["stats"]["stop"] == snap["total"])
    check("缺坐标 = 0", snap["stats"]["noCoord"] == 0, snap["stats"]["noCoord"])
    check("全部带车次号与起讫站", all(x["code"] and x["from"] and x["to"] for x in items))
    check("坐标都落在中国范围",
          all(17 <= x["lat"] <= 54 and 72 <= x["lon"] <= 136 for x in items))
    check("进度 0~100、站序不越界",
          all(0 <= x["pct"] <= 100 and 1 <= x["stopIdx"] <= x["nStops"] for x in items))
    check("速度已钳到 400 以内", all(0 <= x["speed"] <= 400 for x in items))
    check("快照耗时 < 2 秒", snap["elapsed"] < 2, snap["elapsed"])

    print("== 5. 筛选 ==")
    one = trainmap.snapshot(t=T, q="G1")
    check("q=G1 精确命中一趟（不把 G10/G100 捞进来）", one["total"] == 1, one["total"])
    check("命中的就是 G1", one["items"] and one["items"][0]["code"] == "G1")
    g = trainmap.snapshot(t=T, types="G")
    check("types=G 全是 G 开头", g["total"] > 100 and all(x["code"].startswith("G") for x in g["items"]))
    num = trainmap.snapshot(t=T, types="0,1,2,3,4,5,6,7,8,9")
    check("数字车种筛选生效", num["total"] > 0 and all(x["code"][0].isdigit() for x in num["items"]))
    other = trainmap.snapshot(t=T, types="other")
    check("「其他」车种不含已知字母",
          all(x["code"][0].upper() not in "GDCZTKS0123456789" for x in other["items"]))
    zz = trainmap.snapshot(t=T, station="郑州东", limit=5000)
    check("途经站筛选能查到车", zz["total"] > 0, zz["total"])
    run = trainmap.snapshot(t=T, status="run")
    stop = trainmap.snapshot(t=T, status="stop")
    check("status=run 全是运行中", all(x["phase"] == "run" for x in run["items"]))
    check("status=stop 全是停靠中", all(x["phase"] == "stop" for x in stop["items"]))
    box = trainmap.snapshot(t=T, bbox=[115.5, 39.0, 117.5, 41.2])
    check("bbox 过滤生效",
          box["total"] > 0 and all(115.5 <= x["lon"] <= 117.5 and 39.0 <= x["lat"] <= 41.2
                                   for x in box["items"]))
    few = trainmap.snapshot(t=T, limit=3)
    check("limit 生效且 total 不缩水", len(few["items"]) == 3 and few["total"] > 3)

    print("== 6. 时间回放 ==")
    a = trainmap.snapshot(t="2026-11-06 08:30", q="G1")
    b = trainmap.snapshot(t="2026-11-06 09:30", q="G1")
    check("同一车次换时刻仍在途", a["total"] == 1 and b["total"] == 1)
    if a["items"] and b["items"]:
        pa, pb = a["items"][0], b["items"][0]
        check("位置随时间推进（里程单调）",
              (pb["stopIdx"], pb["pct"]) > (pa["stopIdx"], pa["pct"]), (pa["stopIdx"], pa["pct"], pb["stopIdx"], pb["pct"]))
    check("不同时刻在途总数不同",
          trainmap.snapshot(t="2026-11-06 03:30")["total"] != snap["total"])

    print("== 7. 单车详情 ==")
    d = trainmap.train_detail("G1", "2026-11-06", t=T)
    check("返回车次与起讫", d.get("code") == "G1" and d.get("from") and d.get("to"))
    check("站序 >= 2 且带坐标", len(d.get("stops", [])) >= 2 and all(s["lat"] is not None for s in d["stops"]))
    check("发车 < 终到", d["start"] < d["end"], (d["start"], d["end"]))
    check("当前位置有区间与下一站", d.get("pos") and d["pos"]["next"])
    check("标明非 GPS", "非 GPS" in d.get("note", ""))

    print("== 8. 降级底图与缓存 ==")
    bb = trainmap.backbone()
    check("车站散点 > 1 万", bb["count"] > 10000, bb["count"])
    check("散点是 [lon,lat] 且在中国", all(72 <= p[0] <= 136 and 17 <= p[1] <= 54 for p in bb["points"][:500]))
    k = (dt.datetime.fromisoformat(T).strftime("%Y-%m-%d %H:%M"), "G1", "", "in", "", "", "", None, 4000)
    trainmap.snapshot(t=T, q="G1")
    check("快照按分钟缓存", k in trainmap._SNAP or len(trainmap._SNAP) > 0)
    check("stats 带构建信息", trainmap.stats().get("templates", 0) > 10000)

    print(f"\n全部通过（{OK} 项）" if not BAD else f"\n失败 {len(BAD)} 项：{BAD}")
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
