/*
 * StockLab 投资理念页前端逻辑 (app/web/static/insight.js)
 *
 * 职责：
 *   1. 加载筛选面板元数据（/api/insight/meta），渲染投资人卡 / 平台·类型·主题筹码
 *   2. 多维筛选加载言论（/api/insight/quotes），渲染言论流
 *   3. 核验状态徽标与溯源链接的可信度呈现
 *
 * 本页最重要的渲染约束（改代码前先读）：
 *   每条言论都必须带核验状态徽标，且「未核实」用警示竖条 + 警示色徽标，
 *   「已核实」是全页唯一用绿色的地方。
 *   如果把绿色泛用到「未核实」，或者为了版面整齐把状态徽标省掉，
 *   这个页面就退化成一个语录列表 —— 而语录列表正是本域最需要避免的东西：
 *   网上流传的「名人语录」有大量伪造与张冠李戴，读者无从分辨。
 *   tests/test_insight.py 的 test_page_never_renders_unverified_as_fact
 *   就是钉这一条的。
 *
 * 说明：本文件为浏览器端脚本，与 Python 侧无共享约束，保持原生 JS 直白写法。
 */

(function () {
  "use strict";

  var SL = window.SL;
  var ui = SL.ui;

  // ---------- DOM ----------
  var messageBox = document.getElementById("message");
  var investorGrid = document.getElementById("investor-grid");
  var quoteList = document.getElementById("quote-list");
  var quoteHint = document.getElementById("quote-hint");
  var pageInfo = document.getElementById("page-info");
  var btnPrev = document.getElementById("btn-prev");
  var btnNext = document.getElementById("btn-next");
  var btnReload = document.getElementById("btn-reload");
  var chipPlatforms = document.getElementById("f-platforms");
  var chipTypes = document.getElementById("f-types");
  var chipThemes = document.getElementById("f-themes");
  var selectVerification = document.getElementById("f-verification");
  var inputKeyword = document.getElementById("f-keyword");
  var inputSince = document.getElementById("f-since");
  var selectOrder = document.getElementById("f-order");
  var disclaimerText = document.getElementById("disclaimer-text");

  var statInvestors = document.getElementById("stat-investors");
  var statAccounts = document.getElementById("stat-accounts");
  var statQuotes = document.getElementById("stat-quotes");
  var statVerified = document.getElementById("stat-verified");
  var statUnverified = document.getElementById("stat-unverified");

  var PAGE_SIZE = 20;
  var CLAMP_LINES = 6;

  // ---------- 状态 ----------
  var state = {
    investors: [],
    meta: null,
    selected: {},          // investor_code -> true
    platforms: {},         // platform -> true
    types: {},             // quote_type -> true
    themes: {},            // theme -> true
    offset: 0,
    returned: 0,
    busy: false
  };

  // ---------- 工具 ----------

  function showMessage(text, kind) {
    messageBox.textContent = text || "";
    messageBox.className = "message " + (kind || "info");
  }

  function clearMessage() {
    messageBox.textContent = "";
    messageBox.className = "message";
  }

  function setStat(node, value) {
    node.textContent = value === null || value === undefined ? "-" : String(value);
  }

  /**
   * 渲染一组可选筹码（平台 / 类型 / 主题）
   *
   * @param {HTMLElement} host 容器
   * @param {Array} items [{value, label, count}]
   * @param {Object} selected 选中状态表（原地修改）
   */
  function renderChips(host, items, selected) {
    host.textContent = "";
    items.forEach(function (item) {
      var button = ui.el("button", "chip", item.label + (
        item.count === null || item.count === undefined ? "" : " " + item.count
      ));
      button.type = "button";
      button.title = item.value;
      if (selected[item.value]) {
        button.className = "chip active";
      }
      button.addEventListener("click", function () {
        if (selected[item.value]) {
          delete selected[item.value];
        } else {
          selected[item.value] = true;
        }
        state.offset = 0;
        loadQuotes();
      });
      host.appendChild(button);
    });
  }

  /**
   * 渲染投资人卡片
   *
   * @param {Array} investors 投资人列表
   */
  function renderInvestors(investors) {
    investorGrid.textContent = "";
    if (!investors.length) {
      investorGrid.appendChild(ui.el(
        "div", "empty-state",
        "尚未登记任何投资人。执行 python app/scripts/sync_investor_insight.py " +
        "load-sources 载入 configs/insight_sources.json"
      ));
      return;
    }

    var total = 0;
    investors.forEach(function (item) {
      total += item.quote_count || 0;

      var card = ui.el("button", "investor-card");
      card.type = "button";
      if (state.selected[item.investor_code]) {
        card.className = "investor-card active";
      }
      card.appendChild(ui.el("div", "investor-card-name", item.name));
      card.appendChild(ui.el("div", "investor-card-sub",
        item.alias_label || item.role || "—"));

      var foot = ui.el("div", "investor-card-foot");
      foot.appendChild(ui.el("span", null, (item.quote_count || 0) + " 条"));
      if (item.style_label) {
        foot.appendChild(ui.el("span", "tag", item.style_label));
      }
      if (item.verified_count) {
        var vf = ui.el("span", "vf vf-verified", "已核实 " + item.verified_count);
        foot.appendChild(vf);
      }
      card.appendChild(foot);

      card.addEventListener("click", function () {
        if (state.selected[item.investor_code]) {
          delete state.selected[item.investor_code];
        } else {
          state.selected[item.investor_code] = true;
        }
        state.offset = 0;
        renderInvestors(state.investors);
        loadQuotes();
      });
      investorGrid.appendChild(card);
    });

    if (!total) {
      investorGrid.appendChild(ui.el(
        "div", "empty-state",
        "投资人已登记但还没有言论。" +
        "雪球需要登录态：export STOCKLAB_XUEQIU_COOKIE='...' 后再执行 collect"
      ));
    }
  }

  /**
   * 渲染核验状态徽标
   *
   * @param {string} status 状态值
   * @param {string} label  展示文案（后端已翻译）
   */
  function verificationBadge(status, label) {
    var known = ["unverified", "verified", "disputed", "fabricated"];
    var cls = known.indexOf(status) >= 0 ? "vf-" + status : "vf-unknown";
    return ui.el("span", "vf " + cls, label || "未知状态");
  }

  /**
   * 渲染单条言论
   *
   * @param {Object} quote 言论记录
   */
  function renderQuote(quote) {
    var item = ui.el("div", "quote-item");
    item.setAttribute("data-verification", quote.verification || "unverified");
    item.setAttribute("data-quote-id", quote.quote_id || "");

    // 抬头：谁说的 / 哪个平台 / 什么时候 / 核验状态
    var head = ui.el("div", "quote-head");
    head.appendChild(ui.el("span", "quote-attr", quote.investor_name || "—"));
    head.appendChild(ui.el("span", null, "·"));
    head.appendChild(ui.el("span", null, quote.platform_label || quote.platform));
    if (quote.account_name) {
      head.appendChild(ui.el("span", null, "@" + quote.account_name));
    }
    var when = quote.published_at || quote.captured_at;
    if (when) {
      head.appendChild(ui.el("span", null, "·"));
      head.appendChild(ui.el("span", null, when));
    }
    head.appendChild(verificationBadge(
      quote.verification, quote.verification_label));
    item.appendChild(head);

    // 正文
    var body = ui.el("div", "quote-body clamped", quote.content || "");
    item.appendChild(body);

    // 类型与主题标签
    var tags = ui.el("div", "quote-tags");
    if (quote.quote_type_label) {
      tags.appendChild(ui.el("span", "tag tag-type", quote.quote_type_label));
    }
    (quote.themes || []).forEach(function (theme) {
      tags.appendChild(ui.el("span", "tag", theme));
    });
    (quote.stock_codes || []).forEach(function (code) {
      var tag = ui.el("span", "tag", code);
      tag.title = "该条言论提到这只标的";
      tags.appendChild(tag);
    });
    if (tags.childNodes.length) {
      item.appendChild(tags);
    }

    // 尾部：溯源与抓取时间
    var foot = ui.el("div", "quote-foot");
    if (quote.has_source && quote.source_url) {
      var link = ui.el("a", "link-plain", "查看原文 ↗");
      link.href = quote.source_url;
      link.target = "_blank";
      link.rel = "noopener noreferrer nofollow";
      foot.appendChild(link);
    } else {
      var warn = ui.el("span", "no-source", "无溯源链接");
      warn.title = "抓取时没有拿到原始地址。库内不会用首页 URL 凑数 —— " +
        "假链接比没有链接更糟，它会让人以为这句话已被核实过。";
      foot.appendChild(warn);
    }
    if (quote.captured_at) {
      foot.appendChild(ui.el("span", null, "抓取于 " + quote.captured_at));
    }
    item.appendChild(foot);

    // 长文展开
    if ((quote.content || "").length > 180) {
      var toggle = ui.el("button", "expand-btn", "展开全文");
      toggle.type = "button";
      toggle.addEventListener("click", function () {
        var clamped = body.classList.toggle("clamped");
        toggle.textContent = clamped ? "展开全文" : "收起";
      });
      foot.appendChild(toggle);
    }

    return item;
  }

  function renderQuotes(items) {
    quoteList.textContent = "";
    if (!items.length) {
      quoteList.appendChild(ui.el(
        "div", "empty-state",
        "没有匹配的言论。放宽筛选条件，或执行同步命令补数据。"
      ));
      quoteHint.textContent = "0 条";
      return;
    }
    items.forEach(function (quote) {
      quoteList.appendChild(renderQuote(quote));
    });
    quoteHint.textContent = "本页 " + items.length + " 条";
  }

  function updatePager() {
    var from = state.returned ? state.offset + 1 : 0;
    var to = state.offset + state.returned;
    pageInfo.textContent = from + "-" + to;
    btnPrev.disabled = state.offset <= 0 || state.busy;
    btnNext.disabled = state.returned < PAGE_SIZE || state.busy;
  }

  function selectedValues(map) {
    return Object.keys(map).filter(function (key) { return map[key]; });
  }

  function buildQuery() {
    var params = ui.queryParams();
    var codes = selectedValues(state.selected);
    var platforms = selectedValues(state.platforms);
    var types = selectedValues(state.types);
    var themes = selectedValues(state.themes);

    if (codes.length) { params.investor_codes = codes.join(","); }
    if (platforms.length) { params.platforms = platforms.join(","); }
    if (types.length) { params.quote_types = types.join(","); }
    if (themes.length) { params.themes = themes.join(","); }
    if (selectVerification.value) { params.verification = selectVerification.value; }
    if (inputKeyword.value.trim()) { params.keyword = inputKeyword.value.trim(); }
    if (inputSince.value) { params.since = inputSince.value; }
    params.order_by = selectOrder.value;
    params.limit = PAGE_SIZE;
    params.offset = state.offset;

    return Object.keys(params).map(function (key) {
      return encodeURIComponent(key) + "=" + encodeURIComponent(params[key]);
    }).join("&");
  }

  // ---------- 加载 ----------

  function loadQuotes() {
    if (state.busy) { return; }
    state.busy = true;
    ui.setLoading(btnReload, true);

    SL.fetchJson("/api/insight/quotes?" + buildQuery(), 15000)
      .then(function (data) {
        if (data.error) {
          throw new Error(data.error);
        }
        // 用投资人名字替换代码：页面上不该给用户看 dyp_0001 这种内部 ID
        var names = {};
        state.investors.forEach(function (item) {
          names[item.investor_code] = item.name;
        });
        (data.items || []).forEach(function (quote) {
          quote.investor_name = names[quote.investor_code] || quote.investor_code;
        });

        state.returned = (data.items || []).length;
        clearMessage();
        renderQuotes(data.items || []);
        if (data.verification_hint) {
          disclaimerText.textContent = data.verification_hint;
        }
        updatePager();
      })
      .catch(function (error) {
        state.returned = 0;
        quoteList.textContent = "";
        showMessage(error.message || "言论加载失败，请稍后重试", "error");
        updatePager();
      })
      .finally(function () {
        state.busy = false;
        ui.setLoading(btnReload, false);
      });
  }

  function loadMeta() {
    return SL.fetchJson("/api/insight/meta", 15000)
      .then(function (data) {
        if (data.error) {
          throw new Error(data.error);
        }
        state.meta = data;
        state.investors = data.investors || [];

        setStat(statInvestors, data.investor_count);
        statAccounts.textContent = data.account_count + " 个平台账号";

        var summary = data.verification_summary || {};
        var verified = summary.verified || 0;
        var unverified = summary.unverified || 0;
        var disputed = (summary.disputed || 0) + (summary.fabricated || 0);
        statVerified.textContent = verified;
        statUnverified.textContent = unverified + disputed;
        if (disputed) {
          document.getElementById("stat-unverified-sub").textContent =
            "含 " + disputed + " 条存疑/伪造";
        }
        statQuotes.textContent = data.quote_count || 0;

        if (data.verification_hint) {
          disclaimerText.textContent = data.verification_hint;
        }

        // 核验状态下拉
        selectVerification.textContent = "";
        selectVerification.appendChild(ui.el("option", null, "全部"));
        (data.verifications || []).forEach(function (item) {
          var option = ui.el("option", null,
            item.label + "（" + (summary[item.value] || 0) + "）");
          option.value = item.value;
          selectVerification.appendChild(option);
        });

        renderInvestors(state.investors);
        renderChips(chipPlatforms, data.platforms || [], state.platforms);
        renderChips(chipTypes, data.quote_types || [], state.types);
        renderChips(chipThemes, data.themes || [], state.themes);
      });
  }

  function load() {
    if (state.busy) { return; }
    state.busy = true;
    ui.setLoading(btnReload, true);
    showMessage("正在加载投资人观点库…", "info");

    loadMeta()
      .then(loadQuotes)
      .catch(function (error) {
        showMessage(error.message || "加载失败，请稍后重试", "error");
      })
      .finally(function () {
        state.busy = false;
        ui.setLoading(btnReload, false);
        SL.checkHealth();
      });
  }

  // ---------- 事件 ----------

  btnReload.addEventListener("click", function () {
    state.offset = 0;
    load();
  });

  btnPrev.addEventListener("click", function () {
    state.offset = Math.max(0, state.offset - PAGE_SIZE);
    loadQuotes();
  });

  btnNext.addEventListener("click", function () {
    state.offset = state.offset + PAGE_SIZE;
    loadQuotes();
  });

  selectVerification.addEventListener("change", function () {
    state.offset = 0;
    loadQuotes();
  });

  selectOrder.addEventListener("change", function () {
    state.offset = 0;
    loadQuotes();
  });

  inputSince.addEventListener("change", function () {
    state.offset = 0;
    loadQuotes();
  });

  inputKeyword.addEventListener("input", ui.debounce(function () {
    state.offset = 0;
    loadQuotes();
  }, 420));

  inputKeyword.addEventListener("keydown", function (event) {
    if (event.key === "Enter") {
      state.offset = 0;
      loadQuotes();
    }
  });

  // ---------- 启动 ----------
  load();
})();
