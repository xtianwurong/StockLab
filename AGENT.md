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
│                           用户入口层 (Scripts + Web)                         │
│  ┌─────────────────┐  ┌─────────────────────────────────────────────────┐  │
│  │ sync_market_data│  │ generate_sector_trend.py / analyze_*.py         │  │
│  │ (数据同步 CLI)  │  │ serve_web.py (本地分析服务) / test 自检入口     │  │
│  └─────────────────┘  └─────────────────────────────────────────────────┘  │
│                     app.web：Flask 路由 + JSON 接口 + 浏览器端点击分析      │
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
         ├──►  app.web  ──┬──►  stocklab.facade      ──►  stocklab.datasource
         │   （HTTP 接口）  │                              stocklab.persistence
         │                 └──►  stocklab.analytics       （纯统计变换）
         │
         ├──►  stocklab.facade  ──┬──►  stocklab.datasource
         │        （统一取数入口）  │         │
         │                        │         └──►  stocklab.normalization（源表 → 契约帧）
         │                        └──►  stocklab.persistence
         │
         ├──►  stocklab.analytics
         │        （纯统计变换：只吃 DataFrame，不碰网络/数据库，零层内依赖）
         │
         ├──►  stocklab.fundamental      （财务指标纯派生：不取数、不落库）
         │
         ├──►  stocklab.factor ──►  stocklab.screener
         │        （因子与筛选纯计算：只吃 DataFrame，不碰网络/数据库）
         │
         ├──►  stocklab.backtest           （回测纯计算：只吃 DataFrame，不碰网络/数据库）
         │
         ├──►  stocklab.research ──┬──►  stocklab.persistence（查数 + 写研究快照）
         │        （研究编排层）      ├──►  stocklab.factor    （算因子）
         │                          └──►  stocklab.screener   （判条件）
         │
         └──►  stocklab.persistence  ──►  stocklab.persistence.storage
                （只对本地落库）                └──►  stocklab.persistence.migrations
