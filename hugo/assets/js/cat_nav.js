/* #6206 chrome 削減: partials/category_dropdown.html のインライン <script> を
   ここへ。実測 4.7 MB / 全 6,369 枚。
   .cat-nav を即座に querySelector するが、defer は DOM 構築後に走るので
   partial の末尾に置かれていたときと同じ条件になる。 */
(function () {
    var nav = document.querySelector(".cat-nav");
    if (!nav) { return; }
    var list = nav.querySelector(".cat-nav-list");
    // 子が 1 個のグループは <a class="cat-summary"> (直接リンク) で出るので、
    // 開閉の対象はパネルを持つ button だけに絞る。
    var btns = Array.prototype.slice.call(nav.querySelectorAll(".cat-summary[data-cat-target]"));
    function panelOf(btn) {
        return document.getElementById(btn.getAttribute("data-cat-target"));
    }
    function close(btn) {
        btn.setAttribute("aria-expanded", "false");
        var p = panelOf(btn);
        if (p) { p.hidden = true; }
    }
    function closeAll(except) {
        btns.forEach(function (b) { if (b !== except) { close(b); } });
    }
    function openBtn() {
        for (var i = 0; i < btns.length; i++) {
            if (btns[i].getAttribute("aria-expanded") === "true") { return btns[i]; }
        }
        return null;
    }
    btns.forEach(function (b) {
        b.addEventListener("click", function (e) {
            var open = b.getAttribute("aria-expanded") === "true";
            closeAll(b);
            if (open) {
                close(b);
            } else {
                b.setAttribute("aria-expanded", "true");
                var p = panelOf(b);
                if (p) {
                    p.hidden = false;
                    // パネルは DOM 上すべての親ボタンの「後ろ」にあるので、Tab だと残りの
                    // ボタンを全部通らないと中のリンクに届かない。キーボードで開いたとき
                    // (Enter / Space 由来の click は detail が 0) は先頭のリンクへ移す。
                    if (e && e.detail === 0) {
                        var first = p.querySelector("a");
                        if (first) { first.focus(); }
                    }
                }
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
    // Tab の順路。パネルは DOM 上すべての chip の「後ろ」にあるので、そのままだと
    // 開いたボタンから Tab で残りの chip を全部通らないと中のリンクに届かず、
    // 先頭リンクから Shift+Tab で戻ると末尾の chip へ飛ぶ。開いている間だけ
    // 「開いたボタン → パネルのリンク → 次の chip」の順に繋ぎ替える。
    var chips = Array.prototype.slice.call(nav.querySelectorAll(".cat-summary"));
    function tabThroughPanel(e) {
        var b = openBtn();
        var p = b && panelOf(b);
        var links = p ? p.querySelectorAll("a") : [];
        if (!links.length) { return; }
        var first = links[0];
        var last = links[links.length - 1];
        // 開いたボタンが末尾の chip のときは next が無い。パネルが DOM の最後なので
        // 既定の Tab でそのままナビの外へ抜ける。
        var next = chips[chips.indexOf(b) + 1] || null;
        var at = document.activeElement;
        var to = null;
        if (e.shiftKey) {
            if (at === first) { to = b; } else if (next && at === next) { to = last; }
        } else if (at === b) {
            to = first;
        } else if (at === last) {
            to = next;
        }
        if (to) {
            e.preventDefault();
            to.focus();
        }
    }
    // Esc で閉じる。フォーカスがナビの中にあるときは、開いていたボタンへ戻す。
    document.addEventListener("keydown", function (e) {
        if (e.key === "Tab") { tabThroughPanel(e); return; }
        if (e.key !== "Escape") { return; }
        var b = openBtn();
        if (!b) { return; }
        var inside = nav.contains(document.activeElement);
        closeAll(null);
        if (inside) { b.focus(); }
    });

    // 現在地の表示。このナビは partialCached で全ページ共通の HTML なので、
    // テンプレートではなくここで URL を照合して付ける。
    function norm(path) { return path.replace(/\/?$/, "/"); }
    var here = norm(location.pathname);
    var currentChip = null;
    Array.prototype.forEach.call(nav.querySelectorAll("a[href]"), function (a) {
        if (norm(a.pathname) !== here) { return; }
        a.setAttribute("aria-current", "page");
        var chip = a;
        var panel = a.closest(".cat-panel");
        if (panel) {
            chip = nav.querySelector('.cat-summary[data-cat-target="' + panel.id + '"]');
            if (chip) { chip.classList.add("is-current"); }
        }
        currentChip = currentChip || chip;
    });

    // スマホの横スクロール。スクロールバーを隠しているので、続きがある側の端を
    // ぼかして知らせる (PC は折り返し表示で scrollWidth == clientWidth になり付かない)。
    if (!list) { return; }
    function updateHint() {
        var max = list.scrollWidth - list.clientWidth;
        list.classList.toggle("has-more-left", list.scrollLeft > 4);
        list.classList.toggle("has-more-right", list.scrollLeft < max - 4);
    }
    if (currentChip && list.scrollWidth > list.clientWidth) {
        var lr = list.getBoundingClientRect();
        var cr = currentChip.getBoundingClientRect();
        if (cr.right > lr.right || cr.left < lr.left) {
            // 左端のぼかし (36px) に現在地の chip が掛からない位置で止める。
            list.scrollLeft += cr.left - lr.left - 40;
        }
    }
    list.addEventListener("scroll", updateHint, { passive: true });
    window.addEventListener("resize", updateHint);
    updateHint();
})();
