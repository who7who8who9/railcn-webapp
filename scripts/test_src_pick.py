"""回归：站序「默认来源」勾选 + 站序倒排

两条口径：

  1. **默认来源**：同一条线名在几个来源里各有站序，用户在界面上点 ★ 勾一份以后，
     全项目所有「不指定来源」的取数（线路详情、选经由、拼站序）都按那一份来。
     指定 from 的临时查看仍然优先 —— 勾的是默认，不是锁死。
  2. **倒排**：整条线的站序反过来，里程按新起点重算后落库。库外线路先收编，
     档案线路存人工站序，所以「恢复档案站序」还能回到原样。

用法：python scripts/test_src_pick.py      （需要服务在 8686 跑着）
"""
import json
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8686"
DB = "webapp/data/rail.db"
LINE = "京广线"
ALIAS = "侯西铁路"          # 资料库 title，规范名是「侯西线」
CANON = "侯西线"
FILE_LINE = "沪昆高速线"     # 档案线，用来测倒排（测前确认没有人工站序）
TMP = "ZZ回归倒排线"

_fail = 0


def ck(ok, msg, extra=""):
    global _fail
    print(("  ok   " if ok else "  FAIL ") + msg + (("  " + str(extra)) if extra else ""))
    if not ok:
        _fail += 1


def get(path):
    return json.load(urllib.request.urlopen(BASE + path, timeout=120))


def post(path, body):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    return json.load(urllib.request.urlopen(req, timeout=180))


def q(name):
    return urllib.parse.quote(name)


def line_of(name, frm=""):
    return get("/api/line?q=" + q(name) + ("&from=" + q(frm) if frm else ""))


def picks():
    with sqlite3.connect(DB) as c:
        return {r[0]: r[1] for r in c.execute("SELECT name, source FROM line_src_pick")}


def overrides():
    with sqlite3.connect(DB) as c:
        return {r[0] for r in c.execute("SELECT name FROM line_override")}


# ---------------------------------------------------------------------------
# 一、默认来源
# ---------------------------------------------------------------------------
def test_pick():
    print("\n== 默认来源 ==")
    old = picks().get(LINE, "")
    try:
        post("/api/line/src-pick", {"name": LINE, "source": ""})
        d = line_of(LINE)
        ck(not d.get("srcPick"), "没勾时 srcPick 为空", d.get("srcPick"))
        n_auto = d["n"]
        src_auto = d.get("stopsFrom")

        r = post("/api/line/src-pick", {"name": LINE, "source": "osm"})
        ck(r.get("ok") and r.get("source") == "osm", "勾 OSM 为默认能存下")
        ck(picks().get(LINE) == "osm", "库里确实写进去了")

        d = line_of(LINE)
        ck(d.get("srcPick") == "osm", "详情里带 srcPick", d.get("srcPick"))
        ck(d.get("stopsFrom") == "osm", "不指定 from 也按默认走 OSM", d.get("stopsFrom"))
        ck(d["n"] != n_auto or src_auto == "osm",
           "默认生效后站序换了来源", "%s 站（原来 %s/%s 站）" % (d["n"], src_auto, n_auto))
        ck(d.get("effFrom") == "osm", "effFrom 标出这次实际用哪份", d.get("effFrom"))

        # 选经由也用这一份，否则人工挑过的站名和选经由时的站名对不上
        s = get("/api/line/stops?q=" + q(LINE))
        ck(s.get("from") == "osm", "选经由的站序跟着默认走", s.get("from"))
        ck(s.get("count") == d["n"], "两处站数一致", "%s / %s" % (s.get("count"), d["n"]))

        # 联想框要能显示哪份是默认的
        l = get("/api/lines?q=" + q("京广"))
        ck((l.get("picks") or {}).get(LINE) == "osm", "联想接口带出默认标记")
        vs = (l.get("variants") or {}).get(LINE) or []
        ck(any(v["source"] == "osm" and v["n"] > 100 for v in vs),
           "联想里各来源站数仍照列", [(v["source"], v["n"]) for v in vs])

        # 临时点别的来源优先于默认：勾的是默认，不是锁死
        c2 = line_of(LINE, "catalog")
        ck(c2.get("stopsFrom") == "catalog",
           "指定 from=catalog 时不被默认覆盖", c2.get("stopsFrom"))
        ck(c2.get("srcPick") == "osm", "临时查看不改默认", c2.get("srcPick"))

        # 取消
        post("/api/line/src-pick", {"name": LINE, "source": ""})
        d = line_of(LINE)
        ck(not d.get("srcPick"), "取消后不再有默认")
        ck(d.get("stopsFrom") == src_auto, "回到系统规则",
           "%s（原来 %s）" % (d.get("stopsFrom"), src_auto))
    finally:
        post("/api/line/src-pick", {"name": LINE, "source": old})


