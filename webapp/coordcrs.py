# -*- coding: utf-8 -*-
"""车站候选坐标的坐标系换算（统一到 WGS84）。

项目里所有落库 / 地图用的坐标都是 WGS84（前端 `app.js` 的 GCJ() 只负责
显示时把 WGS84 转成腾讯地图的 GCJ-02）。但 `stations_all_sources.csv` 的
「坐标系备注」列记的是**原始仓库**的坐标系，CSV 里的数值却已被上游不同
程度地归一过，按标签盲换会造出假坐标：

  * railway-panorama(12306) 标 GCJ-02，实测 97% 已与 rail-globe(WGS84) 重合
  * TrainVis 标 BD-09，实测是 GCJ-02（做 GCJ→WGS 后中位偏差 584m → 80m）
  * gtfs / station-database 标「未标注」，实际基本就是 WGS84

所以 `calibrate()` 采用**数据驱动**：拿同名站的 WGS84 基准点，分别试
「原样 / GCJ→WGS / BD→WGS」，谁离基准最近就用谁；三个都离得很远说明
本来就是另一个地点（同名不同站），原样保留不换算。原始值与坐标系标签
也都留在候选表里，界面上如实显示。

算法与 `static/app.js` 里的 WGS84→GCJ-02 是同一套常量；反算用迭代逼近，
6 次迭代后误差 < 1e-7°（约 1 cm），对车站定位绰绰有余。
"""
from __future__ import annotations

import math

PI = 3.1415926535897932384626
A = 6378245.0
EE = 0.00669342162296594323
X_PI = PI * 3000.0 / 180.0


def _t_lat(x: float, y: float) -> float:
    ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * PI) + 20.0 * math.sin(2.0 * x * PI)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * PI) + 40.0 * math.sin(y / 3.0 * PI)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * PI) + 320.0 * math.sin(y * PI / 30.0)) * 2.0 / 3.0
    return ret


def _t_lng(x: float, y: float) -> float:
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * PI) + 20.0 * math.sin(2.0 * x * PI)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * PI) + 40.0 * math.sin(x / 3.0 * PI)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * PI) + 300.0 * math.sin(x / 30.0 * PI)) * 2.0 / 3.0
    return ret


def _out_of_china(lon: float, lat: float) -> bool:
    return not (73.66 < lon < 135.05 and 3.86 < lat < 53.55)


def wgs2gcj(lon: float, lat: float) -> tuple:
    """WGS84 → GCJ-02（显示层用；与前端 GCJ() 一致）"""
    if _out_of_china(lon, lat):
        return lon, lat
    dlat = _t_lat(lon - 105.0, lat - 35.0)
    dlng = _t_lng(lon - 105.0, lat - 35.0)
    rad = lat / 180.0 * PI
    magic = math.sin(rad)
    magic = 1 - EE * magic * magic
    sqrt_magic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((A * (1 - EE)) / (magic * sqrt_magic) * PI)
    dlng = (dlng * 180.0) / (A / sqrt_magic * math.cos(rad) * PI)
    return lon + dlng, lat + dlat


def gcj2wgs(lon: float, lat: float) -> tuple:
    """GCJ-02 → WGS84（迭代反算）"""
    if _out_of_china(lon, lat):
        return lon, lat
    wlon, wlat = lon, lat
    for _ in range(6):
        glon, glat = wgs2gcj(wlon, wlat)
        wlon -= glon - lon
        wlat -= glat - lat
    return wlon, wlat


def bd092gcj(lon: float, lat: float) -> tuple:
    """BD-09 → GCJ-02"""
    x, y = lon - 0.0065, lat - 0.006
    z = math.hypot(x, y) - 0.00002 * math.sin(y * X_PI)
    theta = math.atan2(y, x) - 0.000003 * math.cos(x * X_PI)
    return z * math.cos(theta), z * math.sin(theta)


def bd092wgs(lon: float, lat: float) -> tuple:
    """BD-09 → WGS84"""
    glon, glat = bd092gcj(lon, lat)
    return gcj2wgs(glon, glat)


# 六源 CSV 里「坐标系备注」→ 界面上显示的短标记（只描述原始仓库）
CRS_MAP = {
    "WGS84":                "WGS84",
    "GCJ-02(高德)":          "GCJ-02",
    "BD-09百度(见repo爬虫)": "BD-09",
    "未标注(疑GCJ-02)":      "未标注·疑GCJ-02",
    "未标注":                "未标注",
}


def _dist_m(lat1, lon1, lat2, lon2) -> float:
    return 111320.0 * math.hypot(lat2 - lat1,
                                 (lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2)))


def calibrate(lon: float, lat: float, crs_note: str, refs) -> tuple:
    """返回 (lon, lat, crs, converted)，把候选坐标校准到 WGS84。

    `refs` 是同名站里已确认 WGS84 的**全部**基准点 [(lon, lat), …]（档案 /
    rail-globe / OSM / 六源总表…）。同名不同站的地点也在里面，所以取「离哪个
    基准最近」而不是只认第一个。没有基准点时一律原样保留 —— 宁可标
    「未校准」，也不猜坐标系。三种变换都离所有基准 > 2 km 时同样原样保留。
    """
    label = CRS_MAP.get(str(crs_note or "WGS84"), "未标注")
    if not refs:
        return lon, lat, label, False
    g_lon, g_lat = gcj2wgs(lon, lat)
    b_lon, b_lat = bd092wgs(lon, lat)

    def nearest(la, lo):
        return min(_dist_m(la, lo, ra, ro) for ro, ra in refs)

    trials = [
        (nearest(lat, lon), lon, lat, label, False),
        (nearest(g_lat, g_lon), g_lon, g_lat, "GCJ-02→WGS84", True),
        (nearest(b_lat, b_lon), b_lon, b_lat, "BD-09→WGS84", True),
    ]
    best = min(trials, key=lambda t: t[0])
    raw_d = trials[0][0]
    if best[0] > 2000 or raw_d - best[0] < 30:
        return lon, lat, label, False
    return best[1], best[2], best[3], True
