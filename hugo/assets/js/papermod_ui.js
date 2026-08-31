/* #6206 chrome 削減: partials/footer.html の 3 つのインライン <script> をまとめた。
   実測 (2026-08-31): menu 4.5MB + top-link 1.6MB + theme-toggle 1.8MB = 7.9MB が
   全 6,369 枚に複製されていた。1 ファイルにまとめてリクエストは 1 本に保つ。

   defer で読むので DOM 構築後・DOMContentLoaded 前に走る。元は </body> 直前の
   同期スクリプトだったので、DOM が揃っている点は変わらない。

   **`data-theme` の初期化はここに入れてはいけない。** あれは head の同期
   スクリプトのままにする (paint 前に走らないとテーマがちらつく)。ここにあるのは
   クリックハンドラだけ。 */
(function () {
    let menu = document.getElementById('menu');
    if (menu) {
        // Set the scroll position
        const scrollPosition = localStorage.getItem("menu-scroll-position");
        if (scrollPosition) {
            menu.scrollLeft = parseInt(scrollPosition, 10);
        }
        
        menu.onscroll = function () {
            localStorage.setItem("menu-scroll-position", menu.scrollLeft);
        }
    }

    document.querySelectorAll('a[href^="#"]').forEach(anchor => {
        anchor.addEventListener("click", function (e) {
            e.preventDefault();
            var id = this.getAttribute("href").substr(1);
            if (!window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
                document.querySelector(`[id='${decodeURIComponent(id)}']`).scrollIntoView({
                    behavior: "smooth"
                });
            } else {
                document.querySelector(`[id='${decodeURIComponent(id)}']`).scrollIntoView();
            }
            if (id === "top") {
                history.replaceState(null, null, " ");
            } else {
                history.pushState(null, null, `#${id}`);
            }
        });
    });


  /* --- scroll to top (旧 site.Params.disableScrollToTop ガード) --- */
    var toplink = document.getElementById("top-link");
    window.onscroll = function () {
        const scrollThreshold = window.innerHeight;
        if (document.body.scrollTop > scrollThreshold || document.documentElement.scrollTop > scrollThreshold) {
            toplink.classList.remove("hidden");
        } else {
            toplink.classList.add("hidden");
        }
    };


  /* --- theme toggle (旧 site.Params.disableThemeToggle ガード) --- */
    document.getElementById("theme-toggle").addEventListener("click", () => {
        const html = document.querySelector("html");
        if (html.dataset.theme === "dark") {
            html.dataset.theme = 'light';
            localStorage.setItem("pref-theme", 'light');
        } else {
            html.dataset.theme = 'dark';
            localStorage.setItem("pref-theme", 'dark');
        }
    })

})();
