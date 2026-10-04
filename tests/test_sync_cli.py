"""
数据同步 CLI 的阶段编排与失败语义 (tests/test_sync_cli.py)

覆盖 app/scripts/sync_market_data.py：9 个同步阶段 + 参数解析 + main() 的编排。

【为什么值得测】
  这个脚本的每个阶段都是「取数 → 落库 → 返回 bool」，真正的风险不在取数
  （那由 datasource 层各通道自己的测试负责），而在三件事：
    1. 失败语义 —— 什么情况该返回 False，什么情况必须**继续往下跑**。
       例如全跑模式下估值快照失败只告警（此时日 K 已落库，退出会留下不一致的库），
       但 securities 阶段失败必须立刻退出（后面全都依赖证券列表）。
    2. 增量日期推导 —— resolve_price_dates 算错一天就是全市场少同步一天。
    3. 公告的增量水位与二次去重 —— 漏了会重复灌库，多了会漏公告。

  全部用 fake service / 临时库，不联网。

运行：
  ./venv/bin/python -m pytest tests/test_sync_cli.py -v
"""

import argparse
import datetime
import os
import sys

import pandas as pd
import pytest

if "stocklab" not in sys.modules:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.scripts import sync_market_data as cli
from stocklab.persistence import Database
from stocklab.domain import (
    ANNOUNCEMENT_COLUMNS,
    BALANCE_SHEET_COLUMNS,
    DAILY_PRICE_COLUMNS,
    DAILY_VALUATION_COLUMNS,
    INDEX_MEMBERSHIP_COLUMNS,
    INDUSTRY_VALUATION_COLUMNS,
    INCOME_STATEMENT_COLUMNS,
    SECURITY_COLUMNS,
    VALUATION_HISTORY_COLUMNS,
)


def _row(columns, values):
    """按领域列常量补全一行假数据（返回 dict）

    【为什么必须补全】
    Repository 写入前会逐列核对数据契约，缺列直接拒收（这正是 test_data_contract
    在保证的东西）。这里若只给关心的几列，插入会被静默拒绝，测试就会看到
    「upsert 返回 0 条」这种看不出因果的失败。
    """
    row = {name: None for name in columns}
    row.update(values)
    return row


def _frame(columns, *rows):
    """按领域列常量建一帧假数据（列序对齐契约，避免 pandas 补出无名列）"""
    return pd.DataFrame(list(rows), columns=list(columns))


# ---------------------------------------------------------------------------
# 假数据源
# ---------------------------------------------------------------------------
def _securities_frame():
    codes = ["600519.SH", "000001.SZ"]
    rows = []
    for code, name, exchange in zip(codes, ["贵州茅台", "平安银行"], ["SH", "SZ"]):
        rows.append({
            "ts_code": code, "symbol": code.split(".")[0], "name": name,
            "exchange": exchange, "market": "主板",
            "industry": "食品饮料" if exchange == "SH" else "银行",
            "area": None, "list_date": None, "delist_date": None,
            "status": "LISTED", "is_hs": False,
        })
    return pd.DataFrame(rows)[list(SECURITY_COLUMNS)]


def _daily_prices_frame(ts_code):
    rows = []
    for day, close in ((datetime.date(2026, 9, 28), 11.0), (datetime.date(2026, 9, 29), 12.0)):
        rows.append({
            "ts_code": ts_code, "trade_date": day,
            "open": 10.0, "high": close + 1.0, "low": 9.0, "close": close,
            "pre_close": close - 1.0, "change": 1.0, "pct_chg": 0.09,
            "volume": 1000.0, "amount": close * 1000.0,
        })
    return pd.DataFrame(rows)[list(DAILY_PRICE_COLUMNS)]


class FakeMarketService:
    """可控的行情/估值数据源桩"""

    def __init__(self, **behaviour):
        self.behaviour = behaviour
        self.calls = []

    def _result(self, key, default):
        value = self.behaviour.get(key, default)
        return value() if callable(value) else value

    def fetch_securities(self):
        self.calls.append("fetch_securities")
        return self._result("securities", _securities_frame())

    def fetch_daily_prices(self, ts_code, start_date, end_date):
        self.calls.append(("fetch_daily_prices", ts_code))
        # daily_prices 的桩允许是一个按 ts_code 取值的函数，
        # 这样用例可以显式指定「哪只失败」，而不是靠调用顺序推断
        value = self.behaviour.get("daily_prices", None)
        if callable(value):
            return value(ts_code)
        return value if value is not None else _daily_prices_frame(ts_code)

    def fetch_realtime_valuations(self):
        self.calls.append("fetch_realtime_valuations")
        return self._result("valuations", pd.DataFrame())

    def fetch_valuation_history(self, ts_code, period):
        self.calls.append(("fetch_valuation_history", ts_code))
        return self._result("valuation_history", pd.DataFrame())

    def fetch_index_membership(self, index_code):
        self.calls.append(("fetch_index_membership", index_code))
        return self._result("index_membership", pd.DataFrame())

    def fetch_industry_valuation(self, stat_date, classification):
        self.calls.append(("fetch_industry_valuation", classification))
        return self._result("industry_valuation", pd.DataFrame())


