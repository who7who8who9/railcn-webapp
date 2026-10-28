# -*- coding: utf-8 -*-
"""
curate.py — 数据校对（补全经由 / 里程 / 坐标）

扫一遍库里所有车次，把这三类问题标出来存进 `curate` 表：

  via    经由识别不全（有区间认不出线路，或整趟都认不出）
  km     里程只能按直线距离估算（没有线路里程可依）
  coord  有经停站缺坐标（地图上会画成折线跳点）

页面按问题类型筛选，逐条打开人工补全；补完的会从清单里消失。
扫描是后台任务，仿 collector 的 start/status/stop 三件套。
"""
from __future__ import annotations

import json
import threading
import time
from typing import Dict, List, Optional, Tuple

import railcore

_lock = threading.Lock()
_thread: Optional[threading.Thread] = None

_state: Dict[str, object] = {
    "running": False,
    "done": 0,
    "total": 0,
    "message": "",
    "error": None,
    "started": 0.0,
    "finished": 0.0,
    "stopped": False,
}

FILTERS = {
    "todo": "issues <> ''",
    "all": "1=1",
    "via": "issues LIKE '%via%'",
    "via0": "issues LIKE '%via%' AND via_cov < 1",   # 整趟都没认出线路
    "km": "issues LIKE '%km%'",
    "coord": "issues LIKE '%coord%'",
    "manual": "(via_source='manual' OR km_mode='manual')",
    "clean": "issues = ''",
}

ISSUE_LABELS = {
    "via": "经由待补",
    "km": "里程估算",
    "coord": "缺坐标",
}


# ---------------------------------------------------------------------------
# 单趟车次摘要
# ---------------------------------------------------------------------------

def summarize(code: str, date: str) -> Optional[dict]:
    """算一趟车的校对摘要；算不出来返回 None"""
    try:
        d = railcore.build_route_payload(code, date, cache_only=True, with_path=False)
    except Exception:                                # noqa: BLE001
        return None
    if not d.get("stops"):
        return None

    segs = d.get("viaSegs") or []
    unknown = d.get("viaUnknown") or []
    via_text = " / ".join(d.get("viaLines") or [])

    issues: List[str] = []
    if d.get("viaSource") == "none" or not segs:
        issues.append("via")
    elif unknown:
        issues.append("via")
    if d.get("kmMode") == "estimate":
        issues.append("km")
    missing = d.get("missingCoords") or []
    if missing:
        issues.append("coord")

    return {
        "code": d.get("code") or code,
        "date": date,
        "from_sta": d.get("from"),
        "to_sta": d.get("to"),
        "stops": len(d["stops"]),
        "via_text": via_text,
        "via_json": json.dumps(segs, ensure_ascii=False),
        "via_unknown": json.dumps(unknown, ensure_ascii=False),
        "via_source": d.get("viaSource") or "none",
        "via_cov": float(d.get("viaCoverage") or 0),
        "km_mode": d.get("kmMode") or "estimate",
        "km_matched": d.get("kmMatched") or "0/0",
        "total_km": d.get("totalKm") or 0.0,
        "missing": len(missing),
        "missing_json": json.dumps(missing, ensure_ascii=False),
        "issues": ",".join(issues),
        "scanned_at": time.time(),
    }


def _upsert(conn, r: dict) -> None:
    conn.execute(
        """INSERT INTO curate(code, date, from_sta, to_sta, stops, via_text, via_json,
                              via_unknown, via_source, via_cov, km_mode, km_matched,
                              total_km, missing, missing_json, issues, scanned_at)
           VALUES(:code,:date,:from_sta,:to_sta,:stops,:via_text,:via_json,
                  :via_unknown,:via_source,:via_cov,:km_mode,:km_matched,
                  :total_km,:missing,:missing_json,:issues,:scanned_at)
           ON CONFLICT(code) DO UPDATE SET
             date=excluded.date, from_sta=excluded.from_sta, to_sta=excluded.to_sta,
             stops=excluded.stops, via_text=excluded.via_text, via_json=excluded.via_json,
             via_unknown=excluded.via_unknown, via_source=excluded.via_source,
             via_cov=excluded.via_cov, km_mode=excluded.km_mode,
             km_matched=excluded.km_matched, total_km=excluded.total_km,
             missing=excluded.missing, missing_json=excluded.missing_json,
             issues=excluded.issues, scanned_at=excluded.scanned_at""",
        r,
    )


