# StockLab 数据库设计与字段说明

> 本文档梳理本地 DuckDB 数据仓库的**设计思想、整体框架、表间关联、字段含义与当前数据实况**。
>
> 统计基准：2026-10-02 ｜ 数据库文件：`data/stocklab.duckdb`（约 144 MB）
> DDL 唯一来源：`stocklab/persistence/storage/schema.py`

---

## 一、设计思想

### 1.1 定位：嵌入式分析型数据仓库

选 DuckDB 而非 SQLite / PostgreSQL，核心原因是**分析型负载**：
全市场 5572 只标的的估值序列共 442 万行，典型查询是「按标的取时间序列」「按日期做横截面聚合」，
这类操作在列存 + 向量化执行的 DuckDB 上效率显著优于行存引擎。

同时它是**单文件嵌入式**——无需部署服务，`git` 可忽略，符合个人量化工具的运维现实。

### 1.2 三条核心设计原则

#### 原则一：数据域分离（Schema 划分）

```
reference  —— 证券身份与静态属性（慢变维表）
market     —— 行情与估值（时间序列事实表）
sys        —— 同步过程状态（运维元数据）
```

分离的理由是**变化频率差异巨大**：`securities` 一年只随上市/退市变动几次，而 `valuation_history` 每天都在增长。
混在一张表里会让静态属性的更新成本不可接受。

> **为什么是 `sys` 而不是 `system`**：`system` 是 DuckDB 保留字，直接用作 schema 名会抛 `BinderException`。

#### 原则二：NULL 语义保留

**估值字段的 NULL 表示「亏损或无数据」，严禁转换为 0。**

这是本库最容易违反、后果最严重的一条纪律。原因：
- `PE = 0` 与 `PE = NULL` 含义完全不同——前者是数学值，后者是「不存在」；
- 若把亏损股的 NULL 填成 0，全市场 PE 中位数会被系统性拉向 0，所有分布统计结论失真；
- 分析器（如 `ValuationPercentileAnalyzer`）依赖「排除非正值」来剔除亏损期，一旦数据被填 0，亏损期就会被当作极低估值计入分母。

**配套约定：数据源不提供的列必须写 NULL，绝不省略列。**
UPSERT 使用 `SELECT *` 按位置匹配，省略列会导致列数不匹配而整体失败。

#### 原则三：幂等写入

所有写入走同一条路径：

```
DataFrame → conn.register(临时表) → INSERT INTO ... SELECT * FROM 临时表
           ON CONFLICT (主键) DO UPDATE SET ...
```

**绝不逐行插入**（性能灾难），**中断重跑不产生重复记录**（由主键 + ON CONFLICT 保证）。

### 1.3 两套估值数据的分工（最关键的区分）

本库存在两张结构相似的估值表，**服务的分析问题完全不同，不可互相替代**：

| 表 | 粒度 | 回答的问题 | 典型用法 |
|---|---|---|---|
| `market.daily_valuations` | 全市场 × 单日 | 「今天谁贵谁便宜」 | 全市场 PE 分布横截面比较 |
| `market.valuation_history` | 单只 × 跨年序列 | 「它相对自己历史上贵不贵」 | 历史分位计算 |

举例说明差异：
- 平安银行 PE-TTM = 5.17，在**全市场分布**里属于极低档；
- 但在**自身 602 个历史采样点**里，它位于 **8.3% 分位**——只比过去 8% 的时间便宜。

前者回答「相对别人」，后者回答「相对自己」。**混用会导致完全错误的结论。**

---

## 二、整体框架

### 2.1 五张表与规模

| Schema | 表名 | 行数 | 主键 | 数据状态 |
|---|---|---:|---|---|
| `reference` | `securities` | 5,572 | `ts_code` | ✅ 全市场完整 |
| `reference` | `index_memberships` | 4,350 | `ts_code`+`index_code`+`effective_date` | ✅ 6 个宽基指数 |
| `market` | `valuation_history` | 4,423,212 | `ts_code`+`trade_date` | ✅ 100% 覆盖 |
| `market` | `daily_valuations` | 3 | `ts_code`+`trade_date` | ⚠️ 东财阻断，仅样本 |
| `market` | `daily_prices` | 3 | `ts_code`+`trade_date` | ⚠️ 东财阻断，仅样本 |
| `sys` | `sync_tasks` | 0 | 无 | ⬜ 预留表，尚无代码写入 |

