# -*- coding: utf-8 -*-
"""
linedesk.py — 线路整理台：一座独立的整理库 + 跨来源线名归并

rail.db 里五套来源各写各的名字，同一条线最多四种写法：

    底表按段命名   京哈高速线京沈段 / 京哈高速线沈哈段
    辉睿资料库     京沪高铁
    china-emu      京沪高速铁路
    OSM            京沪高速线
    自建           沪苏湖高铁

所以另开一个库 **webapp/data/linedesk.db**，整理结果只写这里，rail.db 一个字节都不碰：

    ld_line      规范线路（一条线一行，name 是人工定的规范名）
    ld_alias     每个来源的原始写法（source + name 唯一）
    ld_stop      整理后的站序（含坐标、里程）
    ld_relation  分段 ↔ 全线 的父子关系
    ld_log       每次改动的流水（可回溯）

归并只做「名字层面」的确定事：后缀族归一（高铁 = 高速线 = 高速铁路 = 客运专线）、
去掉括号备注、认出「X线Y段」。站序对不对、父子关系对不对，留给页面里人工确认。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from typing import Dict, Iterable, List, Optional, Tuple

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "linedesk.db")
RAIL_DB = os.path.join(BASE_DIR, "data", "rail.db")
LIB_JSON = os.path.join(os.path.dirname(BASE_DIR), "data", "cnrail_lines.json")

# 来源优先级：谁的名字当规范名、谁的站序先信
SRC_ORDER = ["lib", "custom", "huirui", "ce", "osm"]
SRC_LABEL = {
    "lib": "底表",
    "custom": "自建",
    "huirui": "资料库",
    "ce": "china-emu",
    "osm": "OSM",
}
_PRIO = {s: i for i, s in enumerate(SRC_ORDER)}

# 后缀 → 族。长后缀必须先匹配（「高速铁路」不能被「铁路」抢走）
_KINDS: Tuple[Tuple[str, str], ...] = (
    ("高速铁路线", "gs"), ("高速铁路", "gs"), ("高速线", "gs"), ("高铁线", "gs"),
    ("客运专线", "gs"), ("客专线", "gs"),
    ("客专", "gs"), ("高铁", "gs"), ("高速", "gs"),
    ("城际铁路线", "cj"), ("城际铁路", "cj"), ("城际线", "cj"), ("城际", "cj"),
    ("市域铁路", "sj"), ("市域线", "sj"), ("市域", "sj"),
    ("联络线", "lj"),
    ("支线", "zx"), ("专线", "zx"),
    ("铁路", "px"), ("线", "px"), ("通道", "px"),
)
KIND_LABEL = {"gs": "高速", "cj": "城际", "sj": "市域",
              "lj": "联络线", "zx": "支线", "px": "普速", "": "—"}

_SEG_RE = re.compile(r"^(.+?(?:铁路|线))([\u4e00-\u9fa5]{1,8})段$")


# ---------------------------------------------------------------------------
# 库
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ld_line (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,     -- 规范线名（人工可改）
    family      TEXT NOT NULL DEFAULT '', -- 归一键：前缀|族（同族 = 同一条线的不同写法）
    kind        TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'draft',   -- draft / confirmed
    total_km    REAL,
    from_sta    TEXT, to_sta TEXT,
    note        TEXT,
    src_main    TEXT,                     -- 站序初稿取自哪个来源
    created_at  TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS ld_alias (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    line_id INTEGER NOT NULL,
    source  TEXT NOT NULL,
    name    TEXT NOT NULL,
    src_id  TEXT,
    UNIQUE(source, name)
);
CREATE TABLE IF NOT EXISTS ld_stop (
    line_id INTEGER NOT NULL,
    seq     INTEGER NOT NULL,
    name    TEXT NOT NULL,
    km      REAL, lon REAL, lat REAL,
    src     TEXT,
    note    TEXT,
    PRIMARY KEY (line_id, seq)
);
CREATE TABLE IF NOT EXISTS ld_relation (
    parent_id INTEGER NOT NULL,
    child_id  INTEGER NOT NULL,
    kind      TEXT NOT NULL DEFAULT 'seg',   -- seg=分段建成 / fork=支线岔出
    note      TEXT,
    PRIMARY KEY (parent_id, child_id)
);
CREATE TABLE IF NOT EXISTS ld_log (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT, op TEXT, target TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS ld_meta (
    k TEXT PRIMARY KEY, v TEXT
);
CREATE INDEX IF NOT EXISTS ix_alias_line ON ld_alias(line_id);
CREATE INDEX IF NOT EXISTS ix_alias_name ON ld_alias(name);
CREATE INDEX IF NOT EXISTS ix_stop_line  ON ld_stop(line_id, seq);
CREATE INDEX IF NOT EXISTS ix_line_fam   ON ld_line(family);
"""


def conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=8000")
    return c


def init() -> None:
    with conn() as c:
        c.executescript(_SCHEMA)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def log(op: str, target: str = "", detail: str = "") -> None:
    with conn() as c:
        c.execute("INSERT INTO ld_log(ts,op,target,detail) VALUES(?,?,?,?)",
                  (_now(), op, str(target), str(detail)))


# ---------------------------------------------------------------------------
# 线名归一
# ---------------------------------------------------------------------------

