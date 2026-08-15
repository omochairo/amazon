// tests/js/sw.test.mjs — hugo/static/sw.js の fetch ルーティング単体テスト
//
// 背景 (2026-08-09): 「久しぶりに開くと古いトップが出て、F5 して初めて最新になる」
// を実機で再現し、原因が 2 層あることを確定させた。
//   層1: トップ "/" が SWR 経路に乗っており、PAGE_CACHE に TTL が無いため
//        前回訪問時の HTML が無期限に即返しされる (= 常に 1 世代遅れ)。
//   層2: 裏の revalidate が既定 cache モードの fetch なので、HTML の
//        Cache-Control: max-age=14400 によりブラウザ HTTP キャッシュのコピーが返り、
//        「取り直した最新」自体が最大 4 時間古くなる (層1 と直列に積み上がる)。
// このテストは 3 つの不変条件を固定する:
//   A. トップは stale を出さない
//   B. PAGE_CACHE の stale 許容は有限で、timestamp が無い応答は期限切れ扱い
//   C. SW からの取り直しは常に HTTP キャッシュを迂回する (cache: "no-cache")
//
// 実行: node --test tests/js/
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import path from "node:path";

// SW_PATH env で対象を差し替えられるようにしてある。修正前の sw.js を指して
// 実際に落ちること (テストが空振りでないこと) を確認するために使う。
const SW_PATH = process.env.SW_PATH || path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../hugo/static/sw.js"
);
const SW_SRC = readFileSync(SW_PATH, "utf8");
const ORIGIN = "https://navi.omcha.jp";

const CACHE_VERSION = /var CACHE_VERSION = "([^"]+)"/.exec(SW_SRC)[1];
const PAGE_CACHE = "omcha-pages-" + CACHE_VERSION;
const ASSET_CACHE = "omcha-assets-" + CACHE_VERSION;
const MAX_STALE_MS = 6 * 60 * 60 * 1000;

function keyOf(req) {
  const raw = typeof req === "string" ? req : req.url;
  return new URL(raw, ORIGIN).href;
}

class MockCache {
  constructor() {
    this.map = new Map();
  }
  async match(req) {
    const hit = this.map.get(keyOf(req));
    return hit ? hit.clone() : undefined;
  }
  async put(req, res) {
    this.map.set(keyOf(req), res);
  }
  async add(req) {
    this.map.set(keyOf(req), new Response("precached"));
  }
  async delete(req) {
    return this.map.delete(keyOf(req));
  }
  async keys() {
    return [...this.map.keys()].map((u) => ({ url: u }));
  }
}

/** SW を隔離 realm で読み込み、fetch リスナと操作用ハンドルを返す。 */
function loadSW({ network }) {
  const stores = new Map();
  const fetchCalls = [];

  const caches = {
    async open(name) {
      if (!stores.has(name)) stores.set(name, new MockCache());
      return stores.get(name);
    },
    async keys() {
      return [...stores.keys()];
    },
    async delete(name) {
      return stores.delete(name);
    },
    async match(req) {
      for (const c of stores.values()) {
        const hit = await c.match(req);
        if (hit) return hit;
      }
      return undefined;
    }
  };

  const listeners = new Map();
  const sandbox = {
    caches,
    fetch: (input, init) => {
      fetchCalls.push({ url: keyOf(input), init: init || {} });
      return network(keyOf(input), init || {});
    },
    Response,
    Request,
    Headers,
    URL,
    console
  };
  sandbox.self = {
    addEventListener: (type, fn) => listeners.set(type, fn),
    location: { origin: ORIGIN },
    skipWaiting: () => Promise.resolve(),
    clients: { claim: () => Promise.resolve() }
  };
  vm.createContext(sandbox);
  vm.runInContext(SW_SRC, sandbox);

  return {
    fetchCalls,
    async store() {
      return caches.open(PAGE_CACHE);
    },
    async assetStore() {
      return caches.open(ASSET_CACHE);
    },
    /** 指紋付きアセット (destination: style/script) を 1 本流す。 */
    async asset(pathname, destination = "style") {
      const req = {
        method: "GET",
        url: ORIGIN + pathname,
        mode: "no-cors",
        destination,
        headers: new Headers()
      };
      let responded;
      listeners.get("fetch")({
        request: req,
        respondWith: (p) => {
          responded = p;
        },
        waitUntil: () => {}
      });
      assert.ok(responded, "SW が respondWith しなかった: " + pathname);
      return await responded;
    },
    /** navigation リクエストを 1 本流し、SW が返す Response を得る。 */
    async navigate(pathname) {
      const req = {
        method: "GET",
        url: ORIGIN + pathname,
        mode: "navigate",
        destination: "document",
        headers: new Headers()
      };
      let responded;
      const event = {
        request: req,
        respondWith: (p) => {
          responded = p;
        },
        waitUntil: () => {}
      };
      listeners.get("fetch")(event);
      assert.ok(responded, "SW が respondWith しなかった: " + pathname);
      return await responded;
    }
  };
}

