#!/usr/bin/env python3
"""
==============================================================================
StockLab - 通达信数据源通道 (stocklab.datasource._sources.tdx_source)
==============================================================================

【模块职责】
  通达信公开行情服务器直连（TCP 7709），作为行情链路第 5 级（最后）备用源：
    - 月线收盘价（K 线命令 category=6）
    - 实时行情快照（盘口报价命令）

【依赖与选型（2026-10-03 Step 0 探针实测）】
  依赖 tdxdata（通达信 2026-09 新版行情协议实现，Python >= 3.11）。
  老 pytdx / mootdx 协议自 2026-09-10 起被服务端拒绝；pytdx3 未发布到 PyPI，
  无法安装。探针结论：tdxdata 握手 + 日线 + 月线 + 盘口全部通过，
  600519 现价与新浪完全一致（1258.62）。

【已知限制（探针实测）】
  1. tdxdata 包内 docstring 标注「7=月线」与实际不符：**category 6 才是月线**，
     7/8 返回 1 分钟线，9/4 为日线（vol 单位不同），0/1/2/3 为 5/15/30/60 分钟；
  2. 市场编号：0 = 深市、1 = 沪市、2 = 北交所（830xxx/920xxx 实测走 market 2），
     tdxdata 自带 market_of 不识别北交所，故本模块自行映射；
  3. 本通道**无复权因子能力**（tdxdata 未提供除权除息命令）：
     adjust_type 非空时返回空表，避免用不复权价格冒充前复权污染下游；
  4. 本通道**无证券简称能力**（协议未返回名称），fetch_stock_name 返回空串，
     由上层名称链路（Tencent -> AkShare -> BaoStock）承担；
  5. 服务端单请求约 1~3 秒固定延迟，且单出口高并发会触发限流：
     本通道只做「最后兜底」，串行单连接、用完即断。
==============================================================================
"""

import logging

import pandas as pd

from stocklab.common.type_conversion import safe_float
from stocklab.datasource._sources.base import StockDataSource
from stocklab.datasource.data_contract import (
    CLOSE_PRICE_COLUMN,
    TRADE_DATE_COLUMN,
    StockRealtimeQuote,
)

# tdxdata 为可选运行时依赖：缺失时本模块仍可导入，仅该通道不可用
try:
    from tdxdata import TdxClient
except ImportError:  # pragma: no cover - 依赖缺失时的降级路径
    TdxClient = None

_logger = logging.getLogger(__name__)

__all__ = [
    "TdxDataSource",
]

# tdxdata 实测的 K 线周期编号（包内注释有误，以下为 2026-10-03 实测值）
_CATEGORY_DAY = 9
_CATEGORY_MONTH = 6

# 单次请求的月线根数（覆盖约 33 年历史）
_MONTH_BAR_COUNT = 400


def _market_of(stock_code):
    """
    标准代码 -> 通达信市场编号（0 深市 / 1 沪市 / 2 北交所）

    Args:
        stock_code (str): "600519.SH" / "000001.SZ" / "920819.BJ"

    Returns:
        int: 市场编号；无法识别返回 None
    """
    raw = str(stock_code).strip()
    if "." in raw:
        code, suffix = raw.split(".")[0], raw.split(".")[1].upper()
        if suffix in ("SH", "SZ", "BJ"):
            return {"SH": 1, "SZ": 0, "BJ": 2}[suffix]

    code = raw
    if code.startswith(("6", "5", "900")):
        return 1
    if code.startswith(("92", "4", "8")):
        return 2
    if code[:1] in ("0", "1", "2", "3"):
        return 0
    return None


def _pure_code(stock_code):
    """取纯数字 6 位代码（TDX 命令只接受数字代码）"""
    raw = str(stock_code).strip()
    if "." in raw:
        raw = raw.split(".")[0]
    if raw[:2].lower() in ("sh", "sz", "bj"):
        raw = raw[2:]
    return raw


