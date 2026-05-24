/*!
 * wiki_tooltip.js — Wikipedia 連携ボトムシート型 教育的背景解説
 * Issue: https://github.com/omochairo/amazon/issues/683
 * 出典: Wikipedia 要約は CC BY-SA 4.0 (cache 内 license 注記に従う)
 */
(function () {
  'use strict';

  var CACHE_URL = '/wiki_cache.json';
  var CONTENT_SEL = '.post-content';
  var SKIP_TAGS = { A: 1, CODE: 1, PRE: 1, SCRIPT: 1, STYLE: 1, NOSCRIPT: 1, BUTTON: 1, TEXTAREA: 1, INPUT: 1 };
  var SKIP_ATTR = 'data-wiki-skip';
  var STATE = { cache: null, opened: false, lastFocus: null, sheet: null, overlay: null, popHandler: null };
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

  function isAscii(s) { return /^[\x20-\x7E]+$/.test(s); }

  function escapeRe(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }

  // 最長一致用に長さ降順でソート。ASCII キーは単語境界を付与して英単語誤マッチを防ぐ。
  function buildPatterns(keys) {
    var ascii = [], cjk = [];
    keys.forEach(function (k) { (isAscii(k) ? ascii : cjk).push(k); });
    ascii.sort(function (a, b) { return b.length - a.length; });
    cjk.sort(function (a, b) { return b.length - a.length; });
    var patterns = [];
    if (ascii.length) {
      patterns.push(new RegExp('(?:^|(?<=[^A-Za-z0-9]))(' + ascii.map(escapeRe).join('|') + ')(?=$|[^A-Za-z0-9])', 'g'));
    }
    if (cjk.length) {
      patterns.push(new RegExp('(' + cjk.map(escapeRe).join('|') + ')', 'g'));
    }
    return patterns;
  }

  function shouldSkipNode(node) {
    for (var p = node.parentNode; p && p !== document.body; p = p.parentNode) {
      if (p.nodeType !== 1) continue;
      if (SKIP_TAGS[p.tagName]) return true;
      if (p.hasAttribute && p.hasAttribute(SKIP_ATTR)) return true;
      if (p.classList && p.classList.contains('wiki-term')) return true;
    }
    return false;
  }

  // テキストノードをスキャンして、マッチを <button.wiki-term> に置換。
  // seen: ページ全体で一度マークしたキーは以後スキップ (first-occurrence-only)。
  function replaceInNode(node, patterns, seen) {
    var text = node.nodeValue;
    if (!text || text.length < 2) return 0;
    var matches = [];
    patterns.forEach(function (re) {
      re.lastIndex = 0;
      var m;
      while ((m = re.exec(text)) !== null) {
        var term = m[1];
        if (seen[term]) continue;
        matches.push({ start: m.index + m[0].indexOf(term), end: m.index + m[0].indexOf(term) + term.length, term: term });
      }
    });
    if (!matches.length) return 0;
    matches.sort(function (a, b) { return a.start - b.start || b.end - b.start - (a.end - a.start); });
    var picked = [], cursor = 0;
    matches.forEach(function (m) {
      if (m.start >= cursor && !seen[m.term]) {
        picked.push(m);
        seen[m.term] = true;
        cursor = m.end;
      }
    });
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
    var patterns = buildPatterns(keys);
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: function (n) {
        if (shouldSkipNode(n)) return NodeFilter.FILTER_REJECT;
        return n.nodeValue && n.nodeValue.trim().length >= 2 ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
      }
    });
    var nodes = [], n;
    while ((n = walker.nextNode())) nodes.push(n);
    var seen = {};
    nodes.forEach(function (node) { replaceInNode(node, patterns, seen); });
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
    attachSwipe(sheet);
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
  window.__wikiTooltip = { buildPatterns: buildPatterns, decorate: decorate, state: STATE };
  // WIKI_TOOLTIP_UI_BELOW
})();
