# -*- coding: utf-8 -*-
"""走向编辑器「调用现有线路」的多段几何回归

背景：编辑器以前优先读 ld_geom.spine_gz（抽稀 + 补断口主轴），又把主站的
多段硬串成一条 chain，导致滨绥线 902 km 只剩 13 km、沪昆线出现 274 km
假直线。现在统一走 lineosm.geom()（主站 /api/line/osm 的同一入口），
载入 / 保存都保留多段，段间绝不连线。

跑法： python scripts/test_osmedit_multipart.py
（正式库只读；写盘验证用临时线名并清理）
"""
import json
import math
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "webapp"))

import confirmed            # noqa: E402
import lineosm              # noqa: E402
import osmedit              # noqa: E402

PASS, FAIL = [], []


def ck(label, cond, extra=""):
    (PASS if cond else FAIL).append(label)
    print(("  ok   " if cond else "  FAIL ") + label +
          ("" if cond else "  ← " + str(extra)[:220]))


def hav(a, b):
    r = math.radians
    dlon, dlat = r(b[0] - a[0]), r(b[1] - a[1])
    h = (math.sin(dlat / 2) ** 2 + math.cos(r(a[1])) * math.cos(r(b[1]))
         * math.sin(dlon / 2) ** 2)
    return 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(h)))


def shape(parts):
    pts = [p for seg in parts for p in seg]
    if not pts:
        return (0, 0, 0, (0, 0, 0, 0), 0)
    jumps = [hav(s[i - 1], s[i]) for s in parts for i in range(1, len(s))]
    return (len(parts), len(pts),
            sum(hav(s[i - 1], s[i]) for s in parts for i in range(1, len(s))),
            (min(p[0] for p in pts), min(p[1] for p in pts),
             max(p[0] for p in pts), max(p[1] for p in pts)),
            max(jumps) if jumps else 0)


