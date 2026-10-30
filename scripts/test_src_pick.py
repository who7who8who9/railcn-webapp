"""回归：站序「默认来源」勾选 + 按来源倒排

两条口径：

  1. **默认来源**：同一条线名在几个来源里各有站序，用户在界面上点 ★ 勾一份以后，
     全项目所有「不指定来源」的取数（线路详情、选经由、拼站序）都按那一份来。
     指定 from 的临时查看仍然优先 —— 勾的是默认，不是锁死。
  2. **按来源倒排**：同一条线的几份站序方向可能不一致（OSM 京沪线是 上海→北京，
     档案是 北京→上海）。倒排只动**当前这一份**：资料库 / china-emu / OSM /
     分段拼记一条 `line_src_fix`，读的时候翻转，原始数据不改；再点一次就恢复。
     库内线路（lib / custom）那份本来就是线路自己，才走人工站序。

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


def fixes():
    try:
        with sqlite3.connect(DB) as c:
            return {(r[0], r[1], r[2])
                    for r in c.execute("SELECT name, source, kind FROM line_src_fix")}
    except sqlite3.DatabaseError:
        return set()


def lineseg_sources(name):
    """详情页那条来源切换条上的各来源（首末站 / 站数 / 是否已倒排）"""
    return line_of(name, "osm").get("sources") or []


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


def test_reverse_source():
    """核心：只翻某一个来源的那份站序，线路本身（档案 / 人工站序）一点都不动"""
    print("\n== 倒排：只翻一个来源 ==")
    # 找一条「OSM 那份和线路库那份方向相反」的线，这就是用户真遇到的情形
    nm = ""
    for cand in ("京沪线", "京广线", "陇海线", "沪昆线", "京哈线"):
        try:
            a, b = line_of(cand), line_of(cand, "osm")
        except Exception:                    # noqa: BLE001
            continue
        sa, sb = a.get("stations") or [], b.get("stations") or []
        if len(sa) > 1 and len(sb) > 1 and sa[0]["name"] != sb[0]["name"]:
            nm = cand
            break
    if not nm:
        print("  跳过：没找到方向相反的线")
        return

    keep = any(f[:2] == (nm, "osm") for f in fixes())      # 跑完还原成进来时的样子
    lib = line_of(nm)
    lib_first, lib_last = lib["stations"][0]["name"], lib["stations"][-1]["name"]
    cat = line_of(nm, "catalog")
    cat_first = (cat.get("stations") or [{}])[0].get("name")
    osm = line_of(nm, "osm")
    osm_first, osm_last = osm["stations"][0]["name"], osm["stations"][-1]["name"]
    ovr = overrides()
    print("  %s：线路库 %s→%s，OSM %s→%s" % (nm, lib_first, lib_last, osm_first, osm_last))
    try:
        post("/api/line/stations/reverse", {"name": nm, "from": "osm", "mode": "off"})

        r = post("/api/line/stations/reverse", {"name": nm, "from": "osm"})
        ck(r.get("inPlace") is True, "只挂来源修正，不当成整条线改", r)
        ck(r.get("reversed") is True and r.get("n") == len(osm["stations"]),
           "OSM 那份翻了，站数不变", "%s 站" % r.get("n"))
        ck(r.get("first") == osm_last and r.get("last") == osm_first,
           "首末对调", "%s→%s" % (r.get("first"), r.get("last")))

        d = line_of(nm, "osm")
        ck(d.get("reversed") is True, "详情页带 reversed 标记")
        ck("reverse" in (d.get("srcFix") or []), "详情页带 srcFix=reverse", d.get("srcFix"))
        ck(d["stations"][0]["name"] == osm_last, "看 OSM 那份是反过来的",
           d["stations"][0]["name"])
        kms = [s["km"] for s in d["stations"] if s.get("km") is not None]
        drops = sum(1 for i in range(len(kms) - 1) if kms[i] > kms[i + 1])
        ck(kms and kms[0] == 0 and drops <= 2, "里程按新起点重算（整体递增）",
           "%s 处倒挂" % drops)

        # 这三条是用户最在意的：别的来源和线路本身不能被带着改
        ck(line_of(nm)["stations"][0]["name"] == lib_first
           and line_of(nm)["stations"][-1]["name"] == lib_last,
           "线路库那份没动", "%s→%s" % (lib_first, lib_last))
        ck((cat.get("stations") or [{}])[0].get("name") == cat_first,
           "资料库那份没动", cat_first)
        ck(nm not in overrides() or nm in ovr, "没有生成本条线的人工站序")

        vs = {v["source"]: v for v in lineseg_sources(nm)}
        ck(vs.get("osm", {}).get("reversed") is True, "来源条上 OSM 标了已倒排")
        ck(vs.get("osm", {}).get("first") == osm_last, "来源条上 OSM 首站也反了")
        ck(not vs.get("catalog", {}).get("reversed"), "资料库那份没被标")

        # 取消：标记删掉，原始站序原样回来
        r2 = post("/api/line/stations/reverse", {"name": nm, "from": "osm"})
        ck(r2.get("reversed") is False, "再点一次是恢复，不是二次翻转")
        d2 = line_of(nm, "osm")
        ck(d2["stations"][0]["name"] == osm_first, "OSM 回到原方向",
           d2["stations"][0]["name"])
        ck(not (nm, "osm", "reverse") in fixes(), "修正标记已删")
    finally:
        post("/api/line/stations/reverse",
             {"name": nm, "from": "osm", "mode": "on" if keep else "off"})


def test_reverse_loose():
    """线路库里没有、只有资料库站序的线：倒排只翻资料库那一份，不把线收编进库"""
    print("\n== 倒排：库外线路 ==")
    if picks().get(CANON):
        post("/api/line/src-pick", {"name": CANON, "source": ""})
    d = line_of(CANON)
    if d.get("custom"):
        print("  跳过：%s 已经被收进线路库了" % CANON)
        return
    before = overrides()
    first = d["stations"][0]["name"]
    try:
        r = post("/api/line/stations/reverse", {"name": CANON, "from": "catalog"})
        ck(r.get("inPlace") is True, "只挂来源修正，不落人工站序", r.get("from"))
        ck(r.get("name") == CANON, "落在线名上", r.get("name"))
        ck(r.get("first") != first and r.get("last") == first,
           "站序反了", "%s→%s（原首站 %s）" % (r.get("first"), r.get("last"), first))
        ck(CANON not in (overrides() - before), "没有因此生出人工站序")
        r2 = post("/api/line/stations/reverse",
                  {"name": CANON, "from": "catalog", "mode": "off"})
        ck(r2.get("reversed") is False, "能取消")
        ck(line_of(CANON)["stations"][0]["name"] == first, "回到原样", first)
    finally:
        post("/api/line/stations/reverse",
             {"name": CANON, "from": "catalog", "mode": "off"})


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
    test_reverse_source()
    test_reverse_loose()
    test_ui()
    print("\n%s" % ("全部通过" if not _fail else "失败 %d 项" % _fail))
    sys.exit(1 if _fail else 0)


main()
