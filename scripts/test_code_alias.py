"""回归：同一趟车中途换向换了车次号，要显示成「4355/8」

12306 的经停站数据里每站都带 station_train_code，换向站前后会变号（4355 → 4358），
同一趟车在车次清单里也就出现两行（4355、4358，train_no 相同、起讫与站数相同）。
要求：
  · 车次详情写成「4355/8」，标出在哪一站换号、从哪个号换成哪个号
  · 车次列表里这类两行合成一行，不再当成两趟车
  · 校对页每行也都挂同一个写法

用法：python scripts/test_code_alias.py
"""
import json
import os
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webapp"))
import railcore  # noqa: E402

BASE = "http://127.0.0.1:8686"


def get(path):
    return json.load(urllib.request.urlopen(BASE + path, timeout=60))


def q(s):
    return urllib.parse.quote(s)


def check_label():
    cases = [
        (["4355", "4358"], "4355/8"),
        (["4358", "4355"], "4355/8"),
        (["K545", "K548"], "K545/8"),
        (["D7931", "D7934"], "D7931/4"),
        (["G1234", "G5678"], "G1234/G5678"),     # 差得多就写全，不硬缩写
        (["4355"], "4355"),
        ([], ""),
    ]
    for codes, want in cases:
        got = railcore.code_label(codes)
        print(f"  {codes} → {got}")
        assert got == want, f"写法不对：{codes} → {got}，应为 {want}"


def pick_pair():
    """从库里找一趟「有经停站缓存、有两个车次号」的车

    先按 (train_no, date) 找多号组（快），再逐个看有没有经停站缓存 —— 别把 trains
    和 routes 整表 JOIN 起来，那会扫爆。
    """
    with railcore.db() as conn:
        groups = conn.execute(
            """SELECT train_no, date, GROUP_CONCAT(DISTINCT code) codes
                 FROM trains GROUP BY train_no, date
                HAVING COUNT(DISTINCT code) > 1
                ORDER BY date DESC LIMIT 40""").fetchall()
        for g in groups:
            hit = conn.execute(
                "SELECT 1 FROM routes WHERE train_no=? AND date=? AND raw IS NOT NULL",
                (g["train_no"], g["date"])).fetchone()
            if hit:
                codes = sorted((g["codes"] or "").split(","))
                return {"train_no": g["train_no"], "date": g["date"], "codes": codes}
    return None


def check_detail(pair):
    """两个车次号都要能打开同一份站表，标签是合并写法，且标出换号站"""
    label = railcore.code_label(pair["codes"])
    print(f"  车次 {pair['codes']}（{pair['date']}）应为「{label}」")
    switches = None
    for code in pair["codes"]:
        d = get(f"/api/route?code={q(code)}&date={pair['date']}&cache=1")
        print(f"  查 {code} → codeLabel={d.get('codeLabel')} codes={d.get('codes')} "
              f"换号={[(w['at'], w['from'], w['to']) for w in d.get('codeSwitches') or []]}")
        assert set(d.get("codes") or []) == set(pair["codes"]), f"{code} 的 codes 不全"
        assert d.get("codeLabel") == label, f"{code} 的写法不是 {label}"
        assert d.get("codeSwitches"), f"{code} 没标出换号站"
        # 站表里每站都要带本次车次号，换号站前后不同
        tc = [s.get("trainCode") for s in d["stops"]]
        assert all(tc), f"{code} 有站缺 trainCode"
        assert len(set(tc)) == len(pair["codes"]), f"{code} 的站表里车次号没变过"
        switches = d["codeSwitches"]
        if code == pair["codes"][0]:
            first = d["stops"]
    # 换号那一站：换号记录里的 i 对应站序号
    sw = switches[0]
    st = next(s for s in first if s["i"] == sw["i"])
    print(f"  换号站：第 {sw['i']} 站 {st['name']}，{sw['from']} → {sw['to']}")
    assert st["name"] == sw["at"], "换号站名对不上"
    assert st["trainCode"] == sw["to"], "换号站的 trainCode 应为换后的号"


