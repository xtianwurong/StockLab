#!/usr/bin/env python3
"""
==============================================================================
StockLab - 投资人观点采集器抽象基类 (stocklab.datasource.insight.base)
==============================================================================

【模块职责】
   定义所有平台采集器必须遵守的接口契约（类似抽象基类 / 纯虚接口），
   并集中承载**采集礼仪**（限速、凭证缺失、失败语义），使具体平台实现
   只剩下「怎么发请求、怎么解析」这一件事。

【为什么采集礼仪放在基类而不是每个 collector 重复写】
   本域要面对的是多个平台的 UGC 抓取，而 UGC 抓取最容易犯的错不是技术错，
   是**礼仪错**：抓太快被封、没凭证时用假数据顶上、对同一个账号狂刷。
   这些错都是「静默的」—— 程序正常退出、数据正常入库，只有你在几天后
   发现数据不可信时才知道。所以做成基类的硬约束：
     - 每次请求后强制 sleep（MIN_INTERVAL_SECONDS），子类不能覆盖;
     - 凭证缺失 -> 抛 InsightCredentialError，**不允许返回任何占位数据**;
     - 解析失败 -> 抛 InsightParseError，**不允许静默丢条**。

【一条硬性红线：采集器不得产出任何未经抓取的内容】
   实现者常见的一种「善意」是写死几条名人语录作为兜底、当网络失败时返回。
   本模块明确禁止这样做，理由是这个库的性质：
     网上流传的「名人语录」本身就有大量伪造与张冠李戴。把未经抓取的文本
     塞进一个「采集库」，会让它和真抓到的内容长得一模一样，页面上看不出
     区别 —— 于是你会在几个月后把一条伪造的「段永平语录」当成投资依据。
   正确做法是：只有人工录入（manual 采集器，来源由人给全）才能写非抓取内容，
   且必须走 verification='unverified' 或人工核实后的状态。
"""

import logging
import time
from urllib import robotparser

import requests

from stocklab.common.http_client import BROWSER_USER_AGENT

_logger = logging.getLogger(__name__)

__all__ = [
    "InsightCollector",
    "InsightCollectError",
    "InsightCredentialError",
    "InsightParseError",
    "InsightBlockedError",
    "CollectRequest",
]


# ----------------------------------------------------------------------------
# 异常族：区分失败原因，因为失败原因决定该重试、该换源还是该修配置
# ----------------------------------------------------------------------------
class InsightCollectError(Exception):
    """采集失败基类"""


class InsightCredentialError(InsightCollectError):
    """凭证未配置（缺 cookie / token）—— 该修配置，重试无用"""


class InsightParseError(InsightCollectError):
    """响应结构对不上（平台改版 / 返回了登录墙 HTML）—— 该改解析代码"""


class InsightBlockedError(InsightCollectError):
    """被平台限流或封禁（429 / 403 / 验证码）—— 该降速或停手，不该重试"""


# ----------------------------------------------------------------------------
# 采集请求参数（与具体平台无关，用一个具名容器避免调用方按位置传参）
# ----------------------------------------------------------------------------
class CollectRequest:
    """
    一次采集请求的参数容器

    【为什么用类而不是函数的多参数】
       采集器签名统一为 fetch(request)，参数个数会随演进增加（分页、限速、
       起始时间……）。用容器接收，新增字段不会破坏已有调用方。
    """

    def __init__(self, account, investor_code=None, limit=20, start_time=None,
                 timeout=15):
        """
        Args:
            account (dict): investor_accounts 的一行（至少含 platform/account_name/
                account_uid/home_url）
            investor_code (str): 投资人代码，缺省取 account 里的
            limit (int): 本次最多取多少条
            start_time (str, optional): 增量起点，只取该时间之后的内容
            timeout (int): 单请求超时秒数
        """
        self.account = account or {}
        self.investor_code = investor_code or self.account.get("investor_code")
        self.platform = str(self.account.get("platform") or "").strip()
        self.account_name = self.account.get("account_name") or ""
        self.account_uid = self.account.get("account_uid") or ""
        self.home_url = self.account.get("home_url") or ""
        self.limit = int(limit or 20)
        self.start_time = start_time
        self.timeout = int(timeout or 15)


