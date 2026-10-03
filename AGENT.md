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
| `app.web` | `facade` `analytics`（层内互引 `web`） | `flask` | `logging` `os` `threading` |

> `stocklab.persistence` **不依赖 `common`**：持久化层无配置语义，解析 `config.ini` 对它没有意义。

- `stocklab/common`：无业务依赖的通用工具（配置解析、类型转换、HTTP 全局配置）。
  - `http_client.py`：浏览器 UA 补丁，**由入口脚本显式调用**，导入本包不产生任何全局副作用。
- `stocklab/datasource`：**只负责对外取数**，不感知本地存储。内部按用途分三块：
  - **分析/行情取数**（单股维度、三级降级）：`quote_service.py`（`StockQuoteService`：月线价格 / PE-TTM / 简称 / 实时行情；价格通道 AkShare → BaoStock → 腾讯，估值通道 AkShare → BaoStock）→ `_sources/`（三个通道的私有实现，外部勿依赖）。
  - **入库取数**（多粒度、单源直连）：`market_service.py`（`MarketService`：7 个 `fetch_*` 方法与 7 张库表一一对应，输出与表**严格同名同序**供 UPSERT 按位置写入；粒度含全市场快照 / 单股序列 / 行业横截面 / 指数成分四种，直连各 akshare 接口（自带重试与限流），**不做通道降级**。调用方仅两个：`app/scripts/sync_market_data.py` 与 facade 远端分支（Cache-Aside 回写本地库）。
  - **公共基础**：
    - `data_contract.py`：行情数据契约（3 个列名常量 + `StockRealtimeQuote`），`quote_service` 与 `_sources` 共用；单独成文件是为避免「通道 import 服务、服务又 import 通道」的循环导入（类比 C++ 只含 struct + constexpr 的公共头文件）。
    - `tencent_client.py`：腾讯 HTTP 传输网关（`TencentMarketClient` + `normalize_symbol`），抹平股票/ETF/指数代码差异，提供 K 线 / 简称 / 盘口原始字段与多标的**并发**抓取。独立于 `_sources` 之外的原因：能力超出单股 `StockDataSource` 契约（ETF/指数 + 并发），且被两个上层独立复用——`_sources/tencent_source`（单股降级第三级）与 `facade.fetch_multi_monthly_close`（dashboard 板块走势 10 标的并发月线），故置于通道之下单独一层。
- `stocklab/persistence`：**只负责本地落库**，既不依赖 `common`，也不依赖任何外部数据源（AkShare / BaoStock / 腾讯）。
  - `storage/`：DuckDB 连接管理与 Schema 定义。
  - `repository/`：SQL 读写封装，仅依赖 pandas 与本层 `storage/`。
