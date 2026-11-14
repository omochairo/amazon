// tests/js/diagnosis.test.mjs — hugo/assets/js/diagnosis.js (診断の画面) の回帰防止。
// 推薦ロジックそのものは diagnosis_core.test.mjs 側。ここでは簡易 DOM の上で
// diagnosis_core.js + diagnosis.js を実際に動かし、画面の流れを確かめる。
//
// 旧 5 問版から引き継いだ観点:
//   A. /search.json の取得失敗で「集計中」のまま固まらず、エラー表示 + 手動リトライになる
//   B. 手動リトライで再取得に成功すれば結果が出る
//   C. 年齢ベスト10 から診断をやり直すと、見出しが「ベスト10」のまま残らない
//   D. エラー表示中に最初からやり直しても再取得を試みる
//   E. 結果カードにも ♡/🆚 トグルを付け直す
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const CORE = readFileSync(path.resolve(here, "../../hugo/assets/js/diagnosis_core.js"), "utf8");
const UI = readFileSync(path.resolve(here, "../../hugo/assets/js/diagnosis.js"), "utf8");
const LAYOUT = readFileSync(path.resolve(here, "../../hugo/layouts/_default/diagnosis.html"), "utf8");

class El {
  constructor(tag, id) {
    this.tagName = String(tag).toUpperCase();
    this.id = id || "";
    this.children = [];
    this.attrs = {};
    this.style = {};
    this.hidden = false;
    this.disabled = false;
    this.className = "";
    this._text = "";
    this._listeners = {};
  }
  get classList() {
    const self = this;
    const set = () => new Set(self.className.split(/\s+/).filter(Boolean));
    return {
      add(c) { const s = set(); s.add(c); self.className = [...s].join(" "); },
      remove(c) { const s = set(); s.delete(c); self.className = [...s].join(" "); },
      contains(c) { return set().has(c); },
    };
  }
  appendChild(c) { this.children.push(c); return c; }
  set innerHTML(v) { this.children = []; this._text = ""; }
  get innerHTML() { return ""; }
  set textContent(v) { this.children = []; this._text = String(v); }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  addEventListener(t, fn) { (this._listeners[t] ||= []).push(fn); }
  click() { if (this.disabled) return; (this._listeners.click || []).forEach((f) => f({})); }
  focus() {}
}

// テンプレートに実在する id だけで簡易 DOM を組む (テンプレートと JS の id のずれも検出する)。
const IDS = [...LAYOUT.matchAll(/id="(dx-[a-z0-9-]+)"/g)].map((m) => m[1]);

const tick = (ms = 5) => new Promise((r) => setTimeout(r, ms));

function boot({ fetchImpl, search = "", saved = null, compare, favorites, gtag, amazonTag = "test-tag", sessionData = {}, sessionThrows = false } = {}) {
  const byId = {};
  for (const id of IDS) byId[id] = new El("div", id);
  if (amazonTag) byId["dx-root"].setAttribute("data-amazon-tag", amazonTag);
  // テンプレートの初期状態で hidden のもの
  for (const id of ["dx-back", "dx-multi-next", "dx-loading", "dx-result", "dx-note", "dx-error", "dx-grid-title", "dx-more"]) {
    byId[id].hidden = true;
  }
  const storage = { omcha_last_diagnosis: saved ? JSON.stringify(saved) : null };
  // navi-brain#166 F17: 「きょうだい・友だちも診断する」の比較は sessionStorage に持つ
  const session = { ...sessionData };
  const location = { search, pathname: "/diagnosis/", origin: "https://example.com", href: "https://example.com/diagnosis/" + search };
  const history = { urls: [], replaceState(_s, _t, url) { this.urls.push(url); } };
  const docListeners = {};
  const document = {
    readyState: "complete",
    title: "診断",
    getElementById: (id) => byId[id] || null,
    createElement: (tag) => new El(tag),
    addEventListener(t, fn) { (docListeners[t] ||= []).push(fn); },
  };
  const window = {
    document, location, history,
    innerWidth: 1280,
    // reduce-motion 扱いにして演出の待ち時間を 0 にする
    matchMedia: () => ({ matches: true }),
    navigator: {},
    sessionStorage: sessionThrows ? {
      getItem: () => { throw new Error("SecurityError"); },
      setItem: () => { throw new Error("SecurityError"); },
      removeItem: () => { throw new Error("SecurityError"); },
    } : {
      getItem: (k) => (k in session ? session[k] : null),
      setItem: (k, v) => { session[k] = String(v); },
      removeItem: (k) => { delete session[k]; },
    },
    OmochaCompare: compare,
    OmochaFavorites: favorites,
    gtag,
    OmochaUtils: { renderProductCard: (item) => { const e = new El("a"); e.className = "product-card"; e.textContent = item.product_name || item.title || ""; e.item = item; return e; } },
  };
  const sandbox = {
    window, document, location, history,
    URLSearchParams, Promise, JSON, Math, Date, Array, String, Number, console,
    setTimeout, clearTimeout,
    localStorage: { getItem: (k) => storage[k] ?? null, setItem: (k, v) => { storage[k] = v; } },
    fetch: fetchImpl || (async () => ({ json: async () => ITEMS })),
  };
  window.window = window;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(CORE, sandbox);
  window.OmochaDiagnosis = sandbox.OmochaDiagnosis || window.OmochaDiagnosis;
  vm.runInContext(UI, sandbox);

  const $ = (id) => byId[id];
  const options = () => $("dx-options").children;
  async function pick(label) {
    const btn = options().find((b) => b.children[1] && b.children[1].textContent === label);
    assert.ok(btn, `選択肢「${label}」がある (いまの設問: ${$("dx-q-title").textContent})`);
    btn.click();
    await tick();
  }
  return { $, options, pick, storage, session, history, docListeners };
}

