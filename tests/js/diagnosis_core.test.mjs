// tests/js/diagnosis_core.test.mjs — hugo/assets/js/diagnosis_core.js (診断の設問・推薦ロジック)。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import path from "node:path";

const SRC = readFileSync(
  path.resolve(path.dirname(fileURLToPath(import.meta.url)),
               "../../hugo/assets/js/diagnosis_core.js"), "utf8");

function load() {
  const sb = {};
  sb.window = sb;
  vm.createContext(sb);
  vm.runInContext(SRC, sb);
  return sb.OmochaDiagnosis;
}
const D = load();

function item(o) {
  return {
    title: o.name, product_name: o.name, permalink: "/products/" + o.name + "/",
    brand: o.brand || o.name, tags: o.tags || [], age_min_months: o.age ?? 36,
    price_amazon: o.price ?? 2000, price_rakuten: 0, price_yahoo: 0,
    ivs_score_100: o.score ?? 70, ivs_axes: o.axes || {},
  };
}

function answers(o) {
  return { ...D.emptyAnswers(), who: "child", age: "3y", interest: "any", budget: "any", priority: "balance", ...o };
}

// vm の別 realm で作られた配列は node:assert の deepEqual で型が一致しないため、JSON で比べる
const plain = (v) => JSON.parse(JSON.stringify(v));

test("分岐: プレゼントは「持っているもの」を聞かず、出産祝いは年齢も聞かない", () => {
  assert.deepEqual(plain(D.stepsFor({ who: "child" })), ["who", "age", "interest", "budget", "priority", "owned"]);
  assert.deepEqual(plain(D.stepsFor({ who: "gift" })), ["who", "age", "interest", "budget", "priority"]);
  assert.deepEqual(plain(D.stepsFor({ who: "birth" })), ["who", "interest", "budget", "priority"]);
});

test("「いま夢中な遊び」の選択肢は年齢で絞られる (1歳にプログラミング、4歳に歯固めを出さない)", () => {
  const v = (age) => D.question("interest", { who: "child", age }).options.map((o) => o.value);
  assert.ok(!v("1y").includes("science"));
  assert.ok(v("1y").includes("sense"));
  assert.ok(v("4y").includes("science"));
  assert.ok(!v("4y").includes("sense"));
  assert.ok(v("1y").includes("any") && v("8y").includes("any"), "おまかせは常にある");
});

test("安全: 対象年齢がお子さんの年齢を超える商品は、どの緩和段階でも出さない", () => {
  // 条件に合う商品が 1 つも無く、緩和が最後の段階まで進む状況を作る
  const items = [
    item({ name: "small-parts", age: 36, tags: ["ブロック"], score: 99 }),
    item({ name: "ok-baby", age: 6, tags: ["ラトル"], score: 40 }),
  ];
  const r = D.recommend(items, answers({ age: "1y", interest: "build", budget: "3000" }));
  const names = r.picks.map((p) => p.item.product_name);
  assert.ok(!names.includes("small-parts"), "3歳〜の商品を1歳に出さない");
  assert.ok(names.includes("ok-baby"));
});

test("予算: 上限を超える商品は通常は出さず、足りないときだけ 3 割増しまで加えて注記する", () => {
  const items = [
    item({ name: "in", price: 2900 }),
    item({ name: "slightly-over", price: 3500 }),
    item({ name: "way-over", price: 9000 }),
  ];
  const r = D.recommend(items, answers({ budget: "3000" }));
  const names = r.picks.map((p) => p.item.product_name);
  assert.equal(names[0], "in");
  assert.ok(names.includes("slightly-over"));
  assert.ok(!names.includes("way-over"));
  assert.equal(r.relaxed, "price");
  assert.ok(D.RELAX_NOTES.price);
  const over = r.picks.find((p) => p.item.product_name === "slightly-over");
  assert.ok(over.reasons.some((x) => /予算を少しこえます/.test(x.text)));
});

test("好きな遊び: タグは部分一致で照合する (「ジグソーパズル」も「考える・ゲーム」に当たる)", () => {
  const items = [
    item({ name: "jigsaw", tags: ["ジグソーパズル"], score: 50 }),
    item({ name: "other", tags: ["ぬいぐるみ"], score: 95 }),
  ];
  const r = D.recommend(items, answers({ interest: "think" }));
  assert.equal(r.picks[0].item.product_name, "jigsaw");
  assert.ok(r.picks[0].reasons.some((x) => x.kind === "interest"));
});

