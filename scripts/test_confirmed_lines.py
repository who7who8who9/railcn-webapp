# -*- coding: utf-8 -*-
"""
test_confirmed_lines.py — 「车站/线路 只认整理台完成全部确认的线路」回归

守这些事（2026-11-06 用户口径）：
  1. 白名单判定：status=confirmed + line_ok + stop_ok + ≥2 停站 + 有真实走向
  2. 未完成确认的线（草稿 / 底表 / 备份版本）一律查不到，联想也不列
  3. 确认线路详情必须带**停站**（名称/里程/定位）和**线路路线位置**（真实走向折线）
  4. 详情不返回来源版本 / ★ 选版本 / OSM 候选（前端据此隐藏选线控件）
  5. 整理台 ③ 合并保存：缺停站 / 缺走向 / 缺定位时拒绝确认
  6. 老口径（不带 confirmed=1）完全不受影响

跑法：python scripts/test_confirmed_lines.py
只读正式库；finalize 校验部分用临时 linedesk.db，不碰正式整理库。
"""
from __future__ import annotations

import gzip
import json
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "webapp"))

import confirmed                                             # noqa: E402
import linedesk                                              # noqa: E402

OK = 0
FAIL = 0


def ck(cond, msg: str) -> None:
    global OK, FAIL
    if cond:
        OK += 1
        print("  ok  " + msg)
    else:
        FAIL += 1
        print("  FAIL " + msg)


_NEG = ""


def _neg_sample() -> str:
    """挑一条「整理台里有站序、但没完成全部确认」的线当负样本。

    别写死线名：用户会陆续在整理台把线确认掉 —— 2026-11-11「京广线」被确认后，
    原来拿它当「未确认线」的 5 项断言（get / detail / geom / HTTP found=false）
    全红了。也别去 cnrail 档案里挑：底表 2026-11-02 清空后
    `data/cnrail_lines.json` 是空的，线路都在整理库里，`search_line()` 返回 0 条。
    """
    global _NEG
    if _NEG:
        return _NEG
    cf = set(confirmed.names())
    for it in linedesk.list_lines("", limit=500)["items"]:
        nm = it.get("name") or ""
        if (nm and nm not in cf and "（" not in nm
                and it.get("status") != "confirmed" and (it.get("n_stop") or 0) >= 2):
            _NEG = nm
            break
    return _NEG


def test_whitelist() -> None:
    print("[1] 白名单：只认整理台完成全部确认的线路")
    st = confirmed.stats()
    ck(st["confirmed"] >= 1, "至少有 1 条确认线路：%d" % st["confirmed"])
    ck(st["draft"] > 0, "库里仍有草稿（%d 条），说明没有把草稿混进来" % st["draft"])
    ck(st["incomplete"] == 0, "确认线路里没有「缺停站 / 缺走向」的")
    names = set(st["lines"])
    ck("京沪高速线" in names and "沪昆线" in names,
       "人工定稿的京沪高速线 / 沪昆线在白名单里：%s" % "、".join(st["lines"][:8]))
    ck(all("（" not in n for n in names), "白名单里没有带「（来源）」后缀的版本名")

    items = confirmed.list_all("")
    ck(len(items) == st["confirmed"], "空搜索返回全部 %d 条确认线路" % st["confirmed"])
    # 走向点数只卡整理台定稿的那几条；用户用走向编辑器新建的线可能只画了站序、
    # 没配走向（如「济南线」7 站 / 33 km），别把某个时点的清单写死在回归里。
    core = {"京沪线", "京沪高速线", "沪昆线", "陇海线"}
    for it in items:
        ck((it["n"] or 0) >= 2, "%s：有停站 %d 个" % (it["name"], it["n"]))
        ck((it["nCoord"] or 0) >= 2, "%s：停站带定位 %d 个" % (it["name"], it["nCoord"]))
        if it["name"] in core:
            ck((it["nGeomPt"] or 0) > 0, "%s：有线路走向 %d 点" % (it["name"], it["nGeomPt"]))
        ck(it["totalKm"] is not None, "%s：有总里程 %.1f km" % (it["name"], it["totalKm"] or 0))

    print("[2] 过滤：其它线路与来源版本一个都不给")
    # 别把「京广」写死成草稿线：用户 2026-11-10 在整理台定稿了「京广高铁」，
    # 搜「京广」现在理应命中它。只卡「结果全在白名单里」，不对具体线名下注。
    ck(not confirmed.list_all("__回归未确认线__"), "编造的未确认线名搜不到")
    ck(all(x["name"] in names for x in confirmed.list_all("京广")),
       "搜「京广」只给白名单里已确认的线路（京广高铁已定稿）")
    ck(not confirmed.list_all("京沪线（底表）"), "备份版本「京沪线（底表）」搜不到")
    NEG = _neg_sample()
    ck(bool(NEG) and not confirmed.get(NEG),
       "get('%s') 返回 None（未确认的线取不到）" % NEG)
    ck(confirmed.get("京沪高速线") is not None, "get('京沪高速线') 命中")
    two = [x["name"] for x in confirmed.list_all("京沪")]
    # 别名 / 走向来源名里带「京沪」的确认线也会命中（如「济南线」借了
    # 「京沪四线」的走向），所以只卡「两条主线都在、且没有来源版本」。
    ck("京沪线" in two and "京沪高速线" in two and all("（" not in n for n in two),
       "搜「京沪」命中两条主线、无来源版本（共 %d 条：%s）"
       % (len(two), "、".join(two)))
    ck("京沪高速线" in confirmed.names(), "names() 是白名单集合")


