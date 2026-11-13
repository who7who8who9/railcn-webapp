# -*- coding: utf-8 -*-
"""2026-11-25 改号：刷新全集要能收进 V/N/Z/Y 新车次

根因（实测 12306）：11-11 搜 V1/N1 返回 0 条，11-25 返回 200/89 条。
旧枚举 (1) 只搜「连跑第一天」、(2) 前缀白名单没有 V/N/X/B 且把 N 当纯数字、
(3) 结果一律写到 job.date —— 于是 11-25 换号后的普速车在新旧两天里都不进库。

现在：enum_prefixes() 覆盖新字头、_detect_type 收任意字母、
enum_dates() 同时搜预售窗口最后一天、结果按搜索日期分别写 trains。

跑法： python scripts/test_renum_enum.py
（正式库只读；写盘验证用临时库 + 假搜索接口，不发外网请求）
"""
import datetime as dt
import json
import os
import sqlite3
import sys
import tempfile
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

import railcore      # noqa: E402
import routestore    # noqa: E402

TMP = os.path.join(tempfile.gettempdir(), "railcn_renum_enum_test.db")
for p in (TMP, TMP + "-wal", TMP + "-shm"):
    if os.path.exists(p):
        os.remove(p)
railcore.DB_PATH = TMP

with sqlite3.connect(TMP) as c:
    c.executescript("""
        CREATE TABLE trains (
            code TEXT NOT NULL, train_no TEXT, type TEXT,
            start_sta TEXT, end_sta TEXT, stop_count INTEGER, date TEXT,
            PRIMARY KEY (code, date));
        CREATE TABLE day_collect (
            date TEXT PRIMARY KEY, status TEXT, phase TEXT,
            trains INTEGER DEFAULT 0, stops INTEGER DEFAULT 0,
            failed INTEGER DEFAULT 0, workers INTEGER DEFAULT 6,
            started_at REAL, updated_at REAL, finished_at REAL, message TEXT);
        CREATE TABLE collect_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT, ts REAL, kind TEXT, msg TEXT);
        CREATE TABLE day_skip (
            date TEXT NOT NULL, code TEXT NOT NULL, ts REAL,
            expire REAL DEFAULT 0, hits INTEGER DEFAULT 1,
            PRIMARY KEY (date, code));
        CREATE TABLE train_universe (
            code TEXT PRIMARY KEY, train_no TEXT, type TEXT,
            start_sta TEXT, end_sta TEXT, d TEXT);
        CREATE TABLE chain_run (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            status TEXT, phase TEXT, cur_date TEXT, idx INTEGER DEFAULT 0,
            total INTEGER DEFAULT 0, plan TEXT, workers INTEGER DEFAULT 6,
            started_at REAL, updated_at REAL, finished_at REAL, message TEXT);
    """)
    routestore.ensure_schema(c)

import collector     # noqa: E402

PASS, FAIL = [], []


def ck(label, cond, extra=""):
    (PASS if cond else FAIL).append(label)
    print(("  ok   " if cond else "  FAIL ") + label +
          ("" if cond else "  ← " + str(extra)[:220]))


TODAY = dt.date.today().isoformat()
HORIZON = (dt.date.today() + dt.timedelta(days=collector.DATE_WINDOW - 1)).isoformat()
TC, HC = TODAY.replace("-", ""), HORIZON.replace("-", "")

print("=== 1. 前缀表与新字头识别 ===")
px = collector.enum_prefixes()
for t in ("V", "N", "X", "B", "G", "Z", "Y"):
    ck("前缀含 %s1..%s9" % (t, t),
       all(f"{t}{n}" in px for n in range(1, 10)))
ck("纯数字前缀 1..9 仍在", all(str(n) in px for n in range(1, 10)))
ck("N 不再被当成数字代号（_prefix('N',1)=='N1'）",
   collector._prefix("N", 1) == "N1")
ck("前缀数 = 12 字母 × 9 + 9 数字", len(px) == 117, len(px))
for code, want in (("V101", "V"), ("N218", "N"), ("X1", "X"), ("B201", "B"),
                   ("Q9", "Q"), ("G1033", "G"), ("1461", "N")):
    ck("_detect_type(%s) == %s" % (code, want),
       collector._detect_type(code) == want, collector._detect_type(code))
