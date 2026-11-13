// tests/js/mobile_sticky_cta.test.mjs — hugo/assets/js/mobile_sticky_cta.js の
// 末尾セクション監視 (_setupVisibility の bottom sentinels)。
//
// 商品ページの末尾には post-footer / recently-viewed-mount--post /
// ranking-history-chart の複数 sentinel を同時に IntersectionObserver で監視
// している。IntersectionObserver の callback には「今回状態が変化した要素」しか
// entries に来ない (仕様) ため、旧コードのように毎回の entries だけで
// anyVisible を決めると、既に表示中の別 sentinel の状態を見失う。
// このテストは「sentinel A が表示中のまま sentinel B だけが非表示になった」
// ケースで sticky CTA が誤って再表示されないことを固定する。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import path from "node:path";

const SRC = readFileSync(
  path.resolve(path.dirname(fileURLToPath(import.meta.url)),
               "../../hugo/assets/js/mobile_sticky_cta.js"), "utf8");

function boot() {
  const ioInstances = [];
  class FakeIntersectionObserver {
    constructor(cb) {
      this.cb = cb;
      this.observed = [];
      ioInstances.push(this);
    }
    observe(el) { this.observed.push(el); }
    disconnect() {}
  }

  const postFooter = { __name: "post-footer" };
  const recentlyViewed = { __name: "recently-viewed" };
  const rankingHistory = { __name: "ranking-history" };
  const selectorMap = new Map([
    [".post-header", null], // topSentinel 無し = topPassed は常に true
    [".post-footer", postFooter],
    [".recently-viewed-mount--post", recentlyViewed],
    [".ranking-history-chart", rankingHistory],
  ]);

  const bar = {
    _hidden: undefined,
    toggleAttribute(name, val) { this._hidden = val; },
    getAttribute() { return null; },
    querySelector() { return null; },
  };

  const sandbox = {
    document: {
      readyState: "complete",
      getElementById: (id) => (id === "mobile-sticky-cta" ? bar : null),
      querySelector: (sel) => selectorMap.get(sel) ?? null,
      querySelectorAll: () => [],
      addEventListener() {},
    },
    window: {},
    IntersectionObserver: FakeIntersectionObserver,
    setTimeout: () => 0,
    console,
  };
  sandbox.window.IntersectionObserver = FakeIntersectionObserver;
  sandbox.window.location = { pathname: "/products/test/" };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(SRC, sandbox);

  return { bar, ioInstances, postFooter, recentlyViewed, rankingHistory };
}

test("末尾 sentinel が1つ表示中のまま別 sentinel が非表示化しても表示中のまま扱う", () => {
  const { bar, ioInstances, postFooter, rankingHistory } = boot();
  assert.equal(ioInstances.length, 1, "bottom sentinel 用の IntersectionObserver が1つ作られる");
  const bottomObserver = ioInstances[0];

  // 1. post-footer が視界に入る (sticky CTA は隠れるはず)
  bottomObserver.cb([{ target: postFooter, isIntersecting: true }]);
  assert.equal(bar._hidden, true, "post-footer が見えている間は隠れる");

  // 2. ranking-history-chart も視界に入る (post-footer は変化なしなので entries に出ない)
  bottomObserver.cb([{ target: rankingHistory, isIntersecting: true }]);
  assert.equal(bar._hidden, true);

  // 3. ranking-history-chart だけが視界から外れる。post-footer はまだ見えている
  //    (変化していないので entries に含まれない) が、旧コードは entries だけで
  //    判定していたため anyVisible=false と誤判定し、ここで sticky CTA が
  //    再表示されてしまっていた。
  bottomObserver.cb([{ target: rankingHistory, isIntersecting: false }]);
  assert.equal(bar._hidden, true, "post-footer がまだ見えているので隠れたままのはず (回帰防止)");

  // 4. 残っていた post-footer も視界から外れたら、ようやく表示してよい
  bottomObserver.cb([{ target: postFooter, isIntersecting: false }]);
  assert.equal(bar._hidden, false, "全 sentinel が非表示になったら sticky CTA を表示する");
});

