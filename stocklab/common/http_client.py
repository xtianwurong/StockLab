#!/usr/bin/env python3
"""
==============================================================================
StockLab - HTTP 客户端配置模块 (stocklab.common.http_client)
==============================================================================

【模块职责】
   统一管理 HTTP 客户端的全局配置，如 User-Agent 补丁。
   避免在数据源模块中隐式修改全局状态。

【设计原则】
   - 显式调用：由应用层在启动时显式调用，而非导入时自动执行
   - 单一职责：只负责 HTTP 客户端配置，不涉及数据获取逻辑
   - 可测试性：测试时可轻松禁用或 mock
"""

import logging

import requests

_logger = logging.getLogger(__name__)

__all__ = [
    "install_browser_user_agent",
    "BROWSER_USER_AGENT",
]

# 浏览器 User-Agent（规避东财 WAF 反爬阻断）
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 是否已安装补丁（保证幂等，避免重复调用叠加多层 wrapper）
_installed = False


def install_browser_user_agent():
    """
    在运行时为 requests 全局 Session.request 挂载浏览器 User-Agent 补丁

    【使用方式】
        from stocklab.common.http_client import install_browser_user_agent

        # 在应用启动时显式调用
        install_browser_user_agent()

    【注意事项】
        - 此函数会修改全局 requests.Session.request 方法
        - 重复调用不会产生重复补丁（内部做了幂等判断）
        - 建议在应用入口（如 main() 函数）中调用
    """
    global _installed
    if _installed:
        return

    original_session_request = requests.Session.request

    def session_request_with_browser_user_agent(self, method, url, **kwargs):
        headers = kwargs.get("headers")
        if headers is None:
            headers = {}
        # 仅当调用方未显式传递 User-Agent 时才赋默认值，不覆盖显式入参
        headers.setdefault("User-Agent", BROWSER_USER_AGENT)
        kwargs["headers"] = headers
        return original_session_request(self, method, url, **kwargs)

    # 替换函数绑定
    requests.Session.request = session_request_with_browser_user_agent
    _installed = True
    _logger.debug("浏览器 User-Agent 补丁已安装")
