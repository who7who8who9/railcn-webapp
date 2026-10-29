# -*- coding: utf-8 -*-
"""
校对页「经由以最后校正的数据和车次绑定」+「同起讫点车次勾选同步」的回归。

要验证的几件事：
  1. 自动推断的经由只是算出来的，要人工确认（或点「确认自动结果并入库」）才进正式库
  2. 校正和车次号绑定，并记下「依据哪一天的站表」；站表变了要能提示
  3. 「同类沿用」已经取消：同起讫点的车次不会再自动套用别人的经由
  4. 只能人工勾选同步；同步过去的每趟各存一条自己的校正（source=sync）
  5. 同步时区间按对方的停站重新落站（对方少停的站并进相邻段）

自带清理：测试用到的校正跑完全部撤销。
用法：python scripts/test_curate_sync.py [来源车次 目标车次]
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.request

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8686"
SHOT = os.path.join(tempfile.gettempdir(), "railcn-curate")
DOC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs")
SRC = sys.argv[1] if len(sys.argv) > 1 else ""
DST = sys.argv[2] if len(sys.argv) > 2 else ""


def get(path):
    return json.load(urllib.request.urlopen(BASE + path, timeout=90))


def post(path, body):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=180))


def clear(code):
    post("/api/override", {"code": code, "via": [], "totalKm": None, "note": ""})


def find_pair():
    """找一组「同起讫点、自动推断整趟认全」的车次：来源 + 一个没校正过的同伴"""
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "webapp"))
    import railcore                                    # noqa: E402

    ovs = railcore._override_map()
    with railcore.db() as conn:
        groups = conn.execute(
            """SELECT from_sta, to_sta, COUNT(*) n FROM curate
                WHERE via_source='auto' AND via_cov >= 100
                  AND from_sta IS NOT NULL AND to_sta IS NOT NULL
                GROUP BY from_sta, to_sta HAVING n >= 2
                ORDER BY n DESC LIMIT 40""").fetchall()
        for g in groups:
            mem = conn.execute(
                "SELECT code FROM curate WHERE from_sta=? AND to_sta=? "
                "AND via_source='auto' AND via_cov >= 100",
                (g["from_sta"], g["to_sta"])).fetchall()
            codes = [m["code"] for m in mem]
            free = [c for c in codes if not (ovs.get(c) or {}).get("via")]
            if len(free) >= 2:
                return free[0], free[1], g["from_sta"], g["to_sta"]
    return None


def check_api(src, dst):
    print("== 接口")
    d = get("/api/curate/detail?code=" + src)
    assert d["exists"], f"{src} 在本地库里没有数据"
    assert "classVia" not in d and "classCount" not in d, "同类沿用的字段还在"
    mates = (d.get("odMates") or {}).get("items") or []
    print(f"  {src} {d['from']}→{d['to']} 自动覆盖 {d['auto']['viaCoverage']}%，"
          f"同起讫点 {len(mates)} 趟")
    assert mates, "没有列出同起讫点的车次"
    codes = [m["code"] for m in mates]
    if dst not in codes:                       # 传进来的车次不在这组里就换一个
        free = [m["code"] for m in mates if not m["hasOverride"]]
        assert free, "同起讫点里没有可同步的车次"
        dst = free[0]
    assert dst != src, "来源和目标是同一趟"
    print(f"  同步目标：{dst}")

    # 1) 自动结果还没入库：payload 里是 auto、viaConfirmed=False
    p = get(f"/api/route?code={src}&date={d['date']}")
    assert p["viaSource"] == "auto" and p["viaConfirmed"] is False, "自动结果不该算已校正"
    print(f"  校正前 payload：viaSource={p['viaSource']} confirmed={p['viaConfirmed']}")

    # 2) 确认为人工 + 勾选同步（等价于点「确认自动结果并入库」并勾了同伴）
    via = [{"line": s["line"], "from": s["from"], "to": s["to"]} for s in p["viaSegs"]]
    assert via, "自动结果没有可确认的段"
    r = post("/api/override", {"code": src, "date": d["date"], "via": via,
                               "note": "回归：确认自动结果",
                               "syncCodes": [dst], "syncOverwrite": True})
    assert r.get("ok"), r
    assert r.get("basedDate") == d["date"], "没记下校正依据的日期"
    synced = r.get("synced") or []
    print(f"  保存 → {len(r['via'])} 段入库，同步 {[(x['code'], x['ok'], x.get('reason')) for x in synced]}")
    assert synced and synced[0]["ok"], f"同步失败：{synced}"
    assert synced[0].get("srcCode") == src, "同步没记来源车次"

    # 3) 目标车次自己也有了一条校正（和它的车次号绑定）
    m = get("/api/curate/mate?code=" + dst)
    ov = m.get("override") or {}
    assert ov.get("via"), f"{dst} 没有自己的校正"
    assert ov.get("source") == "sync", f"{dst} 的校正来源不是 sync：{ov}"
    pm = get(f"/api/route?code={dst}&date={m['date']}")
    assert pm["viaSource"] == "manual" and pm["viaConfirmed"], "同步后目标车次没走人工经由"
    segs = [(s["line"], s["from"], s["to"]) for s in pm["viaSegs"]]
    print(f"  {dst} 入库：source={ov['source']} 依据 {ov.get('basedDate')} · {segs}")

    # 4) 区间按对方的停站落站：首末段必须正好是它的首末站
    assert segs[0][1] == m["from"] and segs[-1][2] == m["to"], "同步后的区间没覆盖对方全程"
    assert len(m["stops"]) == len(pm["stops"]), "停站数被改了"

    # 5) 「同类沿用」确实没了：同组第三趟车仍是 auto，没被悄悄套用
    others = [x["code"] for x in get("/api/curate/detail?code=" + src)["odMates"]["items"]
              if x["code"] not in (src, dst) and not x["hasOverride"]]
    if others:
        po = get("/api/route?code=%s&date=%s" % (others[0], d["date"]))
        assert po["viaSource"] == "auto", f"{others[0]} 自动沿用了别人的校正"
        print(f"  同组第三趟 {others[0]} 仍是 auto（没有自动沿用）")

    # 6) 清单里能按来源筛出来
    lst = get("/api/curate/list?filter=synced&limit=200")
    hit = [x["code"] for x in lst["items"] if x["code"] == dst]
    print(f"  「同步入库」筛选：total={lst['total']}，本组 {dst} 在不在 {bool(hit)}")
    return dst


def check_ui(src, dst):
    errs = []
    os.makedirs(SHOT, exist_ok=True)
    os.makedirs(DOC, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--no-proxy-server"])
        pg = b.new_context(viewport={"width": 1500, "height": 980}).new_page()
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("dialog", lambda d: d.accept())
        pg.goto(BASE + "/", wait_until="load")
        pg.wait_for_timeout(1200)
        pg.click('[data-tab="curate"]')
        pg.wait_for_selector("#cuList .cu-row, #cuList .empty", timeout=30000)
        pg.evaluate("code => cuOpen(code)", src)
        pg.wait_for_selector("#ceMates", timeout=30000)
        pg.wait_for_timeout(700)

        assert pg.query_selector("#ceProp") is None, "「同类 N 趟一并沿用」复选框还在"
        rows = pg.query_selector_all("#ceMates .od-row")
        print(f"  编辑器里同起讫点 {len(rows)} 行")
        assert rows, "没渲染出同起讫点的车次"

        # 勾一个同伴 → 保存按钮旁计数出现
        box = pg.query_selector('#ceMates .od-row[data-code="%s"] input[type=checkbox]' % dst)
        assert box, f"列表里没有 {dst}"
        box.check()
        pg.wait_for_timeout(200)
        cnt = pg.inner_text("#ceSyncCount")
        print("  勾选后计数：" + cnt.replace("\n", " "))
        assert dst in cnt or "1 趟" in cnt, "勾选后没显示同步条数"

        # 停站详情
        pg.query_selector('#ceMates .od-row[data-code="%s"] .od-more' % dst).click()
        pg.wait_for_timeout(900)
        det = pg.query_selector(f'.od-detail[data-for="{dst}"]')
        assert det and not det.get_attribute("class").count("hidden"), "停站详情没展开"
        nst = pg.eval_on_selector_all(f'.od-detail[data-for="{dst}"] .od-st', "els => els.length")
        print(f"  停站详情 {nst} 行")
        assert nst >= 2, "停站详情没内容"
        pg.screenshot(path=os.path.join(SHOT, "cu-sync.png"))
        pg.query_selector("#ceMatesCard").screenshot(path=os.path.join(DOC, "curate-od.png"))

        # 确认自动结果并入库（此时已勾选同伴，会一起同步）
        accept = pg.query_selector("#ceAccept")
        assert accept and not accept.is_disabled(), "「确认自动结果并入库」不可用"
        accept.click()
        pg.wait_for_function(
            "() => (document.querySelector('#ceMsg')||{}).textContent && "
            "document.querySelector('#ceMsg').textContent.indexOf('已保存') >= 0", timeout=60000)
        msg = pg.inner_text("#ceMsg")
        print("  保存提示：" + msg.replace("\n", " ")[:120])
        assert "已同步到" in msg, "保存提示里没说同步了几趟"
        assert not errs, "页面 JS 报错：" + "; ".join(errs)
        print("  页面无 JS 报错")
        b.close()


def check_stale():
    """校正依据的站表变了（调图加站）要能提示重校"""
    print("== 站表变化提示")
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "webapp"))
    import railcore                                    # noqa: E402

    with railcore.db() as conn:
        row = conn.execute(
            """SELECT d.code, MIN(d.date) old, MAX(d.date) new, COUNT(DISTINCT t.n_stops) k
                 FROM route_day d JOIN route_tpl t ON t.id = d.tpl_id
                GROUP BY d.code HAVING k > 1 AND d.code NOT IN (
                    SELECT code FROM train_override) LIMIT 1""").fetchone()
    if not row:
        print("  库里没有多个站表版本的车次，跳过")
        return
    code, old = row["code"], row["old"]
    try:
        post("/api/override", {"code": code, "date": old,
                               "via": [{"line": "京沪线", "from": "甲", "to": "乙"}]})
        d = get("/api/curate/detail?code=" + code)
        st = d.get("overrideStale") or {}
        print(f"  {code} 按 {old} 的站表校正 → changed={st.get('changed')} "
              f"少 {len(st.get('missing') or [])} 站 / 多 {len(st.get('added') or [])} 站")
        assert st.get("changed"), "站表变了没提示"
        assert st.get("basedDate") == old, "没记下校正依据的日期"
    finally:
        clear(code)


def main():
    if SRC:
        src, dst = SRC, DST
    else:
        picked = find_pair()
        assert picked, "库里找不到「同起讫点 + 自动认全」的车次组"
        src, dst, f, t = picked
        print(f"样本：{src} / {dst}（{f} → {t}）")
    try:
        dst = check_api(src, dst)
        check_stale()
        # 前面 API 已经写过校正，跑 UI 前先清掉，让页面回到「自动待确认」状态
        clear(src)
        clear(dst)
        check_ui(src, dst)
    finally:
        for c in (src, dst):
            try:
                clear(c)
            except Exception:                          # noqa: BLE001
                pass
        print("  测试校正已撤销")
    print(f"全部通过（截图 {SHOT}/cu-sync.png、docs/curate-od.png）")


if __name__ == "__main__":
    main()