class FakeLifecycleService:
    def __init__(self, listing, delisting):
        self._listing = listing
        self._delisting = delisting

    def fetch_listing_calendar(self):
        return self._listing

    def fetch_delisting_calendar(self):
        return self._delisting


class FakeFundamentalService:
    def __init__(self, income, balance, cashflow):
        self._frames = (income, balance, cashflow)

    def fetch_income_statement(self, ts_code):
        return self._frames[0]

    def fetch_balance_sheet(self, ts_code):
        return self._frames[1]

    def fetch_cashflow_statement(self, ts_code):
        return self._frames[2]


@pytest.fixture
def service(monkeypatch):
    """把脚本里的 MarketService 换成桩（脚本在函数内部 new，所以要 monkeypatch 类）"""
    fake = FakeMarketService()
    monkeypatch.setattr(cli, "MarketService", lambda *a, **k: fake)
    return fake


@pytest.fixture
def database(tmp_path):
    db = Database(str(tmp_path / "sync.duckdb"))
    yield db
    db.close()


def _push_trade_dates_into_future(database, first=datetime.date(2099, 1, 1)):
    """把已有日 K 的交易日期推到未来，且每行日期互不相同

    【为什么不能一条 UPDATE 搞定】
    日 K 主键是 (ts_code, trade_date)，把多行推到同一天会触发唯一键冲突；
    而 DuckDB 的 UPDATE 又不允许在 SET 里用窗口函数（Binder Error）。
    所以只能先查出来再按行改。
    """
    conn = database.get_connection()
    rows = conn.execute(
        "SELECT ts_code, trade_date FROM market.daily_prices ORDER BY ts_code, trade_date"
    ).fetchall()
    for index, (ts_code, _) in enumerate(rows):
        conn.execute(
            "UPDATE market.daily_prices SET trade_date = ? WHERE ts_code = ? "
            "AND trade_date = ?",
            [first + datetime.timedelta(days=index), ts_code,
             _as_date(rows[index][1])],
        )
    return first + datetime.timedelta(days=max(len(rows) - 1, 0))


def _as_date(value):
    """DuckDB DATE 读回可能是 date / Timestamp / 字符串，统一成 date"""
    return value if isinstance(value, datetime.date) else pd.Timestamp(value).date()


def _args(**overrides):
    base = dict(command=None, period="近五年", stat_date=None,
                classification="国证行业分类", workers=2, ts_code=None,
                start_date=None, end_date=None, incremental=False,
                securities_only=False, db_path=None)
    base.update(overrides)
    return argparse.Namespace(**base)


# ---------------------------------------------------------------------------
# 一、参数解析
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("argv,expected", [
    ([], None),
    (["securities"], "securities"),
    (["prices"], "prices"),
    (["valuations"], "valuations"),
    (["valuation-history"], "valuation-history"),
    (["indexes"], "indexes"),
    (["industries"], "industries"),
    (["lifecycle"], "lifecycle"),
    (["fundamentals"], "fundamentals"),
    (["announcements"], "announcements"),
])
def test_parse_args_commands(monkeypatch, argv, expected):
    monkeypatch.setattr(sys, "argv", ["sync_market_data.py"] + argv)
    assert cli.parse_args().command == expected


def test_parse_args_defaults(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["sync_market_data.py"])
    args = cli.parse_args()
    assert args.period == "近五年"
    assert args.workers == 8
    assert args.classification == "国证行业分类"
    assert args.incremental is False
    assert args.securities_only is False
    assert args.db_path is None


def test_parse_args_public_options_before_or_after_command(monkeypatch):
    """公共参数放在子命令前后都要能解析

    这正是脚本用可选位置参数而非 add_subparsers 的原因：子解析器会用自身
    默认值覆盖父解析器已解析的结果，放在前面就丢了。
    """
    monkeypatch.setattr(sys, "argv", ["sync_market_data.py", "--period", "近十年",
                                      "valuation-history", "--workers", "3"])
    args = cli.parse_args()
    assert args.command == "valuation-history"
    assert args.period == "近十年", "子命令后面的参数必须生效"
    assert args.workers == 3


def test_parse_args_rejects_unknown_command(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["sync_market_data.py", "nope"])
    with pytest.raises(SystemExit):
        cli.parse_args()


# ---------------------------------------------------------------------------
# 二、阶段一：证券基础信息
# ---------------------------------------------------------------------------
def test_sync_securities(database, service):
    assert cli.sync_securities(database) is True
    assert len(cli.SecurityRepository(database).find_all()) == 2


