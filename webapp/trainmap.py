# -*- coding: utf-8 -*-
"""
trainmap.py — 全路网列车「当前位置」地图的数据层

【重要口径】12306 不对外提供列车 GPS，本模块算出来的是**按时刻表推算的位置**：
    绝对时刻 = 发车日 00:00 + 该站 arrive_time / start_time（含跨天 arrive_day_diff）
    区间内   = 上游站坐标 + (下游站坐标 − 上游站坐标) × 已过时间 / 区间运行时间
    停站时   = 钉在车站上
没有晚点/正晚点数据，晚点车会有偏差；页面上必须写明「时刻表推算，非实时定位」。

数据全部来自本地库，离线可用：
    train_universe  车次全集（code / train_no / 起讫站）
    route_tpl       停站时刻模板（同一 train_no 跨日期复用，取最新一份）
    route_day       哪些车次在哪天真的开行（一次开行 = 一个实例）
    station_coords / coord_override / station_master → 经 cnrail + railcore.stations 取坐标

索引一次构建常驻内存；快照按「分钟」缓存，回放时间轴不会反复扫全库。
"""
from __future__ import annotations

import bisect
import datetime as dt
import json
import math
import os
import re
import sqlite3
import threading
import time
from collections import Counter, defaultdict

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "rail.db")

# 开行窗口：今天往前 2 天（接住跨天长途车）、往后 3 天（时间轴回放）
PAST_DAYS = 2
FUTURE_DAYS = 3
# 没有登记开行、但前后几天开过的车，按「推定开行」补进来（页面会标出来）
ASSUME_RUNNING = True

_HM = re.compile(r"^\s*(\d{1,2}):(\d{2})")
_IDX = None
_IDX_LOCK = threading.RLock()
_SNAP: dict = {}                 # key -> (ts, payload)
_SNAP_LOCK = threading.Lock()
_SNAP_TTL = 60.0

INDEX_STATS = {}                 # 供 /api/trainmap/stats 展示构建情况


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def _hm(s: str | None) -> int | None:
    """'08:35' / '8:35' → 分钟；'----'、空、坏值 → None"""
    if not s:
        return None
    m = _HM.match(str(s))
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 47 or mi > 59:
        return None
    return h * 60 + mi


def _day(s) -> int:
    try:
        return max(0, min(9, int(s or 0)))
    except (TypeError, ValueError):
        return 0


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _parse_t(now: dt.datetime | str | None) -> dt.datetime:
    """前端可传 ISO / 'YYYY-MM-DD HH:MM' / Unix 秒 / 空（=现在）"""
    if now is None or now == "" or now == "now":
        return dt.datetime.now()
    if isinstance(now, dt.datetime):
        return now
    s = str(now).strip()
    if s.replace(".", "", 1).isdigit() and len(s) >= 9:      # Unix 秒
        return dt.datetime.fromtimestamp(float(s))
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00").replace("T", " "))
    except ValueError:
        pass
    for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return dt.datetime.strptime(s, f)
        except ValueError:
            continue
    return dt.datetime.now()


# --------------------------------------------------------------------------
# 模板解析：raw（12306 原始经停站）→ 紧凑时刻表
# --------------------------------------------------------------------------
class Tpl:
    __slots__ = ("code", "train_no", "type", "from_sta", "to_sta",
                 "start", "end", "stops", "deps", "arrs", "names", "total_km")

    def __init__(self, code, train_no, typ, fro, to, stops):
        self.code, self.train_no, self.type = code, train_no, typ
        self.from_sta, self.to_sta = fro, to
        # stops: [(name, lat, lon, arr, dep), ...]  arr/dep = 相对发车日 00:00 的分钟
        self.stops = stops
        self.arrs = [s[3] for s in stops]
        self.deps = [s[4] for s in stops]
        self.names = [s[0] for s in stops]
        self.start = self.deps[0]
        self.end = self.arrs[-1] if self.arrs[-1] is not None else self.deps[-1]
        km = 0.0
        for i in range(1, len(stops)):
            a, b = stops[i - 1], stops[i]
            if a[1] is not None and b[1] is not None:
                km += _haversine(a[1], a[2], b[1], b[2]) * 1.25
        self.total_km = round(km, 1)


