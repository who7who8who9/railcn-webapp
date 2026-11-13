#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""renumber_cutover 切换日命令的回归：临时库里跑，不碰真实数据。

验证：
  1. 阶段一：清全部旧校正（updated_at < 切换日）、清 date < 切换日 的待校对行，
     切换日后新建的校正/行保留；写 renum_cutoff
  2. 阶段二：只清 G/D/C/S 字头且 updated_at < 切换日 的校正、date < 切换日 的老行；
     V/N/Z/Y 的校正和切换日后新建的 G/T 行保留；切换日推进
  3. --dry-run 不落库；--check 1/2 落阶段勾选
  4. curate._era_having / _era_where：没切换日不过滤；有切换日但一个阶段都没勾 = 一律不校
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "renumber_cutover.py")
PY = sys.executable

fails: list = []


def ok(cond, msg: str) -> None:
    if cond:
        print("  ok", msg)
    else:
        fails.append(msg)
        print("  FAIL", msg)


def epoch(date: str) -> float:
    return time.mktime(time.strptime(date, "%Y-%m-%d"))


def mkdb(path: str) -> None:
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE train_override (code TEXT PRIMARY KEY, updated_at REAL);
        CREATE TABLE curate (code TEXT PRIMARY KEY, date TEXT);
        CREATE TABLE app_cfg (key TEXT PRIMARY KEY, value TEXT);
        """
    )
    con.commit()
    con.close()


def run(path: str, phase: int, date: str, dry: bool = False, check=None):
    cmd = [PY, SCRIPT, "--phase", str(phase), "--date", date, "--db", path]
    if dry:
        cmd.append("--dry-run")
    if check is not None:
        cmd += ["--check", str(check)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    return r


def rows(path: str, sql: str, args=()):
    con = sqlite3.connect(path)
    out = [tuple(r) for r in con.execute(sql, args).fetchall()]
    con.close()
    return out


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="renum_")
    db = os.path.join(tmp, "rail.db")
    mkdb(db)

    # ---- 场景数据：K666/G1462 是改号前的旧校正；V456 是 11-25 之后新校的 ----
    con = sqlite3.connect(db)
    con.execute("INSERT INTO train_override VALUES('K666', ?)", (epoch("2026-11-01"),))
    con.execute("INSERT INTO train_override VALUES('G1462', ?)", (epoch("2026-11-01"),))
    con.execute("INSERT INTO train_override VALUES('V456', ?)", (epoch("2026-11-26"),))
    con.execute("INSERT INTO curate VALUES('K666', '2026-11-20')")
    con.execute("INSERT INTO curate VALUES('V456', '2026-11-25')")
    con.execute("INSERT INTO curate VALUES('V789', '2026-11-26')")
    con.commit()
    con.close()

    print("阶段一 dry-run 不落库")
    r = run(db, 1, "2026-11-25", dry=True)
    ok(r.returncode == 0, "退出码 0")
    ok("预演" in r.stdout, "标明预演")
    ok(len(rows(db, "SELECT * FROM train_override")) == 3, "dry-run 不删校正")
    ok(len(rows(db, "SELECT * FROM curate")) == 3, "dry-run 不删待校对行")

    print("阶段一 清旧校正 + 清老行 + 写配置")
    r = run(db, 1, "2026-11-25", check=1)
    ok(r.returncode == 0, "退出码 0")
    ovs = sorted(x[0] for x in rows(db, "SELECT code, updated_at FROM train_override"))
    ok(ovs == ["V456"], f"只剩切换日后新校的 V456（实际 {ovs}）")
    cus = sorted(x[0] for x in rows(db, "SELECT code, date FROM curate"))
    ok(cus == ["V456", "V789"], f"待校对只剩新车次行（实际 {cus}）")
    cfg = dict(rows(db, "SELECT key, value FROM app_cfg"))
    ok(cfg.get("renum_cutoff") == "2026-11-25", f"renum_cutoff 写入（实际 {cfg.get('renum_cutoff')}）")
    ok(cfg.get("renum_phase1") == "1" and cfg.get("renum_phase2") == "0",
       f"--check 1 勾上阶段一（实际 {cfg.get('renum_phase1')}/{cfg.get('renum_phase2')}）")
    ok("renum_new_letters" not in cfg, "旧的白名单键已删掉")

    print("阶段一 幂等重跑")
    r = run(db, 1, "2026-11-25")
    ok(r.returncode == 0 and len(rows(db, "SELECT * FROM train_override")) == 1, "重跑不误删")
    cfg = dict(rows(db, "SELECT key, value FROM app_cfg"))
    ok(cfg.get("renum_phase1") == "1", "不带 --check 时不动已有勾选")

    print("阶段二 只清 G/D/C/S 旧校正，V/N/Z/Y 保留")
    con = sqlite3.connect(db)
    con.execute("INSERT INTO train_override VALUES('G123', ?)", (epoch("2026-12-01"),))
    con.execute("INSERT INTO train_override VALUES('D789', ?)", (epoch("2026-12-05"),))
    con.execute("INSERT INTO train_override VALUES('T999', ?)", (epoch("2026-12-21"),))
    con.execute("INSERT INTO train_override VALUES('G234', ?)", (epoch("2026-12-21"),))
    con.execute("INSERT INTO curate VALUES('G123', '2026-12-15')")
    con.execute("INSERT INTO curate VALUES('G234', '2026-12-21')")
    con.execute("INSERT INTO curate VALUES('T999', '2026-12-21')")
    con.commit()
    con.close()
    r = run(db, 2, "2026-12-20", check=2)
    ok(r.returncode == 0, "退出码 0")
    ovs = sorted(x[0] for x in rows(db, "SELECT code FROM train_override"))
    ok(ovs == ["G234", "T999", "V456"],
       f"阶段二后剩 V456（阶段一后新校）+ G234/T999（切换日后新校）（实际 {ovs}）")
    cus = sorted(x[0] for x in rows(db, "SELECT code FROM curate"))
    ok(cus == ["G234", "T999", "V456", "V789"],
       f"待校对老 G123 清掉、新 G234/T999 和 V 系保留（实际 {cus}）")
    cfg = dict(rows(db, "SELECT key, value FROM app_cfg"))
    ok(cfg.get("renum_cutoff") == "2026-12-20", f"切换日推进到 12-20（实际 {cfg.get('renum_cutoff')}）")
    ok(cfg.get("renum_phase1") == "0" and cfg.get("renum_phase2") == "1",
       f"--check 2 只勾阶段二（实际 {cfg.get('renum_phase1')}/{cfg.get('renum_phase2')}）")

    print("curate 过滤条件生成")
    sys.path.insert(0, os.path.join(ROOT, "webapp"))
    import curate  # noqa: E402
    import railcore  # noqa: E402
    orig = railcore.cfg_get
    railcore.cfg_get = lambda k, d="": ""
    h, ha = curate._era_having()
    w, wa = curate._era_where()
    ok(h == "" and ha == [] and w == "" and wa == [], "没配置时不过滤")

    # 已切新口径、两个阶段都没勾 = 老车次全部出范围，清单为空
    railcore.cfg_get = lambda k, d="": {
        "renum_cutoff": "2026-11-25", "renum_phase1": "0", "renum_phase2": "0"}.get(k, d)
    ok(curate._era_having() == (" HAVING 1=0", []), "没勾阶段：扫描不认任何车次")
    ok(curate._era_where() == (" AND 1=0", []), "没勾阶段：清单为空")

    railcore.cfg_get = lambda k, d="": {
        "renum_cutoff": "2026-11-25", "renum_phase1": "1", "renum_phase2": "0"}.get(k, d)
    h, ha = curate._era_having()
    w, wa = curate._era_where()
    ok("HAVING MAX(date) >= ?" in h and ha[0] == "2026-11-25"
       and set(ha[1:]) == {"V", "N", "Z", "Y"}, f"having 带日期+字头（{h} {ha}）")
    ok("upper(substr(code,1,1)) IN" in w and wa[1:] == ["V", "N", "Z", "Y"],
       f"where 带字头白名单（{w} {wa}）")

    railcore.cfg_get = lambda k, d="": {
        "renum_cutoff": "2026-12-20", "renum_phase1": "1", "renum_phase2": "1"}.get(k, d)
    _, ha2 = curate._era_having()
    ok(set(ha2[1:]) == {"V", "N", "Z", "Y", "X", "G", "T", "K", "D", "C", "S"},
       f"两个阶段都勾 = 全部新字头（{ha2}）")
    railcore.cfg_get = orig

    print(("失败 %d 项" % len(fails)) if fails else "全部通过")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