// #9781: 購入ボタンの aria-label が固定の「最安値で購入する」で、見えている店名・
// 価格を読み上げから隠していた。リンク先が決まったら店名と価格を含めて付け直す。
function bootBuy({ cheapestLabel, minPrice }) {
  function el(attrs = {}) {
    return {
      _attrs: { ...attrs },
      textContent: "",
      getAttribute(n) { return n in this._attrs ? this._attrs[n] : null; },
      setAttribute(n, v) { this._attrs[n] = String(v); },
      addEventListener() {},
      querySelector() { return null; },
    };
  }
  const labelEl = el();
  const anchor = el({ href: "https://www.amazon.co.jp/dp/B0TEST0001/", "aria-label": "最安値で購入する" });
  anchor.querySelector = (sel) => (sel === ".m-sticky-buy-label" ? labelEl : null);
  const bar = el({ "data-asin": "B0TEST0001", "data-min-price": String(minPrice) });
  bar.toggleAttribute = () => {};
  bar.querySelector = (sel) => (sel === ".m-sticky-buy" ? anchor : null);
  const cta = el({ href: "https://item.rakuten.co.jp/x/y/" });
  const cardLabel = { textContent: cheapestLabel };
  const selectorMap = new Map([
    [".price-card.is-cheapest .price-card-cta[href]", cta],
    [".price-card.is-cheapest .price-card-label", cardLabel],
  ]);
  const sandbox = {
    document: {
      readyState: "complete",
      getElementById: (id) => (id === "mobile-sticky-cta" ? bar : null),
      querySelector: (sel) => selectorMap.get(sel) ?? null,
      querySelectorAll: () => [],
      addEventListener() {},
    },
    window: { location: { pathname: "/products/test/" } },
    setTimeout: () => 0,
    console,
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(SRC, sandbox);
  return { anchor, labelEl };
}

test("購入ボタンの aria-label に最安の店名と価格が入る (#9781)", () => {
  const { anchor, labelEl } = bootBuy({ cheapestLabel: "楽天", minPrice: 12345 });
  assert.equal(labelEl.textContent, "楽天 最安");
  assert.equal(anchor.getAttribute("aria-label"), "楽天で購入する（最安 12,345円）");
});

test("価格が無ければ店名だけ、店名が無ければ「最安値の店」と読む (#9781)", () => {
  assert.equal(bootBuy({ cheapestLabel: " Yahoo! ", minPrice: 0 }).anchor.getAttribute("aria-label"),
               "Yahoo!で購入する");
  assert.equal(bootBuy({ cheapestLabel: "", minPrice: 980 }).anchor.getAttribute("aria-label"),
               "最安値の店で購入する（最安 980円）");
});

// navi-brain#165: 記事の購入ボタンの箱 (.buy-box) の主ボタンを、.price-card.is-cheapest
// (未確認の楽天/Yahoo を指すことがある) より優先する。金額も主ボタンに合わせる。
function bootBuyBox({ kind, price, site, minPrice }) {
  function el(attrs = {}) {
    return {
      _attrs: { ...attrs },
      textContent: "",
      style: {},
      getAttribute(n) { return n in this._attrs ? this._attrs[n] : null; },
      setAttribute(n, v) { this._attrs[n] = String(v); },
      addEventListener() {},
      querySelector() { return null; },
    };
  }
  const labelEl = el();
  const priceEl = el();
  priceEl.textContent = "¥" + Number(minPrice).toLocaleString("ja-JP");
  const rowEl = el();
  const anchor = el({ href: "https://www.amazon.co.jp/dp/B0TEST0001/", "aria-label": "最安値で購入する" });
  anchor.querySelector = (sel) => ({
    ".m-sticky-buy-label": labelEl,
    ".m-sticky-buy-price": priceEl,
    ".m-sticky-buy-price-row": rowEl,
  })[sel] ?? null;
  const bar = el({ "data-asin": "B0TEST0001", "data-min-price": String(minPrice) });
  bar.toggleAttribute = () => {};
  bar.querySelector = (sel) => (sel === ".m-sticky-buy" ? anchor : null);
  const primaryAttrs = { href: "https://www.amazon.co.jp/dp/B0TEST0001/?tag=x-22",
                         "data-buy-site": site, "data-buy-kind": kind };
  if (price) primaryAttrs["data-buy-price"] = String(price);
  const primary = el(primaryAttrs);
  const cheapestCard = el({ href: "https://item.rakuten.co.jp/x/y/" });
  const selectorMap = new Map([
    [".buy-box .buy-box-primary[href]", primary],
    [".price-card.is-cheapest .price-card-cta[href]", cheapestCard],
    [".price-card.is-cheapest .price-card-label", { textContent: "楽天市場" }],
  ]);
  const sandbox = {
    document: {
      readyState: "complete",
      getElementById: (id) => (id === "mobile-sticky-cta" ? bar : null),
      querySelector: (sel) => selectorMap.get(sel) ?? null,
      querySelectorAll: () => [],
      addEventListener() {},
    },
    window: { location: { pathname: "/products/test/" } },
    setTimeout: () => 0,
    console,
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(SRC, sandbox);
  return { anchor, labelEl, priceEl, rowEl };
}

test("購入ボタンの箱の主ボタンを最安カードより優先し、金額も合わせる (#165)", () => {
  const { anchor, labelEl, priceEl, rowEl } = bootBuyBox(
    { kind: "cheapest", price: 3000, site: "Amazon", minPrice: 2500 });
  assert.equal(anchor.getAttribute("href"), "https://www.amazon.co.jp/dp/B0TEST0001/?tag=x-22");
  assert.equal(labelEl.textContent, "Amazon 最安");
  assert.equal(priceEl.textContent, "¥3,000");
  assert.equal(rowEl.style.display, undefined);
  assert.equal(anchor.getAttribute("aria-label"), "Amazonで購入する（最安 3,000円）");
});

test("主ボタンが最安でない行き先 (商品ページ / 検索) なら金額を出さない (#165)", () => {
  const direct = bootBuyBox({ kind: "direct", price: 0, site: "Amazon", minPrice: 2500 });
  assert.equal(direct.labelEl.textContent, "Amazonで見る");
  assert.equal(direct.rowEl.style.display, "none");
  assert.equal(direct.anchor.getAttribute("aria-label"), "Amazonで購入する");
  const search = bootBuyBox({ kind: "search", price: 0, site: "Amazon", minPrice: 0 });
  assert.equal(search.labelEl.textContent, "Amazonで検索");
  assert.equal(search.anchor.getAttribute("aria-label"), "Amazonで検索する");
});