def check_list(pair):
    """列表里同一趟车只留一行，写合并标签"""
    label = railcore.code_label(pair["codes"])
    for code in pair["codes"]:
        items = get(f"/api/trains?q={q(code)}&limit=20")["items"]
        hit = [it for it in items if set(it.get("codes") or []) == set(pair["codes"])]
        print(f"  搜「{code}」→ 命中 {[(it['code'], it.get('codeLabel')) for it in hit]}")
        assert len(hit) == 1, f"搜「{code}」没合成一行（{len(hit)} 行）"
        assert hit[0]["codeLabel"] == label, f"列表写法不对：{hit[0]['codeLabel']}"
        # 同一 train_no 不该在结果里出现两次
        same = [it for it in items if it.get("train_no") == pair["train_no"]]
        assert len(same) == 1, f"同一趟车在列表里出现了 {len(same)} 行"


def check_ui(pair):
    from playwright.sync_api import sync_playwright
    label = railcore.code_label(pair["codes"])
    code = pair["codes"][-1]          # 用「换号后」的那个号打开，看看会不会认错
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--no-proxy-server"])
        pg = b.new_context(viewport={"width": 1440, "height": 900}).new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(BASE + "/", wait_until="load")
        pg.wait_for_timeout(1500)
        pg.fill("#date", pair["date"])
        pg.fill("#q", code)
        pg.click("#btnSearch")
        pg.wait_for_timeout(2500)
        head = pg.inner_text("#detail .dhead .l1").replace("\n", " ")
        l2 = pg.inner_text("#detail .dhead .l2").replace("\n", " ")
        print(f"  头部：{head} || {l2}")
        assert label in head, f"头部没写 {label}"
        assert "中途换号" in head, "头部没有「中途换号」标记"
        sw = pg.query_selector("#detail .codesw")
        assert sw, "站序表里没标换号站"
        print(f"  站序表换号标记：{sw.inner_text()}（title={sw.get_attribute('title')}）")
        assert "→" in sw.inner_text(), "换号标记写得不像「4355→4358」"
        # 列表：直接把接口结果渲染一遍，看同一趟车是不是只剩一行、写着合并号
        items = get(f"/api/trains?q={q(code)}&limit=10")["items"]
        pg.evaluate("(items) => renderTrainList(items, '回归测试')", items)
        pg.wait_for_timeout(300)
        labels = [x.inner_text().strip() for x in pg.query_selector_all("#trainList .row .tcode")]
        print(f"  列表车次号：{labels}")
        assert label in labels, f"列表里没有 {label}"
        for c in pair["codes"]:
            if c != label.split("/")[0]:
                assert c not in labels, f"列表里还有单独的「{c}」那行"
        same = pg.query_selector_all(f'#trainList .row[data-code="{label.split("/")[0]}"]')
        print(f"  列表行数（同一趟车）：{len(same)} / 共 {len(labels)} 行")
        assert len(same) == 1, "同一趟车在列表里画了不止一行"
        assert not errs, f"页面报错：{errs}"
        b.close()


def check_curate():
    d = get("/api/curate/list?filter=all&limit=60")
    hits = [it for it in d["items"] if it.get("codeLabel") and it["codeLabel"] != it["code"]]
    print(f"  校对清单 {len(d['items'])} 行，带合并写法的 {len(hits)} 行"
          f"：{[(x['code'], x['codeLabel']) for x in hits[:4]]}")
    assert hits, "校对清单里没有一行带合并写法（库里有换号车次，应该有）"
    for it in hits[:5]:
        assert it["code"] in it["codes"], "codes 里没有自己"
    one = hits[0]
    det = get("/api/curate/detail?code=" + q(one["code"]))
    print(f"  编辑器 {det['code']} → {det.get('codeLabel')}")
    assert det.get("codeLabel") == one["codeLabel"], "编辑器标题的写法不一致"


def main():
    print("== 写法规则")
    check_label()
    pair = pick_pair()
    if not pair:
        print("跳过：库里没有「两个车次号 + 有经停站缓存」的车次")
        return
    print("== 车次详情")
    check_detail(pair)
    print("== 车次列表")
    check_list(pair)
    print("== 页面")
    check_ui(pair)
    print("== 校对页")
    check_curate()
    print("全部通过")


if __name__ == "__main__":
    main()
