# -*- coding: utf-8 -*-
"""linemap.py — 「确认线路地图」独立页的数据出口（只读）

口径和「车站/线路」页完全一致：只认整理台**完成全部确认**的线路
（`confirmed.recent()`），几何走 `lineosm.geom(confirmed_only=True)` 那份
多段 `parts`，站序走 `confirmed.detail_payload()`。别在这里按线名去
rail.db 找同名旧版本，否则来源版本 / 草稿线会混进来。

页面要一次画几十条线，所以做成**批量**接口：一次请求把选中线路的
抽稀几何 + 站点全给回来，别让前端一条一条打 42 次请求。
"""
from __future__ import annotations

import time
from typing import Optional, Sequence

import confirmed
import lineosm

_CACHE: dict = {}
_CACHE_TS = 0.0
_CACHE_TTL = 20.0          # 整理台改了站序 / 几何，最多 20 秒自动跟上


def _fresh() -> None:
    global _CACHE_TS
    now = time.time()
    if now - _CACHE_TS > _CACHE_TTL:
        _CACHE.clear()
        _CACHE_TS = now


def index() -> dict:
    """全部确认线路，按「最近确认」倒序（和主站下方清单同一顺序）。"""
    rows = confirmed.recent()
    items = [{
        "name": r.get("name") or "",
        "nStop": r.get("nStop") or 0,
        "totalKm": r.get("totalKm"),
        "fromSta": r.get("fromSta") or "",
        "toSta": r.get("toSta") or "",
        "confirmedAt": r.get("confirmedAt") or "",
        "geomSrc": r.get("geomSrc") or "",
        "nGeomPt": r.get("nGeomPt") or 0,
    } for r in rows if r.get("name")]
    return {"items": items, "total": len(items), "scope": "confirmed"}


def _thin(parts: Sequence, budget: int) -> list:
    """按段抽稀到不超过 budget 个点，每段首尾都保留。

    线路的 `parts` 是乱序碎段，抽稀只能**逐段**做；整条摊平再等距取样
    会把短段全吃掉，画出来就是几根几百公里的假直线（见底稿第 40 条）。
    """
    raw = [p for p in (parts or []) if isinstance(p, (list, tuple)) and len(p) > 1]
    total = sum(len(p) for p in raw)
    step = (total + budget - 1) // budget if total > budget else 1
    out = []
    for p in raw:
        try:
            a = [[round(float(q[0]), 6), round(float(q[1]), 6)]
                 for q in p[::step]]
            tail = [round(float(p[-1][0]), 6), round(float(p[-1][1]), 6)]
        except (TypeError, ValueError, IndexError):
            continue
        if a and a[-1] != tail:
            a.append(tail)
        if len(a) >= 2:
            out.append(a)
    return out


def _bbox(parts: Sequence) -> Optional[list]:
    xs, ys = [], []
    for p in parts:
        for q in p:
            try:
                xs.append(float(q[0]))
                ys.append(float(q[1]))
            except (TypeError, ValueError, IndexError):
                continue
    if not xs:
        return None
    return [round(min(xs), 5), round(min(ys), 5),
            round(max(xs), 5), round(max(ys), 5)]


def geometry(names: Sequence[str], detail: int = 900,
             with_stations: bool = False) -> dict:
    """批量取选中线路的多段几何。

    `names` 里不在确认白名单的名字直接跳过（不报错、不画），这样前端
    保存过期的勾选也不会把草稿线画上去。
    """
    _fresh()
    budget = max(120, min(3000, int(detail or 900)))
    allowed = confirmed.names()
    out = []
    for nm in dict.fromkeys(str(n or "").strip() for n in (names or [])):
        if not nm or nm not in allowed:
            continue
        ck = (nm, budget, bool(with_stations))
        if ck in _CACHE:
            out.append(_CACHE[ck])
            continue
        try:
            g = lineosm.geom(nm, confirmed_only=True) or {}
        except Exception:                       # noqa: BLE001  库忙 / 表结构旧
            g = {}
        parts = _thin(g.get("parts") or [], budget)
        item = {
            "name": nm,
            "found": bool(parts),
            "parts": parts,
            "nPart": len(parts),
            "nPt": sum(len(p) for p in parts),
            "bbox": _bbox(parts),
            "geomSrc": g.get("source") or "",
            "stations": [],
        }
        if with_stations:
            try:
                d = confirmed.detail_payload(nm) or {}
            except Exception:                   # noqa: BLE001
                d = {}
            item["stations"] = [
                {"name": s.get("name"), "lon": round(float(s["lon"]), 6),
                 "lat": round(float(s["lat"]), 6), "km": s.get("km")}
                for s in (d.get("stations") or [])
                if s.get("lat") is not None and s.get("lon") is not None
            ]
        _CACHE[ck] = item
        out.append(item)
    return {"items": out, "detail": budget, "scope": "confirmed"}
