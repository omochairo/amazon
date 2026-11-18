"""faq_merge.merge_faq: 記事の faq と faq_extended を 1 本にする (#11865)。"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from faq_merge import MAX_MERGED_FAQ_ITEMS, merge_faq  # noqa: E402


def qa(q: str, a: str = "答えの本文です。十分な長さの回答。") -> dict:
    return {"question": q, "answer": a}


NAME = ["アイウ パズル ブロック"]


def test_article_only_and_extended_only_pass_through():
    assert merge_faq([qa("発売日はいつですか？")], None, NAME) == [qa("発売日はいつですか？")]
    assert merge_faq(None, [qa("素材は何ですか？")], NAME) == [qa("素材は何ですか？")]
    assert merge_faq(None, None, NAME) == []
    assert merge_faq("bad", {"x": 1}, NAME) == []


def test_extended_first_then_article_only_additions():
    ext = [qa("素材は何ですか？"), qa("お手入れの方法は？")]
    art = [qa("発売日と定価はいくらですか？"), qa("収納サイズは？")]
    stats: dict = {}
    out = merge_faq(art, ext, NAME, stats=stats)
    assert [x["question"] for x in out] == [
        "素材は何ですか？", "お手入れの方法は？", "発売日と定価はいくらですか？", "収納サイズは？",
    ]
    assert stats == {"dup_dropped": 0, "cap_dropped": 0, "added": 2, "dynamic_dropped": 0}


def test_exact_duplicate_dropped_and_extended_wins():
    ext = [qa("素材は何ですか？", "拡張側の答え。十分な長さがあります。")]
    art = [qa("素材は 何ですか？", "記事側の答え。十分な長さがあります。")]
    stats: dict = {}
    out = merge_faq(art, ext, NAME, stats=stats)
    assert len(out) == 1 and out[0]["answer"].startswith("拡張側")
    assert stats["dup_dropped"] == 1


def test_product_name_prefix_difference_is_duplicate():
    ext = [qa("アイウ パズル ブロックは電池交換できますか？")]
    art = [qa("ブロックは電池交換できますか？")]
    assert len(merge_faq(art, ext, NAME)) == 1


def test_same_intent_paraphrase_is_duplicate():
    ext = [qa("アイウ パズルは何歳から遊べますか？")]
    art = [qa("対象年齢はいつからですか？")]
    assert len(merge_faq(art, ext, NAME)) == 1
    ext = [qa("どこで購入できますか？")]
    art = [qa("どこで買うのが一番お得ですか？")]
    assert len(merge_faq(art, ext, NAME)) == 1


def test_different_questions_are_kept():
    ext = [qa("洗えますか？")]
    art = [qa("兄弟で一緒に遊べますか？")]
    assert len(merge_faq(art, ext, NAME)) == 2


def test_extended_internal_items_are_never_dropped():
    ext = [qa("何歳から遊べますか？"), qa("対象年齢の目安は？")]
    assert len(merge_faq(None, ext, NAME)) == 2


def test_cap_applies_after_extended():
    ext = [qa(q) for q in ("素材は何ですか", "洗えますか", "収納サイズは", "保証はありますか", "兄弟で遊べますか", "音は大きいですか")]
    art = [qa(q) for q in ("箱の大きさは", "誕生日に向きますか", "壊れやすいですか", "組み立ては必要ですか", "重さはどのくらい", "色は選べますか")]
    stats: dict = {}
    out = merge_faq(art, ext, NAME, stats=stats)
    assert len(out) == MAX_MERGED_FAQ_ITEMS
    assert [x["question"] for x in out[:6]] == [x["question"] for x in ext]
    assert stats["dup_dropped"] == 0
    assert stats["cap_dropped"] == 12 - MAX_MERGED_FAQ_ITEMS


def test_inputs_not_mutated():
    ext = [qa("素材は何ですか？")]
    art = [qa("素材は何ですか？"), qa("収納サイズは？")]
    merge_faq(art, ext, NAME)
    assert len(ext) == 1 and len(art) == 2


def test_malformed_items_skipped():
    out = merge_faq([{"question": "", "answer": "x"}, "str", qa("収納サイズは？")], None, NAME)
    assert out == [qa("収納サイズは？")]


def test_dynamic_fact_in_article_item_dropped_only_when_extended_exists():
    price = qa("どこで買うのが一番安いですか？", "本記事執筆時点ではAmazonが1,980円で最安値です。")
    other = qa("収納サイズは？")
    stats: dict = {}
    out = merge_faq([price, other], [qa("素材は何ですか？")], NAME, stats=stats)
    assert [x["question"] for x in out] == ["素材は何ですか？", "収納サイズは？"]
    assert stats["dynamic_dropped"] == 1
    # faq_extended の無い記事は従来どおりそのまま出る
    assert merge_faq([price, other], None, NAME) == [price, other]


def test_long_vs_short_product_name_is_duplicate():
    long_name = "ルミ タイマー 勉強 子供 学習 知育"
    ext = [qa("ルミ タイマーの音はうるさくないですか？")]
    art = [qa(f"{long_name}の音はうるさくないですか？"), qa(f"{long_name}は洗えますか？")]
    out = merge_faq(art, ext, [])
    assert [x["question"] for x in out] == [ext[0]["question"], art[1]["question"]]


def test_article_internal_paraphrases_are_not_deduped_without_extended():
    art = [qa("何歳から遊べますか？"), qa("対象年齢の目安は？")]
    assert merge_faq(art, None, NAME) == art


def test_article_only_faq_is_not_capped():
    art = [qa(f"質問{chr(0x3042 + i * 2)}についてのくわしい説明は") for i in range(12)]
    assert len(merge_faq(art, None, NAME)) == 12


def test_battery_replacement_is_not_same_as_battery_needed():
    ext = [qa("電池は必要ですか？")]
    art = [qa("電池の交換方法は？")]
    assert len(merge_faq(art, ext, NAME)) == 2


def test_release_date_and_list_price_are_separate_intents():
    ext = [qa("発売日はいつですか？")]
    art = [qa("定価はいくらですか？", "メーカー希望の販売額はありません。")]
    # 「定価」は動的事実ではない (価格 の語を含まない) ので足される
    assert len(merge_faq(art, ext, NAME)) == 2
