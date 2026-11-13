# -*- coding: utf-8 -*-
"""
osmedit.py — OSM 走向编辑器后端

提供「在地图上画 / 改一条线路的 OSM 走向」所需的三个能力：
  1. 列出可编辑的线路（库里已有几何的 + **整理台已确认线路（红名、排最前）** + OSM 线名）；
  2. 载入某条线的几何，把动辄上万个点的原始 OSM 碎段**抽稀成可拖拽的折线**
     （Ramer–Douglas–Peucker，按里程容差），方便在地图上直接编辑；
  3. 保存编辑结果。

存储策略（双轨，避免误伤）：
  - 人工编辑版存在 rail.db 的 `line_geom_edit`，**不破坏**原 line_geom；
  - 只有用户点「应用到线路库」（apply=1）时才写回 line_geom，让它真正生效于
    贴线 / 列车地图 / 车站·线路页。
  - **整理台已确认线路**另外走 `linedesk.db / ld_geom`（按 line_id 绑定，也就是
    `confirmed.geom()` → `linedesk.merged_geom()` 真正读的那份）。全程不动
    `ld_line` 的 status / line_ok / stop_ok / geom_src / geom_name，
    所以保存完它**仍然是整理台已确认线路**，主站白名单与确认先后都不变。

坐标：库里是 WGS-84；parts_gz / spine_gz 与前端约定一律是 [lon, lat]。
"""
from __future__ import annotations

import gzip
import json
import math
import os
import re
import sqlite3
import time
from typing import List, Optional, Tuple

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RAIL_DB = os.path.join(BASE_DIR, "data", "rail.db")
LING_DB = os.path.join(BASE_DIR, "data", "linedesk.db")

import railgeom  # noqa: E402  reuse chain_parts / geom

# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------

_EARTH_M = 6371008.8


def _gz(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode("utf-8"), 6)


def _ungz(blob) -> Optional[list]:
    if not blob:
        return None
    return json.loads(gzip.decompress(blob).decode("utf-8"))


def _haversine_km(a: List[float], b: List[float]) -> float:
    """a / b = [lon, lat]，返回公里"""
    r = math.radians
    lon1, lat1, lon2, lat2 = a[0], a[1], b[0], b[1]
    dlon, dlat = r(lon2 - lon1), r(lat2 - lat1)
    h = (math.sin(dlat / 2) ** 2
         + math.cos(r(lat1)) * math.cos(r(lat2)) * math.sin(dlon / 2) ** 2)
    return 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(h)))


def _poly_km(pts: List[List[float]]) -> float:
    return sum(_haversine_km(pts[i - 1], pts[i]) for i in range(1, len(pts)))


# --------------------------------------------------------------------------
# RDP 抽稀（点到线段距离用等距投影，单位米）
# --------------------------------------------------------------------------

def _seg_dist_m(p, a, b) -> float:
    lat0 = math.radians(a[1])
    sx = 111320 * math.cos(lat0)
    sy = 110574

    def xy(o):
        return ((o[0] - a[0]) * sx, (o[1] - a[1]) * sy)

    px, py = xy(p)
    bx, by = xy(b)
    dx, dy = bx, by
    l2 = dx * dx + dy * dy
    if l2 == 0:
        return math.hypot(px - bx, py - by)
    t = max(0.0, min(1.0, (px * dx + py * dy) / l2))
    cx, cy = t * dx, t * dy
    return math.hypot(px - cx, py - cy)


def rdp(points: List[List[float]], eps_m: float) -> List[List[float]]:
    """Ramer–Douglas–Peucker，eps_m = 容差（米）"""
    if len(points) < 3:
        return list(points)
    dmax, idx = 0.0, 0
    for i in range(1, len(points) - 1):
        d = _seg_dist_m(points[i], points[0], points[-1])
        if d > dmax:
            dmax, idx = d, i
    if dmax > eps_m:
        left = rdp(points[:idx + 1], eps_m)
        right = rdp(points[idx:], eps_m)
        return left[:-1] + right
    return [points[0], points[-1]]


# --------------------------------------------------------------------------
# 连线：把多段 [lon, lat] 串成一条连续折线
# --------------------------------------------------------------------------

def _chain_to(parts: List[list], gap_m: float = 3000.0,
              max_pts: int = 1400, max_segs: int = 800) -> List[List[float]]:
    """parts = [[ [lon,lat], … ], …] → 单条 [ [lon,lat], … ] 连续折线

    railgeom.chain_parts 是 O(段数²)：每接一段都要扫一遍剩下的段。原始 OSM 碎段
    动辄几千段（陇海线的 line_geom 有 5000+ 段 → 1400 万次距离计算 / 60s）。
    两道闸门把成本压住（形状误差对「可拖拽骨架」无感）：
      1. 超过 max_segs 段时只留最长的 max_segs 段（主干走向，碎渣丢掉）；
      2. 总点数超过 max_pts 就按容差 RDP 抽稀后再串。
    """
    segs = [p for p in (parts or []) if p and len(p) >= 2]
    if not segs:
        return []
    if len(segs) > max_segs:
        segs = sorted(segs, key=len, reverse=True)[:max_segs]
    total = sum(len(s) for s in segs)
    eps = 100.0
    while total > max_pts and eps <= 3200:
        segs = [rdp(s, eps) for s in segs]
        total = sum(len(s) for s in segs)
        eps *= 2
    # railgeom.chain_parts 入参是 OSM 原样 [lon, lat]，返回 (lat, lon) 链
    chain = railgeom.chain_parts(segs, gap_m=gap_m)
    return [[lon, lat] for lat, lon in chain]


def _cand_ldspine(name: str) -> List[List[float]]:
    """确认线路 `ld_geom.spine_gz` → 已串好的连续主轴 [[lon,lat],…]。

    `parts_gz` 是原始多段，直接 `chain_parts` 串经常只连上一小截（京九线
    14 段 6220 点只串出 843 km，主轴却是 3063 km）。编辑器要的是一条完整
    可拖拽折线，所以用主轴；`linedesk._norm_parts` 顺手纠正历史 (lat,lon) 反序。
    """
    try:
        c = sqlite3.connect(LING_DB)
        cur = c.cursor()
        cur.execute("SELECT spine_gz FROM ld_geom WHERE name=?", (name,))
        row = cur.fetchone()
        c.close()
        if not row or not row[0]:
            return []
        import linedesk
        segs = linedesk._norm_parts(_ungz(row[0]), single=True)
        return [list(p) for p in segs[0]] if segs else []
    except Exception:  # noqa: BLE001
        return []


