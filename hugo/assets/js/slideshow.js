/* #6206 chrome 削減: partials/extend_head.html のインライン <script> (商品スライド
   ショー) をここへ。実測 6.9 MB / 全 6,369 枚。
   readyState 判定を内包しているので defer でそのまま動く。 */
(function(){
  function init(root){
    var slides = root.querySelectorAll('.slide');
    var dots = root.querySelectorAll('.slide-thumb');
    var counter = root.querySelector('.slide-current');
    var prev = root.querySelector('.slide-prev');
    var next = root.querySelector('.slide-next');
    if(!slides.length) return;
    var idx = 0;
    var autoplay = parseInt(root.getAttribute('data-autoplay') || '0', 10);
    var timer = null;
    function show(i){
      if(i < 0) i = slides.length - 1;
      if(i >= slides.length) i = 0;
      slides[idx].classList.remove('active');
      if(dots[idx]) dots[idx].classList.remove('active');
      idx = i;
      slides[idx].classList.add('active');
      if(dots[idx]) dots[idx].classList.add('active');
      if(counter) counter.textContent = idx + 1;
    }
    function restart(){
      if(!autoplay) return;
      if(timer) clearInterval(timer);
      timer = setInterval(function(){ show(idx + 1); }, autoplay);
    }
    if(prev) prev.addEventListener('click', function(){ show(idx - 1); restart(); });
    if(next) next.addEventListener('click', function(){ show(idx + 1); restart(); });
    dots.forEach(function(d){
      d.addEventListener('click', function(){
        show(parseInt(d.getAttribute('data-idx'), 10));
        restart();
      });
    });
    root.addEventListener('mouseenter', function(){ if(timer) clearInterval(timer); });
    root.addEventListener('mouseleave', restart);
    restart();
  }
  function boot(){
    document.querySelectorAll('.product-slideshow').forEach(init);
  }
  if(document.readyState === 'loading'){
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
