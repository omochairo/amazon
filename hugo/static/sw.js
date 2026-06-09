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

var CACHE_VERSION = "v1-2026-06-08";
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

function isFresh(path) {
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

function staleWhileRevalidate(req, cacheName) {
  return caches.open(cacheName).then(function (cache) {
    return cache.match(req).then(function (hit) {
      var net = fetch(req).then(function (res) {
        if (res && res.ok) cache.put(req, res.clone());
        return res;
      }).catch(function () { return hit; });
      return hit || net;
    });
  });
}

function networkFirst(req, cacheName) {
  return caches.open(cacheName).then(function (cache) {
    return fetch(req).then(function (res) {
      if (res && res.ok) cache.put(req, res.clone());
      return res;
    }).catch(function () {
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

  // 同一オリジンの指紋付きアセット: cache-first。
  if (req.destination === "script" || req.destination === "style" || req.destination === "font") {
    event.respondWith(cacheFirst(req, ASSET_CACHE));
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
