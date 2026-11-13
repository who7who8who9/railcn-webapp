# -*- coding: utf-8 -*-
"""回归：「车站/线路」下方常驻整理台全部确认线路（2026-11-10 用户口径）

需求：查询线路页面的**下面**列出所有整理台确认线路，按确认先后倒序（最新确认
在最上面），然后输入线名就在这份清单里搜索。

守这些事：
  - 确认时间取自 `ld_log` 的 finalize / publish（最后一次），不是 `ld_line.updated_at`
    （改站序 / 补坐标都会动 updated_at，不能当确认先后）；没有日志的老数据回退。
  - 同一秒确认的多条线按日志 id、再按 ld_line.id 定先后；日志 target 是线名或
    ld_line.id 字符串都要认。
  - `confirmed.recent()` 全量不截断，倒序，带 confirmedAt / confirmSeq / covers。
  - `ld_log` 写进 `_fp()` 指纹：整理台 ③ 刚确认完，新线不用等缓存过期就排到最上。
  - HTTP `GET /api/line/confirmed?full=1` 带 items；不带 full 的老口径不变。
  - 前端：常驻容器 / 本地筛选 / 不再弹联想下拉 / 线路模式才显示。

跑法：python scripts/test_confirmed_list.py
只读正式库；排序 / 回退 / 缓存失效的构造场景用临时 linedesk.db，不碰正式整理库。
"""
from __future__ import annotations

import gzip
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "webapp"))

import confirmed                                             # noqa: E402
import linedesk                                              # noqa: E402

OK = FAIL = 0


def ck(cond, msg):
    global OK, FAIL
    if cond:
        OK += 1
        print("  OK   " + msg)
    else:
        FAIL += 1
        print("  FAIL " + msg)


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
def test_real_db():
    print("[1] 正式库：全量清单、字段、倒序")
    st = confirmed.stats()
    rows = confirmed.recent()
    ck(len(rows) == st["confirmed"],
       "清单条数 == 确认线路数（%d）" % st["confirmed"])
    ck(all(x["name"] in set(st["lines"]) for x in rows),
       "清单里只有白名单线路，没有草稿 / 来源版本")
    ck(all(x["confirmed"] is True and x["base"] is True for x in rows),
       "每条都标 confirmed / base")
    ck(all(x["confirmedAt"] and x["ldId"] and x["n"] >= 2 for x in rows),
       "每条都有确认时间 / ldId / ≥2 站")
    ck(all("confirmedAt" in x and "confirmSeq" in x and "covers" in x
           and "nGeomPt" in x and "fromSta" in x and "toSta" in x for x in rows),
       "字段齐：confirmedAt / confirmSeq / covers / nGeomPt / 起讫")
    keys = [(x["confirmedAt"], x["confirmSeq"], x["ldId"]) for x in rows]
    ck(keys == sorted(keys, reverse=True),
       "★ 按确认先后倒序（最新在最上）：%s → %s"
       % (rows[0]["name"] if rows else "-", rows[-1]["name"] if rows else "-"))
    if len(rows) > 1:
        ck(rows[0]["confirmedAt"] >= rows[-1]["confirmedAt"],
           "第一条（%s %s）比最后一条（%s %s）新"
           % (rows[0]["name"], rows[0]["confirmedAt"],
              rows[-1]["name"], rows[-1]["confirmedAt"]))
    ck(all(x["confirmFrom"] in ("log", "line") for x in rows),
       "confirmFrom 标明时间来自日志还是回退")
    ck(len({x["name"] for x in rows}) == len(rows), "同一条线不重复")


# ---------------------------------------------------------------------------
def _seed(c, name, updated, seq_km=50):
    lid = c.execute(
        "INSERT INTO ld_line(name,family,kind,status,line_ok,stop_ok,total_km,"
        " from_sta,to_sta,created_at,updated_at,geom_src,geom_name)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (name, name, "gs", "confirmed", 1, 1, seq_km, "甲站", "乙站",
         "2026-10-01 00:00:00", updated, "geojson", name)).lastrowid
    for i, (nm, km) in enumerate((("甲站", 0.0), ("乙站", float(seq_km)))):
        c.execute("INSERT INTO ld_stop(line_id,seq,name,km,lat,lon,src)"
                  " VALUES(?,?,?,?,?,?,?)",
                  (lid, i + 1, nm, km, 30.0 + i, 120.0 + i, "manual"))
    parts = gzip.compress(json.dumps(
        [[[120.0, 30.0], [121.0, 31.0]]]).encode())
    c.execute("INSERT INTO ld_geom(name,line_id,filename,total_km,n_part,n_pt,"
              " props,parts_gz,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
              (name, lid, name, seq_km, 1, 2, "{}", parts, updated))
    return lid