def _parse_raw(raw: str, stations) -> list | None:
    """解析 12306 raw；返回 [(name, lat, lon, arr, dep)]，坏数据返回 None"""
    try:
        src = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(src, list) or len(src) < 2:
        return None
    out, prev_dep = [], None
    for i, s in enumerate(src):
        name = (s.get("station_name") or "").strip()
        if not name:
            return None
        d = _day(s.get("arrive_day_diff"))
        arr = _hm(s.get("arrive_time"))
        dep = _hm(s.get("start_time"))
        if arr is not None:
            arr += d * 1440
        if dep is not None:
            dep += d * 1440
            # 停站跨午夜：出发钟点小于到达钟点，且没标跨天 → 补一天
            if arr is not None and dep < arr:
                dep += 1440
        if i == 0:
            if dep is None:
                return None
            arr = dep
        if i == len(src) - 1 and dep is None:
            dep = arr
        # 全程时刻必须单调不减；缺字段就沿用邻近值
        if arr is None:
            arr = prev_dep if prev_dep is not None else 0
        if prev_dep is not None and arr < prev_dep:
            while arr < prev_dep:
                arr += 1440
            if dep is not None and dep < arr:
                dep = arr
        if dep is None:
            dep = arr
        if dep < arr:
            dep = arr
        coord = stations.coord(name)
        lat, lon = (coord[0], coord[1]) if coord else (None, None)
        out.append((name, lat, lon, arr, dep))
        prev_dep = dep
    if out[-1][3] <= out[0][4]:
        return None
    return out


# --------------------------------------------------------------------------
# 索引构建
# --------------------------------------------------------------------------
class Index:
    __slots__ = ("templates", "instances", "by_tn", "codes", "dates", "built_at", "fp")

    def __init__(self):
        self.templates: dict[str, Tpl] = {}      # train_no -> Tpl
        self.instances: list[tuple] = []         # (train_no, code, date, assumed)
        self.by_tn: dict[str, list[int]] = {}    # train_no -> instances 下标
        self.codes: set[str] = set()             # 用于「G1」精确匹配判断
        self.dates: list[str] = []
        self.built_at = 0.0
        self.fp = None


def _fingerprint() -> tuple:
    """route_day / route_tpl / train_universe 变了才重建（远端脚本直接写库也能发现）"""
    with _conn() as c:
        a = c.execute("SELECT COUNT(*), MAX(updated_at) FROM route_day").fetchone()
        b = c.execute("SELECT COUNT(*), MAX(created_at) FROM route_tpl").fetchone()
        d = c.execute("SELECT COUNT(*) FROM train_universe").fetchone()
    return (a[0], a[1], b[0], b[1], d[0])


