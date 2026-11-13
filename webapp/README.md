# railcn webapp · 技术参考（压缩版）

> **2026-11-06 压缩**：原来 1856 行 / 153 KB，60 个按时间堆起来的功能专题合并成一份「当前口径索引」，
> 保留快速开始、数据来源、接口表、前端约定与已知限制。重写前的完整说明在
> [`docs/archive/webapp-README-20261106.md`](../docs/archive/webapp-README-20261106.md)
> （也在 `docs/archive/railcn-docs-20261106.zip` 里），要追溯某次改动的来龙去脉看它。
> 功能全貌看根 [`README.md`](../README.md)，业务规则与数据模型看
> [`docs/PROJECT-CONTEXT.md`](../docs/PROJECT-CONTEXT.md)。

## 模块与页面

| 文件 | 职责 |
|---|---|
| `server.py` | 全部 HTTP 路由（标准库 `ThreadingHTTPServer`），启动后台预热 |
| `railcore.py` / `cnrail.py` | SQLite 缓存、坐标、经由推断、里程、校正存取 / 底表 JSON 查询；**`cnrail.path_stations()` 的跨线经由链**（`_via_line()` 认「（来源）」后缀版本、`_via_sta_pos()` 给线路所端站按坐标就近落点，60 km 内）决定 `/api/route` 的 `path` / `lineStops` 能否展开出中间站，链断了走廊就会退化成几百公里直线、真实走向几何会被 `railosm` 裁掉 |
| `curate.py` / `collector.py` | 数据校对扫描 / 按日期批量下载（并发、断点、限速、连跑） |
| `confvia.py` | 校对线路口径的唯一来源：`confirmed.names()` ∩ `linedesk.ld_stop` 的确认站序索引（2 秒缓存），`infer()` / `cands()` / `normalize()` / `cover_of()`；**`station_index()` / `lines_for()` 给「按发车站筛线路」用，反查索引只留服务端、随 `_CACHE` 换版重建**，整理台新增线路 / 车站自动跟上，别把整份索引下发前端 |
| `historyq.py` / `routestore.py` | 历史查询 / 经停站按趟去重（`route_tpl` + `route_day`） |
| `linedesk.py` / `confirmed.py` | 整理台独立库（三步流程、站序反向与**指定位置插站**）/ 确认线路白名单 + `recent()` 全量倒序清单（只读 `linedesk.db`，确认时间取 `ld_log`） |
| `linebase.py` / `linegroup.py` | 基本线路发布与详情 / 来源版本归组 |
| `lineseg.py` / `railosm.py` / `railgeom.py` | 分段线与走向互补 / OSM 真实走向（按线名分组 + 本段站序二次裁剪 + 走廊拼接） / 几何按名索引 |
| `geoimport.py` / `backup_lines.py` | GeoJSON 图论建线 / 备份线路浏览与按需导回 |
| `trainmap.py` / `traintrack.py` | 列车位置内存索引 / 行车路线贴真实走向 |
| `lineosm.py` | **主站 `/api/line/osm` 与走向编辑器共用的几何入口**：`confirmed_only` 走 `confirmed.geom()`，其余依次基本线路 `ld_geom` → 来源包 `route_geom` → `geoimport` → `railgeom` → `lineseg.borrow_geometry`。两边解析同一份 `parts` 多段，杜绝「编辑器载入的线和主站画的不一样」 |
| `osmedit.py` | 走向编辑器后端：**全量走向索引**（只收有几何的线 + offset 分页 + 借走向别名）/ **整理台已确认线路专属通道**（`confirmed.recent()` 打标置顶、规范名载入 `lineosm.geom()`、按 `line_id` 写回/插入 `ld_geom`，绝不改 `ld_line`）/ **多段载入与保存**（`_as_segs` / `_thin_segs` / `_cap_jumps`，段间不连线）/ 沿线抓站（原始碎段网格索引）/ 建线（转 GeoJSON 走 `import_line`）|
| `stationcoords.py` / `coordcrs.py` | 车站逐源坐标候选表 `station_coord_cand`（17 套来源、60 m 聚类）/ GCJ-02·BD-09→WGS84 数据驱动校准 |
| `static/index.html` + `app.js` + `style.css` | 主站（4 个页签） |
| `static/linedesk.html` + `linedesk.js` | 线路整理台（自带可切底图的 SVG 地图）；站序每行 `+` 预选「插在它后面」，「+ 站」弹窗可选最前 / 第 N 站之后并一次填多个站名 |
| `static/trainmap.html` | 全路网列车位置地图 |
| `static/backup.html` | 备份线路面板 |
| `static/osmedit.html` + `osmedit.js` + `osmedit.css` | 走向编辑器（腾讯地图底图；没 Key 的离线自绘 Canvas 同样能加 / 拖 / 删点，站点参考点默认隐藏）；**整理台已确认线路线名标红 + 「已确认」标签 + 排最前**，确认态下提示「应用后仍是确认线路」、保存 `target` 用规范名；**按段绘制**：`S.breaks` 记各段起点，Canvas / `MultiPolyline` 都逐段画，插点 / 删点 / 撤销维护断点，保存发 `splitParts()`；**连接两点**：`S.links` 记手动两点直连段（端点引用 `pts` 下标），断点两侧改为删断点合并两段，「⇄ 连接两点」按钮 / Shift 点两红点 / Esc 退出；**↳ 接着末尾画**显式置 `S.drawing=true`（载入既有走向默认仍是插点） |
| `data/rail.db` / `data/linedesk.db` | 运行库（自动建）/ 整理库（入库） |
| `data/station_coords.json` | 车站坐标库（WGS-84） |

---

## 快速开始

```bat
:: 方式一：双击
start.bat

:: 方式二：命令行
cd webapp
python server.py --port 8686 --open
```

打开 <http://127.0.0.1:8686/>。

## 功能

| 模块 | 说明 |
|---|---|
| 车次详情 | 输入 `G1` 回车 → 经停站时刻表（到/发/停留/里程）+ 地图线路 |
| 位置推算 | 按「运行时长」把时刻表换算成绝对时间，在相邻两站间按时间比例插值定位 |
| 时间轴 | 自绘时间条（不依赖原生 range，Chrome/Edge 一致），可拖拽/点击/方向键定位；跨日车次在时间条上按自然日分段并标零点刻度；**条上标出全部停站的站名与时刻**（见下） |
| 地图 | 腾讯地图 GL JS（需自备 Key）；无 Key 时自动降级为无底图示意模式 |
| 车站 / 线路 | 查车站（路局 / 电报码 / 服务类型 / 连接线路）或线路（途经站点 + 累计里程），点线路名跳转；**线路模式只列整理台完成全部确认的线路**（见「确认线路白名单」一节），其它线路、来源版本与选线入口都不显示；确认线路地图按整理台合并的**真实走向**画，可一键切回站点直连 |
| 数据下载 | 列出**含当日共 15 天**的日期，选一天批量下载当天**全部车次 + 经停站**，可暂停/续传/导出 JSON·CSV |
| 历史数据 | 已落库的全部日期（含下载窗口之外的更早日期）都能翻，按车次/始发/终到检索、点开看经停站、直接导出 |
| 跨日显示 | 车次详情区分第 1/2/3 日：头部行程带 + 跨零点分隔行 + 时刻旁 `+1`/`+2` 角标；HUD 与时间条均带日期 |
| 数据校对 | 扫全库车次标出「经由认不全 / 里程只能估算 / 缺坐标」，按问题筛选逐条补全；**自动推断的经由只是参考，确认后才写进正式库**；同起讫点的其它车次会连同**停站详情**列出来，勾哪趟就同步给哪趟（各存一条自己的校正，见下文「同起讫点车次」）；**每趟车的所有经停站都能改经纬度**（不只缺失的），两格都有粘贴拆分：`经度:126.576636,纬度:42.038926`、`126.576636,42.038926`、`126.576636E 42.038926N`、`126°34'35.9"E` 都认，顺序随意；手工值优先于自动结果，随改随生效 |

## 数据是怎么来的

1. **车次清单**：`collect_trains_p2.py` 用前缀搜索枚举全国 1.6 万趟车 → Excel
2. **经停站**：`collect_routes.py` 按 `train_no` 逐趟拉 → Excel（也可网页内实时按需拉）
3. **导入本地库**：`python tools/import_data.py`
4. **车站坐标**：12306 不提供经纬度，用 OSM 铁路站点数据匹配站名
   ```bat
   python tools/build_coords.py          :: OSM 导出 → station_coords.json（93%）
   python tools/fill_missing_coords.py   :: 残留缺失用 Overpass 补（98.6%）
   ```

> 网页查询优先读 SQLite 缓存；缓存未命中才向 12306 实时请求 1 次并写回。
> 所以第一批车次慢 1~2 秒，之后是本地毫秒级。

## cnrail 数据层（车站 / 线路 / 里程）

`cnrail_data_v2.xlsx` 补齐了 12306 完全不给的三样东西：车站档案、线路站序、**真实里程**。

```bat
python tools\xlsx_to_json.py        :: Excel → data\*.json（仓库根目录下）
```

| JSON | 内容 |
|---|---|
| `cnrail_stations.json` | 7579 站：中英文名、类型、行政区、经纬度、所属路局、电报码、服务类型、连接线路 |
| `cnrail_lines.json` | 417 条线：途经站点（有序）+ 每站累计里程 + 线路色 + `kmReliable` |
| `cnrail_index.json` | `{站名: [lat, lon]}`，前端/脚本直接取坐标 |
| `cnrail_meta.json` | 生成时间、坐标修正模型、覆盖率统计 |

**两个坑，脚本里都处理了：**

1. **坐标整体偏移**：原表经纬度是 `data = s × true + t` 的仿射变换结果
   （s≈1.016/1.012，t≈−5.12/+1.11），直接用会把北京南画到山西。
   用本地 OSM 车站坐标做稳健回归反解，残差中位 0.0005°（约 50 m）。