def refresh_one(code: str) -> Optional[dict]:
    """人工保存后只重算当前车次，不惊动整张表"""
    code = (code or "").strip().upper()
    if not code:
        return None
    with railcore.db() as conn:
        row = conn.execute(
            "SELECT date FROM curate WHERE code=?", (code,)
        ).fetchone()
        if not row:
            row = conn.execute(
                "SELECT MAX(date) AS date FROM routes WHERE code=? AND raw IS NOT NULL",
                (code,),
            ).fetchone()
        date = row["date"] if row else None
        if not date:
            return None
        s = summarize(code, date)
        if s:
            _upsert(conn, s)
            conn.commit()
        return s


def refresh(code: str) -> dict:
    """
    线路库动过（新增 / 改了站序里程）以后，按新线路重算一趟车的自动经由。

    页面上的「重算自动经由」按钮走这里：
      1. railcore.reload_library() 把自建线路 / 车站重新灌进 cnrail（含坐标）
      2. detail() 重算自动基线 —— 新线路会立刻参与「里程配对 / 站序」的判定
      3. 顺手把 curate 表里这趟车的摘要刷新掉，清单上不再是旧结果
    """
    lib = railcore.reload_library()
    code = (code or "").strip().upper()
    out = detail(code)
    if out.get("exists"):
        try:
            s = refresh_one(code)
            if s:
                out["summary"] = s
        except Exception:                            # noqa: BLE001
            pass
    out["library"] = lib
    out["refreshedAt"] = time.time()
    return out


#: 一个缺口最多给几条候选线路
MAX_GAP_CANDS = 8


def gap_candidates(stops: List[str], gaps: List[dict], segs: List[dict]) -> List[dict]:
    """
    每个缺口「点选加入」用的候选线路。

    排序有讲究：先说缺口**前后已识别段**用的线路（缺口多半就是这条线中间断了
    一段，接着填最常见的答案），再补缺口两端车站自己所在的线路（线路表里缺了
    连接段的时候，两站永远不共线，只报共线等于什么都不给）。
    """
    gaps = gaps or []
    segs = segs or []
    out: List[dict] = []
    for g in gaps:
        a, b = g.get("from"), g.get("to")
        cands: List[dict] = []
        seen = set()

        def push(item: dict) -> None:
            nm = item.get("line")
            if not nm or nm in seen:
                return
            seen.add(nm)
            cands.append(item)

        for s in segs:                       # 与缺口首尾相接的已识别段
            if s.get("to") == a or s.get("from") == b:
                push({"line": s.get("line"), "src": "neighbor", "srcCn": "相邻段同线",
                      "custom": None, "totalKm": None})
        try:
            for l in railcore.cand_lines(a or "", b or ""):
                # 覆盖这趟车经停站越多的线，越可能就是这一段该走的线
                l["cover"] = railcore.line_cover(l["line"], stops)
                push(l)
        except Exception:                    # noqa: BLE001
            pass
        # 「相邻段同线」永远最先（缺口就是那条线中间断的），其余按覆盖站数排
        cands.sort(key=lambda x: (x.get("src") != "neighbor", -(x.get("cover") or 0)))
        out.append({"from": a, "to": b, "i0": g.get("i0"),
                    "lines": cands[:MAX_GAP_CANDS]})
    return out



def _sibling_codes(from_sta: str, to_sta: str, code: str,
                   exclude_manual: bool = False) -> List[str]:
    """同起讫站 + 同车种的车次（不含自己）：人工校正经由后它们会自动沿用"""
    if not from_sta or not to_sta:
        return []
    cat = railcore.train_class(code)
    try:
        with railcore.db() as conn:
            rows = conn.execute(
                "SELECT code FROM curate WHERE from_sta=? AND to_sta=?",
                (from_sta, to_sta),
            ).fetchall()
    except Exception:                                # noqa: BLE001
        return []
    out = [r["code"] for r in rows
           if r["code"] != code and railcore.train_class(r["code"]) == cat]
    if exclude_manual:
        keep = []
        for c in out:
            ov = railcore.get_override(c) or {}
            if not ov.get("via"):
                keep.append(c)
        out = keep
    return sorted(out)


