"""楽天ランキング/検索の未マッチ item を JAN 経由で新規 Amazon ASIN に解決する。

Issue #810 Phase 1 — keyword × top100 の構造的上限を突破する第一歩。
Issue #810 Phase 2 — 探索源を楽天 Ichiba Search にも拡張 (``--search-in``)。
  Creator API には GetBrowseNodes も searchItems(browseNodeId) も無く、Amazon
  ベストセラー経由の探索は API では不可能。代わりに楽天 Search (無料・高 quota・
  スクレイピング不要) の JAN を mine することで、Amazon の keyword×top100 の外に
  ある売れ筋を発見する。ランキング 30 件と同じ Option A 解決経路を再利用する。

背景:
  ``fetch_rakuten.py`` は楽天おもちゃランキングの各 item を既存記事 ASIN に
  マッチングするが、**未マッチ** (=まだ記事化されていない売れ筋) は
  ``data/raw/rakuten_ranking.json`` に ``matched_asin: null`` で残る。本スクリプトは
  その未マッチ item (+ ``--search-in`` 指定時は ``data/raw/rakuten.json`` の Search
  items) から JAN を取り出し、**Creator API searchItems(keyword=JAN)** の応答を
  ``itemInfo.externalIds.eans`` で照合して新規 ASIN を解決する (案A)。

  解決済 ASIN は ``fetch_amazon.py --asin <CSV> --competitors-only`` に渡して
  per_asin スナップショットへ backfill する (ライブの amazon.json プールには触れない)。

🚨 使用 API: **Amazon Creator API (creatorsapi.amazon)**。PA-API 5 ではない。
  - searchItems の ``itemInfo.externalIds`` は valid な resource (fetch_amazon の
    SEARCH_ITEM_RESOURCES に既存。04-validate の dry-run gate #785 が検証済)。
  - [[feedback-omochairo-creators-api-deliveryinfo-trap]]

出力:
  - ``data/raw/ranking_sniper_asins.csv``    : 解決済の新規 ASIN を CSV 1 行で出力
                                               (workflow が fetch_amazon --asin に渡す)
  - ``data/raw/_ranking_resolved_manifest.json`` : 可観測性 (resolved / unresolved /
                                               already-covered の内訳)
  - ``data/raw/ranking_pool.json``           : ランキング由来・未記事化 ASIN の永続
                                               候補リスト (#810 Phase 1.5)。03-invoke-jules
                                               の pick-asin が amazon.json 候補に union し、
                                               記事化済になるまで残す (daily の amazon.json
                                               上書きで消えない別経路)。

Issue #2818 対策4c (title fuzzy match) + Issue #3332 N5-V1 (画像照合ゲート):
  JAN 抽出そのものが不可能な item (未マッチの約7割) には JAN 経由の解決経路が
  届かない。``--enable-title-fuzzy`` (既定 off・オプトイン) を指定すると、
  正規化タイトルで Creator API searchItems し、3 ガードレール (brand一致必須 /
  型番トークン一致 / 楽天価格帯±40%以内) を満たした候補のみ ``match_method:
  "title_fuzzy"`` として解決する。誤マッチ (=誤アフィリエイトリンク) を防ぐため
  precision 優先・recall 犠牲を明示方針とする (issue #2818 2026-07-11 設計確定)。

  さらに ``--vision-embed-url`` を与えると、楽天商品画像 vs 候補商品画像の
  embedding cosine 類似度 (``vision_match.py``) を算出する。``--vision-gate-mode``
  既定は "shadow" (image_sim を manifest に記録するのみ・採否は変えない)。
  閾値較正後にオーナー判断で "enforce" (閾値未満を不採用) に切り替える設計
  (#3332 N5 設計コメント: shadow 実測 → 分布から閾値較正)。K8 への画像埋め込み
  サービスの実デプロイは本 PR のスコープ外 (--vision-embed-url 未指定なら
  vision 完全無効・title fuzzy はガードレール3点のみで判定する)。

環境変数: AMAZON_CREATORS_* (creators_api_client.py 参照) / VISION_EMBED_URL (任意)
"""
import os
import re
import sys
import json
import time
import pathlib
import logging
import argparse
import datetime

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if THIS_DIR not in sys.path:
    sys.path.insert(0, THIS_DIR)

