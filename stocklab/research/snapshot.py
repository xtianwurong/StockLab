#!/usr/bin/env python3
"""
==============================================================================
StockLab - 研究快照 (stocklab.research.snapshot)
==============================================================================

【模块职责】
   V2 需求 §6 与 §2.5「研究结果必须可复现」：
     - create_snapshot  把一次筛选连同它的全部可复现要素落库，返回 snapshot_id
     - load_snapshot    读回快照（spec + 逐股结果）
     - rerun_snapshot   按快照 spec **重新生成**结果，并与存档逐行比对
     - config_version / data_version  生成配置版本号与数据版本号

【一条快照必须能回答】（§2.5）
   使用了什么数据？      data_version（库结构版本 + 数据截止日）
   数据截至什么时候？    as_of_date（并存展开后的股票池）
   使用什么因子？        factor_version（因子库版本）
   使用什么参数？        spec_json（筛选条件 + 预处理配置）+ config_version（其哈希）
   使用什么股票池？      universe 标签 + spec_json 里的 ts_code 明细

【不可变】
   快照只写不改：重复执行同一研究会产生新的 snapshot_id，历史结论不被覆盖。
"""

import datetime
import hashlib
import json
import logging

import pandas as pd

from stocklab.domain import SNAPSHOT_COLUMNS
from stocklab.factor import FACTOR_VERSION
from stocklab.persistence.repository import (
    ResearchSnapshotRepository,
    SnapshotResultRepository,
)
from stocklab.persistence.storage import Database
from stocklab.research.frame import build_factor_frame
from stocklab.screener import ScreenPipeline

_logger = logging.getLogger(__name__)

__all__ = [
    "SnapshotError",
    "create_snapshot",
    "load_snapshot",
    "list_snapshots",
    "rerun_snapshot",
    "config_version",
    "data_version",
]

# 快照编号前缀（Research Snapshot）
SNAPSHOT_PREFIX = "RS"


class SnapshotError(Exception):
    """研究快照非法或无法复现（写入失败、spec 残缺、重跑不一致等）"""


def _canonical_json(payload):
    """把 spec 序列化成稳定的 JSON 文本（键排序、无多余空白，保证同一配置同一哈希）"""
    return json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=_json_default,
    )


def _json_default(value):
    """JSON 序列化兜底：集合转列表、日期时间转 ISO 字符串"""
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    if isinstance(value, (datetime.date, datetime.datetime, datetime.time)):
        return value.isoformat()
    return str(value)


def _json_number(value):
    """把因子值转成可 JSON 化的数值（缺失 -> None，其余保留 float）"""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return number


def _to_date(value):
    """
    把 DATE 查询结果统一成 datetime.date

    【为什么需要】
       DuckDB 的 fetchdf 会把 DATE 读成 pandas.Timestamp，而 fetchone 读成
       datetime.date；快照对外只暴露一种口径，避免调用方拿 == 判断时踩坑。
    """
    if isinstance(value, datetime.datetime):
        return value.date()
    return value


def config_version(screen_spec):
    """
    计算配置版本号：筛选条件与预处理配置的内容哈希

    Args:
        screen_spec (dict): ScreenPipeline.spec()

    Returns:
        str: 十六进制前 12 位；同一配置必然得到同一版本号
    """
    digest = hashlib.sha256(_canonical_json(screen_spec).encode("utf-8")).hexdigest()
    return digest[:12]


def data_version(database, as_of_date):
    """
    计算数据版本号：库结构版本 + 实际取到的数据截止日

    Args:
        database (Database): 数据库实例
        as_of_date (str 或 datetime.date): 研究时点

    Returns:
        str: 形如 "schema_v4@2026-10-01"
    """
    conn = database.get_connection()
    version_row = conn.execute("SELECT MAX(version) FROM sys.schema_version").fetchone()
    schema_version = int(version_row[0]) if version_row and version_row[0] else 0

    cutoff_row = conn.execute(
        "SELECT MAX(trade_date) FROM market.valuation_history WHERE trade_date <= ?",
        [as_of_date],
    ).fetchone()
    cutoff = str(cutoff_row[0]) if cutoff_row and cutoff_row[0] else str(as_of_date)
    return "schema_v%d@%s" % (schema_version, cutoff)


