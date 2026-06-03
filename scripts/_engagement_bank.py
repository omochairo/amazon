"""
Issue #1526 — engagement news trend のキーワード bank 定義。

NEGATIVE_BANK: アカウント毀損リスク語 (含む見出しは即除外)
POSITIVE_BANK_TIERS: tier 別 weight 付き親層関連語 (score 加点)
SOURCE_WEIGHT: source 別の信頼度・質係数

[[project-omochairo-engagement-personas]] で確定した境界:
- 解禁: 不審者 / 侵入 / ワクチン / 予防接種 / 食中毒 / 塾 / 発熱 / 風邪
- 継続除外: 死/殺/事件/容疑者/性関連/政治/宗教/皇室/戦争/発達障害/いじめ/不登校
"""
from __future__ import annotations

NEGATIVE_BANK: list[str] = [
    # 性関連
    "性教育", "性犯罪", "性被害", "性的", "わいせつ", "盗撮", "痴漢",
    # 死・殺
    "死去", "死亡", "殺害", "殺人", "遺体", "自殺", "心中", "自死",
    "刺殺", "撲殺", "絞殺",
    # 重犯罪
    "逮捕", "容疑者", "通り魔", "刺傷", "暴行", "誘拐", "連れ去り",
    # デリケート医療
    "発達障害", "ADHD", "自閉症", "ASD",
    "虐待", "DV", "ネグレクト",
    "薬害", "医療ミス", "誤診",
    # いじめ・不登校
    "いじめ", "不登校",
    # 政治・宗教・皇室
    "政治家", "選挙", "政党", "宗教", "カルト", "天皇", "皇室",
    # 戦争・国際
    "戦争", "紛争", "ミサイル", "テロ",
    "イラン", "イスラエル", "ウクライナ", "ロシア", "ハマス",
    # その他重事故
    "事件", "事故死", "心肺停止", "重体", "重傷",
    "異物混入",
]

POSITIVE_BANK_TIERS: dict[str, dict] = {
    "core_parenting": {
        "weight": 3.0,
        "words": [
            "子育て", "育児", "ワンオペ", "ママ", "パパ", "母親", "父親",
            "子供", "子ども", "赤ちゃん", "新生児", "乳幼児", "幼児",
            "我が子", "息子", "娘", "兄弟", "姉妹", "兄妹", "姉弟",
            "家族", "親子", "親バカ", "保護者", "夫婦",
        ],
    },
    "child_daily": {
        "weight": 2.5,
        "words": [
            "保育園", "幼稚園", "保育士", "学童", "預かり保育",
            "離乳食", "幼児食", "授乳", "断乳", "ミルク", "粉ミルク",
            "おむつ", "オムツ", "夜泣き", "寝かしつけ", "お昼寝",
            "イヤイヤ", "イヤイヤ期", "魔の2歳", "魔の3歳",
            "トイトレ", "トイレトレーニング", "歯磨き",
            "発達", "成長", "予防接種", "ワクチン", "健診", "母子手帳",
            "産後", "妊娠", "出産", "里帰り",
        ],
    },
    "education": {
        "weight": 2.0,
        "words": [
            "知育", "知育玩具", "おもちゃ", "絵本", "図鑑",
            "学習", "教育", "食育",
            "塾", "受験", "中学受験", "中受",
            "小学校", "中学校", "高校", "高校生", "中学生", "小学生",
            "学校", "学級閉鎖", "登園", "登校",
            "入園", "入学", "卒園", "卒業",
            "運動会", "学習発表会", "授業参観", "遠足", "お遊戯会",
            "PTA", "給食", "ランドセル", "宿題", "通学", "通園",
            "モンテッソーリ", "STEM", "プログラミング教育", "ICT",
            "図書館", "奨学金", "幼児教室", "習い事", "ピアノ", "スイミング",
        ],
    },
    "health": {
        "weight": 1.8,
        "words": [
            "インフル", "インフルエンザ", "コロナ", "RSウイルス", "RS感染",
            "手足口", "風邪", "発熱", "ノロ", "アデノ", "プール熱",
            "溶連菌", "胃腸炎", "アレルギー", "アトピー", "ぜんそく",
            "食中毒", "熱中症", "脱水",
        ],
    },
    "disaster": {
        "weight": 1.6,
        "words": [
            "台風", "大雨", "暴風", "豪雨", "地震", "津波", "猛暑", "寒波",
            "大雪", "雷", "洪水", "土砂", "停電", "断水", "警報", "注意報",
            "休校", "休園", "臨時休校", "臨時休園", "登校禁止",
            "避難所", "避難",
            "不審者", "侵入",
        ],
    },
    "season": {
        "weight": 1.4,
        "words": [
            "GW", "ゴールデンウィーク", "お盆", "夏休み", "冬休み", "春休み",
            "連休", "クリスマス", "ハロウィン", "正月", "節分",
            "ひな祭り", "こどもの日", "七夕", "お月見",
            "父の日", "母の日", "敬老の日", "バレンタイン", "ホワイトデー",
            "誕生日", "七五三", "お宮参り", "お食い初め", "初節句",
        ],
    },
    "character_goods": {
        "weight": 1.2,
        "words": [
            "アンパンマン", "プリキュア", "戦隊", "仮面ライダー", "ポケモン",
            "すみっコ", "ちいかわ", "ディズニー", "ジブリ",
            "サンリオ", "シナモロール", "クロミ", "ハローキティ",
            "シルバニア", "リカちゃん", "メルちゃん",
            "ベビーカー", "抱っこ紐", "チャイルドシート",
        ],
    },
}

SOURCE_WEIGHT: dict[str, float] = {
    "asahi_edu": 1.2,
    "google_news_topic": 1.0,
    "nhk_main": 1.0,
    "nhk_social": 1.0,
    "nhk_life": 1.0,
    "yahoo_domestic": 0.9,
    "yahoo_life": 0.9,
    "google_trends_new": 1.0,
    "dowjones_japanlife": 0.8,
}


def freshness_weight(age_hours: float) -> float:
    """user 指示: 6h=1.3 / 12h=1.2 / 24h=1.0 / 48h=0.7 / 72h=0.4 / >72h=0.0"""
    if age_hours <= 6:
        return 1.3
    if age_hours <= 12:
        return 1.2
    if age_hours <= 24:
        return 1.0
    if age_hours <= 48:
        return 0.7
    if age_hours <= 72:
        return 0.4
    return 0.0


def contains_negative(text: str) -> list[str]:
    """text に含まれる NEGATIVE_BANK 語を返す。空ならクリーン。"""
    return [w for w in NEGATIVE_BANK if w in text]


def score_positive(text: str) -> tuple[float, dict[str, list[str]]]:
    """tier ごとに word match 検査。tier hit は binary 加算 (同 tier 複数 word でも +weight 1 回)。"""
    total = 0.0
    matched: dict[str, list[str]] = {}
    for tier_name, tier in POSITIVE_BANK_TIERS.items():
        hits = [w for w in tier["words"] if w in text]
        if hits:
            total += tier["weight"]
            matched[tier_name] = hits
    return total, matched
