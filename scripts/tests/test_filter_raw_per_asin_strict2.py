"""filter_raw_per_asin.filter_items の strict=2 (youtube/news の商品同定判定)。

裏付け語なし ASIN 経路 (2026-09-24) の真陽性と、§4.7 (2026-07-07) で strict=2 を
入れた理由である「同ブランド/同ライン別商品」の陰性を並べて固定する。
タイトルはいずれも本番の per_asin raw / ターゲットから採った実物。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import filter_raw_per_asin as frpa  # noqa: E402


def _passes(target: str, video: str, shared: set | None = None,
            with_title: bool = True) -> bool:
    brands, series = frpa.extract_brand_series(target)
    model = frpa.extract_model_number(target)
    tokens = frpa.tokenize(target)
    terms = frpa.extract_product_terms(target, brands, series)
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