def test_sync_securities_empty_is_failure(database, service):
    service.behaviour["securities"] = pd.DataFrame()
    assert cli.sync_securities(database) is False


# ---------------------------------------------------------------------------
# 三、阶段二：日 K 与日期范围推导
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("argv,start_expected,end_expected,skip_expected", [
    # 指定区间：短横线要被去掉
    (dict(start_date="2025-01-01", end_date="2025-12-31"), "20250101", "20251231", False),
    # 只给起始：结束默认今天
    (dict(start_date="2025-01-01"), "20250101", None, False),
    # 都不给：默认近 30 天
    (dict(), None, None, False),
])
def test_resolve_price_dates_explicit(database, argv, start_expected,
                                      end_expected, skip_expected):
    start, end, skip = cli.resolve_price_dates(_args(**argv), database)
    assert skip is skip_expected
    if start_expected:
        assert start == start_expected
    if end_expected:
        assert end == end_expected
    assert len(start) == 8 and start.isdigit(), "必须是 YYYYMMDD"
    assert len(end) == 8 and end.isdigit()


def test_resolve_price_dates_incremental_from_watermark(database, service):
    """增量：从本地最新交易日的次日开始"""
    cli.sync_securities(database)
    cli.sync_daily_prices(database, "20260928", "20260929")
    start, end, skip = cli.resolve_price_dates(_args(incremental=True), database)
    assert start == "20260930", "应从最新交易日次日开始"
    assert skip is False


def test_resolve_price_dates_incremental_on_empty_db(database):
    """本地无数据：默认回补近 30 天，而不是报错"""
    start, end, skip = cli.resolve_price_dates(_args(incremental=True), database)
    assert skip is False
    today = datetime.datetime.now().strftime("%Y%m%d")
    assert end == today


def test_resolve_price_dates_incremental_already_current(database, service):
    """本地已是最新：必须给出 skip=True，否则会做一次空跑的全市场请求

    先落真实日 K 行，再把水位推到 2099 —— 表里没有行时 UPDATE 是空操作，
    水位仍为 2026-09-29，测不到 skip 分支。
    """
    cli.sync_securities(database)
    cli.sync_daily_prices(database, "20260928", "20260929")
    _push_trade_dates_into_future(database)
    start, end, skip = cli.resolve_price_dates(
        _args(incremental=True), database)
    assert skip is True, "本地已最新时必须跳过"


def test_sync_daily_prices(database, service):
    cli.sync_securities(database)
    assert cli.sync_daily_prices(database, "20260928", "20260929") is True
    rows = cli.DailyPriceRepository(database).find_by_code("600519.SH")
    assert len(rows) == 2


def test_sync_daily_prices_without_securities(database, service):
    """证券列表为空必须失败并提示先跑 securities"""
    assert cli.sync_daily_prices(database, "20260928", "20260929") is False


def test_sync_daily_prices_partial_failure(database, service):
    """部分失败仍算成功（全市场 5400 只，某只取不到不该让整阶段失败）

    【不要用「调用次数」决定行为】
    早先的写法是 `lambda: 空表 if 600519.SH 已被调用过 else 600519 的数据`，
    它同时有两个毛病：既依赖 securities 的返回顺序（顺序一变结论就翻转），
    又会把一只股票的数据写进另一只的行里。这里改成按代码显式指定。
    """
    cli.sync_securities(database)
    failing = {"600519.SH"}
    service.behaviour["daily_prices"] = (
        lambda code: pd.DataFrame() if code in failing else _daily_prices_frame(code))
    assert cli.sync_daily_prices(database, "20260928", "20260929") is True
    stored = cli.DailyPriceRepository(database).find_by_code("000001.SZ")
    assert len(stored) == 2, "成功的标的必须落库"
    assert cli.DailyPriceRepository(database).find_by_code("600519.SH").empty, \
        "失败的标的不应有任何行"


def test_sync_daily_prices_total_failure(database, service):
    service.behaviour["daily_prices"] = pd.DataFrame()
    cli.sync_securities(database)
    assert cli.sync_daily_prices(database, "20260928", "20260929") is False


def test_run_prices_stage_skips_when_current(database, service):
    """skip=True 时 run_prices_stage 直接返回 True，且不得发起任何取数请求"""
    cli.sync_securities(database)
    cli.sync_daily_prices(database, "20260928", "20260929")
    _push_trade_dates_into_future(database)
    before = len(service.calls)
    assert cli.run_prices_stage(_args(incremental=True), database) is True
    assert len(service.calls) == before, "跳过时不应再取数"


