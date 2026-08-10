"""Ubersuggest 需要語の Amazon 実査 (観測のみ, #2686 PR-D) の検査。"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import probe_ubersuggest_products as P  # noqa: E402


TOY_NODE = {"id": "1", "name": "おもちゃ", "root": "おもちゃ"}
NON_TOY_NODE = {"id": "999", "name": "文房具", "root": "文房具"}


def _item(asin, title, nodes=None):
    return {
        "asin": asin,
        "itemInfo": {"title": {"displayValue": title}},
        "browseNodeInfo": {"browseNodes": nodes if nodes is not None else [
            {"id": "1", "displayName": "おもちゃ", "ancestor": {"displayName": "おもちゃ"}},
        ]},
    }


def _response(*items):
    """Creators API SearchItems の実レスポンス構造 (searchResult.items)。"""
    return {"searchResult": {"items": list(items)}}


class FakeAPI:
    """search_items を差し替える偽 API。呼ばれた keyword (=raw_query) を記録する。"""

    def __init__(self, responses: dict, raises: dict | None = None):
        self.responses = responses
        self.raises = raises or {}
        self.calls: list[str] = []

    def search_items(self, keywords=None, search_index=None, item_count=None, item_page=None,
                     resources=None):
        self.calls.append(keywords)
        if keywords in self.raises:
            raise self.raises[keywords]
        return self.responses.get(keywords, _response())


def _demand_file(tmp_path, entries):
    p = tmp_path / "ubersuggest_demand.json"
    p.write_text(json.dumps({"keywords": entries}, ensure_ascii=False), encoding="utf-8")
    return p


# --------------------------------------------------------------------------
# raw_query が検索語に使われること (回帰テスト)
# --------------------------------------------------------------------------

def test_raw_query_used_as_search_term_not_query(tmp_path):
    """query は空白除去済みの重複排除キーなので検索語にしてはいけない。"""
    kw = _demand_file(tmp_path, [
        {"query": "保育園シール貼り", "raw_query": "保育園 シール貼り", "volume": 100, "sites": ["a"]},
    ])
    api = FakeAPI({"保育園 シール貼り": _response(_item("B0X", "保育園 シール貼りセット"))})
    P.run(kw, tmp_path / "out.json", 0, "Toys", 10, dry_run=False, api=api, sleeper=lambda s: None)
    assert api.calls == ["保育園 シール貼り"]


def test_query_itself_is_never_sent_to_api(tmp_path):
    kw = _demand_file(tmp_path, [
        {"query": "たまごっちみみっち", "raw_query": "たまごっち みみっち", "volume": 50, "sites": ["a"]},
    ])
    api = FakeAPI({})
    P.run(kw, tmp_path / "out.json", 0, "Toys", 10, dry_run=False, api=api, sleeper=lambda s: None)
    assert "たまごっちみみっち" not in api.calls
    assert api.calls == ["たまごっち みみっち"]


# --------------------------------------------------------------------------
# レスポンス解釈・ジャンル判定
# --------------------------------------------------------------------------

def test_extract_items_reads_real_search_response_shape():
    res = _response(_item("B0REAL0001", "レゴ デュプロ"))
    items = P.extract_items(res)
    assert len(items) == 1
    assert items[0]["asin"] == "B0REAL0001"
    assert items[0]["title"] == "レゴ デュプロ"
    assert items[0]["browse_nodes"] == [{"id": "1", "name": "おもちゃ", "root": "おもちゃ"}]


@pytest.mark.parametrize("response", [
    None, {}, {"items": None}, "文字列",
    {"searchResult": None}, {"searchResult": {}}, {"searchResult": {"items": None}},
])
def test_extract_items_malformed_response_yields_empty_without_raising(response):
    assert P.extract_items(response) == []


def test_flattened_items_shape_still_works():
    res = {"items": [_item("B0FLAT0001", "つみき")]}
    assert P.extract_items(res)[0]["asin"] == "B0FLAT0001"


def test_non_toy_genre_item_not_counted_in_genre_pass_hits(tmp_path):
    """ジャンル外商品が genre_pass_hits に数えられないこと (genre_gate 再利用)。"""
    kw = _demand_file(tmp_path, [
        {"query": "つみき木製", "raw_query": "つみき 木製", "volume": 100, "sites": ["a"]},
    ])
    api = FakeAPI({"つみき 木製": _response(
        _item("B0TOY0001", "つみき 木製セット", nodes=[
            {"id": "1", "displayName": "おもちゃ", "ancestor": {"displayName": "おもちゃ"}}]),
        _item("B0OFF0001", "つみき 木製 文房具", nodes=[
            {"id": "999", "displayName": "文房具", "ancestor": {"displayName": "文房具"}}]),
    )})
    rep = P.run(kw, tmp_path / "out.json", 0, "Toys", 10, dry_run=False, api=api,
               sleeper=lambda s: None)
    r = rep["results"][0]
    assert r["hits"] == 2
    assert r["genre_pass_hits"] == 1


# --------------------------------------------------------------------------
# タイトル照合 / verdict
# --------------------------------------------------------------------------

def test_full_title_overlap_is_product():
    """正当な商品語: クエリの全トークンがタイトルに現れれば product。"""
    coverage = P.compute_title_overlap("レゴ デュプロ", ["レゴ デュプロ はじめてセット"])
    assert coverage == 1.0
    assert P.judge_verdict(hits=1, genre_pass_hits=1, coverage=coverage) == \
        ("product", "full_title_overlap")


def test_tamagotchi_shurui_type_is_ambiguous_not_asserted_non_product():
    """「たまごっち 種類」型: 主題 (たまごっち) はタイトルに現れるが「種類」は
    現れない → 一部一致 (0 < coverage < 1) は ambiguous にする。

    2026-08-10 owner レビュー: 初版は partial を non_product に固定していたが、
    部分一致は表記揺れ・語順違いで一致しなかった実在商品も含みうる
    (「アンパンマン レジスターdx」のような語がタイトル表記の揺れで一部
    トークンだけ一致しない場合と区別できない)。断定材料が無いので non_product
    にも product にも倒さず ambiguous にして owner レビューへ回す。"""
    coverage = P.compute_title_overlap(
        "たまごっち 種類", ["バンダイ たまごっち スペシャルセット", "たまごっち にじいろ"])
    assert 0 < coverage < 1
    assert P.judge_verdict(hits=2, genre_pass_hits=2, coverage=coverage) == \
        ("ambiguous", "partial_title_overlap")


def test_no_overlap_at_all_is_non_product():
    """ジャンルは通ったがタイトルにクエリ語が1つも現れない → 返っているのは
    クエリと無関係な商品だけ、という強いシグナルなので non_product にする。

    2026-08-10 owner レビュー: 初版はここを ambiguous にしていたが、
    「タイトルに1トークンも無い」は判定材料ゼロではなく、むしろ
    non_product 寄りの強いシグナルだという指摘を受けて変更した。"""
    coverage = P.compute_title_overlap("みみっち", ["バンダイ たまごっち スペシャルセット"])
    assert coverage == 0.0
    assert P.judge_verdict(hits=1, genre_pass_hits=1, coverage=coverage) == \
        ("non_product", "zero_title_overlap")


def test_ambiguous_is_a_distinct_value_not_collapsed_into_product_or_non_product():
    """部分一致 (0 < coverage < 1) は product にも non_product にも潰れない
    独立した値であること。"""
    verdict, reason = P.judge_verdict(hits=3, genre_pass_hits=2, coverage=0.5)
    assert verdict == "ambiguous"
    assert verdict not in ("product", "non_product")
    assert reason == "partial_title_overlap"


def test_weak_genre_evidence_is_ambiguous_not_product_or_non_product():
    """coverage=1.0 でも genre_pass_hits/hits が閾値未満なら product と断定
    せず ambiguous (weak_genre_evidence) にする (#2686 案2)。実測:
    「パルクール」hits=10 genre_pass_hits=3 → 0.3 < 0.6。non_product には
    落とさない (LLM 判定に送る、owner 方針)。"""
    verdict, reason = P.judge_verdict(hits=10, genre_pass_hits=3, coverage=1.0)
    assert verdict == "ambiguous"
    assert reason == "weak_genre_evidence"


def test_weak_genre_evidence_threshold_boundary_kidokido():
    """「木戸木戸」実測: hits=10 genre_pass_hits=2 → 0.2 < 0.6 → ambiguous。"""
    verdict, reason = P.judge_verdict(hits=10, genre_pass_hits=2, coverage=1.0)
    assert verdict == "ambiguous"
    assert reason == "weak_genre_evidence"


def test_strong_genre_evidence_at_or_above_threshold_stays_product():
    """genre_pass_hits/hits が閾値以上なら従来どおり product のまま。"""
    verdict, reason = P.judge_verdict(hits=10, genre_pass_hits=6, coverage=1.0)
    assert verdict == "product"
    assert reason == "full_title_overlap"


def test_weak_genre_evidence_never_falls_to_non_product():
    """coverage=1.0・genre_pass_hits が非常に低くても non_product には
    落ちないこと (判断は LLM に送る、機械側で無理に減らさない)。"""
    verdict, _ = P.judge_verdict(hits=10, genre_pass_hits=1, coverage=1.0)
    assert verdict != "non_product"


def test_zero_hits_is_non_product():
    assert P.judge_verdict(hits=0, genre_pass_hits=0, coverage=0.0) == \
        ("non_product", "no_hits")


def test_hits_but_no_genre_pass_is_non_product():
    assert P.judge_verdict(hits=3, genre_pass_hits=0, coverage=0.0) == \
        ("non_product", "no_genre_pass")


def test_end_to_end_verdict_via_probe_keyword_tamagotchi_shurui():
    api = FakeAPI({"たまごっち 種類": _response(
        _item("B0T0001", "バンダイ たまごっち スペシャルセット"),
    )})
    r = P.probe_keyword(api, "たまごっち種類", "たまごっち 種類", 18100, ["czech.hatenablog.com"],
                        "Toys", 10)
    assert r["verdict"] == "ambiguous"
    assert r["verdict_reason"] == "partial_title_overlap"


def test_end_to_end_verdict_via_probe_keyword_zero_overlap_is_non_product():
    api = FakeAPI({"みみっち": _response(
        _item("B0M0001", "バンダイ たまごっち スペシャルセット"),
    )})
    r = P.probe_keyword(api, "みみっち", "みみっち", 74000, ["czech.hatenablog.com"], "Toys", 10)
    assert r["verdict"] == "non_product"
    assert r["verdict_reason"] == "zero_title_overlap"


def test_end_to_end_verdict_via_probe_keyword_legit_product():
    api = FakeAPI({"レゴ デュプロ": _response(
        _item("B0L0001", "レゴ デュプロ はじめてのブロックセット"),
    )})
    r = P.probe_keyword(api, "レゴデュプロ", "レゴ デュプロ", 20000, ["toysrus"], "Toys", 10)
    assert r["verdict"] == "product"


def test_sample_titles_keeps_all_hits_not_just_first_five():
    """sample_titles は先頭5件でなく全件 (最大 item_count 件) を保存すること。

    5件しか保存していなかったため、実データ200語の --recompute で
    「ウッディー」「玩具」が product → non_product に後退した。原因は
    coverage=1.0 を成立させた一致タイトルが6〜10件目にあり、再採点では
    見えなくなっていたこと。同じ情報欠落は後続 (#2686 案2) のローカル
    LLM 判定でも起きる (LLM に5件しか渡せなければ LLM も同じ誤判定をする)。
    """
    api = FakeAPI({"トミカ": _response(
        *[_item(f"B0T{i:04d}", f"タカラトミー トミカ No.{i} ミニカー") for i in range(10)]
    )})
    r = P.probe_keyword(api, "トミカ", "トミカ", 368000, ["takaratomymall.jp"], "Toys", 10)
    assert r["hits"] == 10
    assert len(r["sample_titles"]) == 10
    # 6件目以降が確かに残っている (先頭5件打ち切りなら消えていた分)
    assert "タカラトミー トミカ No.9 ミニカー" in r["sample_titles"]


# --------------------------------------------------------------------------
# 形態素解析ベースのタイトル照合 (#2686 PR-E)
# --------------------------------------------------------------------------

def test_word_order_reversed_katakana_compound_still_matches():
    """中心的な回帰テスト: 「プレミアムトミカ」(Volume 40,500) は実タイトルが
    「トミカプレミアム」と語順が逆なだけの複合語 (実データ確認済み)。
    空白区切りの部分文字列一致では不一致になっていたが (実測 title_overlap
    =0.0 → non_product)、内容語の集合一致 (語順不問) なら一致するはず。"""
    coverage = P.compute_title_overlap(
        "プレミアムトミカ",
        ["タカラトミー(TAKARA TOMY) トミカプレミアム 05 ランボルギーニ ミウラ "
         "P400S ミニカー おもちゃ 6歳以上"],
    )
    assert coverage == 1.0
    assert P.judge_verdict(hits=10, genre_pass_hits=10, coverage=coverage) == \
        ("product", "full_title_overlap")


def test_rikachan_ningyo_recovers_to_product():
    """「リカちゃん人形」(Volume 40,500) も実在商品。旧ロジックでは
    non_product に落ちていた語が新ロジックで回復すること。"""
    coverage = P.compute_title_overlap(
        "リカちゃん人形",
        ["タカラトミー リカちゃん 人形セット おもちゃ 3歳以上"],
    )
    assert coverage == 1.0
    assert P.judge_verdict(hits=1, genre_pass_hits=1, coverage=coverage) == \
        ("product", "full_title_overlap")


def test_particles_and_symbols_excluded_from_content_words():
    """助詞・記号は内容語 (coverage の分母) に含まれないこと。"""
    words = P.content_words("これはトミカのミニカーです。")
    assert "は" not in words
    assert "の" not in words
    assert "です" not in words
    assert "。" not in words
    assert "トミカ" in words
    assert "ミニカー" in words


def test_lone_digit_excluded_from_content_words():
    """単独の数字 (品詞細分類「数」) は内容語から除外されること。"""
    words = P.content_words("トミカ 05")
    assert "05" not in words
    assert "トミカ" in words


def test_tamagotchi_shurui_still_partial_with_tokenizer_based_overlap():
    """「たまごっち 種類」型: タイトルに現れない内容語 (「種類」) があれば
    coverage < 1 のままであること (誤って救済しないことの確認)。"""
    coverage = P.compute_title_overlap(
        "たまごっち 種類", ["バンダイ たまごっち スペシャルセット", "たまごっち にじいろ"])
    assert 0 < coverage < 1
    assert P.judge_verdict(hits=2, genre_pass_hits=2, coverage=coverage) == \
        ("ambiguous", "partial_title_overlap")


def test_split_katakana_blob_finds_dictionary_anchor():
    """_split_katakana_blob が未知語丸呑みトークンを辞書アンカー
    (「プレミアム」) 経由で分割できること。"""
    assert P.content_words("プレミアムトミカ") == ["プレミアム", "トミカ"]
    assert P.content_words("トミカプレミアム") == ["トミカ", "プレミアム"]


def test_nakaguro_separator_does_not_glue_words_together():
    """中黒「・」は片仮名の Unicode ブロックに含まれるため、未知語処理の
    「同じ文字種の連続」丸呑みに巻き込まれて「マグ・フォーマー」ごと
    1トークンになってしまう不具合が実データ再採点で発覚した (「マグ
    フォーマー」で overlap が誤って 0.0 になった)。中黒は語の区切りとして
    扱い、中黒を挟んだ側の「マグ」が独立した内容語として取り出せること。"""
    words = P.content_words("マグ・フォーマー")
    assert "マグ" in words
    assert "・" not in "".join(words)


def test_katakana_missplit_is_symmetric_and_harmless():
    """_split_katakana_blob は既知語アンカーを貪欲に切り出すため、固有名詞を
    誤分割する (実測: ベイブレード→[ベイ, ブレード] / ボーネルンド→
    [ボーネ, ルンド] / トランスフォーマー→[トランス, フォーマー])。

    これが実害にならないのは compute_title_overlap が **クエリ側とタイトル側の
    両方に同じ content_words をかけている**ため。誤分割は対称に起きるので
    断片同士が一致し、coverage は正しく 1.0 になる。この対称性が壊れると
    (片側だけ分割方法を変える等) 誤分割が直ちに誤判定になるので、性質を
    テストで固定しておく。
    """
    assert P.content_words("ベイブレードx") == ["ベイ", "ブレード", "x"]
    assert P.content_words("ボーネルンド") == ["ボーネ", "ルンド"]
    # 誤分割していても、タイトル側が同じ分割を受けるので一致する
    assert P.compute_title_overlap(
        "ベイブレードx", ["タカラトミー ベイブレードX スターター ドランザースパイラル"]
    ) == 1.0
    assert P.compute_title_overlap(
        "ボーネルンド", ["ボーネルンド ルーピング フリズル 知育玩具"]
    ) == 1.0


def test_split_katakana_blob_gives_up_without_anchor():
    """辞書アンカーが見つからない片仮名の連続は分割を諦めて1語のまま返す
    こと (過分割を避ける側に倒す設計の確認)。"""
    # 短すぎて分割対象にならない (閾値未満)
    assert P.content_words("トミ") == ["トミ"]


# --------------------------------------------------------------------------
# --recompute (API を呼ばないオフライン再採点、#2686 PR-E)
# --------------------------------------------------------------------------

def _probe_json_entry(**overrides):
    base = {
        "query": "存在しない語", "raw_query": "存在しない 語", "volume": 1,
        "sites": ["a"], "error": None, "hits": 0, "genre_pass_hits": 0,
        "title_overlap": 0.0, "verdict": "non_product", "verdict_reason": "no_hits",
        "sample_titles": [],
    }
    base.update(overrides)
    return base


def _write_probe_json(path, results):
    path.write_text(json.dumps({
        "generated_at": "2026-08-01T00:00:00Z",
        "params": {"limit": 200, "search_index": "Toys", "item_count": 10},
        "summary": {"keywords_probed": len(results)},
        "results": results,
    }, ensure_ascii=False), encoding="utf-8")


def test_recompute_does_not_call_network(tmp_path, monkeypatch):
    """--recompute は API クライアントを一切 import/呼び出ししないこと。"""
    import builtins
    orig_import = builtins.__import__

    def _blocking_import(name, *args, **kwargs):
        if name in ("creators_api_client", "requests"):
            raise AssertionError(f"{name} must not be imported by --recompute")
        return orig_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocking_import)
    src = tmp_path / "probe.json"
    _write_probe_json(src, [_probe_json_entry()])
    report = P.recompute_verdicts(src)
    assert report["summary_after"]["non_product"] + report["summary_after"]["product"] \
        + report["summary_after"]["ambiguous"] == 1


def test_recompute_has_before_and_after_verdicts_and_recovers_premium_tomica(tmp_path):
    """新旧 verdict が両方入っていること。旧ロジックで non_product だった
    「プレミアムトミカ」が sample_titles だけで再採点しても product に
    回復すること。"""
    entries = [
        _probe_json_entry(
            query="プレミアムトミカ", raw_query="プレミアムトミカ", volume=40500,
            hits=10, genre_pass_hits=10, title_overlap=0.0, verdict="non_product",
            verdict_reason="zero_title_overlap",
            sample_titles=[
                "タカラトミー(TAKARA TOMY) トミカプレミアムunlimited 世界の名車 "
                "日産 スカイラインGT-R (BNR32) (初期 色) ミニカー おもちゃ 6歳以上",
                "タカラトミー(TAKARA TOMY) トミカプレミアム 05 ランボルギーニ "
                "ミウラ P400S ミニカー おもちゃ 6歳以上",
            ],
        ),
    ]
    src = tmp_path / "probe.json"
    _write_probe_json(src, entries)
    report = P.recompute_verdicts(src)
    r = report["results"][0]
    assert r["verdict_before"] == "non_product"
    assert r["verdict_after"] == "product"
    assert r["title_overlap_before"] == 0.0
    assert r["title_overlap_after"] == 1.0
    assert report["changed_count"] == 1
    assert report["summary_before"]["non_product"] == 1
    assert report["summary_after"]["product"] == 1


def test_recompute_reads_legacy_five_title_format(tmp_path):
    """旧フォーマット (sample_titles が先頭5件だけ) の JSON も再採点できること。

    data/analytics/ubersuggest_product_probe.json (#4894 でマージ済み) は
    5件保存で生成されている。probe_keyword を全件保存に変えた後も、この
    既存ファイルを --recompute で読めなくなってはいけない。
    """
    src = tmp_path / "probe.json"
    _write_probe_json(src, [_probe_json_entry(
        query="プレミアムトミカ", raw_query="プレミアムトミカ", volume=40500,
        hits=10, genre_pass_hits=10, title_overlap=0.0,
        verdict="non_product", verdict_reason="zero_title_overlap",
        # hits=10 なのに 5 件しか無い = 旧フォーマット
        sample_titles=[
            f"タカラトミー(TAKARA TOMY) トミカプレミアム {i:02d} ミニカー おもちゃ 6歳以上"
            for i in range(5)
        ],
    )])
    report = P.recompute_verdicts(src)
    row = report["results"][0]
    assert len(row["sample_titles"]) == 5
    assert row["verdict_before"] == "non_product"
    assert row["verdict_after"] == "product"


def test_recompute_leaves_api_error_entries_unchanged(tmp_path):
    """api_error の語は hits=0 のまま judge_verdict に通すと no_hits に誤って
    倒れてしまうので、再採点せず verdict_before をそのまま保持すること。"""
    entries = [_probe_json_entry(
        query="壊れる語", raw_query="壊れる 語", volume=10, error="RuntimeError: boom",
        hits=0, genre_pass_hits=0, title_overlap=0.0, verdict="ambiguous",
        verdict_reason="api_error", sample_titles=[],
    )]
    src = tmp_path / "probe.json"
    _write_probe_json(src, entries)
    report = P.recompute_verdicts(src)
    r = report["results"][0]
    assert r["verdict_before"] == r["verdict_after"] == "ambiguous"
    assert r["verdict_reason_after"] == "api_error"
    assert r["changed"] is False


def test_recompute_handles_zero_sample_titles_without_raising(tmp_path):
    """sample_titles が0件 (実データに9語ある) でも例外にならないこと。"""
    entries = [_probe_json_entry(
        query="供給ゼロ語", raw_query="供給 ゼロ語", volume=5, hits=0, genre_pass_hits=0,
        title_overlap=0.0, verdict="non_product", verdict_reason="no_hits", sample_titles=[],
    )]
    src = tmp_path / "probe.json"
    _write_probe_json(src, entries)
    report = P.recompute_verdicts(src)
    r = report["results"][0]
    assert r["verdict_after"] == "non_product"
    assert r["verdict_reason_after"] == "no_hits"


def test_recompute_out_file_written(tmp_path):
    """CLI 経由でも動くこと (ネットワークに出ずファイルを書く)。"""
    import subprocess
    src = tmp_path / "probe.json"
    out = tmp_path / "recompute.json"
    _write_probe_json(src, [_probe_json_entry()])
    script = pathlib.Path(P.__file__).resolve()
    subprocess.run(
        [sys.executable, str(script), "--recompute", str(src), "--recompute-out", str(out)],
        check=True, cwd=str(script.parent.parent), timeout=30,
    )
    assert out.exists()
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["changed_count"] == 0
    assert "summary_before" in written and "summary_after" in written


# --------------------------------------------------------------------------
# API 例外 / dry-run / limit
# --------------------------------------------------------------------------

def test_api_exception_is_captured_per_keyword_and_verdict_is_ambiguous(tmp_path):
    kw = _demand_file(tmp_path, [
        {"query": "壊れる語", "raw_query": "壊れる 語", "volume": 10, "sites": ["a"]},
        {"query": "次の語", "raw_query": "次の 語", "volume": 9, "sites": ["a"]},
    ])
    api = FakeAPI({"次の 語": _response(_item("B0N0001", "次の 語 商品"))},
                 raises={"壊れる 語": RuntimeError("boom")})
    rep = P.run(kw, tmp_path / "out.json", 0, "Toys", 10, dry_run=False, api=api,
               sleeper=lambda s: None)
    assert len(rep["results"]) == 2, "1 語の失敗で全体を止めない"
    failed = [r for r in rep["results"] if r["query"] == "壊れる語"][0]
    assert failed["error"].startswith("RuntimeError")
    assert failed["verdict"] == "ambiguous"
    assert failed["verdict_reason"] == "api_error"
    ok = [r for r in rep["results"] if r["query"] == "次の語"][0]
    assert ok["error"] is None


def test_dry_run_makes_no_api_calls(tmp_path):
    kw = _demand_file(tmp_path, [
        {"query": "たまごっちみみっち", "raw_query": "たまごっち みみっち", "volume": 50, "sites": ["a"]},
    ])
    api = FakeAPI({})
    out = tmp_path / "out.json"
    rep = P.run(kw, out, 0, "Toys", 10, dry_run=True, api=api, sleeper=lambda s: None)
    assert api.calls == []
    assert not out.exists()
    assert rep["targets"][0]["raw_query"] == "たまごっち みみっち"


def test_limit_caps_targets_and_api_calls(tmp_path):
    kw = _demand_file(tmp_path, [
        {"query": f"語{i}", "raw_query": f"語 {i}", "volume": 100 - i, "sites": ["a"]}
        for i in range(5)
    ])
    api = FakeAPI({})
    rep = P.run(kw, tmp_path / "out.json", 2, "Toys", 10, dry_run=False, api=api,
               sleeper=lambda s: None)
    assert len(rep["results"]) == 2
    assert api.calls == ["語 0", "語 1"]


def test_targets_are_ordered_by_volume_desc(tmp_path):
    kw = _demand_file(tmp_path, [
        {"query": "小さい", "raw_query": "小さい", "volume": 10, "sites": ["a"]},
        {"query": "大きい", "raw_query": "大きい", "volume": 9999, "sites": ["a"]},
    ])
    api = FakeAPI({})
    rep = P.run(kw, tmp_path / "out.json", 0, "Toys", 10, dry_run=False, api=api,
               sleeper=lambda s: None)
    assert [r["query"] for r in rep["results"]] == ["大きい", "小さい"]


def test_sleep_between_calls_but_not_after_last(tmp_path):
    kw = _demand_file(tmp_path, [
        {"query": f"語{i}", "raw_query": f"語{i}", "volume": 1, "sites": ["a"]} for i in range(3)
    ])
    slept: list[float] = []
    P.run(kw, tmp_path / "out.json", 0, "Toys", 10, dry_run=False, api=FakeAPI({}),
         sleeper=slept.append)
    assert slept == [P.SLEEP_SECONDS, P.SLEEP_SECONDS]


# --------------------------------------------------------------------------
# 観測専用であること (raw/articles を一切触らない)
# --------------------------------------------------------------------------

def test_run_never_touches_amazon_raw_pool(tmp_path, monkeypatch):
    """data/raw/amazon.json を書かないこと。カレントディレクトリを見ない設計
    なので、cwd を tmp_path に切り替えても data/raw が作られないことで担保する。"""
    monkeypatch.chdir(tmp_path)
    kw = _demand_file(tmp_path, [
        {"query": "つみき", "raw_query": "つみき", "volume": 10, "sites": ["a"]},
    ])
    api = FakeAPI({"つみき": _response(_item("B0X", "つみき"))})
    P.run(kw, tmp_path / "out" / "probe.json", 0, "Toys", 10, dry_run=False, api=api,
         sleeper=lambda s: None)
    assert not (tmp_path / "data").exists()


# --------------------------------------------------------------------------
# ネットワークに出ないこと
# --------------------------------------------------------------------------

def test_module_does_not_import_network_libraries_at_call_time(tmp_path, monkeypatch):
    """dry-run / FakeAPI 経路では requests 等の実 HTTP レイヤーが呼ばれないこと。
    creators_api_client を遅延 import している設計を、api 未指定 dry-run では
    import すら発生しないことで確認する。"""
    import builtins
    orig_import = builtins.__import__
    blocked = []

    def _blocking_import(name, *args, **kwargs):
        if name == "creators_api_client":
            blocked.append(name)
            raise AssertionError("creators_api_client must not be imported in --dry-run")
        return orig_import(name, *args, **kwargs)

    kw = _demand_file(tmp_path, [
        {"query": "つみき", "raw_query": "つみき", "volume": 10, "sites": ["a"]},
    ])
    monkeypatch.setattr(builtins, "__import__", _blocking_import)
    rep = P.run(kw, tmp_path / "unused.json", 0, "Toys", 10,
               dry_run=True, api=None, sleeper=lambda s: None)
    assert blocked == []
    assert rep["summary"]["keywords_probed"] == 0
