/* #9449 セール期間の告知帯を、閲覧時刻で出し分ける。
 *
 * partials/sale_notice.html が [data-sale-notice] を hidden で出力し、
 * data-phases に区分 (label / headline / start / end) の JSON を持たせる。
 * ここでは「いまどの区分の中か」を見て文言を入れ、hidden を外す。
 *
 * 実装方針:
 *   - 判定はビルド時ではなく閲覧時。静的サイトなので、ビルド時に決めると
 *     終了後も次のビルドまで「開催中」が残る
 *   - 迷ったら出さない。日付が読めない・期間外・JSON が壊れている、はすべて
 *     hidden のまま (古い告知が残るより、出ない方が安全)
 *   - 表示する日時は閲覧者のタイムゾーンに依らず JST (セールの期間が JST のため)
 */
(function () {
  'use strict';

  var JST_OFFSET_MS = 9 * 60 * 60 * 1000;

  function jst(ms) {
    var d = new Date(ms + JST_OFFSET_MS);
    return {
      y: d.getUTCFullYear(), m: d.getUTCMonth() + 1, d: d.getUTCDate(),
      h: d.getUTCHours(), min: d.getUTCMinutes()
    };
  }

  function hm(p) {
    return p.h + ':' + (p.min < 10 ? '0' : '') + p.min;
  }

  function md(p) {
    return p.m + '/' + p.d;
  }

  function sameDay(a, b) {
    return a.y === b.y && a.m === b.m && a.d === b.d;
  }

  // 1 件でも読めない区分があれば全体を捨てる (半端な期間で出さない)
  function parse(phases) {
    if (!phases || !phases.length) return [];
    var out = [];
    for (var i = 0; i < phases.length; i++) {
      var p = phases[i] || {};
      var start = Date.parse(p.start);
      var end = Date.parse(p.end);
      if (isNaN(start) || isNaN(end) || start >= end || !p.headline) return [];
      out.push({ label: p.label || p.headline, headline: p.headline, start: start, end: end });
    }
    out.sort(function (a, b) { return a.start - b.start; });
    return out;
  }

  // 期間外は空文字 (= 出さない)。short は次の区分の予告を省く (商品ページの 1 行用)
  function message(phases, nowMs, short) {
    var list = parse(phases);
    if (!list.length) return '';
    if (nowMs < list[0].start || nowMs > list[list.length - 1].end) return '';
    var now = jst(nowMs);
    for (var i = 0; i < list.length; i++) {
      var p = list[i];
      if (nowMs < p.start) {
        // 区分と区分のすき間
        var from = jst(p.start);
        return p.headline + 'は ' + md(from) + ' ' + hm(from) + ' から';
      }
      if (nowMs <= p.end) {
        var end = jst(p.end);
        var text = p.headline + ' 開催中（' +
          (sameDay(now, end) ? '本日' : md(end)) + ' ' + hm(end) + ' まで）';
        var next = list[i + 1];
        if (next && !short) {
          var s = jst(next.start);
          text += '／' + next.label + 'は ' + md(s) + ' ' + hm(s) + ' から';
        }
        return text;
      }
    }
    return '';
  }

  function apply(doc, nowMs) {
    var nodes = doc.querySelectorAll('[data-sale-notice]');
    for (var i = 0; i < nodes.length; i++) {
      var node = nodes[i];
      var text = '';
      try {
        text = message(JSON.parse(node.getAttribute('data-phases')), nowMs,
          node.getAttribute('data-sale-notice-short') !== null);
      } catch (e) {
        text = '';
      }
      var main = node.querySelector('[data-sale-notice-main]');
      if (!text || !main) {
        node.hidden = true;
        continue;
      }
      main.textContent = text;
      // 補足は本文 (.Plain) に混ざらないよう data 属性で受け取り、表示するときだけ入れる
      var sub = node.getAttribute('data-sale-notice-sub');
      var subTarget = node.querySelector('[data-sale-notice-sub-target]');
      if (sub && subTarget) subTarget.textContent = sub;
      node.hidden = false;
    }
  }

  /* 告知が押されたことを GA4 へ送る (affiliate_click.js と同じ流儀)。
   *   - リスナーは document に 1 個だけ (capture)。告知の置き場所が増えても配線は不要
   *   - リンクの無い告知 (商品ページの 1 行) も送る。link が空のクリックは
   *     「押せると思って押された」回数で、リンクを付けるかどうかの判断材料になる
   *   - gtag が無い (広告ブロッカー等) 場合は黙って何もしない。リンクは素の <a>
   *     なので、計測が死んでも遷移は必ず起きる
   */
  function track(ev) {
    if (typeof window.gtag !== 'function') return;
    var target = ev && ev.target;
    var node = target && target.closest ? target.closest('[data-sale-notice]') : null;
    if (!node || node.hidden) return;
    var a = target.closest('a[href]');
    try {
      window.gtag('event', 'sale_notice_click', {
        slot: node.getAttribute('data-sale-slot') || 'unknown',
        sale_id: node.getAttribute('data-sale-id') || '',
        link: a ? (a.getAttribute('href') || '') : '',
        page_path: window.location.pathname
      });
    } catch (e) {
      /* 計測の失敗で遷移を妨げない */
    }
  }

  window.OmochaSaleNotice = { message: message, apply: apply, track: track };

  document.addEventListener('click', track, true);

  apply(document, Date.now());
  // このスクリプトはヘッダー直下にインラインで置かれるので、それより後ろにある要素
  // (商品ページの価格カード下の 1 行) は、この時点ではまだ DOM に無い
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { apply(document, Date.now()); });
  }
  // bfcache から戻ったときは再実行されないので、期間をまたいだ表示が残らないよう判定し直す
  window.addEventListener('pageshow', function () { apply(document, Date.now()); });
})();