def http(path):
    with urllib.request.urlopen("http://127.0.0.1:8686" + path, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


TEST = "test_多段几何回归线"


def cleanup():
    for db, table in ((osmedit.RAIL_DB, "line_geom_edit"),
                      (osmedit.RAIL_DB, "line_geom")):
        try:
            c = sqlite3.connect(db)
            c.execute("DELETE FROM %s WHERE name=?" % table, (TEST,))
            c.commit()
            c.close()
        except Exception:
            pass


def test_shared_entry():
    print("[1] 主站与编辑器共用同一个几何入口")
    srv = open(os.path.join(ROOT, "webapp", "server.py"), encoding="utf-8").read()
    los = open(os.path.join(ROOT, "webapp", "lineosm.py"), encoding="utf-8").read()
    oe = open(os.path.join(ROOT, "webapp", "osmedit.py"), encoding="utf-8").read()
    ck("server.py /api/line/osm 调 lineosm.geom", "lineosm.geom(nm" in srv)
    ck("server.py 不再自己写一套解析", "geoimport.geom(nm)" not in srv)
    ck("lineosm.py 保留确认分支", "confirmed.geom(nm)" in los)
    ck("lineosm.py 保留基本线路 / route / GeoJSON / OSM 顺序",
       all(k in los for k in ("linedesk.merged_geom", "linebase.route_geom",
                              "geoimport.geom(nm)", "railgeom.geom(nm",
                              "lineseg.borrow_geometry")))
    ck("osmedit.load_geom 走 lineosm.geom", "lineosm.geom(name" in oe)
    ck("osmedit.load_geom 不再优先读 spine_gz",
       "_cand_ldspine(name)" not in oe.split("def load_geom(")[-1])
    ck("osmedit.load_geom 不再硬串单链",
       "_chain_to(parts" not in oe.split("def load_geom(")[-1])
    a = lineosm.geom("京沪线", confirmed_only=True) or {}
    b = confirmed.geom("京沪线", with_parts=True) or {}
    ck("lineosm confirmed=True 与 confirmed.geom 同一份 parts",
       bool(a.get("parts")) and shape(a["parts"])[:2] == shape(b.get("parts") or [])[:2],
       (shape(a.get("parts") or [])[:2], shape(b.get("parts") or [])[:2]))


def test_real_shape():
    print("[2] 正式库：编辑器载入的形状 == 主站展示")
    names = [x["name"] for x in confirmed.recent()]
    ck("确认线路非空", len(names) >= 20, len(names))
    bad_part, bad_box, bad_km, bad_jump = [], [], [], []
    for nm in names:
        main = confirmed.geom(nm, with_parts=True) or {}
        ed = osmedit.load_geom(nm, 600) or {}
        mp, ep = main.get("parts") or [], ed.get("parts") or []
        if not ep:
            bad_part.append((nm, "empty"))
            continue
        if ed.get("source") != "edit":
            if len(ep) != len(mp):
                bad_part.append((nm, len(mp), len(ep)))
        m_part, m_pt, m_km, m_box, m_jump = shape(mp)
        e_part, e_pt, e_km, e_box, e_jump = shape(ep)
        if any(abs(a - b) > 0.02 for a, b in zip(m_box, e_box)):
            bad_box.append((nm, [round(x, 2) for x in m_box],
                            [round(x, 2) for x in e_box]))
        if m_km > 0 and abs(e_km - m_km) / m_km > 0.08:
            bad_km.append((nm, round(m_km), round(e_km)))
        if e_jump > 40:
            bad_jump.append((nm, round(m_jump), round(e_jump)))
    ck("★ 每条段数与主站一致（草稿除外）", not bad_part, bad_part[:4])
    ck("★ 每条包围盒四至与主站一致（≤0.02°）", not bad_box, bad_box[:4])
    ck("★ 每条里程与主站相差 ≤8%（抽稀损耗）", not bad_km, bad_km[:4])
    ck("★ 没有 >40 km 的段内假直线", not bad_jump, bad_jump[:4])
    for nm in ("滨绥线", "滨北线", "济南线", "京广高铁", "沪昆线"):
        main = confirmed.geom(nm, with_parts=True) or {}
        ed = osmedit.load_geom(nm, 600) or {}
        m_km = shape(main.get("parts") or [])[2]
        e_km = shape(ed.get("parts") or [])[2]
        ck("%s 不再被截断（主站 %.0f km / 编辑器 %.0f km）" % (nm, m_km, e_km),
           m_km > 0 and e_km > m_km * 0.9, round(e_km))
    g = osmedit.load_geom("滨绥线", 600) or {}
    ck("滨绥线返回多段而非单链", (g.get("nPart") or 0) > 1, g.get("nPart"))
    ck("load_geom 回报 nPart / nPt",
       all(k in g for k in ("nPart", "nPt"))
       and g["nPart"] == len(g["parts"])
       and g["nPt"] == sum(len(s) for s in g["parts"]))


def test_helpers():
    print("[3] _as_segs / _thin_segs / _cap_jumps")
    flat = [[116.0, 39.0], [116.1, 39.1], [116.2, 39.2]]
    ck("扁平单链 → 1 段", len(osmedit._as_segs(flat)) == 1)
    multi = [flat, [[117.0, 40.0], [117.1, 40.1]]]
    ck("多段 → 段数不变", len(osmedit._as_segs(multi)) == 2)
    ck("多段点序不变", osmedit._as_segs(multi)[0][0] == [116.0, 39.0])
    rev = [[39.0, 116.0], [39.1, 116.1], [39.2, 116.2]]
    ck("历史 (lat,lon) 反序被纠正",
       osmedit._as_segs([rev])[0][0] == [116.0, 39.0],
       osmedit._as_segs([rev])[0][0])
    segs = [[[116.0 + i * 0.001, 39.0 + i * 0.0004] for i in range(60)]
            for _ in range(6)]
    thin = osmedit._thin_segs(segs, 40)
    ck("抽稀后段数守恒", len(thin) == 6, len(thin))
    ck("抽稀后点数下降", sum(len(s) for s in thin) < sum(len(s) for s in segs))
    ck("抽稀不跨段连线", all(len(s) >= 2 for s in thin))
    orig = [[116.0 + i * 0.01, 39.0] for i in range(101)]
    capped = osmedit._cap_jumps(orig, [orig[0], orig[-1]])
    gap = max(hav(capped[i - 1], capped[i]) for i in range(1, len(capped)))
    ck("_cap_jumps 把 111 km 假直线补到 ≤25 km", gap <= 26 and len(capped) > 2,
       (round(gap, 1), len(capped)))


def test_save_roundtrip():
    print("[4] 多段写入 / 读回（临时线名，跑完清理）")
    cleanup()
    parts = [[[116.0, 39.0], [116.1, 39.05], [116.2, 39.1]],
             [[117.0, 40.0], [117.1, 40.1]],
             [[118.0, 41.0], [118.1, 41.1], [118.2, 41.2]]]
    try:
        r = osmedit.save_geom(TEST, parts, src="osmedit")
        ck("save_geom 成功且回报 3 段", r.get("ok") and r.get("nPart") == 3,
           r.get("nPart"))
        c = sqlite3.connect(osmedit.RAIL_DB)
        row = c.execute("SELECT parts_gz, n_part FROM line_geom_edit WHERE name=?",
                        (TEST,)).fetchone()
        c.close()
        stored = osmedit._ungz(row[0]) if row else None
        ck("line_geom_edit.parts_gz 存的是 3 段（不是扁平 chain）",
           bool(stored) and len(stored) == 3
           and isinstance(stored[0][0], list), len(stored) if stored else 0)
        ck("line_geom_edit.n_part=3", bool(row) and row[1] == 3, row and row[1])
        g = osmedit.load_geom(TEST, 600) or {}
        ck("load_geom 读回仍是 3 段", g.get("nPart") == 3, g.get("nPart"))
        ck("读回点数与写入一致",
           g.get("nPt") == sum(len(s) for s in parts), g.get("nPt"))
        ck("totalKm 不含跨段直线",
           abs(g.get("totalKm", 0) - sum(osmedit._poly_km(s) for s in parts)) < 0.2,
           g.get("totalKm"))
        # apply 到一条临时 line_geom 行，验证 n_part 也跟着走
        c = sqlite3.connect(osmedit.RAIL_DB)
        c.execute(
            "INSERT OR REPLACE INTO line_geom(name,file,total_km,n_part,n_pt,"
            "props,parts_gz,created_at,spine_gz) VALUES(?,?,?,?,?,?,?,?,?)",
            (TEST, "test", 1.0, 1, 2, "{}", osmedit._gz(parts[0]), time.time(), None))
        c.commit()
        c.close()
        r2 = osmedit.save_geom(TEST, parts, apply=True)
        ck("apply 成功写回", r2.get("appliedAny") is True, str(r2)[:120])
        c = sqlite3.connect(osmedit.RAIL_DB)
        n = c.execute("SELECT n_part FROM line_geom WHERE name=?",
                      (TEST,)).fetchone()
        c.close()
        ck("line_geom.n_part 也写成 3", bool(n) and n[0] == 3, n)
    finally:
        cleanup()
    ck("清理后临时线不在草稿表里", osmedit.load_geom(TEST, 600) is None
       or (osmedit.load_geom(TEST, 600) or {}).get("hasEdit") is False)


def test_frontend():
    print("[5] 前端：按段绘制、断点维护、多段保存")
    js = open(os.path.join(ROOT, "webapp", "static", "osmedit.js"),
              encoding="utf-8").read()
    html = open(os.path.join(ROOT, "webapp", "static", "osmedit.html"),
                encoding="utf-8").read()
    ck("S 里有 breaks", "breaks: []" in js)
    ck("有 splitParts / isBreak / shiftBreaks",
       all(k in js for k in ("function splitParts()", "function isBreak(i)",
                             "function shiftBreaks(")))
    ck("Canvas 按段画（断点另起一笔）", "splitParts().forEach(seg =>" in js)
    ck("MultiPolyline paths 是二维多段",
       "return splitParts().map(seg => seg.map(p => toTMap(p[1], p[0])));" in js)
    ck("里程统计跳过断点", "if (!isBreak(i)) s += haversineKm" in js)
    ck("插点跳过跨断点假直线", "if (isBreak(i)) continue;          // 断点两侧没有连线" in js)
    ck("删点维护断点索引", "b > i ? b - 1 : b" in js)
    ck("撤销同时保存 / 恢复断点",
       "breaks: S.breaks.slice()" in js and "st.breaks || []" in js)
    ck("载入从 g.parts 重建断点", "S.breaks.push(S.pts.length)" in js)
    ck("保存 / 抓站 / 建线都发多段",
       js.count("splitParts()") >= 4 and "parts: [S.pts]" not in js,
       js.count("splitParts()"))
    ck("换线 / 新建 / 清空都清断点",
       js.count("S.breaks = []") >= 3, js.count("S.breaks = []"))
    ck("状态条显示段数", "S.breaks.length + 1" in js)
    ck("抽稀默认 600 点（多段留得下）",
       'value="600"' in html and "|| 600" in js)
    ck("帮助文案说明段间不连线", "段与段之间不连线" in html)
    ck("确认线路提示改成载入主站多段",
       "载入的是主站正在显示的那份多段走向" in js)
    ck("不再声明多段会被单条折线覆盖",
       "多段走向会被你画的这一条折线覆盖" not in js)


def test_http():
    print("[6] HTTP：编辑器载入与主站 /api/line/osm 同一份 parts")
    try:
        e = http("/api/osmedit/geom?name=" + urllib.parse.quote("京沪线") + "&n=600")
        m = http("/api/line/osm?confirmed=1&q=" + urllib.parse.quote("京沪线"))
        n = http("/api/line/osm?q=" + urllib.parse.quote("京沪线"))
    except (urllib.error.URLError, OSError) as exc:
        print("  --   服务没起，跳过 HTTP 检查（%s）" % str(exc)[:60])
        return
    ck("编辑器返回多段", (e.get("nPart") or 0) > 1 and len(e.get("parts") or []) > 1,
       e.get("nPart"))
    ck("确认通道段数 == 编辑器段数",
       len(m.get("parts") or []) == len(e.get("parts") or []),
       (len(m.get("parts") or []), len(e.get("parts") or [])))
    ck("两者包围盒一致",
       all(abs(a - b) < 0.02 for a, b in
           zip(shape(m.get("parts") or [])[3], shape(e.get("parts") or [])[3])))
    ck("非确认通道（搬家后）仍能取到几何", bool(n.get("parts")),
       str(n)[:100])
    ck("最大段内跳变 ≤40 km", shape(e.get("parts") or [])[4] <= 40,
       round(shape(e.get("parts") or [])[4], 1))


def main():
    test_shared_entry()
    test_real_shape()
    test_helpers()
    test_save_roundtrip()
    test_frontend()
    test_http()
    print("\n通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        for x in FAIL:
            print("  × " + x)
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
