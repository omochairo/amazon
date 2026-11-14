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

function makeDropHarness({ rows, fetchFails = false }) {
  const classes = () => {
    const values = new Set();
    return { add: (v) => values.add(v), contains: (v) => values.has(v) };
  };
  const list = {
    children: [], parentNode: null,
    appendChild(node) {
      if (node.parentNode) node.parentNode.children.splice(node.parentNode.children.indexOf(node), 1);
      this.children.push(node); node.parentNode = this; return node;
    },
    querySelectorAll() { return this.children.filter((c) => c.className.includes("favorites-card")); },
  };
  const parent = {
    children: [list],
    insertBefore(node, before) {
      if (node.parentNode) node.parentNode.children.splice(node.parentNode.children.indexOf(node), 1);
      this.children.splice(this.children.indexOf(before), 0, node); node.parentNode = this;
    },
  };
  list.parentNode = parent;
  const cards = [
    { asin: "B0AAAAAAAA", price: 1000 }, { asin: "B0BBBBBBBB", price: 1000 },
    { asin: "B0CCCCCCCC", price: 1000 }, { asin: "B0DDDDDDDD", price: 1000 },
  ].map(({ asin, price }) => {
    const meta = { badges: [], querySelector: (sel) => sel === ".price-drop-badge" ? meta.badges[0] || null : null,
      appendChild(node) { meta.badges.push(node); } };
    return { asin, className: "favorites-card", parentNode: null, meta, classList: classes(),
      getAttribute: (name) => name === "data-asin" ? asin : null,
      querySelector: (sel) => sel === ".favorites-card-meta" ? meta : null };
  });
  cards.forEach((card) => list.appendChild(card));
  const notices = () => parent.children.filter((n) => n.className === "favorites-drop-notice");
  const local = memStorage();
  const asinOrder = ["B0AAAAAAAA", "B0BBBBBBBB", "B0CCCCCCCC", "B0DDDDDDDD"];
  local.setItem("omcha:favorites:v1", JSON.stringify({ asins: asinOrder, snapshots: Object.fromEntries(asinOrder.map((asin) => [asin, { min_price: 100 }])) }));
  const sandbox = {
    document: {
      readyState: "loading", addEventListener() {}, getElementById: (id) => id === "favorites-list" ? list : null,
      querySelector: (sel) => sel === ".favorites-drop-notice" ? notices()[0] || null : null,
      createElement: (tag) => ({ tagName: tag, className: "", parentNode: null, set textContent(v) { this._text = v; },
        remove() { if (this.parentNode) { this.parentNode.children.splice(this.parentNode.children.indexOf(this), 1); this.parentNode = null; } } }),
    },
    window: { location: { pathname: "/favorites/" } }, localStorage: local, sessionStorage: memStorage(),
    fetch: () => fetchFails ? Promise.resolve({ ok: false }) : Promise.resolve({ ok: true, json: () => Promise.resolve(rows) }),
    console,
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  const callDrops = (source) => source.replace("})(typeof window", "_decorateFavoritesDrops();\n_decorateFavoritesDrops();\nglobal.__decorateDrops = _decorateFavoritesDrops;\n})(typeof window");
  vm.runInContext(callDrops(SRC), sandbox);
  return { list, cards, notices, decorate: () => sandbox.window.__decorateDrops(), wait: () => new Promise((resolve) => setTimeout(resolve, 0)), sandbox };
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

test("F16: 値下げ率順に先頭へ移動し、残りの順序と通知件数を保ち、二度呼んでも重複しない", async () => {
  const asins = ["B0AAAAAAAA", "B0BBBBBBBB", "B0CCCCCCCC", "B0DDDDDDDD"];
  const rows = [80, 40, 1000, 1000].map((price, i) => ({ permalink: `/products/${asins[i].toLowerCase()}/`, price_amazon: price }));
  const h = makeDropHarness({ rows });
  await h.wait();
  assert.deepEqual(h.list.children.map((c) => c.asin), ["B0BBBBBBBB", "B0AAAAAAAA", "B0CCCCCCCC", "B0DDDDDDDD"]);
  assert.equal(h.notices().length, 1);
  assert.equal(h.notices()[0].innerHTML, "お気に入りのうち <strong>2件</strong> が、登録したときより安くなっています。");
  await h.wait();
  assert.equal(h.notices().length, 1);
  assert.deepEqual(h.list.children.map((c) => c.asin), ["B0BBBBBBBB", "B0AAAAAAAA", "B0CCCCCCCC", "B0DDDDDDDD"]);
});

test("F16: カタログ fetch 失敗時は順序も通知も変えない", async () => {
  const h = makeDropHarness({ rows: [], fetchFails: true });
  const before = h.list.children.map((c) => c.asin);
  await h.wait();
  assert.deepEqual(h.list.children.map((c) => c.asin), before);
  assert.equal(h.notices().length, 0);
});

test("F16: 値下げした商品を全部外したら、通知も消える", async () => {
  const asins = ["B0AAAAAAAA", "B0BBBBBBBB", "B0CCCCCCCC", "B0DDDDDDDD"];
  const rows = [80, 1000, 1000, 1000].map((price, i) => ({ permalink: `/products/${asins[i].toLowerCase()}/`, price_amazon: price }));
  const h = makeDropHarness({ rows });
  await h.wait();
  assert.equal(h.notices().length, 1);
  const dropped = h.list.children.find((c) => c.asin === "B0AAAAAAAA");
  h.list.children.splice(h.list.children.indexOf(dropped), 1);
  h.decorate();
  await h.wait();
  assert.equal(h.notices().length, 0);
});
