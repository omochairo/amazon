// tests/js/papermod_ui.test.mjs — hugo/assets/js/papermod_ui.js のページ内リンク処理。
//
// 固定する動作:
//   - 行き先のあるページ内リンクは、遷移を止めてスクロールし、# を履歴に積む
//   - 行き先の無いリンク (href="#"、id の無い見出し、壊れた % 表記) では例外を出さず、
//     既定の動作に任せる (以前は null.scrollIntoView() で TypeError になっていた)
//   - role="button" の <a> (カテゴリナビの開閉 chip) には付けない
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import path from "node:path";

const SRC = readFileSync(
  path.resolve(path.dirname(fileURLToPath(import.meta.url)),
               "../../hugo/assets/js/papermod_ui.js"), "utf8");

// hrefs: ページ内リンクの href。ids: ページに存在する id。
function setup({ hrefs, ids, reducedMotion = false }) {
  const scrolled = [];
  const pushed = [];
  const replaced = [];
  let selector = null;
  const anchors = hrefs.map((href) => {
    let onClick = null;
    return {
      getAttribute: (n) => (n === "href" ? href : null),
      addEventListener(type, fn) { if (type === "click") { onClick = fn; } },
      click() {
        let prevented = false;
        onClick.call(this, { preventDefault() { prevented = true; } });
        return prevented;
      },
    };
  });
  const doc = {
    getElementById: (id) => (ids.includes(id)
      ? { scrollIntoView: (opt) => scrolled.push([id, opt]) } : null),
    querySelectorAll(sel) { selector = sel; return anchors; },
    querySelector: () => null,
  };
  const win = { matchMedia: () => ({ matches: reducedMotion }) };
  const history = {
    pushState: (s, t, url) => pushed.push(url),
    replaceState: (s, t, url) => replaced.push(url),
  };
  vm.runInNewContext(SRC, { document: doc, window: win, history, localStorage: {} });
  return { anchors, scrolled, pushed, replaced, selector: () => selector };
}

test("行き先のあるリンク: 遷移を止めてスクロールし、# を履歴に積む", () => {
  const { anchors, scrolled, pushed } = setup({ hrefs: ["#spec"], ids: ["spec"] });
  assert.equal(anchors[0].click(), true);
  assert.deepEqual(JSON.parse(JSON.stringify(scrolled)), [["spec", { behavior: "smooth" }]]);
  assert.deepEqual(pushed, ["#spec"]);
});

test("日本語の id (percent-encode された href) を解決する", () => {
  const { anchors, scrolled, pushed } = setup({
    hrefs: ["#%e4%be%a1%e6%a0%bc"], ids: ["価格"], reducedMotion: true,
  });
  assert.equal(anchors[0].click(), true);
  assert.equal(scrolled[0][0], "価格");
  assert.equal(scrolled[0][1], undefined);
  assert.deepEqual(pushed, ["#%e4%be%a1%e6%a0%bc"]);
});

test("#top は履歴に積まず、# を消す", () => {
  const { anchors, pushed, replaced } = setup({ hrefs: ["#top"], ids: ["top"] });
  assert.equal(anchors[0].click(), true);
  assert.deepEqual(pushed, []);
  assert.deepEqual(replaced, [" "]);
});

test("行き先の無いリンクでは例外を出さず、既定の動作に任せる", () => {
  const { anchors, scrolled, pushed } = setup({
    hrefs: ["#", "#missing", "#%E0%A4%A", "#it's"], ids: ["spec"],
  });
  for (const a of anchors) {
    assert.equal(a.click(), false);
  }
  assert.deepEqual(scrolled, []);
  assert.deepEqual(pushed, []);
});

test("role=\"button\" の <a> は対象から外す", () => {
  const { selector } = setup({ hrefs: [], ids: [] });
  assert.equal(selector(), 'a[href^="#"]:not([role="button"])');
});
