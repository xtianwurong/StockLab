/*
 * StockLab 交互组件 (app/web/static/ui.js)
 *
 * 职责：components.css 里那些「有行为」的组件的 JS 实现 ——
 *       Toast 轻提示、模态框、抽屉、下拉菜单、Tooltip 跟随定位、
 *       防抖/节流、剪贴板、CSV 导出、日期与紧凑数字格式化、快捷键注册。
 *
 * 约束：
 *   1. 纯原生 JS，不引入任何框架（项目要求离线可用、无构建步骤）；
 *   2. 只挂在 SL.ui 下，不改动 common.js 已有的 SL 契约；
 *   3. 所有容器（toast-host 等）按需惰性创建，页面无需预先写占位 DOM。
 */

(function () {
  "use strict";

  var SL = window.SL || {};

  // ---------------------------------------------------------------------------
  // DOM 助手
  // ---------------------------------------------------------------------------

  /**
   * 按 CSS 选择器查找单个元素
   *
   * @param {string} selector CSS 选择器
   * @param {HTMLElement} [scope] 查找范围，默认 document
   * @returns {HTMLElement|null} 元素或 null
   */
  function find(selector, scope) {
    return (scope || document).querySelector(selector);
  }

  /**
   * 按 CSS 选择器查找全部元素并转成真数组
   *
   * @param {string} selector CSS 选择器
   * @param {HTMLElement} [scope] 查找范围，默认 document
   * @returns {Array<HTMLElement>} 元素数组
   */
  function findAll(selector, scope) {
    var nodes = (scope || document).querySelectorAll(selector);
    var list = [];
    for (var i = 0; i < nodes.length; i++) {
      list.push(i === 0 ? nodes[0] : nodes[i]);
    }
    return list;
  }

  /**
   * 创建元素（统一走 textContent，杜绝 HTML 注入）
   *
   * @param {string} tag 标签名
   * @param {string} className 类名，可省略
   * @param {string} text 文本，可省略
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
   * 惰性获取（或创建）全局挂载容器
   *
   * @param {string} id 容器 id
   * @param {string} className 容器类名
   * @returns {HTMLElement} 容器
   */
  function host(id, className) {
    var node = document.getElementById(id);
    if (node) {
      return node;
    }
    node = el("div", className);
    node.id = id;
    document.body.appendChild(node);
    return node;
  }

  /**
   * HTML 转义（用于把后端文本塞进 title / aria-label 等属性）
   *
   * @param {string} text 原始文本
   * @returns {string} 转义后的文本
   */
  function escapeHtml(text) {
    return String(text === null || text === undefined ? "" : text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // ---------------------------------------------------------------------------
  // 函数工具
  // ---------------------------------------------------------------------------

  /**
   * 防抖：连续触发只在停止 waitMs 后执行一次
   *
   * @param {Function} fn 待包装函数
   * @param {number} waitMs 等待毫秒
   * @returns {Function} 包装后的函数
   */
  function debounce(fn, waitMs) {
    var timer = null;
    return function () {
      var context = this;
      var args = arguments;
      if (timer) {
        clearTimeout(timer);
      }
      timer = setTimeout(function () {
        timer = null;
        fn.apply(context, args);
      }, waitMs || 200);
    };
  }

  /**
   * 节流：waitMs 窗口内最多执行一次
   *
   * @param {Function} fn 待包装函数
   * @param {number} waitMs 窗口毫秒
   * @returns {Function} 包装后的函数
   */
  function throttle(fn, waitMs) {
    var last = 0;
    var pending = null;
    return function () {
      var context = this;
      var args = arguments;
      var now = Date.now();
      var wait = (waitMs || 200) - (now - last);
      if (wait <= 0) {
        last = now;
        fn.apply(context, args);
      } else if (!pending) {
        pending = setTimeout(function () {
          pending = null;
          last = Date.now();
          fn.apply(context, args);
        }, wait);
      }
    };
  }

  /**
   * 睡眠
   *
   * @param {number} ms 毫秒
   * @returns {Promise} 定时完成的 Promise
   */
  function sleep(ms) {
    return new Promise(function (resolve) {
      setTimeout(resolve, ms);
    });
  }

  // ---------------------------------------------------------------------------
  // 格式化补充（common.js 之外的展示需求）
  // ---------------------------------------------------------------------------

  /**
   * 紧凑数字：万 / 亿 / 万亿（A 股语境）
   *
   * @param {number|null} value 数值
   * @returns {string} 如 12.3亿 / 4,500万
   */
  function formatCompact(value) {
    if (value === null || value === undefined || value !== value) {
      return "-";
    }
    var abs = Math.abs(value);
    if (abs >= 1e12) {
      return (value / 1e12).toFixed(2) + "万亿";
    }
    if (abs >= 1e8) {
      return (value / 1e8).toFixed(2) + "亿";
    }
    if (abs >= 1e4) {
      return (value / 1e4).toFixed(2) + "万";
    }
    return SL.formatNumber(value, 2);
  }

  /**
   * 带符号百分比（涨跌幅场景，+ / - 前缀）
   *
   * @param {number|null} value 数值（已乘 100）
   * @param {number} [digits] 小数位，默认 2
   * @returns {string} 如 +1.86%
   */
  function formatSignedPercent(value, digits) {
    if (value === null || value === undefined || value !== value) {
      return "-";
    }
    var places = digits === undefined ? 2 : digits;
    var sign = value > 0 ? "+" : "";
    return sign + value.toFixed(places) + "%";
  }

  /**
   * 日期 -> YYYY-MM-DD
   *
   * @param {Date|string} value 日期
   * @returns {string} 文本；非法返回 "-"
   */
  function formatDate(value) {
    if (!value) {
      return "-";
    }
    var date = value instanceof Date ? value : new Date(value);
    if (isNaN(date.getTime())) {
      return "-";
    }
    var month = String(date.getMonth() + 1);
    var day = String(date.getDate());
    return (
      date.getFullYear() +
      "-" +
      (month.length < 2 ? "0" + month : month) +
      "-" +
      (day.length < 2 ? "0" + day : day)
    );
  }

  /**
   * 日期 -> YYYY-MM（区间标签）
   *
   * @param {Date|string} value 日期
   * @returns {string} 文本；非法返回 "-"
   */
  function formatMonth(value) {
    var text = formatDate(value);
    return text === "-" ? "-" : text.slice(0, 7);
  }

  // ---------------------------------------------------------------------------
  // Toast 轻提示
  // ---------------------------------------------------------------------------

  var TOAST_ICON = {
    info: "i",
    success: "✓",
    warning: "!",
    danger: "×"
  };

  /**
   * 弹出一条轻提示
   *
   * @param {string} message 提示文案
   * @param {object} [opts] {type: info|success|warning|danger, duration: 毫秒, title: 标题}
   * @returns {void}
   */
  function toast(message, opts) {
    var options = opts || {};
    var type = options.type || "info";
    var duration = options.duration === undefined ? 3200 : options.duration;
    var container = host("sl-toast-host", "toast-host");

    var node = el("div", "toast toast-" + type);
    node.setAttribute("role", type === "danger" ? "alert" : "status");
    node.appendChild(el("span", "toast-icon", TOAST_ICON[type] || "i"));
    var body = el("div", "stack-sm");
    if (options.title) {
      body.appendChild(el("div", "text-bold", options.title));
    }
    body.appendChild(el("div", "", message));
    node.appendChild(body);

    var close = el("button", "toast-close", "×");
    close.setAttribute("type", "button");
    close.setAttribute("aria-label", "关闭提示");
    node.appendChild(close);
    container.appendChild(node);

    var remove = function () {
      if (!node.parentNode) {
        return;
      }
      node.classList.add("is-leaving");
      setTimeout(function () {
        if (node.parentNode) {
          node.parentNode.removeChild(node);
        }
      }, 200);
    };
    close.addEventListener("click", remove);
    if (duration > 0) {
      setTimeout(remove, duration);
    }
  }

  toast.success = function (message, opts) {
    toast(message, Object.assign({}, opts || {}, { type: "success" }));
  };
  toast.error = function (message, opts) {
    toast(message, Object.assign({}, opts || {}, { type: "danger", duration: 5000 }));
  };
  toast.warn = function (message, opts) {
    toast(message, Object.assign({}, opts || {}, { type: "warning" }));
  };

  // ---------------------------------------------------------------------------
  // 模态框
  // ---------------------------------------------------------------------------

  var openModals = [];

  /**
   * 打开一个模态框
   *
   * @param {object} config {title, body(HTMLElement|string), size: ""|lg|xl,
   *                        actions: [{label, variant, onClick, closeAfter}],
   *                        onClose, closable}
   * @returns {object} 控制句柄 {close, backdrop, modal}
   */
  function modal(config) {
    var options = config || {};
    var closable = options.closable !== false;

    var backdrop = el("div", "modal-backdrop");
    backdrop.setAttribute("role", "presentation");

    var box = el("div", "modal" + (options.size ? " modal-" + options.size : ""));
    box.setAttribute("role", "dialog");
    box.setAttribute("aria-modal", "true");
    if (options.title) {
      box.setAttribute("aria-label", options.title);
    }

    var head = el("div", "modal-head");
    head.appendChild(el("div", "modal-title", options.title || ""));
    if (closable) {
      var x = el("button", "btn btn-ghost btn-icon btn-sm", "×");
      x.setAttribute("type", "button");
      x.setAttribute("aria-label", "关闭");
      head.appendChild(x);
      box.appendChild(head);
      x.addEventListener("click", function () {
        handle.close();
      });
    }
    box.insertBefore(head, box.firstChild);

    var body = el("div", "modal-body");
    if (typeof options.body === "string") {
      body.appendChild(el("div", "", options.body));
    } else if (options.body) {
      body.appendChild(options.body);
    }
    box.appendChild(body);

    var actions = options.actions || [];
    if (actions.length) {
      var foot = el("div", "modal-foot");
      for (var i = 0; i < actions.length; i++) {
        var action = actions[i];
        var button = el(
          "button",
          "btn " + (action.variant ? "btn-" + action.variant : "btn-secondary"),
          action.label
        );
        button.setAttribute("type", "button");
        button.addEventListener("click", function (event) {
          var proceed = true;
          if (action.onClick) {
            proceed = action.onClick(event) !== false;
          }
          if (proceed && action.closeAfter !== false) {
            handle.close();
          }
        });
        foot.appendChild(button);
      }
      box.appendChild(foot);
    }

    backdrop.appendChild(box);
    document.body.appendChild(backdrop);

    // 触发入场动画（下一帧加 show，保证 transition 生效）
    window.requestAnimationFrame(function () {
      backdrop.classList.add("show");
    });

    var previousFocus = document.activeElement;
    function onKeydown(event) {
      if (event.key === "Escape" && closable) {
        handle.close();
      }
    }
    document.addEventListener("keydown", onKeydown);

    var handle = {
      backdrop: backdrop,
      modal: box,
      body: body,
      close: function () {
        document.removeEventListener("keydown", onKeydown);
        backdrop.classList.remove("show");
        var index = openModals.indexOf(handle);
        if (index >= 0) {
          openModals.splice(index, 1);
        }
        setTimeout(function () {
          if (backdrop.parentNode) {
            backdrop.parentNode.removeChild(backdrop);
          }
        }, 200);
        if (previousFocus && previousFocus.focus) {
          previousFocus.focus();
        }
        if (options.onClose) {
          options.onClose();
        }
      }
    };
    openModals.push(handle);

    if (closable) {
      backdrop.addEventListener("click", function (event) {
        if (event.target === backdrop) {
          handle.close();
        }
      });
    }

    // 焦点移入对话框，方便键盘操作
    var focusable = box.querySelector(
      "input, select, textarea, button:not([aria-label='关闭'])"
    );
    if (focusable) {
      focusable.focus();
    }
    return handle;
  }

  /**
   * 确认对话框（Promise 版）
   *
   * @param {string} message 提示文案
   * @param {object} [opts] {title, okLabel, cancelLabel, danger}
   * @returns {Promise<boolean>} 用户点确认返回 true
   */
  function confirmDialog(message, opts) {
    var options = opts || {};
    return new Promise(function (resolve) {
      var settled = false;
      modal({
        title: options.title || "请确认",
        body: message,
        actions: [
          {
            label: options.cancelLabel || "取消",
            variant: "ghost",
            onClick: function () {
              settled = true;
              resolve(false);
            }
          },
          {
            label: options.okLabel || "确定",
            variant: options.danger ? "danger" : "primary",
            onClick: function () {
              settled = true;
              resolve(true);
            }
          }
        ],
        onClose: function () {
          if (!settled) {
            resolve(false);
          }
        }
      });
    });
  }

  // ---------------------------------------------------------------------------
  // 抽屉
  // ---------------------------------------------------------------------------

  /**
   * 打开右侧抽屉
   *
   * @param {object} config {title, body, footer}
   * @returns {object} 控制句柄 {close, body, setTitle}
   */
  function drawer(config) {
    var options = config || {};

    var backdrop = el("div", "drawer-backdrop");
    var panel = el("aside", "drawer");
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-modal", "true");
    if (options.title) {
      panel.setAttribute("aria-label", options.title);
    }

    var head = el("div", "drawer-head");
    var title = el("div", "modal-title", options.title || "");
    head.appendChild(title);
    var close = el("button", "btn btn-ghost btn-icon btn-sm", "×");
    close.setAttribute("type", "button");
    close.setAttribute("aria-label", "关闭抽屉");
    head.appendChild(close);
    panel.appendChild(head);

    var body = el("div", "drawer-body");
    if (typeof options.body === "string") {
      body.appendChild(el("div", "", options.body));
    } else if (options.body) {
      body.appendChild(options.body);
    }
    panel.appendChild(body);

    if (options.footer) {
      panel.appendChild(el("div", "drawer-foot"));
      panel.lastChild.appendChild(options.footer);
    }

    document.body.appendChild(backdrop);
    document.body.appendChild(panel);
    window.requestAnimationFrame(function () {
      backdrop.classList.add("show");
      panel.classList.add("show");
    });

    var handle = {
      body: body,
      setTitle: function (text) {
        title.textContent = text;
      },
      close: function () {
        backdrop.classList.remove("show");
        panel.classList.remove("show");
        setTimeout(function () {
          if (backdrop.parentNode) {
            backdrop.parentNode.removeChild(backdrop);
          }
          if (panel.parentNode) {
            panel.parentNode.removeChild(panel);
          }
        }, 280);
        if (options.onClose) {
          options.onClose();
        }
      }
    };

    close.addEventListener("click", handle.close);
    backdrop.addEventListener("click", handle.close);
    document.addEventListener("keydown", function onKey(event) {
      if (event.key === "Escape") {
        document.removeEventListener("keydown", onKey);
        handle.close();
      }
    });
    return handle;
  }

  // ---------------------------------------------------------------------------
  // 下拉菜单（点击外部 / Esc 关闭）
  // ---------------------------------------------------------------------------

  /**
   * 绑定下拉菜单开合
   *
   * @param {HTMLElement} root 带 .dropdown 的容器
   * @returns {void}
   */
  function bindDropdown(root) {
    if (!root || root.getAttribute("data-dropdown-bound") === "1") {
      return;
    }
    root.setAttribute("data-dropdown-bound", "1");
    var trigger = root.querySelector("[data-dropdown-trigger]") || root;
    trigger.addEventListener("click", function (event) {
      event.stopPropagation();
      var wasOpen = root.classList.contains("open");
      closeAllDropdowns();
      root.classList.toggle("open", !wasOpen);
    });
  }

  /**
   * 关闭全部下拉
   *
   * @returns {void}
   */
  function closeAllDropdowns() {
    var open = findAll(".dropdown.open");
    for (var i = 0; i < open.length; i++) {
      open[i].classList.remove("open");
    }
  }

  document.addEventListener("click", closeAllDropdowns);
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      closeAllDropdowns();
    }
  });

  // ---------------------------------------------------------------------------
  // Tooltip 跟随定位（data-tip 属性驱动，供 .term 名词解释复用）
  // ---------------------------------------------------------------------------

  var bubble = null;

  /**
   * 在锚点附近显示提示气泡
   *
   * @param {HTMLElement} anchor 锚点元素
   * @param {string} text 提示文本
   * @returns {void}
   */
  function showTooltip(anchor, text) {
    hideTooltip();
    if (!anchor) {
      return;
    }
    bubble = el("div", "tooltip-bubble", text);
    document.body.appendChild(bubble);
    var rect = anchor.getBoundingClientRect();
    var box = bubble.getBoundingClientRect();
    var left = rect.left + rect.width / 2 - box.width / 2;
    var top = rect.top - box.height - 8;
    // 越界修正：顶部放不下就翻到下方，左右不出屏
    if (top < 8) {
      top = rect.bottom + 8;
    }
    left = Math.max(8, Math.min(left, window.innerWidth - box.width - 8));
    bubble.style.left = Math.round(left) + "px";
    bubble.style.top = Math.round(top) + "px";
    bubble.classList.add("show");
  }

  /**
   * 隐藏提示气泡
   *
   * @returns {void}
   */
  function hideTooltip() {
    if (bubble && bubble.parentNode) {
      bubble.parentNode.removeChild(bubble);
    }
    bubble = null;
  }

  /**
   * 为容器内所有 [data-tip] 元素绑定提示气泡（focus 也能触发，兼顾键盘可达性）
   *
   * @param {HTMLElement} scope 容器
   * @returns {void}
   */
  function bindTooltips(scope) {
    var anchors = findAll("[data-tip]", scope);
    for (var i = 0; i < anchors.length; i++) {
      (function (anchor) {
        var text = anchor.getAttribute("data-tip");
        anchor.addEventListener("mouseenter", function () {
          showTooltip(anchor, text);
        });
        anchor.addEventListener("mouseleave", hideTooltip);
        anchor.addEventListener("focus", function () {
          showTooltip(anchor, text);
        });
        anchor.addEventListener("blur", hideTooltip);
      })(anchors[i]);
    }
  }

  window.addEventListener("scroll", hideTooltip, true);

  // ---------------------------------------------------------------------------
  // 剪贴板 / 导出
  // ---------------------------------------------------------------------------

  /**
   * 复制文本到剪贴板（降级到 execCommand，兼容非安全上下文）
   *
   * @param {string} text 待复制文本
   * @returns {Promise<boolean>} 是否成功
   */
  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text).then(function () {
        return true;
      }).catch(function () {
        return legacyCopy(text);
      });
    }
    return Promise.resolve(legacyCopy(text));
  }

  /**
   * 老浏览器复制实现
   *
   * @param {string} text 待复制文本
   * @returns {boolean} 是否成功
   */
  function legacyCopy(text) {
    var area = el("textarea", "");
    area.value = text;
    area.setAttribute("readonly", "readonly");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    var ok = false;
    try {
      ok = document.execCommand("copy");
    } catch (error) {
      ok = false;
    }
    document.body.removeChild(area);
    return ok;
  }

  /**
   * 触发浏览器下载一段文本
   *
   * @param {string} filename 文件名
   * @param {string} content 文本内容
   * @param {string} [mime] MIME 类型，默认 text/csv
   * @returns {void}
   */
  function downloadText(filename, content, mime) {
    // BOM 让 Excel 正确识别 UTF-8 中文
    var blob = new Blob(["﻿" + content], {
      type: (mime || "text/csv") + ";charset=utf-8"
    });
    var url = URL.createObjectURL(blob);
    var link = el("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    setTimeout(function () {
      URL.revokeObjectURL(url);
    }, 0);
  }

  /**
   * 把对象数组导出为 CSV 并下载
   *
   * @param {string} filename 文件名（建议带 .csv）
   * @param {Array<object>} rows 行数组，键取自第一行
   * @returns {void}
   */
  function downloadCsv(filename, rows) {
    if (!rows || !rows.length) {
      return;
    }
    var headers = Object.keys(rows[0]);
    var lines = [headers.join(",")];
    for (var i = 0; i < rows.length; i++) {
      var cells = [];
      for (var c = 0; c < headers.length; c++) {
        var value = rows[i][headers[c]];
        cells.push(csvCell(value));
      }
      lines.push(cells.join(","));
    }
    downloadText(filename, lines.join("\n"));
  }

  /**
   * 单个 CSV 单元格转义（含逗号/引号/换行时加引号）
   *
   * @param {*} value 原始值
   * @returns {string} 转义后的文本
   */
  function csvCell(value) {
    if (value === null || value === undefined || value !== value) {
      return "";
    }
    var text = String(value);
    if (/[",\n\r]/.test(text)) {
      return '"' + text.replace(/"/g, '""') + '"';
    }
    return text;
  }

  // ---------------------------------------------------------------------------
  // URL 参数助手（页面状态可分享）
  // ---------------------------------------------------------------------------

  /**
   * 读取当前地址栏的查询参数
   *
   * @returns {object} 参数字典（值为字符串）
   */
  function queryParams() {
    var result = {};
    var search = window.location.search.replace(/^\?/, "");
    if (!search) {
      return result;
    }
    var parts = search.split("&");
    for (var i = 0; i < parts.length; i++) {
      if (!parts[i]) {
        continue;
      }
      var pair = parts[i].split("=");
      var key = decodeURIComponent(pair[0] || "");
      var value = decodeURIComponent((pair[1] || "").replace(/\+/g, " "));
      result[key] = value;
    }
    return result;
  }

  /**
   * 用给定参数更新地址栏（replace，不产生历史记录）
   *
   * @param {object} params 参数字典；值为 null 的键会被删除
   * @returns {void}
   */
  function replaceQuery(params) {
    var parts = [];
    var keys = Object.keys(params || {});
    for (var i = 0; i < keys.length; i++) {
      var key = keys[i];
      if (params[key] === null || params[key] === undefined || params[key] === "") {
        continue;
      }
      parts.push(encodeURIComponent(key) + "=" + encodeURIComponent(params[key]));
    }
    var url = window.location.pathname + (parts.length ? "?" + parts.join("&") : "");
    window.history.replaceState(null, "", url);
  }

  // ---------------------------------------------------------------------------
  // 快捷键
  // ---------------------------------------------------------------------------

  var shortcuts = {};

  /**
   * 注册快捷键（单键，不支持组合键；输入态自动让路）
   *
   * @param {string} key 键名，如 "k"
   * @param {Function} handler 回调
   * @param {string} [description] 说明（未来做帮助面板用）
   * @returns {void}
   */
  function shortcut(key, handler, description) {
    shortcuts[String(key).toLowerCase()] = { handler: handler, description: description || "" };
  }

  document.addEventListener("keydown", function (event) {
    if (event.metaKey || event.ctrlKey || event.altKey) {
      return;
    }
    var tag = event.target && event.target.tagName ? event.target.tagName.toUpperCase() : "";
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || event.target.isContentEditable) {
      return;
    }
    var entry = shortcuts[String(event.key).toLowerCase()];
    if (entry) {
      event.preventDefault();
      entry.handler(event);
    }
  });

  /**
   * 取已注册快捷键（供帮助面板渲染）
   *
   * @returns {object} {key: {handler, description}}
   */
  function listShortcuts() {
    return shortcuts;
  }

  // ---------------------------------------------------------------------------
  // 按钮加载态
  // ---------------------------------------------------------------------------

  /**
   * 切换按钮的加载态并禁用交互
   *
   * @param {HTMLElement} button 按钮
   * @param {boolean} loading 是否加载中
   * @returns {void}
   */
  function setLoading(button, loading) {
    if (!button) {
      return;
    }
    if (loading) {
      button.classList.add("is-loading");
      button.disabled = true;
    } else {
      button.classList.remove("is-loading");
      button.disabled = false;
    }
  }

  // ---------------------------------------------------------------------------
  // 应用外壳：侧边栏折叠 + 顶栏全局证券搜索
  // ---------------------------------------------------------------------------

  var SHELL_KEY = "sl-shell-collapsed";

  /**
   * 应用折叠态（写 <html> 上的 class，与首屏脚本读同一个键）
   *
   * @param {boolean} collapsed 是否折叠
   * @returns {void}
   */
  function setShellCollapsed(collapsed) {
    if (collapsed) {
      document.documentElement.classList.add("shell-collapsed");
    } else {
      document.documentElement.classList.remove("shell-collapsed");
    }
    try {
      window.localStorage.setItem(SHELL_KEY, collapsed ? "1" : "0");
    } catch (error) {
      // 存储不可用时折叠只在本次会话生效
    }
    // 折叠会改变可用宽度，通知图表重排
    window.setTimeout(function () {
      if (window.SL && SL.charts) {
        SL.charts.redrawAll();
      }
    }, 320);
  }

  /**
   * 是否处于折叠态
   *
   * @returns {boolean} 折叠态
   */
  function isShellCollapsed() {
    return document.documentElement.classList.contains("shell-collapsed");
  }

  /**
   * 绑定侧边栏折叠按钮与快捷键
   *
   * @returns {void}
   */
  function initShell() {
    var button = document.getElementById("btn-collapse");
    if (button) {
      button.addEventListener("click", function () {
        setShellCollapsed(!isShellCollapsed());
      });
    }
    shortcut("[", function () {
      setShellCollapsed(!isShellCollapsed());
    }, "折叠/展开导航");
  }

  /**
   * 顶栏全局证券搜索：联想 + 回车直达个股页
   *
   * 【为何放在顶栏】
   *   「查某只股票」是跨页面最高频的动作，每个页面各放一个输入框会重复三份
   *   样式与交互；收到顶栏后，任何页面按 / 都能立刻跳转。
   *
   * @returns {void}
   */
  function initGlobalSearch() {
    var input = document.getElementById("global-search");
    var suggest = document.getElementById("global-suggest");
    if (!input || !suggest) {
      return;
    }
    var timer = null;

    /**
     * 拉取联想并渲染
     *
     * @param {string} query 关键字
     * @returns {void}
     */
    var render = function (query) {
      if (!query) {
        suggest.className = "suggest";
        suggest.textContent = "";
        return;
      }
      SL.fetchJson("/api/securities?q=" + encodeURIComponent(query), 8000)
        .then(function (data) {
          var items = data.items || [];
          suggest.textContent = "";
          if (!items.length) {
            suggest.appendChild(el("div", "suggest-empty", "没有匹配的证券"));
            suggest.className = "suggest open";
            return;
          }
          items.forEach(function (item) {
            var row = el("div", "suggest-item");
            row.appendChild(el("span", "suggest-code", item.code));
            row.appendChild(el("span", "suggest-name", item.name));
            if (item.market) {
              row.appendChild(el("span", "suggest-market", item.market));
            }
            row.addEventListener("click", function () {
              window.location.href = "/?code=" + encodeURIComponent(item.code);
            });
            suggest.appendChild(row);
          });
          suggest.className = "suggest open";
        })
        .catch(function () {
          suggest.className = "suggest";
        });
    };

    input.addEventListener("input", function () {
      var value = input.value.trim();
      if (timer) {
        clearTimeout(timer);
      }
      timer = setTimeout(function () {
        render(value);
      }, 220);
    });

    input.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        input.value = "";
        suggest.className = "suggest";
        return;
      }
      if (event.key !== "Enter") {
        return;
      }
      event.preventDefault();
      var value = input.value.trim();
      if (!value) {
        return;
      }
      // 形如代码直接跳；中文名交给联想（可能多命中，不擅自替用户选一只）
      if (/^[0-9]{6}(\.(SH|SZ|BJ))?$/i.test(value)) {
        window.location.href = "/?code=" + encodeURIComponent(value.toUpperCase());
      } else {
        render(value);
      }
    });

    document.addEventListener("click", function (event) {
      if (!suggest.contains(event.target) && event.target !== input) {
        suggest.className = "suggest";
      }
    });

    shortcut("/", function () {
      input.focus();
      input.select();
    }, "聚焦全局搜索");
  }

  // ---------------------------------------------------------------------------
  // 导出
  // ---------------------------------------------------------------------------

  SL.ui = {
    find: find,
    findAll: findAll,
    el: el,
    escapeHtml: escapeHtml,
    debounce: debounce,
    throttle: throttle,
    sleep: sleep,
    formatCompact: formatCompact,
    formatSignedPercent: formatSignedPercent,
    formatDate: formatDate,
    formatMonth: formatMonth,
    toast: toast,
    modal: modal,
    confirm: confirmDialog,
    drawer: drawer,
    bindDropdown: bindDropdown,
    closeAllDropdowns: closeAllDropdowns,
    showTooltip: showTooltip,
    hideTooltip: hideTooltip,
    bindTooltips: bindTooltips,
    copyText: copyText,
    downloadText: downloadText,
    downloadCsv: downloadCsv,
    queryParams: queryParams,
    replaceQuery: replaceQuery,
    shortcut: shortcut,
    listShortcuts: listShortcuts,
    setLoading: setLoading,
    setShellCollapsed: setShellCollapsed,
    isShellCollapsed: isShellCollapsed,
    initShell: initShell,
    initGlobalSearch: initGlobalSearch
  };

  window.SL = SL;

  // 外壳与顶栏属于布局层能力，所有页面自动装配
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      initShell();
      initGlobalSearch();
    });
  } else {
    initShell();
    initGlobalSearch();
  }
})();