# ---------------------------------------------------------------------------
# 四、阶段三 / 四 / 五 / 六
# ---------------------------------------------------------------------------
def test_sync_valuations(database, service):
    service.behaviour["valuations"] = _frame(
        DAILY_VALUATION_COLUMNS,
        _row(DAILY_VALUATION_COLUMNS,
             {"ts_code": "600519.SH", "trade_date": datetime.date(2026, 9, 30),
              "pe_ttm": 28.0, "pb": 10.0, "ps": 13.0, "dv_ttm": 2.3}))
    assert cli.sync_valuations(database) is True


def test_sync_valuations_empty_is_failure(database, service):
    service.behaviour["valuations"] = pd.DataFrame()
    assert cli.sync_valuations(database) is False


def test_sync_valuation_history_requires_securities(database, service):
    assert cli.sync_valuation_history(database, "近五年") is False


def test_sync_valuation_history(database, service, monkeypatch):
    cli.sync_securities(database)
    frame = _frame(
        VALUATION_HISTORY_COLUMNS,
        _row(VALUATION_HISTORY_COLUMNS,
             {"ts_code": "600519.SH", "trade_date": datetime.date(2025, 1, 2),
              "pe_ttm": 28.0, "pb": 10.0, "ps": 13.0, "pe_static": 30.0, "pcf": 20.0}),
        _row(VALUATION_HISTORY_COLUMNS,
             {"ts_code": "600519.SH", "trade_date": datetime.date(2025, 1, 3),
              "pe_ttm": 27.0, "pb": 9.9, "ps": 12.8, "pe_static": 29.0, "pcf": 19.5}),
    )
    monkeypatch.setattr(cli, "MarketService",
                        lambda *a, **k: FakeMarketService(valuation_history=frame))
    assert cli.sync_valuation_history(database, "近五年", max_workers=2) is True


def test_sync_valuation_history_with_explicit_codes(database, service):
    """限定代码时只抓指定标的，不受证券表规模影响

    注意仍必须先跑阶段一：脚本里「证券列表为空」的守卫写在 ts_code_list 分支
    之前，所以定点补齐也依赖证券主表已就位。这是刻意的（阶段一是所有阶段的前置），
    这里一并钉住，免得有人把守卫挪到分支之后。
    """
    cli.sync_securities(database)
    service.behaviour["valuation_history"] = _frame(
        VALUATION_HISTORY_COLUMNS,
        _row(VALUATION_HISTORY_COLUMNS,
             {"ts_code": "600519.SH", "trade_date": datetime.date(2025, 1, 2),
              "pe_ttm": 28.0, "pb": 10.0, "ps": 13.0, "pe_static": 30.0, "pcf": 20.0}))
    assert cli.sync_valuation_history(
        database, "近五年", ts_code_list=["600519.SH"], max_workers=1) is True


def test_sync_valuation_history_requires_securities_even_with_codes(database, service):
    """给了 --ts-code 也必须先有证券主表"""
    service.behaviour["valuation_history"] = _frame(
        VALUATION_HISTORY_COLUMNS,
        _row(VALUATION_HISTORY_COLUMNS,
             {"ts_code": "600519.SH", "trade_date": datetime.date(2025, 1, 2),
              "pe_ttm": 28.0}))
    assert cli.sync_valuation_history(
        database, "近五年", ts_code_list=["600519.SH"], max_workers=1) is False


def test_sync_valuation_history_total_failure(database, service):
    cli.sync_securities(database)
    service.behaviour["valuation_history"] = pd.DataFrame()
    assert cli.sync_valuation_history(database, "近五年", max_workers=1) is False


def test_sync_index_membership(database, service):
    service.behaviour["index_membership"] = _frame(
        INDEX_MEMBERSHIP_COLUMNS,
        *[_row(INDEX_MEMBERSHIP_COLUMNS,
               {"ts_code": code, "index_code": "000300.SH", "index_name": "沪深300",
                "effective_date": datetime.date(2020, 1, 1)})
          for code in ("600519.SH", "000001.SZ")])
    assert cli.sync_index_membership(database) is True


def test_sync_index_membership_all_failed(database, service):
    service.behaviour["index_membership"] = pd.DataFrame()
    assert cli.sync_index_membership(database) is False


def test_sync_industry_valuation(database, service):
    service.behaviour["industry_valuation"] = _frame(
        INDUSTRY_VALUATION_COLUMNS,
        _row(INDUSTRY_VALUATION_COLUMNS,
             {"industry_code": "1", "stat_date": datetime.date(2026, 9, 30),
              "classification": "国证行业分类", "industry_level": 1,
              "industry_name": "食品饮料", "company_count": 50,
              "priced_company_count": 45, "pe_weighted": 30.0, "pe_median": 35.0,
              "pe_arithmetic": 40.0, "total_market_value": 1e12, "net_profit": 3e10}))
    assert cli.sync_industry_valuation(database, "2026-09-30") is True


def test_sync_industry_valuation_empty(database, service):
    service.behaviour["industry_valuation"] = pd.DataFrame()
    assert cli.sync_industry_valuation(database, "2026-09-30") is False


