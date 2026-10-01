# StockLab

A 股核心板块行业 ETF 与主板基准的长周期（默认 10 年）月线走势分析工具。
抓取各赛道纯正行业 ETF 与上证指数的前复权月线数据，清洗对齐后渲染为**自包含的交互式 HTML 网页**（ECharts 图表，单文件、可离线打开、可直接分享）。

**数据全部来自公开接口，不依赖任何本地数据文件。**

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

## 目录结构

```
StockLab/
├── scripts/                    # 辅助与入口脚本
│   ├── generate_sector_trend.py # 命令行入口：生成板块走势网页
│   └── sync_market_data.py     # 数据同步入口：全市场数据同步到本地 DuckDB
├── config.ini                  # 配置：月数、输出路径、基准与板块清单
├── AGENT.md                    # 本文档（项目永久上下文与设计契约）
├── templates/
│   └── dashboard.html          # 网页模板（占位符 __DATA_PAYLOAD__ 由数据替换）
├── output/                     # 生成的 HTML 产物（运行时自动创建）
├── data/                       # 本地 DuckDB 数据库（运行时自动创建，不入库）
├── tests/
│   └── test_data_interfaces.py # 数据接口测试（数据源 / 名称 / 实时行情 / 网页生成）
├── stocklab/                   # 核心库包
│   ├── common/                 # 通用基础层
│   │   ├── config.py           #   config.ini 解析
│   │   └── type_utils.py       #   safe_float / safe_int 类型安全转换
│   ├── datasource/             # 数据源接入层
│   │   ├── tencent_client.py   #   【腾讯直连行情网关】TencentMarketClient（统一接入股票/ETF/指数）
│   │   ├── stock_data.py       #   【个股多源数据服务】MarketDataService（三级容错策略 + 估值对齐）
│   │   ├── market_provider.py  #   【全市场数据 Provider】MarketDataProvider（全市场批量数据获取）
│   │   ├── storage/            #   数据存储层
│   │   │   ├── duckdb.py       #     DuckDB 连接管理
│   │   │   └── schema.py       #     Schema 定义与初始化
│   │   └── repository/         #   数据访问层
│   │       ├── security.py     #     securities 表读写
│   │       ├── daily_price.py  #     daily_prices 表读写
│   │       └── daily_valuation.py #  daily_valuations 表读写
│   └── visualizer/             # 可视化 / Web 呈现层
│       ├── page_generator.py   #   模板填充 → HTML
│       └── sector_trend.py     #   SectorTrendVisualizer 端到端编排
└── venv/                       # Python 虚拟环境（不入库）
```

## 分层与依赖方向

```
入口脚本 (scripts/)
        │
        ▼
stocklab.visualizer  ──►  stocklab.datasource  ──►  stocklab.common
                              │
                              ├── storage/    (DuckDB 连接管理 + Schema)
                              └── repository/ (SQL 读写封装)
```

- `stocklab/common`：无业务依赖的通用工具（配置解析、类型转换）。
- `stocklab/datasource`：对外数据获取与数据持久化。
  - `tencent_client.py`：腾讯直连行情网关，统一接入股票/ETF/指数。
  - `stock_data.py`：单股多源数据服务（三级容错策略 + 估值对齐）。
  - `market_provider.py`：全市场批量数据获取。
  - `storage/`：DuckDB 连接管理与 Schema 定义。
  - `repository/`：SQL 读写封装，不依赖外部数据源。
- `stocklab/visualizer`：把数据渲染成网页。
- 顶层入口脚本只做「参数解析 + 调用库」，不含业务逻辑；自检脚本放在 tests/ 下。

## 数据源架构

**腾讯直连市场数据客户端（`stocklab/datasource/tencent_client.py` - `TencentMarketClient`）**：
- 抹平资产类型差异，完全统一支持 A 股个股、行业/宽基 ETF 以及大盘指数。
- 提供 `fetch_kline` (全要素 OHLCV)、`fetch_monthly_close` (月末收盘序列)、`fetch_multi_monthly_close` (多标的并发抓取) 与 `fetch_name` (名称查询)。

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

**本地数据仓库（`stocklab/datasource/storage/` + `stocklab/datasource/repository/`）**：
- DuckDB 嵌入式分析数据库，存储全市场 A 股数据。
- 数据域分离：reference (证券身份)、market (行情估值)、sys (同步状态)。
- Repository 模式封装 SQL 读写，不依赖外部数据源。
- 核心表：`reference.securities`、`market.daily_prices`、`market.daily_valuations`、`sys.sync_tasks`。

**统一数据契约**：所有单股数据源标准化为三列常量 —— `TRADE_DATE_COLUMN` / `CLOSE_PRICE_COLUMN` / `PE_TTM_COLUMN`。

## 数据库设计（DuckDB）

**存储位置**：`data/stocklab.duckdb`（单文件嵌入式数据库，运行时自动创建，已被 `.gitignore` 排除）。
**DDL 唯一来源**：`stocklab/datasource/storage/schema.py`（全部 `CREATE ... IF NOT EXISTS`，重复初始化幂等安全）。
**连接管理**：`stocklab/datasource/storage/duckdb.py` —— `Database` 类（支持 `with` 上下文管理）；数据库文件不存在时自动重新初始化 Schema。

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
- **依赖方向**：Repository 只依赖 `storage/` 与 pandas，**不 import 任何数据源**（AkShare / BaoStock / 腾讯），保证数据访问层可独立测试与移植。

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
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple akshare baostock pandas requests duckdb
```

| 库 | 用途 |
|----|------|
| akshare | A 股月线 / PE / 公司信息（东方财富、百度股市通接口） |
| baostock | 备用历史行情与 PE |
| pandas | 数据清洗与时序对齐 |
| requests | 腾讯直连 HTTP（实时行情、公司名称、板块 K 线） |
| duckdb | 本地分析型数据仓库（全市场 A 股数据持久化） |

> numpy / matplotlib 为传递或历史依赖，当前代码不直接 import。

## 配置说明（config.ini）

- `[settings]`：`default_months`（默认月数）、`output_html`（输出路径）、`http_timeout`
- `[benchmark]`：主板基准（默认上证指数，白色粗线）
- `[sectors]`：板块清单，每行格式 `标识 = 代码, 简称, 赛道, 颜色, 跟踪指数, 管理人, 纯度说明`
  - 在行首加 `#` 或 `;` 即可临时停用某个板块

## 输出

- `output/sector_etf_trend.html` —— 单文件自包含网页，含多标的月线走势对比图，可直接双击打开或分享。
- `data/stocklab.duckdb` —— 本地 DuckDB 数据库，含全市场 A 股日 K 行情与估值数据。

## 注意事项

1. 需要网络可用；数据来自公开接口。
2. 涉及 matplotlib 图表时，macOS 字体使用 PingFang SC / Arial Unicode MS。
3. 腾讯接口返回 `~` 分隔长串，读取时编码必须设为 `gbk`。
4. DuckDB 的 `system` 是保留字，同步状态表使用 `sys` schema。
5. 全市场数据同步耗时较长，建议使用 `--incremental` 模式进行日常更新。
6. 估值字段（PE/PB 等）允许 NULL，表示亏损或无数据，不应强制转换为 0。

## 许可证

MIT License
