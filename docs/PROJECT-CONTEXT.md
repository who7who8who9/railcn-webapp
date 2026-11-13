# railcn 项目上下文（压缩版）

> 给接手的自己 / 新会话看的一份「工作底稿」。功能细节看 `README.md` 与 `webapp/README.md`，
> 这里只留：**怎么跑、数据长什么样、哪些规则不能改、踩过什么坑、还剩什么没做**。

---

## 1. 这是什么

本地铁路查询网页：查车次 / 经停站 / 余票、按日期批量采集、看历史数据、地图上画走向，
外加一个「数据校对」页把线路 / 站点 / 经由 / 里程 / 坐标的人工修正沉淀成正式库，
和一个独立的「线路整理台」（`/linedesk.html`）把六套来源的线名归并成人能看懂的规范线路。

- 纯标准库后端（`ThreadingHTTPServer` + SQLite），前端单页（`index.html` + `app.js` + `style.css`），无框架无构建。
- 数据源：12306（车次 / 经停 / 余票）、`cnrail_data_v2.xlsx`（车站 / 线路 / 站序底表）、
  辉睿线路资料库（844 条线路参数与逐站里程）、china-emu（660 条线路的逐站 K 里程）、
  OSM（线路几何 + 第六套站序来源：1727 条线路 / 10340 个国铁车站，见业务规则第 19 条）。
- 本地库：`webapp/data/rail.db`（约 160 MB，不入库）；
  整理库 `webapp/data/linedesk.db`（约 2 MB，**入库**，是人工整理的成果）。
- 主站「车站/线路」页的**线路**查的是整理库里「完成全部确认」的线路（2026-11-06 用户定的口径），
  不是 rail.db 里那几千条线路 / 来源版本；见业务规则第 31 条。车站模式、数据校对、
  列车位置地图、备份面板仍按各自老口径取数。

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

## 3. 目录（2026-11-06 更新）

```
railcn/
├─ data/            cnrail JSON、辉睿资料库、chinaemu、osm、crtrain 几何
├─ scripts/         数据导入 / 归一 / 合并 + 58 个回归脚本
├─ tools/           Excel → JSON、坐标反解等一次性工具
├─ docs/            README 截图、本文件、PORTABLE.md、旧版归档 archive/
└─ webapp/
   ├─ server.py     全部 HTTP 路由
   ├─ 17 个业务模块 railcore / cnrail / curate / collector / historyq / routestore /
   │                linedesk / confirmed / linebase / linegroup / lineseg / railosm /
   │                railgeom / geoimport / backup_lines / trainmap / traintrack
   ├─ data/         rail.db（约 160 MB，不入库）、linedesk.db（约 2 MB，入库）
   └─ static/       index / linedesk / trainmap / backup 四个页面
```

## 4. 数据模型（`webapp/data/rail.db`）

| 表 | 行数量级 | 说明 |
|---|---|---|
| `trains` | 47 万 | 车次清单（车次号 + 日期 + 起终站 + 车型），主键 `(code, date)`：31 天各一份，同一天不会重复 |
| **`train_universe`** | 1.78 万 | **车次全集物化表**（2026-11-05 建）：每个车次号一行。`/api/dates`、待处理、连跑都读它，**别再去 `GROUP BY trains`**（47 万行扫一遍 0.2 秒 ×15 天 = 10 秒） |
| `route_tpl` / `route_day` | 1.3 万 / 25.8 万 | **经停站按趟只存一份 + 每天一条指针**；`routes` 是两者 join 的视图（省 5 倍空间） |
| `day_collect` / `day_skip` | 22 / 7 万 | 每天采集状态 / 「最近一次查验没拿到经停站」（**带 `expire`+`hits` 复核期限，不是永久结论**，见第 5 节第 30 条） |
| **`webapp/trainmap.py`** | 内存索引 | **不是表**：全路网列车位置索引（1.26 万时刻模板 + 7.6 万开行实例），服务启动后台预热 4~8 秒；快照按分钟缓存 60 秒。见第 5 节第 29 条 |
| **`webapp/traintrack.py`** | 内存索引 | **不是表**：行车路线走向索引（3686 条折线 / 47.8 万点，构建 2 秒，启动后台预热），把 `line_base`/`line_geom`/`line_route` 的连续走向按站间区间贴到车次上，其余退回直线。见第 5 节第 29 条 |
| `line_catalog` / `line_catalog_stop` | 0 / 0 | 辉睿线路资料库（**2026-11-02 也清空了**，见第 5 节第 22 条；原来 844 / 1 万） |
| `line_custom` / `station_custom` | 2647 / 4496 | 用户自建线路 / 车站。**2026-11-02 清空过两次，当天下午把备份全导回来了**（2646 条，名字带来源后缀），见第 5 节第 22 条 |
| **`station_master`** | 18763 | **多源车站合并主表**（2026-11-05 建，见第 5 节第 27 条）：干净站名 + WGS84 坐标 + 属性 + `sources` / `aliases` / `note`（**所有来源只在备注里**）。由 `scripts/merge_station_sources.py` 整表重建 |
| `line_default` | 0 | **同一条线的默认版本**（`name` 基础线名 → `pick` 版本线名，如 `京沪线` → `京沪线（底表）`），见第 5 节第 23 条 |
| `line_override` | 0 | 档案线路的人工站序 / 人工里程覆盖（原始档案不动） |
| `line_geom` | 2 | GeoJSON 导入的走向：`parts_gz`（原始碎段）+ `spine_gz`（串好的连续走向，gzip 后的 JSON） |
| `coord_override` | 14 | 人工坐标（优先于档案） |
| **`train_override`** | 21 | **人工经由的正式库**，见下 |
| `curate` | 12722 | 校对清单（每趟车一行摘要，扫描生成） |
| `via_class` | 2 | **已废弃**（「同类沿用」已删），可删表 |

### 另一个库：`webapp/data/linedesk.db`（整理库，入库）

线名整理是独立的一件事，另开库，**rail.db 一个字节都不碰**：

| 表 | 说明 |
|---|---|
| `ld_line` | 规范线路：`name` 人工定的规范名、`family` 归一键（`前缀|族`）、`kind`、`status`(draft/confirmed)、`total_km`、起讫、`src_main`、**`src_line`/`src_stop`/`line_ok`/`stop_ok`**（2026-11-04 加：确认的线路来源 / 站点来源 / 各自是否确认）、**`geom_src`/`geom_name`**（2026-11-05 加：合并保存定下的走向，`route:hdx` 这种） |
| `ld_alias` | 每个来源的原始写法，`UNIQUE(source,name)`——一个来源线名只能归一条线；**`ok`** 标记它是否被确认为线路来源 |
| `ld_stop` | 整理后的站序：站名 / 里程 / 经纬度 / 这站取自哪个来源 |
| `ld_relation` | 分段 ↔ 全线的父子（`kind='seg'`），带成环检查 |
| `ld_log` | 改动流水 |
| `ld_geom` | 整理台的走向：`parts_gz` 原始多段 + `spine_gz` 主轴（合并保存 / 导入 GeoJSON 都写这里）+ `line_id` / `filename` / 里程 / props，**不写 rail.db** |
| `ld_ref` | 资料库（辉睿）844 条介绍：中文名称（**正式名称口径**）/ 全称 / 类型 / 等级 / 起止站点 / 长度 / 速度 / 开通日期 / 别名 / 外文名 / 正文 / 原文链接 / 站序 |

初稿由 `scripts/build_linedesk.py` 从五套来源机器归并（`status='draft'`，在 `/linedesk.html` 里人工确认）。
重建是幂等的：只补新出现的线名，已整理过的不动，`--force` 才清空。
2026-11-04 之后流程变了：**来源默认空、人工收 + 分别确认 → 合并成确认线路 → 定为基本线路**（第 5 节 25/32 条）。
**2026-11-06 再变（最终口径，第 5 节 32 条）**：基本信息正式保存 → ① 只存走向 → ② 只存站点
→ 站序（可反向）→ **③ 合并保存即定为基本线路，④ 独立步骤与其走向下拉一起取消**。

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
## 5. 业务规则（48 条硬约束，改代码前先看）

> 每条都是被用户反复确认过、或踩坑换来的。压缩前的完整论证、数字与案例在
> `docs/archive/PROJECT-CONTEXT-20261106.md`；AI 速查版在 `.workbuddy/memory/MEMORY.md`。

### 经由 / 校正 / 里程

1. **经由只有两种**：`train_override`（正式库）和当场按线路库算的 `viaSource='auto'`（不算数据）。
   自动结果必须人工「确认自动结果并入库」。
2. **`via_class`「同类沿用」已彻底删除，别恢复**。替代：列同起讫点车次 + 停站详情 → 人工勾选 → 保存时逐趟同步。
3. 同步入库也算「已校正」：「已校正 N / 其中同步 M」，M 是 N 的子集，不相加。
4. 同步先看「停站是否全在经由里」（`via_covers_stops()`）：全在原样照抄，否则按对方停站重新落站；
   只同步覆盖全程的，对方已有校正默认跳过。
5. 已校正 / 已同步的车次不再进 `od_mates()` 候选，只报 `hidden` 趟数。
6. 校正绑定**车次号**，`based_date` 记依据哪天站表；站表变了在编辑器提示「站表已变」。
7. 里程三档 `real` / `hybrid` / `estimate`，界面必须标明。
8. 车次合并：同一趟车中途换号（`4355/8`）算一趟，`codes[]` + `codeLabel` + `codeSwitches`，每站带 `trainCode`。
9. 坐标：底表经纬度是仿射变换假值，已用 OSM 稳健回归反解回填；人工值优先于档案。
10. **站名一律不带「站」后缀**（`杭州东` / `杭州东站` 同一站），但只剥「剥完的裸名本身认得」的，
    「十八站」「九站」「新九站」「同名站」原样保留；只并改名造成的相邻重复。
11. **不做全量路网 A\***：项目只用「线路 + 车站」两类数据。
12. 经由选站要能选到线路所 / 乘降所 / 中继站（不是客运站但经由常用）。
13. 站序按 `train_no` 取，不按车次号；`routes` 是 `route_day ⋈ route_tpl` 的视图。
14. **站序与走向互补**（`lineseg.py`）只补不合并：分段线借父线几何裁出本段，只有走向的线名从站序补站序。
15. 资料库 ↔ 线路库对应**只认线名**，站序重叠只用来复核、不用来新增。
16. china-emu（`line_ce` / `line_ce_stop`）是第五套来源，补逐站 K 里程 + 站序兜底。
17. **线名整理走独立库 `linedesk.db`，别往 rail.db 里塞**；初稿 `status='draft'` 必须人工确认，不做自动回灌。
18. 库外的线（只有资料库 / china-emu 有站序）也能改站序，保存时**自动收进线路库**，别先报错。
19. **OSM 是第六套来源**：站序兜底 + 车站归属，界面上和前五套平起平坐（不替用户挑），
    优先级排最后、**不参加长度裁判**。
20. **站序默认来源是用户勾的**（`line_src_pick`），一键倒排也是他点的，系统别替他改口径。
    倒排只翻指定来源，里程 = 原末站里程 − 原里程、新起点 0、缺的不插值。
21. 数据校对的线路**只认整理台「全部确认」的线路**（2026-11-11 用户定，推翻 11-03 的
    「库外有站序的线也要能选」）：`/api/curate/lineNames`、`/api/curate/cands`、
    缺口候选、自动识别、`/api/line/stops?confirmed=1` 全部走 `confvia.py`；
    来源版本（京广线（OSM）/（底表）/（自建）…）和没确认的线（京哈线、沈大线）
    **一律不出现**，自动识别认不出就是缺口，等整理台确认那条线自己会出现。
    确认线路的站序读 `linedesk.ld_stop`（京广线 212 站），**不是线路库同名版本
    （193 站 / 4221 km）**；名单实时跟着整理台变（2 秒缓存），别写死。
22. **线路可以整体清空，再从 GeoJSON / 备份人工重建**（2026-11-02 一天改了两次口径）。
    2026-11-02 下午已批量导回 2646 条，**现在库不是空的，再要「清空」必须先问**；
    `scripts/reset_lines.py --restore <dir>` 可回滚。导入走 `raw` 通道（五处放行），站名一字不改。
23. **来源版本归组**：只认「去掉（来源）后缀后同名」，`effective()` = 基本线路 > 默认 > 底表 > 站数最多；
    指纹缓存 2 秒，别改成手动 invalidate；联想每份一行。
24. **基本线路 = 整理台定稿 → 全项目接管**：存 `line_base` + `line_base_stop`，不写 `line_custom`；
    `/api/line` 给 `base=true`、`/api/line/stops` 给 `from=base`。
    **注意：基本线路 ≠ 确认线路** —— 校对下拉 2026-11-11 起只列整理台全部确认的
    那些（见第 21 条），基本线路没走确认流程就不出现在校对里，但 /api/line 照旧接管。
    编辑里程三处必须都按 base 走；`_apply_line_overrides()` 跳过 base 线名。
25. 整理台来源拆「线路来源」+「站点来源」各自确认（**已被第 32 条最终流程取代**）；
    `src_line` 兼容老多选（`|` 取第一条）。
26. **慧睿资料库（辉睿）介绍是「正式名称」的唯一口径**：`ld_ref.name` 当规范名，
    别名匹配必须整串相等；改名两步避免撞唯一约束。