/** PAGE_CACHE に「n ミリ秒前に保存された」HTML を仕込む。 */
async function seed(sw, pathname, body, cachedAtOffsetMs) {
  const headers = new Headers({ "content-type": "text/html; charset=utf-8" });
  if (cachedAtOffsetMs !== null) {
    headers.set("x-omcha-cached-at", String(Date.now() - cachedAtOffsetMs));
  }
  const cache = await sw.store();
  await cache.put(pathname, new Response(body, { status: 200, headers }));
}

const netOk = (body) => async () =>
  new Response(body, {
    status: 200,
    headers: { "content-type": "text/html; charset=utf-8" }
  });

test("トップ / は cache が新しくても stale を返さない (層1 の再発防止)", async () => {
  const sw = loadSW({ network: netOk("FRESH_TOP") });
  await seed(sw, "/", "STALE_TOP", 1000); // 1 秒前に保存 = TTL 的には十分新しい
  const res = await sw.navigate("/");
  assert.equal(await res.text(), "FRESH_TOP");
});

test("トップ以外の静的パスは prefix 一致で誤って鮮度必須にならない", async () => {
  const sw = loadSW({ network: netOk("FRESH") });
  await seed(sw, "/about/", "CACHED_ABOUT", 1000);
  const res = await sw.navigate("/about/");
  assert.equal(await res.text(), "CACHED_ABOUT", "SWR の即返しが壊れている");
});

test("商品ページ: TTL 内の cache は即返しし、裏で取り直す", async () => {
  const sw = loadSW({ network: netOk("FRESH_PRODUCT") });
  await seed(sw, "/products/b0abc/", "CACHED_PRODUCT", 60 * 1000);
  const res = await sw.navigate("/products/b0abc/");
  assert.equal(await res.text(), "CACHED_PRODUCT");
  assert.equal(sw.fetchCalls.length, 1, "裏の revalidate が飛んでいない");
});

test("商品ページ: TTL 超過の cache は即返しせずネットワークを待つ", async () => {
  const sw = loadSW({ network: netOk("FRESH_PRODUCT") });
  await seed(sw, "/products/b0abc/", "OLD_PRODUCT", MAX_STALE_MS + 60 * 1000);
  const res = await sw.navigate("/products/b0abc/");
  assert.equal(await res.text(), "FRESH_PRODUCT");
});

test("timestamp の無い cache (旧バージョン残骸) は期限切れ扱いにする", async () => {
  const sw = loadSW({ network: netOk("FRESH_PRODUCT") });
  await seed(sw, "/products/b0abc/", "LEGACY_NO_TIMESTAMP", null);
  const res = await sw.navigate("/products/b0abc/");
  assert.equal(
    await res.text(),
    "FRESH_PRODUCT",
    "unknown を pass 側に潰すと無期限 stale が生き残る"
  );
});

test("ネットワーク断なら TTL 超過でも cache を返す (オフライン耐性を落とさない)", async () => {
  const sw = loadSW({
    network: async () => {
      throw new TypeError("offline");
    }
  });
  await seed(sw, "/products/b0abc/", "OLD_PRODUCT", MAX_STALE_MS + 60 * 1000);
  const res = await sw.navigate("/products/b0abc/");
  assert.equal(await res.text(), "OLD_PRODUCT");
});

