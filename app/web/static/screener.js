/*
 * StockLab 选股器页面脚本 (app/web/static/screener.js)
 *
 * 职责：可视化规则编辑器 + 结果展示。
 *   1. 规则编辑：增删规则、选因子/算子/阈值，支持从预置模板一键载入
 *   2. 执行筛选：组装 spec 调 POST /api/screener/run
 *   3. 结果展示：概览 / 规则漏斗 / 因子散点 / 明细表 / 行业通过率
 *   4. 分享与导出：条件写进地址栏可刷新可分享，结果可导出 CSV
 *
 * 设计取舍：
 *   - 规则状态是唯一数据源（state.rules），DOM 每次全量重绘；
 *     规则最多 12 条，全量重绘的代价可忽略，换来的是「没有两份真相」。
 *   - 因子选择器按覆盖率降序排列并标色：本地只有 pe_ttm/pb 有数据，
 *     若不提示，用户会对着全 NaN 的因子建规则却只得到 0 只通过。
 */

(function () {
  "use strict";

  var SL = window.SL;

  // ==========================================================================
  // 状态
  // ==========================================================================
  var state = {
    meta: null,          // /api/screener/meta 的应答
    rules: [],           // [{factor, operator, value}]
    lastResult: null,    // 最近一次筛选应答
    scatterChart: null,
    factorOptions: null, // 因子下拉的缓存 DOM（面板打开时构建一次）
    panelOpen: false
  };

  var MAX_RULES = 12;

  // DOM
  var dom = {};

  // ==========================================================================
  // 工具
  // ==========================================================================

  /**
   * 缓存页面 DOM 引用
   *
   * @returns {void}
   */
  function cacheDom() {
    dom.asOf = document.getElementById("as-of-select");
    dom.chkPositive = document.getElementById("chk-positive");
    dom.universe = document.getElementById("meta-universe");
    dom.message = document.getElementById("message");
    dom.preset = document.getElementById("preset-select");
    dom.presetNote = document.getElementById("preset-note");
    dom.ruleList = document.getElementById("rule-list");
    dom.resultEmpty = document.getElementById("result-empty");
    dom.resultBody = document.getElementById("result-body");
    dom.resultRows = document.getElementById("result-body-rows");
    dom.resultHead = document.getElementById("result-head");
    dom.sortSelect = document.getElementById("sort-select");
    dom.condText = document.getElementById("cond-text");
    dom.statPassed = document.getElementById("stat-passed");
    dom.statTotal = document.getElementById("stat-total");
    dom.statElapsed = document.getElementById("stat-elapsed");
    dom.funnelList = document.getElementById("funnel-list");
    dom.scatterCard = document.getElementById("scatter-card");
    dom.scatterHint = document.getElementById("scatter-hint");
    dom.scatterDom = document.getElementById("scatter-chart");
    dom.industryList = document.getElementById("industry-list");
    dom.industryCard = document.getElementById("industry-card");
    dom.tableHint = document.getElementById("table-hint");
  }

  /**
   * 显示消息条
   *
   * @param {string} text 文案
   * @param {string} [type] info / success / warning / error
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
   * 按钮加载态
   *
   * @param {HTMLElement} button 按钮
   * @param {boolean} loading 是否加载中
   * @returns {void}
   */
  function setBusy(button, loading) {
    SL.ui.setLoading(button, loading);
    if (dom.btnRun) {
      dom.btnRun.disabled = loading;
    }
  }

  /**
   * 取算子中文名
   *
   * @param {string} key 算子键
   * @returns {string} 中文名
   */
  function operatorLabel(key) {
    var list = (state.meta && state.meta.operators) || [];
    for (var i = 0; i < list.length; i++) {
      if (list[i].key === key) {
        return list[i].label;
      }
    }
    return key;
  }

  /**
   * 取算子是否需要阈值输入
   *
   * @param {string} key 算子键
   * @returns {boolean} 是否需要阈值
   */
  function operatorNeedsValue(key) {
    return key !== "isna" && key !== "notna";
  }

  /**
   * 按名字取因子元数据
   *
   * @param {string} name 因子名
   * @returns {object|null} 因子元数据
   */
  function factorMeta(name) {
    var list = (state.meta && state.meta.factors) || [];
    for (var i = 0; i < list.length; i++) {
      if (list[i].name === name) {
        return list[i];
      }
    }
    return null;
  }

  /**
   * 组装当前规则的 spec
   *
   * @returns {object} ScreenPipeline spec
   */
  function buildSpec() {
    var rules = [];
    for (var i = 0; i < state.rules.length; i++) {
      var rule = state.rules[i];
      if (!rule.factor) {
        continue;
      }
      var item = { factor: rule.factor, operator: rule.operator };
      if (operatorNeedsValue(rule.operator)) {
        item.value = rule.value === "" || rule.value === null ? null : Number(rule.value);
      }
      rules.push(item);
    }
    return { rules: rules };
  }

  // ==========================================================================
  // 规则编辑器
  // ==========================================================================

  /**
   * 渲染规则列表
   *
   * @returns {void}
   */
  function renderRules() {
    dom.ruleList.textContent = "";

    if (!state.rules.length) {
      var empty = SL.el("div", "empty-state");
      empty.appendChild(SL.el("div", "empty-state-desc", "还没有条件，点上方「加一条」开始"));
      dom.ruleList.appendChild(empty);
      return;
    }

    state.rules.forEach(function (rule, index) {
      dom.ruleList.appendChild(buildRuleCard(rule, index));
    });
  }

  /**
   * 构建单条规则卡
   *
   * @param {object} rule 规则对象（就地修改）
   * @param {number} index 序号
   * @returns {HTMLElement} 规则卡元素
   */
  function buildRuleCard(rule, index) {
    var card = SL.el("div", "rule-card");
    var row = SL.el("div", "rule-row");

    // 因子下拉：按覆盖率降序（后端已排好），并标出数据可用性
    var factorSelect = SL.el("select", "select");
    factorSelect.setAttribute("aria-label", "第 " + (index + 1) + " 条规则的因子");
    var placeholder = SL.el("option", "", "选择因子…");
    placeholder.value = "";
    factorSelect.appendChild(placeholder);
    var factors = (state.meta && state.meta.factors) || [];
    factors.forEach(function (item) {
      var option = SL.el("option");
      option.value = item.name;
      var mark = item.availability === "ready" ? "" : "（无数据）";
      option.textContent = item.name + mark;
      if (item.availability === "none") {
        option.disabled = true;
      }
      factorSelect.appendChild(option);
    });
    factorSelect.value = rule.factor || "";
    factorSelect.addEventListener("change", function () {
      rule.factor = factorSelect.value;
      refreshRuleHint(card, rule);
    });

    // 算子下拉
    var opSelect = SL.el("select", "select");
    opSelect.setAttribute("aria-label", "第 " + (index + 1) + " 条规则的算子");
    var operators = (state.meta && state.meta.operators) || [];
    operators.forEach(function (item) {
      var option = SL.el("option");
      option.value = item.key;
      option.textContent = item.label;
      opSelect.appendChild(option);
    });
    opSelect.value = rule.operator || "lt";
    opSelect.addEventListener("change", function () {
      rule.operator = opSelect.value;
      renderRules();
    });

    // 阈值输入
    var valueInput = SL.el("input", "input rule-value");
    valueInput.type = "text";
    valueInput.inputMode = "decimal";
    valueInput.setAttribute("aria-label", "第 " + (index + 1) + " 条规则的阈值");
    valueInput.value = rule.value === null || rule.value === undefined ? "" : rule.value;
    valueInput.placeholder = "阈值";
    valueInput.addEventListener("input", function () {
      rule.value = valueInput.value;
    });
    if (!operatorNeedsValue(rule.operator)) {
      valueInput.disabled = true;
      valueInput.placeholder = "无需阈值";
    }

    // 删除
    var del = SL.el("button", "btn btn-ghost btn-icon btn-sm rule-del", "×");
    del.type = "button";
    del.setAttribute("aria-label", "删除第 " + (index + 1) + " 条规则");
    del.addEventListener("click", function () {
      state.rules.splice(index, 1);
      renderRules();
    });

    row.appendChild(factorSelect);
    row.appendChild(opSelect);
    row.appendChild(valueInput);
    row.appendChild(del);
    card.appendChild(row);

    refreshRuleHint(card, rule);
    return card;
  }

  /**
   * 在规则卡下方补一行「因子说明 + 覆盖率」，让用户当场看清数据是否可用
   *
   * @param {HTMLElement} card 规则卡
   * @param {object} rule 规则对象
   * @returns {void}
   */
  function refreshRuleHint(card, rule) {
    var old = card.querySelector(".factor-hint");
    if (old) {
      old.parentNode.removeChild(old);
    }
    var meta = factorMeta(rule.factor);
    if (!meta) {
      return;
    }

    var hint = SL.el("div", "factor-hint");
    hint.style.cssText = "display:flex;align-items:center;gap:var(--space-2);margin-top:var(--space-2);font-size:var(--font-xs);color:var(--text-faint)";

    var name = SL.el("span", "mono", meta.name);
    name.style.color = "var(--text-muted)";
    hint.appendChild(name);

    // 覆盖率条
    var cov = SL.el("div", "cov");
    var track = SL.el("div", "cov-track");
    var fill = SL.el("div", "cov-fill " + meta.availability);
    fill.style.width = Math.max(2, Math.min(100, meta.coverage > 0 ? meta.coverage : 0)) + "%";
    if (meta.coverage < 0) {
      fill.style.width = "2%";
    }
    track.appendChild(fill);
    cov.appendChild(track);
    cov.appendChild(SL.el("span", "cov-text",
      meta.coverage < 0 ? "无输入列" : meta.coverage.toFixed(1) + "%"));
    hint.appendChild(cov);

    if (meta.availability !== "ready") {
      var warn = SL.el("span", "badge badge-warning", "本地无数据");
      hint.appendChild(warn);
    }

    var desc = SL.el("span", "factor-desc", meta.description || "");
    desc.style.flex = "1";
    hint.appendChild(desc);

    card.appendChild(hint);
  }

  /**
   * 载入预置模板
   *
   * @param {string} key 模板键
   * @returns {void}
   */
  function loadPreset(key) {
    var presets = (state.meta && state.meta.presets) || [];
    var preset = null;
    for (var i = 0; i < presets.length; i++) {
      if (presets[i].key === key) {
        preset = presets[i];
        break;
      }
    }
    if (!preset) {
      return;
    }

    state.rules = (preset.spec.rules || []).map(function (item) {
      return {
        factor: item.factor,
        operator: item.operator,
        value: item.value === undefined ? null : item.value
      };
    });

    if (preset.readiness !== "ready") {
      showMessage(
        "模板「" + preset.name + "」引用的因子本地暂无数据源，筛选结果可能全为 0。" +
        "请先同步基本面 / 日 K / 分红数据，或改用估值类因子。",
        "warning"
      );
    } else {
      showMessage("已载入模板「" + preset.name + "」，可继续微调阈值。", "info");
    }
    renderRules();
    run();
  }

  /**
   * 把条件写进地址栏（刷新/分享都能还原）
   *
   * @returns {void}
   */
  function syncUrl() {
    var spec = buildSpec();
    SL.ui.replaceQuery({
      spec: spec.rules.length ? JSON.stringify(spec) : null,
      as_of: dom.asOf.value || null,
      pos: dom.chkPositive.checked ? "1" : null
    });
  }

  /**
   * 从地址栏还原条件
   *
   * @returns {void}
   */
  function restoreFromUrl() {
    var params = SL.ui.queryParams();
    if (params.spec) {
      try {
        var parsed = JSON.parse(decodeURIComponent(params.spec));
        if (parsed && parsed.rules && parsed.rules.length) {
          state.rules = parsed.rules.slice(0, MAX_RULES).map(function (item) {
            return {
              factor: item.factor,
              operator: item.operator || "lt",
              value: item.value === undefined ? null : item.value
            };
          });
        }
      } catch (error) {
        // 地址栏里的 spec 坏了就忽略，回退到空条件
      }
    }
    if (params.pos === "0" && dom.chkPositive) {
      dom.chkPositive.checked = false;
    }
  }

  // ==========================================================================
  // 执行筛选
  // ==========================================================================

  /**
   * 执行筛选
   *
   * @returns {void}
   */
  function run() {
    var spec = buildSpec();
    if (!spec.rules.length) {
      showMessage("请至少设置一条筛选条件。", "warning");
      return;
    }

    hideMessage();
    setBusy(dom.btnRun, true);

    SL.fetchJson("/api/screener/run", 30000, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        spec: spec,
        as_of: dom.asOf.value,
        positive_only: dom.chkPositive.checked,
        sort: dom.sortSelect.value || "ts_code",
        order: "asc",
        limit: 200
      })
    }).then(function (data) {
      state.lastResult = data;
      renderResult(data);
      syncUrl();
      SL.ui.toast.success(
        "筛选完成：通过 " + data.counts.passed + " / " + data.counts.total + " 只"
      );
    }).catch(function (error) {
      showMessage(error.message || "筛选失败，请稍后重试", "error");
      SL.ui.toast.error(error.message || "筛选失败");
    }).finally(function () {
      setBusy(dom.btnRun, false);
    });
  }

  /**
   * 渲染筛选结果
   *
   * @param {object} data 接口应答
   * @returns {void}
   */
  function renderResult(data) {
    dom.resultEmpty.classList.add("hidden");
    dom.resultBody.classList.remove("hidden");

    dom.statPassed.textContent = SL.formatInt(data.counts.passed);
    dom.statTotal.textContent = SL.formatInt(data.counts.total);
    dom.statElapsed.textContent = "耗时 " + data.elapsed_ms + " ms · 引擎口径：" +
      (data.positive_only ? "已剔除亏损股" : "含亏损股");
    dom.condText.textContent = data.condition;

    renderFunnel(data.funnel);
    renderScatter(data.scatter);
    renderTable(data);
    renderIndustries(data.industries);
  }

  /**
   * 渲染规则漏斗
   *
   * @param {Array<object>} funnel 漏斗数据
   * @returns {void}
   */
  function renderFunnel(funnel) {
    dom.funnelList.textContent = "";
    if (!funnel || !funnel.length) {
      dom.funnelList.appendChild(SL.el("div", "field-hint", "无漏斗数据"));
      return;
    }

    var maxPassed = 1;
    funnel.forEach(function (item) {
      maxPassed = Math.max(maxPassed, item.passed);
    });

    funnel.forEach(function (item) {
      var row = SL.el("div", "funnel-item");

      var label = SL.el("div", "funnel-label");
      label.textContent = item.factor + " " + operatorLabel(item.operator) +
        (item.threshold === "-" ? "" : " " + item.threshold);
      if (!item.in_frame) {
        label.style.color = "var(--danger-500)";
      }
      row.appendChild(label);

      var bar = SL.el("div", "funnel-bar");
      var fill = SL.el("div", "funnel-fill");
      fill.style.width = (item.passed * 100 / maxPassed) + "%";
      if (!item.in_frame) {
        fill.style.background = "var(--danger-500)";
      }
      bar.appendChild(fill);
      row.appendChild(bar);

      row.appendChild(SL.el("div", "funnel-num", SL.formatInt(item.passed)));
      dom.funnelList.appendChild(row);
    });
  }

  /**
   * 渲染因子散点图（通过=品牌色，未通过=灰）
   *
   * @param {object} scatter 散点数据 {x, y, points}
   * @returns {void}
   */
  function renderScatter(scatter) {
    if (!scatter || !scatter.points || !scatter.points.length) {
      dom.scatterCard.classList.add("hidden");
      if (state.scatterChart) {
        state.scatterChart = null;
      }
      return;
    }
    dom.scatterCard.classList.remove("hidden");

    var xName = scatter.x;
    var yName = scatter.y;
    var color = SL.palette();

    var passed = scatter.points.filter(function (point) {
      return point[2];
    });
    var failed = scatter.points.filter(function (point) {
      return !point[2];
    });

    dom.scatterHint.textContent =
      "横轴 " + xName + "，纵轴 " + yName + "；品牌色为通过（" +
      passed.length + " 只），灰色为未通过（" + failed.length + " 只）";

    var build = function () {
      return SL.charts.baseOption(null);
    };

    var option = build();
    option.grid = SL.charts.grid({ left: 62, right: 24, top: 20, bottom: 46 });
    option.tooltip = SL.charts.tooltip();
    option.tooltip.formatter = function (params) {
      var point = params[0].value;
      if (!point || point.length < 5) {
        return "";
      }
      return "<b>" + SL.escapeHtml(point[4]) + "</b> " + SL.escapeHtml(point[3]) +
        "<br/>" + xName + " " + SL.formatNumber(point[0], 2) +
        "<br/>" + yName + " " + SL.formatNumber(point[1], 2) +
        "<br/>" + (point[2] ? "通过" : "未通过");
    };
    option.xAxis = SL.charts.valueAxis({ name: xName });
    option.xAxis.type = "value";
    option.xAxis.scale = true;
    option.yAxis = SL.charts.valueAxis({ name: yName, grid: true });
    option.yAxis.type = "value";
    option.yAxis.scale = true;
    option.series = [
      {
        name: "未通过",
        type: "scatter",
        symbolSize: 5,
        itemStyle: { color: color.axis, opacity: 0.5 },
        data: failed
      },
      {
        name: "通过",
        type: "scatter",
        symbolSize: 7,
        itemStyle: { color: color.accent },
        data: passed
      }
    ];

    state.scatterChart = SL.charts.render(dom.scatterDom, option, build);
  }

  /**
   * 渲染结果表
   *
   * @param {object} data 接口应答
   * @returns {void}
   */
  function renderTable(data) {
    var rows = data.rows || [];
    dom.tableHint.textContent = "共 " + SL.formatInt(data.total) + " 只，返回 " +
      rows.length + " 行（最多 500 行）";

    // 表头：固定列 + 规则里用到的因子列
    var factorColumns = [];
    (function walk(node) {
      if (!node || typeof node !== "object") {
        return;
      }
      if (node.factor && factorColumns.indexOf(node.factor) === -1) {
        factorColumns.push(node.factor);
      }
      (node.rules || []).forEach(walk);
    })(data.spec);

    dom.resultHead.textContent = "";
    var headCells = ["标的", "行业"];
    factorColumns.forEach(function (name) {
      headCells.push(name);
    });
    headCells.push("结果");
    headCells.push("未通过原因");

    headCells.forEach(function (label, index) {
      var th = SL.el("th", index >= 2 && index < 2 + factorColumns.length ? "ta-r" : "");
      th.textContent = label;
      dom.resultHead.appendChild(th);
    });

    // 表体
    dom.resultRows.textContent = "";
    rows.forEach(function (row) {
      var tr = SL.el("tr");

      var nameCell = SL.el("td");
      var stock = SL.el("div", "stock-cell");
      stock.appendChild(SL.el("span", "stock-name", row.name || "-"));
      stock.appendChild(SL.el("span", "stock-code", row.ts_code));
      nameCell.appendChild(stock);
      tr.appendChild(nameCell);

      tr.appendChild(SL.el("td", "", row.industry || "未分类"));

      factorColumns.forEach(function (name) {
        var cell = SL.el("td", "ta-r mono", SL.formatNumber(row[name], 3));
        if (row[name] === null || row[name] === undefined) {
          cell.style.color = "var(--text-faint)";
        }
        tr.appendChild(cell);
      });

      var resultCell = SL.el("td", "ta-c");
      if (row.passed) {
        resultCell.appendChild(SL.el("span", "badge badge-success", "通过"));
      } else {
        resultCell.appendChild(SL.el("span", "badge badge-neutral", "未通过"));
      }
      tr.appendChild(resultCell);

      var reasonCell = SL.el("td");
      var reason = SL.el("div", "reason-cell", (row.failed_rules || []).join("；") || "-");
      reasonCell.appendChild(reason);
      tr.appendChild(reasonCell);

      tr.className = "row-link";
      tr.addEventListener("click", function () {
        window.location.href = "/?code=" + encodeURIComponent(row.ts_code);
      });

      dom.resultRows.appendChild(tr);
    });

    // 排序下拉：ts_code + 因子列
    var current = dom.sortSelect.value;
    dom.sortSelect.textContent = "";
    dom.sortSelect.appendChild(makeSortOption("", "按代码"));
    factorColumns.forEach(function (name) {
      dom.sortSelect.appendChild(makeSortOption(name, "按 " + name));
    });
    dom.sortSelect.value = current;
    if (dom.sortSelect.selectedIndex === -1) {
      dom.sortSelect.value = "";
    }
  }

  /**
   * 构造排序下拉项
   *
   * @param {string} value 值
   * @param {string} label 文案
   * @returns {HTMLElement} option 元素
   */
  function makeSortOption(value, label) {
    var option = SL.el("option", "", label);
    option.value = value;
    return option;
  }

  /**
   * 渲染行业通过率
   *
   * @param {Array<object>} industries 行业数据
   * @returns {void}
   */
  function renderIndustries(industries) {
    if (!industries || !industries.length) {
      dom.industryCard.classList.add("hidden");
      dom.industryList.textContent = "";
      return;
    }
    dom.industryCard.classList.remove("hidden");
    dom.industryList.textContent = "";

    industries.forEach(function (item) {
      var row = SL.el("div", "ind-row");

      var nameCell = SL.el("div", "ind-name");
      nameCell.appendChild(SL.el("span", "", item.industry));
      var sub = SL.el("span", "text-faint", " " + item.passed + "/" + item.total);
      sub.style.fontSize = "var(--font-xs)";
      nameCell.appendChild(sub);
      row.appendChild(nameCell);

      var bar = SL.el("div", "funnel-bar");
      var fill = SL.el("div", "funnel-fill");
      fill.style.width = item.rate + "%";
      if (item.rate >= 50) {
        fill.style.background = "var(--success-500)";
      } else if (item.rate >= 20) {
        fill.style.background = "var(--warning-500)";
      } else {
        fill.style.background = "var(--danger-500)";
      }
      bar.appendChild(fill);
      row.appendChild(bar);

      row.appendChild(SL.el("div", "funnel-num", item.rate.toFixed(1) + "%"));
      dom.industryList.appendChild(row);
    });
  }

  // ==========================================================================
  // 导出与分享
  // ==========================================================================

  /**
   * 导出当前结果为 CSV
   *
   * @returns {void}
   */
  function exportCsv() {
    if (!state.lastResult || !state.lastResult.rows.length) {
      SL.ui.toast.warn("还没有结果可以导出");
      return;
    }
    SL.ui.downloadCsv(
      "screener_" + state.lastResult.as_of + ".csv",
      state.lastResult.rows
    );
    SL.ui.toast.success("已导出 " + state.lastResult.rows.length + " 行");
  }

  /**
   * 复制分享链接
   *
   * @returns {void}
   */
  function shareLink() {
    if (!state.rules.length) {
      SL.ui.toast.warn("先设置筛选条件再分享");
      return;
    }
    syncUrl();
    SL.ui.copyText(window.location.href).then(function (ok) {
      if (ok) {
        SL.ui.toast.success("分享链接已复制到剪贴板");
      } else {
        SL.ui.toast.warn("复制失败，请手动复制地址栏");
      }
    });
  }

  // ==========================================================================
  // 元数据加载
  // ==========================================================================

  /**
   * 加载元数据并渲染首屏
   *
   * @param {string} asOf 指定时点，可空
   * @returns {void}
   */
  function loadMeta(asOf) {
    var url = "/api/screener/meta" + (asOf ? "?as_of=" + encodeURIComponent(asOf) : "");
    hideMessage();

    SL.fetchJson(url, 60000).then(function (data) {
      state.meta = data;

      if (data.message) {
        showMessage(data.message, "warning");
      }

      // 研究时点下拉
      dom.asOf.textContent = "";
      (data.as_of_options || []).forEach(function (date) {
        var option = SL.el("option", "", date);
        option.value = date;
        dom.asOf.appendChild(option);
      });
      if (!data.as_of_options.length) {
        var none = SL.el("option", "", "无数据");
        none.value = "";
        dom.asOf.appendChild(none);
      }
      dom.asOf.value = data.as_of || "";

      // 预置模板
      dom.preset.textContent = "";
      var custom = SL.el("option", "", "自定义条件");
      custom.value = "";
      dom.preset.appendChild(custom);
      (data.presets || []).forEach(function (preset) {
        var option = SL.el("option", "", preset.name +
          (preset.readiness === "ready" ? "" : "（需补数据源）"));
        option.value = preset.key;
        dom.preset.appendChild(option);
      });

      dom.universe.textContent = data.universe_size
        ? "股票池 " + SL.formatInt(data.universe_size) + " 只"
        : "";

      renderRules();

      // 有条件就自动跑一次（分享链接场景）
      if (state.rules.length) {
        run();
      }
    }).catch(function (error) {
      showMessage("加载因子元数据失败：" + (error.message || "请稍后重试"), "error");
    });
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
    dom.btnRun = document.getElementById("btn-run");
    dom.btnRun.addEventListener("click", run);

    document.getElementById("btn-add-rule").addEventListener("click", function () {
      if (state.rules.length >= MAX_RULES) {
        SL.ui.toast.warn("规则最多 " + MAX_RULES + " 条");
        return;
      }
      // 默认给第一个有数据的因子，减少「选了个没数据的」的概率
      var factors = (state.meta && state.meta.factors) || [];
      var firstReady = "";
      for (var i = 0; i < factors.length; i++) {
        if (factors[i].availability === "ready") {
          firstReady = factors[i].name;
          break;
        }
      }
      state.rules.push({ factor: firstReady, operator: "lt", value: "" });
      renderRules();
    });

    document.getElementById("btn-clear").addEventListener("click", function () {
      state.rules = [];
      dom.preset.value = "";
      dom.presetNote.textContent = "";
      renderRules();
      hideMessage();
      dom.resultBody.classList.add("hidden");
      dom.resultEmpty.classList.remove("hidden");
      syncUrl();
    });

    document.getElementById("btn-export").addEventListener("click", exportCsv);
    document.getElementById("btn-share").addEventListener("click", shareLink);

    dom.preset.addEventListener("change", function () {
      var key = dom.preset.value;
      if (!key) {
        dom.presetNote.textContent = "";
        return;
      }
      var presets = (state.meta && state.meta.presets) || [];
      presets.forEach(function (preset) {
        if (preset.key === key) {
          dom.presetNote.textContent = preset.note || "";
        }
      });
      loadPreset(key);
    });

    dom.asOf.addEventListener("change", function () {
      loadMeta(dom.asOf.value);
    });

    dom.chkPositive.addEventListener("change", function () {
      if (state.rules.length) {
        run();
      }
    });

    dom.sortSelect.addEventListener("change", function () {
      if (state.lastResult) {
        run();
      }
    });

    // 快捷键：R 重跑。
    // 「/ 聚焦条件区」已删除 —— 顶栏的全局证券搜索在所有页面都占着 /，
    // 而 SL.ui.shortcut 按键位覆盖、后注册者胜出，等于同一个键在选股器页
    // 指「编辑规则」、在其他页指「搜股票」。同键两义比少一个快捷键更糟。
    SL.ui.shortcut("r", function () {
      run();
    }, "重新筛选");

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

    var params = SL.ui.queryParams();
    loadMeta(params.as_of || "");
    SL.checkHealth();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
