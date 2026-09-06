"""compute_semantic_related.py

Issue #2995 案1「意味的に近い記事」レーンの埋め込み計算スクリプト。

data/articles/*.json (ASIN ごとの記事、`*.quality.json` sidecar は除外) から
埋め込み用テキストを組み立て、Ollama の embeddings API (`/api/embed`) でベクトル化、
全記事ペアのコサイン類似度を計算して各記事の上位近傍を
``hugo/data/semantic_related.json`` に書き出す。Hugo テンプレート側は
``site.Data.semantic_related`` として消費する (related_carousel.html #2995 案1 統合)。

なぜこのリポジトリにスクリプトだけ置くか:
  埋め込み計算は Ollama (ローカル LLM ランタイム) を要求するため、GitHub Actions
  ホストでは動かせない。自宅 NAS の self-hosted runner から週次で実行する想定
  (fetch_google_suggest.py と同様の「収集/計算ロジックのみこのリポジトリに置く」
  流儀)。

対象選定:
  data/articles/*.json を列挙し、`*.quality.json` sidecar は除外する。ファイル名
  `YYYY-MM-DD-<ASIN>.json` の最後のハイフン区切りトークンを ASIN として扱い、
  ``^[A-Z0-9]{10}$`` で検証する (build_post.py の _winning_stems と同じ「rewrite
  で新旧併存時は stem が最も新しい (文字列として大きい) ものを採用する」流儀、
  #2711)。

埋め込みテキスト:
  title / product.name / タグ / キーワード / narrative.lead / 対象年齢 /
  知育領域 を改行区切りで連結し (空要素はスキップ)、1200 文字に切り詰める。
  記事 JSON の構造は部分的に欠けていてもクラッシュしない (全フィールド optional)。

類似度計算:
  numpy が import できれば行正規化 + 行列積で計算し (M @ M.T)、import できなければ
  純 Python の正規化 + 内積フォールバックを使う (どちらも unit test 可能なように
  関数を分離)。

エラー処理:
  Ollama への embed リクエストが HTTP エラー / 接続エラーで失敗した場合、同じ
  バッチを最大 2 回まで再試行し、それでも失敗したら例外を送出して非 0 終了する。
  類似度行列の一部だけ計算された不完全な出力は無意味なので、fetch_google_suggest.py
  と異なり **部分成果の保存はしない** (graceful-partial ではなく all-or-nothing)。

呼び出し元:
  omochairo/amazon-home-ops リポジトリの週次 workflow から
  `python -m scripts.compute_semantic_related ...` として実行される想定。

Issue: https://github.com/omochairo/amazon/issues/2995
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import pathlib
import re
import time
from datetime import datetime, timezone
from typing import Any

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("compute_semantic_related")

DEFAULT_ARTICLES_DIR = "data/articles"
DEFAULT_OUT = "hugo/data/semantic_related.json"
DEFAULT_TOP_K = 8
DEFAULT_MIN_SCORE = 0.60
DEFAULT_BATCH_SIZE = 32
DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_MODEL = "nomic-embed-text"
DEFAULT_BACKEND = "ollama"
DEFAULT_RURI_URL = "http://localhost:8000"
DEFAULT_MODEL_RURI = "cl-nagoya/ruri-v3-310m"

REQUEST_TIMEOUT = 120
MAX_TEXT_LEN = 1200
_MAX_EXTRA_RETRIES = 2  # 初回 + 2 リトライ = 最大 3 回試行
_RETRY_SLEEP_SECONDS = 2.0
_BATCH_LOG_EVERY = 10

# #4826 項目6: 除外していたのは .quality.json だけだった。あの sidecar は
# 廃止されて実体が 0 件になった一方、実在するのは .seo.json / .enrichment.json で、
# それらが素通りしないのは下の _ASIN_RE が `<stem>.seo` を弾くから **だけ** だった。
# 偶然の防波堤 1 枚に頼る形なので、除外そのものを実体に合わせる。
_SIDECAR_SUFFIXES = (".quality.json", ".seo.json", ".enrichment.json")
_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")


class EmbeddingBatchError(Exception):
    """Ollama embed バッチがリトライ上限まで失敗した (この時点で呼び出し元は書き込みをしない)。"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# 対象選定
# --------------------------------------------------------------------------

