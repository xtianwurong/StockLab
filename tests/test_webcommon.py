"""
横切公共层 (tests/test_webcommon.py)

覆盖 app/web/webcommon.py —— app.web 各接口模块共用的 JSON 安全转换与
参数校验。这些函数此前在 store / compare_api / screener_api / market_api /
insight_api 各有一份拷贝，拷贝之间口径并不一致，所以本文件分两部分：

  TestJsonSafe     —— NaN / ±inf 归一成 null。这不是洁癖：flask.jsonify
                      遇到 Infinity 会产出 `Infinity`，而它**不是合法 JSON**，
                      浏览器侧 JSON.parse 直接抛错、整页数据渲染失败。
  TestParamParsing —— 整数与日期参数的严格校验。这里钉住的核心性质是
                      **假阴性不得发生**：垃圾参数必须 400，而不是静默回退到
                      默认值后返回一批「看起来正常」的数据。

【为什么单列一个文件】
  webcommon 是 app.web 里唯一不碰数据库、不碰 stocklab.facade 的模块，
  纯函数可直接单测，不需要 Flask 应用上下文（parse_paging 除外）。

运行：
  ./venv/bin/python -m pytest tests/test_webcommon.py -v
"""

import math
import os
import sys

import pytest

if "stocklab" not in sys.modules:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.web.webcommon import (
    json_num,
    json_text,
    looks_like_date,
    parse_int_arg,
    parse_paging,
)


# ---------------------------------------------------------------------------
# JSON 安全转换
# ---------------------------------------------------------------------------

class TestJsonSafe:
    """NaN / ±inf 必须归一为 None，否则产出的 JSON 不合法"""

    @pytest.mark.parametrize("bad", [
        float("nan"), float("inf"), float("-inf"),
    ])
    def test_non_finite_becomes_none(self, bad):
        assert json_num(bad) is None
        # 关键：下游是 json.dumps，必须真的能出合法 JSON
        import json as _json
        assert _json.dumps(json_num(bad)) == "null"

    def test_none_and_unparsable_are_none(self):
        assert json_num(None) is None
        assert json_num("abc") is None
        assert json_num(object()) is None

    def test_valid_number_is_kept(self):
        assert json_num(1.5) == 1.5
        assert json_num("37.88999999999999", 4) == 37.89

    def test_digits_none_keeps_full_precision(self):
        """store 的口径：不传 digits 就是原样透出，不擅自截断"""
        assert json_num(1 / 3) == pytest.approx(0.3333333333333333)

    def test_digits_zero_is_allowed(self):
        assert json_num(12.7, 0) == 13.0

    def test_text_maps_none_and_nan_to_empty(self):
        assert json_text(None) == ""
        assert json_text(float("nan")) == ""

    def test_text_passes_strings_through(self):
        assert json_text("600519.SH") == "600519.SH"
        assert json_text("") == ""

    def test_text_stringifies_other_scalars(self):
        assert json_text(600519) == "600519"


# ---------------------------------------------------------------------------
# 参数校验
# ---------------------------------------------------------------------------

class TestParamParsing:
    """垃圾参数必须 400 —— 静默回退会产生假阴性"""

    def test_valid_value(self):
        assert parse_int_arg("limit", "20", 30) == (20, None)

    @pytest.mark.parametrize("raw", [None, "", "   "])
    def test_missing_falls_back_to_default(self, raw):
        """空串来自表单提交空字段，语义就是「没填」，回默认值是诚实的"""
        assert parse_int_arg("limit", raw, 30) == (30, None)

    @pytest.mark.parametrize("raw", ["abc", "1e2", "--5", "1.5", "5px", "+5"])
    def test_garbage_is_rejected(self, raw):
        """注意 `--5`：旧实现用 lstrip('-') 后 isdigit 会误判为合法整数，
        随后 int() 抛错被 except 吞掉、退回默认分页 —— 典型的假阴性。"""
        value, error = parse_int_arg("limit", raw, 30)
        assert value is None, "垃圾值 %r 不该被当成合法整数" % raw
        assert error and "整数" in error

    def test_upper_bound(self):
        value, error = parse_int_arg("limit", "999", 30, minimum=1, maximum=200)
        assert value is None and "1~200" in error

    def test_lower_bound(self):
        value, error = parse_int_arg("limit", "0", 30, minimum=1, maximum=200)
        assert value is None and error
        value, error = parse_int_arg("offset", "-1", 0, minimum=0)
        assert value is None and error

    def test_bounds_are_inclusive(self):
        assert parse_int_arg("limit", "1", 30, minimum=1, maximum=200)[0] == 1
        assert parse_int_arg("limit", "200", 30, minimum=1, maximum=200)[0] == 200

    def test_negative_integer_is_parsed_then_rejected_by_bound(self):
        """负号本身是合法整数语法，靠 minimum 挡，不是靠正则挡"""
        value, error = parse_int_arg("limit", "-5", 30, minimum=1)
        assert value is None and error


class TestParsePaging:
    """parse_paging 组合 parse_int_arg，需要 Flask 请求上下文"""

    def test_defaults(self, app):
        with app.test_request_context("/api/x"):
            assert parse_paging(20, 200) == ((0, 20), None)

    def test_explicit_values(self, app):
        with app.test_request_context("/api/x?offset=40&limit=60"):
            assert parse_paging(20, 200) == ((40, 60), None)

    @pytest.mark.parametrize("qs,expect_err", [
        ("limit=0", "limit"),
        ("limit=9999", "limit"),
        ("limit=abc", "limit"),
        ("offset=-1", "offset"),
        ("offset=x", "offset"),
    ])
    def test_bad_paging_is_rejected(self, app, qs, expect_err):
        with app.test_request_context("/api/x?" + qs):
            paging, error = parse_paging(20, 200)
            assert paging is None, qs
            assert expect_err in error

    def test_empty_value_means_absent(self, app):
        """`?limit=` 是表单空字段的常态，按缺省处理而不是 400"""
        with app.test_request_context("/api/x?limit=&offset="):
            assert parse_paging(20, 200) == ((0, 20), None)


class TestLooksLikeDate:
    """严格 YYYY-MM-DD：只看长度会放行 10 位垃圾，一路走到 SQL 才变成
    「本地无数据」—— 调用方拿到的是业务上正确的空结果，而非参数错误。"""

    @pytest.mark.parametrize("text", [
        "2026-01-31", "2020-12-01", "1999-01-01",
    ])
    def test_accepts_real_dates(self, text):
        assert looks_like_date(text) is True

    @pytest.mark.parametrize("text", [
        "",               # 空
        "20260101",       # 无分隔符
        "2026-01",        # 只到月
        "2026-01-31T00:00:00",  # 太长
        "abcdefghij",     # 长度恰好 10 —— 旧的 len>10 校验会放行它
        "2026-13-01",     # 月份不存在
        "2026-02-30",     # 日期不存在
        "2026-1-1",       # 未补零
    ])
    def test_rejects_bad_dates(self, text):
        assert looks_like_date(text) is False

    def test_rejects_non_string(self):
        assert looks_like_date(None) is False
        assert looks_like_date(20260101) is False
