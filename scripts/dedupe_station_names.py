# -*- coding: utf-8 -*-
"""
dedupe_station_names.py — 站名去重与归一（线路站序 + 车站库）

手工「+ 线路 / + 车站」时，同一个站很容易被写成两份：
线路上写「河边站」、车站库里写「河边」，坐标补在「河边」上、站序引用的却是
「河边站」—— 于是线路详情缺坐标、车次经停与线路站序对不上，看着就是「重复录入」。
（huirui 资料库带进来的线路也有同样问题：那边整段站名都带「站」。）

这个脚本把两处都归一到 12306 口径（去掉「站」后缀），撞名就合并。

判定很保守，只动「看起来就是被多加了个站字」的：
  * 名字本身在坐标表里 —— 说明它就是个带「站」的地名（黑龙江的「九站」），不动
  * 其余「XX站」一律改成「XX」；改完和已有的「XX」撞了，合并成一条
    坐标优先用人工录的（source=manual），其次已有坐标，最后才用被合并那条的

只改 line_custom / station_custom，不碰车站档案与坐标表；改前自动导出备份。

用法：
    python scripts/dedupe_station_names.py              # 只看会改什么
    python scripts/dedupe_station_names.py --apply      # 真改（先备份）
"""
from __future__ import annotations

import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WEBAPP = os.path.join(os.path.dirname(HERE), "webapp")
sys.path.insert(0, WEBAPP)

import railcore as rc                                        # noqa: E402

BACKUP_DIR = os.path.join(WEBAPP, "data", "backup")


def build_map() -> dict:
    """{带「站」的写法: 规范写法}；名字本身在坐标表里的（九站）不参与"""
    rc.stations.ensure()
    coords = set(rc.stations.coords.keys())
    names = set()
    for it in rc.list_custom_lines():
        names.update(p["name"] for p in it["stations"])
    names.update(s["name"] for s in rc.list_custom_stations())
    out = {}
    for n in sorted(names):
        if not n.endswith("站") or len(n) <= 2:
            continue
        if n in coords:                     # 「九站」这种真实地名
            continue
        out[n] = n[:-1]
    return out


def plan(mapping: dict) -> dict:
    """算出要改哪些：线路站序命中多少处、车站库要改名/合并多少条"""
    lines = []
    for it in rc.list_custom_lines():
        ss = [p["name"] for p in it["stations"]]
        hits = [x for x in ss if x in mapping]
        if hits:
            lines.append({"name": it["name"], "stations": ss, "hits": hits})
    stations = [s["name"] for s in rc.list_custom_stations() if s["name"] in mapping]
    return {"lines": lines, "stations": stations}


def backup() -> str:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    path = os.path.join(BACKUP_DIR, "dedupe-%s.json" % time.strftime("%Y%m%d-%H%M%S"))
    with rc.db() as conn:
        dump = {
            "line_custom": [dict(r) for r in conn.execute("SELECT * FROM line_custom")],
            "station_custom": [dict(r) for r in conn.execute("SELECT * FROM station_custom")],
            "coord_override": [dict(r) for r in conn.execute("SELECT * FROM coord_override")],
        }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(dump, f, ensure_ascii=False)
    return path


def merge_station(conn, src: dict, dst_name: str) -> str:
    """把 src 这条并进 dst_name：坐标、线路、电报码取「更可信的那个」"""
    dst = conn.execute("SELECT * FROM station_custom WHERE name = ?", (dst_name,)).fetchone()
    if not dst:
        conn.execute("UPDATE station_custom SET name = ?, updated_at = ? WHERE name = ?",
                     (dst_name, time.time(), src["name"]))
        return "改名"

    dst = dict(dst)
    # 坐标：人工录的优先，其次是已有坐标，最后才用被合并的那条
    lat, lon = dst.get("lat"), dst.get("lon")
    if lat is None or lon is None or (src.get("source") == "manual" and dst.get("source") != "manual"):
        if src.get("lat") is not None and src.get("lon") is not None:
            lat, lon = src["lat"], src["lon"]
    # 连接线路取并集，别丢
    def _lines(v):
        try:
            return list(json.loads(v) or [])
        except (TypeError, ValueError):
            return []
    ls = _lines(dst.get("lines_json"))
    for x in _lines(src.get("lines_json")):
        if x not in ls:
            ls.append(x)
    note = dst.get("note") or src.get("note") or ""
    conn.execute(
        "UPDATE station_custom SET lat=?, lon=?, lines_json=?, tele=COALESCE(tele, ?), "
        "bureau=COALESCE(bureau, ?), note=?, type=COALESCE(type, ?), updated_at=? WHERE name=?",
        (lat, lon, json.dumps(ls, ensure_ascii=False),
         src.get("tele"), src.get("bureau"), note, src.get("type"),
         max(float(dst.get("updated_at") or 0), float(src.get("updated_at") or 0)), dst_name),
    )
    conn.execute("DELETE FROM station_custom WHERE name = ?", (src["name"],))
    return "合并"


def apply(mapping: dict, p: dict) -> None:
    bk = backup()
    print("备份 →", bk)
    now = time.time()
    # 1) 车站库：改名 / 合并
    with rc.db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM station_custom")]
        for row in rows:
            if row["name"] in mapping:
                how = merge_station(conn, row, mapping[row["name"]])
                print(f"  车站 {row['name']:8} → {mapping[row['name']]:8} [{how}]")
        conn.commit()
    # 2) 线路站序：替换站名，并去掉替换后相邻重复的站
    with rc.db() as conn:
        for it in p["lines"]:
            src = conn.execute("SELECT * FROM line_custom WHERE name = ?", (it["name"],)).fetchone()
            if not src:
                continue
            old = json.loads(src["stations_json"])
            new = []
            for pt in old:
                nm = mapping.get(pt["name"], pt["name"])
                if new and new[-1]["name"] == nm:   # 换完撞上，同一站连写两次
                    continue
                new.append({**pt, "name": nm})
            conn.execute("UPDATE line_custom SET stations_json = ?, updated_at = ? WHERE name = ?",
                         (json.dumps(new, ensure_ascii=False), now, it["name"]))
            print(f"  线路 {it['name']:14} 改 {len(it['hits'])} 处"
                  + (f"，并掉相邻重复 {len(old) - len(new)} 处" if len(old) != len(new) else ""))
        conn.commit()
    print("库已更新；重启服务或调用 /api/curate/refresh 后生效")


def main() -> None:
    do_apply = "--apply" in sys.argv
    mapping = build_map()
    p = plan(mapping)
    print(f"可归一的写法 {len(mapping)} 个，涉及线路 {len(p['lines'])} 条、"
          f"车站 {len(p['stations'])} 条")
    for a, b in sorted(mapping.items()):
        print(f"  {a:10} → {b}")
    print(f"\n线路命中 {sum(len(x['hits']) for x in p['lines'])} 处：")
    for it in sorted(p["lines"], key=lambda x: -len(x["hits"])):
        print(f"  {it['name']:14} {len(it['hits']):3} 处  {', '.join(it['hits'][:6])}"
              + (" …" if len(it["hits"]) > 6 else ""))
    if not mapping:
        print("没有需要归一的站名")
        return
    if not do_apply:
        print("\n（只预览，未改动。加 --apply 执行）")
        return
    print()
    apply(mapping, p)


if __name__ == "__main__":
    main()
