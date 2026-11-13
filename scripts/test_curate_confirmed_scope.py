# -*- coding: utf-8 -*-
"""
回归：数据校对的线路口径 —— 只认整理台「全部确认」的线路（2026-11-11 用户定）

以前校对页的线路来自线路库几千条来源版本（京广线（OSM）、沪昆线（底表）、
沪昆线（自建）…），自动识别也按线路库推。用户要求：**数据校对里出现的线路
一律是他整理台确认过的那些**，其它线路不要出现。

现在（`webapp/confvia.py`）：
  · 线路下拉 `/api/curate/lineNames` 只列 `confirmed.names()`；
  · 自动识别按**确认站序**（linedesk.ld_stop）推，认不出就是缺口；
  · 缺口候选 `/api/curate/cands`、`detail.gapCands` 同样只给确认线路；
  · 站序 `/api/line/stops?confirmed=1` 给整理台那份（别拿线路库同名版本）。

跑法：python scripts/test_curate_confirmed_scope.py   （先起 server --port 8686）
只读，不写库。
"""
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8686"
ok = fail = 0
bad: list = []


def check(label: str, cond: bool, val="") -> None:
    global ok, fail
    if cond:
        ok += 1
        print("  ok   %s%s" % (label, ("  " + str(val))[:70] if val != "" else ""))
    else:
        fail += 1
        bad.append(label)
        print("  失败 %s%s" % (label, ("  " + str(val))[:70] if val != "" else ""))