2. **站间距列不一定对齐站序**：部分线路的「站间距」个数少于站点数，按位置解析会错位。
   用「里程 ÷ 相邻站大圆距离」即弯曲系数校验，只保留 0.95~1.5 的线路（231/417）参与里程推算。

**里程怎么用**：把一趟车的相邻两站拿去线路表里查同线里程差，命中就用实测值；
未命中的区间按本趟已命中区间的弯曲系数（默认 ×1.12）折算。
结果在页面上标 `实测里程` / `部分实测` / `里程估算`，鼠标悬停可看匹配了哪几段。
G1 北京南→上海虹桥 因此从「直线 1176 km」修正为 **1318 km**（实际运营里程）。

**路径展开**：相邻两站若同处一条线，就把该线中间的站插进折线，
所以地图上是沿线的真实走向，而不是站与站之间的直线。列车位置也按里程在这条折线上取点。

---

## 功能专题索引（原 60 节合并为「现在的口径」）

以下每节在旧版里都是长篇（当时的问题、修复过程、截图、逐条实测）。这里只留**现在该怎么用、哪条不能改**；
细节和历史原因见归档旧版。

### 数据下载 / 连跑 / 历史

- 含当日 15 天任选日期抓当天全部车次 + 经停站；断点续传、自适应限速、导出 JSON/CSV。
- **连跑第一件事 = 刷新全集**：`#dlEnumFirst` 默认勾（存 `localStorage.railcn.dlEnumFirst`），
  勾了发 `enum=1&force=1` → `chain_start(force_enum=True)` 绕过 7 天冷却；不勾 `enum=0`。
- **刷新全集要搜两个日期**（2026-11-11 修）：`#dlEnumFuture` 默认勾（存 `localStorage.railcn.dlEnumFuture`），
  `future=1` 时 `enum_dates()` 返回「连跑起始日 + 预售窗口最后一天」，两个日期各发一遍
  `enum_prefixes()`（12 字母 ×9 + 数字 9 = 117 词），结果**按搜索日期分别写 `trains`**。
  12306 按开行日展示车次：11-25 改号后的 V/N/Z/Y 只在 11-25 及以后查得到（实测 11-11 搜
  `V1`/`N1` 返回 0，`11-25` 返回 200/89 条），只搜今天会把它们全漏掉；写库日期若是今天，
  `curate` 的 `date >= renum_cutoff` 也筛不到。`future=0` 可只搜起始日（省一半请求、少挨 403），
  但**切换日的新车次会缺失**。`TRAIN_TYPES` 已含 V/N/X/B 且 `N` 不再代表纯数字；
  `_detect_type()` 收任意字母（只存首字母，别拿它推车种）。详见 `docs/renumber-2026.md`。
- **「当天不开行」不是永久结论**：`day_skip` 只是「最近一次查验没拿到经停站」，带 `expire` + `hits`
  复核期限；空结果要二次确认，一拿到经停站就删标记；`_release_stale_skips()` 按 **200 条/天/轮**放行到期标记，
  **别改成到期全放行**。历史页口径改「当时未查到（可能后来开放）」。
- **下载 / 历史页秒开**：`trains` 47 万行是 31 天 ×1.5 万趟每日登记，不是重复数据；慢在 `/api/dates`
  每天现算一次全集。现用 `train_universe` 物化 + `_bulk_basis()` 一次查完 + 忙闲缓存 + 启动预热
  （10.7s → 0.03s）。**别再 `GROUP BY trains` 或对 `routes` 全表 `json_array_length`**。
- 历史查询按车次 / 始发 / 终到检索；同一车次号 ≠ 同一趟车（调图），同一趟车可能有多个车次号（换向换号）。

### 经由推断 / 数据校对

- 自动推断三档证据（里程 → 线路站序 → 车站连接线路交集）+ 跨段连续性择优；**不做全量路网 A\***。
- **经由只有两种**：`train_override` 正式库里的，和当场按线路库算的（`viaSource='auto'`，不算数据）。
  自动结果必须人工「确认自动结果并入库」。
- **`via_class`「同类沿用」已彻底删除**，别恢复：改为列同起讫点车次 + 停站详情 → 人工勾选 → 保存时逐趟同步。
  同步入库也算「已校正」（统计「已校正 N / 其中同步 M」，M 是子集）；同步看停站是否全在经由里，
  全在就原样照抄，否则按对方停站重新落站。
- 校正和**车次号**绑定，`based_date` 记依据哪天站表，站表变了提示重校；已校正 / 已同步的车次不再进候选。
- 里程分 `real` / `hybrid` / `estimate` 三档并在界面标明；线路里程可**按段距**或**按累计**两种填法，
  分开存互不覆盖。填 `-` 保存时按「上一站累计 + 两站直线距离 × **0.85**」取整补值；
  真不详写「不详 / 未知 / ?」，`LN.naHard` 守住。行尾 `≈` 是 ×1.25 的参考值。
- **校对页的线路只用整理台确认的**（2026-11-11，`confvia.py`）：下拉、缺口候选、自动识别、`/api/line/stops?confirmed=1` 全是 `confirmed.names()` 里的线，来源版本（京广线（OSM）/（底表））和没确认的线一律不出现；站序读 `linedesk.ld_stop`，不是线路库同名版本。认不出的区间就当缺口等人工补（确认一条线多认一条），**别为了多认出几段放宽成线路库口径**。
- **手工填经由时按发车站筛线路**（2026-11-12，`docs/PROJECT-CONTEXT.md` 第 48 条）：线路下拉默认只列「含本段发车站」的确认线路（发车站 = 本段 `from` → 上段 `to` → 车次始发站），下拉顶部写明筛了几条，勾「显示全部线路」或点「显示全部」可放宽；一个都匹配不上会退回全部并提示。候选由 `/api/curate/linesForStops` 一次按本趟车站点预取，用户手打新站再按需单查 —— **线路和车站以后增加都会自动跟上**，别写死名单、别缓存整库索引。点开线路框要列出该发车站的全部候选，别被已填的线名缩成一条。
- **校对页右侧常驻「线路走向 / 停站」图，跟着中间段的选线实时刷新**：点任一线路框、下拉选中、顶部搜线名即切到那条线，画真实走向（多段）+ 该线全部停站，叠加本趟车停站（橙）与当前段起讫站（红圈）。选线是**实时的**：聚焦某段就切到该段线路，打字过程中（`cuMapSync` 防抖 300ms）跟着换，方向键 / 鼠标在候选上浏览先预览（140ms 防抖），Esc 不选就退回该段实际填的线；只改起讫站时只挪红圈、不抢走当前线路，**点段落行的空白处也算选中这一段**，增删段后地图仍停在中间选的那一段。**半截线名有前缀保护**：敲「石太」还没补「线」时不会被判成「不是确认线路」把地图清空。有腾讯地图 Key 时开第二个 `TMap.Map('cuTMap')` 带底图（WGS-84 过 `GCJ()`），没 Key 用 Canvas 自绘示意（经纬网格 / 比例尺 / 滚轮缩放 / 拖拽 / 悬停站名，**不画国界、不搬瓦片**）；标题栏「编辑器」跳 `/osmedit.html?line=XXX`。工具条上的「设置底图 Key / 腾讯底图 ✓」既是 Key 入口也是当前后端状态灯；三栏宽度随窗口自适应（校对页左侧名单 330px、右侧地图 500px@1600，<1080px 转上下布局），容器尺寸变化由 `ResizeObserver` 触发重绘。
- 经由选站要能选到线路所 / 乘降所 / 中继站（确认站序里自带，如京沪线的柳村线路所）。
- 每趟车所有经停站都能改经纬度；人工坐标优先于档案。`StationRegistry._coord_key()` 必须先翻 overrides
  再翻 coords（`杭州东` ↔ `杭州东站`），`linebase.detail_payload()` 要逐站用当前 `coord_info()` 覆盖定稿快照。

### 线路库 / 走向 / 来源

- **GeoJSON 导入 = 唯一建线方式**：端点合并建图 → 2 km 断口补直线边 → 车站投影切段 →
  PCA 主轴 + 局部插入 → 站间 Dijkstra 累里程。**别改回按文件点序连**（京沪多算 90 km）或 `chain_parts`。
  档案线路不许被覆盖，同名自建线要 `replace=1`。
- **导入以文件为准（`raw` 通道）**：站名一字不改、同名站照留、里程不归一。
  靠 `line_custom.source ∈ ('geojson','backup')` 识别，**五处都得放行**：
  `_build_custom_line(raw=)` / `save_custom_line(raw=,src=)` / `_apply_custom()` 按 source 重建 /
  `cnrail.line_detail()` 站名不过 `_key()` / `save_line_stations()` 跳过查重。丢一处就半途被归一。
- **线路库 2026-11-02 清空过又批量导回**，现在不是空的，再要「清空」必须先问；
  `scripts/reset_lines.py --restore <dir>` 可回滚。命名：唯一用原名 / 同名加「（来源）」/
  同来源多条加「（来源·N站）」/ 仍撞 `#2`；来源写 `note`。备份面板 `/backup.html` 全列出，
  **点了「导回来」才写库**。
- **来源版本归组**（`linegroup.py` + `line_default`）：只认「去掉（来源）后缀后同名」，
  生效版本 = **基本线路 > 默认 > 底表 > 站数最多**；索引靠 `COUNT(*)+MAX(updated_at) FROM line_custom`
  指纹缓存 2 秒，别改成手动 invalidate。联想下拉每份一行，别合成一行。
- **站序默认来源是用户勾的**（`line_src_pick`），点 ★ 才改全项目口径；不带 `from` 的取数跟随，
  指定 `from` 临时优先。`POST /api/line/stations/reverse` 按来源倒排：里程 = 原末站里程 − 原里程、
  新起点 0、缺的不插值。
- **站序与走向互补**（`lineseg.py`）只补不合并；资料库 ↔ 线路库的对应只认线名，站序重叠只用来复核。
  库外的线（只有资料库 / china-emu 有站序）改站序时自动收进线路库。
