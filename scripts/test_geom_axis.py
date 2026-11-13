# -*- coding: utf-8 -*-
"""回归：整理台走向的点序契约 —— 所有折线点一律 [lon, lat]

2026-11-06 用户报「重新保存了沪昆线，在车站/线路里查找线路时左上角出现一条线路」：
③「合并保存」搬来源包走向时，`_save_geom()` 把 [lon,lat] 的 chain 一律按
`for lat, lon in chain` 解包，写出的 `ld_geom.spine_gz` 整个反成 [lat,lon]。
主站 MapView 按 [lon,lat] 画，纬度当经度用，于是走向变成左上角一条竖线，
停站却按正确坐标挤在右下角。

契约：
  1. `geoimport.assemble()` 的 chain 是 (lat, lon)；`line_route.spine_gz` 是 [lon, lat]；
     两者写进 ld_geom 后都必须是 [lon, lat]。
  2. 主站 / 整理台读走向，原始多段 parts_gz 优先，spine 只作兜底。
  3. 历史反序 spine 要在读取时自动纠正，不能要求用户重新保存一次。
"""
import gc
import gzip
import json
import os
import sys
import tempfile
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "webapp"))
import linedesk  # noqa: E402

OK = FAIL = SKIP = 0


def ck(cond, msg):
    global OK, FAIL
    if cond:
        OK += 1
        print("  OK   " + msg)
    else:
        FAIL += 1
        print("  FAIL " + msg)


def skip(msg):
    global SKIP
    SKIP += 1
    print("  SKIP " + msg)


def is_lonlat(p):
    """中国境内：经度 70~140、纬度 15~55"""
    try:
        return 70 <= p[0] <= 140 and 15 <= p[1] <= 55
    except (TypeError, IndexError):
        return False


def first_point(parts):
    for seg in parts:
        for p in seg:
            return p
    return None


def all_points(parts):
    return [p for seg in parts for p in seg]


print("== 1 _norm_chain：两种输入都归一成 [lon,lat] ==")
latlon = [[31.251959, 121.452749], [30.293752, 120.208454], [25.017547, 102.722355]]
lonlat = [[121.452749, 31.251959], [120.208454, 30.293752], [102.722355, 25.017547]]
a = linedesk._norm_chain(latlon)
b = linedesk._norm_chain(lonlat)
ck(a == lonlat, "(lat,lon) 输入 → [lon,lat]")
ck(b == lonlat, "[lon,lat] 输入保持不变")
ck(all(is_lonlat(p) for p in a + b), "归一化后每个点都落在中国经纬度范围")

print("== 2 整段判定，不逐点翻转（否则折线会被撕出假跳变）==")
mixed = [[121.45, 31.25], [120.20, 30.29], [102.72, 25.02], [110.0, 30.0]]
n = linedesk._norm_chain(mixed)
ck(len(n) == 4 and all(is_lonlat(p) for p in n), "混合点全部统一方向")
ck(n[-1] == [110.0, 30.0], "边界点没有单独被翻转")
ck(linedesk._norm_chain([]) == [], "空 chain 返回空")
ck(linedesk._norm_chain([[1, 2], [None, 3]]) == [[1.0, 2.0]], "坏点被丢弃")

print("== 3 _norm_parts：多段 / 单条两种结构 ==")
multi = linedesk._norm_parts([latlon, latlon])
single = linedesk._norm_parts(latlon, single=True)
ck(len(multi) == 2 and all(is_lonlat(p) for p in all_points(multi)), "parts_gz 多段")
ck(len(single) == 1 and single[0] == lonlat, "spine_gz 单条折线")

print("== 4 _save_geom 写库：两种 chain 都存成 [lon,lat] ==")
orig = linedesk.DB_PATH
tmpdir = tempfile.TemporaryDirectory(prefix="railcn-geom-axis-",
                                      ignore_cleanup_errors=True)
