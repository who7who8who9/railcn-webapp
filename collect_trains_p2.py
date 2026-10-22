#!/usr/bin/env python3
"""
中国铁路列车信息统计工具（两阶段前缀搜索简化版）

核心思路：
  12306 搜索接口是前缀匹配 —— 搜 "G1" 会返回 G1、G10-G19、G100-G199、G1000-G1999。

  只执行两阶段：
    Phase 1（1位数前缀 1-9）：粗粒度覆盖大面积
      - G1 → G1, G10-G19, G100-G199, G1000-G1999
      - 共 9 次请求/车型

    Phase 2（2位数前缀 10-99）：补缺 4 位数车次
      - G10 → G10, G100-G109, G1000-G1099
      - 共 90 次请求/车型

  不再做逐个补缺，速度最快，适合快速普查。

输出字段：
  发车日期 / 起始站 / 车次 / 终到站 / 停站数 / 列车编号

使用方法：
  python collect_trains_p2.py                      # 全部类型
  python collect_trains_p2.py --types G,D          # 只查高铁、动车
  python collect_trains_p2.py --force              # 强制重新采集

日期范围：默认从今天起连续采集 DAYS_AHEAD 天（默认 14 天）
        可用 --days / --start 命令行参数覆盖，或修改文件顶部对应变量
"""

import argparse
import json
import logging
import os
import random
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import requests
import config
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill

# ============================================================================
# 全局配置
# ============================================================================

# 日志对象
logger = logging.getLogger("trains_p2")

# 所有支持的车次类型（字母前缀 + N 代表纯数字普通列车）
ALL_TRAIN_TYPES = ["G", "D", "C", "Z", "T", "K", "L", "Y", "S", "N"]

# 车次类型中文说明
TYPE_NAMES = {
    "G": "高速动车组（高铁）",
    "D": "动车组（动车）",
    "C": "城际动车组",
    "Z": "直达特快旅客列车",
    "T": "特快旅客列车",
    "K": "快速旅客列车",
    "L": "临时旅客列车",
    "Y": "旅游列车",
    "S": "市郊列车",
    "N": "普通旅客列车（纯数字车次）",
}

# ★★★ 日期范围配置 ★★★
# 从起始日期起，连续采集多少天的数据
DAYS_AHEAD = 14
# 起始日期，格式 "YYYY-MM-DD"；留空字符串则从今天开始
START_DATE = ""
# 标记某日期已采集完成的文件名前缀（实际文件形如 .collect_done_2026-07-24）
# 存在该标记的日期会自动跳过，断点续传仅在"未完成的那一天"内生效，避免重复请求
DONE_MARKER_PREFIX = ".collect_done"

# 断点续传进度文件名前缀（实际文件会自动加上日期，如 collect_p2_progress_2026-07-12.json）
# 这样换日期查询时不会误用旧日期的进度
PROGRESS_FILE_PREFIX = "collect_p2_progress"

# 遇到 403 封禁后的等待时间（秒），修改此处即可调整
BAN_WAIT_SECONDS = 300  # 默认 5 分钟

# Phase 1 单次搜索返回结果达到此数量，才需要进入 Phase 2 搜索对应的 2 位前缀
# 12306 搜索接口单次最多返回 200 条，接近满载说明还有更多车次未返回
FULL_RESULT_THRESHOLD = 195

# ============================================================================
# 工具函数
# ============================================================================

def detect_train_type(train_code: str) -> Optional[str]:
    """
    根据车次号检测所属类型

    参数：
      train_code : 车次号，如 "G1"、"D100"、"1234"

    返回：
      类型字母（G/D/C/Z/T/K/L/Y/S/N），无法识别返回 None
    """
    if not train_code:
        return None
    # 首字母是大写字母 → 对应类型
    if train_code[0].isalpha() and train_code[0].upper() in ALL_TRAIN_TYPES:
        return train_code[0].upper()
    # 首字母是数字 → N 型（纯数字车次）
    if train_code[0].isdigit():
        return "N"
    return None


