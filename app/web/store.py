#!/usr/bin/env python3
"""
==============================================================================
StockLab - Web 层数据访问模块 (app.web.store)
==============================================================================

【模块职责】
  给 Web 接口提供两样东西：
    1) 进程级单例门面 —— 所有「取数 + 回写」共用一个连接；
    2) 全市场 / 指数的聚合查询 —— 走本模块自己的单例连接，不占门面锁。

【为何要进程级单例门面（修复 Bug A3）】
  MarketDataFacade 构造时会 initialize_database() → duckdb.connect()，
  以写模式打开数据库。原实现每个 HTTP 请求都新建一个门面，导致：
    1) 每请求重复建连（实测约 11ms/次）；
    2) 每请求重复打印「数据库初始化完成」，日志噪音淹没真实日志；
    3) 频繁开关连接加剧写锁争用。
  改为单例后只在首次建连一次；连接失效时自动重建（重建只发生一次，
  不会退回「每请求建连」的老路）。

【为何聚合查询也是写连接而不是只读连接】
  DuckDB 不允许同一进程内同时存在配置不同的连接 —— 门面是写连接时，
  再开 read_only=True 会直接报
      "Can't open a connection to same database file with a different
       configuration than existing connections"
  （本模块初版即踩此坑，导致门面一打开，Dashboard/指数接口全部返回空）。
  因此这里同样用默认写连接，并用单例 + 锁把它约束在「一条连接、串行使用」。

【并发约定】
  DuckDB 连接非线程安全（见 AGENT.md），因此：
    - _FACADE_LOCK  包住门面的「创建 + 取数 + 可能的回写」，全程串行；
    - _STORE_LOCK   包住本模块连接的全部查询，全程串行；
    - 两把锁互不嵌套，可与 analyzer 纯内存统计并行。
  这两把锁只在**进程内**生效，与外部进程无关 —— DuckDB 同文件跨进程
  单写者是物理限制，服务运行期间外部写入会被阻塞（见 AGENT.md 注意事项）。
==============================================================================
"""

import logging
import threading

import duckdb
import pandas as pd

from stocklab.facade import MarketDataFacade

_logger = logging.getLogger("StockLab.Web.Store")

__all__ = [
    "reset_facade",
    "call_facade",
    "current_priority",
    "current_db_path",
    "load_securities",
    "load_market_percentile",
    "load_index_list",
    "load_index_percentile",
    "load_industry_valuation",
    "industry_stat_dates",
    "market_data_as_of",
    "invalidate_cache",
    "percentile_level",
]


# 保护门面实例（创建 / 重建）的锁；与查询锁分开，避免重入死锁
_FACADE_CREATE_LOCK = threading.Lock()

# 保护门面查询全程（取数 + 回写）的串行锁
_FACADE_LOCK = threading.Lock()

# 保护本模块聚合连接的串行锁
_STORE_LOCK = threading.Lock()

# 进程级单例门面；None 表示尚未创建
_facade = None

# 进程级单例聚合连接；None 表示尚未创建
_store_conn = None

# 聚合结果缓存：key -> 结果；缓存项过多时整体清空，防止内存无界增长
_CACHE = {}
_CACHE_LOCK = threading.Lock()

# 证券表缓存（进程内只读；securities 表变化频率极低）
_SECURITIES_CACHE = None
_SECURITIES_CACHE_LOCK = threading.Lock()

# 有资格进缓存的指标列
_INDICATORS = ("pe_ttm", "pe_static", "pb", "ps", "pcf")


# ---------------------------------------------------------------------------
# 单例门面（取数 + 回写）
# ---------------------------------------------------------------------------

def _make_facade():
    """
    创建一个新的取数门面（按启动参数或 config.ini 配置）

    Returns:
        MarketDataFacade: 新建的门面实例
    """
    from flask import current_app

    return MarketDataFacade(
        priority=current_app.config.get("STOCKLAB_PRIORITY"),
        db_path=current_app.config.get("STOCKLAB_DB_PATH"),
    )


def reset_facade():
    """
    关闭并丢弃当前单例门面（下次取数时重建）

    Returns:
        None: 无返回值
    """
    global _facade
    with _FACADE_CREATE_LOCK:
        if _facade is not None:
            try:
                _facade.close()
            except Exception as exc:
                _logger.warning("关闭旧门面失败: %s", str(exc)[:120])
            _facade = None