27. **车站库合并：所有来源只进备注，名称不带来源**（`station_master`），
    `station_custom` 只补空缺、`station_coords.json` 只 setdefault。
28. **线路走向合并 → 临时基本线路**（`scripts/merge_line_geoms.py`）：同名 ≥2 源合并，
    `note` 以「合并」开头，落 `line_route` / `line_geom` / `line_base`；`--refresh-base` 只覆盖临时行，
    人工定稿绝不动。新来源只接 5 处，**`STOP_SRC_ORDER` 别加**。几何里程含复线重复、不是营业里程。
29. **全路网列车位置地图 `/trainmap.html`**：库里没有 GPS / 正晚点 / position 表，是**时刻表推算**，
    顶栏常驻「非 GPS」；`train_universe` × `route_tpl` × `route_day` 内存索引，
    跨午夜 `dep<arr` 补 1440；启动 `_warm()` 后台预热，`ensure()` 用 `build()` 别 force；
    缓存键必须用原始 `types` 参数。「推定开行」只在结果里标，不回写库。
    **地图合规**：只用腾讯地图，Key 走 localStorage，没 Key 自绘 Canvas 降级不画国界。
    浮层点不动是层叠上下文（`.tm-stage{isolation:isolate}` + `z-index:0`）。
    **贴线**（`traintrack.py`）：`line_base` > `line_geom` > `line_route` 的连续走向贴红线，
    其余停站间直线；四道闸宁可少贴别贴错。
30. **「当天不开行」不是永久结论**：`day_skip` = 最近一次查验没拿到经停站，带 `expire` + `hits`；
    空结果二次确认、拿到经停站即删；`_release_stale_skips()` **200 条/天/轮**，别改成到期全放行。
    下载/历史页性能见第 8 节（`train_universe`，别再 `GROUP BY trains`）。
31. **主站「车站/线路」只认整理台「完成全部确认」的线路**：`webapp/confirmed.py` 只读 `linedesk.db`，
    判据 `confirmed` + `line_ok` + `stop_ok` + ≥2 站带定位 + 有走向；**显式 `confirmed=1` 通道**，
    别改成全局过滤，未确认线给 `200 + found=false` 别报 404；前端 fire-and-forget、隐藏来源条 / ★，
    保留「编辑里程」。站数别写死。
32. **整理台最终流程（2026-11-06，盖过 24/25 里 ④ 的描述）**：四步变三步 ——
    基本信息正式落库 → ① 只存走向（不碰 `ld_stop`/`ld_geom`/基本信息）→
    ② 只存站点（不写 `ld_geom`）→ 站序（可反向、倒排重排里程）→
    **③ 合并 ①② 并自动定为基本线路**；**④ 及其走向下拉全部取消**，走向只由 ① 决定。
    GeoJSON `only='stops'|'geom'` 两入口互斥。publish 放在 `/api/ld/finalize` **HTTP 编排层**，
    **别塞进 `linedesk.finalize()`**（临时库回归会往正式 `line_base` 写脏数据）。
    `publish()` 同名同 `ld_id` 自动 `replaceId`；`finalize()` 拦「规范名带来源后缀」。
    站序反向：`原末站里程 − 原里程`、新起点 0、缺的不插值，坐标 / `total_km` / `stop_ok` 不变。
    **走向点序契约（2026-11-06 修）**：所有折线点一律 `[lon,lat]`。`geoimport.assemble()`
    的 chain 是 `(lat,lon)`，`line_route.spine_gz` 是 `[lon,lat]`，③ 搬来源包走向时
    `_save_geom()` 原来按 `for lat,lon in chain` 解包，把 spine 写反了 —— 主站把纬度
    当经度，地图左上角多出一条竖线、站点挤在右下角。现在 `_norm_chain()` 按**整段
    多数点**判一次并整体交换（不逐点翻，否则撕出假跳变），`merged_geom()` /
    `desk_geom()` 优先 `parts_gz`（spine 只兜底）且读取时自动纠正历史反序数据。
    回归 `test_geom_axis.py`。
    - **① 不得抹掉 ③ 定下的 `route:rg` 走向标记**（2026-11-06 实测 bug）：旧 `set_line_sources()`
      无条件 `UPDATE ... SET geom_src='', geom_name=''`，用户只要再点一次①确认，③ 合并保存定下的
      `route:rg` 走向就被清掉、主站退回按线名找 OSM 几何。现在只清 `geom_src='geojson'`
      （①「导入走向」残留），`route:rg` / `route:hdx` / `route:cnrail` 一律保留。
    - **②「用它并确认」要支持「确认本线当前站序」**（2026-11-06 实测 bug）：旧 `confirm_stop_source(fill=True)`
      永远把来源站序灌回，用户在站序里删/改一站后点「用它并确认」会把来源 29 站重新覆盖回来，
      等于没删。现在前端默认选中「本线当前站序」（删改后 `stops_edited=1` 强制 self），
      `confirm_stop_source(..., fill=False, self=True)` 只把 `stop_ok` 置 1、不动站序；
      只有选了外部来源行才 `fill=True` 覆盖。`ld_line` 新增 `stops_edited` 列记录人工改过站序
      （跨刷新保持，避免自动回到来源选中态）。
    - **① 来源支持「无站点但有走向」的来源**（2026-11-06）：`collect_sources()` 给每条来源加
      `has_geom` / `km` / `nPart`；`search_source()` 的 `rg/hdx/cnrail` 里 `n_stop=0` 但 `parts_gz`
      有几何的线也能选中、预览走向、确认保存，③ 能取到走向。回归 `test_ld_stop_confirm.py`。

33. **走向编辑器 `/osmedit.html`（2026-11-06 增「新建线路」）**：`webapp/osmedit.py` +
    `GET /api/osmedit/lines|geom|stations`、`POST /api/osmedit/geom|stations|new`。
    - 载入已有线路：候选 = `line_geom_edit`(草稿) > `ld_geom` > `line_geom` > OSM，**按里程取大**；
      `ld_geom` 串出来的里程达到库里 `total_km` 的 60% 就短路，不再去串原始碎段。
    - **串线是 O(段数²)**：`railgeom.chain_parts` 每接一段扫一遍剩下的段，陇海线 line_geom
      5000+ 段 = 1400 万次距离计算 / 60s。`_chain_to()` 两道闸门：>800 段只留最长的 800 段、
      总点数 >1400 就按容差 RDP 抽稀后再串（形状误差对「可拖拽骨架」无感）。别把闸门去掉。
    - **抓站不串线**：距离用这条线的**原始碎段**建 0.02° 网格索引 (`_SegIdx`) 直接量，
      顺序/里程用当前折线投 (`_ChainIdx`)。实测 0.02~0.25s（原来 16s）。
      站库候选只取「有正经来源」的国铁站（`_STA_TRUST`）—— 只有 cngeo/coordjson/master/osmhdx
      的那 6000 条是 OSM HDX 抽的，含大量地铁/市域站（崇文门、磁器口），会污染站序；
      命中 <2 个时才放开全量兜底。
    - **建线走唯一通道**：`create_line()` 把「画的走向 + 选中的站」组装成最小 GeoJSON 调
      `geoimport.import_line`，**不另开写库路径**（raw 通道、以文件为准算里程、站名不归一）。
      同名线拦下来（自建线可 `replace=True` 覆盖），建完同步写一份 `line_geom_edit` 草稿。
    - 前端：`S.isNew` / `S.cand` / `S.sel`；新建时地图点击**按点序追加**（不按最近线段插），
      `insertPoint()` 的 `pushUndo()` 必须在改之前（原来在改之后，撤销等于没动）。
    - **列表只收「真有走向」的线（2026-11-09 用户：只关心走向、不关心站点）**：
      `list_lines()` 从 `line_geom` / `line_route` / `ld_geom` / `line_geom_edit` /
      `railgeom.search("", limit=1000000)` 全量去重（当前 **4510 条**）。
      **别再把 `ld_line` 五千多条草稿整表灌进来**（多数没走向，排序后 `[:60]`
      会把有几何的线挤掉），也别只取 OSM `search()` 的前 60 条（漏 200 多条 OSM 独有名字）。
      `offset` 分页 + `count/total/hasMore`，索引缓存 20 秒，写草稿 / 建线 / 删线调
      `_index_invalidate()`。`line_base` / `ld_line` 的 `geom_name != name` 生成
      **借走向别名条目**（济南线 ← 京沪四线），`load_geom()` 另有一层 `geom_name` 回退；
      前端用 `loadName` 载入、`save(target=)` 把应用写回被借的那份几何（草稿仍存显示名）。
    - **点编辑不依赖底图**：没 Key 的离线自绘 Canvas 以前被标「只读」并禁用加点，
      删点只有腾讯 marker 的右键。现在 Canvas 也绑 click / mousedown / mousemove /
      contextmenu —— 点空白处加点、拖顶点、右键删点；再加显式「删除选中点」按钮 +
      Delete/Backspace 键，选中点变蓝（腾讯 `vsel` style / canvas 高亮），
      折线至少留 2 点，Ctrl+Z 撤销。`showStops` 默认 false，站点参考点要勾选才画。
    - **apply 写回曾必报错**：`_apply_to_library()` 的 UPDATE 写了 `updated_at`，
      但 `ld_geom` / `line_geom` **都没有这一列**；已删掉，回归补「目标几何存在 → 真被更新」。
    - 回归 `scripts/test_osmedit.py` **46 项**（含索引覆盖 / 分页 / 无走向草稿不进列表 /
      借走向回退 / **多段 parts 原样保留** / apply+target / 前端静态），另 headless Chrome 端到端 20 项（离线模式加删点）。多段载入口径见第 40 条。

34. **车站多坐标：列出全部让人确认，别只给一个生效值**（2026-11-09 用户要求）：
    `station_master` 是 17 套来源合成的一个实体，以前只在 `note` 里留「同名 N 处坐标，
    最远相距 X km」。现在逐源坐标落 `rail.db / station_coord_cand`（`webapp/stationcoords.py`，
    `python webapp/stationcoords.py --force` 幂等重建），`/api/station` 附 `coordCandidates`，
    独立接口 `GET /api/station/coords?q=`。
    - 来源 = `merge_station_sources.load_sources()` 的 10 套 WGS84 + `stations_all_sources.csv`
      六源（补 gtfs / panorama / trainvis / station-database 的坐标）；归一键同 `station_master.key`。
    - **坐标系备注说的是原始仓库，别按标签盲换**：实测 panorama 标 GCJ-02 但 97% 已是 WGS84、
      TrainVis 标 BD-09 实为 GCJ-02（GCJ→WGS 后中位偏差 584m→80m）。`coordcrs.calibrate()`
      拿同名站的**全部** WGS84 基准点，试「原样 / GCJ→WGS / BD→WGS」取最近者；
      三种都离基准 >2 km = 同名不同地点，原样保留。原始值 + 坐标系都留在表里。
    - 查询时 60 m 内并作「一处」，列出认同它的来源、距当前生效坐标、是否当前；
      「用这个」走**既有** `coord_override`（`set_coord(..., src=)` 记确认来源），
      `/api/coord/clear` 撤销 —— 不另开写库口径。`coord_override` 新增 `src`/`picked_at` 列。
    - 前端 `app.js` 的 `stCoordList()` / `bindCoordCandidates()`；主站卡片与 OSM 独有卡片共用。
      原「补坐标」手填入口不变；人工坐标时多一个「撤销人工」。
    - 回归 `scripts/test_station_coords.py`（31 项）。

35. **确认线路「规范名 ≠ 走向来源名」时，走向折线也要能取到**（2026-11-09 济南线）：
    整理台 ① 选的是 OSM 线名（`src_line='osm:京沪四线'`），③ 的 `finalize()` 在
    `osm` 分支**只记** `geom_src='osm'` / `geom_name='京沪四线'`，不写 `ld_geom`
    （`route:rg` 那几家才把原始多段搬进 `ld_geom`）。于是主站详情的 `d.osm.ask`
    是来源名，前端原来把它原样传给 `/api/line/osm?confirmed=1`，而该通道只按
    **确认线路规范名**查白名单 → 404 → 地图只剩站点直连，看着就是「没有 OSM 走向」。
    现在两处一起兜：① `app.js / lnLoadGeom()` 确认视图一律用 `LN.data.name`
    （规范名）请求，来源名只留给「真实走向」小卡展示；② `confirmed.geom()` 按规范名
    查不到时按 `geom_name` 反查一次（**规范名精确匹配永远优先**，多条共用来源名按名字
    排序取第一条，不放宽白名单 —— `京广线` 仍取不到）。`geimport.meta` 之外的数据
    不用补：实测 `/api/line/osm?q=济南线|京沪四线&confirmed=1` 都是 200 / 5 段 72 点。
    改名收编的线都覆盖（济南线 ← 京沪四线、京沪高速线 ← 京沪高铁）。
    回归 `scripts/test_confirmed_lines.py` **90 项**（[3b] 规范名/来源名两份折线同点数
    + HTTP 按来源名取折线 + 前端静态检查）。
    **待定**：要不要让 ③ 的 `osm` 分支也把 OSM 走向固化进 `ld_geom`（现在是运行时按名解析）。

