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


def test_cards_carry_amazon_url_and_price_for_mini_links():
    import dataclasses
    r = dataclasses.replace(rec("B0TEST0001", "たまごっちパラダイス Orange"),
                            amazon_url="https://www.amazon.co.jp/dp/B0TEST0001/")
    b = iph._brief(r, 72, [])
    assert b["amazon_url"] == "https://www.amazon.co.jp/dp/B0TEST0001/"
    assert b["best_price"] == 3000


def test_compare_entry_carries_amazon_url():
    import dataclasses
    import build_category_hubs as bch
    r = dataclasses.replace(rec("B0TEST0002", "アンパンマン ブロックラボ"),
                            amazon_url="https://www.amazon.co.jp/dp/B0TEST0002/")
    assert bch._compare_entry(r, 36)["amazon_url"] == "https://www.amazon.co.jp/dp/B0TEST0002/"


def test_build_keeps_narrative_when_only_some_products_drift(tmp_path):
    names = ["たまごっちパラダイス Orange", "てんしっちのたまごっち ぴんく", "ドンジャラNEO たまごっち",
             "Original Tamagotchi 初代", "たまごっち 新種発見 ほわいと"]
    records = [rec(f"B0TEST000{i}", n, 4.5 - i * 0.05) for i, n in enumerate(names, start=1)]
    raw_index = {r.asin: raw(r.asin, r.name) for r in records}
    ndir = tmp_path / "n"
    ndir.mkdir()
    n = good_narrative([r.asin for r in records])
    n["by_age"].append({"age_key": "age-0", "text": "この年齢の節は今は無い。"})
    (ndir / "ip-tamagotchi.json").write_text(json.dumps(n, ensure_ascii=False), encoding="utf-8")
    # 1 本の記事が消え、1 本のスコアが下限を割った
    kept = records[:3] + [rec("B0TEST0004", names[3], 3.0)]
    _top, extra = iph.build_ip_payload("ip-tamagotchi", kept, raw_index, {r.asin: 72 for r in kept},
                                       per_asin_dir=tmp_path / "none", narrative_dir=ndir)
    assert [c["asin"] for c in extra["narrative"]["choose"]] == ["B0TEST0001", "B0TEST0002", "B0TEST0003"]
    assert set(extra["narrative"]["by_age"]) == {"age-6"}


def test_build_drops_narrative_when_too_few_remain_or_text_is_bad(tmp_path):
    names = ["たまごっちパラダイス Orange", "てんしっちのたまごっち ぴんく", "ドンジャラNEO たまごっち",
             "Original Tamagotchi 初代", "たまごっち 新種発見 ほわいと"]
    records = [rec(f"B0TEST000{i}", n, 4.5 - i * 0.05) for i, n in enumerate(names, start=1)]
    raw_index = {r.asin: raw(r.asin, r.name) for r in records}
    ndir = tmp_path / "n"
    ndir.mkdir()
    n = good_narrative([r.asin for r in records])
    (ndir / "ip-tamagotchi.json").write_text(json.dumps(n, ensure_ascii=False), encoding="utf-8")
    _top, extra = iph.build_ip_payload("ip-tamagotchi", records[:2], raw_index, {},
                                       per_asin_dir=tmp_path / "none", narrative_dir=ndir)
    assert "narrative" not in extra  # 残り 2 件では出さない
    n["lead"] += "今なら3,980円。"
    (ndir / "ip-tamagotchi.json").write_text(json.dumps(n, ensure_ascii=False), encoding="utf-8")
    _top, extra = iph.build_ip_payload("ip-tamagotchi", records, raw_index, {},
                                       per_asin_dir=tmp_path / "none", narrative_dir=ndir)
    assert "narrative" not in extra  # 文章の違反は外さずに落とす


# ---- G1.5 部品 1〜4 (仕様 v2 §13.9) ----
import dataclasses  # noqa: E402

ANPAN = iph.IP_HUBS["ip-anpanman"]


def _axes(r, **axes):
    return dataclasses.replace(r, ivs_axes={"education": 3.0, "longevity": 3.0, "safety": 5.0,
                                            "cost_performance": 5.0, **axes})


def test_axis_best_skips_axes_with_many_ties_and_breaks_ties_by_rank():
    good = [_axes(rec(f"B0{i}", f"商品{i}", ivs=4.0 - i * 0.1)) for i in range(6)]
    good[3] = _axes(good[3], education=4.8)
    good[1] = _axes(good[1], longevity=4.4)
    good[4] = _axes(good[4], longevity=4.4)
    best = {b["axis"]: b for b in iph.axis_best(good)}
    # 安全性・コスパは 6 件とも満点で並ぶので出さない
    assert set(best) == {"education", "longevity"}
    assert best["education"]["asin"] == "B03"
    # 同点は総合の順 (スコアの高い B01) が先
    assert best["longevity"]["asin"] == "B01" and best["longevity"]["ties"] == 2
    assert iph.axis_best(good[:4]) == []  # 少なすぎるページには出さない


