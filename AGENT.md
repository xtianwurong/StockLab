# StockLab

A 股核心板块行业 ETF 与主板基准的长周期（默认 10 年）月线走势分析工具。
抓取各赛道纯正行业 ETF 与上证指数的前复权月线数据，清洗对齐后渲染为**自包含的交互式 HTML 网页**（ECharts 图表，单文件、可离线打开、可直接分享）。

**数据全部来自公开接口。** 板块走势网页直接实时抓取，不落地任何中间文件；同时项目内置一套**本地 DuckDB 数据仓库**（可选路径），用于沉淀全市场 A 股的日 K 与估值数据，详见「数据库设计（DuckDB）」章节。

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

## 目录结构```
StockLab/
├── .gitignore                          # Git 忽略规则（__pycache__ / venv / output / data / .workbuddy）
├── AGENT.md                            # 本文档（项目永久上下文与设计契约）
├── config.ini                          # 运行配置：月数、输出路径、超时、基准与板块清单
├── requirements.txt                    # 运行依赖（版本用 == 锁定）
├── stocklab/                           # ── 核心库包 ──
│   ├── __init__.py                     #   顶层公共 API 汇总（12 项 __all__ 收敛对外暴露面）
│   ├── common/                         #   通用基础层
│   │   ├── __init__.py                 #     导出 safe_float / safe_int / load_ini_config
│   │   ├── config.py                   #     config.ini 解析与逐级向上查找
│   │   └── type_conversion.py          #     safe_float / safe_int 类型安全转换
│   ├── datasource/                     #   数据源接入层（只负责「从外部取数」）
│   │   ├── __init__.py                 #     导出个股服务与腾讯网关的公共 API
│   │   ├── tencent_client.py           #     【腾讯直连行情网关】TencentMarketClient（股票/ETF/指数统一接入）
│   │   ├── stock_data.py               #     【个股多源数据服务】MarketDataService（三级容错 + 估值独立降级）
│   │   └── market_provider.py          #     【全市场数据 Provider】MarketDataProvider（全市场批量取数）
│   ├── persistence/                    #   本地数据持久化层（只负责「往本地存数」）
│   │   ├── __init__.py                 #     本层统一出口（Database + 三个 Repository）
│   │   ├── storage/                    #     数据存储基础设施
│   │   │   ├── __init__.py             #       导出 Database / initialize_database
│   │   │   ├── duckdb.py               #       DuckDB 连接管理（Database 类，支持 with）
│   │   │   └── schema.py               #       DDL 唯一定义与 initialize_database()
│   │   └── repository/                 #     数据访问层（表级 SQL 封装）
│   │       ├── __init__.py             #       导出三个 Repository
│   │       ├── security.py             #       reference.securities 读写
│   │       ├── daily_price.py          #       market.daily_prices 读写
│   │       └── daily_valuation.py      #       market.daily_valuations 读写
│   ├── facade/                         #   统一数据取数门面层（位于 datasource 与 persistence 之上）
│   │   ├── __init__.py                 #     导出 MarketDataFacade
│   │   └── market_data.py              #     MarketDataFacade：本地/远端优先级路由与自动回退
│   └── visualizer/                     #   可视化 / Web 呈现层
│       ├── __init__.py                 #     导出编排类与网页生成类
│       ├── sector_trend.py             #     SectorTrendVisualizer 端到端编排
│       └── page_generator.py           #     SectorWebPageGenerator 模板填充 → HTML
├── scripts/                            # ── 入口脚本（只做参数解析 + 调用库）──
│   ├── generate_sector_trend.py        #     命令行入口：生成板块走势网页
│   └── sync_market_data.py             #     命令行入口：全市场数据同步到本地 DuckDB
├── tests/                              # ── 自检脚本 ──
│   └── test_data_interfaces.py         #     全链路自检（类型转换 / 跨资产行情 / 实时快照 / 简称 / 月线估值 / 网页生成）
├── templates/                          # ── 静态资源 ──
│   └── dashboard.html                  #     网页模板（占位符 __DATA_PAYLOAD__ 由数据替换）
├── data/                               # ── 以下均为运行时生成，已被 .gitignore 排除 ──
│   └── stocklab.duckdb                 #     本地 DuckDB 单文件数据库
├── output/                             # ── 同上 ──
│   └── sector_etf_trend.html           #     自包含交互式网页产物
└── venv/                               # ── 同上（Python 虚拟环境）──
```

## 分层与依赖方向

```
入口脚本 (scripts/)            ← 取数与落库的唯一编排方
        │
        ├──►  stocklab.visualizer  ──►  stocklab.datasource  ──►  stocklab.common
        │                                  （只对外取数）
        │
        ├──►  stocklab.facade  ──┬──►  stocklab.datasource
        │        （统一取数入口）  └──►  stocklab.persistence
        │
        └──►  stocklab.persistence  ──►  stocklab.persistence.storage
                                       （只对本地落库；零内部依赖）