def _cand_ldgeom(name: str):
    """整理台确认线路的权威走向（linedesk.db / ld_geom）→ (库里总里程, 链) 或 None"""
    try:
        c = sqlite3.connect(LING_DB)
        cur = c.cursor()
        cur.execute("SELECT parts_gz, spine_gz, total_km FROM ld_geom WHERE name=?", (name,))
        row = cur.fetchone()
        c.close()
        if not row:
            return None
        ref_km = float(row[2] or 0)
        raw = _ungz(row[0]) if row[0] else None
        if not raw and row[1]:
            sp = _ungz(row[1])
            raw = [sp] if sp else None
        if not raw:
            return None
        return ref_km, _chain_to(raw, gap_m=12000.0)
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------
# 数据库
# --------------------------------------------------------------------------

def _rail() -> sqlite3.Connection:
    c = sqlite3.connect(RAIL_DB)
    c.execute(
        """CREATE TABLE IF NOT EXISTS line_geom_edit(
            name TEXT PRIMARY KEY,
            parts_gz BLOB,
            spine_gz BLOB,
            total_km REAL,
            n_part INTEGER,
            n_pt INTEGER,
            src TEXT,
            note TEXT,
            created_at REAL,
            updated_at REAL
        )"""
    )
    return c


def _stops(name: str) -> List[dict]:
    """线路基本站台（来自 line_base_stop），用于地图打点参考"""
    try:
        c = sqlite3.connect(RAIL_DB)
        cur = c.cursor()
        cur.execute(
            """SELECT s.name, s.lon, s.lat, s.km
               FROM line_base_stop s JOIN line_base b ON s.base_id=b.id
               WHERE b.name=? ORDER BY s.seq""",
            (name,),
        )
        rows = cur.fetchall()
        c.close()
        out = []
        for nm, lon, lat, km in rows:
            if lon is None or lat is None:
                continue
            out.append({"name": nm, "lon": lon, "lat": lat, "km": km})
        if out:
            return out
    except Exception:  # noqa: BLE001
        pass
    # 退回到整理台 ld_geom 里的站点（linedesk.db）
    try:
        c = sqlite3.connect(LING_DB)
        cur = c.cursor()
        cur.execute(
            "SELECT stops_json FROM ld_geom WHERE name=? LIMIT 1", (name,)
        )
        row = cur.fetchone()
        c.close()
        if row and row[0]:
            import geoimport  # local import to avoid cycle at module load
            raw = geoimport._decode_stops(row[0]) if hasattr(geoimport, "_decode_stops") else json.loads(row[0])
            return [{"name": s.get("name"), "lon": s.get("lon"),
                     "lat": s.get("lat"), "km": s.get("km")}
                    for s in (raw or []) if s.get("lon") is not None]
    except Exception:  # noqa: BLE001
        pass
    return []


# --------------------------------------------------------------------------
# 对外接口
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# 线路索引：**只收「真有走向」的线**（2026-11-09 用户：只关心走向、不关心站点）
# 以前把 ld_line 五千多条整理台草稿整表灌进列表、再截前 60 条，结果有走向的线
# 反而被没走向的草稿挤掉；OSM 名字也只取了 search() 的前 60 条。现在全量收齐，
# 支持 offset 翻页，任何一条有几何的线都能搜到 / 翻到。
# --------------------------------------------------------------------------
_INDEX = {"ts": 0.0, "rows": [], "merged": [], "cfp": None}
_INDEX_TTL = 20.0
# 库里既有几条测试残留几何（L0009_京沪线 / test_… 7399 km）。不删数据，
# 只是别让它们顶在列表第一屏，仍可搜到、仍可打开。
_TESTY_NAME = re.compile(r"^(?:test[_-]|测试|L\d+_|\(在建未命名)", re.I)


def _index_invalidate() -> None:
    """写草稿 / 建线 / 删线后立刻让列表失效"""
    _INDEX["ts"] = 0.0
    _INDEX["rows"] = []
    _INDEX["merged"] = []
    _INDEX["cfp"] = None


def _confirmed_info(name: str):
    """这条线是不是整理台「完成全部确认」的线路 → 返回 ld_line 行，否则 None。

    只认 confirmed.get()（status + line_ok/stop_ok + ≥2 站 + 真有走向），
    草稿 / 备份版本 / 来源版本一律不算。编辑已确认线路时用它判定写库通道。
    """
    try:
        import confirmed
        return confirmed.get(name)
    except Exception:  # noqa: BLE001  整理台库被占用 / 旧表结构
        return None


def _confirmed_rows() -> list:
    """整理台全量已确认线路 → 列表行（每次现算，整理台刚确认完立刻出现）。

    这些线即便自己没有 ld_geom / line_geom（③ 的 osm 分支只记 geom_name，
    走向是运行时借来的）也要能选、能改。`loadName` 一律用**规范名**：
    确认线路走 load_geom() 的专属通道，保存也写回自己名下，不再借别人那份。
    """
    try:
        import confirmed
        rows = confirmed.recent()
    except Exception:  # noqa: BLE001
        return []
    return [{
        "name": r["name"], "loadName": r["name"],
        "km": round(float(r["totalKm"]), 1) if r.get("totalKm") else None,
        "src": ["confirmed"], "hasEdit": False,
        "alias": False, "aliasOf": "", "confirmed": True,
        "ldId": r["ldId"], "geomName": r.get("geomName") or "",
        "geomSrc": r.get("geomSrc") or "",
        "confirmedAt": r.get("confirmedAt") or "",
        "nStop": r.get("n") or 0,
    } for r in rows]


