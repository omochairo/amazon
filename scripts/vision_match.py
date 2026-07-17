"""楽天↔Amazon 商品画像の埋め込み照合モジュール (Issue #3332 N5-V1)。

## 目的

Issue #2818 対策4c (タイトル正規化 fuzzy マッチ) は誤マッチリスク (false positive =
誤アフィリエイトリンク) を理由に保留されていた。本モジュールはその解禁ゲートとして、
楽天商品画像 と Amazon 候補商品画像の埋め込みベクトルのコサイン類似度を算出し、
閾値以上のみ採用する仕組みを提供する (#3332 N5 設計コメント 2026-07-17)。

## モデル候補 (K8 未デプロイ・オーナー判断待ち)

生成不要な照合タスクのため、生成型 VLM ではなく **CLIP/SigLIP 系の画像埋め込みモデル**
を想定する (CPU 1画像 <1秒・決定的な閾値判定)。具体候補 (実タグ・ライセンスは
デプロイ判断時に別途確認すること — 「タグ推測禁止」の教訓):
  - ``openai/clip-vit-base-patch32`` (sentence-transformers の ``clip-ViT-B-32`` 経由で
    テキスト/画像を同一空間に埋め込める。実装コストが最小)
  - ``google/siglig-base-patch16-224`` 系 (SigLIP。CLIP より zero-shot 精度が高い傾向)
  いずれも Apache-2.0 / MIT 系ライセンスの想定だが、K8 デプロイ時に HF の実タグと
  ライセンスを確認してから確定すること。

## HTTP 抽象 (実サービス未デプロイ)

amazon-home-ops の Ruri v3 FastAPI (``/embed``, ``ruri/app.py``) と同型の
``/embed_image`` エンドポイントを想定する:

    POST /embed_image {"image_urls": ["https://...", "https://..."]}
    -> {"vectors": [[...], [...]]}   # 画像ごとに 1 ベクトル (取得失敗時は null 許容)

実サービスの URL は環境変数 ``VISION_EMBED_URL`` / CLI 引数で与える設計とし、
本モジュール自体は K8 への実デプロイを一切前提としない (#3332 スコープ外)。
テストでは ``ImageEmbeddingClient`` の代わりに ``embed_images(urls) -> list`` を実装した
任意のモックオブジェクトを注入できる (DI)。

## 閾値の較正方針

``DEFAULT_MIN_SCORE`` は較正前の保守的な既定値。#3332 N5 設計・#3333 の前例
(「shadow 実測 → 分布から閾値較正」) に倣い、まず ``vision_gate_mode="shadow"`` で
image_sim を manifest に記録するだけの運用を行い、実測分布を見てから
``vision_gate_mode="enforce"`` + 較正済み閾値に切り替える。
"""
from __future__ import annotations

import logging
import math
import time
from typing import Optional, Sequence

import requests

logger = logging.getLogger("vision_match")

# 実サービス URL は未確定 (K8 デプロイはオーナー判断待ち)。空文字は「vision 無効」を
# 意味し、呼び出し側 (resolve_ranking_asins.py) は client=None のまま
# vision_gate_mode に関わらず image_sim=None (未評価) として shadow 記録する。
DEFAULT_VISION_URL = ""

# 較正前の保守的既定値。CLIP/SigLIP 系のコサイン類似度は「同一商品の別アングル写真」
# でも 0.9 を下回ることがある一方、「同カテゴリ別商品」でも 0.7〜0.8 台に乗ることが
# あるため、shadow 実測なしに緩い値を既定にすると誤マッチを通しかねない。過検出
# (=誤って蹴る) 側に倒し、実測分布から緩和する運針とする。
DEFAULT_MIN_SCORE = 0.90

REQUEST_TIMEOUT = 20
_MAX_EXTRA_RETRIES = 2
_RETRY_SLEEP_SECONDS = 2.0


