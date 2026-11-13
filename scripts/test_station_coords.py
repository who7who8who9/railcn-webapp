# -*- coding: utf-8 -*-
"""车站「多个坐标」候选库回归（2026-11-09）。

跑法：python scripts/test_station_coords.py
不依赖运行中的服务；测试里改的 coord_override 一律用临时站名并清干净。

覆盖：坐标系换算 / 自动校准 / 候选表重建幂等 / 聚类与当前标记 /
      别名解析 / 卡片字段 / 确认来源写入与撤销 / OSM 独有卡片。
"""
from __future__ import annotations

import os
import sqlite3
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "webapp"))
sys.path.insert(0, BASE)

import coordcrs                                  # noqa: E402
import railcore                                  # noqa: E402
import stationcoords as sc                       # noqa: E402

DB = os.path.join(BASE, "webapp", "data", "rail.db")
TEST = "测试_坐标候选_临时"
passed = failed = 0


def ok(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✓ {name}")
    else:
        failed += 1
        print(f"  ✗ {name}  {extra}")


def cleanup():
    try:
        railcore.clear_coord(TEST)
    except Exception:                            # noqa: BLE001
        pass
    c = sqlite3.connect(DB)
    c.execute("DELETE FROM coord_override WHERE name=?", (TEST,))
    c.commit()
    c.close()


def main():
    print("== 1. 坐标系换算 / 自动校准 ==")
    wgs = (116.3724, 39.86348)                  # 北京南 WGS84
    gcj = coordcrs.wgs2gcj(*wgs)
    back = coordcrs.gcj2wgs(*gcj)
    ok("WGS84→GCJ-02 有偏移（>100 m）",
       abs(gcj[0] - wgs[0]) > 0.001 or abs(gcj[1] - wgs[1]) > 0.001)
    ok("GCJ-02→WGS84 往返误差 < 1e-6°",
       abs(back[0] - wgs[0]) < 1e-6 and abs(back[1] - wgs[1]) < 1e-6,
       str(back))
    bd = (116.379064, 39.864462)
    ok("BD-09→WGS84 落在中国境内", 73 < coordcrs.bd092wgs(*bd)[0] < 136)
    # 三种变换里挑离基准最近的：GCJ-02 点会被换算，已经是 WGS84 的点不动
    lon, lat, crs, conv = coordcrs.calibrate(gcj[0], gcj[1], "GCJ-02(高德)", [wgs])
    ok("GCJ-02 候选被校准成 WGS84", conv and abs(lon - wgs[0]) < 1e-6,
       f"{crs} {lon},{lat}")
    lon2, lat2, crs2, conv2 = coordcrs.calibrate(wgs[0], wgs[1], "GCJ-02(高德)", [wgs])
    ok("已是 WGS84 的点不重复换算", not conv2 and abs(lon2 - wgs[0]) < 1e-9)
    far = (110.0, 30.0)
    _, _, crs3, conv3 = coordcrs.calibrate(far[0], far[1], "BD-09百度(见repo爬虫)", [wgs])
    ok("离所有基准 >2km 的点原样保留（同名不同站）", not conv3 and crs3 == "BD-09")
    _, _, _, conv4 = coordcrs.calibrate(gcj[0], gcj[1], "GCJ-02(高德)", None)
    ok("没有基准点时不猜坐标系", not conv4)

    print("== 2. 候选表 ==")
    st = sc.build()                              # 已存在则不重建
    c = sqlite3.connect(DB)
    n = c.execute("SELECT COUNT(*) FROM station_coord_cand").fetchone()[0]
    nsrc = c.execute("SELECT COUNT(DISTINCT src) FROM station_coord_cand").fetchone()[0]
    nkey = c.execute("SELECT COUNT(DISTINCT key) FROM station_coord_cand").fetchone()[0]
    c.close()
    ok("候选表有数据（>10 万行）", n > 100000, str(n))
    ok("来源 >= 12 套", nsrc >= 12, str(nsrc))
    ok("覆盖站名 > 1 万", nkey > 10000, str(nkey))
    ok("重复 build 不重建（幂等）", st.get("rebuilt") is False, str(st)[:120])

    print("== 3. 候选查询 / 聚类 ==")
    d = sc.candidates("北京南", cur_lat=39.863334, cur_lon=116.372052)
    ok("北京南有多处坐标", d["n"] >= 2, str(d["n"]))
    ok("每处都有来源", all(x["sources"] for x in d["items"]))
    ok("有且仅有 1 处标为当前", sum(1 for x in d["items"] if x["isCurrent"]) == 1)
    ok("按距当前由近及远排序",
       all((d["items"][i]["distM"] or 0) <= (d["items"][i + 1]["distM"] or 0)
           for i in range(len(d["items"]) - 1)))
    ok("来源里带坐标系标注", any(s["crs"] for x in d["items"] for s in x["sources"]))
    d2 = sc.candidates("北京南站", cur_lat=39.863334, cur_lon=116.372052)
    ok("带「站」后缀的别名也能查到", d2["n"] == d["n"], f"{d2['n']} vs {d['n']}")
    d3 = sc.candidates("东莞西", cur_lat=23.011359, cur_lon=113.622424)
    ok("东莞西保留了 12 km 外的同名异地点", d3["maxDistM"] > 10000, str(d3["maxDistM"]))
    ok("TrainVis 的 BD-09 点已换算并并入总表那一处",
       any(any("→WGS84" in s["crs"] for s in x["sources"])
           for x in d3["items"]))
    ok("查不到的站返回空候选", sc.candidates("不存在的车站名_xyz")["n"] == 0)

    print("== 4. 车站卡片字段 ==")
    det = railcore.station_detail("北京南")
    cc = det.get("coordCandidates") or {}
    ok("station_detail 带 coordCandidates", cc.get("n", 0) >= 2)
    ok("station_detail 带 coordConfirmSrc 键", "coordConfirmSrc" in det)
    ok("候选里有当前生效标记", any(x["isCurrent"] for x in cc.get("items", [])))
    osm = railcore.osm_station_card("晓明")
    ok("OSM 独有卡片也带候选", bool(osm and (osm.get("coordCandidates") or {}).get("n", 0) >= 1))
    ok("OSM 独有卡片带 coordSource", bool(osm and osm.get("coordSource")))

    print("== 5. 确认来源写入 / 撤销 ==")
    cleanup()
    # 基线当场取：用户随时会在车站卡片里「用这个」确认新坐标（2026-11-09 北园），
    # 写死「22 条」会隔天就误报，还会诱使人去删真实的人工坐标。
    c = sqlite3.connect(DB)
    base_n = c.execute("SELECT COUNT(*) FROM coord_override").fetchone()[0]
    c.close()
    r = railcore.set_coord(TEST, 39.123456, 116.654321, src="车站档案(WGS84)")
    ok("确认后 coord_info 返回 confirmSrc", r.get("confirmSrc") == "车站档案(WGS84)", str(r))
    c = sqlite3.connect(DB)
    row = c.execute("SELECT lat, lon, src, picked_at FROM coord_override WHERE name=?",
                    (TEST,)).fetchone()
    c.close()
    ok("coord_override 里落了来源与时间", bool(row and row[2] and row[3]), str(row))
    info = railcore.stations.coord_info(TEST)
    ok("重载坐标缓存后仍能读到来源", info.get("confirmSrc") == "车站档案(WGS84)")
    r2 = railcore.clear_coord(TEST)
    ok("撤销后回到无人工坐标", r2.get("source") == "none" and not r2.get("confirmSrc"))
    cleanup()
    c = sqlite3.connect(DB)
    left = c.execute("SELECT COUNT(*) FROM coord_override WHERE name=?", (TEST,)).fetchone()[0]
    c.close()
    ok("测试站无残留", left == 0)
    c = sqlite3.connect(DB)
    after_n = c.execute("SELECT COUNT(*) FROM coord_override").fetchone()[0]
    c.close()
    ok("真实站的已有人工坐标数量未变（%d）" % base_n, after_n == base_n,
       f"{base_n} -> {after_n}")

    print(f"\n通过 {passed} / 失败 {failed}")
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
