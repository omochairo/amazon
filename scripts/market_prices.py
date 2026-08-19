"""market_prices.py

#4007 follow-up 1 — 楽天/Yahoo の matched JSON 品質ゲート・価格解決ロジックの
共有モジュール。

背景 (#4007):
  ``build_post.py`` は毎ビルド ``data/raw/{rakuten,yahoo}_matched.json`` を
  読んで ``product.prices.{rakuten,yahoo}`` を上書きするが、
  ``build_feature_lists.py`` (/deals/ /cospa/ と年齢/テーマ hub) は記事 JSON の
  凍結値をそのまま使っていたため、記事ページと特集ページで楽天/Yahoo 価格が
  食い違い、停止した古い楽天価格が最安として残って /cospa/ の価格帯バケットが
  誤配置される事故があった。

  本モジュールは元々 ``build_post.py`` にあった quality gate 一式
  (``_matched_passes_quality`` とその依存関数・定数) を移設し、
  ``build_feature_lists.py`` / ``build_category_hubs.py`` からも同じ判定ロジック
  を使えるようにする。ロジック自体は一切変更していない (コピー drift を防ぐため
  build_post.py 側は本モジュールへの薄いエイリアスに変わる)。

Pure aggregation only — 外部 API 呼び出しなし・CLI なし。
"""

from __future__ import annotations

import html
import json
import pathlib
import re
from typing import Any

# 楽天/Yahoo cross-search 結果を本文の価格グリッドに流し込むときに、
# 検索ヒットしただけで実商品とかけ離れた item (ふるさと納税の高額品など) を
# 弾くためのガード閾値。Amazon 価格を anchor にする。
# Issue #1072 Phase 3-B (2026-05-31): jan_unknown 58 ASIN を救済するため
#   price band を [0.5, 2.0] → [0.4, 2.5] に、coverage を 0.7 → 0.5 に緩和。
#   dry-run (scripts/analyze_threshold_relaxation.py) で +25 件救済を確認、
#   FP の主因は閾値ではなく Mamimami Home 系 search_keyword の品質問題
#   (別 Issue で対処予定)。relax_both preset 採用。
PRICE_BAND_LOW = 0.4   # Amazon 価格の 40% 未満は除外
PRICE_BAND_HIGH = 2.5  # Amazon 価格の 250% 超は除外
# verified=False の existing として keep する場合でも、Amazon の 3.0x 超 / 1/3 未満
# の極端な乖離は別商品確定として丸ごと破棄し、検索フォールバックのみ残す。
# 例: B0F4X462WH (amazon 1579円) yahoo_matched が 14395 円 (9.12x) で Jules が同 URL
#     を埋めていたケース。確度低 badge では「クリックすれば確認できる」と誤読される
#     ので、リンク自体を消して「Yahoo!で検索 →」ボタンに置き換える。
PRICE_BAND_EXTREME = 3.0
# coverage ratio (hits / len(meaningful))。これ未満は borderline 扱いで
# verified=False に格下げ → ※確度低 badge + 検索 fallback 表示。
# 例: kw='Hape ビーズコインドロップス E0328' vs title='Hape ビーズコインドロップス
# E0327' は 2/3=0.67 で borderline (異モデル番号) として捕捉される。
COVERAGE_RATIO = 0.5
# search_keyword を title overlap 判定するときに、汎用すぎて根拠にならない語
GENERIC_TOKENS = frozenset({
    "おもちゃ", "知育玩具", "プレゼント", "誕生日", "ギフト",
    "木製", "木のおもちゃ", "セット", "玩具",
})

# 型番ハイフン揺れを吸収 (例: 'RD-6' vs 'RD−6' / 'EH-2310' vs 'EH−2310').
# title vs kw の substring 判定で false negative を防ぐ。Issue #1140 で
# B09BQMCSFL (kw 'RD-6' vs title 'RD−6') が descriptor 不一致と誤判定された対応。
_HYPHEN_VARIANTS_RE = re.compile(r"[‐‑‒–—―−ー]")
# 中黒 (半角 ﾟ・ U+FF65 / 全角 ・ U+30FB / 半角 ･ U+FF65) を 1 文字に統一。
# 'トイ・ストーリー' vs 'トイ･ストーリー' を同一視。
_MIDDOT_VARIANTS_RE = re.compile(r"[･・]")


def _normalize_for_match(s: str) -> str:
    if not s:
        return s
    s = html.unescape(s)  # '&amp;' → '&' 等の HTML entity を吸収
    s = _HYPHEN_VARIANTS_RE.sub("-", s)
    s = _MIDDOT_VARIANTS_RE.sub("・", s)
    return s