def _geom_index() -> list:
    names: dict = {}                        # 几何线名 -> {km, src:set}

    def add(nm, src, km=None):
        nm = str(nm or "").strip()
        if not nm:
            return
        e = names.setdefault(nm, {"km": None, "src": set()})
        e["src"].add(src)
        if km and not e["km"]:
            try:
                e["km"] = round(float(km), 1)
            except (TypeError, ValueError):
                pass

    _rail().close()                        # 确保 line_geom_edit 表存在
    c = sqlite3.connect(RAIL_DB)
    try:
        for nm, km in c.execute(
                "SELECT name,total_km FROM line_geom"
                " WHERE parts_gz IS NOT NULL OR spine_gz IS NOT NULL"):
            add(nm, "line_geom", km)
        for (nm,) in c.execute(
                "SELECT DISTINCT name FROM line_route"
                " WHERE parts_gz IS NOT NULL OR spine_gz IS NOT NULL"):
            add(nm, "route")
        for (nm,) in c.execute("SELECT name FROM line_geom_edit"
                               " WHERE parts_gz IS NOT NULL"):
            add(nm, "edit")
    except sqlite3.Error:
        pass
    finally:
        c.close()
    d = sqlite3.connect(LING_DB)
    try:
        for nm, km in d.execute(
                "SELECT name,total_km FROM ld_geom"
                " WHERE parts_gz IS NOT NULL OR spine_gz IS NOT NULL"):
            add(nm, "desk", km)
    except sqlite3.Error:
        pass
    finally:
        d.close()
    # OSM 线名要给足 limit：取前 60 条会把 200 多条 OSM 独有的线漏掉
    try:
        for it in railgeom.search("", limit=1000000):
            add(it.get("name"), "osm", it.get("km"))
    except Exception:  # noqa: BLE001
        pass

    rows = [{
        "name": nm, "loadName": nm, "km": e["km"],
        "src": sorted(e["src"]), "hasEdit": "edit" in e["src"],
        "alias": False, "aliasOf": "",
    } for nm, e in names.items()]

    # 「借别人走向」的线（济南线 ← 京沪四线）：自己没有几何，但整理台/基本线路
    # 记了 geom_name。也列出来，点开载入那份几何，别让用户以为线丢了。
    aliases = set()
    r = sqlite3.connect(RAIL_DB)
    try:
        aliases |= set(r.execute(
            "SELECT name,geom_name FROM line_base"
            " WHERE geom_name IS NOT NULL AND geom_name<>'' AND geom_name<>name"))
    except sqlite3.Error:
        pass
    finally:
        r.close()
    d = sqlite3.connect(LING_DB)
    try:
        aliases |= set(d.execute(
            "SELECT name,geom_name FROM ld_line"
            " WHERE geom_name IS NOT NULL AND geom_name<>'' AND geom_name<>name"))
    except sqlite3.Error:
        pass
    finally:
        d.close()
    have = {x["name"] for x in rows}
    for nm, gn in sorted(aliases):
        if nm in have or gn not in names:
            continue
        have.add(nm)
        rows.append({
            "name": nm, "loadName": gn, "km": names[gn]["km"],
            "src": ["alias"], "hasEdit": False, "alias": True, "aliasOf": gn,
        })
    rows.sort(key=lambda x: (bool(_TESTY_NAME.search(x["name"])),
                             -(x["km"] or 0), x["name"]))
    return rows


def _geom_source_name(name: str) -> str:
    """线自己没几何，但记了「走向借自谁」→ 返回那个几何名"""
    for db, sql in (
        (RAIL_DB, "SELECT geom_name FROM line_base WHERE name=? AND geom_name<>''"),
        (LING_DB, "SELECT geom_name FROM ld_line WHERE name=? AND geom_name<>''"),
    ):
        try:
            c = sqlite3.connect(db)
            row = c.execute(sql, (name,)).fetchone()
            c.close()
            if row and row[0] and row[0] != name:
                return str(row[0])
        except sqlite3.Error:
            pass
    return ""


def _merge_confirmed(base: list, cf: list) -> list:
    """把「整理台已确认线路」叠到几何索引上：打标、改载入名、排到最前。

    基础索引里可能已有同名行（多半是 `alias` 借走向 / `desk` / `line_geom`），
    这时**复用**它的 km / src，只覆盖确认相关字段；没有同名行的确认线路
    （③ 的 osm 分支，自己没有任何几何）就整行新增，保证一定能选到、改到。
    """
    by = {x["name"]: x for x in base}
    rows = []
    for x in cf:
        b = by.get(x["name"])
        if b:
            m = dict(b)
            # 确认线路现在走自己的 ld_geom，不再借来源名那份几何，
            # 基础索引里的 alias 标签留着会误导（显示成「借走向 已确认」）。
            m["src"] = sorted((set(b["src"]) | {"confirmed"}) - {"alias"})
            m["loadName"] = x["name"]        # 按规范名走确认线路专属通道
            m["alias"], m["aliasOf"] = False, ""
            m["confirmed"] = True
            m["ldId"] = x["ldId"]
            m["geomName"] = x["geomName"]
            m["geomSrc"] = x["geomSrc"]
            m["confirmedAt"] = x["confirmedAt"]
            m["nStop"] = x["nStop"]
            if not m.get("km"):
                m["km"] = x["km"]
            rows.append(m)
        else:
            rows.append(x)
    cf_names = {x["name"] for x in cf}
    rows.extend(b for b in base if b["name"] not in cf_names)
    # 测试残留沉底 → 整理台已确认优先（红名一眼可见）→ 里程 → 名称
    rows.sort(key=lambda x: (bool(_TESTY_NAME.search(x["name"])),
                             not bool(x.get("confirmed")),
                             -(float(x["km"] or 0)), x["name"]))
    return rows


def list_lines(q: str = "", limit: int = 60, offset: int = 0) -> dict:
    """只列有走向的线路 + 整理台全量已确认线路，支持 offset 翻页。

    count=筛选后总数，total=全库总数。确认线路部分每次现算（整理台 ③ 刚
    确认完立刻进列表），几何索引 20 秒缓存；确认签名变了才重新合并。
    """
    if _INDEX["rows"] and time.time() - _INDEX["ts"] < _INDEX_TTL:
        base = _INDEX["rows"]
    else:
        base = _geom_index()
        _INDEX["rows"] = base
        _INDEX["ts"] = time.time()
        _INDEX["merged"] = []
        _INDEX["cfp"] = None
    cf = _confirmed_rows()
    cfp = (len(cf), cf[0]["name"] if cf else "",
           cf[0]["confirmedAt"] if cf else "")
    if not _INDEX["merged"] or _INDEX["cfp"] != cfp:
        _INDEX["merged"] = _merge_confirmed(base, cf)
        _INDEX["cfp"] = cfp
    rows = _INDEX["merged"]
    kw = re.sub(r"\s+", "", str(q or ""))
    hit = [r for r in rows if not kw or kw in r["name"]
           or kw in r["loadName"] or kw in (r.get("geomName") or "")]
    try:
        limit = max(1, min(int(limit or 60), 1000))
        offset = max(0, int(offset or 0))
    except (TypeError, ValueError):
        limit, offset = 60, 0
    return {
        "items": hit[offset:offset + limit],
        "count": len(hit), "total": len(rows),
        "confirmed": len(cf),          # 整理台已确认线路条数（前端红名提示用）
        "offset": offset, "hasMore": offset + limit < len(hit),
    }


