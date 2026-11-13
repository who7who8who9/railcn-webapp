# -*- coding: utf-8 -*-
"""linegroup.py — 一条线，和它的几个「来源版本」

批量导回备份以后，同一份线路在库里是**几条独立的线**：

    京沪线              ← 用户自己导的 GeoJSON，163 站
    京沪线（底表）      150 站   12306 底表口径
    京沪线（OSM）       166 站   几何插值，站点最全
    京沪线（自建）      156 站   清空之前线路库里的那份
    京沪线（线路资料库·82站）

名字后缀就是来源。这个模块把「同名不同来源」重新认成同一条线的几个版本：

    split(name)         名字 → (基础名, 来源)
    members(base)       这条基础名底下的全部版本（底表排最前）
    effective(name)     系统该用哪一份：默认 > 底表 > 站数最多
    default_of / set_default
                        用户点 ★ 选的那份（表 line_default）

规则（2026-11-02 与用户确认）：

  · **只认同名**：去掉「（来源）」后缀之后同名，才算同一条线的不同版本。
    京沪铁路 / 京沪高铁 名字不同，各自独立 —— 系统不替用户猜它们是不是一条线。
  · **只有默认那份**参与数据校对的线路选择与地图展示；没设默认时按「（底表）」
    那份取数，组里没有底表才退到站数最多的那份。
  · 车次地图上的走向是按线名去 OSM 借几何的，各版本共用同一条几何，所以版本只
    影响站序（站数 / 里程 / 车站位置），走向不变。
"""
from __future__ import annotations

import re
import sqlite3
import time
from typing import Dict, List, Optional, Tuple

import railcore

# 备份导入时用的来源名（见 scripts/import_backup_lines.py 的 SOURCES）
SOURCES: Tuple[str, ...] = ("线路资料库", "china-emu", "底表", "自建", "OSM",
                            "GeoJSON", "备份")

# 「京沪线（底表）」「沪昆线（线路资料库·211站）」「京沪线（OSM）#2」
_SUF = re.compile(r"^(?P<base>.+?)（(?P<src>[^（）]+?)(?:·(?P<n>\d+)站)?）(?:#\d+)?$")
_TAIL = re.compile(r"#\d+$")

_INDEX: Optional[Dict[str, List[dict]]] = None
_INDEX_SIG: Optional[tuple] = None
_DEF_CACHE: Optional[Dict[str, str]] = None

# 线路库指纹的缓存时间：一次页面请求里会问几十遍「库里变了吗」，每秒都去
# COUNT/MAX 一遍太浪费；2 秒内直接复用上一次的结果。
_SIG_TTL = 2.0
_SIG_AT = 0.0
_SIG_VAL: Optional[tuple] = None


# ---------------------------------------------------------------------------
# 拆名字 / 归组
# ---------------------------------------------------------------------------
def split(name: str) -> Tuple[str, str]:
    """线名 → (基础名, 来源)；不是「（来源）」这种后缀就原样返回，来源为空串

    只在括号里写着已知来源名时才拆。「某某线（上）」这类真带括号的线名不会被
    误拆成「某某线 / 上」。
    """
    s = re.sub(r"\s+", "", str(name or ""))
    m = _SUF.match(s)
    if not m:
        return _TAIL.sub("", s), ""
    src = m.group("src")
    if src not in SOURCES:
        return _TAIL.sub("", s), ""
    return m.group("base"), src


def _sig() -> Optional[tuple]:
    """线路库的「版本指纹」：行数 + 最后改动时间。变了就重建索引。

    为什么不用全局计数器：`line_custom` 会被批量导入脚本、备份面板、GeoJSON 导入
    各写各的，谁都不一定记得通知这里。查一次 COUNT/MAX 比让每个写入口都记得
    调 invalidate() 靠谱。结果缓存 `_SIG_TTL` 秒，免得一次请求里问几十遍。
    """
    global _SIG_AT, _SIG_VAL
    now = time.time()
    if _SIG_VAL is not None and now - _SIG_AT < _SIG_TTL:
        return _SIG_VAL
    try:
        conn = railcore.db()
        try:
            r = conn.execute(
                "SELECT COUNT(*) AS c, MAX(updated_at) AS u FROM line_custom").fetchone()
            _SIG_VAL = (r["c"], r["u"])
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        _SIG_VAL = None
    _SIG_AT = now
    return _SIG_VAL


def invalidate() -> None:
    global _INDEX, _INDEX_SIG, _DEF_CACHE, _SIG_VAL, _SIG_AT
    _INDEX = _INDEX_SIG = _DEF_CACHE = None
    _SIG_VAL, _SIG_AT = None, 0.0


def _index() -> Dict[str, List[dict]]:
    global _INDEX, _INDEX_SIG
    sig = _sig()
    if _INDEX is not None and _INDEX_SIG == sig:
        return _INDEX
    idx: Dict[str, List[dict]] = {}
    try:
        import cnrail
        for it in cnrail.search_line("", 20000):
            nm = str(it.get("name") or "")
            if not nm:
                continue
            base, origin = split(nm)
            idx.setdefault(base, []).append({
                "name": nm, "origin": origin,
                "n": it.get("n"), "km": it.get("totalKm"),
            })
    except Exception:                       # noqa: BLE001
        idx = {}
    for ms in idx.values():
        # 底表排最前（没设默认时用的就是它），其余按站数多→少
        ms.sort(key=lambda m: (0 if m["origin"] == "底表" else 1,
                              -(m["n"] or 0), m["name"]))
    _INDEX, _INDEX_SIG = idx, sig
    return idx


