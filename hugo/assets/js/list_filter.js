// hugo/assets/js/list_filter.js
// /deals/ と /price/ の一覧を、予算・お子さまの年齢・並び順で絞り込む。
//
// 一覧は全件をサーバー側で描いておき (クローラーと JS 無効の閲覧者はそのまま全件を見る)、
// この JS は各 <li> の data-* を読んで表示/非表示と並び順を切り替えるだけ。
// 新しいデータの取得はしない。
//
// 年齢の判定はおもちゃ診断 (diagnosis_core.js) と同じ規則にそろえる:
//   - 対象年齢 (age_min_months) がお子さまの年齢を超える商品は出さない (安全側)
//   - 対象年齢が幼すぎる商品も出さない (下限の幅は lowerWindow)
//   - 対象年齢が不明な商品は、年齢で絞ったときは出さない
// 選んだ条件は URL のクエリ (?budget=&age=&sort=&lowest=) に残し、共有・戻るで復元する。
(function (global) {
  "use strict";

  var BUDGETS = {
    "u2000": { lo: 0, hi: 1999 },
    "2000-3000": { lo: 2000, hi: 2999 },
    "3000-5000": { lo: 3000, hi: 4999 },
    "5000-10000": { lo: 5000, hi: 9999 },
    "o10000": { lo: 10000, hi: Infinity }
  };

  // lo / hi は月齢。diagnosis_core.js の AGES を 0 歳だけまとめたもの。
  var AGES = {
    "0y": { lo: 0, hi: 11 },
    "1y": { lo: 12, hi: 23 },
    "2y": { lo: 24, hi: 35 },
    "3y": { lo: 36, hi: 47 },
    "4y": { lo: 48, hi: 59 },
    "5y": { lo: 60, hi: 71 },
    "6-7y": { lo: 72, hi: 95 },
    "8y": { lo: 96, hi: 155 }
  };

  function lowerWindow(lo) {
    if (lo < 24) return 12;
    if (lo < 72) return 24;
    return 36;
  }

  function num(v) {
    if (v === undefined || v === null || v === "") return NaN;
    var n = Number(v);
    return isFinite(n) ? n : NaN;
  }

  // item: { price, age, lowest } (数値 or NaN / 真偽)。state: { budget, age, lowest }
  function matches(item, state) {
    if (state.budget) {
      var b = BUDGETS[state.budget];
      if (b) {
        if (isNaN(item.price) || item.price < b.lo || item.price > b.hi) return false;
      }
    }
    if (state.age) {
      var a = AGES[state.age];
      if (a) {
        if (isNaN(item.age)) return false;
        if (item.age > a.hi) return false;
        if (item.age < a.lo - lowerWindow(a.lo)) return false;
      }
    }
    if (state.lowest && !item.lowest) return false;
    return true;
  }

  // 並び順のキー。order は描画時の並び (= 既定の並び) で、同値のときの決め手にも使う。
  var SORTS = {
    "default": function (a, b) { return a.order - b.order; },
    "price-asc": function (a, b) { return cmp(a.price, b.price, 1) || a.order - b.order; },
    "price-desc": function (a, b) { return cmp(a.price, b.price, -1) || a.order - b.order; },
    "drop-yen": function (a, b) { return cmp(a.dropYen, b.dropYen, -1) || a.order - b.order; }
  };

  // 値の無いものは常に末尾。
  function cmp(x, y, dir) {
    var nx = isNaN(x), ny = isNaN(y);
    if (nx && ny) return 0;
    if (nx) return 1;
    if (ny) return -1;
    return (x - y) * dir;
  }

  function readItem(li, i) {
    return {
      el: li,
      order: i,
      price: num(li.getAttribute("data-price")),
      age: num(li.getAttribute("data-age")),
      dropYen: num(li.getAttribute("data-drop-yen")),
      lowest: li.getAttribute("data-lowest") === "1"
    };
  }

  var OWN_KEYS = { budget: 1, age: 1, sort: 1, lowest: 1 };

  function decode(s) {
    try { return decodeURIComponent(s.replace(/\+/g, " ")); } catch (e) { return ""; }
  }

  function readState(search) {
    var st = { budget: "", age: "", sort: "default", lowest: false };
    var q = String(search || "").replace(/^\?/, "");
    if (!q) return st;
    q.split("&").forEach(function (pair) {
      var kv = pair.split("=");
      var k = decode(kv[0] || "");
      var v = decode(kv[1] || "");
      if (k === "budget" && BUDGETS[v]) st.budget = v;
      else if (k === "age" && AGES[v]) st.age = v;
      else if (k === "sort" && SORTS[v]) st.sort = v;
      else if (k === "lowest" && v === "1") st.lowest = true;
    });
    return st;
  }

  // 絞り込み以外のクエリ (utm_* など) は書き換えずに残す。
  function otherParams(search) {
    var q = String(search || "").replace(/^\?/, "");
    if (!q) return [];
    return q.split("&").filter(function (pair) {
      return pair && !OWN_KEYS[decode(pair.split("=")[0] || "")];
    });
  }

  function writeState(st, search) {
    var parts = otherParams(search);
    if (st.budget) parts.push("budget=" + st.budget);
    if (st.age) parts.push("age=" + st.age);
    if (st.sort && st.sort !== "default") parts.push("sort=" + st.sort);
    if (st.lowest) parts.push("lowest=1");
    return parts.length ? "?" + parts.join("&") : "";
  }

  function init(root, doc, win) {
    var list = doc.getElementById(root.getAttribute("data-list"));
    if (!list) return;
    var items = [];
    var lis = list.querySelectorAll(":scope > li");
    for (var i = 0; i < lis.length; i++) items.push(readItem(lis[i], i));
    if (!items.length) return;

    var pageSize = parseInt(root.getAttribute("data-page-size"), 10) || 0;
    var shown = pageSize;
    var state = readState(win.location && win.location.search);
    var countEl = root.querySelector("[data-lf-count]");
    var emptyEl = doc.querySelector("[data-lf-empty][data-list='" + list.id + "']");
    var moreBtn = doc.querySelector("[data-lf-more][data-list='" + list.id + "']");
    var resetBtns = doc.querySelectorAll("[data-lf-reset]");
    var sortSel = root.querySelector("select[data-lf='sort']");
    var lowestCb = root.querySelector("input[data-lf='lowest']");
    // ページに無い操作 (/deals/ の「過去最安値のみ」・値下げ額順) を URL から持ち込まない。
    if (!lowestCb) state.lowest = false;
    if (sortSel && !sortSel.querySelector("option[value='" + state.sort + "']")) state.sort = "default";

    function syncControls() {
      var chips = root.querySelectorAll("button[data-lf]");
      for (var i = 0; i < chips.length; i++) {
        var c = chips[i];
        var on = (state[c.getAttribute("data-lf")] || "") === c.getAttribute("data-v");
        c.setAttribute("aria-pressed", on ? "true" : "false");
        c.classList.toggle("is-on", on);
      }
      if (sortSel) sortSel.value = state.sort;
      if (lowestCb) lowestCb.checked = !!state.lowest;
    }

    function render() {
      var sorted = items.slice().sort(SORTS[state.sort] || SORTS["default"]);
      var hit = 0;
      sorted.forEach(function (it) {
        list.appendChild(it.el);
        var ok = matches(it, state);
        if (ok) hit++;
        var visible = ok && (!pageSize || hit <= shown);
        if (visible) it.el.removeAttribute("hidden");
        else it.el.setAttribute("hidden", "");
      });
      // 既定以外の並びでは順位バッジ (割引率の順位) が並びと食い違うので隠す。
      list.classList.toggle("is-resorted", state.sort !== "default");
      var filtered = !!(state.budget || state.age || state.lowest);
      if (countEl) {
        countEl.textContent = filtered
          ? items.length + " 件中 " + hit + " 件"
          : "全 " + items.length + " 件";
      }
      if (emptyEl) {
        if (hit === 0) emptyEl.removeAttribute("hidden");
        else emptyEl.setAttribute("hidden", "");
      }
      if (moreBtn) {
        var rest = pageSize ? hit - shown : 0;
        if (rest > 0) {
          moreBtn.removeAttribute("hidden");
          moreBtn.textContent = "さらに表示（残り " + rest + " 件）";
        } else {
          moreBtn.setAttribute("hidden", "");
        }
      }
      for (var r = 0; r < resetBtns.length; r++) {
        if (filtered || state.sort !== "default") resetBtns[r].removeAttribute("hidden");
        else resetBtns[r].setAttribute("hidden", "");
      }
      syncControls();
    }

    function commit() {
      shown = pageSize;
      if (win.history && win.history.replaceState && win.location) {
        win.history.replaceState(null, "", win.location.pathname + writeState(state, win.location.search) +(win.location.hash || ""));
      }
      render();
    }

    root.addEventListener("click", function (ev) {
      var t = ev.target && ev.target.closest ? ev.target.closest("button[data-lf]") : null;
      if (!t) return;
      var key = t.getAttribute("data-lf");
      var v = t.getAttribute("data-v");
      // 選択中のチップをもう一度押すと解除 (「すべて」チップは v="")。
      state[key] = state[key] === v ? "" : v;
      commit();
    });
    if (sortSel) sortSel.addEventListener("change", function () { state.sort = sortSel.value; commit(); });
    if (lowestCb) lowestCb.addEventListener("change", function () { state.lowest = lowestCb.checked; commit(); });
    for (var r = 0; r < resetBtns.length; r++) {
      resetBtns[r].addEventListener("click", function () {
        state = { budget: "", age: "", sort: "default", lowest: false };
        commit();
      });
    }
    if (moreBtn) moreBtn.addEventListener("click", function () { shown += pageSize; render(); });

    root.removeAttribute("hidden");
    render();
  }

  function boot() {
    var doc = global.document;
    if (!doc) return;
    var roots = doc.querySelectorAll("[data-list-filter]");
    for (var i = 0; i < roots.length; i++) init(roots[i], doc, global);
  }

  global.OmochaListFilter = {
    BUDGETS: BUDGETS, AGES: AGES, matches: matches, SORTS: SORTS,
    readState: readState, writeState: writeState, init: init
  };

  if (global.document) {
    if (global.document.readyState === "loading") {
      global.document.addEventListener("DOMContentLoaded", boot);
    } else {
      boot();
    }
  }
})(typeof window !== "undefined" ? window : globalThis);
