# -*- coding: utf-8 -*-
"""lineosm.py — 「车站/线路」页与「OSM 走向编辑器」共用的走向几何入口

以前主站 `/api/line/osm` 和 `osmedit.load_geom()` 各写了一套解析顺序，
编辑器还额外优先读 `ld_geom.spine_gz`（抽稀 + 补断口的主轴），又把多段
硬串成一条折线，结果同一条线两处画出来形状完全不同：滨绥线 902 km 只剩
13 km、京广高铁 4137 km 变 2064 km、沪昆线冒出 274 km 直线。

这里把主站那套顺序原样抽出来，两边调同一个函数，保证「调用现有线路」
看到的就是主站正在显示的那份 `parts` 多段，杜绝再漂移。
"""
from __future__ import annotations

from typing import Optional

import cnrail
import confirmed
import geoimport
import linebase
import linedesk
import lineseg
import railgeom


def geom(name: str, confirmed_only: bool = False,
         expect_km=None) -> Optional[dict]:
    """返回与主站 `/api/line/osm` 完全相同的几何 dict（含 `parts` 多段）。

    confirmed_only=True 时只认整理台确认线路白名单（`confirmed.geom()`），
    不再按线名去 rail.db 找同名旧版本；否则依次按
    基本线路 ld_geom → 来源包 route → 导入 GeoJSON → OSM → 分段线借几何。
    """
    nm = (name or "").strip()
    if not nm:
        return None
    if confirmed_only:
        return confirmed.geom(nm)

    g = None
    # 整理台定稿时指定了「某个来源包自己的走向」（route:hdx / rg / cnrail）：
    # ③ 合并保存已经把那份走向整合进 ld_geom，它才是定稿线路的权威几何，
    # 优先用它（带 ldGeom 标记 + 基础线名）；只在它没折点时退回来源包几何。
    try:
        b = linebase.get(nm) or {}
        if b.get("ld_id"):
            g = linedesk.merged_geom(b["ld_id"], nm)
        if not (g and g.get("parts")):
            rf = linebase.route_of(nm)
            if rf:
                g = linebase.route_geom(rf[1], rf[0])
    except Exception:                    # noqa: BLE001  表结构旧 / 库被占用
        g = None
    if not g:
        g = geoimport.geom(nm)          # 导入的 GeoJSON 优先
    if not g:
        g = railgeom.geom(nm, expect_km=expect_km or None)
    if not g:
        # 分段线名（成昆线元昆段）在 OSM 里没有独立线名：借父线的几何，
        # 按本段首末站裁出那一截
        try:
            d = cnrail.line_detail(nm)
            g = lineseg.borrow_geometry(nm, (d or {}).get("stations"),
                                        (d or {}).get("totalKm"))
        except Exception:                # noqa: BLE001
            g = None
        if g:
            g["kmChain"] = g.get("km")
    return g or None
