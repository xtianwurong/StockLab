#!/usr/bin/env python3
"""
==============================================================================
StockLab - 大宗商品取数门面 (stocklab.facade.commodity_data)
==============================================================================

【模块职责】
   CommodityDataFacade：大宗商品页的**唯一取数入口**。**纯本地读** ——
   语料由 app/scripts/sync_commodities.py 落库，门面不发任何远端请求。

   - catalog()  十个品种的当前价、涨跌、分位、数据新鲜度（列表页）
   - history()  单个品种的完整价格序列 + 分位统计（趋势图）

【为什么分位在这里算，而七档评级不在】
   分位 = 当前价格在过去 N 年价格序列中的经验分布位置，是对**历史序列**的
   统计，属于 stocklab 的分析能力；七档「极度低估 ~ 极度高估」是 Web 展示层
   的口径（store.percentile_level + 前端 LEVEL7），两侧由测试双向校验。
   门面只把 percentile（0~100）交出去，映射到文案的一律留在 app.web。
   这条边界让同一个分位能被页面、报告、CLI 复用，而文案只有一处。

【为什么复用 ValuationPercentileAnalyzer 而不是自己写一行】
   CDF 口径只有一处实现是本项目的硬约定：`(严格小于当前值的样本数 / 有效样本数)`。
   自己写 count(x < v)/len(x) 看起来一模一样，但「自身是否计入分母」
   「非正数是否剔除」这两处细节会先漂移半年才发现。所以给分析器加了
   indicators 参数来接收价格列，而不是在调用方再算一遍。

【新鲜度为什么要门面来判】
   拿 2022 年的价格算「当前在近 5 年的分位」，输出的是一个看起来完全正常、
   实际基于四年前数据的评级 —— 这是本页最危险的失败模式：数字不会报错，
   只会误导。所以超过 STALE_DAYS 未更新的品种**不给分位**（percentile=None），
   页面必须显示「数据陈旧」而不是显示一个评级。
"""

import logging
from datetime import date, datetime, timedelta

import pandas as pd

from stocklab.analytics import ValuationPercentileAnalyzer
from stocklab.datasource.commodity import by_symbol, commodities
from stocklab.persistence.repository.commodity import CommodityPriceRepository

_logger = logging.getLogger(__name__)

__all__ = [
    "CommodityDataFacade",
    "DEFAULT_WINDOW_YEARS",
    "STALE_DAYS",
    "MIN_SAMPLES",
    "CHANGE_WINDOWS",
]

# 分位默认窗口（年）。与指数页口径一致，便于横向比较
DEFAULT_WINDOW_YEARS = 5

# 超过这么多自然日没有新数据即判为陈旧，不给分位
# （15 日 > 春节 10 天休市，不会把正常长假误判成停更）
STALE_DAYS = 15

# 少于这么多条有效样本不给分位 —— 60 个交易日约一个季度，
# 再短的话「过去 N 年的分位」这句话本身就不成立
MIN_SAMPLES = 60

# 涨跌幅回看窗口：(展示键, 自然日)
CHANGE_WINDOWS = (("1m", 30), ("3m", 90), ("12m", 365))


