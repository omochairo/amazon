// tests/js/list_filter.test.mjs — hugo/assets/js/list_filter.js の絞り込み判定。
//
// /deals/ と /price/ の一覧を予算・お子さまの年齢・並び順で絞る。固定したいのは:
//   A. 年齢はおもちゃ診断と同じ安全側の規則 (対象年齢がお子さまの年齢を超えるものは出さない、
//      幼すぎるものも出さない、対象年齢不明は出さない)
//   B. 0 歳〜 (age=0) を「不明」と取り違えない
//   C. 予算の境界 (3,000 円ちょうどは 3,000〜5,000 円側)
//   D. 並び替えで値の無い商品は末尾、同値は元の順
//   E. URL のクエリは知らないキー・値を捨てる
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import path from "node:path";

const SRC = readFileSync(
  path.resolve(path.dirname(fileURLToPath(import.meta.url)),
               "../../hugo/assets/js/list_filter.js"), "utf8");

function load() {
  const sb = {};
  sb.window = sb;
  vm.createContext(sb);
  vm.runInContext(SRC, sb);
  return sb.OmochaListFilter;
}

const LF = load();
const item = (o) => ({ price: NaN, age: NaN, dropYen: NaN, lowest: false, order: 0, ...o });

test("A: 対象年齢がお子さまの年齢を超える商品は出さない", () => {
  assert.equal(LF.matches(item({ age: 36 }), { age: "3y" }), true);
  assert.equal(LF.matches(item({ age: 47 }), { age: "3y" }), true);
  assert.equal(LF.matches(item({ age: 48 }), { age: "3y" }), false);
});

test("A: 幼すぎる商品は出さない (3 歳なら 12 か月未満は外す)", () => {
  assert.equal(LF.matches(item({ age: 12 }), { age: "3y" }), true);
  assert.equal(LF.matches(item({ age: 11 }), { age: "3y" }), false);
});

test("A: 対象年齢が不明な商品は年齢で絞ったときだけ出さない", () => {
  assert.equal(LF.matches(item({}), { age: "2y" }), false);
  assert.equal(LF.matches(item({}), {}), true);
});

test("B: 0 歳〜の商品は 0 歳で出る", () => {
  assert.equal(LF.matches(item({ age: 0 }), { age: "0y" }), true);
  assert.equal(LF.matches(item({ age: 0 }), { age: "1y" }), true);
});

test("C: 予算の境界", () => {
  assert.equal(LF.matches(item({ price: 2999 }), { budget: "2000-3000" }), true);
  assert.equal(LF.matches(item({ price: 3000 }), { budget: "2000-3000" }), false);
  assert.equal(LF.matches(item({ price: 3000 }), { budget: "3000-5000" }), true);
  assert.equal(LF.matches(item({ price: 25000 }), { budget: "o10000" }), true);
  assert.equal(LF.matches(item({}), { budget: "u2000" }), false);
});

test("過去最安値のみ", () => {
  assert.equal(LF.matches(item({ lowest: true }), { lowest: true }), true);
  assert.equal(LF.matches(item({ lowest: false }), { lowest: true }), false);
});

test("D: 価格順は値の無い商品が末尾、同値は元の順", () => {
  const xs = [
    item({ order: 0, price: 5000 }),
    item({ order: 1 }),
    item({ order: 2, price: 1000 }),
    item({ order: 3, price: 5000 }),
  ];
  const asc = xs.slice().sort(LF.SORTS["price-asc"]).map((x) => x.order);
  assert.equal(JSON.stringify(asc), JSON.stringify([2, 0, 3, 1]));
  const desc = xs.slice().sort(LF.SORTS["price-desc"]).map((x) => x.order);
  assert.equal(JSON.stringify(desc), JSON.stringify([0, 3, 2, 1]));
});

test("E: クエリの読み書き", () => {
  const st = LF.readState("?budget=3000-5000&age=3y&sort=price-asc&lowest=1&x=1");
  assert.equal(JSON.stringify(st), JSON.stringify({ budget: "3000-5000", age: "3y", sort: "price-asc", lowest: true }));
  assert.equal(LF.writeState(st), "?budget=3000-5000&age=3y&sort=price-asc&lowest=1");
  const bad = LF.readState("?budget=999&age=99y&sort=evil");
  assert.equal(JSON.stringify(bad), JSON.stringify({ budget: "", age: "", sort: "default", lowest: false }));
  assert.equal(LF.writeState(bad), "");
});

test("E: 絞り込み以外のクエリ (utm など) は残す", () => {
  const search = "?utm_source=x&age=1y&ref=a%20b";
  const st = LF.readState(search);
  assert.equal(LF.writeState({ ...st, budget: "u2000" }, search), "?utm_source=x&ref=a%20b&budget=u2000&age=1y");
  assert.equal(LF.writeState({ budget: "", age: "", sort: "default", lowest: false }, search), "?utm_source=x&ref=a%20b");
});

test("E: 壊れたパーセントエンコードで止まらない", () => {
  const st = LF.readState("?age=%E3%81&budget=u2000&%ZZ=1");
  assert.equal(st.budget, "u2000");
  assert.equal(st.age, "");
});

test("値下げ額順は値の無い商品が末尾", () => {
  const xs = [item({ order: 0 }), item({ order: 1, dropYen: 300 }), item({ order: 2, dropYen: 900 })];
  const got = xs.slice().sort(LF.SORTS["drop-yen"]).map((x) => x.order);
  assert.equal(JSON.stringify(got), JSON.stringify([2, 1, 0]));
});