def _get_facade():
    """
    取得进程级单例门面；不存在则创建

    Returns:
        MarketDataFacade: 可用的门面实例
    """
    global _facade
    if _facade is not None:
        return _facade
    with _FACADE_CREATE_LOCK:
        # 双重检查：等锁期间可能已被别的线程创建好
        if _facade is None:
            _facade = _make_facade()
            _logger.info("进程级门面已创建（仅首次建连，之后所有请求复用）")
    return _facade


def call_facade(method_name, *args, **kwargs):
    """
    在串行锁内用单例门面调一个取数方法

    【为何传方法名而不是传方法对象】
      调用方写 call_facade("fetch_valuation_history", code, period) 即可，
      不必持有门面引用 —— 拿到引用就可能在锁外调用，绕过串行约束。

    Args:
        method_name (str): 门面方法名
        *args: 透传的位置参数
        **kwargs: 透传的关键字参数

    Returns:
        pd.DataFrame: 取数结果
    """
    global _facade
    with _FACADE_LOCK:
        facade = _get_facade()
        try:
            return getattr(facade, method_name)(*args, **kwargs)
        except Exception as exc:
            # 连接可能已失效（数据库文件被替换 / 连接中断）：重建后重试一次
            _logger.warning(
                "门面取数失败 [%s]，重建后重试: %s", method_name, str(exc)[:200]
            )
            reset_facade()
            facade = _get_facade()
            return getattr(facade, method_name)(*args, **kwargs)


def current_priority():
    """
    取得当前生效的取数优先级（接口回显给前端用）

    Returns:
        str: local_first 或 remote_first
    """
    with _FACADE_LOCK:
        return _get_facade().priority


def current_db_path():
    """
    取得当前数据库文件路径（健康检查回显用）

    Returns:
        str: 数据库路径
    """
    from flask import current_app

    return current_app.config.get("STOCKLAB_DB_PATH") or "data/stocklab.duckdb"


# ---------------------------------------------------------------------------
# 聚合查询（进程级单例连接 + 串行锁）
# ---------------------------------------------------------------------------

def _get_store_conn():
    """
    取得进程级单例聚合连接；不存在则创建

    Returns:
        duckdb.DuckDBPyConnection: 可用连接；创建失败返回 None
    """
    global _store_conn
    if _store_conn is not None:
        return _store_conn

    db_path = current_db_path()
    try:
        # 注意：这里必须用默认写连接，不能用 read_only=True，
        # 否则与门面的写连接在同进程内配置冲突（见模块 docstring）。
        _store_conn = duckdb.connect(db_path)
        _logger.info("进程级聚合连接已创建（仅首次建连）")
    except Exception as exc:
        _logger.error("创建聚合连接失败: %s", str(exc)[:200])
        _store_conn = None
    return _store_conn


def _reset_store_conn():
    """
    关闭并丢弃聚合连接（查询报错时触发，下次查询重建）

    Returns:
        None: 无返回值
    """
    global _store_conn
    if _store_conn is not None:
        try:
            _store_conn.close()
        except Exception as exc:
            _logger.warning("关闭聚合连接失败: %s", str(exc)[:120])
    _store_conn = None


def _query(sql, params=None):
    """
    在串行锁内执行一条 SQL，返回 DataFrame

    【失败处理】
      报错先重建连接重试一次（应对数据库文件被替换）；仍失败则返回 None，
      由调用方给出「数据不可用」的友好应答，而不是抛 500。

    Args:
        sql (str): SQL 语句，占位符用 ?
        params (list|None): 绑定参数

    Returns:
        pd.DataFrame | None: 查询结果；失败返回 None
    """
    global _store_conn
    with _STORE_LOCK:
        conn = _get_store_conn()
        if conn is None:
            return None
        try:
            if params:
                return conn.execute(sql, params).df()
            return conn.execute(sql).df()
        except Exception as exc:
            _logger.warning("聚合查询失败，重建连接后重试: %s", str(exc)[:300])
            _reset_store_conn()
            conn = _get_store_conn()
            if conn is None:
                return None
            try:
                if params:
                    return conn.execute(sql, params).df()
                return conn.execute(sql).df()
            except Exception as retry_exc:
                _logger.error("聚合查询重试仍失败: %s", str(retry_exc)[:300])
                _reset_store_conn()
                return None


def _cache_get(key):
    """
    读聚合缓存

    Args:
        key (str): 缓存键

    Returns:
        object: 缓存值；未命中返回 None
    """
    with _CACHE_LOCK:
        return _CACHE.get(key)


def _cache_put(key, value):
    """
    写聚合缓存

    Args:
        key (str): 缓存键
        value (object): 缓存值

    Returns:
        None: 无返回值
    """
    with _CACHE_LOCK:
        if len(_CACHE) > 256:
            _CACHE.clear()
        _CACHE[key] = value


