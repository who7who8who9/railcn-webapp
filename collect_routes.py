#!/usr/bin/env python3
"""
列车经停站信息批量采集工具

功能说明：
  从已采集的车次列表 Excel 中读取所有车次（含 train_no 列车编号），
  利用 trainclaw.py 中现成的 TrainAPI 和 parse_route 函数，
  逐一查询每趟列车的经停站信息，保存为单个 Excel 文件。

核心优化：
  车次列表中已包含 train_no（列车编号），可以直接跳过搜索步骤，
  用 train_no 直接调用经停站查询接口，每趟车只需 1 次 API 请求。
  （原 trainclaw.py 的 query_route 需要两步：先搜索 train_no，再查经停站）

输出 Excel 字段：
  车次类型 / 车次 / 列车编号 / 发车日期 / 起始站 / 终到站 /
  站序 / 车站名 / 到达时间 / 出发时间 / 停留时长(分钟) / 运行时长

使用方法：
  python collect_routes.py                                    # 默认读取最新的车次列表 Excel
  python collect_routes.py --input 车次列表_两阶段版_2026-07-11.xlsx  # 指定输入文件
  python collect_routes.py --types G,D                        # 只查高铁、动车
  python collect_routes.py --delay-min 2 --delay-max 4        # 设置延迟范围
  python collect_routes.py --ban-wait 300                 # 设置封禁等待 5 分钟
  python collect_routes.py --force                            # 强制重新采集（忽略断点续传）

日期修改：直接修改文件顶部 QUERY_DATE 变量，如 "2026-07-11"
"""

import argparse
import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import requests
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, Alignment, PatternFill

# ============================================================================
# 导入 trainclaw.py 中现成的组件
# ============================================================================
# TrainAPI   : HTTP 客户端（Cookie 管理、重试、冷却）
# parse_route: 经停站数据解析函数
# config     : API 地址、User-Agent、超时等配置
import config
from trainclaw import TrainAPI, parse_route

# ============================================================================
# 全局配置
# ============================================================================

# 日志对象
logger = logging.getLogger("collect_routes")

# ★★★ 直接修改此行来指定查询日期 ★★★
# 格式: "YYYY-MM-DD"，留空字符串则自动从输入 Excel 中读取每趟车的发车日期
QUERY_DATE = ""

# 默认输入 Excel 文件名（如不通过 --input 指定，则自动查找最新的车次列表文件）
DEFAULT_INPUT_PATTERN = "车次列表_两阶段版_"

# 输出 Excel 文件名前缀（自动追加日期）
OUTPUT_PREFIX = "列车经停站_"

# 断点续传进度文件名前缀（实际文件会自动加日期，如 collect_routes_progress_2026-07-12.json）
# 这样换日期查询时不会误用旧日期的进度。进度以 train_no（列车编号）为键。
PROGRESS_FILE_PREFIX = "collect_routes_progress"

# ★★★ 默认请求延迟范围（秒）—— 可通过 --delay-min / --delay-max 命令行参数覆盖 ★★★
DEFAULT_DELAY_MIN = 1.0   # 最小延迟秒数
DEFAULT_DELAY_MAX = 2.0   # 最大延迟秒数（实际延迟在 [min, max] 范围内随机）

# ★★★ 遇到 403 封禁后的等待时间（秒）—— 可通过 --ban-wait 命令行参数覆盖 ★★★
DEFAULT_BAN_WAIT_SECONDS = 120  # 默认 2 分钟

# 批次大小：每采集满 N 趟列车就保存为一个独立 Excel 文件
BATCH_SIZE = 200

# 所有支持的车次类型
ALL_TRAIN_TYPES = ["G", "D", "C", "Z", "T", "K", "L", "Y", "S", "N"]

# 车次类型中文名
TYPE_NAMES = {
    "G": "高铁",
    "D": "动车",
    "C": "城际",
    "Z": "直达",
    "T": "特快",
    "K": "快速",
    "L": "临客",
    "Y": "旅游",
    "S": "市郊",
    "N": "普通",
}



# ============================================================================
# 核心函数：用 train_no 直接查询经停站（跳过搜索步骤）
# ============================================================================