- **慧睿资料库中文名 = 线路正式名称**；别名匹配必须**整串相等**（按 `、，,;；/|` 切分），
  用 LIKE 会把「沪昆高速铁路」认成沪杭高铁。改名两步（`__tmp_<id>` → 正式名）避免撞唯一约束。
- **车站库合并**（`station_master`）：所有来源只进 `note`/`sources`/`aliases`，名称不带来源；
  站名取 12306/名册裸名。**全项目站名统一不带「站」**，但只剥「剥完的裸名本身是认得的站」，
  「十八站」「九站」这类真名保留；只并改名造成的相邻重复。
- **线路走向合并**（`scripts/merge_line_geoms.py`）：同名 ≥2 源合并，`note` 以「合并」开头，
  落 `line_route` / `line_geom` / `line_base` 临时接管；几何里程含复线重复、**不是营业里程**。

### 整理台 → 主站（当前口径）

- **三步流程**：基本信息（正式落库）→ ① 只定走向 → ② 只定站点 + 站序（可反向）→
  **③ 合并 ①② 并自动定为基本线路**；**④ 及其走向下拉已取消**。
  ① 不碰 `ld_stop` / `ld_geom` / 基本信息；② 不写 `ld_geom`；GeoJSON `only='stops'|'geom'` 两入口互斥。
  站序反向按倒排重排里程、缺的不插值、`stop_ok` 不变。
- ③ 的 publish 放在 `/api/ld/finalize` **HTTP 编排层**，别塞进 `linedesk.finalize()`
  （回归脚本用临时 `linedesk.db`，会往正式 `line_base` 写脏数据）。
- **基本线路 = 全项目接管**：存 `line_base` + `line_base_stop`，优先级最高；编辑里程三处必须都按 base 走
  （`save_line_override()` / `/api/line/km*` 回 `line_detail_eff()` / `detail_payload()` 无条件给 `kmBase`）。
- **主站「车站/线路」只认完成全部确认的线路**（`webapp/confirmed.py` 只读 `linedesk.db`）：
  `status='confirmed'` + `line_ok` + `stop_ok` + ≥2 站带定位 + 有走向。**只有带 `confirmed=1` 才走白名单**，
  校对 / 列车地图 / 备份面板 / 老接口不带 = 老口径，别改成全局过滤；未确认线给 `200 + found=false`，别报 404。
  前端 `netLoadConfirmed()` fire-and-forget（`boot()` 里别 await），确认视图隐藏来源条 / ★ / 改站序，
  **「编辑里程」按钮保留**。
  线路模式查询框**下方常驻 `#netConfirmedList`**：`netLoadConfirmed()` 带 `full=1` 拉全量
  `recent()`，`netRenderConfirmedList()` 按确认时间倒序渲染，`cfHit()` 在 `name` / `covers` /
  `geomName` 里**本地筛选**（不打接口、不弹联想下拉）。容器独立于 `#netList` / `#netDetail`，
  `showLine()` 清 `#netList` 不会抹掉它；切到线路模式清空输入直接列全量，窗口 focus 强制
  刷新让整理台刚确认的线立刻排最上。别改回「chip 列表 + 联想下拉」两套并列。
- **编辑里程专用版面**（`static/app.js` / `style.css`，第 38 条）：`lnWide()` /
  `lnRenderEditor()` 给 `#paneNet` 加 `km-editing`，CSS 把清单折成表头、
  `#lnStops` 隐藏，编辑区 `#lnKmBox` 紧跟 `#lnHead` 占住首屏（旧版被顶到
  `top=899`）；工具条 `.lnkm-bar` sticky、总里程行在 `#lnSegs` 上、说明
  `#lnKmHelp` 默认折叠（`LN.kmHelp`）、段距列表 `min(38vh,360px)`。
  **收起 / 取消 / 换线 / 切 tab 移除 class 即还原**，不动 `NET.cfOpen`，
  不改任何里程算法与 `/api/line/km*` 接口。回归 `test_km_editor_layout`。
- ③ 合并的走向在 `linedesk.db/ld_geom`，`linedesk.merged_geom()` 只读整理库解 spine/parts 供主站用，
  **不写 rail.db**。**点序一律 `[lon,lat]`**：`_save_geom()` 用 `_norm_chain()` 按整段多数点归一
  （`geoimport.assemble()` 给 `(lat,lon)`、`line_route.spine_gz` 给 `[lon,lat]`），读取优先 `parts_gz`、
  spine 只兜底并自动纠正历史反序数据；反序会让主站地图左上角多出一条竖线。回归 `test_geom_axis.py`。

### 地图

- 新页面**只用腾讯地图**（Key 存 `localStorage['railcn.mapkey']`，代码不含 Key，
  WGS-84 → GCJ-02），没 Key 自绘 Canvas 降级、不画国界；**别把整理台的 Leaflet + OSM 瓦片搬过来**。
  Key 按 **origin 隔离**：`localhost:8686` 和 `127.0.0.1:8686` 各存各的，换地址打开要重填；
  新页 Key 弹窗会显示当前站点。保存后**原地重建底图，不要整页刷新**。
- **独立页 `/linemap.html`（确认线路地图）**：左侧只列整理台全部确认线路、默认全选、
  复选 / 单选可切、点行名可单看一条；右侧腾讯地图画选中线路，1600 px 下占 83% 宽、
  收起左栏后 100%。数据走 `linemap.py` 的批量出口，几何 `parts` 逐段抽稀保首尾。
  腾讯地图 GL JS 用 `MultiPolyline`（一条线一个 geometry、`paths` 二维多段）/
  `MultiMarker` / `InfoWindow`，样式一次建全，之后只 `setGeometries`。
  无 Key 时 Canvas 画网格 + 线路（拖拽 / 滚轮缩放），不画国界、不引瓦片。
  回归 `scripts/test_linemap.py`，含注入 mock SDK 验证腾讯分支（不依赖真 Key）。
- **列车位置是时刻表推算，不是 GPS**（库里没有 position / 正晚点表），顶栏常驻「非 GPS」。
  `trainmap.py` 纯内存索引，跨午夜 `dep<arr` 补 1440；缓存键必须用原始 `types` 参数
  （`'other'` 解析后变空集会撞「不筛车种」）。
- **浮层点不动 = 层叠上下文**：`.tm-stage{isolation:isolate}` + `#tmMap/#tmCanvas{z-index:0}`，
  用 `elementFromPoint()` 复验。
- **贴线**（`traintrack.py`）：`line_base` > `line_geom` spine > `line_route` spine 贴真实折线，
  其余停站间直线；四道闸（最近点 / 弧长比 / 跳点阈值 / 两端站名）宁可少贴别贴错。
  贴线只作用于行车路线，列车当前位置点仍是站间直线插值。

---

## 前端样式约定（类名别撞车）

单文件 `static/app.js` 里几套东西共用同一份 CSS，类名一撞就是整块布局塌掉，动 class 前先看一眼这张表：

| 类名 | 干什么 | 注意 |
|---|---|---|
| `.od-form .od-row` | 查询表单的一行（车站/线路页签） | `display:flex`，输入框靠 `.field{flex:1}` 撑开 |
| `.mate-list / .mate-row` | 校对页「同起讫点车次」的六列 grid | 早期也叫 `.od-row`，grid 覆盖了表单的 flex，把输入框压成 24px |
| `.suggest > .si` | 联想下拉的条目 | 左 `<span>` 名字、右 `<span class="c">` 灰字、缺坐标加 `.nocoord`；**别再用 `.sug-item`**（CSS 里没有这个类，写出来就是一行没有内边距的裸文字） |
| `.list:empty` | 空列表 | 自动 `display:none`；车站查询是直接出详情，列表空着不该和 `.detail` 平分高度 |

回归：`python scripts/test_net_form.py` —— 盯输入框宽度、联想框宽度与行高、Esc / 点空白能否收起、空列表是否还占高度。

## HTTP 接口

