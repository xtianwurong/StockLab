# StockLab

A 股核心板块行业 ETF 与主板基准的长周期（默认 10 年）月线走势分析工具。抓取各赛道纯正行业 ETF 与上证指数的前复权月线数据，清洗对齐后渲染为**自包含的交互式 HTML 网页**（ECharts 图表，单文件、可离线打开、可直接分享）。

**数据全部来自公开接口。** 板块走势网页直接实时抓取，不落地任何中间文件；同时项目内置一套**本地 DuckDB 数据仓库**（可选路径），用于沉淀全市场 A 股的日 K、估值与指数成分数据。

> **文档导航：**
> - [`docs/ARCHITECTURE.html`](docs/ARCHITECTURE.html) — 数据源容错策略、Facade 路由机制、分析计算层设计
> - [`docs/ANALYTICS.html`](docs/ANALYTICS.html) — 市盈率分布统计、历史估值分位的技术规格
> - [`docs/DATABASE.html`](docs/DATABASE.html) — 数据库设计思想、表间关联、逐字段含义

---

## 编码规范（用户明确要求，必须遵守）

- Python 代码要**专业、模块化、可复用**：职责单一、接口清晰、便于移植到其他脚本。
- 用户是**资深 C++ 程序员**，对 Python 不熟但能看懂简单代码：
  - 不要使用不常用/高级语法（如装饰器、生成器表达式嵌套、元类、async、海象运算符、pattern matching 等）；
  - **不使用复杂的类型注解**：不使用 Python 的 `typing` 模块（禁止 `-> List[Dict[str, Any]]` 这类冗长返回值标注与参数类型注记），函数签名采用直白朴素的原生风格，参数类型与返回值统一在 docstring 的 Args / Returns 中清晰注明；
  - **严禁过度设计与兼容别名**：保持类名、方法名与文件命名唯一且准确，不保留冗余过渡文件或各种别名映射；
  - **类和对象可以使用**；
  - 写法尽量直白、贴近 C++ 风格，必要时加简短注释说明 Python 特有行为；
  - **import 一律集中在文件开头**，不在函数/方法中间 import；
  - **最小暴露原则**：库模块用 `__all__` 声明公共接口，内部实现一律 `_` 开头。注意 `_` 在 Python 里只是约定、不强制，靠 IDE/linter/自觉约束；
  - 架构与命名倾向于严谨的系统级设计，命名必须反映明确的职责与设计模式（如 Gateway、Pipeline、Adapter、Model），避免 Python 式的模糊命名（如 Manager/Utils/Helper）。
- **日志**：统一用标准库 `logging`；库模块只 `getLogger`，`basicConfig` 只在主程序 `main()` 里做；重试单次失败记 debug，耗尽才 warning；异常信息截断后再打印。

---

## 架构总览

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           用户入口层 (Scripts)                              │
│  ┌─────────────────┐  ┌─────────────────────────────────────────────────┐  │
│  │ sync_market_data│  │ generate_sector_trend.py / test_data_interfaces │  │
│  │ (数据同步 CLI)  │  │ (可视化生成 / 自测入口)                           │  │
│  └─────────────────┘  └─────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           外观门面层 (Facade)                                │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │ stocklab.facade.MarketDataFacade                                      │    │
│  │ - 统一「本地 DuckDB + 远端接口」取数入口                             │    │
│  │ - 策略：local_first / remote_first (config.ini 控制)                │    │
│  │ - Cache-Aside 模式：远端数据回写本地，后续命中本地库                  │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────────────┘
                    │                              │
         ┌──────────┴──────────┐       ┌──────────┴──────────┐
         ▼                     ▼       ▼                     ▼
