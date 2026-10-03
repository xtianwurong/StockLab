#!/usr/bin/env python3
"""
==============================================================================
StockLab - 数据契约校验 (stocklab.domain.contract)
==============================================================================

【模块职责】
   「列名 + 列序」的唯一权威校验入口，供数据源层与持久化层共用：
     - require_columns：源 DataFrame 必须具备的列，缺失即抛 DataContractError
       （数据源改版/列改名的第一道防线，防止缺列被静默写成 NULL）
     - align_columns：把 DataFrame 重排为契约列序，Repository 写入前统一调用
       （缺列拒绝写入、契约外列丢弃告警）

【设计原则】
   - 本模块只依赖 pandas 与标准库，是 datasource 与 persistence 共同依赖的叶子模块，
     两层不得互相 import，但都可以 import domain；
   - 契约常量（见 domain.security / domain.market_data / domain.valuation /
     domain.fundamental）与建表 DDL 同名同序，改任一侧必须同步改另一侧。
"""

import logging

_logger = logging.getLogger(__name__)

__all__ = [
    "DataContractError",
    "check_columns",
    "require_columns",
    "align_columns",
]


class DataContractError(Exception):
    """数据契约违约（源列缺失 / 目标列缺失），调用方应拒绝写入并报警"""


def check_columns(frame, required_columns, owner):
    """
    查出 frame 中缺失的契约列

    Args:
        frame (pd.DataFrame): 待检查的数据表，None 视为全部缺失
        required_columns (tuple/list): 契约要求的列名集合
        owner (str): 契约归属名（表名或源接口名），用于日志定位

    Returns:
        list: 缺失列名列表，无缺失时为空列表
    """
    if frame is None:
        return list(required_columns)
    present = set(frame.columns)
    return [column for column in required_columns if column not in present]


def require_columns(frame, required_columns, owner):
    """
    断言 frame 具备全部契约列，缺失即抛出 DataContractError

    Args:
        frame (pd.DataFrame): 待检查的数据表
        required_columns (tuple/list): 契约要求的列名集合
        owner (str): 契约归属名，用于异常信息定位

    Returns:
        None

    Raises:
        DataContractError: 存在缺失列时抛出，异常信息含缺失列名（截断至 8 个）
    """
    missing = check_columns(frame, required_columns, owner)
    if missing:
        raise DataContractError(
            "%s 数据契约违约，缺失列: %s"
            % (owner, ", ".join(missing[:8]))
        )


def align_columns(frame, contract_columns, owner):
    """
    把 frame 对齐到契约列序：缺列拒绝、契约外列丢弃告警、其余按契约顺序重排

    【为何必须重排】
       Repository 的 INSERT 显式列出全部列名，若 DataFrame 列序与契约不一致，
       重排可保证 SELECT 出来的值与目标列一一对应，写入不再依赖 DataFrame 列序。

    Args:
        frame (pd.DataFrame): 待对齐的数据表
        contract_columns (tuple/list): 契约列序（与建表 DDL 同序）
        owner (str): 契约归属名，用于日志定位

    Returns:
        pd.DataFrame: 仅含契约列、且列序与契约一致的副本

    Raises:
        DataContractError: 缺少契约列时抛出
    """
    require_columns(frame, contract_columns, owner)
    extra = [column for column in frame.columns if column not in contract_columns]
    if extra:
        _logger.warning(
            "%s 含契约外列，已丢弃: %s", owner, ", ".join(extra[:8])
        )
    return frame.loc[:, list(contract_columns)]