# ---------------------------------------------------------------------------
# 五、阶段七：生命周期（失败必须整体中止，不能写半成品）
# ---------------------------------------------------------------------------
def _listing_calendar():
    return pd.DataFrame({
        "ts_code": ["600519.SH", "000001.SZ"], "name": ["贵州茅台", "平安银行"],
        "list_date": [datetime.date(1990, 12, 19), datetime.date(1991, 4, 3)],
    })


def _delisting_calendar():
    return pd.DataFrame({
        "ts_code": ["600519.SH"], "name": ["贵州茅台"],
        "list_date": [datetime.date(1990, 12, 19)],
        "delist_date": [datetime.date(2024, 11, 29)],
    })


def _install_lifecycle(monkeypatch, listing, delisting):
    monkeypatch.setattr(cli, "LifecycleService",
                        lambda *a, **k: FakeLifecycleService(listing, delisting))


def test_sync_lifecycle(database, service, monkeypatch):
    cli.sync_securities(database)
    _install_lifecycle(monkeypatch, _listing_calendar(), _delisting_calendar())
    assert cli.sync_lifecycle(database) is True
    row = cli.SecurityRepository(database).find_by_code("600519.SH").iloc[0]
    assert row["status"] == "DELISTED"
    assert pd.to_datetime(row["delist_date"]).date() == datetime.date(2024, 11, 29)


def test_sync_lifecycle_aborts_when_listing_missing(database, service, monkeypatch):
    """日历不全时整体不写入 —— 写半成品会静默污染股票池推导"""
    cli.sync_securities(database)
    _install_lifecycle(monkeypatch, pd.DataFrame(), _delisting_calendar())
    assert cli.sync_lifecycle(database) is False
    row = cli.SecurityRepository(database).find_by_code("600519.SH").iloc[0]
    assert pd.isna(row["delist_date"]), "失败时不得写入任何生命周期字段"


def test_sync_lifecycle_aborts_when_delisting_missing(database, service, monkeypatch):
    cli.sync_securities(database)
    _install_lifecycle(monkeypatch, _listing_calendar(), pd.DataFrame())
    assert cli.sync_lifecycle(database) is False


# ---------------------------------------------------------------------------
# 六、阶段八：基本面
# ---------------------------------------------------------------------------
def _income_frame():
    return _frame(INCOME_STATEMENT_COLUMNS, _row(INCOME_STATEMENT_COLUMNS, {
        "ts_code": "600519.SH", "report_period": datetime.date(2025, 12, 31),
        "announce_date": datetime.date(2026, 4, 1),
        "available_date": datetime.date(2026, 4, 1), "source": "test",
        "revenue": 1.5e11, "net_profit": 8.6e10, "eps": 68.0}))


def _balance_frame():
    return _frame(BALANCE_SHEET_COLUMNS, _row(BALANCE_SHEET_COLUMNS, {
        "ts_code": "600519.SH", "report_period": datetime.date(2025, 12, 31),
        "announce_date": datetime.date(2026, 4, 1),
        "available_date": datetime.date(2026, 4, 1), "source": "test",
        "total_assets": 3e12, "total_liabilities": 3e11, "equity": 2.5e12}))


def test_sync_fundamentals_requires_securities(database, monkeypatch):
    assert cli.sync_fundamentals(database, max_workers=1) is False


def test_sync_fundamentals(database, service, monkeypatch):
    cli.sync_securities(database)
    _install_fundamental(monkeypatch, _income_frame(), _balance_frame(),
                         pd.DataFrame())
    assert cli.sync_fundamentals(
        database, ts_code_list=["600519.SH"], max_workers=1) is True
    assert len(cli.IncomeStatementRepository(database).find_by_code("600519.SH")) == 1


def test_sync_fundamentals_missing_balance_still_writes_income(database, service, monkeypatch):
    """资产负债表缺失时报表仍要入库（指标可事后重算，报表才是原始事实）"""
    cli.sync_securities(database)
    _install_fundamental(monkeypatch, _income_frame(), pd.DataFrame(), pd.DataFrame())
    assert cli.sync_fundamentals(
        database, ts_code_list=["600519.SH"], max_workers=1) is True
    assert len(cli.IncomeStatementRepository(database).find_by_code("600519.SH")) == 1


def test_sync_fundamentals_total_failure(database, service, monkeypatch):
    cli.sync_securities(database)
    _install_fundamental(monkeypatch, pd.DataFrame(), pd.DataFrame(), pd.DataFrame())
    assert cli.sync_fundamentals(
        database, ts_code_list=["600519.SH"], max_workers=1) is False


def _install_fundamental(monkeypatch, income, balance, cashflow):
    monkeypatch.setattr(cli, "FundamentalService",
                        lambda *a, **k: FakeFundamentalService(income, balance, cashflow))


