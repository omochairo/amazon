// hugo/assets/js/compare.js
// #588 比較モード: 一覧カードに「比較」トグルを付与し、選択中アイテムを
// localStorage で永続化、/compare/ ページで横並び比較表をクライアントレンダリング。
//
// 公開: window.OmochaCompare = { add, remove, clear, list, count, MAX }
// 自動マウント: DOMContentLoaded で
//   1. mountToggles()  — .product-card / .feature-item にチェックボタンを inject
//   2. mountFloatingBar() — 選択中があれば画面下に固定バー表示
//   3. hydrateComparePage() — pathname が /compare/ の場合に table を描画
//
// 仕様: 最大 4 件 / 比較は 2 件以上から / localStorage 不可環境 (Safari Private 等)
//       でも sessionStorage に fallback / どちらも不可ならインメモリで動作。
(function (global) {
  var KEY = "omcha:compare:asins";
  var MAX = 4;
  var MIN_TO_COMPARE = 2;

  // ------- storage (graceful fallback) -------
  var memStore = null;
  function _read() {
    try {
      var raw = (localStorage || sessionStorage).getItem(KEY);
      if (raw == null && memStore != null) return memStore.slice();
      return raw ? JSON.parse(raw) : [];
    } catch (e) {
      return memStore ? memStore.slice() : [];
    }
  }
  function _write(arr) {
    memStore = arr.slice();
    try {
      var s = JSON.stringify(arr);
      try { localStorage.setItem(KEY, s); } catch (e) { sessionStorage.setItem(KEY, s); }
    } catch (e) { /* private mode etc — memStore only */ }
  }

  function list() { return _read(); }
  function count() { return _read().length; }
  function has(asin) { return _read().indexOf(asin) !== -1; }
  function add(asin) {
    if (!asin) return false;
    var arr = _read();
    if (arr.indexOf(asin) !== -1) return false;
    if (arr.length >= MAX) return false;
    arr.push(asin);
    _write(arr);
    return true;
  }
  function remove(asin) {
    var arr = _read();
    var i = arr.indexOf(asin);
    if (i === -1) return false;
    arr.splice(i, 1);
    _write(arr);
    return true;
  }
  function clear() { _write([]); }

  // ------- selection toggle UI on list cards -------
  function _extractAsin(el) {
    if (el.dataset && el.dataset.asin) return el.dataset.asin.toUpperCase();
    // product-card は href="/products/<asin>/" を持つ
    var href = el.getAttribute && el.getAttribute("href");
    if (href) {
      var m = href.match(/\/products\/([a-z0-9]+)\/?/i);
      if (m) return m[1].toUpperCase();
    }
    return null;
  }

  function _makeToggle(asin) {
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "compare-toggle";
    btn.setAttribute("data-asin", asin);
    btn.setAttribute("aria-label", "比較リストに追加");
    btn.setAttribute("aria-pressed", String(has(asin)));
    btn.innerHTML = '<span class="compare-toggle-check" aria-hidden="true">' +
                    (has(asin) ? "✓" : "+") + "</span>" +
                    '<span class="compare-toggle-text">比較</span>';
    btn.addEventListener("click", function (e) {
      e.preventDefault();
      e.stopPropagation();
      var picked = has(asin);
      if (picked) {
        remove(asin);
      } else {
        if (!add(asin)) {
          _flashLimit(btn);
          return;
        }
      }
      _syncAllToggles();
      _renderBar();
    });
    return btn;
  }

  function _flashLimit(btn) {
    btn.classList.add("compare-toggle--limit");
    setTimeout(function () { btn.classList.remove("compare-toggle--limit"); }, 800);
  }

  function _syncAllToggles() {
    var nodes = document.querySelectorAll(".compare-toggle");
    for (var i = 0; i < nodes.length; i++) {
      var btn = nodes[i];
      var asin = btn.getAttribute("data-asin");
      var on = has(asin);
      btn.setAttribute("aria-pressed", String(on));
      btn.classList.toggle("is-selected", on);
      var mark = btn.querySelector(".compare-toggle-check");
      if (mark) mark.textContent = on ? "✓" : "+";
    }
  }

  function mountToggles(root) {
    root = root || document;
    var selectors = [".product-card", ".feature-item", ".ranking-item"];
    var seen = {};
    for (var s = 0; s < selectors.length; s++) {
      var nodes = root.querySelectorAll(selectors[s]);
      for (var i = 0; i < nodes.length; i++) {
        var card = nodes[i];
        if (card.querySelector(":scope > .compare-toggle")) continue;
        var asin = _extractAsin(card);
        if (!asin) continue;
        if (seen[asin]) continue;
        seen[asin] = true;
        var btn = _makeToggle(asin);
        // 先頭に挿入 (画像/タイトルより前) → カードヘッダ右上に absolute 配置
        if (card.firstChild) card.insertBefore(btn, card.firstChild);
        else card.appendChild(btn);
      }
    }
  }

  // ------- floating action bar -------
  function _renderBar() {
    var bar = document.getElementById("compare-floating-bar");
    var n = count();
    if (n === 0) {
      if (bar) bar.remove();
      return;
    }
    if (!bar) {
      bar = document.createElement("div");
      bar.id = "compare-floating-bar";
      bar.className = "compare-floating-bar";
      bar.setAttribute("role", "region");
      bar.setAttribute("aria-label", "比較リスト");
      document.body.appendChild(bar);
    }
    var canCompare = n >= MIN_TO_COMPARE;
    var asins = list();
    var url = "/compare/?asins=" + encodeURIComponent(asins.join(","));
    bar.innerHTML =
      '<div class="compare-floating-bar-inner">' +
      '  <div class="compare-floating-bar-count">' +
      '    🆚 比較中 <strong>' + n + '</strong> / ' + MAX +
      '  </div>' +
      '  <button type="button" class="compare-floating-bar-clear" aria-label="比較リストをすべて解除">解除</button>' +
      '  <a class="compare-floating-bar-cta' + (canCompare ? "" : " is-disabled") + '"' +
            (canCompare ? ' href="' + url + '"' : ' aria-disabled="true"') +
        '>比較する →</a>' +
      "</div>";
    var clearBtn = bar.querySelector(".compare-floating-bar-clear");
    clearBtn.addEventListener("click", function () {
      clear();
      _syncAllToggles();
      _renderBar();
    });
  }

  function mountFloatingBar() { _renderBar(); }

  // ------- /compare/ page hydration -------
  function _readUrlAsins() {
    var params = new URLSearchParams(global.location.search || "");
    var raw = params.get("asins") || "";
    return raw
      .split(",")
      .map(function (s) { return s.trim().toUpperCase(); })
      .filter(function (s) { return /^B0[A-Z0-9]{8}$/.test(s); })
      .slice(0, MAX);
  }

  function _scoreTierClass(s) {
    if (s >= 90) return "score-gold";
    if (s >= 75) return "score-silver";
    return "score-bronze";
  }

  function _formatPrice(n) {
    if (!n) return "—";
    return "¥" + Number(n).toLocaleString("ja-JP");
  }

  function _minPrice(item) {
    var ps = [item.price_amazon, item.price_rakuten, item.price_yahoo];
    var min = 0;
    for (var i = 0; i < ps.length; i++) {
      var v = Number(ps[i] || 0);
      if (v > 0 && (min === 0 || v < min)) min = v;
    }
    return min;
  }

  // index.json は age_range が空文字のことが多い。product_card.html と同じ規則で
  // age_min_months から「Nヶ月〜 / N歳〜 / N.5歳〜」を組み立てる。
  function _formatAge(item) {
    var range = (item.age_range || "").trim();
    if (range) return range;
    var m = Number(item.age_min_months || 0);
    if (!m && m !== 0) return "—";
    if (m <= 0) return "0歳〜";
    if (m < 12) return m + "ヶ月〜";
    var years = Math.floor(m / 12);
    var rem = m % 12;
    if (rem === 0) return years + "歳〜";
    if (rem === 6) return years + ".5歳〜";
    return (m / 12).toFixed(1) + "歳〜";
  }

  function _axisBar(label, icon, value) {
    if (!value && value !== 0) return "";
    var pct = Math.round((value / 5) * 100);
    return '<div class="score-minichart-row" data-axis="' + label + '">' +
           '  <span class="score-minichart-label">' + icon + " " + label + "</span>" +
           '  <span class="score-minichart-bar" role="meter" aria-valuemin="0" aria-valuemax="5" aria-valuenow="' + value.toFixed(1) + '">' +
           '    <span class="score-minichart-bar-fill" style="width: ' + pct + '%"></span>' +
           "  </span>" +
           '  <span class="score-minichart-value">' + value.toFixed(1) + "</span>" +
           "</div>";
  }

  function _renderCompareTable(items) {
    if (!items.length) {
      return '<p class="compare-empty">比較する商品が選択されていません。商品一覧から「比較」ボタンで追加してください。</p>';
    }
    var rows = [];
    rows.push('<tr class="compare-row compare-row--image"><th scope="row">画像</th>');
    items.forEach(function (it) {
      rows.push('<td><a href="' + it.permalink + '">' +
                (it.image ? '<img src="' + it.image + '" alt="' + (it.product_name || it.title || "") + '" loading="lazy">' : "🧸") +
                "</a></td>");
    });
    rows.push("</tr>");

    rows.push('<tr class="compare-row compare-row--name"><th scope="row">商品名</th>');
    items.forEach(function (it) {
      rows.push('<td><a class="compare-name-link" href="' + it.permalink + '">' +
                (it.brand ? '<span class="compare-brand">' + it.brand + "</span>" : "") +
                (it.product_name || it.title || "") + "</a>" +
                '<button type="button" class="compare-remove" data-asin="' + (it.asin || "") + '" aria-label="この商品を比較から外す">×</button></td>');
    });
    rows.push("</tr>");

    rows.push('<tr class="compare-row compare-row--score"><th scope="row">知育スコア</th>');
    items.forEach(function (it) {
      var s = Number(it.ivs_score_100 || 0);
      if (!s) {
        rows.push('<td><span class="compare-score-empty">—</span></td>');
        return;
      }
      rows.push('<td><span class="compare-score-badge ' + _scoreTierClass(s) + '">🏆 ' +
                "<strong>" + s + "</strong><span class=\"compare-score-suffix\">/100</span></span></td>");
    });
    rows.push("</tr>");

    rows.push('<tr class="compare-row compare-row--axes"><th scope="row">スコア内訳</th>');
    items.forEach(function (it) {
      var ax = it.ivs_axes || {};
      var html = "";
      html += _axisBar("知育", "📚", Number(ax.education || 0));
      html += _axisBar("長く遊べる", "🧸", Number(ax.longevity || 0));
      html += _axisBar("コスパ", "💴", Number(ax.cost_performance || 0));
      html += _axisBar("安全性", "🛡️", Number(ax.safety || 0));
      rows.push('<td><div class="score-minichart compare-axes">' + html + "</div></td>");
    });
    rows.push("</tr>");

    rows.push('<tr class="compare-row compare-row--price"><th scope="row">最安値</th>');
    items.forEach(function (it) {
      rows.push("<td>" + _formatPrice(_minPrice(it)) + "</td>");
    });
    rows.push("</tr>");

    rows.push('<tr class="compare-row compare-row--age"><th scope="row">対象年齢</th>');
    items.forEach(function (it) {
      rows.push("<td>" + _formatAge(it) + "</td>");
    });
    rows.push("</tr>");

    rows.push('<tr class="compare-row compare-row--cta"><th scope="row">詳細</th>');
    items.forEach(function (it) {
      rows.push('<td><a class="compare-cta" href="' + it.permalink + '">📝 詳しく読む →</a></td>');
    });
    rows.push("</tr>");

    var cols = items.length;
    return '<div class="compare-table-wrap"><table class="compare-table" data-cols="' + cols + '"><tbody>' +
           rows.join("") + "</tbody></table></div>";
  }

  function hydrateComparePage() {
    var root = document.getElementById("compare-root");
    if (!root) return;
    var asins = _readUrlAsins();
    if (!asins.length) {
      // URL に何もない → localStorage から復元
      asins = _read();
      if (asins.length && global.history && global.history.replaceState) {
        var u = global.location.pathname + "?asins=" + encodeURIComponent(asins.join(","));
        global.history.replaceState(null, "", u);
      }
    }
    if (!asins.length) {
      root.innerHTML = _renderCompareTable([]);
      return;
    }
    root.innerHTML = '<p class="compare-loading">読み込み中...</p>';
    // /index.json から該当 ASIN のメタを引く
    var idxUrl = "/index.json";
    fetch(idxUrl, { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (index) {
        var byAsin = {};
        for (var i = 0; i < index.length; i++) {
          var entry = index[i];
          var m = (entry.permalink || "").match(/\/products\/([a-z0-9]+)\/?/i);
          if (m) {
            var k = m[1].toUpperCase();
            entry.asin = k;
            byAsin[k] = entry;
          }
        }
        var ordered = [];
        for (var j = 0; j < asins.length; j++) {
          if (byAsin[asins[j]]) ordered.push(byAsin[asins[j]]);
        }
        root.innerHTML = _renderCompareTable(ordered);
        // remove buttons
        var rms = root.querySelectorAll(".compare-remove");
        for (var k = 0; k < rms.length; k++) {
          rms[k].addEventListener("click", function (e) {
            var a = e.currentTarget.getAttribute("data-asin");
            if (!a) return;
            remove(a);
            // URL も更新
            var rest = list();
            var u = global.location.pathname +
                    (rest.length ? "?asins=" + encodeURIComponent(rest.join(",")) : "");
            if (global.history && global.history.replaceState) {
              global.history.replaceState(null, "", u);
            }
            hydrateComparePage();
            _renderBar();
          });
        }
      })
      .catch(function () {
        root.innerHTML = '<p class="compare-error">比較データの読み込みに失敗しました。時間を置いてお試しください。</p>';
      });
  }

  // ------- expose + auto-mount -------
  global.OmochaCompare = {
    MAX: MAX,
    MIN_TO_COMPARE: MIN_TO_COMPARE,
    add: add, remove: remove, clear: clear, list: list, count: count, has: has,
    mountToggles: mountToggles,
    mountFloatingBar: mountFloatingBar,
    hydrateComparePage: hydrateComparePage,
  };

  function onReady(fn) {
    if (document.readyState !== "loading") fn();
    else document.addEventListener("DOMContentLoaded", fn);
  }
  onReady(function () {
    mountToggles();
    mountFloatingBar();
    if (global.location && /\/compare\/?$/.test(global.location.pathname)) {
      hydrateComparePage();
    }
  });
})(window);