| 接口 | 参数 | 说明 |
|---|---|---|
| `GET /api/stats` | — | 车次库 / 缓存 / 坐标覆盖率 |
| `GET /api/stations` | `q`、`limit` | 站名联想 |
| `GET /api/trains` | `q`、`limit` | 本地车次检索（车次号 / 起终站）；同一趟车的多个车次号已合成一项，带 `codes` / `codeLabel` |
| `GET /api/route` | `code`、`date`、`force`、`cache` | 经停站 + 坐标 + 绝对时刻（`cache=1` 只读本地库，历史数据页用）；带 `codes` / `codeLabel` / `codeSwitches`，每站带 `trainCode` |
| `GET /api/route/osm` | `code`、`date` | 同一趟车的「真实走向」折线（只读本地库）：`path[]` 带 `km`，另返 `total`/`covered`/`gapKm`/`cover`/`gaps[]`/`fill`/`lines[]`；几何**优先整理台确认线路的定稿走向**（`confirmed.geom()` → `lineosm.geom()` → 老 OSM 包），缺口的 `fill` 只算停车站；拼不出来时 `ok=false` + `error` |
| `GET /api/tickets` | `from`、`to`、`date` | 余票 |
| `GET /api/transfer` | `from`、`to`、`date`、`middle` | 中转换乘 |
| `GET /api/station` | `q` | 车站档案（路局 / 电报码 / 服务 / 连接线路 / 始发终到车次量）；档案里没有但 12306 站表有的站，返回最小档案（`archive=false` + 当天经停车次 `trainsVia`）。附 `coordCandidates`（各来源全部坐标，60 m 聚类 + 来源 / 坐标系 / 距当前距离 / `isCurrent`）、`coordConfirmSrc` |
| `GET /api/station/coords` | `q` | 单独取某站的候选坐标清单（字段同 `coordCandidates`） |
| `GET /api/lines` | `q`、`limit`、`osmLimit` | 线路名检索；`osm[]` 是「和查询词相关、线路库还没收录」的 OSM 线（`name` / `km` / `libSame` 同核心名的库线），能拼出站序的还带 `stopsN` / `stopsFrom`（资料库 / china-emu / 分段）——联想下拉里点到这种就直接进线路详情，不是一条光走向。`variants` 是每条线各来源的站序概况（**同一条线的几个「（底表）/（OSM）/（自建）」版本也在里面，`kind='member'`、用 `sourceName` 跳转**），`picks` 是这条线的默认版本 / 默认来源（联想里 ★ 画实心），`groups` 是归组信息（`base` 基础名 / `groupN` 版本数 / `default` 默认版本 / `effective` 生效版本） |
| `GET /api/line/confirmed` | `full=1`（可选） | 整理台「完成全部确认」的线路白名单：`total` / `draft` / `confirmed` / `incomplete` + `lines[]`。**`full=1` 另附 `items[]`**：全量不截断、按确认先后**倒序**（最新确认在最上），每项带 `name` / `ldId` / `confirmedAt` / `confirmSeq` / `confirmFrom`(`log`\|`line`) / `covers[]`(已确认别名) / `n` / `nCoord` / `nGeomPt` / `totalKm` / `fromSta` / `toSta` / `geomName`。确认时间取 `ld_log` 最后一次 `finalize`/`publish`（同秒按日志 id），无日志才回退 `ld_line.updated_at`，**不是** `updated_at` 直接当确认时间；`ld_log` 已进缓存指纹，③ 刚确认完立刻生效。主站「车站/线路」页下方常驻清单专用，不带 `full` 的老调用仍是纯统计数 |
| `GET /api/lines` · `/api/line` · `/api/line/osm` | `confirmed=1` | **显式开关**：只返白名单线路 / 详情 / `ld_geom` 走向，不带来源版本、OSM 候选、★、选线数据；未确认线给 `found=false + reason`（仍 200，不报 404）。`POST /api/line/km`、`/km/clear`、`/km/rebase` 也透传 `confirmed`，保存后回确认视图。不带此参数 = 老口径，见「确认线路白名单」一节 |
| `GET /api/line/stops` | `q`（线路名）、`confirmed`（可选） | 该线的完整站序（只有站名，按站序），供校对页「经由」选站；命名线路的站序里缺的线路所 / 乘降所 / 中继站会用资料库的同名站序按邻居位置补回来，只在资料库里的线路（银西高铁）也能出站序；OSM 有、库和资料库都没收的，按同名前缀的分段线拼（京哈高速线 = 京沈段 + 沈哈段）；`from` 说明站序来自哪（`lib` / `catalog` / `parts`），`found=false` 表示哪都没这条线；勾过默认来源的线按默认那份给，免得人工在详情页挑过的站名和选经由时的对不上；**同一条线的几个「（底表）/（OSM）」版本按「默认版本 > 底表」解析**（`q=京沪线` 在没设默认时给底表那份 150 站）；**`confirmed=1`（数据校对专用）只给整理台确认站序**（京广线 212 站），没确认的线 `found=false` 且不给站序 —— 前端这时退回这趟车的经停站 |
| `GET /api/linemap/index` | — | 确认线路地图页左侧清单：全量、不截断、按最近确认倒序，只认整理台「全部确认」线路；`items[]` 带 `name` / `nStop` / `totalKm` / `fromSta` / `toSta` / `confirmedAt` / `geomSrc` / `nGeomPt`，`total` 为总数、`scope=confirmed` |
| `GET /api/linemap/geom` | `names`（`\|` 分隔）、`detail`、`stops` | 批量取选中线路的多段几何（一次请求画几十条线，别逐条打）：`items[]` 每项带 `found` / `parts[]`（`[lon,lat]` 逐段）/ `nPart` / `nPt` / `bbox` / `geomSrc`，`stops=1` 另带确认站序 `stations[]`。**逐段抽稀**到 `detail`（400/900/2000）且每段首尾保留；非确认名静默跳过，`found=false` 只提示不报错。只读，20 秒缓存 |
| `GET /api/custom` | — | 用户新增的线路与车站清单 |
| `GET /api/stations/names` | — | 全部站名（9548 条 = 车站档案 + 12306 站表，给新增线路时的站名补全） |
| `GET /api/line` | `q` | 线路详情（有序站点 + 累计里程 + 坐标，可直接画线）；另带 `kmBase`（档案原值）、`segKm`（各段生效里程，不详为 `null`）、`override`、`osm`（OSM 线名 / 几何里程 / 桥隧）、`osmOther`（同名前缀下线路库没收录的 OSM 线，见上文「被同名线挡住」）。线路库里没有、但能补出站序的（成昆线 133 站），返回补好的线路 + `stopsFrom` / `stopsParts`；库内按段命名的线路（成昆线元昆段），`osm` 是借父线裁出来的那份，带 `borrowed` / `clipped` / `clip`。`sources` 是这条线名在各来源的样子，`pickedFrom` 是这次临时点选的来源，`srcPick` 是勾成默认的那份，`effFrom` 是实际生效的（点选 > 默认 > 系统规则）。`from=<来源>` 时按指定来源拼。**`group` 是归组信息**（`base` / `groupN` / `default` / `effective` / `effectiveWhy`：`default` 用户勾的 / `base` 没勾按底表 / `largest` 没有底表按站数最多）；搜基础名（`京沪线`）时系统按生效版本给，`autoFrom` 非空就是这个意思，`askedName` 是原本搜的词；`exact=1` 表示「我就要这个名字的那一份」，不再替换（来源条点版本时用）|GeoJSON 导入的线，`osm` 里 `source='geojson'`、带 `file` / `nPart` / `nPt`，`kmChain` 是按连续走向量的里程 |
| `GET /api/line/osm` | `q`、`km`、`confirmed` | 线路的几何走向（`parts` = WGS-84 **多段**折线数组，可直接画）；解析统一走 `lineosm.geom()`，走向编辑器也调它、保证两处画同一份。`confirmed=1` 只认确认线路 `confirmed.geom()`；其余优先基本线路 `ld_geom`（`merged_geom`）→ 来源包 → GeoJSON → OSM。`km` 给已知里程时挡掉同名残段；分段线名借父线几何并按首末站裁出本段 |
| `GET /api/line/osm/list` | `q`、`limit` | 搜 OSM 线路（线名 / 里程 / 桥隧 / 段数），附全库统计；线路库没收的线带 `stopsN` / `stopsFrom`（能补出站序时），列表上标「可补 N 站」 |
| `GET /api/line/osm/stations` | `q`、`radius`、`extend` | 按 OSM 走向反查沿线车站，返回候选站（`km` / `dist` / `side`）、单线里程 `spineKm`、已有同名自建线路的站表 |
| `GET /api/dates` | — | 含当日共 15 天，每天的下载状态 / 已确认开行数 / 待处理数，另带 `chain`（一键连跑进度） |
| `GET /api/history` | — | 已有数据的全部日期（车次数 / 停站记录数 / 导出文件） |
| `GET /api/history/trains` | `date`、`q`、`limit`、`offset` | 某日车次清单（含「是否已下载经停站」标记），可检索分页 |
| `GET /api/history/coverage` | `from`、`to` | 日期段的采集覆盖情况（每天枚举车次 / 停站数 / 是否只有部分数据）+ 各大类全库车次数 |
| `GET /api/history/calendar` | `code`、`from`、`to` | 某车次的开行日历：每天 `open`（开行）/ `skip`（当时未查到，非终态）/ `nodata`（无数据或待复核，带 `rechecking` 标记），带停站数、`runs` 连续开行区间 |
| `GET /api/history/types` | `from`、`to`、`type` | 按大类的开行 / 不开行统计（`openTimes` / `skipTimes` 按车次·日累计）；给了 `type` 另返该类的每日明细 `focus` |
| `GET /api/history/stops` | `code`、`date` | 某车次某日已落库的停站时刻（不联网；没下载会说明原因） |
| `GET /api/collect/start` | `date`、`workers`、`enum`、`future` | 开始或继续某天的批量下载（`enum=1` 先刷新车次全集；`future=0` 只搜当天，默认 `1` 同时搜预售最后一天） |
| `GET /api/collect/status` | `date` | 任务进度（阶段 / 本轮进度 / 失败 / 当前间隔） |
| `GET /api/collect/stop` | `date` | 暂停（进度保留） |
| `GET /api/collect/chain/start` | `workers`、`enum`、`force`、`start`、`future` | 一键连跑：先刷新全集（`enum=1&force=1` 绕过 7 天冷却）再按天依次下载；`start=<日期>` 用于「继续连跑」；`future=0` 关掉「同时搜预售最后一天」 |
| `GET /api/collect/chain/status` | — | 连跑进度（`status`/`index`/`total`/`curDate`/`plan`/`enumFuture`/`message`） |
| `GET /api/ld/stats` | — | 整理库现状（线数 / 已确认 / 别名 / 有站序 / 父子 / 各来源条数） |
| `GET /api/ld/lines` | `q`、`kind`、`source`、`status`、`hasStops`、`limit`、`offset` | 整理库线路清单，`q` 同时搜规范名和别名；有站序的排前面 |
| `GET /api/ld/line` | `id` | 一条线的全部：别名、站序、父线、子段 |
| `GET /api/ld/geom` | `id` | 画图用：站序坐标 + 借 OSM 别名取到的走向（取不到就只有站） |
| `GET /api/ld/candidates` | `id` | 同族但还没被任何线收下的来源写法（页面「收」按钮的数据） |
| `GET /api/ld/source/search` | `q`、`limit` | 在五套来源里搜线名（加别名时挑来源） |
| `GET /api/ld/source/stops` | `source`、`name` | 某个来源里这条线自己的原始站序（多方对比用） |
| `GET /api/ld/log` | `limit` | 改动流水 |
| `GET /api/ld/export` | — | 导出整理结果：规范名 ← 各来源写法 + 站序 + 父子关系 |
| `POST /api/ld/save` | `id` + 任选 `name`/`kind`/`status`/`total_km`/`note` | 改一条线 |
| `POST /api/ld/new` | `name`、`kind` | 新建一条线 |
| `POST /api/ld/delete` | `id` | 删线（别名、站序、关系一起删） |
| `POST /api/ld/alias/add`、`/alias/del` | `id`+`source`+`name` / `aliasId` | 收 / 删来源写法 |
| `POST /api/ld/stops/save` | `id`、`stops[]` | 整份覆盖站序（缺坐标按站名查档案补） |
| `POST /api/ld/stops/insert` | `id`、`names[]`（或字符串）、`pos` | **指定位置新增车站**：`pos` = 插入后的 0-based 起始下标（`0` 最前 / `i+1` 插在第 `i+1` 站之后 / `-1` 或 ≥站数 = 末尾），越界夹取不报错；一次多名（字符串按换行 / 逗号 / 顿号 / 分号切）按顺序连续插入。新站 `km` 留空不插值、坐标按站名查档案补，原有各站数据不动；复用 `save_stops()` 口径（`stop_ok=0` / `stops_edited=1` / 起讫更新）。回 `line.inserted` / `line.insertPos` |
| `POST /api/ld/stops/reverse` | `id` | **反向站序**：整条倒排，里程 `原末站里程 − 原里程`、新起点 0、缺的不插值；坐标 / 总里程 / `stop_ok` 不变 |
| `POST /api/ld/stops/fill` | `id`、`source`、`name` | 用某个来源的站序替换（china-emu 的 K 里程会平移成段内里程） |
| `POST /api/ld/import-geojson` | `id`、`data`、`fileName`、`only` | `only='stops'` 只导站点、`only='geom'` 只导走向、空值两边都导（顶栏建新线） |
| `POST /api/ld/src/line` | `id`、`items[]`、`rename`（默认 `false`） | ① 只登记走向来源：不碰 `ld_stop`、不改规范名 / 起讫 |
| `POST /api/ld/src/stop` | `id`、`source`、`name`、`fill` | ② 只确认站点 + 定位：不写 `ld_geom` |
| `POST /api/ld/finalize` | `id`、`name` | ③ 合并 ①② **并自动定为基本线路**；回 `published` / `publishError` / `baseInfo` |
| `POST /api/ld/publish`、`/api/ld/unpublish` | `id` / `name` | 定稿 / 撤销基本线路（脚本与老页面用；面板 ④ 按钮已取消） |
| `POST /api/ld/merge` | `ids[]`、`into`、`name` | 合并多条线，别名/站序/关系全部转移 |
| `POST /api/ld/relation`、`/relation/del` | `parent`、`child`、`kind` | 挂 / 解除父子（会挡成环） |
| `POST /api/ld/rebuild` | `force` | 从 rail.db 补新线名；`force=1` 清空重建 |
| `GET /api/collect/chain/stop` | — | 暂停连跑（当天进度保留） |
| `GET /api/collect/download` | `date`、`fmt=json\|csv` | 导出并直接下载 |
| `GET /api/collect/files` | — | 已导出的文件清单 |
| `GET /api/curate/stats` | — | 校对统计（待补全 / 经由 / 里程 / 坐标 / 已校正 / 其中同步（已校正的子集）/ 自动待确认）+ 扫描进度 |
| `GET /api/curate/list` | `filter`、`q`、`limit`、`offset` | 校对清单，`filter=todo\|via\|via0\|km\|coord\|pending\|manual\|synced\|clean\|all`；每项带 `codeLabel` / `codes` / `viaOrigin` |
| `GET /api/curate/detail` | `code` | 某车次的**自动推断基线**（绕开人工校正）+ 已有校正（含 `source` / `basedDate`）+ 经停站 + `gapCands`（缺口候选线路）+ `odMates`（同起讫点的其它车次与停站概况，每项带 `viaFits`：它的停站是否全在这条经由上；已校正/已同步的不列入，只报 `hidden` 趟数）+ `overrideStale`（站表变没变） |
| `GET /api/curate/mate` | `code` | 某趟车的停站明细（站名 / 到发时刻 / 跨日 / 每站车次号），给「同起讫点 → 停站详情」展开用 |
| `GET /api/curate/lineNames` | `sta`（可选）、`q`、`limit` | 补经由时的线路下拉：**只有整理台完成全部确认的线路**（`confirmed.recent()`，`scope=confirmed`）。来源版本（京广线（OSM）/（底表）…）和没确认的线一律不出现；`items[]` 为对象（`name`/`n`/`totalKm`/`src='confirmed'`/`inLib=false`/`base=true`），`names[]` 是纯名字。名单随整理台实时变（2 秒缓存），确认一条多一条。`sta=` 按发车站筛（站名键走 `confvia._key`）、`q=` 按线名关键字搜、`limit=` 截断（地图搜索只回 20 条）；**不下发全库站→线索引** |
| `GET /api/curate/linesForStops` | `stas`（`\|` 或 `,` 分隔） | 「本趟车这些站 → 含该站的确认线路名」映射，**规模随车次站数走**。前端 `cuOpen` 一次预取用于线路下拉的本地零延迟过滤；站名按 `confvia._key` 归一（「北京南站」=「北京南」），未录入的站给空数组。线路/车站以后增加自动生效，不用改名单 |
| `GET /api/curate/lineMap` | `name` | 校对页右侧走向图的数据：只认 `confvia.names()`；`parts` 是与主站/编辑器同一份 `lineosm.geom(confirmed_only=True)` 多段（`[[lon,lat],…]`），总点数 >2000 按段等距抽稀且每段首尾保留；`stations[]` 取 `confirmed.detail_payload()` 带 lat/lon 的确认站序（已套人工坐标）；带 `nPart`/`nPt`/`nStop`/`totalKm`/`geomSrc`。没确认的线给 **200 + `found=false`**，别报 404 |
| `POST /api/curate/refresh` | `code` | 线路库改过之后重算这趟车：重载线路库 → 重算自动基线 → 刷新清单摘要；返回详情 + `library`（线路库条数）+ `summary` |
| `GET /api/curate/cands` | `from`、`to` | 缺口两端的候选线路（`line` / `src='confirmed'` / `srcCn`），**只从确认线路里挑**：两站都在站序里的排前面（`srcCn=确认线路（两站都在）`），只剩一头的垫后；给「点选候选段」用 |
| `POST /api/curate/scan` · `GET /api/curate/stop` | — | 全量扫描开始 / 停止（后台线程，进度可轮询） |
| `POST /api/override` | `code`、`via[]`、`totalKm`、`segKm`、`note`、`date`、`syncCodes[]`、`syncOverwrite` | 保存人工校正；`segKm` 形如 `{"0":205,"2":230}`（键是区间序号）；`date` 记成 `based_date`（校正依据哪天的站表）；全为空即撤销。`syncCodes` 是勾选的同起讫点车次，保存后逐趟套过去、各存一条自己的校正，返回 `synced[]`（每项 `ok` / `reason` / `segs` / `mode`：`direct` = 停站全在经由内、原样照抄，`adapt` = 按它的停站重新落站）；对方已有校正默认跳过，`syncOverwrite=1` 才覆盖 |
| `POST /api/line/km` | `name`、`segKm`、`totalKm`、`note` | 保存线路里程人工校正，返回最新线路详情；全为空即撤销 |
| `POST /api/line/km/clear` | `name` | 线路里程恢复档案值 |
| `POST /api/line/new` | `name`、`stations[]`（`{name, km}`，km 可空）、`note`、`replace` | 新增线路，返回线路详情 + `filledStations`（这次补进车站库的沿线站）；`replace=1` 允许覆盖同名**自建**线路（改站表用） |
| `POST /api/line/stations` | `name`、`stations[]`、`note` | 改站序（站中间插站 / 调先后）：自建线路改自己的站表，档案线路写 `line_override.stops_json`（人工站序覆盖，原始档案不动），返回线路详情 + `stationOrder`；线路库没有这条线时先按资料库规范名收进线路库，返回里多 `adopted` / `adoptedFrom` |
| `POST /api/line/stations/clear` | `name` | 撤销档案线路的人工站序，回到档案原样（连带清掉按旧下标算的人工里程） |
| `POST /api/line/stations/reverse` | `name`、`from`、`mode`（空=切换 / `on` / `off`） | **按来源**倒排：只翻 `from` 那一份的站序，里程按「原末站里程 − 原里程」重算（新起点强制 0，缺里程的仍缺、不插值）。`catalog`/`chinaemu`/`osm`/`parts` 只记一条 `line_src_fix(kind='reverse')` 读时翻转，原始表不改，`inPlace=true`；`lib`/`custom` 那份就是线路自己，走改站序（`inPlace=false`，返回 `line`）。再点一次 = 恢复，不是二次翻转 |
| `POST /api/line/src-pick` | `name`、`source` | 勾「这条线默认用哪一份站序」（`lib`/`custom`/`catalog`/`chinaemu`/`osm`/`parts`，空 = 取消）。勾上以后所有不指定来源的取数都按它：`/api/line`、`/api/line/stops`（选经由）、`lineseg.stations_for()`。指定 `from` 的临时查看仍然优先 |
| `GET` / `POST /api/line/default` | `q` / `name`；POST 另带 `pick` | **默认版本**：POST `{name:"京沪线", pick:"京沪线（底表）"}` 把这条线的默认定成那份（`pick` 空 = 取消，回到「底表 > 站数最多」）。存 `line_default`（基础名 → 版本名）。生效范围：`/api/line`（搜基础名时）、`/api/line/stops`（选经由）。**不再影响 `/api/curate/lineNames`** —— 校对下拉 2026-11-11 起只列整理台确认的线路 |
| `POST /api/line/delete` | `name` | 删除自建线路（连带清掉它的里程校正和 `line_geom` 走向） |
| `POST /api/line/import-geojson` | `data`（GeoJSON 对象或字符串）、`fileName`、`replace`、`note` | 按人工整理的 GeoJSON 重建一条线路：解析 → 建图（端点合并 + 2 km 断口补边）→ 车站投影切线 → 排站序 → 图上最短路算里程 → 落成**自建线路**，走向压进 `line_geom`。返回 `n` / `totalKm` / `chainPoints` / `broken`（补不了的断口）/ `dropped`（投影不上、丢掉的站）/ `offGeom`（偏离走向的站）/ `catalog`（资料库同名线的站数，用来对账）。同名**自建**线路要 `replace=1` 才覆盖，档案线路一律不动 |
| `GET` / `POST /api/line/import-geojson/list` | — | 已导入的 GeoJSON 线路清单（线名 / 文件 / 几何里程 / 段数 / 点数 / 导入时间） |
| `GET /api/backup/lines` | `tag`、`source`、`q`、`limit`、`offset` | 备份里的线路清单（`tag` 空 = 所有批次合起来看），每条带来源（`custom` 自建 / `base` 底表 / `osm` / `ce` china-emu / `catalog` 线路资料库）、站数、里程、首末站、`inLib`（线路库里有没有）；另给 `counts`（各来源条数）和 `batches`（批次及其时间、条数） |
| `GET /api/backup/line` | `tag`、`source`、`name` | 某条线在备份里的**完整站序 + 累计里程**（只读），带 `geom`（备份里存没存走向）和 `inLib` |
| `GET /api/backup/geom` | `tag`、`name` | 备份里那条线的连续走向（`parts`，给预览用），没存走向返回 `{}` |
| `POST /api/backup/restore` | `tag`、`source`、`name`、`replace` | **挑定了才调这个**：把这条线正式写进线路库（自建线，站名 / 里程照备份原样，`source='backup'`），备份里有走向的连 `line_geom` 一起写回。同名线在库里时 `replace=1` 才覆盖 |
| `POST /api/station/new` | `name`、`lat`、`lon`、`lines[]`、`tele`、`bureau`、`note` | 新增车站 |
| `POST /api/station/delete` | `name` | 删除自建车站 |
| `POST /api/coord` | `name`、`lat`、`lon`（或 `coord` 一行文本）、可选 `src` | 设置车站坐标（任何站，写 `coord_override`，立即生效并重算相关车次）。`src` = 车站卡片「用这个」确认的候选来源说明，记进 `coord_override.src`、卡片显示「已确认自 XX」；手填补坐标时不传 |
| `POST /api/coord/clear` | `name` | 撤销人工坐标，回到车站档案原值 |
| `GET /api/osmedit/lines` | `q`、`limit`、`offset` | 走向编辑器线路清单：**只收真有走向的线**（`line_geom` / `line_route` / `ld_geom` / 草稿 / OSM 全量线名去重，当前 4513 条）**+ 整理台全量已确认线路**（`confirmed.recent()`，当前 21 条），滚动分页。每条带 `src[]`、`km`、`hasEdit`、`loadName`、`alias`/`aliasOf`；确认线路另有 `confirmed=true` / `ldId` / `geomName` / `geomSrc` / `confirmedAt` / `nStop`，`loadName` 一律规范名、`src` 去掉 `alias`。**排序：测试名沉底 → 确认线路优先 → 里程倒序**；按线名 / `loadName` / `geomName` 筛选（如输「京沪四线」能搜到济南线）。返回 `count` / `total` / `confirmed`（确认线路总数）/ `hasMore`；几何索引缓存 20s，确认行每次现算（签名变了才重排），写草稿/建线/删线/确认线应用后自动失效 |
| `GET /api/osmedit/geom` | `name`、`n`（抽稀目标点数，前端默认 600） | 载入一条线的走向：与 `/api/line/osm` 共用 `lineosm.geom()`，**返回主站正在显示的那份 `parts` 多段**（`[lon,lat]`，段与段之间不连线）；草稿 `line_geom_edit` 优先，自己没几何但记了 `geom_name` 时回退一层借那份（返回 `loadName`）。逐段 RDP 抽稀到约 `n` 点（每段至少留首尾两点，段内 >25 km 的空隙补回中间点），`totalKm` 按各段累加、**不含跨段假直线**；返回 `nPart` / `nPt` / `totalKm` / `source`（`edit` / `confirmed` / `main`）/ `hasEdit` / `stops`；确认线路另带 `confirmed` / `keepsConfirmed` / `ldId` / `geomName` / `geomSrc`，`loadName` 恒为规范名 |
| `POST /api/osmedit/geom` | `name`、`parts`、`totalKm`、`src`、`note`、`apply`、`target`、`confirmed` | 存编辑结果到 `line_geom_edit`（**不动原几何**）；`parts` 多段原样写 `parts_gz`（扁平链只作 `spine_gz` 兜底，主站 `merged_geom` 优先读 `parts_gz`），`n_part` = 段数、`totalKm` 按段累加，返回 `nPart`；`apply=1` 才写回。`name` 命中整理台确认线路时走**专属通道**：`_apply_confirmed()` 按 `ld_id` 更新 `ld_geom` 里 `merged_geom()` 会命中的那行 + 规范名同名行，一条都没有（③ 的 `osm` 分支）就 INSERT 一行 `line_id=ldId`；同时更新同名 `line_geom`；**全程不写 `ld_line`**，返回 `confirmed` / `keptConfirmed` / `ldId`，保存后仍是整理台已确认线路（保存草稿 `apply=0` 不写回）。非确认线沿用旧口径：`_apply_to_library()` 只覆盖已有同名 `ld_geom` / `line_geom`，`target` 指定写回的几何名（借走向的线写回被借那份），草稿仍存 `name`。返回 `applied{ld,lib}` / `missing`（库里没有这条线，得走建线） |
| `POST /api/osmedit/stations` | `parts`、`name`、`radius`（米，默认 2000） | **沿线抓站**：距离量这条线的**原始碎段**（`_SegIdx` 网格索引，不串线），顺序/里程按 `parts` 投；返回 `items[{name,lon,lat,km,dist}]`（按 km 排序）、`count`、`basis`、`wide`（是否放开到全部 OSM 站兜底） |
| `GET /api/osmedit/stations` | `q`、`limit` | 按名字（含别名）在 `station_master` 里搜站，给「按名字加一站」用 |
| `POST /api/osmedit/new` | `name`、`parts`、`stops[]`、`note`、`replace` | **新建线路**：把画的走向 + 选中的站组装成最小 GeoJSON，走 `geoimport.import_line`（raw 通道，唯一建线路径）落成自建线路 + 写 `line_geom`，并留一份 `line_geom_edit` 草稿。同名线先拦（自建线可 `replace=1`），站数 <2 报人话 |

