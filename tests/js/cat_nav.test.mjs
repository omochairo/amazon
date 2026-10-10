// tests/js/cat_nav.test.mjs — hugo/assets/js/cat_nav.js
// (ヘッダー下の「おもちゃの種類から探す」ナビ)。
//
// 固定する動作:
//   - 親ボタンは 1 つずつ開く (アコーディオン)。子が 1 個のグループは <a> の
//     直接リンクで出るので、開閉の対象にしない (aria-expanded を付けない)
//   - Esc で閉じ、フォーカスがナビの中にあったときだけ開いていたボタンへ戻す
//   - キーボードで開いたときだけ、パネルの先頭リンクへフォーカスを移す
//   - 現在地 (URL が一致するリンクと、その親ボタン) に印を付ける
//   - 横スクロールの続きがある側にだけ has-more-left / has-more-right を付ける
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import path from "node:path";

const SRC = readFileSync(
  path.resolve(path.dirname(fileURLToPath(import.meta.url)),
               "../../hugo/assets/js/cat_nav.js"), "utf8");

function makeEl(doc, attrs = {}, extra = {}) {
  const listeners = {};
  const classes = new Set();
  const a = { ...attrs };
  return {
    inNav: true,
    classList: {
      add: (c) => classes.add(c),
      toggle: (c, v) => { if (v) classes.add(c); else classes.delete(c); },
      contains: (c) => classes.has(c),
    },
    addEventListener(type, fn) { listeners[type] = fn; },
    getAttribute: (n) => (n in a ? a[n] : null),
    setAttribute: (n, v) => { a[n] = String(v); },
    hasAttribute: (n) => n in a,
    focus() { doc.activeElement = this; },
    fire(type, ev = {}) { listeners[type](ev); },
    ...extra,
  };
}

// groups: [{ id, links: [pathname, ...] }]。links が 1 個のグループは直接リンク。
function setup({ pathname = "/", groups, scrollWidth = 300, clientWidth = 300 }) {
  const docListeners = {};
  const winListeners = {};
  const doc = {
    activeElement: null,
    addEventListener(type, fn) { docListeners[type] = fn; },
  };
  const panels = {};
  const btns = [];
  const direct = [];
  const links = [];
  for (const g of groups) {
    if (g.links.length === 1) {
      const a = makeEl(doc, { href: g.links[0] }, { pathname: g.links[0], closest: () => null });
      direct.push(a);
      links.push(a);
      continue;
    }
    const panel = { id: "cat-panel-" + g.id, hidden: true, links: [] };
    for (const p of g.links) {
      const a = makeEl(doc, { href: p }, { pathname: p, closest: () => panel });
      panel.links.push(a);
      links.push(a);
    }
    panel.querySelector = () => panel.links[0] || null;
    panels[panel.id] = panel;
    btns.push(makeEl(doc, { "aria-expanded": "false", "data-cat-target": panel.id }, { panel }));
  }
  const list = makeEl(doc, {}, {
    scrollWidth, clientWidth, scrollLeft: 0,
    getBoundingClientRect: () => ({ left: 0, right: clientWidth }),
  });
  for (const el of [...btns, ...direct]) {
    el.getBoundingClientRect = () => ({ left: 0, right: 10 });
  }
  const nav = {
    querySelector(sel) {
      if (sel === ".cat-nav-list") { return list; }
      const m = /data-cat-target="([^"]+)"/.exec(sel);
      return m ? btns.find((b) => b.getAttribute("data-cat-target") === m[1]) || null : null;
    },
    querySelectorAll(sel) {
      if (sel === ".cat-summary[data-cat-target]") { return btns; }
      if (sel === ".cat-summary") { return [...btns, ...direct]; }
      if (sel === "a[href]") { return links; }
      return [];
    },
    contains: (el) => !!(el && el.inNav),
  };
  doc.querySelector = (sel) => (sel === ".cat-nav" ? nav : null);
  doc.getElementById = (id) => panels[id] || null;
  const win = { addEventListener(type, fn) { winListeners[type] = fn; } };
  vm.runInNewContext(SRC, { document: doc, window: win, location: { pathname } });
  return { doc, btns, direct, links, list, panels, docListeners, winListeners };
}

