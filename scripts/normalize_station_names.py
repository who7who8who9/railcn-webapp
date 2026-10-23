"""一次性迁移：把自建线路 / 自建车站 / 人工经由里的站名统一成 12306 口径（去掉「站」后缀）。

用法：
    python scripts/normalize_station_names.py            # 只看会改什么（dry-run）
    python scripts/normalize_station_names.py --apply    # 真正写库（会先备份 rail.db）
"""
import json
import os
import shutil
import sqlite3
import sys
import time

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "webapp", "data", "rail.db")
DB = os.path.abspath(DB)
APPLY = "--apply" in sys.argv

SUFFIXES = ("线路所", "站", "所")


def key(name: str) -> str:
    n = "".join(str(name or "").split())
    for suf in SUFFIXES:
        if len(n) > len(suf) and n.endswith(suf):
            return n[: -len(suf)]
    return n


def norm(name: str) -> str:
    return "".join(str(name or "").split())


changes = []

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

# ---- 1. 自建线路：stations_json 里的站名 ----
line_updates = []
for row in conn.execute("SELECT name, stations_json FROM line_custom"):
    st = json.loads(row["stations_json"] or "[]")
    diff = []
    out = []
    seen = set()
    for s in st:
        old = s.get("name") or ""
        new = key(old)
        if new != old:
            diff.append(f"{old}->{new}")
        if new in seen:
            diff.append(f"（合并重复 {new}）")
            continue
        seen.add(new)
        out.append({**s, "name": new})
    if diff:
        line_updates.append((row["name"], json.dumps(out, ensure_ascii=False), diff))
        changes.append(f"线路 {row['name']}: " + "、".join(diff))

# ---- 2. 自建车站：主键改名 ----
sta_rows = list(conn.execute("SELECT name, lat, lon, lines_json, tele, bureau, note, updated_at "
                             "FROM station_custom"))
sta_updates = []
for row in sta_rows:
    old = row["name"]
    new = key(old)
    if not new or new == old:
        continue
    exists = conn.execute("SELECT 1 FROM station_custom WHERE name=?", (new,)).fetchone()
    sta_updates.append((old, new, bool(exists), dict(row)))
    changes.append(f"车站 {old} -> {new}" + ("（与已有同名项合并）" if exists else ""))

# ---- 3. 人工经由：from / to ----
via_updates = []
for row in conn.execute("SELECT code, via_json FROM train_override"):
    segs = json.loads(row["via_json"] or "[]")
    diff = []
    for s in segs:
        for f in ("from", "to"):
            old = s.get(f) or ""
            new = key(old)
            if new != old:
                diff.append(f"{old}->{new}")
                s[f] = new
    if diff:
        via_updates.append((row["code"], json.dumps(segs, ensure_ascii=False), diff))
        changes.append(f"经由 {row['code']}: " + "、".join(diff))

# ---- 4. 车站坐标人工校正：name 列 ----
coord_updates = []
try:
    for row in conn.execute("SELECT name FROM coord_override"):
        old = row["name"]
        new = key(old)
        if new and new != old:
            coord_updates.append((old, new))
            changes.append(f"坐标 {old} -> {new}")
except sqlite3.Error:
    pass

if not changes:
    print("没有需要改的站名，已经是一致的 12306 口径。")
    sys.exit(0)

print(f"共 {len(changes)} 处改动：")
for c in changes:
    print("  ·", c)

if not APPLY:
    print("\n（dry-run，未写库；加 --apply 执行）")
    sys.exit(0)

bak = DB + f".bak-{time.strftime('%Y%m%d-%H%M%S')}"
shutil.copy2(DB, bak)
print(f"\n已备份 -> {bak}")

with conn:
    for name, stations_json, _ in line_updates:
        conn.execute("UPDATE line_custom SET stations_json=?, updated_at=? WHERE name=?",
                     (stations_json, time.time(), name))
    for old, new, exists, row in sta_updates:
        if exists:
            conn.execute("DELETE FROM station_custom WHERE name=?", (old,))
        else:
            conn.execute("UPDATE station_custom SET name=? WHERE name=?", (new, old))
    for code, via_json, _ in via_updates:
        conn.execute("UPDATE train_override SET via_json=?, updated_at=? WHERE code=?",
                     (via_json, time.time(), code))
    for old, new in coord_updates:
        if conn.execute("SELECT 1 FROM coord_override WHERE name=?", (new,)).fetchone():
            conn.execute("DELETE FROM coord_override WHERE name=?", (old,))
        else:
            conn.execute("UPDATE coord_override SET name=? WHERE name=?", (new, old))
conn.close()
print("迁移完成。重启服务后生效（curate 表会重新扫描）。")