┌──────────────────┐  ┌──────────────────┐  ┌──────────────────────────┐
│  数据源层        │  │  持久化层        │  │  通用行情客户端          │
│  (datasource)    │  │  (persistence)   │  │  (TencentMarketClient)   │
│                  │  │                  │  │  - 统一股票/ETF/指数接入 │
│ - AkShare(主)    │  │ - Repository 模式 │  │  - 并发批量抓取         │
│ - BaoStock(备)   │  │ - DuckDB 存储     │  │  - 实时快照 / K线 / 月线 │
│ - 腾讯(实时/备)  │  │ - Schema 管理     │  └──────────────────────────┘
└──────────────────┘  └──────────────────┘
         │                     ▲
         │                     │ (数据回写)
         └──────────┬──────────┘
                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           分析计算层 (Analytics)                             │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────────────┐  │
│  │ ValuationPercentile│ │ ValuationDistribu│  │ ...其他分析模块          │  │
│  │ Analyzer         │  │ tionAnalyzer     │  │                          │  │
│  │ - 历史估值分位 CDF│  │ - 全市场 PE 分布  │  │                          │  │
│  │ - 亏损期排除      │  │ - 中位数/分位/桶  │  │                          │  │
│  └──────────────────┘  └──────────────────┘  └──────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           报告渲染层 (Analytics Reporter)                    │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────────────┐  │
│  │ PercentileReporter│ │ MarkdownReporter │  │ ProfileReporter          │  │
│  │ - 控制台/MD 表格  │  │ - 归档级 MD 报告 │  │ - 纯文本控制台报告        │  │
│  └──────────────────┘  └──────────────────┘  └──────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           可视化呈现层 (Visualizer)                          │
│  ┌──────────────────┐  ┌──────────────────┐                                 │
│  │ SectorTrendVisualizer│ │ SectorWebPageGenerator │                       │
│  │ - 编排配置/数据/渲染 │  │ - 数据清洗对齐     │                               │
│  └──────────────────┘  └──────────────────┘                                 │
│                              │                                              │
│                              ▼                                              │
│                    ┌──────────────────┐                                    │
│                    │ dashboard.html   │  (ECharts 5 交互式图表)            │
│                    │ (模板 + JSON 注入)│                                    │
│                    └──────────────────┘                                    │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 分层与依赖方向

```
入口脚本 (app/scripts/)         ← 取数与落库的唯一编排方
         │
         ├──►  app.dashboard  ──►  stocklab.facade  ──►  stocklab.datasource
         │                                  （只对外取数）
         │
         ├──►  stocklab.facade  ──┬──►  stocklab.datasource
         │        （统一取数入口）  └──►  stocklab.persistence
         │
         ├──►  stocklab.analytics
         │        （纯统计变换：只吃 DataFrame，不碰网络/数据库，零层内依赖）
         │
         └──►  stocklab.persistence  ──►  stocklab.persistence.storage
                                        （只对本地落库；零内部依赖）
```

> `stocklab.analytics` **不 import `facade` / `datasource` / `persistence`**：它只接收调用方传入的
> DataFrame 做统计聚合，因此可脱离网络与数据库独立单测。取数仍由入口脚本经 facade 完成。

| 包 | 依赖的 StockLab 包 | 第三方库 | 标准库 |
|----|------------------|---------|--------|
| `stocklab.common` | 无 | `requests`（仅 `http_client` 补丁用） | `configparser` `logging` `math` `os` `types` |
| `stocklab.datasource` | `common`（层内互引 `datasource`） | `akshare` `baostock` `pandas` `requests` | `concurrent.futures` `contextlib` `datetime` `io` `logging` `time` |
| `stocklab.persistence` | 无（仅层内 `persistence.storage`） | `duckdb` `pandas` | `logging` `os` |
| `stocklab.facade` | `common` `datasource` `persistence` | `pandas` | `logging` |
| `stocklab.analytics` | 无（层内互引 `analytics`） | `pandas` | `logging` `os` `unicodedata` |
| `app.dashboard` | `common` `facade`（层内互引 `dashboard`） | 无 | `datetime` `json` `logging` `os` |

> `stocklab.persistence` **不依赖 `common`**：持久化层无配置语义，解析 `config.ini` 对它没有意义。

