#!/usr/bin/env python3
"""
==============================================================================
StockLab - 投资人观点归一化器 (stocklab.normalization.insight)
==============================================================================

【模块职责】
   把各平台采集器产出的**源记录**（dict，字段名随平台而变）
   转换为领域契约 DataFrame（stocklab.domain.insight 的 *_COLUMNS）。
   本模块只依赖标准库与 stocklab.domain，不联网、不碰数据库。

【这一层存在的意义：把「不可信输入」关在门外】
   UGC 文本是本项目遇到的最脏输入 —— 一段话可能是空的、可能是一整页广告、
   可能带 HTML 残留、可能只有 30 个字也可能 8000 个字。这些都不是异常，
   是常态。所以归一化器必须**主动做减法**而不是等下游报错：

     - 空白 / 纯符号正文直接丢弃（不入库）：抓取页经常带出 "转发微博""赞"
       这类空壳条目，存下来只会在页面上显示成一行空白。
     - 摘要按**字符数**截断而不是按字节，中文按字节截会切出半个字。
     - verification 一律由本层写死为 'unverified'，**不接受调用方传值**。
       见 domain/insight.py：把「已核实」写成代码逻辑就是伪自动化。

【去重的口径在这里定死】
   content_hash 由「归一化后的正文」算出，而不是原始 HTML：
     - 去掉空白、全角转半角、去掉常见的转发/表情标记后再算哈希，
       这样同一段话因平台排版差异（多余空格、全半角）不会被算成两条；
     - 但**保留**标点与文字 —— 不能归一化到「都变成一样」，
       那会让不同的观点撞成一条。
   跨平台去重由调用方用 InvestorQuoteRepository.existing_hashes() 执行；
   本层只保证「同一条内容在不同平台上算出同一个哈希」。
"""

import hashlib
import html
import re
import unicodedata

import pandas as pd

from stocklab.domain import (
    INVESTOR_ACCOUNT_COLUMNS,
    INVESTOR_COLUMNS,
    INVESTOR_QUOTE_COLUMNS,
    align_columns,
)

__all__ = [
    "normalize_investors",
    "normalize_investor_accounts",
    "normalize_investor_quotes",
    "compute_content_hash",
    "clean_ugc_text",
    "make_summary",
    "detect_quote_type",
    "detect_stock_codes",
    "detect_themes",
    "build_quote_id",
    "UNVERIFIED",
    "MANUAL_PLATFORM",
]

# 核验状态常量的唯一来源：采集侧能写的值只有这一个
UNVERIFIED = "unverified"

# 人工录入平台标识 —— 唯一允许覆盖 verification 的通道
MANUAL_PLATFORM = "manual"

# 正文最短长度：低于此值视为空壳条目（"转发微博" / "赞" / 纯表情）
MIN_CONTENT_LENGTH = 4

# 摘要长度上限（字符数，非字节）
SUMMARY_LENGTH = 120

# 转发/分享类噪声尾巴，抓到就剥掉 —— 同一段话在不同平台的尾巴不一样，
# 不剥掉会导致同内容跨平台算出不同哈希、去重失效。
_NOISE_TAIL = re.compile(
    r"(转发微博|转发帖子|分享给.{0,12}?(朋友|好友|群)|点赞\s*\d*|评论\s*\d+|"
    r"来自.{0,20}?客户端|阅读全文|查看更多|网页链接|点击查看|"
    r"[（(]?来自.{0,8}?[)）]?$)"
)
_MULTI_BLANK = re.compile(r"[\s　\xa0]+")
_INLINE_TAG = re.compile(r"<[^>]+>")

# 行块分隔标签：这些标签**代表一个分隔**，替换成空格才对。
# 细节标签（<b>/<i>/<a>/<font>）则直接删掉 —— 统一替换成空格会把
# 「平安银行</b>&amp;茅台」洗成「平安银行 &茅台」，
# 多出的符号既改变检索口径，也让同一段话跨平台算出不同哈希（去重失效）。
_BLOCK_TAG = re.compile(
    r"</?(?:p|div|li|br|hr|tr|h[1-6])[^>]*>", re.IGNORECASE
)
# 全角字母数字 -> 半角。**只处理字母数字，不碰标点。**
#
# 【踩坑记录：这里曾经把中文标点也一起转了】
#   初版把 `（），。` 等一并映射成半角，结果语录正文里的句号被改写成 "." ——
#   这是**篡改引文**。中文引文的标点本身就是内容的一部分，尤其在把多位
#   投资人的原话并排对比时，标点变了就可能被误引。
#   跨平台去重只需要「字母数字口径一致」（全角Ａ与半角A 指同一个东西），
#   标点各平台本来就照抄原文，不需要也不应该归一化。
#   所以不用 NFKC() 整体转换（它会把中文标点一起吃掉），只手工列举全角字母数字。
_FULLWIDTH = str.maketrans(
    "０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ"
    "ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ",
    "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz",
)