def _as_segs(raw) -> List[List[List[float]]]:
    """几何统一成 [[[lon,lat],…],…]，顺手纠正历史 (lat,lon) 反序。

    `_ungz` 出来只有两种：多段 [[[x,y],…],…] 或扁平单链 [[x,y],…]，靠
    首个元素的类型区分。**别再把多段硬串成一条折线**：段与段之间本就
    不相连，串起来会补出几十甚至几百公里的假直线，形状完全不是同一条线。
    """
    if not isinstance(raw, list) or not raw:
        return []
    if raw[0] and isinstance(raw[0][0], (int, float)):
        raw = [raw]                       # 扁平单链 → 一段
    try:
        import linedesk
        segs = linedesk._norm_parts(raw)
    except Exception:                     # noqa: BLE001
        segs = []
    if segs:
        return segs
    out = []
    for seg in raw:
        if not isinstance(seg, list) or len(seg) < 2:
            continue
        line = []
        for p in seg:
            if isinstance(p, (list, tuple)) and len(p) >= 2:
                try:
                    line.append([float(p[0]), float(p[1])])
                except (TypeError, ValueError):
                    pass
        if len(line) >= 2:
            out.append(line)
    return out


def _cap_jumps(orig, thin, max_km: float = 25.0):
    """抽稀后若相邻保留点间出现超长直线，从原始段里补回中间点。

    RDP 按「点到弦的垂距」挑点，一条长而缓的弯可能被砍成首尾两点，
    视觉上就成了几十公里的假直线；这里按弧长把中间点补回来。
    """
    if len(thin) < 2 or not orig:
        return thin
    idx, j = [], 0
    for p in thin:
        while j < len(orig) - 1 and orig[j] != p:
            j += 1
        idx.append(j)
        j += 1
    out = [thin[0]]
    for k in range(1, len(thin)):
        gap = _haversine_km(thin[k - 1], thin[k])
        span = idx[k] - idx[k - 1]
        if gap > max_km and span > 1:
            n = min(int(gap / max_km) + 1, span - 1)
            for t in range(1, n + 1):
                pos = idx[k - 1] + int(round(span * t / (n + 1)))
                if idx[k - 1] < pos < idx[k]:
                    out.append(orig[pos])
        out.append(thin[k])
    return out


def _thin_segs(segs: List[List[List[float]]], target: int) -> List[List[List[float]]]:
    """抽稀到约 target 个可编辑点，但**保留段结构**：每段各自 RDP，不跨段连线。"""
    if not segs:
        return []
    total = sum(len(s) for s in segs)
    target = max(int(target or 200), 2 * len(segs))   # 每段至少留首尾两点
    target = min(target, 3000)
    if total <= target:
        return segs
    out = []
    for seg in segs:
        budget = max(2, int(round(target * len(seg) / total)))
        # 别让抽稀造出几十公里的假直线：段越长留点越多（每 ~25 km 至少一点）
        budget = max(budget, min(80, int(_poly_km(seg) / 25.0) + 2))
        if len(seg) <= budget:
            out.append(seg)
            continue
        eps, line = 15.0, seg
        while len(line) > budget and eps <= 20000:
            line = rdp(seg, eps)
            eps *= 1.8
        if len(line) > budget:            # 仍超预算：等距抽，首尾保留
            step = len(line) / budget
            line = [line[int(i * step)] for i in range(budget)]
            line[-1] = seg[-1]
        out.append(_cap_jumps(seg, line))
    return out


def load_geom(name: str, simplify_n: int = 200, _depth: int = 0) -> Optional[dict]:
    """载入一条线的几何，抽稀成可拖拽的**多段**折线。

    与主站 `/api/line/osm` 共用 `lineosm.geom()`：载入的就是主站正在显示的
    那份 parts。不再读 `ld_geom.spine_gz`（抽稀 + 补断口主轴，画地图会失真），
    也不再 `_chain_to()` 把多段硬串成一条 —— 那正是「调用现有线路出来很奇怪」
    的根因（滨绥线 902 km 只剩 13 km、沪昆线冒出 274 km 直线）。
    优先级：人工编辑草稿 > 主站几何 > 借走向来源名兜底一层。
    """
    if not name:
        return None
    ci = _confirmed_info(name)
    segs: List[List[List[float]]] = []
    now_src = ""

    # 1) 人工编辑草稿（新版可能是多段，老版是单段）
    c = sqlite3.connect(RAIL_DB)
    cur = c.cursor()
    cur.execute("SELECT parts_gz FROM line_geom_edit WHERE name=?", (name,))
    row = cur.fetchone()
    c.close()
    if row and row[0]:
        segs = _as_segs(_ungz(row[0]))
        if segs:
            now_src = "edit"

    # 2) 主站同一份几何：确认线路走 confirmed.geom()，其余按主站解析顺序
    if not segs:
        try:
            import lineosm
            g = lineosm.geom(name, confirmed_only=bool(ci))
            if g and g.get("parts"):
                segs = _as_segs(g["parts"])
                if segs:
                    now_src = "confirmed" if ci else "main"
        except Exception:                 # noqa: BLE001
            segs = []

    # 3) 借别人走向的线（济南线 ← 京沪四线）：只回退一层，避免 A→B→A
    if not segs:
        if _depth < 1:
            alt = _geom_source_name(name)
            if alt and alt != name:
                g = load_geom(alt, simplify_n, _depth + 1)
                if g:
                    g["name"] = name
                    if ci:
                        # 确认线路即使走到来源名兜底，也保持确认身份与规范写入目标
                        g["loadName"] = name
                        g["confirmed"] = True
                        g["ldId"] = int(ci["id"])
                        g["geomName"] = str(ci.get("geom_name") or alt)
                        g["geomSrc"] = str(ci.get("geom_src") or "")
                        g["keepsConfirmed"] = True
                    else:
                        g["loadName"] = alt
                    return g
        return None

    if not segs:
        return None

    segs = _thin_segs(segs, simplify_n)
    km = sum(_poly_km(s) for s in segs)     # 各段累加，不含跨段直线
    stops = _stops(name)
    return {
        "name": name,
        "loadName": name,
        "parts": segs,
        "crs": "WGS84",
        "totalKm": round(km, 1),
        "hasEdit": now_src == "edit",
        "source": now_src,
        "nPt": sum(len(s) for s in segs),
        "nPart": len(segs),
        "stops": stops,
        # 整理台已确认线路：前端据此标红、用规范名保存、提示「保存后仍是确认线路」
        "confirmed": bool(ci),
        "keepsConfirmed": bool(ci),
        "ldId": int(ci["id"]) if ci else 0,
        "geomName": str(ci.get("geom_name") or "") if ci else "",
        "geomSrc": str(ci.get("geom_src") or "") if ci else "",
    }