def discover_articles(articles_dir: pathlib.Path) -> dict[str, pathlib.Path]:
    """data/articles/*.json から {asin: article_path} を作る。

    `*.quality.json` sidecar は除外。ファイル名の最後のハイフン区切りトークンを
    ASIN とし、``^[A-Z0-9]{10}$`` を満たさないものはスキップする。同一 ASIN の
    記事が複数ある場合 (rewrite で新旧併存, #2711) は stem が最も新しい
    (文字列として大きい) ものを採用する。
    """
    if not articles_dir.exists():
        return {}
    winners: dict[str, str] = {}
    paths: dict[str, pathlib.Path] = {}
    for f in sorted(articles_dir.glob("*.json")):
        if f.name.endswith(_SIDECAR_SUFFIXES):
            continue
        stem = f.stem
        parts = stem.split("-")
        asin = parts[-1] if parts else ""
        if not _ASIN_RE.match(asin):
            continue
        if asin not in winners or stem > winners[asin]:
            winners[asin] = stem
            paths[asin] = f
    return paths


# --------------------------------------------------------------------------
# 埋め込みテキスト組み立て (pure function)
# --------------------------------------------------------------------------

def build_embedding_text(article: dict[str, Any]) -> str:
    """記事 JSON dict から埋め込み用テキストを組み立てる。

    title / product.name / タグ / キーワード / narrative.lead / 対象年齢 /
    知育領域 を改行区切りで連結 (空要素はスキップ)、1200 文字に切り詰める。
    どのフィールドが欠けていてもクラッシュしない。
    """
    if not isinstance(article, dict):
        return ""

    parts: list[str] = []

    title = article.get("title")
    if isinstance(title, str) and title.strip():
        parts.append(title.strip())

    product = article.get("product")
    product = product if isinstance(product, dict) else {}

    name = product.get("name")
    if isinstance(name, str) and name.strip():
        parts.append(name.strip())

    tags = article.get("tags")
    if isinstance(tags, list):
        tag_strs = [str(t).strip() for t in tags if isinstance(t, str) and t.strip()]
        if tag_strs:
            parts.append("タグ: " + ", ".join(tag_strs))

    keywords = article.get("keywords")
    if isinstance(keywords, list):
        kw_strs = [str(k).strip() for k in keywords if isinstance(k, str) and k.strip()]
        if kw_strs:
            parts.append("キーワード: " + ", ".join(kw_strs))

    narrative = article.get("narrative")
    narrative = narrative if isinstance(narrative, dict) else {}
    lead = narrative.get("lead")
    if isinstance(lead, str) and lead.strip():
        parts.append(lead.strip())

    persona_fit = article.get("persona_fit")
    persona_fit = persona_fit if isinstance(persona_fit, dict) else {}
    age_range = persona_fit.get("age_range")
    if isinstance(age_range, str) and age_range.strip():
        parts.append("対象年齢: " + age_range.strip())

    edu_domains = product.get("edu_domains")
    if isinstance(edu_domains, list):
        edu_strs = [str(e).strip() for e in edu_domains if isinstance(e, str) and e.strip()]
        if edu_strs:
            parts.append("知育領域: " + ", ".join(edu_strs))

    text = "\n".join(parts)
    return text[:MAX_TEXT_LEN]


def load_article_index(articles_dir: pathlib.Path, limit: int = 0) -> list[tuple[str, str]]:
    """discover_articles() の結果を読み込み、(asin, embedding_text) のリストを返す。

    ASIN 昇順で安定ソートしてから --limit (0=全件) で切り詰める
    (スモークテスト用途、決定的な順序にするため)。
    """
    paths = discover_articles(articles_dir)
    asins = sorted(paths.keys())
    if limit and limit > 0:
        asins = asins[:limit]

    items: list[tuple[str, str]] = []
    for asin in asins:
        path = paths[asin]
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("skip %s: failed to read/parse %s: %s", asin, path, e)
            continue
        if not isinstance(data, dict):
            logger.warning("skip %s: article JSON at %s is not an object", asin, path)
            continue
        items.append((asin, build_embedding_text(data)))
    return items


# --------------------------------------------------------------------------
# Ollama embeddings
# --------------------------------------------------------------------------