def get(path: str, **kw) -> dict:
    qs = ("?" + urllib.parse.urlencode(kw)) if kw else ""
    with urllib.request.urlopen(BASE + path + qs, timeout=180) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    sys.path.insert(0, "webapp")
    import confirmed
    import confvia

    cf = confirmed.names()

    # ---- 1) 确认线路索引：和 confirmed 实时一致 ----
    print("== 确认线路索引")
    idx = confvia.index()
    check("索引里的线 == confirmed.names()", set(idx) == cf,
          "%d 条 / confirmed %d 条" % (len(idx), len(cf)))
    check("每条都带站序（≥2 站）", all(v["n"] >= 2 for v in idx.values()),
          [k for k, v in idx.items() if v["n"] < 2][:3])
    # 总里程允许是 0（石获线 / 西长铁路 整理台还没填里程），但别是 None
    zero = [k for k, v in idx.items() if not v["totalKm"]]
    check("每条都有里程字段（允许 0）", all(v["totalKm"] is not None for v in idx.values()),
          "未填里程：%s" % zero[:4])

    # ---- 2) 名字归一：来源版本 → 正名；没确认的留空 ----
    print("== 名字归一")
    check("京广线（OSM）→ 京广线", confvia.normalize("京广线（OSM）") == "京广线",
          confvia.normalize("京广线（OSM）"))
    check("沪昆线（底表）→ 沪昆线", confvia.normalize("沪昆线（底表）") == "沪昆线",
          confvia.normalize("沪昆线（底表）"))
    check("京广线 原样", confvia.normalize("京广线") == "京广线")
    check("京哈线（没确认）→ 空", confvia.normalize("京哈线") == "",
          confvia.normalize("京哈线"))

    # ---- 3) 自动识别：确认站序上连点 3 站必认成这条线 ----
    print("== 自动识别")
    line = "京广线"
    st = get("/api/line/stops", q=line, confirmed=1)["items"]
    check("%s 的确认站序拿得到" % line, len(st) > 10, len(st))
    mid = len(st) // 2
    three = st[mid:mid + 3]
    r = confvia.infer([{"name": n} for n in three])
    check("认成 1 段", len(r["segs"]) == 1, r["segs"])
    check("段名是 %s" % line, r["segs"] and r["segs"][0]["line"] == line, r["segs"])
    check("没有缺口", not r["unknown"], r["unknown"])
    check("覆盖 100%", r["coverage"] == 100.0, r["coverage"])
    check("段区间按站序首尾相接",
          r["segs"] and r["segs"][0]["from"] == three[0] and r["segs"][0]["to"] == three[2],
          r["segs"][0] if r["segs"] else None)

    # 认不出的线（京哈线没确认）→ 一个段都不给，全是缺口
    r2 = confvia.infer([{"name": "沈阳北"}, {"name": "长春"}, {"name": "哈尔滨西"}])
    check("没确认的线不认（段 0）", not r2["segs"], r2["segs"])
    check("没确认的线全算缺口", len(r2["unknown"]) == 1, r2["unknown"])

    # ---- 4) 下拉 / 候选 / 站序 三个出口 ----
    print("== 接口口径")
    ln = get("/api/curate/lineNames")
    names = [x["name"] for x in ln["items"]]
    check("lineNames 标 scope=confirmed", ln.get("scope") == "confirmed", ln.get("scope"))
    check("lineNames 全是确认线路", set(names) == cf,
          "%d 条 / 差集 %s" % (len(names), sorted(set(names) ^ cf)[:4]))
    check("lineNames 不带（来源）后缀", not any("（" in n for n in names),
          [n for n in names if "（" in n][:3])
    check("lineNames 数量不再是几千条", len(names) < 200, len(names))

    c = get("/api/curate/cands", **{"from": three[0], "to": three[2]})
    check("cands 两站都在的排第一", c["items"] and c["items"][0]["line"] == line,
          [x["line"] for x in c["items"][:3]])
    check("cands 全是确认线路", all(x["line"] in cf for x in c["items"]),
          [x["line"] for x in c["items"] if x["line"] not in cf][:3])

    s1 = get("/api/line/stops", q=line, confirmed=1)
    check("站序走 confirmed 分支", s1.get("from") == "confirmed", s1.get("from"))
    check("站序站数 == 下拉站数",
          s1["count"] == next(x["n"] for x in ln["items"] if x["name"] == line),
          (s1["count"], next(x["n"] for x in ln["items"] if x["name"] == line)))
    s2 = get("/api/line/stops", q="京哈线", confirmed=1)
    check("没确认的线在确认口径下不给站序",
          not s2.get("found") and not s2.get("items"), s2.get("count"))

    # ---- 5) 校对清单：auto 行里不许出现来源版本名 ----
    print("== 校对清单")
    lst = get("/api/curate/list", filter="all", q="京广线", limit=200)
    vers = [x["code"] for x in lst["items"]
            if "（" in (x.get("via") or "")]
    check("清单里的经由名不带（来源）后缀", not vers, vers[:4])
    check("清单条目都是确认线路里的名字",
          all(all(n.strip() in cf for n in (x.get("via") or "").split(" / ") if n.strip())
              for x in lst["items"]),
          [x["code"] for x in lst["items"]
           if any(n.strip() and n.strip() not in cf
                  for n in (x.get("via") or "").split(" / "))][:4])

    # ---- 6) 编辑器详情：自动段与缺口候选都只给确认线路 ----
    print("== 编辑器详情")
    pick = next((x["code"] for x in lst["items"] if (x.get("via") or "").strip()), "")
    if not pick:
        check("挑到一趟有经由的车", False, "清单里没有带经由的车次")
    else:
        d = get("/api/curate/detail", code=pick)
        segs = d["auto"]["viaSegs"]
        check("%s 自动段全是确认线路" % pick, all(s["line"] in cf for s in segs),
              [s["line"] for s in segs if s["line"] not in cf][:3])
        check("%s 缺口候选全是确认线路" % pick,
              all(x["line"] in cf for g in d.get("gapCands") or [] for x in g.get("lines") or []),
              [x["line"] for g in d.get("gapCands") or [] for x in g.get("lines") or []
               if x["line"] not in cf][:3])
        n = len(d.get("stations") or [])
        pairs = sum(1 for s in segs) + sum(1 for g in d["auto"]["viaUnknown"])
        check("%s 段+缺口不超过相邻站对数" % pick, pairs <= max(n - 1, 0), (pairs, n - 1))

    # ---- 7) 静态护栏：别又走回线路库口径 ----
    print("== 静态护栏")
    import io
    src = io.open("webapp/curate.py", encoding="utf-8").read()
    check("curate 不再用 railcore.cand_lines", "railcore.cand_lines" not in src)
    check("curate 用 confvia.infer", "confvia.infer(" in src)
    srv = io.open("webapp/server.py", encoding="utf-8").read()
    check("lineNames 用 confirmed.recent()",
          'if path == "/api/curate/lineNames"' in srv and "confirmed.recent()" in srv)
    seg = srv[srv.index('if path == "/api/curate/lineNames"'):]
    seg = seg[:seg.index("if path ==", 10)]
    check("lineNames handler 不再碰 linebase / linegroup / lineseg",
          all(k not in seg for k in ("linebase", "linegroup", "lineseg")))

    print("\n通过 %d / 失败 %d" % (ok, fail))
    if fail:
        print("失败项：", "；".join(bad))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
