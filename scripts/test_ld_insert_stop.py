# -*- coding: utf-8 -*-
"""回归：整理台「新增车站」可以指定插入位置（2026-11-09）

旧行为：`#ldAddStop` 用 prompt() 只收一个站名，一律 `a.push(...)` 追加到末尾，
想插到中间只能连点 ↑；站序行只有 ↑↓✕，帮助文案里写的「+ 加站」其实没这个按钮。

新契约：
  - 后端 `linedesk.insert_stops(lid, names, pos)`，pos 是**插入后的 0-based
    起始下标**：0 最前 / i+1 插在第 i+1 站之后 / -1 或 >= n 追加到末尾。
  - 一次可插多个站名（数组，或按换行 / 逗号 / 顿号 / 分号切的字符串），按填写顺序连续插入。
  - 新站 km 留空、不插值；原有各站 km / 坐标 / src / note 一个字不动；
    坐标按站名查档案补。
  - 落库复用 `save_stops()` ⇒ stop_ok=0、stops_edited=1、起讫站更新、seq 连续。
  - 前端：站序每行 + 预选「插在它后面」，顶部「+ 站」弹窗可选任意位置。
  - HTTP：`POST /api/ld/stops/insert` {id, names, pos}。
"""
import gc
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "webapp"))
import linedesk  # noqa: E402

OK = FAIL = 0


def ck(cond, msg):
    global OK, FAIL
    if cond:
        OK += 1
        print("  OK   " + msg)
    else:
        FAIL += 1
        print("  FAIL " + msg)


BASE = [
    {"name": "甲站", "km": 0, "lat": 31.0, "lon": 121.0, "src": "rg", "note": "n1"},
    {"name": "乙站", "km": 20, "lat": 31.2, "lon": 120.5, "src": "rg", "note": "n2"},
    {"name": "丙站", "km": 50, "lat": 31.4, "lon": 120.0, "src": "rg", "note": "n3"},
]
NEW = "__回归插入站A__"


def names(d):
    return [s["name"] for s in (d.get("stops") or [])]


def raw(lid):
    with linedesk.conn() as c:
        return dict(c.execute("SELECT * FROM ld_line WHERE id=?", (lid,)).fetchone())


def seqs(lid):
    with linedesk.conn() as c:
        return [r[0] for r in c.execute(
            "SELECT seq FROM ld_stop WHERE line_id=? ORDER BY seq", (lid,))]


def reset(lid):
    return linedesk.save_stops(lid, BASE)


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


print("=== 整理台新增车站：指定插入位置 ===")
orig_db = linedesk.DB_PATH
tmpdir = tempfile.TemporaryDirectory(prefix="railcn-ld-insert-",
                                     ignore_cleanup_errors=True)