const ITEMS = [
  { title: "ブロックA", product_name: "ブロックA", brand: "A", permalink: "/products/b0000000a1/", tags: ["ブロック"], age_min_months: 36, price_amazon: 2800, ivs_score_100: 80, ivs_axes: { safety: 4.5 }, card_reason: "積み重ねて遊べます" },
  { title: "キッチンB", product_name: "キッチンB", brand: "B", permalink: "/products/b0000000b1/", tags: ["ままごと"], age_min_months: 36, price_amazon: 4500, ivs_score_100: 75, ivs_axes: { safety: 4.0 } },
  { title: "ラトルC", product_name: "ラトルC", brand: "C", permalink: "/products/b0000000c1/", tags: ["ラトル", "出産祝い"], age_min_months: 0, price_amazon: 1500, ivs_score_100: 70, ivs_axes: { safety: 4.8 } },
  { title: "パズルD", product_name: "パズルD", brand: "D", permalink: "/products/b0000000d1/", tags: ["パズル"], age_min_months: 48, price_amazon: 1800, ivs_score_100: 85, ivs_axes: {} },
  { title: "トミカE", product_name: "トミカE", brand: "E", permalink: "/products/b0000000e1/", tags: ["トミカ"], age_min_months: 36, price_amazon: 900, ivs_score_100: 65, ivs_axes: {} },
  { title: "ロボF", product_name: "ロボF", brand: "F", permalink: "/products/b0000000f1/", tags: ["プログラミング"], age_min_months: 72, price_amazon: 9000, ivs_score_100: 90, ivs_axes: {} },
];

async function answerChild3y(ui) {
  await ui.pick("わが子に");
  await ui.pick("3歳");
  await ui.pick("つくる・組み立てる");
  await ui.pick("〜3,000円");
  await ui.pick("安全・安心");
}

