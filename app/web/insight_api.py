#!/usr/bin/env python3
"""
==============================================================================
StockLab - 投资人观点接口处理模块 (app.web.insight_api)
==============================================================================

【模块职责】
  投资人观点页面的三个接口：
    - handle_insight_meta()     筛选面板元数据（投资人 / 平台 / 类型 / 主题 /
                                 核验状态分布 / 数据源就绪状态）
    - handle_insight_quotes()   多维筛选言论列表
    - handle_insight_investor() 单个投资人的档案（生平 + 账号 + 观点时间线）

【一个不可让渡的页面契约：核验状态必须显示】
  接口在每条言论上都返回 verification，且 meta 接口单独返回全库状态分布。
  前端**禁止**把 unverified 的条目显示成「XX 说」。理由不是保守，而是：
  网上流传的「名人语录」有大量伪造与张冠李戴，一个不带状态标记的列表，
  在三个月后你会分不清哪句是原话、哪句是网友转述。
  这条约束在 tests/test_insight.py 里有对应的断言钉住。

【不做什么】
  - 不做「投资理念总结」的自动生成：本域只负责**存取与呈现可溯源原文**，
    自动总结会把 unverified 的转述再加工一层，让来源更不可考。
    理念的归纳应当由人读完原文后做，且必须连着原文一起呈现。
  - 不做情感打分 / 涨跌预测：言论不是数据，不该被当成信号回灌到估值模型里。

【取数路径】
  经 stocklab.facade.InsightDataFacade 取数，连接仍统一向
  store.facade_database() 借出门面那一个 —— 不能自己 duckdb.connect()，
  同文件第二个写连接会抛 "Could not set lock"。
  **本模块不 import stocklab.persistence**：取数编排属于 stocklab 内部，
  接口层只做参数校验、展示文案与 JSON 组装（依赖方向见 AGENT.md）。

【应答约定】
  与 app.web.api 一致：200 + 业务 JSON / 400 参数非法 / 500 通用文案。
==============================================================================
"""

import logging

import pandas as pd
from flask import jsonify, render_template, request

from app.web import store
from app.web.webcommon import looks_like_date, parse_int_arg, parse_paging
from stocklab.domain import (
    INVESTOR_STYLES,
    PLATFORMS,
    QUOTE_TYPES,
    VERIFICATION_STATUSES,
    decode_raw_meta,
)
from stocklab.facade import InsightDataFacade

_logger = logging.getLogger("StockLab.Web.InsightApi")

__all__ = [
    "handle_insight_page",
    "handle_insight_meta",
    "handle_insight_quotes",
    "handle_insight_investor",
]

# 单页最多返回条数上限（防 ?limit=999999 把整库拖到浏览器）
_MAX_LIMIT = 200

# 关键词最短长度：比这更短的单字会命中大量噪声且无检索价值
_MIN_KEYWORD_LENGTH = 2


def _reject_unencoded_query():
    """
    拒绝未做 URL 百分号编码的查询串

    【为什么需要它】
       WSGI 规定 QUERY_STRING 是 latin-1 字节流。客户端直接把中文字节塞进
       URL（例如 `curl '/api/insight/quotes?keyword=估'` 而不加
       --data-urlencode，或前端漏写 encodeURIComponent）时，Werkzeug 会把它
       按 latin-1 解成 'ä¼°' —— 长度 2，**绕过了单字关键词校验**，
       随后拿这串乱码去 LIKE 匹配，必然 0 条。

       于是用户看到的是「没有相关语录」，而真相是「他的关键词根本没送达」。
       这是本项目一贯警惕的假阴性：错误被包装成了数据。

    Returns:
        Response | None: 参数未编码时返回 400，正常时返回 None
    """
    raw = request.query_string or b""
    if any(byte >= 0x80 for byte in raw):
        return jsonify({
            "error": "查询参数含未编码的非 ASCII 字符，"
                     "请使用 URL 百分号编码（如 encodeURIComponent / "
                     "curl --data-urlencode）"
        }), 400
    return None