# A 股代码抽取：把正文里提到的标的抓出来，供「这只股票被谁提过」这类检索。
#
# 【踩坑记录：这里不能用 \w 做前后哨兵】
#   Python 默认 Unicode 语义下 \w 匹配汉字，于是正文「买了600519」里，
#   600519 前面的「了」被当成单词字符，(?<!\w) 断言失败 -> 一个代码都抽不到。
#   改为显式写 [0-9A-Za-z.]，只挡真正的数字/字母/小数点，不挡中文。
_STOCK_CODE = re.compile(
    r"(?<![0-9A-Za-z.])(?:sh|sz|bj)?([0368][0-9]{5}|00[0-9]{4}|30[0-9]{4})"
    r"(?:\.(?:sh|sz|bj))?(?![0-9A-Za-z.])",
    re.IGNORECASE,
)


# ----------------------------------------------------------------------------
# 文本清洗与哈希
# ----------------------------------------------------------------------------
def clean_ugc_text(text, max_length=None):
    """
    清洗 UGC 正文：去标签、解实体、转半角、压空白、剥转发噪声尾巴

    Args:
        text (str): 原始正文
        max_length (int, optional): 超长截断的字符数上限

    Returns:
        str: 清洗后的正文；无有效内容时返回空字符串
    """
    if text is None:
        return ""
    value = str(text)
    if not value.strip():
        return ""

    value = _BLOCK_TAG.sub(" ", value)
    value = _INLINE_TAG.sub("", value)
    value = html.unescape(value)
    # 清掉零宽字符 / BOM：平台正文里混着 U+FEFF 与 U+200B，
    # 不清会让「看起来一样」的两段话算出不同哈希
    value = value.replace("\ufeff", "").replace("\u200b", "")
    value = value.translate(_FULLWIDTH)
    value = _MULTI_BLANK.sub(" ", value).strip()

    # 剥噪声尾巴：可能剥多次（"转发微博 来自XX客户端"）
    previous = None
    while previous != value:
        previous = value
        value = _NOISE_TAIL.sub("", value).strip()
    value = _MULTI_BLANK.sub(" ", value).strip(" -|｜·")

    if max_length and len(value) > int(max_length):
        value = value[: int(max_length)].rstrip()
    return value


def compute_content_hash(text):
    """
    计算正文哈希（跨平台去重的依据）

    Args:
        text (str): 正文

    Returns:
        str: 64 位十六进制哈希；正文为空时返回空字符串
    """
    cleaned = clean_ugc_text(text)
    if not cleaned:
        return ""
    # 归一化到「去掉所有空白 + 转小写」：这样排版差异（全角/半角、换行、
    # 多余空格）不会让同一段话算成不同哈希；但文字本身一字不动，
    # 不同观点仍然撞不到一起。
    canonical = _MULTI_BLANK.sub("", cleaned).lower()
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def make_summary(text, length=SUMMARY_LENGTH):
    """
    生成正文摘要（按字符截断，中文不会被切半个字）

    Args:
        text (str): 正文
        length (int): 摘要长度上限

    Returns:
        str: 摘要；正文短于上限时返回全文
    """
    cleaned = clean_ugc_text(text)
    if len(cleaned) <= int(length):
        return cleaned
    return cleaned[: int(length)].rstrip() + "…"


