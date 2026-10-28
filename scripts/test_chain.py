# -*- coding: utf-8 -*-
"""一键连跑流程自测：用临时库 + 假接口，验证「刷新全集 → 按天依次下载 → 暂停 → 继续」"""
import datetime as dt
import os
import sqlite3
import sys
import tempfile
import threading
import time

WEBAPP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webapp")
sys.path.insert(0, WEBAPP)

import railcore  # noqa: E402
import routestore  # noqa: E402

TMP = os.path.join(tempfile.gettempdir(), "railcn_chain_test.db")
for p in (TMP, TMP + "-wal", TMP + "-shm"):
    if os.path.exists(p):
        os.remove(p)
railcore.DB_PATH = TMP

with sqlite3.connect(TMP) as c:
    c.executescript(
        """
        CREATE TABLE trains (
            code TEXT NOT NULL, train_no TEXT, type TEXT,
            start_sta TEXT, end_sta TEXT, stop_count INTEGER, date TEXT,
            PRIMARY KEY (code, date));
        """
    )
    routestore.ensure_schema(c)      # 经停站表：route_tpl / route_day / routes 视图

import collector  # noqa: E402

UNI = [
    {"code": "G1", "train_no": "T1", "type": "G", "start_sta": "北京南", "end_sta": "上海虹桥"},
    {"code": "G2", "train_no": "T2", "type": "G", "start_sta": "北京南", "end_sta": "上海虹桥"},
    {"code": "K3", "train_no": "T3", "type": "K", "start_sta": "北京", "end_sta": "莫斯科"},
]
with sqlite3.connect(TMP) as c:                 # 车次全集直接落库，走真实 SQL 路径
    c.executemany(
        "INSERT INTO trains (code,train_no,type,start_sta,end_sta,stop_count,date)"
        " VALUES (?,?,?,?,?,?,?)",
        [(u["code"], u["train_no"], u["type"], u["start_sta"], u["end_sta"], 2,
          dt.date.today().isoformat()) for u in UNI],
    )

ENUM_CALLS = []
REAL_ENUM = collector.enumerate_trains        # 备份真身，第 6 步要用


def fake_enum(job, date, delay=2.0):
    ENUM_CALLS.append(date)
    job.phase = "enum"
    job.message = "假枚举完成"
    return len(UNI)


def fake_stops(slow=0.0, empty_for=()):
    def _f(train_no, date, retries=3):
        if slow:
            time.sleep(slow)
        if train_no in empty_for:
            return ("empty", None)
        return ("ok", [{"station_name": "A"}, {"station_name": "B"}])
    return _f


collector.enumerate_trains = fake_enum
collector._stops = fake_stops()
# 测试里把「换天冷却」和「全集刷新间隔」压掉，否则要等十几分钟
collector.CHAIN_GAP = 0.05
collector.CHAIN_GAP_BAD = 0.05
collector.ENUM_MIN_UNIVERSE = 1

today = dt.date.today()
PLAN = [(today + dt.timedelta(days=i)).isoformat() for i in range(3)]


def counts(date):
    with sqlite3.connect(TMP) as c:
        return (c.execute("SELECT COUNT(*) FROM routes WHERE date=?", (date,)).fetchone()[0],
                c.execute("SELECT COUNT(*) FROM day_skip WHERE date=?", (date,)).fetchone()[0])


def run(ch, timeout=30):
    t = threading.Thread(target=collector._chain_runner, args=(ch,))
    t.start()
    t.join(timeout)
    assert not t.is_alive(), "连跑线程没有结束（疑似死循环）"


print("== 1. 全新连跑：先枚举再逐天 ==")
ch = collector.Chain(PLAN, 2, True)
ch.status = "running"
collector._CHAIN = ch
run(ch)
print("  status:", ch.status, "index:", ch.index, "msg:", ch.message)
print("  enum 调用:", ENUM_CALLS)
for d in PLAN:
    print("  ", d, "routes/day_skip =", counts(d))
assert ch.status == "done", ch.status
assert ch.index == len(PLAN)
assert ENUM_CALLS == [PLAN[0]], ENUM_CALLS            # 只在第一天刷一次全集
assert all(counts(d)[0] == 3 for d in PLAN)           # 每天 3 趟都下到了