> 总计约 **443.3 万行**，其中 99.8% 来自 `valuation_history`。

### 2.2 关联关系图

```
                  ┌──────────────────────────┐
                  │  reference.securities    │  ← 全市场证券档案（权威主体表）
                  │  PK: ts_code             │
                  │  5,572 行                │
                  └────────┬─────────────────┘
                           │
                           │ ts_code（唯一关联键，全库通用）
                           │
        ┌──────────────────┼──────────────────┬────────────────────┐
        │                  │                  │                    │
        ▼                  ▼                  ▼                    ▼
┌────────────────┐ ┌──────────────────┐ ┌────────────────┐ ┌──────────────────────┐
│ market.        │ │ market.          │ │ market.        │ │ reference.           │
│ daily_prices   │ │ daily_valuations │ │ valuation_     │ │ index_memberships    │
│ 日K（不复权）  │ │ 全市场估值快照    │ │ history        │ │ 指数成分             │
│ 3 行           │ │ 3 行             │ │ 历史估值序列   │ │ 4,350 行             │
│ PK(2列)        │ │ PK(2列)          │ │ 4,423,212 行   │ │ PK(3列)             │
│                │ │                  │ │ PK(2列)        │ │                      │
└────────────────┘ └──────────────────┘ └────────────────┘ └──────────────────────┘
      ts_code + trade_date 为主键，均以 ts_code 关联 securities


  ┌────────────────────────────────────────────────┐
  │  sys.sync_tasks（预留，当前 0 行）              │
  │  用途：记录各阶段同步任务的状态与行数           │
  │  现状：断点续传实际由 DailyPriceRepository      │
  │        .get_max_trade_date() 实现              │
  └────────────────────────────────────────────────┘
```

### 2.3 关联关系的三条规则

1. **`ts_code` 是全库唯一关联键**，格式统一为 `{6位数字}.{交易所后缀}`，如 `600519.SH` / `000001.SZ` / `430047.BJ`
2. **不存在物理外键约束**——DuckDB 对 FK 支持有限，且批量 UPSERT 下 FK 校验成本高。关联完整性由**同步脚本保证**（实测差集为 0）
3. **`securities` 是权威主体表**：其他表的 `ts_code` 必须在其中存在，否则为孤儿记录（实测 0 条）

---

## 三、逐表字段说明

### 3.1 `reference.securities` — 证券基础信息

**粒度**：一只证券一行 ｜ **5,572 行** ｜ **主键**：`ts_code`

| 字段 | 类型 | 空 | 当前填充 | 含义与说明 |
|---|---|---|---:|---|
| `ts_code` | VARCHAR | 否 | 100% | **标准证券代码**，`{数字}.{交易所后缀}`。PK，由纯数字代码 + 交易所后缀归一化生成 |
| `symbol` | VARCHAR | 是 | 100% | 6 位纯数字代码，不含后缀 |
| `name` | VARCHAR | 是 | 100% | 证券简称。**保留 `*ST` / `ST` / `XD` 等状态前缀，原样不做清洗** |
| `exchange` | VARCHAR | 是 | 100% | 交易所：`SH` / `SZ` / `BJ` |
| `market` | VARCHAR | 是 | 100% | 市场：`SH` / `SZ` / `BJ`。当前与 `exchange` 同值，两字段语义重叠 |
| `industry` | VARCHAR | 是 | **0%** | 行业分类。⬜ **当前数据源不可用**（巨潮接口已需授权） |
| `area` | VARCHAR | 是 | **0%** | 地区。⬜ 同上，当前不可用 |
| `list_date` | DATE | 是 | **0%** | 上市日期。⬜ 同上，当前不可用 |
| `delist_date` | DATE | 是 | **0%** | 退市日期。⬜ 同上，当前不可用（当前无退市记录） |
| `list_status` | VARCHAR | 是 | 100% | 上市状态。当前全部为 `'L'`（上市中），无退市样本 |
| `is_hs` | VARCHAR | 是 | **0%** | 沪深港通标记。⬜ 当前不可用 |