from fetch_rakuten import _extract_jan_from_item  # noqa: E402
from brand_normalizer import normalize as _normalize_brand  # noqa: E402
from genre_gate import classify_genre  # noqa: E402
# browse_nodes の抽出ロジックは fetch_amazon 側の実装を再利用する (同等ロジックを
# 書き直すと ancestor チェーンの遡り方が二重管理になり、#2823 のジャンル判定が
# 経路ごとにズレるため)。fetch_amazon の import は副作用なし (module 直下は
# 定数定義と logging 設定のみ)。
from fetch_amazon import extract_browse_nodes  # noqa: E402
from vision_match import (  # noqa: E402
    DEFAULT_MIN_SCORE as VISION_DEFAULT_MIN_SCORE,
    image_similarity,
    passes_vision_gate,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("resolve_ranking_asins")

# searchItems で JAN→ASIN を引くための最小 resources。externalIds で JAN を
# 照合し、title はログ可読性のため。fetch_amazon.SEARCH_ITEM_RESOURCES と矛盾
# しない部分集合 (dry-run gate が本体の方を検証する)。
#
# browseNodeInfo.browseNodes / .ancestor は #2823 のジャンルゲートを本スクリプトの
# 解決経路にも効かせるために必要 (ancestor が無いと root が全件 null になり
# classify_genre が indeterminate 直行で fail-open のまま = ゲートが成立しない)。
# 両 resource とも fetch_amazon.SEARCH_ITEM_RESOURCES に既存で、04-validate の
# dry-run gate (#785) が実 API で検証済み。
RESOLVE_RESOURCES = [
    "itemInfo.title",
    "itemInfo.externalIds",
    "browseNodeInfo.browseNodes",
    "browseNodeInfo.browseNodes.ancestor",
]

# #2818 対策4c: title fuzzy 検索専用の resources。JAN 解決 (RESOLVE_RESOURCES) には
# 影響させず、fuzzy 経路だけ価格・画像も要求する (既存 JAN 解決の payload/挙動を
# 変えないため、あえて別定数にしている)。images.primary.large / offersV2.listings.price
# は fetch_amazon.SEARCH_ITEM_RESOURCES で既に dry-run gate 検証済 (Issue #785)。
FUZZY_MATCH_RESOURCES = RESOLVE_RESOURCES + [
    "offersV2.listings.price",
    "images.primary.large",
]

# 型番トークン: 英数字4文字以上の連続で、かつ**数字を1文字以上含む**もの
# (#2818 対策4c ガードレール2)。数字を必須にするのは、Latin 表記のブランド名
# ("LEGO"/"BANDAI" 等) が型番トークンとして誤って一致し、同ブランドの別商品同士を
# 取り違えるのを防ぐため (brand_match は別途 §_brand_of で判定済み。型番トークンは
# 「モデルを一意に識別する記号」= 数字を含む型番/JAN 断片/ASIN 断片を担うべき)。
# 純アルファベットの共通語 (DUPLO 等) を落とす分 recall は下がるが、対策4c は
# precision 優先・recall 犠牲を明示方針としている (issue #2818 2026-07-11)。
_MODEL_TOKEN_RE = re.compile(r"[A-Za-z0-9]{4,}")
_HAS_DIGIT_RE = re.compile(r"\d")

# 楽天タイトルの装飾的な角括弧・記号 (【送料無料】【あす楽】等) を検索キーワードから除く。
_TITLE_DECORATION_RE = re.compile(r"[【】\[\]()（）]")


def _safe_get(obj, *attrs, default=None):
    cur = obj
    for a in attrs:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(a)
    return cur if cur is not None else default


def _external_ids_of(item: dict) -> list:
    """searchItems 応答 item の外部識別子 (eans + upcs) をまとめて返す。

    #2818 対策3: eans のみ見ていると UPC-A で登録された商品 (isbns/upcs 側にしか
    識別子が無い) を取りこぼす。fetch_amazon.extract_jan の eans→isbns→upcs
    フォールバックと整合させ、eans/upcs の双方を照合対象にする。
    """
    out = []
    for key in ("eans", "upcs"):
        vals = _safe_get(item, "itemInfo", "externalIds", key, "displayValues", default=[])
        if isinstance(vals, list):
            out.extend(str(v).strip() for v in vals)
    return out


# --------------------------------------------------------------------------
# #2823 ジャンルゲート (解決経路版)
#
# fetch_amazon.py のゲートは search sweep 経路 (items_for_jules) にしか掛かって
# おらず、本スクリプト → ``fetch_amazon.py --asin ... --competitors-only`` の
# sniper 経路には一切効いていなかった。その穴を塞ぐ。
#
# 判定の向きはオーナー方針で確定 (2026-07-19):
#   - "flag" のみ不採用
#   - "indeterminate" (browse_nodes 欠落等) は **必ず通す** = fail-open 維持。
#     ここを fail-closed にすると、水遊び・プール等の対象内商品や browse_nodes を
#     返さない商品まで巻き込んで潰すため絶対に変えない。
#   - プール・水遊びおもちゃは対象内 (夏季の季節需要として歓迎)。排除するのは
#     ALLOWED_ROOTS から完全に外れた「おもちゃ外」だけ。
# --------------------------------------------------------------------------

def _genre_verdict_for_item(item: dict):
    """searchItems 応答 item から (verdict, roots) を返す。

    verdict は classify_genre と同じ "pass" | "flag" | "indeterminate"。
    roots は判定に使った実カテゴリノードの root カテゴリ名 (manifest 記録用)。
    """
    item = item or {}
    nodes = extract_browse_nodes(item)
    asin = (item.get("asin") or "").strip()
    verdict, cat_nodes = classify_genre(nodes, asin)
    roots = sorted({nd.get("root") for nd in cat_nodes if nd.get("root")})
    return verdict, roots


def _is_genre_rejected(verdict: str) -> bool:
    """不採用にすべき verdict か。"flag" のみ True (indeterminate は fail-open)。"""
    return verdict == "flag"


def _genre_drop_entry(item: dict, rank, title: str, roots, match_method: str) -> dict:
    """ゲートで落とした候補の記録 1 件分 (manifest 用)。"""
    return {
        "asin": ((item or {}).get("asin") or "").strip(),
        "rank": rank,
        "title": (title or "")[:40],
        "roots": roots,
        "match_method": match_method,
    }


def resolve_jan_to_item(api, jan: str, search_index: str = "All") -> dict:
    """Creator API searchItems(keyword=JAN) で JAN 一致 item を 1 件返す (案A の本体)。

    ジャンルゲートには ASIN だけでなく応答 item 全体 (browse_nodes) が要るため、
    照合結果を item のまま返す層を分けている。一致なしは {} を返す。
    ``resolve_jan_to_asin`` は本関数の薄いラッパで、既存の呼び出し規約
    (ASIN 文字列を返す) を維持する。
    """
    try:
        res = api.search_items(
            keywords=jan, search_index=search_index,
            item_count=10, item_page=1, resources=RESOLVE_RESOURCES,
        )
    except Exception as e:
        logger.warning(f"  searchItems failed for JAN {jan}: {e}")
        return {}
    items = _safe_get(res, "searchResult", "items", default=[]) or []
    for it in items:
        if jan in _external_ids_of(it):
            if (it.get("asin") or "").strip():
                return it
    return {}


def resolve_jan_to_asin(api, jan: str, search_index: str = "All") -> str:
    """Creator API searchItems(keyword=JAN) で JAN 一致 ASIN を 1 件返す (案A)。

    externalIds (eans/upcs) に問い合わせ JAN を含む item の ASIN を返す。JAN は
    世界一意なので応答 top10 のどれかが一致すれば確定。一致なしは "" を返す。

    #2818 対策2: search_index は "Toys" 固定だとベビー用品・ホビー等 Amazon 側で
    別カテゴリ登録された商品を取りこぼすため、既定を "All" に緩和する。JAN 完全一致で
    照合するのでカテゴリを広げても誤マッチのリスクは無い (物理的に同一商品であることを
    バーコードが保証する)。
    """
    return (resolve_jan_to_item(api, jan, search_index=search_index).get("asin") or "").strip()


# --------------------------------------------------------------------------
# #2818 対策4c: title fuzzy match (JAN 抽出不可能な item への唯一の残経路)
# --------------------------------------------------------------------------

def _normalize_title_for_search(title: str) -> str:
    """検索キーワード用にタイトルを正規化する。

    楽天タイトルは【送料無料】【あす楽】等の装飾的な角括弧・記号を大量に含み、
    そのまま Creator API に投げるとノイズで検索精度が落ちる。角括弧・記号を除去し
    空白を1つに畳んで長さを保守的に切り詰める (過剰に長いクエリはヒット率を
    下げるため)。
    """
    if not title:
        return ""
    t = _TITLE_DECORATION_RE.sub(" ", title)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:120]