def save_geom(name: str, parts: list, total_km=None, src: str = "",
              note: str = "", apply: bool = False, target: str = "",
              confirmed_line: bool = False) -> dict:
    """保存编辑结果。

    parts 可以是单条折线 [[lon,lat],…] 或多段 [[…],…]；**多段原样保留**
    写 parts_gz，扁平链只作 spine_gz 兜底（merged_geom 优先读 parts_gz）。
    apply=True 时写回 line_geom；
    **整理台已确认线路**另外写回 linedesk.db / ld_geom（按 line_id 绑定），
    但不碰 ld_line 的任何确认字段，保存完仍是确认线路。
    """
    if not name:
        raise ValueError("缺少线路名")
    if not parts:
        raise ValueError("几何为空")

    # 规整成多段：单条折线 [[lon,lat],…] 自动包成一段，多段原样保留。
    # 绝不把各段首尾压成一条 chain —— 段间本就不相连，压平会在保存后
    # 冒出几十/几百公里的假直线，主站地图立刻变形。
    segs = _as_segs(parts)
    chain = [p for seg in segs for p in seg]     # 扁平链只作 spine 兜底
    if len(chain) < 2:
        raise ValueError("有效点少于 2 个")

    km = (sum(_poly_km(s) for s in segs)
          if total_km in (None, "") else float(total_km))
    parts_gz = _gz(segs)
    spine_gz = _gz(chain)
    n_part = len(segs)
    ts = time.time()

    c = _rail()
    c.execute(
        """INSERT INTO line_geom_edit(name, parts_gz, spine_gz, total_km,
                                      n_part, n_pt, src, note, created_at, updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(name) DO UPDATE SET
             parts_gz=excluded.parts_gz, spine_gz=excluded.spine_gz,
             total_km=excluded.total_km, n_part=excluded.n_part,
             n_pt=excluded.n_pt, src=excluded.src, note=excluded.note,
             updated_at=excluded.updated_at""",
        (name, parts_gz, spine_gz, km, n_part, len(chain),
         src or "osmedit", note or "", ts, ts),
    )
    c.commit()
    c.close()
    _index_invalidate()                  # 新草稿要立刻出现在列表里

    ci = _confirmed_info(name)
    is_conf = bool(ci)
    applied = {"ld": False, "lib": False, "missing": True}
    if apply:
        if is_conf:
            # 确认线路的权威走向在 ld_geom（confirmed.geom → merged_geom(ld_id)）。
            # 不写它，只更新 rail.db / line_geom 的话，主站确认通道根本读不到，
            # ③ 的 osm 分支那三条更是连 ld_geom 记录都没有。写回后确认状态不变。
            applied = _apply_confirmed(int(ci["id"]), name, parts_gz, spine_gz,
                                       km, len(chain), n_part)
            lib = _apply_to_library(name, parts_gz, spine_gz, km,
                                    len(chain), n_part)
            applied["lib"] = bool(lib.get("lib"))
            applied["missing"] = not (applied["ld"] or applied["lib"])
            _index_invalidate()
            try:
                import confirmed
                confirmed.invalidate()       # 确认清单的 nGeomPt 立刻跟上
            except Exception:  # noqa: BLE001
                pass
        else:
            # 借走向的线编辑的是「几何名」那份，应用也写回那份，别凭空造一份假几何
            applied = _apply_to_library((target or name), parts_gz, spine_gz,
                                        km, len(chain), n_part)

    return {
        "ok": True,
        "name": name,
        "target": name if is_conf else (target or name),
        "confirmed": is_conf,
        "keptConfirmed": is_conf,           # 没动 ld_line 任何确认字段
        "requestedConfirmed": bool(confirmed_line),
        "ldId": int(ci["id"]) if ci else 0,
        "nPt": len(chain),
        "nPart": n_part,
        "totalKm": round(km, 1),
        "saved": True,
        "applied": applied,
        "appliedAny": bool(applied.get("ld") or applied.get("lib")),
        "missing": bool(applied.get("missing")),
    }


def _apply_confirmed(ld_id: int, name: str, parts_gz: bytes,
                     spine_gz: bytes, km: float, n_pt: int,
                     n_part: int = 1) -> dict:
    """把编辑后的走向写回整理台 `ld_geom`，按 `line_id` 绑定。

    `linedesk.merged_geom(ld_id)` 是 `ORDER BY created_at DESC LIMIT 1`，所以：
      - 已有 ld_geom → 更新「会被命中的那一行」+ 规范名同名行（后者给
        `_cand_ldgeom()` / `_raw_parts()` 按线名读，必须一致）；
      - 一条都没有（③ 的 osm 分支）→ INSERT 一份 name=规范名、line_id=ld_id
        的记录，等于把借来的走向固化到自己名下，不再依赖来源名解析。
    `created_at` 保证比同 line_id 已有记录都新，merged_geom 一定命中新几何。

    **全程只碰 ld_geom**，不写 ld_line / ld_stop / ld_alias，
    所以 status='confirmed' / line_ok / stop_ok / geom_src / geom_name 原样保留。
    """
    res = {"ld": False, "lib": False, "missing": True}
    try:
        c = sqlite3.connect(LING_DB)
        cur = c.cursor()
        hit = cur.execute(
            "SELECT name FROM ld_geom WHERE line_id=?"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (int(ld_id),)).fetchone()
        names = {hit[0]} if hit else set()
        names |= {r[0] for r in cur.execute(
            "SELECT name FROM ld_geom WHERE name=?", (name,))}
        if names:
            qs = ",".join("?" for _ in names)
            cur.execute(
                "UPDATE ld_geom SET parts_gz=?, spine_gz=?, total_km=?,"
                " n_part=?, n_pt=? WHERE name IN (%s)" % qs,
                (parts_gz, spine_gz, km, n_part, n_pt, *sorted(names)))
        else:
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            mx = cur.execute(
                "SELECT MAX(created_at) FROM ld_geom WHERE line_id=?",
                (int(ld_id),)).fetchone()[0]
            if mx and str(mx) >= now:
                from datetime import datetime, timedelta
                try:
                    now = (datetime.strptime(str(mx), "%Y-%m-%d %H:%M:%S")
                           + timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
                except ValueError:
                    pass
            cur.execute(
                "INSERT INTO ld_geom(name,line_id,filename,total_km,n_part,n_pt,"
                " props,parts_gz,spine_gz,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (name, int(ld_id), "OSM 走向编辑器", km, n_part, n_pt,
                 '{"source":"osmedit"}', parts_gz, spine_gz, now))
        c.commit()
        c.close()
        res["ld"] = True
        res["missing"] = False
    except Exception:  # noqa: BLE001  表被占用 / 老库缺 parts_gz 列
        res["ld"] = False
    return res


