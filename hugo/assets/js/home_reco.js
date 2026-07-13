// hugo/assets/js/home_reco.js
// #3055 E2: ホーム「あなたにおすすめ」レール。商品ページに SSR 済みの関連商品カルーセル
// (related_carousel.html。semantic 近傍スコアも含む) が描画した候補を localStorage に
// 蓄積し、ホームで「最近見たおもちゃに近いアイテム」をクライアント集計・描画する。
// 新規データ取得は無し (index.json 等への追加フェッチはゼロ、SSR 済み情報の再利用のみ)。
//
// 公開: window.OmochaReco = { list, clear, render }
// 自動:
//   1. 商品ページ (/products/<asin>/) を開いたら related_carousel.html の候補を収集して
//      _pushSource で記録 (自分自身の ASIN は除外)
//   2. ホーム (/) に .home-reco-mount があれば集計して render (3件未満なら hidden のまま)
//
// 仕様:
// - localStorage key "omcha:reco:v1"
//   { sources: { "<閲覧ASIN大文字>": { ts: ISO文字列, items: [{asin,title,image,url}] } } }
// - source 上限 12 (超えたら ts 最古を evict)、items は 1 source あたり最大 8
// - 集計は omcha:recent:v1 (recently_viewed.js の key、読み取り専用) の並び順 (新しい順) に
//   sources を走査。score = 出現回数 + source の新しさボーナス (先頭 source +2、以降 +1)。
//   除外: 閲覧済み ASIN (recent list に居るもの)、重複。上位 10 件
// - localStorage 不可なら sessionStorage / memory fallback (recently_viewed.js と同方針)
(function (global) {
  var KEY = "omcha:reco:v1";
  var RECENT_KEY = "omcha:recent:v1"; // recently_viewed.js の key を読み取り専用で参照
  var MAX_SOURCES = 12;
  var MAX_ITEMS_PER_SOURCE = 8;
  var MAX_RESULT = 10;
  var MIN_RESULT = 3;
  var memStore = null;

  // ------- storage (graceful fallback) -------
  function _read() {
    try {
      var raw = (localStorage || sessionStorage).getItem(KEY);
      if (raw == null && memStore != null) return _clone(memStore);
      return raw ? JSON.parse(raw) : { sources: {} };
    } catch (e) {
      return memStore ? _clone(memStore) : { sources: {} };
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
    var out = { sources: {} };
    var src = (o && o.sources) || {};
    for (var k in src) {
      if (!src.hasOwnProperty(k)) continue;
      out.sources[k] = { ts: src[k].ts, items: (src[k].items || []).slice() };
    }
    return out;
  }

  function _readRecent() {
    try {
      var raw = (localStorage || sessionStorage).getItem(RECENT_KEY);
      return raw ? JSON.parse(raw) : [];
    } catch (e) {
      return [];
    }
  }

  function _currentAsin() {
    var m = global.location.pathname.match(/\/products\/([a-z0-9]+)\/?/i);
    return m ? m[1].toUpperCase() : null;
  }

  // ------- 収集 (商品ページ) -------
  // related_carousel.html が描画した .carousel-card 群を抽出。future_age_carousel.html
  // (.future-age-section) と recently-viewed-mount も同じ .related-carousel-section +
  // .carousel-card クラスを再利用しているため両方除外し、related_carousel.html 由来
  // (semantic 近傍含む) のみに絞る。
  function _collectFromArticle(selfAsin) {
    var sections = document.querySelectorAll(".related-carousel-section");
    var items = [];
    var seen = {};
    for (var s = 0; s < sections.length; s++) {
      var sec = sections[s];
      if (sec.classList.contains("recently-viewed-mount")) continue;
      if (sec.classList.contains("future-age-section")) continue;
      var cards = sec.querySelectorAll(".carousel-card");
      for (var i = 0; i < cards.length; i++) {
        if (items.length >= MAX_ITEMS_PER_SOURCE) break;
        var card = cards[i];
        var href = card.getAttribute("href");
        if (!href) continue;
        var m = href.match(/\/products\/([a-z0-9]+)\/?/i);
        if (!m) continue;
        var asin = m[1].toUpperCase();
        if (asin === selfAsin) continue;
        if (seen[asin]) continue;
        seen[asin] = true;
        var titleEl = card.querySelector(".carousel-card-title");
        var imgEl = card.querySelector(".carousel-card-image img");
        items.push({
          asin: asin,
          title: titleEl ? (titleEl.textContent || "").trim() : null,
          image: imgEl ? imgEl.getAttribute("src") : null,
          url: href
        });
      }
      if (items.length >= MAX_ITEMS_PER_SOURCE) break;
    }
    return items;
  }

  function _pushSource(asin, items) {
    if (!asin || !items || items.length === 0) return;
    var data = _read();
    data.sources[asin] = { ts: new Date().toISOString(), items: items.slice(0, MAX_ITEMS_PER_SOURCE) };
    // source 上限超過なら ts 最古から evict
    var keys = Object.keys(data.sources);
    if (keys.length > MAX_SOURCES) {
      keys.sort(function (a, b) {
        return new Date(data.sources[a].ts).getTime() - new Date(data.sources[b].ts).getTime();
      });
      while (keys.length > MAX_SOURCES) {
        var oldest = keys.shift();
        delete data.sources[oldest];
      }
    }
    _write(data);
  }

  function _autoCollect() {
    var asin = _currentAsin();
    if (!asin) return;
    var items = _collectFromArticle(asin);
    if (items.length) _pushSource(asin, items);
  }

  // ------- 集計 (ホーム) -------
  function _aggregate() {
    var recent = _readRecent();
    var excluded = {};
    for (var r = 0; r < recent.length; r++) {
      if (recent[r] && recent[r].asin) excluded[recent[r].asin] = true;
    }
    var data = _read();
    var scoreMap = {};
    var metaMap = {};
    var sourceIndex = 0;
    for (var i = 0; i < recent.length; i++) {
      var rAsin = recent[i] && recent[i].asin;
      if (!rAsin) continue;
      var source = data.sources[rAsin];
      if (!source || !source.items || !source.items.length) continue;
      var bonus = sourceIndex === 0 ? 2 : 1;
      sourceIndex++;
      for (var j = 0; j < source.items.length; j++) {
        var it = source.items[j];
        if (!it || !it.asin) continue;
        if (excluded[it.asin]) continue;
        scoreMap[it.asin] = (scoreMap[it.asin] || 0) + 1 + bonus;
        if (!metaMap[it.asin]) metaMap[it.asin] = it;
      }
    }
    var candidates = [];
    for (var asin in scoreMap) {
      if (!scoreMap.hasOwnProperty(asin)) continue;
      candidates.push({ asin: asin, score: scoreMap[asin], item: metaMap[asin] });
    }
    candidates.sort(function (a, b) { return b.score - a.score; });
    return candidates.slice(0, MAX_RESULT).map(function (c) { return c.item; });
  }

  function list() { return _aggregate(); }
  function clear() { _write({ sources: {} }); }

  function _renderTo(mount, items) {
    if (!items || items.length < MIN_RESULT) {
      mount.setAttribute("hidden", "");
      return;
    }
    mount.removeAttribute("hidden");
    var grid = mount.querySelector(".recently-viewed-grid");
    if (!grid) return;
    grid.innerHTML = "";
    items.forEach(function (r) {
      // recently-viewed のカード構造を再利用 (CSS 追加を最小化)。削除ボタンは無し。
      var item = document.createElement("div");
      item.className = "recently-viewed-item";
      item.setAttribute("data-asin", r.asin);

      var card = document.createElement("a");
      card.className = "recently-viewed-card";
      card.href = r.url || ("/products/" + r.asin.toLowerCase() + "/");
      card.innerHTML =
        (r.image ? '<img class="recently-viewed-image" src="' + r.image + '" alt="" loading="lazy" referrerpolicy="no-referrer">' : '<div class="recently-viewed-image-fallback">🧸</div>') +
        '<div class="recently-viewed-body">' +
        '<div class="recently-viewed-title">' + (r.title || r.asin) + '</div>' +
        '</div>';

      item.appendChild(card);
      grid.appendChild(item);
    });
  }

  function render(root) {
    root = root || document;
    var mounts = root.querySelectorAll(".home-reco-mount");
    if (!mounts.length) return;
    var items = _aggregate();
    for (var i = 0; i < mounts.length; i++) {
      _renderTo(mounts[i], items);
    }
    // carousel_swipe.js (同居) の矢印 disabled 状態を再計算させる (recently_viewed.js と同方針)。
    try { global.dispatchEvent(new Event("resize")); } catch (e) { /* noop */ }
  }

  function _isHome() {
    var p = global.location.pathname;
    return p === "/" || p === "" || p === "/index.html";
  }

  function boot() {
    try {
      // 商品ページなら関連カルーセル候補を収集 (render より先でも後でも影響なし。
      // ホームと商品ページは別リクエストのため実質どちらか一方のみ実行される)
      if (/^\/products\/[a-z0-9]+\/?$/i.test(global.location.pathname)) {
        _autoCollect();
      }
      if (_isHome()) {
        render(document);
      }
    } catch (e) { /* never break page */ }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }

  global.OmochaReco = { list: list, clear: clear, render: render };
})(typeof window !== "undefined" ? window : this);