test("わが子: 6 問 (持っているものは任意) で結果が出て、理由・チップ・保存・URL が揃う", async () => {
  const ui = boot();
  assert.equal(ui.$("dx-q-title").textContent, "だれのためのおもちゃ？");
  assert.equal(ui.$("dx-progress-text").textContent, "Q1 / 6");
  await answerChild3y(ui);
  assert.equal(ui.$("dx-q-title").textContent, "もう持っているおもちゃは？");
  assert.equal(ui.$("dx-remain").textContent, "ラスト！");
  assert.equal(ui.$("dx-multi-next").hidden, false);
  ui.$("dx-multi-next").click();
  await tick(20);

  assert.equal(ui.$("dx-result").hidden, false);
  assert.equal(ui.$("dx-quiz").hidden, true);
  assert.equal(ui.$("dx-persona-title").textContent, "つくって発見タイプ");
  assert.ok(ui.$("dx-persona-catch").textContent);
  assert.equal(ui.$("dx-persona-strengths-list").children.length, 3);
  assert.ok(ui.$("dx-persona-pair").textContent);
  assert.match(ui.$("dx-share-line").getAttribute("href"), /text=/);
  assert.match(ui.$("dx-share-x").getAttribute("href"), /url=/);
  const top = ui.$("dx-top").children[0];
  assert.equal(top.children[1].textContent, "ブロックA");
  assert.match(top.children.find((c) => c.className === "dx-reasons").textContent, /つくる・組み立てる/, "1 位に選んだ理由が付く");
  const buy = top.children.find((c) => c.className === "dx-buy dx-buy-amazon");
  assert.ok(buy);
  assert.equal(buy.getAttribute("href"), "https://www.amazon.co.jp/dp/B0000000A1/?tag=test-tag");
  assert.equal(buy.getAttribute("data-cta-slot"), "diagnosis");
  // card_reason はカード本体 (product-card.js) が出す。理由リストには重ねて出さない
  assert.ok(!top.children.find((c) => c.className === "dx-reasons").textContent.includes("積み重ねて遊べます"));
  assert.equal(ui.$("dx-chips").children.length, 5);
  const saved = JSON.parse(ui.storage.omcha_last_diagnosis);
  assert.equal(saved.v, 2);
  assert.equal(saved.answers.interest, "build");
  assert.equal(ui.history.urls.at(-1), "/diagnosis/?w=child&a=3y&i=build&b=3000&p=safety");
  assert.ok(ui.$("dx-root").classList.contains("dx--wide"), "結果は全幅");
});

test("Amazonボタンはタグと価格が揃った商品だけに出る", async () => {
  async function resultWith(options) {
    const ui = boot(options);
    await answerChild3y(ui);
    ui.$("dx-multi-next").click();
    await tick(20);
    return ui.$("dx-top").children[0];
  }
  const noTag = await resultWith({ amazonTag: "" });
  assert.ok(!noTag.children.some((c) => c.className === "dx-buy dx-buy-amazon"));
  const noPriceItem = { ...ITEMS[0], price_amazon: 0, price_rakuten: 2800 };
  const noPrice = await resultWith({ fetchImpl: async () => ({ json: async () => [noPriceItem] }) });
  assert.ok(!noPrice.children.some((c) => c.className === "dx-buy dx-buy-amazon"));
});

test("出産祝い: 年齢を聞かずに 4 問で終わる", async () => {
  const ui = boot();
  await ui.pick("出産祝いに");
  assert.equal(ui.$("dx-progress-text").textContent, "Q2 / 4");
  assert.equal(ui.$("dx-q-title").textContent, "どんなものを贈りたい？");
  await ui.pick("おまかせ");
  await ui.pick("〜3,000円");
  await ui.pick("安全・安心");
  await tick(20);
  assert.equal(ui.$("dx-result").hidden, false);
  assert.equal(ui.$("dx-persona-title").textContent, "はじめてのおもちゃ");
  assert.equal(ui.$("dx-top").children[0].children[1].textContent, "ラトルC");
});

test("プレゼント: 「持っているもの」を聞かずに結果へ進む", async () => {
  const ui = boot();
  await ui.pick("プレゼントに");
  await ui.pick("3歳");
  await ui.pick("おまかせ");
  await ui.pick("こだわらない");
  await ui.pick("バランスよく");
  await tick(20);
  assert.equal(ui.$("dx-result").hidden, false);
  assert.equal(ui.$("dx-share").hidden, false);
});

test("戻って年齢を下げると、その年齢では選べない「夢中な遊び」は選び直しになる", async () => {
  const ui = boot();
  await ui.pick("わが子に");
  await ui.pick("4歳");
  await ui.pick("しくみ・科学");
  ui.$("dx-back").click(); // → interest
  ui.$("dx-back").click(); // → age
  assert.equal(ui.$("dx-q-title").textContent, "お子さんの年齢は？");
  await ui.pick("1歳");
  const labels = ui.options().map((b) => b.children[1].textContent);
  assert.ok(!labels.includes("しくみ・科学"));
  assert.ok(ui.options().every((b) => b.getAttribute("aria-checked") === "false"), "前の選択は残らない");
});

test("結果のチップから 1 問だけ直すと、そのまま結果に戻る", async () => {
  const ui = boot();
  await answerChild3y(ui);
  ui.$("dx-multi-next").click();
  await tick(20);
  const budgetChip = ui.$("dx-chips").children.find((c) => c.textContent.includes("3,000円"));
  budgetChip.click();
  assert.equal(ui.$("dx-quiz").hidden, false);
  assert.equal(ui.$("dx-q-title").textContent, "ご予算は？");
  await ui.pick("〜10,000円");
  await tick(20);
  assert.equal(ui.$("dx-result").hidden, false);
  assert.ok(ui.$("dx-chips").children.some((c) => c.textContent.includes("10,000円")));
});

