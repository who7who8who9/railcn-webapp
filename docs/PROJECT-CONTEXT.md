# railcn 项目上下文（压缩版）

> 给接手的自己 / 新会话看的一份「工作底稿」。功能细节看 `README.md` 与 `webapp/README.md`，
> 这里只留：**怎么跑、数据长什么样、哪些规则不能改、踩过什么坑、还剩什么没做**。

---

## 1. 这是什么

本地铁路查询网页：查车次 / 经停站 / 余票、按日期批量采集、看历史数据、地图上画走向，
外加一个「数据校对」页把线路 / 站点 / 经由 / 里程 / 坐标的人工修正沉淀成正式库。

- 纯标准库后端（`ThreadingHTTPServer` + SQLite），前端单页（`index.html` + `app.js` + `style.css`），无框架无构建。
- 数据源：12306（车次 / 经停 / 余票）、`cnrail_data_v2.xlsx`（车站 / 线路 / 站序底表）、
  辉睿线路资料库（844 条线路参数与逐站里程）、OSM（线路几何，可选）。
- 本地库：`webapp/data/rail.db`（约 160 MB，不入库）。

## 2. 怎么跑

```bash
# 服务（默认端口 8686）
C:/Users/Administrator/AppData/Local/Programs/Python/Python313/python.exe webapp/server.py --port 8686
# 或双击 webapp/start.bat（会自己修 PATH 再找 Python 3.9+）
```

- **改了 `.py` 必须重启服务**（常驻进程，没有热重载）；只改 `static/` 下的文件刷新页面即可。
- 服务日志走 stderr，一行一个请求；排查先看 `GET/POST -> 200` 有没有异常。
- 推送：`env -u HTTPS_PROXY -u HTTP_PROXY -u ALL_PROXY git -c http.proxy= -c https.proxy= push origin main`
  （本机代理会 502，必须剥掉）。
- 截图 / 调试产物一律进 `%TEMP%`，不要落进 `docs/`（`docs/` 只放 README 引用的图）。

## 3. 目录

```
railcn/
├─ data/            车站档案 7579、线路 417、坐标索引、huirui 线路资料库
├─ scripts/         数据导入（crtrain / huirui / 站名归一 / 去重）+ 17 个回归脚本
├─ webapp/
│  ├─ server.py     HTTP 路由（563 行，所有接口在这一个文件里）
│  ├─ railcore.py   核心（3036 行）：SQLite 缓存、经由推断、里程、校正存取、同步
│  ├─ curate.py     校对页后端（655 行）：扫描、清单、详情、同起讫点、停站明细
│  ├─ cnrail.py     底表 JSON 查询：线路展开、里程、站序、经由推断 cnrail.infer_via
│  ├─ collector.py  按日期批量下载（并发 / 断点 / 自适应限速 / 一键连跑）
│  ├─ railosm.py + railgeom.py  OSM 真实走向折线
│  ├─ historyq.py   历史数据查询（开行日历 / 覆盖 / 大类统计）
│  └─ static/       index.html / app.js(6057 行) / style.css(1101 行)
└─ docs/            README 用截图 + 本文件
```

## 4. 数据模型（`webapp/data/rail.db`）

| 表 | 行数量级 | 说明 |
|---|---|---|
| `trains` | 38 万 | 车次清单（车次号 + 日期 + 起终站 + 车型） |
| `route_tpl` / `route_day` | 1.3 万 / 25.8 万 | **经停站按趟只存一份 + 每天一条指针**；`routes` 是两者 join 的视图（省 5 倍空间） |
| `day_collect` / `day_skip` | 22 / 5 万 | 每天采集状态、已确认不开行的车次 |
| `line_catalog` / `line_catalog_stop` | 844 / 1 万 | 辉睿线路资料库 |
| `line_custom` / `station_custom` | 59 / 1878 | 用户自建线路 / 车站 |
| `line_override` | 5 | 档案线路的人工站序 / 人工里程覆盖（原始档案不动） |
| `coord_override` | 14 | 人工坐标（优先于档案） |
| **`train_override`** | 21 | **人工经由的正式库**，见下 |
| `curate` | 12722 | 校对清单（每趟车一行摘要，扫描生成） |
| `via_class` | 2 | **已废弃**（「同类沿用」已删），可删表 |

### `train_override`（最重要）

| 列 | 含义 |
|---|---|
| `code` | 车次号（主键，**校正和车次号绑定**，不按日期、不按起讫站组） |
| `via_json` | 分段经由 `[{line, from, to}, …]` |
| `total_km` / `seg_km_json` | 人工总里程 / 站间里程（`{"0":205}` 键是区间序号） |
| `source` | `manual` 逐趟人工校正 / `sync` 从同起讫点车次同步来的 |
| `src_code` | 同步自哪趟车 |
| `based_date` | 这次校正依据哪一天的站表（站表变了要提示重校） |

