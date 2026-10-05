/*
 * StockLab 大宗商品页前端逻辑 (app/web/static/commodities.js)
 *
 * 职责：
 *   1. 加载品种目录（/api/commodities），按分类渲染卡片与明细表
 *   2. 点击卡片加载单品种趋势（/api/commodity/history），渲染价格走势与分位参考线
 *   3. 概览条统计（覆盖品种 / 数据截止 / 有效评级 / 分位窗口）
 *
 * 两条不可让渡的展示约束（与 commodity_api.py 对应）：
 *   - 数据陈旧（stale）时**不显示评级**，改为显式提示。拿四年前的价格
 *     显示一个「当前分位」，数字不会报错，只会误导。
 *   - 涨跌用红涨绿跌（--rise/--fall），估值用七档色（绿便宜→红贵），
 *     两套方向相反，绝不能出现在同一个数上。
 *
 * 说明：本文件为浏览器端脚本，与 Python 侧无共享约束，保持原生 JS 直白写法。
 */

(function () {
  "use strict";

  // ---------- DOM ----------
  var groupsEl = document.getElementById("cm-groups");
  var emptyEl = document.getElementById("cm-empty");
  var messageBox = document.getElementById("message");
  var detailCard = document.getElementById("detail-card");
  var chartDom = document.getElementById("commodity-chart");
  var btnReload = document.getElementById("btn-reload");

  // ---------- 状态 ----------
  var items = [];
  var selected = null;
  var detailChart = null;
  var busy = false;
  var meta = {};
  var windowYears = 5;

  // ---------- 展示口径 ----------

  /**
   * 显示消息条
   *
   * @param {string} text 文本
   * @param {string} kind info | error
   */
  function showMessage(text, kind) {
    SL.showMessage(messageBox, text, kind);
  }

  /**
   * 清空消息条
   */
  function clearMessage() {
    SL.clearMessage(messageBox);
  }

  /**
   * 涨跌幅格式化（带显式正负号）
   *
   * 不复用 formatNumber：它会去掉尾零且不补正号，涨 0.5% 会显示成 "0.5"
   * —— 涨跌语境下「没有正号」读起来像没变化。
   *
   * @param {number|null} value 百分比数值
   * @returns {string}
   */
  function formatChange(value) {
    if (value === null || value === undefined || value !== value) {
      return "-";
    }
    if (Math.abs(value) < 0.005) {
      return "0.00%";
    }
    return (value > 0 ? "+" : "") + value.toFixed(2) + "%";
  }

  /**
   * 涨跌语义色类名（红涨绿跌，A 股口径）
   *
   * @param {number|null} value 百分比数值
   * @returns {string}
   */
  function changeClass(value) {
    if (value === null || value === undefined || value !== value) {
      return "";
    }
    if (value > 0) {
      return "chg-up";
    }
    if (value < 0) {
      return "chg-down";
    }
    return "chg-flat";
  }

  /**
   * 分位行文案；无分位时如实说原因，而不是留白
   *
   * 顺序很关键：先判「本地无数据」再判陈旧 —— 从未同步过的品种
   * 也是 stale=True（lag_days=None → 门面按无数据处理），
   * 若先判陈旧会把它说成「已停更」，把「还没同步过」误报成「同步中断」。
   *
   * @param {object} item 品种项
   * @param {number} windowYears 分位窗口（年）
   * @returns {string}
   */
  function percentileText(item, windowYears) {
    if (item.close === null || item.close === undefined) {
      return "本地无数据";
    }
    if (item.stale) {
      return "数据已停更，不给评级";
    }
    if (item.percentile === null || item.percentile === undefined) {
      return "样本不足，不给评级";
    }
    return "价格分位 " + SL.formatPercent(item.percentile) +
      " · 近 " + windowYears + " 年";
  }

  // ---------- 加载 ----------

  /**
   * 加载品种目录
   */
  function load() {
    if (busy) {
      return;
    }
    busy = true;
    btnReload.disabled = true;
    clearMessage();

    SL.fetchJson("/api/commodities", 30000)
      .then(function (data) {
        items = data.items || [];
        windowYears = data.window_years || 5;
        if (data.message) {
          showMessage(data.message, "info");
        }
        renderStats(data);
        renderGroups();
        renderTable();
      })
      .catch(function (error) {
        items = [];
        renderGroups();
        renderTable();
        showMessage(error.message || "加载失败，请稍后重试", "error");
      })
      .finally(function () {
        busy = false;
        btnReload.disabled = false;
      });
  }

  // ---------- 渲染 ----------

  /**
   * 概览条
   *
   * @param {object} data 目录应答
   */
  function renderStats(data) {
    var withData = items.filter(function (item) {
      return item.close !== null && item.close !== undefined;
    });
    var rated = items.filter(function (item) {
      return item.percentile !== null && item.percentile !== undefined;
    });

    document.getElementById("stat-count").textContent = withData.length + "/" + items.length;
    document.getElementById("stat-asof").textContent = data.as_of || "-";
    // 分母用「有数据的品种数」而不是登记品种数 —— 一个从未同步过的品种
    // 出现在分母里会把「评级覆盖率」这个数说谎
    document.getElementById("stat-rated").textContent =
      rated.length + (withData.length ? "/" + withData.length : "");
    document.getElementById("stat-window").textContent = (data.window_years || 5) + " 年";
  }

  /**
   * 按分类分组渲染卡片（items 已按登记顺序排好，同分类连续出现）
   */
  function renderGroups() {
    groupsEl.textContent = "";
    var hasAny = false;
    var index = 0;

    while (index < items.length) {
      var category = items[index].category;
      var run = [];
      while (index < items.length && items[index].category === category) {
        run.push(items[index]);
        index++;
      }

      var head = SL.el("div", "cm-group-head");
      head.appendChild(SL.el("span", "cm-group-title", category));
      head.appendChild(SL.el("span", "cm-group-count", run.length + " 个品种"));
      groupsEl.appendChild(head);

      var grid = SL.el("div", "cm-grid");
      run.forEach(function (item) {
        grid.appendChild(buildCard(item));
        hasAny = true;
      });
      groupsEl.appendChild(grid);
    }

    emptyEl.classList.toggle("hidden", hasAny);
  }

  /**
   * 单个品种卡片
   *
   * @param {object} item 品种项
   * @returns {HTMLElement}
   */
  function buildCard(item) {
    var card = SL.el("div", "cm-card" + (selected === item.symbol ? " active" : ""));

    var top = SL.el("div", "cm-top");
    var titleBox = SL.el("div");
    titleBox.appendChild(SL.el("div", "cm-name", item.name));
    titleBox.appendChild(SL.el("div", "cm-meta",
      item.source_symbol + " · " + item.exchange));
    top.appendChild(titleBox);
    var badge = SL.el("div");
    badge.innerHTML = SL.levelBadge(item.percentile, item.stale ? "数据陈旧" : item.level7);
    top.appendChild(badge);
    card.appendChild(top);

    var price = SL.el("div", "cm-price", SL.formatNumber(item.close, 2));
    price.appendChild(SL.el("span", "cm-unit", item.unit));
    card.appendChild(price);

    card.appendChild(SL.el("div", "cm-pct", percentileText(item, windowYears)));

    var temp = SL.el("div", "cm-temp");
    temp.innerHTML = SL.tempbar(item.percentile);
    card.appendChild(temp);

    var changes = SL.el("div", "cm-changes");
    [["1 月", item.changes["1m"]],
     ["3 月", item.changes["3m"]],
     ["12 月", item.changes["12m"]]].forEach(function (pair) {
      var cell = SL.el("div");
      cell.appendChild(document.createTextNode(pair[0]));
      var value = SL.el("b", changeClass(pair[1]), formatChange(pair[1]));
      cell.appendChild(value);
      changes.appendChild(cell);
    });
    card.appendChild(changes);

    if (item.stale) {
      card.appendChild(SL.el("div", "cm-stale-note",
        "已 " + item.lag_days + " 天无新数据（末次 " + item.trade_date + "），" +
        "评级已隐藏 —— 用旧价格给出的「当前分位」会误导判断。"));
    }

    var foot = SL.el("div", "cm-foot");
    foot.appendChild(SL.el("span", "", "样本 " + SL.formatInt(item.sample_count)));
    foot.appendChild(SL.el("span", "", item.trade_date || "-"));
    card.appendChild(foot);

    card.addEventListener("click", function () {
      if (selected === item.symbol) {
        return;
      }
      selected = item.symbol;
      renderGroups();
      loadDetail(item.symbol);
    });

    return card;
  }

  /**
   * 明细表
   */
  function renderTable() {
    var body = document.getElementById("cm-body");
    body.textContent = "";

    items.forEach(function (item) {
      var row = SL.el("tr");

      var nameCell = SL.el("td");
      nameCell.appendChild(document.createTextNode(item.name));
      nameCell.appendChild(SL.el("span", "sub", item.source_symbol + " · " + item.unit));
      row.appendChild(nameCell);

      row.appendChild(SL.el("td", "", item.category));
      row.appendChild(SL.el("td", "num", SL.formatNumber(item.close, 2)));

      [["1m"], ["3m"], ["12m"]].forEach(function (key) {
        var value = item.changes[key[0]];
        row.appendChild(SL.el("td", "num " + changeClass(value), formatChange(value)));
      });

      row.appendChild(SL.el("td", "num", SL.formatPercent(item.percentile)));

      var badgeCell = SL.el("td");
      badgeCell.innerHTML = SL.levelBadge(item.percentile, item.stale ? "数据陈旧" : item.level7);
      row.appendChild(badgeCell);

      row.appendChild(SL.el("td", "num", SL.formatInt(item.sample_count)));

      var dateCell = SL.el("td", "num", item.trade_date || "-");
      if (item.lag_days !== null && item.lag_days !== undefined) {
        dateCell.appendChild(SL.el("span", "sub", item.lag_days + " 天前"));
      }
      row.appendChild(dateCell);

      body.appendChild(row);
    });
  }

  // ---------- 趋势 ----------

  /**
   * 加载单品种趋势
   *
   * @param {string} symbol 品种代号
   */
  function loadDetail(symbol) {
    clearMessage();

    SL.fetchJson(
      "/api/commodity/history?symbol=" + encodeURIComponent(symbol), 30000
    )
      .then(function (data) {
        if (!data.series || !data.series.length) {
          detailCard.classList.add("hidden");
          showMessage(data.message || "该品种暂无价格序列", "info");
          return;
        }
        renderDetail(data);
      })
      .catch(function (error) {
        detailCard.classList.add("hidden");
        showMessage(error.message || "加载趋势失败", "error");
      });
  }

  /**
   * 渲染趋势面板
   *
   * @param {object} data 趋势应答
   */
  function renderDetail(data) {
    detailCard.classList.remove("hidden");

    document.getElementById("detail-name").textContent =
      data.name + "（" + data.source_symbol + "）";
    document.getElementById("detail-desc").textContent = data.description || "";
    document.getElementById("detail-unit-note").textContent =
      data.unit_note ? "计价：" + data.unit + " · " + data.unit_note : "";

    var facts = document.getElementById("detail-facts");
    facts.textContent = "";
    [["当前", SL.formatNumber(data.close, 2) + " " + data.unit],
     ["价格分位", data.stale ? "数据陈旧（隐藏）" : SL.formatPercent(data.percentile)],
     ["区间中位", SL.formatNumber(data.median_value, 2)],
     ["区间", data.interval_text || "-"],
     ["样本", SL.formatInt(data.sample_count)]].forEach(function (pair) {
      var cell = SL.el("span");
      cell.appendChild(document.createTextNode(pair[0]));
      cell.appendChild(SL.el("b", "", pair[1]));
      facts.appendChild(cell);
    });

    if (data.stale) {
      showMessage(
        "该品种已 " + data.lag_days + " 天无新数据，评级已隐藏。请先运行同步。", "info"
      );
    }

    var dates = [];
    var values = [];
    data.series.forEach(function (point) {
      dates.push(point.date);
      values.push(point.close);
    });

    var history = { dates: dates, series: {} };
    history.series[data.name] = values;
    meta = {};
    meta[data.name] = data.name;

    // 重建函数：主题切换时由 charts.js 回调，用当前主题重新取色。
    // singleAxis 必须显式给：默认逻辑会把任何非 PE 序列放到右侧副轴，
    // 单一价格序列被摆到副轴会得到一条空的左轴。
    var rebuild = function () {
      return SL.buildTrendOption({
        history: history,
        indicatorMeta: meta,
        primary: data.name,
        singleAxis: true
      });
    };
    detailChart = SL.renderChart(chartDom, rebuild(), rebuild);

    // 滚动到详情区，避免大屏下点了卡片却看不到变化
    if (detailCard.scrollIntoView) {
      detailCard.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }

  // ---------- 事件 ----------
  btnReload.addEventListener("click", function () {
    load();
  });

  window.addEventListener("resize", function () {
    if (detailChart) {
      detailChart.resize();
    }
  });

  var legendBox = document.getElementById("level-legend");
  if (legendBox) {
    legendBox.innerHTML = SL.levelLegend();
  }

  SL.checkHealth();
  load();
})();
