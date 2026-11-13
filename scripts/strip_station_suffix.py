# -*- coding: utf-8 -*-
"""一次性迁移：全项目站名统一不带「站」后缀（杭州东站 → 杭州东），撞名就合并。

「同一个站两种写法」是这库里最常见的脏数据来源：车站档案叫「杭州东站」、12306 站表
叫「杭州东」，搜「杭州东」时一个在左一个在右，改坐标改到其中一个、另一个不跟着变。
这个脚本把库里所有站名过一遍 `railcore.canon_station()`：

  * 剥掉的是「多余的站字」：「杭州东站」→「杭州东」、「北京南站」→「北京南」；
  * 真名保住：「十八站」「九站」「新九站」「同名站」这种本来带「站」的地名原样不动
    （判据：剥完的裸名本身得是认得的站 —— 12306 站表 / station_master 里有它）；
  * 「十八站站」这种重复一层的，并进「十八站」。

改这些地方：
  station_master / station_custom / coord_override（撞名就合并，来源、别名、坐标都并进去）
  station_coords.json（坐标键）
  line_custom.stations_json / line_base_stop.name / line_route.stops_json / line_override.stops_json
  linedesk.db 的 ld_stop.name

用法：
    python scripts/strip_station_suffix.py              # dry-run，只列要改什么
    python scripts/strip_station_suffix.py --apply      # 真改（先整库备份）
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WEBAPP = os.path.join(os.path.dirname(HERE), "webapp")
sys.path.insert(0, WEBAPP)

import railcore as rc                                          # noqa: E402

APPLY = "--apply" in sys.argv
RAIL_DB = os.path.join(WEBAPP, "data", "rail.db")
DESK_DB = os.path.join(WEBAPP, "data", "linedesk.db")
COORDS = os.path.join(WEBAPP, "data", "station_coords.json")
BACKUP_ROOT = os.path.join(WEBAPP, "data", "backup")

canon = rc.canon_station
report = {}

# 把自建线路 / 车站并进 cnrail：这样 `_line_names()` 才包含「安陆西」这类
# 只在线路站序里出现过的裸名（否则「安陆西站」判不出来，会留着「站」）
try:
    rc._apply_custom()
except Exception as e:                               # noqa: BLE001
    print("（cnrail 线路未并入：%s）" % e)


def bump(k, n=1):
    report[k] = report.get(k, 0) + n


def backup() -> str:
    tag = time.strftime("station-suffix-%Y%m%d-%H%M%S")
    out = os.path.join(BACKUP_ROOT, tag)
    os.makedirs(out, exist_ok=True)
    for db_path in (RAIL_DB, DESK_DB):
        if not os.path.exists(db_path):
            continue
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(os.path.join(out, os.path.basename(db_path)))
        with dst:
            src.backup(dst)
        dst.close()
        src.close()
    if os.path.exists(COORDS):
        shutil.copy2(COORDS, os.path.join(out, "station_coords.json"))
    return out


# ---------------------------------------------------------------------------
# 车站表：改名 / 合并
# ---------------------------------------------------------------------------

def merge_text(a: str, b: str, sep: str = "|") -> str:
    """两个「a|b|c」字段取并集（保持稳定顺序）"""
    out = []
    for part in (a or "").split(sep) + (b or "").split(sep):
        p = part.strip()
        if p and p not in out:
            out.append(p)
    return sep.join(out)


def dedupe_note(*notes: str) -> str:
    bits = []
    for n in notes:
        for seg in (n or "").split("；"):
            s = seg.strip(" ；")
            if s and s not in bits:
                bits.append(s)
    return "；".join(bits)


def migrate_master(con: sqlite3.Connection) -> None:
    con.row_factory = sqlite3.Row
    rows = {r["key"]: dict(r) for r in con.execute("SELECT * FROM station_master")}
    plans = []
    for key, r in rows.items():
        new = canon(r["name"])
        if new == r["name"]:
            continue
        tgt = rows.get(new)                    # 归一键就是裸名，直接拿新名字当 key 找
        plans.append((key, r["name"], new, tgt["key"] if tgt and tgt["key"] != key else ""))
    print("station_master: %d 条要改（其中合并 %d）"
          % (len(plans), sum(1 for p in plans if p[3])))
    for key, old, new, tkey in plans[:12]:
        print("   %s -> %s%s" % (old, new, "（并入已有 %s）" % tkey if tkey else ""))
    if len(plans) > 12:
        print("   …还有 %d 条" % (len(plans) - 12))
    bump("station_master_rename", len(plans))
    bump("station_master_merge", sum(1 for p in plans if p[3]))
    if not APPLY:
        return

    for key, old, new, tkey in plans:
        r = rows.get(key)
        if not r:
            continue
        if tkey and tkey in rows:
            t = rows[tkey]
            sources = merge_text(t["sources"], r["sources"])
            aliases = merge_text(t["aliases"], old)
            note = dedupe_note(t["note"], r["note"])
            if old not in aliases.split("|"):
                aliases = merge_text(aliases, old)
            con.execute(
                """UPDATE station_master SET
                     lat=COALESCE(lat, ?), lon=COALESCE(lon, ?),
                     province=COALESCE(province, ?), city=COALESCE(city, ?),
                     type=COALESCE(type, ?), level=COALESCE(level, ?),
                     tele=COALESCE(tele, ?), pinyin=COALESCE(pinyin, ?),
                     bureau=COALESCE(bureau, ?), kind=COALESCE(kind, ?),
                     n_sources=?, sources=?, aliases=?, coord_src=COALESCE(coord_src, ?),
                     coord_note=COALESCE(coord_note, ?), note=?, updated_at=?
                   WHERE key=?""",
                (r["lat"], r["lon"], r["province"], r["city"], r["type"], r["level"],
                 r["tele"], r["pinyin"], r["bureau"], r["kind"],
                 len(sources.split("|")) if sources else t["n_sources"],
                 sources, aliases, r["coord_src"], r["coord_note"], note,
                 time.strftime("%Y-%m-%d %H:%M:%S"), tkey))
            con.execute("DELETE FROM station_master WHERE key=?", (key,))
            t["aliases"] = aliases          # 后面若有第三条并进来，别再漏掉
            t["note"] = note
            rows.pop(key, None)
        else:
            # 没有同名的裸名站：只把 name 改干净，key 本来就是裸名
            con.execute("UPDATE station_master SET name=?, updated_at=? WHERE key=?",
                        (new, time.strftime("%Y-%m-%d %H:%M:%S"), key))
            rows[key]["name"] = new


def dedupe_master_by_name(con: sqlite3.Connection) -> None:
    """改名之后撞名的两条并成一条

    「十八站站」（key=十八站）改完叫「十八站」，而 key=十八 那条本来就叫「十八站」——
    键不同、名字一样，联想里就会看到两个一模一样的站。这里按名字归并：
    留 key 就等于名字的那条（次选来源多、坐标全的），其余并进去再删掉。
    """
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute("SELECT * FROM station_master")]
    groups: dict = {}
    for r in rows:
        groups.setdefault(canon(r["name"]), []).append(r)
    dup = {k: v for k, v in groups.items() if len(v) > 1}
    print("station_master 同名归并：%d 组" % len(dup))
    for nm, rs in list(dup.items())[:8]:
        print("   %s：%s" % (nm, " / ".join(r["key"] for r in rs)))
    bump("station_master_name_merge", len(dup))
    if not APPLY:
        return
    for nm, rs in dup.items():
        rs.sort(key=lambda r: (r["key"] != nm, -(r["n_sources"] or 0),
                               r["lat"] is None))
        keep = rs[0]
        for r in rs[1:]:
            sources = merge_text(keep["sources"], r["sources"])
            aliases = merge_text(keep["aliases"], r["key"])
            note = dedupe_note(keep["note"], r["note"])
            con.execute(
                """UPDATE station_master SET
                     lat=COALESCE(lat, ?), lon=COALESCE(lon, ?),
                     province=COALESCE(province, ?), city=COALESCE(city, ?),
                     type=COALESCE(type, ?), level=COALESCE(level, ?),
                     tele=COALESCE(tele, ?), pinyin=COALESCE(pinyin, ?),
                     bureau=COALESCE(bureau, ?), kind=COALESCE(kind, ?),
                     n_sources=?, sources=?, aliases=?, note=?
                   WHERE key=?""",
                (r["lat"], r["lon"], r["province"], r["city"], r["type"], r["level"],
                 r["tele"], r["pinyin"], r["bureau"], r["kind"],
                 len(sources.split("|")) if sources else keep["n_sources"],
                 sources, aliases, note, keep["key"]))
            con.execute("DELETE FROM station_master WHERE key=?", (r["key"],))
            keep["aliases"], keep["note"] = aliases, note


def migrate_custom(con: sqlite3.Connection) -> None:
    con.row_factory = sqlite3.Row
    rows = {r["name"]: dict(r) for r in con.execute("SELECT * FROM station_custom")}
    plans = []
    for nm, r in rows.items():
        new = canon(nm)
        if new == nm:
            continue
        plans.append((nm, new, new in rows))
    print("station_custom: %d 条要改（其中合并 %d）"
          % (len(plans), sum(1 for p in plans if p[2])))
    for old, new, hit in plans[:10]:
        print("   %s -> %s%s" % (old, new, "（并入已有）" if hit else ""))
    bump("station_custom_rename", len(plans))
    bump("station_custom_merge", sum(1 for p in plans if p[2]))
    if not APPLY:
        return
    for old, new, hit in plans:
        r = rows.get(old)
        if not r:
            continue
        if hit and old in rows:
            t = rows[new]
            lines = merge_text(t["lines_json"], r["lines_json"])
            note = dedupe_note(t["note"], r["note"], "含原名：" + old)
            alias = merge_text(t.get("alias") or "", old)
            # 坐标：人工录的优先；两边都是人工就留已有的
            prefer_old = (r.get("coord_src") == "manual" and t.get("coord_src") != "manual")
            con.execute(
                """UPDATE station_custom SET
                     lat=?, lon=?, lines_json=?, tele=COALESCE(tele, ?),
                     bureau=COALESCE(bureau, ?), note=?, alias=?,
                     coord_src=COALESCE(coord_src, ?)
                   WHERE name=?""",
                (r["lat"] if prefer_old else t["lat"], r["lon"] if prefer_old else t["lon"],
                 lines, r["tele"], r["bureau"], note, alias,
                 r.get("coord_src"), new))
            con.execute("DELETE FROM station_custom WHERE name=?", (old,))
            rows.pop(old, None)
        else:
            con.execute("UPDATE station_custom SET name=? WHERE name=?", (new, old))
            rows[new] = rows.pop(old)
            rows[new]["name"] = new


def migrate_coord_override(con: sqlite3.Connection) -> None:
    con.row_factory = sqlite3.Row
    rows = {r["name"]: dict(r) for r in con.execute("SELECT * FROM coord_override")}
    plans = [(nm, canon(nm)) for nm in rows if canon(nm) != nm]
    print("coord_override: %d 条要改" % len(plans))
    bump("coord_override", len(plans))
    if not APPLY:
        return
    for old, new in plans:
        if new in rows:
            con.execute("DELETE FROM coord_override WHERE name=?", (old,))
        else:
            con.execute("UPDATE coord_override SET name=? WHERE name=?", (new, old))
            rows[new] = rows.pop(old)


# ---------------------------------------------------------------------------
# 线路站序
# ---------------------------------------------------------------------------

def migrate_line_custom(con: sqlite3.Connection) -> None:
    con.row_factory = sqlite3.Row
    rows = list(con.execute("SELECT name, stations_json, updated_at FROM line_custom"))
    n_stop = 0
    changed = []
    for r in rows:
        try:
            sts = json.loads(r["stations_json"] or "[]")
        except (TypeError, ValueError):
            continue
        hit = 0
        out = []
        prev_renamed = False
        dirty = False
        for s in sts:
            old = s.get("name") or ""
            new = canon(old)
            was = new != old
            if was:
                hit += 1
                dirty = True
            # 「苏州南站 / 苏州南」这种同一个站两种写法挨在一起：改名后并成一个
            # （只并「改名造成的」；库里本来就重复的站条目不在这里动，那是另一件事）
            if out and new == (out[-1].get("name") or "") and (was or prev_renamed) \
                    and s.get("km") == out[-1].get("km"):
                prev_renamed = prev_renamed or was
                continue
            out.append({**s, "name": new})
            prev_renamed = was
        sts = out
        if dirty:
            n_stop += hit
            changed.append((r["name"], hit, json.dumps(sts, ensure_ascii=False)))
    print("line_custom: %d 条线路 / %d 个站名要改" % (len(changed), n_stop))
    for nm, hit, _ in changed[:8]:
        print("   %s（%d 处）" % (nm, hit))
    bump("line_custom_stops", n_stop)
    if not APPLY:
        return
    for nm, _, blob in changed:
        con.execute("UPDATE line_custom SET stations_json=? WHERE name=?", (blob, nm))


def migrate_line_base(con: sqlite3.Connection) -> None:
    con.row_factory = sqlite3.Row
    rows = list(con.execute("SELECT base_id, seq, name FROM line_base_stop"))
    plans = [(r["base_id"], r["seq"], r["name"], canon(r["name"]))
             for r in rows if canon(r["name"]) != r["name"]]
    print("line_base_stop: %d 个站名要改" % len(plans))
    for b, s, old, new in plans[:8]:
        print("   %s -> %s" % (old, new))
    bump("line_base_stop", len(plans))
    if not APPLY:
        return
    for b, s, _, new in plans:
        con.execute("UPDATE line_base_stop SET name=? WHERE base_id=? AND seq=?", (new, b, s))


def migrate_stops_json(con: sqlite3.Connection, table: str) -> None:
    con.row_factory = sqlite3.Row
    rows = list(con.execute("SELECT name, stops_json FROM %s" % table))
    changed = []
    n = 0
    for r in rows:
        try:
            sts = json.loads(r["stops_json"] or "[]")
        except (TypeError, ValueError):
            continue
        hit = 0
        for s in sts:
            if not isinstance(s, dict):
                continue
            old = s.get("name") or ""
            new = canon(old)
            if new != old:
                s["name"] = new
                hit += 1
        if hit:
            n += hit
            changed.append((r["name"], json.dumps(sts, ensure_ascii=False)))
    print("%s: %d 条 / %d 个站名要改" % (table, len(changed), n))
    bump(table, n)
    if not APPLY:
        return
    for nm, blob in changed:
        con.execute("UPDATE %s SET stops_json=? WHERE name=?" % table, (blob, nm))


def migrate_coords_json() -> None:
    if not os.path.exists(COORDS):
        return
    with open(COORDS, encoding="utf-8") as f:
        d = json.load(f)
    out = {}
    hit = 0
    for k, v in d.items():
        nk = canon(k)
        if nk != k:
            hit += 1
        out.setdefault(nk, v)          # 已经有裸名就用裸名那份（都是同一个站的坐标）
    print("station_coords.json: %d 个键要改（%d -> %d）" % (hit, len(d), len(out)))
    bump("coords_json", hit)
    if APPLY and hit:
        with open(COORDS, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, sort_keys=True)


def migrate_desk() -> None:
    if not os.path.exists(DESK_DB):
        return
    con = sqlite3.connect(DESK_DB)
    con.row_factory = sqlite3.Row
    try:
        rows = list(con.execute("SELECT line_id, seq, name FROM ld_stop"))
    except sqlite3.Error:
        con.close()
        return
    plans = [(r["line_id"], r["seq"], r["name"], canon(r["name"]))
             for r in rows if canon(r["name"]) != r["name"]]
    print("linedesk ld_stop: %d 个站名要改" % len(plans))
    for lid, s, old, new in plans[:8]:
        print("   #%d %s -> %s" % (lid, old, new))
    bump("ld_stop", len(plans))
    if APPLY and plans:
        with con:
            for lid, s, _, new in plans:
                con.execute("UPDATE ld_stop SET name=? WHERE line_id=? AND seq=?",
                            (new, lid, s))
    con.close()


def main() -> None:
    print("站名白名单：12306 %d 条 / station_master %d 条"
          % (len(rc.cnrail.known_names()), len(rc._master_station_names())))
    for nm in ("杭州东站", "北京南站", "十八站", "新九站", "同名站"):
        print("   例：%s -> %s" % (nm, canon(nm)))
    if APPLY:
        print("备份：", backup())
    con = sqlite3.connect(RAIL_DB)
    try:
        with con:
            migrate_master(con)
            dedupe_master_by_name(con)
            migrate_custom(con)
            migrate_coord_override(con)
            migrate_line_custom(con)
            migrate_line_base(con)
            migrate_stops_json(con, "line_route")
            migrate_stops_json(con, "line_override")
    finally:
        con.close()
    migrate_coords_json()
    migrate_desk()
    print("\n汇总：", json.dumps(report, ensure_ascii=False))
    if not APPLY:
        print("（dry-run，未写库；加 --apply 才真改）")


if __name__ == "__main__":
    main()
