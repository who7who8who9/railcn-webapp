# -*- coding: utf-8 -*-
"""
collector.py — 按日期批量下载「全部车次 + 经停站」，数据永久落库

设计要点
--------
1. **不依赖 12306 搜索接口**（它限流很凶，403 一挂就是几十分钟）。
   利用「train_no 是车次固定编号、跨日期不变」这一点：
   本地库里已有 1.7 万条 (车次号, train_no) 的车次全集，
   对目标日期逐个查经停站 —— 有数据就说明当天开行；没数据只记进带复核期限的 day_skip
   （12306 在放票前 / 临时加开 / 接口抖动时都会返空，不能当成永久结论）。
2. 一切结果写进 SQLite（trains / routes 表），**长期保留**，不清库。
   重复下载只补缺失（断点续传可随时暂停/继续）。
3. 可并发（默认 6 线程）+ 自适应退避；连续失败判定为限流则自动暂停。
4. 搜索接口只用来「刷新车次全集」（可选操作），单线程 2 秒间隔，遇到 403 长时间冷却。
"""
from __future__ import annotations

import contextlib
import csv
import datetime as dt
import json
import os
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import railcore  # noqa: E402
import routestore  # noqa: E402
import config as rail_config  # noqa: E402

EXPORT_DIR = os.path.join(railcore.DATA_DIR, "exports")
os.makedirs(EXPORT_DIR, exist_ok=True)

# 车次搜索前缀的字母表。2026-11-25 改号后新增 V/N（普速）、X（350km 动车）、
# B（货运班列）等字头，见 docs/renumber-2026.md；**N 现在是真的 N 字头**，
# 纯数字车次前缀由 DIGIT_PREFIXES 单独生成，不能再拿 N 当「数字」的代号。
# 这里只决定「发哪些搜索词」，不用来判定车种：_detect_type 收任意字母。
TRAIN_TYPES = ["G", "D", "C", "Z", "T", "K", "S", "Y", "V", "N", "X", "B"]
DIGIT_PREFIXES = [str(n) for n in range(1, 10)]       # 纯数字车次：1..9
DIGIT_MARK = "#"                                       # 仅用于 _prefix 生成数字词
FULL_RESULT_THRESHOLD = 195      # 搜索接口单页上限约 200，达到则继续细分
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
DATE_WINDOW = 15                 # 含今天在内共 15 天（offset 0..14）

# 限流相关：12306 是按 IP 算的，连续几小时满速跑迟早 403
THROTTLE_BASE = 0.45             # 正常每线程间隔
THROTTLE_MAX = 3.0               # 退避上限
THROTTLE_FRESH_AFTER = 1800      # 半小时没再出事 → 回到正常速度
CHAIN_GAP = 60                   # 连跑换天冷却（秒）：让节奏看起来不像压测
CHAIN_GAP_BAD = 180              # 上一天有失败 / 被限流 → 多歇一会
ENUM_MIN_INTERVAL = 7 * 86400    # 全集一周刷一次就够；这步要发 800 个搜索请求，最容易被 403
ENUM_MIN_UNIVERSE = 1000         # 全集小于这个数说明库是空的，必须刷

# 「当天不开行」只是「最近一次查验没拿到经停站」，不是永久结论：
# 预售期滚动放票、临时加开、接口偶发空响应都会让它事后变错。
# expire > now 的才屏蔽下载；到期的由 _release_stale_skips 每轮放行一批重查。
SKIP_FOREVER = 4_000_000_000.0   # 发车日已过去的记录：历史结论，不再折腾
SKIP_TTL_NEAR = 6 * 3600         # 距发车 ≤2 天：复核勤一点
SKIP_TTL_DAY = 86400             # 距发车 ≥3 天：每天至少有一次机会
SKIP_TTL_MAX = 7 * 86400         # 连续查空越多越可信，退避上限 7 天
SKIP_RECHECK_BUDGET = 200        # 每轮每天最多放行多少条到期标记（避免 7 万条一次涌出来）

# 全局节流记忆：某次被限流拉长的间隔，不会因为「换一天 / 继续」被清零
_THROTTLE = {"delay": THROTTLE_BASE, "ts": 0.0}
_THROTTLE_LOCK = threading.Lock()


def _note_throttle(delay: float) -> None:
    """把这次学到的间隔记下来，跨天、跨任务继续用"""
    with _THROTTLE_LOCK:
        _THROTTLE["delay"] = max(_THROTTLE["delay"], min(THROTTLE_MAX, float(delay)))
        _THROTTLE["ts"] = time.time()


def learned_delay() -> float:
    """新任务该从多慢起步：继承上次的退避，随时间慢慢回到正常速度"""
    with _THROTTLE_LOCK:
        d, ts = _THROTTLE["delay"], _THROTTLE["ts"]
    if d <= THROTTLE_BASE:
        return THROTTLE_BASE
    age = time.time() - ts
    if age > THROTTLE_FRESH_AFTER:
        return THROTTLE_BASE
    if age > 600:                # 10 分钟以上没出事：退一半
        return max(THROTTLE_BASE, THROTTLE_BASE + (d - THROTTLE_BASE) * 0.5)
    return min(THROTTLE_MAX, d)