def _log(c, ts, op, target):
    return c.execute("INSERT INTO ld_log(ts,op,target,detail)"
                     " VALUES(?,?,?,?)", (ts, op, str(target), "回归")).lastrowid


def test_tmp_order():
    print("[2] 临时库：确认时间取日志、回退、同秒定序、缓存失效")
    old_db = linedesk.DB_PATH
    tmpdir = tempfile.TemporaryDirectory(prefix="railcn-cflist-",
                                         ignore_cleanup_errors=True)
    linedesk.DB_PATH = os.path.join(tmpdir.name, "linedesk.db")
    confirmed.invalidate()
    try:
        linedesk.init()
        with linedesk.conn() as c:
            # 甲：日志比 updated_at 早 —— 用来验证「别拿 updated_at 当确认时间」
            l1 = _seed(c, "回归甲线", "2026-11-02 08:00:00")
            _log(c, "2026-11-01 10:00:00", "finalize", "回归甲线")
            # 乙：publish 也算确认动作
            _seed(c, "回归乙线", "2026-11-04 08:00:00")
            _log(c, "2026-11-03 12:00:00", "publish", "回归乙线")
            # 丙：最新确认
            l3 = _seed(c, "回归丙线", "2026-11-10 09:00:00")
            _log(c, "2026-11-10 09:00:00", "finalize", "回归丙线")
            _log(c, "2026-11-10 09:00:00", "publish", "回归丙线")
            # 丁：完全没有日志 → 回退 updated_at
            _seed(c, "回归丁线", "2026-11-08 08:00:00")
            # 戊 / 己：同一秒确认，靠日志 id 定先后；戊的 target 用 ld_line.id
            l5 = _seed(c, "回归戊线", "2026-11-05 09:00:00")
            _log(c, "2026-11-05 10:00:00", "finalize", l5)
            _seed(c, "回归己线", "2026-11-05 09:00:00")
            _log(c, "2026-11-05 10:00:00", "finalize", "回归己线")
            # 别名：ok=1 才进 covers
            c.execute("INSERT INTO ld_alias(line_id,source,name,ok) VALUES(?,?,?,1)",
                      (l3, "回归", "丙线别名A"))
            c.execute("INSERT INTO ld_alias(line_id,source,name,ok) VALUES(?,?,?,1)",
                      (l3, "回归", "丙线别名B"))
            c.execute("INSERT INTO ld_alias(line_id,source,name,ok) VALUES(?,?,?,0)",
                      (l3, "回归", "丙线别名_未确认"))
        rows = confirmed.recent()
        got = [x["name"] for x in rows]
        want = ["回归丙线", "回归丁线", "回归己线", "回归戊线",
                "回归乙线", "回归甲线"]
        ck(got == want, "★ 全量 6 条且顺序正确：%s" % " > ".join(got))
        by = {x["name"]: x for x in rows}
        ck(by["回归甲线"]["confirmedAt"] == "2026-11-01 10:00:00"
           and by["回归甲线"]["confirmFrom"] == "log",
           "日志时间优先于 updated_at（甲线 11-01 而非 11-02）")
        ck(by["回归丁线"]["confirmedAt"] == "2026-11-08 08:00:00"
           and by["回归丁线"]["confirmFrom"] == "line"
           and by["回归丁线"]["confirmSeq"] == 0,
           "无日志的老数据回退 updated_at 并标 line")
        ck(by["回归戊线"]["confirmedAt"] == "2026-11-05 10:00:00",
           "日志 target 是 ld_line.id 字符串也能取到时间")
        ck(by["回归己线"]["confirmSeq"] > by["回归戊线"]["confirmSeq"],
           "同一秒确认靠日志 id 定先后（己 > 戊）")
        ck(by["回归丙线"]["covers"] == ["丙线别名A", "丙线别名B"],
           "covers 只收 ok=1 的别名且有序：%s" % by["回归丙线"]["covers"])
        ck(all(x["n"] == 2 and x["nCoord"] == 2 and x["nGeomPt"] == 2
               for x in rows), "站数 / 定位 / 走向点数跟整理台口径一致")
        ck(all(x["totalKm"] == 50 for x in rows), "总里程按末站累计里程给")

        # 新确认一条 → 指纹里含 ld_log，不用等缓存过期也不用手动 invalidate
        with linedesk.conn() as c:
            _log(c, "2026-11-11 12:00:00", "finalize", "回归甲线")
        again = confirmed.recent()
        ck(again[0]["name"] == "回归甲线"
           and again[0]["confirmedAt"] == "2026-11-11 12:00:00",
           "★ 整理台刚确认完，新时间立刻排到最上（缓存指纹含 ld_log）")
        ck(len(again) == 6, "刷新后仍是全量 6 条，没被缓存截断")
        ck(by["回归丙线"]["covers"] == ["丙线别名A", "丙线别名B"],
           "别名在刷新后仍跟着线走")
    finally:
        linedesk.DB_PATH = old_db
        confirmed.invalidate()
        tmpdir.cleanup()