- `stocklab/common`：无业务依赖的通用工具（配置解析、类型转换、HTTP 全局配置）。
  - `http_client.py`：浏览器 UA 补丁，**由入口脚本显式调用**，导入本包不产生任何全局副作用。
- `stocklab/datasource`：**只负责对外取数**，不感知本地存储。
  - `tencent_client.py`：腾讯直连行情网关，统一接入股票/ETF/指数。
  - `stock_data.py`：单股多源数据服务（三级容错策略 + 估值对齐）。
  - `market_provider.py`：全市场批量数据获取。
- `stocklab/persistence`：**只负责本地落库**，既不依赖 `common`，也不依赖任何外部数据源（AkShare / BaoStock / 腾讯）。
  - `storage/`：DuckDB 连接管理与 Schema 定义。
  - `repository/`：SQL 读写封装，仅依赖 pandas 与本层 `storage/`。
- `stocklab/facade`：**统一取数入口**，同时依赖 `datasource` 与 `persistence`，负责按优先级在两者间路由与回退。
- `stocklab/analytics`：**纯统计变换层**，只接收 DataFrame 做聚合，不取数、不落库、不 import 上游三层。
- `app/dashboard`：把数据渲染成网页。
- **分层命名契约**：
  - `datasource`（data source，只出不进）与 `persistence`（data sink，只进不出）是两个平行关注点，取数与落库的调用方是 `facade` 或入口脚本，**两层之间不得互相 import**；
  - `facade` 可依赖两者，但 **`datasource` 与 `persistence` 绝不可反向 import `facade`**，否则形成循环依赖。
- 顶层入口脚本只做「参数解析 + 调用库」，不含业务逻辑；自检脚本放在 tests/ 下。

---

## 目录结构

