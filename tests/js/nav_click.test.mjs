// tests/js/nav_click.test.mjs — hugo/assets/js/nav_click.js の単体テスト
//
// navi-brain#213: サイト内の移動 (nav_click) とトップの区画の到達 (home_section_view)。
// このテストが固定する不変条件:
//   A. data-nav-slot の付いた a / button が押されると slot をそのまま送る
//   B. [data-nav-section] の中の同一オリジンの内部リンクは slot = "home:<区画>"
//   C. 外部リンク・アフィリエイトのリンクでは送らない (購入クリックは affiliate_click.js が数える)
//   D. gtag が無くても例外を出さない。遷移を止めない
//   E. 二重に読み込まれてもリスナーは 1 本
//   F. home_section_view は区画ごとに 1 回だけ。見出しを見る
//   G. 新イベントの内訳は slot 1 つだけ (未登録のカスタムディメンションを作らない)
//
// 実行: node --test tests/js/
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import path from "node:path";

const SRC_PATH = process.env.NAV_CLICK_PATH || path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../hugo/assets/js/nav_click.js"
);
const SRC = readFileSync(SRC_PATH, "utf8");
const ORIGIN = "https://navi.omcha.jp";

// 最小の DOM スタブ。closest は属性セレクタ 3 種だけ実装する。
function makeEl({ tag = "a", attrs = {}, parent = null, children = [] } = {}) {
  const el = {
    tagName: tag.toUpperCase(),
    parentElement: parent,
    children,
    getAttribute: (n) => (n in attrs ? attrs[n] : null),
    hasAttribute: (n) => n in attrs,
    closest(sel) {
      let node = el;
      while (node) {
        const m = /^\[([a-z-]+)\]$/.exec(sel);
        if (m && node.hasAttribute(m[1])) return node;
        if (sel === "a[href]" && node.tagName === "A" && node.hasAttribute("href")) return node;
        if (sel === "button" && node.tagName === "BUTTON") return node;
        node = node.parentElement;
      }
      return null;
    },
    contains(other) {
      let node = other;
      while (node) { if (node === el) return true; node = node.parentElement; }
      return false;
    },
    querySelector(sel) {
      if (sel !== "h2") return null;
      const stack = [...el.children];
      while (stack.length) {
        const c = stack.shift();
        if (c.tagName === "H2") return c;
        stack.push(...c.children);
      }
      return null;
    },
  };
  children.forEach((c) => { c.parentElement = el; });
  return el;
}

function load({ sections = [], withIO = true, withGtag = true, sandbox: existing = null } = {}) {
  const events = [];
  const listeners = [];
  const observed = [];
  const unobserved = [];
  let ioCallback = null;
  const sandbox = existing || {
    window: {
      location: { href: ORIGIN + "/", origin: ORIGIN, pathname: "/" },
    },
    document: {
      readyState: "interactive",
      addEventListener: (type, fn, capture) => listeners.push({ type, fn, capture }),
      querySelectorAll: (sel) => (sel === "[data-nav-section]" ? sections : []),
    },
    URL,
  };
  if (withGtag) sandbox.window.gtag = (kind, name, params) => events.push({ kind, name, params });
  if (withIO && !existing) {
    sandbox.IntersectionObserver = function (cb, opts) {
      ioCallback = cb;
      sandbox.__ioOpts = opts;
      this.observe = (t) => observed.push(t);
      this.unobserve = (t) => unobserved.push(t);
    };
  }
  sandbox.globalThis = sandbox;
  if (!existing) vm.createContext(sandbox);
  vm.runInContext(SRC, sandbox);
  const click = (target) => {
    const l = listeners.find((x) => x.type === "click");
    assert.ok(l, "click listener was not registered");
    l.fn({ target });
  };
  const intersect = (target, isIntersecting = true) =>
    ioCallback([{ target, isIntersecting }]);
  return { events, listeners, observed, unobserved, sandbox, click, intersect };
}

test("A: data-nav-slot の a が押されると slot をそのまま送る", () => {
  const { events, click } = load();
  click(makeEl({ attrs: { href: "/diagnosis/", "data-nav-slot": "menu:diagnosis" } }));
  assert.equal(events.length, 1);
  assert.equal(events[0].kind, "event");
  assert.equal(events[0].name, "nav_click");
  assert.deepEqual(Object.keys(events[0].params), ["slot"]); // G: slot 以外のパラメータを作らない
  assert.equal(events[0].params.slot, "menu:diagnosis");
});