# ---------------------------------------------------------------------------
# 七、阶段九：公告（增量水位 + 二次去重 + A 股过滤）
# ---------------------------------------------------------------------------
def _announcement_frame(codes=("600519.SH",), day=datetime.date(2026, 9, 1)):
    return _frame(
        ANNOUNCEMENT_COLUMNS,
        *[_row(ANNOUNCEMENT_COLUMNS,
               {"ts_code": code,
                "announcement_id": "%s-%d" % (code, index),
                "announcement_date": day,
                "title": "公告%d" % index,
                "source": "cninfo",
                "pdf_url": "http://example/%s.pdf" % code})
          for index, code in enumerate(codes)])


class FakeCninfoClient:
    def __init__(self, frames):
        self._frames = frames
        self.calls = []

    def fetch_announcements(self, ts_code, start, end, max_pages=100):
        self.calls.append((ts_code, start, end))
        return self._frames.get(ts_code, pd.DataFrame())


def test_sync_announcements_requires_start_date_on_empty(database):
    """表为空时必须拒绝无限回溯，要显式给起始日期"""
    assert cli.sync_announcements(
        database, client=FakeCninfoClient({})) is False


def test_sync_announcements_rejects_reversed_range(database):
    assert cli.sync_announcements(
        database, start_date="2026-09-30", end_date="2026-01-01",
        client=FakeCninfoClient({})) is False


def test_sync_announcements_whole_market(database, service):
    cli.sync_securities(database)
    client = FakeCninfoClient({None: _announcement_frame()})
    assert cli.sync_announcements(
        database, start_date="2026-01-01", end_date="2026-12-31", client=client) is True
    assert client.calls == [(None, "2026-01-01", "2026-12-31")]


def test_sync_announcements_per_code_mode(database, service):
    cli.sync_securities(database)
    client = FakeCninfoClient({"600519.SH": _announcement_frame(("600519.SH",))})
    assert cli.sync_announcements(
        database, start_date="2026-01-01", end_date="2026-12-31",
        ts_codes=["600519.SH"], client=client) is True
    assert client.calls[0][0] == "600519.SH", "逐只模式必须带 stock 参数"


def test_sync_announcements_filters_non_a_share(database, service):
    """全市场模式会把债券/ETF 的公告也带回来，必须按 A 股股票池过滤"""
    cli.sync_securities(database)   # 只有 600519.SH / 000001.SZ
    frame = _announcement_frame(("600519.SH", "123456.SH"))
    client = FakeCninfoClient({None: frame})
    cli.sync_announcements(database, start_date="2026-01-01",
                           end_date="2026-12-31", client=client)
    stored = database.get_connection().execute(
        "SELECT DISTINCT ts_code FROM corporate.announcements").fetchall()
    assert {row[0] for row in stored} == {"600519.SH"}


def test_sync_announcements_dedupes_against_previous_runs(database, service):
    """跨批次去重：上次已落库的 (代码, 日期, 标题) 本次不再重复写入

    _write_announcements 的 existing_keys 取自库内已有行，且每写入一批后就地
    补进该集合，因此「上次已有、本次又抓到」会被正确去重。
    """
    cli.sync_securities(database)
    first = FakeCninfoClient({None: _announcement_frame(day=datetime.date(2026, 9, 1))})
    assert cli.sync_announcements(database, start_date="2026-01-01",
                                   end_date="2026-12-31", client=first) is True

    again = FakeCninfoClient({None: _announcement_frame(day=datetime.date(2026, 9, 1))})
    cli.sync_announcements(database, start_date="2026-01-01",
                           end_date="2026-12-31", client=again)
    stored = database.get_connection().execute(
        "SELECT COUNT(*) FROM corporate.announcements").fetchone()[0]
    assert stored == 1, "重复执行不应产生重复行，实际 %d 行" % stored


def test_sync_announcements_does_not_dedupe_within_one_batch(database, service):
    """【已知缺口，钉住现状】同一批内的同内容公告不会被去重

    _write_announcements 的去重是拿每一行去比对 existing_keys（来源是库内已有行），
    但比较用的集合在整批过滤期间**不会随本批结果增长** —— 集合只在 insert 之后
    才补。于是同一批里两条 (代码, 日期, 标题) 相同、announcement_id 不同的记录
    会双双落库；主键是 announcement_id，拦不住。

    影响面：巨潮一页内若同时给出「原公告」与「更正后重发」（新 id、同标题同日）
    就会重复。修法是在算 keep_mask 时顺带累积已保留的键（一行即可），但那会改动
    同步语义，不在「补覆盖率」这次改动范围内，故此处按现状钉住并记录。
    """
    cli.sync_securities(database)
    frame = pd.concat([
        _announcement_frame(("600519.SH",), datetime.date(2026, 9, 1)),
        _announcement_frame(("600519.SH",), datetime.date(2026, 9, 1)),
    ], ignore_index=True)
    frame.loc[1, "announcement_id"] = "OTHER-ID"
    client = FakeCninfoClient({None: frame})
    cli.sync_announcements(database, start_date="2026-01-01",
                           end_date="2026-12-31", client=client)
    stored = database.get_connection().execute(
        "SELECT announcement_id FROM corporate.announcements ORDER BY announcement_id"
    ).fetchall()
    assert len(stored) == 2, "现状是两条都落库；修复后本用例会红，需同步改期望为 1"


