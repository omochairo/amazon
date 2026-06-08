// hugo/assets/js/sw_register.js — Service Worker 登録 + 最近見た商品の precache (#1371)
//
// - /sw.js を root scope で登録 (Cloudflare Pages は static/ を root 配信)。
// - 登録後、最近見た商品 (OmochaRecent.list) の遷移先 URL を SW に postMessage し、
//   商品ページ HTML を意図的に precache させる (オフライン carousel 遷移用 / Phase 2)。
// - ?nocache=1 が付いていれば登録しない (開発 bypass)。
(function () {
  if (!("serviceWorker" in navigator)) return;
  try {
    if (new URL(location.href).searchParams.get("nocache") === "1") return;
  } catch (e) { /* noop */ }

  function precacheRecent(reg) {
    try {
      var target = (reg && reg.active) || navigator.serviceWorker.controller;
      if (!target || !window.OmochaRecent || typeof OmochaRecent.list !== "function") return;
      var urls = OmochaRecent.list().map(function (r) {
        return r.url || (r.asin ? "/products/" + r.asin.toLowerCase() + "/" : null);
      }).filter(Boolean);
      if (urls.length) target.postMessage({ type: "PRECACHE_URLS", urls: urls });
    } catch (e) { /* never break page */ }
  }

  window.addEventListener("load", function () {
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).then(function (reg) {
      // active なら即 precache、まだなら controllerchange を待つ。
      if (reg.active) {
        precacheRecent(reg);
      } else {
        navigator.serviceWorker.ready.then(function (r) { precacheRecent(r); });
      }
    }).catch(function () { /* 登録失敗は無視 (HTTPS/scope 等) */ });
  });
})();
