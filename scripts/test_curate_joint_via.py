# -*- coding: utf-8 -*-
"""
回归：把「线路所」纳入数据校对的自动计算经由

2026-11-12 用户提「将线路所纳入自动计算经由里面」。整理台确认站序里本来就有
771 行线路所，但 `confvia.infer()` 只按车次经停站相邻配对，两站不共线就直接算
缺口 —— N1534 的「南疆线→头道河所→大河沿联络线」、Z75 的「石获线→获鹿所→
石太客专线」以前只能人工填。

口径（别改宽）：
  · 只对**原本是缺口**的区间启用多段链路，已能单线直连的一律不动；
  · 链路里**至少经过一个线路所**才采用，普通多线绕行仍留缺口给人工判断；
  · 线路所按**精确站名**换线（`_key()` 会把「获鹿所」砍成「获鹿」，与同名
    普通站撞键，否则会拼出「石太线 获鹿→获鹿」这种自环）；
  · 最多 4 段、BFS 状态上限 1500，换乘表与结果都按 index() 版本缓存，
    线路 / 车站以后增加自动跟上，不写死名单，也不拖慢全库扫描。
  · 自动推断仍然只是参考，人工确认后才写正式库。

跑法：python scripts/test_curate_joint_via.py   （先起 server --port 8686）
只读，不写库。
"""
from __future__ import annotations

import io
import sys
import time
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8686"
ok = fail = 0
bad: list = []


def check(label: str, cond: bool, val="") -> None:
    global ok, fail
    if cond:
        ok += 1
        print("  ok   %s%s" % (label, ("  " + str(val))[:90] if val != "" else ""))
    else:
        fail += 1
        bad.append(label)
        print("  失败 %s%s" % (label, ("  " + str(val))[:90] if val != "" else ""))


def get(path: str, **kw) -> dict:
    import json
    qs = ("?" + urllib.parse.urlencode(kw)) if kw else ""
    with urllib.request.urlopen(BASE + path + qs, timeout=180) as r:
        return json.loads(r.read().decode("utf-8"))


def src(rel: str) -> str:
    return io.open(rel, encoding="utf-8").read()