def refresh_class(code: str) -> int:
    """人工保存经由后，把同类车次的摘要一起重算 —— 它们读的时候就会自动沿用"""
    code = (code or "").strip().upper()
    if not code:
        return 0
    try:
        with railcore.db() as conn:
            row = conn.execute(
                "SELECT from_sta, to_sta FROM curate WHERE code=?", (code,)
            ).fetchone()
    except Exception:                                # noqa: BLE001
        return 0
    if not row or not row["from_sta"] or not row["to_sta"]:
        return 0
    n = 0
    for c in _sibling_codes(row["from_sta"], row["to_sta"], code, exclude_manual=True)[:200]:
        try:
            if refresh_one(c):
                n += 1
        except Exception:                            # noqa: BLE001
            continue
    return n


def refresh_station(name: str) -> int:
    """补录或撤销某站坐标后，把清单里涉及该站的车次重算一遍"""
    name = (name or "").strip()
    if not name:
        return 0

    targets: List[Tuple[str, str]] = []
    try:
        with railcore.db() as conn:
            rows = conn.execute(
                "SELECT code, date FROM curate WHERE missing_json LIKE ?",
                (f"%{name}%",),
            ).fetchall()
            targets = [(r["code"], r["date"]) for r in rows]
            if not targets:
                # 缺失清单里已经没有这个站了（比如刚撤销了坐标），
                # 就从原始经停数据反查：全表 LIKE 约 0.3s，撤销是低频操作，可以接受
                rows2 = conn.execute(
                    "SELECT code, MAX(date) AS date FROM routes "
                    "WHERE raw LIKE ? GROUP BY code",
                    (f'%"station_name": "{name}"%',),
                ).fetchall()
                targets = [(r["code"], r["date"]) for r in rows2]
    except Exception:                                # noqa: BLE001
        return 0

    n = 0
    for code, date in targets:
        if not date:
            continue
        try:
            with railcore.db() as conn:
                s = summarize(code, date)
                if s:
                    _upsert(conn, s)
                    conn.commit()
                    n += 1
        except Exception:                            # noqa: BLE001
            continue
    return n


# ---------------------------------------------------------------------------
# 全量扫描
# ---------------------------------------------------------------------------

def _scan() -> None:
    """先算后写：算的时候不开写事务，攒 40 条再用一个短事务落盘。
    否则一边算一边挂着写锁，用户在页面上点「保存校正」会被锁到超时。"""
    read_conn = None
    try:
        read_conn = railcore.db()
        rows = read_conn.execute(
            "SELECT code, MAX(date) AS d FROM routes WHERE raw IS NOT NULL GROUP BY code"
        ).fetchall()
        total = len(rows)
        read_conn.close()
        read_conn = None
        with _lock:
            _state.update(total=total, done=0, running=True, message="扫描中",
                          error=None, started=time.time(), finished=0.0, stopped=False)

        done = 0
        buf: List[dict] = []
        for r in rows:
            if _state.get("stopped"):
                break
            s = summarize(r["code"], r["d"])
            if s:
                buf.append(s)
            done += 1
            if len(buf) >= 40:
                write_conn = railcore.db()
                try:
                    for item in buf:
                        _upsert(write_conn, item)
                    write_conn.commit()
                finally:
                    write_conn.close()
                buf.clear()
                with _lock:
                    _state["done"] = done
                    _state["message"] = f"扫描中 {done}/{total}"
        if buf:
            write_conn = railcore.db()
            try:
                for item in buf:
                    _upsert(write_conn, item)
                write_conn.commit()
            finally:
                write_conn.close()
        with _lock:
            _state["done"] = done
            _state["message"] = f"已停止（{done}/{total}）" if _state.get("stopped") else "完成"
    except Exception as exc:                          # noqa: BLE001
        with _lock:
            _state["error"] = f"{type(exc).__name__}: {exc}"
            _state["message"] = "出错：" + _state["error"]  # type: ignore[operator]
    finally:
        if read_conn is not None:
            try:
                read_conn.close()
            except Exception:                        # noqa: BLE001
                pass
        with _lock:
            _state.update(running=False, finished=time.time())


def start() -> dict:
    global _thread
    if _state.get("running"):
        return {"ok": False, "message": "扫描已在运行", **status()}
    with _lock:                       # 先置位，避免接口返回时还没起来看起来像没启动
        _state.update(running=True, done=0, total=0, message="正在准备扫描",
                      error=None, stopped=False, started=time.time(), finished=0.0)
    _thread = threading.Thread(target=_scan, name="curate-scan", daemon=True)
    _thread.start()
    time.sleep(0.05)
    return {"ok": True, **status()}


