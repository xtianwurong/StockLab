/*
 * StockLab 指数估值页前端逻辑 (app/web/static/indices.js)
 *
 * 职责：
 *   1. 加载指数估值列表（/api/indices），渲染指数卡片
 *   2. 点击卡片加载单指数详情（/api/index/detail），渲染走势与分位参考线
 *   3. 指标切换（PE-TTM / PE静 / PB / PS / PCF）
 *
 * 说明：本文件为浏览器端脚本，与 Python 侧无共享约束，保持原生 JS 直白写法。
 */

(function () {
  "use strict";

  // ---------- DOM ----------
  var indicatorTabs = document.getElementById("indicator-tabs");
  var indexGrid = document.getElementById("index-grid");
  var messageBox = document.getElementById("message");
  var detailCard = document.getElementById("detail-card");
  var emptyDetail = document.getElementById("empty-detail");
  var chartDom = document.getElementById("index-chart");
  var btnReload = document.getElementById("btn-reload");

  // ---------- 状态 ----------
  var state = {
    indicator: "pe_ttm",
    selected: null
  };
  var labels = {};
  var items = [];
  var detailChart = null;
  var busyList = false;
  var busyDetail = false;

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
   * 加载指数列表
   */
  function loadList() {
    if (busyList) {
      return;
    }
    busyList = true;
    btnReload.disabled = true;
    clearMessage();

    SL.fetchJson("/api/indices?indicator=" + state.indicator, 30000)
      .then(function (data) {
        if (data.indicator_labels) {
          labels = data.indicator_labels;
          renderTabs();
        }
        if (data.message) {
          showMessage(data.message, "info");
        }
        items = data.items || [];
        renderGrid();
      })
      .catch(function (error) {
        items = [];
        renderGrid();
        showMessage(error.message || "加载失败，请稍后重试", "error");
      })
      .finally(function () {
        busyList = false;
        btnReload.disabled = false;
      });
  }

  /**
   * 渲染指标切换标签
   */
  function renderTabs() {
    indicatorTabs.textContent = "";
    Object.keys(labels).forEach(function (indicator) {
      var tab = SL.el("button",
        "tab" + (indicator === state.indicator ? " active" : ""), labels[indicator]);
      tab.type = "button";
      tab.addEventListener("click", function () {
        if (state.indicator === indicator) {
          return;
        }
        state.indicator = indicator;
        renderTabs();
        loadList();
      });
      indicatorTabs.appendChild(tab);
    });
  }

  /**
   * 渲染指数卡片网格
   */
  function renderGrid() {
    indexGrid.textContent = "";

    if (!items.length) {
      var box = SL.el("div", "card", "");
      var empty = SL.el("div", "empty-state");
      empty.appendChild(SL.el("div", "empty-state-title", "暂无指数数据"));
      empty.appendChild(document.createTextNode("请先同步 reference.index_memberships 与估值历史"));
      box.appendChild(empty);
      indexGrid.appendChild(box);
      return;
    }

    items.forEach(function (item) {
      var card = SL.el("div",
        "index-card" + (state.selected === item.index_code ? " active" : ""));

      var top = SL.el("div", "index-top");
      var nameBox = SL.el("div");
      nameBox.appendChild(SL.el("div", "index-name", item.index_name));
      nameBox.appendChild(SL.el("div", "index-code", item.index_code));
      top.appendChild(nameBox);
      var badge = SL.el("div");
      badge.innerHTML = SL.levelBadge(item.percentile, item.level7);
      top.appendChild(badge);
      card.appendChild(top);

      card.appendChild(SL.el("div", "index-value", SL.formatPercent(item.percentile)));
      card.appendChild(SL.el("div", "index-current",
        "当前 " + SL.formatNumber(item.current_value) + " / 中位 " + SL.formatNumber(item.median_value)));

      var temp = SL.el("div", "index-temp");
      temp.innerHTML = SL.tempbar(item.percentile);
      card.appendChild(temp);

      var facts = SL.el("div", "index-facts");
      [
        ["成分股", SL.formatInt(item.member_count)],
        ["历史区间", item.interval_text ? item.interval_text.split("（")[0] : "-"],
        ["样本", SL.formatInt(item.sample_count)]
      ].forEach(function (pair) {
        var cell = SL.el("div");
        cell.appendChild(document.createTextNode(pair[0]));
        cell.appendChild(SL.el("b", "", pair[1]));
        facts.appendChild(cell);
      });
      card.appendChild(facts);

      card.addEventListener("click", function () {
        if (state.selected === item.index_code) {
          return;
        }
        state.selected = item.index_code;
        renderGrid();
        loadDetail(item.index_code, item.index_name);
      });

      indexGrid.appendChild(card);
    });
  }

  /**
   * 加载并渲染单个指数的详情
   *
   * @param {string} indexCode 指数代码
   * @param {string} indexName 指数名称
   */
  function loadDetail(indexCode, indexName) {
    if (busyDetail) {
      return;
    }
    busyDetail = true;
    clearMessage();

    SL.fetchJson(
      "/api/index/detail?code=" + encodeURIComponent(indexCode) +
      "&indicator=" + state.indicator,
      30000
    )
      .then(function (data) {
        if (!data.results || !data.results.length) {
          detailCard.className = "card section hidden";
          emptyDetail.className = "card section";
          showMessage(data.message || "该指数暂无数据", "info");
          return;
        }
        renderDetail(indexCode, indexName, data);
      })
      .catch(function (error) {
        detailCard.className = "card section hidden";
        emptyDetail.className = "card section";
        showMessage(error.message || "加载详情失败", "error");
      })
      .finally(function () {
        busyDetail = false;
      });
  }

  /**
   * 渲染详情面板
   *
   * @param {string} indexCode 指数代码
   * @param {string} indexName 指数名称
   * @param {object} data 详情应答
   */
  function renderDetail(indexCode, indexName, data) {
    emptyDetail.className = "card section hidden";
    detailCard.className = "card section";

    document.getElementById("detail-name").textContent = indexName;
    document.getElementById("detail-code").textContent = indexCode;

    var target = data.results[0];
    var facts = document.getElementById("detail-facts");
    facts.textContent = "";
    [
      ["分位", SL.formatPercent(target.percentile)],
      ["当前", SL.formatNumber(target.current_value)],
      ["中位数", SL.formatNumber(target.median_value)],
      ["区间", data.interval_text || "-"],
      ["样本", SL.formatInt(data.sample_rows)]
    ].forEach(function (pair) {
      var cell = SL.el("span");
      cell.appendChild(document.createTextNode(pair[0] + " "));
      cell.appendChild(SL.el("b", "", pair[1]));
      facts.appendChild(cell);
    });

    var meta = {};
    data.results.forEach(function (item) {
      meta[item.indicator] = item.label;
    });

    detailChart = SL.renderChart(chartDom, SL.buildTrendOption({
      history: data.history,
      indicatorMeta: meta,
      primary: state.indicator
    }));

    // 滚动到详情区，避免大屏下点了卡片却看不到变化
    if (detailCard.scrollIntoView) {
      detailCard.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }

  // ---------- 事件 ----------
  btnReload.addEventListener("click", function () {
    loadList();
  });

  window.addEventListener("resize", function () {
    if (detailChart) {
      detailChart.resize();
    }
  });

  var legendBox = document.getElementById("index-legend");
  if (legendBox) {
    legendBox.innerHTML = SL.levelLegend();
  }

  SL.checkHealth();
  renderTabs();
  loadList();
})();
