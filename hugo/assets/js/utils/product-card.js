// hugo/assets/js/utils/product-card.js
// product-card 描画ロジックの共通実装 (Issue #745 Phase 2)。
// 旧: list.html#renderCards, term.html#renderCards, diagnosis.js#createProductCardHtml
// (JS 3 箇所重複) → ここに集約。SSR (layouts/partials/product_card.html) と
// HTML 構造を完全一致させ、検索/並べ替え/絞込/診断の全クライアント経路で同じカードを描画する。
// 依存: window.OmochaUtils.formatAgeMin (utils/age.js)
(function (global) {
  var UNKNOWN_BRANDS = [
    "不明", "不明 / 不明", "Unknown", "N/A", "なし", "—", "-",
    "ノーブランド", "ノーブランド品", "NoBrand", "No Brand", "Generic"
  ];

  function scoreTierClass(score100) {
    if (score100 >= 90) return "score-gold";
    if (score100 >= 75) return "score-silver";
    return "score-bronze";
  }

  function minNonZero(values) {
    var min = 99999999;
    for (var i = 0; i < values.length; i++) {
      var v = values[i];
      if (v && v > 0 && v < min) min = v;
    }
    return min === 99999999 ? 0 : min;
  }

  function formatYen(n) {
    return n.toLocaleString("ja-JP");
  }

  function escapeAttr(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/"/g, "&quot;")
      .replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function escapeText(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  // item -> <a class="product-card"> 要素を返す。
  // 入力 item は /index.json のエントリ形 (permalink, image, title, product_name,
  // brand, age_min_months, ivs_score, ivs_score_100, price_*, tags[], date, unix_time)。
  function renderProductCard(item) {
    var ns = global.OmochaUtils || {};
    var formatAgeMin = ns.formatAgeMin || function () { return ""; };

    var score100 = item.ivs_score_100 || 0;
    var score5 = item.ivs_score || 0;
    var tierClass = scoreTierClass(score100);
    var minPrice = minNonZero([item.price_amazon, item.price_rakuten, item.price_yahoo]);
    var ageStr = formatAgeMin(item.age_min_months);
    var displayName = item.product_name || item.title || "";
    var brand = item.brand || "";
    if (UNKNOWN_BRANDS.indexOf(brand) !== -1) brand = "";

    var imgHtml = item.image
      ? '<img src="' + escapeAttr(item.image) + '" alt="' + escapeAttr(displayName) +
        '" loading="lazy" decoding="async" referrerpolicy="no-referrer">'
      : '<div class="product-card-image-fallback">&#129528;</div>';

    var scoreHtml = "";
    var priceSuffix = minPrice > 0
      ? '<span class="product-card-score-price">最安 &yen;' + formatYen(minPrice) + '</span>'
      : "";
    if (score100) {
      scoreHtml =
        '<div class="product-card-badges">' +
          '<span class="product-card-score ' + tierClass + '" title="知育スコア (100点満点)">' +
            '🏆 <strong>' + score100 + '</strong>' +
            '<span class="product-card-score-suffix">点</span>' +
            priceSuffix +
          '</span>' +
        '</div>';
    } else if (score5) {
      scoreHtml =
        '<div class="product-card-badges">' +
          '<span class="product-card-score score-bronze" title="知育スコア (5点満点)">' +
            '🏆 <strong>' + score5.toFixed(1) + '</strong>' +
            '<span class="product-card-score-suffix">/5</span>' +
            priceSuffix +
          '</span>' +
        '</div>';
    }

    var ageHtml = ageStr
      ? '<span class="product-card-age" title="対象年齢">&#128118; ' + escapeText(ageStr) + '</span>'
      : "";
    var brandHtml = brand
      ? '<div class="product-card-brand">' + escapeText(brand) + '</div>'
      : "";

    var tagsHtml = "";
    if (item.tags && item.tags.length > 0) {
      var tagSpans = item.tags.slice(0, 2).map(function (t) {
        return '<span class="product-card-tag">#' + escapeText(t) + '</span>';
      }).join("");
      tagsHtml = '<span class="product-card-tags">' + tagSpans + '</span>';
    }

    var dateStr = item.date || "";

    var a = document.createElement("a");
    a.className = "product-card";
    a.href = item.permalink || "#";
    a.setAttribute("aria-label", displayName + " の比較レビューを読む");
    a.setAttribute("data-ivs", score5 || 0);
    a.setAttribute("data-ivs100", score100 || 0);
    a.setAttribute("data-age-min", item.age_min_months || 0);
    a.innerHTML =
      '<div class="product-card-image">' +
        imgHtml + scoreHtml + ageHtml +
      '</div>' +
      '<div class="product-card-body">' +
        brandHtml +
        '<h3 class="product-card-title">' + escapeText(item.title || "") + '</h3>' +
        '<div class="product-card-meta">' +
          '<time datetime="' + escapeAttr(dateStr) + '">' + escapeText(dateStr) + '</time>' +
          tagsHtml +
        '</div>' +
      '</div>';
    return a;
  }

  var ns = global.OmochaUtils || (global.OmochaUtils = {});
  ns.renderProductCard = renderProductCard;
  ns.scoreTierClass = scoreTierClass;
})(typeof window !== "undefined" ? window : this);