def test_detail() -> None:
    print("[3] 详情：停站 + 线路路线位置一起给")
    # 站数跟着整理库走：用户可能重做 ② 换了站点来源（如京沪线 124 → OSM 166 站），
    # 别把某个时点的数字写死在回归里。
    for nm in ("京沪线", "京沪高速线", "沪昆线"):
        n = (confirmed.get(nm) or {}).get("n_stop") or 0
        d = confirmed.detail_payload(nm)
        ck(bool(d), "%s：能取到确认线路详情" % nm)
        if not d:
            continue
        ck(d["confirmedOnly"] is True and d["from"] == "confirmed",
           "%s：标记为整理台确认视图" % nm)
        ck(len(d["stations"]) == n == d["n"], "%s：%d 站" % (nm, n))
        ck(all(s.get("name") for s in d["stations"]), "%s：每站都有站名" % nm)
        ck(sum(1 for s in d["stations"] if s.get("lat") is not None) >= 2,
           "%s：停站带定位（%d%%）" % (nm, d["coordCoverage"]))
        ck(d["totalKm"] is not None and d["totalKm"] > 0,
           "%s：总里程 %.1f km" % (nm, d["totalKm"]))
        ck(d["osm"] is not None and (d["osm"].get("nPt") or 0) > 0,
           "%s：有真实走向元数据（%s）" % (nm, (d["osm"] or {}).get("file")))
        ck(d["sources"] == [] and d["group"] == {} and d["osmOther"] == [],
           "%s：不返回来源版本 / ★ / OSM 候选" % nm)
        ck((d["confirm"] or {}).get("ok") is True,
           "%s：confirm 完成度 ok" % nm)
        g = confirmed.geom(nm)
        ck(bool(g and g.get("parts") and any(len(p) > 1 for p in g["parts"])),
           "%s：能拿到可画的走向折线（%d 点）"
           % (nm, sum(len(p) for p in g["parts"]) if g else 0))
    ck(confirmed.detail_payload(_neg_sample()) is None,
       "未确认线取不到详情（%s）" % _neg_sample())
    ck(confirmed.geom(_neg_sample()) is None,
       "未确认线取不到走向（%s）" % _neg_sample())


def test_geom_source_alias() -> None:
    """规范名 ≠ 走向来源名的确认线路，也要能画出走向（2026-11-09 济南线）"""
    print("[3b] 改名收编的确认线路：按规范名 / 来源名都能取到走向")
    alias = [(it["name"], it["geomName"]) for it in confirmed.list_all("")
             if it.get("geomName") and it["geomName"] != it["name"]]
    if not alias:
        print("  skip  当前没有「规范名 ≠ 走向来源名」的确认线路")
        return
    for nm, gn in alias[:3]:
        a, b = confirmed.geom(nm), confirmed.geom(gn)
        ck(bool(a and a.get("parts")), "%s：按规范名取到走向折线" % nm)
        ck(bool(b and b.get("parts")), "%s：按来源名「%s」也取到走向折线" % (nm, gn))
        # merged_geom 的 name 字段跟着请求名走（规范名 / 来源名会不同），
        # 但折线本身必须是同一份，比点数而不是比 name。
        pts = lambda g: sum(len(p) for p in (g or {}).get("parts") or [])
        ck(pts(a) > 0 and pts(a) == pts(b),
           "%s：两种叫法返回同一份走向（%d 点）" % (nm, pts(a)))
    ck(confirmed.geom(_neg_sample()) is None,
       "未确认线仍然取不到走向（回退不放宽白名单）：%s" % _neg_sample())
    src = open(os.path.join(ROOT, "webapp", "static", "app.js"),
               encoding="utf-8").read()
    ck("const qname = (confirmedOnly && LN.data && LN.data.name)" in src,
       "前端确认视图用规范名请求 /api/line/osm")