36. **整理台「新增车站」要能指定插入位置**（2026-11-09 用户要求）：旧 `#ldAddStop` 用
    `prompt()` 只收一个站名、一律 `push` 到末尾，想插中间只能连点 ↑；站序行只有 ↑↓✕，
    帮助里写的「+ 加站」其实没有这个按钮。现在 `POST /api/ld/stops/insert`
    → `linedesk.insert_stops(lid, names, pos)`：**`pos` 是插入后的 0-based 起始下标**
    （0 = 最前，`i+1` = 插在第 `i+1` 站之后，`-1` / ≥n = 末尾），越界夹到 `[0, n]` 不报错。
    - 一次可插多站：数组或按换行 / 逗号 / 顿号 / 分号切的字符串，按填写顺序连续插入；
      数组入参不二次切分（站名自带顿号要保留）。
    - **新站 `km` 留空、不插值**（口径同第 32 条反向站序），原有各站 `km` / 坐标 /
      `src` / `note` 一个字不动；坐标按站名查档案补，查不到留空。
    - 落库**复用 `save_stops()`**（别自己写 INSERT）：顺带重排 `seq`、更新起讫、
      `stop_ok=0` / `stops_edited=1`，和手工「↑↓ / 改名 / 删站」完全同口径。
    - 前端：站序每行 `+`（`data-ins`）预选「插在它后面」，顶部「+ 站（可指定位置）」
      弹窗可选「最前面 / 第 N 站之后（末尾）」，插完前端设 `S.pick.stop={self:true}`，
      ② 不会自动选回来源行。**别改回 `prompt()` 一律追加**。
    - 回归 `scripts/test_ld_insert_stop.py` **48 项**（临时库）+ headless Chrome
      隔离服务 E2E 20 项（另起临时 `linedesk.db` + 临时端口，不碰正式库 / 8686）。

37. **「车站/线路」线路模式下方常驻全量确认清单，按确认先后倒序**（2026-11-10 用户要求）：
    用户原话「下面列出所有整理台确认线路，并按确认先后将最新确认的放在上面，
    然后再根据输入线路在其中搜索」。旧行为是空输入时往 `#netList` 塞 `limit=12`
    的 chip、一输字改弹联想下拉 —— 既列不全，也不是「在其中搜索」。
    - **确认时间只能取 `ld_log` 的 `finalize` / `publish`（最后一次）**，
      **别用 `ld_line.updated_at`**：改站序 / 补坐标都会动它。没有日志的老数据
      才回退 `updated_at`（`confirmFrom='line'`）。同一秒确认按日志 id、再按
      `ld_line.id` 定先后；日志 `target` 是规范线名或 `ld_line.id` 字符串都要认。
    - `confirmed.recent()` 给**全量不截断**的清单，带 `confirmedAt` / `confirmSeq` /
      `covers`（`ld_alias ok=1`）/ 站数 / 定位 / 走向点数 / 里程 / 起讫；
      `GET /api/line/confirmed?full=1` 附 `items`，**不带 full 仍是纯统计数**。
    - `_fp()` 指纹纳入 `ld_log` 的 `COUNT/MAX(id)` 与 `ld_alias` 计数：整理台 ③
      刚确认完，回主站窗口 focus 强制刷新就能立刻排到最上，不用等 30s 缓存。
    - 前端 `#netConfirmedList` 是**独立容器**（在 `#netList` / `#netDetail` 之前），
      `showLine()` 清空 `#netList` 时不会把清单抹掉；输入时 `cfHit()` 按线名 +
      别名 + `geomName` **本地筛选**（不打接口、不弹下拉），名次用全量名次不重排。
      可收起 / 展开，当前打开的线高亮，车站模式隐藏，切模式清空输入直接列全量。
      **别改回「chip 列表 + 联想下拉」两套并列**，也别把清单塞进 `#netList`。
    - 回归 `scripts/test_confirmed_list.py` **43 项**（正式库全量 / 倒序 +
      临时库日志优先 / 回退 / 同秒定序 / target=id / 别名 / 缓存失效 + HTTP +
      前端静态）+ headless Chrome E2E 28 项（只读 8686）。

38. **「车站/线路」编辑里程必须占首屏，别被常驻清单 + 站序顶到下面**
    （2026-11-10 用户原话「编辑里程时页面帮我调整一下，现在都在下面太挤了」）：
    - 旧版实测：清单 360px + `dhead` 163 + `lnInfo` 127 → 详情可视只剩 291px；
      `#lnKmBox` 550px + 站序 1268px，编辑区 `top=899`（窗口 900）**整块在首屏外**，
      详情页要滚 2108px。改后 `top=429`、`bottom=876`、详情滚动内容 719px。
    - 编辑态统一由 `lnWide(on)` + `lnRenderEditor()` 给 `#paneNet` 加 `km-editing`
      （幂等，切 tab / 换线 / 取消 / 保存都会还原）：CSS 把 `#netConfirmedList`
      折成表头（隐藏 `.cf-row` / `#cfToggle`、表头文案改「编辑里程中已收起」）、
      `#lnStops` 隐藏。**退出编辑移除 class 即自动还原**，别在 JS 里手动删清单 DOM、
      别改 `NET.cfOpen`（用户自己收起 / 展开的状态要留着）。
    - `#lnKmBox` 必须紧跟 `#lnHead`（别排回 `lnInfo` / `lnOsmOther` 后面）；
      内部顺序：工具条 `.lnkm-bar`（sticky）→ 总里程行 → `#lnSegs` → 折叠说明
      `#lnKmHelp`（`LN.kmHelp` 切段距 / 累计后保留）。段距列表按视口
      `min(38vh,360px)` 自适应 + 矮屏 `30vh`，**别写死 296px**。
    - 只改版面：`#lnSave` / `#lnResetAll` / `#lnCancel` / `#lnSum` / `#lnWarn` /
      `#lnMsg` / `#lnTotal` / `#lnTotalClr` / `#lnSegs` / `#lnMode` 一个不能少，
      `lnSave()` / `lnResetAll()` / `lnRefreshPreview()` 等里程算法不碰。
    - 回归 `scripts/test_km_editor_layout.py` **52 项** + headless Chrome E2E
      **39 项**（只读 8686，输入后走「取消」，不写库）。

39. **走向编辑器里整理台已确认线路：红名置顶、可改、保存后仍是确认线路**
    （2026-11-10 用户原话「将整理台已确认线路放入可以进行修改，并且保存后还是
    作为整理台已确认线路，这些线路显示时线路名称标红」）：
    - **列表**：`_geom_index()` 基础索引（只收真有几何的线 / 测试名沉底，4513 条）
      不带确认标记；`list_lines()` 每次现算 `confirmed.recent()` → `_confirmed_rows()`
      / `_merge_confirmed()` 打 `confirmed` / `ldId` / `geomName` / `confirmedAt`，
      同名行复用 km+src 但**去掉 `alias` 标签**、`loadName` 改规范名、`alias=False`；
      没有同名几何行的确认线（③ 的 `osm` 分支）整行新增。排序
      `测试名沉底 → 确认优先 → 里程 → 名称`；缓存签名 `(条数, 首线名, 确认时间)`，
      ③ 刚确认完立刻生效，不用等 20s。**别把 `ld_line` 五千条草稿灌回列表**。
    - **载入**：`_confirmed_info(name)` = `confirmed.get(name)`，只认完成确认白名单。
      草稿 `line_geom_edit` 仍优先；否则一律 `lineosm.geom(name, confirmed_only=True)`
      → `confirmed.geom()`（含 ③ 的 `osm` 分支按 `geom_name` 解析 OSM），拿到的
      就是主站正在画的那份**多段 parts**。旧版优先读 `spine_gz` 连续主轴 /
      `_chain_to()` 硬串单链已废弃，原因见第 40 条。`loadName` 恒为规范名，
      草稿也按规范名读写，别退回「存济南线、载入京沪四线」。
    - **保存**：`save_geom()` 自动识别确认线路（前端 `confirmed` 只是声明，后端以
      `confirmed.get()` 为准），`apply=1` 调 `_apply_confirmed(ld_id, …)`：更新
      `ld_geom` 里 `merged_geom()` 会命中的那行（`line_id` + `created_at DESC`）
      + 规范名同名行；一条都没有就 INSERT（`created_at` 保证比同 `line_id` 已有
      记录新，主站必命中）。同时更新同名 `line_geom`，`confirmed.invalidate()`
      刷新清单。**绝不写 `ld_line`**：status / line_ok / stop_ok / geom_src /
      geom_name / updated_at / 站序一个不动 → 保存完仍是确认线路、确认先后不变。
      只更新 `rail.db/line_geom` 对 ③ 的 `osm` 分支**无效**（主站读 `merged_geom`），
      必须写 `ld_geom`。`apply=0` 只存草稿、不写回；非确认线保持旧的双轨口径。
    - **前端**：`.oe-item.confirmed .nm{color:#c5221f}` + 「已确认」红标签 +
      `#oeCount` 显示确认条数；`S.confirmed/geomName/ldId`，`selectLine(item)` 传整行；
      确认态下 `oeKind` / `oeStat` / `oeSaveHint` / 应用确认框都写明「仍是已确认
      线路」与「载入主站那份多段走向、段间不补假直线」；`save()` 的 `target` 用规范名；
      `startNew()` 清确认态。
    - 回归 `scripts/test_osmedit_confirmed.py` **61 项**（正式库索引 / 21 条全载入 /
      载入段数与主站一致 / 临时库写回且 `ld_line` 逐字段比对 / 静态 / HTTP）
      + 隔离临时库 + 临时端口的 headless Chrome E2E **26 项**（不碰正式库和 8686）。
      `test_osmedit.py` 的济南线断言已改为确认线路口径；抓站 / 建线样本改用
      `_cand_ldgeom()` 原始碎段串线（多段载入后首段不覆盖全线）。

40. **走向编辑器载入的走向必须和主站画的是同一份多段几何**（2026-11-10 用户
    反馈「调用现有线路出来的路线都很奇怪，和正常展示的不一样」）：
    - **根因**：编辑器以前优先读 `ld_geom.spine_gz`（抽稀 + 补断口主轴，
      `merged_geom()` 注释早就警告「画地图会失真」），还把主站的 `parts` 多段
      用 `_chain_to()` 硬串成一条 chain、再抽稀到 200 点。实测滨绥线 902 km
      只剩 13 km、滨北线 445 km 只剩 14 km、京广高铁 4137 km 变 2064 km、
      沪昆线冒出 274 km 假直线。
    - **公共入口**：新建 `webapp/lineosm.py`，把 `server.py /api/line/osm` 的解析
      顺序原样抽出（`confirmed.geom` → `linebase.get(ld_id)`/`merged_geom` →
      `route_geom` → `geoimport` → `railgeom` → `lineseg.borrow_geometry`），
      `server.py` 与 `osmedit.load_geom()` 都调它，**两边永远同一份 parts**。
      别再在编辑器里另写一套「按里程挑最完整」的候选逻辑。
    - **载入**：`_as_segs()` 把各来源几何统一成多段（顺带纠正历史 `[lat,lon]`），
      `_thin_segs()` 逐段 RDP（target = max(n, 2×段数)、上限 3000；每段至少
      首尾两点，长段按 25 km/点加预算），`_cap_jumps()` 把段内 >25 km 的空隙
      从原点补回中间点；`totalKm` = 各段累加，**不含跨段直线**。返回 `nPart` /
      `nPt`，`source` ∈ `edit` / `confirmed` / `main`。
    - **保存**：`save_geom()` 多段原样写 `parts_gz`（扁平 chain 只作 `spine_gz`
      兜底，`merged_geom` 优先读 `parts_gz`），`n_part` = 段数；`_apply_confirmed()`
      / `_apply_to_library()` 都带 `n_part`。**别把多段压成一条 chain**，否则
      保存后主站又会冒出跨段假直线。
    - **前端**：`S.breaks` 记各段起点下标，`splitParts()` 切段；Canvas 逐段
      `beginPath/stroke`，`routePaths()` 给 `MultiPolyline` 传二维 paths；
      `routeKm()` / `insertPoint()` / `projOnChain()` 跳过断点，`deletePoint()` /
      `pushUndo()` / `undo()` 维护断点；`save()` / 抓站 / 建线都发 `splitParts()`。
      `#oeN` 默认 600（多段留得下），状态条显示段数。
    - 回归 `scripts/test_osmedit_multipart.py` **58 项**（公共入口 / 21 条段数与
      bbox 逐条对齐主站 / 里程差 ≤8% / 无 >40 km 假直线 / 多段写盘读回 / 前端静态
      / HTTP）+ Playwright E2E **16 项**（注入探针读 IIFE 内部状态：京沪线 40 段、
      滨绥线 400 段、插点撤销保住断点、无 JS 异常）。`test_osmedit` 46 项、
      `test_osmedit_confirmed` 61 项、`test_confirmed_lines` 159 项跟着更新。