def main() -> int:
    sys.path.insert(0, "webapp")
    import confvia

    print("== 1) 静态护栏：动态口径 / 精确名 / 上限")
    cf = src("webapp/confvia.py")
    check("线路所判定独立成函数", "def _is_joint" in cf and "线路所" in cf)
    check("线路所索引跟缓存版本走", "def joint_index" in cf and "_JOINT" in cf)
    check("换乘表按精确站名，不按撞名键",
          "def _build_transfers" in cf and "exact[nm2].get(s) if joint" in cf)
    check("自环兜底", 'any(s["from"] == s["to"]' in cf)
    check("BFS 段数 / 状态上限", "_MAX_LEG" in cf and "_MAX_STATE" in cf)
    check("链路结果按站对缓存", '"trs"' in cf and "cache[ck] = out" in cf)
    check("线路暴涨时直接不搜", "len(idx) > 80" in cf)
    check("infer 输出 bridged / joints",
          '"bridged":' in cf and '"joints":' in cf)
    check("invalidate 清线路所 / 链路缓存",
          "_JOINT[\"data\"] = None" in cf and "_BRIDGE[\"trs\"] = None" in cf)
    check("缺口才走链路，不改单线直连",
          "if best:" in cf and "link = _bridge_chain" in cf)

    print("== 2) 线路所索引：真实数据 + 以后新增也能跟上")
    ji = confvia.joint_index()
    check("确认线路里确有线路所", len(ji) >= 10, len(ji))
    check("头道河所跨 ≥2 条线", len(ji.get("头道河", {})) >= 2,
          list(ji.get("头道河", {})))
    check("获鹿所跨 ≥2 条线", len(ji.get("获鹿", {})) >= 2,
          list(ji.get("获鹿", {})))
    check("跨线线路所只认精确名",
          all(confvia._is_joint(x[1]) for v in ji.values() for x in v.values()))

    # 内存里伪造一条新线 + 一个新线路所（不写库），验证索引随数据重建
    confvia.invalidate()
    base = dict(confvia.index())
    try:
        base["测试新线"] = {
            "pos": {"甲站": 0, "测试新所": 1, "乙站": 2},
            "stops": ["甲站", "测试新所", "乙站"],
            "km": [0, 10, 20], "n": 3, "totalKm": 20,
        }
        confvia._CACHE["data"] = base
        confvia._CACHE["ts"] = time.time() + 10
        j2 = confvia.joint_index()
        # 索引键按 _key() 剥「所」后缀（换乘时再用精确站名，见 _build_transfers）
        check("新增线路所自动进索引", "测试新" in j2, list(j2)[:8])
        check("新增线路所挂在新线下", "测试新线" in j2.get("测试新", {}),
              j2.get("测试新"))
        check("索引保留线路所原样名",
              j2.get("测试新", {}).get("测试新线", (None, ""))[1] == "测试新所",
              j2.get("测试新"))
    finally:
        confvia.invalidate()

    print("== 3) 三趟车的自动推断")
    n1534 = confvia.infer([{"name": x} for x in
                           ["阿克苏", "库车", "库尔勒", "乌鲁木齐"]])
    check("N1534 自动认全", not n1534["unknown"] and n1534["coverage"] == 100.0,
          (n1534["unknown"], n1534["coverage"]))
    check("N1534 用上头道河所", "头道河所" in n1534["joints"], n1534["joints"])
    lines = {s["line"] for s in n1534["segs"]}
    check("N1534 拆出南疆线 / 大河沿联络线 / 兰新客专",
          {"南疆线", "大河沿联络线", "兰新客专"} <= lines, sorted(lines))
    check("N1534 段端点带线路所",
          any("头道河所" in (s["from"], s["to"]) for s in n1534["segs"]),
          [(s["from"], s["to"]) for s in n1534["segs"]])
    check("N1534 bridged 计数", n1534["bridged"] >= 1, n1534["bridged"])

    z75 = confvia.infer([{"name": x} for x in
                         ["北京西", "保定", "石家庄北", "阳泉北", "太原", "中卫", "兰州"]])
    check("Z75 用上获鹿所", "获鹿所" in z75["joints"], z75["joints"])
    check("Z75 覆盖率不低于旧口径 33.3%", z75["coverage"] >= 33.3,
          z75["coverage"])
    check("链路段数不超过 4", z75["bridged"] <= 4 and len(z75["segs"]) <= 12,
          (z75["bridged"], len(z75["segs"])))

    g1 = confvia.infer([{"name": x} for x in
                        ["北京南", "济南西", "南京南", "上海虹桥"]])
    check("G1 仍单线直连 1 段", len(g1["segs"]) == 1 and g1["segs"][0]["line"] == "京沪高速线",
          g1["segs"])
    check("G1 不触发线路所链路", g1["bridged"] == 0 and not g1["joints"],
          (g1["bridged"], g1["joints"]))

    three = ["石家庄", "邢台", "邯郸"]
    r3 = confvia.infer([{"name": x} for x in three])
    check("连点 3 站仍认成 1 段（单线优先）",
          len(r3["segs"]) == 1 and not r3["unknown"], r3["segs"])
    check("所有段都不自环",
          all(s["from"] != s["to"] for r in (n1534, z75, g1, r3) for s in r["segs"]))
    check("没确认的线不因链路冒出来",
          all(s["line"] in confvia.names() for r in (n1534, z75, g1, r3) for s in r["segs"]))

    print("== 4) 全库扫描不拖慢（缓存 + 快速预判）")
    idx = confvia.index()
    allsta = list({s for e in idx.values() for s in e["stops"]})
    import random
    random.seed(7)
    pairs = [(random.choice(allsta), random.choice(allsta)) for _ in range(1000)]
    t0 = time.time()
    for a, b in pairs:
        confvia.infer([{"name": a}, {"name": b}])
    dt = time.time() - t0
    check("1000 次随机站对 infer < 2s", dt < 2.0, "%.3fs" % dt)

    print("== 5) 接口：校对详情的自动基线带上线路所")
    try:
        d = get("/api/curate/detail", code="N1534", date="2026-11-25")
        auto = d.get("auto") or {}
        segs = auto.get("viaSegs") or []
        check("detail 有自动基线", bool(segs), len(segs))
        check("自动基线里出现头道河所",
              any("头道河所" in (s.get("from"), s.get("to")) for s in segs),
              [(s.get("line"), s.get("from"), s.get("to")) for s in segs[:6]])
        check("自动基线仍标 auto，不冒充人工", auto.get("viaSource") in ("auto", "none"),
              auto.get("viaSource"))
    except Exception as e:                               # noqa: BLE001
        check("接口可用", False, str(e))

    print("\n通过 %d / 失败 %d" % (ok, fail))
    if fail:
        print("失败项：", "；".join(bad))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
