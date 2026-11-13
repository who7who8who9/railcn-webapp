# -*- coding: utf-8 -*-
"""test_osmedit.py — OSM 走向编辑器后端冒烟测试

覆盖：列表、载入抽稀、保存草稿（line_geom_edit）、重载草稿、应用到线路库（不存在的线应安全跳过）。
不污染真实几何：测试用临时线名，结束后清理 line_geom_edit。
"""
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "webapp"))
import cnrail  # noqa: E402
import osmedit  # noqa: E402
import railcore  # noqa: E402

RAIL_DB = os.path.join(os.path.dirname(__file__), "..", "webapp", "data", "rail.db")
TEST = "测试线_osmedit_临时"

passed = 0
failed = 0


def ok(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✓ {name}")
    else:
        failed += 1
        print(f"  ✗ {name}  {extra}")


def _purge(name: str) -> None:
    """删掉测试建的线（线路库 + 几何 + 草稿）"""
    try:
        railcore.delete_custom_line(name)
    except Exception:  # noqa: BLE001
        pass
    c = sqlite3.connect(RAIL_DB)
    c.execute("DELETE FROM line_geom WHERE name=?", (name,))
    c.execute("DELETE FROM line_geom_edit WHERE name=?", (name,))
    c.commit()
    c.close()
    railcore._reload_coord_cache() if hasattr(railcore, "_reload_coord_cache") else None


def _has_geom(name: str) -> bool:
    c = sqlite3.connect(RAIL_DB)
    row = c.execute("SELECT 1 FROM line_geom WHERE name=?", (name,)).fetchone()
    c.close()
    return bool(row)


def cleanup():
    c = sqlite3.connect(RAIL_DB)
    c.execute("DELETE FROM line_geom_edit WHERE name=?", (TEST,))
    c.commit()
    c.close()


def main():
    # 1) 列表
    lst = osmedit.list_lines("沪昆", 20)
    names = [i["name"] for i in lst["items"]]
    ok("list_lines 返回含 沪昆线", "沪昆线" in names, str(names[:5]))

    # 1b) 索引只收「真有走向」的线：全量覆盖 + 分页 + 借走向别名（2026-11-09）
    LD_DB = os.path.join(os.path.dirname(__file__), "..", "webapp", "data", "linedesk.db")
    a0 = osmedit.list_lines("", 100, 0)
    ok("索引覆盖 4000+ 条有走向线路", a0["total"] > 4000, str(a0["total"]))
    ok("空搜索 count=total 且 hasMore", a0["count"] == a0["total"] and a0["hasMore"] is True)
    ok("首批 100 条无重复", len({i["name"] for i in a0["items"]}) == 100)
    a1 = osmedit.list_lines("", 100, a0["total"] - 100)
    ok("能翻到最后一页", len(a1["items"]) == 100 and a1["hasMore"] is False)
    ok("分页不重复", not ({i["name"] for i in a0["items"]} &
                       {i["name"] for i in a1["items"]}))
    jn = osmedit.list_lines("济南线", 20)
    # 2026-11-10：济南线是整理台已确认线路，改走「规范名 + ld_geom」专属通道，
    # 不再以 alias 借「京沪四线」的几何；列表标红、载入/保存都按规范名。
    cf = next((i for i in jn["items"] if i["name"] == "济南线"), None)
    ok("整理台已确认线路也在列表里（济南线，红名）",
       bool(cf and cf["confirmed"] and cf["ldId"]
            and cf["loadName"] == "济南线"
            and cf["geomName"] == "京沪四线"), str(cf))
    ga = osmedit.load_geom("济南线", 100)
    ok("load_geom 按规范名走确认通道拿到走向",
       bool(ga and ga.get("parts") and ga.get("confirmed")
            and ga.get("loadName") == "济南线"))
    # 整理台里「没走向的草稿」不该再混进列表
    d = sqlite3.connect(LD_DB)
    drafts = [r[0] for r in d.execute(
        "SELECT name FROM ld_line WHERE status='draft'"
        " AND (geom_src IS NULL OR geom_src='') LIMIT 200")]
    geom_names = {r[0] for r in d.execute("SELECT name FROM ld_geom")}
    d.close()
    c = sqlite3.connect(RAIL_DB)
    geom_names |= {r[0] for r in c.execute("SELECT name FROM line_geom")}
    c.close()
    no_geom = [n for n in drafts if n not in geom_names]
    if no_geom:
        ok("无走向草稿不进列表", no_geom[0] not in
           [i["name"] for i in osmedit.list_lines(no_geom[0], 20)["items"]], no_geom[0])

    # 2) 载入真实线并抽稀：现在保留主站那份多段，不再压成一条 chain
    g = osmedit.load_geom("沪昆线", 600)
    npt = sum(len(s) for s in g["parts"]) if g and g.get("parts") else 0
    ok("load_geom 沪昆线 有多段 parts", g and g.get("parts") and npt > 2,
       "nPart=%s nPt=%s" % (g and g.get("nPart"), npt))
    ok("load_geom 抽稀点数受控", g and npt <= 2000, str(npt))
    ok("load_geom 段数原样保留", g and g.get("nPart") == len(g["parts"]) > 1,
       str(g and g.get("nPart")))
    ok("load_geom 含坐标顺序 [lon,lat]", g and abs(g["parts"][0][0][0]) <= 180 and abs(g["parts"][0][0][1]) <= 90)
    ok("load_geom 返回停运里程数", g and g.get("totalKm", 0) > 0, str(g and g.get("totalKm")))

    # 3) 保存草稿（临时线名）
    chain = [[116.0, 39.0], [116.1, 39.05], [116.2, 39.1], [116.3, 39.0]]
    res = osmedit.save_geom(TEST, chain, src="osmedit", note="test")
    ok("save_geom 成功", res.get("ok"))
    ok("save_geom 未误写 line_geom/ld_geom", (res.get("applied") or {}).get("lib") is False
       and (res.get("applied") or {}).get("ld") is False)

    # 4) 重载草稿
    g2 = osmedit.load_geom(TEST, 200)
    ok("重载草稿 hasEdit=true", g2 and g2.get("hasEdit") is True)
    ok("重载草稿点数一致", g2 and len(g2["parts"][0]) == 4, str(g2 and len(g2["parts"][0])))

    # 5) 应用到线路库：临时线名在 line_geom/ld_geom 不存在 → 安全跳过
    res2 = osmedit.save_geom(TEST, chain, apply=True)
    ok("apply 对不存在的线返回 applied=False（不误造几何）",
       (res2.get("applied") or {}).get("lib") is False and (res2.get("applied") or {}).get("ld") is False)

    # 5b) 目标几何已存在时 apply 真能写回（以前 UPDATE 带了不存在的 updated_at 列，
    #     一应用就 500）；借走向的线用 target 写回被借的那份几何。
    try:
        c = sqlite3.connect(RAIL_DB)
        c.execute(
            "INSERT OR REPLACE INTO line_geom(name,file,total_km,n_part,n_pt,props,parts_gz,created_at,spine_gz)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (TEST, "test", 1.0, 1, 2, "{}", osmedit._gz(chain[:2]), time.time(), None))
        c.commit()
        c.close()
        r4 = osmedit.save_geom(TEST + "_别名", chain, apply=True, target=TEST)
        ok("apply+target 写回目标几何", r4.get("appliedAny") is True, str(r4)[:140])
        c = sqlite3.connect(RAIL_DB)
        row = c.execute("SELECT n_pt FROM line_geom WHERE name=?", (TEST,)).fetchone()
        c.close()
        ok("line_geom 真的被更新（不再撞 updated_at 列）",
           bool(row and row[0] == len(chain)), str(row))
    finally:
        c = sqlite3.connect(RAIL_DB)
        c.execute("DELETE FROM line_geom WHERE name=?", (TEST,))
        c.execute("DELETE FROM line_geom_edit WHERE name=?", (TEST + "_别名",))
        c.commit()
        c.close()

    # 5c) 前端：离线自绘也能加/删点、列表滚动加载
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    js = open(os.path.join(ROOT, "webapp", "static", "osmedit.js"), encoding="utf-8").read()
    html = open(os.path.join(ROOT, "webapp", "static", "osmedit.html"), encoding="utf-8").read()
    ok("离线自绘模式不再被强制只读", "只读模式下不能编辑" not in js and "function bindCanvas()" in js)
    ok("列表支持滚动加载更多", "loadLines(S.listQ, true)" in js)
    ok("有显式删除选中点按钮", 'id="oeDel"' in html and "function deletePoint(i)" in js)
    ok("站点参考点默认隐藏", "showStops: false" in js and 'id="oeShowSta"' in html)

    # 6) 多段 parts 原样保留，绝不被压成一条 chain（压平会产生跨段假直线）
    chain2 = [[[117.0, 40.0], [117.1, 40.1]], [[117.2, 40.2], [117.3, 40.3]]]
    r3 = osmedit.save_geom(TEST, chain2, src="osmedit")
    g3 = osmedit.load_geom(TEST, 200)
    ok("多段 parts 原样保留、不压成单链",
       g3 and len(g3["parts"]) == 2
       and sum(len(s) for s in g3["parts"]) == 4,
       str(g3 and [len(s) for s in g3["parts"]]))
    ok("save_geom 回报段数", r3.get("nPart") == 2, str(r3.get("nPart")))

    cleanup()
    # 7) 清理后草稿消失
    g4 = osmedit.load_geom(TEST, 200)
    ok("清理后临时线草稿不可见", g4 is None or g4.get("hasEdit") is False)

    # 8) 抓站：京沪线走向能认出北京 / 上海，里程单调递增
    #    确认线路的 load_geom 现在返回主站那份多段几何（段间不连线），拿它
    #    当抓站样本会漏末站。抓站距离本来就按 _raw_parts() 原始碎段量，这里
    #    用原始多段串线只借它排序 / 算里程（界面里抓站只对「新建线路」开放，
    #    编辑已有线路不抓站）。
    ref = osmedit._cand_ldgeom("京沪线")
    chain5 = ref[1] if ref else osmedit.load_geom("京沪线", 150)["parts"][0]
    st = osmedit.suggest_stations([chain5], 2000, name="京沪线")
    nm_st = [i["name"] for i in st["items"]]
    ok("抓站认出首末站（北京 / 上海）", "北京" in nm_st and "上海" in nm_st, str(nm_st[:3] + nm_st[-3:]))
    kms = [i["km"] for i in st["items"]]
    ok("抓站里程单调不减", all(kms[i] <= kms[i + 1] for i in range(len(kms) - 1)))
    ok("抓站带距离与坐标", all(i.get("dist") is not None and i.get("lon") for i in st["items"]))
    t0 = time.time()
    osmedit.suggest_stations([chain5], 2000, name="京沪线")
    ok("抓站够快（<2s，原始碎段不串线）", time.time() - t0 < 2.0, "%.2fs" % (time.time() - t0))

    # 9) 建线：画的走向 + 挑的站 → 线路库里真的多一条（结束删除）
    NEW = "测试手绘线_osmedit_临时"
    _purge(NEW)
    stops = st["items"][:4]
    r = osmedit.create_line(NEW, [chain5], stops, note="")
    ok("create_line 返回 ok", r.get("ok") is True, str(r)[:120])
    ok("create_line 站数 = 选的 4 站", r.get("n") == 4, str(r.get("n")))
    ln = cnrail.line(NEW)
    ok("建完线路库里能查到", bool(ln), str(ln)[:80])
    ok("建完是自建线（raw 通道）", bool(ln) and ln.get("custom") is True)
    ok("建完写入 line_geom 走向", _has_geom(NEW))
    ok("建完里程 > 0", (r.get("totalKm") or 0) > 0, str(r.get("totalKm")))
    ok("建完留下草稿，编辑器能接着改", (osmedit.load_geom(NEW, 100) or {}).get("hasEdit") is True)

    # 10) 重名保护：再建一次应被拦下（自建线需 replace=1）
    try:
        osmedit.create_line(NEW, [chain5], stops)
        ok("同名自建线再建被拦", False)
    except ValueError as e:
        ok("同名自建线再建被拦", "已经有" in str(e), str(e))
    r2 = osmedit.create_line(NEW, [chain5], stops, replace=True)
    ok("replace=1 可覆盖同名自建线", r2.get("ok") is True)

    # 11) 站数不足要报人话
    try:
        osmedit.create_line(NEW + "x", [chain5], stops[:1], replace=True)
        ok("少于 2 站被拦", False)
    except ValueError as e:
        ok("少于 2 站被拦", "2 个车站" in str(e), str(e))

    _purge(NEW)
    _purge(NEW + "x")
    ok("清理后测试线不在库里", cnrail.line(NEW) is None)

    # 12) 删线时草稿要跟着清（否则删完线它还在编辑器列表里冒充一条线）
    D = NEW + "_草稿"
    osmedit.save_geom(D, [[116.0, 39.0], [116.2, 39.1]], src="draw")
    ok("草稿已存在", (osmedit.load_geom(D, 50) or {}).get("hasEdit") is True)
    ok("drop_draft 返回 True", osmedit.drop_draft(D) is True)
    ok("drop_draft 后读不到草稿", osmedit.load_geom(D, 50) is None)

    print(f"\n通过 {passed} / 失败 {failed}")
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
