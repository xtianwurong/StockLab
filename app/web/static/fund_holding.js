/*
 * StockLab 基金持仓与行业暴露页前端逻辑 (app/web/static/fund_holding.js)
 *
 * 职责：
 *   1. 基金代码/名称联想搜索
 *   2. 读取定期报告披露的重仓股，汇总真实行业暴露
 *   3. 暴露度时序图（报告期间线性插值）
 *   4. RBSA 反推暴露 vs 持仓实测暴露 双轨对比
 *   5. URL 状态同步（刷新/分享可还原）
 *
 * 与 fund_analysis.js 同款约定：日期一律按 ISO 串渲染（后端已在应答边界
 * 转好），缺失值一律显示 "-"，绝不用 0 冒充。
 */

(function () {
  "use strict";

  var codeInput = document.getElementById("fh-code");
  if (!codeInput) {
    // 非本页（脚本可能因缓存被加载到别的页面）—— 直接退出
    return;
  }

  // ---------- DOM ----------
  var suggestList = document.getElementById("fh-suggest");
  var nameDisplay = document.getElementById("fh-name");
  var dateInput = document.getElementById("fh-date");
  var levelInput = document.getElementById("fh-level");
  var startInput = document.getElementById("fh-start");
  var endInput = document.getElementById("fh-end");
  var btnQuery = document.getElementById("fh-query");
  var btnRefresh = document.getElementById("fh-refresh");
  var messageBox = document.getElementById("fh-message");

  var overview = document.getElementById("fh-overview");
  var holdingsSection = document.getElementById("fh-holdings-section");
  var exposureSection = document.getElementById("fh-exposure-section");
  var chartSection = document.getElementById("fh-chart-section");
  var rbsaSection = document.getElementById("fh-rbsa-section");
  var emptyState = document.getElementById("fh-empty");
  var loadingState = document.getElementById("fh-loading");

  var ovName = document.getElementById("fh-ov-name");
  var ovCode = document.getElementById("fh-ov-code");
  var ovReport = document.getElementById("fh-ov-report");
  var ovReportType = document.getElementById("fh-ov-report-type");
  var ovCount = document.getElementById("fh-ov-count");
  var ovTop10 = document.getElementById("fh-ov-top10");
  var ovTopSector = document.getElementById("fh-ov-top-sector");
  var ovTopSectorWeight = document.getElementById("fh-ov-top-sector-weight");

  var holdingsBody = document.getElementById("fh-holdings-body");
  var exposureList = document.getElementById("fh-exposure-list");
  var rbsaSummary = document.getElementById("fh-rbsa-summary");
  var rbsaBody = document.getElementById("fh-rbsa-body");
  var chartDom = document.getElementById("fh-exposure-chart");
  var chartTabs = document.querySelectorAll("#fh-chart-tabs .fa-chart-tab");

  // ---------- 状态 ----------
  var state = {
    code: "",
    name: "",
    reportDate: "",
    level: 1,
    start: "",
    end: "",
    daily: null,
    chart: null,
    tab: "area",
    sectorNames: {},
  };

  var suggestTimer = null;
  var busy = false;

  // 报告类型 -> 中文（后端给的是 "quarterly" 这类英文键）
  var REPORT_TYPE_LABELS = {
    quarterly: "季报",
    semiannual: "半年报",
    semi_annual: "半年报",
    annual: "年报",
    yearly: "年报",
  };

  // ---------- 工具函数 ----------
  function showMessage(text, kind) {
    SL.showMessage(messageBox, text, kind);
  }

  function clearMessage() {
    SL.clearMessage(messageBox);
  }

  /**
   * Date -> YYYY-MM-DD（字符串原样返回）
   *
   * @param {Date|string} value 日期
   * @returns {string} ISO 日期串
   */
  function isoDate(value) {
    if (!value) {
      return "";
    }
    if (typeof value === "string") {
      return value;
    }
    return value.getFullYear() + "-" +
      String(value.getMonth() + 1).padStart(2, "0") + "-" +
      String(value.getDate()).padStart(2, "0");
  }

  function formatDate(value) {
    return value ? value : "-";
  }

  function formatPercent(value, decimals) {
    if (value === null || value === undefined || isNaN(value)) {
      return "-";
    }
    var d = decimals !== undefined ? decimals : 2;
    return (value * 100).toFixed(d) + "%";
  }

  function formatNumber(value, decimals) {
    if (value === null || value === undefined || isNaN(value)) {
      return "-";
    }
    return SL.formatNumber(value, decimals);
  }

  function reportTypeLabel(value) {
    if (!value) {
      return "-";
    }
    return REPORT_TYPE_LABELS[String(value).toLowerCase()] || value;
  }

  /**
   * 按当前图表 tab 调整 series 形态（面积 / 折线 / 堆叠）
   *
   * @param {object} option ECharts option
   * @returns {object} 调整后的 option
   */
  function applyChartTab(option) {
    if (!option || !option.series) {
      return option;
    }
    var tab = state.tab;
    option.series.forEach(function (s) {
      if (tab === "stacked") {
        s.stack = "total";
        s.areaStyle = { opacity: 0.85 };
        s.lineStyle = { width: 1 };
      } else if (tab === "line") {
        delete s.stack;
        delete s.areaStyle;
        s.lineStyle = { width: 1.8 };
      } else {
        delete s.stack;
        s.areaStyle = { opacity: 0.18 };
        s.lineStyle = { width: 1.6 };
      }
    });
    return option;
  }

  function setLoading(isLoading) {
    busy = isLoading;
    btnQuery.disabled = isLoading;
    btnRefresh.disabled = isLoading;
    btnQuery.setAttribute("aria-busy", isLoading ? "true" : "false");
    btnRefresh.setAttribute("aria-busy", isLoading ? "true" : "false");
    loadingState.classList.toggle("hidden", !isLoading);
    if (isLoading) {
      emptyState.classList.add("hidden");
    }
  }

  function showSections(show) {
    overview.style.display = show ? "grid" : "none";
    holdingsSection.style.display = show ? "block" : "none";
    exposureSection.style.display = show ? "block" : "none";
    chartSection.style.display = show ? "block" : "none";
    rbsaSection.style.display = show ? "block" : "none";
    emptyState.style.display = show ? "none" : "block";
  }

  // ---------- 基金联想 ----------
  function fetchFundSuggest(query) {
    if (!query || query.length < 2) {
      suggestList.innerHTML = "";
      suggestList.style.display = "none";
      return;
    }
    clearTimeout(suggestTimer);
    suggestTimer = setTimeout(function () {
      SL.fetchJson("/api/fund/list?q=" + encodeURIComponent(query), 8000)
        .then(function (data) {
          var items = data.items || [];
          suggestList.innerHTML = "";
          if (items.length === 0) {
            suggestList.style.display = "none";
            return;
          }
          items.slice(0, 15).forEach(function (item) {
            var div = document.createElement("div");
            div.className = "suggest-item";
            div.textContent = item.fund_code + " " + item.fund_name;
            div.dataset.code = item.fund_code;
            div.dataset.name = item.fund_name;
            div.addEventListener("click", function () {
              selectFund(this.dataset.code, this.dataset.name);
            });
            suggestList.appendChild(div);
          });
          suggestList.style.display = "block";
        })
        .catch(function () {
          suggestList.style.display = "none";
        });
    }, 200);
  }

  function selectFund(code, name) {
    state.code = code;
    state.name = name || "";
    codeInput.value = code;
    nameDisplay.value = state.name;
    suggestList.style.display = "none";
    clearMessage();
  }

  function validateFundCode(code) {
    nameDisplay.value = "查询中...";
    SL.fetchJson("/api/fund/info?code=" + encodeURIComponent(code), 8000)
      .then(function (data) {
        state.name = data.fund_name || "";
        nameDisplay.value = state.name || "（无名称）";
        clearMessage();
      })
      .catch(function (err) {
        if (err && err.status === 404) {
          state.name = "";
          nameDisplay.value = "未找到";
          showMessage(err.message || ("未找到该基金: " + code), "error");
        } else {
          nameDisplay.value = "查询失败";
          showMessage(err.message || "基金信息查询失败", "error");
        }
      });
  }

  codeInput.addEventListener("input", function () {
    fetchFundSuggest(this.value.trim());
    if (!this.value.includes(".")) {
      nameDisplay.value = "";
      state.name = "";
    }
  });

  codeInput.addEventListener("keydown", function (e) {
    if (e.key !== "Enter") {
      return;
    }
    e.preventDefault();
    var code = this.value.trim().toUpperCase();
    if (!code) {
      return;
    }
    var items = suggestList.querySelectorAll(".suggest-item");
    var matched = Array.prototype.find.call(items, function (el) {
      return el.dataset.code === code || el.dataset.name === code;
    });
    if (matched) {
      selectFund(matched.dataset.code, matched.dataset.name);
      query();
    } else if (/^\d{6}\.OF$/.test(code)) {
      state.code = code;
      validateFundCode(code);
    } else {
      showMessage("基金代码格式应为 000001.OF", "error");
    }
  });

  codeInput.addEventListener("blur", function () {
    setTimeout(function () {
      suggestList.style.display = "none";
    }, 200);
  });

  // ---------- 数据加载 ----------
  function loadHoldings(code) {
    var url = "/api/fund/holding/latest?code=" + encodeURIComponent(code);
    return SL.fetchJson(url, 30000).then(function (data) {
      renderOverview(data);
      renderHoldingsTable(data.items || []);
    });
  }

  function loadExposure(code, level) {
    var params = new URLSearchParams({ code: code, level: level });
    if (state.reportDate) {
      params.set("date", state.reportDate);
    }
    return SL.fetchJson("/api/fund/exposure?" + params.toString(), 30000)
      .then(function (data) {
        state.sectorNames = {};
        (data.items || []).forEach(function (item) {
          if (item.sector_code) {
            state.sectorNames[item.sector_code] = item.sector_name;
          }
        });
        renderExposure(data.items || []);
        if (data.report_date) {
          ovReport.textContent = formatDate(data.report_date);
        }
      });
  }

  function loadDaily(code, level) {
    var params = new URLSearchParams({ code: code, level: level });
    if (state.start) {
      params.set("start", state.start);
    }
    if (state.end) {
      params.set("end", state.end);
    }
    return SL.fetchJson("/api/fund/exposure/daily?" + params.toString(), 60000)
      .then(function (data) {
        state.daily = data.items || [];
        renderExposureChart();
      });
  }

  function loadRbsa(code, level) {
    var params = new URLSearchParams({ code: code, level: level });
    if (state.reportDate) {
      params.set("date", state.reportDate);
    }
    return SL.fetchJson("/api/fund/holding/vs_rbsa?" + params.toString(), 60000)
      .then(function (data) {
        renderRbsa(data);
      });
  }

  // ---------- 渲染 ----------
  function renderOverview(data) {
    ovName.textContent = state.name || data.code || "-";
    ovCode.textContent = data.code || "-";
    ovReport.textContent = formatDate(data.report_date);
    ovReportType.textContent = reportTypeLabel(data.report_type);

    var items = data.items || [];
    ovCount.textContent = String(items.length);

    var weightSum = 0;
    items.forEach(function (item) {
      if (typeof item.weight === "number" && !isNaN(item.weight)) {
        weightSum += item.weight;
      }
    });
    ovTop10.textContent = formatPercent(weightSum, 2);
  }

  function renderHoldingsTable(items) {
    holdingsBody.innerHTML = "";
    if (!items.length) {
      var tr = document.createElement("tr");
      var td = document.createElement("td");
      td.colSpan = 5;
      td.style.textAlign = "center";
      td.style.padding = "var(--rhythm-4)";
      td.style.color = "var(--text-muted)";
      td.textContent = "无重仓股数据";
      tr.appendChild(td);
      holdingsBody.appendChild(tr);
      return;
    }

    var sorted = items.slice().sort(function (a, b) {
      return (a.rank || 0) - (b.rank || 0);
    });

    sorted.forEach(function (item) {
      var tr = document.createElement("tr");

      var tdRank = document.createElement("td");
      tdRank.className = "num";
      tdRank.textContent = item.rank === null || item.rank === undefined ? "-" : String(item.rank);
      tr.appendChild(tdRank);

      var tdCode = document.createElement("td");
      tdCode.textContent = item.stock_code || "-";
      tr.appendChild(tdCode);

      var tdName = document.createElement("td");
      tdName.textContent = item.stock_name || "-";
      tr.appendChild(tdName);

      var tdWeight = document.createElement("td");
      tdWeight.className = "num";
      tdWeight.textContent = formatPercent(item.weight, 2);
      tr.appendChild(tdWeight);

      var tdValue = document.createElement("td");
      tdValue.className = "num";
      tdValue.textContent = item.market_value === null || item.market_value === undefined ?
        "-" : formatNumber(item.market_value / 10000, 0);
      tr.appendChild(tdValue);

      holdingsBody.appendChild(tr);
    });
  }

  function renderExposure(items) {
    exposureList.innerHTML = "";
    if (!items.length) {
      exposureList.innerHTML = '<div style="grid-column:1/-1;text-align:center;color:var(--text-muted);padding:var(--rhythm-4)">无行业暴露数据</div>';
      ovTopSector.textContent = "-";
      ovTopSectorWeight.textContent = "-";
      return;
    }

    var sorted = items.slice()
      .filter(function (item) { return typeof item.weight === "number" && item.weight > 0; })
      .sort(function (a, b) { return b.weight - a.weight; });

    ovTopSector.textContent = sorted.length ? sorted[0].sector_name : "-";
    ovTopSectorWeight.textContent = sorted.length ? formatPercent(sorted[0].weight, 2) : "-";

    sorted.slice(0, 20).forEach(function (item) {
      var card = document.createElement("div");
      card.className = "fa-holding-card";

      var name = document.createElement("div");
      name.className = "fa-holding-name";
      name.textContent = item.sector_name || item.sector_code || "-";
      card.appendChild(name);

      var exp = document.createElement("div");
      exp.className = "fa-holding-exposure";
      exp.textContent = formatPercent(item.weight, 2);
      card.appendChild(exp);

      exposureList.appendChild(card);
    });
  }

  function renderExposureChart() {
    var items = state.daily || [];
    if (!items.length) {
      return;
    }

    var dates = [];
    var seenDates = {};
    var bySector = {};

    items.forEach(function (item) {
      var day = item.trade_date;
      if (day && !seenDates[day]) {
        seenDates[day] = true;
        dates.push(day);
      }
      if (!day) {
        return;
      }
      var key = item.sector_name || item.sector_code;
      if (!key) {
        return;
      }
      if (!bySector[key]) {
        bySector[key] = {};
      }
      bySector[key][day] = item.weight || 0;
    });

    dates.sort();

    var names = Object.keys(bySector);
    if (!names.length || !dates.length) {
      return;
    }

    // 取峰值暴露度最高的前 12 个行业，其余并成「其他」
    names.sort(function (a, b) {
      return peak(b) - peak(a);
    });

    function peak(name) {
      var values = bySector[name];
      var max = 0;
      Object.keys(values).forEach(function (day) {
        if (values[day] > max) {
          max = values[day];
        }
      });
      return max;
    }

    var topNames = names.slice(0, 12);
    var restNames = names.slice(12);
    var series = {};

    topNames.forEach(function (name) {
      series[name] = dates.map(function (day) {
        return bySector[name][day] || 0;
      });
    });
    if (restNames.length) {
      series["其他"] = dates.map(function (day) {
        var sum = 0;
        restNames.forEach(function (name) {
          sum += bySector[name][day] || 0;
        });
        return sum;
      });
    }

    var historyData = { dates: dates, series: series };
    var rebuild = function () {
      return applyChartTab(SL.buildTrendOption({
        history: historyData,
        indicatorMeta: {},
        primary: Object.keys(series)[0] || "",
        singleAxis: true,
      }));
    };

    if (state.chart) {
      state.chart.dispose();
    }
    state.chart = SL.renderChart(chartDom, rebuild(), rebuild);
  }

  function renderRbsa(data) {
    var correlation = data.correlation;
    var rSquared = data.r_squared;
    var coverage = data.coverage;

    rbsaSummary.innerHTML = "";
    appendSummary("相关性", correlation === null || correlation === undefined ? "-" : correlation.toFixed(2));
    appendSummary("拟合优度 R²", rSquared === null || rSquared === undefined ? "-" : rSquared.toFixed(4));
    appendSummary("映射覆盖率", coverage === null || coverage === undefined ? "-" : formatPercent(coverage, 1));
    appendSummary("持仓报告期", formatDate(data.report_date));

    function appendSummary(label, value) {
      var span = document.createElement("span");
      span.textContent = label + " ";
      var b = document.createElement("b");
      b.textContent = value;
      span.appendChild(b);
      rbsaSummary.appendChild(span);
    }

    rbsaBody.innerHTML = "";
    var diffs = data.top_diffs || [];
    if (!diffs.length) {
      var tr = document.createElement("tr");
      var td = document.createElement("td");
      td.colSpan = 4;
      td.style.textAlign = "center";
      td.style.padding = "var(--rhythm-4)";
      td.style.color = "var(--text-muted)";
      td.textContent = "无可对比行业（RBSA 或持仓映射样本不足）";
      tr.appendChild(td);
      rbsaBody.appendChild(tr);
      return;
    }

    var holding = data.holding_exposures || {};
    var rbsa = data.rbsa_exposures || {};

    diffs.forEach(function (entry) {
      var sectorCode = entry.sector_code;
      var holdingValue = holding[sectorCode];
      var rbsaValue = rbsa[sectorCode];
      var diff = entry.diff;

      var tr = document.createElement("tr");

      var tdName = document.createElement("td");
      tdName.textContent = state.sectorNames[sectorCode] || sectorCode || "-";
      tr.appendChild(tdName);

      var tdHolding = document.createElement("td");
      tdHolding.className = "num";
      tdHolding.textContent = formatPercent(holdingValue, 2);
      tr.appendChild(tdHolding);

      var tdRbsa = document.createElement("td");
      tdRbsa.className = "num";
      tdRbsa.textContent = formatPercent(rbsaValue, 2);
      tr.appendChild(tdRbsa);

      var tdDiff = document.createElement("td");
      tdDiff.className = "num " + (diff > 0 ? "positive" : (diff < 0 ? "negative" : ""));
      tdDiff.textContent = (diff > 0 ? "+" : "") + formatPercent(diff, 2);
      tr.appendChild(tdDiff);

      rbsaBody.appendChild(tr);
    });
  }

  // ---------- 查询 ----------
  function query() {
    var code = codeInput.value.trim().toUpperCase();
    if (!code) {
      showMessage("请输入基金代码", "error");
      return;
    }
    if (!/^\d{6}\.OF$/.test(code)) {
      showMessage("基金代码格式应为 000001.OF", "error");
      return;
    }
    var level = parseInt(levelInput.value, 10);
    if (level !== 1 && level !== 2) {
      showMessage("行业层级必须是 1 或 2", "error");
      return;
    }
    if (startInput.value && endInput.value && startInput.value > endInput.value) {
      showMessage("时序起点不能晚于终点", "error");
      return;
    }

    state.code = code;
    state.level = level;
    state.reportDate = dateInput.value || "";
    state.start = startInput.value || "";
    state.end = endInput.value || "";

    setLoading(true);
    clearMessage();
    updateUrl();

    loadHoldings(code)
      .then(function () {
        // 行业名称先落地，后续 RBSA 差异表才能显示中文行业名
        return loadExposure(code, level).catch(function () { return null; });
      })
      .then(function () {
        return Promise.all([
          loadDaily(code, level).catch(function () { return null; }),
          loadRbsa(code, level).catch(function () { return null; }),
        ]);
      })
      .then(function () {
        showSections(true);
        showMessage("查询完成", "success");
      })
      .catch(function (err) {
        showSections(false);
        if (err && err.status === 404) {
          showMessage(err.message || ("未找到基金 " + code + " 的持仓数据"), "error");
        } else {
          showMessage(err.message || "查询失败，请稍后重试", "error");
        }
      })
      .finally(function () {
        setLoading(false);
      });
  }

  // ---------- 事件绑定 ----------
  btnQuery.addEventListener("click", function () {
    if (!busy) {
      query();
    }
  });

  btnRefresh.addEventListener("click", function () {
    if (!busy) {
      query();
    }
  });

  chartTabs.forEach(function (tab) {
    tab.addEventListener("click", function () {
      chartTabs.forEach(function (t) { t.classList.remove("active"); });
      this.classList.add("active");
      state.tab = this.dataset.tab;
      if (state.daily && state.chart) {
        renderExposureChart();
      }
    });
  });

  // ---------- URL 状态同步 ----------
  function updateUrl() {
    var params = new URLSearchParams();
    if (state.code) {
      params.set("code", state.code);
    }
    if (state.reportDate) {
      params.set("date", state.reportDate);
    }
    if (state.level !== 1) {
      params.set("level", state.level);
    }
    if (state.start) {
      params.set("start", state.start);
    }
    if (state.end) {
      params.set("end", state.end);
    }
    var url = window.location.pathname + (params.toString() ? "?" + params.toString() : "");
    history.replaceState(null, "", url);
  }

  function loadFromUrl() {
    var params = new URLSearchParams(window.location.search);
    var code = params.get("code");
    var date = params.get("date");
    var level = params.get("level");
    var start = params.get("start");
    var end = params.get("end");

    if (date) {
      dateInput.value = date;
      state.reportDate = date;
    }
    if (level) {
      levelInput.value = level;
      state.level = parseInt(level, 10);
    }
    if (start) {
      startInput.value = start;
      state.start = start;
    }
    if (end) {
      endInput.value = end;
      state.end = end;
    }
    if (code) {
      codeInput.value = code;
      state.code = code;
      validateFundCode(code);
      query();
    }
  }

  // ---------- 初始化 ----------
  (function init() {
    var today = new Date();
    endInput.value = isoDate(today);
    var threeYearsAgo = new Date(today.getFullYear() - 3, today.getMonth(), today.getDate());
    startInput.value = isoDate(threeYearsAgo);
    loadFromUrl();
  })();
})();