def query_route_by_no(api: TrainAPI, train_no: str, date: str) -> List[dict]:
    """
    用列车编号（train_no）直接查询经停站信息

    这是 trainclaw.py 中 query_route 的优化版：
    原版需要两步（先搜索 train_no，再查经停站），
    本函数跳过第一步，直接用已知的 train_no 查询，节省一次 API 请求。

    参数：
      api      : TrainAPI 实例（提供 Cookie 管理和重试机制）
      train_no : 列车内部编号（如 "24000000G10L"）
      date     : 查询日期，格式 "YYYY-MM-DD"

    返回：
      经停站原始数据列表（每个元素是一个字典，包含站名、到/发时间等）

    异常：
      RuntimeError : 12306 返回无数据或查询失败
    """
    # 构造查询参数 —— 直接调用 12306 经停站查询接口
    route_params = {
        "leftTicketDTO.train_no": train_no,       # 列车内部编号
        "leftTicketDTO.train_date": date,          # 查询日期（YYYY-MM-DD 格式）
        "rand_code": "",                           # 验证码（留空即可）
    }
    # 发送请求（TrainAPI._get 会自动处理 Cookie 和重试）
    resp = api._get(f"{config.API_BASE}/otn/queryTrainInfo/query", params=route_params)
    route_data = resp.json()

    # 检查返回数据是否有效
    if not route_data.get("data", {}).get("data"):
        raise RuntimeError(f"未查询到经停站信息: train_no={train_no}")

    return route_data["data"]["data"]


# ============================================================================
# 停留时长计算
# ============================================================================

def calculate_stay_minutes(arrive_time: str, start_time: str) -> int:
    """
    计算列车在某站的停留时长（分钟）

    规则：
      - 起点站：到达时间为空 → 返回 -1（表示始发站，不停留）
      - 终点站：出发时间为空 → 返回 0（表示终到站，到达即结束）
      - 途经站：出发时间 - 到达时间 = 停留分钟数

    参数：
      arrive_time : 到达时间，格式 "HH:MM"（起点站为空字符串）
      start_time  : 出发时间，格式 "HH:MM"（终点站为空字符串）

    返回：
      停留分钟数（整数）
    """
    # 12306 对空时间的表示可能是 "" 或 "--" 或 "----" 等，统一处理
    def _is_empty_time(t: str) -> bool:
        """判断时间字符串是否为空（空字符串或纯横杠）"""
        return not t or t.replace("-", "").strip() == ""

    # 起点站：没有到达时间
    if _is_empty_time(arrive_time):
        return -1
    # 终点站：没有出发时间
    if _is_empty_time(start_time):
        return 0

    # 途经站：计算停留时长
    try:
        ah, am = map(int, arrive_time.split(":"))
        sh, sm = map(int, start_time.split(":"))
        stay = (sh * 60 + sm) - (ah * 60 + am)
        # 处理跨天情况（如到达 23:50，出发 00:10 → 停留 20 分钟）
        if stay < 0:
            stay += 24 * 60
        return stay
    except (ValueError, IndexError):
        return 0


# ============================================================================
# 读取车次列表 Excel
# ============================================================================

def read_train_list(filepath: str, train_types: List[str]) -> List[dict]:
    """
    从车次列表 Excel 中读取所有车次信息

    Excel 结构：每个车型一个 Sheet，字段为：
      发车日期 / 起始站 / 车次 / 终到站 / 停站数 / 列车编号

    参数：
      filepath    : Excel 文件路径
      train_types : 要读取的车型列表（如 ["G", "D"]），传空列表则读取全部

    返回：
      车次信息字典列表，每个字典包含：
        train_type   : 车次类型（如 "G"）
        date         : 发车日期（原始格式，如 "20260711"）
        from_station : 起始站
        train_code   : 车次号（如 "G1"）
        to_station   : 终到站
        total_stops  : 停站数
        train_no     : 列车编号（如 "24000000G10L"）
    """
    # 打开 Excel 文件（read_only 模式提高读取速度）
    wb = load_workbook(filepath, read_only=True, data_only=True)

    # 确定要读取的 Sheet
    if not train_types:
        # 未指定车型 → 读取所有 Sheet
        sheets_to_read = wb.sheetnames
    else:
        # 只读取指定车型的 Sheet
        sheets_to_read = [t for t in train_types if t in wb.sheetnames]

    all_trains = []  # 存放所有车次信息

    for sheet_name in sheets_to_read:
        ws = wb[sheet_name]
        rows = list(ws.iter_rows(min_row=1, values_only=True))

        if not rows:
            continue

        # 第一行是表头，从第二行开始读取数据
        for row in rows[1:]:
            if not row or not row[2]:  # 车次为空则跳过
                continue

            all_trains.append({
                "train_type": sheet_name,          # 车型（Sheet 名）
                "date": str(row[0] or ""),         # 发车日期
                "from_station": str(row[1] or ""), # 起始站
                "train_code": str(row[2] or ""),   # 车次号
                "to_station": str(row[3] or ""),   # 终到站
                "total_stops": str(row[4] or ""),  # 停站数
                "train_no": str(row[5] or ""),     # 列车编号
            })

    wb.close()
    return all_trains


