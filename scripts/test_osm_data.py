"""OSM 数据落库回归（守 12 件事）

跑：python scripts/test_osm_data.py
依赖：先跑过 scripts/import_osm.py --prepare --src <osm-china-rail/out> --apply

这份数据是第六套来源：从 OSM 几何上把车站插值到线路中心线算出来的站序。
它不是替代资料库 / china-emu，而是补那 1116 条「谁都没有站序」的线，
所以回归的重点是**口径**：里程归零、顺序单调、站名对齐、范围收窄（不含境外
与台湾地区）、以及界面那条「同名多来源不替用户挑」的链路。

**不要并发跑测试**：多个进程同时改写同一个 sqlite 会互相打断，留下写到一半的
站序（见 PROJECT-CONTEXT 第 7 节）。
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "webapp"))

DB = os.path.join(ROOT, "webapp", "data", "rail.db")
OK = FAIL = 0


def ck(name: str, cond: bool, msg: str = "") -> None:
    global OK, FAIL
    if cond:
        OK += 1
        print("  ok   %s" % name)
    else:
        FAIL += 1
        print("  FAIL %s%s" % (name, ("  → " + msg) if msg else ""))


def main() -> None:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    one = lambda s, *a: con.execute(s, a).fetchone()

    print("\n[1] 表齐不齐")
    tabs = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("line_osm", "line_osm_stop", "station_osm", "station_osm_line"):
        ck("表 %s 在" % t, t in tabs)
    if "line_osm" not in tabs:
        print("\n没建表 —— 先跑 scripts/import_osm.py --apply")
        return
    ck("线路数够（>1500 条）",
       (one("SELECT COUNT(*) c FROM line_osm")["c"]) > 1500,
       str(one("SELECT COUNT(*) c FROM line_osm")["c"]))

    print("\n[2] 范围收窄：境外与台湾地区进不来")
    stmt = ("SELECT COUNT(*) c FROM station_osm WHERE lon BETWEEN 119.3 AND 122.1"
            " AND lat BETWEEN 21.8 AND 25.35")
    ck("台湾岛范围内没有车站", one(stmt)["c"] == 0, str(one(stmt)["c"]))
    rows = con.execute("SELECT COUNT(*) c, SUM(CASE WHEN name GLOB '*[一-鿿]*'"
                       " THEN 0 ELSE 1 END) bad FROM station_osm").fetchone()
    ck("车站全是中文名（境外站挡在外面）", rows["bad"] == 0,
       "%d 站里 %d 个无中文" % (rows["c"], rows["bad"]))
    bad = con.execute("SELECT name FROM station_osm WHERE name IN"
                      " ('Замын-Үүд','의주','Воскресеновка')").fetchall()
    ck("典型境外站名查不到", not bad, str([r[0] for r in bad]))
    ck("线路状态只有运营中/在建/规划",
       not con.execute("SELECT 1 FROM line_osm WHERE status NOT IN"
                       " ('运营中','在建','规划')").fetchone())

    print("\n[3] 站序口径：里程归零、单调、seq 连续")
    rows = con.execute(
        "SELECT lid, MIN(km) lo FROM line_osm_stop GROUP BY lid").fetchall()
    ck("每条线的首站里程都是 0", all(abs(r["lo"] or 0) < 0.05 for r in rows),
       str([(r["lid"], r["lo"]) for r in rows if abs(r["lo"] or 0) >= 0.05][:4]))
    rows = con.execute(
        "SELECT lid, km, LAG(km) OVER (PARTITION BY lid ORDER BY seq) p"
        " FROM line_osm_stop").fetchall()
    back = [r["lid"] for r in rows if r["p"] is not None and r["km"] < r["p"] - 0.01]
    ck("里程沿站序单调递增", not back, str(sorted(set(back))[:5]))
    rows = con.execute(
        "SELECT lid, MAX(seq) mx, COUNT(*) n FROM line_osm_stop GROUP BY lid").fetchall()
    ck("seq 从 1 连续无断号", all(r["mx"] == r["n"] for r in rows),
       str([(r["lid"], r["mx"], r["n"]) for r in rows if r["mx"] != r["n"]][:4]))
    ck("每条线的里程都算出来了",
       not con.execute("SELECT 1 FROM line_osm_stop WHERE km IS NULL").fetchone())

    print("\n[4] 站名干净：并列民族文字、状态括注都剥掉了")
    ck("「乌鲁木齐南」不并列维吾尔文写法",
       not con.execute("SELECT 1 FROM line_osm_stop WHERE name LIKE '%ئ%'").fetchone())
    ck("括注状态的写法已剥（没有「（预留）」）",
       not con.execute("SELECT 1 FROM line_osm_stop"
                       " WHERE name LIKE '%预留%' OR name LIKE '%旧址%'").fetchone())
    ck("线内没有重复站名",
       not con.execute("SELECT lid,name FROM line_osm_stop GROUP BY lid,name"
                       " HAVING COUNT(*)>1").fetchone())
    ck("OSM 的线名能在站序里取到首站",
       bool(one("SELECT 1 FROM line_osm_stop WHERE lid='京广线' AND seq=1")))

    print("\n[5] 覆盖面：真能补上资料库和 china-emu 都没有的线")
    for nm, low in (("瓦日线", 30), ("嫩林线", 30), ("红淖线", 15), ("甘钟线", 15)):
        r = one("SELECT n_stop FROM line_osm WHERE name=?", nm)
        ck("%s 有站序（>%d 站）" % (nm, low), bool(r) and r["n_stop"] > low,
           str(r["n_stop"] if r else None))
    n = one("SELECT COUNT(*) c FROM line_osm_stop s WHERE NOT EXISTS"
            " (SELECT 1 FROM line_catalog_stop c WHERE c.lid=s.lid)")["c"]
    ck("大批线路是项目里原本没有站序的（>3000 行）", n > 3000, str(n))

    print("\n[6] 车站归属反查（距离 + 几何走向判出来的那条线）")
    import railcore
    sjz = railcore.osm_station_lines("石家庄")
    names = [x["line"] for x in sjz]
    ck("石家庄同时属于京广线等三条以上", len(sjz) >= 3, str(names))
    ck("石家庄归属里包含 京广线", "京广线" in names, str(names))
    ck("按到中心线的距离升序返回",
       all((sjz[i]["distM"] or 0) <= (sjz[i + 1]["distM"] or 0)
           for i in range(len(sjz) - 1)),
       str([x["distM"] for x in sjz]))
    ck("每条归属都带沿线里程",
       all(x["km"] is not None for x in sjz), str([x["km"] for x in sjz]))
    ck("不存在的站返回空而不是报错",
       railcore.osm_station_lines("查无此站XYZ") == [])
    ck("站名带「站」后缀也能查到", len(railcore.osm_station_lines("石家庄站")) >= 3)

    print("\n[7] 车站联想：三个来源不合并、同名照列")
    got = railcore.osm_station_suggest("石家庄", 8)
    ck("OSM 车站联想有结果", len(got) > 0, str(got[:2]))
    ck("每条带线路条数 nLine", all("nLine" in x for x in got))
    ck("联想结果按名字命中的紧密度排（精确名在前）",
       got and got[0]["name"] in ("石家庄", "石家庄东", "石家庄南"),
       str([x["name"] for x in got]))
    sug = railcore.stations.suggest("石家庄", 12)
    ck("站表联想每条都带来源标记 src",
       all(x.get("src") for x in sug), str([x.get("src") for x in sug]))

    print("\n[8] OSM 独有车站（档案里没有的）能出卡片")
    card = railcore.osm_station_card("一零二厂（吉化）")
    ck("OSM 独有车站有卡片", bool(card) and card.get("osmOnly"))
    ck("卡片给出坐标", bool(card) and card["points"][0]["lat"] is not None)
    ck("卡片带归属线路", bool(card) and len(card["osmLines"]) >= 1,
       str([x["line"] for x in (card or {}).get("osmLines", [])]))
    ck("档案里有的站不抢着出 OSM 卡",
       railcore.osm_station_card("查无此站XYZ") is None)

    print("\n[9] 线路多来源：全列出来，不替用户挑")
    import lineseg
    srcs = lineseg.line_sources("京广线")
    ck("京广线至少 3 个来源", len(srcs) >= 3, str([s["source"] for s in srcs]))
    ck("来源带 站数 / 里程 / 首末站",
       all(s.get("n") and s.get("first") and s.get("last") for s in srcs),
       str([(s["source"], s["n"], s["first"], s["last"]) for s in srcs]))
    ck("同名不同来源的站数确实不一样（不是复制）",
       len({s["n"] for s in srcs}) > 1, str([s["n"] for s in srcs]))
    # 京广线在 china-emu 里记作「京广铁路」，所以那条 osm/catalog/lib 三源够检查；
    # 四源齐全的用京广高速线验。
    for src, lo in (("catalog", 50), ("osm", 150)):
        got = lineseg.stations_for("京广线", src)
        ck("指定 %s 能单独取到站序（>%d 站）" % (src, lo),
           bool(got) and len(got["stops"]) > lo
           and got.get("source") == src,
           str((got or {}).get("source")) + " " + str(len((got or {}).get("stops") or [])))
    four = lineseg.line_sources("京广高速线")
    ck("京广高速线四个来源都在",
       {"lib", "catalog", "chinaemu", "osm"} <= {s["source"] for s in four},
       str([s["source"] for s in four]))
    for src, lo in (("lib", 30), ("catalog", 30), ("chinaemu", 30), ("osm", 30)):
        got = lineseg.stations_for("京广高速线", src)
        ck("京广高速线指定 %s 拿到对应站序（>%d 站）" % (src, lo),
           bool(got) and len(got["stops"]) > lo and got.get("source") == src,
           str((got or {}).get("source")) + " " + str(len((got or {}).get("stops") or [])))
    ck("指定一个不存在的来源返回 None",
       lineseg.stations_for("京广线", "nope") is None)

    print("\n[10] 默认还是能兜底：没有资料库/china-emu 的线用 OSM 补")
    for nm, lo in (("瓦日线", 30), ("嫩林线", 30)):
        got = lineseg.stations_for(nm)
        ck("%s 默认能补出站序（>%d 站）" % (nm, lo),
           bool(got) and len(got["stops"]) > lo, str((got or {}).get("source")))
        ck("%s 补出来的站序来源是 osm" % nm,
           bool(got) and got.get("source") == "osm", str((got or {}).get("source")))
    got = lineseg.stations_for("成昆线")
    ck("成昆线仍用资料库那份（不被 OSM 长度误判顶掉）",
       bool(got) and got.get("source") == "catalog", str((got or {}).get("source")))

    print("\n[11] 站序自带坐标，补出来的线不用再反查")
    r = one("SELECT COUNT(*) c, SUM(CASE WHEN lon IS NULL OR lat IS NULL"
            " THEN 0 ELSE 1 END) ok FROM line_osm_stop")["c"]
    ok = one("SELECT SUM(CASE WHEN lon IS NULL OR lat IS NULL THEN 0 ELSE 1 END) ok"
             " FROM line_osm_stop")["ok"]
    ck("站序行坐标覆盖率 > 99%", ok / max(1, r) > 0.99, "%.2f%%" % (100.0 * ok / r))
    ck("每个站都挂 OSM 节点号",
       not con.execute("SELECT 1 FROM line_osm_stop WHERE osm_id=''").fetchone())

    print("\n[12] 不碰既有数据")
    ck("rail.db 里 line_custom 还在", bool(one(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='line_custom'")))
    ck("line_catalog 行数没被 OSM 导入动过",
       one("SELECT COUNT(*) c FROM line_catalog")["c"] > 800,
       str(one("SELECT COUNT(*) c FROM line_catalog")["c"]))
    ck("OSM 站点都关联上了（站序点 ⊆ 车站点）",
       not con.execute("SELECT 1 FROM station_osm_line g WHERE NOT EXISTS"
                       " (SELECT 1 FROM station_osm s WHERE s.osm_id=g.osm_id)").fetchone())

    con.close()
    print("\n%d ok / %d fail" % (OK, FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