test("持っているもの: 選んだ種類は下げる。ただし好きな遊びと同じ種類は「買い足し」なので下げない", () => {
  const items = [
    item({ name: "blocks", tags: ["ブロック"], score: 90 }),
    item({ name: "kitchen", tags: ["ままごと"], score: 60 }),
  ];
  const owned = D.recommend(items, answers({ interest: "any", owned: ["build"] }));
  assert.equal(owned.picks[0].item.product_name, "kitchen");
  const same = D.recommend(items, answers({ interest: "build", owned: ["build"] }));
  assert.equal(same.picks[0].item.product_name, "blocks");
});

test("大事にしたいこと: 安全重視なら安全性の軸が高い商品が上に来て、理由にも出る", () => {
  const items = [
    item({ name: "safe", score: 70, axes: { safety: 4.8, education: 3.0 } }),
    item({ name: "edu", score: 70, axes: { safety: 3.0, education: 4.8 } }),
  ];
  const s = D.recommend(items, answers({ priority: "safety" }));
  assert.equal(s.picks[0].item.product_name, "safe");
  assert.ok(s.picks[0].reasons.some((x) => /安全性 4\.8/.test(x.text)));
  const e = D.recommend(items, answers({ priority: "education" }));
  assert.equal(e.picks[0].item.product_name, "edu");
});

test("同じ商品の別出品はまとめ、同じブランドは 2 件までに散らす", () => {
  const items = [
    item({ name: "トイローヤル たたいてベビードラム", brand: "トイローヤル", score: 90 }),
    item({ name: "たたいて ベビードラム", brand: "トイローヤル", score: 89 }),
    item({ name: "A1", brand: "A", score: 88 }),
    item({ name: "A2", brand: "A", score: 87 }),
    item({ name: "A3", brand: "A", score: 86 }),
    item({ name: "B1", brand: "B", score: 10 }),
  ];
  const names = D.recommend(items, answers({}), 4).picks.map((p) => p.item.product_name);
  assert.equal(names.filter((n) => /ベビードラム/.test(n)).length, 1);
  assert.deepEqual(plain(names), ["トイローヤル たたいてベビードラム", "A1", "A2", "B1"]);
});

test("理由は最大 4 つ、好きな遊び → 年齢 → 予算 → 大事にしたいことの順", () => {
  const items = [item({ name: "x", tags: ["ブロック"], age: 36, price: 2500, score: 90, axes: { safety: 4.5 } })];
  const r = D.recommend(items, answers({ interest: "build", budget: "3000", priority: "safety" }));
  const kinds = r.picks[0].reasons.map((x) => x.kind);
  assert.deepEqual(plain(kinds), ["interest", "age", "price", "priority"]);
});

test("出産祝い: 0〜5か月向けとして扱い、出産祝いタグを優先する", () => {
  const items = [
    item({ name: "gift", age: 0, tags: ["出産祝い"], score: 50 }),
    item({ name: "plain", age: 0, tags: [], score: 80 }),
    item({ name: "toddler", age: 12, tags: ["出産祝い"], score: 99 }),
  ];
  const a = { ...D.emptyAnswers(), who: "birth", interest: "any", budget: "any", priority: "balance" };
  assert.ok(D.isComplete(a), "出産祝いは年齢なしで完了");
  const names = D.recommend(items, a).picks.map((p) => p.item.product_name);
  assert.equal(names[0], "gift");
  assert.ok(!names.includes("toddler"));
  assert.equal(D.persona(a).title, "はじめてのおもちゃ");
});

test("共有リンク: toQuery → fromParams で往復でき、不正な値や欠けた回答は null", () => {
  const a = answers({ interest: "build", budget: "5000", priority: "safety", owned: ["pretend", "vehicle"] });
  const q = D.toQuery(a);
  const back = D.fromParams(new URLSearchParams(q));
  assert.deepEqual(plain(back), plain(a));
  assert.equal(D.fromParams(new URLSearchParams("w=child&a=3y&i=build&b=999&p=safety")), null);
  assert.equal(D.fromParams(new URLSearchParams("w=child&a=1y&i=science&b=any&p=safety")), null, "1歳に科学は選べない");
  assert.equal(D.fromParams(new URLSearchParams("")), null);
  const birth = D.fromParams(new URLSearchParams("w=birth&a=8y&i=any&b=any&p=balance"));
  assert.equal(birth.age, null, "出産祝いでは年齢を持たない");
});

