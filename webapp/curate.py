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

import confvia
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
    # 自动推断已经认全、但还没人工确认 —— 这类不写进正式库，只作参考
    "pending": "via_source='auto'",
    # 已校正＝进了正式库的（人工逐趟填的 + 从同起讫点车次同步过来的），后者的 via_origin='sync'
    "manual": "(via_source='manual' OR km_mode='manual')",
    # 只是「已校正」的一个子集，单独列出来方便回看同步进来的那批
    "synced": "via_origin='sync'",
    "clean": "issues = ''",
}

ISSUE_LABELS = {
    "via": "经由待补",
    "km": "里程估算",
    "coord": "缺坐标",
}


# 2026 车次编排改革（docs/renumber-2026.md）：「待校对」只认新车次，老车次不进列表。
# 配置（页面上两个勾选框 / scripts/renumber_cutover.py 写）：
#   renum_cutoff        切换日 YYYY-MM-DD。**有值 = 已切到新口径**，老车次全部出范围
#   renum_phase1        '1' = 阶段一已切换（普速 V/N/Z/Y 纳入校对）
#   renum_phase2        '1' = 阶段二已切换（高速/动集/城际/市郊 G/T/K/D/C/S/X 纳入）
# 两个都没勾 = 一律不校（等新号出现）；renum_cutoff 空 = 完全不过滤（改号前的老行为）。
# 注意别只拿 curate.date 卡切换日 —— 它是扫描时的参考日期，这里只在切换日之后
# 有新号数据时才有意义，所以是「>= 切换日」而不是「= 某一天」。

PHASE1_LETTERS = ["V", "N", "Z", "Y"]
PHASE2_LETTERS = ["X", "G", "T", "K", "D", "C", "S"]


def renum_state() -> dict:
    """当前改号口径（页面上两个勾选框就绑这个）"""
    cut = (railcore.cfg_get("renum_cutoff") or "").strip()
    p1 = (railcore.cfg_get("renum_phase1") or "").strip() == "1"
    p2 = (railcore.cfg_get("renum_phase2") or "").strip() == "1"
    letters = (PHASE1_LETTERS if p1 else []) + (PHASE2_LETTERS if p2 else [])
    return {"enabled": bool(cut), "cutoff": cut, "phase1": p1, "phase2": p2,
            "letters": letters}


def set_renum(phase1: Optional[bool] = None, phase2: Optional[bool] = None,
              cutoff: Optional[str] = None) -> dict:
    """勾选/取消阶段（页面接口）。切换日不传就不动。"""
    if cutoff is not None:
        railcore.cfg_set("renum_cutoff", str(cutoff).strip())
    if phase1 is not None:
        railcore.cfg_set("renum_phase1", "1" if phase1 else "0")
    if phase2 is not None:
        railcore.cfg_set("renum_phase2", "1" if phase2 else "0")
    return renum_state()


def _era() -> Tuple[str, List[str], bool]:
    st = renum_state()
    return st["cutoff"], st["letters"], st["enabled"]


def _era_where() -> Tuple[str, List[object]]:
    """给 curate 表查询用：切换日之后的、勾选阶段的字头才在列表里"""
    cut, letters, on = _era()
    if not on:
        return "", []
    if not letters:                     # 已切新口径但一个阶段都没勾 = 暂时不校任何车次
        return " AND 1=0", []
    return (" AND COALESCE(date,'') >= ? AND upper(substr(code,1,1)) IN (%s)"
            % ",".join("?" * len(letters)), [cut] + letters)


def _era_having() -> Tuple[str, List[object]]:
    """给 routes 的 GROUP BY code 查询用：只认切换日后仍在开行、且是新车次字头的"""
    cut, letters, on = _era()
    if not on:
        return "", []
    if not letters:
        return " HAVING 1=0", []
    return (" HAVING MAX(date) >= ? AND upper(substr(code,1,1)) IN (%s)"
            % ",".join("?" * len(letters)), [cut] + letters)


# ---------------------------------------------------------------------------
# 单趟车次摘要
# ---------------------------------------------------------------------------

