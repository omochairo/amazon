// tests/js/carousel_snap.test.mjs — hugo/assets/js/carousel_swipe.js のスナップ制御。
//
// #5081: .carousel-wrapper / .age-timeline-track は scroll-snap コンテナで、
// 読み込み中に中身が入るたび再スナップして scrollLeft が動く。Chrome はその
// スクロールで LCP 候補の記録を打ち切るため、商品ページは LCP 候補が 1 件も
// 記録されない状態だった (実測 2026-08-31: trace の
// largestContentfulPaint::Candidate 0 件 / Invalidate 9 件)。
//
// そこで scroll-snap-type を CSS の :root.snap-ready 側に移し、この JS が
// **最初のユーザー操作まで**クラスを付けないようにした。このテストが固定するのは:
//   A. 読み込み時点では snap-ready を付けない (付けたら LCP が死ぬ)
//   B. 最初の入力で付く (吸着感は失わない)
//   C. 待ち受けは once + passive (スクロール性能を落とさない)
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import path from "node:path";

const SRC = readFileSync(
  path.resolve(path.dirname(fileURLToPath(import.meta.url)),
               "../../hugo/assets/js/carousel_swipe.js"), "utf8");

function run(readyState = "complete") {
  const classes = new Set();
  const listeners = [];
  const sandbox = {
    document: {
      readyState,
      documentElement: { classList: { add: (c) => classes.add(c),
                                      contains: (c) => classes.has(c) } },
      querySelectorAll: () => [],
      addEventListener: () => {},
    },
    window: {
      addEventListener: (type, fn, opts) => listeners.push({ type, fn, opts }),
    },
  };
  sandbox.globalThis = sandbox;
  sandbox.addEventListener = sandbox.window.addEventListener;
  vm.createContext(sandbox);
  vm.runInContext(SRC, sandbox);
  return { classes, listeners };
}

test("A: 読み込み時点では snap-ready を付けない", () => {
  const { classes } = run();
  assert.equal(classes.has("snap-ready"), false);
});

test("B: 最初の入力で snap-ready が付く", () => {
  for (const type of ["pointerdown", "touchstart", "wheel", "keydown"]) {
    const { classes, listeners } = run();
    const l = listeners.find((x) => x.type === type);
    assert.ok(l, `${type} listener missing`);
    l.fn();
    assert.equal(classes.has("snap-ready"), true, type);
  }
});

test("C: 入力待ち受けは once かつ passive", () => {
  const { listeners } = run();
  for (const type of ["pointerdown", "touchstart", "wheel", "keydown"]) {
    const l = listeners.find((x) => x.type === type);
    assert.equal(l.opts.once, true, `${type} once`);
    assert.equal(l.opts.passive, true, `${type} passive`);
  }
});

test("A: DOMContentLoaded 前でも付けない (readyState=loading)", () => {
  const { classes } = run("loading");
  assert.equal(classes.has("snap-ready"), false);
});
