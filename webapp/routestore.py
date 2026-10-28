"""经停站数据：一天一条指针，内容按趟去重。

12306 的经停站数据里没有日期字段，同一趟车（`train_no`）停站不变时每天抓到的
内容逐字节相同。以前 `routes` 表按 (train_no, date) 存整份 JSON，25 天下来
24.6 万行、520 MB，其中只有 1.3 万份内容是不同的——重复率 95%。

现在拆成两张表 + 一个只读视图（视图名仍叫 `routes`，老的 SELECT 一行都不用改）：

    route_tpl   一趟车的一份「停站表」，按 (train_no, 指纹) 唯一
    route_day   每个日期一条记录，只存「指向哪份停站表」，几十字节
    routes      视图：route_day ⋈ route_tpl，字段与老表一致

每天采集照旧抓全量（不抓就不知道有没有变），但只有内容变了才新存一份；
`route_day` 的 `tpl_id` 从哪天开始换了，就是「这趟车从哪天起停站变了」。
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Iterable, List, Optional, Sequence

# 判定「停站一样」只看这些字段：哪一站、几点到、几点开、挂什么车次号。
# 故意不含 wz_num（余票占位数）、service_type 这类会随日期变、又不参与展示的字段。
FP_KEYS = (
    "station_no",
    "station_name",
    "station_train_code",
    "arrive_time",
    "start_time",
    "arrive_day_diff",
    "train_class_name",
    "is_start",
    "start_station_name",
    "end_station_name",
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS route_tpl (
    id          INTEGER PRIMARY KEY,
    train_no    TEXT NOT NULL,
    fp          TEXT NOT NULL,       -- 停站指纹（STOP_KEY 算出来的）
    raw         TEXT NOT NULL,       -- 12306 原始经停站 JSON
    n_stops     INTEGER,
    created_at  REAL,
    hits        INTEGER DEFAULT 0,   -- 被几个日期复用
    UNIQUE (train_no, fp)
);

CREATE TABLE IF NOT EXISTS route_day (
    train_no    TEXT NOT NULL,
    date        TEXT NOT NULL,
    code        TEXT,
    tpl_id      INTEGER NOT NULL,
    updated_at  REAL,
    PRIMARY KEY (train_no, date)
);

CREATE INDEX IF NOT EXISTS ix_route_day_date ON route_day (date);
CREATE INDEX IF NOT EXISTS ix_route_day_code ON route_day (code, date);
CREATE INDEX IF NOT EXISTS ix_route_day_tpl  ON route_day (tpl_id);
"""

VIEW_SQL = """
CREATE VIEW routes AS
SELECT d.train_no, d.date, d.code, t.raw, d.updated_at, d.tpl_id
  FROM route_day d
  JOIN route_tpl t ON t.id = d.tpl_id
"""

VIEW_COLS = ("train_no", "date", "code", "raw", "updated_at", "tpl_id")


# ---------------------------------------------------------------------------
# 指纹
# ---------------------------------------------------------------------------
def stop_key(raw) -> str:
    """停站表的指纹。传 JSON 字符串或已解析的 list 都行。

    同一趟车只要「站序 + 到点 + 开点 + 车次号」不变，指纹就一样——不管 12306
    返回的字段顺序、空白、余票数有没有变。
    """
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "ignore")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return "raw:" + hashlib.blake2b(raw.encode("utf-8", "ignore"),
                                           digest_size=16).hexdigest()
    if not isinstance(raw, list):
        return ""
    buf: List[str] = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        buf.append("\x1f".join(str(it.get(k) or "").strip() for k in FP_KEYS))
    return hashlib.blake2b("\n".join(buf).encode("utf-8"), digest_size=16).hexdigest()


# ---------------------------------------------------------------------------
# 建表 / 迁移
# ---------------------------------------------------------------------------
def table_kind(conn, name: str) -> Optional[str]:
    """'table' / 'view' / None"""
    row = conn.execute(
        "SELECT type FROM sqlite_master WHERE name=? COLLATE NOCASE", (name,)
    ).fetchone()
    return (row[0] if row else None)


def ensure_schema(conn) -> str:
    """保证有 route_tpl / route_day / routes 视图。返回当前状态：

    'ok'      已经是新结构
    'legacy'  还是老的 routes 表（需要跑 scripts/migrate_routes_dedup.py）
    'empty'   全新库，已建好
    """
    kind = table_kind(conn, "routes")
    if kind == "view":
        create_schema(conn)
        return "ok"
    create_schema(conn)          # 老库也先把新表建好，免得写入路径直接报错；读还是读老表
    if kind == "table":
        return "legacy"
    conn.executescript(VIEW_SQL)
    return "empty"


