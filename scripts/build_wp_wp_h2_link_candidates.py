"""build_wp_wp_h2_link_candidates.py

omcha-ops Issue #174 (親issue #26 P7)「omcha.jp 内部リンク候補レーン (WP→WP・H2粒度)」
の候補生成スクリプト。

``build_wp_navi_link_candidates.py`` (Issue #3333 Phase 1・WP→navi・記事単位) と
同じ shadow レポート方式だが、差分は2つ:

  1. 向き: WP記事 → navi ではなく **WP記事 → WP既存記事** (自サイト内)
  2. クエリ粒度: 記事単位ではなく **対象記事の H2 見出し単位**
     (「この H2 にはこの既存記事への内部リンクが合いそうだ」を H2 ごとに出す)

WP は収益源かつ手動管理領域のため自動改稿は絶対にしない。本スクリプトは対象記事
(query) の H2 見出しと、既存 WP 記事一覧 (document 候補) を意味的に照合し、
markdown の候補レポートを出力するだけである。WP への書き込み API は一切呼ばない
(read-only GET のみ)。貼るかどうかは人が判断し、WP を手動編集する運用。

処理の流れ:
  1. 対象記事 (query) を読み込む。次のどちらか:
     - ``--query-post-id``: WP REST から単発取得 (``/wp/v2/posts/<id>`` →
       404 なら ``/wp/v2/pages/<id>``)。1 記事だけなので ``content.rendered``
       でも安全 (全 844 件走査で the_content 経由の Amazon 商品ブロックが
       ``TooManyRequests`` を返した 2026-09-10 実測とは規模が違う)
     - ``--query-content-file``: ``wp_draft.py pull`` 出力形式のローカル JSON
       (``source_id``/``source_link``/``title``/``content``)。未公開の下書きや、
       較正用に保存済みの記事を使うとき用
  2. document 候補 (照合先) は ``omcha-ops/content/index.jsonl`` の全 844 件
     (post 820 + **page 24**。page を含めないと clicks 1位の記事が丸ごと漏れる)。
     **v1 は埋め込みテキストをタイトルのみ**にする — 844 件ぶんの本文を安全に
     取るには WP 認証 (``context=edit`` の ``content.raw``) が要り、この K8 runner
     には資格情報が無い。タイトルのみなら index.jsonl 一発で済み、追加の WP
     負荷がゼロになる (2026-09-11 設計判断。まず動くものを作り、精度は後で見る)
  3. 自リンク除外は **id で行う** (URL 文字列一致ではない)。下書き段階の
     ``source_link`` は ``https://omcha.jp/?p=<id>`` 形式で、index.jsonl の
     パーマリンクと文字列が一致しないため (issue #174 の実装ノート)
  4. amazon-home-ops K8 LLM ワーカーの Ruri v3 API (``/embed``) で H2 見出しと
     document タイトルを埋め込み、コサイン類似度で照合する。reranker
     (``/rerank``) が使えれば上位候補の並べ替え精度を上げる (失敗時は cosine
     順にフォールバック)
  5. 対象記事の本文に既に ``[blogcard url="..."]`` がある候補は、**落とさず
     「既出」フラグを付けて出す** (消すと「なぜ出てこないのか」が分からず、
     同じ調査を人が繰り返すため)
  6. 類似度閾値 (``--min-score``) 未満の候補は出さない。H2 ごとに最大
     ``--top-k`` (既定 3) 件のみ出す

``--min-score`` の既定値 0.87 は ``build_wp_navi_link_candidates.py`` (WP→navi・
記事単位、2026-07-18 実測較正) からの**暫定流用**。WP→WP・H2粒度は対の性質も
粒度も違うため、そのまま当てはまる保証はない。issue #174 の較正材料
(``omcha-ops/work/after_15750.json``) は ``.gitignore`` 対象のローカルファイルで
この場からは参照できなかったため、較正は初回 shadow run の実データを見てから
行う (2026-09-11 オーナー判断)。

このスクリプトが**しないこと** (構造的にゼロ):
  - WP への書き込み・自動改稿・自動挿入 (POST/PUT/DELETE を一切呼ばない)
  - 本文からの厳密なアンカー抽出 (「候補」の提示までで、貼る判断は人)

Issue: https://github.com/omochairo/omcha-ops/issues/174
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("build_wp_wp_h2_link_candidates")

DEFAULT_WP_BASE_URL = "https://omcha.jp"
DEFAULT_RURI_URL = "http://localhost:8000"
DEFAULT_OUT = "data/analytics/wp_wp_h2_link_candidates.md"

# build_wp_navi_link_candidates.py の 2026-07-18 較正 (WP→navi・記事単位) からの
# 暫定流用。WP→WP・H2粒度での較正はまだ済んでいない (issue #174 参照)。
DEFAULT_MIN_SCORE = 0.87
DEFAULT_TOP_K = 3
DEFAULT_RERANK_TOP_N = 8
DEFAULT_EMBED_BATCH_SIZE = 32

REQUEST_TIMEOUT = 30
_MAX_EXTRA_RETRIES = 2  # 初回 + 2 リトライ = 最大 3 回試行
_RETRY_SLEEP_SECONDS = 2.0
MAX_TEXT_LEN = 1200

WP_USER_AGENT = "omochairo-wp-wp-h2-link-bot/1.0 (+https://navi.omcha.jp; read-only WP REST fetch, omcha-ops#174)"

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_H2_RE = re.compile(r"<h2[^>]*>(.*?)</h2>", re.IGNORECASE | re.DOTALL)
_SHORTCODE_RE = re.compile(r"\[/?[a-zA-Z0-9_-]+[^\]]*\]")
_BLOGCARD_RE = re.compile(r"\[blogcard\b[^\]]*\burl=[\"']([^\"']+)[\"'][^\]]*\]", re.IGNORECASE)


class EmbeddingBatchError(Exception):
    """Ruri embed バッチがリトライ上限まで失敗した。"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def strip_html(text: str) -> str:
    """HTML タグ・エンティティ込みのテキストをプレーンテキスト化する。"""
    if not isinstance(text, str) or not text:
        return ""
    no_tags = _TAG_RE.sub(" ", text)
    unescaped = html.unescape(no_tags)
    return _WS_RE.sub(" ", unescaped).strip()