```
StockLab/
├── .gitignore                          # Git 忽略规则（__pycache__ / venv / output / data / .workbuddy）
├── AGENT.md                            # 本文档（项目永久上下文与设计契约）
├── config.ini                          # 运行配置：月数、输出路径、超时、基准与板块清单
├── requirements.txt                    # 运行依赖（版本用 == 锁定）
├── docs/                               # ── 详细设计文档 ──
│   ├── ARCHITECTURE.html               #   数据源容错策略、Facade 路由机制
│   ├── ANALYTICS.html                  #   分析计算层技术规格
│   └── DATABASE.html                   #   数据库设计、表间关联、字段说明
├── stocklab/                           # ── 核心库包 ──
│   ├── __init__.py                     #   顶层公共 API 汇总（12 项 __all__ 收敛对外暴露面）
│   ├── common/                         #   通用基础层
│   │   ├── __init__.py                 #     导出 safe_float / safe_int / load_ini_config
│   │   ├── config.py                   #     config.ini 解析与逐级向上查找
│   │   ├── http_client.py              #     浏览器 UA 全局补丁（入口显式调用，导入无副作用）
│   │   └── type_conversion.py          #     safe_float / safe_int 类型安全转换
│   ├── datasource/                     #   数据源接入层（只负责「从外部取数」）
│   │   ├── __init__.py                 #     导出个股服务与腾讯网关的公共 API
│   │   ├── tencent_client.py           #     【腾讯直连行情网关】TencentMarketClient（股票/ETF/指数统一接入）
│   │   ├── stock_data.py               #     【个股多源数据服务】MarketDataService（三级容错 + 估值独立降级）
│   │   └── market_provider.py          #     【全市场数据 Provider】MarketDataProvider（全市场批量取数）
│   ├── persistence/                    #   本地数据持久化层（只负责「往本地存数」）
│   │   ├── __init__.py                 #     本层统一出口（Database + 五个 Repository）
│   │   ├── storage/                    #     数据存储基础设施
│   │   │   ├── __init__.py             #       导出 Database / initialize_database
│   │   │   ├── duckdb.py               #       DuckDB 连接管理（Database 类，支持 with）
│   │   │   └── schema.py               #       DDL 唯一定义与 initialize_database()
│   │   └── repository/                 #     数据访问层（表级 SQL 封装）
│   │       ├── __init__.py             #       导出 BaseRepository 与五个 Repository
│   │       ├── base.py                 #       BaseRepository：通用 UPSERT / 异常处理 / 日志模板
│   │       ├── security.py             #       reference.securities 读写
│   │       ├── daily_price.py          #       market.daily_prices 读写
│   │       ├── daily_valuation.py      #       market.daily_valuations 读写
│   │       ├── valuation_history.py    #       market.valuation_history 读写
│   │       ├── index_membership.py     #       reference.index_memberships 读写
│   │       └── industry_valuation.py   #       market.industry_valuations 读写
│   ├── facade/                         #   统一数据取数门面层（位于 datasource 与 persistence 之上）
│   │   ├── __init__.py                 #     导出 MarketDataFacade
│   │   └── market_data.py              #     MarketDataFacade：本地/远端优先级路由与自动回退
│   ├── analytics/                      #   统计分析层（纯变换，不取数不落库）
│   │   ├── __init__.py                 #     导出分析器 / 渲染器 / 口径常量
│   │   ├── valuation_distribution.py   #     ValuationDistributionAnalyzer：全市场市盈率分布统计
│   │   ├── valuation_percentile.py     #     ValuationPercentileAnalyzer：个股历史估值分位计算
│   │   ├── profile_reporter.py         #     ValuationDistributionReporter：统计报告文本渲染（控制台）
│   │   ├── markdown_reporter.py        #     ValuationDistributionMarkdownReporter：统计报告 Markdown 渲染（归档）
│   │   └── percentile_reporter.py      #     ValuationPercentileReporter：分位报告渲染（文本 + Markdown）
├── app/                               # ── 应用层（业务特定）──
│   ├── dashboard/                      #   仪表板模块
│   │   ├── __init__.py
│   │   ├── sector_trend.py            #     SectorTrendVisualizer 端到端编排
│   │   ├── page_generator.py          #     SectorWebPageGenerator 模板填充 → HTML
│   │   └── templates/
│   │       └── dashboard.html         #    网页模板（占位符 __DATA_PAYLOAD__ 由数据替换）
│   └── scripts/                       #   CLI 入口
│       ├── generate_sector_trend.py   #     命令行入口：生成板块走势网页
│       ├── sync_market_data.py        #     命令行入口：全市场数据同步到本地 DuckDB（五个阶段）
│       ├── analyze_pe_distribution.py #     命令行入口：全市场市盈率分布统计
│       └── analyze_valuation_percentile.py # 命令行入口：个股历史估值分位计算
├── tests/                              # ── 自检脚本 ──
│   └── test_data_interfaces.py         #     全链路自检（类型转换 / 跨资产行情 / 实时快照 / 简称 / 月线估值 / 网页生成）
├── data/                               # ── 以下均为运行时生成，已被 .gitignore 排除 ──
│   └── stocklab.duckdb                 #     本地 DuckDB 单文件数据库
├── output/                             # ── 同上 ──
│   └── sector_etf_trend.html           #     自包含交互式网页产物
└── venv/                               # ── 同上（Python 虚拟环境）──
```

---

## 核心模块职责

