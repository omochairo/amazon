"""ASIN 単位の素材読み込みと、gemma プロンプト用テキストへの組み立て (pure functions)。

読むファイルは data/raw/per_asin/<ASIN>/ 配下の amazon.json / competitors.json /
experience.json / youtube.json / news.json のみ。本番の fetch レーンが書いた
ものをそのまま読み取るだけで、書き込みは一切行わない。
"""
from __future__ import annotations

import json
import pathlib
from typing import Any

DEFAULT_RAW_DIR = "data/raw/per_asin"

# 各セクションの目安文字数。material_text 全体を MAX_MATERIAL_TEXT_LEN 以内に
# 収めるための配分 (num_ctx 超過による無言切り詰めを避ける、#4528 実測)。
MAX_MATERIAL_TEXT_LEN = 4000
MAX_FEATURES = 6
MAX_COMPETITORS = 5
MAX_SNIPPETS_PER_ASPECT = 3
MAX_YOUTUBE_TITLES = 5
MAX_NEWS_HEADLINES = 5


def _load_json(path: pathlib.Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def load_raw_material(asin: str, raw_dir: str | pathlib.Path = DEFAULT_RAW_DIR) -> dict[str, Any]:
    """data/raw/per_asin/<ASIN>/ の全ファイルを dict にまとめて読み込む。"""
    base = pathlib.Path(raw_dir) / asin
    return {
        "asin": asin,
        "amazon": _load_json(base / "amazon.json"),
        "competitors": _load_json(base / "competitors.json"),
        "experience": _load_json(base / "experience.json"),
        "youtube": _load_json(base / "youtube.json"),
        "news": _load_json(base / "news.json"),
    }


def allowed_competitor_asins(raw_material: dict[str, Any]) -> set[str]:
    """competitors.json に載っている ASIN の集合 (how_to_choose の封じ込めチェック用)。"""
    comp = raw_material.get("competitors")
    if not isinstance(comp, dict):
        return set()
    items = comp.get("competitors")
    if not isinstance(items, list):
        return set()
    out: set[str] = set()
    for it in items:
        if isinstance(it, dict) and isinstance(it.get("asin"), str) and it["asin"]:
            out.add(it["asin"])
    return out


def experience_snippets_by_aspect(raw_material: dict[str, Any]) -> dict[str, list[str]]:
    """experience.json の snippets を aspect ごとにまとめる。"""
    exp = raw_material.get("experience")
    if not isinstance(exp, dict):
        return {}
    snippets = exp.get("snippets")
    if not isinstance(snippets, list):
        return {}
    out: dict[str, list[str]] = {}
    for s in snippets:
        if not isinstance(s, dict):
            continue
        aspect = s.get("aspect")
        text = s.get("text")
        if isinstance(aspect, str) and isinstance(text, str) and text.strip():
            out.setdefault(aspect, []).append(text.strip())
    return out


def _item_field(item: Any, *names: str) -> Any:
    if not isinstance(item, dict):
        return None
    for n in names:
        if n in item and item[n] not in (None, ""):
            return item[n]
    return None


def build_material_text(raw_material: dict[str, Any], max_len: int = MAX_MATERIAL_TEXT_LEN) -> str:
    """gemma プロンプトに埋め込む「素材」テキストを組み立てる。

    どのソースが欠けていてもクラッシュしない。全体を ``max_len`` で切り詰める
    (num_ctx を明示していても、素材そのものが際限なく育たないようにするため)。
    """
    parts: list[str] = []

    amazon = raw_material.get("amazon")
    item = amazon.get("item") if isinstance(amazon, dict) else None
    if isinstance(item, dict):
        name = _item_field(item, "title", "name_full", "name")
        price = _item_field(item, "price")
        features = _item_field(item, "features")
        lines = ["## 商品データ (Amazon)"]
        if name:
            lines.append(f"商品名: {name}")
        if price:
            lines.append(f"価格: {price}円")
        if isinstance(features, list) and features:
            f_strs = [str(f).strip() for f in features if str(f).strip()][:MAX_FEATURES]
            if f_strs:
                lines.append("特徴: " + " / ".join(f_strs))
        if len(lines) > 1:
            parts.append("\n".join(lines))

    comp = raw_material.get("competitors")
    comp_items = comp.get("competitors") if isinstance(comp, dict) else None
    if isinstance(comp_items, list) and comp_items:
        lines = ["## 競合商品 (比較対象として使ってよいのはこの一覧の ASIN のみ)"]
        for c in comp_items[:MAX_COMPETITORS]:
            if not isinstance(c, dict):
                continue
            c_asin = c.get("asin", "")
            c_name = c.get("name", "")
            c_price = c.get("price")
            price_str = f"{c_price}円" if c_price else "価格不明"
            lines.append(f"- [{c_asin}] {c_name} ({price_str})")
        if len(lines) > 1:
            parts.append("\n".join(lines))

    snippets_by_aspect = experience_snippets_by_aspect(raw_material)
    if snippets_by_aspect:
        lines = ["## 体験談・レビューの要約 (aspect 別)"]
        for aspect, texts in snippets_by_aspect.items():
            for t in texts[:MAX_SNIPPETS_PER_ASPECT]:
                lines.append(f"- [{aspect}] {t}")
        parts.append("\n".join(lines))

    youtube = raw_material.get("youtube")
    yt_items = youtube.get("items") if isinstance(youtube, dict) else None
    if isinstance(yt_items, list) and yt_items:
        titles = [str(_item_field(v, "title") or "").strip() for v in yt_items if isinstance(v, dict)]
        titles = [t for t in titles if t][:MAX_YOUTUBE_TITLES]
        if titles:
            parts.append("## 関連動画タイトル\n" + "\n".join(f"- {t}" for t in titles))

    news = raw_material.get("news")
    news_items = news.get("items") if isinstance(news, dict) else None
    if isinstance(news_items, list) and news_items:
        heads = [str(_item_field(n, "title") or "").strip() for n in news_items if isinstance(n, dict)]
        heads = [h for h in heads if h][:MAX_NEWS_HEADLINES]
        if heads:
            parts.append("## 関連ニュース見出し\n" + "\n".join(f"- {h}" for h in heads))

    text = "\n\n".join(parts)
    if len(text) > max_len:
        text = text[: max_len - 1] + "…"
    return text
