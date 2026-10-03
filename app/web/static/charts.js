/*
 * StockLab 图表主题适配 (app/web/static/charts.js)
 *
 * 职责：让 ECharts 图表跟随亮/暗主题，并提供跨页面复用的图表脚手架。
 *
 * 依赖：theme.js（主题广播）、common.js（SL.fetchJson / SL.formatNumber 等）。
 * 加载顺序：common.js -> charts.js -> theme.js -> 页面脚本。
 *
 * 三件事：
 *   1. palette()：从 CSS 变量读取当前主题的图表配色（唯一的颜色来源）；
 *   2. 图表登记簿：render() 记录「容器 -> 实例 + 重建函数」，
 *      主题切换时调用重建函数重绘，避免各页面自己监听事件；
 *   3. option 工厂：把 ECharts 常用配置（坐标轴 / tooltip / 网格 / 缩放）
 *      抽成可复用片段，新页面（选股器 / 多股对比）直接拼装，不再各写一遍。
 */

(function () {
  "use strict";

  // 登记簿：用数组而非 WeakMap —— 需要可遍历才能批量重绘
  var registry = [];
  var seq = 0;

  // ---------------------------------------------------------------------------
  // 1. 配色
  // ---------------------------------------------------------------------------

  /**
   * 读取 CSS 变量（自动 trim，兼容 getPropertyValue 的空白返回）
   *
   * @param {string} name 变量名，如 "--accent"
   * @param {string} [fallback] 取不到时的兜底色
   * @returns {string} 颜色文本（可能是 #hex / rgb() / color-mix() 等任意合法值）
   */
  function cssVar(name, fallback) {
    var value = "";
    try {
      value = window.getComputedStyle(document.documentElement).getPropertyValue(name);
    } catch (error) {
      value = "";
    }
    value = (value || "").trim();
    return value || fallback || "";
  }

  /**
   * 解析颜色：接受 #hex / var(--x) / rgb()
   *
   * 【为何需要】页面与 common.js 里既有字面量色值，也有 var(--level-1) 这类
   *   主题变量引用；徽章需要在此解析成具体颜色才能叠加透明度。
   *
   * @param {string} color 颜色文本
   * @param {string} [fallback] 解析失败时的兜底
   * @returns {string} 具体颜色
   */
  function cssColor(color, fallback) {
    if (!color) {
      return fallback || "";
    }
    var text = String(color).trim();
    if (text.indexOf("var(") === 0) {
      var name = text.slice(4, text.indexOf(")")).trim();
      return cssVar(name, fallback || text);
    }
    return text;
  }

  /**
   * 当前主题下的图表调色板
   *
   * @returns {object} 供 ECharts option 直接使用的颜色集合
   */
  function palette() {
    return {
      textMain: cssVar("--text-main", "#0f172a"),
      textSub: cssVar("--text-sub", "#334155"),
      textMuted: cssVar("--text-muted", "#64748b"),
      textFaint: cssVar("--text-faint", "#94a3b8"),
      axis: cssVar("--chart-axis", "#cbd5e1"),
      axisLabel: cssVar("--chart-axis-label", "#64748b"),
      split: cssVar("--chart-split", "#eef2f7"),
      accent: cssVar("--accent", "#2563eb"),
      accentSoft: cssVar("--accent-soft", "#eff6ff"),
      tooltipBg: cssVar("--chart-tooltip-bg", "#ffffff"),
      tooltipText: cssVar("--chart-tooltip-text", "#0f172a"),
      tooltipBorder: cssVar("--chart-tooltip-border", "#e2e8f0"),
      band: cssVar("--chart-band", "rgba(100,116,139,0.09)"),
      zoomFiller: cssVar("--chart-zoom-filler", "rgba(37,99,235,0.12)"),
      gridBg: cssVar("--chart-grid-bg", "#f8fafc"),
      border: cssVar("--border-color", "#e2e8f0"),
      danger: cssVar("--danger-500", "#dc2626"),
      success: cssVar("--success-500", "#16a34a")
    };
  }

  /**
   * 七档评级的当前主题配色
   *
   * @returns {Array<string>} 长度 7 的颜色数组（顺序与 SL.LEVEL7 一致）
   */
  function levelColors() {
    return [
      cssVar("--level-1", "#15803d"),
      cssVar("--level-2", "#16a34a"),
      cssVar("--level-3", "#65a30d"),
      cssVar("--level-4", "#ca8a04"),
      cssVar("--level-5", "#ea580c"),
      cssVar("--level-6", "#dc2626"),
      cssVar("--level-7", "#991b1b")
    ];
  }

  /**
   * 取分位对应的档位色（暗色主题下自动换成亮色系）
   *
   * @param {number} percentile 分位 0~100
   * @returns {string} 颜色；不可用返回灰色
   */
  function levelColorOf(percentile) {
    if (percentile === null || percentile === undefined || percentile !== percentile) {
      return cssVar("--level-na", "#94a3b8");
    }
    var edges = [10, 20, 40, 60, 80, 90, 100];
    var colors = levelColors();
    for (var i = 0; i < edges.length; i++) {
      if (percentile < edges[i]) {
        return colors[i];
      }
    }
    return colors[colors.length - 1];
  }

  // ---------------------------------------------------------------------------
  // 2. 登记簿与重绘
  // ---------------------------------------------------------------------------

  /**
   * 渲染（或更新）一张图表，并登记以便主题切换时重绘
   *
   * @param {HTMLElement} dom 图表容器
   * @param {object} option ECharts option
   * @param {Function} [rebuild] 返回新 option 的函数；主题切换时会再调用一次，
   *                          缺省则仅复用旧 option（颜色不会跟着主题变）
   * @returns {object} ECharts 实例
   */
  function render(dom, option, rebuild) {
    if (!dom || typeof window.echarts === "undefined") {
      return null;
    }
    var chart = window.echarts.getInstanceByDom(dom);
    if (!chart) {
      chart = window.echarts.init(dom, null, { renderer: "canvas" });
    }
    chart.setOption(option, true);

    var found = false;
    for (var i = 0; i < registry.length; i++) {
      if (registry[i].dom === dom) {
        registry[i].chart = chart;
        registry[i].rebuild = rebuild || null;
        found = true;
        break;
      }
    }
    if (!found) {
      seq += 1;
      registry.push({ id: seq, dom: dom, chart: chart, rebuild: rebuild || null });
    }
    return chart;
  }

  /**
   * 主题切换后重绘全部已登记图表
   *
   * @returns {void}
   */
  function redrawAll() {
    for (var i = 0; i < registry.length; i++) {
      var entry = registry[i];
      // 容器已被移除（切页/条件渲染）时顺手注销，避免登记表无限增长
      if (!entry.dom || !entry.dom.isConnected) {
        registry.splice(i, 1);
        i -= 1;
        continue;
      }
      try {
        if (entry.rebuild) {
          entry.chart.setOption(entry.rebuild(), true);
        }
        entry.chart.resize();
      } catch (error) {
        // 单张图表失败不影响其它图表
        if (window.console && console.warn) {
          console.warn("[StockLab] 图表重绘失败", error);
        }
      }
    }
  }

  // ---------------------------------------------------------------------------
  // 3. 可复用 option 片段
  // ---------------------------------------------------------------------------

  /**
   * 类目轴（X 轴）通用配置
   *
   * @param {Array} data 类目数组
   * @returns {object} ECharts xAxis 配置
   */
  function categoryAxis(data) {
    var color = palette();
    return {
      type: "category",
      data: data,
      boundaryGap: false,
      axisLine: { lineStyle: { color: color.axis } },
      axisTick: { show: false },
      axisLabel: { color: color.axisLabel, fontSize: 11, hideOverlap: true }
    };
  }

  /**
   * 数值轴（Y 轴）通用配置
   *
   * @param {object} [opts] {name, position, grid} 覆盖项
   * @returns {object} ECharts yAxis 配置
   */
  function valueAxis(opts) {
    var options = opts || {};
    var color = palette();
    return {
      type: "value",
      name: options.name || "",
      scale: true,
      position: options.position || "left",
      showGrid: options.grid !== false,
      axisLabel: { color: color.axisLabel, fontSize: 11 },
      axisLine: { lineStyle: { color: color.axis } },
      splitLine: {
        show: options.grid !== false,
        lineStyle: { color: color.split }
      },
      nameTextStyle: { color: color.textFaint, fontSize: 11 }
    };
  }

  /**
   * 提示框通用配置（axis 触发）
   *
   * @returns {object} ECharts tooltip 配置
   */
  function tooltip() {
    var color = palette();
    return {
      trigger: "axis",
      axisPointer: { type: "line", lineStyle: { color: color.axis } },
      backgroundColor: color.tooltipBg,
      borderColor: color.tooltipBorder,
      borderWidth: 1,
      padding: 9,
      textStyle: { color: color.tooltipText, fontSize: 12.5 },
      extraCssText: "box-shadow:0 4px 14px rgba(15,23,42,.10);border-radius:8px;"
    };
  }

  /**
   * 网格留白通用配置
   *
   * @param {object} [opts] {left, right, top, bottom} 覆盖项
   * @returns {object} ECharts grid 配置
   */
  function grid(opts) {
    var options = opts || {};
    return {
      left: options.left === undefined ? 58 : options.left,
      right: options.right === undefined ? 24 : options.right,
      top: options.top === undefined ? 40 : options.top,
      bottom: options.bottom === undefined ? 34 : options.bottom,
      containLabel: false
    };
  }

  /**
   * 缩放条通用配置（区间内缩放 + 底部滑块）
   *
   * @returns {Array} ECharts dataZoom 配置
   */
  function dataZoom() {
    var color = palette();
    return [
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
    ];
  }

  /**
   * 统一底色（透明，交给卡片背景）与图例配置
   *
   * @param {Array} legendData 图例名数组
   * @returns {object} ECharts 顶层 option 片段
   */
  function baseOption(legendData) {
    var color = palette();
    return {
      animation: false,
      backgroundColor: "transparent",
      textStyle: { fontFamily: "inherit" },
      legend: legendData && legendData.length
        ? {
            data: legendData,
            top: 4,
            left: 8,
            icon: "roundRect",
            itemWidth: 13,
            itemHeight: 4,
            textStyle: { color: color.textSub, fontSize: 12 },
            selectedMode: true
          }
        : { show: false }
    };
  }

  // ---------------------------------------------------------------------------
  // 导出与事件绑定
  // ---------------------------------------------------------------------------

  window.SL = window.SL || {};
  window.SL.charts = {
    cssVar: cssVar,
    cssColor: cssColor,
    palette: palette,
    levelColors: levelColors,
    levelColorOf: levelColorOf,
    render: render,
    redrawAll: redrawAll,
    categoryAxis: categoryAxis,
    valueAxis: valueAxis,
    tooltip: tooltip,
    grid: grid,
    dataZoom: dataZoom,
    baseOption: baseOption
  };

  document.addEventListener("sl:themechange", redrawAll);
})();