# 类型 / 平台 / 状态的展示文案（前端按 key 取，这里给一份权威映射）
_TYPE_LABELS = {
    "philosophy": "投资理念",
    "macro_view": "宏观判断",
    "selection": "选股标准",
    "position": "持仓看法",
    "caution": "风险提示",
    "discipline": "纪律心态",
}

_PLATFORM_LABELS = {
    "xueqiu": "雪球",
    "guba": "东方财富股吧",
    "eastmoney": "东方财富",
    "weibo": "微博",
    "manual": "人工录入",
}

# 核验状态的展示文案 —— 「已核实」与「未核实」必须视觉上不可混淆
_VERIFICATION_LABELS = {
    "unverified": "未核实",
    "verified": "已核实原文",
    "disputed": "版本存疑",
    "fabricated": "判定伪造",
}

_STYLE_LABELS = {
    "value": "价值投资",
    "growth": "成长投资",
    "value_growth": "价值成长",
    "macro": "宏观择时",
    "turnaround": "困境反转",
}

_VERIFICATION_HINT = (
    "本页内容多数来自公开平台的抓取，未经逐条核对原始出处。"
    "标记为「未核实」的条目仅供线索参考，不应直接作为投资依据。"
)


# ----------------------------------------------------------------------------
# 页面
# ----------------------------------------------------------------------------
def handle_insight_page():
    """
    投资人观点页面

    Returns:
        str: HTML 页面
    """
    return render_template("insight.html")


# ----------------------------------------------------------------------------
# 接口
# ----------------------------------------------------------------------------
def handle_insight_meta():
    """
    筛选面板元数据 + 核验状态分布 + 数据源就绪状态

    Returns:
        Response: JSON
    """
    try:
        database = store.facade_database()
    except Exception as error:                    # noqa: BLE001
        _logger.error("取数据库句柄失败: %s", str(error)[:200])
        return jsonify({"error": "数据库不可用，请检查服务日志"}), 500

    try:
        facade = InsightDataFacade(database)
        meta = facade.metadata(theme_limit=40)

        return jsonify({
            "investors": _investor_rows(meta["investors"]),
            "platforms": _label_list(PLATFORMS, _PLATFORM_LABELS),
            "quote_types": _label_list(QUOTE_TYPES, _TYPE_LABELS),
            "styles": _label_list(INVESTOR_STYLES, _STYLE_LABELS),
            "verifications": _label_list(
                VERIFICATION_STATUSES, _VERIFICATION_LABELS),
            "verification_summary": meta["summary"],
            "verification_hint": _VERIFICATION_HINT,
            "themes": [] if meta["themes"] is None else [
                {"theme": row["theme"], "count": _as_int(row["quote_count"])}
                for row in meta["themes"].to_dict("records")
            ],
            "account_count": meta["counts"]["account"],
            "investor_count": meta["counts"]["investor"],
            "quote_count": meta["counts"]["quote"],
        })
    except Exception as error:                    # noqa: BLE001
        _logger.error("投资人观点元数据查询失败: %s", str(error)[:300])
        return jsonify({"error": "投资人观点数据不可用"}), 500