ck("_detect_type('') 为 None", collector._detect_type("") is None)
ck("refine V1 → V10..V19",
   collector.refine_prefixes("V1") == [f"V1{k}" for k in range(10)])
ck("refine 3 → 30..39",
   collector.refine_prefixes("3") == [f"3{k}" for k in range(10)])
ck("refine 空前缀不炸", collector.refine_prefixes("") == [])

print("\n=== 2. 搜索日期 ===")
ck("默认同时搜起始日 + 预售最后一天",
   collector.enum_dates(TODAY) == [TODAY, HORIZON])
ck("future=False 只搜起始日",
   collector.enum_dates(TODAY, False) == [TODAY])
ck("起始日就是最后一天时不重复",
   collector.enum_dates(HORIZON) == [HORIZON])

print("\n=== 3. 双日期枚举 + 按日期写库 ===")
CALLS = []


def item(code, n=0):
    return {"station_train_code": code, "train_no": "TN" + code + str(n),
            "from_station": "甲", "to_station": "乙", "total_num": 3}


def fake_search(keyword, date_compact, retries=2):
    CALLS.append((keyword, date_compact))
    if date_compact == HC:
        if keyword == "V1":                   # 顶到单页上限，触发细分
            return ("ok", [item(f"V10{i:03d}", i) for i in range(200)])
        if keyword[:1] in ("V", "N", "X", "B"):
            return ("ok", [item(keyword + "01"), item(keyword + "02")])
        return ("ok", [item(keyword + "01")])
    # 起始日 = 改号前：V/N/X/B 一律查不到（12306 实测 11-11 搜 V1/N1 返回 0 条）
    if keyword[:1] in ("V", "N", "X", "B"):
        return ("ok", [])
    return ("ok", [item(keyword + "01")])     # 起始日：旧号


collector._search = fake_search
job = collector.Job(TODAY, 2)
got = collector.enumerate_trains(job, TODAY, delay=0)
npx = len(px)
ck("请求覆盖两个日期", {d for _, d in CALLS} == {TC, HC}, {d for _, d in CALLS})
ck("请求数 = 前缀×2 + V1 细分 10 个",
   len(CALLS) == npx * 2 + 10, (len(CALLS), npx * 2 + 10))
ck("细分请求仍用最后一天的日期",
   all(d == HC for k, d in CALLS if k.startswith("V1") and len(k) == 3))
ck("job.total / job.done 与请求数一致",
   job.total == len(CALLS) and job.done == len(CALLS),
   (job.total, job.done, len(CALLS)))
ck("返回去重车次数 > 0", got > 0, got)
ck("日志写明两个日期的收获", TODAY in job.message and HORIZON in job.message,
   job.message)

with sqlite3.connect(TMP) as c:
    rows = c.execute("SELECT code, date, type FROM trains").fetchall()
new_v = [r for r in rows if r[0].startswith("V")]
new_n = [r for r in rows if r[0].startswith("N")]
old = [r for r in rows if r[1] == TODAY]
ck("新车次 V 已入库", len(new_v) > 0, len(new_v))
ck("新车次 N 已入库", len(new_n) > 0, len(new_n))
ck("V/N 新号全部登记在预售最后一天（不是今天）",
   all(r[1] == HORIZON for r in new_v + new_n),
   sorted({r[1] for r in new_v + new_n}))
ck("V/N 的 type 就是首字母（没被白名单丢掉）",
   all(r[2] == r[0][0] for r in new_v + new_n))
ck("起始日也拿到了自己的旧号", len(old) > 0, len(old))
ck("起始日没有误收 V/N 新号",
   not any(r[0][:1] in ("V", "N") for r in old))
with sqlite3.connect(TMP) as c:
    logs = c.execute("SELECT kind, msg FROM collect_log ORDER BY id").fetchall()
ck("collect_log 记录了搜索日期",
   any(HORIZON in (m or "") for _, m in logs), logs)

print("\n=== 4. 下载链路：新号能被抓到经停站、按切换日可见 ===")
# 枚举完接着下载切换日：_pending 从 train_universe 看到新号 → 抓经停站 →
# route_day / trains 都落在 11-25，curate 的 date>=切换日 才筛得到。
collector._stops = lambda train_no, date, retries=3: (
    "ok", [{"station_name": "甲"}, {"station_name": "乙"}])
