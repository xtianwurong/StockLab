"""
单股取数的五级降级链 (tests/test_quote_service.py)

覆盖 stocklab/datasource/quote_service.py —— 项目里降级逻辑最密的一层：
  价格链  AkShare -> BaoStock -> 腾讯 -> 新浪 -> 通达信（全部失败才返回空表）
  估值链  AkShare -> BaoStock
  简称链  腾讯 -> AkShare -> BaoStock（新浪/通达信不参与）
  盘口链  腾讯 -> 新浪 -> 通达信
  外加    价格 + PE 对齐、实时行情字段派生

【为什么必须逐级测】
  降级链最容易出的错是「跳级」和「记错生效源」：某一级被跳过，表现为偶发拿不到数据；
  used_source_name 记错，表现为日志与排查结论对不上。这两类都不会抛异常，
  只有把每一级的「失败后是否走到下一级」「最终记的是哪一级」都钉住才测得出来。

运行：
  ./venv/bin/python -m pytest tests/test_quote_service.py -v
  （五个通道全部用桩替换，不联网）
"""

import datetime
import os
import sys

import pandas as pd
import pytest

if "stocklab" not in sys.modules:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stocklab.datasource import quote_service as qs


# 注意 trade_date 必须是 pandas 时间戳：合并走 `merged[...].dt.to_period("M")`，
# 传 datetime.date 会抛 "Can only use .dt accessor with datetimelike values"。
PRICE_FRAME = pd.DataFrame({"trade_date": pd.to_datetime(["2026-08-03"]),
                            "close_price": [10.0]})

PE_FRAME = pd.DataFrame({"trade_date": pd.to_datetime(["2026-08-03"]),
                         "pe_ttm": [20.0]})


# 真实通道自己吞掉上游异常并返回空表（它们的契约是「失败即空表」），
# 所以服务层判断降级只看 .empty，不看异常。桩默认就返回空表来贴合这个契约；
# 「服务层不 catch 通道异常」这一点由 test_quote_service_propagates_channel_error 单独钉住。
EMPTY = pd.DataFrame()
FAIL = object()          # 哨兵：让桩按方法各自的失败返回类型给结果


class FakeSource:
    """单个通道的桩：给什么就返回什么，可以是空表或异常"""

    SOURCE_NAME = "fake"

    def __init__(self, result):
        self._result = result
        self.calls = []

    def _resolve(self, failed):
        if self._result is FAIL:
            return failed
        if isinstance(self._result, Exception):
            raise self._result
        if callable(self._result):
            return self._result()
        return self._result

    def fetch_monthly_close_prices(self, stock_code, adjust_type="qfq",
                                   retry_count=3, retry_interval=3):
        """签名与各通道真实实现一致（含默认参数，便于只传 stock_code 的用例）"""
        self.calls.append(("price", (stock_code, adjust_type),
                           {"retry_count": retry_count,
                            "retry_interval": retry_interval}))
        return self._resolve(EMPTY)

    def fetch_monthly_pe_ttm(self, stock_code):
        self.calls.append(("pe", (stock_code,), {}))
        return self._resolve(EMPTY)

    def fetch_stock_name(self, stock_code):
        self.calls.append(("name", (stock_code,), {}))
        return self._resolve("")

    def fetch_realtime_quote(self, stock_code):
        self.calls.append(("quote", (stock_code,), {}))
        return self._resolve(None)


def _service(results):
    """按 SOURCE_NAME 顺序装配五个桩；results 里没给的名字用「永远失败」"""
    order = ["AkShare", "BaoStock", "Tencent", "Sina", "Tdx"]
    sources = {}
    for name in order:
        # 默认失败 = 返回空表（贴合真实通道「失败即空表」的契约）
        result = results.get(name, FAIL)
        stub = FakeSource(result)
        stub.SOURCE_NAME = name
        sources[name] = stub

    instance = qs.StockQuoteService.__new__(qs.StockQuoteService)
    instance._akshare_source = sources["AkShare"]
    instance._baostock_source = sources["BaoStock"]
    instance._tencent_source = sources["Tencent"]
    instance._sina_source = sources["Sina"]
    instance._tdx_source = sources["Tdx"]
    instance.used_source_name = ""
    instance._sources = sources
    return instance


