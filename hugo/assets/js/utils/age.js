// hugo/assets/js/utils/age.js
// 対象年齢フォーマッタの共通実装。
// 旧: diagnosis.js#formatAge, layouts/posts/list.html#formatAgeMin,
//     layouts/_default/term.html#formatAgeMin (3 箇所重複) → ここに集約。
// Issue #745 Phase 1。
(function (global) {
  function formatAgeMin(months) {
    if (months === undefined || months === null) return "";
    var m = parseInt(months, 10);
    if (isNaN(m)) return "";
    if (m === 0) return "0歳〜";
    if (m < 12) return m + "ヶ月〜";
    var years = Math.floor(m / 12);
    var rem = m % 12;
    if (rem === 0) return years + "歳〜";
    if (rem === 6) return years + ".5歳〜";
    return (m / 12).toFixed(1).replace(".0", "") + "歳〜";
  }

  var ns = global.OmochaUtils || (global.OmochaUtils = {});
  ns.formatAgeMin = formatAgeMin;
  // diagnosis.js 旧 API 互換
  ns.formatAge = formatAgeMin;
})(typeof window !== "undefined" ? window : this);
