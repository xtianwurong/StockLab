#!/usr/bin/env python3
"""
==============================================================================
StockLab - 基准指数行业权重同步 CLI (app/scripts/sync_benchmark.py)
==============================================================================

【功能用途】
   把「基准指数成分券权重 × 本地行业映射」聚合出的行业权重快照写入
   reference.benchmark_industry_weights，供 Brinson 归因读取。

   可选阶段：
     --with-mapping      先刷新全市场股票 → 申万行业映射（快照覆盖率的前提）
     --with-sw-indices   同步快照涉及的申万行业指数日线（归因需要行业收益）

【运行方式】
   # 刷新中证系全部基准的权重快照（缺省 000300/000905/000852/000906/000985）
   python app/scripts/sync_benchmark.py sync

   # 只刷一个基准
   python app/scripts/sync_benchmark.py sync --benchmark 000300.SH

   # 先补行业映射再刷快照，并补行业指数日线
   python app/scripts/sync_benchmark.py sync --with-mapping --with-sw-indices

   # 只看本地快照状态（不联网）
   python app/scripts/sync_benchmark.py status

【退出码语义】
   0  全部成功
   1  至少一个基准刷新失败（网络 / 源站改版 / 行业映射为空）
   2  配置或输入错误（基准代码未登记、库路径无效等）
"""

import argparse
import logging
import os
import sys
from datetime import date, timedelta

import pandas as pd