| 层级 | 模块 | 核心职责 |
|------|------|----------|
| **配置** | `stocklab.common.config` | 解析 `config.ini`，输出强类型配置对象（基准、板块、优先级） |
| **基础工具** | `stocklab.common.type_conversion` | `safe_float` / `safe_int` —— 统一处理 `None/空串/占位符/-` |
| **数据源** | `stocklab.datasource.stock_data` | **单股核心服务**：三通道降级、月线价格+PE对齐、实时行情 |
| | `stocklab.datasource.tencent_client` | **通用行情客户端**：股票/ETF/指数不区分、并发批量、OHLCV全要素 |
| | `stocklab.datasource.market_provider` | **全市场批量 Provider**：基础信息/日K/估值快照/历史估值/行业估值/指数成分/公司概况 |
| **外观** | `stocklab.facade.market_data` | **统一取数门面**：本地优先/远端优先策略、Cache-Aside 回写 |
| **持久化** | `stocklab.persistence.storage.schema` | DuckDB DDL 定义：7 张表、3 个 Schema、复合主键 |
| | `stocklab.persistence.storage.duckdb` | 连接管理：延迟初始化、上下文管理器 |
| | `stocklab.persistence.repository.*` | 表级 Repository：继承 `BaseRepository` 统一 UPSERT / 异常处理，按代码/日期查询 |
| **分析** | `stocklab.analytics.valuation_percentile` | **历史分位 CDF 口径**：排除亏损期、输出档位判定 |
| | `stocklab.analytics.valuation_distribution` | **全市场 PE 分布**：中位数/分位/固定语义分桶/交易所对比/极值榜单 |
| **渲染** | `stocklab.analytics.percentile_reporter` | 单股分位：控制台表格 + Markdown |
| | `stocklab.analytics.profile_reporter` | 全市场分布：纯文本报告（ASCII 条形图） |
| | `stocklab.analytics.markdown_reporter` | 全市场分布：归档级 Markdown（表格 + 自动结论） |
| **仪表板** | `app.dashboard.sector_trend` | 板块走势 Facade：配置→取数→HTML 编排 |
| | `app.dashboard.page_generator` | 模板渲染：月份并集对齐、JSON 注入 dashboard.html |
| **脚本** | `app/scripts/sync_market_data.py` | 5 阶段同步 CLI：证券/日K/估值快照/历史估值/指数成分 |
| | `app/scripts/generate_sector_trend.py` | 可视化生成 CLI：月数/输出路径/配置文件可配 |

---

## 运行方式

```bash
# 生成板块走势网页（默认读取 config.ini）
./venv/bin/python app/scripts/generate_sector_trend.py

# 自定义月数与输出路径
./venv/bin/python app/scripts/generate_sector_trend.py --months 60 --output output/trend_5y.html

# 指定其他配置文件
./venv/bin/python app/scripts/generate_sector_trend.py --config config.ini

# 全链路自检（可选股票代码，默认 000001.SZ）
./venv/bin/python tests/test_data_interfaces.py 000001.SZ

# 同步全市场数据到本地 DuckDB（不带子命令 = 一键全跑前三个阶段）
./venv/bin/python app/scripts/sync_market_data.py --start-date 2025-01-01 --end-date 2026-09-30

# 增量同步（自动从最新交易日期同步到当前）
./venv/bin/python app/scripts/sync_market_data.py --incremental

# 五个阶段分开执行（子命令，公共参数可在子命令前后任意位置）
./venv/bin/python app/scripts/sync_market_data.py securities                 # 阶段一：股票基础信息
./venv/bin/python app/scripts/sync_market_data.py prices --incremental       # 阶段二：日 K 行情
./venv/bin/python app/scripts/sync_market_data.py prices --start-date 1990-12-19   # 阶段二：全历史回补
./venv/bin/python app/scripts/sync_market_data.py valuations                 # 阶段三：估值快照
./venv/bin/python app/scripts/sync_market_data.py valuation-history --period 近五年  # 阶段四：历史估值序列
./venv/bin/python app/scripts/sync_market_data.py indexes                     # 阶段五：主流宽基指数成分

# 仅同步股票基础信息（兼容旧用法，等价于 securities 子命令）
./venv/bin/python app/scripts/sync_market_data.py --securities-only

# 个股历史估值分位（替代「处于低位/高位」这类无法复核的定性描述）
./venv/bin/python app/scripts/analyze_valuation_percentile.py 600519.SH

# 指定计算区间与当前值（当前值缺省取历史序列最新一日）
./venv/bin/python app/scripts/analyze_valuation_percentile.py 600519.SH \
    --start-date 2021-01-01 --pe-ttm 19.32

# 导出 Markdown
./venv/bin/python app/scripts/analyze_valuation_percentile.py 600519.SH \
    --markdown output/600519_percentile.md

# 统计 A 股全市场市盈率分布（PE-TTM 口径，默认仅打印到控制台）
./venv/bin/python app/scripts/analyze_pe_distribution.py

# 换口径 / 换取数通路
./venv/bin/python app/scripts/analyze_pe_distribution.py --pe-column dynamic
./venv/bin/python app/scripts/analyze_pe_distribution.py --priority remote_first

# 归档 Markdown 报告（长期留存、可被 grep 与其他文档引用）
./venv/bin/python app/scripts/analyze_pe_distribution.py --markdown output/pe_distribution.md

# 指定统计交易日并同时输出两种形态
./venv/bin/python app/scripts/analyze_pe_distribution.py \
    --trade-date 2026-09-30 --output output/pe_distribution.txt --markdown output/pe_distribution.md
```