def _seg_line_names(segs: List[dict]) -> List[str]:
    """经由里用到的线名，按首次出现去重（清单里「京广线 / 陇海线」用）"""
    out: List[str] = []
    for s in segs or []:
        nm = (s.get("line") or "").strip()
        if nm and nm not in out:
            out.append(nm)
    return out


def summarize(code: str, date: str) -> Optional[dict]:
    """算一趟车的校对摘要；算不出来返回 None"""
    try:
        d = railcore.build_route_payload(code, date, cache_only=True, with_path=False)
    except Exception:                                # noqa: BLE001
        return None
    if not d.get("stops"):
        return None

    # 经由口径：人工校正原样照抄；自动推断一律按**整理台确认线路**重算一遍
    # （见 confvia.py）。线路库里那些来源版本 / 没确认的线不参与识别 —— 认不出
    # 的区间就是缺口，等人工补，等整理台确认了那条线自己会认出来。
    manual_via = d.get("viaSource") == "manual"
    if manual_via:
        segs = d.get("viaSegs") or []
        unknown = d.get("viaUnknown") or []
        via_text = " / ".join(d.get("viaLines") or [])
        via_cov = float(d.get("viaCoverage") or 0)
        via_source = "manual"
    else:
        cv = confvia.infer(d.get("stops") or [])
        segs = cv["segs"]
        unknown = cv["unknown"]
        via_text = " / ".join(_seg_line_names(segs))
        via_cov = float(cv["coverage"] or 0)
        via_source = "auto" if segs else "none"

    issues: List[str] = []
    if via_source == "none" or not segs:
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
        "via_source": via_source,
        "via_origin": ((d.get("manual") or {}).get("source") or "") if manual_via else "",
        "via_cov": via_cov,
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
                              via_unknown, via_source, via_origin, via_cov, km_mode, km_matched,
                              total_km, missing, missing_json, issues, scanned_at)
           VALUES(:code,:date,:from_sta,:to_sta,:stops,:via_text,:via_json,
                  :via_unknown,:via_source,:via_origin,:via_cov,:km_mode,:km_matched,
                  :total_km,:missing,:missing_json,:issues,:scanned_at)
           ON CONFLICT(code) DO UPDATE SET
             date=excluded.date, from_sta=excluded.from_sta, to_sta=excluded.to_sta,
             stops=excluded.stops, via_text=excluded.via_text, via_json=excluded.via_json,
             via_unknown=excluded.via_unknown, via_source=excluded.via_source,
             via_origin=excluded.via_origin, via_cov=excluded.via_cov,
             km_mode=excluded.km_mode,
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
    每个缺口「点选加入」用的候选线路 —— **只从整理台确认的线路里挑**（confvia）。

    排序有讲究：先说缺口**前后已识别段**用的线路（缺口多半就是这条线中间断了
    一段，接着填最常见的答案），再补两站都在确认线路站序里的线（两站在站序里
    隔得越近越可能），最后才是只含一头的线。
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
            for l in confvia.cands(a or "", b or "", limit=MAX_GAP_CANDS * 2):
                # 覆盖这趟车经停站越多的线，越可能就是这一段该走的线
                l["cover"] = confvia.cover_of(l["line"], stops)
                push(l)
        except Exception:                    # noqa: BLE001
            pass
        # 「相邻段同线」永远最先（缺口就是那条线中间断的），其余按覆盖站数排
        cands.sort(key=lambda x: (x.get("src") != "neighbor", -(x.get("cover") or 0)))
        out.append({"from": a, "to": b, "i0": g.get("i0"),
                    "lines": cands[:MAX_GAP_CANDS]})
    return out