```

> `stocklab.factor`（27 个因子 + 预处理）、`stocklab.screener`（规则/分组/流水线）与
> `stocklab.backtest`（引擎/组合/成本/指标/股票池）**只接收调用方传入的 DataFrame**：
> 因子算什么、条件怎么判、怎么撮合都与网络和数据库无关，可离线单测；
> 「取数」这一件事只由 `stocklab.research.frame` 承担，它按 Point-in-Time 拼出因子输入帧
> （每只股票一行），再交给因子层与筛选层。`stocklab.research` 是**编排层**：
> 允许同时依赖 persistence 与三个纯计算包，与 facade 同属「取数 / 编排」这一侧
> （回测结果是否落库、信号由谁拼，也都在编排层决定）。

> `stocklab.analytics` **不 import `facade` / `datasource` / `persistence`**：它只接收调用方传入的
> DataFrame 做统计聚合，因此可脱离网络与数据库独立单测。取数仍由入口脚本经 facade 完成。
>
> `stocklab.domain` 与 `stocklab.normalization` 是**共享叶子包**：`domain` 只放列契约与异常，
> `normalization` 只把源表帧改写成契约帧（不碰网络与数据库），两者的导入方可以是
> `datasource`、`persistence`、`analytics` 或 `fundamental`，但它们自己不得反向依赖任何一方。

| 包 | 依赖的 StockLab 包 | 第三方库 | 标准库 |
|----|------------------|---------|--------|
| `stocklab.common` | 无 | `requests`（仅 `http_client` 补丁用） | `configparser` `logging` `math` `os` `types` |
| `stocklab.domain` | 无 | `pandas` | `logging` |
| `stocklab.normalization` | `domain` | `pandas` | `logging` |
| `stocklab.datasource` | `common` `domain` `normalization`（层内互引 `datasource`） | `akshare` `baostock` `pandas` `requests` | `concurrent.futures` `contextlib` `datetime` `io` `logging` `time` |
| `stocklab.persistence` | `domain`（仅层内 `persistence.storage` / `persistence.migrations`） | `duckdb` `pandas` | `logging` `os` `datetime` `re` |
| `stocklab.facade` | `common` `datasource` `persistence` | `pandas` | `logging` |
| `stocklab.analytics` | 无（层内互引 `analytics`） | `pandas` | `logging` `os` `unicodedata` |
| `stocklab.fundamental` | `domain` | `pandas` | 无 |
| `stocklab.factor` | 无（层内互引 `factor`） | `pandas` | `math` |
| `stocklab.screener` | `factor` | `pandas` | 无 |
| `stocklab.backtest` | 无（层内互引 `backtest.order`） | `pandas` | 无 |
| `stocklab.research` | `domain` `factor` `screener` `persistence` | `pandas` | `datetime` `hashlib` `json` `logging` |
| `app.dashboard` | `common` `facade`（层内互引 `dashboard`） | 无 | `datetime` `json` `logging` `os` |
| `app.web` | `facade` `analytics`（层内互引 `web`） | `flask` | `logging` `os` `threading` |

> `stocklab.persistence` **不依赖 `common`**：持久化层无配置语义，解析 `config.ini` 对它没有意义。
> `datasource` 与 `persistence` 仍然互不 import：`normalization` 是两者的公共下游，
> `datasource` 产出契约帧后交由入口脚本/facade 写库，`persistence` 不知道数据来自哪里。

- `stocklab/common`：无业务依赖的通用工具（配置解析、类型转换、HTTP 全局配置）。
  - `http_client.py`：浏览器 UA 补丁，**由入口脚本显式调用**，导入本包不产生任何全局副作用。
- `stocklab/datasource`：**只负责对外取数**，不感知本地存储。内部按用途分三块：
  - **分析/行情取数**（单股维度、五级降级）：`quote_service.py`（`StockQuoteService`：月线价格 / PE-TTM / 简称 / 实时行情；价格通道 AkShare → BaoStock → 腾讯 → 新浪 → 通达信，估值通道 AkShare → BaoStock，实时通道 腾讯 → 新浪 → 通达信，简称通道 腾讯 → AkShare → BaoStock）→ `_sources/`（五个通道的私有实现，外部勿依赖）。
  - **入库取数**（多粒度、单源直连）：`market_service.py`（`MarketService`：7 个 `fetch_*` 方法与 7 张库表一一对应，输出**领域契约列**（`stocklab.domain`）而非「与表同序的 DataFrame」；粒度含全市场快照 / 单股序列 / 行业横截面 / 指数成分四种，直连各 akshare 接口（自带重试与限流），**不做通道降级**。归一化一律委托 `stocklab.normalization`，源表缺列时抛 `DataContractError` 并返回空表（绝不产出缺列帧）。调用方仅两个：`app/scripts/sync_market_data.py` 与 facade 远端分支（Cache-Aside 回写本地库）。
  - **逐只深度取数**：`fundamental_service.py`（`FundamentalService`：东财三大报表 + 财务指标，单只约 60 次请求，按报告期分批）、`lifecycle_service.py`（`LifecycleService`：沪深北上市日历与退市日历，只读交易所官网）。两者同样经 `stocklab.normalization` 产出契约帧，只被 `sync_market_data.py` 调用。
  - **公共基础**：
    - `data_contract.py`：行情数据契约（3 个列名常量 + `StockRealtimeQuote`），`quote_service` 与 `_sources` 共用；单独成文件是为避免「通道 import 服务、服务又 import 通道」的循环导入（类比 C++ 只含 struct + constexpr 的公共头文件）。
    - `tencent_client.py`：腾讯 HTTP 传输网关（`TencentMarketClient` + `normalize_symbol`），抹平股票/ETF/指数代码差异，提供 K 线 / 简称 / 盘口原始字段与多标的**并发**抓取。独立于 `_sources` 之外的原因：能力超出单股 `StockDataSource` 契约（ETF/指数 + 并发），且被两个上层独立复用——`_sources/tencent_source`（单股降级第三级，新浪/通达信分列四/五级）与 `facade.fetch_multi_monthly_close`（dashboard 板块走势 10 标的并发月线），故置于通道之下单独一层。
    - `cninfo_client.py`：巨潮资讯公告 Gateway（独立于行情链路），提供 `resolve_org_id()`、`get_announcements()`、`fetch_announcements()`，支持分页 / UTC+8 日期 / PDF 拼接 / 去重；**不继承 StockDataSource**。
- `stocklab/domain`：**列契约层（叶子包）**，只放「列名 + 列序元组」与 `DataContractError`，
  被 `datasource`、`persistence`、`normalization` 共同引用，自己不依赖任何 StockLab 包。
  - `contract.py`：`check_columns` / `require_columns` / `align_columns`（缺列抛异常、多余列丢弃并告警、按契约重排）。
  - `security.py` / `market_data.py` / `valuation.py` / `fundamental.py`：各表契约元组与状态/事件枚举。
- `stocklab/normalization`：**源表 → 契约帧的唯一改写点（叶子包）**，不取数、不落库。
  - `base.py`：`normalize_ts_code` / `pick_column` / `to_numeric_column` / `to_date_series` / `clean_text_value` / `clean_date_value` 等原子原语。
  - `akshare.py`：证券名录、日K、估值快照、历史估值、行业估值、指数成分、公司概况 7 个源格式归一化。
  - `exchange.py`：上市/退市日历归一化、`merge_lifecycle`（回填日期 + 推导 status + 补入退市股）、`build_lifecycle_events`。
  - `eastmoney.py`：东财三大报表 → PIT 帧（`available_date = announce_date`，有息负债口径在此定义）。
  > **构造帧必须用 `pd.DataFrame(dict)` / `pd.DataFrame([dict])`**：逐列赋值时若先给标量再给 Series，
  > 标量列会广播成全 NaN（曾导致日 K 的 `ts_code` 全 NaN、DuckDB 主键拒绝、整批静默写 0 行）。
- `stocklab/fundamental`：**财务指标纯派生**，由利润表 + 资产负债表算 ROE / ROA / ROIC / 毛利率 /
  净利率 / 同比，公告日取两表较晚者；不取数、不落库，可离线单测。
- `stocklab/factor`：**因子引擎（V2 §7），纯计算**，只吃 DataFrame。
  - `base.py`：`Factor(name, categories, columns, compute, description)` + `FactorDataError` +
    `divide` / `log_positive`；`categories` 是**元组**，`dividend_yield` 同时挂在 `value` 与 `dividend` 两个分类下（§7.1 与 §7.5 都列了它）。
  - `registry.py`：**显式 `register()` 登记（不用装饰器）**，`get` / `list_factors` / `compute`；
    本模块**不 import 分类包**（否则循环），登记动作由 `factor/__init__.py` 导入六个分类包完成；
    `FACTOR_VERSION = "factor_v1"`（快照里的因子版本号）。
  - `value/` `quality/` `growth/` `momentum/` `dividend/` `risk/`：六个分类包共 **27 个因子**；
    `preprocessing/`：winsorize / zscore / rank / missing / industry_neutralize / market_cap_neutralize（§7.6）。
  - **两种失败模式严格区分**：输入列缺失 = 帧构造方的缺陷 → 抛 `FactorDataError`；
    数据为 NaN = 「无数据」→ 筛选一律判不通过并在 `failed_rules` 里写明原因，绝不静默放行。
- `stocklab/screener`：**结构化筛选（V2 §8），纯计算**，不取数、不落库。
  - `rules.py`：`ScreenRule`（9 种操作符 `lt/le/gt/ge/eq/ne/in/isna/notna` 表驱动，除 `isna` 外
    缺失值一律判不通过；`explain()` 给出「满足 / 未满足 / 无数据」的逐行原因）、
    `ScreenGroup`（AND/OR 可嵌套；每条规则只评估一次，`evaluate` 算完再 `combine` 组合）、
    `ScreenError`（未知因子 / 非法阈值 / 非法组逻辑统一转成它，便于 CLI 一次报清）。
  - `pipeline.py`：`ScreenPipeline.from_spec(spec)` 与 `.spec()` **往返一致**（spec 与 §8.1 的 YAML
    结构同构；本项目收 JSON，因为环境未装 PyYAML）；执行顺序 = 算因子 → 并入工作帧 →
    **预处理(§7.6) → 判定**，所以阈值作用在处理后的值上；`ScreenResult.summary`（每只股票一行：
    ts_code / name / passed / failed_rules / 各因子值）+ `detail`（股票 × 规则长表：
    factor_value / threshold / passed / reason），正是 §8.2 要求的输出。
- `stocklab/backtest`：**回测层（V2 §9 / Phase 3），纯计算**，不取数、不落库。
  - `engine.py`：`BacktestEngine.run(prices, signals, dividends, benchmark)` —— 每个交易日固定四步：
    `unlock()`（T+1 解锁）→ 分红入账 → 撮合昨日委托（**先卖后买**，一律按收盘价）→ 计值并由当日信号下单；
    **成交日恒为信号日的下一个交易日**，「当天算的信号当天成交」在结构上不可能，输出 `BacktestResult`
    （权益曲线 / 成交 / 委托 / 期末持仓 / `metrics()`）。
  - `portfolio.py` + `order.py` + `trade.py`：`Cash` / `Position` / `Order` / `Trade`；
    `available` 就是 T+1 可卖数量——买入只加 `qty` 不加 `available`，`unlock()` 才解锁，卖不动即报错；
    平均成本**含买入费用**、卖出费用从已实现盈亏里扣（现金与权益对得上账）；
    委托只有 待撮合 / 已成交 / 已拒绝 三种归宿，**拒绝必须写明原因**。
  - `cost.py`：`CostModel` = 佣金（最低 5 元）+ 印花税（仅卖出）+ 滑点，三段分列不揉成一个费率。
  - `universe.py`：`universe_as_of(securities, as_of)` —— 与 `SecurityRepository.universe` **逐字同口径**
    的幸存者安全股票池（上市日 <= as-of 且退市日 > as-of，`list_date` 未知保守纳入）。
  - `metrics.py`：§9.3 要求的全部 11 个指标，统一按 **252 个交易日**年化；CAGR（几何）与
    Annual Return（算术）分列；**分母为 0（无波动 / 无下行 / 无回撤 / 无亏损交易）一律返回 NaN**，
    绝不用 inf 冒充「表现很好」。
  - 涨跌停 / 停牌：行情给 `limit_up` / `limit_down` / `suspended` 列就用它，否则由
    `pre_close × (1±limit_rate)`（按 0.01 取整）与「有行情但成交量为 0」推导；
    **买不进 / 卖不出的原因逐条写进 `orders.reason`**，退市持仓按最近价计值并在
    `equity.stale_count` 计数，绝不静默清零。
- `stocklab/research`：**研究编排层（V2 §6）**，因子与筛选唯一「查库 / 落库」的入口。
  - `frame.py`：`build_factor_frame(as_of_date, ts_codes, factor_names, extra_columns, database)` ——
    as-of 股票池（含 as-of 之后才退市的、排除已退市与后上市）、估值与基本面只读
    `available_date <= as-of` 的可见期、上年同期对照列、日线派生动量 / 波动率 / 最大回撤、
    `EV = 市值 + 有息负债 - 现金`；**没有数据源的输入列（ebitda / dps / dps_prior_year /
    dividend_years_*）一律按 NaN 落地并记 WARNING**，筛选命中时报「无数据」，绝不伪造。
  - `snapshot.py`：`create_snapshot` / `load_snapshot` / `list_snapshots` / `rerun_snapshot`
    （按快照 spec 重新生成结果并逐行比对，不一致时返回差异列表），以及 `config_version`
    （条件内容哈希）、`data_version`（`schema_vN@真实数据截止日`）、`factor_version`；
    **快照只写不改**，重复执行同一研究产生新的 `snapshot_id`，历史结论不被覆盖。
- `stocklab/persistence`：**只负责本地落库**，既不依赖 `common`，也不依赖任何外部数据源（AkShare / BaoStock / 腾讯）。
  - `storage/`：DuckDB 连接管理；`schema.py` 只做「委托迁移器」，**DDL 全部写在 `migrations/`**。
  - `migrations/`：`NNN_*.sql` 迁移文件 + `SchemaMigrator`（引导 `sys.schema_version`、按序补跑未应用迁移）。
  - `repository/`：SQL 读写封装，仅依赖 pandas、`domain` 与本层 `storage/`；写入一律经 `BaseRepository`
    契约对齐 + 显式列名 INSERT（不依赖 DataFrame 列序、不写 `SELECT *`）。
- `stocklab/facade`：**统一取数入口**，同时依赖 `datasource` 与 `persistence`，负责按优先级在两者间路由与回退。
- `stocklab/analytics`：**纯统计变换层**，只接收 DataFrame 做聚合，不取数、不落库、不 import 上游三层。
- `app/dashboard`：把数据渲染成网页。
- `app/web`：**本地 Web 分析服务**（Flask），把已有分析能力以 HTTP 接口暴露给浏览器，七个页面共用一套数据层与一套应用外壳：
  - **个股分析 `/`**：输入代码或中文名 → 投资仪表盘三屏：**结论区**（标的条 → 主指标大数 + 温度条 + 极值样本 → 多窗口分位；右侧并列**全市场横向位置**名次与分布直方图）→ **指标速览**（五张可点指标卡，取代原来重复的胶囊标签页）→ **走势图**（10/50/90 分位参考线与 25%~75% 分位带）→ **指标明细表**。
  - **全市场 Dashboard `/market`**：概览 → 分布 → 明细三段。筛选条（5 指标 / 市场 / 搜索 / 排序）→ 四格概览条 → 分位直方图与七档评级分布并排（点档位即钻取）→ 可排序分页排行表（点行跳个股页）。
  - **指数估值 `/indices`**：6 大宽基指数卡片（成分中位数口径）+ 点击展开走势图。
  - **行业估值 `/industries`**：国证行业分类 1~4 级横截面，PE 三种口径 + 规模数据，条形图 + 明细表（板块洼地判断）。
  - `server.py`：`create_app()` 装配层——**路由一律用 `app.add_url_rule()` 注册表写法，不用 `@app.route` 装饰器**（遵守本文件禁用装饰器的规定）。
  - `store.py`：**进程级数据访问单例**——(1) 单例门面 + 串行锁，修复「每请求重建门面反复抢写锁」；(2) 全市场窗口函数 SQL 与指数聚合，走本模块自己的单例连接 + 第二把锁，与门面锁互不嵌套；(3) 七档评级 `percentile_level()`、证券表与聚合结果的进程内缓存；(4) 行业估值横截面 `load_industry_valuation()` 与数据截止日期 `market_data_as_of()`。
  - `api.py`：个股接口层——只做「参数解析 → 代码/名称解析 → store 取数 → analyzer 计算 → 组装 JSON」，纯内存统计放在锁外；`/api/health` 额外返回 `data_as_of` 数据截止日期。个股页给的是**两个维度**的答案：`results` 是「相对它自己贵不贵」（走自选区间 + 可选当前值覆盖），`market_context` 是「相对别的股票贵不贵」（复用 /market 页的全市场分位，名次由分位升序位次现算）。两者**口径不同、不可直接比大小**，页面文案必须点明——全市场都在高估时，一只自身分位 20% 的股票仍可能是全市场最贵的一批。
  - `market_api.py`：全市场与指数接口层——参数校验 → store 聚合 → 过滤/排序/分页，不经门面锁；`/api/market/ranking` 支持 `level` 七档评级过滤（summary 仍按过滤前口径统计）与 `codes` **精确 ts_code 集合过滤**（与 `q` 的「代码或名称子串匹配」语义不同、不可互替），组合监控页据此一次拉回自选清单，**不新建接口**；`/api/industries` 行业横截面。
  - 组合监控（`/portfolio`，自选股 + 分位阈值告警）：**自选列表存浏览器 localStorage、不落服务端**——告警是「打开页面看一眼」的辅助，不是需要后台常驻的任务，落库反而要处理多用户与过期。全市场名次由分位升序位次**现算**（分位本身就是「严格低于当前值的样本占比」，两者同源，另查排名只是多扫一次全表）。数据仍走既有 `/api/market/ranking`，PE / PB 各请求一次后按 `ts_code` 合并。
  - `screener_api.py`：选股器接口（`/api/screener/meta` 因子覆盖率+算子+模板就绪度、`/api/screener/run` 执行）。**只做编排**：筛选委托 `stocklab.screener.ScreenPipeline`、取数委托 `stocklab.research.frame.build_factor_frame`；因子帧按 as-of 缓存；数据库连接向 `store.facade_database()` **借用门面那一个**（DuckDB 同文件只允许一个写连接，另建会抛 `Could not set lock`）。默认注入 `pe_ttm > 0` 剔除亏损股——否则「PE<15」会把 PE 为负的亏损股全放进来，与估值分位页「亏损期剔除」口径相矛盾。
  - `static/`：**三段式设计系统**，`tokens.css` → `components.css` → `base.css` 逐层只依赖上层变量，不写字面色值。
    - `tokens.css`（唯一取值来源）：亮/暗/跟随系统三套色板 + 间距/圆角/阴影/字号/动效/层级 + 8px 垂直韵律 `--rhythm-*`，保留全部历史变量名（`--mono`、`--topbar-height` 等改为 `var(--font-mono)` / `var(--topbar-h)` 别名）。**涨跌语义色单列一组**（`--rise/--fall/--flat`，A 股口径红涨绿跌），与七档估值色（绿=便宜→红=贵）方向相反，两套变量分属不同语义、不得混用。
    - `components.css`（组件唯一定义处）：按钮 / 表单 / 卡片 / 表格 / 徽章 / 标签页 / 消息条 / 名词解释 / 提示气泡 / 浮层（模态框 · 抽屉）/ Toast / 骨架屏 / 空态 / 分隔线 / 七档评级与温度条 / 概览指标条 / 联想下拉 / 数字排版 / 工具类 / 主题切换按钮。
    - `base.css`（外壳与版式）：左侧常驻导航 + 顶部工具条 + 独立滚动内容区、页面韵律（`.page-head` / `.section` / `.split-*` / `.grid-auto`）、布局工具（`.stack` / `.row` / `.spacer`）、页脚、动效降级、打印样式。
    - **两文件不得重复定义同一个类**：重构前有 47 条规则在两边各写一份、靠 `<link>` 顺序决定谁生效，导致「改了样式没反应」，已归一（`verify_css` 口径：顶层选择器全等才算重复，`@media` 内同名不算）。
    - **组件只收有真实调用方的**：删掉了「只有样式、全站找不到任何 JS 会生成它」的下拉菜单 / 时间线 / 进度条 / 键盘按键 / 单选复选框 / 滑块 / 输入框前后缀 / 表格条纹与固定列 / 筹码药丸 / 按钮 xs·lg·outline·success·danger·warning·group 等变体；反过来模态框与抽屉虽无页面调用，但 `SL.modal` / `SL.drawer` / `SL.confirm` 有完整实现并挂在 `window.SL` 上，属可用对外能力，样式必须留着。
    - **JS 分层**：`common.js`（请求/格式化/评级/图表 option，导出 `window.SL`）、`charts.js`（`SL.charts`：调色板 + 图表登记簿 + 主题切换重绘 + 可复用 option 片段）、`ui.js`（`SL.ui`：Toast/模态/抽屉/Tooltip/防抖节流/剪贴板/CSV 导出/URL 参数/快捷键 + **外壳装配** `initShell` / `initGlobalSearch`，在 DOM ready 时自动装配，全站无需各页调用）、`theme.js`（`SL.theme`：亮暗切换 + localStorage + `sl:themechange` 广播 + 快捷键 T），另加各页脚本（`app.js` / `market.js` / `indices.js` / `industries.js` / `screener.js` / `compare.js` / `portfolio.js`）；`templates/`：七个页面模板统一 `extends "_layout.html"`（**只覆盖 `page_title` / `page_css` / `content` / `page_scripts` 与 `active_page`**）+ `_sidebar.html`（**导航数据驱动，分「分析 / 估值 / 工具」三组，加页面只加一行**）+ `_topbar.html`（全局证券搜索 / 折叠 / 主题切换，**只放跨页面都常用的东西**，页面级筛选一律留在页面内部）+ `_footer.html`（口径说明 / 免责声明）。
  - **外壳常驻的意义**：七个页面切换时「我在哪、能去哪、当前数据多新」始终可见，不靠回忆；旧结构是顶栏平铺 7 个 `01~07` 序号链接，序号本身是噪音且切换后不保留任何上下文。
  - **页面私有样式仍写在各自模板的 `<style>` 里**（加载在 `base.css` 之后，可覆盖差异）；重构时必须保住各页 JS 依赖的全部 `id` 与类名（`tests/test_web_api.py` 有页面要素断言，另可用「模板 id ⊇ JS `getElementById`」自查）。
  - `compare_api.py`：多股对比接口（`/api/compare?codes=`，2~10 只 × 5 指标）。历史序列走 `store.load_valuation_histories`（一次 IN 查询，避免逐只走门面的取数优先级），分位走**与个股页同一个 `ValuationPercentileAnalyzer`**。两点硬约束：① 序列按各标的交易日**并集对齐**，缺失点填 `None` 让图上断线，**不做前向填充**（否则停牌日会被画成「价格没变」）；② 走势图分类色板**刻意避开绿/黄/红这一段语义轴**，与七档评级色零重叠——那套颜色读者已理解为「低估→高估」，拿来区分标的会误读成优劣。
  - **暗色主题链路**：`tokens.css` 是唯一定义处，`html[data-theme]` 切换；首屏防闪烁靠 `_layout.html` 里的内联脚本在 CSS 首绘前写 `data-theme` 与 `html.shell-collapsed`（侧边栏折叠态同样要在 CSS 前落定，否则折叠会闪一下）；ECharts 颜色一律经 `SL.charts.palette()` 读 CSS 变量，各页 `renderChart` 传 rebuild 回调，主题切换时由登记簿统一重绘（容器已移除则自动注销）；侧边栏折叠会改变可用宽度，`setShellCollapsed` 延迟 320ms 调 `SL.charts.redrawAll()` 让图表重排。
  - **依赖方向 `app.web → stocklab.facade / stocklab.analytics / stocklab.screener / stocklab.factor / stocklab.research`（`store.py` 另直接用 `duckdb` 做只读聚合；`screener_api.py` 直接调 `ScreenPipeline` / `build_factor_frame`，但只做参数解析与 JSON 组装，判定与取数逻辑一律留在 `stocklab/` 内），与 `app.dashboard` 平行，**不修改 `stocklab/` 核心库任何文件**。**
  - **快捷键**：`T` 切主题（亮 / 暗 / 跟随系统）、`[` 折叠或展开侧边栏、`/` 聚焦顶栏全局证券搜索、`R` 重跑（选股器 / 多股对比 / 组合监控）。全部由 `SL.ui.shortcut()` 统一登记，输入态自动让位（输入框里打字不触发）。
    **同一按键在全站只能有一个含义**：`SL.ui.shortcut` 按键位覆盖、后注册者胜出，所以页面脚本不得再注册已被外壳占用的键（选股器原本用 `/` 聚焦规则编辑，与顶栏全局搜索撞车，已删除——同键两义比少一个快捷键更糟）。
  - **七档评级仅存在于 Web 展示层**（`store.percentile_level` 与 `static/common.js` 的 `LEVEL7`，两侧口径由 `tests/test_web_api.py` 双向校验）；`stocklab.analytics` 内部仍是三档结论。
- **分层命名契约**：
  - `datasource`（data source，只出不进）与 `persistence`（data sink，只进不出）是两个平行关注点，取数与落库的调用方是 `facade` 或入口脚本，**两层之间不得互相 import**；
  - `facade` 可依赖两者，但 **`datasource` 与 `persistence` 绝不可反向 import `facade`**，否则形成循环依赖。
- **与 V2 需求文档的命名对照**：V2 §4.2 所称 `AkShareAdapter` / `BaoStockAdapter` / `TencentAdapter` 即本项目的 `datasource/_sources/{akshare,baostock,tencent}_source`（三个数据源通道实现）；`domain/corporate_action.py` 对应**尚未接入的公司行为（分红/送转）数据**——Phase 2 因子层已按「因子库先行、数据后补」交付，分红相关输入（`dps` / `dps_prior_year` / `dividend_years_*`）在因子输入帧里按 NaN 落地，`domain/corporate_action.py` 依旧**不预置空文件**，等真正接入公司行为数据源时再建。
- 顶层入口脚本只做「参数解析 + 调用库」，不含业务逻辑；自检脚本放在 tests/ 下。

### 测试架构（tests/）

- **三层装置，全在 `tests/conftest.py`**
  - `tmp_db_path` / `fresh_db` —— 临时 DuckDB。迁移前 5 个文件各手写一遍
    `tempfile.mkdtemp()` + `try/finally: shutil.rmtree()`，改一处漏一处就得手动排查。
  - `app` / `ctx` / `client` —— 真实库上的 Flask 应用与测试客户端。`client` 自动带
    应用上下文（`store` 的只读聚合依赖 `current_app`）。迁移前是模块级 `_APP` 全局
    + `main()` 里手动 `create_app()`，app_context 边界只存在于 `main()` 里，
    **单跑任何一个用例必然 `RuntimeError: Working outside of application context`**。
  - `http_reachable` —— 外网探测。断网时整组 `integration` 用例 skip，
    而不是逐个超时几十秒。
  - `app` fixture 会**主动检测 `serve_web.py` 是否在跑**（DuckDB 同文件只允许一个写
    连接），并给出可操作的报错，而不是抛一句看不懂的 `Could not set lock`。

- **marker 分两类**（`pytest.ini` 里的 `--strict-markers` 会让拼错的 marker 直接报错）
  - `integration` —— 打真实外部数据源。默认会被执行；只跑离线单元测试用 `-m "not integration"`。
  - `real_db` —— 需要 `data/stocklab.duckdb` 的真实数据。

- **用例与断言的纪律**
  - **一个用例只验一件事**。迁移前 `test_web_api.py` 有 16 个「阶段」，每个几百行、
    失败只能看到一个 `AssertionError`；现在拆成独立用例，`-k` 可单选、`--lf` 可只重跑失败项。
  - **注释声称的不变量，断言必须真的在查**。这是本次迁移挖出来的最大问题，见下。
  - 需要穷举而非举例时用 hypothesis（`tests/test_properties.py`），而不是多列几个例子。
  - **阶段数不得减少**。机械改名（`run_x_test` → `test_x`）会漏掉带参数的函数 ——
    迁移时 `test_research_snapshot.py` 的 5 个阶段里有 4 个靠手工传参串联，
    重命名后 pytest 一个都不收集，覆盖率静默归零。对照方法是
    `git show HEAD:tests/x.py | grep -c "^def run_"` 与 `--collect-only` 的条数比对。

- **`tests/test_properties.py`：两个已证实的测试缺口**
  迁移前用手工变异测试（故意改坏代码，看断言抓不抓得到）跑了 5 个变异，抓到 3 个、
  **漏网 2 个**，且都不是框架能力问题，而是断言没在查它声称要查的东西：

  | 漏网的变异 | 原断言为什么抓不到 | 现在怎么钉住 |
  |---|---|---|
  | `compare_api._align_history` 把缺失点改成**前向填充** | 只有 `assert gaps > 0`，而前置空档（上市前）就能满足它；上市后中途停牌被填成水平线完全看不出来 | 属性测试逐点核对「输出为 None 的位置，原始数据里也必须没有那一行」 |
  | `/api/market/ranking` 的 `codes` 精确过滤退化成**子串匹配** | 只验「传 3 个完整代码返回 3 只」，而这 3 个代码之间本来就没有子串关系 | 参数化传 `60051` / `6005` / `00000` / `30075` / `519` 等**片段**，精确过滤必须 0 条 |

  **教训**：`assert gaps > 0` 这类「看起来在查、其实只覆盖了一半」的断言最危险 ——
  它给出「已覆盖」的错觉。写断言时要问：这段代码如果做错了，我的断言会不会红？
  另一个高频错法是**假设错了的前提**：hypothesis 上线当天就抓出我自己写的
  `len(dict)` 取到键数、以及「分位算的是序列最小值」（实际是**最新一日**那个值）。

- **覆盖率基线**（`--cov=stocklab --cov=app`）：**87%**（6335 语句 / 825 未覆盖），422 个用例。
  第一轮补覆盖率时是 65%，主要靠新增 6 个测试文件（见上表）拉起来。

  - **接近满覆盖的关键路径**（改动这里最需要担心）
    | 模块 | 覆盖率 | 为什么重要 |
    |---|---|---|
    | `facade/market_data.py` | 98% | 取数优先级路由与 cache-aside，错了不报错只给另一批数据 |
    | `datasource/lifecycle_service.py` | 98% | 上市/退市日历，任一来源失败须整体中止 |
    | `persistence/migrations/runner.py` | 98% | 迁移幂等与失败不记版本 |
    | `analytics/valuation_distribution.py` | 98% | 全市场 PE 分布口径（直接进报告文案） |
    | `datasource/fundamental_service.py` | 94% | 三大报表的契约失败不得产出 |
    | `analytics/percentile_reporter.py` | 95% | 单股分位报告 |
    | `app/scripts/sync_market_data.py` | 93% | 9 个同步阶段的失败语义与增量水位 |
    | `analytics/profile_reporter.py` / `markdown_reporter.py` | 92% / 90% | 报告排版 |

  - **仍偏低、且补起来性价比低的**（都是「要有真实外部响应才测得到」的分支）
    | 模块 | 覆盖率 | 未覆盖原因 |
    |---|---|---|
    | `datasource/market_service.py` | 56% | 未覆盖 74 条，主要是各 akshare 源站改列名后的分支 |
    | `_sources/baostock_source.py` / `akshare_source.py` | 55% / 56% | 真实 baostock 会话与 akshare 分页 |
    | `common/http_client.py` | 35% | 只有 UA 补丁与重试包装，需真实网络故障才能触发 |
    | `persistence/repository/index_membership.py` | 46% | 成分股按 as-of 还原，缺真实成分数据 |

    这几处的共性：**要触发它们必须先有一个「上游返回了奇怪东西」的实况**。
    与其用桩硬造（造出来的形状和真异常不一样，测了也不可信），不如等 `integration`
    用例在真实上游出问题时自然覆盖。所以这里刻意停在 87%，不追求数字。

- **补覆盖率时踩到的四类坑（都写进了测试注释，值得复用）**
  1. **列名必须照抄契约，不能凭印象写。** 数据源归一化只认
     `eastmoney.py` / `exchange.py` 里的候选列清单；写错一个字母就走到
     「缺关键列 → DataContractError」分支，测试会「全绿但什么都没测到」。
     造假数据一律用 `stocklab.domain.*_COLUMNS` 补全，别手写。
  2. **断言要写实际契约，不是合理推测。** 这一轮写错了 8 处，全是同一类：
     以为「按交易日对齐」实际是「按月（Period）对齐」；以为「不传层级只返回一级」
     实际返回全部层级；以为门面会吞掉远端异常实际会向上抛；以为估值快照的
     `pe_ttm` 取「市盈率-动态」实际取「市盈率(TTM)」。每个都会让测试验证
     一个不存在的保护。
  3. **不要用「调用次数」推断行为。** 曾写过 `空表 if 该代码已被调用过 else 数据`，
     既依赖 `find_all()` 的返回顺序（顺序一变结论翻转），又把一只股票的数据写进了
     另一只的行里。要表达「哪只失败」就用代码集合显式指定。
  4. **改行为要拆成两个用例。** 发现公告「批内同内容不去重」时，拆成
     `test_..._against_previous_runs`（应有行为）与
     `test_..._does_not_dedupe_within_one_batch`（已知缺口 + 修复后本用例会红），
     这样修复时必然要同步更新期望值，不会悄悄把缺口当成规范。

- **`live_check_sources.py` 不是 pytest 用例**：它的函数名是 `check_*`，
  pytest 不会收集；有意保留为带退出码（0/1）的诊断脚本，供「上游疑似变更时
  快速复查」用。不要把它改成 `test_*` —— 它要的是退出码语义与逐源打印，不是断言。

---

## 目录结构

```
StockLab/
├── .gitignore                          # Git 忽略规则（__pycache__ / venv / output / data / .workbuddy）
├── AGENT.md                            # 本文档（项目永久上下文与设计契约）
├── config.ini                          # 运行配置：月数、输出路径、超时、基准与板块清单
├── configs/                            # 结构化筛选条件示例（JSON，供 run_research.py --config 使用）
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
│   │   ├── data_contract.py              #     统一数据契约：列名常量 + StockRealtimeQuote（无依赖，单股/入库两服务共用）
│   │   ├── tencent_client.py           #     【腾讯传输网关】TencentMarketClient（股票/ETF/指数统一接入）
│   │   ├── quote_service.py            #     【单股行情服务】StockQuoteService：五级降级编排 + 数据契约 re-export
│   │   ├── market_service.py             #     【入库取数】MarketService：7 个 fetch_* 输出领域契约帧（源缺列抛 DataContractError）
│   │   ├── fundamental_service.py        #     【逐只基本面】FundamentalService：东财三大报表（单只约 60 次请求）
│   │   ├── lifecycle_service.py          #     【生命周期】LifecycleService：沪深北上市日历 + 退市日历
│   │   └── _sources/                   #     单股通道实现包（下划线前缀 = 私有，外部勿依赖）
│   │       ├── __init__.py             #       导出抽象基类与五个通道实现
│   │       ├── base.py                 #       StockDataSource 抽象基类（纯虚接口 + 标准化/降采样工具）
│   │       ├── akshare_source.py       #       东方财富主通道（akshare，含重试与列名防御）
│   │       ├── baostock_source.py      #       证券宝备用通道（专有 Socket + login/logout 会话管理）
│   │       ├── tencent_source.py       #       腾讯直连通道（实时行情/简称/备用日线降采样）
│   │       ├── sina_source.py          #       新浪财经通道（实时/月线/qfq+hfq，HTTPS+Referer+GBK，约 4 年日线）
│   │       └── tdx_source.py           #       通达信通道（tdxdata 新协议，category=6 月线，market=2 北交所，仅不复权）
│   ├── domain/                         #   列契约层（叶子包，零 StockLab 依赖）
│   │   ├── __init__.py                 #     导出契约元组、状态/事件枚举与 DataContractError
│   │   ├── contract.py                 #     check_columns / require_columns / align_columns（缺列即拒绝）
│   │   ├── security.py                 #     SECURITY_COLUMNS / SECURITY_EVENT_COLUMNS / INDEX_MEMBERSHIP_COLUMNS + 枚举
│   │   ├── market_data.py              #     DAILY_PRICE_COLUMNS
│   │   ├── valuation.py                #     DAILY_VALUATION / VALUATION_HISTORY / INDUSTRY_VALUATION 列契约
│   │   ├── fundamental.py              #     FUNDAMENTAL_PIT_COLUMNS（报告期 + 公告日 + 可见日）与四表契约
│   │   └── research.py                 #     SNAPSHOT_COLUMNS / SNAPSHOT_RESULT_COLUMNS（研究快照两表，与 004 同序）
│   ├── normalization/                  #   归一化层（叶子包：源表 → 契约帧，不取数不落库）
│   │   ├── __init__.py                 #     导出三层归一化入口
│   │   ├── base.py                     #     原子原语：代码 / 选列 / 数值 / 日期 / 文本清洗
│   │   ├── akshare.py                  #     证券名录、日K、估值快照、历史估值、行业估值、指数成分、公司概况
│   │   ├── exchange.py                 #     上市/退市日历、merge_lifecycle（回填日期 + 推导 status）、build_lifecycle_events
│   │   └── eastmoney.py                #     东财三大报表 → PIT 帧（available_date = announce_date）
│   ├── fundamental/                    #   基本面派生层（纯计算，不取数不落库）
│   │   ├── __init__.py                 #     导出 build_financial_indicators + 与 V2 文档 §3.1 目录的对应关系
│   │   └── indicator.py                #     ROE / ROA / ROIC / 毛利率 / 净利率 / 同比；公告日取两表较晚者
│   ├── factor/                         #   因子引擎（V2 §7，纯计算：只吃 DataFrame，零 StockLab 依赖）
│   │   ├── __init__.py                 #     导出 Factor / registry / 预处理入口，并**触发六个分类包的因子登记**
│   │   ├── base.py                     #     Factor 定义 + FactorDataError + divide / log_positive（categories 为元组）
│   │   ├── registry.py                 #     显式 register() 登记（无装饰器）、get / list_factors / compute、FACTOR_VERSION
│   │   ├── preprocessing/              #     §7.6 预处理：winsorize / zscore / rank / missing / 行业·市值中性化
│   │   ├── value/                      #     估值类（pe_ttm / pb / ps / ev_ebitda / fcf_yield / dividend_yield…）
│   │   ├── quality/                    #     质量类（roe / roic / 毛利·营业净利率 / cfo_to_net_profit…）
│   │   ├── growth/                     #     成长类（营收·利润·EPS·FCF 同比、roe_trend…）
│   │   ├── momentum/                   #     动量类（1/3/6/12M 收益率）
│   │   ├── dividend/                   #     分红类（dividend_yield / payout_ratio / 分红稳定性与增速）
│   │   └── risk/                       #     风险类（6/12M 波动率、12M 最大回撤）
│   ├── screener/                       #   结构化筛选（V2 §8，纯计算，不取数不落库）
│   │   ├── __init__.py                 #     导出 ScreenRule / ScreenGroup / ScreenPipeline / ScreenResult / ScreenError
│   │   ├── rules.py                    #     9 种操作符表驱动 + AND/OR 组嵌套 + 逐行 explain（满足/未满足/无数据）
│   │   └── pipeline.py                 #     from_spec / spec 往返、执行顺序（算因子→预处理→判定→组合）、summary + detail
│   ├── backtest/                       #   回测层（V2 §9，纯计算：只吃 DataFrame，不联网不查库）
│   │   ├── __init__.py                 #     导出 BacktestEngine / BacktestResult / Portfolio / Order / Trade / CostModel / universe_as_of
│   │   ├── engine.py                   #     四步日循环（解锁→分红→撮合昨日委托→计值下单）、成交日=信号日+1、拒绝理由留痕
│   │   ├── portfolio.py                #     Cash / Position / T+1 可卖数量 / 平均成本含费 / 缺价拒绝计值
│   │   ├── order.py                    #     BacktestError + 买卖方向 + Order（待撮合/已成交/已拒绝）
│   │   ├── trade.py                    #     Trade：成交金额、佣金/印花税/滑点三段、已实现盈亏
│   │   ├── cost.py                     #     CostModel：佣金（最低 5 元）+ 印花税（仅卖出）+ 滑点
│   │   ├── universe.py                 #     universe_as_of：与 SecurityRepository.universe 同口径的 as-of 股票池
│   │   └── metrics.py                  #     §9.3 十一项指标（252 日年化；分母为 0 → NaN）
│   ├── persistence/                    #   本地数据持久化层（只负责「往本地存数」）
│   │   ├── __init__.py                 #     本层统一出口（Database + 14 个 Repository）
│   │   ├── storage/                    #     数据存储基础设施
│   │   │   ├── __init__.py             #       导出 Database / initialize_database
│   │   │   ├── duckdb.py               #       DuckDB 连接管理（Database 类，支持 with，打开时校验版本）
│   │   │   └── schema.py               #       initialize_database()：委托迁移器（**不含 DDL**）
│   │   ├── migrations/                 #     数据库结构迁移（DDL 的唯一真相）
│   │   │   ├── __init__.py             #       导出 SchemaMigrator / MIGRATIONS_DIR
│   │   │   ├── runner.py               #       SchemaMigrator：引导 sys.schema_version、按序补跑、失败不记版本
│   │   │   ├── 001_initial.sql         #       基线迁移（机制上线前的既有结构，全部 IF NOT EXISTS）
│   │   │   ├── 002_fundamental.sql     #       fundamental 域四张表
│   │   │   ├── 003_security_events.sql #       list_status → status + reference.security_events
│   │   │   ├── 004_research.sql        #       research.snapshots + research.snapshot_results（研究快照，只写不改）
│   │   │   └── 005_announcements.sql   #       corporate.announcements（巨潮公告索引，主键 announcement_id + 二次去重键）
│   │   └── repository/                 #     数据访问层（表级 SQL 封装）
│   │       ├── __init__.py             #       导出 BaseRepository 与 14 个 Repository
│   │       ├── base.py                 #       BaseRepository：契约对齐 + 显式列名 UPSERT / 异常处理 / 日志模板
│   │       ├── security.py             #       reference.securities（名录 upsert / 生命周期 upsert_lifecycle / universe(as_of)）
│   │       ├── security_event.py       #       reference.security_events（生命周期事件按 as-of 查询）
│   │       ├── daily_price.py          #       market.daily_prices 读写 + find_window（只回原始收盘价）
│   │       ├── daily_valuation.py      #       market.daily_valuations 读写 + cross_section_as_of
│   │       ├── valuation_history.py    #       market.valuation_history 读写 + cross_section_as_of（<= as-of 最近一条）
│   │       ├── index_membership.py     #       reference.index_memberships 读写
│   │       ├── industry_valuation.py   #       market.industry_valuations 读写
│   │       ├── fundamental.py          #       fundamental 四表 + find_as_of / latest_as_of / cross_section_as_of
│   │       └── research_snapshot.py    #       研究快照两表（只 INSERT，重复 snapshot_id 拒绝覆盖）
│   ├── research/                       #   研究编排层（V2 §6：取数 + 算因子 + 筛选 + 落快照的唯一编排方）
│   │   ├── __init__.py                 #     导出 build_factor_frame / create_snapshot / load_snapshot / rerun_snapshot
│   │   ├── frame.py                    #     build_factor_frame：按 Point-in-Time 拼因子输入帧，无数据源的列 NaN 落地并告警
│   │   └── snapshot.py                 #     快照读写与复现比对 + config_version / data_version（库版本@真实数据截止日）
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
│   ├── web/                            #   本地 Web 分析服务（Flask，浏览器端点击分析）
│   │   ├── __init__.py                #     导出 create_app
│   │   ├── server.py                  #     create_app 装配：add_url_rule 路由注册表（非装饰器），页面 7 + 接口 10 + 图标 1
│   │   ├── store.py                   #     进程级数据访问单例：单例门面锁 / 聚合连接锁 / 七档评级 / 行业横截面 / 进程内缓存
│   │   ├── api.py                     #     个股接口：代码与中文名解析 → store → analyzer → JSON
│   │   ├── screener_api.py             #     选股器接口：因子覆盖率元数据 + 执行筛选（委托 screener/research，连接借门面）
│   │   ├── compare_api.py              #     多股对比接口：2~10 只 × 5 指标分位 + 按交易日对齐的叠加走势
│   │   ├── market_api.py               #     全市场/指数/行业接口：过滤排序分页（ranking 支持 codes 精确过滤）→ store 聚合 → JSON
│   │   ├── templates/
│   │   │   ├── _layout.html           #     应用外壳骨架 + 资源加载顺序 + 首屏防闪烁脚本
│   │   │   ├── _sidebar.html          #     左侧常驻导航（三组，NAV_* 列表数据驱动）
│   │   │   ├── _topbar.html           #     顶部工具条（全局证券搜索 / 折叠 / 主题切换）
│   │   │   ├── _footer.html           #     共享页脚（分位与七档口径 / 数据来源 / 免责声明）
│   │   │   ├── analysis.html          #     个股分析页（温度条 / 多窗口 / 明细表 / 七档图例）
│   │   │   ├── market.html            #     全市场 Dashboard（统计卡 / 分布图 / 排行表 / 分页 / 评级钻取）
│   │   │   ├── indices.html           #     指数估值页（指数卡片 + 走势详情）
│   │   │   ├── industries.html        #     行业估值页（层级切换 / PE 条形图 / 明细表）
│   │   │   ├── screener.html          #     选股器页（规则编辑器 / 漏斗 / 散点 / 行业通过率 / 导出分享）
│   │   │   ├── compare.html           #     多股对比页（筹码挑选 / 指标对比卡 / 叠加走势 / 分位雷达 / 明细表）
│   │   │   └── portfolio.html         #     组合监控页（自选筹码 / 分位阈值告警 / 卡片与表格双视图 / 导出对比）
│   │   └── static/
│   │       ├── echarts.min.js         #     ECharts 5.5 vendored（本地托管，离线可用）
│   │       ├── tokens.css             #     设计令牌（唯一取值来源）：亮/暗双主题 + 间距/圆角/阴影/字号/动效/层级
│   │       ├── components.css         #     组件唯一定义处：按钮 / 表单 / 卡片 / 表格 / 徽章 / 标签页 / 消息条 / 浮层 / Toast / 骨架屏 / 温度条 / 概览条 / 联想下拉
│   │       ├── base.css               #     应用外壳与版式：侧边栏 / 顶栏 / 内容区 / 页面韵律 / 布局工具 / 页脚 / 打印
│   │       ├── common.js              #     SL：请求（支持 POST）/ 格式化 / 七档配色 / 图例 / 健康检查 / ECharts option
│   │       ├── charts.js              #     SL.charts：调色板 + 图表登记簿 + 主题重绘 + 可复用 option 片段
│   │       ├── ui.js                  #     SL.ui：Toast / 模态 / 抽屉 / Tooltip / 防抖 / CSV 导出 / 快捷键 + 外壳折叠与全局搜索
│   │       ├── theme.js               #     SL.theme：亮暗切换 + 持久化 + sl:themechange 广播 + 快捷键 T
│   │       ├── screener.js            #     选股页：规则编辑 / 执行 / 漏斗 / 散点 / 导出分享
│   │       ├── compare.js             #     对比页：标的联想挑选 / 指标卡 / 叠加走势 / 雷达 / 明细表
│   │       ├── portfolio.js           #     组合监控页：localStorage 自选 / 阈值告警 / 全市场名次 / 双视图
│   │       ├── app.js                 #     个股页：联想键盘操作 / 分析渲染 / URL 还原
│   │       ├── market.js              #     Dashboard：统计卡 / 直方图 / 评级钻取 / 排行分页
│   │       ├── indices.js             #     指数页：卡片列表 / 详情加载
│   │       └── industries.js          #     行业页：层级切换 / 条形图 / 明细表
│   └── scripts/                       #   CLI 入口
│       ├── generate_sector_trend.py   #     命令行入口：生成板块走势网页
│       ├── sync_market_data.py        #     命令行入口：全市场数据同步到本地 DuckDB（八个阶段）
│       ├── run_research.py            #     命令行入口：研究筛选（screen / rerun / list / factors 四个子命令）
│       ├── serve_web.py               #     命令行入口：启动本地 Web 分析服务（仅监听 127.0.0.1）
│       ├── verify_market_sql.py       #     抽样校验：全市场分位 SQL 与 analyzer 口径一致（退出码可进 CI）
│       ├── analyze_pe_distribution.py #     命令行入口：全市场市盈率分布统计
│       └── analyze_valuation_percentile.py # 命令行入口：个股历史估值分位计算
├── pytest.ini                          # ── pytest 配置（pythonpath / markers / addopts）──
├── tests/                              # ── 测试（pytest 9，统一用 `./venv/bin/python -m pytest` 运行）──
│   ├── conftest.py                     #     共享装置：tmp_db_path / fresh_db 临时库、app·ctx·client 真实库、联网探测
│   ├── test_properties.py              #     口径不变量属性测试（hypothesis 随机穷举 + 两个回归钉子）
│   ├── test_analytics_distribution.py   #     PE 分布分析器与三类报告渲染（此前 18%）
│   ├── test_sync_cli.py                #     同步 9 阶段的失败语义、增量日期推导、main() 编排（此前 9%）
│   ├── test_facade_routing.py           #     取数门面的 local_first / remote_first 两个方向与回写（此前 46%）
│   ├── test_quote_service.py            #     单股取数的五级降级链逐级验证 + 按月对齐（此前 56%）
│   ├── test_datasource_services.py      #     日历/财报服务的重试、限流、契约失败降级（此前 25%）
│   ├── test_config_and_normalization.py #     config.ini 解析与归一化边界（此前 38%）
│   ├── test_industry_repo_and_dashboard.py #    行业表读写与看板网页生成（此前 33% / 20%）
│   ├── test_data_interfaces.py         #     全链路自检（类型转换 / 跨资产行情 / 实时快照 / 简称 / 月线估值 / 网页生成），除类型转换外均联网
│   ├── test_web_api.py                 #     Web 层自检（路由 / 参数校验 / 中文名解析 / 七档前后端一致 / 口径抽样 / 页面要素与导航高亮 / 选股器 / 多股对比 / 组合监控 / 横向位置）
│   ├── test_migrations.py              #     迁移自检（新库 / 幂等 / 老库升级 / 失败不记版本 / 序号重复）
│   ├── test_data_contract.py           #     数据契约自检（缺列拒绝 / 乱序写入不错位 / 契约与 DDL 一致）
│   ├── test_factor_engine.py           #     因子引擎自检（登记契约 / 六分类 / 数学手算核对 / 预处理与非法配置）
│   ├── test_screener.py                #     选股器自检（操作符 / AND·OR 嵌套 / 缺失值 / 预处理生效 / spec 往返）
│   ├── test_research_snapshot.py       #     研究快照自检（PIT 帧 / 筛选 / 快照写入 / 复现比对 / 空库），全部用临时库
│   ├── test_backtest.py                #     回测自检（成本 / T+1 / 指标手算 / 幸存者安全 / 撮合与拒绝理由 / 非法输入）
│   ├── test_pit_universe.py            #     PIT 与股票池自检（未来泄漏 / 幸存者偏差 / 指标派生）
│   ├── test_sina_source.py             #     新浪通道自检（符号转换 / JSONP / 实时 / 简称 / 月线复权 / 8 组异常），离线 fixture
│   ├── test_tdx_source.py              #     通达信通道自检（市场编号 / 月线 category / 实时量额 / 6 组失败 / 简称空串），注入 fake client
│   ├── test_cninfo_client.py           #     巨潮客户端自检（解析 / 分页 / orgId缓存+精确匹配 / 去重 / 5 组网络异常），离线 fixture
│   ├── test_announcements.py           #     公告持久化与同步自检（迁移005 / Repo / 水位 / 过滤 / 去重 / 逐只 / 幂等），临时库
│   └── live_check_sources.py           #     手工联网验证脚本（**不是 pytest 用例**，函数名不匹配 test_*，pytest 不收集；
│                                         #     有意保留为带退出码的诊断工具，供「上游疑似变更时快速复查」用）
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
| **数据源** | `stocklab.datasource.quote_service` | **单股行情服务 `StockQuoteService`**：五通道降级（AkShare→BaoStock→Tencent→Sina→TDX）、月线价格+PE对齐、实时行情 |
| | `stocklab.datasource.data_contract` | **数据契约**：列名常量 + `StockRealtimeQuote`，单股行情/入库取数两服务共用 |
| | `stocklab.datasource.tencent_client` | **腾讯传输网关 `TencentMarketClient`**：股票/ETF/指数不区分、并发批量、OHLCV全要素 |
| | `stocklab.datasource.market_service` | **入库取数 `MarketService`**：基础信息/日K/估值快照/历史估值/行业估值/指数成分/公司概况 —— 输出领域契约帧，源缺列抛 `DataContractError` |
| | `stocklab.datasource.fundamental_service` | **逐只基本面 `FundamentalService`**：东财利润表/资产负债表/现金流量表（按报告期分批，单只约 60 次请求） |
| | `stocklab.datasource.lifecycle_service` | **生命周期 `LifecycleService`**：沪深北上市日历与退市日历（只读交易所官网，名录外的退市股也由此补齐） |
| **契约** | `stocklab.domain.contract` | **列契约三件套**：`check_columns` / `require_columns` / `align_columns`，缺列即拒绝 |
| | `stocklab.domain.*` | 各表列契约元组与 `SECURITY_STATUSES` / `SECURITY_EVENT_TYPES` 枚举 |
| **归一化** | `stocklab.normalization.base` | 源表原子原语：代码归一、选列、数值/日期/文本清洗 |
| | `stocklab.normalization.akshare` / `exchange` / `eastmoney` | 7 类 akshare 源表、交易所上市退市日历（含 `merge_lifecycle`）、东财三大报表 → 契约帧 |
| **派生** | `stocklab.fundamental.indicator` | 财务指标纯派生：ROE / ROA / ROIC / 毛利率 / 净利率 / 同比，公告日取两表较晚者 |
| **因子** | `stocklab.factor.registry` | **因子登记与批量计算**：显式 `register()`（**无装饰器**）、`get` / `list_factors` / `compute`、`FACTOR_VERSION = "factor_v1"`；本模块不 import 分类包（由包入口触发登记） |
| | `stocklab.factor.base` / `preprocessing` | `Factor` 定义（`categories` 为元组，一个因子可挂多个分类）+ `FactorDataError`；§7.6 预处理 winsorize / zscore / rank / missing / 行业·市值中性化（非法配置一律拒绝） |
| **筛选** | `stocklab.screener.rules` | 9 种操作符表驱动 + AND/OR 可嵌套组；除 `isna` 外缺失值一律判不通过，`explain()` 逐行给出「满足 / 未满足 / 无数据」 |
| | `stocklab.screener.pipeline` | `from_spec` / `spec` 往返（与 §8.1 YAML 同构）；执行顺序 = 算因子 → 预处理 → **判定** → 组合；输出 `summary` + `detail`（§8.2） |
| **回测** | `stocklab.backtest.engine` | `BacktestEngine.run(prices, signals, dividends, benchmark)`：四步日循环（T+1 解锁 → 分红 → 撮合昨日委托先卖后买按收盘价 → 计值下单），**成交日恒为信号日+1**；输出权益曲线 / 成交 / 委托 / 期末持仓 |
| | `stocklab.backtest.portfolio` / `order` / `trade` | `Portfolio`（现金 + `available` 可卖数量，`unlock()` 才解锁）、`Order`（待撮合/已成交/已拒绝，拒绝必写原因）、`Trade`（费用三段 + 已实现盈亏，平均成本含买入费用） |
| | `stocklab.backtest.cost` / `universe` / `metrics` | `CostModel`（佣金最低 5 元 + 印花税仅卖出 + 滑点）、`universe_as_of`（与 `SecurityRepository.universe` 同口径）、`compute_metrics`（§9.3 十一项，252 日年化，分母为 0 → NaN） |
| **研究** | `stocklab.research.frame` | `build_factor_frame`：Point-in-Time 拼因子输入帧（as-of 股票池 / `available_date <= as-of` / 上年同期 / 日线派生动量·波动率·回撤）；无数据源的列 NaN 落地并告警 |
| | `stocklab.research.snapshot` | `create_snapshot` / `load_snapshot` / `list_snapshots` / `rerun_snapshot`（按 spec 重新生成并逐行比对）+ `config_version` / `data_version` / `factor_version`；快照只写不改 |
| **外观** | `stocklab.facade.market_data` | **统一取数门面**：本地优先/远端优先策略、Cache-Aside 回写 |
| **持久化** | `stocklab.persistence.migrations` | **迁移执行器 `SchemaMigrator`**：`NNN_*.sql` 为 DDL 唯一真相，`sys.schema_version` 记录版本、失败不记版本 |
| | `stocklab.persistence.storage.schema` | `initialize_database()`：委托迁移器（**本文件不含 DDL**） |
| | `stocklab.persistence.storage.duckdb` | 连接管理：延迟初始化、上下文管理器、打开时校验结构版本 |
| | `stocklab.persistence.repository.*` | 表级 Repository：经 `BaseRepository` 契约对齐 + 显式列名 UPSERT / 异常处理；含 PIT 查询与 `universe(as_of)` |
| **分析** | `stocklab.analytics.valuation_percentile` | **历史分位 CDF 口径**：排除亏损期、输出档位判定 |
| | `stocklab.analytics.valuation_distribution` | **全市场 PE 分布**：中位数/分位/固定语义分桶/交易所对比/极值榜单 |
| **渲染** | `stocklab.analytics.percentile_reporter` | 单股分位：控制台表格 + Markdown |
| | `stocklab.analytics.profile_reporter` | 全市场分布：纯文本报告（ASCII 条形图） |
| | `stocklab.analytics.markdown_reporter` | 全市场分布：归档级 Markdown（表格 + 自动结论） |
| **仪表板** | `app.dashboard.sector_trend` | 板块走势 Facade：配置→取数→HTML 编排 |
| | `app.dashboard.page_generator` | 模板渲染：月份并集对齐、JSON 注入 dashboard.html |
| **Web 服务** | `app.web.server` | `create_app()`：Flask 装配 + `add_url_rule` 路由注册表（无装饰器），页面 7 + 接口 10 + favicon = 18 条 |
| | `app.web.store` | **进程级数据访问单例**：门面锁 / 聚合连接锁、全市场窗口函数 SQL、指数成分中位数序列、七档评级、进程内缓存 |
| | `app.web.api` | 个股接口：`/api/percentile` 分位 + 多窗口、`/api/securities` 联想（排序 + 大小写不敏感）、代码与中文名解析 |
| | `app.web.market_api` | 全市场接口：`/api/market/ranking`（过滤/排序/分页 + 七档分布与直方图）、`/api/indices`、`/api/index/detail` |
| **脚本** | `app/scripts/sync_market_data.py` | 8 阶段同步 CLI：证券/日K/估值快照/历史估值/指数成分/行业估值/生命周期/基本面（默认全跑一、二、三、五、七） |
| | `app/scripts/run_research.py` | 研究筛选 CLI：`screen`（取数 → 因子 → 筛选 → 写研究快照）/ `rerun`（复现比对，不一致退出码 1）/ `list` / `factors` |
| | `app/scripts/generate_sector_trend.py` | 可视化生成 CLI：月数/输出路径/配置文件可配 |
| | `app/scripts/serve_web.py` | 本地分析服务 CLI：端口/优先级/数据库路径可配，仅监听 127.0.0.1 |

---

## 运行方式

```bash
# 启动本地 Web 分析服务（浏览器打开 http://127.0.0.1:8000 点击分析；七个页面）
./venv/bin/python app/scripts/serve_web.py

# 指定端口与取数优先级
./venv/bin/python app/scripts/serve_web.py --port 8321 --priority remote_first

# 生成板块走势网页（默认读取 config.ini）
./venv/bin/python app/scripts/generate_sector_trend.py

# 自定义月数与输出路径
./venv/bin/python app/scripts/generate_sector_trend.py --months 60 --output output/trend_5y.html

# 指定其他配置文件
./venv/bin/python app/scripts/generate_sector_trend.py --config config.ini

# 全链路自检（可选股票代码，默认 000001.SZ）
./venv/bin/python -m pytest tests/test_data_interfaces.py -v -s   # 联网用例，看实时输出

# ---- 测试 ----
# Web 层用例会打开本地 DuckDB，须先停掉 serve_web.py（conftest 会主动检测并给出提示）
pkill -f serve_web.py

./venv/bin/python -m pytest                      # 全量（含联网，约 50s）
./venv/bin/python -m pytest -m "not integration" # 只跑离线单元测试（约 30s）
./venv/bin/python -m pytest tests/test_web_api.py -v        # 单文件
./venv/bin/python -m pytest -k "codes" -v                   # 按名字筛
./venv/bin/python -m pytest --lf                 # 只重跑上次失败的
./venv/bin/python -m pytest -n 4                 # 4 进程并行
./venv/bin/python -m pytest --cov=stocklab --cov=app --cov-report=term-missing
./venv/bin/python -m pytest --cov=stocklab --cov=app --cov-report=html  # 打开 htmlcov/index.html 看未覆盖行

# 全市场分位 SQL 与 analyzer 口径抽样比对（默认 300 只，非 0 退出码即为不一致）
./venv/bin/python app/scripts/verify_market_sql.py 300

# 同步全市场数据到本地 DuckDB（不带子命令 = 一键全跑前三个阶段）
./venv/bin/python app/scripts/sync_market_data.py --start-date 2025-01-01 --end-date 2026-09-30

# 增量同步（自动从最新交易日期同步到当前）
./venv/bin/python app/scripts/sync_market_data.py --incremental

# 各阶段分开执行（子命令，公共参数可在子命令前后任意位置）
./venv/bin/python app/scripts/sync_market_data.py securities                 # 阶段一：股票基础信息
./venv/bin/python app/scripts/sync_market_data.py prices --incremental       # 阶段二：日 K 行情
./venv/bin/python app/scripts/sync_market_data.py prices --start-date 1990-12-19   # 阶段二：全历史回补
./venv/bin/python app/scripts/sync_market_data.py valuations                 # 阶段三：估值快照
./venv/bin/python app/scripts/sync_market_data.py valuation-history --period 近五年  # 阶段四：历史估值序列
./venv/bin/python app/scripts/sync_market_data.py indexes                     # 阶段五：主流宽基指数成分
./venv/bin/python app/scripts/sync_market_data.py industries --stat-date 2026-09-30 # 阶段六：行业估值横截面
./venv/bin/python app/scripts/sync_market_data.py lifecycle                   # 阶段七：证券生命周期（上市/退市日历）

# 阶段八：Point-in-Time 基本面（逐只抓取，耗时以小时计，不参与一键全跑）
./venv/bin/python app/scripts/sync_market_data.py fundamentals --ts-code 600519.SH
./venv/bin/python app/scripts/sync_market_data.py fundamentals --workers 8

# ── 研究筛选（V2 §6 / §8）──
# screen：按 JSON 条件筛选并写入研究快照（重跑历史必须显式给 --as-of）
./venv/bin/python app/scripts/run_research.py screen --as-of 2026-10-02 --config configs/screen_value.json
# 只看结果不落快照；限定股票池与打印行数
./venv/bin/python app/scripts/run_research.py screen --as-of 2026-10-02 --config configs/screen_value.json --no-snapshot
./venv/bin/python app/scripts/run_research.py screen --as-of 2026-10-02 --config configs/screen_value.json \
    --ts-codes 600519.SH,000001.SZ --top 5
# rerun：按快照编号重新生成并与存档逐行比对（不一致时退出码 1，可进 CI）
./venv/bin/python app/scripts/run_research.py rerun --snapshot-id RS20261003155523-5180f2
# list：全部研究快照；factors：已登记因子（分类 / 输入列 / 口径）
./venv/bin/python app/scripts/run_research.py list
./venv/bin/python app/scripts/run_research.py factors

# 按主题挑测试（-k 支持布尔表达式；下面按被测对象分组）
./venv/bin/python -m pytest -k "migration or contract"   # 迁移幂等/老库升级 + 契约与 DDL 对齐 + 乱序写入
./venv/bin/python -m pytest -k "universe or point_in_time or survivorship"  # 幸存者偏差与 PIT 未来泄漏
./venv/bin/python -m pytest -k "factor or preproces"      # 因子登记契约、数学手算、预处理、口径不变量
./venv/bin/python -m pytest -k "screen or snapshot or rerun"  # 操作符嵌套、spec 往返、快照复现
./venv/bin/python -m pytest tests/test_backtest.py         # 回测：成本 / T+1 / 指标手算 / 幸存者安全 / 撮合拒绝

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
| **浏览器点开分析** | `python app/scripts/serve_web.py` → `/` `/market` `/indices` `/industries` `/screener` `/compare` `/portfolio` | store(单例锁) → facade/analyzer → JSON → ECharts |
| **全市场分位排行** | `GET /api/market/ranking?indicator=pe_ttm` | store 窗口函数 SQL → 七档评级 → 过滤/排序/分页 |
| **行业估值横截面** | `GET /api/industries?level=1` | store 行业表 → 层级过滤 → PE 三口径 + 规模 |
| **指数估值** | `GET /api/indices`、`GET /api/index/detail?code=000300` | index_memberships 成分中位数序列 → analyzer 分位 |

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
| duckdb | 1.4.3 | 本地分析型数据仓库（全市场 A 股数据持久化；`app.web.store` 另直接用它做全市场与指数聚合查询） |
| Flask | 3.1.3 | 本地 Web 分析服务（`app/web` 专用；含 Werkzeug/Jinja2 等 6 个传递依赖，均锁定实测版本） |

> `numpy` 与 `matplotlib` **不在 requirements.txt 中**，是 akshare / pandas 带入的传递依赖（实测环境：numpy 2.5.3、matplotlib 3.11.2）。全库无 `import numpy` / `import matplotlib`，绘图一律由前端 ECharts 在浏览器内完成。
> ECharts 5.5 已 vendored 于 `app/web/static/echarts.min.js`（约 1MB），**本地托管、离线可用**，页面不依赖外部 CDN。

> **因子已登记 ≠ 因子有数据**：本地目前只有 `pe_ttm`（100%）、`pb`（99.5%）两个因子真正有数据源
> （基本面仅同步了 600519 一只，日 K 尚未灌入），其余 25 个因子虽已登记但取值全为 NaN。
> 因此选股器 `/api/screener/meta` **逐因子下发 coverage 与 availability**，规则编辑器里直接标色提示，
> 预置模板也按「引用了无数据因子」标 `needs_data`——避免用户对着空因子建规则却只得到 0 只通过。
> 数据补齐后覆盖率会自动上升，无需改前端。

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
13. **巨潮 `stock_profile_cninfo` 已恢复可用**（实测 600519 / 000001 / 300750 均返回 1×26 表，
    曾于 2025 年返回 `resultcode 451 ApiFilter` 授权错误）。`MarketService.fetch_company_profile()`
    返回 `industry / list_date / index_membership` 契约帧，可一次性补齐
    `securities.industry` 与 `index_membership`；全量回填约 5572 次请求（单只约 0.2 秒），
    需要时按 `fundamentals` 阶段的「并发取数 → 串行落库」模式接一个可选阶段，
    接口再失效时仍以 `reference.index_memberships` 的指数成分作为同业分组的替代维度。
14. **Web 分析服务（`serve_web.py`）的并发与锁约束**：
    - 服务只监听 `127.0.0.1`，单用户本地工具，**不设鉴权、不对外暴露**；若将来要开放到局域网，必须先补鉴权。
    - **两把进程内串行锁，全在 `app.web.store` 里，新增接口必须经 `store`，不要绕过它直连 facade**：
      - `_FACADE_LOCK`：包住「单例门面的创建 + 取数 + 可能的回写」；门面是**进程级单例**，只在首次建连一次，
        连接失效才重建（修复了「每请求重建门面反复抢写锁」——初版实测每个请求都要重建一次）。
      - `_STORE_LOCK`：包住全市场 / 指数聚合连接的全部查询。
      - 两把锁**互不嵌套**，纯内存统计（analyzer）在锁外并行。实测混合 36 个并发请求全部 200。
    - **DuckDB 对数据库文件是进程级独占锁，服务会长期持有**：
      - 服务运行期间，**任何**外部进程（`sync_market_data.py`、`run_research.py`、`tests/test_web_api.py`、
        `verify_market_sql.py` 等）连**只读连接**都会被拒，报 `Could not set lock on file`。
      - 因此这些脚本**必须先停服务**再跑；相关脚本已对锁冲突给出可操作提示而非抛栈。
      - 反向也成立：外部进程持锁时服务启动即失败。
      - pytest 侧已把这条约束自动化：`tests/conftest.py` 的 `app` fixture 会 `pgrep -f
        serve_web.py`，命中就 `pytest.fail("serve_web.py 正在运行……请先 pkill -f serve_web.py")`，
        而不是让人去猜 `Could not set lock` 是谁占的。
        另注意 `pytest -n 4`（xdist）下 `test_web_api.py` 不能与任何其他用例并发跑 ——
        真实库只允许一个写连接。
    - **同进程内不允许混合配置的连接**：门面是写连接时，同进程再开 `read_only=True` 会直接报
      `Can't open a connection to same database file with a different configuration than existing connections`。
      所以 `store` 的聚合连接也必须用默认写连接（初版踩过此坑，表现为「门面一打开，Dashboard 与指数接口全返回空」）。
    - **上游 `securities` 表的 `industry` / `area` / `is_hs` 全表为空**：
      这三列由名录阶段写入但名录不提供，页面不查也不展示；
      `list_date` / `delist_date` / `status` 必须先跑
      `sync_market_data.py lifecycle`（阶段七）才有值，Web 层读 `status`
      （`list_status` 已由迁移 003 删除，同一含义不留两列）。
    - 未知代码或远端回退时接口耗时可达数秒（实测 5.7s），属正常现象，前端已带 loading 态与 15~30s 超时。
15. **七档评级只属于 Web 展示层**：`store.percentile_level()` 与 `static/common.js` 的 `LEVEL7` 必须保持
    完全一致（`tests/test_web_api.py` 既解析 JS 阈值、又用 node 真实执行 `levelOf()` 双向校验）。
    `stocklab.analytics` 内部仍是三档（≤30 / 30~70 / ≥70），三档用于档位结论、七档仅用于页面展示，互不替代。
16. **全市场分位走 SQL、个股详情走 analyzer，两者口径必须恒等**：
    5572 只 × 平均 794 行 = 442 万行，逐只调 analyzer 不可行，故用一条窗口函数
    （`store._MARKET_SQL`）一次算完（实测 0.13s）。改动该 SQL 后**必须**重跑
    `app/scripts/verify_market_sql.py 300` 做抽样比对。
17. **数据库结构只由迁移文件定义**：
    - `stocklab/persistence/migrations/NNN_*.sql` 是 DDL 的唯一真相；改表结构必须**新增**
      `NNN_描述.sql`，禁止直接改 `storage/schema.py`（该文件只剩 `initialize_database()` 的委托）。
    - `SchemaMigrator` 在每次打开/初始化数据库时比对 `sys.schema_version`，按序补跑未应用迁移；
      每个迁移只执行一次，执行失败即中止且**不记版本**（修复文件后重跑即可，故迁移文件自身必须幂等，
      新建表一律 `IF NOT EXISTS`；`ALTER TABLE` 只能写在基线 `001` 之后的迁移里）。
    - `001_initial.sql` 是机制上线前的既有结构，全部 `IF NOT EXISTS`，老库补跑不会报错、
      新库与老库走完全相同的升级路径；两个迁移文件同号会直接 `RuntimeError`。
    - 自检：`tests/test_migrations.py`（新库 / 幂等 / 老库升级 / 失败不记版本 / 序号重复）。
18. **数据契约是「源 → 库」的唯一闸门**：
    - 新增表必须同时落三处：`stocklab/domain` 契约元组、`migrations/NNN_*.sql` DDL、
      `repository/` 的 `_TABLE_NAME` + `_COLUMNS`；`tests/test_data_contract.py` 会比对
      契约列名集合与 DDL 列名集合是否完全一致。
    - 归一化层（`stocklab.normalization`）在源表缺列时抛 `DataContractError`，
      `fetch_*` 捕获后记 ERROR 并返回空表，`sync_*` 把空表判为该阶段失败（退出码 1）——
      **绝不产出缺列帧**，也绝不把半批数据写进库。
    - Repository 写入一律「契约对齐 → 显式列名 INSERT ... ON CONFLICT」，因此 DataFrame 列序任意、
      契约外列被丢弃、缺契约列直接拒绝；`conflict_columns` / `update_columns` 必须属于契约。
19. **构造 DataFrame 必须用 `pd.DataFrame(dict)` 或 `pd.DataFrame([dict])`**：
    逐列赋值时若先给标量再给 Series，标量列会被广播成全 NaN（实测导致日 K 的 `ts_code`
    全 NaN → DuckDB 主键拒绝 → 整批静默写 0 行）。
20. **证券生命周期的字段归属（两套写入不可合并）**：
    - `SecurityRepository.upsert()`（名录阶段）只更新 `symbol/name/exchange/market/industry/area/is_hs`；
    - `SecurityRepository.upsert_lifecycle()`（生命周期阶段）只更新 `list_date/delist_date/status`，
      并把退市日历中、名录里没有的退市股整行插入（否则退市股永远进不了样本池）；
    - 合并的后果：名录帧的日期恒为空，一并更新会把生命周期阶段回填的日期抹成 NULL。
    - `securities.status` 取值见 `SECURITY_STATUSES`（`list_status` 已由迁移 003 删除）。
      历史研究用 `SecurityRepository.universe(as_of_date)` 取 as-of 股票池，
      **禁止拿「当前在市列表」当历史样本**（幸存者偏差）。自检：`tests/test_pit_universe.py`。
21. **Point-in-Time 查询必须带 `available_date`**：`fundamental` 四张表的 `find_as_of` /
    `latest_as_of` / `cross_section_as_of` 一律以**公告日**为可见性判据——报告期早于 as-of
    不等于当时已知（4 月才公告的年报，在 3 月底的回测里就是未来信息）。
22. **`fundamentals` 阶段不参与一键全跑**：单只约 60 次 HTTP 请求，全市场约 35 万次（数小时量级）。
    与阶段四一致，**并发只用于取数、落库必须串行**（DuckDB 连接非线程安全）；
    支持 `--ts-code`（逗号分隔，单只验证）与 `--workers`（默认 8）。
23. **行业估值接口的日期参数是 `YYYYMMDD`**：巨潮按 `date[:4]+date[4:6]+date[6:]` 拼接，
    传 `2026-09-30` 会拼成非法日期并抛 `KeyError 'records'`（表现为整阶段无数据）。
    `fetch_industry_valuation` 内部已统一转换，CLI 的 `--stat-date` 两种格式都能用。
24. **因子与筛选是纯计算，取数只在 `stocklab.research.frame`**：
    - 因子输入帧的契约是「**一行一标的** + 所选因子的全部输入列」：`build_factor_frame`
      先按 as-of 拼数、补齐缺数据源的列，最后检查重复 `ts_code`（出现即抛 `FactorDataError`）。
    - **两种失败必须严格区分**：帧里缺列（构造方漏了列）→ `FactorDataError`，属编程错误；
      帧里是 NaN（当时确实没有数据）→ 筛选一律判不通过，`failed_rules` 写明「无数据」，
      **绝不静默放行，也绝不拿近似值顶替**。
    - 预处理引用的非因子列（如 `market_cap_neutralize` 要的 `total_mv`、行业列）
      由 `ScreenPipeline.input_columns()` 报出，取数方必须一并放进帧，否则执行期直接报缺列。
25. **Phase 2 按「因子库先行、数据后补」交付，当前数据缺口必须知道**：
    - 可用的：`market.valuation_history` 440 万行（pe_ttm / pb / ps）→ 估值类因子有数；
      `fundamental` 四表只有 600519.SH；`market.daily_prices` 只有 3 行、
      `market.daily_valuations` 只有 3 行 → 动量 / 波动 / 回撤、股息率 / 市值、
      质量 / 成长 / 现金流类因子在全市场筛选里几乎全部报「无数据」
      （`configs/screen_value.json` 实跑就是 0 通过，原因逐只写在 `failed_rules`）。
    - **没有数据源、一律按 NaN 落地的输入列**：`ebitda`、`dps`、`dps_prior_year`、
      `dividend_years_paid`、`dividend_years_total`（公司行为数据 `domain/corporate_action.py`
      尚未接入）；构造帧时会记 WARNING。
    - 补数据路径：`sync_market_data.py prices / valuations / fundamentals`；
      当前环境下 `push2.eastmoney.com` 与 `push2his.eastmoney.com` 经 Proxy 被拒
      （`ProxyError`），`prices` / `valuations` 两阶段跑不通；可用的替代通道是
      腾讯 `fetch_kline(period="day")`（约 800 根 ≈ 3.3 年）与 baostock。
26. **研究快照的复现约定**：
    - 快照**只写不改**：重复执行同一研究产生新的 `snapshot_id`，历史结论不被覆盖；
      `spec_json` 同时存了**展开后的 ts_code 列表**与筛选条件，重跑按它逐只对齐。
    - `rerun` = 按 spec 重新生成 → 与存档逐行比对，库里的数据一旦变化就如实报差异
      （退出码 1，可进 CI）；`data_version` = `schema_v{N}@{as-of 前最近一个估值交易日}`，
      `factor_version` = `stocklab.factor.FACTOR_VERSION`，`config_version` = 条件内容哈希。
    - `run_research.py` 要写库，**必须先停 `serve_web.py`**（见第 14 条的 DuckDB 写锁）。
27. **回测层（`stocklab.backtest`）是纯计算，撮合口径约定**：
    - 只吃四个帧：`prices` / `signals` / `dividends` / `benchmark`，**不联网、不查库**；
      行情、信号怎么来由调用方（research 层）负责，回测结果是否落库也由调用方决定。
    - **信号 = 当日完整目标权重**（`0 <= weight <= 1`、同一日合计 `<= 1`，A 股不做空），
      持仓里没出现的标的按 0 处理即清仓；信号日必须是行情里的交易日，否则直接报错——
      绝不把信号悄悄挪到别的日子。
    - **成交日恒为信号日的下一个交易日、一律按收盘价、先卖后买**：当天算的信号当天成交
      这种前视偏差在结构上不可能；每日顺序固定为
      `unlock()`（T+1 解锁）→ 分红入账 → 撮合昨日委托 → 计值并下单。
    - 买入按手（`lot_size`，默认 100 股）取整；现金不够时**部分成交**，
      成交数量与「部分成交」原因都写进 `orders`，不静默少买。
    - **买不进 / 卖不出必须留痕**：涨停不可买、跌停不可卖、停牌与退市无价格、
      现金不足一手、T+1 不可卖——全部写进 `orders.reason`，`result.rejected` 可直接查。
      涨跌停先看行情的 `limit_up` / `limit_down` 列，没有才用
      `pre_close × (1±limit_rate)` 按 0.01 取整推导；`limit_rate` 默认 0.10，
      **创业板 30xxxx / 科创板 688xxx 须传 0.20、北交所 0.30**，否则判断偏松。
    - **退市持仓不清零**：仍持有的退市标的按最近价格计值并在 `equity.stale_count` 计数，
      卖出被明确拒绝；股票池用 `universe_as_of`（口径与 `SecurityRepository.universe`
      逐字一致）在信号侧防幸存者偏差。
    - 分红只建模**现金分红**（`cash_per_share` × 除权日持仓），送股转增未建模——
      公司行为数据仍未接入，同第 25 条；过户费（上交所 0.001%）金额远低于佣金，未建模。
28. **回测指标口径（§9.3）**：
    - 统一按 **252 个交易日**年化，`cagr` 是几何（复合）、`annual_return` 是算术（日均 × 252），
      两者不同、都要给；`excess_return` = 策略 CAGR − 基准 CAGR，需在 `run(benchmark=...)` 传入。
    - 胜率 / 盈亏比只认**卖出的已实现盈亏**（平均成本法、已扣买卖全部费用），不用浮动盈亏凑数。
    - **分母为 0 一律 NaN**（无波动 / 无下行 / 无回撤 / 无亏损交易），绝不用 inf 或 0 冒充；
      权益为空 → NaN，有权益但一笔没交易 → 换手 0（有意义的 0，不是缺数据）。
29. **Phase 3 同样是「引擎先行、数据后补」**：
    - 引擎 / 组合 / 成本 / 股票池 / 指标全部用合成数据单测（`tests/test_backtest.py` 七阶段手算核对）；
    - `market.daily_prices` 仍只有 3 行 → 真实回测暂时无从谈起，因此**本阶段没有新增迁移**，
      `BacktestResult` 只在内存，`run_backtest.py` 待价格数据补齐后再接（§11 未要求回测表）；
    - 补数据路径与网络限制见第 25 条。

---

## 许可证

MIT License —— 声明于本文档，**仓库内暂未附 LICENSE 文件**；正式对外分发前需补齐。
