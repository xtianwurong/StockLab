"""
StockLab 数据库迁移包 (stocklab.persistence.migrations)

【模块职责】
   存放数据库结构迁移的 SQL 文件与执行器：
     - NNN_*.sql   迁移脚本（001 基线 = 机制上线前的既有结构，后续只增不改）
     - runner.py   SchemaMigrator：读 sys.schema_version 版本表 → 按序执行未应用迁移

【使用约定】
   - 数据库结构变更一律新增迁移文件，禁止修改 storage/schema.py；
   - initialize_database() 每次都会调用执行器，因此「启动即检查版本并升级」。
"""

from .runner import MIGRATIONS_DIR, SchemaMigrator

__all__ = [
    "SchemaMigrator",
    "MIGRATIONS_DIR",
]
