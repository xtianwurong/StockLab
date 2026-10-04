#!/usr/bin/env python3
"""
==============================================================================
StockLab 投资人观点取数门面 (stocklab.facade.insight_data)
==============================================================================

【模块职责】
  投资理念页（/insight）三个接口背后的取数编排：把「筛选面板元数据」「多维
  筛选言论」「单投资人档案」这三种查询，从三个 Repository 组装成上层可直接
  消费的结构。

【为什么不把 Repository 交给 app.web 自己拼】
  `AGENT.md` 的依赖方向写明：`app.web` 只依赖 `stocklab.facade /
  analytics / screener / factor / research`，**不得直接 import
  stocklab.persistence**。原因是取数逻辑一旦散落在接口层，同一个查询会在
  多个接口里各写一份，口径迟早分叉 —— 例如「主题分布」必须先把逗号分隔的
  多值列 unnest 成单值行再分组，这件事一旦被复制到别处，两边迟早不一致。
  本模块就是那条边界的落点：接口层只做参数校验、展示文案与 JSON 组装。

【本域特有的三条约束，实现必须守住】
  1. **核验状态必须随每条言论一起下发**，meta 另给全库分布。
     少了它，前端无法诚实地呈现「这句话我们有没有核实过出处」。
  2. **不做任何自动总结 / 打分 / 推断**。本模块只做「取数 + 过滤 + 计数」，
     言论正文原样返回。自动总结会把 unverified 的转述再加工一层。
  3. **查不到就返回 None，不猜**。不存在的投资人不返回「最接近的那个」。

【取数路径】
  不自己开连接：由调用方（app.web.store）借出门面那一个 Database。
  DuckDB 同文件只允许一个写连接，第二个 connect 会抛 Could not set lock。

【写入路径不在这里】
  同步 CLI 直接用 Repository（与 sync_market_data.py 一致），因为落库编排
  是入口脚本的职责，与「网页怎么读」是两回事。
==============================================================================
"""

import logging

from stocklab.persistence.repository.insight import (
    InvestorAccountRepository,
    InvestorQuoteRepository,
    InvestorRepository,
)

_logger = logging.getLogger("StockLab.Facade.InsightData")

__all__ = ["InsightDataFacade"]


class InsightDataFacade:
    """
    投资人观点取数门面

    构造时只持有仓库，不做查询；每次调用即一次查询，无进程内缓存 ——
    言论库是小表且随同步增长，缓存失效的复杂度不值得。
    """

    def __init__(self, database):
        """
        Args:
            database (Database): 已打开的数据库句柄（**由调用方借出**，
                本类不开新连接、也不负责关闭它）
        """
        self._database = database
        self._investor_repo = InvestorRepository(database)
        self._account_repo = InvestorAccountRepository(database)
        self._quote_repo = InvestorQuoteRepository(database)

    # ------------------------------------------------------------------
    # 筛选面板元数据
    # ------------------------------------------------------------------
    def metadata(self, theme_limit=40):
        """
        筛选面板所需的一次性元数据

        Args:
            theme_limit (int): 主题条目上限（主题是开放集合，必须封顶）

        Returns:
            dict:
                investors  pd.DataFrame 投资人（含 quote_count /
                           verified_count / last_captured 三列统计；
                           **无言论的投资人也会出现**，空状态是这页要正确
                           展示的场景）
                themes     pd.DataFrame theme / quote_count，按条数降序
                summary    dict 核验状态分布 {status: count}，空库为 {}
                counts     dict {"investor": n, "account": n, "quote": n}
        """
        return {
            "investors": self._quote_repo.list_investors(),
            "themes": self._quote_repo.list_themes(limit=theme_limit),
            "summary": self._quote_repo.verification_summary(),
            "counts": {
                "investor": self._investor_repo.count(),
                "account": self._account_repo.count(),
                "quote": self._quote_repo.count(),
            },
        }

    # ------------------------------------------------------------------
    # 多维筛选
    # ------------------------------------------------------------------
    def search_quotes(
        self,
        keyword=None,
        investor_codes=None,
        platforms=None,
        quote_types=None,
        themes=None,
        verification=None,
        stock_codes=None,
        since=None,
        order_by="captured_at",
        limit=30,
        offset=0,
    ):
        """
        多维筛选言论

        入参取值是否**合法**由接口层负责（它才知道哪些值对用户可见并给出
        400），这里只保证不越权解释未知取值、并把过滤原样下推到 SQL。

        Args:
            keyword (str|None): 正文关键词（None 表示不按关键词过滤）
            investor_codes (list|None): 投资人代码
            platforms (list|None): 平台
            quote_types (list|None): 言论类型
            themes (list|None): 主题（命中任一即可）
            verification (str|None): 单一核验状态
            stock_codes (list|None): 涉及的标的代码
            since (str|None): YYYY-MM-DD，只看 published_at 不早于该日
            order_by (str): captured_at / published_at / investor_code
            limit (int): 分页大小
            offset (int): 分页偏移

        Returns:
            pd.DataFrame: 命中言论，按 order_by 排序（可能为空表）
        """
        return self._quote_repo.search(
            keyword=keyword,
            investor_codes=investor_codes,
            platforms=platforms,
            quote_types=quote_types,
            themes=themes,
            verification=verification,
            stock_codes=stock_codes,
            since=since,
            order_by=order_by,
            limit=limit,
            offset=offset,
        )

    # ------------------------------------------------------------------
    # 投资人档案
    # ------------------------------------------------------------------
    def profile(self, investor_code, limit=20):
        """
        单个投资人档案：本人 + 平台账号 + 观点时间线 + 该库核验分布

        Args:
            investor_code (str): 投资人代码
            limit (int): 时间线条数上限

        Returns:
            dict | None: 不存在该投资人时返回 None（调用方据此回 404，
                **不返回「最接近的那个」**）；否则：
                    investor pd.DataFrame 1 行
                    accounts pd.DataFrame 该人的平台账号
                    quotes   pd.DataFrame 观点时间线（published_at 倒序）
                    summary  dict 全库核验状态分布
        """
        investor = self._investor_repo.find_by_code(investor_code)
        if investor is None or investor.empty:
            _logger.info("投资人不存在: %r", investor_code)
            return None
        return {
            "investor": investor,
            "accounts": self._account_repo.find_by_investor(investor_code),
            "quotes": self._quote_repo.search(
                investor_codes=[investor_code],
                order_by="published_at",
                limit=limit,
            ),
            "summary": self._quote_repo.verification_summary(),
        }