def build(force: bool = False) -> Index:
    global _IDX
    fp = _fingerprint()
    with _IDX_LOCK:
        if _IDX is not None and not force and _IDX.fp == fp and time.time() - _IDX.built_at < 300:
            return _IDX
        t0 = time.time()
        idx = Index()
        idx.fp = fp
        idx.built_at = time.time()
        today = dt.date.today()
        dates = [(today + dt.timedelta(days=i)).isoformat()
                 for i in range(-PAST_DAYS, FUTURE_DAYS + 1)]
        idx.dates = dates
        lo, hi = dates[0], dates[-1]

        import railcore
        stations = railcore.stations

        with _conn() as c:
            # 1) 模板：同一 train_no 取 id 最大（最新）的一份 raw
            raws: dict[str, str] = {}
            for r in c.execute("SELECT train_no, raw FROM route_tpl ORDER BY id"):
                raws[r["train_no"]] = r["raw"]
            # 2) 车次全集
            uni = c.execute(
                "SELECT code, train_no, type, start_sta, end_sta FROM train_universe"
            ).fetchall()
            # 3) 窗口内实际登记的开行
            runs = c.execute(
                "SELECT train_no, date, code FROM route_day WHERE date BETWEEN ? AND ?",
                (lo, hi),
            ).fetchall()

        parsed: dict[str, list] = {}
        for r in uni:
            tn = r["train_no"]
            if not tn or tn not in raws:
                continue
            if tn not in parsed:
                parsed[tn] = _parse_raw(raws[tn], stations)
            stops = parsed[tn]
            if not stops:
                continue
            fro = (r["start_sta"] or stops[0][0]).strip()
            to = (r["end_sta"] or stops[-1][0]).strip()
            idx.templates[tn] = Tpl(r["code"], tn, r["type"] or "", fro, to, stops)

        have = defaultdict(set)
        for r in runs:
            tn, date, code = r["train_no"], r["date"], (r["code"] or "").upper()
            if tn not in idx.templates:
                continue
            have[date].add((tn, code))
            code = code or idx.templates[tn].code
            idx.instances.append((tn, code, date, 0))

        # 推定开行：某车次在窗口里前后几天都开、就偏偏这天没登记（多半是还没下完）
        if ASSUME_RUNNING:
            assumed = 0
            for date in dates:
                neighbours = set()
                for d2 in dates:
                    if d2 != date:
                        neighbours |= have[d2]
                for tn, code in neighbours:
                    if (tn, code) not in have[date]:
                        idx.instances.append((tn, code, date, 1))
                        assumed += 1
            INDEX_STATS["assumed"] = assumed

        for i, inst in enumerate(idx.instances):
            idx.by_tn.setdefault(inst[0], []).append(i)
            idx.codes.add(inst[1].upper())
        INDEX_STATS.update({
            "templates": len(idx.templates),
            "instances": len(idx.instances),
            "realRuns": sum(1 for x in idx.instances if not x[3]),
            "dates": dates,
            "buildSec": round(time.time() - t0, 2),
            "builtAt": dt.datetime.now().isoformat(timespec="seconds"),
        })
        _IDX = idx
        return idx


def ensure() -> Index:
    if _IDX is None:
        # 不要 force：预热线程可能正在构建，这里排队等它完成再直接复用（锁在 build 内部）
        return build()
    # 闲时 30 秒最多验一次指印（指纹查询本身很便宜）
    if time.time() - _IDX.built_at > 30 and _fingerprint() != _IDX.fp:
        return build(force=True)
    return _IDX


# --------------------------------------------------------------------------
# 位置推算
# --------------------------------------------------------------------------
def _locate(tpl: Tpl, local: float) -> dict | None:
    """local = 相对该车发车日 00:00 的分钟；返回这一刻的状态与坐标"""
    deps = tpl.deps
    i = bisect.bisect_right(deps, local) - 1     # 最后一座已经发车的站
    if i < 0 or i >= len(tpl.stops) - 1:
        return None                              # 未发车 / 已终到
    a, b = tpl.stops[i], tpl.stops[i + 1]
    arr_b = b[3]
    if arr_b is None or arr_b <= a[4]:
        return None
    stop = local <= arr_b                        # 已经到下一站、还没再开 = 停靠
    running = local < arr_b
    if running:
        span = arr_b - a[4]
        pct = 0.0 if span <= 0 else max(0.0, min(1.0, (local - a[4]) / span))
        prev, nxt = i, i + 1
    else:
        pct, prev, nxt = 1.0, i + 1, i + 1
    if b[1] is None or a[1] is None:
        return None                              # 段两端缺坐标，不画（前端会看到 noCoord 计数）
    if running:
        lat = a[1] + (b[1] - a[1]) * pct
        lon = a[2] + (b[2] - a[2]) * pct
    else:
        lat, lon = b[1], b[2]
    seg_km = _haversine(a[1], a[2], b[1], b[2]) * 1.25
    hours = (arr_b - a[4]) / 60
    speed = min(400, round(seg_km / hours)) if hours > 0.05 else 0
    return {
        "phase": "run" if running else "stop",
        "lat": round(lat, 5), "lon": round(lon, 5),
        "prev": a[0], "next": b[0], "at": b[0] if not running else None,
        "pct": round(pct * 100, 1),
        "speed": int(speed),
        "etaMin": max(0, round(arr_b - local)) if running else 0,
        "segKm": round(seg_km, 1),
        "stopIdx": i + 1, "nStops": len(tpl.stops),
    }


