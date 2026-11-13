# -*- coding: utf-8 -*-
"""回归：走向编辑器里「整理台已确认线路」（2026-11-10 用户口径）

需求：OSM 走向编辑器「选择线路」里把整理台已确认线路放进来、可以修改，
保存后**仍然是整理台已确认线路**，这些线在列表里**线名标红**。

守这些事：
  - 只认 `confirmed.get()` 的完成确认白名单（status + line_ok/stop_ok + ≥2 站
    + 真有走向），草稿 / 来源版本一律不标红；确认线路排在最前。
  - 确认线路的 `loadName` 一律是**规范名**：`load_geom()` 走 `confirmed.geom()`
    专属通道（有 ld_geom 用连续主轴，③ 的 osm 分支按 geom_name 解析 OSM），
    草稿也按规范名读写，不再是「存济南线、载入京沪四线」。
  - 保存 apply 时写回 `linedesk.db / ld_geom`，按 `line_id` 绑定，让
    `merged_geom()` / `confirmed.geom()` 真能命中；原本没有 ld_geom 的 osm
    借走向线要 INSERT 一份，等于把走向固化到自己名下。
  - **绝不改 `ld_line`**：status / line_ok / stop_ok / geom_src / geom_name /
    updated_at / 站序全不动，所以保存完仍是确认线路、确认先后不变。
  - 非确认线路保持旧的双轨语义（草稿 line_geom_edit + apply 写 line_geom）。
  - 前端红名 / 标签 / 提示 / 保存 target / 确认文案，服务端参数透传。

跑法：python scripts/test_osmedit_confirmed.py
正式库只读；写库验证用**临时 linedesk.db + 临时 rail.db**，不碰正式数据。
"""
from __future__ import annotations

import gzip
import json
import os
import sqlite3
import sys
import tempfile
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "webapp"))

import confirmed                                             # noqa: E402
import linedesk                                              # noqa: E402
import osmedit                                               # noqa: E402

OK = FAIL = 0
LD_COLS = ("status", "line_ok", "stop_ok", "geom_src", "geom_name",
           "updated_at", "created_at", "total_km", "from_sta", "to_sta",
           "note", "src_line", "src_stop", "stops_edited")

CHAIN = [[116.0, 39.0], [116.2, 39.1], [116.4, 39.05], [116.6, 39.2]]
CHAIN2 = [[117.0, 36.0], [117.3, 36.2], [117.6, 36.1], [117.9, 36.4],
          [118.2, 36.3]]


def ck(cond, msg, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print("  OK   " + msg)
    else:
        FAIL += 1
        print("  FAIL " + msg + ("  " + str(extra) if extra else ""))


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _gz(obj):
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode(), 6)


def _line_row(db, lid):
    c = sqlite3.connect(db)
    row = c.execute("SELECT %s FROM ld_line WHERE id=?" % ",".join(LD_COLS),
                    (int(lid),)).fetchone()
    c.close()
    return row


