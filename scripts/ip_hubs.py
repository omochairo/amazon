"""キャラクター (IP) 別の束ねページ (navi-brain#211 の G1 試作)。

なぜ: 1 商品 1 記事の形では、アンパンマンやたまごっちのように記事が何十〜何百本ある
キャラクターでも、「どれを選べばいいか」を受けるページが無い (タグページは商品を
並べるだけで、GSC の 1 ページあたりクリックはサイトで最も低い)。一方、選び分けを
書いた特集ページは 1 商品記事の約 7 倍クリックされている (仕様 v2 §12.1)。
そこで特集と同じ型 (layout feature) で、キャラクターごとに
  - 年齢別・遊びの種類別の節 (データから決定的に作る)
  - 予算別の比較表 (年齢 hub と同じ部品)
  - 選び分けの文章 (data/hub_narratives/<key>.json。書き手が作り、validate_narrative を通ったものだけ)
を出す。

載せるのは記事のある商品だけ (出典検証済みの 1 商品記事へリンクで戻す)。
選び分けの文章は、ここで渡した事実 (記事の長所・短所・年齢・スコア) だけで書く。
外の事実 (キャラクターの歴史など) は書かない — 出典を付けられないため。

使い方:
    python scripts/ip_hubs.py prompt ip-anpanman > prompt.txt   # 書き手への依頼文
    python scripts/ip_hubs.py gate data/hub_narratives/ip-anpanman.json
build_category_hubs.py の run() が build_ip_hubs() を呼び、hugo/data/features/<key>.json を書く。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import price_overlay  # noqa: E402
import toy_type  # noqa: E402
from candidate_priority import load_character_terms  # noqa: E402

# キー → 表示名・character_terms.yaml の正規化名・URL。
# 選んだ基準 (仕様 v2 §12.3 L1): 記事 30 本以上かつ「選ぶ系」の需要語がある IP。
# G1 では 2 本だけ作り、K が読んでから広げる。
# series: 同じキャラクターの中のシリーズ (型違い・色違いを束ねる。先に書いたものから当てる)。
# accessory: 本体ではない付属品・関連グッズ (上位・年齢・種類の節と比較表に出さず、関連グッズの節にまとめる)。
# 語は _fold した商品名への部分一致。記事の商品名を数えて決めた (2026-11-24)。
IP_HUBS: dict[str, dict[str, Any]] = {
    "ip-anpanman": {
        "label": "アンパンマン", "character": "アンパンマン", "url": "/anpanman-toys/",
        "series": [
            {"label": "ブロックラボ", "terms": ["ブロックラボ"]},
            {"label": "ベビラボ", "terms": ["ベビラボ", "べビラボ"]},
            {"label": "ことばずかん", "terms": ["ことばずかん"]},
            {"label": "おしゃべり・知育パソコン", "terms": ["パソコン", "タブレット"]},
            # 天才脳はつみかさねカップ・らくがき教室もあり、パズルだけではない
            {"label": "天才脳シリーズ", "terms": ["天才脳"]},
            {"label": "くねくねチューブ", "terms": ["くねくね"]},
            # 以下は G1.5 で足した (シリーズ外が 199 本中 174 本だったため。商品名の語を数えて決めた 2026-11-25)。
            # group: 製品シリーズではなく種類のまとまり。シリーズの鳥瞰表・シリーズの節にだけ使い、
            # 上位・年齢・遊び方の節の間引き (pick_distinct) には使わない (別の商品を同じシリーズとみなさないため)。
            # 「おふろでピタッと」はおふろに入れたいので、ピタッより先に当てる。ブロックは最後 (ブロックラボを先に)
            {"label": "よくばりシリーズ", "terms": ["よくばり"]},
            {"label": "おふろあそび", "terms": ["おふろ", "お風呂"], "group": True},
            {"label": "ピタッと貼る・マグネット", "terms": ["ピタッ", "マグネット", "マグパネル"], "group": True},
            {"label": "楽器・音あそび", "terms": ["ドラム", "キーボード", "楽器"], "group": True},
            {"label": "パズル", "terms": ["パズル"], "group": True},
            {"label": "ブロック", "terms": ["ブロック"], "group": True},
        ],
        "accessory": ["保護フィルム", "シール", "ステッカー"],
    },
    "ip-tamagotchi": {
        "label": "たまごっち", "character": "たまごっち", "url": "/tamagotchi-toys/",
        "series": [
            # 「Original Tamagotchi Angel Carousel」「たまごっちオリジナル パラダイス」(8 歳〜) はオリジナルの
            # 柄違いなので、オリジナルを先に当てる
            {"label": "オリジナル・初代", "terms": ["original", "オリジナル", "初代", "オリたま"]},
            {"label": "たまごっちパラダイス", "terms": ["パラダイス", "paradise"]},
            {"label": "てんしっち", "terms": ["てんしっち"]},
            {"label": "ケーたま", "terms": ["ケーたま"]},
            {"label": "新種発見", "terms": ["新種発見"]},
        ],
        "accessory": ["シール", "ステッカー", "ストラップ", "キャリー", "ケース", "ヘアゴム",
                      "リフィル", "保護フィルム", "たまもりアクセ", "コレクタブルズ", "コレクティブルズ",
                      "pop!", "キーホルダー", "ハンディファン", "ネックストラップ"],
    },
    # G2 の 2 本 (記事数の順で次の 2 つ。商品名を数えて決めた 2026-11-25)
    "ip-tomica": {
        "label": "トミカ", "character": "トミカ", "url": "/tomica-toys/",
        # どのシリーズにも当たらない 700 円以下 = 1 台もののトミカ (通常品は 330〜670 円、2026-11-25)
        "basic": {"label": "ベーシックトミカ（1台）", "price_max": 700},
        # 「トミカ・プラレールブロック」のシリーズ名でトミカに当たる、プラレール側の電車の商品
        "exclude": ["新幹線", "ドクターイエロー", "トーマス", "ジェームス", "プラレールパッド"],
        "series": [
            # 「ユナイトファイヤブレイバー」「キャリーブレイバー」のようにジョブレイバーの語が無いものもある
            {"label": "ジョブレイバー（変形ロボ）", "terms": ["ジョブレイバー", "ブレイバー"]},
            {"label": "トミカ・プラレールブロック", "terms": ["プラレールブロック"]},
            {"label": "トミカプレミアム", "terms": ["トミカプレミアム"]},
            {"label": "頭文字D", "terms": ["頭文字d"]},
            {"label": "トミカタウン・トミカワールド", "terms": ["トミカタウン", "トミカワールド", "ビークルタウン"]},
            {"label": "トミカ限定・大人向けモデル", "terms": ["tomicacustomworks", "tomicaview", "tomicagarage",
                                                  "tomicaトランスポーター", "lv-n", "tlv"], "group": True},
            {"label": "パズル・ぬりえ", "terms": ["パズル", "ぬりえ"], "group": True},
        ],
        # 本体ではない収納・ケース。パーキングケースは車を並べて遊べるが、本体の比較には入れない
        "accessory": ["収納", "ミニカーケース", "トミカケース", "パーキングケース", "パノラマケース",
                      "おもちゃ収納ボックス"],
    },
    "ip-pokemon": {
        "label": "ポケモン", "character": "ポケモン", "url": "/pokemon-toys/",
        "series": [
            {"label": "モンコレ（フィギュア）", "terms": ["モンコレ"]},
            {"label": "ナノブロック", "terms": ["ナノブロック"]},
            {"label": "ポケモンフレンダ", "terms": ["フレンダ"]},
            {"label": "ポケなで", "terms": ["ポケなで"]},
            {"label": "なりきりグッズ", "terms": ["テラスタルオーブ", "ダイマックスバンド", "メガリング", "スマホロトム"],
             "group": True},
            {"label": "パズル・ボードゲーム", "terms": ["パズル", "ウボンゴ", "ドブル", "ごいた", "リバーシ",
                                                "ブロックスシャッフル", "ナインタイル", "危機一発", "whoareyou"],
             "group": True},
            {"label": "ぬいぐるみ", "terms": ["ぬいぐるみ", "すやすやフレンド", "mocchi", "ぽけふわ", "のんびりドール"],
             "group": True},
        ],
        # 本体ではないカード入れ・日用品
        "accessory": ["ふれんずポーチ", "フレンダフォルダー", "フレンダトランク", "フレンダキャリー", "フレンダボックス", "メザスタトランク",
                      "メラミンカップ", "アンブレラチャーム", "エコバッグ", "カードホルダー", "コレクションポーチ",
                      "コレクションファイル", "ストラップ", "ヘアバンド", "キャップ", "ビーチボール", "サッカーボール"],
    },
    # G2 の次の 2 本 (2026-11-25)。ディズニーは 0 歳のメリーから大人向けのレゴまで年齢の幅が最も広い
    "ip-disney": {
        "label": "ディズニー", "character": "ディズニー", "url": "/disney-toys/",
        "series": [
            # 製品のシリーズを先に (レゴのエルサはプリンセスではなくレゴに入れる)
            {"label": "レゴ・デュプロ", "terms": ["レゴ", "lego", "デュプロ"]},
            {"label": "はじめて英語", "terms": ["はじめて英語"]},
            {"label": "ベビージム・メリー", "terms": ["ジム", "メリー"], "group": True},
            # 「リグミー」はやのまんの木製ジグソー (短い商品名にパズルの語が無い)
            {"label": "パズル・カードゲーム", "terms": ["パズル", "メモリー", "ジグソー", "リグミー"], "group": True},
            # 以下はキャラクターのまとまり (製品シリーズではない)
            {"label": "くまのプーさん", "terms": ["プーさん", "くまのプー"], "group": True},
            {"label": "プリンセス・アナと雪の女王", "terms": ["プリンセス", "ラプンツェル", "エルサ", "アナと雪", "アナ雪",
                                                 "ジャスミン", "ファッションドール"], "group": True},
            {"label": "ピクサー（カーズ・トイ・ストーリー）", "terms": ["カーズ", "トイストーリー", "トイ・ストーリー", "ウッディ",
                                                       "バズ"], "group": True},
            # 「ピクサー」は入れない。正式名に「ディズニー&ディズニー/ピクサーキャラクターズ」と並ぶ総合の学習玩具が入るため
        ],
        "accessory": [],
        # ディズニーの社名で当たる、キャラクターの子ども向けおもちゃではない商品
        "exclude": ["スター・ウォーズ", "スターウォーズ", "star wars", "レイザー・クレスト", "マーベル", "marvel"],
    },
    "ip-plarail": {
        "label": "プラレール", "character": "プラレール", "url": "/plarail-toys/",
        "strip": ["プラレール"],
        "series": [
            {"label": "シンカリオン", "terms": ["シンカリオン"]},
            # 「DXレールキット」はレールの語も含むので、キット・セットを部品より先に当てる
            {"label": "レールキット・セット", "terms": ["レールキット", "レールセット", "コントロールセット", "ベストセレクション",
                                              "レイアウト"]},
            # except: 「モノレール」の中の「レール」で車両が部品に入らないよう、この節の判定の前に取り除く
            {"label": "レール・駅・情景部品", "except": ["モノレール"], "terms": ["レール", "橋脚", "駅", "車両基地", "ふみきり", "踏切", "跨線橋",
                                              "連結部品", "車庫", "トレインケース"]},
            # strip で「プラレール」を取り除いた後なので「ブロック」で当てる。「J-14 ブロック橋脚」は上の部品で先に当たる
            {"label": "トミカ・プラレールブロック", "terms": ["ブロック"]},
            # 車両 (1 編成) は名前に共通の語が無いので、系・新幹線などの語でまとめる。種類のまとまり
            {"label": "車両（電車・新幹線）", "terms": ["系", "新幹線", "ドクターイエロー", "特急", "モノレール", "号機",
                                            "くろしお", "カシオペア", "999号", "リゾートライン", "貨車"], "group": True},
        ],
        # 他社製の固定パーツ (本体の比較に入れない)
        "accessory": ["固定パーツ"],
    },
    # G3 (2026-11-25)。ベイブレードは全部 6 歳以上なので、年齢でなく「何から買うか」(商品の種類) で分ける。
    # 短い商品名に種類が無いもの (「ラグナレイジ」) は正式名の「ブースター」「スターター」で当たる
    "ip-beyblade": {
        "label": "ベイブレード", "character": "ベイブレード", "url": "/beyblade-toys/",
        "series": [
            # スタジアム同梱のセットはセットに。スターターの正式名は「〜&ランチャー」と並ぶので、ランチャーは最後に当てる
            {"label": "バトルセット・デッキセット", "terms": ["エントリーセット", "オールインワン", "デッキセット", "ランブルパック"]},
            {"label": "スタジアム", "terms": ["スタジアム"]},
            # 「ランダムブースター」は「ブースター」を含むので先に当てる
            {"label": "ランダムブースター", "terms": ["ランダムブースター", "セレクト"]},
            {"label": "スターター（ベイ＋ランチャー）", "terms": ["スターター"]},
            {"label": "ブースター（ベイ単品）", "terms": ["ブースター"]},
            {"label": "ランチャー・ギア", "terms": ["ランチャー", "バトルパス"]},
        ],
        # ベイもランチャーも付かない収納ケース
        "accessory": ["ギアケース"],
    },
    # 3 歳からのワイルドキングと、15 歳以上のコレクター向け (エイジ・オブ・ザ・プライム・スタジオシリーズ) が同じ名前で並ぶ
    "ip-transformers": {
        "label": "トランスフォーマー", "character": "トランスフォーマー", "url": "/transformers-toys/",
        "series": [
            {"label": "ワイルドキング", "terms": ["ワイルドキング", "エナジーマスター", "エナジービースト", "ワイルドッキング"]},
            {"label": "スタジオシリーズ", "terms": ["スタジオシリーズ", "studioseries"]},
            {"label": "エイジ・オブ・ザ・プライム", "terms": ["エイジ・オブ・ザ・プライム", "aotp"]},
            {"label": "コラボ（エヴァンゲリオンほか）", "terms": ["エヴァンゲリオン", "nfl", "モンスタージャム", "キスぷれ", "コラボ"]},
            {"label": "ブロック・プラモデル", "terms": ["blokees", "ブロッキーズ", "amk", "プラモデル"]},
        ],
        "accessory": [],
        # 名前にトランスフォーマーと入るパソコン用のセキュリティロック (おもちゃではない)
        "exclude": ["セキュリティロック"],
    },
    # G4 (2026-11-25)。サンリオは製品のシリーズより遊び方がばらばらなので、遊び方のまとまりで分ける
    "ip-sanrio": {
        "label": "サンリオ", "character": "サンリオ", "url": "/sanrio-toys/",
        "series": [
            # 「おしゃべりお医者さんセット」はごっこ遊びに入れるため、音の出るおもちゃより先に当てる
            {"label": "ごっこ遊び（お医者さん・おせわ・ままごと）", "terms": ["お医者さん", "ナース", "おせわ", "ままごと", "ドールセット",
                                                          "おべんとう", "リカちゃん"], "group": True},
            {"label": "たまごっち・ぷにるんず・スマホ", "terms": ["たまごっち", "tamagotchi", "ぷにるんず", "スマホ"]},
            {"label": "ブロック", "terms": ["ブロック"]},
            {"label": "シール・おえかき・ネイル", "terms": ["シール", "おえかき", "ぬりえ", "ネイル", "トレーサー"], "group": True},
            {"label": "音の出るおもちゃ", "terms": ["キーボード", "マイク", "おしゃべり"], "group": True},
            {"label": "パズル・ゲーム", "terms": ["パズル", "メモリー", "バランスタワー", "にこだんご"], "group": True},
        ],
        "accessory": [],
        # キャラクター名で当たる、おもちゃではない雑貨・食品・節句飾り
        "exclude": ["メッセージカード", "メロディカード", "バースデーカード", "リップクリーム", "ハンドクリーム", "五月人形", "食玩"],
    },
    # ほぼ全部 3 歳以上。お家を土台に、人形・家具・お店を買い足していく遊び方なので、その種類で分ける
    "ip-sylvanian": {
        "label": "シルバニアファミリー", "character": "シルバニアファミリー", "url": "/sylvanian-toys/",
        # 「シルバニアファミリー」の中の「ファミリー」で人形に当たらないように
        "strip": ["シルバニアファミリー", "シルバニア"],
        "series": [
            # 「フクロウの星のみえるお家」は短い商品名に Little Tales が無い (フクロウの 3 点は全部このシリーズ)
            {"label": "Little Tales Collection", "terms": ["little tales", "littletales", "フクロウの"]},
            {"label": "季節のセット", "terms": ["シーズナル", "クリスマス"]},
            {"label": "ゆうえんち・のりもの", "terms": ["ゆうえんち", "ワゴン", "トレイン", "のりもの"]},
            # 「赤い屋根の大きなお家 デラックス家具セット」は家具なので、お家より先に当てる
            {"label": "家具・小物", "terms": ["家具", "ランチセット"]},
            {"label": "お家・お城", "terms": ["お家", "お城", "はじめての"]},
            {"label": "お店・施設", "terms": ["お店", "マーケット", "美容師", "ヘアサロン", "ほいくえん", "タウン"]},
            {"label": "人形（ファミリー・赤ちゃん）", "terms": ["ファミリー", "赤ちゃん", "人形"]},
        ],
        # 遊ぶ本体ではないキーチェーン・手作りキット
        "accessory": ["キーチェーン", "ホイップる"],
    },
}

# 上位・節に載せる知育スコアの下限 (0-5)。全ラインナップ節には下限をかけない。
IP_MIN_IVS = 3.4
IP_TOP_N = 12
AGE_SECTION_MAX = 4
TYPE_SECTION_MAX = 3
TYPE_SECTION_MIN = 2

# 年齢の節。年齢 hub (build_category_hubs.AGE_HUBS) と同じ区切り。
AGE_BUCKETS = [
    ("age-0", "0歳", 0, 12),
    ("age-1", "1歳", 12, 24),
    ("age-2", "2歳", 24, 36),
    ("age-3", "3歳", 36, 48),
    ("age-4", "4〜5歳", 48, 72),
    ("age-6", "6歳以上", 72, 216),
]

NARRATIVE_DIR = Path("data/hub_narratives")

# 観点別の「いちばん」(仕様 v2 §13.9 部品 1)。表示名は score_minichart.html と同じ。
AXES = (("education", "知育"), ("longevity", "長く遊べる"),
        ("safety", "安全性"), ("cost_performance", "コスパ"))
# 1 位の値を持つ商品がこれより多い観点は出さない。安全性・コスパは満点が数十〜百数十件並び
# (アンパンマン 58 / 175 件、2026-11-25)、「いちばん」と呼べないため
AXIS_BEST_MAX_TIES = 3
AXIS_BEST_MIN_POOL = 5
SERIES_TABLE_MIN = 2


def _fold(value: Any) -> str:
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[\s\-‐‑‒–—―・_.,/（）()]+", "", value)


def ip_text(raw: dict[str, Any]) -> str:
    """キャラクター判定に使う文字列。商品名・正式名・ブランドだけを見る。

    題や本文は見ない (「アンパンマン好きにも」のような別商品の言及で当たるため)。
    """
    p = raw.get("product") or {}
    return _fold(" ".join(str(p.get(k) or "") for k in ("name", "name_full", "brand")))


def matches_character(raw: dict[str, Any], aliases: tuple[str, ...]) -> bool:
    text = ip_text(raw)
    # 「デュプロ・アンパンマン互換」のような互換品はキャラクター商品ではない (candidate_priority と同じ)
    if "互換" in text:
        return False
    return any(_fold(a) and _fold(a) in text for a in aliases)


def load_raw_articles(articles_dir: Path) -> dict[str, dict[str, Any]]:
    """{asin: 記事 JSON}。同じ ASIN が複数あれば日付の新しいファイルを使う。"""
    out: dict[str, dict[str, Any]] = {}
    from build_feature_lists import _is_article_json
    for path in sorted(articles_dir.glob("*.json")):
        if not _is_article_json(path):  # *.seo.json などの付属ファイル
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        asin = (raw.get("product") or {}).get("asin") if isinstance(raw, dict) else None
        if asin:
            out[str(asin)] = raw
    return out


def _amazon_item(per_asin_dir: Path, asin: str) -> dict[str, Any] | None:
    try:
        d = json.loads((per_asin_dir / asin / "amazon.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(d, dict):
        return None
    item = d.get("item") or d
    return item if isinstance(item, dict) else None


def _rank_key(rec):
    return (-(rec.ivs_100 or 0), rec.best_price or 10**9)


def _bucket(mm: int | None) -> str | None:
    if mm is None:
        return None
    for key, _label, lo, hi in AGE_BUCKETS:
        if lo <= mm < hi:
            return key
    return None


def _brief(rec, mm: int | None, types: list[str]) -> dict[str, Any]:
    return {
        "asin": rec.asin,
        "name": rec.name,
        "url_internal": f"/products/{rec.asin.lower()}/",
        "image": rec.image,
        "ivs_100": rec.ivs_100,
        # 商品ごとの小さな「Amazon で見る」(表示側で amazon_affiliate_url.html がタグを強制)
        "amazon_url": rec.amazon_url,
        "best_price": rec.best_price,
        "best_platform": rec.best_platform,
        "age_min_months": rec.age_min_months if rec.age_min_months is not None else mm,
        "types": [toy_type.label(t) for t in types],
    }


def series_of(name: str | None, hub: dict[str, Any], *, groups: bool = False) -> str | None:
    """商品名のシリーズ。groups=False では種類のまとまり (group) を見ない (間引き用)。

    hub の strip の語は先に取り除く (「プラレール」の中の「レール」がレール部品に当たらないように)。
    """
    text = _fold(name)
    for t in hub.get("strip") or []:
        text = text.replace(_fold(t), " ")
    for s in hub.get("series") or []:
        if s.get("group") and not groups:
            continue
        t_s = text
        for t in s.get("except") or []:
            t_s = t_s.replace(_fold(t), " ")
        if any(_fold(t) in t_s for t in s["terms"]):
            return s["label"]
    return None


def series_defs(hub: dict[str, Any]) -> list[dict[str, Any]]:
    """表と節に出すシリーズの定義。価格で決める「ベーシック」(basic) を最後に足す。"""
    out = list(hub.get("series") or [])
    if hub.get("basic"):
        out.append({"label": hub["basic"]["label"], "terms": []})
    return out


def series_of_rec(rec, hub: dict[str, Any], *, groups: bool = False) -> str | None:
    """record のシリーズ。名前で当たらず、hub に basic があり価格がその上限以下なら basic のシリーズ。

    トミカの 1 台もの (No.〜) は名前に共通の語が無く、上位がほぼ 370 円前後の 1 台ものだけで
    埋まったため (2026-11-25)。間引き (groups=False) にも効かせ、上位に並ぶのを抑える。
    """
    # 短い商品名から削られたシリーズ名 (「トミカプレミアム 05 ランボルギーニ ミウラ」→「トミカ ランボルギーニ ミウラ」)
    # を拾うため、短い商品名で当たらないときだけ正式名を見る (agy 指摘・2026-11-25)。
    # 先に正式名を混ぜると、正式名に「オリジナル」と入る「てんしっち」がオリジナルに吸われた
    s = series_of(rec.name, hub, groups=True) or series_of(getattr(rec, "name_full", None), hub, groups=True)
    if s:
        # 種類のまとまり (パズルなど) に当たったものは、間引きではシリーズ外。ベーシックにも落とさない
        is_group = any(d.get("group") and d["label"] == s for d in hub.get("series") or [])
        return None if (is_group and not groups) else s
    basic = hub.get("basic")
    if basic and not is_accessory(rec.name, hub) and rec.best_price and rec.best_price <= basic["price_max"]:
        return basic["label"]
    return None


def is_accessory(name: str | None, hub: dict[str, Any]) -> bool:
    """付属品か。「特典ステッカー付き」のように本体に付いてくるものは付属品にしない。"""
    text = _fold(name)
    for t in hub.get("accessory") or []:
        ft = _fold(t)
        i = text.find(ft)
        while i >= 0:
            if not text.startswith("付", i + len(ft)):
                return True
            i = text.find(ft, i + 1)
    return False


def axis_best(good) -> list[dict[str, Any]]:
    """観点ごとの 1 位。1 位の値を持つ商品が AXIS_BEST_MAX_TIES 件を超える観点は出さない。

    同じ値が並んだら総合の順 (_rank_key) で先の商品。返す値は 5 点満点の軸の値。
    """
    if len(good) < AXIS_BEST_MIN_POOL:
        return []
    out = []
    for axis, label in AXES:
        scored = [(r, (r.ivs_axes or {}).get(axis)) for r in good]
        scored = [(r, v) for r, v in scored if isinstance(v, (int, float))]
        if not scored:
            continue
        top = max(v for _, v in scored)
        holders = sorted((r for r, v in scored if v == top), key=_rank_key)
        if len(holders) > AXIS_BEST_MAX_TIES:
            continue
        out.append({"axis": axis, "label": label, "value": top, "asin": holders[0].asin,
                    "ties": len(holders)})
    return out


# キャラクターそのものへの興味・好みの文だけ (「ブロック遊びに興味がない」のような商品の特徴の文は残す)
_CHAR_ONLY_TAIL = r"(興味|知らない|避け|以外|好みでない|好まない)"


def _is_character_interest(s: str, hub: dict[str, Any]) -> bool:
    """「アンパンマンに興味がない子」「キャラクターものを避けたい方」「アンパンマン以外のキャラクターを好む子」か。"""
    words = ("キャラクター", "キャラ", str(hub.get("character") or ""))
    f = _fold(s)
    if not any(w and _fold(w) in f for w in words):
        return False
    return re.search(_CHAR_ONLY_TAIL, s) is not None


def voice_of(raw: dict[str, Any], hub: dict[str, Any]) -> dict[str, str]:
    """記事にある「買った人の声の要約」と「こんな子に／向かない子」の 1 件目 (部品 3・4)。

    作らない。記事に無ければ出さない。「キャラクターに興味がない子」は、このページに来る人には
    当たり前で役に立たないので、向かない子の 1 件目から外す。
    """
    out: dict[str, str] = {}
    rs = raw.get("review_signals") or {}
    summary = rs.get("summary_one_line") if isinstance(rs, dict) else None
    if isinstance(summary, str) and summary.strip():
        out["voice"] = summary.strip()
    pf = raw.get("persona_fit") or {}
    if not isinstance(pf, dict):
        return out
    for s in pf.get("recommended_for") or []:
        if isinstance(s, str) and s.strip():
            out["fits"] = s.strip()
            break
    for s in pf.get("not_recommended_for") or []:
        if not isinstance(s, str) or not s.strip():
            continue
        if _is_character_interest(s, hub):
            continue
        out["not_for"] = s.strip()
        break
    return out


def series_overview(pool, hub: dict[str, Any], mm_index) -> list[dict[str, Any]]:
    """シリーズの鳥瞰表 (部品 2): シリーズごとの記事数・対象年齢の幅・価格帯・代表 1 商品。

    価格はビルド時の最安値 (price_overlay 済みの best_price)。在庫の無いものは価格帯に入れない。
    代表は総合の順で先の商品。2 件未満のシリーズは出さない (表にする意味が無い)。
    """
    rows = []
    for s in series_defs(hub):
        members = sorted((r for r in pool if series_of_rec(r, hub, groups=True) == s["label"]
                          and not is_accessory(r.name, hub)), key=_rank_key)
        if len(members) < SERIES_TABLE_MIN:
            continue
        ages = [mm_index.get(r.asin) for r in members]
        ages = [a for a in ages if a is not None]
        prices = [r.best_price for r in members if r.best_price
                  and not price_overlay.is_explicitly_unavailable(r.availability)]
        rep = members[0]
        rows.append({
            "label": s["label"], "count": len(members),
            "age_min": min(ages) if ages else None, "age_max": max(ages) if ages else None,
            "price_min": min(prices) if prices else None, "price_max": max(prices) if prices else None,
            "rep": {"asin": rep.asin, "name": rep.name, "url_internal": f"/products/{rep.asin.lower()}/",
                    "image": rep.image, "ivs_100": rep.ivs_100},
        })
    rows.sort(key=lambda x: -x["count"])
    return rows


# 商品の dict に声・バッジを差し込む節 (lineup は件数が多く、名前だけの一覧なので入れない)
_ANNOTATE_KEYS = ("age_sections", "type_sections", "series_sections", "accessories",
                  "compare_table", "narrative", "items", "sections", "bands", "choose", "series_tips",
                  "axis_best", "item", "series_overview", "rep")


def annotate_items(node: Any, voices: dict[str, dict[str, str]],
                   badges: dict[str, list[str]], *, _key: str | None = None) -> None:
    """extra の中の商品 dict (asin と url_internal を持つもの) に voice/fits/not_for/badges を足す。"""
    if isinstance(node, list):
        for x in node:
            annotate_items(x, voices, badges, _key=_key)
        return
    if not isinstance(node, dict):
        return
    asin = node.get("asin")
    if isinstance(asin, str) and "url_internal" in node:
        node.update(voices.get(asin) or {})
        if badges.get(asin):
            node["badges"] = list(badges[asin])
    for k in _ANNOTATE_KEYS:
        if k in node:
            annotate_items(node[k], voices, badges, _key=k)


def pick_distinct(recs, hub: dict[str, Any], limit: int, *, per_series: int = 1):
    """スコア順の recs から、色違い・同じシリーズが並ばないように limit 件選ぶ。

    同じシリーズは per_series 件まで、シリーズ外は名前のほぼ同じもの (別出品・色違い) を 1 件に。
    足りなければ外したものをスコア順に戻して埋める (件数を減らすより重なりを残す)。
    """
    import build_category_hubs as bch
    picked, held, keys = [], [], []
    used: Counter = Counter()
    for r in recs:
        if len(picked) >= limit:
            break
        s = series_of_rec(r, hub)
        key = bch._name_key(r.name)
        # 同じ商品の別出品 (名前がほぼ同じ) はシリーズの内外を問わず 1 件に
        if (s and used[s] >= per_series) or bch._same_product(key, keys):
            held.append(r)
            continue
        if s:
            used[s] += 1
        keys.append(key)
        picked.append(r)
    if len(picked) < limit:
        picked.extend(held[:limit - len(picked)])
        picked.sort(key=_rank_key)
    return picked


def select_ip_records(records, raw_index: dict[str, dict[str, Any]], aliases: tuple[str, ...],
                      exclude: tuple[str, ...] | list[str] = ()):
    """IP に当たる記事の record をスコア順で返す (記事のある商品だけ)。

    exclude: 商品名・正式名に含まれていたら外す語 (トミカのページからプラレール側の電車を外すなど)。
    """
    ex = [_fold(t) for t in exclude if _fold(t)]
    pool = [r for r in records if r.asin in raw_index and matches_character(raw_index[r.asin], aliases)
            and not any(t in ip_text(raw_index[r.asin]) for t in ex)]
    pool.sort(key=_rank_key)
    return pool


def build_ip_payload(key: str, records, raw_index, mm_index, *, per_asin_dir: Path,
                     narrative_dir: Path = NARRATIVE_DIR, min_ivs: float = IP_MIN_IVS,
                     compare_builder=None, rules=None) -> tuple[list, dict[str, Any]]:
    """(上位 record の list, feature JSON に足す項目) を返す。"""
    hub = IP_HUBS[key]
    terms = load_character_terms()
    aliases = (hub["character"], *terms.get(hub["character"], ()))
    pool = select_ip_records(records, raw_index, aliases, hub.get("exclude") or ())
    rules = rules or toy_type.load_rules()
    types_of: dict[str, list[str]] = {}
    for rec in pool:
        raw = raw_index[rec.asin]
        types_of[rec.asin] = toy_type.classify_article(raw, _amazon_item(per_asin_dir, rec.asin))
    good_all = [r for r in pool if r.ivs_score is not None and r.ivs_score >= min_ivs]
    accessories = [r for r in good_all if is_accessory(r.name, hub)]
    good = [r for r in good_all if not is_accessory(r.name, hub)]
    top = pick_distinct(good, hub, IP_TOP_N, per_series=2)

    age_sections = []
    for bkey, label, lo, hi in AGE_BUCKETS:
        members = [r for r in good if _bucket(mm_index.get(r.asin)) == bkey]
        if not members:
            continue
        age_sections.append({
            "key": bkey, "label": label, "count": sum(1 for r in pool if _bucket(mm_index.get(r.asin)) == bkey),
            "items": [_brief(r, mm_index.get(r.asin), types_of[r.asin])
                      for r in pick_distinct(members, hub, AGE_SECTION_MAX)],
        })

    type_sections = []
    type_counts = Counter(t for r in pool for t in types_of[r.asin])
    for tkey in rules.keys:
        members = [r for r in good if tkey in types_of[r.asin]]
        if len(members) < TYPE_SECTION_MIN:
            continue
        type_sections.append({
            "key": tkey, "label": toy_type.label(tkey), "count": type_counts[tkey],
            "items": [_brief(r, mm_index.get(r.asin), types_of[r.asin])
                      for r in pick_distinct(members, hub, TYPE_SECTION_MAX)],
        })

    # シリーズの節: 同じシリーズの型違い・色違いを 1 か所で見比べる (仕様 v2 §12.3 L3 の受け皿)
    series_sections = []
    for s in series_defs(hub):
        members = [r for r in pool if series_of_rec(r, hub, groups=True) == s["label"] and not is_accessory(r.name, hub)]
        if len(members) < 2:
            continue
        series_sections.append({
            "label": s["label"], "count": len(members),
            "items": [_brief(r, mm_index.get(r.asin), types_of[r.asin]) for r in members[:6]],
        })
    accessory_section = [_brief(r, mm_index.get(r.asin), types_of[r.asin])
                         for r in pick_distinct(accessories, hub, 6)]

    stats = {
        "articles": len(pool),
        "scored_ok": len(good),
        "age_counts": {b[0]: sum(1 for r in pool if _bucket(mm_index.get(r.asin)) == b[0]) for b in AGE_BUCKETS},
        "type_counts": {toy_type.label(k): v for k, v in type_counts.most_common()},
        "in_stock": sum(1 for r in pool if r.best_price and not price_overlay.is_explicitly_unavailable(r.availability)),
    }
    extra: dict[str, Any] = {
        "label": hub["label"],
        "ip": hub["character"],
        "stats": stats,
        "age_sections": age_sections,
        "type_sections": type_sections,
        "series_sections": series_sections,
        "accessories": accessory_section,
    }
    if compare_builder is not None:
        table = compare_builder([r for r in pool if not is_accessory(r.name, hub)])
        if table:
            extra["compare_table"] = table
    shown = {r.asin for r in top}
    extra["lineup"] = [
        {"asin": r.asin, "name": r.name, "url_internal": f"/products/{r.asin.lower()}/",
         "ivs_100": r.ivs_100, "age_min_months": mm_index.get(r.asin)}
        for r in pool if r.asin not in shown
    ]
    narrative = load_narrative(narrative_dir / f"{key}.json")
    if narrative is not None:
        by_asin = {r.asin: r for r in pool}
        age_keys = {s["key"] for s in age_sections}
        accessory_asins = {r.asin for r in pool if is_accessory(r.name, hub)}
        choose_ok = {r.asin for r in good} - accessory_asins
        # 記事データの変化 (記事が消えた・スコアが下限を割った・年齢の節が無くなった・辞書で
        # 付属品になった) で合わなくなった項目だけを外す。合格した文章がデータの小さな変化で
        # 丸ごと消えないように (仕様 v2 §13・agy 指摘)。外したら WARNING を出し、書き直しを待つ
        narrative, dropped = prune_for_data_drift(narrative, choose_ok, set(by_asin), age_keys)
        errors = validate_narrative(narrative, set(by_asin), age_keys,
                                    names=[r.name or "" for r in pool],
                                    accessory_asins=accessory_asins,
                                    choose_asins=choose_ok,
                                    choose_min=CHOOSE_MIN_AFTER_DRIFT if dropped else CHOOSE_MIN)
        if dropped:
            print(f"[ip_hubs] WARNING {key}: dropped for data drift: {dropped[:5]}", file=sys.stderr)
        if errors:
            # 文章そのものの違反 (金額・煽り・作った数字など) や、外した結果足りなくなったときは
            # 文章ごと出さない (データの節だけのページにする)。理由はビルドのログへ
            print(f"[ip_hubs] WARNING {key}: narrative rejected: {errors[:5]}", file=sys.stderr)
        else:
            extra["narrative"] = _render_narrative(narrative, by_asin, mm_index, types_of)

    # 部品 1〜4 (仕様 v2 §13.9)。どれも記事データから決まり、LLM は使わない
    best = axis_best(good)
    by_pool = {r.asin: r for r in pool}
    extra["axis_best"] = [
        {**b, "item": _brief(by_pool[b["asin"]], mm_index.get(b["asin"]), types_of[b["asin"]]),
         "of": len(good)}
        for b in best
    ]
    badges: dict[str, list[str]] = {}
    for b in best:
        badges.setdefault(b["asin"], []).append(f"「{b['label']}」1位")
    extra["series_overview"] = series_overview(pool, hub, mm_index)
    voices = {r.asin: voice_of(raw_index[r.asin], hub) for r in pool}
    annotate_items(extra, voices, badges)
    stats["with_voice"] = sum(1 for v in voices.values() if v.get("voice"))
    stats["in_series"] = sum(1 for r in pool if series_of_rec(r, hub, groups=True) and not is_accessory(r.name, hub))
    return top, extra


def prune_for_data_drift(n: Any, choose_ok: set[str], pool_asins: set[str],
                         age_keys: set[str]) -> tuple[Any, list[str]]:
    """データの変化で合わなくなった項目だけを外した写しと、外した項目の一覧を返す。

    文章の中身の違反は外さない (validate_narrative がそのまま落とす)。形が崩れているときは触らない。
    """
    if not isinstance(n, dict) or _shape_errors(n):
        return n, []
    out = dict(n)
    dropped: list[str] = []
    choose = []
    for c in n.get("choose") or []:
        if c["asin"] in choose_ok:
            choose.append(c)
        else:
            dropped.append(f"choose {c['asin']}")
    out["choose"] = choose
    by_age = []
    for a in n.get("by_age") or []:
        if a["age_key"] in age_keys:
            by_age.append(a)
        else:
            dropped.append(f"by_age {a['age_key']}")
    out["by_age"] = by_age
    tips = []
    for t in n.get("series_tips") or []:
        asins = [a for a in t.get("asins") or [] if a in pool_asins]
        if len(asins) >= 2:
            tips.append({**t, "asins": asins})
        elif len(asins) != len(t.get("asins") or []):
            dropped.append(f"series_tips {t['title']}")
        else:
            tips.append(t)  # もともと 2 件未満の違反は文章の違反として検査に任せる
    out["series_tips"] = tips
    return out, dropped


def _render_narrative(n: dict[str, Any], by_asin, mm_index, types_of) -> dict[str, Any]:
    """表示用に、文章の ASIN 参照へ商品の名前・URL・画像を差し込む。"""
    def card(asin):
        r = by_asin[asin]
        return _brief(r, mm_index.get(asin), types_of.get(asin, []))
    return {
        "lead": n["lead"],
        "choose": [{**card(c["asin"]), "when": c["when"], "why": c["why"]} for c in n["choose"]],
        "by_age": {a["age_key"]: a["text"] for a in n.get("by_age") or []},
        "series_tips": [
            {"title": s["title"], "text": s["text"], "items": [card(a) for a in s.get("asins") or []]}
            for s in n.get("series_tips") or []
        ],
        "not_for": list(n.get("not_for") or []),
        "faq": list(n.get("faq") or []),
        "generated_at": n.get("generated_at"),
    }


# --------------------------------------------------------------------------
# 選び分けの文章の検査 (仕様 v2 §12.3 R3)。「仕様を並べただけ」「データに無いこと」を落とす。
# --------------------------------------------------------------------------

_ASIN_RE = re.compile(r"\bB0[0-9A-Z]{8}\b")
# 本文は NFKC + casefold してから当てる (全角数字・全角記号も半角に寄る)
_YEN_RE = re.compile(r"[0-9,.]+\s*(万|千)?\s*円|[¥￥]\s*[0-9]|[一二三四五六七八九十百千万]+円")
_NUM_RE = re.compile(r"[0-9０-９]+(?:[.．][0-9０-９]+)?")
_AGE_TAIL_RE = re.compile(r"\s*(歳|才|ヶ月|か月|カ月|ヵ月|ケ月)")
# 「4〜5歳」の 4 のように、年齢の幅の前の数字も年齢 (ページの節の名前「4〜5歳」と同じ書き方)
# 区切りは波線だけ (ハイフンは型番「TJB-10」などと紛れる)
_AGE_RANGE_RE = re.compile(r"\s*[〜~～]\s*[0-9]+(?:\.[0-9]+)?\s*(歳|才|ヶ月|か月|カ月|ヵ月|ケ月)")
# 書かせない言い切り・誇大表現 (景表法・Google の品質方針)。
_HYPE = ("絶対", "必ず", "最強", "No.1", "ナンバーワン", "間違いな", "100%", "誰でも")
# 価格の動きは本文に書かない (凍った文になる。金額はビルド時に描く・amazon#11867)。
_PRICE_WORDS = ("最安", "値下がり", "値上がり", "セール", "特価", "割引", "今が買い")
CHOOSE_MIN = 5
CHOOSE_MAX = 8
# データの変化で選び分けの商品を外したあと、表示を続ける下限 (書く時は CHOOSE_MIN)
CHOOSE_MIN_AFTER_DRIFT = 3
WHY_MIN_CHARS = 30
LEAD_MIN_CHARS = 120
LEAD_MAX_CHARS = 400
SIMILAR_MAX = 0.5


def load_narrative(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _grams(text: str) -> set[str]:
    t = re.sub(r"\s+", "", text)
    return {t[i:i + 3] for i in range(max(0, len(t) - 2))}


def _jaccard(a: str, b: str) -> float:
    ga, gb = _grams(a), _grams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def _texts(n: dict[str, Any]) -> list[str]:
    out = [str(n.get("lead") or "")]
    for c in n.get("choose") or []:
        out += [str(c.get("when") or ""), str(c.get("why") or "")]
    out += [str(a.get("text") or "") for a in n.get("by_age") or []]
    for s in n.get("series_tips") or []:
        out += [str(s.get("title") or ""), str(s.get("text") or "")]
    out += [str(x) for x in n.get("not_for") or []]
    for f in n.get("faq") or []:
        out += [str(f.get("q") or ""), str(f.get("a") or "")]
    return out


def _shape_errors(n: dict[str, Any]) -> list[str]:
    """表示 (_render_narrative) が読む形になっているか。崩れた JSON でビルドを落とさない。"""
    errs: list[str] = []
    if not isinstance(n.get("lead"), str):
        errs.append("lead must be a string")
    specs = {
        "choose": ("asin", "when", "why"),
        "by_age": ("age_key", "text"),
        "series_tips": ("title", "text"),
        "faq": ("q", "a"),
    }
    for field, keys in specs.items():
        v = n.get(field)
        if v is None:
            v = []
        if not isinstance(v, list):
            errs.append(f"{field} must be a list")
            continue
        for i, item in enumerate(v):
            if not isinstance(item, dict) or any(
                    not isinstance(item.get(k), str) or not item.get(k).strip() for k in keys):
                errs.append(f"{field}[{i}] must have non-empty {', '.join(keys)}")
            elif field == "series_tips" and not isinstance(item.get("asins") or [], list):
                errs.append(f"series_tips[{i}].asins must be a list")
    nf = n.get("not_for")
    if nf is not None and (not isinstance(nf, list) or any(not isinstance(x, str) for x in nf)):
        errs.append("not_for must be a list of strings")
    return errs


def validate_narrative(n: dict[str, Any], pool_asins: set[str], age_keys: set[str],
                       *, names: list[str] | None = None,
                       accessory_asins: set[str] | None = None,
                       choose_asins: set[str] | None = None,
                       choose_min: int | None = None) -> list[str]:
    """違反の一覧を返す (空なら合格)。choose_asins を渡すと、選び分けに出せる商品をそこに限る。"""
    errors: list[str] = []
    if not isinstance(n, dict):
        return ["not an object"]
    shape = _shape_errors(n)
    if shape:
        return shape  # 形が崩れていると以降の検査も表示も読めない
    lead = str(n.get("lead") or "")
    if not (LEAD_MIN_CHARS <= len(lead) <= LEAD_MAX_CHARS):
        errors.append(f"lead length {len(lead)} not in {LEAD_MIN_CHARS}-{LEAD_MAX_CHARS}")
    choose = n.get("choose") or []
    cmin = CHOOSE_MIN if choose_min is None else choose_min
    if not (cmin <= len(choose) <= CHOOSE_MAX):
        errors.append(f"choose count {len(choose)} not in {cmin}-{CHOOSE_MAX}")
    seen: set[str] = set()
    for i, c in enumerate(choose):
        asin = str(c.get("asin") or "")
        if asin not in pool_asins:
            errors.append(f"choose[{i}] asin {asin} is not in this hub")
        elif choose_asins is not None and asin not in choose_asins:
            errors.append(f"choose[{i}] asin {asin} is below the score floor")
        if asin in seen:
            errors.append(f"choose[{i}] asin {asin} duplicated")
        # 「どれを選ぶ」は本体の選び分け。付属品 (シール・ケース等) を入れると読者の問いに答えない
        if accessory_asins and asin in accessory_asins:
            errors.append(f"choose[{i}] asin {asin} is an accessory")
        seen.add(asin)
        if not str(c.get("when") or "").strip():
            errors.append(f"choose[{i}] when is empty")
        if len(str(c.get("why") or "")) < WHY_MIN_CHARS:
            errors.append(f"choose[{i}] why shorter than {WHY_MIN_CHARS}")
    # 分岐どうしが同じ言い回しなら選び分けになっていない
    whens = [str(c.get("when") or "") for c in choose]
    for i in range(len(whens)):
        for j in range(i + 1, len(whens)):
            if _jaccard(whens[i], whens[j]) >= SIMILAR_MAX:
                errors.append(f"choose[{i}] and choose[{j}] 'when' too similar")
    for a in n.get("by_age") or []:
        if a.get("age_key") not in age_keys:
            errors.append(f"by_age key {a.get('age_key')} has no section")
    for s in n.get("series_tips") or []:
        asins = s.get("asins") or []
        if len(asins) < 2:
            errors.append(f"series_tips '{s.get('title')}' compares fewer than 2 products")
        for a in asins:
            if a not in pool_asins:
                errors.append(f"series_tips asin {a} is not in this hub")
    if not (n.get("not_for") or []):
        errors.append("not_for is empty")
    allowed_numbers = {unicodedata.normalize("NFKC", m) for nm in (names or [])
                       for m in _NUM_RE.findall(nm)}
    for t in _texts(n):
        if _ASIN_RE.search(t):
            errors.append(f"raw ASIN in text: {t[:40]}")
        norm = unicodedata.normalize("NFKC", t).casefold()
        if _YEN_RE.search(norm):
            errors.append(f"price amount in text: {t[:40]}")
        for w in _HYPE:
            if w.casefold() in norm:
                errors.append(f"hype word '{w}': {t[:40]}")
        for w in _PRICE_WORDS:
            if w in norm:
                errors.append(f"price-movement word '{w}': {t[:40]}")
        # 数字は年齢・月齢と、商品名に同じ数字として出るものだけ (渡していない数字 = 作った事実)
        for m in _NUM_RE.finditer(norm):
            num = m.group(0)
            if _AGE_TAIL_RE.match(norm, m.end()) or _AGE_RANGE_RE.match(norm, m.end()):
                continue
            if num in allowed_numbers:
                continue
            errors.append(f"number '{num}' not from ages or product names: {t[:40]}")
    return errors


# --------------------------------------------------------------------------
# 書き手への依頼文
# --------------------------------------------------------------------------

PROMPT_HEAD = """あなたは知育玩具の比較サイト「おもちゃいろ 知育玩具比較ナビ」の編集者です。
キャラクター「{label}」のおもちゃを選ぶ親のために、下の【事実】だけを使って「選び分け」の文章を JSON で書いてください。

