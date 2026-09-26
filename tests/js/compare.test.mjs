// tests/js/compare.test.mjs — hugo/assets/js/compare.js の2つの回帰防止。
//
// A. localStorage.setItem が失敗する環境 (Safari プライベートブラウズ等) では
//    _write() が sessionStorage に書くが、旧 _read() は
//    (localStorage || sessionStorage).getItem(KEY) という常に localStorage 側に
//    評価される式で読んでいたため、sessionStorage に逃がしたデータを二度と
//    読み戻せなかった (見た目には毎回選択状態が消える)。
// B. 共有比較 URL (/compare/?asins=A,B) を開いた閲覧者が「×」で1件削除すると、
//    削除処理が localStorage 側の list()/remove() から残り ASIN を作っていたため、
//    閲覧者自身の (無関係な、あるいは空の) 保存済み比較リストで URL の残り ASIN が
//    丸ごと上書きされ、残るはずの商品が消える/別の商品にすり替わっていた。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import path from "node:path";

const SRC = readFileSync(
  path.resolve(path.dirname(fileURLToPath(import.meta.url)),
               "../../hugo/assets/js/compare.js"), "utf8");

function makeGenericEl() {
  const classes = new Set();
  const el = {
    _html: "",
    classList: {
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
      toggle: (c, v) => { if (v) classes.add(c); else classes.delete(c); },
      contains: (c) => classes.has(c),
    },
    setAttribute() {},
    getAttribute() { return null; },
    appendChild() {},
    remove() {},
    querySelector() { return { addEventListener() {} }; },
    querySelectorAll() { return []; },
  };
  Object.defineProperty(el, "innerHTML", {
    configurable: true,
    get() { return el._html; },
    set(v) { el._html = v; },
  });
  return el;
}

function makeCompareRoot() {
  const buttons = [];
  const root = makeGenericEl();
  root.querySelectorAll = (sel) => (sel === ".compare-remove" ? buttons.slice() : []);
  Object.defineProperty(root, "innerHTML", {
    configurable: true,
    get() { return root._html; },
    set(v) {
      root._html = v;
      buttons.length = 0;
      const re = /class="compare-remove" data-asin="([^"]*)"/g;
      let m;
      while ((m = re.exec(v))) {
        const asin = m[1];
        const btn = { getAttribute: () => asin, addEventListener(type, fn) { btn._click = fn; } };
        buttons.push(btn);
      }
    },
  });
  return { root, buttons };
}