def _apply_to_library(name: str, parts_gz: bytes, spine_gz: bytes,
                      km: float, n_pt: int, n_part: int = 1) -> dict:
    """写回库，让贴线 / 列车地图 / 车站·线路页真正采用这份走向。

    双写：
      - ld_geom（linedesk.db）：确认线路的权威几何，confirmed.geom / 地图都读它，
        写了才会让「车站·线路」页和列车地图的真实走向真正变；
      - line_geom（rail.db）：非确认线 / 来源回退时的几何。
    两处都只覆盖已有同名记录，库里没有则不动（避免凭空造几何）。
    返回 {ld, lib} 标记各自是否写入。
    """
    res = {"ld": False, "lib": False, "missing": True}
    # 1) 整理台 ld_geom
    try:
        c = sqlite3.connect(LING_DB)
        cur = c.cursor()
        cur.execute("SELECT name FROM ld_geom WHERE name=?", (name,))
        if cur.fetchone():
            res["missing"] = False
            cur.execute(
                """UPDATE ld_geom SET parts_gz=?, spine_gz=?, total_km=?,
                                      n_part=?, n_pt=?
                   WHERE name=?""",
                (parts_gz, spine_gz, km, n_part, n_pt, name),
            )
            c.commit()
            res["ld"] = True
        c.close()
    except Exception:  # noqa: BLE001
        pass
    # 2) 线路库 line_geom
    c = sqlite3.connect(RAIL_DB)
    cur = c.cursor()
    cur.execute("SELECT name FROM line_geom WHERE name=?", (name,))
    if cur.fetchone():
        res["missing"] = False
        cur.execute(
            """UPDATE line_geom SET parts_gz=?, spine_gz=?, total_km=?,
                                  n_part=?, n_pt=?
               WHERE name=?""",
            (parts_gz, spine_gz, km, n_part, n_pt, name),
        )
        c.commit()
        res["lib"] = True
    c.close()
    return res


def _now_iso() -> str:
    return __import__("datetime").datetime.now().isoformat(timespec="seconds")


def drop_draft(name: str) -> bool:
    """删掉某条线的编辑器草稿（线路被删时一起清，否则草稿会剩在列表里冒充一条线）"""
    if not name:
        return False
    try:
        c = _rail()
        n = c.execute("DELETE FROM line_geom_edit WHERE name=?", (name,)).rowcount
        c.commit()
        c.close()
        _index_invalidate()
        return n > 0
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------
# 新建线路：画一条线 → 沿线抓站 → 建线入库
#
# 建线**不另开写库路径**，一律走 geoimport.import_line（项目里唯一的建线通道，
# raw 通道、以文件为准、里程按走向量），这里只负责把「地图上画的折线 + 挑好的车站」
# 组装成一份最小 GeoJSON。
# --------------------------------------------------------------------------

def _flatten(parts) -> List[List[float]]:
    """接受 [[lon,lat],…] / [[[…]],…] / 混合，统一成一条 [lon,lat] 折线"""
    if not parts:
        return []
    if isinstance(parts[0], (int, float)):
        parts = [parts]
    chain: List[List[float]] = []
    for seg in parts:
        if not seg:
            continue
        if isinstance(seg[0], (list, tuple)):
            chain.extend([[float(p[0]), float(p[1])] for p in seg if len(p) >= 2])
        elif isinstance(seg[0], (int, float)) and len(seg) >= 2:
            chain.append([float(seg[0]), float(seg[1])])
    return chain


def _pt_seg(a, b, p) -> Tuple[float, float]:
    """点 p 到线段 a-b 的距离（米）与投影参数 t∈[0,1]（等距投影）"""
    lat0 = math.radians(a[1])
    sx = 111320.0 * math.cos(lat0)
    sy = 110574.0
    px, py = (p[0] - a[0]) * sx, (p[1] - a[1]) * sy
    bx, by = (b[0] - a[0]) * sx, (b[1] - a[1]) * sy
    l2 = bx * bx + by * by
    if l2 == 0:
        return math.hypot(px, py), 0.0
    t = max(0.0, min(1.0, (px * bx + py * by) / l2))
    return math.hypot(px - t * bx, py - t * by), t


class _ChainIdx:
    """折线段的 0.05° 网格索引：把上万车站批量投到走向上，别逐段暴力算"""

    CELL = 0.05

    def __init__(self, chain: List[List[float]]):
        self.chain = chain
        self.acc = [0.0]
        for i in range(1, len(chain)):
            self.acc.append(self.acc[-1] + _haversine_km(chain[i - 1], chain[i]))
        self.b: dict = {}
        C = self.CELL
        for i in range(1, len(chain)):
            a, b = chain[i - 1], chain[i]
            x0, x1 = sorted((a[0], b[0]))
            y0, y1 = sorted((a[1], b[1]))
            for cx in range(int(math.floor(x0 / C)), int(math.floor(x1 / C)) + 1):
                for cy in range(int(math.floor(y0 / C)), int(math.floor(y1 / C)) + 1):
                    self.b.setdefault((cx, cy), []).append(i)

    def project(self, lon: float, lat: float):
        """→ (最近距离米, 沿线累计公里)；附近没段则 (None, 0)"""
        C = self.CELL
        cx0, cy0 = int(math.floor(lon / C)), int(math.floor(lat / C))
        best_d: Optional[float] = None
        best_km = 0.0
        seen = set()
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for i in self.b.get((cx0 + dx, cy0 + dy), ()):
                    if i in seen:
                        continue
                    seen.add(i)
                    d, t = _pt_seg(self.chain[i - 1], self.chain[i], [lon, lat])
                    if best_d is None or d < best_d:
                        best_d = d
                        best_km = self.acc[i - 1] + (self.acc[i] - self.acc[i - 1]) * t
        return best_d, best_km


_STA_CACHE = {"ts": 0.0, "rows": [], "wide_ts": 0.0, "wide": []}

# 有这些来源之一的才算「可当站序用」的国铁站；只有 cngeo/coordjson/master/osmhdx
# 的那批是 OSM HDX 抽出来的（含大量地铁/市域站，如 崇文门、磁器口），抓站时会污染站序。
_STA_TRUST = ("st12306", "archive", "chnsta", "crtrain", "gtfs", "panorama",
              "pomerun", "railglobe", "trainvis", "osmdb", "osmraw")


