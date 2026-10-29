"""回归：线路库里没有、只有资料库有站序的线，改站序要能存下来

侯西线就是这种：底表 417 条里没有它，站序只在「线路资料库」（42 站），
走向只有 OSM 有。线路页能打开、能点「改站序」，但以前保存直接报
「线路库里没有这条线：侯西铁路」—— 在弹窗里排了半天站序全丢。

现在的约定：
  1. 搜资料库的 title（侯西铁路）拼出来的线，线名要收敛到资料库的 name（侯西线），
     不能同一条线在库里长出两个名字；
  2. 保存站序时如果线路库没有这条线，先按规范名整条收进线路库（自建），
     再把用户改的站序落上去，返回里 adopted=True 说明发生过收编；
  3. 收进去之后就是普通自建线路，再改站序走覆盖；删掉它要能回到资料库原样。

用法：python scripts/test_loose_line.py      （需要服务在 8686 跑着）
"""
import json
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8686"
LINE = "侯西线"          # 资料库的线名（规范名）
ALIAS = "侯西铁路"       # 资料库的 title，也是用户会搜的词

_fail = 0


def ck(ok, msg):
    global _fail
    print(("  ok   " if ok else "  FAIL ") + msg)
    if not ok:
        _fail += 1


def get(path):
    return json.load(urllib.request.urlopen(BASE + path, timeout=90))


def post(path, body):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    return json.load(urllib.request.urlopen(req, timeout=120))


def q(name):
    return urllib.parse.quote(name)


def line_of(name):
    return get("/api/line?q=" + q(name))


def drop():
    try:
        post("/api/line/delete", {"name": LINE})
    except urllib.error.HTTPError:
        pass


def main():
    print("== 规范名：搜资料库标题，出来的是资料库线名")
    d = line_of(ALIAS)
    ck(d["name"] == LINE, "搜「%s」得到「%s」（不是自己也叫 %s）" % (ALIAS, d["name"], ALIAS))
    ck(d.get("aliasOf") == ALIAS, "aliasOf 记着原始搜索词：%s" % d.get("aliasOf"))
    ck(d.get("composed") is True, "标记为拼出来的线（库外）")
    ck(d["n"] == 42, "资料库 42 站：%s" % d["n"])

    base = [s["name"] for s in d["stations"]]
    if line_of(LINE).get("custom"):
        print("  上一次没收拾干净，先删掉自建的那条")
        drop()

    print("== 保存站序：库外的线自动收进线路库")
    stops = [{"name": base[1]}, {"name": base[0]}] + [{"name": x} for x in base[2:5]]
    ln = post("/api/line/stations", {"name": ALIAS, "stations": stops})["line"]
    ck(ln.get("adopted") is True, "返回 adopted=True，说明是这次收编的")
    ck(ln["name"] == LINE, "收编后的线名是规范名：%s" % ln["name"])
    ck(ln.get("custom") is True, "成了自建线路，之后改站序走覆盖")
    ck([s["name"] for s in ln["stations"]][:2] == [base[1], base[0]],
       "用户排的先后生效了：%s → %s" % (ln["stations"][0]["name"], ln["stations"][1]["name"]))
    ck(len(ln["stations"]) == 5, "只留提交的 5 站：%s" % len(ln["stations"]))

    after = line_of(LINE)
    ck(after.get("custom") is True, "再查已经是自建线路")
    ck(after["n"] == 5, "站数跟保存的一致：%s" % after["n"])

    print("== 再改一次：走覆盖，不再收编")
    stops2 = stops + [{"name": base[5]}]
    ln2 = post("/api/line/stations", {"name": LINE, "stations": stops2})["line"]
    ck(not ln2.get("adopted"), "第二次保存不再报收编")
    ck(ln2["n"] == 6, "站数 6：%s" % ln2["n"])

    print("== 删掉自建线：回到资料库原样")
    drop()
    back = line_of(LINE)
    ck(not back.get("custom"), "不再是自建线路")
    ck(back["n"] == 42, "又回到资料库的 42 站：%s" % back["n"])
    ck([s["name"] for s in back["stations"]][:2] == base[:2], "站序回到资料库顺序")

    print("== 站序来源确实没有的线，仍然要明确报错")
    try:
        post("/api/line/stations", {"name": "根本不存在的测试线", "stations": [
            {"name": base[0]}, {"name": base[1]}]})
        ck(False, "不该成功")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        ck(e.code == 400 and "没有这条线" in body, "报错说清楚了：%s" % body[:80])

    print("全部通过" if not _fail else "有 %d 项没过" % _fail)
    return 1 if _fail else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        drop()
    raise SystemExit(code)
