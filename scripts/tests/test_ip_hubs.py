"""scripts/ip_hubs.py (navi-brain#211 キャラクター別の束ねページ) のテスト。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ip_hubs as iph  # noqa: E402
from build_feature_lists import ArticleRecord  # noqa: E402

TAMA = iph.IP_HUBS["ip-tamagotchi"]


def rec(asin, name, ivs=4.0, price=3000):
    return ArticleRecord(asin=asin, slug=f"2026-01-01-{asin}", name=name, image=None,
                         ivs_score=ivs, ivs_100=int(ivs * 20), best_price=price,
                         best_platform="Amazon", amazon_url=None)


def raw(asin, name, brand=""):
    return {"product": {"asin": asin, "name": name, "name_full": name, "brand": brand}}


def good_narrative(asins, age_keys=("age-6",)):
    return {
        "lead": "たまごっちは、お世話の手間をどこまでかけられるかと、通信で友達と遊びたいかで選び方が変わります。"
                "本体のシリーズごとに、できることと注意したい点を整理しました。迷ったら、遊ぶ子の年齢と、"
                "毎日のお世話を誰が見られるかから考えると決めやすくなります。",
        "choose": [
            {"asin": a, "when": w, "why": "選ぶ理由を三十文字以上で書いた説明文です。短所も踏まえて選べます。"}
            for a, w in zip(asins, ["通信で友達と遊びたい子に", "お世話をひとりで続けたい子に",
                                    "家族みんなで囲んで遊ぶ家庭に", "持ち歩いて外で遊ぶことが多い子に",
                                    "飾ったり集めたりが好きな子に"])
        ],
        "by_age": [{"age_key": k, "text": "この年齢では本体が選べます。"} for k in age_keys],
        "series_tips": [],
        "not_for": ["毎日のお世話が難しいときは、シッター機能のある本体を選びます。"],
        "faq": [{"q": "何歳から遊べますか？", "a": "多くは6歳以上が対象です。"}],
    }


ASINS = [f"B0TEST000{i}" for i in range(1, 6)]


def test_matches_character_uses_name_and_brand_and_skips_compatible():
    al = ("アンパンマン", "Anpanman")
    assert iph.matches_character(raw("B1", "アンパンマン おしゃべりパソコン"), al)
    assert iph.matches_character(raw("B1", "ねんどセット", brand="ANPANMAN"), al)
    # 互換品はキャラクター商品ではない
    assert not iph.matches_character(raw("B1", "デュプロ アンパンマン互換 基礎板"), al)
    assert not iph.matches_character(raw("B1", "木のつみき"), al)


def test_series_and_accessory():
    assert iph.series_of("たまごっちパラダイス Orange Tropics", TAMA) == "たまごっちパラダイス"
    # オリジナルの柄違いの Angel はてんしっちにしない
    assert iph.series_of("Original Tamagotchi Angel Carousel", TAMA) == "オリジナル・初代"
    assert iph.series_of("てんしっち ぱーるぴんく", TAMA) == "てんしっち"
    assert iph.series_of("ドンジャラNEO たまごっち", TAMA) is None
    assert iph.is_accessory("たまごっちパラダイス パステルブルーケース", TAMA)
    assert iph.is_accessory("たまもりしーるリフィル エンジェル＆デビルセット", TAMA)
    assert not iph.is_accessory("たまごっちパラダイス Jade Forest", TAMA)


def test_pick_distinct_limits_series_and_colors():
    recs = [rec("B01", "たまごっちパラダイス Orange", 4.6), rec("B02", "たまごっちパラダイス Blue", 4.5),
            rec("B03", "たまごっちパラダイス Pink", 4.4), rec("B04", "てんしっち ぴんく", 4.3),
            rec("B05", "ドンジャラNEO たまごっち", 4.2), rec("B06", "ドンジャラNEO たまごっち", 4.1)]
    picked = iph.pick_distinct(recs, TAMA, 3)
    assert [r.asin for r in picked] == ["B01", "B04", "B05"]
    # 足りなければ外したものを戻して埋める
    assert len(iph.pick_distinct(recs, TAMA, 6)) == 6


def test_validate_narrative_ok():
    assert iph.validate_narrative(good_narrative(ASINS), set(ASINS), {"age-6"}) == []


def test_validate_narrative_rejects_unknown_and_accessory_and_duplicates():
    n = good_narrative(ASINS)
    n["choose"][0]["asin"] = "B0NOTHERE1"
    n["choose"][1]["asin"] = ASINS[2]
    errs = iph.validate_narrative(n, set(ASINS), {"age-6"}, accessory_asins={ASINS[3]})
    text = " ".join(errs)
    assert "not in this hub" in text
    assert "duplicated" in text
    assert "accessory" in text


def test_validate_narrative_rejects_made_up_facts_and_prices():
    n = good_narrative(ASINS)
    n["lead"] += "価格は3,980円で、最安です。絶対おすすめ。"
    n["faq"].append({"q": "人気ですか？", "a": "累計100万個売れています。"})
    errs = " ".join(iph.validate_narrative(n, set(ASINS), {"age-6"}))
    assert "price amount" in errs
    assert "hype word" in errs
    assert "price-movement word" in errs
    assert "number '100'" in errs


def test_validate_narrative_allows_ages_and_numbers_in_product_names():
    n = good_narrative(ASINS)
    n["faq"].append({"q": "ことばずかん3000とPremiumの違いは？", "a": "どちらも1歳6ヶ月から遊べます。"})
    assert iph.validate_narrative(n, set(ASINS), {"age-6"},
                                  names=["アンパンマンことばずかん3000"]) == []


def test_validate_narrative_rejects_same_branches_and_missing_sections():
    n = good_narrative(ASINS)
    for c in n["choose"]:
        c["when"] = "通信で友達と遊びたい子に"
    n["not_for"] = []
    n["by_age"] = [{"age_key": "age-0", "text": "x"}]
    n["series_tips"] = [{"title": "t", "text": "t", "asins": [ASINS[0]]}]
    errs = " ".join(iph.validate_narrative(n, set(ASINS), {"age-6"}))
    assert "too similar" in errs
    assert "not_for is empty" in errs
    assert "by_age key age-0" in errs
    assert "fewer than 2" in errs


def test_build_payload_sections_and_narrative(tmp_path, monkeypatch):
    names = ["たまごっちパラダイス Orange", "たまごっちパラダイス Blue", "てんしっちのたまごっち ぴんく",
             "ドンジャラNEO たまごっち", "Original Tamagotchi 初代", "たまごっち 新種発見 ほわいと",
             "たまごっちパラダイス パステルブルーケース", "木のつみき"]
    records = [rec(f"B0TEST000{i}", n, 4.5 - i * 0.05) for i, n in enumerate(names, start=1)]
    raw_index = {r.asin: raw(r.asin, r.name) for r in records}
    mm = {r.asin: 72 for r in records}
    ndir = tmp_path / "n"
    ndir.mkdir()
    asins = [r.asin for r in records[:6]]
    (ndir / "ip-tamagotchi.json").write_text(json.dumps(good_narrative(asins[:5]), ensure_ascii=False),
                                             encoding="utf-8")
    top, extra = iph.build_ip_payload("ip-tamagotchi", records, raw_index, mm,
                                      per_asin_dir=tmp_path / "none", narrative_dir=ndir)
    top_asins = [r.asin for r in top]
    assert "B0TEST0008" not in top_asins  # キャラクター外
    assert "B0TEST0007" not in top_asins  # 付属品
    assert extra["stats"]["articles"] == 7
    assert [a["name"] for a in extra["accessories"]] == ["たまごっちパラダイス パステルブルーケース"]
    assert {s["label"] for s in extra["series_sections"]} == {"たまごっちパラダイス"}
    assert [s["key"] for s in extra["age_sections"]] == ["age-6"]
    assert len(extra["narrative"]["choose"]) == 5
    assert extra["narrative"]["choose"][0]["url_internal"] == "/products/b0test0001/"
    assert len(extra["lineup"]) == 7 - len(top)


def test_build_payload_drops_rejected_narrative(tmp_path):
    records = [rec(f"B0TEST000{i}", f"たまごっちパラダイス 色{i}", 4.0) for i in range(1, 4)]
    raw_index = {r.asin: raw(r.asin, r.name) for r in records}
    ndir = tmp_path / "n"
    ndir.mkdir()
    (ndir / "ip-tamagotchi.json").write_text(json.dumps({"lead": "短い"}), encoding="utf-8")
    _top, extra = iph.build_ip_payload("ip-tamagotchi", records, raw_index, {},
                                       per_asin_dir=tmp_path / "none", narrative_dir=ndir)
    assert "narrative" not in extra


# 記事データの変化 (記事が消えた・スコアが下限を割った・年齢の節が無くなった) で起きる違反。
# これはビルド時に文章ごと外して WARNING を出す扱いで、無関係な PR の CI を落とさないためにここでは見ない。
_DATA_DRIFT = ("is not in this hub", "below the score floor", "has no section", "is an accessory")


def test_validate_rejects_broken_shapes_without_crashing():
    for bad in ({"lead": "x", "by_age": None, "choose": "x"},
                {"lead": "x", "series_tips": [{"title": "t"}]},
                {"lead": "x", "faq": [None]},
                {"lead": 1, "not_for": "x"}):
        errs = iph.validate_narrative(bad, set(ASINS), {"age-6"})
        assert errs, bad


def test_numbers_must_match_product_name_numbers_exactly():
    n = good_narrative(ASINS)
    n["faq"].append({"q": "人気ですか？", "a": "30件の記事があります。1.5歳から遊べます。"})
    errs = " ".join(iph.validate_narrative(n, set(ASINS), {"age-6"}, names=["ことばずかん3000"]))
    assert "number '30'" in errs  # 3000 の部分一致では通さない
    assert "number '1" not in errs and "number '5'" not in errs  # 小数の年齢は年齢として通す
    n2 = good_narrative(ASINS)
    n2["lead"] += "１万円以内で選べます。ナンバーワンの人気。"
    errs2 = " ".join(iph.validate_narrative(n2, set(ASINS), {"age-6"}))
    assert "price amount" in errs2 and "hype word" in errs2


def test_accessory_with_tsuki_is_not_accessory():
    assert not iph.is_accessory("たまごっちパラダイス 特典ステッカー付き", TAMA)
    assert iph.is_accessory("たまごっち 4sizeステッカー", TAMA)


def test_committed_narratives_pass_against_current_articles():
    """リポジトリに置いた文章が、今の記事データに当てても検査に通ること (データの変化による違反を除く)。"""
    repo = Path(__file__).resolve().parents[2]
    articles = repo / "data" / "articles"
    paths = sorted((repo / "data" / "hub_narratives").glob("*.json"))
    if not paths or not articles.is_dir():
        return
    import os
    cwd = os.getcwd()
    os.chdir(repo)  # character_terms.yaml などは repo 相対で読む
    try:
        records, raw_index, mm_index = iph._load_context(articles, repo / "data/raw/per_asin", repo / "data/raw")
        for path in paths:
            n = json.loads(path.read_text(encoding="utf-8"))
            errs = iph.check_against_data(path.stem, n, records, raw_index, mm_index,
                                          per_asin_dir=repo / "data/raw/per_asin")
            errs = [e for e in errs if not any(d in e for d in _DATA_DRIFT)]
            assert errs == [], (path.name, errs)
    finally:
        os.chdir(cwd)
