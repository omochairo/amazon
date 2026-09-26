// tests/js/term_list.test.mjs — hugo/assets/js/term_list.js のブランド絞り込み。
//
// ブランド term ページ (/brands/<slug>/) で並び替え・年齢フィルターを押すと
// ensureItemsLoaded() が /index.json を取り直しフィルタする。data-term は term
// ページの表示名 (.Title = ブランド正規名) だが、index.json の item.brands は
// /brands/<slug>/ タクソノミー用のローマ字スラッグ配列 (例: "raaningurisooshizu")
// で表示名と絶対に一致しない。旧コードは item.brands.includes(term) で比較して
// いたため、ブランド hub での並び替え・年齢フィルター操作は常に 0 件になっていた。
// このテストは item.brand (表示名と同じ値を持つ単数形フィールド) との比較に
// 直っていることを固定する。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import path from "node:path";

const SRC = readFileSync(
  path.resolve(path.dirname(fileURLToPath(import.meta.url)),
               "../../hugo/assets/js/term_list.js"), "utf8");

function makeEl(overrides = {}) {
  const listeners = {};
  const classes = new Set();
  return {
    classList: {
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
      toggle: (c, v) => { if (v === undefined) { classes.has(c) ? classes.delete(c) : classes.add(c); } else if (v) classes.add(c); else classes.delete(c); },
      contains: (c) => classes.has(c),
    },
    style: {},
    addEventListener(type, fn) { listeners[type] = fn; },
    getAttribute() { return null; },
    setAttribute() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
    _listeners: listeners,
    ...overrides,
  };
}

function makeStage({ min, max, ageValues }) {
  const trigger = makeEl();
  const ageBtnEls = ageValues.map((v) => makeEl({ getAttribute: (n) => (n === "data-age" ? String(v) : null) }));
  const stage = makeEl({
    getAttribute: (name) => (name === "data-min" ? String(min) : name === "data-max" ? String(max) : null),
    querySelector: (sel) => (sel === ".age-filter-stage-trigger" ? trigger : null),
    querySelectorAll: (sel) => (sel === ".age-btn" ? ageBtnEls : []),
  });
  return { stage, trigger, ageBtnEls };
}