def strip_shortcodes(text: str) -> str:
    """``[deco type="..."]...[/deco]`` のような WP ショートコードを除去する。"""
    if not isinstance(text, str) or not text:
        return ""
    return _SHORTCODE_RE.sub(" ", text)


def clean_heading_text(raw_heading_html: str) -> str:
    """H2 見出しの生 HTML から埋め込み用のプレーンテキストを作る。"""
    return strip_html(strip_shortcodes(raw_heading_html))


def normalize_url(url: str) -> str:
    """末尾スラッシュの有無を吸収して URL を比較可能にする。"""
    return url.strip().rstrip("/")


# --------------------------------------------------------------------------
# document 候補: omcha-ops/content/index.jsonl (post + page、タイトルのみ)
# --------------------------------------------------------------------------

def parse_index_entry(raw: Any) -> dict[str, Any] | None:
    """index.jsonl の 1 行を ``{id, url, title}`` に正規化する。publish 以外は除外。"""
    if not isinstance(raw, dict):
        return None
    entry_id = raw.get("id")
    link = raw.get("link")
    title = raw.get("title")
    status = raw.get("status")
    if not isinstance(entry_id, int) or not isinstance(link, str) or not link.strip():
        return None
    if not isinstance(title, str) or not title.strip():
        return None
    if status != "publish":
        return None
    return {"id": entry_id, "url": link.strip(), "title": title.strip()}


