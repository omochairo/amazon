// hugo/assets/js/last_diagnosis.js
// #1365 Layer 1-③ 診断結果の永続化 — ホーム上部「前回の診断」バナー。
// diagnosis.js が localStorage("omcha_last_diagnosis") に保存した回答を読み、
// 人が読める要約 (年齢・伸ばしたい力・場所・予算) を組み立てて再表示する。
// mount (`[data-last-diagnosis]`) はホームにのみ出力されるため、他ページでは no-op。
// 保存が無ければ hidden のまま (空状態は何も出さない)。サーバ不要・全 localStorage。
(function () {
  "use strict";

  var KEY = "omcha_last_diagnosis";

  // diagnosis.html の各 data-value → バナー用の短い日本語ラベル。
  var LABELS = {
    q1: { "0-1": "0〜1歳", "1-2": "1〜2歳", "3-4": "3〜4歳", "5-6": "5〜6歳" },
    q2: {
      dexterity: "手先の器用さ", language: "言語・数",
      imagination: "想像力", social: "社会性"
    },
    q3: { indoor: "室内メイン", outdoor: "外でも使う" },
    q4: { "3000": "〜3,000円", "8000": "〜8,000円", "8000+": "8,000円以上" },
    q5: {
      block: "積み木・ブロック系", puzzle: "パズル・カード系",
      gokko: "ごっこ遊び系", few: "定番から"
    }
  };

  function label(q, val) {
    var map = LABELS[q] || {};
    return map[val] || "";
  }

  // ts (ISO8601) → 「3日前 / きのう / きょう」程度のゆるい相対表記。
  function relativeDays(ts) {
    if (!ts) return "";
    var then = new Date(ts).getTime();
    if (isNaN(then)) return "";
    var days = Math.floor((Date.now() - then) / 86400000);
    if (days <= 0) return "きょう";
    if (days === 1) return "きのう";
    if (days < 7) return days + "日前";
    if (days < 30) return Math.floor(days / 7) + "週間前";
    return Math.floor(days / 30) + "か月前";
  }

  function readSaved() {
    try {
      var raw = localStorage.getItem(KEY);
      return raw ? JSON.parse(raw) : null;
    } catch (e) {
      return null;
    }
  }

  function hydrate() {
    var mount = document.querySelector("[data-last-diagnosis]");
    if (!mount) return; // ホーム以外では mount 自体が無い

    var saved = readSaved();
    if (!saved || !saved.answers || !saved.answers.q1) return; // 空状態

    var a = saved.answers;
    var parts = [
      label("q1", a.q1), label("q2", a.q2),
      label("q3", a.q3), label("q4", a.q4)
    ].filter(Boolean);
    if (!parts.length) return;

    var summaryEl = mount.querySelector(".last-diagnosis-summary");
    if (summaryEl) summaryEl.textContent = parts.join("・");

    var when = relativeDays(saved.ts);
    var metaEl = mount.querySelector(".last-diagnosis-meta");
    if (metaEl) {
      var bits = [];
      if (when) bits.push(when);
      if (saved.count) bits.push("おすすめ " + saved.count + " 件");
      metaEl.textContent = bits.join(" ・ ");
    }

    // top スナップショットがあればサムネ + 商品名を添える (任意)
    var top = saved.top;
    var topEl = mount.querySelector(".last-diagnosis-top");
    if (topEl && top && (top.image || top.title) && top.permalink) {
      var img = top.image
        ? '<img src="' + top.image.replace(/"/g, "&quot;") + '" alt="" loading="lazy">'
        : "";
      var name = (top.title || "").replace(/</g, "&lt;").replace(/>/g, "&gt;");
      topEl.innerHTML =
        '<a class="last-diagnosis-top-link" href="' + top.permalink.replace(/"/g, "&quot;") + '">' +
        img + '<span class="last-diagnosis-top-name">' + name + "</span></a>";
      topEl.hidden = false;
    }

    mount.hidden = false;
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", hydrate);
  } else {
    hydrate();
  }
})();