def _fmt_clock(abs_min: int) -> str:
    return f"{int(abs_min // 60) % 24:02d}:{int(abs_min % 60):02d}"


# --------------------------------------------------------------------------
# 快照
# --------------------------------------------------------------------------
def snapshot(t=None, q="", types="", status="in", station="",
             fro="", to="", bbox=None, limit=4000) -> dict:
    """某一时刻全路网在途列车的位置清单（时刻表推算）"""
    now = _parse_t(t)
    q = (q or "").strip().upper()
    types_arg = (types or "").strip()            # 缓存键用原始参数：'other' 解析后会变成空集，
    types = set(x.strip().upper() for x in types_arg.split(",") if x.strip())
                                                 # 否则会和「不筛车种」撞同一个 key
    other_type = "OTHER" in types                 # 「其他车种」= 首字母不属于已知集合
    types.discard("OTHER")
    # 2026-11-25 改号新增 V/N/X/B（Y 原本就有）：勾「其他车种」时不该把它们
    # 一起算进来；这里只判断「字头是否已知」，不按字头推断车种 / 速度。
    known_kinds = set("GDCZTKSVNYXB0123456789")
    station = (station or "").strip()
    fro, to = (fro or "").strip(), (to or "").strip()
    key = (now.strftime("%Y-%m-%d %H:%M"), q, types_arg, status,
           station, fro, to, tuple(bbox) if bbox else None, limit)
    with _SNAP_LOCK:
        hit = _SNAP.get(key)
        if hit and time.time() - hit[0] < _SNAP_TTL:
            return hit[1]

    t0 = time.time()
    idx = ensure()
    base = now.date()
    mins = now.hour * 60 + now.minute + now.second / 60
    items, stats = [], Counter()
    by_type = Counter()
    no_coord = 0
    box = None
    exact = bool(q) and q in idx.codes     # 搜「G1」就只要 G1 这一趟，别把 G100/G1001 全捞进来
    if bbox:
        try:
            box = tuple(float(x) for x in bbox)
        except (TypeError, ValueError):
            box = None

    for tn, code, date, assumed in idx.instances:
        tpl = idx.templates.get(tn)
        if tpl is None:
            stats["noTemplate"] += 1
            continue
        # ---- 先做不依赖位置的便宜筛选 ----
        if types or other_type:
            kind = code[:1].upper()
            if other_type:
                if kind in known_kinds:
                    continue
            elif kind not in types:
                continue
        if q:
            if exact:
                if code.upper() != q:
                    continue
            elif not (code.upper().startswith(q) or q in tpl.from_sta or q in tpl.to_sta):
                continue
        if fro and fro not in tpl.from_sta:
            continue
        if to and to not in tpl.to_sta:
            continue
        if station and station not in tpl.from_sta and station not in tpl.to_sta \
                and not any(station in n for n in tpl.names):
            continue
        offset = (dt.date.fromisoformat(date) - base).days * 1440
        local = mins - offset
        if local < tpl.start or local > tpl.end:
            continue
        pos = _locate(tpl, local)
        if pos is None:
            if local < tpl.end:
                no_coord += 1
            continue
        if status == "run" and pos["phase"] != "run":
            continue
        if status == "stop" and pos["phase"] != "stop":
            continue
        if box:
            lo1, la1, lo2, la2 = box
            if not (lo1 <= pos["lon"] <= lo2 and la1 <= pos["lat"] <= la2):
                continue
        stats["total"] += 1
        stats[pos["phase"]] += 1
        if assumed:
            stats["assumed"] += 1
        by_type[code[:1] if code[:1].isalpha() else "0"] += 1
        if len(items) >= limit:
            stats["truncated"] += 1
            continue
        items.append({
            "code": code, "from": tpl.from_sta, "to": tpl.to_sta,
            "date": date, "trainNo": tn,
            "phase": pos["phase"], "lat": pos["lat"], "lon": pos["lon"],
            "prev": pos["prev"], "next": pos["next"],
            "pct": pos["pct"], "speed": pos["speed"], "etaMin": pos["etaMin"],
            "stopIdx": pos["stopIdx"], "nStops": pos["nStops"],
            "assumed": bool(assumed),
            "clock": _fmt_clock(local),
        })

    items.sort(key=lambda x: (x["phase"] != "run", x["code"]))
    out = {
        "t": now.isoformat(timespec="seconds"),
        "minute": now.strftime("%Y-%m-%d %H:%M"),
        "total": stats["total"], "returned": len(items),
        "truncated": stats.get("truncated", 0),
        "stats": {"run": stats["run"], "stop": stats["stop"],
                  "assumed": stats["assumed"], "noCoord": no_coord,
                  "byType": dict(by_type.most_common())},
        "scope": {"templates": INDEX_STATS.get("templates", 0),
                  "instances": INDEX_STATS.get("instances", 0),
                  "dates": idx.dates,
                  "buildSec": INDEX_STATS.get("buildSec", 0)},
        "elapsed": round(time.time() - t0, 3),
        "note": "位置按 12306 时刻表推算（区间内匀速直线插值），不是 GPS 实时定位；晚点会有偏差。",
        "items": items,
    }
    with _SNAP_LOCK:
        _SNAP.clear() if len(_SNAP) > 40 else None
        _SNAP[key] = (time.time(), out)
    return out


