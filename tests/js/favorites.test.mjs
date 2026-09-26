// tests/js/favorites.test.mjs — hugo/assets/js/favorites.js の2つの回帰防止。
//
// A. compare.js と同じ (localStorage || sessionStorage) 読み込みバグ。
// B. mountToggles が window.OmochaFavorites から export されておらず、
//    term_list.js のような「絞り込み後にカードを再生成するページ」から
//    再マウントできなかった (並び替え・絞り込みのたびに ♡ ボタンが消える)。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import path from "node:path";

const SRC = readFileSync(
  path.resolve(path.dirname(fileURLToPath(import.meta.url)),
               "../../hugo/assets/js/favorites.js"), "utf8");

function memStorage() {
  const data = new Map();
  return {
    getItem: (k) => (data.has(k) ? data.get(k) : null),
    setItem: (k, v) => data.set(k, v),
  };
}

function throwingStorage() {
  return { getItem() { return null; }, setItem() { throw new Error("QuotaExceededError"); } };
}

function makeButtonEl() {
  const classes = new Set();
  return {
    classList: { add: (c) => classes.add(c), remove: (c) => classes.delete(c), toggle: (c, v) => (v ? classes.add(c) : classes.delete(c)) },
    setAttribute() {},
    addEventListener() {},
    set innerHTML(v) { this._html = v; },
    get innerHTML() { return this._html; },
  };
}

function boot({ storage }) {
  const sandbox = {
    // readyState を "loading" にして boot() (mountToggles(document) 等の
    // フル起動) を走らせず、export された API だけを直接検証する。
    document: {
      readyState: "loading",
      addEventListener() {},
      createElement: () => makeButtonEl(),
    },
    window: {},
    localStorage: storage.local,
    sessionStorage: storage.session,
    console,
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(SRC, sandbox);
  return sandbox;
}

test("A: localStorage.setItem が失敗しても、ページ再読み込み後に sessionStorage から読み戻せる", () => {
  const session = memStorage();
  const page1 = boot({ storage: { local: throwingStorage(), session } });
  assert.equal(page1.window.OmochaFavorites.add("B0AAAAAAAA"), true);

  const page2 = boot({ storage: { local: throwingStorage(), session } });
  assert.deepEqual(Array.from(page2.window.OmochaFavorites.list()), ["B0AAAAAAAA"]);
});

test("B: mountToggles が公開 API として export されている (term_list.js 等からの再マウント用)", () => {
  const sandbox = boot({ storage: { local: memStorage(), session: memStorage() } });
  assert.equal(typeof sandbox.window.OmochaFavorites.mountToggles, "function");
});

test("B-2: mountToggles(root) が指定した要素配下の product-card に ♡ トグルを注入する", () => {
  const sandbox = boot({ storage: { local: memStorage(), session: memStorage() } });
  const inserted = [];
  const card = {
    dataset: { asin: "B0AAAAAAAA" },
    firstChild: null,
    querySelector() { return null; }, // まだ .fav-toggle が無い
    insertBefore() {},
    appendChild: (node) => inserted.push(node),
  };
  const root = {
    querySelectorAll: (sel) => (sel === ".product-card" ? [card] : []),
  };
  sandbox.window.OmochaFavorites.mountToggles(root);
  assert.equal(inserted.length, 1, "新しく生成したカードにも ♡ ボタンが挿入される");
});