---

## 典型使用场景

| 场景 | 入口 | 核心路径 |
|------|------|----------|
| **全量建库** | `python app/scripts/sync_market_data.py` | Provider → Repository → DuckDB |
| **增量同步日K** | `python app/scripts/sync_market_data.py prices --incremental` | Facade(local_first) → 本地库最大日期 → 补齐 |
| **历史估值分位(单股)** | Facade → ValuationHistoryRepo → Analyzer → Reporter | 本地序列 → CDF分位 → 控制台/MD |
| **全市场PE分布快照** | Facade → DailyValuationRepo → DistributionAnalyzer → MarkdownReporter | 单日横截面 → 中位数/分桶/极值 → 归档MD |
| **板块10年走势网页** | `python app/scripts/generate_sector_trend.py` | Facade → TencentClient(并发月线) → PageGenerator → dashboard.html |

---

## 环境与依赖

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

> 国内网络可用清华源加速：`pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt`
> `requirements.txt` 用 `==` 锁定版本（Python 3.14.4 / venv 环境实测对齐）。

| 库 | 版本 | 用途 |
|----|------|------|
| akshare | 1.18.97 | A 股月线 / PE / 公司信息（东方财富、百度股市通接口） |
| baostock | 0.9.4 | 备用历史行情与 PE |
| pandas | 3.0.6 | 数据清洗与时序对齐 |
| requests | 2.34.2 | 腾讯直连 HTTP（实时行情、公司名称、板块 K 线） |
| duckdb | 1.4.3 | 本地分析型数据仓库（全市场 A 股数据持久化） |

> `numpy` 与 `matplotlib` **不在 requirements.txt 中**，是 akshare / pandas 带入的传递依赖（实测环境：numpy 2.5.3、matplotlib 3.11.2）。全库无 `import numpy` / `import matplotlib`，绘图一律由前端 ECharts 在浏览器内完成。

---

## 配置说明（config.ini）

- `[settings]`：`default_months`（默认月数）、`output_html`（输出路径）、`http_timeout`
- `[data_source]`：`priority` —— 取数优先级，`local_first`（默认）/ `remote_first`；非法值回退 `local_first`
- `[benchmark]`：主板基准（默认上证指数，白色粗线）
- `[sectors]`：板块清单，每行格式 `标识 = 代码, 简称, 赛道, 颜色, 跟踪指数, 管理人, 纯度说明`
  - 在行首加 `#` 或 `;` 即可临时停用某个板块

---

## 输出

- `output/sector_etf_trend.html` —— 单文件自包含网页，含多标的月线走势对比图，可直接双击打开或分享。
- `data/stocklab.duckdb` —— 本地 DuckDB 数据库，含全市场 A 股日 K 行情与估值数据。
- 市盈率分布统计报告 —— 默认仅打印到控制台；`--output` 落盘纯文本，`--markdown` 落盘 Markdown 归档。