def od_mates(code: str, limit: int = 60, via: Optional[List[dict]] = None) -> dict:
    """
    同起讫点的**其它车次**，连同停站概况一起列出来，供人工勾选后同步经由。

    以前是「同起讫站 + 同车种自动沿用」，看着省事，但同 OD 的车走法未必一样，
    悄悄套过去反而把数据改坏。现在只列出候选，勾哪趟由人定，同步下去的也各存
    一条自己的校正（和车次号绑定）。

    `via` 给来源车次当前的经由：算一下对方的每一站是不是都在这条经由走过的路上
    （`viaFits=True` 的那几趟，同步时原样照抄就行，不必重新落站）。

    已经有自己校正的车次（人工填的、从别处同步来的）不再进候选，只把趟数记在
    `hidden` 里，供界面说一句「还有 N 趟已校正」。
    """
    code = (code or "").strip().upper()
    out: Dict[str, object] = {"from": None, "to": None, "items": [], "total": 0, "hidden": 0}
    segs = [s for s in (via or []) if isinstance(s, dict) and s.get("line")]
    span_cache: Dict[str, Optional[List[str]]] = {}
    if not code:
        return out
    try:
        with railcore.db() as conn:
            me = conn.execute(
                "SELECT from_sta, to_sta, date FROM curate WHERE code=?", (code,)
            ).fetchone()
            if not me or not me["from_sta"] or not me["to_sta"]:
                return out
            rows = conn.execute(
                """SELECT code, date, stops, via_text, via_source, via_origin, via_cov
                     FROM curate WHERE from_sta=? AND to_sta=? AND code<>?""",
                (me["from_sta"], me["to_sta"], code),
            ).fetchall()
    except Exception:                                # noqa: BLE001
        return out

    out["from"], out["to"] = me["from_sta"], me["to_sta"]
    mine = railcore.route_stops_brief(code, me["date"])
    mine_names = [s["name"] for s in mine.get("stops") or []]
    mine_set = set(mine_names)
    labels = railcore.code_infos([r["code"] for r in rows])
    ov_map = railcore.override_map()

    items: List[dict] = []
    hidden = 0
    for r in rows:
        ov = ov_map.get(r["code"]) or {}
        # 已经有自己校正的（人工的、同步来的都算）不再列出来：它已经定稿了，
        # 再摆在这里只会被误勾一次、被覆盖一次。数量单独报给前端做说明。
        if ov.get("via"):
            hidden += 1
            continue
        brief = railcore.route_stops_brief(r["code"])
        names = [s["name"] for s in brief.get("stops") or []]
        info = labels.get(r["code"]) or {}
        shared = len(mine_set & set(names)) if mine_set else 0
        # 它的每一站都在这条经由走过的路上 → 同步时原样照抄，不用重新落站
        fits = railcore.via_covers_stops(segs, brief.get("stops") or [], span_cache) if segs else None
        items.append({
            "code": r["code"],
            "codeLabel": info.get("codeLabel") or r["code"],
            "codes": info.get("codes") or [r["code"]],
            "date": brief.get("date") or r["date"],
            "trainClass": railcore.train_class(r["code"]),
            "stops": len(names),
            "stopsText": " · ".join(names),
            "startTime": (brief.get("stops") or [{}])[0].get("dep") if brief.get("stops") else None,
            "endTime": (brief.get("stops") or [{}])[-1].get("arr") if brief.get("stops") else None,
            "via": r["via_text"] or "",
            "viaSource": r["via_source"],
            "viaCoverage": r["via_cov"],
            "overlap": round(shared / len(mine_set) * 100) if mine_set else 0,
            "canCompare": bool(mine_set),           # 本车自己有没有站序：没有就算不了重合度
            "sameStops": bool(names) and names == mine_names,
            "stopsMissing": not names,
            "viaFits": bool(fits),                  # 停站全在来源经由里：同步即原样照抄
        })
    # 站序一模一样的排最前（套过去最准），其次按重合站数
    items.sort(key=lambda x: (not x["sameStops"], -x["overlap"], x["code"]))
    out["total"] = len(items)
    out["hidden"] = hidden                          # 同起讫点里已完成校正、没列出来的趟数
    out["items"] = items[:max(1, int(limit))]
    return out