function run({ taxonomy, term, indexData, omochaCompare, omochaFavorites, stages: stageConfigs }) {
  const appended = [];
  const grid = makeEl({
    getAttribute: (name) => (name === "data-taxonomy" ? taxonomy : name === "data-term" ? term : null),
    appendChild: (node) => appended.push(node),
    innerHTML: "",
  });
  const sortBtn = makeEl({ getAttribute: (name) => (name === "data-sort" ? "score" : null) });
  const paginationContainer = makeEl();

  const stageObjs = (stageConfigs || []).map(makeStage);
  const allAgeBtnEls = stageObjs.flatMap((s) => s.ageBtnEls);

  const byId = {
    "term-card-grid": grid,
    "term-pagination-container": paginationContainer,
    "age-filter-clear": null,
    "age-toggle-match": null,
    "age-toggle-all": null,
  };

  const domContentLoadedHandlers = [];
  const sandbox = {
    document: {
      readyState: "complete",
      addEventListener(type, fn) {
        if (type === "DOMContentLoaded") domContentLoadedHandlers.push(fn);
      },
      getElementById: (id) => (id in byId ? byId[id] : null),
      querySelectorAll: (sel) => {
        if (sel === ".sort-btn") return [sortBtn];
        if (sel === ".age-filter-stage") return stageObjs.map((s) => s.stage);
        if (sel === ".age-btn") return allAgeBtnEls;
        return [];
      },
    },
    window: {
      OmochaUtils: {
        renderProductCard: (item) => ({ __item: item }),
      },
      OmochaCompare: omochaCompare,
      OmochaFavorites: omochaFavorites,
    },
    fetch: async () => ({ json: async () => indexData }),
    console,
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(SRC, sandbox);

  // term_list.js は defer 相当 (DOMContentLoaded 前に評価される) 前提なので、
  // ハンドラ登録後に発火させる。
  for (const fn of domContentLoadedHandlers) fn();

  return { sortBtn, appended, grid, stages: stageObjs };
}

test("brand term: item.brand (表示名) が一致すれば拾う", async () => {
  const { sortBtn, appended } = run({
    taxonomy: "brands",
    term: "ラーニングリソーシズ",
    indexData: [
      { brand: "ラーニングリソーシズ", brands: ["raaningurisooshizu"], ivs_score_100: 80 },
      { brand: "レゴ", brands: ["rego"], ivs_score_100: 90 },
    ],
  });
  await sortBtn._listeners.click.call(sortBtn);
  assert.equal(appended.length, 1);
  assert.equal(appended[0].__item.brand, "ラーニングリソーシズ");
});

test("brand term: スラッグ配列 (item.brands) との一致では拾わない (回帰防止)", async () => {
  const { sortBtn, appended } = run({
    taxonomy: "brands",
    term: "raaningurisooshizu", // もし term がスラッグ側だったとしても
    indexData: [
      { brand: "ラーニングリソーシズ", brands: ["raaningurisooshizu"], ivs_score_100: 80 },
    ],
  });
  await sortBtn._listeners.click.call(sortBtn);
  // data-term は常に表示名なので、スラッグ一致は起こらない = 0 件が正しい
  assert.equal(appended.length, 0);
});

test("tags term: 従来どおり item.tags (表示名配列) と一致すれば拾う", async () => {
  const { sortBtn, appended } = run({
    taxonomy: "tags",
    term: "知育玩具",
    indexData: [
      { tags: ["知育玩具", "木製"], ivs_score_100: 70 },
      { tags: ["電子玩具"], ivs_score_100: 60 },
    ],
  });
  await sortBtn._listeners.click.call(sortBtn);
  assert.equal(appended.length, 1);
});

test("並び替え後の再描画で compare.js / favorites.js の mountToggles を再呼び出しする (回帰防止)", async () => {
  // renderProductCard は素のカードしか作らず、♡/🆚 トグルは compare.js /
  // favorites.js が DOMContentLoaded 時に静的DOMへ1回だけ inject する。
  // term_list.js が並び替え・絞り込みのたびに grid.innerHTML を作り直すのに
  // 再呼び出ししていなかったため、操作するたびにカードからトグルが消えていた。
  const compareCalls = [];
  const favoritesCalls = [];
  const { sortBtn, grid } = run({
    taxonomy: "tags",
    term: "知育玩具",
    indexData: [{ tags: ["知育玩具"], ivs_score_100: 70 }],
    omochaCompare: { mountToggles: (root) => compareCalls.push(root) },
    omochaFavorites: { mountToggles: (root) => favoritesCalls.push(root) },
  });
  await sortBtn._listeners.click.call(sortBtn);
  assert.equal(compareCalls.length, 1, "OmochaCompare.mountToggles が呼ばれる");
  assert.equal(compareCalls[0], grid, "対象は再描画した grid 自身");
  assert.equal(favoritesCalls.length, 1, "OmochaFavorites.mountToggles が呼ばれる");
  assert.equal(favoritesCalls[0], grid);
});

test("アコーディオンを閉じると、中の個別年齢ボタンで選んだ絞り込みも解除される (回帰防止)", async () => {
  // ステージを開く → 中の個別年齢ボタン (data-age=10) を選ぶ → ステージを
  // 閉じる、という操作をすると、旧コードは activeAgeFilter が数値 (10) の
  // ままステージの type==="stage" チェックに一致せず解除されなかった
  // (見た目は閉じているのに絞り込みだけ残る不一致)。
  const { appended, stages } = run({
    taxonomy: "tags",
    term: "知育玩具",
    indexData: [
      { tags: ["知育玩具"], age_min_months: 10, ivs_score_100: 80 },  // 個別年齢10に一致
      { tags: ["知育玩具"], age_min_months: 100, ivs_score_100: 90 }, // 一致しない
    ],
    stages: [{ min: 6, max: 10, ageValues: [10] }],
  });
  const { stage, trigger, ageBtnEls } = stages[0];

  // 1. ステージを開く
  await trigger._listeners.click.call(trigger);
  // 2. 中の個別年齢ボタン (10) を選ぶ
  appended.length = 0;
  const ageBtn10 = ageBtnEls[0];
  await ageBtn10._listeners.click.call(ageBtn10, { stopPropagation() {} });
  assert.equal(appended.length, 1, "個別年齢絞り込みが効いて1件だけ表示される");

  // 3. ステージを閉じる (isOpen のトリガーを再クリック)
  appended.length = 0;
  await trigger._listeners.click.call(trigger);
  assert.equal(appended.length, 2, "閉じたら絞り込みが解除されて2件とも表示される (回帰防止)");
});

test("OmochaCompare / OmochaFavorites が無い (未ロード) ページでも並び替えは落ちない", async () => {
  const { sortBtn, appended } = run({
    taxonomy: "tags",
    term: "知育玩具",
    indexData: [{ tags: ["知育玩具"], ivs_score_100: 70 }],
    omochaCompare: undefined,
    omochaFavorites: undefined,
  });
  await assert.doesNotReject(sortBtn._listeners.click.call(sortBtn));
  assert.equal(appended.length, 1);
});