# ---------------------------------------------------------------------------
# 一、参数对象
# ---------------------------------------------------------------------------
def test_params_defaults():
    params = qs.StockDataFetchParams()
    assert params.stock_code == "000001.SZ"
    assert params.adjust_type == "qfq", "默认前复权"
    assert params.retry_count == 3
    assert params.retry_interval_seconds == 3


def test_params_setters():
    params = qs.StockDataFetchParams()
    params.set_stock_code("600519.SH")
    params.set_adjust_type("hfq")
    assert params.stock_code == "600519.SH"
    assert params.adjust_type == "hfq"


def test_params_accepts_empty_adjust_type():
    """通达信只支持不复权，复权方式必须能置空"""
    params = qs.StockDataFetchParams("600519.SH")
    params.set_adjust_type("")
    assert params.adjust_type == ""


# ---------------------------------------------------------------------------
# 二、价格链：五级降级
# ---------------------------------------------------------------------------
def test_price_chain_first_level_hit():
    """第一级命中就不该再问后面四级"""
    service = _service({"AkShare": PRICE_FRAME})
    result = service.fetch_monthly_close_prices(qs.StockDataFetchParams("600519.SH"))
    assert not result.empty
    assert service.used_source_name == "AkShare"
    for name in ("BaoStock", "Tencent", "Sina", "Tdx"):
        assert service._sources[name].calls == [], \
            "%s 不该被调用" % name


@pytest.mark.parametrize("hit", ["BaoStock", "Tencent", "Sina", "Tdx"])
def test_price_chain_falls_back_to_each_level(hit):
    """逐级验证：前 N 级失败时，第 N+1 级接管且生效源记对"""
    order = ["AkShare", "BaoStock", "Tencent", "Sina", "Tdx"]
    earlier = order[:order.index(hit)]
    # 只有 hit 这一级成功；earlier 全部失败（桩默认就是 RuntimeError）
    service = _service({hit: PRICE_FRAME})

    result = service.fetch_monthly_close_prices(qs.StockDataFetchParams("600519.SH"))
    assert not result.empty, "%s 应接管" % hit
    assert service.used_source_name == hit, "生效源应记为 %s，实际 %s" % (
        hit, service.used_source_name)
    # 排在 hit 之前的每一级都该被尝试过（证明确实是逐级降级而非跳级）
    for name in earlier:
        assert service._sources[name].calls, "%s 应被尝试过" % name
    # 排在其后的通道一个都不该被问
    for name in order[order.index(hit) + 1:]:
        assert service._sources[name].calls == [], "%s 不该被调用" % name


def test_price_chain_all_failed_returns_empty():
    """五级全挂才返回空表 —— 这是「降级」而非「一失败就放弃」的关键"""
    service = _service({})
    assert service.fetch_monthly_close_prices(
        qs.StockDataFetchParams("600519.SH")).empty
    for name in ("AkShare", "BaoStock", "Tencent", "Sina", "Tdx"):
        assert service._sources[name].calls, "%s 应被尝试过" % name


def test_price_chain_empty_result_counts_as_failure():
    """返回空表的通道等于失败，必须继续降级"""
    service = _service({"AkShare": pd.DataFrame(), "BaoStock": PRICE_FRAME})
    result = service.fetch_monthly_close_prices(qs.StockDataFetchParams("600519.SH"))
    assert not result.empty
    assert service.used_source_name == "BaoStock"


def test_price_chain_passes_retry_params_through():
    """重试参数要透传给通道，否则上层调优无效"""
    service = _service({"AkShare": PRICE_FRAME})
    params = qs.StockDataFetchParams("600519.SH")
    params.set_adjust_type("hfq")
    service.fetch_monthly_close_prices(params)
    _, positional, keywords = service._sources["AkShare"].calls[0]
    code, adjust_type = positional
    assert code == "600519.SH"
    assert adjust_type == "hfq"
    assert keywords["retry_count"] == params.retry_count
    assert keywords["retry_interval"] == params.retry_interval_seconds