# ---------------------------------------------------------------------------
def test_real_index():
    print("[1] 正式库：全量确认线路进列表、标红、排最前")
    st = confirmed.stats()
    rec = confirmed.recent()
    d = osmedit.list_lines("", 100, 0)
    n = st["confirmed"]
    ck(d.get("confirmed") == n,
       "接口返回整理台已确认条数（%d）" % n, d.get("confirmed"))
    ck(d["total"] > 4000 and d["count"] == d["total"],
       "全量有走向线路仍在（%d 条），没被确认线路挤掉" % d["total"])
    head = d["items"][:n]
    ck(len(head) == n and all(x.get("confirmed") for x in head),
       "★ 前 %d 条全是整理台已确认线路（红名）" % n)
    ck({x["name"] for x in head} == {r["name"] for r in rec},
       "前 N 条 = confirmed.recent() 全量，不多不少")
    ck(all(x["loadName"] == x["name"] for x in head),
       "确认线路 loadName 一律用规范名（按规范名载入/保存）")
    ck(all(x.get("ldId") and "confirmed" in x["src"] for x in head),
       "每条都带 ldId 与「已确认」来源标签")
    ck(all(x.get("confirmedAt") for x in head), "每条都带确认时间")
    ck(not any(x.get("confirmed") for x in d["items"][n:]),
       "非确认线路不打标、不冒充红名")
    jn = osmedit.list_lines("济南线", 20)
    cf = next((x for x in jn["items"] if x["name"] == "济南线"), None)
    ck(bool(cf and cf["confirmed"] and cf["ldId"]
            and cf["loadName"] == "济南线"
            and cf["geomName"] == "京沪四线"
            and not cf["alias"] and "alias" not in cf["src"]),
       "★ 济南线（osm 借走向）不再是 alias，作为确认线路按规范名编辑", str(cf))
    alt = osmedit.list_lines("京沪四线", 30)
    ck(any(x["name"] == "济南线" for x in alt["items"]),
       "按走向来源名也能搜到借走向的确认线路")
    a0 = osmedit.list_lines("", 100, 0)
    a1 = osmedit.list_lines("", 100, a0["total"] - 100)
    ck(not ({x["name"] for x in a0["items"]} & {x["name"] for x in a1["items"]}),
       "确认线路置顶后分页仍不重复")


def test_real_load():
    print("[2] 正式库：每条确认线路都能载入走向、带确认身份")
    rec = confirmed.recent()
    got = {r["name"]: osmedit.load_geom(r["name"], 200) for r in rec}
    ck(all(g and g.get("parts") and g["parts"][0] for g in got.values()),
       "★ %d 条确认线路全部能载入可编辑折线" % len(got))
    ck(all(g.get("confirmed") and g.get("keepsConfirmed") and g.get("ldId")
           and g.get("loadName") == nm for nm, g in got.items()),
       "每条都标 confirmed / keepsConfirmed / ldId / 规范名 loadName")
    ck(all(g["parts"][0][0] and abs(g["parts"][0][0][0]) <= 180
           and abs(g["parts"][0][0][1]) <= 90 for g in got.values()),
       "点序是 [lon,lat]（没有经纬度反序）")
    ck(all(g and g.get("source") in ("confirmed", "edit") for g in got.values()),
       "★ 所有确认线路一律走主站同一份几何（或已有草稿），不再读失真主轴",
       str({nm: g.get("source") for nm, g in got.items()}))
    ck(all(g and g.get("nPart", 0) >= 1 for g in got.values()),
       "载入返回段数 nPart")
    same = []
    for nm, g in got.items():
        if not g or g.get("source") == "edit":
            continue                       # 草稿是用户自己画的，另算
        main = confirmed.geom(nm, with_parts=True) or {}
        same.append((nm, len(main.get("parts") or []), g.get("nPart")))
    ck(bool(same) and all(a == b for _, a, b in same),
       "★ 载入段数与主站 /api/line/osm 逐条一致：%s" % same[:4])


# ---------------------------------------------------------------------------
def _seed(c, name, with_geom, geom_src="geojson", geom_name=None,
          status="confirmed"):
    lid = c.execute(
        "INSERT INTO ld_line(name,family,kind,status,line_ok,stop_ok,total_km,"
        " from_sta,to_sta,created_at,updated_at,geom_src,geom_name)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (name, name, "gs", status, 1 if status == "confirmed" else 0,
         1 if status == "confirmed" else 0, 60.0, "甲站", "乙站",
         "2026-11-01 00:00:00", "2026-11-09 10:00:00",
         geom_src, geom_name or name)).lastrowid
    for i, (nm, km) in enumerate((("甲站", 0.0), ("乙站", 60.0))):
        c.execute("INSERT INTO ld_stop(line_id,seq,name,km,lat,lon,src)"
                  " VALUES(?,?,?,?,?,?,?)",
                  (lid, i + 1, nm, km, 36.0 + i, 116.0 + i, "manual"))
    if with_geom:
        c.execute(
            "INSERT INTO ld_geom(name,line_id,filename,total_km,n_part,n_pt,"
            " props,parts_gz,spine_gz,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (name, lid, name, 60.0, 1, len(CHAIN), "{}",
             _gz([CHAIN]), _gz(CHAIN), "2026-11-09 10:00:00"))
    return lid