def create_snapshot(as_of_date, universe_label, pipeline, summary, database=None):
    """
    写入一次研究快照（元数据 + 逐股结果）

    Args:
        as_of_date (str 或 datetime.date): 研究时点
        universe_label (str): 股票池标签，如 "A股全市场"
        pipeline (ScreenPipeline): 已执行的筛选流水线（提供条件与 spec）
        summary (pd.DataFrame): 筛选结果 summary（ts_code / passed / failed_rules / 因子值）
        database (Database, optional): 数据库实例，默认新建

    Returns:
        str: snapshot_id

    Raises:
        SnapshotError: 快照或结果写入失败
    """
    database = database if database else Database()
    created_at = datetime.datetime.now()
    if "ts_code" not in summary.columns or "passed" not in summary.columns:
        raise SnapshotError(
            "summary 必须至少包含 ts_code 与 passed 两列，实际: %s"
            % list(summary.columns)
        )
    ts_codes = summary["ts_code"].tolist() if len(summary) else []

    spec = {
        "universe": {"label": universe_label, "ts_codes": ts_codes},
        "screen": pipeline.spec(),
    }
    spec_json = _canonical_json(spec)
    digest = hashlib.sha1(
        (created_at.isoformat() + spec_json).encode("utf-8")
    ).hexdigest()[:6]
    snapshot_id = "%s%s-%s" % (
        SNAPSHOT_PREFIX,
        created_at.strftime("%Y%m%d%H%M%S"),
        digest,
    )

    factor_names = [
        name for name in pipeline.factor_names if name in summary.columns
    ]
    snapshot_row = pd.DataFrame(
        [
            {
                "snapshot_id": snapshot_id,
                "created_at": created_at,
                "as_of_date": as_of_date
                if isinstance(as_of_date, datetime.date)
                else datetime.date.fromisoformat(str(as_of_date)[:10]),
                "universe": universe_label,
                "universe_size": len(ts_codes),
                "data_version": data_version(database, as_of_date),
                "factor_version": FACTOR_VERSION,
                "config_version": config_version(pipeline.spec()),
                "condition": pipeline.condition,
                "passed_count": int(summary["passed"].sum()) if len(summary) else 0,
                "result_count": len(ts_codes),
                "spec_json": spec_json,
            }
        ]
    )
    # 列序显式对齐领域契约（不依赖字面量顺序）
    snapshot_row = snapshot_row[list(SNAPSHOT_COLUMNS)]

    records = []
    for row in summary.to_dict(orient="records"):
        records.append(
            {
                "snapshot_id": snapshot_id,
                "ts_code": row["ts_code"],
                "passed": bool(row["passed"]),
                "failed_rules": _canonical_json(list(row.get("failed_rules") or [])),
                "factor_values": _canonical_json(
                    {
                        name: _json_number(row.get(name))
                        for name in factor_names
                    }
                ),
            }
        )

    snapshot_repository = ResearchSnapshotRepository(database)
    written = snapshot_repository.insert(snapshot_row)
    if written != 1:
        raise SnapshotError("快照 %s 写入失败（详见日志）" % snapshot_id)

    results_written = SnapshotResultRepository(database).insert(
        pd.DataFrame(records) if records else None
    )
    if results_written != len(records):
        raise SnapshotError(
            "快照 %s 的逐股结果写入失败：期望 %d 条，实际 %d 条"
            % (snapshot_id, len(records), results_written)
        )

    _logger.info(
        "研究快照已写入: %s（as_of=%s, 股票 %d 只, 通过 %d 只, config=%s）",
        snapshot_id,
        as_of_date,
        len(ts_codes),
        snapshot_row.iloc[0]["passed_count"],
        snapshot_row.iloc[0]["config_version"],
    )
    return snapshot_id


def list_snapshots(database=None):
    """列出全部研究快照（按创建时间倒序）"""
    database = database if database else Database()
    return ResearchSnapshotRepository(database).find_all()


