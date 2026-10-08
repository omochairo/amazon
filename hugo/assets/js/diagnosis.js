// hugo/assets/js/diagnosis.js
// おもちゃ診断の画面。設問・推薦・文言は diagnosis_core.js (window.OmochaDiagnosis) にあり、
// ここは表示の切り替えと操作だけを受け持つ。
//
// DOM の扱いは getElementById / createElement / appendChild / textContent / setAttribute /
// classList / hidden に限っている (tests/js/diagnosis.test.mjs の簡易 DOM で動かすため)。
//
// 入口は 4 つ:
//   /diagnosis/                 … 1 問目から
//   /diagnosis/?w=..&a=..       … 共有リンク。回答を復元して結果を出す
//   /diagnosis/?restore=1       … ホームの「前回の診断」バナーから。保存済みの回答で再計算
//   /diagnosis/?age=0-1 等      … ホームの年齢タイムライン (age_timeline.html) から。年齢別ベスト10
(function () {
  "use strict";

  var STORAGE_KEY = "omcha_last_diagnosis";
  var FETCH_TIMEOUT_MS = 15000; // fetch が resolve も reject もしないままハングしたときの上限
  var FIRST_SHOWN = 6;
  var MORE_STEP = 6;
  var MAX_PICKS = 12;

  function init() {
    var D = window.OmochaDiagnosis;
    if (!D) return;
    var $ = function (id) { return document.getElementById(id); };
    var els = {
      root: $("dx-root"), quiz: $("dx-quiz"), back: $("dx-back"), progressBar: $("dx-progress-bar"),
      progressText: $("dx-progress-text"), remain: $("dx-remain"),
      qTitle: $("dx-q-title"), qHint: $("dx-q-hint"), options: $("dx-options"), multiNext: $("dx-multi-next"),
      loading: $("dx-loading"), result: $("dx-result"),
      eyebrow: $("dx-result-eyebrow"), pEmoji: $("dx-persona-emoji"), pTitle: $("dx-persona-title"),
      pText: $("dx-persona-text"), ageTip: $("dx-age-tip"), chips: $("dx-chips"), chipsLabel: $("dx-chips-label"), note: $("dx-note"),
      error: $("dx-error"), errorRetry: $("dx-error-retry"),
      top: $("dx-top"), gridTitle: $("dx-grid-title"), grid: $("dx-grid"), more: $("dx-more"),
      share: $("dx-share"), shareMsg: $("dx-share-msg"), retry: $("dx-retry")
    };
    if (!els.quiz || !els.options || !els.result) return;

    var reduceMotion = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
    var PICK_DELAY = reduceMotion ? 0 : 160;   // 選んだボタンが弾む演出を見せてから次へ
    var LOADING_MIN = reduceMotion ? 0 : 650;  // 「診断中」演出の最短表示時間

    var answers = D.emptyAnswers();
    var stepIndex = 0;
    var editing = false;   // 結果画面のチップから 1 問だけ直しに来たとき
    var mode = "quiz";     // quiz | loading | result
    var token = 0;         // 古い非同期処理 (通信待ち・演出待ち) が後から画面を書き換えないための世代番号
    var picks = [];
    var shown = FIRST_SHOWN;
    var lastView = "diagnosis"; // diagnosis | age_best
    var entry = "quiz";    // 計測用: 結果に来た経路 (quiz | shared | restore | edit)
    var started = false;

    // GA4 計測。回答の選択肢 (年齢帯・予算帯など) だけを送り、個人を特定する値は送らない。
    // 計測の失敗で操作を妨げない。
    function track(name, params) {
      if (typeof window.gtag !== "function") return;
      try { window.gtag("event", name, params || {}); } catch (e) { /* 無視 */ }
    }

    // ------------------------------------------------------------ データ取得
    var items = null;
    var itemsPromise = null;

    function loadItems() {
      if (items) return Promise.resolve(items);
      if (itemsPromise) return itemsPromise;
      var p = new Promise(function (resolve, reject) {
        var timer = setTimeout(function () { reject(new Error("timeout")); }, FETCH_TIMEOUT_MS);
        fetch("/search.json")
          .then(function (res) {
            if (res.ok === false) throw new Error("HTTP " + res.status);
            return res.json();
          })
          .then(function (data) {
            clearTimeout(timer);
            // 配列以外 (エラーページの JSON 等) を空のカタログとして握ると、0 件のまま再試行できなくなる
            if (!Array.isArray(data)) throw new Error("unexpected search.json");
            resolve(data);
          })
          .catch(function (err) { clearTimeout(timer); reject(err); });
      });
      itemsPromise = p.then(function (data) { items = data; return data; }, function (err) {
        // 失敗した promise を握ったままにすると、再試行しても永久に失敗し続ける
        itemsPromise = null;
        if (window.console) console.error("Failed to load search.json", err);
        throw err;
      });
      return itemsPromise;
    }
    loadItems().catch(function () {}); // 先読み。失敗は結果を出すときに改めて扱う

    function wait(ms) {
      return new Promise(function (r) { setTimeout(r, ms); });
    }

    // ------------------------------------------------------------ 画面切り替え
    function show(next) {
      mode = next;
      els.quiz.hidden = next !== "quiz";
      els.loading.hidden = next !== "loading";
      els.result.hidden = next !== "result";
      // 結果は main 幅いっぱいに広げる (diagnosis.css の .dx--wide)
      if (els.root) {
        if (next === "result") els.root.classList.add("dx--wide");
        else els.root.classList.remove("dx--wide");
      }
    }

    function clear(el) {
      if (el) el.innerHTML = "";
    }

    function make(tag, className, text) {
      var el = document.createElement(tag);
      if (className) el.className = className;
      if (text != null) el.textContent = text;
      return el;
    }

    // スマホでは結果やローディングが画面外 (下) に出ることがあるので先頭へ寄せる
    function scrollToTop(el) {
      if (el && el.scrollIntoView && window.innerWidth != null && window.innerWidth < 768) {
        el.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
      }
    }

    // ------------------------------------------------------------ 設問
    function currentSteps() {
      return D.stepsFor(answers);
    }

    // 前の回答を変えたことで、後ろの回答が選べない値になったら捨てる
    // (例: 4 歳で「しくみ・科学」を選んだあと、年齢を 1 歳に直した)。
    function dropInvalidAnswers() {
      var steps = currentSteps();
      ["age", "interest", "budget", "priority"].forEach(function (id) {
        if (answers[id] == null) return;
        if (steps.indexOf(id) === -1) { answers[id] = null; return; }
        var q = D.question(id, answers);
        if (!q || !D.find(q.options, answers[id])) answers[id] = null;
      });
      if (answers.who !== "child") {
        answers.owned = [];
      } else {
        var oq = D.question("owned", answers);
        answers.owned = answers.owned.filter(function (v) { return !!D.find(oq.options, v); });
      }
    }

    function renderStep() {
      var steps = currentSteps();
      if (stepIndex >= steps.length) stepIndex = steps.length - 1;
      var id = steps[stepIndex];
      var q = D.question(id, answers);
      // 「だれに」が未回答のうちは最長 (わが子) の問数を出す。あとから増えるより減る方が気持ちいい
      var total = answers.who ? steps.length : D.stepsFor({ who: "child" }).length;

      els.qTitle.textContent = q.title;
      els.qHint.textContent = q.hint || "";
      els.progressText.textContent = "Q" + (stepIndex + 1) + " / " + total;
      var left = total - stepIndex - 1;
      els.remain.textContent = left === 0 ? "ラスト！" : "あと " + left + " 問";
      els.progressBar.style.width = Math.round((stepIndex / total) * 100) + "%";
      els.progressBar.setAttribute("aria-valuenow", String(stepIndex + 1));
      els.progressBar.setAttribute("aria-valuemax", String(total));
      els.progressBar.setAttribute("aria-valuetext", "質問 " + (stepIndex + 1) + " / " + total);
      // 先頭では「結果に戻る」だけ出す。回答が欠けていて戻れないときは出さない
      els.back.hidden = stepIndex === 0 && !(editing && D.isComplete(answers));
      els.back.textContent = stepIndex === 0 ? "← 結果に戻る" : "← 戻る";

      clear(els.options);
      els.options.setAttribute("role", q.multi ? "group" : "radiogroup");
      els.options.className = "dx-options" + (q.options.length > 6 ? " dx-options--many" : "");
      q.options.forEach(function (opt, i) {
        var btn = make("button", "dx-option");
        btn.type = "button";
        btn.setAttribute("role", q.multi ? "checkbox" : "radio");
        var selected = q.multi ? answers.owned.indexOf(opt.value) !== -1 : answers[id] === opt.value;
        btn.setAttribute("aria-checked", selected ? "true" : "false");
        btn.setAttribute("data-value", opt.value);
        if (i < 9) btn.setAttribute("data-key", String(i + 1));
        btn.appendChild(make("span", "dx-option-emoji", opt.emoji)).setAttribute("aria-hidden", "true");
        btn.appendChild(make("span", "dx-option-label", opt.label));
        if (opt.sub) btn.appendChild(make("span", "dx-option-sub", opt.sub));
        btn.addEventListener("click", function () { onPick(id, opt.value, btn, q.multi); });
        els.options.appendChild(btn);
      });

      els.multiNext.hidden = !q.multi;
      if (q.multi) updateMultiLabel();

      if (els.qTitle.focus) els.qTitle.focus();
    }

    function updateMultiLabel() {
      els.multiNext.textContent = answers.owned.length ? "この内容で診断する →" : "とくにない・わからない → 診断する";
    }

    function onPick(id, value, btn, multi) {
      if (mode !== "quiz") return;
      if (multi) {
        var i = answers.owned.indexOf(value);
        if (i === -1) answers.owned.push(value); else answers.owned.splice(i, 1);
        btn.setAttribute("aria-checked", i === -1 ? "true" : "false");
        updateMultiLabel();
        return;
      }
      // 「だれに」を変えると聞くべき設問そのものが変わる (プレゼント → わが子なら
      // 「持っているもの」が増える) ので、結果へ直行せず残りを順に聞く
      if (id === "who" && answers.who !== value) editing = false;
      if (!started && id === "who") { started = true; track("diagnosis_start", { who: value }); }
      answers[id] = value;
      dropInvalidAnswers();
      var siblings = els.options.children || [];
      for (var k = 0; k < siblings.length; k++) siblings[k].setAttribute("aria-checked", "false");
      btn.setAttribute("aria-checked", "true");
      btn.classList.add("is-picked");
      var myToken = ++token;
      setTimeout(function () {
        if (myToken !== token || mode !== "quiz") return;
        advance();
      }, PICK_DELAY);
    }

    function advance() {
      // 結果画面のチップから直しに来た場合は、揃った時点で結果へ戻す
      if (editing && D.isComplete(answers)) { finish(); return; }
      var steps = currentSteps();
      var next = stepIndex + 1;
      if (next >= steps.length) { finish(); return; }
      stepIndex = next;
      renderStep();
    }

    els.back.addEventListener("click", function () {
      if (mode !== "quiz") return;
      token++;
      if (stepIndex === 0) {
        if (editing && D.isComplete(answers)) finish();
        return;
      }
      stepIndex--;
      renderStep();
    });

    els.multiNext.addEventListener("click", function () {
      if (mode !== "quiz") return;
      finish();
    });

    // PC では数字キーで選べる (1〜9)。Backspace で 1 問戻る。
    document.addEventListener("keydown", function (e) {
      if (mode !== "quiz" || e.ctrlKey || e.metaKey || e.altKey) return;
      var t = e.target && e.target.tagName;
      if (t === "INPUT" || t === "TEXTAREA" || t === "SELECT") return;
      if (/^[1-9]$/.test(e.key)) {
        var btn = els.options.children && els.options.children[Number(e.key) - 1];
        if (btn && btn.click) { e.preventDefault(); btn.click(); }
      } else if (e.key === "Backspace" && !els.back.hidden) {
        e.preventDefault();
        els.back.click();
      }
    });

    function startQuiz(keepAnswers) {
      token++;
      if (!keepAnswers) answers = D.emptyAnswers();
      editing = false;
      stepIndex = 0;
      show("quiz");
      renderStep();
    }

    function editStep(stepId) {
      token++;
      editing = true;
      entry = "edit";
      track("diagnosis_edit", { step: stepId });
      stepIndex = Math.max(0, currentSteps().indexOf(stepId));
      show("quiz");
      renderStep();
      scrollToTop(els.quiz);
    }

    // ------------------------------------------------------------ 結果
    function finish() {
      if (!D.isComplete(answers)) { startQuiz(true); return; }
      editing = false;
      var myToken = ++token;
      show("loading");
      scrollToTop(els.loading);
      Promise.all([loadItems(), wait(LOADING_MIN)]).then(function (res) {
        if (myToken !== token) return;
        renderDiagnosis(res[0]);
      }, function () {
        if (myToken !== token) return;
        showError(finish);
      });
    }

    function resetResultArea() {
      els.error.hidden = true;
      els.note.hidden = true;
      clear(els.top);
      clear(els.grid);
      clear(els.chips);
      if (els.chipsLabel) els.chipsLabel.hidden = true;
      els.more.hidden = true;
      els.gridTitle.hidden = true;
      els.shareMsg.textContent = "";
    }

    var retryAction = null;
    function showError(retryFn) {
      show("result");
      resetResultArea();
      els.eyebrow.textContent = "";
      els.pEmoji.textContent = "😥";
      els.pTitle.textContent = "おもちゃ情報の読み込みに失敗しました";
      els.pText.textContent = "通信状況をご確認のうえ、もう一度お試しください。";
      els.ageTip.textContent = "";
      els.share.hidden = true;
      els.error.hidden = false;
      els.errorRetry.disabled = false;
      els.errorRetry.textContent = "🔄 もう一度読み込む";
      retryAction = retryFn;
    }

    els.errorRetry.addEventListener("click", function () {
      if (!retryAction) return;
      // 低速回線で連打されると再取得が多重に走るため、結果が出るまで押せなくする
      els.errorRetry.disabled = true;
      els.errorRetry.textContent = "読み込み中...";
      retryAction();
    });

    function rankLabel(rank) {
      if (rank === 1) return "🥇 いちばんのおすすめ";
      if (rank === 2) return "🥈 2位";
      if (rank === 3) return "🥉 3位";
      return rank + "位";
    }

    function buildPick(pick, rank) {
      var wrap = make("div", "dx-pick" + (rank === 1 ? " dx-pick--top" : ""));
      wrap.appendChild(make("span", "dx-rank", rankLabel(rank)));
      var render = window.OmochaUtils && window.OmochaUtils.renderProductCard;
      if (render) {
        var card = render(pick.item);
        // どの順位のおすすめが開かれているか (理由つき 1 位が効いているか) を見る
        if (card && card.addEventListener) {
          card.addEventListener("click", function () {
            track("diagnosis_pick_click", { rank: rank, view: lastView });
          });
        }
        wrap.appendChild(card);
      }
      if (pick.reasons && pick.reasons.length) {
        var ul = make("ul", "dx-reasons");
        ul.setAttribute("aria-label", "おすすめの理由");
        pick.reasons.forEach(function (r) {
          ul.appendChild(make("li", "dx-reason dx-reason--" + r.kind, r.text));
        });
        wrap.appendChild(ul);
      }
      return wrap;
    }

    function mountToggles(root) {
      // renderProductCard は素のカードしか作らない。compare.js / favorites.js は
      // 読み込み時の静的 DOM にしかトグルを付けないため、描画のたびに付け直す。
      if (window.OmochaCompare && window.OmochaCompare.mountToggles) window.OmochaCompare.mountToggles(root);
      if (window.OmochaFavorites && window.OmochaFavorites.mountToggles) window.OmochaFavorites.mountToggles(root);
    }

    function renderPicks() {
      clear(els.top);
      clear(els.grid);
      if (!picks.length) return;
      els.top.appendChild(buildPick(picks[0], 1));
      var end = Math.min(shown, picks.length);
      for (var i = 1; i < end; i++) els.grid.appendChild(buildPick(picks[i], i + 1));
      els.gridTitle.hidden = end <= 1;
      els.more.hidden = picks.length <= shown;
      mountToggles(els.top);
      mountToggles(els.grid);
    }

    // 「もっと見る」は描画済みのカードを作り直さずに末尾へ足す (フォーカスとスクロール位置を保つ)
    els.more.addEventListener("click", function () {
      var from = Math.min(shown, picks.length);
      shown += MORE_STEP;
      var end = Math.min(shown, picks.length);
      var first = null;
      for (var i = from; i < end; i++) {
        var node = buildPick(picks[i], i + 1);
        if (!first) first = node;
        els.grid.appendChild(node);
      }
      els.more.hidden = picks.length <= shown;
      mountToggles(els.grid);
      var link = first && first.children && first.children[1];
      if (link && link.focus) link.focus();
    });

    function renderDiagnosis(data) {
      var r = D.recommend(data, answers, MAX_PICKS);
      picks = r.picks;
      shown = FIRST_SHOWN;
      show("result");
      resetResultArea();

      var p = D.persona(answers);
      els.eyebrow.textContent = "診断結果";
      els.pEmoji.textContent = p.emoji;
      els.pTitle.textContent = p.title;
      els.pText.textContent = p.text;
      var tip = D.ageTip(answers);
      els.ageTip.textContent = tip ? "この時期のポイント: " + tip : "";
      els.share.hidden = false;
      els.retry.textContent = "🔄 最初から診断する";

      D.summaryChips(answers).forEach(function (c) {
        var chip = make("button", "dx-chip", c.text);
        chip.type = "button";
        chip.setAttribute("aria-label", c.text + " (変更する)");
        chip.addEventListener("click", function () { editStep(c.step); });
        els.chips.appendChild(chip);
      });
      if (els.chipsLabel) els.chipsLabel.hidden = false;

      if (r.relaxed && D.RELAX_NOTES[r.relaxed]) {
        els.note.textContent = "💡 " + D.RELAX_NOTES[r.relaxed];
        els.note.hidden = false;
      }
      lastView = "diagnosis";
      renderPicks();
      writeUrl(D.toQuery(answers));
      save();
      track("diagnosis_complete", {
        entry: entry,
        who: answers.who,
        age: answers.age || "",
        interest: answers.interest,
        budget: answers.budget,
        priority: answers.priority,
        owned_count: (answers.owned || []).length,
        relaxed: r.relaxed || "none",
        result_count: picks.length
      });
      entry = "quiz";
      if (els.pTitle.focus) els.pTitle.focus();
      scrollToTop(els.result);
    }

    function save() {
      try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(D.toSaved(answers, picks)));
      } catch (e) {
        // localStorage が無効/満杯の環境では保存を諦める (診断自体は成立する)
      }
    }

    // 結果の URL を共有リンクと同じ形にしておく (リロードしても同じ結果に戻れる)。
    function writeUrl(query) {
      if (!window.history || !window.history.replaceState) return;
      try {
        var path = window.location.pathname;
        window.history.replaceState(null, "", query ? path + "?" + query : path);
      } catch (e) { /* file:// など replaceState できない環境 */ }
    }

    function shareUrl() {
      var loc = window.location;
      var origin = loc.origin || (loc.protocol + "//" + loc.host);
      var q = D.toQuery(answers);
      return origin + loc.pathname + (q ? "?" + q : "");
    }

    els.share.addEventListener("click", function () {
      var url = shareUrl();
      var p = D.persona(answers);
      var text = "おもちゃ診断の結果は「" + p.title + "」でした！";
      var nav = window.navigator || {};
      if (nav.share) {
        track("diagnosis_share", { method: "web_share" });
        nav.share({ title: document.title, text: text, url: url }).catch(function () {});
        return;
      }
      if (nav.clipboard && nav.clipboard.writeText) {
        track("diagnosis_share", { method: "clipboard" });
        nav.clipboard.writeText(url).then(function () {
          els.shareMsg.textContent = "✅ 結果のリンクをコピーしました。家族やパートナーに送ってみてください。";
        }, function () {
          els.shareMsg.textContent = url;
        });
        return;
      }
      els.shareMsg.textContent = url;
    });

    els.retry.addEventListener("click", function () {
      writeUrl("");
      startQuiz(false);
      scrollToTop(els.quiz);
    });

    // ------------------------------------------------------------ 年齢別ベスト10
    function showAgeBest(band) {
      var b = D.LEGACY_AGE_BANDS[band];
      var myToken = ++token;
      show("loading");
      loadItems().then(function (data) {
        if (myToken !== token) return;
        picks = D.ageBest(data, band, 10);
        shown = 10;
        show("result");
        resetResultArea();
        els.eyebrow.textContent = "年齢別ベスト10";
        els.pEmoji.textContent = b.emoji;
        els.pTitle.textContent = b.label + "のベスト10";
        els.pText.textContent = b.label + "のお子さんに、知育スコアの高い定番おもちゃを集めました。好きな遊びや予算で絞り込むなら、下のボタンから診断をどうぞ。";
        var tip = D.ageTip({ age: b.age });
        els.ageTip.textContent = tip ? "この時期のポイント: " + tip : "";
        els.share.hidden = true;
        els.retry.textContent = "🧸 好きな遊び・予算で絞り込む";
        lastView = "age_best";
        renderPicks();
        track("diagnosis_age_best", { band: band });
      }, function () {
        if (myToken !== token) return;
        showError(function () { showAgeBest(band); });
      });
    }

    // ------------------------------------------------------------ 入口
    function route() {
      var params = new URLSearchParams(window.location.search);
      var band = params.get("age");
      if (band && D.LEGACY_AGE_BANDS[band]) { showAgeBest(band); return; }

      var shared = D.fromParams(params);
      if (shared) { answers = shared; entry = "shared"; finish(); return; }

      if (params.get("restore") === "1") {
        var saved = null;
        try { saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null"); } catch (e) { saved = null; }
        var restored = D.fromSaved(saved);
        if (restored) { answers = restored; entry = "restore"; finish(); return; }
      }
      startQuiz(false);
    }

    route();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
