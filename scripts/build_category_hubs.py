"""Build information-type category hub feature lists (Issue #2687 / 柱1).

実SERP×DA 検証 (#2690) で「テーマ別ランキング hub」が DA11 射程内 (英語/
プログラミング/算数) と確定。bare head ("おもちゃ ランキング") や取引型/
navigational は対象外。本 script は商品 (data/articles/*.json) をテーマに分類し、
知育スコア順に束ねた hub 用 feature データを出力する。746 商品を spoke として
情報型 hub に内部リンクで集約する柱1の中核。

Reads:
    data/articles/*.json   (article body; excludes *.quality.json etc.)

Writes:
    hugo/data/features/<theme>.json   (feature layout が feature_type で読む)

build_feature_lists.py の loader / scorer / serializer を再利用する (スコアは
score_calculator で再計算 = 記事ページ表示値と一致)。外部 API 呼び出しなし。
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import unicodedata
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from age_semantics import is_seasonal_decoration, override_age_months, seasonal_names  # noqa: E402
from brand_normalizer import normalize as normalize_brand  # noqa: E402
from ranking_dates import resolve_entry_dates, today_jst  # noqa: E402
from score_claims import LOW_TOTAL_MAX  # noqa: E402
from build_feature_lists import (  # noqa: E402
    _dedupe_by_asin,
    _is_article_json,
    _now_iso,
    _record_to_payload_common,
    load_articles,
    overlay_amazon_images,
    overlay_current_prices,
    parse_min_months,
)

logger = logging.getLogger("build_category_hubs")


# モンテッソーリ hub 用の signal 群 (#3654)。「モンテッソーリ」tag を持たない木製
# 教具 (積み木/型はめ/紐通し等) が多数取りこぼされる実態 (実測: 語彙一致 40 件中
# ivs>=3.8 は 2 件だが、木製 AND モンテ的教具形態の高スコア商品が別に 6 件埋もれる)
# に対応するため、明示語 (include) に加えて「天然素材 AND 教具形態」の両群一致を
# 第 2 の合格経路 (all_of) として持つ。過剰マッチ (量産プラ/キャラ/電子玩具) は
# _MONTESSORI_DENY で fail-closed。
_MONTESSORI_WOOD = [
    "木製", "木のおもちゃ", "無垢", "ブナ材", "ぶな材", "beech", "天然木",
]
_MONTESSORI_FORM = [
    "型はめ", "型落とし", "紐通し", "ひもとおし", "ペグ", "スタッキング",
    "日常生活", "はさみの練習", "ボタンかけ", "分類", "並べ替え",
    "感覚教育", "実物大", "自己教育", "円柱", "はめ込みパズル",
]
_MONTESSORI_DENY = [
    "lego", "レゴ", "デュプロ", "duplo", "アンパンマン", "フィッシャープライス",
    "fisher", "ディズニー", "disney", "トミカ", "プラレール", "電子", "光る",
]

# 図形・空間認識 hub 用 DENY (#2687 柱1 / #2690 iroya 型テーマ拡張)。図形/立体/
# 空間の語はキャラ立体パズルや量産構成玩具にも現れるため、ライセンスキャラ・乗り物
# を除外して編集型ランキングの品質を保つ。レゴは character セットが大量に混入する
# ため除外 (レゴクラシックの構成玩具は将来 supply 判断で個別追加余地あり)。
_SHAPE_DENY = [
    "トミカ", "プラレール", "シンカリオン", "アンパンマン", "ディズニー", "disney",
    "リカちゃん", "シルバニア", "レゴ", "lego", "デュプロ", "duplo", "ニンジャゴー",
    "ソニック", "マリオ", "ポケモン", "すみっこ", "すみっコ", "妖怪",
]

# 楽器・リズム hub 用 DENY。図形 hub と違いキャラ楽器 (アンパンマン ピアノ等) は
# この棚の需要の中心なので除外しない。「ドラム」がナーフのドラムマガジンに、
# 「ピアノ」がレゴの大人向けセットに当たるため、それらと英語 hub の商品を除く。
_MUSIC_DENY = [
    "ナーフ", "nerf", "ブラスター", "ドラムマガジン",
    "レゴ", "lego", "英語", "えいご",
]


# テーマ定義。exclude のいずれにもマッチせず、かつ include のいずれかにマッチする
# (または all_of の全群にマッチする) 商品が候補。マッチは title+name+tags+keywords
# +features+edu_domains を結合した小文字テキストへの部分文字列照合。keyword は
# #2690 の spoke 実測を踏まえ精度優先。min_ivs は省略時 CLI 既定 (3.8) を使う。
THEMES: dict[str, dict[str, Any]] = {
    "english": {
        "label": "英語",
        "include": [
            "英語", "えいご", "アルファベット", "フォニックス",
            "english", "abcの", "abc絵", "abcカード", "バイリンガル",
        ],
        "exclude": ["建設"],
    },
    "programming": {
        "label": "プログラミング",
        "include": [
            "プログラミング", "programming", "コーディング",
            "論理的思考", "プログラム的思考", "scratch", "スクラッチ",
        ],
        "exclude": [],
    },
    "math": {
        "label": "算数",
        "include": [
            "算数", "計算", "九九", "そろばん", "たし算", "ひき算",
            "足し算", "引き算", "数の概念", "数量感覚", "アバカス",
        ],
        "exclude": [],
    },
    "montessori": {
        "label": "モンテッソーリ",
        # 明示語 (単独で合格) — merchant が「モンテッソーリ」を自称する商品を拾う。
        "include": ["モンテッソーリ", "montessori", "モンテ", "教具"],
        # 第 2 経路: 天然素材群 AND 教具形態群 の両方にマッチ (取りこぼし回収)。
        "all_of": [_MONTESSORI_WOOD, _MONTESSORI_FORM],
        "exclude": _MONTESSORI_DENY,
        # テーマ既定 (3.8) だと適格 2 件で thin hub になるため 3.4 に個別緩和し 10 件
        # 確保 (#3654 owner 承認)。閾値感度で 3.3 だと 21 件へ低スコアクラスタが流入
        # するため 3.4 が「勝てるゾーンの品質」を保てる上限。
        "min_ivs": 3.4,
    },
    "shape": {
        # 図形・空間認識 (#2690 iroya 型テーマ拡張・供給レディで実装)。マグ・フォーマー
        # 等の磁石構成・立体パズル・タングラム・図形モザイクを束ねる情報型 hub。実測で
        # ivs>=3.8 の適格が 17 件・14 ブランド揃うため min_ivs 緩和は不要。既存 math hub
        # (計算/数のみ) とは include が重ならず near-duplicate 化しない (重複は 4/17 と低い)。
        "label": "図形・空間",
        "include": [
            "図形", "立体パズル", "立体図形", "タングラム", "パターンブロック",
            "空間認識", "幾何", "ジオボード", "マグ・フォーマー", "マグフォーマー",
            "図形モザイク", "ペントミノ",
        ],
        "exclude": _SHAPE_DENY,
    },
    "music": {
        # 楽器・リズム (brain#67)。タグ「リズムゲーム」が「リズム おもちゃ ○○」で
        # 2 ページ目に出ていたが、受け皿になる hub が無かった。ivs>=3.8 の適格が
        # 約 70 件あり、既存 hub との重複は top-24 中 2 件。
        "label": "楽器・リズム",
        "include": [
            "楽器", "ピアノ", "木琴", "鉄琴", "シロフォン", "シロホン", "モッキン",
            "ドラム", "太鼓", "たいこ", "リズム", "音ゲー", "マラカス", "カスタネット",
            "タンバリン", "鍵盤", "音感", "リトミック",
        ],
        "exclude": _MUSIC_DENY,
    },
}


# 年齢 hub 定義 (#2687 柱1・年齢軸)。テーマ hub と異なり数値軸 = persona_fit.age_range
# から抽出した「最小推奨年齢 (月)」でバケットへ単一所属させる。範囲オーバーラップ
# (3歳以上 を 3/4/5歳 hub に重複所属) させると隣接 age hub が near-duplicate 化し
# #2765 で潰した hub カニバリを再発させるため、最小年齢アンカーで 1 商品 1 hub に限定。
# min_months <= x < max_months。max=216 (18歳) で adult 玩具を全 hub から除外。
AGE_HUBS: dict[str, dict[str, Any]] = {
    "age-0": {"label": "0歳", "min_months": 0, "max_months": 12},
    "age-1": {"label": "1歳", "min_months": 12, "max_months": 24},
    "age-2": {"label": "2歳", "min_months": 24, "max_months": 36},
    "age-3": {"label": "3歳", "min_months": 36, "max_months": 48},
    "age-4": {"label": "4〜5歳", "min_months": 48, "max_months": 72},
    "age-6": {"label": "6歳以上", "min_months": 72, "max_months": 216},
}

# 年齢 hub の上位 (top-n) に載せる同一メーカーの上限。24 件なら 6 メーカー以上が並ぶ。
AGE_MAX_PER_BRAND = 4

# navi-brain#166 F15: ランキング初登場を発売・予約商品の代理指標として扱う。
NEW_ARRIVAL_WINDOW_DAYS = 30
NEW_ARRIVAL_RECENT_DAYS = 7
NEW_ARRIVAL_MAX = 6
NEW_ARRIVAL_MIN = 4
NEW_ARRIVAL_MAX_PER_BRAND = 2
NEW_ARRIVAL_AGE_LOOKBACK_MONTHS = 12

# 直近30日で3日以上ランク入りした商品を、販売実績が見える候補として前に出す。
RANK_DAYS_PRIORITY_THRESHOLD = 3

# navi-brain#166 F10: クリスマス × 年齢の特集ページ (K が案 A を選んだ)。
# 一覧は検索語のデータに出た組み合わせだけ (集計は navi-brain#166)。ここに無いページは作らない。
# 誕生日 × 年齢は年齢 hub の題 (「誕生日プレゼントにも」) と同じ語を狙って食い合うので作らない。
# lo / hi は診断 (hugo/assets/js/diagnosis_core.js の AGES) と同じ月齢の幅で、選び方も
# 診断の「プレゼント・〜10,000円・バランスよく」の採点 (scoreItem) に合わせている。
# hub は歯止めで比べる年齢 hub。4歳・5歳はどちらも age-4 (4〜5歳) と比べる。
XMAS_PAGES: dict[str, dict[str, Any]] = {
    "xmas-age-0": {"label": "0歳", "lo": 0, "hi": 11, "hub": "age-0"},
    "xmas-age-1": {"label": "1歳", "lo": 12, "hi": 23, "hub": "age-1"},
    "xmas-age-2": {"label": "2歳", "lo": 24, "hi": 35, "hub": "age-2"},
    "xmas-age-3": {"label": "3歳", "lo": 36, "hi": 47, "hub": "age-3"},
    "xmas-age-4": {"label": "4歳", "lo": 48, "hi": 59, "hub": "age-4"},
    "xmas-age-5": {"label": "5歳", "lo": 60, "hi": 71, "hub": "age-4"},
    "xmas-age-6": {"label": "小学生", "lo": 72, "hi": 155, "hub": "age-6"},
}
XMAS_TOP_N = 12
XMAS_MIN_ITEMS = 8           # これに届かないページは作らない (薄いページを出さない)
XMAS_BUDGET_MAX = 10000      # 診断の「〜10,000円 クリスマス・特別な日に」
XMAS_MAX_PER_BRAND = 2       # 診断の diversify と同じ
XMAS_OVERLAP_MAX = 0.6       # 年齢 hub の上位・ほかのクリスマスページとこの割合以上同じなら作らない
XMAS_HUB_COMPARE_N = 24      # 比べる年齢 hub の上位件数
XMAS_MAX_FROM_HUB = 6        # 年齢 hub の上位から選ぶのはここまで (12 件の半分。select_xmas_items)


def load_rank_history(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("history", {})
    except (OSError, json.JSONDecodeError):
        return {}


def rank_metrics(history: dict[str, Any], asin: str, today: date | None = None) -> tuple[int, int]:
    """historyのMM-DDを直近30日の日付に展開し、distinct日数と最高順位を返す。

    MM-DD は JST。today を省くと JST の今日 (GitLab の UTC ビルドでも 0〜9 時 JST の当日分を落とさない)。
    年の解釈は ranking_dates.resolve_entry_dates (挿入順で遡る) に任せる。
    """
    today = today or today_jst()
    start = today - timedelta(days=29)
    days: dict[date, int] = {}
    rows = (history.get(asin) or {}).get("history", [])
    if not isinstance(rows, list):
        return 0, 0
    for row, appeared in zip(rows, resolve_entry_dates(rows, today)):
        if appeared is None:
            continue
        try:
            rank = int(row["r"])
        except (KeyError, TypeError, ValueError):
            continue
        if start <= appeared <= today:
            days[appeared] = min(rank, days.get(appeared, rank))
    return len(days), min(days.values(), default=0)


def first_last_seen(history: dict[str, Any], asin: str, today: date | None = None) -> tuple[date | None, date | None]:
    """navi-brain#166 F15: 挿入順のランキング履歴から初回・最終観測日を得る。"""
    today = today or today_jst()
    rows = (history.get(asin) or {}).get("history", [])
    if not isinstance(rows, list):
        return None, None
    dates = [d for d in resolve_entry_dates(rows, today) if d is not None and d <= today]
    return (min(dates), max(dates)) if dates else (None, None)


