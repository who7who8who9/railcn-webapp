# railcn 项目上下文（压缩版）

> 给接手的自己 / 新会话看的一份「工作底稿」。功能细节看 `README.md` 与 `webapp/README.md`，
> 这里只留：**怎么跑、数据长什么样、哪些规则不能改、踩过什么坑、还剩什么没做**。

---

## 1. 这是什么

本地铁路查询网页：查车次 / 经停站 / 余票、按日期批量采集、看历史数据、地图上画走向，
外加一个「数据校对」页把线路 / 站点 / 经由 / 里程 / 坐标的人工修正沉淀成正式库，
和一个独立的「线路整理台」（`/linedesk.html`）把五套来源的线名归并成人能看懂的规范线路。

- 纯标准库后端（`ThreadingHTTPServer` + SQLite），前端单页（`index.html` + `app.js` + `style.css`），无框架无构建。
- 数据源：12306（车次 / 经停 / 余票）、`cnrail_data_v2.xlsx`（车站 / 线路 / 站序底表）、
  辉睿线路资料库（844 条线路参数与逐站里程）、china-emu（660 条线路的逐站 K 里程）、OSM（线路几何，可选）。
- 本地库：`webapp/data/rail.db`（约 160 MB，不入库）；
  整理库 `webapp/data/linedesk.db`（约 2 MB，**入库**，是人工整理的成果）。

## 2. 怎么跑

```bash
# 服务（默认端口 8686）
C:/Users/Administrator/AppData/Local/Programs/Python/Python313/python.exe webapp/server.py --port 8686
# 或双击 webapp/start.bat（会自己修 PATH 再找 Python 3.9+）
```

- **改了 `.py` 必须重启服务**（常驻进程，没有热重载）；只改 `static/` 下的文件刷新页面即可。
- **U 盘同步已暂停**：推到 U 盘那一步先别做，等用户说「需要」再恢复（代码和库都别拷）。
- 服务日志走 stderr，一行一个请求；排查先看 `GET/POST -> 200` 有没有异常。
- 推送：`env -u HTTPS_PROXY -u HTTP_PROXY -u ALL_PROXY git -c http.proxy= -c https.proxy= push origin main`
  （本机代理会 502，必须剥掉）。
- 截图 / 调试产物一律进 `%TEMP%`，不要落进 `docs/`（`docs/` 只放 README 引用的图）。

## 3. 目录