# ============================================================================
# 车次号合并（同 train_no 但不同车次号因方向/上下行导致）
# ============================================================================

def merge_train_codes(codes: List[str]) -> str:
    """
    将同一列车编号的多个车次号合并为统一格式

    合并规则：
      - 单号不合并：1461 → "1461"
      - 相邻号合并：1461, 1464 → "1461/4"
      - 非相邻号合并：1449, 1450 → "1449/50"
      - 高低号合并：K1808, K1805 → "K1808/5"
      - 高号在前（列车编号对应的首个车次号为主号）

    参数：
      codes : 同一 train_no 下的所有车次号列表

    返回：
      合并后的车次号字符串
    """
    if len(codes) == 1:
        return codes[0]

    # 按车次号排序（保持原有顺序，取第一个为主号）
    first = codes[0]   # 主号（通常是从 train_no 列表中按序拿到的第一个）
    last  = codes[-1]

    # 找到从左侧开始的公共前缀
    common_len = 0
    for a, b in zip(first, last):
        if a == b:
            common_len += 1
        else:
            break

    common_prefix = first[:common_len]

    # 把数字部分按首位的 padding 去掉（如 "08" 变为 "8" 显示）
    last_suffix = last[common_len:]

    # 如果后缀全是数字，去掉前导零使显示更简洁
    if last_suffix.isdigit() and len(last_suffix) > 1:
        last_suffix = str(int(last_suffix))

    return f"{first}/{last_suffix}"


def merge_train_list(trains: List[dict]) -> List[dict]:
    """
    按列车编号（train_no）合并车次列表

    同一 train_no 可能有多个车次号（列车换向后车次号变化），
    合并为一条记录并生成统一的车次号显示（如 "1461/4"）。

    参数：
      trains : 原始车次列表（来自 read_train_list）

    返回：
      合并后的车次列表（去重，每 train_no 一行）
    """
    # 按 train_no 分组
    groups: Dict[str, List[dict]] = {}
    for t in trains:
        tn = t["train_no"]
        if tn not in groups:
            groups[tn] = []
        groups[tn].append(t)

    merged = []
    multi_code_count = 0  # 统计发生了合并的组数

    for train_no, group in groups.items():
        # 取第一个元素作为基础信息
        base = dict(group[0])

        # 收集该组所有不同的车次号
        all_codes = []
        seen_codes = set()
        for t in group:
            code = t["train_code"]
            if code not in seen_codes:
                all_codes.append(code)
                seen_codes.add(code)

        # 合并车次号
        merged_code = merge_train_codes(all_codes)
        base["train_code"] = merged_code

        # 如果原始停站数不同（取最大的），这里用第一个为准即可
        # 如果起始/终到站不同，保持第一个的（通常相同）

        merged.append(base)

        if len(all_codes) > 1:
            multi_code_count += 1

    if multi_code_count > 0:
        logger.info("共有 %d 个列车编号涉及多个车次号（已合并显示）", multi_code_count)

    return merged


# ============================================================================
# 进度条显示 & 批次管理
# ============================================================================

