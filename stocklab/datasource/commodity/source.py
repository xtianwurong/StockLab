#!/usr/bin/env python3
"""
==============================================================================
StockLab - 大宗商品采集 (stocklab.datasource.commodity.source)
==============================================================================

【模块职责】
   从 akshare 取主力连续合约日线，归一成 commodity.price_history 的契约列序。
   只出不进：本模块不知道表长什么样，也不知道「分位」是什么。

【akshare 是延迟导入的】
   akshare 这个包 import 一次要一两秒、且会拖进一串传递依赖。web 服务全程
   不需要它（只读库），所以 import 放在函数体内 —— 否则每次启动服务都要
   为一个只在手动同步时才用到的库付出启动成本。

【归一化为什么在这里而不是 repository】
   源站给的是「日期 / 开盘价 / 最高价 / 最低价 / 收盘价 / 成交量 / 持仓量 /
   动态结算价」，契约要的是 symbol / trade_date / close / source / fetched_at。
   中间必须有人做列名翻译、类型转换、脏行剔除。这一层属于「理解源站」，
   属于 datasource；repository 只负责「理解表」。两者一旦合流，
   源站改列名就会变成一次表结构改动。
"""

import logging
import math
from datetime import datetime

import pandas as pd

from stocklab.domain import COMMODITY_PRICE_COLUMNS
from stocklab.datasource.commodity.registry import (
    CommoditySpec,
    SOURCE_AKSHARE_SINA_MAIN,
)

_logger = logging.getLogger(__name__)

__all__ = [
    "CommoditySourceError",
    "fetch_main_history",
]


# 源站列名 → 语义（只取用得到的两列，其余列一律丢弃，不固化进表结构）
_DATE_COLUMN = "日期"
_CLOSE_COLUMN = "收盘价"

# 单次抓取失败后的重试次数（新浪偶发连接重置，一次就放弃会白白丢一个品种）
_RETRIES = 2
_RETRY_BACKOFF_SECONDS = 2.0


class CommoditySourceError(RuntimeError):
    """采集失败（无数据 / 源站改版 / 网络不可达）—— 与「抓到了但没有新数据」不同"""


def fetch_main_history(spec):
    """
    抓取单个品种的主力连续日线

    Args:
        spec (CommoditySpec): 品种登记项

    Returns:
        pd.DataFrame: 契约列序 [symbol, trade_date, close, source, fetched_at]，
                      按 trade_date 升序，无重复日期

    Raises:
        CommoditySourceError: 源站无响应、列名对不上、或归一化后一条不剩
    """
    if not isinstance(spec, CommoditySpec):
        raise CommoditySourceError("传入的不是 CommoditySpec: %r" % (spec,))
    if not spec.is_active:
        raise CommoditySourceError("品种 [%s] 已停用，跳过采集" % spec.symbol)

    # 延迟导入：见模块说明
    import akshare as ak

    raw = None
    last_error = None
    for attempt in range(_RETRIES + 1):
        try:
            raw = ak.futures_main_sina(symbol=spec.source_symbol)
            break
        except Exception as error:  # akshare 抛的异常类型不稳定，统一按源站故障处理
            last_error = error
            if attempt < _RETRIES:
                import time
                time.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))

    if raw is None:
        raise CommoditySourceError(
            "品种 [%s %s] 采集失败（%s 次重试后仍失败）: %s"
            % (spec.name, spec.source_symbol, _RETRIES, last_error)
        )

    missing = [column for column in (_DATE_COLUMN, _CLOSE_COLUMN)
               if column not in raw.columns]
    if missing:
        # 源站改版会走到这里。必须显式失败而不是猜列 —— 猜中了会把开盘价
        # 当收盘价写进库，数字看着完全正常，没有任何信号表明它错了。
        raise CommoditySourceError(
            "品种 [%s %s] 源站缺列 %s，实际列名: %s"
            % (spec.name, spec.source_symbol, missing, list(raw.columns))
        )

    if raw.empty:
        raise CommoditySourceError(
            "品种 [%s %s] 源站返回 0 行" % (spec.name, spec.source_symbol)
        )

    return _normalize(spec, raw)


def _normalize(spec, raw):
    """把源站 DataFrame 归一成契约列序，并剔除无法用于分位计算的脏行"""

    frame = raw.loc[:, [_DATE_COLUMN, _CLOSE_COLUMN]].copy()

    # 日期：源站给的是 YYYY-MM-DD 文本；解析失败的行直接丢弃（缺日期的
    # 价格行无法参与「按时间的分位」，留着反而会破坏排序与去重）
    frame["trade_date"] = pd.to_datetime(
        frame[_DATE_COLUMN], errors="coerce"
    ).dt.date
    # 收盘价：非数值（空串、"—"、"—"）转成 NaN 后一并剔除
    frame["close"] = pd.to_numeric(frame[_CLOSE_COLUMN], errors="coerce")

    frame = frame[frame["trade_date"].notna() & frame["close"].notna()]
    # 分位的口径是「严格小于当前值的样本占比」，非正价格没有大小关系可言，
    # 且源站换月回填时会写 0；负价在期货上不出现，出现即数据错。
    frame = frame[frame["close"].map(
        lambda value: math.isfinite(value) and value > 0
    )]

    if frame.empty:
        raise CommoditySourceError(
            "品种 [%s %s] 归一化后 0 条有效行（源站 %d 行全部被剔除）"
            % (spec.name, spec.source_symbol, len(raw))
        )

    # 同日重复保留最后一条（源站对换月回填会给同一日期两行）
    frame = frame.drop_duplicates(subset=["trade_date"], keep="last")

    now = datetime.now()
    frame = pd.DataFrame({
        "symbol": spec.symbol,
        "trade_date": frame["trade_date"],
        "close": frame["close"].astype(float),
        "source": SOURCE_AKSHARE_SINA_MAIN,
        "fetched_at": now,
    })
    frame = frame.sort_values("trade_date").reset_index(drop=True)
    frame = frame.loc[:, list(COMMODITY_PRICE_COLUMNS)]

    _logger.info(
        "品种 [%s %s] 归一化完成: %d 条，%s ~ %s",
        spec.name, spec.source_symbol, len(frame),
        frame["trade_date"].iloc[0], frame["trade_date"].iloc[-1],
    )
    return frame