# ----------------------------------------------------------------------------
# 启发式标注（明确是启发式，不是分类器）
# ----------------------------------------------------------------------------
def detect_quote_type(text):
    """
    依据关键词猜测言论类型

    【可信度说明】
       这是关键词启发式，只用于页面的「粗分类 + 便于筛选」，**不是**语义分类。
       语录类型错了不产生错误结论（页面上会同时显示原文），但会让按类型的
       筛选漏掉一些条目。因此调用方可以传 None 关掉它，让库里存 NULL ——
       宁可缺失，不要看起来很确定的错分类。

    Args:
        text (str): 正文

    Returns:
        str | None: QUOTE_TYPES 之一；无明显特征返回 None
    """
    cleaned = clean_ugc_text(text)
    if not cleaned:
        return None
    rules = (
        ("macro_view", ("宏观", "经济", "利率", "降息", "加息", "通胀", "cpi",
                        "gdp", "地产", "汇率", "美联储", "周期", "大势")),
        ("selection", ("选股", "标准是", "买入条件", "商业模式", "护城河",
                       "好公司", "长期持有", "值得买", "买点", "筛选")),
        ("position", ("持仓", "重仓", "加仓", "减仓", "清仓", "买入", "卖出",
                      "我的仓位", "成本")),
        ("caution", ("风险", "小心", "警惕", "不要买", "别买", "退市", "泡沫",
                     "谨慎", "avoid")),
        ("discipline", ("耐心", "心态", "纪律", "不追高", "看不懂", "放弃",
                        "等待", "耐心持有", "克制")),
    )
    for quote_type, keywords in rules:
        if any(word in cleaned.lower() for word in keywords):
            return quote_type
    return "philosophy" if len(cleaned) >= MIN_CONTENT_LENGTH else None


def detect_themes(text, limit=3):
    """
    依据关键词猜测主题标签（可多命中）

    Args:
        text (str): 正文
        limit (int): 最多返回几个主题

    Returns:
        str: 逗号分隔的主题串；无命中返回空字符串
    """
    cleaned = clean_ugc_text(text).lower()
    if not cleaned:
        return ""
    themes = []
    table = (
        ("估值", ("估值", "pe", "市盈率", "市净率", "pb", "便宜", "贵", "价格")),
        ("商业模式", ("商业模式", "生意", "赚钱能力", "毛利", "现金流")),
        ("企业文化", ("企业文化", "管理层", "诚信", "mot", "董事长")),
        ("能力圈", ("能力圈", "看不懂", "不懂", "熟悉的")),
        ("长期持有", ("长期", "长期持有", "不动", "一直持有", "永久")),
        ("宏观判断", ("宏观", "经济", "利率", "通胀", "汇率")),
        ("科技成长", ("科技", "芯片", "ai", "互联网", "新能源", "半导体")),
        ("消费", ("消费", "白酒", "品牌", "零售")),
        ("止损纪律", ("止损", "纪律", "认错", "退出")),
    )
    for theme, keywords in table:
        if any(word in cleaned for word in keywords):
            themes.append(theme)
        if len(themes) >= int(limit):
            break
    return ",".join(themes)


def detect_stock_codes(text):
    """
    从正文抽取提到的 A 股代码

    Args:
        text (str): 正文

    Returns:
        str: 逗号分隔的代码串（已去重、保持出现顺序）；无命中返回空字符串
    """
    cleaned = clean_ugc_text(text)
    if not cleaned:
        return ""
    found = []
    for match in _STOCK_CODE.finditer(cleaned):
        code = match.group(1)
        if code not in found:
            found.append(code)
    return ",".join(found[:10])


def build_quote_id(platform, platform_id, content_hash):
    """
    生成 quote_id（主键）

    【优先级】
       1. 平台有稳定 id -> "platform:id"。可重跑、可增量对齐。
       2. 平台没有 id -> "platform:hash16"。用内容哈希兜底，
          保证同一段话重复抓到不会重复入库。

    Args:
        platform (str): 平台标识
        platform_id (str): 平台侧 id，可为空
        content_hash (str): 正文哈希（平台无 id 时必填）

    Returns:
        str: quote_id；两者都缺时返回空字符串
    """
    platform = str(platform or "unknown")
    if platform_id:
        return "%s:%s" % (platform, str(platform_id))
    if content_hash:
        return "%s:h%s" % (platform, content_hash[:16])
    return ""


