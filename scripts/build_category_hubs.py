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
    overlay_current_prices,
    parse_min_months,
)

logger = logging.getLogger("build_category_hubs")


# モンテッソーリ hub 用の signal 群 (#3654)。「モンテッソーリ」tag を持たない木製
# 教具 (積み木/型はめ/紐通し等) が多数取りこぼされる実態 (実測: 語彙一致 40 件中
# ivs>=3.8 は 2 件だが、木製 AND モンテ的教具形態の高スコア商品が別に 6 件埋もれる)
# に対応するため、明示語 (include) に加えて「天然素材 AND 教具形態」の両群一致を
# 第 2 の合格経路 (all_of) として持つ。過剰マッチ (量産プラ/キャラ/電子玩具) は
# _MONTESSORI_DENY で fail-closed。
_MONTESSORI_WOOD = [
    "木製", "木のおもちゃ", "無垢", "ブナ材", "ぶな材", "beech", "天然木",
]
_MONTESSORI_FORM = [
    "型はめ", "型落とし", "紐通し", "ひもとおし", "ペグ", "スタッキング",
    "日常生活", "はさみの練習", "ボタンかけ", "分類", "並べ替え",
    "感覚教育", "実物大", "自己教育", "円柱", "はめ込みパズル",
]
_MONTESSORI_DENY = [
    "lego", "レゴ", "デュプロ", "duplo", "アンパンマン", "フィッシャープライス",
    "fisher", "ディズニー", "disney", "トミカ", "プラレール", "電子", "光る",
]

# 図形・空間認識 hub 用 DENY (#2687 柱1 / #2690 iroya 型テーマ拡張)。図形/立体/
# 空間の語はキャラ立体パズルや量産構成玩具にも現れるため、ライセンスキャラ・乗り物
# を除外して編集型ランキングの品質を保つ。レゴは character セットが大量に混入する
# ため除外 (レゴクラシックの構成玩具は将来 supply 判断で個別追加余地あり)。
_SHAPE_DENY = [
    "トミカ", "プラレール", "シンカリオン", "アンパンマン", "ディズニー", "disney",
    "リカちゃん", "シルバニア", "レゴ", "lego", "デュプロ", "duplo", "ニンジャゴー",
    "ソニック", "マリオ", "ポケモン", "すみっこ", "すみっコ", "妖怪",
]


# テーマ定義。exclude のいずれにもマッチせず、かつ include のいずれかにマッチする
# (または all_of の全群にマッチする) 商品が候補。マッチは title+name+tags+keywords
# +features+edu_domains を結合した小文字テキストへの部分文字列照合。keyword は
# #2690 の spoke 実測を踏まえ精度優先。min_ivs は省略時 CLI 既定 (3.8) を使う。
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
    "montessori": {
        "label": "モンテッソーリ",
        # 明示語 (単独で合格) — merchant が「モンテッソーリ」を自称する商品を拾う。
        "include": ["モンテッソーリ", "montessori", "モンテ", "教具"],
        # 第 2 経路: 天然素材群 AND 教具形態群 の両方にマッチ (取りこぼし回収)。
        "all_of": [_MONTESSORI_WOOD, _MONTESSORI_FORM],
        "exclude": _MONTESSORI_DENY,
        # テーマ既定 (3.8) だと適格 2 件で thin hub になるため 3.4 に個別緩和し 10 件
        # 確保 (#3654 owner 承認)。閾値感度で 3.3 だと 21 件へ低スコアクラスタが流入
        # するため 3.4 が「勝てるゾーンの品質」を保てる上限。
        "min_ivs": 3.4,
    },
    "shape": {
        # 図形・空間認識 (#2690 iroya 型テーマ拡張・供給レディで実装)。マグ・フォーマー
        # 等の磁石構成・立体パズル・タングラム・図形モザイクを束ねる情報型 hub。実測で
        # ivs>=3.8 の適格が 17 件・14 ブランド揃うため min_ivs 緩和は不要。既存 math hub
        # (計算/数のみ) とは include が重ならず near-duplicate 化しない (重複は 4/17 と低い)。
        "label": "図形・空間",
        "include": [
            "図形", "立体パズル", "立体図形", "タングラム", "パターンブロック",
            "空間認識", "幾何", "ジオボード", "マグ・フォーマー", "マグフォーマー",
            "図形モザイク", "ペントミノ",
        ],
        "exclude": _SHAPE_DENY,
    },
}


