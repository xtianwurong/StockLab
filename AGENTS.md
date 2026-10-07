# StockLab AGENTS.md

**目的**：让未来 OpenCode 会话快速上手，避开已知坑点。每条只记「不看源码会猜错」的事。

---

## 快速上手命令

| 任务 | 命令 | 关键点 |
|------|------|--------|
| 全量同步核心数据 | `python app/scripts/sync_all.py` | 默认跑 securities→prices→valuations→indexes→lifecycle |
| 仅同步日 K（增量） | `python app/scripts/sync_market_data.py prices --incremental` | 从本地最新交易日次日开始 |
| 同步历史估值（耗时最长） | `python app/scripts/sync_market_data.py valuation-history --period 近五年` | 需先跑 securities |
| 启动 Web 分析服务 | `python app/scripts/serve_web.py` | 默认 127.0.0.1:8000，只监听回环 |
| 离线单测（默认） | `./venv/bin/python -m pytest` | `pytest.ini` 的 addopts 已含 `-m "not integration"`，无需手写；约 1061 用例 / 22s |
| 联网集成测试 | `./venv/bin/python -m pytest -m integration` | 4 个用例，打真实外部源；命令行 `-m` 覆盖 addopts |
| 只跑单个测试文件 | `./venv/bin/python -m pytest tests/test_sync_cli.py -v` | 无需装包，conftest 已加 pythonpath |

> **环境**：Python 3.14.4 + `venv/`（已在 requirements.txt 锁版）。直接用 `./venv/bin/python`。

---

## 关键架构约束（违反即报错）

1. **分层单向依赖**
   - `datasource`（只出不进）↔ `persistence`（只进不出）**互不 import**
   - `facade` 可依赖两者；两者**绝不反向 import facade**
   - `analytics` / `factor` / `screener` / `backtest` / `fundamental`：**纯计算**，零层内依赖，只吃 DataFrame

2. **取数唯一入口**
   - 入口脚本（`app/scripts/`）→ `facade` → `datasource` / `persistence`
   - 上层（dashboard/web）**不直接拼接** datasource 与 persistence

3. **配置只在入口读**
   - `stocklab.common.config.load_ini_config()` 仅在脚本 `main()` 调用
   - `persistence` 层**完全不依赖 common**，解析 config.ini 对它无意义

4. **DuckDB 单写连接**
   - `serve_web.py` 运行时独占 `data/stocklab.duckdb` 写连接
   - 跑 `real_db` 测试前**必须** `pkill -f serve_web.py`
   - `conftest.py` 会主动探测并给出可操作报错

---

## 编码规范（用户硬性要求）

- **无装饰器、无生成器嵌套、无元类、无 async、无海象、无 pattern matching**
- **无复杂类型注解**：禁止 `typing` 模块（`List[Dict[str, Any]]` 这类），函数签名用原生风格，类型在 docstring Args/Returns 说明
- **import 集中文件头**，函数中间不 import
- **最小暴露**：`__all__` 声明公共接口，内部实现 `_` 开头
- **命名严谨**：Gateway/Pipeline/Adapter/Model 反映职责，禁 Manager/Utils/Helper
- **日志**：库只 `getLogger()`，`basicConfig` 只在脚本 `main()` 做；重试单次失败 debug，耗尽才 warning

---

## 测试纪律（conftest.py 已钉住）

- **三层 fixture**：
  - `tmp_db_path` — 只给目录（迁移测试要造残缺旧库）
  - `fresh_db` — 给目录 + 跑完迁移的 Database（新库契约类用例用）
  - `app` / `client` — 真实库的 Flask 应用与测试客户端（仅 `test_web_api.py` 用）
- **Marker**：
  - `integration` — 打真实外部源，**默认被排除**（addopts 的 `-m "not integration"`）；要跑用 `-m integration`
  - `real_db` — 需 `data/stocklab.duckdb`，DuckDB 写锁冲突会在 fixture 里给明确报错