# 项目根目录加入模块搜索路径（与其它 CLI 一致）
sys.path.insert(0, os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from stocklab.common.http_client import install_browser_user_agent
from stocklab.datasource.benchmark_index import CSI_BENCHMARK_CODES
from stocklab.datasource.stock_industry import fetch_stock_industry_mapping
from stocklab.datasource.sw_indices import (
    fetch_sw_index_daily,
    fetch_sw_industry_mapping,
)
from stocklab.facade.benchmark_data import BenchmarkDataFacade
from stocklab.persistence.repository.fund_holding import StockIndustryMappingRepository
from stocklab.persistence.repository.fund_analysis import SWIndexDailyRepository
from stocklab.persistence.repository.benchmark import BenchmarkIndustryWeightRepository
from stocklab.persistence.storage import initialize_database
from stocklab.persistence.storage.duckdb import Database

_logger = logging.getLogger(__name__)

__all__ = [
    "parse_args",
    "sync",
    "show_status",
    "main",
]

_DEFAULT_BENCHMARKS = list(CSI_BENCHMARK_CODES.keys())
_SOURCE = "csindex"
_SW_DAYS = 400   # 行业指数回看区间（覆盖归因窗口 + 缓冲）


def parse_args(argv=None):
    """解析命令行参数（argv 便于测试注入）"""
    parser = argparse.ArgumentParser(
        description=(
            "基准指数行业权重同步工具\n\n"
            "子命令：\n"
            "  sync    抓取成分券权重并聚合为行业权重快照（缺省）\n"
            "  status  查看本地快照状态，不联网"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("command", nargs="?", default="sync",
                        choices=["sync", "status"], help="子命令")
    parser.add_argument("--benchmark", action="append", default=None,
                        help="指定基准指数代码（可重复；缺省为中证系全部基准）")
    parser.add_argument("--with-mapping", action="store_true",
                        help="刷新前先同步全市场股票行业映射（覆盖率前提）")
    parser.add_argument("--with-sw-indices", action="store_true",
                        help="同步快照涉及的申万行业指数日线（含二级行业）")
    parser.add_argument("--level", action="append", type=int, choices=[1, 2],
                        default=None,
                        help="要刷新的行业层级，可重复；缺省 1 和 2 都刷（二级行业归因要用）")
    parser.add_argument("--days", type=int, default=_SW_DAYS,
                        help="行业指数回看天数（默认 %d）" % _SW_DAYS)
    parser.add_argument("--db-path", default=None, help="DuckDB 库路径（缺省读 config.ini）")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不写库")
    return parser.parse_args(argv)


def _resolve_benchmarks(raw_list):
    """把用户输入规范成受支持的基准代码列表；非法输入直接退出码 2"""
    if not raw_list:
        return list(_DEFAULT_BENCHMARKS)

    known = set(CSI_BENCHMARK_CODES) | {c.split(".")[0] for c in CSI_BENCHMARK_CODES}
    resolved = []
    for raw in raw_list:
        code = str(raw).strip().upper()
        plain = code.split(".")[0]
        if code in CSI_BENCHMARK_CODES:
            resolved.append(code)
        elif plain in {c.split(".")[0] for c in CSI_BENCHMARK_CODES}:
            matched = next(c for c in CSI_BENCHMARK_CODES if c.startswith(plain))
            resolved.append(matched)
        else:
            _logger.error("基准 %s 暂不支持：仅中证指数公司发布的指数可取成分权重（可选：%s）",
                          raw, "、".join(_DEFAULT_BENCHMARKS))
            sys.exit(2)
    return list(dict.fromkeys(resolved))


def sync(args) -> int:
    """
    刷新基准行业权重快照

    Returns:
        int: 退出码（0 全部成功 / 1 有基准失败）
    """
    if args.dry_run:
        _logger.info("[dry-run] 计划刷新基准：%s", ", ".join(_resolve_benchmarks(args.benchmark)))
        _logger.info("[dry-run] with_mapping=%s with_sw_indices=%s",
                     args.with_mapping, args.with_sw_indices)
        return 0

    db_path = initialize_database(args.db_path)
    database = Database(db_path)
    facade = BenchmarkDataFacade(database)

    if args.with_mapping:
        _logger.info("=== 刷新股票行业映射 ===")
        mapping = fetch_stock_industry_mapping()
        if mapping.empty:
            _logger.error("行业映射抓取为空，后续聚合覆盖率将极低")
        else:
            written = StockIndustryMappingRepository(database).upsert(mapping)
            _logger.info("行业映射写入 %d 行", written)

    benchmarks = _resolve_benchmarks(args.benchmark)
    failures = []

    levels = sorted(set(args.level)) if args.level else [1, 2]
    for code in benchmarks:
        for level in levels:
            _logger.info("=== 刷新基准 %s 行业权重（level=%d）===", code, level)
            result = facade.refresh_industry_weights(code, level=level)
            meta = result["meta"]
            if result["frame"].empty:
                # 一级失败=本轮失败（主链路）；二级失败只告警，
                # 否则一个二级聚合口径问题会把已经成功的一级同步判成失败。
                if level == 1:
                    _logger.error("基准 %s 刷新失败：%s", code, meta.get("reason"))
                    failures.append(code)
                else:
                    _logger.warning("基准 %s 二级行业刷新失败（不影响一级）：%s",
                                    code, meta.get("reason"))
                continue
            _logger.info(
                "基准 %s level=%d：%d 个行业，权重覆盖率 %.1f%%，披露日 %s，本地行业映射覆盖 %.1f%%",
                code, level, meta["sector_count"], 100.0, meta["as_of_date"],
                meta["coverage"] * 100,
            )
            if level == 1 and meta.get("coverage", 0.0) < 0.9:
                _logger.warning("基准 %s 本地行业映射覆盖率仅 %.1f%%，建议加 --with-mapping 刷新映射",
                                code, meta["coverage"] * 100)

    if args.with_sw_indices:
        _logger.info("=== 同步申万行业指数日线 ===")
        failures.extend(_sync_sw_indices(database, benchmarks, args.days))

    database.close()
    if failures:
        _logger.error("本轮失败基准：%s", "、".join(dict.fromkeys(failures)))
        return 1
    _logger.info("基准权重快照同步完成")
    return 0


def _sync_sw_catalog(database) -> None:
    """
    刷新申万行业指数目录（sw.industry_mapping）

    【为什么要在这儿写】
      /api/sw/indices 与 /api/sw/mapping 读的就是这张表，但全项目**没有任何常驻任务**
      写它 —— 表一直是空的，接口恒回 count=0，页面上「申万指数列表」永远空白。
      与 fund_industry_exposure 是同一类漏写，读写必须成对出现。
    二级行业的 parent_code 依赖 legulegu 接口（偶发失败），失败时父级留空、
    一二级目录照常入库 —— 目录缺父级是降级，目录整表为空才是故障。
    """
    from stocklab.persistence.repository.fund_analysis import SWIndustryMappingRepository

    try:
        catalog = fetch_sw_industry_mapping()
    except Exception as exc:
        _logger.warning("申万行业指数目录刷新失败（不影响指数日线同步）：%s", exc)
        return
    if catalog.empty:
        _logger.warning("申万行业指数目录返回空，/api/sw/indices 仍会是空列表")
        return

    SWIndustryMappingRepository(database).upsert(catalog)
    with_parent = (catalog["level"] == 2).sum()
    filled = ((catalog["level"] == 2) & (catalog["parent_code"].astype(str) != "")).sum()
    _logger.info("申万行业指数目录：%d 条（一级 %d / 二级 %d，二级带父级 %d/%d）",
                 len(catalog),
                 int((catalog["level"] == 1).sum()),
                 int(with_parent), int(filled), int(with_parent))


def _sync_sw_indices(database, benchmarks, days) -> list:
    """把快照里出现过的行业指数日线补全，返回失败的指数代码列表"""
    weight_repo = BenchmarkIndustryWeightRepository(database)
    sw_repo = SWIndexDailyRepository(database)

    _sync_sw_catalog(database)

    # 收集快照里出现过的全部行业（不区分层级）：二级行业归因同样要指数收益
    sectors = set()
    for code in benchmarks:
        for level in (1, 2):
            frame = weight_repo.find_weights(code, level)
            if not frame.empty:
                sectors.update(frame["sector_code"].astype(str).tolist())
    sectors.discard("")

    if not sectors:
        _logger.warning("快照中无行业，跳过行业指数同步")
        return []

    start = (date.today() - timedelta(days=int(days))).isoformat()
    end = date.today().isoformat()
    failures = []

    for sector in sorted(sectors):
        symbol = sector if ".SI" in sector else f"{sector}.SI"
        try:
            frame = fetch_sw_index_daily(symbol, start, end)
            if frame.empty:
                failures.append(symbol)
                continue
            sw_repo.upsert(frame)
        except Exception as exc:
            _logger.warning("行业指数 %s 同步失败：%s", symbol, exc)
            failures.append(symbol)

    _logger.info("行业指数同步：%d 成功 / %d 失败", len(sectors) - len(failures), len(failures))
    return failures


def show_status(args) -> int:
    """打印本地快照状态（不联网）"""
    db_path = initialize_database(args.db_path)
    database = Database(db_path)

    frames = BenchmarkIndustryWeightRepository(database).find_all_benchmarks()
    if frames.empty:
        _logger.info("本地暂无基准行业权重快照，执行 `sync` 生成")
    else:
        _logger.info("本地基准行业权重快照：")
        for row in frames.itertuples():
            _logger.info("  %s level=%s 披露日=%s 行业数=%s",
                         row.benchmark_code, row.level, row.as_of_date, row.sector_count)

    mapping_count = len(StockIndustryMappingRepository(database).find_all())
    _logger.info("本地股票行业映射：%d 只", mapping_count)

    database.close()
    return 0


def main(argv=None) -> int:
    """主入口"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    install_browser_user_agent()
    args = parse_args(argv)

    if args.command == "status":
        return show_status(args)
    return sync(args)


if __name__ == "__main__":
    sys.exit(main())
