/*
 * StockLab 分析页前端逻辑 (app/web/static/app.js)
 *
 * 职责：
 *   1. 证券代码/名称联想（/api/securities）
 *   2. 触发分位分析（/api/percentile）并渲染卡片、明细表
 *   3. ECharts 绘制区间内历史估值走势 + 当前值横线
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
  var btnAnalyze = document.getElementById("btn-analyze");
  var messageBox = document.getElementById("message");
  var resultArea = document.getElementById("result-area");
  var cardsBox = document.getElementById("cards");
  var detailBody = document.getElementById("detail-body");
  var metaInterval = document.getElementById("meta-interval");
  var metaRows = document.getElementById("meta-rows");
  var metaPriority = document.getElementById("meta-priority");

  var chart = null;          // ECharts 实例
  var suggestTimer = null;   // 联想防抖计时器
  var suggestItems = [];     // 当前联想结果
  var activeIndex = -1;      // 键盘选中的联想项

  var LEVEL_CLASS = {
    "相对低位": "level-low",
    "中性": "level-mid",
    "相对高位": "level-high"
  };

  var PRIORITY_TEXT = {
    "local_first": "本地库优先",
    "remote_first": "远端接口优先"
  };

  // ---------- 工具函数 ----------

  function showMessage(text, kind) {
    messageBox.textContent = text;
    messageBox.className = "message " + (kind || "info");
  }

  function clearMessage() {
    messageBox.textContent = "";
    messageBox.className = "message";
  }

  function formatNumber(value) {
    if (value === null || value === undefined) {
      return "-";
    }
    if (Math.abs(value) >= 1000) {
      return value.toFixed(0);
    }
    return value.toFixed(2);
  }

  function formatPercent(value) {
    if (value === null || value === undefined) {
      return "-";
    }
    return value.toFixed(1) + "%";
  }

  // ---------- 证券联想 ----------

  function hideSuggest() {
    suggestList.className = "suggest-list";
    suggestList.innerHTML = "";
    suggestItems = [];
    activeIndex = -1;
  }

  function renderSuggest(items) {
    suggestItems = items;
    activeIndex = -1;
    if (!items.length) {
      suggestList.innerHTML = '<div class="suggest-empty">无匹配标的</div>';
      suggestList.className = "suggest-list open";
      return;
    }
    var html = "";
    for (var i = 0; i < items.length; i++) {
      html += '<div class="suggest-item" data-index="' + i + '">' +
              '<span class="code">' + items[i].code + "</span>" +
              '<span class="name">' + (items[i].name || "") + "</span></div>";
    }
    suggestList.innerHTML = html;
    suggestList.className = "suggest-list open";
  }

  function fetchSuggest(query) {
    fetch("/api/securities?q=" + encodeURIComponent(query))
      .then(function (resp) { return resp.json(); })
      .then(function (data) {
        // 用户可能已继续输入，只采纳与当前输入一致的结果
        if (query !== codeInput.value.trim()) {
          return;
        }
        renderSuggest(data.items || []);
      })
      .catch(function () {
        hideSuggest();
      });
  }

  function onSuggestInput() {
    var query = codeInput.value.trim();
    if (suggestTimer) {
      clearTimeout(suggestTimer);
    }
    if (!query) {
      hideSuggest();
      return;
    }
    // 防抖 250ms，避免每个按键都发请求
    suggestTimer = setTimeout(function () {
      fetchSuggest(query);
    }, 250);
  }

  function applySuggestItem(index) {
    var item = suggestItems[index];
    if (!item) {
      return;
    }
    codeInput.value = item.code;
    hideSuggest();
    codeInput.focus();
  }

  function onSuggestKeydown(event) {
    if (!suggestItems.length) {
      return;
    }
    if (event.key === "ArrowDown") {
      activeIndex = (activeIndex + 1) % suggestItems.length;
    } else if (event.key === "ArrowUp") {
      activeIndex = (activeIndex - 1 + suggestItems.length) % suggestItems.length;
    } else if (event.key === "Enter" && activeIndex >= 0) {
      event.preventDefault();
      applySuggestItem(activeIndex);
      return;
    } else if (event.key === "Escape") {
      hideSuggest();
      return;
    } else {
      return;
    }
    event.preventDefault();
    var nodes = suggestList.querySelectorAll(".suggest-item");
    for (var i = 0; i < nodes.length; i++) {
      nodes[i].className = "suggest-item" + (i === activeIndex ? " active" : "");
    }
  }

  // ---------- 分析主流程 ----------

  function buildQueryUrl(code) {
    var params = ["code=" + encodeURIComponent(code)];
    params.push("period=" + encodeURIComponent(periodSelect.value));
    if (startDateInput.value) {
      params.push("start_date=" + encodeURIComponent(startDateInput.value));
    }
    if (endDateInput.value) {
      params.push("end_date=" + encodeURIComponent(endDateInput.value));
    }
    return "/api/percentile?" + params.join("&");
  }

  function analyze() {
    var code = codeInput.value.trim();
    if (!code) {
      showMessage("请先输入证券代码或名称", "error");
      codeInput.focus();
      return;
    }

    hideSuggest();
    clearMessage();
    btnAnalyze.disabled = true;
    btnAnalyze.textContent = "分析中...";

    fetch(buildQueryUrl(code))
      .then(function (resp) {
        return resp.json().then(function (data) {
          return { ok: resp.ok, data: data };
        });
      })
      .then(function (result) {
        if (!result.ok) {
          showMessage(result.data.error || "请求失败", "error");
          return;
        }
        renderResult(result.data);
      })
      .catch(function (error) {
        showMessage("请求失败：" + error.message, "error");
      })
      .finally(function () {
        btnAnalyze.disabled = false;
        btnAnalyze.textContent = "开始分析";
      });
  }

  function renderResult(data) {
    if (!data.results || !data.results.length) {
      resultArea.className = "hidden";
      showMessage(data.message || "没有可用的分析结果", "error");
      return;
    }

    if (data.message) {
      showMessage(data.message, "info");
    }

    metaInterval.textContent = data.interval_text || "-";
    metaRows.textContent = "样本 " + data.sample_rows + " 个交易日";
    metaPriority.textContent = PRIORITY_TEXT[data.priority] || data.priority;

    renderCards(data.results);
    renderTable(data.results);
    renderChart(data.history, data.results);

    resultArea.className = "";
  }

  function renderCards(results) {
    var html = "";
    for (var i = 0; i < results.length; i++) {
      var item = results[i];
      var available = item.percentile !== null;
      var levelClass = available ? (LEVEL_CLASS[item.level] || "level-none") : "level-none";
      var levelText = available ? item.level : "不可用";

      html += '<div class="card">' +
              '<div class="ind">' + item.label + "</div>" +
              '<div class="pct' + (available ? "" : " none") + '">' +
                (available ? formatPercent(item.percentile) : "无有效样本") +
              "</div>" +
              '<div class="detail">当前 ' + formatNumber(item.current_value) +
                " / 中位 " + formatNumber(item.median_value) + "</div>" +
              '<span class="level ' + levelClass + '">' + levelText + "</span>" +
              "</div>";
    }
    cardsBox.innerHTML = html;
  }

  function renderTable(results) {
    var html = "";
    for (var i = 0; i < results.length; i++) {
      var item = results[i];
      var levelClass = item.percentile !== null ? (LEVEL_CLASS[item.level] || "level-none") : "level-none";
      html += "<tr>" +
              "<td>" + item.label + "</td>" +
              '<td class="mono">' + formatNumber(item.current_value) + "</td>" +
              '<td class="mono">' + formatPercent(item.percentile) + "</td>" +
              '<td class="mono">' + item.sample_count + "</td>" +
              '<td class="mono">' + formatNumber(item.median_value) + "</td>" +
              '<td class="mono">' + formatNumber(item.min_value) + "</td>" +
              '<td class="mono">' + formatNumber(item.max_value) + "</td>" +
              '<td><span class="level ' + levelClass + '">' +
                (item.percentile !== null ? item.level : "-") + "</span></td>" +
              "</tr>";
    }
    detailBody.innerHTML = html;
  }

  // ---------- 走势图 ----------

  function renderChart(history, results) {
    if (!history || !history.dates || !history.dates.length) {
      return;
    }

    if (!chart) {
      chart = echarts.init(document.getElementById("chart"));
      window.addEventListener("resize", function () {
        if (chart) {
          chart.resize();
        }
      });
    }

    // 只画有数据的指标；每个指标配一条当前值横线（markLine）
    var series = [];
    var legends = [];
    for (var i = 0; i < results.length; i++) {
      var item = results[i];
      var values = history.series[item.indicator];
      if (!values || item.percentile === null) {
        continue;
      }
      legends.push(item.label);
      series.push({
        name: item.label,
        type: "line",
        showSymbol: false,
        connectNulls: false,
        data: values,
        lineStyle: { width: 1.6 },
        markLine: {
          silent: true,
          symbol: "none",
          label: {
            formatter: "当前 " + formatNumber(item.current_value),
            position: "insideEndTop",
            fontSize: 11
          },
          data: [{ yAxis: item.current_value }]
        }
      });
    }

    chart.setOption({
      animation: false,
      color: ["#2563eb", "#16a34a", "#dc2626", "#7c3aed", "#ea580c"],
      tooltip: {
        trigger: "axis",
        valueFormatter: function (value) {
          return value === null ? "-" : formatNumber(value);
        }
      },
      legend: {
        data: legends,
        top: 0,
        textStyle: { color: "#475569", fontSize: 12 }
      },
      grid: { left: 50, right: 20, top: 34, bottom: 40 },
      xAxis: {
        type: "category",
        data: history.dates,
        axisLabel: { color: "#64748b", fontSize: 11 },
        axisLine: { lineStyle: { color: "#cbd5e1" } }
      },
      yAxis: {
        type: "value",
        scale: true,
        axisLabel: { color: "#64748b", fontSize: 11 },
        splitLine: { lineStyle: { color: "#e2e8f0" } }
      },
      dataZoom: [
        { type: "inside" },
        { type: "slider", height: 16, bottom: 6 }
      ],
      series: series
    }, true);
  }

  // ---------- 事件绑定 ----------

  codeInput.addEventListener("input", onSuggestInput);
  codeInput.addEventListener("keydown", onSuggestKeydown);

  suggestList.addEventListener("mousedown", function (event) {
    var node = event.target;
    while (node && node !== suggestList && !node.getAttribute("data-index")) {
      node = node.parentNode;
    }
    if (node && node.getAttribute("data-index")) {
      applySuggestItem(parseInt(node.getAttribute("data-index"), 10));
    }
  });

  // 点击页面其他区域时收起联想
  document.addEventListener("click", function (event) {
    if (event.target !== codeInput) {
      hideSuggest();
    }
  });

  btnAnalyze.addEventListener("click", analyze);

  // 回车直接分析（联想关闭时）
  codeInput.addEventListener("keydown", function (event) {
    if (event.key === "Enter" && suggestList.className.indexOf("open") === -1) {
      analyze();
    }
  });
})();
