// tests/js/wiki_tooltip.test.mjs — hugo/assets/js/wiki_tooltip.js の「どの語に 💡 を付けるか」の選定。
//
// 以前は読み込み後に .post-content 全体を走査して、cache の全語を何個でも差し込んでいた。
// 日本語の語は語の区切りを見ず (「マルカン」の「マルカ」)、見出し・強調・テンプレートの
// 決まり文句の中にも入り、同じ解説が別名で何度も出ていた (#165 T5)。
// DOM に触らない純粋な関数 (selectMatches / boundaryOk / isSkipElement ...) だけを検証する。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import path from "node:path";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const SRC = readFileSync(path.join(ROOT, "hugo/assets/js/wiki_tooltip.js"), "utf8");

const sandbox = {
  window: { matchMedia: () => ({ matches: false }) },
  document: { readyState: "complete", querySelector: () => null, addEventListener() {} },
  fetch: () => Promise.reject(new Error("fetch は呼ばれない想定")),
};
vm.createContext(sandbox);
vm.runInContext(SRC, sandbox);
const W = sandbox.window.__wikiTooltip;

// term -> entry の最小の cache。url が解説の同一性を決める。
function mkEntries(spec) {
  const out = {};
  for (const [term, url] of Object.entries(spec)) out[term] = { type: "wikipedia", title: url, url: "https://x/" + url, extract: "e" };
  return out;
}
function pick(text, entries, state = W.newState()) {
  // vm の別 realm の配列なので、deepEqual が通るよう Array.from で作り直す
  return Array.from(W.selectMatches(text, W.sortKeys(Object.keys(entries)), entries, state), (m) => m.term);
}
function el(tag, { cls = "", skipAttr = false } = {}) {
  return {
    nodeType: 1, tagName: tag,
    hasAttribute: (n) => n === "data-wiki-skip" && skipAttr,
    classList: { contains: (c) => cls.split(/\s+/).includes(c) },
  };
}

// ---- 規則 5: 字種の境界 ----
test("カタカナの続きの途中には当てない: マルカン の マルカ", () => {
  assert.deepEqual(pick("マルカンのおもちゃ", mkEntries({ マルカ: "a" })), []);
  assert.deepEqual(pick("マルカのおもちゃ", mkEntries({ マルカ: "a" })), ["マルカ"]);
});

test("2 文字の漢字の語は、漢字が続くとき当てない", () => {
  const e = mkEntries({ 指先: "a" });
  assert.deepEqual(pick("親指先に力を入れる", e), []);
  assert.deepEqual(pick("指先を使う", e), ["指先"]);
  assert.deepEqual(pick("両手の指先", e), ["指先"]);
});

test("漢字の語の前後が漢字なら当てない", () => {
  assert.deepEqual(pick("発達段階表を見る", mkEntries({ 発達段階: "a" })), []);
  assert.deepEqual(pick("発達段階に合わせる", mkEntries({ 発達段階: "a" })), ["発達段階"]);
});

test("英数字の語は前後が英数字なら当てない (従来どおり)", () => {
  const e = mkEntries({ Hape: "a" });
  assert.deepEqual(pick("Hapeful toys", e), []);
  assert.deepEqual(pick("by Hape.", e), ["Hape"]);
  assert.deepEqual(pick("(Hape)", e), ["Hape"]);
});

test("ひらがなは字種の対象外 (助詞が続くため)", () => {
  assert.equal(W.charClass("の"), null);
  assert.equal(W.charClass("ー"), "kata");
  assert.equal(W.charClass("館"), "kanji");
  assert.equal(W.boundaryOk("お試し感覚", 3, 5), true); // 字種の規則だけでは防げない。一覧から外して防ぐ (下の cache のテスト)
});

test("字種が違う続きは語の途中とみなさない: タカラトミー製", () => {
  assert.deepEqual(pick("タカラトミー製のプラレール", mkEntries({ タカラトミー: "a" })), ["タカラトミー"]);
});

// ---- 字種の規則では防げない別物 ----
test("フレーベル館 (出版社) の フレーベル には当てない", () => {
  const e = mkEntries({ フレーベル: "a" });
  assert.deepEqual(pick("やなせたかし/フレーベル館", e), []);
  assert.deepEqual(pick("フレーベルが考案した恩物", e), ["フレーベル"]);
});