def load_snapshot(snapshot_id, database=None):
    """
    读回一条研究快照（spec 与逐股结果都已解析）

    Args:
        snapshot_id (str): 快照编号
        database (Database, optional): 数据库实例，默认新建

    Returns:
        dict: 元数据 + spec + results（DataFrame）

    Raises:
        SnapshotError: 快照不存在
    """
    database = database if database else Database()
    row = ResearchSnapshotRepository(database).find(snapshot_id)
    if row is None:
        raise SnapshotError("快照 %s 不存在" % snapshot_id)

    try:
        spec = json.loads(row["spec_json"])
    except ValueError as error:
        raise SnapshotError("快照 %s 的 spec_json 无法解析: %s" % (snapshot_id, error))

    results = SnapshotResultRepository(database).find_by_snapshot(snapshot_id)
    parsed = []
    for record in results.to_dict(orient="records"):
        parsed.append(
            {
                "ts_code": record["ts_code"],
                "passed": bool(record["passed"]),
                "failed_rules": json.loads(record["failed_rules"] or "[]"),
                "factor_values": json.loads(record["factor_values"] or "{}"),
            }
        )

    return {
        "snapshot_id": row["snapshot_id"],
        "created_at": row["created_at"],
        "as_of_date": _to_date(row["as_of_date"]),
        "universe": row["universe"],
        "universe_size": int(row["universe_size"]),
        "data_version": row["data_version"],
        "factor_version": row["factor_version"],
        "config_version": row["config_version"],
        "condition": row["condition"],
        "passed_count": int(row["passed_count"]),
        "result_count": int(row["result_count"]),
        "spec": spec,
        "results": pd.DataFrame(parsed, columns=[
            "ts_code", "passed", "failed_rules", "factor_values",
        ]),
    }


def rerun_snapshot(snapshot_id, database=None):
    """
    按快照记录的 spec **重新生成**研究结果，并与存档逐行比对

    Args:
        snapshot_id (str): 快照编号
        database (Database, optional): 数据库实例，默认新建

    Returns:
        dict: {"snapshot_id", "identical", "differences": [描述], "result": ScreenResult}

    Raises:
        SnapshotError: 快照不存在、spec 残缺
    """
    database = database if database else Database()
    snapshot = load_snapshot(snapshot_id, database)
    spec = snapshot["spec"]

    if "universe" not in spec or "screen" not in spec:
        raise SnapshotError("快照 %s 的 spec 缺少 universe/screen 节点" % snapshot_id)

    try:
        pipeline = ScreenPipeline.from_spec(spec["screen"])
    except Exception as error:
        raise SnapshotError("快照 %s 的筛选条件无法解析: %s" % (snapshot_id, error))

    frame = build_factor_frame(
        snapshot["as_of_date"],
        ts_codes=spec["universe"].get("ts_codes"),
        database=database,
    )
    result = pipeline.run(frame)

    differences = _compare(snapshot, result)
    return {
        "snapshot_id": snapshot_id,
        "identical": not differences,
        "differences": differences,
        "result": result,
        "condition": snapshot["condition"],
    }


def _compare(snapshot, result):
    """
    比对重跑结果与存档结果

    Args:
        snapshot (dict): load_snapshot 的返回值
        result (ScreenResult): 重跑结果

    Returns:
        list: 差异描述列表，空列表表示完全一致
    """
    differences = []
    stored = snapshot["results"]
    fresh = result.summary

    stored_codes = stored["ts_code"].tolist() if len(stored) else []
    fresh_codes = fresh["ts_code"].tolist() if len(fresh) else []
    if stored_codes != fresh_codes:
        differences.append(
            "股票池不一致: 存档 %d 只 / 重跑 %d 只" % (len(stored_codes), len(fresh_codes))
        )
        return differences

    stored_by_code = {
        row["ts_code"]: row for row in stored.to_dict(orient="records")
    }
    mismatched_pass = 0
    mismatched_value = 0
    for row in fresh.to_dict(orient="records"):
        archived = stored_by_code[row["ts_code"]]
        if bool(row["passed"]) != bool(archived["passed"]):
            mismatched_pass += 1
            differences.append(
                "%s 判定不一致: 存档 %s / 重跑 %s"
                % (row["ts_code"], archived["passed"], row["passed"])
            )
        for name, value in archived["factor_values"].items():
            fresh_value = _json_number(row.get(name))
            if value is None and fresh_value is None:
                continue
            if value is None or fresh_value is None or abs(value - fresh_value) > 1e-9:
                mismatched_value += 1
                differences.append(
                    "%s 的 %s 不一致: 存档 %s / 重跑 %s"
                    % (row["ts_code"], name, value, fresh_value)
                )

    if mismatched_pass or mismatched_value:
        _logger.warning(
            "快照 %s 重跑不一致: 判定差异 %d 处，因子值差异 %d 处",
            snapshot["snapshot_id"],
            mismatched_pass,
            mismatched_value,
        )
    else:
        _logger.info(
            "快照 %s 重跑一致: %d 只标的逐行相同", snapshot["snapshot_id"], len(fresh)
        )
    return differences
