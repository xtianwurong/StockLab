#!/usr/bin/env python3
"""
==============================================================================
StockLab - 数据源统一数据契约 (stocklab.datasource.contract)
==============================================================================

【模块职责】
  定义所有数据源必须遵守的统一数据契约：列名常量与共享数据模型。
  独立成模块的原因：数据源实现（_sources/）与对外服务（quote_service.py）
  都依赖这些符号，若放在 quote_service.py 内会造成循环导入。

【数据契约】
  所有数据源在完成抓取后，必须通过标准化转换输出只包含以下列名的 DataFrame：
    - trade_date  : 交易日期（pd.Timestamp / datetime64 类型）
    - close_price : 月收盘价（float 类型）
    - pe_ttm      : 月度滚动市盈率（float 类型，缺失为 NaN）
==============================================================================
"""

__all__ = [
    "TRADE_DATE_COLUMN",
    "CLOSE_PRICE_COLUMN",
    "PE_TTM_COLUMN",
    "StockRealtimeQuote",
]

# ============================================================================
# 统一数据契约常量 (Unified Data Contract Constants)
# ============================================================================
# 【为何使用常量而非魔法字符串？】
#   1. 编译期与运行时安全：若开发者拼错常量名（如 TRADE_DATE_COLUM），Python 会立即抛出
#      NameError，而不是在复杂的数据管道运行中途报隐蔽的 KeyError。
#   2. 单点维护与重构支持：未来若需变更字段名称，只需修改此处常量定义，业务调用层无需改动。
#   3. IDE 友好：现代 IDE 能够提供精准的代码自动补全与交叉跳转支持。
TRADE_DATE_COLUMN = "trade_date"
CLOSE_PRICE_COLUMN = "close_price"
PE_TTM_COLUMN = "pe_ttm"


# ============================================================================
# 实时行情数据对象 (Real-time Quote Model)
# ============================================================================
class StockRealtimeQuote:
    """
    股票实时行情快照数据类

    【C++ 概念映射：简单实体类 / 结构体 (Plain Old Data Class)】
      字段全带默认初值，无隐藏的元类或装饰器黑魔法，属性直观、易于调试打印与序列化。
    """

    def __init__(self, stock_code=""):
        self.stock_code = stock_code              # 股票代码，如 "000001.SZ"
        self.stock_name = ""                      # 公司简称，如 "平安银行"
        self.source_name = ""                     # 数据源标识，如 "tencent(腾讯财经)"
        self.current_price = 0.0                  # 最新价格 (元)
        self.yesterday_close = 0.0                # 昨日收盘价 (元)
        self.today_open = 0.0                     # 今日开盘价 (元)
        self.highest_price = 0.0                  # 今日最高价 (元)
        self.lowest_price = 0.0                   # 今日最低价 (元)
        self.change_amount = 0.0                  # 涨跌额 (元)
        self.change_percent = 0.0                 # 涨跌幅 (%)
        self.volume_shares = 0                    # 成交量 (股，与日线历史成交量口径一致)
        self.amount_yuan = 0.0                    # 成交额 (元)
        self.turnover_rate = 0.0                  # 换手率 (%)
        self.pe_ttm = None                        # 动态市盈率 PE-TTM (可为 None)
        self.pb_ratio = None                      # 市净率 PB (可为 None)
        self.total_market_value = 0.0             # 总市值 (元)
        self.circulating_market_value = 0.0       # 流通市值 (元)
        self.quote_time = ""                      # 行情时间字符串，如 "2026-09-30 16:15:00"

    def to_dict(self):
        """转为字典格式，便于外部模块做 JSON 序列化或数据加工"""
        return {
            "stock_code": self.stock_code,
            "stock_name": self.stock_name,
            "source_name": self.source_name,
            "current_price": self.current_price,
            "yesterday_close": self.yesterday_close,
            "today_open": self.today_open,
            "highest_price": self.highest_price,
            "lowest_price": self.lowest_price,
            "change_amount": self.change_amount,
            "change_percent": self.change_percent,
            "volume_shares": self.volume_shares,
            "amount_yuan": self.amount_yuan,
            "turnover_rate": self.turnover_rate,
            "pe_ttm": self.pe_ttm,
            "pb_ratio": self.pb_ratio,
            "total_market_value": self.total_market_value,
            "circulating_market_value": self.circulating_market_value,
            "quote_time": self.quote_time,
        }

    def __repr__(self):
        """控制台及日志友好的字符串表述"""
        pe_str = f"{self.pe_ttm:.2f}" if self.pe_ttm is not None else "None"
        return (
            f"StockRealtimeQuote({self.stock_code} {self.stock_name} "
            f"现价:{self.current_price:.2f} 涨跌:{self.change_percent:+.2f}% "
            f"PE-TTM:{pe_str} 时间:{self.quote_time})"
        )