`/api/route` 返回结构（前端直接消费）：

```json
{
  "code": "G1", "codes": ["G1"], "codeLabel": "G1", "codeSwitches": [],
  "from": "北京南", "to": "上海虹桥",
  "startIso": "2026-10-21T06:30:00", "endIso": "2026-10-21T11:24:00",
  "durationMin": 294, "totalKm": 1318.0, "straightKm": 1176.4,
  "kmMode": "real", "kmFactor": 1.12, "kmMatched": "6/6",
  "viaLines": ["京沪高速线"],
  "stops": [
    {"i":1,"name":"北京南","trainCode":"G1","arr":null,"dep":"06:30","stayMin":0,
     "runMin":0,"km":0.0,"lat":39.86333,"lon":116.37205,
     "bureau":"中国铁路北京局","tele":"VNP","lines":["京沪线","京沪高速","京津城际"],
     "iso":"2026-10-21T06:30:00","depIso":"2026-10-21T06:30:00"}
  ],
  "path": [{"lat":39.86333,"lon":116.37205,"km":0.0}],
  "pathPoints": 27,
  "missingCoords": [], "coverage": 100.0, "source": "cache"
}
```

## 时间条上的停站

时间条不只画进度，还把**每一停站的站名和时刻**直接标在轴上——轴上每隔一段就有一个小圆点，圆点下面两行小字，上行是站名、下行是时刻：

