"""detect_demand_gaps.py

Issue #3332 N2 消費側 PR1「需要ギャップ検出」レポートスクリプト (inert)。

背景:
  供給側 (Lane A, fetch_suggest_info.py) が data/raw/suggest_info/<theme_key>.json
  に「hub テーマ (年齢/カテゴリ) × 修飾語」の情報型サジェストを収集済み。加えて
  data/analytics/history/gsc_by_query.jsonl (#1329 由来) に実クエリの GSC
  impressions が日次蓄積されている。本スクリプトはこの2系統を需要シグナルとして
  統合し、「需要はあるが記事が無い/弱い」クエリを Ruri v3 (#2995 案1 K8 LLM
  ワーカー) の意味的類似度で検出し、data/analytics/demand_gaps.json にレポート
  する。

  本 PR (PR1) はスクリプト・テスト・validate paths 登録のみで **cron 配線なし・
  CI 実行なし = inert**。実行 (K8/Ruri) は後続 PR2 (amazon-home-ops 側の shadow
  レーン) で行う。

既存コードの再利用 (compute_semantic_related.py, #2995 案1):
  - discover_articles() / load_article_index() / build_embedding_text():
    記事コーパスの列挙・選定・埋め込みテキスト組み立て。
  - embed_batch_ruri(): Ruri v3 API (`/embed`) への文書側 (kind="document") 埋め込み。
  - EmbeddingBatchError: リトライ上限到達時の fail-closed 例外。
  これらを再実装せず import して使う。Ruri v3 は非対称埋め込み (クエリ/文書で
  プレフィックスが異なる) のため、クエリ側は kind="query" 用の薄いラッパ
  (embed_batch_ruri_query, 本ファイル内) を別途用意する — 同じ /embed
  エンドポイントに kind だけ変えて投げる、embed_batch_ruri とほぼ同一のリトライ
  ロジック。

アルゴリズム:
  1. 需要クエリ集合を構築する: suggest_info 全ファイルの suggestions[].query
     (source="suggest", theme_key 付与)、gsc_by_query.jsonl (navi 自身、
     source="gsc", --min-impressions 以上)、gsc_wp_by_query.jsonl (omcha.jp
     本家、source="gsc_wp", --min-wp-impressions 以上) を正規化
     (NFKC + 空白圧縮 + strip) してマージする。1クエリが複数の source を
     持ちうる (sources リスト)。impressions は **サイトごとに別フィールド**
     (impressions / wp_impressions) に持ち、合算しない。
  1.5. 日本語 (ひらがな/カタカナ/漢字) を一切含まず ASCII 英数・記号のみの
     クエリ (ASIN/型番のナビゲーショナル検索、例: "b0h1b5qdjk", "75445") を
     母集団から除外する (is_code_like_query)。除外件数は summary に
     excluded_code_queries として記録する。同様に theme_key=="english"
     (english.json シード由来の「〜を英語で？」翻訳/語学クエリ) も
     コンテンツ機会でないノイズとして除外し、excluded_english_queries
     として記録する。
  2. 記事コーパス (load_article_index) を Ruri で kind="document" 埋め込み。
  3. 需要クエリを Ruri で kind="query" 埋め込み。
  4. 各クエリについて全記事ベクトルとの最大コサイン類似度 (max_sim) と
     その記事 (nearest_asin/nearest_title) を求める。
  5. max_sim < --gap-threshold をギャップと判定する。
  6. ギャップ一覧を需要シグナル (WP impressions → suggest 出現 → navi
     impressions) 降順で並べる。
  7. data/analytics/demand_gaps.json に summary (総需要クエリ数・ギャップ数・
     除外コード系クエリ数・テーマ別内訳) + gaps 一覧を書き出す。

なぜ WP (omcha.jp) の GSC を需要の一次シグナルにするか (2026-08-10 実測):
  navi 自身の GSC は 30 日で 239 クエリ / 2,202 impressions しかなく、これを
  需要の定義に使うと「既に記事があって露出している語」しか出てこない (供給駆動の
  循環参照)。同領域の omcha.jp は 30 日 526 クエリ / 112,205 impressions = 51 倍
  あり、上位は「メルちゃん 服」(imp 14,863) 「ぷにるんず」(6,930) のように
  **navi に在庫が無い語**で立っている。実際 WP で imp>=200 の 90 クエリのうち
  navi にブランド在庫があるのは 8 件 (imp 6,198) で、残り 82 件 (imp 87,507 =
  93%) は記事が無い。需要はここにあって navi の実績側には無い。

エラー処理 (fail-closed, compute_semantic_related と同様):
  Ruri への embed リクエストが最終的に失敗した場合は EmbeddingBatchError を
  送出して非 0 終了し、出力ファイルへの書き込みは一切行わない (部分成果の
  保存はしない)。需要クエリ 0 件、または記事コーパス 0 件の場合は Ruri を
  一切呼ばずに summary はゼロ件・gaps 空で正常終了 (exit 0) する。

呼び出し元:
  amazon-home-ops リポジトリの shadow レーン (PR2, 未実装) から
  `python -m scripts.detect_demand_gaps ...` として実行される想定
  (CI からは呼ばない — inert)。

Issue: https://github.com/omochairo/amazon/issues/3332 (N2 消費側 PR1)
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import pathlib
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from typing import Any, Callable

import requests

# compute_semantic_related.py は `from scripts import ...` 形式だと
# `python scripts/detect_demand_gaps.py` の直接実行で ImportError になる
# (scripts/ に __init__.py が無く、パッケージとして解決できないため)。
# `python -m scripts.detect_demand_gaps` / 直接実行の両方で動くように、
# scripts/ ディレクトリ自体を sys.path に足してから単純 import する。
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import compute_semantic_related as C  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("detect_demand_gaps")

DEFAULT_ARTICLES_DIR = "data/articles"
DEFAULT_SUGGEST_DIR = "data/raw/suggest_info"
DEFAULT_GSC_QUERY_PATH = "data/analytics/history/gsc_by_query.jsonl"
# 2026-08-10: 需要ソースに omcha.jp (WordPress 本家) の GSC を足した。
# navi 自身の gsc_by_query は 30 日で 239 クエリ / 2,202 impressions しかなく、
# 「需要」を測る母数として小さすぎる (サイト全体で 376 imp/日)。同じ知育玩具領域で
# omcha.jp は 30 日 526 クエリ / 112,205 impressions = **51 倍**あり、しかも
# 「メルちゃん 服」「ぷにるんず」のように navi に在庫が無い語で需要が立っている。
# 需要を navi 自身の実績だけで定義すると、既に記事がある語しか出てこない
# (供給駆動の循環参照) ので、本家側の実測需要を一次シグナルにする。
DEFAULT_GSC_WP_QUERY_PATH = "data/analytics/history/gsc_wp_by_query.jsonl"
DEFAULT_OUT = "data/analytics/demand_gaps.json"
DEFAULT_RURI_URL = "http://localhost:8000"
# 2026-07-19 較正 (home-ops run 29685348953, 需要クエリ 2,056 件全数の
# max_sim 分布): p0=0.786 p25=0.855 p50=0.868 p75=0.878 p95=0.894 p100=0.932
# の狭帯域に圧縮される。0.5 には何も落ちない死んだ閾値だった。0.84 は
# p50 をカバー済みの中心とし、p5〜p10 帯 (0.84 時点で自然文ギャップ 137 件、
# コード系ノイズ 18 件) を review 対象にする較正値。
DEFAULT_GAP_THRESHOLD = 0.84
DEFAULT_MIN_IMPRESSIONS = 3
# WP 側は母数が 51 倍あるので下限も別に持つ。3 のままだと裾のノイズを大量に拾う。
# 実測 (30 日): imp>=200 で 90 クエリ、imp>=50 で 214 クエリ。
DEFAULT_MIN_WP_IMPRESSIONS = 50

SOURCE_SUGGEST = "suggest"
SOURCE_GSC = "gsc"
SOURCE_GSC_WP = "gsc_wp"

_SPACE_RE = re.compile(r"\s+")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# クエリ正規化
# --------------------------------------------------------------------------

def normalize_query(text: Any) -> str:
    """需要クエリのマージ・dedupe キー用正規化 (NFKC + 空白圧縮 + strip + lower)。"""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text)).lower()
    return _SPACE_RE.sub(" ", t).strip()


def is_code_like_query(query: Any) -> bool:
    """ASIN/型番のナビゲーショナル検索 (例: "b0h1b5qdjk", "75445", "hnw46-986q") を判定する。

    日本語 (ひらがな/カタカナ/漢字) を一切含まず ASCII 文字のみで構成される
    クエリはコンテンツギャップではなく型番のナビゲーショナル検索とみなし、
    gap 判定の母集団から除外する対象。ASCII のみという条件は「日本語を
    含まない」を自動的に含意する (日本語の各文字コードポイントは非 ASCII)
    ため、判定はこの一条件のみで足りる。
    """
    q = str(query).strip() if query else ""
    if not q:
        return False
    return all(ord(ch) < 128 for ch in q)


# --------------------------------------------------------------------------
# 需要クエリ集合の構築
# --------------------------------------------------------------------------

def load_suggest_queries(suggest_dir: pathlib.Path) -> list[dict[str, str]]:
    """data/raw/suggest_info/*.json 全ファイルから需要クエリを集める。

    各ファイルの theme_key を付与する。壊れたファイル (読み込み/パース失敗、
    非 dict、suggestions が list でない) はスキップし、run 全体は落とさない。
    """
    out: list[dict[str, str]] = []
    if not suggest_dir.exists():
        return out
    for f in sorted(suggest_dir.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("skip %s: failed to read/parse: %s", f, e)
            continue
        if not isinstance(data, dict):
            logger.warning("skip %s: suggest_info JSON is not an object", f)
            continue
        theme_key = data.get("theme_key")
        theme_key = theme_key if isinstance(theme_key, str) and theme_key else f.stem
        suggestions = data.get("suggestions")
        if not isinstance(suggestions, list):
            continue
        for s in suggestions:
            if not isinstance(s, dict):
                continue
            q = s.get("query")
            if isinstance(q, str) and q.strip():
                out.append({"query": q.strip(), "theme_key": theme_key})
    return out


def load_gsc_impressions(gsc_query_path: pathlib.Path, min_impressions: int) -> dict[str, dict[str, Any]]:
    """gsc_by_query.jsonl をクエリ単位で impressions 合算し、閾値以上のみ返す。

    戻り値のキーは normalize_query() 済みのクエリ、値は
    ``{"query": 表示用の生文字列 (最初に見たもの), "impressions": 合算値}``。
    壊れた行 (JSON パース失敗・非 dict・型不正) は個別にスキップする。
    """
    agg: dict[str, dict[str, Any]] = {}
    if not gsc_query_path.exists():
        return agg
    with gsc_query_path.open(encoding="utf-8") as f:
        for line_no, raw_line in enumerate(f, 1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as e:
                logger.warning("skip malformed line %d in %s: %s", line_no, gsc_query_path, e)
                continue
            if not isinstance(row, dict):
                continue
            q = row.get("query")
            impressions = row.get("impressions")
            if not isinstance(q, str) or not q.strip():
                continue
            if not isinstance(impressions, (int, float)) or isinstance(impressions, bool):
                continue
            key = normalize_query(q)
            if not key:
                continue
            entry = agg.setdefault(key, {"query": q.strip(), "impressions": 0})
            entry["impressions"] += impressions

    return {k: v for k, v in agg.items() if v["impressions"] >= min_impressions}


def _new_demand_entry(query: str) -> dict[str, Any]:
    return {"query": query, "sources": [], "impressions": 0, "wp_impressions": 0, "theme_key": None}


def build_demand_queries(
    suggest_dir: pathlib.Path,
    gsc_query_path: pathlib.Path,
    min_impressions: int,
    gsc_wp_query_path: pathlib.Path | None = None,
    min_wp_impressions: int = DEFAULT_MIN_WP_IMPRESSIONS,
) -> list[dict[str, Any]]:
    """suggest_info と 2 系統の GSC を正規化キーでマージした需要クエリ一覧を返す。

    1クエリが複数の source を持ちうる (sources に全部積む)。theme_key は suggest
    側からのみ得られる (gsc 単独ヒットは None)。

    impressions は **サイトごとに別フィールドに持つ**:

    - ``impressions``    … navi (navi.omcha.jp) 自身の GSC 実績
    - ``wp_impressions`` … omcha.jp (WordPress 本家) の GSC 実績

    合算しないのは別サイトの数字だからで、意味が違う。navi 側は「既に記事があって
    露出している」ことの実績 (=供給の裏返し) だが、WP 側は「この領域に需要がある」
    ことの証拠で、navi に記事があるかどうかと独立している。ギャップ検出で効かせたい
    のは後者なので _demand_rank_key は wp_impressions を第一キーにしている。
    """
    merged: dict[str, dict[str, Any]] = {}

    for item in load_suggest_queries(suggest_dir):
        key = normalize_query(item["query"])
        if not key:
            continue
        entry = merged.setdefault(key, _new_demand_entry(item["query"]))
        if SOURCE_SUGGEST not in entry["sources"]:
            entry["sources"].append(SOURCE_SUGGEST)
        if entry["theme_key"] is None:
            entry["theme_key"] = item["theme_key"]

    gsc_agg = load_gsc_impressions(gsc_query_path, min_impressions)
    for key, g in gsc_agg.items():
        entry = merged.setdefault(key, _new_demand_entry(g["query"]))
        if SOURCE_GSC not in entry["sources"]:
            entry["sources"].append(SOURCE_GSC)
        entry["impressions"] = g["impressions"]

    if gsc_wp_query_path is not None:
        wp_agg = load_gsc_impressions(gsc_wp_query_path, min_wp_impressions)
        for key, g in wp_agg.items():
            entry = merged.setdefault(key, _new_demand_entry(g["query"]))
            if SOURCE_GSC_WP not in entry["sources"]:
                entry["sources"].append(SOURCE_GSC_WP)
            entry["wp_impressions"] = g["impressions"]

    return list(merged.values())


# --------------------------------------------------------------------------
# Ruri embed (クエリ側 kind="query" ラッパ)
# --------------------------------------------------------------------------

def embed_batch_ruri_query(
    texts: list[str],
    ruri_url: str,
    session: requests.Session,
    sleeper=time.sleep,
) -> list[list[float]]:
    """1 バッチ分のクエリを amazon-home-ops の Ruri v3 API (`/embed`) で kind="query"
    としてベクトル化する。

    compute_semantic_related.embed_batch_ruri は文書側 (kind="document") 固定。
    Ruri v3 は非対称埋め込みでプレフィックスをサービス側が kind から出し分ける
    ため、クエリ側はこの薄いラッパで kind="query" を明示する。リトライ挙動・
    エラー型 (compute_semantic_related.EmbeddingBatchError) は embed_batch_ruri
    と同一。
    """
    url = f"{ruri_url.rstrip('/')}/embed"
    last_err: Exception | None = None
    attempts = C._MAX_EXTRA_RETRIES + 1
    for attempt in range(1, attempts + 1):
        try:
            resp = session.post(
                url, json={"texts": texts, "kind": "query"}, timeout=C.REQUEST_TIMEOUT
            )
            resp.raise_for_status()
            payload = resp.json()
            vectors = payload.get("vectors") if isinstance(payload, dict) else None
            if not isinstance(vectors, list) or len(vectors) != len(texts):
                raise C.EmbeddingBatchError(
                    f"unexpected /embed response shape (expected {len(texts)} vectors)"
                )
            return vectors
        except (requests.RequestException, C.EmbeddingBatchError, ValueError) as e:
            last_err = e
            if attempt < attempts:
                logger.warning(
                    "ruri query embed batch failed (attempt %d/%d): %s; retrying", attempt, attempts, e
                )
                sleeper(C._RETRY_SLEEP_SECONDS)
            else:
                logger.error("ruri query embed batch failed after %d attempt(s): %s", attempts, e)
    raise C.EmbeddingBatchError(str(last_err)) from last_err


def _embed_chunked(
    texts: list[str],
    embed_fn: Callable[..., list[list[float]]],
    ruri_url: str,
    session: requests.Session,
    sleeper,
    batch_size: int = 0,
    label: str = "document",
) -> list[list[float]]:
    """``texts`` を ``batch_size`` 件ずつに分割して ``embed_fn`` を呼び、結果を連結する。

    embed_batch_ruri / embed_batch_ruri_query は「1 呼び出し = 1 HTTP リクエスト」の
    低レベル関数で、バッチ分割は呼び出し側の責務
    (compute_semantic_related.compute_embeddings と同じ役割分担)。全件を単一
    リクエストに載せると記事 1,500 件超では Ruri のエンコードが REQUEST_TIMEOUT
    (120s) を必ず超過し、リトライも同じ巨大リクエストを再送するだけで回復しない
    (2026-07-19 初回 shadow run 29683020962 で実測)。
    """
    if batch_size <= 0:
        batch_size = C.DEFAULT_BATCH_SIZE
    vectors: list[list[float]] = []
    n_batches = (len(texts) + batch_size - 1) // batch_size
    for bi in range(n_batches):
        chunk = texts[bi * batch_size: (bi + 1) * batch_size]
        vectors.extend(embed_fn(chunk, ruri_url, session, sleeper))
        if (bi + 1) % C._BATCH_LOG_EVERY == 0 or (bi + 1) == n_batches:
            logger.info("%s embed: %d/%d batches done", label, bi + 1, n_batches)
    return vectors


# --------------------------------------------------------------------------
# 記事コーパス (タイトル付き)
# --------------------------------------------------------------------------

def load_article_titles(articles_dir: pathlib.Path, limit: int = 0) -> dict[str, str]:
    """discover_articles() の選定結果と同じ ASIN 集合について title のみ読む。

    load_article_index() と選定ロジック (discover_articles → ASIN 昇順 → limit
    切り詰め) を揃えているため、同じ articles_dir/limit で呼べば同一 ASIN 集合
    になる。nearest_title 表示専用の軽量ヘルパ。
    """
    paths = C.discover_articles(articles_dir)
    asins = sorted(paths.keys())
    if limit and limit > 0:
        asins = asins[:limit]

    titles: dict[str, str] = {}
    for asin in asins:
        path = paths[asin]
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        title = data.get("title")
        titles[asin] = title.strip() if isinstance(title, str) else ""
    return titles


# --------------------------------------------------------------------------
# クエリ×記事 の最大コサイン類似度 (矩形行列)
# --------------------------------------------------------------------------

def _normalize_vectors_pure(vectors: list[list[float]]) -> list[list[float]]:
    normed: list[list[float]] = []
    for v in vectors:
        norm = math.sqrt(sum(x * x for x in v))
        if norm == 0:
            normed.append([0.0] * len(v))
        else:
            normed.append([x / norm for x in v])
    return normed


def _max_similarity_pure(
    query_vectors: list[list[float]], doc_vectors: list[list[float]]
) -> list[tuple[int, float]]:
    """純 Python によるクエリ×記事 最大コサイン類似度 (numpy 不在時のフォールバック)。

    compute_semantic_related._cosine_similarity_pure は正方行列 (記事×記事) 専用
    のため、ここではクエリ×記事の矩形行列用に別実装する。戻り値は
    query_vectors と同順の [(最も近い doc のインデックス, その類似度), ...]。
    """
    qn = _normalize_vectors_pure(query_vectors)
    dn = _normalize_vectors_pure(doc_vectors)
    results: list[tuple[int, float]] = []
    for qv in qn:
        best_idx = -1
        best_score = float("-inf")
        for i, dv in enumerate(dn):
            score = sum(a * b for a, b in zip(qv, dv))
            if score > best_score:
                best_score = score
                best_idx = i
        results.append((best_idx, best_score))
    return results


def _max_similarity_numpy(
    query_vectors: list[list[float]], doc_vectors: list[list[float]]
) -> list[tuple[int, float]]:
    import numpy as np

    q = np.asarray(query_vectors, dtype=float)
    d = np.asarray(doc_vectors, dtype=float)
    q_norms = np.linalg.norm(q, axis=1, keepdims=True)
    q_norms[q_norms == 0] = 1.0
    d_norms = np.linalg.norm(d, axis=1, keepdims=True)
    d_norms[d_norms == 0] = 1.0
    sim = (q / q_norms) @ (d / d_norms).T
    best_idx = sim.argmax(axis=1)
    best_score = sim[np.arange(sim.shape[0]), best_idx]
    return list(zip(best_idx.tolist(), best_score.tolist()))


def compute_max_similarity(
    query_vectors: list[list[float]], doc_vectors: list[list[float]]
) -> list[tuple[int, float]]:
    """各クエリベクトルについて全記事ベクトル中の最大コサイン類似度を計算する。

    numpy が import できればベクトル化した計算を使い、できなければ
    ``_max_similarity_pure`` にフォールバックする。doc_vectors が空の場合は
    呼び出し元 (run) が事前にガードする想定 (ここでは呼ばれない)。
    """
    if not query_vectors or not doc_vectors:
        return [(-1, 0.0) for _ in query_vectors]
    try:
        return _max_similarity_numpy(query_vectors, doc_vectors)
    except ImportError:
        return _max_similarity_pure(query_vectors, doc_vectors)


# --------------------------------------------------------------------------
# ランク付け・出力
# --------------------------------------------------------------------------

def _demand_rank_key(q: dict[str, Any]) -> tuple[float, int, float, str]:
    """需要シグナル降順のソートキー。

    優先順位: (1) omcha.jp (WP) の impressions 降順、(2) suggest 出現の有無、
    (3) navi の impressions 降順、(4) query 昇順 (決定的な同点順序)。

    2026-08-10 に (1) を最上位へ変更した。旧実装は「suggest 出現あり」を最優先の
    二値にしていたが、suggest は Google サジェストの出現有無であって**量が付いて
    いない**ため、上位が数百件の同点で埋まり実質 query 昇順になっていた
    (docstring 自身が「実データを見て加重合成に変える余地がある」としていた箇所)。
    実データが揃った結果、量つきで最も強い需要シグナルは WP の impressions だった
    ので、これを一次キーにする。navi 側 impressions を一次キーにしない理由は
    build_demand_queries の docstring を参照 (供給の裏返しなので循環参照になる)。
    """
    wp_impressions = q.get("wp_impressions") or 0
    has_suggest = 1 if SOURCE_SUGGEST in q["sources"] else 0
    impressions = q.get("impressions") or 0
    return (-wp_impressions, -has_suggest, -impressions, q["query"])


def _write_output(out_path: pathlib.Path, payload: dict[str, Any]) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
# 実行本体
# --------------------------------------------------------------------------

def run(
    articles_dir: pathlib.Path,
    suggest_dir: pathlib.Path,
    gsc_query_path: pathlib.Path,
    out_path: pathlib.Path,
    gap_threshold: float = DEFAULT_GAP_THRESHOLD,
    min_impressions: int = DEFAULT_MIN_IMPRESSIONS,
    gsc_wp_query_path: pathlib.Path | None = None,
    min_wp_impressions: int = DEFAULT_MIN_WP_IMPRESSIONS,
    limit_articles: int = 0,
    dry_run: bool = False,
    ruri_url: str = DEFAULT_RURI_URL,
    session: requests.Session | None = None,
    sleeper=time.sleep,
    embed_document_fn: Callable[..., list[list[float]]] | None = None,
    embed_query_fn: Callable[..., list[list[float]]] | None = None,
) -> dict[str, Any]:
    """全体を実行し、件数サマリを dict で返す (テスト・main 双方から呼べるように分離)。

    ``embed_document_fn`` / ``embed_query_fn`` はテスト用の DI ポイント
    (既定はそれぞれ compute_semantic_related.embed_batch_ruri /
    embed_batch_ruri_query の実 HTTP 呼び出し)。シグネチャは
    ``(texts, ruri_url, session, sleeper) -> list[list[float]]``。いずれも
    ``_embed_chunked`` 経由で最大 ``C.DEFAULT_BATCH_SIZE`` 件ずつに分割して
    呼ばれる (1 呼び出し = 1 リクエスト)。

    需要クエリ 0 件、または記事コーパス 0 件の場合は Ruri を一切呼ばずに
    summary をゼロ件・gaps 空で書き出し正常終了する (--dry-run 時は書き込み
    もしない)。埋め込みが最終的に失敗した場合は
    compute_semantic_related.EmbeddingBatchError を送出し、出力ファイルへの
    書き込みは一切行わない (fail-closed、部分成果を保存しない)。
    """
    embed_document_fn = embed_document_fn or C.embed_batch_ruri
    embed_query_fn = embed_query_fn or embed_batch_ruri_query

    demand_queries_all = build_demand_queries(
        suggest_dir, gsc_query_path, min_impressions,
        gsc_wp_query_path=gsc_wp_query_path, min_wp_impressions=min_wp_impressions,
    )
    after_code = [q for q in demand_queries_all if not is_code_like_query(q["query"])]
    excluded_code_queries = len(demand_queries_all) - len(after_code)
    demand_queries = [q for q in after_code if q.get("theme_key") != "english"]
    excluded_english_queries = len(after_code) - len(demand_queries)
    total_demand_queries = len(demand_queries)

    article_items = C.load_article_index(articles_dir, limit_articles)
    titles = load_article_titles(articles_dir, limit_articles)

    logger.info(
        "demand queries: %d (excluded %d code-like), articles: %d (gap_threshold=%.3f, min_impressions=%d)",
        total_demand_queries, excluded_code_queries, len(article_items), gap_threshold, min_impressions,
    )

    params = {
        "gap_threshold": gap_threshold,
        "min_impressions": min_impressions,
        "min_wp_impressions": min_wp_impressions,
        "wp_demand_enabled": gsc_wp_query_path is not None,
        "ruri_url": ruri_url,
    }

    if not demand_queries or not article_items:
        logger.info(
            "empty input (demand_queries=%d, articles=%d); writing empty gap report without calling Ruri",
            total_demand_queries, len(article_items),
        )
        result = {
            "generated_at": _now_iso(),
            "params": params,
            "summary": {
                "total_demand_queries": total_demand_queries,
                "gap_count": 0,
                "by_theme": {},
                "excluded_code_queries": excluded_code_queries,
                "excluded_english_queries": excluded_english_queries,
            },
            "gaps": [],
        }
        if not dry_run:
            _write_output(out_path, result)
        return {"total_demand_queries": total_demand_queries, "gap_count": 0, "written": not dry_run}

    session = session or requests.Session()

    asins = [a for a, _ in article_items]
    doc_texts = [t for _, t in article_items]
    doc_vectors = _embed_chunked(
        doc_texts, embed_document_fn, ruri_url, session, sleeper, label="document"
    )

    query_texts = [q["query"] for q in demand_queries]
    query_vectors = _embed_chunked(
        query_texts, embed_query_fn, ruri_url, session, sleeper, label="query"
    )

    sims = compute_max_similarity(query_vectors, doc_vectors)

    gaps: list[dict[str, Any]] = []
    by_theme: dict[str, int] = {}
    for q, (best_idx, best_score) in zip(demand_queries, sims):
        max_sim = round(best_score, 3)
        if max_sim >= gap_threshold:
            continue
        nearest_asin = asins[best_idx] if best_idx >= 0 else None
        nearest_title = titles.get(nearest_asin, "") if nearest_asin else ""
        gaps.append({
            "query": q["query"],
            "sources": list(q["sources"]),
            "impressions": q["impressions"],
            "wp_impressions": q.get("wp_impressions", 0),
            "theme_key": q["theme_key"],
            "max_sim": max_sim,
            "nearest_asin": nearest_asin,
            "nearest_title": nearest_title,
        })
        if q["theme_key"]:
            by_theme[q["theme_key"]] = by_theme.get(q["theme_key"], 0) + 1

    gaps.sort(key=_demand_rank_key)

    result = {
        "generated_at": _now_iso(),
        "params": params,
        "summary": {
            "total_demand_queries": total_demand_queries,
            "gap_count": len(gaps),
            "by_theme": by_theme,
            "excluded_code_queries": excluded_code_queries,
            "excluded_english_queries": excluded_english_queries,
        },
        "gaps": gaps,
    }

    if dry_run:
        logger.info(
            "[dry-run] computed %d gap(s) out of %d demand queries; not writing output",
            len(gaps), total_demand_queries,
        )
        return {"total_demand_queries": total_demand_queries, "gap_count": len(gaps), "written": False}

    _write_output(out_path, result)
    logger.info("wrote %s: %d gap(s) out of %d demand queries", out_path, len(gaps), total_demand_queries)
    return {"total_demand_queries": total_demand_queries, "gap_count": len(gaps), "written": True}


def main() -> None:
    ap = argparse.ArgumentParser(
        description="需要はあるが記事が無い/弱いクエリを検出する (#3332 N2 消費側 PR1、inert)"
    )
    ap.add_argument("--articles-dir", default=DEFAULT_ARTICLES_DIR, help="記事 JSON のディレクトリ")
    ap.add_argument("--suggest-dir", default=DEFAULT_SUGGEST_DIR, help="suggest_info (Lane A 収集結果) のディレクトリ")
    ap.add_argument("--gsc-query", default=DEFAULT_GSC_QUERY_PATH, help="GSC クエリ別履歴 jsonl のパス")
    ap.add_argument(
        "--ruri-url",
        default=os.environ.get("RURI_URL", DEFAULT_RURI_URL),
        help="Ruri v3 API の URL (#2995 案1 K8 LLM ワーカー)",
    )
    ap.add_argument("--out", default=DEFAULT_OUT, help="出力先 JSON パス")
    ap.add_argument(
        "--gap-threshold",
        type=float,
        default=DEFAULT_GAP_THRESHOLD,
        help="max_sim がこれ未満をギャップと判定する閾値 (較正可能、既定は初期値の当てずっぽう)",
    )
    ap.add_argument("--min-impressions", type=int, default=DEFAULT_MIN_IMPRESSIONS, help="navi GSC を需要とみなす impressions の下限")
    ap.add_argument("--gsc-wp-query", default=DEFAULT_GSC_WP_QUERY_PATH,
                    help="omcha.jp (WP 本家) の GSC クエリ別履歴 jsonl のパス")
    ap.add_argument("--min-wp-impressions", type=int, default=DEFAULT_MIN_WP_IMPRESSIONS,
                    help="WP GSC を需要とみなす impressions の下限")
    ap.add_argument("--no-wp-demand", action="store_true",
                    help="WP 側の需要ソースを無効化する (旧挙動: navi 自身の GSC + suggest のみ)")
    ap.add_argument("--limit-articles", type=int, default=0, help="処理する記事数の上限 (0=全件、テスト/デバッグ用)")
    ap.add_argument("--dry-run", action="store_true", help="計算とログ出力のみ行い、出力ファイルへの書き込みは行わない")
    args = ap.parse_args()

    try:
        run(
            articles_dir=pathlib.Path(args.articles_dir),
            suggest_dir=pathlib.Path(args.suggest_dir),
            gsc_query_path=pathlib.Path(args.gsc_query),
            gsc_wp_query_path=(None if args.no_wp_demand else pathlib.Path(args.gsc_wp_query)),
            min_wp_impressions=args.min_wp_impressions,
            out_path=pathlib.Path(args.out),
            gap_threshold=args.gap_threshold,
            min_impressions=args.min_impressions,
            limit_articles=args.limit_articles,
            dry_run=args.dry_run,
            ruri_url=args.ruri_url,
        )
    except C.EmbeddingBatchError as e:
        logger.error("embedding computation failed: %s; aborting without writing output", e)
        raise SystemExit(1) from e


if __name__ == "__main__":
    main()