def handle_insight_quotes():
    """
    多维筛选言论列表

    查询参数：
      keyword        正文关键词（>= 2 字）
      investor_codes 逗号分隔的投资人代码
      platforms      逗号分隔的平台
      quote_types    逗号分隔的类型
      themes         逗号分隔的主题（命中任一即可）
      verification   单一核验状态
      stock_codes    逗号分隔的标的代码
      since          YYYY-MM-DD，只看 published_at 晚于该日
      order_by       captured_at / published_at / investor_code
      limit / offset 分页

    Returns:
        Response: JSON（含 items / total / limit / offset）
    """
    reject = _reject_unencoded_query()
    if reject is not None:
        return reject

    keyword = (request.args.get("keyword") or "").strip()
    if keyword and len(keyword) < _MIN_KEYWORD_LENGTH:
        return jsonify({
            "error": "关键词至少 %d 个字（单字检索噪声太大）"
                     % _MIN_KEYWORD_LENGTH
        }), 400

    verification = (request.args.get("verification") or "").strip()
    if verification and verification not in VERIFICATION_STATUSES:
        return jsonify({
            "error": "verification 非法取值，应为: %s"
                     % ", ".join(VERIFICATION_STATUSES)
        }), 400

    order_by = (request.args.get("order_by") or "captured_at").strip()
    if order_by not in ("captured_at", "published_at", "investor_code"):
        return jsonify({
            "error": "order_by 非法取值，应为: captured_at / published_at / "
                     "investor_code"
        }), 400

    since = (request.args.get("since") or "").strip() or None
    if since and not looks_like_date(since):
        return jsonify({"error": "since 应为 YYYY-MM-DD 格式"}), 400

    paging, error = parse_paging(30, _MAX_LIMIT)
    if error:
        return jsonify({"error": error}), 400
    offset, limit = paging

    try:
        database = store.facade_database()
        facade = InsightDataFacade(database)
        frame = facade.search_quotes(
            keyword=keyword or None,
            investor_codes=_csv(request.args.get("investor_codes")),
            platforms=_csv(request.args.get("platforms")),
            quote_types=_csv(request.args.get("quote_types")),
            themes=_csv(request.args.get("themes")),
            verification=verification or None,
            stock_codes=_csv(request.args.get("stock_codes")),
            since=since,
            order_by=order_by,
            limit=limit,
            offset=offset,
        )
        return jsonify({
            "items": [] if frame is None else _quote_rows(frame),
            "limit": limit,
            "offset": offset,
            "returned": 0 if frame is None else len(frame),
            "verification_hint": _VERIFICATION_HINT,
        })
    except Exception as error:                    # noqa: BLE001
        _logger.error("投资人言论查询失败: %s", str(error)[:300])
        return jsonify({"error": "投资人言论数据不可用"}), 500


def handle_insight_investor():
    """
    单个投资人档案：生平 + 平台账号 + 观点时间线

    查询参数：
      investor_code  必填
      limit          时间线条数（默认 20）

    Returns:
        Response: JSON
    """
    reject = _reject_unencoded_query()
    if reject is not None:
        return reject

    code = (request.args.get("investor_code") or "").strip()
    if not code:
        return jsonify({"error": "缺少 investor_code"}), 400

    limit, error = parse_int_arg(
        "limit", request.args.get("limit"), 20,
        minimum=1, maximum=_MAX_LIMIT)
    if error:
        return jsonify({"error": error}), 400

    try:
        database = store.facade_database()
        facade = InsightDataFacade(database)

        profile = facade.profile(code, limit=limit)
        if profile is None:
            return jsonify({"error": "投资人 %s 不存在" % code}), 404

        return jsonify({
            "investor": _investor_row(profile["investor"].iloc[0].to_dict()),
            "accounts": _account_rows(profile["accounts"]),
            "quotes": _quote_rows(profile["quotes"]),
            "verification_summary": profile["summary"],
            "verification_hint": _VERIFICATION_HINT,
        })
    except Exception as error:                    # noqa: BLE001
        _logger.error("投资人档案查询失败 %s: %s", code, str(error)[:300])
        return jsonify({"error": "投资人档案不可用"}), 500