_RELEASE_DATE_RE = re.compile(
    r"(?<!\d)(?P<y>\d{4})(?:年|[/.\-])(?P<m>\d{1,2})(?:月|[/.\-])(?P<d>\d{1,2})(?!\d)日?")
# 日付の直後にこれが「発売」より先に来たら、発売日ではなく出荷日・予約開始日などとみなす
_NOT_RELEASE_WORDS = ("出荷", "発送", "入荷", "お届け", "到着", "開始", "受付", "締切", "まで")


def parse_release_date(name: str) -> date | None:
    """navi-brain#166 F15: 楽天タイトルの発売日表記だけを安全に抽出する。

    採るのは「2026年11月21日発売」(日付の直後 8 字以内に「発売」) と
    「【予約 2026/11/21】」(日付の直前 8 字以内に「予約」) の形だけ。
    直後に「出荷・発送・開始」などが「発売」より先に来るもの、直前が「予約開始・予約受付」
    のものは採らない (出荷日・予約の受付開始日を発売日と取り違えないため)。
    型番などの長い数字の一部には当てない (前後が数字なら不一致)。
    """
    if not isinstance(name, str):
        return None
    for match in _RELEASE_DATE_RE.finditer(name):
        tail = name[match.end():match.end() + 8]
        head = name[max(0, match.start() - 8):match.start()]
        release_at = tail.find("発売")
        not_release_at = min((i for i in (tail.find(w) for w in _NOT_RELEASE_WORDS) if i >= 0), default=-1)
        if not_release_at >= 0 and (release_at < 0 or not_release_at < release_at):
            continue
        reserved = "予約" in head and "予約開始" not in head and "予約受付" not in head
        if release_at < 0 and not reserved:
            continue
        try:
            return date(int(match.group("y")), int(match.group("m")), int(match.group("d")))
        except ValueError:
            continue
    return None


