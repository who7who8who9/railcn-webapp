# -*- coding: utf-8 -*-
"""「不开行」可复核机制回归：迁移 / 期限 / 放行 / 二次确认 / 历史页口径。

跑法：python scripts/test_skip_recheck.py
全部在临时 SQLite 副本上做，不碰 webapp/data/rail.db，也不依赖 8686 服务。
"""
import datetime as dt
import os
import shutil
import sqlite3
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "webapp"))

TMPDIR = tempfile.mkdtemp(prefix="railcn-skip-test-")
TMPDB = os.path.join(TMPDIR, "rail.db")

TODAY = dt.date.today()
D_FUT = (TODAY + dt.timedelta(days=3)).isoformat()   # 远期
D_NEAR = (TODAY + dt.timedelta(days=1)).isoformat()  # 临期
D_PAST = (TODAY - dt.timedelta(days=2)).isoformat()  # 已过去

OK, BAD = 0, []


def check(name, cond, extra=""):
    global OK
    if cond:
        OK += 1
        print("  ok  ", name)
    else:
        BAD.append(name)
        print("  FAIL", name, extra)


# ---------------------------------------------------------------------------
# 造一个「老版本」的库：day_skip 只有 date/code/ts，还带着自相矛盾的旧结论
# ---------------------------------------------------------------------------
c = sqlite3.connect(TMPDB)
c.executescript("""
    CREATE TABLE day_skip (date TEXT NOT NULL, code TEXT NOT NULL, ts REAL,
                           PRIMARY KEY(date, code));
    CREATE TABLE trains (code TEXT NOT NULL, train_no TEXT, type TEXT,
                         start_sta TEXT, end_sta TEXT, stop_count INTEGER, date TEXT NOT NULL,
                         PRIMARY KEY(code, date));
    CREATE TABLE route_day (train_no TEXT, date TEXT, code TEXT, tpl_id INTEGER,
                            updated_at REAL);
    CREATE TABLE route_tpl (id INTEGER PRIMARY KEY, train_no TEXT, raw TEXT, n_stops INTEGER);
    CREATE TABLE train_universe (code TEXT PRIMARY KEY, train_no TEXT, type TEXT,
                                 start_sta TEXT, end_sta TEXT, d TEXT);
    CREATE VIEW routes AS
        SELECT d.train_no, d.date, d.code, t.raw
          FROM route_day d JOIN route_tpl t ON t.id=d.tpl_id;
""")
TS = time.time() - 86400 * 5
c.executemany("INSERT INTO trains VALUES(?,?,?,?,?,?,?)", [
    ("G1", "TN1", "G", "北京南", "上海虹桥", 5, D_FUT),   # 当日车次清单里有 → 旧结论错
])
c.executemany("INSERT INTO train_universe VALUES(?,?,?,?,?,?)", [
    ("G1", "TN1", "G", "北京南", "上海虹桥", D_FUT),
    ("G2", "TN2", "G", "北京", "天津", D_FUT),
    ("G3", "TN3", "D", "济南", "青岛", D_NEAR),
    ("G4", "TN4", "D", "杭州", "宁波", D_NEAR),
])
c.execute("INSERT INTO route_tpl VALUES(1,'TN4','[]',2)")
c.execute("INSERT INTO route_day VALUES('TN4',?,?,1,?)", (D_FUT, "G4", TS))
c.executemany("INSERT INTO day_skip VALUES(?,?,?)", [
    (D_FUT, "G1", TS),    # 清单里还有这趟 → 应删
    (D_FUT, "G4", TS),    # 已经有经停站 → 应删
    (D_FUT, "G2", TS),    # 真「当时未查到」→ 保留
    (D_NEAR, "G3", TS),   # 临期 → 保留
    (D_PAST, "G2", TS),   # 历史日 → 保留且定为终态
])
c.commit()
c.close()

import railcore  # noqa: E402
railcore.DB_PATH = TMPDB
railcore.DATA_DIR = TMPDIR          # 迁移备份也写进临时目录，别弄脏项目
import collector  # noqa: E402      # 模块底部 init_db() 会跑迁移
import historyq  # noqa: E402


def q(sql, args=()):
    with sqlite3.connect(TMPDB) as cc:
        cc.row_factory = sqlite3.Row
        return [dict(r) for r in cc.execute(sql, args)]


def one(sql, args=()):
    r = q(sql, args)
    return r[0] if r else None


