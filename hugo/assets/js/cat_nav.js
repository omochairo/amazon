/* #6206 chrome 削減: partials/category_dropdown.html のインライン <script> を
   ここへ。実測 4.7 MB / 全 6,369 枚。
   .cat-nav を即座に querySelector するが、defer は DOM 構築後に走るので
   partial の末尾に置かれていたときと同じ条件になる。 */
(function () {
    var nav = document.querySelector(".cat-nav");
    if (!nav) { return; }
    var btns = Array.prototype.slice.call(nav.querySelectorAll(".cat-summary"));
    function close(btn) {
        btn.setAttribute("aria-expanded", "false");
        var p = document.getElementById(btn.getAttribute("data-cat-target"));
        if (p) { p.hidden = true; }
    }
    function closeAll(except) {
        btns.forEach(function (b) { if (b !== except) { close(b); } });
    }
    btns.forEach(function (b) {
        b.addEventListener("click", function () {
            var open = b.getAttribute("aria-expanded") === "true";
            closeAll(b);
            if (open) {
                close(b);
            } else {
                b.setAttribute("aria-expanded", "true");
                var p = document.getElementById(b.getAttribute("data-cat-target"));
                if (p) { p.hidden = false; }
            }
        });
    });
    // バー外クリック / bfcache 復元時は閉じる。
    document.addEventListener("click", function (e) {
        if (!nav.contains(e.target)) { closeAll(null); }
    });
    window.addEventListener("pageshow", function (e) {
        if (e.persisted) { closeAll(null); }
    });
})();