print("== 2. 暂停：跑到一半停掉，状态为 paused，进度保留 ==")
collector._CHAIN = None
with sqlite3.connect(TMP) as c:
    c.execute("DELETE FROM route_day")
    c.execute("DELETE FROM route_tpl")
    c.execute("DELETE FROM day_skip")
collector._stops = fake_stops(slow=0.08)
ch2 = collector.Chain(PLAN, 1, False)
ch2.status = "running"
collector._CHAIN = ch2
t2 = threading.Thread(target=collector._chain_runner, args=(ch2,))
t2.start()
time.sleep(0.25)
snap = collector.chain_stop()
t2.join(20)
print("  status:", ch2.status, "index:", ch2.index, "msg:", ch2.message)
print("  暂停时快照:", snap["status"], snap["index"], snap["curDate"])
assert ch2.status == "paused", ch2.status
part = sum(counts(d)[0] for d in PLAN)
print("  已保存经停站:", part)
assert part > 0

print("== 3. 继续：从暂停那天接着跑，不重复枚举 ==")
collector._stops = fake_stops()
enum_before = len(ENUM_CALLS)
res = collector.chain_start(workers=2, do_enum=False, start=ch2.cur_date)
print("  chain_start ->", res["status"], res["index"], "/", res["total"],
      "from", res["plan"][0])
assert res["plan"][0] == ch2.cur_date, (res["plan"][0], ch2.cur_date)
for _ in range(600):
    if collector.chain_status()["status"] in ("done", "error", "paused"):
        break
    time.sleep(0.1)
st = collector.chain_status()
print("  最终:", st["status"], st["index"], "/", st["total"], st["message"])
assert st["status"] == "done", st["status"]
assert len(ENUM_CALLS) == enum_before, "继续时不该再刷新全集"
assert all(counts(d)[0] == 3 for d in PLAN), [counts(d) for d in PLAN]

print("== 4. 重启恢复：把 running 落库后读回应为 paused ==")
ch3 = collector.Chain(PLAN, 4, False)
ch3.status = "running"
ch3.index = 1
collector._CHAIN = ch3                       # 只有「当前这条」才允许写库
collector._chain_persist(ch3)
collector._CHAIN = None                      # 模拟进程退出
collector._chain_restore()
st = collector.chain_status()
print("  恢复 ->", st["status"], st["index"], st["curDate"], st["message"])
assert st["status"] == "paused", st["status"]
assert st["index"] == 1
assert st["curDate"] == PLAN[1]

print("\n== 5. 全集刚刷过 → 连跑不再重复刷（一次近 900 个搜索请求，最容易被 403）==")
with sqlite3.connect(TMP) as c:
    c.execute("INSERT INTO collect_log (date, ts, kind, msg) VALUES (?,?,?,?)",
              (PLAN[0], time.time(), "enum", "车次全集已刷新，本次拿到 3 趟"))
collector._CHAIN = None
collector._stops = fake_stops()
res = collector.chain_start(workers=2, do_enum=True)
print("  enum 开关:", res["enum"], "| note:", res["note"])
assert res["enum"] is False, res
assert "跳过刷新" in (res["note"] or ""), res["note"]
for _ in range(600):
    if collector.chain_status()["status"] in ("done", "error", "paused"):
        break
    time.sleep(0.05)
print("  最终:", collector.chain_status()["status"])

print("\n== 6. 刷新全集：只细分顶到单页上限的前缀（以前 891 个请求）==")
calls = []


def fake_search(keyword, date_compact, retries=2):
    calls.append(keyword)
    n = 200 if keyword == "G1" else 4        # 只有 G1 顶到上限
    return ("ok", [{"station_train_code": f"{keyword}{i:03d}",
                    "train_no": f"X{i}", "from_station": "甲",
                    "to_station": "乙", "total_num": 3} for i in range(n)])


collector._search = fake_search
collector.enumerate_trains = REAL_ENUM
job = collector.Job(PLAN[0], 4)
collector.enumerate_trains(job, PLAN[0], delay=0)
print("  搜索请求数:", len(calls), "（81 个一位前缀 + 10 个细分）")
assert len(calls) == 91, len(calls)
assert calls[81:].count("G1") == 0 and "G10" in calls[81:]

print("\n全部通过 ✓")