`curate.via_origin` = `manual` / `sync`，用来在清单里区分来源；`via_source` 是 `manual` / `auto`。

## 5. 业务规则（改代码前先看，这几条是被反复确认过的）

1. **经由只有两种：正式库里的（`train_override`）和当场算出来的。**
   `build_route_payload()` 每次按线路库现算的叫「自动推断」，`viaSource='auto'`、`viaConfirmed=false`，
   **不算正式数据**，要人工确认（编辑页「确认自动结果并入库」）才写进 `train_override`。
2. **「同类沿用」已经彻底删掉。** 曾经有 `via_class` 表按「同起讫站 + 同车种」自动套用，
   后来证明会把别的车次的经由悄悄套上去，已整块删除（表、函数、样式、`test_via_class.py`）。
   现在的替代是：列出**同起讫点车次 + 每趟停站详情**，人工勾选，保存时逐趟同步。
3. **同步入库也算「已校正」。** 统计条是「已校正 N / 其中同步 M」，M 是 N 的子集，不相加；
   `filter=manual` 含同步的，`filter=synced` 只是单独回看。行标统一写「已校正 · 同步」。
4. **同步先看「停站是否全在经由里」，全在就原样照抄。** `via_covers_stops()` 拿每条经由段在那条线的
   站序（`cnrail.line_station_names` → `merge_catalog_nodes`，查不到再 `catalog_stop_names`）取
   `from→to` 之间的一截，合起来就是这条经由走过的全部站：
   - 对方的每一站都在里面 → `mode=direct`，区间端点、转线的所（春申南所/灭火沟线路所）**一个不动**，
     不重新落站、不重算。列表里挂青标「停站全在经由内」（`odMates.items[].viaFits`）。
   - 停了经由之外的站 → `mode=adapt`，`adapt_via_to_stops()` 按「第 i 站 / (n-1)」比例落站，
     中间没停的站并进相邻段。
   - 线路站序查不到 → 返回 `None`，退回 `adapt`（保守）。
   只同步**覆盖全程**的经由。
5. **已校正/已同步的车次不再进候选。** `od_mates()` 直接跳过 `train_override` 里有 `via` 的车次
   （人工的、同步的同样对待），只把趟数记在 `odMates.hidden` 里，界面标题说一句
   「另有 N 趟已校正，不再列出」；同组全校正完就只剩这句话，没有勾选框。
   所以同步保存不必再问「要覆盖吗」，`syncOverwrite` 前端已不再发（接口仍支持，防止别处调用）。
6. **站表变了要提示。** 用 `based_date` 那天的站序和今天比，不等就在编辑器打「站表已变」，
   `overrideStale` 给出 `missing[]`（少了哪些站）/ `added[]`（多了哪些站）。
7. **里程三档**：`real`（实测）/ `hybrid`（部分实测）/ `estimate`（按直距估算），界面必须标明是哪档。
8. **车次合并**：同一趟车中途换号（4355/8）算一趟，`codes[]` + `codeLabel` + `codeSwitches`，每站带 `trainCode`。
9. **坐标**：底表经纬度是仿射变换后的假值（`data = s·true + t`），已用 OSM 反解回填；
   人工坐标写 `coord_override`，优先于档案。
10. **站名两种写法合并**（`xx` / `xx站`），缺档案的站用 12306 站表兜底。
11. **不要做全量路网 A\* 寻路**：项目只用「线路 + 车站」两类数据（提交 50c37ee 的边界）。
12. **经由选站要能选到线路所。** 线路所 / 乘降所 / 中继站不是客运站，但经由常常正是在
    这些地方转线（沪春线的春申南所、宝成线的灭火沟线路所），所以候选里照列、右侧标「线路所」；
    命名线路站序里缺的节点由 `merge_catalog_nodes()` 用资料库同名站序按邻居位置补进来，
    客运站顺序不动。`/api/line/stops` 对只存在资料库的线路（银西高铁）也返回站序。
13. **站序按 `train_no` 取，不按车次号。** `routes` 是 `route_day ⋈ route_tpl` 的视图，
    一天一页按 `train_no` 存；换号车（C344/5、C4326/7）整趟站序只落在**到达号**那一行，
    按出发号查 `code` 会一无所获 → 同起讫点列表显示「0 站、没有时刻」（曾影响 1044 趟）。
    `route_stops_brief()` 先用 `trains` 查 `train_no` 再取那一页，查不到才退回按 `code` 查。
    真的没抓过的（全库还剩 3 趟）行上标红「无停站数据」且勾选框禁用；
    本车自己没站序时同伴标「无法比对」（`odMates.items[].canCompare=false`），不显示「重合 0%」。

