// hugo/static/sw.js — おもちゃいろ 比較ナビ Service Worker (#1371 / epic #1365 P6)
//
// 目的: お気に入り / 最近見た商品 / 診断結果を「オフラインでも自分の選択だけは
// 確実に見られる」状態にする。フルオフラインサイトは目指さない。
//
// 戦略 (Phase 1 + 2):
//   - App shell (static page の HTML)      : cache-first + 裏で revalidate
//   - 静的アセット (JS/CSS/font, fingerprint): cache-first (URL が指紋付きで不変)
//   - 商品ページ (/products/<asin>/)         : stale-while-revalidate
//   - 画像                                   : cache-first + LRU 上限
//   - JSON / data                            : network-first (失敗時 cache fallback)
//   - 鮮度必須ページ (ranking/cospa/deals/search): network-first (stale を出さない)
//   - GA / gtag / 解析 beacon                : 素通し (intercept しない)
//
// バージョン更新: CACHE_VERSION を bump すると activate で旧 cache を全削除する。
// 開発時の bypass: URL に ?nocache=1 を付けると SW は fetch を intercept しない。

// v2 (2026-07-20): ASSET_CACHE の HTML 汚染を全ユーザーから一掃するための bump。
// デプロイ伝播窓に指紋付き JS URL へ HTML (404/offline) が返り、それが
// cache-first で恒久固定される事故を article_actions.min.*.js で実測 (#3568)。
//
// v3 (2026-08-09): 「久しぶりに開くと古いトップが出て F5 で初めて最新になる」
// の実機再現を受けた bump。PAGE_CACHE の内容は互換性が無い (timestamp header 付き)
// ため、旧 cache は activate で破棄させる。
var CACHE_VERSION = "v3-2026-08-09";
var SHELL_CACHE = "omcha-shell-" + CACHE_VERSION;
var PAGE_CACHE = "omcha-pages-" + CACHE_VERSION;
var ASSET_CACHE = "omcha-assets-" + CACHE_VERSION;
var IMAGE_CACHE = "omcha-images-" + CACHE_VERSION;
var IMAGE_MAX_ENTRIES = 120; // ~画像のみ。超過分は古い順に trim (擬似 LRU)

// install 時に precache する app shell。指紋なしの安定 URL のみ列挙する
// (指紋付きアセットは runtime cache-first で勝手に貯まる)。
var SHELL_URLS = [
  "/",
  "/favorites/",
  "/about/",
  "/about-score/",
  "/privacy/",
  "/diagnosis/",
  "/offline/",
  "/site.webmanifest"
];

// network-first で扱う = stale を出したくない鮮度必須パス。
var FRESH_PREFIXES = ["/ranking", "/cospa", "/deals", "/search"];
// 前方一致では表現できない鮮度必須パス (トップは "/" なので prefix 一致だと全 URL に当たる)。
// トップは新着記事・おすすめ・価格カードの入口で、毎デプロイ内容が変わる。
var FRESH_EXACT = ["/", "/index.html"];

// PAGE_CACHE の stale 許容上限。これを超えた cache hit は「即返し」に使わず
// ネットワークを待つ (失敗すればフォールバックとして返す)。
// SWR は本来「1 世代遅れ」を常態にする戦略で、上限が無いと数日ぶりの訪問でも
// 前回訪問時の HTML がそのまま出る (2026-08-09 に実機で再現)。
var PAGE_MAX_STALE_MS = 6 * 60 * 60 * 1000; // 6h
var CACHED_AT_HEADER = "x-omcha-cached-at";

function isFresh(path) {
  for (var e = 0; e < FRESH_EXACT.length; e++) {
    if (path === FRESH_EXACT[e]) return true;
  }
  for (var i = 0; i < FRESH_PREFIXES.length; i++) {
    if (path.indexOf(FRESH_PREFIXES[i]) === 0) return true;
  }
  return false;
}

self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(SHELL_CACHE).then(function (cache) {
      // 個別 add で 1 件失敗しても install 全体を落とさない。
      return Promise.all(SHELL_URLS.map(function (u) {
        return cache.add(new Request(u, { cache: "reload" })).catch(function () {});
      }));
    }).then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener("activate", function (event) {
  var keep = [SHELL_CACHE, PAGE_CACHE, ASSET_CACHE, IMAGE_CACHE];
  event.waitUntil(
    caches.keys().then(function (names) {
      return Promise.all(names.map(function (n) {
        if (keep.indexOf(n) === -1) return caches.delete(n);
      }));
    }).then(function () { return self.clients.claim(); })
  );
});

