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
      
      const updateButtons = () => {
        const scrollLeft = wrapper.scrollLeft;
        const maxScroll = wrapper.scrollWidth - wrapper.clientWidth;
        prevBtn.disabled = scrollLeft <= 2;
        nextBtn.disabled = scrollLeft >= maxScroll - 2;
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
        if (autoPlayTimer) return;
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
      
      // Start Autoplay
      startTimer();
      
      // Pause autoplay on mouse enter / touch start
      carousel.addEventListener('mouseenter', stopTimer);
      carousel.addEventListener('mouseleave', startTimer);
      wrapper.addEventListener('touchstart', stopTimer, { passive: true });
      wrapper.addEventListener('touchend', startTimer, { passive: true });
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initCarousel);
  } else {
    initCarousel();
  }
})();