def _extract_model_tokens(text: str) -> set:
    """型番トークン集合 (#2818 対策4c ガードレール: 型番一致)。

    英数字4文字以上の連続のうち **数字を含むもの** だけを返す (Latin ブランド名の
    誤一致を防ぐため。上の ``_MODEL_TOKEN_RE`` コメント参照)。
    """
    return {
        t.upper() for t in _MODEL_TOKEN_RE.findall(text or "")
        if _HAS_DIGIT_RE.search(t)
    }


def _brand_of(text: str) -> str:
    """brand_taxonomy 経由でテキストからブランド canonical 名を抽出する (#2818 対策4c ガードレール: brand一致)。

    ``brand_normalizer.normalize`` はエイリアスの部分文字列一致 (fuzzy) にも対応して
    いるため、タイトル全文をそのまま渡してよい。ブランドを特定できない場合は ""
    (unknown) を返す — brand_match は「双方が unknown」では成立させない
    (呼び出し側で明示的に判定する)。
    """
    if not text:
        return ""
    b = _normalize_brand(text)
    return b.canonical if b.match_type != "unknown" else ""


def _candidate_price(item: dict) -> float:
    """searchItems 応答 item の Buy Box 価格 (円) を返す。無ければ 0.0。

    ``fetch_amazon.extract_price`` と同一の読み出しパス (``offersV2.listings[0]
    .price.money.amount``) をこのファイル内で完結させる (RESOLVE_RESOURCES と同じく
    fetch_amazon.py への依存を増やさない設計方針)。
    """
    listings = _safe_get(item, "offersV2", "listings", default=[]) or []
    if listings:
        money = _safe_get(listings[0], "price", "money", default={}) or {}
        try:
            return float(money.get("amount", 0) or 0)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def _candidate_image_url(item: dict) -> str:
    """searchItems 応答 item のメイン商品画像 URL を返す。無ければ空文字。"""
    return (
        _safe_get(item, "images", "primary", "large", "url")
        or _safe_get(item, "images", "primary", "medium", "url")
        or ""
    )


def search_title_fuzzy_candidates(
    api, title: str, search_index: str = "All", item_count: int = 10,
) -> list:
    """#2818 対策4c: 正規化タイトルで Creator API searchItems し候補 item リストを返す。"""
    query = _normalize_title_for_search(title)
    if not query:
        return []
    try:
        res = api.search_items(
            keywords=query, search_index=search_index,
            item_count=item_count, item_page=1, resources=FUZZY_MATCH_RESOURCES,
        )
    except Exception as e:
        logger.warning(f"  title-fuzzy searchItems failed for {query!r}: {e}")
        return []
    items = _safe_get(res, "searchResult", "items", default=[]) or []
    return items if isinstance(items, list) else []


