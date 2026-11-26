// hugo/assets/js/games_filter.js
// /family-games/ (家族で遊べるゲーム・年齢別。navi-brain#216) の比較表とランキングを、
// ゲームの種類と予算で絞り込む。
//
// 全件はサーバー側で描いておき (クローラーと JS 無効の閲覧者は全件を見る)、この JS は
// [data-games-row] の data-type (種類) / data-price (最安値) を読んで表示/非表示を切り替えるだけ。
// 「迷ったらこの 3 つ」は絞らない (年齢帯ごとの答えなので、条件で消すと答えが無くなる)。
// 選んだ条件は URL のクエリ (?kind=&budget=) に残し、共有・戻るで復元する。
(function (global) {
  "use strict";

  // 上限 (円、この値ちょうどを含む)。family_games_page.py の BUDGETS と同じ
  var BUDGETS = { "u1000": 1000, "u3000": 3000, "u5000": 5000 };

  // 壊れた % エンコード (?q=100% など、ほかのクエリ) で例外を投げて絞り込み欄ごと消えないように
  function dec(s) {
    try { return decodeURIComponent(s); } catch (e) { return s; }
  }

  function matches(row, state) {
    if (state.kind && row.kind !== state.kind) return false;
    if (state.budget) {
      var max = BUDGETS[state.budget];
      if (max !== undefined) {
        if (!(row.price > 0) || row.price > max) return false;
      }
    }
    return true;
  }

  // kinds: 知っている種類のキーの配列。知らないキー・値は捨てる
  function readState(search, kinds) {
    var state = { kind: "", budget: "" };
    var q = (search || "").replace(/^\?/, "").split("&");
    for (var i = 0; i < q.length; i++) {
      var kv = q[i].split("=");
      var k = dec(kv[0] || ""), v = dec(kv[1] || "");
      if (k === "kind" && kinds.indexOf(v) >= 0) state.kind = v;
      if (k === "budget" && Object.prototype.hasOwnProperty.call(BUDGETS, v)) state.budget = v;
    }
    return state;
  }

  function writeState(search, state) {
    var keep = [];
    var q = (search || "").replace(/^\?/, "").split("&");
    for (var i = 0; i < q.length; i++) {
      if (!q[i]) continue;
      var k = dec(q[i].split("=")[0] || "");
      if (k !== "kind" && k !== "budget") keep.push(q[i]);
    }
    if (state.kind) keep.push("kind=" + encodeURIComponent(state.kind));
    if (state.budget) keep.push("budget=" + encodeURIComponent(state.budget));
    return keep.length ? "?" + keep.join("&") : "";
  }

  function init(root, doc, win) {
    var page = root.closest ? (root.closest(".feature-content") || doc) : doc;
    var chips = root.querySelectorAll("[data-gf-kind], [data-gf-budget]");
    var kinds = [];
    for (var i = 0; i < chips.length; i++) {
      var kk = chips[i].getAttribute("data-gf-kind");
      if (kk) kinds.push(kk);
    }
    var rows = page.querySelectorAll("[data-games-row]");
    var bands = page.querySelectorAll("[data-games-band]");
    var count = root.querySelector("[data-gf-count]");
    var state = readState(win.location ? win.location.search : "", kinds);

    function render() {
      for (var c = 0; c < chips.length; c++) {
        var ch = chips[c];
        var isKind = ch.hasAttribute("data-gf-kind");
        var val = isKind ? ch.getAttribute("data-gf-kind") : ch.getAttribute("data-gf-budget");
        var on = (isKind ? state.kind : state.budget) === val;
        ch.setAttribute("aria-pressed", on ? "true" : "false");
        ch.classList.toggle("is-active", on);
      }
      var shownCards = 0;
      for (var r = 0; r < rows.length; r++) {
        var el = rows[r];
        var ok = matches({ kind: el.getAttribute("data-type") || "",
                           price: Number(el.getAttribute("data-price") || "0") }, state);
        if (ok) el.removeAttribute("hidden"); else el.setAttribute("hidden", "");
        if (ok && el.tagName === "LI") shownCards++;
      }
      for (var b = 0; b < bands.length; b++) {
        var band = bands[b];
        var any = band.querySelector("li[data-games-row]:not([hidden])");
        var empty = band.querySelector("[data-gf-empty]");
        if (empty) { if (any) empty.setAttribute("hidden", ""); else empty.removeAttribute("hidden"); }
      }
      if (count) count.textContent = (state.kind || state.budget) ? shownCards + "件" : "";
    }

    function commit() {
      render();
      if (win.history && win.history.replaceState && win.location) {
        win.history.replaceState(null, "", win.location.pathname + writeState(win.location.search, state) + win.location.hash);
      }
    }

    for (var j = 0; j < chips.length; j++) {
      chips[j].addEventListener("click", function (ev) {
        var t = ev.currentTarget;
        if (t.hasAttribute("data-gf-kind")) state.kind = t.getAttribute("data-gf-kind");
        else state.budget = t.getAttribute("data-gf-budget");
        commit();
      });
    }
    root.removeAttribute("hidden");
    render();
  }

  function boot() {
    var doc = global.document;
    if (!doc) return;
    var roots = doc.querySelectorAll("[data-games-filter]");
    for (var i = 0; i < roots.length; i++) init(roots[i], doc, global);
  }

  global.OmochaGamesFilter = { BUDGETS: BUDGETS, matches: matches, readState: readState, writeState: writeState };

  if (global.document) {
    if (global.document.readyState === "loading") {
      global.document.addEventListener("DOMContentLoaded", boot);
    } else {
      boot();
    }
  }
})(typeof window !== "undefined" ? window : globalThis);
