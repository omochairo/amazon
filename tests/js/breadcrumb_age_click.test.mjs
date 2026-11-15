import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import path from "node:path";

const SRC = readFileSync(path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../hugo/assets/js/breadcrumb_age_click.js"), "utf8");

function run(withGtag = true) {
  const events = [];
  let click;
  const anchor = { getAttribute: (name) => name === "data-age-hub" ? "age-1" : null };
  const sandbox = {
    window: withGtag ? { gtag: (...args) => events.push(args) } : {},
    document: { addEventListener: (type, fn) => { if (type === "click") click = fn; } },
  };
  vm.createContext(sandbox);
  vm.runInContext(SRC, sandbox);
  click({ target: { closest: (selector) => selector === 'a[data-nav="breadcrumb-age"]' ? anchor : null } });
  return events;
}

test("年齢パンくずのクリックを1回計測する", () => {
  const events = run();
  assert.equal(events.length, 1);
  assert.equal(events[0][0], "event");
  assert.equal(events[0][1], "breadcrumb_age_click");
  assert.equal(events[0][2].age_hub, "age-1");
});

test("gtag が無くても例外にならない", () => {
  assert.doesNotThrow(() => run(false));
});
