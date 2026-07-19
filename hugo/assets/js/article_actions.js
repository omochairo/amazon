// hugo/assets/js/article_actions.js
// #3568 E2: 記事ページ (デスクトップ) の post-header に ♡ お気に入り / 📊 比較 の
// 控えめなピルボタンを追加する。モバイルは mobile_sticky_cta.js の下メニューが
// 同機能を担うため CSS で ≤720px は .article-actions ごと非表示 (二重導線回避)。
//
// mobile_sticky_cta.js と同じ流儀で:
//   1. window.OmochaFavorites / window.OmochaCompare を直接呼ぶ (add/remove/toggle/has/list)
//   2. クリック直後は自分自身の aria-pressed / 見た目を即時反映
//   3. 同一 ASIN を参照する他の UI (下メニュー、カード側 .fav-toggle/.compare-toggle、
//      ヘッダの件数バッジ) も手動で同期する (favorites.js/compare.js は独自に生成した
//      要素しか知らないため、Hugo テンプレ側で静的に置いた本ボタンは対象外)
(function (global) {
  function _byId(id) { return document.getElementById(id); }
  function _q(sel, root) { return (root || document).querySelector(sel); }
  function _qa(sel, root) { return (root || document).querySelectorAll(sel); }

  function _buildMeta(container) {
    var asin = container.getAttribute("data-asin");
    var title = (_q(".post-title") || {}).textContent || "";
    var img = _q(".post-content img, .cover img, article img");
    var image = img ? img.getAttribute("src") : null;
    var scoreEl = _q("[data-ivs100]");
    var score = null;
    if (scoreEl) {
      var s = parseInt(scoreEl.getAttribute("data-ivs100"), 10);
      if (s > 0) score = s;
    }
    var sticky = _byId("mobile-sticky-cta");
    var minPrice = sticky ? parseInt(sticky.getAttribute("data-min-price") || "0", 10) : 0;
    return {
      asin: asin,
      title: (title || "").trim(),
      image: image,
      score: score,
      min_price: minPrice || null,
      url: global.location.pathname
    };
  }

  function _syncSelf(container) {
    var asin = container.getAttribute("data-asin");
    var favBtn = _q(".article-action-fav", container);
    if (favBtn) {
      var favOn = !!(global.OmochaFavorites && global.OmochaFavorites.has(asin));
      favBtn.setAttribute("aria-pressed", String(favOn));
      favBtn.classList.toggle("is-on", favOn);
      var favIcon = favBtn.querySelector(".article-action-icon");
      if (favIcon) favIcon.textContent = favOn ? "♥" : "♡";
      var favLabel = favBtn.querySelector(".article-action-label");
      if (favLabel) favLabel.textContent = favOn ? "保存済み" : "お気に入り";
      favBtn.setAttribute("aria-label", favOn ? "お気に入りから外す" : "お気に入りに追加");
    }
    var cmpBtn = _q(".article-action-compare", container);
    if (cmpBtn) {
      var arr = (global.OmochaCompare && global.OmochaCompare.list()) || [];
      var cmpOn = arr.indexOf(asin) !== -1;
      cmpBtn.setAttribute("aria-pressed", String(cmpOn));
      cmpBtn.classList.toggle("is-on", cmpOn);
      var cmpLabel = cmpBtn.querySelector(".article-action-label");
      if (cmpLabel) cmpLabel.textContent = cmpOn ? ("比較中 " + arr.length + "/4") : "比較に追加";
    }
  }

  // 他の同一 ASIN UI (下メニュー・カード側 toggle・ヘッダバッジ) を同期する。
  // favorites.js / compare.js は自前生成の .fav-toggle / .compare-toggle しか
  // 更新しないため、静的に置いた本ボタン発の変更は明示的に反映する必要がある。
  function _syncOthers(asin) {
    var stickyBar = _byId("mobile-sticky-cta");
    if (stickyBar && stickyBar.getAttribute("data-asin") === asin) {
      var stickyFav = _q(".m-sticky-fav", stickyBar);
      if (stickyFav) {
        var on = !!(global.OmochaFavorites && global.OmochaFavorites.has(asin));
        stickyFav.setAttribute("aria-pressed", String(on));
        stickyFav.classList.toggle("is-on", on);
        var icon = stickyFav.querySelector(".m-sticky-icon");
        if (icon) icon.textContent = on ? "♥" : "♡";
        var label = stickyFav.querySelector(".m-sticky-label");
        if (label) label.textContent = on ? "保存済" : "気になる";
      }
      var stickyCmp = _q(".m-sticky-compare", stickyBar);
      if (stickyCmp) {
        var arr = (global.OmochaCompare && global.OmochaCompare.list()) || [];
        var on2 = arr.indexOf(asin) !== -1;
        stickyCmp.setAttribute("aria-pressed", String(on2));
        stickyCmp.classList.toggle("is-on", on2);
        var label2 = stickyCmp.querySelector(".m-sticky-label");
        if (label2) label2.textContent = on2 ? ("比較中 " + arr.length + "/4") : "比較";
      }
    }

    var favNodes = _qa(".fav-toggle[data-asin='" + asin + "']");
    for (var i = 0; i < favNodes.length; i++) {
      var onF = !!(global.OmochaFavorites && global.OmochaFavorites.has(asin));
      favNodes[i].classList.toggle("is-on", onF);
      favNodes[i].setAttribute("aria-pressed", String(onF));
      var ic = favNodes[i].querySelector(".fav-toggle-icon");
      if (ic) ic.textContent = onF ? "♥" : "♡";
    }

    var arrAll = (global.OmochaCompare && global.OmochaCompare.list()) || [];
    var cmpNodes = _qa(".compare-toggle[data-asin='" + asin + "']");
    for (var j = 0; j < cmpNodes.length; j++) {
      var onC = arrAll.indexOf(asin) !== -1;
      cmpNodes[j].classList.toggle("is-selected", onC);
      cmpNodes[j].setAttribute("aria-pressed", String(onC));
      var mark = cmpNodes[j].querySelector(".compare-toggle-check");
      if (mark) mark.textContent = onC ? "✓" : "+";
    }

    var badge = _q("#menu .fav-menu-count");
    if (badge && global.OmochaFavorites) {
      var n = global.OmochaFavorites.count();
      badge.textContent = String(n);
      var link = _q("#menu .fav-menu-link");
      if (link) link.classList.toggle("has-items", n > 0);
    }
  }

  function _wireFav(container, asin) {
    var btn = _q(".article-action-fav", container);
    if (!btn) return;
    btn.addEventListener("click", function (e) {
      e.preventDefault();
      if (!global.OmochaFavorites) return;
      var meta = _buildMeta(container);
      global.OmochaFavorites.toggle(asin, meta);
      _syncSelf(container);
      _syncOthers(asin);
    });
  }

  function _wireCompare(container, asin) {
    var btn = _q(".article-action-compare", container);
    if (!btn) return;
    btn.addEventListener("click", function (e) {
      e.preventDefault();
      if (!global.OmochaCompare) return;
      var arr = global.OmochaCompare.list();
      if (arr.indexOf(asin) !== -1) {
        global.OmochaCompare.remove(asin);
      } else {
        if (!global.OmochaCompare.add(asin)) {
          btn.classList.add("article-action-flash");
          setTimeout(function () { btn.classList.remove("article-action-flash"); }, 800);
          return;
        }
      }
      _syncSelf(container);
      _syncOthers(asin);
    });
  }

  function boot() {
    try {
      var containers = _qa(".article-actions[data-asin]");
      if (!containers.length) return;
      for (var i = 0; i < containers.length; i++) {
        var container = containers[i];
        var asin = container.getAttribute("data-asin");
        if (!asin) continue;
        _wireFav(container, asin);
        _wireCompare(container, asin);
        _syncSelf(container);
      }
      // favorites.js / compare.js が本スクリプトより後に boot する場合があるので
      // 少し待って再同期 (mobile_sticky_cta.js と同じ保険)
      setTimeout(function () {
        for (var i = 0; i < containers.length; i++) _syncSelf(containers[i]);
      }, 200);
    } catch (e) { /* never break page */ }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})(typeof window !== "undefined" ? window : this);