test("A: button も拾う。中の要素のクリックでも親の属性を見る", () => {
  const { events, click } = load();
  const btn = makeEl({ tag: "button", attrs: { "data-nav-slot": "catnav:group-x" } });
  const inner = makeEl({ tag: "span", parent: btn });
  click(inner);
  assert.equal(events.length, 1);
  assert.equal(events[0].params.slot, "catnav:group-x");
});

test("A: slot は 100 字で切る", () => {
  const { events, click } = load();
  click(makeEl({ attrs: { href: "/", "data-nav-slot": "x".repeat(150) } }));
  assert.equal(events[0].params.slot.length, 100);
});

test("B: [data-nav-section] の中の同一オリジンのリンクは home:<区画>", () => {
  const { events, click } = load();
  const sec = makeEl({ tag: "section", attrs: { "data-nav-section": "latest" } });
  for (const href of ["/products/b0bsh34yr8/", ORIGIN + "/ranking/", "posts/"]) {
    click(makeEl({ attrs: { href }, parent: sec }));
  }
  assert.equal(events.length, 3);
  assert.ok(events.every((e) => e.name === "nav_click" && e.params.slot === "home:latest"));
});

test("B: カードの中のボタン (お気に入り・比較) では送らない。カード本体なら送る", () => {
  const { events, click } = load();
  const sec = makeEl({ tag: "section", attrs: { "data-nav-section": "latest" } });
  const card = makeEl({ attrs: { href: "/products/b0bsh34yr8/", class: "product-card" }, parent: sec });
  const fav = makeEl({ tag: "button", attrs: { class: "fav-toggle" }, parent: card });
  const icon = makeEl({ tag: "span", parent: fav });
  click(fav);
  click(icon);
  assert.equal(events.length, 0);
  click(makeEl({ tag: "img", parent: card }));
  assert.equal(events.length, 1);
  assert.equal(events[0].params.slot, "home:latest");
});

test("B: data-nav-slot があれば区画の中でもそちらを優先する (二重に送らない)", () => {
  const { events, click } = load();
  const sec = makeEl({ tag: "section", attrs: { "data-nav-section": "spotlight" } });
  click(makeEl({ attrs: { href: "/cospa/", "data-nav-slot": "spot:cospa" }, parent: sec }));
  assert.equal(events.length, 1);
  assert.equal(events[0].params.slot, "spot:cospa");
});

test("B: 区画の外の素のリンクでは何も送らない", () => {
  const { events, click } = load();
  click(makeEl({ attrs: { href: "/products/b0bsh34yr8/" } }));
  click(null);
  click(makeEl({ tag: "div" }));
  assert.equal(events.length, 0);
});

test("C: 外部リンク・アフィリエイトのリンクでは送らない", () => {
  const { events, click } = load();
  const sec = makeEl({ tag: "section", attrs: { "data-nav-section": "latest" } });
  const cases = [
    { href: "https://omcha.jp/" },
    { href: "https://github.com/omochairo/amazon" },
    { href: "https://www.amazon.co.jp/dp/B0BSH34YR8/?tag=chk01-22" },
    { href: "https://item.rakuten.co.jp/shop/xyz/" },
    { href: "https://store.shopping.yahoo.co.jp/shop/xyz.html" },
    { href: "https://amzn.to/abc" },
    { href: "/go/amazon/", rel: "sponsored noopener" }, // 同一オリジンでも sponsored は除く
    { href: "mailto:a@example.com" },
  ];
  for (const { href, rel } of cases) {
    click(makeEl({ attrs: rel ? { href, rel } : { href }, parent: sec }));
  }
  assert.equal(events.length, 0);
});

test("D: gtag が無くても例外を投げない (クリックもセクション監視も)", () => {
  const sec = makeEl({ tag: "section", attrs: { "data-nav-section": "latest" },
                       children: [makeEl({ tag: "h2" })] });
  const { click, intersect, sandbox } = load({ sections: [sec], withGtag: false });
  assert.equal(sandbox.window.gtag, undefined);
  assert.doesNotThrow(() => click(makeEl({ attrs: { href: "/", "data-nav-slot": "menu:home" } })));
  assert.doesNotThrow(() => intersect(sec.children[0]));
});

