/*
 * ranking_sort.js — /ranking/ の並べ替え (2026-11-26・K の決定)
 *
 * はじめの並びは「解説ありを先に」(navi-brain#166 F3)。ボタンで「楽天の順位順」に
 * 並べ替え、もう一度押すと元に戻す。カードは消して描き直さず、同じ <li> を
 * 入れ物の間で移す (計測の属性・開いた <details> がそのまま残る)。
 *
 * 動き: カードの移動前と移動後の位置を測り、差の分だけずらした所から元へ戻す
 * (FLIP)。画面の近くにあるカードだけ動かす (遠くのカードまで動かすと、何千 px も
 * 飛ぶ線が見えるだけで分かりにくい)。「動きを減らす」設定の人には動かさない。
 *
 * JS が無いときは並べ替えボタンを出さない (hidden のまま)。そのときも
 * 「解説ありを先に」の並びとページ内リンクはそのまま使える。
 */
(function () {
  'use strict';

  var root = document.querySelector('.rk-page');
  if (!root) return;
  var sortBox = root.querySelector('.rk-sort');
  var groupsBox = root.querySelector('.rk-groups');
  var rankGroup = root.querySelector('.rk-group--rank');
  if (!sortBox || !groupsBox || !rankGroup) return;
  var rankList = rankGroup.querySelector('.ranking-list');
  var items = Array.prototype.slice.call(groupsBox.querySelectorAll('.ranking-item'));
  if (!rankList || items.length < 2) return;

  // 元の場所 (どの <ol> の何番目か) を覚えておき、戻すときに使う
  items.forEach(function (li, i) {
    li._rkHome = li.parentNode;
    li._rkIndex = i;
  });
  var byRank = items.slice().sort(function (a, b) {
    return (+a.getAttribute('data-rank') || 0) - (+b.getAttribute('data-rank') || 0);
  });
  var buttons = Array.prototype.slice.call(sortBox.querySelectorAll('.rk-sort-btn'));
  var status = root.querySelector('.rk-sort-status');
  var current = 'article';
  var animating = [];

  function reduceMotion() {
    return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }

  function track(name, params) {
    if (typeof window.gtag !== 'function') return;
    try { window.gtag('event', name, params); } catch (e) { /* 計測の失敗で操作を止めない */ }
  }

  function topsOf(list) {
    var map = [];
    for (var i = 0; i < list.length; i++) {
      map.push(list[i].getBoundingClientRect().top);
    }
    return map;
  }

  function stopAnimations() {
    animating.forEach(function (a) { try { a.cancel(); } catch (e) { /* noop */ } });
    animating = [];
  }

  function apply(order) {
    if (order === current) return;
    stopAnimations();

    // 並べ替えの前後でボタンが画面の同じ高さに残るよう、ボタンの位置で画面を合わせる
    var anchor = sortBox.getBoundingClientRect().top;
    var before = topsOf(items);

    if (order === 'rank') {
      byRank.forEach(function (li) { rankList.appendChild(li); });
      rankGroup.hidden = false;
      groupsBox.querySelectorAll('.rk-group[data-group="article"], .rk-group[data-group="others"]').forEach(function (g) { g.hidden = true; });
    } else {
      items.forEach(function (li) { li._rkHome.appendChild(li); });
      rankGroup.hidden = true;
      groupsBox.querySelectorAll('.rk-group[data-group="article"], .rk-group[data-group="others"]').forEach(function (g) { g.hidden = false; });
    }
    root.classList.toggle('rk-order-rank', order === 'rank');
    current = order;

    var shift = sortBox.getBoundingClientRect().top - anchor;
    if (shift) window.scrollBy(0, shift);

    buttons.forEach(function (b) {
      b.setAttribute('aria-pressed', b.getAttribute('data-order') === order ? 'true' : 'false');
    });
    if (status) {
      status.textContent = order === 'rank'
        ? '楽天の順位順に並べ替えました'
        : '解説のあるおもちゃを先に並べました';
    }
    track('ranking_sort', { order: order });

    if (reduceMotion() || typeof Element.prototype.animate !== 'function') return;

    var after = topsOf(items);
    var vh = window.innerHeight || 800;
    for (var i = 0; i < items.length; i++) {
      var dy = before[i] - after[i];
      if (Math.abs(dy) < 2) continue;
      // 移動後に画面の近く (上下 1 画面ぶん) に来るカードだけ動かす
      if (after[i] > vh * 2 || after[i] < -vh) continue;
      // 遠くから来るカードは、1 画面ぶんの所から滑り込ませる (何千 px も飛ばさない)
      var from = Math.max(-vh, Math.min(vh, dy));
      var a = items[i].animate([
        { transform: 'translateY(' + from + 'px)', opacity: Math.abs(dy) > vh ? 0 : 1 },
        { transform: 'translateY(0)', opacity: 1 }
      ], {
        duration: 520,
        easing: 'cubic-bezier(.22,1,.36,1)',
        delay: Math.min(i, 8) * 18
      });
      animating.push(a);
    }
  }

  buttons.forEach(function (b) {
    b.addEventListener('click', function () { apply(b.getAttribute('data-order')); });
  });

  // 「解説あり」「そのほか」へのページ内リンクは、順位順のときは群が無いので元の並びに戻してから飛ぶ
  root.querySelectorAll('[data-jump-group]').forEach(function (a) {
    a.addEventListener('click', function () {
      if (current === 'rank') apply('article');
    });
  });

  sortBox.hidden = false;

  // 大きく順位を上げたおもちゃの印を、画面に入ったときに 1 回だけ跳ねさせる
  if (!reduceMotion() && 'IntersectionObserver' in window) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) {
          e.target.classList.add('rk-pop');
          io.unobserve(e.target);
        }
      });
    }, { rootMargin: '0px 0px -15% 0px' });
    root.querySelectorAll('.rk-surge').forEach(function (el) { io.observe(el); });
  }
})();
