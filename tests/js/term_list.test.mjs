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
  return {
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    style: {},
    addEventListener(type, fn) { listeners[type] = fn; },
    getAttribute() { return null; },
    setAttribute() {},
    querySelector() { return null; },
    _listeners: listeners,
    ...overrides,
  };
}

function run({ taxonomy, term, indexData }) {
  const appended = [];
  const grid = makeEl({
    getAttribute: (name) => (name === "data-taxonomy" ? taxonomy : name === "data-term" ? term : null),
    appendChild: (node) => appended.push(node),
    innerHTML: "",
  });
  const sortBtn = makeEl({ getAttribute: (name) => (name === "data-sort" ? "score" : null) });
  const paginationContainer = makeEl();

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
      querySelectorAll: (sel) => (sel === ".sort-btn" ? [sortBtn] : []),
    },
    window: {
      OmochaUtils: {
        renderProductCard: (item) => ({ __item: item }),
      },
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

  return { sortBtn, appended };
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