class VisionEmbeddingError(Exception):
    """画像埋め込み取得に失敗した。呼び出し元は image_sim=None として扱う (fail-closed)。"""


class ImageEmbeddingClient:
    """画像埋め込みサービスへの HTTP 抽象クライアント (注入可能)。

    ``compute_semantic_related.embed_batch_ruri`` と同じリトライ方針
    (HTTP エラー / 想定外レスポンス形状を ``_MAX_EXTRA_RETRIES`` 回まで再試行)。
    実サービス未デプロイのため、``base_url`` は呼び出し側が明示的に与えない限り
    使われない (resolve_ranking_asins.py は URL 未設定なら本クラスを生成しない)。
    """

    def __init__(
        self,
        base_url: str,
        session: Optional[requests.Session] = None,
        timeout: float = REQUEST_TIMEOUT,
        sleeper=time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.timeout = timeout
        self.sleeper = sleeper

    def embed_images(self, image_urls: Sequence[str]) -> list:
        """画像 URL 群を埋め込みベクトルへ変換する。

        レスポンス形状が想定と異なる/HTTP エラーが続く場合は ``VisionEmbeddingError``
        を送出する。呼び出し元 (``image_similarity``) はこれを捕捉して None を返す。
        """
        if not image_urls:
            return []
        url = f"{self.base_url}/embed_image"
        last_err: Optional[Exception] = None
        attempts = _MAX_EXTRA_RETRIES + 1
        for attempt in range(1, attempts + 1):
            try:
                resp = self.session.post(
                    url, json={"image_urls": list(image_urls)}, timeout=self.timeout
                )
                resp.raise_for_status()
                payload = resp.json()
                vectors = payload.get("vectors") if isinstance(payload, dict) else None
                if not isinstance(vectors, list) or len(vectors) != len(image_urls):
                    raise VisionEmbeddingError(
                        f"unexpected /embed_image response shape "
                        f"(expected {len(image_urls)} vectors)"
                    )
                return vectors
            except (requests.RequestException, VisionEmbeddingError, ValueError) as e:
                last_err = e
                if attempt < attempts:
                    logger.warning(
                        "vision embed batch failed (attempt %d/%d): %s; retrying",
                        attempt, attempts, e,
                    )
                    self.sleeper(_RETRY_SLEEP_SECONDS)
                else:
                    logger.error(
                        "vision embed batch failed after %d attempt(s): %s", attempts, e
                    )
        raise VisionEmbeddingError(str(last_err)) from last_err


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """2 ベクトルのコサイン類似度。長さ不一致・ゼロベクトルは 0.0 を返す (0除算回避)。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def image_similarity(client, rakuten_image_url: str, amazon_image_url: str) -> Optional[float]:
    """楽天商品画像 vs Amazon 候補画像のコサイン類似度を返す。

    どちらかの URL が空、または埋め込み取得に失敗した場合は ``None`` (未評価) を返す。
    呼び出し元は None を shadow 記録し、``vision_gate_mode="enforce"`` では
    fail-closed (不採用) として扱う (``passes_vision_gate`` 参照)。
    """
    if not rakuten_image_url or not amazon_image_url:
        return None
    try:
        vectors = client.embed_images([rakuten_image_url, amazon_image_url])
    except Exception as e:  # noqa: BLE001 — 注入クライアントの例外型を限定しない
        logger.warning(
            "image_similarity: embedding failed (%s vs %s): %s",
            rakuten_image_url, amazon_image_url, e,
        )
        return None
    if not isinstance(vectors, list) or len(vectors) != 2:
        return None
    v0, v1 = vectors[0], vectors[1]
    if not v0 or not v1:
        return None
    return cosine_similarity(v0, v1)


def passes_vision_gate(score: Optional[float], min_score: float) -> bool:
    """score が None (未評価/失敗) の場合は fail-closed で False を返す。"""
    if score is None:
        return False
    return score >= min_score