def split_seg(name: str) -> Optional[Tuple[str, str]]:
    """「京哈高速线沈哈段」→ ('京哈高速线', '沈哈')；不是分段名返回 None"""
    n = re.sub(r"\s+", "", str(name or ""))
    m = _SEG_RE.match(n)
    if not m:
        return None
    parent, seg = m.group(1), m.group(2)
    return (parent, seg) if parent and seg and parent != seg else None


def normalize(name: str) -> dict:
    """把一条来源线名拆成 (base, kind, seg, note)

    京沪高铁 / 京沪高速线 / 京沪高速铁路 / 京沪高铁（京沪段） → 同一个 base=京沪, kind=gs
    """
    raw = str(name or "").strip()
    n = re.sub(r"\s+", "", raw)
    note = ""
    m = re.search(r"[（(]([^)）]*)[)）]", n)
    if m:
        note = m.group(1).strip()
        n = (n[:m.start()] + n[m.end():]).strip()
    seg = ""
    sp = split_seg(n)
    if sp:
        n, seg = sp
    for suf, kind in _KINDS:
        if n.endswith(suf) and len(n) > len(suf):
            return {"raw": raw, "base": n[: -len(suf)], "kind": kind,
                    "seg": seg, "note": note}
    return {"raw": raw, "base": n, "kind": "", "seg": seg, "note": note}


def family_of(name: str) -> str:
    """归一键：同一族 = 同一条线的不同写法（不分段时）"""
    d = normalize(name)
    return f"{d['base']}|{d['kind']}"


def family_key(d: dict) -> str:
    """用 normalize() 的结果直接算归一键（分段名的族 = 父线的族）"""
    return f"{d['base']}|{d['kind']}"


# OSM 把桥、隧道单独存成一条 element，名字写成「图佳线（第一五林河桥）」
# 有 150 个。它们不是线路的另一种叫法，收进别名只会把「写法数」灌水
_WORK_RE = re.compile(r"[（(][^）)]*(桥|隧道|涵洞|立交)[^）)]*[）)]")


def is_work_name(name: str) -> bool:
    return bool(_WORK_RE.search(str(name or "")))


def sort_key(src: str, n_stop: int) -> Tuple[int, int]:
    return (_PRIO.get(src, 99), -int(n_stop or 0))


# ---------------------------------------------------------------------------
# 收来源
# ---------------------------------------------------------------------------