**当前分布**：

| 交易所 | 家数 |
|---|---:|
| SH（上交所） | 2,320 |
| SZ（深交所） | 2,904 |
| BJ（北交所） | 348 |

**已可用于生产的字段**：
- `name` 含 `ST` 字样的有 **201 只**（如 `ST嘉应`、`*ST天喻`）→ **ST 风险可离线识别，无需额外数据源**
- `market` 可做交易所分组统计

**⬜ 待补齐的四个字段**：
`industry` / `area` / `list_date` / `is_hs`。
实现已就绪（`MarketDataProvider.fetch_company_profile()`），待数据源开放后可直接灌入。
在此之前，**用 `index_memberships` 的指数成分作为同业分组的替代维度**。

---

### 3.2 `reference.index_memberships` — 指数成分股

**粒度**：某标的 × 某指数 × 某生效日 ｜ **4,350 行** ｜ **主键**：`(ts_code, index_code, effective_date)`

| 字段 | 类型 | 空 | 当前填充 | 含义与说明 |
|---|---|---|---:|---|
| `ts_code` | VARCHAR | 否 | 100% | 证券代码，关联 `securities.ts_code` |
| `index_code` | VARCHAR | 否 | 100% | 指数代码，如 `000300` |
| `index_name` | VARCHAR | 是 | 100% | 指数名称，如 `沪深300` |
| `effective_date` | DATE | 否 | 100% | 成分生效日期（调样日） |

**当前覆盖的 6 个宽基指数**：

| 指数代码 | 指数名称 | 成分数 |
|---|---|---:|
| `932000` | 中证2000 | 2,000 |
| `000852` | 中证1000 | 1,000 |
| `000905` | 中证500 | 500 |
| `000510` | 中证A500 | 500 |
| `000300` | 沪深300 | 300 |
| `000016` | 上证50 | 50 |

合计覆盖全市场 **3,826 只（68.7%）**，其余 1,746 只属中小盘未被宽基纳入。

**为何按 `effective_date` 分区而非只存当前状态**：
指数每年定期调样两次。保留生效日期才能回溯「某只股票在历史某时点属于哪个指数」，
**避免用今天的成分去解释历史估值分位**——这是时间对齐的正确性要求。

**重叠情况**（说明分组维度是正交的）：

| 所属指数数 | 标的数 |
|---:|---:|
| 1 个 | 3,349 |
| 2 个 | 430 |
| 3 个 | 47 |

**解决的选股问题**：
行业分类不可用时，同指数成分股构成可比样本池，可做「指数内估值横向比较」以替代「行业平均估值」。

---

### 3.3 `market.valuation_history` — 历史估值序列 ⭐

**粒度**：某标的 × 某采样日 ｜ **4,423,212 行** ｜ **主键**：`(ts_code, trade_date)`

| 字段 | 类型 | 空 | 当前填充 | 含义与说明 |
|---|---|---|---:|---|
| `ts_code` | VARCHAR | 否 | 100% | 证券代码 |
| `trade_date` | DATE | 否 | 100% | 采样日期。⚠️ **非交易日**，详见下方「采样频率」说明 |
| `pe_ttm` | DOUBLE | 是 | 100% | 市盈率 TTM，滚动 12 个月。**跨期可比，分位计算的默认口径** |
| `pe_static` | DOUBLE | 是 | 100% | 市盈率（静），基于最近年报。跨期不可比，仅供参考 |
| `pb` | DOUBLE | 是 | 100% | 市净率 |
| `ps` | DOUBLE | 是 | **0%** | 市销率。⬜ **数据源不提供，整列 NULL** |
| `pcf` | DOUBLE | 是 | 100% | 市现率 |

