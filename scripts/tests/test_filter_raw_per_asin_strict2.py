"""filter_raw_per_asin.filter_items の strict=2 (youtube/news の商品同定判定)。

裏付け語なし ASIN 経路 (2026-09-24) の真陽性と、§4.7 (2026-07-07) で strict=2 を
入れた理由である「同ブランド/同ライン別商品」の陰性を並べて固定する。
タイトルはいずれも本番の per_asin raw / ターゲットから採った実物。
"""

import json
import sys
from collections import defaultdict
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import filter_raw_per_asin as frpa  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _passes(target: str, video: str, shared: set | None = None,
            with_title: bool = True) -> bool:
    # allows_title_only_path (#8162 案B/C) は ASIN の raw プール全体で判定する
    # ゲートで、ここでの目的 (bounded な断片一致そのものの単体テスト) とは
    # 別の関心事なので常に通す。ゲート自体のテストは fixture を使う下のブロック。
    brands, series = frpa.extract_brand_series(target)
    model = frpa.extract_model_number(target)
    tokens = frpa.tokenize(target)
    terms = frpa.extract_product_terms(target, brands, series)
    with mock.patch.object(frpa, "allows_title_only_path", return_value=True):
        return bool(frpa.filter_items(
            [{"title": video, "url": "u"}], brands, series, model, tokens, terms,
            ["title"], top_n=1, strict=2, shared_terms=shared or set(),
            asin_title=target if with_title else ""))


KNOWN_POSITIVES = [
    ("きらめく財宝", "おうちでボードゲーム#5『きらめく財宝』"),
    ("きらめく財宝", "最近ハマってる☺️きらめく財宝  #shorts"),
    # 12 字で切れた product_term (ミュウミュウおすわりぬい) は部分一致しない。
    # 文字種の断片を順に拾う経路で当たる。
    ("ミュウミュウおすわりぬいぐるみ",
     "BabyBusミュウミュウのふわふわおすわりぬいぐるみ"),
    ("クラッシュアイスゲーム", "皆知ってる『クラッシュアイスゲーム』今更やっても面白い説"),
    ("NEW OH!寿司ゲーム", "お寿司を積んで、はらはらドキドキバランスゲーム！「NEW OH！寿司ゲーム」"),
    ("金庫破りのジギ", "フクハナのボードゲーム紹介 No.123：金庫破りのジギ"),
    ("やみつきボックス+", "生後9ヶ月がやみつきボックス＋に夢中！✨知育玩具レビュー"),
]


@pytest.mark.parametrize("target,video", KNOWN_POSITIVES)
def test_brandless_target_passes_matching_video(target, video):
    assert _passes(target, video)


def test_without_asin_title_keeps_previous_behaviour():
    # asin_title を渡さない呼び出し (news / 既存の one-off) は従来どおり落とす
    assert not _passes("きらめく財宝", "おうちでボードゲーム#5『きらめく財宝』",
                       with_title=False)


KNOWN_NEGATIVES = [
    # §4.7 の実測 (2026-07-07): franchise + カテゴリの共有語だけで別商品が通った
    ("ナノブロック ポケットモンスター リザードン",
     "ナノブロック ポケットモンスター カビゴン 作ってみた",
     {"ナノブロック", "ポケットモンスター"}),
    ("アンパンマン あそびがいっぱいよくばりバケツ",
     "アンパンマン わくわくのりものブロックバケツで遊ぶ", set()),
    ("GraviTrax POWER スターターセット",
     "GraviTrax POWER エレベーター 紹介", {"GraviTrax", "POWER"}),
    # 裏付け語なし経路で塞ぐ同ライン別商品 (2026-09-24 の per_asin raw から)
    ("はじめてのブロック", "【PV】はじめてのブロックワゴン　ブロックラボ", set()),
    ("エヴァンゲリオンプライム2号機",
     "【シナジネクス】トランスフォーマー×エヴァンゲリオンコラボ　"
     "エヴァンゲリオンプライム初号機 商品紹介PV", set()),
    ("25音 カラフル鉄琴", "カラフル鉄琴 20音（半音付き） #おもちゃ #楽器", set()),
    ("ばいきんまんブロックバケツ",
     "BlockLabo ばいきんまんのくるくるメカ工場ブロックバケツ！ トイキッズ", set()),
    ("やみつきボックス", "トイローヤル たのしく知育! やみつきボックス+ ( 知育玩具 )",
     set()),
    ("LaQ ブルーインパルス", "ブルーインパルス 展示飛行 松島基地", set()),
    ("キッズドラムセット", "キッズドラムの発表会を成功させたい!!", set()),
]


@pytest.mark.parametrize("target,video,shared", KNOWN_NEGATIVES)
def test_same_line_other_product_stays_rejected(target, video, shared):
    assert not _passes(target, video, shared)
    assert not _passes(target, video, shared, with_title=False)


def test_target_with_corroborator_does_not_use_title_path():
    # 共有語 (GraviTrax) を持つ ASIN は従来経路のまま: 固有語 1 語だけでは足りない
    assert not _passes("GraviTrax 追加パーツ トランポリン",
                       "トランポリン 室内 子ども", {"GraviTrax", "追加パーツ"})


