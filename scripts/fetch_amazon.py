"""Amazon 商品データ取得スクリプト (Creator API クライアント)

🚨 重要 — 使用 API: **Amazon Creator API (creatorsapi.amazon)**
  - PA-API 5 (webservices.amazon.co.jp/paapi5/...) では **ない**。
  - PA-API 5 公式ドキュメントの resource / endpoint / レスポンス仕様は流用不可。
  - OAuth 2.0 Client Credentials 認証。実装は ``scripts/creators_api_client.py``。

エンドポイント:
  - searchItems: ``POST /catalog/v1/searchItems`` → 応答は ``searchResult.items``
  - getItems:    ``POST /catalog/v1/getItems``    → 応答は ``itemsResult.items``

resource 配列の真実の源:
  本ファイルの ``SEARCH_ITEM_RESOURCES`` 定数。これを変更する PR は
  ``04-validate-article-pr.yml`` の **Creator API resources dry-run gate** で
  ``scripts/fetch_amazon_dry_run.py`` が実 API に 1 keyword × 1 item の
  searchItems を投げて HTTP 200 + items[] != [] を確認する (Issue #785)。

有効/無効が分かっている resource (2026-05-26 時点):
  ✅ 有効: images.primary.large / images.variants.large /
          itemInfo.{title,features,externalIds,productInfo} /
          offersV2.listings.{price,availability,loyaltyPoints,merchantInfo}
  ❌ 無効: offersV2.listings.deliveryInfo
          (PR #761 で投入 → 全 keyword 400 で cron 死亡 → PR #783 hotfix で削除)
  ⚠ Creator API では getItems と searchItems で valid set が異なる可能性あり
    (deliveryInfo trap)。新規 resource は必ず dry-run gate (Issue #785) を通すこと。
    itemInfo.productInfo は getItems で実用済 + dry-run gate で searchItems も OK
    と確認済 (PR #795 で導入)。

関連 trap:
  [[feedback-omochairo-creators-api-deliveryinfo-trap]] —
  Creator API は PA-API 5 と response schema / resource 名が異なる。
  resource を追加する前に必ず Creator API 公式ドキュメントで確認すること。
"""
import os
import re
import json
import pathlib
import sys
import time
import random
import logging
import argparse
from datetime import datetime, timezone
from typing import Any, Optional

import _fetch_targets
import price_history
import image_dimensions
from genre_gate import classify_genre

# #795 follow-up (session 62): 旧 DEFAULT_KEYWORDS は 10 件中 4 件が王道
# (知育玩具/木のおもちゃ/パズル/レゴ) で、これらは Amazon search の top 100 が
# 半年単位で動かないため毎回同じ既存 ASIN を吐き出す = 95% 重複の元凶だった。
# v2 (本リスト) では王道を **完全削除** し、ニッチ寄り 240+ 件に拡張。
#
# PA-API searchItems は 1 keyword あたり最大 100 件 (10 件 × 10 page) の構造的
# 上限がある。keyword 数を増やしても 1 keyword の top 100 を超えた SKU は永遠に
# 拾えないので、本リストは「discovery のロングテール側」を担う設計。網羅性
# (Amazon ベストセラー全件カバー) は別経路 (Issue #810: bestseller → sniper
# getItems pipeline) で担保する予定。
#
# 構成 (約 240 件):
#   A. 海外ブランド (木製/教育/構築/アクション/STEAM)
#   B. 国内ブランド (大手/中堅/伝統工芸)
#   C. 年齢 × 主要カテゴリ (1-6 歳 × 6 カテゴリ)
#   D. 教育メソッド (モンテッソーリ/シュタイナー/etc + STEAM)
#   E. 発達課題 (微細運動/数概念/形認識/etc)
#   F. おもちゃ種別 (積み木/型はめ/スロープ/etc)
#   G. 素材 (木製/布製/シリコン/etc)
#   H. ギフト/シーン (誕生日/出産祝い/孫プレゼント/etc)
#   I. LEGO エデュケーション系サブ (デュプロ/クラシック/教育版のみ)
#   J. 楽器系 (リトミック/木琴/etc)
#   K. ごっこ遊び 知育
#   L. 言語/国際
DEFAULT_KEYWORDS = [
    # A. 海外ブランド (木製/教育)
    "ボーネルンド", "Hape", "PlanToys", "BRIO", "HABA", "kiko+",
    "Grimm's", "Ostheimer", "Goki", "Selecta", "Wooden Story", "Tegu",
    # A. 海外ブランド (構築/ブロック)
    "LaQ", "Magformers", "Geomag", "GraviTrax", "Lasy", "Connetix",
    "Picasso Tiles", "Mobilo", "Tinkertoy", "Smartmax",
    # A. 海外ブランド (アクション/フィギュア)
    "PLAYMOBIL", "Schleich", "Janod", "Vilac",
    # A. 海外ブランド (STEAM/プログラミング)
    "Wonder Workshop", "Osmo", "Snap Circuits", "Cubetto",

    # B. 国内ブランド (教育系大手)
    "エド・インター", "くもん 知育", "学研ステイフル", "ピープル 知育",
    "アガツマ 知育", "ピノチオ", "ローヤル ベビー",
    # B. 国内ブランド (玩具メーカー知育ライン)
    "バンダイ 知育", "セガトイズ 知育", "MEGA HOUSE 知育",
    # B. 国内ブランド (中堅/伝統工芸)
    "ジョリーキッズ", "ニチガン 知育", "三進 木のおもちゃ", "平和工業 知育",
    "IKONIH", "MOCCO 木", "ノーチェ 知育", "銀鳥産業 知育",
    "BorneLund Japan", "ハバ ジャパン", "クムン", "ヤマト工芸 知育",
    "デザインレーベル 木のおもちゃ", "ファルマトイ", "ベルニーニ おもちゃ",

    # C. 年齢 × カテゴリ (1-6 歳 × 6 カテゴリ)
    "1歳 ブロック", "1歳 パズル", "1歳 木 おもちゃ", "1歳 絵本", "1歳 楽器", "1歳 型はめ",
    "2歳 ブロック", "2歳 パズル", "2歳 木 おもちゃ", "2歳 絵本", "2歳 楽器", "2歳 型はめ",
    "3歳 ブロック", "3歳 パズル", "3歳 木 おもちゃ", "3歳 絵本", "3歳 楽器", "3歳 ボードゲーム",
    "4歳 ブロック", "4歳 パズル", "4歳 木 おもちゃ", "4歳 絵本", "4歳 ボードゲーム", "4歳 STEAM",
    "5歳 ブロック", "5歳 パズル", "5歳 ボードゲーム", "5歳 STEAM", "5歳 プログラミング", "5歳 算数",
    "6歳 パズル", "6歳 ボードゲーム", "6歳 STEAM", "6歳 プログラミング", "6歳 算数", "6歳 工作",

    # D. 教育メソッド
    "モンテッソーリ", "シュタイナー おもちゃ", "ピクラー", "ヴァルドルフ",
    "レッジョ・エミリア おもちゃ", "OKIDOKI 知育", "STEAM 知育",
    "SDGs おもちゃ", "プログラミング おもちゃ", "ScratchJr",
    "Bee-Bot", "ロボット おもちゃ",

    # E. 発達課題
    "微細運動 おもちゃ", "粗大運動 おもちゃ", "感覚統合 おもちゃ",
    "五感 おもちゃ", "因果関係 おもちゃ", "数概念 おもちゃ",
    "形認識 おもちゃ", "色覚 おもちゃ", "文字認識 おもちゃ",
    "鏡 知育", "集中力 おもちゃ", "記憶力 ゲーム",
    "言語発達 おもちゃ", "空間認識 おもちゃ", "ロジック おもちゃ",
    "創造性 おもちゃ", "問題解決 おもちゃ", "想像力 おもちゃ",

    # F. おもちゃ種別 (40 件)
    "積み木", "型はめ", "スロープ おもちゃ", "ハンマートイ",
    "スタッキング おもちゃ", "リングタワー", "ペグさし", "ビーズコースター",
    "紐通し", "ボール落とし", "形合わせ", "パズルボックス",
    "引き車 木", "知育時計", "バランスゲーム", "マグネット ボード",
    "ジオボード", "あいうえお ボード", "アルファベット 知育", "タングラム",
    "ソロバン 子供", "ボードゲーム 子供", "カードゲーム 子供", "ドミノ おもちゃ",
    "ジェンガ 子供", "パターンブロック", "ナンプレ 子供", "シール 知育",
    "シールブック", "図形 パズル", "知育絵本", "仕掛け絵本",
    "さわる絵本", "数字 おもちゃ", "算数 ボード", "大型ブロック",
    "ソフトブロック", "マグネットブロック", "ロディ おもちゃ", "ベビージム 木",

    # G. 素材
    "木製 知育", "布製 おもちゃ", "シリコン 歯固め 知育",
    "EVA おもちゃ", "フェルト おもちゃ", "紙パズル",
    "厚紙 絵本", "オーガニック おもちゃ", "無塗装 木", "竹 おもちゃ",

    # H. ギフト/シーン
    "誕生日 プレゼント 子供 知育", "出産祝い 知育", "入園祝い",
    "入学祝い 知育", "七五三 プレゼント 知育", "初節句 おもちゃ",
    "クリスマス 知育", "お正月 おもちゃ", "帰省 手土産 おもちゃ",
    "病院 おもちゃ", "旅行 おもちゃ 子供", "室内 おもちゃ 子供",
    "雨の日 おもちゃ", "静かに遊ぶ おもちゃ", "一人遊び 知育",
    "兄弟 で遊ぶ おもちゃ", "孫 プレゼント 知育", "姪 プレゼント 知育",
    "甥 プレゼント 知育", "ハーフバースデー おもちゃ",

    # I. LEGO エデュケーション系 (王道 LEGO は除外、教育系サブのみ)
    "LEGO デュプロ", "LEGO クラシック", "LEGO エデュケーション",
    "LEGO アイデア", "LEGO デュプロ 動物", "LEGO デュプロ 街",
    "LEGO 数字", "LEGO アルファベット",

    # J. 楽器系
    "知育 楽器", "子供 ピアノ おもちゃ", "子供 ドラム",
    "タンバリン 子供", "シロフォン 木", "鉄琴 子供",
    "木琴 子供", "カリンバ 子供", "リトミック おもちゃ",
    "ハンドベル 子供", "トライアングル 子供", "リズム おもちゃ",

    # K. ごっこ遊び 知育
    "キッチン おもちゃ 木", "レジスター おもちゃ", "お医者さん 知育",
    "クッキング おもちゃ 知育", "お店屋さん 知育", "病院ごっこ",
    "美容師 ごっこ", "工具 おもちゃ 木", "ごっこ遊び セット",
    "知育 ぬいぐるみ", "抱き人形 教育", "食育 おもちゃ",

    # L. 言語/国際
    "英語 知育", "バイリンガル 知育", "タッチペン 英語",
    "フォニックス おもちゃ", "多言語 おもちゃ", "中国語 子供 知育",
    "スペイン語 子供 知育", "数字 英語 おもちゃ",
]