test("共有リンク (?w=..) を開くと設問を飛ばして結果を出す", async () => {
  const ui = boot({ search: "?w=gift&a=3y&i=pretend&b=5000&p=safety" });
  await tick(20);
  assert.equal(ui.$("dx-result").hidden, false);
  assert.equal(ui.$("dx-persona-title").textContent, "なりきり名人タイプ");
  assert.equal(ui.$("dx-top").children[0].children[1].textContent, "キッチンB");
});

test("壊れた共有リンクは 1 問目から始める", async () => {
  const ui = boot({ search: "?w=gift&a=99y" });
  await tick(20);
  assert.equal(ui.$("dx-quiz").hidden, false);
  assert.equal(ui.$("dx-q-title").textContent, "だれのためのおもちゃ？");
});

test("?restore=1: 旧 5 問の保存からも結果を再表示できる", async () => {
  const ui = boot({ search: "?restore=1", saved: { answers: { q1: "3-4", q2: "social", q3: "indoor", q4: "8000", q5: "few" } } });
  await tick(20);
  assert.equal(ui.$("dx-result").hidden, false);
  assert.equal(ui.$("dx-persona-title").textContent, "なりきり名人タイプ");
});

test("A: 通信失敗で固まらず、エラー表示 + 手動リトライボタンを出す", async () => {
  let calls = 0;
  const ui = boot({ fetchImpl: async () => { calls++; throw new Error("network"); } });
  await tick();
  await answerChild3y(ui);
  ui.$("dx-multi-next").click();
  await tick(20);
  assert.equal(ui.$("dx-persona-title").textContent, "おもちゃ情報の読み込みに失敗しました");
  assert.equal(ui.$("dx-error").hidden, false);
  assert.equal(ui.$("dx-share").hidden, true);
  for (const id of ["dx-share-line", "dx-share-x", "dx-compare-start", "dx-types", "dx-persona-strengths", "dx-persona-pair"]) {
    assert.equal(ui.$(id).hidden, true, `エラー表示に ${id} を残さない (navi-brain#166 F17)`);
  }
  assert.ok(calls >= 2, "結果を出すときに再取得を試みている");
});

test("B: 手動リトライで再取得に成功すれば結果が出る", async () => {
  let attempt = 0;
  const ui = boot({ fetchImpl: async () => { attempt++; if (attempt <= 2) throw new Error("network"); return { json: async () => ITEMS }; } });
  await tick();
  await answerChild3y(ui);
  ui.$("dx-multi-next").click();
  await tick(20);
  assert.equal(ui.$("dx-error").hidden, false);
  ui.$("dx-error-retry").click();
  assert.equal(ui.$("dx-error-retry").disabled, true, "連打できない");
  await tick(20);
  assert.equal(ui.$("dx-persona-title").textContent, "つくって発見タイプ");
  assert.equal(ui.$("dx-error").hidden, true);
});

test("C: 年齢ベスト10 (?age=0-1) から診断をやり直すと、見出しがベスト10のまま残らない", async () => {
  const ui = boot({ search: "?age=0-1" });
  await tick(20);
  assert.equal(ui.$("dx-persona-title").textContent, "0〜1歳のベスト10");
  const names = ui.$("dx-top").children.concat(ui.$("dx-grid").children).map((p) => p.children[1].textContent);
  assert.deepEqual(names, ["ラトルC"], "3歳〜の商品は 0〜1歳のベスト10 に入らない");
  ui.$("dx-retry").click();
  assert.equal(ui.$("dx-quiz").hidden, false);
  assert.equal(ui.history.urls.at(-1), "/diagnosis/", "?age= を消してリロードで巻き戻らないようにする");
  await answerChild3y(ui);
  ui.$("dx-multi-next").click();
  await tick(20);
  assert.equal(ui.$("dx-persona-title").textContent, "つくって発見タイプ");
  assert.equal(ui.$("dx-result-eyebrow").textContent, "診断結果");
});

test("年齢タイムラインの 9 段階 (?age=6-7y) でも年齢別ベスト10 を出す", async () => {
  const ui = boot({ search: "?age=6-7y" });
  await tick(20);
  assert.equal(ui.$("dx-result-eyebrow").textContent, "年齢別ベスト10");
  assert.equal(ui.$("dx-persona-title").textContent, "6〜7歳のベスト10");
  assert.match(ui.$("dx-age-tip").textContent, /^この時期のポイント: /);
});

