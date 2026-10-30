# -*- coding: utf-8 -*-
"""
server.py — railcn 本地查询服务

零第三方 Web 框架（标准库 http.server），只依赖 requests / openpyxl（系统 Python 已有）。
启动：  python server.py            # 默认 http://127.0.0.1:8686
       python server.py --port 9000 --open
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import mimetypes
import os
import socket
import sys
import threading
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

sys.path.insert(0, BASE_DIR)
import railcore  # noqa: E402
import cnrail   # noqa: E402
import collector  # noqa: E402
import historyq  # noqa: E402
import curate   # noqa: E402
import railgeom  # noqa: E402
import lineseg   # noqa: E402  分段线 ↔ 整线的站序 / 走向互补
import linedesk  # noqa: E402  线路整理台（独立库 linedesk.db）

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")


def today() -> str:
    return dt.date.today().isoformat()


class Handler(BaseHTTPRequestHandler):
    server_version = "railcn/1.0"

    # ---------- 基础响应 ----------
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def json(self, data, code: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def err(self, msg: str, code: int = 400) -> None:
        self.json({"error": str(msg)}, code)

    def file(self, path: str, filename: str) -> None:
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Content-Disposition",
            "attachment; filename*=UTF-8''" + urllib.parse.quote(filename),
        )
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, fmt, *args):          # 安静一点
        if "/api/" in (self.path or ""):
            sys.stderr.write(f"  {self.command} {self.path.split('?')[0]} -> {args[1] if len(args) > 1 else ''}\n")

    # ---------- 路由 ----------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        q = urllib.parse.parse_qs(parsed.query)
        try:
            if path.startswith("/api/"):
                return self.api(path, q)
            return self.static(path)
        except ValueError as exc:
            return self.err(exc, 400)
        except RuntimeError as exc:
            return self.err(exc, 502)
        except Exception as exc:            # noqa: BLE001
            traceback.print_exc()
            return self.err(f"{type(exc).__name__}: {exc}", 500)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.end_headers()

    def do_POST(self):
        """人工校正类操作走 POST（JSON body）"""
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            payload = json.loads(raw or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("请求体必须是 JSON 对象")
        except Exception:                     # noqa: BLE001
            return self.err("请求体不是合法 JSON", 400)

        try:
            if path == "/api/override":
                code = payload.get("code") or ""
                res = railcore.save_override(
                    code,
                    payload.get("via") or [],
                    payload.get("totalKm"),
                    payload.get("note") or "",
                    payload.get("segKm") or {},
                    date=payload.get("date") or "",
                    source="manual",
                )
                # 勾了「同起讫点的车次」：把刚存好的经由逐趟套过去，各存一条自己的校正。
                # 不再有「同类自动沿用」——同步哪些车、同步到哪，全由这次勾选决定。
                synced = []
                if res.get("via") and (payload.get("syncCodes") or []):
                    synced = railcore.sync_via(
                        payload.get("syncCodes") or [], res["via"], code,
                        res.get("note") or "", payload.get("date") or "",
                        bool(payload.get("syncOverwrite")),
                    )
                    for it in synced:
                        if it.get("ok"):
                            try:
                                curate.refresh_one(it["code"])
                            except Exception:        # noqa: BLE001
                                pass
                curate.refresh_one(code)
                return self.json({"ok": True, **res, "synced": synced})

            if path == "/api/line/km":
                res = railcore.save_line_override(
                    payload.get("name"),
                    payload.get("segKm") or {},
                    payload.get("totalKm"),
                    payload.get("note") or "",
                    payload.get("staKm") or {},
                )
                return self.json({"ok": True, **res,
                                  "line": cnrail.line_detail(res["name"])})

            if path == "/api/line/km/rebase":
                res = railcore.rebase_km_from_catalog(payload.get("name"),
                                                      bool(payload.get("save", True)))
                return self.json({"ok": True, **res,
                                  "line": cnrail.line_detail(res["name"])})

            if path == "/api/line/km/clear":
                res = railcore.clear_line_override(payload.get("name"))
                return self.json({"ok": True, "cleared": res,
                                  "line": cnrail.line_detail(payload.get("name"))})

            if path == "/api/coord":
                res = railcore.set_coord(
                    payload.get("name"), payload.get("lat"), payload.get("lon"),
                    payload.get("coord"),
                )
                refreshed = curate.refresh_station(res["name"])
                return self.json({"ok": True, "refreshed": refreshed, **res})

            # ---- 新增线路 / 车站 ----
            if path == "/api/line/new":
                ln = railcore.save_custom_line(
                    payload.get("name"), payload.get("stations") or [],
                    payload.get("note") or "", bool(payload.get("replace")),
                )
                return self.json({"ok": True, "line": ln})

            # 改站序（站中间插站 / 调先后）：自建线路改自己的站表，档案线路存人工站序
            if path == "/api/line/stations":
                ln = railcore.save_line_stations(
                    payload.get("name"), payload.get("stations") or [],
                    payload.get("note") or "",
                )
                return self.json({"ok": True, "line": ln})

            if path == "/api/line/stations/clear":
                return self.json({"ok": True, **railcore.clear_line_stations(payload.get("name"))})

            # 站序倒排：只对「当前这一份来源」生效（OSM 京沪线 ↛ 所有京沪线）
            if path == "/api/line/stations/reverse":
                return self.json(lineseg.reverse_line(
                    payload.get("name"), payload.get("from") or "",
                    payload.get("mode") or ""))

            # 勾/取消「这条线默认用哪一份站序」
            if path == "/api/line/src-pick":
                return self.json({"ok": True,
                                  **railcore.set_src_pick(payload.get("name"),
                                                          payload.get("source") or "")})

            if path == "/api/line/catalog/add":
                ln = railcore.add_catalog_line(payload.get("id"))
                return self.json({"ok": True, "line": ln})

            if path == "/api/line/delete":
                hit = railcore.delete_custom_line(payload.get("name"))
                return self.json({"ok": True, "deleted": hit})

            if path == "/api/station/new":
                st = railcore.save_custom_station(
                    payload.get("name"), payload.get("lat"), payload.get("lon"),
                    payload.get("lines") or [], payload.get("tele") or "",
                    payload.get("bureau") or "", payload.get("note") or "",
                    payload.get("coord"), payload.get("type") or "",
                )
                return self.json({"ok": True, "station": st})

            if path == "/api/station/delete":
                hit = railcore.delete_custom_station(payload.get("name"))
                return self.json({"ok": True, "deleted": hit})

            if path == "/api/coord/clear":
                res = railcore.clear_coord(payload.get("name"))
                refreshed = curate.refresh_station(res["name"])
                return self.json({"ok": True, "refreshed": refreshed, **res})

            # ---- 线路整理台（独立库 linedesk.db，不写 rail.db）----
            if path == "/api/ld/save":
                return self.json({"ok": True,
                                  "line": linedesk.save_line(int(payload.get("id") or 0),
                                                             payload)})

            if path == "/api/ld/new":
                return self.json({"ok": True, "line": linedesk.create_line(
                    payload.get("name"), payload.get("kind") or "",
                    payload.get("note") or "")})

            if path == "/api/ld/delete":
                linedesk.delete_line(int(payload.get("id") or 0))
                return self.json({"ok": True})

            if path == "/api/ld/alias/add":
                return self.json({"ok": True, "line": linedesk.add_alias(
                    int(payload.get("id") or 0), payload.get("source") or "",
                    payload.get("name") or "", payload.get("srcId") or "")})

            if path == "/api/ld/alias/del":
                return self.json({"ok": True,
                                  "line": linedesk.del_alias(int(payload.get("aliasId") or 0))})

            if path == "/api/ld/stops/save":
                return self.json({"ok": True, "line": linedesk.save_stops(
                    int(payload.get("id") or 0), payload.get("stops") or [])})

            if path == "/api/ld/stops/fill":
                return self.json({"ok": True, "line": linedesk.fill_stops(
                    int(payload.get("id") or 0), payload.get("source") or "",
                    payload.get("name") or "")})

            if path == "/api/ld/merge":
                return self.json({"ok": True, "line": linedesk.merge_lines(
                    payload.get("ids") or [], int(payload.get("into") or 0),
                    payload.get("name") or "")})

            if path == "/api/ld/relation":
                return self.json({"ok": True, "line": linedesk.set_relation(
                    int(payload.get("parent") or 0), int(payload.get("child") or 0),
                    payload.get("kind") or "seg", payload.get("note") or "")})

            if path == "/api/ld/relation/del":
                return self.json({"ok": True, "line": linedesk.del_relation(
                    int(payload.get("parent") or 0), int(payload.get("child") or 0))})

            if path == "/api/ld/rebuild":
                linedesk.clear_cache()
                return self.json({"ok": True,
                                  **linedesk.build_seed(bool(payload.get("force")))})

            if path == "/api/curate/scan":
                return self.json(curate.start())
            if path == "/api/curate/refresh":
                # 线路库动过之后按新线路重算这趟车的自动经由（校对页「重算」）
                return self.json(curate.refresh(payload.get("code") or ""))

            if path == "/api/curate/stop":
                return self.json(curate.stop())

            return self.err("未知接口: " + path, 404)
        except ValueError as exc:
            return self.err(exc, 400)
        except Exception as exc:              # noqa: BLE001
            traceback.print_exc()
            return self.err(f"{type(exc).__name__}: {exc}", 500)

    # ---------- API ----------
    def api(self, path: str, q: dict) -> None:
        def one(k: str, default: str = "") -> str:
            v = q.get(k)
            return v[0] if v else default

        if path == "/api/stats":
            st = railcore.stats()
            st["osm"] = railgeom.stats()
            return self.json(st)

        if path == "/api/stations":
            kw = one("q")
            # 三个来源**全列出来，不替用户挑**：同一个站名在 12306 站表、线路档案、
            # OSM 国铁数据里不是一回事（电报码 vs 连接线路 vs 几何归属），同名的也
            # 都展示，界面上标来源让他自己点。
            try:
                extra = railcore.osm_station_suggest(kw, int(one("osmLimit", "8") or 8))
            except Exception:                # noqa: BLE001
                extra = []
            return self.json({"items": railcore.stations.suggest(kw, int(one("limit", "12"))),
                              "osm": extra})

        if path == "/api/trains":
            items = railcore.search_trains(one("q"), int(one("limit", "40")))
            return self.json({"items": items, "count": len(items)})

        if path == "/api/route":
            code = one("code").strip().upper()
            if not code:
                return self.err("缺少车次号")
            date = one("date") or today()
            force = one("force") in ("1", "true")
            cache_only = one("cache") in ("1", "true")
            return self.json(
                railcore.build_route_payload(code, date, force=force, cache_only=cache_only)
            )

        if path == "/api/route/osm":
            code = one("code").strip().upper()
            if not code:
                return self.err("缺少车次号")
            return self.json(railcore.route_osm(code, one("date") or today()))

        if path == "/api/tickets":
            return self.json(
                railcore.query_tickets(one("from"), one("to"), one("date") or today())
            )

        if path == "/api/transfer":
            return self.json(
                railcore.query_transfer(
                    one("from"), one("to"), one("date") or today(), one("middle")
                )
            )

        # ---- cnrail_data_v2 静态知识库 ----
        if path == "/api/station":
            kw = one("q")
            try:
                d = railcore.station_detail(kw)
            except Exception:                # noqa: BLE001
                d = {"found": False}
            if not d.get("found"):
                # 档案/站表都没有、但 OSM 有的：给一张只有 OSM 信息的卡
                card = None
                try:
                    card = railcore.osm_station_card(kw)
                except Exception:            # noqa: BLE001
                    card = None
                if card:
                    return self.json(card)
            # OSM 归属：这个站被哪几条线经过（按到中心线的垂直距离由近及远）
            try:
                d["osmLines"] = railcore.osm_station_lines(kw)
            except Exception:                # noqa: BLE001
                d["osmLines"] = []
            return self.json(d)

        if path == "/api/lines":
            kw = one("q")
            items = cnrail.search_line(kw, int(one("limit", "24")))
            osm = railcore.osm_extras(kw, int(one("osmLimit", "6") or 6))
            # OSM 里有、线路库没收的线：能拼出站序的（资料库 / china-emu / OSM 自己）
            # 标出来。搜「侯西」时「侯西线」本来只显示「OSM 未收录」一条光走向，
            # 其实资料库有 42 站，点进去就该是能编辑的线路详情。
            for it in osm:
                s = lineseg.stations_for(it.get("name")) or {}
                n = len(s.get("stops") or [])
                if n:
                    it["stopsN"] = n
                    it["stopsFrom"] = s.get("source")
            # **同名多来源全列出来，系统不替用户挑**：搜「京广线」时线路库 212 站、
            # 资料库 89 站、china-emu 154 站、OSM 218 站各有各的说法（客运站顺序 /
            # 带线路所 / 几何插值），界面上标来源让用户点，点哪个用哪个。
            variants, picks = {}, {}
            for nm in [x.get("name") for x in items] + [x.get("name") for x in osm]:
                if nm and nm not in variants:
                    try:
                        variants[nm] = lineseg.line_sources(nm)
                    except Exception:        # noqa: BLE001
                        variants[nm] = []
                    # 勾过的默认来源：联想框里把星标画实，别让用户以为没生效
                    picks[nm] = railcore.src_pick(nm)
            return self.json({"items": items, "osm": osm,
                              "variants": variants, "picks": picks})

        if path == "/api/line/stops":
            nm = one("q") or one("name")
            # 勾过默认来源的线，选经由时也用那一份 —— 否则人工在详情页挑了
            # 「china-emu 带线路所的那份」，回头选经由又是另一套站名，对不上。
            pick = railcore.src_pick(nm) if nm else ""
            if pick and pick not in ("lib", "custom"):
                seg = lineseg.stations_for(nm, pick) or {}
                got = [x["name"] for x in (seg.get("stops") or [])]
                if got:
                    return self.json({"name": nm, "found": True, "from": pick,
                                      "items": got, "count": len(got)})
            items = cnrail.line_station_names(nm) if nm else None
            found = items is not None
            stopsFrom = "lib" if found else ""
            if nm:
                if found:
                    # 命名线路的站序里常常没有线路所（沪春线的春申南所、宝成线的灭火沟线路所），
                    # 而经由恰恰经常要在这些所转线 —— 用资料库的同名站序把节点补回来
                    items = railcore.merge_catalog_nodes(nm, items)
                else:
                    # 只在资料库里有、认不出对应自建线路的（银西高铁），直接给资料库站序
                    items = railcore.catalog_stop_names(nm)
                    found = bool(items)
                    stopsFrom = "catalog" if found else ""
                if not found:
                    # OSM 有这条线、线路库和资料库都没收（京哈高速线由京沈 + 沈哈两段组成）：
                    # 按同名前缀的分段线把站序拼出来，供「选经由」和后期自动解析路径用
                    seg = lineseg.stations_for(nm) or {}
                    got = [x["name"] for x in (seg.get("stops") or [])]
                    if got:
                        items, found = got, True
                        stopsFrom = seg.get("source") or "parts"
            return self.json({"name": nm, "found": found, "from": stopsFrom,
                              "items": items or [], "count": len(items or [])})

        if path == "/api/line":
            kw = one("q")
            frm = one("from")
            # 用户勾过的默认来源：没点具体来源时按它取数。库内线路本来就以自己的
            # 站序为准（line_detail 那条路），勾 lib / custom 等于没勾。
            pick = railcore.src_pick(kw)
            eff = frm or pick
            if eff in ("lib", "custom"):
                eff = ""

            def out(payload: dict):
                """每条返回都带上「这条线名在各来源的样子」，详情页据此画来源切换条"""
                try:
                    payload["sources"] = lineseg.line_sources(kw)
                except Exception:            # noqa: BLE001
                    payload["sources"] = []
                payload["pickedFrom"] = frm or ""
                payload["srcPick"] = pick
                payload["effFrom"] = eff or ""
                # 当前这份站序是否被人工倒排过（来源级修正，只影响这一份）
                payload.setdefault("srcFix", [])
                if not payload.get("srcFix"):
                    payload["reversed"] = False
                return self.json(payload)

            # 指定来源：同一条线几个来源各有说法，界面上点了哪个就按哪个拼，
            # 不再走「系统替你挑」的那条路（lib/custom 本来就是库内线路，照旧）
            if eff:
                composed = lineseg.compose_line(kw, eff)
                if not composed:
                    if frm:
                        return self.err("这个来源没有「%s」的站序" % kw, 404)
                    pick = ""                # 默认来源过期了，退回系统规则
                    eff = ""
                else:
                    m = railgeom.meta(kw, composed.get("totalKm"))
                    if m:
                        m["kmChain"] = railgeom.spine_km(m["name"],
                                                         composed.get("totalKm"))
                    return out({**composed, "from": eff,
                                "info": cnrail.catalog_info(kw),
                                "osm": m, "osmOther": railcore.osm_extras(kw, 6)})
            d = cnrail.line_detail(kw)
            if not d:
                # 资料库里另有叫法（京沪高铁 → 京沪高速线），指到库里对应的那条
                alt = railcore.catalog_detail(kw) or {}
                target = alt.get("line_name")
                # 库内那条只是整条线的一段（成昆线 → 成昆线成攀段），别把半条当整条给出去：
                # 站序改从资料库取全线，走向用 OSM 的整条几何，两侧一次补齐
                if target and lineseg.split(target):
                    composed = lineseg.compose_line(kw)
                    if composed:
                        m = railgeom.meta(kw, composed.get("totalKm"))
                        if m:
                            m["kmChain"] = railgeom.spine_km(m["name"], composed.get("totalKm"))
                        return out({**composed, "composedFrom": target,
                                          "info": cnrail.catalog_info(kw),
                                          "osm": m, "osmOther": railcore.osm_extras(kw, 6)})
                if target:
                    d = cnrail.line_detail(target)
                    if d:
                        m = (railgeom.meta(kw, d.get("totalKm"))
                             or railgeom.meta(target, d.get("totalKm")))
                        if not m:
                            # 分段线名在 OSM 里不存在，借整条线的几何再裁到本段
                            m = lineseg.borrow_geometry(target, d.get("stations"),
                                                        d.get("totalKm"))
                        if m:
                            m["kmChain"] = railgeom.spine_km(m["name"], d.get("totalKm"))
                        return out({**d, "aliasOf": kw, "osm": m,
                                          "osmOther": railcore.osm_extras(kw, 6)})
                # 线路库、资料库都没有：OSM 有整条几何的话，站序也能从资料库/分段拼出来
                composed = lineseg.compose_line(kw)
                if composed:
                    m = railgeom.meta(kw, composed.get("totalKm"))
                    if m:
                        m["kmChain"] = railgeom.spine_km(m["name"], composed.get("totalKm"))
                    return out({**composed, "composed": True,
                                      "info": cnrail.catalog_info(kw),
                                      "osm": m, "osmOther": railcore.osm_extras(kw, 6)})
                return self.err("未找到线路: " + kw, 404)
            m = railgeom.meta(d.get("name") or kw, d.get("totalKm"))
            if not m:
                # 库里按段命名、OSM 只有整条（成昆线元昆段 ← 成昆线），借来再按
                # 本段首末站裁掉两头，免得地图把整条线的走向画到半条线头上
                m = lineseg.borrow_geometry(d.get("name") or kw, d.get("stations"),
                                            d.get("totalKm"))
            if m:
                m["kmChain"] = railgeom.spine_km(m["name"], d.get("totalKm"))
            # 同名前缀下、线路库没收录的 OSM 线（「津霸线」之于「津霸客运专线」）
            return out({**d, "osm": m, "osmOther": railcore.osm_extras(kw, 6)})

        # OSM 线路几何（CRTrainRoute 整理）：真实走向 + 几何里程 + 桥隧统计
        if path == "/api/line/osm":
            # km 给了已知里程时，几何里程差太多的残段会被排除
            nm = one("q") or one("name")
            g = railgeom.geom(nm, expect_km=one("km") or None)
            if not g:
                # 分段线名（成昆线元昆段）在 OSM 里没有独立线名：借父线的几何，
                # 按本段首末站裁出那一截
                d = cnrail.line_detail(nm) if nm else None
                g = lineseg.borrow_geometry(nm, (d or {}).get("stations"),
                                            (d or {}).get("totalKm"))
                if g:
                    g["kmChain"] = g.get("km")
            if not g:
                return self.err("OSM 数据里没有这条线路: " + (nm or ""), 404)
            return self.json(g)

        if path == "/api/line/osm/list":
            kw = one("q")
            used, _fam, _km = railcore._lib_index()
            items = railgeom.search(kw, int(one("limit", "30")))
            for it in items:
                it["inLib"] = it["name"] in used
                it["libName"] = used.get(it["name"]) if it["name"] in used else None
                if not it["inLib"]:
                    # 线路库没有、但能补出站序的（资料库同名 / 同前缀分段线拼）：
                    # 列表里先标出来，点进去就不会只剩一条光走向
                    s = lineseg.stations_for(it["name"]) or {}
                    n = len(s.get("stops") or [])
                    if n:
                        it["stopsN"] = n
                        it["stopsFrom"] = s.get("source")
            return self.json({"items": items, "stats": railgeom.stats()})

        # OSM-only 线路补站：按走向反查车站库，给「新增线路」弹窗预填站点与里程
        if path == "/api/line/osm/stations":
            return self.json(railcore.osm_line_stations(
                one("q") or one("name"),
                int(one("radius", "1200") or 1200),
                int(one("extend", "3000") or 3000),
            ))

        if path == "/api/line/catalog":
            items = railcore.catalog_search(one("q"), int(one("limit", "20")))
            return self.json({"items": items, "count": len(items)})

        if path == "/api/line/catalog/detail":
            d = railcore.catalog_detail(one("id") or one("q"))
            if not d:
                return self.err("资料库里没有这条线路", 404)
            d["osm"] = railgeom.meta(d.get("line_name") or d.get("name") or "",
                                     d.get("length_km"))
            return self.json(d)

        # ---- 线路整理台 ----
        if path == "/api/ld/stats":
            return self.json(linedesk.stats())

        if path == "/api/ld/lines":
            return self.json(linedesk.list_lines(
                one("q"), one("kind"), one("source"), one("status"),
                bool(one("hasStops")),
                int(one("limit", "200") or 200), int(one("offset", "0") or 0)))

        if path == "/api/ld/line":
            d = linedesk.line_detail(int(one("id", "0") or 0))
            if not d:
                return self.err("整理库里没有这条线路", 404)
            return self.json(d)

        if path == "/api/ld/geom":
            return self.json(linedesk.geom(int(one("id", "0") or 0)))

        if path == "/api/ld/candidates":
            return self.json({"items": linedesk.family_candidates(
                int(one("id", "0") or 0))})

        if path == "/api/ld/source/search":
            return self.json({"items": linedesk.search_source(
                one("q"), int(one("limit", "40") or 40))})

        if path == "/api/ld/source/stops":
            return self.json(linedesk.source_stops(one("source"), one("name")))

        if path == "/api/ld/log":
            return self.json({"items": linedesk.recent_log(int(one("limit", "60") or 60))})

        if path == "/api/ld/export":
            return self.json(linedesk.export_map())

        # ---- 按日期批量下载 ----
        if path == "/api/dates":
            return self.json({"today": today(), "items": collector.avail_dates(),
                              "chain": collector.chain_status()})

        if path == "/api/collect/chain/status":
            return self.json(collector.chain_status())

        if path == "/api/collect/chain/start":
            return self.json(collector.chain_start(
                int(one("workers", "6") or 6),
                one("enum", "1") in ("1", "true"),
                one("start") or None,
            ))

        if path == "/api/collect/chain/stop":
            return self.json(collector.chain_stop())

        if path == "/api/collect/status":
            return self.json(collector.status(one("date") or today()))

        if path == "/api/collect/start":
            date = one("date") or today()
            workers = int(one("workers", "6") or 6)
            do_enum = one("enum", "0") in ("1", "true")
            return self.json(collector.start(date, workers, do_enum))

        if path == "/api/collect/stop":
            return self.json(collector.stop(one("date") or today()))

        if path == "/api/collect/export":
            date = one("date") or today()
            fmt = one("fmt", "json")
            p, size = collector.export(date, fmt)
            return self.json({"ok": True, "file": os.path.basename(p), "size": size})

        if path == "/api/collect/download":
            date = one("date") or today()
            fmt = one("fmt", "json")
            p, _ = collector.export(date, fmt)
            return self.file(p, os.path.basename(p))

        if path == "/api/collect/files":
            return self.json({"items": collector.export_list()})

        # ---- 历史数据（已落库，长期保留）----
        if path == "/api/history":
            items = collector.history_dates()
            return self.json({
                "today": today(),
                "count": len(items),
                "dates": items,
                "files": collector.export_list(),
            })

        if path == "/api/history/trains":
            date = one("date") or today()
            return self.json(
                collector.history_trains(
                    date, one("q"), int(one("limit", "40") or 40), int(one("offset", "0") or 0)
                )
            )

        # 历史查询：车次开行日历 / 大类统计 / 某日停站时刻
        if path == "/api/history/coverage":
            return self.json(historyq.coverage(one("from"), one("to")))

        if path == "/api/history/calendar":
            return self.json(historyq.train_calendar(one("code"), one("from"), one("to")))

        if path == "/api/history/types":
            return self.json(historyq.type_stats(one("from"), one("to"), one("type")))

        if path == "/api/history/stops":
            return self.json(historyq.stops_of(one("code"), one("date")))

        # ---- 数据校对（补全经由 / 里程 / 坐标）----
        if path == "/api/curate/stats":
            return self.json(curate.status())

        if path == "/api/curate/list":
            return self.json(
                curate.listing(
                    one("filter", "todo"), one("q"),
                    int(one("limit", "60") or 60), int(one("offset", "0") or 0),
                )
            )

        if path == "/api/curate/detail":
            return self.json(curate.detail(one("code")))

        if path == "/api/curate/mate":
            # 同起讫点某趟车的停站详情（勾选前展开看）
            return self.json(curate.mate_detail(one("code")))

        if path == "/api/curate/cands":
            a, b = one("from"), one("to")
            items = railcore.cand_lines(a, b) if (a and b) else []
            return self.json({"from": a, "to": b, "items": items, "count": len(items)})

        # ---- 用户新增的线路 / 车站 ----
        if path == "/api/custom":
            return self.json({"lines": railcore.list_custom_lines(),
                              "stations": railcore.list_custom_stations()})

        if path == "/api/stations/names":
            names = railcore.stations.all_names()
            return self.json({"items": names, "count": len(names)})

        if path == "/api/curate/lineNames":
            items = [{"name": l["name"], "n": l.get("n"), "totalKm": l.get("totalKm"),
                      "kmComplete": l.get("kmComplete"), "kmManual": l.get("kmManual", False)}
                     for l in cnrail.search_line("", 10000)]
            # 干线在前（站数多的排前面），下拉里默认先看到主要线路
            items.sort(key=lambda x: (-(x.get("n") or 0), x["name"]))
            return self.json({"items": items, "count": len(items),
                              "names": [x["name"] for x in items]})

        if path == "/api/curate/scan":
            return self.json(curate.start())

        if path == "/api/curate/stop":
            return self.json(curate.stop())

        if path == "/api/override":
            return self.json({"code": one("code").upper(),
                              "override": railcore.get_override(one("code"))})

        return self.err("未知接口: " + path, 404)

    # ---------- 静态文件 ----------
    def static(self, path: str) -> None:
        if path in ("", "/"):
            path = "/index.html"
        rel = path.lstrip("/").replace("..", "")
        full = os.path.join(STATIC_DIR, rel)
        if not os.path.isfile(full):
            return self._send(404, b"404 Not Found", "text/plain; charset=utf-8")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        with open(full, "rb") as f:
            body = f.read()
        self._send(200, body, ctype)


def free_port(preferred: int) -> int:
    for port in [preferred] + list(range(preferred + 1, preferred + 20)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return preferred


def main() -> None:
    # 输出被重定向到文件 / 管道时，控制台编码（中文 Windows 是 cp936）编不出横幅里的 emoji，
    # print 抛 UnicodeEncodeError 会把服务带走。换成 replace，宁可显示成 ? 也别崩
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8686)
    ap.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    args = ap.parse_args()

    port = free_port(args.port)
    url = f"http://127.0.0.1:{port}/"
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True

    st = railcore.stats()
    print("=" * 62)
    print("  🚄 railcn 本地车次查询")
    print("=" * 62)
    print(f"  地址      : {url}")
    print(f"  车次库    : {st['trains']} 条")
    print(f"  经停站缓存: {st['routesCached']} 趟")
    print(f"  车站坐标  : {st['stationsWithCoord']}/{st['stations']}（{st['coordCoverage']}%）")
    print("-" * 62)
    print("  Ctrl+C 退出")
    print("=" * 62)

    if args.open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
        srv.shutdown()


if __name__ == "__main__":
    main()
