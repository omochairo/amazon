/*!
 * wiki_tooltip.js — Wikipedia 連携ボトムシート型 教育的背景解説
 * Issue: https://github.com/omochairo/amazon/issues/683
 * 出典: Wikipedia 要約は CC BY-SA 4.0 (cache 内 license 注記に従う)
 */
(function () {
  'use strict';

  var CACHE_URL = '/wiki_cache.json';
  var CONTENT_SEL = '.post-content';
  // 💡 を差し込まない要素。見出し・summary・small・強調・リンクの中は、本文の流れを
  // 邪魔する (見出しに 💡 が出る・強調の語が二重に目立つ) ので当てない (#165 T5)。
  var SKIP_TAGS = {
    A: 1, CODE: 1, PRE: 1, SCRIPT: 1, STYLE: 1, NOSCRIPT: 1, BUTTON: 1, TEXTAREA: 1, INPUT: 1,
    H1: 1, H2: 1, H3: 1, H4: 1, H5: 1, H6: 1, SUMMARY: 1, SMALL: 1, STRONG: 1, B: 1
  };
  var SKIP_ATTR = 'data-wiki-skip';
  // テンプレートが全ページに入れる決まり文句の塊。語の解説を出す場所ではないので当てない。
  // score-breakdown-doc は加減点根拠の機械的な箇条書き (知育領域の列挙) で、ここに 💡 が出ると
  // 全ページの同じ位置に同じ語が付く。
  var SKIP_CLASSES = ['score-axes-doc', 'score-breakdown-doc', 'development-stage-accordion', 'where-to-buy-offline-note', 'auto-gen-notice'];
  // 語を含むが別のもの (別の会社・別の語) を指す句。字種の規則では防げないものをここで除く。
  // 例: 「フレーベル館」(出版社) の「フレーベル」は教育者フレーベルの解説とは無関係。
  var STOP_PHRASES = ['フレーベル館'];
  // 1 ページに出す 💡 の上限。同じ解説 (同じリンク先) は 1 回まで。
  var MAX_TERMS_PER_PAGE = 5;
  var STATE = { cache: null, opened: false, lastFocus: null, sheet: null, overlay: null, popHandler: null, detachSwipe: null };
  var REDUCED_MOTION = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  function idle(fn) {
    if (typeof requestIdleCallback === 'function') requestIdleCallback(fn, { timeout: 2000 });
    else setTimeout(fn, 200);
  }

  function loadCache() {
    return fetch(CACHE_URL, { credentials: 'omit' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; });
  }

  // ---- 語の選定 (DOM に触らない純粋な関数。tests/js/wiki_tooltip.test.mjs で検証) ----

  // 字種。カタカナ・漢字・英数字は「同じ字種が続く = 語の途中」とみなす。
  // ひらがなは助詞が続くので対象外 (null)。
  function charClass(ch) {
    if (!ch) return null;
    var c = ch.charCodeAt(0);
    if ((c >= 0x30A1 && c <= 0x30FA) || c === 0x30FC || (c >= 0x30FD && c <= 0x30FF) ||
        (c >= 0x31F0 && c <= 0x31FF) || (c >= 0xFF66 && c <= 0xFF9F)) return 'kata';
    if ((c >= 0x4E00 && c <= 0x9FFF) || (c >= 0x3400 && c <= 0x4DBF) || c === 0x3005 || c === 0x3007) return 'kanji';
    if ((c >= 0x30 && c <= 0x39) || (c >= 0x41 && c <= 0x5A) || (c >= 0x61 && c <= 0x7A)) return 'alnum';
    return null;
  }

  // text[start,end) の前後に、語の端と同じ字種が続くなら false (別の語の一部)。
  // 例: 「マルカン」の「マルカ」・「お試し感覚」の「感覚」・「フレーベル館」の「フレーベル」。
  // 英数字の語 (Hape など) は元々の「前後が英数字なら不可」と同じ判定になる。
  function boundaryOk(text, start, end) {
    var first = charClass(text.charAt(start));
    if (first && charClass(text.charAt(start - 1)) === first) return false;
    var last = charClass(text.charAt(end - 1));
    if (last && charClass(text.charAt(end)) === last) return false;
    return true;
  }

  // 「同じ解説」を表すキー。別名 (コナミ / KONAMI) は同じ url なので 1 回にまとまる。
  function explanationKey(entries, term) {
    var e = entries[term] || {};
    return e.url || ('title:' + (e.title || term));
  }

  // 最長一致を優先するため長さ降順に並べる。
  function sortKeys(keys) {
    return keys.slice().sort(function (a, b) { return b.length - a.length; });
  }

  function inStopPhrase(stops, start, end) {
    for (var i = 0; i < stops.length; i++) if (start >= stops[i][0] && end <= stops[i][1]) return true;
    return false;
  }

  // text の中から、語の境界が正しい候補をすべて集める。
  function findCandidates(text, sortedKeys) {
    var out = [];
    var stops = [];
    for (var j = 0; j < STOP_PHRASES.length; j++) {
      var sp = STOP_PHRASES[j], sf = 0, sa;
      while ((sa = text.indexOf(sp, sf)) !== -1) { stops.push([sa, sa + sp.length]); sf = sa + sp.length; }
    }
    for (var i = 0; i < sortedKeys.length; i++) {
      var term = sortedKeys[i];
      if (!term) continue;
      var from = 0, at;
      while ((at = text.indexOf(term, from)) !== -1) {
        if (boundaryOk(text, at, at + term.length) && !inStopPhrase(stops, at, at + term.length)) {
          out.push({ start: at, end: at + term.length, term: term });
        }
        from = at + 1;
      }
    }
    return out;
  }

  // state: { seen: {term:true}, groups: {explanationKey:true}, count: n, max: n }
  // 採用した候補を返し、state を更新する。上限・同じ解説・同じ語・重なりを除く。
  function selectMatches(text, sortedKeys, entries, state) {
    if (!text || text.length < 2 || state.count >= state.max) return [];
    var cands = findCandidates(text, sortedKeys);
    if (!cands.length) return [];
    cands.sort(function (a, b) { return a.start - b.start || (b.end - b.start) - (a.end - a.start); });
    var picked = [], cursor = 0;
    for (var i = 0; i < cands.length; i++) {
      var m = cands[i];
      if (state.count >= state.max) break;
      if (m.start < cursor || state.seen[m.term]) continue;
      var g = explanationKey(entries, m.term);
      if (state.groups[g]) continue;
      picked.push(m);
      state.seen[m.term] = true;
      state.groups[g] = true;
      state.count++;
      cursor = m.end;
    }
    return picked;
  }

  function newState(max) {
    return { seen: Object.create(null), groups: Object.create(null), count: 0, max: max == null ? MAX_TERMS_PER_PAGE : max };
  }

  // 要素単体で 💡 を避けるか。祖先をたどる側 (shouldSkipNode) から使う。
  function isSkipElement(el) {
    if (!el || el.nodeType !== 1) return false;
    if (SKIP_TAGS[el.tagName]) return true;
    if (el.hasAttribute && el.hasAttribute(SKIP_ATTR)) return true;
    var cl = el.classList;
    if (cl) {
      if (cl.contains('wiki-term')) return true;
      for (var i = 0; i < SKIP_CLASSES.length; i++) if (cl.contains(SKIP_CLASSES[i])) return true;
    }
    return false;
  }

  function shouldSkipNode(node) {
    for (var p = node.parentNode; p && p !== document.body; p = p.parentNode) {
      if (isSkipElement(p)) return true;
    }
    return false;
  }

  // テキストノードをスキャンして、マッチを <button.wiki-term> に置換。
  function replaceInNode(node, sortedKeys, entries, state) {
    var text = node.nodeValue;
    var picked = selectMatches(text, sortedKeys, entries, state);
    if (!picked.length) return 0;
    var frag = document.createDocumentFragment();
    var pos = 0;
    picked.forEach(function (m) {
      if (m.start > pos) frag.appendChild(document.createTextNode(text.slice(pos, m.start)));
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'wiki-term';
      btn.setAttribute('data-term', m.term);
      btn.setAttribute('aria-haspopup', 'dialog');
      btn.setAttribute('aria-label', m.term + 'の解説を開く');
      btn.appendChild(document.createTextNode(text.slice(m.start, m.end)));
      var hint = document.createElement('span');
      hint.className = 'wiki-term__icon';
      hint.setAttribute('aria-hidden', 'true');
      hint.textContent = '💡';
      btn.appendChild(hint);
      frag.appendChild(btn);
      pos = m.end;
    });
    if (pos < text.length) frag.appendChild(document.createTextNode(text.slice(pos)));
    node.parentNode.replaceChild(frag, node);
    return picked.length;
  }

  function decorate(root, entries) {
    var keys = Object.keys(entries);
    if (!keys.length) return;
    var sorted = sortKeys(keys);
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: function (n) {
        if (shouldSkipNode(n)) return NodeFilter.FILTER_REJECT;
        return n.nodeValue && n.nodeValue.trim().length >= 2 ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
      }
    });
    var nodes = [], n;
    while ((n = walker.nextNode())) nodes.push(n);
    var state = newState();
    for (var i = 0; i < nodes.length && state.count < state.max; i++) {
      replaceInNode(nodes[i], sorted, entries, state);
    }
  }

  function buildSheet(entry, term) {
    var sheet = document.createElement('div');
    sheet.className = 'wiki-sheet' + (REDUCED_MOTION ? ' wiki-sheet--instant' : '');
    sheet.setAttribute('role', 'dialog');
    sheet.setAttribute('aria-modal', 'true');
    sheet.setAttribute('aria-labelledby', 'wiki-sheet-title');
    sheet.innerHTML =
      '<div class="wiki-sheet__handle" aria-hidden="true"><span class="wiki-sheet__bar"></span></div>' +
      '<div class="wiki-sheet__header">' +
      '<h3 id="wiki-sheet-title" class="wiki-sheet__title"></h3>' +
      '<button type="button" class="wiki-sheet__close" aria-label="閉じる">×</button>' +
      '</div>' +
      '<div class="wiki-sheet__body">' +
      '<p class="wiki-sheet__extract"></p>' +
      '<div class="wiki-sheet__footer">' +
      '<a class="wiki-sheet__link" target="_blank" rel="noopener noreferrer"></a>' +
      '<span class="wiki-sheet__license"></span>' +
      '</div></div>';
    sheet.querySelector('.wiki-sheet__title').textContent = entry.title || term;
    sheet.querySelector('.wiki-sheet__extract').textContent = entry.extract || '';
    var link = sheet.querySelector('.wiki-sheet__link');
    if (entry.url) {
      link.href = entry.url;
      link.textContent = (entry.type === 'wikipedia' ? 'Wikipediaで読む' : '詳しく見る') + ' ↗';
    } else {
      link.style.display = 'none';
    }
    sheet.querySelector('.wiki-sheet__license').textContent =
      entry.type === 'wikipedia' ? '出典: Wikipedia (CC BY-SA 4.0)' : '';
    return sheet;
  }

  function trapFocus(e) {
    if (!STATE.opened || !STATE.sheet) return;
    if (e.key === 'Escape') { e.preventDefault(); closeSheet(); return; }
    if (e.key !== 'Tab') return;
    var focusables = STATE.sheet.querySelectorAll('a[href], button:not([disabled])');
    if (!focusables.length) return;
    var first = focusables[0], last = focusables[focusables.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }

  function attachSwipe(sheet) {
    var startY = null, currentY = null, dragging = false;
    function onStart(e) {
      var t = (e.touches && e.touches[0]) || e;
      startY = t.clientY; currentY = startY; dragging = true;
      sheet.style.transition = 'none';
    }
    function onMove(e) {
      if (!dragging) return;
      var t = (e.touches && e.touches[0]) || e;
      currentY = t.clientY;
      var dy = Math.max(0, currentY - startY);
      sheet.style.transform = 'translateY(' + dy + 'px)';
    }
    function onEnd() {
      if (!dragging) return;
      dragging = false;
      sheet.style.transition = '';
      var dy = (currentY || 0) - (startY || 0);
      if (dy > 50) closeSheet(); else sheet.style.transform = '';
    }
    var handle = sheet.querySelector('.wiki-sheet__handle');
    handle.addEventListener('touchstart', onStart, { passive: true });
    handle.addEventListener('touchmove', onMove, { passive: true });
    handle.addEventListener('touchend', onEnd);
    handle.addEventListener('mousedown', onStart);
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onEnd);
    // closeSheet 側で外せるよう、document に付けたリスナーの解除関数を返す。
    // 元々ここが無く、用語ツールチップを開閉するたびに mousemove/mouseup が
    // document に増え続け、閉じた (DOM からは外れた) sheet を参照するクロージャが
    // 溜まり続けるメモリリークになっていた。
    return function detachSwipe() {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onEnd);
    };
  }

  function openSheet(term, trigger) {
    if (STATE.opened || !STATE.cache) return;
    var entry = STATE.cache.entries[term];
    if (!entry) return;
    STATE.lastFocus = trigger || document.activeElement;
    var overlay = document.createElement('div');
    overlay.className = 'wiki-overlay';
    overlay.addEventListener('click', closeSheet);
    var sheet = buildSheet(entry, term);
    sheet.querySelector('.wiki-sheet__close').addEventListener('click', closeSheet);
    document.body.appendChild(overlay);
    document.body.appendChild(sheet);
    document.body.classList.add('wiki-scroll-lock');
    // 次フレームで open class を付与してスライドアップ
    requestAnimationFrame(function () {
      overlay.classList.add('is-open');
      sheet.classList.add('is-open');
    });
    STATE.sheet = sheet; STATE.overlay = overlay; STATE.opened = true;
    STATE.detachSwipe = attachSwipe(sheet);
    document.addEventListener('keydown', trapFocus);
    setTimeout(function () { sheet.querySelector('.wiki-sheet__close').focus(); }, 50);
    try {
      history.pushState({ wikiSheet: true }, '');
      STATE.popHandler = function () { if (STATE.opened) closeSheet(true); };
      window.addEventListener('popstate', STATE.popHandler);
    } catch (e) { /* history API unavailable; ignore */ }
  }

  function closeSheet(fromPop) {
    if (!STATE.opened) return;
    var sheet = STATE.sheet, overlay = STATE.overlay;
    sheet.classList.remove('is-open');
    overlay.classList.remove('is-open');
    document.removeEventListener('keydown', trapFocus);
    if (STATE.detachSwipe) { STATE.detachSwipe(); STATE.detachSwipe = null; }
    if (STATE.popHandler) {
      window.removeEventListener('popstate', STATE.popHandler);
      STATE.popHandler = null;
      if (!fromPop) { try { history.back(); } catch (e) {} }
    }
    setTimeout(function () {
      if (sheet.parentNode) sheet.parentNode.removeChild(sheet);
      if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
      document.body.classList.remove('wiki-scroll-lock');
      if (STATE.lastFocus && STATE.lastFocus.focus) STATE.lastFocus.focus();
      STATE.sheet = null; STATE.overlay = null; STATE.opened = false; STATE.lastFocus = null;
    }, REDUCED_MOTION ? 0 : 240);
  }

  function boot() {
    var root = document.querySelector(CONTENT_SEL);
    if (!root) return;
    loadCache().then(function (doc) {
      if (!doc || !doc.entries) return;
      STATE.cache = doc;
      idle(function () { decorate(root, doc.entries); });
    });
    document.addEventListener('click', function (e) {
      var btn = e.target.closest && e.target.closest('.wiki-term');
      if (btn) { e.preventDefault(); openSheet(btn.getAttribute('data-term'), btn); }
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();

  // expose for tests / debug
  window.__wikiTooltip = {
    decorate: decorate, state: STATE,
    // 純粋な関数 (テスト用)
    charClass: charClass, boundaryOk: boundaryOk, explanationKey: explanationKey, sortKeys: sortKeys,
    findCandidates: findCandidates, selectMatches: selectMatches, newState: newState, isSkipElement: isSkipElement,
    MAX_TERMS_PER_PAGE: MAX_TERMS_PER_PAGE
  };
  // WIKI_TOOLTIP_UI_BELOW
})();
