// tests/js/sale_notice.test.mjs — hugo/assets/js/sale_notice.js のセール告知の出し分け (#9449)。
//
// サイトは静的で、main への push でしか再ビルドされない。ビルド時に出し分けると
// 終了後も「開催中」が残るので、判定は閲覧時にこの JS がやる。このテストが固定するのは:
//   A. 期間外 (開始前 / 終了後) は何も出さない
//   B. 区分の境目で文言が切り替わる (先行 → 本番)
//   C. 日時は閲覧者のタイムゾーンではなく JST で出る
//   D. データが壊れているときは出さない側に倒れる
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import path from "node:path";

const SRC = readFileSync(
  path.resolve(path.dirname(fileURLToPath(import.meta.url)),
               "../../hugo/assets/js/sale_notice.js"), "utf8");

const PHASES = [
  { label: "先行セール", headline: "Amazon プライム感謝祭 先行セール",
    start: "2026-10-13T00:00:00+09:00", end: "2026-10-15T23:59:59+09:00" },
  { label: "本番", headline: "Amazon プライム感謝祭",
    start: "2026-10-16T00:00:00+09:00", end: "2026-10-19T23:59:59+09:00" },
];

// attrs: data-phases 以外の属性 (data-sale-notice-short など)
function makeNode(phasesAttr, { withMain = true, attrs = {} } = {}) {
  const main = { textContent: "" };
  const sub = { textContent: "" };
  const all = { "data-phases": phasesAttr, ...attrs };
  return {
    hidden: true,
    main,
    sub,
    getAttribute: (name) => (name in all ? all[name] : null),
    querySelector: (sel) => {
      if (sel === "[data-sale-notice-main]") return withMain ? main : null;
      if (sel === "[data-sale-notice-sub-target]") return sub;
      return null;
    },
  };
}

// nodes を持つ document で読み込む。読み込み時の apply は now で判定される。
function load(nodes = [], now = "2026-10-01T00:00:00+09:00", readyState = "complete") {
  const listeners = [];
  const RealDate = Date;
  class FakeDate extends RealDate {
    static now() { return RealDate.parse(now); }
  }
  const sandbox = {
    Date: FakeDate,
    JSON,
    isNaN,
    document: {
      readyState,
      querySelectorAll: () => nodes,
      addEventListener: (type, fn) => listeners.push({ type, fn }),
    },
    window: { addEventListener: (type, fn) => listeners.push({ type, fn }) },
  };
  sandbox.globalThis = sandbox;
  vm.runInNewContext(SRC, sandbox);
  return { api: sandbox.window.OmochaSaleNotice, listeners };
}

const at = (iso) => Date.parse(iso);
const { api } = load();

test("A: 開始前は出さない", () => {
  assert.equal(api.message(PHASES, at("2026-10-12T23:59:59+09:00")), "");
});

test("A: 終了後は出さない", () => {
  assert.equal(api.message(PHASES, at("2026-10-20T00:00:00+09:00")), "");
});

test("B: 先行セール中は終了日時と本番の開始を出す", () => {
  assert.equal(
    api.message(PHASES, at("2026-10-13T00:00:00+09:00")),
    "Amazon プライム感謝祭 先行セール 開催中（10/15 23:59 まで）／本番は 10/16 0:00 から");
});

test("B: 区分の最終日は「本日」と出す", () => {
  assert.equal(
    api.message(PHASES, at("2026-10-15T12:00:00+09:00")),
    "Amazon プライム感謝祭 先行セール 開催中（本日 23:59 まで）／本番は 10/16 0:00 から");
});

test("B: 本番に入ると先行セールの文言は残らない", () => {
  assert.equal(
    api.message(PHASES, at("2026-10-16T00:00:00+09:00")),
    "Amazon プライム感謝祭 開催中（10/19 23:59 まで）");
  assert.equal(
    api.message(PHASES, at("2026-10-19T23:59:59+09:00")),
    "Amazon プライム感謝祭 開催中（本日 23:59 まで）");
});

test("B: 区分のすき間は次の区分の開始を出す", () => {
  assert.equal(
    api.message(PHASES, at("2026-10-15T23:59:59.500+09:00")),
    "Amazon プライム感謝祭は 10/16 0:00 から");
});

test("B: 区分が時系列順でなくても同じ結果になる", () => {
  assert.equal(
    api.message([PHASES[1], PHASES[0]], at("2026-10-14T09:00:00+09:00")),
    api.message(PHASES, at("2026-10-14T09:00:00+09:00")));
});