def _is_model_number(token: str) -> bool:
    """ASCII 英数の型番トークン (E3209 / E0328 等) か判定する。

    別 SKU 誤マッチ検出用。>=1 英字 + >=2 数字 + ASCII[英数ハイフン]のみ を満たす
    ものだけ型番とみなす。RD-6 のような 1 桁や 2025 (数字のみ) / Lon-Bi (数字無し)
    は除外し、明確なカタログ型番だけを対象にして false-trip を避ける。
    """
    if not re.fullmatch(r"[A-Za-z0-9\-]+", token):
        return False
    if not any(c.isalpha() for c in token):
        return False
    return sum(c.isdigit() for c in token) >= 2


def _compact_for_model_match(s: str) -> str:
    """型番比較用の正規化: hyphen variants を畳んでから空白/ハイフンを除去し大文字化。

    楽天/Yahoo は Amazon と型番の区切りを変えて載せる (`SMX310` / `SMX 310`、
    `CH-060` / `CH060`、`6381254A` / `CN−6381254−A`)。この比較を型番ガードと
    件数カウントの両方で使い、同じ関数内で判定が食い違わないようにする。
    """
    return re.sub(r"[-\s]", "", _normalize_for_match(s or "")).upper()


def _descriptor_hits_title(descriptor: str, title_norm: str, title_tokens_norm: list) -> bool:
    """Issue #1140: descriptor token が title に「実質的に」出現するか。

    1. 正規化後の substring 一致 (タッチペン → 'タッチペン...' を含む title)
    2. title token が descriptor の substring (タッチペン付 ⊃ タッチペン)
       → suffix 表記揺れ (付/版/セット) を吸収
    3. 先頭/末尾の punctuation を剥がして再判定 ('-Switch' → 'Switch')
    """
    d = _normalize_for_match(descriptor)
    if d in title_norm:
        return True
    for tt in title_tokens_norm:
        if len(tt) >= 3 and tt in d:
            return True
    d_stripped = d.strip("-_/.,;:!?()[]{}<>")
    if d_stripped and d_stripped != d and len(d_stripped) >= 3 and d_stripped in title_norm:
        return True
    return False


def load_matched_index(path: pathlib.Path) -> dict[str, dict[str, Any]]:
    """data/raw/{rakuten,yahoo}_matched.json を ``{asin: item}`` に展開。"""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    index: dict[str, dict[str, Any]] = {}
    for it in data.get("items", []):
        if not isinstance(it, dict):
            continue
        asin = it.get("matched_asin") or it.get("asin")
        if asin:
            index[asin] = it
    return index


