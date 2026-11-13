# -*- coding: utf-8 -*-
"""
test_ld_merge.py — 整理台「① 线路来源 + ② 站点来源 → ③ 合并保存」的回归

守这些事（2026-11-05 用户口径）：
  1. ① 线路来源只选一条，记成 `src:name`，并决定这条线用哪家的**走向**
  2. ② 站点来源只选一条，搬过来的是**站名 + 站点定位**（定位跟着那一家走）
  3. ③ 合并保存真的把「走向」和「站点 + 定位」合成一条：ld_geom 存原始多段、
     ld_line 记 geom_src=route:xxx、status=confirmed
  4. 定基本线路时不另选走向，就沿用合并保存定下的那一份，主站/地图跟着用

跑法：python scripts/test_ld_merge.py
（写临时 linedesk.db；定基本线路那一步会短暂写 rail.db，测完自己撤销）
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "webapp"))

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


def route_row(name: str, source: str) -> dict:
    c = sqlite3.connect("file:%s?mode=ro" % linedesk.RAIL_DB, uri=True)
    c.row_factory = sqlite3.Row
    try:
        r = c.execute("SELECT name,source,n_stop,n_part,n_pt,total_km"
                      " FROM line_route WHERE name=? AND source=?", (name, source)).fetchone()
        return dict(r) if r else {}
    finally:
        c.close()


def main() -> None:
    tmp = os.path.join(tempfile.gettempdir(), "test_ld_merge_%d.db" % os.getpid())
    for suf in ("", "-wal", "-shm"):
        try:
            os.remove(tmp + suf)
        except OSError:
            pass
    linedesk.DB_PATH = tmp

    NAME = "京沪线"
    hdx = route_row(NAME, "hdx")          # 走向：3214 段 / 1527.9 km
    cn = route_row(NAME, "cnrail")        # 站点：另一家
    ck(bool(hdx) and bool(cn), "rail.db 里有 hdx / cnrail 两份「京沪线」走向数据")
    if not (hdx and cn):
        return

    print("[1] 建草稿线 + 收两条来源（走向一家、站点另一家）")
    d = linedesk.create_line("测试合并京沪", "gs", "回归用")
    lid = d["id"]
    for a in ({"source": "hdx", "name": NAME}, {"source": "cnrail", "name": NAME}):
        linedesk.add_alias(lid, a["source"], a["name"])
    d = linedesk.line_detail(lid)
    stats = {a["source"]: a for a in d["aliases"]}
    ck(stats["hdx"].get("hasGeom") is True, "① 里能看出 hdx 这条带走向")
    ck((stats["cnrail"].get("nStop") or 0) >= 2, "② 里能看出 cnrail 这条带站序")

    print("[2] ① 线路来源只选 hdx（走向）")
    # rename=False：不让资料库中文名称把测试线改名（正式流程里那是「确认线路来源」顺带的）
    r = linedesk.set_line_sources(lid, [{"source": "hdx", "name": NAME}], rename=False)
    line = r["line"]
    ck(line["line_ok"] == 1, "线路来源已确认")
    ck(line["src_line"] == "hdx:" + NAME, "src_line 只有选中那一条：%s" % line["src_line"])
    ck([a["name"] for a in line["aliases"] if a["ok"]] == [NAME], "只有 hdx 这条被标 ok")
    ck(line.get("geom_src") in ("", None), "还没合并时没有定下走向")

    print("[3] ② 站点来源选 cnrail（站点 + 定位一起搬）")
    r = linedesk.confirm_stop_source(lid, "cnrail", NAME, fill=True)
    ck(r["stop_ok"] == 1, "站点来源已确认")
    ck(r["src_stop"] == "cnrail:" + NAME, "src_stop 记的是选中那一家的线名")
    ck(len(r["stops"]) == cn["n_stop"], "站数跟着站点来源：%d / %d"
       % (len(r["stops"]), cn["n_stop"]))
    ck((r["coordDone"] or 0) >= 2, "站点带上了定位（%d 站有坐标）" % r["coordDone"])
    ck(all(s["src"] == "cnrail" for s in r["stops"]), "每站的 src 都记成 cnrail")

    print("[4] ③ 合并保存：走向取 hdx、站点取 cnrail，合成一条")
    r = linedesk.finalize(lid)
    m = r.get("merged") or {}
    ck(r["status"] == "confirmed", "已合并成确认线路")
    ck((m.get("geom") or {}).get("src") == "route:hdx", "走向来自 hdx：%s"
       % ((m.get("geom") or {}).get("src")))
    ck((m.get("geom") or {}).get("nPart") == hdx["n_part"],
       "走向段数和来源包一致：%s / %s" % ((m.get("geom") or {}).get("nPart"), hdx["n_part"]))
    ck(r.get("geom_src") == "route:hdx", "ld_line.geom_src 记下 route:hdx")
    ck(r.get("geom_name") == NAME, "ld_line.geom_name 记下来源里的线名")
    ck(len(r["stops"]) == cn["n_stop"], "站点没有被走向那家覆盖，还是 cnrail 的 %d 站"
       % cn["n_stop"])
    ck((r["coordDone"] or 0) >= 2, "合并后仍带着定位（%d）" % r["coordDone"])

    g = linedesk.desk_geom(r["name"], lid)
    ck(g and g["src"] == "route", "整理台地图知道这是合并来的走向")
    ck(g and len(g["parts"]) == hdx["n_part"], "存的是原始多段（%d 段），不是补断口的主轴"
       % (len(g["parts"]) if g else 0))
    ck(g and (g["props"] or {}).get("merge") == "line+stop", "props 里记了「走向 + 站点」合并")
    ck(g and g["km"] and abs(g["km"] - hdx["total_km"]) < 0.2, "里程取走向那家的 %.1f km"
       % hdx["total_km"])
    gj = linedesk.geom(lid)
    ck(bool((gj.get("geom") or {}).get("parts")), "geom() 直接能拿到合并后的走向")

    print("[5] 定基本线路：不另选走向 → 沿用合并保存的那份")
    import linebase
    before = len(linebase.list_all())
    base_name = ""
    try:
        b = linedesk.publish(lid)
        base_name = b.get("name") or ""
        ck(b.get("geom_src") == "route:hdx", "基本线路 geom_src 自动取 route:hdx：%s"
           % b.get("geom_src"))
        ck(b.get("geom_name") == NAME, "基本线路 geom_name 是来源里的线名")
        ck(len(linebase.list_all()) == before + 1, "基本线路多了一条")
    finally:
        if base_name:
            linebase.unpublish(base_name)
        else:
            linebase.unpublish("测试合并京沪")
    ck(len(linebase.list_all()) == before, "测完撤掉，基本线路数量回到原样")

    print("[6] 缺一步不许合并")
    d2 = linedesk.create_line("测试合并缺步", "", "回归用")
    try:
        linedesk.finalize(d2["id"])
        ck(False, "没确认来源就合并应当报错")
    except ValueError as e:
        ck("都要先分别确认" in str(e), "没确认来源就合并会拦住：%s" % e)
    finally:
        linedesk.delete_line(d2["id"])

    print("\n绿 %d / 红 %d" % (OK, FAIL))
    for suf in ("", "-wal", "-shm"):
        try:
            os.remove(tmp + suf)
        except OSError:
            pass


if __name__ == "__main__":
    main()