def test_voice_of_skips_character_interest_and_keeps_article_text():
    raw_ = {"review_signals": {"summary_one_line": " 音で遊べると好評です。 "},
            "persona_fit": {"recommended_for": ["音あそびが好きな子"],
                            "not_recommended_for": ["アンパンマンに興味がないお子さま",
                                                    "キャラクターものを避けたい方",
                                                    "静かなおもちゃを探している方"]}}
    v = iph.voice_of(raw_, ANPAN)
    assert v == {"voice": "音で遊べると好評です。", "fits": "音あそびが好きな子",
                 "not_for": "静かなおもちゃを探している方"}
    assert iph.voice_of({}, ANPAN) == {}


def test_series_overview_counts_prices_in_stock_and_picks_top_as_rep():
    pool = [rec("B01", "アンパンマン よくばりボックス", ivs=4.2, price=5000),
            rec("B02", "アンパンマン よくばりキューブ", ivs=4.5, price=2000),
            dataclasses.replace(rec("B03", "アンパンマン よくばりバケツ", ivs=3.9, price=900),
                                availability="在庫切れ"),
            rec("B04", "アンパンマン おふろでピッピ", ivs=4.0, price=800)]
    mm = {"B01": 12, "B02": 18, "B03": 0}
    rows = iph.series_overview(pool, ANPAN, mm)
    assert [r["label"] for r in rows] == ["よくばりシリーズ"]  # おふろは 1 件なので出さない
    r = rows[0]
    assert (r["count"], r["age_min"], r["age_max"]) == (3, 0, 18)
    assert (r["price_min"], r["price_max"]) == (2000, 5000)  # 在庫切れの 900 円は入れない
    assert r["rep"]["asin"] == "B02"


def test_annotate_items_reaches_compare_and_narrative_but_not_lineup():
    extra = {"compare_table": {"sections": [{"bands": [{"items": [{"asin": "B01", "url_internal": "/p/"}]}]}]},
             "narrative": {"choose": [{"asin": "B01", "url_internal": "/p/"}]},
             "lineup": [{"asin": "B01", "url_internal": "/p/"}]}
    iph.annotate_items(extra, {"B01": {"voice": "声"}}, {"B01": ["「知育」1位"]})
    row = extra["compare_table"]["sections"][0]["bands"][0]["items"][0]
    assert row["voice"] == "声" and row["badges"] == ["「知育」1位"]
    assert extra["narrative"]["choose"][0]["voice"] == "声"
    assert "voice" not in extra["lineup"][0]


def test_genre_groups_are_for_the_series_table_only():
    # パズル・ブロックは種類のまとまり。間引き (groups=False) では別の商品として扱う
    assert iph.series_of("アンパンマン おべんとうパズル", ANPAN) is None
    assert iph.series_of("アンパンマン おべんとうパズル", ANPAN, groups=True) == "パズル"
    assert iph.series_of("アンパンマン 天才脳つみかさねカップ", ANPAN) == "天才脳シリーズ"
    assert iph.series_of("アンパンマン おふろでピタッと! あいうえお教室", ANPAN, groups=True) == "おふろあそび"
    recs = [rec("B01", "アンパンマン 木のかたちあわせパズル", 4.6), rec("B02", "アンパンマン おべんとうパズル", 4.5),
            rec("B03", "アンパンマン ジグソーパズル", 4.4)]
    assert [r.asin for r in iph.pick_distinct(recs, ANPAN, 3, per_series=1)] == ["B01", "B02", "B03"]
    assert iph.series_of("たまごっちオリジナル パラダイス", TAMA) == "オリジナル・初代"


def test_voice_of_keeps_product_feature_dislikes():
    for keep in ("ブロック遊びに興味がないお子さま", "音の出るおもちゃを避けたい方",
                 "電子音やキャラクターの音声が出ない静かなおもちゃを求めている方"):
        v = iph.voice_of({"persona_fit": {"not_recommended_for": [keep]}}, ANPAN)
        assert v.get("not_for") == keep, keep
    for drop in ("アンパンマン以外のキャラクターを好むお子さま", "キャラクターものを避けたい方",
                 "アンパンマンを知らないお子さま"):
        assert "not_for" not in iph.voice_of({"persona_fit": {"not_recommended_for": [drop]}}, ANPAN), drop


def test_age_range_numbers_are_ages_but_other_numbers_are_not():
    n = good_narrative(ASINS, age_keys=("age-4",))
    n["by_age"][0]["text"] = "4〜5歳になると遊び方が広がります。"
    assert not any("number" in e for e in iph.validate_narrative(n, set(ASINS), {"age-4"}))
    n["by_age"][0]["text"] = "4〜5個のパーツがあります。"
    assert any("number '4'" in e for e in iph.validate_narrative(n, set(ASINS), {"age-4"}))


