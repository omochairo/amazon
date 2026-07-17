"""build_wp_navi_link_candidates.py

Issue #3333 Phase 1「WP (omcha.jp) → navi 相互リンクレーン」の候補生成スクリプト。

本家 WP (omcha.jp) は月 ~90,000 PV の収益源かつ手動管理領域であり、**自動改稿は
絶対にしない**。本スクリプトは WP 記事一覧と navi 側の記事カタログ (年齢 hub /
診断ツール / 商品ページ) を意味的に照合し、「この WP 記事にはこの navi ページへの
リンクが文脈に合いそうだ」という**候補レポート (markdown)** を出力するだけである。
WP への書き込み API は一切呼ばない (read-only GET のみ)。navi 側へのリンク追記も
本スクリプトはしない — レポートを見てオーナーが WP を手動編集する運用。

処理の流れ:
  1. WP REST API (``/wp-json/wp/v2/posts``) から published 記事を id/link/title/
     excerpt に絞ってページネーション取得する (低レート・User-Agent 明示)
  2. navi 側の候補カタログを構築する:
     - 年齢 hub (``/toys-age-N/``) 6 件・診断ツール (``/diagnosis/``) 1 件は
       固定データ (:data:`AGE_HUBS` / :data:`DIAGNOSIS_HUB`)
     - 商品ページは ``data/articles/*.json`` の title + meta_description から
       構築 (URL は ``build_post.py`` と同じ ``/products/<asin>/`` 規約)
  3. amazon-home-ops K8 LLM ワーカーの Ruri v3 API (``/embed``) で両者を埋め込み、
     コサイン類似度で WP 記事ごとに navi 候補を絞り込む。reranker
     (``/rerank``) が使えれば上位候補だけ並べ替えの精度を上げる (失敗しても
     cosine 順にフォールバックし、レポート生成自体は止めない — 補助的な役割)
  4. 類似度閾値 (``--min-score``、既定値は保守的に設定。実データでの初回シャドー
     実行後にオーナーが目視評価して調整する想定 — issue #3333 設計コメントの
     「まず保守的な既定値」) 未満の候補は出さない。WP 記事ごとに最大
     ``--top-k`` (既定 3) 件のみ出す
  5. markdown レポートを書き出す (WP 記事タイトル・URL / navi 候補 URL・
     アンカー案・類似度スコア)

このスクリプトが**しないこと** (構造的にゼロ):
  - WP への書き込み・自動改稿 (POST/PUT/DELETE を一切呼ばない)
  - navi 側 (data/articles や hugo/content) への書き込み
  - 記事本文からのアンカー抽出 (generate_internal_links.py の逐語アンカー契約とは
    別物。ここでの「アンカー案」は navi 候補側のタイトル/商品名から機械的に
    作る短いフレーズであり、WP 本文の実際の文言と一致するとは限らない —
    最終的な埋め込み判断はオーナーに委ねる)

WP 記事本文取得の対象を「高 PV 上位 ~20 本」に絞る仕組みは v1 では未実装
(GA4 に omcha.jp 分のページ別データがあるか要確認、issue #3333 設計コメント
の未確定点)。v1 は全 published 記事を対象にスコア順で出し、どの WP 記事に
実際に手を入れるかはオーナーの編集判断に委ねる。

Issue: https://github.com/omochairo/amazon/issues/3333 (Phase 1 設計コメント)
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import math
import os
import pathlib
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

import requests

from scripts.audit_query_entailment import discover_articles

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("build_wp_navi_link_candidates")

DEFAULT_WP_BASE_URL = "https://omcha.jp"
DEFAULT_ARTICLES_DIR = "data/articles"
DEFAULT_RURI_URL = "http://localhost:8000"
DEFAULT_OUT = "data/analytics/wp_navi_link_candidates.md"
NAVI_BASE_URL = "https://navi.omcha.jp"

DEFAULT_MIN_SCORE = 0.55  # 保守的な既定値 (要実データ較正・issue #3333 設計コメント)
DEFAULT_TOP_K = 3
DEFAULT_PER_PAGE = 100
DEFAULT_SLEEP_SECONDS = 1.0
DEFAULT_MAX_PAGES = 200  # 安全弁 (無限ページネーション防止)
DEFAULT_RERANK_TOP_N = 8
DEFAULT_EMBED_BATCH_SIZE = 32

REQUEST_TIMEOUT = 30
_MAX_EXTRA_RETRIES = 2  # 初回 + 2 リトライ = 最大 3 回試行
_RETRY_SLEEP_SECONDS = 2.0
MAX_TEXT_LEN = 1200

WP_USER_AGENT = "omochairo-wp-navi-link-bot/1.0 (+https://navi.omcha.jp; read-only WP REST fetch, #3333)"

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


class EmbeddingBatchError(Exception):
    """Ruri embed バッチがリトライ上限まで失敗した。"""


# --------------------------------------------------------------------------
# navi 側候補カタログ: 年齢 hub / 診断ツール (固定データ)
# --------------------------------------------------------------------------

AGE_HUBS: tuple[dict[str, str], ...] = (
    {
        "url": "/toys-age-0/",
        "title": "0歳の知育玩具 おすすめランキング｜赤ちゃんの五感を育てる安全なおもちゃ",
        "anchor": "0歳の知育玩具 おすすめランキング",
        "description": "0歳（0〜11ヶ月）の赤ちゃんに安心して与えられる知育玩具を、独自の知育スコア（教育効果・安全性・長く遊べる・コスパの4軸）で評価してランキング。にぎる・見る・音を聞くなど五感を刺激するおもちゃを、対象月齢・安全性とあわせて厳選しました。",
    },
    {
        "url": "/toys-age-1/",
        "title": "1歳の知育玩具 おすすめランキング｜手指・言葉・歩く力を育てるおもちゃ",
        "anchor": "1歳の知育玩具 おすすめランキング",
        "description": "1歳（1歳〜1歳11ヶ月）の知育玩具を、独自の知育スコアで評価してランキング。手指を使う型はめ・積み木、言葉が増えるおしゃべりトイ、歩き始めを応援する手押し車などを厳選。",
    },
    {
        "url": "/toys-age-2/",
        "title": "2歳の知育玩具 おすすめランキング｜指先・ごっこ遊び・言葉を伸ばすおもちゃ",
        "anchor": "2歳の知育玩具 おすすめランキング",
        "description": "2歳（2歳〜2歳11ヶ月）の知育玩具を、独自の知育スコアで評価してランキング。指先を使うパズル・ブロック、想像力を育てるごっこ遊び、言葉を増やすおもちゃを厳選。",
    },
    {
        "url": "/toys-age-3/",
        "title": "3歳の知育玩具 おすすめランキング｜文字・数・手先を伸ばす学べるおもちゃ",
        "anchor": "3歳の知育玩具 おすすめランキング",
        "description": "3歳（3歳〜3歳11ヶ月）の知育玩具を、独自の知育スコアで評価してランキング。ひらがな・数の入り口、手先を使うブロックやパズル、ルールのあるゲームを厳選。",
    },
    {
        "url": "/toys-age-4/",
        "title": "4歳・5歳の知育玩具 おすすめランキング｜考える力と手先を伸ばすおもちゃ",
        "anchor": "4歳・5歳の知育玩具 おすすめランキング",
        "description": "4歳・5歳（4〜5歳）の知育玩具を、独自の知育スコアで評価してランキング。論理的に考えるパズル・ボードゲーム、文字や時計・数の学び、手先を使う工作系を厳選。",
    },
    {
        "url": "/toys-age-6/",
        "title": "6歳以上の知育玩具 おすすめランキング｜小学生の思考力を伸ばすおもちゃ",
        "anchor": "6歳以上の知育玩具 おすすめランキング",
        "description": "6歳以上（小学生〜）の知育玩具を、独自の知育スコアで評価してランキング。プログラミング・科学実験、戦略的に考えるボードゲーム、ものづくり系の工作キットを厳選。",
    },
)

DIAGNOSIS_HUB: dict[str, str] = {
    "url": "/diagnosis/",
    "title": "おもちゃ処方箋 診断ツール",
    "anchor": "おもちゃ処方箋 診断ツール",
    "description": "5つの質問に答えるだけで、お子さまにぴったりな知育玩具をAIが診断・選定します。年齢、伸ばしたい知育効果、遊ぶ場所、予算から最適なおもちゃを提案します。",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _short_anchor(title: str, max_len: int = 30) -> str:
    """title からアンカー案用の短いフレーズを作る (｜区切りの前半 → max_len で切り詰め)。"""
    if not isinstance(title, str) or not title.strip():
        return ""
    head = title.split("｜", 1)[0].strip()
    return (head or title.strip())[:max_len]


def strip_html(text: str) -> str:
    """WP REST の ``.rendered`` フィールド (HTML タグ・エンティティ込み) をプレーンテキスト化する。"""
    if not isinstance(text, str) or not text:
        return ""
    no_tags = _TAG_RE.sub(" ", text)
    unescaped = html.unescape(no_tags)
    return _WS_RE.sub(" ", unescaped).strip()


# --------------------------------------------------------------------------
# navi 側候補カタログの構築 (pure function)
# --------------------------------------------------------------------------

def build_hub_candidates() -> list[dict[str, Any]]:
    """年齢 hub 6 件 + 診断ツール 1 件を候補 dict のリストにする。"""
    candidates: list[dict[str, Any]] = []
    for hub in AGE_HUBS:
        embed_text = f"{hub['title']}\n{hub['description']}"[:MAX_TEXT_LEN]
        candidates.append({
            "kind": "age_hub",
            "url": hub["url"],
            "title": hub["title"],
            "anchor": hub["anchor"],
            "embed_text": embed_text,
        })
    embed_text = f"{DIAGNOSIS_HUB['title']}\n{DIAGNOSIS_HUB['description']}"[:MAX_TEXT_LEN]
    candidates.append({
        "kind": "diagnosis",
        "url": DIAGNOSIS_HUB["url"],
        "title": DIAGNOSIS_HUB["title"],
        "anchor": DIAGNOSIS_HUB["anchor"],
        "embed_text": embed_text,
    })
    return candidates


def build_product_candidates(articles_dir: pathlib.Path) -> list[dict[str, Any]]:
    """``data/articles/*.json`` から商品ページ候補を構築する (asin 昇順・決定的)。

    URL は ``build_post.py`` と同じ ``/products/<asin(小文字)>/`` 規約。
    アンカー案は product.name (無ければ title の｜前半) を使う。
    """
    candidates: list[dict[str, Any]] = []
    article_index = discover_articles(articles_dir)
    for asin in sorted(article_index.keys()):
        path = article_index[asin]
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("skip %s: failed to read/parse %s: %s", asin, path, e)
            continue
        if not isinstance(data, dict):
            continue

        title = data.get("title") if isinstance(data.get("title"), str) else ""
        meta_description = data.get("meta_description") if isinstance(data.get("meta_description"), str) else ""
        product = data.get("product") if isinstance(data.get("product"), dict) else {}
        product_name = product.get("name") if isinstance(product.get("name"), str) else ""

        display_title = (title or product_name).strip()
        if not display_title:
            continue

        anchor = (product_name.strip() or _short_anchor(title))[:30]
        embed_text = "\n".join(p.strip() for p in (title, meta_description) if p and p.strip())
        embed_text = (embed_text or display_title)[:MAX_TEXT_LEN]

        candidates.append({
            "kind": "product",
            "url": f"/products/{asin.lower()}/",
            "title": display_title,
            "anchor": anchor,
            "embed_text": embed_text,
        })
    return candidates


def build_navi_candidates(articles_dir: pathlib.Path) -> list[dict[str, Any]]:
    """navi 側の全候補 (年齢 hub + 診断ツール + 商品ページ) を構築する。"""
    return build_hub_candidates() + build_product_candidates(articles_dir)


# --------------------------------------------------------------------------
# WP REST API 取得 (read-only. GET のみ)
# --------------------------------------------------------------------------

def parse_wp_post(raw: Any) -> dict[str, Any] | None:
    """WP REST の 1 記事 raw dict を ``{id, link, title, excerpt}`` に正規化する。

    id/link/title のいずれかが欠けている・型が不正な要素は None を返す (呼び出し
    元でスキップ)。title/excerpt は ``.rendered`` の HTML をプレーンテキスト化する。
    """
    if not isinstance(raw, dict):
        return None
    post_id = raw.get("id")
    link = raw.get("link")
    title_field = raw.get("title")
    excerpt_field = raw.get("excerpt")

    title_raw = title_field.get("rendered") if isinstance(title_field, dict) else None
    excerpt_raw = excerpt_field.get("rendered") if isinstance(excerpt_field, dict) else None

    title = strip_html(title_raw or "")
    excerpt = strip_html(excerpt_raw or "")

    if not isinstance(post_id, int) or not isinstance(link, str) or not link.strip() or not title:
        return None

    return {"id": post_id, "link": link.strip(), "title": title, "excerpt": excerpt}


def _fetch_wp_page(
    url: str, params: dict[str, Any], session: requests.Session, headers: dict[str, str], sleeper=time.sleep,
) -> list[Any] | None:
    """WP REST の 1 ページを取得する。ページ範囲外 (400) や再試行上限到達時は None を返す。"""
    last_err: Exception | None = None
    attempts = _MAX_EXTRA_RETRIES + 1
    for attempt in range(1, attempts + 1):
        try:
            resp = session.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 400:
                # WP は最終ページを超えると 400 rest_post_invalid_page_number を返す
                # (= ページネーション終端。エラー扱いにしない)。
                return None
            resp.raise_for_status()
            payload = resp.json()
            if not isinstance(payload, list):
                raise ValueError("unexpected WP posts response shape (expected a list)")
            return payload
        except (requests.RequestException, ValueError) as e:
            last_err = e
            if attempt < attempts:
                logger.warning("WP posts page fetch failed (attempt %d/%d): %s; retrying", attempt, attempts, e)
                sleeper(_RETRY_SLEEP_SECONDS)
            else:
                logger.error("WP posts page fetch failed after %d attempt(s): %s", attempts, e)
    return None


def fetch_wp_posts(
    wp_base_url: str,
    session: requests.Session,
    *,
    per_page: int = DEFAULT_PER_PAGE,
    sleep_seconds: float = DEFAULT_SLEEP_SECONDS,
    max_pages: int = DEFAULT_MAX_PAGES,
    limit: int = 0,
    sleeper=time.sleep,
) -> list[dict[str, Any]]:
    """WP REST API (``/wp-json/wp/v2/posts``) から published 記事を全件 (read-only) 取得する。

    ``_fields=id,link,title,excerpt`` で絞り、``status=publish`` を明示する
    (未認証リクエストはどのみち published しか返らないが自己文書化のため明示)。
    ページ間には ``sleep_seconds`` 秒の低レートを挟む (最終ページの後は挟まない)。
    ``limit>0`` のときは取得件数が limit に達し次第打ち切る (スモーク用)。

    途中のページ取得が (再試行しても) 失敗した場合はそこまでの取得結果を返す
    (レポートは部分的でも出す方が、全体を諦めるより有用なため)。
    """
    url = f"{wp_base_url.rstrip('/')}/wp-json/wp/v2/posts"
    headers = {"User-Agent": WP_USER_AGENT}
    posts: list[dict[str, Any]] = []
    page = 1
    while page <= max_pages:
        params = {
            "status": "publish",
            "per_page": per_page,
            "page": page,
            "_fields": "id,link,title,excerpt",
        }
        raw_list = _fetch_wp_page(url, params, session, headers, sleeper=sleeper)
        if raw_list is None:
            break
        if not raw_list:
            break

        for raw in raw_list:
            parsed = parse_wp_post(raw)
            if parsed is not None:
                posts.append(parsed)
            if limit and limit > 0 and len(posts) >= limit:
                return posts[:limit]

        if len(raw_list) < per_page:
            break  # 最終ページ

        page += 1
        if sleep_seconds > 0:
            sleeper(sleep_seconds)

    return posts


# --------------------------------------------------------------------------
# Ruri v3 埋め込み / reranker クライアント
# (amazon-home-ops ruri/app.py の /embed・/rerank。
#  scripts/compute_semantic_related.py の embed_batch_ruri と同じリトライ挙動。
#  kind ("query"|"document") を可変にする必要があるためここでは独立実装する)
# --------------------------------------------------------------------------

def embed_batch_ruri(
    texts: list[str], kind: str, ruri_url: str, session: requests.Session, sleeper=time.sleep,
) -> list[list[float]]:
    """1 バッチ分のテキストを Ruri v3 API (``/embed``) でベクトル化する。

    ``kind`` は "query" (WP 記事側) か "document" (navi 候補側) を指定する
    (ruri app.py がクエリ/文書プレフィックスを自動付与する)。
    """
    url = f"{ruri_url.rstrip('/')}/embed"
    last_err: Exception | None = None
    attempts = _MAX_EXTRA_RETRIES + 1
    for attempt in range(1, attempts + 1):
        try:
            resp = session.post(url, json={"texts": texts, "kind": kind}, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            payload = resp.json()
            vectors = payload.get("vectors") if isinstance(payload, dict) else None
            if not isinstance(vectors, list) or len(vectors) != len(texts):
                raise EmbeddingBatchError(
                    f"unexpected /embed response shape (expected {len(texts)} vectors)"
                )
            return vectors
        except (requests.RequestException, EmbeddingBatchError, ValueError) as e:
            last_err = e
            if attempt < attempts:
                logger.warning("ruri embed batch failed (attempt %d/%d): %s; retrying", attempt, attempts, e)
                sleeper(_RETRY_SLEEP_SECONDS)
            else:
                logger.error("ruri embed batch failed after %d attempt(s): %s", attempts, e)
    raise EmbeddingBatchError(str(last_err)) from last_err


def embed_texts_ruri(
    texts: list[str],
    kind: str,
    ruri_url: str,
    session: requests.Session,
    *,
    batch_size: int = DEFAULT_EMBED_BATCH_SIZE,
    sleeper=time.sleep,
) -> list[list[float]]:
    """テキスト全件を ``batch_size`` ごとに ``embed_batch_ruri`` へ渡す。"""
    vectors: list[list[float]] = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        vectors.extend(embed_batch_ruri(batch, kind, ruri_url, session, sleeper=sleeper))
    return vectors


def rerank_candidates(
    query_text: str,
    doc_texts: list[str],
    ruri_url: str,
    session: requests.Session,
    *,
    sleeper=time.sleep,
) -> list[dict[str, Any]] | None:
    """Ruri v3 reranker (``/rerank``) を呼ぶ。

    reranker は候補の並べ替え精度を上げる補助的な役割であり、最終的なフィルタ
    閾値には使わない (スコア表示は常に cosine 類似度)。リトライ上限まで失敗
    したら例外を送出せず None を返し、呼び出し元は cosine 順にフォールバック
    する (reranker 障害でレポート生成全体を止めない)。
    """
    if not doc_texts:
        return None
    url = f"{ruri_url.rstrip('/')}/rerank"
    body: dict[str, Any] = {"query": query_text, "documents": doc_texts}
    last_err: Exception | None = None
    attempts = _MAX_EXTRA_RETRIES + 1
    for attempt in range(1, attempts + 1):
        try:
            resp = session.post(url, json=body, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            payload = resp.json()
            results = payload.get("results") if isinstance(payload, dict) else None
            if not isinstance(results, list):
                raise ValueError("unexpected /rerank response shape (expected a results list)")
            cleaned = [r for r in results if isinstance(r, dict) and isinstance(r.get("index"), int)]
            return cleaned
        except (requests.RequestException, ValueError) as e:
            last_err = e
            if attempt < attempts:
                logger.warning("ruri rerank failed (attempt %d/%d): %s; retrying", attempt, attempts, e)
                sleeper(_RETRY_SLEEP_SECONDS)
            else:
                logger.error(
                    "ruri rerank failed after %d attempt(s); falling back to cosine order: %s", attempts, e
                )
    return None


# --------------------------------------------------------------------------
# 類似度計算 (numpy があればベクトル化、無ければ純 Python フォールバック)
# --------------------------------------------------------------------------

def _normalize_rows_pure(vectors: list[list[float]]) -> list[list[float]]:
    normed: list[list[float]] = []
    for v in vectors:
        norm = math.sqrt(sum(x * x for x in v))
        normed.append([0.0] * len(v) if norm == 0 else [x / norm for x in v])
    return normed


def cosine_similarity_cross(a_vectors: list[list[float]], b_vectors: list[list[float]]) -> list[list[float]]:
    """a (行・WP 記事) x b (列・navi 候補) のコサイン類似度行列を計算する。

    ``compute_semantic_related.compute_similarity_matrix`` と異なり正方行列を
    仮定しない (WP 記事数と navi 候補数は一致しない)。
    """
    if not a_vectors or not b_vectors:
        return [[] for _ in a_vectors]
    try:
        import numpy as np

        a = np.asarray(a_vectors, dtype=float)
        b = np.asarray(b_vectors, dtype=float)
        a_norms = np.linalg.norm(a, axis=1, keepdims=True)
        a_norms[a_norms == 0] = 1.0
        b_norms = np.linalg.norm(b, axis=1, keepdims=True)
        b_norms[b_norms == 0] = 1.0
        return ((a / a_norms) @ (b / b_norms).T).tolist()
    except ImportError:
        a_normed = _normalize_rows_pure(a_vectors)
        b_normed = _normalize_rows_pure(b_vectors)
        return [[sum(x * y for x, y in zip(row, col)) for col in b_normed] for row in a_normed]


# --------------------------------------------------------------------------
# WP 記事ごとの navi 候補選定 (pure function; reranker は注入可能な callable)
# --------------------------------------------------------------------------

# K8 runner は Python 3.8 (#3053 と同じ制約)。モジュールレベルの型エイリアスは
# `from __future__ import annotations` の対象外で実行時評価されるため、
# builtin generics (list[str]) ではなく typing generics を使う。
RerankerFn = Callable[[str, List[str]], Optional[List[Dict[str, Any]]]]


def select_navi_candidates_for_wp(
    wp_text: str,
    similarity_row: list[float],
    navi_candidates: list[dict[str, Any]],
    *,
    min_score: float = DEFAULT_MIN_SCORE,
    top_k: int = DEFAULT_TOP_K,
    rerank_top_n: int = DEFAULT_RERANK_TOP_N,
    reranker: RerankerFn | None = None,
) -> list[dict[str, Any]]:
    """1 WP 記事分の類似度行から navi 候補 top-k を選ぶ。

    1. cosine 類似度 >= ``min_score`` の候補だけを残す (score 降順 → url 昇順
       の決定的順序)
    2. ``reranker`` が渡されていれば、上位 ``max(rerank_top_n, top_k)`` 件を
       reranker で並べ替える (reranker が None を返したら cosine 順のまま)
    3. 最終的に上位 ``top_k`` 件を返す。返り値の ``score`` は常に cosine 類似度
       (reranker のスコアはフィルタ閾値と単位が異なるため表示には使わない)
    """
    eligible = [
        (similarity_row[j], j) for j in range(len(navi_candidates)) if similarity_row[j] >= min_score
    ]
    if not eligible:
        return []
    eligible.sort(key=lambda t: (-t[0], navi_candidates[t[1]]["url"]))

    shortlist_size = max(rerank_top_n, top_k)
    shortlist = eligible[:shortlist_size]
    order = [j for _, j in shortlist]
    cosine_by_index = {j: score for score, j in eligible}

    if reranker is not None and len(order) > 1:
        doc_texts = [navi_candidates[j]["embed_text"] for j in order]
        rerank_result = reranker(wp_text, doc_texts)
        if rerank_result:
            reordered = [
                order[r["index"]] for r in rerank_result if 0 <= r["index"] < len(order)
            ]
            seen = set(reordered)
            reordered.extend(j for j in order if j not in seen)
            order = reordered

    top_indices = order[:top_k]
    return [
        {**navi_candidates[j], "score": round(cosine_by_index[j], 3)} for j in top_indices
    ]


# --------------------------------------------------------------------------
# レポート生成 (pure function)
# --------------------------------------------------------------------------

def render_markdown_report(
    wp_entries: list[dict[str, Any]],
    *,
    generated_at: str,
    min_score: float,
    wp_total: int,
    navi_total: int,
) -> str:
    """WP 記事ごとの navi 候補を markdown レポートにする。"""
    lines = [
        "# WP → navi リンク候補レポート (Phase 1・issue #3333)",
        "",
        f"- 生成日時: {generated_at}",
        f"- WP 記事取得件数: {wp_total}",
        f"- navi 候補件数: {navi_total}",
        f"- 候補あり WP 記事数: {len(wp_entries)}",
        f"- 類似度閾値 (--min-score): {min_score}",
        "",
        "本レポートは候補提案のみです。WP への書き込み・自動改稿は一切行っていません。"
        " navi へのリンク追記は、本レポートを見てオーナーが WP を手動編集してください。",
        "",
    ]
    if not wp_entries:
        lines.append("(類似度閾値以上の navi 候補が見つかった WP 記事はありませんでした)")
        return "\n".join(lines) + "\n"

    for entry in wp_entries:
        lines.append(f"## {entry['wp_title']}")
        lines.append(f"- WP URL: {entry['wp_link']}")
        lines.append("- navi 候補:")
        for i, c in enumerate(entry["candidates"], start=1):
            navi_url = f"{NAVI_BASE_URL}{c['url']}"
            lines.append(
                f"  {i}. [{c['title']}]({navi_url}) — アンカー案: 「{c['anchor']}」 (score: {c['score']:.3f})"
            )
        lines.append("")

    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# 実行本体
# --------------------------------------------------------------------------

def run(
    *,
    wp_base_url: str = DEFAULT_WP_BASE_URL,
    articles_dir: pathlib.Path,
    ruri_url: str = DEFAULT_RURI_URL,
    out_path: pathlib.Path,
    min_score: float = DEFAULT_MIN_SCORE,
    top_k: int = DEFAULT_TOP_K,
    per_page: int = DEFAULT_PER_PAGE,
    sleep_seconds: float = DEFAULT_SLEEP_SECONDS,
    wp_limit: int = 0,
    rerank_top_n: int = DEFAULT_RERANK_TOP_N,
    use_reranker: bool = True,
    session: requests.Session | None = None,
    sleeper=time.sleep,
) -> dict[str, Any]:
    """WP 記事取得 → navi 候補構築 → 埋め込み照合 → レポート書き込みを行う。"""
    summary: dict[str, Any] = {
        "wp_posts": 0, "navi_candidates": 0, "wp_with_candidates": 0, "aborted": False,
    }
    session = session or requests.Session()

    wp_posts = fetch_wp_posts(
        wp_base_url, session, per_page=per_page, sleep_seconds=sleep_seconds, limit=wp_limit, sleeper=sleeper,
    )
    summary["wp_posts"] = len(wp_posts)
    if not wp_posts:
        # WP が実際に 0 件公開ということは実運用上考えにくく、取得失敗と区別が
        # つかないため abort する (空レポートを誤って「候補なし」と誤読させない)。
        logger.error("no WP posts fetched (possible fetch failure); aborting without writing report")
        summary["aborted"] = True
        return summary

    navi_candidates = build_navi_candidates(articles_dir)
    summary["navi_candidates"] = len(navi_candidates)
    if not navi_candidates:
        logger.error("no navi candidates built (age hubs/diagnosis should always exist); aborting")
        summary["aborted"] = True
        return summary

    wp_texts = [f"{p['title']}\n{p['excerpt']}".strip()[:MAX_TEXT_LEN] for p in wp_posts]
    navi_texts = [c["embed_text"] for c in navi_candidates]

    try:
        wp_vectors = embed_texts_ruri(wp_texts, "query", ruri_url, session, sleeper=sleeper)
        navi_vectors = embed_texts_ruri(navi_texts, "document", ruri_url, session, sleeper=sleeper)
    except EmbeddingBatchError as e:
        logger.error("embedding failed; aborting without writing report: %s", e)
        summary["aborted"] = True
        return summary

    similarity = cosine_similarity_cross(wp_vectors, navi_vectors)

    reranker_fn: RerankerFn | None = None
    if use_reranker:
        def reranker_fn(query_text: str, doc_texts: list[str]) -> list[dict[str, Any]] | None:
            return rerank_candidates(query_text, doc_texts, ruri_url, session, sleeper=sleeper)

    wp_entries: list[dict[str, Any]] = []
    for i, post in enumerate(wp_posts):
        candidates = select_navi_candidates_for_wp(
            wp_texts[i], similarity[i], navi_candidates,
            min_score=min_score, top_k=top_k, rerank_top_n=rerank_top_n, reranker=reranker_fn,
        )
        if not candidates:
            continue
        wp_entries.append({"wp_title": post["title"], "wp_link": post["link"], "candidates": candidates})

    summary["wp_with_candidates"] = len(wp_entries)

    report = render_markdown_report(
        wp_entries,
        generated_at=_now_iso(),
        min_score=min_score,
        wp_total=len(wp_posts),
        navi_total=len(navi_candidates),
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")

    logger.info(
        "done: wp_posts=%d navi_candidates=%d wp_with_candidates=%d -> %s",
        summary["wp_posts"], summary["navi_candidates"], summary["wp_with_candidates"], out_path,
    )
    print(json.dumps(summary, ensure_ascii=False))
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--wp-base-url", default=os.environ.get("WP_BASE_URL", DEFAULT_WP_BASE_URL), help="WP のベース URL")
    ap.add_argument("--articles-dir", default=DEFAULT_ARTICLES_DIR, help="navi 記事 JSON のディレクトリ")
    ap.add_argument("--ruri-url", default=os.environ.get("RURI_URL", DEFAULT_RURI_URL), help="Ruri v3 API の URL")
    ap.add_argument("--out", default=DEFAULT_OUT, help="markdown レポートの出力先パス")
    ap.add_argument("--min-score", type=float, default=DEFAULT_MIN_SCORE, help="この cosine 類似度未満の候補は出さない")
    ap.add_argument("--top-k", type=int, default=DEFAULT_TOP_K, help="WP 記事あたりの navi 候補数上限")
    ap.add_argument("--per-page", type=int, default=DEFAULT_PER_PAGE, help="WP REST API の 1 ページあたり件数")
    ap.add_argument("--sleep-seconds", type=float, default=DEFAULT_SLEEP_SECONDS, help="WP ページ間の待機秒数 (低レート)")
    ap.add_argument("--wp-limit", type=int, default=0, help="取得する WP 記事数の上限 (0=全件、スモーク用)")
    ap.add_argument("--rerank-top-n", type=int, default=DEFAULT_RERANK_TOP_N, help="reranker に渡す候補数の上限")
    ap.add_argument("--no-reranker", action="store_true", help="reranker (/rerank) を使わず cosine 類似度のみで選ぶ")
    args = ap.parse_args()

    summary = run(
        wp_base_url=args.wp_base_url,
        articles_dir=pathlib.Path(args.articles_dir),
        ruri_url=args.ruri_url,
        out_path=pathlib.Path(args.out),
        min_score=args.min_score,
        top_k=args.top_k,
        per_page=args.per_page,
        sleep_seconds=args.sleep_seconds,
        wp_limit=args.wp_limit,
        rerank_top_n=args.rerank_top_n,
        use_reranker=not args.no_reranker,
    )
    return 1 if summary.get("aborted") else 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
