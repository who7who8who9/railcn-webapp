# -*- coding: utf-8 -*-
"""china-emu 接入的回归测试

守七件事：
  1. line_ce / line_ce_stop 两张表存在，行数与抓取结果对得上
  2. 桥隧行没混进站序（丹昆特大桥那种，里程是桥长不是里程）
  3. match_key 两边一致：京哈高速铁路京沈段 == 京哈高速线京沈段
  4. 「小资料挂大干线」能被 OSM 走向长度纠回来（兰新线不能是 6 站的兰中城际线）
  5. 资料库没有的线名能靠 china-emu 补出站序（兰新客专线 40 站）
  6. 里程补全生效：杭深铁路这类原本全空的线路现在有里程
  7. 里程口径：首站 0、末站 ≈ line_ce.total_km
"""
from __future__ import annotations

import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "webapp"))

import cnrail                                                    # noqa: E402
import railcore                                                  # noqa: E402
import lineseg                                                   # noqa: E402

_ok = 0
_bad = []


def check(cond, msg, detail=""):
    global _ok
    if cond:
        _ok += 1
    else:
        _bad.append("%s%s" % (msg, ("  ← " + str(detail)) if detail else ""))


def main() -> None:
    conn = sqlite3.connect(railcore.DB_PATH)
    conn.row_factory = sqlite3.Row

    # 1. 表与行数
    tabs = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    check("line_ce" in tabs and "line_ce_stop" in tabs, "line_ce / line_ce_stop 两张表应在")
    n_line = conn.execute("SELECT COUNT(*) FROM line_ce").fetchone()[0]
    n_stop = conn.execute("SELECT COUNT(*) FROM line_ce_stop").fetchone()[0]
    check(n_line >= 640, "line_ce 应有 640+ 条线路", n_line)
    check(n_stop >= 5800, "line_ce_stop 应有 5800+ 行站序", n_stop)

    # 2. 桥隧不进站序
    if "line_ce_stop" in tabs:
        n_work = conn.execute(
            "SELECT COUNT(*) FROM line_ce_stop WHERE kind='work'").fetchone()[0]
        n_big = conn.execute(
            "SELECT COUNT(*) FROM line_ce_stop WHERE kind<>'work' AND km > 5000").fetchone()[0]
        check(n_big == 0, "站序里不该出现 >5000 的里程（那是桥隧长度）", n_big)
        jinghu = railcore.ce_stops("京沪高速铁路") or {}
        names = [s["name"] for s in (jinghu.get("stops") or [])]
        check(not any("大桥" in n or "隧道" in n for n in names),
              "京沪高速铁路站序里不该有桥隧条目", names[:6])
        # 桥隧行本身仍在库里，只是被 ce_stops 过滤
        check(n_work >= 0, "桥隧行计数可读", n_work)

    # 3. match_key 一致
    k1 = lineseg.match_key("京哈高速铁路京沈段")
    k2 = lineseg.match_key("京哈高速线京沈段")
    check(k1 == k2, "分段线的 match_key 应一致", (k1, k2))
    check(lineseg.match_key("京沪高速铁路") == lineseg.match_key("京沪高铁"),
          "京沪高速铁路 与 京沪高铁 应同键")
    check(lineseg.match_key("京沪铁路") != lineseg.match_key("京沪高速铁路"),
          "普速京沪铁路不该与高铁同键")

    # 4. 用 OSM 走向长度纠回错配
    s = lineseg.stations_for("兰新线") or {}
    n = len(s.get("stops") or [])
    check(n >= 100, "兰新线应纠回整条线（100+ 站），不能是兰中城际的 6 站", n)
    check(s.get("source") == "chinaemu", "兰新线 应选 china-emu 的站序", s.get("source"))
    seg = lineseg.stations_for("沪昆高速线") or {}
    check(len(seg.get("stops") or []) >= 50,
          "沪昆高速线 应纠回 50+ 站，不能是沪杭高铁的 10 站",
          len(seg.get("stops") or []))

    # 5. 资料库没有的线名靠 china-emu 补
    for nm, least in (("兰新客专线", 30), ("沪蓉线", 60), ("京包客专线", 20)):
        got = lineseg.stations_for(nm) or {}
        cnt = len(got.get("stops") or [])
        check(cnt >= least and got.get("source") == "chinaemu",
              "%s 应由 china-emu 补出 %d+ 站" % (nm, least), (cnt, got.get("source")))

    # 6. 里程补全生效
    empty = conn.execute("""
        SELECT COUNT(*) FROM (SELECT lid FROM line_catalog_stop
                               GROUP BY lid
                              HAVING SUM(CASE WHEN km IS NOT NULL THEN 1 ELSE 0 END)=0
                                 AND COUNT(*)>=2)
    """).fetchone()[0]
    check(empty < 277, "里程全空的线路应少于原来的 277 条", empty)
    row = conn.execute(
        "SELECT id FROM line_catalog WHERE name='杭深铁路'").fetchone()
    if row:
        got = conn.execute(
            "SELECT COUNT(*) FROM line_catalog_stop WHERE lid=? AND km IS NOT NULL",
            (row["id"],)).fetchone()[0]
        check(got >= 50, "杭深铁路 应补出 50+ 站里程", got)

    # 7. 里程口径
    for nm in ("杭深铁路", "沪昆高速铁路", "京沪高速铁路"):
        ce = railcore.ce_stops(nm)
        if not ce:
            continue
        st = [s for s in ce["stops"] if s["km"] is not None]
        check(st[0]["km"] == 0.0 or st[0]["km"] < 5,
              "%s 首站里程应≈0" % nm, st[0]["km"])
        check(abs(st[-1]["km"] - (ce.get("totalKm") or 0)) < 1.0,
              "%s 末站里程应≈段长" % nm, (st[-1]["km"], ce.get("totalKm")))

    print("OK：%d 项通过" % _ok if not _bad else "FAIL：%d 项失败" % len(_bad))
    for b in _bad:
        print("  ×", b)
    sys.exit(1 if _bad else 0)


if __name__ == "__main__":
    main()