def parse_keywords(cli_value: Optional[str], shuffle: bool = False, sample_size: int = 0) -> list:
    """CLI/ENV のキーワード文字列を list に。

    優先順: --keywords (CSV/改行) > $AMAZON_SEARCH_KEYWORDS > DEFAULT_KEYWORDS

    ``shuffle=True`` のとき返り値をシャッフルし、``sample_size > 0`` のとき先頭
    ``sample_size`` 個を返す (#795: 王道偏重の新規 ASIN ヒット率低下を緩和)。
    """
    raw = cli_value if cli_value else os.environ.get("AMAZON_SEARCH_KEYWORDS", "")
    if not raw or not raw.strip():
        kws = list(DEFAULT_KEYWORDS)
    else:
        kws = [k.strip() for k in re.split(r"[,\n]", raw) if k.strip()]
    if shuffle:
        random.shuffle(kws)
    if sample_size > 0:
        kws = kws[:sample_size]
    return kws

def load_demand_keywords(path: str, slots: int) -> list:
    """需要側キーワード (#2686) を imp 上位から slots 件返す。

    data/demand_keywords.json は build_demand_keywords.py の出力で、omcha.jp (WP 本家)
    の GSC 実需要を Amazon 検索語に変換したもの。既に (1) Amazon 非販売品と実店舗名の
    除外、(2) 供給 probe で 0 件だった情報クエリの除去、を通してあるので、ここでは
    上から取るだけでよい。

    なぜ必要か (2026-08-10 実測): DEFAULT_KEYWORDS は 231 件の**供給側**リストで
    「どんなおもちゃが存在するか」から引いている。結果 navi は 554 記事中 417 (75%) が
    平均 10 位以内なのにサイト全体で 376 impressions/日 しかない。誰も検索しない語で
    1 位を取っている状態。

    slots<=0・ファイル無し・壊れている場合は空リストを返す (従来動作のまま)。
    """
    if slots <= 0 or not path:
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"demand keywords unreadable ({path}): {e}; falling back to supply-side only")
        return []
    out = []
    for e in (data.get("keywords") or []):
        kw = e.get("keyword") if isinstance(e, dict) else None
        if isinstance(kw, str) and kw.strip():
            out.append(kw.strip())
        if len(out) >= slots:
            break
    return out


def get_secret(name: str) -> str:
    return os.environ.get(name)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("fetch_amazon")

# Creator API searchItems / getItems の resources 配列。
# session 60 で PR #761 が "offersV2.listings.deliveryInfo" を入れて Creator API
# が全 keyword で 400 を返し cron が死亡 (PR #783 hotfix で復旧)。再発防止として
# 04-validate-article-pr.yml が scripts/fetch_amazon.py / creators_api_client.py
# を触る PR で scripts/fetch_amazon_dry_run.py を呼んで実 API への 1 リクエストで
# 200 OK + items[] を確認する (Issue #785)。
#
# 記事 ASIN 巡回 (2026-07-07) の GetItems で browseNodeInfo を追加していたが、
# 2026-07-08 (#2823 前倒し) から search sweep でも同じ resources を使う。理由:
# SearchIndex=Toys でも他カテゴリ商品が混入する実例 (B0GJXXRWM5=車＆バイク、
# B002KLX798=Clover 糸通し 手芸用品) があり、#2823 のジャンル不一致検査ゲート
# (scripts/genre_gate.py) を「search で拾った時点」で効かせるには search 応答
# 自体に browse_nodes が要る (記事化されてからの棚卸しだけでは供給元を絞れない)。
# browseNodeInfo.browseNodes / .ancestor は creators_api_client.get_items /
# search_items 両方のデフォルト resources に含まれており Creator API で valid
# (2026-07-08 時点 per_asin 207 snapshot で ancestor 取得を実証済)。.ancestor が
# 無いと ancestor チェーンが返らず extract_browse_nodes の root が全件 null に
# なり、ジャンル検査が成立しない (indeterminate 直行で fail-open のまま)。
# 注意: invalid resource だと fetch 全体が落ちる前例あり (PR #761→hotfix #783)。
# 再発防止として 04-validate-article-pr.yml の dry-run gate (Issue #785) が PR CI
# で SEARCH_ITEM_RESOURCES を searchItems に投げて 200 + items[] != [] を確認する。
# マージ後も workflow_dispatch で 1 run 実証し、raw に "ancestor" が入ること・
# snapshot の root が非 null になることを確認すること。
SEARCH_ITEM_RESOURCES = [
    "images.primary.large",
    "images.variants.large",
    "itemInfo.title",
    "itemInfo.features",
    "itemInfo.externalIds",
    # #795: parentAsin を取得してバリエーション (色違い/サイズ違い) 重複を排除する。
    # creators_api_client.get_items のデフォルト resources に同名 ("itemInfo.productInfo")
    # が含まれているので Creator API の getItems では valid と判明済。searchItems も
    # 同じ resource 名で valid なはず — 04-validate の dry-run gate (Issue #785)
    # が PR で 200 + items[] != [] を確認する。
    "itemInfo.productInfo",
    "offersV2.listings.price",
    "offersV2.listings.availability",
    "offersV2.listings.loyaltyPoints",
    # Buy Box の出品者名を取得して転売 ASIN を弾くために使う。
    # is_trusted_seller(seller) が False になる ASIN は items_for_jules
    # から除外される (search mode)。
    "offersV2.listings.merchantInfo",
    # PR #761 で "offersV2.listings.deliveryInfo" を追加したが Creator API では
    # invalid だったため hotfix PR #783 で削除。Issue #784 (本 PR) で代替案として
    # extract_free_shipping を merchantInfo.name == Amazon 直販 ベースに切替済
    # (Amazon 直販なら Prime/2,000円以上 free shipping が保証されるため実質同義)。
    # #2823 前倒し (2026-07-08): search sweep でもカテゴリ判定を可能にするため
    # browseNodeInfo を追加。上のブロックコメント参照。
    "browseNodeInfo.browseNodes",
    "browseNodeInfo.browseNodes.ancestor",
]

# 変数名は互換のため残す (記事 ASIN 巡回の GetItems 用途で参照している箇所がある)。
# #2823 前倒しで SEARCH_ITEM_RESOURCES 自体に browseNodeInfo を統合したので、
# 現在は SEARCH_ITEM_RESOURCES と完全に同一。
REFRESH_ITEM_RESOURCES = SEARCH_ITEM_RESOURCES

# 記事 ASIN 巡回で GetItems 応答から連続でこの回数欠落したら、その ASIN は
# Amazon から消滅した (dp が 404) とみなし snapshot に status="gone" を書く。
# 1 回の欠落は API の一時不調・throttle と区別できないため即断しない。
# 未確定 (miss_count < threshold) の ASIN は mark_queried せず次 run で再チェック
# するので、fetch-data 1日2run なら 半日〜1日で確定する。
GONE_MISS_THRESHOLD = 2

HAS_CREATORS_API = False
try:
    from creators_api_client import CreatorsAPIClient
    HAS_CREATORS_API = True
except ImportError as e:
    logger.warning(f"creators_api_client module not found or import failed: {e}. Falling back to mock data generation.")

def _safe_get(obj: dict, *attrs: str, default: Any = None) -> Any:
    cur = obj
    for a in attrs:
        if cur is None: return default
        if isinstance(cur, dict):
            cur = cur.get(a)
        else:
            cur = getattr(cur, a, None)
    return cur if cur is not None else default

def extract_features(item: dict) -> list:
    return _safe_get(item, "itemInfo", "features", "displayValues", default=[])

def extract_jan(item: dict) -> str:
    """Return the first external identifier (EAN/ISBN/UPC) from externalIds.

    Requires the `itemInfo.externalIds` resource to be requested. Falls back
    eans → isbns → upcs so書籍 (ISBN13) や輸入品 (UPC12) でも識別子を拾える
    (#2747: backfill_jan_codes.extract_jan_from_item と挙動を一致させる)。
    Returns "" when none is registered, which the downstream cross-search
    treats as "JAN unavailable → fall back to text search".
    """
    for key in ("eans", "isbns", "upcs"):
        values = _safe_get(item, "itemInfo", "externalIds", key, "displayValues", default=[]) or []
        if values and isinstance(values[0], str) and values[0].strip():
            return values[0].strip()
    return ""

