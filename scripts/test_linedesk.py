# -*- coding: utf-8 -*-
"""
test_linedesk.py — 线路整理台（独立库 linedesk.db）的回归

守这些事：
  1. 五套来源收得全，初稿能建出来
  2. 同一条线在四个来源里的不同写法归到同一条线上（京沪高铁 / 京沪高速线 / 京沪高速铁路）
  3. 分段建成的线挂在整条线下面（京哈高速线京沈段 / 沈哈段 → 京哈高速线）
  4. 改规范名、加删别名、改站序都只动 linedesk.db，rail.db 一个字节不改
  5. 从 china-emu 导站序时 K 里程要换成段内里程（首站归 0）
  6. 合并：别名与站序转到目标线，被并的线消失
  7. 父子关系不能成环
  8. 导出结构完整

跑法：python scripts/test_linedesk.py
"""
from __future__ import annotations

import json
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


def main() -> None:
    # 全部写临时库，别动用户正在整理的 linedesk.db
    tmp = os.path.join(tempfile.gettempdir(), "test_linedesk_%d.db" % os.getpid())
    for suf in ("", "-wal", "-shm"):
        try:
            os.remove(tmp + suf)
        except OSError:
            pass
    linedesk.DB_PATH = tmp

    rail_before = os.path.getmtime(os.path.join(ROOT, "webapp", "data", "rail.db"))

    print("[1] 建初稿")
    st = linedesk.build_seed(force=True)
    s = linedesk.stats()
    ck(st["sources"].get("lib") == 417, "底表 417 条收进来")
    ck(st["sources"].get("huirui") == 844, "资料库 844 条收进来")
    ck(st["sources"].get("ce") == 660, "china-emu 660 条收进来")
    ck(st["sources"].get("osm") > 2500, "OSM 线名 > 2500")
    ck(s["lines"] > 3000, "规范线 > 3000 条（实际 %d）" % s["lines"])
    ck(s["withStops"] > 1000, "有站序的线 > 1000（实际 %d）" % s["withStops"])

    print("[2] 同一线路的多种写法归到一条")
    d = linedesk.find_line("京沪高速线")
    ck(bool(d), "找得到京沪高速线")
    names = {a["name"] for a in d["aliases"]}
    srcs = {a["source"] for a in d["aliases"]}
    ck("京沪高铁" in names, "收下了资料库的「京沪高铁」")
    ck("京沪高速铁路" in names, "收下了 china-emu 的「京沪高速铁路」")
    ck({"lib", "huirui", "ce", "osm"} <= srcs, "四个来源都归到了这一条")

    print("[3] 分段挂到整线")
    kh = linedesk.find_line("京哈高速线")
    ck(bool(kh), "找得到京哈高速线")
    kids = {c["name"] for c in kh["children"]}
    ck(any("京沈" in k for k in kids), "挂着京沈段")
    ck(any("沈哈" in k for k in kids), "挂着沈哈段")
    ck(len(kids) == 2, "同一段的两种写法只挂一次（实际 %d）" % len(kids))

    print("[4] 改规范名 / 别名 / 站序")
    d = linedesk.save_line(d["id"], {"name": "京沪高速线①", "note": "测试改名"})
    ck(d["name"] == "京沪高速线①", "规范名改掉了")
    ck(d["family"] == "京沪|gs", "认不出后缀的名字保留原族：%s" % d["family"])
    d = linedesk.save_line(d["id"], {"name": "京沪高速线"})       # 改回来，后面还要用
    n0 = len(d["aliases"])
    d = linedesk.add_alias(d["id"], "custom", "测试别名线")
    ck(len(d["aliases"]) == n0 + 1, "别名加进去了")
    aid = [a["id"] for a in d["aliases"] if a["name"] == "测试别名线"][0]
    d = linedesk.del_alias(aid)
    ck(len(d["aliases"]) == n0, "别名删掉了")

    print("[5] 从 china-emu 导站序：K 里程换成段内里程")
    # 陇海铁路西宝段在 china-emu 里是 K1073→K1256，直接搬会算成 1256 km
    t = linedesk.find_line("陇海铁路西宝段")
    ck(bool(t), "初稿里就有陇海铁路西宝段")
    try:
        other = linedesk.find_line("京沪高速线")
        linedesk.add_alias(other["id"], "ce", "陇海铁路西宝段")
        ck(False, "同一个来源线名不该被两条线抢")
    except ValueError:
        ck(True, "同一个来源线名不能被两条线抢")
    r = linedesk.fill_stops(t["id"], "ce", "陇海铁路西宝段")
    ks = [x["km"] for x in r["stops"] if x["km"] is not None]
    ck(len(r["stops"]) > 10, "导进来 %d 站" % len(r["stops"]))
    ck(ks and ks[0] == 0, "首站里程归 0（原 K 里程已平移），首站=%s" % (ks[0] if ks else None))
    ck(ks == sorted(ks), "里程单调不减")
    ck(ks and ks[-1] < 250, "末站是段内里程而不是 K 里程（%s）" % (ks[-1] if ks else None))

    print("[6] 合并两条线")
    a = linedesk.find_line("京沪高速线")
    b = linedesk.find_line("京沪高速铁路二线")
    nb = len(b["aliases"])
    before = linedesk.stats()["lines"]
    m = linedesk.merge_lines([a["id"], b["id"]], into=b["id"], name="京沪二线合并测试")
    ck(m["name"] == "京沪二线合并测试", "合并后的规范名生效")
    ck(len(m["aliases"]) >= nb + 1, "两边的别名都在")
    ck(len(m["stops"]) > 0, "站序留下来了")
    ck(linedesk.stats()["lines"] == before - 1, "少了一条线")
    ck(linedesk.find_line("京沪高速线") is not None, "旧写法还能按别名找回来")

    print("[7] 父子关系不能成环")
    p = linedesk.find_line("京哈高速线")
    c = linedesk.find_line("京哈高速线京沈段")
    linedesk.set_relation(p["id"], c["id"], "seg", "测试")
    try:
        linedesk.set_relation(c["id"], p["id"], "seg", "反着挂")
        ck(False, "反向挂父子应该被拒绝")
    except ValueError as exc:
        ck("环" in str(exc), "反向挂被挡住：%s" % exc)
    linedesk.del_relation(p["id"], c["id"])

    print("[8] 导出")
    ex = linedesk.export_map()
    ck(ex["counts"]["lines"] == linedesk.stats()["lines"], "导出条数对得上")
    ck(any(x["alias"].get("ce") for x in ex["lines"]), "导出里带各来源写法")
    ck(all("stops" in x for x in ex["lines"]), "导出里带站序")
    ck(isinstance(ex["relations"], list), "导出里带父子关系")

    print("[9] rail.db 没被动过")
    rail_after = os.path.getmtime(os.path.join(ROOT, "webapp", "data", "rail.db"))
    ck(rail_before == rail_after, "rail.db 修改时间没变")

    # 收尾
    linedesk.DB_PATH = tmp
    try:
        with sqlite3.connect(tmp) as cx:
            cx.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        pass
    for suf in ("", "-wal", "-shm"):
        try:
            os.remove(tmp + suf)
        except OSError:
            pass

    print("\n%d 项通过，%d 项失败" % (OK, FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
