"""回归：同一条线的「几个来源版本」怎么归组、默认版本怎么生效

背景（2026-11-02 用户要求）：备份批量导回后，同一条线在库里是几条独立的线 ——
`京沪线` / `京沪线（底表）` / `京沪线（OSM）` / `京沪线（自建）` / `京沪线（线路资料库·82站）`。
用户要的是：**只有被选中的默认版本**用于数据校对的线路选择和地图；没设默认时校对里
把所有版本都列出来，系统按「底表」那份取数。

这个脚本验的就是这条口径：

  1. 归组：搜「京沪线」时 variants 里能看到那几个版本，`groups['京沪线'].base == '京沪线'`
  2. 生效版本：没设默认 → 底表；`autoFrom` 标出来，`askedName` 是原搜索词
  3. 明确要看某一份：`exact=1` 或名字带「（来源）」后缀，不被替换
  4. 设默认：`POST /api/line/default` 之后，搜基础名直接给默认那份；
     `/api/curate/lineNames` 只剩一条（名字用基础名），`/api/line/stops` 跟着换
  5. 取消默认：回到底表
  6. 乱填版本名要 400

用法：python scripts/test_line_group.py      （需要服务在 8686 跑着）
跑完会把 `line_default` 里自己写的行删掉，不留脏数据。
"""
import json
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8686"
DB = "webapp/data/rail.db"
# 2026-11-05：京沪线等 490 条主线已被合并走向接管成「基本线路」（effectiveWhy=linebase），
# 归组/默认这套口径要在**没有基本线路**的线上验，否则测的是 linebase 优先级。
CANDIDATES = ["京沪高速线", "丰双线", "伊加线", "北黑线", "东吴线", "乌大高速线"]
LINE = CANDIDATES[0]

_fail = 0
_written = []          # 这个脚本动过的 line_default 行（收尾删掉）


def ck(ok, msg, extra=""):
    global _fail
    print(("  ok   " if ok else "  FAIL ") + msg + (("  " + str(extra)) if extra else ""))
    if not ok:
        _fail += 1


def get(path, **params):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v != ""})
    return json.load(urllib.request.urlopen(url, timeout=120))


def post(path, body):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    return json.load(urllib.request.urlopen(req, timeout=180))


def set_default(name, pick):
    post("/api/line/default", {"name": name, "pick": pick})
    with sqlite3.connect(DB) as c:
        if pick:
            _written.append(name)
        else:
            c.execute("DELETE FROM line_default WHERE name=?", (name,))
            c.commit()


def cleanup():
    with sqlite3.connect(DB) as c:
        for nm in set(_written):
            c.execute("DELETE FROM line_default WHERE name=?", (nm,))
        c.commit()


def main():
    global LINE
    bases = set()
    try:
        with sqlite3.connect("file:" + DB.replace("\\", "/") + "?mode=ro", uri=True) as c:
            bases = {r[0] for r in c.execute("SELECT name FROM line_base")}
    except sqlite3.DatabaseError:
        pass
    picked = None
    for cand in CANDIDATES:
        if cand in bases:
            continue
        gg = (get("/api/lines", q=cand, limit=8).get("groups") or {}).get(cand) or {}
        if (gg.get("groupN") or 0) >= 2:
            picked = cand
            break
    if not picked:
        print("跳过：候选线名都被基本线路接管或只有一个版本")
        return 0
    LINE = picked
    print("  用「%s」验归组（不是基本线路）" % LINE)

    d = get("/api/lines", q=LINE, limit=8)
    items = d.get("items") or []
    groups = d.get("groups") or {}
    g = groups.get(LINE) or {}
    if not g or (g.get("groupN") or 0) < 2:
        print("跳过：库里「%s」只有一个版本（没有可归组的来源版本）" % LINE)
        return 0
    ck(g.get("base") == LINE, "归组：基础名是「%s」" % LINE, g.get("base"))
    ck(g.get("groupN") >= 2, "归组：%d 个版本" % g.get("groupN"))

    vs = (d.get("variants") or {}).get(LINE) or []
    mem = [v for v in vs if v.get("kind") == "member"]
    ck(bool(mem), "variants 里有 kind=member 的版本条目", len(mem))
    names = [v.get("sourceName") for v in mem]
    ck(any("底表" in (n or "") for n in names), "版本里有「（底表）」那份", names[:4])
    ck(all(v.get("n") for v in vs), "每个版本都带站数", [v.get("n") for v in vs])

    # 1) 没设默认 → 底表
    set_default(LINE, "")
    one = get("/api/line", q=LINE)
    ck(bool(one.get("autoFrom")), "系统替用户挑了一份：autoFrom 非空", one.get("autoFrom"))
    ck("底表" in (one.get("name") or ""), "没设默认：生效版本是底表那份", one.get("name"))
    ck(one.get("askedName") == LINE, "askedName 记着原搜索词", one.get("askedName"))
    ck((one.get("group") or {}).get("effectiveWhy") == "base", "effectiveWhy = base")

    # 2) exact / 带后缀：要看哪份给哪份
    ex = get("/api/line", q=LINE, exact=1)
    ck(ex.get("name") == LINE, "exact=1 时就是你点的那个名字", ex.get("name"))
    ck(not ex.get("autoFrom"), "exact=1 不替换", ex.get("autoFrom"))

    # 3) 设默认 = 某个来源版本（有 OSM 就用 OSM，没有就随便挑一个非基础名）
    osm_name = next((v["sourceName"] for v in mem if "OSM" in (v.get("short") or "")), "")
    if not osm_name:
        osm_name = next((v["sourceName"] for v in mem
                         if v.get("sourceName") and v["sourceName"] != LINE), "")
    if osm_name:
        set_default(LINE, osm_name)
        two = get("/api/line", q=LINE)
        ck(two.get("name") == osm_name, "设默认后搜基础名给默认那份", two.get("name"))
        ck((two.get("group") or {}).get("effectiveWhy") == "default", "effectiveWhy = default")

        # 校对下拉从 2026-11-11 起只认整理台确认的线，跟「默认版本」无关了
        # （见 confvia.py）：基础名在确认集里才出现，且只有一条正名、不带后缀。
        rows = [x for x in (get("/api/curate/lineNames").get("items") or [])
                if (x.get("name") or "").startswith(LINE)]
        ck(len(rows) <= 1, "校对下拉里同一条线最多一条", [r.get("name") for r in rows])
        ck(all("（" not in (r.get("name") or "") for r in rows),
           "校对下拉不带（来源）后缀", [r.get("name") for r in rows])

        # 选经由的站序仍按默认那份走（这条没变，别被上面的口径改动带跑）
        st = get("/api/line/stops", q=LINE)
        ck(st.get("count", 0) > 0, "选经由仍能取到站序（按默认那份）", st.get("count"))

    # 4) 取消默认 → 回到底表
    set_default(LINE, "")
    three = get("/api/line", q=LINE)
    ck("底表" in (three.get("name") or ""), "取消默认：回到按底表取数", three.get("name"))

    # 5) 乱填版本名要报 400
    try:
        post("/api/line/default", {"name": LINE, "pick": "这条线不存在"})
        ck(False, "乱填版本名应该 400")
    except urllib.error.HTTPError as e:
        ck(e.code == 400, "乱填版本名报 400", e.code)

    if _fail:
        print("\n失败 %d 项" % _fail)
        return 1
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        cleanup()
    sys.exit(rc)
