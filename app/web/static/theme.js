/*
 * StockLab 主题管理 (app/web/static/theme.js)
 *
 * 职责：亮/暗主题的读取、应用、持久化与广播。
 *
 * 设计要点：
 *   1. 防闪烁（FOUC）：在 <head> 里先用一段**内联**脚本把 data-theme 写进
 *      <html>，CSS 首次绘制前就已确定主题；本文件负责之后的交互与广播，
 *      不承担首屏染色职责。
 *   2. 三态：light / dark / system。system 跟随 prefers-color-scheme 实时变化。
 *   3. 广播：切换后派发 document 事件 "sl:themechange"，charts.js 监听它重绘图表，
 *      页面也可监听它刷新自有配色，避免各页面各自轮询系统主题。
 *   4. 无依赖：不改动 common.js 导出的 SL 契约，只在 SL.theme 下挂载。
 */

(function () {
  "use strict";

  // localStorage 键名（带版本号，便于将来迁移格式）
  var STORAGE_KEY = "sl-theme";
  var MODES = ["light", "dark", "system"];
  var EVENT_NAME = "sl:themechange";

  // 媒体查询对象：system 模式下监听系统切换
  var mediaQuery = null;
  if (window.matchMedia) {
    mediaQuery = window.matchMedia("(prefers-color-scheme: dark)");
  }

  /**
   * 判断是否为暗色主题
   *
   * @returns {boolean} 当前生效主题是否为暗色
   */
  function isDark() {
    var mode = readMode();
    if (mode === "dark") {
      return true;
    }
    if (mode === "light") {
      return false;
    }
    return !!(mediaQuery && mediaQuery.matches);
  }

  /**
   * 读取用户选择的主题模式（localStorage 缺失或非法时返回 system）
   *
   * @returns {string} "light" | "dark" | "system"
   */
  function readMode() {
    var mode = "system";
    try {
      mode = window.localStorage.getItem(STORAGE_KEY) || "system";
    } catch (error) {
      // 隐私模式 / 禁用存储时静默降级为跟随系统
      mode = "system";
    }
    return MODES.indexOf(mode) === -1 ? "system" : mode;
  }

  /**
   * 把主题模式写入 localStorage（失败静默，不影响使用）
   *
   * @param {string} mode 主题模式
   * @returns {void}
   */
  function persistMode(mode) {
    try {
      window.localStorage.setItem(STORAGE_KEY, mode);
    } catch (error) {
      // 忽略：无法持久化时本次会话仍然生效
    }
  }

  /**
   * 把模式写进 <html data-theme>；system 模式下移除该属性，
   * 让 tokens.css 里的 prefers-color-scheme 分支接管
   *
   * @param {string} mode 主题模式
   * @returns {void}
   */
  function applyMode(mode) {
    var root = document.documentElement;
    if (mode === "system") {
      root.removeAttribute("data-theme");
    } else {
      root.setAttribute("data-theme", mode);
    }
  }

  /**
   * 派发主题变更事件
   *
   * @param {string} mode 用户选择的模式
   * @returns {void}
   */
  function broadcast(mode) {
    var detail = { mode: mode, dark: isDark() };
    var event;
    if (typeof window.CustomEvent === "function") {
      event = new window.CustomEvent(EVENT_NAME, { detail: detail });
    } else {
      // 老浏览器兜底
      event = document.createEvent("CustomEvent");
      event.initCustomEvent(EVENT_NAME, false, false, detail);
    }
    document.dispatchEvent(event);
  }

  /**
   * 设置主题
   *
   * @param {string} mode "light" | "dark" | "system"
   * @returns {string} 实际生效的模式
   */
  function setTheme(mode) {
    var next = MODES.indexOf(mode) === -1 ? "system" : mode;
    // 打开过渡类，让主题色切换有一帧柔和过渡
    document.documentElement.classList.add("theme-switching");
    applyMode(next);
    persistMode(next);
    syncToggles(next);
    broadcast(next);
    window.setTimeout(function () {
      document.documentElement.classList.remove("theme-switching");
    }, 320);
    return next;
  }

  /**
   * 在亮/暗之间切换（system 模式下以当前实际呈现为起点翻转）
   *
   * @returns {string} 切换后的模式
   */
  function toggleTheme() {
    return setTheme(isDark() ? "light" : "dark");
  }

  /**
   * 同步所有主题切换按钮的图标与提示文案
   *
   * @param {string} mode 当前模式
   * @returns {void}
   */
  function syncToggles(mode) {
    var buttons = document.querySelectorAll("[data-theme-toggle]");
    var resolved = mode === "system" ? (isDark() ? "暗色" : "亮色") : (mode === "dark" ? "暗色" : "亮色");
    for (var i = 0; i < buttons.length; i++) {
      var button = buttons[i];
      button.setAttribute("aria-label", "切换主题（当前：" + resolved + "）");
      button.setAttribute("title", "切换主题（当前：" + resolved + "，快捷键 T）");
      button.setAttribute("data-theme-mode", mode);
    }
  }

  /**
   * 给按钮绑定切换行为（同一按钮重复调用只会绑定一次）
   *
   * @param {HTMLElement} button 带 data-theme-toggle 的按钮
   * @returns {void}
   */
  function bindToggle(button) {
    if (!button || button.getAttribute("data-theme-bound") === "1") {
      return;
    }
    button.setAttribute("data-theme-bound", "1");
    button.addEventListener("click", function () {
      toggleTheme();
    });
  }

  /**
   * 初始化：应用已保存的主题、绑定现有按钮、监听系统主题变化
   *
   * @returns {void}
   */
  function init() {
    applyMode(readMode());
    syncToggles(readMode());

    var toggles = document.querySelectorAll("[data-theme-toggle]");
    for (var i = 0; i < toggles.length; i++) {
      bindToggle(toggles[i]);
    }

    // system 模式下跟随系统实时切换
    if (mediaQuery) {
      var onSystemChange = function () {
        if (readMode() === "system") {
          applyMode("system");
          syncToggles("system");
          broadcast("system");
        }
      };
      if (typeof mediaQuery.addEventListener === "function") {
        mediaQuery.addEventListener("change", onSystemChange);
      } else if (typeof mediaQuery.addListener === "function") {
        mediaQuery.addListener(onSystemChange);
      }
    }

    // 快捷键：T 切换主题（输入框内不触发）
    document.addEventListener("keydown", function (event) {
      if (event.key !== "t" && event.key !== "T") {
        return;
      }
      if (event.metaKey || event.ctrlKey || event.altKey) {
        return;
      }
      var tag = event.target && event.target.tagName ? event.target.tagName.toUpperCase() : "";
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") {
        return;
      }
      toggleTheme();
    });
  }

  // 挂到 SL 命名空间（common.js 已建立 window.SL；独立引入时自建）
  window.SL = window.SL || {};
  window.SL.theme = {
    init: init,
    get: readMode,
    set: setTheme,
    toggle: toggleTheme,
    isDark: isDark,
    bindToggle: bindToggle,
    syncToggles: syncToggles,
    EVENT: EVENT_NAME
  };

  // DOM 就绪后自动初始化；放在 head 引入时也能安全等待
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