test("年齢別ベスト10: 10 件ずつ「ほかのおもちゃも見る」で足し、順位は付けず、診断に戻ると元の表示に戻る", async () => {
  const genres = ["ブロック", "ままごと", "パズル", "お絵かき", "ひらがな", "木琴", "ボール", "プラレール", "トミカ", "ミニカー", "電車", "新幹線"];
  const many = genres.map((t, i) => ({
    title: "T" + i, product_name: t + "Toy" + String.fromCharCode(65 + i) + "xyz", brand: "B" + i,
    permalink: "/products/b00000000" + i + "/", tags: [t], age_min_months: 36, price_amazon: 2000, ivs_score_100: 90 - i,
  }));
  const events = [];
  const ui = boot({ search: "?age=3y", fetchImpl: async () => ({ ok: true, json: async () => many }), gtag: (t, n, p) => events.push({ n, p }) });
  await tick(20);
  const ranks = () => ui.$("dx-top").children.concat(ui.$("dx-grid").children).map((p) => p.children[0]);
  assert.equal(ranks().length, 10);
  assert.equal(ranks()[0].textContent, "✨ ピックアップ");
  assert.ok(ranks().slice(1).every((r) => r.hidden), "2 件目以降に順位の札を出さない");
  assert.equal(ui.$("dx-more").textContent, "🔀 ほかのおもちゃも見る");
  assert.equal(ui.$("dx-more").hidden, false);
  ui.$("dx-more").click();
  assert.equal(ranks().length, 12);
  assert.equal(ui.$("dx-more").hidden, true);
  assert.deepEqual(JSON.parse(JSON.stringify(events.at(-1))), { n: "diagnosis_more", p: { view: "age_best", shown: 12 } });

  ui.$("dx-retry").click();
  await answerChild3y(ui);
  ui.$("dx-multi-next").click();
  await tick(20);
  assert.equal(ui.$("dx-more").textContent, "もっと見る");
  assert.equal(ui.$("dx-top").children[0].children[0].textContent, "🥇 いちばんのおすすめ");
});

test("D: エラー表示中に最初からやり直しても再取得を試みる", async () => {
  let attempt = 0;
  const ui = boot({ fetchImpl: async () => { attempt++; if (attempt <= 2) throw new Error("network"); return { json: async () => ITEMS }; } });
  await tick();
  await answerChild3y(ui);
  ui.$("dx-multi-next").click();
  await tick(20);
  assert.equal(ui.$("dx-error").hidden, false);
  ui.$("dx-retry").click();
  await answerChild3y(ui);
  ui.$("dx-multi-next").click();
  await tick(20);
  assert.equal(ui.$("dx-persona-title").textContent, "つくって発見タイプ");
});

test("E: 結果カードにも ♡/🆚 トグルを付け直す (1 位と候補の両方)", async () => {
  const compareRoots = [];
  const favRoots = [];
  const ui = boot({
    compare: { mountToggles: (r) => compareRoots.push(r) },
    favorites: { mountToggles: (r) => favRoots.push(r) },
  });
  await answerChild3y(ui);
  ui.$("dx-multi-next").click();
  await tick(20);
  assert.deepEqual(compareRoots.map((r) => r.id), ["dx-top", "dx-grid"]);
  assert.deepEqual(favRoots.map((r) => r.id), ["dx-top", "dx-grid"]);
});

test("数字キーで選べる (PC)", async () => {
  const ui = boot();
  const keydown = ui.docListeners.keydown[0];
  keydown({ key: "2", target: { tagName: "BODY" }, preventDefault() {} });
  await tick();
  assert.equal(ui.$("dx-q-title").textContent, "贈るお子さんの年齢は？");
});

test("HTTP エラーや配列でない応答を空のカタログとして握らず、エラー表示 → 再試行できる (agy レビュー指摘)", async () => {
  let attempt = 0;
  const ui = boot({ fetchImpl: async () => {
    attempt++;
    if (attempt === 1) return { ok: false, status: 503, json: async () => ({ error: "busy" }) };
    if (attempt === 2) return { ok: true, json: async () => ({ error: "not array" }) };
    return { ok: true, json: async () => ITEMS };
  } });
  await tick();
  await answerChild3y(ui);
  ui.$("dx-multi-next").click();
  await tick(20);
  assert.equal(ui.$("dx-error").hidden, false, "503 / 配列でない応答はエラー扱い");
  ui.$("dx-error-retry").click();
  await tick(20);
  assert.equal(ui.$("dx-persona-title").textContent, "つくって発見タイプ");
});