// ---- 規則 3: 同じ解説は 1 回 ----
test("別名でも同じリンク先なら 1 ページ 1 回 (KONAMI / コナミ)", () => {
  const e = mkEntries({ KONAMI: "konami", コナミ: "konami" });
  const st = W.newState();
  assert.equal(pick("コナミ (KONAMI) の新作", e, st).length, 1);
  assert.equal(pick("KONAMI のゲーム", e, st).length, 0);
});

test("同じ語は、別のテキストノードでも 1 回だけ", () => {
  const e = mkEntries({ レゴ: "lego" });
  const st = W.newState();
  assert.deepEqual(pick("レゴで遊ぶ", e, st), ["レゴ"]);
  assert.deepEqual(pick("レゴは楽しい", e, st), []);
});

test("実際の cache: タカラトミー(TAKARA TOMY) は 💡 が 1 つ", () => {
  const entries = JSON.parse(readFileSync(path.join(ROOT, "hugo/static/wiki_cache.json"), "utf8")).entries;
  const got = pick("タカラトミー(TAKARA TOMY)のプラレール", entries);
  assert.equal(got.length, 1);
});

// ---- 規則 4: 上限 ----
test("1 ページ 5 個まで (テキストをまたいでも)", () => {
  const e = mkEntries({ あア: "1", いイ: "2", うウ: "3", えエ: "4", おオ: "5", かカ: "6", きキ: "7" });
  const st = W.newState();
  assert.equal(W.MAX_TERMS_PER_PAGE, 5);
  assert.equal(pick("あア いイ うウ", e, st).length, 3);
  assert.equal(pick("えエ おオ かカ きキ", e, st).length, 2);
  assert.equal(st.count, 5);
  assert.deepEqual(pick("かカ", e, st), []);
});

test("最長一致: キッズデザイン賞 の中の キッズデザイン には当てない", () => {
  const e = mkEntries({ キッズデザイン: "a", キッズデザイン賞: "b" });
  assert.deepEqual(pick("キッズデザイン賞を受賞", e), ["キッズデザイン賞"]);
});

// ---- 規則 2: 除外する要素 ----
test("見出し・summary・small・強調・リンク・data-wiki-skip・決まり文句の塊は除外", () => {
  for (const tag of ["H1", "H2", "H3", "H4", "H5", "H6", "SUMMARY", "SMALL", "STRONG", "B", "A", "CODE", "BUTTON"]) {
    assert.equal(W.isSkipElement(el(tag)), true, tag);
  }
  assert.equal(W.isSkipElement(el("P", { skipAttr: true })), true);
  for (const cls of ["score-axes-doc", "score-breakdown-doc", "development-stage-accordion", "where-to-buy-offline-note", "auto-gen-notice", "wiki-term"]) {
    assert.equal(W.isSkipElement(el("DIV", { cls })), true, cls);
  }
});

test("本文の段落・リストは除外しない", () => {
  for (const tag of ["P", "LI", "DIV", "SPAN", "TD", "UL", "EM"]) assert.equal(W.isSkipElement(el(tag, { cls: "post-content" })), false, tag);
  assert.equal(W.isSkipElement({ nodeType: 3 }), false);
});

// ---- 規則 6: 💡 そのものは残す ----
test("差し込むボタンに 💡 のアイコンが残っている", () => {
  assert.match(SRC, /className = 'wiki-term__icon'/);
  assert.match(SRC, /hint\.textContent = '💡'/);
});

// ---- 規則 1: 一覧から外した語 ----
test("cache は mapping の部分集合で、店名・一般語・別物の語を含まない", () => {
  const cache = JSON.parse(readFileSync(path.join(ROOT, "hugo/static/wiki_cache.json"), "utf8")).entries;
  const mapping = JSON.parse(readFileSync(path.join(ROOT, "hugo/data/wiki_mapping.json"), "utf8"));
  const dropped = [
    "トイザらス", "日本トイザらス", 'Toys"R"Us', "博品館", "HAKUHINKAN", "藤田屋",
    "感覚", "知育玩具", "コミュニケーション", "集中力", "思考力", "想像力", "創造力", "手先", "指先", "社会性",
    "LEMON", "レモン", "マルカ", "MARUKA", "パルス", "ブルーダー",
  ];
  for (const k of dropped) {
    assert.equal(k in cache, false, "cache に残っている: " + k);
    assert.equal(k in mapping, false, "mapping に残っている: " + k);
  }
  for (const k of Object.keys(cache)) assert.equal(k in mapping, true, "mapping に無い cache の語: " + k);
});
