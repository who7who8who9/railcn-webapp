# -*- coding: utf-8 -*-
"""
collector.py — 按日期批量下载「全部车次 + 经停站」，数据永久落库

设计要点
--------
1. **不依赖 12306 搜索接口**（它限流很凶，403 一挂就是几十分钟）。
   利用「train_no 是车次固定编号、跨日期不变」这一点：
   本地库里已有 1.7 万条 (车次号, train_no) 的车次全集，
   对目标日期逐个查经停站 —— 有数据就说明当天开行，没数据就记进 day_skip 不再重试。
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
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import railcore  # noqa: E402
import config as rail_config  # noqa: E402

EXPORT_DIR = os.path.join(railcore.DATA_DIR, "exports")
os.makedirs(EXPORT_DIR, exist_ok=True)

TRAIN_TYPES = ["G", "D", "C", "Z", "T", "K", "S", "Y", "N"]   # N = 纯数字车次
FULL_RESULT_THRESHOLD = 195      # 搜索接口单页上限约 200，达到则继续细分
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
DATE_WINDOW = 15                 # 含今天在内共 15 天（offset 0..14）


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
            -- 某天确认「不开行」的车次，避免每次续传都重查
            CREATE TABLE IF NOT EXISTS day_skip (
                date TEXT NOT NULL,
                code TEXT NOT NULL,
                ts   REAL,
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
            """
        )


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
def universe() -> list[dict]:
    """本地已知的全部车次（车次号 + train_no），train_no 跨日期稳定"""
    with _conn() as c:
        rows = c.execute(
            """SELECT code, train_no, type, start_sta, end_sta, stop_count, MAX(date) AS d
               FROM trains WHERE train_no IS NOT NULL AND train_no <> ''
               GROUP BY code"""
        ).fetchall()
    return [dict(r) for r in rows]


def _pending(date: str) -> list[dict]:
    """该日期还没处理过的车次（既没下过经停站，也没被标记为不开行）"""
    uni = universe()
    with _conn() as c:
        done = {r[0] for r in c.execute("SELECT train_no FROM routes WHERE date=?", (date,))}
        skip = {r[0] for r in c.execute("SELECT code FROM day_skip WHERE date=?", (date,))}
    return [u for u in uni if u["train_no"] not in done and u["code"] not in skip]


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
    for i in range(retries):
        try:
            r = _session().get(url, params=params, timeout=20)
            if r.status_code == 403:
                return ("blocked", None)
            r.raise_for_status()
            data = (r.json() or {}).get("data") or {}
            raw = data.get("data") or None
            return ("ok", raw) if raw else ("empty", None)
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
    if not code:
        return None
    c = code[0]
    if c.isalpha() and c.upper() in TRAIN_TYPES:
        return c.upper()
    if c.isdigit():
        return "N"
    return None


def _prefix(t: str, n: int) -> str:
    return str(n) if t == "N" else f"{t}{n}"


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


def _save_batch(date: str, rows) -> None:
    """rows: (train_no, code, type, start, end, n_stops, raw_json)"""
    if not rows:
        return
    now = time.time()
    with railcore._db_lock, _conn() as c:
        c.executemany(
            "INSERT OR REPLACE INTO routes (train_no, date, code, raw, updated_at)"
            " VALUES (?,?,?,?,?)",
            [(r[0], date, r[1], r[6], now) for r in rows],
        )
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


def _save_skip(date: str, codes) -> None:
    if not codes:
        return
    now = time.time()
    with railcore._db_lock, _conn() as c:
        c.executemany(
            "INSERT OR REPLACE INTO day_skip (date, code, ts) VALUES (?,?,?)",
            [(date, c0, now) for c0 in codes],
        )