class TdxDataSource(StockDataSource):
    """
    数据源实现：通达信公开行情服务器（第 5 级 / 最后备用源）

    【连接策略】每次调用短连接（connect -> 取数 -> close）：
      TDX 服务端延迟高且长连接易被回收，兜底场景调用频率极低，
      短连接比维护常驻会话更简单可靠，也避免 socket 泄漏。
    """

    SOURCE_NAME = "tdx(通达信)"

    def __init__(self, client_factory=None):
        """
        Args:
            client_factory: 可注入的客户端工厂（测试用桩替换真实 TDX 连接），
                            需返回带 connect()/bars()/quotes()/close() 的对象
        """
        self._client_factory = client_factory

    def _create_client(self):
        """创建并连接一个 TDX 客户端；失败返回 None"""
        if self._client_factory is None:
            if TdxClient is None:
                _logger.warning("%s 未安装 tdxdata 依赖，通道不可用", self.SOURCE_NAME)
                return None
            client = TdxClient()
        else:
            client = self._client_factory()

        try:
            client.connect()
            return client
        except Exception as error:
            _logger.warning("%s 连接失败: %s", self.SOURCE_NAME, error)
            try:
                client.close()
            except Exception:
                pass  # 清理连接失败不影响主流程，主错误已在外层记录
            return None

    def _request(self, method_name, *args, **kwargs):
        """
        短连接执行一次 TDX 请求；统一异常兜底

        Returns:
            object: 成功返回结果，失败返回 None
        """
        client = self._create_client()
        if client is None:
            return None
        try:
            handler = getattr(client, method_name)
            return handler(*args, **kwargs)
        except Exception as error:
            _logger.warning("%s %s 请求失败: %s", self.SOURCE_NAME, method_name, error)
            return None
        finally:
            try:
                client.close()
            except Exception:
                pass  # 清理连接失败不影响主流程，主错误已在外层记录

    # ------------------------------------------------------------------
    # StockDataSource 接口
    # ------------------------------------------------------------------
    def fetch_monthly_close_prices(
        self, stock_code, adjust_type, retry_count=1, retry_interval_seconds=0
    ):
        """
        获取月线收盘价（链路第 5 级 / 最后兜底）

        Returns:
            pd.DataFrame: trade_date + close_price；非空复权类型返回空表（无复权因子能力）
        """
        if adjust_type:
            _logger.info(
                "%s 无复权因子能力，跳过 adjust_type=%s 的月线请求（%s）",
                self.SOURCE_NAME, adjust_type, stock_code,
            )
            return pd.DataFrame()

        market = _market_of(stock_code)
        code = _pure_code(stock_code)
        if market is None or len(code) != 6 or not code.isdigit():
            _logger.warning("%s 无法识别股票代码: %s", self.SOURCE_NAME, stock_code)
            return pd.DataFrame()

        bars = self._request("bars", _CATEGORY_MONTH, code, 0, _MONTH_BAR_COUNT, market)
        if not bars:
            return pd.DataFrame()

        rows = []
        for bar in bars:
            day = str(bar.get("datetime", "")).strip()[:10]
            close = safe_float(bar.get("close"))
            if day and close is not None:
                rows.append((day, close))
        if not rows:
            return pd.DataFrame()

        frame = pd.DataFrame(rows, columns=[TRADE_DATE_COLUMN, CLOSE_PRICE_COLUMN])
        frame[TRADE_DATE_COLUMN] = pd.to_datetime(frame[TRADE_DATE_COLUMN])
        frame = frame.sort_values(TRADE_DATE_COLUMN).reset_index(drop=True)
        return frame

    def fetch_stock_name(self, stock_code):
        """
        获取股票简称

        通达信行情命令不返回证券名称，本通道诚实地返回空串，
        由上层名称链路（Tencent -> AkShare -> BaoStock）兜底。
        """
        return ""

    def fetch_realtime_quote(self, stock_code):
        """
        获取实时行情快照（链路第 3 级：Tencent -> Sina -> TDX）

        Returns:
            StockRealtimeQuote | None: 失败返回 None
        """
        market = _market_of(stock_code)
        code = _pure_code(stock_code)
        if market is None or len(code) != 6 or not code.isdigit():
            _logger.warning("%s 无法识别股票代码: %s", self.SOURCE_NAME, stock_code)
            return None

        payload = self._request("quotes", [(market, code)])
        if not payload:
            return None

        row = payload[0]
        # 服务端对无效/退市代码会返回「重定向到其他代码」的垃圾数据，必须校验
        if str(row.get("code", "")).strip() != code:
            _logger.warning(
                "%s %s 返回代码不匹配（%s），视为无数据",
                self.SOURCE_NAME, stock_code, row.get("code"),
            )
            return None

        price = safe_float(row.get("price"), 0.0)
        prev_close = safe_float(row.get("last_close"), 0.0)
        if price <= 0 or prev_close <= 0:
            _logger.warning("%s %s 价格字段非法: price=%s prev=%s",
                            self.SOURCE_NAME, stock_code, price, prev_close)
            return None

        quote = StockRealtimeQuote(stock_code)
        quote.source_name = self.SOURCE_NAME
        quote.current_price = price
        quote.yesterday_close = prev_close
        quote.today_open = safe_float(row.get("open"), 0.0)
        quote.highest_price = safe_float(row.get("high"), 0.0)
        quote.lowest_price = safe_float(row.get("low"), 0.0)
        quote.change_amount = round(price - prev_close, 4)
        quote.change_percent = round((price - prev_close) / prev_close * 100.0, 4)
        # vol 单位为手（实测 600519 vol=38330 手 = 3833098 股），换算为股
        quote.volume_shares = int(safe_float(row.get("vol"), 0.0) * 100)
        quote.amount_yuan = safe_float(row.get("amount"), 0.0)
        return quote