def _first(target: str, video: str, shared: set | None = None,
           with_title: bool = True) -> dict:
    brands, series = frpa.extract_brand_series(target)
    model = frpa.extract_model_number(target)
    tokens = frpa.tokenize(target)
    terms = frpa.extract_product_terms(target, brands, series)
    with mock.patch.object(frpa, "allows_title_only_path", return_value=True):
        result = frpa.filter_items(
            [{"title": video, "url": "u"}], brands, series, model, tokens, terms,
            ["title"], top_n=1, strict=2, shared_terms=shared or set(),
            asin_title=target if with_title else "")
    assert result, "expected a match"
    return result[0]


@pytest.mark.parametrize("target,video", KNOWN_POSITIVES)
def test_title_only_path_tags_the_item(target, video):
    # 裏付け語なし経路 (title_chunks) だけで strong になった項目は
    # _match: "title_only" を付ける (#8162 案 A)。
    assert _first(target, video).get("_match") == "title_only"


def test_strong_anchor_match_is_not_tagged():
    # brand/series/shared に裏付けられた既存 strong 経路は tag を付けない。
    item = _first("アンパンマン ことばずかんプラス",
                  "アンパンマンの ことばずかんプラス で遊んでみた")
    assert "_match" not in item


def test_exclude_title_only_drops_only_tagged_items():
    kept = {"title": "keep", "url": "u1"}
    dropped = {"title": "drop", "url": "u2", "_match": "title_only"}
    assert frpa.exclude_title_only([kept, dropped]) == [kept]
    assert frpa.exclude_title_only({"items": [kept, dropped]}) == {"items": [kept]}
    # dict/list 以外・items の無い dict は素通し
    assert frpa.exclude_title_only(None) is None
    assert frpa.exclude_title_only({"foo": "bar"}) == {"foo": "bar"}


# --------------------------------------------------------------------------
# allows_title_only_path (#8162 案B/C, 段階3): ASIN 単位の一般語/シリーズ名
# ゲート。ラベル付き集合 (#8122 で目視判定した 282 件) と、その raw プール
# (title_chunks に一致する候補のみに絞ったもの) を fixture として固定する。
# 出典: omcha-ops/exchange/2026-09-24-amazon8162-strict2-labels
# (#8122 のマージコミット 8e8ac6a7c8 時点)。
# --------------------------------------------------------------------------

def _load_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


_LABELS = _load_jsonl(FIXTURES / "filter_strict2_title_only_labels.jsonl")
_POOL_ROWS = _load_jsonl(FIXTURES / "filter_strict2_title_only_pool.jsonl")
_AMAZON_TITLES = {r["asin"]: r["amazon_title"]
                  for r in _load_jsonl(FIXTURES / "filter_strict2_title_only_amazon_titles.jsonl")}

_POOL_BY_ASIN: dict[str, list[str]] = defaultdict(list)
_TARGET_BY_ASIN: dict[str, str] = {}
for _row in _POOL_ROWS:
    _POOL_BY_ASIN[_row["asin"]].append(_row["video_title"])
    _TARGET_BY_ASIN[_row["asin"]] = _row["target_title"]


def _gate_enabled(asin: str) -> bool:
    target = _TARGET_BY_ASIN.get(asin)
    if not target:
        return False
    chunks = frpa.title_chunks(target)
    return frpa.allows_title_only_path(
        chunks, _POOL_BY_ASIN.get(asin, []), _AMAZON_TITLES.get(asin, ""))


def test_gate_labels_fixture_matches_committed_additions_exactly():
    # additions.jsonl (母艦への受け渡し、282件) と (asin,url) で 282/282 一致する
    # ことは母艦で確認済み (#8162 コメント)。fixture 側の件数だけ固定しておく。
    assert len(_LABELS) == 282
    labels = {l["label"] for l in _LABELS}
    assert labels == {"same_product", "other_product", "unknown"}


def test_gate_reduces_other_product_errors_by_three_quarters():
    # 現在の閾値 (TOY_CONTEXT_MIN_RATIO=0.01, SERIES_CONTINUATION_MIN_ITEMS=5) の
    # 回帰テスト。閾値やヒューリスティックを変えて誤りが増えたらここで落ちる。
    same_kept = same_total = other_kept = other_total = 0
    for l in _LABELS:
        enabled = _gate_enabled(l["asin"])
        if l["label"] == "same_product":
            same_total += 1
            same_kept += enabled
        elif l["label"] == "other_product":
            other_total += 1
            other_kept += enabled
    assert same_total == 257 and other_total == 20
    # other_product (誤り) の残存はラベル付き集合の 25% (5/20) を超えない
    assert other_kept <= 5
    # same_product (真陽性) は 70% 以上残す (75.1% = 193/257 を実測)
    assert same_kept >= 180


@pytest.mark.parametrize("asin", [
    "B0CRVH975W",  # スクイッシュ → 釣具のルアー
    "B0D5XS2X2P",  # スカイチーム → 航空連合
    "B0DQ8LWG4H",  # KIKKA → BGM
    "B089YXV214",  # にぎにぎおすしやさん → グミ
    "4344790936",  # さんかくパズル → マリオパーティ
])
def test_gate_disables_general_word_targets(asin):
    # 案B: raw プールに「おもちゃの文脈」が一切無いターゲットは無効化される。
    assert not _gate_enabled(asin)


@pytest.mark.parametrize("asin", [
    "B097BMF5DN",  # ミュウミュウおすわりぬいぐるみ (案A の代表例)
    "B003I4DTM6",  # リズムあそびいっぱいマジカルバンド (ディズニーで toy context)
    "B00370BTAA",  # ポカポンゲーム
])
def test_gate_keeps_known_positive_targets(asin):
    assert _gate_enabled(asin)
