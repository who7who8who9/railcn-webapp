# -*- coding: utf-8 -*-
"""整理台「全部确认」线路 —— 主站「车站/线路」页唯一可见的线路清单（只读）

2026-11-06 用户要求：车站/线路里能查到的线路，**只有**在线路整理台完成全部确认的
那几条；底表 / 资料库 / china-emu / OSM / 备份导回来的来源版本、候选线路、
「选一条来源版本」的 ★ 都不再显示。确认线路必须自带**停站**和**线路路线位置**。

判定「全部确认」（四条全满足才算）：
  1. `ld_line.status = 'confirmed'`（③ 合并保存走完）
  2. `line_ok = 1` 且 `stop_ok = 1`（① 走向来源、② 站点来源各自确认过）
  3. `ld_stop` 至少 2 站（站点 + 定位）
  4. 能取到真实走向（`ld_geom` 的多段，或 geom_src 指向的来源包 / GeoJSON / OSM）

数据全部从 `linedesk.db` 现读：整理台里改了站序、补了坐标、重配了走向，主站
立刻跟着变。**不往 rail.db 写任何东西**，也不碰 `line_custom` / `line_base`。
主站的 `/api/line?q=…&confirmed=1`、`/api/lines?confirmed=1`、
`/api/line/osm?confirmed=1` 三个出口走这里；不带 confirmed 的老调用完全不受影响
（数据校对、列车地图、备份面板等仍按原来的口径取数）。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from typing import Optional

import linedesk

# 指纹缓存：整理台是独立库，脚本批量导入不会通知本模块。
# 指纹包含三张表，改站序 / 补走向 / 改状态都能让缓存立刻失效。
_LOCK = threading.RLock()
_CACHE: dict = {"fp": None, "ts": 0.0, "rows": None}
_TTL = 2.0

_LIST_SQL = """
SELECT l.id, l.name, l.family, l.kind, l.total_km, l.from_sta, l.to_sta,
       l.note, l.src_line, l.src_stop, l.geom_src, l.geom_name,
       l.created_at, l.updated_at,
       (SELECT COUNT(*) FROM ld_stop s WHERE s.line_id=l.id) AS n_stop,
       (SELECT COUNT(*) FROM ld_stop s
         WHERE s.line_id=l.id AND s.lat IS NOT NULL AND s.lon IS NOT NULL) AS n_coord,
       (SELECT COUNT(*) FROM ld_geom g
         WHERE g.line_id=l.id AND (g.parts_gz IS NOT NULL OR g.spine_gz IS NOT NULL)) AS n_geom,
       (SELECT MAX(g.n_pt) FROM ld_geom g WHERE g.line_id=l.id) AS n_pt,
       (SELECT s.km FROM ld_stop s WHERE s.line_id=l.id
         ORDER BY s.seq DESC LIMIT 1) AS last_km
  FROM ld_line l
 WHERE l.status='confirmed' AND l.line_ok=1 AND l.stop_ok=1
 ORDER BY l.id
