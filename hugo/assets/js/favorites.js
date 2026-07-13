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
    // #3055 E3: ?share=<asin>.<asin>... が付いていれば共有ビューに切替え、
    // 自分のお気に入り一覧 (localStorage 由来) は描画しない。
    var shareAsins = _parseShareParam();
    if (shareAsins) {
      _renderSharedView(shareAsins);
      return;
    }
    var sharedEl = document.getElementById("favorites-shared");
    if (sharedEl) sharedEl.style.display = "none";

    var container = document.getElementById("favorites-list");
    var emptyEl = document.getElementById("favorites-empty");
    if (!container) return;
    var data = _read();
    _wireShareButton();
    _renderShareBar(data.asins.length);
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

  // ------- 共有 URL (#3055 E3) -------
  // ユースケース: プレゼント選び相談で候補リストを LINE 等で送る。バックエンド不要、
  // URL パラメータ (小文字 ASIN を "." 区切り、先頭 40 件) + /index.json でメタ解決。
  var SHARE_ASIN_MAX = 40;
  var SHARE_ASIN_RE = /^[a-z0-9]{8,14}$/;
  var _toastTimer = null;

  function _showToast(msg) {
    var el = document.getElementById("favorites-toast");
    if (!el) return;
    el.textContent = msg;
    el.hidden = false;
    el.classList.add("is-visible");
    if (_toastTimer) clearTimeout(_toastTimer);
    _toastTimer = setTimeout(function () {
      el.classList.remove("is-visible");
      el.hidden = true;
    }, 2000);
  }

  function _fallbackPrompt(url) {
    try { global.prompt("URL をコピーしてください:", url); } catch (e) { /* ignore */ }
  }

  function _renderShareBar(n) {
    var bar = document.getElementById("favorites-share-bar");
    if (!bar) return;
    bar.style.display = n > 0 ? "" : "none";
  }

  var _shareBtnWired = false;
  function _wireShareButton() {
    if (_shareBtnWired) return;
    var btn = document.getElementById("favorites-share-btn");
    if (!btn) return;
    _shareBtnWired = true;
    btn.addEventListener("click", function () {
      var asins = list().slice(0, SHARE_ASIN_MAX).map(function (a) { return a.toLowerCase(); });
      if (!asins.length) return;
      var url = global.location.origin + "/favorites/?share=" + asins.join(".");
      var title = "おもちゃいろ お気に入りリスト";

      if (navigator.share) {
        navigator.share({ title: title, url: url }).catch(function () { /* user cancelled, no-op */ });
        return;
      }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(url).then(function () {
          _showToast("リンクをコピーしました");
        }).catch(function () {
          _fallbackPrompt(url);
        });
        return;
      }
      _fallbackPrompt(url);
    });
  }

  // 受信側: ?share=<asin>.<asin>... を parse。不正な要素は捨て、上限 SHARE_ASIN_MAX。
  function _parseShareParam() {
    var qs = global.location.search || "";
    var m = qs.match(/[?&]share=([^&]*)/);
    if (!m || !m[1]) return null;
    var raw;
    try { raw = decodeURIComponent(m[1]); } catch (e) { raw = m[1]; }
    var asins = raw.split(".")
      .map(function (s) { return s.toLowerCase(); })
      .filter(function (s) { return SHARE_ASIN_RE.test(s); })
      .slice(0, SHARE_ASIN_MAX)
      .map(function (s) { return s.toUpperCase(); });
    return asins.length ? asins : null;
  }

  function _renderSharedView(asins) {
    var listContainer = document.getElementById("favorites-list");
    var emptyEl = document.getElementById("favorites-empty");
    var shareBar = document.getElementById("favorites-share-bar");
    var shared = document.getElementById("favorites-shared");
    var sharedList = document.getElementById("favorites-shared-list");
    var titleEl = document.getElementById("favorites-shared-title");
    if (!shared || !sharedList) return;

    if (listContainer) listContainer.style.display = "none";
    if (emptyEl) emptyEl.style.display = "none";
    if (shareBar) shareBar.style.display = "none";
    shared.style.display = "";
    if (titleEl) titleEl.textContent = "共有されたお気に入りリスト (" + asins.length + "件)";

    _loadCatalogEntries().then(function (map) {
      sharedList.innerHTML = "";
      asins.forEach(function (asin) {
        sharedList.appendChild(_buildSharedCard(asin, map[asin]));
      });
      _wireSharedAddAll(asins);
    });
  }

  // XSS 対策: title 等は必ず textContent で挿入する (favorites.js 既存の自分リスト側は
  // innerHTML だが、共有側は他人が組み立てた URL から来る asin をキーに外部 JSON の値を
  // 描画するため、新規コードでは escape 不要な DOM API のみを使う)。
  function _buildSharedCard(asin, entry) {
    var url = (entry && entry.url) || ("/products/" + asin.toLowerCase() + "/");
    var title = (entry && entry.title) || asin;
    var image = entry && entry.image;
    var score = entry && entry.score;
    var minPrice = entry && entry.min_price;

    var card = document.createElement("article");
    card.className = "favorites-card";
    card.setAttribute("data-asin", asin);

    var link = document.createElement("a");
    link.className = "favorites-card-link";
    link.setAttribute("href", url);

    if (image) {
      var img = document.createElement("img");
      img.className = "favorites-card-image";
      img.setAttribute("src", image);
      img.setAttribute("alt", "");
      img.setAttribute("loading", "lazy");
      img.setAttribute("referrerpolicy", "no-referrer");
      link.appendChild(img);
    } else {
      var fallback = document.createElement("div");
      fallback.className = "favorites-card-image-fallback";
      fallback.textContent = "🧸";
      link.appendChild(fallback);
    }

    var body = document.createElement("div");
    body.className = "favorites-card-body";

    var h3 = document.createElement("h3");
    h3.className = "favorites-card-title";
    h3.textContent = title;
    body.appendChild(h3);

    var meta = document.createElement("div");
    meta.className = "favorites-card-meta";
    if (score) {
      var scoreEl = document.createElement("span");
      scoreEl.className = "favorites-card-score";
      scoreEl.textContent = "🏆 " + score + "点";
      meta.appendChild(scoreEl);
    }
    if (minPrice) {
      var priceEl = document.createElement("span");
      priceEl.className = "favorites-card-price";
      priceEl.textContent = "最安 ¥" + Number(minPrice).toLocaleString();
      meta.appendChild(priceEl);
    }
    body.appendChild(meta);
    link.appendChild(body);
    card.appendChild(link);

    var addBtn = document.createElement("button");
    addBtn.type = "button";
    addBtn.className = "favorites-card-add";
    addBtn.setAttribute("data-asin", asin);
    var already = has(asin);
    addBtn.textContent = already ? "追加済み" : "♡ 自分のリストに追加";
    addBtn.disabled = already;
    addBtn.addEventListener("click", function () {
      add(asin, { title: title, image: image || null, score: score || null, min_price: minPrice || null, url: url });
      addBtn.textContent = "追加済み";
      addBtn.disabled = true;
      _renderHeaderBadge();
    });
    card.appendChild(addBtn);

    return card;
  }

  function _wireSharedAddAll(asins) {
    var btn = document.getElementById("favorites-shared-addall");
    if (!btn) return;
    btn.onclick = function () {
      _loadCatalogEntries().then(function (map) {
        asins.forEach(function (asin) {
          var entry = map[asin] || {};
          add(asin, {
            title: entry.title || asin,
            image: entry.image || null,
            score: entry.score || null,
            min_price: entry.min_price || null,
            url: entry.url || ("/products/" + asin.toLowerCase() + "/")
          });
        });
        var addButtons = document.querySelectorAll(".favorites-card-add");
        for (var i = 0; i < addButtons.length; i++) {
          addButtons[i].textContent = "追加済み";
          addButtons[i].disabled = true;
        }
        _renderHeaderBadge();
        _showToast("すべて自分のリストに追加しました");
      });
    };
  }

  // ------- 値下げ検出 (#1365 Layer 1-④) -------
  // 登録時 snapshot.min_price と現在の最安値 (index.json の price_* 非ゼロ最小) を
  // 比較し、DROP_THRESHOLD 以上下落していたら 🔻 バッジを描画する。リピート訪問の
  // 動機を作る核心機能。snapshot は登録時価格で固定 (現価格は触らない)。
  var CATALOG_URL = "/index.json";
  var DROP_THRESHOLD = 0.05; // 5% 以上で値下げ扱い
  var _catalogPromise = null;
  var _catalogEntriesPromise = null;
  var _indexRowsPromise = null;

  // index.json (全 rows) の fetch は 1 回だけ行い、_loadCatalog (最安値 map) と
  // _loadCatalogEntries (共有リスト用の title/image/score 込みエントリ map) の
  // 両方がこの Promise を共有する (#3055 E3: 二重 fetch 回避)。
  function _fetchIndexRows() {
    if (_indexRowsPromise) return _indexRowsPromise;
    if (typeof fetch !== "function") { _indexRowsPromise = Promise.resolve([]); return _indexRowsPromise; }
    _indexRowsPromise = fetch(CATALOG_URL, { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : []; })
      .catch(function () { return []; });
    return _indexRowsPromise;
  }

  // index.json は asin を持たない (permalink/title/image/price_* のみ)。
  // permalink 末尾の /products/<asin>/ から asin を逆引きする。
  function _rowAsin(row) {
    var m = (row.permalink || "").match(/\/products\/([a-z0-9]+)\/?/i);
    return m ? m[1].toUpperCase() : null;
  }

  function _rowMinPrice(row) {
    var prices = [row.price_amazon, row.price_rakuten, row.price_yahoo]
      .map(function (v) { return parseInt(v, 10) || 0; })
      .filter(function (v) { return v > 0; });
    return prices.length ? Math.min.apply(null, prices) : null;
  }

  function _loadCatalog() {
    if (_catalogPromise) return _catalogPromise;
    _catalogPromise = _fetchIndexRows().then(function (rows) {
      var map = {};
      (rows || []).forEach(function (row) {
        var asin = _rowAsin(row);
        if (!asin) return;
        var min = _rowMinPrice(row);
        if (min) map[asin] = min;
      });
      return map;
    }).catch(function () { return {}; });
    return _catalogPromise;
  }

  // 共有リスト (#3055 E3) 用: asin -> {title, image, score, min_price, url} の
  // エントリ全体を引ける map。_loadCatalog (最安値のみ) と別関数にしているが
  // fetch は _fetchIndexRows 経由で共有する。
  function _loadCatalogEntries() {
    if (_catalogEntriesPromise) return _catalogEntriesPromise;
    _catalogEntriesPromise = _fetchIndexRows().then(function (rows) {
      var map = {};
      (rows || []).forEach(function (row) {
        var asin = _rowAsin(row);
        if (!asin) return;
        map[asin] = {
          title: row.title || null,
          image: row.image || null,
          score: parseInt(row.ivs_score_100, 10) || null,
          min_price: _rowMinPrice(row),
          url: "/products/" + asin.toLowerCase() + "/"
        };
      });
      return map;
    }).catch(function () { return {}; });
    return _catalogEntriesPromise;
  }

  function _dropInfo(asin, currentMin) {
    var snap = snapshot(asin);
    var was = snap && Number(snap.min_price);
    var now = Number(currentMin);
    if (!(was > 0) || !(now > 0) || now >= was) return null;
    var pct = (was - now) / was;
    if (pct < DROP_THRESHOLD) return null;
    return { was: was, now: now, pct: Math.round(pct * 100), diff: was - now };
  }

  function _yen(n) { return "¥" + Number(n).toLocaleString(); }

  // favorites 一覧: 各カードの meta に 🔻 バッジを追記
  function _decorateFavoritesDrops() {
    if (!/\/favorites\/?$/.test(global.location.pathname)) return;
    _loadCatalog().then(function (map) {
      var cards = document.querySelectorAll(".favorites-card[data-asin]");
      for (var i = 0; i < cards.length; i++) {
        var card = cards[i];
        var asin = card.getAttribute("data-asin");
        var info = _dropInfo(asin, map[asin]);
        if (!info) continue;
        var meta = card.querySelector(".favorites-card-meta");
        if (!meta || meta.querySelector(".price-drop-badge")) continue;
        var badge = document.createElement("span");
        badge.className = "price-drop-badge";
        badge.title = "登録時 " + _yen(info.was) + " → 現在 " + _yen(info.now);
        badge.textContent = "🔻 " + info.pct + "% 値下げ";
        meta.appendChild(badge);
        card.classList.add("has-price-drop");
      }
    });
  }

  // 商品ページ: 表示中の商品がお気に入り済 & 値下げ時、タイトル直下にバナー
  function _decorateProductDrop() {
    var m = global.location.pathname.match(/\/products\/([a-z0-9]+)\/?$/i);
    if (!m) return;
    var asin = m[1].toUpperCase();
    if (!has(asin)) return;
    _loadCatalog().then(function (map) {
      var info = _dropInfo(asin, map[asin]);
      if (!info) return;
      var title = document.querySelector(".post-title");
      if (!title || document.querySelector(".price-drop-banner")) return;
      var banner = document.createElement("div");
      banner.className = "price-drop-banner";
      banner.setAttribute("role", "status");
      banner.innerHTML = '<span class="price-drop-banner-icon" aria-hidden="true">🔻</span>' +
        '<span class="price-drop-banner-text">お気に入り登録時より <strong>' + info.pct +
        '%</strong> 値下げ（' + _yen(info.was) + ' → <strong>' + _yen(info.now) + '</strong>）</span>';
      title.parentNode.insertBefore(banner, title.nextSibling);
    });
  }

  // ------- boot -------
  function boot() {
    try {
      mountToggles(document);
      mountHeaderBadge();
      hydrateFavoritesPage();
      _decorateFavoritesDrops();
      _decorateProductDrop();
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
