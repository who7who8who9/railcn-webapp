# -*- coding: utf-8 -*-
"""
railcore.py — 数据与业务核心层

职责：
  1. 复用 D:\\railcn 下 trainclaw.py 的 TrainAPI（12306 客户端）
  2. SQLite 本地库：车次清单 / 经停站缓存 / 车站坐标
  3. 把 12306 原始数据加工成前端可直接绘制地图的结构：
     站点坐标 + 累计里程 + 绝对时刻

不依赖任何第三方 Web 框架；网络能力依赖 requests（系统 Python 已安装）。
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import sqlite3
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "rail.db")
COORDS_PATH = os.path.join(DATA_DIR, "station_coords.json")
STATIONS_PATH = os.path.join(DATA_DIR, "stations.json")
RAILCN_DIR = os.path.dirname(BASE_DIR)          # D:\railcn

if RAILCN_DIR not in sys.path:
    sys.path.insert(0, RAILCN_DIR)

import config as rail_config            # noqa: E402
import trainclaw                        # noqa: E402
import cnrail                           # noqa: E402   cnrail_data_v2 静态知识库

# ---------------------------------------------------------------------------
# 基础设施
# ---------------------------------------------------------------------------

os.makedirs(DATA_DIR, exist_ok=True)

_db_lock = threading.Lock()


_wal_ready = False


def db() -> sqlite3.Connection:
    global _wal_ready
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    # 切 WAL 要拿独占锁，只在进程内第一次做；后台扫描在写库时重复设置会互相等锁
    if not _wal_ready:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            _wal_ready = True
        except sqlite3.DatabaseError:
            pass
    return conn


def init_db() -> None:
    with _db_lock, db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS trains (
                code        TEXT NOT NULL,
                train_no    TEXT,
                type        TEXT,
                start_sta   TEXT,
                end_sta     TEXT,
                stop_count  INTEGER,
                date        TEXT,
                PRIMARY KEY (code, date)
            );
            CREATE INDEX IF NOT EXISTS idx_trains_code ON trains(code);
            CREATE INDEX IF NOT EXISTS idx_trains_sta  ON trains(start_sta, end_sta);

            CREATE TABLE IF NOT EXISTS routes (
                train_no    TEXT NOT NULL,
                date        TEXT NOT NULL,
                code        TEXT,
                raw         TEXT,          -- 12306 原始经停站 JSON
                updated_at  REAL,
                PRIMARY KEY (train_no, date)
            );

            CREATE TABLE IF NOT EXISTS coord_override (
                name        TEXT PRIMARY KEY,
                lat         REAL,
                lon         REAL
            );

            -- 人工校正：经由 / 总里程 / 备注（按车次，与日期无关）
            CREATE TABLE IF NOT EXISTS train_override (
                code        TEXT PRIMARY KEY,
                via_json    TEXT,          -- [{"line":"陇海线","from":"郑州","to":"兰州"}]
                total_km    REAL,          -- 手工总里程，各站按比例缩放
                note        TEXT,
                updated_at  REAL
            );

            -- 数据校对扫描结果（curate.py 生成）
            CREATE TABLE IF NOT EXISTS curate (
                code        TEXT PRIMARY KEY,
                date        TEXT,
                from_sta    TEXT,
                to_sta      TEXT,
                stops       INTEGER,
                via_text    TEXT,
                via_json    TEXT,
                via_unknown TEXT,
                via_source  TEXT,
                via_cov     REAL,
                km_mode     TEXT,
                km_matched  TEXT,
                total_km    REAL,
                missing     INTEGER,
                missing_json TEXT,
                issues      TEXT,
                scanned_at  REAL
            );
            CREATE INDEX IF NOT EXISTS idx_curate_issues ON curate(issues);
            """
        )


# ---------------------------------------------------------------------------
# 车站注册表 + 坐标
# ---------------------------------------------------------------------------

def _norm(name: str) -> str:
    """站名归一化：去空白、去末尾'站'"""
    if not name:
        return ""
    s = re.sub(r"\s+", "", str(name))
    return s


# ---------------------------------------------------------------------------
# 人工校正：经由 / 总里程 / 备注
# ---------------------------------------------------------------------------