- **位置**按到站时刻在全程中的占比放（首站用发车时刻），所以站与站在轴上的疏密就是实际的运行快慢。
- **时刻口径**：首站显示发车，中间站显示到达，终到显示到达。悬停任一站点还能看到「到 / 开 / 停几分钟 / 完整日期时间」。
- **挤不下就错层**：站距太小、标签会打架时自动移到下一层，最多两层；首末站一定保留标签，实在挤就单独占第三层。
- **再挤不下只留圆点**：标签放不下的站仍然有一个小圆点在轴上（说明这站确实停了），悬停或点击就能看到时刻。
- **点标签 = 跳到那一刻**：列车图标和时间同时落到该站，播放暂停。
- 站多的时候时间条会变高，嫌挡地图就点右下角 **「停站」** 关掉，选择记在本机，下次打开还是关的。

> 站特别多的车（30 站以上）在 1000px 宽的窗口里一层只能放十来个标签，剩下的只能靠悬停。想全看到，把窗口拉宽或看左侧「车次详情」里的完整时刻表。

## 地图上的两种路线

车次详情的地图可以画两套走法，点车次详情里「按真实走向 / 回到站点直连」切换：

| | 怎么来的 | 适合看什么 |
|---|---|---|
| **站点直连**（默认） | 前端只取 `/api/route` 的 `stops[]`（本趟车实际停车站）连直线，**不画通过站** | 站序、里程、时刻：一眼看清在哪停、跑了多远 |
| **沿真实线路** | `GET /api/route/osm` → `railosm.build()`，几何优先取**整理台确认线路的定稿走向**（`confirmed.geom()` → `lineosm.geom()`），老 `geoms.json.gz` 兜底 | 线路实际怎么绕：弯道、绕行、跨江位置 |