# ---- 车次号自然排序 ----
# 用于对 "G1", "G10", "G100", "G2", "1461" 等车次号进行自然排序
def _train_code_key(train_code: str) -> tuple:
    """提取车次号的排序键：(类型字母, 数字部分)，如 G1 → ('G', 1)"""
    m = re.match(r"([A-Za-z]*)(\d+)$", train_code.strip())
    if m:
        return (m.group(1).upper(), int(m.group(2)))
    return ("", 0)


def build_batch_filename(
    batch_rows: List[dict],
    date: str,
    batch_num: int,
    script_dir: str,
) -> str:
    """
    根据批次内容生成带语义的文件名

    命名规则：列车经停站_{车型}_{起始车次}_{结束车次}_{日期}_p{批次号}.xlsx
    示例：列车经停站_G_G1_G200_20260711_p1.xlsx

    参数：
      batch_rows : 本批次的经停站数据行列表
      date       : 查询日期 "YYYY-MM-DD"
      batch_num  : 批次序号（从 1 开始）
      script_dir : 输出目录（脚本所在目录）

    返回：
      完整的输出文件路径
    """
    # 收集本批次中唯一的车次号，按自然顺序排列
    train_codes = sorted(set(r["train_code"] for r in batch_rows), key=_train_code_key)

    if not train_codes:
        start_code = end_code = "未知"
    else:
        start_code = train_codes[0]
        end_code = train_codes[-1]

    # 统计车次类型（取本批次出现最多的类型作为主类型标识）
    type_counts: Dict[str, int] = {}
    for r in batch_rows:
        tt = r["train_type"]
        type_counts[tt] = type_counts.get(tt, 0) + 1
    # 最多取前两种类型
    top_types = sorted(type_counts.items(), key=lambda x: -x[1])[:2]
    type_str = "_".join(t for t, _ in top_types)

    date_tag = date.replace("-", "")  # 2026-07-11 → 20260711

    # Windows 文件名不能含 /，合并车次（如 T301/4）中的 / 替换为 -
    safe_start = start_code.replace("/", "-")
    safe_end = end_code.replace("/", "-")
    filename = f"{OUTPUT_PREFIX}{type_str}_{safe_start}_{safe_end}_{date_tag}_p{batch_num}.xlsx"
    return os.path.join(script_dir, filename)