job3 = collector.Job(HORIZON, 2)
job3._delay = 0
collector.download_stops(job3, HORIZON)
with sqlite3.connect(TMP) as c:
    vr = c.execute(
        "SELECT code, date FROM routes WHERE date=? AND "
        "(code LIKE 'V%' OR code LIKE 'N%')", (HORIZON,)).fetchall()
    era = c.execute(
        "SELECT code, MAX(date) FROM routes WHERE raw IS NOT NULL GROUP BY code "
        "HAVING MAX(date) >= ? AND upper(substr(code,1,1)) IN ('V','N','Z','Y')",
        (HORIZON,)).fetchall()
ck("新号被下载经停站（routes 视图有切换日的 V/N 行）", len(vr) > 0, len(vr))
ck("经停站行日期就是切换日", all(r[1] == HORIZON for r in vr))
ck("待校对按 date>=切换日 + V/N/Z/Y 能筛到新车次", len(era) > 0, len(era))
ck("筛到的都是新字头", all(r[0][:1] in ("V", "N", "Z", "Y") for r in era))

print("\n=== 5. 关掉未来日期时的旧行为 ===")
CALLS.clear()
with sqlite3.connect(TMP) as c:
    c.execute("DELETE FROM trains")
job2 = collector.Job(TODAY, 2)
got2 = collector.enumerate_trains(job2, TODAY, delay=0, future=False)
ck("只发起始日的请求", {d for _, d in CALLS} == {TC}, {d for _, d in CALLS})
ck("请求数 = 前缀数（起始日 V1 没顶上限，无细分）",
   len(CALLS) == npx, len(CALLS))
with sqlite3.connect(TMP) as c:
    rest = c.execute("SELECT code, date FROM trains").fetchall()
ck("没有把切换日的新字头写进来",
   not any(r[0][:1] in ("V", "N", "X", "B") for r in rest))
ck("写库日期全是起始日", all(r[1] == TODAY for r in rest))
ck("future=False 也拿到车次", got2 > 0, got2)

print("\n=== 6. 开关透传（静态） ===")
srv = open(os.path.join(ROOT, "webapp", "server.py"), encoding="utf-8").read()
js = open(os.path.join(ROOT, "webapp", "static", "app.js"), encoding="utf-8").read()
html = open(os.path.join(ROOT, "webapp", "static", "index.html"), encoding="utf-8").read()
col = open(os.path.join(ROOT, "webapp", "collector.py"), encoding="utf-8").read()
ck("服务端透传 future（连跑）", 'one("future", "1")' in srv)
ck("服务端透传 future（单日）", "enum_future = one(\"future\"" in srv)
ck("前端有 #dlEnumFuture 勾选框", 'id="dlEnumFuture"' in html)
ck("前端持久化勾选状态", "railcn.dlEnumFuture" in js)
ck("连跑请求带 future", "future: wantFuture ? 1 : 0" in js)
ck("单日重新枚举也带 future", "act === 'reenum'" in js and "future:" in js)
ck("枚举结果按搜索日期写库", "_save_trains(d, bucket.values())" in col)
ck("不再用白名单卡 _detect_type", "if c.isalpha():\n        return c" in col)
ck("Chain.snapshot 带 enumFuture", '"enumFuture": self.enum_future' in col)

print("\n=== 7. HTTP 只读（服务没起自动跳过） ===")
try:
    with urllib.request.urlopen("http://127.0.0.1:8686/api/collect/chain/status",
                                timeout=3) as r:
        st = json.loads(r.read().decode("utf-8"))
    if "enumFuture" in st:
        ck("连跑状态接口有 enumFuture 字段", isinstance(st["enumFuture"], bool))
    else:
        print("  skip  enumFuture（8686 还是旧进程，重启后本项才会验）")
    with urllib.request.urlopen("http://127.0.0.1:8686/api/dates", timeout=4) as r:
        d = json.loads(r.read().decode("utf-8"))
    ck("/api/dates 仍可用", bool(d.get("items")))
except Exception as e:                       # noqa: BLE001
    print("  skip  HTTP（服务未启动）：%s" % type(e).__name__)

print("\n通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
if FAIL:
    for x in FAIL:
        print("  × " + x)
    sys.exit(1)
print("全部通过 ✓")