ルール (守らないと自動検査で全部捨てられます):
- 【事実】に無いことは書かない。キャラクターの歴史・発売年・人気の度合い・他社の評判など、外の知識は使わない
- 金額・「最安」「値下がり」は書かない (価格は別に表示される)。「絶対」「必ず」「最強」「No.1」も使わない
- 数字は年齢・月齢と、商品名に入っている数字だけ。記事数や割合は書かない (ページが自動で出す)
- ASIN のコードを本文に書かない (asin 欄にだけ書く)
- 商品の紹介を 1 つずつ並べるのではなく、「どういう子・家庭なら、どれを選ぶか」の分かれ目を書く。長所だけでなく短所 (cons) も選び分けの理由に使う
- 「choose」は {cmin}〜{cmax} 件。accessory が true の商品 (シール・ケースなどの付属品) は入れない。それぞれ違う商品で、when (どんな子・どんな場面に) は互いに違う切り口にする (年齢・遊び方・置き場所・遊ぶ人数・電池の有無など)
- 「series_tips」は、同じシリーズの型違い・似た名前の商品があるときだけ 0〜3 件。違いが【事実】から言えるものだけ
- 「by_age」は【事実】の年齢の節にあるキーだけ。各 1〜2 文
- 「not_for」は、このキャラクターのおもちゃを選ばない方がいい場合や、選ぶときの注意 1〜3 件
- 「faq」は親が実際に迷う質問 2〜4 件。答えは【事実】から言えることだけ
- 文体は「です・ます」。読者に向けて書く。調べた経緯や「事実によると」は書かない