def _clean_via(via) -> List[dict]:
    """把前端传来的经由整理成 [{line, from, to}]，丢掉没有线路名的空行"""
    out: List[dict] = []
    for seg in via or []:
        if isinstance(seg, str):
            seg = {"line": seg}
        if not isinstance(seg, dict):
            continue
        name = str(seg.get("line") or "").strip()
        if not name:
            continue
        out.append({
            "line": name,
            "from": _norm(seg.get("from") or ""),
            "to": _norm(seg.get("to") or ""),
        })
    return out


def _via_line_names(segs: List[dict]) -> List[str]:
    """区间列表 → 去连续重复的线路名列表（给「经由 京沪高速线」这类展示用）"""
    out: List[str] = []
    for s in segs or []:
        ln = (s or {}).get("line")
        if ln and (not out or out[-1] != ln):
            out.append(ln)
    return out


def get_override(code: str) -> Optional[dict]:
    code = (code or "").strip().upper()
    if not code:
        return None
    ov_map = _override_map()
    ov = ov_map.get(code)
    if not ov:
        return None
    return {"code": code, **ov}


_override_cache: Optional[Dict[str, dict]] = None


def _override_map() -> Dict[str, dict]:
    """整表缓存：批量扫描时每趟车都查一次库太慢（一万多次建连），这里只读一次"""
    global _override_cache
    if _override_cache is None:
        _override_cache = list_overrides()
    return _override_cache


def _drop_override_cache() -> None:
    global _override_cache
    _override_cache = None


def save_override(code: str, via, total_km=None, note: str = "") -> dict:
    """
    保存人工校正。via / total_km 都为空时视作撤销（删除记录）。
    """
    code = (code or "").strip().upper()
    if not code:
        raise ValueError("缺少车次号")
    clean = _clean_via(via)
    try:
        km = float(total_km) if total_km not in (None, "", 0) else None
    except (TypeError, ValueError):
        raise ValueError("总里程必须是数字")
    if km is not None and not (1 <= km <= 20000):
        raise ValueError("总里程超出合理范围（1~20000 km）")

    note = (note or "").strip()
    if not clean and km is None and not note:
        clear_override(code)
        return {"code": code, "cleared": True}

    with _db_lock, db() as conn:
        conn.execute(
            """INSERT INTO train_override(code, via_json, total_km, note, updated_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(code) DO UPDATE SET
                 via_json=excluded.via_json, total_km=excluded.total_km,
                 note=excluded.note, updated_at=excluded.updated_at""",
            (code, json.dumps(clean, ensure_ascii=False), km, note, time.time()),
        )
    _drop_override_cache()
    return {"code": code, "via": clean, "totalKm": km, "note": note}


def clear_override(code: str) -> bool:
    code = (code or "").strip().upper()
    with _db_lock, db() as conn:
        cur = conn.execute("DELETE FROM train_override WHERE code=?", (code,))
        hit = cur.rowcount > 0
    _drop_override_cache()
    return hit


def list_overrides() -> Dict[str, dict]:
    try:
        with db() as conn:
            rows = conn.execute(
                "SELECT code, via_json, total_km, note, updated_at FROM train_override"
            ).fetchall()
    except sqlite3.Error:
        return {}
    out: Dict[str, dict] = {}
    for row in rows:
        try:
            via = _clean_via(json.loads(row["via_json"] or "[]"))
        except (TypeError, ValueError):
            via = []
        out[row["code"]] = {
            "via": via, "totalKm": row["total_km"],
            "note": row["note"], "updatedAt": row["updated_at"],
        }
    return out


def set_coord(name: str, lat: float, lon: float) -> dict:
    """人工补录车站坐标（写 coord_override，并让坐标缓存失效重载）"""
    name = _norm(name)
    if not name:
        raise ValueError("缺少站名")
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        raise ValueError("经纬度必须是数字")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("经纬度超出范围")
    if lat == 0 and lon == 0:
        raise ValueError("(0,0) 多半是填错了")
    with _db_lock, db() as conn:
        conn.execute(
            """INSERT INTO coord_override(name, lat, lon) VALUES(?,?,?)
               ON CONFLICT(name) DO UPDATE SET lat=excluded.lat, lon=excluded.lon""",
            (name, lat, lon),
        )
    stations.reload_coords()
    return {"name": name, "lat": lat, "lon": lon, **stations.coord_info(name)}