# ---------------------------------------------------------------------------
def test_http():
    print("[3] HTTP：/api/line/confirmed?full=1（服务没起则跳过）")
    import urllib.request
    import urllib.parse

    def get(path):
        with urllib.request.urlopen("http://127.0.0.1:8686" + path,
                                    timeout=30) as r:
            return json.loads(r.read().decode())

    try:
        plain = get("/api/line/confirmed")
    except Exception as e:                                   # noqa: BLE001
        print("  skip  服务没起：%s" % e)
        return
    ck(plain["confirmed"] == confirmed.stats()["confirmed"],
       "不带 full 的老口径仍是纯统计数")
    ck("items" not in plain, "不带 full 不返回 items（老调用零影响）")
    d = get("/api/line/confirmed?" + urllib.parse.urlencode({"full": 1}))
    items = d.get("items") or []
    ck(d.get("ok") is True and len(items) == d["confirmed"],
       "full=1 返回全量 %d 条" % len(items))
    ck([x["name"] for x in items] == [x["name"] for x in confirmed.recent()],
       "HTTP 顺序与模块一致（最新确认在最上）")
    ck(all(x.get("confirmedAt") and "covers" in x and "confirmSeq" in x
           for x in items), "HTTP 每条都带确认时间 / 序号 / 别名")
    if items:
        ck(items[0]["confirmedAt"] >= items[-1]["confirmedAt"],
           "HTTP 第一条 %s 不早于最后一条 %s"
           % (items[0]["name"], items[-1]["name"]))


# ---------------------------------------------------------------------------
def test_frontend():
    print("[4] 前端：常驻清单 / 本地筛选 / 不再弹下拉")
    html = read("webapp/static/index.html")
    css = read("webapp/static/style.css")
    js = read("webapp/static/app.js")
    srv = read("webapp/server.py")
    ck('id="netConfirmedList"' in html, "index.html 有常驻清单容器")
    ck(html.index('id="netConfirmedList"') < html.index('id="netList"'),
       "清单在 #netList / 详情区之前，打开详情不会被覆盖")
    ck(".cf-list.fold" in css and ".cf-toggle" in css
       and ".cf-row .at" in css, "CSS 有清单行 / 折叠按钮 / 时间列")
    for needle, desc in (
        ("function netRenderConfirmedList(", "渲染函数"),
        ("function cfHit(", "本地筛选（线名 + 别名 + 走向来源名）"),
        ("const cfShortTime =", "确认时间显示"),
        ("'/api/line/confirmed', { full: 1 }", "拉全量清单"),
        ("all.indexOf(it) + 1", "用全量名次编号，筛选后不重排"),
        ("NET.cfOpen", "清单可收起 / 展开"),
        ("data-cf=", "每行可点开线路详情"),
        ("if (netKind === 'line') {", "线路模式分支"),
        ("netRenderConfirmedList();", "输入 / 切换 / 回车都刷新清单"),
    ):
        ck(needle in js, "app.js：%s" % desc)
    ck("if (netKind === 'line') {\n    box.classList.add('hidden');\n"
       "    await netRenderConfirmedList();\n    return;\n  }" in js,
       "suggestNet 线路模式直接筛清单、不再弹联想下拉")
    ck("netKind !== 'line'" in js and "box.classList.add('hidden'); return;"
       in js, "车站模式隐藏常驻清单")
    ck("cf-list hidden" in html, "容器默认隐藏，切到线路模式才出现")
    ck('st["items"] = confirmed.recent()' in srv
       and 'one("full")' in srv, "server.py 的 full=1 出口")
    ck("SELECT COUNT(*),MAX(id) FROM ld_log" in read("webapp/confirmed.py"),
       "指纹纳入 ld_log，新确认立刻生效")


def main():
    print("=== 车站/线路：下方全量确认线路（按确认先后倒序） ===")
    test_real_db()
    test_tmp_order()
    test_http()
    test_frontend()
    print("\n绿 %d / 红 %d" % (OK, FAIL))
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
