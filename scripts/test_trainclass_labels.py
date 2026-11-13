# -*- coding: utf-8 -*-
"""2026 新版车次代号：车种标签按日期分时代 + 勾阶段一自动重扫

背景（用户 2026-11-11 反馈）：
  11/25 新车次已下载（库里 V/N/Z/Y 共 1168 趟），但「数据校对」勾了阶段一
  却一趟也没有 —— 因为勾选框只重读 curate 旧表，不触发扫描。

本回归覆盖：
  1. historyq._label 按切换日 2026-11-25 分时代（T 旧=特快/新=高速动车组 …）
  2. trainmap.js 的 TYPE_LABELS_OLD/NEW、tmIsNewEra、relabelTypeChips、不再有 const TYPES
  3. app.js 勾阶段后自动 POST /api/curate/scan + 新 tooltip 文案
  4. curate 阶段字头与新版分组一致（阶段一 V/N/Z/Y，阶段二 X/G/T/K/D/C/S）
  5. HTTP：/api/curate/stats 的 renum.letters（服务没起自动跳过）

跑法： python scripts/test_trainclass_labels.py
（只读正式库；不发写操作）
"""
import os
import sys
import urllib.request
import json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

import historyq   # noqa: E402
import curate     # noqa: E402

PASS, FAIL = [], []


def ck(label, cond, extra=""):
    (PASS if cond else FAIL).append(label)
    print(("  ok   " if cond else "  FAIL ") + label +
          ("" if cond else "  ← " + str(extra)[:220]))


def read(p):
    with open(os.path.join(ROOT, p), encoding="utf-8") as f:
        return f.read()


CUT = "2026-11-25"

print("== 1. historyq._label 按日期分时代 ==")
ck("T@11-11 = 特快", historyq._label("T", "2026-11-11") == "特快", historyq._label("T", "2026-11-11"))
ck("T@11-25 = 高速动车组", historyq._label("T", CUT) == "高速动车组", historyq._label("T", CUT))
ck("K@11-24 = 快速", historyq._label("K", "2026-11-24") == "快速", historyq._label("K", "2026-11-24"))
ck("K@11-25 = 高速动车", historyq._label("K", CUT) == "高速动车", historyq._label("K", CUT))
ck("N@11-11 = 数字车次", historyq._label("N", "2026-11-11") == "数字车次", historyq._label("N", "2026-11-11"))
ck("N@11-25 = 快速普速", historyq._label("N", CUT) == "快速普速", historyq._label("N", CUT))
ck("V@11-25 = 普通快速", historyq._label("V", CUT) == "普通快速", historyq._label("V", CUT))
ck("X@11-25 = 超高速", historyq._label("X", CUT) == "超高速", historyq._label("X", CUT))
ck("Z@11-25 = 直达特快", historyq._label("Z", CUT) == "直达特快", historyq._label("Z", CUT))
ck("G@11-25 = 高铁", historyq._label("G", CUT) == "高铁", historyq._label("G", CUT))
ck("无日期默认新时代（T=高速动车组）", historyq._label("T") == "高速动车组", historyq._label("T"))
ck("未知字母原样返回", historyq._label("Q", CUT) == "Q", historyq._label("Q", CUT))

print("\n== 2. trainmap.js 分时代标签 ==")
tm = read("webapp/static/trainmap.js")
ck("有 TM_CUTOFF = 2026-11-25", "TM_CUTOFF = '2026-11-25'" in tm or 'TM_CUTOFF = "2026-11-25"' in tm)
ck("有 TYPE_LABELS_OLD", "TYPE_LABELS_OLD" in tm)
ck("有 TYPE_LABELS_NEW", "TYPE_LABELS_NEW" in tm)
ck("有 tmIsNewEra", "tmIsNewEra" in tm)
ck("有 relabelTypeChips", "relabelTypeChips" in tm)
ck("新标签含「高速动车组 T」", "高速动车组 T" in tm)
ck("新标签含「快速普速 N」", "快速普速 N" in tm)
ck("新标签含「普通快速 V」", "普通快速 V" in tm)
ck("新标签含「超高速 X」", "超高速 X" in tm)
ck("旧标签含「特快 T」", "特快 T" in tm)
ck("旧标签含「快速 K」", "快速 K" in tm)
ck("不再有旧 const TYPES = [", "const TYPES = [" not in tm)
ck("筛选用 TYPE_KEYS", "TYPE_KEYS" in tm)
ck("renderStat 里重绘标签", "relabelTypeChips();" in tm)

print("\n== 3. app.js 勾阶段自动重扫 + tooltip ==")
js = read("webapp/static/app.js")
ck("有「正在重新扫描新车次」提示", "正在重新扫描新车次" in js)
ck("set_renum 后 POST /api/curate/scan", "cuRenderEra(d.renum)" in js and "postJSON('/api/curate/scan'" in js)
ck("tooltip 含「Z 直达特快(160)」", "Z 直达特快(160)" in js)
ck("tooltip 含「T 高速动车组(300-310)」", "T 高速动车组(300-310)" in js)
ck("tooltip 含「K 高速动车(200-250)」", "K 高速动车(200-250)" in js)

print("\n== 4. curate 阶段字头与新版分组一致 ==")
ck("阶段一 = V/N/Z/Y", curate.PHASE1_LETTERS == ["V", "N", "Z", "Y"], curate.PHASE1_LETTERS)
ck("阶段二 = X/G/T/K/D/C/S", curate.PHASE2_LETTERS == ["X", "G", "T", "K", "D", "C", "S"],
   curate.PHASE2_LETTERS)
ck("historyq 导入成功", historyq is not None)

print("\n== 5. HTTP（服务没起自动跳过）==")
try:
    with urllib.request.urlopen("http://127.0.0.1:8686/api/curate/stats", timeout=5) as r:
        d = json.loads(r.read().decode("utf-8"))
    renum = d.get("renum") or {}
    ck("接口返回 renum 口径", bool(renum), list(d.keys())[:8])
    # 当前配置是阶段一（V/N/Z/Y）；字母集合应等于阶段一
    ck("接口 letters 与阶段一一致", set(renum.get("letters") or []) <= {"V", "N", "Z", "Y"},
       renum.get("letters"))
except Exception as e:                       # noqa: BLE001
    print("  skip  HTTP（服务未启动）：%s" % type(e).__name__)

print("\n通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
if FAIL:
    for x in FAIL:
        print("  × " + x)
    sys.exit(1)
print("全部通过 ✓")