# 年齢 hub 定義 (#2687 柱1・年齢軸)。テーマ hub と異なり数値軸 = persona_fit.age_range
# から抽出した「最小推奨年齢 (月)」でバケットへ単一所属させる。範囲オーバーラップ
# (3歳以上 を 3/4/5歳 hub に重複所属) させると隣接 age hub が near-duplicate 化し
# #2765 で潰した hub カニバリを再発させるため、最小年齢アンカーで 1 商品 1 hub に限定。
# min_months <= x < max_months。max=216 (18歳) で adult 玩具を全 hub から除外。
AGE_HUBS: dict[str, dict[str, Any]] = {
    "age-0": {"label": "0歳", "min_months": 0, "max_months": 12},
    "age-1": {"label": "1歳", "min_months": 12, "max_months": 24},
    "age-2": {"label": "2歳", "min_months": 24, "max_months": 36},
    "age-3": {"label": "3歳", "min_months": 36, "max_months": 48},
    "age-4": {"label": "4〜5歳", "min_months": 48, "max_months": 72},
    "age-6": {"label": "6歳以上", "min_months": 72, "max_months": 216},
}


def build_min_months_index(articles_dir: Path) -> dict[str, int]:
    """{asin: 最小推奨年齢(月)} を返す。同一 ASIN 複数ファイルは最小値を採用。"""
    index: dict[str, int] = {}
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
        asin = (raw.get("product") or {}).get("asin")
        if not asin:
            continue
        mm = parse_min_months((raw.get("persona_fit") or {}).get("age_range"))
        if mm is None:
            continue
        prev = index.get(str(asin))
        index[str(asin)] = mm if prev is None else min(prev, mm)
    return index


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
    if any(kw in text for kw in theme.get("include", [])):
        return True
    # all_of: 第 2 の合格経路。指定した全群それぞれで 1 語以上マッチしたら合格
    # (#3654 モンテッソーリ = 木製 AND 教具形態)。未指定テーマは従来どおり include のみ。
    all_of = theme.get("all_of")
    if all_of:
        return all(any(kw in text for kw in group) for group in all_of)
    return False


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


def build_age_hub(records, min_months_index, hub, *, top_n, min_ivs):
    """最小推奨年齢が [min_months, max_months) に入る record を ivs_100 降順で束ねる。"""
    lo, hi = hub["min_months"], hub["max_months"]
    pool = []
    for rec in records:
        if rec.ivs_score is None or (rec.ivs_score < min_ivs):
            continue
        mm = min_months_index.get(rec.asin)
        if mm is None or not (lo <= mm < hi):
            continue
        pool.append(rec)
    pool.sort(key=lambda r: (-(r.ivs_100 or 0), r.best_price or 10**9))
    return pool[:top_n]


def build_age_lineup(records, min_months_index, hub, exclude_asins):
    """年齢hub の全ラインナップ節 (#2687 Slice A) 用に bucket 全 member を返す。

    build_age_hub と異なり **min_ivs ゲートを適用しない** (低スコア長尾も網羅する
    = crawl discovery が目的)。top-24 curated (``exclude_asins``) を除いた残りを
    ivs_100 降順 (同点は best_price 昇順) で返す。discontinued フラグ (旧
    ``is_purchase_unavailable`` 相当) は ArticleRecord に無いため適用しない
    (対象は live 記事のみなので許容)。
    """
    lo, hi = hub["min_months"], hub["max_months"]
    pool = []
    for rec in records:
        if rec.asin in exclude_asins:
            continue
        mm = min_months_index.get(rec.asin)
        if mm is None or not (lo <= mm < hi):
            continue
        pool.append(rec)
    pool.sort(key=lambda r: (-(r.ivs_100 or 0), r.best_price or 10**9))
    return pool


def _lineup_entry(rec, age_min_months: int | None) -> dict[str, Any]:
    """全ラインナップ節用の最小 payload (カード用の重い項目は持たない)。"""
    return {
        "asin": rec.asin,
        "name": rec.name,
        "url_internal": f"/products/{rec.asin.lower()}/",
        "ivs_100": rec.ivs_100,
        "age_min_months": age_min_months,
    }