const GROUPS = [
  { id: "blocks", links: ["/tags/burokku/", "/tags/tsumiki/"] },
  { id: "puzzle", links: ["/tags/pazuru/", "/tags/jigusoo/"] },
  { id: "wooden", links: ["/tags/kinoomocha/"] },
];

test("親ボタンは 1 つずつ開き、もう一度押すと閉じる", () => {
  const { btns } = setup({ groups: GROUPS });
  btns[0].fire("click", { detail: 1 });
  assert.equal(btns[0].getAttribute("aria-expanded"), "true");
  assert.equal(btns[0].panel.hidden, false);
  btns[1].fire("click", { detail: 1 });
  assert.equal(btns[0].getAttribute("aria-expanded"), "false");
  assert.equal(btns[0].panel.hidden, true);
  assert.equal(btns[1].panel.hidden, false);
  btns[1].fire("click", { detail: 1 });
  assert.equal(btns[1].panel.hidden, true);
});

test("直接リンクのグループには aria-expanded を付けない", () => {
  const { btns, direct, docListeners } = setup({ groups: GROUPS });
  btns[0].fire("click", { detail: 1 });
  docListeners.click({ target: { inNav: false } });
  assert.equal(btns[0].panel.hidden, true);
  assert.equal(direct[0].hasAttribute("aria-expanded"), false);
});

test("Esc で閉じ、ナビの中にフォーカスがあればボタンへ戻す", () => {
  const { doc, btns, docListeners } = setup({ groups: GROUPS });
  btns[0].fire("click", { detail: 1 });
  doc.activeElement = btns[0].panel.links[1];
  docListeners.keydown({ key: "Escape" });
  assert.equal(btns[0].panel.hidden, true);
  assert.equal(doc.activeElement, btns[0]);
});

test("Esc はナビの外のフォーカスを奪わない", () => {
  const { doc, btns, docListeners } = setup({ groups: GROUPS });
  btns[0].fire("click", { detail: 1 });
  const outside = { inNav: false };
  doc.activeElement = outside;
  docListeners.keydown({ key: "Escape" });
  assert.equal(btns[0].panel.hidden, true);
  assert.equal(doc.activeElement, outside);
});

test("キーボードで開いたときだけ先頭リンクへフォーカスを移す", () => {
  const { doc, btns } = setup({ groups: GROUPS });
  btns[0].fire("click", { detail: 1 });
  assert.equal(doc.activeElement, null);
  btns[1].fire("click", { detail: 0 });
  assert.equal(doc.activeElement, btns[1].panel.links[0]);
});

test("現在地: 一致する子リンクと親ボタンに印を付ける", () => {
  const { btns } = setup({ groups: GROUPS, pathname: "/tags/jigusoo" });
  assert.equal(btns[1].panel.links[1].getAttribute("aria-current"), "page");
  assert.equal(btns[1].classList.contains("is-current"), true);
  assert.equal(btns[1].panel.links[0].getAttribute("aria-current"), null);
  assert.equal(btns[0].classList.contains("is-current"), false);
  // 現在地だからといってパネルは開かない (本文を押し下げない)。
  assert.equal(btns[1].panel.hidden, true);
});

test("現在地: 直接リンクのグループは自身に aria-current が付く", () => {
  const { direct, btns } = setup({ groups: GROUPS, pathname: "/tags/kinoomocha/" });
  assert.equal(direct[0].getAttribute("aria-current"), "page");
  assert.equal(btns.some((b) => b.classList.contains("is-current")), false);
});

test("横スクロールの続きがある側にだけ印を付ける", () => {
  const { list } = setup({ groups: GROUPS, scrollWidth: 900, clientWidth: 350 });
  assert.equal(list.classList.contains("has-more-right"), true);
  assert.equal(list.classList.contains("has-more-left"), false);
  list.scrollLeft = 200;
  list.fire("scroll");
  assert.equal(list.classList.contains("has-more-right"), true);
  assert.equal(list.classList.contains("has-more-left"), true);
  list.scrollLeft = 550;
  list.fire("scroll");
  assert.equal(list.classList.contains("has-more-right"), false);
  assert.equal(list.classList.contains("has-more-left"), true);
});

test("折り返し表示 (PC) では印を付けない", () => {
  const { list } = setup({ groups: GROUPS });
  assert.equal(list.classList.contains("has-more-right"), false);
  assert.equal(list.classList.contains("has-more-left"), false);
});