def evaluate_title_fuzzy_candidate(
    rakuten_title: str, rakuten_price, candidate: dict, price_tolerance: float = 0.4,
) -> dict:
    """#2818 対策4c ガードレール3点を評価する。3条件すべて満たさない候補は不採用 (fail-closed)。

    ガードレール: (i) brand 一致必須 (ii) 型番トークン一致 (iii) 楽天価格帯 ±40%以内。
    false positive は誤アフィリエイトリンクに直結するため precision 優先・recall 犠牲を
    明示方針とする (issue #2818 2026-07-11 設計確定)。判定根拠つき dict を返し、
    ``_ranking_resolved_manifest.json`` にそのまま残して人が spot check できるようにする。
    """
    cand_title = _safe_get(candidate, "itemInfo", "title", "displayValue", default="") or ""

    rakuten_brand = _brand_of(rakuten_title)
    candidate_brand = _brand_of(cand_title)
    brand_match = bool(rakuten_brand) and rakuten_brand == candidate_brand

    rakuten_tokens = _extract_model_tokens(rakuten_title)
    candidate_tokens = _extract_model_tokens(cand_title)
    common_tokens = rakuten_tokens & candidate_tokens
    model_token_match = bool(common_tokens)

    cand_price = _candidate_price(candidate)
    price_ok = False
    if rakuten_price and cand_price:
        lo = rakuten_price * (1 - price_tolerance)
        hi = rakuten_price * (1 + price_tolerance)
        price_ok = lo <= cand_price <= hi

    return {
        "asin": (candidate.get("asin") or "").strip(),
        "candidate_title": cand_title,
        "brand_match": brand_match,
        "rakuten_brand": rakuten_brand,
        "candidate_brand": candidate_brand,
        "model_token_match": model_token_match,
        "common_tokens": sorted(common_tokens),
        "price_ok": price_ok,
        "rakuten_price": rakuten_price,
        "candidate_price": cand_price,
        "passed_guardrails": brand_match and model_token_match and price_ok,
    }


def _collect_unmatched_no_jan(ranking_items: list) -> list:
    """未マッチ・かつ JAN も抽出できない item を rank 順で返す (#2818 対策4c 対象母集団)。

    JAN 抽出できる item は既存の JAN 経由解決 (``_collect_unmatched_jans``) が優先的に
    処理するため、title fuzzy はその外側 (JAN 抽出そのものが構造的に不可能な残り) だけを
    対象にする (issue #2818 実測: 未マッチ 27〜28 件中 JAN 抽出可能は 6〜8 件のみ)。
    """
    return [
        it for it in ranking_items
        if not it.get("matched_asin") and not _extract_jan_from_item(it) and it.get("title")
    ]


def resolve_title_fuzzy(
    items: list,
    api,
    covered: set,
    seen_asins: set,
    search_index: str = "All",
    price_tolerance: float = 0.4,
    item_count: int = 10,
    limit: int = 0,
    sleep: float = 1.1,
    vision_client=None,
    vision_gate_mode: str = "shadow",
    vision_min_score: float = VISION_DEFAULT_MIN_SCORE,
    enable_genre_gate: bool = True,
    genre_dropped: list = None,
) -> dict:
    """#2818 対策4c: JAN 抽出不可能な item を title fuzzy + vision ゲートで解決する (オプトイン)。

    ``vision_gate_mode``:
      - ``"shadow"`` (既定): ``image_sim`` を manifest に記録するだけで採否には使わない
        (ガードレール3点のみで判定)。#3332 N5 設計: 閾値較正前は採否を変えない
        (「初回は採否を変えず image_sim スコアを記録のみ → 実測分布を見て閾値確定」)。
      - ``"enforce"``: ``image_sim >= vision_min_score`` を採用の追加必須条件にする
        (score が None = 未評価/取得失敗の場合も fail-closed で不採用)。

    ``vision_client`` が None (=--vision-embed-url 未指定) の場合、vision_gate_mode に
    関わらず image_sim は常に None (未評価) になり、"enforce" では全件不採用になる —
    実サービス未デプロイの間は "shadow" (既定) で運用すること。

    ``enable_genre_gate=True`` (既定) のとき、ガードレール通過後・採用確定前に
    #2823 ジャンルゲートを掛け、verdict が "flag" の候補は採用しない (次の候補へ
    進む)。落とした候補は ``genre_dropped`` リストに追記する (呼び出し側が
    manifest に載せる)。
    """
    if genre_dropped is None:
        genre_dropped = []
    candidates_before_limit = len(items)
    if limit and limit > 0:
        items = items[:limit]
    resolved, rejected = [], []
    seen_asins = set(seen_asins)

    for i, item in enumerate(items):
        title = item.get("title") or ""
        rank = item.get("rank")
        rakuten_price = item.get("price") or 0
        rakuten_image = item.get("image") or ""

        raw_candidates = search_title_fuzzy_candidates(
            api, title, search_index=search_index, item_count=item_count
        )
        best = None
        best_raw = None
        for cand in raw_candidates:
            evald = evaluate_title_fuzzy_candidate(
                title, rakuten_price, cand, price_tolerance=price_tolerance
            )
            if not evald["passed_guardrails"]:
                continue
            asin = evald["asin"]
            if not asin or asin.upper() in covered or asin in seen_asins:
                continue
            # #2823: ガードレール通過後・採用確定前にジャンルゲート。"flag" は
            # 不採用にして次候補へ進む (covered/seen と同じ扱い)。"indeterminate"
            # は fail-open で通す。
            if enable_genre_gate:
                verdict, roots = _genre_verdict_for_item(cand)
                if _is_genre_rejected(verdict):
                    genre_dropped.append(
                        _genre_drop_entry(cand, rank, title, roots, "title_fuzzy")
                    )
                    continue
            best, best_raw = evald, cand
            break  # Creator API の関連度順を信頼し、最初にガードレール通過した候補を採用

        if best is None:
            rejected.append({"rank": rank, "title": title, "reason": "no_guardrail_pass"})
            if sleep and i < len(items) - 1:
                time.sleep(sleep)
            continue

        image_sim = None
        if vision_client is not None:
            candidate_image = _candidate_image_url(best_raw or {})
            image_sim = image_similarity(vision_client, rakuten_image, candidate_image)

        vision_gate_passed = True
        if vision_gate_mode == "enforce":
            vision_gate_passed = passes_vision_gate(image_sim, vision_min_score)

        entry = {
            "rank": rank,
            "title": title,
            "asin": best["asin"],
            "match_method": "title_fuzzy",
            "brand_match": best["brand_match"],
            "rakuten_brand": best["rakuten_brand"],
            "candidate_brand": best["candidate_brand"],
            "model_token_match": best["model_token_match"],
            "common_tokens": best["common_tokens"],
            "price_ok": best["price_ok"],
            "rakuten_price": best["rakuten_price"],
            "candidate_price": best["candidate_price"],
            "image_sim": image_sim,
            "vision_gate_mode": vision_gate_mode,
            "vision_gate_passed": vision_gate_passed,
        }
        if vision_gate_passed:
            resolved.append(entry)
            seen_asins.add(best["asin"])
        else:
            entry["reason"] = "vision_gate_rejected"
            rejected.append(entry)

        if sleep and i < len(items) - 1:
            time.sleep(sleep)

    return {
        "title_fuzzy_candidates_before_limit": candidates_before_limit,
        "title_fuzzy_input": len(items),
        "title_fuzzy_resolved": resolved,
        "title_fuzzy_rejected": rejected,
    }