try:
    linedesk.DB_PATH = os.path.join(tmpdir.name, "linedesk.db")
    linedesk.init()
    for label, chain in (("latlon", latlon), ("lonlat", lonlat)):
        nm = "__点序测试_" + label
        linedesk._save_geom(nm, 990000 + len(label), "unit-test",
                            [lonlat], chain, 100.0, {"test": 1}, raw_parts=[lonlat])
        c = linedesk.conn()
        try:
            r = c.execute("SELECT spine_gz,parts_gz FROM ld_geom WHERE name=?",
                          (nm,)).fetchone()
        finally:
            c.close()
        spine = json.loads(gzip.decompress(r["spine_gz"]).decode())
        parts = json.loads(gzip.decompress(r["parts_gz"]).decode())
        ck(spine == lonlat, "%s chain → spine_gz 是 [lon,lat]" % label)
        ck(parts == [lonlat], "%s raw_parts → parts_gz 是 [lon,lat]" % label)
finally:
    linedesk.DB_PATH = orig
    gc.collect()                    # Windows 上要等 sqlite 连接回收才能删临时库
    tmpdir.cleanup()

print("== 5 正式库：4 条确认线路走向全部 [lon,lat]，沪昆线用原始多段 ==")
CONFIRMED = [(24130, "京沪线"), (24131, "京沪高速线"),
             (24285, "沪昆线"), (24686, "陇海线")]
for lid, nm in CONFIRMED:
    g = linedesk.merged_geom(lid, nm)
    if not g or not g.get("parts"):
        ck(False, nm + "：merged_geom 有折线")
        continue
    p = g["parts"]
    pts = all_points(p)
    ck(all(is_lonlat(x) for x in pts[:200]),
       "%s：merged_geom 抽样 200 点全是 [lon,lat]（%d 段 / %d 点）"
       % (nm, len(p), len(pts)))
    d = linedesk.desk_geom(nm, lid)
    ck(bool(d) and all(is_lonlat(x) for x in all_points(d["parts"])[:200]),
       nm + "：desk_geom（整理台地图）同样正确")

hk = linedesk.merged_geom(24285, "沪昆线")
ck(len(hk["parts"]) == 47 and len(all_points(hk["parts"])) == 10670,
   "沪昆线优先原始多段 47 段 / 10670 点，不再用抽稀的 1 段 224 点 spine")

print("== 6 走向与停站落在同一片区域（不再一上一下）==")
c = linedesk.conn()
try:
    rows = [dict(x) for x in c.execute(
        "SELECT lat,lon FROM ld_stop WHERE line_id=24285 AND lat IS NOT NULL")]
finally:
    c.close()
st_lon = [r["lon"] for r in rows]
st_lat = [r["lat"] for r in rows]
gpts = all_points(hk["parts"])
ck(abs(min(p[0] for p in gpts) - min(st_lon)) < 3
   and abs(max(p[0] for p in gpts) - max(st_lon)) < 3,
   "走向经度范围与停站一致")
ck(abs(min(p[1] for p in gpts) - min(st_lat)) < 3
   and abs(max(p[1] for p in gpts) - max(st_lat)) < 3,
   "走向纬度范围与停站一致")

print("== 7 HTTP /api/line/osm?confirmed=1 同样契约 ==")
for _, nm in CONFIRMED:
    url = ("http://127.0.0.1:8686/api/line/osm?q="
           + urllib.parse.quote(nm) + "&confirmed=1")
    try:
        d = json.loads(urllib.request.urlopen(url, timeout=20).read().decode())
    except Exception as e:                       # noqa: BLE001
        skip(nm + "：服务未启动（%s）" % type(e).__name__)
        continue
    p = d.get("parts") or []
    ck(bool(p) and all(is_lonlat(x) for x in all_points(p)[:200]),
       nm + "：HTTP 出口 [lon,lat]（%d 段）" % len(p))

print("\n绿 %d / 红 %d / 跳过 %d" % (OK, FAIL, SKIP))
sys.exit(1 if FAIL else 0)