class CommodityDataFacade:
    """大宗商品门面（纯本地读，无远端回退）"""

    def __init__(self, database=None):
        """
        Args:
            database (Database, None): 数据库实例；None 时由 Repository 自建
                                       （仅限单次性脚本，Web 侧必须传
                                        store.facade_database()）
        """
        self._repo = CommodityPriceRepository(database)
        self._analyzer = ValuationPercentileAnalyzer()

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def catalog(self, window_years=DEFAULT_WINDOW_YEARS):
        """
        全部品种的当前价、涨跌与分位

        Args:
            window_years (int): 分位回看窗口（年）

        Returns:
            dict: {as_of, window_years, stale_days, items: [...]}
        """
        specs = commodities()
        frame = self._load(specs, window_years)
        today = date.today()

        items = []
        for spec in specs:
            items.append(self._item(spec, frame, window_years, today))

        as_of = None
        dates = [item["trade_date"] for item in items if item["trade_date"]]
        if dates:
            as_of = max(dates)

        return {
            "as_of": as_of,
            "window_years": window_years,
            "stale_days": STALE_DAYS,
            "items": items,
        }

    def history(self, symbol, window_years=DEFAULT_WINDOW_YEARS):
        """
        单个品种的价格序列与分位统计

        Args:
            symbol (str): 品种代号
            window_years (int): 分位回看窗口（年）

        Returns:
            dict: 品种元信息 + series + 分位统计；symbol 未登记返回 None

        Raises:
            ValueError: symbol 未登记（接口层据此 404/400）
        """
        spec = by_symbol(symbol)
        if spec is None:
            raise ValueError("未登记的大宗商品: %s" % symbol)

        frame = self._load([spec], window_years)
        today = date.today()
        item = self._item(spec, frame, window_years, today)
        item["series"] = self._series(frame, spec.symbol)
        return item

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _since(self, window_years):
        """窗口下界：按自然日回看，宁可多取一点也不要把边界样本切掉"""
        days = int(round(window_years * 365.25))
        return date.today() - timedelta(days=days)

    def _load(self, specs, window_years):
        return self._repo.load_series(
            symbols=[spec.symbol for spec in specs],
            since=self._since(window_years),
        )

    def _empty_item(self, spec):
        """库里没有该品种时的占位：所有统计一律为 None，不编默认值"""
        item = self._meta(spec)
        item.update({
            "trade_date": None,
            "close": None,
            "lag_days": None,
            "stale": True,
            "percentile": None,
            "sample_count": 0,
            "interval_text": "",
            "median_value": None,
            "min_value": None,
            "max_value": None,
            "changes": {key: None for key, _ in CHANGE_WINDOWS},
        })
        return item

    def _meta(self, spec):
        return {
            "symbol": spec.symbol,
            "name": spec.name,
            "category": spec.category,
            "unit": spec.unit,
            "unit_note": spec.unit_note,
            "exchange": spec.exchange,
            "source_symbol": spec.source_symbol,
            "description": spec.description,
        }

    def _item(self, spec, frame, window_years, today):
        """单个品种的全部统计（不含序列）"""
        series = frame.loc[frame["symbol"] == spec.symbol].copy()
        if series.empty:
            return self._empty_item(spec)

        series = series.sort_values("trade_date").reset_index(drop=True)
        latest_date = _as_date(series["trade_date"].iloc[-1])
        latest_close = float(series["close"].iloc[-1])
        lag_days = (today - latest_date).days
        stale = lag_days > STALE_DAYS

        result = self._percentile(series)

        item = self._meta(spec)
        item.update({
            "trade_date": str(latest_date),
            "close": latest_close,
            "lag_days": lag_days,
            "stale": stale,
            # 陈旧一律不给分位 —— 见模块说明，这是本页最容易误导人的地方
            "percentile": None if stale else (
                result.percentile if result else None
            ),
            "sample_count": result.sample_count if result else 0,
            "interval_text": result.interval_text if result else "",
            "median_value": result.median_value if result else None,
            "min_value": result.min_value if result else None,
            "max_value": result.max_value if result else None,
            "changes": self._changes(series, latest_date),
        })
        return item

    def _percentile(self, series):
        """
        价格序列的分位（窗口内）

        Returns:
            ValuationPercentileResult | None: 样本不足或无有效值时 None
        """
        sample = series.loc[:, ["trade_date", "close"]]
        if len(sample) < MIN_SAMPLES:
            _logger.info(
                "品种 [%s] 窗口内仅 %d 条样本，不足 %d，不给分位",
                series["symbol"].iloc[0], len(sample), MIN_SAMPLES,
            )
            return None
        results = self._analyzer.analyze(sample, indicators=["close"])
        if not results:
            return None
        result = results[0]
        return result if result.is_available() else None

    def _changes(self, series, latest_date):
        """
        相对窗口起点的涨跌幅（%）

        【锚点用序列末日而不是「今天」】
          锚点取 today 时，同一个数据集在不同日期跑会得到不同结果，
          排序与告警会随时间漂移。用序列末日锚定，结果只由数据本身决定，
          重跑一次必然得到同一个数 —— 可复算比「看起来更接近现在」重要。
        """
        out = {}
        for key, days in CHANGE_WINDOWS:
            anchor = latest_date - timedelta(days=days)
            before = series.loc[series["trade_date"].map(_as_date) <= anchor]
            if before.empty:
                out[key] = None
                continue
            base = float(before["close"].iloc[-1])
            out[key] = None if base <= 0 else round(
                (float(series["close"].iloc[-1]) / base - 1.0) * 100.0, 2
            )
        return out

    def _series(self, frame, symbol):
        """序列化成前端直接可用的 [{date, close}]"""
        rows = frame.loc[frame["symbol"] == symbol]
        if rows.empty:
            return []
        rows = rows.sort_values("trade_date")
        return [
            {"date": str(_as_date(row.trade_date)), "close": float(row.close)}
            for row in rows.itertuples(index=False)
        ]


def _as_date(value):
    """
    统一成 datetime.date

    DuckDB 的 DATE 列经 fetchdf 出来可能是 date、pandas.Timestamp 或
    numpy.datetime64（随驱动版本变化）。**不能只判 hasattr(date)**：
    datetime.datetime 是 date 的子类，先判 date 会把 datetime 放过去，
    随后 `date.today() - datetime(...)` 直接 TypeError。
    统一交给 pd.Timestamp 归一，三种类型都走同一条路。
    """
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return pd.Timestamp(value).date()