// ---- strategy helpers --------------------------------------------------

function cacheFirst(req, cacheName) {
  return caches.open(cacheName).then(function (cache) {
    return cache.match(req).then(function (hit) {
      if (hit) return hit;
      return fetch(req).then(function (res) {
        if (res && (res.ok || res.type === "opaque")) cache.put(req, res.clone());
        return res;
      });
    });
  });
}

// 同一オリジンの指紋付きアセット (script/style/font) 用 cache-first。
// GitLab Pages のデプロイ伝播窓では、新しい指紋付き URL への初回リクエストに
// 一時的に HTML (404/offline ページ) が 200 で返ることがあり、素の cache-first
// だとその HTML が指紋 URL (内容不変) の下で恒久汚染として残り続ける
// (2026-07-20 に article_actions.min.*.js で実測。nosniff により script は
// 無音でブロックされ、機能だけが静かに死ぬ)。ここでは:
//   1. HTML を返す応答は絶対にキャッシュしない
//   2. 既存キャッシュヒットが HTML なら破棄して network へ落とす (自己修復)
// 同一オリジン応答は opaque にならないため content-type 検査は常に可能。
function isHtmlResponse(res) {
  var ct = res && res.headers && res.headers.get("content-type");
  return !!ct && ct.indexOf("text/html") !== -1;
}

// 指紋付きアセットの取得。失敗したときだけ HTTP キャッシュを迂回して 1 回だけ
// 取り直す (#5260)。
// デプロイ入れ替え窓に掴んだ 404 は `Cache-Control: max-age=31536000` を伴って
// 返ることが実測されており (2026-08-15)、素の fetch(req) は既定 cache モードなので
// **その 404 のコピーが 1 年間返り続ける** (ネットワークに出ないのでリロードでも
// 直らない。curl は 200 なのにブラウザだけスタイル無し = #5260 の症状)。
// 指紋付き URL は内容不変なので、成功応答は HTTP キャッシュに任せたまま、
// 失敗したときだけ reload で取り直せば副作用なく自己修復できる。
function fetchAsset(req) {
  return fetch(req).then(function (res) {
    if (res && res.ok && !isHtmlResponse(res)) return res;
    return fetch(req.url, { cache: "reload", credentials: "same-origin" })
      .catch(function () { return res; });
  });
}

function assetCacheFirst(req) {
  return caches.open(ASSET_CACHE).then(function (cache) {
    return cache.match(req).then(function (hit) {
      if (hit && !isHtmlResponse(hit)) return hit;
      var refetch = fetchAsset(req).then(function (res) {
        if (res && res.ok && !isHtmlResponse(res)) cache.put(req, res.clone());
        return res;
      });
      if (hit) return cache.delete(req).then(function () { return refetch; });
      return refetch;
    });
  });
}

// PAGE_CACHE へ入れる応答には保存時刻を打つ。stale 判定はこれだけを根拠にする
// (Date / Age ヘッダは Cloudflare のエッジ滞留時間が乗るので保存時刻の代わりに
// ならない)。同一オリジンの HTML なので body を読み直して詰め替えられる。
function stampCachedAt(res) {
  return res.blob().then(function (body) {
    var headers = new Headers(res.headers);
    headers.set(CACHED_AT_HEADER, String(Date.now()));
    return new Response(body, {
      status: res.status,
      statusText: res.statusText,
      headers: headers
    });
  });
}

// timestamp が無い応答 (旧バージョンの cache が残っていた場合) は期限切れ扱い。
// 「不明」を pass 側に潰すと、まさに直したい無期限 stale が生き残る。
function isStale(res, now) {
  if (!res) return true;
  var at = Number(res.headers && res.headers.get(CACHED_AT_HEADER));
  if (!at) return true;
  return (now - at) > PAGE_MAX_STALE_MS;
}

// SW からの取り直しは必ず HTTP キャッシュを迂回する。
// 素の fetch(req) は既定 cache モードなので、HTML に付いている
// Cache-Control: max-age=14400 (実測 2026-08-09) の下ではブラウザ HTTP キャッシュの
// コピーが返り、「裏で取り直した最新」自体が最大 4 時間古くなる。
// SWR の 1 世代遅れと直列に積み上がるのを防ぐ。
function fetchFresh(req) {
  return fetch(req.url, { cache: "no-cache", credentials: "same-origin" });
}