def _collect_unmatched_jans(ranking_items: list) -> list:
    """未マッチ ranking item から (jan, rank, title) を rank 順・JAN 重複排除で抽出。"""
    seen = set()
    out = []
    for it in ranking_items:
        if it.get("matched_asin"):
            continue
        jan = _extract_jan_from_item(it)
        if jan and jan not in seen:
            seen.add(jan)
            out.append((jan, it.get("rank"), it.get("title")))
    return out


# 記事スラッグ末尾の 10 文字 ASIN サフィックス。fetch_amazon._load_existing_article_asins
# と同一規約 (B0... / ISBN-10 数字 ASIN 両対応、.enrichment/.seo/.quality は除外)。
_ASIN_SUFFIX_RE = re.compile(r"-([A-Z0-9]{10})$")


def _load_article_asins(articles_dir: str) -> set:
    """``data/articles`` のスラッグ末尾 ASIN 集合 (= **記事化済**)。

    ranking_pool の prune に使う。per_asin は **含めない**: per_asin に
    backfill 済でもまだ記事が無い ASIN は pool に残すべきだから (#810 Phase 1.5)。
    """
    covered = set()
    adir = pathlib.Path(articles_dir)
    if adir.is_dir():
        for p in adir.glob("*.json"):
            stem = p.stem
            if stem.endswith((".enrichment", ".seo", ".quality")):
                continue
            m = _ASIN_SUFFIX_RE.search(stem)
            if m:
                covered.add(m.group(1))
    return covered


def _load_per_asin_asins(per_asin_root: str) -> set:
    """``data/raw/per_asin/<ASIN>/`` ディレクトリ名の ASIN 集合 (= snapshot 済)。"""
    out = set()
    proot = pathlib.Path(per_asin_root)
    if proot.is_dir():
        for d in proot.iterdir():
            if d.is_dir():
                out.add(d.name.upper())
    return out


def _load_covered_asins(articles_dir: str, per_asin_root: str) -> set:
    """記事化 ∪ per_asin に存在する ASIN 集合 (再フェッチ不要の判定用; resolve step)。"""
    return _load_article_asins(articles_dir) | _load_per_asin_asins(per_asin_root)


def update_ranking_pool(existing_asins, new_asins, article_covered) -> list:
    """ranking_pool の asins を再構築して返す (#810 Phase 1.5)。

    Jules の記事化プールに渡す「ランキング由来・未記事化」ASIN の永続リスト。
    既存 pool から **記事化済** (article_covered) を除き、今回 resolved を末尾に
    追加して順序保持 dedup する。per_asin に backfill 済でも記事が無ければ残す
    (article_covered のみで prune)。03-invoke-jules の pick-asin がこの asins を
    amazon.json 候補に union して記事化する。
    """
    covered_u = {a.upper() for a in article_covered}
    merged, seen = [], set()
    for a in list(existing_asins) + list(new_asins):
        if not isinstance(a, str) or not a.strip():
            continue
        au = a.upper()
        if au in covered_u or au in seen:
            continue
        seen.add(au)
        merged.append(a)
    return merged