def invalidate_cache():
    """
    清空全部缓存（数据同步完成后由调用方触发）

    Returns:
        None: 无返回值
    """
    global _SECURITIES_CACHE
    with _CACHE_LOCK:
        _CACHE.clear()
    with _SECURITIES_CACHE_LOCK:
        _SECURITIES_CACHE = None
    _logger.info("Web 层缓存已清空")


def load_securities():
    """
    读取全市场证券基础信息（进程内缓存，只查询一次）

    【为何缓存】
      证券联想接口每次按键都要读 5572 行；缓存后改用内存过滤，
      既不占门面锁，也消除每按键一次的建连开销。

    【列的取舍】
      上游数据源只填充 ts_code / symbol / name / exchange / market /
      list_status；industry、area、list_date、is_hs 全表为空，
      因此这里不查这些列，页面也不展示（避免出现恒为空的字段）。

    Returns:
        pd.DataFrame: 证券表；读取失败返回空 DataFrame
    """
    global _SECURITIES_CACHE
    with _SECURITIES_CACHE_LOCK:
        if _SECURITIES_CACHE is not None:
            return _SECURITIES_CACHE

    frame = _query(
        "SELECT ts_code, symbol, name, exchange, market, list_status "
        "FROM reference.securities"
    )
    if frame is None:
        frame = pd.DataFrame()

    with _SECURITIES_CACHE_LOCK:
        _SECURITIES_CACHE = frame
    return frame


def load_market_percentile(indicator):
    """
    计算全市场每只股票在指定指标上的历史分位（首页 Dashboard 数据源）

    【口径】与 stocklab.analytics.ValuationPercentileAnalyzer 完全一致：
        - 有效样本 = 该指标 > 0 的历史样本（亏损期排除出分母）；
        - 当前值   = 该股最新交易日的原始值（不加 >0 过滤，与 analyzer 相同）；
        - 分位     = 有效样本中严格小于当前值的条数 / 有效样本总数 × 100；
        - 当前值 <= 0 或无有效样本时分位为 null（前端显示「不可用」）。
      一致性由 app/scripts/verify_market_sql.py 抽样逐只比对 analyzer 保证
      （最近一次 300/300 一致）。

    【为何直接查库而不是逐只调 analyzer】
      全市场 5572 只 × 平均 794 行 = 442 万行；逐只要 5572 次独立计算与
      取数，一条窗口函数 SQL 一次算完（实测 0.13s），且不占门面锁。

    Args:
        indicator (str): 指标列名，取值 pe_ttm / pe_static / pb / ps / pcf

    Returns:
        list[dict]: 每项含 ts_code / name / market / current_value /
                    percentile / level / sample_count / median_value /
                    min_value / max_value，按分位升序（不可用者排最后）
    """
    if indicator not in _INDICATORS:
        return []

    cache_key = "market:" + indicator
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    frame = _query(_MARKET_SQL.format(col=indicator))
    if frame is None or frame.empty:
        _logger.error("全市场分位查询无结果 [%s]", indicator)
        return []

    results = []
    for row in frame.itertuples(index=False):
        percentile = _num(row.percentile)
        if percentile is not None:
            percentile = round(percentile, 2)
        results.append({
            "ts_code": row.ts_code,
            "name": _text(row.name),
            "market": _text(row.market),
            "current_value": _round4(_num(row.current_value)),
            "percentile": percentile,
            "level": percentile_level(percentile),
            "sample_count": int(row.sample_count),
            "median_value": _round4(_num(row.median_value)),
            "min_value": _round4(_num(row.min_value)),
            "max_value": _round4(_num(row.max_value)),
        })

    _cache_put(cache_key, results)
    return results


# 全市场分位查询：先取每只股票最新交易日的原始值作为「当前值」，
# 再只用 >0 的有效样本统计样本数 / 中位数 / 低于当前值的条数。
_MARKET_SQL = """
WITH latest AS (
    SELECT ts_code, {col} AS current_value
    FROM market.valuation_history
    WHERE {col} IS NOT NULL
    QUALIFY ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY trade_date DESC) = 1
),
valid AS (
    SELECT h.ts_code, h.{col} AS v, l.current_value
    FROM market.valuation_history h
    JOIN latest l USING (ts_code)
    WHERE h.{col} IS NOT NULL AND h.{col} > 0
)
SELECT v.ts_code,
       l.current_value,
       CASE WHEN COUNT(*) = 0 OR l.current_value <= 0 THEN NULL
            ELSE 100.0 * SUM(CASE WHEN v.v < l.current_value THEN 1 ELSE 0 END)
                 / COUNT(*) END AS percentile,
       COUNT(*) AS sample_count,
       MEDIAN(v.v) AS median_value,
       MIN(v.v) AS min_value,
       MAX(v.v) AS max_value,
       s.name, s.market
FROM valid v
JOIN latest l USING (ts_code)
LEFT JOIN reference.securities s ON s.ts_code = v.ts_code
GROUP BY v.ts_code, l.current_value, s.name, s.market
ORDER BY percentile ASC NULLS LAST, v.ts_code
"""


