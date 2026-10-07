// hugo/assets/js/last_diagnosis.js
// #1365 Layer 1-③ 診断結果の永続化 — ホーム上部「前回の診断」バナー。
// diagnosis.js が localStorage("omcha_last_diagnosis") に保存した回答を読み、
// 人が読める要約 (だれに・年齢・好きな遊び・予算) を組み立てて再表示する。
// 旧 5 問 (v なし: q1..q5) の保存も読める。
// mount (`[data-last-diagnosis]`) はホームにのみ出力されるため、他ページでは no-op。
// 保存が無ければ hidden のまま (空状態は何も出さない)。サーバ不要・全 localStorage。
(function () {
  "use strict";

  var KEY = "omcha_last_diagnosis";

  // 旧 5 問 (v1) の data-value → バナー用の短い日本語ラベル。
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

  // v2 (2026-10 の作り直し以降) の保存形式。値は diagnosis_core.js の各選択肢と一致させる。
  // ホームでは diagnosis_core.js を読み込まない (重いので) ため、要約に要る分だけ持つ。
  var LABELS_V2 = {
    who: { child: "わが子に", gift: "プレゼント", birth: "出産祝い" },
    age: {
      "0-5m": "0〜5か月", "6-11m": "6〜11か月", "1y": "1歳", "2y": "2歳", "3y": "3歳",
      "4y": "4歳", "5y": "5歳", "6-7y": "6〜7歳", "8y": "8歳以上"
    },
    interest: {
      sense: "さわる・にぎる", build: "つくる・組み立てる", pretend: "ごっこ遊び",
      vehicle: "のりもの", think: "考える・ゲーム", art: "描く・工作",
      words: "ことば・数・英語", music: "音・リズム", science: "しくみ・科学",
      active: "体を動かす", any: "おまかせ"
    },
    budget: { "3000": "〜3,000円", "5000": "〜5,000円", "10000": "〜10,000円", any: "予算こだわらない" }
  };

  function label(q, val) {
    var map = LABELS[q] || {};
    return map[val] || "";
  }

  function labelV2(q, val) {
    var map = LABELS_V2[q] || {};
    return map[val] || "";
  }

  function summaryParts(saved) {
    var a = saved.answers;
    if (saved.v === 2) {
      return [
        labelV2("who", a.who), a.who === "birth" ? "" : labelV2("age", a.age),
        labelV2("interest", a.interest), labelV2("budget", a.budget)
      ].filter(Boolean);
    }
    return [
      label("q1", a.q1), label("q2", a.q2),
      label("q3", a.q3), label("q4", a.q4)
    ].filter(Boolean);
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
    if (!saved || !saved.answers) return; // 空状態
    if (saved.v === 2 ? !saved.answers.who : !saved.answers.q1) return;

    var parts = summaryParts(saved);
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
