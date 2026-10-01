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
│   └── generate_sector_trend.py # 命令行入口：生成板块走势网页
├── config.ini                  # 配置：月数、输出路径、基准与板块清单
├── AGENT.md                    # 本文档（项目永久上下文与设计契约）
├── templates/
│   └── dashboard.html          # 网页模板（占位符 __DATA_PAYLOAD__ 由数据替换）
├── output/                     # 生成的 HTML 产物（运行时自动创建）
├── tests/
│   └── test_data_interfaces.py # 数据接口测试（数据源 / 名称 / 实时行情 / 网页生成）
├── stocklab/                   # 核心库包
│   ├── common/                 # 通用基础层
│   │   ├── config.py           #   config.ini 解析
│   │   └── type_utils.py       #   safe_float / safe_int 类型安全转换
│   ├── datasource/             # 数据源接入层
│   │   ├── tencent_client.py   #   【腾讯直连行情网关】TencentMarketClient（统一接入股票/ETF/指数）
│   │   └── stock_data.py       #   【个股多源数据服务】MarketDataService（三级容错策略 + 估值对齐）
│   └── visualizer/             # 可视化 / Web 呈现层
│       ├── page_generator.py   #   模板填充 → HTML
│       └── sector_trend.py     #   SectorTrendVisualizer 端到端编排
└── venv/                       # Python 虚拟环境（不入库）
```

## 分层与依赖方向

```
入口脚本 (scripts/generate_sector_trend.py)  /  测试 (tests/test_data_interfaces.py)
        │
        ▼
stocklab.visualizer  ──►  stocklab.datasource  ──►  stocklab.common
```

- `stocklab/common`：无业务依赖的通用工具（配置解析、类型转换）。
- `stocklab/datasource`：对外数据获取。通用的 K 线时序与批量并发抓取走 `TencentMarketClient`（`tencent_client.py`，不区分股票、ETF 与指数）；单股估值综合走 `MarketDataService`（`stock_data.py`）的多源容错。
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

**统一数据契约**：所有单股数据源标准化为三列常量 —— `TRADE_DATE_COLUMN` / `CLOSE_PRICE_COLUMN` / `PE_TTM_COLUMN`。

## 运行方式

```bash
# 生成板块走势网页（默认读取 config.ini）
./venv/bin/python scripts/generate_sector_trend.py

# 自定义月数与输出路径
./venv/bin/python scripts/generate_sector_trend.py --months 60 --output output/trend_5y.html

# 全链路自检（可选股票代码，默认 000001.SZ）
./venv/bin/python tests/test_data_interfaces.py 000001.SZ
```

## 环境与依赖

```bash
python3 -m venv venv
source venv/bin/activate
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple akshare baostock pandas requests
```

| 库 | 用途 |
|----|------|
| akshare | A 股月线 / PE / 公司信息（东方财富、百度股市通接口） |
| baostock | 备用历史行情与 PE |
| pandas | 数据清洗与时序对齐 |
| requests | 腾讯直连 HTTP（实时行情、公司名称、板块 K 线） |

> numpy / matplotlib 为传递或历史依赖，当前代码不直接 import。

## 配置说明（config.ini）

- `[settings]`：`default_months`（默认月数）、`output_html`（输出路径）、`http_timeout`
- `[benchmark]`：主板基准（默认上证指数，白色粗线）
- `[sectors]`：板块清单，每行格式 `标识 = 代码, 简称, 赛道, 颜色, 跟踪指数, 管理人, 纯度说明`
  - 在行首加 `#` 或 `;` 即可临时停用某个板块

## 输出

`output/sector_etf_trend.html` —— 单文件自包含网页，含多标的月线走势对比图，可直接双击打开或分享。

## 注意事项

1. 需要网络可用；数据来自公开接口。
2. 涉及 matplotlib 图表时，macOS 字体使用 PingFang SC / Arial Unicode MS。
3. 腾讯接口返回 `~` 分隔长串，读取时编码必须设为 `gbk`。

## 许可证

MIT License