def load_index_jsonl(path: pathlib.Path) -> list[dict[str, Any]]:
    """``omcha-ops/content/index.jsonl`` (1行1記事の JSON Lines) を読み込む。"""
    entries: list[dict[str, Any]] = []
    if not path.exists():
        logger.error("index file not found: %s", path)
        return entries
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as e:
            logger.warning("skip malformed index line: %s", e)
            continue
        parsed = parse_index_entry(raw)
        if parsed is not None:
            entries.append(parsed)
    return entries


def build_document_candidates(
    index_entries: list[dict[str, Any]], *, exclude_id: int | None = None,
) -> list[dict[str, Any]]:
    """index エントリから document 候補を構築する (自記事は id で除外)。"""
    candidates: list[dict[str, Any]] = []
    for entry in index_entries:
        if exclude_id is not None and entry["id"] == exclude_id:
            continue
        candidates.append({
            "id": entry["id"],
            "url": entry["url"],
            "title": entry["title"],
            "embed_text": entry["title"][:MAX_TEXT_LEN],
        })
    return candidates


# --------------------------------------------------------------------------
# query 記事: H2 セクション分割 + 既出 [blogcard] 検出
# --------------------------------------------------------------------------

def extract_h2_sections(content_html: str) -> list[dict[str, Any]]:
    """本文 HTML を H2 単位でセクション分割する (H3/H4 は親 H2 の body に含める)。"""
    if not isinstance(content_html, str) or not content_html:
        return []
    matches = list(_H2_RE.finditer(content_html))
    sections: list[dict[str, Any]] = []
    for i, m in enumerate(matches):
        heading = clean_heading_text(m.group(1))
        if not heading:
            continue
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content_html)
        sections.append({"heading": heading, "body_html": content_html[start:end]})
    return sections


def extract_blogcard_urls(content_html: str) -> set[str]:
    """本文全体から既存の ``[blogcard url="..."]`` の URL 集合を取る (正規化済み)。"""
    if not isinstance(content_html, str) or not content_html:
        return set()
    return {normalize_url(u) for u in _BLOGCARD_RE.findall(content_html)}


# --------------------------------------------------------------------------
# query 記事の読み込み: WP REST 単発取得 / ローカル JSON ファイル
# --------------------------------------------------------------------------

def parse_query_post(raw: Any) -> dict[str, Any] | None:
    """WP REST の 1 記事 raw dict (query 側) を ``{id, link, title, content_html}`` に正規化する。"""
    if not isinstance(raw, dict):
        return None
    post_id = raw.get("id")
    link = raw.get("link")
    title_field = raw.get("title")
    content_field = raw.get("content")
    title_raw = title_field.get("rendered") if isinstance(title_field, dict) else None
    content_raw = content_field.get("rendered") if isinstance(content_field, dict) else None
    title = strip_html(title_raw or "")
    if not isinstance(post_id, int) or not isinstance(link, str) or not link.strip() or not title:
        return None
    return {"id": post_id, "link": link.strip(), "title": title, "content_html": content_raw or ""}


def fetch_query_article(
    wp_base_url: str, post_id: int, session: requests.Session,
) -> dict[str, Any] | None:
    """対象記事を単発取得する (posts → 404 なら pages。最大 2 リクエストのみ)。

    全 844 件走査ではなく 1 記事だけなので ``content.rendered`` を使っても
    the_content 経由の Amazon 商品ブロック連打にはならない
    (2026-09-10 実測の ``TooManyRequests`` は 844 件を連続で叩いたときの話)。
    """
    headers = {"User-Agent": WP_USER_AGENT}
    for ptype in ("posts", "pages"):
        url = f"{wp_base_url.rstrip('/')}/wp-json/wp/v2/{ptype}/{post_id}"
        params = {"_fields": "id,link,title,content"}
        try:
            resp = session.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            logger.warning("query article fetch failed (%s/%d): %s", ptype, post_id, e)
            continue
        if resp.status_code == 404:
            continue
        try:
            resp.raise_for_status()
        except requests.HTTPError as e:
            logger.warning("query article fetch failed (%s/%d): %s", ptype, post_id, e)
            continue
        parsed = parse_query_post(resp.json())
        if parsed is not None:
            return parsed
    return None