def test_old_api() -> None:
    print("[4] 老口径不受影响")
    # confirmed=1 是显式开关：不带这个参数时 /api/lines 仍走 cnrail.search_line
    src = open(os.path.join(ROOT, "webapp", "server.py"), encoding="utf-8").read()
    ck('if one("confirmed") in ("1", "yes", "true"):' in src,
       "server.py 的 /api/lines 走显式 confirmed 开关")
    ck("items = cnrail.search_line(kw" in src, "/api/lines 老分支仍是 cnrail.search_line")
    ck('if one("confirmed") in ("1", "yes", "true"):\n                nm = one("q")' in src,
       "/api/line 的确认分支在老逻辑之前")
    # 几何解析已抽到 lineosm.geom()，主站与走向编辑器共用同一份 parts
    ck("lineosm.geom(nm" in src, "/api/line/osm 走公共几何入口")
    los = open(os.path.join(ROOT, "webapp", "lineosm.py"), encoding="utf-8").read()
    ck("confirmed.geom(nm)" in los, "lineosm.py 的确认分支接整理台走向")
    ck("linedesk.merged_geom(b[\"ld_id\"], nm)" in los,
       "lineosm.py 保留基本线路 ld_geom 优先顺序")
    ck(confirmed.get(_neg_sample()) is None,
       "整理台里的草稿线「%s」不在白名单里（老口径才看得到）" % _neg_sample())


