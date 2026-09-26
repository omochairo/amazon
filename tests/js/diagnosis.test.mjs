// tests/js/diagnosis.test.mjs — hugo/assets/js/diagnosis.js の回帰防止。
//
// A. /search.json の取得に失敗すると itemsCache が永久に null のままになり、
//    showResults() の setTimeout(showResults, 100) が無限に再帰して「集計中...」
//    のまま固まっていた。取得失敗をフラグで検知してエラー表示 + 手動リトライに
//    切り替えた。
// B. 「もう一度読み込む」ボタンで再取得が成功すれば、通常どおり結果が表示される。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import path from "node:path";

const SRC = readFileSync(
  path.resolve(path.dirname(fileURLToPath(import.meta.url)),
               "../../hugo/assets/js/diagnosis.js"), "utf8");

function makeEl(overrides = {}) {
  const listeners = {};
  const classes = new Set();
  return {
    style: {},
    classList: { add: (c) => classes.add(c), remove: (c) => classes.delete(c), contains: (c) => classes.has(c) },
    addEventListener(type, fn) { listeners[type] = fn; },
    setAttribute() {},
    getAttribute() { return null; },
    textContent: "",
    _listeners: listeners,
    ...overrides,
  };
}

function boot({ fetchImpl, search, omochaCompare, omochaFavorites }) {
  const steps = [1, 2, 3, 4, 5, 6].map((n) => makeEl({ getAttribute: () => String(n) }));
  const prevBtn = makeEl();
  const retryBtn = makeEl();
  const progressBar = makeEl({ parentElement: { style: {} } });
  const progressText = makeEl();
  const wizardContainer = makeEl();

  const resultHeaderH2 = makeEl();
  const resultHeaderP = makeEl();
  const resultHeader = {
    querySelector: (sel) => (sel === "h2" ? resultHeaderH2 : sel === "p" ? resultHeaderP : null),
  };
  const resultGrid = {
    _children: [],
    set innerHTML(v) { this._children = []; this._html = v; },
    get innerHTML() { return this._html || ""; },
    appendChild(node) { this._children.push(node); },
    querySelector() { return null; },
  };
  const resultContainer = makeEl({
    querySelector: (sel) => (sel === ".diagnosis-result-header" ? resultHeader : null),
  });
  const fallbackBadge = makeEl();

  // q1..q5 の5ボタン。押した順に currentStep が進み、5問目で showResults() が走る。
  const optionBtns = ["q1", "q2", "q3", "q4", "q5"].map((name) =>
    makeEl({ getAttribute: (n) => (n === "data-name" ? name : n === "data-value" ? "x" : null) })
  );

  const byId = {
    "diagnosis-prev-btn": prevBtn,
    "diagnosis-retry-btn": retryBtn,
    "diagnosis-progress-bar": progressBar,
    "diagnosis-progress-text": progressText,
    "diagnosis-wizard": wizardContainer,
    "diagnosis-result-container": resultContainer,
    "diagnosis-result-grid": resultGrid,
    "diagnosis-fallback-badge": fallbackBadge,
  };

  const domContentLoadedHandlers = [];
  const location = { href: "https://example.com/diagnosis/" + (search || ""), pathname: "/diagnosis/", search: search || "" };
  const sandbox = {
    document: {
      readyState: "complete",
      addEventListener(type, fn) { if (type === "DOMContentLoaded") domContentLoadedHandlers.push(fn); },
      getElementById: (id) => (id in byId ? byId[id] : null),
      querySelectorAll: (sel) => (sel === ".diagnosis-step" ? steps : sel === ".diagnosis-option-btn" ? optionBtns : []),
      querySelector: () => null, // .diagnosis-wrapper 無し
      createElement: () => makeEl(),
    },
    window: {
      location,
      history: { replaceState() {} },
      OmochaCompare: omochaCompare,
      OmochaFavorites: omochaFavorites,
      OmochaUtils: { renderProductCard: (item) => ({ __item: item }) },
    },
    location,
    URLSearchParams,
    URL,
    localStorage: { getItem: () => null, setItem() {} },
    fetch: fetchImpl,
    setTimeout,
    console,
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(SRC, sandbox);
  for (const fn of domContentLoadedHandlers) fn();

  return { optionBtns, resultContainer, resultGrid, resultHeaderH2, resultHeaderP, retryBtn };
}

function answerAllQuestions(optionBtns) {
  for (const btn of optionBtns) btn._listeners.click();
}

test("A: 通信失敗が続くと、無限リトライせずエラー表示 + 手動リトライボタンを出す", async () => {
  let fetchCalls = 0;
  const { optionBtns, resultGrid, resultHeaderH2 } = boot({
    fetchImpl: async () => { fetchCalls++; throw new Error("network error"); },
  });
  // 初回 loadItems() の fetch 失敗 (catch) が解決するのを待つ
  await new Promise((r) => setTimeout(r, 10));

  answerAllQuestions(optionBtns);

  assert.equal(resultHeaderH2.textContent, "😥 おもちゃ情報の読み込みに失敗しました");
  const retryButton = resultGrid._children.find((c) => c && c.textContent === "🔄 もう一度読み込む");
  assert.ok(retryButton, "手動リトライボタンが表示される (無限ループしない・回帰防止)");
  assert.ok(fetchCalls >= 1);
});

test("B: リトライボタンで再取得に成功すれば結果が表示される", async () => {
  let attempt = 0;
  const { optionBtns, resultGrid, resultHeaderH2 } = boot({
    fetchImpl: async () => {
      attempt++;
      if (attempt === 1) throw new Error("network error");
      return { json: async () => [{ ivs_score_100: 80, price_amazon: 2000 }] };
    },
  });
  await new Promise((r) => setTimeout(r, 10)); // 1回目の失敗が反映されるのを待つ

  answerAllQuestions(optionBtns);
  assert.equal(resultHeaderH2.textContent, "😥 おもちゃ情報の読み込みに失敗しました");

  const retryButton = resultGrid._children.find((c) => c && c.textContent === "🔄 もう一度読み込む");
  retryButton._listeners.click();
  // loadItems() の再取得 (成功) が反映され、showResults() の再帰リトライが
  // itemsCache を検知するまで待つ (100ms ポーリング)。
  await new Promise((r) => setTimeout(r, 250));

  assert.notEqual(resultHeaderH2.textContent, "😥 おもちゃ情報の読み込みに失敗しました", "再取得成功後はエラー表示のままにならない");
});

test("C: 年齢ベスト10から5問診断を完了すると、結果見出しが正しくリセットされる", async () => {
  const { optionBtns, resultHeaderH2, retryBtn } = boot({
    search: "?age=0-1",
    fetchImpl: async () => ({ json: async () => [{ ivs_score_100: 80, age_min_months: 6 }] }),
  });
  // ?age=0-1 による showAgeBest の初回ロード待ち (100ms ポーリング)
  await new Promise((r) => setTimeout(r, 150));
  assert.match(resultHeaderH2.textContent, /ベスト10/, "年齢ベスト10の見出しになっている");

  // 「もう一度診断する」でウィザードに戻り、5問診断を完了する
  retryBtn._listeners.click();
  answerAllQuestions(optionBtns);
  await new Promise((r) => setTimeout(r, 20));

  assert.equal(
    resultHeaderH2.textContent,
    "✨ あなたにおすすめのおもちゃ処方箋 ✨",
    "見出しが「0〜1歳のベスト10」のまま残らずデフォルトに戻る (回帰防止)"
  );
});

test("D: エラー表示中に「もう一度診断する」から最初からやり直しても再取得を試みる (agy レビュー指摘の回帰防止)", async () => {
  let attempt = 0;
  const { optionBtns, resultHeaderH2, retryBtn } = boot({
    fetchImpl: async () => {
      attempt++;
      if (attempt === 1) throw new Error("network error");
      return { json: async () => [{ ivs_score_100: 80, price_amazon: 2000 }] };
    },
  });
  await new Promise((r) => setTimeout(r, 10)); // 初回失敗が反映されるのを待つ

  // 1回目: 5問回答してエラー表示になる (小さいエラー内リトライボタンは使わない)
  answerAllQuestions(optionBtns);
  assert.equal(resultHeaderH2.textContent, "😥 おもちゃ情報の読み込みに失敗しました");

  // 「もう一度診断する」で最初からやり直す
  retryBtn._listeners.click();
  answerAllQuestions(optionBtns);
  // やり直し時に再取得 (今度は成功) が走り、ポーリングで拾われるのを待つ
  await new Promise((r) => setTimeout(r, 250));

  assert.notEqual(
    resultHeaderH2.textContent,
    "😥 おもちゃ情報の読み込みに失敗しました",
    "やり直し後は再取得を試み、成功していればエラー表示のままにならない (回帰防止)"
  );
});

test("E: 診断結果カードにも term_list.js と同様に ♡/🆚 トグルを再マウントする (agy レビュー指摘の回帰防止)", async () => {
  const compareCalls = [];
  const favoritesCalls = [];
  const { optionBtns, resultGrid } = boot({
    fetchImpl: async () => ({ json: async () => [{ ivs_score_100: 80, price_amazon: 2000 }] }),
    omochaCompare: { mountToggles: (root) => compareCalls.push(root) },
    omochaFavorites: { mountToggles: (root) => favoritesCalls.push(root) },
  });
  answerAllQuestions(optionBtns);
  // fetch (2 microtask hop) がまだ解決していないと showResults() は
  // setTimeout(showResults, 100) で一旦リトライに回るため、そのポーリングが
  // itemsCache を拾うまで待つ。
  await new Promise((r) => setTimeout(r, 150));

  assert.ok(resultGrid._children.length > 0, "結果カードが描画されている");
  assert.equal(compareCalls.length, 1);
  assert.equal(compareCalls[0], resultGrid);
  assert.equal(favoritesCalls.length, 1);
  assert.equal(favoritesCalls[0], resultGrid);
});
