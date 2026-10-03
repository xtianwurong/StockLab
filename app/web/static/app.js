/*
 * StockLab 个股分析页前端逻辑 (app/web/static/app.js)
 *
 * 职责：
 *   1. 证券代码/名称联想（/api/securities），键盘可完整操作
 *   2. 触发分位分析（/api/percentile）并渲染：标的信息 / 主指标 / 多窗口 /
 *      指标卡 / 走势图 / 明细表
 *   3. 把查询条件写回地址栏，刷新与分享均可还原结果
 *
 * 【本版修复的交互缺陷】
 *   - Enter 在联想列表打开时毫无反应 -> 单一 keydown 处理，Enter 永远有明确行为
 *   - 分析失败时旧结果残留 -> 失败即清理结果区
 *   - 请求无超时导致按钮永久「分析中...」-> 统一走 SL.fetchJson（15s 超时）
 *   - 联想项用 innerHTML 拼接存在注入风险 -> 全部改用 textContent 构建
 *   - 结果区不显示标的名称 -> 新增标的信息头
 *   - 刷新即丢失结果 -> 查询条件写入 URL，加载时自动还原
 *
 * 说明：本文件为浏览器端脚本，与 Python 侧无共享约束，保持原生 JS 直白写法。
 */

(function () {
  "use strict";

  // ---------- DOM 引用 ----------
  var codeInput = document.getElementById("code-input");
  var suggestList = document.getElementById("suggest-list");
  var periodSelect = document.getElementById("period-select");
  var startDateInput = document.getElementById("start-date");
  var endDateInput = document.getElementById("end-date");
  var inputPe = document.getElementById("input-pe");
  var inputPb = document.getElementById("input-pb");
  var btnAnalyze = document.getElementById("btn-analyze");
  var messageBox = document.getElementById("message");
  var resultArea = document.getElementById("result-area");
  var emptyState = document.getElementById("empty-state");
  var metricGrid = document.getElementById("metric-grid");
  var indicatorTabs = document.getElementById("indicator-tabs");
  var detailBody = document.getElementById("detail-body");
  var windowsBox = document.getElementById("windows");
  var chartDom = document.getElementById("trend-chart");

  // ---------- 状态 ----------
  var suggestItems = [];     // 联想命中项
  var activeIndex = -1;      // 键盘高亮位置
  var suggestTimer = null;   // 输入防抖计时器
  var lastPayload = null;    // 最近一次成功的分析结果
  var primaryIndicator = "pe_ttm";
  var trendChart = null;
  var busy = false;          // 请求进行中，防止重复提交

  // ---------- 工具 ----------

  /**
   * 显示消息条
   *
   * @param {string} text 文本
   * @param {string} kind info | error
   */
  function showMessage(text, kind) {
    messageBox.textContent = text;
    messageBox.className = "message show " + (kind || "info");
  }

  /**
   * 清空消息条
   */
  function clearMessage() {
    messageBox.textContent = "";
    messageBox.className = "message";
  }

  /**
   * 清理结果区（分析开始前或失败时调用，避免旧结果被误读为新结果）
   */
  function clearResult() {
    lastPayload = null;
    resultArea.className = "hidden";
    emptyState.className = "card section";
    if (trendChart) {
      trendChart.clear();
    }
  }

  /**
   * 创建带指定文本的元素
   *
   * @param {string} tag 标签名
   * @param {string} className class
   * @param {string} text 文本内容
   * @returns {HTMLElement} 元素
   */
  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (text !== undefined && text !== null) {
      node.textContent = text;
    }
    return node;
  }

  // ---------- 联想 ----------

  /**
   * 渲染联想下拉（全部用 DOM 构建，杜绝 HTML 注入）
   */
  function renderSuggest() {
    suggestList.textContent = "";

    if (!suggestItems.length) {
      suggestList.appendChild(el("div", "suggest-empty", "无匹配标的"));
      suggestList.className = "suggest open";
      return;
    }

    suggestItems.forEach(function (item, index) {
      var row = el("div", "suggest-item" + (index === activeIndex ? " active" : ""));
      row.appendChild(el("span", "suggest-code", item.code));
      row.appendChild(el("span", "suggest-name", item.name || "-"));
      row.appendChild(el("span", "suggest-industry", item.market || ""));
      row.addEventListener("mousedown", function (event) {
        event.preventDefault();  // 防止输入框失焦导致列表先被关闭
        applySuggestItem(index);
      });
      suggestList.appendChild(row);
    });

    suggestList.className = "suggest open";
    scrollActiveIntoView();
  }

  /**
   * 关闭联想下拉
   */
  function hideSuggest() {
    suggestList.className = "suggest";
    suggestList.textContent = "";
    activeIndex = -1;
    suggestItems = [];
  }

  /**
   * 把某一条联想项回填到输入框
   *
   * @param {number} index 下标
   */
  function applySuggestItem(index) {
    var item = suggestItems[index];
    if (!item) {
      return;
    }
    codeInput.value = item.code;
    hideSuggest();
  }

  /**
   * 让键盘高亮项滚进可视区
   */
  function scrollActiveIntoView() {
    if (activeIndex < 0) {
      return;
    }
    var node = suggestList.children[activeIndex];
    if (node && node.scrollIntoView) {
      node.scrollIntoView({ block: "nearest" });
    }
  }

  /**
   * 输入防抖后请求联想
   */
  function requestSuggest() {
    var query = codeInput.value.trim();
    if (!query) {
      hideSuggest();
      return;
    }
    clearTimeout(suggestTimer);
    suggestTimer = setTimeout(function () {
      SL.fetchJson("/api/securities?q=" + encodeURIComponent(query), 8000)
        .then(function (data) {
          if (codeInput.value.trim() !== query) {
            return;  // 输入已变化，丢弃过期结果
          }
          suggestItems = data.items || [];
          activeIndex = -1;
          renderSuggest();
        })
        .catch(function () {
          hideSuggest();
        });
    }, 200);
  }

  /**
   * 输入框键盘处理（唯一的 keydown 监听，避免两个监听器互相打架）
   *
   * @param {KeyboardEvent} event 事件
   */
  function onCodeKeydown(event) {
    var isOpen = suggestList.className.indexOf("open") !== -1;

    if (event.key === "Escape") {
      hideSuggest();
      return;
    }

    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      if (!suggestItems.length) {
        return;
      }
      event.preventDefault();
      if (!isOpen) {
        renderSuggest();
      }
      var step = event.key === "ArrowDown" ? 1 : -1;
      activeIndex = activeIndex + step;
      if (activeIndex < 0) {
        activeIndex = suggestItems.length - 1;
      }
      if (activeIndex >= suggestItems.length) {
        activeIndex = 0;
      }
      renderSuggest();
      return;
    }

    if (event.key === "Enter") {
      event.preventDefault();
      clearTimeout(suggestTimer);
      hideSuggest();
      // 有高亮项则回填其代码；没有就直接用当前输入（后端支持中文名解析）
      if (activeIndex >= 0 && suggestItems[activeIndex]) {
        codeInput.value = suggestItems[activeIndex].code;
      }
      analyze();
    }
  }

  // ---------- 分析 ----------

  /**
   * 收集查询参数
   *
   * @returns {string} 查询串
   */
  function buildQuery() {
    var params = [];
    params.push("code=" + encodeURIComponent(codeInput.value.trim()));
    params.push("period=" + encodeURIComponent(periodSelect.value));
    if (startDateInput.value) {
      params.push("start_date=" + startDateInput.value);
    }
    if (endDateInput.value) {
      params.push("end_date=" + endDateInput.value);
    }
    var pe = inputPe.value.trim();
    if (pe) {
      params.push("pe_ttm=" + encodeURIComponent(pe));
    }
    var pb = inputPb.value.trim();
    if (pb) {
      params.push("pb=" + encodeURIComponent(pb));
    }
    return params.join("&");
  }

  /**
   * 把查询条件写入地址栏（不新增历史记录）
   *
   * @param {string} query 查询串
   */
  function syncUrl(query) {
    try {
      window.history.replaceState(null, "", "?" + query);
    } catch (err) {
      // 某些内嵌环境禁用 pushState，忽略即可
    }
  }

  /**
   * 触发分析
   */
  function analyze() {
    if (busy) {
      return;
    }
    var code = codeInput.value.trim();
    if (!code) {
      showMessage("请先输入证券代码或名称", "error");
      codeInput.focus();
      return;
    }

    clearMessage();
    clearResult();
    busy = true;
    btnAnalyze.disabled = true;
    btnAnalyze.textContent = "分析中...";

    var query = buildQuery();
    syncUrl(query);

    SL.fetchJson("/api/percentile?" + query, 20000)
      .then(function (data) {
        if (!data.results || !data.results.length) {
          emptyState.className = "card section";
          showMessage(data.message || "没有可展示的结果", "error");
          return;
        }
        renderResult(data);
      })
      .catch(function (error) {
        clearResult();
        showMessage(error.message || "分析失败，请稍后重试", "error");
      })
      .finally(function () {
        busy = false;
        btnAnalyze.disabled = false;
        btnAnalyze.textContent = "开始分析";
      });
  }

  // ---------- 渲染 ----------

  /**
   * 渲染整块结果
   *
   * @param {object} data /api/percentile 应答
   */
  function renderResult(data) {
    lastPayload = data;
    emptyState.className = "hidden";
    resultArea.className = "";

    if (data.message) {
      showMessage(data.message, "info");
    }

    renderSecurityHead(data);
    pickPrimaryIndicator(data);
    renderHero(data);
    renderWindows(data);
    renderMetricCards(data);
    renderIndicatorTabs(data);
    renderDetailTable(data);
    renderTrendChart();
  }

  /**
   * 渲染标的信息头
   *
   * @param {object} data 应答
   */
  function renderSecurityHead(data) {
    var security = data.security || {};
    document.getElementById("sec-name").textContent = security.name || data.code;
    document.getElementById("sec-code").textContent = data.code;

    var facts = document.getElementById("sec-facts");
    facts.textContent = "";
    [
      ["交易所", security.exchange],
      ["市场", security.market],
      ["代码", security.symbol]
    ].forEach(function (pair) {
      if (pair[1]) {
        var span = el("span");
        span.appendChild(document.createTextNode(pair[0] + " "));
        span.appendChild(el("b", "", pair[1]));
        facts.appendChild(span);
      }
    });

    document.getElementById("meta-interval").textContent = data.interval_text || "-";
    document.getElementById("meta-rows").textContent = SL.formatInt(data.sample_rows);
    document.getElementById("meta-priority").textContent = data.priority || "-";
  }

  /**
   * 选定主指标：优先保留用户已选的那个，否则取第一个分位可用的指标
   *
   * @param {object} data 应答
   */
  function pickPrimaryIndicator(data) {
    var indicators = data.results.map(function (item) { return item.indicator; });
    if (indicators.indexOf(primaryIndicator) !== -1) {
      return;
    }
    var usable = data.results.filter(function (item) {
      return item.percentile !== null && item.percentile !== undefined;
    });
    primaryIndicator = usable.length ? usable[0].indicator : (indicators[0] || "pe_ttm");
  }

  /**
   * 找到指定指标的结果项
   *
   * @param {object} data 应答
   * @param {string} indicator 指标名
   * @returns {object|undefined} 结果项
   */
  function findResult(data, indicator) {
    for (var i = 0; i < data.results.length; i++) {
      if (data.results[i].indicator === indicator) {
        return data.results[i];
      }
    }
    return undefined;
  }

  /**
   * 渲染主指标大卡
   *
   * @param {object} data 应答
   */
  function renderHero(data) {
    var item = findResult(data, primaryIndicator) || data.results[0];
    document.getElementById("hero-name").textContent = item.label;
    document.getElementById("hero-value").textContent = SL.formatPercent(item.percentile);
    document.getElementById("hero-value").style.color =
      item.percentile === null ? "var(--text-faint)" : "var(--text-main)";
    document.getElementById("hero-badge").innerHTML = SL.levelBadge(item.percentile, item.level7);
    document.getElementById("hero-current").textContent =
      "当前值 " + SL.formatNumber(item.current_value) + "　中位数 " + SL.formatNumber(item.median_value);
    document.getElementById("hero-temp").innerHTML = SL.tempbar(item.percentile);

    var facts = document.getElementById("hero-facts");
    facts.textContent = "";
    [
      ["历史最小", item.min_value],
      ["历史最大", item.max_value],
      ["样本天数", item.sample_count]
    ].forEach(function (pair) {
      var box = el("div");
      box.appendChild(el("div", "fact-k", pair[0]));
      box.appendChild(el("div", "fact-v",
        pair[0] === "样本天数" ? SL.formatInt(pair[1]) : SL.formatNumber(pair[1])));
      facts.appendChild(box);
    });
  }

  /**
   * 渲染多窗口分位对比
   *
   * @param {object} data 应答
   */
  function renderWindows(data) {
    windowsBox.textContent = "";
    var windows = data.windows || [];
    if (!windows.length) {
      windowsBox.appendChild(el("div", "empty-state", "无窗口数据"));
      return;
    }

    windows.forEach(function (win) {
      var percentile = win.percentiles[primaryIndicator];
      var cell = el("div", "win-cell");
      cell.appendChild(el("div", "win-label", win.label));
      cell.appendChild(el("div", "win-value",
        percentile === null || percentile === undefined ? "-" : percentile.toFixed(1) + "%"));

      var badge = el("div", "win-badge");
      badge.innerHTML = SL.levelBadge(percentile);
      cell.appendChild(badge);
      cell.appendChild(el("div", "win-rows", SL.formatInt(win.sample_rows) + " 个交易日"));
      windowsBox.appendChild(cell);
    });
  }

  /**
   * 渲染指标卡网格
   *
   * @param {object} data 应答
   */
  function renderMetricCards(data) {
    metricGrid.textContent = "";

    data.results.forEach(function (item) {
      var usable = item.percentile !== null && item.percentile !== undefined;
      var card = el("div",
        "metric-card" + (item.indicator === primaryIndicator ? " primary" : "") +
        (usable ? "" : " na"));

      var top = el("div", "metric-top");
      top.appendChild(el("span", "metric-name", item.label));
      top.appendChild(SL.levelBadge(item.percentile, item.level7));
      card.appendChild(top);

      card.appendChild(el("div", "metric-percentile", SL.formatPercent(item.percentile)));
      card.appendChild(el("div", "metric-current",
        "当前 " + SL.formatNumber(item.current_value) + " / 中位 " + SL.formatNumber(item.median_value)));

      var temp = el("div", "metric-temp");
      temp.innerHTML = SL.tempbar(item.percentile);
      card.appendChild(temp);

      if (usable) {
        card.addEventListener("click", function () {
          setPrimary(item.indicator);
        });
      }
      metricGrid.appendChild(card);
    });
  }

  /**
   * 渲染指标切换标签
   *
   * @param {object} data 应答
   */
  function renderIndicatorTabs(data) {
    indicatorTabs.textContent = "";
    data.results.forEach(function (item) {
      var tab = el("button",
        "tab" + (item.indicator === primaryIndicator ? " active" : ""), item.label);
      tab.type = "button";
      tab.addEventListener("click", function () {
        setPrimary(item.indicator);
      });
      indicatorTabs.appendChild(tab);
    });
  }

  /**
   * 切换主指标并局部刷新
   *
   * @param {string} indicator 新的主指标
   */
  function setPrimary(indicator) {
    if (primaryIndicator === indicator || !lastPayload) {
      return;
    }
    primaryIndicator = indicator;
    renderHero(lastPayload);
    renderWindows(lastPayload);
    renderMetricCards(lastPayload);
    renderIndicatorTabs(lastPayload);
    renderTrendChart();
  }

  /**
   * 渲染明细表
   *
   * @param {object} data 应答
   */
  function renderDetailTable(data) {
    detailBody.textContent = "";

    data.results.forEach(function (item) {
      var row = document.createElement("tr");

      var nameCell = el("td");
      nameCell.appendChild(el("b", "", item.label));
      if (item.indicator === primaryIndicator) {
        nameCell.appendChild(el("span", "", "  ●"));
        nameCell.lastChild.style.color = "var(--accent)";
        nameCell.lastChild.style.fontSize = "10px";
      }
      row.appendChild(nameCell);

      row.appendChild(el("td", "ta-r num", SL.formatNumber(item.current_value)));

      var pctCell = el("td", "ta-r num");
      pctCell.style.fontWeight = "650";
      pctCell.textContent = SL.formatPercent(item.percentile);
      if (item.percentile !== null && item.percentile !== undefined) {
        pctCell.style.color = (SL.levelOf(item.percentile) || {}).color || "inherit";
      }
      row.appendChild(pctCell);

      var levelCell = el("td", "ta-c");
      levelCell.innerHTML = SL.levelBadge(item.percentile, item.level7);
      row.appendChild(levelCell);

      row.appendChild(el("td", "ta-r num", SL.formatNumber(item.median_value)));
      row.appendChild(el("td", "ta-r num", SL.formatNumber(item.min_value)));
      row.appendChild(el("td", "ta-r num", SL.formatNumber(item.max_value)));
      row.appendChild(el("td", "ta-r num", SL.formatInt(item.sample_count)));

      detailBody.appendChild(row);
    });
  }

  /**
   * 组织指标中文名映射
   *
   * @param {object} data 应答
   * @returns {object} 指标 -> 中文名
   */
  function buildMeta(data) {
    var meta = {};
    data.results.forEach(function (item) {
      meta[item.indicator] = item.label;
    });
    return meta;
  }

  /**
   * 渲染走势图
   */
  function renderTrendChart() {
    if (!lastPayload || !lastPayload.history) {
      return;
    }
    var option = SL.buildTrendOption({
      history: lastPayload.history,
      indicatorMeta: buildMeta(lastPayload),
      primary: primaryIndicator
    });
    trendChart = SL.renderChart(chartDom, option);
  }

  // ---------- 页面初始化 ----------

  /**
   * 从地址栏恢复查询条件并（若有 code）自动分析
   */
  function restoreFromUrl() {
    var params = new URLSearchParams(window.location.search);
    var code = params.get("code");
    if (!code) {
      return;
    }
    codeInput.value = code;
    if (params.get("period")) {
      periodSelect.value = params.get("period");
    }
    if (params.get("start_date")) {
      startDateInput.value = params.get("start_date");
    }
    if (params.get("end_date")) {
      endDateInput.value = params.get("end_date");
    }
    if (params.get("pe_ttm")) {
      inputPe.value = params.get("pe_ttm");
    }
    if (params.get("pb")) {
      inputPb.value = params.get("pb");
    }
    analyze();
  }

  /**
   * 拉取健康检查，点亮顶栏状态
   */
  function checkHealth() {
    var dot = document.getElementById("status-dot");
    var text = document.getElementById("status-text");
    SL.fetchJson("/api/health", 6000)
      .then(function (data) {
        dot.className = "status-dot ok";
        text.textContent = "服务正常 · " + data.priority;
      })
      .catch(function () {
        dot.className = "status-dot bad";
        text.textContent = "服务不可用";
      });
  }

  // 事件绑定（普通函数引用，不用装饰器式写法）
  codeInput.addEventListener("input", requestSuggest);
  codeInput.addEventListener("keydown", onCodeKeydown);
  codeInput.addEventListener("focus", function () {
    if (suggestItems.length) {
      renderSuggest();
    }
  });

  document.addEventListener("click", function (event) {
    if (event.target !== codeInput) {
      hideSuggest();
    }
  });

  btnAnalyze.addEventListener("click", analyze);

  document.getElementById("btn-reset-zoom").addEventListener("click", function () {
    if (!trendChart) {
      return;
    }
    trendChart.dispatchAction({ type: "dataZoom", start: 0, end: 100 });
  });

  window.addEventListener("resize", function () {
    if (trendChart) {
      trendChart.resize();
    }
  });

  checkHealth();
  restoreFromUrl();
})();
