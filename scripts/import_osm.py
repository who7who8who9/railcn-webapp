# -*- coding: utf-8 -*-
"""
把 OSM 中国区铁路数据入库，作为**第六套来源**，与 line_catalog / line_ce 平行。

建三张表（纯外部快照，每次导入整表重建）：
  line_osm       线路：类型、状态、电气化、时速、轨距、way 数、几何里程、站数、首末站、断口
  line_osm_stop  站序：站名、段内里程、偏离中心线距离、本线/并线站、乘降所
  station_osm    车站点：经纬度、站/乘降所、运营方、是否境内

为什么还是单开表：现有五套来源都靠线名字符串硬绑，OSM 的站序是新变量
（1747 条线、11943 个站次），并进 line_catalog_stop 会把「资料库」的含义搞浑，
也没法对账。查询层按需取（lineseg.stations_for 第三候选）。

数据来源（2026-10-29 的 china-latest.osm.pbf，pyosmium 提取 + 建图求里程）：
  data/osm/lines.csv   每条具名线路一行（来自 out/line-summary.csv）
  data/osm/stops.csv   站序明细（来自 out/line-stations.csv）
  data/osm/stations.json.gz  国铁车站点（从 out/china-rail-stations.geojsonseq 精简）

两个口径要记住：
  1. **站名并列民族文字**：OSM 新疆/内蒙的站常写成「乌鲁木齐 ئۈرۈمچى」，
     入库前取中文段（clean_name），原始写法留在 name_raw。
  2. **km_along 是沿线里程，不是段内里程**：860 条线的首站不是 0（京沪从上海端算起
     就得到 1434），入库统一减首站值，并保证单调递增。

用法：
    python scripts/import_osm.py --prepare --src <osm-china-rail/out>   # 抽源数据进 data/osm
    python scripts/import_osm.py                     # 只报告
    python scripts/import_osm.py --apply             # 写库
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import re
import sqlite3
import sys
import time
from collections import defaultdict
from typing import Dict, List

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "webapp"))

import railcore                                                  # noqa: E402

DIR = os.path.join(ROOT, "data", "osm")
LINES_CSV = os.path.join(DIR, "lines.csv")
STOPS_CSV = os.path.join(DIR, "stops.csv")
STATIONS_GZ = os.path.join(DIR, "stations.json.gz")

# 中国境内判据（含港澳）
LON_MIN, LON_MAX = 73.0, 136.0
LAT_MIN, LAT_MAX = 17.0, 54.5
TW = (119.3, 122.1, 21.8, 25.35)      # 台湾岛，整块挖掉

# 只收国铁。源文件 china-rail-stations.geojsonseq 已经是 train（另一个 china-stations
# 才含地铁/轻轨），这里照样按 kind 挡一道 —— 换源重跑不会把地铁混进来。
STATION_KINDS = {"train"}
# 线路只留这三种状态（源文件里也就这三种，没有废弃/拆除的）
LINE_STATUS = {"运营中", "在建", "规划"}

_TAILS = ("高速铁路", "客运专线", "城际铁路", "高速线", "客专线", "铁路", "高铁", "线")
HS_RE = re.compile(r"高速|客专|客运专线|城际|高铁")

DDL = """
CREATE TABLE IF NOT EXISTS line_osm (
    name          TEXT PRIMARY KEY,  -- 线名（OSM 的写法）
    match_key     TEXT,              -- 归一化比对键（与 line_ce 同口径）
    line_type     TEXT,              -- 干线/高速铁路/城际铁路/支线/联络线/铁路专用线/站场线/其他
    status        TEXT,              -- 运营中 / 在建 / 规划
    elec          TEXT,              -- 电气化 / 部分电气化 / 非电气化
    maxspeed      INTEGER,           -- 各 way 时速众数
    gauge         INTEGER,           -- 轨距众数（mm）
    way_count     INTEGER,           -- 组成这条线的 way 数（复线上下行分开计）
    total_km      REAL,              -- 几何里程之和（≠ 营业里程：复线会翻倍）
    n_stop        INTEGER,           -- 站序行数
    n_station     INTEGER,           -- 其中 railway=station（非乘降所）
    first_sta     TEXT,
    last_sta      TEXT,
    bridges       INTEGER,           -- 建图时补接的断口数
    max_bridge_km REAL,              -- 最大断口长度，大则本线里程可疑
    in_china      INTEGER,           -- 站点多数在境内
    updated_at    TEXT
);
CREATE TABLE IF NOT EXISTS line_osm_stop (
    lid       TEXT NOT NULL,         -- line_osm.name
    seq       INTEGER NOT NULL,
    name      TEXT NOT NULL,         -- 清洗后的站名（去掉并列的民族文字）
    name_raw  TEXT,                  -- OSM 原始站名
    km        REAL,                  -- 段内里程（已按首站归零）
    dist_m    REAL,                  -- 车站到线路中心线的垂直距离
    role      TEXT,                  -- primary 本线车站 / secondary 并线枢纽站
    lifecycle TEXT,
    halt      INTEGER,               -- 乘降所
    osm_id    TEXT,                  -- 车站节点 id
    lon       REAL,                  -- 站序自带的坐标：车站点是插值到线上的那个点，
    lat       REAL,                  -- 比拿站名去别处反查准（100% 有值）
    PRIMARY KEY (lid, seq)
);
CREATE INDEX IF NOT EXISTS ix_osm_stop_name ON line_osm_stop(name);
CREATE TABLE IF NOT EXISTS station_osm (
    osm_id    TEXT PRIMARY KEY,      -- n<id>
    name      TEXT,                  -- 清洗后的站名
    name_raw  TEXT,
    lon       REAL,
    lat       REAL,
    railway   TEXT,                  -- station / halt
    halt      INTEGER,
    operator  TEXT,
    ref       TEXT,
    lifecycle TEXT,
    in_china  INTEGER,
    n_lines   INTEGER                -- 附近线路条数（lines_near 长度）
);
CREATE INDEX IF NOT EXISTS ix_osm_sta_name ON station_osm(name);
CREATE INDEX IF NOT EXISTS ix_osm_sta_ll ON station_osm(lon, lat);
-- 车站 → 所属线路的反查表。line-stations.csv 里「哪条线经过这个站」是
-- **距离 + 几何走向**判出来的：站插值到线路中心线、记下垂直距离 dist_m 和沿线
-- 里程 km，所以这张表把老关系掉个个儿存一份，查「XX 站属于哪几条线」不用全表扫。
CREATE TABLE IF NOT EXISTS station_osm_line (
    osm_id    TEXT NOT NULL,          -- station_osm.osm_id
    lid       TEXT NOT NULL,          -- line_osm.name
    seq       INTEGER,                -- 在这条线的站序里的序号
    km        REAL,                   -- 沿线里程（自首站起算）
    dist_m    REAL,                   -- 站到线路中心线的垂直距离，越大越可疑
    role      TEXT,                   -- primary 本线车站 / secondary 并线枢纽站
    PRIMARY KEY (osm_id, lid)
);
CREATE INDEX IF NOT EXISTS ix_osm_sl_line ON station_osm_line(lid);
"""


# ---------------------------------------------------------------------------
# 站名清洗
# ---------------------------------------------------------------------------
_STATE_BRACKET = ("预留", "在建", "待建", "旧址", "废弃", "规划", "缓建", "停用", "已拆")
_BRACKET_TAIL = re.compile(r"[（(][^（()）]{1,12}[)）]\s*$")
_BRACKET_ONLY = re.compile(r"^[（(].*[)）]$")


def show_name(raw: str, known: set = frozenset()) -> str:
    """OSM 站名清洗，两步：

    1. **并列民族文字**取中文那段（新疆/内蒙的站常写成「乌鲁木齐 ئۈرۈمچى」）：
           「乌鲁木齐南 ئۇرۇمچى جەنۇبى」→ 乌鲁木齐南
           「ບໍ່ເຕັນ 磨丁」          → 磨丁
           「Блокпост 1560 км」       → 原样（没有中文段，是境外的会让站）
       只按空白切分是有意的：中文站名内部不带空格，带空格的多半是并列写法。

    2. **尾部括注**看情况剥：
           「中沙坝（预留）」「沈荡站(待建)」「禾家村(旧址)」—— 建设状态，必剥；
           「一零二厂（吉化）」「望江门（杭州机辆段）」—— 归属说明，剥掉后
           短名在项目站表里存在才剥（否则会造出一个查不到的站）；
           「临淄(辛店)」这类旧名括注是项目自己在用的写法，不动；
           「(预留)」这种只剩括注的，剥完为空 → 调用方丢弃该行。
    """
    s = " ".join(str(raw or "").split())
    if not s:
        return ""
    parts = s.split(" ")
    if len(parts) > 1:
        # 「车溪 (预留)」切出来两段都含中文，纯括注那段不能算站名
        zh = [p for p in parts
              if re.search(r"[\u4e00-\u9fff]", p) and not _BRACKET_ONLY.match(p)]
        s = max(zh, key=len) if zh else parts[0]

    m = _BRACKET_TAIL.search(s)
    if m:
        inner = m.group(0).strip("（）() ")
        short = s[: m.start()].strip()
        if short and (inner in _STATE_BRACKET or inner.startswith(_STATE_BRACKET)
                      or short in known):
            s = short
    return s


def in_cn(lon: float, lat: float) -> bool:
    """境内判定（含港澳，不含台湾地区）

    经纬度框只能框到大致范围：台湾岛（119.3~122.1°E, 21.8~25.35°N）整个落在框里，
    得多挖一块把它剔除。福建平潭（119.78°E, 25.55°N）刚好在台湾框北边，不受影响。
    源数据本来就没收台湾，这里是把它变成**硬约束** —— 换一批源数据重跑也不会漏进来。
    """
    if lon is None or lat is None:
        return False
    if not (LON_MIN <= lon <= LON_MAX and LAT_MIN <= lat <= LAT_MAX):
        return False
    return not (TW[0] <= lon <= TW[1] and TW[2] <= lat <= TW[3])


def known_stations() -> set:
    """项目现有站名（底表 + 自建），用来判断括注能不能剥"""
    out = set()
    try:
        base = json.load(open(os.path.join(ROOT, "data", "cnrail_stations.json"),
                              encoding="utf-8"))
        out.update(base.keys())
    except (OSError, ValueError):
        pass
    try:
        conn = sqlite3.connect(railcore.DB_PATH)
        out.update(r[0] for r in conn.execute("SELECT name FROM station_custom"))
        conn.close()
    except sqlite3.DatabaseError:
        pass
    return {re.sub(r"\s+", "", x) for x in out if x}


# ---------------------------------------------------------------------------
# 线名比对键（与 import_chinaemu 同口径）
# ---------------------------------------------------------------------------
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


def match_key(name: str) -> str:
    import lineseg
    hs = "H" if HS_RE.search(name or "") else "P"
    sp = lineseg.split(name)
    if sp:
        return hs + core(sp[0]) + "|" + sp[1]
    return hs + core(name)


# ---------------------------------------------------------------------------
# 抽取：把 2.7 GB 源目录里要用的三样东西搬到 data/osm/
# ---------------------------------------------------------------------------
def prepare(src: str) -> None:
    os.makedirs(DIR, exist_ok=True)
    for fn, dst in (("line-summary.csv", LINES_CSV), ("line-stations.csv", STOPS_CSV)):
        p = os.path.join(src, fn)
        if not os.path.exists(p):
            sys.exit("缺少 " + p)
        with open(p, "rb") as f, open(dst, "wb") as g:
            g.write(f.read())
        print("  %-22s → %s (%.1f KB)" % (fn, os.path.relpath(dst, ROOT),
                                          os.path.getsize(dst) / 1024))

    p = os.path.join(src, "china-rail-stations.geojsonseq")
    if not os.path.exists(p):
        sys.exit("缺少 " + p)
    known = known_stations()
    out: List[dict] = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)["properties"]
            if d.get("kind") not in STATION_KINDS:      # 只要国铁，地铁/轻轨不进来
                continue
            if not in_cn(d.get("lon"), d.get("lat")):   # 境外 + 台湾剔掉
                continue
            raw = d.get("name") or ""
            out.append({
                "osm_id": d.get("osm_id"), "name": show_name(raw, known), "name_raw": raw,
                "lon": d.get("lon"), "lat": d.get("lat"),
                "railway": d.get("railway"), "halt": 1 if d.get("halt") else 0,
                "operator": d.get("operator"), "ref": d.get("railway:ref"),
                "lifecycle": d.get("lifecycle"),
                "in_china": 1 if in_cn(d.get("lon"), d.get("lat")) else 0,
                "n_lines": len(d.get("lines_near") or []),
            })
    with gzip.open(STATIONS_GZ, "wt", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print("  %-22s → %s (%.1f KB, %d 个点)"
          % ("stations.geojsonseq", os.path.relpath(STATIONS_GZ, ROOT),
             os.path.getsize(STATIONS_GZ) / 1024, len(out)))


# ---------------------------------------------------------------------------
# 读源数据
# ---------------------------------------------------------------------------
def load() -> tuple:
    if not os.path.exists(LINES_CSV):
        sys.exit("缺少 %s —— 先跑 python scripts/import_osm.py --prepare --src <目录>" % LINES_CSV)
    with open(LINES_CSV, encoding="utf-8-sig") as f:
        lines = list(csv.DictReader(f))

    byline: Dict[str, List[dict]] = defaultdict(list)
    with open(STOPS_CSV, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            byline[r["line"]].append(r)

    stations: List[dict] = []
    if os.path.exists(STATIONS_GZ):
        with gzip.open(STATIONS_GZ, "rt", encoding="utf-8") as f:
            stations = json.load(f)
    return lines, byline, stations


def fnum(v, cast=float):
    try:
        if v is None or v == "":
            return None
        return cast(float(v))
    except (TypeError, ValueError):
        return None


def rows_of_line(l: dict, rs: List[dict], known: set = frozenset(),
                 coord: Dict[str, tuple] = None) -> List[dict]:
    """把一条线的站序行规整成：里程归零、单调递增、seq 从 1 连续

    剥完括注只剩空名的行（沪蓉线的「(预留)」）直接丢掉，它不是一个站；
    剥完撞名的（「大花岭（普速场）」和「大花岭」）只留里程在前的那一行 ——
    下游按站名排站序，同名两行会把站序算乱。
    """
    rs = sorted(rs, key=lambda x: int(x["seq"]))
    # 先挑站（可能丢掉无名行和撞名行），**再**按留下来的那些算归零基准：
    # 定银线头两行是无名的会让点，若先拿全表的 min 当基准，剥掉它们之后首站就变成
    # K13.3 而不是 K0 了。
    keep = []
    seen = set()
    for r in rs:
        nm = show_name(r.get("station"), known)
        if not nm or nm in seen:
            continue
        seen.add(nm)
        keep.append((r, nm, r.get("km_along")))
    kms = [fnum(k) for _, _, k in keep]
    got = [k for k in kms if k is not None]
    base = min(got) if got else None
    out = []
    for i, (r, nm, raw_km) in enumerate(keep):
        k = fnum(raw_km)
        c = (coord or {}).get(r.get("osm_id") or "") or (None, None)
        out.append({
            "seq": i + 1,
            "name": nm,
            "name_raw": r.get("station") or "",
            "km": round(k - base, 1) if (k is not None and base is not None) else None,
            "dist_m": fnum(r.get("dist_to_line_m")),
            "role": r.get("role") or "",
            "lifecycle": r.get("lifecycle") or "",
            "halt": 1 if str(r.get("halt")).lower() == "true" else 0,
            "osm_id": r.get("osm_id") or "",
            "lon": c[0], "lat": c[1],
        })
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prepare", action="store_true", help="从外部 OSM 源目录抽取数据")
    ap.add_argument("--src", default="", help="--prepare 用：osm-china-rail/out 目录")
    ap.add_argument("--apply", action="store_true", help="写库")
    args = ap.parse_args()

    if args.prepare:
        if not args.src:
            sys.exit("--prepare 需要 --src <osm-china-rail/out>")
        print("抽取源数据：")
        prepare(args.src)
        print()

    lines, byline, stations = load()
    known = known_stations()
    coord = {s["osm_id"]: (s["lon"], s["lat"]) for s in stations if s.get("osm_id")}
    # 只要运营中 / 在建 / 规划。废弃、拆除的历史线路不进主库 —— 它们在线名列表里
    # 会和现役线路撞名，查「XX线」时分不清要看哪一条。
    drop_status = [l["line"] for l in lines if (l.get("status") or "") not in LINE_STATUS]
    lines = [l for l in lines if (l.get("status") or "") in LINE_STATUS]

    recs = []
    for l in lines:
        rs = byline.get(l["line"]) or []
        if not rs:
            continue
        st = rows_of_line(l, rs, known, coord)
        cn = sum(1 for s in st if s["km"] is not None)
        recs.append({
            "line": l["line"], "line_type": l.get("line_type") or "",
            "status": l.get("status") or "", "elec": l.get("elec") or "",
            "maxspeed": fnum(l.get("maxspeed"), int), "gauge": fnum(l.get("gauge"), int),
            "way_count": fnum(l.get("way_count"), int), "total_km": fnum(l.get("line_km")),
            "n_stop": len(st), "first": l.get("first") or "", "last": l.get("last") or "",
            "bridges": fnum(l.get("bridges"), int), "max_bridge_km": fnum(l.get("max_bridge_km")),
            "in_china": 1, "stops": st, "n_km": cn,
            "n_station": sum(1 for s in st if not s["halt"]),
        })

    # 境内判定：**站名带中文的占多数**。坐标范围判不住 —— 朝鲜咸镜北道
    # （함북선，129°E/42°N）和俄远东都落在 [73,136]×[17,54.5] 里；而中国站点
    # 在 OSM 里一律带中文名（新疆的是「乌鲁木齐 ئۈرۈمچى」这种并列写法）。
    # 香港西九龙、霍尔果斯这类跨境站只占少数，不影响整条线。
    for r in recs:
        zh = sum(1 for s in r["stops"] if re.search(r"[\u4e00-\u9fff]", s["name"]))
        r["in_china"] = 1 if zh * 2 >= max(1, len(r["stops"])) else 0
    foreign = [r for r in recs if not r["in_china"]]
    recs = [r for r in recs if r["in_china"]]

    if drop_status:
        print("  剔除非常态线路 %d 条：%s"
              % (len(drop_status), "、".join(drop_status[:12])))
    # 车站再去一遍境外：经纬度框判不住 —— 朝鲜咸镜北道、俄远东、蒙古扎门乌德都落在
    # 框里。**站名带中文**才是硬判据（中国站点在 OSM 里一律有中文名），已经躺在中方
    # 线路站序里的例外放行（跨境口岸站可能只有原文名）。
    on_line = {s["osm_id"] for r in recs for s in r["stops"] if s.get("osm_id")}
    keep = [s for s in stations
            if s.get("name") and (re.search(r"[\u4e00-\u9fff]", s["name"])
                                  or s["osm_id"] in on_line)]
    drop_sta = [s["name"] for s in stations if s not in keep]
    # n_lines 自己算：源文件里那个 lines_near 字段从来没值，照抄会全是 0
    per_line: Dict[str, set] = defaultdict(set)
    for s in keep:
        per_line[s["osm_id"]] = set()
    for r in recs:
        for s in r["stops"]:
            if s.get("osm_id") in per_line:
                per_line[s["osm_id"]].add(r["line"])
    for s in keep:
        s["n_lines"] = len(per_line[s["osm_id"]])
    stations = keep

    n_stop = sum(r["n_stop"] for r in recs)
    print("OSM 线路 %d 条（剔除境外 %d 条），站序 %d 行，有里程 %d 行（%.1f%%）"
          % (len(recs), len(foreign), n_stop, sum(r["n_km"] for r in recs),
             100.0 * sum(r["n_km"] for r in recs) / max(1, n_stop)))
    print("车站点 %d 个（境内 %d，剔除境外 %d）"
          % (len(stations), sum(1 for s in stations if s["in_china"]), len(drop_sta)))
    if foreign:
        print("  剔除的境外线路：", "、".join(r["line"] for r in foreign[:12]))
    if drop_sta:
        print("  剔除的境外车站：", "、".join(drop_sta[:12]))

    if not args.apply:
        print("\n（只报告，未写库。加 --apply 才落库）")
        for r in recs[:5]:
            print("    %-18s %-6s %3d 站 %8.1f km"
                  % (r["line"], r["line_type"], r["n_stop"], r["total_km"] or 0))
        return

    conn = sqlite3.connect(railcore.DB_PATH)
    prev = 0
    try:
        prev = conn.execute("SELECT COUNT(*) FROM line_osm").fetchone()[0]
    except sqlite3.DatabaseError:
        pass
    if prev:
        print("  库里现有 line_osm %d 条，将整表重建" % prev)

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    with conn:
        # 全量快照：整表重建，改了列定义也不会留下旧结构
        conn.executescript("DROP TABLE IF EXISTS station_osm_line;"
                           "DROP TABLE IF EXISTS line_osm_stop;"
                           "DROP TABLE IF EXISTS line_osm;"
                           "DROP TABLE IF EXISTS station_osm;")
        conn.executescript(DDL)
        for r in recs:
            conn.execute(
                "INSERT OR REPLACE INTO line_osm(name,match_key,line_type,status,elec,"
                "maxspeed,gauge,way_count,total_km,n_stop,n_station,first_sta,last_sta,"
                "bridges,max_bridge_km,in_china,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["line"], match_key(r["line"]), r["line_type"], r["status"], r["elec"],
                 r["maxspeed"], r["gauge"], r["way_count"], r["total_km"], r["n_stop"],
                 r["n_station"], r["first"], r["last"], r["bridges"], r["max_bridge_km"],
                 r["in_china"], now))
            conn.executemany(
                "INSERT OR REPLACE INTO line_osm_stop"
                "(lid,seq,name,name_raw,km,dist_m,role,lifecycle,halt,osm_id,lon,lat)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                [(r["line"], s["seq"], s["name"], s["name_raw"], s["km"], s["dist_m"],
                  s["role"], s["lifecycle"], s["halt"], s["osm_id"], s["lon"], s["lat"])
                 for s in r["stops"]])
        conn.executemany(
            "INSERT OR REPLACE INTO station_osm(osm_id,name,name_raw,lon,lat,railway,halt,"
            "operator,ref,lifecycle,in_china,n_lines) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            [(s["osm_id"], s["name"], s["name_raw"], s["lon"], s["lat"], s["railway"],
              s["halt"], s["operator"], s["ref"], s["lifecycle"], s["in_china"],
              s["n_lines"]) for s in stations if s.get("osm_id") and s.get("name")])
        # 车站归属：每个站次落成一条 (站 → 线)。role=secondary 是并线枢纽站，
        # 它确实在线上（dist_m 也小），只是同时还属于别的线 —— 照记，界面上标一下。
        conn.executemany(
            "INSERT OR REPLACE INTO station_osm_line(osm_id,lid,seq,km,dist_m,role)"
            " VALUES(?,?,?,?,?,?)",
            [(s["osm_id"], r["line"], s["seq"], s["km"], s["dist_m"], s["role"])
             for r in recs for s in r["stops"] if s.get("osm_id")])

    print("\n已写入：line_osm %d，line_osm_stop %d，station_osm %d，station_osm_line %d"
          % (conn.execute("SELECT COUNT(*) FROM line_osm").fetchone()[0],
             conn.execute("SELECT COUNT(*) FROM line_osm_stop").fetchone()[0],
             conn.execute("SELECT COUNT(*) FROM station_osm").fetchone()[0],
             conn.execute("SELECT COUNT(*) FROM station_osm_line").fetchone()[0]))
    print("库:", railcore.DB_PATH)
    conn.close()


if __name__ == "__main__":
    main()
