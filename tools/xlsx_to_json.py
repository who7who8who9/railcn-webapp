# -*- coding: utf-8 -*-
"""
把 cnrail_data_v2.xlsx（火车站 / 铁路线 / 站点线路关联）导出为便于程序使用的 JSON。

产出（默认写到 D:/railcn/data/）：
  cnrail_stations.json   车站档案（按 12306 风格站名索引，站名去掉「站/所」后缀）
  cnrail_lines.json      铁路线：线路档案 + 途经站点（含累计里程）
  cnrail_index.json      极简索引 {站名: [lat, lon]}，给查询/渲染直接用
  cnrail_meta.json       生成参数、坐标修正模型、覆盖率统计

坐标修正
---------
原始表的经纬度存在系统性偏差：data = s * true + t（s≈1.016 / 1.012，t≈-5.12 / +1.11）。
用本地 OSM 车站坐标（WGS-84，准确）作基准做稳健线性回归拟合，反解出真实经纬度，
中位残差约 0.0005°（~50 m）。OSM 库里有的站直接沿用 OSM，其余用修正后的值。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
RAILCN = os.path.dirname(HERE)
DEFAULT_XLSX = os.path.join(RAILCN, "cnrail_data_v2.xlsx")
DEFAULT_OUT = os.path.join(RAILCN, "data")
WEBAPP_DATA = os.path.join(RAILCN, "webapp", "data")


# --------------------------------------------------------------------------
# 坐标修正模型
# --------------------------------------------------------------------------

def robust_fit(pairs, tol=0.6, iters=8):
    """pairs: [(data_x, true_x)]，迭代剔除离群后最小二乘拟合 data = s*true + t"""
    cur = list(pairs)
    keep = []
    for _ in range(iters):
        d = sorted((x - y) for x, y in cur)
        med = statistics.median(d)
        keep = [p for p in cur if abs((p[0] - p[1]) - med) < tol]
        if not keep:
            break
        cur = keep
    if len(cur) < 30:
        return 1.0, 0.0, 0
    xs = [p[1] for p in cur]
    ys = [(p[0] - p[1]) for p in cur]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    s = (sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den) if den else 0.0
    t = my - s * mx
    return s, t, len(cur)


def load_osm_coords():
    """本地 OSM 派生坐标库 {站名: [lat, lon]}（WGS-84，准确）"""
    for p in (os.path.join(WEBAPP_DATA, "station_coords.json"),
              os.path.join(DEFAULT_OUT, "station_coords.json")):
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return json.load(f), p
    return {}, None


def fit_correction(rows, osm):
    """rows: [(name, lon, lat)] —— 用 OSM 同名站做基准拟合修正模型"""
    plon, plat = [], []
    for name, lon, lat in rows:
        a = osm.get(name)
        if a:
            plon.append((lon, a[1]))
            plat.append((lat, a[0]))
    s_lon, t_lon, n1 = robust_fit(plon)
    s_lat, t_lat, n2 = robust_fit(plat)
    return {"lonScale": s_lon, "lonOffset": t_lon, "latScale": s_lat,
            "latOffset": t_lat, "samples": min(n1, n2)}


def make_corrector(model):
    s_lon, t_lon = model["lonScale"], model["lonOffset"]
    s_lat, t_lat = model["latScale"], model["latOffset"]

    def fix(lon, lat):
        return round((lon - t_lon) / (1.0 + s_lon), 6), round((lat - t_lat) / (1.0 + s_lat), 6)

    return fix


# --------------------------------------------------------------------------
# 线路几何校验：用「前后站 + 里程」推断该站应该在哪，纠正错位坐标
# --------------------------------------------------------------------------

def haversine(lat1, lon1, lat2, lon2):
    R = 6371.0
    p = math.radians
    dphi, dlam = p(lat2 - lat1), p(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p(lat1)) * math.cos(p(lat2)) * math.sin(dlam / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def repair_coords(coords, lines, rounds=3, min_dev_km=60.0):
    """
    对每条线，用前后两站的坐标 + 里程线性插值出中间站的理论位置；
    实际位置偏离理论位置超过 min_dev_km 且超过前后站弦长的，判为错位并替换。
    返回被修正的站名集合。
    """
    fixed = set()
    for _ in range(rounds):
        changed = 0
        suggestions = defaultdict(list)      # key -> [(dev, lat, lon)]
        for ln in lines:
            st = ln["stations"]
            for i in range(1, len(st) - 1):
                a, b, c = st[i - 1], st[i], st[i + 1]
                ka, kb, kc = station_key(a["name"]), station_key(b["name"]), station_key(c["name"])
                if None in (a["km"], b["km"], c["km"]) or kc == ka:
                    continue
                if not (a["km"] < b["km"] < c["km"]):
                    continue
                if ka not in coords or kb not in coords or kc not in coords:
                    continue
                (la, oa), (lb, ob), (lc, oc) = coords[ka], coords[kb], coords[kc]
                chord = haversine(la, oa, lc, oc)
                if chord < 8:                # 前后站过近，插值无意义
                    continue
                t = (b["km"] - a["km"]) / (c["km"] - a["km"])
                elat, elon = la + (lc - la) * t, oa + (oc - oa) * t
                dev = haversine(lb, ob, elat, elon)
                if dev > min_dev_km and dev > chord * 0.9:
                    suggestions[kb].append((dev, elat, elon))
        for key, sug in suggestions.items():
            sug.sort()
            dev, elat, elon = sug[len(sug) // 2]
            coords[key] = (round(elat, 6), round(elon, 6))
            fixed.add(key)
            changed += 1
        if not changed:
            break
    return fixed


# --------------------------------------------------------------------------
# 名称 / 行政区划归一
# --------------------------------------------------------------------------

def station_key(name: str) -> str:
    """12306 风格站名：去掉「站」「线路所」「所」等后缀"""
    n = (name or "").strip()
    for suf in ("线路所", "站", "所"):
        if len(n) > len(suf) and n.endswith(suf):
            return n[: -len(suf)]
    return n


PROVINCES = ("北京市", "天津市", "上海市", "重庆市", "河北省", "山西省", "辽宁省",
             "吉林省", "黑龙江省", "江苏省", "浙江省", "安徽省", "福建省", "江西省",
             "山东省", "河南省", "湖北省", "湖南省", "广东省", "海南省", "四川省",
             "贵州省", "云南省", "陕西省", "甘肃省", "青海省", "台湾省",
             "内蒙古自治区", "广西壮族自治区", "西藏自治区", "宁夏回族自治区",
             "新疆维吾尔自治区", "中国香港", "中国澳门", "香港特别行政区", "澳门特别行政区")


def split_loc(loc: str):
    """'中国 广东省 东莞市' / '新疆维吾尔自治区 哈密市 伊州区' -> (省, 市, 区县)"""
    parts = [p for p in (loc or "").split() if p]
    if parts and parts[0] in ("中国", "中华人民共和国"):
        parts = parts[1:]
    if not parts:
        return None, None, None
    if parts[0] not in PROVINCES:          # 境外：老挝 / 朝鲜 …
        return (parts[0], parts[1] if len(parts) > 1 else None, None)
    prov = parts[0]
    # 直辖市：省=市，第二级即为区县
    if prov in ("北京市", "天津市", "上海市", "重庆市"):
        return prov, prov, (parts[1] if len(parts) > 1 else None)
    city = parts[1] if len(parts) > 1 else None
    dist = parts[2] if len(parts) > 2 else None
    return prov, city, dist


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--xlsx", default=DEFAULT_XLSX)
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()

    import openpyxl

    wb = openpyxl.load_workbook(args.xlsx, read_only=True, data_only=True)
    os.makedirs(args.out, exist_ok=True)

    # ---------- 铁路线 ----------
    lines, line_by_name = [], {}
    for r in wb["铁路线"].iter_rows(min_row=2, values_only=True):
        eid, name, nline, nstop, stops_s, dist_s = (list(r) + [None] * 6)[:6]
        if not name:
            continue
        stops = [s.strip() for s in str(stops_s or "").split(",") if s.strip()]
        dists = []
        for d in str(dist_s or "").split("->"):
            d = d.strip()
            try:
                dists.append(float(d))
            except ValueError:
                dists.append(None)
        pts = []
        for i, s in enumerate(stops):
            km = dists[i] if i < len(dists) else None
            pts.append({"name": s, "km": km})
        nums = [p["km"] for p in pts if p["km"] is not None]
        lines.append({
            "eid": eid, "name": str(name).strip(), "n": len(stops),
            "stations": pts,
            "totalKm": round(max(nums), 1) if nums else None,
            "kmComplete": len(nums) == len(stops),
            "tracks": str(nline).strip() if nline else None,
        })
        line_by_name[str(name).strip()] = lines[-1]

    # 线路名模糊解析：京沪高速 -> 京沪高速线
    def resolve_line(n):
        n = (n or "").strip()
        if not n:
            return None
        if n in line_by_name:
            return n
        for cand in (n + "线", n + "铁路", n[:-1] if n.endswith("线") else n):
            if cand in line_by_name:
                return cand
        return None

    # ---------- 站点-线路关联 ----------
    rel_by_kid = defaultdict(list)
    line_color = {}
    for r in wb["站点线路关联"].iter_rows(min_row=2, values_only=True):
        kid, sname, eid, lname, color = (list(r) + [None] * 5)[:5]
        if not kid or not lname:
            continue
        rel_by_kid[kid].append(str(lname).strip())
        if color:
            line_color[str(lname).strip()] = color

    # ---------- 火车站 ----------
    raw = []
    for r in wb["火车站"].iter_rows(min_row=2, values_only=True):
        (kid, name, en, typ, loc, lon, lat, opened, bureau,
         tele, nlink, links, services, wikidata, *_rest) = (list(r) + [None] * 14)[:14]
        if not name:
            continue
        try:
            lon_f, lat_f = float(lon), float(lat)
        except (TypeError, ValueError):
            continue
        key = station_key(str(name))
        prov, city, dist = split_loc(str(loc))
        link_list = [x.strip() for x in str(links or "").split(",") if x.strip()]
        svc = [x.strip() for x in str(services or "").split(",") if x.strip()]
        raw.append({
            "kid": kid, "name": str(name).strip(), "key": key, "en": en,
            "type": str(typ).strip() if typ else None,
            "loc": str(loc).strip() if loc else None,
            "province": prov, "city": city, "district": dist,
            "lon": lon_f, "lat": lat_f,
            "opened": str(opened).strip() if opened else None,
            "bureau": bureau, "tele": tele,
            "links": link_list, "services": svc, "wikidata": wikidata,
        })

    # ---------- 拟合坐标修正 ----------
    osm, osm_path = load_osm_coords()
    model = fit_correction([(s["key"], s["lon"], s["lat"]) for s in raw], osm)
    fix = make_corrector(model)
    print(f"[coord] OSM 基准库: {osm_path or '未找到'} ({len(osm)} 站)")
    print(f"[coord] 修正模型: lon' = (lon - {model['lonOffset']:.5f}) / {1 + model['lonScale']:.6f}"
          f" | lat' = (lat - {model['latOffset']:.5f}) / {1 + model['latScale']:.6f}"
          f" | 样本 {model['samples']}")

    # ---------- 坐标：表内车站一律用「校正值」（与 OSM 吻合度 p95 = 0.6 km，
    #            且不会踩 OSM 同名重复站的坑）；表里没有的线路所等再用 OSM 兜底 ----------
    coords: dict[str, tuple[float, float]] = {}      # key -> (lat, lon)
    for s in raw:
        f_lon, f_lat = fix(s["lon"], s["lat"])        # fix() 返回 (lon, lat)
        coords[s["key"]] = (f_lat, f_lon)
    from_osm = 0
    for key in {station_key(p["name"]) for ln in lines for p in ln["stations"]}:
        if key not in coords and key in osm:
            coords[key] = (float(osm[key][0]), float(osm[key][1]))
            from_osm += 1

    fixed = repair_coords(coords, lines)
    print(f"[coord] 线路几何校验：修正 {len(fixed)} 个错位站点"
          f"（OSM 兜底 {from_osm} 个表外站）")
    in_table = {s["key"] for s in raw}

    stations, by_key = {}, {}
    for s in raw:
        key = s["key"]
        lat, lon = coords.get(key, (None, None))
        src = "interp" if key in fixed else "cnrail校正"
        rec = {k: s[k] for k in ("name", "key", "en", "type", "loc", "province",
                                 "city", "district", "opened", "bureau", "tele",
                                 "services", "wikidata")}
        raw_lines = sorted(set(s["links"] + rel_by_kid.get(s["kid"], [])))
        # 「京沪高速」和「京沪高速线」指向同一条线，统一收敛到线路表里的正式名
        canon, seen = [], set()
        for x in raw_lines:
            official = resolve_line(x) or x
            if official not in seen:
                seen.add(official)
                canon.append(official)
        rec.update({"lat": lat, "lon": lon, "coordSrc": src,
                    "lines": canon,
                    "lineIds": [l for l in (resolve_line(x) for x in raw_lines) if l]})
        prev = by_key.get(s["key"])
        # 同名（火车站 / 地铁站 / 线路所）取「火车站」优先、其次连接线路多的
        if prev is None or (rec["type"] == "火车站" and prev["type"] != "火车站") \
                or (rec["type"] == prev["type"] and len(rec["lines"]) > len(prev["lines"])):
            by_key[s["key"]] = rec
        stations[s["kid"]] = rec

    for k, v in by_key.items():
        v["key"] = k

    # ---------- 只在「铁路线」里出现的站（多为线路所）：补成精简档案，保证画线不缺站 ----------
    line_keys = {}
    for ln in lines:
        for p in ln["stations"]:
            k = station_key(p["name"])
            if k and k not in by_key:
                line_keys.setdefault(k, p["name"])
    added = 0
    for k, orig in line_keys.items():
        c = coords.get(k)
        by_key[k] = {
            "name": orig, "key": k, "en": None,
            "type": "线路所" if orig.endswith(("所", "线路所")) else "车站(仅线路表)",
            "loc": None, "province": None, "city": None, "district": None,
            "opened": None, "bureau": None, "tele": None, "services": [], "wikidata": None,
            "lat": c[0] if c else None, "lon": c[1] if c else None,
            "coordSrc": "interp" if k in fixed else ("osm" if c else None),
            "lines": [], "lineIds": [],
        }
        added += 1

    # ---------- 里程可信度校验 ----------
    # 部分线路的「站间距」列与站点列不对齐（值个数少于站点数），按位置解析会错位。
    # 用「里程 / 相邻站大圆距离之和」即弯曲系数判定：真实铁路应在 1.0~1.5 之间。
    k = 0
    for ln in lines:
        ln["kmReliable"] = False
        if not ln["kmComplete"]:
            continue
        chord, ok = 0.0, True
        for a, b in zip(ln["stations"], ln["stations"][1:]):
            ca, cb = coords.get(station_key(a["name"])), coords.get(station_key(b["name"]))
            if not ca or not cb:
                ok = False
                break
            chord += haversine(ca[0], ca[1], cb[0], cb[1])
        if not ok or not ln["totalKm"]:
            continue
        if chord < 40:                       # 联络线一类，短到无所谓的量级
            if ln["totalKm"] <= 80:
                ln["kmReliable"] = True
                ln["sinuosity"] = None
                k += 1
            continue
        sin = ln["totalKm"] / chord
        if 0.95 <= sin <= 1.5:
            ln["kmReliable"] = True
            ln["sinuosity"] = round(sin, 3)
            k += 1
        else:
            for p in ln["stations"]:
                p["km"] = None
            ln["kmComplete"] = False
            ln["kmReliable"] = False
    print(f"[km] 里程可信线路 {k} 条（弯曲系数 0.95~1.5）")

    # ---------- 线路补全颜色 + 反向索引 ----------
    for ln in lines:
        ln["color"] = line_color.get(ln["name"])
    line_station_index = defaultdict(list)
    for ln in lines:
        for p in ln["stations"]:
            line_station_index[station_key(p["name"])].append(ln["name"])
    # 反向补全：站 -> 所属线路（含「途经」关系）
    for k, ln_names in line_station_index.items():
        rec = by_key.get(k)
        if not rec:
            continue
        merged = rec["lines"] + [n for n in ln_names if n not in rec["lines"]]
        rec["lines"] = merged
    print(f"[lines] 表外站点补录 {added} 个")

    # ---------- 输出 ----------
    def dump(fn, obj):
        p = os.path.join(args.out, fn)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        print(f"[out] {p}  {os.path.getsize(p)/1024:.0f} KB")

    dump("cnrail_stations.json", by_key)
    dump("cnrail_lines.json", lines)
    dump("cnrail_index.json", {k: [v["lat"], v["lon"]] for k, v in by_key.items()})

    types = Counter(v["type"] for v in by_key.values())
    provinces = Counter(v["province"] for v in by_key.values())
    meta = {
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": os.path.basename(args.xlsx),
        "stations": len(by_key),
        "stationsWithCoord": sum(1 for v in by_key.values() if v["lat"] is not None),
        "coordFromOsm": from_osm,
        "coordFromCnrail": len(by_key) - from_osm,
        "lines": len(lines),
        "linesWithFullKm": sum(1 for l in lines if l["kmComplete"]),
        "stationLineLinks": sum(len(v["lines"]) for v in by_key.values()),
        "types": dict(types.most_common()),
        "topProvinces": dict(provinces.most_common(12)),
        "coordCorrection": model,
    }
    dump("cnrail_meta.json", meta)

    print(f"[stats] 车站 {len(by_key)} | 铁路线 {len(lines)} "
          f"(里程完整 {meta['linesWithFullKm']}) | 坐标来源 OSM {from_osm} / 校正 {len(by_key) - from_osm}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