print("\n== 1. 老库迁移 ==")
cols = [r["name"] for r in q("PRAGMA table_info(day_skip)")]
check("补出 expire 列", "expire" in cols, cols)
check("补出 hits 列", "hits" in cols, cols)
rows = q("SELECT * FROM day_skip")
check("删掉 2 条自相矛盾的旧结论", len(rows) == 3, rows)
check("清单里还有的车不再被标不开行",
      one("SELECT * FROM day_skip WHERE date=? AND code='G1'", (D_FUT,)) is None)
check("已有经停站的车不再被标不开行",
      one("SELECT * FROM day_skip WHERE date=? AND code='G4'", (D_FUT,)) is None)
check("真·未查到的记录保留", one("SELECT * FROM day_skip WHERE date=? AND code='G2'", (D_FUT,)) is not None)
past = one("SELECT * FROM day_skip WHERE date=?", (D_PAST,))
check("发车日已过 → 定为终态", past and past["expire"] == collector.SKIP_FOREVER, past)
check("未来日期 → 有限复核期", all(r["expire"] < collector.SKIP_FOREVER
                                 for r in rows if r["date"] >= TODAY.isoformat()))
check("老记录 hits 初始化为 2", all(r["hits"] == 2 for r in rows), rows)
bak = os.path.join(TMPDIR, "backup")
check("删掉的行有 CSV 备份",
      any("dropped_skip.csv" in f for _d, _s, fs in os.walk(bak) for f in fs))
n_before = len(rows)
collector.init_db()
check("迁移只跑一次（重复 init_db 不再删）", len(q("SELECT * FROM day_skip")) == n_before)

print("\n== 2. 复核期限 _skip_ttl ==")
now = time.time()
check("发车日已过 → 永久",
      collector._skip_ttl(D_PAST, 1, now) == collector.SKIP_FOREVER)
check("当天发车 → 6 小时",
      abs(collector._skip_ttl(TODAY.isoformat(), 1, now) - (now + collector.SKIP_TTL_NEAR)) < 1)
check("远期发车 → 1 天",
      abs(collector._skip_ttl(D_FUT, 1, now) - (now + collector.SKIP_TTL_DAY)) < 1)
check("连续查空 → 指数退避",
      abs(collector._skip_ttl(D_FUT, 2, now) - (now + 2 * collector.SKIP_TTL_DAY)) < 1)
check("退避不超过 7 天",
      collector._skip_ttl(D_FUT, 9, now) - now <= collector.SKIP_TTL_MAX + 1)

print("\n== 3. 屏蔽 / 放行 / 待处理 ==")
# 迁移之后再把这几趟补进「当日车次清单」：_pending 会调 _uni_ensure 重建全集，
# 临时库里得先有这些 trains 行，否则全集只剩 G1，测不出屏蔽效果。
with sqlite3.connect(TMPDB) as cc:
    cc.executemany("INSERT OR IGNORE INTO trains VALUES(?,?,?,?,?,?,?)", [
        ("G2", "TN2", "G", "北京", "天津", 3, D_FUT),
        ("G3", "TN3", "D", "济南", "青岛", 3, D_NEAR),
        ("G4", "TN4", "D", "杭州", "宁波", 2, D_FUT),
    ])
collector.mark_universe_dirty(urgent=True)
pend0 = collector._pending(D_FUT)
check("未到期的旧标记仍屏蔽下载", all(x["code"] != "G2" for x in pend0), pend0)
check("被删掉错误标记的车回到待处理", "G1" in {x["code"] for x in pend0}, pend0)
check("已有经停站的车不重复下", "G4" not in {x["code"] for x in pend0}, pend0)

with sqlite3.connect(TMPDB) as cc:
    cc.execute("UPDATE day_skip SET expire=? WHERE date=? AND code='G2'", (now - 10, D_FUT))
check("到期标记计入 skip_stale_stats",
      collector.skip_stale_stats(D_FUT)["stale"] == 1, collector.skip_stale_stats(D_FUT))
n = collector._release_stale_skips(D_FUT, budget=1)
check("按预算只放行 1 条", n == 1, n)
r2 = one("SELECT * FROM day_skip WHERE date=? AND code='G2'", (D_FUT,))
check("放行的标记 expire=0", r2 and r2["expire"] == 0, r2)
check("放行后该车回到待处理", "G2" in {x["code"] for x in collector._pending(D_FUT)})

with sqlite3.connect(TMPDB) as cc:
    cc.execute("UPDATE day_skip SET expire=? WHERE date=? AND code='G3'", (now - 10, D_NEAR))
    cc.execute("UPDATE day_skip SET expire=? WHERE date=? AND code='G2'", (now - 10, D_PAST))
collector._release_stale_skips(D_NEAR, budget=0)
r3 = one("SELECT * FROM day_skip WHERE date=? AND code='G3'", (D_NEAR,))
check("没轮上的到期标记延后一小时", r3 and r3["expire"] > now, r3)