def embed_batch(
    texts: list[str],
    ollama_url: str,
    model: str,
    session: requests.Session,
    sleeper=time.sleep,
) -> list[list[float]]:
    """1 バッチ分のテキストを Ollama `/api/embed` でベクトル化する。

    HTTP エラー / 接続エラー / 想定外レスポンス形状は最大 ``_MAX_EXTRA_RETRIES``
    回まで再試行し、それでも失敗したら ``EmbeddingBatchError`` を送出する
    (呼び出し元はこれを捕捉して非 0 終了し、出力は一切書き込まない)。
    """
    url = f"{ollama_url.rstrip('/')}/api/embed"
    last_err: Exception | None = None
    attempts = _MAX_EXTRA_RETRIES + 1
    for attempt in range(1, attempts + 1):
        try:
            resp = session.post(url, json={"model": model, "input": texts}, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            payload = resp.json()
            embeddings = payload.get("embeddings") if isinstance(payload, dict) else None
            if not isinstance(embeddings, list) or len(embeddings) != len(texts):
                raise EmbeddingBatchError(
                    f"unexpected /api/embed response shape (expected {len(texts)} vectors)"
                )
            return embeddings
        except (requests.RequestException, EmbeddingBatchError, ValueError) as e:
            last_err = e
            if attempt < attempts:
                logger.warning(
                    "embed batch failed (attempt %d/%d): %s; retrying", attempt, attempts, e
                )
                sleeper(_RETRY_SLEEP_SECONDS)
            else:
                logger.error("embed batch failed after %d attempt(s): %s", attempts, e)
    raise EmbeddingBatchError(str(last_err)) from last_err


def embed_batch_ruri(
    texts: list[str],
    ruri_url: str,
    session: requests.Session,
    sleeper=time.sleep,
) -> list[list[float]]:
    """1 バッチ分のテキストを amazon-home-ops の Ruri v3 API (`/embed`) でベクトル化する。

    #2995 案1 の K8 LLM ワーカー経路 (``http://ruri:8000``、compose 内部ネットワーク)。
    リトライ挙動は ``embed_batch`` (Ollama 版) と同一。レスポンス形状のみ異なる
    (``embeddings`` ではなく ``vectors``)。文書側の "検索文書: " プレフィックスは
    ruri app.py 側が ``kind="document"`` から自動付与するため、ここでは生テキスト
    をそのまま送る。
    """
    url = f"{ruri_url.rstrip('/')}/embed"
    last_err: Exception | None = None
    attempts = _MAX_EXTRA_RETRIES + 1
    for attempt in range(1, attempts + 1):
        try:
            resp = session.post(
                url, json={"texts": texts, "kind": "document"}, timeout=REQUEST_TIMEOUT
            )
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
                logger.warning(
                    "ruri embed batch failed (attempt %d/%d): %s; retrying", attempt, attempts, e
                )
                sleeper(_RETRY_SLEEP_SECONDS)
            else:
                logger.error("ruri embed batch failed after %d attempt(s): %s", attempts, e)
    raise EmbeddingBatchError(str(last_err)) from last_err


# --------------------------------------------------------------------------
# 埋め込みキャッシュ (#6602 N3a)
# --------------------------------------------------------------------------
#
# なぜ要るか: このレーンも audit_uniqueness も毎 run コーパス全件を embed して
# いた。実測 (2026-09-06) では 24-uniqueness-audit が 2,359 記事で 38.5 分、
# ジョブ 39.0 分のほぼ全部が埋め込み。一方 data/articles の変更は 7 日で 6.8%
# (日次なら 1.7%) しかない。**93〜98% が同じテキストの再計算**だった。
#
# timeout の期限もある。61 記事/分・timeout 60 分なので容量は約 3,660 記事で、
# 新規記事は 30 日で 812 本。放置すると 2 ヶ月弱で頭を打つ。このレーンは
# 2026-07-19 に同じ理由 (全件埋め込みが時間に収まらない) で timeout し、
# #3203 Phase 4 の観察がサイレント停止した実績がある。
#
# 設計:
#   - キーは **embed に渡すテキストそのもの** の hash。記事 JSON 全体では
#     ない。無関係な項目が変わっただけでキャッシュを捨てないため
#   - キーには **サーバが名乗る実モデル ID** を混ぜる (resolve_embed_model)。
#     ruri backend の `--model` はラベルでしかなく、実モデルはコンテナの
#     EMBED_MODEL で決まる。呼び出し側のラベルを信じると、モデルを差し替えた
#     ときに**別モデルのベクタを黙って配る**
#   - **キャッシュは壊れても落とさない。** 読めなければ warning を出して
#     コールドで回る。速いだけの仕組みのために本番レーンを止めない
#   - 保存は tmp + os.replace の原子的置換。途中で殺されても壊れた JSON を
#     残さない
#   - 保存時に**今回参照されなかったキーを捨てる**。捨てないと削除済み記事の
#     ベクタが永久に溜まる

CACHE_FORMAT = 1


def resolve_embed_model(
    backend: str, *, model: str, ruri_url: str, session: requests.Session,
) -> str:
    """キャッシュキーに混ぜる「実際に embed するモデル」の ID を決める。

    ollama backend は ``model`` がそのままリクエストに乗るので、それが実体。
    ruri backend は ``model`` が出力ラベル用でしかないため、``/health`` が
    名乗る ``embed_model`` を採る。取れなければ ``None`` を返す
    (**キャッシュを使わない**。誤ったキーで別モデルのベクタを配るより、
    遅くても正しい方を採る)。
    """
    if backend != "ruri":
        return model
    try:
        resp = session.get(f"{ruri_url.rstrip('/')}/health", timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()
        name = payload.get("embed_model") if isinstance(payload, dict) else None
        if isinstance(name, str) and name:
            return name
        logger.warning("ruri /health に embed_model が無い — 埋め込みキャッシュを使いません")
    except (requests.RequestException, ValueError) as e:
        logger.warning("ruri /health の取得に失敗 (%s) — 埋め込みキャッシュを使いません", e)
    return ""


def cache_key(model_id: str, text: str) -> str:
    return hashlib.sha256(
        "{}\x00{}".format(model_id, text).encode("utf-8")
    ).hexdigest()


class EmbeddingCache:
    """テキスト hash → ベクタ の永続キャッシュ。

    ファイルは 1 本の JSON。**リポジトリには置かない** (2,359 記事 × 768 次元で
    数十 MB になり、#5047 のリポジトリ肥大を悪化させる)。K8 の named volume の
    ようなランナーローカルの永続領域に置く。消えても遅い run が 1 回走るだけ
    なので、可用性は要らない。
    """

    def __init__(self, path: pathlib.Path | None, model_id: str) -> None:
        self.path = path
        self.model_id = model_id
        self.vectors: dict[str, list[float]] = {}
        self.seen: set[str] = set()
        self.hits = 0
        self.misses = 0

    @property
    def enabled(self) -> bool:
        return self.path is not None and bool(self.model_id)

    def load(self) -> None:
        if not self.enabled:
            return
        assert self.path is not None
        if not self.path.exists():
            logger.info("埋め込みキャッシュ %s は未作成 — コールドで開始します", self.path)
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("cache root is not an object")
            if data.get("format") != CACHE_FORMAT:
                logger.warning(
                    "埋め込みキャッシュの format が違う (%r != %r) — 捨てて作り直します",
                    data.get("format"), CACHE_FORMAT,
                )
                return
            # モデルが変わったら全部無効。混ぜて使うと似ていないもの同士の
            # コサイン類似度を比べることになる
            if data.get("model") != self.model_id:
                logger.info(
                    "埋め込みキャッシュのモデルが変わった (%r -> %r) — 捨てて作り直します",
                    data.get("model"), self.model_id,
                )
                return
            vectors = data.get("vectors")
            if not isinstance(vectors, dict):
                raise ValueError("cache 'vectors' is not an object")
            self.vectors = {
                k: v for k, v in vectors.items()
                if isinstance(k, str) and isinstance(v, list) and v
            }
            logger.info("埋め込みキャッシュを読み込み: %d 件 (%s)", len(self.vectors), self.path)
        except (OSError, ValueError) as e:
            # 壊れたキャッシュでレーンを止めない
            logger.warning("埋め込みキャッシュの読み込みに失敗 (%s) — コールドで続行します", e)
            self.vectors = {}

    def get(self, text: str) -> list[float] | None:
        if not self.enabled:
            return None
        k = cache_key(self.model_id, text)
        v = self.vectors.get(k)
        if v is None:
            self.misses += 1
            return None
        self.seen.add(k)
        self.hits += 1
        return v

    def put(self, text: str, vector: list[float]) -> None:
        if not self.enabled:
            return
        k = cache_key(self.model_id, text)
        self.vectors[k] = vector
        self.seen.add(k)

    def save(self, *, prune: bool = True) -> None:
        """原子的に書き出す。

        ``prune=True`` なら今回参照されなかったキーを捨てる (削除済み記事の
        ベクタが永久に溜まらないように)。**部分実行では必ず ``prune=False``**。
        ``--limit 50`` のスモーク 1 回で 2,300 件の warm キャッシュが消え、
        次の全件 run が丸ごと再計算になる — キャッシュを入れた意味が飛ぶ。
        """
        if not self.enabled:
            return
        assert self.path is not None
        kept = ({k: v for k, v in self.vectors.items() if k in self.seen}
                if prune else dict(self.vectors))
        dropped = len(self.vectors) - len(kept)
        payload = {
            "format": CACHE_FORMAT,
            "model": self.model_id,
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "vectors": kept,
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, self.path)
            logger.info(
                "埋め込みキャッシュを保存: %d 件 (未参照 %d 件を破棄) -> %s",
                len(kept), dropped, self.path,
            )
        except OSError as e:
            # 保存できなくても結果は正しい。次の run が遅くなるだけ
            logger.warning("埋め込みキャッシュの保存に失敗 (%s) — 続行します", e)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def stats(self) -> dict[str, int]:
        total = self.hits + self.misses
        return {
            "hits": self.hits,
            "misses": self.misses,
            "total": total,
            "hit_rate_pct": round(100.0 * self.hits / total) if total else 0,
        }


def embed_texts(
    texts: list[str],
    *,
    backend: str,
    batch_size: int,
    ollama_url: str,
    model: str,
    ruri_url: str,
    session: requests.Session,
    sleeper=time.sleep,
    cache: EmbeddingCache | None = None,
    progress_every: int | None = None,
) -> list[list[float]]:
    """テキスト列をベクトル化する。キャッシュがあればミス分だけ embed する。

    compute_semantic_related と audit_uniqueness が同じループを別々に持って
    いたので、ここに一本化した (キャッシュを 2 箇所に書かないため)。
    """
    n = len(texts)
    out: list[list[float]] = [[] for _ in range(n)]
    todo: list[int] = []
    for i, t in enumerate(texts):
        hit = cache.get(t) if cache is not None else None
        if hit is None:
            todo.append(i)
        else:
            out[i] = hit

    if cache is not None and cache.enabled:
        s = cache.stats()
        logger.info(
            "埋め込みキャッシュ: hit %d / %d (%d%%) — embed するのは %d 件",
            s["hits"], s["total"], s["hit_rate_pct"], len(todo),
        )

    n_batches = (len(todo) + batch_size - 1) // batch_size
    for bi in range(n_batches):
        idxs = todo[bi * batch_size: (bi + 1) * batch_size]
        batch_texts = [texts[i] for i in idxs]
        if backend == "ruri":
            vectors = embed_batch_ruri(batch_texts, ruri_url, session, sleeper)
        else:
            vectors = embed_batch(batch_texts, ollama_url, model, session, sleeper)
        for i, vec in zip(idxs, vectors):
            out[i] = vec
            if cache is not None:
                cache.put(texts[i], vec)
        if progress_every and (bi + 1) % progress_every == 0:
            logger.info("embedding batches progress: %d/%d", bi + 1, n_batches)
    if n_batches and progress_every:
        logger.info("embedding batches progress: %d/%d (done)", n_batches, n_batches)
    return out


def open_embedding_cache(
    cache_path: str | os.PathLike[str] | None,
    *,
    backend: str,
    model: str,
    ruri_url: str,
    session: requests.Session,
) -> EmbeddingCache | None:
    """``--embed-cache`` の値からキャッシュを用意する (未指定なら None)。"""
    if not cache_path:
        return None
    model_id = resolve_embed_model(backend, model=model, ruri_url=ruri_url, session=session)
    cache = EmbeddingCache(pathlib.Path(cache_path), model_id)
    if not cache.enabled:
        return None
    cache.load()
    return cache


# --------------------------------------------------------------------------
# 類似度計算 (numpy があればベクトル化、無ければ純 Python フォールバック)
# --------------------------------------------------------------------------

def _cosine_similarity_pure(vectors: list[list[float]]) -> list[list[float]]:
    """純 Python によるコサイン類似度行列 (numpy 不在時のフォールバック)。

    各ベクトルを一度だけ正規化してから内積を取る。単体テスト可能なように
    ``compute_similarity_matrix`` から分離してある。
    """
    normed: list[list[float]] = []
    for v in vectors:
        norm = math.sqrt(sum(x * x for x in v))
        if norm == 0:
            normed.append([0.0] * len(v))
        else:
            normed.append([x / norm for x in v])

    n = len(normed)
    sim = [[0.0] * n for _ in range(n)]
    for i in range(n):
        vi = normed[i]
        for j in range(i, n):
            dot = sum(a * b for a, b in zip(vi, normed[j]))
            sim[i][j] = dot
            sim[j][i] = dot
    return sim


def _cosine_similarity_numpy(vectors: list[list[float]]):
    import numpy as np

    arr = np.asarray(vectors, dtype=float)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    normed = arr / norms
    return (normed @ normed.T).tolist()


def compute_similarity_matrix(vectors: list[list[float]]) -> list[list[float]]:
    """全ベクトルペアのコサイン類似度行列を計算する。

    numpy が import できればベクトル化した計算 (行正規化 + M @ M.T) を使い、
    import できなければ ``_cosine_similarity_pure`` にフォールバックする。
    """
    if not vectors:
        return []
    try:
        return _cosine_similarity_numpy(vectors)
    except ImportError:
        return _cosine_similarity_pure(vectors)


# --------------------------------------------------------------------------
# 近傍抽出
# --------------------------------------------------------------------------

def compute_neighbors(
    asins: list[str],
    similarity: list[list[float]],
    top_k: int,
    min_score: float,
) -> dict[str, list[dict[str, Any]]]:
    """記事ごとの top-k 近傍を計算する。

    自分自身を除外し、``min_score`` 未満は候補から外したうえで
    (score 降順, asin 昇順) で決定的に並べ替え、上位 ``top_k`` 件を残す。
    スコアの丸めは並べ替え・閾値判定の *後* に行う。近傍が 0 件の記事は
    出力から省く (ファイルサイズ削減のため)。
    """
    n = len(asins)
    result: dict[str, list[dict[str, Any]]] = {}
    for i in range(n):
        candidates: list[tuple[str, float]] = []
        for j in range(n):
            if j == i:
                continue
            score = similarity[i][j]
            if score >= min_score:
                candidates.append((asins[j], score))
        candidates.sort(key=lambda t: (-t[1], t[0]))
        top = candidates[:top_k]
        if not top:
            continue
        result[asins[i].lower()] = [
            {"asin": asin.lower(), "score": round(score, 3)} for asin, score in top
        ]
    return result


def _write_output(out_path: pathlib.Path, neighbors: dict[str, Any], model: str, article_count: int) -> None:
    payload: dict[str, Any] = {
        "_meta": {
            "generated_at": _now_iso(),
            "model": model,
            "articles": article_count,
        }
    }
    payload.update(neighbors)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")


# --------------------------------------------------------------------------
# 実行本体
# --------------------------------------------------------------------------

def run(
    articles_dir: pathlib.Path,
    out_path: pathlib.Path,
    top_k: int = DEFAULT_TOP_K,
    min_score: float = DEFAULT_MIN_SCORE,
    batch_size: int = DEFAULT_BATCH_SIZE,
    limit: int = 0,
    dry_run: bool = False,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    model: str = DEFAULT_MODEL,
    backend: str = DEFAULT_BACKEND,
    ruri_url: str = DEFAULT_RURI_URL,
    session: requests.Session | None = None,
    sleeper=time.sleep,
    embed_cache: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """全体を実行し、件数サマリを dict で返す (テスト・main 双方から呼べるように分離)。

    ``backend="ollama"`` (既定) は ``ollama_url``/``model`` で Ollama の
    `/api/embed` を叩く。``backend="ruri"`` は ``ruri_url`` で amazon-home-ops の
    Ruri v3 API (`/embed`) を叩く (#2995 案1 K8 LLM ワーカー経路)。``model`` は
    ruri 経路では出力 `_meta.model` のラベルにのみ使う (実モデルはコンテナ側で固定)。

    埋め込みバッチが最終的に失敗した場合は ``EmbeddingBatchError`` を送出し、
    出力ファイルへの書き込みは一切行わない (呼び出し元は非 0 終了させること)。
    """
    if backend not in ("ollama", "ruri"):
        raise ValueError(f"unknown backend: {backend!r} (expected 'ollama' or 'ruri')")

    items = load_article_index(articles_dir, limit)
    logger.info("articles after dedupe: %d (limit=%d)", len(items), limit)

    if not items:
        logger.info("no articles to process; nothing to write")
        return {"articles": 0, "written": False, "entries": 0}

    asins = [a for a, _ in items]
    texts = [t for _, t in items]

    session = session or requests.Session()
    cache = open_embedding_cache(
        embed_cache, backend=backend, model=model, ruri_url=ruri_url, session=session,
    )
    embeddings = embed_texts(
        texts, backend=backend, batch_size=batch_size, ollama_url=ollama_url,
        model=model, ruri_url=ruri_url, session=session, sleeper=sleeper,
        cache=cache, progress_every=_BATCH_LOG_EVERY,
    )
    if cache is not None:
        # limit 付きは部分実行。prune すると warm キャッシュを削ってしまう
        cache.save(prune=not limit)

    if dry_run:
        logger.info("[dry-run] computed embeddings for %d articles; not writing output", len(asins))
        return {"articles": len(asins), "written": False, "entries": 0}

    similarity = compute_similarity_matrix(embeddings)
    neighbors = compute_neighbors(asins, similarity, top_k, min_score)
    _write_output(out_path, neighbors, model, len(asins))
    logger.info("wrote %s: %d entries (of %d articles)", out_path, len(neighbors), len(asins))
    return {"articles": len(asins), "written": True, "entries": len(neighbors)}


def main() -> None:
    ap = argparse.ArgumentParser(
        description="記事間の意味的近傍を Ollama embeddings で計算する (#2995 案1)"
    )
    ap.add_argument("--articles-dir", default=DEFAULT_ARTICLES_DIR, help="記事 JSON のディレクトリ")
    ap.add_argument("--out", default=DEFAULT_OUT, help="出力先 JSON パス")
    ap.add_argument("--top-k", type=int, default=DEFAULT_TOP_K, help="記事あたりの最大近傍数")
    ap.add_argument("--min-score", type=float, default=DEFAULT_MIN_SCORE, help="近傍として採用する最小コサイン類似度")
    ap.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="embed API へのバッチサイズ")
    ap.add_argument("--limit", type=int, default=0, help="処理する記事数の上限 (0=全件、スモークテスト用)")
    ap.add_argument("--dry-run", action="store_true", help="計算のみ行い、出力ファイルへの書き込みは行わない")
    ap.add_argument(
        "--backend",
        choices=("ollama", "ruri"),
        default=os.environ.get("EMBED_BACKEND", DEFAULT_BACKEND),
        help="埋め込みバックエンド: ollama (既定) または ruri (#2995 案1 K8 LLM ワーカー)",
    )
    ap.add_argument(
        "--ollama-url",
        default=os.environ.get("OLLAMA_URL", DEFAULT_OLLAMA_URL),
        help="Ollama サーバーの URL (--backend ollama 時のみ使用)",
    )
    ap.add_argument(
        "--ruri-url",
        default=os.environ.get("RURI_URL", DEFAULT_RURI_URL),
        help="Ruri v3 API の URL (--backend ruri 時のみ使用)",
    )
    ap.add_argument(
        "--model",
        default=os.environ.get("EMBED_MODEL"),
        help="embeddings に使うモデル名 (ollama: リクエストに使用 / ruri: _meta ラベルのみ、未指定ならバックエンド別既定値)",
    )
    ap.add_argument(
        "--embed-cache",
        default=os.environ.get("EMBED_CACHE"),
        help=("埋め込みキャッシュの JSON パス (未指定ならキャッシュしない)。"
              "**リポジトリ内を指さないこと** — 数十 MB になる。ランナーローカルの"
              "永続領域 (K8 の named volume 等) を指す"),
    )
    args = ap.parse_args()

    model = args.model
    if not model:
        model = DEFAULT_MODEL_RURI if args.backend == "ruri" else DEFAULT_MODEL

    try:
        run(
            articles_dir=pathlib.Path(args.articles_dir),
            out_path=pathlib.Path(args.out),
            top_k=args.top_k,
            min_score=args.min_score,
            batch_size=args.batch_size,
            limit=args.limit,
            dry_run=args.dry_run,
            ollama_url=args.ollama_url,
            model=model,
            backend=args.backend,
            ruri_url=args.ruri_url,
            embed_cache=args.embed_cache,
        )
    except EmbeddingBatchError as e:
        logger.error("embedding computation failed: %s; aborting without writing output", e)
        raise SystemExit(1) from e


if __name__ == "__main__":
    main()
