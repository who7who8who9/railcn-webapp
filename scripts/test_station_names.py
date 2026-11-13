# -*- coding: utf-8 -*-
"""回归：站名统一不带「站」后缀（杭州东站 / 杭州东 合并成一个）

规则（`cnrail.station_name()` / `railcore.canon_station()`）：
  * 「XX站」的裸名本身是认得的站 → 去掉「站」（杭州东站 → 杭州东、北京南站 → 北京南）
  * 裸名不是站名 → 原样保留（十八站、九站、新九站、同名站）
  * 重复一层后缀的并掉（十八站站 → 十八站）
  * 名字里的空格不碰（Yên Viên 不能被压成 YênViên）

服务要在跑：python webapp/server.py --port 8686
"""
import json
import os
import sqlite3
import sys
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
WEBAPP = os.path.join(os.path.dirname(HERE), "webapp")
sys.path.insert(0, WEBAPP)

import cnrail                                                # noqa: E402
import railcore as rc                                        # noqa: E402

BASE = "http://127.0.0.1:8686"
RAIL_DB = os.path.join(WEBAPP, "data", "rail.db")
bad = []


def ok(cond, label, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label + (("  " + str(detail)) if detail else ""))
    if not cond:
        bad.append(label)


def get(path):
    return json.load(urllib.request.urlopen(BASE + path, timeout=60))


def test_rules():
    print("== 命名规则")
    rc.stations.ensure()
    cases = {
        "杭州东站": "杭州东",
        "北京南站": "北京南",
        "十八站": "十八站",
        "九站": "九站",
        "新九站": "新九站",
        "十八站站": "十八站",
        "郑州航空港站站": "郑州航空港",
        "新站站": "新站",
        "同名站": "同名站",
        "Yên Viên": "Yên Viên",
    }
    for src, want in cases.items():
        got = rc.canon_station(src)
        ok(got == want, f"{src} → {want}", got)


def test_station_api():
    print("== 车站详情 / 联想")
    d = get("/api/station?q=" + urllib.parse.quote("杭州东"))
    ok(d.get("found"), "杭州东查得到")
    ok(d.get("name") == "杭州东", "车站详情名字不带「站」", d.get("name"))
    ok(d.get("lat") == 30.293752 or d.get("lat") is not None, "杭州东有坐标", (d.get("lat"), d.get("lon")))
    d2 = get("/api/station?q=" + urllib.parse.quote("杭州东站"))
    ok(d2.get("name") == d.get("name"), "带「站」查同一个站，名字一致", d2.get("name"))

    for q in ("杭州东", "十八站", "十八站站"):
        items = get("/api/stations?q=" + urllib.parse.quote(q) + "&limit=12")["items"]
        names = [x["name"] for x in items]
        dup = [n for n in names if n.endswith("站") and len(n) > 2 and n[:-1] in names]
        ok(not dup, f"联想「{q}」里没有「XX / XX站」两条", names[:6])
    items = get("/api/stations?q=" + urllib.parse.quote("十八站") + "&limit=12")["items"]
    ok("十八站站" not in [x["name"] for x in items], "十八站站已并进十八站")


def test_line_api():
    print("== 线路站序")
    for nm in ("沪昆线", "京滨城际", "京沪线"):
        d = get("/api/line?q=" + urllib.parse.quote(nm))
        names = [s["name"] for s in (d.get("stations") or [])]
        if not names:
            ok(True, f"{nm} 跳过（不在库里）")
            continue
        bad_names = [n for n in names if n.endswith("站") and len(n) > 2
                     and not rc.canon_station(n) == n]
        ok(not bad_names, f"{nm} 站名都去掉了「站」", bad_names[:4])
    d = get("/api/line?q=" + urllib.parse.quote("沪昆线"))
    hit = [s for s in d.get("stations") or [] if s["name"] == "杭州东"]
    ok(bool(hit), "沪昆线里有「杭州东」")
    ok(hit and hit[0].get("lon") == 120.208454, "沪昆线杭州东用的是人工坐标",
       hit and (hit[0].get("lat"), hit[0].get("lon")))
    stops = get("/api/line/stops?q=" + urllib.parse.quote("沪昆线"))
    names = stops.get("items") or []
    ok(bool(names), "选经由能拿到沪昆线站序", len(names))
    ok(all(not (n.endswith("站") and len(n) > 2 and rc.canon_station(n) != n) for n in names),
       "选经由站序也不带多余的「站」", [n for n in names if n.endswith("站")][:4])


def test_db():
    print("== 库里的名字")
    con = sqlite3.connect(RAIL_DB)
    con.row_factory = sqlite3.Row
    rows = list(con.execute("SELECT key, name FROM station_master"))
    names = [r["name"] for r in rows]
    dup = sorted({n for n in names if names.count(n) > 1})
    ok(not dup, "station_master 里没有同名两条", dup[:4])
    leftover = [n for n in names if n.endswith("站") and len(n) > 2
                and rc.canon_station(n) != n]
    ok(not leftover, "station_master 没有还能再简化的「站」", leftover[:4])
    cust = [r["name"] for r in con.execute("SELECT name FROM station_custom")]
    more = [n for n in cust if n.endswith("站") and len(n) > 2 and rc.canon_station(n) != n]
    ok(not more, "station_custom 没有还能再简化的「站」", more[:4])
    con.close()
    with open(os.path.join(WEBAPP, "data", "station_coords.json"), encoding="utf-8") as f:
        coords = json.load(f)
    still = [k for k in coords if k.endswith("站") and rc.canon_station(k) != k]
    ok(not still, "坐标表里没有还能再简化的「站」键", still[:4])
    ok("杭州东" in coords and "杭州东站" not in coords,
       "坐标表按裸名存（杭州东 / 没有杭州东站）")


def main():
    test_rules()
    test_station_api()
    test_line_api()
    test_db()
    print("\n%s（%d 项失败）" % ("全部通过" if not bad else "失败：" + "、".join(bad), len(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