41. **刷新全集必须搜到「改号切换日」，新字头不能卡白名单**（2026-11-11 用户
    反馈「今天已经开始可以下载 11/25 更新后的 V/N/Z/Y，下载完没有」）：
    - **现象**：库里 11-25 的 `trains` 比 11-24 少 **2038 趟**（K 1262 /
      纯数字 293 / Z 266 / T 174，全是阶段一要换号的普速车），但 V/N 字头
      **一条都没有**。实测 12306：`date=20261111` 搜 `V1`/`N1` 返回 0，
      `date=20261125` 返回 200 / 89 条。
    - **四个根因，一处都别退回**：① `enumerate_trains()` 原来只搜
      `job.date`（连跑起始日 = 今天），而新车次只在切换日开行；
      ② `TRAIN_TYPES` 白名单没有 V/X/B，且 **`N` 被当成「纯数字」代号**，
      `N1..N9` 从来不发；③ `_detect_type()` 用白名单卡，回包里出现新字头
      也会被丢；④ `_save_trains()` 一律写 `job.date`，即使搜到新号也登记
      在今天，`curate` 的 `date >= renum_cutoff` 永远筛不到。
    - **现在**：`enum_dates(date, future=True)` = 起始日 + 预售窗口最后一天；
      `enum_prefixes()` = 12 字母（含 V/N/X/B）×9 + 数字 1..9 = 117 词；
      `refine_prefixes()` 按 `^([A-Za-z]?)(\d+)$` 细分且**细分请求仍用同一日期**；
      `_detect_type()` 收任意字母（纯数字沿用历史写法 `"N"`，只是首字母别名，
      **禁止按它推车种 / 速度**）；每个搜索日期的结果分别 `_save_trains(d, ...)`。
    - **开关**：下载面板 `#dlEnumFuture`（`localStorage.railcn.dlEnumFuture`）
      默认勾，`future=1` 经 `server.py` 透传到 `chain_start/start` →
      `Job.enum_future` → `Chain.enum_future`（`snapshot()` 带 `enumFuture`）；
      `future=0` 只搜起始日（省一半请求、少挨 403），但会漏掉切换日新车次。
      双日期 117×2 + 细分，单日期 117 + 细分。
    - 列车位置地图 `trainmap.js` 的 `TYPES` 加 V/N/Y/X/B **中性**标签，
      `trainmap.py` 的 `known_kinds` 同步扩展（勾「其他车种」不该把它们算进去）。
    - 回归 `scripts/test_renum_enum.py` **56 项**（临时库 + 假搜索接口：前缀 /
      `_detect_type` / 双日期 / 按日期写库 / 细分用同一日期 / `future=0` 旧行为 /
      下载链路能抓到新号且按切换日可筛 / 静态 / HTTP）；`test_chain.py` 第 6 步
      断言改成数据驱动（254 / 127 个请求）、`fake_enum` 接 `future` 参数。
      数据面还要用户自己「勾阶段一 + 重新扫描」，脚本不替他改 `app_cfg`。
42. **车种标签按日期分时代 + 勾阶段自动重扫**（2026-11-11）：新版车次分类见
    `docs/renumber-2026.md` 第 6 节（X 超高速 / G 高铁 / T 高速动车组 / K 高速动车 /
    D 动车 / C 城际 / S 市郊 / Z 直达特快 / N 快速普速 / V 普通快速 / Y 旅游）。
    **同一字母新旧含义不同**（T 旧=特快、K 旧=快速、N 旧=纯数字），**禁止一套标签套两代数据**：
    `historyq._label(t, date)` 按 `date >= '2026-11-25'` 选 `TYPE_LABEL_NEW` / `TYPE_LABEL_OLD`，
    按日统计传区间末日 `dt_`；`trainmap.js` 用 `TYPE_LABELS_OLD/NEW` + `tmIsNewEra()`（按 `S.when`）
    + `relabelTypeChips()`，`renderStat()` 每次刷新重绘筛选按钮文字（`TYPE_KEYS` 只定筛选词、
    `TYPE_OF` 只认字头不推车种）。**「勾阶段一一趟也没有」根因**：`curate` 表是旧扫描结果，
    勾阶段只 `set_renum`+`cuLoad` 重读旧表、不扫描；`cuRenderEra` 保存回调现在自动
    `POST /api/curate/scan` + `cuPoll()`。11-25 的 V/N/Z/Y 经停数据本就在 `routes`（1168 趟），
    `summarize()` 全可算，扫一次即入库。回归 `test_trainclass_labels.py` **36 项**。
43. **添加点的点序：起画阶段一律往后接，第 3 点不许插回前面**（2026-11-11 用户
    「点第 3 个点线直连第 1 点，而不是第 2 点」）：`insertPoint()` 以前只把
    「点数 < 2」当画线，第 3 点就掉进「最近线段插入」分支，而那时只有一段
    （1→2），于是插成 1→3→2，看着像新点连回第 1 点（清空后重画同样中招）。
    现在 `S.drawing` 记「正在按点序画线」：`isNew` / 刚起头（不足 2 点）/
    `drawing` 三者任一都 `push` 到末尾并置 `drawing=true`；`loadGeom()` /
    `selectLine()` / 建线成功一律复位 `drawing=false`，**载入已有走向才按最近
    线段插**。`segDistM()` 改成 `segProj()`（同时给距离 `d` 和投影参数 `t`）：
    最近段是末段且 `t > 0.999`（点落在线尾延长线上）→ 也往后接，否则会插到
    倒数第二点前面画成回折。状态条显示「画线追加 / 插点」两种模式，别删
    （E2E 靠它判模式）。回归 `scripts/test_osmedit_addpt.py` **34 项**（离线
    自绘 Playwright：起画 5 点 `sel` 逐次=点数、里程单调、1 段；载入草稿后仍插入、
    线尾延长线接末尾；清空重画仍追加）。

44. **数据校对只用整理台确认的线路**（2026-11-11 用户：「数据校对中出现的线路全部用
    我已经整理台确认的线路，其他线路不要出现了」）。新增 `webapp/confvia.py` 作为唯一口径：
    · `lineNames` / `cands` / `detail.gapCands` / 自动识别 / `/api/line/stops?confirmed=1`
      全部只给 `confirmed.names()` 里的线；
    · 站序读 `linedesk.ld_stop`，**不是线路库同名版本**（京广线 212 站/2284 km vs 库 193 站/4221 km；
      京广高铁、合福高铁、滨北线、滨绥线线路库里压根没有）；
    · 识别算法：对每对相邻站，先看确认线路是否同时含这两站（站序位置不同），
      排名 `(与上一段同线, -覆盖本趟站数, |站序差|, -线站数)`；认不出 → `unknown` 缺口；
    · 名字归一 `normalize()`：完全同名 → 去「（来源）」后缀同名 → `ld_alias.ok=1` 别名。
    **代价（用户已知并接受）**：确认线路现在 28 条，重扫后 `todo` 828→1127、`via0` 11→424、
    `clean` 340→46 —— 认不出的都是还没确认的线，确认一条就多认一条，**别拿它当 bug 去放宽**。
    回归 `scripts/test_curate_confirmed_scope.py` **33 项**；`test_via_nodes` 已改成守这条新口径
    （老口径「库外线路进下拉」作废，但 `/api/line/stops` 不带 confirmed 时**行为不变**，
    其它页面照旧）。

45. **车次详情两种走向各管各的**（2026-11-11 Z75 11-25 用户「按站点直连 / 按真实走向显示都不正确」）：
    · **站点直连**只连 `/api/route` 的 `stops[]`（本趟车实际停车站），前端 `directPath()` /
      `directStops()`；后端 `path` / `lineStops`（`cnrail.path_stations()` 从**线路库**展开的
      68 个通过站，坐标不少是仿射假值、站序还乱）**保持不变**存到 `expandedPath` / `expandedStops`
      备用，别拿它画「直连」，也别改后端（其它页面要用）。两种模式都只画停车站点。
    · **按真实走向**（`railosm.py`）几何来源：`confirmed.geom(nm)`（整理台定稿 `ld_geom`）>
      `lineosm.geom(nm)`（主站共用入口）> `railgeom.geom(nm)`（老 `geoms.json.gz`）。
      **老包没有「太中线」「石获线」**，Z75 的 太原→中卫 700 km 以前只能画一根大直线，
      缺口 750 km —— 整理台明明把 7 段全都确认了，别绕开它去补老包。
    · 整条线几何（京广线 28 段 / 2344 km）必须**逐点裁**成贴本趟车走廊的连续子段
      （`_trim_parts()`，走廊宽度随全程里程自适应 12~45 km）；只看段两端会把 820 km 长段
      （一端广州一端北京）整段丢掉，连带丢掉良乡—石家庄那一截。缺口只按停车站直线补，
      `fill` 不再是通过站个数。
    · 实测 Z75 覆盖 `0.481→0.925`、缺口 `750→108 km`、最大跳 `625→54 km`；G1 仍零缺口。
      `test_geo_toggle` 的「切回逐字还原」靠 `route.directPath` 不被覆盖，别改回 `pathBase`。
      回归 `scripts/test_route_geo.py` **56 项**。

46. **走向编辑器「连接两点」+「接着末尾画」**（2026-11-11 用户：「OSM 走向编辑器
    可以直接连接原有的 2 点，现在不行」）：多段改造后段间不连线，点已有红点又只做
    「选中」，于是既没有把两个已有点连起来的入口，也没法在载入已有线路后接着末尾画。
    · `S.links = [[i,j],…]` 存**手动直连段**，端点引用 `pts` 下标（拖点自动跟着走），
      `splitParts()` / `routeKm()` / `routePaths()` / 保存 / 建线 / 抓站全部自动带上；
      插点 `shiftLinks(at,1)`、删点平移且删掉含该点的段、`pushUndo()` 存 links 快照。
    · `connectPoints(a,b)`：两点正好在断点两侧（`hi===lo+1 && isBreak(hi)`）→ **删断点**
      把两段合一段；本来相邻 → 提示不用连；其余 → 新增两点直连段；再点同一对 → 断开。
      **别改回「自动把段间串成一条链」**（那是规则 15/多段改造要修的假直线）。
    · 入口三个：「⇄ 连接两点」按钮（`S.linkMode`）、按住 **Shift** 点两个红点（不进模式）、
      Esc 退出；连接模式关掉添加点模式，点空白处只提示不插点。腾讯地图的 vert click 与
      离线 Canvas 的 click 都走同一个 `onVertClick()`，别只改一处。
    · 「↳ 接着末尾画」按钮显式置 `S.drawing=true` 并打开添加点模式；**默认载入既有
      走向仍是插点**，别把这个默认值改成追加（规则 43 的 E2E 靠它判模式）。
    回归 `scripts/test_osmedit_link.py` **51 项**（离线自绘 Playwright：断点合并 2 段→1 段、
    任意两点直连的画布像素断言、再点断开、撤销、Shift 快捷、接着末尾画第 7 点、
    保存后 parts 多一条两点段、静态护栏）。

47. **跨线经由链必须能拼全，走廊别退化成大直线**（2026-11-12 用户：N1534/1 11-25
    「和硕往吐鲁番走然后走大河沿联络线」没显示）：人工 `train_override.via_json`
    三段（南疆线 阿克苏→头道河所 / 大河沿联络线 头道河所→大河沿 / 兰新客专
    大河沿→乌鲁木齐），`cnrail._via_chain()` 却只解析出 1 段 ——
    · 库里联络线叫「大河沿联络线（china-emu）」，`line()` 只认「+线 / 去线」，
      带「（来源）」后缀的那份认不出 → 整段跳过；
    · 「头道河所」是线路所，南疆线站序从「珍珠泉」起算没收它，`_key()` 也匹配不上
      → 段 1 被跳过。只剩段 3 兰新客专线（大河沿→乌鲁木齐），跟「库尔勒→乌鲁木齐」
      无关，`_chain_mids()` 返回 None → `path_stations()` 末段一个中间站都补不出来，
      `path` 从库尔勒(523 km) 到乌鲁木齐(793 km) 成了 **270 km 大直线**。
    `railosm.build()` 拿这根直线当走廊（自适应宽 24 km），南疆线绕去吐鲁番
    （偏出 150 km）的几何被 `_trim_parts()` 全裁掉 → 只剩直线，
    cover `0.781` / 缺口 `174 km` / 最大跳 `165 km`。**几何库里一直有那段，别去补几何**。
    · 修在 `cnrail.py`：`_via_line()` 剥「（来源）」后缀找同名版本；`_via_sta_pos()`
      先按 `_key()` 匹配，找不到就借站表里同键正式名（头道河线路所）的坐标，
      在本线站序里取 **60 km 内**最近一站当端点（段 1 落到「珍珠泉」）。
      三段链齐 → `_chain_mids()` 串出 和硕 / 托克逊 / 珍珠泉 / 大河沿 / 盐湖西… 19 站，
      `path` 17→34 点，走廊贴真实线路，几何自然全留下。**`railosm.py` 一行没改**。
    · **规则 45 的边界**：那条说的是「别为了修前端直连去改后端 `path`」，
      不是禁止把 `path` 本身修对。跨线站序本来就该展开；改完实测
      N1534 `0.781→0.995` / 缺口 `174→4 km` / 最大跳 `165→27 km` / 经度画到 `89.03`；
      Z75 `0.925→0.967`（太中线补出 27 站）、G1 零缺口、V6061 cover 1.0 全不变。
      「站点直连」仍只连 4 个停车站，`fill` 仍只算停车站，别把通过站放回直连。
    · 有人工经由的车次共 7 趟，只有 3 趟 `path` 变（Z75 / V6061 / N1534），
      全是补齐不是替换。
    · **换向车次号别名**（N1534 / N1531）：人工校正按主号 `N1534` 存，直接用
      `N1531` 调 `/api/route` 会退回自动推断（只剩「南疆线（OSM）」一段），
      同一趟车两个号显示两套走向。`build_route_payload()` 现在先查自己那条、
      没有才继承 `item_code(raw)` 得到的主号；**已有独立校正的仍优先用自己的**
      （`sync_via` 逐号各存一条，不受影响）。两个号的接口与页面实测完全一致。
    · 回归 `scripts/test_route_via_chain.py` **80 项**（三段链、线路所落点、
      lineStops、cover / 缺口 / 经度、换向别名继承、Z75 / G1 / V6061 不退化、
      HTTP、Playwright 两种走向切换 + 用 N1531 打开）。

