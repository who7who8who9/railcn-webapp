# -*- coding: utf-8 -*-
"""
把辉睿软件的「中国铁路线路一览」整合进 railcn 线路库。

数据来源：https://tool.huiruisoft.com/train/railway/index.html（先用 fetch_huirui.py 抓）
版权归原站所有，此处只做本地索引与展示，保留来源链接。

做三件事：
  1. line_catalog / line_catalog_stop —— 844 条线路的参数、介绍、逐站里程，全部入库
  2. 与线路库同名/同族的条目，回填 line_catalog.line_name，线路详情页即可显示资料
  3. 线路库没有、而车站坐标齐全的线路，append 成线路库里的新线路（source='huirui'）

用法：
    python scripts/import_huirui.py              # 只出报告
    python scripts/import_huirui.py --apply      # 写库（会先备份 rail.db）
    python scripts/import_huirui.py --apply --add-lines 0.8   # 顺便补线路（坐标覆盖阈值）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from typing import Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "webapp"))

import cnrail                                                    # noqa: E402
import railcore                                                  # noqa: E402  触发自定义线路/资料库的装载

DB = os.path.join(ROOT, "webapp", "data", "rail.db")
SRC = os.path.join(ROOT, "data", "huirui", "lines.json")

HS_RE = re.compile(r"高速|客专|客运专线|城际|高铁")

# 「铁路 / 线 / 高铁 …」这些尾巴不影响线路身份
_TAILS = ("高速铁路", "客运专线", "城际铁路", "高速线", "客专线", "铁路", "高铁", "线")


def core(name: str) -> str:
    s = re.sub(r"\s+", "", name or "")
    changed = True
    while changed:
        changed = False
        for t in _TAILS:
            if len(s) > len(t) and s.endswith(t):
                s = s[: -len(t)]
                changed = True
                break
    return s


def is_hs(name: str) -> bool:
    return bool(HS_RE.search(name or ""))


def pick_meta(meta: Dict[str, str], *labels: str) -> Optional[str]:
    for lb in labels:
        v = meta.get(lb)
        if v:
            return v
    return None


def km_of(s: str) -> Optional[float]:
    if not s:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", s.replace(",", ""))
    return round(float(m.group(0)), 1) if m else None


def build_rec(r: dict) -> dict:
    meta = r.get("meta") or {}
    stops = []
    for i, s in enumerate(r.get("stops") or [], start=1):
        nm = cnrail.station_name(s.get("name") or "")
        if not nm:
            continue
        stops.append({"seq": len(stops) + 1, "name": nm, "km": s.get("km"),
                      "note": s.get("note") or "", "sid": s.get("sid")})
    length = km_of(pick_meta(meta, "线路长度") or "")
    from_to = pick_meta(meta, "起止站点") or ""
    a = b = None
    m = re.match(r"(.+?)\s*-\s*(.+)$", from_to)
    if m:
        a, b = cnrail.station_name(m.group(1)) or None, cnrail.station_name(m.group(2)) or None
    return {
        "id": r["id"], "name": r.get("name") or r.get("title") or "",
        "title": r.get("title") or "", "type": pick_meta(meta, "铁路类型"),
        "level": pick_meta(meta, "铁路等级"), "service": pick_meta(meta, "服务类型"),
        "alias": pick_meta(meta, "别名"), "foreign": pick_meta(meta, "外文名称"),
        "design_speed": pick_meta(meta, "设计速度"), "run_speed": pick_meta(meta, "运营速度"),
        "length_km": length, "open_date": pick_meta(meta, "开通日期"),
        "from_sta": a, "to_sta": b, "intro": (r.get("intro") or "").strip(),
        "url": r.get("url") or "", "stops": stops,
        "n_stop": len(stops), "n_km": sum(1 for s in stops if s["km"] is not None),
    }


def match_lines(recs: List[dict], our: Dict[str, dict]) -> None:
    """给每条 catalog 记录找线路库里的对应线路（就地写 line_name / match_how / score）"""
    our_keys = {n: {cnrail._key(s["name"]) for s in l["stations"] if s.get("name")}
                for n, l in our.items()}
    by_core: Dict[str, List[str]] = {}
    for n in our:
        by_core.setdefault(core(n), []).append(n)

    for rec in recs:
        rec["line_name"] = rec["match_how"] = None
        rec["score"] = 0.0
        names = [x for x in (rec["name"], rec["title"]) if x]
        # 1) 同名
        hit = next((x for x in names if x in our), None)
        if hit:
            rec.update(line_name=hit, match_how="exact", score=1.0)
            continue
        # 2) 去后缀同族（京沪高铁 vs 京沪高速线 不算：核心名不同就不认）
        c = core(rec["name"]) or core(rec["title"] or "")
        cands = [n for n in by_core.get(c, [])
                 if is_hs(n) == ("高速" in (rec["type"] or ""))]
        if len(cands) == 1:
            rec.update(line_name=cands[0], match_how="family", score=0.95)
            continue
        # 3) 站序重叠（只用来给「资料」挂靠，不用于回填里程）
        #    分母必须是「资料库的站数」。原来写 min(两边)，等于要求两边都有站，
        #    于是库内那条小联络线只要两站都落在资料库的站序里，就必然拿 1.0 ——
        #    「侯西线」42 站 288.5 km 被匹配到了 9 km 的「陇海西成联络线」。
        #    再卡一头量级：站数和里程**同时**不到一半，才算「小线冒充大线」
        #    （只卡下界，不加区间上限：资料库那条线本来常常只是整条干线的一段，
        #      「京石高铁」7 站之于「京广高速线」42 站是对的，不能误杀）
        hset = {cnrail._key(s["name"]) for s in rec["stops"]}
        if not hset:
            continue
        km0 = rec.get("length_km") or 0
        best, bs = None, 0.0
        for n, oset in our_keys.items():
            if not oset or is_hs(n) != ("高速" in (rec["type"] or "")):
                continue
            okm = our[n].get("totalKm") or 0
            if (len(oset) < len(hset) * 0.5) and (okm and km0 and okm < km0 * 0.5):
                continue
            v = len(hset & oset) / len(hset)
            if v > bs:
                best, bs = n, v
        if best and bs >= 0.6:
            rec.update(line_name=best, match_how="stops", score=round(bs, 3))


def km_check(rec: dict, our: Dict[str, dict]) -> dict:
    """辉睿里程 vs 档案里程：看两套坐标能不能对上"""
    ln = our.get(rec["line_name"] or "")
    if not ln:
        return {}
    hk = {cnrail._key(s["name"]): s["km"] for s in rec["stops"] if s["km"] is not None}
    pairs = [(s["km"], hk[cnrail._key(s["name"])]) for s in ln["stations"]
             if s.get("km") is not None and cnrail._key(s["name"]) in hk]
    miss = [s for s in ln["stations"] if s.get("km") is None
            and cnrail._key(s["name"]) in hk]
    if not pairs:
        return {"pairs": 0, "fill": len(miss)}
    diffs = sorted(h - o for o, h in pairs)
    med = diffs[len(diffs) // 2]
    spread = max(abs(d) for d in diffs)
    return {"pairs": len(pairs), "fill": len(miss), "median": round(med, 1),
            "spread": round(spread, 1), "agree": spread <= 2.0}


DDL = """
CREATE TABLE IF NOT EXISTS line_catalog (
    id          TEXT PRIMARY KEY,     -- 来源站点的线路编号
    name        TEXT,                 -- 中文名称
    title       TEXT,                 -- 中文全称
    line_name   TEXT,                 -- 对应到线路库里的名字（NULL = 库里没有）
    match_how   TEXT,                 -- exact / family / stops
    match_score REAL,
    type        TEXT, level TEXT, service TEXT, alias TEXT, foreign_name TEXT,
    design_speed TEXT, run_speed TEXT, length_km REAL, open_date TEXT,
    from_sta    TEXT, to_sta TEXT,
    intro       TEXT, url TEXT, n_stop INTEGER, n_km INTEGER, updated_at REAL
);
CREATE TABLE IF NOT EXISTS line_catalog_stop (
    lid   TEXT NOT NULL,
    seq   INTEGER NOT NULL,
    name  TEXT,
    km    REAL,
    note  TEXT,
    sid   TEXT,
    PRIMARY KEY (lid, seq)
);
CREATE INDEX IF NOT EXISTS idx_catalog_line ON line_catalog(line_name);
CREATE INDEX IF NOT EXISTS idx_catalog_stop ON line_catalog_stop(lid, seq);
"""


def apply(recs: List[dict], our: Dict[str, dict], add_lines: float) -> dict:
    bak = DB + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(DB, bak)
    print("备份数据库 →", os.path.basename(bak))

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.executescript(DDL)
    # 老库迁移：自建线路加来源列（manual / huirui）
    cols = {r[1] for r in conn.execute("PRAGMA table_info(line_custom)")}
    if "source" not in cols:
        conn.execute("ALTER TABLE line_custom ADD COLUMN source TEXT DEFAULT 'manual'")

    now = time.time()
    conn.execute("DELETE FROM line_catalog_stop")
    conn.execute("DELETE FROM line_catalog")
    for rec in recs:
        conn.execute(
            """INSERT INTO line_catalog(id,name,title,line_name,match_how,match_score,type,level,
                 service,alias,foreign_name,design_speed,run_speed,length_km,open_date,
                 from_sta,to_sta,intro,url,n_stop,n_km,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rec["id"], rec["name"], rec["title"], rec["line_name"], rec["match_how"],
             rec["score"], rec["type"], rec["level"], rec["service"], rec["alias"],
             rec["foreign"], rec["design_speed"], rec["run_speed"], rec["length_km"],
             rec["open_date"], rec["from_sta"], rec["to_sta"], rec["intro"], rec["url"],
             rec["n_stop"], rec["n_km"], now))
        conn.executemany(
            "INSERT INTO line_catalog_stop(lid,seq,name,km,note,sid) VALUES(?,?,?,?,?,?)",
            [(rec["id"], s["seq"], s["name"], s["km"], s["note"], s["sid"]) for s in rec["stops"]])

    # 线路库没有、但车站坐标齐全的，补成新线路
    added: List[str] = []
    skipped: List[str] = []
    if add_lines > 0:
        for rec in recs:
            if rec["line_name"] or rec["n_stop"] < 4:
                continue
            nm = cnrail.station_name(rec["name"])
            if not nm or cnrail.line(nm):
                continue
            names = [s["name"] for s in rec["stops"]]
            cov = sum(1 for x in names if cnrail.coord(x)) / len(names)
            if cov < add_lines:
                skipped.append(f"{nm}({cov:.0%})")
                continue
            stations = []
            seen_st, last_km = set(), None
            for s in rec["stops"]:
                if s["name"] in seen_st:              # 同名重复站只取第一次
                    continue
                seen_st.add(s["name"])
                km = s["km"]
                if km is not None and last_km is not None and km <= last_km:
                    km = None                          # 里程倒挂的先当不详，别让整条线建不起来
                if km is not None:
                    last_km = km
                stations.append({"name": s["name"], "km": km})
            if len(stations) < 2:
                continue
            conn.execute(
                """INSERT INTO line_custom(name, stations_json, note, created_at, updated_at, source)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(name) DO UPDATE SET stations_json=excluded.stations_json,
                     note=excluded.note, updated_at=excluded.updated_at, source=excluded.source""",
                (nm, json.dumps(stations, ensure_ascii=False),
                 f"来自线路资料库（{rec['type'] or '线路'}）", now, now, "huirui"))
            conn.execute("UPDATE line_catalog SET line_name=?, match_how='added', match_score=? WHERE id=?",
                         (nm, round(cov, 3), rec["id"]))
            rec["line_name"], rec["match_how"], rec["score"] = nm, "added", round(cov, 3)
            added.append(nm)

    conn.commit()
    conn.close()
    return {"backup": bak, "added": added, "skipped": skipped}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--add-lines", type=float, default=0.0,
                    help="坐标覆盖率阈值，达到就补进线路库；0 = 不补")
    args = ap.parse_args()

    cnrail._ensure()
    our = {l["name"]: l for l in cnrail._LINES}
    raw = json.load(open(SRC, encoding="utf-8"))
    recs = [build_rec(r) for r in raw]
    recs = [r for r in recs if r["name"]]
    match_lines(recs, our)

    by_how: Dict[str, int] = {}
    for r in recs:
        by_how[r["match_how"] or "none"] = by_how.get(r["match_how"] or "none", 0) + 1
    print(f"资料 {len(recs)} 条 · 线路库 {len(our)} 条")
    print("  匹配：", " ".join(f"{k}={v}" for k, v in sorted(by_how.items())))

    covered = {r["line_name"] for r in recs if r["line_name"]}
    print(f"  线路库里被资料覆盖的线路：{len(covered)}/{len(our)}"
          f"（{len(covered)/max(len(our),1):.0%}）")

    # 里程口径核对
    agree = dis = 0
    fill_total = 0
    worst = []
    for r in recs:
        if r["match_how"] not in ("exact", "family", "added"):
            continue
        ck = km_check(r, our)
        if not ck or not ck.get("pairs"):
            continue
        fill_total += ck.get("fill") or 0
        if ck["agree"]:
            agree += 1
        else:
            dis += 1
            worst.append((ck["spread"], r["line_name"], ck["median"], ck["pairs"]))
    print(f"  里程口径：与档案一致 {agree} 条，不一致 {dis} 条；可补档案缺里程 {fill_total} 站")
    for sp, nm, med, pr in sorted(worst, reverse=True)[:6]:
        print(f"     {nm}：共同站 {pr}，中位差 {med} km，最大差 {sp} km")

    # 可补的新线路
    cand = []
    for r in recs:
        if r["line_name"] or r["n_stop"] < 4:
            continue
        names = [s["name"] for s in r["stops"]]
        cov = sum(1 for x in names if cnrail.coord(x)) / len(names)
        cand.append((cov, r["name"], r["n_stop"], r["type"]))
    for th in (0.95, 0.8, 0.6):
        print(f"  可补线路（坐标覆盖 ≥{th:.0%}）：{sum(1 for c in cand if c[0] >= th)} 条")
    print("  其中覆盖最好的：")
    for cov, nm, n, ty in sorted(cand, reverse=True)[:8]:
        print(f"     {nm:<16} {n:>3}站 覆盖{cov:.0%} {ty or ''}")

    if not args.apply:
        print("\n（只出报告，加 --apply 写库）")
        return
    info = apply(recs, our, args.add_lines)
    print(f"\n已写入 line_catalog {len(recs)} 条 / line_catalog_stop "
          f"{sum(r['n_stop'] for r in recs)} 行")
    if args.add_lines > 0:
        print(f"补进线路库 {len(info['added'])} 条：{'、'.join(info['added'][:10])}"
              + (" …" if len(info["added"]) > 10 else ""))
    print("重启服务后生效")


if __name__ == "__main__":
    main()