- **一个用例只验一件事**；注释声称的不变量，**断言必须真的在查**
- 覆盖率基线 **语句 86% / 分支 76%**（`--cov=stocklab --cov=app` + `--cov-branch`，全量实测合并口径 83.9%）
  门槛按**合并口径**取 `--cov-fail-under=83`（勿套语句口径的 85 —— 分支一开，TOTAL 比语句低约 2 分，写 85 会当场失败）
  新代码没配测试就会掉线
- `pytest.ini` **必须留在项目根目录**：pytest 从 args 公共祖先向上找 ini，放 `tests/` 时 `pytest`（无参）会完全不读配置 —— integration 不排除、`--strict-markers` 失效、覆盖率不测，**且不报错**

---

## 常见坑点速查

| 现象 | 根因 | 规避 |
|------|------|------|
| Web 用例直接失败并提示 `pkill -f serve_web.py` | `app` fixture 前置守卫：服务在跑时只复制主库、不复制 `.wal`，快照可能不完整 | `pkill -f serve_web.py` 再跑测试 |
| `RuntimeError: Working outside of application context` | 直接 import `app.web.store` 而非用 `client` fixture | Web 测试必须用 `client` fixture（自带 app_context） |
| 历史估值分位算错 | 分位算的是**最新一日**值，而非序列最小值 | 看 `valuation_percentile.py` 实现，别凭直觉 |
| `/api/market/ranking` 的 `codes` 过滤失效 | 误写成子串匹配 | 精确过滤：传片段（`60051`）必须 0 条 |
| `compare_api` 走势图填成水平线 | 缺失点做了前向填充 | **必须填 `None` 让图断线**，不前向填充 |
| 估值快照失败却继续跑 | 全跑模式下 valuations 失败只告警（日 K 已落库） | 单独跑 `valuations` 子命令失败才退出码 1 |
| 基本面同步只写了利润表 | 资产负债表缺失时**利润表仍要入库**（指标可事后重算） | 别把三表当原子事务 |
| 公告首次同步不给 `--start-date` 就报错 | 空表拒绝无限回溯 | 首次必填 `--start-date 2015-01-01` |

---

## 数据同步阶段依赖（sync_market_data.py）

```
1. securities    （所有后续阶段前提）
2. prices        （依赖 securities）
3. valuations    （依赖 prices）
4. indexes       （依赖 securities）
5. lifecycle     （依赖 securities）
6. fundamentals  （依赖 securities；可选，最耗时）
7. commodities   （无依赖；可选）
8. insight       （依赖 securities；可选）
9. funds         （可选）
10. announcements（依赖 securities；可选，首次需 --start-date）
```

- **全跑模式**（无子命令）：只跑 1→2→3→4→5
- **阶段失败语义**：
  - securities 失败 = 立刻退出（后续全依赖它）
  - valuations/indexes 失败 = 只告警，继续跑
  - lifecycle 失败 = 必须退出（幸存者偏差研究前提）

---

## Web 服务关键约束

- **路由注册**：`app.add_url_rule()` 表驱动，**禁 `@app.route` 装饰器**
- **错误响应**：各模块自带文案，**禁统一 `bad_request()` 包装**（会藏住关键话）
- **参数校验**：垃圾值**一律 400**，不静默回退默认值（静默回退 = 假阴性）
- **七档评级**：仅存在于 Web 展示层（`store.percentile_level` + `static/common.js`），`analytics` 内部仍是三档
- **暗色主题**：`tokens.css` 唯一定义处，`html[data-theme]` 切换；首屏防闪烁靠内联脚本在 CSS 前写 `data-theme`
- **快捷键**：全站同一按键只能有一个含义（`SL.ui.shortcut` 后注册者胜出）

---

## 投资理念域（insight）硬约束