48. **校对的线路下拉按发车站筛 + 页面内线路走向图**（2026-11-12 用户：「手工填写经由时，
    根据发车站来显示有该站的线路，不用全部线路都出来；这个页面最好也能提供地图供查看
    线路的走向和停站」，并特别补一句「**线路和车站之后都会增加哦**」）。
    · **动态是第一要求**：绝不写死线路名单，也**不要把全库「站→线」索引下发前端**
      （现在 36 线 / 2114 站无所谓，将来会爆）。服务端 `confvia.station_index()`
      按 `index()` 版本缓存（`_SIDX`，`invalidate()` 一起清），`lines_for(stas)` 只算
      传入的几个站；`/api/curate/linesForStops?stas=a|b|c` 一次返回「本趟车这些站」
      的候选，**返回量随车次站数走**。整理台确认新线 / 补新站 → 2 秒缓存一过自动出现，
      重开编辑器必重取一次（`cuOpen` 里 `await cuLoadLineScope()`）。
    · `/api/curate/lineNames` 仍只列 `confirmed.recent()`，另支持 `sta=`（按发车站单查
      兜底）、`q=` + `limit=`（地图按线名搜，只回 20 条）。**别给这个 handler 里的
      局部变量起名 `q`**：`one()` 是闭包，读的正是本函数的请求参数 `q`，同名会 500。
    · 发车站 = **当前段 `from` → 上一段 `to` → 车次始发站**。站名键用 `cuStaKey()`
      （前端）/ `confvia._key()`（后端），去空白 + 去「线路所/站/所」后缀，
      「北京南站」和「北京南」同候选，线路所照样在站序里。
    · 交互兜底三处：下拉顶部标「发车站 X · 含该站的确认线路 N 条」；一行都匹配不上
      → 退回全部并解释（站名写法 / 还没确认那条线）；「显示全部线路」开关 +
      下拉里的「显示全部」按钮可一键放宽。用户手打了车次站表里没有的新站 →
      `cuFetchStaLines()` 只查这一个站并回填缓存。
    · **点开线路框要列出该发车站的全部候选**，别被已填的线名缩成一条（`cbOpen()`
      对 `kind==='line'` 一律 `cbPaintList(box,'')` 再把 `CB.idx` 对准当前值），
      打字时 `input` 事件再实时过滤；无搜索词时最多 300 个 DOM。
    · **地图面板**常驻校对编辑器右侧（`#cuMapPane`，`.curate-stage.with-map` 控制）。
      数据走 `/api/curate/lineMap?name=`：只认 `confvia.names()`，几何用
      `lineosm.geom(confirmed_only=True)` 的 `parts` 多段（和主站 / 编辑器同一份），
      站序用 `confirmed.detail_payload()`（带 lat/lon、已套人工坐标）；
      总点数 >2000 时按段等距抽稀且**每段首尾保留**；没确认的线给 `200+found=false`。
    · 双后端：**有 Key 时复用已加载的 TMap SDK 开第二个 `TMap.Map('cuTMap')`**
      （WGS-84 走 `GCJ()` 纠偏，`fitBounds`）；没 Key 用 Canvas 自绘 —— 只画经纬网格、
      线路折线、站点、比例尺，**不画国界、不搬瓦片**（规则 14）。两种都叠加本趟车停站
      （橙）与当前段起讫站（红圈）；Canvas 支持滚轮缩放 / 拖拽 / 双击复位 / 悬停站名。
      点任一行的线路框、下拉选中、顶部搜线路都会让地图跟随；「编辑器」按钮打开
      `/osmedit.html?line=XXX`（`osmedit.js` boot 读 `line` 参数自动载入）。
    · **中间段怎么选，右侧地图就怎么显示（实时）**：聚焦某段立刻切到该段线路；
      打字过程走 `cuMapSync(i,false,true,300)` 防抖跟随，**半截线名有前缀保护**
      （敲「石太」还没补「线」时不把地图闪成「不是确认线路」）；方向键 / 鼠标在候选上
      浏览由 `cbPreviewLine()` 先预览（140ms），`Esc` 不选就退回该段实际线名；
      只改起讫站时传 `switchLine=false`，只挪红圈、不抢走当前线路；增删段后
      `cuMapFollowRows()` 保证地图仍停在中间选的那一段（没选过线就保持本趟车全景）。
    · 回归 `scripts/test_curate_linefilter_map.py` **114 项**：静态护栏、
      `lineNames` 三种过滤、`linesForStops`（**内存伪造一条新线+新站验证自动出现，不写库**）、
      `lineMap` 抽稀不改变四至、Playwright 7 段车次逐行不同发车站、开关、
      Canvas 像素断言、缩放/拖拽/hover/收起/切页签、聚焦换线 / 点段落行 / 半截线名 /
      方向键预览 / Esc 回退 / 改站红圈跟随、假 TMap SDK 路径。

49. **N17 真实走向：站序口径与几何裁剪两处同时修**（2026-11-12 用户「N17 在 11/25
    在车次详情的真实线路还是不正确」）：N17（北京→齐齐哈尔）停站只有 6 个
    （北京 / 秦皇岛 / 沈阳北 / 哈尔滨 / 大庆西 / 齐齐哈尔），人工经由 4 段
    京哈线 北京→哈尔滨、滨洲线 哈尔滨→红旗营、榆红线 红旗营→榆树屯、
    平齐线 榆树屯→齐齐哈尔。
    · **根因 1：`cnrail._via_chain()` 只认线路库那份站序**。滨洲线库版 44 站，
      「对青山」之后直接跳 367 km，哈尔滨—大庆西—红旗营整段缺失 ⇒ `红旗营`
      认不出、这一段被丢掉，跨线链断在大庆西，`path` 末段变成 107 km 直线；
      平齐线库版尾段方向还是乱的（榆树屯之后往南绕到大兴再跳回齐齐哈尔）。
      整理台确认版滨洲线 78 站、含红旗营，平齐线方向也对。**整理台确认站序优先、
      底表兜底**（未确认的线 / 来源后缀名仍回底表），`chain` 每项直接带
      `(label, stations, pf, pt)`，**别再按 `_LINES` 下标回查**（兜底那份站序不在库里）。
      `_via_confirmed_stations()` 只取 `confirmed=1` 的线、读 `confirmed.detail_payload()`。
    · **根因 2：`railosm.build()` 把 4 条线的整条几何摊平后贪心拼**。滨洲线过昂昂溪
      继续往满洲里的延伸段也在走廊 45 km 内，因「走得最远」被选中，真正进齐齐哈尔的
      平齐线短段反而被丢掉 ⇒ 终点偏 41 km、尾段多出 48.5 km 西绕折返。修法：
      `_parts_of()` **按线名分组**；`_seg_polyline()` 取每条经由线路 `from→to` 的
      确认站序折线（20 秒缓存，整理台改站序自动跟上）；`_clip_to_seg()` 按站序折线
      逐点二次裁剪；`_seg_hug()` 判站序坐标是否可靠 —— 只有 ≥60% 的贴线点在 8 km 内
      才裁，否则退回老逻辑（**Z75 石太线 111 个点中位偏离 25.8 km，硬裁会把真走向切光**）。
    · 实测 N17：`path` 末段最大跳 107→26.2 km、`lineStops` 补出 红旗营 / 榆树屯 /
      大民屯 / 齐齐哈尔南，真实走向 1949 点、**cover 1.0 / 缺口 0 km**、终点离齐齐哈尔
      0.44 km、尾段最西 123.718°E（不再到 123.3°E）、最大跳 38.8 km；
      Z75 0.984 / N1534 0.996 / N1531 0.996 / G1 1.0 全不退化。
    · **别再试「按方向裁剪几何消掉尾段小回环」**：试过硬裁 run 的起止方向，
      京哈 / 滨洲 / 平齐三段被一起切掉，cover 掉到 0.6 以下；已撤回，
      当前保留的只是「按线名 + 本段站序」二次裁剪。
    · 回归 `scripts/test_route_n17_via.py` **61 项**（静态护栏 / 四段链 / 跨线中间站 /
      lineStops / 真实走向终点·覆盖·西绕 / 其它 4 趟不退化 / HTTP / Playwright 两种走向）。
    · **推广成全局规则见第 50 条**，本条只记 N17 的原始根因与修法。

50. **「真实线路」是全项目统一规则，不是某一趟车的特例**（2026-11-12 用户：「把这次的
    修改规则固定下来，用到所有的线路车次详情的真实线路上」）。第 49 条两条修法本来
    写在通用函数里，但全量扫 11-25 的 240 趟车发现 **943 个经由段只有 53 个（5.1%）
    取得到本段站序**：`railosm._seg_polyline()` 自己另写了一份「只认
    `confirmed.detail_payload()`」的解析，非确认线路一律退回贪心拼；还有 7 趟车被裁后
    覆盖率反而下降（D40 `0.986→0.888`、V1118 `0.613→0.481`、V1524 终点偏 17.8 km…
    站间折线是粗弦，18 km 硬带会把真弯道一起切掉）。固化成三条：
    · **① 唯一站序入口**：新增 `cnrail.via_segment_polyline(name, frm, to)`，直接复用
      `_via_chain()`（整理台确认站序优先 / 线路库含「（来源）」后缀版本兜底 /
      线路所按坐标就近落点），返回 `{"pts":[(lon,lat)…], "confirmed":bool, "label":str}`。
      `path_stations()` 的跨线展开与 `railosm` 的几何裁剪**从此同源**；`railosm._seg_polyline()`
      只留 20 秒缓存，别再另写一份解析。端点认不出 / 点不足 2 个必须返回 None ⇒ 不裁剪。
      推广后能取到本段站序的段 **5.1% → 98.2%（926/943）**。
    · **② 可靠性门槛**：`_seg_hug()` < 0.60 不裁；兜底（非确认）站序门槛抬到 0.75，
      宁可不裁也别裁错（Z75 石太线站序中位偏离 25.8 km）。
    · **③ 全局不退化闸**：`build()` 把「裁过」和「不裁」各拼一次
      （`_assemble(clip_parts)` / `_assemble(base_parts)`），覆盖率掉 >`COVER_DROP=0.02`
      或终点多偏 >`END_WORSE_KM=5` ⇒ **整趟退回不裁**；覆盖率相同则保留裁剪
      （N17 裁不裁都是 1.0，但未裁会往满洲里方向西绕一圈，光看覆盖发现不了）。
      `info["clipped"]` 回报这次真正裁过的线名，接口 / 回归都能查。
    · **缓存也得跟着改**：`railcore._OSM_CACHE` 原来只在满 300 条时丢最早的，用户改了
      整理台站序 / 几何 / 人工经由后，已看过的车次会一直用旧拼法。现在
      `_osm_data_epoch()` 取两库（含 `-wal`）mtime 指纹，变了就整包 clear；
      别改成「只在某几个写接口里清」，会漏。
    · 全量复扫 240 趟：**覆盖率明显变差（>0.02）0 趟、终点变差（>5 km）0 趟**，
      终点偏离改善 9 趟；剩 25 趟 `ok=false` 与 >100 km 大跳都是「这条线本来就没几何」
      的既有缺口，不是裁剪造成，**别拿补几何/放宽走廊来掩盖**。
    · 回归 `scripts/test_route_osm_global.py` **97 项**：三条静态护栏、唯一入口对
      确认 / 非确认线路都出折线且点序 `[lon,lat]`、13 趟车逐趟「不比不裁更差」、
      N17 原修复不被改坏、缓存 epoch 失效、HTTP 三趟车同一出口。只读不写库。

