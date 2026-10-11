# StockLab

**StockLab** 是一个本地化的 A 股量化分析平台，提供完整的股票数据获取、分析和可视化功能。

## 📋 项目概述

StockLab 采用严格的 **分层架构** 设计，遵循单向依赖原则：

```
datasource (只出不进) ↔ persistence (只进不出) → 互不 import
facade → datasource + persistence ← 两者绝不反向 import
analytics / factor / screener / backtest / fundamental → 纯计算，零层内依赖，只吃 DataFrame
```

**核心模块**：
- **datasource**: 远端数据取数（AkShare、BaoStock、腾讯、新浪、通达信）
- **persistence**: DuckDB 本地持久化存储
- **facade**: 统一数据访问入口（7 门面 + 1 归因引擎）
- **analytics**: 纯统计变换（估值分位、风格分析、归因报告）
- **factor**: 27 个因子 + 预处理
- **backtest**: 回测引擎（T+1、成本三段、涨跌停推导）
- **screener**: 结构化筛选器

## 🚀 快速开始

### 环境要求

- Python **3.14.4**
- 虚拟环境（推荐使用 `venv`）
- 系统依赖：见 `requirements.txt`

### Linux/macOS

```bash
# 安装依赖
./run.sh install

# 运行 Web 分析服务 (含安装) → http://127.0.0.1:8000
./run.sh run

# 运行单元测试
./run.sh test

# 全量同步核心数据
./run.sh sync
```

### Windows

```cmd
REM 安装依赖
run.bat install

REM 运行 Web 分析服务 (含安装) → http://127.0.0.1:8000
run.bat run

REM 运行单元测试
run.bat test

REM 全量同步核心数据
run.bat sync
```

### 手动命令（不使用 run.sh/run.bat）

```bash
# 安装依赖
./venv/bin/python -m pip install -r requirements.txt

# 运行 Web 分析服务
./venv/bin/python app/scripts/serve_web.py

# 运行离线测试（默认，约 1061 用例 / 22s）
./venv/bin/python -m pytest

# 仅运行单元测试（非 integration）
./venv/bin/python -m pytest -m "not integration"

# 全量同步核心数据
./venv/bin/python app/scripts/sync_all.py

# 仅同步日 K（增量）
./venv/bin/python app/scripts/sync_market_data.py prices --incremental
```

## 🧪 测试说明

- **单元测试**: `./venv/bin/python -m pytest`（默认排除 integration 测试）
- **集成测试**: `./venv/bin/python -m pytest -m integration`（4 个用例）
- **测试覆盖率**: 语句 86% / 分支 76%，合并口径门槛 `--cov-fail-under=83`
- **关键坑点**:
  - Web 测试前必须执行: `pkill -f serve_web.py`
  - `pytest.ini` 必须留在项目根目录（放入 `tests/` 会导致配置不生效）

## 📁 项目结构

```
StockLab/
├── app/                     # 入口脚本与 UI
│   ├── scripts/            # CLI 入口脚本
│   ├── web/                # Flask Web 服务
│   └── dashboard/          # 板块走势 HTML 生成
├── stocklab/               # 主源代码
│   ├── common/            # 配置/HTTP/类型转换
│   ├── domain/            # 列契约 + 异常（叶子包）
│   ├── normalization/     # 源表→契约帧改写点
│   ├── datasource/        # 远端取数
│   ├── persistence/       # DuckDB 落库
│   ├── facade/            # 统一取数入口
│   ├── analytics/         # 纯统计变换
│   ├── factor/            # 27 因子
│   ├── screener/          # 结构化筛选
│   ├── backtest/          # 回测引擎
│   └── research/          # 研究编排
├── tests/                  # 1061 个测试用例
├── configs/                # insight_sources.json / screen_value.json
├── data/                   # stocklab.duckdb（本地数据仓库）
├── requirements.txt        # 依赖精确版本锁
├── config.ini             # 核心配置文件
├── run.sh                  # Linux/macOS 统一入口
├── run.bat                 # Windows 统一入口
└── AGENTS.md              # Agent 使用规则
```

## ⚠️ 关键约束

请参考 `AGENTS.md` 中的详细规范，主要包括：

1. **分层单向依赖**: 严格遵守各层级导入关系
2. **取数唯一入口**: 所有数据获取必须通过 `facade` 层
3. **配置只在入口读**: `config.ini` 仅在脚本 `main()` 调用
4. **DuckDB 单写连接**: `serve_web.py` 独占写连接
5. **编码规范**: 无装饰器、无生成器嵌套、无元类等
6. **测试纪律**: 三层 fixture，标记系统

## 🔧 开发环境设置

```bash
# 1. 安装环境
./run.sh install

# 2. 验证环境
./venv/bin/python -c "import stocklab; print('StockLab OK')"

# 3. 常用开发命令
./venv/bin/python -m pytest           # 运行测试
./run.sh test                         # 使用 run.sh 运行测试

# 4. 如需联网同步
export STOCKLAB_XUEQIU_COOKIE="your_cookie"
export STOCKLAB_GUBA_COOKIE="your_cookie"
```

## 📄 文档导航

- `ARCHITECTURE.html` — 数据源容错、Facade 路由、分析计算层设计
- `ANALYTICS.html` — PE 分布统计、历史估值分位技术规格
- `DATABASE.html` — 表设计、关联、字段含义
- `数据源清单.md` — 接口清单与降级策略

> **重要**: 代码与文档冲突时，请信代码（可执行的才是真理）。