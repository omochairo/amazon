// hugo/assets/js/recently_viewed.js
// #1367 最近見た商品: 商品ページ訪問時に自動で localStorage 記録 (最大 12 件)、
// ホーム下部 + 商品ページ脇に carousel として表示。
//
// 公開: window.OmochaRecent = { push, list, clear, render }
// 自動:
//   1. 商品ページ (/products/<asin>/) を開いたら _autoPush で記録
//   2. .recently-viewed-mount (hidden) があれば render
//
// 仕様:
// - 最大 12 件、重複 ASIN は viewed_at 更新で先頭へ
// - 自分自身は除外
// - localStorage 不可なら sessionStorage / memory fallback
(function (global) {
  var KEY = "omcha:recent:v1";
  var MAX = 12;
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
    } catch (e) { /* memory only */ }
  }

  function list() { return _read(); }
  function clear() { _write([]); }

  function remove(asin) {
    if (!asin) return false;
    var arr = _read();
    var next = arr.filter(function (r) { return r.asin !== asin; });
    if (next.length === arr.length) return false;
    _write(next);
    return true;
  }

  function push(record) {
    if (!record || !record.asin) return false;
    var arr = _read();
    // remove existing same ASIN
    for (var i = arr.length - 1; i >= 0; i--) {
      if (arr[i].asin === record.asin) arr.splice(i, 1);
    }
    arr.unshift({
      asin: record.asin,
      title: record.title || null,
      image: record.image || null,
      score: record.score || null,
      min_price: record.min_price || null,
      url: record.url || ("/products/" + record.asin.toLowerCase() + "/"),
      viewed_at: new Date().toISOString()
    });
    if (arr.length > MAX) arr = arr.slice(0, MAX);
    _write(arr);
    return true;
  }

  function _currentAsin() {
    var m = global.location.pathname.match(/\/products\/([a-z0-9]+)\/?/i);
    return m ? m[1].toUpperCase() : null;
  }

  function _autoPushFromArticle() {
    var asin = _currentAsin();
    if (!asin) return;
    // メタデータを記事ページから extract
    var title = document.querySelector(".post-title");
    var image = document.querySelector(".post-content img, .cover img, article img");
    var meta = {
      asin: asin,
      title: title ? (title.textContent || "").trim() : document.title,
      image: image ? image.getAttribute("src") : null,
      score: null,
      min_price: null,
      url: global.location.pathname
    };
    var scoreEl = document.querySelector("[data-ivs100]");
    if (scoreEl) {
      var s = parseInt(scoreEl.getAttribute("data-ivs100"), 10);
      if (s > 0) meta.score = s;
    }
    push(meta);
  }

  function _renderTo(mount) {
    var arr = _read();
    var current = _currentAsin();
    // 自分自身は除外
    if (current) arr = arr.filter(function (r) { return r.asin !== current; });
    if (arr.length === 0) {
      mount.setAttribute("hidden", "");
      return;
    }
    mount.removeAttribute("hidden");
    var grid = mount.querySelector(".recently-viewed-grid");
    if (!grid) return;
    grid.innerHTML = "";
    arr.forEach(function (r) {
      // item = card (<a>) + 個別削除 (×) ボタン。× は anchor の外側 sibling に置き
      // (anchor 内 button は HTML 不正・遷移する) absolute 配置で右上に重ねる。
      var item = document.createElement("div");
      item.className = "recently-viewed-item";
      item.setAttribute("data-asin", r.asin);

      var card = document.createElement("a");
      card.className = "recently-viewed-card";
      card.href = r.url || ("/products/" + r.asin.toLowerCase() + "/");
      var price = r.min_price ? ('<span class="recently-viewed-price">最安 ¥' + Number(r.min_price).toLocaleString() + '</span>') : "";
      var score = r.score ? ('<span class="recently-viewed-score">🏆 ' + r.score + '点</span>') : "";
      card.innerHTML =
        (r.image ? '<img class="recently-viewed-image" src="' + r.image + '" alt="" loading="lazy" referrerpolicy="no-referrer">' : '<div class="recently-viewed-image-fallback">🧸</div>') +
        '<div class="recently-viewed-body">' +
        '<div class="recently-viewed-title">' + (r.title || r.asin) + '</div>' +
        '<div class="recently-viewed-meta">' + score + price + '</div>' +
        '</div>';

      var del = document.createElement("button");
      del.type = "button";
      del.className = "recently-viewed-remove";
      del.setAttribute("aria-label", "最近見た商品から削除");
      del.setAttribute("data-asin", r.asin);
      del.textContent = "×";

      item.appendChild(card);
      item.appendChild(del);
      grid.appendChild(item);
    });

    // × クリックの delegated handler は mount ごとに 1 回だけ束ねる
    if (!mount._rvBound) {
      mount._rvBound = true;
      mount.addEventListener("click", function (ev) {
        var btn = ev.target.closest ? ev.target.closest(".recently-viewed-remove") : null;
        if (!btn) return;
        ev.preventDefault();
        ev.stopPropagation();
        var asin = btn.getAttribute("data-asin");
        if (remove(asin)) render(document);
      });
    }
  }

  function render(root) {
    root = root || document;
    var mounts = root.querySelectorAll(".recently-viewed-mount");
    for (var i = 0; i < mounts.length; i++) {
      _renderTo(mounts[i]);
    }
    // carousel_swipe.js (同居) の矢印 disabled 状態を再計算させる。
    // 件数変化や × 削除後に scrollWidth が変わるため resize を擬似発火。
    try { global.dispatchEvent(new Event("resize")); } catch (e) { /* noop */ }
  }

  function boot() {
    try {
      // 商品ページなら自動 push (render 前に実行して当該 ASIN を先頭に)
      if (/^\/products\/[a-z0-9]+\/?$/i.test(global.location.pathname)) {
        _autoPushFromArticle();
      }
      render(document);
    } catch (e) { /* never break page */ }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }

  global.OmochaRecent = { push: push, list: list, clear: clear, remove: remove, render: render };
})(typeof window !== "undefined" ? window : this);