def test_tmp_save():
    print("[3] 临时库：保存写回 ld_geom 且 ld_line 确认字段一字不动")
    old = (linedesk.DB_PATH, osmedit.RAIL_DB, osmedit.LING_DB)
    tmp = tempfile.TemporaryDirectory(prefix="railcn-osmcf-",
                                      ignore_cleanup_errors=True)
    ld = os.path.join(tmp.name, "linedesk.db")
    rb = os.path.join(tmp.name, "rail.db")
    linedesk.DB_PATH = ld
    osmedit.LING_DB = ld
    osmedit.RAIL_DB = rb
    confirmed.invalidate()
    osmedit._index_invalidate()
    try:
        linedesk.init()
        c = sqlite3.connect(rb)
        c.executescript("""
          CREATE TABLE IF NOT EXISTS line_geom(name TEXT PRIMARY KEY, file TEXT,
            total_km REAL, n_part INTEGER, n_pt INTEGER, props TEXT,
            parts_gz BLOB, spine_gz BLOB, created_at REAL);
          CREATE TABLE IF NOT EXISTS line_route(name TEXT, parts_gz BLOB,
            spine_gz BLOB);
          CREATE TABLE IF NOT EXISTS line_base(name TEXT, geom_name TEXT);
          CREATE TABLE IF NOT EXISTS line_base_stop(base_id INTEGER, name TEXT,
            lon REAL, lat REAL, km REAL, seq INTEGER);
        """)
        c.commit()
        c.close()
        with linedesk.conn() as d:
            l1 = _seed(d, "回归甲线", True)
            l2 = _seed(d, "回归乙线", False, geom_src="osm",
                       geom_name="京沪四线")
            _seed(d, "回归草稿线", False, status="draft")

        rec = confirmed.recent()
        ck(len(rec) == 2 and {x["name"] for x in rec} ==
           {"回归甲线", "回归乙线"},
           "临时库里 2 条完成确认线路（含无 ld_geom 的 osm 借走向线）")
        lst = osmedit.list_lines("", 100, 0)
        ck(lst.get("confirmed") == 2, "列表只把 2 条确认线路标红",
           lst.get("confirmed"))
        by = {x["name"]: x for x in lst["items"]}
        ck(by["回归甲线"]["confirmed"] and by["回归乙线"]["confirmed"]
           and by["回归甲线"]["loadName"] == "回归甲线"
           and by["回归乙线"]["loadName"] == "回归乙线",
           "两条都用规范名载入，不借别人几何")
        ck(not by.get("回归草稿线", {}).get("confirmed"),
           "草稿线不标红")
        ck(by["回归乙线"]["geomName"] == "京沪四线",
           "osm 借走向线保留来源名，前端可提示")

        g1 = osmedit.load_geom("回归甲线", 200)
        ck(bool(g1 and g1["confirmed"] and g1["source"] == "confirmed"),
           "有 ld_geom 的确认线载入主站同一份几何", g1 and g1.get("source"))
        g2 = osmedit.load_geom("回归乙线", 200)
        ck(bool(g2 and g2["confirmed"] and g2["source"] == "confirmed"
               and g2["geomName"] == "京沪四线"),
           "无 ld_geom 的确认线按 geom_name 解析 OSM 走向",
           g2 and g2.get("source"))

        # ① 已有 ld_geom 的确认线：应用后写回，确认字段一字不动
        before1 = _line_row(ld, l1)
        r1 = osmedit.save_geom("回归甲线", CHAIN2, apply=True,
                               confirmed_line=True)
        ck(bool(r1["confirmed"] and r1["keptConfirmed"]
                and r1["requestedConfirmed"]),
           "返回确认身份：保存后仍是整理台已确认线路")
        ck(r1["target"] == "回归甲线" and r1["applied"]["ld"] is True
           and r1["appliedAny"] is True and not r1["missing"],
           "★ 写回整理台 ld_geom，target 用规范名", str(r1)[:180])
        ck(before1 == _line_row(ld, l1),
           "★ ld_line 的 status/line_ok/stop_ok/geom_src/geom_name/时间/站序一字不动")
        c = sqlite3.connect(ld)
        row = c.execute("SELECT parts_gz,spine_gz,n_pt FROM ld_geom"
                        " WHERE name='回归甲线'").fetchone()
        c.close()
        parts = osmedit._ungz(row[0]) if row else None
        ck(bool(parts and len(parts) == 1 and len(parts[0]) == len(CHAIN2)
                and parts[0][0] == CHAIN2[0]),
           "ld_geom.parts_gz 真的换成新折线（%d 点）" % (len(parts[0]) if parts else 0))
        ck(bool(row and row[2] == len(CHAIN2)), "n_pt 同步更新")
        mg = linedesk.merged_geom(l1, "回归甲线")
        ck(bool(mg and mg.get("ldGeom")
                and len(mg["parts"][0]) == len(CHAIN2)),
           "★ merged_geom(ld_id) 命中新几何 → 主站确认通道会跟着变")

        # ② 原本没有 ld_geom 的 osm 借走向线：INSERT 一份并按 line_id 绑定
        before2 = _line_row(ld, l2)
        r2 = osmedit.save_geom("回归乙线", CHAIN2, apply=True,
                               confirmed_line=True)
        ck(before2 == _line_row(ld, l2),
           "★ osm 借走向线保存后 ld_line 也一字不动")
        ck(bool(r2["confirmed"] and r2["keptConfirmed"]
                and r2["applied"]["ld"] and not r2["missing"]),
           "★ 没有 ld_geom 也能生效（不再依赖来源名解析）", str(r2)[:180])
        c = sqlite3.connect(ld)
        rows = c.execute("SELECT name,line_id FROM ld_geom WHERE line_id=?",
                         (l2,)).fetchall()
        c.close()
        ck(len(rows) >= 1 and all(x[1] == l2 for x in rows),
           "★ 新插入的 ld_geom 按 line_id 绑定：%s" % rows)
        mg2 = linedesk.merged_geom(l2, "回归乙线")
        ck(bool(mg2 and mg2.get("ldGeom")
                and len(mg2["parts"][0]) == len(CHAIN2)),
           "★ merged_geom 现在命中这条线自己的几何")
        cg = confirmed.geom("回归乙线")
        ck(bool(cg and cg.get("ldGeom")),
           "confirmed.geom() 改读新几何（不再借 OSM 来源名）")
        ck(confirmed.stats()["confirmed"] == 2
           and len(confirmed.recent()) == 2,
           "确认线路仍 2 条，没被降级成草稿 / 没多出一条普通线")

        # ③ 非确认线路保持旧口径
        r3 = osmedit.save_geom("回归草稿线", CHAIN2, apply=True)
        ck(r3["confirmed"] is False and r3["keptConfirmed"] is False,
           "草稿线不会被当成确认线路")
        ck(r3["applied"]["ld"] is False, "草稿线不写整理台 ld_geom")
        c = sqlite3.connect(ld)
        n_ld = c.execute("SELECT COUNT(*) FROM ld_geom").fetchone()[0]
        c.close()
        ck(n_ld == 2, "整理台 ld_geom 只多了一份（乙线），没凭空造其它几何",
           n_ld)

        # ④ 只存草稿（apply=False）不写回整理台
        c = sqlite3.connect(ld)
        snap = c.execute("SELECT n_pt FROM ld_geom WHERE name='回归甲线'").fetchone()[0]
        c.close()
        r4 = osmedit.save_geom("回归甲线", CHAIN, apply=False)
        c = sqlite3.connect(ld)
        snap2 = c.execute("SELECT n_pt FROM ld_geom WHERE name='回归甲线'").fetchone()[0]
        c.close()
        ck(snap == snap2 and r4["applied"]["ld"] is False,
           "只存草稿不写回整理台，双轨语义不变")

        lst2 = osmedit.list_lines("", 100, 0)
        ck(lst2.get("confirmed") == 2
           and all(x["confirmed"] for x in lst2["items"][:2]),
           "保存 / 刷新索引后仍是 2 条红名排最前")
    finally:
        (linedesk.DB_PATH, osmedit.RAIL_DB, osmedit.LING_DB) = old
        confirmed.invalidate()
        osmedit._index_invalidate()
        tmp.cleanup()