def stop() -> dict:
    with _lock:
        _state["stopped"] = True
        _state["message"] = "正在停止…"
    return status()


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def counts() -> dict:
    out = {"scanned": 0, "todo": 0, "via": 0, "via0": 0, "km": 0, "coord": 0,
           "manual": 0, "clean": 0, "lastScan": None}
    try:
        with railcore.db() as conn:
            for key, sql in (
                ("scanned", "SELECT COUNT(*) FROM curate"),
                ("todo", "SELECT COUNT(*) FROM curate WHERE issues <> ''"),
                ("via", "SELECT COUNT(*) FROM curate WHERE issues LIKE '%via%'"),
                ("via0", "SELECT COUNT(*) FROM curate WHERE issues LIKE '%via%' AND via_cov < 1"),
                ("km", "SELECT COUNT(*) FROM curate WHERE issues LIKE '%km%'"),
                ("coord", "SELECT COUNT(*) FROM curate WHERE issues LIKE '%coord%'"),
                ("manual", "SELECT COUNT(*) FROM curate WHERE via_source='manual' OR km_mode='manual'"),
                ("clean", "SELECT COUNT(*) FROM curate WHERE issues = ''"),
                ("lastScan", "SELECT MAX(scanned_at) FROM curate"),
            ):
                v = conn.execute(sql).fetchone()[0]
                out[key] = v
    except Exception:                                # noqa: BLE001
        pass
    if not out["scanned"]:
        out["lastScan"] = None
    return out


def status() -> dict:
    with _lock:
        s = dict(_state)
    s["counts"] = counts()
    return s


def listing(mode: str = "todo", q: str = "", limit: int = 60, offset: int = 0) -> dict:
    where = FILTERS.get(mode or "todo", FILTERS["todo"])
    args: List[object] = []
    if q:
        like = f"%{q.strip()}%"
        where += " AND (code LIKE ? OR from_sta LIKE ? OR to_sta LIKE ? OR via_text LIKE ?)"
        args += [like, like, like, like]
    try:
        with railcore.db() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM curate WHERE {where}", args).fetchone()[0]
            rows = conn.execute(
                f"""SELECT code, date, from_sta, to_sta, stops, via_text, via_source,
                           via_cov, via_unknown, km_mode, km_matched, total_km, missing, issues
                      FROM curate WHERE {where}
                     ORDER BY (issues = '') ASC, (via_source = 'none') DESC,
                              via_cov ASC, code ASC
                     LIMIT ? OFFSET ?""",
                args + [int(limit), int(offset)],
            ).fetchall()
    except Exception:                                # noqa: BLE001
        return {"items": [], "total": 0, "mode": mode}
    items = []
    for r in rows:
        issues = [x for x in (r["issues"] or "").split(",") if x]
        try:
            gaps = len(json.loads(r["via_unknown"] or "[]"))
        except (TypeError, ValueError):
            gaps = 0
        items.append({
            "code": r["code"], "date": r["date"],
            "from": r["from_sta"], "to": r["to_sta"], "stops": r["stops"],
            "via": r["via_text"], "viaSource": r["via_source"],
            "viaCoverage": r["via_cov"], "gaps": gaps,
            "kmMode": r["km_mode"],
            "kmMatched": r["km_matched"], "totalKm": r["total_km"],
            "missing": r["missing"],
            "issues": issues,
            "issueLabels": [ISSUE_LABELS.get(x, x) for x in issues],
        })
    return {"items": items, "total": total, "mode": mode}