# ---------------------------------------------------------------------------
# 阶段 B：下载经停站（主流程）
# ---------------------------------------------------------------------------
def download_stops(job: Job, date: str) -> None:
    todo = _pending(date)
    job.phase = "stops"
    job.total = len(todo)
    job.done = 0
    job.failed = 0
    job.skipped = 0
    job.message = f"正在下载经停站：本轮 {len(todo)} 趟待处理（{job.workers} 线程）"
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
            return "ok", u
        if status == "empty":                 # 当天不开行，记下来别再查
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
                job._delay = min(3.0, job._delay * 1.35)
        if status == "blocked" or n >= 25:
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
        job.message = (f"完成：开行 {job.done} 趟、当天不开行 {job.skipped} 趟、"
                       f"失败 {job.failed} 趟")
    job.phase = "done" if job.status == "done" else "stops"
    _log(date, "stops", job.message)
    _persist(job)


# ---------------------------------------------------------------------------
# 可选：刷新车次全集（12306 搜索接口，限流很凶，单线程慢速）
# ---------------------------------------------------------------------------
def enumerate_trains(job: Job, date: str, delay: float = 2.0) -> int:
    job.phase = "enum"
    job.message = "正在刷新车次全集（12306 搜索接口较慢，约 5~10 分钟）…"
    date_compact = date.replace("-", "")
    found: dict[str, dict] = {}
    blocked = 0

    prefixes = [_prefix(t, n) for t in TRAIN_TYPES for n in range(1, 10)]
    job.total = len(prefixes)
    job.done = 0

    def take(res):
        for item in res:
            code = (item.get("station_train_code") or "").strip()
            if not code:
                continue
            t = _detect_type(code)
            if t is None:
                continue
            found[code] = {
                "code": code,
                "train_no": (item.get("train_no") or "").strip(),
                "type": t,
                "start": (item.get("from_station") or "").strip(),
                "end": (item.get("to_station") or "").strip(),
                "stop_count": int(item.get("total_num") or 0) or None,
            }

    def run(plist):
        nonlocal blocked
        for p in plist:
            if job.stopped:
                return
            status, res = _search(p, date_compact)
            if status == "blocked":
                blocked += 1
                job.message = f"搜索接口限流（403），冷却 {30 * blocked}s 后重试…"
                _persist(job)
                time.sleep(30 * blocked)
                status, res = _search(p, date_compact)
            if status == "ok":
                take(res)
                blocked = 0
            job.bump(done=1)
            time.sleep(delay)

    run(prefixes)
    _save_trains(date, found.values())

    # 返回量接近上限的前缀继续细分
    p2 = []
    for t in TRAIN_TYPES:
        for n in range(1, 10):
            p2.extend(_prefix(t, n * 10 + k) for k in range(10))
    if not job.stopped:
        job.total = len(p2)
        job.done = 0
        run(p2)
        _save_trains(date, found.values())

    job.message = f"车次全集已刷新，本次拿到 {len(found)} 趟"
    _log(date, "enum", job.message)
    return len(found)


# ---------------------------------------------------------------------------
# 任务调度
# ---------------------------------------------------------------------------
def _persist(job: Job) -> None:
    with _conn() as c:
        n_trains = c.execute("SELECT COUNT(*) n FROM trains WHERE date=?", (job.date,)).fetchone()["n"]
        n_stops = c.execute("SELECT COUNT(*) n FROM routes WHERE date=?", (job.date,)).fetchone()["n"]
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


def _runner(job: Job, do_enum: bool) -> None:
    try:
        job.started_at = job.started_at or time.time()
        if do_enum:
            enumerate_trains(job, job.date)
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


def start(date: str, workers: int = 6, do_enum: bool = False) -> dict:
    try:
        d = dt.date.fromisoformat(date)
    except ValueError:
        raise ValueError("日期格式应为 YYYY-MM-DD")
    offset = (d - dt.date.today()).days
    if not (0 <= offset < DATE_WINDOW):
        raise ValueError(
            f"只能下载含今天在内 {DATE_WINDOW} 天内的车次（今天 ~ 第 {DATE_WINDOW} 天）：{date} 超出范围"
        )

    with _JOBS_LOCK:
        job = _JOBS.get(date)
        if job and job.status == "running":
            return job.snapshot()
        if job is None:
            job = Job(date, workers)
            _JOBS[date] = job
    job.workers = max(1, min(8, int(workers or 6)))
    job._stop.clear()
    job.status = "running"
    job.phase = ""
    job.done = job.failed = job.skipped = 0
    job.total = 0
    job.message = "准备中…"
    job.finished_at = None
    job.started_at = job.started_at or time.time()
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
_STAT_TTL = 2.0


