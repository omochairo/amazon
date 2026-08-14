"""需要クエリ → Amazon 検索キーワード変換 (#2686) の検査。"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import build_demand_keywords as B  # noqa: E402

REAL_TERMS = ROOT.parent / "data" / "demand_topic_terms.yaml"


def _terms_file(tmp_path, modifiers=None, excluded=None):
    p = tmp_path / "terms.yaml"
    p.write_text(yaml.safe_dump(
        {"version": 1, "search_modifiers": modifiers or [], "excluded_keywords": excluded or []},
        allow_unicode=True), encoding="utf-8")
    return p


def _mods(tmp_path, modifiers):
    return B.load_vocab(_terms_file(tmp_path, modifiers=modifiers))[0]


# --------------------------------------------------------------------------
# 正規化 (空白除去)
# --------------------------------------------------------------------------

def test_normalize_key_removes_all_spaces():
    """GSC の分かち書き揺れを吸収する。"""
    assert B.normalize_key("はじめて ず かん 1000") == "はじめてずかん1000"
    assert B.normalize_key("トミカ　収納") == "トミカ収納"


def test_spacing_variants_collapse_to_one_keyword(tmp_path):
    """「トミカ 収納」と「トミカ収納」が別集計にならないこと (実データで踏んだ)。"""
    mods = _mods(tmp_path, ["収納ケース"])  # 収納 は商品カテゴリなので落とさない
    assert B.to_search_keyword("トミカ 収納", mods) == B.to_search_keyword("トミカ収納", mods)


def test_modifier_split_by_spaces_is_still_stripped(tmp_path):
    """「危険 性」のように分断された修飾語も落ちること (実データで踏んだ)。"""
    mods = _mods(tmp_path, ["危険性"])
    assert B.to_search_keyword("ジップストリング 危険 性", mods) == "ジップストリング"


# --------------------------------------------------------------------------
# 修飾語の除去
# --------------------------------------------------------------------------

def test_longer_modifier_is_stripped_before_shorter(tmp_path):
    """「どこで売ってる」を先に落とさないと「どこで」が残る。"""
    mods = _mods(tmp_path, ["売ってる", "どこで売ってる"])
    assert B.to_search_keyword("スクイーズ どこで売ってる", mods) == "スクイーズ"


def test_trailing_particle_residue_is_cleaned(tmp_path):
    mods = _mods(tmp_path, ["違い"])
    assert B.to_search_keyword("はじめてずかん1000と1500の違い", mods) == "はじめてずかん1000と1500"


def test_keyword_shorter_than_minimum_is_dropped(tmp_path):
    mods = _mods(tmp_path, ["おすすめ"])
    assert B.to_search_keyword("おすすめ", mods) == ""


def test_product_category_word_is_not_treated_as_modifier():
    """「収納」を落とすとミニカー本体が返り検索意図が変わる。実語彙で守る。"""
    mods, _ = B.load_vocab(REAL_TERMS)
    assert "収納" not in mods
    assert B.to_search_keyword("トミカ 収納 100均 ダイソー", mods) == "トミカ収納"


# --------------------------------------------------------------------------
# 供給ゲート (Amazon に商品が無いもの)
# --------------------------------------------------------------------------

def test_excluded_keyword_is_skipped_with_reason(tmp_path):
    mods, excluded = B.load_vocab(_terms_file(
        tmp_path,
        modifiers=["偽物"],
        excluded=[{"keyword": "メロジョイ", "aliases": ["mellojoy"], "reason": "Amazon 非販売"}],
    ))
    topics = {"rows": [
        {"query": "メロジョイ 偽物", "wp_impressions": 100, "bucket": "toy"},
        {"query": "mellojoy どこの国", "wp_impressions": 50, "bucket": "toy"},
        {"query": "スクイーズ", "wp_impressions": 10, "bucket": "toy"},
    ]}
    rep = B.build(topics, mods, B.build_excluded_terms(excluded), ("toy",))
    assert [k["keyword"] for k in rep["keywords"]] == ["スクイーズ"]
    assert rep["summary"]["excluded_wp_impressions"] == 150
    assert all(e["reason"] for e in rep["excluded"]), "除外は理由つきで記録すること"


def test_alias_of_excluded_keyword_is_matched_after_space_removal(tmp_path):
    mods, excluded = B.load_vocab(_terms_file(
        tmp_path, excluded=[{"keyword": "メロジョイ", "aliases": ["メロン ジョイ"], "reason": "x"}]))
    terms = B.build_excluded_terms(excluded)
    assert "メロンジョイ" in terms


def test_real_vocabulary_excludes_amazon_unavailable_items():
    """owner 確認済みの非販売品・実店舗名が検索語に出ないこと。"""
    mods, excluded = B.load_vocab(REAL_TERMS)
    terms = B.build_excluded_terms(excluded)
    topics = {"rows": [
        {"query": "メロジョイ 偽物", "wp_impressions": 58768, "bucket": "toy"},
        {"query": "おもちゃのバンバン", "wp_impressions": 11300, "bucket": "toy"},
        {"query": "おもちゃ屋さんの倉庫 閉店", "wp_impressions": 917, "bucket": "toy"},
        {"query": "スクイーズ どこで 買える", "wp_impressions": 12010, "bucket": "toy"},
    ]}
    rep = B.build(topics, mods, terms, ("toy",))
    assert [k["keyword"] for k in rep["keywords"]] == ["スクイーズ"]


# --------------------------------------------------------------------------
# 集約
# --------------------------------------------------------------------------

def test_impressions_are_summed_per_keyword_with_source_queries(tmp_path):
    mods = _mods(tmp_path, ["一覧", "種類"])
    topics = {"rows": [
        {"query": "ナーフ 一覧", "wp_impressions": 100, "bucket": "toy"},
        {"query": "ナーフ 種類", "wp_impressions": 60, "bucket": "toy"},
    ]}
    rep = B.build(topics, mods, {}, ("toy",))
    assert len(rep["keywords"]) == 1
    k = rep["keywords"][0]
    assert k["keyword"] == "ナーフ" and k["wp_impressions"] == 160
    assert len(k["source_queries"]) == 2


def test_only_requested_buckets_are_used(tmp_path):
    mods = _mods(tmp_path, [])
    topics = {"rows": [
        {"query": "トミカ", "wp_impressions": 10, "bucket": "toy"},
        {"query": "授乳クッション", "wp_impressions": 999, "bucket": "baby_goods"},
    ]}
    assert [k["keyword"] for k in B.build(topics, mods, {}, ("toy",))["keywords"]] == ["トミカ"]
    both = B.build(topics, mods, {}, ("toy", "baby_goods"))["keywords"]
    assert len(both) == 2


def test_keywords_are_impression_ordered_and_limited(tmp_path):
    mods = _mods(tmp_path, [])
    topics = {"rows": [
        {"query": f"語{i}", "wp_impressions": i, "bucket": "toy"} for i in range(5)
    ]}
    rep = B.build(topics, mods, {}, ("toy",), limit=2)
    assert [k["wp_impressions"] for k in rep["keywords"]] == [4, 3]


def test_summary_counts_only_kept_keywords(tmp_path):
    mods = _mods(tmp_path, [])
    topics = {"rows": [{"query": f"語{i}", "wp_impressions": 10, "bucket": "toy"} for i in range(4)]}
    rep = B.build(topics, mods, {}, ("toy",), limit=2)
    assert rep["summary"]["keywords"] == 2
    assert rep["summary"]["wp_impressions"] == 20


@pytest.mark.parametrize("query,expected", [
    ("スクイーズ どこに売ってる", "スクイーズ"),
    ("ダイヤブロック 生産終了 なぜ", "ダイヤブロック"),
    ("ボーネルンド ルーピング販売終了 理由", "ボーネルンドルーピング"),
    ("ミッケ 難易度 ランキング", "ミッケ"),
    ("こえだちゃん 販売休止 理由", "こえだちゃん"),
])
def test_real_vocabulary_end_to_end(query, expected):
    mods, _ = B.load_vocab(REAL_TERMS)
    assert B.to_search_keyword(query, mods) == expected


# --------------------------------------------------------------------------
# 供給 probe による情報クエリの排除 (owner 指示 2026-08-10)
# --------------------------------------------------------------------------

def _probe_file(tmp_path, results):
    p = tmp_path / "probe.json"
    p.write_text(json.dumps({"results": results}, ensure_ascii=False), encoding="utf-8")
    return p


def test_zero_hit_keywords_are_dropped_with_their_demand_recorded(tmp_path):
    """商品が 1 件も返らない語 = 商品名として通らない情報クエリ。落とす。"""
    probe = _probe_file(tmp_path, [
        {"keyword": "食玩法律", "hits": 0, "error": None},
        {"keyword": "トミカ収納", "hits": 10, "error": None},
    ])
    zero = B.load_zero_supply_keywords(probe)
    topics = {"rows": [
        {"query": "食玩 法律", "wp_impressions": 192, "bucket": "toy"},
        {"query": "トミカ 収納", "wp_impressions": 21365, "bucket": "toy"},
    ]}
    rep = B.build(topics, [], {}, ("toy",), zero_supply=zero)
    assert [k["keyword"] for k in rep["keywords"]] == ["トミカ収納"]
    assert rep["summary"]["dropped_no_supply"] == 1
    assert rep["summary"]["dropped_no_supply_wp_impressions"] == 192
    assert rep["dropped_no_supply"][0]["keyword"] == "食玩法律"


def test_api_error_keywords_are_not_dropped(tmp_path):
    """測れていないだけの語を「供給なし」と混同しない。"""
    probe = _probe_file(tmp_path, [{"keyword": "トミカ収納", "hits": 0, "error": "boom"}])
    assert B.load_zero_supply_keywords(probe) == set()


def test_keywords_absent_from_probe_are_kept(tmp_path):
    """新しい需要は未測定なので通す (probe に無い = 落とす、にしない)。"""
    probe = _probe_file(tmp_path, [{"keyword": "旧い語", "hits": 0, "error": None}])
    zero = B.load_zero_supply_keywords(probe)
    topics = {"rows": [{"query": "新しい商品名", "wp_impressions": 100, "bucket": "toy"}]}
    rep = B.build(topics, [], {}, ("toy",), zero_supply=zero)
    assert [k["keyword"] for k in rep["keywords"]] == ["新しい商品名"]


def test_missing_or_broken_probe_disables_the_filter(tmp_path):
    assert B.load_zero_supply_keywords(None) == set()
    assert B.load_zero_supply_keywords(tmp_path / "nope.json") == set()
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    assert B.load_zero_supply_keywords(bad) == set()


def test_probe_keyword_matching_ignores_spacing(tmp_path):
    """probe 側と build 側で分かち書きが違っても同じ語として扱う。"""
    probe = _probe_file(tmp_path, [{"keyword": "食玩 法律", "hits": 0, "error": None}])
    zero = B.load_zero_supply_keywords(probe)
    topics = {"rows": [{"query": "食玩法律", "wp_impressions": 10, "bucket": "toy"}]}
    rep = B.build(topics, [], {}, ("toy",), zero_supply=zero)
    assert rep["keywords"] == []


def test_real_probe_drops_only_informational_residue():
    """実 probe を当てて、落ちるのが情報クエリだけであることを固定する。"""
    probe = ROOT.parent / "data" / "analytics" / "demand_supply_probe.json"
    if not probe.exists():
        pytest.skip("probe report not present")
    zero = B.load_zero_supply_keywords(probe)
    # 商品名の語が巻き込まれていないこと
    for kw in ["トミカ収納", "スクイーズ", "ジョブレイバー", "知育ボックス", "ナーフ"]:
        assert B.normalize_key(kw) not in zero, f"商品名 {kw} が供給なし扱いになっている"
    # 情報クエリ残骸が落ちていること
    assert B.normalize_key("食玩法律") in zero


# --------------------------------------------------------------------------
# WPランクガード (#2686): WP が既に上位で取っている語のカニバリ防止
# --------------------------------------------------------------------------

def _wp_history_file(tmp_path, rows):
    """実レコード構造 ({"clicks","ctr","date","impressions","position","query"}) の JSONL を書く。"""
    p = tmp_path / "gsc_wp_by_query.jsonl"
    lines = []
    for r in rows:
        rec = {
            "query": r["query"],
            "date": r.get("date", "2026-05-01"),
            "impressions": r["impressions"],
            "clicks": r["clicks"],
            "position": r["position"],
            "ctr": (r["clicks"] / r["impressions"]) if r["impressions"] else 0.0,
        }
        lines.append(json.dumps(rec, ensure_ascii=False))
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def test_position_is_impression_weighted_not_simple_average(tmp_path):
    """日次 position の単純平均と impression 加重平均で結果が変わるケース。

    1日目: imp=1000, pos=1.0 (支配的) / 2日目: imp=10, pos=20.0 (少数の閑散日)。
    単純平均なら (1.0+20.0)/2 = 10.5 (pos_max=3.0 の対象外) だが、
    imp 加重なら (1000*1.0 + 10*20.0)/1010 ≈ 1.19 (pos_max=3.0 の対象内)。
    """
    history = _wp_history_file(tmp_path, [
        {"query": "テスト語", "date": "2026-05-01", "impressions": 1000, "clicks": 200, "position": 1.0},
        {"query": "テスト語", "date": "2026-05-02", "impressions": 10, "clicks": 1, "position": 20.0},
    ])
    stats = B.load_wp_rank_stats(history)
    q = B.normalize_key("テスト語")
    assert stats[q]["imp"] == 1010
    assert stats[q]["clicks"] == 201
    assert abs(stats[q]["pos"] - ((1000 * 1.0 + 10 * 20.0) / 1010)) < 1e-9
    assert stats[q]["pos"] < 3.0  # 単純平均 (10.5) なら閾値を超えて残ってしまう


def test_wp_ranked_keyword_is_dropped_when_pos_and_clicks_both_qualify(tmp_path):
    history = _wp_history_file(tmp_path, [
        {"query": "アンパンマンシール", "impressions": 500, "clicks": 220, "position": 1.1},
    ])
    stats = B.load_wp_rank_stats(history)
    topics = {"rows": [
        {"query": "アンパンマンシール", "wp_impressions": 500, "bucket": "toy"},
    ]}
    rep = B.build(topics, [], {}, ("toy",), wp_rank_stats=stats)
    assert rep["keywords"] == []
    assert rep["summary"]["dropped_wp_ranked"] == 1
    assert rep["summary"]["dropped_wp_ranked_clicks"] == 220
    d = rep["dropped_wp_ranked"][0]
    assert d["keyword"] == "アンパンマンシール"
    assert d["wp_position"] == 1.1
    assert d["wp_clicks"] == 220
    assert d["reason"]


def test_low_clicks_keyword_is_kept_even_if_pos_qualifies(tmp_path):
    """pos<=3.0 でも clicks<100 なら AND 条件を満たさず残る。"""
    history = _wp_history_file(tmp_path, [
        {"query": "低クリック語", "impressions": 50, "clicks": 5, "position": 1.5},
    ])
    stats = B.load_wp_rank_stats(history)
    topics = {"rows": [{"query": "低クリック語", "wp_impressions": 50, "bucket": "toy"}]}
    rep = B.build(topics, [], {}, ("toy",), wp_rank_stats=stats)
    assert [k["keyword"] for k in rep["keywords"]] == ["低クリック語"]
    assert rep["summary"]["dropped_wp_ranked"] == 0


def test_low_position_keyword_is_kept_even_with_many_clicks(tmp_path):
    """clicks>=100 でも pos>3.0 なら AND 条件を満たさず残る (スクイーズ型)。"""
    history = _wp_history_file(tmp_path, [
        {"query": "スクイーズ", "impressions": 18290, "clicks": 500, "position": 11.4},
    ])
    stats = B.load_wp_rank_stats(history)
    topics = {"rows": [{"query": "スクイーズ", "wp_impressions": 18290, "bucket": "toy"}]}
    rep = B.build(topics, [], {}, ("toy",), wp_rank_stats=stats)
    assert [k["keyword"] for k in rep["keywords"]] == ["スクイーズ"]
    assert rep["summary"]["dropped_wp_ranked"] == 0


def test_missing_history_file_fails_open(tmp_path):
    """JSONL 不在時はガードを適用せず従来動作のまま (件数が減らない)。"""
    stats = B.load_wp_rank_stats(tmp_path / "nope.jsonl")
    assert stats == {}
    topics = {"rows": [
        {"query": "アンパンマンシール", "wp_impressions": 500, "bucket": "toy"},
    ]}
    without_guard = B.build(topics, [], {}, ("toy",))
    with_missing_history = B.build(topics, [], {}, ("toy",), wp_rank_stats=stats)
    assert len(with_missing_history["keywords"]) == len(without_guard["keywords"]) == 1
    assert with_missing_history["summary"]["dropped_wp_ranked"] == 0


def test_broken_history_file_fails_open(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text("not json\n", encoding="utf-8")
    assert B.load_wp_rank_stats(bad) == {}


def test_spacing_variant_source_queries_are_not_double_counted(tmp_path):
    """同じ需要語に空白違いの source_query が2件あっても imp/clicks は1件分。

    「ぷにるんず サンリオ」と「ぷにるんずサンリオ」は normalize_key で同じキーに潰れる。
    wp_rank_stats はキー単位で集計済みなので、source_queries 側で重複加算すると
    2倍になる (2026-08-10 実データで発覚した回帰)。
    """
    history = _wp_history_file(tmp_path, [
        {"query": "ぷにるんずサンリオ", "impressions": 2617, "clicks": 1085, "position": 2.1},
    ])
    stats = B.load_wp_rank_stats(history)
    # to_search_keyword で同一キーワードに集約される2つの元クエリ (空白揺れ違い)。
    topics = {"rows": [
        {"query": "ぷにるんず サンリオ", "wp_impressions": 300, "bucket": "toy"},
        {"query": "ぷにるんずサンリオ", "wp_impressions": 200, "bucket": "toy"},
    ]}
    rep = B.build(topics, [], {}, ("toy",), wp_rank_stats=stats)
    assert rep["summary"]["dropped_wp_ranked"] == 1
    d = rep["dropped_wp_ranked"][0]
    assert d["wp_impressions"] == 2617, "2倍 (5234) になっていたら重複加算のバグ"
    assert d["wp_clicks"] == 1085, "2倍 (2170) になっていたら重複加算のバグ"


def test_no_rank_guard_flag_disables_guard_even_with_qualifying_stats(tmp_path):
    history = _wp_history_file(tmp_path, [
        {"query": "アンパンマンシール", "impressions": 500, "clicks": 220, "position": 1.1},
    ])
    stats = B.load_wp_rank_stats(history)
    topics = {"rows": [
        {"query": "アンパンマンシール", "wp_impressions": 500, "bucket": "toy"},
    ]}
    rep = B.build(topics, [], {}, ("toy",), wp_rank_stats=stats, rank_guard_enabled=False)
    assert [k["keyword"] for k in rep["keywords"]] == ["アンパンマンシール"]
    assert rep["summary"]["dropped_wp_ranked"] == 0


# --------------------------------------------------------------------------
# Ubersuggest 由来の需要語の合流 (#2686 PR1)
# --------------------------------------------------------------------------

def _uber_probe_file(tmp_path, results, name="uber_probe.json"):
    p = tmp_path / name
    p.write_text(json.dumps({"results": results}, ensure_ascii=False), encoding="utf-8")
    return p


def _uber_llm_judge_file(tmp_path, results, name="uber_llm.json"):
    p = tmp_path / name
    p.write_text(json.dumps({"results": results}, ensure_ascii=False), encoding="utf-8")
    return p


def test_ubersuggest_probe_product_verdicts_are_kept(tmp_path):
    probe = _uber_probe_file(tmp_path, [
        {"query": "たまごっち", "raw_query": "たまごっち", "volume": 1000000, "sites": ["a.jp"],
         "verdict": "product"},
        {"query": "ねこ道", "raw_query": "ねこ 道", "volume": 500, "sites": ["b.jp"],
         "verdict": "non_product"},
        {"query": "あいまい語", "raw_query": "あいまい語", "volume": 100, "sites": ["c.jp"],
         "verdict": "ambiguous"},
    ], name="probe_only.json")
    kws, missing = B.load_ubersuggest_keywords(probe, tmp_path / "nope_llm.json")
    assert missing == ["llm_judge"]
    assert [k["keyword"] for k in kws] == ["たまごっち"]
    assert kws[0]["source"] == "ubersuggest"
    assert kws[0]["volume"] == 1000000


def test_ubersuggest_llm_judge_is_product_query_true_is_kept(tmp_path):
    llm = _uber_llm_judge_file(tmp_path, [
        {"query": "商品A", "raw_query": "商品 A", "volume": 200, "sites": ["x.jp"],
         "is_product_query": True, "confidence": 0.55},
        {"query": "情報語B", "raw_query": "情報語B", "volume": 300, "sites": ["y.jp"],
         "is_product_query": False, "confidence": 0.99},
    ])
    kws, missing = B.load_ubersuggest_keywords(tmp_path / "nope_probe.json", llm)
    assert missing == ["probe"]
    assert [k["keyword"] for k in kws] == ["商品 A"]


def test_ubersuggest_confidence_is_not_used_for_selection(tmp_path):
    """回帰テスト: is_product_query==true なら confidence の値によらず採用される。

    実測 (2026-08-10): ambiguous 56語中54語が confidence 0.9-1.0 に張り付き、
    その帯の正答率は87%で confidence 自体が判別に効いていない。閾値ゲートを
    入れてはいけない。
    """
    llm = _uber_llm_judge_file(tmp_path, [
        {"query": "低confidence商品", "raw_query": "低confidence商品", "volume": 10,
         "sites": [], "is_product_query": True, "confidence": 0.1},
        {"query": "高confidence非商品", "raw_query": "高confidence非商品", "volume": 10,
         "sites": [], "is_product_query": False, "confidence": 0.99},
    ])
    kws, _ = B.load_ubersuggest_keywords(tmp_path / "nope.json", llm)
    assert [k["keyword"] for k in kws] == ["低confidence商品"]


def test_ubersuggest_dedup_across_probe_and_llm_judge(tmp_path):
    """同じ語が probe (product) と llm_judge (is_product_query) の両方に出ても1件になる。"""
    probe = _uber_probe_file(tmp_path, [
        {"query": "重複語", "raw_query": "重複語", "volume": 50, "sites": ["a.jp"],
         "verdict": "product"},
    ])
    llm = _uber_llm_judge_file(tmp_path, [
        {"query": "重複語", "raw_query": "重複語", "volume": 50, "sites": ["b.jp"],
         "is_product_query": True, "confidence": 0.9},
    ])
    kws, missing = B.load_ubersuggest_keywords(probe, llm)
    assert missing == []
    assert len(kws) == 1
    assert set(kws[0]["sites"]) == {"a.jp", "b.jp"}


def test_ubersuggest_raw_query_with_space_is_used_as_keyword(tmp_path):
    """query は重複排除で空白除去済みなので使わない。raw_query (空白保持) を使う。"""
    probe = _uber_probe_file(tmp_path, [
        {"query": "トミカシール", "raw_query": "トミカ シール", "volume": 10, "sites": [],
         "verdict": "product"},
    ])
    kws, _ = B.load_ubersuggest_keywords(probe, tmp_path / "nope.json")
    assert kws[0]["keyword"] == "トミカ シール"


def test_ubersuggest_missing_both_inputs_fails_open(tmp_path):
    kws, missing = B.load_ubersuggest_keywords(
        tmp_path / "nope_probe.json", tmp_path / "nope_llm.json")
    assert kws == []
    assert set(missing) == {"probe", "llm_judge"}


def test_ubersuggest_broken_input_fails_open(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    kws, missing = B.load_ubersuggest_keywords(bad, tmp_path / "nope_llm.json")
    assert kws == []
    assert "probe" in missing


def test_round_robin_merge_interleaves_by_rank_not_by_value():
    """単位が違う2ソースの数値を混ぜてソートせず、各ソース内の順位を保って交互に出す。"""
    wp = [{"keyword": f"wp{i}"} for i in range(3)]  # 既に降順ソート済みという前提
    uber = [{"keyword": f"uber{i}"} for i in range(5)]
    merged = B.round_robin_merge(wp, uber)
    assert [m["keyword"] for m in merged] == [
        "wp0", "uber0", "wp1", "uber1", "wp2", "uber2", "uber3", "uber4",
    ]


def test_build_merges_wp_and_ubersuggest_via_round_robin(tmp_path):
    mods = _mods(tmp_path, [])
    topics = {"rows": [
        {"query": "WP語1", "wp_impressions": 100, "bucket": "toy"},
        {"query": "WP語2", "wp_impressions": 50, "bucket": "toy"},
    ]}
    uber_kws = [
        {"keyword": "uber語1", "volume": 900000, "sites": [], "source_queries": ["uber語1"],
         "source": "ubersuggest"},
        {"keyword": "uber語2", "volume": 500, "sites": [], "source_queries": ["uber語2"],
         "source": "ubersuggest"},
    ]
    rep = B.build(topics, mods, {}, ("toy",), ubersuggest_keywords=uber_kws)
    # WP は wp_impressions (imp) 降順、Ubersuggest は volume 降順、で交互。
    # volume (900000) が wp_impressions (100) よりずっと大きくても、
    # 数値比較でソートしないので先頭に来るのは wp_impressions 最大の WP語1 のまま。
    # (WP側のkeywordはto_search_keywordのnormalize()でlowercaseされる)
    assert [k["keyword"] for k in rep["keywords"]] == ["wp語1", "uber語1", "wp語2", "uber語2"]
    assert rep["summary"]["ubersuggest_keywords"] == 2
    assert rep["summary"]["wp_keywords"] == 2


def test_ubersuggest_entries_get_wp_rank_guard_too(tmp_path):
    """WP 順位ガードは出所を問わず適用される。"""
    history = _wp_history_file(tmp_path, [
        {"query": "既にWP上位の語", "impressions": 500, "clicks": 220, "position": 1.1},
    ])
    stats = B.load_wp_rank_stats(history)
    topics = {"rows": []}
    uber_kws = [
        {"keyword": "既にWP上位の語", "volume": 1000, "sites": [],
         "source_queries": ["既にWP上位の語"], "source": "ubersuggest"},
    ]
    rep = B.build(topics, [], {}, ("toy",), wp_rank_stats=stats, ubersuggest_keywords=uber_kws)
    assert rep["keywords"] == []
    assert rep["summary"]["ubersuggest_dropped_wp_ranked"] == 1
    assert rep["summary"]["dropped_wp_ranked"] == 1


def test_ubersuggest_missing_inputs_recorded_in_summary(tmp_path):
    topics = {"rows": []}
    rep = B.build(topics, [], {}, ("toy",), ubersuggest_missing_inputs=["probe", "llm_judge"])
    assert rep["summary"]["ubersuggest_missing_inputs"] == ["probe", "llm_judge"]


def test_run_fails_open_when_ubersuggest_inputs_absent(tmp_path):
    """入力ファイルが無いとき run() 全体が従来の WP 70語相当動作のまま壊れないこと。"""
    terms = _terms_file(tmp_path, modifiers=[], excluded=[])
    topics_path = tmp_path / "topics.json"
    topics_path.write_text(json.dumps({"rows": [
        {"query": "語A", "wp_impressions": 10, "bucket": "toy"},
    ]}, ensure_ascii=False), encoding="utf-8")
    out_path = tmp_path / "out.json"
    result = B.run(
        terms, topics_path, out_path, ("toy",), 0, dry_run=True,
        supply_probe_path=None, wp_history_path=tmp_path / "nope_history.jsonl",
        rank_guard_enabled=False,
        ubersuggest_probe_path=tmp_path / "nope_probe.json",
        ubersuggest_llm_judge_path=tmp_path / "nope_llm.json",
    )
    assert [k["keyword"] for k in result["keywords"]] == ["語a"]  # normalize() で lowercase
    assert set(result["summary"]["ubersuggest_missing_inputs"]) == {"probe", "llm_judge"}


# ---------- WP順位履歴の鮮度 (fail-closed, #5107) ----------

def test_stale_wp_history_aborts_instead_of_guarding_on_old_ranks(tmp_path):
    """古いだけのファイルは「壊れている」に入らず正常なガードとして通ってしまう。

    #5107 の実害そのもの: 収集が omcha-ops へ移設された後も public 側の凍結
    スナップショットで判定が続き、出力を見ても気づけない。
    """
    history = _wp_history_file(tmp_path, [
        {"query": "テスト語", "date": "2026-08-04", "impressions": 500, "clicks": 220,
         "position": 1.1},
    ])
    with pytest.raises(SystemExit) as e:
        B.assert_wp_history_fresh(history, 8, today=B.date.fromisoformat("2026-08-25"))
    assert "2026-08-04" in str(e.value)


def test_fresh_wp_history_passes_and_returns_last_date(tmp_path):
    history = _wp_history_file(tmp_path, [
        {"query": "テスト語", "date": "2026-08-04", "impressions": 500, "clicks": 220,
         "position": 1.1},
        {"query": "テスト語", "date": "2026-08-10", "impressions": 400, "clicks": 100,
         "position": 1.2},
    ])
    assert B.assert_wp_history_fresh(
        history, 8, today=B.date.fromisoformat("2026-08-15")) == "2026-08-10"


def test_max_age_zero_disables_the_freshness_check(tmp_path):
    history = _wp_history_file(tmp_path, [
        {"query": "テスト語", "date": "2020-01-01", "impressions": 5, "clicks": 1,
         "position": 1.1},
    ])
    assert B.assert_wp_history_fresh(
        history, 0, today=B.date.fromisoformat("2026-08-15")) is None


def test_absent_history_still_fails_open_not_closed(tmp_path):
    """「無い」は従来どおり fail-open。ここを塞ぐと初回導入時に動かせなくなる。"""
    assert B.assert_wp_history_fresh(
        tmp_path / "nope.jsonl", 8, today=B.date.fromisoformat("2026-08-15")) is None


def test_run_aborts_when_rank_guard_reads_stale_history(tmp_path):
    """run() 経由でも止まること (ガード有効時のみ)。"""
    terms = _terms_file(tmp_path, modifiers=[], excluded=[])
    topics_path = tmp_path / "topics.json"
    topics_path.write_text(json.dumps({"rows": [
        {"query": "語A", "wp_impressions": 10, "bucket": "toy"},
    ]}, ensure_ascii=False), encoding="utf-8")
    history = _wp_history_file(tmp_path, [
        {"query": "語A", "date": "2020-01-01", "impressions": 5, "clicks": 1, "position": 1.1},
    ])
    kwargs = dict(
        supply_probe_path=None, wp_history_path=history,
        ubersuggest_probe_path=None, ubersuggest_llm_judge_path=None,
    )
    with pytest.raises(SystemExit):
        B.run(terms, topics_path, tmp_path / "out.json", ("toy",), 0, dry_run=True, **kwargs)
    # --no-rank-guard なら従来どおり通る
    result = B.run(terms, topics_path, tmp_path / "out.json", ("toy",), 0, dry_run=True,
                   rank_guard_enabled=False, **kwargs)
    assert [k["keyword"] for k in result["keywords"]] == ["語a"]