def matched_passes_quality(
    matched: dict[str, Any],
    amazon_price: int,
    *,
    price_low: float = PRICE_BAND_LOW,
    price_high: float = PRICE_BAND_HIGH,
    coverage_ratio: float = COVERAGE_RATIO,
    hits_threshold_multi: int = 2,
) -> bool:
    """Phase 2 quality gate: 価格帯と検索語タイトル overlap で誤マッチを弾く。

    - Amazon 価格 (>0) を anchor に [price_low, price_high] 帯外を除外 (ふるさと納税対策)。
    - search_keyword のうち汎用語を除いた meaningful token が、matched title に
      閾値以上一致しているかを確認 (median band 選出後の無関係 hit 除外)。

    閾値は keyword-only 引数で上書き可能 (デフォルト = 本番 `PRICE_BAND_*` 定数)。
    analyze_threshold_relaxation の dry-run がこの 1 関数を直接呼ぶことで、
    判定ロジックのコピー drift (#2723) を構造的に排除する。
    """
    title = matched.get("title") or ""
    try:
        price = int(matched.get("price") or 0)
    except (TypeError, ValueError):
        price = 0
    if not title or price <= 0:
        return False

    if amazon_price > 0:
        if price < amazon_price * price_low:
            return False
        if price > amazon_price * price_high:
            return False

    kw = matched.get("search_keyword") or ""
    kw_tokens = [t for t in re.split(r"\s+", kw) if len(t) >= 2]

    # 型番ガード: search_keyword に ASCII 型番 (E3209 等) があるのに matched title に
    # 一つも存在しなければ別 SKU の誤マッチとして弾く。同ブランド別商品
    # (Hape レジカウンター E3209 vs ファーマーズマーケットの食べ物セット) が
    # カテゴリ語「ままごと」一致だけで通過し、quality_gate の reseller-pricing
    # check を誤発火させる事故 (B0CDTQWRN1) を source で断つ。
    title_compact = _compact_for_model_match(title)
    model_tokens = [t for t in kw_tokens if _is_model_number(t)]
    if model_tokens:
        if not any(_compact_for_model_match(m) in title_compact for m in model_tokens):
            return False

    meaningful = [t for t in kw_tokens if t not in GENERIC_TOKENS]
    if not meaningful:
        return True  # 区別語が無ければ cross-search 側 median band の選出を尊重
    title_norm = _normalize_for_match(title)

    def _token_hits_title(token: str) -> bool:
        """token が title に出現するか。

        型番だけは上の型番ガードと**同じ比較** (空白/ハイフン除去) を使う。
        揃えないと、同じ関数の中で同じ型番が「ガードでは一致・件数では不一致」に
        なる。実際に楽天/Yahoo は型番の区切りを Amazon と変えて載せるため
        (`SMX310` を `SMX 310`、`CH-060` を `CH060`、`6381254A` を `CN−6381254−A`)、
        正しいマッチが hits に数えられず verified=False へ落ちていた。
        実測 (2026-08-19): 未検証リンク 864 件中 9 件がこれで、いずれも目視で
        同一商品。逆に落ちるものは 0 件。
        """
        if _normalize_for_match(token) in title_norm:
            return True
        if _is_model_number(token):
            return _compact_for_model_match(token) in title_compact
        return False

    hits = sum(1 for t in meaningful if _token_hits_title(t))
    threshold = hits_threshold_multi if len(meaningful) >= 2 else 1
    if hits < threshold:
        return False
    # 絶対 hits 数を満たしても、meaningful 全体に占める割合が低いマッチは
    # シリーズ違い/別モデルの誤マッチが多いため verified=False に格下げ。
    if hits / len(meaningful) < coverage_ratio:
        return False
    # Issue #1140: 非ブランド descriptor 一致を必須化。
    # search_keyword は extract_search_keyword で「brand head (先頭 1-2 token) +
    # descriptor」の構造になる。Mamimami Home のようなカテゴリ横断ブランドだと
    # brand head だけが title と一致して通過し、descriptor (ティッシュ/積み木/楽器)
    # が別商品とずれていても FP として救済されてしまう (relax_both で 4 件確認)。
    # kw と matched title の共通 leading token prefix を brand head とみなし、
    # それ以外の descriptor から最低 1 token は title に含まれることを要求する。
    title_tokens = [t for t in re.split(r"\s+", title) if t]
    prefix_len = 0
    for kt, tt in zip(kw_tokens, title_tokens):
        if _normalize_for_match(kt) == _normalize_for_match(tt):
            prefix_len += 1
        else:
            break
    if prefix_len >= 1:
        descriptor = [
            t for t in kw_tokens[prefix_len:]
            if len(t) >= 2 and t not in GENERIC_TOKENS
        ]
        if descriptor:
            title_tokens_norm = [_normalize_for_match(t) for t in title_tokens if len(t) >= 2]
            if not any(_descriptor_hits_title(d, title_norm, title_tokens_norm) for d in descriptor):
                return False
    return True


def resolve_price(existing_price: int, matched: dict[str, Any] | None, amazon_price: int) -> int:
    """楽天/Yahoo の「価格数値」を優先順位に沿って 1 つ解決する (#4007 follow-up 1)。

    ``build_post._attach_market_prices`` の価格解決優先順位のうち、価格の
    数値だけを再現した軽量版 (verified フラグ・search_url フォールバック・
    確度低 badge 用の分岐は build_post 側の責務のまま持たせる)。

    優先順位:
      1. ``matched`` があり ``matched_passes_quality`` を通る →
         matched の price (int 化失敗は 0)。
      2. そうでなく ``existing_price`` が正で、かつ Amazon 価格に対して
         extreme outlier (``PRICE_BAND_EXTREME`` = 3.0x 超 / 1/3 未満) でない
         → ``existing_price`` をそのまま維持。
      3. それ以外 (matched が gate 落ち かつ existing が extreme、または
         existing が無い) → 0 (= 最安候補から外れる)。
    """
    if matched and matched_passes_quality(matched, amazon_price):
        try:
            return int(matched.get("price") or 0)
        except (TypeError, ValueError):
            return 0

    if existing_price > 0:
        extreme = (
            amazon_price > 0 and (
                existing_price > amazon_price * PRICE_BAND_EXTREME
                or existing_price < amazon_price / PRICE_BAND_EXTREME
            )
        )
        if not extreme:
            return existing_price

    return 0
