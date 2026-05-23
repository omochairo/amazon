import * as params from '@params';

let fuse;
const resList = document.getElementById('searchResults');
const summary = document.getElementById('searchSummary');
const sInput = document.getElementById('searchInput');

function escapeHtml(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

const BRAND_BLACKLIST = new Set([
  '不明', '不明 / 不明', 'Unknown', 'N/A', 'なし', '—', '-',
  'ノーブランド', 'ノーブランド品', 'NoBrand', 'No Brand', 'Generic'
]);

function renderCard(it) {
  const title = escapeHtml(it.title || '');
  const url = escapeHtml(it.permalink || '#');
  const img = it.image ? escapeHtml(it.image) : '';
  const brand = it.brand && !BRAND_BLACKLIST.has(it.brand) ? escapeHtml(it.brand) : '';
  const score100 = Number(it.ivs_score_100) || 0;
  const score = Number(it.ivs_score) || 0;
  const date = escapeHtml(it.date || '');
  const tags = Array.isArray(it.tags) ? it.tags.slice(0, 2) : [];

  let scoreBadge = '';
  if (score100 > 0) {
    scoreBadge = `<span class="product-card-score" title="知育スコア (100点満点)">🏆 <strong>${score100}</strong><span class="product-card-score-suffix">点</span></span>`;
  } else if (score > 0) {
    scoreBadge = `<span class="product-card-score" title="知育スコア (5点満点)">🏆 <strong>${score.toFixed(1)}</strong><span class="product-card-score-suffix">/5</span></span>`;
  }

  const imgBlock = img
    ? `<img src="${img}" alt="${title}" loading="lazy" decoding="async" referrerpolicy="no-referrer">`
    : `<div class="product-card-image-fallback">🧸</div>`;

  const brandBlock = brand ? `<div class="product-card-brand">${brand}</div>` : '';
  const tagsBlock = tags.length
    ? `<span class="product-card-tags">${tags.map(t => `<span class="product-card-tag">#${escapeHtml(t)}</span>`).join('')}</span>`
    : '';

  return (
    `<a class="product-card" href="${url}" aria-label="${title} の比較レビューを読む" data-ivs="${score}" data-ivs100="${score100}">` +
      `<div class="product-card-image">${imgBlock}${scoreBadge}</div>` +
      `<div class="product-card-body">` +
        brandBlock +
        `<h3 class="product-card-title">${title}</h3>` +
        `<div class="product-card-meta">` +
          (date ? `<time datetime="${date}">${date}</time>` : '<span></span>') +
          tagsBlock +
        `</div>` +
      `</div>` +
    `</a>`
  );
}

window.onload = function () {
  const xhr = new XMLHttpRequest();
  xhr.onreadystatechange = function () {
    if (xhr.readyState !== 4) return;
    if (xhr.status !== 200) { console.log(xhr.responseText); return; }
    const data = JSON.parse(xhr.responseText);
    if (!data) return;
    let options = {
      distance: 1000,
      threshold: 0.4,
      ignoreLocation: true,
      keys: ['title', 'brand', 'product_name', 'tags', 'summary', 'content', 'permalink']
    };
    if (params.fuseOpts) {
      options = {
        isCaseSensitive: params.fuseOpts.iscasesensitive ?? false,
        includeScore: params.fuseOpts.includescore ?? false,
        includeMatches: params.fuseOpts.includematches ?? false,
        minMatchCharLength: params.fuseOpts.minmatchcharlength ?? 1,
        shouldSort: params.fuseOpts.shouldsort ?? true,
        findAllMatches: params.fuseOpts.findallmatches ?? false,
        keys: params.fuseOpts.keys ?? ['title', 'brand', 'product_name', 'tags', 'summary', 'content', 'permalink'],
        location: params.fuseOpts.location ?? 0,
        threshold: params.fuseOpts.threshold ?? 0.4,
        distance: params.fuseOpts.distance ?? 1000,
        ignoreLocation: params.fuseOpts.ignorelocation ?? true
      };
    }
    fuse = new Fuse(data, options);
    if (summary) summary.textContent = `${data.length} 件の記事から検索できます`;
  };
  xhr.open('GET', '../index.json');
  xhr.send();
};

function reset() {
  resList.innerHTML = '';
  sInput.value = '';
  if (summary) summary.textContent = '';
  sInput.focus();
}

sInput.onkeyup = function () {
  if (!fuse) return;
  const q = this.value.trim();
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
  resList.innerHTML = results.map(r => renderCard(r.item)).join('');
};

sInput.addEventListener('search', function () {
  if (!this.value) reset();
});

document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape') reset();
});
