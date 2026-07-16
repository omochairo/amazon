"""_seo_sidecar.py

``data/articles/<slug>.seo.json`` sidecar への read-modify-write (キー単位更新) を
集約する共通ヘルパ。Issue #3332 N1 (``generate_faq_seo.py``) と N3 (後続 PR、
``generate_internal_links.py``) の両方がこのモジュール経由で sidecar を書く。

sidecar のキーは ``scripts/build_post.py`` の ``SEO_KEYS`` (title_variants /
meta_description_optimized / h1_recommendation / h2_recommendations /
faq_extended / breadcrumbs / jsonld / internal_link_suggestions) が対応する
消費側であり、本モジュールはその生成側が共有する書き込みロジックのみを持つ。

Issue: https://github.com/omochairo/amazon/issues/3332 (N1 設計コメント)
"""
from __future__ import annotations

import json
import pathlib
from typing import Any


def load_sidecar(path: pathlib.Path) -> dict[str, Any]:
    """sidecar JSON を読み込む。不在または壊れた JSON なら例外を投げず ``{}`` を返す。"""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def update_sidecar(path: pathlib.Path, updates: dict[str, Any]) -> None:
    """既存の sidecar を読み、``updates`` のキーだけを上書きして書き戻す。

    - ``updates`` に無いキーは既存値を保持する。
    - 値が ``None`` のキーは既存 dict から削除する。
    - 結果が空 dict になった場合はファイルを書かず、既存ファイルがあれば削除する。
    - 書き出しは ``ensure_ascii=False`` (日本語を escape しない) + 末尾改行。
    """
    current = load_sidecar(path)
    for key, value in updates.items():
        if value is None:
            current.pop(key, None)
        else:
            current[key] = value

    if not current:
        if path.exists():
            path.unlink()
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sidecar_path(articles_dir: pathlib.Path, stem: str) -> pathlib.Path:
    """記事 stem から sidecar パス (``<articles_dir>/<stem>.seo.json``) を作る。"""
    return articles_dir / f"{stem}.seo.json"
