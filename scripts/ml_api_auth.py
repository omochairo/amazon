"""amazon-home-ops の ruri / vision API に付ける認証ヘッダ (amazon-home-ops#158)。

両 API は ``API_TOKEN`` を設定すると ``/health`` 以外で ``X-API-Key`` の一致を要求する。
K8 の runner コンテナには同じ値が ``RURI_API_TOKEN`` / ``VISION_API_TOKEN`` として
渡っている (amazon-home-ops docker/docker-compose.k8.yml)。未設定なら空の dict を返し、
従来どおりヘッダ無しで送る (サービス側も未設定なら無認証で受ける)。
"""
from __future__ import annotations

import os


def _headers(env_name: str) -> dict[str, str]:
    token = os.environ.get(env_name, "").strip()
    return {"X-API-Key": token} if token else {}


def ruri_headers() -> dict[str, str]:
    """Ruri v3 API (``/embed`` ``/rerank``) 用。"""
    return _headers("RURI_API_TOKEN")


def vision_headers() -> dict[str, str]:
    """画像埋め込み API (``/embed_image``) 用。"""
    return _headers("VISION_API_TOKEN")