```

| 包 | 依赖的 StockLab 包 | 第三方库 | 标准库 |
|----|------------------|---------|--------|
| `stocklab.common` | 无 | 无 | `configparser` `logging` `math` `os` `types` |
| `stocklab.datasource` | `common`（层内互引 `datasource`） | `akshare` `baostock` `pandas` `requests` | `concurrent.futures` `contextlib` `datetime` `io` `logging` `time` |
| `stocklab.persistence` | 无（仅层内 `persistence.storage`） | `duckdb` `pandas` | `logging` `os` |
| `stocklab.facade` | `common` `datasource` `persistence` | `pandas` | `logging` |
| `stocklab.visualizer` | `common` `datasource`（层内互引 `visualizer`） | 无 | `datetime` `json` `logging` `os` |

> `stocklab.persistence` **不依赖 `common`**：持久化层无配置语义，解析 `config.ini` 对它没有意义。

- `stocklab/common`：无业务依赖的通用工具（配置解析、类型转换）。
- `stocklab/datasource`：**只负责对外取数**，不感知本地存储。
  - `tencent_client.py`：腾讯直连行情网关，统一接入股票/ETF/指数。
  - `stock_data.py`：单股多源数据服务（三级容错策略 + 估值对齐）。
  - `market_provider.py`：全市场批量数据获取。
- `stocklab/persistence`：**只负责本地落库**，既不依赖 `common`，也不依赖任何外部数据源（AkShare / BaoStock / 腾讯）。
  - `storage/`：DuckDB 连接管理与 Schema 定义。
  - `repository/`：SQL 读写封装，仅依赖 pandas 与本层 `storage/`。
- `stocklab/facade`：**统一取数入口**，同时依赖 `datasource` 与 `persistence`，负责按优先级在两者间路由与回退。
- `stocklab/visualizer`：把数据渲染成网页。
- **分层命名契约**：
  - `datasource`（data source，只出不进）与 `persistence`（data sink，只进不出）是两个平行关注点，取数与落库的调用方是 `facade` 或入口脚本，**两层之间不得互相 import**；
  - `facade` 可依赖两者，但 **`datasource` 与 `persistence` 绝不可反向 import `facade`**，否则形成循环依赖。
- 顶层入口脚本只做「参数解析 + 调用库」，不含业务逻辑；自检脚本放在 tests/ 下。

## 数据源架构

**腾讯直连市场数据客户端（`stocklab/datasource/tencent_client.py` - `TencentMarketClient`）**：
- 抹平资产类型差异，完全统一支持 A 股个股、行业/宽基 ETF 以及大盘指数。
- 提供 `fetch_kline` (全要素 OHLCV)、`fetch_monthly_close` (月末收盘序列)、`fetch_multi_monthly_close` (多标的并发抓取)、`fetch_name` (名称查询) 与 `fetch_quote_fields` (盘口原始字段列表，`~` 分隔，供上层解析)。

**单股深度数据与估值（`stocklab/datasource/stock_data.py` - `MarketDataService`）** 三级容错，价格与 PE 各自独立降级：

| 数据 | 主 | 备 |
|------|----|----|
| 月线收盘价 | AkShare（东方财富） | BaoStock → 腾讯 (TencentMarketClient) |
| PE-TTM | AkShare（百度股市通） | BaoStock（日线降采样） |
| 公司简称 | 腾讯直连 (TencentMarketClient) | AkShare → BaoStock |

> `stock_data.py` 内部组合复用 `TencentMarketClient` 作为其腾讯直连与基础行情的底层驱动，实现职责分层并消除跨模块重复代码。

**全市场数据 Provider（`stocklab/datasource/market_provider.py` - `MarketDataProvider`）**：
- 负责全市场 A 股数据的批量获取，与单股数据服务明确边界。
- 提供 `fetch_securities` (股票基础信息)、`fetch_daily_prices` (日 K 行情)、`fetch_realtime_valuations` (最新估值快照)。

**统一数据取数门面（`stocklab/facade/market_data.py` - `MarketDataFacade`）**：

- 设计模式 **Facade（外观模式，GoF 结构型模式）**：为「本地 DuckDB 库 + 远端公开接口」这一子系统提供统一简化接口，对上层隐藏来源选择、命中判定与回退策略。
- > 「外观」指**对外的门面 / 入口**，与界面美观无关。
- **命名模式、不命名策略**：优先级不写进类名（若策略演变为「按新鲜度路由」或「加缓存中间层」，那是重构而非改名）。
- **cache-aside 回写**：远端取到的数据自动写回本地库，使后续查询命中本地。

| 优先级（`config.ini` `[data_source] priority`） | 行为 | 适用场景 |
|---|---|---|
| `local_first` | 先查本地库 → 未命中回退远端 → 远端结果回写本地 | 历史分析、批量回补，可容忍新鲜度差异 |
| `remote_first` | 先取远端 → 失败（返回空表）回退本地库 | 实时行情、最新估值快照，对新鲜度敏感 |

```python
from stocklab.facade import MarketDataFacade

