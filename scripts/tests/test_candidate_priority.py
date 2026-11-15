import datetime as dt
import json
import pathlib
import random
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import candidate_priority as cp
import article_pick


TERMS = {"ポケモン": ["Pokemon", "ポケットモンスター"], "トミカ": ["TOMICA"]}


def test_score_breakdown_and_character_alias_english():
    score, detail, char = cp.score_candidate({
        "asin": "B0H1234567", "title": "Pokemon TOMICA set", "lane": "demand",
        "price": 12000, "brand": "タカラトミー",
    }, character_terms=TERMS, existing_class_counts={})
    assert char == "ポケモン"
    assert detail == {"asin_prefix": 2, "character": 2, "origin_demand": 2,
                      "brand_tier": 1, "price": 1, "similarity": 0}
    assert score == 8


def test_brand_tier_can_be_resolved_from_amazon_title():
    score, detail, _ = cp.score_candidate({
        "asin": "B000000001", "title": "[BANDAI] toy set", "lane": "supply-random",
        "price": 1,
    }, character_terms={})
    assert detail["brand_tier"] == 1
    assert score == 1


def test_similar_requires_random_weak_unmatched_and_existing_count():
    item = {"asin": "B0H1234567", "title": "shape puzzle", "lane": "supply-random",
            "price": "bad", "browse_nodes": [{"id": "123"}]}
    assert cp.similar_candidate(item, lane="supply-random", tier="D", character=None,
                                existing_class_counts={"123": 30})
    assert not cp.similar_candidate(item, lane="demand", tier="D", character=None,
                                    existing_class_counts={"123": 50})
    assert not cp.similar_candidate(item, lane="supply-random", tier="A", character=None,
                                    existing_class_counts={"123": 50})
    assert not cp.similar_candidate(item, lane="supply-random", tier="D", character="トミカ",
                                    existing_class_counts={"123": 50})


def test_recent_character_counts_only_recent_named_rows(tmp_path):
    now = dt.datetime(2026, 11, 15, tzinfo=dt.timezone.utc)
    rows = [
        {"ts": "2026-11-14T00:00:00Z", "character": "ポケモン"},
        {"ts": "2026-11-01T00:00:00Z", "character": "ポケモン"},
        {"ts": "2026-11-14T00:00:00Z", "pool": "demand"},
    ]
    p = tmp_path / "origin.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in rows), encoding="utf-8")
    assert cp.load_recent_character_counts(p, now=now) == {"ポケモン": 1}


def test_keyword_sort_and_caps(tmp_path, capsys):
    terms_path = tmp_path / "characters.yaml"
    terms_path.write_text("characters:\n  ポケモン: [Pokemon]\n", encoding="utf-8")
    items = [
        {"asin": "B0A0000001", "title": "generic puzzle", "lane": "supply-random"},
        {"asin": "B0H0000001", "title": "Pokemon play", "lane": "demand"},
        {"asin": "B0H0000002", "title": "Pokemon cards", "lane": "demand"},
        {"asin": "B0A0000002", "title": "another generic", "lane": "supply-random"},
    ]
    # Deterministic stable tie behavior: shuffle order is retained among equal scores.
    out = article_pick.select_candidates(items, set(), [], [], None, 7, scorer=None,
        character_terms_path=terms_path, recent_character_counts={},
        existing_class_counts={}, has_browse_nodes=False)
    assert out[0]["asin"] == "B0H0000001"
    assert out[0]["character"] == "ポケモン"
    # 同じキャラクターの 2 件目は捨てずに、上限に掛からなかった候補の後ろへ回る
    order = [x["asin"] for x in out]
    assert order[-1] == "B0H0000002"
    assert order.index("B0H0000002") > order.index("B0A0000001")
    assert order.index("B0H0000002") > order.index("B0A0000002")
    assert "character=1" in capsys.readouterr().out


def test_absent_character_dictionary_loads_empty(tmp_path):
    assert cp.load_character_terms(tmp_path / "missing.yaml") == {}
    assert cp.match_character({"title": "Pokemon"}, {}) is None


def test_absent_dictionary_keeps_seeded_random_order_and_zero_scores(tmp_path):
    items = [{"asin": f"B0A00000{i:02d}", "title": "toy", "lane": "demand"}
             for i in range(1, 8)]
    expected = [i["asin"] for i in items]
    random.Random("seed").shuffle(expected)
    out = article_pick.select_candidates(items, set(), [], [], None, "seed",
        character_terms_path=tmp_path / "missing.yaml", recent_character_counts={})
    assert [x["asin"] for x in out] == expected
    assert all(x["priority_score"] == 0 for x in out)


def test_similar_cap_and_recent_character_cap(tmp_path):
    terms_path = tmp_path / "characters.yaml"
    terms_path.write_text("characters:\n  ポケモン: [Pokemon]\n", encoding="utf-8")
    items = [
        {"asin": "B0A0000001", "title": "plain puzzle", "lane": "supply-random"},
        {"asin": "B0A0000002", "title": "plain blocks", "lane": "supply-random"},
        {"asin": "B0A0000003", "title": "Pokemon toy", "lane": "demand"},
    ]
    out = article_pick.select_candidates(items, set(), [], [], None, 1,
        character_terms_path=terms_path, recent_character_counts={"ポケモン": 3},
        existing_class_counts={"__all__": 30}, has_browse_nodes=False)
    order = [o["asin"] for o in out]
    # 上限に掛からなかった候補は 1 件 (類似品の 1 件目) だけで、残りの 2 件
    # (類似品の 2 件目・直近 7 日に 3 件以上のキャラクター) はその後ろに回る
    first_similar = [x for x in order if x in ("B0A0000001", "B0A0000002")][0]
    assert order[0] == first_similar
    assert set(order[1:]) == {"B0A0000003", ({"B0A0000001", "B0A0000002"} - {first_similar}).pop()}


def test_compatible_knockoff_is_not_character(tmp_path):
    terms_path = tmp_path / "characters.yaml"
    terms_path.write_text("characters:\n  アンパンマン: [アンパンマン]\n", encoding="utf-8")
    terms = cp.load_character_terms(terms_path)
    assert cp.match_character({"title": "アンパンマン ブロックラボ"}, terms) == "アンパンマン"
    # 「デュプロ・アンパンマン互換」の基礎板はキャラクター商品ではない
    assert cp.match_character({"title": "ブロック 基礎板 デュプロ アンパンマン 互換"}, terms) is None