14. **站序与走向互补（`webapp/lineseg.py`），只补不合并。** 线名在两套数据里常对不齐：
    库内按段收、OSM 只有整条（成昆线成攀段 / 元昆段 ↔ OSM 成昆线），或 OSM 有整条、库内没这条线（成昆线）。
    - **借几何**：`borrow_geometry(段名, stations, totalKm)` 拿父线几何，按本段首末站裁出区间。
      13 条分段能借，11 条精确（元昆段 260→257.7、成攀段 734→725.4）。裁不出就**退整条并写 `why`**，
      不许硬裁（宁西线西合段几何断 7 截，硬连要横跨 294 km 空白 → 退整条）。
    - **补站序**：`stations_for(OSM 名)` 先查线路资料库同名（成昆线 133 站），再拿同前缀分段拼
      （京哈高速线 = 京沈段 + 沈哈段 37 站）。拼接只认硬接缝（≤25 km），
      京港高速线商合段与赣深段隔 407 km → 拒绝并说明。
    - **裁剪算法不用 `railgeom._chain`**：它按「接完会不会离起点更近」判复线，长干线会把该接的段全丢掉
      （成昆线只串出 313 km）。正确做法：复线去重（首尾都对上、容差 0.35 km）→ **按连通分量做最小生成树缝合**
      （不能「每端点就近连几个」，小分量永远连不出去）→ Dijkstra 走最短路，断口按直线补并记数，
      缝合半径 8→25→60→200 km 逐级试，里程差 > 25% 判借错线退整条。
    - **不写库**：几何是站序的派生物，只在查询时补，进程内缓存。前端取几何要用 `osm.ask`（本段名），
      用 `osm.name` 会拿到整条。首末站离几何 > 30 km 视为站名坐标指错，不裁（龙烟线龙泉北偏 782 km）。

15. **资料库 ↔ 线路库的对应只认线名，站序重叠只用来复核，不用来新增。**
    `line_catalog.line_name` 决定「这条资料挂给库内哪条线」。打分曾经写成
    `len(交集) / min(资料库站数, 库内线站数)`——分母取小值，**库内的小联络线只要几站落在
    资料库站序里就必然 1.0**：「侯西线」42 站 288.5 km 被挂到了 9 km 的「陇海西成联络线」，
    搜侯西线出来的是三桥→西安西两站。正确做法：
    - 分母用资料库站数（`len(交集)/len(资料库)`），覆盖率 ≥0.6 才算
    - 量级只卡下界、且要**站数与里程同时**不到一半才判死——只小一头的是正常分段关系
      （京石高铁 7 站 283.7 km 之于京广高速线 42 站 2291 km，京石就是它的一段），加上界会误杀半张表
    - **不做站序覆盖率的新增匹配**：cover 0.5~0.7 那一档全是噪音（广佛环线→南广线、
      汉宜高铁→襄荆高铁）。宁可 line_name 留空——`/api/line` 会走 `compose_line`，
      用资料库站序 + OSM 走向拼出一条完整线路，比挂错线准
    - 清空后资料卡会丢（`line_detail` 只在库内认得出时才挂 `info`），所以 compose 分支要补
      `cnrail.catalog_info(kw)`；它得用 `catalog_get` 按资料名查——`_CATALOG` 的键是 **line_name**，
      资料库自己的线名查不到（这也是「搜陇海西成联络线冒出侯西线资料卡」的原因）
    - 一次性修正用 `scripts/refix_catalog_match.py`（默认 dry-run，`--apply` 先备份库）

## 6. 前端约定

- 5 个页签：`车站/线路`、`站站查询`、`数据下载`、`历史数据`、`数据校对`；状态都在全局 `CU` 对象里。
- 校对页筛选：`todo / via0 / via / km / coord / pending / manual / synced / clean / all`。
- **类名别撞车**（`.od-form .od-row` 是查询表单 flex；`.mate-*` 是同起讫点六列 grid；
  `.suggest > .si` 是联想条目；`.cb-item` 是经由 / 线路下拉项，`.cb-item.node` 是线路所这类
  非客运节点；`.list:empty{display:none}`）——详见 `webapp/README.md` 的同名章节。
- `cuLoad()` 带请求序号 `CU.loadSeq`，连点筛选 / 搜索时旧响应整包丢弃（否则列表会被旧结果盖住）。

## 7. 回归脚本（18 个，改完必跑）

```bash
for f in test_chain test_line_km test_line_stations test_nm_stations test_osm_extras \
         test_station_loose test_tl_stops test_geo_toggle test_dock_speed test_code_alias \
         test_routestore test_nm_insert test_curate_refresh test_curate_sync test_net_form \
         test_via_nodes test_lineseg test_catalog_match; do
  "C:/Users/Administrator/AppData/Local/Programs/Python/Python313/python.exe" scripts/$f.py
done
```