# ---------------------------------------------------------------------------
# 数据库
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def _conn():
    conn = sqlite3.connect(railcore.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _conn() as c:
        c.executescript(
            """
            -- 「最近一次查验没拿到经停站」：带复核期限，不是永久结论
            CREATE TABLE IF NOT EXISTS day_skip (
                date   TEXT NOT NULL,
                code   TEXT NOT NULL,
                ts     REAL,
                expire REAL DEFAULT 0,      -- > now 屏蔽下载；= 0 已放行待重查
                hits   INTEGER DEFAULT 1,   -- 连续查空次数，指数退避用
                PRIMARY KEY (date, code)
            );
            CREATE TABLE IF NOT EXISTS day_collect (
                date        TEXT PRIMARY KEY,
                status      TEXT,
                phase       TEXT,
                trains      INTEGER DEFAULT 0,
                stops       INTEGER DEFAULT 0,
                failed      INTEGER DEFAULT 0,
                workers     INTEGER DEFAULT 6,
                started_at  REAL,
                updated_at  REAL,
                finished_at REAL,
                message     TEXT
            );
            CREATE TABLE IF NOT EXISTS collect_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT, ts REAL, kind TEXT, msg TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_collectlog_date ON collect_log(date);
            -- 车次全集的物化表（每个车次号一行）：统计/待处理都读它，别再去 GROUP BY 47 万行
            CREATE TABLE IF NOT EXISTS train_universe (
                code      TEXT PRIMARY KEY,
                train_no  TEXT,
                type      TEXT,
                start_sta TEXT,
                end_sta   TEXT,
                d         TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_train_uni_no ON train_universe(train_no);
            -- 一键连跑的断点（单行）：服务重启后还能看出来跑到哪天
            CREATE TABLE IF NOT EXISTS chain_run (
                id          INTEGER PRIMARY KEY CHECK (id = 1),
                status      TEXT,
                phase       TEXT,
                cur_date    TEXT,
                idx         INTEGER DEFAULT 0,
                total       INTEGER DEFAULT 0,
                plan        TEXT,
                workers     INTEGER DEFAULT 6,
                started_at  REAL,
                updated_at  REAL,
                finished_at REAL,
                message     TEXT
            );
            """
        )
        _migrate_day_skip(c)


def _skip_ttl(date: str, hits: int, now: float | None = None) -> float:
    """「不开行」标记的复核期限：距发车越近查得越勤，连续查空越多退避越长"""
    now = time.time() if now is None else now
    try:
        lead = (dt.date.fromisoformat(date) - dt.date.today()).days
    except (ValueError, TypeError):
        lead = 0
    if lead < 0:                       # 发车日已过去：历史结论，不再折腾
        return SKIP_FOREVER
    base = SKIP_TTL_NEAR if lead <= 2 else SKIP_TTL_DAY
    return now + min(base * (2 ** max(0, int(hits) - 1)), SKIP_TTL_MAX)


def _migrate_day_skip(c) -> dict:
    """老库升级：补 expire/hits，清掉自相矛盾的旧结论，给未来日期排好复核期

    只跑一次（靠「列是否已存在」判断）。清掉的行会先备份成 CSV，不静默丢数据。
    """
    cols = {r["name"] for r in c.execute("PRAGMA table_info(day_skip)")}
    if "expire" in cols and "hits" in cols:
        return {"migrated": False}
    if "expire" not in cols:
        c.execute("ALTER TABLE day_skip ADD COLUMN expire REAL DEFAULT 0")
    if "hits" not in cols:
        c.execute("ALTER TABLE day_skip ADD COLUMN hits INTEGER DEFAULT 1")
    c.execute("CREATE INDEX IF NOT EXISTS idx_skip_expire ON day_skip(date, expire)")
    now = time.time()
    today = dt.date.today()

    # 当日车次清单里有、或已经拿到经停站，却还挂着「不开行」—— 旧结论明确是错的。
    # 这些车下一轮下载会重新查一遍（真不开行会再写回来，并带上新的复核期限）。
    bad = c.execute(
        """SELECT s.date, s.code FROM day_skip s
            WHERE EXISTS (SELECT 1 FROM trains t WHERE t.code=s.code AND t.date=s.date)
               OR EXISTS (SELECT 1 FROM route_day r WHERE r.code=s.code AND r.date=s.date)"""
    ).fetchall()
    if bad:
        try:
            bdir = os.path.join(railcore.DATA_DIR, "backup",
                                "skip-migrate-" + time.strftime("%Y%m%d-%H%M%S"))
            os.makedirs(bdir, exist_ok=True)
            with open(os.path.join(bdir, "dropped_skip.csv"), "w",
                      newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["date", "code"])
                w.writerows((r["date"], r["code"]) for r in bad)
        except OSError:
            pass
        c.executemany("DELETE FROM day_skip WHERE date=? AND code=?",
                      [(r["date"], r["code"]) for r in bad])

    rows = c.execute("SELECT date, code FROM day_skip").fetchall()
    upd = []
    for r in rows:
        try:
            lead = (dt.date.fromisoformat(r["date"]) - today).days
        except (ValueError, TypeError):
            lead = -1
        # 历史日定为终态；未来日在 2 小时内按「发车日越远越早到期」错峰，
        # 之后全部进入待复核池，交给连跑按预算分批复核。
        exp = (SKIP_FOREVER if lead < 0 else
               now + (DATE_WINDOW - 1 - min(lead, DATE_WINDOW - 1)) * 480)
        upd.append((exp, 2, r["date"], r["code"]))
    c.executemany("UPDATE day_skip SET expire=?, hits=? WHERE date=? AND code=?", upd)
    return {"migrated": True, "dropped": len(bad), "backfilled": len(upd)}


def _log(date: str, kind: str, msg: str) -> None:
    try:
        with _conn() as c:
            c.execute(
                "INSERT INTO collect_log (date, ts, kind, msg) VALUES (?,?,?,?)",
                (date, time.time(), kind, msg[:400]),
            )
    except Exception:                                   # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 车次全集
# ---------------------------------------------------------------------------
# 同一车次号在不同日期可能登记了不同 train_no，这里统一取 MAX(train_no)，
# 保证「全集 / 待处理 / 进度统计」三处选到的是同一个值（否则 pending 会算不准）
_UNIVERSE_SQL = """
    SELECT code, MAX(train_no) AS train_no, MAX(type) AS type,
           MAX(start_sta) AS start_sta, MAX(end_sta) AS end_sta,
           MAX(date) AS d
      FROM trains
     WHERE train_no IS NOT NULL AND train_no <> ''
     GROUP BY code
"""

# 全集物化：trains 是「每个日期一份」的登记表（31 天 ≈ 47 万行），
# 每次统计都现场 GROUP BY 一遍要 0.2 秒，15 天轮询就是十几秒。
# 这里只把「每个车次号挑一行」的结论存成 1.8 万行的小表，变脏了才重建（0.3 秒）。
_UNI_LOCK = threading.Lock()
_UNI_STATE = {"fp": None, "ts": 0.0, "dirty": True, "urgent": False}
_UNI_MIN_GAP = 30                 # 下载过程中每天都往 trains 写进度，全集不必每次重建

_PENDING_SQL = """
    SELECT u.code, u.train_no, u.type, u.start_sta, u.end_sta, u.d
      FROM train_universe u
     WHERE NOT EXISTS (SELECT 1 FROM route_day r
                        WHERE r.train_no = u.train_no AND r.date = ?)
       AND NOT EXISTS (SELECT 1 FROM day_skip s
                        WHERE s.code = u.code AND s.date = ?
                          AND COALESCE(s.expire, 0) > unixepoch('now'))
"""


def _uni_fingerprint() -> tuple:
    """trains 的廉价指印（14 + 0 毫秒），用来判断全集要不要重建"""
    with _conn() as c:
        row = c.execute("SELECT COUNT(*) n, MAX(date) d, MAX(rowid) r FROM trains").fetchone()
    return (row["n"], row["d"], row["r"])


def mark_universe_dirty(urgent: bool = False) -> None:
    """trains 被写过（枚举/补录）→ 下次取数前重建全集

    urgent=True：刚跑完枚举，下一轮取数必须用新全集（不能被下面的冷却期拖住）
    """
    _UNI_STATE["dirty"] = True
    if urgent:
        _UNI_STATE["urgent"] = True


def rebuild_universe(force: bool = False) -> int:
    """把「每个车次号的代表行」物化进 train_universe。没变就什么也不做"""
    with _UNI_LOCK:
        if not force and not _UNI_STATE["dirty"] and _UNI_STATE["fp"] is not None:
            if _uni_fingerprint() == _UNI_STATE["fp"]:
                return 0
        fp = _uni_fingerprint()
        with railcore._db_lock, _conn() as c:
            c.execute("DELETE FROM train_universe")
            c.execute(
                "INSERT INTO train_universe (code, train_no, type, start_sta, end_sta, d) "
                + _UNIVERSE_SQL
            )
            n = c.execute("SELECT COUNT(*) n FROM train_universe").fetchone()["n"]
        _UNI_STATE.update({"fp": fp, "ts": time.time(), "dirty": False, "urgent": False})
        return n


def _uni_ensure() -> None:
    """便宜的新鲜度检查：真变了才重建；下载过程中最多 30 秒重建一次"""
    st = _UNI_STATE
    now = time.time()
    if st["fp"] is None:                       # 冷启动：表可能还是空的
        rebuild_universe(force=True)
        return
    if not st["urgent"] and now - st["ts"] < _UNI_MIN_GAP:
        return
    if st["urgent"] or _uni_fingerprint() != st["fp"]:
        rebuild_universe(force=True)
    else:
        st["ts"] = now


def universe() -> list[dict]:
    """本地已知的全部车次（车次号 + train_no），train_no 跨日期稳定"""
    _uni_ensure()
    with _conn() as c:
        rows = c.execute(
            "SELECT code, train_no, type, start_sta, end_sta, d FROM train_universe"
        ).fetchall()
    return [dict(r) for r in rows]


def _pending(date: str) -> list[dict]:
    """该日期还没处理过的车次（既没下过经停站，也没被未到复核期的 day_skip 屏蔽）"""
    _uni_ensure()
    with _conn() as c:
        rows = c.execute(_PENDING_SQL, (date, date)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 任务对象
# ---------------------------------------------------------------------------
class Job:
    def __init__(self, date: str, workers: int = 6):
        self.date = date
        self.workers = max(1, min(8, int(workers or 6)))
        self.status = "idle"          # idle / running / paused / done / error
        self.phase = ""               # enum / stops / done
        self.total = 0
        self.done = 0
        self.failed = 0
        self.skipped = 0
        self.message = ""
        self.started_at = None
        self.updated_at = time.time()
        self.finished_at = None
        self.errors: list[str] = []
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._delay = 0.45            # 自适应：每线程每次请求后的间隔
        self.enum_future = True       # 刷新全集时是否也搜预售窗口最后一天

    def snapshot(self) -> dict:
        finished = self.done + self.failed + self.skipped
        pct = round(finished / self.total * 100, 1) if self.total else 0.0
        return {
            "date": self.date,
            "status": self.status,
            "phase": self.phase,
            "total": self.total,
            "done": self.done,
            "failed": self.failed,
            "skipped": self.skipped,
            "percent": min(100.0, pct),
            "message": self.message,
            "workers": self.workers,
            "delay": round(self._delay, 2),
            "errors": self.errors[-8:],
            "startedAt": self.started_at,
            "updatedAt": self.updated_at,
            "finishedAt": self.finished_at,
        }

    def bump(self, **kw) -> None:
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, getattr(self, k) + v)
            self.updated_at = time.time()

    def stop(self) -> None:
        self._stop.set()
        if self.status == "running":
            self.status = "paused"
            self.message = self.message or "已暂停（进度已保存，可继续）"

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()


_JOBS: dict[str, Job] = {}
_JOBS_LOCK = threading.Lock()


def busy() -> bool:
    """有任务在跑吗（决定统计缓存能放多久：闲时可以多留一会儿，跑起来就得跟得上）"""
    if _CHAIN is not None and _CHAIN.status == "running":
        return True
    with _JOBS_LOCK:
        return any(j.status == "running" for j in _JOBS.values())


# 一键连跑：刷新全集 → 从今天起按天依次下载（同一时刻只允许一个）
_CHAIN = None
_CHAIN_LOCK = threading.Lock()


def get_job(date: str) -> Job | None:
    with _JOBS_LOCK:
        return _JOBS.get(date)


# ---------------------------------------------------------------------------
# HTTP：线程独立 session
# ---------------------------------------------------------------------------
_TLS = threading.local()


def _session():
    s = getattr(_TLS, "session", None)
    if s is None:
        import requests
        s = requests.Session()
        s.headers.update({"User-Agent": rail_config.USER_AGENT})
        _TLS.session = s
    return s


def _stops(train_no: str, date: str, retries: int = 3):
    """查经停站。返回 ('ok', raw) / ('empty', None) / ('blocked', None) / ('error', None)"""
    url = f"{rail_config.API_BASE}/otn/queryTrainInfo/query"
    params = {
        "leftTicketDTO.train_no": train_no,
        "leftTicketDTO.train_date": date,
        "rand_code": "",
    }
    last = "error"
    empty_seen = 0
    for i in range(retries):
        try:
            r = _session().get(url, params=params, timeout=20)
            if r.status_code == 403:
                return ("blocked", None)
            r.raise_for_status()
            data = (r.json() or {}).get("data") or {}
            raw = data.get("data") or None
            if raw:
                return ("ok", raw)
            # 12306 在「尚未放票 / 接口抖动」时也会回一个空 body。
            # 空一次就判「当天不开行」会把后来开放的车次漏掉，所以再确认一遍。
            empty_seen += 1
            if empty_seen >= 2:
                return ("empty", None)
            time.sleep(0.4)
        except Exception:                                # noqa: BLE001
            last = "error"
            time.sleep(0.6 * (i + 1))
    return (last, None)


def _search(keyword: str, date_compact: str, retries: int = 2):
    """搜索接口。返回 ('ok', list) / ('blocked', None) / ('error', None)"""
    url = f"{rail_config.SEARCH_API_BASE}/search/v1/train/search"
    for i in range(retries):
        try:
            r = _session().get(url, params={"keyword": keyword, "date": date_compact},
                               timeout=20)
            if r.status_code == 403:
                return ("blocked", None)
            r.raise_for_status()
            return ("ok", (r.json() or {}).get("data") or [])
        except Exception:                                # noqa: BLE001
            time.sleep(1.0 * (i + 1))
    return ("error", None)


def _detect_type(code: str) -> str | None:
    """只把「车次号首字母」存进 type，不做车种白名单。

    改号后新字头会陆续出现（V / N / X / B …），白名单会把它们全部丢掉：
    2026-11-25 的 V/N 新车次就是这样「下载完查不到」的。type 只是展示用的
    首字母别名（纯数字沿用历史写法 "N"），禁止用它推断车种 / 速度。
    """
    if not code:
        return None
    c = code[0].upper()
    if c.isalpha():
        return c
    if c.isdigit():
        return "N"
    return None


def _prefix(t: str, n: int) -> str:
    """搜索词：字母字头 → "V1"；DIGIT_MARK → 纯数字 "1" """
    return str(n) if t == DIGIT_MARK else f"{t}{n}"


def enum_prefixes() -> list[str]:
    """全集刷新要发的全部搜索词（字母字头 1..9 + 纯数字 1..9）"""
    return ([_prefix(t, n) for t in TRAIN_TYPES for n in range(1, 10)]
            + list(DIGIT_PREFIXES))


def refine_prefixes(p: str) -> list[str]:
    """单个前缀顶到单页上限时再细分一层：V1 → V10..V19；3 → 30..39"""
    m = re.match(r"^([A-Za-z]?)(\d+)$", p or "")
    if not m:
        return []
    letter, num = m.group(1), int(m.group(2))
    t = letter or DIGIT_MARK
    return [_prefix(t, num * 10 + k) for k in range(10)]


# ---------------------------------------------------------------------------
# 写库
# ---------------------------------------------------------------------------
def _save_trains(date: str, items) -> None:
    rows = [(i["code"], i["train_no"], i["type"], i["start"], i["end"], i["stop_count"], date)
            for i in items if i.get("code")]
    if not rows:
        return
    with railcore._db_lock, _conn() as c:
        c.executemany(
            """INSERT INTO trains (code, train_no, type, start_sta, end_sta, stop_count, date)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(code, date) DO UPDATE SET
                 train_no=excluded.train_no,
                 type=excluded.type,
                 start_sta=excluded.start_sta,
                 end_sta=excluded.end_sta,
                 stop_count=COALESCE(excluded.stop_count, trains.stop_count)""",
            rows,
        )
    mark_universe_dirty(urgent=True)          # 枚举刚拿到的车次，下一次取数就得看见


def _save_batch(date: str, rows) -> None:
    """rows: (train_no, code, type, start, end, n_stops, raw_json)

    经停站走 routestore：内容与这趟车已有版本相同就只记一条指针，
    只有停站真变了才新存一份（每天照抓不误，存的是「是否一致」的结论）。
    """
    if not rows:
        return
    with railcore._db_lock, _conn() as c:
        routestore.put_routes_for_date(
            c, date, [(r[0], r[1], r[6]) for r in rows])
        # 已经拿到经停站 = 这趟车这天确实开行，旧的「不开行」标记必须撤掉，
        # 否则历史页会出现「既不开行又有站序」的自相矛盾。
        c.executemany("DELETE FROM day_skip WHERE date=? AND code=?",
                      [(date, r[1]) for r in rows])
        c.executemany(
            """INSERT INTO trains (code, train_no, type, start_sta, end_sta, stop_count, date)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(code, date) DO UPDATE SET
                 train_no=excluded.train_no,
                 type=COALESCE(excluded.type, trains.type),
                 start_sta=COALESCE(excluded.start_sta, trains.start_sta),
                 end_sta=COALESCE(excluded.end_sta, trains.end_sta),
                 stop_count=excluded.stop_count""",
            [(r[1], r[0], r[2], r[3], r[4], r[5], date) for r in rows],
        )
    mark_universe_dirty()


def _save_skip(date: str, codes) -> None:
    """记下「这次没查到经停站」，只在复核期内屏蔽，绝不写成永久结论"""
    codes = list(dict.fromkeys(codes))
    if not codes:
        return
    now = time.time()
    with railcore._db_lock, _conn() as c:
        ph = ",".join("?" * len(codes))
        old = {r["code"]: (r["hits"] or 1) for r in c.execute(
            f"SELECT code, hits FROM day_skip WHERE date=? AND code IN ({ph})",
            [date, *codes])}
        rows = []
        for c0 in codes:
            hits = old.get(c0, 0) + 1
            rows.append((date, c0, now, _skip_ttl(date, hits, now), hits))
        c.executemany(
            """INSERT INTO day_skip (date, code, ts, expire, hits) VALUES (?,?,?,?,?)
               ON CONFLICT(date, code) DO UPDATE SET
                 ts=excluded.ts, expire=excluded.expire, hits=excluded.hits""",
            rows,
        )


def _release_stale_skips(date: str, budget: int = SKIP_RECHECK_BUDGET) -> int:
    """每轮下载放行一批「已到复核期」的标记，剩下的延后一小时等下一轮。

    优先级：当日车次清单里还查得到这趟车的（最可能是后来开放/当时漏判）> 判定得最早的。
    不做预算的话，几万条历史标记会在一次连跑里全涌出来。
    """
    now = time.time()
    with railcore._db_lock, _conn() as c:
        picked = c.execute(
            """SELECT s.code FROM day_skip s
                WHERE s.date=? AND s.expire > 0 AND s.expire <= ?
                ORDER BY EXISTS (SELECT 1 FROM trains t
                                  WHERE t.code=s.code AND t.date=s.date) DESC,
                         s.ts ASC
                LIMIT ?""", (date, now, budget)).fetchall()
        # 先整体延后，再把选中的放行；expire=0 的行会被 _pending 纳入本轮重查
        c.execute("""UPDATE day_skip SET expire=?
                      WHERE date=? AND expire>0 AND expire<=?""",
                  (now + 3600, date, now))
        c.executemany("UPDATE day_skip SET expire=0 WHERE date=? AND code=?",
                      [(date, r["code"]) for r in picked])
    return len(picked)


def skip_stale_stats(date: str | None = None) -> dict:
    """还没复核的「不开行」标记数（前端用来显示「另有 N 趟待复核」）"""
    with _conn() as c:
        if date:
            total = c.execute("SELECT COUNT(*) n FROM day_skip WHERE date=?", (date,)).fetchone()["n"]
            stale = c.execute(
                """SELECT COUNT(*) n FROM day_skip
                    WHERE date=? AND expire>0 AND expire<=unixepoch('now')""",
                (date,)).fetchone()["n"]
            return {"date": date, "total": total, "stale": stale}
        total = c.execute("SELECT COUNT(*) n FROM day_skip").fetchone()["n"]
        stale = c.execute(
            """SELECT COUNT(*) n FROM day_skip
                WHERE expire>0 AND expire<=unixepoch('now') AND date>=?""",
            (dt.date.today().isoformat(),)).fetchone()["n"]
        return {"date": "", "total": total, "stale": stale}


def recheck_skips(date: str | None = None) -> dict:
    """手动把「不开行」标记全部放行，下一轮下载会挨个重查（发车日已过的不动）"""
    today = dt.date.today().isoformat()
    with railcore._db_lock, _conn() as c:
        if date and date.lower() != "all":
            n = c.execute("UPDATE day_skip SET expire=0 WHERE date=?", (date,)).rowcount
        else:
            n = c.execute("UPDATE day_skip SET expire=0 WHERE date>=?", (today,)).rowcount
    _STAT_CACHE.clear()
    _BULK["data"] = None
    return {"released": n, "date": date or "all"}


# ---------------------------------------------------------------------------
# 阶段 B：下载经停站（主流程）
# ---------------------------------------------------------------------------
def download_stops(job: Job, date: str) -> None:
    released = _release_stale_skips(date)
    todo = _pending(date)
    job.phase = "stops"
    job.total = len(todo)
    job.done = 0
    job.failed = 0
    job.skipped = 0
    extra = f"（含复核旧标记 {released} 趟）" if released else ""
    job.message = f"正在下载经停站：本轮 {len(todo)} 趟待处理（{job.workers} 线程）{extra}"
    job.updated_at = time.time()
    _persist(job)

    if not todo:
        job.phase = "done"
        job.status = "done"
        job.finished_at = time.time()
        job.message = "全部车次已处理完毕（本地库已是最新）"
        _persist(job)
        return

    buf: list[tuple] = []
    skipbuf: list[str] = []
    buf_lock = threading.Lock()
    consec = [0]
    err_lock = threading.Lock()

    def flush() -> None:
        with buf_lock:
            rows, buf[:] = list(buf), []
            skips, skipbuf[:] = list(skipbuf), []
        _save_batch(date, rows)
        _save_skip(date, skips)

    def work(u):
        if job.stopped:
            return "stop", u
        status, raw = _stops(u["train_no"], date)
        time.sleep(job._delay)
        if status == "ok":
            with buf_lock:
                buf.append((u["train_no"], u["code"], u.get("type"), u.get("start_sta"),
                            u.get("end_sta"), len(raw),
                            json.dumps(raw, ensure_ascii=False)))
                size = len(buf)
            if size >= 40:
                flush()
            with err_lock:
                consec[0] = 0
                if job._delay > THROTTLE_BASE:      # 恢复顺利就慢慢提速回来
                    job._delay = max(THROTTLE_BASE, job._delay - 0.002)
            return "ok", u
        if status == "empty":                 # 这次没查到，记进带复核期限的 day_skip
            with buf_lock:
                skipbuf.append(u["code"])
                size = len(skipbuf)
            if size >= 80:
                flush()
            with err_lock:
                consec[0] = 0
            return "skip", u
        # 失败 / 被封
        with err_lock:
            consec[0] += 1
            n = consec[0]
            if n >= 5:
                job._delay = min(THROTTLE_MAX, job._delay * 1.35)
                _note_throttle(job._delay)
        if status == "blocked" or n >= 25:
            _note_throttle(max(job._delay, 1.5))     # 被封了，下次起步就慢一点
            job.message = f"连续 {n} 次失败（疑似 12306 限流），已暂停。稍后点「继续」即可接上"
            job.stop()
            return "blocked", u
        return "fail", u

    with ThreadPoolExecutor(max_workers=job.workers) as ex:
        futs = [ex.submit(work, it) for it in todo]
        for f in as_completed(futs):
            try:
                kind, u = f.result()
            except Exception as exc:                     # noqa: BLE001
                kind, u = "fail", None
                job.errors.append(str(exc)[:160])
            if kind == "ok":
                job.bump(done=1)
            elif kind == "skip":
                job.bump(skipped=1)
            elif kind in ("fail", "blocked"):
                job.bump(failed=1)
            if job.stopped:
                break
    flush()

    if job.stopped:
        job.status = "paused"
        job.message = job.message or "已暂停（进度已保存，可继续）"
    else:
        job.status = "done"
        job.finished_at = time.time()
        job.message = (f"完成：开行 {job.done} 趟、当时未查到 {job.skipped} 趟、"
                       f"失败 {job.failed} 趟")
    job.phase = "done" if job.status == "done" else "stops"
    _log(date, "stops", job.message)
    _persist(job)


# ---------------------------------------------------------------------------
# 可选：刷新车次全集（12306 搜索接口，限流很凶，单线程慢速）
# ---------------------------------------------------------------------------
def enum_dates(date: str, future: bool = True) -> list[str]:
    """刷新全集要搜哪几天。

    12306 按「开行日」展示车次，预售期 15 天。2026-11-25 改号后启用的
    V/N/Z/Y 新车次，只拿今天去搜是搜不到的（实测 11-11 搜 V1/N1 都是 0 条，
    11-25 分别 200 / 89 条）。所以除下载起始日外还要搜预售窗口最后一天：
    改号切换日只要落在窗口里，就一定在最后一天或之前。
    搜到的车次按「搜索的那一天」分别登记，curate 的 date >= 切换日 才看得见。
    """
    out = [date]
    if future:
        horizon = (dt.date.today() + dt.timedelta(days=DATE_WINDOW - 1)).isoformat()
        if horizon > date:
            out.append(horizon)
    return out


def enumerate_trains(job: Job, date: str, delay: float = 2.0,
                     future: bool = True) -> int:
    """刷新车次全集。future=True 时同时搜预售窗口最后一天，抓改号后的新车次。

    关键点：搜索结果按「搜索日期」分别写进 trains，不能一股脑全写到 job.date，
    否则 11-25 的新号会登记在 11-11，待校对清单按切换日过滤时永远看不到。
    """
    dates = enum_dates(date, future)
    job.phase = "enum"
    more = (f"；并同时搜预售最后一天 {dates[-1]}（改号后新车次只在那一天开行）"
            if len(dates) > 1 else "")
    job.message = ("正在刷新车次全集（搜索接口单线程慢速，这步最容易 403）…" + more)
    _log(date, "enum_try", "开始刷新车次全集：" + ",".join(dates))
    prefixes = enum_prefixes()
    job.total = len(prefixes) * len(dates)
    job.done = 0
    blocked = 0
    buckets: dict[str, dict[str, dict]] = {d: {} for d in dates}

    def take(res, bucket):
        for item in res:
            code = (item.get("station_train_code") or "").strip()
            if not code:
                continue
            t = _detect_type(code)
            if t is None:
                continue
            bucket[code] = {
                "code": code,
                "train_no": (item.get("train_no") or "").strip(),
                "type": t,
                "start": (item.get("from_station") or "").strip(),
                "end": (item.get("to_station") or "").strip(),
                "stop_count": int(item.get("total_num") or 0) or None,
            }

    def run(plist, search_date: str, bucket, refine=None):
        nonlocal blocked
        dc = search_date.replace("-", "")
        for p in plist:
            if job.stopped:
                return
            status, res = _search(p, dc)
            if status == "blocked":
                blocked += 1
                job.message = f"搜索接口限流（403），冷却 {30 * blocked}s 后重试…"
                _persist(job)
                time.sleep(30 * blocked)
                status, res = _search(p, dc)
            if status == "ok":
                take(res, bucket)
                blocked = 0
                # 只有顶到单页上限（200 条）的前缀才需要再细分一次，
                # 以前是全部词都细分 → 上千个请求，白白多挨 403
                if refine is not None and len(res) >= FULL_RESULT_THRESHOLD:
                    refine.append(p)
            job.bump(done=1)
            time.sleep(delay)

    for d in dates:
        bucket = buckets[d]
        need: list[str] = []
        run(prefixes, d, bucket, need)
        _save_trains(d, bucket.values())

        # 只细分「这个日期顶到上限」的那几个前缀，细分请求仍用同一日期
        p2: list[str] = []
        for p in need:
            p2.extend(refine_prefixes(p))
        if not job.stopped and p2:
            job.total += len(p2)
            job.message = f"{d} 细分 {len(need)} 个顶到上限的前缀（{len(p2)} 个请求）…"
            run(p2, d, bucket)
            _save_trains(d, bucket.values())

    all_codes = set()
    for b in buckets.values():
        all_codes.update(b.keys())
    per = " / ".join(f"{d}: {len(buckets[d])}" for d in dates)
    job.message = f"车次全集已刷新，本次拿到 {len(all_codes)} 趟（{per}）"
    _log(date, "enum", job.message)
    return len(all_codes)


# ---------------------------------------------------------------------------
# 任务调度
# ---------------------------------------------------------------------------
def _persist(job: Job) -> None:
    with _conn() as c:
        n_trains = c.execute("SELECT COUNT(*) n FROM trains WHERE date=?", (job.date,)).fetchone()["n"]
        n_stops = c.execute("SELECT COUNT(*) n FROM route_day WHERE date=?", (job.date,)).fetchone()["n"]
    with _conn() as c:
        c.execute(
            """INSERT INTO day_collect (date,status,phase,trains,stops,failed,workers,
                   started_at,updated_at,finished_at,message)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(date) DO UPDATE SET
                 status=excluded.status, phase=excluded.phase, trains=excluded.trains,
                 stops=excluded.stops, failed=excluded.failed, workers=excluded.workers,
                 started_at=COALESCE(day_collect.started_at, excluded.started_at),
                 updated_at=excluded.updated_at, finished_at=excluded.finished_at,
                 message=excluded.message""",
            (job.date, job.status, job.phase, n_trains, n_stops, job.failed,
             job.workers, job.started_at, time.time(), job.finished_at, job.message),
        )
    if job.status != "running":        # 跑完了：列表里那几个数别再被缓存冻住
        _STAT_CACHE.clear()
        _BULK["ts"] = 0.0


def _runner(job: Job, do_enum: bool) -> None:
    try:
        job.started_at = job.started_at or time.time()
        if do_enum:
            enumerate_trains(job, job.date,
                             future=getattr(job, "enum_future", True))
            _persist(job)
        if job.stopped:
            job.status = "paused"
            job.message = job.message or "已暂停"
            _persist(job)
            return
        download_stops(job, job.date)
    except Exception as exc:                             # noqa: BLE001
        import traceback
        traceback.print_exc()
        job.status = "error"
        job.message = f"{type(exc).__name__}: {exc}"
        _log(job.date, "error", job.message)
        _persist(job)


def _reset_job(job: Job, workers: int, enum_future: bool | None = None) -> None:
    """把某一天的 Job 复位到「准备开跑」状态（单独下载 / 连跑共用）"""
    job.workers = max(1, min(8, int(workers or 6)))
    if enum_future is not None:
        job.enum_future = bool(enum_future)
    job._stop.clear()
    job.status = "running"
    job.phase = ""
    job.done = job.failed = job.skipped = 0
    job.total = 0
    job.errors = []
    job.message = "准备中…"
    job.finished_at = None
    job.started_at = time.time()
    # 关键：不要写死 0.45。刚被限流过的话，从这里接着慢起步，否则一开就又被封
    job._delay = learned_delay()


def _ensure_job(date: str, workers: int = 6) -> Job:
    with _JOBS_LOCK:
        job = _JOBS.get(date)
        if job is None:
            job = Job(date, workers)
            _JOBS[date] = job
        return job


def start(date: str, workers: int = 6, do_enum: bool = False,
          enum_future: bool = True) -> dict:
    try:
        d = dt.date.fromisoformat(date)
    except ValueError:
        raise ValueError("日期格式应为 YYYY-MM-DD")
    offset = (d - dt.date.today()).days
    if not (0 <= offset < DATE_WINDOW):
        raise ValueError(
            f"只能下载含今天在内 {DATE_WINDOW} 天内的车次（今天 ~ 第 {DATE_WINDOW} 天）：{date} 超出范围"
        )
    ch = _CHAIN
    if ch and ch.status == "running":
        raise ValueError(f"一键连跑正在进行（{ch.cur_date}），先「暂停连跑」再单独操作某一天")

    job = _ensure_job(date, workers)
    if job.status == "running":
        return job.snapshot()
    _reset_job(job, workers, enum_future)
    _persist(job)
    t = threading.Thread(target=_runner, args=(job, do_enum), daemon=True)
    job._thread = t
    t.start()
    return job.snapshot()


def stop(date: str) -> dict:
    job = get_job(date)
    if job:
        job.stop()
        _persist(job)
        return job.snapshot()
    return {"date": date, "status": "idle"}


def status(date: str) -> dict:
    base = date_stat(date)
    job = get_job(date)
    if not job:
        return base
    snap = job.snapshot()
    out = dict(base)
    for k in ("status", "phase", "total", "done", "skipped", "failed", "workers",
              "delay", "errors", "startedAt", "updatedAt", "finishedAt", "message"):
        v = snap.get(k)
        if v not in (None, ""):
            out[k] = v
    return out


_STAT_CACHE: dict[str, tuple[float, dict]] = {}
_STAT_TTL = 2.0              # 有任务在跑：2 秒就过期，进度要跟手
_STAT_TTL_IDLE = 30.0        # 闲着：库不会变，多留一会儿，点开就是秒开

# 整窗统计的一次性结果（15 天一起查，比逐天查快 20 倍）；轮询时直接复用
_BULK_TTL = 2.5
_BULK_TTL_IDLE = 60.0
_BULK: dict = {"key": None, "ts": 0.0, "data": None}


def date_stat(date: str) -> dict:
    """从库里统计某天的现状（服务重启后也能看到）。带缓存，避免轮询打爆库"""
    hit = _STAT_CACHE.get(date)
    ttl = _STAT_TTL if busy() else _STAT_TTL_IDLE
    if hit and time.time() - hit[0] < ttl:
        return dict(hit[1])
    _uni_ensure()
    with _conn() as c:
        # 「已确认开行」= 已拿到经停站的车次（枚举进来但没验证的不算）
        trains = c.execute("SELECT COUNT(*) n FROM route_day WHERE date=?", (date,)).fetchone()["n"]
        uni = c.execute("SELECT COUNT(*) n FROM train_universe").fetchone()["n"]
        pending = c.execute(
            "SELECT COUNT(*) n FROM (" + _PENDING_SQL + ")", (date, date)
        ).fetchone()["n"]
        stale = c.execute(
            """SELECT COUNT(*) n FROM day_skip
                WHERE date=? AND expire>0 AND expire<=unixepoch('now')""",
            (date,)).fetchone()["n"]
        skip_tot = c.execute(
            """SELECT COUNT(*) n FROM day_skip
                WHERE date=? AND expire>0 AND expire<?""",
            (date, SKIP_FOREVER)).fetchone()["n"]
        row = c.execute("SELECT * FROM day_collect WHERE date=?", (date,)).fetchone()
    d = _stat_from(date, trains=trains, uni_total=uni, pending=pending,
                   stale=stale, skip_total=skip_tot,
                   dc_row=(dict(row) if row else None))
    _STAT_CACHE[date] = (time.time(), d)
    return dict(d)


def _stat_from(date: str, *, trains: int, uni_total: int, pending: int,
               stale: int = 0, skip_total: int = 0,
               dc_row: dict | None) -> dict:
    """把「已下载 / 全集 / 待处理 / day_collect 行」拼成前端要的统计块"""
    st = dc_row or {}
    status = st.get("status") or ("done" if trains and not pending else "idle")
    msg = st.get("message") or ""
    if status == "running":          # 服务重启 / 进程中断留下的残状态
        status = "paused"
        if not msg or msg == "准备中…":
            msg = "上次任务被中断（服务重启），进度已保留，点「继续」即可接上"
    return {
        "date": date,
        "status": status,
        "phase": st.get("phase") or "",
        "trains": trains,
        "stops": trains,
        "pending": pending,
        "stale": stale,               # 已到复核期，本轮会被放行的趟数（受预算限制）
        "skipTotal": skip_total,      # 该日所有仍可复核的旧标记
        "universe": uni_total,
        "total": uni_total,
        "done": trains,
        "failed": st.get("failed") or 0,
        "skipped": 0,
        "percent": round((uni_total - pending) / uni_total * 100, 1) if uni_total else 0.0,
        "message": msg,
        "workers": st.get("workers") or 6,
        "delay": round(learned_delay(), 2),
        "errors": [],
        "startedAt": st.get("started_at"),
        "updatedAt": st.get("updated_at"),
        "finishedAt": st.get("finished_at"),
    }


def _bulk_basis(dates: list[str]) -> dict:
    """一次查完整窗：已下载车次数 / 待处理数 / day_collect 行 / 全集规模"""
    key = (dates[0], dates[-1], len(dates))
    ttl = _BULK_TTL if busy() else _BULK_TTL_IDLE
    if _BULK["data"] is not None and _BULK["key"] == key and time.time() - _BULK["ts"] < ttl:
        return _BULK["data"]
    _uni_ensure()
    lo, hi = dates[0], dates[-1]
    with _conn() as c:
        uni = c.execute("SELECT COUNT(*) n FROM train_universe").fetchone()["n"]
        trains = {r["date"]: r["n"] for r in c.execute(
            "SELECT date, COUNT(*) n FROM route_day WHERE date BETWEEN ? AND ? GROUP BY date",
            (lo, hi))}
        dc = {r["date"]: dict(r) for r in c.execute(
            "SELECT * FROM day_collect WHERE date BETWEEN ? AND ?", (lo, hi))}
        pend = {}
        for d in dates:
            pend[d] = c.execute(
                "SELECT COUNT(*) n FROM (" + _PENDING_SQL + ")", (d, d)).fetchone()["n"]
        stale = {r["date"]: r["n"] for r in c.execute(
            """SELECT date, COUNT(*) n FROM day_skip
                WHERE date BETWEEN ? AND ? AND expire>0 AND expire<=unixepoch('now')
                GROUP BY date""", (lo, hi))}
        skip_tot = {r["date"]: r["n"] for r in c.execute(
            """SELECT date, COUNT(*) n FROM day_skip
                WHERE date BETWEEN ? AND ? AND expire>0 AND expire<?
                GROUP BY date""", (lo, hi, SKIP_FOREVER))}
    data = {"uni": uni, "trains": trains, "dc": dc, "pending": pend,
            "stale": stale, "skipTot": skip_tot}
    _BULK.update({"key": key, "ts": time.time(), "data": data})
    return data


def avail_dates() -> list[dict]:
    today = dt.date.today()
    dates = [(today + dt.timedelta(days=i)).isoformat() for i in range(DATE_WINDOW)]
    basis = _bulk_basis(dates)
    out = []
    for i, ds in enumerate(dates):
        s = _stat_from(ds, trains=basis["trains"].get(ds, 0), uni_total=basis["uni"],
                       pending=basis["pending"].get(ds, 0),
                       stale=basis["stale"].get(ds, 0),
                       skip_total=basis["skipTot"].get(ds, 0),
                       dc_row=basis["dc"].get(ds))
        job = get_job(ds)
        if job:                                  # 内存里的实时进度优先
            snap = job.snapshot()
            for k in ("status", "phase", "total", "done", "skipped", "failed", "workers",
                      "delay", "errors", "startedAt", "updatedAt", "finishedAt", "message"):
                v = snap.get(k)
                if v not in (None, ""):
                    s[k] = v
        s["weekday"] = WEEKDAYS[(today + dt.timedelta(days=i)).weekday()]
        s["isToday"] = (i == 0)
        s["offset"] = i
        out.append(s)
    return out


# ---------------------------------------------------------------------------
# 一键连跑：先刷新车次全集，再从今天起按天依次下载
# ---------------------------------------------------------------------------
class Chain:
    """一条流水线：enum（刷新全集，只第一天做）→ 第 1 天 → 第 2 天 → …"""

    def __init__(self, plan: list[str], workers: int = 6, do_enum: bool = True,
                 enum_future: bool = True):
        self.plan = list(plan)
        self.workers = max(1, min(8, int(workers or 6)))
        self.do_enum = bool(do_enum)
        self.enum_future = bool(enum_future)
        self.index = 0
        self.status = "idle"          # idle / running / paused / done / error
        self.phase = ""               # enum / stops / gap
        self.message = ""
        self.note = ""                # 附加说明（比如「全集刚刷过，本次跳过」）
        self.started_at = time.time()
        self.updated_at = time.time()
        self.finished_at = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = None

    @property
    def cur_date(self) -> str | None:
        return self.plan[self.index] if 0 <= self.index < len(self.plan) else None

    def snapshot(self) -> dict:
        n = len(self.plan)
        pos = n if self.status == "done" else self.index
        return {
            "status": self.status,
            "phase": self.phase,
            "index": pos,
            "total": n,
            "curDate": self.cur_date,
            "plan": self.plan,
            "workers": self.workers,
            "enum": self.do_enum,
            "enumFuture": self.enum_future,
            "percent": round(pos / n * 100, 1) if n else 0.0,
            "message": self.message,
            "note": self.note,
            "startedAt": self.started_at,
            "updatedAt": self.updated_at,
            "finishedAt": self.finished_at,
        }


def _chain_persist(chain: Chain) -> None:
    # 被新连跑顶掉的旧线程不要再写库，免得把新版状态盖成旧的
    if _CHAIN is not None and _CHAIN is not chain:
        return
    try:
        with _conn() as c:
            c.execute(
                """INSERT INTO chain_run (id,status,phase,cur_date,idx,total,plan,workers,
                       started_at,updated_at,finished_at,message)
                   VALUES (1,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     status=excluded.status, phase=excluded.phase, cur_date=excluded.cur_date,
                     idx=excluded.idx, total=excluded.total, plan=excluded.plan,
                     workers=excluded.workers, started_at=excluded.started_at,
                     updated_at=excluded.updated_at, finished_at=excluded.finished_at,
                     message=excluded.message""",
                (chain.status, chain.phase, chain.cur_date, chain.index, len(chain.plan),
                 json.dumps(chain.plan), chain.workers, chain.started_at,
                 time.time(), chain.finished_at, chain.message),
            )
    except Exception:                                   # noqa: BLE001
        pass


def _chain_restore() -> None:
    """服务重启后把上一次连跑的断点读回来（running 的降级成 paused，可继续）"""
    global _CHAIN
    try:
        with _conn() as c:
            row = c.execute("SELECT * FROM chain_run WHERE id=1").fetchone()
    except sqlite3.Error:
        return
    if not row:
        return
    try:
        plan = json.loads(row["plan"] or "[]")
    except json.JSONDecodeError:
        plan = []
    if not plan:
        return
    ch = Chain(plan, row["workers"] or 6, False)
    ch.status = row["status"] or "idle"
    ch.phase = row["phase"] or ""
    ch.index = max(0, min(int(row["idx"] or 0), len(plan)))
    ch.message = row["message"] or ""
    ch.started_at = row["started_at"] or time.time()
    ch.updated_at = row["updated_at"] or time.time()
    ch.finished_at = row["finished_at"]
    if ch.status == "running":
        ch.status = "paused"
        ch.message = "上次连跑被中断（服务重启），点「继续连跑」接着跑"
    _CHAIN = ch


def enum_last_ts() -> float:
    """上次「刷新全集」的时间戳（0 = 从没刷过）。失败/中断的也算，避免反复重试同一个坑"""
    try:
        with _conn() as c:
            row = c.execute(
                "SELECT MAX(ts) AS t FROM collect_log WHERE kind IN ('enum','enum_try')"
            ).fetchone()
        return float(row["t"] or 0.0)
    except (sqlite3.Error, TypeError, ValueError):
        return 0.0


def universe_size() -> int:
    _uni_ensure()
    with _conn() as c:
        return c.execute("SELECT COUNT(*) n FROM train_universe").fetchone()["n"]


def _ago(sec: float) -> str:
    sec = max(0.0, sec)
    if sec < 90:
        return "刚刚"
    m = int(sec // 60)
    if m < 60:
        return f"{m} 分钟"
    h = m / 60
    return f"{h:.0f} 小时" if h < 48 else f"{h / 24:.0f} 天"


def _chain_plan(start: str | None = None) -> list[str]:
    """从 start（默认今天）到窗口最后一天的所有日期"""
    today = dt.date.today()
    d0 = today
    if start:
        try:
            d0 = max(today, dt.date.fromisoformat(start))
        except ValueError:
            raise ValueError("日期格式应为 YYYY-MM-DD")
    horizon = today + dt.timedelta(days=DATE_WINDOW - 1)
    out: list[str] = []
    d = d0
    while d <= horizon:
        out.append(d.isoformat())
        d += dt.timedelta(days=1)
    if not out:
        raise ValueError(f"可下载窗口只剩今天起 {DATE_WINDOW} 天，没有可跑的日期")
    return out


def _chain_runner(chain: Chain) -> None:
    n = len(chain.plan)
    try:
        for i in range(chain.index, n):
            if chain._stop.is_set():
                break
            date = chain.plan[i]
            with chain._lock:
                chain.index = i
                chain.phase = "enum" if (i == 0 and chain.do_enum) else "stops"
                chain.updated_at = time.time()
                chain.message = ("正在刷新车次全集（搜索接口慢且容易 403，先耐心等这一步）…"
                                 if chain.phase == "enum"
                                 else f"第 {i + 1}/{n} 天 · {date} · 下载经停站")
            _chain_persist(chain)

            job = _ensure_job(date, chain.workers)
            _reset_job(job, chain.workers,
                       getattr(chain, "enum_future", True))
            _persist(job)
            _runner(job, do_enum=(chain.phase == "enum"))

            with chain._lock:
                chain.updated_at = time.time()
            if chain._stop.is_set():
                break
            if job.status != "done":
                chain.status = "paused" if job.status in ("paused", "running", "idle") else "error"
                chain.message = (f"{date} 没跑完：{job.message or job.status}。"
                                 f"点「继续连跑」从这一天接上")
                _log(date, "chain", chain.message)
                return

            # 换天冷却：连跑是几小时的持续请求，中间不留缝就容易被当成压测
            if i + 1 < n:
                gap = CHAIN_GAP
                if job.failed or learned_delay() > THROTTLE_BASE:
                    gap = CHAIN_GAP_BAD
                with chain._lock:
                    chain.phase = "gap"
                    chain.message = (f"第 {i + 1}/{n} 天（{date}）完成，"
                                     f"冷却 {gap}s 后接着下 {chain.plan[i + 1]}…")
                    chain.updated_at = time.time()
                _chain_persist(chain)
                if chain._stop.wait(gap):
                    break
        else:
            chain.status = "done"
            chain.index = n
            chain.finished_at = time.time()
            chain.message = f"连跑完成：{n} 天全部处理完毕"
            _log(chain.plan[-1] if n else "", "chain", chain.message)
            return

        chain.status = "paused"
        chain.message = chain.message or "连跑已暂停（进度已保存，点「继续连跑」可接上）"
    except Exception as exc:                             # noqa: BLE001
        import traceback
        traceback.print_exc()
        chain.status = "error"
        chain.message = f"{type(exc).__name__}: {exc}"
        _log(chain.cur_date or "", "chain", chain.message)
    finally:
        chain.updated_at = time.time()
        _chain_persist(chain)


def chain_start(workers: int = 6, do_enum: bool = True,
                start: str | None = None, force_enum: bool = False,
                enum_future: bool = True) -> dict:
    """开始一键连跑。start 给了就从那天开始（用于「继续连跑」）

    force_enum=True：用户明确要求「开头先刷新全集」，不再按 7 天冷却自动跳过。
    enum_future=True：刷新全集时一并搜预售窗口最后一天（改号后的新车次）。
    """
    global _CHAIN
    plan = _chain_plan(start)
    with _CHAIN_LOCK:
        cur = _CHAIN
        if cur and cur.status == "running":
            return cur.snapshot()
        for d in plan:
            j = get_job(d)
            if j and j.status == "running":
                raise ValueError(f"{d} 正在单独下载，先「暂停」它再开连跑")
        skipped_note = ""
        age = time.time() - enum_last_ts()
        if do_enum and not force_enum:
            if universe_size() > ENUM_MIN_UNIVERSE and age < ENUM_MIN_INTERVAL:
                do_enum = False
                ago = _ago(age)
                skipped_note = (f"车次全集{'刚刚' if ago == '刚刚' else ' ' + ago + '前'}刷过"
                                f"（本地 {universe_size()} 趟），本次跳过刷新这一步"
                                f" —— 它最容易被 403；要强制刷：勾上「先刷新全集」再点连跑")
        ch = Chain(plan, workers, do_enum, enum_future)
        ch.status = "running"
        ch.note = skipped_note
        ch.message = ("正在刷新车次全集…" if do_enum
                      else (skipped_note or f"准备下载 {len(plan)} 天…"))
        if do_enum:
            horizon = plan[-1] if plan else ""
            fh = (f"；同时搜预售最后一天 {horizon}，把改号后只在那天开行的新车次也收进来"
                  if enum_future else "；本次只搜起始日（不会抓到切换日后的新车次）")
            ch.note = ("开头先刷新车次全集（搜索接口要跑十几分钟，且最容易被 403；"
                       "刷完 15 天都直接下）" + fh)
            if force_enum and age < ENUM_MIN_INTERVAL:
                ch.note = (f"按要求先刷新车次全集（上次是 {_ago(age)}前）；"
                           "搜索接口最容易被 403，这一步慢是正常的，刷完 15 天都直接下"
                           + fh)
        _CHAIN = ch
        _chain_persist(ch)
        t = threading.Thread(target=_chain_runner, args=(ch,), daemon=True)
        ch._thread = t
        t.start()
        return ch.snapshot()


def chain_stop() -> dict:
    ch = _CHAIN
    if not ch:
        return {"status": "idle", "index": 0, "total": 0}
    ch._stop.set()
    d = ch.cur_date
    if d:
        job = get_job(d)
        if job and job.status == "running":
            job.stop()
            _persist(job)
    if ch.status == "running":
        ch.status = "paused"
        ch.message = "连跑已暂停（进度已保存，点「继续连跑」可接上）"
    ch.updated_at = time.time()
    _chain_persist(ch)
    return ch.snapshot()


def chain_status() -> dict:
    ch = _CHAIN
    if ch:
        return ch.snapshot()
    return {"status": "idle", "phase": "", "index": 0, "total": 0,
            "curDate": None, "plan": [], "percent": 0.0, "message": "", "note": "",
            "startedAt": None, "updatedAt": None, "finishedAt": None}


# ---------------------------------------------------------------------------
# 历史数据：已落库的日期 / 某日的车次清单
# ---------------------------------------------------------------------------
def _has_json_array_length() -> bool:
    with _conn() as c:
        try:
            c.execute("SELECT json_array_length('[1,2]')")
            return True
        except sqlite3.Error:
            return False


_JSON_FN: bool | None = None


def _json_ok() -> bool:
    global _JSON_FN
    if _JSON_FN is None:
        _JSON_FN = _has_json_array_length()
    return _JSON_FN


_HIST_TTL = 60.0
_HIST: dict = {"key": None, "ts": 0.0, "data": None}


def history_dates() -> list[dict]:
    """库里所有有数据的日期（不限于可下载窗口），按日期倒序

    停站总数走「模板 n_stops 求和」，不再对 33 万行 raw 做 json_array_length
    （那个要 3 秒多）；整个结果再按库的指印缓存 60 秒。
    """
    with _conn() as c:
        fp = c.execute("SELECT COUNT(*) n, MAX(date) d FROM route_day").fetchone()
    key = (fp["n"], fp["d"])
    if _HIST["data"] is not None and _HIST["key"] == key and time.time() - _HIST["ts"] < _HIST_TTL:
        return [dict(x) for x in _HIST["data"]]

    with _conn() as c:
        route_rows = c.execute("SELECT date, COUNT(*) n FROM route_day GROUP BY date").fetchall()
        train_rows = c.execute("SELECT date, COUNT(*) n FROM trains GROUP BY date").fetchall()
        dc_rows = c.execute("SELECT * FROM day_collect").fetchall()
        tpl_stops = {r["id"]: (r["n_stops"] or 0)
                     for r in c.execute("SELECT id, n_stops FROM route_tpl")}
        acc: dict[str, int] = {}
        for r in c.execute("SELECT date, tpl_id FROM route_day"):
            acc[r["date"]] = acc.get(r["date"], 0) + tpl_stops.get(r["tpl_id"], 0)

    routes = {r["date"]: dict(r) for r in route_rows}
    trains = {r["date"]: r["n"] for r in train_rows}
    stops = acc
    dc = {r["date"]: dict(r) for r in dc_rows}

    today = dt.date.today().isoformat()
    out = []
    for date in sorted(set(routes) | set(trains), reverse=True):
        d = dt.date.fromisoformat(date)
        r = routes.get(date, {})
        rec = dc.get(date, {})
        n_route = r.get("n", 0)
        out.append({
            "date": date,
            "weekday": WEEKDAYS[d.weekday()],
            "routes": n_route,                     # 已下载经停站的车次
            "stops": int(stops.get(date, 0) or 0),
            "listed": trains.get(date, 0),         # trains 表登记的车次
            "status": rec.get("status") or ("done" if n_route else "idle"),
            "updatedAt": rec.get("updated_at") or None,
            "isPast": date < today,
            "isToday": date == today,
            "offset": (d - dt.date.today()).days,
        })
    _HIST.update({"key": key, "ts": time.time(), "data": out})
    return [dict(x) for x in out]


def history_trains(date: str, q: str = "", limit: int = 40, offset: int = 0) -> dict:
    """某日已入库的车次清单，带「是否已下载经停站」标记"""
    q = (q or "").strip()
    where = ["t.date = ?"]
    args: list = [date]
    if q:
        like = f"%{q}%"
        where.append("(t.code LIKE ? OR t.start_sta LIKE ? OR t.end_sta LIKE ?)")
        args += [f"{q}%", like, like]
    sql_where = " AND ".join(where)
    with _conn() as c:
        total = c.execute(f"SELECT COUNT(*) n FROM trains t WHERE {sql_where}", args).fetchone()["n"]
        rows = c.execute(
            f"""SELECT t.code, t.train_no, t.type, t.start_sta, t.end_sta, t.stop_count,
                       (r.train_no IS NOT NULL) AS has_route,
                       {("json_array_length(r.raw)" if _json_ok() else "NULL")} AS stops
                FROM trains t
                LEFT JOIN routes r ON r.train_no = t.train_no AND r.date = t.date
                WHERE {sql_where}
                ORDER BY has_route DESC, LENGTH(t.code), t.code
                LIMIT ? OFFSET ?""",
            args + [max(1, min(200, limit)), max(0, offset)],
        ).fetchall()
    return {
        "date": date,
        "q": q,
        "total": total,
        "offset": offset,
        "items": [dict(r) for r in rows],
    }


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------
def _dump(date: str) -> dict:
    trains = []
    with _conn() as c:
        rows = c.execute(
            "SELECT t.code, t.train_no, t.type, t.start_sta, t.end_sta, t.stop_count, r.raw "
            "FROM trains t LEFT JOIN routes r ON r.train_no=t.train_no AND r.date=t.date "
            "WHERE t.date=? ORDER BY t.type, t.code", (date,)
        ).fetchall()
    for r in rows:
        stops = []
        if r["raw"]:
            try:
                stops = json.loads(r["raw"])
            except json.JSONDecodeError:
                stops = []
        trains.append({
            "code": r["code"],
            "trainNo": r["train_no"],
            "type": r["type"],
            "from": r["start_sta"],
            "to": r["end_sta"],
            "stopCount": r["stop_count"] or (len(stops) or None),
            "stops": stops,
        })
    return {
        "date": date,
        "generatedAt": dt.datetime.now().isoformat(timespec="seconds"),
        "trainCount": len(trains),
        "stopCount": sum(len(t["stops"]) for t in trains),
        "trains": trains,
    }


def export(date: str, fmt: str = "json") -> tuple[str, int]:
    fmt = (fmt or "json").lower()
    base = f"railcn_{date}"
    if fmt == "json":
        path = os.path.join(EXPORT_DIR, base + ".json")
        data = _dump(date)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    elif fmt == "csv":
        path = os.path.join(EXPORT_DIR, base + "_stops.csv")
        data = _dump(date)
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["日期", "车次", "车次类型", "始发", "终到",
                        "站序", "站名", "到站", "发车", "停留", "运行(分)", "里程(km)", "天数偏移"])
            for t in data["trains"]:
                for i, s in enumerate(t["stops"], 1):
                    w.writerow([date, t["code"], t["type"] or "", t["from"] or "", t["to"] or "",
                                i, s.get("station_name", ""), s.get("arrive_time", ""),
                                s.get("start_time", ""), s.get("stopover_time", ""),
                                s.get("running_time", ""), s.get("distance") or "",
                                s.get("arrive_day_diff", "")])
    else:
        raise ValueError("不支持的格式：" + fmt)
    return path, os.path.getsize(path)


def export_list() -> list[dict]:
    out = []
    if not os.path.isdir(EXPORT_DIR):
        return out
    for name in sorted(os.listdir(EXPORT_DIR), reverse=True):
        p = os.path.join(EXPORT_DIR, name)
        if os.path.isfile(p):
            out.append({"name": name, "size": os.path.getsize(p),
                        "mtime": dt.datetime.fromtimestamp(os.path.getmtime(p))
                        .strftime("%Y-%m-%d %H:%M")})
    return out


init_db()
_chain_restore()
