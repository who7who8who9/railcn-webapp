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
            return self.json({"items": railcore.stations.suggest(one("q"), int(one("limit", "12")))})

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
            return self.json(railcore.station_detail(one("q")))

        if path == "/api/lines":
            kw = one("q")
            return self.json({"items": cnrail.search_line(kw, int(one("limit", "24"))),
                              "osm": railcore.osm_extras(kw, int(one("osmLimit", "6") or 6))})

        if path == "/api/line/stops":
            nm = one("q") or one("name")
            items = cnrail.line_station_names(nm) if nm else None
            found = items is not None
            if nm:
                if found:
                    # 命名线路的站序里常常没有线路所（沪春线的春申南所、宝成线的灭火沟线路所），
                    # 而经由恰恰经常要在这些所转线 —— 用资料库的同名站序把节点补回来
                    items = railcore.merge_catalog_nodes(nm, items)
                else:
                    # 只在资料库里有、认不出对应自建线路的（银西高铁），直接给资料库站序
                    items = railcore.catalog_stop_names(nm)
                    found = bool(items)
            return self.json({"name": nm, "found": found,
                              "items": items or [], "count": len(items or [])})

        if path == "/api/line":
            kw = one("q")
            d = cnrail.line_detail(kw)
            if not d:
                # 资料库里另有叫法（京沪高铁 → 京沪高速线），指到库里对应的那条
                alt = railcore.catalog_detail(kw) or {}
                target = alt.get("line_name")
                if target:
                    d = cnrail.line_detail(target)
                    if d:
                        m = (railgeom.meta(kw, d.get("totalKm"))
                             or railgeom.meta(target, d.get("totalKm")))
                        if m:
                            m["kmChain"] = railgeom.spine_km(m["name"], d.get("totalKm"))
                        return self.json({**d, "aliasOf": kw, "osm": m,
                                          "osmOther": railcore.osm_extras(kw, 6)})
                return self.err("未找到线路: " + kw, 404)
            m = railgeom.meta(d.get("name") or kw, d.get("totalKm"))
            if m:
                m["kmChain"] = railgeom.spine_km(m["name"], d.get("totalKm"))
            # 同名前缀下、线路库没收录的 OSM 线（「津霸线」之于「津霸客运专线」）
            return self.json({**d, "osm": m, "osmOther": railcore.osm_extras(kw, 6)})

        # OSM 线路几何（CRTrainRoute 整理）：真实走向 + 几何里程 + 桥隧统计
        if path == "/api/line/osm":
            # km 给了已知里程时，几何里程差太多的残段会被排除
            g = railgeom.geom(one("q") or one("name"), expect_km=one("km") or None)
            if not g:
                return self.err("OSM 数据里没有这条线路: " + (one("q") or one("name")), 404)
            return self.json(g)

        if path == "/api/line/osm/list":
            kw = one("q")
            used, _fam, _km = railcore._lib_index()
            items = railgeom.search(kw, int(one("limit", "30")))
            for it in items:
                it["inLib"] = it["name"] in used
                it["libName"] = used.get(it["name"]) if it["name"] in used else None
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