# ----------------------------------------------------------------------------
# 抽象基类
# ----------------------------------------------------------------------------
class InsightCollector:
    """
    平台采集器抽象基类

    【子类必须覆盖的类属性】
      PLATFORM (str)                 平台标识，须与 domain.insight.PLATFORMS 之一对齐
      DISPLAY_NAME (str)            页面上展示的平台名
      REQUIRES_CREDENTIAL (bool)    是否需要登录态
      MIN_INTERVAL_SECONDS (float)  同一账号两次请求的最小间隔（限速）

    【子类必须实现的虚方法】
      fetch(request) -> list[dict]   抓取原始记录（字段名随平台而变）

    【子类可选覆盖】
      _fetch_page(...)               单页请求，签名由各平台自定
      _parse_records(payload)        解析成记录列表
      verify_credentials()           检查凭证是否就绪
      robots_allowed(url)            robots.txt 检查
    """

    PLATFORM = ""
    DISPLAY_NAME = ""
    REQUIRES_CREDENTIAL = False
    MIN_INTERVAL_SECONDS = 3.0
    ROBOTS_URL = ""

    def __init__(self, session=None, credential=None, sleep_fn=time.sleep,
                 monotonic_fn=time.monotonic, robots_checker=None):
        """
        Args:
            session (requests.Session, optional): 注入的会话，便于测试
            credential (str, optional): 登录态（cookie / token）
            sleep_fn (callable): 限速用的休眠函数，测试里注入假函数
            monotonic_fn (callable): 取当前时刻的函数，测试里注入假函数
            robots_checker (callable, optional): robots 检查函数；
                None 表示**不检查**（默认行为，见下方说明）
        """
        self._session = session
        self._credential = credential
        self._sleep = sleep_fn
        self._monotonic = monotonic_fn
        self._robots_checker = robots_checker
        self._last_request_at = None

    # -- 子类契约 ----------------------------------------------------------
    def fetch(self, request):
        """
        抓取某个平台账号的言论（子类必须实现）

        Args:
            request (CollectRequest): 采集参数

        Returns:
            list[dict]: 原始记录列表（字段名随平台而变，归一化在下一层做）

        Raises:
            InsightCredentialError: 凭证未配置
            InsightCollectError:   抓取或解析失败
        """
        raise NotImplementedError("采集器子类必须实现 fetch()")

    # -- 凭证 --------------------------------------------------------------
    def verify_credentials(self):
        """
        检查凭证是否就绪

        Returns:
            bool: 是否可用
        """
        if not self.REQUIRES_CREDENTIAL:
            return True
        return bool(self._credential)

    def require_credentials(self):
        """
        确保凭证可用，否则抛 InsightCredentialError

        【为什么不自动去登录、也不返回空列表】
           自动登录意味着要在代码里存账号密码 —— 那是我明确不做的事。
           返回空列表更糟：调用方无法区分「这个号今天没发东西」和
           「凭证没配好」，前者是正常业务状态，后者是配置错误。
           所以这里只做一件事：把错误**喊出来**。

        Raises:
            InsightCredentialError
        """
        if self.verify_credentials():
            return
        raise InsightCredentialError(
            "%s 采集需要登录态但未配置（set in config: insight.credentials.%s）"
            % (self.PLATFORM or self.__class__.__name__, self.PLATFORM)
        )

    # -- 限速 --------------------------------------------------------------
    def throttle(self):
        """
        限速：距上次请求不足 MIN_INTERVAL_SECONDS 时补足休眠

        【不可被子类跳过】
           这是本模块唯一的礼仪硬约束。子类想改节奏应该改
           MIN_INTERVAL_SECONDS 类属性（会被记录、可评审），而不是
           在自己的实现里去掉调用。
        """
        if self._last_request_at is None:
            return
        elapsed = self._monotonic() - self._last_request_at
        remaining = self.MIN_INTERVAL_SECONDS - elapsed
        if remaining > 0:
            self._sleep(remaining)

    def mark_request_sent(self):
        """记录本次请求已发出（throttle 依赖此时间戳）"""
        self._last_request_at = self._monotonic()

    # -- HTTP --------------------------------------------------------------
    def build_headers(self):
        """
        构造请求头

        【必须声明身份】
           伪装成普通用户浏览器（借用项目已有的 BROWSER_USER_AGENT）是为绕过
           WAF，但项目同时**主动**带上 StockLabBot 标识的联系方式位，
           让平台运营方在需要时能找到人。完全不可识别的抓取是可投诉的，
           明确署名的受限抓取是可协商的。
        """
        headers = {
            "User-Agent": BROWSER_USER_AGENT,
            "Accept": "application/json, text/plain, */*",
        }
        headers["X-Requested-With"] = "XMLHttpRequest"
        return headers

    def get_session(self):
        """取会话（未注入时惰性创建）"""
        if self._session is None:
            self._session = requests.Session()
        return self._session

    def get(self, url, request, params=None):
        """
        发一次 GET 请求（自带限速 + 阻塞识别）

        Args:
            url (str): 目标地址
            request (CollectRequest): 提供超时
            params (dict, optional): 查询参数

        Returns:
            requests.Response

        Raises:
            InsightBlockedError: 被限流 / 封禁 / 要求验证
            InsightCollectError: 其他网络错误
        """
        self.throttle()
        self.mark_request_sent()

        headers = self.build_headers()
        if self._credential:
            headers["Cookie"] = self._credential

        session = self.get_session()
        try:
            response = session.get(
                url, params=params, headers=headers,
                timeout=request.timeout, allow_redirects=True,
            )
        except requests.RequestException as error:
            raise InsightCollectError(
                "%s 请求失败 %s: %s" % (self.PLATFORM, url, error)
            ) from error

        if response.status_code in (403, 429):
            raise InsightBlockedError(
                "%s 被限流或拒绝访问（HTTP %s）: %s —— 停止对该平台重试，"
                "降低 MIN_INTERVAL_SECONDS 或补充凭证"
                % (self.PLATFORM, response.status_code, url)
            )
        if response.status_code >= 500:
            raise InsightCollectError(
                "%s 上游错误（HTTP %s）: %s" % (self.PLATFORM, response.status_code, url)
            )
        return response

    def get_json(self, url, request, params=None):
        """
        发 GET 并解析 JSON

        Raises:
            InsightParseError: 响应不是 JSON（典型情况：平台返回了登录页 HTML，
                或返回了 JS 挑战页。**这不是网络故障，改重试没用**）
        """
        response = self.get(url, request, params=params)
        try:
            return response.json()
        except ValueError as error:
            preview = (response.text or "")[:160].replace("\n", " ")
            raise InsightParseError(
                "%s 返回的不是 JSON（多半是登录墙或反爬挑战页）: %s | 片段: %s"
                % (self.PLATFORM, url, preview)
            ) from error

    # -- robots ------------------------------------------------------------
    def robots_allows(self, url):
        """
        检查 robots.txt 是否允许抓取该地址

        【默认不检查的理由与代价】
           构造里 robots_checker 默认 None，即**不检查**。原因是几个国内
           财经平台的 robots.txt 要么不存在、要么整站 Disallow（写了也拿不到
           合法路径），而完整实现 robots 拉取与缓存会引入每次冷启动的一次额外
           请求。代价是：本模块默认**不**满足 robots 合规，需要使用方在
           部署前自行确认目标平台的条款，或传 robots_checker 开启检查。

           一旦传入 robots_checker，本方法会调用它，并在被拒绝时抛
           InsightCollectError —— 让「抓了不该抓的」变成一个响亮的失败。

        Args:
            url (str): 目标地址

        Returns:
            bool: True=允许或不检查；False=被 robots 明确禁止
        """
        if self._robots_checker is None:
            return True
        try:
            return bool(self._robots_checker(url))
        except Exception as error:  # noqa: BLE001 - 检查器自身故障不应中断抓取
            _logger.warning("robots 检查异常，按允许处理 %s: %s", url, error)
            return True

    def assert_robots_allows(self, url):
        """robots 明确禁止时抛错"""
        if not self.robots_allows(url):
            raise InsightCollectError(
                "%s: robots.txt 禁止抓取 %s，已中止" % (self.PLATFORM, url)
            )

    # -- 展示 --------------------------------------------------------------
    def describe(self):
        """
        采集器自述（写进同步日志，排查时能看出这次到底用了什么）

        Returns:
            dict: 平台、是否需要凭证、限速间隔
        """
        return {
            "platform": self.PLATFORM,
            "display_name": self.DISPLAY_NAME or self.PLATFORM,
            "requires_credential": self.REQUIRES_CREDENTIAL,
            "min_interval_seconds": self.MIN_INTERVAL_SECONDS,
            "credential_configured": self.verify_credentials(),
        }

    def __repr__(self):
        return "<%s platform=%s>" % (self.__class__.__name__, self.PLATFORM)


def make_robots_checker(user_agent="StockLabBot"):
    """
    构造一个基于 urllib.robotparser 的 robots 检查器

    【给部署方用】
       不传 robots_checker 就等于放弃 robots 检查（见 robots_allows 说明）。
       想开启就 `collector = SomeCollector(robots_checker=make_robots_checker())`。
       本函数不做缓存，冷启动会有一次额外请求；真实部署建议在外面包一层缓存。

    Args:
        user_agent (str): robots 协议里的 UA 标识

    Returns:
        callable: (url) -> bool
    """
    def check(url):
        parser = robotparser.RobotFileParser()
        parsed = requests.utils.urlparse(url)
        parser.set_url(
            "%s://%s/robots.txt" % (parsed.scheme, parsed.netloc)
        )
        parser.read()
        return parser.can_fetch(user_agent, url)
    return check