```
railcn/
├─ data/            车站档案 7579、线路 417、坐标索引、huirui 线路资料库、chinaemu
├─ scripts/         数据导入（crtrain / huirui / chinaemu / 站名归一 / 去重）+ 19 个回归脚本
│                   （含 build_linedesk.py 建整理库、report_line_names.py 出清理报告）
├─ webapp/
│  ├─ server.py     HTTP 路由（约 700 行，所有接口在这一个文件里）
│  ├─ railcore.py   核心（3036 行）：SQLite 缓存、经由推断、里程、校正存取、同步
│  ├─ curate.py     校对页后端（655 行）：扫描、清单、详情、同起讫点、停站明细
│  ├─ cnrail.py     底表 JSON 查询：线路展开、里程、站序、经由推断 cnrail.infer_via
│  ├─ linedesk.py   线路整理台：独立库 linedesk.db、线名归并、站序与父子关系维护
│  ├─ collector.py  按日期批量下载（并发 / 断点 / 自适应限速 / 一键连跑）
│  ├─ railosm.py + railgeom.py  OSM 真实走向折线
│  ├─ historyq.py   历史数据查询（开行日历 / 覆盖 / 大类统计）
│  └─ static/       index.html / app.js(6100 行) / style.css
│                   linedesk.html / linedesk.js / linedesk.css（整理台，自带 SVG 地图）
└─ docs/            README 用截图 + 本文件 + line-names-report.md（清理报告）
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

### 另一个库：`webapp/data/linedesk.db`（整理库，入库）

线名整理是独立的一件事，另开库，**rail.db 一个字节都不碰**：

| 表 | 说明 |
|---|---|
| `ld_line` | 规范线路：`name` 人工定的规范名、`family` 归一键（`前缀|族`）、`kind`、`status`(draft/confirmed)、`total_km`、起讫、`src_main` |
| `ld_alias` | 每个来源的原始写法，`UNIQUE(source,name)`——一个来源线名只能归一条线 |
| `ld_stop` | 整理后的站序：站名 / 里程 / 经纬度 / 这站取自哪个来源 |
| `ld_relation` | 分段 ↔ 全线的父子（`kind='seg'`），带成环检查 |
| `ld_log` | 改动流水 |

初稿由 `scripts/build_linedesk.py` 从五套来源机器归并（3445 条线 / 4841 个写法 / 61 条父子），
`status='draft'`，在 `/linedesk.html` 里人工确认。重建是幂等的：只补新出现的线名，
已整理过的不动，`--force` 才清空。

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

16. **第五套来源：china-emu（`line_ce` / `line_ce_stop`），补里程 + 补站序兜底。**
    `scripts/fetch_chinaemu.py` 抓 `china-emu.cn/RailRoads/`（骨干 133 / 区域 116 / 市域 62 /
    枢纽联络线 385，去重 660 条），`import_chinaemu.py` 入库。它是**独立表，不并进 line_catalog**
    —— 现有四套已经靠线名字符串硬绑，再塞一套只会更乱。
    - **它的独特价值是逐站计价里程**：资料库 10377 行站序只有 5420 行有里程，277 条线路里程全空
      （侯西线 42 站一个都没有）。china-emu 每站都有到起点的里程。
    - **里程是 K 里程（全线累计），不是段内里程**：陇海铁路西宝段是 K1073→K1256，段长 183 km。
      `total_km` 是 `km_max - km_min`，用末站值会算成 1256。
    - **桥隧行不能取里程**：`/RailRoads/Construction/` 那些行（丹昆特大桥、大胜关长江大桥）
      写的是桥隧**自身长度（米）**，163763 是 164 km 的桥，不是里程。入库时 `kind='work'`，
      `railcore.ce_stops()` 会滤掉。
    - **站名带 `[上行]` / `[车场]` 后缀**，上下行分列成两行，`_ce_name()` 负责清洗。
    - **抓取要防空壳页**：约 17 条第一次会返回 5 KB「只有标题没有站序」的 200 页
      （池黄高速铁路、黎湛铁路），重抓一次就正常。`_looks_empty()` 判空并重试 3 次。
    - **站序选择用 OSM 走向长度当裁判**：两个来源都有时，取总长更接近 OSM 几何长度的那个。
      兰新线 OSM 2362 km，资料库给它「兰中城际线」49 km（错配），china-emu 给「兰新铁路」2423 km → 选后者；
      沪昆高速线 OSM 2054 km，资料库给「沪杭高铁」158 km（错配），china-emu 给 2252 km → 选后者。
      差距 <15% 时仍按老规矩用资料库，不无事生非。
    - **补里程用 `scripts/fill_catalog_km.py`**：只填 `km IS NULL`，两边共有站算中位数偏移再平移，
      填完必须单调不减，否则整条跳过。已补 55 条线路 400 站，里程全空的从 277 降到 233。
    - 回归 `scripts/test_chinaemu.py`（守 7 件事），对账 `scripts/report_chinaemu.py`。
    - **遗留**：资料库还有 39 条「小资料挂大干线」的错配（马坊支线→京哈线、库俄线→南疆线、
      南昌西环线→京九线…），站序包含关系能判，但需要再跑一轮修正。

17. **线名整理走独立库，别往 rail.db 里塞。** 五套来源的线名各写各的（底表分段命名
    `京哈高速线京沈段`、辉睿 `京沪高铁`、china-emu `京沪高速铁路`、OSM `京沪高速线`），
    整理结果落在 `webapp/data/linedesk.db`，`rail.db` 一个字节都不改。
    - **归并只做名字层面确定的事**：后缀族归一（`高铁/高速线/高速铁路/客运专线/客专` → 高速族；
      `城际/城际线/城际铁路` → 城际族；`线/铁路` → 普速族），`X线Y段` 拆成「父线 + 段名」，
      同一段的两种写法按「父线族 + 段名」再并一次（否则京哈沈哈段会挂两遍）。
    - **OSM 的桥隧名不算写法**：`图佳线（第一五林河桥）` 这种有 150 个，是桥隧要素不是线名，
      `is_work_name()` 滤掉。不加的话「写法数」全是水（图佳线能显示 24 种写法）。
    - **分段名的父线没人命名时补一条空壳**（`src_main='inferred'`），站序留空待人工补。
    - **初稿是机器归并的，`status='draft'`，必须人工确认**；重建幂等，只补新线名，
      `--force` 才清空（会连人工结果一起清）。
    - **合并时被并掉那条的规范名也要存成别名**，否则搜旧名再也找不到它；
      转移别名要先把整批读出来再改（边查边改同一张表会漏行）。
    - **不做自动回灌**：整理结果先导出（`/api/ld/export`）人工看，确认真对了再谈改 rail.db。

## 6. 前端约定
- 5 个页签：`车站/线路`、`站站查询`、`数据下载`、`历史数据`、`数据校对`；状态都在全局 `CU` 对象里。
- 校对页筛选：`todo / via0 / via / km / coord / pending / manual / synced / clean / all`。
- **类名别撞车**（`.od-form .od-row` 是查询表单 flex；`.mate-*` 是同起讫点六列 grid；
  `.suggest > .si` 是联想条目；`.cb-item` 是经由 / 线路下拉项，`.cb-item.node` 是线路所这类
  非客运节点；`.list:empty{display:none}`）——详见 `webapp/README.md` 的同名章节。
- `cuLoad()` 带请求序号 `CU.loadSeq`，连点筛选 / 搜索时旧响应整包丢弃（否则列表会被旧结果盖住）。
- **地图底图两套并存**：主站车次详情用腾讯 GL JS（`localStorage.railcn.mapkey`，WGS-84 按 GCJ-02
  纠偏后画）；整理台 `linedesk.html` 左下角可切腾讯 / OSM / 示意，Key 与主站共用。
- OSM 走 Leaflet，**库文件随仓库带**（`static/vendor/leaflet/`），不引 CDN——境内 jsDelivr / unpkg
  全超时，官方 `tile.openstreetmap.org` 也连不通；瓦片用 `tile.openstreetmap.de`（有中文地名），
  失败 4 次自动换 `tile.openstreetmap.fr`；CARTO 现在返回「API KEY REQUIRED」，别再用。
- 覆盖层（底图切换条 / 缩放 / 图例 / 信息卡）`z-index:900`：Leaflet 的 pane 是 400~800，
  不抬上去会被瓦片整个吃掉。

## 7. 回归脚本（20 个，改完必跑）

```bash
for f in test_chain test_line_km test_line_stations test_nm_stations test_osm_extras \
         test_station_loose test_tl_stops test_geo_toggle test_dock_speed test_code_alias \
         test_routestore test_nm_insert test_curate_refresh test_curate_sync test_net_form \
         test_via_nodes test_lineseg test_catalog_match test_chinaemu test_linedesk; do
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
| `test_chinaemu` | china-emu 表在且行数够、桥隧行没混进站序、match_key 两套命名同键、兰新线纠回 118 站（不是兰中城际 6 站）、资料库没有的线名能补出站序、杭深铁路补出 50+ 站里程、首末站里程口径对 |
| `test_linedesk` | 整理库独立（跑在临时文件上，rail.db 修改时间不变）、京沪四种写法归到一条、京哈沈哈段只挂一次、改名不破坏线族、同一来源线名不能被两条线抢、china-emu 的 K 里程搬过来变段内里程、合并转移别名/站序/旧名仍可搜、父子不能成环、导出结构完整 |
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