def test_basic_series_by_price_for_tomica():
    tom = iph.IP_HUBS["ip-tomica"]
    assert iph.series_of_rec(rec("B01", "トミカ No.16 日本交通 タクシー", price=375), tom) == "ベーシックトミカ（1台）"
    assert iph.series_of_rec(rec("B02", "トミカ おかたづけコンボイ", price=2170), tom) is None
    assert iph.series_of_rec(rec("B03", "トミカプレミアム 13 カローラ", price=600), tom) == "トミカプレミアム"
    assert iph.series_of_rec(rec("B04", "トミカ はじめてのパズル", price=600), tom) is None  # 種類のまとまりは間引きに使わない
    assert iph.series_of_rec(rec("B04", "トミカ はじめてのパズル", price=600), tom, groups=True) == "パズル・ぬりえ"


def test_series_uses_name_full_and_basic_skips_accessories():
    tom = iph.IP_HUBS["ip-tomica"]
    r = dataclasses.replace(rec("B01", "トミカ ランボルギーニ ミウラ", price=662),
                            name_full="トミカプレミアム 05 ランボルギーニ ミウラ P400S")
    assert iph.series_of_rec(r, tom) == "トミカプレミアム"
    assert iph.series_of_rec(rec("B02", "トミカ 収納ケース", price=500), tom) is None


def test_exclude_drops_plarail_trains_from_tomica():
    tom = iph.IP_HUBS["ip-tomica"]
    recs = [rec("B01", "トミカ・プラレールブロック タクシー"), rec("B02", "プラレールブロック ドクターイエロー")]
    raws = {"B01": raw("B01", "トミカ・プラレールブロック タクシー"),
            "B02": raw("B02", "トミカ・プラレールブロック ドクターイエロー")}
    pool = iph.select_ip_records(recs, raws, ("トミカ",), tom["exclude"])
    assert [r.asin for r in pool] == ["B01"]


def test_pokenade_pouch_alone_is_accessory():
    pok = iph.IP_HUBS["ip-pokemon"]
    assert iph.is_accessory("ポケなで ピカチュウふれんずポーチ", pok)
    assert not iph.is_accessory("ポケなで モンスターボール ピカチュウふれんずセット", pok)


def test_plarail_strip_keeps_rail_parts_apart_from_trains():
    pla = iph.IP_HUBS["ip-plarail"]
    assert iph.series_of("プラレール R-17 橋直線レール", pla) == "レール・駅・情景部品"
    assert iph.series_of("プラレール DXレールキット", pla) == "レールキット・セット"
    assert iph.series_of("プラレール J-14 ブロック橋脚", pla) == "レール・駅・情景部品"
    assert iph.series_of("トミカ・プラレールブロック ヘリコプター", pla) == "トミカ・プラレールブロック"
    # 「プラレール」の中の「レール」では当たらない
    assert iph.series_of("プラレール 103系総武線", pla) is None
    assert iph.series_of("プラレール 103系総武線", pla, groups=True) == "車両（電車・新幹線）"
    # 「モノレール」の中の「レール」でも部品に入らない (agy 指摘・2026-11-25)
    assert iph.series_of("プラレール 東京モノレール10000形セット", pla, groups=True) == "車両（電車・新幹線）"
    assert iph.series_of("プラレール いっぱいつなごう 金太郎&貨車セット", pla, groups=True) == "車両（電車・新幹線）"


def test_disney_pixar_group_takes_characters_not_mixed_learning_toys():
    dis = iph.IP_HUBS["ip-disney"]
    assert iph.series_of("やのまん リグミー ウッディ＆バズ", dis, groups=True) == "パズル・カードゲーム"
    # 正式名に「ディズニー/ピクサーキャラクターズ」と並ぶ総合の学習玩具はピクサーに入れない
    assert iph.series_of("ディズニー&ディズニー/ピクサーキャラクターズ ワンダフルドリームタッチパソコン", dis,
                         groups=True) is None


def test_disney_lego_before_characters_and_star_wars_excluded():
    dis = iph.IP_HUBS["ip-disney"]
    assert iph.series_of("レゴ エルサのアイスパレス", dis, groups=True) == "レゴ・デュプロ"
    assert iph.series_of("ディズニーモータース ポティロン ベル", dis, groups=True) is None
    recs = [rec("B01", "レゴ レイザー・クレスト 75447"), rec("B02", "レゴ ミッキーマウスのくるま")]
    raws = {"B01": raw("B01", "レゴ スター・ウォーズ レイザー・クレスト", brand="ディズニー"),
            "B02": raw("B02", "レゴ ディズニー ミッキーマウスのくるま")}
    assert [r.asin for r in iph.select_ip_records(recs, raws, ("ディズニー",), dis["exclude"])] == ["B02"]


def test_article_links_to_every_hub_it_is_on_but_respects_exclude():
    import build_post
    assert build_post._ip_hubs_for(raw("B1", "カーズ トミカ でるでるバケツ", brand="ディズニー")) == ["ip-tomica", "ip-disney"]
    # トミカのページから外した電車の商品は、プラレールのページだけへ
    assert build_post._ip_hubs_for(raw("B2", "トミカ・プラレールブロック ドクターイエロー")) == ["ip-plarail"]
    assert build_post._ip_hubs_for(raw("B3", "木のつみき")) == []
