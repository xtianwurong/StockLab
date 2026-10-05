#!/usr/bin/env python3
"""
==============================================================================
StockLab - 大宗商品登记表 (stocklab.datasource.commodity.registry)
==============================================================================

【模块职责】
   声明本项目跟踪哪些大宗商品，一个品种一条 CommoditySpec。
   这里同时放**元信息**（名称 / 分类 / 计价单位）与**抓取策略**（抓哪个合约），
   因为二者是同一件事的两面 —— 拆开只会制造「代码改了、表没改」的静默分叉。

【为什么不落库】
   落库需要一步 load-sources、需要处理「库里有、代码已删」的孤儿行，
   换来的收益只有「运行时可改品种」。本项目加品种就是改这个文件再跑一次同步，
   没有运行时增删品种的需求，所以不承担那套成本。
   （对比 insight 域：那边要落库是因为**账号会变** —— 被封、改名、换平台，
    且需要 `is_enabled` 开关；本域不存在这类运行时状态。）

【为什么用主力连续合约，不用某个具体月份】
   具体月份合约会到期、会在换月时留下断崖，跨年比较时「价格从 6000 涨到 3000」
   可能只是从 1 月合约换到了 5 月合约。主力连续（如 RB0）由源站按持仓量
   自动拼接，是一条口径一致、可跨年比较的序列。
   代价：换月日附近存在拼接跳空，做**收益率**研究时需要自行处理；
   本域只算「当前价格在过去 N 年的分位」，对拼接跳空不敏感，故接受这个代价。

【计价单位必须写实】
   单位不一致是本域最容易出错的地方：原油 SC 是**元/桶**（人民币，不是美元）、
   黄金是**元/克**、其余是**元/吨**。写错单位会让页面上的数字差三个数量级，
   而这种错从数字本身看不出来（10680 挂什么单位都像那么回事）。
"""

from dataclasses import dataclass

__all__ = [
    "CommoditySpec",
    "CATEGORIES",
    "SOURCE_AKSHARE_SINA_MAIN",
    "commodities",
    "by_symbol",
]


# 抓取通道标识，写入 commodity.price_history.source
SOURCE_AKSHARE_SINA_MAIN = "akshare:futures_main_sina"

# 展示分组顺序（卡片按此分组，未列在其中的品种会被标为「其它」）
CATEGORIES = ("能源", "黑色系", "有色金属", "贵金属", "农产品")


@dataclass(frozen=True)
class CommoditySpec:
    """单个大宗商品的登记项（不可变）"""

    symbol: str          # 内部代号，全库主键，定下来就不要改（改了等于换一个品种）
    name: str            # 中文名，页面主标题
    category: str        # 展示分组，必须 ∈ CATEGORIES
    unit: str            # 计价单位，**合约报价单位**，照实写
    unit_note: str       # 单位提示，静态文本（不参与计算，仅展示）
    source_symbol: str   # 新浪主力连续合约代码，如 "RB0"
    exchange: str        # 交易所中文名
    description: str     # 为什么盯它（这个品种是哪个宏观变量的代理）
    sort_order: int      # 全局排序（先按 CATEGORIES 分组，组内按此排序）
    is_active: bool = True


# ---------------------------------------------------------------------------
# 首版 10 个品种：能源 2 / 黑色系 2 / 有色金属 2 / 贵金属 1 / 农产品 3
# 每个 source_symbol 都在 2026-09-30 实测可用（末行有成交、序列 ≥ 1300 行）。
# ---------------------------------------------------------------------------
COMMODITIES = (
    CommoditySpec(
        symbol="crude_oil", name="原油", category="能源",
        unit="元/桶", unit_note="INE 人民币计价，非美元/桶",
        source_symbol="SC0", exchange="上海期货交易所(INE)",
        description="全球通胀与工业成本的源头，化工、运输、农业投入品都从它定价",
        sort_order=10,
    ),
    CommoditySpec(
        symbol="coke", name="焦炭", category="能源",
        unit="元/吨", unit_note="由焦煤干馏而成，主要供高炉燃料",
        source_symbol="J0", exchange="大连商品交易所",
        description="煤—焦—钢链的中游，煤价与钢价之间的传导观察点",
        sort_order=20,
    ),
    CommoditySpec(
        symbol="iron_ore", name="铁矿石", category="黑色系",
        unit="元/吨", unit_note="干吨计价，人民币",
        source_symbol="I0", exchange="大连商品交易所",
        description="钢铁上游原料，进口依赖度高，反映国内开工强度",
        sort_order=30,
    ),
    CommoditySpec(
        symbol="rebar", name="螺纹钢", category="黑色系",
        unit="元/吨", unit_note="12mm HRB400 现货对应口径",
        source_symbol="RB0", exchange="上海期货交易所",
        description="建筑钢材，地产与基建开工的直接温度计",
        sort_order=40,
    ),
    CommoditySpec(
        symbol="copper", name="铜", category="有色金属",
        unit="元/吨", unit_note="1# 电解铜",
        source_symbol="CU0", exchange="上海期货交易所",
        description="「铜博士」—— 全球制造业与电网投资的同步指标",
        sort_order=50,
    ),
    CommoditySpec(
        symbol="aluminum", name="铝", category="有色金属",
        unit="元/吨", unit_note="A00 铝锭",
        source_symbol="AL0", exchange="上海期货交易所",
        description="能源密集型金属，电解铝成本直接挂钩电价",
        sort_order=60,
    ),
    CommoditySpec(
        symbol="gold", name="黄金", category="贵金属",
        unit="元/克", unit_note="含人民币汇率因素，与美元金价不等价",
        source_symbol="AU0", exchange="上海期货交易所",
        description="避险与实际利率的镜像；人民币金价同时受金价与汇率影响",
        sort_order=70,
    ),
    CommoditySpec(
        symbol="live_hog", name="生猪", category="农产品",
        unit="元/吨", unit_note="现货按元/公斤报价时约为其千分之一",
        source_symbol="LH0", exchange="大连商品交易所",
        description="CPI 权重最大的食品项，猪周期是四到五年一轮的产能出清",
        sort_order=80,
    ),
    CommoditySpec(
        symbol="soybean_meal", name="豆粕", category="农产品",
        unit="元/吨", unit_note="43% 蛋白含量基准",
        source_symbol="M0", exchange="大连商品交易所",
        description="饲料蛋白主料，养殖成本；压榨依赖进口大豆，跟踪到岸价",
        sort_order=90,
    ),
    CommoditySpec(
        symbol="natural_rubber", name="天然橡胶", category="农产品",
        unit="元/吨", unit_note="SCR 5 标准胶",
        source_symbol="RU0", exchange="上海期货交易所",
        description="轮胎与汽车需求的代理；东南亚产区天气亦是供给变量",
        sort_order=100,
    ),
)


def commodities(include_inactive=False):
    """
    全部品种，按 sort_order 升序

    Args:
        include_inactive (bool): 是否包含 is_active=False 的品种

    Returns:
        tuple[CommoditySpec, ...]
    """
    specs = COMMODITIES if include_inactive else tuple(
        spec for spec in COMMODITIES if spec.is_active
    )
    return tuple(sorted(specs, key=lambda spec: spec.sort_order))


_SYMBOLS = {spec.symbol: spec for spec in COMMODITIES}


def by_symbol(symbol):
    """
    按 symbol 取登记项

    Args:
        symbol (str): 品种代号

    Returns:
        CommoditySpec | None: 未登记返回 None（由调用方决定 400 还是忽略）
    """
    return _SYMBOLS.get(symbol)