def format_duration(seconds: float) -> str:
    """将秒数格式化为 HH:MM:SS"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def print_progress(done: int, total: int, success: int, fail: int,
                   total_stops: int, start_time: float, force_flush: bool = False):
    """
    单行刷新进度条

    参数：
      done        : 已完成数
      total       : 总数
      success     : 成功数
      fail        : 失败数
      total_stops : 累计采集站点数
      start_time  : 开始时间戳
      force_flush : 是否强制换行（阶段结束时使用）
    """
    pct = done / total * 100 if total > 0 else 0
    elapsed = time.time() - start_time
    rate = done / elapsed if elapsed > 0 else 0
    remaining = (total - done) / rate if rate > 0 else 0

    # 生成文本进度条（40 格）
    bar_len = 40
    filled = int(bar_len * done / total) if total > 0 else 0
    bar = "█" * filled + " " * (bar_len - filled)

    # 单行刷新（\r 回到行首覆盖）
    end_char = "\n" if force_flush else ""
    print(
        f"\r[{bar}] {pct:5.1f}%  ({done}/{total})  "
        f"✅{success}  ❌{fail}  站点:{total_stops}  "
        f"速度:{rate:.1f}个/秒  "
        f"已用:{format_duration(elapsed)}  "
        f"剩余:{format_duration(remaining)}",
        end=end_char, flush=True
    )


# ============================================================================
# Excel 写入
# ============================================================================

def write_excel(all_rows: List[dict], output_file: str):
    """
    将所有经停站数据写入 Excel 文件

    参数：
      all_rows    : 数据行列表，每个字典包含：
                      train_type / train_code / train_no / date /
                      from_station / to_station / stop_seq /
                      station_name / arrive_time / start_time /
                      stay_minutes / running_time
      output_file : 输出文件路径
    """
    wb = Workbook()
    # 删除默认创建的空 Sheet
    wb.remove(wb.active)

    # 创建数据 Sheet
    ws = wb.create_sheet(title="经停站信息")
    ws.sheet_view.showGridLines = False  # 隐藏网格线

    # ---- 写表头 ----
    headers = [
        "车次类型", "车次", "列车编号", "发车日期",
        "起始站", "终到站", "站序", "车站名",
        "到达时间", "出发时间", "停留时长(分钟)", "运行时长", "到达日期"
    ]
    header_font = Font(name="微软雅黑", size=10, bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    header_align = Alignment(horizontal="center", vertical="center")

    for col_idx, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_align

    # ---- 写数据行 ----
    data_font = Font(name="微软雅黑", size=9)
    data_align = Alignment(horizontal="center", vertical="center")

    for row_idx, row_data in enumerate(all_rows, 2):
        values = [
            row_data["train_type"],
            row_data["train_code"],
            row_data["train_no"],
            row_data["date"],
            row_data["from_station"],
            row_data["to_station"],
            row_data["stop_seq"],
            row_data["station_name"],
            row_data["arrive_time"],
            row_data["start_time"],
            row_data["stay_minutes"],
            row_data["running_time"],
            row_data["arrive_day"],
        ]
        for col_idx, val in enumerate(values, 1):
            cell = ws.cell(row=row_idx, column=col_idx, value=val)
            cell.font = data_font
            cell.alignment = data_align

    # ---- 自动调整列宽 ----
    for col_idx in range(1, len(headers) + 1):
        max_len = len(str(headers[col_idx - 1]))
        for row_idx in range(2, min(len(all_rows) + 2, 200)):  # 采样前 200 行估算
            val = ws.cell(row=row_idx, column=col_idx).value
            if val:
                max_len = max(max_len, len(str(val)))
        col_letter = ws.cell(row=1, column=col_idx).column_letter
        ws.column_dimensions[col_letter].width = min(max_len + 4, 40)

    # ---- 冻结首行 ----
    ws.freeze_panes = "A2"

    # ---- 保存 Excel（带文件占用容错）----
    saved = False
    for attempt in range(5):
        try:
            wb.save(output_file)
            saved = True
            break
        except PermissionError:
            if attempt < 4:
                logger.warning(
                    "⚠️ 文件被占用（%s），3秒后重试 (%d/5)...",
                    os.path.basename(output_file), attempt + 1
                )
                time.sleep(3)
            else:
                # 5 次都失败，换个文件名保存
                base, ext = os.path.splitext(output_file)
                counter = 2
                while os.path.exists(f"{base}_{counter}{ext}"):
                    counter += 1
                fallback_file = f"{base}_{counter}{ext}"
                logger.warning(
                    "⚠️ 原文件被占用，保存至: %s",
                    os.path.basename(fallback_file)
                )
                wb.save(fallback_file)
                saved = True

    if not saved:
        raise PermissionError(
            f"无法保存文件，请关闭 Excel 后重试: {output_file}"
        )


# ============================================================================
# 断点续传
# ============================================================================

def load_progress(progress_file: str) -> set:
    """加载已完成的车次号集合"""
    if os.path.exists(progress_file):
        try:
            with open(progress_file, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except (json.JSONDecodeError, IOError):
            pass
    return set()


def save_progress(progress_file: str, done_set: set):
    """保存已完成的车次号集合"""
    with open(progress_file, "w", encoding="utf-8") as f:
        json.dump(sorted(list(done_set)), f, ensure_ascii=False)


# ============================================================================
# 主采集逻辑
# ============================================================================

def collect_routes(
    trains: List[dict],
    date: str,
    delay_min: float,
    delay_max: float,
    ban_wait_seconds: int,
    script_dir: str,
    progress_file: str,
    force: bool,
):
    """
    主采集函数：遍历所有车次，逐一查询经停站，分批保存

    每采集满 BATCH_SIZE（默认 200）趟列车，就保存为一个独立 Excel 文件。
    文件名体现车次类型、起止车次号和发车日期。

    参数：
      trains           : 车次列表（来自 read_train_list + merge_train_list）
      date             : 查询日期 "YYYY-MM-DD"
      delay_min/max    : 请求间随机延迟范围（秒）
      ban_wait_seconds : 403 封禁后等待秒数
      script_dir       : 脚本所在目录（用于生成输出路径）
      progress_file    : 断点续传文件路径
      force        : 是否忽略断点续传，从头开始
    """
    total = len(trains)
    logger.info("共 %d 趟列车需要采集经停站信息", total)
    logger.info("每 %d 趟保存一个独立文件", BATCH_SIZE)

    # ---- 加载断点续传进度 ----
    if force:
        done_set = set()
        logger.info("已启用 --force，从头开始采集")
    else:
        done_set = load_progress(progress_file)
        if done_set:
            logger.info("断点续传：已完成 %d 趟，剩余 %d 趟", len(done_set), total - len(done_set))
        else:
            logger.info("无历史进度，从头开始采集")

    # ---- 创建 TrainAPI 实例（复用 Cookie 和 Session）----
    api = TrainAPI()
    logger.info("已创建 TrainAPI 实例，Cookie 将在首次请求时自动获取")

    # ---- 统计变量 ----
    success_count = 0      # 成功采集的车次数
    fail_count = 0         # 失败的车次数
    skip_count = 0         # 跳过的车次数（断点续传）
    total_stops = 0        # 累计采集的站点总数

    # ---- 批次管理 ----
    current_batch_rows: List[dict] = []   # 当前批次的数据行
    current_batch_trains = 0              # 当前批次已累计的车次数
    batch_num = 1                         # 批次序号（从 1 开始）
    batch_files: List[str] = []           # 所有已保存的批次文件路径
    batch_start_code = ""                 # 当前批次的起始车次（用于文件名）

    start_time = time.time()
    done = 0               # 已处理数（含跳过）

    # ---- 逐个采集 ----
    for train in trains:
        train_code = train["train_code"]
        train_no = train["train_no"]

        # 断点续传：跳过已查询过的列车编号
        if train_no in done_set:
            skip_count += 1
            done += 1
            if done % 10 == 0 or done == total:
                print_progress(done, total, success_count, fail_count,
                               total_stops, start_time)
            continue

        # ---- 查询经停站 ----
        try:
            # 用 train_no 直接查询（跳过搜索步骤）
            raw_stops = query_route_by_no(api, train_no, date)
            # 解析经停站数据（复用 trainclaw.py 的 parse_route）
            stops = parse_route(raw_stops)

            # 将每个站点信息整理为输出行
            for seq, stop in enumerate(stops, 1):
                stay_min = calculate_stay_minutes(stop.arrive_time, stop.start_time)
                current_batch_rows.append({
                    "train_type": train["train_type"],
                    "train_code": train_code,
                    "train_no": train_no,
                    "date": train["date"],
                    "from_station": train["from_station"],
                    "to_station": train["to_station"],
                    "stop_seq": seq,
                    "station_name": stop.station_name,
                    "arrive_time": stop.arrive_time or "--",
                    "start_time": stop.start_time or "--",
                    "stay_minutes": stay_min,
                    "running_time": stop.lishi or "",
                    "arrive_day": stop.arrive_day_str or "",  # 当日到达/次日到达/三日到达
                })

            total_stops += len(stops)
            success_count += 1
            current_batch_trains += 1
            done_set.add(train_no)  # 以列车编号为键（合并后唯一）

            # 保存进度（每 20 趟保存一次）
            if success_count % 20 == 0:
                save_progress(progress_file, done_set)

            # ---- 批次保存：每满 BATCH_SIZE 趟列车，保存为独立文件 ----
            if current_batch_trains >= BATCH_SIZE:
                batch_file = build_batch_filename(
                    current_batch_rows, date, batch_num, script_dir
                )
                write_excel(current_batch_rows, batch_file)
                batch_files.append(batch_file)
                logger.info(
                    "  📦 批次 %d 已保存: %s (%d 趟, %d 行数据)",
                    batch_num, os.path.basename(batch_file),
                    current_batch_trains, len(current_batch_rows)
                )
                # 重置批次
                current_batch_rows = []
                current_batch_trains = 0
                batch_num += 1

        except Exception as e:
            err_msg = str(e)

            # ---- 403 封禁处理 ----
            if "403" in err_msg or "Forbidden" in err_msg:
                fail_count += 1
                logger.warning("\n  ⛔ %s 遭遇封禁(403)，保存进度后等待 %d 秒...",
                               train_code, ban_wait_seconds)
                # 先保存进度和数据，防止丢失
                save_progress(progress_file, done_set)
                if current_batch_rows:
                    batch_file = build_batch_filename(
                        current_batch_rows, date, batch_num, script_dir
                    )
                    write_excel(current_batch_rows, batch_file)
                    batch_files.append(batch_file)
                    logger.info("  📦 封禁前批次已保存: %s", os.path.basename(batch_file))

                # 倒计时等待
                for remaining in range(ban_wait_seconds, 0, -1):
                    mins, secs = divmod(remaining, 60)
                    print(f"\r  ⏸ 封禁等待中... 剩余 {mins:02d}:{secs:02d}",
                          end="", flush=True)
                    time.sleep(1)
                print()  # 换行
                logger.info("  ▶ 恢复采集...")

                # 重新创建 API 实例（刷新 Cookie）
                api = TrainAPI()

            else:
                # ---- 其他异常（超时、无数据等）----
                fail_count += 1
                if done % 50 == 0 or done < 5:
                    logger.debug("  ⚠️ %s 失败: %s", train_code, err_msg)

        done += 1

        # ---- 进度条刷新 ----
        if done % 10 == 0 or done == total:
            print_progress(done, total, success_count, fail_count,
                           total_stops, start_time)

        # ---- 随机延迟 ----
        delay = random.uniform(delay_min, delay_max)
        time.sleep(delay)

    # ---- 最终输出 ----
    print()  # 进度条换行

    # 保存最后一批（不足 BATCH_SIZE 的剩余数据）
    if current_batch_rows:
        batch_file = build_batch_filename(
            current_batch_rows, date, batch_num, script_dir
        )
        write_excel(current_batch_rows, batch_file)
        batch_files.append(batch_file)
        logger.info("  📦 最后批次已保存: %s (%d 趟, %d 行数据)",
                    os.path.basename(batch_file),
                    current_batch_trains, len(current_batch_rows))

    if not batch_files:
        logger.warning("⚠️ 未采集到任何经停站数据，不生成 Excel 文件")

    # 保存最终进度
    save_progress(progress_file, done_set)

    # 打印最终统计
    elapsed = time.time() - start_time
    logger.info("=" * 60)
    logger.info("🎉 采集完成！")
    logger.info("   总车次数  : %d", total)
    logger.info("   成功采集  : %d", success_count)
    logger.info("   跳过(续传): %d", skip_count)
    logger.info("   失败      : %d", fail_count)
    logger.info("   站点总数  : %d", total_stops)
    logger.info("   总耗时    : %s", format_duration(elapsed))
    logger.info("   生成文件  : %d 个批次", len(batch_files))
    for f in batch_files:
        logger.info("     - %s", os.path.basename(f))
    logger.info("=" * 60)


# ============================================================================
# 自动查找最新车次列表 Excel
# ============================================================================

def find_latest_excel(directory: str, pattern: str) -> Optional[str]:
    """
    在指定目录下查找文件名以 pattern 开头的最新 Excel 文件

    参数：
      directory : 搜索目录
      pattern   : 文件名前缀

    返回：
      找到的文件完整路径，未找到返回 None
    """
    candidates = []
    for fname in os.listdir(directory):
        if fname.startswith(pattern) and fname.endswith(".xlsx"):
            full_path = os.path.join(directory, fname)
            mtime = os.path.getmtime(full_path)
            candidates.append((mtime, full_path))

    if candidates:
        # 按修改时间降序，取最新的
        candidates.sort(reverse=True)
        return candidates[0][1]
    return None


# ============================================================================
# 主函数
# ============================================================================

def main():
    """程序入口：解析命令行参数，执行采集"""
    parser = argparse.ArgumentParser(
        description="列车经停站信息批量采集工具"
    )
    parser.add_argument("--input", type=str, default=None,
                        help="车次列表 Excel 文件路径（默认自动查找最新）")
    parser.add_argument("--types", type=str, default=None,
                        help="只采集指定车型，逗号分隔（如 G,D,C）。默认全部")
    parser.add_argument("--delay-min", type=float, default=DEFAULT_DELAY_MIN,
                        help=f"最小延迟秒数（默认: {DEFAULT_DELAY_MIN}）")
    parser.add_argument("--delay-max", type=float, default=DEFAULT_DELAY_MAX,
                        help=f"最大延迟秒数（默认: {DEFAULT_DELAY_MAX}）")
    parser.add_argument("--ban-wait", type=int, default=DEFAULT_BAN_WAIT_SECONDS,
                        help=f"403 封禁等待秒数（默认: {DEFAULT_BAN_WAIT_SECONDS}）")
    parser.add_argument("--force", action="store_true",
                        help="强制重新采集（忽略断点续传）")
    args = parser.parse_args()

    # ---- 配置日志 ----
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )

    # ---- 脚本所在目录（用于查找输入文件和保存输出）----
    script_dir = os.path.dirname(os.path.abspath(__file__))

    # ---- 确定输入 Excel 文件 ----
    if args.input:
        # 用户指定了文件
        input_file = args.input
        if not os.path.isabs(input_file):
            input_file = os.path.join(script_dir, input_file)
    else:
        # 自动查找最新的车次列表文件
        input_file = find_latest_excel(script_dir, DEFAULT_INPUT_PATTERN)
        if not input_file:
            # 尝试查找其他车次列表文件
            for prefix in ["车次列表_对照版_", "车次列表_智能版_", "车次列表_快速版_"]:
                input_file = find_latest_excel(script_dir, prefix)
                if input_file:
                    break

    if not input_file or not os.path.exists(input_file):
        logger.error("❌ 未找到车次列表 Excel 文件！")
        logger.error("   请通过 --input 参数指定，或将文件放在脚本同目录下")
        sys.exit(1)

    logger.info("输入文件: %s", os.path.basename(input_file))

    # ---- 确定查询日期 ----
    if QUERY_DATE:
        date = QUERY_DATE
    else:
        # 从输入文件名中提取日期（如 "车次列表_两阶段版_2026-07-11.xlsx" → "2026-07-11"）
        fname = os.path.basename(input_file)
        import re
        m = re.search(r"(\d{4}-\d{2}-\d{2})", fname)
        date = m.group(1) if m else (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    logger.info("查询日期: %s", date)

    # ---- 解析车型参数 ----
    if args.types:
        train_types = [t.strip().upper() for t in args.types.split(",")]
    else:
        train_types = []  # 空列表表示全部

    # ---- 读取车次列表 ----
    logger.info("正在读取车次列表...")
    trains = read_train_list(input_file, train_types)

    if not trains:
        logger.error("❌ 未从 Excel 中读取到任何车次数据！")
        sys.exit(1)

    # ---- 合并相同 train_no 的不同车次号 ----
    trains = merge_train_list(trains)

    # 按车型统计
    type_counts = {}
    for t in trains:
        tt = t["train_type"]
        type_counts[tt] = type_counts.get(tt, 0) + 1
    type_summary = ", ".join(f"{k}:{v}" for k, v in sorted(type_counts.items()))
    logger.info("共读取 %d 趟列车 (%s)", len(trains), type_summary)

    # ---- 确定输出目录和进度文件 ----
    # 进度文件按日期隔离，换日期不会误用旧进度
    progress_file = os.path.join(script_dir, f"{PROGRESS_FILE_PREFIX}_{date}.json")

    # ---- 打印启动信息 ----
    logger.info("=" * 60)
    logger.info("🚄 列车经停站信息批量采集")
    logger.info("   输入文件  : %s", os.path.basename(input_file))
    logger.info("   查询日期  : %s", date)
    logger.info("   车次总数  : %d", len(trains))
    logger.info("   延迟      : %.0f-%.0f 秒/请求", args.delay_min, args.delay_max)
    logger.info("   封禁等待  : %d 秒", args.ban_wait)
    logger.info("   批次大小  : %d 趟/文件", BATCH_SIZE)
    logger.info("   断点续传  : %s",
                "关闭（--force）" if args.force else "已开启")
    logger.info("=" * 60)

    # ---- 执行采集 ----
    collect_routes(
        trains=trains,
        date=date,
        delay_min=args.delay_min,
        delay_max=args.delay_max,
        ban_wait_seconds=args.ban_wait,
        script_dir=script_dir,
        progress_file=progress_file,
        force=args.force,
    )


if __name__ == "__main__":
    main()