| 脚本 | 守什么 |
|---|---|
| `test_curate_sync` | 自动结果要确认才入库、同步「原样照抄 / 重新落站」两条路、站表变化、UI 勾选与同步标记、跑完自动撤销 |
| `test_curate_refresh` | 线路库改动后重算这趟车的自动经由 |
| `test_net_form` | 车站/线路查询输入框宽度、联想框宽度与行高、Esc/点空白收起、空列表不占高度 |
| `test_via_nodes` | 经由选站能选到线路所（沪春线春申南所、宝成线灭火沟线路所）、节点插入位置、只在资料库的线路也出站序 |
| `test_lineseg` | 分段线借整条线的几何并按首末站裁剪（元昆段 257.7 / 成攀段 725.4）、裁不出如实退整条（宁西线断 7 截）、只有 OSM 的线名补站序（成昆 133 站 / 京哈 37 站）、京港两段隔 407 km 拒绝拼、UI 标「借…裁出本段」|
| `test_catalog_match` | 资料库不会挂到小联络线上（侯西线 42 站不能匹配陇海西成联络线 2 站）、分段关系不被误杀（京石高铁 → 京广高速线）、搜侯西线拿到 42 站 288.5 km 且 OSM 走向在 |
| `test_chain` | 车次合并（中途换号）与检索 |
| `test_line_km` / `test_line_stations` | 线路里程双模式、站序编辑 |
| `test_nm_stations` / `test_station_loose` | 新增线路站名并集、站名带「站」也能搜 |
| `test_tl_stops` | 时间条停站与时刻 |
| `test_geo_toggle` | 地图「站点直连 ⇄ 沿真实线路」 |
| `test_dock_speed` | 地图事件层不抢 dock 命中、倍速可用 |
| `test_routestore` | 经停站按趟去重（route_tpl / route_day） |
| `test_code_alias` / `test_nm_insert` / `test_osm_extras` | 车次号别名、新增站、OSM 未收录线路 |

**跑测试的注意**：脚本会写真实库再撤销，**不要并发跑同一个脚本**（会互相踩，留下孤儿 override）。

## 8. 踩过的坑（省时间用）

- 服务是常驻进程：改 `.py` 必须 `taskkill` 后台重发，否则看起来「改了没生效」。
- `git push` 必须剥代理，否则 502。
- 底表坐标是假值；直接用来画图北京南会落在山西。
- 线路里程表大量缺站（京沪 150 站只有 58 个里程值）→ 里程必须分档，不能一律当实测。
- SQLite 写锁争用：开 WAL + 批写 + 后台扫描「先算后写、短事务」，否则保存会等到超时。
- 经停站每天整份存会膨胀 5 倍 → 内容按趟存一份（`route_tpl`）+ 每天指针（`route_day`）。
- 前端类名撞车（`.od-row` 被同起讫点列表占用）会把输入框压成 24px；联想下拉写错类名会退化成裸文字。
- `route_stops_brief()` 只给站名不给里程，落站时按**站序比例**算，别按里程算（会得到空结果）。
- 测试并发会留下 `train_override` 孤儿记录，排查数据问题时先看 `source IS NOT NULL` 的近期记录。

## 9. 还没做 / 待定

- 武冈城际、武石城际的站序一样（`line_catalog_stop` 源头数据错），是否按资料改正 —— 未确认。
- 唐曹线站序从七道桥起、唐山站不在线上，所以 C248「唐山→南堡北」永远要人工填；是否把唐山接进站序 —— 未确认。
- override 是否改成按 `code + 站表版本` 指纹存（调图后自动失效）。
- 孤立坐标「枣强南」、10-30 之后的数据补全。
- `webapp/data/railways.geojson`（326 MB，已 gitignore）是否删除。
- `via_class` 表已废弃但仍在库里（2 行），可 `DROP TABLE`。

## 10. 近期提交

```
df90cde 校对页：同步入库的也算已校正，只作已校正里的一个子集
4945fcf 车站/线路查询：输入框被同起讫点列表的 grid 挤成 24px，联想框退化成裸文字
4f1086b 调试截图挪到临时目录，别再落进 docs/
04bf4b2 校对页：经由确认才入正式库，同起讫点车次改为勾选同步
dfc7aee 经停站按趟去重：每天核对内容，只存变了的那一版
6433809 车次合并写法：同一趟车中途换号显示成 4355/8
fd2b75a 线路编辑：能在两站之间插一站，档案线路也能改站序
d1bcc92 校对页：人工经由按「同起讫站 + 同车种」自动沿用到同类车次（后被 04bf4b2 反转）
```

远程：`https://github.com/who7who8who9/railcn-webapp`（branch `main`）。
