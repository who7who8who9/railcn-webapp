# -*- coding: utf-8 -*-
"""
historyq.py — 历史数据查询

面向「已经落库的历史」而不是「今天还能不能买到票」：
  1. 某车次在日期段内哪天开行、哪天不开行
  2. 开行日的经停站时刻（复用 railcore.build_route_payload）
  3. 按列车大类「按日」统计开行 / 不开行趟数（每天每类一行数据）

三张表的关系：
  trains     每日枚举到的可售车次        → 某日有该车次 = 当天开行
  routes     已下载的经停站（raw JSON）  → 有记录 = 停站已入库
  day_skip   试过但查不到经停站的车次     → 明确「当天不开行」

单日状态判定顺序（从上往下，命中即止）：
  routes 命中       → open   开行，停站已存
  day_skip 命中     → skip   明确不开行
  trains 命中       → open   开行，但停站没下
  该日 trains 非空  → skip   推断：当天枚举里没有这趟
  该日无任何采集    → nodata 不知道

注意：开行与否以 12306「当日可售车次」为准，停运但留有数据的车次不会出现在这里。
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

import railcore

WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
MAX_DAYS = 400          # 一次查询最多覆盖的天数，避免全表长扫

TYPE_LABEL = {
    "G": "高铁", "D": "动车", "C": "城际", "Z": "直达特快", "T": "特快",
    "K": "快速", "S": "市郊", "Y": "旅游", "N": "数字车次",
}

# trains.type 里除了字母，还混着纯数字车次的首字符（4/5/6/7/8…），统一归到 N
_TYPE_EXPR = "CASE WHEN type GLOB '[A-Z]' THEN type ELSE 'N' END"

_ENSURED = False


def _label(t: str) -> str:
    return TYPE_LABEL.get(t, t)


def ensure_indexes() -> None:
    """按需建索引（只在进程内第一次查询时做一次）"""
    global _ENSURED
    if _ENSURED:
        return
    _ENSURED = True
    stmts = (
        "CREATE INDEX IF NOT EXISTS idx_trains_date_type ON trains(date, type)",
        "CREATE INDEX IF NOT EXISTS idx_trains_code_date ON trains(code, date)",
        "CREATE INDEX IF NOT EXISTS idx_routes_code_date ON routes(code, date)",
        "CREATE INDEX IF NOT EXISTS idx_skip_code_date ON day_skip(code, date)",
    )
    try:
        with railcore.db() as c:
            for s in stmts:
                try:
                    c.execute(s)
                except sqlite3.Error:
                    pass
    except sqlite3.Error:
        pass


def _norm(s: Optional[str]) -> Optional[str]:
    s = (s or "").strip()
    if not s:
        return None
    if len(s) == 8 and s.isdigit():          # 20261026 → 2026-10-26
        s = f"{s[:4]}-{s[4:6]}-{s[6:]}"
    try:
        return dt.date.fromisoformat(s).isoformat()
    except ValueError:
        return None


def _covered_dates(c: sqlite3.Connection, df: str, dt_: str) -> set:
    """「这一天有采集过」的日期：枚举到的车次表 或 已下载的经停站，任一非空即可"""
    out = {r[0] for r in c.execute(
        "SELECT DISTINCT date FROM trains WHERE date BETWEEN ? AND ?", (df, dt_))}
    out |= {r[0] for r in c.execute(
        "SELECT DISTINCT date FROM routes WHERE date BETWEEN ? AND ?", (df, dt_))}
    return out


def _enumerated_dates(c: sqlite3.Connection, df: str, dt_: str) -> set:
    """「这一天枚举过车次表」的日期 —— 只有这种日子才能断言某车「不开行」

    只有停站、没有枚举的日子（下载中途停掉）确实不知道当天有哪些车，
    拿它去推断「不开行」会把没跑完的日子误判成停运。
    """
    return {r[0] for r in c.execute(
        "SELECT DISTINCT date FROM trains WHERE date BETWEEN ? AND ?", (df, dt_))}


def _range(date_from: Optional[str], date_to: Optional[str]) -> Tuple[str, str, int]:
    df, dt_ = _norm(date_from), _norm(date_to)
    if not df or not dt_:
        with railcore.db() as c:
            lo, hi = c.execute(
                """SELECT MIN(d), MAX(d) FROM (
                       SELECT date d FROM trains UNION ALL SELECT date d FROM routes)"""
            ).fetchone()
        if not lo:
            raise ValueError("库里还没有历史数据，先到「数据下载」下几天")
        df = df or lo
        dt_ = dt_ or hi
    if df > dt_:
        df, dt_ = dt_, df
    span = (dt.date.fromisoformat(dt_) - dt.date.fromisoformat(df)).days + 1
    if span > MAX_DAYS:
        raise ValueError(f"日期段太长（{span} 天），一次最多查 {MAX_DAYS} 天")
    ensure_indexes()
    return df, dt_, span


def _walk(df: str, dt_: str):
    d = dt.date.fromisoformat(df)
    end = dt.date.fromisoformat(dt_)
    while d <= end:
        yield d
        d += dt.timedelta(days=1)


def _partial(totals: Dict[str, int]) -> Dict[str, bool]:
    """哪几天明显没下完（枚举车次不到最全那天的 75%）—— 这种日子的「不开行」会虚高"""
    vals = [v for v in totals.values() if v]
    if not vals:
        return {}
    ref = max(vals)
    return {d: (v > 0 and v < ref * 0.75) for d, v in totals.items()}


# ---------------------------------------------------------------------------
# 一、有哪些数据
# ---------------------------------------------------------------------------
def coverage(date_from: Optional[str] = None, date_to: Optional[str] = None) -> dict:
    df, dt_, span = _range(date_from, date_to)
    with railcore.db() as c:
        listed = {r["date"]: r["n"] for r in c.execute(
            "SELECT date, COUNT(*) n FROM trains WHERE date BETWEEN ? AND ? GROUP BY date",
            (df, dt_))}
        routed = {r["date"]: r["n"] for r in c.execute(
            "SELECT date, COUNT(*) n FROM routes WHERE date BETWEEN ? AND ? GROUP BY date",
            (df, dt_))}
        skipped = {r["date"]: r["n"] for r in c.execute(
            "SELECT date, COUNT(*) n FROM day_skip WHERE date BETWEEN ? AND ? GROUP BY date",
            (df, dt_))}
        pools = [(r["t"], r["u"]) for r in c.execute(
            f"SELECT {_TYPE_EXPR} t, COUNT(DISTINCT code) u FROM trains GROUP BY t ORDER BY u DESC")]

    days = []
    part = _partial(listed)
    for d in _walk(df, dt_):
        s = d.isoformat()
        days.append({
            "date": s, "weekday": WEEKDAYS[d.weekday()],
            "listed": listed.get(s, 0),
            "routes": routed.get(s, 0),
            "skipped": skipped.get(s, 0),
            "covered": listed.get(s, 0) > 0 or routed.get(s, 0) > 0,
            "partial": bool(part.get(s)),
        })
    return {
        "from": df, "to": dt_, "span": span,
        "coveredDays": sum(1 for x in days if x["covered"]),
        "days": days,
        "types": [{"type": t, "label": _label(t), "pool": u} for t, u in pools],
        "poolTotal": sum(u for _, u in pools),
    }


# ---------------------------------------------------------------------------
# 二、某车次的开行日历
# ---------------------------------------------------------------------------
def train_calendar(code: str, date_from: Optional[str] = None,
                   date_to: Optional[str] = None) -> dict:
    code = (code or "").strip().upper()
    if not code:
        raise ValueError("缺少车次号")
    df, dt_, span = _range(date_from, date_to)

    with railcore.db() as c:
        rows = c.execute(
            """SELECT date, train_no, type, start_sta, end_sta, stop_count
               FROM trains WHERE code=? AND date BETWEEN ? AND ?""",
            (code, df, dt_)).fetchall()
        info = {r["date"]: dict(r) for r in rows}
        rrows = c.execute(
            "SELECT date, raw FROM routes WHERE code=? AND date BETWEEN ? AND ?",
            (code, df, dt_)).fetchall()
        stops_n: Dict[str, int] = {}
        for r in rrows:
            try:
                stops_n[r["date"]] = len(json.loads(r["raw"])) if r["raw"] else 0
            except (ValueError, TypeError):
                stops_n[r["date"]] = 0
        skipped = {r["date"] for r in c.execute(
            "SELECT date FROM day_skip WHERE code=? AND date BETWEEN ? AND ?",
            (code, df, dt_))}
        covered = _enumerated_dates(c, df, dt_)
        known = c.execute(
            """SELECT 1 FROM trains WHERE code=?
               UNION SELECT 1 FROM day_skip WHERE code=?
               UNION SELECT 1 FROM routes WHERE code=? LIMIT 1""",
            (code, code, code)).fetchone()

    if not known:
        # 全库都没见过这个车次号：不要硬套「不开行」，否则看起来像真跑过
        days = [{"date": d.isoformat(), "weekday": WEEKDAYS[d.weekday()],
                 "state": "nodata", "hasRoute": False, "stops": 0,
                 "trainNo": "", "type": "", "from": "", "to": "", "stopCount": 0}
                for d in _walk(df, dt_)]
        return {
            "code": code, "unknown": True,
            "from": df, "to": dt_, "span": span, "od": {}, "trainNos": [],
            "days": days, "runs": [],
            "summary": {"open": 0, "skip": 0, "nodata": len(days), "total": len(days),
                        "withStops": 0, "covered": 0},
        }

    days = []
    prev = None
    runs: List[dict] = []
    for d in _walk(df, dt_):
        s = d.isoformat()
        meta = info.get(s) or {}
        if s in stops_n:
            state, has_route = "open", True
        elif s in skipped:
            state, has_route = "skip", False
        elif meta:
            state, has_route = "open", False
        elif s in covered:
            state, has_route = "skip", False      # 推断
        else:
            state, has_route = "nodata", False

        row = {
            "date": s, "weekday": WEEKDAYS[d.weekday()],
            "state": state,
            "hasRoute": has_route,
            "stops": stops_n.get(s, 0) if has_route else 0,
            "trainNo": meta.get("train_no") or "",
            "type": meta.get("type") or "",
            "from": meta.get("start_sta") or "",
            "to": meta.get("end_sta") or "",
            "stopCount": meta.get("stop_count") or 0,
        }
        if state == "skip" and s not in skipped:
            row["inferred"] = True
        days.append(row)

        if state == "open":
            if runs and runs[-1]["to"] == prev:
                runs[-1]["to"] = s
                runs[-1]["days"] += 1
            else:
                runs.append({"from": s, "to": s, "days": 1})
        prev = s

    od = {}
    for r in reversed(days):
        if r["from"] or r["to"]:
            od = {"from": r["from"], "to": r["to"]}
            break

    return {
        "code": code,
        "from": df, "to": dt_, "span": span,
        "od": od,
        "trainNos": sorted({x["trainNo"] for x in days if x["trainNo"]}),
        "days": days,
        "runs": runs,
        "summary": {
            "open": sum(1 for x in days if x["state"] == "open"),
            "skip": sum(1 for x in days if x["state"] == "skip"),
            "nodata": sum(1 for x in days if x["state"] == "nodata"),
            "total": len(days),
            "withStops": sum(1 for x in days if x["hasRoute"]),
            "covered": sum(1 for x in days if x["state"] != "nodata"),
        },
    }


# ---------------------------------------------------------------------------
# 三、按列车大类统计
# ---------------------------------------------------------------------------
def type_stats(date_from: Optional[str] = None, date_to: Optional[str] = None,
               focus: Optional[str] = None) -> dict:
    """按日统计：日期段内每天各大类开行 / 不开行多少趟

    口径：
      pool        全库登记过的该大类车次数（分母）
      openTimes   覆盖日里每天开行趟数之和（车次·日）
      skipTimes   每天 (pool - 当日开行) 之和（车次·日）
      openDays    段内有开行的天数
      daily[]     日期段内的每一天，types 里放当日各大类开行趟数
    """
    fk = (focus or "").strip().upper() or None
    if fk and not fk.isalpha():
        fk = None
    df, dt_, span = _range(date_from, date_to)

    with railcore.db() as c:
        per_rows = c.execute(
            f"""SELECT date, {_TYPE_EXPR} t, COUNT(*) n FROM trains
                WHERE date BETWEEN ? AND ? GROUP BY date, t""",
            (df, dt_)).fetchall()
        pool_rows = c.execute(
            f"SELECT {_TYPE_EXPR} t, COUNT(DISTINCT code) u FROM trains GROUP BY t")
        pools = {r["t"]: r["u"] for r in pool_rows}
        union_rows = c.execute(
            f"""SELECT {_TYPE_EXPR} t, COUNT(DISTINCT code) u FROM trains
                WHERE date BETWEEN ? AND ? GROUP BY t""",
            (df, dt_)).fetchall()
        unions = {r["t"]: r["u"] for r in union_rows}
        totals = {r["date"]: r["n"] for r in c.execute(
            "SELECT date, COUNT(*) n FROM trains WHERE date BETWEEN ? AND ? GROUP BY date",
            (df, dt_))}
        routed = {r[0] for r in c.execute(
            "SELECT DISTINCT date FROM routes WHERE date BETWEEN ? AND ?", (df, dt_))}

    # 只有「枚举过车次」的日子才能算开行/不开行；只有停站没有枚举的日子无从判断，
    # 既不能算进分母，也不该被当成「全库都不开行」
    covered = sorted(totals)
    per: Dict[Tuple[str, str], int] = {(r["date"], r["t"]): r["n"] for r in per_rows}
    cset = set(covered)
    part = _partial(totals)

    types = []
    for t, pool in sorted(pools.items(), key=lambda x: -x[1]):
        open_days = [per.get((d, t), 0) for d in covered]
        open_times = sum(open_days)
        skip_times = sum(max(0, pool - v) for v in open_days)
        types.append({
            "type": t, "label": _label(t),
            "pool": pool,
            "unionInRange": unions.get(t, 0),
            "openTimes": open_times,
            "skipTimes": skip_times,
            "openDays": sum(1 for v in open_days if v > 0),
            "coveredDays": len(covered),
            "avgOpen": round(open_times / len(covered), 1) if covered else 0,
            "maxOpen": max(open_days) if open_days else 0,
            "minOpen": min(open_days) if open_days else 0,
        })

    # 按日矩阵：段内每一天 × 每个大类（无采集的日也留一行，前端灰显）
    order = [t["type"] for t in types]
    daily = []
    for d in _walk(df, dt_):
        s = d.isoformat()
        ok = s in cset
        row = {
            "date": s, "weekday": WEEKDAYS[d.weekday()],
            "covered": ok,
            "routesOnly": (not ok) and s in routed,
            "listed": totals.get(s, 0),
            "partial": bool(part.get(s)),
            "types": {}, "open": 0, "skip": 0,
        }
        for t in order:
            o = per.get((s, t), 0) if ok else 0
            row["types"][t] = o
            if ok:
                row["open"] += o
                row["skip"] += max(0, pools[t] - o)
        daily.append(row)

    out = {
        "from": df, "to": dt_, "span": span,
        "coveredDays": len(covered),
        "days": [{"date": d, "weekday": WEEKDAYS[dt.date.fromisoformat(d).weekday()],
                  "covered": d in cset, "listed": totals.get(d, 0),
                  "partial": bool(part.get(d))} for d in covered],
        "daily": daily,
        "types": types,
        "poolTotal": sum(pools.values()),
    }

    if fk:
        daily_f = []
        for d in _walk(df, dt_):
            s = d.isoformat()
            ok = s in cset
            o = per.get((s, fk), 0) if ok else 0
            daily_f.append({
                "date": s, "weekday": WEEKDAYS[d.weekday()],
                "covered": ok,
                "open": o,
                "skip": max(0, pools.get(fk, 0) - o) if ok else 0,
                "listed": totals.get(s, 0),
                "partial": bool(part.get(s)),
            })
        row = next((x for x in types if x["type"] == fk), None)
        out["focus"] = {
            "type": fk, "label": _label(fk),
            "summary": row or {"type": fk, "label": _label(fk), "pool": pools.get(fk, 0),
                               "openTimes": 0, "skipTimes": 0, "avgOpen": 0,
                               "openDays": 0, "unionInRange": 0},
            "days": daily_f,
        }
    return out


# ---------------------------------------------------------------------------
# 四、某车次某日的停站时刻
# ---------------------------------------------------------------------------
def stops_of(code: str, date: str) -> dict:
    """从库里取已落库的停站时刻（不联网；没有就是没下载）"""
    code = (code or "").strip().upper()
    date = _norm(date) or ""
    if not code or not date:
        raise ValueError("需要车次号和日期")
    with railcore.db() as c:
        r = c.execute(
            "SELECT raw FROM routes WHERE code=? AND date=?", (code, date)).fetchone()
        skipped = c.execute(
            "SELECT 1 FROM day_skip WHERE code=? AND date=?", (code, date)).fetchone()
        meta = c.execute(
            "SELECT train_no, type, start_sta, end_sta, stop_count FROM trains WHERE code=? AND date=?",
            (code, date)).fetchone()
    if not r or not r["raw"]:
        return {
            "code": code, "date": date, "found": False,
            "reason": "不开行（当天查不到经停站）" if skipped else "还没下载这天的经停站",
            "meta": dict(meta) if meta else None,
            "stops": [],
        }
    try:
        raw = json.loads(r["raw"])
    except (ValueError, TypeError):
        raw = []
    stops = []
    for x in raw:
        stops.append({
            "no": x.get("station_no") or "",
            "name": x.get("station_name") or "",
            "arrive": (x.get("arrive_time") or "").replace("----", ""),
            "depart": (x.get("start_time") or "").replace("----", ""),
            "dayDiff": x.get("arrive_day_diff") or "0",
            "dayStr": x.get("arrive_day_str") or "",
            "running": x.get("running_time") or "",
            "isStart": x.get("is_start") == "Y",
            "isEnd": ((x.get("station_name") or "") == (x.get("end_station_name") or "")),
        })
    return {
        "code": code, "date": date, "found": True,
        "meta": dict(meta) if meta else None,
        "stops": stops,
    }
