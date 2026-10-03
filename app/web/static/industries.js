/*
 * StockLab 行业估值页前端逻辑 (app/web/static/industries.js)
 *
 * 职责：
 *   1. 加载行业估值横截面（/api/industries），渲染统计卡 / PE 条形图 / 明细表
 *   2. 行业层级切换（国证 1~4 级）
 *   3. 数据缺失时给出同步提示而不是空白页
 *
 * 说明：本文件为浏览器端脚本，与 Python 侧无共享约束，保持原生 JS 直白写法。
 */

(function () {
  "use strict";

  // ---------- DOM ----------
  var levelTabs = document.getElementById("level-tabs");
  var btnReload = document.getElementById("btn-reload");
  var messageBox = document.getElementById("message");
  var statGrid = document.getElementById("stat-grid");
  var chartDom = document.getElementById("industry-chart");
  var tableBody = document.getElementById("industry-body");

  // ---------- 状态 ----------
  var state = {
    level: 1
  };
  var chart = null;
  var busy = false;

  var LEVEL_NAMES = {
    1: "一级行业",
    2: "二级行业",
    3: "三级行业",
    4: "细分行业"
  };

  // ---------- 工具 ----------

  /**
   * 显示消息条
   *
   * @param {string} text 文本
   * @param {string} kind info | error
   */
  function showMessage(text, kind) {
    messageBox.textContent = text || "";
    messageBox.className = "message " + (kind || "info");
  }

  /**
   * 清空消息条
   */
  function clearMessage() {
    messageBox.textContent = "";
    messageBox.className = "message";
  }

  /**
   * 格式化亿元（保留 1 位小数）
   *
   * @param {number|null} value 亿元数值
   * @returns {string} 文本
   */
  function formatYi(value) {
    if (value === null || value === undefined) {
      return "-";
    }
    return value.toFixed(1);
  }

  // ---------- 渲染 ----------

  /**
   * 渲染层级切换标签
   */
  function renderLevelTabs() {
    levelTabs.textContent = "";
    [1, 2, 3, 4].forEach(function (level) {
      var tab = document.createElement("button");
      tab.type = "button";
      tab.className = "level-tab" + (state.level === level ? " active" : "");
      tab.textContent = LEVEL_NAMES[level];
      tab.addEventListener("click", function () {
        if (state.level === level) {
          return;
        }
        state.level = level;
        renderLevelTabs();
        load();
      });
      levelTabs.appendChild(tab);
    });
  }

  /**
   * 渲染统计卡
   *
   * @param {object} summary 汇总数据
   */
  function renderStats(summary) {
    var cards = statGrid.querySelectorAll(".stat-card .stat-v");
    var values = [
      SL.formatInt(summary.industry_count),
      SL.formatInt(summary.priced_count),
      summary.median_pe === null ? "-" : summary.median_pe.toFixed(2),
      SL.formatInt(summary.company_count)
    ];
    cards.forEach(function (cell, index) {
      cell.textContent = values[index] !== undefined ? values[index] : "-";
    });
  }

  /**
   * 渲染 PE 中位数条形图
   *
   * @param {Array} items 行业数据（已按 PE 中位数升序）
   */
  function renderChart(items) {
    var names = [];
    var values = [];
    items.forEach(function (item) {
      names.push(item.industry_name);
      values.push(item.pe_median === null ? null : item.pe_median);
    });

    var option = {
      grid: { left: 8, right: 40, top: 10, bottom: 8, containLabel: true },
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "shadow" },
        formatter: function (params) {
          var param = params[0];
          var item = items[param.dataIndex];
          if (!item) {
            return "";
          }
          var lines = [item.industry_name];
          lines.push("PE 中位：" + (item.pe_median === null ? "无" : item.pe_median.toFixed(2)));
          lines.push("PE 加权：" + (item.pe_weighted === null ? "无" : item.pe_weighted.toFixed(2)));
          lines.push("公司数：" + SL.formatInt(item.company_count));
          return lines.join("<br/>");
        }
      },
      xAxis: {
        type: "value",
        name: "PE",
        nameTextStyle: { color: "#64748b", fontSize: 11 },
        axisLabel: { color: "#64748b" },
        splitLine: { lineStyle: { color: "#eef2f7" } }
      },
      yAxis: {
        type: "category",
        data: names,
        axisLabel: { color: "#334155", fontSize: 11 },
        axisLine: { lineStyle: { color: "#e2e8f0" } }
      },
      series: [{
        type: "bar",
        data: values.map(function (value) {
          return {
            value: value,
            itemStyle: {
              color: value === null ? "#cbd5e1" : "#2563eb",
              borderRadius: [0, 4, 4, 0]
            }
          };
        }),
        barMaxWidth: 16,
        label: {
          show: true,
          position: "right",
          color: "#64748b",
          fontSize: 10.5,
          formatter: function (param) {
            return param.value === null ? "-" : param.value.toFixed(1);
          }
        }
      }]
    };

    if (!chart) {
      chart = echarts.init(chartDom);
    }
    chart.setOption(option, true);
  }

  /**
   * 渲染行业明细表
   *
   * @param {Array} items 行业数据
   */
  function renderTable(items) {
    tableBody.textContent = "";
    items.forEach(function (item) {
      var row = document.createElement("tr");

      var nameCell = document.createElement("td");
      nameCell.textContent = item.industry_name;
      row.appendChild(nameCell);

      [
        SL.formatInt(item.company_count),
        SL.formatInt(item.priced_company_count),
        item.pe_weighted === null ? "-" : item.pe_weighted.toFixed(2),
        item.pe_median === null ? "-" : item.pe_median.toFixed(2),
        item.pe_arithmetic === null ? "-" : item.pe_arithmetic.toFixed(2),
        formatYi(item.total_market_value),
        formatYi(item.net_profit)
      ].forEach(function (text) {
        var cell = document.createElement("td");
        cell.className = "num";
        cell.textContent = text;
        row.appendChild(cell);
      });

      tableBody.appendChild(row);
    });
  }

  /**
   * 清空图表与表格（失败时不留旧数据）
   */
  function clearAll() {
    tableBody.textContent = "";
    if (chart) {
      chart.clear();
    }
  }

  // ---------- 加载 ----------

  /**
   * 加载行业估值数据
   */
  function load() {
    if (busy) {
      return;
    }
    busy = true;
    btnReload.disabled = true;
    clearMessage();

    SL.fetchJson("/api/industries?level=" + state.level, 15000)
      .then(function (data) {
        if (data.message) {
          showMessage(data.message, "info");
        }
        renderStats(data.summary || {});
        renderChart(data.items || []);
        renderTable(data.items || []);
      })
      .catch(function (error) {
        clearAll();
        showMessage(error.message || "加载失败，请稍后重试", "error");
      })
      .finally(function () {
        busy = false;
        btnReload.disabled = false;
      });
  }

  // ---------- 事件 ----------
  btnReload.addEventListener("click", load);

  window.addEventListener("resize", function () {
    if (chart) {
      chart.resize();
    }
  });

  // ---------- 启动 ----------
  renderLevelTabs();
  load();
  SL.checkHealth();
})();