---

## 注意事项

1. 需要网络可用；数据来自公开接口。
2. 绘图由前端 ECharts 在浏览器内完成，Python 侧不产出图片。若将来引入服务端绘图，macOS 字体需指定 PingFang SC / Arial Unicode MS。
3. 腾讯接口返回 `~` 分隔长串，读取时编码必须设为 `gbk`。
4. DuckDB 的 `system` 是保留字，同步状态表使用 `sys` schema。
5. 全市场数据同步耗时较长（全历史回补约 5400 次请求），建议日常用 `--incremental`，或按交易所分批执行以控制失败影响面。
6. **`valuation-history` 阶段四不参与一键全跑**：单只标的需 5 个指标的独立 HTTP 请求，
   串行全市场约 7.5 小时。脚本已内置**并发取数**（默认 `--workers 8`，实测 12 仍稳定），
   全市场实测 **5572 只约 55 分钟**完成。
   > **并发只用于取数，落库必须串行**：DuckDB 连接非线程安全。
   > 脚本采用「工作线程池取数 → 主线程单连接 upsert」，既能并发加速又不引入数据库并发风险。
   > 新增并发逻辑时务必保持这个分工。
   日常按需单只调用 `analyze_valuation_percentile.py <代码>` 即可，它会自动「本地未命中 → 取远端 → 回写本地」。
6.1 **`indexes` 阶段五参与一键全跑**：仅 6 次请求、数秒完成，无副作用。
7. 估值字段（PE/PB 等）允许 NULL，表示亏损或无数据，不应强制转换为 0。
8. `output/` 与 `data/` 已被 `.gitignore` 排除；`templates/` 与 `config.ini` 则是入库的运行必需文件，删除后网页生成会失败。
9. **市盈率分布统计依赖 `market.daily_valuations` 有数据**。`local_first` 下只要本地快照非空即判定命中，
   即使只有个位数条数。代表性判定在库层（`is_representative()`，阈值 1000 只），
   报告表头会自动标注「分布结论不成立」，入口脚本再补充补救提示。
   首次使用先跑 `sync_market_data.py valuations`，或加 `--priority remote_first` 直接取远端快照。
10. 估值快照的 `trade_date` 是**写入快照的日期**，不一定是真实交易日；报告展示的日期取自实际样本的最大值。
11. **两个估值数据源的「亏损」表达方式不同，但分析口径等价**：
    - 东财（`stock_zh_a_spot_em`）对亏损股返回 `-`，`to_numeric(errors="coerce")` 后为 **NaN** → 计入「无有效 PE」；
    - 腾讯（`qt.gtimg.cn` 字段 39）对亏损股返回**负值**（如 ST嘉应 -703）→ 计入「PE ≤ 0」。
    两者都会被排除出分位数与分桶，但分别落在不同分类里，对比不同数据源的报告时需注意。
    > 实测回测：同一批 5572 只证券，负值形态与 NaN 形态下有效样本数与中位数完全一致。
12. **历史估值分位的样本数会小于交易日数**：差额即被排除的亏损期。
    例如金科股份 606 个交易日中 PE-TTM 仅 402 个有效样本（排除 204 个亏损期），
    这是刻意为之——亏损期不具备估值比较意义，计入分母会扭曲结论。
13. **巨潮 `stock_profile_cninfo` 已需授权**：实测返回
    `{"resultcode": 451, "resultmsg": "ApiFilter 未经授权的访问,code:003 token null"}`，
    HTTP 200 但数据为空。`MarketDataProvider.fetch_company_profile()` 已实现，
    在接口开放的环境可直接用于补齐 `securities.industry` / `list_date`；
    接口不可用时以 `reference.index_memberships` 的指数成分作为同业分组的替代维度。

---

## 许可证

MIT License —— 声明于本文档，**仓库内暂未附 LICENSE 文件**；正式对外分发前需补齐。
