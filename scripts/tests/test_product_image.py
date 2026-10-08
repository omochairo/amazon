"""product_image: 集計ページの画像を amazon.json の検証済み画像に揃える (#2812 の横展開)。

/deals/ で B0DF72LSP7 の画像が壊れた事故の回帰テスト。記事 JSON の
``product.image`` に Jules が捏造した URL (``71xyz123abc._AC_SX679_.jpg``) が
入っていても、amazon.json に画像があればそちらを出す。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import build_brand_hub_stats  # noqa: E402
import build_feature_lists  # noqa: E402
import build_post  # noqa: E402
import build_price_dashboard  # noqa: E402
import product_image  # noqa: E402
import quality_census  # noqa: E402
import quality_gate  # noqa: E402

ASIN = "B0DF72LSP7"
FAKE = "https://m.media-amazon.com/images/I/71xyz123abc._AC_SX679_.jpg"
REAL = "https://m.media-amazon.com/images/I/41PF9dk71TL._SL500_.jpg"


def _write_amazon(root: Path, asin: str, item: dict, *, wrapped: bool = True) -> Path:
    d = root / asin
    d.mkdir(parents=True, exist_ok=True)
    payload = {"asin": asin, "fetched_at": "2026-10-01T00:00:00+00:00", "item": item} if wrapped else item
    (d / "amazon.json").write_text(json.dumps(payload), encoding="utf-8")
    return root


def _write_article(articles: Path, asin: str, image: str) -> None:
    articles.mkdir(parents=True, exist_ok=True)
    data = {
        "slug": f"2026-09-25-{asin}",
        "product": {
            "asin": asin,
            "name": "すみっコスマホワイド",
            "brand": "タカラトミー",
            "image": image,
            "best_price": 6600,
            "prices": {"amazon": {"price": 6600, "url": f"https://www.amazon.co.jp/dp/{asin}"}},
        },
    }
    (articles / f"2026-09-25-{asin}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_resolve_prefers_amazon_json(tmp_path):
    root = _write_amazon(tmp_path, ASIN, {"image": REAL})
    assert product_image.resolve_product_image(ASIN, FAKE, root) == REAL


def test_resolve_falls_back_to_images_then_article(tmp_path):
    root = _write_amazon(tmp_path, ASIN, {"images": ["", REAL]})
    assert product_image.resolve_product_image(ASIN, FAKE, root) == REAL
    # amazon.json が無い ASIN は記事 JSON の値のまま (fail-soft)
    assert product_image.resolve_product_image("B000000000", FAKE, root) == FAKE
    assert product_image.resolve_product_image("B000000000", None, root) == ""


def test_load_accepts_legacy_root_shape_and_lowercase_asin(tmp_path):
    root = _write_amazon(tmp_path, ASIN, {"image": REAL}, wrapped=False)
    assert product_image.load_amazon_image(root, ASIN.lower()) == REAL


def test_load_tolerates_broken_json(tmp_path):
    (tmp_path / ASIN).mkdir()
    (tmp_path / ASIN / "amazon.json").write_text("{", encoding="utf-8")
    assert product_image.load_amazon_image(tmp_path, ASIN) == ""


def test_non_list_images_and_blank_asin_are_tolerated(tmp_path):
    root = _write_amazon(tmp_path, ASIN, {"images": 3})
    assert product_image.load_amazon_image(root, ASIN) == ""
    # 空白だけの ASIN で per_asin 直下の amazon.json を読みに行かない
    (tmp_path / "amazon.json").write_text(json.dumps({"item": {"image": REAL}}), encoding="utf-8")
    assert product_image.load_amazon_image(tmp_path, "  ") == ""
    assert product_image.load_amazon_image(tmp_path, 123) == ""


def test_missing_image_is_empty_string_everywhere(tmp_path):
    articles = tmp_path / "articles"
    _write_article(articles, ASIN, None)
    meta = build_price_dashboard.load_article_meta(articles, tmp_path / "per_asin")
    assert meta[ASIN]["image"] == ""


def test_feature_lists_overlay_replaces_fabricated_image(tmp_path):
    root = _write_amazon(tmp_path / "per_asin", ASIN, {"image": REAL})
    articles = tmp_path / "articles"
    _write_article(articles, ASIN, FAKE)
    records = build_feature_lists.load_articles(articles)
    assert records and records[0].image == FAKE
    assert build_feature_lists.overlay_amazon_images(records, root) == 1
    assert records[0].image == REAL


def test_brand_hub_and_dashboard_use_amazon_image(tmp_path):
    root = _write_amazon(tmp_path / "per_asin", ASIN, {"image": REAL})
    articles = tmp_path / "articles"
    _write_article(articles, ASIN, FAKE)
    meta = build_price_dashboard.load_article_meta(articles, root)
    assert meta[ASIN]["image"] == REAL
    # ブランドハブは narrative_min_count (3) 件以上で代表画像を出す
    for other in ("B0DF72LSP8", "B0DF72LSP9"):
        _write_amazon(root, other, {"image": REAL})
        _write_article(articles, other, FAKE)
    payload = json.dumps(build_brand_hub_stats.aggregate(articles, root))
    assert REAL in payload and FAKE not in payload


def test_build_post_article_index_uses_amazon_image(tmp_path):
    root = _write_amazon(tmp_path / "per_asin", ASIN, {"image": REAL})
    articles = tmp_path / "articles"
    _write_article(articles, ASIN, FAKE)
    index = build_post._build_article_index(articles, root)
    assert index[ASIN]["image"] == REAL


# --- #9155 B 案: 品質ゲートは落とさず減点だけする -------------------------

def _article(image):
    return {"slug": f"2026-10-08-{ASIN}", "product": {"asin": ASIN, "image": image}}


def test_gate_ok_when_same_image_id_even_if_size_differs(tmp_path):
    root = _write_amazon(tmp_path, ASIN, {"image": REAL})
    r = quality_gate.check_product_image_matches_amazon(
        _article("https://m.media-amazon.com/images/I/41PF9dk71TL._AC_SX679_.jpg"), root)
    assert r.passed and r.score == 1.0


def test_gate_warns_on_fabricated_image_without_failing(tmp_path):
    root = _write_amazon(tmp_path, ASIN, {"image": REAL, "images": [REAL]})
    r = quality_gate.check_product_image_matches_amazon(_article(FAKE), root)
    assert r.passed and r.score < 1.0
    # census の集計キー (最初の ";" より前) に URL を含めない
    assert FAKE not in quality_census.normalize_reason(r.message)
    other = quality_gate.check_product_image_matches_amazon(
        _article("https://m.media-amazon.com/images/I/dummy.jpg"), root)
    assert quality_census.normalize_reason(other.message) == quality_census.normalize_reason(r.message)


def test_gate_warns_on_empty_and_skips_without_amazon_image(tmp_path):
    root = _write_amazon(tmp_path, ASIN, {"image": REAL})
    r = quality_gate.check_product_image_matches_amazon(_article(""), root)
    assert r.passed and r.score < 1.0
    no_img = quality_gate.check_product_image_matches_amazon(_article(FAKE), tmp_path / "none")
    assert no_img.passed and no_img.score == 1.0