def extract_price(item: dict) -> int:
    listings = _safe_get(item, "offersV2", "listings", default=[])
    if listings and len(listings) > 0:
        money = _safe_get(listings[0], "price", "money", default={})
        return int(money.get("amount", 0))
    return 0

def extract_availability(item: dict) -> str:
    listings = _safe_get(item, "offersV2", "listings", default=[])
    if listings:
        msg = _safe_get(listings[0], "availability", "message")
        if isinstance(msg, str):
            return msg.strip()
    return ""


# #784: Amazon 直販 (= 実質送料無料) と判定する merchant 名パターン。
# Amazon 直販は Prime / 2,000円以上で送料無料が保証されるため、
# Buy Box の merchant がここに合致するとき free_shipping=True とする。
# 第三者セラーや TRUSTED_SELLER_PATTERNS のメーカー直販店は各店舗ごとに
# 送料ポリシーが異なるため対象外 (false-positive を避ける目的)。
AMAZON_DIRECT_SELLER_PATTERNS = (
    "amazon.co.jp",
    "amazon japan",
    "amazonアウトレット",
    "アマゾン",  # アマゾンジャパン G.K. を含む
)


def extract_free_shipping(item: dict) -> bool:
    """offersV2.listings.merchantInfo.name が Amazon 直販なら送料無料判定 True。

    #784 / PR #761 で導入した `offersV2.listings.deliveryInfo.isFreeShippingEligible`
    は Creator API では invalid resource で 400 を返し cron が死亡 (hotfix
    PR #783 で削除済)。代替案として既に valid な `offersV2.listings.merchantInfo`
    の name を検査し、Amazon 直販系のとき True を返す (Amazon 直販は Prime /
    2,000円以上で送料無料保証)。

    検出失敗 / 第三者セラー / 取得不可は False (バッジ非表示) にフォールバック。
    """
    seller = extract_seller(item).lower()
    if not seller:
        return False
    return any(p in seller for p in AMAZON_DIRECT_SELLER_PATTERNS)

def extract_loyalty_points(item: dict) -> int:
    listings = _safe_get(item, "offersV2", "listings", default=[])
    if listings:
        pts = _safe_get(listings[0], "loyaltyPoints", "points")
        try:
            return int(pts) if pts is not None else 0
        except (TypeError, ValueError):
            return 0
    return 0


# 信頼できる出品者 (Buy Box が Amazon 直販 or メーカー直販の場合のみ
# 記事化候補にする)。第三者セラーが Buy Box を取っている ASIN は転売価格の
# プレミアム上乗せが多発するため、article pool から除外する。
TRUSTED_SELLER_PATTERNS = (
    "amazon.co.jp",
    "amazon japan",
    "アマゾン",                       # アマゾンジャパン G.K.
    "任天堂",                         # 任天堂直販 (漢字)
    "ニンテンドー",                   # 任天堂直販 (カタカナ)
    "nintendo",
    "タカラトミー",
    "takara tomy",
    "バンダイ",                       # BANDAI
    "bandai",
    "エポック",                       # エポック社
    "epoch",
    "セガトイズ",
    "sega",
    "lego",                          # レゴジャパン
    "学研",
    "くもん",
    "公文",
    "kumon",
    "ボーネルンド",
    "borne",
    "woodypuddy",                    # ウッディプッディ
    "ウッディプッディ",
    "マザーガーデン",
    "シャオール",
    "すごろくや",
    "エジソン",                      # エジソンママ / エジソンブレイン
    "jelly jelly games",
    "人形屋ホンポ",
    "人形の伏見屋",
    "長生堂",
    "エデュテ",
    "amazonアウトレット",
    "トイスタジアム",
    "だいわ",                        # 木製おもちゃのだいわ
    "ぷりふあ",                      # ぷりふあ人形
    "rufas",                         # Rufas オンラインストア
    "sora",                          # 木のおもちゃと雑貨 Sora
    "おもちゃの森",                  # 本の森おもちゃの森
    "mamimami",                      # mamimami home
    "木玩社",
    "lotus life",                    # LOTUS LIFE
    "joyreal",                       # Joyreal
    "jasonwell",                     # Jasonwell
    "kaekid",                        # KaeKid
    "plafarm",                       # plafarm
    "駿河屋",                        # 大手ホビー/中古チェーン (#817 follow-up)
    # 2026-05-30 (#740): data-driven 追加。`scripts/analyze_reseller_drops.py`
    # で per_asin 2192 ASIN を集計したところ以下が untrusted top-10 に出現、
    # かつ正規流通として裏取り可能だったため TRUSTED 化。
    "amazon us",                     # Amazon 海外直販 (52 ASIN)
    "ナビッピ",                      # ナビッピドットコム (老舗知育玩具店, 12 ASIN)
    "ティーズマルシェ",              # シルバニア専門の老舗 (12 ASIN)
    "人と木",                        # 木製節句飾り 人と木 (13 ASIN)
)


def extract_seller(item: dict) -> str:
    """offersV2.listings.merchantInfo.name から Buy Box の出品者名を取り出す。

    PA-API resources に `offersV2.listings.merchantInfo` を要求している場合
    のみ取得できる。未指定または取得失敗時は空文字。
    """
    listings = _safe_get(item, "offersV2", "listings", default=[])
    if listings:
        name = _safe_get(listings[0], "merchantInfo", "name")
        if isinstance(name, str):
            return name.strip()
    return ""


# 汎用 legitimate-seller heuristic 標識 (#817):
# seller 名にこれらの語が含まれていれば、TRUSTED_SELLER_PATTERNS の個別
# 列挙に頼らず信頼扱いとする。enumeration の whack-a-mole を避ける目的。
# - 「公式 / 直営 / メーカー」: 自己宣言の正規/直販ルート
# - 「専門店」: ジャンル専門店 (誠文堂書店, 家電専門店エコアース 等)
# - 「株式会社 / (株) / Co., Ltd / Co.,Ltd」: 法人実在の標識
# - 「インボイス / 適格請求書 / 発行事業者」: 適格事業者登録あり (法人登録要)
# - 「商標登録」: 商標保有 = 法人実在
TRUSTED_SELLER_GENERIC_MARKERS = (
    "公式",
    "直営",
    "メーカー",
    "専門店",
    "株式会社",
    "合同会社",                       # #817 follow-up: LLC も法人実在標識
    "(株)",
    "（株）",
    "co., ltd",
    "co.,ltd",
    "直売",                           # #817 follow-up: 直売店 = 直営と同義の直販標識
    "インボイス",
    "適格請求書",
    "発行事業者",
    "商標登録",
    # 2026-05-30 (#740): メーカーから流通契約を結んだ自己宣言。完全な保証では
    # ないが、契約違反リスクを背負ってまで偽る reseller は稀。untrusted top-30
    # に「embot正規取扱店」「bebicia正規販売店」「わんぱく堂」等 6 件以上出現。
    "正規取扱店",
    "正規販売店",
)


def is_trusted_seller(seller: str) -> bool:
    """seller 名が信頼セラーに該当するか。

    判定順:
      1. 空文字 → False (PA-API merchantInfo 取得失敗等は信頼不能扱い)
      2. TRUSTED_SELLER_PATTERNS に部分一致 → True (well-known brand)
      3. TRUSTED_SELLER_GENERIC_MARKERS に部分一致 → True (#817: 汎用 heuristic)
    """
    if not seller:
        return False
    s = seller.lower()
    if any(pat in s for pat in TRUSTED_SELLER_PATTERNS):
        return True
    if any(marker in s for marker in TRUSTED_SELLER_GENERIC_MARKERS):
        return True
    return False


def is_low_stock_reseller_signal(availability: str) -> bool:
    """`残り\\d+点` などの限定在庫表記は転売出品の典型シグナル。

    Amazon 直販 / メーカー直販は通常潤沢な在庫を持つため低在庫メッセージは
    出ない。`カートに追加できます` / `通常配送無料` 等の標準表記は False。

    #817 緩和:
      - 「入荷予定あり」付き → 補充確約で健全在庫 (False)
      - 残り N 点 で N >= 10 → 直販でも一時的に低在庫になる場合あり (False)
        転売出品は通常 5 点以下のフリッピング在庫が典型。
    """
    if not availability:
        return False
    # 補充確約: 「残り N 点（入荷予定あり）」等は健全
    if "入荷予定あり" in availability:
        return False
    m = re.search(r"残り\s*(\d+)\s*点", availability)
    if m:
        try:
            remaining = int(m.group(1))
        except ValueError:
            return True
        # 10 点以上は正規在庫の transient 低在庫として扱う
        return remaining < 10
    if "在庫わずか" in availability:
        return True
    return False


def _load_asin_blocklist(path: str = "data/asin_blocklist.json") -> set:
    """data/asin_blocklist.json から ASIN ブロックリストを読む。

    ファイル形式: {"blocked": [{"asin": "B0G398BYV6", "reason": "..."}]}
    取り扱いはセッション中に動的: 検出済みの転売 ASIN を恒久ブロックする。
    """
    if not os.path.exists(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        blocked = data.get("blocked", []) if isinstance(data, dict) else []
        return {entry["asin"] for entry in blocked if isinstance(entry, dict) and entry.get("asin")}
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"Failed to load ASIN blocklist {path}: {e}")
        return set()


# #3314: 画像実寸取得の CDN への配慮レート (1 リクエスト/秒以下)。
IMAGE_DIMENSIONS_FETCH_INTERVAL_SEC = 1.0


