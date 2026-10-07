/* #9155: 読み込めなかった画像を「画像なし」のプレースホルダーに差し替える。
 *
 * 商品画像は Amazon の CDN を直接参照しており、URL が 404 になると壊れた
 * アイコンのまま表示されていた (B0DF72LSP7 の捏造 URL が /deals/ に出た件)。
 * 生成側は #9154 で amazon.json の画像に揃えたが、Amazon 側で画像が消える
 * ケースと、お気に入り / 閲覧履歴が localStorage に保存した古い URL は
 * サーバー側では直せないので、表示側にも最後の防御を置く。
 *
 * 実装方針:
 *   - リスナーは document に 1 個だけ (error は bubble しないので capture)。
 *     テンプレート 10 箇所 + JS 描画のカードに個別の onerror を書かずに済む
 *   - 差し替えは 1 回だけ (data-img-fallback に元 URL を残す)。プレースホルダー
 *     自体が失敗しても再差し替えのループにならない
 *   - defer で読み込まれる前に失敗済みの画像は complete && naturalWidth === 0 で
 *     拾う。ただし外部ホストの画像に限る (サイズ指定の無い SVG ロゴは正常でも
 *     naturalWidth が 0 になることがあるため)
 */
(function () {
  'use strict';

  var PLACEHOLDER = 'data:image/svg+xml,' + encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200">' +
    '<rect width="200" height="200" fill="#8882"/>' +
    '<text x="100" y="108" text-anchor="middle" font-size="18" fill="#888" ' +
    'font-family="sans-serif">画像なし</text></svg>'
  );

  function fallback(el) {
    if (!el || el.tagName !== 'IMG') return;
    var src = el.getAttribute('src');
    if (!src || src === PLACEHOLDER || el.hasAttribute('data-img-fallback')) return;
    el.setAttribute('data-img-fallback', src);
    el.removeAttribute('srcset');
    el.src = PLACEHOLDER;
    if (el.classList) el.classList.add('img-fallback');
  }

  function isExternal(el) {
    try {
      return new URL(el.getAttribute('src'), location.href).origin !== location.origin;
    } catch (e) {
      return false;
    }
  }

  document.addEventListener('error', function (e) { fallback(e.target); }, true);

  var imgs = document.images || [];
  for (var i = 0; i < imgs.length; i++) {
    var im = imgs[i];
    if (im.complete && im.naturalWidth === 0 && im.getAttribute('src') && isExternal(im)) {
      fallback(im);
    }
  }
})();
