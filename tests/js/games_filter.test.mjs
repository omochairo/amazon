// tests/js/games_filter.test.mjs — hugo/assets/js/games_filter.js の絞り込み判定 (navi-brain#216)。
//
// 固定したいのは:
//   A. 予算の境界 (3,000 円ちょうどは「〜3,000円」に入る) と、価格の分からない物は予算で絞ると出ない
//   B. 種類は完全一致
//   C. URL のクエリは知らないキー・値を捨て、ほかのクエリは残す
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import path from "node:path";

const SRC = readFileSync(
  path.resolve(path.dirname(fileURLToPath(import.meta.url)),
               "../../hugo/assets/js/games_filter.js"), "utf8");

function load() {
  const sb = {};
  sb.window = sb;
  vm.createContext(sb);
  vm.runInContext(SRC, sb);
  return sb.OmochaGamesFilter;
}

const GF = load();

test("A. 予算の境界と価格不明", () => {
  assert.equal(GF.matches({ kind: "card", price: 3000 }, { kind: "", budget: "u3000" }), true);
  assert.equal(GF.matches({ kind: "card", price: 3001 }, { kind: "", budget: "u3000" }), false);
  assert.equal(GF.matches({ kind: "card", price: 0 }, { kind: "", budget: "u1000" }), false);
  assert.equal(GF.matches({ kind: "card", price: 0 }, { kind: "", budget: "" }), true);
});

test("B. 種類は完全一致", () => {
  assert.equal(GF.matches({ kind: "karuta", price: 500 }, { kind: "karuta", budget: "" }), true);
  assert.equal(GF.matches({ kind: "board", price: 500 }, { kind: "karuta", budget: "" }), false);
  assert.equal(GF.matches({ kind: "board", price: 500 }, { kind: "karuta", budget: "u1000" }), false);
});

test("C. クエリの読み書き", () => {
  const kinds = ["karuta", "board"];
  const s = GF.readState("?kind=board&budget=u5000&x=1", kinds);
  assert.equal(s.kind, "board");
  assert.equal(s.budget, "u5000");
  const bad = GF.readState("?kind=evil&budget=u999", kinds);
  assert.equal(bad.kind, "");
  assert.equal(bad.budget, "");
  assert.equal(GF.writeState("?x=1&kind=card", { kind: "karuta", budget: "" }), "?x=1&kind=karuta");
  assert.equal(GF.writeState("?kind=card", { kind: "", budget: "" }), "");
});

test("D. 壊れた % エンコードでも例外を投げない", () => {
  const s = GF.readState("?q=100%&kind=karuta", ["karuta"]);
  assert.equal(s.kind, "karuta");
  assert.equal(GF.writeState("?q=100%", { kind: "", budget: "u1000" }), "?q=100%&budget=u1000");
});