def mate_detail(code: str) -> dict:
    """勾选前的「停站详情」：直接读 12306 缓存，含到发时刻与换号点"""
    code = (code or "").strip().upper()
    brief = railcore.route_stops_brief(code)
    info = railcore.code_info(code)
    ov = railcore.get_override(code) or {}
    return {
        "code": code,
        "codeLabel": info.get("codeLabel") or code,
        "codes": info.get("codes") or [code],
        "date": brief.get("date"),
        "from": brief.get("from"),
        "to": brief.get("to"),
        "stops": brief.get("stops") or [],
        "override": {"via": len(ov.get("via") or []), "source": ov.get("source"),
                     "basedDate": ov.get("basedDate"), "note": ov.get("note")} if ov else None,
    }


def refresh_station(name: str) -> int:
    """补录或撤销某站坐标后，把清单里涉及该站的车次重算一遍"""
    name = (name or "").strip()
    if not name:
        return 0

    targets: List[Tuple[str, str]] = []
    try:
        with railcore.db() as conn:
            ew, eargs = _era_where()
            rows = conn.execute(
                "SELECT code, date FROM curate WHERE missing_json LIKE ?" + ew,
                [f"%{name}%"] + eargs,
            ).fetchall()
            targets = [(r["code"], r["date"]) for r in rows]
            if not targets:
                # 缺失清单里已经没有这个站了（比如刚撤销了坐标），
                # 就从原始经停数据反查：全表 LIKE 约 0.3s，撤销是低频操作，可以接受
                hav, hargs = _era_having()
                rows2 = conn.execute(
                    "SELECT code, MAX(date) AS date FROM routes "
                    "WHERE raw LIKE ? GROUP BY code" + hav,
                    [f'%"station_name": "{name}"%'] + hargs,
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
        hav, hargs = _era_having()
        rows = read_conn.execute(
            "SELECT code, MAX(date) AS d FROM routes WHERE raw IS NOT NULL GROUP BY code" + hav,
            hargs,
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
           "manual": 0, "pending": 0, "synced": 0, "clean": 0, "lastScan": None}
    try:
        with railcore.db() as conn:
            ew, eargs = _era_where()
            for key, sql in (
                ("scanned", "SELECT COUNT(*) FROM curate WHERE 1=1"),
                ("todo", "SELECT COUNT(*) FROM curate WHERE issues <> ''"),
                ("via", "SELECT COUNT(*) FROM curate WHERE issues LIKE '%via%'"),
                ("via0", "SELECT COUNT(*) FROM curate WHERE issues LIKE '%via%' AND via_cov < 1"),
                ("km", "SELECT COUNT(*) FROM curate WHERE issues LIKE '%km%'"),
                ("coord", "SELECT COUNT(*) FROM curate WHERE issues LIKE '%coord%'"),
                ("manual", "SELECT COUNT(*) FROM curate WHERE via_source='manual' OR km_mode='manual'"),
                ("pending", "SELECT COUNT(*) FROM curate WHERE via_source='auto'"),
                # 同步入库也算已校正，这里是它其中的一部分，不是并列的另一个数
                ("synced", "SELECT COUNT(*) FROM curate WHERE via_origin='sync'"),
                ("clean", "SELECT COUNT(*) FROM curate WHERE issues = ''"),
                ("lastScan", "SELECT MAX(scanned_at) FROM curate"),
            ):
                if key == "lastScan":
                    v = conn.execute(sql).fetchone()[0]
                else:
                    v = conn.execute(sql + ew, eargs).fetchone()[0]
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
    ew, eargs = _era_where()
    where = FILTERS.get(mode or "todo", FILTERS["todo"]) + ew
    args: List[object] = list(eargs)
    if q:
        like = f"%{q.strip()}%"
        where += " AND (code LIKE ? OR from_sta LIKE ? OR to_sta LIKE ? OR via_text LIKE ?)"
        args += [like, like, like, like]
    try:
        with railcore.db() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM curate WHERE {where}", args).fetchone()[0]
            rows = conn.execute(
                f"""SELECT code, date, from_sta, to_sta, stops, via_text, via_source,
                           via_origin, via_cov, via_unknown, km_mode, km_matched, total_km,
                           missing, issues
                      FROM curate WHERE {where}
                     ORDER BY (issues = '') ASC, (via_source = 'none') DESC,
                              via_cov ASC, code ASC
                     LIMIT ? OFFSET ?""",
                args + [int(limit), int(offset)],
            ).fetchall()
    except Exception:                                # noqa: BLE001
        return {"items": [], "total": 0, "mode": mode}
    # 同一趟车换向会换车次号（4355/4358）：两行都挂同一个写法，免得当成两趟车各校一遍
    labels = railcore.code_infos([r["code"] for r in rows])
    items = []
    for r in rows:
        issues = [x for x in (r["issues"] or "").split(",") if x]
        try:
            gaps = len(json.loads(r["via_unknown"] or "[]"))
        except (TypeError, ValueError):
            gaps = 0
        info = labels.get(r["code"]) or {}
        items.append({
            "code": r["code"], "date": r["date"],
            "codeLabel": info.get("codeLabel") or r["code"],
            "codes": info.get("codes") or [r["code"]],
            "from": r["from_sta"], "to": r["to_sta"], "stops": r["stops"],
            "via": r["via_text"], "viaSource": r["via_source"],
            "viaOrigin": r["via_origin"] or "",
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

    # 自动基线也按整理台确认线路重算（detial 的 payload 本身就绕开了 override，
    # 这里再把线路口径换掉即可）：没确认的线不认、认不出就是缺口
    cv = confvia.infer(d.get("stops") or [])
    segs = cv["segs"]
    out["exists"] = True
    out["from"] = d.get("from")
    out["to"] = d.get("to")
    out["auto"] = {
        "viaText": " / ".join(_seg_line_names(segs)),
        "viaSegs": segs,
        "viaUnknown": cv["unknown"],
        "viaSource": "auto" if segs else "none",
        "viaCoverage": cv["coverage"],
        "kmMode": d.get("kmMode") or "estimate",
        "kmMatched": d.get("kmMatched") or "0/0",
        "totalKm": d.get("totalKm") or 0.0,
    }
    out["stations"] = [s["name"] for s in d.get("stops") or []]
    # 同一趟车换向换号（4355/4358）：编辑器标题也用合并写法
    info = railcore.code_info(code)
    out["codeLabel"] = info.get("codeLabel") or code
    out["codes"] = info.get("codes") or [code]
    out["codeSwitches"] = d.get("codeSwitches") or []
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

    # ---- 同起讫点的其它车次（人工勾选后同步经由，不再自动沿用）----
    try:
        out["odMates"] = od_mates(code, via=(out.get("override") or {}).get("via") or [])
    except Exception:                                # noqa: BLE001
        out["odMates"] = {"from": out.get("from"), "to": out.get("to"),
                          "items": [], "total": 0, "hidden": 0}

    # ---- 校正和「校正时的站表」绑定：调图换了站序就提醒重校 ----
    ov = out.get("override") or {}
    stale = {"changed": False, "basedDate": ov.get("basedDate") or "",
             "currentDate": date, "missing": [], "added": []}
    if ov.get("via") and stale["basedDate"]:
        old = railcore.route_stops_brief(code, stale["basedDate"])
        old_names = [s["name"] for s in old.get("stops") or []]
        new_names = out["stations"]
        # 依据那天本身没抓到站序（拿到的是相邻某天的）就不比：差一天不代表调图
        if old_names and old.get("dateExact", True) and old_names != new_names:
            stale.update(changed=True,
                         missing=[n for n in old_names if n not in new_names],
                         added=[n for n in new_names if n not in old_names])
    out["overrideStale"] = stale

    # 这趟车实际生效的经由（人工校正优先，否则就是上面按确认线路算出来的自动基线）
    try:
        eff = railcore.build_route_payload(code, date, cache_only=True, with_path=False)
        if eff.get("viaSource") == "manual":
            out["effective"] = {"viaSource": "manual",
                                "viaSegs": eff.get("viaSegs") or [],
                                "viaConfirmed": True}
        else:
            out["effective"] = {"viaSource": out["auto"]["viaSource"],
                                "viaSegs": out["auto"]["viaSegs"],
                                "viaConfirmed": False}
    except Exception:                                # noqa: BLE001
        out["effective"] = {"viaSource": "none", "viaSegs": [], "viaConfirmed": False}
    return out