**数据规模**：

| 项 | 值 |
|---|---|
| 记录总数 | 4,423,212 |
| 覆盖标的 | **5,572 / 5,572（100%）** |
| 时间跨度 | 1991-03-19 ~ 2026-10-01 |
| 同步失败 | 0 |

**⚠️ 采样频率说明（重要，容易误读）**：
数据源为百度股市通 `stock_zh_valuation_baidu`。实测该接口的 `period="全部"`
返回的**不是逐日数据，而是约每半月的采样点**——因此：

- 单只标的记录数为 2 ~ 1,676 个采样点（不是 35 年 × 240 交易日 ≈ 8,400）
- **约 27.9% 的记录落在周六/周日**（因采样按自然日对齐）
- **对分位计算无实质影响**：分位 =「低于当前值的样本占比」，采样密度不改变分布形状
- **但报告措辞须准确**：应表述为「N 个历史采样点」，而非「N 个交易日」

**⚠️ 非正值的处理**：
- `pe_ttm < 0` 共 **647,850 条**，代表**亏损期**——PE 为负没有估值比较意义
- 分析器 `ValuationPercentileAnalyzer` 已将 `PE <= 0` 排除出分位计算的分母
- 负值**保留在库中不做篡改**，只在统计口径上排除

实测效果（金科股份）：606 个采样点中 PE-TTM 仅 **402 个有效**，排除 204 个亏损期。

**已知数据盲区**：

| 盲区 | 标的数 | 说明 |
|---|---:|---|
| `pe_ttm` 恒为负 | 29 只 | 持续亏损公司（未盈利生物医药、AI 芯片等），无法计算 PE 分位。分析器会正确返回 `is_available() == False`，不会给出错误分位 |
| `ps` 全 NULL | 全市场 | 数据源不支持该指标。如需市销率分位须另找数据源 |

---

### 3.4 `market.daily_valuations` — 全市场估值快照

**粒度**：全市场 × 单日 ｜ **当前 3 行** ｜ **主键**：`(ts_code, trade_date)`

**⚠️ 当前状态：仅样本数据。** 数据源为东方财富 `stock_zh_a_spot_em`（全市场实时快照，1 次请求），
但该接口在当前环境下被阻断，未能灌入全量。表结构与 Repository 已完整就绪，数据源恢复后执行
`./venv/bin/python scripts/sync_market_data.py valuations` 即可灌满。

| 字段 | 类型 | 含义与说明 |
|---|---|---|
| `ts_code` | VARCHAR | 证券代码 |
| `trade_date` | DATE | 快照写入日期。**不一定是真实交易日** |
| `turnover_rate` | DOUBLE | 换手率（%） |
| `turnover_rate_f` | DOUBLE | 自由流通换手率。⬜ 数据源未提供 → NULL |
| `pe` | DOUBLE | 市盈率（动态） |
| `pe_ttm` | DOUBLE | 市盈率 TTM |
| `pb` | DOUBLE | 市净率 |
| `ps` | DOUBLE | 市销率。⬜ 数据源未提供 → NULL |
| `ps_ttm` | DOUBLE | 市销率 TTM。⬜ 数据源未提供 → NULL |
| `dv_ratio` | DOUBLE | 股息率。⬜ 数据源未提供 → NULL，**需自行计算** |
| `dv_ttm` | DOUBLE | 股息率 TTM。⬜ 同上 |
| `total_share` | DOUBLE | 总股本。⬜ 数据源未提供 → NULL |
| `float_share` | DOUBLE | 流通股本。⬜ 数据源未提供 → NULL |
| `free_share` | DOUBLE | 自由流通股本。⬜ 数据源未提供 → NULL |
| `total_mv` | DOUBLE | 总市值（元） |
| `circ_mv` | DOUBLE | 流通市值（元） |

> **⚠️ 列序即契约**：本表 16 列的顺序与 `MarketDataProvider.fetch_realtime_valuations()`
> 的 DataFrame 输出**严格同名同序**。UPSERT 使用 `SELECT *` 按位置匹配，
> **修改任一侧必须同步修改另一侧**，否则会因列数不匹配导致整体写入失败。