def resolve_ranking_asins(
    ranking_items, api, covered, limit=0, search_index="All", sleep=1.1,
    enable_title_fuzzy=False,
    title_fuzzy_limit=0,
    title_fuzzy_item_count=10,
    title_fuzzy_price_tolerance=0.4,
    vision_client=None,
    vision_gate_mode="shadow",
    vision_min_score=None,
    enable_genre_gate=True,
):
    """純粋ロジック: 未マッチ JAN を解決し manifest dict を返す (テスト driver)。

    ``enable_title_fuzzy=True`` (既定 False・オプトイン) を渡すと、JAN 経由の
    解決に加えて #2818 対策4c (title fuzzy match + vision ゲート) も実行し、結果を
    manifest の ``"title_fuzzy"`` キーと ``new_asins`` に merge する。既定 False では
    このファイルの既存の挙動を一切変えない (#3332 スコープ: 既定挙動を後退させない)。

    ``enable_genre_gate=True`` (既定) のとき、JAN 経路・title fuzzy 経路の**両方**で
    Amazon 応答 item に #2823 ジャンルゲートを掛け、"flag" の候補を ``resolved`` /
    ``new_asins`` に載せない (= ranking_pool にも入らない)。``False`` で無効化
    (``--no-genre-gate``)。
    """
    genre_dropped = []
    jans = _collect_unmatched_jans(ranking_items)
    # #2818 対策0: --limit で切り詰める前の候補プール総数を記録する。旧実装は
    # limit 適用後の len(jans) しか manifest に残さず、smoke run (--limit 1) の
    # ログが「候補は1件しかなかった」ように誤読された (実際は切り詰めの結果)。
    jan_candidates_before_limit = len(jans)
    if limit and limit > 0:
        jans = jans[:limit]
    resolved, unresolved, skipped = [], [], []
    seen_asins = set()
    for i, (jan, rank, title) in enumerate(jans):
        matched = resolve_jan_to_item(api, jan, search_index=search_index)
        asin = (matched.get("asin") or "").strip()
        # #2823: JAN 一致 item を採用確定する前にジャンルゲート。"flag" は resolved に
        # 積まず genre_gate_dropped に回す (unresolved とは別枠 — 「解決できなかった」
        # ではなく「解決できたが対象ジャンル外」なので混ぜると再試行判断を誤らせる)。
        gate_verdict, gate_roots = ("indeterminate", [])
        if asin and enable_genre_gate:
            gate_verdict, gate_roots = _genre_verdict_for_item(matched)
        if not asin:
            unresolved.append({"jan": jan, "rank": rank, "title": title})
        elif _is_genre_rejected(gate_verdict):
            entry = _genre_drop_entry(matched, rank, title, gate_roots, "jan")
            entry["jan"] = jan
            genre_dropped.append(entry)
        elif asin.upper() in covered:
            skipped.append({"jan": jan, "asin": asin, "rank": rank})
        elif asin in seen_asins:
            pass  # 別 JAN が同 ASIN に解決 — 重複は無視
        else:
            seen_asins.add(asin)
            resolved.append({"jan": jan, "asin": asin, "rank": rank, "title": title})
        if sleep and i < len(jans) - 1:
            time.sleep(sleep)

    manifest = {
        "jan_candidates_before_limit": jan_candidates_before_limit,
        "input_unmatched_jans": len(jans),
        "resolved": resolved,
        "unresolved": unresolved,
        "skipped_already_covered": skipped,
        "new_asins": [r["asin"] for r in resolved],
    }

    if enable_title_fuzzy:
        vmin = vision_min_score if vision_min_score is not None else VISION_DEFAULT_MIN_SCORE
        no_jan_items = _collect_unmatched_no_jan(ranking_items)
        fuzzy_result = resolve_title_fuzzy(
            no_jan_items, api, covered=covered, seen_asins=seen_asins,
            search_index=search_index, price_tolerance=title_fuzzy_price_tolerance,
            item_count=title_fuzzy_item_count, limit=title_fuzzy_limit, sleep=sleep,
            vision_client=vision_client, vision_gate_mode=vision_gate_mode,
            vision_min_score=vmin,
            enable_genre_gate=enable_genre_gate, genre_dropped=genre_dropped,
        )
        manifest["title_fuzzy"] = fuzzy_result
        fuzzy_new_asins = [e["asin"] for e in fuzzy_result["title_fuzzy_resolved"]]
        manifest["new_asins"] = manifest["new_asins"] + fuzzy_new_asins

    # #2823: 両経路のゲート除外をまとめて記録する (fuzzy 実行後に確定するのでここ)。
    manifest["genre_gate_enabled"] = bool(enable_genre_gate)
    manifest["genre_gate_dropped"] = len(genre_dropped)
    manifest["genre_gate_dropped_items"] = genre_dropped

    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--in", dest="in_path", default="data/raw/rakuten_ranking.json")
    parser.add_argument("--search-in", dest="search_in_path", default="",
                        help="Optional Rakuten Ichiba Search pool (data/raw/rakuten.json). #810 Phase 2: "
                             "その items の itemCaption/title からも JAN を mine し、ランキング 30 件の外の "
                             "売れ筋まで新規 ASIN 解決の対象を広げる。空文字なら無効 (ランキングのみ)。")
    parser.add_argument("--out", default="data/raw/")
    parser.add_argument("--articles-dir", default="data/articles")
    parser.add_argument("--per-asin-root", default="data/raw/per_asin")
    parser.add_argument("--pool", default="data/raw/ranking_pool.json",
                        help="Persistent ranking-discovered ASIN pool consumed by 03-invoke-jules pick-asin (#810 Phase 1.5).")
    parser.add_argument("--search-index", default="All",
                        help="#2818 対策2: 既定 All。JAN 完全一致で照合するため Toys 固定だと "
                             "ベビー/ホビー等の別カテゴリ登録商品を取りこぼす。")
    parser.add_argument("--limit", type=int, default=0,
                        help="Resolve at most N unmatched JANs (0 = all). Use --limit 1 for a single-JAN dry-run verification of Option A.")
    parser.add_argument("--sleep", type=float, default=1.1,
                        help="Seconds between searchItems calls (Creator API TPS safety).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Resolve and log results but write no output files. For Actions verification of Option A.")
    parser.add_argument("--no-genre-gate", dest="genre_gate", action="store_false",
                        help="#2823: 解決した候補への知育玩具ジャンル検査を無効化する "
                             "(既定は有効)。ゲートは verdict が flag の候補だけを落とし、"
                             "indeterminate (browse_nodes 欠落) は fail-open で通す。"
                             "緊急時の退避経路であり、常用しないこと。")
    parser.add_argument("--enable-title-fuzzy", action="store_true",
                        help="#2818 対策4c: JAN 抽出不可能な item に title fuzzy 解決を有効化する "
                             "(既定 off。既存の JAN 解決挙動は変えないオプトイン)。")
    parser.add_argument("--title-fuzzy-limit", type=int, default=0,
                        help="title fuzzy で解決を試みる item 数の上限 (0 = 全件)。")
    parser.add_argument("--title-fuzzy-item-count", type=int, default=10,
                        help="title fuzzy searchItems 1 回あたりの取得候補数。")
    parser.add_argument("--title-fuzzy-price-tolerance", type=float, default=0.4,
                        help="#2818 対策4c ガードレール: 楽天価格からの許容乖離率 (既定 ±40%%)。")
    parser.add_argument("--vision-embed-url", default=os.environ.get("VISION_EMBED_URL", ""),
                        help="#3332 N5-V1: 画像埋め込みサービスの URL。空文字なら vision 完全無効 "
                             "(title fuzzy はガードレール3点のみで判定)。実サービスは本 PR のスコープ外の "
                             "K8 デプロイ待ち — 較正が終わるまでは未設定のままでよい。")
    parser.add_argument("--vision-gate-mode", choices=("shadow", "enforce"), default="shadow",
                        help="shadow (既定): image_sim を manifest に記録するのみで採否に使わない "
                             "(閾値較正用)。enforce: image_sim < --vision-min-score の候補を不採用にする。")
    parser.add_argument("--vision-min-score", type=float, default=None,
                        help=f"vision gate の採用閾値 (enforce モード時のみ有効)。既定 "
                             f"{VISION_DEFAULT_MIN_SCORE} (較正前の保守的な値。shadow 実測で見直すこと)。")
    parser.add_argument("--manifest-out", default="",
                        help="manifest JSON をこのパスにも書き出す (--dry-run 中でも有効)。"
                             "#3332 N5 shadow workflow が採否を変えずに較正データを回収するために使う。"
                             "空なら無効 (既存挙動を変えない)。")
    args = parser.parse_args()

    in_path = pathlib.Path(args.in_path)
    if not in_path.exists():
        logger.error(f"Ranking input not found: {in_path}")
        sys.exit(1)
    payload = json.loads(in_path.read_text(encoding="utf-8"))
    ranking_items = payload.get("items", []) if isinstance(payload, dict) else []

    # #810 Phase 2: 楽天 Ichiba Search プールからも JAN を mine する。Search items は
    # matched_asin を持たないので _collect_unmatched_jans が全件を候補として扱う
    # (resolve 段で covered/JAN 重複は弾かれる)。ランキングを先に置き JAN dedup を優先。
    search_items = []
    if args.search_in_path:
        sp = pathlib.Path(args.search_in_path)
        if sp.exists():
            try:
                sdata = json.loads(sp.read_text(encoding="utf-8"))
                search_items = sdata.get("items", []) if isinstance(sdata, dict) else []
            except (OSError, json.JSONDecodeError) as e:
                logger.warning(f"Failed to load search pool {sp}: {e}")
        else:
            logger.info(f"Search pool not found (ranking-only): {sp}")
    mine_items = list(ranking_items) + list(search_items)
    logger.info(f"JAN mining input: ranking={len(ranking_items)} search={len(search_items)} "
                f"total={len(mine_items)}")

    try:
        from creators_api_client import CreatorsAPIClient
    except ImportError as e:
        logger.error(f"creators_api_client import failed: {e}")
        sys.exit(1)
    api = CreatorsAPIClient()

    article_covered = _load_article_asins(args.articles_dir)
    covered = article_covered | _load_per_asin_asins(args.per_asin_root)
    logger.info(f"Covered ASINs (articles ∪ per_asin): {len(covered)} "
                f"(articles-only={len(article_covered)})")

    # #3332 N5-V1: 画像埋め込みクライアントは URL が与えられたときだけ生成する。
    # 未指定 (=K8 未デプロイ) の間は None のまま → title fuzzy はガードレール3点のみで
    # 判定し、image_sim は常に None (shadow 記録) になる。
    vision_client = None
    if args.enable_title_fuzzy and args.vision_embed_url:
        try:
            from vision_match import ImageEmbeddingClient
            vision_client = ImageEmbeddingClient(args.vision_embed_url)
            logger.info(f"Vision gate: client → {args.vision_embed_url} (mode={args.vision_gate_mode})")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Vision client init failed ({e}); continuing without vision gate")
            vision_client = None

    manifest = resolve_ranking_asins(
        mine_items, api, covered,
        limit=args.limit, search_index=args.search_index, sleep=args.sleep,
        enable_title_fuzzy=args.enable_title_fuzzy,
        title_fuzzy_limit=args.title_fuzzy_limit,
        title_fuzzy_item_count=args.title_fuzzy_item_count,
        title_fuzzy_price_tolerance=args.title_fuzzy_price_tolerance,
        vision_client=vision_client,
        vision_gate_mode=args.vision_gate_mode,
        vision_min_score=args.vision_min_score,
        enable_genre_gate=args.genre_gate,
    )
    manifest["generated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    logger.info(
        f"Resolve: unmatched_jans={manifest['input_unmatched_jans']} "
        f"(candidates_before_limit={manifest['jan_candidates_before_limit']}) "
        f"resolved={len(manifest['resolved'])} unresolved={len(manifest['unresolved'])} "
        f"skipped_covered={len(manifest['skipped_already_covered'])}"
    )
    for r in manifest["resolved"]:
        logger.info(f"  resolved JAN {r['jan']} → {r['asin']} (rank={r['rank']})")
    logger.info(
        f"Genre gate (#2823): enabled={manifest['genre_gate_enabled']} "
        f"dropped={manifest['genre_gate_dropped']} (flag のみ除外・indeterminate は通す)"
    )
    for g in manifest["genre_gate_dropped_items"]:
        logger.info(
            f"  genre-drop {g['asin']} (rank={g['rank']}, via={g['match_method']}) "
            f"roots={g['roots']} title={g['title']!r}"
        )
    if "title_fuzzy" in manifest:
        tf = manifest["title_fuzzy"]
        logger.info(
            f"Title-fuzzy (#2818 対策4c): input={tf['title_fuzzy_input']} "
            f"(candidates_before_limit={tf['title_fuzzy_candidates_before_limit']}) "
            f"resolved={len(tf['title_fuzzy_resolved'])} "
            f"rejected={len(tf['title_fuzzy_rejected'])} "
            f"gate_mode={args.vision_gate_mode}"
        )
        for e in tf["title_fuzzy_resolved"]:
            logger.info(
                f"  fuzzy {e['asin']} (rank={e['rank']}) brand={e['rakuten_brand']!r} "
                f"tokens={e['common_tokens']} image_sim={e['image_sim']}"
            )

    # ranking_pool: ランキング由来・未記事化 ASIN の永続候補リスト (#810 Phase 1.5)。
    # 既存 pool を読み、記事化済を prune して今回 resolved を足し、Jules pick-asin に渡す。
    pool_path = pathlib.Path(args.pool)
    existing_pool = []
    if pool_path.exists():
        try:
            loaded = json.loads(pool_path.read_text(encoding="utf-8"))
            existing_pool = loaded.get("asins", []) if isinstance(loaded, dict) else []
        except (OSError, json.JSONDecodeError):
            existing_pool = []
    updated_pool = update_ranking_pool(existing_pool, manifest["new_asins"], article_covered)
    manifest["ranking_pool_size"] = len(updated_pool)
    logger.info(
        f"ranking_pool: {len(existing_pool)} → {len(updated_pool)} asins "
        f"(+{len(manifest['new_asins'])} resolved, prune by {len(article_covered)} article-covered)"
    )

    # #3332 N5 shadow: --manifest-out は dry-run 中でも書ける (採否を変えずに較正
    # データを回収するため)。既存の manifest 出力 (data/raw/_ranking_resolved_manifest.json)
    # とは別パスに出す用途で、指定が無ければ何もしない (既存挙動を変えない)。
    if args.manifest_out:
        mp = pathlib.Path(args.manifest_out)
        mp.parent.mkdir(parents=True, exist_ok=True)
        mp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"Wrote manifest to {mp}")

    if args.dry_run:
        logger.info("--dry-run: no output files written (except --manifest-out if given).")
        print(",".join(manifest["new_asins"]))
        return

    os.makedirs(args.out, exist_ok=True)
    (pathlib.Path(args.out) / "ranking_sniper_asins.csv").write_text(
        ",".join(manifest["new_asins"]), encoding="utf-8"
    )
    (pathlib.Path(args.out) / "_ranking_resolved_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    pool_path.parent.mkdir(parents=True, exist_ok=True)
    pool_path.write_text(
        json.dumps({"asins": updated_pool, "generated_at": manifest["generated_at"]},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(",".join(manifest["new_asins"]))


if __name__ == "__main__":
    main()
