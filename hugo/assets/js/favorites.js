// hugo/assets/js/favorites.js
// #1366 お気に入り機能: 商品カードに ♡ アイコンを追加し、選択中 ASIN を
// localStorage で永続化、/favorites/ ページで一覧表示、ヘッダにバッジで件数表示。
//
// 公開: window.OmochaFavorites = { add, remove, toggle, has, list, count, clear, snapshot }
// 自動マウント: DOMContentLoaded で
//   1. mountToggles() — .product-card / .feature-item / .ranking-item に ♡ inject
//   2. mountHeaderBadge() — #menu に ♡ <count> badge を inject
//   3. hydrateFavoritesPage() — /favorites/ の場合に一覧描画
//
// 仕様:
// - 上限なし (実用 100 件想定)
// - 価格 snapshot を同時保存し、将来の値下げ通知 (#1365 Layer 1-④) と接続
// - localStorage 不可環境では sessionStorage / memory fallback
// - 既存 compare.js と共存 (♡ と 📊 が並ぶ)
(function (global) {
  var KEY = "omcha:favorites:v1";
  var memStore = null;

  // ------- storage (graceful fallback) -------
  function _read() {
    try {
      var raw = (localStorage || sessionStorage).getItem(KEY);
      if (raw == null && memStore != null) return _clone(memStore);
      return raw ? JSON.parse(raw) : { asins: [], snapshots: {} };
    } catch (e) {
      return memStore ? _clone(memStore) : { asins: [], snapshots: {} };
    }
  }
  function _write(data) {
    memStore = _clone(data);
    try {
      var s = JSON.stringify(data);
      try { localStorage.setItem(KEY, s); } catch (e) { sessionStorage.setItem(KEY, s); }
    } catch (e) { /* memory only */ }
  }
  function _clone(o) {
    return { asins: (o.asins || []).slice(), snapshots: Object.assign({}, o.snapshots || {}) };
  }

  function list() { return _read().asins; }
  function count() { return _read().asins.length; }
  function has(asin) { return _read().asins.indexOf(asin) !== -1; }
  function snapshot(asin) { return _read().snapshots[asin] || null; }

  function add(asin, meta) {
    if (!asin) return false;
    var data = _read();
    if (data.asins.indexOf(asin) !== -1) return false;
    data.asins.unshift(asin);
    if (meta) {
      data.snapshots[asin] = {
        title: meta.title || null,
        image: meta.image || null,
        score: meta.score || null,
        min_price: meta.min_price || null,
        url: meta.url || ("/products/" + asin.toLowerCase() + "/"),
        captured_at: new Date().toISOString()
      };
    }
    _write(data);
    return true;
  }
  function remove(asin) {
    var data = _read();
    var i = data.asins.indexOf(asin);
    if (i === -1) return false;
    data.asins.splice(i, 1);
    delete data.snapshots[asin];
    _write(data);
    return true;
  }
  function toggle(asin, meta) {
    return has(asin) ? (remove(asin), false) : (add(asin, meta), true);
  }
  function clear() { _write({ asins: [], snapshots: {} }); }

  // ------- card metadata extraction -------
  function _extractAsin(el) {
    if (el.dataset && el.dataset.asin) return el.dataset.asin.toUpperCase();
    var href = el.getAttribute && el.getAttribute("href");
    if (href) {
      var m = href.match(/\/products\/([a-z0-9]+)\/?/i);
      if (m) return m[1].toUpperCase();
    }
    return null;
  }

  function _extractMeta(card, asin) {
    var meta = { title: null, image: null, score: null, min_price: null, url: null };
    if (card.getAttribute) {
      meta.url = card.getAttribute("href") || null;
      var ivs100 = card.getAttribute("data-ivs100");
      if (ivs100 && parseInt(ivs100, 10) > 0) meta.score = parseInt(ivs100, 10);
    }
    var img = card.querySelector && card.querySelector("img");
    if (img) {
      meta.image = img.getAttribute("src") || null;
      if (!meta.title) meta.title = img.getAttribute("alt") || null;
    }
    var nameEl = card.querySelector && card.querySelector(".product-card-name, .ranking-title, .feature-item-name");
    if (nameEl) meta.title = (nameEl.textContent || "").trim();
    var priceEl = card.querySelector && card.querySelector(".product-card-score-price");
    if (priceEl) {
      var pm = (priceEl.textContent || "").replace(/[^0-9]/g, "");
      if (pm) meta.min_price = parseInt(pm, 10);
    }
    return meta;
  }

  // ------- toggle button -------
  function _makeToggle(asin, card) {
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "fav-toggle";
    btn.setAttribute("data-asin", asin);
    btn.setAttribute("aria-pressed", String(has(asin)));
    btn.setAttribute("aria-label", has(asin) ? "お気に入りから外す" : "お気に入りに追加");
    btn.innerHTML = '<span class="fav-toggle-icon" aria-hidden="true">' +
                    (has(asin) ? "♥" : "♡") + "</span>";
    if (has(asin)) btn.classList.add("is-on");
    btn.addEventListener("click", function (e) {
      e.preventDefault();
      e.stopPropagation();
      var meta = _extractMeta(card, asin);
      var added = toggle(asin, meta);
      btn.classList.toggle("is-on", added);
      btn.setAttribute("aria-pressed", String(added));
      btn.setAttribute("aria-label", added ? "お気に入りから外す" : "お気に入りに追加");
      var icon = btn.querySelector(".fav-toggle-icon");
      if (icon) icon.textContent = added ? "♥" : "♡";
      _syncAllToggles();
      _renderHeaderBadge();
      if (added) _bumpAnim(btn);
    });
    return btn;
  }

  function _bumpAnim(btn) {
    btn.classList.add("fav-toggle--bump");
    setTimeout(function () { btn.classList.remove("fav-toggle--bump"); }, 360);
  }

  function _syncAllToggles() {
    var nodes = document.querySelectorAll(".fav-toggle");
    for (var i = 0; i < nodes.length; i++) {
      var btn = nodes[i];
      var asin = btn.getAttribute("data-asin");
      var on = has(asin);
      btn.classList.toggle("is-on", on);
      btn.setAttribute("aria-pressed", String(on));
      btn.setAttribute("aria-label", on ? "お気に入りから外す" : "お気に入りに追加");
      var icon = btn.querySelector(".fav-toggle-icon");
      if (icon) icon.textContent = on ? "♥" : "♡";
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
        if (card.querySelector(":scope > .fav-toggle")) continue;
        var asin = _extractAsin(card);
        if (!asin) continue;
        if (seen[asin]) continue;
        seen[asin] = true;
        var btn = _makeToggle(asin, card);
        if (card.firstChild) card.insertBefore(btn, card.firstChild);
        else card.appendChild(btn);
      }
    }
  }

  // ------- header badge -------
  function _renderHeaderBadge() {
    var n = count();
    var existing = document.querySelector("#menu .fav-menu-link");
    if (!existing) {
      var menu = document.getElementById("menu");
      if (!menu) return;
      var li = document.createElement("li");
      li.className = "fav-menu-link-item";
      li.innerHTML = '<a class="fav-menu-link" href="/favorites/" aria-label="お気に入り一覧">' +
                     '<span class="fav-menu-icon" aria-hidden="true">♡</span>' +
                     '<span class="fav-menu-count" data-count="0">0</span>' +
                     '</a>';
      menu.appendChild(li);
      existing = li.querySelector(".fav-menu-link");
    }
    var countEl = existing.querySelector(".fav-menu-count");
    if (countEl) {
      countEl.textContent = String(n);
      countEl.setAttribute("data-count", String(n));
      existing.classList.toggle("has-items", n > 0);
    }
  }

  function mountHeaderBadge() { _renderHeaderBadge(); }

  // ------- /favorites/ page hydration -------
  function hydrateFavoritesPage() {
    if (!/\/favorites\/?$/.test(global.location.pathname)) return;
    var container = document.getElementById("favorites-list");
    var emptyEl = document.getElementById("favorites-empty");
    if (!container) return;
    var data = _read();
    if (data.asins.length === 0) {
      container.style.display = "none";
      if (emptyEl) emptyEl.style.display = "";
      return;
    }
    if (emptyEl) emptyEl.style.display = "none";
    container.style.display = "";
    container.innerHTML = "";
    data.asins.forEach(function (asin) {
      var snap = data.snapshots[asin] || {};
      var url = snap.url || ("/products/" + asin.toLowerCase() + "/");
      var card = document.createElement("article");
      card.className = "favorites-card";
      card.setAttribute("data-asin", asin);
      var price = snap.min_price ? ("最安 ¥" + Number(snap.min_price).toLocaleString()) : "";
      var score = snap.score ? ("🏆 " + snap.score + "点") : "";
      card.innerHTML =
        '<a class="favorites-card-link" href="' + url + '">' +
        (snap.image ? '<img class="favorites-card-image" src="' + snap.image + '" alt="" loading="lazy" referrerpolicy="no-referrer">' : '<div class="favorites-card-image-fallback">🧸</div>') +
        '<div class="favorites-card-body">' +
        '<h3 class="favorites-card-title">' + (snap.title || asin) + '</h3>' +
        '<div class="favorites-card-meta">' +
          (score ? '<span class="favorites-card-score">' + score + '</span>' : '') +
          (price ? '<span class="favorites-card-price">' + price + '</span>' : '') +
        '</div></div></a>' +
        '<button type="button" class="favorites-card-remove" aria-label="お気に入りから外す">×</button>';
      card.querySelector(".favorites-card-remove").addEventListener("click", function (e) {
        e.preventDefault();
        e.stopPropagation();
        remove(asin);
        card.remove();
        _syncAllToggles();
        _renderHeaderBadge();
        if (count() === 0) hydrateFavoritesPage();
      });
      container.appendChild(card);
    });
  }

  // ------- boot -------
  function boot() {
    try {
      mountToggles(document);
      mountHeaderBadge();
      hydrateFavoritesPage();
    } catch (e) { /* never break page */ }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }

  global.OmochaFavorites = {
    add: add, remove: remove, toggle: toggle, has: has,
    list: list, count: count, clear: clear, snapshot: snapshot
  };
})(typeof window !== "undefined" ? window : this);
