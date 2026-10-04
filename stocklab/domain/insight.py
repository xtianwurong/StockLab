#!/usr/bin/env python3
"""
==============================================================================
StockLab - 投资人观点领域契约 (stocklab.domain.insight)
==============================================================================

【模块职责】
   定义「投资理念 / 公开言论」域的列契约（列名 + 列序，与
   006_investor_insight.sql 严格同序）：
     - INVESTOR_COLUMNS        insight.investors        投资人主表
     - INVESTOR_ACCOUNT_COLUMNS insight.investor_accounts 平台账号映射
     - INVESTOR_QUOTE_COLUMNS  insight.investor_quotes  理念与言论

【为什么拆成三张表而不是一张】
   投资人、平台账号、言论是三个变化频率完全不同的实体：
     · 投资人     —— 几乎不变（新增一位价值投资家必须人工确认）
     · 平台账号   —— 会变（雪球账号被封 / 改名 / 新开号），且一个人有多个号
     · 言论       —— 每次同步都在增量
   合成一张表的后果：改一次账号要把上万条言论一起 UPDATE，
   而言论本身是不可变的（既成事实），UPDATE 会破坏审计链。

【写入语义】
   三张表都**只 INSERT、不 UPDATE**：
     - 投资人 / 账号是「已确认的事实」，变了就新增一条历史记录；
     - 言论是既成事实，纠正靠 verification 字段表达，不靠改原文。

【核心风险控制：verification 字段】
   这是本域最容易被做成「谣言集」的地方。国内投资平台上大量流传的
   「名人语录」无法溯源到原始出处 —— 同一句话在不同帖子间以不同署名传播，
   甚至是被伪造的。因此：
     1. source_url 允许为 NULL（表示「无溯源链接」，而不是硬凑一个）；
     2. verification 默认 'unverified'，只有人工核对过原文出处才改 'verified'；
     3. 采集侧**禁止**自动写 verified —— 没有任何自动化手段能保证语录属实，
        这必须是人做的判断，写进代码里就是伪自动化。
   页面与报告必须把该状态显示出来，不得把 unverified 当成名人原话呈现。
"""

import json

__all__ = [
    "INVESTOR_COLUMNS",
    "INVESTOR_ACCOUNT_COLUMNS",
    "INVESTOR_QUOTE_COLUMNS",
    "dumps_raw_meta",
    "decode_raw_meta",
    "PLATFORMS",
    "QUOTE_TYPES",
    "VERIFICATION_STATUSES",
    "INVESTOR_STYLES",
]

# ---------------------------------------------------------------------------
# 投资人主表
# ---------------------------------------------------------------------------
INVESTOR_COLUMNS = (
    "investor_code",
    "name",
    "aliases",
    "role",
    "organization",
    "style_tags",
    "profile_url",
    "is_active",
    "created_time",
)

# ---------------------------------------------------------------------------
# 平台账号映射（投资人 × 平台）
# ---------------------------------------------------------------------------
INVESTOR_ACCOUNT_COLUMNS = (
    "account_id",
    "investor_code",
    "platform",
    "account_name",
    "account_uid",
    "home_url",
    "is_enabled",
    "note",
    "created_time",
)

# ---------------------------------------------------------------------------
# 理念与言论
# ---------------------------------------------------------------------------
INVESTOR_QUOTE_COLUMNS = (
    "quote_id",
    "investor_code",
    "platform",
    "account_name",
    "source_url",
    "published_at",
    "captured_at",
    "content_hash",
    "content",
    "summary",
    "quote_type",
    "theme",
    "stock_codes",
    "verification",
    "language",
    "raw_meta",
)

# 平台标识（与 collector 的 SOURCE_NAME 对齐；新增平台须同步注册 collector）
PLATFORMS = (
    "xueqiu",        # 雪球
    "guba",          # 东方财富股吧
    "eastmoney",     # 东方财富财富号 / 博客
    "weibo",         # 微博
    "manual",        # 人工录入（线下访谈、书籍、演讲稿）
)

# 言论类型：决定页面上怎么分组展示，也决定下游能用它做什么
QUOTE_TYPES = (
    "philosophy",    # 投资理念 / 方法论（长期稳定）
    "macro_view",    # 宏观与大盘判断（时效性强）
    "selection",     # 选股标准 / 买入条件
    "position",      # 对具体标的的公开看法
    "caution",       # 风险提示 / 反对意见
    "discipline",    # 纪律、心态、持仓耐心
)

# 核验状态：见模块头部说明
VERIFICATION_STATUSES = (
    "unverified",    # 默认：已抓取，但未核对原始出处
    "verified",      # 人工核对过原始出处
    "disputed",      # 网上流传但存在互相矛盾的版本
    "fabricated",    # 判定为伪造/张冠李戴
)

# 投资风格标签（供页面筛选与画像）
INVESTOR_STYLES = (
    "value",         # 价值投资
    "growth",        # 成长 / 科技
    "value_growth",  # 价值成长（GARP 类）
    "macro",         # 宏观择时
    "turnaround",    # 困境反转
)


# ----------------------------------------------------------------------------
# raw_meta 列编解码
# ----------------------------------------------------------------------------
# 【为什么放在 domain 而不是 persistence/repository】
#   这两个函数描述的是「raw_meta 这一列是什么格式」，属于**列契约**的一部分。
#   放在 repository 里，接口层为了读一列就得 import stocklab.persistence ——
#   与 AGENT.md 写明的依赖方向（app.web 不得 import persistence）冲突。
#   persistence 层继续 re-export 这两个名字，老的 import 路径不变。
def dumps_raw_meta(payload):
    """
    把原始载荷序列化成 raw_meta 列

    Args:
        payload (dict): 采集到的原始字段

    Returns:
        str: JSON 文本；payload 为空或无法序列化时返回 None
    """
    if not payload:
        return None
    try:
        return json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        # 原始字段是「线索」不是「数据」，丢掉它不该中断整轮同步
        return None


def decode_raw_meta(text):
    """
    解析 raw_meta 列

    Args:
        text (str): JSON 文本或 None

    Returns:
        dict: 原始载荷；解析失败返回空字典（**不抛异常** ——
            一条脏的原始字段不该让整页言论都读不出来）
    """
    if not text:
        return {}
    try:
        result = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return result if isinstance(result, dict) else {}
