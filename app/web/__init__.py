"""
StockLab 本地 Web 分析服务 (app.web)

【模块职责】]
  把已有的分析能力（facade 取数 + analytics 计算）以 HTTP 接口暴露给浏览器，
  提供「输入代码 → 点击分析 → 图表与分位结论」的交互体验。

【分层约束】
  本包位于应用层，依赖方向 app.web -> stocklab.facade / stocklab.analytics，
  与 app.dashboard 平行；不修改 stocklab 核心库，也不被核心库反向依赖。
"""

from .server import create_app

__all__ = [
    "create_app",
]
