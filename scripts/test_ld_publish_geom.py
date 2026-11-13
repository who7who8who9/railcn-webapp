# -*- coding: utf-8 -*-
"""test_ld_publish_geom.py —— 整理台定稿的「走向」有没有跟到主站

背景：整理台「线路来源 + 站点来源 → 合并保存 → 定为基本线路」之后，
站序/里程确实进了 line_base，但合并好的真实走向留在 linedesk.db / ld_geom，
主站以前只能按线名去 rail.db 找同名的旧版本；重新定稿时 #ldGeomName 没预填
还会把 geom_name 冲成空串。用户看到的就是「整合完了，到车站/线路却没有了」。

跑法：python scripts/test_ld_publish_geom.py  （要先起 webapp/server.py --port 8686）
不写任何库：linebase.publish / linedesk.log 都临时打桩，只捕获 payload。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "webapp"))
BASE = "http://127.0.0.1:8686"
LD_ID, NAME = 24131, "京沪高速线"
ok = fail = 0


def check(label: str, cond: bool, val=""):
    global ok, fail
    if cond:
        ok += 1
        print("  ok   %s%s" % (label, ("  " + str(val))[:80] if val != "" else ""))
    else:
        fail += 1
        print("  失败 %s%s" % (label, ("  " + str(val))[:80] if val != "" else ""))


def get(path: str) -> dict:
    try:
        with urllib.request.urlopen(BASE + path, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"status": e.code, "error": e.read().decode("utf-8")[:120]}
    except Exception as e:                                   # noqa: BLE001
        return {"error": str(e)}


def main() -> int:
    global ok, fail
    print("== 整理台合并走向 → 主站 / 重新定稿不丢 geomName ==")
    import linedesk
    import linebase

    # 走向类型/线名随整理台数据走，别写死（用户可能重做③换了来源包）
    d0 = linedesk.line_detail(LD_ID) or {}
    saved_src = (d0.get("geom_src") or "").strip()
    saved_name = (d0.get("geom_name") or "").strip()
    other_src = "osm" if saved_src != "osm" else "geojson"
    check("整理台里记了合并走向", bool(saved_src), "%s / %s" % (saved_src, saved_name))
    # 站数也别写死：用户在整理台增删过站，这里跟着草稿走
    n_exp = len(d0.get("stops") or [])
    check("整理台草稿有站序", n_exp >= 2, n_exp)

    # --- 1) merged_geom：只读 linedesk.db，形状对齐 geoimport.geom() ---
    g = linedesk.merged_geom(LD_ID, NAME)
    check("merged_geom 有几何", bool(g and g.get("parts")),
          "%s 段 / %d 点" % (len(g["parts"]), sum(len(p) for p in g["parts"])) if g else g)
    check("merged_geom 名字用基本线路名", bool(g and g.get("name") == NAME),
          g.get("name") if g else None)
    check("merged_geom 标了 ldGeom", bool(g and g.get("ldGeom") is True))
    check("merged_geom 有 ask（给前端取几何）", bool(g and g.get("ask") == NAME))
    check("merged_geom 有 crs", bool(g and g.get("crs") == "WGS84"))
    meta = linedesk.merged_geom(LD_ID, NAME, with_parts=False)
    check("with_parts=False 不解压几何", bool(meta and "parts" not in meta))
    check("没有 ld_id 就返回 None", linedesk.merged_geom(0) is None)
    check("找不到的 ld_id 返回 None", linedesk.merged_geom(99999999) is None)

    # --- 2) publish 的 geomName 兜底（打桩，不写库） ---
    captured = {}
    orig_pub, orig_log = linebase.publish, linedesk.log

    def fake_publish(payload: dict) -> dict:
        captured.clear()
        captured.update(payload)
        return {"name": payload.get("name")}

    linebase.publish = fake_publish
    linedesk.log = lambda *a, **k: None
    try:
        linedesk.publish(LD_ID, geom_src=saved_src, replace_id=1031)
        check("只给走向类型时回填合并时定下的线名",
              captured.get("geomName") == saved_name, captured.get("geomName"))
        check("回填后 stops 仍是整理台的站序", len(captured.get("stops") or []) == n_exp,
              len(captured.get("stops") or []))

        linedesk.publish(LD_ID, geom_src=saved_src, geom_name="",
                         replace_id=1031, geom_name_given=True)
        check("明确传空 geomName（留空=按规范名找）不回填",
              captured.get("geomName") == "", repr(captured.get("geomName")))

        linedesk.publish(LD_ID, geom_src=other_src, replace_id=1031)
        check("换了走向类型就不拿旧线名", captured.get("geomName") == "",
              repr(captured.get("geomName")))

        linedesk.publish(LD_ID, replace_id=1031)
        check("完全不传走向时沿用合并时定的类型+线名",
              captured.get("geomSrc") == saved_src
              and captured.get("geomName") == saved_name,
              (captured.get("geomSrc"), captured.get("geomName")))
    finally:
        linebase.publish, linedesk.log = orig_pub, orig_log

    # --- 3) 主站接口：基本线路详情 + 真实走向 ---
    d = get("/api/line?q=%E4%BA%AC%E6%B2%AA%E9%AB%98%E9%80%9F%E7%BA%BF")
    check("主站详情命中基本线路", d.get("found") is True and d.get("from") == "base")
    check("主站详情站数 = 草稿站数", len(d.get("stations") or []) == n_exp,
          len(d.get("stations") or []))
    check("主站详情带回了走向名", d.get("geomName") == "京沪高铁", d.get("geomName"))
    check("主站详情带出 ldId", d.get("ldId") == LD_ID, d.get("ldId"))
    om = d.get("osm") or {}
    check("详情 osm 指向整理台合并走向", om.get("ldGeom") is True, om.get("file"))
    check("详情 osm 的 name = 基本线路名", om.get("name") == NAME, om.get("name"))
    check("详情 osm 没被塞 kmChain（复线重复会算错）",
          "kmChain" not in om, om.get("kmChain"))

    x = get("/api/line/osm?q=%E4%BA%AC%E6%B2%AA%E9%AB%98%E9%80%9F%E7%BA%BF")
    check("走向接口返回整理台那份", x.get("ldGeom") is True, x.get("file"))
    check("走向接口有折线点", bool(x.get("parts")) and len(x["parts"][0]) > 100,
          len(x["parts"][0]) if x.get("parts") else 0)

    # 定稿线路（沪昆线 route:rg）优先用 ③ 合并保存整合出的真实走向（ld_geom），
    # 而不是被按线名找的旧版本盖掉。
    hu = get("/api/line?q=%E6%B2%AA%E6%98%86%E7%BA%BF")
    hos = hu.get("osm") or {}
    check("route:rg 定稿线走 merged_geom（ldGeom）", hos.get("ldGeom") is True, hos.get("source"))
    check("route:rg 定稿线带真实走向来源", bool(hos.get("source")), hos.get("source"))

    ls = get("/api/lines?q=%E4%BA%AC%E6%B2%AA%E9%AB%98%E9%80%9F%E7%BA%BF&limit=8")
    items = ls.get("items") or []
    check("联想第一项是基本线路", bool(items) and items[0].get("name") == NAME
          and items[0].get("base") is True, items[0] if items else None)
    check("来源版本仍列得出来", any(i.get("name") == "京沪高速线（底表）" for i in items),
          [i.get("name") for i in items])

    print("\n通过 %d 项，失败 %d 项" % (ok, fail))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