51. **「按确认线路」是车次详情真实走向的默认画法，旧画法原样留着可切**
    （2026-11-12 用户：「车次详情 地图展示还是不行，帮我重新按照我的逻辑来写，
    并且将原来的逻辑保留」+ 三条规则）。新增 `webapp/routeshape.py`（666 行），
    **`railosm.build()` 一行没动**，`railcore.route_osm(code, date, mode)` 按
    `mode="confirmed"`（默认）/ `"legacy"` 分流，缓存键带 mode。
    · **规则 1：一切按整理台确认线路的站序与累计 km**。每段经由取
      `cnrail.via_segment_polyline()` 的站序折线（与第 50 条同一入口），
      km 用 `_anchor_km()` 从车次停站表取该站真实累计里程，段内按几何弧长线性
      落位（`_remap_km`）；找不到锚点才退回 `_guess_span()` 按站序折线长度估。
    · **规则 2a 跨线必在中转站转线**：逐段串接，段首段尾一律用**中转站坐标**
      （`_coord_of()`：先停站表、再该线站序、最后站点档案），不让几何自带端点
      带偏；两段中转站不是同一站时，中间那截按停站直连补（`_missing_stops()`）。
    · **规则 2b 断裂自动连最近两点**：确认线路的几何往往是整条线的**乱序碎段**
      （京广线 28 段从湖北排到石家庄）。`_seg_candidates()` 把每个碎段按站序折线
      投影切成 `(s, 点)` 序列、只留投影落在本段 `from→to` 区间（`s∈[-tol, span+tol]`）
      且离折线 ≤`CLIP_OFF=16 km` 的点；`_chain_parts()` 按投影顺序串段，翻转取
      **最近的一对端点**相接（相距 ≤`LINK_MAX=80 km` 直接连，>80 km 记成缺口但照样连，
      不再画假长直线）。
    · **规则 2c/2d 长出截至起讫 / 不足用首末点接**：裁剪已由站序投影完成，
      `_trim_ends()` 只在「真的长出一大截」时按起讫切；整趟首尾不足就补上
      始发 / 终到站坐标（`out[0]/out[-1]` 严格对齐 `stops[0]/stops[-1]`）。
      **别再按「离起讫最近的点」二次截取** —— 试过，会把折线中段整截裁掉。
    · **规则 3 上下行分线只选一条**：`_pick_branches()` 先按投影区间重叠 >60% 归组，
      组内优先级依次为 **含停站数最多**（`_stop_hits`，按 6 km 内命中停车站计）→
      覆盖本段最长（`span`）→ 最贴站序折线（`hug` 小）→ 点最多。
      **别把 `hug` 放第一位**：试过，太中线 898 点的长段被 8 点的小碎段挤掉，
      拼出 376~551 km 的假长直线。
    · **规则外的场景沿用原画法**（用户：「没有提及的按照原来的方式继续」）：
      非确认线路段、以及两段经由之间的空隙，`_legacy_slice()` 按 km 区间从
      `railosm.build()` 的结果里切一段接上；整趟一个确认段都没有（如 G1 全是底表线）
      时 `build()` 直接返回 `None`，`route_osm()` 整趟回退 legacy 并标
      `info["fallback"]="legacy"` / `mode="legacy"`，前端按钮不失效。
    · 返回 `info` 带 `segInfo`（每段 `line/from/to/confirmed/source/links/linkKm`，
      `source ∈ geom|stations|legacy|straight`）、`confirmedSegs`、`legacySegs`、
      `links`/`linkKm`（自动连接的接头数与总长）、`cover`/`gapKm`/`gaps`。
    · **前端**：「按真实走向」旁边多一个画法下拉（`#geoMode`，选中值存在
      `localStorage['railcn.geoMode']`），备注文案 `geoNoteText()` 会带上
      「按确认线路 / 原画法」与缺口；`toggleGeo()` 带 `mode` 参数请求，两种画法
      互不串缓存。别把下拉改成单选按钮组——宽度会把备注挤掉。
    · 实测：N17 2093 点 / cover 1.0 / 终点 0.00 km / 最大跳 38.8 km / 尾段 123.724°E；
      Z75 3196 点 / 7 段全确认 / 最大跳 28.1 km；D40 空隙无 >60 km 直线。
    · 回归 `scripts/test_route_shape.py` **55 项**（静态护栏、4 个纯函数单测、
      N17/Z75/D40 实测、HTTP 两种画法、Playwright 下拉来回切）。只读不写库。

52. **独立页「确认线路地图」**（2026-11-13 用户：「单独做一个页面，左侧是所有经整理台
    已经确认的线路，可单选可复选，默认全部选中，右侧是将选中的线路展示的地图，
    地图用腾讯地图，右侧占屏比例尽可能大」）。
    · 入口 `/linemap.html`（主站顶栏「确认线路地图」链接，**不占页签**），
      独立三件套 `static/linemap.{html,css,js}`，不引主站 `app.js`（副作用太多）。
      后端只读模块 `webapp/linemap.py` + 两个路由 `/api/linemap/index`、
      `/api/linemap/geom`。**别在主站加页签**，也别把几何逐条打 42 次请求。
    · **口径**：清单 `confirmed.recent()`（全量、不截断、按最近确认倒序），
      几何 `lineosm.geom(nm, confirmed_only=True)` 的多段 `parts`，
      站序 `confirmed.detail_payload()`；非确认名（草稿 / 来源版本）静默跳过，
      `found=false` 只提示不报错。现 42 条确认线路，全都有走向。
    · **批量 + 逐段抽稀**：`_thin()` 按段抽到 `detail`（简 400 / 标 900 / 细 2000）
      且每段首尾都留 —— 几何是乱序碎段，摊平等距取样会画出几百公里假直线
      （同第 40 条）。`_bbox()` 给自动缩放；20 秒缓存跟整理台改动。
      实测全选 42 条 / 26,875 点 / 2,267 站，约 2.2 s（带站点约 5 s）。
    · **左栏**：默认全选；复选（checkbox）/ 单选（radio）模式切换；复选下勾
      「点名单选」则点行名只看这一条；全选 / 清空 / 反选 / 搜索 / 显示车站 /
      自动缩放 / 精度；「收起」把左栏折到 0，地图变成 100% 宽。
      1600 px 下右栏 **83.1%** 宽、满屏高，收起后 100%。
    · **右栏**：腾讯地图 GL JS（`TMap.Map` / `MultiPolyline` / `MultiMarker` /
      `InfoWindow`），每条线一个 `PolylineStyle`（黄金角配色，样式在图层创建时
      一次建全，之后只 `setGeometries`）；一条线一个 geometry、`paths` 是二维多段，
      点线弹线路信息；喂给 TMap 前统一 `wgs2gcj`。无 Key 时自绘 Canvas 示意图
      （网格 + 线路 + 拖拽缩放，**不画国界、不搬瓦片**）。
    · **Key 全站共用**：只读 `localStorage['railcn.mapkey']`，代码不含任何 Key；
      Key 弹窗保存后**原地重建地图，绝不 `location.reload()`**（用户在数据校对页
      投诉过刷新整页）。注意 **localStorage 按 origin 隔离**，
      `localhost:8686` 与 `127.0.0.1:8686` 各存各的 —— 弹窗显示当前站点并提示，
      别让用户以为 Key 丢了；代理模式占位符 `__WB_HTTP_PORT__` /
      `__WB_TMAP_SECRET__` 保持原样。
    · 回归 `scripts/test_linemap.py` **69 项**：静态合规护栏（无 Leaflet / OSM /
      Mapbox / Google、无写死 Key、只读后端）、清单与批量几何、
      Playwright 两种后端（无 Key Canvas 分支 + 注入 mock SDK 验证腾讯 GL JS
      的 multi-geometry / 颜色 / GCJ 坐标 / fitBounds / 信息窗）、
      默认全选 / 单选复选 / 折叠占比 / Key 不刷新。只读不写库。

## 6. 前端约定
- 4 个页签：`车站/线路`、`数据下载`、`历史数据`、`数据校对`；状态都在全局 `CU` 对象里。
  （`站站查询` 2026-11-05 按用户要求从页面去掉：`index.html` 的页签 + `#paneOD`、
  `app.js` 的 `searchOD()` / `bindSuggest()` 与相关绑定都删了；后端 `/api/tickets`
  保留（无前端入口），将来要恢复只补前端即可。）
- 校对页筛选：`todo / via0 / via / km / coord / pending / manual / synced / clean / all`。
- 编辑里程的版面开关是 `#paneNet.km-editing`（第 38 条）：清单折表头、站序隐藏、
  编辑区紧跟标题置顶；**退出即靠移除 class 还原**，别 clone / 删清单 DOM。
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


## 7. 回归脚本（58 个，改完必跑；不要并发跑）

> 历史提示：2026-11-02 线路库清空又导回、来源表一度为空，当时 11 个脚本报红，
> 全是「脚本写死具体线名」或「依赖已清空的来源表」，不是代码坏；现在按新口径全绿。
> 完整历史逐条原因见归档旧版。跑完检查 `station_custom` / `line_override` 有无测试残留。

```bash
for f in test_chain test_line_km test_line_stations test_nm_stations test_osm_extras \
         test_station_loose test_tl_stops test_geo_toggle test_dock_speed test_code_alias \
         test_routestore test_nm_insert test_curate_refresh test_curate_sync test_net_form \
         test_via_nodes test_lineseg test_catalog_match test_chinaemu test_linedesk \
         test_loose_line test_osm_data test_src_pick test_geo_import \
         test_line_group test_line_base test_renum_cutover \
         test_ld_merge test_line_route_geom test_station_names test_trainmap test_skip_recheck \
         test_traintrack test_ld_publish_geom test_confirmed_lines test_ld_flow \
         test_geom_axis test_ld_stop_confirm test_osmedit test_station_coords \
         test_ld_insert_stop test_confirmed_list test_km_editor_layout \
         test_osmedit_confirmed test_osmedit_multipart test_osmedit_addpt \
         test_curate_confirmed_scope test_route_geo test_renum_enum test_trainclass_labels \
         test_osmedit_link test_route_via_chain test_curate_linefilter_map \
         test_curate_joint_via test_route_n17_via test_route_osm_global \
         test_route_shape test_linemap; do
  "C:/Users/Administrator/AppData/Local/Programs/Python/Python313/python.exe" scripts/$f.py
done
```

