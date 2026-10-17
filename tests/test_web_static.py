#!/usr/bin/env python3
"""
==============================================================================
StockLab - Web 静态资源契约测试 (tests/test_web_static.py)
==============================================================================

【功能用途】
  把静态资源里「不会被页面渲染冒烟测试覆盖、却会静默失效」的约定钉死：
  1. common.js 的 escapeHtml 必须**真的**转义 < > & " '（它被多处 innerHTML
     与 ECharts tooltip 依赖，退化成 no-op 就等于开了注入口子）
  2. SL.ui.renderSuggest 的 DOM 结构（行数 / 单元格 / 空态 / 回调）
  3. --rhythm-* 必须是 --space-* 的别名，不得各自写死数值（两套刻度会漂移）
  4. el() 只在 common.js 定义一次，ui.js / app.js 复用 SL.el
  5. 所有 static/*.js 通过 node --check 语法校验

【为什么用 node 实跑而不是只做文本断言】
  转义是否正确只有真执行才知道；本项目无 JS 构建步骤，用 node 的最小 DOM 桩
  实跑 common.js + ui.js，既快又不需要引入 jsdom 依赖。

【运行方式】
  ./venv/bin/python -m pytest tests/test_web_static.py -v
"""

import json
import os
import re
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "app", "web", "static")

# 最小 DOM 桩：足够加载 common.js + ui.js 并驱动 renderSuggest。
# 顶层只做函数定义与 SL 挂载，因此不需要完整 DOM 行为。
_NODE_PROBE = """
function FakeEl(tag) {
  this.tagName = tag;
  this.children = [];
  this.className = "";
  this.textContent = "";
  this.listeners = {};
}
FakeEl.prototype.appendChild = function (child) { this.children.push(child); return child; };
FakeEl.prototype.addEventListener = function (type, fn) { this.listeners[type] = fn; };
FakeEl.prototype.click = function () { if (this.listeners.click) this.listeners.click(); };

global.window = { addEventListener: function () {} };
global.document = {
  readyState: "loading",
  createElement: function (tag) { return new FakeEl(tag); },
  getElementById: function () { return null; },
  querySelector: function () { return null; },
  querySelectorAll: function () { return []; },
  addEventListener: function () {},
  documentElement: { classList: { add: function () {}, remove: function () {} } }
};

var fs = require("fs");
var vm = require("vm");
vm.runInThisContext(fs.readFileSync("app/web/static/common.js", "utf8"));
vm.runInThisContext(fs.readFileSync("app/web/static/ui.js", "utf8"));
var SL = window.SL;

var withMarket = new FakeEl("div");
SL.ui.renderSuggest(withMarket, [{ code: "600519.SH", name: "贵州茅台", market: "SH" }],
  function () {});
var noMarket = new FakeEl("div");
SL.ui.renderSuggest(noMarket, [{ code: "600519.SH", name: "贵州茅台", market: "SH" }],
  function () {}, { showMarket: false });
var empty = new FakeEl("div");
SL.ui.renderSuggest(empty, [], function () {});
var picked = null;
var panel = new FakeEl("div");
SL.ui.renderSuggest(panel, [{ code: "000001.SZ", name: "平安银行" }],
  function (item) { picked = item; });
panel.children[0].click();

process.stdout.write(JSON.stringify({
  escaped: SL.escapeHtml(String.fromCharCode(60, 98, 62, 38, 34, 39)),
  plain: SL.escapeHtml("600519.SH"),
  nullish: SL.escapeHtml(null),
  withMarketCells: withMarket.children[0].children.length,
  withMarketClass: withMarket.className,
  noMarketCells: noMarket.children[0].children.length,
  emptyRows: empty.children.length,
  emptyChildClass: empty.children[0].className,
  emptyPanelClass: empty.className,
  picked: picked && picked.code
}));
"""


