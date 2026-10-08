// tests/js/img_fallback.test.mjs — hugo/assets/js/img_fallback.js の単体テスト
//
// #9155: 読み込めなかった商品画像を「画像なし」に差し替える。固定する不変条件:
//   A. error を拾った <img> をプレースホルダーに差し替え、元 URL を残す
//   B. 差し替えは 1 回だけ (プレースホルダーが失敗してもループしない)
//   C. 読み込み前に失敗済みの画像は外部ホストのものだけ拾う (SVG ロゴの誤爆防止)
//   D. <img> 以外の error では何もしない
//
// 実行: node --test tests/js/
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import path from "node:path";

const SRC = readFileSync(path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../hugo/assets/js/img_fallback.js"
), "utf8");

function makeImg(src, { complete = false, naturalWidth = 0 } = {}) {
  const attrs = { src };
  const classes = new Set();
  const el = {
    tagName: "IMG",
    complete,
    naturalWidth,
    getAttribute: (n) => (n in attrs ? attrs[n] : null),
    setAttribute: (n, v) => { attrs[n] = String(v); },
    hasAttribute: (n) => n in attrs,
    removeAttribute: (n) => { delete attrs[n]; },
    classList: { add: (c) => classes.add(c), has: (c) => classes.has(c) },
    attrs,
  };
  Object.defineProperty(el, "src", {
    get: () => attrs.src,
    set: (v) => { attrs.src = v; },
  });
  return el;
}

function load(images = []) {
  let listener = null;
  const document = {
    images,
    addEventListener(type, fn, capture) {
      assert.equal(type, "error");
      assert.equal(capture, true);
      listener = fn;
    },
  };
  vm.runInNewContext(SRC, {
    document,
    location: { href: "https://navi.omcha.jp/deals/", origin: "https://navi.omcha.jp" },
    URL,
  });
  return (target) => listener({ target });
}

const FAKE = "https://m.media-amazon.com/images/I/71xyz123abc._AC_SX679_.jpg";

test("A: error の <img> をプレースホルダーに差し替え、元 URL を残す", () => {
  const fire = load();
  const img = makeImg(FAKE);
  img.attrs.srcset = FAKE + " 2x";
  fire(img);
  assert.match(img.src, /^data:image\/svg\+xml,/);
  assert.equal(img.getAttribute("data-img-fallback"), FAKE);
  assert.equal(img.hasAttribute("srcset"), false);
  assert.ok(img.classList.has("img-fallback"));
});

test("B: 2 回目の error では差し替えない", () => {
  const fire = load();
  const img = makeImg(FAKE);
  fire(img);
  const placeholder = img.src;
  fire(img);
  assert.equal(img.src, placeholder);
  assert.equal(img.getAttribute("data-img-fallback"), FAKE);
});

test("C: 読み込み前に失敗済みの外部画像だけ拾う", () => {
  const broken = makeImg(FAKE, { complete: true, naturalWidth: 0 });
  const ok = makeImg(FAKE.replace("71xyz123abc", "41PF9dk71TL"), { complete: true, naturalWidth: 500 });
  const lazy = makeImg(FAKE, { complete: false, naturalWidth: 0 });
  const localSvg = makeImg("/images/logo.svg", { complete: true, naturalWidth: 0 });
  load([broken, ok, lazy, localSvg]);
  assert.ok(broken.hasAttribute("data-img-fallback"));
  assert.equal(ok.hasAttribute("data-img-fallback"), false);
  assert.equal(lazy.hasAttribute("data-img-fallback"), false);
  assert.equal(localSvg.hasAttribute("data-img-fallback"), false);
});

test("D: <img> 以外・src 無しの error では何もしない", () => {
  const fire = load();
  const script = { tagName: "SCRIPT", getAttribute: () => "x.js" };
  assert.doesNotThrow(() => fire(script));
  const empty = makeImg("");
  fire(empty);
  assert.equal(empty.hasAttribute("data-img-fallback"), false);
  assert.doesNotThrow(() => fire(null));
});