def _attach_image_dimensions(out_root: str, item: dict) -> None:
    """``item`` に ``image_width`` / ``image_height`` を付与する (#3314)。

    既存 snapshot と ``image`` URL が一致すればキャッシュとして再利用し、
    ネットワークを叩かない (通常運用では画像 URL が変わったときのみ 1 回
    叩けば足りる)。取得できない場合はキー自体を付けない fail-soft。
    """
    image_url = item.get("image")
    if not isinstance(image_url, str) or not image_url:
        return
    asin = item.get("asin")
    existing_path = (
        os.path.join(out_root, "per_asin", asin, "amazon.json") if asin else None
    )
    if existing_path and os.path.exists(existing_path):
        try:
            with open(existing_path, "r", encoding="utf-8") as f:
                prev = json.load(f)
            prev_item = prev.get("item") if isinstance(prev, dict) else None
            if isinstance(prev_item, dict) and prev_item.get("image") == image_url:
                w, h = prev_item.get("image_width"), prev_item.get("image_height")
                if isinstance(w, int) and isinstance(h, int) and w > 0 and h > 0:
                    item["image_width"], item["image_height"] = w, h
                    return
        except (OSError, json.JSONDecodeError):
            pass
    dims = image_dimensions.fetch_image_dimensions(image_url)
    time.sleep(IMAGE_DIMENSIONS_FETCH_INTERVAL_SEC)
    if dims:
        item["image_width"], item["image_height"] = dims