linedesk.DB_PATH = os.path.join(tmpdir.name, "linedesk.db")
try:
    linedesk.init()
    with linedesk.conn() as c:
        lid = c.execute(
            "INSERT INTO ld_line(name,family,kind,status) VALUES(?,?,?,?)",
            ("__回归插入站线__", "__回归插入站线__", "gs", "draft")).lastrowid
        lid2 = c.execute(
            "INSERT INTO ld_line(name,family,kind,status) VALUES(?,?,?,?)",
            ("__回归空站序线__", "__回归空站序线__", "gs", "draft")).lastrowid
    N1, N2, N3 = [linedesk._norm_stop_name(x["name"]) for x in BASE]
    NM = linedesk._norm_stop_name(NEW)

    reset(lid)
    d = linedesk.save_stops(lid, BASE)
    ck(names(d) == [N1, N2, N3], "基线 3 站")

    # ---- 末尾插入（pos=-1 / 缺省） ----
    d = linedesk.insert_stops(lid, [NEW], -1)
    ck(names(d) == [N1, N2, N3, NM], "★ pos=-1 追加到末尾")
    ck(d["inserted"] == 1 and d["insertPos"] == 3, "返回插入数量与位置（3 = 第 4 位）")
    ck(seqs(lid) == [1, 2, 3, 4], "落库 seq 连续 1..4")
    old = d["stops"][:3]
    ck(all(old[i]["km"] == BASE[i]["km"] and old[i]["lat"] == BASE[i]["lat"]
           and old[i]["lon"] == BASE[i]["lon"] and old[i]["src"] == BASE[i]["src"]
           and old[i]["note"] == BASE[i]["note"] for i in range(3)),
       "★ 原有 3 站的里程 / 坐标 / 来源 / 备注一个字没动")
    nw = d["stops"][3]
    ck(nw["km"] is None, "★ 新站里程留空、不插值")
    ck(nw["src"] == "manual", "新站来源标记 manual")
    ck(nw["lat"] is None and nw["lon"] is None, "查不到档案的站名坐标留空（页面标没定位）")
    ck(raw(lid)["from_sta"] == N1 and raw(lid)["to_sta"] == NM,
       "末尾插入后起讫站跟着更新")
    ck(raw(lid)["stop_ok"] == 0 and raw(lid)["stops_edited"] == 1,
       "★ 插入和手工改站序同口径：stop_ok 打回 0、stops_edited=1")
    ck(d["stopsEdited"] is True, "line_detail 输出 stopsEdited（前端据此默认选本线站序）")

    # ---- 最前面 ----
    reset(lid)
    d = linedesk.insert_stops(lid, NEW, 0)
    ck(names(d) == [NM, N1, N2, N3], "★ pos=0 插到最前面")
    ck(d["insertPos"] == 0, "返回位置 0")
    ck(d["stops"][0]["km"] is None and d["stops"][1]["km"] == 0,
       "★ 插到最前不插值：新站里程仍空，原首站 0 原样保留")
    ck(raw(lid)["from_sta"] == NM and raw(lid)["to_sta"] == N3,
       "插到最前后起讫站 = 新站 / 原末站")

    # ---- 中间 ----
    reset(lid)
    d = linedesk.insert_stops(lid, NEW, 2)
    ck(names(d) == [N1, N2, NM, N3], "★ pos=2 插在第 2 站之后、第 3 站之前")
    ck(d["insertPos"] == 2, "返回位置 2")
    reset(lid)
    ck(names(linedesk.insert_stops(lid, NEW, 1)) == [N1, NM, N2, N3],
       "pos=1 插在第 1 站之后")

    # ---- 一次多站 / 字符串切分 ----
    reset(lid)
    many = ["__回归B__", "__回归C__", "__回归D__"]
    d = linedesk.insert_stops(lid, many, 2)
    ck(names(d) == [N1, N2] + [linedesk._norm_stop_name(x) for x in many] + [N3],
       "★ 一次填 3 站按填写顺序连续插入")
    ck(d["inserted"] == 3, "返回插入数量 3")
    reset(lid)
    d = linedesk.insert_stops(lid, "__回归B__，__回归C__、__回归D__; __回归E__\n__回归F__", 1)
    ck(d["inserted"] == 5, "★ 字符串按逗号 / 顿号 / 分号 / 换行切成 5 站")
    ck(all(s["km"] is None for s in d["stops"] if s["name"].startswith("__回归")),
       "批量插入的每一站里程都留空")
    ck(linedesk._split_stop_names(["A、B", " C "]) == ["A、B", "C"],
       "数组入参不再二次切分，站名自带顿号也保留")

    # ---- 越界 / 非法位置 ----
    reset(lid)
    ck(names(linedesk.insert_stops(lid, NEW, 999)) == [N1, N2, N3, NM],
       "pos 超上界夹到末尾，不报错")
    reset(lid)
    ck(names(linedesk.insert_stops(lid, NEW, -5)) == [N1, N2, N3, NM],
       "pos 负数按末尾处理")
    reset(lid)
    ck(names(linedesk.insert_stops(lid, NEW, "abc")) == [N1, N2, N3, NM],
       "pos 不是数字也按末尾处理")
    reset(lid)
    ck(names(linedesk.insert_stops(lid, NEW, 3)) == [N1, N2, N3, NM],
       "pos=n（第 n 站之后）= 末尾，和 -1 一致")

    # ---- 空站序 / 错误输入 ----
    d = linedesk.insert_stops(lid2, "__回归X__", 0)
    ck(names(d) == [linedesk._norm_stop_name("__回归X__")],
       "★ 原本没有站序也能直接插入建站序")
    ck(raw(lid2)["from_sta"] == raw(lid2)["to_sta"], "1 站时起讫都指向它")
    for bad, label in (([], "空数组"), (["", "  "], "全是空白"), ("，；\n", "全是分隔符")):
        try:
            linedesk.insert_stops(lid, bad, 1)
            ck(False, label + "应被拦住")
        except ValueError as e:
            ck("站名" in str(e), label + "被拦住：" + str(e))
    try:
        linedesk.insert_stops(999999, NEW, 0)
        ck(False, "不存在的线路应被拦住")
    except ValueError as e:
        ck("没有这条线路" in str(e), "不存在的线路被拦住：" + str(e))

    # ---- 真实站名按档案补坐标 ----
    coords = linedesk._coords()
    probe = next((k for k in ("北京南", "杭州东", "上海", "广州南")
                  if (coords.get(k) or [None])[0] is not None), None)
    if probe:
        reset(lid)
        d = linedesk.insert_stops(lid, probe, 1)
        s = d["stops"][1]
        ck(s["lat"] is not None and s["lon"] is not None,
           "★ 真实站「%s」插入后自动补到坐标" % probe)
    else:
        print("  SKIP 档案里没找到可用的真实站名，跳过补坐标检查")

finally:
    linedesk.DB_PATH = orig_db
    gc.collect()
    tmpdir.cleanup()


print()
print("=== 前端 / 路由静态检查 ===")
js = read("webapp/static/linedesk.js")
srv = read("webapp/server.py")
css = read("webapp/static/linedesk.css")
ck('data-ins="${i}"' in js, "站序每行有 + 插入按钮 data-ins")
ck("function openInsertStop(" in js, "有指定插入位置的弹窗函数")
ck("$$('#ldPanel [data-ins]')" in js, "每行的 + 已绑定")
ck("openInsertStop(null)" in js, "顶部「+ 站」走同一个弹窗（默认末尾）")
ck("Number(b.dataset.ins) + 1" in js, "行内 + 预选「插在这一站之后」")
ck("ldInsName" in js and "ldInsPos" in js, "弹窗有站名框 + 插入位置下拉")
ck("最前面（作为第 1 站）" in js and "之后${i === n - 1 ? '（末尾）'" in js,
   "下拉含「最前面」与「第 N 站之后（末尾）」")
ck("/api/ld/stops/insert" in js, "前端调新接口")
ck("S.pick.stop = { self: true }" in js, "插入后 ② 默认选中「现在的站序」")
ck("prompt('加哪一站" not in js, "★ 旧 prompt 一律追加到末尾的写法已删")
ck("指定插入位置" in js, "帮助 / 提示文案写明可指定位置")
ck("里程留空、不自动插值" in js, "提示写明新站里程不插值")
ck("/api/ld/stops/insert" in srv and "linedesk.insert_stops(" in srv,
   "server.py 有 /api/ld/stops/insert 路由")
ck(".st .op button.ins:hover" in css, "插入按钮有独立 hover 样式")

print()
print("通过 %d / 失败 %d" % (OK, FAIL))
sys.exit(1 if FAIL else 0)