# ----------------------------------------------------------------------------
# 契约帧构造
# ----------------------------------------------------------------------------
def normalize_investors(raw, created_time=None):
    """
    归一化投资人主表

    Args:
        raw (list): 投资人 dict，键：investor_code/name/aliases/...
        created_time (pd.Timestamp, optional): 记录时间（NOT NULL 列）；
            缺省用当前时间。传入固定值可让测试结果确定。

    Returns:
        pd.DataFrame: INVESTOR_COLUMNS 契约列序；输入为空返回空帧
    """
    import pandas as _pd

    default_created = created_time or _pd.Timestamp.now("UTC")
    records = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("investor_code") or "").strip()
        name = clean_ugc_text(item.get("name"))
        if not code or not name:
            # 投资人没有 code 或姓名就无法与账号/言论关联，静默丢弃
            continue
        style_tags = item.get("style_tags")
        if isinstance(style_tags, (list, tuple, set)):
            style_tags = ",".join(str(tag).strip() for tag in style_tags if tag)
        records.append({
            "investor_code": code,
            "name": name,
            "aliases": _join_texts(item.get("aliases")),
            "role": clean_ugc_text(item.get("role")) or None,
            "organization": clean_ugc_text(item.get("organization")) or None,
            "style_tags": style_tags or None,
            "profile_url": _clean_url(item.get("profile_url")),
            "is_active": _to_bool(item.get("is_active"), default=True),
            # created_time 是「我们何时记录了这条事实」，不是「这个人何时出道」。
            # 登记表里手写这个字段几乎一定是错的，所以只有显式传入才采信。
            "created_time": item.get("created_time") or default_created,
        })
    return _to_contract_frame(records, INVESTOR_COLUMNS)


def normalize_investor_accounts(raw, default_platform=None, created_time=None):
    """
    归一化平台账号映射

    Args:
        raw (list): 账号 dict，键：investor_code/platform/account_name/...
        default_platform (str, optional): 记录里没写 platform 时的兜底
        created_time (pd.Timestamp, optional): 记录时间；缺省用当前时间

    Returns:
        pd.DataFrame: INVESTOR_ACCOUNT_COLUMNS 契约列序
    """
    import pandas as _pd

    default_created = created_time or _pd.Timestamp.now("UTC")
    records = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("investor_code") or "").strip()
        name = clean_ugc_text(item.get("account_name"))
        platform = str(item.get("platform") or default_platform or "").strip()
        if not code or not name or not platform:
            continue
        account_id = str(
            item.get("account_id")
            or "%s:%s" % (platform, item.get("account_uid") or name)
        ).strip()
        records.append({
            "account_id": account_id,
            "investor_code": code,
            "platform": platform,
            "account_name": name,
            "account_uid": (str(item["account_uid"]).strip()
                            if item.get("account_uid") else None),
            "home_url": _clean_url(item.get("home_url")),
            "is_enabled": _to_bool(item.get("is_enabled"), default=True),
            "note": clean_ugc_text(item.get("note")) or None,
            "created_time": item.get("created_time") or default_created,
        })
    return _to_contract_frame(records, INVESTOR_ACCOUNT_COLUMNS)