def clear_coord(name: str) -> dict:
    """撤销某站的人工坐标，回到车站档案里的原值（原值也没有就是「缺失」）"""
    name = _norm(name)
    if not name:
        raise ValueError("缺少站名")
    with _db_lock, db() as conn:
        removed = conn.execute(
            "DELETE FROM coord_override WHERE name=?", (name,)
        ).rowcount > 0
    stations.reload_coords()
    info = stations.coord_info(name)
    return {"name": name, "removed": removed, **info}


class StationRegistry:
    def __init__(self) -> None:
        self.by_code: Dict[str, dict] = {}
        self.by_name: Dict[str, str] = {}          # name -> telecode
        self.coords: Dict[str, Tuple[float, float]] = {}   # name -> (lat, lon)，已含人工值
        self.base_coords: Dict[str, Tuple[float, float]] = {}   # 仅档案/OSM 的原值
        self.overrides: Dict[str, Tuple[float, float]] = {}     # 人工改过的站
        self.loaded = False

    def ensure(self) -> None:
        if self.loaded:
            return
        # 1) 车站字典（12306 station_name.js 的缓存副本）
        if os.path.exists(STATIONS_PATH):
            with open(STATIONS_PATH, encoding="utf-8") as f:
                self.by_code = json.load(f)
        else:
            trainclaw.station_manager.ensure_loaded()
            for code, st in trainclaw.station_manager._stations.items():
                self.by_code[code] = st.__dict__.copy()

        for code, st in self.by_code.items():
            name = _norm(st.get("station_name", ""))
            if name:
                self.by_name.setdefault(name, code)

        # 2) cnrail_data_v2 车站库（含路局/电报码/连接线路，坐标优先用它）
        base: Dict[str, Tuple[float, float]] = {}
        cnrail.load()
        for name, st in (cnrail._STATIONS or {}).items():
            if st.get("lat") is not None and st.get("lon") is not None:
                base[_norm(name)] = (float(st["lat"]), float(st["lon"]))

        # 3) OSM 派生坐标（补足 cnrail 没有的站）
        if os.path.exists(COORDS_PATH):
            with open(COORDS_PATH, encoding="utf-8") as f:
                raw = json.load(f)
            for name, latlon in raw.items():
                if not latlon:
                    continue
                base.setdefault(_norm(name), (float(latlon[0]), float(latlon[1])))

        # 4) 人工修正覆盖：单独存一份，编辑器要区分「档案原值」和「人工改过的值」
        overrides: Dict[str, Tuple[float, float]] = {}
        try:
            with db() as conn:
                for row in conn.execute("SELECT name, lat, lon FROM coord_override"):
                    overrides[_norm(row["name"])] = (row["lat"], row["lon"])
        except sqlite3.Error:
            pass

        self.base_coords = base
        self.overrides = overrides
        self.coords = dict(base)
        self.coords.update(overrides)

        self.loaded = True

    def coord(self, name: str) -> Optional[Tuple[float, float]]:
        self.ensure()
        n = _norm(name)
        if n in self.coords:
            return self.coords[n]
        for cand in (n[:-1] if n.endswith("站") else None, n + "站"):
            if cand and cand in self.coords:
                return self.coords[cand]
        return None

    def reload_coords(self) -> None:
        """人工补录坐标后重建缓存（车站字典不动，只重算坐标）"""
        self.coords = {}
        self.base_coords = {}
        self.overrides = {}
        self.loaded = False
        self.ensure()

    def _coord_key(self, name: str) -> str:
        """站名归一化，并兼容「XX」↔「XX站」两种写法"""
        n = _norm(name)
        if n in self.coords:
            return n
        for cand in (n[:-1] if n.endswith("站") else None, n + "站"):
            if cand and cand in self.coords:
                return cand
        return n

    def coord_info(self, name: str) -> dict:
        """单站坐标详情：当前值 / 来源（manual|base|none）/ 档案原值"""
        self.ensure()
        key = self._coord_key(name)
        cur = self.coords.get(key)
        base = self.base_coords.get(key)
        manual = key in self.overrides
        return {
            "name": name,
            "lat": cur[0] if cur else None,
            "lon": cur[1] if cur else None,
            "source": "manual" if manual else ("base" if cur else "none"),
            "baseLat": base[0] if base else None,
            "baseLon": base[1] if base else None,
        }

    def meta(self, name: str) -> Optional[dict]:
        """cnrail_data_v2 的车站档案：路局 / 电报码 / 连接线路 / 服务类型"""
        st = cnrail.station(name)
        if not st:
            return None
        return {
            "name": st.get("name"),
            "bureau": st.get("bureau"),
            "tele": st.get("tele"),
            "services": st.get("services") or [],
            "lines": st.get("lines") or [],
            "province": st.get("province"),
            "city": st.get("city"),
            "opened": st.get("opened"),
        }

    def telecode(self, name: str) -> Optional[str]:
        self.ensure()
        n = _norm(name)
        if n in self.by_name:
            return self.by_name[n]
        # 容错：站名带"站"
        if n.endswith("站") and n[:-1] in self.by_name:
            return self.by_name[n[:-1]]
        if n + "站" in self.by_name:
            return self.by_name[n + "站"]
        return None

    def canonical(self, name: str) -> str:
        code = self.telecode(name)
        if code and code in self.by_code:
            return _norm(self.by_code[code].get("station_name", name))
        return _norm(name)

    def suggest(self, q: str, limit: int = 12) -> List[dict]:
        self.ensure()
        q = _norm(q)
        if not q:
            return []
        out = []
        low = q.lower()
        for name, code in self.by_name.items():
            st = self.by_code.get(code, {})
            hit = 0
            if name == q:
                hit = 0
            elif name.startswith(q):
                hit = 1
            elif q in name:
                hit = 2
            elif st.get("station_pinyin", "").startswith(low) or st.get("station_short", "").startswith(low):
                hit = 3
            else:
                continue
            out.append((hit, len(name), name, st.get("city", ""), bool(self.coords.get(name))))
        out.sort()
        return [
            {"name": n, "city": city, "hasCoord": has}
            for _, _, n, city, has in out[:limit]
        ]


