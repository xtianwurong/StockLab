/*
 * StockLab 多股对比页面脚本 (app/web/static/compare.js)
 *
 * 职责：标的挑选（代码/中文名联想）+ 一次请求取回多股分位与对齐走势，
 *       再渲染指标对比卡、叠加走势图、分位雷达与明细表。
 *
 * 设计取舍：
 *   - 选中集合是唯一状态（state.picked 的 code 数组），筹码行与请求参数都由它派生；
 *   - 图表用 SL.charts 的可复用片段拼装，并传 rebuild 回调跟随主题切换；
 *   - 雷达轴固定为五项指标（表格里出现过的），缺失分位不画点而非画 0。
 */

(function () {
  "use strict";

  var SL = window.SL;

  var MIN_CODES = 2;
  var MAX_CODES = 10;

  // 雷达固定轴：与 valuation_history 的五列一致
  var RADAR_AXES = ["pe_ttm", "pe_static", "pb", "ps", "pcf"];

  var state = {
    picked: [],        // [{code, name}]
    result: null,      // 最近一次接口应答
    mainIndicator: "", // 走势图当前主指标
    chart: null,
    radar: null,
    labels: {},        // 指标 -> 中文名
    suggestTimer: null
  };

  var dom = {};

  // ==========================================================================
  // 工具
  // ==========================================================================

  /**
   * 缓存 DOM
   *
   * @returns {void}
   */
  function cacheDom() {
    dom.input = document.getElementById("cmp-input");
    dom.suggest = document.getElementById("cmp-suggest");
    dom.period = document.getElementById("cmp-period");
    dom.chipRow = document.getElementById("chip-row");
    dom.message = document.getElementById("message");
    dom.resultEmpty = document.getElementById("result-empty");
    dom.resultBody = document.getElementById("result-body");
    dom.cards = document.getElementById("cmp-cards");
    dom.cardTitle = document.getElementById("cmp-card-title");
    dom.cardDesc = document.getElementById("cmp-card-desc");
    dom.chartDom = document.getElementById("cmp-chart");
    dom.radarDom = document.getElementById("radar-chart");
    dom.tabs = document.getElementById("cmp-indicator-tabs");
    dom.head = document.getElementById("cmp-head");
    dom.body = document.getElementById("cmp-body");
    dom.tableHint = document.getElementById("cmp-table-hint");
    dom.btn = document.getElementById("btn-cmp");
  }

  /**
   * 显示消息条
   *
   * @param {string} text 文案
   * @param {string} [type] info / warning / error / success
   * @returns {void}
   */
  function showMessage(text, type) {
    SL.showMessage(dom.message, text, type);
  }

  /**
   * 隐藏消息条
   *
   * @returns {void}
   */
  function hideMessage() {
    SL.clearMessage(dom.message);
  }

  /**
   * 取某个标的的显示名
   *
   * @param {string} code 代码
   * @returns {string} 名称
   */
  function nameOf(code) {
    for (var i = 0; i < state.picked.length; i++) {
      if (state.picked[i].code === code) {
        return state.picked[i].name || code;
      }
    }
    if (state.result && state.result.names) {
      for (var j = 0; j < state.result.names.length; j++) {
        if (state.result.names[j].ts_code === code) {
          return state.result.names[j].name || code;
        }
      }
    }
    return code;
  }

  /**
   * 取某标的的分类色
   *
   * @param {string} code 代码
   * @returns {string} 颜色
   */
  function colorOf(code) {
    if (state.result && state.result.names) {
      for (var i = 0; i < state.result.names.length; i++) {
        if (state.result.names[i].ts_code === code) {
          return state.result.names[i].color;
        }
      }
    }
    return SL.charts.cssVar("--accent", "#2563eb");
  }

  /**
   * 取指标的七档颜色
   *
   * @param {number|null} percentile 分位
   * @returns {string} 颜色
   */
  function levelColor(percentile) {
    if (SL.charts && SL.charts.levelColorOf) {
      return SL.charts.levelColorOf(percentile);
    }
    return SL.charts.cssVar("--level-na", "#94a3b8");
  }

  /**
   * 取指标中文名
   *
   * @param {string} indicator 指标列名
   * @returns {string} 中文名
   */
  function labelOf(indicator) {
    return state.labels[indicator] || indicator;
  }

  // ==========================================================================
  // 标的挑选
  // ==========================================================================

  /**
   * 渲染已选筹码行
   *
   * @returns {void}
   */
  function renderChips() {
    dom.chipRow.textContent = "";
    if (!state.picked.length) {
      dom.chipRow.appendChild(SL.el("span", "field-hint", "还没有添加标的"));
      return;
    }
    state.picked.forEach(function (item) {
      var chip = SL.el("div", "stock-chip");

      var dot = SL.el("span", "chip-dot");
      dot.style.background = colorOf(item.code);
      chip.appendChild(dot);

      chip.appendChild(SL.el("span", "chip-name", item.name || item.code));
      chip.appendChild(SL.el("span", "chip-code", item.code));

      var remove = SL.el("button", "chip-btn", "×");
      remove.type = "button";
      remove.setAttribute("aria-label", "移除 " + (item.name || item.code));
      remove.addEventListener("click", function () {
        state.picked = state.picked.filter(function (one) {
          return one.code !== item.code;
        });
        renderChips();
      });
      chip.appendChild(remove);

      dom.chipRow.appendChild(chip);
    });
  }

  /**
   * 添加标的（已存在则忽略）
   *
   * @param {string} code 代码
   * @param {string} [name] 名称
   * @returns {void}
   */
  function addCode(code, name) {
    for (var i = 0; i < state.picked.length; i++) {
      if (state.picked[i].code === code) {
        showMessage(code + " 已在对比列表里", "info");
        return;
      }
    }
    if (state.picked.length >= MAX_CODES) {
      SL.ui.toast.warn("一次最多对比 " + MAX_CODES + " 只");
      return;
    }
    state.picked.push({ code: code, name: name || "" });
    renderChips();
    syncUrl();
  }

  /**
   * 证券联想（复用个股页同一接口 /api/securities）
   *
   * @param {string} query 关键字
   * @returns {void}
   */
  function fetchSuggest(query) {
    if (!query) {
      dom.suggest.className = "suggest";
      dom.suggest.textContent = "";
      return;
    }
    SL.fetchJson("/api/securities?q=" + encodeURIComponent(query), 8000)
      .then(function (data) {
        var items = data.items || [];
        dom.suggest.textContent = "";
        if (!items.length) {
          dom.suggest.appendChild(SL.el("div", "suggest-empty", "没有匹配的证券"));
          dom.suggest.className = "suggest open";
          return;
        }
        items.forEach(function (item) {
          var row = SL.el("div", "suggest-item");
          row.appendChild(SL.el("span", "suggest-code", item.code));
          row.appendChild(SL.el("span", "suggest-name", item.name));
          row.addEventListener("click", function () {
            addCode(item.code, item.name);
            dom.input.value = "";
            dom.suggest.className = "suggest";
          });
          dom.suggest.appendChild(row);
        });
        dom.suggest.className = "suggest open";
      })
      .catch(function () {
        dom.suggest.className = "suggest";
      });
  }

  // ==========================================================================
  // 执行对比
  // ==========================================================================

  /**
   * 请求并渲染对比结果
   *
   * @returns {void}
   */
  function run() {
    if (state.picked.length < MIN_CODES) {
      showMessage("至少添加 " + MIN_CODES + " 只标的再开始对比。", "warning");
      return;
    }
    hideMessage();
    SL.ui.setLoading(dom.btn, true);

    var codes = state.picked.map(function (item) {
      return item.code;
    });
    var url = "/api/compare?codes=" + encodeURIComponent(codes.join(",")) +
      "&period=" + encodeURIComponent(dom.period.value);

    SL.fetchJson(url, 60000).then(function (data) {
      state.result = data;
      state.labels = data.indicator_labels || {};
      // 用接口返回的名称回填筹码（此时才有权威名称）
      data.names.forEach(function (entry) {
        for (var i = 0; i < state.picked.length; i++) {
          if (state.picked[i].code === entry.ts_code && entry.name) {
            state.picked[i].name = entry.name;
          }
        }
      });
      if (!state.mainIndicator || data.indicators.indexOf(state.mainIndicator) === -1) {
        state.mainIndicator = data.primary_indicator;
      }
      renderChips();
      renderAll(data);
      syncUrl();
      if (data.message) {
        showMessage(data.message, "warning");
      } else {
        SL.ui.toast.success("已对比 " + data.rows.length + " 只标的");
      }
    }).catch(function (error) {
      showMessage(error.message || "对比失败，请稍后重试", "error");
      SL.ui.toast.error(error.message || "对比失败");
    }).finally(function () {
      SL.ui.setLoading(dom.btn, false);
    });
  }

  /**
   * 渲染全部结果区块
   *
   * @param {object} data 接口应答
   * @returns {void}
   */
  function renderAll(data) {
    dom.resultEmpty.classList.add("hidden");
    dom.resultBody.classList.remove("hidden");
    renderCards(data);
    renderTabs(data);
    renderChart(data);
    renderRadar(data);
    renderTable(data);
  }

  /**
   * 渲染主指标对比卡（按分位从低到高排名，便宜的在左）
   *
   * @param {object} data 接口应答
   * @returns {void}
   */
  function renderCards(data) {
    var indicator = state.mainIndicator;
    dom.cardTitle.textContent = "指标对比 · " + labelOf(indicator);

    var ranked = data.rows.slice().filter(function (row) {
      return row.metrics && row.metrics[indicator];
    });
    ranked.sort(function (a, b) {
      var left = a.metrics[indicator].percentile;
      var right = b.metrics[indicator].percentile;
      if (left === null) {
        return 1;
      }
      if (right === null) {
        return -1;
      }
      return left - right;
    });

    dom.cardDesc.textContent = ranked.length
      ? "按分位从低到高排列（越靠左越便宜），区间 " + (data.interval_text || data.period)
      : "所选标的在该指标上都没有本地历史";

    dom.cards.textContent = "";
    ranked.forEach(function (row, index) {
      var metric = row.metrics[indicator];
      var card = SL.el("div", "cmp-card");

      var head = SL.el("div", "cmp-head");
      var nameBox = SL.el("div", "cmp-name");
      var dot = SL.el("span", "chip-dot");
      dot.style.background = colorOf(row.ts_code);
      nameBox.appendChild(dot);
      nameBox.appendChild(SL.el("span", "", nameOf(row.ts_code)));
      head.appendChild(nameBox);
      head.appendChild(SL.el("span", "cmp-rank", "#" + (index + 1)));
      card.appendChild(head);

      card.appendChild(SL.el("div", "cmp-label", labelOf(indicator) + " 当前值"));
      var value = SL.el("div", "cmp-value", SL.formatNumber(metric.current_value, 3));
      value.style.color = levelColor(metric.percentile);
      card.appendChild(value);

      var badgeRow = SL.el("div", "row row-tight");
      badgeRow.style.marginTop = "var(--space-2)";
      badgeRow.innerHTML = SL.levelBadge(metric.percentile, metric.level7);
      card.appendChild(badgeRow);

      var facts = SL.el("div", "cmp-facts");
      [
        ["中位数", SL.formatNumber(metric.median_value, 2)],
        ["最小值", SL.formatNumber(metric.min_value, 2)],
        ["最大值", SL.formatNumber(metric.max_value, 2)]
      ].forEach(function (pair) {
        var cell = SL.el("div");
        cell.appendChild(SL.el("div", "cmp-fk", pair[0]));
        cell.appendChild(SL.el("div", "cmp-fv", pair[1]));
        facts.appendChild(cell);
      });
      card.appendChild(facts);

      dom.cards.appendChild(card);
    });
  }

  /**
   * 渲染指标切换 tab
   *
   * @param {object} data 接口应答
   * @returns {void}
   */
  function renderTabs(data) {
    dom.tabs.textContent = "";
    data.indicators.forEach(function (indicator) {
      var tab = SL.el("button", "tab" + (indicator === state.mainIndicator ? " active" : ""),
        labelOf(indicator));
      tab.type = "button";
      tab.addEventListener("click", function () {
        if (state.mainIndicator === indicator) {
          return;
        }
        state.mainIndicator = indicator;
        renderTabs(data);
        renderCards(data);
        renderChart(data);
      });
      dom.tabs.appendChild(tab);
    });
  }

  /**
   * 渲染叠加走势图（每只标的一条线）
   *
   * @param {object} data 接口应答
   * @returns {void}
   */
  function renderChart(data) {
    var history = data.history || { dates: [], series: {} };
    var indicator = state.mainIndicator;

    var build = function () {
      var color = SL.palette();
      var legendData = [];
      var seriesList = [];

      data.codes.forEach(function (code) {
        legendData.push(nameOf(code));
        seriesList.push({
          name: nameOf(code),
          type: "line",
          data: history.series[code] || [],
          showSymbol: false,
          smooth: false,
          connectNulls: false,   // 缺失交易日断线，不假装有值
          lineStyle: { width: 1.9 },
          itemStyle: { color: colorOf(code) },
          z: 4
        });
      });

      var option = SL.charts.baseOption(legendData);
      option.grid = SL.charts.grid({ left: 62, right: 24, top: 40, bottom: 66 });
      option.tooltip = SL.charts.tooltip();
      option.tooltip.formatter = function (params) {
        if (!params.length) {
          return "";
        }
        var html = '<div style="font-weight:600;margin-bottom:5px">' +
          SL.escapeHtml(params[0].axisValue) + "</div>";
        for (var i = 0; i < params.length; i++) {
          var item = params[i];
          var value = (item.value === null || item.value === undefined || item.value !== item.value)
            ? "-"
            : SL.formatNumber(item.value, 2);
          html += '<div style="display:flex;justify-content:space-between;gap:16px">' +
            '<span style="color:' + color.textMuted + '">' +
            item.marker + SL.escapeHtml(item.seriesName) + "</span>" +
            '<span style="font-weight:600;font-family:var(--font-mono)">' + value + "</span></div>";
        }
        return html;
      };
      option.xAxis = SL.charts.categoryAxis(history.dates);
      option.yAxis = SL.charts.valueAxis({ name: labelOf(indicator), position: "left" });
      option.dataZoom = SL.charts.dataZoom();
      option.series = seriesList;
      return option;
    };

    state.chart = SL.charts.render(dom.chartDom, build(), build);
  }

  /**
   * 渲染分位雷达（各标的在五项指标上的分位）
   *
   * @param {object} data 接口应答
   * @returns {void}
   */
  function renderRadar(data) {
    var axes = RADAR_AXES.filter(function (indicator) {
      return data.indicators.indexOf(indicator) !== -1;
    });
    if (axes.length < 3) {
      dom.radarDom.textContent = "";
      return;
    }

    var build = function () {
      var option = SL.charts.baseOption(null);
      option.tooltip = SL.charts.tooltip();
      option.tooltip.formatter = function () {
        return "";
      };
      option.radar = {
        indicator: axes.map(function (indicator) {
          return { name: labelOf(indicator), max: 100 };
        }),
        shape: "polygon",
        splitNumber: 4,
        axisName: { color: SL.palette().textMuted, fontSize: 11 },
        splitLine: { lineStyle: { color: SL.palette().split } },
        splitArea: { areaStyle: { color: ["transparent"] } },
        axisLine: { lineStyle: { color: SL.palette().split } }
      };
      option.series = [{
        type: "radar",
        symbolSize: 3,
        data: data.rows.map(function (row) {
          var values = axes.map(function (indicator) {
            var metric = row.metrics ? row.metrics[indicator] : null;
            return metric && metric.percentile !== null ? metric.percentile : 0;
          });
          return {
            name: nameOf(row.ts_code),
            value: values,
            lineStyle: { width: 1.8, color: colorOf(row.ts_code) },
            itemStyle: { color: colorOf(row.ts_code) },
            areaStyle: { opacity: 0.06 }
          };
        })
      }];
      return option;
    };

    state.radar = SL.charts.render(dom.radarDom, build(), build);
  }

  /**
   * 渲染分位明细表
   *
   * @param {object} data 接口应答
   * @returns {void}
   */
  function renderTable(data) {
    dom.tableHint.textContent = "区间 " + (data.interval_text || data.period) +
      "；点指标名可切换主图指标与上方对比卡";

    dom.head.textContent = "";
    dom.head.appendChild(SL.el("th", "", "标的"));
    dom.head.appendChild(SL.el("th", "", "市场"));
    data.indicators.forEach(function (indicator) {
      var th = SL.el("th", "ta-r col-indicator" +
        (indicator === state.mainIndicator ? " active" : ""), labelOf(indicator));
      th.addEventListener("click", function () {
        if (state.mainIndicator === indicator) {
          return;
        }
        state.mainIndicator = indicator;
        renderTabs(data);
        renderCards(data);
        renderChart(data);
        renderTable(data);
      });
      dom.head.appendChild(th);
    });

    dom.body.textContent = "";
    data.rows.forEach(function (row) {
      var tr = SL.el("tr");

      var nameCell = SL.el("td");
      var box = SL.el("div", "name-cell");
      var dot = SL.el("span", "chip-dot");
      dot.style.background = colorOf(row.ts_code);
      box.appendChild(dot);
      box.appendChild(SL.el("span", "", nameOf(row.ts_code)));
      box.appendChild(SL.el("span", "stock-code", row.ts_code));
      nameCell.appendChild(box);
      tr.appendChild(nameCell);

      tr.appendChild(SL.el("td", "", row.market || "-"));

      data.indicators.forEach(function (indicator) {
        var metric = row.metrics ? row.metrics[indicator] : null;
        var td = SL.el("td", "ta-r");
        if (!metric) {
          td.appendChild(SL.el("span", "text-faint", "-"));
          tr.appendChild(td);
          return;
        }
        var cell = SL.el("div", "cell-v");
        var value = SL.el("span", "mono", SL.formatNumber(metric.current_value, 2));
        value.style.color = levelColor(metric.percentile);
        value.style.fontWeight = "600";
        cell.appendChild(value);
        cell.appendChild(SL.el("span", "cell-p",
          "分位 " + SL.formatPercent(metric.percentile) +
          " · " + metric.level7 + " · " + SL.formatInt(metric.sample_count) + "样本"));
        td.appendChild(cell);
        tr.appendChild(td);
      });

      tr.className = "row-link";
      tr.addEventListener("click", function () {
        window.location.href = "/?code=" + encodeURIComponent(row.ts_code);
      });
      dom.body.appendChild(tr);
    });
  }

  // ==========================================================================
  // URL 还原与分享
  // ==========================================================================

  /**
   * 条件写进地址栏
   *
   * @returns {void}
   */
  function syncUrl() {
    SL.ui.replaceQuery({
      codes: state.picked.length ? state.picked.map(function (item) {
        return item.code;
      }).join(",") : null,
      period: dom.period.value !== "全部" ? dom.period.value : null,
      ind: state.mainIndicator !== "pe_ttm" ? state.mainIndicator : null
    });
  }

  /**
   * 从地址栏还原
   *
   * @returns {void}
   */
  function restoreFromUrl() {
    var params = SL.ui.queryParams();
    if (params.codes) {
      params.codes.split(",").forEach(function (code) {
        var clean = code.trim().toUpperCase();
        if (clean) {
          state.picked.push({ code: clean, name: "" });
        }
      });
    }
    if (params.period) {
      dom.period.value = params.period;
    }
    if (params.ind) {
      state.mainIndicator = params.ind;
    }
  }

  // ==========================================================================
  // 初始化
  // ==========================================================================

  /**
   * 绑定事件
   *
   * @returns {void}
   */
  function bindEvents() {
    dom.btn.addEventListener("click", run);

    dom.input.addEventListener("input", function () {
      var value = dom.input.value.trim();
      if (state.suggestTimer) {
        clearTimeout(state.suggestTimer);
      }
      state.suggestTimer = setTimeout(function () {
        fetchSuggest(value);
      }, 220);
    });

    dom.input.addEventListener("keydown", function (event) {
      if (event.key !== "Enter") {
        return;
      }
      event.preventDefault();
      var value = dom.input.value.trim();
      if (!value) {
        return;
      }
      // 输入框支持「代码,代码」批量，或单个中文名（交给联想）
      if (value.indexOf(",") !== -1 || /^[0-9]{6}(\.(SH|SZ|BJ))?$/i.test(value)) {
        value.split(",").forEach(function (code) {
          var clean = code.trim().toUpperCase();
          if (clean) {
            addCode(clean, "");
          }
        });
        dom.input.value = "";
        dom.suggest.className = "suggest";
      } else {
        fetchSuggest(value);
      }
    });

    dom.period.addEventListener("change", function () {
      if (state.picked.length >= MIN_CODES) {
        run();
      }
    });

    document.addEventListener("click", function (event) {
      if (!dom.suggest.contains(event.target) && event.target !== dom.input) {
        dom.suggest.className = "suggest";
      }
    });

    SL.ui.shortcut("r", function () {
      run();
    }, "重新对比");

    SL.ui.bindTooltips(document);
  }

  /**
   * 启动
   *
   * @returns {void}
   */
  function init() {
    cacheDom();
    bindEvents();
    restoreFromUrl();
    renderChips();
    if (state.picked.length >= MIN_CODES) {
      run();
    }
    SL.checkHealth();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
