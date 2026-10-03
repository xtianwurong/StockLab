/*
 * StockLab 组合监控页面脚本 (app/web/static/portfolio.js)
 *
 * 职责：自选股（localStorage 持久化）+ 分位阈值告警 + 卡片/表格双视图。
 *
 * 设计取舍：
 *   - 自选列表存在浏览器本地，不建服务端表：告警是「打开页面看一眼」的辅助，
 *     不是需要后台常驻的任务，落库反而要处理多用户与过期；
 *   - 数据只走已有的 /api/market/ranking（新增 codes 精确过滤参数），
 *     一次请求同时拿到 PE / PB 两套分位与全市场统计，不新增接口；
 *   - 全市场排名由「分位在可用样本中的升序位次」现算，与分位同源，不另查库。
 */

(function () {
  "use strict";

  var SL = window.SL;

  var STORAGE_KEY = "sl-watchlist";
  var SETTINGS_KEY = "sl-watchlist-alerts";

  // 告警档位（与七档评级边界对齐，用户看得懂）
  var ALERT_LEVELS = [
    { value: "", label: "不提醒" },
    { value: "10", label: "跌破 10%（极度低估）" },
    { value: "20", label: "跌破 20%（低估）" },
    { value: "40", label: "跌破 40%（正常偏低）" },
    { value: "60", label: "跌破 60%（正常）" }
  ];

  var state = {
    watch: [],        // [{code, name}]
    alerts: {},       // {code: {"pe_ttm": "20", "pb": ""}}
    rows: [],         // /api/market/ranking 的 items（两指标合并）
    summary: null,
    view: "card",
    suggestTimer: null
  };

  var dom = {};

  // ==========================================================================
  // 本地存储
  // ==========================================================================

  /**
   * 读取本地自选列表
   *
   * @returns {void}
   */
  function loadLocal() {
    try {
      var raw = window.localStorage.getItem(STORAGE_KEY);
      var list = raw ? JSON.parse(raw) : [];
      if (Array.isArray(list)) {
        state.watch = list.filter(function (item) {
          return item && item.code;
        }).map(function (item) {
          return { code: item.code, name: item.name || "" };
        });
      }
    } catch (error) {
      state.watch = [];
    }

    try {
      var rawAlerts = window.localStorage.getItem(SETTINGS_KEY);
      var alerts = rawAlerts ? JSON.parse(rawAlerts) : {};
      state.alerts = (alerts && typeof alerts === "object") ? alerts : {};
    } catch (error) {
      state.alerts = {};
    }
  }

  /**
   * 写回本地自选列表
   *
   * @returns {void}
   */
  function saveLocal() {
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(state.watch));
      window.localStorage.setItem(SETTINGS_KEY, JSON.stringify(state.alerts));
    } catch (error) {
      // 隐私模式下写入会失败：本次会话内仍可用，只是刷新后丢失
      SL.ui.toast.warn("浏览器禁用了本地存储，自选列表刷新后不会保留");
    }
  }

  /**
   * 取某标的某指标的告警档位
   *
   * @param {string} code 代码
   * @param {string} indicator 指标列名
   * @returns {string} 档位阈值文本；未设置返回空串
   */
  function alertOf(code, indicator) {
    var item = state.alerts[code];
    if (!item) {
      return "";
    }
    return item[indicator] || "";
  }

  /**
   * 判断某标的某指标是否已跌破阈值
   *
   * @param {object} row 排行行
   * @param {string} indicator 指标列名
   * @returns {boolean} 是否触发
   */
  function isAlerted(row, indicator) {
    var threshold = alertOf(row.ts_code, indicator);
    if (!threshold) {
      return false;
    }
    var value = row[indicator + "_percentile"];
    if (value === null || value === undefined) {
      return false;
    }
    return value < Number(threshold);
  }

  // ==========================================================================
  // DOM 与消息
  // ==========================================================================

  /**
   * 缓存 DOM
   *
   * @returns {void}
   */
  function cacheDom() {
    dom.input = document.getElementById("add-input");
    dom.suggest = document.getElementById("add-suggest");
    dom.alertIndicator = document.getElementById("alert-indicator");
    dom.btnRefresh = document.getElementById("btn-refresh");
    dom.btnCompare = document.getElementById("btn-compare");
    dom.btnExport = document.getElementById("btn-export");
    dom.btnClear = document.getElementById("btn-clear");
    dom.metaCount = document.getElementById("meta-count");
    dom.message = document.getElementById("message");
    dom.watchEmpty = document.getElementById("watch-empty");
    dom.watchBody = document.getElementById("watch-body");
    dom.statGrid = document.getElementById("stat-grid");
    dom.grid = document.getElementById("watch-grid");
    dom.tableCard = document.getElementById("watch-table-card");
    dom.head = document.getElementById("watch-head");
    dom.rows = document.getElementById("watch-body-rows");
    dom.viewTabs = document.getElementById("view-tabs");
  }

  /**
   * 显示消息条
   *
   * @param {string} text 文案
   * @param {string} [type] info / warning / error / success
   * @returns {void}
   */
  function showMessage(text, type) {
    dom.message.textContent = text;
    dom.message.className = "message show " + (type || "info");
  }

  /**
   * 隐藏消息条
   *
   * @returns {void}
   */
  function hideMessage() {
    dom.message.className = "message";
    dom.message.textContent = "";
  }

  /**
   * 取档位色
   *
   * @param {number|null} percentile 分位
   * @returns {string} 颜色
   */
  function levelColor(percentile) {
    if (SL.charts && SL.charts.levelColorOf) {
      return SL.charts.levelColorOf(percentile);
    }
    return SL.cssVar("--level-na", "#94a3b8");
  }

  // ==========================================================================
  // 自选增删
  // ==========================================================================

  /**
   * 加入自选
   *
   * @param {string} code 代码
   * @param {string} [name] 名称
   * @returns {void}
   */
  function addWatch(code, name) {
    for (var i = 0; i < state.watch.length; i++) {
      if (state.watch[i].code === code) {
        showMessage(code + " 已在自选列表里", "info");
        return;
      }
    }
    if (state.watch.length >= 10) {
      SL.ui.toast.warn("自选最多 10 只，请先移除一只");
      return;
    }
    state.watch.push({ code: code, name: name || "" });
    saveLocal();
    run();
  }

  /**
   * 移除自选
   *
   * @param {string} code 代码
   * @returns {void}
   */
  function removeWatch(code) {
    state.watch = state.watch.filter(function (item) {
      return item.code !== code;
    });
    delete state.alerts[code];
    saveLocal();
    run();
  }

  /**
   * 证券联想
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
            addWatch(item.code, item.name);
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
  // 取数
  // ==========================================================================

  /**
   * 拉取自选股在两项指标上的分位
   *
   * @returns {void}
   */
  function run() {
    if (!state.watch.length) {
      dom.watchEmpty.classList.remove("hidden");
      dom.watchBody.classList.add("hidden");
      dom.metaCount.textContent = "";
      return;
    }

    hideMessage();
    SL.ui.setLoading(dom.btnRefresh, true);

    var codes = state.watch.map(function (item) {
      return item.code;
    });
    var url = "/api/market/ranking?codes=" + encodeURIComponent(codes.join(",")) +
      "&limit=50&sort=percentile&order=asc";

    // PE 与 PB 各请求一次：指标维度不同，接口一次只服务一个指标
    Promise.all([
      SL.fetchJson(url.replace("sort=percentile", "indicator=pe_ttm&sort=percentile"), 20000),
      SL.fetchJson(url.replace("sort=percentile", "indicator=pb&sort=percentile"), 20000)
    ]).then(function (responses) {
      var pe = responses[0];
      var pb = responses[1];

      // 按 ts_code 合并两个指标，顺序沿用用户添加顺序
      var byCode = {};
      (pe.items || []).forEach(function (row) {
        byCode[row.ts_code] = {
          ts_code: row.ts_code,
          name: row.name,
          market: row.market,
          pe_ttm_current: row.current_value,
          pe_ttm_percentile: row.percentile,
          pe_ttm_level: row.level,
          pe_ttm_median: row.median_value,
          pe_ttm_sample: row.sample_count
        };
      });
      (pb.items || []).forEach(function (row) {
        if (!byCode[row.ts_code]) {
          return;
        }
        byCode[row.ts_code].pb_current = row.current_value;
        byCode[row.ts_code].pb_percentile = row.percentile;
        byCode[row.ts_code].pb_level = row.level;
        byCode[row.ts_code].pb_median = row.median_value;
        byCode[row.ts_code].pb_sample = row.sample_count;
      });

      state.rows = [];
      state.watch.forEach(function (item) {
        var merged = byCode[item.code] || {
          ts_code: item.code,
          name: item.name || item.code,
          market: "",
          pe_ttm_current: null,
          pe_ttm_percentile: null,
          pe_ttm_level: "-",
          pb_current: null,
          pb_percentile: null,
          pb_level: "-"
        };
        if (item.name) {
          merged.name = item.name;
        }
        state.rows.push(merged);
      });

      // 全市场分位升序 -> 名次即「比多少只更便宜」
      state.peRank = rankOf(pe.items || [], "percentile");
      state.pbRank = rankOf(pb.items || [], "percentile");
      state.peTotal = pe.summary ? pe.summary.available : 0;
      state.pbTotal = pb.summary ? pb.summary.available : 0;

      render();
      saveLocal();
    }).catch(function (error) {
      showMessage(error.message || "读取自选股数据失败", "error");
    }).finally(function () {
      SL.ui.setLoading(dom.btnRefresh, false);
    });
  }

  /**
   * 把分位升序映射成「名次 / 可用样本数」
   *
   * 【为何名次可由分位现算】
   * 分位本身就是「严格低于当前值的样本占比」，在可用样本上做升序位次，
   * 与分位互为同源；另查一次排名只会多一次全表扫描，没有额外信息。
   *
   * @param {Array<object>} items 排行项
   * @param {string} field 分位字段名
   * @returns {object} {ts_code: {rank, total}}
   */
  function rankOf(items, field) {
    var usable = items.filter(function (row) {
      return row[field] !== null && row[field] !== undefined;
    });
    usable.sort(function (a, b) {
      return a[field] - b[field];
    });
    var map = {};
    usable.forEach(function (row, index) {
      map[row.ts_code] = { rank: index + 1, total: usable.length };
    });
    return map;
  }

  // ==========================================================================
  // 渲染
  // ==========================================================================

  /**
   * 渲染整页
   *
   * @returns {void}
   */
  function render() {
    dom.watchEmpty.classList.add("hidden");
    dom.watchBody.classList.remove("hidden");
    renderMeta();
    renderStats();
    renderCards();
    renderTable();
  }

  /**
   * 渲染计数与数据来源说明
   *
   * @returns {void}
   */
  function renderMeta() {
    dom.metaCount.textContent = "自选 " + state.watch.length + " 只";
  }

  /**
   * 渲染概览统计卡
   *
   * @returns {void}
   */
  function renderStats() {
    dom.statGrid.textContent = "";

    var peAlerts = 0;
    var pbAlerts = 0;
    var peMissing = 0;
    var peValues = [];

    state.rows.forEach(function (row) {
      if (isAlerted(row, "pe_ttm")) {
        peAlerts += 1;
      }
      if (isAlerted(row, "pb")) {
        pbAlerts += 1;
      }
      if (row.pe_ttm_percentile === null || row.pe_ttm_percentile === undefined) {
        peMissing += 1;
      } else {
        peValues.push(row.pe_ttm_percentile);
      }
    });
    var peMedian = null;
    if (peValues.length) {
      peValues.sort(function (a, b) {
        return a - b;
      });
      var mid = Math.floor(peValues.length / 2);
      peMedian = peValues.length % 2
        ? peValues[mid]
        : (peValues[mid - 1] + peValues[mid]) / 2;
    }

    [
      {
        key: "自选标的",
        value: String(state.rows.length),
        sub: "本地存储，不上传",
        color: ""
      },
      {
        key: "PE 分位中位数",
        value: peMedian === null ? "-" : peMedian.toFixed(1) + "%",
        sub: peMissing ? (peMissing + " 只无可用分位") : "全部有分位",
        color: peMedian === null ? "" : levelColor(peMedian)
      },
      {
        key: "PE 告警",
        value: String(peAlerts),
        sub: peAlerts ? "已跌破设定档位" : "均在档位之上",
        color: peAlerts ? "var(--warning-600)" : "var(--success-600)"
      },
      {
        key: "PB 告警",
        value: String(pbAlerts),
        sub: pbAlerts ? "已跌破设定档位" : "均在档位之上",
        color: pbAlerts ? "var(--warning-600)" : "var(--success-600)"
      }
    ].forEach(function (item) {
      var card = SL.el("div", "stat-card");
      card.appendChild(SL.el("div", "stat-k", item.key));
      var value = SL.el("div", "stat-v", item.value);
      if (item.color) {
        value.style.color = item.color;
      }
      card.appendChild(value);
      card.appendChild(SL.el("div", "stat-sub", item.sub));
      dom.statGrid.appendChild(card);
    });
  }

  /**
   * 渲染卡片视图
   *
   * @returns {void}
   */
  function renderCards() {
    dom.grid.textContent = "";
    if (state.view !== "card") {
      return;
    }

    state.rows.forEach(function (row) {
      var peHit = isAlerted(row, "pe_ttm");
      var pbHit = isAlerted(row, "pb");
      var card = SL.el("div", "watch-card" + (peHit || pbHit ? " alerted" : ""));

      // 头部：名称 + 移除
      var head = SL.el("div", "watch-head");
      var nameBox = SL.el("div");
      nameBox.appendChild(SL.el("div", "watch-name", row.name || row.ts_code));
      var codeLine = SL.el("div", "watch-code", row.ts_code +
        (row.market ? " · " + row.market : ""));
      nameBox.appendChild(codeLine);
      head.appendChild(nameBox);

      var close = SL.el("button", "watch-close", "×");
      close.type = "button";
      close.setAttribute("aria-label", "移除 " + (row.name || row.ts_code));
      close.addEventListener("click", function () {
        removeWatch(row.ts_code);
      });
      head.appendChild(close);
      card.appendChild(head);

      // 两项指标
      var metrics = SL.el("div", "watch-metrics");
      metrics.appendChild(buildMetricCell(row, "pe_ttm", "PE-TTM"));
      metrics.appendChild(buildMetricCell(row, "pb", "PB"));
      card.appendChild(metrics);

      // 告警设置
      card.appendChild(buildAlertBox(row));

      // 底部：去个股页 / 加入对比
      var foot = SL.el("div", "watch-foot");
      var toDetail = SL.el("button", "btn btn-sm btn-ghost", "个股详情");
      toDetail.type = "button";
      toDetail.addEventListener("click", function () {
        window.location.href = "/?code=" + encodeURIComponent(row.ts_code);
      });
      foot.appendChild(toDetail);

      var toCompare = SL.el("button", "btn btn-sm btn-ghost", "加入对比");
      toCompare.type = "button";
      toCompare.addEventListener("click", function () {
        window.location.href = "/compare?codes=" + encodeURIComponent(row.ts_code);
      });
      foot.appendChild(toCompare);
      card.appendChild(foot);

      dom.grid.appendChild(card);
    });
  }

  /**
   * 构造一个指标格（当前值 + 分位 + 评级）
   *
   * @param {object} row 排行行
   * @param {string} indicator 指标前缀 pe_ttm / pb
   * @param {string} label 指标中文名
   * @returns {HTMLElement} 指标格
   */
  function buildMetricCell(row, indicator, label) {
    var percentile = row[indicator + "_percentile"];
    var cell = SL.el("div");

    var head = SL.el("div", "watch-mrow");
    head.appendChild(SL.el("div", "watch-mk", label));
    var rank = indicator === "pe_ttm"
      ? (state.peRank || {})[row.ts_code]
      : (state.pbRank || {})[row.ts_code];
    if (rank && rank.total) {
      var total = indicator === "pe_ttm" ? (state.peTotal || rank.total)
        : (state.pbTotal || rank.total);
      head.appendChild(SL.el("span", "watch-rank",
        "全市场 " + rank.rank + "/" + total));
    }
    cell.appendChild(head);

    var value = SL.el("div", "watch-mv", SL.formatNumber(row[indicator + "_current"], 2));
    value.style.color = levelColor(percentile);
    cell.appendChild(value);

    var badgeRow = SL.el("div", "row row-tight");
    badgeRow.style.marginTop = "var(--space-1)";
    badgeRow.innerHTML = SL.levelBadge(percentile, row[indicator + "_level"]);
    cell.appendChild(badgeRow);

    return cell;
  }

  /**
   * 构造告警设置条
   *
   * @param {object} row 排行行
   * @returns {HTMLElement} 告警条
   */
  function buildAlertBox(row) {
    var indicator = dom.alertIndicator.value;
    var threshold = alertOf(row.ts_code, indicator);
    var hit = isAlerted(row, indicator);
    var box = SL.el("div", "watch-alert" + (hit ? " hit" : ""));

    var label = SL.el("span", "watch-mk", "提醒档位");
    box.appendChild(label);

    var select = SL.el("select", "select");
    select.setAttribute("aria-label", (row.name || row.ts_code) + " 的告警档位");
    ALERT_LEVELS.forEach(function (level) {
      var option = SL.el("option", "", level.label);
      option.value = level.value;
      select.appendChild(option);
    });
    select.value = threshold;
    select.addEventListener("change", function () {
      if (!state.alerts[row.ts_code]) {
        state.alerts[row.ts_code] = {};
      }
      state.alerts[row.ts_code][indicator] = select.value;
      saveLocal();
      render();
    });
    box.appendChild(select);

    var stateText = SL.el("span", "watch-alert-state",
      hit ? "已跌破 " + threshold + "%" : (threshold ? "未触发" : "未设置"));
    stateText.style.color = hit
      ? "var(--warning-700)"
      : (threshold ? "var(--text-muted)" : "var(--text-faint)");
    box.appendChild(stateText);

    return box;
  }

  /**
   * 渲染表格视图
   *
   * @returns {void}
   */
  function renderTable() {
    if (state.view !== "table") {
      return;
    }

    dom.head.textContent = "";
    ["标的", "市场", "PE-TTM", "PE 分位", "PB", "PB 分位", "告警"].forEach(function (label) {
      dom.head.appendChild(SL.el("th", label === "标的" || label === "市场" || label === "告警" ? "" : "ta-r",
        label));
    });

    dom.rows.textContent = "";
    state.rows.forEach(function (row) {
      var peHit = isAlerted(row, "pe_ttm");
      var pbHit = isAlerted(row, "pb");
      var tr = SL.el("tr");

      var nameCell = SL.el("td");
      var box = SL.el("div", "name-cell");
      var dot = SL.el("span", "alert-dot" + (peHit || pbHit ? " hit" : ""));
      box.appendChild(dot);
      box.appendChild(SL.el("span", "", row.name || row.ts_code));
      box.appendChild(SL.el("span", "watch-code", row.ts_code));
      nameCell.appendChild(box);
      tr.appendChild(nameCell);

      tr.appendChild(SL.el("td", "", row.market || "-"));

      [
        ["pe_ttm_current", "pe_ttm_percentile"],
        ["pb_current", "pb_percentile"]
      ].forEach(function (pair) {
        var valueCell = SL.el("td", "ta-r");
        var value = SL.el("span", "mono", SL.formatNumber(row[pair[0]], 2));
        value.style.fontWeight = "600";
        value.style.color = levelColor(row[pair[1]]);
        valueCell.appendChild(value);
        tr.appendChild(valueCell);

        var pctCell = SL.el("td", "ta-r");
        var wrap = SL.el("div", "cell-v");
        wrap.appendChild(SL.el("span", "mono", SL.formatPercent(row[pair[1]])));
        wrap.appendChild(SL.el("span", "cell-p", row[pair[0].replace("_current", "") + "_level"] || "-"));
        pctCell.appendChild(wrap);
        tr.appendChild(pctCell);
      });

      var alertCell = SL.el("td");
      alertCell.innerHTML = (peHit || pbHit)
        ? '<span class="badge badge-warning">已触发</span>'
        : '<span class="badge badge-neutral">正常</span>';
      tr.appendChild(alertCell);

      tr.className = "row-link";
      tr.addEventListener("click", function () {
        window.location.href = "/?code=" + encodeURIComponent(row.ts_code);
      });
      dom.rows.appendChild(tr);
    });
  }

  /**
   * 切换视图
   *
   * @param {string} view card / table
   * @returns {void}
   */
  function switchView(view) {
    state.view = view;
    var tabs = SL.ui.findAll("[data-view]", dom.viewTabs);
    tabs.forEach(function (tab) {
      tab.className = "tab" + (tab.getAttribute("data-view") === view ? " active" : "");
    });
    if (view === "card") {
      dom.grid.classList.remove("hidden");
      dom.tableCard.classList.add("hidden");
    } else {
      dom.grid.classList.add("hidden");
      dom.tableCard.classList.remove("hidden");
    }
    renderCards();
    renderTable();
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
    dom.btnRefresh.addEventListener("click", run);

    dom.btnClear.addEventListener("click", function () {
      if (!state.watch.length) {
        return;
      }
      SL.ui.confirm("确定清空全部自选股？", {
        title: "清空自选",
        okLabel: "清空",
        danger: true
      }).then(function (ok) {
        if (!ok) {
          return;
        }
        state.watch = [];
        state.alerts = {};
        saveLocal();
        run();
        SL.ui.toast.success("自选列表已清空");
      });
    });

    dom.btnCompare.addEventListener("click", function () {
      if (state.watch.length < 2) {
        SL.ui.toast.warn("至少 2 只标的才能进对比页");
        return;
      }
      var codes = state.watch.map(function (item) {
        return item.code;
      });
      window.location.href = "/compare?codes=" + encodeURIComponent(codes.join(","));
    });

    dom.btnExport.addEventListener("click", function () {
      if (!state.rows.length) {
        SL.ui.toast.warn("还没有自选股数据可导出");
        return;
      }
      SL.ui.downloadCsv(
        "watchlist_pe_pb.csv",
        state.rows.map(function (row) {
          return {
            ts_code: row.ts_code,
            name: row.name,
            market: row.market,
            pe_ttm: row.pe_ttm_current,
            pe_percentile: row.pe_ttm_percentile,
            pe_level: row.pe_ttm_level,
            pb: row.pb_current,
            pb_percentile: row.pb_percentile,
            pb_level: row.pb_level,
            pe_alert: isAlerted(row, "pe_ttm") ? "触发" : "正常",
            pb_alert: isAlerted(row, "pb") ? "触发" : "正常"
          };
        })
      );
      SL.ui.toast.success("已导出 " + state.rows.length + " 行");
    });

    dom.alertIndicator.addEventListener("change", function () {
      renderCards();
      renderTable();
    });

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
      if (/^[0-9]{6}(\.(SH|SZ|BJ))?$/i.test(value)) {
        addWatch(value.toUpperCase().indexOf(".") === -1 ? value : value.toUpperCase(), "");
        dom.input.value = "";
        dom.suggest.className = "suggest";
      } else {
        fetchSuggest(value);
      }
    });

    var tabs = SL.ui.findAll("[data-view]", dom.viewTabs);
    tabs.forEach(function (tab) {
      tab.addEventListener("click", function () {
        switchView(tab.getAttribute("data-view"));
      });
    });

    document.addEventListener("click", function (event) {
      if (!dom.suggest.contains(event.target) && event.target !== dom.input) {
        dom.suggest.className = "suggest";
      }
    });

    SL.ui.shortcut("r", function () {
      run();
    }, "刷新自选股");

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
    loadLocal();
    run();
    SL.checkHealth();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