def test_sync_announcements_uses_watermark_as_start(database, service):
    """未指定 --start-date 时取表内水位，而不是回溯到远古

    水位来自表内已有数据，所以必须先同步过一次；空表时脚本会直接拒绝
    （见 test_sync_announcements_requires_start_date_on_empty）。
    """
    cli.sync_securities(database)
    first = FakeCninfoClient({None: _announcement_frame(day=datetime.date(2026, 9, 1))})
    assert cli.sync_announcements(database, start_date="2026-01-01",
                                   end_date="2026-12-31", client=first) is True

    second = FakeCninfoClient({None: _announcement_frame(day=datetime.date(2026, 10, 1))})
    assert cli.sync_announcements(database, end_date="2026-12-31",
                                   client=second) is True
    assert second.calls[0][1] == "2026-09-01", "起点应是表内水位（上一条公告日期）"


def test_write_announcements_empty_frame(database):
    repository = cli.AnnouncementRepository(database)
    assert cli._write_announcements(repository, pd.DataFrame(), set()) == 0
    assert cli._write_announcements(repository, None, set()) == 0


def test_write_announcements_all_duplicates(database, service):
    """全部命中已有键时直接返回 0，且不得写库"""
    cli.sync_securities(database)
    repository = cli.AnnouncementRepository(database)
    frame = _announcement_frame(("600519.SH",))
    keys = {("600519.SH", datetime.date(2026, 9, 1), "公告0")}
    assert cli._write_announcements(repository, frame, keys) == 0


# ---------------------------------------------------------------------------
# 八、main() 的编排
# ---------------------------------------------------------------------------
def _patch_main_deps(monkeypatch, **stages):
    """把 main() 依赖的阶段函数与数据库初始化全部替换掉"""
    calls = []
    for name, result in stages.items():
        def make(name=name, result=result):
            def stub(*args, **kwargs):
                calls.append(name)
                return result
            return stub
        monkeypatch.setattr(cli, name, make())
    monkeypatch.setattr(cli, "install_browser_user_agent", lambda: None)
    monkeypatch.setattr(cli, "initialize_database", lambda path: "ignored.duckdb")
    return calls


def test_main_runs_default_stages(monkeypatch):
    """无子命令 = 一键全跑阶段一/二/三/五/七"""
    calls = _patch_main_deps(
        monkeypatch, sync_securities=True, sync_daily_prices=True,
        sync_valuations=True, sync_index_membership=True, sync_lifecycle=True)
    monkeypatch.setattr(sys, "argv", ["sync_market_data.py"])
    monkeypatch.setattr(cli, "Database",
                        lambda *a, **k: __import__("contextlib").nullcontext(object()))
    cli.main()
    assert calls == ["sync_securities", "sync_daily_prices", "sync_valuations",
                     "sync_index_membership", "sync_lifecycle"]


def test_main_exits_when_securities_fail(monkeypatch):
    """阶段一失败必须立刻退出：后面全都依赖证券列表"""
    calls = _patch_main_deps(monkeypatch, sync_securities=False)
    monkeypatch.setattr(sys, "argv", ["sync_market_data.py"])
    monkeypatch.setattr(cli, "Database",
                        lambda *a, **k: __import__("contextlib").nullcontext(object()))
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == 1
    assert "sync_daily_prices" not in calls, "证券失败后不得继续"


def test_main_securities_only_legacy_flag(monkeypatch):
    """--securities-only 是旧用法，等价于只跑阶段一"""
    calls = _patch_main_deps(monkeypatch, sync_securities=True)
    monkeypatch.setattr(sys, "argv", ["sync_market_data.py", "--securities-only"])
    monkeypatch.setattr(cli, "Database",
                        lambda *a, **k: __import__("contextlib").nullcontext(object()))
    cli.main()
    assert calls == ["sync_securities"], "只应执行阶段一"


def test_main_tolerates_valuation_failure_in_full_run(monkeypatch):
    """全跑模式下估值快照失败只告警：此时日 K 已落库，退出会留下不一致的库"""
    calls = _patch_main_deps(monkeypatch, sync_securities=True,
                             sync_daily_prices=True, sync_valuations=False,
                             sync_index_membership=False, sync_lifecycle=True)
    monkeypatch.setattr(sys, "argv", ["sync_market_data.py"])
    monkeypatch.setattr(cli, "Database",
                        lambda *a, **k: __import__("contextlib").nullcontext(object()))
    cli.main()
    assert "sync_lifecycle" in calls, "估值失败不应中断后续阶段"


