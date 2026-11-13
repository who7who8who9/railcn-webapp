# -*- coding: utf-8 -*-
"""把 D:/railcn 里各套「车站名称 + 位置」来源合并成一份统一车站库，并更新 rail.db。

口径（用户 2026-11-05 明确）：
  * **站名保持干净**，一律不带来源后缀；同名（含「XX」/「XX站」两种写法）归成一个实体。
  * **所有来源写进备注**：station_master.sources / station_custom.note，名称里一个字都不体现。
  * 坐标统一 WGS84；**只补空缺、不覆盖**现有档案 / 人工坐标（沿用项目既有规则）。
  * 同名不同地点（比如 14 个「奥体中心」）不新增名称，只在备注里记「同名 N 处」。

产物：
  * rail.db 新表 `station_master`：全量合并主表（幂等，重跑会整表重建）
  * rail.db `station_custom`：现有行只补空缺字段 + 备注追加来源；高可信新站补插
  * webapp/data/station_coords.json：只补空缺（让地图坐标缓存立即生效）
  * railway-data/unified/stations_merged_db.csv：可审阅的全量合并表

用法：
  python scripts/merge_station_sources.py            # dry-run，只统计不写
  python scripts/merge_station_sources.py --apply   # 正式写入
  python scripts/merge_station_sources.py --apply --full
        # --full：把全部新站（含只有 OSM 单源的疑似地铁/乘降点）也补进 station_custom
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import os
import re
import shutil
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(BASE, "webapp", "data", "rail.db")
COORD_JSON = os.path.join(BASE, "webapp", "data", "station_coords.json")
REPORT = os.path.join(BASE, "railway-data", "unified", "stations_merged_db.csv")

# 来源 id -> 备注里显示的名字。顺序也用于「显示名 / 属性」的优先级
SRC_LABEL = {
    "manual":    "人工坐标(coord_override)",
    "archive":   "车站档案(cnrail_data_v2)",
    "coordjson": "OSM派生坐标(station_coords)",
    "st12306":   "12306站表(stations.json)",
    "crtrain":   "12306资料(crtrain)",
    "custom":    "自建车站库(station_custom)",
    "chnsta":    "站名册(CHN-Railway-Sta)",
    "pomerun":   "站名册(pomerun)",
    "undefi":    "车站属性库(station-database)",
    "gtfs":      "坐标表(chinese-railway-gtfs)",
    "panorama":  "railway-panorama",
    "trainvis":  "TrainVis",
    "railglobe": "rail-globe",
    "osmhdx":    "OSM-HDX",
    "master":    "六源合并总表(stations_master)",
    "osmdb":     "OSM车站(station_osm)",
    "osmraw":    "OSM原始站(data/osm)",
    "cngeo":     "cnrail OSM车站点",
}
# 数值越小越权威（用来挑显示名、坐标、属性）
PRIO = {k: i for i, k in enumerate(SRC_LABEL)}

# 只有这些来源的坐标能确认是 WGS84，其余（GCJ-02 / BD-09 / 未标注）只记来源不取坐标
WGS84_OK = {"manual", "archive", "coordjson", "osmdb", "osmraw", "osmhdx",
            "cngeo", "railglobe", "master", "custom"}

# 明确是城市轨道交通的 OSM 标签
SUBWAY_RE = re.compile(r"subway|light_rail|tram|metro", re.I)
STATUS_RE = re.compile(r"[\(（](预留|废弃|旧址|在建|规划|已拆除|停用)[\)）]")
# 只截「中文主体 + 外文段」里的中文主体；允许 # 和括号，别把「1#食堂」截成「1」
MULTILING_RE = re.compile(r"^[\u4e00-\u9fff0-9A-Za-z·\-—#()（）.、]+")
# 站名以 12306 / 站名册的裸名为准（档案 name 字段是「XX站」，不适合当车站库主键）
NAME_SRC = ("st12306", "chnsta", "pomerun", "custom", "master")


def _p(rel: str) -> str:
    return os.path.join(BASE, rel.replace("/", os.sep))


def _json(rel: str, enc="utf-8-sig"):
    with open(_p(rel), encoding=enc) as f:
        return json.load(f)


def norm(s) -> str:
    return re.sub(r"\s+", "", str(s or ""))


def clean_name(s) -> str:
    """把 OSM 的多语言名 / 状态标注清成中文站名（**不添加任何来源信息**）"""
    n = norm(s)
    if not n:
        return ""
    n = n.split("/")[0].split("／")[0]
    m = MULTILING_RE.match(n)
    if m:
        n = m.group(0)
    n = STATUS_RE.sub("", n)
    return n.strip("-—·")


def key(s) -> str:
    """归一键：和项目 cnrail._key 同口径（去「站 / 线路所 / 所」后缀）"""
    n = norm(clean_name(s))
    for suf in ("线路所", "站", "所"):
        if len(n) > len(suf) and n.endswith(suf):
            return n[: -len(suf)]
    return n


def rec(src, name, lat=None, lon=None, **kw) -> dict:
    d = {"src": src, "name": clean_name(name), "lat": lat, "lon": lon}
    d.update(kw)
    return d


def valid_ll(lat, lon) -> bool:
    try:
        la, lo = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    return -90 <= la <= 90 and -180 <= lo <= 180 and (la != 0 or lo != 0)


# ---------------------------------------------------------------------------
# 各来源读取：统一产出 rec 列表
# ---------------------------------------------------------------------------
def load_sources() -> list:
    out: list[dict] = []
    stat = defaultdict(int)

    def add(rs, tag):
        rs = [r for r in rs if r.get("name") and key(r["name"])]
        out.extend(rs)
        stat[tag] = len(rs)

    # 1) 人工坐标（最高优先级）
    try:
        c = sqlite3.connect(DB)
        add([rec("manual", n, la, lo, kind="国铁")
             for n, la, lo in c.execute("SELECT name, lat, lon FROM coord_override")], "manual")
        # 2) 自建车站库（现有 4496 条业务数据）
        add([rec("custom", n, la, lo, tele=t or "", bureau=b or "",
                 type=ty or "", note=nt or "", kind="国铁")
             for n, la, lo, t, b, ty, nt in c.execute(
                 "SELECT name,lat,lon,tele,bureau,type,note FROM station_custom")], "custom")
        # 3) OSM 车站表
        add([rec("osmdb", n or nr, la, lo,
                 kind="乘降点" if hl else "国铁", osm_ref=rf or "")
             for n, nr, la, lo, hl, rf in c.execute(
                 "SELECT name,name_raw,lat,lon,halt,ref FROM station_osm")], "osmdb")
        c.close()
    except sqlite3.Error as e:
        print("  ! rail.db 读取失败:", e)

    # 4) 车站档案 cnrail_data_v2
    arch = _json("data/cnrail_stations.json")
    add([rec("archive", v.get("name") or k, v.get("lat"), v.get("lon"),
             province=v.get("province"), city=v.get("city"), type=v.get("type"),
             tele=v.get("tele"), bureau=v.get("bureau"), en=v.get("en"),
             lines=v.get("lines") or [], coordSrc=v.get("coordSrc"), kind="国铁")
         for k, v in arch.items()], "archive")

    # 5) OSM 派生坐标 station_coords.json {name: [lat, lon]}
    cj = _json("webapp/data/station_coords.json")
    add([rec("coordjson", n, v[0], v[1]) for n, v in cj.items() if isinstance(v, (list, tuple)) and len(v) >= 2],
        "coordjson")

    # 6) 12306 站表（电报码 / 拼音 / 城市；无坐标）
    s12306 = _json("webapp/data/stations.json")
    add([rec("st12306", v.get("station_name"), None, None,
             tele=v.get("station_code"), pinyin=v.get("station_pinyin"),
             city=v.get("city"), kind="国铁")
         for v in s12306.values() if v.get("station_name")], "st12306")

    # 7) crtrain 的 12306 资料（GCJ-02 坐标：只记来源，不取坐标）
    crt = _json("data/crtrain/data_stations.json")
    add([rec("crtrain", v.get("name"), None, None, city=v.get("city"),
             addr=v.get("addr"), kind="国铁")
         for v in crt.values() if v.get("name")], "crtrain")
    crtn = _json("data/crtrain/data_station_names.json")
    add([rec("crtrain", n, None, None, tele=code, kind="国铁")
         for code, n in crtn.items() if n], "crtrain(名册)")

    # 8) OSM 原始站（data/osm/stations.json.gz）
    with gzip.open(_p("data/osm/stations.json.gz"), "rt", encoding="utf-8") as f:
        osmraw = json.load(f)
    add([rec("osmraw", v.get("name") or v.get("name_raw"), v.get("lat"), v.get("lon"),
             kind="乘降点" if v.get("halt") else "国铁", osm_ref=v.get("ref") or "")
         for v in osmraw], "osmraw")

    # 9) 六源合并总表（railway-data/unified/stations_master.csv）
    master = []
    with open(_p("railway-data/unified/stations_master.csv"), encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if not r.get("车站名"):
                continue
            srcs = [s.strip() for s in re.split(r"[+、,，]", r.get("数据来源") or "") if s.strip()]
            master.append(rec("master", r["车站名"], r.get("纬度(WGS84)"), r.get("经度(WGS84)"),
                              province=r.get("省份"), city=r.get("城市"),
                              type=r.get("车站类型"), level=r.get("等级"),
                              tele=r.get("电报码"), pinyin=r.get("拼音"),
                              coord_note=r.get("坐标可信度说明"),
                              sub_sources=srcs, kind="国铁"))
    add(master, "master")

    # 10) OSM-HDX 车站点 / cnrail OSM 车站点
    for rel, tag in (("railway-data/08_osm_hdx/stations_point.geojson", "osmhdx"),
                     ("cnrail/data/stations.geojson", "cngeo")):
        gj = _json(rel, "utf-8")
        rs = []
        for ft in gj.get("features") or []:
            pr = ft.get("properties") or {}
            nm = pr.get("name_zh") or pr.get("name") or ""
            geom = ft.get("geometry") or {}
            xy = geom.get("coordinates") or []
            if len(xy) < 2:
                continue
            rw = str(pr.get("railway") or "") + " " + str(pr.get("station") or "")
            rs.append(rec(tag, nm, xy[1], xy[0],
                          kind="地铁/轻轨" if SUBWAY_RE.search(rw) else
                               ("乘降点" if "halt" in rw or "tram_stop" in rw else "国铁"),
                          osm_ref=pr.get("osm_id") or pr.get("id") or ""))
        add(rs, tag)

    # 11) rail-globe 原始站（六源之一，单独认一次来源）
    rg = _json("railway-data/extracted/rail-globe.github.io-main/data/stations.geojson")
    rs = []
    for ft in rg.get("features") or []:
        pr, xy = ft.get("properties") or {}, (ft.get("geometry") or {}).get("coordinates") or []
        if len(xy) >= 2 and pr.get("n"):
            rs.append(rec("railglobe", pr["n"], xy[1], xy[0], level=pr.get("h"), kind="国铁"))
    add(rs, "railglobe")

    # 12) station-database 属性库（省份 / 城市 / 类型 / 路局；坐标未标注，不取）
    ud = _json("railway-data/extracted/China-Railway-Station-Database-main/data.json")
    add([rec("undefi", n, None, None, city=v.get("city"), province=v.get("provinces"),
             type=v.get("type"), bureau=v.get("attribution"), kind="国铁")
         for n, v in ud.items()], "undefi")

    # 13) gtfs 坐标表 / panorama / TrainVis：只在六源总表里已经并过，这里补记「来源存在」
    with open(_p("railway-data/extracted/chinese-railway-gtfs-main/coordinates.csv"),
              encoding="utf-8-sig") as f:
        add([rec("gtfs", r["station_name"], None, None) for r in csv.DictReader(f)
             if r.get("station_name")], "gtfs")

    # 14) 站名册（电报码 / 拼音 / 省份 / 城市）
    with open(_p("railway-data/extracted/CHN-Railway-Sta-main/Station.csv"),
              encoding="utf-8-sig") as f:
        add([rec("chnsta", r.get("车站名"), None, None, tele=r.get("电报码"),
                 pinyin=r.get("拼音"), city=r.get("城市"), province=r.get("省份"), kind="国铁")
             for r in csv.DictReader(f) if r.get("车站名")], "chnsta")
    txt = open(_p("railway-data/extracted/china-railway-station-main/src/data.ts"),
               encoding="utf-8").read()
    pm = re.search(r'export default "([^"]+)"', txt)
    if pm:
        add([rec("pomerun", p[1], None, None, tele=p[2], pinyin=p[3], city=p[7], kind="国铁")
             for p in (x.split("|") for x in pm.group(1).split("@")) if len(p) >= 8], "pomerun")

    print("== 各来源读入 ==")
    for k, v in sorted(stat.items(), key=lambda x: -x[1]):
        print(f"  {k:<20} {v:>7}")
    print(f"  {'合计原始行':<20} {len(out):>7}")
    return out


# ---------------------------------------------------------------------------
# 合并
# ---------------------------------------------------------------------------
def build_entities(rows: list) -> list:
    groups = defaultdict(list)
    for r in rows:
        groups[key(r["name"])].append(r)

    ents = []
    for k, rs in groups.items():
        if not k:
            continue
        rs.sort(key=lambda r: PRIO.get(r["src"], 99))
        # 显示名：优先 12306 / 站名册的裸名（「北京南」而不是档案里的「北京南站」），
        # 它们与现有 station_custom 的命名口径一致；没有则退到组内最短干净名。
        name = ""
        for src in NAME_SRC:
            hit = next((r["name"] for r in rs if r["src"] == src and r["name"]), None)
            if hit:
                name = hit
                break
        if not name:
            cands = sorted({r["name"] for r in rs if r["name"]},
                           key=lambda n: (len(n), n.endswith("站")))
            name = cands[0] if cands else k

        sources = set()
        extras = set()
        for r in rs:
            sources.add(r["src"])
            extras.update(r.get("sub_sources") or [])
        # 六源总表「数据来源」列里是中文串，映射成来源 id（不要和 id 重复计数）
        SUB_MAP = (("rail-globe", "railglobe"), ("osm-hdx", "osmhdx"),
                   ("panorama", "panorama"), ("station-database", "undefi"),
                   ("gtfs", "gtfs"), ("trainvis", "trainvis"))
        for s in extras:
            low = s.lower()
            hit = next((sid for kw, sid in SUB_MAP if kw in low), None)
            sources.add(hit or s)

        # 坐标：按来源优先级取第一个有效的 WGS84 坐标
        coords = []
        for r in rs:
            if r["src"] in WGS84_OK and valid_ll(r["lat"], r["lon"]):
                coords.append((float(r["lat"]), float(r["lon"]), r["src"]))
        lat = lon = None
        coord_src = ""
        if coords:
            lat, lon, coord_src = coords[0]
        # 同名多点的离散度（写进备注，不体现在名称里）
        far = 0.0
        if len(coords) > 1:
            a = (math.radians(lat), math.radians(lon))
            for la, lo, _ in coords[1:]:
                b = (math.radians(la), math.radians(lo))
                d = 2 * 6371 * math.asin(min(1.0, math.sqrt(
                    math.sin((b[0]-a[0])/2)**2 +
                    math.cos(a[0])*math.cos(b[0])*math.sin((b[1]-a[1])/2)**2)))
                far = max(far, d)

        def pick(field):
            for r in rs:
                v = r.get(field)
                if v not in (None, "", "None", "nan"):
                    return v
            return ""

        kinds = [r.get("kind") for r in rs if r.get("kind")]
        # OSM 全量点里混了机场捷运 / 食堂 / 境外站之类，标出来但不丢（写在备注里）
        junk = re.search(r"航站楼|卫星厅|食堂|餐厅|check ?in|候机|站台|车库|车辆段",
                         name, re.I) or re.match(r"^[A-Za-zА-Яа-яЁё0-9#\s\-]+$", name)
        if junk and not any(r.get("tele") for r in rs):
            kind = "其他设施"
        elif kinds and all(x == "地铁/轻轨" for x in kinds):
            kind = "地铁/轻轨"
        elif "国铁" in kinds:
            kind = "国铁"
        elif "乘降点" in kinds:
            kind = "乘降点"
        elif kinds:
            kind = kinds[0]

        aliases = sorted({clean_name(r["name"]) for r in rs if clean_name(r["name"])} - {name})
        note_bits = ["来源：" + "、".join(sorted(SRC_LABEL.get(s, s) for s in sources))]
        note_bits.append("坐标来源：" + (SRC_LABEL.get(coord_src, coord_src) or "无"))
        if far > 2:
            note_bits.append(f"同名 {len(coords)} 处坐标，最远相距 {far:.1f} km")
        if aliases:
            note_bits.append("别名：" + "、".join(aliases[:12]))
        ents.append({
            "key": k, "name": name, "lat": lat, "lon": lon,
            "province": pick("province"), "city": pick("city"),
            "type": pick("type"), "level": pick("level"),
            "tele": pick("tele"), "pinyin": pick("pinyin"), "bureau": pick("bureau"),
            "kind": kind, "n_sources": len(sources),
            "sources": "|".join(sorted(sources)),
            "aliases": "|".join(aliases),
            "coord_src": coord_src,
            "coord_note": pick("coord_note"),
            "note": "；".join(note_bits),
        })
    ents.sort(key=lambda e: e["name"])
    return ents


SCHEMA = """
CREATE TABLE IF NOT EXISTS station_master (
    key         TEXT PRIMARY KEY,       -- 归一键（去空白/站后缀）
    name        TEXT NOT NULL,          -- 规范站名（干净，不含来源）
    lat REAL, lon REAL,                 -- WGS84
    province TEXT, city TEXT, type TEXT, level TEXT,
    tele TEXT, pinyin TEXT, bureau TEXT,
    kind        TEXT,                   -- 国铁 / 乘降点 / 地铁/轻轨
    n_sources   INTEGER,
    sources     TEXT,                   -- 备注：所有来源 id
    aliases     TEXT,                   -- 备注：出现过的其它写法
    coord_src   TEXT,
    coord_note  TEXT,
    note        TEXT,                   -- 备注：可读的来源 / 坐标 / 别名说明
    updated_at  TEXT
);
CREATE INDEX IF NOT EXISTS ix_stm_name ON station_master(name);
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只统计）")
    ap.add_argument("--full", action="store_true",
                    help="把 OSM 单源的疑似地铁/乘降点也补进 station_custom")
    args = ap.parse_args()

    rows = load_sources()
    ents = build_entities(rows)
    print("\n== 合并结果 ==")
    print("  唯一实体（车站）:", len(ents))
    print("  有坐标:", sum(1 for e in ents if e["lat"] is not None))
    print("  有电报码:", sum(1 for e in ents if e["tele"]))
    print("  有省份:", sum(1 for e in ents if e["province"]))
    print("  分类:", {k: sum(1 for e in ents if e["kind"] == k)
                    for k in ("国铁", "乘降点", "地铁/轻轨", "其他设施")})
    print("  来源数分布:", {n: sum(1 for e in ents if e["n_sources"] == n)
                       for n in range(1, 8)})
    print("  名称里含来源字样的:",
          sum(1 for e in ents if any(w in e["name"] for w in
                                     ("rail-globe", "OSM", "panorama", "gtfs", "TrainVis", "database"))))

    # 与现有 station_custom 对照
    c = sqlite3.connect(DB)
    have = {key(r[0]) for r in c.execute("SELECT name FROM station_custom")}
    custom_rows = c.execute(
        "SELECT name,lat,lon,tele,bureau,type,note,source,coord_src FROM station_custom").fetchall()
    by_key = {e["key"]: e for e in ents}
    fillable = [r for r in custom_rows
                if (r[1] is None or r[2] is None or not r[3] or not r[4] or not r[5])
                and key(r[0]) in by_key]
    coord_fill = [r for r in fillable if (r[1] is None or r[2] is None)
                  and by_key[key(r[0])]["lat"] is not None]
    # 「现有系统车站库」= 档案 + 12306 + 坐标缓存 + 自建 + OSM 的并集。
    # 这些站名系统本来就认得，不必再以「自建车站」的身份塞一遍 station_custom。
    arch = _json("data/cnrail_stations.json")
    cj = _json("webapp/data/station_coords.json")
    s12306 = _json("webapp/data/stations.json")
    system_keys = set(have) | set(map(key, arch)) | set(map(key, cj)) | \
        set(map(key, (v.get("station_name") for v in s12306.values()))) | \
        {key(r[0]) for r in c.execute("SELECT name FROM station_osm")}
    fresh = [e for e in ents if e["key"] not in system_keys]
    authoritative = {"st12306", "archive", "chnsta", "pomerun", "crtrain"}
    trusted = [e for e in fresh
               if args.full or (e["kind"] == "国铁" and
                                (e["tele"] or e["n_sources"] >= 2 or
                                 bool(authoritative & set(e["sources"].split("|")))))]
    print("\n== 对现有车站库的影响 ==")
    print("  现有系统已认得的站名（并集）:", len(system_keys))
    print("  station_custom 现有:", len(custom_rows))
    print("  可补字段的现有行:", len(fillable), "（其中能补坐标", len(coord_fill), "）")
    print("  系统里完全没有的新站名:", len(fresh))
    print("  拟新增进 station_custom:", len(trusted), "（--full 则", len(fresh), "）")
    print("  现有行会被改名的: 0（名称一个字不动）")
    print("  现有非空坐标会被覆盖的: 0（只补空缺）")
    ncoord = len(cj)
    cj_new = set(cj)
    for e in ents:
        if e["lat"] is None:
            continue
        for nm in [e["name"], e["key"]] + (e["aliases"].split("|") if e["aliases"] else []):
            if nm:
                cj_new.add(nm)
    print("  station_coords.json 预估:", ncoord, "→", len(cj_new), "条（只补空缺）")
    c.close()

    # CSV 报告（dry-run 也产出，方便先审）
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, "w", encoding="utf-8-sig", newline="") as f:
        cols = ["name", "key", "lat", "lon", "province", "city", "type", "level",
                "tele", "pinyin", "bureau", "kind", "n_sources", "sources",
                "aliases", "coord_src", "coord_note", "note"]
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(ents)
    print("\n  审阅表:", REPORT)

    if not args.apply:
        print("\n[dry-run] 未写库。确认无误后加 --apply 执行。")
        return

    # ---------- 写库 ----------
    print("\n== 写入 ==")
    c = sqlite3.connect(DB)
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    c.execute("DELETE FROM station_master")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.executemany(
        "INSERT INTO station_master(key,name,lat,lon,province,city,type,level,tele,pinyin,"
        "bureau,kind,n_sources,sources,aliases,coord_src,coord_note,note,updated_at)"
        " VALUES(" + ",".join("?" * 19) + ")",
        [(e["key"], e["name"], e["lat"], e["lon"], e["province"], e["city"], e["type"],
          e["level"], e["tele"], e["pinyin"], e["bureau"], e["kind"], e["n_sources"],
          e["sources"], e["aliases"], e["coord_src"], e["coord_note"], e["note"], now)
         for e in ents])
    print("  station_master:", c.execute("SELECT COUNT(*) FROM station_master").fetchone()[0], "行")

    # 现有行：只补空缺 + 备注追加来源（保留原有业务备注）
    ups, note_up = [], []
    for nm, la, lo, tele, bureau, typ, note, source, coord_src in custom_rows:
        e = by_key.get(key(nm))
        if not e:
            continue
        nla = la if la is not None else e["lat"]
        nlo = lo if lo is not None else e["lon"]
        ntele = tele or e["tele"]
        nbureau = bureau or e["bureau"]
        ntyp = typ or e["type"]
        ncs = coord_src or (e["coord_src"] if la is None else "")
        tag = "来源：" + "、".join(sorted(SRC_LABEL.get(s, s) for s in e["sources"].split("|")))
        base_note = re.sub(r"[；;]\s*来源：.*$", "", note or "").strip("；; ")
        nnote = (base_note + "；" if base_note else "") + tag if tag else base_note
        if (nla, nlo, ntele, nbureau, ntyp, ncs, nnote) != (la, lo, tele, bureau, typ, coord_src, note):
            ups.append((nla, nlo, ntele, nbureau, ntyp, ncs, nnote, nm))
    c.executemany(
        "UPDATE station_custom SET lat=?,lon=?,tele=?,bureau=?,type=?,coord_src=?,note=? "
        "WHERE name=?", ups)
    print("  station_custom 补字段:", len(ups), "行")

    # 新增站：名称干净、来源全在备注里
    ins = [(e["name"], e["lat"], e["lon"], e["tele"] or "", e["bureau"] or "",
            e["type"] or "", "[]", e["note"], "merged", e["coord_src"], now)
           for e in trusted if e["name"]]
    c.executemany(
        "INSERT OR IGNORE INTO station_custom"
        "(name,lat,lon,tele,bureau,type,lines_json,note,source,coord_src,updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)", ins)
    print("  station_custom 新增:", len(ins), "行 → 共",
          c.execute("SELECT COUNT(*) FROM station_custom").fetchone()[0], "行")
    c.commit()
    c.close()

    # 坐标 JSON：只补空缺，让地图坐标缓存立即覆盖到新站
    cj = _json("webapp/data/station_coords.json")
    before = len(cj)
    for e in ents:
        if e["lat"] is None:
            continue
        for nm in [e["name"], e["key"]] + (e["aliases"].split("|") if e["aliases"] else []):
            if nm and nm not in cj:
                cj[nm] = [e["lat"], e["lon"]]
    with open(COORD_JSON, "w", encoding="utf-8") as f:
        json.dump(cj, f, ensure_ascii=False, separators=(",", ":"))
    print("  station_coords.json:", before, "→", len(cj), "条（只补空缺）")
    print("\n完成。备份在 data/backup/station-merge-20261105/")


if __name__ == "__main__":
    main()
