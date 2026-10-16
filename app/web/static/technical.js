/*
 * StockLab 技术面与风险前端逻辑 (app/web/static/technical.js)
 *
 * 职责：
 *   1. 读取 /api/technical，渲染概览卡、价格与均线图、RSI 图、回撤曲线、
 *      风险指标与区间动量
 *   2. 支持 URL 参数（?code=&start_date=&end_date=&benchmark=）直达分析
 *
 * 说明：本文件为浏览器端脚本，保持原生 JS 直白写法，配色一律走 SL.charts 令牌。
 */

(function () {
  "use strict";

  // ---------- DOM ----------
  var codeInput = document.getElementById("tech-code");
  var startInput = document.getElementById("tech-start");
  var endInput = document.getElementById("tech-end");
  var benchmarkInput = document.getElementById("tech-benchmark");
  var btnAnalyze = document.getElementById("btn-analyze");
  var messageBox = document.getElementById("message");
  var statGrid = document.getElementById("stat-grid");
  var priceCard = document.getElementById("price-card");
  var priceTitle = document.getElementById("price-title");
  var priceHint = document.getElementById("price-hint");
  var riskCard = document.getElementById("risk-card");
  var riskMetrics = document.getElementById("risk-metrics");
  var momentumCard = document.getElementById("momentum-card");
  var momentumMetrics = document.getElementById("momentum-metrics");
  var rsiCard = document.getElementById("rsi-card");
  var ddCard = document.getElementById("dd-card");
  var ddHint = document.getElementById("dd-hint");
  var priceDom = document.getElementById("price-chart");
  var rsiDom = document.getElementById("rsi-chart");
  var ddDom = document.getElementById("dd-chart");

  var charts = {
    price: null,
    rsi: null,
    dd: null
  };
  var busy = false;

  /**
   * 把小数收益率格式化为百分数文本
   *
   * @param {number} fraction 小数（0.05 表示 5%）
   * @param {number} [digits] 小数位，默认 2
   * @returns {string} 带符号的百分数
   */
  function asPercent(fraction, digits) {
    if (fraction === null || fraction === undefined || isNaN(fraction)) {
      return "-";
    }
    var value = fraction * 100;
    var text = value.toFixed(digits === undefined ? 2 : digits) + "%";
    return value > 0 ? ("+" + text) : text;
  }

  /**
   * 数值配正负样式类（涨红跌绿，A 股口径）
   *
   * @param {number} value 数值
   * @returns {string} "pos" | "neg" | ""
   */
  function signClass(value) {
    if (value === null || value === undefined || isNaN(value)) {
      return "";
    }
    if (value > 0) {
      return "pos";
    }
    if (value < 0) {
      return "neg";
    }
    return "";
  }

  /**
   * 构造一个指标格
   *
   * @param {string} label 名称
   * @param {string} text 值文本
   * @param {string} [sign] 正负样式类
   * @returns {HTMLElement} 指标节点
   */
  function metricBox(label, text, sign) {
    var box = SL.el("div", "tech-metric");
    box.appendChild(SL.el("div", "tech-metric-k", label));
    box.appendChild(SL.el("div", "tech-metric-v" + (sign ? " " + sign : ""), text));
    return box;
  }

  /**
   * 显示消息
   *
   * @param {string} text 文本
   * @param {string} kind info | error
   */
  function showMessage(text, kind) {
    SL.showMessage(messageBox, text, kind);
  }

  /**
   * 切换结果区的显隐
   *
   * @param {boolean} visible 是否显示
   */
  function setSectionsVisible(visible) {
    [statGrid, priceCard, riskCard, momentumCard, rsiCard, ddCard].forEach(function (node) {
      node.classList.toggle("hidden", !visible);
    });
  }

  /**
   * 渲染概览卡
   *
   * @param {object} data 应答
   */
  function renderStats(data) {
    var risk = data.risk || {};
    var technical = data.technical || {};
    document.getElementById("stat-close").textContent = SL.formatNumber(technical.close);
    document.getElementById("stat-return").textContent = asPercent(risk.annual_return);
    document.getElementById("stat-return").className =
      "stat-v " + signClass(risk.annual_return);
    document.getElementById("stat-vol").textContent = asPercent(risk.annual_volatility);
    document.getElementById("stat-sharpe").textContent = SL.formatNumber(risk.sharpe);
    var drawdownText = asPercent(risk.max_drawdown);
    document.getElementById("stat-dd").textContent = drawdownText;
    document.getElementById("stat-dd").className = "stat-v" + (drawdownText === "-" ? "" : " neg");
    document.getElementById("stat-rsi").textContent = SL.formatNumber(technical.rsi14, 1);
  }

  /**
   * 渲染价格与均线图
   *
   * @param {object} data 应答
   */
  function renderPriceChart(data) {
    var price = data.price || {};
    priceTitle.textContent = (data.name || data.code) + " · 价格与均线";
    priceHint.textContent = "数据 " + (price.dates || []).length + " 个交易日，区间 " +
      (price.dates && price.dates.length ? (price.dates[0] + " ~ " + price.dates[price.dates.length - 1]) : "-");

    var rebuild = function () {
      var palette = SL.charts.palette();
      return Object.assign(SL.charts.baseOption(["收盘价", "MA5", "MA20", "MA60"]), {
        grid: SL.charts.grid({ left: 58, right: 20, top: 44, bottom: 48 }),
        tooltip: SL.charts.tooltip(),
        xAxis: SL.charts.categoryAxis(price.dates || []),
        yAxis: SL.charts.valueAxis({ name: "价格" }),
        dataZoom: SL.charts.dataZoom(),
        series: [
          { name: "收盘价", type: "line", showSymbol: false, data: price.close,
            lineStyle: { width: 1.8, color: palette.accent },
            itemStyle: { color: palette.accent } },
          { name: "MA5", type: "line", showSymbol: false, data: price.ma5, lineStyle: { width: 1 } },
          { name: "MA20", type: "line", showSymbol: false, data: price.ma20, lineStyle: { width: 1 } },
          { name: "MA60", type: "line", showSymbol: false, data: price.ma60, lineStyle: { width: 1 } }
        ]
      });
    };
    charts.price = SL.renderChart(priceDom, rebuild(), rebuild);
  }

  /**
   * 渲染 RSI 图（带 30/70 参考线）
   *
   * @param {object} data 应答
   */
  function renderRsiChart(data) {
    var price = data.price || {};
    var rebuild = function () {
      var palette = SL.charts.palette();
      return Object.assign(SL.charts.baseOption([]), {
        grid: SL.charts.grid({ left: 58, right: 20, top: 20, bottom: 30 }),
        tooltip: SL.charts.tooltip(),
        xAxis: SL.charts.categoryAxis(price.dates || []),
        yAxis: SL.charts.valueAxis({ name: "RSI", min: 0, max: 100 }),
        series: [{
          name: "RSI(14)",
          type: "line",
          showSymbol: false,
          data: price.rsi14,
          lineStyle: { width: 1.6, color: palette.accent },
          itemStyle: { color: palette.accent },
          markLine: {
            silent: true,
            symbol: "none",
            lineStyle: { type: "dashed", color: palette.textFaint },
            data: [{ yAxis: 70 }, { yAxis: 30 }]
          }
        }]
      });
    };
    charts.rsi = SL.renderChart(rsiDom, rebuild(), rebuild);
  }

  /**
   * 渲染回撤曲线
   *
   * @param {object} data 应答
   */
  function renderDrawdownChart(data) {
    var price = data.price || {};
    var drawdown = data.drawdown || {};
    ddHint.textContent = "最大回撤 " + asPercent(drawdown.max_drawdown) +
      (drawdown.trough_date ? ("，谷底 " + drawdown.trough_date) : "") +
      (drawdown.recovery_date ? ("，修复 " + drawdown.recovery_date) : "，尚未修复");

    var rebuild = function () {
      var palette = SL.charts.palette();
      return Object.assign(SL.charts.baseOption([]), {
        grid: SL.charts.grid({ left: 58, right: 20, top: 20, bottom: 30 }),
        tooltip: SL.charts.tooltip(),
        xAxis: SL.charts.categoryAxis(price.dates || []),
        yAxis: SL.charts.valueAxis({ name: "回撤" }),
        series: [{
          name: "回撤",
          type: "line",
          showSymbol: false,
          data: price.drawdown,
          lineStyle: { width: 1.2, color: palette.danger },
          areaStyle: { color: palette.danger, opacity: 0.12 }
        }]
      });
    };
    charts.dd = SL.renderChart(ddDom, rebuild(), rebuild);
  }

  /**
   * 渲染风险指标与动量格
   *
   * @param {object} data 应答
   */
  function renderMetrics(data) {
    var risk = data.risk || {};
    var technical = data.technical || {};

    riskMetrics.textContent = "";
    riskMetrics.appendChild(metricBox("年化收益", asPercent(risk.annual_return), signClass(risk.annual_return)));
    riskMetrics.appendChild(metricBox("年化波动", asPercent(risk.annual_volatility)));
    riskMetrics.appendChild(metricBox("夏普比率", SL.formatNumber(risk.sharpe)));
    riskMetrics.appendChild(metricBox("索提诺比率", SL.formatNumber(risk.sortino)));
    riskMetrics.appendChild(metricBox("卡玛比率", SL.formatNumber(risk.calmar)));
    riskMetrics.appendChild(metricBox("最大回撤", asPercent(risk.max_drawdown), "neg"));
    riskMetrics.appendChild(metricBox("Beta", SL.formatNumber(risk.beta)));
    riskMetrics.appendChild(metricBox("年化 Alpha", asPercent(risk.alpha), signClass(risk.alpha)));
    riskMetrics.appendChild(metricBox("R²", SL.formatNumber(risk.r_squared)));
    riskMetrics.appendChild(metricBox("样本交易日", SL.formatInt(risk.sample_days)));

    momentumMetrics.textContent = "";
    [["ret_5d", "近 5 日"], ["ret_20d", "近 20 日"], ["ret_60d", "近 60 日"],
     ["ret_120d", "近 120 日"], ["ret_250d", "近 250 日"]].forEach(function (pair) {
      var value = technical[pair[0]];
      momentumMetrics.appendChild(metricBox(pair[1], asPercent(value), signClass(value)));
    });
  }

  /**
   * 渲染一次完整应答
   *
   * @param {object} data 应答
   */
  function render(data) {
    if (data.message) {
      showMessage(data.message, "info");
    }
    if (!data.price || !data.price.dates || !data.price.dates.length) {
      setSectionsVisible(false);
      return;
    }
    setSectionsVisible(true);
    renderStats(data);
    renderPriceChart(data);
    renderRsiChart(data);
    renderDrawdownChart(data);
    renderMetrics(data);
  }

  /**
   * 发起分析请求
   */
  function analyze() {
    if (busy) {
      return;
    }
    var code = codeInput.value.trim();
    if (!code) {
      showMessage("请输入证券代码或名称", "error");
      codeInput.focus();
      return;
    }
    busy = true;
    btnAnalyze.disabled = true;
    SL.clearMessage(messageBox);

    var params = new URLSearchParams({ code: code });
    if (startInput.value) {
      params.set("start_date", startInput.value);
    }
    if (endInput.value) {
      params.set("end_date", endInput.value);
    }
    if (benchmarkInput.value.trim()) {
      params.set("benchmark", benchmarkInput.value.trim());
    }
    syncUrl(params);

    SL.fetchJson("/api/technical?" + params.toString(), 30000)
      .then(function (data) {
        if (data.error) {
          setSectionsVisible(false);
          showMessage(data.error, "error");
          return;
        }
        render(data);
      })
      .catch(function (error) {
        showMessage(error.message || "分析失败，请稍后重试", "error");
      })
      .finally(function () {
        busy = false;
        btnAnalyze.disabled = false;
      });
  }

  /**
   * 把当前筛选写回地址栏（便于分享/刷新）
   *
   * @param {URLSearchParams} params 参数
   */
  function syncUrl(params) {
    var query = params.toString();
    var url = window.location.pathname + (query ? "?" + query : "");
    window.history.replaceState(null, "", url);
  }

  /**
   * 初始化：读取 URL 参数并自动分析
   */
  function init() {
    var params = SL.ui.queryParams();
    if (params.code) {
      codeInput.value = params.code;
    }
    if (params.start_date) {
      startInput.value = params.start_date;
    }
    if (params.end_date) {
      endInput.value = params.end_date;
    }
    if (params.benchmark) {
      benchmarkInput.value = params.benchmark;
    }
    if (codeInput.value) {
      analyze();
    }
  }

  btnAnalyze.addEventListener("click", analyze);
  codeInput.addEventListener("keydown", function (event) {
    if (event.key === "Enter") {
      analyze();
    }
  });

  window.addEventListener("resize", function () {
    Object.keys(charts).forEach(function (key) {
      if (charts[key]) {
        charts[key].resize();
      }
    });
  });

  SL.checkHealth();
  init();
})();
