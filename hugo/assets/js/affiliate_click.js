/* omcha-ops#19 P4: アフィリエイト CTA のクリックを GA4 へ明示イベントで送る。
 *
 * これまでは GA4 拡張計測の自動 outbound click に頼っており、
 *   - どの CTA (price-card / sticky / competitor / 一覧カード / 本文リンク) が
 *     押されたのかの内訳が取れない
 *   - 3 ASP (Amazon / 楽天 / Yahoo) のどれが効いているのかも取れない
 * ため、「CTA を変えて改善した」が測れなかった。#1980 (CTA レイアウト切替) も
 * #3988 (計測基盤の 3 欠陥) もこの土台が無いまま積まれている。
 *
 * P0 の切り分けにも直接効く: このイベント数と Amazon 管理画面のクリック数の差が
 * そのまま「JS を実行しないクライアント (= ボット/クローラ)」の量になる。
 *
 * 実装方針:
 *   - リスナーは document に 1 個だけ (capture)。CTA が増えても配線は不要
 *   - slot は data-cta-slot 属性を第一に見る。属性が無い場合 (エッジに残った
 *     古い HTML / 未対応の新しい CTA) は class 名から推測して必ず何かを送る。
 *     「属性が無いから計測されない」という無言の欠測を作らないため
 *   - gtag が無い (広告ブロッカー等) 場合は黙って何もしない。購入導線は
 *     素の <a> なので計測が死んでも遷移は必ず起きる
 */
(function () {
  'use strict';

  var NETWORKS = [
    { host: 'amazon.co.jp', network: 'amazon' },
    { host: 'amzn.to', network: 'amazon' },
    { host: 'amzn.asia', network: 'amazon' },
    { host: 'rakuten.co.jp', network: 'rakuten' },
    { host: 'hb.afl.rakuten.co.jp', network: 'rakuten' },
    { host: 'shopping.yahoo.co.jp', network: 'yahoo' },
    { host: 'ck.jp.ap.valuecommerce.com', network: 'yahoo' },
    { host: 'paypaymall.yahoo.co.jp', network: 'yahoo' }
  ];

  /* data-cta-slot が無い HTML 向けの後方互換。class 名 → slot。
     先頭から順に一致を見るので、より具体的なものを上に置く。 */
  var SLOT_BY_CLASS = [
    ['m-sticky-buy', 'sticky'],
    ['competitor-cta', 'competitor'],
    ['price-card-cta', 'price-card'],
    ['price-search-fallback', 'price-search-fallback'],
    ['feature-cta-external', 'list-card'],
    ['ranking-cta-external', 'ranking'],
    ['review-cta', 'review'],
    ['baby-registry-cta', 'baby-registry']
  ];

  function networkOf(href) {
    var host;
    try {
      host = new URL(href, window.location.href).hostname;
    } catch (e) {
      return null;
    }
    for (var i = 0; i < NETWORKS.length; i++) {
      var h = NETWORKS[i].host;
      if (host === h || host.slice(-(h.length + 1)) === '.' + h) {
        return NETWORKS[i].network;
      }
    }
    return null;
  }

  function slotOf(a) {
    var explicit = a.getAttribute('data-cta-slot');
    if (explicit) return explicit;
    var el = a;
    while (el && el !== document.body) {
      var slot = el.getAttribute && el.getAttribute('data-cta-slot');
      if (slot) return slot;
      el = el.parentElement;
    }
    var cls = a.className || '';
    if (typeof cls !== 'string') cls = '';
    for (var i = 0; i < SLOT_BY_CLASS.length; i++) {
      if (cls.indexOf(SLOT_BY_CLASS[i][0]) !== -1) return SLOT_BY_CLASS[i][1];
    }
    /* 出典一覧のリンクは <ul class="sources-list"> の中にしか無い */
    if (a.closest && a.closest('.sources-list')) return 'source';
    return 'body';
  }

  function asinOf(a, href) {
    var m = /\/(?:dp|gp\/product)\/([A-Z0-9]{10})/i.exec(href);
    if (m) return m[1].toUpperCase();
    var el = a.closest ? a.closest('[data-asin]') : null;
    if (el) {
      var v = el.getAttribute('data-asin');
      if (v) return v.toUpperCase();
    }
    /* 楽天 / Yahoo の URL に ASIN は入らないので、商品ページの URL から取る。
       これが無いと「どの商品で楽天が押されたか」が全く分からず、
       ASP 間の比較 (P5 の前提) ができない。 */
    var p = /^\/products\/([a-z0-9]{10})\//i.exec(window.location.pathname);
    if (p) return p[1].toUpperCase();
    return '';
  }

  document.addEventListener('click', function (ev) {
    if (typeof window.gtag !== 'function') return;
    var a = ev.target && ev.target.closest ? ev.target.closest('a[href]') : null;
    if (!a) return;
    var href = a.getAttribute('href') || '';
    var network = networkOf(href);
    if (!network) return;
    try {
      window.gtag('event', 'affiliate_click', {
        network: network,
        slot: slotOf(a),
        asin: asinOf(a, href),
        page_path: window.location.pathname
      });
    } catch (e) {
      /* 計測の失敗で遷移を妨げない */
    }
  }, true);
})();