def select_new_arrivals(records, history, min_months_index, brand_index, *, today=None,
                        age_range=None, low_total_max=LOW_TOTAL_MAX):
    """navi-brain#166 F15: 初登場・直近掲載・年齢・スコアで新着商品を選ぶ。"""
    today = today or today_jst()
    start = today - timedelta(days=NEW_ARRIVAL_WINDOW_DAYS - 1)
    recent_start = today - timedelta(days=NEW_ARRIVAL_RECENT_DAYS - 1)
    candidates = []
    for rec in records:
        first, last = first_last_seen(history, rec.asin, today)
        mm = min_months_index.get(rec.asin)
        if first is None or last is None or not (start <= first <= today) or not (recent_start <= last <= today):
            continue
        if mm is None or rec.ivs_100 is None or rec.ivs_100 <= low_total_max:
            continue
        if age_range is not None and not (age_range[0] <= mm < age_range[1]):
            continue
        days, best = rank_metrics(history, rec.asin, today)
        rec.first_seen, rec.rank_days_30, rec.best_rank_30 = first, days, best
        release = parse_release_date((history.get(rec.asin) or {}).get("name", ""))
        rec.release_date = release if release and (release >= today or 0 < (today - release).days <= 30) else None
        rec.release_upcoming = bool(rec.release_date and rec.release_date >= today)
        candidates.append(rec)
    candidates.sort(key=lambda r: (-r.first_seen.toordinal(), rank_metrics(history, r.asin, today)[1] or 10**9,
                                   -(r.ivs_100 or 0), r.asin))
    picked, held, used = [], [], {}
    for rec in candidates:
        brand = brand_index.get(rec.asin)
        if brand is not None and used.get(brand, 0) >= NEW_ARRIVAL_MAX_PER_BRAND:
            held.append(rec)
            continue
        if brand is not None:
            used[brand] = used.get(brand, 0) + 1
        picked.append(rec)
        if len(picked) >= NEW_ARRIVAL_MAX:
            break
    # 上限で外した品だけで NEW_ARRIVAL_MIN に届かないとき (例: 3 歳はバンダイが多い) は、
    # 枠ごと消すより偏りを残す (_cap_per_brand と同じ考え方)。戻すのは MIN までで、
    # 並びは元の順 (初登場の新しい順) に揃える。
    if len(picked) < NEW_ARRIVAL_MIN and held:
        order = {id(rec): pos for pos, rec in enumerate(candidates)}
        picked.extend(held[:NEW_ARRIVAL_MIN - len(picked)])
        picked.sort(key=lambda r: order[id(r)])
    return picked


