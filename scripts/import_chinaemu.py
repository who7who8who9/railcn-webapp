# -*- coding: utf-8 -*-
"""
把 china-emu 抓下来的线路数据入库，作为**独立的第五套来源**，不动现有四张表。

建两张表（纯外部快照，每次导入整表重建）：
  line_ce       线路：线名、分类、类型、K 里程区间、段长、站数、里程覆盖
  line_ce_stop  站序：站名、K 里程、站/桥隧/其它、段名、序号

为什么单独建表而不并进 line_catalog：
  现有四套来源已经靠线名字符串硬绑在一起了（这轮整理要解决的正是这个问题），
  再往 line_catalog 里塞一套只会更乱。先让它独立、可对账，等你定了骨架再映射。

里程语义（重要）：
  china-emu 给的是 **K 里程（全线累计）**，不是段内里程。
  陇海铁路西宝段是 K1073 → K1256，段长 183 km。所以 km_min/km_max 都保留，
  total_km 是差值。写进 line_catalog_stop.km 时要注意口径是否一致。

用法：
    python scripts/import_chinaemu.py            # 只出报告
    python scripts/import_chinaemu.py --apply    # 写库
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from typing import List

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "webapp"))

import railcore                                                  # noqa: E402

SRC = os.path.join(ROOT, "data", "chinaemu", "lines.json")

_TAILS = ("高速铁路", "客运专线", "城际铁路", "高速线", "客专线", "铁路", "高铁", "线")
HS_RE = re.compile(r"高速|客专|客运专线|城际|高铁")

DDL = """
CREATE TABLE IF NOT EXISTS line_ce (
    name        TEXT PRIMARY KEY,   -- 线名（china-emu 的写法）
    match_key   TEXT,               -- 归一化后的比对键（高铁/普速分开，分段拆开）
    kind        TEXT,               -- 高速铁路线路 / 城际铁路线路 …
    cats        TEXT,               -- 逗号分隔：骨干线路,枢纽与联络线
    km_min      REAL,               -- 起始 K 里程
    km_max      REAL,               -- 终止 K 里程
    total_km    REAL,               -- 段长 = km_max - km_min
    n_stop      INTEGER,            -- 站行数（含线路所）
    n_station   INTEGER,            -- 其中车站行数
    n_km        INTEGER,            -- 有里程的行数
    km_desc     INTEGER,            -- 里程逆序次数（>0 表示页面混了可疑数字）
    sections    TEXT,               -- 段名，逗号分隔
    ask         TEXT,               -- 抓取时用的原始线名
    updated_at  TEXT
);
CREATE TABLE IF NOT EXISTS line_ce_stop (
    lid     TEXT NOT NULL,          -- line_ce.name
    seq     INTEGER NOT NULL,
    name    TEXT NOT NULL,
    km      REAL,
    kind    TEXT,                   -- station / work / other
    cls     TEXT,                   -- A/B/C/Q* 页面原始档位
    section TEXT,                   -- 所属段名
    PRIMARY KEY (lid, seq)
);
CREATE INDEX IF NOT EXISTS ix_ce_stop_name ON line_ce_stop(name);
"""


def core(name: str) -> str:
    s = re.sub(r"\s+", "", name or "")
    changed = True
    while changed:
        changed = False
        for t in _TAILS:
            if len(s) > len(t) and s.endswith(t):
                s = s[: -len(t)]
                changed = True
                break
    return s


def match_key(name: str) -> str:
    """与 report_chinaemu.nkey 一致：高铁/普速分开，分段线拆成「父线|段名」"""
    import lineseg
    hs = "H" if HS_RE.search(name or "") else "P"
    sp = lineseg.split(name)
    if sp:
        return hs + core(sp[0]) + "|" + sp[1]
    return hs + core(name)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="写库")
    args = ap.parse_args()

    if not os.path.exists(SRC):
        print("缺少", SRC, "——先跑 python scripts/fetch_chinaemu.py")
        sys.exit(1)
    with open(SRC, encoding="utf-8") as f:
        recs: List[dict] = json.load(f)

    conn = sqlite3.connect(railcore.DB_PATH)
    conn.row_factory = sqlite3.Row
    old = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='line_ce'").fetchone()[0]
    prev = conn.execute("SELECT COUNT(*) FROM line_ce").fetchone()[0] if old else 0

    print("准备导入 %d 条线路、%d 行站序"
          % (len(recs), sum(r["n_stop"] for r in recs)))
    if prev:
        print("  库里现有 line_ce %d 条，将整表重建" % prev)
    if not args.apply:
        print("\n（只报告，未写库。加 --apply 才落库）")
        print("  样例：")
        for r in recs[:3]:
            print("    %-20s %6s km  %3d 站行  %s"
                  % (r["name"], r["totalKm"], r["n_stop"], ",".join(r.get("cats") or [])))
        return

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    with conn:
        conn.executescript(DDL)
        conn.execute("DELETE FROM line_ce_stop")
        conn.execute("DELETE FROM line_ce")
        for r in recs:
            conn.execute(
                "INSERT OR REPLACE INTO line_ce"
                "(name,match_key,kind,cats,km_min,km_max,total_km,n_stop,n_station,n_km,"
                " km_desc,sections,ask,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["name"], match_key(r["name"]), r.get("kind"),
                 ",".join(r.get("cats") or []), r.get("kmMin"), r.get("kmMax"),
                 r.get("totalKm"), r["n_stop"], r["n_station"], r["n_km"],
                 r.get("kmDesc") or 0,
                 ",".join(s.get("name") or "" for s in (r.get("sections") or [])),
                 r.get("ask"), now))
            conn.executemany(
                "INSERT OR REPLACE INTO line_ce_stop(lid,seq,name,km,kind,cls,section)"
                " VALUES(?,?,?,?,?,?,?)",
                [(r["name"], s["seq"], s["name"], s["km"], s["kind"],
                  s.get("cls"), s.get("section")) for s in r["stops"]])

    n1 = conn.execute("SELECT COUNT(*) FROM line_ce").fetchone()[0]
    n2 = conn.execute("SELECT COUNT(*) FROM line_ce_stop").fetchone()[0]
    n3 = conn.execute("SELECT COUNT(*) FROM line_ce_stop WHERE km IS NOT NULL").fetchone()[0]
    print("\n已写入：line_ce %d 行，line_ce_stop %d 行（有里程 %d，%.1f%%）"
          % (n1, n2, n3, 100.0 * n3 / max(1, n2)))
    print("库:", railcore.DB_PATH)


if __name__ == "__main__":
    main()
