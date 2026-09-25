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
