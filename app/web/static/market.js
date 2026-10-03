/*
 * StockLab 全市场 Dashboard 前端逻辑 (app/web/static/market.js)
 *
 * 职责：
 *   1. 加载全市场分位排行（/api/market/ranking），渲染统计卡 / 分位分布 /
 *      七档评级分布 / 排行表 / 分页
 *   2. 指标、市场、搜索、排序、分页的交互
 *   3. 点击任意行跳转到该股的个股分析页
 *
 * 说明：本文件为浏览器端脚本，与 Python 侧无共享约束，保持原生 JS 直白写法。
 */

(function () {
  "use strict";

  // ---------- DOM ----------
  var indicatorTabs = document.getElementById("indicator-tabs");
  var marketFilter = document.getElementById("market-filter");
  var searchInput = document.getElementById("search-input");
  var sortSelect = document.getElementById("sort-select");
  var btnReload = document.getElementById("btn-reload");
  var messageBox = document.getElementById("message");
  var statGrid = document.getElementById("stat-grid");
  var levelList = document.getElementById("level-list");
  var rankBody = document.getElementById("rank-body");
  var pagerInfo = document.getElementById("pager-info");
  var btnPrev = document.getElementById("btn-prev");
  var btnNext = document.getElementById("btn-next");
  var tableHint = document.getElementById("table-hint");
  var resultHint = document.getElementById("result-hint");
  var distDom = document.getElementById("dist-chart");

  // ---------- 状态 ----------
  var state = {
    indicator: "pe_ttm",
    market: "",
    q: "",
    sort: "percentile",
    order: "asc",
    offset: 0,
    limit: 50
  };
  var labels = {};          // 指标 -> 中文名
  var total = 0;
  var busy = false;
  var distChart = null;
  var searchTimer = null;

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
   * 构造查询串
   *
   * @returns {string} 查询串
   */
  function buildQuery() {
    var parts = [
      "indicator=" + state.indicator,
      "sort=" + state.sort,
      "order=" + state.order,
      "offset=" + state.offset,
      "limit=" + state.limit
    ];
    if (state.market) {
      parts.push("market=" + state.market);
    }
    if (state.q) {
      parts.push("q=" + encodeURIComponent(state.q));
    }
    return parts.join("&");
  }

  /**
   * 加载排行数据
   */
  function load() {
    if (busy) {
      return;
    }
    busy = true;
    btnReload.disabled = true;
    clearMessage();

    SL.fetchJson("/api/market/ranking?" + buildQuery(), 25000)
      .then(function (data) {
        if (data.indicator_labels) {
          labels = data.indicator_labels;
          renderTabs();
        }
        if (data.message) {
          showMessage(data.message, "info");
        }
        total = data.total || 0;
        renderStats(data.summary);
        renderDistribution(data.summary);
        renderTable(data.items || []);
        renderPager();
      })
      .catch(function (error) {
        clearTable();
        showMessage(error.message || "加载失败，请稍后重试", "error");
      })
      .finally(function () {
        busy = false;
        btnReload.disabled = false;
      });
  }

  /**
   * 清空表格与统计（失败时不留旧数据）
   */
  function clearTable() {
    rankBody.textContent = "";
    tableHint.textContent = "-";
    pagerInfo.textContent = "-";
    btnPrev.disabled = true;
    btnNext.disabled = true;
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
        state.offset = 0;
        renderTabs();
        load();
      });
      indicatorTabs.appendChild(tab);
    });
  }

  /**
   * 渲染统计卡
   *
   * @param {object} summary 应答里的 summary 字段
   */
  function renderStats(summary) {
    if (!summary) {
      return;
    }

    var lowCount = 0;
    var levelCounts = summary.level_counts || {};
    ["极度低估", "低估", "正常偏低"].forEach(function (name) {
      lowCount += levelCounts[name] || 0;
    });

    var cards = [
      { k: "统计标的", v: SL.formatInt(summary.total), sub: "按当前筛选口径" },
      { k: "分位可用", v: SL.formatInt(summary.available),
        sub: summary.total ? (100 * summary.available / summary.total).toFixed(1) + "% 覆盖" : "-" },
      { k: "分位中位数", v: SL.formatPercent(summary.median_percentile),
        sub: "全市场中位水平" },
      { k: "低估（<30%）", v: SL.formatInt(lowCount),
        sub: summary.available ? (100 * lowCount / summary.available).toFixed(1) + "% 的标的" : "-" }
    ];

    statGrid.textContent = "";
    cards.forEach(function (card) {
      var box = SL.el("div", "stat-card");
      box.appendChild(SL.el("div", "stat-k", card.k));
      var value = SL.el("div", "stat-v", card.v);
      if (card.k === "分位中位数") {
        var level = SL.levelOf(summary.median_percentile);
        if (level) {
          value.style.color = level.color;
        }
      }
      box.appendChild(value);
      box.appendChild(SL.el("div", "stat-sub", card.sub));
      statGrid.appendChild(box);
    });
  }

  /**
   * 渲染分位分布柱状图与七档评级分布
   *
   * @param {object} summary 应答里的 summary 字段
   */
  function renderDistribution(summary) {
    if (!summary) {
      return;
    }

    var histogram = summary.histogram || [];
    var categories = [];
    for (var i = 0; i < histogram.length; i++) {
      categories.push((i * 10) + "-" + ((i + 1) * 10) + "%");
    }

    var colors = histogram.map(function (_, index) {
      var level = SL.levelOf(index * 10 + 5);
      return level ? level.color : "#94a3b8";
    });

    distChart = SL.renderChart(distDom, {
      animation: false,
      grid: { left: 44, right: 12, top: 18, bottom: 30 },
      xAxis: {
        type: "category",
        data: categories,
        axisLabel: { color: "#64748b", fontSize: 10.5, interval: 1 },
        axisLine: { lineStyle: { color: "#cbd5e1" } },
        axisTick: { show: false }
      },
      yAxis: {
        type: "value",
        axisLabel: { color: "#64748b", fontSize: 10.5 },
        splitLine: { lineStyle: { color: "#eef2f7" } }
      },
      tooltip: {
        trigger: "axis",
        backgroundColor: "#fff",
        borderColor: "#e2e8f0",
        textStyle: { color: "#0f172a", fontSize: 12 },
        formatter: function (params) {
          var item = params[0];
          return item.axisValue + "<br><b>" + SL.formatInt(item.value) + " 只</b>";
        }
      },
      series: [{
        type: "bar",
        data: histogram.map(function (value, index) {
          return { value: value, itemStyle: { color: colors[index], borderRadius: [3, 3, 0, 0] } };
        }),
        barWidth: "72%"
      }]
    });

    // 七档评级分布
    levelList.textContent = "";
    var counts = summary.level_counts || {};
    var maxCount = 1;
    SL.LEVEL7.forEach(function (level) {
      maxCount = Math.max(maxCount, counts[level.name] || 0);
    });
    SL.LEVEL7.forEach(function (level) {
      var count = counts[level.name] || 0;
      var row = SL.el("div", "level-row");
      row.appendChild(SL.el("span", "level-name", level.name));
      row.firstChild.style.color = level.color;

      var bar = SL.el("div", "level-bar");
      var fill = SL.el("div", "level-fill");
      fill.style.width = (100 * count / maxCount) + "%";
      fill.style.background = level.color;
      bar.appendChild(fill);
      row.appendChild(bar);

      row.appendChild(SL.el("span", "level-count", SL.formatInt(count)));
      levelList.appendChild(row);
    });
  }

  /**
   * 渲染排行表
   *
   * @param {Array<object>} items 当前页数据
   */
  function renderTable(items) {
    rankBody.textContent = "";

    if (!items.length) {
      var emptyRow = document.createElement("tr");
      var cell = document.createElement("td");
      cell.colSpan = 9;
      cell.className = "empty-state";
      cell.textContent = "没有符合条件的标的，请调整筛选或搜索词";
      emptyRow.appendChild(cell);
      rankBody.appendChild(emptyRow);
      tableHint.textContent = "共 0 条";
      return;
    }

    items.forEach(function (item, index) {
      var row = document.createElement("tr");
      row.className = "row-link";

      row.appendChild(SL.el("td", "rank", String(state.offset + index + 1)));

      var stockCell = SL.el("td");
      var line = SL.el("div", "stock-cell");
      line.appendChild(SL.el("span", "stock-name", item.name || item.ts_code));
      line.appendChild(SL.el("span", "stock-code", item.ts_code));
      stockCell.appendChild(line);
      row.appendChild(stockCell);

      row.appendChild(SL.el("td", "", item.market || "-"));

      var currentCell = SL.el("td", "ta-r num", SL.formatNumber(item.current_value));
      row.appendChild(currentCell);

      var pctCell = SL.el("td", "ta-r num", SL.formatPercent(item.percentile));
      pctCell.style.fontWeight = "650";
      var level = SL.levelOf(item.percentile);
      if (level) {
        pctCell.style.color = level.color;
      } else {
        pctCell.style.color = "var(--text-faint)";
      }
      row.appendChild(pctCell);

      var levelCell = SL.el("td", "ta-c");
      levelCell.innerHTML = SL.levelBadge(item.percentile, item.level);
      row.appendChild(levelCell);

      var tempCell = SL.el("td");
      var temp = SL.el("div", "mini-temp");
      temp.innerHTML = SL.tempbar(item.percentile);
      tempCell.appendChild(temp);
      row.appendChild(tempCell);

      row.appendChild(SL.el("td", "ta-r num", SL.formatNumber(item.median_value)));
      row.appendChild(SL.el("td", "ta-r num", SL.formatInt(item.sample_count)));

      row.addEventListener("click", function () {
        window.location.href = "/?code=" + encodeURIComponent(item.ts_code);
      });

      rankBody.appendChild(row);
    });

    tableHint.textContent = "共 " + SL.formatInt(total) + " 条，点击行进入个股分析";
  }

  /**
   * 刷新分页信息与按钮状态
   */
  function renderPager() {
    var from = total ? state.offset + 1 : 0;
    var to = Math.min(state.offset + state.limit, total);
    pagerInfo.textContent = total
      ? ("第 " + SL.formatInt(from) + " ~ " + SL.formatInt(to) + " 条 / 共 " + SL.formatInt(total) + " 条")
      : "无数据";
    btnPrev.disabled = state.offset <= 0;
    btnNext.disabled = state.offset + state.limit >= total;
  }

  /**
   * 从排序下拉解析出 sort 与 order
   */
  function applySortFromSelect() {
    var parts = sortSelect.value.split(":");
    state.sort = parts[0];
    state.order = parts[1];
  }

  // ---------- 事件 ----------
  btnReload.addEventListener("click", function () {
    state.market = marketFilter.value;
    applySortFromSelect();
    state.offset = 0;
    load();
  });

  marketFilter.addEventListener("change", function () {
    state.market = marketFilter.value;
    state.offset = 0;
    load();
  });

  sortSelect.addEventListener("change", function () {
    applySortFromSelect();
    state.offset = 0;
    load();
  });

  searchInput.addEventListener("input", function () {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(function () {
      state.q = searchInput.value.trim();
      state.offset = 0;
      load();
    }, 320);
  });

  searchInput.addEventListener("keydown", function (event) {
    if (event.key === "Enter") {
      clearTimeout(searchTimer);
      state.q = searchInput.value.trim();
      state.offset = 0;
      load();
    }
  });

  btnPrev.addEventListener("click", function () {
    state.offset = Math.max(0, state.offset - state.limit);
    load();
    window.scrollTo({ top: 0, behavior: "smooth" });
  });

  btnNext.addEventListener("click", function () {
    state.offset += state.limit;
    load();
    window.scrollTo({ top: 0, behavior: "smooth" });
  });

  window.addEventListener("resize", function () {
    if (distChart) {
      distChart.resize();
    }
  });

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

  checkHealth();
  renderTabs();
  load();
})();
