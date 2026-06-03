// Issue #600 Phase B: 楽天ランキング順位推移チャート (Chart.js renderer)。
// hugo/layouts/partials/ranking-history-chart.html が <canvas data-ranking-history="[...]">
// を出力するので、ここで Chart.js を使って描画する。Chart.js 本体は extend_footer
// から CDN defer 読み込み済みである前提。低い順位ほど良いので y 軸は reverse。
(function () {
  "use strict";

  function parsePoints(canvas) {
    var raw = canvas.getAttribute("data-ranking-history") || "[]";
    try {
      var arr = JSON.parse(raw);
      return Array.isArray(arr) ? arr : [];
    } catch (e) {
      return [];
    }
  }

  function render(canvas) {
    if (typeof window.Chart !== "function") return;
    var points = parsePoints(canvas);
    if (points.length < 2) return;

    var labels = points.map(function (p) { return p.d; });
    var ranks = points.map(function (p) { return Number(p.r); });
    var maxRank = Math.max.apply(null, ranks);
    var yMax = Math.min(50, Math.max(10, Math.ceil(maxRank * 1.1)));

    new window.Chart(canvas.getContext("2d"), {
      type: "line",
      data: {
        labels: labels,
        datasets: [{
          label: "楽天ランキング順位",
          data: ranks,
          borderColor: "#e85d5d",
          backgroundColor: "rgba(232, 93, 93, 0.12)",
          tension: 0.25,
          fill: true,
          pointRadius: 3,
          pointHoverRadius: 5,
          borderWidth: 2,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: function (ctx) { return ctx.parsed.y + " 位"; },
            },
          },
        },
        scales: {
          y: {
            reverse: true,
            min: 1,
            max: yMax,
            title: { display: true, text: "順位 (低いほど上位)" },
            ticks: { stepSize: 1, precision: 0 },
          },
          x: {
            title: { display: true, text: "日付" },
          },
        },
      },
    });
  }

  function init() {
    var nodes = document.querySelectorAll(".ranking-history-canvas[data-ranking-history]");
    if (!nodes.length) return;
    if (typeof window.Chart !== "function") {
      // Chart.js がまだ読まれていない場合 (defer の race) は遅延リトライ。
      window.setTimeout(init, 100);
      return;
    }
    nodes.forEach(render);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
