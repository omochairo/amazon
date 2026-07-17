"""画像 URL から実寸 (width, height) を取得するユーティリティ (#3314)。

Amazon 商品画像の `_SL500_` は長辺を 500px に収める指定で正方形とは限らない
(縦長画像は幅が細くなる)。hero 画像の `<img width height>` に固定値
500x500 を入れると CLS を悪化させるため、実寸をパイプラインに通す必要がある。

JPEG/PNG/WebP はファイル先頭のヘッダにサイズ情報を持つため、画像を全量
ダウンロードせず、PIL の incremental parser にストリーミングで feed して
ヘッダが揃った時点で打ち切る。Amazon 画像は数十〜数百KBあるため、全量
取得を避けることでフェッチパイプラインの負荷を抑える。
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("image_dimensions")

DEFAULT_TIMEOUT = 5.0
DEFAULT_MAX_BYTES = 300_000
CHUNK_SIZE = 4096


def fetch_image_dimensions(
    url: str,
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> Optional[tuple[int, int]]:
    """``url`` の画像実寸を ``(width, height)`` で返す。取得できなければ None。

    ネットワーク障害・タイムアウト・不正な画像データはすべて None にフォール
    バックする (fail-soft)。呼び出し側はこれを「実寸なし」として扱い、
    width/height 属性を出さない既存挙動を維持すること。
    """
    if not url:
        return None
    try:
        import requests
        from PIL import ImageFile
    except ImportError as e:
        logger.warning(f"image_dimensions: missing dependency: {e}")
        return None

    parser = ImageFile.Parser()
    try:
        with requests.get(url, stream=True, timeout=timeout) as resp:
            resp.raise_for_status()
            read = 0
            for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                if not chunk:
                    continue
                parser.feed(chunk)
                read += len(chunk)
                if parser.image is not None:
                    return parser.image.size
                if read >= max_bytes:
                    break
    except Exception as e:
        logger.warning(f"image_dimensions: failed to fetch {url}: {e}")
        return None
    return None
