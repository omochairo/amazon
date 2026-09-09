"""Issue #6821 項目1/2: 構造化データ欠損スキャナのテスト。

このスクリプトの価値は「欠損を数えられること」ではなく **欠損の原因が生成側の
分岐と 1:1 で対応していること** にある (原因ラベルをそのまま issue にぶら下げる)。
そのため、原因ラベルごとに「その分岐に落ちる最小の記事 JSON」を作って検証する。

もう 1 点、監査は read-only でなければならない (data/articles を書き換えたら
記事生成に影響する)。`audit_article` が引数を変更しないことも直接見る。
"""
from __future__ import annotations

import copy
import json
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, "..", ".."))
sys.path.insert(0, os.path.join(THIS_DIR, ".."))

from scripts.audit_structured_data import audit_article, run  # noqa: E402


def _article(**over):
    """全エンティティが揃う記事 JSON の最小形。"""
    data = {
        "title": "テスト商品のレビュー",
        "date": "2026-01-01T10:00:00+09:00",
        "meta_description": "説明",
        "editorial_comment": "おもちゃロボの所感。",
        "faq": [{"q": "対象年齢は?", "a": "3歳から。"}],
        "product": {
            "asin": "B000000001",
            "name": "テスト商品",
            "image": "https://example.com/i.jpg",
            "brand": "テストブランド",
            "ivs_score": 4.2,
            "prices": {
                "amazon": {"price": 1000},
                "rakuten": {"price": 1200},
                "yahoo": {"price": 0},
            },
        },
    }
    data.update(over)
    return data


def test_full_article_has_every_entity():
    res = audit_article(_article())
    for key in ("product", "aggregateRating", "offers", "review", "faq", "webpage"):
        assert res["present"][key] is True, key
    assert res["causes"] == []
    assert res["flags"] == []


def test_audit_does_not_mutate_input():
    data = _article()
    before = copy.deepcopy(data)
    audit_article(data)
    assert data == before


def test_offers_missing_when_no_valid_price():
    art = _article()
    art["product"]["prices"] = {"amazon": {"price": 0}}
    art["product"].pop("best_price", None)
    res = audit_article(art)
    assert res["present"]["offers"] is False
    assert "offers_missing__price_unavailable" in res["causes"]
    # 価格が無いだけで Product/Review 側は欠けない (原因の取り違えを防ぐ)
    assert res["present"]["product"] is True
    assert res["present"]["review"] is True


def test_offers_present_from_best_price_only():
    art = _article()
    art["product"].pop("prices")
    art["product"]["best_price"] = 2480
    res = audit_article(art)
    assert res["present"]["offers"] is True
    assert res["causes"] == []


def test_is_search_price_does_not_count_as_offer():
    """検索結果 URL の価格は offer にしない (#4826 と同じ「買えないものを出さない」)。"""
    art = _article()
    art["product"]["prices"] = {"rakuten": {"price": 1500, "is_search": True}}
    res = audit_article(art)
    assert res["present"]["offers"] is False
    assert "offers_missing__price_unavailable" in res["causes"]


def test_review_missing_when_no_review_body():
    art = _article()
    art.pop("editorial_comment")
    res = audit_article(art)
    assert res["present"]["review"] is False
    assert "review_missing__no_review_body" in res["causes"]


def test_faq_missing_when_no_faq_source():
    art = _article()
    art.pop("faq")
    res = audit_article(art)
    assert res["present"]["faq"] is False
    assert "faq_missing__no_faq_source" in res["causes"]


def test_no_asin_stops_at_product():
    """build_post の `if product and asin` に対応。webpage/review は emit されない。"""
    art = _article()
    art["product"].pop("asin")
    res = audit_article(art)
    assert res["present"]["product"] is True
    assert res["present"]["webpage"] is False
    assert res["present"]["review"] is False
    assert "product_missing__no_asin" in res["causes"]


def test_no_product_block():
    res = audit_article({"title": "t"})
    assert res["present"]["product"] is False
    assert res["causes"] == ["product_missing__no_product"]


def test_image_empty_is_flag_not_missing():
    art = _article()
    art["product"]["image"] = ""
    res = audit_article(art)
    assert res["present"]["product"] is True
    assert "product_image_empty" in res["flags"]
    assert res["causes"] == []


def test_run_aggregates_counts_and_samples(tmp_path):
    ok = _article()
    ng = _article()
    ng["product"]["asin"] = "B000000002"
    ng["product"]["prices"] = {"amazon": {"price": 0}}
    (tmp_path / "2026-01-01-B000000001.json").write_text(
        json.dumps(ok, ensure_ascii=False), encoding="utf-8"
    )
    (tmp_path / "2026-01-01-B000000002.json").write_text(
        json.dumps(ng, ensure_ascii=False), encoding="utf-8"
    )
    # sidecar と ASIN でないファイルは discover_articles が除外する
    (tmp_path / "2026-01-01-B000000001.quality.json").write_text("{}", encoding="utf-8")

    report = run(tmp_path)
    assert report["articles_total"] == 2
    assert report["present"]["product"] == 2
    assert report["present"]["offers"] == 1
    assert report["missing"]["offers"] == 1
    assert report["causes"]["offers_missing__price_unavailable"] == 1
    assert report["samples"]["offers_missing__price_unavailable"] == ["B000000002"]
    assert report["unreadable"] == []


def test_run_records_unreadable_file(tmp_path):
    (tmp_path / "2026-01-01-B000000003.json").write_text("{ broken", encoding="utf-8")
    report = run(tmp_path)
    assert report["articles_total"] == 0
    assert report["unreadable"] == ["2026-01-01-B000000003.json"]


def test_run_limit(tmp_path):
    for i in (1, 2, 3):
        (tmp_path / f"2026-01-01-B00000000{i}.json").write_text(
            json.dumps(_article(), ensure_ascii=False), encoding="utf-8"
        )
    assert run(tmp_path, limit=2)["articles_total"] == 2
