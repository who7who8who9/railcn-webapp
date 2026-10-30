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
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import routestore  # noqa: E402  经停站存储：一天一条指针 + 内容按趟去重

import config as rail_config            # noqa: E402
import trainclaw                        # noqa: E402
import cnrail                            # noqa: E402   cnrail_data_v2 静态知识库
import railgeom                          # noqa: E402   OSM 线路几何 / 12306 站资料
import railosm                           # noqa: E402   车次路线的 OSM 版

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

            -- 经停站：route_tpl（每份停站表只存一次）+ route_day（一天一条指针）
            -- 外面看到的 routes 是二者 join 出来的只读视图，字段与老表一致
            -- 详见 routestore.py

            CREATE TABLE IF NOT EXISTS coord_override (
                name        TEXT PRIMARY KEY,
                lat         REAL,
                lon         REAL
            );

            -- 人工校正：经由 / 总里程 / 站间里程 / 备注（按车次，与日期无关）
            -- 校正和车次号绑定：同一趟车换别的一天开行也照这套走；
            -- based_date 记下「当时是照哪一天的站表校的」，站表换了能提醒重校。
            CREATE TABLE IF NOT EXISTS train_override (
                code        TEXT PRIMARY KEY,
                via_json    TEXT,          -- [{"line":"陇海线","from":"郑州","to":"兰州"}]
                total_km    REAL,          -- 手工总里程，各站按比例缩放
                seg_km_json TEXT,          -- {"0":203.4} 站间里程，键是区间序号（第 i 站 → 第 i+1 站）
                note        TEXT,
                source      TEXT,          -- manual = 逐趟人工校正；sync = 从同起讫点车次同步来的
                src_code    TEXT,          -- sync 时记「同步自哪趟车」
                based_date  TEXT,          -- 校正时依据的经停站日期（站表变了要提醒）
                updated_at  REAL
            );

            -- 人工校正：线路里程（按线路，站间 / 总里程 / 站累计）
            CREATE TABLE IF NOT EXISTS line_override (
                name        TEXT PRIMARY KEY,
                seg_km_json TEXT,          -- {"0":12.3} 站间里程，键是区间序号
                sta_km_json TEXT,          -- {"15":163.0} 站累计里程，键是车站序号；null = 这一站明确不详
                total_km    REAL,          -- 手工线路总里程，未手填段按比例缩放
                stops_json  TEXT,          -- 人工站序 [{"name":"XX","km":12.3}]；null = 用档案站序
                note        TEXT,
                updated_at  REAL
            );

            -- 用户新增的线路（档案里没有的）
            CREATE TABLE IF NOT EXISTS line_custom (
                name          TEXT PRIMARY KEY,
                stations_json TEXT,        -- [{"name":"XX","km":12.3}]  km 可为 null
                note          TEXT,
                created_at    REAL,
                updated_at    REAL
            );

            -- 用户新增的车站（档案里没有的）
            CREATE TABLE IF NOT EXISTS station_custom (
                name        TEXT PRIMARY KEY,
                lat         REAL,
                lon         REAL,
                lines_json  TEXT,          -- ["XX线"]
                tele        TEXT,
                bureau      TEXT,
                note        TEXT,
                updated_at  REAL,
                type        TEXT,          -- 车站 / 线路所 / 乘降所
                source      TEXT,          -- manual 手工自建 / line-fill 线路补全
                alias       TEXT,          -- 线路里出现过的原写法（如带「站」的）
                coord_src   TEXT           -- 坐标来源：osm / manual / null
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
                via_source  TEXT,          -- 这次生效的经由是哪来的：manual / auto / none
                via_origin  TEXT,          -- 人工校正的来源：manual（逐趟校）/ sync（同起讫点同步）
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
        # 经停站表结构（route_tpl / route_day / routes 视图）
        routestore.ensure_schema(conn)
        # 老库迁移：seg_km_json 是后加的列
        cols = {r[1] for r in conn.execute("PRAGMA table_info(train_override)")}
        if "seg_km_json" not in cols:
            conn.execute("ALTER TABLE train_override ADD COLUMN seg_km_json TEXT")
        # 老库迁移：校正的来源 / 依据日期（同步入库、站表变了以后要能区分）
        for col in ("source", "src_code", "based_date"):
            if col not in cols:
                conn.execute(f"ALTER TABLE train_override ADD COLUMN {col} TEXT")
        # 老库迁移：清单里区分「逐趟人工校正」和「同起讫点同步入库」
        ccols = {r[1] for r in conn.execute("PRAGMA table_info(curate)")}
        if "via_origin" not in ccols:
            conn.execute("ALTER TABLE curate ADD COLUMN via_origin TEXT")
        # 老库迁移：线路里程的「站累计」也是后加的列
        lcols = {r[1] for r in conn.execute("PRAGMA table_info(line_override)")}
        if "sta_km_json" not in lcols:
            conn.execute("ALTER TABLE line_override ADD COLUMN sta_km_json TEXT")
        # 老库迁移：档案线路的人工站序（站中间插站 / 调站序）
        if "stops_json" not in lcols:
            conn.execute("ALTER TABLE line_override ADD COLUMN stops_json TEXT")
        # 老库迁移：车站扩展信息（类型 / 来源 / 原写法 / 坐标来源）
        scols = {r[1] for r in conn.execute("PRAGMA table_info(station_custom)")}
        for col in ("type", "source", "alias", "coord_src"):
            if col not in scols:
                conn.execute(f"ALTER TABLE station_custom ADD COLUMN {col} TEXT")


# ---------------------------------------------------------------------------
# 车站注册表 + 坐标
# ---------------------------------------------------------------------------

def _norm(name: str) -> str:
    """站名归一化：只去空白（末尾的「站」用 _bare 去，别在这里动，坐标表是按带「站」的键存的）"""
    if not name:
        return ""
    s = re.sub(r"\s+", "", str(name))
    return s


def _bare(name: str) -> str:
    """比对用的裸站名：唐海南站 → 唐海南（12306 站表不带「站」，线路档案多半带）"""
    n = _norm(name)
    return n[:-1] if n.endswith("站") and len(n) > 1 else n


# ---------------------------------------------------------------------------
# 经纬度文本解析（前端只要粘一行就能填坐标）
# ---------------------------------------------------------------------------

# 认这些写法：
#   经度:126.576636,纬度:42.038926     ← 带中文标签（顺序任意）
#   lng:126.576636, lat:42.038926      ← 英文标签
#   126.576636, 42.038926              ← 裸数字成对
#   126.576636E 42.038926N             ← 带方向字母
#   126°34'35.9"E, 42°2'20.1"N         ← 度分秒
_COORD_LABEL_RE = re.compile(
    r"(?P<lab>经度|纬度|longitude|latitude|long|lng|lon|lat)\s*[:=：]?\s*"
    r"(?P<num>[-+]?\d+(?:\.\d+)?"
    r"(?:\s*[°度]\s*\d+(?:\.\d+)?\s*['′分]?"
    r"(?:\s*\d+(?:\.\d+)?\s*[\"″秒]?)?)?)"
    r"\s*(?P<dir>[NSEWnsew北南东西])?", re.I)

_COORD_DIR = {"N": "lat", "S": "lat", "北": "lat", "南": "lat",
              "E": "lon", "W": "lon", "东": "lon", "西": "lon"}
_COORD_LAB_AXIS = {"经度": "lon", "longitude": "lon", "long": "lon",
                   "lng": "lon", "lon": "lon",
                   "纬度": "lat", "latitude": "lat", "lat": "lat"}


def _angle(text: str) -> Optional[float]:
    """把一段角度文本（10 进制 / 度分秒）转成 10 进制度数"""
    s = str(text or "").strip().replace("，", "").replace(",", "")
    if not s:
        return None
    nums = re.findall(r"[-+]?\d+(?:\.\d+)?", s)
    if not nums:
        return None
    val = abs(float(nums[0]))
    if len(nums) > 1:
        val += abs(float(nums[1])) / 60.0
    if len(nums) > 2:
        val += abs(float(nums[2])) / 3600.0
    if str(nums[0]).startswith("-"):
        val = -val
    return val


def parse_latlon(text) -> Tuple[float, float]:
    """
    从一行文本里解析出 (lat, lon)。支持「经度:126.576636,纬度:42.038926」这类写法，
    也支持反序、英文标签、方向字母、度分秒。解析不出来就抛 ValueError。

    只有一对裸数字时按「经度, 纬度」处理，但只要落在北纬 3~54 / 东经 73~136 之外
    就按另一种顺序再猜一次（中国境内的站两种都试得出来）。
    """
    raw = str(text if text is not None else "").strip()
    if not raw:
        raise ValueError("经纬度不能为空")
    s = raw.replace("：", ":").replace("（", "(").replace("）", ")")
    s = re.sub(r"[()（）]", " ", s)

    # 1) 带标签的写法
    got: Dict[str, float] = {}
    for m in _COORD_LABEL_RE.finditer(s):
        lab = m.group("lab").strip().lower()
        axis = _COORD_LAB_AXIS.get(lab)
        v = _angle(m.group("num"))
        if v is None:
            continue
        d = (m.group("dir") or "").strip()
        if d:                                  # 带了方向字母就以字母为准
            axis = _COORD_DIR.get(d.upper()) or _COORD_DIR.get(d) or axis
            if d.upper() in ("S", "W") or d in ("南", "西"):
                v = -abs(v)
        if axis and axis not in got:
            got[axis] = v
    if "lat" in got and "lon" in got:
        lat, lon = got["lat"], got["lon"]
    else:
        # 2) 裸数字：先看方向字母，再按数值范围猜
        dirs = re.findall(r"(\d+(?:\.\d+)?(?:\s*[°度][^,;，；\s]*)?)\s*([NSEWnsew北南东西])", s)
        pair = re.findall(r"[-+]?\d+(?:\.\d+)?", s)
        if len(dirs) >= 2:
            vals: Dict[str, float] = {}
            for num, d in dirs[:2]:
                axis = _COORD_DIR.get(d.upper(), _COORD_DIR.get(d))
                v = _angle(num)
                if not axis or v is None:
                    continue
                if d.upper() in ("S", "W") or d in ("南", "西"):
                    v = -abs(v)
                vals[axis] = v
            if "lat" in vals and "lon" in vals:
                lat, lon = vals["lat"], vals["lon"]
            else:
                raise ValueError("经纬度里认不出经度/纬度，试试「经度:126.576636,纬度:42.038926」")
        elif len(pair) >= 2:
            a, b = float(pair[0]), float(pair[1])
            lat, lon = _order_pair(a, b)
        else:
            raise ValueError("经纬度要成对填，比如「经度:126.576636,纬度:42.038926」")

    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        raise ValueError("经纬度超出范围（纬度 ±90，经度 ±180）")
    if lat == 0 and lon == 0:
        raise ValueError("(0, 0) 多半是填错了")
    return round(lat, 6), round(lon, 6)


def _order_pair(a: float, b: float) -> Tuple[float, float]:
    """两个裸数字谁是谁：先按「经度, 纬度」，不合中国范围就换个顺序再试"""
    def in_cn(lon: float, lat: float) -> bool:
        return 73 <= lon <= 136 and 3 <= lat <= 54

    if in_cn(a, b):
        return b, a
    if in_cn(b, a):
        return a, b
    if abs(b) > 90 and abs(a) <= 90:          # 第二个数超出纬度范围 → 它是经度
        return a, b
    if abs(a) > 90 and abs(b) <= 90:
        return b, a
    return b, a                                # 兜底按「经度, 纬度」


# ---------------------------------------------------------------------------
# 人工校正：经由 / 总里程 / 备注
# ---------------------------------------------------------------------------

def _clean_via(via) -> List[dict]:
    """把前端传来的经由整理成 [{line, from, to}]，丢掉没有线路名的空行

    `src="auto"` 是自动推断出来的「虚拟经由」，只在校对页里给人看；正式经由
    只收人工确认过的段，这一类在这里一律丢弃（保存时再兜一道底）。
    """
    out: List[dict] = []
    for seg in via or []:
        if isinstance(seg, str):
            seg = {"line": seg}
        if not isinstance(seg, dict):
            continue
        if str(seg.get("src") or "").strip().lower() == "auto":
            continue
        name = str(seg.get("line") or "").strip()
        if not name:
            continue
        out.append({
            "line": name,
            # 与车次站名同口径：库里写「天津站」的，存成「天津」
            "from": cnrail.station_name(_norm(seg.get("from") or "")),
            "to": cnrail.station_name(_norm(seg.get("to") or "")),
        })
    return out


_NA_TOKENS = {"-", "--", "—", "——", "–", "~", "～", "?", "？", "不详", "未知", "n/a", "na", "unknown", "无"}


def _clean_seg_km(seg_km) -> Dict[str, Optional[float]]:
    """站间里程整理成 {"0": 203.4}：键是区间序号（第 i 站 → 第 i+1 站），值是公里数。

    允许传 dict 或 list（list 的下标就是区间序号）。
    值为 None 或 "-" / "不详" 之类的记号表示**这一段里程明确不详**（存 None）：
    与「没填」（键不存在，沿用档案值）是两回事，不详的段之后累计里程不再外推。
    """
    out: Dict[str, Optional[float]] = {}
    if not seg_km:
        return out
    if isinstance(seg_km, dict):
        items = list(seg_km.items())
    elif isinstance(seg_km, (list, tuple)):
        items = list(enumerate(seg_km))
    else:
        return out
    for k, v in items:
        try:
            idx = int(k)
        except (TypeError, ValueError):
            continue
        if idx < 0:
            continue
        if v is None:
            out[str(idx)] = None               # 明确不详
            continue
        if isinstance(v, str) and v.strip().lower() in _NA_TOKENS:
            out[str(idx)] = None
            continue
        try:
            km = float(v)
        except (TypeError, ValueError):
            continue
        if not (0 < km <= 5000):
            continue
        out[str(idx)] = round(km, 1)
    return out


def _clean_sta_km(sta_km) -> Dict[str, Optional[float]]:
    """站累计里程整理成 {"15": 163.0}：键是车站序号，值是「自起点站算起的公里数」。

    None 或 "-" 表示**这一站**的累计里程明确不详（与「没填」不同：没填会沿用档案值）。
    """
    out: Dict[str, Optional[float]] = {}
    if not sta_km:
        return out
    if isinstance(sta_km, dict):
        items = list(sta_km.items())
    elif isinstance(sta_km, (list, tuple)):
        items = list(enumerate(sta_km))
    else:
        return out
    for k, v in items:
        try:
            idx = int(k)
        except (TypeError, ValueError):
            continue
        if idx < 0:
            continue
        if v is None:
            out[str(idx)] = None
            continue
        if isinstance(v, str) and v.strip().lower() in _NA_TOKENS:
            out[str(idx)] = None
            continue
        try:
            km = float(v)
        except (TypeError, ValueError):
            continue
        if not (0 <= km <= 20000):
            continue
        out[str(idx)] = round(km, 1)
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


def save_override(code: str, via, total_km=None, note: str = "",
                  seg_km=None, date: Optional[str] = None,
                  source: str = "manual", src_code: str = "") -> dict:
    """
    保存人工校正。via / total_km / seg_km / note 都为空时视作撤销（删除记录）。
    seg_km: {"区间序号": 公里数}，区间序号 i 表示「第 i 站 → 第 i+1 站」。
    date:   这次校正依据的是哪一天的经停站（站表变了要能提醒重校）。
    source: manual = 逐趟人工校正；sync = 从同起讫点车次同步来的。
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

    segs = _clean_seg_km(seg_km)
    note = (note or "").strip()
    if not clean and km is None and not segs and not note:
        clear_override(code)
        return {"code": code, "cleared": True}

    src = (source or "manual").strip().lower()
    if src not in ("manual", "sync"):
        src = "manual"
    with _db_lock, db() as conn:
        conn.execute(
            """INSERT INTO train_override(code, via_json, total_km, seg_km_json, note,
                                          source, src_code, based_date, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(code) DO UPDATE SET
                 via_json=excluded.via_json, total_km=excluded.total_km,
                 seg_km_json=excluded.seg_km_json,
                 note=excluded.note, source=excluded.source, src_code=excluded.src_code,
                 based_date=excluded.based_date, updated_at=excluded.updated_at""",
            (code, json.dumps(clean, ensure_ascii=False), km,
             json.dumps(segs, ensure_ascii=False), note, src,
             (src_code or "").strip().upper(), (date or "").strip() or None, time.time()),
        )
    _drop_override_cache()
    # 线路库里没有的线名，展开中间站时用不上，回给界面提个醒
    outside = sorted({(s.get("line") or "").strip() for s in clean
                      if (s.get("line") or "").strip() and not cnrail.line(s.get("line"))})
    return {"code": code, "via": clean, "totalKm": km, "segKm": segs, "note": note,
            "source": src, "srcCode": (src_code or "").strip().upper(),
            "basedDate": (date or "").strip() or None, "outsideLines": outside}


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
                "SELECT code, via_json, total_km, seg_km_json, note, source, src_code, "
                "based_date, updated_at FROM train_override"
            ).fetchall()
    except sqlite3.Error:
        return {}
    out: Dict[str, dict] = {}
    for row in rows:
        try:
            via = _clean_via(json.loads(row["via_json"] or "[]"))
        except (TypeError, ValueError):
            via = []
        try:
            seg = _clean_seg_km(json.loads(row["seg_km_json"] or "{}"))
        except (TypeError, ValueError):
            seg = {}
        out[row["code"]] = {
            "via": via, "totalKm": row["total_km"], "segKm": seg,
            "note": row["note"], "updatedAt": row["updated_at"],
            "source": row["source"] or "manual",
            "srcCode": row["src_code"] or "",
            "basedDate": row["based_date"] or "",
        }
    return out


# ---------------------------------------------------------------------------
# 车种
# ---------------------------------------------------------------------------

def train_class(code: str) -> str:
    """车种 = 车次号首字母（G / D / K / T / Z / S / C / L / Y …）"""
    m = re.match(r"^([A-Za-z])", (code or "").strip())
    return m.group(1).upper() if m else ""


def _match_stop(stops: List[dict], name: Optional[str], frac: Optional[float]) -> Optional[int]:
    """在一趟车的经停站里找这个区间端点：先按站名，找不到再按里程占比就近落站"""
    k = _norm(name or "")
    if k:
        for i, s in enumerate(stops):
            if _norm(s.get("name") or "") == k:
                return i
        # 「XX站」/「XX」两种写法都可能是对的，再用 cnrail 的口径试一次
        k2 = cnrail._key(k)
        for i, s in enumerate(stops):
            if cnrail._key(s.get("name") or "") == k2:
                return i
    if frac is None:
        return None
    total = float(stops[-1].get("km") or 0.0)
    if total > 0:
        target = frac * total
        return min(range(len(stops)), key=lambda i: abs(float(stops[i].get("km") or 0.0) - target))
    # 停站明细没带里程（同步别人的经由时只有站名）→ 按「第几站」的比例就近落站，
    # 对方少停的站就是这么并进相邻段的
    target = frac * max(1, len(stops) - 1)
    return min(range(len(stops)), key=lambda i: abs(i - target))


def adapt_via_to_stops(segs: List[dict], stops: List[dict]) -> List[dict]:
    """
    把一条经由套到**另一趟车**的经停站上（同起讫点同步、人工选中的车次）：

    - 区间端点在它的经停站里就按站名对；
    - 对不上（比如它跳过那一站没停）就按区间端点在全程里的占比，就近落到某一站；
    - 再把相邻同一线名的步骤合并成一段，区间首尾必然首尾相接。
    """
    n = len(stops or [])
    if n < 2 or not segs:
        return []
    steps: List[Optional[str]] = [None] * (n - 1)
    for s in segs:
        line = str(s.get("line") or "").strip()
        if not line:
            continue
        a = _match_stop(stops, s.get("from"), s.get("f0"))
        b = _match_stop(stops, s.get("to"), s.get("f1"))
        if a is None or b is None:
            continue
        if b <= a:
            b = a + 1
        if b > n - 1:
            a = max(0, n - 2)
            b = n - 1
        for i in range(a, b):
            steps[i] = line

    # 端点对不准会留空：先向后、再向前，用相邻段的线名补齐，保证整条链不断
    last = None
    for i in range(n - 1):
        if steps[i]:
            last = steps[i]
        elif last:
            steps[i] = last
    nxt = None
    for i in range(n - 2, -1, -1):
        if steps[i]:
            nxt = steps[i]
        elif nxt:
            steps[i] = nxt
    if any(x is None for x in steps):
        return []

    out: List[dict] = []
    i = 0
    while i < n - 1:
        j = i
        while j + 1 < n - 1 and steps[j + 1] == steps[i]:
            j += 1
        out.append({"line": steps[i], "from": stops[i]["name"], "to": stops[j + 1]["name"],
                    "i0": i, "src": "manual"})
        i = j + 1
    return out


def _line_span_names(line: str, cache: Dict[str, Optional[List[str]]]) -> Optional[List[str]]:
    """某条线的完整站序（含线路所这类节点）。线路库、资料库都查不到 → None"""
    key = cnrail._key(line or "")
    if key in cache:
        return cache[key]
    names: Optional[List[str]] = None
    try:
        names = cnrail.line_station_names(line)
        if names:
            names = merge_catalog_nodes(line, names)
        else:
            names = catalog_stop_names(line) or None
    except Exception:                                # noqa: BLE001
        names = None
    cache[key] = names
    return names


def _span_slice(names: List[str], a: str, b: str) -> Optional[List[str]]:
    """站序里 a→b 之间（含两端）的站名；有一端对不上就是 None"""
    keys = [cnrail._key(n) for n in names]
    ka, kb = cnrail._key(a or ""), cnrail._key(b or "")
    if ka not in keys or kb not in keys:
        return None
    i, j = keys.index(ka), keys.index(kb)
    if i > j:
        i, j = j, i
    return names[i:j + 1]


def via_covers_stops(segs: List[dict], stops: List[dict],
                     cache: Optional[Dict[str, Optional[List[str]]]] = None) -> Optional[bool]:
    """
    这条经由是不是已经把 stops 里的每一站都走过了？

    True  → 原样照抄就是对的：转线的所、区间端点都不用改，不必重新落站；
    False → 目标车停了经由之外的站，得按它的停站重新落站；
    None  → 线路站序查不到，判断不了，走重新落站那条路（老办法）。

    `cache` 是「线路名 → 站序」的记忆表：一次列几十趟同起讫点车次时传一个进来，
    别每趟都把线路站序重新读一遍。
    """
    n = len(stops or [])
    if n < 2 or not segs:
        return None
    cache = {} if cache is None else cache
    seen: set = set()
    for s in segs:
        names = _line_span_names(str(s.get("line") or ""), cache)
        if not names:
            return None
        part = _span_slice(names, s.get("from") or "", s.get("to") or "")
        if part is None:
            return None
        seen.update(cnrail._key(x) for x in part)
    return all(cnrail._key(s.get("name") or "") in seen for s in stops)


# ---------------------------------------------------------------------------
# 同起讫点车次：列停站明细、按勾选同步已校正的经由
# ---------------------------------------------------------------------------

def override_map() -> Dict[str, dict]:
    """整表（只读）。批量判断「这趟车有没有自己的校正」时用它，别一行一个查询"""
    return _override_map()


def route_stops_brief(code: str, date: Optional[str] = None) -> dict:
    """
    一条车次的停站明细（只读 12306 原始缓存，不做线路推断，很快）。

    校对页拿它比对「同起讫点的车次」：站序、到发时刻、换号点都在这里，
    也用来把已校正的经由套到别的车次上（只需要站名顺序）。

    注意缓存是按 **train_no** 存一天的（`route_day`），不是按车次号：中途换号的车
    （C344/5、C4326/7）整趟站序只落在到达号 C345/C4327 那一行里，
    按出发号 C344 查 code 会一无所获——所以先按 train_no 找，找不到再退回按 code 找。

    指定的那一天完全没有这一页时（换号车常常只抓到相邻某天），退到最接近的一天并
    把 `dateExact` 置 False：调用方要么照用（同起讫点比站名，够用），要么据此不做
    「站表变没变」这种按日期的判断。
    """
    code = (code or "").strip().upper()
    out: Dict[str, object] = {"code": code, "date": None, "stops": [], "from": None, "to": None,
                              "dateExact": True}
    if not code:
        return out
    try:
        with db() as conn:
            # 换号车两个号共用一个 train_no，用它才能把这趟车的整趟站序找全
            t = conn.execute(
                "SELECT train_no FROM trains WHERE code=? ORDER BY date DESC LIMIT 1",
                (code,)).fetchone()
            train_no = (t["train_no"] if t else "") or ""
            d = (date or "").strip()
            if not d:
                row = None
                if train_no:
                    row = conn.execute(
                        "SELECT MAX(date) AS d FROM routes WHERE train_no=? AND raw IS NOT NULL",
                        (train_no,)).fetchone()
                if not (row and row["d"]):
                    row = conn.execute(
                        "SELECT MAX(date) AS d FROM routes WHERE code=? AND raw IS NOT NULL",
                        (code,)).fetchone()
                d = (row["d"] if row else "") or ""
            if not d:
                return out
            row = None
            if train_no:
                row = conn.execute(
                    "SELECT date, raw FROM routes WHERE train_no=? AND date=? LIMIT 1",
                    (train_no, d)).fetchone()
            if not (row and row["raw"]):
                row = conn.execute(
                    "SELECT date, raw FROM routes WHERE code=? AND date=?", (code, d)).fetchone()
            if not (row and row["raw"]):
                # 这一天没抓到这一页（换号车常常只抓到相邻某天）：退到最接近的那一天。
                # 站名顺序基本不变，比在界面上显示「0 站、没有时刻」有用；
                # 实际用的日期写进 out["date"]，停站详情照实显示，不冒充当天
                near = ("""SELECT date, raw FROM routes
                            WHERE %s AND raw IS NOT NULL
                            ORDER BY ABS(julianday(date) - julianday(?)) LIMIT 1""")
                row = None
                if train_no:
                    row = conn.execute(near % "train_no=?", (train_no, d)).fetchone()
                if not (row and row["raw"]):
                    row = conn.execute(near % "code=?", (code, d)).fetchone()
                if row and row["raw"]:
                    d = row["date"]
                    out["dateExact"] = False
    except sqlite3.Error:
        return out
    if not row or not row["raw"]:
        return out
    try:
        raw = json.loads(row["raw"]) or []
    except (TypeError, ValueError):
        return out

    stops: List[dict] = []
    for s in raw:
        if not isinstance(s, dict):
            continue
        name = _norm(s.get("station_name") or "")
        if not name:
            continue
        arr = (s.get("arrive_time") or "").strip()
        dep = (s.get("start_time") or "").strip()
        try:
            day = int(s.get("arrive_day_diff") or 0)
        except (TypeError, ValueError):
            day = 0
        stops.append({
            "name": name,
            "arr": arr if re.match(r"^\d{1,2}:\d{2}$", arr) else None,
            "dep": dep if re.match(r"^\d{1,2}:\d{2}$", dep) else None,
            "day": max(0, day),
            "trainCode": (s.get("station_train_code") or "").strip().upper(),
        })
    out["date"] = d
    out["stops"] = stops
    out["from"] = stops[0]["name"] if stops else None
    out["to"] = stops[-1]["name"] if stops else None
    return out


def sync_via(codes, segs, src_code: str, note: str = "", date: Optional[str] = None,
             overwrite: bool = False) -> List[dict]:
    """
    把一趟车的已校正经由同步到勾选的同起讫点车次。

    逐趟各存一条 `train_override`（和车次号绑定，不是抄进一份「同类」表）：
    对方的每一站都在这条经由走过的路上 → 原样照抄，区间端点、转线的所一个不动；
    只有它停了经由之外的站，才按它的停站重新落站（`mode=adapt`）。
    返回每趟的结果，同步不过去的（没数据 / 已有自己的校正 / 站序对不上）如实说明，不硬写。
    """
    src = (src_code or "").strip().upper()
    clean = _clean_via(segs)
    marks = []
    for c in (codes or []):
        c = (c or "").strip().upper()
        if c and c != src and c not in marks:
            marks.append(c)
    if not marks:
        return []
    if not clean:
        return [{"code": c, "ok": False, "reason": "当前车次还没有已校正的经由"} for c in marks]

    # 只同步「覆盖全程」的经由：只填了中间一段就套给别人，别人的首末段会被硬接上
    mine = route_stops_brief(src, date)
    cover = (mine.get("stops") or [])
    if len(cover) >= 2 and (clean[0].get("from") != cover[0]["name"]
                            or clean[-1].get("to") != cover[-1]["name"]):
        return [{"code": c, "ok": False, "reason": "经由没有覆盖全程，没有同步"} for c in marks]

    # 区间端点在自己这趟车的第几站 → 折成比例：对方少停某站时按比例就近落站
    idx = {s["name"]: i for i, s in enumerate(cover)}
    span = max(1, len(cover) - 1)
    routed: List[dict] = []
    for s in clean:
        s = dict(s)
        a, b = idx.get(s.get("from") or ""), idx.get(s.get("to") or "")
        if a is not None:
            s["f0"] = round(a / span, 5)
        if b is not None:
            s["f1"] = round(b / span, 5)
        routed.append(s)

    out: List[dict] = []
    for c in marks:
        brief = route_stops_brief(c)
        stops = brief.get("stops") or []
        if len(stops) < 2:
            out.append({"code": c, "ok": False, "reason": "本地库里没有它的经停站数据"})
            continue
        if cover and (brief.get("from") != cover[0]["name"]
                      or brief.get("to") != cover[-1]["name"]):
            out.append({"code": c, "ok": False, "reason": "起讫点和这趟车不一样"})
            continue
        prev = get_override(c) or {}
        if (prev.get("via") or []) and not overwrite:
            out.append({"code": c, "ok": False, "reason": "它已有自己的校正，未覆盖"})
            continue
        text = f"{note}（同起讫点同步自 {src}）" if note else f"同起讫点同步自 {src}"
        # 它的每一站都在这条经由走过的路上 → 直接照抄，不用重新落站、不用重算
        direct = via_covers_stops(clean, stops)
        if direct:
            via = [dict(s) for s in clean]
            mode = "direct"
        else:
            via = adapt_via_to_stops(routed, stops)
            mode = "adapt"
            if not via:
                out.append({"code": c, "ok": False,
                            "reason": ("经由和它的停站对不上" if direct is False
                                       else "线路站序查不到，没法按它的停站落站") + "，未同步"})
                continue
        # 站间里程按「第 i 站」记下标，站数不同就对不上，所以同步只带总里程不带段里程
        saved = save_override(c, via, prev.get("totalKm"), text, {}, brief.get("date"),
                              source="sync", src_code=src)
        out.append({"code": c, "ok": True, "mode": mode,
                    "segs": len(saved.get("via") or []),
                    "date": brief.get("date"), "srcCode": src,
                    "stops": len(stops)})
    return out


# ---------------------------------------------------------------------------
# 线路里程人工校正（按线路，写库后立即套回 cnrail 的内存线路表）
# ---------------------------------------------------------------------------

_line_ovr_cache: Optional[Dict[str, dict]] = None


def list_line_overrides() -> Dict[str, dict]:
    try:
        with db() as conn:
            rows = conn.execute(
                "SELECT name, seg_km_json, sta_km_json, stops_json, total_km, note, updated_at "
                "FROM line_override"
            ).fetchall()
    except sqlite3.Error:
        return {}
    out: Dict[str, dict] = {}
    for row in rows:
        try:
            seg = _clean_seg_km(json.loads(row["seg_km_json"] or "{}"))
        except (TypeError, ValueError):
            seg = {}
        try:
            sta = _clean_sta_km(json.loads(row["sta_km_json"] or "{}"))
        except (TypeError, ValueError):
            sta = {}
        try:
            stops = _clean_stops(json.loads(row["stops_json"] or "[]"))
        except (TypeError, ValueError):
            stops = []
        out[row["name"]] = {
            "segKm": seg, "staKm": sta, "stops": stops, "totalKm": row["total_km"],
            "note": row["note"], "updatedAt": row["updated_at"],
        }
    return out


def _clean_stops(raw) -> List[dict]:
    """人工站序的入库 / 出库口径：[{name, km}]，km 可为 None"""
    if not isinstance(raw, list):
        return []
    out: List[dict] = []
    for s in raw:
        if isinstance(s, str):
            s = {"name": s}
        if not isinstance(s, dict):
            continue
        nm = _norm_line_name(s.get("name") or "")
        if not nm:
            continue
        km = s.get("km")
        if km in ("", None):
            km = None
        else:
            try:
                km = round(float(km), 1)
            except (TypeError, ValueError):
                km = None
        out.append({"name": nm, "km": km})
    return out


# ---------------------------------------------------------------------------
# 「这条线用哪一份站序」的默认选择（用户在界面上勾的那个）
# ---------------------------------------------------------------------------
# 同一条线名在各来源里各有一套站序（线路库 89 站 / 资料库 89 站 / china-emu
# 154 站 / OSM 220 站）。界面上默认让用户自己点，点一次只对这一次查看生效；
# 勾成默认以后，全项目所有「不指定来源」的取数（选经由、算里程、补站序）
# 都按这一份来 —— 否则同一趟车在不同页面会按不同站序算，用户看着就是
# 「刚才明明改好了，怎么又变了」。

_SRC_PICK_CACHE: Optional[Dict[str, str]] = None
_SRC_ALIAS_CACHE: Dict[str, str] = {}       # 别名 → 规范线名（与勾选内容无关，可长留）


def _ensure_src_pick(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS line_src_pick (
               name       TEXT PRIMARY KEY,   -- 线名（按请求里的写法存，与 line_override 同口径）
               source     TEXT NOT NULL,      -- lib / custom / catalog / chinaemu / osm / parts
               updated_at REAL
           )""")


def list_src_picks() -> Dict[str, str]:
    """全部默认来源（一次性读进内存，热路径上不再打库）"""
    global _SRC_PICK_CACHE
    if _SRC_PICK_CACHE is None:
        try:
            with db() as conn:
                _ensure_src_pick(conn)
                _SRC_PICK_CACHE = {r["name"]: r["source"] for r in
                                   conn.execute("SELECT name, source FROM line_src_pick")}
        except sqlite3.DatabaseError:
            _SRC_PICK_CACHE = {}
    return _SRC_PICK_CACHE


def _canon_name(nm: str) -> str:
    """别名 → 规范线名（线路库优先，其次资料库）；查不到返回空串"""
    global _SRC_ALIAS_CACHE
    if nm in _SRC_ALIAS_CACHE:
        return _SRC_ALIAS_CACHE[nm]
    canon = ""
    try:
        ln = cnrail.line(nm)
        if ln and ln.get("name"):
            canon = ln["name"]
        if not canon:
            # 资料库按 name 或 title 都能查到，返回里的 name 就是规范线名
            canon = (cnrail.catalog_get(nm) or {}).get("name") or ""
    except Exception:                        # noqa: BLE001
        canon = ""
    _SRC_ALIAS_CACHE[nm] = canon
    return canon


def src_pick(name: str) -> str:
    """这条线的默认站序来源；没勾过返回空串（= 走系统规则）

    勾的时候存的是「界面上显示的那个线名」（多半是资料库的规范名），而查询时
    拿到的可能是它的别名 —— 搜「侯西铁路」进来的线名就是别名的写法。所以查
    不到时再按规范名找一次，否则同一条线搜别名和搜正名会给出两套站序。
    """
    nm = re.sub(r"\s+", "", str(name or ""))
    if not nm:
        return ""
    m = list_src_picks()
    if nm in m:
        return m[nm]
    canon = _canon_name(nm)
    return m.get(canon, "") if canon else ""


def set_src_pick(name: str, source: str) -> dict:
    """把某条线的默认来源设成 source；source 为空 = 取消默认，退回系统规则"""
    global _SRC_PICK_CACHE
    nm = re.sub(r"\s+", "", str(name or ""))
    if not nm:
        raise ValueError("要指定是哪个线名")
    src = (source or "").strip()
    allowed = {"", "lib", "custom", "catalog", "chinaemu", "osm", "parts"}
    if src not in allowed:
        raise ValueError("不认识的来源：" + src)
    with _db_lock, db() as conn:
        _ensure_src_pick(conn)
        if src:
            conn.execute(
                "INSERT INTO line_src_pick(name, source, updated_at) VALUES(?,?,?)"
                " ON CONFLICT(name) DO UPDATE SET source=excluded.source,"
                " updated_at=excluded.updated_at", (nm, src, time.time()))
        else:
            conn.execute("DELETE FROM line_src_pick WHERE name=?", (nm,))
    _SRC_PICK_CACHE = None
    _SRC_ALIAS_CACHE.clear()     # 之前可能把「查不到规范名」缓下来了
    return {"name": nm, "source": src}


# ---------------------------------------------------------------------------
# 来源级修正：只改「某一条来源的某一份站序」，不动线路本身
#
# 为什么要有这张表：同一条线名在各来源里的方向不一定一致 —— OSM 的京沪线是
# 按 way 的绘制方向拼的，可能正好从上海往北京；而资料库那份是对的。原来的
# 「倒排站序」把结果存进线路的人工站序（override），等于把**整条线**翻过来，
# 界面上点着 OSM 那份看的时候还是原样，其它来源反倒跟着变了。
#
# 所以倒排按 (线名, 来源) 记一个修正标记，读这份来源的站序时当场翻转 ——
# 只影响「OSM 京沪线」，资料库 / china-emu / 线路库自己的那份一个字节都不动。
# 取消就是把标记删掉，原始数据永远不被改写。

_SRC_FIX_CACHE: Optional[Dict[str, Dict[str, List[str]]]] = None
SRC_FIX_KINDS = ("reverse",)


def _ensure_src_fix(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS line_src_fix (
               name       TEXT NOT NULL,      -- 线名（存规范名，别名查询时回退匹配）
               source     TEXT NOT NULL,      -- catalog / chinaemu / osm / parts
               kind       TEXT NOT NULL,      -- 目前只有 reverse
               updated_at REAL,
               PRIMARY KEY(name, source, kind)
           )""")


def list_src_fixes() -> Dict[str, Dict[str, List[str]]]:
    """全部来源级修正：{线名: {来源: [kind, ...]}}，一次读进内存"""
    global _SRC_FIX_CACHE
    if _SRC_FIX_CACHE is None:
        try:
            with db() as conn:
                _ensure_src_fix(conn)
                got: Dict[str, Dict[str, List[str]]] = {}
                for r in conn.execute("SELECT name, source, kind FROM line_src_fix"):
                    got.setdefault(r["name"], {}).setdefault(r["source"], []).append(r["kind"])
            _SRC_FIX_CACHE = got
        except sqlite3.DatabaseError:
            _SRC_FIX_CACHE = {}
    return _SRC_FIX_CACHE


def src_fix(name: str, source: str) -> List[str]:
    """这条线这个来源上挂了哪些修正；没有返回空表

    与 src_pick 同一个坑：存的是界面上那个规范线名，查询可能用的是别名
    （侯西铁路 → 侯西线），所以要回退按规范名再找一次。
    """
    nm = re.sub(r"\s+", "", str(name or ""))
    src = (source or "").strip()
    if not nm or not src:
        return []
    m = list_src_fixes()
    hit = (m.get(nm) or {}).get(src)
    if hit:
        return list(hit)
    canon = _canon_name(nm)
    if canon and canon != nm:
        hit = (m.get(canon) or {}).get(src)
        if hit:
            return list(hit)
    return []


def set_src_fix(name: str, source: str, kind: str, on: bool) -> dict:
    """挂上 / 取消一个来源级修正（on=False 仍是删标记，原始数据不受影响）"""
    global _SRC_FIX_CACHE
    nm = re.sub(r"\s+", "", str(name or ""))
    src = (source or "").strip()
    if not nm:
        raise ValueError("要指定是哪个线名")
    if src not in ("catalog", "chinaemu", "osm", "parts"):
        raise ValueError("这个来源不支持单独倒排：" + src)
    if kind not in SRC_FIX_KINDS:
        raise ValueError("不认识的修正：" + kind)
    key = _canon_name(nm) or nm          # 存规范名，别名查询也能找到
    with _db_lock, db() as conn:
        _ensure_src_fix(conn)
        if on:
            conn.execute(
                "INSERT INTO line_src_fix(name, source, kind, updated_at) VALUES(?,?,?,?)"
                " ON CONFLICT(name, source, kind) DO UPDATE SET"
                " updated_at=excluded.updated_at", (key, src, kind, time.time()))
        else:
            conn.execute("DELETE FROM line_src_fix WHERE name=? AND source=? AND kind=?",
                         (key, src, kind))
    _SRC_FIX_CACHE = None
    return {"name": key, "source": src, "kind": kind, "on": bool(on)}


def line_override_map() -> Dict[str, dict]:
    global _line_ovr_cache
    if _line_ovr_cache is None:
        _line_ovr_cache = list_line_overrides()
    return _line_ovr_cache


def _apply_line_overrides() -> None:
    """把库里的人工线路站序 / 里程灌进 cnrail，并重建「站名→线路里程」索引"""
    cnrail.set_line_overrides({
        name: {"segKm": ov.get("segKm") or {}, "staKm": ov.get("staKm") or {},
               "stops": ov.get("stops") or [], "totalKm": ov.get("totalKm")}
        for name, ov in line_override_map().items()
    })


# ---------------------------------------------------------------------------
# 用户新增的线路 / 车站（档案里没有的，存库后合并进 cnrail 的内存表）
# ---------------------------------------------------------------------------

def _norm_line_name(name: str) -> str:
    return re.sub(r"\s+", "", str(name or ""))


def _entry_name(name: str) -> str:
    """录入用的站名口径：自建线路要和库内线路、车次站名对得上

    先按档案口径（cnrail.station_name，去「站」但保住「十八站」这种地名）；
    档案里还没收的站（唐海南、曹妃甸港这批）它判不出来，再问一次 12306 站表，
    输入「唐海南站」也存成「唐海南」。
    """
    sn = cnrail.station_name(_norm_line_name(name))
    if sn.endswith("站") and stations.telecode(sn):
        return stations.canonical(sn) or sn
    return sn


def _build_custom_line(name: str, stations, note: str = "") -> dict:
    """校验并整理一条新增线路：站序 + 每站「自起点累计里程」（可空）"""
    nm = _norm_line_name(name)
    if not nm:
        raise ValueError("线路名不能为空")
    if len(nm) > 30:
        raise ValueError("线路名太长（最多 30 字）")

    clean: List[dict] = []
    seen = set()
    for it in stations or []:
        # 站名统一成 12306 口径（去掉「站」后缀）：自建线路要和库内线路、
        # 车次站名对得上，否则「天津站」和「天津」会被当成两个站
        sn = _entry_name(it.get("name") if isinstance(it, dict) else it)
        if not sn:
            continue
        if sn in seen:
            raise ValueError("站点重复：" + sn)
        seen.add(sn)
        raw = it.get("km") if isinstance(it, dict) else None
        try:
            km = float(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            raise ValueError(f"{sn} 的里程不是数字")
        if km is not None and km < 0:
            raise ValueError(f"{sn} 的里程不能为负")
        clean.append({"name": sn, "km": round(km, 1) if km is not None else None})

    if len(clean) < 2:
        raise ValueError("一条线路至少要两个站")

    last = None
    for p in clean:
        if p["km"] is None:
            continue
        if last is not None and p["km"] <= last:
            raise ValueError(f"{p['name']} 的累计里程要大于前一站（{last} km）")
        last = p["km"]

    total = clean[-1]["km"] if clean[-1]["km"] is not None else None
    complete = all(p["km"] is not None for p in clean)
    return {
        "name": nm, "n": len(clean), "totalKm": total,
        "kmComplete": complete, "kmReliable": complete,
        "custom": True, "note": (note or "").strip(), "stations": clean,
    }


def list_custom_lines() -> List[dict]:
    try:
        with db() as conn:
            rows = conn.execute(
                "SELECT name, stations_json, note, updated_at, "
                "COALESCE(source,'manual') AS source FROM line_custom ORDER BY name"
            ).fetchall()
    except sqlite3.Error:
        return []
    out = []
    for row in rows:
        try:
            sts = json.loads(row["stations_json"] or "[]")
        except (TypeError, ValueError):
            sts = []
        out.append({"name": row["name"], "stations": sts, "n": len(sts),
                    "note": row["note"], "updatedAt": row["updated_at"],
                    "source": row["source"] or "manual"})
    return out


def _reload_coord_cache() -> None:
    """让 station_custom 里新补的坐标立即可用（reload_coords 会重建整个坐标缓存）"""
    try:
        stations.reload_coords()
    except Exception:                    # noqa: BLE001
        pass


def _fill_line_stations(line_name: str, names: Sequence[str]) -> List[str]:
    """自建线路落库后，把沿线站补进 station_custom —— 不补就没有坐标可画

    线路页的站点坐标读的是 cnrail 车站档案；档案里没有的站（唐海南、曹妃甸港
    这批货运支线上的站）坐标只存在于 OSM 派生的坐标表里，于是「站名能显示、
    却标缺坐标、地图上也没有点」。这里把档案里没有的站补成 station_custom
    （`source='line-fill'`，和 fill_missing_stations.py 一个口径），
    坐标按 `stations.coord()`（档案 → OSM → 人工补录 的合并值）填，
    取不到就留空 —— 界面照旧标「缺坐标」，补录走车站详情的「补坐标」。
    """
    names = [n for n in dict.fromkeys(names or []) if n]
    if not names:
        return []
    custom = {_norm_line_name(it["name"]) for it in list_custom_stations()}
    rows: List[tuple] = []
    for nm in names:
        if cnrail.station(nm) or _norm_line_name(nm) in custom:
            continue
        c = stations.coord(nm)
        src = stations.coord_info(nm).get("source") or ""
        typ = "线路所" if nm.endswith("线路所") or nm.endswith("所") else "车站"
        rows.append((
            nm, c[0] if c else None, c[1] if c else None,
            json.dumps([line_name], ensure_ascii=False),
            stations.telecode(nm) or "", typ,
            "manual" if (c and src == "manual") else ("osm" if c else ""),
        ))
    if not rows:
        return []
    now = time.time()
    with _db_lock, db() as conn:
        for nm, lat, lon, lines_json, tele, typ, csrc in rows:
            conn.execute(
                """INSERT INTO station_custom(name, lat, lon, lines_json, tele, bureau, note,
                                              updated_at, type, source, alias, coord_src)
                   VALUES(?,?,?,?,?,'','',?,?,'line-fill','',?)
                   ON CONFLICT(name) DO UPDATE SET
                     lat=COALESCE(station_custom.lat, excluded.lat),
                     lon=COALESCE(station_custom.lon, excluded.lon),
                     updated_at=excluded.updated_at""",
                (nm, lat, lon, lines_json, tele, now, typ, csrc),
            )
    return [r[0] for r in rows]


def backfill_line_stations() -> int:
    """把自建线路（含资料库带进来的）的沿线站统一补一遍，返回补了几站

    老库里已经存下的线路不可能回头重存，启动时顺手补一次，补过就什么都不写。
    """
    n = 0
    for it in list_custom_lines():
        try:
            ln = _build_custom_line(it["name"], it["stations"], it.get("note") or "")
        except ValueError:
            continue
        n += len(_fill_line_stations(ln["name"], [p["name"] for p in ln["stations"]]))
    if n:
        _apply_custom()                  # 补进来的车站要并进 cnrail，线路详情才有坐标
        _reload_coord_cache()
        _apply_coord_overrides()
    return n


def save_custom_line(name: str, stations, note: str = "", replace: bool = False) -> dict:
    """新增 / 覆盖一条自建线路

    replace=True 时允许覆盖「自己建的」同名线路（补站、改站序），档案线路与
    资料库带进来的线路仍然拒绝 —— 那些的站表不归这里管。
    """
    ln = _build_custom_line(name, stations, note)
    cur = cnrail.line(ln["name"])
    if cur and not (replace and cur.get("custom")):
        raise ValueError(f"线路库里已经有「{ln['name']}」，改里程请用「编辑里程」")
    now = time.time()
    with _db_lock, db() as conn:
        conn.execute(
            """INSERT INTO line_custom(name, stations_json, note, created_at, updated_at, source)
               VALUES(?,?,?,?,?,'manual')
               ON CONFLICT(name) DO UPDATE SET stations_json=excluded.stations_json,
                 note=excluded.note, updated_at=excluded.updated_at, source='manual'""",
            (ln["name"], json.dumps(ln["stations"], ensure_ascii=False), ln["note"], now, now),
        )
    _apply_custom()
    # 档案里没有的站补成自建车站（带 OSM / 人工坐标），否则线路页缺坐标、地图上没有点
    filled = _fill_line_stations(ln["name"], [p["name"] for p in ln["stations"]])
    if filled:
        _apply_custom()                  # 新补的车站要并进 cnrail，线路详情才拿得到坐标
        _reload_coord_cache()
        _apply_coord_overrides()
    detail = cnrail.line_detail(ln["name"]) or ln
    if isinstance(detail, dict):
        detail["filledStations"] = filled
    return detail


def delete_custom_line(name: str) -> bool:
    global _line_ovr_cache
    nm = _norm_line_name(name)
    with _db_lock, db() as conn:
        hit = conn.execute("DELETE FROM line_custom WHERE name=?", (nm,)).rowcount > 0
        # 顺手清掉这条线的里程校正，免得下次新建同名线路时套用旧值
        conn.execute("DELETE FROM line_override WHERE name=?", (nm,))
        # 资料库里指向它的挂靠也解开，否则搜索还会指到不存在的线路
        try:
            conn.execute("UPDATE line_catalog SET line_name=NULL, match_how=NULL "
                         "WHERE line_name=?", (nm,))
        except sqlite3.Error:
            pass
    if hit:
        _line_ovr_cache = None
        _apply_custom()
        _apply_line_overrides()
    return hit


def list_custom_stations() -> List[dict]:
    try:
        with db() as conn:
            rows = conn.execute(
                "SELECT name, lat, lon, lines_json, tele, bureau, note, updated_at, "
                "       type, source, alias, coord_src "
                "FROM station_custom ORDER BY name"
            ).fetchall()
    except sqlite3.Error:
        return []
    out = []
    for row in rows:
        try:
            lines = json.loads(row["lines_json"] or "[]")
        except (TypeError, ValueError):
            lines = []
        out.append({"name": row["name"], "lat": row["lat"], "lon": row["lon"],
                    "lines": lines, "tele": row["tele"], "bureau": row["bureau"],
                    "note": row["note"], "updatedAt": row["updated_at"],
                    "type": row["type"], "source": row["source"] or "manual",
                    "alias": row["alias"], "coordSrc": row["coord_src"]})
    return out


def save_custom_station(name: str, lat, lon, lines=None, tele: str = "",
                        bureau: str = "", note: str = "", coord=None,
                        stype: str = "") -> dict:
    # 站名统一成档案口径（「天津站」→「天津」，「十八站」保持不变）
    nm = cnrail.station_name(_norm_line_name(name))
    if not nm:
        raise ValueError("站名不能为空")
    if cnrail.station(nm):
        raise ValueError(f"车站库里已经有「{nm}」，改坐标请到「车站」详情里补")
    if coord not in (None, ""):
        lat, lon = parse_latlon(coord)
    else:
        lat, lon = _coord_pair(lat, lon)
    if isinstance(lines, str):
        lines = [x for x in re.split(r"[,，、;；\s]+", lines) if x]
    lines = [x.strip() for x in (lines or []) if str(x).strip()]

    now = time.time()
    with _db_lock, db() as conn:
        conn.execute(
            """INSERT INTO station_custom(name, lat, lon, lines_json, tele, bureau, note,
                                          updated_at, type, source, coord_src)
               VALUES(?,?,?,?,?,?,?,?,?,'manual','manual')
               ON CONFLICT(name) DO UPDATE SET lat=excluded.lat, lon=excluded.lon,
                 lines_json=excluded.lines_json, tele=excluded.tele,
                 bureau=excluded.bureau, note=excluded.note, updated_at=excluded.updated_at,
                 type=COALESCE(excluded.type, station_custom.type),
                 source='manual', coord_src='manual'""",
            (nm, lat, lon, json.dumps(lines, ensure_ascii=False),
             (tele or "").strip(), (bureau or "").strip(), (note or "").strip(), now,
             (stype or "").strip() or "车站"),
        )
    _apply_custom()
    reg = globals().get("stations")
    if reg is not None:
        try:
            reg.reload_coords()               # 让新站的坐标立刻可用
        except Exception:                     # noqa: BLE001
            pass
    return {"name": nm, "lat": lat, "lon": lon, "lines": lines}


def delete_custom_station(name: str) -> bool:
    nm = _norm_line_name(name)
    with _db_lock, db() as conn:
        hit = conn.execute("DELETE FROM station_custom WHERE name=?", (nm,)).rowcount > 0
    if hit:
        _apply_custom()
        reg = globals().get("stations")
        if reg is not None:
            try:
                reg.reload_coords()
            except Exception:                 # noqa: BLE001
                pass
    return hit


def _catalog_rows() -> List[dict]:
    """线路资料库（外部整理的线路参数 / 介绍 / 站表），整表读出"""
    try:
        with db() as conn:
            rows = conn.execute(
                """SELECT id, name, title, line_name, match_how, type, level, service,
                          alias, foreign_name, design_speed, run_speed, length_km,
                          open_date, from_sta, to_sta, intro, url
                   FROM line_catalog ORDER BY id""").fetchall()
            stops: Dict[str, List[dict]] = {}
            for r in conn.execute(
                    "SELECT lid, seq, name, km FROM line_catalog_stop ORDER BY lid, seq"):
                stops.setdefault(r["lid"], []).append(
                    {"seq": r["seq"], "name": r["name"], "km": r["km"]})
    except sqlite3.Error:
        return []
    out = []
    for row in rows:
        d = dict(row)
        d["foreign"] = d.pop("foreign_name", None)
        d["line_name"] = d.get("line_name")
        d["stops"] = stops.get(d["id"], [])
        out.append(d)
    return out


def _apply_catalog() -> None:
    """把线路资料库交给 cnrail：挂靠到线路详情，口径一致时补空白站里程"""
    cnrail.set_catalog(_catalog_rows())


def catalog_search(q: str, limit: int = 20) -> List[dict]:
    return cnrail.catalog_search(q, limit)


def catalog_detail(lid: str) -> Optional[dict]:
    rec = cnrail.catalog_get(lid)
    if not rec:
        return None
    stops = []
    for s in rec.get("stops") or []:
        c = cnrail.coord(s.get("name") or "")
        stops.append({**s, "lat": c[0] if c else None, "lon": c[1] if c else None})
    return {**rec, "stops": stops,
            "coordDone": sum(1 for s in stops if s["lat"] is not None)}


_NODE_SUFFIXES = ("线路所", "乘降所", "中继站", "所")


def is_node_stop(name: str) -> bool:
    """线路所 / 乘降所 / 中继站：不是客运站，但经由常在这里转线"""
    return bool(name) and str(name).endswith(_NODE_SUFFIXES)


def catalog_stop_names(line_name: str) -> List[str]:
    """只在资料库里有、命名线路库里认不出来的线路（银西高铁之于银西高速线）：直接给它的站序"""
    try:
        rec = cnrail.catalog_get(line_name) or {}
    except Exception:                        # noqa: BLE001
        return []
    names = [cnrail.station_name(s.get("name")) for s in (rec.get("stops") or [])]
    return list(dict.fromkeys(n for n in names if n))


def ce_stops(line_name: str, key: str = "") -> Optional[dict]:
    """china-emu（第五套来源）的线路站序。

    先按线名精确找，再按归一化键（match_key）找。桥隧行（kind='work'）
    不进站序 —— 它们是「丹昆特大桥」这种工程点，不是车站，里程栏写的是桥隧
    自身长度，混进来会把站距算成几万公里。

    km 是 K 里程（全线累计），这里统一减掉首站里程，变成和资料库一致的
    「从本线起点算」的口径。
    """
    nm = (line_name or "").strip()
    if not nm and not key:
        return None
    try:
        conn = db()
    except sqlite3.DatabaseError:
        return None
    try:
        if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='line_ce'").fetchone():
            return None
        row = conn.execute("SELECT * FROM line_ce WHERE name=?", (nm,)).fetchone()
        if not row and key:
            row = conn.execute(
                "SELECT * FROM line_ce WHERE match_key=?"
                " ORDER BY n_station DESC, n_km DESC LIMIT 1", (key,)).fetchone()
        if not row:
            return None
        rows = conn.execute(
            "SELECT seq,name,km,kind,cls,section FROM line_ce_stop"
            " WHERE lid=? AND kind<>'work' ORDER BY seq", (row["name"],)).fetchall()
    finally:
        conn.close()
    if len(rows) < 2:
        return None
    kms = [r["km"] for r in rows if r["km"] is not None]
    base = min(kms) if kms else None
    stops = []
    for r in rows:
        km = round(r["km"] - base, 1) if (r["km"] is not None and base is not None) else None
        stops.append({"seq": r["seq"], "name": r["name"], "km": km,
                      "kind": r["kind"], "cls": r["cls"], "section": r["section"]})
    return {"name": row["name"], "kind": row["kind"], "cats": row["cats"],
            "kmMin": row["km_min"], "kmMax": row["km_max"],
            "totalKm": row["total_km"], "nStop": row["n_stop"],
            "kmDesc": row["km_desc"] or 0, "stops": stops}


def osm_stops(line_name: str, key: str = "") -> Optional[dict]:
    """OSM（第六套来源）的线路站序。

    这份站序是**从几何算出来的**：way 端点按 40 m 聚合成节点、车站插值到最近的边上，
    km 是沿线真实里程（不是票价里程）。三个来源里它覆盖最广 —— 1727 条线、
    11788 个站次，专门补「资料库和 china-emu 都没有」的那一千多条线
    （瓦日线 44 站、嫩林线 43 站、甘钟线 25 站、红淖线 23 站）。

    两个口径已在入库时归一：
      - km 按首站归零（京沪线在 OSM 里从上海端算起，首站是 1434 而不是 0）
      - 站序**自带坐标**（lon/lat 列），不必再拿站名去别处反查

    role='secondary' 是并线枢纽站（该站同时属于别的线），不是噪音，照留。
    """
    nm = (line_name or "").strip()
    if not nm and not key:
        return None
    try:
        conn = db()
    except sqlite3.DatabaseError:
        return None
    try:
        if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='line_osm'").fetchone():
            return None
        row = conn.execute("SELECT * FROM line_osm WHERE name=?", (nm,)).fetchone()
        if not row and key:
            row = conn.execute(
                "SELECT * FROM line_osm WHERE match_key=?"
                " ORDER BY n_station DESC, total_km DESC LIMIT 1", (key,)).fetchone()
        if not row:
            return None
        rows = conn.execute(
            "SELECT seq,name,km,dist_m,role,halt,lon,lat FROM line_osm_stop"
            " WHERE lid=? ORDER BY seq", (row["name"],)).fetchall()
    finally:
        conn.close()
    if len(rows) < 2:
        return None
    stops = [{"seq": r["seq"], "name": r["name"], "km": r["km"],
              "distM": r["dist_m"], "role": r["role"], "halt": r["halt"],
              "lon": r["lon"], "lat": r["lat"]} for r in rows]
    return {"name": row["name"], "lineType": row["line_type"], "status": row["status"],
            "elec": row["elec"], "maxspeed": row["maxspeed"],
            "totalKm": row["total_km"], "nStop": row["n_stop"],
            "bridges": row["bridges"] or 0,
            "maxBridgeKm": row["max_bridge_km"] or 0, "stops": stops}


def osm_station_lines(name: str, limit: int = 24) -> List[dict]:
    """这个站在 OSM 里属于哪几条线（车站 → 线路的归属反查）

    归属是**距离 + 几何走向**判出来的：抽取时把车站插值到线路中心线上，留下
    垂直距离 dist_m 和沿线里程 km，dist_m 越大说明这个站其实离正线越远
    （支线引入、站场股道误判都在这种地方）。所以按 dist_m 升序返回，
    界面上从小到大地列 —— 排最前的就是真正把车站包在里面的那条线。

    同名站要都能查到（全国有多个「沙河」「临河」），所以按名字取全部 osm_id。
    """
    nm = re.sub(r"\s+", "", str(name or ""))
    if not nm:
        return []
    try:
        conn = db()
    except sqlite3.DatabaseError:
        return []
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table'"
                            " AND name='station_osm_line'").fetchone():
            return []
        rows = conn.execute(
            "SELECT g.lid, g.seq, g.km, g.dist_m, g.role, s.name AS sname,"
            "       s.osm_id, L.total_km, L.status, L.line_type"
            "  FROM station_osm_line g"
            "  JOIN station_osm s ON s.osm_id = g.osm_id"
            "  LEFT JOIN line_osm L ON L.name = g.lid"
            " WHERE s.name = ? OR s.name = ? OR s.name = ?"
            " ORDER BY COALESCE(g.dist_m, 0), g.lid"
            " LIMIT ?", (nm, nm + "站", nm[:-1] if nm.endswith("站") else nm, limit)
        ).fetchall()
    finally:
        conn.close()
    out = []
    seen = set()
    for r in rows:
        key = r["lid"]
        if key in seen:
            continue
        seen.add(key)
        out.append({"line": r["lid"], "seq": r["seq"], "km": r["km"],
                    "distM": round(r["dist_m"], 1) if r["dist_m"] is not None else None,
                    "role": r["role"], "totalKm": r["total_km"],
                    "status": r["status"], "lineType": r["line_type"],
                    "stationName": r["sname"], "osmId": r["osm_id"]})
    return out


def osm_station_suggest(q: str, limit: int = 12) -> List[dict]:
    """OSM 车站联想（国铁 railway=station/halt，不含地铁）

    **和站表/档案里的同名站不去重**：用户要的就是全都列出来让他自己挑。
    「石家庄」在项目站表里也有、OSM 里也有（京广线 / 京广高速线 / 石济客专线
    三线交汇），两边的数据不是一个口径，替他合并等于替他做判断。
    """
    kw = re.sub(r"\s+", "", str(q or ""))
    if not kw:
        return []
    try:
        conn = db()
    except sqlite3.DatabaseError:
        return []
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table'"
                            " AND name='station_osm'").fetchone():
            return []
        rows = conn.execute(
            "SELECT s.osm_id, s.name, s.railway, s.halt, s.ref, s.lifecycle,"
            "       COUNT(DISTINCT g.lid) AS n_line"
            "  FROM station_osm s LEFT JOIN station_osm_line g ON g.osm_id = s.osm_id"
            " WHERE s.name LIKE ?"
            " GROUP BY s.osm_id"
            " ORDER BY (CASE WHEN s.name = ? THEN 0"
            "                WHEN s.name LIKE ? THEN 1 ELSE 2 END),"
            "          n_line DESC, s.name LIMIT ?",
            (kw + "%", kw, kw + "%", limit)).fetchall()
    finally:
        conn.close()
    return [{"name": r["name"], "osmId": r["osm_id"],
             "railway": "乘降所" if r["halt"] else ("车站" if r["railway"] == "station" else (r["railway"] or "")),
             "ref": r["ref"], "lifecycle": r["lifecycle"], "nLine": r["n_line"]}
            for r in rows if r["name"]]


def osm_station_card(name: str) -> Optional[dict]:
    """只在 OSM 里有的车站（档案和 12306 站表都没有）的详情卡

    丰沙线上的「雁翅」「珠窝」、滨洲线上的小会让站这类，项目站表里查不到，
    但 OSM 知道它在哪条线的哪一公里、是车站还是乘降所。联想里既然把它们列了出来，
    点进去就不能只回一句「车站库里没有」—— 至少把 OSM 掌握的那点信息给出来，
    并且能顺着归属线路跳过去看整条线。
    """
    nm = re.sub(r"\s+", "", str(name or ""))
    if not nm:
        return None
    lines = osm_station_lines(nm)
    try:
        conn = db()
    except sqlite3.DatabaseError:
        return None
    try:
        rows = conn.execute(
            "SELECT * FROM station_osm WHERE name IN (?,?,?) ORDER BY halt",
            (nm, nm + "站", nm[:-1] if nm.endswith("站") else nm)).fetchall()
    finally:
        conn.close()
    if not rows:
        return None
    pts = [{"osmId": r["osm_id"], "name": r["name"], "nameRaw": r["name_raw"],
            "lon": r["lon"], "lat": r["lat"],
            "railway": "乘降所" if r["halt"] else r["railway"],
            "ref": r["ref"], "lifecycle": r["lifecycle"]} for r in rows]
    # 字段名与 station_detail 保持一致：详情页也用 osmLines，前端 stOsmLines()
    # 只认这一个键。档案里的 lines（线路名字符串数组）OSM 卡没有，别用空数组占位。
    return {"found": False, "osmOnly": True, "name": nm, "points": pts,
            "osmLines": lines, "services": [], "trainsFrom": 0, "trainsTo": 0,
            "trainsVia": [], "custom": False, "archive": False}


def merge_catalog_nodes(line_name: str, names: List[str]) -> List[str]:
    """命名线路的站序里常漏掉线路所（宝成线少了灭火沟线路所、长图线少了西山线路所）。

    资料库里存有同名线路的完整站序时，把这类节点按它在资料库里的前后邻居位置补进来：
    客运站的顺序一个不动，只是让「选车站」时也能挑到这些所。
    """
    names = [n for n in (names or []) if n]
    try:
        rec = cnrail.catalog_get(line_name) or {}
    except Exception:                        # noqa: BLE001
        return names
    cat = [cnrail.station_name(s.get("name")) for s in (rec.get("stops") or [])]
    cat = [n for n in cat if n]
    if len(cat) < 2:
        return names
    out = list(names)
    have = set(out)
    for i, nm in enumerate(cat):
        if nm in have or not is_node_stop(nm):
            continue
        pos = None
        for j in range(i - 1, -1, -1):       # 先找前一个能在现站序里对上的站
            if cat[j] in have:
                pos = out.index(cat[j]) + 1
                break
        if pos is None:                      # 没有就贴着后一个站
            for j in range(i + 1, len(cat)):
                if cat[j] in have:
                    pos = out.index(cat[j])
                    break
        if pos is None:
            pos = len(out)
        out.insert(pos, nm)
        have.add(nm)
    return out


def add_catalog_line(lid: str) -> dict:
    """把资料库里的一条线路（线路库还没有的）加成正式线路，站表与里程照抄资料"""
    rec = cnrail.catalog_get(lid)
    if not rec:
        raise ValueError("资料库里没有这条线路")
    name = cnrail.station_name(rec.get("name") or "")
    if not name:
        raise ValueError("线路名不可用")
    if cnrail.line(name):
        raise ValueError(f"线路库里已经有「{name}」了")
    stations, seen = [], set()
    for s in rec.get("stops") or []:
        nm = cnrail.station_name(s.get("name") or "")
        if not nm or nm in seen:
            continue
        seen.add(nm)
        stations.append({"name": nm, "km": s.get("km")})
    if len(stations) < 2:
        raise ValueError("这条线路可用站点不足两个")
    ln = _build_custom_line(name, stations, f"来自线路资料库（{rec.get('type') or '线路'}）")
    now = time.time()
    with _db_lock, db() as conn:
        conn.execute(
            """INSERT INTO line_custom(name, stations_json, note, created_at, updated_at, source)
               VALUES(?,?,?,?,?,'huirui')
               ON CONFLICT(name) DO UPDATE SET stations_json=excluded.stations_json,
                 note=excluded.note, updated_at=excluded.updated_at, source='huirui'""",
            (ln["name"], json.dumps(ln["stations"], ensure_ascii=False), ln["note"], now, now),
        )
        conn.execute("UPDATE line_catalog SET line_name=?, match_how='added' WHERE id=?",
                     (ln["name"], str(rec.get("id"))))
    _apply_catalog()
    _apply_custom()
    return cnrail.line_detail(ln["name"]) or ln


_OSM_STOP_TYPES = {"火车站", "线路所", "车站(仅线路表)", "铁路站场"}


def osm_line_stations(name: str, radius_m: int = 1200, extend_m: int = 3000) -> dict:
    """
    OSM-only 线路的候选站表：按几何反查车站库，加上「档案里注明属于这条线」的站。

    线路库里没有、只有 OSM 几何的线（联络线 / 支线最多见）没有站表，也就没法进
    车次经由与里程推断。这里给「新增线路」弹窗预填一份站点序列：里程按 OSM 走向
    折算，人只删减补漏，不用从空白开始一站站敲。
    """
    nm = _norm_line_name(name)
    if not nm:
        raise ValueError("缺少线路名")
    g = railgeom.geom(nm)
    if not g:
        raise ValueError(f"OSM 数据里没有这条线路：{nm}")
    cur = cnrail.line(nm)
    if cur and not cur.get("custom"):
        raise ValueError(f"线路库里已经有「{nm}」了，加站请走「编辑里程 / 站表」")

    pool, hint = [], []
    for st in cnrail.stations_all().values():
        if st.get("type") not in _OSM_STOP_TYPES:
            continue                        # 城市 / 地级行政区 / 地铁站不算线路上的站
        if st.get("lat") is None or st.get("lon") is None:
            continue
        sn = st.get("name") or ""
        if nm in (st.get("lines") or []):
            hint.append(sn)                 # 站表里直接写着属于这条线
        pool.append({"name": sn, "lat": st["lat"], "lon": st["lon"],
                     "type": st.get("type") or "", "tele": st.get("tele") or ""})

    rows = railgeom.stations_along(g.get("parts") or [], pool,
                                   radius_m=radius_m, extend_m=extend_m, must=hint)
    # 站名按 12306 口径归一（霸州西站 → 霸州西）：预填进「新增线路」后直接保存，
    # 名称带「站」会再次造出一批和车次经停站对不上的重名站
    for r in rows:
        r["name"] = _entry_name(r.get("name") or "")
    sp = railgeom.chain_info(g.get("parts") or [])
    # 丢掉的段长度会把复线也数进去，给用户看时按「源数据里程 − 串上的里程」封顶
    drop = min(sp["dropKm"], max(0.0, (g.get("km") or 0) - sp["km"]))
    existing = next((x for x in list_custom_lines() if x["name"] == nm), None)
    return {
        "name": nm,
        "osmKm": g.get("km"),
        "spineKm": sp["km"],
        "spineComplete": sp["complete"],
        "dupKm": sp["dupKm"],
        "dropKm": round(drop, 1),
        "nPart": g.get("nPart"),
        "nFrag": len(g.get("parts") or []),
        "radius": radius_m,
        "stations": rows,
        "hint": sorted(set(hint)),
        "existing": (existing or {}).get("stations") or [],
        "editable": bool(cur and cur.get("custom")),
        "note": (existing or {}).get("note") or "",
    }


def _apply_coord_overrides() -> int:
    """
    把人工补录的坐标（coord_override）套进 cnrail 车站表。

    之前只有车站详情（station_detail）读 coord_override，线路页 / 地图 / 站间里程
    走的是 cnrail.station() 里的经纬度，还是档案或 OSM 的原值 —— 于是改完坐标
    详情里是新值、地图上还是旧位置。这里统一成一处生效。

    必须在 _apply_custom() 之后调用：那边重建 _STATIONS 会把人工值冲掉。
    """
    try:
        with db() as conn:
            rows = conn.execute("SELECT name, lat, lon FROM coord_override").fetchall()
    except sqlite3.Error:
        return 0
    hit = 0
    for r in rows:
        if r["lat"] is None or r["lon"] is None:
            continue
        st = cnrail.station(r["name"])
        if not st:
            continue
        st["lat"] = float(r["lat"])
        st["lon"] = float(r["lon"])
        st["coordSrc"] = "manual"
        hit += 1
    return hit


def _apply_custom() -> None:
    """把库里新增的线路 / 车站合并进 cnrail"""
    global _LIB_IDX
    _LIB_IDX = None                      # 库变了，OSM 占用表跟着作废（osm_extras 用）
    lines: Dict[str, dict] = {}
    for it in list_custom_lines():
        try:
            ln = _build_custom_line(it["name"], it["stations"], it.get("note") or "")
        except ValueError:
            continue
        src = it.get("source") or "manual"
        ln["source"] = src
        ln["custom"] = src == "manual"        # 资料库带进来的不算「自建」
        lines[ln["name"]] = ln
    sts: Dict[str, dict] = {}
    for it in list_custom_stations():
        nm = _norm_line_name(it["name"])
        if nm:
            # 「XX线路所」「XX所」是独立设施，别被 cnrail._key 截成「XX」把车站顶掉
            k = nm if nm.endswith("所") else cnrail._key(nm)
            sts[k] = {
                "name": it["name"], "key": it["name"],
                "lat": it["lat"], "lon": it["lon"],
                "lines": it.get("lines") or [], "tele": it.get("tele") or "",
                "bureau": it.get("bureau") or "", "custom": True,
                "type": it.get("type") or "车站",
                "source": it.get("source") or "manual",
                "alias": it.get("alias") or "",
                "coordSrc": it.get("coordSrc") or "",
                "note": it.get("note") or "",
            }
    cnrail.set_custom(lines, sts)
    _apply_coord_overrides()     # 人工补录的坐标盖在档案 / OSM 之上


# --------------------------------------------------------------------------
# 线路库没收录、但 OSM 里有几何的线
# --------------------------------------------------------------------------
_LINE_TAIL_Q = re.compile(r"(高速铁路|高速线|客运专线|客专线|客专|城际铁路|城际线|高铁|普速线|铁路|线)$")

#: 派生线名（某某联络线 / 疏解线 / 第三线）：OSM 里是独立要素，但不是一条「线路」
_SIDING = re.compile(r"(联线|绕行线|进站线|站线|走行线|三线|四线|III线|Ⅱ线|I线|货线|大桥)")


def _core_key(name: str) -> str:
    """去掉「高速 / 客专 / 高铁 / 城际 / 铁路 / 线」这类后缀，得到「这是哪条线」的键。

    比 `railgeom._family()` 更宽：它把「京沪高铁」「京沪高速线」当两条，
    这里都归成「京沪」，用来判「同一条线的分段残片」。口径差异（客专 vs 普速）
    由 `_lib_index()` 里按名字本身判断，不靠这个键。
    """
    n = re.sub(r"\s+", "", str(name or ""))
    prev = None
    while prev != n:                     # 「京沪高铁线」要去两次：线 → 高铁
        prev = n
        n = _LINE_TAIL_Q.sub("", n)
    return n or str(name or "")

#: (OSM 线名 -> 占用它的库线名, 族键 -> 库线名列表, 库线名 -> 里程)；线路库一变就作废
_LIB_IDX: Optional[Tuple[Dict[str, str], Dict[str, List[str]], Dict[str, float]]] = None


def _lib_index() -> Tuple[Dict[str, str], Dict[str, List[str]], Dict[str, float]]:
    """
    线路库当前「占用」了哪些 OSM 线名。

    两边不是一一对应（库里叫「兰新客运专线」，OSM 里叫「兰新客专线」），
    所以按 `railgeom.match()` 反查一遍，把每条库线真正对上的那条 OSM 线记下来。
    名字像但口径不同的不算占用 —— 「津霸客运专线」是客专（72 km），
    「津霸线」是普速既有线（83 km），本来就是两条，客专收录了不代表普速线收录了。
    """
    global _LIB_IDX
    if _LIB_IDX is not None:
        return _LIB_IDX
    used: Dict[str, str] = {}
    fam: Dict[str, List[str]] = {}
    km: Dict[str, float] = {}
    try:
        lines = cnrail.search_line("", 100000)
    except Exception:                            # noqa: BLE001
        lines = []
    for ln in lines or []:
        nm = (ln.get("name") or "").strip()
        if not nm:
            continue
        used.setdefault(nm, nm)                  # 同名一定算收录（哪怕几何对不上）
        fam.setdefault(railgeom._family(nm), []).append(nm)
        try:
            km[nm] = float(ln.get("totalKm") or 0)
        except (TypeError, ValueError):
            km[nm] = 0.0
        try:
            hit = railgeom.match(nm, ln.get("totalKm"))
        except Exception:                        # noqa: BLE001
            hit = None
        if hit:
            used.setdefault(hit[0], nm)
    _LIB_IDX = (used, fam, km)
    return _LIB_IDX


def _lib_same_family(name: str) -> List[str]:
    """库里和它「同族不同口径」的线（津霸线 ↔ 津霸客运专线），界面用来说明不是同一条"""
    _used, fam, _km = _lib_index()
    return [x for x in fam.get(railgeom._family(name), []) if x != name]


def osm_extras(q: str, limit: int = 8, min_km: float = 5.0) -> List[dict]:
    """
    线路查询的「顺带提示」：和查询词相关、但线路库还没收录的 OSM 线路。

    为什么需要：搜「津霸」时线路库里的「津霸客运专线」直接命中，页面就停在这一条上，
    根本不会落到「查不到 → 去 OSM 找」那条兜底路径，于是 OSM 里同一前缀下的
    「津霸线」永远看不见。这类「被同名前缀挡住」的线不止一条（同类 400 多条 ≥20 km），
    所以在命中结果里附一份差集，点开就能看走向、补站、收进线路库。

    滤掉两类噪声：
      * 短于 min_km 的（疏解线、站线、分段残片，用户查线路时不需要）
      * 同族里已有更长一条被收录的（京沪高速线 1296 km 收录了，
        「京沪高速铁路」9 km 这种 OSM 残段就不必再提）
    """
    q = (q or "").strip()
    if not q:
        return []
    used, _fam, lib_km = _lib_index()
    used_km: Dict[str, float] = {}
    for osm_nm, lib_nm in used.items():
        a = float((railgeom._META.get(osm_nm) or {}).get("km") or 0)
        used_km[osm_nm] = max(a, float(lib_km.get(lib_nm) or 0))
    used_core: Dict[str, List[float]] = {}
    for nm, km in used_km.items():
        used_core.setdefault(_core_key(nm), []).append(km)
    core = _LINE_TAIL_Q.sub("", q) or q        # 「津霸客运专线」也按「津霸」去对 OSM
    out: List[dict] = []
    seen = set()
    cands = railgeom.search(q, 300)
    if core != q:
        cands += railgeom.search(core, 300)
    for it in cands:
        nm = (it.get("name") or "").strip()
        if not nm or nm in seen or nm in used:
            continue
        seen.add(nm)
        if _SIDING.search(nm):
            continue                           # 联络线 / 疏解线 / 第三线：不是一条独立线路
        km = float(it.get("km") or 0)
        if km < min_km:
            continue
        sib = used_core.get(_core_key(nm)) or []
        if sib and km < max(sib) * 0.35:
            continue                           # 同族的长线已收录，这是它的残段
        out.append({
            "name": nm, "km": round(km, 1),
            "nPart": it.get("nPart"), "nBridge": it.get("nBridge"),
            "nTunnel": it.get("nTunnel"), "nFeat": it.get("nFeat"),
            "bbox": it.get("bbox"), "inLib": False,
            "libSame": _lib_same_family(nm),
        })
    # 和查询词同前缀的排前面（「津霸线」在「津霸上行疏解线」之前），同档按里程
    out.sort(key=lambda r: (0 if r["name"].startswith(core) else 1, -(r["km"] or 0)))
    return out[:limit]


def cand_lines(a: str, b: str) -> List[dict]:
    """两站之间的候选线路（转发 cnrail.cand_lines，供校对页点选）"""
    return cnrail.cand_lines(a, b)


def line_cover(name: str, stops) -> int:
    """这条线覆盖了这趟车多少个经停站（候选线路排序用）"""
    return cnrail.line_cover(name, list(stops or []))


def reload_library() -> dict:
    """
    把库里的自建线路 / 车站 / 人工坐标重新灌进 cnrail，返回线路库概况。

    「新增线路」保存时本来就会 `_apply_custom()`，但校对页可能开着编辑面板、
    前端又缓存着线路名清单 —— 线路库改过之后要有个明确的「重算」入口，
    让自动经由按新线路重新推一遍（页面上的按钮就走这里）。
    """
    global _LIB_IDX
    stations.reload_coords()
    _apply_custom()
    _LIB_IDX = None                      # 新加的线可能正好对上 OSM 里的某条，占用表要重算
    return {"lines": len(cnrail.search_line("", 20000)),
            "customLines": len(list_custom_lines()),
            "stations": len(cnrail._STATIONS or {})}


def get_line_override(name: str) -> Optional[dict]:
    ln = cnrail.line(name)
    if not ln:
        return None
    ov = line_override_map().get(ln["name"])
    return {"name": ln["name"], **ov} if ov else None


def rebase_km_from_catalog(name: str, save: bool = True) -> dict:
    """
    用线路资料库里的里程锚点重算整条线的累计里程。

    档案里有些线路的里程是「按站数摊」出来的（京沪线泰山 1463 km，实际 555.8），
    资料库的里程来自铁路里程表，锚点可信。做法：
      · 资料库里给了里程的站 → 直接当锚点
      · 锚点之间的站 → 按站间直线距离在锚点里程区间内插值（火车不会飞直线，
        但同一区间内线性映射，比例误差很小）
      · 首锚点之前 / 末锚点之后 → 借相邻区间的「每公里直线距离对应的里程」外推
      · 没有坐标的站 → 退回按站序均分
    结果写成人工校正（staKm），随时可以「恢复档案值」撤回。
    """
    ln = cnrail.line(name)
    if not ln:
        raise ValueError("线路库里没有这条线：" + str(name or ""))
    rec = cnrail.catalog_of(ln["name"])
    if not rec:
        raise ValueError(f"线路资料库里没有「{ln['name']}」的里程数据")

    stations = ln.get("stations") or []
    hint: Dict[str, float] = {}
    for s in rec.get("stops") or []:
        k = cnrail._key(s.get("name") or "")
        if k and s.get("km") is not None and k not in hint:
            hint[k] = float(s["km"])
    anchors = [(i, hint[cnrail._key(p.get("name") or "")])
               for i, p in enumerate(stations)
               if cnrail._key(p.get("name") or "") in hint]
    if len(anchors) < 2:
        raise ValueError(f"资料库里这条线只有 {len(anchors)} 个带里程的站，不够重算")

    pts = []
    for p in stations:
        c = cnrail.coord(p.get("name") or "")
        pts.append(c)

    def d(a: int, b: int) -> Optional[float]:
        if pts[a] and pts[b]:
            return haversine(pts[a][0], pts[a][1], pts[b][0], pts[b][1])
        return None

    def spread(a: int, b: int, ka: float, kb: float) -> Dict[int, float]:
        """a、b 是两个锚点（含端点），中间的站按直线距离比例摊开"""
        out: Dict[int, float] = {}
        if b <= a:
            return out
        legs = [d(j, j + 1) for j in range(a, b)]
        if any(x is None for x in legs) or sum(legs) <= 0:
            step = (kb - ka) / (b - a)                 # 缺坐标：按站序均分
            return {j: ka + step * (j - a) for j in range(a + 1, b)}
        cum = 0.0
        total = sum(legs)
        for j in range(a + 1, b):
            cum += legs[j - 1 - a]
            out[j] = ka + (cum / total) * (kb - ka)
        return out

    kms: Dict[int, float] = {anchors[0][0]: anchors[0][1], anchors[-1][0]: anchors[-1][1]}
    for (a, ka), (b, kb) in zip(anchors, anchors[1:]):
        kms[a], kms[b] = ka, kb
        kms.update(spread(a, b, ka, kb))

    # 首尾外推
    (a1, k1), (a2, k2) = anchors[0], anchors[1]
    if a1 > 0:
        base = d(a1, a2)
        ratio = ((k2 - k1) / base) if (base and k2 > k1) else None
        for j in range(a1 - 1, -1, -1):
            dd = d(j, j + 1)
            if ratio and dd:
                kms[j] = kms[j + 1] - dd * ratio
            else:
                kms[j] = kms[j + 1] - (k2 - k1) / max(a2 - a1, 1)
    (b1, m1), (b2, m2) = anchors[-2], anchors[-1]
    if b2 < len(stations) - 1:
        base = d(b1, b2)
        ratio = ((m2 - m1) / base) if (base and m2 > m1) else None
        for j in range(b2 + 1, len(stations)):
            dd = d(j - 1, j)
            if ratio and dd:
                kms[j] = kms[j - 1] + dd * ratio
            else:
                kms[j] = kms[j - 1] + (m2 - m1) / max(b2 - b1, 1)

    ordered = [round(kms.get(i, 0.0), 1) for i in range(len(stations))]
    ordered[0] = round(anchors[0][1], 1)
    bad = [i for i in range(1, len(ordered)) if ordered[i] <= ordered[i - 1]]
    if bad:
        raise ValueError(f"重算后有 {len(bad)} 处里程倒挂（第 {bad[0] + 1} 站起），"
                         "资料库站序与库内线路可能不一致，未写入")
    sta = {str(i): v for i, v in enumerate(ordered)}
    note = (get_line_override(ln["name"]) or {}).get("note") or ""
    note = (note + " · 按线路资料库里程重算").strip(" ·")
    if save:
        save_line_override(ln["name"], seg_km={}, total_km=None, note=note, sta_km=sta)
    return {"name": ln["name"], "stations": len(ordered), "anchors": len(anchors),
            "totalKm": ordered[-1], "staKm": sta, "saved": bool(save)}


def save_line_override(name: str, seg_km=None, total_km=None, note: str = "",
                       sta_km=None) -> dict:
    """
    保存线路里程人工校正：
      seg_km 键为区间序号（第 i 站 → 第 i+1 站），值是这一段站间距；
      sta_km 键为车站序号，值是「自起点站算起的累计里程」，None = 这一站明确不详。
    全部为空即撤销，回到档案值。给了总里程时只把「没手填」的段等比缩放到 totalKm − 手填段之和。
    """
    ln = cnrail.line(name)
    if not ln:
        raise ValueError("线路库里没有这条线：" + str(name or ""))
    lname = ln["name"]
    segs = _clean_seg_km(seg_km)
    stas = _clean_sta_km(sta_km)
    try:
        km = float(total_km) if total_km not in (None, "", 0) else None
    except (TypeError, ValueError):
        raise ValueError("总里程必须是数字")
    if km is not None and not (1 <= km <= 20000):
        raise ValueError("线路总里程超出合理范围（1~20000 km）")

    nst = len(ln.get("stations") or [])
    nseg = max(nst - 1, 0)
    bad = [k for k in segs if int(k) >= nseg]
    if bad:
        raise ValueError(f"区间序号超出范围（这条线共 {nseg} 段）")
    bad = [k for k in stas if int(k) >= nst]
    if bad:
        raise ValueError(f"车站序号超出范围（这条线共 {nst} 站）")

    note = (note or "").strip()
    if not segs and not stas and km is None and not note:
        clear_line_override(lname)
        return {"name": lname, "cleared": True}

    with _db_lock, db() as conn:
        conn.execute(
            """INSERT INTO line_override(name, seg_km_json, sta_km_json, total_km, note, updated_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET
                 seg_km_json=excluded.seg_km_json, sta_km_json=excluded.sta_km_json,
                 total_km=excluded.total_km,
                 note=excluded.note, updated_at=excluded.updated_at""",
            (lname, json.dumps(segs, ensure_ascii=False),
             json.dumps(stas, ensure_ascii=False), km, note, time.time()),
        )
    global _line_ovr_cache
    _line_ovr_cache = None
    _apply_line_overrides()
    return {"name": lname, "segKm": segs, "staKm": stas, "totalKm": km, "note": note}


def clear_line_override(name: str) -> bool:
    """清掉人工里程，回到档案值

    只改过站序的线（stops_json 有值）保留站序 —— 「恢复档案值」说的是里程，
    不该顺带把用户插的站一起撤掉；站序要恢复走「恢复档案站序」。
    """
    ln = cnrail.line(name)
    lname = ln["name"] if ln else _norm(name)
    with _db_lock, db() as conn:
        row = conn.execute("SELECT stops_json FROM line_override WHERE name=?",
                           (lname,)).fetchone()
        hit = False
        if row:
            if (row["stops_json"] or "").strip() not in ("", "[]", "null"):
                conn.execute(
                    "UPDATE line_override SET seg_km_json='{}', sta_km_json='{}', "
                    "total_km=NULL, note='', updated_at=? WHERE name=?",
                    (time.time(), lname))
            else:
                conn.execute("DELETE FROM line_override WHERE name=?", (lname,))
            hit = True
    global _line_ovr_cache
    _line_ovr_cache = None
    _apply_line_overrides()
    return hit


def _adopt_loose_line(name: str) -> Optional[dict]:
    """线路库里没有、但资料库 / china-emu / 分段能拼出站序的线：先收进线路库

    侯西线就是这种：底表没有，站序只在资料库（42 站）里，走向只有 OSM 有。
    线路页能打开、能「改站序」，但保存时找不到库内对象，只能报
    「线路库里没有这条线」—— 用户改了半天白改。所以这里按资料库的规范名
    落一条自建线（站序先按来源照抄），紧接着的覆盖写会把用户改的结果落上去。

    线名优先用资料库那条的 name（搜「侯西铁路」时传进来的是 title）。
    """
    if not name:
        return None
    try:
        import lineseg                        # 延迟导入：lineseg 顶层依赖本模块
    except Exception:                        # noqa: BLE001
        return None
    try:
        got = lineseg.stations_for(name) or {}
    except Exception:                        # noqa: BLE001
        return None
    stops = got.get("stops") or []
    if len(stops) < 2:
        return None
    canon = name
    if got.get("source") == "catalog" and got.get("sourceName"):
        canon = got["sourceName"]
    for cand in (canon, name):
        hit = cnrail.line(cand)
        if hit:
            return hit
    rows = [{"name": s.get("name"), "km": s.get("km")} for s in stops if s.get("name")]
    src = {"catalog": "线路资料库", "chinaemu": "china-emu"}.get(
        got.get("source") or "", "分段线拼")
    save_custom_line(canon, rows,
                     f"改站序时按{src}的站序收进线路库", replace=False)
    # 自建线路的总里程只能从站里程算，而这类线的资料常常只有总长没有逐站里程
    # （侯西线 42 站一个都没有）→ 总里程会变「-」。把资料库给的总长存成人工里程：
    # _rebuild_km 在站里程全空时不会均分，站里程仍然是缺的，不会造出假数据。
    km = got.get("totalKm")
    if km:
        try:
            save_line_override(canon, None, km, "", None)
        except (ValueError, sqlite3.Error):      # 总长不合法就算了，不影响站序
            pass
    return cnrail.line(canon)


def save_line_stations(name: str, stations, note: str = "") -> dict:
    """改一条线的站序（站中间插站、调先后）—— 自建 / 档案 / 库外线路都走这里

    自建线路直接改自己的站表；档案线路改不了原始档案，改的是本机覆盖（line_override
    的 stops_json）：整份替换档案站序，`/api/line` 一读就套用，随时能「恢复档案站序」。
    站序一变，旧人工里程的下标（第 i 段 / 第 i 站）就对不上了，所以一并清掉，
    改为按这次填的累计里程重算。

    线路库里没有、只有资料库或 OSM 拼得出来的线（侯西线），先收进线路库再改，
    返回的 detail 里 `adopted=True`、`adoptedName` 是收进去以后的名字。
    """
    ln = cnrail.line(name)
    adopted_from = ""
    if not ln:
        ln = _adopt_loose_line(name)
        if ln:
            adopted_from = str(name or "")
        else:
            raise ValueError("线路库里没有这条线：" + str(name or "")
                             + "，资料库里也查不到它的站序")
    stops = _clean_stops(stations)
    # 落库前按档案 / 12306 口径归一（「唐海南站」→「唐海南」），和车次经停站对得上
    stops = [{"name": _entry_name(s["name"]), "km": s["km"]} for s in stops]
    stops = [s for s in stops if s["name"]]
    if len(stops) < 2:
        raise ValueError("一条线路至少要两个车站")
    names = [s["name"] for s in stops]
    dup = list(dict.fromkeys(n for i, n in enumerate(names) if n in names[:i]))
    if dup:
        raise ValueError("站表里有重复车站：" + "、".join(dup))

    lname = ln["name"]
    now = time.time()
    if ln.get("custom"):
        old = next((x for x in list_custom_lines() if x["name"] == lname), None)
        detail = save_custom_line(lname, stops, note or (old or {}).get("note") or "",
                                  replace=True)
        detail["stationOrder"] = "custom"
        if adopted_from:
            detail["adopted"] = True
            detail["adoptedFrom"] = adopted_from
        return detail

    # 档案线路：存成人工站序（保留备注；下标类里程一律作废）
    old_ov = line_override_map().get(lname) or {}
    note = (note or "").strip() or (old_ov.get("note") or "")
    with _db_lock, db() as conn:
        conn.execute(
            """INSERT INTO line_override(name, seg_km_json, sta_km_json, stops_json,
                                         total_km, note, updated_at)
               VALUES(?,'{}','{}',?,NULL,?,?)
               ON CONFLICT(name) DO UPDATE SET
                 seg_km_json='{}', sta_km_json='{}', stops_json=excluded.stops_json,
                 total_km=NULL, note=excluded.note, updated_at=excluded.updated_at""",
            (lname, json.dumps(stops, ensure_ascii=False), note, now),
        )
    global _line_ovr_cache, _LIB_IDX
    _line_ovr_cache = None
    _apply_line_overrides()
    _LIB_IDX = None                       # 站序变了，OSM 占用比对要重算

    filled = _fill_line_stations(lname, names)
    if filled:
        _apply_custom()                   # 新补进来的站要并进 cnrail，线路页才有坐标
        _reload_coord_cache()
        _apply_coord_overrides()
    detail = cnrail.line_detail(lname) or dict(ln)
    detail["stationOrder"] = "override"
    detail["filledStations"] = filled
    return detail


def clear_line_stations(name: str) -> dict:
    """撤销档案线路的人工站序，回到档案原样

    站序变了以后旧里程下标没意义，所以整行校正一起清掉（界面会说明）。
    """
    ln = cnrail.line(name)
    if not ln:
        raise ValueError("线路库里没有这条线：" + str(name or ""))
    lname = ln["name"]
    if ln.get("custom"):
        raise ValueError("自建线路没有档案站序可恢复，直接改站表即可")
    with _db_lock, db() as conn:
        hit = conn.execute("DELETE FROM line_override WHERE name=?", (lname,)).rowcount > 0
    global _line_ovr_cache, _LIB_IDX
    _line_ovr_cache = None
    _apply_line_overrides()
    _LIB_IDX = None
    return {"name": lname, "cleared": hit, "line": cnrail.line_detail(lname)}


def _coord_pair(lat, lon) -> Tuple[float, float]:
    """
    把前端/接口传来的坐标整理成 (lat, lon)。

    两种传法都认：
      - 两个数字：lat / lon
      - 一行文本：lat 或 lon 里是「经度:126.576636,纬度:42.038926」这种（另一格填空）
    """
    lo_s = "" if lon is None else str(lon).strip()
    la_s = "" if lat is None else str(lat).strip()
    # 明显是「一行文本」的：里面除了数字还有别的字
    for one in (lo_s, la_s):
        if one and re.search(r"[^\d.eE+\-\s]", one):
            return parse_latlon(one)
    if not la_s or not lo_s:
        raise ValueError("经纬度要成对填，比如「经度:126.576636,纬度:42.038926」")
    try:
        a, b = float(la_s), float(lo_s)
    except (TypeError, ValueError):
        raise ValueError("经纬度必须是数字")
    if not (-90 <= a <= 90) or not (-180 <= b <= 180):
        raise ValueError("经纬度超出范围（纬度 ±90，经度 ±180）")
    if a == 0 and b == 0:
        raise ValueError("(0,0) 多半是填错了")
    return round(a, 6), round(b, 6)


def set_coord(name: str, lat, lon, coord=None) -> dict:
    """人工补录车站坐标（写 coord_override，并让坐标缓存失效重载）

    coord 可以直接给一行文本（「经度:126.576636,纬度:42.038926」），这时 lat/lon 留空。
    """
    name = _norm(name)
    if not name:
        raise ValueError("缺少站名")
    st = cnrail.station(name)
    if st and st.get("key"):
        name = st["key"]                       # 与档案口径对齐（「天津站」→「天津」）
    if coord not in (None, ""):
        lat, lon = parse_latlon(coord)
    else:
        lat, lon = _coord_pair(lat, lon)
    with _db_lock, db() as conn:
        conn.execute(
            """INSERT INTO coord_override(name, lat, lon) VALUES(?,?,?)
               ON CONFLICT(name) DO UPDATE SET lat=excluded.lat, lon=excluded.lon""",
            (name, lat, lon),
        )
    _apply_coord_overrides()          # 线路页 / 地图读的是 cnrail 那份，一起更新
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
    if removed:
        _apply_custom()               # 重建 _STATIONS，把这站的人工值抹掉
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
        """站名联想：12306 站表 + 车站档案（含自建 / 线路补全的车站）

        两边叫法不统一 —— 12306 站表里是「唐海南」，线路档案里多半是「唐山站」——
        所以查询词先去掉末尾的「站」，候选也按去掉「站」的名字归并，
        同一个站只出现一次，显示优先用 12306 的写法。

        **每个候选带 src 来源标记**（12306 站表 / 线路档案 / 两者都有）：
        同一个名字在两边的数据不是一个口径 —— 站表里有电报码和当日车次，
        档案里有路局、启用年份、连接线路。界面上标出来，用户点哪个取哪个，
        系统不再替他挑。
        """
        self.ensure()
        q = _norm(q)
        if not q:
            return []
        bare_q = _bare(q)
        low = q.lower()
        cand: Dict[str, dict] = {}

        def put(name: str, city: str, archived: bool,
                pinyin: str = "", short: str = "") -> None:
            nm = _norm(name)
            b = _bare(nm)
            if not b:
                return
            if b == bare_q:
                hit = 0
            elif b.startswith(bare_q) or nm.startswith(q):
                hit = 1
            elif bare_q in b:
                hit = 2
            elif pinyin.startswith(low) or short.startswith(low):
                hit = 3
            else:
                return
            has = bool(self.coords.get(b) or self.coords.get(nm) or self.coords.get(b + "站"))
            cur = cand.get(b)
            if cur is None:
                cand[b] = {"hit": hit, "name": nm, "city": city,
                           "hasCoord": has, "archive": archived,
                           "from123": not archived, "fromArch": archived}
                return
            cur["hit"] = min(cur["hit"], hit)
            cur["archive"] = cur["archive"] and archived   # 两边都有才不算「仅档案」
            cur["from123"] = cur["from123"] or not archived
            cur["fromArch"] = cur["fromArch"] or archived
            cur["hasCoord"] = cur["hasCoord"] or has
            if not cur["city"] and city:
                cur["city"] = city
            if len(nm) < len(cur["name"]):          # 「唐山站」和「唐山」并成「唐山」
                cur["name"] = nm

        for name, code in self.by_name.items():
            st = self.by_code.get(code, {})
            put(name, st.get("city") or "", False,
                st.get("station_pinyin") or "", st.get("station_short") or "")

        # 12306 站表里没有、但线路 / 档案里有的站（补全进来的那批就在这里）
        for key, st in (cnrail._STATIONS or {}).items():
            put(st.get("name") or key, st.get("city") or "", True)

        out = sorted(cand.values(), key=lambda x: (x["hit"], len(x["name"]), x["name"]))
        return [
            {"name": x["name"], "city": x["city"],
             "hasCoord": x["hasCoord"], "archive": x["archive"],
             "src": "两边都有" if (x["from123"] and x["fromArch"])
                    else ("线路档案" if x["fromArch"] else "12306 站表")}
            for x in out[:limit]
        ]

    def all_names(self) -> List[str]:
        """全部站名（12306 站表 + 线路档案），给「新增线路」的站名下拉一次取全

        和 suggest() 用同一套来源：唐海南 / 曹妃甸港这类只在 12306 站表里、
        线路档案还没收的站，搜索框能搜到、下拉里却没有，只因为下拉原来只读
        档案那一半。这里取并集，同一个站按去掉末尾「站」的名字归并，
        写法取短的（「唐山站」和「唐山」只留「唐山」）。
        """
        self.ensure()
        seen: Dict[str, str] = {}
        names = list(self.by_name)
        names += [st.get("name") or k for k, st in (cnrail._STATIONS or {}).items()]
        for name in names:
            nm = _norm(name)
            b = _bare(nm)
            if not b:
                continue
            # 同一个站按去掉末尾「站」的名字归并，写法取短的（「唐山站」和「唐山」只留
            # 「唐山」）。档案里有的站只写了长写法（「唐山东站」），照样留着 ——
            # 不能一律截成「唐山东」：「九站」这种地名去掉「站」就成了另一个站。
            # 输入「唐山东」也能搜到它（前端 nmBare 按同一口径比对），落库仍按
            # _entry_name 归一，所以不会造出重复的站。
            old = seen.get(b)
            if old is None or len(nm) < len(old):
                seen[b] = nm
        return sorted(seen.values())


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
    # 取每个车次号「最近一天」的那一行：MAX() 分开聚合会把站数/起讫取到不同日期上，
    # 同一趟车的两个号就会因为站数对不上而合不到一起
    stmt = """
        SELECT t.code, t.train_no, t.type, t.start_sta, t.end_sta, t.stop_count, t.date
        FROM trains t
        JOIN (SELECT code, MAX(date) AS md FROM trains GROUP BY code) m
          ON m.code = t.code AND m.md = t.date
        WHERE t.code LIKE ? OR t.start_sta LIKE ? OR t.end_sta LIKE ?
        GROUP BY t.code
        ORDER BY
          CASE WHEN t.code = ? THEN 0
               WHEN t.code LIKE ? THEN 1
               WHEN t.start_sta = ? THEN 2
               WHEN t.end_sta = ? THEN 3
               ELSE 4 END,
          LENGTH(t.code), t.code
        LIMIT ?
    """
    with db() as conn:
        rows = [dict(r) for r in conn.execute(
            stmt,
            (q + "%", q + "%", q + "%", q.upper(), q.upper() + "%", q, q, limit * 3),
        ).fetchall()]
        # 同一 train_no 的另一个车次号未必命中 LIKE（搜 4358 时 4355 那行不在结果里），
        # 单独捞一遍，才能合并成「4355/8」
        pairs = {(r["train_no"], r["date"]) for r in rows if r.get("train_no")}
        extra: List[dict] = []
        for tn, d in pairs:
            extra.extend(dict(r) for r in conn.execute(
                "SELECT code, train_no, type, start_sta, end_sta, stop_count, date "
                "FROM trains WHERE train_no=? AND date=? GROUP BY code", (tn, d)).fetchall())
        items = _merge_train_rows(rows + extra, conn)
    return items[:limit]


def code_label(codes, primary: Optional[str] = None) -> str:
    """同一趟车的多个车次号按铁路写法合并：4355 + 4358 → 4355/8，K545 + K548 → K545/8

    只在「末一两位不同」时缩写（那是同一趟车换向换号的典型写头），差得远就写全。
    """
    cs = [str(c).strip().upper() for c in (codes or []) if str(c or "").strip()]
    cs = list(dict.fromkeys(cs))
    if not cs:
        return ""
    if len(cs) == 1:
        return cs[0]
    main = primary if primary in cs else sorted(cs, key=lambda c: (len(c), c))[0]
    outs = []
    for c in cs:
        if c == main:
            continue
        i = 0
        while i < min(len(main), len(c)) and main[i] == c[i]:
            i += 1
        tail = c[i:]
        outs.append(tail if 0 < len(tail) <= 2 and i >= len(main) - 2 else c)
    return main + "/" + "/".join(outs)


def _origin_code(train_no: str, date: Optional[str], conn) -> Optional[str]:
    """这趟车在始发站用的车次号（routes 缓存里第一站的 station_train_code）"""
    if not train_no:
        return None
    try:
        row = conn.execute(
            "SELECT raw FROM routes WHERE train_no=? AND date=? AND raw IS NOT NULL",
            (train_no, date or ""),
        ).fetchone()
        if not row:
            row = conn.execute(
                "SELECT raw FROM routes WHERE train_no=? AND raw IS NOT NULL "
                "ORDER BY date DESC LIMIT 1", (train_no,)).fetchone()
        if not row:
            return None
        raw = json.loads(row["raw"] or "[]")
        return (raw[0].get("station_train_code") or "").strip() or None if raw else None
    except (sqlite3.Error, TypeError, ValueError):
        return None


def _merge_train_rows(rows: List[dict], conn=None) -> List[dict]:
    """把「同一趟车的两个车次号」合成一行

    4355 与 4358 在 `trains` 里是两行、在 12306 上也是两条搜索结果，其实是同一趟车
    （双辽换向，车次号从 4355 变 4358）。同一个 `train_no` + 同一天 + 起讫站和站数都
    一样，就是同一趟车的两个号，列表里该显示成「4355/8」一行。
    """
    out: List[dict] = []
    seen: Dict[tuple, dict] = {}
    for r in rows:
        tn = (r.get("train_no") or "").strip()
        if not tn:
            out.append({**r, "codes": [r["code"]], "codeLabel": r["code"]})
            continue
        key = (tn, r.get("date") or "", r.get("start_sta") or "",
               r.get("end_sta") or "", r.get("stop_count") or 0)
        hit = seen.get(key)
        if hit is not None:
            if r["code"] not in hit["codes"]:
                hit["codes"].append(r["code"])
            continue
        it = {**r, "codes": [r["code"]], "codeLabel": r["code"]}
        seen[key] = it
        out.append(it)

    for it in out:
        if len(it["codes"]) < 2:
            continue
        origin = _origin_code(it.get("train_no"), it.get("date"), conn) if conn else None
        label = code_label(it["codes"], origin)
        it["codeLabel"] = label
        it["code"] = label.split("/")[0]
    return out


def code_infos(codes) -> Dict[str, dict]:
    """一批车次号的「合并写法」信息：{4355: {codes:[4355,4358], codeLabel:'4355/8'}, ...}

    给校对页这类按车次逐行列出的地方用：两行都挂上同一个写法，一眼看出是同一趟车。
    """
    codes = [str(c or "").strip().upper() for c in (codes or [])]
    codes = [c for c in dict.fromkeys(codes) if c]
    out = {c: {"codes": [c], "codeLabel": c} for c in codes}
    if not codes:
        return out
    try:
        with db() as conn:
            marks = ",".join("?" * len(codes))
            rows = conn.execute(
                f"""SELECT t.code, t.train_no, t.date FROM trains t
                    JOIN (SELECT code, MAX(date) AS md FROM trains GROUP BY code) m
                      ON m.code = t.code AND m.md = t.date
                    WHERE t.code IN ({marks}) GROUP BY t.code""", codes).fetchall()
            pairs = {(r["train_no"], r["date"]) for r in rows if r["train_no"]}
            for tn, d in pairs:
                full = [x["code"] for x in conn.execute(
                    "SELECT DISTINCT code FROM trains WHERE train_no=? AND date=?",
                    (tn, d)).fetchall()]
                if len(full) < 2:
                    continue
                label = code_label(full, _origin_code(tn, d, conn))
                for c in full:
                    out[c] = {"codes": full, "codeLabel": label}
    except sqlite3.Error:
        pass
    return out


def code_info(code: str) -> dict:
    code = (code or "").strip().upper()
    return code_infos([code]).get(code, {"codes": [code], "codeLabel": code})


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
    """写一天的经停站。内容与这趟车已有版本相同就只挂个指针，不再存整份"""
    with _db_lock, db() as conn:
        routestore.put_route(conn, train_no, date, code,
                             json.dumps(raw, ensure_ascii=False))


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

def latest_date(code: str) -> Optional[str]:
    """本地库里这趟车最新一天的日期（人工校正不带日期，用它兜底）"""
    code = (code or "").strip().upper()
    if not code:
        return None
    try:
        with db() as conn:
            row = conn.execute(
                "SELECT MAX(date) AS d FROM routes WHERE code=? AND raw IS NOT NULL", (code,)
            ).fetchone()
    except sqlite3.Error:
        return None
    return row["d"] if row and row["d"] else None


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
                "trainCode": (item.get("station_train_code") or "").strip() or None,
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
    manual = {"via": False, "km": False, "segKm": False, "note": None, "updatedAt": None,
              "source": None, "srcCode": None, "basedDate": None}
    ov = get_override(code) if use_override else None
    if ov:
        manual["note"] = ov.get("note")
        manual["updatedAt"] = ov.get("updatedAt")
        manual["source"] = ov.get("source") or "manual"
        manual["srcCode"] = ov.get("srcCode")
        manual["basedDate"] = ov.get("basedDate")
        if ov.get("via"):
            via = {"segs": [dict(s, src="manual") for s in ov["via"]],
                   "unknown": [], "pairs": via.get("pairs", 0),
                   "resolved": via.get("pairs", 0), "coverage": 100.0,
                   "sources": ["manual"]}
            manual["via"] = True

        # ---- 站间里程：手填的段按手填值，其余段沿用自动值重新累计 ----
        # 若同时还填了总里程，则让「没手填的段」按比例缩放到 总里程 - 手填段之和，
        # 这样既能锁定已知的精确区间，又能保住全程总量。
        seg_applied = False
        seg_ov = ov.get("segKm") or {}
        if seg_ov and len(stops) > 1:
            auto_diffs = [max(0.0, stops[i + 1]["km"] - stops[i]["km"])
                          for i in range(len(stops) - 1)]
            fixed: Dict[int, float] = {}
            for k, v in seg_ov.items():
                try:
                    idx = int(k)
                    val = float(v)
                except (TypeError, ValueError):
                    continue
                if 0 <= idx < len(auto_diffs) and val > 0:
                    fixed[idx] = val
            if fixed:
                diffs = list(auto_diffs)
                for idx, val in fixed.items():
                    diffs[idx] = val
                free = [i for i in range(len(diffs)) if i not in fixed]
                total_ov = ov.get("totalKm")
                if total_ov and free:
                    base = sum(auto_diffs[i] for i in free)
                    target = float(total_ov) - sum(fixed.values())
                    if base > 0 and target > 0:
                        ratio_free = target / base
                        for i in free:
                            diffs[i] *= ratio_free
                acc = 0.0
                stops[0]["km"] = 0.0
                for i, dv in enumerate(diffs):
                    acc += dv
                    stops[i + 1]["km"] = round(acc, 1)
                seg_applied = True
                km_mode = "manual"
                manual["km"] = True
                manual["segKm"] = True

        if not seg_applied and ov.get("totalKm"):
            base_km = stops[-1]["km"] if stops else 0.0
            if base_km and base_km > 0:
                ratio = float(ov["totalKm"]) / base_km
                for s in stops:
                    s["km"] = round(s["km"] * ratio, 1)
                km_mode = "manual"
                manual["km"] = True

    # 经由只有两种来源：这趟车自己的人工校正（已确认，写入正式库），
    # 或者读的时候按线路库临时推断（自动，未确认）。不再从别的车次「借用」。

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
    # line_stops 同时给出「经由铁路线上的全部车站」，并标出哪些是本趟车停车站
    path: List[dict] = []
    line_stops: List[dict] = []
    if with_path:
        try:
            detail = cnrail.path_stations(
                stops,
                prefer=[s.get("line") for s in (via.get("segs") or []) if s.get("line")],
                via=via.get("segs") or [],)
        except Exception:                             # noqa: BLE001
            detail = []
        path = [{"lat": p["lat"], "lon": p["lon"], "km": p["km"]} for p in detail]
        for p in detail:
            item = {"name": p["name"], "lat": p["lat"], "lon": p["lon"],
                    "km": p["km"], "stop": p["stop"]}
            if p["stop"] and p.get("idx") is not None:
                st = stops[p["idx"]]
                item.update({
                    "i": st["i"], "arr": st["arr"], "dep": st["dep"],
                    "day": st.get("day", 0), "depDay": st.get("depDay", st.get("day", 0)),
                    "stayMin": st.get("stayMin", 0),
                })
            line_stops.append(item)

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

    # ---- 中途换向会换车次号（4355 → 4358）：按站表里 station_train_code 的出现顺序收 ----
    codes: List[str] = []
    switches: List[dict] = []
    prev_code = None
    for s in stops:
        c = s.get("trainCode")
        if not c:
            continue
        if c not in codes:
            codes.append(c)
        if prev_code and c != prev_code:
            switches.append({"i": s["i"], "at": s["name"], "from": prev_code, "to": c})
        prev_code = c
    main_code = item_code(raw) or code

    return {
        "code": main_code,
        "codes": codes or [main_code],
        "codeLabel": code_label(codes or [main_code], main_code),
        "codeSwitches": switches,
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
        "viaSource": ("manual" if manual["via"] else ("auto" if via.get("segs") else "none")),
        "viaConfirmed": bool(manual["via"]),        # 进正式库（train_override）的才算已校正
        "viaCoverage": via.get("coverage", 0.0),
        "manual": manual,
        "kmMatched": f"{km_info.get('matched', 0)}/{km_info.get('segments', 0)}",
        "stops": stops,
        "path": path,
        "pathPoints": len(path),
        "lineStops": line_stops,
        "linePassCount": sum(1 for p in line_stops if not p["stop"]),
        "missingCoords": missing,
        "coverage": round((len(stops) - len(missing)) / max(len(stops), 1) * 100, 1),
        "source": "cache" if from_cache else "live",
    }


# ---------------------------------------------------------------------------
# 车次路线的 OSM 真实走向
# ---------------------------------------------------------------------------

_OSM_CACHE: Dict[Tuple[str, str], dict] = {}
_OSM_LOCK = threading.Lock()


def route_osm(code: str, date: str) -> dict:
    """
    把这条车次的折线换成「沿真实线路」的版本（带里程，缺口用直线跳过去）。
    只读本地缓存，不联网 —— 能点这个按钮时车次详情早已加载过。
    """
    key = ((code or "").strip().upper(), date or "")
    with _OSM_LOCK:
        hit = _OSM_CACHE.get(key)
    if hit:
        return hit

    try:
        payload = build_route_payload(code, date, cache_only=True)
    except Exception as exc:                                     # noqa: BLE001
        return {"ok": False, "error": f"取不到这趟车的本地缓存：{exc}"}

    pts, info = railosm.build(payload)
    if not pts:
        return {"ok": False, "error": info.get("err") or "这段路线拼不出真实走向", **info}

    out = {"ok": True, "path": pts, "code": payload.get("code"),
           "total": payload.get("totalKm"), **info}
    with _OSM_LOCK:
        _OSM_CACHE[key] = out
        if len(_OSM_CACHE) > 300:                # 只是本地进程缓存，满了丢最早的
            _OSM_CACHE.pop(next(iter(_OSM_CACHE)))
    return out


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
        return _station_detail_loose(q)
    name = st.get("key") or q
    with db() as conn:
        as_start = conn.execute(
            "SELECT COUNT(*) c FROM trains WHERE start_sta = ?", (name,)).fetchone()["c"]
        as_end = conn.execute(
            "SELECT COUNT(*) c FROM trains WHERE end_sta = ?", (name,)).fetchone()["c"]
    # 坐标取「当前生效值」：人工补录的优先，其次档案 / OSM
    info = stations.coord_info(st.get("name") or name)
    lat = info.get("lat") if info.get("lat") is not None else st.get("lat")
    lon = info.get("lon") if info.get("lon") is not None else st.get("lon")
    out = {
        "found": True,
        **{k: st.get(k) for k in ("name", "key", "en", "type", "loc", "province", "city",
                                  "district", "opened", "bureau", "tele", "services",
                                  "wikidata", "coordSrc", "lines", "custom",
                                  "source", "alias", "note")},
        "lat": lat, "lon": lon,
        "coordSource": info.get("source") or ("base" if lat is not None else "none"),
        "trainsFrom": as_start,
        "trainsTo": as_end,
    }
    # 自建 / 线路补全的站（唐海南、曹妃甸港这批）档案里没有车次数据，
    # 和兜底档案一个口径，把当天经停的车次也列上
    if st.get("custom"):
        day, codes = _trains_via(name)
        out["viaDate"] = day
        out["trainsVia"] = codes
    # 12306 那份站资料只用来补：缺电报码时补上，顺带给个街道地址
    extra = railgeom.station_extra(out.get("name") or name) or {}
    if not out.get("tele") and extra.get("code"):
        out["tele"] = extra["code"]
    if extra.get("addr"):
        out["addr"] = extra["addr"]
    return out


def _trains_via(name: str):
    """当天经停这个站的车次：只扫最新一天的路由缓存（约 1 万行，0.2 秒）

    全表扫要 4 秒，别那么干 —— 车站详情是一个点击就到的地方。
    """
    with db() as conn:
        day = conn.execute("SELECT MAX(date) d FROM routes").fetchone()["d"]
        if not day:
            return None, []
        rows = conn.execute(
            "SELECT DISTINCT code FROM routes WHERE date = ? AND raw LIKE ? ORDER BY code",
            (day, f'%"station_name": "{name}"%')).fetchall()
    return day, [r["code"] for r in rows]


def _station_detail_loose(q: str) -> dict:
    """档案里没有的站：用 12306 站表 + OSM 站表兜一个最小档案

    唐海南 / 曹妃甸港 就是这种情况：12306 有电报码（IEP / PGP）、经停缓存里也有它
    （C248、C133 …），但它所属的唐曹线 / 曹妃甸港线还没收进线路库（源数据里站序是空的），
    cnrail 自然没有档案 —— 联想下拉能搜到、点进去却说「没有」，这就很不合理。
    这里把能拿到的都摆出来：站名、电报码、城市、地址、坐标、当天经停的车次。
    """
    name = stations.canonical(q)
    code = stations.telecode(name)
    raw = (stations.by_code.get(code) or {}) if code else {}
    extra = railgeom.station_extra(name) or {}
    if not raw and not extra:
        return {"found": False, "query": q}
    nm = _norm(raw.get("station_name") or extra.get("name") or name)
    info = stations.coord_info(nm)
    lat, lon = info.get("lat"), info.get("lon")
    if lat is None and extra.get("lat") is not None:
        lat, lon = extra.get("lat"), extra.get("lon")
    with db() as conn:
        as_start = conn.execute(
            "SELECT COUNT(*) c FROM trains WHERE start_sta = ?", (nm,)).fetchone()["c"]
        as_end = conn.execute(
            "SELECT COUNT(*) c FROM trains WHERE end_sta = ?", (nm,)).fetchone()["c"]
    day, codes = _trains_via(nm)
    return {
        "found": True,
        "name": nm, "key": nm,
        "city": raw.get("city") or extra.get("city") or None,
        "tele": raw.get("station_code") or extra.get("code") or None,
        "addr": extra.get("addr") or None,
        "lat": lat, "lon": lon,
        "coordSource": info.get("source") or ("base" if lat is not None else "none"),
        "lines": cnrail.lines_of(nm),
        "services": [], "custom": False, "source": "12306", "archive": False,
        "note": "线路库里没有这个站的档案（它所属的线路还没收进线路库），"
                "站名、电报码、坐标来自 12306 站表",
        "trainsFrom": as_start,
        "trainsTo": as_end,
        "viaDate": day,
        "trainsVia": codes,
    }


init_db()
try:
    _apply_catalog()                 # 线路资料库（参数 / 介绍 / 里程锚点）
except Exception:                    # 库坏了也不能让服务起不来
    pass
try:
    _apply_custom()                  # 用户新增的线路 / 车站先合进 cnrail
except Exception:                    # 库坏了也不能让服务起不来
    pass
try:
    _apply_line_overrides()          # 人工线路里程在进程启动时就套进 cnrail
except Exception:                    # 库坏了也不能让服务起不来
    pass


# ---------------------------------------------------------------------------
# 启动时的一次性补齐
# ---------------------------------------------------------------------------

def _startup_backfill() -> None:
    try:
        k = backfill_line_stations()
        if k:
            print(f"[railcore] 已把 {k} 个沿线车站补进车站库（带 OSM / 人工坐标）")
    except Exception:                                # noqa: BLE001
        pass


threading.Thread(target=_startup_backfill, daemon=True).start()
