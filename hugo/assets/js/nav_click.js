/* navi-brain#213: サイト内の移動とトップの区画への到達を GA4 へ送る。
 *
 * ヘッダー・トップを作り直すとき、その効果を区画ごとに見るための計測。
 *   - nav_click        : data-nav-slot の付いた要素 (a / button) が押された。
 *                        属性が無くても、トップの [data-nav-section] の中の
 *                        内部リンク (商品カードなど) は slot = "home:<区画>" で送る
 *   - home_section_view: トップの [data-nav-section] の見出しが初めて画面に入った
 *
 * 内訳はすべて slot パラメータ 1 つに載せる。GA4 Data API は未登録のカスタム
 * ディメンションを指定するとリクエスト全体が 400 になり、既存の affiliate_click の
 * 取得まで巻き込むため、新しいパラメータは作らない (登録済みは slot / network / asin)。
 *
 * 購入クリック (affiliate_click) は affiliate_click.js が別に数える。ここでは
 * アフィリエイトのリンクを nav_click として送らない (二重に数えない)。
 *
 * 遷移は止めない (preventDefault しない)。gtag が無い (広告ブロッカー等) 場合は
 * 黙って何もしない。例外は外に出さない。
 */
(function () {
  'use strict';

  /* 二重に読み込まれても、リスナーと監視は 1 回だけ */
  if (window.__omochaNavClick) return;
  window.__omochaNavClick = true;

  var SLOT_MAX = 100;

  /* affiliate_click.js の NETWORKS と同じ (アフィリエイトのリンクは除外する) */
  var AFFILIATE_HOSTS = [
    'amazon.co.jp', 'amzn.to', 'amzn.asia',
    'rakuten.co.jp', 'hb.afl.rakuten.co.jp',
    'shopping.yahoo.co.jp', 'ck.jp.ap.valuecommerce.com', 'paypaymall.yahoo.co.jp'
  ];

  function isAffiliateHost(host) {
    for (var i = 0; i < AFFILIATE_HOSTS.length; i++) {
      var h = AFFILIATE_HOSTS[i];
      if (host === h || host.slice(-(h.length + 1)) === '.' + h) return true;
    }
    return false;
  }

  function send(name, slot) {
    if (typeof window.gtag !== 'function') return;
    try {
      window.gtag('event', name, { slot: String(slot).slice(0, SLOT_MAX) });
    } catch (e) {
      /* 計測の失敗で遷移を妨げない */
    }
  }

  /* 同一オリジンで、アフィリエイトでない内部リンクか */
  function isInternalLink(a) {
    var rel = (a.getAttribute('rel') || '').toLowerCase();
    if (rel.indexOf('sponsored') !== -1) return false;
    var url;
    try {
      url = new URL(a.getAttribute('href') || '', window.location.href);
    } catch (e) {
      return false;
    }
    if (url.protocol !== 'http:' && url.protocol !== 'https:') return false;
    if (url.origin !== window.location.origin) return false;
    return !isAffiliateHost(url.hostname);
  }

  function onClick(ev) {
    try {
      var t = ev.target;
      if (!t || !t.closest) return;
      var el = t.closest('[data-nav-slot]');
      if (el) {
        var slot = el.getAttribute('data-nav-slot');
        if (slot) send('nav_click', slot);
        return;
      }
      var a = t.closest('a[href]');
      if (!a) return;
      /* カードの中のボタン (お気に入り・比較) は favorites.js / compare.js が遷移を
         止めて別の操作をする。capture で先に呼ばれるので、ここで除かないと遷移して
         いない操作まで親カードの移動として数えてしまう */
      var btn = t.closest('button');
      if (btn && a.contains(btn)) return;
      var sec = a.closest('[data-nav-section]');
      if (!sec) return;
      var name = sec.getAttribute('data-nav-section');
      if (!name || !isInternalLink(a)) return;
      send('nav_click', 'home:' + name);
    } catch (e) {
      /* 何があっても遷移は止めない */
    }
  }

  document.addEventListener('click', onClick, true);

  /* トップの区画の到達。区画全体は縦に長いことがあるので、見出しを見る */
  function observeSections() {
    if (typeof IntersectionObserver !== 'function') return;
    if (!document.querySelectorAll) return;
    var sections = document.querySelectorAll('[data-nav-section]');
    if (!sections || !sections.length) return;
    var io;
    try {
      io = new IntersectionObserver(function (entries) {
        for (var i = 0; i < entries.length; i++) {
          var en = entries[i];
          if (!en.isIntersecting) continue;
          io.unobserve(en.target);
          var name = en.target.__omochaNavSection;
          en.target.__omochaNavSection = null; /* 万一また通知されても 1 回だけ */
          if (name) send('home_section_view', 'sec:' + name);
        }
      }, {
        threshold: 0,
        /* 上側を大きく広げる: 速いスクロールで見出しがフレームの間に画面を飛び越えると、
           交差が一度も起きず通知が来ない (ローカルの実測で区画が 1 つ欠けた)。上へ
           広げておけば、通り過ぎた見出しも「画面より上 = 到達済み」として交差に入る。
           下側は -20%: 画面の下端をかすめただけでは数えない */
        rootMargin: '100000px 0px -20% 0px'
      });
    } catch (e) {
      return;
    }
    for (var i = 0; i < sections.length; i++) {
      var s = sections[i];
      var n = s.getAttribute('data-nav-section');
      if (!n) continue;
      var target = (s.querySelector && s.querySelector('h2')) || s;
      target.__omochaNavSection = n;
      io.observe(target);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', observeSections);
  } else {
    observeSections();
  }
})();
