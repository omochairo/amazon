"""official_howto_format.py

#7959 (#7955 C 改訂版) — 「📘 公式の取扱説明書・遊び方」ブロックの
決定的レンダリング層。

``where_to_buy_format.py`` と同じ作り: official_howto.py (読み込み・判定) の
上に、テンプレートへ渡す辞書を **決定的に** 組み立てる。LLM (Jules) は通さない。

呼び出し元: build_post.py が ``official_howto.load()`` の結果をここへ渡し、
戻り値を ``data["official_howto_block"]`` としてテンプレートへ渡す。

なぜ Jinja 側で組み立てないか:
  publisher/kind のラベル変換・日付整形・HTML エスケープを 1 か所に集め、
  テンプレは受け取った文字列をそのまま置くだけにするため (where_to_buy_format
  と同じ分業)。official_name / steps の text・section は AGY の書き起こしを
  経由した外部由来の文字列なので、テンプレの ``autoescape=False`` を前提に
  ここで html.escape する。

pure formatting only — 外部 API 呼び出しなし・data/ 書き込みなし。
"""
from __future__ import annotations

import html
from typing import Any, Optional

import official_howto

# #7955 改訂 (2026-09-21): publisher コード -> 表示ラベル。
PUBLISHER_LABELS: dict[str, str] = {
    "bandai": "バンダイ",
    "lego": "レゴ",
    "tamagotchi": "バンダイ",  # あそびかたは tamagotchi-official.com (バンダイ運営)
}

# kind コード -> 表示ラベル。
KIND_LABELS: dict[str, str] = {
    "manual_pdf": "取扱説明書（PDF）",
    "building_instructions": "組み立て説明書",
    "howto_page": "あそびかた",
}

_DEFAULT_PUBLISHER_LABEL = "メーカー"
_DEFAULT_KIND_LABEL = "取扱説明書"


def _publisher_label(publisher: Any) -> str:
    if isinstance(publisher, str) and publisher in PUBLISHER_LABELS:
        return PUBLISHER_LABELS[publisher]
    return _DEFAULT_PUBLISHER_LABEL


def _kind_label(kind: Any) -> str:
    if isinstance(kind, str) and kind in KIND_LABELS:
        return KIND_LABELS[kind]
    return _DEFAULT_KIND_LABEL


def _fetched_date(fetched_at: Any) -> Optional[str]:
    """``fetched_at`` (ISO8601) から ``YYYY-MM-DD`` を取る。JST 変換はしない
    (#7959: 日付だけで足りるという設計判断。時刻まで出す意味が無い)。"""
    if not isinstance(fetched_at, str) or len(fetched_at) < 10:
        return None
    head = fetched_at[:10]
    if len(head) == 10 and head[4] == "-" and head[7] == "-":
        return head
    return None


def _clean_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    v = value.strip()
    return v if v else None


def _build_steps(raw_steps: Any) -> list[dict[str, Optional[str]]]:
    """``steps`` (#7960 が書く ``[{"text","section"}, ...]``) をエスケープ済みで
    整形する。text が無い/空の要素は落とす (フェイルセーフ)。"""
    steps: list[dict[str, Optional[str]]] = []
    if not isinstance(raw_steps, list):
        return steps
    for step in raw_steps:
        if not isinstance(step, dict):
            continue
        text = _clean_text(step.get("text"))
        if text is None:
            continue
        section = _clean_text(step.get("section"))
        steps.append({
            "text": html.escape(text),
            "section": html.escape(section) if section is not None else None,
        })
    return steps


def build_official_howto_block(obj: dict | None) -> Optional[dict[str, Any]]:
    """``official_howto.json`` から決定的にテンプレート用ブロックを組み立てる。

    ``official_howto.has_official_url(obj)`` が False (url が無い/レコードが
    無い/status=not_found 等) のときは None を返す — 呼び出し元 (build_post)
    はこのとき ``data["official_howto_block"] = None`` のままにし、テンプレは
    何も描画しない (=既存ページの出力はバイト単位で変わらない)。
    """
    if not official_howto.has_official_url(obj):
        return None

    publisher = _publisher_label(obj.get("publisher"))
    kind = _kind_label(obj.get("kind"))
    url = html.escape(obj["url"], quote=True)
    official_name = _clean_text(obj.get("official_name"))
    official_name = html.escape(official_name) if official_name is not None else None
    date_label = _fetched_date(obj.get("fetched_at"))

    steps = _build_steps(obj.get("steps")) if official_howto.has_reviewed_steps(obj) else []

    if steps:
        intro = f"{publisher}公式の{kind}から、はじめ方をまとめました。"
        closing_link_text = f"▶ {kind}の全文を{publisher}の公式サイトで見る"
        return {
            "has_steps": True,
            "intro": intro,
            "official_name": official_name,
            "steps": steps,
            "url": url,
            "closing_link_text": closing_link_text,
            "date_label": date_label,
        }

    # steps 無し (or reviewed でない) — リンクのみ。
    intro = f"{publisher}が公開している{kind}"
    intro += f"「{official_name}」" if official_name else ""
    intro += "です。"
    return {
        "has_steps": False,
        "intro": intro,
        "official_name": official_name,
        "steps": [],
        "url": url,
        "link_text": "公式サイトで見る →",
        "date_note": f"（{date_label} 時点で公開を確認）" if date_label else None,
        "date_label": date_label,
    }
