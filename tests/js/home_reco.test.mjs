// tests/js/home_reco.test.mjs — hugo/assets/js/home_reco.js の回帰防止 (#9781)。
//
// localStorage.setItem が失敗する環境 (Safari プライベートブラウズ等) では _write() が
// sessionStorage に書くが、旧 _read() / _readRecent() は (localStorage || sessionStorage)
// という常に localStorage 側に評価される式で読んでいたため、ページ遷移後に読み戻せず
// ホームのおすすめが出なかった (compare.test.mjs の A と同じ型)。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import path from "node:path";

const SRC = readFileSync(
  path.resolve(path.dirname(fileURLToPath(import.meta.url)),
               "../../hugo/assets/js/home_reco.js"), "utf8");

function memStorage(init) {
  const data = new Map(Object.entries(init || {}));
  return {
    getItem: (k) => (data.has(k) ? data.get(k) : null),
    setItem: (k, v) => data.set(k, v),
  };
}

function throwingStorage() {
  return {
    getItem() { return null; },
    setItem() { throw new Error("QuotaExceededError"); },
  };
}

function boot({ local, session }) {
  const location = { pathname: "/" };
  const sandbox = {
    // readyState=loading にして DOMContentLoaded 待ちで止め、自動描画を走らせない
    document: { readyState: "loading", addEventListener() {} },
    location,
    localStorage: local,
    sessionStorage: session,
    console,
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(SRC, sandbox);
  return sandbox;
}

test("localStorage に書けず sessionStorage に逃がした候補を、別ページでも読み戻せる", () => {
  const session = memStorage({
    "omcha:recent:v1": JSON.stringify([{ asin: "B0AAAAAAAA" }]),
    "omcha:reco:v1": JSON.stringify({
      sources: {
        B0AAAAAAAA: { ts: "2026-10-18T00:00:00Z", items: [{ asin: "B0BBBBBBBB", title: "B" }] },
      },
    }),
  });
  const page = boot({ local: throwingStorage(), session });
  const got = Array.from(page.OmochaReco.list()).map((it) => it.asin);
  assert.deepEqual(got, ["B0BBBBBBBB"]);
});