with MarketDataFacade() as facade:          # 优先级取自 config.ini
    print(facade.priority)                  # 'local_first' 或 'remote_first'
    df = facade.fetch_daily_prices("600519.SH", "2025-01-01", "2026-09-30")
```

| 方法 | 说明 |
|------|------|
| `fetch_securities()` | 全市场证券基础信息；本地非空即命中，否则远端取并回写 |
| `fetch_daily_prices(ts_code, start_date, end_date)` | 单只日 K（不复权）；日期入参用 `YYYY-MM-DD`，转调远端时自动压缩为 `YYYYMMDD` |
| `fetch_valuations(trade_date=None)` | 全市场估值；`trade_date` 为空时取本地最新交易日，否则回退远端实时快照 |
| `priority` | 只读属性，返回当前生效的优先级 |
| `close()` / `with` | 释放本地数据库连接（支持上下文管理） |

> **优先级取值来源与容错**：显式构造参数 > `config.ini [data_source] priority` > 默认 `local_first`；配置文件缺失、段落缺失或取值非法时**记 warning 并回退默认值**，不会静默采用错误策略。合法取值由 `stocklab.common.config.DATA_SOURCE_PRIORITIES` 单点定义。

> ⚠️ **当前覆盖缺口（务必知悉）**：本地库仅覆盖 A 股个股（`reference.securities` 为 A 股全量），且 `market.daily_prices` 存的是**不复权日 K**。因此行业 ETF 与指数标的本地无记录、需要**前复权月线**的分析场景**必然回退远端**——对本项目主要消费方（板块走势图）命中率接近 0。补齐覆盖与口径前，回退分支是唯一被实际走到的路径。

**本地数据仓库（`stocklab/persistence/`）**：
- DuckDB 嵌入式分析数据库，存储全市场 A 股数据。
- 数据域分离：reference (证券身份)、market (行情估值)、sys (同步状态)。
- Repository 模式封装 SQL 读写，不依赖外部数据源。
- 核心表：`reference.securities`、`market.daily_prices`、`market.daily_valuations`、`sys.sync_tasks`。
- 对外统一出口：`from stocklab.persistence import Database, initialize_database, SecurityRepository, DailyPriceRepository, DailyValuationRepository`。

> **已知演进债（阶段二）**：`visualizer` 目前仍直连 `TencentMarketClient` 出图，尚未改走 `MarketDataFacade`，因此「两条并行数据通路」尚未合流。`MarketDataFacade` 已是合流的落点：待本地库补齐 ETF / 指数覆盖与前复权口径后，把 `sector_trend.py` 的取数改为经由 facade 即可收口。

**统一数据契约**：所有单股数据源标准化为三列常量 —— `TRADE_DATE_COLUMN` / `CLOSE_PRICE_COLUMN` / `PE_TTM_COLUMN`。

**数据载体类（`stocklab/__init__.py` 对外暴露的其余三类）**：

| 类 | 所在文件 | 设计模式 | 说明 |
|----|---------|---------|------|
| `StockDataFetchParams` | `datasource/stock_data.py` | 参数对象 (Parameter Object) | 收拢 `stock_code` / `adjust_type`（`qfq`/`hfq`/不复权）/ `retry_count` / `retry_interval_seconds`，后续新增控制项不改方法签名 |
| `StockRealtimeQuote` | `datasource/stock_data.py` | 简单实体类 (POD) | 18 个带默认初值的字段：现价、昨收、今开、最高、最低、涨跌额/幅、成交量/额、换手率、PE-TTM、PB、总/流通市值、行情时间；提供 `to_dict()` |
| `SectorWebPageGenerator` | `visualizer/page_generator.py` | 模板视图生成器 | 提取全标的月份并集 → 对齐缺失数据 → 填充 `dashboard.html` 的 `__DATA_PAYLOAD__` → 落盘单文件 HTML |

`MarketDataService` 另有一个状态字段 `used_source_name`，记录本次价格查询实际命中的数据源标识（如 `akshare` / `baostock` / `tencent`），供上层日志展示。

## 数据库设计（DuckDB）

**存储位置**：`data/stocklab.duckdb`（单文件嵌入式数据库，运行时自动创建，已被 `.gitignore` 排除）。
**DDL 唯一来源**：`stocklab/persistence/storage/schema.py`（全部 `CREATE ... IF NOT EXISTS`，重复初始化幂等安全）。
**连接管理**：`stocklab/persistence/storage/duckdb.py` —— `Database` 类（支持 `with` 上下文管理）；数据库文件不存在时自动重新初始化 Schema。

### 数据域（Schema）

| Schema | 职责 | 表 |
|--------|------|----|
| `reference` | 证券身份（慢变维表） | `securities` |
| `market` | 行情与估值（时间序列事实表） | `daily_prices`、`daily_valuations` |
| `sys` | 同步任务状态 | `sync_tasks` |

> **为什么是 `sys` 不是 `system`**：`system` 是 DuckDB 保留字，直接用作 schema 名会抛 `BinderException`。

### 表结构

#### `reference.securities` — 证券基础信息

主键：`ts_code`（如 `600519.SH`，`.SH/.SZ/.BJ` 后缀表达交易所）

| 列 | 类型 | 说明 |
|----|------|------|
| `ts_code` | VARCHAR | 标准证券代码（PK），由纯数字代码 + 交易所后缀归一化生成 |
| `symbol` | VARCHAR | 6 位纯数字代码 |
| `name` | VARCHAR | 证券简称（含 `*ST`、`XD` 等状态前缀，原样保留） |
| `exchange` | VARCHAR | 交易所：`SH` / `SZ` / `BJ` |
| `market` | VARCHAR | 市场：`SH` / `SZ` / `BJ` |
| `industry` | VARCHAR | 行业（当前数据源未提供，留空待增强） |
| `area` | VARCHAR | 地区（当前数据源未提供，留空待增强） |
| `list_date` | DATE | 上市日期（当前数据源未提供，NULL） |
| `delist_date` | DATE | 退市日期（NULL） |
| `list_status` | VARCHAR | 上市状态，当前统一写 `L` |
| `is_hs` | VARCHAR | 沪深港通标记（当前数据源未提供，留空） |

当前数据规模：**5572 只**（SH 2320 / SZ 2904 / BJ 348）。

#### `market.daily_prices` — 日 K 行情（不复权）

主键：`(ts_code, trade_date)`

| 列 | 类型 | 说明 |
|----|------|------|
| `ts_code` | VARCHAR | 证券代码 |
| `trade_date` | DATE | 交易日期 |
| `open` / `high` / `low` / `close` | DOUBLE | 开 / 高 / 低 / 收（不复权价，`adjust=""`） |
| `pre_close` | DOUBLE | 昨收 |
| `change` | DOUBLE | 涨跌额 |
| `pct_chg` | DOUBLE | 涨跌幅（%） |
| `volume` | DOUBLE | 成交量 |
| `amount` | DOUBLE | 成交额 |

> 价格类型固定为**不复权**；复权价如需计算在查询层处理，不在存储层冗余。

#### `market.daily_valuations` — 每日估值

主键：`(ts_code, trade_date)`，16 列与 `MarketDataProvider.fetch_realtime_valuations()` 输出**严格同名同序**（UPSERT 用 `SELECT *` 按位置匹配，列序即契约）。

| 列 | 类型 | 说明 |
|----|------|------|
| `ts_code` | VARCHAR | 证券代码 |
| `trade_date` | DATE | 交易日期（快照写入日） |
| `turnover_rate` | DOUBLE | 换手率（%） |
| `turnover_rate_f` | DOUBLE | 自由流通换手率（当前数据源未提供 → NULL） |
| `pe` | DOUBLE | 市盈率（动态） |
| `pe_ttm` | DOUBLE | 市盈率 TTM |
| `pb` | DOUBLE | 市净率 |
| `ps` / `ps_ttm` | DOUBLE | 市销率 / 市销率 TTM（当前数据源未提供 → NULL） |
| `dv_ratio` / `dv_ttm` | DOUBLE | 股息率 / 股息率 TTM（当前数据源未提供 → NULL） |
| `total_share` / `float_share` / `free_share` | DOUBLE | 总股本 / 流通股本 / 自由流通股本（当前数据源未提供 → NULL） |
| `total_mv` | DOUBLE | 总市值（元） |
| `circ_mv` | DOUBLE | 流通市值（元） |

> **NULL 语义红线**：估值字段 NULL 表示亏损或无数据，**严禁**强制转换为 0；数据源不提供的列写 `NaN`（落库即 NULL），绝不省略列——省略会导致 UPSERT 列数不匹配而整体失败。

#### `sys.sync_tasks` — 同步任务状态

| 列 | 类型 | 说明 |
|----|------|------|
| `task_id` | BIGINT | 任务序号 |
| `data_type` | VARCHAR | 数据类型（securities / prices / valuations） |
| `start_date` / `end_date` | DATE | 同步日期范围 |
| `status` | VARCHAR | 状态（running / success / failed） |
| `row_count` | BIGINT | 写入行数 |
| `started_at` / `finished_at` | TIMESTAMP | 起止时间 |
| `error_message` | VARCHAR | 失败原因 |

> 当前为预留表：**尚无代码写入**（断点续传暂由 `daily_prices.get_max_trade_date()` 实现），后续如需任务级审计再接入。

### 写入语义（Repository 层）

- **批量 UPSERT**：先将 pandas DataFrame 注册为临时表，再 `INSERT INTO ... SELECT * FROM 临时表 ON CONFLICT (...) DO UPDATE SET ...`，单语句原子提交，**绝不逐行插入**。
- **幂等性**：同一批数据重复写入行数不变；中断后重跑不会产生重复记录。
- **主键冲突键**：`securities` 冲突于 `ts_code`；`daily_prices` / `daily_valuations` 冲突于 `(ts_code, trade_date)`。
- **依赖方向**：Repository 只依赖 `persistence.storage` 与 pandas，**不 import 任何数据源**（AkShare / BaoStock / 腾讯），保证数据访问层可独立测试与移植。

### Repository 公共接口

| 类 | 方法 | 说明 |
|----|------|------|
| `SecurityRepository` | `upsert(df)` / `find_all()` / `find_by_code(ts_code)` / `count()` | 证券信息读写 |
| `DailyPriceRepository` | `upsert(df)` / `find_by_code(ts_code, start_date, end_date)` / `find_by_date(trade_date)` / `get_max_trade_date()` / `count()` | 日 K 读写；`get_max_trade_date()` 供增量同步定位断点 |
| `DailyValuationRepository` | `upsert(df)` / `find_by_code(...)` / `find_by_date(trade_date)` / `count()` | 估值读写 |

### 使用约束

- **单写者**：DuckDB 文件级锁，同一时刻只允许一个读写连接——同步运行前必须关闭打开该文件的 duckdb CLI / 其他进程，否则抛 `IOException: Could not set lock`。
- **只读查询**：临时只读分析可先复制文件再 `duckdb.connect(path, read_only=True)`，避免与写入抢锁。
- **列序契约**：`daily_valuations` 等表的 UPSERT 依赖 DataFrame 列序与表定义一致，修改任一侧必须同步修改另一侧（建议改用显式列名清单前先补回归测试）。

## 运行方式

```bash
# 生成板块走势网页（默认读取 config.ini）
./venv/bin/python scripts/generate_sector_trend.py