- `stocklab/facade`：**统一取数入口**，同时依赖 `datasource` 与 `persistence`，负责按优先级在两者间路由与回退。
- `stocklab/analytics`：**纯统计变换层**，只接收 DataFrame 做聚合，不取数、不落库、不 import 上游三层。
- `app/dashboard`：把数据渲染成网页。
- `app/web`：**本地 Web 分析服务**（Flask），把已有分析能力以 HTTP 接口暴露给浏览器，四个页面共用一套数据层：
  - **个股分析 `/`**：输入代码或中文名 → 分位徽章 + 0-100 温度条 + 多窗口分位对比 + 双 y 轴走势图（10/50/90 分位参考线与 25%~75% 分位带）+ 指标明细表。
  - **全市场 Dashboard `/market`**：5 指标切换、市场/搜索/评级过滤、七档评级分布（可点击钻取）与分位直方图、可排序分页排行表（点行跳个股页）。
  - **指数估值 `/indices`**：6 大宽基指数卡片（成分中位数口径）+ 点击展开走势图。
  - **行业估值 `/industries`**：国证行业分类 1~4 级横截面，PE 三种口径 + 规模数据，条形图 + 明细表（板块洼地判断）。
  - `server.py`：`create_app()` 装配层——**路由一律用 `app.add_url_rule()` 注册表写法，不用 `@app.route` 装饰器**（遵守本文件禁用装饰器的规定）。
  - `store.py`：**进程级数据访问单例**——(1) 单例门面 + 串行锁，修复「每请求重建门面反复抢写锁」；(2) 全市场窗口函数 SQL 与指数聚合，走本模块自己的单例连接 + 第二把锁，与门面锁互不嵌套；(3) 七档评级 `percentile_level()`、证券表与聚合结果的进程内缓存；(4) 行业估值横截面 `load_industry_valuation()` 与数据截止日期 `market_data_as_of()`。
  - `api.py`：个股接口层——只做「参数解析 → 代码/名称解析 → store 取数 → analyzer 计算 → 组装 JSON」，纯内存统计放在锁外；`/api/health` 额外返回 `data_as_of` 数据截止日期。
  - `market_api.py`：全市场与指数接口层——参数校验 → store 聚合 → 过滤/排序/分页，不经门面锁；`/api/market/ranking` 支持 `level` 七档评级过滤（summary 仍按过滤前口径统计）；`/api/industries` 行业横截面。
  - `static/`：`base.css` 设计系统、`common.js` 共享工具（请求超时、七档配色、温度条、七档图例、健康检查、ECharts option 工厂）与各页脚本；`templates/`：四个页面模板 + `_topbar.html` / `_footer.html` 共享 partial。
  - **依赖方向 `app.web → stocklab.facade / stocklab.analytics`（`store.py` 另直接用 `duckdb` 做只读聚合），与 `app.dashboard` 平行，不修改 `stocklab/` 核心库任何文件。**
  - **七档评级仅存在于 Web 展示层**（`store.percentile_level` 与 `static/common.js` 的 `LEVEL7`，两侧口径由 `tests/test_web_api.py` 双向校验）；`stocklab.analytics` 内部仍是三档结论。
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
│   │   ├── data_contract.py              #     统一数据契约：列名常量 + StockRealtimeQuote（无依赖，单股/入库两服务共用）
│   │   ├── tencent_client.py           #     【腾讯传输网关】TencentMarketClient（股票/ETF/指数统一接入）
│   │   ├── quote_service.py            #     【单股行情服务】StockQuoteService：三级降级编排 + 数据契约 re-export
│   │   ├── market_service.py             #     【入库取数】MarketService：7 个 fetch_* 与 7 张库表同名同序（粒度混合，非仅全市场）
│   │   └── _sources/                   #     单股通道实现包（下划线前缀 = 私有，外部勿依赖）
│   │       ├── __init__.py             #       导出抽象基类与三个通道实现
│   │       ├── base.py                 #       StockDataSource 抽象基类（纯虚接口 + 标准化/降采样工具）
│   │       ├── akshare_source.py       #       东方财富主通道（akshare，含重试与列名防御）
│   │       ├── baostock_source.py      #       证券宝备用通道（专有 Socket + login/logout 会话管理）
│   │       └── tencent_source.py       #       腾讯直连通道（实时行情/简称/备用日线降采样）
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
│   ├── web/                            #   本地 Web 分析服务（Flask，浏览器端点击分析）
│   │   ├── __init__.py                #     导出 create_app
│   │   ├── server.py                  #     create_app 装配：add_url_rule 路由注册表（非装饰器），页面 4 + 接口 7 + 图标 1
│   │   ├── store.py                   #     进程级数据访问单例：单例门面锁 / 聚合连接锁 / 七档评级 / 行业横截面 / 进程内缓存
│   │   ├── api.py                     #     个股接口：代码与中文名解析 → store → analyzer → JSON
│   │   ├── market_api.py              #     全市场/指数/行业接口：过滤排序分页 → store 聚合 → JSON
│   │   ├── templates/
│   │   │   ├── _topbar.html           #     共享顶栏（导航 / 状态 / 数据截止）
│   │   │   ├── _footer.html           #     共享页脚（口径说明 / 免责声明）
│   │   │   ├── analysis.html          #     个股分析页（温度条 / 多窗口 / 明细表 / 七档图例）
│   │   │   ├── market.html            #     全市场 Dashboard（统计卡 / 分布图 / 排行表 / 分页 / 评级钻取）
│   │   │   ├── indices.html           #     指数估值页（指数卡片 + 走势详情）
│   │   │   └── industries.html        #     行业估值页（层级切换 / PE 条形图 / 明细表）
│   │   └── static/
│   │       ├── echarts.min.js         #     ECharts 5.5 vendored（本地托管，离线可用）
│   │       ├── base.css               #     共享设计系统：顶栏 / 卡片 / 表格 / 徽章 / 温度条 / 七档色 / 图例 / 页脚
│   │       ├── common.js              #     带超时的请求、格式化、七档配色、七档图例、健康检查、ECharts option 工厂
│   │       ├── app.js                 #     个股页：联想键盘操作 / 分析渲染 / URL 还原
│   │       ├── market.js              #     Dashboard：统计卡 / 直方图 / 评级钻取 / 排行分页
│   │       ├── indices.js             #     指数页：卡片列表 / 详情加载
│   │       └── industries.js          #     行业页：层级切换 / 条形图 / 明细表
│   └── scripts/                       #   CLI 入口
│       ├── generate_sector_trend.py   #     命令行入口：生成板块走势网页
│       ├── sync_market_data.py        #     命令行入口：全市场数据同步到本地 DuckDB（五个阶段）
│       ├── serve_web.py               #     命令行入口：启动本地 Web 分析服务（仅监听 127.0.0.1）
│       ├── verify_market_sql.py       #     抽样校验：全市场分位 SQL 与 analyzer 口径一致（退出码可进 CI）
│       ├── analyze_pe_distribution.py #     命令行入口：全市场市盈率分布统计
│       └── analyze_valuation_percentile.py # 命令行入口：个股历史估值分位计算
├── tests/                              # ── 自检脚本 ──
│   ├── test_data_interfaces.py         #     全链路自检（类型转换 / 跨资产行情 / 实时快照 / 简称 / 月线估值 / 网页生成）
│   └── test_web_api.py                 #     Web 层自检（路由 / 参数校验 / 中文名解析 / 七档前后端一致 / 口径抽样）
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
| **数据源** | `stocklab.datasource.quote_service` | **单股行情服务 `StockQuoteService`**：三通道降级、月线价格+PE对齐、实时行情 |
| | `stocklab.datasource.data_contract` | **数据契约**：列名常量 + `StockRealtimeQuote`，单股行情/入库取数两服务共用 |
| | `stocklab.datasource.tencent_client` | **腾讯传输网关 `TencentMarketClient`**：股票/ETF/指数不区分、并发批量、OHLCV全要素 |
| | `stocklab.datasource.market_service` | **入库取数 `MarketService`**：基础信息/日K/估值快照/历史估值/行业估值/指数成分/公司概况 —— 输出与库表同名同序 |
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
| **Web 服务** | `app.web.server` | `create_app()`：Flask 装配 + `add_url_rule` 路由注册表（无装饰器），页面 4 + 接口 7 + favicon |
| | `app.web.store` | **进程级数据访问单例**：门面锁 / 聚合连接锁、全市场窗口函数 SQL、指数成分中位数序列、七档评级、进程内缓存 |
| | `app.web.api` | 个股接口：`/api/percentile` 分位 + 多窗口、`/api/securities` 联想（排序 + 大小写不敏感）、代码与中文名解析 |
| | `app.web.market_api` | 全市场接口：`/api/market/ranking`（过滤/排序/分页 + 七档分布与直方图）、`/api/indices`、`/api/index/detail` |
| **脚本** | `app/scripts/sync_market_data.py` | 5 阶段同步 CLI：证券/日K/估值快照/历史估值/指数成分 |
| | `app/scripts/generate_sector_trend.py` | 可视化生成 CLI：月数/输出路径/配置文件可配 |
| | `app/scripts/serve_web.py` | 本地分析服务 CLI：端口/优先级/数据库路径可配，仅监听 127.0.0.1 |

