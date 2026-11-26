// /ranking/ の並べ替え (hugo/assets/js/ranking_sort.js) と、テンプレートが前提の属性を出していること。
// 2026-11-26: 並びは「解説ありを先に」(navi-brain#166 F3) のまま、「楽天の順位順」にも並べ替えられる。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SRC = readFileSync(path.resolve(HERE, "../../hugo/assets/js/ranking_sort.js"), "utf8");
const LAYOUTS = path.resolve(HERE, "../../hugo/layouts");

// ---- 最小の DOM (このスクリプトが使う分だけ) ----
class El {
  constructor(cls = "", attrs = {}) {
    this.classes = new Set(cls.split(" ").filter(Boolean));
    this.attrs = { ...attrs };
    this.children = [];
    this.parentNode = null;
    this.hidden = false;
    this.listeners = {};
    this.classList = {
      add: (c) => this.classes.add(c),
      contains: (c) => this.classes.has(c),
      toggle: (c, on) => (on ? this.classes.add(c) : this.classes.delete(c)),
    };
  }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  appendChild(c) {
    if (c.parentNode) c.parentNode.children = c.parentNode.children.filter((x) => x !== c);
    c.parentNode = this;
    this.children.push(c);
    return c;
  }
  addEventListener(t, f) { (this.listeners[t] ||= []).push(f); }
  click() { (this.listeners.click || []).forEach((f) => f({})); }
  getBoundingClientRect() { return { top: 0 }; }
  *walk() { for (const c of this.children) { yield c; yield* c.walk(); } }
  matches(sel) {
    // ".a.b" / ".a[data-x=\"y\"]" / "[data-x]" だけ扱う
    const m = /^((?:\.[\w-]+)*)(?:\[([\w-]+)(?:="([^"]*)")?\])?$/.exec(sel.trim());
    if (!m) throw new Error("unsupported selector: " + sel);
    const cls = (m[1] || "").split(".").filter(Boolean);
    if (!cls.every((c) => this.classes.has(c))) return false;
    if (m[2]) {
      if (!(m[2] in this.attrs)) return false;
      if (m[3] !== undefined && this.attrs[m[2]] !== m[3]) return false;
    }
    return true;
  }
  querySelectorAll(sel) {
    const parts = sel.split(",").map((s) => s.trim());
    return [...this.walk()].filter((e) => parts.some((p) => e.matches(p)));
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}

function li(rank, hasArticle) {
  return new El("ranking-item", { "data-rank": String(rank), "data-has-article": hasArticle ? "1" : "0" });
}

function boot() {
  const root = new El("rk-page");
  const sort = root.appendChild(new El("rk-sort"));
  sort.hidden = true;
  const bArticle = sort.appendChild(new El("rk-sort-btn", { "data-order": "article", "aria-pressed": "true" }));
  const bRank = sort.appendChild(new El("rk-sort-btn", { "data-order": "rank", "aria-pressed": "false" }));
  const jump = root.appendChild(new El("rk-jump-link", { "data-jump-group": "" }));
  const status = root.appendChild(new El("rk-sort-status"));
  const groups = root.appendChild(new El("rk-groups"));
  const gA = groups.appendChild(new El("rk-group", { "data-group": "article" }));
  const olA = gA.appendChild(new El("ranking-list"));
  const gO = groups.appendChild(new El("rk-group", { "data-group": "others" }));
  const olO = gO.appendChild(new El("ranking-list"));
  const gR = groups.appendChild(new El("rk-group rk-group--rank", { "data-group": "rank" }));
  gR.hidden = true;
  const olR = gR.appendChild(new El("ranking-list"));
  // 解説ありを先に: 1,3,5 / そのほか: 2,4
  [1, 3, 5].forEach((r) => olA.appendChild(li(r, true)));
  [2, 4].forEach((r) => olO.appendChild(li(r, false)));

  const events = [];
  const window = {
    gtag: (_e, name, params) => events.push({ name, params }),
    innerHeight: 800,
    scrollBy: () => {},
    matchMedia: () => ({ matches: true }), // 動きを減らす = アニメーションの経路は通さない
  };
  const document = { querySelector: (s) => (root.matches(s) ? root : root.querySelector(s)) };
  vm.runInNewContext(SRC, { window, document, Element: { prototype: {} } });
  return { root, sort, bArticle, bRank, jump, status, gA, gO, gR, olA, olO, olR, events };
}

const ranks = (ol) => ol.children.map((c) => +c.getAttribute("data-rank"));

test("JS があるときだけ並べ替えボタンを出す", () => {
  const d = boot();
  assert.equal(d.sort.hidden, false);
});

test("楽天の順位順を押すと 1 位から順に 1 つの一覧へ移り、もう一度で元の 2 群に戻る", () => {
  const d = boot();
  d.bRank.click();
  assert.deepEqual(ranks(d.olR), [1, 2, 3, 4, 5]);
  assert.equal(d.gR.hidden, false);
  assert.equal(d.gA.hidden, true);
  assert.equal(d.gO.hidden, true);
  assert.equal(d.bRank.getAttribute("aria-pressed"), "true");
  assert.equal(d.bArticle.getAttribute("aria-pressed"), "false");
  assert.ok(d.root.classList.contains("rk-order-rank"));
  assert.match(d.status.textContent, /楽天の順位順/);

  d.bArticle.click();
  assert.deepEqual(ranks(d.olA), [1, 3, 5]);
  assert.deepEqual(ranks(d.olO), [2, 4]);
  assert.equal(d.olR.children.length, 0);
  assert.equal(d.gR.hidden, true);
  assert.equal(d.gA.hidden, false);
  assert.ok(!d.root.classList.contains("rk-order-rank"));
});

test("並べ替えを計測に送る (同じ並びをもう一度押しても送らない)", () => {
  const d = boot();
  d.bArticle.click();
  d.bRank.click();
  d.bRank.click();
  // params は vm の別の realm で作られるので、JSON にして比べる
  assert.equal(JSON.stringify(d.events), JSON.stringify([{ name: "ranking_sort", params: { order: "rank" } }]));
});

test("順位順のときに群へのページ内リンクを押すと、元の並びに戻す", () => {
  const d = boot();
  d.bRank.click();
  d.jump.click();
  assert.equal(d.gA.hidden, false);
  assert.deepEqual(ranks(d.olA), [1, 3, 5]);
});

test("テンプレートが並べ替え・計測・ページ内リンクの前提を出している", () => {
  const item = readFileSync(path.join(LAYOUTS, "partials/ranking_item.html"), "utf8");
  for (const needle of ['id="rank-{{ $rank }}"', 'data-rank="{{ $rank }}"', 'data-asin="{{ upper . }}"', "data-has-article=", '<h3 class="ranking-title">']) {
    assert.ok(item.includes(needle), "ranking_item.html: " + needle);
  }
  const page = readFileSync(path.join(LAYOUTS, "_default/ranking.html"), "utf8");
  for (const needle of ['class="rk-sort" role="group" aria-label="並び順" hidden', 'data-order="rank"', 'class="rk-group rk-group--rank" data-group="rank" hidden', "rk-page", "js/ranking_sort.js", '"short" true']) {
    assert.ok(page.includes(needle), "ranking.html: " + needle);
  }
});
