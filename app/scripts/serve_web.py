#!/usr/bin/env python3
"""
==============================================================================
StockLab - 本地分析服务 CLI 入口 (app/scripts/serve_web.py)
==============================================================================

【功能用途】
   启动本地 Web 分析服务：浏览器输入证券代码，点击分析即可查看
   历史估值分位、指标表格与历史走势，无需记忆命令行参数。

【执行流程】
   1. 安装浏览器 UA 补丁（规避东财 WAF 反爬，入口显式调用一次）
   2. 创建 Flask 应用（app.web.create_app）
   3. 仅监听 127.0.0.1，启动开发级服务器

【运行方式】
   # 默认端口 8000
   ./venv/bin/python app/scripts/serve_web.py

   # 指定端口与取数优先级
   ./venv/bin/python app/scripts/serve_web.py --port 8321 --priority remote_first
==============================================================================
"""

import argparse
import logging
import os
import sys

# 将项目根目录加入模块搜索路径，保证直接运行脚本时能 import stocklab
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.web import create_app
from stocklab.common.http_client import install_browser_user_agent

_logger = logging.getLogger("StockLab.ServeWeb")

# 服务默认只监听本机回环地址，不对外网暴露
_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8000


def parse_args():
    """
    解析命令行参数

    Returns:
        argparse.Namespace: 解析结果
    """
    parser = argparse.ArgumentParser(
        description="StockLab 本地 Web 分析服务（浏览器端点击分析）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--host",
        type=str,
        default=_DEFAULT_HOST,
        help="监听地址，默认仅本机回环 %s" % _DEFAULT_HOST,
    )
    parser.add_argument(
        "--port",
        type=int,
        default=_DEFAULT_PORT,
        help="监听端口，默认 %d" % _DEFAULT_PORT,
    )
    parser.add_argument(
        "--priority",
        type=str,
        choices=["local_first", "remote_first"],
        default=None,
        help="取数优先级；缺省时读取 config.ini 的 [data_source] priority",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default=None,
        help="本地 DuckDB 文件路径（默认 data/stocklab.duckdb）",
    )
    parser.add_argument(
        "--config",
        type=str,
        default="config.ini",
        help="配置文件路径（取数优先级），默认 config.ini",
    )
    return parser.parse_args()


def main():
    """
    主入口函数

    Returns:
        None: 正常情况下不返回（阻塞服务）；启动失败退出码 1
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 全局安装浏览器 UA 补丁（规避东财 WAF 反爬阻断），仅在入口显式调用一次
    install_browser_user_agent()

    args = parse_args()
    app = create_app(priority=args.priority, db_path=args.db_path)

    _logger.info("StockLab 分析服务启动: http://%s:%d/", args.host, args.port)
    _logger.info("按 Ctrl+C 停止服务")
    try:
        # threaded=True：静态资源与接口可并发响应；
        # facade 层已用全局锁保证 DuckDB 串行访问
        app.run(host=args.host, port=args.port, threaded=True, debug=False)
    except KeyboardInterrupt:
        _logger.info("服务已停止")


if __name__ == "__main__":
    main()