def create_schema(conn) -> None:
    conn.executescript(SCHEMA_SQL)


def create_view(conn) -> None:
    if table_kind(conn, "routes") is None:
        conn.executescript(VIEW_SQL)


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------
def _tpl_id(conn, train_no: str, fp: str, raw_text: str, n_stops: int,
            cache: Optional[dict] = None) -> tuple[int, bool]:
    if cache is not None:
        hit = cache.get((train_no, fp))
        if hit:
            return hit, False
    row = conn.execute(
        "SELECT id FROM route_tpl WHERE train_no=? AND fp=?", (train_no, fp)
    ).fetchone()
    if row:
        conn.execute("UPDATE route_tpl SET hits=hits+1 WHERE id=?", (row[0],))
        if cache is not None:
            cache[(train_no, fp)] = row[0]
        return row[0], False
    cur = conn.execute(
        "INSERT INTO route_tpl (train_no, fp, raw, n_stops, created_at, hits)"
        " VALUES (?,?,?,?,?,0)",
        (train_no, fp, raw_text, n_stops, time.time()),
    )
    tid = int(cur.lastrowid)
    if cache is not None:
        cache[(train_no, fp)] = tid
    return tid, True


def put_route(conn, train_no: str, date: str, code: str, raw_text: str,
              updated_at: Optional[float] = None,
              cache: Optional[dict] = None) -> dict:
    """存一天的经停站：内容与这趟车已有版本相同就只挂个指针，返回 {tplId, created}"""
    raw_text = raw_text if isinstance(raw_text, str) else json.dumps(raw_text, ensure_ascii=False)
    fp = stop_key(raw_text)
    tid, created = _tpl_id(conn, train_no, fp, raw_text, _n_stops(raw_text), cache)
    conn.execute(
        "INSERT INTO route_day (train_no, date, code, tpl_id, updated_at)"
        " VALUES (?,?,?,?,?)"
        " ON CONFLICT(train_no, date) DO UPDATE SET"
        "   code=excluded.code, tpl_id=excluded.tpl_id, updated_at=excluded.updated_at",
        (train_no, date, code, tid, updated_at if updated_at is not None else time.time()),
    )
    return {"tplId": tid, "created": created}


def put_routes_for_date(conn, date: str, rows: Iterable[Sequence]) -> dict:
    """rows: (train_no, code, raw_json)。返回 {written, newVersions, rows}"""
    cache: dict = {}
    written = created = 0
    for r in rows:
        train_no, code, raw_text = r[0], r[1], r[2]
        if not train_no or not raw_text:
            continue
        fp = stop_key(raw_text)
        tid, is_new = _tpl_id(conn, train_no, fp, raw_text, _n_stops(raw_text), cache)
        conn.execute(
            "INSERT INTO route_day (train_no, date, code, tpl_id, updated_at)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(train_no, date) DO UPDATE SET"
            "   code=excluded.code, tpl_id=excluded.tpl_id, updated_at=excluded.updated_at",
            (train_no, date, code, tid, time.time()),
        )
        written += 1
        created += 1 if is_new else 0
    return {"written": written, "newVersions": created}


def _n_stops(raw_text: str) -> int:
    try:
        return len(json.loads(raw_text) or [])
    except ValueError:
        return 0


# ---------------------------------------------------------------------------
# 读：版本明细（哪个日期段用哪份停站表）
# ---------------------------------------------------------------------------
def versions(conn, train_no: str) -> List[dict]:
    """一趟车的停站版本时间线：[{tplId, from, to, days, nStops, code}]"""
    rows = conn.execute(
        "SELECT d.date, d.tpl_id, d.code, t.n_stops"
        "  FROM route_day d JOIN route_tpl t ON t.id = d.tpl_id"
        " WHERE d.train_no=? ORDER BY d.date",
        (train_no,),
    ).fetchall()
    out: List[dict] = []
    for r in rows:
        date, tid = r[0], r[1]
        if out and out[-1]["tplId"] == tid:
            out[-1]["to"] = date
            out[-1]["days"] += 1
            continue
        out.append({"tplId": tid, "from": date, "to": date, "days": 1,
                    "nStops": r[3], "code": r[2]})
    return out