| 脚本 | 守什么 |
|---|---|
| `test_geo_import` | GeoJSON 导入：站序按走向排（不是文件点序）、首站 0、里程单调、走沿线而不是直线、几何里程 = 站序里程、`source='geojson'`、文件名记着、重复导入 400 且提示「可以覆盖」、`replace` 能覆盖、坏文件三种人话报错、删线后走向 404 |
| `test_curate_sync` | 自动结果要确认才入库、同步「原样照抄 / 重新落站」两条路、站表变化、UI 勾选与同步标记、跑完自动撤销 |
| `test_curate_refresh` | 线路库改动后重算这趟车的自动经由 |
| `test_net_form` | 车站/线路查询输入框宽度、联想框宽度与行高、Esc/点空白收起、空列表不占高度 |
| `test_via_nodes` | 数据校对线路口径（2026-11-11 改）：下拉只列确认线路、没确认的 `沪春线 / 合新高速线 / 银西高铁` 不出现；确认线路的站序自带线路所（京沪线 `柳村线路所` / `黄土坡线路所`）并在选站候选里标「线路所」；`/api/line/stops` 老口径不变 |
| `test_curate_confirmed_scope` | **校对只用整理台确认线路**（第 44 条）：`confvia.index()` 与 `confirmed.names()` 实时一致、名字归一把「京广线（OSM）→ 京广线」、确认站序上连点 3 站必认成一条线、没确认的线全变缺口、`lineNames/cands/detail.gapCands/清单` 里没有来源版本名、静态护栏（curate 不再用 `railcore.cand_lines`） |
| `test_lineseg` | 分段线借整条线的几何并按首末站裁剪（元昆段 257.7 / 成攀段 725.4）、裁不出如实退整条（宁西线断 7 截）、只有 OSM 的线名补站序（成昆 133 站 / 京哈 37 站）、京港两段隔 407 km 拒绝拼、UI 标「借…裁出本段」|
| `test_catalog_match` | 资料库不会挂到小联络线上（侯西线 42 站不能匹配陇海西成联络线 2 站）、分段关系不被误杀（京石高铁 → 京广高速线）、搜侯西线拿到 42 站 288.5 km 且 OSM 走向在 |
| `test_chinaemu` | china-emu 表在且行数够、桥隧行没混进站序、match_key 两套命名同键、兰新线纠回 118 站（不是兰中城际 6 站）、资料库没有的线名能补出站序、杭深铁路补出 50+ 站里程、首末站里程口径对 |
| `test_linedesk` | 整理库独立（跑在临时文件上，rail.db 修改时间不变）、京沪四种写法归到一条、京哈沈哈段只挂一次、改名不破坏线族、同一来源线名不能被两条线抢、china-emu 的 K 里程搬过来变段内里程、合并转移别名/站序/旧名仍可搜、父子不能成环、导出结构完整 |
| `test_loose_line` | 库外线路（侯西线：资料库 42 站 + OSM 走向）搜 title 收敛到规范名、改站序时自动收进线路库并落用户站序、再改走覆盖、删掉回资料库原样、真没有站序的线仍明确报错 |
| `test_src_pick` | 站序默认来源勾选后全链路跟随（详情 / 选经由 / 联想标记）、指定 from 的临时查看不被默认覆盖、按规范名勾的默认搜 title 也认、非法来源被拒；倒排**只翻指定来源**（线路库 / 资料库那份不动、不生成本条线人工站序、来源条标 ↺、再点一次恢复）、库内线路才走人工站序可恢复、库外线路不再被收编、备注不被覆盖、按钮两段确认 |
| `test_line_group` | 同一条线的几个来源版本（`京沪线` / `（底表）` / `（OSM）` / …）：归组信息、`variants` 里的 member 条目、没设默认按底表、`exact=1` 不替换、设默认后搜基础名给默认那份、校对下拉只剩一条（名字用基础名）、选经由站序跟着换、取消默认回底表、乱填版本名 400。**自己清 `line_default`** |
| `test_line_base` | **基本线路**（整理台定稿 → 全项目接管）：publish 后 `/api/line` 给 `base=true` + 定稿站序、点名看「（底表）」不接管、选经由 `from=base`、校对下拉只列它且排最前、`effective.why=linebase`、同名 publish 报 400 且 `replaceId` 可覆盖、unpublish 退回原样、无来源版本的线也认基本线路。**自己清 `line_base` + 整理台草稿** |
| `test_ld_publish_geom` | **整合成果跟到主站**：`merged_geom()` 从 `ld_geom` 解出折线并标 `ldGeom`、`publish()` 的 geomName 兜底（传空串不回填 / 换走向类型不回填）、`/api/line` 与 `/api/line/osm` 优先用整理台合并走向（`route:rg` 定稿线也走 merged_geom，不被来源包几何抢）、来源版本仍在。27 项（走向来源改为从 `line_detail()` 动态取，别写死 `geojson`），`linebase.publish` / `linedesk.log` 打桩，**不写库** |
| `test_ld_stop_confirm` | **站序删改后「用它并确认」不回灌 + ① 无站点但有走向的来源可选中**：删站后默认确认本线站序（`confirm_stop_source(self=True)` 只置 `stop_ok` 不动站序）、外部来源才 `fill=True` 覆盖、`stops_edited` 跨刷新保持；`set_line_sources()` 只清 `geom_src='geojson'`、保留 `route:rg`；① 来源里 `n_stop=0` 但有几何的线也能选中 / 预览走向 / 确认；GeoJSON 导入 `only='stops'\|'geom'` 互斥。`ld_line` 临时库跑，**不碰 rail.db** |
| `test_ld_insert_stop` | **新增车站指定插入位置（第 36 条）**：首 / 中 / 尾插入、`pos` 越界与非法值夹取、一次多站按顺序、字符串切分、**原站里程 / 坐标 / 来源 / 备注一个字不动**、新站里程留空不插值、真实站名自动补坐标、`seq` 连续、起讫 / `stop_ok` / `stops_edited` 口径、空站序插入、空名字与不存在线路拦住；前端静态（每行 `+`、弹窗下拉、旧 `prompt` 已删）+ 路由静态。**48 项**，临时库 |
| `test_osmedit` | 走向编辑器 **46 项**：全量索引只收有走向的线 / offset 分页 / 无走向草稿不进列表 / **济南线按确认线路口径（第 39 条）** / 载入抽稀 / 草稿重载 / 多段规整 / apply 对不存在的线安全跳过 / **apply 写回已有 `line_geom` 真生效**（`updated_at` 列坑）/ 前端静态（离线可编辑、滚动加载、删除按钮、站点默认隐藏）；**抓站**（京沪线认得出北京上海、里程单调、带距离、<2s）+ **真建线**（3~4 站建进线路库、`custom=True`、写入 `line_geom`、留草稿、同名拦下、`replace` 可覆盖、<2 站报人话）→ 跑完删掉测试线。**抓站 / 建线样本一律用 `_cand_ldgeom('京沪线')` 原始碎段串线**，别拿 `load_geom()` 结果（现在返回主站多段，首段不覆盖全线） |
| `test_station_coords` | **车站多坐标（第 5 节 34 条）**：GCJ-02 往返误差 <1e-6°、`calibrate()` 对「已归一 / 需换算 / 同名异地 / 无基准」四种情形的处理、候选表 12+ 来源且重建幂等、60 m 聚类 + 唯一当前标记 + 按距离排序 + 「站」后缀别名、东莞西 12.7 km 同名异地点保留且 BD-09 已换算并入、`station_detail` / OSM 独有卡片都带 `coordCandidates`、确认来源写进 `coord_override` 并可撤销、跑完零残留。**31 项**；人工坐标总数基线**当场取**，别写死（用户随时会在车站卡片确认新坐标，2026-11-09「北园」就从 22 变 23） |
| `test_confirmed_list` | **线路模式下方全量确认清单（第 37 条）**：正式库条数 = `stats()['confirmed']` / 字段齐 / 严格倒序；**临时库**验证确认时间取 `ld_log.finalize/publish` 而非 `updated_at`、无日志回退、同秒按日志 id、日志 target 为 `ld_line.id`、别名只收 `ok=1`、**新日志让缓存立刻失效**；HTTP `full=1` 带 items 而老口径不带；前端静态（独立容器 / 本地筛选 / 不弹下拉）。**43 项** + E2E 28 项 |
| `test_km_editor_layout` | **编辑里程版面（第 38 条）**：`km-editing` 开关在 `lnWide()` / `lnRenderEditor()` 里幂等 toggle、进编辑滚回顶部、详情 DOM 顺序「标题 → 编辑区 → 资料 → 站序」；工具条 / 总里程 / 段距 / 折叠说明的顺序与全部既有 id、填写规则文案、里程算法与保存链路；CSS 编辑态折清单 / 藏站序 / sticky / `38vh` / 矮屏 / 说明隐藏；HTTP 冒烟（服务没起自动跳过）。**52 项**，纯静态不写库 + E2E 39 项 |
| `test_osmedit_confirmed` | **走向编辑器里整理台已确认线路（第 39 条）**：正式库只读验证全量确认线路进列表 / 红名标记 / 置顶 / 按 `geomName` 可搜 / 21 条全都能载入且点序 `[lon,lat]` / 全部走主站同一份几何（`source=confirmed`，草稿 `edit`）且载入段数与主站逐条一致；**临时 linedesk.db + 临时 rail.db** 验证已有 `ld_geom` 的线写回后 `merged_geom()` 命中新几何、没有 `ld_geom` 的线按 `line_id` INSERT 且 `confirmed.geom()` 改读新几何、**`ld_line` 14 个确认相关字段保存前后逐字段相等**、草稿线不冒充确认线、只存草稿不写回、保存后确认条数不变；前端静态（红名 class / CSS 色值 / 提示文案 / `target` 规范名 / `startNew` 清态）+ 服务端透传 + **走向编辑器全程不写 `ld_line`**；HTTP 冒烟（服务没起自动跳过）。**61 项** + 隔离临时库 / 临时端口 headless Chrome E2E **26 项**（不碰正式库与 8686） |
| `test_osmedit_multipart` | **走向编辑器载入形状与主站一致（第 40 条）**：`lineosm.geom()` 公共入口静态检查 / 21 条确认线路段数与 bbox 逐条对齐主站 / 里程差 ≤8% / 没有 >40 km 段内假直线 / 滨绥线等 5 条不再被截断 / `_as_segs` 扁平与多段与历史反序 / `_thin_segs` 段数守恒 / `_cap_jumps` 补长直线 / 多段写入 `line_geom_edit` 与 `line_geom` 后读回仍是 3 段 / 前端断点（绘制 / 路径 / 里程 / 插删撤销 / 保存 / 状态条）/ HTTP 出口。**58 项** + Playwright E2E **16 项**（探针注入 IIFE，不写库） |
| `test_osmedit_addpt` | **走向编辑器添加点的点序（第 43 条）**：离线自绘 Playwright（不要地图 Key）——起画 5 个点，每次「已选中第 N 点」都等于点数、1 段、里程单调、状态条标「画线追加」；临时草稿（`test_加点回归线`）载入后状态条不再标追加、点空白处仍插到第 2 位、点线尾延长线接成第 5 点；清空重画（用户实际路径）同样按点序追加；静态护栏（`S.drawing` / 复位 ≥3 处 / `bt > 0.999` / `segProj` / 帮助文案）。**34 项**，跑完删临时草稿 |
| `test_confirmed_lines` | **主站确认线路白名单**：白名单判定（草稿 / 备份版本不算）、确认线路的停站 / 定位 / 真实走向 / 总里程 / 不返回来源版本与 ★、**规范名 ≠ 走向来源名时按两种叫法都能取到折线（第 35 条）**、老口径不受影响、`finalize()` 缺停站 / 缺走向 / 缺定位都拦住（临时库）、HTTP 通道只给确认线路且老 `/api/lines` 照旧。项数随确认线路条数走（2026-11-11 为 **159 项**），站数从 `confirmed.get(nm)['n_stop']` 动态取；走向点数只卡 4 条整理台定稿线。**负样本一律现挑**（`_neg_sample()` 从 `linedesk.list_lines()` 里取第一条 `status≠confirmed`、`n_stop≥2` 的线）：11-10 把「京广」写死成草稿线后 11-11 京广线被确认，5 项断言全红；也别从 `cnrail.search_line()` 挑 —— 底表清空后 `data/cnrail_lines.json` 是空的，那边返回 0 条 |
| `test_geom_axis` | **走向点序契约**（第 5 节 32 条）：`_norm_chain` 两种输入都归一成 `[lon,lat]`、整段判定不逐点翻转、`_save_geom` 写库的 spine/parts 都是 `[lon,lat]`、4 条确认线路 `merged_geom`/`desk_geom`/HTTP 出口全部正确、沪昆线优先 47 段 10670 点原始多段、走向与停站经纬度范围一致。**28 项**，临时库测写入，不碰正式数据 |
| `test_ld_flow` | **整理台新流程（第 5 节 32 条）**：① 只存走向（快照证明 `ld_stop`/`ld_geom` 一字节没动、不改基本信息）、② 只存站点（不写 `ld_geom`）、站序反向与再反向（里程倒排 / 缺的不插值 / 坐标与 `stop_ok` 不变）、GeoJSON `only='stops'/'geom'` 分流、前端静态检查（④ 与走向下拉已删 / 面板顺序 / 反向按钮）、HTTP 走一遍 ③ 自动 publish 并清理临时线。**44 项**，后端 1~4 在临时 `linedesk.db` 跑，HTTP 部分服务没起自动跳过 |
| `test_renum_enum` | **第 41 条**：临时库 + 假搜索接口，验证 `enum_prefixes()` 含 V/N/X/B 且数字前缀独立、`_detect_type()` 收任意字母、`enum_dates()` 双日期、两个日期各发一遍请求且细分用同一日期、结果按搜索日期分别写 `trains`、开关关闭时回到旧行为、**后续 `download_stops()` 能抓到新号经停站且 `curate` 按 `date>=切换日 + V/N/Z/Y` 筛得到**、静态与 HTTP。**57 项**，不碰正式库 |
| `test_trainclass_labels` | **第 42 条**：`historyq._label(t, date)` 按 `date >= 2026-11-25` 分时代（T 旧=特快/新=高速动车组、K 旧=快速/新=高速动车、N 旧=数字/新=快速普速、V 普通快速、X 超高速）、无日期按新版；`trainmap.js` 有 `TYPE_LABELS_OLD/NEW` + `tmIsNewEra` + `relabelTypeChips` + `TYPE_KEYS`、无旧 `const TYPES`、`renderStat` 重绘；`app.js` 勾阶段后自动 `POST /api/curate/scan`、tooltip 新文案；`curate` 阶段字头与新分组一致；HTTP `renum.letters`。**36 项**，只读不写库 |
| `test_renum_cutover` | 2026 车次改号切换日：阶段一清全部旧校正 / 阶段二只清 G/D/C/S、清切换日前的待校对行、切换日后新校的保留、`--dry-run` 不落库、`--check` 写阶段勾选、幂等；`curate._era_having/_era_where` 三态（没切换日不过滤 / 有切换日没勾阶段 = 1=0 / 勾了按字头+日期）。**临时库，不碰真实数据** |
| `test_chain` | 车次合并（中途换号）与检索 |
| `test_line_km` / `test_line_stations` | 线路里程双模式、站序编辑 |
| `test_nm_stations` / `test_station_loose` | 新增线路站名并集、站名带「站」也能搜 |
| `test_tl_stops` | 时间条停站与时刻 |
| `test_geo_toggle` | 地图「站点直连 ⇄ 沿真实线路」按钮来回切、切回后路径逐字还原 |
| `test_route_geo` | **车次详情两种走向**（第 45 条）：直连只连 7 个停车站、真实走向优先用确认线路几何（太中线 / 石获线老包没有）、逐点裁整线几何、缺口只按停车站补、Z75 覆盖 ≥90% 且无 >100 km 大跳、G1 仍零缺口、HTTP + 浏览器 E2E 两套模式可来回切 |
| `test_dock_speed` | 地图事件层不抢 dock 命中、倍速可用 |
| `test_routestore` | 经停站按趟去重（route_tpl / route_day） |
| `test_code_alias` / `test_nm_insert` / `test_osm_extras` | 车次号别名、新增站、OSM 未收录线路 |
| `test_osm_data` | 第六套来源 OSM：表齐、境外与台湾地区进不来、状态只有运营中/在建/规划、里程归零+单调+seq 连续、站名清洗、自有的分段拼接仍优先于 OSM、站名清洗、自有分段拼接仍排在 OSM 之前、原本没站序的线补上了（瓦日/嫩林 >30 站）且成昆仍选资料库、车站归属反查按垂距升序、三来源联想不合并、OSM 独有车站出卡片、默认兜底仍是自分段拼接在前、不碰既有表 |

| `test_curate_linefilter_map` | **校对线路下拉按发车站筛 + 页面内走向图**（第 48 条）：静态护栏（不下发全库索引 / `linesForStops` / `lineMap` / TMap+Canvas 双后端 / 合规不画国界）、`lineNames` 的 `sta`/`q`/`limit` 三种按需过滤、`linesForStops` 站名键归一与**内存伪造一条新线+新站验证候选自动出现（不写库）**、`lineMap` 多段 / ≤2000 点 / 带坐标站序 / 抽稀不改变四至 / 未确认线 `200+found=false`；Playwright 7 段车次逐行不同发车站（2~4 条 vs 全部 36 条）、当前线路仍在候选、「显示全部」开关、地图跟随选中线路、Canvas 蓝线/橙点像素断言、滚轮/拖拽/适应/悬停/收起展开/切页签、假 TMap SDK 路径。**105 项**，只读不写库 |