# ---------------------------------------------------------------------------
def test_static():
    print("[4] 静态：前端标红 / 保存口径 / 服务端透传 / 后端通道")
    js = read("webapp/static/osmedit.js")
    css = read("webapp/static/osmedit.css")
    html = read("webapp/static/osmedit.html")
    py = read("webapp/osmedit.py")
    srv = read("webapp/server.py")
    ck("confirmed: '已确认'" in js, "来源标签里有「已确认」")
    ck("(it.confirmed ? ' confirmed' : '')" in js,
       "列表项按 confirmed 打 class（前端标红依据）")
    ck("整理台已确认 ${d.confirmed" in js,
       "列表计数显示确认线路条数（红名提示）")
    ck("S.confirmed ? S.name : (S.loadName || S.name)" in js,
       "载入按规范名请求（确认线路不借来源名）")
    ck("target: S.confirmed ? S.name : (S.loadName || S.name)" in js,
       "保存 target 对确认线路用规范名")
    ck("confirmed: S.confirmed" in js, "保存请求带确认标志")
    ck("仍是整理台已确认线路" in js and "载入的是主站正在显示的那份多段走向" in js,
       "提示写明「仍是确认线路」+ 载入主站多段走向（段间不补假直线）")
    ck("r.confirmed" in js, "保存成功后回写确认状态")
    ck("S.confirmed = false; S.geomName = ''; S.ldId = 0" in js,
       "新建线路会清掉确认态，不会误写整理台")
    ck(".oe-item.confirmed{background" in css.replace(" ", "")
       or ".oe-item.confirmed{border-left" in css,
       "CSS 有确认线路样式块")
    ck(".oe-item.confirmed .nm{color:#c5221f}" in css,
       "★ 线名标红")
    ck("红名 = 整理台已确认线路" in html, "① 选择线路说明里有红名解释")
    ck("confirmed_line=bool(payload.get(\"confirmed\"))" in srv,
       "服务端透传 confirmed 标志")
    ck("def _apply_confirmed(" in py and "INSERT INTO ld_geom" in py,
       "后端有按 line_id 写回 / 插入 ld_geom 的专属通道")
    ck("not bool(x.get(\"confirmed\"))" in py,
       "排序把确认线路置顶、测试名仍沉底")
    ck("confirmed.invalidate()" in py, "保存后主动刷新确认清单缓存")
    ck("UPDATE ld_line" not in py and "INSERT INTO ld_line" not in py,
       "★ 走向编辑器全程不写 ld_line（确认状态不会被改）")


def test_http():
    print("[5] HTTP：走向编辑器列表 / 载入出口（服务没起则跳过）")

    def get(path):
        with urllib.request.urlopen("http://127.0.0.1:8686" + path,
                                    timeout=30) as r:
            return json.loads(r.read().decode())

    try:
        d = get("/api/osmedit/lines?q=&limit=5")
    except Exception as e:                                   # noqa: BLE001
        print("  skip  服务没起：%s" % e)
        return
    ck(d.get("confirmed") == confirmed.stats()["confirmed"],
       "列表接口返回确认线路条数")
    ck(all(x.get("confirmed") and x.get("ldId") for x in d["items"]),
       "首屏全是红名确认线路")
    g = get("/api/osmedit/geom?name=" + urllib.parse.quote("济南线") + "&n=100")
    ck(g.get("confirmed") and g.get("keepsConfirmed") and g.get("ldId")
       and g.get("geomName") == "京沪四线" and g.get("loadName") == "济南线",
       "济南线按规范名载入，返回确认身份与走向来源名")


def main():
    test_real_index()
    test_real_load()
    test_tmp_save()
    test_static()
    test_http()
    print("\n通过 %d 项，失败 %d 项" % (OK, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