出力は次の形の JSON だけ (前後に説明を付けない):
{{"lead": "{lmin}〜{lmax} 字", "choose": [{{"asin": "...", "when": "...", "why": "{wmin} 字以上"}}], "by_age": [{{"age_key": "age-1", "text": "..."}}], "series_tips": [{{"title": "...", "text": "...", "asins": ["...", "..."]}}], "not_for": ["..."], "faq": [{{"q": "...", "a": "..."}}]}}

【事実】
"""


def build_prompt(key: str, records, raw_index, mm_index, *, per_asin_dir: Path, top_k: int = 40) -> str:
    hub = IP_HUBS[key]
    top, extra = build_ip_payload(key, records, raw_index, mm_index, per_asin_dir=per_asin_dir,
                                  narrative_dir=Path("/nonexistent"))
    terms = load_character_terms()
    aliases = (hub["character"], *terms.get(hub["character"], ()))
    pool = select_ip_records(records, raw_index, aliases, hub.get("exclude") or ())
    pool = [r for r in pool if r.ivs_score is not None and r.ivs_score >= IP_MIN_IVS]
    import build_category_hubs as bch
    # 同じ商品の別出品 (たまごっちパラダイス Blue Water / ブルーウォーター) を 1 件にしてから渡す。
    # 重複が入ると書き手が同じ商品の ASIN 違いを選び分けや見分けの比較に使ってしまう
    deduped, keys = [], []
    for r in pool:
        k = bch._name_key(r.name)
        if bch._same_product(k, keys):
            continue
        keys.append(k)
        deduped.append(r)
    pool = deduped[:top_k]
    products = []
    for r in pool:
        raw = raw_index[r.asin]
        p = raw.get("product") or {}
        axes = r.ivs_axes or {}
        products.append({
            "asin": r.asin,
            "name": r.name,
            "brand": p.get("brand"),
            "series": series_of_rec(r, hub),
            "accessory": is_accessory(r.name, hub),
            "age_section": _bucket(mm_index.get(r.asin)),
            "target_age": p.get("target_age"),
            "types": [toy_type.label(t) for t in toy_type.classify_article(raw, _amazon_item(per_asin_dir, r.asin))],
            "score_100": r.ivs_100,
            "score_axes": {k: axes.get(k) for k in ("education", "longevity", "safety", "cost_performance")},
            "price_band": _price_band(r.best_price),
            "pros": list(p.get("pros") or [])[:4],
            "cons": list(p.get("cons") or [])[:3],
        })
    facts = {
        "character": hub["label"],
        "age_sections": [{"key": s["key"], "label": s["label"]} for s in extra["age_sections"]],
        "products": products,
    }
    head = PROMPT_HEAD.format(label=hub["label"], cmin=CHOOSE_MIN, cmax=CHOOSE_MAX,
                              lmin=LEAD_MIN_CHARS, lmax=LEAD_MAX_CHARS, wmin=WHY_MIN_CHARS)
    return head + json.dumps(facts, ensure_ascii=False, indent=1) + "\n"


def _price_band(price: int | None) -> str | None:
    if not price:
        return None
    if price < 3000:
        return "3,000円未満"
    if price < 5000:
        return "3,000〜5,000円"
    if price < 10000:
        return "5,000〜10,000円"
    return "10,000円以上"


def check_against_data(key: str, n: dict[str, Any], records, raw_index, mm_index, *,
                       per_asin_dir: Path) -> list[str]:
    """ビルド (build_ip_payload) と同じ条件で、文章を今の記事データに当てる。"""
    hub = IP_HUBS[key]
    _top, extra = build_ip_payload(key, records, raw_index, mm_index, per_asin_dir=per_asin_dir,
                                   narrative_dir=Path("/nonexistent"))
    terms = load_character_terms()
    pool = select_ip_records(records, raw_index, (hub["character"], *terms.get(hub["character"], ())),
                             hub.get("exclude") or ())
    good = {r.asin for r in pool if r.ivs_score is not None and r.ivs_score >= IP_MIN_IVS}
    return validate_narrative(n, {r.asin for r in pool}, {s["key"] for s in extra["age_sections"]},
                              names=[r.name or "" for r in pool],
                              accessory_asins={r.asin for r in pool if is_accessory(r.name, hub)},
                              choose_asins=good)


def _load_context(articles_dir: Path, per_asin_dir: Path, raw_root: Path):
    import build_category_hubs as bch
    from build_feature_lists import _dedupe_by_asin, load_articles, overlay_current_prices
    records = _dedupe_by_asin(load_articles(articles_dir))
    overlay_current_prices(records, per_asin_dir, raw_root=raw_root)
    return records, load_raw_articles(articles_dir), bch.build_min_months_index(articles_dir)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("prompt")
    p1.add_argument("key", choices=list(IP_HUBS))
    p2 = sub.add_parser("gate")
    p2.add_argument("path", type=Path)
    for p in (p1, p2):
        p.add_argument("--articles-dir", default=Path("data/articles"), type=Path)
        p.add_argument("--per-asin-dir", default=Path("data/raw/per_asin"), type=Path)
        p.add_argument("--raw-root", default=Path("data/raw"), type=Path)
    args = ap.parse_args(argv)
    records, raw_index, mm_index = _load_context(args.articles_dir, args.per_asin_dir, args.raw_root)
    if args.cmd == "prompt":
        sys.stdout.write(build_prompt(args.key, records, raw_index, mm_index, per_asin_dir=args.per_asin_dir))
        return 0
    key = args.path.stem
    if key not in IP_HUBS:
        print(f"unknown hub key {key}", file=sys.stderr)
        return 2
    n = load_narrative(args.path)
    if n is None:
        print("cannot read narrative", file=sys.stderr)
        return 2
    errors = check_against_data(key, n, records, raw_index, mm_index, per_asin_dir=args.per_asin_dir)
    for e in errors:
        print("NG", e)
    print("OK" if not errors else f"{len(errors)} errors")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