def serialize_hub(items, theme_key: str, generated_at: str,
                   *, lineup: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    payload_items = [
        _record_to_payload_common(rec, idx) for idx, rec in enumerate(items, start=1)
    ]
    payload: dict[str, Any] = {
        "generated_at": generated_at,
        "type": theme_key,
        "count": len(payload_items),
        "items": payload_items,
    }
    if lineup is not None:
        payload["lineup"] = lineup
    return payload


def _write_hub(out_hugo: Path, key: str, items, generated_at: str,
               counts: dict[str, int], label: str,
               *, lineup: list[dict[str, Any]] | None = None) -> None:
    payload = serialize_hub(items, key, generated_at, lineup=lineup)
    (out_hugo / f"{key}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    counts[key] = len(items)
    logger.info("%s (%s): %d items", key, label, len(items))


def _write_hub_index(out_hugo: Path, entries: list[dict[str, str]]) -> None:
    """info hub 集合の単一 source of truth (#2687 Slice C)。

    生成した全 info hub (THEMES + AGE_HUBS) の ``[{key, label, url}]`` を
    平坦な JSON 配列で書く。hugo/layouts/partials/hub_siblings.html が
    ``site.Data.features.hub_index`` として直接 range する。
    """
    (out_hugo / "hub_index.json").write_text(
        json.dumps(entries, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    logger.info("hub_index: %d hubs", len(entries))


def run(articles_dir: Path, out_hugo: Path, *, top_n: int, min_ivs: float,
        themes: list[str], age_hubs: list[str] | None = None,
        per_asin_dir: Path | None = None,
        raw_root: Path | None = None) -> dict[str, int]:
    records = _dedupe_by_asin(load_articles(articles_dir))
    # #4007: hub カードも記事ページ・/deals/ /cospa/ と同じ日次観測の価格を出す。
    # per_asin_dir=None のときは price_overlay の既定 (data/raw/per_asin) を使う。
    # raw_root を渡すと楽天/Yahoo も matched JSON で更新する (follow-up 1)。
    stats = overlay_current_prices(records, per_asin_dir, raw_root=raw_root)
    logger.info("price_overlay: %d price_watch / %d per_asin / %d no observation",
                stats["price_watch"], stats["per_asin"], stats["none"])
    logger.info(
        "market_prices: %d rakuten / %d yahoo updated from matched JSON, "
        "%d extreme outlier dropped",
        stats["market_rakuten"], stats["market_yahoo"], stats["market_extreme_dropped"],
    )
    generated_at = _now_iso()
    out_hugo.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    hub_index: list[dict[str, str]] = []
    if themes:
        text_index = build_theme_text_index(articles_dir)
        for key in themes:
            theme = THEMES[key]
            # テーマ個別 min_ivs があれば優先 (#3654 montessori=3.4)。
            theme_min_ivs = theme.get("min_ivs", min_ivs)
            items = build_hub(records, text_index, theme,
                              top_n=top_n, min_ivs=theme_min_ivs)
            _write_hub(out_hugo, key, items, generated_at, counts, theme["label"])
            hub_index.append({"key": key, "label": theme["label"],
                              "url": f"/{key}-toys/"})
    if age_hubs:
        mm_index = build_min_months_index(articles_dir)
        for key in age_hubs:
            hub = AGE_HUBS[key]
            items = build_age_hub(records, mm_index, hub,
                                  top_n=top_n, min_ivs=min_ivs)
            lineup_recs = build_age_lineup(
                records, mm_index, hub, exclude_asins={r.asin for r in items})
            lineup = [_lineup_entry(r, mm_index.get(r.asin)) for r in lineup_recs]
            _write_hub(out_hugo, key, items, generated_at, counts, hub["label"],
                      lineup=lineup)
            hub_index.append({"key": key, "label": hub["label"],
                              "url": f"/toys-{key}/"})
    if themes or age_hubs:
        _write_hub_index(out_hugo, hub_index)
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--articles-dir", default="data/articles", type=Path)
    parser.add_argument("--out-hugo", default="hugo/data/features", type=Path)
    parser.add_argument("--top-n", type=int, default=24)
    parser.add_argument("--min-ivs", type=float, default=3.8)
    parser.add_argument("--themes", nargs="*", default=list(THEMES.keys()),
                        choices=list(THEMES.keys()))
    parser.add_argument("--age-hubs", nargs="*", default=list(AGE_HUBS.keys()),
                        choices=list(AGE_HUBS.keys()),
                        help="生成する年齢 hub キー (既定=全件)。")
    # #4007: 価格を日次観測で上書きするための per_asin スナップショット置き場。
    parser.add_argument("--per-asin-dir", default=Path("data/raw/per_asin"), type=Path)
    # #4007 follow-up 1: data/raw/{rakuten,yahoo}_matched.json の親ディレクトリ。
    # 楽天/Yahoo 価格を build_post.py と同じ matched JSON で更新するために使う。
    parser.add_argument("--raw-root", default=Path("data/raw"), type=Path)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level,
                        format="%(levelname)s %(name)s: %(message)s")
    counts = run(args.articles_dir, args.out_hugo,
                 top_n=args.top_n, min_ivs=args.min_ivs,
                 themes=args.themes, age_hubs=args.age_hubs,
                 per_asin_dir=args.per_asin_dir,
                 raw_root=args.raw_root)
    print(json.dumps(counts, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