def load_index_list():
    """
    列出本地已有成分股的全部指数（指数估值页的数据源）

    Returns:
        list[dict]: 每项含 index_code / index_name / member_count
    """
    cached = _cache_get("index_list")
    if cached is not None:
        return cached

    frame = _query(
        "SELECT index_code, index_name, COUNT(*) AS member_count "
        "FROM reference.index_memberships "
        "GROUP BY index_code, index_name "
        "ORDER BY index_code"
    )
    if frame is None or frame.empty:
        return []

    results = []
    for row in frame.itertuples(index=False):
        results.append({
            "index_code": row.index_code,
            "index_name": row.index_name,
            "member_count": int(row.member_count),
        })
    _cache_put("index_list", results)
    return results


def load_index_percentile(index_code, indicator):
    """
    计算单个指数的估值历史序列与其历史分位

    【口径】
      指数估值 = 成分股该指标的**中位数**（等权口径，剔除 <= 0 的样本）；
      按交易日聚合成序列后，套用与个股完全相同的分位公式
      （由 ValuationPercentileAnalyzer 计算，保证档位与个股一致）。

    Args:
        index_code (str): 指数代码，如 000300
        indicator (str): 指标列名 pe_ttm / pe_static / pb / ps / pcf

    Returns:
        tuple: (历史序列 DataFrame[trade_date, indicator], 分位结果 list)；
               失败返回 (None, [])
    """
    if indicator not in _INDICATORS:
        return None, []

    cache_key = "index:%s:%s" % (index_code, indicator)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached["frame"], cached["results"]

    sql = (
        "SELECT h.trade_date, MEDIAN(h.{col}) AS value "
        "FROM market.valuation_history h "
        "JOIN reference.index_memberships m ON m.ts_code = h.ts_code "
        "WHERE m.index_code = ? AND h.{col} IS NOT NULL AND h.{col} > 0 "
        "GROUP BY h.trade_date "
        "ORDER BY h.trade_date"
    ).format(col=indicator)
    frame = _query(sql, [index_code])
    if frame is None or frame.empty:
        _logger.error("指数估值序列无结果 [%s, %s]", index_code, indicator)
        return None, []

    # 把聚合列改名成 analyzer 认识的指标列名
    frame = frame.rename(columns={"value": indicator})

    from stocklab.analytics import ValuationPercentileAnalyzer

    results = ValuationPercentileAnalyzer().analyze(frame)

    _cache_put(cache_key, {"frame": frame, "results": results})
    return frame, results


def industry_stat_dates():
    """
    列出本地已有的行业估值统计日期（最新在前），供行业页显示数据截止日期

    Returns:
        list[str]: YYYY-MM-DD 文本列表；无数据时为空列表
    """
    cached = _cache_get("industry_dates")
    if cached is not None:
        return cached

    frame = _query(
        "SELECT DISTINCT stat_date FROM market.industry_valuations "
        "ORDER BY stat_date DESC"
    )
    if frame is None or frame.empty:
        return []

    dates = []
    for value in frame["stat_date"].tolist():
        # DuckDB 可能返回 datetime 或 date，统一成 YYYY-MM-DD 文本
        if hasattr(value, "date") and not isinstance(value, str):
            value = value.date()
        dates.append(value.isoformat() if hasattr(value, "isoformat") else str(value))
    _cache_put("industry_dates", dates)
    return dates


