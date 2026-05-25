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
      '.price-cta-grid, ' +
      '.keepa-graph, ' +
      '.competitor-grid, ' +
      '.yt-embed, ' +
      '.omcha-card-grid'
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
          entry.target.classList.add('is-revealed');
          observer.unobserve(entry.target); // 一度フェードインしたら監視解除して負荷軽減
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