- **整理库怎么回灌 rail.db 还没定**（用户未拍板）：A 以 cnrail 417 + 自建 62 为骨架 + alias 映射表；
  B 直接以辉睿 844 为骨架；C 新建 canonical line 表。整理台现在只是把关系攒在 `linedesk.db`，
  导出（`/api/ld/export`）能拿到「规范名 ← 各来源写法 + 站序 + 父子」，回灌要等骨架定了再写。
- 整理库 3445 条线里 **1223 条有站序**，其余 2000 多条只有 OSM 线名（有走向没站序）；
  是否按 OSM 走向反查补站 —— 未定。
- 资料库那 **39 条「小资料挂大干线」** 的历史错配（马坊支线→京哈线、库俄线→南疆线…）仍在 rail.db 里，
  整理台能看到族内多条线，但要不要清空 `line_name` 让 compose 接管 —— 未确认。
- 武冈城际、武石城际的站序一样（`line_catalog_stop` 源头数据错），是否按资料改正 —— 未确认。
- 唐曹线站序从七道桥起、唐山站不在线上，所以 C248「唐山→南堡北」永远要人工填；是否把唐山接进站序 —— 未确认。
- override 是否改成按 `code + 站表版本` 指纹存（调图后自动失效）。
- 孤立坐标「枣强南」、10-30 之后的数据补全。
- `webapp/data/railways.geojson`（326 MB，已 gitignore）是否删除。
- `via_class` 表已废弃但仍在库里（2 行），可 `DROP TABLE`。

## 10. 近期提交

```
4b53d6c 线路整理台：单开一个库，把五套来源的线名归并成人能看懂的线路
890559f 接入 china-emu 作第五套线路来源：补 400 个站的里程，并纠回 39 条错配
09f3018 资料库挂错线：侯西线不再匹配到 9 km 的陇海西成联络线
c17f797 站序与 OSM 走向互补：分段线借整条线几何裁出本段，只有走向的线名从站序补站
2362505 站序退让：指定的那一天没抓到就退到最接近的一天，并标明不是当天
a9b1f41 换号车按 train_no 取站序：同起讫点不再显示「0 站 / 没有时刻」
df90cde 校对页：同步入库的也算已校正，只作已校正里的一个子集
4945fcf 车站/线路查询：输入框被同起讫点列表的 grid 挤成 24px，联想框退化成裸文字
```

远程：`https://github.com/who7who8who9/railcn-webapp`（branch `main`）。
