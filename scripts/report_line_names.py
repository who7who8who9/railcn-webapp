# -*- coding: utf-8 -*-
"""
report_line_names.py — 线名清理报告

整理库 linedesk.db 建好之后，把「清掉了什么、还剩什么要人工看」摊成一份
Markdown，给 review 用。只读，不改任何库。

    python scripts/report_line_names.py                 # 打到屏幕
    python scripts/report_line_names.py --out docs/line-names-report.md
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from typing import List

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "webapp"))

import linedesk                                              # noqa: E402

SRC_CN = {"lib": "底表（cnrail 417 条）", "custom": "自建", "huirui": "辉睿资料库",
          "ce": "china-emu", "osm": "OSM 线名"}


def q(c: sqlite3.Connection, sql: str, args=()) -> List[sqlite3.Row]:
    return c.execute(sql, args).fetchall()


def build() -> str:
    linedesk.init()
    st = linedesk.stats()
    c = sqlite3.connect(f"file:{linedesk.DB_PATH}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row

    L = []
    add = L.append
    add("# 线路命名清理报告\n")
    add("> 由 `scripts/report_line_names.py` 生成，数据来自独立整理库 "
        "`webapp/data/linedesk.db`（不是 rail.db）。\n")
    add(f"生成时间：{st['seededAt']}　·　"
        f"规范线 **{st['lines']}** 条，别名 **{st['aliases']}** 条，"
        f"有站序 **{st['withStops']}** 条，父子关系 **{st['relations']}** 条\n")

    # ---- 一 ----
    add("\n## 一、五套来源怎么合到一起的\n")
    add("| 来源 | 条数 | 说明 |")
    add("|---|---:|---|")
    desc = {
        "lib": "按「X线Y段」分段命名，是主库原本的名字",
        "custom": "人工自建线路",
        "huirui": "辉睿线路资料库，叫「XX高铁」",
        "ce": "china-emu，叫「XX高速铁路」，逐站带 K 里程",
        "osm": "只有线名和走向，没有站序",
    }
    for k in ("lib", "custom", "huirui", "ce", "osm"):
        add(f"| {SRC_CN[k]} | {st['bySource'].get(k, 0)} | {desc[k]} |")
    add(f"| **合计写法** | **{sum(st['bySource'].values())}** | "
        f"归并成 **{st['lines']}** 条线、**{st['families']}** 个线族 |")

    # ---- 二 ----
    add("\n## 二、同一条线的多种写法（已归并）\n")
    add("后缀族归一后，「高铁 / 高速线 / 高速铁路 / 客运专线」算同一个名字；"
        "同一族里各来源的写法都收成别名。下面是最典型的几组：\n")
    rows = q(c, """
        SELECT l.id, l.name, l.kind,
               (SELECT COUNT(*) FROM ld_alias a WHERE a.line_id=l.id) n,
               (SELECT GROUP_CONCAT(a.source||'='||a.name, ' | ')
                  FROM ld_alias a WHERE a.line_id=l.id) al
          FROM ld_line l
         WHERE n >= 3
         ORDER BY n DESC, l.id LIMIT 18""")
    add("| 规范名 | 类型 | 写法数 | 各来源写法 |")
    add("|---|---|---:|---|")
    for r in rows:
        add(f"| {r['name']} | {linedesk.KIND_LABEL.get(r['kind'], '—')} | {r['n']} | "
            f"{r['al'] or ''} |")

    # ---- 三 ----
    add("\n## 三、全线命名 + 分段建设\n")
    add("一条线修完才通车，所以既有整条的名字，也有按段起的名字。"
        "整理库里把分段挂到整线下面（`ld_relation`），查整线能看见它由哪几段组成：\n")
    parents = q(c, """
        SELECT l.id, l.name, l.total_km,
               (SELECT COUNT(*) FROM ld_relation r WHERE r.parent_id=l.id) k
          FROM ld_line l
         WHERE EXISTS(SELECT 1 FROM ld_relation r WHERE r.parent_id=l.id)
         ORDER BY k DESC, l.id LIMIT 15""")
    add("| 整线 | 分段数 | 分 段 |")
    add("|---|---:|---|")
    for p in parents:
        kids = q(c, """
            SELECT l.name, (SELECT COUNT(*) FROM ld_stop s WHERE s.line_id=l.id) n
              FROM ld_relation r JOIN ld_line l ON l.id=r.child_id
             WHERE r.parent_id=? ORDER BY l.name""", (p["id"],))
        add(f"| {p['name']} | {p['k']} | " +
            "、".join(f"{k['name']}（{k['n']} 站）" for k in kids) + " |")

    # ---- 四 ----
    add("\n## 四、还要人工看的\n")
    lonely = q(c, """
        SELECT l.name FROM ld_line l
         WHERE NOT EXISTS(SELECT 1 FROM ld_relation r WHERE r.child_id=l.id)
           AND l.name LIKE '%段'""")
    lonely = [r["name"] for r in lonely if linedesk.split_seg(r["name"])]
    add(f"### 4.1 有「段」但没挂上父线的：{len(lonely)} 条\n")
    add("分段名背后的整线，五套来源里都没出现，需要人工确认父线叫什么"
        "（整理台「挂到父线」）：\n")
    for n in lonely[:30]:
        add(f"- {n}")
    if not lonely:
        add("- 没有了")

    empty = q(c, """
        SELECT l.name FROM ld_line l
         WHERE NOT EXISTS(SELECT 1 FROM ld_stop s WHERE s.line_id=l.id)
           AND l.total_km IS NULL
         ORDER BY l.id""")
    add(f"\n### 4.2 一条站序都没有的线：{len(empty)} 条\n")
    add("多数是只在 OSM 里出现的线名（有走向没站序），整理时按需要补站，"
        "或者直接并到已有线上：\n")
    for r in empty[:20]:
        add(f"- {r['name']}")
    if len(empty) > 20:
        add(f"- …… 其余 {len(empty) - 20} 条")

    dup = q(c, """
        SELECT family, COUNT(*) n, GROUP_CONCAT(name, '、') nm
          FROM ld_line WHERE family <> ''
         GROUP BY family HAVING n > 1
         ORDER BY n DESC, family LIMIT 12""")
    add(f"\n### 4.3 同一族里还有多条线的：{len(dup)} 族\n")
    add("正常情况下就是「整线 + 分段」，若发现两个都不是分段的，说明该合并：\n")
    add("| 线族 | 条数 | 成员 |")
    add("|---|---:|---|")
    for r in dup:
        add(f"| `{r['family']}` | {r['n']} | {r['nm']} |")

    c.close()
    return "\n".join(L) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="", help="写到文件（默认打到屏幕）")
    a = ap.parse_args()
    md = build()
    if a.out:
        path = a.out if os.path.isabs(a.out) else os.path.join(ROOT, a.out)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(md)
        print("已写出", path, f"（{len(md)} 字符）")
    else:
        print(md)


if __name__ == "__main__":
    main()
