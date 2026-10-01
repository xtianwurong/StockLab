# StockLab

A 股核心板块行业 ETF 与主板基准的长周期（默认 10 年）月线走势分析工具。
抓取各赛道纯正行业 ETF 与上证指数的前复权月线数据，清洗对齐后渲染为**自包含的交互式 HTML 网页**（ECharts 图表，单文件、可离线打开、可直接分享）。

**数据全部来自公开接口，不依赖任何本地数据文件。**

---

## 目录结构

```
StockLab/
├── generate_sector_trend.py    # 命令行入口：生成板块走势网页
├── config.ini                  # 配置：月数、输出路径、基准与板块清单
├── AGENT.md                    # 本文档
├── templates/
│   └── dashboard.html          # 网页模板（占位符 __DATA_PAYLOAD__ 由数据替换）
├── output/                     # 生成的 HTML 产物（运行时自动创建）
├── tests/
│   └── test_data_interfaces.py # 数据接口测试（数据源 / 名称 / 实时行情 / 网页生成）
├── stocklab/                   # 核心库包
│   ├── common/                 # 通用基础层
│   │   ├── config.py           #   config.ini 解析
│   │   └── type_utils.py       #   safe_float / safe_int 类型安全转换
│   ├── data/                   # 数据访问层
│   │   ├── provider.py         #   单股数据提供：多数据源容错 + 统一契约 + 实时行情
│   │   └── sector_fetcher.py   #   板块 ETF 月线并发抓取
│   └── visualizer/             # 可视化 / Web 呈现层
│       ├── page_generator.py   #   模板填充 → HTML
│       └── sector_trend.py     #   SectorTrendVisualizer 端到端编排
└── venv/                       # Python 虚拟环境（不入库）
```

## 分层与依赖方向

```
入口脚本 (generate_sector_trend.py)  /  测试 (tests/test_data_interfaces.py)
        │
        ▼
stocklab.visualizer  ──►  stocklab.data  ──►  stocklab.common
```

- `stocklab/common`：无业务依赖的通用工具（配置解析、类型转换）。
- `stocklab/data`：对外数据获取。单股走 `provider.py` 的多源容错；板块批量走 `sector_fetcher.py`。
- `stocklab/visualizer`：把数据渲染成网页。
- 顶层入口脚本只做「参数解析 + 调用库」，不含业务逻辑；自检脚本放在 tests/ 下。

## 数据源架构

**单股数据（`stocklab/data/provider.py`）** 三级容错，价格与 PE 各自独立降级：

| 数据 | 主 | 备 |
|------|----|----|
| 月线收盘价 | AkShare（东方财富） | BaoStock → 腾讯 |
| PE-TTM | AkShare（百度股市通） | BaoStock（日线降采样） |
| 公司简称 | 腾讯轻量直连 | AkShare → BaoStock |

**板块数据（`stocklab/data/sector_fetcher.py`）**：腾讯 `web.ifzq.gtimg.cn` 前复权月线，
`ThreadPoolExecutor` 并发抓取。

**统一数据契约**：所有单股数据源标准化为三列常量 —— `TRADE_DATE_COLUMN` / `CLOSE_PRICE_COLUMN` / `PE_TTM_COLUMN`。

## 运行方式

```bash
# 生成板块走势网页（默认读取 config.ini）
./venv/bin/python generate_sector_trend.py

# 自定义月数与输出路径
./venv/bin/python generate_sector_trend.py --months 60 --output output/trend_5y.html

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

## 编码约定

1. **不使用高级 Python 语法**（装饰器、生成器嵌套、元类、async、海象运算符、模式匹配）；类和对象可用，写法尽量直白、贴近 C++。
2. **import 集中在文件开头**，不在函数/方法中间 import。
3. **模块按职责命名**（而非单一类名）；数据源适配方法统一 `_convert_to_<源>_<字段>`，查询方法统一 `_query_<源>_<内容>`。
4. **最小暴露**：库模块用 `__all__` 声明公共接口，内部实现以 `_` 开头。
5. **日志**：统一使用标准库 `logging`；库模块只 `getLogger`，`basicConfig` 只在入口脚本里做。

## 注意事项

1. 需要网络可用；数据来自公开接口。
2. 涉及 matplotlib 图表时，macOS 字体使用 PingFang SC / Arial Unicode MS。
3. 腾讯接口返回 `~` 分隔长串，读取时编码必须设为 `gbk`。

## 许可证

MIT License