# ---------------------------------------------------------------------------
# 三、估值链：AkShare -> BaoStock（两级）
# ---------------------------------------------------------------------------
def test_pe_chain_falls_back_to_baostock():
    service = _service({"AkShare": pd.DataFrame(), "BaoStock": PE_FRAME})
    frame = service._fetch_pe_ttm_with_fallback("600519.SH")
    assert not frame.empty
    assert service._sources["BaoStock"].calls, "AkShare 空时应降到 BaoStock"


def test_pe_chain_both_failed_returns_empty():
    service = _service({})
    assert service._fetch_pe_ttm_with_fallback("600519.SH").empty


# ---------------------------------------------------------------------------
# 四、简称链：腾讯 -> AkShare -> BaoStock
# ---------------------------------------------------------------------------
def test_name_chain_prefers_tencent():
    service = _service({"Tencent": "贵州茅台"})
    assert service.fetch_stock_name("600519.SH") == "贵州茅台"
    assert service._sources["AkShare"].calls == [], "腾讯命中后不应再问其他"


def test_name_chain_falls_back_through_akshare_then_baostock():
    service = _service({"BaoStock": "某某"})          # 腾讯、AkShare 都空
    assert service.fetch_stock_name("600519.SH") == "某某"


def test_name_chain_excludes_sina_and_tdx():
    """新浪/通达信不提供简称，且被刻意排除在主链之外（文档已写明）"""
    service = _service({})
    service.fetch_stock_name("600519.SH")
    assert service._sources["Sina"].calls == [], "新浪不应参与简称查询"
    assert service._sources["Tdx"].calls == [], "通达信不应参与简称查询"


def test_name_chain_all_failed_returns_empty_string():
    """三级都空时返回空串而非 None —— 调用方直接拿去显示，None 会变成 "None" 字样"""
    service = _service({})
    assert service.fetch_stock_name("600519.SH") == ""


def test_quote_service_propagates_channel_error():
    """【契约】服务层不 catch 通道抛出的异常

    降级判断只看「空表」，因为各通道自己保证「失败即空表」（见 FakeSource 上方说明）。
    若某个通道违反了这条契约直接抛出，服务层会把它透传给上层 —— 这是有意的：
    静默吞掉会让整条降级链停在那一步，上层拿到空表却以为「已尝试完所有通道」。
    这里把该行为钉住，防止有人误以为服务层有兜底。
    """
    service = _service({"AkShare": RuntimeError("通道违反契约")})
    with pytest.raises(RuntimeError, match="通道违反契约"):
        service.fetch_monthly_close_prices(qs.StockDataFetchParams("600519.SH"))


# ---------------------------------------------------------------------------
# 五、盘口链：腾讯 -> 新浪 -> 通达信
# ---------------------------------------------------------------------------
def test_realtime_chain_prefers_tencent():
    service = _service({"Tencent": object()})
    assert service.fetch_realtime_quote("600519.SH") is not None
    assert service._sources["Sina"].calls == []


def test_realtime_chain_falls_back_to_sina_then_tdx():
    service = _service({"Sina": object()})            # 腾讯空 -> 新浪接管
    assert service.fetch_realtime_quote("600519.SH") is not None
    assert service._sources["Tdx"].calls == [], "新浪命中后不应再问通达信"

    service = _service({"Tdx": object()})             # 腾讯、新浪都空 -> 通达信兜底
    assert service.fetch_realtime_quote("600519.SH") is not None


def test_realtime_chain_all_failed_returns_none():
    service = _service({})
    assert service.fetch_realtime_quote("600519.SH") is None


