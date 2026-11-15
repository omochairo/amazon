/* Age hub breadcrumb click measurement. */
(function () {
  "use strict";
  document.addEventListener("click", function (event) {
    var link = event.target && event.target.closest
      ? event.target.closest('a[data-nav="breadcrumb-age"]')
      : null;
    if (!link || typeof window.gtag !== "function") return;
    window.gtag("event", "breadcrumb_age_click", {
      age_hub: link.getAttribute("data-age-hub") || "",
    });
  });
})();