test("チップから「だれに」をプレゼント → わが子に変えると、増えた「持っているもの」も聞く (agy レビュー指摘)", async () => {
  const ui = boot({ search: "?w=gift&a=3y&i=build&b=3000&p=safety" });
  await tick(20);
  ui.$("dx-chips").children.find((c) => c.textContent.includes("プレゼント")).click();
  await ui.pick("わが子に");
  assert.equal(ui.$("dx-quiz").hidden, false, "結果へ直行しない");
  for (let i = 0; i < 4; i++) {
    const checked = ui.options().find((b) => b.getAttribute("aria-checked") === "true");
    checked.click(); // 前の回答のまま進む
    await tick();
  }
  assert.equal(ui.$("dx-q-title").textContent, "もう持っているおもちゃは？");
});

test("単一選択では選び直したときに前の選択の aria-checked を外す (agy レビュー指摘)", async () => {
  const ui = boot();
  await ui.pick("わが子に");
  ui.$("dx-back").click();
  const before = ui.options().find((b) => b.children[1].textContent === "わが子に");
  assert.equal(before.getAttribute("aria-checked"), "true");
  const other = ui.options().find((b) => b.children[1].textContent === "プレゼントに");
  other.click();
  assert.equal(before.getAttribute("aria-checked"), "false");
  assert.equal(other.getAttribute("aria-checked"), "true");
});

test("「もっと見る」は描画済みのカードを作り直さずに足す (agy レビュー指摘)", async () => {
  const many = Array.from({ length: 14 }, (_, i) => ({
    title: "T" + i, product_name: "Toy" + String.fromCharCode(65 + i) + "xyz", brand: "B" + i,
    permalink: "/products/b00000000" + i + "/", tags: ["ブロック"], age_min_months: 36, price_amazon: 2000, ivs_score_100: 90 - i,
  }));
  const ui = boot({ fetchImpl: async () => ({ ok: true, json: async () => many }) });
  await answerChild3y(ui);
  ui.$("dx-multi-next").click();
  await tick(20);
  const firstCard = ui.$("dx-grid").children[0];
  assert.equal(ui.$("dx-grid").children.length, 5);
  assert.equal(ui.$("dx-more").hidden, false);
  ui.$("dx-more").click();
  assert.equal(ui.$("dx-grid").children[0], firstCard, "既存のカードは同じ要素のまま");
  assert.equal(ui.$("dx-grid").children.length, 11);
  assert.equal(ui.$("dx-more").hidden, true);
});

test("GA4: 開始・完了 (回答の選択肢と経路)・順位つきクリック・答え直しを送る", async () => {
  const events = [];
  const ui = boot({ gtag: (type, name, params) => events.push({ type, name, params }) });
  await answerChild3y(ui);
  ui.$("dx-multi-next").click();
  await tick(20);
  assert.deepEqual(events.map((e) => e.name), ["diagnosis_start", "diagnosis_complete"]);
  assert.ok(events.every((e) => e.type === "event"));
  const done = events[1].params;
  assert.equal(done.entry, "quiz");
  assert.equal(done.who, "child");
  assert.equal(done.age, "3y");
  assert.equal(done.interest, "build");
  assert.equal(done.relaxed, "age", "テスト用の 6 商品では条件緩和が年齢段階まで進む");
  assert.equal(done.owned_count, 0);

  ui.$("dx-top").children[0].children[1].click();
  assert.deepEqual(JSON.parse(JSON.stringify(events.at(-1))), { type: "event", name: "diagnosis_pick_click", params: { rank: 1, view: "diagnosis" } });

  ui.$("dx-chips").children.find((c) => c.textContent.includes("3,000円")).click();
  await ui.pick("〜10,000円");
  await tick(20);
  const names = events.map((e) => e.name);
  assert.deepEqual(names.slice(-2), ["diagnosis_edit", "diagnosis_complete"]);
  assert.equal(events.at(-1).params.entry, "edit");
  assert.equal(names.filter((n) => n === "diagnosis_start").length, 1, "開始は 1 回だけ");
});