test("保存: v2 を読み戻せ、旧 5 問 (q1..q5) の保存も読み替える", () => {
  const a = answers({ interest: "art", budget: "10000" });
  const saved = JSON.parse(JSON.stringify(D.toSaved(a, [{ item: item({ name: "x" }) }])));
  assert.equal(saved.v, 2);
  assert.equal(saved.top.title, "x");
  assert.deepEqual(plain(D.fromSaved(saved)), plain(a));

  const v1 = D.fromSaved({ answers: { q1: "3-4", q2: "social", q3: "indoor", q4: "8000", q5: "block" } });
  assert.equal(v1.who, "child");
  assert.equal(v1.age, "3y");
  assert.equal(v1.interest, "pretend");
  assert.equal(v1.budget, "10000");
  assert.deepEqual(plain(v1.owned), ["build"]);
  const baby = D.fromSaved({ answers: { q1: "0-1", q2: "dexterity", q4: "3000", q5: "few" } });
  assert.equal(baby.interest, "sense", "赤ちゃんの「手先」は「さわる・にぎる」");
  assert.equal(D.fromSaved({ answers: {} }), null);
});

test("年齢別ベスト10 (?age=): 帯の上限を超える対象年齢は出さない", () => {
  const items = [
    item({ name: "baby", age: 6, score: 60 }),
    item({ name: "three", age: 36, score: 99 }),
  ];
  const names = D.ageBest(items, "0-1", 10).map((p) => p.item.product_name);
  assert.deepEqual(plain(names), ["baby"]);
  assert.deepEqual(plain(D.ageBest(items, "nope", 10)), []);
  assert.deepEqual(plain(D.ageBest(items, "constructor", 10)), [], "Object の組み込み名を帯と取り違えない");
});

test("年齢別ベスト10 (?age=): 診断の 9 段階で引け、小学生の帯もある", () => {
  const items = [
    item({ name: "baby", age: 0, score: 60 }),
    item({ name: "three", age: 36, score: 80 }),
    item({ name: "six", age: 72, score: 90 }),
    item({ name: "ten", age: 120, score: 99 }),
  ];
  const names = (band) => plain(D.ageBest(items, band, 10).map((p) => p.item.product_name));
  assert.deepEqual(names("0-5m"), ["baby"]);
  assert.deepEqual(names("3y"), ["three"], "1 歳未満向けは 3 歳には幼すぎる");
  assert.deepEqual(names("6-7y"), ["six", "three"], "下限は診断と同じ lowerWindow (72-36=36 か月) まで");
  assert.deepEqual(names("8y"), ["ten", "six"]);
  for (const a of D.AGES) assert.ok(D.ageBand(a.value), a.value + " が引ける");
  assert.equal(D.ageBand("6-7y").label, "6〜7歳");
  assert.equal(D.ageBand("1-2").label, "1〜2歳", "旧 4 区分のリンクも生きている");
});

test("ホームの年齢タイムラインは診断の AGES と同じ帯・表記で並ぶ", () => {
  const tpl = readFileSync(
    path.resolve(path.dirname(fileURLToPath(import.meta.url)),
                 "../../hugo/layouts/partials/age_timeline.html"), "utf8");
  const bands = [...tpl.matchAll(/\(dict "v" "([^"]+)" "emoji" "([^"]+)" "label" "([^"]+)" "sub" "([^"]+)"\)/g)]
    .map((m) => ({ value: m[1], emoji: m[2], label: m[3], sub: m[4] }));
  assert.deepEqual(bands, plain(D.AGES.map((a) => ({ value: a.value, emoji: a.emoji, label: a.label, sub: a.sub }))));
});

test("結果のチップは回答した設問ぶん出て、出産祝いでは年齢チップを出さない", () => {
  const chips = D.summaryChips(answers({ owned: ["build"] }));
  assert.deepEqual(plain(chips.map((c) => c.step)), ["who", "age", "interest", "budget", "priority", "owned"]);
  const b = D.summaryChips({ ...D.emptyAnswers(), who: "birth", interest: "any", budget: "any", priority: "balance" });
  assert.ok(!b.some((c) => c.step === "age"));
});