> **股息率说明**：所有数据源都不提供股息率这个派生指标（东财不映射、百度接口抛 TypeError、腾讯 88 字段无此语义）。
> 需由「每股股利 ÷ 当前股价」组合计算。

---

### 3.5 `market.daily_prices` — 日 K 行情

**粒度**：某标的 × 某交易日 ｜ **当前 3 行** ｜ **主键**：`(ts_code, trade_date)`

**⚠️ 当前状态：仅样本数据**，同为东财阻断所致。

| 字段 | 类型 | 含义与说明 |
|---|---|---|
| `ts_code` | VARCHAR | 证券代码 |
| `trade_date` | DATE | 交易日期 |
| `open` / `high` / `low` / `close` | DOUBLE | 开 / 高 / 低 / 收（**不复权价**，`adjust=""`） |
| `pre_close` | DOUBLE | 昨收 |
| `change` | DOUBLE | 涨跌额 |
| `pct_chg` | DOUBLE | 涨跌幅（%） |
| `volume` | DOUBLE | 成交量 |
| `amount` | DOUBLE | 成交额 |

> **价格固定为不复权**：跨越除权日会失真。若需计算复权价或以价格为分母的指标
> （如「股息率 = 每股股利 ÷ 股价」），应在查询层处理，不在存储层冗余。

---

### 3.6 `sys.sync_tasks` — 同步任务状态（预留）

**当前 0 行，尚无代码写入。**

| 字段 | 类型 | 含义与说明 |
|---|---|---|
| `task_id` | BIGINT | 任务序号 |
| `data_type` | VARCHAR | 数据类型：`securities` / `prices` / `valuations` |
| `start_date` / `end_date` | DATE | 同步日期范围 |
| `status` | VARCHAR | 状态：`running` / `success` / `failed` |
| `row_count` | BIGINT | 写入行数 |
| `started_at` / `finished_at` | TIMESTAMP | 起止时间 |
| `error_message` | VARCHAR | 失败原因 |

**现状说明**：这是为任务级审计预留的表。**断点续传目前由 `DailyPriceRepository.get_max_trade_date()`
实现**（查本地最大交易日后增量同步），尚未接入本表。若后续需要任务级审计可在此落地。

---

## 四、字段填充率总览

用 `COUNT(NULLIF(TRIM(col),''))` 排除空字符串后的**真实**填充率：

### `reference.securities`（5,572 行）

| 字段 | 填充率 | 状态 |
|---|---:|---|
| `ts_code` / `symbol` / `name` | 100% | ✅ |
| `exchange` / `market` | 100% | ✅ |
| `list_status` | 100% | ✅（全为 `'L'`） |
| `industry` / `area` | **0%** | ⬜ 数据源不可用 |
| `list_date` / `delist_date` / `is_hs` | **0%** | ⬜ 数据源不可用 |

### `market.valuation_history`（4,423,212 行）

| 字段 | 填充率 | 状态 |
|---|---:|---|
| `pe_ttm` / `pe_static` / `pb` / `pcf` | 100% | ✅ |
| `ps` | **0%** | ⬜ 数据源不支持 |

### `reference.index_memberships`（4,350 行）

| 字段 | 填充率 | 状态 |
|---|---:|---|
| `index_code` / `index_name` / `effective_date` | 100% | ✅ |

---

## 五、数据完整性核验

### 5.1 已验证通过的项

| 核验项 | 结果 |
|---|---|
| `securities` 与 `valuation_history` 差集 | **0**（5572 = 5572） |
| 孤儿记录（估值有档案无） | **0** |
| 同步失败标的 | **0** |
| 指数成分中有历史估值的比例 | **3,826 / 3,826（100%）** |
| 重复同步行数是否变化 | 幂等（4,350 行不变） |

### 5.2 已知待处理项