# 自定义月数与输出路径
./venv/bin/python scripts/generate_sector_trend.py --months 60 --output output/trend_5y.html

# 指定其他配置文件
./venv/bin/python scripts/generate_sector_trend.py --config config.ini

# 全链路自检（可选股票代码，默认 000001.SZ）
./venv/bin/python tests/test_data_interfaces.py 000001.SZ

# 同步全市场数据到本地 DuckDB（不带子命令 = 一键全跑三个阶段）
./venv/bin/python scripts/sync_market_data.py --start-date 2025-01-01 --end-date 2026-09-30

# 增量同步（自动从最新交易日期同步到当前）
./venv/bin/python scripts/sync_market_data.py --incremental

# 三个阶段分开执行（子命令，公共参数可在子命令前后任意位置）
./venv/bin/python scripts/sync_market_data.py securities                 # 阶段一：股票基础信息
./venv/bin/python scripts/sync_market_data.py prices --incremental       # 阶段二：日 K 行情
./venv/bin/python scripts/sync_market_data.py prices --start-date 1990-12-19   # 阶段二：全历史回补
./venv/bin/python scripts/sync_market_data.py valuations                 # 阶段三：估值快照

# 仅同步股票基础信息（兼容旧用法，等价于 securities 子命令）
./venv/bin/python scripts/sync_market_data.py --securities-only
```

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

## 配置说明（config.ini）

- `[settings]`：`default_months`（默认月数）、`output_html`（输出路径）、`http_timeout`
- `[data_source]`：`priority` —— 取数优先级，`local_first`（默认）/ `remote_first`；非法值回退 `local_first`
- `[benchmark]`：主板基准（默认上证指数，白色粗线）
- `[sectors]`：板块清单，每行格式 `标识 = 代码, 简称, 赛道, 颜色, 跟踪指数, 管理人, 纯度说明`
  - 在行首加 `#` 或 `;` 即可临时停用某个板块

## 输出

- `output/sector_etf_trend.html` —— 单文件自包含网页，含多标的月线走势对比图，可直接双击打开或分享。
- `data/stocklab.duckdb` —— 本地 DuckDB 数据库，含全市场 A 股日 K 行情与估值数据。

## 注意事项

1. 需要网络可用；数据来自公开接口。
2. 绘图由前端 ECharts 在浏览器内完成，Python 侧不产出图片。若将来引入服务端绘图，macOS 字体需指定 PingFang SC / Arial Unicode MS。
3. 腾讯接口返回 `~` 分隔长串，读取时编码必须设为 `gbk`。
4. DuckDB 的 `system` 是保留字，同步状态表使用 `sys` schema。
5. 全市场数据同步耗时较长（全历史回补约 5400 次请求），建议日常用 `--incremental`，或按交易所分批执行以控制失败影响面。
6. 估值字段（PE/PB 等）允许 NULL，表示亏损或无数据，不应强制转换为 0。
7. `output/` 与 `data/` 已被 `.gitignore` 排除；`templates/` 与 `config.ini` 则是入库的运行必需文件，删除后网页生成会失败。

## 许可证

MIT License —— 声明于本文档，**仓库内暂未附 LICENSE 文件**；正式对外分发前需补齐。
