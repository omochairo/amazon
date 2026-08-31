// tests/js/affiliate_click.test.mjs — hugo/assets/js/affiliate_click.js の単体テスト
//
// omcha-ops#19 P4: どの CTA が押されたかの内訳が取れず「CTA を変えて改善した」が
// 測れない、という土台の欠落を埋めるスクリプト。このテストが固定する不変条件:
//   A. 3 ASP (Amazon / 楽天 / Yahoo) を network で撃ち分ける
//   B. slot は data-cta-slot を第一に見て、無ければ class から推測する
//      (エッジに残った古い HTML でも欠測にしない)
//   C. アフィリエイト外のリンクでは何も送らない
//   D. gtag が無い環境 (広告ブロッカー等) で例外を投げない
//
// 実行: node --test tests/js/
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import path from "node:path";

const SRC_PATH = process.env.AFFILIATE_CLICK_PATH || path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../hugo/assets/js/affiliate_click.js"
);
const SRC = readFileSync(SRC_PATH, "utf8");
const PAGE_PATH = "/products/b0bsh34yr8/";

// 最小の DOM スタブ。closest / parentElement / getAttribute だけ実装する。
function makeAnchor({ href, className = "", slot = null, parent = null }) {
  const attrs = { href, "data-cta-slot": slot };
  const el = {
    className,
    parentElement: parent,
    getAttribute: (n) => (n in attrs ? attrs[n] : null),
    closest(sel) {
      if (sel === "a[href]") return el;
      let node = el;
      while (node) {
        if (sel === "[data-asin]" && node.getAttribute("data-asin")) return node;
        if (sel.startsWith(".") && (node.className || "").includes(sel.slice(1))) return node;
        node = node.parentElement;
      }
      return null;
    },
  };
  return el;
}

function run(anchor) {
  const events = [];
  let listener = null;
  const sandbox = {
    window: {
      location: { href: "https://navi.omcha.jp" + PAGE_PATH, pathname: PAGE_PATH },
      gtag: (kind, name, params) => events.push({ kind, name, params }),
    },
    document: {
      body: {},
      addEventListener: (type, fn) => {
        if (type === "click") listener = fn;
      },
    },
    URL,
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(SRC, sandbox);
  assert.ok(listener, "click listener was not registered");
  if (anchor !== undefined) listener({ target: anchor });
  return { events, sandbox, listener };
}

test("A: Amazon / 楽天 / Yahoo を network で撃ち分ける", () => {
  const cases = [
    ["https://www.amazon.co.jp/dp/B0BSH34YR8/?tag=chk01-22", "amazon"],
    ["https://item.rakuten.co.jp/shop/xyz/", "rakuten"],
    ["https://store.shopping.yahoo.co.jp/shop/xyz.html", "yahoo"],
  ];
  for (const [href, network] of cases) {
    const { events } = run(makeAnchor({ href, slot: "price-card" }));
    assert.equal(events.length, 1, href);
    assert.equal(events[0].name, "affiliate_click");
    assert.equal(events[0].params.network, network);
  }
});

test("A: Amazon リンクから ASIN を取り出す", () => {
  const { events } = run(makeAnchor({
    href: "https://www.amazon.co.jp/dp/B0BSH34YR8/?tag=chk01-22",
    slot: "price-card",
  }));
  assert.equal(events[0].params.asin, "B0BSH34YR8");
  assert.equal(events[0].params.page_path, PAGE_PATH);
});

test("A: 楽天 / Yahoo は URL に ASIN が無いので商品ページの URL から補う", () => {
  const { events } = run(makeAnchor({
    href: "https://item.rakuten.co.jp/shop/xyz/",
    slot: "price-card",
  }));
  assert.equal(events[0].params.asin, "B0BSH34YR8");
});

test("B: data-cta-slot があればそれを使う", () => {
  const { events } = run(makeAnchor({
    href: "https://www.amazon.co.jp/dp/B0BSH34YR8/?tag=chk01-22",
    className: "price-card-cta price-card-cta--amazon",
    slot: "competitor",
  }));
  assert.equal(events[0].params.slot, "competitor");
});

test("B: data-cta-slot が無ければ class から推測する (古い HTML の欠測防止)", () => {
  const cases = [
    ["m-sticky-btn m-sticky-buy", "sticky"],
    ["competitor-cta competitor-cta--amazon", "competitor"],
    ["price-card-cta price-card-cta--amazon", "price-card"],
    ["ranking-cta-external feature-cta-external--amazon", "list-card"],
    ["ranking-cta-external ranking-cta-amazon", "ranking"],
  ];
  for (const [className, slot] of cases) {
    const { events } = run(makeAnchor({
      href: "https://www.amazon.co.jp/dp/B0BSH34YR8/?tag=chk01-22",
      className,
    }));
    assert.equal(events[0].params.slot, slot, className);
  }
});

test("B: らくらくベビー導線 (omcha-ops#19 P2) は slot=baby-registry で送る", () => {
  // /dp/ が無い URL なので ASIN は商品ページの URL から補われる。
  const { events } = run(makeAnchor({
    href: "https://www.amazon.co.jp/baby-reg/?tag=chk01-22",
    className: "baby-registry-cta__link",
    slot: "baby-registry",
  }));
  assert.equal(events.length, 1);
  assert.equal(events[0].params.network, "amazon");
  assert.equal(events[0].params.slot, "baby-registry");
  assert.equal(events[0].params.asin, "B0BSH34YR8");
});

test("B: らくらくベビー導線は data-cta-slot が無くても class で拾える", () => {
  const { events } = run(makeAnchor({
    href: "https://www.amazon.co.jp/baby-reg/?tag=chk01-22",
    className: "baby-registry-cta__link",
  }));
  assert.equal(events[0].params.slot, "baby-registry");
});

test("B: 手掛かりが何も無いリンクは body として必ず送る", () => {
  const { events } = run(makeAnchor({
    href: "https://www.amazon.co.jp/dp/B0BSH34YR8/?tag=chk01-22",
  }));
  assert.equal(events.length, 1);
  assert.equal(events[0].params.slot, "body");
});

test("C: アフィリエイト外のリンクでは何も送らない", () => {
  for (const href of [
    "/products/b0bsh34yr8/",
    "https://navi.omcha.jp/cospa/",
    "https://github.com/omochairo/amazon/issues/new",
    "https://images-na.ssl-images-amazon.com/images/P/B0BSH34YR8.jpg",
  ]) {
    const { events } = run(makeAnchor({ href }));
    assert.equal(events.length, 0, href);
  }
});

test("C: <a> の外のクリックでは何も送らない", () => {
  const { events } = run({ target: null });
  assert.equal(events.length, 0);
});

test("D: gtag が無くても例外を投げない", () => {
  const { listener, sandbox } = run(undefined);
  delete sandbox.window.gtag;
  assert.doesNotThrow(() => listener({
    target: makeAnchor({ href: "https://www.amazon.co.jp/dp/B0BSH34YR8/?tag=chk01-22" }),
  }));
});
