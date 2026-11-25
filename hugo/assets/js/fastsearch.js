import * as params from '@params';

let fuse;
const resList = document.getElementById('searchResults');
const summary = document.getElementById('searchSummary');
const sInput = document.getElementById('searchInput');

// カード描画は共通実装 window.OmochaUtils.renderProductCard (utils/product-card.js) に
// 集約済み。SSR (product_card.html) / 診断 / 一覧と完全に同一構造のカード
// (スコア tier・最安値・年齢バッジ) を返す。以前ここに独自の renderCard があり、
// バッジ用ラッパー (.product-card-badges) と価格・年齢を欠いていたため、検索結果だけ
// スコアバッジの位置が崩れ・最安値/年齢が出ていなかった (#3055 follow-up)。
function makeCard(item) {
  const fn = window.OmochaUtils && window.OmochaUtils.renderProductCard;
  return fn ? fn(item) : null;
}

window.onload = function () {
  const xhr = new XMLHttpRequest();
  xhr.onreadystatechange = function () {
    if (xhr.readyState !== 4) return;
    if (xhr.status !== 200) {
      console.log(xhr.responseText);
      if (summary) summary.textContent = '検索データを読み込めませんでした。ページを再読み込みしてください';
      return;
    }
    const data = JSON.parse(xhr.responseText);
    if (!data) return;
    let options = {
      distance: 1000,
      threshold: 0.3,
      minMatchCharLength: 2,
      ignoreLocation: true,
      keys: ['title', 'brand', 'product_name', 'tags', 'summary', 'content', 'permalink']
    };
    if (params.fuseOpts) {
      options = {
        isCaseSensitive: params.fuseOpts.iscasesensitive ?? false,
        includeScore: params.fuseOpts.includescore ?? false,
        includeMatches: params.fuseOpts.includematches ?? false,
        minMatchCharLength: params.fuseOpts.minmatchcharlength ?? 2,
        shouldSort: params.fuseOpts.shouldsort ?? true,
        findAllMatches: params.fuseOpts.findallmatches ?? false,
        keys: params.fuseOpts.keys ?? ['title', 'brand', 'product_name', 'tags', 'summary', 'content', 'permalink'],
        location: params.fuseOpts.location ?? 0,
        threshold: params.fuseOpts.threshold ?? 0.3,
        distance: params.fuseOpts.distance ?? 1000,
        ignoreLocation: params.fuseOpts.ignorelocation ?? true
      };
    }
    fuse = new Fuse(data, options);
    if (summary) summary.textContent = `${data.length} 件の記事から検索できます`;
    // トップの検索欄から ?q= で来たとき、または search.json の読み込み前に
    // 打ち始めていたときは、準備ができた時点でその語で検索する (navi-brain#213 PR D)
    if (sInput.value.trim()) runSearch(sInput.value);
  };
  xhr.open('GET', '/search.json');
  xhr.send();
};

function reset() {
  resList.innerHTML = '';
  sInput.value = '';
  if (summary) summary.textContent = '';
  sInput.focus();
}

// ?q= の語を先に欄へ入れておく (結果は Fuse の準備ができてから出す)
(function prefillFromQuery() {
  let q = '';
  try {
    q = new URLSearchParams(window.location.search).get('q') || '';
  } catch (e) {
    return;
  }
  q = q.trim().slice(0, 64);
  if (q) sInput.value = q;
})();

sInput.onkeyup = function () {
  runSearch(this.value);
};

function runSearch(raw) {
  if (!fuse) return;
  const q = raw.trim();
  if (!q) {
    resList.innerHTML = '';
    if (summary) summary.textContent = '';
    return;
  }
  const limit = (params.fuseOpts && params.fuseOpts.limit) || 24;
  const results = fuse.search(q, { limit });
  if (!results.length) {
    resList.innerHTML = '';
    if (summary) summary.textContent = `「${q}」に一致する記事は見つかりませんでした`;
    return;
  }
  if (summary) summary.textContent = `「${q}」の検索結果: ${results.length} 件`;
  resList.innerHTML = '';
  const frag = document.createDocumentFragment();
  results.forEach(r => {
    const card = makeCard(r.item);
    if (card) frag.appendChild(card);
  });
  resList.appendChild(frag);
}

sInput.addEventListener('search', function () {
  if (!this.value) reset();
});

document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape') reset();
});