"""


def _fp(c: sqlite3.Connection) -> tuple:
    def one(sql: str):
        try:
            return c.execute(sql).fetchone()
        except sqlite3.DatabaseError:
            return None
    a = one("SELECT COUNT(*),MAX(updated_at) FROM ld_line") or (0, "")
    b = one("SELECT COUNT(*) FROM ld_stop") or (0,)
    d = one("SELECT COUNT(*),MAX(created_at) FROM ld_geom") or (0, "")
    # ③ 合并保存会写 ld_log；收录它，新确认的线路才不用等缓存过期就能排到最上面
    e = one("SELECT COUNT(*),MAX(id) FROM ld_log") or (0, 0)
    f = one("SELECT COUNT(*) FROM ld_alias") or (0,)
    return (a[0], a[1], b[0], d[0], d[1], e[0], e[1], f[0])


def _rows() -> list:
    with _LOCK:
        now = time.time()
        c = linedesk.conn()
        try:
            fp = _fp(c)
            if _CACHE["rows"] is None or fp != _CACHE["fp"] or now - _CACHE["ts"] > 30:
                _CACHE["rows"] = [dict(r) for r in c.execute(_LIST_SQL)]
                _CACHE["fp"] = fp
                _CACHE["ts"] = now
            elif now - _CACHE["ts"] > _TTL:
                # TTL 内直接复用；过了 TTL 但指纹没变也只需把时间往前推
                _CACHE["ts"] = now
            return [dict(r) for r in _CACHE["rows"]]
        finally:
            c.close()


def invalidate() -> None:
    """整理台有写入后主动打点（其实指纹已经能兜住，这里给脚本/测试一个显式入口）"""
    with _LOCK:
        _CACHE["rows"] = None
        _CACHE["fp"] = None
        _CACHE["ts"] = 0.0


def _has_geom(r: dict) -> bool:
    """要么合并/导入时落了 ld_geom 多段，要么 geom_src 真能解析出走向折线"""
    if r.get("n_geom"):
        return True
    src = str(r.get("geom_src") or "")
    gname = str(r.get("geom_name") or "").strip() or r["name"]
    try:
        if src.startswith("route:"):
            import linebase
            return bool(linebase.route_geom(gname, src.split(":", 1)[1],
                                            with_parts=False))
        if src == "geojson":
            import geoimport
            return bool(geoimport.meta(gname))
        if src == "osm":
            import railgeom
            return bool(railgeom.meta(gname) or railgeom.meta(r["name"]))
    except Exception:                      # noqa: BLE001  旧库 / 表被占用
        return False
    return False


def _complete(r: dict) -> bool:
    return (r.get("n_stop") or 0) >= 2 and _has_geom(r)


def completeness(r: dict) -> dict:
    """给整理台面板标「还差什么」用"""
    missing = []
    if (r.get("n_stop") or 0) < 2:
        missing.append("停站")
    if not _has_geom(r):
        missing.append("线路走向")
    return {"ok": not missing, "missing": missing,
            "nStop": r.get("n_stop") or 0, "nCoord": r.get("n_coord") or 0,
            "nGeomPt": r.get("n_pt") or 0}


def _total_km(r: dict, c: sqlite3.Connection, ov: dict):
    """确认线路的总里程（含 line_override 人工校正），口径与 detail_payload 一致"""
    ks = [x[0] for x in c.execute(
        "SELECT km FROM ld_stop WHERE line_id=? ORDER BY seq", (int(r["id"]),))]
    if ov.get("segKm") or ov.get("staKm") or ov.get("totalKm"):
        try:
            import cnrail
            cum, _ = cnrail._rebuild_km(
                [{"km": k} for k in ks], ov.get("segKm"),
                ov.get("totalKm"), ov.get("staKm"))
            ks = cum
        except Exception:                  # noqa: BLE001
            ov = {}
    last = ks[-1] if ks else None
    return last if last is not None else (ov.get("totalKm") or r["total_km"])


def list_all(q: str = "") -> list:
    """确认线路清单（主站联想 / 搜索用），只返回完成全部确认的那些"""
    kw = str(q or "").strip()
    # 和详情同一个总里程口径（含人工里程校正），别出现「列表 2534 km、
    # 点进去 2690 km」这种两个数。这里只算里程，别走 detail_payload 的
    # 坐标/几何那套（联想框每敲一个字都要用，不能拖到秒级）。
    try:
        import railcore
        ovs = railcore.line_override_map() or {}
    except Exception:                      # noqa: BLE001
        ovs = {}
    c = linedesk.conn()
    try:
        km_cache = {r["id"]: _total_km(r, c, ovs.get(r["name"]) or {})
                    for r in _rows() if _complete(r)}
    finally:
        c.close()
    out = []
    for r in _rows():
        if not _complete(r):
            continue
        if kw and kw not in r["name"] and not _alias_hit(r["id"], kw):
            continue
        total_km = km_cache.get(r["id"])
        out.append({
            "name": r["name"], "ldId": r["id"],
            "n": r["n_stop"], "nStop": r["n_stop"],
            "nCoord": r["n_coord"], "nGeomPt": r["n_pt"] or 0,
            "totalKm": total_km,
            "fromSta": r["from_sta"] or "", "toSta": r["to_sta"] or "",
            "geomSrc": r["geom_src"] or "", "geomName": r["geom_name"] or "",
            "confirmed": True, "base": True,
            "kmComplete": True, "kmManual": False, "color": None,
        })
    # 完全同名的排前面，其余按规范名
    out.sort(key=lambda x: (x["name"] != kw, x["name"]))
    return out


def _confirm_meta(c: sqlite3.Connection) -> tuple:
    """每条线最后一次「③ 合并保存」的时间 / 操作序号 + 已确认别名。

    `ld_line.updated_at` 是「最后改过」，改站序、补坐标都会动它，不能当确认先后。
    真正的确认动作是 `ld_log` 的 `finalize` / `publish`（两条同一秒写出）；
    同一秒里再按日志 id 定先后。target 绝大多数是规范线名，少数旧数据是
    `ld_line.id` 字符串，两种都认。没有日志的老数据由 recent() 回退到 updated_at。
    """
    logs: dict = {}
    try:
        for tgt, ts, rid in c.execute(
                "SELECT target, ts, id FROM ld_log"
                " WHERE op IN ('finalize','publish') ORDER BY id"):
            k = str(tgt or "").strip()
            if not k:
                continue
            prev = logs.get(k)
            if not prev or int(rid or 0) >= prev[1]:
                logs[k] = (str(ts or ""), int(rid or 0))
    except sqlite3.DatabaseError:            # 旧库没有 ld_log / 表被占用
        logs = {}
    aliases: dict = {}
    try:
        for lid, nm in c.execute("SELECT line_id, name FROM ld_alias WHERE ok=1"):
            aliases.setdefault(int(lid), []).append(str(nm))
    except sqlite3.DatabaseError:
        aliases = {}
    return logs, aliases


def recent() -> list:
    """「车站/线路」页下方的**全量**确认清单，按确认先后倒序（最新确认在最上）。

    和 `list_all()` 的区别：list_all 给联想下拉用（同名优先、按名字排、会被
    limit 截断）；这里不截断，附带 `confirmedAt` / `confirmSeq` / `covers`，
    前端拿到后直接在输入框里本地筛选，不用每敲一个字都打一次接口。
    """
    try:
        import railcore
        ovs = railcore.line_override_map() or {}
    except Exception:                        # noqa: BLE001
        ovs = {}
    c = linedesk.conn()
    try:
        rows = [r for r in _rows() if _complete(r)]
        km = {r["id"]: _total_km(r, c, ovs.get(r["name"]) or {}) for r in rows}
        logs, aliases = _confirm_meta(c)
    finally:
        c.close()
    out = []
    for r in rows:
        lid = int(r["id"])
        hit = logs.get(r["name"]) or logs.get(str(lid))
        ts, seq = hit if hit else ("", 0)
        if not ts:
            ts = str(r.get("updated_at") or r.get("created_at") or "")
        out.append({
            "name": r["name"], "ldId": lid,
            "n": r["n_stop"], "nStop": r["n_stop"],
            "nCoord": r["n_coord"], "nGeomPt": r["n_pt"] or 0,
            "totalKm": km.get(lid),
            "fromSta": r["from_sta"] or "", "toSta": r["to_sta"] or "",
            "geomSrc": r["geom_src"] or "", "geomName": r["geom_name"] or "",
            "confirmedAt": ts, "confirmSeq": seq,
            "confirmFrom": "log" if seq else "line",
            "covers": sorted(aliases.get(lid, [])),
            "confirmed": True, "base": True,
        })
    # 时间戳是 YYYY-MM-DD HH:MM:SS，可直接按字符串比；同一秒按日志 id 再按 ld_line.id
    out.sort(key=lambda x: (x["confirmedAt"], x["confirmSeq"], x["ldId"]),
             reverse=True)
    return out


def _alias_hit(lid: int, kw: str) -> bool:
    c = linedesk.conn()
    try:
        return bool(c.execute(
            "SELECT 1 FROM ld_alias WHERE line_id=? AND ok=1 AND name LIKE ? LIMIT 1",
            (lid, "%" + kw + "%")).fetchone())
    except sqlite3.DatabaseError:
        return False
    finally:
        c.close()


def get(name: str) -> Optional[dict]:
    kw = str(name or "").strip()
    if not kw:
        return None
    for r in _rows():
        if r["name"] == kw and _complete(r):
            return r
    return None


def names() -> set:
    return {r["name"] for r in _rows() if _complete(r)}


def stats() -> dict:
    c = linedesk.conn()
    try:
        total = c.execute("SELECT COUNT(*) FROM ld_line").fetchone()[0]
        draft = c.execute(
            "SELECT COUNT(*) FROM ld_line WHERE status<>'confirmed'").fetchone()[0]
    finally:
        c.close()
    rows = [r for r in _rows() if _complete(r)]
    return {"total": total, "draft": draft, "confirmed": len(rows),
            "incomplete": len([r for r in _rows() if not _complete(r)]),
            "lines": [r["name"] for r in rows]}


# ---------------------------------------------------------------------------
# 走向：整理台合并的多段优先，其次定稿时选的来源包 / GeoJSON / OSM
# ---------------------------------------------------------------------------
def _geom(r: dict, name: str, with_parts: bool) -> Optional[dict]:
    src = str(r.get("geom_src") or "")
    gname = str(r.get("geom_name") or "").strip() or name
    # ① 整理台「合并保存」整合好的走向（ld_geom，不回灌 rail.db）
    try:
        g = linedesk.merged_geom(int(r["id"]), name, with_parts=with_parts)
    except Exception:                      # noqa: BLE001  表结构旧 / 库被占用
        g = None
    if g and (not with_parts or g.get("parts")):
        return g
    # ② 明确选了某个来源包自己的走向
    if src.startswith("route:"):
        try:
            import linebase
            g = linebase.route_geom(gname, src.split(":", 1)[1],
                                    with_parts=with_parts)
        except Exception:                  # noqa: BLE001
            g = None
        if g and (not with_parts or g.get("parts")):
            return g
    if not with_parts:
        # 详情卡只要元数据；GeoJSON / OSM 按名字找
        try:
            import geoimport
            m = geoimport.meta(gname) if src == "geojson" else None
        except Exception:                  # noqa: BLE001
            m = None
        if not m and src in ("osm", "geojson", ""):
            try:
                import railgeom
                m = railgeom.meta(gname) or railgeom.meta(name)
            except Exception:              # noqa: BLE001
                m = None
        return m
    # 画地图要折线：GeoJSON 导入 / OSM 走向按名字取
    try:
        import geoimport
        import railgeom
        g = (geoimport.geom(gname) if src == "geojson" else None) \
            or railgeom.geom(gname) or railgeom.geom(name)
    except Exception:                      # noqa: BLE001
        g = None
    return g if g and g.get("parts") else None


def geom_meta(r: dict, name: str) -> Optional[dict]:
    return _geom(r, name, False)


def geom(name: str, with_parts: bool = True) -> Optional[dict]:
    r = get(name)
    if not r:
        # 「车站/线路」页的「真实走向」按钮把详情里的 d.osm.ask 原样传给
        # /api/line/osm?confirmed=1。整理台 ① 选的常常是**来源线名**
        # （济南线 ← 「京沪四线」），确认线路存的却是规范名 + geom_name=来源名；
        # 按规范名查自然查不到，404 后地图只剩站点直连，看起来就是「没有 OSM 走向」。
        # 规范名精确匹配永远优先；查不到时按 geom_name 反查一次，让这类改名收编的
        # 线也能拿到折线。多条共用同一来源名时按规范名排序取第一条，不扩散歧义。
        kw = str(name or "").strip()
        hit = [x for x in _rows()
               if str(x.get("geom_name") or "").strip() == kw
               and x["name"] != kw and _complete(x)]
        r = sorted(hit, key=lambda x: x["name"])[0] if hit else None
    return _geom(r, name, with_parts) if r else None


# ---------------------------------------------------------------------------
# 线路详情：形状对齐 /api/line，前端 lnRenderHead / lnRenderInfo / lnDrawMap 直接用
# ---------------------------------------------------------------------------
def detail_payload(name: str) -> Optional[dict]:
    r = get(name)
    if not r:
        return None
    c = linedesk.conn()
    try:
        raw = [dict(x) for x in c.execute(
            "SELECT seq,name,km,lat,lon,src,note FROM ld_stop"
            " WHERE line_id=? ORDER BY seq", (int(r["id"]),))]
        aliases = [x[0] for x in c.execute(
            "SELECT name FROM ld_alias WHERE line_id=? AND ok=1 ORDER BY name",
            (int(r["id"]),))]
    finally:
        c.close()
    if len(raw) < 2:
        return None
    # 站名出门统一不带「站」：和线路库 / 车站页一个口径
    try:
        import railcore
        canon = railcore.canon_station
    except Exception:                      # noqa: BLE001
        canon = lambda s: s
    stations = [{
        "name": canon(s["name"]), "km": s["km"],
        "lat": s["lat"], "lon": s["lon"],
        "src": s["src"] or "", "note": s["note"] or "",
        "kmBase": s["km"],
    } for s in raw]
    # 人工补录的坐标（车站页改的）优先；整理台自己给的定位是权威，不被档案值覆盖
    for s in stations:
        try:
            ci = railcore.stations.coord_info(s["name"]) or {}
        except Exception:                  # noqa: BLE001
            ci = {}
        if ci.get("source") == "manual" and ci.get("lat") is not None:
            s["lat"], s["lon"], s["coordSrc"] = ci["lat"], ci["lon"], "manual"
    # 「编辑里程」存的人工校正也要认：确认线路是整理台的站序，但里程校正仍在
    # rail.db / line_override，保存后详情要按它重算，不然看着像「保存不正确」
    try:
        ov = (railcore.line_override_map() or {}).get(name) or {}
    except Exception:                      # noqa: BLE001
        ov = {}
    if ov.get("segKm") or ov.get("staKm") or ov.get("totalKm"):
        try:
            import cnrail as _cnrail
            cum, _ = _cnrail._rebuild_km(
                [{"km": s["kmBase"]} for s in stations],
                ov.get("segKm"), ov.get("totalKm"), ov.get("staKm"))
            for s, v in zip(stations, cum):
                s["km"] = v
        except Exception:                  # noqa: BLE001
            ov = {}
    # 总里程口径同 cnrail：末站有累计用末站，末站缺里程才退 ld_line.total_km
    last_km = stations[-1]["km"]
    total_km = last_km if last_km is not None else (ov.get("totalKm") or r["total_km"])
    n_coord = sum(1 for s in stations if s["lat"] is not None and s["lon"] is not None)
    seg = []
    for i in range(len(stations) - 1):
        a, b = stations[i]["km"], stations[i + 1]["km"]
        seg.append(round(float(b) - float(a), 1)
                   if (a is not None and b is not None) else None)
    cp = completeness(r)
    m = geom_meta(r, name)
    return {
        "name": name, "found": True,
        "from": "confirmed", "confirmed": True, "confirmedOnly": True,
        "base": True, "baseId": None, "ldId": int(r["id"]),
        "stations": stations, "n": len(stations),
        "totalKm": total_km,
        "override": {"segKm": ov.get("segKm") or {}, "staKm": ov.get("staKm") or {},
                     "stops": [], "totalKm": ov.get("totalKm"),
                     "note": ov.get("note") or ""} if ov else None,
        "fromSta": r["from_sta"] or "", "toSta": r["to_sta"] or "",
        "geomSrc": r["geom_src"] or "", "geomName": r["geom_name"] or "",
        "covers": aliases, "note": r["note"] or "",
        "coordCoverage": round(100.0 * n_coord / len(stations)),
        "segKm": seg,
        "kmComplete": all(s["km"] is not None for s in stations),
        "kmManual": False, "kmTotalOnly": False,
        "stopsFrom": "linedesk", "stopsManual": True,
        "custom": False, "source": "",
        "srcFix": [], "reversed": False,
        # 下面这几个原来由 /api/line 的 out() 补：确认视图里全部留空 / 关掉，
        # 前端据此不画「来源版本条」「★ 设默认」「OSM 里另有 N 条」等选线控件
        "sources": [], "group": {}, "osmOther": [],
        "askedName": name, "autoFrom": "", "pickedFrom": "",
        "srcPick": "", "effFrom": "",
        "info": None, "kmCheck": None,
        "osm": m,
        "confirm": {
            **cp,
            "lineSrc": r["src_line"] or "", "stopSrc": r["src_stop"] or "",
            "geomLabel": (m or {}).get("file") or "",
            "geomNPart": (m or {}).get("nPart") or 0,
            "geomNPt": (m or {}).get("nPt") or cp["nGeomPt"],
        },
    }