# ---------------------------------------------------------------------------
# 六、价格 + PE 对齐
# ---------------------------------------------------------------------------
def test_merge_price_and_pe_aligns_by_month_not_exact_date():
    """【关键口径】按「年-月」对齐，不是按精确交易日

    价格来自月线（交易所定义的月末交易日），PE 来自日线采样（同样是月末交易日），
    但两家数据源的节假日日历维护可能差 1~2 天。若按精确日期 join，那个月的 PE
    会静默变成 NaN。所以归一化成 pd.Period("M") 再左连接。

    这个用例把「同月不同日」写死成回归：哪天有人改回按日期 join，它会立刻红。
    """
    service = _service({})
    price = pd.DataFrame({
        "trade_date": pd.to_datetime(["2026-08-28", "2026-09-29"]),
        "close_price": [10.0, 11.0]})
    pe = pd.DataFrame({
        # 8 月的 PE 采样日是 31 号，和价格的 28 号不同 —— 仍应挂上
        "trade_date": pd.to_datetime(["2026-08-31", "2026-09-30"]),
        "pe_ttm": [20.0, 21.0]})
    merged = service._merge_price_and_pe(price, pe)
    assert len(merged) == 2, "以价格为基准左连接，不应增行"
    indexed = merged.set_index("trade_date")
    assert indexed.loc[pd.Timestamp("2026-08-28"), "pe_ttm"] == 20.0, \
        "同月不同日也必须挂上 PE"
    assert indexed.loc[pd.Timestamp("2026-09-29"), "pe_ttm"] == 21.0


def test_merge_price_and_pe_missing_month_gets_nan():
    """PE 缺某个月时补 NaN，而不是错挂到别的月份"""
    service = _service({})
    price = pd.DataFrame({
        "trade_date": pd.to_datetime(["2026-08-28", "2026-09-29"]),
        "close_price": [10.0, 11.0]})
    pe = pd.DataFrame({"trade_date": pd.to_datetime(["2026-08-31"]), "pe_ttm": [20.0]})
    merged = service._merge_price_and_pe(price, pe).set_index("trade_date")
    assert merged.loc[pd.Timestamp("2026-08-28"), "pe_ttm"] == 20.0
    assert pd.isna(merged.loc[pd.Timestamp("2026-09-29"), "pe_ttm"])


def test_merge_price_and_pe_drops_temp_key_column():
    """临时对齐列必须清理掉，否则会污染下游列契约"""
    service = _service({})
    merged = service._merge_price_and_pe(PRICE_FRAME, PE_FRAME)
    assert "_month_key" not in merged.columns


def test_merge_price_and_pe_with_empty_pe():
    service = _service({})
    price = PRICE_FRAME
    merged = service._merge_price_and_pe(price, pd.DataFrame())
    assert len(merged) == 1
    assert pd.isna(merged.iloc[0]["pe_ttm"])


def test_merge_price_and_pe_is_not_called_with_empty_price():
    """价格为空时不会走到合并 —— 由 fetch_monthly_price_and_pe 提前终止

    所以这里不测 _merge_price_and_pe 自身对空价格帧的表现（那属于不可达路径）：
    直接调用会因缺 trade_date 列抛 KeyError。
    """
    service = _service({})
    assert service.fetch_monthly_price_and_pe(
        qs.StockDataFetchParams("600519.SH")).empty
    pe_calls = [c for c in service._sources["AkShare"].calls if c[0] == "pe"]
    pe_calls += [c for c in service._sources["BaoStock"].calls if c[0] == "pe"]
    assert pe_calls == [], "价格取不到就不该再发起估值请求（省一次网络往返）"


def test_merge_price_and_pe_both_empty():
    service = _service({})
    assert service._merge_price_and_pe(pd.DataFrame(), pd.DataFrame()).empty


# ---------------------------------------------------------------------------
# 七、月线价格 + PE 组合接口
# ---------------------------------------------------------------------------
def test_monthly_price_and_pe_end_to_end(monkeypatch):
    """组合接口：价格命中 AkShare、PE 也命中 AkShare，最终两列齐备"""
    service = _service({"AkShare": PRICE_FRAME})
    monkeypatch.setattr(qs.pd, "concat", pd.concat)   # 保持真实行为
    service._fetch_pe_ttm_with_fallback = lambda code: PE_FRAME
    frame = service.fetch_monthly_price_and_pe(qs.StockDataFetchParams("600519.SH"))
    assert "close_price" in frame.columns
    assert "pe_ttm" in frame.columns


def test_monthly_price_and_pe_price_failed(monkeypatch):
    """价格链全失败时，组合接口也返回空表（不能只剩 PE 列）"""
    service = _service({})
    service._fetch_pe_ttm_with_fallback = lambda code: PE_FRAME
    assert service.fetch_monthly_price_and_pe(
        qs.StockDataFetchParams("600519.SH")).empty