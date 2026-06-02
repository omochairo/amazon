// hugo/assets/js/mobile_sticky_cta.js
// #1368 モバイル sticky CTA bar (epic #1365 Layer 2-①)
// 商品ページの画面下端に ♡ / 📊 / 🛒 最安値 の 3 ボタン bar を固定表示。
//
// 動作:
//   1. mount: #mobile-sticky-cta が存在し viewport ≤ 720px のとき表示
//   2. buy URL 抽出: 記事内 .price-card.is-cheapest .price-card-cta の href を
//      🛒 ボタンに反映 (見つからない場合は #price-comparison アンカーへ scroll)
//   3. ♡ / 📊 は OmochaFavorites / OmochaCompare と双方向同期
//   4. IntersectionObserver で footer / 関連商品セクションが見えたら hide
//   5. ヒーロー (記事冒頭) が見えている時は非表示、スクロールで本文に入ったら表示
(function (global) {
  function _byId(id) { return document.getElementById(id); }
  function _q(sel, root) { return (root || document).querySelector(sel); }

  function _extractBuyUrl() {
    // 1. .price-card.is-cheapest の CTA を最優先
    var cheapest = _q(".price-card.is-cheapest .price-card-cta[href]");
    if (cheapest && cheapest.getAttribute("href") && !/^#/.test(cheapest.getAttribute("href"))) {
      return {
        url: cheapest.getAttribute("href"),
        label: (_q(".price-card.is-cheapest .price-card-label") || {}).textContent || ""
      };
    }
    // 2. fallback: 任意の amazon CTA
    var amazon = _q(".price-card-cta--amazon[href]");
    if (amazon && amazon.getAttribute("href") && !/^#/.test(amazon.getAttribute("href"))) {
      return { url: amazon.getAttribute("href"), label: "Amazon" };
    }
    return null;
  }

  function _buildMeta(bar) {
    var asin = bar.getAttribute("data-asin");
    var title = (_q(".post-title") || {}).textContent || "";
    var img = _q(".post-content img, .cover img, article img");
    var image = img ? img.getAttribute("src") : null;
    var scoreEl = _q("[data-ivs100]");
    var score = null;
    if (scoreEl) {
      var s = parseInt(scoreEl.getAttribute("data-ivs100"), 10);
      if (s > 0) score = s;
    }
    var minPrice = parseInt(bar.getAttribute("data-min-price") || "0", 10);
    return {
      asin: asin,
      title: (title || "").trim(),
      image: image,
      score: score,
      min_price: minPrice || null,
      url: global.location.pathname
    };
  }

  function _syncFavState(bar) {
    var btn = _q(".m-sticky-fav", bar);
    if (!btn) return;
    var asin = bar.getAttribute("data-asin");
    var on = global.OmochaFavorites && global.OmochaFavorites.has(asin);
    btn.setAttribute("aria-pressed", String(!!on));
    btn.classList.toggle("is-on", !!on);
    var icon = btn.querySelector(".m-sticky-icon");
    if (icon) icon.textContent = on ? "♥" : "♡";
    var label = btn.querySelector(".m-sticky-label");
    if (label) label.textContent = on ? "保存済" : "気になる";
  }

  function _syncCompareState(bar) {
    var btn = _q(".m-sticky-compare", bar);
    if (!btn) return;
    var asin = bar.getAttribute("data-asin");
    var arr = (global.OmochaCompare && global.OmochaCompare.list()) || [];
    var on = arr.indexOf(asin) !== -1;
    btn.setAttribute("aria-pressed", String(!!on));
    btn.classList.toggle("is-on", !!on);
    var label = btn.querySelector(".m-sticky-label");
    if (label) label.textContent = on ? ("比較中 " + arr.length + "/4") : "比較";
  }

  function _wireFav(bar) {
    var btn = _q(".m-sticky-fav", bar);
    if (!btn) return;
    btn.addEventListener("click", function (e) {
      e.preventDefault();
      if (!global.OmochaFavorites) return;
      var asin = bar.getAttribute("data-asin");
      var meta = _buildMeta(bar);
      global.OmochaFavorites.toggle(asin, meta);
      _syncFavState(bar);
      // 既存カードの ♡ も同期 (favorites.js _syncAllToggles 相当)
      var nodes = document.querySelectorAll(".fav-toggle[data-asin='" + asin + "']");
      for (var i = 0; i < nodes.length; i++) {
        var on = global.OmochaFavorites.has(asin);
        nodes[i].classList.toggle("is-on", on);
        nodes[i].setAttribute("aria-pressed", String(on));
        var ic = nodes[i].querySelector(".fav-toggle-icon");
        if (ic) ic.textContent = on ? "♥" : "♡";
      }
      // ヘッダバッジ
      var badge = _q("#menu .fav-menu-count");
      if (badge && global.OmochaFavorites) {
        var n = global.OmochaFavorites.count();
        badge.textContent = String(n);
        var link = _q("#menu .fav-menu-link");
        if (link) link.classList.toggle("has-items", n > 0);
      }
    });
  }

  function _wireCompare(bar) {
    var btn = _q(".m-sticky-compare", bar);
    if (!btn) return;
    btn.addEventListener("click", function (e) {
      e.preventDefault();
      if (!global.OmochaCompare) return;
      var asin = bar.getAttribute("data-asin");
      var arr = global.OmochaCompare.list();
      if (arr.indexOf(asin) !== -1) {
        global.OmochaCompare.remove(asin);
      } else {
        if (!global.OmochaCompare.add(asin)) {
          btn.classList.add("m-sticky-flash");
          setTimeout(function () { btn.classList.remove("m-sticky-flash"); }, 800);
          return;
        }
      }
      _syncCompareState(bar);
      // 既存 compare-toggle 同期
      var nodes = document.querySelectorAll(".compare-toggle[data-asin='" + asin + "']");
      var on = global.OmochaCompare.list().indexOf(asin) !== -1;
      for (var i = 0; i < nodes.length; i++) {
        nodes[i].classList.toggle("is-selected", on);
        nodes[i].setAttribute("aria-pressed", String(on));
        var mark = nodes[i].querySelector(".compare-toggle-check");
        if (mark) mark.textContent = on ? "✓" : "+";
      }
    });
  }

  function _wireBuy(bar) {
    var anchor = _q(".m-sticky-buy", bar);
    if (!anchor) return;
    var info = _extractBuyUrl();
    if (info && info.url) {
      anchor.setAttribute("href", info.url);
      anchor.setAttribute("target", "_blank");
      anchor.setAttribute("rel", "noopener nofollow sponsored");
      if (info.label) {
        var labelEl = anchor.querySelector(".m-sticky-buy-label");
        if (labelEl) labelEl.textContent = info.label + " 最安";
      }
    } else {
      // 価格 CTA が見つからなければ価格比較セクションへ scroll
      anchor.addEventListener("click", function (e) {
        e.preventDefault();
        var target = _q(".price-cards") || _q(".price-card") || _q(".post-content h2");
        if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    }
  }

  function _setupVisibility(bar) {
    // ヒーロー (post-header) を抜けるまで非表示、抜けたら表示
    // 末尾セクション (post-footer / sources / related) が見えたら非表示
    var topSentinel = _q(".post-header");
    var bottomSentinels = [
      _q(".post-footer"),
      _q(".recently-viewed-mount--post"),
      _q(".ranking-history-chart")
    ].filter(Boolean);

    var topPassed = !topSentinel;
    var bottomVisible = false;

    function update() {
      var shouldShow = topPassed && !bottomVisible;
      bar.toggleAttribute("hidden", !shouldShow);
    }

    if (topSentinel && "IntersectionObserver" in global) {
      new IntersectionObserver(function (entries) {
        entries.forEach(function (en) {
          topPassed = !en.isIntersecting && en.boundingClientRect.top < 0;
          // ヘッダがビューポート上方にスクロールアウトしたら topPassed = true
          if (en.boundingClientRect.bottom < 0) topPassed = true;
          update();
        });
      }, { threshold: 0 }).observe(topSentinel);
    } else {
      topPassed = true;
    }

    if (bottomSentinels.length && "IntersectionObserver" in global) {
      var bottomObserver = new IntersectionObserver(function (entries) {
        var anyVisible = false;
        entries.forEach(function (en) {
          if (en.isIntersecting) anyVisible = true;
        });
        bottomVisible = anyVisible;
        update();
      }, { threshold: 0 });
      bottomSentinels.forEach(function (el) { bottomObserver.observe(el); });
    }

    update();
  }

  function boot() {
    try {
      var bar = _byId("mobile-sticky-cta");
      if (!bar) return;
      _wireBuy(bar);
      _wireFav(bar);
      _wireCompare(bar);
      // initial sync (favorites.js / compare.js が boot 後の場合があるので少し待つ)
      function syncAll() {
        _syncFavState(bar);
        _syncCompareState(bar);
      }
      syncAll();
      setTimeout(syncAll, 200);
      _setupVisibility(bar);
    } catch (e) { /* never break page */ }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})(typeof window !== "undefined" ? window : this);
