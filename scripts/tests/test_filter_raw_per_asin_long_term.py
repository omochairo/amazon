"""filter_raw_per_asin._long_term_strong / filter_items の長い固有語 strong 化 (#8164)。

12 字上限撤廃 (#8164 案D) の副作用で、13 字以上の固有語を持つ ASIN が
uniq=1 に落ちて strict=2 の裏付け (brand/series/shared) を新たに要求され、
同一商品の動画/ニュースが脱落していた (#8163 の計測)。その救済経路
(`_long_term_strong`) と、それが再び持ち込む「系列名の取り違え」陰性
(ハマクロンコンストラクター/ライジングポリスブレイバー、#8164 実測) を
固定する。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import filter_raw_per_asin as frpa  # noqa: E402


def _strong(target: str, video: str, allow_long_term: bool = True) -> bool:
    brands, series = frpa.extract_brand_series(target)
    model = frpa.extract_model_number(target)
    tokens = frpa.tokenize(target)
    terms = frpa.extract_product_terms(target, brands, series)
    return bool(frpa.filter_items(
        [{"title": video, "url": "u"}], brands, series, model, tokens, terms,
        ["title"], top_n=1, strict=2, shared_terms=set(),
        asin_title=target, allow_long_term=allow_long_term,
        enable_title_chunks=False))


def test_exact_long_term_alone_is_strong():
    # 完全一致 (17字)。旧実装なら 12字+残りに割れて uniq=2 で通っていた。
    assert _strong("リズムあそびいっぱいマジカルバンド",
                   "リズムあそびいっぱいマジカルバンド")


def test_leading_unknown_maker_name_not_required():
    # メガハウス/アーテック等 KNOWN_BRANDS 未登録のメーカー名は、長い固有語
    # より前に付くだけなら省略されていても strong (#8164 実測)。
    assert _strong("メガハウス ルービックキューブジャポネスク",
                   "ルービックキューブジャポネスク広告用30sec")


def test_trailing_variant_suffix_required():
    # ハマクロンコンストラクター (13字, df<4) は系列名で、後ろに続く
    # バリエーション名 (緊急車両/はたらく車/ファイヤーステーション) を
    # 落とすと同系列の別商品を誤って通す (#8164 実測)。
    assert not _strong("LaQ ハマクロンコンストラクター 緊急車両",
                       "【ラキュー公式】LaQハマクロンコンストラクター ファイヤーステーション紹介")
    assert not _strong("LaQ ハマクロンコンストラクター はたらく車",
                       "【ラキュー公式】LaQハマクロンコンストラクター 緊急車両紹介")


def test_trailing_variant_suffix_present_is_strong():
    assert _strong("LaQ ハマクロンコンストラクター 緊急車両",
                   "【ラキュー公式】LaQハマクロンコンストラクター 緊急車両を紹介")


def test_trailing_color_variant_required():
    # ライジングポリスブレイバー (13字, df<4) + 色違い (白バイ/黒バイ)。
    assert not _strong("トミカ ライジングポリスブレイバー デカライドアーマー白バイ",
                       "【ジョブレイバー】ライジングポリスブレイバーZERO デカライドアーマー黒バイDXセット")


def test_shared_long_term_not_used_alone():
    # df>=4 の長い語は shared_terms に入るので、この経路の対象外
    # (score_item 側の unique/shared 判定と一貫させる)。
    brands, series = frpa.extract_brand_series("たいへんながいこゆうめいしょうのおもちゃ")
    terms = frpa.extract_product_terms("たいへんながいこゆうめいしょうのおもちゃ", brands, series)
    shared = {t for t in terms if len(t) >= frpa.LONG_TERM_MIN_LEN}
    assert not frpa._long_term_strong(
        "たいへんながいこゆうめいしょうのおもちゃ", terms, shared,
        frpa._norm("たいへんながいこゆうめいしょうのおもちゃで遊んでみた"))


def test_allow_long_term_false_disables_path():
    assert not _strong("リズムあそびいっぱいマジカルバンド",
                       "リズムあそびいっぱいマジカルバンド", allow_long_term=False)


def test_long_term_shared_with_sibling_target_not_used_alone():
    # 他のターゲットタイトルにも出る長い語 (df>=2) は系列名。ターゲット側に
    # 識別語が無い基本セットでも、兄弟商品の動画を単独一致で通さない。
    target = "LaQ ハマクロンコンストラクター"
    sibling = "LaQ ハマクロンコンストラクター 緊急車両"
    b, s = frpa.extract_brand_series(target)
    terms = frpa.extract_product_terms(target, b, s)
    df = frpa.compute_title_df([
        (target, terms),
        (sibling, frpa.extract_product_terms(sibling, *frpa.extract_brand_series(sibling)))])
    video = frpa._norm("LaQハマクロンコンストラクター 緊急車両を紹介")
    assert frpa._long_term_strong(target, terms, set(), video)
    assert not frpa._long_term_strong(target, terms, set(), video, title_df=df)


def test_trailing_short_marker_required():
    # 「2」「DX」のような短い版番号は strong 語にならないが、識別語として要求する。
    assert not _strong("トミカ ライジングポリスブレイバー 2",
                       "ライジングポリスブレイバー 3 を開封")
    assert _strong("トミカ ライジングポリスブレイバー 2",
                   "ライジングポリスブレイバー 2 を開封")
    assert not _strong("トミカ ライジングポリスブレイバー DX",
                       "ライジングポリスブレイバー を開封")


def test_different_model_number_rejected():
    assert not _strong("ともだちいっぱいアドベンチャーパック 71439",
                       "71440 ともだちいっぱいアドベンチャーパック 開封")
    assert _strong("ともだちいっぱいアドベンチャーパック 71439",
                   "ともだちいっぱいアドベンチャーパック 開封")


def test_compute_title_df_counts_substring_occurrences():
    # 語の切り方に依らず、他のタイトルに複合語の一部として出る語も数える。
    df = frpa.compute_title_df([
        ("すみっコぐらしパソコン", {"パソコン"}),
        ("ドラえもんAIパソコン", {"ドラえもん", "パソコン"}),
        ("カメラもIN!すみっコぐらしパソコンプレミアムプラス", {"パソコンプレミアムプラス"}),
    ])
    assert df["パソコン"] == 3
    assert df["ドラえもん"] == 1


def _terms(title):
    return frpa.extract_product_terms(title, *frpa.extract_brand_series(title))


def test_sibling_long_term_needs_distinguishing_suffix():
    # 兄弟商品がカタログにある系列名は、後置の識別語 (版番号 6) があれば
    # それと合わせて一致したときだけ strong。識別語にならない「ミニ」しか
    # 無い基本版には、兄弟 (…ゲーム5) の動画を通さない (#8164 実測)。
    six = "どこでもドラえもん日本旅行ゲーム6"
    mini = "エポック社 どこでもドラえもん日本旅行ゲーム ミニ"
    df = frpa.compute_title_df([(six, _terms(six)), (mini, _terms(mini))])
    assert frpa._long_term_strong(
        six, _terms(six), set(),
        frpa._norm("「どこでもドラえもん日本旅行ゲーム6」が登場！"), title_df=df)
    assert not frpa._long_term_strong(
        mini, _terms(mini), set(),
        frpa._norm("どこでもドラえもん日本旅行ゲーム５（ファイブ）あそび方"), title_df=df)
    # 基本版の直後の語 (ミニ) が候補にあれば、兄弟 (…ゲーム６) と並記された
    # 動画でも基本版の動画として残す (版表記はどれか 1 回の出現が満たせばよい)。
    assert frpa._long_term_strong(
        mini, _terms(mini), set(),
        frpa._norm("【ドラえもん】どこでもドラえもん日本旅行ゲーム６＋"
                   "どこでもドラえもん日本旅行ゲーム ミニ〈エポック社公式〉"),
        title_df=df)


def test_trailing_character_brand_required():
    # 後ろに付くキャラクター (KNOWN_BRANDS) 違いは別商品 (#8164 実測)。
    assert not _strong("アイアップ はじめてのマナー豆おおつぶ すみっコぐらし",
                       "「はじめてのマナー豆おおつぶ ドラえもん」が登場！")
    assert _strong("アイアップ はじめてのマナー豆おおつぶ すみっコぐらし",
                   "はじめてのマナー豆おおつぶ すみっコぐらし で練習")


def test_long_term_matches_with_inserted_spaces():
    # 動画/ニュース側で途中に空白が入る表記 (#8164 実測)。
    assert _strong("プラレール ありがとう!北の大地を駆け抜けた寝台特急カシオペア",
                   "プラレール　ありがとう！北の大地を駆け抜けた　寝台特急カシオペア")
    # 空白をまたいでも語境界は見る (後ろにカタカナが続けば別の語)。
    assert not _strong("北の大地を駆け抜けた寝台特急カシオペア",
                       "北の大地を駆け抜けた 寝台特急カシオペアエクスプレス")


def test_attached_variant_marker_in_candidate_rejected():
    # 候補側で長い語に英数字が直接続くのは版違い (#8164 実測)。
    assert not _strong("クリスタルルービックキューブ",
                       "#466:クリスタルルービックキューブ４×４")
    assert _strong("クリスタルルービックキューブ",
                   "メガハウスのクリスタルルービックキューブ開封の儀")
    # ターゲット側にも同じ表記があれば一致 (ZERO)。
    assert _strong("ライジングポリスブレイバーZERO",
                   "ＴＪＢＤＸ ライジングポリスブレイバーＺＥＲＯ 組み立て説明動画")
    # 後ろに漢字が続く数量表現は版表記ではない。
    assert _strong("セガトイズ いらっしゃいませ!ジャムおじさんのやきたてパン工場",
                   "いらっしゃいませ！ジャムおじさんのやきたてパン工場35万個突破！")