print("\n== 4. 再次查空 / 下载成功 ==")
collector._save_skip(D_FUT, ["G2"])
r4 = one("SELECT * FROM day_skip WHERE date=? AND code='G2'", (D_FUT,))
check("重查仍空 → hits 递增", r4 and r4["hits"] == 3, r4)
check("重查仍空 → 重新屏蔽（expire 在未来）", r4 and r4["expire"] > now, r4)
collector._save_skip(D_FUT, ["G2", "G2"])
r5 = one("SELECT * FROM day_skip WHERE date=? AND code='G2'", (D_FUT,))
check("同一批去重，hits 只加一次", r5 and r5["hits"] == 4, r5)

collector.routestore.put_routes_for_date = lambda conn, date, rows: None  # 只测删标记
collector._save_batch(D_FUT, [("TN2", "G2", "G", "北京", "天津", 3, "[]")])
check("下载到经停站 → 撤掉旧的未查到标记",
      one("SELECT * FROM day_skip WHERE date=? AND code='G2'", (D_FUT,)) is None)

print("\n== 5. 手动复核接口 ==")
with sqlite3.connect(TMPDB) as cc:      # 上一节 _save_batch 把 G2 的标记删了，补一条回来
    cc.execute("INSERT OR REPLACE INTO day_skip VALUES(?,?,?,?,?)",
               (D_FUT, "G2", now, now + 99999, 2))
res = collector.recheck_skips(D_FUT)
check("单日复核返回放行条数", res["released"] >= 1, res)
check("单日复核后该日标记全部 expire=0",
      all(r["expire"] == 0 for r in q("SELECT * FROM day_skip WHERE date=?", (D_FUT,))))
past2 = one("SELECT * FROM day_skip WHERE date=?", (D_PAST,))
check("全量复核不动历史日", collector.recheck_skips("all") and
      one("SELECT * FROM day_skip WHERE date=?", (D_PAST,))["expire"] == past2["expire"])

print("\n== 6. 空结果二次确认 ==")
class FakeResp:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("http")

    def json(self):
        return {"data": {"data": self.payload}}


class FakeSession:
    def __init__(self, seq, status=200):
        self.seq, self.status, self.calls = seq, status, 0

    def get(self, *a, **kw):
        v = self.seq[min(self.calls, len(self.seq) - 1)]
        self.calls += 1
        return FakeResp(v, self.status)


def probe(seq, status=200):
    s = FakeSession(seq, status)
    collector._session = lambda: s
    return collector._stops("TN1", D_FUT), s.calls


(r6, calls6) = probe([None, None])
check("连续两次空才判 empty", r6[0] == "empty" and calls6 == 2, (r6, calls6))
(r7, calls7) = probe([None, [{"station_name": "北京"}]])
check("第二次拿到数据 → 不误判不开行", r7[0] == "ok" and calls7 == 2, (r7, calls7))
(r8, calls8) = probe([[{"station_name": "北京"}]])
check("一次就有数据不重复请求", r8[0] == "ok" and calls8 == 1, (r8, calls8))
(r9, _c9) = probe([None], status=403)
check("403 仍判 blocked", r9[0] == "blocked", r9)

print("\n== 7. 历史页口径 ==")
with sqlite3.connect(TMPDB) as cc:      # 先清掉上一节留下的待复核标记
    cc.execute("DELETE FROM day_skip WHERE date=? AND code='G2'", (D_FUT,))
d = historyq.stops_of("G2", D_FUT)
check("无标记时 reason 是「还没下载」", "还没下载" in d["reason"], d["reason"])
with sqlite3.connect(TMPDB) as cc:
    cc.execute("INSERT OR REPLACE INTO day_skip VALUES(?,?,?,?,?)", (D_FUT, "G2", now, 0, 1))
d = historyq.stops_of("G2", D_FUT)
check("已放行待复核 → 不再说「不开行」", "待复核" in d["reason"] and "不开行" not in d["reason"], d["reason"])
with sqlite3.connect(TMPDB) as cc:
    cc.execute("UPDATE day_skip SET expire=? WHERE date=? AND code='G2'", (now + 99999, D_FUT))
d = historyq.stops_of("G2", D_FUT)
check("仍在屏蔽期 → 提示可能后来开放", "可能后来开放" in d["reason"], d["reason"])

print(f"\n结果：通过 {OK} 项，失败 {len(BAD)} 项")
if BAD:
    print("失败清单：", BAD)
shutil.rmtree(TMPDIR, ignore_errors=True)
sys.exit(1 if BAD else 0)