test("取り直しは常に HTTP キャッシュを迂回する (層2 の再発防止)", async () => {
  const sw = loadSW({ network: netOk("FRESH") });
  await seed(sw, "/products/b0abc/", "CACHED", 60 * 1000);
  await sw.navigate("/products/b0abc/"); // SWR 経路
  await sw.navigate("/"); // network-first 経路
  await sw.navigate("/ranking/"); // network-first 経路 (既存の鮮度必須)
  assert.equal(sw.fetchCalls.length, 3);
  for (const call of sw.fetchCalls) {
    assert.equal(
      call.init.cache,
      "no-cache",
      "既定 cache モードだと max-age=14400 の HTTP キャッシュが返る: " + call.url
    );
  }
});

// ---- 指紋付きアセットの焼き付き (#5260) ---------------------------------
// デプロイ入れ替え窓に掴んだ /assets/** の 404 は Cache-Control: max-age=31536000 を
// 伴って返る (2026-08-15 実測)。素の fetch(req) は既定 cache モードなので、その
// 404 のコピーが 1 年間ネットワークに出ないまま返り続ける。SW 側は「失敗したら
// HTTP キャッシュを迂回して 1 回だけ取り直す」ことで自力で剥がす。

const CSS_PATH = "/assets/css/stylesheet.deadbeef.css";

/** 1 回目は poisoned な 404、cache: "reload" のときだけ実体を返すネットワーク。 */
function poisonedThenFresh(body) {
  return async (url, init) => {
    if (init.cache === "reload") {
      return new Response(body, {
        status: 200,
        headers: { "content-type": "text/css" }
      });
    }
    return new Response("not found", {
      status: 404,
      headers: {
        "content-type": "text/html; charset=utf-8",
        "cache-control": "max-age=31536000"
      }
    });
  };
}

test("焼き付いた 404 は HTTP キャッシュを迂回して取り直す (#5260)", async () => {
  const sw = loadSW({ network: poisonedThenFresh("body{color:red}") });
  const res = await sw.asset(CSS_PATH);
  assert.equal(res.status, 200, "404 のまま返している = 焼き付きを剥がせていない");
  assert.equal(await res.text(), "body{color:red}");
  assert.equal(sw.fetchCalls.length, 2, "取り直しが飛んでいない");
  assert.equal(sw.fetchCalls[1].init.cache, "reload",
    "既定 cache モードだと max-age=31536000 の 404 が返り続ける");
});

test("取り直しで得た実体は ASSET_CACHE に入る (次回はネットワーク不要)", async () => {
  const sw = loadSW({ network: poisonedThenFresh("body{color:red}") });
  await sw.asset(CSS_PATH);
  const cache = await sw.assetStore();
  const hit = await cache.match(CSS_PATH);
  assert.ok(hit, "取り直した実体がキャッシュされていない");
  assert.equal(await hit.text(), "body{color:red}");
});

test("成功時は取り直さない (毎回 2 往復させない)", async () => {
  const sw = loadSW({
    network: async () =>
      new Response("body{color:blue}", {
        status: 200,
        headers: { "content-type": "text/css" }
      })
  });
  await sw.asset(CSS_PATH);
  assert.equal(sw.fetchCalls.length, 1, "成功しているのに再取得している");
});

test("取り直しても駄目なら 404 を返す (offline HTML を style として掴ませない)", async () => {
  const sw = loadSW({
    network: async () =>
      new Response("not found", {
        status: 404,
        headers: { "content-type": "text/html; charset=utf-8" }
      })
  });
  const res = await sw.asset(CSS_PATH);
  assert.equal(res.status, 404);
  const cache = await sw.assetStore();
  assert.equal(await cache.match(CSS_PATH), undefined,
    "壊れた応答をキャッシュしている (#3568 の再発)");
});

test("PAGE_CACHE に保存される応答には保存時刻が打たれる", async () => {
  const sw = loadSW({ network: netOk("FRESH") });
  const before = Date.now();
  await sw.navigate("/products/b0abc/");
  const cache = await sw.store();
  const hit = await cache.match("/products/b0abc/");
  const at = Number(hit.headers.get("x-omcha-cached-at"));
  assert.ok(at >= before && at <= Date.now(), "保存時刻ヘッダが無い/不正: " + at);
  assert.equal(await hit.text(), "FRESH", "body が壊れている");
});