test("GA4: 共有リンクと年齢別ベスト10 は経路を区別して送る", async () => {
  const events = [];
  const shared = boot({ search: "?w=gift&a=3y&i=pretend&b=5000&p=safety", gtag: (t, n, p) => events.push({ n, p }) });
  await tick(20);
  assert.equal(events.at(-1).n, "diagnosis_complete");
  assert.equal(events.at(-1).p.entry, "shared");

  const ev2 = [];
  const age = boot({ search: "?age=0-1", gtag: (t, n, p) => ev2.push({ n, p }) });
  await tick(20);
  assert.deepEqual(JSON.parse(JSON.stringify(ev2)), [{ n: "diagnosis_age_best", p: { band: "0-1" } }]);
  age.$("dx-top").children[0].children[1].click();
  assert.equal(ev2.at(-1).p.view, "age_best");
});

// ------------------------------------------------------------ navi-brain#166 F17
async function answerChildVehicle3y(ui) {
  await ui.pick("わが子に");
  await ui.pick("3歳");
  await ui.pick("のりもの・うごくもの");
  await ui.pick("〜3,000円");
  await ui.pick("安全・安心");
  ui.$("dx-multi-next").click();
  await tick(20);
}

test("F17: 結果にタイプの一言・得意なこと・相性・全タイプ一覧が出て、完了イベントにタイプが付く", async () => {
  const events = [];
  const ui = boot({ gtag: (t, n, p) => events.push({ n, p }) });
  await answerChild3y(ui);
  ui.$("dx-multi-next").click();
  await tick(20);
  assert.equal(ui.$("dx-persona-strengths").hidden, false);
  assert.match(ui.$("dx-persona-pair").textContent, /^一緒に遊ぶと盛り上がるのは「.+」。/);
  assert.equal(ui.$("dx-types").hidden, false);
  const items = ui.$("dx-types-list").children;
  assert.ok(items.length >= 10, "出産祝い以外の全タイプ");
  const cur = items.filter((li) => li.className === "is-current");
  assert.equal(cur.length, 1);
  assert.equal(cur[0].getAttribute("aria-current"), "true");
  assert.match(cur[0].textContent, /つくって発見タイプ/);
  assert.equal(ui.$("dx-compare").hidden, true, "比較の相手がいなければ出さない");
  assert.equal(events.find((e) => e.n === "diagnosis_complete").p.persona, "build");
  ui.$("dx-share-line").click();
  assert.deepEqual(JSON.parse(JSON.stringify(events.at(-1))), { n: "diagnosis_share", p: { method: "line", persona: "build" } });
});

test("F17: 「きょうだい・友だちも診断する」で 2 人目を診断すると、さっきの子と並べて相性を出す。最初からやり直すと消える", async () => {
  const events = [];
  const ui = boot({ gtag: (t, n, p) => events.push({ n, p }) });
  await answerChild3y(ui);
  ui.$("dx-multi-next").click();
  await tick(20);
  ui.$("dx-compare-start").click();
  assert.equal(events.at(-1).n, "diagnosis_compare_start");
  assert.equal(events.at(-1).p.persona, "build");
  assert.equal(ui.$("dx-quiz").hidden, false, "1 問目に戻る");
  assert.equal(ui.$("dx-q-title").textContent, "だれのためのおもちゃ？");
  assert.equal(JSON.parse(ui.session.omcha_dx_compare).key, "build");

  await answerChildVehicle3y(ui);
  assert.equal(ui.$("dx-compare").hidden, false);
  const text = ui.$("dx-compare").textContent;
  assert.match(text, /さっきの子/);
  assert.match(text, /つくって発見タイプ/);
  assert.match(text, new RegExp(ui.$("dx-persona-title").textContent));
  assert.match(text, /相性ぴったり/, "build の相性の相手は vehicle");
  const link = ui.$("dx-compare").children[1].children[0].children[1];
  assert.equal(link.getAttribute("href"), "https://example.com/diagnosis/?w=child&a=3y&i=build&b=3000&p=safety", "さっきの子の結果へ戻れる");

  ui.$("dx-retry").click();
  assert.equal(ui.session.omcha_dx_compare, undefined);
  await answerChildVehicle3y(ui);
  assert.equal(ui.$("dx-compare").hidden, true);
});

test("F17: 同じ結果のまま比べても比較枠は出さない (自分どうしを並べない)", async () => {
  const ui = boot();
  await answerChild3y(ui);
  ui.$("dx-multi-next").click();
  await tick(20);
  ui.$("dx-compare-start").click();
  await answerChild3y(ui);
  ui.$("dx-multi-next").click();
  await tick(20);
  assert.equal(ui.$("dx-compare").hidden, true);
});