---

## 运行方式

```bash
# 启动本地 Web 分析服务（浏览器打开 http://127.0.0.1:8000 点击分析）
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
./venv/bin/python tests/test_data_interfaces.py 000001.SZ

# Web 层自检（会打开本地 DuckDB，须先停止 serve_web.py）
./venv/bin/python tests/test_web_api.py

# 全市场分位 SQL 与 analyzer 口径抽样比对（默认 300 只，非 0 退出码即为不一致）
./venv/bin/python app/scripts/verify_market_sql.py 300

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
| **浏览器点开分析** | `python app/scripts/serve_web.py` → `/` `/market` `/indices` `/industries` | store(单例锁) → facade/analyzer → JSON → ECharts |
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
    HTTP 200 但数据为空。`MarketService.fetch_company_profile()` 已实现，
    在接口开放的环境可直接用于补齐 `securities.industry` / `list_date`；
    接口不可用时以 `reference.index_memberships` 的指数成分作为同业分组的替代维度。
14. **Web 分析服务（`serve_web.py`）的并发与锁约束**：
    - 服务只监听 `127.0.0.1`，单用户本地工具，**不设鉴权、不对外暴露**；若将来要开放到局域网，必须先补鉴权。
    - **两把进程内串行锁，全在 `app.web.store` 里，新增接口必须经 `store`，不要绕过它直连 facade**：
      - `_FACADE_LOCK`：包住「单例门面的创建 + 取数 + 可能的回写」；门面是**进程级单例**，只在首次建连一次，
        连接失效才重建（修复了「每请求重建门面反复抢写锁」——初版实测每个请求都要重建一次）。
      - `_STORE_LOCK`：包住全市场 / 指数聚合连接的全部查询。
      - 两把锁**互不嵌套**，纯内存统计（analyzer）在锁外并行。实测混合 36 个并发请求全部 200。
    - **DuckDB 对数据库文件是进程级独占锁，服务会长期持有**：
      - 服务运行期间，**任何**外部进程（`sync_market_data.py`、`tests/test_web_api.py`、
        `verify_market_sql.py` 等）连**只读连接**都会被拒，报 `Could not set lock on file`。
      - 因此这些脚本**必须先停服务**再跑；相关脚本已对锁冲突给出可操作提示而非抛栈。
      - 反向也成立：外部进程持锁时服务启动即失败。
    - **同进程内不允许混合配置的连接**：门面是写连接时，同进程再开 `read_only=True` 会直接报
      `Can't open a connection to same database file with a different configuration than existing connections`。
      所以 `store` 的聚合连接也必须用默认写连接（初版踩过此坑，表现为「门面一打开，Dashboard 与指数接口全返回空」）。
    - **上游 `securities` 表只有 `ts_code/symbol/name/exchange/market/list_status` 有值**：
      `industry`、`area`、`list_date`、`is_hs` 全表为空（巨潮接口已需授权，见第 13 条），
      因此 Web 层不查也不展示这些字段，避免出现恒为空的「行业 / 上市日期」。
    - 未知代码或远端回退时接口耗时可达数秒（实测 5.7s），属正常现象，前端已带 loading 态与 15~30s 超时。
15. **七档评级只属于 Web 展示层**：`store.percentile_level()` 与 `static/common.js` 的 `LEVEL7` 必须保持
    完全一致（`tests/test_web_api.py` 既解析 JS 阈值、又用 node 真实执行 `levelOf()` 双向校验）。
    `stocklab.analytics` 内部仍是三档（≤30 / 30~70 / ≥70），三档用于档位结论、七档仅用于页面展示，互不替代。
16. **全市场分位走 SQL、个股详情走 analyzer，两者口径必须恒等**：
    5572 只 × 平均 794 行 = 442 万行，逐只调 analyzer 不可行，故用一条窗口函数
    （`store._MARKET_SQL`）一次算完（实测 0.13s）。改动该 SQL 后**必须**重跑
    `app/scripts/verify_market_sql.py 300` 做抽样比对。

---

## 许可证

MIT License —— 声明于本文档，**仓库内暂未附 LICENSE 文件**；正式对外分发前需补齐。
