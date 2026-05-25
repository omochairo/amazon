(function() {
  function initCarousel() {
    const carousels = document.querySelectorAll('.related-carousel-section');
    carousels.forEach(carousel => {
      const wrapper = carousel.querySelector('.carousel-wrapper');
      const prevBtn = carousel.querySelector('.carousel-arrow.prev');
      const nextBtn = carousel.querySelector('.carousel-arrow.next');
      
      if (!wrapper || !prevBtn || !nextBtn) return;
      
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
      prevBtn.addEventListener('click', () => {
        const scrollAmount = Math.min(wrapper.clientWidth * 0.8, 320);
        wrapper.scrollBy({
          left: -scrollAmount,
          behavior: 'smooth'
        });
      });
      
      nextBtn.addEventListener('click', () => {
        const scrollAmount = Math.min(wrapper.clientWidth * 0.8, 320);
        wrapper.scrollBy({
          left: scrollAmount,
          behavior: 'smooth'
        });
      });
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initCarousel);
  } else {
    initCarousel();
  }
})();
