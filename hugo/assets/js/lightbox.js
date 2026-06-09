/* epic #1365 Layer 4: 画像 lightbox + ピンチズーム
   商品メイン画像 (figure.product-hero-image img) とギャラリー
   (.product-slideshow .slideshow-stage img) をクリックで全画面拡大。
   touch ピンチ / ホイール / ドラッグ pan / ダブルタップ切替 /
   ギャラリーは ← → 送り / ESC・背景・✕ で閉じる。商品ページ限定。 */
(function () {
  'use strict';

  var overlay, imgEl, captionEl, counterEl, prevBtn, nextBtn;
  var group = [], index = 0, lastFocus = null;
  var scale = 1, tx = 0, ty = 0;
  var MIN = 1, MAX = 4;

  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }
  function dist(a, b) { return Math.hypot(a.x - b.x, a.y - b.y); }

  function applyTransform() {
    imgEl.style.transform = 'translate(' + tx + 'px,' + ty + 'px) scale(' + scale + ')';
    overlay.classList.toggle('is-zoomed', scale > 1.01);
  }
  function resetTransform() { scale = 1; tx = 0; ty = 0; applyTransform(); }

  function buildOverlay() {
    if (overlay) return;
    overlay = document.createElement('div');
    overlay.className = 'lightbox-overlay';
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');
    overlay.setAttribute('aria-label', '商品画像の拡大表示');
    overlay.innerHTML =
      '<button type="button" class="lightbox-close" aria-label="閉じる">×</button>' +
      '<button type="button" class="lightbox-nav lightbox-prev" aria-label="前の画像">‹</button>' +
      '<button type="button" class="lightbox-nav lightbox-next" aria-label="次の画像">›</button>' +
      '<figure class="lightbox-figure"><img class="lightbox-img" alt="">' +
      '<figcaption class="lightbox-caption"></figcaption></figure>' +
      '<div class="lightbox-counter" aria-hidden="true"></div>' +
      '<div class="lightbox-hint">ピンチ / ホイールで拡大・ドラッグで移動</div>';
    document.body.appendChild(overlay);
    imgEl = overlay.querySelector('.lightbox-img');
    captionEl = overlay.querySelector('.lightbox-caption');
    counterEl = overlay.querySelector('.lightbox-counter');
    prevBtn = overlay.querySelector('.lightbox-prev');
    nextBtn = overlay.querySelector('.lightbox-next');
    overlay.querySelector('.lightbox-close').addEventListener('click', close);
    prevBtn.addEventListener('click', function (e) { e.stopPropagation(); step(-1); });
    nextBtn.addEventListener('click', function (e) { e.stopPropagation(); step(1); });
    overlay.addEventListener('click', function (e) {
      if (e.target === overlay || e.target.classList.contains('lightbox-figure')) close();
    });
    bindZoom();
  }

  function load(i) {
    index = (i + group.length) % group.length;
    var item = group[index];
    imgEl.src = item.full;
    imgEl.alt = item.alt || '';
    captionEl.textContent = item.alt || '';
    var multi = group.length > 1;
    prevBtn.hidden = !multi;
    nextBtn.hidden = !multi;
    counterEl.hidden = !multi;
    counterEl.textContent = multi ? (index + 1) + ' / ' + group.length : '';
    resetTransform();
  }
  function step(d) { load(index + d); }

  function open(grp, i) {
    buildOverlay();
    group = grp;
    lastFocus = document.activeElement;
    load(i);
    overlay.classList.add('is-open');
    document.body.classList.add('lightbox-lock');
    overlay.querySelector('.lightbox-close').focus();
    document.addEventListener('keydown', onKey);
  }
  function close() {
    if (!overlay) return;
    overlay.classList.remove('is-open', 'is-zoomed');
    document.body.classList.remove('lightbox-lock');
    document.removeEventListener('keydown', onKey);
    imgEl.removeAttribute('src');
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }
  function onKey(e) {
    if (e.key === 'Escape') close();
    else if (e.key === 'ArrowLeft' && group.length > 1) step(-1);
    else if (e.key === 'ArrowRight' && group.length > 1) step(1);
  }

  function bindZoom() {
    var pointers = {};
    var startDist = 0, startScale = 1;
    var dragging = false, dragStart = null, lastTap = 0;

    function pts() {
      return Object.keys(pointers).map(function (k) { return pointers[k]; });
    }
    function toggleZoom() {
      if (scale > 1.01) { resetTransform(); }
      else { scale = 2; applyTransform(); }
    }

    imgEl.addEventListener('pointerdown', function (e) {
      pointers[e.pointerId] = { x: e.clientX, y: e.clientY };
      try { imgEl.setPointerCapture(e.pointerId); } catch (err) {}
      var p = pts();
      if (p.length === 2) {
        startDist = dist(p[0], p[1]);
        startScale = scale;
      } else if (p.length === 1) {
        dragging = scale > 1.01;
        dragStart = { x: e.clientX - tx, y: e.clientY - ty };
        var now = Date.now();
        if (now - lastTap < 300) { toggleZoom(); lastTap = 0; } else { lastTap = now; }
      }
    });
    imgEl.addEventListener('pointermove', function (e) {
      if (!pointers[e.pointerId]) return;
      pointers[e.pointerId] = { x: e.clientX, y: e.clientY };
      var p = pts();
      if (p.length === 2 && startDist) {
        scale = clamp(startScale * (dist(p[0], p[1]) / startDist), MIN, MAX);
        applyTransform();
      } else if (p.length === 1 && dragging) {
        tx = e.clientX - dragStart.x;
        ty = e.clientY - dragStart.y;
        applyTransform();
      }
    });
    function release(e) {
      delete pointers[e.pointerId];
      var n = pts().length;
      if (n < 2) startDist = 0;
      if (n === 0) { dragging = false; if (scale < 1.05) resetTransform(); }
    }
    imgEl.addEventListener('pointerup', release);
    imgEl.addEventListener('pointercancel', release);
    imgEl.addEventListener('dblclick', function (e) { e.preventDefault(); toggleZoom(); });
    imgEl.addEventListener('wheel', function (e) {
      e.preventDefault();
      scale = clamp(scale * (e.deltaY < 0 ? 1.15 : 1 / 1.15), MIN, MAX);
      if (scale <= MIN) { tx = 0; ty = 0; }
      applyTransform();
    }, { passive: false });
  }

  function trigger(im, grpFn, i) {
    im.classList.add('lightbox-trigger');
    im.addEventListener('click', function () { open(grpFn(), i); });
  }

  function collect() {
    var hero = document.querySelector('.post-content figure.product-hero-image img');
    if (hero) {
      trigger(hero, function () {
        return [{ full: hero.currentSrc || hero.src, alt: hero.alt }];
      }, 0);
    }
    var shows = document.querySelectorAll('.post-content .product-slideshow');
    Array.prototype.forEach.call(shows, function (sw) {
      var slides = Array.prototype.slice.call(sw.querySelectorAll('.slideshow-stage img'));
      if (!slides.length) return;
      var grpFn = function () {
        return slides.map(function (im) { return { full: im.currentSrc || im.src, alt: im.alt }; });
      };
      slides.forEach(function (im, i) { trigger(im, grpFn, i); });
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', collect);
  } else {
    collect();
  }
})();
