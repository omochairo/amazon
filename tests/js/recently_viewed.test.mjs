// tests/js/recently_viewed.test.mjs — hugo/assets/js/recently_viewed.js の2つの回帰防止。
//
// A. compare.js / favorites.js と同じ (localStorage || sessionStorage) 読み込みバグ。
// B. 商品ページ経由で自動記録される「最近見た商品」の min_price が常に null の
//    ままで、最安値ラベルが一切表示されなかった問題。#mobile-sticky-cta の
//    data-min-price から拾うように直した。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import path from "node:path";

const SRC = readFileSync(
  path.resolve(path.dirname(fileURLToPath(import.meta.url)),
               "../../hugo/assets/js/recently_viewed.js"), "utf8");

function memStorage() {
  const data = new Map();
  return { getItem: (k) => (data.has(k) ? data.get(k) : null), setItem: (k, v) => data.set(k, v) };
}
function throwingStorage() {
  return { getItem() { return null; }, setItem() { throw new Error("QuotaExceededError"); } };
}

function bootIdle(storage) {
  // readyState を "loading" にして boot() (自動 push / render) を走らせず
  // push/list を直接呼んで検証する (ストレージ fallback のテスト用)。
  const sandbox = {
    document: { readyState: "loading", addEventListener() {} },
    window: { location: { pathname: "/" } },
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
  const page1 = bootIdle({ local: throwingStorage(), session });
  assert.equal(page1.window.OmochaRecent.push({ asin: "B0AAAAAAAA" }), true);

  const page2 = bootIdle({ local: throwingStorage(), session });
  const list = Array.from(page2.window.OmochaRecent.list());
  assert.equal(list.length, 1);
  assert.equal(list[0].asin, "B0AAAAAAAA");
});

test("B: 商品ページ訪問時、#mobile-sticky-cta の data-min-price から最安値を拾って記録する", () => {
  const stickyBar = { getAttribute: (n) => (n === "data-min-price" ? "2980" : null) };
  const sandbox = {
    document: {
      readyState: "complete",
      title: "テスト商品ページ",
      addEventListener() {},
      querySelector: () => null, // .post-title / img / [data-ivs100] は無し
      querySelectorAll: () => [], // .recently-viewed-mount 無し (render は no-op)
      getElementById: (id) => (id === "mobile-sticky-cta" ? stickyBar : null),
    },
    window: {
      location: { pathname: "/products/b0aaaaaaaa/" },
      dispatchEvent() {},
    },
    Event: class { constructor(type) { this.type = type; } },
    localStorage: memStorage(),
    sessionStorage: memStorage(),
    console,
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(SRC, sandbox);

  const list = Array.from(sandbox.window.OmochaRecent.list());
  assert.equal(list.length, 1);
  assert.equal(list[0].asin, "B0AAAAAAAA");
  assert.equal(list[0].min_price, 2980, "data-min-price から最安値が入る (回帰防止)");
});
