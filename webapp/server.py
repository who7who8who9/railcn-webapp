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
import linegroup  # noqa: E402  同名多来源版本（京沪线 / 京沪线（底表）/…）归组
import linebase   # noqa: E402  基本线路（整理台定稿、全面接管的那条）
import linedesk  # noqa: E402  线路整理台（独立库 linedesk.db）
import geoimport  # noqa: E402  手工导入 GeoJSON 线路
import backup_lines  # noqa: E402  备份里的线路：看、挑、导回来
import trainmap  # noqa: E402  全路网列车位置（时刻表推算）
import traintrack  # noqa: E402  行车路线：已整理线路贴真实走向，其余站间直线
import confirmed  # noqa: E402  整理台「全部确认」线路（车站/线路页唯一可见的线路）
import lineosm  # noqa: E402  主站 /api/line/osm 与走向编辑器共用的几何入口
import confvia  # noqa: E402  数据校对的线路口径（只用整理台确认线路）
import osmedit  # noqa: E402  OSM 走向编辑器（独立页面 /osmedit.html）
import linemap  # noqa: E402  确认线路地图（独立页面 /linemap.html）

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")


def today() -> str:
    return dt.date.today().isoformat()


def line_detail_eff(name, confirmed_only: bool = False):
    """线路详情：基本线路优先 —— 它才是全项目生效的那一份。

    编辑里程保存后必须回这一份：库里常躺着同名但站数不同的来源版本
    （沪昆线 base 207 站 / line_custom 183 站），回错了前端整条站序会跳掉。
    车站/线路页（confirmed=1）要回整理台确认线路自己的详情，否则保存完
    详情会跳回旧的多来源视图。
    """
    if confirmed_only:
        d = confirmed.detail_payload(name) if name else None
        if d:
            return d
    d = linebase.detail_payload(name) if name else None
    return d or cnrail.line_detail(name)


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
                nm = res.get("name") or res.get("cleared") or payload.get("name")
                return self.json({"ok": True, **res,
                                  "line": line_detail_eff(nm,
                                      bool(payload.get("confirmed")))})

            if path == "/api/line/km/rebase":
                res = railcore.rebase_km_from_catalog(payload.get("name"),
                                                      bool(payload.get("save", True)))
                return self.json({"ok": True, **res,
                                  "line": line_detail_eff(
                                      res.get("name") or payload.get("name"),
                                      bool(payload.get("confirmed")))})

            if path == "/api/line/km/clear":
                res = railcore.clear_line_override(payload.get("name"))
                return self.json({"ok": True, "cleared": res,
                                  "line": line_detail_eff(payload.get("name"),
                                      bool(payload.get("confirmed")))})

            if path == "/api/coord":
                res = railcore.set_coord(
                    payload.get("name"), payload.get("lat"), payload.get("lon"),
                    payload.get("coord"), payload.get("src"),
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

            # 勾/取消「这条线默认用哪个来源版本」（京沪线 → 京沪线（底表））
            # 设了之后：数据校对的线路选择只给这一份，地图也按它的站序画。
            if path == "/api/line/default":
                try:
                    r = linegroup.set_default(payload.get("name"),
                                              payload.get("pick") or "")
                except ValueError as e:
                    return self.err(str(e), 400)
                return self.json({"ok": True, **r,
                                  "group": linegroup.info(payload.get("name"))})

            # 手工导入 GeoJSON 线路：前端把文件读成对象塞在 data 里一起传
            if path == "/api/line/import-geojson":
                raw = payload.get("data")
                if raw in (None, ""):
                    return self.err("没有收到 GeoJSON 内容", 400)
                try:
                    r = geoimport.import_line(raw, payload.get("fileName") or "",
                                              bool(payload.get("replace")),
                                              (payload.get("note") or "").strip())
                except ValueError as e:
                    return self.err(str(e), 400)
                return self.json(r)

            # 导入过的 GeoJSON 线路一览（走向从哪来、什么时候导的）
            if path == "/api/line/import-geojson/list":
                return self.json({"items": geoimport.list_imported()})

            # 备份里的某条线：挑定了才正式写进线路库
            if path == "/api/backup/restore":
                try:
                    r = backup_lines.restore(payload.get("tag"), payload.get("source"),
                                             payload.get("name"), bool(payload.get("replace")))
                except ValueError as e:
                    return self.err(str(e), 400)
                return self.json(r)

            if path == "/api/line/catalog/add":
                ln = railcore.add_catalog_line(payload.get("id"))
                return self.json({"ok": True, "line": ln})

            if path == "/api/line/delete":
                hit = railcore.delete_custom_line(payload.get("name"))
                # 走向编辑器的草稿跟着走，否则删完线它还挂在编辑器列表里
                osmedit.drop_draft(payload.get("name") or "")
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

            # 站序指定位置插入新站：pos = 插入后的 0-based 起始下标，-1 = 末尾
            if path == "/api/ld/stops/insert":
                try:
                    pos = int(payload.get("pos") if payload.get("pos") is not None else -1)
                except (TypeError, ValueError):
                    pos = -1
                return self.json({"ok": True, "line": linedesk.insert_stops(
                    int(payload.get("id") or 0), payload.get("names") or [], pos)})

            # 站序反向：倒排站序 + 按「原末站里程 − 原里程」重算，新起点 0（缺的不插值）
            if path == "/api/ld/stops/reverse":
                return self.json({"ok": True, "line": linedesk.reverse_stops(
                    int(payload.get("id") or 0))})

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
            # ---- 基本线路：整理台定稿 → 全项目生效 ----
            if path == "/api/ld/publish":
                gname_given = "geomName" in (payload or {})
                return self.json({"ok": True, "line": linedesk.publish(
                    int(payload.get("id") or 0),
                    payload.get("geomSrc") or "",
                    payload.get("geomName") or "",
                    payload.get("name") or "",
                    int(payload.get("replaceId") or 0),
                    gname_given)})
            if path == "/api/ld/unpublish":
                nm = (payload or {}).get("name") or ""
                return self.json({"ok": linebase.unpublish(nm), "name": nm})

            # ---- 整理台：线路来源 / 站点来源，分别确认 ----
            if path == "/api/ld/src/line":
                # ① 只登记走向来源（OSM 值），不再顺带改规范名 / 起讫（那是基本信息的事）
                return self.json({"ok": True, **linedesk.set_line_sources(
                    int(payload.get("id") or 0), payload.get("items") or [],
                    bool(payload.get("rename", False)))})
            if path == "/api/ld/src/stop":
                return self.json({"ok": True, "line": linedesk.confirm_stop_source(
                    int(payload.get("id") or 0), payload.get("source") or "",
                    payload.get("name") or "", bool(payload.get("fill")))})
            if path == "/api/ld/src/clear":
                return self.json({"ok": True, "line": linedesk.clear_source(
                    int(payload.get("id") or 0), payload.get("kind") or "both")})
            if path == "/api/ld/finalize":
                # ③ 合并保存为确认线路 = 合并 ① 走向 + ② 站点，并**直接定为基本线路**
                # （2026-11-06 用户取消 ④）。自动 publish 放在 HTTP 编排层：
                # 直接调 linedesk.finalize() 的脚本 / 临时库回归仍是「只合并确认」，
                # 不会被迫往 rail.db 写基本线路。
                lid = int(payload.get("id") or 0)
                d = linedesk.finalize(lid, payload.get("name") or "")
                merged = dict(d.get("merged") or {})
                pub_err = ""
                try:
                    import linebase as _lb
                    old = _lb.get_by_ld(lid)
                    # ④ 的走向下拉已取消：基本线路直接用 ① 确认 / ③ 合并时定下的那份
                    linedesk.publish(lid, "", "", payload.get("name") or "",
                                     int(old["id"]) if old and old.get("id") else 0)
                    d = linedesk.line_detail(lid) or d
                except Exception as e:                       # noqa: BLE001
                    pub_err = str(e)
                d["merged"] = merged
                d["published"] = not pub_err
                d["publishError"] = pub_err
                return self.json({"ok": True, "line": d})

            # ---- 整理台：导入 GeoJSON（不写 rail.db，站序 + 走向进整理库）----
            if path == "/api/ld/import-geojson":
                raw = payload.get("data")
                if raw in (None, ""):
                    return self.err("没有收到 GeoJSON 内容", 400)
                try:
                    lid = int(payload.get("id") or 0)
                    if not lid:                     # 没指定线路：按文件名/线名新建一条空线
                        import geoimport as _gi
                        pg = _gi.parse(raw, payload.get("fileName") or "")
                        nm = (payload.get("name") or pg.get("name") or "").strip()
                        if not nm:
                            return self.err("文件里没写线路名称，也推不出文件名", 400)
                        if linedesk.find_line(nm):
                            return self.err(f"整理台里已经有「{nm}」，先点开它再导入", 400)
                        lid = linedesk.create_line(nm)["id"]
                    return self.json(linedesk.import_geojson(
                        lid, raw, payload.get("fileName") or "",
                        payload.get("name") or "", payload.get("only") or ""))
                except ValueError as e:
                    return self.err(str(e), 400)

            # ---- 整理台：资料库介绍（正式名称 / 起止站点的口径）----
            if path == "/api/ld/ref/apply":
                return self.json({"ok": True, **linedesk.apply_ref(
                    int(payload.get("id") or 0), payload.get("what") or "all")})
            if path == "/api/ld/ref/import":
                return self.json({"ok": True, "count": linedesk.ensure_ref(
                    bool(payload.get("force")))})

            if path == "/api/curate/scan":
                return self.json(curate.start())
            if path == "/api/curate/refresh":
                # 线路库动过之后按新线路重算这趟车的自动经由（校对页「重算」）
                return self.json(curate.refresh(payload.get("code") or ""))

            if path == "/api/curate/stop":
                return self.json(curate.stop())

            if path == "/api/curate/renum":
                # 两个勾选框：阶段一 / 阶段二已切换到新车次，勾上才把那批字头纳入校对
                st = curate.set_renum(
                    phase1=(payload.get("phase1") if "phase1" in payload else None),
                    phase2=(payload.get("phase2") if "phase2" in payload else None),
                )
                return self.json({"ok": True, "renum": st, "counts": curate.counts()})

            # ---- OSM 走向编辑器 ----
            if path == "/api/osmedit/geom":
                try:
                    res = osmedit.save_geom(
                        payload.get("name"),
                        payload.get("parts") or [],
                        total_km=payload.get("totalKm"),
                        src=payload.get("src") or "",
                        note=payload.get("note") or "",
                        apply=bool(payload.get("apply")),
                        target=payload.get("target") or "",
                        confirmed_line=bool(payload.get("confirmed")),
                    )
                    return self.json(res)
                except ValueError as exc:
                    return self.err(str(exc), 400)

            # 新建线路：沿线抓站（把走向附近的车站排成候选站序）
            if path == "/api/osmedit/stations":
                try:
                    return self.json(osmedit.suggest_stations(
                        payload.get("parts") or [],
                        radius_m=float(payload.get("radius") or 2000),
                        name=payload.get("name") or "",
                    ))
                except ValueError as exc:
                    return self.err(str(exc), 400)

            # 新建线路：画的走向 + 挑好的车站 → 建线入库（走 GeoJSON 导入通道）
            if path == "/api/osmedit/new":
                try:
                    return self.json(osmedit.create_line(
                        payload.get("name"),
                        payload.get("parts") or [],
                        payload.get("stops") or [],
                        note=payload.get("note") or "",
                        replace=bool(payload.get("replace")),
                    ))
                except ValueError as exc:
                    return self.err(str(exc), 400)

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
            # mode=legacy 走原来的贪心覆盖画法；默认走「确认线路站序」新画法
            return self.json(railcore.route_osm(
                code, one("date") or today(), mode=one("mode") or "confirmed"))

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

        # 车站的全部候选坐标（车站/线路页的「多个坐标」清单也可单独取）
        if path == "/api/station/coords":
            kw = one("q")
            info = railcore.stations.coord_info(kw)
            return self.json(railcore.stationcoords.candidates(
                kw, info.get("name"),
                cur_lat=info.get("lat"), cur_lon=info.get("lon")))

        if path == "/api/lines":
            kw = one("q")
            # 「车站/线路」页的线路模式：只认线路整理台里**完成全部确认**的线路。
            # 底表 / 资料库 / china-emu / OSM / 备份导回来的来源版本、候选线路、
            # ★ 选版本这些统统不出现。confirmed=1 是一个显式开关 —— 数据校对、
            # 列车地图、备份面板等老调用不走这里，口径完全不变。
            if one("confirmed") in ("1", "yes", "true"):
                lim = int(one("limit", "24") or 24)
                items = confirmed.list_all(kw)[:lim]
                return self.json({
                    "items": items, "osm": [], "variants": {}, "picks": {},
                    "groups": {}, "confirmedOnly": True,
                    "total": len(confirmed.list_all(kw)),
                    "confirmedTotal": len(confirmed.list_all("")),
                })
            items = cnrail.search_line(kw, int(one("limit", "24")))
            # 基本线路（整理台定稿、全面接管）在搜索里也要说话：
            #   · 同名项按基本线路的站数 / 里程给 —— 否则「沪昆线」显示的是 line_custom
            #     里那条合并脚本写的 183 站 / 3172 km，点进去却是定稿的 211 站，两头对不上；
            #   · 线路库里压根没有这条（整理台新建 / 定稿后改了名）就补进来，
            #     不然在整理台编好了却在搜索里找不到。
            try:
                _bases = {b["name"]: b for b in linebase.list_all()}
            except Exception:                # noqa: BLE001
                _bases = {}
            for it in items:
                _b = _bases.get(it.get("name"))
                if _b:
                    it["n"] = _b["nStop"]
                    it["totalKm"] = _b["total_km"]
                    it["base"] = True
            if kw:
                _have = {x.get("name") for x in items}
                _extra = [b for nm, b in _bases.items() if kw in nm and nm not in _have]
                # 完全同名的排前面，其次站多的
                _extra.sort(key=lambda b: (b["name"] != kw, -(b["nStop"] or 0)))
                _extra = _extra[:6]
                if _extra:
                    items = [{"name": b["name"], "n": b["nStop"],
                              "totalKm": b["total_km"], "color": None,
                              "kmComplete": True, "kmManual": False, "base": True}
                             for b in _extra] + items
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
            # 备份导回来的「（底表）/（OSM）/（自建）」也是同一条线的版本，在
            # line_sources 里一并列（kind=member）。
            variants, picks, groups = {}, {}, {}
            for it in items:
                try:
                    groups[it["name"]] = linegroup.info(it["name"])
                except Exception:            # noqa: BLE001
                    groups[it["name"]] = {}
            # 联想下拉最多画 16 行（app.js 的 slice(0,16)），多算的没人看；
            # 每条要探三套来源表，24 条能拖到 1.5 s。两边数字要一致，
            # 少算了后面的行会被前端当成「无从查站序」。
            for nm in ([x.get("name") for x in items][:16]
                       + [x.get("name") for x in osm]):
                if nm and nm not in variants:
                    try:
                        variants[nm] = lineseg.line_sources(nm)
                    except Exception:        # noqa: BLE001
                        variants[nm] = []
                    # 勾过的默认来源：联想框里把星标画实，别让用户以为没生效
                    picks[nm] = linegroup.default_of(nm) or railcore.src_pick(nm)
            return self.json({"items": items, "osm": osm,
                              "variants": variants, "picks": picks,
                              "groups": groups})

        if path == "/api/line/confirmed":
            # 车站/线路页头部用：整理台一共确认了几条、还有多少草稿。
            # full=1 另外给一份全量清单（带确认时间，按最新确认在前），页面下方
            # 常驻列表用它渲染并在输入框里本地筛选，不用每敲一个字打一次接口。
            st = confirmed.stats()
            if one("full") in ("1", "yes", "true"):
                st["items"] = confirmed.recent()
            return self.json({"ok": True, **st})

        if path == "/api/line/default":
            nm = one("q") or one("name")
            return self.json({"ok": True, "name": nm,
                              "pick": linegroup.default_of(nm),
                              "group": linegroup.info(nm)})

        if path == "/api/line/stops":
            nm = one("q") or one("name")
            # 数据校对（confirmed=1）：站序一律取整理台确认的那一份。
            # 线路库里同名版本站序不一样（京广线 193 站 vs 整理台 212 站），
            # 校对按确认站序填「从…到…」，落站才对得上。
            if one("confirmed") in ("1", "yes", "true"):
                d = confirmed.detail_payload(nm) if nm else None
                got = [s["name"] for s in (d or {}).get("stations") or []]
                return self.json({"name": nm, "found": bool(got),
                                  "from": "confirmed", "confirmed": True,
                                  "items": got, "count": len(got)})
            # 同一条线的几个版本（京沪线（底表）/（OSM）/…）：
            #   · 带「（来源）」后缀 → 就按那一份，不替换；
            #   · 有默认版本 → 按默认那份（校对下拉里这时候显示的就是默认那份，站数对得上）；
            #   · 没默认、但名字本身就是库里的一条线 → **也别替换**：下拉里「京沪线 163 站」
            #     点进去就该给 163 站，不能偷偷换成底表的 150 站；
            #   · 没默认、名字只是个基础名（京沪高速线）→ 按底表 / 站数最多那份。
            if nm and not linegroup.split(nm)[1]:
                # 基本线路：站序用整理台定稿那一份（点名看某个来源版本时不接管）
                try:
                    bn = linebase.stop_names(nm)
                except Exception:            # noqa: BLE001
                    bn = None
                if bn:
                    return self.json({"name": nm, "found": True, "from": "base",
                                      "base": True, "items": bn, "count": len(bn)})
                pick0 = linegroup.default_of(nm)
                if pick0:
                    nm = pick0
                elif nm not in [m["name"] for m in linegroup.members(nm)]:
                    eff = linegroup.effective(nm)
                    if eff.get("members") and eff["name"] != nm:
                        nm = eff["name"]
            # 勾过默认来源的线，选经由时也用那一份 —— 否则人工在详情页挑了
            # 「china-emu 带线路所的那份」，回头选经由又是另一套站名，对不上。
            pick = railcore.src_pick(nm) if nm else ""
            if pick and pick not in ("lib", "custom"):
                seg = lineseg.stations_for(nm, pick) or {}
                got = [railcore.canon_station(x["name"]) for x in (seg.get("stops") or [])]
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
            # 站名出门统一不带「站」（杭州东站 → 杭州东），和线路详情 / 车站页一致
            items = [railcore.canon_station(x) for x in (items or [])]
            return self.json({"name": nm, "found": found, "from": stopsFrom,
                              "items": items, "count": len(items)})

        # ---- 备份里的线路：先看、再挑，挑中才写库 ----
        if path == "/api/backup/lines":
            return self.json(backup_lines.lines(
                one("tag"), one("source"), one("q"),
                int(one("limit", "200")), int(one("offset", "0"))))

        if path == "/api/backup/line":
            try:
                return self.json(backup_lines.detail(one("tag"), one("source"), one("name")))
            except ValueError as e:
                return self.err(str(e), 404)

        if path == "/api/backup/geom":
            g = backup_lines.geom_preview(one("tag"), one("name"))
            return self.json(g or {})

        if path == "/api/line":
            # 车站/线路页（confirmed=1）：白名单之外一律不给，也不退回资料库 / OSM /
            # 备份来源版本。停站与走向直接读整理台，编辑成果立刻可见。
            if one("confirmed") in ("1", "yes", "true"):
                nm = one("q").strip()
                d = confirmed.detail_payload(nm) if nm else None
                if not d:
                    # 给 200 + found=false：浏览器不会因为 404 报一堆控制台错误，
                    # 前端照样把「还没确认」的原因摆出来
                    st = confirmed.stats()
                    return self.json({
                        "found": False, "confirmedOnly": True, "name": nm,
                        "confirmedTotal": st["confirmed"],
                        "reason": "「%s」还没在线路整理台完成全部确认（线路来源 ✓ + "
                                  "站点来源 ✓ + 停站 + 线路走向）。目前确认了 %d 条：%s"
                                  % (nm or "（空）", st["confirmed"],
                                     "、".join(st["lines"][:8]) or "暂无"),
                    })
                return self.json(d)
            asked = kw = one("q")
            frm = one("from")
            exact = one("exact") in ("1", "yes", "true")
            # 同一条线的几个来源版本（备份导回来的「京沪线（底表）/（OSM）/（自建）」）：
            # 搜**基础名**时系统按「用户 ★ 勾的默认 > 底表 > 站数最多」给一份，别把
            # 某一份（比如用户自己导的 GeoJSON）当默认。明确要看某个版本时，名字带
            # 「（来源）」后缀，或带 exact=1（来源条上点的那一下）。
            autoFrom = ""
            if kw and not frm and not exact:
                try:
                    ge = linegroup.effective(kw)
                except Exception:            # noqa: BLE001
                    ge = {}
                if (ge.get("members") and ge["name"] != kw
                        and not linegroup.split(kw)[1]):
                    autoFrom = kw = ge["name"]
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
                try:
                    payload["group"] = linegroup.info(kw)
                except Exception:            # noqa: BLE001
                    payload["group"] = {}
                payload["askedName"] = asked
                payload["autoFrom"] = autoFrom
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
                    m = railgeom.meta(kw, composed.get("totalKm")) or geoimport.meta(kw)
                    if m:
                        m["kmChain"] = (railgeom.spine_km(m["name"], composed.get("totalKm"))
                                        or m.get("km"))
                    return out({**composed, "from": eff,
                                "info": cnrail.catalog_info(kw),
                                "osm": m, "osmOther": railcore.osm_extras(kw, 6)})
            # 基本线路（整理台定稿的成果）：全面接管，优先于五套来源。
            # 点名要看某个来源版本（名字带「（来源）」后缀）时 linebase.get 会
            # 自己拒绝，走下面的老路。
            if not eff:
                try:
                    bp = linebase.detail_payload(kw)
                except Exception:            # noqa: BLE001
                    bp = None
                if bp:
                    gname = bp.get("geomName") or ""
                    gsrc = bp.get("geomSrc") or ""
                    m = None
                    # ③ 合并保存整合出的真实走向（linedesk.db / ld_geom）是定稿线路的
                    # 权威几何；gsrc（route:*/geojson/...）只是记「① 用了哪家来源包」，
                    # 不能盖过它。优先用 merged_geom，缺几何才退回来源包 / 按线名找。
                    if bp.get("ldId"):
                        try:
                            m = linedesk.merged_geom(bp["ldId"], kw,
                                                     with_parts=False)
                        except Exception:          # noqa: BLE001
                            m = None
                    if not (m and m.get("ldGeom")):
                        if gsrc.startswith("route:"):
                            # 定稿时选了「某个来源包自己的走向」（OSM 走向 / rail-globe / cnrail）
                            m = linebase.route_geom(gname or kw, gsrc.split(":", 1)[1],
                                                    with_parts=False)
                        elif gsrc == "geojson":
                            m = geoimport.meta(gname or kw)
                        else:
                            m = ((railgeom.meta(gname, bp.get("totalKm"))
                                  if gname else None)
                                 or railgeom.meta(kw, bp.get("totalKm")))
                    if m and not m.get("ldGeom"):
                        # 来源包走向 / 按线名找的里程就是它自己的 total_km，
                        # 别拿 OSM 的复线并流再改一遍
                        m["kmChain"] = (railgeom.spine_km(m["name"], bp.get("totalKm"))
                                        or m.get("km"))
                    return out({**bp, "info": cnrail.catalog_info(kw),
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
            # 手工导入的 GeoJSON 线路先看它自己那份走向（用户导进来的就是权威），
            # 再退回 OSM 几何库
            m = geoimport.meta(d.get("name") or kw)
            if not m:
                m = railgeom.meta(d.get("name") or kw, d.get("totalKm"))
            if not m:
                # 库里按段命名、OSM 只有整条（成昆线元昆段 ← 成昆线），借来再按
                # 本段首末站裁掉两头，免得地图把整条线的走向画到半条线头上
                m = lineseg.borrow_geometry(d.get("name") or kw, d.get("stations"),
                                            d.get("totalKm"))
            if m:
                m["kmChain"] = (railgeom.spine_km(m["name"], d.get("totalKm"))
                                or m.get("km"))
            # 同名前缀下、线路库没收录的 OSM 线（「津霸线」之于「津霸客运专线」）
            return out({**d, "osm": m, "osmOther": railcore.osm_extras(kw, 6)})

        # OSM 线路几何（CRTrainRoute 整理）：真实走向 + 几何里程 + 桥隧统计
        if path == "/api/linemap/index":
            # 确认线路地图页左侧清单：全量、不截断、只认整理台全部确认的线路
            return self.json(linemap.index())

        if path == "/api/linemap/geom":
            # 批量几何：names 用 | 分隔（线路名里可能有逗号，别用逗号切）
            # 注意别把局部变量叫 q —— `one()` 闭包读的就是请求参数 q
            raw = one("names")
            nms = [x for x in raw.split("|") if x.strip()] if raw else []
            return self.json(linemap.geometry(
                nms, int(one("detail", "900") or 900),
                one("stops") in ("1", "yes", "true")))

        if path == "/api/line/osm":
            # 几何解析统一走 lineosm.geom()：走向编辑器也调它，两边永远是
            # 同一份 parts 多段（以前各写一套，编辑器画出的是失真主轴）。
            nm = one("q") or one("name")
            cf = one("confirmed") in ("1", "yes", "true")
            g = lineosm.geom(nm, confirmed_only=cf, expect_km=one("km") or None)
            if not g:
                return self.err("这条确认线路没有可用的走向折线" if cf
                                else "OSM 数据里没有这条线路: " + (nm or ""), 404)
            return self.json(g)

        if path == "/api/line/import-geojson/list":
            return self.json({"items": geoimport.list_imported()})

        if path == "/api/line/osm/list":
            # 线路库清空后（2026-11-02 用户要求「空库、全靠人工导 GeoJSON」），
            # 拿 OSM 三千多条线名当候选只会让人以为没清干净 —— 与 osm_extras 同一个开关。
            if not railcore.OSM_EXTRAS_ON:
                return self.json({"items": [], "stats": {}})
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
        if path == "/api/base/lines":
            _its = linebase.list_all()
            return self.json({"items": _its, "count": len(_its)})

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

        if path == "/api/ld/ref":
            d = linedesk.ref_get(one("name"), one("hid"))
            return self.json({"item": d})

        if path == "/api/ld/ref/search":
            return self.json({"items": linedesk.ref_search(
                one("q"), int(one("limit", "40") or 40))})

        if path == "/api/ld/ref/stats":
            return self.json(linedesk.ref_stats())

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
                one("force", "0") in ("1", "true"),
                one("future", "1") in ("1", "true"),
            ))

        if path == "/api/collect/chain/stop":
            return self.json(collector.chain_stop())

        if path == "/api/collect/status":
            return self.json(collector.status(one("date") or today()))

        if path == "/api/collect/start":
            date = one("date") or today()
            workers = int(one("workers", "6") or 6)
            do_enum = one("enum", "0") in ("1", "true")
            enum_future = one("future", "1") in ("1", "true")
            return self.json(collector.start(date, workers, do_enum, enum_future))

        if path == "/api/collect/stop":
            return self.json(collector.stop(one("date") or today()))

        # 「不开行」只是当时没查到经停站：手动放行，下一轮下载重新核实
        if path == "/api/collect/skip/stats":
            return self.json(collector.skip_stale_stats(one("date") or today()))

        if path == "/api/collect/recheck":
            return self.json(collector.recheck_skips(one("date") or "all"))

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
            st = curate.status()
            st["renum"] = curate.renum_state()
            return self.json(st)

        if path == "/api/curate/renum":
            return self.json(curate.renum_state())

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

        if path == "/api/curate/lineMap":
            # 数据校对页右侧「线路走向 / 停站」图的数据源。
            # 口径和校对下拉一致：只认整理台确认线路，没确认的给 200 + found=false
            # （别报 404，见 PROJECT-CONTEXT 规则 7）。
            nm = (one("name") or one("q") or "").strip()
            if not nm or nm not in confvia.names():
                return self.json({"found": False, "name": nm, "parts": [],
                                  "stations": [], "scope": "confirmed",
                                  "reason": "不是整理台确认线路"})
            try:
                g = lineosm.geom(nm, confirmed_only=True) or {}
            except Exception:                  # noqa: BLE001
                g = {}
            raw = [p for p in (g.get("parts") or [])
                   if isinstance(p, (list, tuple)) and len(p) > 1]
            total = sum(len(p) for p in raw)
            # 长线（京广高铁 4000+ 点）抽到 2000 点以内，每段首尾都保留，
            # 只是给示意图省流量，不改库、不影响主站和编辑器那份全量几何。
            step = (total + 1999) // 2000 if total > 2000 else 1
            parts = []
            for p in raw:
                a = [[round(float(q[0]), 6), round(float(q[1]), 6)]
                     for q in p[::step]]
                tail = [round(float(p[-1][0]), 6), round(float(p[-1][1]), 6)]
                if a and a[-1] != tail:
                    a.append(tail)
                parts.append(a)
            try:
                d = confirmed.detail_payload(nm) or {}
            except Exception:                  # noqa: BLE001
                d = {}
            stations = [{"name": s.get("name"), "lat": s.get("lat"),
                         "lon": s.get("lon"), "km": s.get("km")}
                        for s in (d.get("stations") or [])
                        if s.get("lat") is not None and s.get("lon") is not None]
            ent = confvia.index().get(nm) or {}
            return self.json({"found": True, "name": nm, "parts": parts,
                              "stations": stations, "nStop": len(stations),
                              "nPart": len(parts), "nPt": sum(len(x) for x in parts),
                              "totalKm": ent.get("totalKm"),
                              "geomSrc": g.get("source") or "",
                              "scope": "confirmed"})

        if path == "/api/curate/cands":
            a, b = one("from"), one("to")
            # 只给整理台确认的线路（数据校对的口径，见 confvia.py）
            items = confvia.cands(a, b, limit=8) if (a and b) else []
            return self.json({"from": a, "to": b, "items": items, "count": len(items)})

        # ---- 用户新增的线路 / 车站 ----
        if path == "/api/custom":
            return self.json({"lines": railcore.list_custom_lines(),
                              "stations": railcore.list_custom_stations()})

        if path == "/api/stations/names":
            names = railcore.stations.all_names()
            return self.json({"items": names, "count": len(names)})

        if path == "/api/curate/lineNames":
            # 数据校对只认整理台「全部确认」的线路（2026-11-11 用户定）：
            # 底表 / 资料库 / china-emu / OSM / 自建 的来源版本（京广线（OSM）、
            # 沪昆线（底表）…）以及还没确认的线，一律不进这个下拉，也不参与自动
            # 识别（见 confvia.py）。整理台确认一条就多一条，实时跟着变。
            items = []
            try:
                for b in confirmed.recent():
                    items.append({
                        "name": b["name"], "n": b.get("nStop"),
                        "totalKm": b.get("totalKm"),
                        "kmComplete": True, "kmManual": False,
                        "src": "confirmed", "inLib": False,
                        "base": True, "confirmed": True,
                        "pickName": b["name"],
                        "geomSrc": b.get("geomSrc") or "",
                        "fromSta": b.get("fromSta") or "",
                        "toSta": b.get("toSta") or "",
                    })
            except Exception:                # noqa: BLE001
                items = []
            items.sort(key=lambda x: (-(x.get("n") or 0), x["name"]))
            # 线路和车站会持续增加：这里**不下发全库「站→线」索引**（将来会很大）。
            # 支持两个按需过滤，前端只取当前车次用得到的那一小份：
            #   sta= 单个发车站        → 手工填写经由时单查兜底
            #   q=   线名关键字 + limit → 地图里搜线路，只回前若干条
            # 整理台确认一条新线，confvia.index() 的 2 秒缓存一过就自动出现，不用改名单。
            sta = (one("sta") or "").strip()
            if sta:
                hit = set(confvia.lines_for([sta]).get(sta) or [])
                items = [x for x in items if x["name"] in hit]
            # 别把局部变量叫 q：one() 是闭包，读的就是本函数的请求参数 q
            kw = (one("q") or "").strip()
            if kw:
                items = [x for x in items if kw in x["name"]]
            try:
                lim = int(one("limit") or 0)
            except ValueError:
                lim = 0
            if lim > 0:
                items = items[:max(1, min(lim, 200))]
            return self.json({"items": items, "count": len(items),
                              "names": [x["name"] for x in items],
                              "scope": "confirmed", "sta": sta, "q": kw})

        if path == "/api/curate/linesForStops":
            # 一次性预取「本趟车这些站」各自的候选线路。规模随车次站数走，
            # 不随线路库 / 车站库的增长而膨胀；新确认的线路自动进候选。
            raw = one("stas") or ""
            stas = [s.strip() for s in raw.replace(",", "|").split("|")
                    if s.strip()][:500]
            return self.json({"staMap": confvia.lines_for(stas),
                              "count": len(stas), "scope": "confirmed"})

        if path == "/api/curate/scan":
            return self.json(curate.start())

        if path == "/api/curate/stop":
            return self.json(curate.stop())

        if path == "/api/override":
            return self.json({"code": one("code").upper(),
                              "override": railcore.get_override(one("code"))})

        # ---- 列车位置地图（按时刻表推算，非 GPS） ----
        if path == "/api/trainmap/snapshot":
            bb = one("bbox")
            return self.json(trainmap.snapshot(
                t=one("t"), q=one("q"), types=one("types"),
                status=one("status", "in") or "in",
                station=one("station"), fro=one("from"), to=one("to"),
                bbox=bb.split(",") if bb else None,
                limit=max(1, min(20000, int(one("limit", "4000") or 4000))),
            ))

        if path == "/api/trainmap/train":
            return self.json(trainmap.train_detail(
                one("code"), one("date"), one("t") or None))

        if path == "/api/trainmap/track":
            return self.json(traintrack.track(
                one("code"), one("date"), one("t") or None))

        if path == "/api/trainmap/backbone":
            return self.json(trainmap.backbone())

        if path == "/api/trainmap/stats":
            return self.json(dict(trainmap.stats(), track=traintrack.stats()))

        if path == "/api/trainmap/rebuild":
            trainmap.invalidate()
            traintrack.invalidate()
            trainmap.build(force=True)
            return self.json({"ok": True, **trainmap.stats()})

        # ---- OSM 走向编辑器（独立页面 /osmedit.html）----
        if path == "/api/osmedit/lines":
            try:
                lim = int(one("limit", "60") or 60)
                off = int(one("offset", "0") or 0)
            except ValueError:
                lim, off = 60, 0
            return self.json(osmedit.list_lines(one("q"), lim, off))

        if path == "/api/osmedit/stations":
            return self.json(osmedit.search_stations(one("q"),
                                                     int(one("limit", "12") or 12)))

        if path == "/api/osmedit/geom":
            nm = one("name")
            if not nm:
                return self.err("缺少线路名 name")
            try:
                n = int(one("n", "200") or 200)
            except ValueError:
                n = 200
            g = osmedit.load_geom(nm, n)
            if not g:
                return self.err("找不到这条线路的几何（OSM / 线路库都没有）: " + nm, 404)
            return self.json(g)

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

    # 后台预热：物化车次全集 + 统计 + 全路网位置索引 + 真实走向索引
    #（构建约 9 秒，别让第一次点地图页 / 点第一趟车等）
    def _warm() -> None:
        try:
            collector.avail_dates()
            collector.history_dates()
            trainmap.build()
            traintrack.lines()
            traintrack.line_stops()
            # 车站多坐标候选表：表已建好时只是查个数（毫秒级）；全新库要读
            # 15 万行源数据建表（约 7s），放后台预热，别卡第一次点车站详情
            railcore.stationcoords.ensure()
        except Exception:                            # noqa: BLE001
            traceback.print_exc()
    threading.Thread(target=_warm, daemon=True).start()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
        srv.shutdown()


if __name__ == "__main__":
    main()