> 2026-11-11 修：以前「站点直连」直接画后端 `path_stations()` 从**线路库**展开的
> 通过站折线（假坐标 / 乱站序 → 北京西—保定段来回折返、中卫—兰州段一串锯齿），
> 而「沿真实线路」只认老 OSM 包（没有整理台刚确认的「太中线」「石获线」，Z75 的
> 太原→中卫 700 km 只能画一根大直线，两种模式看着都不对）。现在直连只连停车站，
> 真实走向用确认线路的几何；后端 `/api/route` 的 `path`/`lineStops` **保持不变**
> （其它页面还要用），前端存到 `route.expandedPath` / `expandedStops` 备用。
>
> 2026-11-12 补：这里的「保持不变」指**别为了修直连去改后端的画法**，不是不许把
> `path` 本身修对。N1534/1（11-25 阿克苏→乌鲁木齐）人工经由跨三条线，
> `cnrail._via_chain()` 因「大河沿联络线（china-emu）」后缀名和线路所「头道河所」
> 解析不到，只拼出 1 段，库尔勒→乌鲁木齐 270 km 一个中间站都没展开；`railosm`
> 拿这根直线当走廊，把绕去吐鲁番的几何全裁了。现在链能拼全（`_via_line()` /
> `_via_sta_pos()`），`path` 补出 和硕 / 托克逊 / 珍珠泉 / 大河沿 / 盐湖西…，
> 覆盖 `0.781→0.995`、缺口 `174→4 km`。**`railosm.py` 没动**；直连仍只连停车站。
> 换向车次号 `N1531` 的人工校正只按主号 `N1534` 存，原来直接调接口会退回自动推断；
> `railcore.build_route_payload()` 现在「先查自己那条、没有才继承主号」，两个车次号
> 的 `/api/route`、`/api/route/osm` 与页面显示完全一致（自己有独立校正的仍优先用自己的）。

> 2026-11-12 再补（N17）：上面那句「`railosm.py` 没动」只针对 N1534 那次修复。
> N17（11-25 北京→齐齐哈尔）随后又暴露两层问题：`cnrail._via_chain()` 改用**整理台
> 确认站序优先**（底表滨洲线缺哈尔滨—红旗营整段、平齐线尾段方向乱，chain 断在大庆西）；
> `railosm.build()` 改成**按线名分组 + 按本段 `from→to` 确认站序二次裁剪几何**
> （滨洲线往满洲里的延伸段原本会被贪心选中，把真正进齐齐哈尔的短段挤掉）。
> 站序坐标不可靠时（`_seg_hug()` < 0.60，如 Z75 石太线）跳过二次裁剪，别把真走向切光。
> 实测 N17 cover 1.0 / 缺口 0 / 终点离齐齐哈尔 0.44 km / 尾段不再西绕到 123.3°E。

> 2026-11-12 定稿（用户「把这次的修改规则固定下来，用到所有的线路车次详情的
> 真实线路上」）：上面两条不再是 N17 特例，而是**所有车次**的统一规则。
> 站序统一走 `cnrail.via_segment_polyline()`（与 `path_stations()` 的跨线链同源：
> 整理台确认站序优先、线路库 / 来源后缀版本兜底），以前几何裁剪另写一份
> 「只认确认线路」的解析 ⇒ 943 个经由段只有 5.1% 生效，现在 98.2%。
> `build()` 另加**全局不退化闸**：裁过 / 不裁各拼一次，覆盖率掉 >2 个百分点或
> 终点多偏 >5 km 就整趟退回不裁；`clipped[]` 回报真正裁过的线名。
> `/api/route/osm` 的进程缓存按两库 mtime 指纹失效，改过站序 / 几何 / 人工经由
> 后已看过的车次也会按新规则重拼。

> 2026-11-12 再定稿（用户「车次详情 地图展示还是不行，帮我重新按照我的逻辑来写，
> 并且将原来的逻辑保留」+ 三条规则）：新增 `routeshape.py` 作为**默认画法**
> （`/api/route/osm?mode=confirmed`），下面这套 `railosm.py` 的拼法**一行没改**，
> 通过 `?mode=legacy`（前端画法下拉）随时切回去。新画法只认整理台确认线路：
> ① 站序与累计 km 全部按确认线路；② 跨线在中转站转线、碎段断裂自动连最近的
> 一对端点、长出截至起讫、不足用首末点补；③ 上下行分线只选一条，**含停站的那条优先**。
> 用户没提及的场景（非确认线路段、两段经由之间的空隙、整趟没有确认线路）
> 按原方式续画 —— 非确认段与空隙直接从 `railosm.build()` 的结果按 km 区间切片接上，
> 整趟都没有确认线路时 `route_osm()` 整趟回退 legacy 并在 `info.fallback` 标明。
> 实测 N17 2093 点 / cover 1.0 / 终点 0.00 km / 尾段 123.724°E；Z75 3196 点 / 7 段全确认。
> 回归 `scripts/test_route_shape.py` 55 项。旧画法的拼法说明保留在下面，别再删。

**拼法**（`railosm.py`，`mode=legacy` 时用）：

0. **先按线名分组**，每条经由线路取本趟车 `from→to` 那一截的站序折线
   （唯一入口 `cnrail.via_segment_polyline()`，`_seg_polyline` 只做 20 秒缓存），
   按它对几何逐点二次裁剪（`_clip_to_seg`）；站序坐标明显不贴线时整条退回原几何
   （`_seg_hug` < 0.60 不裁，非确认站序抬到 0.75），避免把真实走向裁光；
1. 逐点把每个几何段投到车次走廊上，按「离走廊 ≤ off km」切成**连续子段** —— 整条线的
   几何（京广线 28 段 / 2344 km）里只留本趟车走的那一截，广州以南的段自然被切掉，
   也不会因为长段一端在广州就把良乡—石家庄那一截整段丢掉；
2. 子段两端投到走廊拿到里程，段内按弧长线性插值；走廊宽度随全程里程自适应（12~45 km）；
3. 各段按里程区间贪心覆盖，复线上下行只留一条；
4. 覆盖不到的地方**只用本趟车停车站**直线补过去（别用线路库展开的通过站，坐标不可信），
   缺口位置和里程仍如实报给前端。

切到「沿真实线路」时，按钮右边会显示这次拼得怎么样，比如 `10 处缺口 · 94 km 走站间直线`；全程都贴合时显示 `全程沿真实线路`。

**实测数据**（2026-10-28，均取当天缓存）：

| 车次 | 区间 | 全程 | 折点数 | 缺口 | 缺口里程 | 覆盖率 |
|---|---|---|---|---|---|---|
| G1 | 北京南→上海虹桥 | 1302 km | 519 | 10 处 | 94 km | 93% |
| G1033 | 北京南→上海虹桥 | 1446 km | 429 | 0 | 0 | 100% |
| K8361 | 信阳→上海 | 792 km | 270 | 1 处 | 107 km | 86% |
| D1 | 北京西→长沙 | 1579 km | 824 | 1 处 | 216 km | 86% |
| D2290 | 深圳北→南通 | 1788 km | 669 | 3 处 | 660 km | 63% |

**已知局限**：这份几何是按线名切碎的（一条 1300 km 的高铁被切成 55 段，总长还是上下行双算的 2437 km）。所以：

- 干线车次贴合很好 —— G1 京沪 1302 km，只有 94 km 靠直线；
- 跨线 / 普速车次缺口多 —— D2290 深圳北→南通有 660 km 只能走站间直线（末段 600 km 那条线的几何还没进库）；
- 想让某条线本身画得准，从车次的经由线路名点进「车站 / 线路」页看那条线。
- 缺口只影响画得像不像，不影响里程、时刻和列车位置：位置一律按里程在折线上插值（`pointAtKm`），换哪套折线都对得上。

> **本项目只用「线路 + 车站」两类数据**：线路一侧是 `data/crtrain/lines.json` 的站序与里程，
> 车站一侧是 `stations.json` / `station_coords.json` 的坐标，走向靠 `geoms.json.gz` 里**按线名切好的**几何来对齐。
> 「把 OSM 全部铁路 way 建成一张路网再 A\* 寻路」那条路试过（早先的 `railgraph.py`），单程 6~11 秒、部分 OD 直接不连通，
> 还要背 300 MB+ 的 `railways.geojson`，和「按线名 + 走廊」比没有优势，**已从仓库移除，不再用这条路**。

## 关于「即时位置」

12306 不对外提供列车 GPS，所以位置由时刻表推算：

```
绝对时间 = 发车日期 00:00 + running_time（自始发站起的运行分钟）
段内位置 = 上游站坐标 + (下游站坐标 - 上游站坐标) × (已过时间 / 区间运行时间)
```

在区间内按匀速近似，停站时钉在站上。HUD 里的「速度」是区间平均速度（里程 ÷ 区间时长）。
优点是稳定、离线可用；缺点是列车晚点时会有偏差（见下方「后续」）。

## 全路网列车位置地图（`/trainmap.html`）

首页顶栏「列车位置地图」进入，是**独立页面**（不是页签）：左边查询/统计/结果列表，右边全屏地图。
和上面的「即时位置」是同一套口径 —— **按时刻表推算，不是 GPS 定位，也不是正晚点数据**。
12306 不提供列车定位，库里也没有任何 `position` / `late` 表；页面顶部常驻这条提示。

**数据怎么拼出来的**（`webapp/trainmap.py`，纯内存索引，不新建表、不改库）：

```
train_universe  1.78 万车次   （code / train_no / 起讫站）
  × route_tpl   1.26 万模板   （每 train_no 取最新一份 12306 raw → 到/发/跨天 + 车站坐标）
  × route_day   窗口内真实开行（今天 −2 天 ~ +3 天，接住跨天长途车）
  = 约 7.6 万个「开行实例」
```

每一刻按「发车日 00:00 + 分钟」二分定位到区间，在上下游站坐标之间按时间比例插值；
停站钉在站上。某天没登记开行、但前后几天开过的车会按「**推定开行**」补进来（约 270 趟），
统计和列表都标出来，可一键隐藏。`arrive_day_diff` 处理跨天，停站跨午夜会自动补 1440 分钟。

