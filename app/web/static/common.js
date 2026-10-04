/*
 * StockLab 前端共享工具 (app/web/static/common.js)
 *
 * 职责：三个页面共用的工具集 ——
 *   1. fetchJson：带超时与统一错误处理的请求（修复「请求卡住按钮永远转圈」）
 *   2. 数值格式化 / 七档评级与配色
 *   3. 温度条（0-100 分位可视化）渲染
 *   4. ECharts 走势图 option 工厂：双 y 轴 + 图例切换 + 分位参考线 + 分位带
 *
 * 说明：本文件为浏览器端脚本，与 Python 侧无共享约束，保持原生 JS 直白写法。
 */

(function () {
  "use strict";

  // ---------- 常量 ----------
  // 七档评级：与 app/web/store.py 的 percentile_level() 必须保持一致
  // color 写成 var(--level-N)：由 tokens.css 决定亮/暗两套具体色值，
  // 徽章与图例渲染时经 cssColor() 解析为当前主题的颜色（见 charts.js）。
  var LEVEL7 = [
    { name: "极度低估", max: 10, color: "var(--level-1)" },
    { name: "低估", max: 20, color: "var(--level-2)" },
    { name: "正常偏低", max: 40, color: "var(--level-3)" },
    { name: "正常", max: 60, color: "var(--level-4)" },
    { name: "正常偏高", max: 80, color: "var(--level-5)" },
    { name: "高估", max: 90, color: "var(--level-6)" },
    { name: "极度高估", max: 100, color: "var(--level-7)" }
  ];

  var LEVEL_COLOR_NA = "var(--level-na)";

  // PE 类指标走左轴，其余（PB/PS/PCF）走右轴：量纲差异大，混轴会互相压扁
  var PE_INDICATORS = ["pe_ttm", "pe_static"];

  var LEVEL_NAME_COLOR = {};
  for (var i = 0; i < LEVEL7.length; i++) {
    LEVEL_NAME_COLOR[LEVEL7[i].name] = LEVEL7[i].color;
  }

  // ---------- 请求 ----------

  /**
   * 发起 JSON 请求（带超时、自动解析、统一错误信息）
   *
   * @param {string} url 请求地址
   * @param {number} [timeoutMs] 超时毫秒，默认 15000
   * @param {object} [options] fetch 选项扩展（method / headers / body），
   *                            用于 POST JSON 这类带请求体的接口
   * @returns {Promise<object>} 解析后的 JSON
   */
  function fetchJson(url, timeoutMs, options) {
    var controller = new AbortController();
    var timer = setTimeout(function () {
      controller.abort();
    }, timeoutMs || 15000);

    var init = { signal: controller.signal };
    if (options) {
      if (options.method) {
        init.method = options.method;
      }
      if (options.headers) {
        init.headers = options.headers;
      }
      if (options.body) {
        init.body = options.body;
      }
    }

    return fetch(url, init)
      .then(function (response) {
        return response.json().catch(function () {
          return {};
        }).then(function (data) {
          if (!response.ok) {
            var error = new Error(data.error || ("请求失败 (HTTP " + response.status + ")"));
            error.status = response.status;
            throw error;
          }
          return data;
        });
      })
      .finally(function () {
        clearTimeout(timer);
      });
  }

  // ---------- 格式化 ----------

  /**
   * 数值格式化：按量级选择小数位，负数与千分位一并处理
   *
   * @param {number|null} value 数值
   * @param {number} digits 指定小数位；缺省时按量级自适应
   * @returns {string} 展示文本
   */
  function formatNumber(value, digits) {
    if (value === null || value === undefined || value !== value) {
      return "-";
    }
    var abs = Math.abs(value);
    var places;
    if (digits !== undefined && digits !== null) {
      places = digits;
    } else if (abs >= 1000) {
      places = 0;
    } else if (abs >= 100) {
      places = 1;
    } else if (abs >= 1) {
      places = 2;
    } else {
      places = 4;
    }
    var text = value.toFixed(places);
    // 去掉 12.00 这类无意义的尾零（但保留 12.00 -> 12）
    if (places > 0 && text.indexOf(".") !== -1) {
      text = text.replace(/0+$/, "").replace(/\.$/, "");
    }
    var parts = text.split(".");
    parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    return parts.join(".");
  }

  /**
   * 分位数格式化（百分比，一位小数）
   *
   * @param {number|null} percentile 0~100
   * @returns {string} 如 19.4%
   */
  function formatPercent(percentile) {
    if (percentile === null || percentile === undefined || percentile !== percentile) {
      return "-";
    }
    return percentile.toFixed(1) + "%";
  }

  /**
   * 整数格式化（带千分位）
   *
   * @param {number} value 数值
   * @returns {string} 如 5,543
   */
  function formatInt(value) {
    if (value === null || value === undefined || value !== value) {
      return "-";
    }
    return String(Math.round(value)).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }

  // ---------- 评级 ----------

  /**
   * 把分位数映射为七档评级（与后端 store.percentile_level 同口径）
   *
   * @param {number|null} percentile 0~100
   * @returns {object|null} {name, color}；不可用返回 null
   */
  function levelOf(percentile) {
    if (percentile === null || percentile === undefined || percentile !== percentile) {
      return null;
    }
    for (var i = 0; i < LEVEL7.length; i++) {
      if (percentile < LEVEL7[i].max) {
        return LEVEL7[i];
      }
    }
    // 分位恰为 100 时（末档是闭区间）落到最后一档，与后端一致
    return LEVEL7[LEVEL7.length - 1];
  }

  /**
   * 评级徽章 HTML（值不可用时返回灰色「不可用」）
   *
   * @param {number|null} percentile 分位数
   * @param {string} levelName 后端给定的档位名（可省略，内部重新推导）
   * @returns {string} HTML 字符串
   */
  function levelBadge(percentile, levelName) {
    var level = levelOf(percentile);
    var name = level ? level.name : (levelName || "不可用");
    var color = level ? cssColor(level.color) : cssColor(LEVEL_COLOR_NA, "#94a3b8");
    return (
      '<span class="badge" style="background:' + hexA(color, 0.12) +
      ";color:" + color + '"><span class="badge-dot"></span>' +
      escapeHtml(name) + "</span>"
    );
  }

  /**
   * 把 #rrggbb 转成带透明度的 rgba
   *
   * @param {string} hex 颜色
   * @param {number} alpha 透明度 0~1
   * @returns {string} rgba(...) 文本
   */
  function hexA(hex, alpha) {
    var value = hex.replace("#", "");
    if (value.length === 3) {
      value = value[0] + value[0] + value[1] + value[1] + value[2] + value[2];
    }
    var r = parseInt(value.substring(0, 2), 16);
    var g = parseInt(value.substring(2, 4), 16);
    var b = parseInt(value.substring(4, 6), 16);
    return "rgba(" + r + "," + g + "," + b + "," + alpha + ")";
  }

  /**
   * 解析主题色：接受 var(--level-1) 这类变量引用与 #hex 字面量
   *
   * 【为何需要】评级色搬进了 tokens.css（亮暗两套），徽章要叠加透明度，
   *   必须先拿到当前主题下的具体颜色；charts.js 提供实现，此处做降级兜底。
   *
   * @param {string} color 颜色文本
   * @param {string} [fallback] 解析失败时的兜底
   * @returns {string} 具体颜色
   */
  function cssColor(color, fallback) {
    if (window.SL && window.SL.charts && window.SL.charts.cssColor) {
      return window.SL.charts.cssColor(color, fallback);
    }
    return color || fallback || "";
  }

  /**
   * 当前主题的图表调色板（charts.js 提供；缺省时给出亮色兜底值）
   *
   * 【为何图表颜色要读变量】暗色主题下 ECharts 的轴线/文字若仍用写死的浅色，
   *   会在深色卡片上几乎不可见；统一从 tokens.css 取值才能自动跟随主题。
   *
   * @returns {object} 颜色集合
   */
  function palette() {
    if (window.SL && window.SL.charts && window.SL.charts.palette) {
      return window.SL.charts.palette();
    }
    return {
      textMain: "#0f172a", textSub: "#475569", textMuted: "#64748b", textFaint: "#94a3b8",
      axis: "#cbd5e1", axisLabel: "#64748b", split: "#eef2f7", accent: "#2563eb",
      tooltipBg: "#ffffff", tooltipText: "#0f172a", tooltipBorder: "#e2e8f0",
      band: "rgba(100,116,139,0.09)", zoomFiller: "rgba(37,99,235,0.12)",
      gridBg: "#f8fafc", border: "#e2e8f0", danger: "#dc2626", success: "#16a34a"
    };
  }

  /**
   * 生成温度条 HTML（0-100 分位可视化，指针落在当前分位处）
   *
   * @param {number|null} percentile 分位数
   * @returns {string} HTML 字符串
   */
  function tempbar(percentile) {
    var usable = percentile !== null && percentile !== undefined && percentile === percentile;
    var position = usable ? Math.max(0, Math.min(100, percentile)) : 50;
    return (
      '<div class="tempbar' + (usable ? "" : " tempbar-na") + '">' +
      '<div class="tempbar-track">' +
      '<div class="tempbar-thumb" style="left:' + position + '%"></div>' +
      "</div>" +
      '<div class="tempbar-scale"><span>低估</span><span>适中</span><span>高估</span></div>' +
      "</div>"
    );
  }

  // ---------- 统计量 ----------

  /**
   * 从数值数组求分位数（线性插值，供图上参考线使用）
   *
   * @param {Array<number>} values 数值数组（可含 null，内部剔除）
   * @param {number} q 0~1
   * @returns {number|null} 分位值
   */
  function quantile(values, q) {
    var clean = [];
    for (var i = 0; i < values.length; i++) {
      var v = values[i];
      if (v !== null && v !== undefined && v === v) {
        clean.push(v);
      }
    }
    if (!clean.length) {
      return null;
    }
    clean.sort(function (a, b) { return a - b; });
    var pos = (clean.length - 1) * q;
    var base = Math.floor(pos);
    var rest = pos - base;
    if (clean[base + 1] !== undefined) {
      return clean[base] + rest * (clean[base + 1] - clean[base]);
    }
    return clean[base];
  }

  /**
   * 取数组中最后一个非空值
   *
   * @param {Array<number>} values 数值数组
   * @returns {number|null} 最新有效值
   */
  function lastValid(values) {
    for (var i = values.length - 1; i >= 0; i--) {
      var v = values[i];
      if (v !== null && v !== undefined && v === v) {
        return v;
      }
    }
    return null;
  }

  // ---------- 七档图例 ----------

  /**
   * 生成七档评级图例 HTML（各页统一口径展示）
   *
   * @returns {string} HTML 字符串
   */
  function levelLegend() {
    var html = '<div class="legend">';
    var low = 0;
    for (var i = 0; i < LEVEL7.length; i++) {
      var item = LEVEL7[i];
      html +=
        '<span class="legend-item"><span class="legend-swatch" style="background:' +
        cssColor(item.color) + '"></span>' + item.name +
        '<span class="legend-range">' + low + "~" + item.max + "%</span></span>";
      low = item.max;
    }
    return html + "</div>";
  }

  // ---------- 顶栏状态 ----------

  /**
   * 拉取健康检查，点亮顶栏状态并填充数据截止日期
   *
   * @returns {Promise<object|null>} 健康检查应答；失败返回 null
   */
  function checkHealth() {
    var dot = document.getElementById("status-dot");
    var text = document.getElementById("status-text");
    return fetchJson("/api/health", 6000)
      .then(function (data) {
        if (dot) {
          dot.className = "status-dot ok";
        }
        if (text) {
          text.textContent = "服务正常 · " + data.priority;
        }
        var asOf = data.data_as_of || "";
        var strip = document.getElementById("data-as-of");
        if (strip) {
          if (asOf) {
            strip.textContent = "数据截止 " + asOf;
            strip.className = "";
          }
        }
        var footer = document.getElementById("footer-as-of");
        if (footer && asOf) {
          footer.textContent = "数据截止 " + asOf;
        }
        return data;
      })
      .catch(function () {
        if (dot) {
          dot.className = "status-dot bad";
        }
        if (text) {
          text.textContent = "服务不可用";
        }
        return null;
      });
  }

  // ---------- ECharts 走势图 ----------

  /**
   * 构造走势图 option：双 y 轴（PE 类 / 其他）+ 图例切换 + 分位参考线 + 分位带
   *
   * 【双轴缘由】PE 常在 10~100 量级而 PB 多在 1~10、PCF 可到 200+，
   *              混在同一坐标轴会让低量纲曲线被压成直线，无法辨识走势。
   *
   * @param {object} args 参数集
   *   history     {dates, series}  后端 history 字段
   *   indicatorMeta {指标: {label}} 指标中文名映射
   *   primary     {string} 主指标（画参考线与分位带的那一个）
   * @returns {object} ECharts option
   */
  function buildTrendOption(args) {
    var history = args.history;
    var meta = args.indicatorMeta || {};
    var primary = args.primary;
    var color = palette();

    var dates = history.dates;
    var seriesList = [];
    var yAxis = [];
    var leftIndex = 0;
    var rightIndex = -1;

    var keys = Object.keys(history.series);
    var hasRight = false;
    for (var k = 0; k < keys.length; k++) {
      if (PE_INDICATORS.indexOf(keys[k]) === -1) {
        hasRight = true;
        break;
      }
    }
    if (hasRight) {
      rightIndex = 1;
      yAxis.push(
        { type: "value", name: "PE 类", scale: true, position: "left",
          axisLabel: { color: color.axisLabel, fontSize: 11 },
          axisLine: { lineStyle: { color: color.axis } },
          splitLine: { lineStyle: { color: color.split } },
          nameTextStyle: { color: color.textFaint, fontSize: 11 } },
        { type: "value", name: "PB / PS / PCF", scale: true, position: "right",
          axisLabel: { color: color.axisLabel, fontSize: 11 },
          axisLine: { lineStyle: { color: color.axis } },
          splitLine: { show: false },
          nameTextStyle: { color: color.textFaint, fontSize: 11 } }
      );
    } else {
      yAxis.push(
        { type: "value", scale: true,
          axisLabel: { color: color.axisLabel, fontSize: 11 },
          axisLine: { lineStyle: { color: color.axis } },
          splitLine: { lineStyle: { color: color.split } } }
      );
    }

    var legendData = [];
    for (var i = 0; i < keys.length; i++) {
      var indicator = keys[i];
      var values = history.series[indicator];
      var isPrimary = indicator === primary;
      var axisIndex = hasRight ? (PE_INDICATORS.indexOf(indicator) === -1 ? 1 : 0) : 0;

      var extra = {};
      if (isPrimary) {
        extra = buildPrimaryMarks(values);
      }

      seriesList.push({
        name: meta[indicator] || indicator,
        type: "line",
        data: values,
        yAxisIndex: axisIndex,
        showSymbol: false,
        smooth: false,
        connectNulls: false,
        lineStyle: { width: isPrimary ? 2.2 : 1.4, opacity: isPrimary ? 1 : 0.65 },
        emphasis: { focus: "series" },
        z: isPrimary ? 5 : 3,
        animation: false,
        markArea: extra.markArea,
        markLine: extra.markLine
      });
      legendData.push(meta[indicator] || indicator);
    }

    return {
      animation: false,
      backgroundColor: "transparent",
      legend: {
        data: legendData,
        top: 4,
        left: 8,
        icon: "roundRect",
        itemWidth: 13,
        itemHeight: 4,
        textStyle: { color: color.textSub, fontSize: 12 },
        selectedMode: true
      },
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "line", lineStyle: { color: color.axis } },
        backgroundColor: color.tooltipBg,
        borderColor: color.tooltipBorder,
        borderWidth: 1,
        padding: 9,
        textStyle: { color: color.tooltipText, fontSize: 12.5 },
        extraCssText: "box-shadow:0 4px 14px rgba(15,23,42,.10);border-radius:8px;",
        formatter: function (params) {
          if (!params.length) {
            return "";
          }
          var html = '<div style="font-weight:600;margin-bottom:5px">' + params[0].axisValue + "</div>";
          for (var p = 0; p < params.length; p++) {
            var item = params[p];
            var value = item.value;
            if (value === null || value === undefined || value !== value) {
              value = "-";
            } else {
              value = formatNumber(value, 2);
            }
            html +=
              '<div style="display:flex;justify-content:space-between;gap:16px">' +
              '<span style="color:' + color.textMuted + '">' +
              item.marker + escapeHtml(item.seriesName) + "</span>" +
              '<span style="font-weight:600;font-family:var(--font-mono)">' + value + "</span>" +
              "</div>";
          }
          return html;
        }
      },
      grid: { left: 58, right: hasRight ? 58 : 20, top: 40, bottom: 66 },
      xAxis: {
        type: "category",
        data: dates,
        boundaryGap: false,
        axisLine: { lineStyle: { color: color.axis } },
        axisTick: { show: false },
        axisLabel: { color: color.axisLabel, fontSize: 11, hideOverlap: true }
      },
      yAxis: yAxis,
      dataZoom: [
        { type: "inside", start: 0, end: 100 },
        {
          type: "slider",
          start: 0,
          end: 100,
          height: 16,
          bottom: 8,
          borderColor: color.border,
          backgroundColor: color.gridBg,
          fillerColor: color.zoomFiller,
          handleStyle: { color: color.accent, borderColor: color.accent },
          moveHandleStyle: { color: color.accent },
          textStyle: { color: color.textFaint, fontSize: 10 },
          dataBackground: {
            lineStyle: { color: color.axis },
            areaStyle: { color: color.border }
          }
        }
      ],
      series: seriesList
    };
  }

  /**
   * 给主指标构造分位参考线与分位带
   *
   * 【参考线口径】10% / 50% / 90% 分位 + 25%~75% 分位带，
   *   直接由当前窗口内的序列现算，避免后端再传一组分位数。
   *
   * @param {Array<number>} values 主指标序列
   * @returns {object} {markLine, markArea}
   */
  function buildPrimaryMarks(values) {
    var p10 = quantile(values, 0.10);
    var p25 = quantile(values, 0.25);
    var p50 = quantile(values, 0.50);
    var p75 = quantile(values, 0.75);
    var p90 = quantile(values, 0.90);
    var current = lastValid(values);

    if (p50 === null) {
      return {};
    }

    var color = palette();
    var markLineData = [
      { yAxis: p90, name: "90% 分位",
        label: { formatter: "高估线 90%", position: "insideEndTop", color: color.danger, fontSize: 10.5 },
        lineStyle: { color: color.danger, type: "dashed", width: 1 } },
      { yAxis: p50, name: "中位数",
        label: { formatter: "中位数", position: "insideEndTop", color: color.textMuted, fontSize: 10.5 },
        lineStyle: { color: color.textFaint, type: "solid", width: 1 } },
      { yAxis: p10, name: "10% 分位",
        label: { formatter: "低估线 10%", position: "insideEndBottom", color: color.success, fontSize: 10.5 },
        lineStyle: { color: color.success, type: "dashed", width: 1 } }
    ];
    if (current !== null) {
      markLineData.push({
        yAxis: current,
        name: "当前值",
        label: { formatter: "当前 " + formatNumber(current, 2), position: "insideStartTop",
          color: color.accent, fontSize: 11, fontWeight: 600 },
        lineStyle: { color: color.accent, type: "solid", width: 1.6 }
      });
    }

    return {
      markLine: { silent: true, symbol: "none", data: markLineData, z: 6 },
      markArea: {
        silent: true,
        itemStyle: { color: color.band },
        data: [[{ yAxis: p25 }, { yAxis: p75 }]]
      }
    };
  }

  /**
   * 创建或复用 ECharts 实例并应用 option
   *
   * @param {HTMLElement} dom 图表容器
   * @param {object} option ECharts option
   * @param {Function} [rebuild] 返回新 option 的重建函数；主题切换时由
   *                          charts.js 回调，用于让图表配色跟随亮/暗主题。
   * @returns {object} ECharts 实例
   */
  function renderChart(dom, option, rebuild) {
    if (window.SL && window.SL.charts && window.SL.charts.render) {
      return window.SL.charts.render(dom, option, rebuild);
    }
    var chart = echarts.getInstanceByDom(dom);
    if (!chart) {
      chart = echarts.init(dom, null, { renderer: "canvas" });
    }
    chart.setOption(option, true);
    return chart;
  }

  /**
   * 创建带指定文本的元素（统一用 textContent，杜绝 HTML 注入）
   *
   * @param {string} tag 标签名
   * @param {string} className class，可省略
   * @param {string} text 文本内容，可省略
   * @returns {HTMLElement} 元素
   */
  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (text !== undefined && text !== null) {
      node.textContent = text;
    }
    return node;
  }

  /**
   * HTML 转义
   *
   * 【什么时候必须用】
   *     只要这段字符串要拼进 innerHTML、或拼进 tooltip 的 formatter
   *     返回值，且内容可能来自后端（行业名、股票名、语录正文），
   *     就必须先过这里。写死的常量标签不需要。
   *
   * @param {*} text 原始文本
   * @returns {string} 转义后的文本
   */
  function escapeHtml(text) {
    return String(text === null || text === undefined ? "" : text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  /**
   * 显示页面消息条
   *
   * 【为什么全局只留一份】
   *     必须带 `show` 类，消息条才会从 display:none 变成可见。
   *     此前 8 个页面各抄一遍，industries.js 与 insight.js 都漏了这个类
   *     —— 提示被正确渲染却永远看不见。所以它只该存在一次。
   *
   * @param {HTMLElement} el 消息条元素
   * @param {string} text 文本（按 textContent 写入，天然免疫注入）
   * @param {string} [kind] info | success | warning | error
   * @returns {void}
   */
  function showMessage(el, text, kind) {
    if (!el) {
      return;
    }
    el.textContent = text || "";
    el.className = "message show " + (kind || "info");
  }

  /**
   * 隐藏消息条
   *
   * @param {HTMLElement} el 消息条元素
   * @returns {void}
   */
  function clearMessage(el) {
    if (!el) {
      return;
    }
    el.textContent = "";
    el.className = "message";
  }

  // ---------- 导出 ----------
  window.SL = {
    el: el,
    escapeHtml: escapeHtml,
    showMessage: showMessage,
    clearMessage: clearMessage,
    fetchJson: fetchJson,
    formatNumber: formatNumber,
    formatPercent: formatPercent,
    formatInt: formatInt,
    levelOf: levelOf,
    levelBadge: levelBadge,
    tempbar: tempbar,
    quantile: quantile,
    lastValid: lastValid,
    buildTrendOption: buildTrendOption,
    renderChart: renderChart,
    hexA: hexA,
    levelLegend: levelLegend,
    checkHealth: checkHealth,
    cssColor: cssColor,
    palette: palette,
    LEVEL7: LEVEL7
  };
})();