# --------------------------------------------------------------------------
# 单车详情 / 轨迹（点标记后侧栏用）
# --------------------------------------------------------------------------
def train_detail(code: str, date: str = "", t=None) -> dict:
    idx = ensure()
    code = (code or "").upper().strip()
    date = date or dt.date.today().isoformat()
    tpl = None
    for x in idx.instances:
        if x[1] == code and x[2] == date:
            tpl = idx.templates.get(x[0])
            break
    if tpl is None:
        for tn, tp in idx.templates.items():
            if tp.code == code:
                tpl = tp
                break
    if tpl is None:
        return {"error": f"找不到 {code} 的停站时刻"}
    now = _parse_t(t)
    offset = (dt.date.fromisoformat(date) - now.date()).days * 1440
    local = now.hour * 60 + now.minute - offset
    pos = _locate(tpl, local) if tpl.start <= local <= tpl.end else None
    stops = [{
        "i": i + 1, "name": s[0], "lat": s[1], "lon": s[2],
        "arr": _fmt_clock(s[3]) if s[3] is not None else "",
        "dep": _fmt_clock(s[4]) if s[4] is not None else "",
        "day": int(s[3] // 1440) if s[3] is not None else 0,
    } for i, s in enumerate(tpl.stops)]
    return {
        "code": code, "from": tpl.from_sta, "to": tpl.to_sta, "date": date,
        "trainNo": tpl.train_no, "start": _fmt_clock(tpl.start),
        "end": _fmt_clock(tpl.end), "totalKm": tpl.total_km,
        "nStops": len(stops), "stops": stops, "pos": pos,
        "clock": _fmt_clock(local),
        "note": "时刻表推算，非 GPS 实时定位",
    }


# --------------------------------------------------------------------------
# 无底图降级用的车站散点（不画国界，避免领土表达风险）
# --------------------------------------------------------------------------
_BACKBONE = None


def backbone() -> dict:
    global _BACKBONE
    if _BACKBONE is not None:
        return _BACKBONE
    pts = []
    try:
        with open(os.path.join(BASE_DIR, "data", "station_coords.json"),
                  encoding="utf-8") as f:
            src = json.load(f)
        for _n, v in src.items():
            if isinstance(v, (list, tuple)) and len(v) >= 2:
                la, lo = float(v[0]), float(v[1])
                if 17 <= la <= 54 and 72 <= lo <= 136:
                    pts.append([round(lo, 3), round(la, 3)])
    except (OSError, ValueError):
        pass
    _BACKBONE = {"count": len(pts), "points": pts}
    return _BACKBONE


def stats() -> dict:
    idx = ensure()
    return dict(INDEX_STATS, cached=len(_SNAP), note="时刻表推算，非 GPS")


def invalidate() -> None:
    """库被批量改写后调用（下一个请求会重建索引）"""
    global _IDX
    with _IDX_LOCK:
        _IDX = None
    with _SNAP_LOCK:
        _SNAP.clear()