def _stations_all(wide: bool = False) -> list:
    """站库候选：国铁 + 有坐标 + 排除城市/行政区/地铁站。缓存 10 分钟。

    wide=False（默认）只要「有正经来源」的站，抓站精度优先；
    wide=True 放开成全部，用作兜底（偏远支线只有 OSM 点时）。
    """
    key, ts_key = ("wide", "wide_ts") if wide else ("rows", "ts")
    now = time.time()
    if _STA_CACHE[key] and now - _STA_CACHE[ts_key] < 600:
        return _STA_CACHE[key]
    rows: list = []
    cond = ""
    if not wide:
        cond = " AND (" + " OR ".join(
            f"sources LIKE '%{k}%'" for k in _STA_TRUST) + ")"
    try:
        c = sqlite3.connect(RAIL_DB)
        cur = c.cursor()
        cur.execute(
            """SELECT name, lon, lat FROM station_master
               WHERE kind='国铁' AND lat IS NOT NULL AND lon IS NOT NULL
                 AND (type IS NULL OR type NOT IN ('城市','地级行政区','地铁站'))"""
            + cond
        )
        rows = [(r[0], float(r[1]), float(r[2])) for r in cur.fetchall() if r[0]]
        c.close()
    except Exception:  # noqa: BLE001
        rows = []
    _STA_CACHE[key] = rows
    _STA_CACHE[ts_key] = now
    return rows


def search_stations(q: str, limit: int = 12) -> dict:
    """按名字（含别名）找站，用于「按名字加一站」。只给有坐标的。"""
    q = (q or "").strip()
    if not q:
        return {"items": []}
    like = f"%{q}%"
    items = []
    try:
        c = sqlite3.connect(RAIL_DB)
        cur = c.cursor()
        cur.execute(
            """SELECT name, lon, lat FROM station_master
               WHERE lat IS NOT NULL AND lon IS NOT NULL
                 AND (name LIKE ? OR aliases LIKE ?)
               ORDER BY CASE WHEN name=? THEN 0
                             WHEN name LIKE ? THEN 1 ELSE 2 END, n_sources DESC
               LIMIT ?""",
            (like, like, q, f"{q}%", limit),
        )
        items = [{"name": r[0], "lon": float(r[1]), "lat": float(r[2])}
                 for r in cur.fetchall()]
        c.close()
    except Exception:  # noqa: BLE001
        pass
    return {"items": items}


_RAW_CACHE: dict = {}


def _raw_parts(name: str) -> list:
    """这条线的**原始碎段**（不串线，纯解码，快）。

    ld_geom.parts_gz（整理台存的原始多段）> line_geom.parts_gz > railgeom(OSM)。
    抓站只需要「点在不在轨道附近」，串不串成一条线无所谓 —— 串线是 O(段数²)，
    陇海线要 16s、沪昆线更久，抓站没必要付这个代价。
    """
    hit = _RAW_CACHE.get(name)
    if hit and time.time() - hit[0] < 1800:
        return hit[1]
    parts: list = []
    for db, sql in ((LING_DB, "SELECT parts_gz FROM ld_geom WHERE name=?"),
                    (RAIL_DB, "SELECT parts_gz FROM line_geom WHERE name=?")):
        try:
            c = sqlite3.connect(db)
            cur = c.cursor()
            cur.execute(sql, (name,))
            row = cur.fetchone()
            c.close()
            raw = _ungz(row[0]) if row and row[0] else None
            if raw and isinstance(raw[0][0], (list, tuple)):
                parts = [p for p in raw if p and len(p) >= 2]
                if parts:
                    break
        except Exception:  # noqa: BLE001
            pass
    if not parts:
        try:
            g = railgeom.geom(name, expect_km=None)
            parts = [p for p in (g or {}).get("parts", []) if p and len(p) >= 2]
        except Exception:  # noqa: BLE001
            parts = []
    _RAW_CACHE[name] = (time.time(), parts)
    return parts


class _SegIdx:
    """原始碎段的网格索引：只回答「离最近的轨道多远」，不管顺序"""

    CELL = 0.02

    def __init__(self, parts: list):
        self.b: dict = {}
        C = self.CELL
        for pts in parts:
            for i in range(1, len(pts)):
                a, b = pts[i - 1], pts[i]
                x0, x1 = (a[0], b[0]) if a[0] <= b[0] else (b[0], a[0])
                y0, y1 = (a[1], b[1]) if a[1] <= b[1] else (b[1], a[1])
                for cx in range(int(math.floor(x0 / C)), int(math.floor(x1 / C)) + 1):
                    for cy in range(int(math.floor(y0 / C)), int(math.floor(y1 / C)) + 1):
                        self.b.setdefault((cx, cy), []).append((a, b))

    def nearest(self, lon: float, lat: float):
        C = self.CELL
        cx0, cy0 = int(math.floor(lon / C)), int(math.floor(lat / C))
        best: Optional[float] = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for a, b in self.b.get((cx0 + dx, cy0 + dy), ()):
                    d, _ = _pt_seg(a, b, [lon, lat])
                    if best is None or d < best:
                        best = d
        return best


def suggest_stations(parts, radius_m: float = 2000.0, name: str = "",
                     min_gap_km: float = 0.3, max_n: int = 200) -> dict:
    """把走向附近的车站投到线上，按沿线里程排好序 → 建线的候选站序。

    - **距离**用这条线的原始碎段量（弯道旁的站也能正确判在不在线上）；
      库里没有碎段（纯手绘新线）就用当前画的折线。
    - **里程/顺序**用当前折线（编辑器里的抽稀骨架足够排序）。
    min_gap_km 压掉同一站的多份重名记录（里程几乎重合的只留最近的）。
    """
    chain = _flatten(parts)
    if len(chain) < 2:
        raise ValueError("走向至少需要 2 个点")
    chain_idx = _ChainIdx(chain)
    raw = _raw_parts(name) if name else []
    seg_idx = _SegIdx(raw) if raw else None
    basis = ("raw+%d段" % len(raw)) if seg_idx else "drawn"
    minx = min(p[0] for p in chain)
    maxx = max(p[0] for p in chain)
    miny = min(p[1] for p in chain)
    maxy = max(p[1] for p in chain)
    pad = max(0.02, radius_m / 90000.0)   # 度，留点余量

    def _scan(rows, hits: dict) -> None:
        for nm, lon, lat in rows:
            if lon < minx - pad or lon > maxx + pad or lat < miny - pad or lat > maxy + pad:
                continue
            km = chain_idx.project(lon, lat)[1]
            if seg_idx is not None:
                d = seg_idx.nearest(lon, lat)     # 距离按原始轨道量
            else:
                d = chain_idx.project(lon, lat)[0]
            if d is None or d > radius_m:
                continue
            cur = hits.get(nm)
            if cur is None or d < cur["dist"]:
                hits[nm] = {"name": nm, "lon": round(lon, 6), "lat": round(lat, 6),
                            "km": round(km, 1), "dist": round(d)}

    hits: dict = {}
    _scan(_stations_all(), hits)
    wide = False
    if len(hits) < 2:                      # 支线/新线：放宽到全部 OSM 站再抓一遍
        wide = True
        _scan(_stations_all(wide=True), hits)
    items = sorted(hits.values(), key=lambda x: x["km"])
    keep, last = [], -1e9
    for it in items:
        if it["km"] - last < min_gap_km:
            # 里程几乎重合：留离走向更近的那个
            if keep and it["dist"] < keep[-1]["dist"]:
                keep[-1] = it
                last = it["km"]
            continue
        keep.append(it)
        last = it["km"]
    total = round(_poly_km(chain), 1)
    return {"items": keep[:max_n], "count": len(keep), "totalKm": total,
            "radius": radius_m, "wide": wide, "basis": basis}