def test_finalize_guard() -> None:
    """临时库：③ 合并保存必须拦住「缺停站 / 缺走向 / 缺定位」"""
    print("[5] 整理台 ③ 合并保存的完整性校验（临时库）")
    tmp = os.path.join(tempfile.gettempdir(), "test_confirmed_%d.db" % os.getpid())
    for suf in ("", "-wal", "-shm"):
        try:
            os.remove(tmp + suf)
        except OSError:
            pass
    old_db = linedesk.DB_PATH
    linedesk.DB_PATH = tmp
    try:
        linedesk.init()
        lid = linedesk.create_line("测试确认线", "", "回归用")["id"]
        with linedesk.conn() as c:
            c.execute("UPDATE ld_line SET line_ok=1, stop_ok=1,"
                      " src_line='osm:测试确认线', src_stop='custom:测试确认线' WHERE id=?",
                      (lid,))
            # 先只给 1 站
            c.execute("INSERT INTO ld_stop(line_id,seq,name,km,lat,lon) VALUES(?,?,?,?,?,?)",
                      (lid, 1, "甲", 0, 30.0, 120.0))
        try:
            linedesk.finalize(lid)
            ck(False, "只有 1 站时应当拒绝确认")
        except ValueError as e:
            ck("2 个停站" in str(e), "只有 1 站时拦住：%s" % e)

        with linedesk.conn() as c:
            c.execute("INSERT INTO ld_stop(line_id,seq,name,km,lat,lon) VALUES(?,?,?,?,?,?)",
                      (lid, 2, "乙", 50, 31.0, 121.0))
        try:
            linedesk.finalize(lid)
            ck(False, "没有走向时应当拒绝确认")
        except ValueError as e:
            ck("线路路线位置" in str(e) or "走向" in str(e), "没有走向时拦住：%s" % e)

        with linedesk.conn() as c:
            c.execute("UPDATE ld_stop SET lat=NULL,lon=NULL WHERE line_id=?", (lid,))
            parts = gzip.compress(json.dumps(
                [[[120.0, 30.0], [120.5, 30.5]]]).encode())
            c.execute("INSERT INTO ld_geom(name,line_id,filename,total_km,n_part,n_pt,"
                      "props,parts_gz,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                      ("测试确认线", lid, "测试走向", 50, 1, 2, "{}", parts, "2026-11-06"))
        try:
            linedesk.finalize(lid)
            ck(False, "停站没定位时应当拒绝确认")
        except ValueError as e:
            ck("定位" in str(e), "停站没定位时拦住：%s" % e)

        with linedesk.conn() as c:
            c.execute("UPDATE ld_stop SET lat=30,lon=120 WHERE line_id=? AND seq=1", (lid,))
            c.execute("UPDATE ld_stop SET lat=31,lon=121 WHERE line_id=? AND seq=2", (lid,))
        r = linedesk.finalize(lid)
        ck(r["status"] == "confirmed", "停站 + 走向 + 定位齐了才允许确认")
        m = r.get("merged") or {}
        ck(m.get("complete") is True and m.get("hasGeom") is True and (m.get("nCoord") or 0) >= 2,
           "merged 回报完成度（%s）" % json.dumps({k: m.get(k) for k in ("complete", "hasGeom", "nCoord")},
                                                ensure_ascii=False))
    finally:
        linedesk.DB_PATH = old_db
        confirmed.invalidate()
        for suf in ("", "-wal", "-shm"):
            try:
                os.remove(tmp + suf)
            except OSError:
                pass


def test_http() -> None:
    """接口联通性：确认通道只给确认线路，老通道照旧（服务没起就跳过）"""
    import urllib.error
    import urllib.parse
    import urllib.request

    def get(path: str):
        with urllib.request.urlopen("http://127.0.0.1:8686" + path, timeout=30) as r:
            return json.loads(r.read().decode())

    print("[6] HTTP 接口（服务未启动则跳过）")
    try:
        st = get("/api/line/confirmed")
    except Exception as e:                                   # noqa: BLE001
        print("  skip  服务没起：%s" % e)
        return
    ck(st["confirmed"] == confirmed.stats()["confirmed"], "确认清单接口与本地模块一致")
    q = urllib.parse.urlencode({"confirmed": 1, "q": "京广"})
    d = get("/api/lines?" + q)
    ck(all(x.get("name") in confirmed.names() for x in (d.get("items") or []))
       and d.get("confirmedOnly") is True,
       "联想搜「京广」只给已确认线路（京广高铁已定稿，不能断言为空）")
    ck(d.get("osm") == [] and d.get("variants") == {}, "联想不返回 OSM / 来源版本")
    q = urllib.parse.urlencode({"confirmed": 1, "q": _neg_sample()})
    try:
        d = get("/api/line?" + q)
        ck(d.get("found") is False and "整理台" in (d.get("reason") or ""),
           "查未确认线给 found=false + 明确原因（不报 404）")
    except urllib.error.HTTPError as e:
        ck(False, "查未确认线不该报 404：%s" % e.code)
    q = urllib.parse.urlencode({"confirmed": 1, "q": "京沪高速线"})
    d = get("/api/line?" + q)
    # 站数跟整理台草稿走（用户在整理台增删过站，别写死）
    n_st = len(d.get("stations") or [])
    ck(d.get("confirmedOnly") is True and n_st >= 2,
       "确认线路详情有站序（%d 站）" % n_st)
    ck(d.get("sources") == [] and d.get("group") == {}, "详情不返回来源版本 / 选线数据")
    q = urllib.parse.urlencode({"confirmed": 1, "q": "京沪高速线"})
    g = get("/api/line/osm?" + q)
    ck(bool(g.get("parts")), "确认线路能拿到真实走向折线")
    alias = [(it["name"], it["geomName"]) for it in confirmed.list_all("")
             if it.get("geomName") and it["geomName"] != it["name"]]
    if alias:
        nm, gn = alias[0]
        q = urllib.parse.urlencode({"confirmed": 1, "q": gn})
        try:
            g = get("/api/line/osm?" + q)
            ck(bool(g.get("parts")),
               "按走向来源名「%s」也能取到折线（%s）" % (gn, nm))
        except urllib.error.HTTPError as e:
            ck(False, "按走向来源名取折线不该报 404：%s" % e.code)
    ck(len(get("/api/lines?q=" + urllib.parse.quote("京广"))["items"]) > 0,
       "老 /api/lines（不带 confirmed）仍返回京广线等其它线路")


def main() -> None:
    test_whitelist()
    test_detail()
    test_geom_source_alias()
    test_old_api()
    test_finalize_guard()
    test_http()
    print("\n绿 %d / 红 %d" % (OK, FAIL))
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
