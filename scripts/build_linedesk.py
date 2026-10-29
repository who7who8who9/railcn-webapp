# -*- coding: utf-8 -*-
"""
build_linedesk.py — 建线路整理库、灌初稿

把五套来源（底表 / 自建 / 辉睿资料库 / china-emu / OSM）的线名归并一遍，
写进独立库 webapp/data/linedesk.db。**不动 rail.db**。

    python scripts/build_linedesk.py              # 只补 rail.db 里新出现的线名
    python scripts/build_linedesk.py --force      # 清空重建（人工整理结果会没）
    python scripts/build_linedesk.py --stat       # 只看现状
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "webapp"))

import linedesk                                              # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="清空重建（会把人工改过的规范名、别名、站序一起清掉）")
    ap.add_argument("--stat", action="store_true", help="只看现状，不写库")
    a = ap.parse_args()

    if a.stat:
        print(json.dumps(linedesk.stats(), ensure_ascii=False, indent=1))
        return

    if a.force:
        print("！清空重建，整理库里所有人工改动都会丢失")
    st = linedesk.build_seed(force=a.force)
    print("来源：", json.dumps(st["sources"], ensure_ascii=False))
    print(f"归并：{st['groups']} 组 → 新建 {st['lines']} 条线，"
          f"收下 {st['aliases']} 个写法，{st['segged']} 个分段名，"
          f"挂上 {st['relations']} 条父子，"
          f"补进 {st['stopped']} 条站序，反推父线 {st.get('inferred', 0)} 条")
    if not a.force:
        print(f"（已整理过的线跳过 {st['skipped']} 条）")
    print("库：", linedesk.DB_PATH)
    print("页面：http://127.0.0.1:8686/linedesk.html")
    print("报告：python scripts/report_line_names.py --out docs/line-names-report.md")


if __name__ == "__main__":
    main()