function revalidate(cache, req) {
  return fetchFresh(req).then(function (res) {
    if (!res || !res.ok) return res;
    return stampCachedAt(res.clone()).then(function (stamped) {
      return cache.put(req, stamped).then(function () { return res; });
    });
  });
}

function staleWhileRevalidate(req, cacheName) {
  return caches.open(cacheName).then(function (cache) {
    return cache.match(req).then(function (hit) {
      var net = revalidate(cache, req);
      if (hit && !isStale(hit, Date.now())) {
        net.catch(function () {}); // 裏更新の失敗は表示に影響させない
        return hit;
      }
      // 初回 / 期限切れはネットワークを待つ。落ちたときだけ古い cache を出す。
      return net.catch(function () { return hit; });
    });
  });
}

function networkFirst(req, cacheName) {
  return caches.open(cacheName).then(function (cache) {
    return revalidate(cache, req).catch(function () {
      return cache.match(req).then(function (hit) {
        return hit || caches.match("/offline/");
      });
    });
  });
}

// 画像 cache は擬似 LRU: 上限超過時に最古 (= 先頭) を間引く。
function trimCache(cacheName, max) {
  caches.open(cacheName).then(function (cache) {
    cache.keys().then(function (keys) {
      if (keys.length <= max) return;
      for (var i = 0; i < keys.length - max; i++) cache.delete(keys[i]);
    });
  });
}

// ---- fetch routing -----------------------------------------------------

self.addEventListener("fetch", function (event) {
  var req = event.request;
  if (req.method !== "GET") return;

  var url;
  try { url = new URL(req.url); } catch (e) { return; }
  if (url.searchParams.get("nocache") === "1") return; // 開発 bypass

  // 解析 / 計測系は intercept しない (常に network へ素通し)。
  var ANALYTICS = ["googletagmanager.com", "google-analytics.com", "analytics.google.com"];
  for (var a = 0; a < ANALYTICS.length; a++) {
    if (url.hostname.indexOf(ANALYTICS[a]) !== -1) return;
  }

  var sameOrigin = url.origin === self.location.origin;

  // 画像: 同一/別オリジン問わず cache-first + LRU。
  if (req.destination === "image") {
    event.respondWith(cacheFirst(req, IMAGE_CACHE).then(function (res) {
      trimCache(IMAGE_CACHE, IMAGE_MAX_ENTRIES);
      return res;
    }));
    return;
  }

  // 別オリジン (font CDN 等) は cache-first、それ以外は素通し。
  if (!sameOrigin) {
    if (req.destination === "font" || req.destination === "style" || req.destination === "script") {
      event.respondWith(cacheFirst(req, ASSET_CACHE));
    }
    return;
  }

  // 同一オリジンの指紋付きアセット: cache-first + HTML 汚染ガード (上の
  // assetCacheFirst コメント参照)。
  if (req.destination === "script" || req.destination === "style" || req.destination === "font") {
    event.respondWith(assetCacheFirst(req));
    return;
  }

  // JSON / data (Fuse index・index.json 等): network-first。
  if (url.pathname.indexOf("/data/") === 0 || /\.json$/.test(url.pathname)) {
    event.respondWith(networkFirst(req, PAGE_CACHE));
    return;
  }

  // HTML ナビゲーション。
  if (req.mode === "navigate" || req.destination === "document") {
    if (isFresh(url.pathname)) {
      event.respondWith(networkFirst(req, PAGE_CACHE));
    } else {
      // 静的 page / 商品ページとも SWR (即返し + 裏更新)。失敗時 offline へ。
      event.respondWith(
        staleWhileRevalidate(req, PAGE_CACHE).then(function (res) {
          return res || caches.match("/offline/");
        })
      );
    }
    return;
  }
});

// ---- precache message (Phase 2) ----------------------------------------
// page から最近見た商品 12 件の URL を受け取り、商品ページ HTML を意図的に
// precache する (オフラインで carousel の遷移先を開けるように)。
self.addEventListener("message", function (event) {
  var data = event.data || {};
  if (data.type === "PRECACHE_URLS" && Array.isArray(data.urls)) {
    caches.open(PAGE_CACHE).then(function (cache) {
      data.urls.slice(0, 12).forEach(function (u) {
        cache.match(u).then(function (hit) {
          if (!hit) cache.add(new Request(u, { cache: "no-cache" })).catch(function () {});
        });
      });
    });
  } else if (data.type === "SKIP_WAITING") {
    self.skipWaiting();
  }
});