test("F17: 年齢別ベスト10 ではタイプの飾り (得意なこと・相性・共有・比較・一覧) を出さない", async () => {
  // 簡易 DOM はテンプレートの hidden を引き継がない (全部 hidden=false で始まる) ので、
  // ここで hidden になっていれば showAgeBest が明示的に隠している
  const age = boot({ search: "?age=0-1" });
  await tick(20);
  for (const id of ["dx-share-line", "dx-share-x", "dx-compare-start", "dx-compare", "dx-types", "dx-persona-strengths", "dx-persona-pair"]) {
    assert.equal(age.$(id).hidden, true, id);
  }
  assert.equal(age.$("dx-persona-catch").textContent, "");
});

test("F17: 開き直し (共有リンク・?restore=1) では、前のタブ操作で残った比較の相手を持ち越さない", async () => {
  const stale = { omcha_dx_compare: JSON.stringify({ key: "build", emoji: "🧱", title: "つくって発見タイプ", url: "https://example.com/diagnosis/?w=child&a=3y&i=build&b=3000&p=safety" }) };
  const shared = boot({ search: "?w=child&a=3y&i=vehicle&b=3000&p=safety", sessionData: stale });
  await tick(20);
  assert.equal(shared.$("dx-persona-title").textContent, "のりもの博士タイプ");
  assert.equal(shared.$("dx-compare").hidden, true);
  assert.equal(shared.session.omcha_dx_compare, undefined, "ページを開いた時点で消す");
  // 開き直したあとチップで 1 問直しても、比較枠は出てこない
  shared.$("dx-chips").children.find((c) => c.textContent.includes("3,000円")).click();
  await shared.pick("〜10,000円");
  await tick(20);
  assert.equal(shared.$("dx-compare").hidden, true);

  const savedV2 = { v: 2, answers: { who: "child", age: "3y", interest: "vehicle", budget: "3000", priority: "safety", owned: [] } };
  const restored = boot({ search: "?restore=1", saved: savedV2, sessionData: stale });
  await tick(20);
  assert.equal(restored.$("dx-persona-title").textContent, "のりもの博士タイプ");
  assert.equal(restored.$("dx-compare").hidden, true);
});

test("F17: 年齢別ベスト10 から診断に戻ると、タイプの飾りが出し直される", async () => {
  const ui = boot({ search: "?age=0-1" });
  await tick(20);
  assert.equal(ui.$("dx-types").hidden, true);
  ui.$("dx-retry").click();
  await answerChild3y(ui);
  ui.$("dx-multi-next").click();
  await tick(20);
  for (const id of ["dx-share-line", "dx-share-x", "dx-compare-start", "dx-types", "dx-persona-strengths", "dx-persona-pair"]) {
    assert.equal(ui.$(id).hidden, false, id);
  }
  assert.ok(ui.$("dx-persona-catch").textContent);
});

test("F17: sessionStorage が使えない環境でも結果が出て、比較ボタンを押しても止まらない", async () => {
  const ui = boot({ sessionThrows: true });
  await answerChild3y(ui);
  ui.$("dx-multi-next").click();
  await tick(20);
  assert.equal(ui.$("dx-persona-title").textContent, "つくって発見タイプ");
  ui.$("dx-compare-start").click();
  assert.equal(ui.$("dx-quiz").hidden, false);
  await answerChildVehicle3y(ui);
  assert.equal(ui.$("dx-persona-title").textContent, "のりもの博士タイプ");
  assert.equal(ui.$("dx-compare").hidden, true);
});

test("F17: 出産祝いは「選ぶポイント」を出し、相性とタイプ一覧は出さない", async () => {
  const ui = boot();
  await ui.pick("出産祝いに");
  await ui.pick("おまかせ");
  await ui.pick("〜3,000円");
  await ui.pick("安全・安心");
  await tick(20);
  assert.equal(ui.$("dx-result").hidden, false);
  assert.equal(ui.$("dx-persona-title").textContent, "はじめてのおもちゃ");
  assert.equal(ui.$("dx-persona-strengths-label").textContent, "選ぶポイント");
  assert.equal(ui.$("dx-persona-strengths").hidden, false);
  assert.equal(ui.$("dx-persona-pair").hidden, true);
  assert.equal(ui.$("dx-types").hidden, true);
});
