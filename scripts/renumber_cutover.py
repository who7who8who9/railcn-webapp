#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""2026 车次编排改革 · 切换日命令（详见 docs/renumber-2026.md）。

用法（D:/railcn 下）：
    python scripts/renumber_cutover.py --phase 1 --date 2026-11-25 [--dry-run]
    python scripts/renumber_cutover.py --phase 2 --date 2026-12-20 [--dry-run]
    python scripts/renumber_cutover.py --phase 1 --date 2026-11-25 --check 1   # 顺手勾上阶段一

做三件事（幂等，可重复跑）：
  1. 清旧校正 train_override（用户决定：直接清掉，不归档）：
     阶段一清全部旧校正；阶段二只清 G/D/C/S 字头（阶段一之后新校的 V/N/Z/Y 等保留）。
     只清 updated_at < 切换日 0 点的——切换日之后新建的校正算新车次的，不动。
  2. 清待校对老行 curate：阶段一清 date < 切换日 的；阶段二清字头 G/D/C/S 且 date < 切换日 的。
  3. 写配置（**有切换日 = 老车次全部出校对范围**）：
       renum_cutoff   切换日
       renum_phase1   '0'/'1' 阶段一是否已纳入校对（页面上可以直接勾，不用跑脚本）
       renum_phase2   '0'/'1' 阶段二是否已纳入校对
     两个都没勾 = 待校对清单为空（等新号真的出现再勾）。

清完旧校正后由人工按新车次重新校对（用户口径：所有车次重新校）。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(BASE, "webapp", "data", "rail.db")

# 各阶段纳入校对的新字头（与 webapp/curate.py 的 PHASE1_LETTERS / PHASE2_LETTERS 一致）
PHASE_LETTERS = {
    1: ["V", "N", "Z", "Y"],
    2: ["X", "G", "T", "K", "D", "C", "S"],
}
# 阶段二要清旧校正 / 清待校对行的字头（高速 / 动集 / 城际 / 市郊）
PHASE2_CLEAR = ["G", "D", "C", "S"]


def _count(conn, sql, args):
    return conn.execute(sql, args).fetchone()[0]


def main() -> int:
    ap = argparse.ArgumentParser(description="2026 车次改号切换日命令")
    ap.add_argument("--phase", type=int, choices=(1, 2), required=True,
                    help="1 = 普速 2026-11-25；2 = 高铁/动集/城际/市郊（12 月下旬，日期待公布）")
    ap.add_argument("--date", required=True, help="切换日 YYYY-MM-DD")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--dry-run", action="store_true", help="只打印将清多少条，不落库")
    ap.add_argument("--check", type=int, choices=(0, 1, 2), default=None,
                    help="顺手勾上的阶段（0=两个都不勾；默认不动现有勾选）")
    a = ap.parse_args()

    try:
        ts = time.mktime(time.strptime(a.date, "%Y-%m-%d"))
    except ValueError:
        print("日期格式不对，要 YYYY-MM-DD")
        return 2

    conn = sqlite3.connect(a.db)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE IF NOT EXISTS app_cfg (key TEXT PRIMARY KEY, value TEXT)")
    for t, cols in (("train_override", "code TEXT PRIMARY KEY, updated_at REAL"),
                    ("curate", "code TEXT PRIMARY KEY, date TEXT")):
        try:
            conn.execute(f"SELECT {cols.split(',')[0].split()[0]} FROM {t} LIMIT 1")
        except sqlite3.Error:
            print(f"库里没有 {t} 表，路径不对？ {a.db}")
            conn.close()
            return 2

    row = conn.execute("SELECT value FROM app_cfg WHERE key='renum_cutoff'").fetchone()
    cut_old = (row["value"] if row else "") or ""
    cut = max(cut_old, a.date) if cut_old else a.date

    marks = ",".join("?" * len(PHASE2_CLEAR))
    if a.phase == 1:
        n_ov = _count(conn,
                      "SELECT COUNT(*) FROM train_override WHERE COALESCE(updated_at,0) < ?",
                      (ts,))
        sql_ov = "DELETE FROM train_override WHERE COALESCE(updated_at,0) < ?"
        args_ov = [ts]
        n_cu = _count(conn, "SELECT COUNT(*) FROM curate WHERE COALESCE(date,'') < ?",
                      (a.date,))
        sql_cu = "DELETE FROM curate WHERE COALESCE(date,'') < ?"
        args_cu = [a.date]
    else:
        n_ov = _count(conn,
                      f"SELECT COUNT(*) FROM train_override WHERE COALESCE(updated_at,0) < ? "
                      f"AND upper(substr(code,1,1)) IN ({marks})",
                      [ts] + PHASE2_CLEAR)
        sql_ov = (f"DELETE FROM train_override WHERE COALESCE(updated_at,0) < ? "
                  f"AND upper(substr(code,1,1)) IN ({marks})")
        args_ov = [ts] + PHASE2_CLEAR
        n_cu = _count(conn,
                      f"SELECT COUNT(*) FROM curate WHERE COALESCE(date,'') < ? "
                      f"AND upper(substr(code,1,1)) IN ({marks})",
                      [a.date] + PHASE2_CLEAR)
        sql_cu = (f"DELETE FROM curate WHERE COALESCE(date,'') < ? "
                  f"AND upper(substr(code,1,1)) IN ({marks})")
        args_cu = [a.date] + PHASE2_CLEAR

    print(f"切换日 {a.date}（阶段{a.phase}）" + ("【预演，不落库】" if a.dry_run else ""))
    print(f"  清旧校正 train_override：{n_ov} 条"
          + ("" if a.phase == 1 else "（只清 G/D/C/S 字头，新校的保留）"))
    print(f"  清待校对老行 curate：{n_cu} 条")
    print(f"  写配置：renum_cutoff={cut}（老车次从此不进校对范围）"
          + (f"  勾选阶段{a.check}" if a.check else ""))
    if not n_ov and not n_cu:
        print("  （没有要清的行；配置仍会写入/更新）")

    if a.dry_run:
        conn.close()
        return 0

    conn.execute(sql_ov, args_ov)
    conn.execute(sql_cu, args_cu)
    conn.execute("INSERT INTO app_cfg(key,value) VALUES('renum_cutoff',?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (cut,))
    if a.check is not None:
        for ph in (1, 2):
            conn.execute("INSERT INTO app_cfg(key,value) VALUES(?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (f"renum_phase{ph}", "1" if ph == a.check else "0"))
    conn.execute("DELETE FROM app_cfg WHERE key='renum_new_letters'")   # 旧键，改由两个阶段勾选推导
    conn.commit()
    conn.close()
    print("完成。配置即刻生效（不用重启服务）；两个阶段勾选框在「数据校对」页顶部。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