**性能**：位置索引 + 真实走向索引都在服务启动的后台预热线程里构建（合计约 6~10 秒，不在请求里等）；
快照按「分钟」缓存 60 秒，一次请求 0.05~0.3 秒（含 3400 条 JSON 序列化）。
`route_day` / `route_tpl` 指纹变了会自动重建，也可以点顶栏「重建索引」手动重建。

**查询方式**（左侧①区，全部可叠加）：

| 维度 | 说明 |
|---|---|
| 关键词 | 车次号（`G1` 精确命中；`G10` 前缀）+ 起讫站包含 |
| 车种 | 固定筛选项：X / G / T / K / D / C / S / Z / N / V / Y / 数字 / B / 其他，多选；**标签按查看日期分时代**（`<2026-11-25` 旧：T=特快、K=快速、N=字头；`>=2026-11-25` 新：T=高速动车组、K=高速动车、N=快速普速、V=普通快速、X=超高速），见 `trainmap.js` 的 `TYPE_LABELS_OLD/NEW`；只按字头筛，不按字头推车种 / 速度 |
| 始发站、终到站、途经站 | 三个独立条件，`途经站` 含起讫和全部经停站 |
| 运行状态 | 在途（默认）/ 仅运行中 / 仅停靠中 |
| 地图视野 | 勾「只统计当前地图视野内」后按 `bbox` 过滤，拖拽缩放自动重查 |
| 时间 | 实时（30 秒自动刷新）/ 指定任意时刻 / ±1 分 ±10 分 ±1 时 / 2 分钟一跳播放回放 |
| 推定开行 | 可一键隐藏 |

**标注**：列车标记旁写车次号 + 起讫站。全路网 3400 趟同时标会糊成一片，
所以默认按经纬网格**聚合**（紫色圆 + 数字，点一下放大拆散），结果 ≤40 趟时自动全部标注
「`G1 北京南→上海虹桥`」，41~220 趟只标车次号，更多时只画点；顶栏「标注」可强制改。
点任一列车 → 地图自动对准、画**行车路线**（已整理线路贴真实走向，见下节）、右侧浮层给出
发车终到/当前区间/区间进度/速度/下一站 ETA/完整停站表；左侧结果列表始终完整显示车次号与起讫站。

**底图**：只用**腾讯地图**（合规地图服务）。Key 和主页面「地图 Key」按钮（及数据校对右侧地图工具条）共用，
存 `localStorage['railcn.mapkey']`，**代码里不含任何 Key**；WorkBuddy 预览环境下走官方
`_TMapSecurityConfig` 代理（SDK URL 不带 key）。库里坐标是 WGS-84，喂给 TMap 前统一 wgs2gcj。
没配 Key / SDK 加载失败 → **自动降级为自绘 Canvas**：1.8 万个车站散点当底图（不画国界）、
同样的聚合/标注/轨迹/点击/缩放拖拽，完全离线可用。
> 别为了省事在新页面引 Leaflet + OSM 瓦片：地图服务有合规要求，
> 整理台里那套是历史代码，新增页面不要沿用。

### 行车路线：已整理线路贴真实走向，其余停站间直线

2026-11-06 用户定：**整理确定的路线用实际路线，其他用停站间直线**。
新增 `webapp/traintrack.py` + `/api/trainmap/track?code=&date=`，**逐站间区间**独立判定：

| 数据源 | 什么算「整理确定」 | 优先级 |
|---|---|---|
| `line_base` | 整理台「定为基本线路」/ 全项目接管的规范线路 | 0 |
| `line_geom` | 导入 / 合并好、有连续 spine 的走向 | 1 |
| `line_route` | 来源包自己的走向（`rg` / `hdx` / `cnrail`） | 2 |

只有碎段、串不成连续折线的线**不算**。按坐标贴线并设了四道闸：站到几何最近点
≤4~18 km；沿线弧长 / 直线距离在 0.65~2.6；区间内不允许 > `min(30km, 区间/2)` 的跳点
（合并走向偶尔把不相连的碎段用 90~150 km 大直线连上，直接画会横穿半个省）；
线路若在整理库里存过站序、而两端站名一个都不认得，判「贴到隔壁岔线」放弃。
短于 12 km 的区间一律直线（收益小、最容易贴错）。

返回 `pieces[{mode,line,src,km,pts}]`：真实走向画**红色实线**，没走向数据的画
**橙色虚线 / 细线**；详情卡显示「实际走向 X/Y 段（覆盖 N%）」和用到的线名，
另有「**看全程路线**」按钮把视野对准整条路径（默认只对着列车当前位置）。
实测 G1 5/6 段贴「京沪高铁」、D2290 24/38 段贴「杭深线 / 杭台高速线」、T110 10/10；
普速车因很多小线还没整理走向，覆盖率明显低 —— 这是数据缺口，不是代码问题。

索引 3686 条折线 / 47.8 万点，构建约 2 秒（服务启动后台预热），单次贴线 0.02~0.05 秒，
结果按 `(车次, 日期, 站序指纹)` 缓存；导入新走向 / 重新定稿后点「重建索引」会一起失效。
回归 `scripts/test_traintrack.py`（45 项，纯读库不写库）。

> **别混为一谈**：贴线只发生在「行车路线」这条折线上；**列车当前位置点**仍然是在
> 上下游站坐标之间按时间直线插值（没做每趟车的沿线投影），弯道仍会偏离真实线路。

**浮层点击坑（2026-11-06 修）**：腾讯地图 SDK 会在 `#tmMap` 内插入 z-index 上千的瓦片/控件层。
容器若是 `z-index:auto`，这些层会越过同级的详情卡 / 工具条 / 下拉框，表现就是
「地图上层的窗口点不动」。现在 `.tm-stage{isolation:isolate}` + `#tmMap{z-index:0}`
把 SDK 锁在地图层内部，浮层提到 20 / 30 / 40、弹窗 100。
改地图容器层级前用浏览器 `elementFromPoint` 复验，别只看截图。

**接口**（`server.py` 手写 `if path == ...` 分支）：
`/api/trainmap/snapshot`（主快照）、`/trainmap/train`（单车详情 + 停站表）、
`/trainmap/track`（行车路线折线）、`/trainmap/backbone`（降级用车站散点）、
`/trainmap/stats`（含 `track` 索引统计）、`/trainmap/rebuild`（同时重建位置与走向索引）。

**已知限制**：列车位置点是站间直线插值（见上），没有晚点数据；
速度是「直线 × 1.25 ÷ 区间时长」的估算值（上限钳到 400）；
回放窗口是今天 −2 ~ +3 天；没有真实走向的区间只能连直线。

## 地图 Key（可选）

未配置时页面用无底图示意模式（站点、线路、位置全都能用，只是没有底图）。

配置方法：打开 <https://lbs.qq.com/dev/console/application/mine> 创建应用 → 添加 Key（Web 端 GL JS）
→ 网页右上角「**地图 Key**」按钮粘贴 Key 保存；在**数据校对**页也可以直接点右侧走向图工具条上的
「**设置底图 Key**」（没 Key 时）／「腾讯底图 ✓」（已生效时）／「底图未就绪」（Key 填了但没起来），
打开的是同一个弹窗。主站、线路整理台、走向编辑器、数据校对共用
`localStorage['railcn.mapkey']`，填一次全站生效。

**保存后原地生效，绝不刷新页面**（刷新会丢掉校对页选中的车次和正在编辑的经由）：
`TMapSDK.load(force)` 重新注入带 Key 的 GL JS 脚本 → 重建主站 `TMapView` →
`cuMapUpgrade()` 让校对页右侧从 Canvas 升级成真底图。Key 只存本机 localStorage，不会写进代码。

两个曾经的坑，别改回去：
- 腾讯 GL JS 在 `display:none` 的容器里创建会拿到 0 尺寸、底图不渲染 ——
  `cuMapBackend()` 必须**先移除 `#cuTMap` 的 `hidden` 再 `new TMap.Map`**，然后 `resize()`。
- SDK 可能是在页面起来之后才加载好的（用户运行中填 Key）—— `cuMapEnsure()` 允许
  `canvas → tmap` 升级，别把后端一次性锁死。

> 加载失败时弹窗就地显示原因（不关闭、不清空输入），按钮变「底图未就绪」。
> 排查：Key 类型必须是 **Web 端 / JavaScript GL**、控制台域名白名单放行
> `localhost` / `127.0.0.1`（或先不限制）、网络可达。否则自动退回无底图示意模式。

## 已知限制

- **里程**分三档：`实测里程`（线路表匹配）/ `部分实测` / `里程估算`（直线 × 弯曲系数），页面上有角标。
- **坐标覆盖**：cnrail 库 7579 站全有坐标；12306 车站字典里另有约 0.7% 的老挝段、香港西九龙等缺失。
- 12306 接口有频控，短时间大量查询会 403；服务端有节流，仍建议不要连续狂点。
- 只能查「当天开行」的车次，非当日日期会查不到经停站。

## 后续可以做的

1. **补齐真实走向的缺口**：`railosm.py` 已接上（见上文「地图上的两种路线」），剩下的是把缺口填掉 ——
   补对应线名的几何是首选：**在整理台确认这条线并合并保存走向**（`ld_geom`）最省事，
   `confirmed.geom()` 会优先用它；往 `geoms.json.gz` 里加一条线是老办法，只作兜底。
2. **正晚点**：如能稳定拿到 12306 正晚点数据，位置可按实际晚点分钟偏移。
3. **批量预热**：用 `collect_routes.py` 的产出直接导入，网页端即可离线全量查询（不依赖网络）。
4. 打包成 exe（PyInstaller），双击即用，不要求装 Python。
---

## 归档

- 压缩前完整版：[`docs/archive/webapp-README-20261106.md`](../docs/archive/webapp-README-20261106.md)
- 全部旧版文档与记忆日志打包：[`docs/archive/railcn-docs-20261106.zip`](../docs/archive/railcn-docs-20261106.zip)