test("C: UTC で表した同じ瞬間でも JST の日付で出る", () => {
  // 2026-10-15T15:30Z = JST 10/16 0:30。UTC の日付 (10/15) で判定すると先行セールのままになる
  assert.equal(
    api.message(PHASES, at("2026-10-15T15:30:00Z")),
    "Amazon プライム感謝祭 開催中（10/19 23:59 まで）");
});

test("D: 壊れたデータは出さない", () => {
  const now = at("2026-10-14T09:00:00+09:00");
  assert.equal(api.message(null, now), "");
  assert.equal(api.message([], now), "");
  assert.equal(api.message([{ ...PHASES[0], end: "いつか" }, PHASES[1]], now), "");
  assert.equal(api.message([{ ...PHASES[0], start: PHASES[0].end, end: PHASES[0].start }], now), "");
  assert.equal(api.message([{ ...PHASES[0], headline: "" }], now), "");
});

test("apply: 期間中は文言を入れて hidden を外す", () => {
  const node = makeNode(JSON.stringify(PHASES));
  load([node], "2026-10-14T09:00:00+09:00");
  assert.equal(node.hidden, false);
  assert.match(node.main.textContent, /先行セール 開催中/);
});

test("apply: 期間外・壊れた JSON・文言の置き場が無いときは hidden のまま", () => {
  const ended = makeNode(JSON.stringify(PHASES));
  load([ended], "2026-10-20T00:00:00+09:00");
  assert.equal(ended.hidden, true);
  assert.equal(ended.main.textContent, "");

  const broken = makeNode("{not json");
  const noMain = makeNode(JSON.stringify(PHASES), { withMain: false });
  load([broken, noMain], "2026-10-14T09:00:00+09:00");
  assert.equal(broken.hidden, true);
  assert.equal(noMain.hidden, true);
});

test("apply: 表示中に期間が終わったら pageshow で消える", () => {
  const node = makeNode(JSON.stringify(PHASES));
  const { api: loaded, listeners } = load([node], "2026-10-19T23:00:00+09:00");
  assert.equal(node.hidden, false);
  assert.ok(listeners.some((l) => l.type === "pageshow"));
  loaded.apply({ querySelectorAll: () => [node] }, at("2026-10-20T00:00:00+09:00"));
  assert.equal(node.hidden, true);
});

// ---- 第 2 段: 商品ページの価格カード下の 1 行 ----

test("short: 次の区分の予告を省く", () => {
  assert.equal(
    api.message(PHASES, at("2026-10-14T09:00:00+09:00"), true),
    "Amazon プライム感謝祭 先行セール 開催中（10/15 23:59 まで）");
  assert.equal(api.message(PHASES, at("2026-10-20T00:00:00+09:00"), true), "");
});

test("apply: data-sale-notice-short の要素には短い文言と補足を入れる", () => {
  const node = makeNode(JSON.stringify(PHASES), {
    attrs: { "data-sale-notice-short": "", "data-sale-notice-sub": "販売ページでご確認ください" },
  });
  load([node], "2026-10-14T09:00:00+09:00");
  assert.equal(node.hidden, false);
  assert.equal(node.main.textContent, "Amazon プライム感謝祭 先行セール 開催中（10/15 23:59 まで）");
  assert.equal(node.sub.textContent, "販売ページでご確認ください");
});

test("apply: 期間外は補足も入れない", () => {
  const node = makeNode(JSON.stringify(PHASES), {
    attrs: { "data-sale-notice-short": "", "data-sale-notice-sub": "販売ページでご確認ください" },
  });
  load([node], "2026-10-20T00:00:00+09:00");
  assert.equal(node.hidden, true);
  assert.equal(node.sub.textContent, "");
});

test("apply: 読み込み中に実行されたら、後から現れる要素を DOMContentLoaded で拾う", () => {
  // スクリプトはヘッダー直下にインラインで置かれ、価格カード下の 1 行はその後ろにある
  const nodes = [];
  const { listeners } = load(nodes, "2026-10-14T09:00:00+09:00", "loading");
  const late = makeNode(JSON.stringify(PHASES));
  nodes.push(late);
  assert.equal(late.hidden, true);
  const ready = listeners.find((l) => l.type === "DOMContentLoaded");
  assert.ok(ready);
  ready.fn();
  assert.equal(late.hidden, false);
});