def normalize_investor_quotes(raw, investor_code=None, platform=None,
                              captured_at=None, auto_annotate=True):
    """
    归一化理念与言论（采集侧的唯一入口）

    【verification 的唯一例外：manual 平台】
       抓取侧一律写死 'unverified' —— 核验是人的判断，代码不能代劳。
       唯一例外是 platform == 'manual'（人工录入，来源由人给全）：
       那条通道背后的判断是「一个人对照原始出处确认过了」，
       所以允许调用方传 verified。
       其他平台即便传了 verified 也会被**强制降级**成 unverified ——
       这是有意的：万一某个采集器误传了 verified，页面上把一句抓来的话
       标成「已核实」，比标成「未核实」危险得多。

    Args:
        raw (list): 言论 dict，需含 content（或 text/body）
        investor_code (str, optional): 记录未指定时的兜底投资人
        platform (str, optional): 记录未指定时的兜底平台
        captured_at (datetime, optional): 抓取时间；缺失会用当前时间
        auto_annotate (bool): 是否用启发式填 quote_type / theme / stock_codes。
            传 False 则全部留 NULL（宁可缺失，不要看起来确定的错分类）

    Returns:
        pd.DataFrame: INVESTOR_QUOTE_COLUMNS 契约列序
    """
    import datetime as _dt

    default_captured = captured_at or _dt.datetime.now()
    records = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        content = clean_ugc_text(
            item.get("content") or item.get("text") or item.get("body")
        )
        if len(content) < MIN_CONTENT_LENGTH:
            # 空壳条目（"转发微博"/"赞"）：不入库，否则页面上是一行空白
            continue

        content_hash = str(item.get("content_hash") or "").strip()
        if not content_hash:
            content_hash = compute_content_hash(content)
        if not content_hash:
            continue

        row_platform = str(item.get("platform") or platform or "").strip()
        if not row_platform:
            # 没有平台就无法回答「这段话出自哪里」，而这是本域的基本要求
            continue
        row_investor = str(
            item.get("investor_code") or investor_code or ""
        ).strip()
        if not row_investor:
            # 没有投资人的言论无法归到任何人名下，丢弃而不是留个空壳
            continue

        quote_id = str(item.get("quote_id") or "").strip() or build_quote_id(
            row_platform, item.get("platform_id"), content_hash
        )
        if not quote_id:
            continue

        records.append({
            "quote_id": quote_id,
            "investor_code": row_investor,
            "platform": row_platform,
            "account_name": clean_ugc_text(item.get("account_name")) or None,
            "source_url": _clean_url(item.get("source_url")),
            "published_at": item.get("published_at"),
            "captured_at": item.get("captured_at") or default_captured,
            "content_hash": content_hash,
            "content": content,
            "summary": clean_ugc_text(item.get("summary")) or make_summary(content),
            "quote_type": (
                (item.get("quote_type")
                 or (detect_quote_type(content) if auto_annotate else None))
            ),
            "theme": (
                item.get("theme")
                or (detect_themes(content) if auto_annotate else "")
            ) or None,
            "stock_codes": (
                item.get("stock_codes")
                or (detect_stock_codes(content) if auto_annotate else "")
            ) or None,
            # 抓取侧写死 unverified；只有人工录入能覆盖（见函数说明）
            "verification": _resolve_verification(
                item.get("verification"), row_platform
            ),
            "language": str(item.get("language") or "zh").strip() or "zh",
            "raw_meta": item.get("raw_meta"),
        })
    return _to_contract_frame(records, INVESTOR_QUOTE_COLUMNS)


# ----------------------------------------------------------------------------
# 内部工具
# ----------------------------------------------------------------------------
def _to_contract_frame(records, columns):
    """把记录列表转成契约列序的 DataFrame（空列表返回空帧）"""
    frame = pd.DataFrame(records) if records else pd.DataFrame()
    if frame.empty:
        return pd.DataFrame(columns=list(columns))
    return align_columns(frame, columns, "insight.normalization")


def _join_texts(value):
    """把 list/str 的别名拼成逗号分隔串；空值返回 None"""
    if value is None:
        return None
    if isinstance(value, (list, tuple, set)):
        parts = [str(item).strip() for item in value if item and str(item).strip()]
        return ",".join(parts) if parts else None
    text = str(value).strip()
    return text or None


def _resolve_verification(requested, platform):
    """
    决定 verification 的最终取值

    Args:
        requested (str): 调用方请求的状态
        platform (str): 记录所属平台

    Returns:
        str: manual 平台尊重 requested（非法值则拒收，由 manual 采集器先校验过）；
             其余平台一律 UNVERIFIED
    """
    if str(platform).strip().lower() == MANUAL_PLATFORM:
        value = str(requested or "").strip()
        return value or UNVERIFIED
    return UNVERIFIED


def _clean_url(value):
    """
    清洗 URL：只接受 http/https，其余一律丢弃

    【为什么不用「兜底一个首页 URL」】
       假的溯源链接比没有链接更糟：它会让人以为这句话已被核实过。
       与其这样，不如诚实地留 NULL，让页面显示「无溯源」。
    """
    if not value:
        return None
    url = str(value).strip()
    if not url:
        return None
    lowered = url.lower()
    if not (lowered.startswith("http://") or lowered.startswith("https://")):
        return None
    return url


def _to_bool(value, default=True):
    """把各种真值表示转成 bool，识别不了时用 default"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "y", "是", "t"):
        return True
    if text in ("0", "false", "no", "n", "否", "f"):
        return False
    return default
