# -*- coding: utf-8 -*-
"""行车路线（已整理线路贴真实走向，其余停站间直线）回归。

跑法：python scripts/test_traintrack.py
纯本地读库 + 内存索引，不依赖 8686 服务、不写库。
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "webapp"))
import traintrack as T  # noqa: E402

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
    print("== 1. 真实走向索引 ==")
    t0 = time.time()
    lines = T.lines()
    check("索引有线路", len(lines) > 1000, f"实际 {len(lines)}")
    check("索引构建不超过 20 秒", time.time() - t0 < 20, f"{time.time() - t0:.1f}s")
    check("折线都有 >=8 个点", all(len(x.pts) >= 8 for x in lines))
    check("累计里程单调不减", all(all(b >= a for a, b in zip(x.cum, x.cum[1:]))
                                for x in lines))
    check("坐标都在合理范围", all(17 <= p[0] <= 55 and 72 <= p[1] <= 137
                                for x in lines for p in x.pts))
    stops_idx = T.line_stops()
    check("站序索引可用", len(stops_idx) > 1000, f"实际 {len(stops_idx)}")

    print("== 2. 典型车次：真实走向 + 直线混合 ==")
    for code in ("G1", "D2290", "T110"):
        r = T.track(code)
        if not r.get("ok"):
            check(f"{code} 能算出路线", False, r.get("error", ""))
            continue
        segs = r["pieces"]
        check(f"{code} 有分段", len(segs) == r["totalSegs"],
              f"{len(segs)} vs {r['totalSegs']}")
        check(f"{code} 用上了真实走向", r["realSegs"] > 0, str(r["lines"]))
        check(f"{code} 分段计数自洽", r["realSegs"] + r["straightSegs"] == r["totalSegs"])
        check(f"{code} 覆盖率在 0~1", 0 <= r["coverage"] <= 1)
        check(f"{code} path 点数与分段吻合",
              r["nPts"] == sum(len(p["pts"]) for p in segs) - max(0, len(segs) - 1))
        check(f"{code} 直线段只有两个端点",
              all(len(p["pts"]) == 2 for p in segs if p["mode"] == "straight"))
        check(f"{code} 真实走向段点数受控",
              all(2 <= len(p["pts"]) <= T.MAX_PIECE_PTS + 2
                  for p in segs if p["mode"] == "real"))
        check(f"{code} 真实走向段有线路名",
              all(p["line"] for p in segs if p["mode"] == "real"))
        # 大跳变 = 合并走向把不相连的碎段硬连成一根长直线，必须被挡住
        worst = 0.0
        for p in segs:
            if p["mode"] != "real":
                continue
            q = p["pts"]
            worst = max(worst, max((T._hav(q[i - 1], q[i]) for i in range(1, len(q))),
                                  default=0))
        check(f"{code} 没有横穿式假跳变（<35km）", worst < 35, f"最大 {worst:.1f} km")
        check(f"{code} 坐标全是 [lat,lon] 合法值",
              all(-90 <= a <= 90 and -180 <= b <= 180 for a, b in r["path"]))

    print("== 3. 防止贴到隔壁岔线 ==")
    k = T.track("K106")
    bad = [p for p in k["pieces"] if p["mode"] == "real" and "柳广线" in p["line"]]
    check("北京丰台→北京西 不再误贴柳广线", not bad,
          str([(p["from"], p["to"], p["line"]) for p in bad]))
    check("短站间距一律走直线",
          all(p["mode"] == "straight" for p in k["pieces"] if p["km"] < T.MIN_REAL_KM))

    print("== 4. 没有整理走向的线路 → 全部停站间直线 ==")
    fake = {
        "code": "TEST-NONE", "date": "2026-11-06",
        "stops": [
            {"name": "远海一", "lat": 8.0, "lon": 108.0},
            {"name": "远海二", "lat": 8.6, "lon": 108.7},
            {"name": "远海三", "lat": 9.3, "lon": 109.4},
        ],
    }
    r = T.track("TEST-NONE", "2026-11-06", detail=fake)
    check("虚构远海线路全直线", r["ok"] and r["realSegs"] == 0
          and r["straightSegs"] == 2, str(r.get("pieces")))
    check("全直线时端点就是站点坐标",
          r["path"] == [[8.0, 108.0], [8.6, 108.7], [9.3, 109.4]], str(r["path"]))

    print("== 5. 缓存 / 失效 ==")
    a = T.track("G1")
    t0 = time.time()
    b = T.track("G1")
    check("第二次命中缓存", a is b)
    check("缓存命中够快", time.time() - t0 < 0.05)
    before = len(T._TRACK)
    T.invalidate()
    check("invalidate 清掉结果缓存", len(T._TRACK) == 0, f"之前 {before}")
    check("invalidate 清掉线路索引", T._LINES is None and T._STOPS is None)
    check("失效后能重建", len(T.lines()) > 1000)

    print("\n绿 %d / 红 %d" % (OK, len(BAD)))
    if BAD:
        for x in BAD:
            print("  -", x)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