@pytest.fixture(scope="module")
def static_probe():
    """在最小 DOM 桩里加载 common.js + ui.js，返回探针结果"""
    result = subprocess.run(
        ["node", "-e", _NODE_PROBE],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        pytest.skip("node 不可用或加载失败：%s" % result.stderr[:400])
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# 一、escapeHtml 必须真的转义
# ---------------------------------------------------------------------------
def test_escape_html_escapes_dangerous_characters(static_probe):
    assert static_probe["escaped"] == "&lt;b&gt;&amp;&quot;&#39;"


def test_escape_html_leaves_safe_text_untouched(static_probe):
    assert static_probe["plain"] == "600519.SH"


def test_escape_html_handles_null(static_probe):
    assert static_probe["nullish"] == ""


# ---------------------------------------------------------------------------
# 二、证券联想面板
# ---------------------------------------------------------------------------
def test_render_suggest_shows_market_by_default(static_probe):
    assert static_probe["withMarketCells"] == 3


def test_render_suggest_hides_market_when_disabled(static_probe):
    assert static_probe["noMarketCells"] == 2


def test_render_suggest_empty_state(static_probe):
    assert static_probe["emptyRows"] == 1
    assert static_probe["emptyChildClass"] == "suggest-empty"


def test_render_suggest_pick_invokes_callback(static_probe):
    assert static_probe["picked"] == "000001.SZ"


def test_render_suggest_opens_panel(static_probe):
    assert static_probe["withMarketClass"] == "suggest open"
    assert static_probe["emptyPanelClass"] == "suggest open"


# ---------------------------------------------------------------------------
# 三、design token：韵律刻度不得写死
# ---------------------------------------------------------------------------
def test_rhythm_tokens_alias_space_tokens():
    with open(os.path.join(STATIC, "tokens.css"), encoding="utf-8") as handle:
        css = handle.read()
    for step in ("1", "2", "3", "4", "5", "6", "8", "10", "12"):
        assert "--rhythm-%s: var(--space-%s)" % (step, step) in css, \
            "--rhythm-%s 应别名 --space-%s，不得写死数值" % (step, step)


# ---------------------------------------------------------------------------
# 四、el() 单一实现
# ---------------------------------------------------------------------------
def test_el_defined_once_across_static_js():
    definers = []
    for name in sorted(os.listdir(STATIC)):
        if not name.endswith(".js") or name == "echarts.min.js":
            continue
        with open(os.path.join(STATIC, name), encoding="utf-8") as handle:
            if "function el(tag" in handle.read():
                definers.append(name)
    assert definers == ["common.js"], "el() 只应由 common.js 定义，实为 %s" % definers


@pytest.mark.parametrize("name", ["ui.js", "app.js"])
def test_el_consumers_reuse_sl_el(name):
    with open(os.path.join(STATIC, name), encoding="utf-8") as handle:
        content = handle.read()
    assert "var el = SL.el;" in content


# ---------------------------------------------------------------------------
# 五、全部静态脚本语法正确
# ---------------------------------------------------------------------------
def test_all_static_scripts_parse():
    failures = []
    for name in sorted(os.listdir(STATIC)):
        if not name.endswith(".js"):
            continue
        result = subprocess.run(
            ["node", "--check", os.path.join(STATIC, name)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            failures.append("%s: %s" % (name, result.stderr[:200]))
    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# 六、静态资源必须走条件请求（改了脚本立即生效，不被浏览器缓存拦住）
# ---------------------------------------------------------------------------
def test_static_assets_are_revalidated(client):
    response = client.get("/static/common.js")
    assert response.status_code == 200
    cache_control = response.headers.get("Cache-Control", "")
    assert "no-cache" in cache_control, "静态资源应重新校验，实为 %r" % cache_control
    assert response.headers.get("ETag"), "需保留 ETag 以复用 304"


# ---------------------------------------------------------------------------
# 七、页面 JS 引用的 DOM id 必须在对应模板里存在
#     （id 打错一个字，脚本就在运行时静默拿到 null —— 控件「点了没反应」，
#      而只有真在浏览器里手点才能发现；这里用文本契约提前钉死）
# ---------------------------------------------------------------------------
PAGE_JS_TEMPLATES = {
    "app.js": "analysis.html",
    "market.js": "market.html",
    "indices.js": "indices.html",
    "industries.js": "industries.html",
    "commodities.js": "commodities.html",
    "technical.js": "technical.html",
    "insight.js": "insight.html",
    "screener.js": "screener.html",
    "compare.js": "compare.html",
    "portfolio.js": "portfolio.html",
    "fund_analysis.js": "fund_analysis.html",
    "fund_holding.js": "fund_holding.html",
}

TEMPLATES = os.path.join(ROOT, "app", "web", "templates")


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def test_page_js_references_only_existing_template_ids():
    """每个页面脚本 getElementById 的 id 必须能在其模板里找到"""
    failures = []
    for js_name, tpl_name in PAGE_JS_TEMPLATES.items():
        template = _read(os.path.join(TEMPLATES, tpl_name))
        script = _read(os.path.join(STATIC, js_name))
        template_ids = set(re.findall(r'id="([^"]+)"', template))
        referenced = set(re.findall(r'getElementById\(\s*"([^"]+)"', script))
        missing = sorted(ref for ref in referenced if ref not in template_ids)
        if missing:
            failures.append("%s -> %s 缺失: %s" % (js_name, tpl_name, missing))
    assert not failures, "\n".join(failures)


@pytest.mark.parametrize("tpl_name", sorted(set(PAGE_JS_TEMPLATES.values())))
def test_page_template_has_no_duplicate_ids(tpl_name):
    """同一页面内 id 必须唯一（重复 id 会让 getElementById 只命中第一个）"""
    template = _read(os.path.join(TEMPLATES, tpl_name))
    ids = re.findall(r'id="([^"]+)"', template)
    duplicates = sorted({value for value in ids if ids.count(value) > 1})
    assert not duplicates, "%s 存在重复 id：%s" % (tpl_name, duplicates)


def test_page_interactive_controls_are_wired():
    """模板里带 id 的 button/input/select 必须被对应脚本引用（即「控件有接线」）"""
    failures = []
    for js_name, tpl_name in PAGE_JS_TEMPLATES.items():
        template = _read(os.path.join(TEMPLATES, tpl_name))
        script = _read(os.path.join(STATIC, js_name))
        for tag, attrs in re.findall(r'<(button|input|select|textarea)\b([^>]*)>', template, re.I):
            if 'type="hidden"' in attrs:
                continue
            match = re.search(r'id="([^"]+)"', attrs)
            if not match:
                continue
            control_id = match.group(1)
            if ('"%s"' % control_id) not in script and ("'%s'" % control_id) not in script:
                failures.append("%s: <%s id=%r> 未被 %s 引用" % (tpl_name, tag, control_id, js_name))
    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# 八、基金持仓页脚本在最小 DOM 桩里能完成初始化
#     （init() 会回填默认日期区间并绑定按钮；id 对不上或组件缺失会直接抛错）
# ---------------------------------------------------------------------------
_FUND_HOLDING_PROBE = """
function FakeEl(tag) {
  this.tagName = tag;
  this.children = [];
  this.className = "";
  this.value = "";
  this.innerHTML = "";
  this.textContent = "";
  this.style = {};
  this.dataset = {};
  this.listeners = {};
  var noop = function () {};
  this.classList = { add: noop, remove: noop, toggle: noop, contains: function () { return false; } };
}
FakeEl.prototype.appendChild = function (child) { this.children.push(child); return child; };
FakeEl.prototype.addEventListener = function (type, fn) { this.listeners[type] = fn; };
FakeEl.prototype.querySelectorAll = function () { return []; };
FakeEl.prototype.querySelector = function () { return null; };
FakeEl.prototype.setAttribute = function () {};
FakeEl.prototype.closest = function () { return null; };

var els = {};
global.window = {
  addEventListener: function () {},
  location: { search: "", pathname: "/fund-holding" }
};
global.document = {
  readyState: "complete",
  createElement: function (tag) { return new FakeEl(tag); },
  getElementById: function (id) { if (!els[id]) { els[id] = new FakeEl("div"); } return els[id]; },
  querySelector: function () { return null; },
  querySelectorAll: function () { return []; },
  addEventListener: function () {},
  documentElement: { classList: { add: function () {}, remove: function () {} } }
};
global.history = { replaceState: function () {} };

var fs = require("fs");
var vm = require("vm");
vm.runInThisContext(fs.readFileSync("app/web/static/common.js", "utf8"));
vm.runInThisContext(fs.readFileSync("app/web/static/fund_holding.js", "utf8"));

process.stdout.write(JSON.stringify({
  start: els["fh-start"] ? els["fh-start"].value : null,
  end: els["fh-end"] ? els["fh-end"].value : null,
  boundQuery: !!(els["fh-query"] && els["fh-query"].listeners.click),
  boundRefresh: !!(els["fh-refresh"] && els["fh-refresh"].listeners.click),
  boundInput: !!(els["fh-code"] && els["fh-code"].listeners.input)
}));
"""


@pytest.fixture(scope="module")
def fund_holding_probe():
    """在最小 DOM 桩里加载 common.js + fund_holding.js，返回初始化结果"""
    result = subprocess.run(
        ["node", "-e", _FUND_HOLDING_PROBE],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        pytest.fail("fund_holding.js 初始化失败：%s" % result.stderr[:600])
    return json.loads(result.stdout)


def test_fund_holding_prefills_date_range(fund_holding_probe):
    start = fund_holding_probe["start"]
    end = fund_holding_probe["end"]
    assert re.match(r"^\d{4}-\d{2}-\d{2}$", end or ""), "终点应默认回填今天，实为 %r" % end
    assert re.match(r"^\d{4}-\d{2}-\d{2}$", start or ""), "起点应默认回填，实为 %r" % start
    assert start < end, "时序起点应早于终点"


def test_fund_holding_binds_controls(fund_holding_probe):
    assert fund_holding_probe["boundQuery"], "查询按钮必须有 click 处理器"
    assert fund_holding_probe["boundRefresh"], "刷新按钮必须有 click 处理器"
    assert fund_holding_probe["boundInput"], "基金代码输入必须有 input 处理器"


# ---------------------------------------------------------------------------
# 九、fetchJson 的失败文案必须是中文
#     （超时 = AbortError "The operation was aborted."、断网 = TypeError
#      "Failed to fetch"；各页面都写 `error.message || 中文兜底`，原文会直接
#      显示给用户 —— 一条英文技术串，对用户毫无可操作信息）
# ---------------------------------------------------------------------------
_FETCH_JSON_PROBE = """
var noop = function () {};
global.window = { addEventListener: noop, location: { search: "", pathname: "/" } };
global.document = {
  readyState: "complete",
  createElement: function () {
    return { style: {}, classList: { add: noop, remove: noop, toggle: noop },
             setAttribute: noop, appendChild: noop, addEventListener: noop };
  },
  getElementById: function () { return null; },
  querySelector: function () { return null; },
  querySelectorAll: function () { return []; },
  addEventListener: noop,
  documentElement: { classList: { add: noop, remove: noop } }
};
global.fetch = function (url, init) {
  if (url.indexOf("netfail") !== -1) {
    return Promise.reject(new TypeError("Failed to fetch"));
  }
  return new Promise(function (resolve, reject) {
    init.signal.addEventListener("abort", function () {
      var err = new Error("The operation was aborted.");
      err.name = "AbortError";
      reject(err);
    });
  });
};

var fs = require("fs");
var vm = require("vm");
vm.runInThisContext(fs.readFileSync("app/web/static/common.js", "utf8"));
var SL = window.SL;

Promise.all([
  SL.fetchJson("/api/timeout", 20).then(function () { return "unexpected"; },
                                        function (e) { return e.message; }),
  SL.fetchJson("/api/netfail", 1000).then(function () { return "unexpected"; },
                                          function (e) { return e.message; })
]).then(function (messages) {
  process.stdout.write(JSON.stringify({ timeout: messages[0], network: messages[1] }));
});
"""


@pytest.fixture(scope="module")
def fetch_json_probe():
    result = subprocess.run(
        ["node", "-e", _FETCH_JSON_PROBE],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        pytest.fail("fetchJson 探测失败：%s" % result.stderr[:600])
    return json.loads(result.stdout)


def test_fetch_json_timeout_message_is_chinese(fetch_json_probe):
    message = fetch_json_probe["timeout"]
    assert "超时" in message, message
    assert "abort" not in message.lower(), "不得把 AbortError 原文显示给用户：%r" % message


def test_fetch_json_network_error_message_is_chinese(fetch_json_probe):
    message = fetch_json_probe["network"]
    assert "网络" in message, message
    assert "Failed to fetch" not in message, message


# ---------------------------------------------------------------------------
# 十、页面 JS 用到的 class 必须真的被定义（否则「静默无色 / 无样式」）
# ---------------------------------------------------------------------------
# 已知的无害项：都已经有等价的样式来源，不属于缺陷。
#   level-clickable —— .level-row 自带 cursor:pointer，类名冗余
#   factor-hint     —— screener.js 只把它当查询选择器，元素本身用 inline style
_UNSTYLED_ALLOWLIST = {"level-clickable", "factor-hint"}


def _defined_classes():
    """收集所有 CSS / 页面 <style> 里出现过的 class 名"""
    defined = set()
    for folder in (STATIC, os.path.join(ROOT, "app", "web", "templates")):
        for name in os.listdir(folder):
            if not name.endswith((".css", ".html")):
                continue
            text = open(os.path.join(folder, name), encoding="utf-8").read()
            defined |= set(re.findall(r"\.([A-Za-z][\w-]*)", text))
    return defined


def _js_class_literals(text):
    """抽取 JS 里写成字面量、最终会落到 DOM class 上的名字"""
    used = set()
    for pattern in (
        r'SL\.el\(\s*"[a-z]+"\s*,\s*"([^"]+)"',              # SL.el("div", "card x")
        r'className\s*=\s*"([^"]+)"',                          # el.className = "card x"
        r'classList\.(?:add|remove|toggle)\(\s*"([^"]+)"',     # classList.add("hidden")
    ):
        for match in re.finditer(pattern, text):
            used |= set(match.group(1).split())
    return used


@pytest.mark.parametrize("js_name", sorted(
    name for name in os.listdir(STATIC)
    if name.endswith(".js") and name not in ("echarts.min.js", "common.js", "ui.js", "charts.js", "theme.js")
))
def test_page_js_class_names_are_defined(js_name):
    used = _js_class_literals(open(os.path.join(STATIC, js_name), encoding="utf-8").read())
    missing = sorted(used - _defined_classes() - _UNSTYLED_ALLOWLIST)

    assert not missing, (
        "%s 使用了没有任何 CSS 定义的 class：%s —— 元素会静默失去样式"
        % (js_name, missing)
    )


def test_technical_sign_classes_are_styled():
    """technical.js 给 stat-v 统计卡拼 pos/neg 上色（字符串拼接，上面那条扫描抓不到），
    页内必须真的定义了 .stat-v.pos / .stat-v.neg，否则 signClass() 悄悄失效：
    指标格有色，统计卡无色，同一数值两处表现不一致。"""
    js = open(os.path.join(STATIC, "technical.js"), encoding="utf-8").read()
    css = open(os.path.join(ROOT, "app", "web", "templates", "technical.html"),
               encoding="utf-8").read()

    assert "stat-v " in js and "signClass" in js
    for cls in ("pos", "neg"):
        assert re.search(r"\.stat-v\.%s\b" % cls, css), \
            "technical.html 缺少 .stat-v.%s 规则，统计卡的正负着色会静默失效" % cls


# ---------------------------------------------------------------------------
# 十一、个股分析页「运行期」冒烟
#
#   HTTP 层只能证明接口 200，证明不了页面能渲染完。appendChild 收到 HTML
#   字符串会抛 TypeError，而 analyze() 的 .catch 会把它吞成一条错误提示并清空
#   结果区 —— 从接口侧完全看不出来。这里用最小 DOM（appendChild 严格校验节点）
#   + fetch 桩，把 app.js 从 init 到渲染整条链真跑一遍。
# ---------------------------------------------------------------------------
_PAGE_RUNTIME_HARNESS = r'''
var APP_JS = "app/web/static/app.js";

function FakeEl(tag) {
  this.tagName = String(tag || "div").toUpperCase();
  this.nodeType = 1;
  this.children = [];
  this.className = "";
  this._text = "";
  this._html = "";
  this.value = "";
  this.style = {};
  this.attributes = {};
  this.listeners = {};
  this.parentNode = null;
  this.isConnected = true;
  this.classList = {
    add: function () {}, remove: function () {}, toggle: function () {},
    contains: function () { return false; }
  };
}
Object.defineProperty(FakeEl.prototype, "textContent", {
  get: function () { return this._text; },
  set: function (v) { this._text = v === null || v === undefined ? "" : String(v); this.children = []; }
});
Object.defineProperty(FakeEl.prototype, "innerHTML", {
  get: function () { return this._html; },
  set: function (v) { this._html = v === null || v === undefined ? "" : String(v); this.children = []; }
});
FakeEl.prototype.appendChild = function (child) {
  if (!child || typeof child !== "object" ||
      (child.nodeType !== 1 && child.nodeType !== 3 && child.nodeType !== 11)) {
    throw new TypeError(
      "Failed to execute 'appendChild' on 'Node': parameter 1 is not of type 'Node'.");
  }
  this.children.push(child);
  child.parentNode = this;
  return child;
};
FakeEl.prototype.insertBefore = function (node) { return this.appendChild(node); };
Object.defineProperty(FakeEl.prototype, "lastChild", {
  get: function () { return this.children.length ? this.children[this.children.length - 1] : null; }
});
Object.defineProperty(FakeEl.prototype, "firstChild", {
  get: function () { return this.children.length ? this.children[0] : null; }
});
FakeEl.prototype.removeChild = function (child) {
  var i = this.children.indexOf(child);
  if (i >= 0) this.children.splice(i, 1);
  return child;
};
FakeEl.prototype.remove = function () { if (this.parentNode) this.parentNode.removeChild(this); };
FakeEl.prototype.setAttribute = function (k, v) { this.attributes[k] = String(v); };
FakeEl.prototype.getAttribute = function (k) {
  return Object.prototype.hasOwnProperty.call(this.attributes, k) ? this.attributes[k] : null;
};
FakeEl.prototype.removeAttribute = function (k) { delete this.attributes[k]; };
FakeEl.prototype.addEventListener = function (t, fn) {
  (this.listeners[t] = this.listeners[t] || []).push(fn);
};
FakeEl.prototype.removeEventListener = function () {};
FakeEl.prototype.dispatchEvent = function () { return true; };
FakeEl.prototype.querySelector = function () { return null; };
FakeEl.prototype.querySelectorAll = function () { return []; };
FakeEl.prototype.contains = function () { return false; };
FakeEl.prototype.focus = function () {};
FakeEl.prototype.select = function () {};
FakeEl.prototype.click = function () {};
FakeEl.prototype.getBoundingClientRect = function () {
  return { top: 0, left: 0, width: 0, height: 0 };
};
FakeEl.prototype.cloneNode = function () { return new FakeEl(this.tagName); };

function FakeText(text) { this.nodeType = 3; this.textContent = String(text); }

var ELEMENTS = {};
function getEl(id) {
  if (!ELEMENTS[id]) { ELEMENTS[id] = new FakeEl("div"); ELEMENTS[id].id = id; }
  return ELEMENTS[id];
}

global.window = {
  location: { search: "?code=600519.SH", href: "http://127.0.0.1:8000/?code=600519.SH", pathname: "/" },
  history: { replaceState: function () {} },
  localStorage: { getItem: function () { return null; }, setItem: function () {}, removeItem: function () {} },
  addEventListener: function () {},
  matchMedia: function () {
    return { matches: false, addEventListener: function () {},
             addListener: function () {}, removeListener: function () {} };
  },
  isSecureContext: false,
  navigator: { clipboard: null }
};
global.document = {
  readyState: "complete",
  documentElement: new FakeEl("html"),
  body: new FakeEl("body"),
  createElement: function (t) { return new FakeEl(t); },
  createTextNode: function (t) { return new FakeText(t); },
  getElementById: getEl,
  querySelector: function () { return null; },
  querySelectorAll: function () { return []; },
  addEventListener: function () {},
  removeEventListener: function () {},
  execCommand: function () { return false; }
};
global.navigator = window.navigator;
global.getComputedStyle = function () {
  return { getPropertyValue: function () { return "#2563eb"; } };
};
global.echarts = {
  getInstanceByDom: function () { return null; },
  init: function () {
    return {
      setOption: function () {}, resize: function () {}, dispose: function () {},
      dispatchAction: function () {}, on: function () {},
      getOption: function () { return { series: [] }; }
    };
  }
};

/* /api/percentile 的信封：一个分位可用、一个为 null，覆盖两条渲染分支 */
var PERCENTILE = {
  code: "600519.SH", raw_code: "600519",
  security: { name: "贵州茅台", exchange: "SH", market: "SH", symbol: "600519" },
  interval_text: "2001-08-28 ~ 2026-10-01（612 个交易日）",
  sample_rows: 612, priority: "配置文件默认值",
  results: [
    { indicator: "pe_ttm", label: "PE-TTM", percentile: 21.5, level: 2, level7: "低估",
      current_value: 18.2, median_value: 30.1, min_value: 8.9, max_value: 70.2,
      sample_count: 612, interval_text: "x" },
    { indicator: "pb", label: "PB", percentile: null, level: 0, level7: "-",
      current_value: 7.1, median_value: 9.9, min_value: 3.1, max_value: 30.0,
      sample_count: 500, interval_text: "x" }
  ],
  windows: [{ label: "近一年", percentiles: { pe_ttm: 21.5, pb: null }, sample_rows: 240 }],
  market_context: {
    pe_ttm: { available: true, total: 3936, rank: 1030, percentile: 26.2,
      histogram: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10], median_percentile: 50.45 },
    pb: { available: false, total: 0, rank: null, percentile: null,
      histogram: [], median_percentile: null }
  }
};

global.fetch = function (url) {
  var body;
  if (String(url).indexOf("/api/health") !== -1) {
    body = { status: "ok", priority: "配置文件默认值", data_as_of: "2026-10-01", db_path: "x" };
  } else if (String(url).indexOf("/api/percentile") !== -1) {
    body = PERCENTILE;
  } else {
    body = { items: [] };
  }
  return Promise.resolve({
    ok: true, status: 200,
    json: function () { return Promise.resolve(body); }
  });
};

var fs = require("fs");
var vm = require("vm");
["common.js", "charts.js", "ui.js"].forEach(function (name) {
  vm.runInThisContext(fs.readFileSync("app/web/static/" + name, "utf8"), { filename: name });
});
global.SL = window.SL;            /* 浏览器里 window.SL 即全局 SL */
vm.runInThisContext(fs.readFileSync(APP_JS, "utf8"), { filename: APP_JS });

setTimeout(function () {
  process.stdout.write(JSON.stringify({
    message: ELEMENTS["message"] ? ELEMENTS["message"].textContent : "",
    metricCards: ELEMENTS["metric-grid"] ? ELEMENTS["metric-grid"].children.length : -1,
    detailRows: ELEMENTS["detail-body"] ? ELEMENTS["detail-body"].children.length : -1,
    secName: ELEMENTS["sec-name"] ? ELEMENTS["sec-name"].textContent : ""
  }));
  process.exit(0);
}, 80);
'''


@pytest.fixture(scope="module")
def analysis_page_runtime():
    result = subprocess.run(
        ["node", "-e", _PAGE_RUNTIME_HARNESS],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        pytest.fail("个股分析页运行期冒烟失败：%s" % (result.stderr or result.stdout)[:800])
    return json.loads(result.stdout)


def test_analysis_page_renders_without_runtime_error(analysis_page_runtime):
    """页面必须能从 init 一直渲染到明细表，且没有抛错被兜底提示接住"""
    assert analysis_page_runtime["secName"] == "贵州茅台", "安全头都没渲染出来"
    assert analysis_page_runtime["message"] == "", \
        "页面渲染抛错并被兜底提示吞下（结果区被清空）：%r" % analysis_page_runtime["message"]
    assert analysis_page_runtime["metricCards"] == 2, "指标卡未渲染完（中途抛错）"
    assert analysis_page_runtime["detailRows"] == 2, "明细表未渲染"


def _html_string_helpers():
    """从共享模块 JSDoc 里解析出「返回 HTML 字符串」的函数名（如 levelBadge）"""
    helpers = set()
    for name in ("common.js", "ui.js", "charts.js", "theme.js"):
        text = open(os.path.join(STATIC, name), encoding="utf-8").read()
        pattern = r"/\*\*(?:(?!\*/).)*@returns\s*\{string\}(?:(?!\*/).)*\*/\s*function\s+([A-Za-z_]\w*)"
        for match in re.finditer(pattern, text, re.S):
            helpers.add(match.group(1))
    return helpers


def test_html_string_helpers_are_not_append_child():
    """appendChild 只接受节点：levelBadge / tempbar / levelLegend 返回 HTML 字符串，
    必须走 innerHTML，直接 appendChild 会在运行期抛 TypeError 并中断整块渲染。"""
    helpers = _html_string_helpers()
    assert {"levelBadge", "tempbar", "levelLegend"} <= helpers, \
        "未能从 JSDoc 解析出 HTML 字符串 helper，请检查注释格式：%s" % sorted(helpers)

    offenders = []
    for name in sorted(os.listdir(STATIC)):
        if not name.endswith(".js") or name == "echarts.min.js":
            continue
        text = open(os.path.join(STATIC, name), encoding="utf-8").read()
        for helper in sorted(helpers):
            pattern = r"appendChild\(\s*(?:SL\.)?%s\s*\(" % re.escape(helper)
            if re.search(pattern, text):
                offenders.append("%s: appendChild(%s(...))" % (name, helper))

    assert not offenders, "HTML 字符串不能直接 appendChild，应改为 innerHTML：%s" % offenders