- **三表分离**：`investors` / `investor_accounts` / `investor_quotes` 变化频率不同，**只 INSERT，纠正靠 `verification` 字段**
- **`verified` 仅 manual 通道能写**；采集器传 `verified` 强制降级为 `unverified`
- **假溯源比无溯源更糟**：`source_url` 允许 NULL，禁用首页/搜索页 URL 凑数
- **凭证缺失必须失败**（抛 `InsightCredentialError`），不许返回空列表
- **`collect` 不指定平台即退出码 2**（防误触全量爬取）
- **UID 必须人工确认**（`configs/insight_sources.json`），猜错会把别人发言记错人名下

---

## 目录速览（只列会改的）

```
app/scripts/          入口脚本（参数解析 + 调用库，无业务逻辑）
app/dashboard/        板块走势 HTML 生成
app/web/              Flask 服务（9 页面：个股/全市场/指数/行业/商品/基金/筛选/对比/组合/理念）
stocklab/
  common/             配置/HTTP/类型转换（无业务依赖）
  domain/             列契约 + 异常（叶子包，零依赖）
  normalization/      源表→契约帧唯一改写点（叶子包，不取数不落库）
  datasource/         远端取数（AkShare/BaoStock/腾讯/新浪/通达信通道 + 基本面/生命周期/公告/商品/基金/洞察）
  persistence/        DuckDB 落库（migrations/ + repository/）
  facade/             统一取数入口（7 门面 + 1 归因引擎）
  analytics/          纯统计变换（估值分位/分布/风格/归因/报告）
  fundamental/        财务指标纯派生
  factor/             27 因子 + 预处理（纯计算，显式 register）
  screener/           结构化筛选（规则/分组/流水线，spec 往返一致）
  backtest/           回测引擎（T+1、先卖后买、成本三段、涨跌停/停牌推导）
  research/           研究编排（因子帧构建/快照管理，唯一「查库/落库」入口）
tests/                990 用例，conftest 集中 fixture
configs/              insight_sources.json / screen_value.json
data/stocklab.duckdb  本地数据仓库（git 忽略 .wal）
```

---

## 环境变量（运行时需知）

| 变量 | 用途 | 缺失行为 |
|------|------|----------|
| `STOCKLAB_XUEQIU_COOKIE` | 雪球采集登录态 | insight 采集报 `InsightCredentialError` 退出码 1 |
| `STOCKLAB_GUBA_COOKIE` | 股吧采集登录态 | 同上 |

> 凭证**不在配置文件**，走环境变量（见 `stocklab/datasource/insight/base.py`）

---

## 版本锁定

`requirements.txt` 用 `==` 全锁（含测试工具），单兵本地工具**不分生产/开发环境**。改版本只改这文件。

---

## Skill 路由与使用规则（改码前必过）

> 本节是**加载门禁**，不是建议：触发词命中却不加载对应 Skill → **不许改代码**。

### 路由表

| 任务涉及 | 必须加载（Skill ID） |
|---|---|
| `tests/`、`pytest`、测试、单元测试、集成测试、fixture、`conftest.py`、mock、patch、parametrization、coverage、测试隔离、异步测试、测试失败、测试重构 | `python-testing-patterns` |
| A股、股票、股票代码、行情、OHLCV、日K、基本面、PE、PB、市值、股票数据 | `alphaear-stock` |

同时命中（例：「测试金融数据」「给 A 股数据加 pytest 测试」）→ **两个都加载**。

### Skill Check（修改代码前输出）

```text
Skill Check

Required Skills:
- python-testing-patterns

Loaded Skills:
- python-testing-patterns

Status:
PASS
```

- Required 中有未加载的 → `Status: FAIL` → **停止改码**，告诉用户缺哪个 Skill
- 加载失败必须如实报 `Required Skill: xxx` + `Status: NOT LOADED`，**不许假装已用**

### 加载真实性

**只有 `skill` 工具真的取回了 SKILL.md 正文，才能写 `Loaded: YES`。**
以下一律不算：已安装、文件存在、名字匹配、出现在可用列表里。
（`skill` 返回体带 `Base directory:` 行即为实证。）