def load_industry_valuation(industry_level, stat_date=None):
    """
    读取指定层级的行业估值横截面（行业估值页的数据源）

    【口径说明】
      每行是某行业在某统计日的横截面：pe_weighted 为总市值加权 PE，
      pe_median 为成分 PE 中位数，pe_arithmetic 为算术平均 PE。
      行业层由国证行业分类给出（1 = 一级 ~ 4 = 细分），不是由个股聚合而来。

    Args:
        industry_level (int): 行业层级，1~4（1 最粗、4 最细）
        stat_date (str | None): 统计日期 YYYY-MM-DD；None 取本地最新一期

    Returns:
        tuple: (list[dict], str) —— 每行含 industry_code / industry_name /
                company_count / priced_company_count / pe_weighted /
                pe_median / pe_arithmetic / total_market_value / net_profit，
                按 pe_median 升序（缺失排最后）；第二个元素为实际使用的统计日期
    """
    dates = industry_stat_dates()
    if not dates:
        return [], ""
    if stat_date is None:
        stat_date = dates[0]
    elif stat_date not in dates:
        return [], ""

    cache_key = "industry:%d:%s" % (industry_level, stat_date)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached, stat_date

    frame = _query(
        "SELECT industry_code, industry_name, company_count, priced_company_count, "
        "       pe_weighted, pe_median, pe_arithmetic, total_market_value, net_profit "
        "FROM market.industry_valuations "
        "WHERE stat_date = ? AND industry_level = ? "
        "ORDER BY pe_median ASC NULLS LAST, industry_code",
        [stat_date, industry_level],
    )
    if frame is None or frame.empty:
        return [], stat_date

    results = []
    for row in frame.itertuples(index=False):
        results.append({
            "industry_code": _text(row.industry_code),
            "industry_name": _text(row.industry_name),
            "company_count": int(row.company_count or 0),
            "priced_company_count": int(row.priced_company_count or 0),
            "pe_weighted": _round4(_num(row.pe_weighted)),
            "pe_median": _round4(_num(row.pe_median)),
            "pe_arithmetic": _round4(_num(row.pe_arithmetic)),
            "total_market_value": _round4(_num(row.total_market_value)),
            "net_profit": _round4(_num(row.net_profit)),
        })

    _cache_put(cache_key, results)
    return results, stat_date


def market_data_as_of():
    """
    本地估值序列的最新交易日（页面显示「数据截止」用）

    Returns:
        str: YYYY-MM-DD；无数据时为空串
    """
    cached = _cache_get("market_as_of")
    if cached is not None:
        return cached

    frame = _query("SELECT MAX(trade_date) AS d FROM market.valuation_history")
    value = ""
    if frame is not None and not frame.empty:
        raw = frame["d"].iloc[0]
        if raw is not None and raw == raw:  # None / NaT 排除
            # DuckDB 可能返回 datetime 或 date，统一成 YYYY-MM-DD 文本
            if hasattr(raw, "date") and not isinstance(raw, str):
                raw = raw.date()
            value = raw.isoformat() if hasattr(raw, "isoformat") else str(raw)
    _cache_put("market_as_of", value)
    return value


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _num(value):
    """
    把 pandas 数值转成 JSON 安全的数值（NaN -> None）

    Args:
        value: 原始数值

    Returns:
        float | None: JSON 可序列化数值
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN 自比较不等
        return None
    return number


def _text(value):
    """
    把可能为 NaN 的字符串转成空串

    Args:
        value: 原始字符串或 NaN

    Returns:
        str: 非空字符串
    """
    if value is None:
        return ""
    if isinstance(value, float) and value != value:  # NaN
        return ""
    return str(value)


def _round4(value):
    """
    数值轮转到 4 位小数（None 原样返回）

    【为何要轮转】
      SQL 返回的中位数可能是 37.88999999999999 这类全长浮点，
      全市场 5572 行直接下发会让 payload 白白变大，且展示时还得再格式化一次。

    Args:
        value (float | None): 原始数值

    Returns:
        float | None: 轮转后的数值
    """
    if value is None:
        return None
    return round(value, 4)


def percentile_level(percentile):
    """
    把分位数映射为七档评级

    【七档口径】参考同类开源项目 myquant 的公开分档，比三档信息量更足：
        [0,10)   极度低估      [40,60)  正常
        [10,20)  低估          [60,80)  正常偏高
        [20,40)  正常偏低      [80,90)  高估
                              [90,100] 极度高估
    注：stocklab.analytics 内部仍是三档（<=30 / 30~70 / >=70），
        三档用于档位结论，七档仅用于 Web 展示，两者不互相替代。
        前端 app/web/static/common.js 的 LEVEL7 必须与此保持一致。

    Args:
        percentile (float | None): 分位数（0~100）

    Returns:
        str: 七档评级文本；分位不可用时返回 "-"
    """
    if percentile is None:
        return "-"
    if percentile < 10:
        return "极度低估"
    if percentile < 20:
        return "低估"
    if percentile < 40:
        return "正常偏低"
    if percentile < 60:
        return "正常"
    if percentile < 80:
        return "正常偏高"
    if percentile < 90:
        return "高估"
    return "极度高估"