function boot({ initialSearch, storage, index }) {
  const { root, buttons } = makeCompareRoot();
  const location = { pathname: "/compare/", search: initialSearch };
  const history = {
    replaceState(_state, _title, url) {
      const qi = url.indexOf("?");
      location.pathname = qi === -1 ? url : url.slice(0, qi);
      location.search = qi === -1 ? "" : url.slice(qi);
    },
  };

  const localStorageStub = storage.local; // { getItem, setItem } or throwing variants
  const sessionStorageStub = storage.session;

  const sandbox = {
    document: {
      readyState: "complete",
      getElementById: (id) => (id === "compare-root" ? root : null),
      createElement: () => makeGenericEl(),
      body: makeGenericEl(),
      querySelectorAll: () => [], // mountToggles 用 (.product-card 等は無し)
      addEventListener() {},
    },
    window: { location, history },
    location,
    history,
    localStorage: localStorageStub,
    sessionStorage: sessionStorageStub,
    // 起動時の自動 hydrateComparePage() が読む索引を script 実行前に確定させておく
    // (実行後に差し替えると、初回フェッチと競合してどちらの結果が最終的に
    // 描画されるか非決定的になる)。
    __index: index || [],
    fetch: async function () { return { json: async function () { return sandbox.__index; } }; },
    URLSearchParams,
    console,
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(SRC, sandbox);
  return { sandbox, root, buttons: () => buttons, location };
}

function memStorage() {
  const data = new Map();
  return {
    getItem: (k) => (data.has(k) ? data.get(k) : null),
    setItem: (k, v) => data.set(k, v),
    removeItem: (k) => data.delete(k),
  };
}

function throwingStorage() {
  return {
    getItem() { return null; },
    setItem() { throw new Error("QuotaExceededError"); },
  };
}

test("A: localStorage.setItem が失敗しても、ページ再読み込み後に sessionStorage から読み戻せる", () => {
  // メモリ内キャッシュ (memStore) は同一ページ内なら書き込み直後の値を隠して
  // しまい旧バグを再現しないため、ストレージだけ共有した「別ページ読み込み」を
  // 2回 boot() することで memStore が空の状態からの読み込みを検証する。
  const session = memStorage();
  const { sandbox: page1 } = boot({
    initialSearch: "",
    storage: { local: throwingStorage(), session },
  });
  const ok = page1.window.OmochaCompare.add("B0AAAAAAAA");
  assert.equal(ok, true);

  const { sandbox: page2 } = boot({
    initialSearch: "",
    storage: { local: throwingStorage(), session },
  });
  // vm サンドボックス realm の配列なので Array.from で素の配列に直してから比較する
  assert.deepEqual(Array.from(page2.window.OmochaCompare.list()), ["B0AAAAAAAA"], "sessionStorage フォールバックから読み戻せる (回帰防止)");
});

test("B: 共有比較URLで1件削除しても、閲覧者自身の無関係な保存済みリストで上書きされない", async () => {
  const { sandbox, buttons } = boot({
    initialSearch: "?asins=B0AAAAAAAA,B0BBBBBBBB",
    storage: { local: memStorage(), session: memStorage() },
    index: [
      { permalink: "https://example.com/products/b0aaaaaaaa/", product_name: "A" },
      { permalink: "https://example.com/products/b0bbbbbbbb/", product_name: "B" },
      { permalink: "https://example.com/products/b0cccccccc/", product_name: "C" },
    ],
  });
  // 閲覧者自身が別途保存していた、共有URLとは無関係な比較リスト
  sandbox.window.OmochaCompare.add("B0CCCCCCCC");
  // 起動時の自動 hydrateComparePage() (fetch().then().then()) が解決するのを待つ
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));

  assert.equal(buttons().length, 2, "URL の2件 (A, B) が表示されている");

  const btnA = buttons().find((b) => b.getAttribute() === "B0AAAAAAAA");
  assert.ok(btnA, "A の削除ボタンが見つかる");
  btnA._click({ currentTarget: btnA });
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));

  // 期待: 残りは URL 由来の B のみ。旧コードは localStorage 側 (C) で
  // 上書きされ、C が表示されるか B が消えるかしていた。
  const remainingAsins = buttons().map((b) => b.getAttribute());
  assert.deepEqual(remainingAsins, ["B0BBBBBBBB"], "残りは B のみ (C にすり替わらない・回帰防止)");

  // 残った B も削除して0件にする。ここで hydrateComparePage() を呼び直すと
  // ?asins= が空の URL になり、「URL 指定なし → localStorage から復元」の
  // 分岐に入って閲覧者自身の保存済みリスト (C) が勝手に復元される、削除前と
  // 同じ種類の不整合が起きうる境界値 (agy レビュー指摘)。
  const btnB = buttons().find((b) => b.getAttribute() === "B0BBBBBBBB");
  assert.ok(btnB, "B の削除ボタンが見つかる");
  btnB._click({ currentTarget: btnB });
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));

  assert.equal(buttons().length, 0, "0件になったら空表示のまま (C が勝手に復元されない・回帰防止)");
});

test("B-2: 全件削除後にページを再読み込みしても、無関係な保存済みリストへ復元されない (agy レビュー指摘の回帰防止)", async () => {
  const session = memStorage();
  const local = memStorage();
  const { sandbox, buttons, location } = boot({
    initialSearch: "?asins=B0AAAAAAAA",
    storage: { local, session },
    index: [{ permalink: "https://example.com/products/b0aaaaaaaa/", product_name: "A" }],
  });
  // 閲覧者自身の別の保存済みリスト
  sandbox.window.OmochaCompare.add("B0CCCCCCCC");
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));

  const btnA = buttons().find((b) => b.getAttribute() === "B0AAAAAAAA");
  btnA._click({ currentTarget: btnA });
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(buttons().length, 0);
  // ?asins= 自体は残っている (bare pathname に戻していない) はず
  assert.equal(location.search, "?asins=", "0件でも asins パラメータのキー自体は残す");

  // 「ページを再読み込み」= 同じ検索クエリ・同じストレージで boot() をやり直す
  const { buttons: buttons2 } = boot({
    initialSearch: location.search,
    storage: { local, session },
    index: [{ permalink: "https://example.com/products/b0aaaaaaaa/", product_name: "A" }],
  });
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(buttons2().length, 0, "再読み込みしても C が復元されず空のまま (回帰防止)");
});