def _new_arrivals_payload(items):
    payload = []
    for idx, rec in enumerate(items, start=1):
        item = _record_to_payload_common(rec, idx)
        if hasattr(rec, "brand"):
            item["brand"] = rec.brand
        item.update({"first_seen": rec.first_seen.isoformat(), "first_seen_label": f"{rec.first_seen.month}/{rec.first_seen.day}",
                     "rank_days_30": rec.rank_days_30, "best_rank_30": rec.best_rank_30})
        if rec.release_date:
            item.update({"release_date": rec.release_date.isoformat(),
                         "release_label": f"{rec.release_date.month}/{rec.release_date.day}",
                         "release_upcoming": rec.release_upcoming})
        payload.append(item)
    return payload


def prioritize_ranked(items, history: dict[str, Any], today: date | None = None):
    """既存の選抜済み商品内で閾値商品だけを優先し、実績値を付与する。"""
    decorated = []
    for pos, rec in enumerate(items):
        days, best = rank_metrics(history, rec.asin, today)
        if days:
            rec.rank_days_30, rec.best_rank_30 = days, best
        decorated.append((pos, rec, days))
    decorated.sort(key=lambda x: (0 if x[2] >= RANK_DAYS_PRIORITY_THRESHOLD else 1,
                                  -x[2] if x[2] >= RANK_DAYS_PRIORITY_THRESHOLD else x[0],
                                  -(x[1].ivs_100 or 0) if x[2] >= RANK_DAYS_PRIORITY_THRESHOLD else 0,
                                  x[0]))
    return [x[1] for x in decorated]