| `test_curate_joint_via` | **线路所纳入自动计算经由**（11-12）：`confvia.joint_index()` / `_bridge_chain()` 按线路所精确站名做 BFS 跨线链（≤4 段、带缓存与版本失效），N1534「头道河所」、Z75「获鹿所」两条链路能自动推断出来；只采用至少经过一个线路所的链路，普通多线绕行仍留缺口；`_key()` 剥后缀会让「获鹿所 / 获鹿」撞名 ⇒ 线路所按**精确名**匹配。**34 项**，只读不写库 |
| `test_route_osm_global` | **真实走向全局规则（第 50 条）**：三条规则静态护栏、唯一站序入口对确认 / 非确认线路都出折线且 `[lon,lat]`、13 趟车逐趟「裁后不比不裁更差」（cover 掉 ≤2 个点 / 终点不多偏 5 km）、规则确实落到非确认线路、N17 原修复不被改坏、`_OSM_CACHE` 按 mtime 指纹失效、HTTP 三趟车走同一出口。**95 项**，只读不写库 |
| `test_linemap` | **确认线路地图（第 52 条）**：静态合规（只有腾讯 GL JS / 无 Leaflet·OSM·Mapbox·Google / 不写死 Key / 后端只读 / 默认全选）、清单 = `confirmed.recent()` 且无来源版本名、批量几何逐段抽稀保首尾 / bbox / `stops` 开关 / 非确认名静默跳过、Playwright 无 Key Canvas 分支（默认全选 42 条、右栏 83.1%、收起 100%、全选·清空·反选·复选增减·单选 radio·点名单选·搜索·车站·精度·Key 弹窗不刷新）+ 注入 mock SDK 验证腾讯地图分支（42 条 multi-geometry、多段 paths、GCJ 坐标、42 色、fitBounds、信息窗、2267 站点）。**69 项**，只读不写库 |
| `test_route_shape` | **按确认线路画法（第 51 条）**：静态护栏（`railosm.build` 未改 / 默认 `mode=confirmed` / 缓存键带 mode / 规则 3 的分线优先级顺序 / 站序唯一入口 `via_segment_polyline`）、4 个纯函数单测（长出截断 / 分线归组 / 断口连最近两点 / 含停站优先）、N17·Z75·D40 实测（上千点、里程单调、首尾 = 0 / 全程、终点 0.00 km、无 >60 km 假长直线、尾段不再西绕）、无确认线路整趟回退 legacy、HTTP 两种画法结果不同且不串缓存、Playwright 下拉来回切。**55 项**，只读不写库 |
| `test_route_n17_via` | **N17 真实走向（第 49 条）**：静态护栏（确认站序兜底 / chain 自带站序 / 按线名分组 / 站序二次裁剪 / 站序不可靠跳过 / 20s 缓存）、四段经由链全解析、滨洲线确认版含红旗营而底表版不含、跨线中间站不往南绕、`lineStops` 补出红旗营 / 榆树屯 / 大民屯 / 齐齐哈尔南、末段最大跳 <50km、真实走向终点在齐齐哈尔 5km 内 / cover ≥0.98 / 缺口 ≤10km / 尾段不再到 123.3°E / 无 48km 西绕、Z75·N1534·N1531·G1 不退化、HTTP + Playwright 两种走向切换。**61 项**，只读不写库 |

**跑测试的注意**：脚本会写真实库再撤销，**不要并发跑同一个脚本**（会互相踩，留下孤儿 override）。

**回归脚本不许删真实线路**：要拿真实的线做实验，就**先备份、跑完原样还原**，
不能用「有就删掉」—— 2026-10-30 `test_loose_line` 就这么把用户手工整理的
侯西线（28 站 侯马北→阎良）删了，站序只存在库里，最后靠 WAL 里的旧页捞回来
（`sqlite3` 打不开的话，直接按帧扫 `[{"name": …}]` 的 JSON 也能找）。
现在它开场 `snap_custom()` 备份（站序 + 备注 + 默认来源）、`finally` 里
`restore()` 按原收编路径重建（这样总里程 288.5 这类派生量也不会丢）。
同类脚本 `test_catalog_match` 遇到「侯西线被用户收进库」时跳过站数断言，
别把用户的整理结果当成 bug。

## 8. 踩过的坑（省时间用）
- **串行全量回归的 2 个「冷缓存假红」**（11-12）：`test_code_alias` 用固定
  `wait_for_timeout(2500)` 等搜索结果、`test_curate_refresh` 同样等「重算」回显；
  刚重启服务就整包顺序跑时（55 个脚本约 9 分钟），前几个浏览器脚本偶尔读到上一趟车
  （头部还是 `G1`）或空提示而报红，**单独重跑必绿**。判「零新增回归」看的是既有红
  **12 个集合**（侯西线站数 / `line_ce` 空表 / 曹妃甸 / 成昆·京哈高速 / 404×2 /
  playwright 超时 / `osm_data` NoneType / 无当日在途 / traintrack D2290 /
  京哈线被确认后的 `curate_confirmed_scope` / 南疆线草稿的 `osmedit_multipart`），
  缓存热之后整包复跑一次即回到 12 个既有红。别被这两个固定等待的假红带偏，
  更别为了它们去改业务代码。

- **别把「走向画到某地」写成绝对经度阈值**：`test_route_via_chain` 原来断言
  `lonMax >= 88.9`（11-12 上午 10:45 全量时实际 88.872，5 项假红）。走廊宽度 /
  几何裁剪边界稍有变化，最东点会在 88.87~89.03 之间摆，而 cover 0.995 / 缺口
  3.6 km / 1356 点都正常。现在改成 `LON_TURPAN=88.4` + 「最东点比本趟车停车站
  最东经度多 0.8°」，仍然能抓住「几何被整段裁掉、只剩库尔勒→乌鲁木齐直线」
  （最东 87.52）这种真回归。改阈值不等于修数据，别拿它掩盖覆盖率下降。

- **「数据下载」点开要等 10 秒**（2026-11-05 修）：不是数据重复，是 15 天每天现算一次全集
  （`GROUP BY` 47 万行）+ 待处理（1.7 万次 `NOT EXISTS`）。现在全集落 `train_universe`
  （写 `trains` 时打脏标记，30 秒最多验一次指纹），15 天合成一次批量查询，缓存分忙闲
  （跑任务 2.5s / 闲着 60s），服务启动后台预热。`/api/dates` 0.03s、`/api/history` 0.05s。
  **新代码别再直接 `GROUP BY trains` 或对 `routes` 做 `json_array_length` 全表聚合**
  （停站总数用 `route_tpl.n_stops` 求和，口径已逐日核对一致）。
- 服务是常驻进程：改 `.py` 必须 `taskkill` 后台重发，否则看起来「改了没生效」。
- **`queryTrainInfo` 返回空 ≠ 当天不开行**（2026-11-06 修）：预售期滚动放票、临时加开、
  接口抖动都会给空 body。旧逻辑把空结果永久写进 `day_skip`，后来开行的车再也不会被下载，
  还会在历史页显示成「不开行」（库里 3783 条自相矛盾记录）。现在空要二次确认，
  `day_skip` 带复核期限、下载成功即撤销，详见第 5 节第 30 条。
  **别把 `_pending` 里的 `COALESCE(s.expire,0) > unixepoch('now')` 简化回 `NOT EXISTS`**。
- **人工坐标「保存不下来」的真因是站名两个键**（2026-11-05 杭州东）：档案里同一站常同时有
  `key='杭州东'` 和 `name='杭州东站'`，`coord_override` 存的是 key，而 `station_detail()`
  拿 `st["name"]`（杭州东站）去 `coord_info()` 查 → 命中另一个键 → 读到档案旧值。
  `StationRegistry._coord_key()` 现在**先看 `overrides`**（`杭州东`↔`杭州东站` 来回试），
  人工值永远压过档案。另外 `_apply_coord_overrides()` 会给车站在 `_STATIONS` 上留
  `lat0/lon0`（档案原值），`ensure()` 建 `base_coords` 用它 —— 否则「档案原值」会显示成人工值。
- **基本线路的站坐标不跟随车站库**（同一天发现）：`line_base_stop.lat/lon` 是定稿那天的快照，
  在车站页改完坐标，`/api/line` 的 base 分支还是旧位置。`linebase.detail_payload()` 现在逐站用
  `railcore.stations.coord_info()` 的当前值覆盖（`source=manual` 时带 `coordSrc`）。
- `git push` 必须剥代理，否则 502。
- 底表坐标是假值；直接用来画图北京南会落在山西。
- 线路里程表大量缺站（京沪 150 站只有 58 个里程值）→ 里程必须分档，不能一律当实测。
- SQLite 写锁争用：开 WAL + 批写 + 后台扫描「先算后写、短事务」，否则保存会等到超时。
- 经停站每天整份存会膨胀 5 倍 → 内容按趟存一份（`route_tpl`）+ 每天指针（`route_day`）。
- 前端类名撞车（`.od-row` 被同起讫点列表占用）会把输入框压成 24px；联想下拉写错类名会退化成裸文字。
- `route_stops_brief()` 只给站名不给里程，落站时按**站序比例**算，别按里程算（会得到空结果）。
- 测试并发会留下 `train_override` 孤儿记录，排查数据问题时先看 `source IS NOT NULL` 的近期记录。
- **整理台删线是级联删，WAL 很快被覆盖，别指望「回滚整个库」**（2026-11-06 京沪高速线误删）：
  `/api/ld/delete` 连带清 `ld_stop` / `ld_geom` / `ld_alias`，**但不动 `line_base` /
  `line_base_stop`**（在 rail.db）。先查 `ld_log` 里的 `delete <id>` 定位时间与线名；
  恢复**只补那一条**，整体还原备份会把之后确认的其它线路一起丢掉：
  ① `ld_line` 按 `ld_log` 最后一次 `save` / `finalize` 的状态重建（`status/line_ok/stop_ok/
  src_line/src_stop/geom_src/geom_name`）；② 站序从
  `line_base_stop(base_id=<line_base.id>)` 复制（定稿时同一份，含 km + 经纬度）；
  ③ 别名从 `webapp/data/backup/station-suffix-*/linedesk.db` 复制，
  `ld_alias(source,name)` **有唯一约束**，先查重再插；④ 调 `linedesk.finalize(lid)`
  按 `src_line=route:xxx` 重新生成 `ld_geom`（spine + parts），别手工拼几何。
  写库前先备份 `linedesk.db` + `-wal` + `-shm`。恢复后 `confirmed.py` 的指纹缓存自动失效，
  **不用重启**；但跑完里程类测试后服务可能缓存旧 `line_override`，页面总里程对不上时
  先重启服务再怀疑数据。
- `line_override` 按**线名**索引：整理台里把「京沪高铁」改名「京沪高速线」后，
  旧的人工里程记录就成了孤儿（不报错也不生效）。改名时记得同步迁，或明确告诉用户。

## 9. 还没做 / 待定

- **其余线路的 GeoJSON 还没给**（2026-11-02 用户只给了京沪线一份）—— 导入功能在，
  文件来了就导；一次一条，线名取 `properties.线路名称`，重名自建线会问要不要覆盖。
- **整理库怎么回灌 rail.db 还没定**（用户未拍板）：A 以 cnrail 417 + 自建 62 为骨架 + alias 映射表；
  B 直接以辉睿 844 为骨架；C 新建 canonical line 表。整理台现在只是把关系攒在 `linedesk.db`，
  导出（`/api/ld/export`）能拿到「规范名 ← 各来源写法 + 站序 + 父子」，回灌要等骨架定了再写。
- 整理库 3445 条线里 **1223 条有站序**，其余 2000 多条只有 OSM 线名（有走向没站序）；
  是否按 OSM 走向反查补站 —— 未定。（**2026-11-02 整理库已随线路一起清空**，
  `scripts/build_linedesk.py` 重建要先有来源表：OSM / china-emu 那几张现在都是空的，
  只剩资料库 844 条 —— 所以重建出来只会是资料库那一批。）
- 资料库那 **39 条「小资料挂大干线」** 的历史错配（马坊支线→京哈线、库俄线→南疆线…）仍在 rail.db 里，
  整理台能看到族内多条线，但要不要清空 `line_name` 让 compose 接管 —— 未确认。
- 武冈城际、武石城际的站序一样（`line_catalog_stop` 源头数据错），是否按资料改正 —— 未确认。
- **OSM 站序的方向可能和规范方向相反**：抽取时从「离重心最远的悬挂端点」起算，
  京广线在 OSM 里首站是广州、末站长辛店。现在如实按来源给（来源条 tooltip 里标了首末站），
  要不要加一个「反向」按钮 —— 未定。
- 唐曹线站序从七道桥起、唐山站不在线上，所以 C248「唐山→南堡北」永远要人工填；是否把唐山接进站序 —— 未确认。
- override 是否改成按 `code + 站表版本` 指纹存（调图后自动失效）。
- 孤立坐标「枣强南」、10-30 之后的数据补全。
- `webapp/data/railways.geojson`（326 MB，已 gitignore）是否删除。
- `via_class` 表已废弃但仍在库里（2 行），可 `DROP TABLE`。
- ③ 的 `osm` 来源分支只记 `geom_src='osm'` / `geom_name`，不把 OSM 走向固化进 `ld_geom`，
  主站靠运行时按名解析（见第 5 节第 35 条）；要不要和 `route:rg` 一样落 `ld_geom` 未定。


## 10. 提交记录

看 `git log` 即可（旧版文档里抄录的那批 hash 已过时）。远程 `https://github.com/who7who8who9/railcn-webapp` branch `main`。
**提交 / push 暂停**：改动只留本地，等用户明说「需要提交」再执行；U 盘同步同样暂停。
