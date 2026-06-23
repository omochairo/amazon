"""Build information-type category hub feature lists (Issue #2687 / 柱1).

実SERP×DA 検証 (#2690) で「テーマ別ランキング hub」が DA11 射程内 (英語/
プログラミング/算数) と確定。bare head ("おもちゃ ランキング") や取引型/
navigational は対象外。本 script は商品 (data/articles/*.json) をテーマに分類し、
知育スコア順に束ねた hub 用 feature データを出力する。746 商品を spoke として
情報型 hub に内部リンクで集約する柱1の中核。

Reads:
    data/articles/*.json   (article body; excludes *.quality.json etc.)

Writes:
    hugo/data/features/<theme>.json   (feature layout が feature_type で読む)

build_feature_lists.py の loader / scorer / serializer を再利用する (スコアは
score_calculator で再計算 = 記事ページ表示値と一致)。外部 API 呼び出しなし。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_feature_lists import (  # noqa: E402
    _dedupe_by_asin,
    _is_article_json,
    _now_iso,
    _record_to_payload_common,
    load_articles,
)

logger = logging.getLogger("build_category_hubs")


# テーマ定義。include のいずれかにマッチし exclude のいずれにもマッチしない商品が
# 候補。マッチは title+name+tags+keywords+features+edu_domains を結合した小文字
# テキストへの部分文字列照合。keyword は #2690 の spoke 実測を踏まえ精度優先。
THEMES: dict[str, dict[str, Any]] = {
    "english": {
        "label": "英語",
        "include": [
            "英語", "えいご", "アルファベット", "フォニックス",
            "english", "abcの", "abc絵", "abcカード", "バイリンガル",
        ],
        "exclude": ["建設"],
    },
    "programming": {
        "label": "プログラミング",
        "include": [
            "プログラミング", "programming", "コーディング",
            "論理的思考", "プログラム的思考", "scratch", "スクラッチ",
        ],
        "exclude": [],
    },
    "math": {
        "label": "算数",
        "include": [
            "算数", "計算", "九九", "そろばん", "たし算", "ひき算",
            "足し算", "引き算", "数の概念", "数量感覚", "アバカス",
        ],
        "exclude": [],
    },
}


def build_theme_text_index(articles_dir: Path) -> dict[str, str]:
    """{asin: 小文字結合テキスト} を返す。同一 ASIN 複数ファイルはテキスト union。"""
    index: dict[str, str] = {}
    if not articles_dir.exists():
        logger.warning("articles_dir does not exist: %s", articles_dir)
        return index
    for path in sorted(articles_dir.glob("*.json")):
        if not _is_article_json(path):
            continue
        try:
            with path.open(encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        prod = raw.get("product") or {}
        asin = prod.get("asin")
        if not asin:
            continue
        parts: list[str] = [
            str(raw.get("title") or ""),
            str(prod.get("name") or ""),
            str(prod.get("name_full") or ""),
        ]
        parts += [str(x) for x in (raw.get("tags") or [])]
        parts += [str(x) for x in (raw.get("keywords") or [])]
        parts += [str(x) for x in (prod.get("features") or [])]
        parts += [str(x) for x in (prod.get("edu_domains") or [])]
        text = " ".join(parts).lower()
        index[str(asin)] = (index.get(str(asin), "") + " " + text).strip()
    return index


def _matches(text: str, theme: dict[str, Any]) -> bool:
    if any(kw in text for kw in theme.get("exclude", [])):
        return False
    return any(kw in text for kw in theme["include"])


def build_hub(records, text_index, theme, *, top_n, min_ivs):
    """テーマにマッチする record を ivs_100 降順 (同点は安価優先) で top_n 束ねる。"""
    pool = []
    for rec in records:
        if rec.ivs_score is None or (rec.ivs_score < min_ivs):
            continue
        text = text_index.get(rec.asin)
        if not text or not _matches(text, theme):
            continue
        pool.append(rec)
    pool.sort(key=lambda r: (-(r.ivs_100 or 0), r.best_price or 10**9))
    return pool[:top_n]


def serialize_hub(items, theme_key: str, generated_at: str) -> dict[str, Any]:
    payload_items = [
        _record_to_payload_common(rec, idx) for idx, rec in enumerate(items, start=1)
    ]
    return {
        "generated_at": generated_at,
        "type": theme_key,
        "count": len(payload_items),
        "items": payload_items,
    }


def run(articles_dir: Path, out_hugo: Path, *, top_n: int, min_ivs: float,
        themes: list[str]) -> dict[str, int]:
    records = _dedupe_by_asin(load_articles(articles_dir))
    text_index = build_theme_text_index(articles_dir)
    generated_at = _now_iso()
    out_hugo.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for key in themes:
        theme = THEMES[key]
        items = build_hub(records, text_index, theme,
                          top_n=top_n, min_ivs=min_ivs)
        payload = serialize_hub(items, key, generated_at)
        (out_hugo / f"{key}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        counts[key] = len(items)
        logger.info("%s (%s): %d items", key, theme["label"], len(items))
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--articles-dir", default="data/articles", type=Path)
    parser.add_argument("--out-hugo", default="hugo/data/features", type=Path)
    parser.add_argument("--top-n", type=int, default=24)
    parser.add_argument("--min-ivs", type=float, default=3.8)
    parser.add_argument("--themes", nargs="*", default=list(THEMES.keys()),
                        choices=list(THEMES.keys()))
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level,
                        format="%(levelname)s %(name)s: %(message)s")
    counts = run(args.articles_dir, args.out_hugo,
                 top_n=args.top_n, min_ivs=args.min_ivs, themes=args.themes)
    print(json.dumps(counts, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