def build_min_months_index(articles_dir: Path) -> dict[str, int]:
    """{asin: 最小推奨年齢(月)} を返す。同一 ASIN 複数ファイルは最小値を採用。"""
    index: dict[str, int] = {}
    if not articles_dir.exists():
        logger.warning("articles_dir does not exist: %s", articles_dir)
        return index
    for path in sorted(articles_dir.glob("*.json")):
        if not _is_article_json(path):
            continue
        try:
            with path.open(encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        asin = (raw.get("product") or {}).get("asin")
        if not asin:
            continue
        # 節句・正月の飾り物は年齢別 hub に入れない。「0歳〜」は初節句の赤ちゃんの意味 (#9186)
        mm = override_age_months(raw)  # 確認済みの年齢 (data/age_overrides.json) が最優先
        if mm is None:
            if is_seasonal_decoration(*seasonal_names(raw)):
                continue
            mm = parse_min_months((raw.get("persona_fit") or {}).get("age_range"))
        if mm is None:
            continue
        prev = index.get(str(asin))
        index[str(asin)] = mm if prev is None else min(prev, mm)
    return index


def build_brand_index(articles_dir: Path) -> dict[str, str]:
    """{asin: メーカーのキー} を返す。年齢 hub の同一メーカー上限に使う。

    表記ゆれ ("レゴ" / "レゴ(LEGO)" / "LEGO") は data/brand_taxonomy.yaml の
    canonical に寄せる。taxonomy に無いメーカーは canonical が全部「ノーブランド」
    になるので、そのまま使うと無関係なメーカー同士が 1 つの枠を食い合う。
    その場合は元の表記をキーにする。brand が空の商品は index に入れない
    (= 上限の対象外)。
    """
    index: dict[str, str] = {}
    if not articles_dir.exists():
        return index
    for path in sorted(articles_dir.glob("*.json")):
        if not _is_article_json(path):
            continue
        try:
            with path.open(encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        product = raw.get("product") or {}
        asin = product.get("asin")
        brand = product.get("brand")
        if not asin or not isinstance(brand, str) or not brand.strip():
            continue
        nb = normalize_brand(brand)
        key = brand.strip().casefold() if nb.match_type == "unknown" else nb.canonical
        index.setdefault(str(asin), key)
    return index


def build_theme_text_index(articles_dir: Path) -> dict[str, str]:
    """{asin: 小文字結合テキスト} を返す。同一 ASIN 複数ファイルはテキスト union。"""
    index: dict[str, str] = {}
    if not articles_dir.exists():
        logger.warning("articles_dir does not exist: %s", articles_dir)
        return index
    for path in sorted(articles_dir.glob("*.json")):
        if not _is_article_json(path):
            continue
        try:
            with path.open(encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        prod = raw.get("product") or {}
        asin = prod.get("asin")
        if not asin:
            continue
        parts: list[str] = [
            str(raw.get("title") or ""),
            str(prod.get("name") or ""),
            str(prod.get("name_full") or ""),
        ]
        parts += [str(x) for x in (raw.get("tags") or [])]
        parts += [str(x) for x in (raw.get("keywords") or [])]
        parts += [str(x) for x in (prod.get("features") or [])]
        parts += [str(x) for x in (prod.get("edu_domains") or [])]
        text = " ".join(parts).lower()
        index[str(asin)] = (index.get(str(asin), "") + " " + text).strip()
    return index


def _matches(text: str, theme: dict[str, Any]) -> bool:
    if any(kw in text for kw in theme.get("exclude", [])):
        return False
    if any(kw in text for kw in theme.get("include", [])):
        return True
    # all_of: 第 2 の合格経路。指定した全群それぞれで 1 語以上マッチしたら合格
    # (#3654 モンテッソーリ = 木製 AND 教具形態)。未指定テーマは従来どおり include のみ。
    all_of = theme.get("all_of")
    if all_of:
        return all(any(kw in text for kw in group) for group in all_of)
    return False


def build_hub(records, text_index, theme, *, top_n, min_ivs):
    """テーマにマッチする record を ivs_100 降順 (同点は安価優先) で top_n 束ねる。"""
    pool = []
    for rec in records:
        if rec.ivs_score is None or (rec.ivs_score < min_ivs):
            continue
        text = text_index.get(rec.asin)
        if not text or not _matches(text, theme):
            continue
        pool.append(rec)
    pool.sort(key=lambda r: (-(r.ivs_100 or 0), r.best_price or 10**9))
    return pool[:top_n]


def build_age_hub(records, min_months_index, hub, *, top_n, min_ivs,
                  brand_index=None, max_per_brand=None):
    """最小推奨年齢が [min_months, max_months) に入る record を ivs_100 降順で束ねる。

    brand_index と max_per_brand (>0) を渡すと、同一メーカーの件数に上限をかける
    (_cap_per_brand)。省略時は上限なし。
    """
    lo, hi = hub["min_months"], hub["max_months"]
    pool = []
    for rec in records:
        if rec.ivs_score is None or (rec.ivs_score < min_ivs):
            continue
        mm = min_months_index.get(rec.asin)
        if mm is None or not (lo <= mm < hi):
            continue
        pool.append(rec)
    pool.sort(key=_rank_key)
    if brand_index and max_per_brand and max_per_brand > 0:
        return _cap_per_brand(pool, brand_index, top_n, max_per_brand)
    return pool[:top_n]


def _rank_key(rec):
    return (-(rec.ivs_100 or 0), rec.best_price or 10**9)


def _cap_per_brand(pool, brand_index, top_n, max_per_brand):
    """スコア順の pool から、同一メーカー max_per_brand 件までで top_n 件選ぶ。

    上限を入れる前は、スコアが僅差で並ぶ 1 メーカーが上位を埋めていた
    (2026-10-12 実測: 3歳 hub の 24 件中 23 件がタカラトミー、2歳は 17 件がレゴ)。
    年齢 hub は「その年齢で何を選ぶか」を見比べる入口なので、メーカーを散らす。

    上限で外した商品だけでは top_n に届かないとき (メーカー数が少ない bucket) は、
    外した商品をスコア順に戻して埋める。件数を減らすより偏りを残す方を選ぶ。
    外れた商品は全ラインナップ節 (build_age_lineup) に回るのでリンクは失われない。
    """
    picked, held = [], []
    used: dict[str, int] = {}
    for rec in pool:
        if len(picked) >= top_n:
            break
        brand = brand_index.get(rec.asin)
        if brand is not None and used.get(brand, 0) >= max_per_brand:
            held.append(rec)
            continue
        if brand is not None:
            used[brand] = used.get(brand, 0) + 1
        picked.append(rec)
    if len(picked) < top_n:
        picked.extend(held[:top_n - len(picked)])
        picked.sort(key=_rank_key)
    return picked


def _xmas_lower_window(lo: int) -> int:
    """診断の lowerWindow と同じ。これより幼い対象年齢の物は物足りないので出さない。"""
    if lo < 24:
        return 12
    if lo < 72:
        return 24
    return 36


def xmas_score(rec, min_months: int | None, page: dict[str, Any]) -> float | None:
    """クリスマスページ 1 件の採点。None は「このページには出さない」。

    診断の scoreItem (プレゼント・〜10,000円・バランスよく) と同じ配点:
    年齢 (その年齢から 20 / 1 歳下から 14 / それより下 6) + 予算 (上限の 3 割以上 14 / 未満 7)
    + 知育スコア×0.45。対象年齢がその年齢より上の物と、価格が分からない物は出さない。
    """
    lo, hi = page["lo"], page["hi"]
    if min_months is None or min_months > hi:
        return None
    if min_months < lo - _xmas_lower_window(lo):
        return None
    price = rec.best_price
    if not isinstance(price, (int, float)) or price <= 0 or price > XMAS_BUDGET_MAX:
        return None
    if min_months >= lo:
        score = 20.0
    elif min_months >= lo - 12:
        score = 14.0
    else:
        score = 6.0
    score += 14 if price >= XMAS_BUDGET_MAX * 0.3 else 7
    score += (rec.ivs_100 or 0) * 0.45
    return score


_NAME_KEY_DROP_RE = re.compile(r"[\s　・、。,.!！?？()（）「」『』【】\[\]\-—ー_/]")


def _name_key(name: str | None) -> str:
    """同じ商品の別出品をまとめるための名前キー (診断の nameKey と同じ考え方)。"""
    return _NAME_KEY_DROP_RE.sub("", unicodedata.normalize("NFKC", name or "").lower())


def _same_product(key: str, seen: list[str]) -> bool:
    """診断の sameProduct と同じ。一致か、6 文字以上の名前がもう一方に含まれていれば同じ商品。"""
    if len(key) < 4:
        return False
    for s in seen:
        if len(s) < 4:
            continue
        if s == key or (len(s) >= 6 and s in key) or (len(key) >= 6 and key in s):
            return True
    return False


def select_xmas_items(records, min_months_index, brand_index, page, *, min_ivs,
                      hub_top: set[str] | frozenset[str] = frozenset(),
                      top_n: int = XMAS_TOP_N):
    """クリスマスページの候補を採点順に top_n 件選ぶ。

    - 同じ商品の別出品 (名前がほぼ同じ) は 1 件にする
    - 同じメーカーは XMAS_MAX_PER_BRAND 件まで (届かないときだけ戻して埋める)
    - 年齢 hub の上位 (hub_top) からは XMAS_MAX_FROM_HUB 件まで。年齢 hub と同じ商品が
      並ぶだけのページにしないため。2026-11-14 の実データでは、この上限なしだと
      0歳・2歳・3歳が年齢 hub と 67〜83% 同じになり、歯止め (6 割) で作られなかった
    """
    brand_index = brand_index or {}
    scored = []
    for rec in records:
        if rec.ivs_score is None or rec.ivs_score < min_ivs:
            continue
        s = xmas_score(rec, min_months_index.get(rec.asin), page)
        if s is not None:
            scored.append((s, rec))
    scored.sort(key=lambda x: (-x[0], -(x[1].ivs_100 or 0), x[1].best_price or 10**9, x[1].asin))
    order = {rec.asin: pos for pos, (_, rec) in enumerate(scored)}
    picked, held, seen = [], [], []
    used: dict[str, int] = {}
    from_hub = 0
    for _, rec in scored:
        if len(picked) >= top_n:
            break
        key = _name_key(rec.name)
        if _same_product(key, seen):
            continue
        # 年齢 hub の上限で外す商品の名前も seen に残す。その別出品 (ASIN 違い) を
        # 代わりに載せると、年齢 hub と同じ商品が並ぶことになるため
        seen.append(key)
        in_hub = rec.asin in hub_top
        if in_hub and from_hub >= XMAS_MAX_FROM_HUB:
            continue
        brand = brand_index.get(rec.asin)
        if brand is not None and used.get(brand, 0) >= XMAS_MAX_PER_BRAND:
            held.append(rec)
            continue
        if brand is not None:
            used[brand] = used.get(brand, 0) + 1
        from_hub += in_hub
        picked.append(rec)
    for rec in held:
        if len(picked) >= top_n:
            break
        in_hub = rec.asin in hub_top
        if in_hub and from_hub >= XMAS_MAX_FROM_HUB:
            continue
        from_hub += in_hub
        picked.append(rec)
    picked.sort(key=lambda r: order[r.asin])
    return picked


def _overlap_ratio(asins: list[str], other: set[str]) -> float:
    """asins のうち other にも入っている割合 (asins が空なら 0)。"""
    if not asins:
        return 0.0
    return sum(1 for a in asins if a in other) / len(asins)


def build_xmas_pages(records, min_months_index, brand_index, hub_tops: dict[str, set[str]],
                     *, min_ivs: float):
    """XMAS_PAGES を順に選び、歯止めを通ったページだけ返す。

    歯止め (navi-brain#166 F10 の設計):
      1. XMAS_MIN_ITEMS 件に届かない → 作らない
      2. 同じ年齢の年齢 hub の上位 XMAS_HUB_COMPARE_N 件と XMAS_OVERLAP_MAX 以上同じ → 作らない
      3. 先に作ると決めたクリスマスページと XMAS_OVERLAP_MAX 以上同じ → 作らない
    戻り値は ({key: items}, {key: 作らない理由})。
    """
    accepted: dict[str, list] = {}
    skipped: dict[str, str] = {}
    for key, page in XMAS_PAGES.items():
        items = select_xmas_items(records, min_months_index, brand_index, page, min_ivs=min_ivs,
                                  hub_top=hub_tops.get(page["hub"], set()))
        asins = [r.asin for r in items]
        if len(items) < XMAS_MIN_ITEMS:
            skipped[key] = f"too_few: {len(items)} < {XMAS_MIN_ITEMS}"
            continue
        hub_ratio = _overlap_ratio(asins, hub_tops.get(page["hub"], set()))
        if hub_ratio >= XMAS_OVERLAP_MAX:
            skipped[key] = f"overlap_hub: {page['hub']} {hub_ratio:.0%}"
            continue
        reason = ""
        for other_key, other_items in accepted.items():
            ratio = _overlap_ratio(asins, {r.asin for r in other_items})
            if ratio >= XMAS_OVERLAP_MAX:
                reason = f"overlap_xmas: {other_key} {ratio:.0%}"
                break
        if reason:
            skipped[key] = reason
            continue
        accepted[key] = items
        logger.info("%s (%s): %d items, overlap with %s top%d = %.0f%%", key, page["label"],
                    len(items), page["hub"], XMAS_HUB_COMPARE_N, hub_ratio * 100)
    for key, reason in skipped.items():
        logger.warning("%s: not built (%s)", key, reason)
    return accepted, skipped


def build_age_lineup(records, min_months_index, hub, exclude_asins):
    """年齢hub の全ラインナップ節 (#2687 Slice A) 用に bucket 全 member を返す。

    build_age_hub と異なり **min_ivs ゲートを適用しない** (低スコア長尾も網羅する
    = crawl discovery が目的)。top-24 curated (``exclude_asins``) を除いた残りを
    ivs_100 降順 (同点は best_price 昇順) で返す。discontinued フラグ (旧
    ``is_purchase_unavailable`` 相当) は ArticleRecord に無いため適用しない
    (対象は live 記事のみなので許容)。
    """
    lo, hi = hub["min_months"], hub["max_months"]
    pool = []
    for rec in records:
        if rec.asin in exclude_asins:
            continue
        mm = min_months_index.get(rec.asin)
        if mm is None or not (lo <= mm < hi):
            continue
        pool.append(rec)
    pool.sort(key=lambda r: (-(r.ivs_100 or 0), r.best_price or 10**9))
    return pool


def _lineup_entry(rec, age_min_months: int | None) -> dict[str, Any]:
    """全ラインナップ節用の最小 payload (カード用の重い項目は持たない)。"""
    return {
        "asin": rec.asin,
        "name": rec.name,
        "url_internal": f"/products/{rec.asin.lower()}/",
        "ivs_100": rec.ivs_100,
        "age_min_months": age_min_months,
    }


def serialize_hub(items, theme_key: str, generated_at: str,
                   *, lineup: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    payload_items = [
        _record_to_payload_common(rec, idx) for idx, rec in enumerate(items, start=1)
    ]
    for rec, item in zip(items, payload_items):
        if getattr(rec, "rank_days_30", 0) > 0:
            item["rank_days_30"] = rec.rank_days_30
            item["best_rank_30"] = rec.best_rank_30
    payload: dict[str, Any] = {
        "generated_at": generated_at,
        "type": theme_key,
        "count": len(payload_items),
        "items": payload_items,
    }
    if lineup is not None:
        payload["lineup"] = lineup
    return payload


def _write_hub(out_hugo: Path, key: str, items, generated_at: str,
               counts: dict[str, int], label: str,
               *, lineup: list[dict[str, Any]] | None = None) -> None:
    payload = serialize_hub(items, key, generated_at, lineup=lineup)
    (out_hugo / f"{key}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    counts[key] = len(items)
    logger.info("%s (%s): %d items", key, label, len(items))


def _write_hub_index(out_hugo: Path, entries: list[dict[str, str]]) -> None:
    """info hub 集合の単一 source of truth (#2687 Slice C)。

    生成した全 info hub (THEMES + AGE_HUBS) の ``[{key, label, url}]`` を
    平坦な JSON 配列で書く。hugo/layouts/partials/hub_siblings.html が
    ``site.Data.features.hub_index`` として直接 range する。
    """
    (out_hugo / "hub_index.json").write_text(
        json.dumps(entries, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    logger.info("hub_index: %d hubs", len(entries))


def run(articles_dir: Path, out_hugo: Path, *, top_n: int, min_ivs: float,
        themes: list[str], age_hubs: list[str] | None = None,
        per_asin_dir: Path | None = None,
        raw_root: Path | None = None,
        age_max_per_brand: int = 0, history_path: Path | None = None,
        today: date | None = None, xmas: bool = True) -> dict[str, int]:
    records = _dedupe_by_asin(load_articles(articles_dir))
    # #2812: カード画像も記事ページと同じ amazon.json の検証済み画像に揃える。
    logger.info("amazon image overlay: %d replaced",
                overlay_amazon_images(records, per_asin_dir))
    # #4007: hub カードも記事ページ・/deals/ /cospa/ と同じ日次観測の価格を出す。
    # per_asin_dir=None のときは price_overlay の既定 (data/raw/per_asin) を使う。
    # raw_root を渡すと楽天/Yahoo も matched JSON で更新する (follow-up 1)。
    stats = overlay_current_prices(records, per_asin_dir, raw_root=raw_root)
    logger.info("price_overlay: %d price_watch / %d per_asin / %d no observation",
                stats["price_watch"], stats["per_asin"], stats["none"])
    logger.info(
        "market_prices: %d rakuten / %d yahoo updated from matched JSON, "
        "%d extreme outlier dropped",
        stats["market_rakuten"], stats["market_yahoo"], stats["market_extreme_dropped"],
    )
    generated_at = _now_iso()
    ranking_history = load_rank_history(history_path or Path("hugo/data/ranking/history.json"))
    out_hugo.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    hub_index: list[dict[str, str]] = []
    if themes:
        text_index = build_theme_text_index(articles_dir)
        for key in themes:
            theme = THEMES[key]
            # テーマ個別 min_ivs があれば優先 (#3654 montessori=3.4)。
            theme_min_ivs = theme.get("min_ivs", min_ivs)
            items = build_hub(records, text_index, theme,
                              top_n=top_n, min_ivs=theme_min_ivs)
            items = prioritize_ranked(items, ranking_history, today)
            _write_hub(out_hugo, key, items, generated_at, counts, theme["label"])
            hub_index.append({"key": key, "label": theme["label"],
                              "url": f"/{key}-toys/"})
    mm_index = build_min_months_index(articles_dir) if age_hubs else None
    age_brand_index = build_brand_index(articles_dir) if age_hubs and age_max_per_brand > 0 else None
    age_tops: dict[str, set[str]] = {}
    if age_hubs:
        for key in age_hubs:
            hub = AGE_HUBS[key]
            items = build_age_hub(records, mm_index, hub,
                                  top_n=top_n, min_ivs=min_ivs,
                                  brand_index=age_brand_index,
                                  max_per_brand=age_max_per_brand)
            items = prioritize_ranked(items, ranking_history, today)
            # クリスマスページの歯止めは、実際に載る年齢 hub の上位と比べる
            # (配信ビルドは --top-n 24 なので XMAS_HUB_COMPARE_N と同じ件数になる)
            age_tops[key] = {r.asin for r in items[:XMAS_HUB_COMPARE_N]}
            lineup_recs = build_age_lineup(
                records, mm_index, hub, exclude_asins={r.asin for r in items})
            lineup = [_lineup_entry(r, mm_index.get(r.asin)) for r in lineup_recs]
            _write_hub(out_hugo, key, items, generated_at, counts, hub["label"],
                      lineup=lineup)
            hub_index.append({"key": key, "label": hub["label"],
                              "url": f"/toys-{key}/"})
    # navi-brain#166 F15: 新着棚は年齢 hub の生成有無にかかわらず毎回出力する。
    if mm_index is None:
        mm_index = build_min_months_index(articles_dir)
    new_brand_index = age_brand_index if age_brand_index is not None else build_brand_index(articles_dir)
    top_items = select_new_arrivals(records, ranking_history, mm_index, new_brand_index, today=today)
    top_payload = _new_arrivals_payload(top_items) if len(top_items) >= NEW_ARRIVAL_MIN else []
    age_payload = {}
    for key, hub in AGE_HUBS.items():
        lo = max(0, hub["min_months"] - NEW_ARRIVAL_AGE_LOOKBACK_MONTHS)
        selected = select_new_arrivals(records, ranking_history, mm_index, new_brand_index,
                                       today=today, age_range=(lo, hub["max_months"]))
        if len(selected) >= NEW_ARRIVAL_MIN:
            age_payload[key] = _new_arrivals_payload(selected)
    (out_hugo / "new_arrivals.json").write_text(json.dumps({
        "generated_at": generated_at, "window_days": NEW_ARRIVAL_WINDOW_DAYS,
        "top": top_payload, "age": age_payload,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    counts["new_arrivals_top"] = len(top_payload)
    if xmas:
        _write_xmas_pages(records, mm_index, new_brand_index, age_tops, out_hugo, generated_at,
                          counts, min_ivs=min_ivs, age_max_per_brand=age_max_per_brand,
                          ranking_history=ranking_history, today=today)
    if themes or age_hubs:
        _write_hub_index(out_hugo, hub_index)
    return counts


def _write_xmas_pages(records, mm_index, brand_index, age_tops, out_hugo: Path, generated_at: str,
                      counts: dict[str, int], *, min_ivs: float, age_max_per_brand: int,
                      ranking_history, today) -> None:
    """navi-brain#166 F10: クリスマスページ 7 本分の JSON と xmas_index.json を書く。

    作らないページも items を空にして書く (Hugo 側は items が空なら noindex にして
    サイトマップから外す。partials/is_empty_feature.html)。xmas_index.json には作った
    ページだけを載せ、年齢 hub とクリスマスページどうしのリンクはこれだけを見る。
    """
    hub_tops = dict(age_tops)
    for hub_key in {p["hub"] for p in XMAS_PAGES.values()} - set(hub_tops):
        # --age-hubs で年齢 hub を絞ったときも、比べる相手は本番と同じ選び方で作る
        hub_items = build_age_hub(records, mm_index, AGE_HUBS[hub_key],
                                  top_n=XMAS_HUB_COMPARE_N, min_ivs=min_ivs,
                                  brand_index=brand_index, max_per_brand=age_max_per_brand)
        hub_tops[hub_key] = {r.asin for r in hub_items}
    accepted, skipped = build_xmas_pages(records, mm_index, brand_index, hub_tops, min_ivs=min_ivs)
    index = []
    for key, page in XMAS_PAGES.items():
        items = prioritize_ranked(accepted.get(key, []), ranking_history, today)
        payload = serialize_hub(items, key, generated_at)
        payload["label"] = page["label"]
        payload["budget_max"] = XMAS_BUDGET_MAX
        if key in skipped:
            payload["skipped"] = skipped[key]
        (out_hugo / f"{key}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        counts[key] = len(items)
        if items:
            index.append({"key": key, "label": page["label"], "hub": page["hub"],
                          "url": f"/{key}/"})
    (out_hugo / "xmas_index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--articles-dir", default="data/articles", type=Path)
    parser.add_argument("--out-hugo", default="hugo/data/features", type=Path)
    parser.add_argument("--top-n", type=int, default=24)
    parser.add_argument("--min-ivs", type=float, default=3.8)
    parser.add_argument("--themes", nargs="*", default=list(THEMES.keys()),
                        choices=list(THEMES.keys()))
    parser.add_argument("--age-hubs", nargs="*", default=list(AGE_HUBS.keys()),
                        choices=list(AGE_HUBS.keys()),
                        help="生成する年齢 hub キー (既定=全件)。")
    parser.add_argument("--age-max-per-brand", type=int, default=AGE_MAX_PER_BRAND,
                        help="年齢 hub の上位に載せる同一メーカーの上限件数 (0 で上限なし)。")
    # #4007: 価格を日次観測で上書きするための per_asin スナップショット置き場。
    parser.add_argument("--per-asin-dir", default=Path("data/raw/per_asin"), type=Path)
    # #4007 follow-up 1: data/raw/{rakuten,yahoo}_matched.json の親ディレクトリ。
    # 楽天/Yahoo 価格を build_post.py と同じ matched JSON で更新するために使う。
    parser.add_argument("--raw-root", default=Path("data/raw"), type=Path)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level,
                        format="%(levelname)s %(name)s: %(message)s")
    counts = run(args.articles_dir, args.out_hugo,
                 top_n=args.top_n, min_ivs=args.min_ivs,
                 themes=args.themes, age_hubs=args.age_hubs,
                 per_asin_dir=args.per_asin_dir,
                 raw_root=args.raw_root,
                 age_max_per_brand=args.age_max_per_brand)
    print(json.dumps(counts, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
