/*
 * StockLab 基金调仓分析页前端逻辑 (app/web/static/fund_analysis.js)
 *
 * 职责：
 *   1. 基金代码/名称联想搜索
 *   2. 参数收集与校验
 *   3. 调用 /api/fund/allocation 进行调仓分析
 *   3. 渲染：概览卡片、暴露度时序图、调仓信号表格、当前持仓分布
 *   4. URL 状态同步（刷新/分享可还原）
 *
 * 两条不可让渡的展示约束：
 *   - R² < 0.5 时必须显著警告：模型拟合差，调仓信号不可信
 *   - 资金流佐证仅作辅助，不作为唯一判断依据
 */

(function () {
  "use strict";

  // ---------- DOM ----------
  var fundCodeInput = document.getElementById("fund-code-input");
  var fundSuggestList = document.getElementById("fund-suggest-list");
  var fundNameDisplay = document.getElementById("fund-name-display");
  var analysisDateInput = document.getElementById("analysis-date");
  var windowDaysInput = document.getElementById("window-days");
  var industryLevelInput = document.getElementById("industry-level");
  var useCapitalFlowInput = document.getElementById("use-capital-flow");
  var showCashInput = document.getElementById("show-cash");
  var btnAnalyze = document.getElementById("btn-analyze");
  var btnRefresh = document.getElementById("btn-refresh");
  var messageBox = document.getElementById("message");

  var overviewCards = document.getElementById("overview-cards");
  var chartSection = document.getElementById("chart-section");
  var signalsSection = document.getElementById("signals-section");
  var holdingsSection = document.getElementById("holdings-section");
  var emptyState = document.getElementById("empty-state");
  var loadingState = document.getElementById("loading-state");

  var ovFundName = document.getElementById("ov-fund-name");
  var ovFundCode = document.getElementById("ov-fund-code");
  var ovDate = document.getElementById("ov-date");
  var ovR2 = document.getElementById("ov-r2");
  var ovCash = document.getElementById("ov-cash");
  var ovInc = document.getElementById("ov-inc");
  var ovDec = document.getElementById("ov-dec");

  var exposureChartDom = document.getElementById("fa-exposure-chart");
  var signalsBody = document.getElementById("signals-body");
  var holdingsGrid = document.getElementById("holdings-grid");

  var chartTabs = document.querySelectorAll(".fa-chart-tab");

  // ---------- 状态 ----------
  var state = {
    fundCode: "",
    fundName: "",
    analysisDate: null,
    windowDays: 60,
    level: 1,
    useCapitalFlow: true,
    showCash: true,
    analysisResult: null,
    exposureChart: null,
    activeChartTab: "area",
  };

  var suggestTimer = null;
  var busy = false;

  // ---------- 工具函数 ----------
  function showMessage(text, kind) {
    SL.showMessage(messageBox, text, kind);
  }

  function clearMessage() {
    SL.clearMessage(messageBox);
  }

  function formatDate(d) {
    if (!d) return "-";
    if (typeof d === "string") return d;
    return d.getFullYear() + "-" +
      String(d.getMonth() + 1).padStart(2, "0") + "-" +
      String(d.getDate()).padStart(2, "0");
  }

  function formatPercent(value, decimals) {
    if (value === null || value === undefined || isNaN(value)) return "-";
    var d = decimals !== undefined ? decimals : 2;
    return (value * 100).toFixed(d) + "%";
  }

  function formatNumber(value, decimals) {
    if (value === null || value === undefined || isNaN(value)) return "-";
    return SL.formatNumber(value, decimals);
  }

  function escapeHtml(text) {
    return SL.escapeHtml(text);
  }

  function setLoading(isLoading) {
    busy = isLoading;
    btnAnalyze.disabled = isLoading;
    btnRefresh.disabled = isLoading;
    btnAnalyze.setAttribute("aria-busy", isLoading ? "true" : "false");
    btnRefresh.setAttribute("aria-busy", isLoading ? "true" : "false");
    loadingState.classList.toggle("hidden", !isLoading);
    if (!isLoading) {
      emptyState.classList.add("hidden");
    }
  }

  function showSections(show) {
    overviewCards.style.display = show ? "grid" : "none";
    chartSection.style.display = show ? "block" : "none";
    signalsSection.style.display = show ? "block" : "none";
    holdingsSection.style.display = show ? "block" : "none";
    emptyState.style.display = show ? "none" : "block";
  }

  // ---------- 基金联想 ----------
  function fetchFundSuggest(query) {
    if (!query || query.length < 2) {
      fundSuggestList.innerHTML = "";
      fundSuggestList.style.display = "none";
      return;
    }
    clearTimeout(suggestTimer);
    suggestTimer = setTimeout(function () {
      SL.fetchJson("/api/fund/list?type=" + encodeURIComponent(query), 8000)
        .then(function (data) {
          var items = data.items || [];
          fundSuggestList.innerHTML = "";
          if (items.length === 0) {
            fundSuggestList.style.display = "none";
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
            fundSuggestList.appendChild(div);
          });
          fundSuggestList.style.display = "block";
        })
        .catch(function () {
          fundSuggestList.style.display = "none";
        });
    }, 200);
  }

  function selectFund(code, name) {
    state.fundCode = code;
    state.fundName = name;
    fundCodeInput.value = code;
    fundNameDisplay.value = name;
    fundSuggestList.style.display = "none";
    clearMessage();
  }

  fundCodeInput.addEventListener("input", function () {
    fetchFundSuggest(this.value.trim());
    // 清除名称显示
    if (!this.value.includes(".")) {
      fundNameDisplay.value = "";
    }
  });

  fundCodeInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter") {
      e.preventDefault();
      var code = this.value.trim().toUpperCase();
      if (code) {
        // 尝试从联想列表匹配
        var items = fundSuggestList.querySelectorAll(".suggest-item");
        var matched = Array.from(items).find(function (el) {
          return el.dataset.code === code || el.dataset.name === code;
        });
        if (matched) {
          selectFund(matched.dataset.code, matched.dataset.name);
        } else {
          // 直接用代码查询
          state.fundCode = code;
          state.fundName = "";
          fundNameDisplay.value = "查询中...";
          // 验证代码格式
          if (/^\d{6}\.OF$/.test(code)) {
            validateFundCode(code);
          } else {
            showMessage("基金代码格式应为 000001.OF", "error");
          }
        }
      }
    }
  });

  function validateFundCode(code) {
    SL.fetchJson("/api/fund/info?code=" + encodeURIComponent(code), 8000)
      .then(function (data) {
        if (data.error) {
          showMessage("未找到该基金: " + code, "error");
          fundNameDisplay.value = "未找到";
        } else {
          state.fundCode = code;
          state.fundName = data.fund_name || "";
          fundNameDisplay.value = state.fundName;
          clearMessage();
        }
      })
      .catch(function (err) {
        showMessage(err.message || "基金信息查询失败", "error");
        fundNameDisplay.value = "查询失败";
      });
  }

  fundCodeInput.addEventListener("blur", function () {
    setTimeout(function () {
      fundSuggestList.style.display = "none";
    }, 200);
  });

  // ---------- 日期输入 ----------
  // 默认今天
  analysisDateInput.value = formatDate(new Date());

  // ---------- 图表标签切换 ----------
  chartTabs.forEach(function (tab) {
    tab.addEventListener("click", function () {
      chartTabs.forEach(function (t) { t.classList.remove("active"); });
      this.classList.add("active");
      state.activeChartTab = this.dataset.tab;
      if (state.analysisResult && state.exposureChart) {
        renderExposureChart();
      }
    });
  });

  // ---------- 核心分析 ----------
  function analyze() {
    var code = fundCodeInput.value.trim().toUpperCase();
    if (!code) {
      showMessage("请输入基金代码", "error");
      return;
    }
    if (!/^\d{6}\.OF$/.test(code)) {
      showMessage("基金代码格式应为 000001.OF", "error");
      return;
    }

    var dateVal = analysisDateInput.value;
    if (!dateVal) {
      showMessage("请选择分析基准日", "error");
      return;
    }

    var windowDays = parseInt(windowDaysInput.value, 10);
    if (isNaN(windowDays) || windowDays < 10 || windowDays > 250) {
      showMessage("回看窗口必须在 10-250 之间", "error");
      return;
    }

    var level = parseInt(industryLevelInput.value, 10);
    if (level !== 1 && level !== 2) {
      showMessage("行业层级必须是 1 或 2", "error");
      return;
    }

    state.fundCode = code;
    state.analysisDate = dateVal;
    state.windowDays = windowDays;
    state.level = level;
    state.useCapitalFlow = useCapitalFlowInput.checked;
    state.showCash = showCashInput.checked;

    // 更新 URL
    updateUrl();

    setLoading(true);
    clearMessage();

    var params = new URLSearchParams({
      code: code,
      date: dateVal,
      window: windowDays,
      level: level,
    });

    SL.fetchJson("/api/fund/allocation?" + params.toString(), 60000)
      .then(function (data) {
        if (data.error) {
          showMessage(data.error, "error");
          showSections(false);
          return;
        }
        state.analysisResult = data;
        renderResults(data);
        showSections(true);
        showMessage("分析完成", "success");
      })
      .catch(function (err) {
        showMessage(err.message || "分析失败，请稍后重试", "error");
        showSections(false);
      })
      .finally(function () {
        setLoading(false);
      });
  }

  // ---------- 渲染结果 ----------
  function renderResults(data) {
    state.analysisResult = data;

    // 概览卡片
    ovFundName.textContent = data.fund_code + " " + (data.fund_name || "");
    ovFundCode.textContent = data.fund_code;
    ovDate.textContent = formatDate(data.analysis_date);
    ovR2.textContent = formatPercent(data.r_squared, 4);
    ovCash.textContent = formatPercent(data.cash_exposure, 2);

    var signals = data.signals || [];
    var incCount = signals.filter(function (s) { return s.exposure_change > 0.005; }).length;
    var decCount = signals.filter(function (s) { return s.exposure_change < -0.005; }).length;
    ovInc.textContent = incCount;
    ovDec.textContent = decCount;

    // R² 警告
    var r2 = data.r_squared || 0;
    if (r2 < 0.5) {
      showMessage("⚠️ 拟合优度 R² = " + formatPercent(r2, 4) + " 低于 0.5，模型拟合较差，调仓信号仅供参考", "warning");
    }

    // 暴露度图表
    renderExposureChart();

    // 信号表格
    renderSignalsTable(data.signals || []);

    // 持仓分布
    renderHoldings(data.current_exposures || {}, data.exposure_history || []);
  }

  function renderExposureChart() {
    if (!state.analysisResult || !state.analysisResult.exposure_history) return;

    var history = state.analysisResult.exposure_history;
    if (!history.length) return;

    // 准备数据
    var dates = history.map(function (h) { return h.date; });
    var sectors = [];
    var sectorNames = {};

    // 收集所有出现过的行业
    history.forEach(function (h) {
      Object.keys(h.exposures).forEach(function (code) {
        if (sectors.indexOf(code) === -1) {
          sectors.push(code);
        }
      });
    });

    // 取前 10 大行业（按最后一期暴露度）
    var lastExposures = history[history.length - 1].exposures || {};
    sectors.sort(function (a, b) {
      return (lastExposures[b] || 0) - (lastExposures[a] || 0);
    });
    var topSectors = sectors.slice(0, 10);

    // 行业名称映射
    var signals = state.analysisResult.signals || [];
    signals.forEach(function (s) {
      sectorNames[s.sector_code] = s.sector_name;
    });

    // 构建系列数据
    var series = {};
    topSectors.forEach(function (code) {
      var name = sectorNames[code] || code;
      series[name] = history.map(function (h) {
        return h.exposures[code] || 0;
      });
    });

    // 其余归为"其他"
    if (sectors.length > 10) {
      var otherName = "其他";
      series[otherName] = history.map(function (h) {
        var sum = 0;
        sectors.slice(10).forEach(function (code) {
          sum += h.exposures[code] || 0;
        });
        return sum;
      });
    }

    var historyData = { dates: dates, series: series };

    var rebuild = function () {
      return SL.buildTrendOption({
        history: historyData,
        indicatorMeta: {},
        primary: Object.keys(series)[0] || "",
        singleAxis: true,
      });
    };

    if (state.exposureChart) {
      state.exposureChart.dispose();
    }
    state.exposureChart = SL.renderChart(exposureChartDom, rebuild(), rebuild);
  }

  function renderSignalsTable(signals) {
    signalsBody.innerHTML = "";

    if (!signals.length) {
      var tr = document.createElement("tr");
      var td = document.createElement("td");
      td.colSpan = 9;
      td.style.textAlign = "center";
      td.style.padding = "var(--rhythm-4)";
      td.style.color = "var(--text-muted)";
      td.textContent = "无显著调仓信号（所有行业环比变化 < 0.5%）";
      tr.appendChild(td);
      signalsBody.appendChild(tr);
      return;
    }

    signals.forEach(function (s) {
      var tr = document.createElement("tr");

      // 行业名称
      var tdName = document.createElement("td");
      tdName.textContent = s.sector_name;
      tr.appendChild(tdName);

      // 当前暴露度
      var tdExp = document.createElement("td");
      tdExp.className = "num";
      tdExp.textContent = formatPercent(s.current_exposure, 2);
      tr.appendChild(tdExp);

      // 环比变化
      var tdChg = document.createElement("td");
      tdChg.className = "num " + (s.exposure_change > 0 ? "positive" : (s.exposure_change < 0 ? "negative" : ""));
      tdChg.textContent = formatPercent(s.exposure_change, 2);
      tr.appendChild(tdChg);

      // 5日变化
      var tdChg5 = document.createElement("td");
      tdChg5.className = "num";
      tdChg5.textContent = formatPercent(s.exposure_change_5d, 2);
      tr.appendChild(tdChg5);

      // 20日变化
      var tdChg20 = document.createElement("td");
      tdChg20.className = "num";
      tdChg20.textContent = formatPercent(s.exposure_change_20d, 2);
      tr.appendChild(tdChg20);

      // 信号
      var tdSignal = document.createElement("td");
      var badge = document.createElement("span");
      badge.className = "signal-badge signal-" + s.signal.toLowerCase().replace(/ /g, "-");
      badge.textContent = s.signal;
      tdSignal.appendChild(badge);
      tr.appendChild(tdSignal);

      // 资金流相关性
      var tdCorr = document.createElement("td");
      tdCorr.className = "num";
      tdCorr.textContent = s.capital_flow_corr !== null && s.capital_flow_corr !== undefined ?
        s.capital_flow_corr.toFixed(2) : "-";
      tr.appendChild(tdCorr);

      // 资金流佐证（三态）：null = 窗口内资金流样本不足，既不能说吻合，
      // 也不能说不吻合 —— 显示成「✗ 不吻合」是拿没数据当成了反证
      var tdConfirm = document.createElement("td");
      var indicator = document.createElement("span");
      var hasVerdict = s.capital_flow_confirm !== null && s.capital_flow_confirm !== undefined;
      var confirmed = s.capital_flow_confirm === true;
      indicator.className = "fa-capital-flow-indicator" + (confirmed ? " confirm" : "");
      indicator.textContent = hasVerdict ? (confirmed ? "✓ 吻合" : "✗ 不吻合") : "— 无数据";
      tdConfirm.appendChild(indicator);
      tr.appendChild(tdConfirm);

      // 置信度
      var tdConf = document.createElement("td");
      var badgeConf = document.createElement("span");
      badgeConf.className = "fa-confidence-badge fa-confidence-" + s.confidence.toLowerCase();
      badgeConf.textContent = s.confidence;
      tdConf.appendChild(badgeConf);
      tr.appendChild(tdConf);

      signalsBody.appendChild(tr);
    });
  }

  function renderHoldings(currentExposures, history) {
    holdingsGrid.innerHTML = "";

    if (!currentExposures || Object.keys(currentExposures).length === 0) {
      holdingsGrid.innerHTML = '<div style="grid-column:1/-1;text-align:center;color:var(--text-muted);padding:var(--rhythm-4)">无持仓数据</div>';
      return;
    }

    // 按暴露度降序排序
    var sorted = Object.entries(currentExposures)
      .filter(function (e) { return e[1] > 0.001; })
      .sort(function (a, b) { return b[1] - a[1]; })
      .slice(0, 20);

    // 获取历史变化（最近一期 vs 上一期）
    var prevExposures = {};
    if (history.length >= 2) {
      prevExposures = history[history.length - 2].exposures || {};
    }

    // 行业名称映射
    var nameMap = {};
    var signals = state.analysisResult ? state.analysisResult.signals : [];
    signals.forEach(function (s) { nameMap[s.sector_code] = s.sector_name; });

    sorted.forEach(function (entry) {
      var code = entry[0];
      var exp = entry[1];
      var prev = prevExposures[code] || 0;
      var change = exp - prev;

      var card = document.createElement("div");
      card.className = "fa-holding-card";

      var name = document.createElement("div");
      name.className = "fa-holding-name";
      name.textContent = nameMap[code] || code;
      card.appendChild(name);

      var expDiv = document.createElement("div");
      expDiv.className = "fa-holding-exposure";
      expDiv.textContent = formatPercent(exp, 2);
      card.appendChild(expDiv);

      var chgDiv = document.createElement("div");
      chgDiv.className = "fa-holding-change " + (change > 0.0005 ? "positive" : (change < -0.0005 ? "negative" : "neutral"));
      chgDiv.textContent = (change > 0 ? "+" : "") + formatPercent(change, 2);
      card.appendChild(chgDiv);

      holdingsGrid.appendChild(card);
    });
  }

  // ---------- URL 状态同步 ----------
  function updateUrl() {
    var params = new URLSearchParams();
    if (state.fundCode) params.set("code", state.fundCode);
    if (state.analysisDate) params.set("date", state.analysisDate);
    if (state.windowDays !== 60) params.set("window", state.windowDays);
    if (state.level !== 1) params.set("level", state.level);
    var newUrl = window.location.pathname + (params.toString() ? "?" + params.toString() : "");
    history.replaceState(null, "", newUrl);
  }

  function loadFromUrl() {
    var params = new URLSearchParams(window.location.search);
    var code = params.get("code");
    var date = params.get("date");
    var window = params.get("window");
    var level = params.get("level");

    if (code) {
      fundCodeInput.value = code;
      state.fundCode = code;
      validateFundCode(code);
    }
    if (date) {
      analysisDateInput.value = date;
      state.analysisDate = date;
    }
    if (window) {
      windowDaysInput.value = window;
      state.windowDays = parseInt(window, 10);
    }
    if (level) {
      industryLevelInput.value = level;
      state.level = parseInt(level, 10);
    }

    // 如果 URL 里有完整参数，自动分析
    if (code && date) {
      analyze();
    }
  }

  // ---------- 事件绑定 ----------
  btnAnalyze.addEventListener("click", function () {
    if (!busy) analyze();
  });

  btnRefresh.addEventListener("click", function () {
    if (!busy && state.fundCode) analyze();
  });

  window.addEventListener("resize", function () {
    if (state.exposureChart) {
      state.exposureChart.resize();
    }
  });

  // ---------- 初始化 ----------
  loadFromUrl();
  SL.checkHealth();

  // 暴露给全局调试
  window.FundAnalysis = {
    state: state,
    analyze: analyze,
    renderResults: renderResults,
  };
})();