def test_pick_alias():
    """按规范名勾的默认，搜别名（资料库 title）也要认"""
    print("\n== 默认来源按别名查找 ==")
    old = picks().get(CANON, "")
    try:
        post("/api/line/src-pick", {"name": CANON, "source": ""})
        post("/api/line/src-pick", {"name": CANON, "source": "catalog"})
        d = line_of(ALIAS)
        ck(d.get("name") == CANON or d.get("aliasOf") == ALIAS,
           "搜别名仍落到规范线名", "%s / aliasOf=%s" % (d.get("name"), d.get("aliasOf")))
        ck(d.get("srcPick") == "catalog",
           "搜别名也能看到按规范名勾的默认", d.get("srcPick"))
    finally:
        post("/api/line/src-pick", {"name": CANON, "source": old})


def test_pick_bad():
    print("\n== 非法来源 ==")
    try:
        post("/api/line/src-pick", {"name": LINE, "source": "huirui-x"})
        ck(False, "非法来源要报错")
    except urllib.error.HTTPError as e:
        ck(e.code >= 400, "非法来源被拒", "HTTP %s" % e.code)


# ---------------------------------------------------------------------------
# 二、倒排
# ---------------------------------------------------------------------------
def test_reverse_custom():
    print("\n== 倒排：自建线往返 ==")
    post("/api/line/new", {"name": TMP, "replace": True, "note": "回归用，跑完就删",
                           "stations": [{"name": "石家庄", "km": 0},
                                        {"name": "保定", "km": 131.5},
                                        {"name": "北京西", "km": 281.0}]})
    try:
        r = post("/api/line/stations/reverse", {"name": TMP})
        st = r["line"]["stations"]
        ck([s["name"] for s in st] == ["北京西", "保定", "石家庄"], "站序反过来了",
           [s["name"] for s in st])
        ck([s["km"] for s in st] == [0.0, 149.5, 281.0], "里程按新起点重排",
           [s["km"] for s in st])
        ck(abs((r.get("totalKm") or 0) - 281.0) < 0.05, "总里程不变", r.get("totalKm"))
        ck(r.get("missing") == 0, "没有缺里程的站", r.get("missing"))

        r2 = post("/api/line/stations/reverse", {"name": TMP})
        st = r2["line"]["stations"]
        ck([s["name"] for s in st] == ["石家庄", "保定", "北京西"], "再倒一次回到原样")
        ck([s["km"] for s in st] == [0.0, 131.5, 281.0], "里程也回到原样",
           [s["km"] for s in st])

        # 备注不能被倒排改掉
        with sqlite3.connect(DB) as c:
            note = c.execute("SELECT note FROM line_custom WHERE name=?", (TMP,)).fetchone()
        ck(note and "回归用" in (note[0] or ""), "自建线的备注没被覆盖", note)
    finally:
        post("/api/line/delete", {"name": TMP})