stations = StationRegistry()


# ---------------------------------------------------------------------------
# 距离与时间工具
# ---------------------------------------------------------------------------

def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """两点球面距离（公里）"""
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


_EMPTY_TIME = {"", "--", "----", "---", "-"}


def _parse_hhmm(value: str) -> Optional[int]:
    """'06:30' / '6:30' -> 分钟（自 00:00）"""
    if not value or str(value).strip() in _EMPTY_TIME:
        return None
    m = re.match(r"^\s*(\d{1,2})\s*:\s*(\d{1,2})\s*$", str(value))
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def _fmt_hhmm(minutes: int) -> str:
    minutes %= 24 * 60
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


# ---------------------------------------------------------------------------
# 12306 客户端（单例，带请求节流）
# ---------------------------------------------------------------------------

_api: Optional[trainclaw.TrainAPI] = None
_api_lock = threading.Lock()


def api() -> trainclaw.TrainAPI:
    global _api
    with _api_lock:
        if _api is None:
            _api = trainclaw.TrainAPI()
        return _api


# ---------------------------------------------------------------------------
# 车次搜索（本地库）
# ---------------------------------------------------------------------------

def search_trains(q: str, limit: int = 40, date: Optional[str] = None) -> List[dict]:
    q = (q or "").strip()
    if not q:
        return []
    stmt = """
        SELECT code, MAX(train_no) AS train_no, MAX(type) AS type,
               MAX(start_sta) AS start_sta, MAX(end_sta) AS end_sta,
               MAX(stop_count) AS stop_count, MAX(date) AS date
        FROM trains
        WHERE code LIKE ? OR start_sta LIKE ? OR end_sta LIKE ?
        GROUP BY code
        ORDER BY
          CASE WHEN code = ? THEN 0
               WHEN code LIKE ? THEN 1
               WHEN start_sta = ? THEN 2
               WHEN end_sta = ? THEN 3
               ELSE 4 END,
          LENGTH(code), code
        LIMIT ?
    """
    with db() as conn:
        rows = conn.execute(
            stmt,
            (q + "%", q + "%", q + "%", q.upper(), q.upper() + "%", q, q, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def lookup_train(code: str, date: Optional[str] = None) -> Optional[dict]:
    code = (code or "").strip().upper()
    with db() as conn:
        if date:
            row = conn.execute(
                "SELECT * FROM trains WHERE code = ? AND date = ?", (code, date)
            ).fetchone()
            if row:
                return dict(row)
        row = conn.execute(
            "SELECT * FROM trains WHERE code = ? ORDER BY date DESC LIMIT 1", (code,)
        ).fetchone()
        if row:
            return dict(row)
        # 合并车次（如 1461/4）用主号匹配
        main = code.split("/")[0]
        row = conn.execute(
            "SELECT * FROM trains WHERE code LIKE ? ORDER BY date DESC LIMIT 1",
            (main + "%",),
        ).fetchone()
        if row:
            return dict(row)
        # 反查：合并车次库里的 "1461/4" 包含 "1464"
        row = conn.execute(
            "SELECT * FROM trains WHERE code LIKE ? ORDER BY date DESC LIMIT 1",
            ("%/" + main[-2:] + "%",),
        ).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# 经停站：缓存优先，miss 则实时抓取
# ---------------------------------------------------------------------------

def _cache_get_route(train_no: str, date: str) -> Optional[List[dict]]:
    with db() as conn:
        row = conn.execute(
            "SELECT raw FROM routes WHERE train_no = ? AND date = ?", (train_no, date)
        ).fetchone()
    if row and row["raw"]:
        try:
            return json.loads(row["raw"])
        except json.JSONDecodeError:
            return None
    return None


def _cache_put_route(train_no: str, date: str, code: str, raw: List[dict]) -> None:
    with _db_lock, db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO routes (train_no, date, code, raw, updated_at) VALUES (?,?,?,?,?)",
            (train_no, date, code, json.dumps(raw, ensure_ascii=False), time.time()),
        )


def fetch_route(code: str, date: str, force: bool = False,
                cache_only: bool = False) -> Tuple[List[dict], dict, bool]:
    """
    返回 (12306 原始站点列表, 车次元信息, 是否命中缓存)
    策略：train_no + date 命中缓存直接用；否则调用 12306（1~2 次请求）并写缓存。
    cache_only=True 时只读本地库（查历史数据时不会去请求 12306）。
    """
    info = lookup_train(code, date) or {}
    train_no = info.get("train_no")

    if train_no and not force:
        cached = _cache_get_route(train_no, date)
        if cached:
            return cached, info, True

    if cache_only:
        raise RuntimeError(f"{date} 的 {code} 经停站尚未下载（历史数据只查本地库）")

    raw: Optional[List[dict]] = None
    err: Optional[Exception] = None
    a = api()

    # 有 train_no 时只需 1 次请求
    if train_no:
        try:
            resp = a._get(
                f"{rail_config.API_BASE}/otn/queryTrainInfo/query",
                params={
                    "leftTicketDTO.train_no": train_no,
                    "leftTicketDTO.train_date": date,
                    "rand_code": "",
                },
            )
            data = resp.json()
            raw = (data.get("data") or {}).get("data") or None
        except Exception as exc:      # noqa: BLE001
            err = exc

    # 回退：走 trainclaw 的两步查询（搜索 train_no → 查经停站）
    if not raw:
        try:
            raw = a.query_route(code, date)
            if raw and not train_no:
                train_no = None
        except Exception as exc:      # noqa: BLE001
            err = err or exc

    if not raw:
        raise RuntimeError(f"未能获取 {code} 的经停站信息：{err}")

    if train_no:
        _cache_put_route(train_no, date, code, raw)
    return raw, info, False


# ---------------------------------------------------------------------------
# 组装前端用的路线负载
# ---------------------------------------------------------------------------

def build_route_payload(code: str, date: str, force: bool = False,
                        cache_only: bool = False, with_path: bool = True,
                        use_override: bool = True) -> dict:
    raw, info, from_cache = fetch_route(code, date, force=force, cache_only=cache_only)
    stations.ensure()

    origin_dep = _parse_hhmm(raw[0].get("start_time")) or 0
    stops: List[dict] = []
    missing: List[str] = []

    for idx, item in enumerate(raw):
        name = _norm(item.get("station_name", ""))
        run_min = _parse_hhmm(item.get("running_time")) or 0
        arr_min = _parse_hhmm(item.get("arrive_time"))
        dep_min = _parse_hhmm(item.get("start_time"))
        coord = stations.coord(name)
        if not coord:
            missing.append(name)

        day_diff = 0
        try:
            day_diff = int(item.get("arrive_day_diff") or 0)
        except (TypeError, ValueError):
            day_diff = 0

        # running_time 缺失时：用「到达时刻 + 日差 - 始发时刻」反推自始发起的运行分钟
        abs_min = None
        if arr_min is not None:
            abs_min = day_diff * 24 * 60 + arr_min - origin_dep
        if idx == 0:
            run_min = 0
        elif abs_min is not None:
            if run_min <= 0 or abs(run_min - abs_min) > 180:
                run_min = max(0, abs_min)

        # 停留时长：始发站/终到站按 0 处理；跨零点用取模修正
        if arr_min is None or dep_min is None or idx == len(raw) - 1:
            stay = 0
        else:
            stay = (dep_min - arr_min) % (24 * 60)
            if stay > 12 * 60:          # 异常值保护
                stay = 0

        stops.append(
            {
                "i": idx + 1,
                "name": name,
                "arr": _fmt_hhmm(arr_min) if arr_min is not None else None,
                "dep": _fmt_hhmm(dep_min) if dep_min is not None else None,
                "day": day_diff,
                "runMin": run_min,
                "stayMin": stay,
                "km": 0.0,
                "lat": coord[0] if coord else None,
                "lon": coord[1] if coord else None,
            }
        )

    # 运行分钟必须单调不减，否则后面按里程插值会乱序
    for i in range(1, len(stops)):
        if stops[i]["runMin"] < stops[i - 1]["runMin"]:
            stops[i]["runMin"] = stops[i - 1]["runMin"]

    # ---- 里程：先用有坐标的站累计直线距离，缺坐标的站按站序线性插值 ----
    known = [i for i, s in enumerate(stops) if s["lat"] is not None]
    cum: Dict[int, float] = {}
    if known:
        cum[known[0]] = 0.0
        acc = 0.0
        for a, b in zip(known, known[1:]):
            acc += haversine(stops[a]["lat"], stops[a]["lon"], stops[b]["lat"], stops[b]["lon"])
            cum[b] = acc
        for i, s in enumerate(stops):
            if i in cum:
                s["km"] = round(cum[i], 1)
                continue
            prev = max((k for k in known if k < i), default=None)
            nxt = min((k for k in known if k > i), default=None)
            if prev is None:
                s["km"] = 0.0
            elif nxt is None:
                s["km"] = round(cum[prev], 1)
            else:
                ratio = (i - prev) / (nxt - prev)
                s["km"] = round(cum[prev] + (cum[nxt] - cum[prev]) * ratio, 1)

    straight_km = [s["km"] for s in stops]          # 直线累计，留作对照

    # ---- 用 cnrail 线路里程表修正为真实运营里程 ----
    km_info = {"matched": 0, "segments": 0, "viaLines": [], "mode": "estimate", "factor": 1.0}
    if stops:
        try:
            km_info = cnrail.refine_km(stops, haversine)
        except Exception:                            # noqa: BLE001
            pass
        if km_info.get("mode") in ("real", "hybrid") and km_info.get("segments"):
            for s, v in zip(stops, km_info["km"]):
                s["km"] = v
        elif km_info.get("mode") == "estimate" and km_info.get("segments"):
            # 没有线路里程可依，按铁路弯曲系数放大直线距离
            for s, v in zip(stops, km_info["km"]):
                s["km"] = v

    # ---- 经由：里程配对 → 线路站序 → 车站连接线路交集 ----
    via = {"segs": [], "unknown": [], "pairs": 0, "resolved": 0,
           "coverage": 0.0, "sources": []}
    if len(stops) > 1:
        try:
            via = cnrail.infer_via(stops)
        except Exception:                            # noqa: BLE001
            pass

    # ---- 人工校正覆盖（优先于自动结果）----
    km_mode = km_info.get("mode", "estimate")
    manual = {"via": False, "km": False, "note": None, "updatedAt": None}
    ov = get_override(code) if use_override else None
    if ov:
        manual["note"] = ov.get("note")
        manual["updatedAt"] = ov.get("updatedAt")
        if ov.get("via"):
            via = {"segs": [dict(s, src="manual") for s in ov["via"]],
                   "unknown": [], "pairs": via.get("pairs", 0),
                   "resolved": via.get("pairs", 0), "coverage": 100.0,
                   "sources": ["manual"]}
            manual["via"] = True
        if ov.get("totalKm"):
            base_km = stops[-1]["km"] if stops else 0.0
            if base_km and base_km > 0:
                ratio = float(ov["totalKm"]) / base_km
                for s in stops:
                    s["km"] = round(s["km"] * ratio, 1)
                km_mode = "manual"
                manual["km"] = True

    # ---- 车站档案（路局 / 电报码 / 连接线路）----
    for s in stops:
        m = stations.meta(s["name"])
        if m:
            s["bureau"] = m.get("bureau")
            s["tele"] = m.get("tele")
            s["lines"] = m.get("lines") or []
        else:
            s["lines"] = []

    # ---- 展开成沿线折线（把线路中间站补进路径，地图不再是一根直线）----
    path: List[dict] = []
    if with_path:
        try:
            path = cnrail.expand_path(stops)
        except Exception:                             # noqa: BLE001
            path = []

    total_km = stops[-1]["km"] if stops else 0.0

    first, last = stops[0], stops[-1]
    # 始发站没有到达时间；终到站 12306 有时把 start_time 填成与到达相同
    start_min = _parse_hhmm(raw[0].get("start_time")) or 0
    duration = last["runMin"]

    base = dt.datetime.strptime(date, "%Y-%m-%d") if re.match(r"^\d{4}-\d{2}-\d{2}$", date) else dt.datetime.now()
    start_dt = base.replace(hour=start_min // 60, minute=start_min % 60)

    # 跨日信息：day / depDay 是相对始发日的自然日序号（0=当日，1=次日，2=第三日…）
    for s in stops:
        arrive_dt = start_dt + dt.timedelta(minutes=s["runMin"])
        dep_dt = arrive_dt + dt.timedelta(minutes=s["stayMin"])
        s["iso"] = arrive_dt.isoformat(timespec="seconds")
        s["depIso"] = dep_dt.isoformat(timespec="seconds")
        s["day"] = (arrive_dt.date() - start_dt.date()).days
        s["depDay"] = (dep_dt.date() - start_dt.date()).days
        s["dayLabel"] = day_label(s["day"])
        s["date"] = arrive_dt.date().isoformat()

    end_dt = start_dt + dt.timedelta(minutes=duration)
    day_span = (end_dt.date() - start_dt.date()).days + 1          # 占用的自然日天数

    return {
        "code": item_code(raw) or code,
        "trainNo": info.get("train_no"),
        "date": date,
        "trainClass": raw[0].get("train_class_name") or "",
        "from": first["name"],
        "to": last["name"],
        "startIso": start_dt.isoformat(timespec="seconds"),
        "endIso": end_dt.isoformat(timespec="seconds"),
        "startDate": start_dt.date().isoformat(),
        "endDate": end_dt.date().isoformat(),
        "daySpan": day_span,
        "overnight": day_span > 1,
        "durationMin": duration,
        "totalKm": round(total_km, 1),
        "straightKm": round(straight_km[-1], 1) if straight_km else 0.0,
        "kmReal": km_mode in ("real", "hybrid", "manual"),
        "kmMode": km_mode,
        "kmFactor": km_info.get("factor", 1.0),
        "viaLines": _via_line_names(via.get("segs") or []),
        "viaSegs": via.get("segs") or [],
        "viaUnknown": via.get("unknown") or [],
        "viaSource": ("manual" if manual["via"]
                      else ("auto" if via.get("segs") else "none")),
        "viaCoverage": via.get("coverage", 0.0),
        "manual": manual,
        "kmMatched": f"{km_info.get('matched', 0)}/{km_info.get('segments', 0)}",
        "stops": stops,
        "path": path,
        "pathPoints": len(path),
        "missingCoords": missing,
        "coverage": round((len(stops) - len(missing)) / max(len(stops), 1) * 100, 1),
        "source": "cache" if from_cache else "live",
    }


_DAY_LABELS = {0: "", 1: "次日", 2: "第三日", 3: "第四日", 4: "第五日"}


def day_label(day: int) -> str:
    if day <= 0:
        return ""
    return _DAY_LABELS.get(day) or f"第{day + 1}日"


def item_code(raw: List[dict]) -> str:
    for it in raw:
        c = it.get("station_train_code")
        if c:
            return str(c).strip()
    return ""


# ---------------------------------------------------------------------------
# 余票 / 中转
# ---------------------------------------------------------------------------

def _seat_num(field: str) -> str:
    if field in ("", "--", None):
        return "无"
    return field


def query_tickets(from_sta: str, to_sta: str, date: str) -> dict:
    stations.ensure()
    f_code = stations.telecode(from_sta)
    t_code = stations.telecode(to_sta)
    if not f_code:
        raise ValueError(f"无法识别车站：{from_sta}")
    if not t_code:
        raise ValueError(f"无法识别车站：{to_sta}")

    raw_items, station_map = api().query_tickets(date, f_code, t_code)
    tickets = trainclaw.parse_tickets(raw_items, station_map)

    out = []
    for t in tickets:
        out.append(
            {
                "code": t.station_train_code,
                "trainNo": t.train_no,
                "from": t.from_station,
                "to": t.to_station,
                "dep": t.start_time,
                "arr": t.arrive_time,
                "lishi": t.lishi,
                "flags": t.dw_flags,
                "seats": [
                    {
                        "name": p.seat_name,
                        "price": p.price,
                        "num": p.num,
                        "discount": p.discount,
                    }
                    for p in t.prices
                ],
            }
        )
    out.sort(key=lambda x: (x["dep"], x["code"]))
    return {
        "from": stations.canonical(from_sta),
        "to": stations.canonical(to_sta),
        "date": date,
        "count": len(out),
        "trains": out,
    }


def query_transfer(from_sta: str, to_sta: str, date: str, middle: str = "") -> dict:
    stations.ensure()
    f_code = stations.telecode(from_sta)
    t_code = stations.telecode(to_sta)
    m_code = stations.telecode(middle) if middle else ""
    if not f_code or not t_code:
        raise ValueError("车站无法识别")
    raw = api().query_transfer(date, f_code, t_code, m_code, show_wz=False, limit=10)
    plans = trainclaw.parse_transfer(raw)
    out = []
    for p in plans:
        out.append(
            {
                "dep": p.start_time,
                "arr": p.arrive_time,
                "from": p.from_station_name,
                "middle": p.middle_station_name,
                "to": p.end_station_name,
                "lishi": p.lishi,
                "wait": p.wait_time,
                "sameStation": p.same_station,
                "legs": [
                    {
                        "code": l.station_train_code,
                        "from": l.from_station,
                        "to": l.to_station,
                        "dep": l.start_time,
                        "arr": l.arrive_time,
                    }
                    for l in p.ticket_list
                ],
            }
        )
    return {"count": len(out), "plans": out}


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------

def stats() -> dict:
    stations.ensure()
    with db() as conn:
        trains = conn.execute("SELECT COUNT(*) c FROM trains").fetchone()["c"]
        routes = conn.execute("SELECT COUNT(*) c FROM routes").fetchone()["c"]
    total = len(stations.by_name)
    hit = sum(1 for n in stations.by_name if n in stations.coords)
    return {
        "trains": trains,
        "routesCached": routes,
        "stations": total,
        "stationsWithCoord": hit,
        "coordCoverage": round(hit / max(total, 1) * 100, 1),
        "cnrail": cnrail.stats(),
    }


def station_detail(q: str) -> dict:
    """车站档案 + 该站在本地车次库里出现的次数（粗略热度）"""
    stations.ensure()
    st = cnrail.station(q)
    if not st:
        return {"found": False, "query": q}
    name = st.get("key") or q
    with db() as conn:
        as_start = conn.execute(
            "SELECT COUNT(*) c FROM trains WHERE start_sta = ?", (name,)).fetchone()["c"]
        as_end = conn.execute(
            "SELECT COUNT(*) c FROM trains WHERE end_sta = ?", (name,)).fetchone()["c"]
    return {
        "found": True,
        **{k: st.get(k) for k in ("name", "key", "en", "type", "loc", "province", "city",
                                  "district", "opened", "bureau", "tele", "services",
                                  "wikidata", "lat", "lon", "coordSrc", "lines")},
        "trainsFrom": as_start,
        "trainsTo": as_end,
    }


init_db()