def load_query_from_file(path: pathlib.Path) -> dict[str, Any] | None:
    """``wp_draft.py pull`` 出力形式のローカル JSON を読み込む。

    フィールドは ``source_id``/``source_link``/``title``/``content`` (無ければ
    ``id``/``link`` にフォールバック)。較正用の保存済み記事や未公開の下書きに使う。
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.error("failed to read query content file %s: %s", path, e)
        return None
    if not isinstance(data, dict):
        return None
    post_id = data.get("source_id") if isinstance(data.get("source_id"), int) else data.get("id")
    link = data.get("source_link") if isinstance(data.get("source_link"), str) else data.get("link")
    title = data.get("title")
    content_html = data.get("content")
    if (
        not isinstance(post_id, int)
        or not isinstance(link, str) or not link.strip()
        or not isinstance(title, str) or not title.strip()
        or not isinstance(content_html, str)
    ):
        logger.error("query content file %s missing required fields (id/link/title/content)", path)
        return None
    return {"id": post_id, "link": link.strip(), "title": strip_html(title) or title.strip(), "content_html": content_html}


# --------------------------------------------------------------------------
# Ruri v3 埋め込み / reranker クライアント
# (amazon-home-ops ruri/app.py の /embed・/rerank。build_wp_navi_link_candidates.py
#  と同じ独立実装 — kind ("query"|"document") を可変にする必要があるため)
# --------------------------------------------------------------------------

def embed_batch_ruri(
    texts: list[str], kind: str, ruri_url: str, session: requests.Session, sleeper=time.sleep,
) -> list[list[float]]:
    """1 バッチ分のテキストを Ruri v3 API (``/embed``) でベクトル化する。"""
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
    """Ruri v3 reranker (``/rerank``) を呼ぶ。失敗時は None (呼び出し元は cosine 順にフォールバック)。"""
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
    """a (行・H2 見出し) x b (列・document 候補) のコサイン類似度行列を計算する。"""
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
# H2 ごとの document 候補選定 (pure function; reranker は注入可能な callable)
# --------------------------------------------------------------------------

# K8 runner は Python 3.8 (#3053 と同じ制約)。モジュールレベルの型エイリアスは
# `from __future__ import annotations` の対象外で実行時評価されるため、
# builtin generics (list[str]) ではなく typing generics を使う。
RerankerFn = Callable[[str, List[str]], Optional[List[Dict[str, Any]]]]


def select_document_candidates_for_h2(
    h2_text: str,
    similarity_row: list[float],
    document_candidates: list[dict[str, Any]],
    *,
    min_score: float = DEFAULT_MIN_SCORE,
    top_k: int = DEFAULT_TOP_K,
    rerank_top_n: int = DEFAULT_RERANK_TOP_N,
    reranker: RerankerFn | None = None,
    existing_urls: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """1 H2 分の類似度行から document 候補 top-k を選ぶ。

    ``existing_urls`` に含まれる候補は除外せず ``already_linked=True`` を付けて
    返す (本文に既にある [blogcard] を黙って落とさない — issue #174 の実装ノート)。
    """
    eligible = [
        (similarity_row[j], j) for j in range(len(document_candidates)) if similarity_row[j] >= min_score
    ]
    if not eligible:
        return []
    eligible.sort(key=lambda t: (-t[0], document_candidates[t[1]]["url"]))

    shortlist_size = max(rerank_top_n, top_k)
    shortlist = eligible[:shortlist_size]
    order = [j for _, j in shortlist]
    cosine_by_index = {j: score for score, j in eligible}

    if reranker is not None and len(order) > 1:
        doc_texts = [document_candidates[j]["embed_text"] for j in order]
        rerank_result = reranker(h2_text, doc_texts)
        if rerank_result:
            reordered = [
                order[r["index"]] for r in rerank_result if 0 <= r["index"] < len(order)
            ]
            seen = set(reordered)
            reordered.extend(j for j in order if j not in seen)
            order = reordered

    top_indices = order[:top_k]
    return [
        {
            **document_candidates[j],
            "score": round(cosine_by_index[j], 3),
            "already_linked": normalize_url(document_candidates[j]["url"]) in existing_urls,
        }
        for j in top_indices
    ]


# --------------------------------------------------------------------------
# レポート生成 (pure function)
# --------------------------------------------------------------------------

def render_markdown_report(
    query_title: str,
    query_link: str,
    h2_entries: list[dict[str, Any]],
    *,
    generated_at: str,
    min_score: float,
    doc_total: int,
) -> str:
    """対象記事の H2 ごとに document 候補を markdown レポートにする。"""
    lines = [
        "# WP → WP 内部リンク候補レポート (H2粒度・omcha-ops#174)",
        "",
        f"- 生成日時: {generated_at}",
        f"- 対象記事: [{query_title}]({query_link})",
        f"- 候補記事総数: {doc_total}",
        f"- H2見出し数: {len(h2_entries)}",
        f"- 類似度閾値 (--min-score): {min_score} (WP→navi・記事単位からの暫定流用。未較正)",
        "",
        "本レポートは候補提案のみです。WP への書き込み・自動挿入は一切行っていません。"
        " 貼るかどうかは本レポートを見てオーナーが判断し、WP を手動編集してください。",
        "",
    ]
    if not h2_entries:
        lines.append("(H2 見出しが検出できませんでした)")
        return "\n".join(lines) + "\n"

    for entry in h2_entries:
        lines.append(f"## {entry['heading']}")
        if not entry["candidates"]:
            lines.append("(類似度閾値以上の候補なし)")
            lines.append("")
            continue
        for i, c in enumerate(entry["candidates"], start=1):
            flag = " **[既出]**" if c.get("already_linked") else ""
            lines.append(f"{i}. [{c['title']}]({c['url']}) (score: {c['score']:.3f}){flag}")
        lines.append("")

    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# 実行本体
# --------------------------------------------------------------------------

def run(
    *,
    index_path: pathlib.Path,
    ruri_url: str = DEFAULT_RURI_URL,
    out_path: pathlib.Path,
    wp_base_url: str = DEFAULT_WP_BASE_URL,
    query_post_id: int | None = None,
    query_content_file: pathlib.Path | None = None,
    min_score: float = DEFAULT_MIN_SCORE,
    top_k: int = DEFAULT_TOP_K,
    rerank_top_n: int = DEFAULT_RERANK_TOP_N,
    use_reranker: bool = True,
    session: requests.Session | None = None,
    sleeper=time.sleep,
) -> dict[str, Any]:
    """query 記事の読み込み → document 候補構築 → 埋め込み照合 → レポート書き込みを行う。"""
    summary: dict[str, Any] = {
        "doc_candidates": 0, "h2_count": 0, "h2_with_candidates": 0, "aborted": False,
    }
    session = session or requests.Session()

    if query_content_file is not None:
        query = load_query_from_file(query_content_file)
    elif query_post_id is not None:
        query = fetch_query_article(wp_base_url, query_post_id, session)
    else:
        raise ValueError("either query_post_id or query_content_file is required")

    if query is None:
        logger.error("failed to load query article; aborting without writing report")
        summary["aborted"] = True
        return summary

    index_entries = load_index_jsonl(index_path)
    document_candidates = build_document_candidates(index_entries, exclude_id=query["id"])
    summary["doc_candidates"] = len(document_candidates)
    if not document_candidates:
        logger.error("no document candidates loaded from %s; aborting", index_path)
        summary["aborted"] = True
        return summary

    h2_sections = extract_h2_sections(query["content_html"])
    summary["h2_count"] = len(h2_sections)
    if not h2_sections:
        logger.error("no H2 headings detected in query article; aborting without writing report")
        summary["aborted"] = True
        return summary

    existing_urls = frozenset(extract_blogcard_urls(query["content_html"]))

    h2_texts = [s["heading"][:MAX_TEXT_LEN] for s in h2_sections]
    doc_texts = [c["embed_text"] for c in document_candidates]

    try:
        h2_vectors = embed_texts_ruri(h2_texts, "query", ruri_url, session, sleeper=sleeper)
        doc_vectors = embed_texts_ruri(doc_texts, "document", ruri_url, session, sleeper=sleeper)
    except EmbeddingBatchError as e:
        logger.error("embedding failed; aborting without writing report: %s", e)
        summary["aborted"] = True
        return summary

    similarity = cosine_similarity_cross(h2_vectors, doc_vectors)

    reranker_fn: RerankerFn | None = None
    if use_reranker:
        def reranker_fn(query_text: str, doc_texts_inner: list[str]) -> list[dict[str, Any]] | None:
            return rerank_candidates(query_text, doc_texts_inner, ruri_url, session, sleeper=sleeper)

    h2_entries: list[dict[str, Any]] = []
    for i, section in enumerate(h2_sections):
        candidates = select_document_candidates_for_h2(
            h2_texts[i], similarity[i], document_candidates,
            min_score=min_score, top_k=top_k, rerank_top_n=rerank_top_n,
            reranker=reranker_fn, existing_urls=existing_urls,
        )
        h2_entries.append({"heading": section["heading"], "candidates": candidates})

    summary["h2_with_candidates"] = sum(1 for e in h2_entries if e["candidates"])

    report = render_markdown_report(
        query["title"], query["link"], h2_entries,
        generated_at=_now_iso(), min_score=min_score, doc_total=len(document_candidates),
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")

    logger.info(
        "done: doc_candidates=%d h2_count=%d h2_with_candidates=%d -> %s",
        summary["doc_candidates"], summary["h2_count"], summary["h2_with_candidates"], out_path,
    )
    print(json.dumps(summary, ensure_ascii=False))
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--index-path", required=True, help="omcha-ops/content/index.jsonl のパス")
    ap.add_argument("--ruri-url", default=os.environ.get("RURI_URL", DEFAULT_RURI_URL), help="Ruri v3 API の URL")
    ap.add_argument("--out", default=DEFAULT_OUT, help="markdown レポートの出力先パス")
    ap.add_argument("--wp-base-url", default=os.environ.get("WP_BASE_URL", DEFAULT_WP_BASE_URL), help="WP のベース URL")

    query_group = ap.add_mutually_exclusive_group(required=True)
    query_group.add_argument("--query-post-id", type=int, help="対象記事の WP post/page ID (公開記事から単発取得)")
    query_group.add_argument("--query-content-file", help="対象記事の wp_draft.py pull 出力形式ローカル JSON")

    ap.add_argument("--min-score", type=float, default=DEFAULT_MIN_SCORE, help="この cosine 類似度未満の候補は出さない")
    ap.add_argument("--top-k", type=int, default=DEFAULT_TOP_K, help="H2 あたりの候補数上限")
    ap.add_argument("--rerank-top-n", type=int, default=DEFAULT_RERANK_TOP_N, help="reranker に渡す候補数の上限")
    ap.add_argument("--no-reranker", action="store_true", help="reranker (/rerank) を使わず cosine 類似度のみで選ぶ")
    args = ap.parse_args()

    summary = run(
        index_path=pathlib.Path(args.index_path),
        ruri_url=args.ruri_url,
        out_path=pathlib.Path(args.out),
        wp_base_url=args.wp_base_url,
        query_post_id=args.query_post_id,
        query_content_file=pathlib.Path(args.query_content_file) if args.query_content_file else None,
        min_score=args.min_score,
        top_k=args.top_k,
        rerank_top_n=args.rerank_top_n,
        use_reranker=not args.no_reranker,
    )
    return 1 if summary.get("aborted") else 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