def test_reverse_file():
    print("\n== 倒排：档案线进人工站序、可恢复 ==")
    before = overrides()
    d = line_of(FILE_LINE)
    if FILE_LINE in before:
        print("  跳过：这条线本来就有手工站序，不动它")
        return
    try:
        first, last = d["stations"][0]["name"], d["stations"][-1]["name"]
        r = post("/api/line/stations/reverse", {"name": FILE_LINE, "from": "lib"})
        st = r["line"]["stations"]
        ck(st[0]["name"] == last and st[-1]["name"] == first,
           "首末对调", "%s→%s" % (st[0]["name"], st[-1]["name"]))
        ck(st[0].get("km") in (0, 0.0), "新起点从 0 起算", st[0].get("km"))
        ck(abs((st[-1].get("km") or 0) - (r.get("totalKm") or -1)) < 0.6,
           "新终点里程等于总长", "%s vs %s" % (st[-1].get("km"), r.get("totalKm")))
        ck(FILE_LINE in overrides(), "落成了人工站序")
        # 里程必须按原里程镜像，不是把数组反过来。原档案里个别站里程是错的
        # （沪昆高速线 溆浦南 1415 排在 怀化南 1149 前面），镜像后仍然错，
        # 这里只要求"整体是往上的"，允许原有的那几处倒挂
        kms = [s["km"] for s in st if s.get("km") is not None]
        drops = sum(1 for i in range(len(kms) - 1) if kms[i] > kms[i + 1])
        ck(drops <= 2, "里程整体递增（只保留原数据本来就有的倒挂）",
           "%d 处倒挂 / %d 站" % (drops, len(kms)))

        c = post("/api/line/stations/clear", {"name": FILE_LINE})
        ck(c.get("cleared") and c["line"]["stations"][0]["name"] == first,
           "恢复档案站序能回到原样")
    finally:
        post("/api/line/stations/clear", {"name": FILE_LINE})


def test_reverse_loose():
    """线路库里没有、只有资料库站序的线：倒排要先把线收编再落站序"""
    print("\n== 倒排：库外线路 ==")
    if picks().get(CANON):
        post("/api/line/src-pick", {"name": CANON, "source": ""})
    d = line_of(CANON)
    if d.get("custom"):
        print("  跳过：%s 已经被收进线路库了" % CANON)
        return
    try:
        first = d["stations"][0]["name"]
        r = post("/api/line/stations/reverse", {"name": CANON})
        st = r["line"]["stations"]
        ck(r["name"] == CANON, "落到规范线名", r.get("name"))
        ck(st[0]["name"] != first and st[-1]["name"] == first,
           "站序反了", "%s→%s（原首站 %s）" % (st[0]["name"], st[-1]["name"], first))
    finally:
        post("/api/line/delete", {"name": CANON})


def test_ui():
    print("\n== 界面 ==")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  跳过：没装 playwright")
        return
    errs = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(BASE + "/?line=" + q(FILE_LINE), wait_until="load")
        pg.wait_for_timeout(3000)
        ck(pg.locator("#lnRevBtn").count() == 1, "详情页有「倒排站序」按钮")
        ck(pg.locator("#lnHead .ln-src .pick").count() >= 2, "来源条每份都带 ★")
        pg.click("#lnRevBtn")
        pg.wait_for_timeout(300)
        ck("确认" in pg.inner_text("#lnRevBtn"), "点一次先问一句，不直接动数据",
           pg.inner_text("#lnRevBtn"))
        # 四秒后自动复位，别让误点留在确认状态
        pg.wait_for_timeout(4200)
        ck(pg.inner_text("#lnRevBtn").strip() == "倒排站序", "超时自动复位",
           pg.inner_text("#lnRevBtn"))
        b.close()
    ck(not errs, "页面没有 JS 报错", errs[:2])


def main():
    test_pick()
    test_pick_alias()
    test_pick_bad()
    test_reverse_custom()
    test_reverse_file()
    test_reverse_loose()
    test_ui()
    print("\n%s" % ("全部通过" if not _fail else "失败 %d 项" % _fail))
    sys.exit(1 if _fail else 0)


main()