def test_main_exits_when_valuation_fails_standalone(monkeypatch):
    """单独跑 valuations 子命令时失败必须给退出码 1"""
    _patch_main_deps(monkeypatch, sync_securities=True, sync_valuations=False)
    monkeypatch.setattr(sys, "argv", ["sync_market_data.py", "valuations"])
    monkeypatch.setattr(cli, "Database",
                        lambda *a, **k: __import__("contextlib").nullcontext(object()))
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == 1


def test_main_tolerates_index_failure(monkeypatch):
    """指数成分失败只告警：它是辅助维度，不影响其他阶段数据"""
    calls = _patch_main_deps(monkeypatch, sync_securities=True,
                             sync_daily_prices=True, sync_valuations=True,
                             sync_index_membership=False, sync_lifecycle=True)
    monkeypatch.setattr(sys, "argv", ["sync_market_data.py"])
    monkeypatch.setattr(cli, "Database",
                        lambda *a, **k: __import__("contextlib").nullcontext(object()))
    cli.main()
    assert "sync_lifecycle" in calls


def test_main_exits_when_lifecycle_fails(monkeypatch):
    """生命周期失败必须退出：它是历史研究（幸存者偏差）的前提"""
    _patch_main_deps(monkeypatch, sync_securities=True, sync_daily_prices=True,
                     sync_valuations=True, sync_index_membership=True,
                     sync_lifecycle=False)
    monkeypatch.setattr(sys, "argv", ["sync_market_data.py"])
    monkeypatch.setattr(cli, "Database",
                        lambda *a, **k: __import__("contextlib").nullcontext(object()))
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == 1


@pytest.mark.parametrize("command,stage", [
    ("valuation-history", "sync_valuation_history"),
    ("industries", "sync_industry_valuation"),
    ("fundamentals", "sync_fundamentals"),
    ("announcements", "sync_announcements"),
])
def test_main_optional_stages_only_on_demand(monkeypatch, command, stage):
    """阶段四/六/八/九默认不参与一键全跑，必须显式触发"""
    calls = _patch_main_deps(
        monkeypatch, sync_securities=True, sync_daily_prices=True,
        sync_valuations=True, sync_index_membership=True, sync_lifecycle=True,
        **{stage: True})
    monkeypatch.setattr(sys, "argv", ["sync_market_data.py"])
    monkeypatch.setattr(cli, "Database",
                        lambda *a, **k: __import__("contextlib").nullcontext(object()))
    cli.main()
    assert stage not in calls, "%s 不该在全跑模式下执行" % stage


@pytest.mark.parametrize("command,stage", [
    ("valuation-history", "sync_valuation_history"),
    ("industries", "sync_industry_valuation"),
    ("fundamentals", "sync_fundamentals"),
    ("announcements", "sync_announcements"),
])
def test_main_optional_stages_run_when_requested(monkeypatch, command, stage):
    calls = _patch_main_deps(monkeypatch, **{stage: True})
    monkeypatch.setattr(sys, "argv", ["sync_market_data.py", command])
    monkeypatch.setattr(cli, "Database",
                        lambda *a, **k: __import__("contextlib").nullcontext(object()))
    cli.main()
    assert calls == [stage]


@pytest.mark.parametrize("command,stage", [
    ("valuation-history", "sync_valuation_history"),
    ("industries", "sync_industry_valuation"),
    ("fundamentals", "sync_fundamentals"),
    ("announcements", "sync_announcements"),
])
def test_main_optional_stage_failure_exits(monkeypatch, command, stage):
    _patch_main_deps(monkeypatch, **{stage: False})
    monkeypatch.setattr(sys, "argv", ["sync_market_data.py", command])
    monkeypatch.setattr(cli, "Database",
                        lambda *a, **k: __import__("contextlib").nullcontext(object()))
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == 1


def test_main_forwards_workers_to_history_stage(monkeypatch):
    """--workers 必须透传到并发阶段，否则 12 并发的调优参数形同虚设"""
    seen = {}

    def stub(database, period, max_workers=8):
        seen["period"] = period
        seen["max_workers"] = max_workers
        return True

    _patch_main_deps(monkeypatch, sync_valuation_history=True)
    monkeypatch.setattr(cli, "sync_valuation_history", stub)
    monkeypatch.setattr(sys, "argv", ["sync_market_data.py", "valuation-history",
                                      "--period", "近十年", "--workers", "12"])
    monkeypatch.setattr(cli, "Database",
                        lambda *a, **k: __import__("contextlib").nullcontext(object()))
    cli.main()
    assert seen == {"period": "近十年", "max_workers": 12}