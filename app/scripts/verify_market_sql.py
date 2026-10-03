#!/usr/bin/env python3
"""
==============================================================================
StockLab - 全市场分位 SQL 与 analyzer 口径一致性抽样校验
==============================================================================

【模块职责】
  抽样 N 只股票，比对「全市场分位 SQL」与 stocklab.analytics.ValuationPercentileAnalyzer
  的计算结果，确认两者口径一致。不一致时以退出码 1 结束（可进 CI）。

【为何需要这个校验】
  个股详情走 analyzer（权威口径），全市场排行走一条窗口函数 SQL（性能考虑，
  5572 只 × 794 行 = 442 万行，逐只算要 5572 次独立计算）。两者一旦出现偏差，
  首页排行与个股详情会给出互相矛盾的评级，属严重数据缺陷。

【为何用只读连接】
  本脚本只读表、不回写，用 read_only=True 语义最贴近实际用途。
  但它**仍要求先停止 serve_web.py**：DuckDB 对数据库文件的锁是进程级独占的，
  服务用进程级单例长连接后会长期持有写锁，此时其他进程连只读连接都会被拒
  （"Could not set lock on file"）。见 AGENT.md 注意事项。
  脚本检测到锁冲突时会给出明确提示而不是抛栈。

【运行方式】
  pkill -f serve_web.py       # 先停服务
  ./venv/bin/python app/scripts/verify_market_sql.py [抽样数量]
==============================================================================
"""

import logging
import os
import random
import sys

import duckdb
import pandas as pd

# 将项目根目录加入模块搜索路径，保证直接运行脚本时能 import stocklab
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# 直接引用 store 的 SQL 常量（私有名，但「校验对象就是这条 SQL」，抄一份反而会失真）
from app.web.store import _MARKET_SQL
from stocklab.analytics import ValuationPercentileAnalyzer

# 默认抽样数量
DEFAULT_SAMPLE_SIZE = 300

# 比对所用的指标列
INDICATOR = "pe_ttm"

# 本地数据库默认路径（与 stocklab.persistence 默认值一致）
DEFAULT_DB_PATH = "data/stocklab.duckdb"


def open_readonly(db_path):
    """
    打开只读连接；被其他进程占锁时给出可操作提示而不是抛栈

    Args:
        db_path (str): DuckDB 文件路径

    Returns:
        duckdb.DuckDBPyConnection: 只读连接（失败时直接退出进程）
    """
    try:
        return duckdb.connect(db_path, read_only=True)
    except Exception as exc:
        if "Could not set lock" in str(exc):
            print("错误：数据库文件被其他进程占用。")
            print("      服务运行期间会长期持有 DuckDB 写锁，请先停止服务再校验：")
            print("        pkill -f serve_web.py")
            sys.exit(1)
        raise


def load_market_rows(conn, indicator):
    """
    跑全市场分位 SQL，并按 store 的方式对分位做两位小数舍入

    Args:
        conn: 只读连接
        indicator (str): 指标列名

    Returns:
        list[dict]: 每项含 ts_code / percentile / sample_count
    """
    frame = conn.execute(_MARKET_SQL.format(col=indicator)).df()
    rows = []
    for row in frame.itertuples(index=False):
        if row.percentile is None or row.percentile != row.percentile:
            continue
        rows.append({
            "ts_code": row.ts_code,
            "percentile": round(float(row.percentile), 2),
            "sample_count": int(row.sample_count),
        })
    return rows


def load_history(conn, ts_code):
    """
    读取单只股票的估值历史（与 facade.fetch_valuation_history 同一张表）

    Args:
        conn: 只读连接
        ts_code (str): 证券代码

    Returns:
        pd.DataFrame: 按交易日升序的历史序列；无数据返回 None
    """
    frame = conn.execute(
        "SELECT * FROM market.valuation_history WHERE ts_code = ? ORDER BY trade_date",
        [ts_code],
    ).df()
    if frame.empty:
        return None
    frame = frame.copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date
    return frame


def analyze_percentile(frame, indicator):
    """
    跑 analyzer 并取出指定指标的分位结果

    Args:
        frame (pd.DataFrame): 历史序列
        indicator (str): 指标列名

    Returns:
        object: PercentileResult；未找到返回 None
    """
    for item in ValuationPercentileAnalyzer().analyze(frame):
        if item.indicator == indicator:
            return item
    return None


def main():
    """抽样校验入口"""
    logging.basicConfig(level=logging.ERROR)

    db_path = DEFAULT_DB_PATH
    count = DEFAULT_SAMPLE_SIZE
    for arg in sys.argv[1:]:
        if arg.endswith(".duckdb"):
            db_path = arg
        else:
            count = int(arg)

    conn = open_readonly(db_path)
    try:
        rows = load_market_rows(conn, INDICATOR)
        print("全市场 %s 有效标的: %d 只" % (INDICATOR, len(rows)))
        if not rows:
            # 没有样本时不能宣称「通过」，否则校验结果是假阳性
            print("错误：全市场分位结果为空，无法校验")
            sys.exit(1)

        random.seed(7)
        picks = random.sample(rows, min(count, len(rows)))

        matched = 0
        mismatches = []
        skipped = 0

        for pick in picks:
            ts_code = pick["ts_code"]
            history = load_history(conn, ts_code)
            if history is None:
                skipped += 1
                continue

            target = analyze_percentile(history, INDICATOR)
            if target is None:
                skipped += 1
                continue

            if target.percentile is None:
                mismatches.append((ts_code, "None", pick["percentile"],
                                   "SQL 有值但 analyzer 为 None"))
                continue

            rounded = round(target.percentile, 2)
            if rounded == pick["percentile"] and target.sample_count == pick["sample_count"]:
                matched += 1
            else:
                mismatches.append(
                    (ts_code, target.percentile, pick["percentile"],
                     "样本数 %s vs %s" % (target.sample_count, pick["sample_count"]))
                )
    finally:
        conn.close()

    print("一致: %d   不一致: %d   跳过: %d" % (matched, len(mismatches), skipped))
    for item in mismatches[:20]:
        print("   MISMATCH", item)

    if mismatches:
        sys.exit(1)
    if not matched:
        # 全部标的都被跳过时同样不能宣称通过
        print("错误：没有任何标的成功比对（跳过 %d 条）" % skipped)
        sys.exit(1)
    print("口径一致，校验通过（比对 %d 只）" % matched)


if __name__ == "__main__":
    main()
