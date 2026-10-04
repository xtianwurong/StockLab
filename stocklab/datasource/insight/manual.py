#!/usr/bin/env python3
"""
==============================================================================
StockLab - 人工录入采集器 (stocklab.datasource.insight.manual)
==============================================================================

【模块职责】
   从本地 JSON 文件读入人工整理的观点条目。这是**唯一**允许写入
   「非抓取内容」的采集器 —— 来源由人给全（书籍页码 / 访谈链接 / 演讲视频
   时间戳），而不是从平台上抓来。

【为什么必须有这个采集器，而不是让用户在网页上手填】
   价值投资最核心的东西本来就不在交易平台上：林园的访谈在电视与公众号，
   段永平早期的东西在网易博客，但斌的观点在私募路演与采访里，
   巴菲特/芒格的经典表述在书里。这些是本域**质量最高**的部分，
   却一条也抓不到。所以「人工录入」不是补充手段，是主线之一。

   同时它也解决了 base.py 里那条红线的另一面：既然采集器不得编造内容，
   那「我确实知道这条语录的出处」这件事就必须有一个正式入口，
   而不是靠改数据库。

【文件格式】
   顶层 {"quotes": [ {...}, ... ]}，也可以直接是列表。字段：
     content        必填，正文
     investor_code  必填，投资人代码（须与 investors 表对得上）
     quote_type     可选，见 domain.insight.QUOTE_TYPES
     theme          可选
     stock_codes    可选
     source_url     强烈建议填（书/视频的链接），留空则页面上显示「无溯源」
     platform       可选，默认 'manual'
     published_at   可选，观点原始产生时间（没有就用采集时间，别编造）
     account_name   可选
     verification   可选，**仅当人工核对过原始出处**才填 'verified'

【verification 允许从这里写成 verified】
   base.py 禁止采集侧写 verified，人工录入是唯一例外 —— 因为它背后的判断
   是「一个人对照原始出处确认过了」，而不是「程序抓到了」。若填写内容里
   verification 既不是 verified 也不是四个合法值之一，本条会被拒收并报错，
   而不是悄悄改成 unverified（悄悄降级同样会让人误信）。
"""

import json
import logging
import os

from stocklab.domain import VERIFICATION_STATUSES
from stocklab.datasource.insight.base import (
    InsightCollector,
    InsightCollectError,
)

_logger = logging.getLogger(__name__)

__all__ = [
    "ManualInsightCollector",
    "DEFAULT_SEED_PATH",
]

# 默认的录入文件位置（相对项目根）
DEFAULT_SEED_PATH = os.path.join("data", "insight_manual.json")


class ManualInsightCollector(InsightCollector):
    """
    人工录入采集器（读本地 JSON）

    与其他采集器的关键差异：
      - 不发任何网络请求，不限速，不需要凭证；
      - 数据来自文件，所以**天然可离线测试**，不依赖平台可用性。
    """

    PLATFORM = "manual"
    DISPLAY_NAME = "人工录入"
    REQUIRES_CREDENTIAL = False
    MIN_INTERVAL_SECONDS = 0.0

    def __init__(self, path=None, **kwargs):
        """
        Args:
            path (str, optional): 录入文件路径，默认 data/insight_manual.json
            **kwargs: 透传给基类（便于测试注入 session 等）
        """
        super().__init__(**kwargs)
        self._path = path or DEFAULT_SEED_PATH

    @property
    def path(self):
        """录入文件路径"""
        return self._path

    def fetch(self, request):
        """
        读取录入文件并返回记录

        Args:
            request (CollectRequest): 采集参数；account 可为空（人工录入
                通常不绑定平台账号）

        Returns:
            list[dict]: 通用记录列表

        Raises:
            InsightCollectError: 文件不存在 / JSON 非法 / 条目字段不对
        """
        if not os.path.exists(self._path):
            raise InsightCollectError(
                "人工录入文件不存在: %s（模板见 %s）"
                % (self._path, "data/insight_manual.example.json")
            )
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except json.JSONDecodeError as error:
            raise InsightCollectError(
                "人工录入文件 JSON 解析失败 %s: %s" % (self._path, error)
            ) from error
        except OSError as error:
            raise InsightCollectError(
                "读取人工录入文件失败 %s: %s" % (self._path, error)
            ) from error

        if isinstance(payload, dict):
            entries = payload.get("quotes")
            if entries is None:
                raise InsightCollectError(
                    "人工录入文件是对象但没有 quotes 字段: %s" % self._path
                )
        elif isinstance(payload, list):
            entries = payload
        else:
            raise InsightCollectError(
                "人工录入文件顶层应为对象或列表，实际是 %s"
                % type(payload).__name__
            )

        if not isinstance(entries, list):
            raise InsightCollectError(
                "人工录入文件的 quotes 应为列表，实际是 %s"
                % type(entries).__name__
            )

        records = []
        for index, entry in enumerate(entries):
            record = self._parse_entry(entry, request, index)
            if record:
                records.append(record)
        _logger.info("人工录入：读到 %d 条（来源 %s）", len(records), self._path)
        return records

    def _parse_entry(self, entry, request, index):
        """
        校验并转换单条录入

        【为什么不静默跳过坏数据】
           录入是人工的，一条格式错误就说明文件写错了。跳过它等于
           「库里少了一条我不知道的东西」，这在语录库里是最糟的失败模式。
           所以格式不对就整轮失败，把行号报出来。

        Args:
            entry (dict): 一条录入
            request (CollectRequest): 采集参数
            index (int): 行序号（用于报错定位）

        Returns:
            dict | None: 通用记录；内容为空的条目返回 None（那是空壳，不是错误）

        Raises:
            InsightCollectError: 字段类型/取值不对
        """
        if not isinstance(entry, dict):
            raise InsightCollectError(
                "人工录入第 %d 条不是对象（是 %s）"
                % (index + 1, type(entry).__name__)
            )

        content = entry.get("content")
        if content is None or not str(content).strip():
            # 内容为空的条目直接跳过而不报错：留个空位给人补是常见用法
            return None

        investor_code = str(
            entry.get("investor_code") or request.investor_code or ""
        ).strip()
        if not investor_code:
            raise InsightCollectError(
                "人工录入第 %d 条缺少 investor_code" % (index + 1)
            )

        verification = entry.get("verification")
        if verification in (None, ""):
            verification = "unverified"
        else:
            verification = str(verification).strip()
            if verification not in VERIFICATION_STATUSES:
                raise InsightCollectError(
                    "人工录入第 %d 条 verification=%s 不是合法值，应为 %s"
                    % (index + 1, verification, "/".join(VERIFICATION_STATUSES))
                )

        return {
            "platform_id": entry.get("platform_id"),
            "platform": str(entry.get("platform") or self.PLATFORM).strip(),
            "investor_code": investor_code,
            "account_name": entry.get("account_name"),
            "source_url": entry.get("source_url"),
            "published_at": entry.get("published_at"),
            "content": content,
            "quote_type": entry.get("quote_type"),
            "theme": entry.get("theme"),
            "stock_codes": entry.get("stock_codes"),
            # 人工录入是唯一允许把 verification 传出去的通道（见模块说明）
            "verification": verification,
            "language": entry.get("language") or "zh",
            "raw_meta": entry.get("raw_meta"),
        }