| 问题 | 影响 | 建议 |
|---|---|---|
| 27.9% 记录落在周末 | **对分位计算无影响**，但报告措辞易误导 | 修正表述为「采样点」而非「交易日」 |
| 118 只标的样本 < 250 个采样点 | 分位结论稳定性不足 | 需进一步区分「次新股」与「数据异常」 |
| 29 只标的 PE 恒为负 | 无法计算 PE 分位 | 属正确行为（持续亏损），非缺陷 |
| `industry` 全空 | 同业比较缺「行业」维度 | 暂用指数成分替代；待数据源开放后补齐 |

---

## 六、同步与访问路径

### 6.1 同步命令

```bash
./venv/bin/python scripts/sync_market_data.py securities          # 阶段一：证券档案
./venv/bin/python scripts/sync_market_data.py prices --incremental # 阶段二：日 K
./venv/bin/python scripts/sync_market_data.py valuations          # 阶段三：估值快照
./venv/bin/python scripts/sync_market_data.py indexes             # 阶段五：指数成分
./venv/bin/python scripts/sync_market_data.py valuation-history \
    --period 全部 --workers 8                                     # 阶段四：历史估值（耗时最长）
```

不带子命令时按顺序执行阶段一 → 二 → 三 → 五（**阶段四默认不参与**，因耗时以小时计）。

### 6.2 分层约束（写代码前必读）

```
stocklab.persistence   —— 只负责「往本地存数」
  ├─ 不 import 任何数据源（AkShare / BaoStock / 百度 / 腾讯）
  └─ 仅依赖 pandas 与本层 storage/，保证可独立测试与移植
```

Repository 层因此可以在没有网络的情况下完整测试。

### 6.3 并发约束（阶段四特有）

DuckDB **连接非线程安全**。阶段四采用：
```
工作线程池 → 取数（并发，线程间无共享状态）
主线程     → upsert（串行，单连接）
```
这是既能并发加速、又不引入数据库并发风险的唯一组合。修改并发逻辑时必须保持这个分工。

---

## 七、常见误用提醒

| 误用 | 正确做法 |
|---|---|
| 用 `daily_valuations` 算历史分位 | 用 `valuation_history`，前者只有单日快照 |
| 用 `valuation_history` 做全市场比较 | 用 `daily_valuations`，后者才有全市场横截面 |
| 把 NULL 当 0 计算均值 | NULL 表示「亏损或无数据」，必须排除 |
| 相信 `ps` 字段有值 | 数据源不支持，恒为 NULL |
| 认为 `trade_date` 是交易日 | 历史表是**采样日**，可能落在周末 |
| 直接改 `valuation_history` 列顺序 | 必须同步改 `fetch_valuation_history()` 的输出顺序 |
| 写入时省略数据源不提供的列 | 必须保留并写 NULL，否则 UPSERT 列数不匹配而整体失败 |

---

## 八、演进方向

当前数据覆盖可支撑**「低估值」单一维度**的量化分析（历史分位 + 指数内横向比较）。

尚未覆盖的选股维度及所需数据：

| 维度 | 所需表 | 数据源 | 优先级 |
|---|---|---|---|
| 高 ROE | 财务指标表（ROE / 毛利率 / 资产负债率） | 新浪 `stock_financial_analysis_indicator` | 高 |
| 高股息 | 分红明细表（每股股利 / 报告期） | 巨潮 `stock_dividend_cninfo` | 高 |
| DCF 前提 | 三大报表（利润表 / 资产负债表 / 现金流量表） | 新浪 `stock_financial_report_sina` | 中 |
| 行业分类 | `securities.industry` 补齐 | 巨潮 `stock_profile_cninfo`（已需授权） | 中 |
| 风险识别 | 已可用 `name` 含 `ST` 离线识别（201 只） | 无需新源 | — |

---

## 附：相关文档

- `AGENT.md` —— 项目架构、分层约定、运行方式（本库的架构上下文在此）
- `stocklab/persistence/storage/schema.py` —— DDL 唯一来源，一切结构以该文件为准
- `stocklab/persistence/repository/` —— 各表的数据访问接口