def _ro(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def _lib_lines() -> List[dict]:
    """底表 417 条（按段命名的原始档案）"""
    if not os.path.exists(LIB_JSON):
        return []
    with open(LIB_JSON, encoding="utf-8") as f:
        raw = json.load(f)
    out = []
    for x in raw:
        stops = [{"name": s.get("name"), "km": s.get("km")}
                 for s in (x.get("stations") or []) if s.get("name")]
        out.append({"source": "lib", "name": x.get("name"), "src_id": x.get("eid") or "",
                    "stops": stops, "total_km": x.get("totalKm")})
    return out


def _huirui_lines() -> List[dict]:
    with _ro(RAIL_DB) as c:
        rows = c.execute("SELECT id,name,length_km FROM line_catalog").fetchall()
        byid: Dict[str, List[dict]] = {}
        for r in c.execute("SELECT lid,seq,name,km FROM line_catalog_stop ORDER BY lid,seq"):
            byid.setdefault(r["lid"], []).append({"name": r["name"], "km": r["km"]})
    return [{"source": "huirui", "name": r["name"], "src_id": r["id"],
             "stops": byid.get(r["id"], []), "total_km": r["length_km"]} for r in rows]


def _ce_lines() -> List[dict]:
    with _ro(RAIL_DB) as c:
        rows = c.execute("SELECT name,total_km FROM line_ce").fetchall()
        byid: Dict[str, List[dict]] = {}
        for r in c.execute("SELECT lid,seq,name,km,kind FROM line_ce_stop ORDER BY lid,seq"):
            if (r["kind"] or "") == "work":       # 桥隧行不是站
                continue
            byid.setdefault(r["lid"], []).append({"name": r["name"], "km": r["km"]})
    return [{"source": "ce", "name": r["name"], "src_id": r["name"],
             "stops": byid.get(r["name"], []), "total_km": r["total_km"]} for r in rows]


def _custom_lines() -> List[dict]:
    with _ro(RAIL_DB) as c:
        rows = c.execute("SELECT name,stations_json,note FROM line_custom").fetchall()
    out = []
    for r in rows:
        try:
            stops = json.loads(r["stations_json"] or "[]")
        except Exception:                          # noqa: BLE001
            stops = []
        stops = [{"name": s.get("name"), "km": s.get("km")}
                 for s in stops if isinstance(s, dict) and s.get("name")]
        total = stops[-1].get("km") if stops else None
        out.append({"source": "custom", "name": r["name"], "src_id": "",
                    "stops": stops, "total_km": total})
    return out


def _osm_names() -> List[dict]:
    try:
        import railgeom
        if not railgeom.ready():
            return []
        names = [n for n in railgeom._geoms().keys() if not is_work_name(n)]
    except Exception:                              # noqa: BLE001
        return []
    return [{"source": "osm", "name": n, "src_id": n, "stops": [], "total_km": None}
            for n in names]


def collect_sources() -> List[dict]:
    """五套来源全收（OSM 只有线名，没有站序）"""
    return (_lib_lines() + _custom_lines() + _huirui_lines()
            + _ce_lines() + _osm_names())


# ---------------------------------------------------------------------------
# 建初稿
# ---------------------------------------------------------------------------

STOP_SRC_ORDER = ["lib", "custom", "huirui", "ce"]


def _pick_stops(items: List[dict]) -> Tuple[List[dict], str]:
    for s in STOP_SRC_ORDER:
        cand = [it for it in items if it["source"] == s and it["stops"]]
        if cand:
            best = max(cand, key=lambda x: len(x["stops"]))
            return best["stops"], s
    return [], ""


class _Coords:
    """站名 → (lat, lon)：档案 + 人工坐标，railcore 已经合并好两种来源"""

    def __init__(self) -> None:
        self._c: Dict[str, Optional[Tuple[float, float]]] = {}

    def get(self, name: str) -> Optional[Tuple[float, float]]:
        if not name:
            return None
        if name not in self._c:
            import railcore
            try:
                self._c[name] = railcore.stations.coord(name)
            except Exception:                      # noqa: BLE001
                self._c[name] = None
        return self._c[name]


def _coords() -> "_Coords":
    return _Coords()


def _norm_stop_name(name: str) -> str:
    import cnrail
    try:
        return cnrail.station_name(name) or name
    except Exception:                              # noqa: BLE001
        return name


def build_seed(force: bool = False) -> dict:
    """把五套来源灌成整理库初稿。

    幂等：默认只补「rail.db 里有、整理库里还没被任何别名收下」的新线名，
    已经整理过（改过名、确认过、合并过）的一律不动。force=True 才清空重建。
    """
    init()
    items = collect_sources()
    stat = {"sources": {}, "groups": 0, "lines": 0, "aliases": 0,
            "segged": 0, "relations": 0, "stopped": 0, "skipped": 0}

    c = conn()
    try:
        if force:
            for t in ("ld_stop", "ld_relation", "ld_alias", "ld_line"):
                c.execute(f"DELETE FROM {t}")
        known = {r["name"] for r in c.execute("SELECT name FROM ld_alias")}
        _seed(c, items, stat, known, force)
        c.execute("INSERT OR REPLACE INTO ld_meta(k,v) VALUES('seeded_at',?)", (_now(),))
        c.execute("INSERT OR REPLACE INTO ld_meta(k,v) VALUES('seed_stat',?)",
                  (json.dumps(stat, ensure_ascii=False),))
        c.commit()
    finally:
        c.close()
    log("seed", "", json.dumps(stat, ensure_ascii=False))
    return stat


def _seed(c: sqlite3.Connection, items: List[dict], stat: dict,
          known: set, force: bool) -> None:
    # 按 family 分组（只分「不分段」的名；分段名单独成线）
    groups: Dict[str, List[dict]] = {}
    segged: List[dict] = []
    for it in items:
        if not it.get("name"):
            continue
        it["n"] = normalize(it["name"])
        stat["sources"][it["source"]] = stat["sources"].get(it["source"], 0) + 1
        if it["n"]["seg"]:
            segged.append(it)
        else:
            groups.setdefault(family_of(it["name"]), []).append(it)
    stat["groups"] = len(groups)
    stat["segged"] = len(segged)

    # 分段名自己也会有两种写法（京哈高速线京沈段 / 京哈高速铁路京沈段）：
    # 按「父线族 + 段名」再归并一次，别让同一段挂两遍
    seg_groups: Dict[str, List[dict]] = {}
    for it in segged:
        seg_groups.setdefault(f"{family_key(it['n'])}|{it['n']['seg']}", []).append(it)

    coords = _coords()
    fam_id: Dict[str, int] = {}
    name_id: Dict[str, int] = {}

    def add_line(name: str, fam: str, kind: str, items_: List[dict],
                 src_main: str = "", note: str = "") -> Optional[int]:
        row = c.execute("SELECT id FROM ld_line WHERE name=?", (name,)).fetchone()
        if row:
            lid = row["id"]
        else:
            taken = [it for it in items_ if it["name"] in known]
            if taken and not force:
                # 这条线的某个写法已经被整理过了，别再建一条新的
                r2 = c.execute("SELECT line_id FROM ld_alias WHERE source=? AND name=?",
                               (taken[0]["source"], taken[0]["name"])).fetchone()
                if r2:
                    name_id[name] = r2["line_id"]
                    stat["skipped"] += 1
                    return None
            km = next((it["total_km"] for it in items_
                       if it["source"] == src_main and it.get("total_km")), None)
            cur = c.execute(
                "INSERT INTO ld_line(name,family,kind,status,created_at,updated_at,src_main,note,"
                "total_km) VALUES(?,?,?,'draft',?,?,?,?,?)",
                (name, fam, kind, _now(), _now(), src_main, note, km))
            lid = cur.lastrowid
            stat["lines"] += 1
        name_id[name] = lid
        for it in items_:
            if it["name"] in known:
                continue
            c.execute("INSERT OR IGNORE INTO ld_alias(line_id,source,name,src_id)"
                      " VALUES(?,?,?,?)", (lid, it["source"], it["name"], it["src_id"]))
            stat["aliases"] += 1
        return lid

    def add_stops(lid: int, items_: List[dict]) -> None:
        if c.execute("SELECT 1 FROM ld_stop WHERE line_id=? LIMIT 1", (lid,)).fetchone():
            return
        stops, src = _pick_stops(items_)
        if not stops:
            return
        names = []
        for i, s in enumerate(stops, 1):
            nm = _norm_stop_name(s.get("name") or "")
            names.append(nm)
            ll = coords.get(nm)
            c.execute("INSERT OR REPLACE INTO ld_stop(line_id,seq,name,km,lon,lat,src)"
                      " VALUES(?,?,?,?,?,?,?)",
                      (lid, i, nm, s.get("km"),
                       ll[1] if ll else None, ll[0] if ll else None, src))
        c.execute("UPDATE ld_line SET from_sta=?, to_sta=? WHERE id=?",
                  (names[0], names[-1], lid))
        stat["stopped"] += 1

    for fam, its in groups.items():
        its.sort(key=lambda x: sort_key(x["source"], len(x["stops"])))
        head = its[0]
        n = head["n"]
        lid = add_line(head["name"], fam, n["kind"], its, head["source"],
                       note=n["note"])
        if lid:
            fam_id[fam] = lid
            add_stops(lid, its)

    for _key, its in seg_groups.items():
        its.sort(key=lambda x: sort_key(x["source"], len(x["stops"])))
        head = its[0]
        n = head["n"]
        lid = add_line(head["name"], family_key(n), n["kind"], its,
                       head["source"], note=n["note"])
        if lid:
            for x in its:
                x["_id"] = lid
            add_stops(lid, its)
    # 分段名的父线如果五套来源里都没有整条的名字（海南环岛高铁线只剩东西两段），
    # 补一条只有名字的整线挂上去，免得这两段永远悬空；站序留给人工补
    for _key, its in seg_groups.items():
        n = its[0]["n"]
        fam = family_key(n)
        if fam in fam_id:
            continue
        sp = split_seg(its[0]["name"])
        if not sp:
            continue
        parent_name = sp[0]
        if c.execute("SELECT 1 FROM ld_line WHERE name=?", (parent_name,)).fetchone():
            continue
        cur = c.execute(
            "INSERT INTO ld_line(name,family,kind,status,created_at,updated_at,src_main,note)"
            " VALUES(?,?,?,'draft',?,?,'inferred','由分段名反推出来的整线名，站序待补')",
            (parent_name, fam, n["kind"], _now(), _now()))
        fam_id[fam] = cur.lastrowid
        stat["lines"] += 1
        stat["inferred"] = stat.get("inferred", 0) + 1

    # 父子：分段名挂到整线名的规范线上
    for _key, its in seg_groups.items():
        lid_child = its[0].get("_id")
        if not lid_child:
            continue
        n = its[0]["n"]
        pid = fam_id.get(family_key(n))
        if not pid or pid == lid_child:
            continue
        cur = c.execute("INSERT OR IGNORE INTO ld_relation(parent_id,child_id,kind,note)"
                        " VALUES(?,?,'seg',?)", (pid, lid_child, n["seg"]))
        stat["relations"] += cur.rowcount


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def stats() -> dict:
    init()
    with conn() as c:
        g = lambda sql: c.execute(sql).fetchone()[0]        # noqa: E731
        return {
            "lines": g("SELECT COUNT(*) FROM ld_line"),
            "confirmed": g("SELECT COUNT(*) FROM ld_line WHERE status='confirmed'"),
            "aliases": g("SELECT COUNT(*) FROM ld_alias"),
            "stops": g("SELECT COUNT(*) FROM ld_stop"),
            "withStops": g("SELECT COUNT(DISTINCT line_id) FROM ld_stop"),
            "relations": g("SELECT COUNT(*) FROM ld_relation"),
            "families": g("SELECT COUNT(DISTINCT family) FROM ld_line WHERE family<>''"),
            "bySource": {r["source"]: r["n"] for r in
                         c.execute("SELECT source,COUNT(*) n FROM ld_alias GROUP BY source")},
            "seededAt": (c.execute("SELECT v FROM ld_meta WHERE k='seeded_at'").fetchone()
                         or ["—"])[0],
        }


def list_lines(q: str = "", kind: str = "", source: str = "", status: str = "",
               has_stops: bool = False, limit: int = 200, offset: int = 0) -> dict:
    init()
    where, args = [], []
    if q:
        where.append("(l.name LIKE ? OR EXISTS(SELECT 1 FROM ld_alias a WHERE a.line_id=l.id"
                     " AND a.name LIKE ?))")
        args += [f"%{q}%", f"%{q}%"]
    if kind:
        where.append("l.kind=?")
        args.append(kind)
    if status:
        where.append("l.status=?")
        args.append(status)
    if source:
        where.append("EXISTS(SELECT 1 FROM ld_alias a WHERE a.line_id=l.id AND a.source=?)")
        args.append(source)
    if has_stops:
        where.append("EXISTS(SELECT 1 FROM ld_stop s WHERE s.line_id=l.id)")
    w = ("WHERE " + " AND ".join(where)) if where else ""
    # 有站序的排前面（没站序的多数是只在 OSM 里出现的线名），同档按站数多少
    sql = (f"SELECT l.*, (SELECT COUNT(*) FROM ld_stop s WHERE s.line_id=l.id) n_stop,"
           f" (SELECT COUNT(*) FROM ld_alias a WHERE a.line_id=l.id) n_alias,"
           f" (SELECT COUNT(*) FROM ld_relation r WHERE r.parent_id=l.id) n_child"
           f" FROM ld_line l {w}"
           f" ORDER BY (SELECT COUNT(*) FROM ld_stop s WHERE s.line_id=l.id) DESC,"
           f" l.total_km DESC, l.id LIMIT ? OFFSET ?")
    with conn() as c:
        rows = [dict(r) for r in c.execute(sql, args + [limit, offset])]
        total = c.execute(f"SELECT COUNT(*) FROM ld_line l {w}", args).fetchone()[0]
        ids = [r["id"] for r in rows]
        al: Dict[int, List[dict]] = {}
        if ids:
            qs = ",".join("?" * len(ids))
            for r in c.execute(f"SELECT line_id,source,name FROM ld_alias"
                               f" WHERE line_id IN ({qs})", ids):
                al.setdefault(r["line_id"], []).append(
                    {"source": r["source"], "name": r["name"]})
    for r in rows:
        r["aliases"] = al.get(r["id"], [])
        r["kindLabel"] = KIND_LABEL.get(r["kind"], r["kind"])
    return {"items": rows, "total": total}


def line_detail(lid: int) -> Optional[dict]:
    init()
    with conn() as c:
        row = c.execute("SELECT * FROM ld_line WHERE id=?", (lid,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["kindLabel"] = KIND_LABEL.get(d["kind"], d["kind"])
        d["aliases"] = [dict(r) for r in c.execute(
            "SELECT id,source,name,src_id FROM ld_alias WHERE line_id=? ORDER BY source", (lid,))]
        d["stops"] = [dict(r) for r in c.execute(
            "SELECT seq,name,km,lon,lat,src,note FROM ld_stop WHERE line_id=? ORDER BY seq", (lid,))]
        d["parents"] = [dict(r) for r in c.execute(
            "SELECT r.parent_id id, r.kind, r.note, l.name FROM ld_relation r"
            " JOIN ld_line l ON l.id=r.parent_id WHERE r.child_id=?", (lid,))]
        d["children"] = [dict(r) for r in c.execute(
            "SELECT r.child_id id, r.kind, r.note, l.name,"
            " (SELECT COUNT(*) FROM ld_stop s WHERE s.line_id=l.id) n_stop"
            " FROM ld_relation r JOIN ld_line l ON l.id=r.child_id"
            " WHERE r.parent_id=? ORDER BY l.name", (lid,))]
        d["coordDone"] = sum(1 for s in d["stops"] if s["lat"] is not None)
    return d


def find_line(name: str) -> Optional[dict]:
    """按规范名 / 任意来源写法找一条线"""
    init()
    with conn() as c:
        r = c.execute("SELECT id FROM ld_line WHERE name=?", (name,)).fetchone()
        if not r:
            r = c.execute("SELECT line_id id FROM ld_alias WHERE name=?"
                          " ORDER BY id LIMIT 1", (name,)).fetchone()
        return line_detail(r["id"]) if r else None


def source_stops(source: str, name: str) -> dict:
    """某个来源里这条线自己的原始站序（整理台做「多方对比」用）"""
    pool = {"lib": _lib_lines, "huirui": _huirui_lines,
            "ce": _ce_lines, "custom": _custom_lines}.get(source)
    if not pool:
        return {"source": source, "name": name, "stops": []}
    for it in pool():
        if it["name"] == name:
            return {"source": source, "name": name, "stops": it["stops"],
                    "totalKm": it["total_km"]}
    return {"source": source, "name": name, "stops": []}


def search_source(q: str, limit: int = 40) -> List[dict]:
    """在五套来源里找线名（整理台「加别名」时挑来源用）"""
    out, seen = [], set()
    ql = (q or "").strip()
    for it in collect_sources():
        if ql and ql not in (it["name"] or ""):
            continue
        k = (it["source"], it["name"])
        if k in seen:
            continue
        seen.add(k)
        out.append({"source": it["source"], "name": it["name"],
                    "n_stop": len(it["stops"]), "totalKm": it["total_km"]})
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# 编辑
# ---------------------------------------------------------------------------

_LINE_FIELDS = ("name", "kind", "status", "total_km", "from_sta", "to_sta", "note")


def save_line(lid: int, payload: dict) -> dict:
    init()
    with conn() as c:
        row = c.execute("SELECT * FROM ld_line WHERE id=?", (lid,)).fetchone()
        if not row:
            raise ValueError("没有这条线路")
        sets, args = [], []
        for k in _LINE_FIELDS:
            if k not in payload:
                continue
            v = payload[k]
            if k == "name":
                v = str(v or "").strip()
                if not v:
                    raise ValueError("线名不能为空")
                dup = c.execute("SELECT id FROM ld_line WHERE name=? AND id<>?",
                                (v, lid)).fetchone()
                if dup:
                    raise ValueError(f"已经有叫「{v}」的线路了")
            if k == "total_km" and v in ("", None):
                v = None
            if k == "kind":
                v = str(v or "")
            sets.append(f"{k}=?")
            args.append(v)
        if sets:
            sets.append("updated_at=?")
            args.append(_now())
            args.append(lid)
            c.execute(f"UPDATE ld_line SET {', '.join(sets)} WHERE id=?", args)
        # 改名时重算归一键；新名字认不出后缀（改成「京沪高速线①」这种）就保留原族，
        # 否则一改名就把自己和同族写法拆散了
        if "name" in payload:
            nm = c.execute("SELECT name,family FROM ld_line WHERE id=?", (lid,)).fetchone()
            d = normalize(nm["name"])
            fam = f"{d['base']}|{d['kind']}" if d["kind"] else (nm["family"] or "")
            c.execute("UPDATE ld_line SET family=? WHERE id=?", (fam, lid))
    log("save", str(lid), json.dumps(payload, ensure_ascii=False)[:400])
    return line_detail(lid) or {}


def create_line(name: str, kind: str = "", note: str = "") -> dict:
    init()
    nm = str(name or "").strip()
    if not nm:
        raise ValueError("线名不能为空")
    with conn() as c:
        if c.execute("SELECT 1 FROM ld_line WHERE name=?", (nm,)).fetchone():
            raise ValueError(f"已经有叫「{nm}」的线路了")
        d = normalize(nm)
        cur = c.execute(
            "INSERT INTO ld_line(name,family,kind,status,created_at,updated_at,note,src_main)"
            " VALUES(?,?,?,'confirmed',?,?,?,'manual')",
            (nm, family_of(nm), kind or d["kind"], _now(), _now(), note))
        lid = cur.lastrowid
    log("new", str(lid), nm)
    return line_detail(lid) or {}


def delete_line(lid: int) -> None:
    init()
    with conn() as c:
        c.execute("DELETE FROM ld_relation WHERE parent_id=? OR child_id=?", (lid, lid))
        c.execute("DELETE FROM ld_stop WHERE line_id=?", (lid,))
        c.execute("DELETE FROM ld_alias WHERE line_id=?", (lid,))
        c.execute("DELETE FROM ld_line WHERE id=?", (lid,))
    log("delete", str(lid))


def add_alias(lid: int, source: str, name: str, src_id: str = "") -> dict:
    init()
    nm = str(name or "").strip()
    src = str(source or "").strip()
    if not nm or src not in SRC_LABEL:
        raise ValueError("来源或线名不对")
    with conn() as c:
        dup = c.execute("SELECT line_id FROM ld_alias WHERE source=? AND name=?",
                        (src, nm)).fetchone()
        if dup and dup["line_id"] != lid:
            raise ValueError(f"「{nm}」已经被另一条线收作 {SRC_LABEL[src]} 别名")
        if not dup:
            c.execute("INSERT INTO ld_alias(line_id,source,name,src_id) VALUES(?,?,?,?)",
                      (lid, src, nm, src_id))
        c.execute("UPDATE ld_line SET updated_at=? WHERE id=?", (_now(), lid))
    log("alias+", str(lid), f"{src}:{nm}")
    return line_detail(lid) or {}


def del_alias(alias_id: int) -> dict:
    init()
    with conn() as c:
        r = c.execute("SELECT line_id FROM ld_alias WHERE id=?", (alias_id,)).fetchone()
        if not r:
            raise ValueError("没有这个别名")
        lid = r["line_id"]
        c.execute("DELETE FROM ld_alias WHERE id=?", (alias_id,))
        c.execute("UPDATE ld_line SET updated_at=? WHERE id=?", (_now(), lid))
    log("alias-", str(lid), str(alias_id))
    return line_detail(lid) or {}


def save_stops(lid: int, stops: List[dict]) -> dict:
    """整份覆盖站序（页面上拖动 / 增删之后一次性保存）

    没带坐标的站按站名查档案补上——手打一个站名也不该在地图上是个洞。
    """
    init()
    coords = _coords()
    with conn() as c:
        if not c.execute("SELECT 1 FROM ld_line WHERE id=?", (lid,)).fetchone():
            raise ValueError("没有这条线路")
        c.execute("DELETE FROM ld_stop WHERE line_id=?", (lid,))
        for i, s in enumerate(stops, 1):
            nm = _norm_stop_name(str((s or {}).get("name") or "").strip())
            if not nm:
                continue
            km = s.get("km")
            lat, lon = s.get("lat"), s.get("lon")
            if lat is None or lon is None:
                ll = coords.get(nm)
                if ll:
                    lat, lon = ll[0], ll[1]
            c.execute("INSERT INTO ld_stop(line_id,seq,name,km,lon,lat,src,note)"
                      " VALUES(?,?,?,?,?,?,?,?)",
                      (lid, i, nm, km, lon, lat,
                       s.get("src") or "manual", s.get("note") or ""))
        n = c.execute("SELECT COUNT(*) FROM ld_stop WHERE line_id=?", (lid,)).fetchone()[0]
        first = c.execute("SELECT name FROM ld_stop WHERE line_id=? ORDER BY seq LIMIT 1",
                          (lid,)).fetchone()
        last = c.execute("SELECT name FROM ld_stop WHERE line_id=? ORDER BY seq DESC LIMIT 1",
                         (lid,)).fetchone()
        c.execute("UPDATE ld_line SET total_km=COALESCE(?,total_km), from_sta=?, to_sta=?,"
                  " updated_at=? WHERE id=?",
                  (stops[-1].get("km") if stops else None,
                   first["name"] if first else None, last["name"] if last else None,
                   _now(), lid))
    log("stops", str(lid), f"{len(stops)} 站")
    return line_detail(lid) or {}


def fill_stops(lid: int, source: str, name: str) -> dict:
    """从某个来源把站序搬过来（页面「用 XX 来源的站序」按钮）"""
    d = source_stops(source, name)
    stops = d.get("stops") or []
    if not stops:
        raise ValueError(f"{SRC_LABEL.get(source, source)} 里没有「{name}」的站序")
    coords = _coords()
    # china-emu 记的是 K 里程（全线累计），陇海西宝段是 K1073→K1256：
    # 搬过来时统一减掉首站，换成段内里程，否则总里程会被算成 1256
    base = stops[0].get("km") or 0
    out = []
    for s in stops:
        nm = _norm_stop_name(s.get("name") or "")
        ll = coords.get(nm)
        km = s.get("km")
        out.append({"name": nm, "km": (km - base) if km is not None else None,
                    "lat": ll[0] if ll else None, "lon": ll[1] if ll else None,
                    "src": source})
    return save_stops(lid, out)


def merge_lines(ids: List[int], into: int = 0, name: str = "") -> dict:
    """把几条线并成一条：别名、站序、父子关系全部转到目标线上

    站序默认保留目标线自己的；目标线没有站序时，借用站数最多的那一条。
    """
    init()
    ids = [int(x) for x in ids if int(x)]
    if len(ids) < 2:
        raise ValueError("至少选两条线才能合并")
    into = int(into or ids[0])
    if into not in ids:
        ids.append(into)
    with conn() as c:
        rows = {r["id"]: dict(r) for r in
                c.execute(f"SELECT * FROM ld_line WHERE id IN ({','.join('?' * len(ids))})",
                          ids)}
        if into not in rows:
            raise ValueError("目标线路不存在")
        keep = rows[into]
        has = c.execute("SELECT COUNT(*) FROM ld_stop WHERE line_id=?", (into,)).fetchone()[0]
        if not has:
            best, bn = 0, 0
            for i in ids:
                n = c.execute("SELECT COUNT(*) FROM ld_stop WHERE line_id=?", (i,)).fetchone()[0]
                if n > bn:
                    best, bn = i, n
            if best:
                for s in c.execute("SELECT * FROM ld_stop WHERE line_id=? ORDER BY seq", (best,)):
                    c.execute("INSERT INTO ld_stop(line_id,seq,name,km,lon,lat,src,note)"
                              " VALUES(?,?,?,?,?,?,?,?)",
                              (into, s["seq"], s["name"], s["km"], s["lon"], s["lat"],
                               s["src"], s["note"]))
        for i in ids:
            if i == into:
                continue
            # 别名全转过来；重名的丢掉。
            # 先把这批别名读出来再改（边查边改同一张表会漏行）
            rows_al = c.execute("SELECT id,source,name FROM ld_alias WHERE line_id=?",
                                (i,)).fetchall()
            for a in rows_al:
                dup = c.execute("SELECT 1 FROM ld_alias WHERE source=? AND name=?",
                                (a["source"], a["name"])).fetchone()
                if dup:
                    c.execute("DELETE FROM ld_alias WHERE id=?", (a["id"],))
                else:
                    c.execute("UPDATE ld_alias SET line_id=? WHERE id=?", (into, a["id"]))
            # 被并掉的那条线的规范名本身也得留个写法，否则搜索里就再也找不到它了
            old = rows[i]
            if old["name"] and old["name"] != keep["name"]:
                hit = c.execute("SELECT 1 FROM ld_alias WHERE name=?", (old["name"],)).fetchone()
                if not hit:
                    c.execute("INSERT OR IGNORE INTO ld_alias(line_id,source,name,src_id)"
                              " VALUES(?,?,?,'')",
                              (into, old["src_main"] or "custom", old["name"]))
            # 父子关系转过来，指向自己的丢掉
            for r in c.execute("SELECT parent_id,child_id,kind,note FROM ld_relation"
                               " WHERE parent_id=? OR child_id=?", (i, i)).fetchall():
                p, ch = (into, r["child_id"]) if r["parent_id"] == i else (r["parent_id"], into)
                if p == ch or p == i or ch == i:
                    continue
                c.execute("INSERT OR IGNORE INTO ld_relation(parent_id,child_id,kind,note)"
                          " VALUES(?,?,?,?)", (p, ch, r["kind"], r["note"]))
            c.execute("DELETE FROM ld_relation WHERE parent_id=? OR child_id=?", (i, i))
            c.execute("DELETE FROM ld_stop WHERE line_id=?", (i,))
            c.execute("DELETE FROM ld_line WHERE id=?", (i,))
        if name:
            nm = str(name).strip()
            if nm and nm != keep["name"]:
                dup = c.execute("SELECT id FROM ld_line WHERE name=? AND id<>?",
                                (nm, into)).fetchone()
                if dup:
                    raise ValueError(f"已经有叫「{nm}」的线路了")
                c.execute("UPDATE ld_line SET name=?, family=? WHERE id=?",
                          (nm, family_of(nm), into))
        c.execute("UPDATE ld_line SET updated_at=?, status='confirmed' WHERE id=?",
                  (_now(), into))
    log("merge", str(into), f"{len(ids)} 条合并")
    return line_detail(into) or {}


def set_relation(parent: int, child: int, kind: str = "seg", note: str = "") -> dict:
    init()
    parent, child = int(parent), int(child)
    if parent == child:
        raise ValueError("不能自己当自己的父线")
    with conn() as c:
        for i in (parent, child):
            if not c.execute("SELECT 1 FROM ld_line WHERE id=?", (i,)).fetchone():
                raise ValueError("线路不存在")
        # 防成环：child 的祖先里不能有 parent
        seen, stack = set(), [parent]
        while stack:
            cur = stack.pop()
            if cur == child:
                raise ValueError("会成环，换个父线")
            if cur in seen:
                continue
            seen.add(cur)
            stack += [r[0] for r in c.execute(
                "SELECT parent_id FROM ld_relation WHERE child_id=?", (cur,))]
        c.execute("INSERT OR REPLACE INTO ld_relation(parent_id,child_id,kind,note)"
                  " VALUES(?,?,?,?)", (parent, child, kind or "seg", note))
    log("rel+", f"{parent}->{child}", kind)
    return line_detail(parent) or {}


def del_relation(parent: int, child: int) -> dict:
    init()
    with conn() as c:
        c.execute("DELETE FROM ld_relation WHERE parent_id=? AND child_id=?",
                  (int(parent), int(child)))
    log("rel-", f"{parent}->{child}")
    return line_detail(int(parent)) or {}


# ---------------------------------------------------------------------------
# 同族未归属的写法（「这条线还有别的叫法没收进来」）
# ---------------------------------------------------------------------------

_SRC_CACHE: Dict[str, object] = {"at": 0.0, "items": []}
_SRC_TTL = 300.0


def _sources_cached() -> List[dict]:
    now = time.time()
    if now - float(_SRC_CACHE["at"] or 0) > _SRC_TTL or not _SRC_CACHE["items"]:
        _SRC_CACHE["items"] = collect_sources()
        _SRC_CACHE["at"] = now
    return _SRC_CACHE["items"]                        # type: ignore[return-value]


def clear_cache() -> None:
    _SRC_CACHE["at"] = 0.0
    _SRC_CACHE["items"] = []


def family_candidates(lid: int, limit: int = 60) -> List[dict]:
    """同族但还没被任何线收下的来源写法"""
    init()
    with conn() as c:
        row = c.execute("SELECT name,family FROM ld_line WHERE id=?", (lid,)).fetchone()
        if not row:
            return []
        fam = row["family"] or family_of(row["name"])
        taken = {(r["source"], r["name"]) for r in
                 c.execute("SELECT source,name FROM ld_alias")}
        own = {r["name"] for r in c.execute(
            "SELECT name FROM ld_alias WHERE line_id=?", (lid,))}
    out = []
    for it in _sources_cached():
        if (it["source"], it["name"]) in taken:
            continue
        if family_of(it["name"]) != fam:
            continue
        out.append({"source": it["source"], "name": it["name"],
                    "n_stop": len(it["stops"]), "totalKm": it["total_km"],
                    "sameName": it["name"] in own})
    out.sort(key=lambda x: (sort_key(x["source"], x["n_stop"])))
    return out[:limit]


# ---------------------------------------------------------------------------
# 几何 / 导出
# ---------------------------------------------------------------------------

def geom(lid: int) -> dict:
    """这条线在地图上的走向：拿它的 OSM 别名去取几何，取不到就只给站间直连"""
    d = line_detail(lid)
    if not d:
        raise ValueError("没有这条线路")
    import railgeom
    names = [a["name"] for a in d["aliases"] if a["source"] == "osm"]
    names += [d["name"]]
    g = None
    for nm in names:
        try:
            g = railgeom.geom(nm, expect_km=d.get("total_km")) if railgeom.ready() else None
        except Exception:                          # noqa: BLE001
            g = None
        if g:
            g["ask"] = nm
            break
    return {"line": {"id": d["id"], "name": d["name"], "totalKm": d["total_km"]},
            "stops": [{"name": s["name"], "km": s["km"], "lat": s["lat"], "lon": s["lon"]}
                      for s in d["stops"]],
            "geom": g}


def export_map() -> dict:
    """整理成果导出：规范线名 ← 各来源写法，外加父子关系与站序

    将来要把整理结果回灌 rail.db 时，照这个结构生成 alias 映射即可。
    """
    init()
    with conn() as c:
        lines = []
        for r in c.execute("SELECT * FROM ld_line ORDER BY id"):
            lid = r["id"]
            al = {}
            for a in c.execute("SELECT source,name FROM ld_alias WHERE line_id=? ORDER BY source",
                               (lid,)):
                al.setdefault(a["source"], []).append(a["name"])
            stops = [{"name": s["name"], "km": s["km"], "lon": s["lon"], "lat": s["lat"]}
                     for s in c.execute("SELECT name,km,lon,lat FROM ld_stop"
                                        " WHERE line_id=? ORDER BY seq", (lid,))]
            lines.append({"id": lid, "name": r["name"], "kind": r["kind"],
                          "status": r["status"], "totalKm": r["total_km"],
                          "from": r["from_sta"], "to": r["to_sta"],
                          "family": r["family"], "alias": al, "stops": stops})
        rel = [{"parent": r["parent_id"], "child": r["child_id"],
                "kind": r["kind"], "note": r["note"]}
               for r in c.execute("SELECT * FROM ld_relation")]
    return {"exportedAt": _now(),
            "counts": {"lines": len(lines), "relations": len(rel)},
            "lines": lines, "relations": rel}


def recent_log(limit: int = 60) -> List[dict]:
    init()
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT ts,op,target,detail FROM ld_log ORDER BY id DESC LIMIT ?", (limit,))]