def write_per_asin_snapshot(out_root: str, item: dict) -> None:
    """Persist per-ASIN amazon snapshot so build_post.py can back-fill badge
    fields for past articles even after data/raw/amazon.json gets overwritten.

    #2953 C案 (訂正版): search sweep / sniper / 記事巡回 (refresh) の 3 経路が
    すべてここを通る単一チョークポイントなので、スナップショット書き込み成功後に
    price_history.append_price_point で自前の append-only 価格履歴も蓄積する
    (追加 API 呼び出しはゼロ)。price_history 側はベストエフォートで例外を握り
    つぶすため、ここが失敗してもフェッチ本体は止まらない。

    #3314: 同じチョークポイントで image_width/image_height も付与する
    (キャッシュ優先・fail-soft。詳細は _attach_image_dimensions 参照)。
    """
    asin = item.get("asin")
    if not asin:
        return
    _attach_image_dimensions(out_root, item)
    per_asin_dir = os.path.join(out_root, "per_asin", asin)
    try:
        os.makedirs(per_asin_dir, exist_ok=True)
        snapshot = {
            "asin": asin,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "item": item,
        }
        with open(os.path.join(per_asin_dir, "amazon.json"), "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.warning(f"Failed to write per_asin snapshot for {asin}: {e}")
        return
    price_history_dir = os.path.join(str(pathlib.Path(out_root).parent), "price_history")
    price_history.append_price_point(
        price_history_dir, asin, "amazon", item.get("price"), item.get("availability"))


def _is_competitors_cache_fresh(out_root: str, asin: str, ttl_days: float) -> bool:
    """``per_asin/<ASIN>/competitors.json`` が ``ttl_days`` 以内に書かれていれば True。

    Issue #792: 既存記事 ASIN の毎週 SearchItems 再検索 (= 旧挙動) は
    PA-API quota / 待機時間ともに大きな浪費だった。``fetched_at`` を見て
    TTL 以内ならスキップする。``ttl_days <= 0`` は常に False (= 必ず refresh)
    で、sniper / backfill / 旧挙動と等価。
    """
    if ttl_days <= 0:
        return False
    path = os.path.join(out_root, "per_asin", asin, "competitors.json")
    if not os.path.exists(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        ts = data.get("fetched_at") if isinstance(data, dict) else None
        if not ts:
            return False
        # ISO 8601 with 'Z' suffix written by ``datetime.now(timezone.utc).isoformat()``
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        age = datetime.now(timezone.utc) - dt
        return age.total_seconds() < ttl_days * 86400
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return False


def _load_published_snapshots_pool(out_root: str, covered_asins: set, limit: int = 500) -> list:
    """既掲載 ASIN の per_asin/<ASIN>/amazon.json (=snapshot) を candidate 形式に整形。

    内部リンク floor 用のプール。Plan D の SearchItems 結果と合流して
    ranking にかけるため、`items[]` と同じ shape にして返す。
    """
    pool: list = []
    per_asin_root = os.path.join(out_root, "per_asin")
    if not os.path.isdir(per_asin_root):
        return pool
    for asin in covered_asins:
        path = os.path.join(per_asin_root, asin, "amazon.json")
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                snap = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(snap, dict) and snap.get("status") == "gone":
            continue  # Amazon から消滅した商品を競合候補に出さない
        item = snap.get("item") if isinstance(snap, dict) else None
        if not isinstance(item, dict) or not item.get("asin") or not item.get("image"):
            continue
        pool.append(item)
        if len(pool) >= limit:
            break
    return pool


def _search_competitor_pool(api, target_item: dict, search_index: str, resources: list, max_n: int = 10) -> list:
    """target の title からキーワードを組み立てて SearchItems を 1 回叩く。

    Plan D の本体: Amazon の検索 relevance に「似た商品」の判断を任せる。
    失敗時は空 list を返して呼び出し側で Plan B フォールバックに繋ぐ。
    """
    from competitor_ranking import build_search_keyword
    kw = build_search_keyword(target_item.get("title") or "")
    if not kw:
        return []
    tag = get_secret("AMAZON_PARTNER_TAG") or ""
    try:
        res = api.search_items(keywords=kw, search_index=search_index, item_count=max_n, item_page=1, resources=resources)
    except Exception as e:
        logger.warning(f"  SearchItems failed for target {target_item.get('asin')} kw='{kw}': {e}")
        return []
    found = _safe_get(res, "searchResult", "items", default=[]) or []
    pool: list = []
    for it in found:
        asin = it.get("asin")
        if not asin:
            continue
        pool.append({
            "asin": asin,
            "title": _safe_get(it, "itemInfo", "title", "displayValue"),
            "price": extract_price(it),
            "features": extract_features(it),
            "url": f"https://www.amazon.co.jp/dp/{asin}/?tag={tag}" if tag else f"https://www.amazon.co.jp/dp/{asin}/",
            "image": _safe_get(it, "images", "primary", "large", "url") or _safe_get(it, "images", "primary", "medium", "url"),
        })
    return pool


def write_per_asin_competitors(
    out_root: str, target_item: dict, fallback_pool: list,
    api=None, search_index: str = "Toys", resources: list = None,
    covered_asins: set = None, max_n: int = 5,
    published_pool: list = None,
    cache_ttl_days: float = 0.0,
) -> None:
    """Plan D+ 実装: target ごとに SearchItems を打って候補プールを作り、
    自社既掲載 ASIN の snapshot プールと統合してから類似度ランキングする。

    旧実装は ``items`` をそのまま先頭から N 件取っていたため、全 target が
    同じ items[0..2] を競合に焼き付けていた (= 78% のレポート濃度問題)。
    Plan D+ は (1) Amazon 検索の relevance、(2) 価格距離、(3) 自社内部リンク
    の 3 シグナルを target ごとに合成して並べ替える。

    ``published_pool`` は呼び出し側で 1 度だけビルドした内部リンク用プール。
    省略時はターゲットごとに ``_load_published_snapshots_pool`` を呼ぶ
    (= N*M ファイル open) ので、カタログが大きいときは必ず渡すこと。

    ``cache_ttl_days > 0`` のとき、既存 ``competitors.json`` が TTL 以内なら
    SearchItems と ``time.sleep(1.1)`` ごとスキップする (#792)。sniper /
    backfill では 0 のまま呼んで強制 refresh を維持する。
    """
    from competitor_ranking import (
        dedupe_candidates, rank_candidates, ensure_internal_link_floor,
    )
    target_asin = (target_item or {}).get("asin")
    if not target_asin:
        return
    # #792: 既存記事 ASIN の毎週 SearchItems 再検索を TTL でスキップ。
    # 関数全体を早期 return することで _search_competitor_pool と
    # time.sleep(1.1) の両方を省ける (= 200 件で約 4 分削減)。
    if _is_competitors_cache_fresh(out_root, target_asin, cache_ttl_days):
        logger.info(f"  skip competitors refresh for {target_asin}: cache < {cache_ttl_days}d")
        return
    covered_asins = covered_asins or set()
    # Pool 1: SearchItems (Plan D の本体)
    search_pool: list = []
    if api is not None:
        search_pool = _search_competitor_pool(api, target_item, search_index, resources or [])
        time.sleep(1.1)  # PA-API TPS=1 safety margin
    # Pool 2: 自社既掲載 ASIN の snapshot (内部リンク floor 用)
    if published_pool is None:
        article_pool = _load_published_snapshots_pool(out_root, covered_asins - {target_asin})
    else:
        article_pool = [it for it in published_pool if it.get("asin") != target_asin]
    # Pool 3: 旧来の amazon.json プール (Plan B フォールバック)
    candidates = dedupe_candidates([search_pool, article_pool, fallback_pool or []], exclude_asin=target_asin)
    if not candidates:
        return
    ranked = rank_candidates(target_item, candidates, covered_asins)
    top = ensure_internal_link_floor(
        ranked, covered_asins, total=max_n, min_internal=1, target=target_item,
    )
    competitors = [{
        "asin": c["asin"],
        "name": c.get("title") or c.get("name") or "",
        "image": c.get("image") or "",
        "price": c.get("price") or 0,
        "url": c.get("url") or f"https://www.amazon.co.jp/dp/{c['asin']}/",
        "features": (c.get("features") or [])[:3],
    } for c in top if c.get("asin") and c.get("image")]
    if not competitors:
        return
    per_asin_dir = os.path.join(out_root, "per_asin", target_asin)
    try:
        os.makedirs(per_asin_dir, exist_ok=True)
        payload = {
            "asin": target_asin,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "competitors": competitors,
            "source": "plan_d_plus" if search_pool else "fallback",
        }
        with open(os.path.join(per_asin_dir, "competitors.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        n_internal = sum(1 for c in competitors if c["asin"] in covered_asins)
        logger.info(f"Wrote {len(competitors)} competitors for {target_asin} (internal={n_internal}, source={payload['source']})")
    except OSError as e:
        logger.warning(f"Failed to write competitors for {target_asin}: {e}")


def _load_existing_article_asins(articles_dir: str = "data/articles") -> set:
    """Return the set of ASINs that already have an article.

    Used to strip already-covered ASINs from amazon.json before Jules reads
    it, so the AI can't pick a duplicate even if it ignores the textual
    "no duplicate ASIN" rule in the prompt.

    Filename suffix is authoritative: every article slug ends with the ASIN
    (e.g. ``2026-05-26-B01MUBACGI.json``), so we can derive the ASIN set
    by regex alone — no JSON parsing required. Skips
    ``.enrichment / .seo / .quality`` companion files.
    """
    asins: set = set()
    if not os.path.isdir(articles_dir):
        return asins
    # Match any 10-char uppercase ASIN suffix, not just ``B0...`` — book ASINs
    # are ISBN-10 (numeric-only, e.g. ``4910762175``) and would otherwise leak
    # past the duplicate filter.
    pattern = re.compile(r"-([A-Z0-9]{10})$")
    for name in os.listdir(articles_dir):
        if not name.endswith(".json"):
            continue
        stem = name[:-5]
        if stem.endswith((".enrichment", ".seo", ".quality")):
            continue
        m = pattern.search(stem)
        if m:
            asins.add(m.group(1))
    return asins


def extract_images(item: dict, max_n: int = 6) -> list:
    """Return up to ``max_n`` large-image URLs: primary first, then variants.

    PA-API ``images.variants`` is an optional array of additional product
    images (sub-cuts, lifestyle, package shots). We surface them so the
    Hugo template can render a small thumbnail strip below the hero image
    and let readers see more than one angle before clicking through.
    """
    urls: list = []
    primary = _safe_get(item, "images", "primary", "large", "url")
    if primary:
        urls.append(primary)
    variants = _safe_get(item, "images", "variants", default=[]) or []
    for v in variants:
        u = _safe_get(v, "large", "url")
        if u and u not in urls:
            urls.append(u)
        if len(urls) >= max_n:
            break
    return urls


def extract_savings_percentage(item: dict) -> int:
    listings = _safe_get(item, "offersV2", "listings", default=[])
    if listings:
        pct = _safe_get(listings[0], "price", "savings", "percentage")
        try:
            return int(pct) if pct is not None else 0
        except (TypeError, ValueError):
            return 0
    return 0


def extract_parent_asin(item: dict) -> Optional[str]:
    """``itemInfo.productInfo`` から parentAsin を取り出す (#795)。

    PA-API 5 でも Creator API でも parentAsin は ``itemInfo.productInfo`` 配下に
    入る (=色違い・サイズ違いバリエーション群の root ASIN)。Creator API の
    レスポンス schema は PA-API 5 と微妙に違うため、ありうる 2 形を両方
    decode する:

      * ``productInfo.parentAsin = "B0XXXXXXXX"`` (フラットな文字列)
      * ``productInfo.parentAsin = {"displayValue": "B0XXXXXXXX"}`` (DisplayValue 包装)

    どちらも取れないときは None を返し、呼び出し側で「dedup skip」と
    fallback させる (= 機能無効 / 旧挙動と等価)。Creator API で parentAsin
    フィールド自体が空のときも cron は止めない。
    """
    pi = _safe_get(item, "itemInfo", "productInfo")
    if not isinstance(pi, dict):
        return None
    raw = pi.get("parentAsin")
    if isinstance(raw, dict):
        val = raw.get("displayValue")
        return val if isinstance(val, str) and val else None
    return raw if isinstance(raw, str) and raw else None


def _load_existing_parent_asins(out_root: str) -> set:
    """過去 run で per_asin/<ASIN>/amazon.json snapshot に保存された
    parent_asin を集めて返す (#795 cross-session dedup)。

    snapshot[item][parent_asin] は本 PR 以降の run で初めて埋められるため、
    初回 cron では空集合 → 段階的に蓄積される。
    """
    parents: set = set()
    per_asin_root = os.path.join(out_root, "per_asin")
    if not os.path.isdir(per_asin_root):
        return parents
    for asin_dir in os.listdir(per_asin_root):
        path = os.path.join(per_asin_root, asin_dir, "amazon.json")
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                snap = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        it = snap.get("item") if isinstance(snap, dict) else None
        if isinstance(it, dict):
            p = it.get("parent_asin")
            if isinstance(p, str) and p:
                parents.add(p)
    return parents

def extract_browse_nodes(item: dict) -> list:
    """browseNodeInfo.browseNodes から [{id, name, root}] を返す。

    root は ancestor チェーンを遡った最上位カテゴリ名 (例: おもちゃ / 車＆バイク)。
    #2823 前倒し (2026-07-08) 以降は SEARCH_ITEM_RESOURCES に browseNodeInfo を
    統合したため search sweep でも取得される。resource 未要求の古い応答では
    空リスト → normalize_api_item はキー自体を省く。
    """
    nodes = _safe_get(item, "browseNodeInfo", "browseNodes", default=[]) or []
    out = []
    for n in nodes:
        if not isinstance(n, dict):
            continue
        root = None
        anc = n.get("ancestor")
        while isinstance(anc, dict):
            root = anc.get("displayName") or root
            anc = anc.get("ancestor")
        out.append({"id": n.get("id"), "name": n.get("displayName"), "root": root})
    return out


def normalize_api_item(it: dict, tag: str, source: str) -> dict:
    """Creator API の item response を amazon.json / snapshot の item 形式に整形。

    sniper / search / 記事巡回 (refresh) の 3 経路で同一 shape を保証する。
    """
    asin = it.get("asin")
    normalized = {
        "asin": asin,
        "title": _safe_get(it, "itemInfo", "title", "displayValue"),
        "price": extract_price(it),
        "features": extract_features(it),
        "jan_code": extract_jan(it),
        "url": f"https://www.amazon.co.jp/dp/{asin}/?tag={tag}",
        "image": _safe_get(it, "images", "primary", "large", "url"),
        "images": extract_images(it),
        "availability": extract_availability(it),
        "loyalty_points": extract_loyalty_points(it),
        "savings_percentage": extract_savings_percentage(it),
        "free_shipping": extract_free_shipping(it),
        "seller": extract_seller(it),
        "parent_asin": extract_parent_asin(it),
        "source": source,
    }
    browse_nodes = extract_browse_nodes(it)
    if browse_nodes:
        normalized["browse_nodes"] = browse_nodes
    return normalized


def _record_snapshot_miss(out_root: str, asin: str) -> int:
    """GetItems 応答から欠落した ASIN の snapshot に miss を記録し、更新後の
    連続 miss 回数を返す。GONE_MISS_THRESHOLD 連続で status="gone" を確定する。

    最後に取得できた item / fetched_at は参考情報としてそのまま保持する
    (build_post の画像 backfill 等が最終既知データを使い続けられるように)。
    ヒット時は write_per_asin_snapshot が {asin, fetched_at, item} で全書きする
    ため miss_count / status は自然にリセットされる (再出品からの復帰経路)。
    """
    path = os.path.join(out_root, "per_asin", asin, "amazon.json")
    snap: dict = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                snap = loaded
        except (OSError, json.JSONDecodeError):
            pass
    snap.setdefault("asin", asin)
    miss = int(snap.get("miss_count") or 0) + 1
    now = datetime.now(timezone.utc).isoformat()
    snap["miss_count"] = miss
    snap["last_checked_at"] = now
    if miss >= GONE_MISS_THRESHOLD:
        snap["status"] = "gone"
        snap.setdefault("gone_since", now)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.warning(f"Failed to record snapshot miss for {asin}: {e}")
    return miss


def refresh_article_snapshots(api, out_root: str, articles_dir: str, tag: str,
                              max_per_run: int, stale_days: int) -> None:
    """既掲載記事 ASIN の Amazon データを stale-first で巡回更新する (2026-07-07)。

    背景: per_asin/<ASIN>/amazon.json snapshot は search sweep / sniper が偶然
    触れた ASIN しか更新されず、記事化済み ASIN の価格・在庫・存在確認は記事
    作成時のまま凍結していた。B0G5DLSTS8 のように Amazon から出品が消えても
    検知できず、記事は古い価格と 404 リンクを出し続けた。

    youtube/news/books と同じ _fetch_targets の stale-first 巡回を GetItems
    (10 ASIN/call) で回す:
      - ヒット → snapshot を最新 item で更新 (価格/在庫バッジも最新化)
      - 欠落 → _record_snapshot_miss で miss を記録、連続 GONE_MISS_THRESHOLD
        回で status="gone" 確定。未確定 miss は mark_queried しない = 次 run で
        即再チェック。
    status="gone" は build_post._apply_amazon_discontinued がビルド時に読んで
    「取り扱い終了」表示に切り替える。

    quota: fetch-data は 1 日 2 run。default cap 110 ASIN/run = +11 GetItems
    call/run (~12秒) で、全記事 ~1420 ASIN を 7 日で一巡する。
    """
    picked = _fetch_targets.pick_target_asins(
        pathlib.Path(out_root), "amazon_refresh", [],
        pathlib.Path(articles_dir),
        max_per_run=max_per_run, stale_after_days=stale_days)
    if not picked:
        logger.info("Article refresh: no stale targets")
        return
    asins = [a for a, _ in picked]
    refreshed = gone_confirmed = pending = 0
    queried: list = []
    for i in range(0, len(asins), 10):
        chunk = asins[i:i + 10]
        if i:
            time.sleep(1.1)  # PA-API TPS=1 safety margin
        try:
            res = api.get_items(chunk, resources=REFRESH_ITEM_RESOURCES)
        except Exception as e:
            logger.warning(f"Article refresh: GetItems failed for {chunk}: {e}")
            continue
        found_items = _safe_get(res, "itemsResult", "items")
        if not isinstance(found_items, list):
            # 応答形が想定外 (throttle 等)。欠落扱いにすると誤 gone 化するので skip。
            logger.warning(f"Article refresh: unexpected response for {chunk}; skipped")
            continue
        found = {it.get("asin"): it for it in found_items if isinstance(it, dict)}
        for asin in chunk:
            it = found.get(asin)
            if it is not None:
                write_per_asin_snapshot(out_root, normalize_api_item(it, tag, "Amazon (Refresh)"))
                refreshed += 1
                queried.append(asin)
            else:
                miss = _record_snapshot_miss(out_root, asin)
                if miss >= GONE_MISS_THRESHOLD:
                    gone_confirmed += 1
                    queried.append(asin)  # 確定後は通常周期 (週1) で復活チェック
                else:
                    pending += 1
    if queried:
        _fetch_targets.mark_queried(pathlib.Path(out_root), "amazon_refresh", queried)
    logger.info(
        f"Article refresh: {refreshed} refreshed, {gone_confirmed} gone-confirmed, "
        f"{pending} pending-confirmation (picked {len(asins)})"
    )


# Global API client reference for summary logging on exit
api = None

def write_github_step_summary() -> None:
    """PA-API の利用統計と TooManyRequests レートを $GITHUB_STEP_SUMMARY に書き込み、
    必要に応じて警告を出力します。
    """
    global api
    if not api or not hasattr(api, "total_requests"):
        return

    total = api.total_requests
    throttled = api.throttle_count
    
    if total == 0:
        return

    throttle_rate = (throttled / total) * 100
    
    # 1日あたりの Quota 上限（デフォルトは PA-API 標準の 8,640）
    daily_limit = int(os.environ.get("AMAZON_PAAPI_DAILY_LIMIT", 8640))
    quota_usage_percent = (total / daily_limit) * 100

    # Markdown レポート作成
    lines = [
        "## 📊 PA-API Quota & スロットリング観測レポート",
        "",
        "今回の実行における Amazon Product Advertising API (Creators API) のリクエスト統計です。",
        "",
        "| 項目 | 統計値 | 割合 / 詳細 |",
        "| --- | --- | --- |",
        f"| **リクエスト総数 (リトライ含む)** | `{total}` 回 | - |",
        f"| **TooManyRequests (429) エラー** | `{throttled}` 回 | `{throttle_rate:.2f}%` |",
        f"| **1日あたりQuota消費量 (見積もり)** | `{total} / {daily_limit}` | `{quota_usage_percent:.2f}%` (上限 {daily_limit} に対する割合) |",
        ""
    ]

    # 警告しきい値判定 (環境変数で変更可能にする)
    # quota_usage_percent_threshold: デフォルト 80%
    # throttle_rate_threshold: デフォルト 20%
    quota_threshold = float(os.environ.get("AMAZON_PAAPI_QUOTA_WARNING_THRESHOLD", 80.0))
    throttle_threshold = float(os.environ.get("AMAZON_PAAPI_THROTTLE_WARNING_THRESHOLD", 20.0))

    warnings = []
    if quota_usage_percent >= quota_threshold:
        warnings.append(f"PA-API quota usage ({quota_usage_percent:.2f}%) exceeded threshold ({quota_threshold}%)")
    if throttle_rate >= throttle_threshold:
        warnings.append(f"PA-API TooManyRequests rate ({throttle_rate:.2f}%) exceeded threshold ({throttle_threshold}%)")

    if warnings:
        warning_msg = " | ".join(warnings)
        # GHA の Warning 記法
        print(f"::warning title=PA-API Quota Warning::{warning_msg}")
        lines.append(f"> [!WARNING]\n> **警告**: {warning_msg}\n")

    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        try:
            with open(summary_file, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            logger.info(f"Wrote PA-API metrics to GITHUB_STEP_SUMMARY: {summary_file}")
        except Exception as e:
            logger.error(f"Failed to write to GITHUB_STEP_SUMMARY: {e}")
    else:
        # ローカル実行時のために標準出力にも表示
        logger.info("--- PA-API Metrics Summary ---")
        for line in lines:
            if line.strip() and not line.startswith("|") and not line.startswith("##"):
                logger.info(line)
        logger.info(f"Total Requests: {total}, Throttled: {throttled} ({throttle_rate:.2f}%), Quota Usage: {quota_usage_percent:.2f}%")

# Creator API の getItems が itemIds に課している制約。実際の 400 応答から転記:
#   Member must satisfy regular expression pattern: [0-9]{9}[0-9X]|[A-Z][A-Z0-9]{9}
# (10 桁の ASIN、または ISBN-10。後者は末尾チェックディジットが X になりうる)
_ASIN_INPUT_RE = re.compile(r"^(?:[0-9]{9}[0-9X]|[A-Z][A-Z0-9]{9})$")


def _parse_asin_csv(raw: str) -> tuple[list[str], list[str]]:
    """``--asin`` の CSV を (妥当な ASIN, 不正な入力) に分ける。

    ASIN は大文字なので小文字入力は正規化して受ける (``b0abc12345`` → 大文字)。
    workflow_dispatch の "カンマ区切りASIN" 欄に**検索キーワードを入れる**
    取り違えが実際に起きており (run #388)、そのときは Creator API が
    ``1 validation error detected ... at 'itemIds'`` を返して sniper が
    ``sys.exit(1)``。ログを深く辿らないと原因が分からなかったので、
    API に投げる前にここで弾く。
    """
    valid: list[str] = []
    invalid: list[str] = []
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        normalized = token.upper()
        if _ASIN_INPUT_RE.match(normalized):
            valid.append(normalized)
        else:
            invalid.append(token)
    return valid, invalid


def main():
    global api
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="daily_random")
    parser.add_argument("--asin", default="")
    parser.add_argument("--keyword", default="知育玩具",
                        help="Primary search keyword(s). Comma/読点/改行区切りで複数指定可 "
                             "(workflow input カンマ区切り対応)。指定した各キーワードは優先 "
                             "検索として全ページ検索され、--keyword-sample-size のランダム "
                             "サンプル件数はその個数分だけ減らされる。")
    parser.add_argument("--out", default="data/raw/")
    parser.add_argument("--articles-dir", default="data/articles",
                        help="Directory scanned for already-published ASINs; matching items are excluded from amazon.json so Jules cannot pick a duplicate")
    parser.add_argument("--keywords", default="",
                        help="Additional search keywords (CSV or newline-separated). Combined with --keyword and falls back to DEFAULT_KEYWORDS / $AMAZON_SEARCH_KEYWORDS.")
    parser.add_argument("--pages", type=int, default=2,
                        help="PA-API search pages per keyword (1-10, each up to 10 items)")
    parser.add_argument("--min-new", type=int, default=40,
                        help="Stop searching once this many ASINs not in articles-dir are collected")
    parser.add_argument("--demand-keywords", default="data/demand_keywords.json",
                        help="需要側キーワード JSON (build_demand_keywords.py の出力, #2686)")
    parser.add_argument("--demand-slots", type=int, default=0,
                        help="keyword サンプル枠のうち需要側に割り当てる件数 (0=無効・従来動作)")
    parser.add_argument("--search-index", default="Toys",
                        help="PA-API SearchIndex / category (e.g. 'Toys', 'Baby', 'All'). 'All' disables ItemPage on JP marketplace; pick a concrete category to use pagination.")
    parser.add_argument("--competitors-only", action="store_true",
                        help="Backfill mode: fetch --asin CSV, merge current amazon.json pool as competitor candidates, and write per_asin/<ASIN>/{snapshot.json,competitors.json} for the sniper ASINs only. Does NOT overwrite amazon.json (so the live Jules pool is preserved). Requires --asin.")
    parser.add_argument("--all-articles", action="store_true",
                        help="Backfill mode helper: replace --asin with the full set of ASINs discovered under --articles-dir. Only valid together with --competitors-only. Useful for one-shot regeneration of every per_asin/<ASIN>/competitors.json after a ranking change (e.g. Plan D+).")
    parser.add_argument("--competitors-cache-ttl-days", type=float, default=7.0,
                        help="In non-sniper cron runs, skip per-target SearchItems if per_asin/<ASIN>/competitors.json is newer than this many days (default 7.0; #792). Pass 0 to force-refresh every target (legacy behaviour). Sniper (--asin) and --competitors-only backfill always force-refresh regardless of this flag.")
    parser.add_argument("--refresh-max-per-run", type=int, default=110,
                        help="Stale-first refresh of published-article ASIN snapshots via GetItems, this many ASINs per run (10/call). Detects vanished ASINs (status=gone after 2 consecutive misses) so build_post can render 取り扱い終了. 0 disables. Default 110 = full ~1400-article sweep per week on the 2-runs/day fetch-data schedule.")
    parser.add_argument("--refresh-stale-days", type=int, default=7,
                        help="Skip refresh for ASINs checked within this many days (default 7 = weekly cycle)")
    parser.add_argument("--keyword-sample-size", type=int, default=20,
                        help="Shuffle the keyword list each run and pick this many to actually search (#795: spreads search space across runs so new-ASIN hit rate stays high even when DEFAULT_KEYWORDS saturates). Default 20 = ~8%% of the 240-entry niche list per run, hitting each keyword ~once/3 months on weekly cron. Pass 0 to disable sampling (= search every keyword in shuffled order). Ignored when --keywords is set explicitly.")
    args = parser.parse_args()

    # --all-articles expands sniper input to every published article ASIN.
    # Validate up front so the user gets a clear error instead of an opaque
    # PA-API failure later. --competitors-only is required because --all-articles
    # in search/sniper mode would issue 200+ GetItems with no clear benefit.
    if args.all_articles:
        if not args.competitors_only:
            logger.error("--all-articles requires --competitors-only")
            sys.exit(1)
        if args.asin:
            logger.error("--all-articles and --asin are mutually exclusive")
            sys.exit(1)
        discovered = sorted(_load_existing_article_asins(args.articles_dir))
        if not discovered:
            logger.error(f"--all-articles: no ASINs found under {args.articles_dir}")
            sys.exit(1)
        args.asin = ",".join(discovered)
        logger.info(f"--all-articles: expanded to {len(discovered)} ASIN(s) from {args.articles_dir}")

    # ASIN 入力は API に投げる前に検証する。--all-articles で組み立てた場合も
    # 通すが、そちらは記事ファイル名由来なので実質常に通過する。
    _sniper_asins: list[str] = []
    if args.asin:
        _sniper_asins, _invalid_asins = _parse_asin_csv(args.asin)
        if _invalid_asins:
            logger.error(
                "--asin に ASIN として不正な値が %d 件あります: %s",
                len(_invalid_asins), ", ".join(repr(a) for a in _invalid_asins),
            )
            logger.error(
                "ASIN は 10 桁 (例 B0ABC12345) か ISBN-10 です。"
                "検索キーワードを指定したい場合は --asin ではなく --keyword "
                "(workflow の「検索キーワード」欄) を使ってください。"
            )
            sys.exit(1)

    app_id = get_secret("AMAZON_CREATORS_APPLICATION_ID")
    cid = get_secret("AMAZON_CREATORS_CREDENTIAL_ID")
    cs = get_secret("AMAZON_CREATORS_CREDENTIAL_SECRET")
    tag = get_secret("AMAZON_PARTNER_TAG")

    items = []

    if not app_id or not cid or not cs or not tag or not HAS_CREATORS_API:
        logger.warning("Amazon API keys or module missing. Skipping Amazon fetch (returning empty data).")
        os.makedirs(args.out, exist_ok=True)
        with open(os.path.join(args.out, "amazon.json"), "w", encoding="utf-8") as f:
            json.dump({"keyword": args.keyword, "items": [], "mode": args.mode}, f, ensure_ascii=False, indent=4)
        return

    api = CreatorsAPIClient()

    # 共通定数 SEARCH_ITEM_RESOURCES を流用 (Issue #785: fetch_amazon_dry_run.py が
    # validate gate で同一配列を Creator API に投げて 200 OK + items[] を確認する)。
    resources = SEARCH_ITEM_RESOURCES

    blocklist = _load_asin_blocklist()
    if blocklist:
        logger.info(f"ASIN blocklist loaded: {len(blocklist)} ASIN(s) will be skipped")

    # Sniper Mode: Fetch specific ASIN(s) first.
    # --asin accepts CSV per workflow input "カンマ区切りASIN"; PA-API GetItems
    # supports up to 10 ASINs per call, so chunk to be safe.
    sniper_asins: list[str] = []
    if args.asin:
        sniper_asins = _sniper_asins
    if sniper_asins:
        logger.info(f"Sniper Mode: Fetching ASIN(s) {sniper_asins}")
        for chunk_start in range(0, len(sniper_asins), 10):
            chunk = sniper_asins[chunk_start:chunk_start + 10]
            try:
                res = api.get_items(chunk, resources=resources)
                found_items = _safe_get(res, "itemsResult", "items", default=[])
                for it in found_items:
                    items.append(normalize_api_item(it, tag, "Amazon (Target)"))
            except Exception as e:
                logger.error(f"Failed to fetch target ASIN(s) {chunk}: {e}")
                sys.exit(1)
            if chunk_start + 10 < len(sniper_asins):
                time.sleep(1.1)  # PA-API TPS=1 safety margin

    # Competitors-only backfill mode: emit per_asin snapshot + competitors.json
    # for the sniper ASINs without overwriting amazon.json or running the
    # search sweep. Merges the existing amazon.json pool as competitor
    # candidates so the backfilled competitors.json contains real listings
    # rather than just the sniper batch competing with itself.
    if args.competitors_only:
        if not sniper_asins:
            logger.error("--competitors-only requires --asin (CSV)")
            sys.exit(1)
        existing_pool_path = os.path.join(args.out, "amazon.json")
        if os.path.exists(existing_pool_path):
            try:
                with open(existing_pool_path, "r", encoding="utf-8") as f:
                    existing_data = json.load(f)
                existing_items = existing_data.get("items", []) if isinstance(existing_data, dict) else []
                sniper_set = set(sniper_asins)
                merged = 0
                for ex in existing_items:
                    if ex.get("asin") and ex.get("asin") not in sniper_set:
                        items.append(ex)
                        merged += 1
                logger.info(f"Merged {merged} items from existing amazon.json as competitor candidates")
            except Exception as e:
                logger.warning(f"Failed to load existing amazon.json: {e}")
        os.makedirs(args.out, exist_ok=True)
        sniper_set = set(sniper_asins)
        for it in items:
            if it.get("asin") in sniper_set:
                write_per_asin_snapshot(args.out, it)
        existing_covered = _load_existing_article_asins(args.articles_dir)
        for target_asin in sniper_asins:
            target_item = next((it for it in items if it.get("asin") == target_asin), None)
            if not target_item:
                logger.warning(f"--competitors-only: target {target_asin} not present in items; GetItems may have failed. Skipping.")
                continue
            write_per_asin_competitors(
                args.out, target_item, fallback_pool=items,
                api=api, search_index=args.search_index, resources=resources,
                covered_asins=existing_covered,
            )
        logger.info(f"--competitors-only complete: backfilled {len(sniper_asins)} ASIN(s)")
        return

    # Search Mode: multi-keyword × multi-page sweep to keep the new-ASIN pool
    # large enough that the existing-article dedup gate below doesn't starve
    # Jules of fresh ASINs.
    existing = _load_existing_article_asins(args.articles_dir)
    target_asins: set[str] = set(sniper_asins)
    seen_asins = {it["asin"] for it in items if it.get("asin")}

    # #795: 親 ASIN プール (色違い/サイズ違い重複の排除に使う)。
    # snapshot.item.parent_asin は本 PR 以降の run でしか埋まらないので、初回は
    # ほぼ空。run を重ねるたびに蓄積されて精度が上がる段階的設計。
    existing_parent_asins = _load_existing_parent_asins(args.out)
    seen_parent_asins: set[str] = set()
    parent_skipped_existing = 0
    parent_skipped_session = 0

    # #795: 王道偏重を避けるため、shuffled + sampled キーワードで検索する。
    # --keywords 明示時は env/CLI が user 意図なのでシャッフルだけ (= 順序ランダム
    # 化のみ、件数は明示分を尊重)。Default keywords のときはサンプリングも有効。
    #
    # workflow input カンマ区切り対応: --keyword はカンマ/読点/改行区切りで複数
    # 指定できる (例 "ビーチボール, 水鉄砲,折りたたみプール")。順序を保持したまま
    # dedupe し、ユーザ指定分は「優先キーワード」として扱う (early-termination
    # 対象外・全ページ検索)。サンプリングが有効なときはランダムキーワードの件数
    # をユーザ指定分だけ減らし、検索コストの総量が跳ね上がらないようにする。
    user_keywords: list = []
    for kw in re.split(r"[,，、\n]", args.keyword or ""):
        kw = kw.strip()
        if kw and kw not in user_keywords:
            user_keywords.append(kw)
    # Sniper Mode fallback: no explicit keyword but sniper ASINs were resolved
    # -> fall back to the AMAZON title slice (unchanged from before).
    if not user_keywords and sniper_asins and items:
        user_keywords = [items[0]["title"][:20]]

    # #2686: 需要側キーワードを優先枠で確保する。供給側のロングテール探索を止めない
    # ため全面切替はせず、sample 枠の一部だけを需要側に割り当てる。枠内に収めるので
    # SearchItems の総リクエスト数は増えない (レート的に従来と同じ)。
    demand_kws = load_demand_keywords(args.demand_keywords, args.demand_slots)

    sampling_active = not args.keywords and not os.environ.get("AMAZON_SEARCH_KEYWORDS")
    if sampling_active:
        if args.keyword_sample_size <= 0:
            # --keyword-sample-size 0 = 明示的なサンプリング無効 (help に文書化
            # された従来仕様)。ユーザキーワード数による削減はせず全件を検索する。
            random_kws = parse_keywords(args.keywords, shuffle=True, sample_size=0)
        else:
            effective_sample_size = max(
                0, args.keyword_sample_size - len(user_keywords) - len(demand_kws))
            if effective_sample_size > 0:
                random_kws = parse_keywords(args.keywords, shuffle=True, sample_size=effective_sample_size)
            else:
                # TRAP: parse_keywords(..., sample_size=0) means "no sampling ="
                # return ALL keywords, NOT "return none". Once the reduced budget
                # bottoms out at 0 we must skip the call and use [] explicitly,
                # or we'd silently search the full ~240-entry DEFAULT_KEYWORDS list.
                random_kws = []
    else:
        # --keywords / $AMAZON_SEARCH_KEYWORDS explicit: user intent, no
        # reduction, shuffle order only (today's behaviour, unchanged).
        random_kws = parse_keywords(args.keywords, shuffle=True, sample_size=0)

    # User keywords first, in given order (not shuffled); then the demand-side
    # keywords (#2686); random supply-side keywords fill the rest. Dedupe keeps
    # the earliest occurrence so the priority order above is what survives.
    keywords = list(user_keywords)
    for k in demand_kws + list(random_kws):
        if k not in keywords:
            keywords.append(k)
    if demand_kws:
        logger.info(f"keyword mix: user={len(user_keywords)} demand={len(demand_kws)} "
                    f"supply={len([k for k in random_kws if k not in demand_kws])} "
                    f"total={len(keywords)}")
    # Surface the first user keyword as the legacy primary_kw for the
    # amazon.json "keyword" field (back-compat with downstream consumers).
    primary_kw = user_keywords[0] if user_keywords else None

    def _is_jules_eligible(it: dict) -> bool:
        """Return True iff ``it`` would survive the post-loop reseller filter.

        Used both for the early-termination counter inside the search loop
        and for the final pruning pass below, so the ``min_new`` target
        reflects items that will *actually* reach Jules (#794: previously
        the counter included items the reseller filter would later drop,
        causing 'Pool below target' warnings on every run).
        """
        asin = it.get("asin") or ""
        if asin in target_asins:
            return True
        if asin in existing:
            return False
        if not is_trusted_seller(it.get("seller") or ""):
            return False
        if is_low_stock_reseller_signal(it.get("availability") or ""):
            return False
        if classify_genre(it.get("browse_nodes") or [], asin)[0] == "flag":
            return False
        return True

    new_for_jules = sum(1 for it in items if _is_jules_eligible(it))

    pages = max(1, min(args.pages, 10))
    n_random = len(keywords) - len(user_keywords)
    logger.info(
        f"Search Mode: {len(user_keywords)} user keyword(s) + {n_random} sampled keyword(s) "
        f"× {pages} page(s), target new-for-Jules ASINs = {args.min_new}"
    )

    done = False
    for idx, kw in enumerate(keywords):
        if done:
            break
        # User-supplied keywords are mandatory: search every page regardless
        # of min_new. Early termination only applies to the random/sampled
        # tail (min_new is a floor/target, exceeding it via user keywords is fine).
        mandatory = idx < len(user_keywords)
        for page in range(1, pages + 1):
            if not mandatory and new_for_jules >= args.min_new:
                done = True
                break
            logger.info(f"  '{kw}' page={page} (new={new_for_jules}/{args.min_new})")
            try:
                res = api.search_items(
                    keywords=kw, search_index=args.search_index,
                    item_page=page, resources=resources,
                )
                found_items = _safe_get(res, "searchResult", "items", default=[])
            except Exception as e:
                # PA-API can return TooManyRequests / no-results errors per page;
                # log and continue rather than aborting the whole pipeline.
                logger.warning(f"  search failed for '{kw}' p{page}: {e}")
                time.sleep(1.1)
                continue
            for it in found_items:
                asin = it.get("asin")
                if not asin or asin in seen_asins:
                    continue
                if asin in blocklist:
                    logger.info(f"  skip blocklisted ASIN {asin}")
                    seen_asins.add(asin)
                    continue
                # #795: 親 ASIN dedup — 色違い/サイズ違いバリエーションを排除
                parent = extract_parent_asin(it)
                if parent and asin not in target_asins:
                    if parent in existing_parent_asins:
                        logger.info(f"  skip variation {asin}: parent {parent} already covered by article")
                        seen_asins.add(asin)
                        parent_skipped_existing += 1
                        continue
                    if parent in seen_parent_asins:
                        logger.info(f"  skip variation {asin}: parent {parent} already in this session")
                        seen_asins.add(asin)
                        parent_skipped_session += 1
                        continue
                    seen_parent_asins.add(parent)
                seen_asins.add(asin)
                normalized = normalize_api_item(it, tag, "Amazon")
                items.append(normalized)
                if _is_jules_eligible(normalized):
                    new_for_jules += 1
            time.sleep(1.1)  # PA-API TPS=1 safety margin

    if not items:
        logger.error("Search returned zero items across all keywords; aborting")
        sys.exit(1)

    os.makedirs(args.out, exist_ok=True)

    # Strip already-covered ASINs + reseller/scam signals from the Jules-facing
    # list. Sniper-mode target ASIN is exempt (user explicitly asked to
    # re-fetch it). Snapshots and per-ASIN competitor files still get every
    # item so that internal linking and badge back-fill keep working for the
    # full catalog.
    #
    # 転売 ASIN 検出ロジック (2026-05-25, #732 follow-up):
    #   1. blocklist (data/asin_blocklist.json): 既知の転売 ASIN を恒久除外
    #   2. seller != Amazon.co.jp / メーカー公式: Buy Box が第三者出品の ASIN は
    #      価格プレミアム上乗せが典型。is_trusted_seller() で除外
    #   3. availability に "残り N 点" 表記: 直販は通常潤沢な在庫なのでこの
    #      表記が出る ASIN は転売出品の確率が高い
    # sniper 指定された ASIN はユーザ意図を尊重して除外しない (=明示確認済み)
    reseller_dropped = 0
    genre_dropped = 0
    items_for_jules: list = []
    for it in items:
        asin = it.get("asin") or ""
        if asin in target_asins:
            items_for_jules.append(it)
            continue
        if asin in existing:
            continue
        seller = it.get("seller") or ""
        availability = it.get("availability") or ""
        if not is_trusted_seller(seller):
            logger.info(f"  reseller-drop {asin}: seller={seller!r} not in trusted list")
            reseller_dropped += 1
            continue
        if is_low_stock_reseller_signal(availability):
            logger.info(f"  reseller-drop {asin}: low-stock availability={availability!r}")
            reseller_dropped += 1
            continue
        verdict, cat_nodes = classify_genre(it.get("browse_nodes") or [], asin)
        if verdict == "flag":
            cats = ", ".join(f"{nd.get('name')}({nd.get('root')})" for nd in cat_nodes[:4])
            logger.info(f"  genre-drop {asin}: 非対象カテゴリ [{cats}]")
            genre_dropped += 1
            continue
        items_for_jules.append(it)
    dropped = len(items) - len(items_for_jules)
    logger.info(
        f"Collected {len(items)} unique ASINs, {len(items_for_jules)} new for Jules "
        f"(dropped {dropped} already-covered + {reseller_dropped} reseller-signal "
        f"+ {genre_dropped} genre-mismatch)"
    )
    # #795: 親 ASIN dedup の効きを観察する。0 が続けば Creator API の productInfo
    # で parentAsin が取れていない可能性 → Issue を再起票して resource を再評価。
    logger.info(
        f"Parent-ASIN dedup: {parent_skipped_existing} variation(s) already in articles, "
        f"{parent_skipped_session} variation(s) collapsed in this session "
        f"(parent pool size={len(existing_parent_asins)})"
    )
    jan_present = sum(1 for it in items if it.get("jan_code"))
    if items:
        logger.info(
            f"JAN coverage: {jan_present}/{len(items)} "
            f"({jan_present * 100 // len(items)}%) items have EAN/JAN from PA-API"
        )
    if len(items_for_jules) < args.min_new:
        logger.warning(
            f"Pool below target ({len(items_for_jules)} < {args.min_new}); "
            f"consider expanding --keywords or --pages"
        )

    with open(os.path.join(args.out, "amazon.json"), "w", encoding="utf-8") as f:
        json.dump({"keyword": primary_kw or keywords[0], "items": items_for_jules, "mode": args.mode}, f, ensure_ascii=False, indent=4)

    for it in items:
        write_per_asin_snapshot(args.out, it)

    # 記事 ASIN 巡回 (2026-07-07): 既掲載記事の snapshot を stale-first で更新し、
    # Amazon から消滅した ASIN に status="gone" を付ける。失敗しても pool 生成
    # (amazon.json は書き込み済み) には影響させない。
    if args.refresh_max_per_run > 0:
        try:
            refresh_article_snapshots(
                api, args.out, args.articles_dir, tag,
                max_per_run=args.refresh_max_per_run,
                stale_days=args.refresh_stale_days)
        except Exception as e:
            logger.warning(f"Article refresh failed (non-fatal): {e}")

    # Write competitors.json for every ASIN in the pool so that whichever
    # ASIN Jules ends up picking from amazon.json has API-verified competitors
    # available at build time. Without this, scheduled cron runs (which pass
    # an empty --asin) leave build_post.py with no real competitor data, and
    # competitor cards render as text-only boxes with no image or Amazon CTA.
    targets = sniper_asins if sniper_asins else [it["asin"] for it in items if it.get("asin")]
    # Hoist the per_asin/<ASIN>/amazon.json snapshot pool out of the per-target
    # loop. The old call site re-opened up to 500 snapshot files for every
    # target (=> N*M = ~10万 opens on a full catalog); building it once and
    # filtering by target_asin in-memory cuts that to a single pass.
    published_pool = _load_published_snapshots_pool(args.out, existing)
    # #792: sniper の場合は user 意図優先で強制 refresh (TTL=0)。
    # 通常 cron は --competitors-cache-ttl-days で既存 ASIN の SearchItems
    # 再検索をスキップする。
    competitors_ttl = 0.0 if sniper_asins else args.competitors_cache_ttl_days
    for target_asin in targets:
        target_item = next((it for it in items if it.get("asin") == target_asin), None)
        if not target_item:
            continue
        write_per_asin_competitors(
            args.out, target_item, fallback_pool=items,
            api=api, search_index=args.search_index, resources=resources,
            covered_asins=existing,
            published_pool=published_pool,
            cache_ttl_days=competitors_ttl,
        )

if __name__ == "__main__":
    try:
        main()
    finally:
        write_github_step_summary()
