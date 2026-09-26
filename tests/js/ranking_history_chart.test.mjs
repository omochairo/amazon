// tests/js/ranking_history_chart.test.mjs — hugo/assets/js/ranking_history_chart.js
//
// Chart.js は extend_footer から CDN 経由で defer 読み込みされる。CDN がブロック・
// 障害で読み込めないと window.Chart が永久に関数にならず、init() の
// window.setTimeout(init, 100) が無限に再帰していた (CPU/バッテリー消費が
// 無限に続く)。5秒 (50回) でリトライを諦めるようにした。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import path from "node:path";

const SRC = readFileSync(
  path.resolve(path.dirname(fileURLToPath(import.meta.url)),
               "../../hugo/assets/js/ranking_history_chart.js"), "utf8");

function boot() {
  const canvas = { getAttribute: () => "[]" }; // parsePoints 用 (中身は使わない)
  const timeoutCalls = [];
  const sandbox = {
    document: {
      readyState: "complete",
      addEventListener() {},
      querySelectorAll: (sel) =>
        (sel === ".ranking-history-canvas[data-ranking-history]" ? [canvas] : []),
    },
    window: {
      // Chart は最後まで定義しない = CDN 読み込み失敗を模す
      setTimeout: (fn, ms) => { timeoutCalls.push({ fn, ms }); return timeoutCalls.length; },
    },
    console,
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(SRC, sandbox);
  return { timeoutCalls };
}

test("Chart.js が読み込まれないままだとリトライ回数に上限があり、無限ループしない", () => {
  const { timeoutCalls } = boot();
  // 初回の init() 呼び出しで1回 setTimeout が積まれる。以降はテスト側で
  // コールバックを手動で回し、上限に達したら setTimeout が呼ばれなくなる
  // ことを確認する (旧コードは無条件に無限リトライしていた)。
  let iterations = 0;
  const MAX_ITER = 1000; // これを超えたら安全装置として打ち切る (無限ループ検出)
  while (timeoutCalls.length > 0 && iterations < MAX_ITER) {
    const call = timeoutCalls.shift();
    call.fn();
    iterations++;
  }
  assert.ok(iterations < MAX_ITER, "リトライが自然に終わる (上限が効いている・回帰防止)");
  assert.ok(iterations >= 1, "少なくとも1回はリトライしている");
});