def detail(code: str) -> dict:
    """
    打开编辑器时用：**自动推断基线**（不含人工校正）+ 已有校正 + 该车经停站名。
    基线故意绕开 override，这样改错了还能对照原本自动算出来的是什么。
    """
    code = (code or "").strip().upper()
    out = {
        "code": code, "date": None, "from": None, "to": None,
        "auto": {
            "viaText": "", "viaSegs": [], "viaUnknown": [], "viaSource": "none",
            "viaCoverage": 0.0, "kmMode": "estimate", "kmMatched": "0/0", "totalKm": 0.0,
        },
        "override": railcore.get_override(code),
        "stations": [],
        "exists": False,
        "error": None,
    }

    try:
        with railcore.db() as conn:
            row = conn.execute(
                "SELECT date FROM curate WHERE code=?", (code,)
            ).fetchone()
            if row and row["date"]:
                date = row["date"]
            else:
                r2 = conn.execute(
                    "SELECT MAX(date) AS d FROM routes WHERE code=? AND raw IS NOT NULL",
                    (code,),
                ).fetchone()
                date = r2["d"] if r2 else None
    except Exception as exc:                          # noqa: BLE001
        out["error"] = str(exc)
        return out

    if not date:
        out["error"] = "本地库里没有这趟车的经停站数据，先下载该日数据"
        return out

    out["date"] = date
    # 同一车次号可能被调图改得面目全非（不同经由/停站/时刻），而校正按 code 保存，
    # 会同时作用到其他日期的运行 —— 这里给出方案数，让编辑者知道自己在校哪一套。
    try:
        with railcore.db() as conn:
            row = conn.execute(
                """SELECT COUNT(DISTINCT IFNULL(train_no,'')||'|'||IFNULL(start_sta,'')
                          ||'|'||IFNULL(end_sta,'')) n
                   FROM trains WHERE code=?""", (code,)).fetchone()
            out["variants"] = (row["n"] if row else 0) or 0
    except Exception:                                 # noqa: BLE001
        out["variants"] = 0
    try:
        d = railcore.build_route_payload(
            code, date, cache_only=True, with_path=False, use_override=False
        )
    except Exception as exc:                          # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    segs = d.get("viaSegs") or []
    out["exists"] = True
    out["from"] = d.get("from")
    out["to"] = d.get("to")
    out["auto"] = {
        "viaText": " / ".join(d.get("viaLines") or []),
        "viaSegs": segs,
        "viaUnknown": d.get("viaUnknown") or [],
        "viaSource": d.get("viaSource") or "none",
        "viaCoverage": d.get("viaCoverage") or 0.0,
        "kmMode": d.get("kmMode") or "estimate",
        "kmMatched": d.get("kmMatched") or "0/0",
        "totalKm": d.get("totalKm") or 0.0,
    }
    out["stations"] = [s["name"] for s in d.get("stops") or []]
    # 缺口里的候选线路（人工「点选加入」用）：自动认不出来的区间，给出最可能的那几条线
    out["gapCands"] = gap_candidates(out["stations"],
                                     out["auto"].get("viaUnknown") or [],
                                     out["auto"].get("viaSegs") or [])
    stop_rows: List[dict] = []
    for s in d.get("stops") or []:
        ci = railcore.stations.coord_info(s["name"])
        stop_rows.append({
            "name": s["name"], "km": s["km"], "arr": s.get("arr"), "dep": s.get("dep"),
            "lat": ci["lat"], "lon": ci["lon"], "coordSource": ci["source"],
            "baseLat": ci["baseLat"], "baseLon": ci["baseLon"],
        })
    out["stops"] = stop_rows
    out["missingCoords"] = d.get("missingCoords") or []
    out["coordSummary"] = {
        "total": len(stop_rows),
        "missing": sum(1 for s in stop_rows if s["lat"] is None),
        "manual": sum(1 for s in stop_rows if s["coordSource"] == "manual"),
    }

    # ---- 同类沿用：同起讫站 + 同车种的人工经由 ----
    try:
        cls = railcore.get_via_class(out.get("from") or "", out.get("to") or "", code)
    except Exception:                                # noqa: BLE001
        cls = None
    out["classVia"] = ({"from": cls["from"], "to": cls["to"],
                        "category": cls.get("category"), "srcCode": cls.get("srcCode"),
                        "segs": len(cls.get("via") or [])} if cls else None)
    out["classCount"] = len(_sibling_codes(out.get("from") or "", out.get("to") or "", code))
    # 这趟车实际生效的经由（可能来自同类沿用）—— 编辑器拿它做初始值，
    # 否则面板上写着「未识别」、地图上却已经画出正确走向，看着自相矛盾
    try:
        eff = railcore.build_route_payload(code, date, cache_only=True, with_path=False)
        out["effective"] = {"viaSource": eff.get("viaSource"),
                            "viaSegs": eff.get("viaSegs") or [],
                            "viaClass": eff.get("viaClass")}
    except Exception:                                # noqa: BLE001
        out["effective"] = {"viaSource": "none", "viaSegs": [], "viaClass": None}
    return out