def create_line(name: str, parts, stops, note: str = "",
                replace: bool = False) -> dict:
    """把「画的走向 + 挑好的车站」建成线路库里的正式线路。

    走 geoimport.import_line（唯一建线通道）：以文件为准、里程按走向量、
    站名不归一。这里只组装最小 GeoJSON，并先把能报的错报成人话。
    """
    import geoimport  # 局部导入，避免与服务启动顺序耦合

    name = (name or "").strip()
    if not name:
        raise ValueError("请先填写线路名")
    if "（" in name or "(" in name:
        raise ValueError("线名里不要带「（来源）」括号，规范名会撞整理台的归并规则")
    chain = _flatten(parts)
    if len(chain) < 2:
        raise ValueError("走向至少需要 2 个点：先在「添加点模式」下顺着线路点几下")
    chain = [_clean_pt(p) for p in chain]
    chain = [p for i, p in enumerate(chain)
             if i == 0 or p != chain[i - 1]]

    # 站：坐标以站库/前端给的为准，名字原样保留（不归一、不删「站」）
    sel: List[dict] = []
    seen = set()
    for s in (stops or []):
        nm = str((s or {}).get("name") or "").strip()
        if not nm or nm in seen:
            continue
        lon, lat = (s or {}).get("lon"), (s or {}).get("lat")
        if lon is None or lat is None:
            got = _station_xy(nm)
            if not got:
                continue
            lon, lat = got
        seen.add(nm)
        sel.append({"name": nm, "lon": float(lon), "lat": float(lat)})
    if len(sel) < 2:
        raise ValueError("至少要 2 个车站才成一条线（点「沿线抓站」自动认，或按名字加）")

    try:
        cur = __import__("cnrail").line(name)
    except Exception:  # noqa: BLE001
        cur = None
    if cur and not (replace and cur.get("custom")):
        if cur.get("custom"):
            raise ValueError("线路库里已经有「%s」（%s 站，自建），可勾选「覆盖同名自建线」"
                             % (name, cur.get("n")))
        raise ValueError("线路库里已经有「%s」（%s 站，底表/资料库线路），不能覆盖 —— "
                         "换个线名，或先在线路页把这条线删掉"
                         % (name, cur.get("n")))

    # 先试排一次：拿到准确的站数/里程写进备注，也提前拦掉「串不成线」
    seq, gch, stat = geoimport.assemble(
        [chain], sel, sel[0]["name"] if sel else "", sel[-1]["name"] if sel else "")
    if len(seq) < 2:
        raise ValueError("能落到这条走向上的车站少于 2 个：检查站的坐标、或把走向补全")
    km = round((seq[-1]["km"] if seq else 0) or stat.get("totalKm") or 0, 1)

    feats = [{
        "type": "Feature",
        "properties": {"线路名称": name, "起点": seq[0]["name"], "终点": seq[-1]["name"]},
        "geometry": {"type": "LineString", "coordinates": [[p[0], p[1]] for p in chain]},
    }]
    for s in sel:
        feats.append({
            "type": "Feature",
            "properties": {"站名": s["name"], "类型": "车站"},
            "geometry": {"type": "Point", "coordinates": [s["lon"], s["lat"]]},
        })
    geo = {"type": "FeatureCollection",
           "properties": {"线路名称": name, "起点": seq[0]["name"], "终点": seq[-1]["name"]},
           "features": feats}
    auto_note = note.strip() or ("地图手绘 · %d 站 / %.1f km" % (len(seq), km))

    r = geoimport.import_line(geo, "地图手绘", replace=replace, note=auto_note)

    # 草稿同步一份，整理台/编辑器再打开时看到的就是这条
    try:
        parts_gz = _gz([chain])
        spine_gz = _gz(chain)
        c = _rail()
        c.execute(
            """INSERT INTO line_geom_edit(name, parts_gz, spine_gz, total_km,
                                          n_part, n_pt, src, note, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET
                 parts_gz=excluded.parts_gz, spine_gz=excluded.spine_gz,
                 total_km=excluded.total_km, n_part=excluded.n_part,
                 n_pt=excluded.n_pt, src=excluded.src, note=excluded.note,
                 updated_at=excluded.updated_at""",
            (name, parts_gz, spine_gz, km, 1, len(chain), "draw", auto_note,
             time.time(), time.time()),
        )
        c.commit()
        c.close()
    except Exception:  # noqa: BLE001
        pass
    _index_invalidate()                  # 新建线也要立刻能在列表里搜到

    out = dict(r)
    out.update({"ok": True, "created": True, "name": r.get("name") or name,
                "km": km, "note": auto_note})
    return out


def _clean_pt(p) -> List[float]:
    lon = max(-180.0, min(180.0, float(p[0])))
    lat = max(-90.0, min(90.0, float(p[1])))
    return [round(lon, 6), round(lat, 6)]


def _station_xy(name: str):
    """站库里按名字/别名查坐标 → (lon, lat) 或 None"""
    try:
        c = sqlite3.connect(RAIL_DB)
        cur = c.cursor()
        cur.execute(
            """SELECT lon, lat FROM station_master
               WHERE lat IS NOT NULL AND lon IS NOT NULL
                 AND (name=? OR key=? OR aliases LIKE ?)
               ORDER BY CASE WHEN name=? THEN 0 ELSE 1 END, n_sources DESC LIMIT 1""",
            (name, name, f"%{name}%", name),
        )
        row = cur.fetchone()
        c.close()
        if row:
            return float(row[0]), float(row[1])
    except Exception:  # noqa: BLE001
        pass
    return None