# ----------------------------------------------------------------------------
# 行转换
# ----------------------------------------------------------------------------
def _quote_rows(frame):
    """言论帧 -> JSON 行列表"""
    if frame is None or frame.empty:
        return []
    rows = []
    for record in frame.to_dict("records"):
        rows.append({
            "quote_id": record.get("quote_id"),
            "investor_code": record.get("investor_code"),
            "platform": record.get("platform"),
            "platform_label": _PLATFORM_LABELS.get(
                record.get("platform"), record.get("platform") or "未知"),
            "account_name": record.get("account_name"),
            "source_url": record.get("source_url"),
            # 溯源链接为空时返回显式标记，而不是空串 ——
            # 前端要能把「无溯源」显示成警告样式，空串做不到
            "has_source": bool(record.get("source_url")),
            "published_at": _as_text(record.get("published_at")),
            "captured_at": _as_text(record.get("captured_at")),
            "content": record.get("content") or "",
            "summary": record.get("summary") or "",
            "quote_type": record.get("quote_type"),
            "quote_type_label": _TYPE_LABELS.get(record.get("quote_type")),
            "theme": record.get("theme"),
            "themes": _csv(record.get("theme")),
            "stock_codes": _csv(record.get("stock_codes")),
            "verification": record.get("verification") or "unverified",
            "verification_label": _VERIFICATION_LABELS.get(
                record.get("verification") or "unverified", "未知状态"),
            "raw_meta": decode_raw_meta(record.get("raw_meta")),
        })
    return rows


def _investor_rows(frame):
    """投资人帧（含统计列）-> JSON 行列表"""
    if frame is None or frame.empty:
        return []
    rows = []
    for record in frame.to_dict("records"):
        row = _investor_row(record)
        row["quote_count"] = _as_int(record.get("quote_count"))
        row["verified_count"] = _as_int(record.get("verified_count"))
        row["last_captured"] = _as_text(record.get("last_captured"))
        rows.append(row)
    return rows


def _investor_row(record):
    """投资人单行 -> JSON"""
    aliases = _csv(record.get("aliases"))
    return {
        "investor_code": record.get("investor_code"),
        "name": record.get("name"),
        "aliases": aliases,
        "alias_label": "、".join(aliases),
        "role": record.get("role"),
        "organization": record.get("organization"),
        "style_tags": _csv(record.get("style_tags")),
        "style_label": "、".join(
            _STYLE_LABELS.get(tag, tag) for tag in _csv(record.get("style_tags"))
        ),
        "profile_url": record.get("profile_url"),
        "is_active": bool(record.get("is_active", True)),
    }


def _account_rows(frame):
    """平台账号帧 -> JSON 行列表"""
    if frame is None or frame.empty:
        return []
    return [
        {
            "account_id": record.get("account_id"),
            "platform": record.get("platform"),
            "platform_label": _PLATFORM_LABELS.get(
                record.get("platform"), record.get("platform") or "未知"),
            "account_name": record.get("account_name"),
            "account_uid": record.get("account_uid"),
            "home_url": record.get("home_url"),
            "is_enabled": bool(record.get("is_enabled", True)),
            "note": record.get("note"),
        }
        for record in frame.to_dict("records")
    ]


def _label_list(keys, labels):
    """
    把枚举值转成 [{value, label}] 供前端渲染

    Args:
        keys (tuple): 枚举值
        labels (dict): 展示文案

    Returns:
        list[dict]
    """
    return [
        {"value": key, "label": labels.get(key, key)} for key in keys
    ]


# ----------------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------------
def _csv(text):
    """逗号分隔串 -> 去空列表（支持 list 直传）"""
    if text is None:
        return []
    if isinstance(text, (list, tuple, set)):
        items = [str(item).strip() for item in text]
    else:
        items = [part.strip() for part in str(text).split(",")]
    return [item for item in items if item]


def _as_int(value, default=0):
    """宽松转 int；失败用默认值（不抛异常，参数脏不该让整个接口 500）"""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_text(value):
    """时间戳 -> YYYY-MM-DD HH:MM:SS 文本；空值返回 None"""
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    try:
        parsed = pd.Timestamp(value)
    except (ValueError, TypeError):
        return str(value)
    if pd.isna(parsed):
        return None
    return parsed.strftime("%Y-%m-%d %H:%M:%S")