def date_stat(date: str) -> dict:
    """从库里统计某天的现状（服务重启后也能看到）。带 2 秒缓存，避免轮询打爆库"""
    hit = _STAT_CACHE.get(date)
    if hit and time.time() - hit[0] < _STAT_TTL:
        return dict(hit[1])
    d = _date_stat(date)
    _STAT_CACHE[date] = (time.time(), d)
    return dict(d)


def _date_stat(date: str) -> dict:
    with _conn() as c:
        # 「已确认开行」= 已拿到经停站的车次（枚举进来但没验证的不算）
        trains = c.execute("SELECT COUNT(*) n FROM routes WHERE date=?", (date,)).fetchone()["n"]
        total_uni = c.execute(
            "SELECT COUNT(DISTINCT code) n FROM trains WHERE train_no IS NOT NULL AND train_no<>''"
        ).fetchone()["n"]
        pending = c.execute(
            """SELECT COUNT(*) n FROM (
                   SELECT code, train_no FROM trains
                   WHERE train_no IS NOT NULL AND train_no<>'' GROUP BY code
               ) u
               WHERE NOT EXISTS (SELECT 1 FROM routes r WHERE r.train_no=u.train_no AND r.date=?)
                 AND NOT EXISTS (SELECT 1 FROM day_skip s WHERE s.code=u.code AND s.date=?)""",
            (date, date),
        ).fetchone()["n"]
        row = c.execute("SELECT * FROM day_collect WHERE date=?", (date,)).fetchone()
    st = dict(row) if row else {}
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
        "universe": total_uni,
        "total": total_uni,
        "done": trains,
        "failed": st.get("failed") or 0,
        "skipped": 0,
        "percent": round((total_uni - pending) / total_uni * 100, 1) if total_uni else 0.0,
        "message": msg,
        "workers": st.get("workers") or 6,
        "delay": 0.45,
        "errors": [],
        "startedAt": st.get("started_at"),
        "updatedAt": st.get("updated_at"),
        "finishedAt": st.get("finished_at"),
    }


def avail_dates() -> list[dict]:
    today = dt.date.today()
    out = []
    for i in range(DATE_WINDOW):
        d = today + dt.timedelta(days=i)
        s = status(d.isoformat())
        s["weekday"] = WEEKDAYS[d.weekday()]
        s["isToday"] = (i == 0)
        s["offset"] = i
        out.append(s)
    return out


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


def history_dates() -> list[dict]:
    """库里所有有数据的日期（不限于可下载窗口），按日期倒序"""
    with _conn() as c:
        route_rows = c.execute(
            """SELECT date, COUNT(*) n,
                      SUM(CASE WHEN raw IS NOT NULL AND raw <> '' THEN 1 ELSE 0 END) ok
               FROM routes GROUP BY date"""
        ).fetchall()
        train_rows = c.execute("SELECT date, COUNT(*) n FROM trains GROUP BY date").fetchall()
        dc_rows = c.execute("SELECT * FROM day_collect").fetchall()
        if _json_ok():
            stop_rows = c.execute(
                """SELECT date, SUM(json_array_length(raw)) s FROM routes
                   WHERE raw IS NOT NULL AND raw <> '' GROUP BY date"""
            ).fetchall()
        else:
            stop_rows = []

    routes = {r["date"]: dict(r) for r in route_rows}
    trains = {r["date"]: r["n"] for r in train_rows}
    stops = {r["date"]: (r["s"] or 0) for r in stop_rows}
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
    return out


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