test("D: gtag が例外を投げても外に出さない", () => {
  const { click, sandbox } = load();
  sandbox.window.gtag = () => { throw new Error("boom"); };
  assert.doesNotThrow(() => click(makeEl({ attrs: { href: "/", "data-nav-slot": "menu:home" } })));
});

test("D: preventDefault を呼ばない (遷移を止めない)", () => {
  const { listeners } = load();
  const l = listeners.find((x) => x.type === "click");
  let prevented = false;
  l.fn({
    target: makeEl({ attrs: { href: "/", "data-nav-slot": "menu:home" } }),
    preventDefault() { prevented = true; },
  });
  assert.equal(prevented, false);
});

test("E: 二重に読み込んでもリスナーは 1 本", () => {
  const first = load();
  const before = first.listeners.filter((x) => x.type === "click").length;
  assert.equal(before, 1);
  vm.runInContext(SRC, first.sandbox); // 同じ window にもう一度
  assert.equal(first.listeners.filter((x) => x.type === "click").length, 1);
  first.click(makeEl({ attrs: { href: "/", "data-nav-slot": "menu:home" } }));
  assert.equal(first.events.length, 1);
});

test("F: home_section_view は区画ごとに 1 回だけ。見出し (h2) を見る", () => {
  const h2a = makeEl({ tag: "h2" });
  const secA = makeEl({ tag: "section", attrs: { "data-nav-section": "latest" },
                        children: [makeEl({ tag: "header", children: [h2a] })] });
  const secB = makeEl({ tag: "section", attrs: { "data-nav-section": "recently-viewed", hidden: "" } });
  const { events, observed, unobserved, intersect, sandbox } = load({ sections: [secA, secB] });
  // 見出しがあれば見出し、無ければ区画そのものを監視する
  assert.deepEqual(observed, [h2a, secB]);
  assert.deepEqual({ ...sandbox.__ioOpts }, { threshold: 0, rootMargin: "100000px 0px -20% 0px" });

  intersect(h2a, false); // 画面外 → 送らない
  assert.equal(events.length, 0);
  intersect(h2a);
  intersect(h2a);
  intersect(h2a); // 万一 observer が再通知しても 2 回目は送らない
  assert.ok(unobserved.includes(h2a));
  assert.equal(events.length, 1);
  assert.equal(events[0].name, "home_section_view");
  assert.deepEqual(Object.keys(events[0].params), ["slot"]);
  assert.equal(events[0].params.slot, "sec:latest");
  intersect(secB);
  assert.equal(events[events.length - 1].params.slot, "sec:recently-viewed");
});

test("F: IntersectionObserver が無いブラウザでは何もしない (クリックは動く)", () => {
  const sec = makeEl({ tag: "section", attrs: { "data-nav-section": "latest" } });
  const { events, observed, click } = load({ sections: [sec], withIO: false });
  assert.equal(observed.length, 0);
  assert.equal(events.length, 0);
  click(makeEl({ attrs: { href: "/", "data-nav-slot": "menu:home" } }));
  assert.equal(events.length, 1);
});

test("F: DOM の構築中に読み込まれたら DOMContentLoaded を待つ", () => {
  const sec = makeEl({ tag: "section", attrs: { "data-nav-section": "latest" } });
  const sandbox = {
    window: { location: { href: ORIGIN + "/", origin: ORIGIN, pathname: "/" } },
    document: { readyState: "loading", addEventListener() {}, querySelectorAll: () => [sec] },
    URL,
  };
  const { listeners, observed } = load({ sections: [sec], sandbox: null });
  assert.equal(observed.length, 1); // interactive の通常ケース
  // loading のとき: DOMContentLoaded のリスナーが付き、発火後に監視を始める
  const l2 = [];
  const obs = [];
  sandbox.document.addEventListener = (type, fn) => l2.push({ type, fn });
  sandbox.IntersectionObserver = function () { this.observe = (t) => obs.push(t); this.unobserve = () => {}; };
  vm.createContext(sandbox);
  vm.runInContext(SRC, sandbox);
  assert.equal(obs.length, 0);
  l2.find((x) => x.type === "DOMContentLoaded").fn();
  assert.equal(obs.length, 1);
  assert.ok(listeners.length >= 1);
});