def members(name: str) -> List[dict]:
    """这条基础名底下的全部版本（含自己）；名字不认识就返回空列表"""
    base, _ = split(name)
    if not base:
        return []
    return _index().get(base) or []


# ---------------------------------------------------------------------------
# 默认版本（用户点 ★ 选的那份）
# ---------------------------------------------------------------------------
def _ensure(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS line_default (
               name       TEXT PRIMARY KEY,   -- 基础线名（组名）
               pick       TEXT NOT NULL,      -- 选中的版本线名（line_custom.name）
               updated_at REAL
           )""")


def all_defaults() -> Dict[str, str]:
    global _DEF_CACHE
    if _DEF_CACHE is None:
        try:
            conn = railcore.db()
            try:
                _ensure(conn)
                _DEF_CACHE = {r["name"]: r["pick"] for r in
                              conn.execute("SELECT name, pick FROM line_default")}
            finally:
                conn.close()
        except sqlite3.DatabaseError:
            _DEF_CACHE = {}
    return _DEF_CACHE


def default_of(name: str) -> str:
    """这条线的默认版本名；没设过返回空串"""
    base, _ = split(name)
    return all_defaults().get(base, "") if base else ""


def set_default(name: str, pick: str) -> dict:
    """把某条线的默认版本设成 pick；pick 为空 = 取消默认，退回「底表 / 站数最多」"""
    global _DEF_CACHE
    base, _ = split(name)
    if not base:
        raise ValueError("要指定是哪条线")
    pick = str(pick or "").strip()
    if pick:
        names = [m["name"] for m in members(base)]
        if names and pick not in names:
            raise ValueError("「%s」不是「%s」的版本" % (pick, base))
    with railcore._db_lock, railcore.db() as conn:
        _ensure(conn)
        if pick:
            conn.execute(
                "INSERT INTO line_default(name, pick, updated_at) VALUES(?,?,?)"
                " ON CONFLICT(name) DO UPDATE SET pick=excluded.pick,"
                " updated_at=excluded.updated_at", (base, pick, time.time()))
        else:
            conn.execute("DELETE FROM line_default WHERE name=?", (base,))
    _DEF_CACHE = None
    return {"name": base, "pick": pick}


# ---------------------------------------------------------------------------
# 生效版本
# ---------------------------------------------------------------------------
def effective(name: str) -> dict:
    """这条线「系统该用哪一份」

    返回 {name, base, origin, why, members}，why ∈ linebase / default / base / largest / none：
      linebase 整理台定稿的「基本线路」，全面接管（2026-11-03 用户确认）
      default  用户在界面上 ★ 勾过
      base     没勾过 → 用「（底表）」那份（12306 底表口径）
      largest  组里没有底表 → 站数最多的那份
      none     这条线压根不在版本组里（只有它自己）

    点名要看某个来源版本（名字带「（来源）」后缀）时不接管，返回那一份本身。
    """
    base, origin = split(name)
    ms = members(base)
    # 基本线路最高优先：用户在整理台定稿、全项目接管的那一份。
    # 连来源版本都没有的线（整理台新建的）也认它。
    if not origin:
        try:
            import linebase
            b = linebase.get(base)
        except Exception:                    # noqa: BLE001
            b = None
        if b:
            return {"name": b["name"], "base": base, "origin": "基本",
                    "why": "linebase", "members": ms,
                    "baseId": b.get("id"), "baseStops": len(b.get("stops") or [])}
    if not ms:
        return {"name": name, "base": base, "origin": origin,
                "why": "none", "members": []}
    names = [m["name"] for m in ms]
    d = all_defaults().get(base) or ""
    if d in names:
        return {"name": d, "base": base, "origin": split(d)[1],
                "why": "default", "members": ms}
    for m in ms:
        if m["origin"] == "底表":
            return {"name": m["name"], "base": base, "origin": "底表",
                    "why": "base", "members": ms}
    m = max(ms, key=lambda x: (x["n"] or 0, x["name"]))
    return {"name": m["name"], "base": base, "origin": m["origin"],
            "why": "largest", "members": ms}


def effective_name(name: str) -> str:
    eff = effective(name)
    return eff["name"] if eff.get("members") else ""


def info(name: str) -> dict:
    """给接口用的归组信息（列表行、详情页头部都用它）"""
    base, origin = split(name)
    ms = members(base)
    names = [m["name"] for m in ms]
    d = all_defaults().get(base) or ""
    b = None
    if not origin:
        try:
            import linebase
            b = linebase.get(base)
        except Exception:                    # noqa: BLE001
            b = None
    eff = (effective(base) if (ms or b) else {"name": name, "why": "none"})
    return {
        "base": base,
        "origin": origin,
        "groupN": len(ms),
        "default": d if d in names else "",
        "effective": eff.get("name") or "",
        "effectiveWhy": eff.get("why") or "none",
        "isDefault": bool(d) and d == name,
        "isEffective": (eff.get("name") == name),
        # 基本线路（整理台定稿、全面接管的那条）；有它时 effectiveWhy=linebase
        "hasBase": bool(b),
        "baseInfo": {"id": b["id"], "nStop": len(b["stops"]),
                     "totalKm": b.get("total_km"),
                     "geomSrc": b.get("geom_src") or "",
                     "geomName": b.get("geom_name") or "",
                     "covers": [c for c in (b.get("covers") or "").split(",") if c]}
                     if b else None,
        "members": [dict(m, conflictsBase=bool(b) and m["name"] == b["name"])
                    for m in ms] if len(ms) > 1 else [],
    }
