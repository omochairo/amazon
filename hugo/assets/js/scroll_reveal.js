(function() {
  // IntersectionObserver がサポートされていない環境では機能させない
  if (!('IntersectionObserver' in window)) return;

  function initReveal() {
    // フェードインさせたい主要なブロック要素
    var targets = document.querySelectorAll(
      '.post-content h2, ' +
      '.post-content h3, ' +
      '.hero-score, ' +
      '.hero-pros-cons, ' +
      '.score-recap, ' +
      '.price-cta-grid, ' +
      '.keepa-graph, ' +
      '.competitor-grid, ' +
      '.yt-embed, ' +
      '.omcha-card-grid, ' +
      '.related-carousel-section'
    );

    if (!targets.length) return;

    var observerOptions = {
      root: null, // ビューポートを基準
      rootMargin: '0px 0px -80px 0px', // 画面下部から少し入ったところでトリガーしてチラつき防止
      threshold: 0.05
    };

    var observer = new IntersectionObserver(function(entries, observer) {
      entries.forEach(function(entry) {
        if (entry.isIntersecting) {
          var target = entry.target;
          target.classList.add('is-revealed');
          observer.unobserve(target); // 一度フェードインしたら監視解除して負荷軽減
          // #6977: will-change は遷移中だけレイヤーを確保するためのヒント。付けっぱなしに
          // すると合成レイヤーが常駐し続け、内部で独立スクロールする要素(カルーセル等)と
          // ネストしたときに描画がティアリングする一因になりうる。遷移完了で外す。
          target.addEventListener('transitionend', function onRevealEnd() {
            target.style.willChange = 'auto';
          }, { once: true });
        }
      });
    }, observerOptions);

    targets.forEach(function(target) {
      target.classList.add('scroll-reveal');
      observer.observe(target);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initReveal);
  } else {
    initReveal();
  }
})();
