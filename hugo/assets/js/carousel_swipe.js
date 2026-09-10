(function() {
  function initCarousel() {
    const carousels = document.querySelectorAll('.related-carousel-section');
    carousels.forEach(carousel => {
      const wrapper = carousel.querySelector('.carousel-wrapper');
      const prevBtn = carousel.querySelector('.carousel-arrow.prev');
      const nextBtn = carousel.querySelector('.carousel-arrow.next');
      
      if (!wrapper || !prevBtn || !nextBtn) return;

      let autoPlayTimer = null;
      const autoPlayInterval = 5000; // 5 seconds
      // #6977: IntersectionObserver 非対応環境は従来通り常時オートプレイ扱いにする
      let isInView = !('IntersectionObserver' in window);
      
      const updateButtons = () => {
        const scrollLeft = wrapper.scrollLeft;
        const maxScroll = wrapper.scrollWidth - wrapper.clientWidth;
        const atStart = scrollLeft <= 2;
        const atEnd = scrollLeft >= maxScroll - 2;
        prevBtn.disabled = atStart;
        nextBtn.disabled = atEnd;
        // #3568 E3: 端フェード (.carousel-wrapper::before/::after) を終端で消す。
        // 矢印 disabled 判定にそのまま便乗するので追加コストはほぼゼロ。
        carousel.classList.toggle('is-at-start', atStart);
        carousel.classList.toggle('is-at-end', atEnd);
      };
      
      // Initialize button states
      updateButtons();
      
      // Update on scroll & resize
      wrapper.addEventListener('scroll', updateButtons, { passive: true });
      window.addEventListener('resize', updateButtons, { passive: true });
      
      // Slide navigation
      const slide = (direction) => {
        const scrollAmount = Math.min(wrapper.clientWidth * 0.8, 300);
        const maxScroll = wrapper.scrollWidth - wrapper.clientWidth;
        
        if (direction === 'next') {
          // If reached the end, wrap around to the beginning
          if (wrapper.scrollLeft >= maxScroll - 5) {
            wrapper.scrollTo({
              left: 0,
              behavior: 'smooth'
            });
          } else {
            wrapper.scrollBy({
              left: scrollAmount,
              behavior: 'smooth'
            });
          }
        } else {
          // If reached the beginning, wrap around to the end
          if (wrapper.scrollLeft <= 5) {
            wrapper.scrollTo({
              left: maxScroll,
              behavior: 'smooth'
            });
          } else {
            wrapper.scrollBy({
              left: -scrollAmount,
              behavior: 'smooth'
            });
          }
        }
      };
      
      prevBtn.addEventListener('click', () => {
        slide('prev');
        resetTimer();
      });
      
      nextBtn.addEventListener('click', () => {
        slide('next');
        resetTimer();
      });
      
      // Autoplay logic
      const startTimer = () => {
        // #6977: 画面外(または初回入場アニメーション中)は起動しない。入場フェードイン
        // (0.7s) の transform/opacity トランジションと、この横スクロールが同時に走ると
        // ネストした合成レイヤーの再ラスタライズが間に合わずティアリングする一因になる。
        if (autoPlayTimer || !isInView) return;
        autoPlayTimer = setInterval(() => {
          slide('next');
        }, autoPlayInterval);
      };

      const stopTimer = () => {
        if (autoPlayTimer) {
          clearInterval(autoPlayTimer);
          autoPlayTimer = null;
        }
      };

      const resetTimer = () => {
        stopTimer();
        startTimer();
      };

      // Start Autoplay (画面内に入ってから; #6977)
      if ('IntersectionObserver' in window) {
        const visibilityObserver = new IntersectionObserver((entries) => {
          entries.forEach((entry) => {
            isInView = entry.isIntersecting;
            if (isInView) {
              startTimer();
            } else {
              stopTimer();
            }
          });
        }, { threshold: 0.2 });
        visibilityObserver.observe(carousel);
      } else {
        startTimer();
      }

      // Pause autoplay on mouse enter / touch start
      carousel.addEventListener('mouseenter', stopTimer);
      carousel.addEventListener('mouseleave', startTimer);
      wrapper.addEventListener('touchstart', stopTimer, { passive: true });
      wrapper.addEventListener('touchend', startTimer, { passive: true });
    });
  }

  /* #5081: スクロールスナップは **最初のユーザー操作まで付けない**。

     .carousel-wrapper / .age-timeline-track は overflow-x:auto のスナップ
     コンテナで、読み込み中に中身 (画像・カード) が入るたび再スナップして
     scrollLeft が動く。Chrome はそのスクロールで LCP 候補の記録を打ち切るため、
     **商品ページは LCP 候補が 1 件も記録されない**状態だった (実測 2026-08-31:
     trace の largestContentfulPaint::Candidate 0 件 / Invalidate 9 件。
     PerformanceObserver でも LCP エントリ 0 件。scroll-snap-type を切ると同じ
     ページで LCP が記録される)。

     `load` を境にしないのは、`load` が最初の描画より前に来る条件 (画像が
     すべてキャッシュ/失敗した場合など) が実在し、そこで付けると同じ穴に
     戻るため。**LCP はどのみち最初の入力で確定する**ので、入力を境にすれば
     吸着感を落とさずに計測とぶつからない。 */
  function enableSnap() {
    document.documentElement.classList.add('snap-ready');
  }
  ['pointerdown', 'touchstart', 'wheel', 'keydown'].forEach(function (type) {
    window.addEventListener(type, enableSnap, { once: true, passive: true, capture: true });
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initCarousel);
  } else {
    initCarousel();
  }

})();