def extract_trains_from_type(raw_results: List[dict], target_type: str) -> List[dict]:
    """
    从搜索接口返回的原始结果中，筛选属于指定类型的车次并提取字段

    参数：
      raw_results  : 搜索接口返回的 data 数组
      target_type  : 当前正在查询的车型字母，如 "G"

    返回：
      只包含属于 target_type 的车次列表
    """
    results = []
    for item in raw_results:
        code = item.get("station_train_code", "").strip()
        if not code:
            continue
        # 只保留属于当前车型的车次，避免跨类型误收
        if detect_train_type(code) != target_type:
            continue
        results.append({
            "date":               item.get("date", "").strip(),
            "from_station":       item.get("from_station", "").strip(),
            "station_train_code": code,
            "to_station":         item.get("to_station", "").strip(),
            "total_num":          item.get("total_num", "").strip(),
            "train_no":           item.get("train_no", "").strip(),
        })
    return results


def format_duration(seconds: float) -> str:
    """将秒数格式化为 HH:MM:SS 字符串"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def build_prefix(t_type: str, num: int) -> str:
    """
    构建搜索前缀

    参数：
      t_type : 车型字母，N 型为纯数字
      num    : 前缀数字

    返回：
      搜索关键词，如 "G1", "10"
    """
    return str(num) if t_type == "N" else f"{t_type}{num}"


# ============================================================================
# TrainAPI — 12306 搜索接口封装
# ============================================================================

class TrainAPI:
    """
    12306 搜索 API HTTP 客户端
    封装 Session 复用、冷却间隔、自动重试
    """

    def __init__(self):
        # Cookie 缓存（只需获取一次）
        self._cookie_str: Optional[str] = None
        # Session 复用 TCP 连接，减少握手开销
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": config.USER_AGENT})
        # 上次请求完成时间戳，用于冷却计算
        self._last_request_time = 0.0

    def _get_cookie(self) -> str:
        """访问 12306 leftTicket/init 页面获取有效 Cookie"""
        url = f"{config.API_BASE}/otn/leftTicket/init"
        resp = self._session.get(url, timeout=config.REQUEST_TIMEOUT)
        resp.raise_for_status()
        cookies = resp.cookies.get_dict()
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
        if not cookie_str:
            raise RuntimeError("无法从 12306 获取 Cookie")
        return cookie_str

    def _ensure_cookie(self):
        """若 Cookie 未初始化，立即获取"""
        if not self._cookie_str:
            self._cookie_str = self._get_cookie()

    def _get(self, url: str, params: dict = None,
             use_cookie: bool = True, retries: int = None) -> requests.Response:
        """
        带冷却间隔 + 自动重试的 GET 请求封装

        参数：
          url        : 请求 URL
          params     : URL 查询参数字典
          use_cookie : 是否携带 Cookie
          retries    : 最大重试次数，None 则取 config.MAX_RETRIES
        """
        if retries is None:
            retries = config.MAX_RETRIES

        # 冷却间隔：确保两次请求之间至少间隔 config.QUERY_COOLDOWN 秒
        elapsed = time.time() - self._last_request_time
        if elapsed < config.QUERY_COOLDOWN:
            time.sleep(config.QUERY_COOLDOWN - elapsed)

        headers = {}
        if use_cookie:
            self._ensure_cookie()
            headers["Cookie"] = self._cookie_str

        last_err = None
        for attempt in range(retries + 1):
            try:
                resp = self._session.get(
                    url, params=params, headers=headers,
                    timeout=config.REQUEST_TIMEOUT
                )
                resp.raise_for_status()
                self._last_request_time = time.time()
                return resp
            except Exception as e:
                last_err = e
                if attempt < retries:
                    time.sleep(0.5)
        raise last_err

    def search_train_broad(self, keyword: str, date: str, target_type: str) -> List[dict]:
        """
        前缀模糊搜索，返回所有匹配 target_type 的车次

        参数：
          keyword     : 搜索关键词，如 "G1"、"K10"
          date        : 查询日期，格式 "YYYY-MM-DD"
          target_type : 当前车型

        返回：
          匹配的车次列表，无结果时返回 []
        """
        params = {
            "keyword": keyword,
            "date": date.replace("-", ""),  # 搜索接口日期格式：YYYYMMDD
        }
        resp = self._get(
            f"{config.SEARCH_API_BASE}/search/v1/train/search",
            params=params,
            use_cookie=False  # 搜索接口无需 Cookie
        )
        data = resp.json()
        if not data.get("data"):
            return []
        return extract_trains_from_type(data["data"], target_type)


# ============================================================================
# 进度管理
# ============================================================================

def load_progress(progress_file: str) -> Dict[str, bool]:
    """从 JSON 文件加载已完成车次集合，文件不存在或损坏时返回空字典"""
    if os.path.exists(progress_file):
        try:
            with open(progress_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
                elif isinstance(data, list):
                    return {k: True for k in data}
        except Exception:
            pass
    return {}


def save_progress(progress_file: str, done_set: Dict[str, bool]):
    """将已完成车次集合保存到 JSON 进度文件"""
    with open(progress_file, "w", encoding="utf-8") as f:
        json.dump(done_set, f, ensure_ascii=False, indent=2)


# ============================================================================
# Excel 输出
# ============================================================================

# Excel 列定义：(表头, 数据字段名)
COLUMNS = [
    ("发车日期",  "date"),
    ("起始站",    "from_station"),
    ("车次",      "station_train_code"),
    ("终到站",    "to_station"),
    ("停站数",    "total_num"),
    ("列车编号",  "train_no"),
]


def write_excel(all_data: Dict[str, List[dict]], output_file: str):
    """
    将所有车次数据写入同一个 Excel 文件

    参数：
      all_data    : {车型字母: [车次数据列表]}
      output_file : 输出文件完整路径

    Excel 结构：
      - 每个车型一个 Sheet
      - 表头：蓝色背景，白色加粗，居中对齐
    """
    wb = Workbook()
    # 删除默认空白 Sheet
    wb.remove(wb.active)

    # 表头样式
    header_font  = Font(bold=True, color="FFFFFF")
    header_fill  = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    header_align = Alignment(horizontal="center", vertical="center")

    has_data = False

    for train_type, trains in all_data.items():
        if not trains:
            continue  # 该车型无数据，跳过

        ws = wb.create_sheet(title=train_type)
        has_data = True
        ws.sheet_view.showGridLines = False

        # 写表头
        for col_num, (header, _) in enumerate(COLUMNS, 1):
            cell = ws.cell(row=1, column=col_num, value=header)
            cell.font      = header_font
            cell.fill      = header_fill
            cell.alignment = header_align

        # 写数据行
        for row_num, train in enumerate(trains, 2):
            for col_num, (_, key) in enumerate(COLUMNS, 1):
                ws.cell(row=row_num, column=col_num, value=train.get(key, ""))

        # 自动调整列宽
        for col in ws.columns:
            col_letter = col[0].column_letter
            max_len = max(
                (len(str(cell.value)) for cell in col if cell.value),
                default=0
            )
            ws.column_dimensions[col_letter].width = min(max_len + 4, 60)

    if not has_data:
        logger.warning("所有车型均无有效数据，未生成 Excel 文件")
        return

    wb.save(output_file)
    logger.info("Excel 已保存: %s", os.path.basename(output_file))


# ============================================================================
# 两阶段前缀搜索核心逻辑
# ============================================================================

def search_and_collect(
    api: TrainAPI,
    prefix: str,
    date: str,
    t_type: str,
    done_set: Dict[str, bool],
    type_data: List[dict],
) -> tuple:
    """
    搜索一个前缀，将返回的所有新车次加入 done_set 和 type_data

    返回：
      (本次新发现的车次数, API 本次返回的总车次数)
    """
    results = api.search_train_broad(prefix, date, t_type)
    total_returned = len(results)
    new_count = 0
    for item in results:
        code = item["station_train_code"]
        if code not in done_set:
            done_set[code] = True
            type_data.append(item)
            new_count += 1
    return new_count, total_returned


def run_phase(
    api: TrainAPI,
    phase_label: str,
    prefixes: List[int],
    date: str,
    t_type: str,
    done_set: Dict[str, bool],
    type_data: List[dict],
    delay_min: float,
    delay_max: float,
    progress_file: str,
    output_file: str,
    all_data: Dict[str, List[dict]],
) -> tuple:
    """
    执行一个前缀搜索阶段，带进度条和 403 封禁自动等待

    参数：
      phase_label   : 阶段标识，用于日志
      prefixes      : 要搜索的前缀数字列表
      其余参数同 search_and_collect

    返回：
      (本阶段新发现的车次总数, {前缀数字: API返回总车次数})
    """
    total = len(prefixes)
    phase_new = 0
    prefix_counts: Dict[int, int] = {}  # 记录每个前缀的 API 返回总数

    for i, num in enumerate(prefixes):
        prefix = build_prefix(t_type, num)
        done = i + 1

        try:
            new_count, total_returned = search_and_collect(api, prefix, date, t_type, done_set, type_data)
            phase_new += new_count
            prefix_counts[num] = total_returned

            # 单行刷新进度条
            pct = done * 100.0 / total
            bar_w = 30
            filled = int(bar_w * done / total)
            bar = "[" + "=" * filled + " " * (bar_w - filled) + "]"
            print(
                f"\r  {bar} {pct:5.1f}% ({done}/{total})  "
                f"✅新增:{new_count:>4}  📦总计:{len(type_data):>5}  "
                f"返回:{total_returned:>3}  前缀:{prefix:<6}",
                end="", flush=True
            )

        except Exception as e:
            err_str = str(e)
            print()  # 换行

            # 403 封禁：保存进度并倒计时等待
            if "403" in err_str or "Forbidden" in err_str:
                logger.warning("  ⚠️ [%s] 前缀 %s 遇到 403 封禁: %s", phase_label, prefix, e)
                logger.info("  正在保存进度，暂停 %d 秒 (%d 分钟)...",
                            BAN_WAIT_SECONDS, BAN_WAIT_SECONDS // 60)
                save_progress(progress_file, done_set)
                all_data[t_type] = type_data
                write_excel(all_data, output_file)

                for remaining in range(BAN_WAIT_SECONDS, 0, -1):
                    mins, secs = divmod(remaining, 60)
                    print(
                        f"\r  ⏸ 封禁等待中... 剩余 {mins:02d}:{secs:02d}",
                        end="", flush=True
                    )
                    time.sleep(1)
                print()
                logger.info("  ▶ 恢复采集...")
            else:
                # 其他异常：仅记录，继续下一个
                logger.warning("  ⚠️ [%s] 前缀 %s 异常: %s", phase_label, prefix, e)

        # 随机延迟，避免被 12306 封 IP
        if done < total:
            time.sleep(random.uniform(delay_min, delay_max))

    print()  # 阶段结束后换行
    return phase_new, prefix_counts


# ============================================================================
# 单车型采集主流程
# ============================================================================

def collect_one_type(
    api: TrainAPI,
    t_type: str,
    date: str,
    delay_min: float,
    delay_max: float,
    done_set: Dict[str, bool],
    all_data: Dict[str, List[dict]],
    progress_file: str,
    output_file: str,
) -> List[dict]:
    """
    对单个车型执行两阶段前缀搜索

    Phase 1: 1位数前缀 (1-9)    → 最多 9 次请求
    Phase 2: 2位数前缀 (10-99)  → 根据 Phase 1 返回量智能跳过

    优化逻辑：
      Phase 1 中某 1 位前缀（如 G1）返回结果 < FULL_RESULT_THRESHOLD 条，
      说明该前缀下已无更多车次，跳过对应的 2 位前缀（G10-G19）搜索。
      仅当返回量 >= FULL_RESULT_THRESHOLD（接近 200 条上限）时，
      才对对应的 2 位前缀进行细化搜索。

    返回：
      该车型的所有车次数据列表
    """
    type_data: List[dict] = []
    type_start = time.time()

    logger.info("=" * 60)
    logger.info("🚄 车型: %s（%s）", t_type, TYPE_NAMES[t_type])

    # ============== Phase 1: 1位数前缀 (1-9) ==============
    p1 = list(range(1, 10))
    logger.info("-" * 40)
    logger.info("📋 Phase 1: 1位数前缀 (%s - %s)  共 %d 次请求",
                build_prefix(t_type, p1[0]), build_prefix(t_type, p1[-1]), len(p1))
    p1_new, p1_counts = run_phase(
        api, "P1", p1, date, t_type,
        done_set, type_data, delay_min, delay_max,
        progress_file, output_file, all_data
    )
    logger.info("   Phase 1 完成: 新发现 %d 个, 累计 %d 个车次", p1_new, len(type_data))

    # ============== Phase 2: 智能选择 2 位数前缀 ==============
    # 只对 Phase 1 返回量 >= FULL_RESULT_THRESHOLD 的 1 位前缀，展开搜索其 2 位子前缀
    # 例如 G1 返回 210 条（>= 195）→ 搜索 G10-G19
    #      G2 返回  50 条（<  195）→ 跳过 G20-G29
    p2 = []
    skipped_groups = []
    for n in range(1, 10):
        returned = p1_counts.get(n)  # None 表示该前缀出错未拿到数据
        if returned is not None and returned < FULL_RESULT_THRESHOLD:
            # 返回量明确不足，安全跳过
            p2.extend([])  # 不添加
            skipped_groups.append(
                f"{build_prefix(t_type, n)}({returned}条)"
            )
        else:
            # 返回量 >= 阈值，或因出错无数据（保守起见仍搜索）
            p2.extend(range(n * 10, n * 10 + 10))

    if skipped_groups:
        logger.info("   ⏭ 智能跳过 %d 组 2 位前缀（返回量 < %d）: %s",
                    len(skipped_groups), FULL_RESULT_THRESHOLD,
                    ", ".join(skipped_groups))

    if not p2:
        logger.info("-" * 40)
        logger.info("📋 Phase 2: 无需搜索（所有 1 位前缀返回量均 < %d）", FULL_RESULT_THRESHOLD)
    else:
        logger.info("-" * 40)
        logger.info("📋 Phase 2: 2位数前缀  共 %d 次请求（原 90 次, 跳过 %d 次）",
                    len(p2), 90 - len(p2))
        p2_new, _ = run_phase(
            api, "P2", p2, date, t_type,
            done_set, type_data, delay_min, delay_max,
            progress_file, output_file, all_data
        )
        logger.info("   Phase 2 完成: 新发现 %d 个, 累计 %d 个车次", p2_new, len(type_data))

    # ============== 车型总结 ==============
    elapsed = time.time() - type_start
    logger.info("✅ %s 型完成: 共 %d 个车次, 用时 %s",
                t_type, len(type_data), format_duration(elapsed))

    return type_data


# ============================================================================
# 主函数
# ============================================================================

def collect_for_date(
    date: str,
    train_types: List[str],
    delay_min: float,
    delay_max: float,
    force: bool,
    script_dir: str,
) -> Dict[str, List[dict]]:
    """
    采集单个日期的所有车型车次数据

    参数：
      date        : 查询日期 "YYYY-MM-DD"
      train_types : 车型字母列表
      delay_min   : 最小请求间隔（秒）
      delay_max   : 最大请求间隔（秒）
      force       : 是否强制重新采集（忽略断点续传）
      script_dir  : 脚本所在目录，用于输出 Excel 与进度文件

    返回：
      该日期的 {车型字母: [车次数据列表]}
    """
    output_file   = os.path.join(script_dir, f"车次列表_两阶段版_{date}.xlsx")
    progress_file = os.path.join(script_dir, f"{PROGRESS_FILE_PREFIX}_{date}.json")
    done_marker   = os.path.join(script_dir, f"{DONE_MARKER_PREFIX}_{date}")

    # 加载断点续传进度（每个日期独立，互不干扰）
    done_set = {} if force else load_progress(progress_file)

    # 初始化数据结构
    all_data: Dict[str, List[dict]] = {t: [] for t in train_types}

    logger.info("-" * 60)
    logger.info("📅 采集日期  : %s", date)
    logger.info("   输出文件  : %s", os.path.basename(output_file))
    logger.info("   断点续传  : %s",
                "关闭（--force）" if force else f"已开启（已记录 {len(done_set)} 个车次）")
    logger.info("-" * 60)

    # 单日内复用一个 API 实例，减少 Cookie 获取次数
    api = TrainAPI()
    for idx, t_type in enumerate(train_types):
        logger.info("")
        logger.info("▶ [%s] 车型 %d/%d: %s", date, idx + 1, len(train_types), t_type)

        type_data = collect_one_type(
            api=api,
            t_type=t_type,
            date=date,
            delay_min=delay_min,
            delay_max=delay_max,
            done_set=done_set,
            all_data=all_data,
            progress_file=progress_file,
            output_file=output_file,
        )

        all_data[t_type] = type_data

        # 每个车型结束后保存进度 + 刷新 Excel
        save_progress(progress_file, done_set)
        write_excel(all_data, output_file)

    # 标记该日期已采集完成（用于多日循环时跳过已完成的日期）
    try:
        with open(done_marker, "w", encoding="utf-8") as f:
            f.write(date)
    except Exception as e:
        logger.warning("   写入完成标记失败: %s", e)

    return all_data


def main():
    """
    程序入口

    命令行参数：
      --types      车次类型，逗号分隔（默认: 全部）
      --days       连续采集天数（默认: 14，从起始日期起算）
      --start      起始日期 YYYY-MM-DD（默认: 今天）
      --delay-min  最小请求间隔（秒，默认: 5）
      --delay-max  最大请求间隔（秒，默认: 6）
      --force      忽略断点续传与完成标记，重新采集
    """
    parser = argparse.ArgumentParser(description="列车车次采集（两阶段前缀版，支持连续多日）")
    parser.add_argument("--types", type=str, default="G,D,C,Z,T,K,L,Y,S,N",
                        help="车次类型，逗号分隔（默认: 全部）")
    parser.add_argument("--days", type=int, default=DAYS_AHEAD,
                        help=f"连续采集天数（默认: {DAYS_AHEAD}）")
    parser.add_argument("--start", type=str, default=START_DATE,
                        help="起始日期 YYYY-MM-DD（默认: 今天）")
    parser.add_argument("--delay-min", type=float, default=5,
                        help="最小延迟秒数（默认: 5）")
    parser.add_argument("--delay-max", type=float, default=6,
                        help="最大延迟秒数（默认: 6）")
    parser.add_argument("--force", action="store_true",
                        help="强制重新采集，忽略断点续传进度与完成标记")
    args = parser.parse_args()

    # 初始化日志
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    # 解析车型参数
    train_types = [t.strip().upper() for t in args.types.split(",") if t.strip()]
    for t in train_types:
        if t not in ALL_TRAIN_TYPES:
            logger.error("不支持的车次类型: %s（可选: %s）", t, ",".join(ALL_TRAIN_TYPES))
            return

    # 解析天数
    if args.days < 1:
        logger.error("--days 必须 >= 1，当前: %d", args.days)
        return

    # 确定起始日期（--start 优先，否则取今天）
    start_str = args.start.strip()
    if start_str:
        try:
            start_dt = datetime.strptime(start_str, "%Y-%m-%d")
        except ValueError:
            logger.error("--start 日期格式错误，应为 YYYY-MM-DD: %s", start_str)
            return
    else:
        start_dt = datetime.now()

    # 生成日期列表：start_dt 起，连续 args.days 天
    dates = [(start_dt + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(args.days)]

    script_dir = os.path.dirname(os.path.abspath(__file__))
    global_start = time.time()
    grand_total = 0
    grand_by_type: Dict[str, int] = {t: 0 for t in train_types}

    # 启动信息
    logger.info("=" * 60)
    logger.info("🚄 列车车次采集（两阶段前缀版，连续多日）")
    logger.info("   日期范围  : %s ~ %s（共 %d 天）", dates[0], dates[-1], len(dates))
    logger.info("   车型      : %s (%d 种)", ", ".join(train_types), len(train_types))
    logger.info("   延迟      : %.0f-%.0f 秒/请求", args.delay_min, args.delay_max)
    logger.info("   策略      : Phase 1（1位前缀）+ Phase 2（2位前缀智能跳过, 阈值%d条）", FULL_RESULT_THRESHOLD)
    logger.info("   封禁等待  : %d 秒（%d 分钟）", BAN_WAIT_SECONDS, BAN_WAIT_SECONDS // 60)
    logger.info("   跳过已完成: %s", "否（--force）" if args.force else "是（检测到完成标记则跳过）")
    logger.info("=" * 60)

    # 逐日采集
    for date_idx, date in enumerate(dates):
        done_marker = os.path.join(script_dir, f"{DONE_MARKER_PREFIX}_{date}")

        logger.info("")
        logger.info("#" * 60)
        logger.info("## 日期 %d/%d: %s", date_idx + 1, len(dates), date)

        # 跳过已完成的日期（除非 --force）
        if not args.force and os.path.exists(done_marker):
            logger.info("   ✅ 已采集完成（存在完成标记），跳过")
            logger.info("   如需重新采集，请删除 %s 或使用 --force", os.path.basename(done_marker))
            continue

        date_start = time.time()
        all_data = collect_for_date(
            date=date,
            train_types=train_types,
            delay_min=args.delay_min,
            delay_max=args.delay_max,
            force=args.force,
            script_dir=script_dir,
        )
        date_elapsed = time.time() - date_start

        # 累计该日期各车型车次数
        for t in train_types:
            grand_by_type[t] += len(all_data.get(t, []))

        date_total = sum(len(v) for v in all_data.values())
        grand_total += date_total
        logger.info("")
        logger.info("📅 %s 完成: %d 个车次, 用时 %s",
                    date, date_total, format_duration(date_elapsed))

    # 最终统计
    global_elapsed = time.time() - global_start

    logger.info("")
    logger.info("=" * 60)
    logger.info("🏁 全部 %d 天采集完成！", len(dates))
    logger.info("   总用时      : %s", format_duration(global_elapsed))
    logger.info("   总有效车次  : %d 个", grand_total)
    for t_type in train_types:
        logger.info("     %s 型: %d 个", t_type, grand_by_type[t_type])
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