### Skill Usage（任务结束输出）

```text
Skill Usage

Skill:
python-testing-patterns

Loaded:
YES

Applied:
YES
```

多个 Skill 分别列出。

### 优先级与按需加载

```text
本文件（项目级） → StockLab 专属 Skill → 领域 Skill → 通用工程 Skill
```

**只在任务相关时加载，不全量加载**：改 README 不必加载金融 Skill；修 pytest 只要 `python-testing-patterns`；
设计 A 股 PE 数据模型只要 `alphaear-stock`。其余 skill 按各自 `description` 自行判断相关性，不强制。

### Skill 不得驱动架构变更

金融 Skill 只提供**金融领域知识 / 数据含义 / 金融数据使用方法**；架构由本文件与项目代码决定。
未经用户允许，**不因 Skill 推荐而**：换数据源、换数据库、改数据模型、加外部 API、
加第三方金融数据服务、改生产依赖。

### 最小修改原则

理解代码 → 判断所需 Skill → 加载 → Skill Check → **最小化修改** → 跑测试 → Skill Usage。
**不为用 Skill 而重构无关代码。**

---

## Agent Skills（**不入库**，换机器须重装）

**18 个 skill 全部集中在 `.opencode/skills/`**（OpenCode 原生项目级目录，已实测移动后重扫并取回正文）。
`.gitignore` 按既有「AI 工具元数据不入库」约定忽略 `.opencode/`、`.agents/`、`skills-lock.json` ——
**新克隆默认一个都没有且不报错**，需重跑：

```bash
npx -y ui-ux-pro-max-cli init --ai opencode          # 7 个（含 design/banner-design/brand/slides/design-system/ui-styling）
npx -y skills add shadcn/ui -y --copy                # 2 个（shadcn、migrate-radix-to-base）
npx -y skills add fastapi/fastapi -y --copy          # 1 个
npx -y skills add wshobson/agents -s responsive-design -s tailwind-design-system \
        -s python-design-patterns -s python-testing-patterns \
        -s architecture-patterns -s code-review-excellence -y --copy   # 6 个
npx -y skills add RKiding/Awesome-finance-skills@alphaear-stock -y --copy  # 1 个
# ↑ `npx skills add` 固定写 .agents/skills/（已实测 -a opencode 无效），装完必须归一，否则又裂回两个目录：
[ -d .agents/skills ] && mv .agents/skills/* .opencode/skills/ && rmdir .agents/skills .agents
```

> **归一的代价（已确认）**：`npx skills list / update / remove` **只扫 `.agents/skills/`**，归一后报空、管不到这 18 个；
> 更新 = 按上面重装。`skills-lock.json` 只存 source/skillPath/hash、**不存安装路径**，移动它不受影响。
>
> `frontend-design` 原在 `.opencode/skill/`（单数）—— 日志证实单复数**都**被扫描（V2 文档漏写），已并入 `.opencode/skills/`。
> 内容与 `anthropics/claude-code` 官方版逐字节一致。
> `shadcn` 依赖 `components.json`、`tailwind-design-system` 依赖 Tailwind v4 —— **本项目两样都没有，装了也不触发**，属占位。
> `alphaear-stock` 是 Agent 的 A/港/美临场查数能力，**不得接入同步链**（绕过 normalization/domain，违反「取数唯一入口」）；
> 其脚本另需 `yfinance`、`loguru`，按约定**不加入 `requirements.txt`**。

---

## 文档导航（HTML 在 docs/）

- `ARCHITECTURE.html` — 数据源容错、Facade 路由、分析计算层设计
- `ANALYTICS.html` — PE 分布统计、历史估值分位技术规格
- `DATABASE.html` — 表设计、关联、字段含义
- `数据源清单.md` — 接口清单与降级策略

> 代码与文档冲突时**信代码**（可执行的才是真理）。