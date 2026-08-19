"""Article quality gate for おもちゃいろ v4.

Checks article JSON (and rendered Markdown if available) against:
- Schema (data/schema/article.schema.json)
- Word/char count thresholds
- Heading hierarchy (h1 -> h2 -> h3)
- Required sections in narrative
- SEO: product-name occurrence in title/meta/h1/h2/body
- SERP fit: 商品名 + 検索意図語 1 つが title の先頭 30 字に収まるか (#5083 項目2)
- Forbidden childish tone tokens
- FAQ completeness

Prints a per-article verdict and exits non-zero if ANY article fails the
configured minimum.

Note (#4826, 2026-08-10): the <slug>.quality.json sidecar this script used to
write next to each input file has been retired. Corpus-wide quality is observed
by .github/workflows/48-quality-census.yml, which aggregates into a single
data/analytics/quality_census.json instead of one derived file per article.

Usage:
    python scripts/quality_gate.py
    python scripts/quality_gate.py --src data/articles/ --posts hugo/content/posts/
    python scripts/quality_gate.py --min-score 70 --strict
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Optional

try:
    # Reuse build_post's verification logic so quality_gate and the actual
    # rendered template agree on what counts as "verified". Loaded lazily so
    # quality_gate still works when run in isolation without build_post on
    # the import path.
    from build_post import _load_matched_index as _bp_load_matched_index
    from build_post import _matched_passes_quality as _bp_matched_passes_quality
except ImportError:  # pragma: no cover - best-effort fallback
    _bp_load_matched_index = None
    _bp_matched_passes_quality = None
from typing import Any, Optional

try:
    from jsonschema import Draft7Validator
except ImportError:
    Draft7Validator = None  # type: ignore

try:
    import frontmatter  # type: ignore
except ImportError:  # pragma: no cover - best-effort fallback
    frontmatter = None  # type: ignore

import stock_status


# 幼児口調・子ども向け演出は禁止（女性誌調をキープするため）。
# 「おもちゃロボ」はサイト公式キャラ（AI編集ロボの名称）として narrative や
# editorial_comment に登場可。ただし「おもちゃロボがしらべたよ」のように
# 幼児口調と組み合わせると下記パターンに引っかかるので注意。
FORBIDDEN_TONE_PATTERNS = [
    r"だよ[。！\s]",
    r"なんだ[。！\s]",
    r"みてね",
    r"しらべたよ",
    r"ぼく[はが、]",
    r"だね[。！\s]",
]

# narrative.lead 専用の禁止フレーズ (PROMPT_TEMPLATE.md §1.B WARNING)。
# meta_description のメタ説明調が lead に流入する事故 (#506) を catch する。
# title/meta_description には適用しない (そちらは SEO 用で「3サイト横断」等は許容)。
LEAD_FORBIDDEN_PATTERNS = [
    r"本記事では",  # 「本記事ではおもちゃロボが…」型のメタ説明 (lead では全面禁止)
    r"3\s*サイト[をで]?\s*横断",  # 「3サイト横断で徹底比較」マーケ常套句
    r"丁寧に(比較|解説)",  # 「丁寧に比較しました」「丁寧に解説します」
    r"でしょうか[。\s]+本記事",  # 「〜なのでしょうか。本記事では…」型のメタ説明
]

REQUIRED_NARRATIVE_KEYS = [
    "lead",
    "why_this_product",
    "gift_appeal",
    "daily_use",
    "safety_note",
    "closing",
]

NARRATIVE_MIN_CHARS = {
    "lead": 120,
    "why_this_product": 150,
    "gift_appeal": 120,
    "daily_use": 150,
    "safety_note": 120,
    "closing": 120,
}

# #3203 Phase 1-A: narrative.how_to_choose (比較・選び分け) 施行日ゲート。
# この日付以降の slug (YYYY-MM-DD-ASIN) のみ必須にする。既存記事には遡及しない。
HOW_TO_CHOOSE_ENFORCE_FROM = "2026-07-16"
HOW_TO_CHOOSE_MIN_CHARS = 150
# #4826 項目2: 本文に生の ASIN コードを書かない規律の soft スコア。
# 施行日より前の slug では合否 (passed) を変えず、census の「減点のみ」に
# 発火率を出すためだけの値として残す。
HOW_TO_CHOOSE_INLINE_ASIN_SOFT_SCORE = 0.8
# 生 ASIN 表記を hard にする施行日 (#4826 項目2 の昇格)。
#
# プロンプト v7.2 (amazon-navi-brain、commit 2026-08-10T01:00Z) が入ったあとに
# 生成された記事の実測:
#
#   2026-08-11 以降の 163 本 … hard 不合格 0 / soft 減点 **0**
#   2026-08-10 当日の 24 本  … soft 減点 1 (v7.2 の前後が混在する日)
#
# soft 導入時 (#4855) に置いた昇格条件「施行後に新規追加された記事の発火が 0 なら
# hard へ」を満たしている。n=10 だった当時は 95% 上限が約 30% だったが、n=163 では
# 約 1.8% (rule of three) まで締まった。
#
# 既存 94 本 (2026-07-16〜08-10 = v7 施行後・v7.2 前の窓) を巻き込まないよう、
# HOW_TO_CHOOSE_ENFORCE_FROM とは別の施行日を持つ。ゲートは PR の変更ファイルに
# しか当たらず、リライトは新 slug で追加される (#2711) ので、既存 94 本が発火する
# のはアドホックな一括修正 PR (実測で月 1 回未満) のときだけ。そこで落ちると
# 無関係な修正が止まるので、施行日で切る — `HOW_TO_CHOOSE_ENFORCE_FROM` を
# 置いたときと同じ理由。
HOW_TO_CHOOSE_INLINE_ASIN_ENFORCE_FROM = "2026-08-11"

# #5088 タグ粒度。data/articles 全 1,977 本の実測 (2026-08-14) で校正した:
#   - 記事あたりの「他記事に 1 本も無いタグ」の比率は中央値 0.2 (5 個中 1 個)。
#     0.4 に置くと 22.8% が発火して選別にならないので、0.6 (発火 5.9%) を採る。
#   - tag == product.name/name_full は全体 12.4%・直近 (2026-07 以降) 20.4% と
#     増加傾向。しきい値の要らない決定的な違反なので個数で減点する。
#   - 「タグが同一記事内の別タグの部分文字列 かつ corpus 新出」は 2.2% (44 本)。
#     「イト」(商品名 "アークライト ito(イト)レインボー" 由来) を捕まえるのは
#     この規則だけ。断片の疑いとして warn する。
# 文字数ベースの規則 (例: len<=2 を断片とみなす) は採らない。実測 101 種の
# 2 文字タグはレゴ/学研/恐竜/1歳のような正当な束ね語で、断片は 1 種だけだった。
TAG_NOVEL_RATIO_WARN = 0.6

# #5083 項目2: SERP に表示される先頭だけで「商品名 + 検索意図」が読めること。
#
# 日本語 SERP の title は全角 30〜35 字あたりで打ち切られる。ここで見えている
# 範囲に「どの商品か」と「その検索意図に答えているか」の両方が入っていないと、
# 読者は自分のクエリに対する答えだと判断できない。項目1 (サフィックス短縮) で
# title 全体は 78 → 53 字まで縮んだが、**縮んだのは切れる位置より後ろ**なので
# 見える範囲は 1 文字も変わっていない。
#
# 実測 (data/articles 2,061 本、2026-08-17):
#   - 先頭 30 字に product.name が同一表記で入る … 85.0%
#   - 先頭 30 字に検索意図語が入る             … 53.3%
#   - 両方                                    … 53.3%
#   束縛条件は意図語だけで、識別性はほぼ無料。**直近 (2026-07-16 以降) の 571 本
#   では 45.0% と corpus 全体より低い**ので、放っておいて改善する類ではない。
#
# 落ちている 1,002 本を読むと原因は 2 つに分かれる:
#   A. product.name 自体が長い (26 字超が 19.4%)。意図語を置く余地が無い
#   B. title が product.name の周りに語を足している。product.name が 11 字
#      ("くもんの日本地図パズル") なのに title は "くもん出版(KUMON PUBLISHING)
#      くもんの日本地図パズル 日本の世界遺産すごろく付きの…" と Amazon の
#      正式タイトル由来の型番・シリーズ・英字併記を盛り直していて、意図語が
#      50 字目に押し出される
# メッセージで A と B を撃ち分ける (生成側が直す場所が違うため)。
#
# 30 字にしたのは、切り詰め位置が端末・クエリで揺れるなかで最も保守的に見える
# 下限だから。28 字だと発火 59.7% で「ほぼ全部鳴る」に寄り、32 字だと 37.8% まで
# 落ちて切れる直前を許してしまう。
TITLE_SERP_HEAD_CHARS = 30
# product.name がこれを超えると、意図語 (最短 "の最安値" = 4 字) を先頭 30 字に
# 置けない。この場合は title ではなく product.name 側 (§4 の「短い通称」) の問題。
TITLE_SERP_MAX_NAME_CHARS = TITLE_SERP_HEAD_CHARS - len("の最安値")
# 検索意図語。読者が商品名に添えて打つクエリ語で、#5083 の計測もこの語彙で数えた。
# 「何歳」は「対象年齢」の口語形なので同じ意図として数える。
TITLE_SERP_INTENT_WORDS = (
    "口コミ", "最安値", "対象年齢", "何歳", "比較", "評判",
    "レビュー", "徹底", "価格", "遊び方", "選び方", "安い",
)
# 合否は変えず score だけ下げる (#4826 項目2 / #5088 と同じ warn-only の流儀)。
# 施行日ゲートは置かない — census (#4828) が main 全量で発火率を出すので、
# 既存記事も含めて数えないと項目3 の before/after が測れない。
TITLE_SERP_FIT_SOFT_SCORE = 0.8


def _normalize_tag(value: Any) -> str:
    """空白差・大小文字差を吸収したタグ比較キー。"""
    return re.sub(r"\s+", "", str(value or "")).lower()


@dataclass
class TagCorpus:
    """「タグ -> それを持つ記事数」と、数えた記事の slug 集合。

    slug 集合が要るのは自己カウントを引くため。判定したいのは常に
    「**自分以外に**このタグを持つ記事があるか」で、母集団に自分が
    含まれるかは文脈で変わる:

    - census (main 全量): 記事は corpus に入っている → 自分の 1 を引く
    - PR 検証: 記事はまだ corpus に無い → 引かない

    これを区別しないと、PR 時だけ「既存 1 本と共有しているタグ」が
    未共有に見え、同じ記事が merge 前後で違う判定になる。
    """

    counts: collections.Counter
    slugs: set[str]

    def others(self, tag: Any, slug: str | None) -> int:
        """slug の記事を除いて、このタグを持つ記事数。"""
        n = self.counts.get(_normalize_tag(tag), 0)
        if slug is not None and slug in self.slugs:
            n -= 1
        return max(0, n)


def load_tag_corpus(src: pathlib.Path) -> TagCorpus | None:
    """記事 JSON 群から TagCorpus を作る。

    PR 検証時 (04-validate-article-pr.yml) の ``--src`` は変更分だけを
    コピーした mktemp なので、束ね能力の判定には使えない。corpus は常に
    リポジトリの data/articles/ を別途読む (存在しなければ None = skip)。
    """
    if not src.exists() or not src.is_dir():
        return None
    counter: collections.Counter = collections.Counter()
    slugs: set[str] = set()
    files = [
        p for p in src.glob("*.json")
        if not p.stem.endswith((".enrichment", ".seo", ".quality"))
    ]
    if not files:
        return None
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        tags = data.get("tags")
        if not isinstance(tags, list):
            continue
        slugs.add(str(data.get("slug") or path.stem))
        for tag in {_normalize_tag(t) for t in tags if str(t).strip()}:
            counter[tag] += 1
    if not counter:
        return None
    return TagCorpus(counts=counter, slugs=slugs)


@dataclass
class CheckResult:
    name: str
    passed: bool
    score: float  # 0.0 - 1.0
    message: str = ""


@dataclass
class ArticleReport:
    slug: str
    path: str
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def total_score(self) -> int:
        if not self.checks:
            return 0
        return int(round(sum(c.score for c in self.checks) / len(self.checks) * 100))

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "path": self.path,
            "total_score": self.total_score,
            "passed": self.passed,
            "checks": [
                {"name": c.name, "passed": c.passed, "score": round(c.score, 2), "message": c.message}
                for c in self.checks
            ],
        }


def _is_legacy_article(data: dict) -> bool:
    """Check if the article date is on or before 2026-05-17 to treat it as legacy."""
    date_str = data.get("date")
    if not date_str:
        return True
    try:
        dt = date_str.split("T")[0]
        if dt <= "2026-05-17":
            return True
    except Exception:
        pass
    return False


def _count_chars(text) -> int:
    if text is None:
        return 0
    if isinstance(text, str):
        return len(text)
    if isinstance(text, list):
        return sum(len(s) for s in text if isinstance(s, str))
    return 0


def check_schema(data: dict, schema: dict) -> CheckResult:
    if Draft7Validator is None:
        return CheckResult("schema", True, 1.0, "jsonschema not installed; skipped")
    validator = Draft7Validator(schema)
    errors = sorted(validator.iter_errors(data), key=lambda e: e.path)
    if not errors:
        return CheckResult("schema", True, 1.0, "OK")
    msgs = [f"{'/'.join(map(str, e.path))}: {e.message}" for e in errors[:5]]
    return CheckResult("schema", False, 0.0, "; ".join(msgs))


def check_title_seo(data: dict, product_name: str) -> CheckResult:
    title = data.get("title", "")
    if not title:
        return CheckResult("title_seo", False, 0.0, "empty title")
    head = title[:60]
    has_name = product_name and product_name in head
    length_ok = 20 <= len(title) <= 80
    score = (1.0 if has_name else 0.0) * 0.7 + (1.0 if length_ok else 0.0) * 0.3
    msg = []
    if not has_name:
        msg.append(f"product name '{product_name}' missing in first 60 chars")
    if not length_ok:
        msg.append(f"length {len(title)} not in 20-80")
    return CheckResult("title_seo", score >= 0.7, score, "; ".join(msg) or "OK")


def check_title_serp_fit(data: dict, product_name: str) -> CheckResult:
    """#5083 項目2: title の先頭 30 字だけで「商品名 + 検索意図語 1 つ」が読めるか。

    `check_title_seo` が見ているのは「冒頭 60 字以内に商品名」で、SERP で実際に
    表示される範囲 (全角 30〜35 字) より広い。つまり既存のゲートを満点で通っても、
    読者が見る範囲には商品名しか無く**検索意図語が切れている**という状態が通る。
    実測ではそれが半数近くで起きていた。

    warn-only (`passed` は True のまま score を下げる)。理由は 2 つ:
      - 記事生成レーンは auto-merge で回っているので、既存の 46.7% を一斉に
        hard fail にすると生成が止まる
      - quality_census (#4828) が「減点のみ」を週次で拾うので、hard 化の判断材料に
        なる発火率と、項目3 が求める before/after がそのまま取れる

    意図語を **1 つだけ**前半に置けば足りる。現状 1 タイトルあたり平均 3.89 語を
    並べていて、そのほとんどが切れる位置より後ろにあるだけなので、増やす必要は
    無い (#2717 の「定型句だけで済ませない」とも衝突しない — 残りの枠はその商品
    固有の差別化シグナルに使える)。
    """
    title = data.get("title", "")
    if not title:
        # 空 title は check_title_seo が hard fail で拾う。二重に鳴らさない。
        return CheckResult("title_serp_fit", True, 1.0, "skipped (empty title)")

    head = title[:TITLE_SERP_HEAD_CHARS]
    name_ok = bool(product_name) and product_name in head
    intent_hits = [w for w in TITLE_SERP_INTENT_WORDS if w in head]
    if name_ok and intent_hits:
        return CheckResult("title_serp_fit", True, 1.0,
                           f"OK ({intent_hits[0]} @ head{TITLE_SERP_HEAD_CHARS})")

    notes: list[str] = []
    if not name_ok:
        if len(product_name) > TITLE_SERP_MAX_NAME_CHARS:
            # 原因A: 名前が長すぎて意図語の余地が無い。直す場所は product.name。
            notes.append(
                f"product.name が {len(product_name)} 字 "
                f"(先頭 {TITLE_SERP_HEAD_CHARS} 字に意図語を置くには "
                f"{TITLE_SERP_MAX_NAME_CHARS} 字以下): §4 の「短い通称」に寄せる"
            )
        else:
            # 原因B: 名前は短いのに title 側で語を盛り直している。
            notes.append(
                f"先頭 {TITLE_SERP_HEAD_CHARS} 字に product.name が無い "
                f"(name={len(product_name)} 字): title で型番・シリーズ・"
                "ブランドの英字併記などを足し直さない"
            )
    if not intent_hits:
        first = next((title.find(w) for w in TITLE_SERP_INTENT_WORDS if w in title), -1)
        where = f"{first} 字目" if first >= 0 else "タイトル全体に無し"
        notes.append(
            f"検索意図語が先頭 {TITLE_SERP_HEAD_CHARS} 字に無い ({where}): "
            "商品名の直後に 1 つ置く"
        )
    return CheckResult(
        "title_serp_fit", True, TITLE_SERP_FIT_SOFT_SCORE,
        "; ".join(notes) + " (warn-only, #5083 項目2)",
    )


def check_meta_description(data: dict, product_name: str) -> CheckResult:
    meta = data.get("meta_description", "")
    head = meta[:40]
    has_name = product_name and product_name in head
    length_ok = 100 <= len(meta) <= 160
    score = (1.0 if has_name else 0.0) * 0.6 + (1.0 if length_ok else 0.0) * 0.4
    msg = []
    if not has_name:
        msg.append("product name missing in first 40 chars")
    if not length_ok:
        msg.append(f"length {len(meta)} not in 100-160")
    return CheckResult("meta_description", score >= 0.7, score, "; ".join(msg) or "OK")


def check_keywords(data: dict, product_name: str, brand: str) -> CheckResult:
    kws = data.get("keywords", [])
    if not isinstance(kws, list):
        return CheckResult("keywords", False, 0.0, "keywords not a list")
    count_ok = 5 <= len(kws) <= 15
    has_product = any(product_name and product_name in k for k in kws)
    has_brand = bool(brand) and any(brand in k for k in kws)
    score = sum([count_ok, has_product, has_brand]) / 3.0
    msg = []
    if not count_ok:
        msg.append(f"count {len(kws)} not in 5-15")
    if not has_product:
        msg.append("product name not in any keyword")
    if not has_brand and brand:
        msg.append(f"brand '{brand}' not in any keyword")
    return CheckResult("keywords", score >= 0.66, score, "; ".join(msg) or "OK")


def check_narrative(data: dict, product_name: str) -> CheckResult:
    narrative = data.get("narrative", {})
    missing = [k for k in REQUIRED_NARRATIVE_KEYS if k not in narrative]
    if missing:
        return CheckResult("narrative", False, 0.0, f"missing keys: {missing}")

    issues = []
    char_score_sum = 0.0
    for key in REQUIRED_NARRATIVE_KEYS:
        text = narrative.get(key, "")
        min_chars = NARRATIVE_MIN_CHARS.get(key, 100)
        actual = _count_chars(text)
        if actual < min_chars:
            issues.append(f"{key} {actual}<{min_chars}")
            char_score_sum += actual / min_chars
        else:
            char_score_sum += 1.0

    total = char_score_sum / len(REQUIRED_NARRATIVE_KEYS)
    msg = "; ".join(issues) if issues else "OK"
    return CheckResult("narrative", total >= 0.7, total, msg)


_ASIN_MENTION_RE = re.compile(r"\bB0[A-Z0-9]{8}\b")


def _how_to_choose_enforced(data: dict) -> bool:
    slug = data.get("slug") or ""
    if not isinstance(slug, str) or len(slug) < 10:
        # slug が無い/短すぎる (=施行日を判定できない) 場合は安全側で施行する
        return True
    return slug[:10] >= HOW_TO_CHOOSE_ENFORCE_FROM


def _inline_asin_enforced(data: dict) -> bool:
    """生 ASIN 表記を hard で判定してよい slug か (#4826 項目2 の昇格)。"""
    slug = data.get("slug") or ""
    if not isinstance(slug, str) or len(slug) < 10:
        # slug が無い/短すぎる場合は安全側で施行する (施行日ゲートと同じ流儀)。
        return True
    return slug[:10] >= HOW_TO_CHOOSE_INLINE_ASIN_ENFORCE_FROM


def _inline_asin_soft(mentioned_all: set[str], hard_ok_message: str = "OK",
                      *, data: Optional[dict] = None) -> CheckResult:
    """本文に生の ASIN コードを書かない規律 (#4826 項目2)。

    施行日 (HOW_TO_CHOOSE_INLINE_ASIN_ENFORCE_FROM) 以降の slug では **hard**。
    それより前の slug では従来どおり合否を変えず score だけ下げ、quality_census が
    「減点のみ (passed=True かつ score<1.0)」として拾えるようにしておく
    (既存 94 本の残存が census で見えなくなると、消化の進み方が追えなくなる)。
    """
    if not mentioned_all:
        return CheckResult("how_to_choose", True, 1.0, hard_ok_message)
    codes = sorted(mentioned_all)
    if data is not None and _inline_asin_enforced(data):
        return CheckResult(
            "how_to_choose", False, HOW_TO_CHOOSE_INLINE_ASIN_SOFT_SCORE,
            f"how_to_choose に生の ASIN コード: {codes} "
            "(読者に意味がないので商品名か特徴で書くこと)",
        )
    return CheckResult(
        "how_to_choose", True, HOW_TO_CHOOSE_INLINE_ASIN_SOFT_SCORE,
        f"how_to_choose に生の ASIN コード: {codes} "
        "(soft: 表記規律。読者に意味がないので生成側で書かないこと)",
    )


def check_how_to_choose(data: dict) -> CheckResult:
    """#3203 Phase 1-A: narrative.how_to_choose (比較・選び分け) の機械検査。

    1. 施行日ゲート: slug 先頭 10 文字 (YYYY-MM-DD) が HOW_TO_CHOOSE_ENFORCE_FROM
       より前なら skip (既存記事の軽微修正 PR を落とさない)
    2. 存在 + 合計 150 字以上 (NARRATIVE_MIN_CHARS と同じ枠組み)
    3. ASIN 封じ込め (hard): 本文中の B0[A-Z0-9]{8} は
       data/raw/per_asin/<ASIN>/competitors.json の asin 集合の部分集合であること
       (自商品の ASIN への言及は許可)。competitors.json が読めない場合は
       ASIN 言及ゼロのみ合格 (安全側フォールバック)
    4. 生 ASIN 表記 (soft, #4826 項目2): 3 を通っても本文に生の ASIN コードが
       あれば score のみ下げる。自商品の ASIN も対象 (読者に意味がないのは同じ)。

    3 と 4 を分けている理由 (2026-08-10 実測):
      封じ込め (3) は「実在する競合か」の**捏造検査**であって、ASIN コードを
      読者に見せてよいかは見ていない。実際 main 全量 1913 件のうち 3 で落ちるのは
      11 件だが、生 ASIN が本文に出ているのは 107 件ある (build_post の
      _strip_inline_asin_codes が拾ったのと同じ 107 件)。レンダリング側の除去は
      catch であって prevent ではなく、地の文に埋まった裸のコードは日本語が壊れる
      ので除去できない。そこで生成側を縛るのがここ。
      いきなり hard にすると新規記事の発火率が未知のまま Jules PR を落としうる
      (v7.2 後の実測は 10/10 で言及ゼロだが n=10 では 95% 上限が約 30%)。
      まず soft で 1 週間観測し、census の発火が 0 に近ければ hard へ昇格する。
    """
    if not _how_to_choose_enforced(data):
        return CheckResult("how_to_choose", True, 1.0, "pre-v7 slug, skipped (施行日前)")

    narrative = data.get("narrative", {})
    value = narrative.get("how_to_choose") if isinstance(narrative, dict) else None
    if not value:
        return CheckResult("how_to_choose", False, 0.0, "narrative.how_to_choose missing/empty")
    if not isinstance(value, (str, list)):
        return CheckResult("how_to_choose", False, 0.0, "narrative.how_to_choose must be string or array of string")

    chars = _count_chars(value)
    if chars < HOW_TO_CHOOSE_MIN_CHARS:
        return CheckResult(
            "how_to_choose", False, chars / HOW_TO_CHOOSE_MIN_CHARS,
            f"how_to_choose {chars}<{HOW_TO_CHOOSE_MIN_CHARS} chars",
        )

    blob = value if isinstance(value, str) else "\n".join(s for s in value if isinstance(s, str))
    mentioned_all = set(_ASIN_MENTION_RE.findall(blob))

    product = data.get("product") or {}
    own_asin = product.get("asin") if isinstance(product, dict) else None
    # hard 判定 (捏造検査) は従来どおり自商品を除外した集合で行う。
    # soft 判定 (表記規律) は自商品も含めた mentioned_all を使う。
    mentioned = mentioned_all - ({own_asin} if own_asin else set())

    if not mentioned:
        return _inline_asin_soft(mentioned_all, data=data)

    asin = own_asin or ""
    comp_path = pathlib.Path("data/raw/per_asin") / asin / "competitors.json"
    try:
        comp_data = json.loads(comp_path.read_text(encoding="utf-8"))
        allowed = {
            c.get("asin") for c in (comp_data.get("competitors") or [])
            if isinstance(c, dict) and c.get("asin")
        }
    except Exception:
        # competitors.json が読めない環境では ASIN 言及ゼロのみ合格 (安全側)
        return CheckResult(
            "how_to_choose", False, 0.0,
            f"competitors.json unreadable ({comp_path}) but how_to_choose mentions ASIN(s): {sorted(mentioned)}",
        )

    hallucinated = mentioned - allowed
    if hallucinated:
        return CheckResult(
            "how_to_choose", False, 0.0,
            f"how_to_choose mentions ASIN(s) not in competitors.json: {sorted(hallucinated)}",
        )
    return _inline_asin_soft(mentioned_all, data=data)


def check_faq(data: dict, product_name: str) -> CheckResult:
    faq = data.get("faq", [])
    if not isinstance(faq, list):
        return CheckResult("faq", False, 0.0, "faq not a list")
    count_ok = len(faq) >= 3
    name_in_q = sum(1 for f in faq if product_name and product_name in f.get("question", ""))
    answers_ok = all(_count_chars(f.get("answer", "")) >= 30 for f in faq)
    score = (1.0 if count_ok else len(faq) / 3.0) * 0.4 + (min(name_in_q / 2, 1.0)) * 0.3 + (1.0 if answers_ok else 0.5) * 0.3
    msg = []
    if not count_ok:
        msg.append(f"only {len(faq)} FAQ items (need >=3)")
    if name_in_q < 2:
        msg.append(f"only {name_in_q} questions contain product name (recommend >=2)")
    if not answers_ok:
        msg.append("some answers too short (<30 chars)")
    return CheckResult("faq", score >= 0.7, score, "; ".join(msg) or "OK")


def check_score_rationale(data: dict) -> CheckResult:
    # v5: score_rationale は ivs_detail 内の任意フィールド (schema の
    # ivs_detail.required にも含まれない)。完全除外された記事は skip (1.0)。
    # target_age / certifications と同じ「field 欠如 → skip」慣用句に揃える。
    # 存在する場合のみ ≥3 well-formed を要求する (#1599)。
    ivs_detail = data.get("product", {}).get("ivs_detail")
    if not isinstance(ivs_detail, dict) or "score_rationale" not in ivs_detail:
        return CheckResult("score_rationale", True, 1.0, "field absent (v5 optional, skipped)")
    rationale = ivs_detail.get("score_rationale")
    if not isinstance(rationale, list):
        return CheckResult("score_rationale", False, 0.0, "score_rationale not a list")
    count = len(rationale)
    well_formed = sum(
        1 for r in rationale
        if isinstance(r, dict) and r.get("factor") and r.get("delta") and len(r.get("reason", "")) >= 10
    )
    score = min(well_formed / 3.0, 1.0)
    msg = []
    if count < 3:
        msg.append(f"only {count} rationale entries (need >=3)")
    if well_formed < count:
        msg.append(f"{count - well_formed} entries malformed")
    return CheckResult("score_rationale", score >= 0.7, score, "; ".join(msg) or "OK")


_V5_VALID_EDU_DOMAINS = {"STEM", "言語", "運動", "想像"}
_V5_TARGET_AGE_RE = re.compile(r"\d+\s*(歳|才|ヶ月)")


def check_target_age(data: dict) -> CheckResult:
    """v5: product.target_age が正規化表記である。フィールド無しは skip (score 1.0)."""
    product = data.get("product") or {}
    if "target_age" not in product:
        return CheckResult("target_age_v5", True, 1.0, "field absent (legacy article, skipped)")
    raw = product.get("target_age")
    if not isinstance(raw, str) or not raw.strip():
        return CheckResult("target_age_v5", False, 0.0, "target_age must be non-empty string")
    if not _V5_TARGET_AGE_RE.search(raw):
        return CheckResult("target_age_v5", False, 0.0, f"target_age '{raw[:30]}' lacks digits+歳/才/ヶ月")
    return CheckResult("target_age_v5", True, 1.0, "OK")


_SALES_PAGE_HOSTS = (
    "amazon.co.jp", "amazon.com",
    "rakuten.co.jp", "hb.afl.rakuten.co.jp", "search.rakuten.co.jp",
    "shopping.yahoo.co.jp", "ck.jp.ap.valuecommerce.com", "store.shopping.yahoo.co.jp",
)


def _is_sales_source(src: dict) -> bool:
    url = (src.get("url") or "").lower()
    return any(h in url for h in _SALES_PAGE_HOSTS)


# 検索エンジン結果ページ自体は独立した第三者ソースになり得ない (PR #325 後継)
# Jules が「検索した URL をそのまま src に貼る」手抜きを禁止する
_SEARCH_ENGINE_URL_PATTERNS = (
    "html.duckduckgo.com",
    "duckduckgo.com/?q=", "duckduckgo.com/html",
    "www.google.com/search", "google.com/search?",
    "www.bing.com/search", "bing.com/search?",
    "search.yahoo.co.jp", "search.yahoo.com",
    "search.brave.com",
    "www.baidu.com/s?", "baidu.com/s?",
)


def _is_search_engine_url(url: str) -> bool:
    u = (url or "").lower()
    return any(p in u for p in _SEARCH_ENGINE_URL_PATTERNS)


def _normalize_url(url: str) -> str:
    """URL 正規化: 同一 URL を src 間で重複検出するための比較キー。
    末尾スラッシュ・大小文字・affiliate tag を吸収するが、path や query 構造は保持。
    """
    if not url:
        return ""
    u = url.strip().lower()
    # affiliate tag を剥がす (Amazon の ?tag= や &tag= が代表)
    u = re.sub(r"[?&]tag=[^&]*", "", u)
    u = re.sub(r"[?&]utm_[a-z_]+=[^&]*", "", u)
    # 残った "?" や "&" の連結を整える
    u = re.sub(r"\?&", "?", u)
    u = re.sub(r"&+", "&", u)
    u = u.rstrip("?&/")
    return u


def check_certifications(data: dict) -> CheckResult:
    """v5: product.certifications が list。空配列も可。フィールド無しは skip.

    追加 (P2): certifications が非空のとき、その cert 名を本文で主張している
    claim を探し、その claim の supporting_source_ids が空または全販売ページ
    だけだった場合は failure とする (§6.5.6 ハルシネーション防止)。
    """
    product = data.get("product") or {}
    if "certifications" not in product:
        return CheckResult("certifications_v5", True, 1.0, "field absent (legacy article, skipped)")
    val = product.get("certifications")
    if not isinstance(val, list):
        return CheckResult("certifications_v5", False, 0.0, "certifications must be a list")
    non_str = [c for c in val if not isinstance(c, str)]
    if non_str:
        return CheckResult("certifications_v5", False, 0.0, f"non-string entries: {non_str[:3]}")
    certs = [c for c in val if c]
    if not certs:
        return CheckResult("certifications_v5", True, 1.0, "OK (empty)")

    claims = data.get("claims") or []
    sources_by_id = {s.get("id"): s for s in (data.get("sources") or []) if isinstance(s, dict) and s.get("id")}

    # cert 名を含む claim を抽出
    mentioning = [
        c for c in claims
        if isinstance(c, dict) and isinstance(c.get("claim"), str)
        and any(cert in c["claim"] for cert in certs)
    ]
    if not mentioning:
        return CheckResult(
            "certifications_v5", False, 0.0,
            f"certifications {certs} 主張だが本文 claim 内に cert 名を含む裏付け記述が無い",
        )

    bad = []
    for cl in mentioning:
        sids = cl.get("supporting_source_ids") or []
        srcs = [sources_by_id.get(sid) for sid in sids if sources_by_id.get(sid)]
        if not srcs:
            bad.append((cl.get("claim", "")[:60], "sources=空"))
            continue
        if all(_is_sales_source(s) for s in srcs):
            bad.append((cl.get("claim", "")[:60], f"全販売ページ ({len(srcs)}件)"))
    if bad:
        first = bad[0]
        return CheckResult(
            "certifications_v5", False, 0.0,
            f"certifications {certs} の裏付け不足: claim='{first[0]}' → {first[1]}",
        )
    return CheckResult("certifications_v5", True, 1.0, "OK")


def _undeclared_cert_claims(data: dict) -> dict:
    """claims で主張されているのに product.certifications に無い cert を返す。

    戻り値は {cert 名: 最初に見つかった claim テキスト}。判定語彙は
    _CERT_HTML_TOKENS を再利用する (check_cert_sources_content が HTML 裏取りに
    使うものと同じにして、主張の検出と検証の語彙がずれないようにする)。
    """
    declared = {
        c for c in ((data.get("product") or {}).get("certifications") or [])
        if isinstance(c, str) and c
    }
    found: dict = {}
    for cl in data.get("claims") or []:
        if not isinstance(cl, dict):
            continue
        text = cl.get("claim")
        if not isinstance(text, str) or not text:
            continue
        if any(neg in text for neg in _CERT_CLAIM_NEGATIONS):
            continue  # 「STマークの記載はありません」等は主張ではない
        for cert, tokens in _CERT_HTML_TOKENS.items():
            if cert in declared or cert in found:
                continue
            # 別の cert 名そのものを alias に持つ組があるので (CE の alias に "EN71")、
            # 検出では自分以外の cert 名を alias として使わない。EN71 は EN71 として
            # 数える。裏取り側 (check_cert_sources_content) は「CE の根拠として EN71
            # の記述を認める」ために持っている対応なので、あちらは変えない。
            probes = [t for t in tokens if t == cert or t not in _CERT_HTML_TOKENS]
            if any(tok in text for tok in probes):
                found[cert] = text
    return found


def check_cert_claims_declared(data: dict) -> CheckResult:
    """claims の認証主張が product.certifications に申告されているか (soft)。

    check_certifications は certifications が空なら "OK (empty)" で抜けるので、
    **申告しなければ裏取りを免れる**という抜け道が残っていた (#5490 信頼レーン)。
    ここは「主張したなら申告しろ」だけを見る。申告されれば check_certifications と
    check_cert_sources_content が従来どおり裏取りする。

    まだ soft (passed=True, score<1.0)。昇格の判断材料は quality_census が拾う。
    """
    if "claims" not in data or _is_legacy_article(data):
        return CheckResult("cert_claims_declared", True, 1.0,
                           "field absent or legacy article (skipped)")
    if not isinstance(data.get("claims"), list):
        return CheckResult("cert_claims_declared", True, 1.0, "claims is not a list (skipped)")
    undeclared = _undeclared_cert_claims(data)
    if not undeclared:
        return CheckResult("cert_claims_declared", True, 1.0, "OK")
    certs = sorted(undeclared)
    # quality_census.normalize_reason は ";" より前を集計キーにする。記事ごとに違う
    # claim 本文を頭に入れると理由が 44 通りに散るので、可変部は ";" の後ろに置く。
    return CheckResult(
        "cert_claims_declared", True, CERT_CLAIM_UNDECLARED_SOFT_SCORE,
        f"claim が {certs} を主張しているが product.certifications 未申告 "
        f"→ 裏取りが走らない"
        f"; claim='{undeclared[certs[0]][:60]}' "
        "(soft: 主張するなら certifications に載せ、非販売ソースで裏を取ること)",
    )


def check_source_uniqueness(data: dict) -> CheckResult:
    """sources 構造健全性チェック (v5 §6.5.1 補強):
    1. 検索エンジン結果ページ URL (DuckDuckGo/Google 等) は src に不可
    2. 同一 URL (正規化後) が 2 つ以上の src id に分割されていない (1 URL multi-source 化禁止)
    """
    if "sources" not in data or _is_legacy_article(data):
        return CheckResult("source_uniqueness", True, 1.0, "field absent or legacy article (skipped)")
    srcs = data.get("sources") or []
    if not isinstance(srcs, list) or not srcs:
        return CheckResult("source_uniqueness", True, 1.0, "OK (empty)")

    # 1. 検索エンジン URL
    bad_search = []
    for s in srcs:
        if not isinstance(s, dict):
            continue
        url = s.get("url") or ""
        if _is_search_engine_url(url):
            bad_search.append((s.get("id", "?"), url[:100]))
    if bad_search:
        sid, u = bad_search[0]
        return CheckResult(
            "source_uniqueness", False, 0.0,
            f"検索エンジン結果ページが src に含まれる: {sid}={u}",
        )

    # 2. URL 重複 (正規化後)
    seen: dict[str, list[str]] = {}
    for s in srcs:
        if not isinstance(s, dict):
            continue
        norm = _normalize_url(s.get("url") or "")
        if not norm:
            continue
        seen.setdefault(norm, []).append(s.get("id") or "?")
    dups = {u: ids for u, ids in seen.items() if len(ids) >= 2}
    if dups:
        u0 = next(iter(dups))
        return CheckResult(
            "source_uniqueness", False, 0.0,
            f"同一 URL が複数 src に分割: {dups[u0]} → {u0[:100]}",
        )

    return CheckResult("source_uniqueness", True, 1.0, "OK")


def check_sources_v5(data: dict) -> CheckResult:
    """v5 §6.5.1 件数規律: sources は最低 5 件、うち非販売 (第三者) が 2 件以上。

    セッション 19 で「certs=[] にすれば cert 系 check が全 skip → sources を
    緩めても通過する」盲点が判明 (例: B0GFVV4YG9 は販売 5 件のみ / B0C8HM1F94
    は sources=3)。プロンプトには明記されているが gate 未強制だったため追加。

    判定:
    - sources field 無し → skip (legacy article)
    - sources が list でない → fail
    - len(sources) < 5 → fail
    - 非販売 (= _is_sales_source False) が 2 件未満 → fail
    """
    if "sources" not in data or _is_legacy_article(data):
        return CheckResult("sources_v5", True, 1.0, "field absent or legacy article (skipped)")
    srcs = data.get("sources") or []
    if not isinstance(srcs, list):
        return CheckResult("sources_v5", False, 0.0, "sources must be a list")

    valid = [s for s in srcs if isinstance(s, dict) and s.get("url")]
    total = len(valid)
    if total < 5:
        return CheckResult(
            "sources_v5", False, 0.0,
            f"sources={total} 件 (v5 §6.5.1 最低 5 件必須)",
        )

    non_sales = [s for s in valid if not _is_sales_source(s)]
    if len(non_sales) < 2:
        sales_count = total - len(non_sales)
        return CheckResult(
            "sources_v5", False, 0.0,
            f"非販売 source={len(non_sales)} 件 / 販売={sales_count} 件 "
            f"(v5 §6.5.1 第三者 2 件以上必須)",
        )
    return CheckResult("sources_v5", True, 1.0, f"OK (total={total}, non_sales={len(non_sales)})")


# #5490 信頼レーン: claims で認証を主張しながら product.certifications に載せない
# 記事の soft スコア。certifications が空だと check_certifications は "OK (empty)" で
# 素通りするため、「STマーク取得済で安全」のような claim が一度も裏取りされない。
#
# 実測 (2026-08-19, 記事 2,064 件): 41 件 (2.0%) が該当。
#   例) B0DC6GCTTN「舐めても安心な安全設計とPSC基準適合」  certifications=None
#       B0875FV2BQ「食品衛生法等の…検査基準に準拠した塗料を使用している」 certifications=[]
# 無名ブランド品にも出ており、子ども向け玩具の安全性主張が裏取りされないまま
# 配信されるのは E-E-A-T 上いちばん出してはいけない類のもの。
#
# soft で入れる理由: 発火率 2.0% は #4826 項目2 の前例 (soft 導入 → 発火 0 を確認 →
# hard 昇格) でいう「まだ 0 ではない」段階。いきなり hard にすると既存記事に触る
# PR が落ちる。quality_census が「減点のみ」を週次で拾うので、プロンプト側で
# certifications を書かせる改訂が入ったあとに発火率を見て昇格を判断する。
CERT_CLAIM_UNDECLARED_SOFT_SCORE = 0.8

# 「STマークの記載はありません」のような否定文を主張と誤認しないためのガード。
# claim 1 件のテキスト全体に対して見る (claims は 1 文が基本のため)。
_CERT_CLAIM_NEGATIONS = (
    "ありません", "ありませんが", "ございません", "記載はな", "記載がな",
    "取得していな", "未取得", "確認できま", "不明", "対象外", "非対象",
)


# cert 名と HTML 本文内で許容するトークン (alias) の対応
# 個別 cert 主張時に、第三者 source HTML がこれらのいずれかを含めば OK
_CERT_HTML_TOKENS: dict[str, tuple[str, ...]] = {
    "食品衛生法": ("食品衛生法", "食品衛生", "食品級", "食用級"),
    "EN71": ("EN71", "EN 71", "EN-71"),
    "ASTM": ("ASTM",),
    "CE": ("CE承認", "CE認証", "CEマーク", "CE基準", "CE適合", "CE標準", "EN71"),
    "ST": ("STマーク", "ST基準", "ST規格", "ST認証", "日本玩具協会", "玩具安全基準"),
    "PSC": ("PSCマーク", "PSC認証", "PSC基準", "PSC適合", "消費生活用製品安全法"),
    "KC": ("KCマーク", "KC認証", "KC基準"),
}


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_SCRIPT_RE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)


def _fetch_url_text(url: str, *, cache: dict[str, str | None], timeout: float = 8.0) -> str | None:
    """source URL を fetch して本文テキストを返す。失敗時 None。簡易 cache 付き。
    bs4 を避け、regex で <script>/<style>/<noscript> と HTML タグを剥がす軽量版。
    cert トークンは plain string なので tag stripping が雑でも捕捉できる。
    """
    if url in cache:
        return cache[url]
    try:
        import requests  # type: ignore
    except ImportError:
        cache[url] = None
        return None
    try:
        r = requests.get(
            url,
            timeout=timeout,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (compatible; omochairo-quality-gate/1.0; "
                    "+https://omochairo.github.io/amazon/)"
                ),
                "Accept-Language": "ja,en;q=0.5",
            },
            allow_redirects=True,
        )
        if r.status_code >= 400:
            cache[url] = None
            return None
        ct = r.headers.get("Content-Type", "").lower()
        if "html" not in ct and "text" not in ct:
            cache[url] = None
            return None
        raw = r.text
        raw = _HTML_SCRIPT_RE.sub(" ", raw)
        text = _HTML_TAG_RE.sub(" ", raw)
        cache[url] = text
        return text
    except Exception:
        cache[url] = None
        return None


def check_cert_sources_content(data: dict, *, fetch_enabled: bool = True) -> CheckResult:
    """v5 §6.5.6 補強 (P0.5): cert claim の supporting 第三者 source HTML を
    fetch して cert 名 (or alias) トークンが本文に出現するか検証する。

    PR #313 ゲートは「non-sales URL を 1 件 supporting に挙げる」だけで通過する
    構造だったため、B07GCYY4M6 の kanamiblog のように cert を一切言及しない
    blog を Jules が「形だけ」埋めて gate 突破する事例が確認された (セッション18)。

    判定:
    - certifications 空 → skip
    - fetch_enabled=False → skip (local dryrun 用)
    - cert ごとに claim を抽出 → 非販売 supporting source HTML を順次 fetch
    - 1 source でも cert token を含めば、その cert は OK
    - 全 fetch 成功かつ cert token 0 件 → fail
    - 全 fetch 失敗 (network/4xx/5xx) → warn (score 0.7, passed=True) — flake 耐性
    """
    product = data.get("product") or {}
    if "certifications" not in product:
        return CheckResult("cert_sources_content", True, 1.0, "field absent (skipped)")
    certs = [c for c in (product.get("certifications") or []) if isinstance(c, str) and c]
    if not certs:
        return CheckResult("cert_sources_content", True, 1.0, "no certifications (skipped)")
    if not fetch_enabled:
        return CheckResult("cert_sources_content", True, 1.0, "network disabled (skipped)")

    claims = data.get("claims") or []
    sources_by_id = {
        s.get("id"): s for s in (data.get("sources") or [])
        if isinstance(s, dict) and s.get("id")
    }
    cache: dict[str, str | None] = {}

    indeterminate_certs: list[str] = []
    failed_certs: list[tuple[str, str]] = []  # (cert, reason)

    for cert in certs:
        tokens = _CERT_HTML_TOKENS.get(cert, (cert,))
        mentioning = [
            c for c in claims
            if isinstance(c, dict) and isinstance(c.get("claim"), str) and cert in c["claim"]
        ]
        if not mentioning:
            # cert listed but no claim mentions it — caught by check_certifications already
            continue

        # この cert を主張する全 claim の非販売 supporting を集約
        non_sales_srcs: list[dict] = []
        for cl in mentioning:
            for sid in cl.get("supporting_source_ids") or []:
                s = sources_by_id.get(sid)
                if s and not _is_sales_source(s) and s.get("url"):
                    non_sales_srcs.append(s)
        if not non_sales_srcs:
            # 全販売 supporting — check_certifications 側で既に fail されるはず
            continue

        # 重複 URL は 1 回だけ fetch
        seen_urls = []
        for s in non_sales_srcs:
            u = s.get("url") or ""
            if u and u not in seen_urls:
                seen_urls.append(u)

        any_fetch_success = False
        token_found = False
        for url in seen_urls:
            text = _fetch_url_text(url, cache=cache)
            if text is None:
                continue
            any_fetch_success = True
            if any(tok in text for tok in tokens):
                token_found = True
                break

        if token_found:
            continue
        if not any_fetch_success:
            indeterminate_certs.append(cert)
            continue
        failed_certs.append((cert, f"非販売 source {len(seen_urls)} 件 fetch 成功 / token {tokens} 言及無し"))

    if failed_certs:
        cert, reason = failed_certs[0]
        return CheckResult(
            "cert_sources_content", False, 0.0,
            f"certifications '{cert}' の第三者 source 本文に cert 言及無し: {reason}",
        )
    if indeterminate_certs:
        return CheckResult(
            "cert_sources_content", True, 0.7,
            f"network 失敗で未検証: {indeterminate_certs}",
        )
    return CheckResult("cert_sources_content", True, 1.0, "OK")


def check_edu_domains(data: dict) -> CheckResult:
    """v5: product.edu_domains が {STEM,言語,運動,想像} の部分集合。空配列も可。フィールド無しは skip."""
    product = data.get("product") or {}
    if "edu_domains" not in product:
        return CheckResult("edu_domains_v5", True, 1.0, "field absent (legacy article, skipped)")
    val = product.get("edu_domains")
    if not isinstance(val, list):
        return CheckResult("edu_domains_v5", False, 0.0, "edu_domains must be a list")
    invalid = [d for d in val if d not in _V5_VALID_EDU_DOMAINS]
    if invalid:
        return CheckResult(
            "edu_domains_v5", False, 0.0,
            f"invalid domains: {invalid[:3]} (allowed: STEM, 言語, 運動, 想像)"
        )
    return CheckResult("edu_domains_v5", True, 1.0, "OK")


def check_tag_granularity(
    data: dict, tag_corpus: "TagCorpus | None" = None
) -> CheckResult:
    """#5088 Warn-only: タグが「複数記事を束ねる軸」になっているかを見る。

    タグは 3,295 種のうち 77% が 1 記事にしか付いておらず、その 84% は
    product.name の部分文字列だった (実測)。原因は文字列処理のバグではなく、
    生成側が商品固有の固有名詞をタグに書いていること。ページ化されない
    (= 404 も出ない) ので実害はまだ無く、**合否は変えない**。census で
    発火率を追い、AGENTS.md の粒度規約が効いているかを観測するための check。

    tag_corpus が None (corpus 不明) のときは束ね能力の判定だけを skip し、
    corpus 非依存の 2 規則は評価する。
    """
    tags = data.get("tags")
    if not isinstance(tags, list) or not tags:
        return CheckResult("tag_granularity", True, 1.0, "no tags")
    tags = [str(t) for t in tags if str(t).strip()]
    if not tags:
        return CheckResult("tag_granularity", True, 1.0, "no tags")

    product = data.get("product") or {}
    product_names = {
        _normalize_tag(product.get("name")),
        _normalize_tag(product.get("name_full")),
    } - {""}

    slug = data.get("slug")
    slug = str(slug) if slug else None

    def _is_novel(tag: str) -> bool:
        """自分以外にこのタグを持つ記事が 1 本も無いか。"""
        if tag_corpus is None:
            return False
        return tag_corpus.others(tag, slug) == 0

    # 規則1: 商品名そのもの。定義上その 1 記事しか持てない。
    product_name_tags = [t for t in tags if _normalize_tag(t) in product_names]
    # 規則2: 同一記事内の別タグの部分文字列で、かつどの記事とも共有していない。
    #        「イト」⊂「アークライト」型の断片を拾う。
    fragment_tags = [
        t for t in tags
        if _is_novel(t) and any(t != other and t in other for other in tags)
    ]
    novel_ratio = None
    if tag_corpus is not None:
        novel_ratio = sum(1 for t in tags if _is_novel(t)) / len(tags)

    notes: list[str] = []
    score = 1.0
    if product_name_tags:
        score -= 0.1 * len(product_name_tags)
        notes.append(f"product-name tags={product_name_tags[:3]}")
    if fragment_tags:
        score -= 0.1 * len(fragment_tags)
        notes.append(f"fragment suspects={fragment_tags[:3]}")
    if novel_ratio is not None and novel_ratio >= TAG_NOVEL_RATIO_WARN:
        score -= 0.2
        notes.append(
            f"unshared tags {novel_ratio:.0%} >= {TAG_NOVEL_RATIO_WARN:.0%} "
            f"(median 20%)"
        )
    if not notes:
        detail = "corpus unavailable" if tag_corpus is None else f"unshared {novel_ratio:.0%}"
        return CheckResult("tag_granularity", True, 1.0, f"OK ({detail})")
    return CheckResult(
        "tag_granularity", True, max(0.5, score),
        "; ".join(notes) + " (warn-only, #5088)",
    )


def check_prices_verified(data: dict) -> CheckResult:
    """Warn-only: count rakuten/yahoo price entries that have a clickable URL
    but lack deterministic cross_search verification. Build_post tags entries
    with ``verified=True`` only when ``data/raw/{rakuten,yahoo}_matched.json``
    confirms an ASIN-level match. Jules-supplied URLs without that match are
    legitimate (often correct) but carry a higher chance of pointing to a
    look-alike product, especially for no-brand listings. We surface the count
    in the report (and modestly nudge the score) but never strict-fail —
    blocking every Jules PR with an unverified Yahoo link would be too harsh.
    """
    product = data.get("product") or {}
    prices = product.get("prices") or {}
    unverified: list[str] = []
    verified: list[str] = []
    for key in ("rakuten", "yahoo"):
        entry = prices.get(key)
        if not isinstance(entry, dict):
            continue
        try:
            p = int(entry.get("price") or 0)
        except (TypeError, ValueError):
            p = 0
        if p <= 0 or not entry.get("url"):
            continue  # no clickable link rendered to readers
        if entry.get("verified") is True:
            verified.append(key)
        else:
            unverified.append(key)
    total = len(verified) + len(unverified)
    if total == 0:
        return CheckResult("prices_verified", True, 1.0, "no rakuten/yahoo links present")
    if not unverified:
        return CheckResult("prices_verified", True, 1.0, f"all {total} link(s) verified via cross_search")
    score = max(0.5, 1.0 - 0.1 * len(unverified))
    msg = (
        f"unverified={','.join(unverified)} verified={','.join(verified) or 'none'} "
        f"(warn-only; template renders ※確度低 badge + search fallback)"
    )
    return CheckResult("prices_verified", True, score, msg)


def check_no_reseller_pricing(data: dict) -> CheckResult:
    """Detect ASINs whose Amazon Buy Box price is dramatically higher than
    the same product on Rakuten / Yahoo — a strong indicator that the ASIN
    is a third-party reseller scam (e.g. B0G398BYV6 Switch Mario Wonder
    listed at ¥8480 when official ASIN B0C8Y9THVS is ¥5980).

    Hard fail when amazon_price > 1.3x min(rakuten, yahoo). 1.3x balances
    legitimate Amazon markup (1.0-1.15x typical) against clear reseller
    premiums (1.3x+). Only fires when at least one of rakuten/yahoo has a
    real price so the gate doesn't false-trip on niche products.

    Skipped entirely when amazon_price <= 5000 JPY (#2416): Yahoo/Rakuten
    listings sometimes display prices excluding shipping, and for cheap
    items the shipping cost is a large fraction of the total, so the ratio
    spikes even for legitimate, non-reseller pricing. Manual review of 5
    quarantined ASINs confirmed the 3 under ¥5000 were false positives
    while the 1 over ¥5000 (¥24699) was a confirmed reseller.
    """
    product = data.get("product") or {}
    prices = product.get("prices") or {}
    amazon = prices.get("amazon") or {}
    try:
        a_price = int(amazon.get("price") or 0)
    except (TypeError, ValueError):
        a_price = 0
    if a_price <= 0:
        return CheckResult("no_reseller_pricing", True, 1.0, "no amazon price")
    if a_price <= 5000:
        return CheckResult("no_reseller_pricing", True, 1.0, "amazon price <= 5000 JPY, skipped (shipping-inclusive ratio noise)")
    cross_prices: list[tuple[str, int]] = []
    for src in ("rakuten", "yahoo"):
        entry = prices.get(src) or {}
        try:
            p = int(entry.get("price") or 0)
        except (TypeError, ValueError):
            p = 0
        if p > 0:
            cross_prices.append((src, p))
    if not cross_prices:
        return CheckResult("no_reseller_pricing", True, 1.0, "no rakuten/yahoo price to compare")
    best_src, best_p = min(cross_prices, key=lambda x: x[1])
    ratio = a_price / best_p
    if ratio > 1.30:
        asin = product.get("asin", "?")
        msg = (
            f"reseller-pricing suspect: Amazon ¥{a_price} vs {best_src} ¥{best_p} "
            f"(ratio {ratio:.2f}x > 1.30). ASIN {asin} may be a third-party "
            f"reseller scam. Investigate canonical ASIN and add to "
            f"data/asin_blocklist.json if confirmed."
        )
        return CheckResult("no_reseller_pricing", False, 0.0, msg)
    return CheckResult("no_reseller_pricing", True, 1.0, f"OK (ratio {ratio:.2f}x vs {best_src})")


def check_lead_hook(data: dict) -> CheckResult:
    """narrative.lead に §1.B 禁止フレーズが残っていないかを検出する (#506)。

    meta_description のメタ説明調 (「本記事では…3サイト横断…丁寧に比較しました」)
    が lead に流れ込む事故を catch する。legacy 記事は skip。
    """
    if _is_legacy_article(data):
        return CheckResult("lead_hook", True, 1.0, "legacy article (skipped)")
    narrative = data.get("narrative", {})
    lead = narrative.get("lead", "")
    if not isinstance(lead, str) or not lead:
        return CheckResult("lead_hook", True, 1.0, "lead empty (other check handles)")
    hits = []
    for pat in LEAD_FORBIDDEN_PATTERNS:
        m = re.search(pat, lead)
        if m:
            hits.append(m.group(0))
    if not hits:
        return CheckResult("lead_hook", True, 1.0, "OK")
    msg = ", ".join(f"'{h}'" for h in hits)
    return CheckResult(
        "lead_hook", False, 0.0,
        f"forbidden meta-narration in narrative.lead: {msg} "
        f"(see PROMPT_TEMPLATE.md §1.B)"
    )


def check_tone(data: dict) -> CheckResult:
    """Scan all narrative + faq text for forbidden childish tone patterns."""
    texts = []
    narrative = data.get("narrative", {})
    texts.extend(narrative.values())
    for f in data.get("faq", []):
        texts.append(f.get("question", ""))
        texts.append(f.get("answer", ""))
    texts.append(data.get("editorial_comment", ""))
    blob = "\n".join(t for t in texts if isinstance(t, str))

    hits = []
    for pat in FORBIDDEN_TONE_PATTERNS:
        for m in re.finditer(pat, blob):
            hits.append((pat, m.group(0)))
            if len(hits) >= 5:
                break
        if len(hits) >= 5:
            break

    if not hits:
        return CheckResult("tone", True, 1.0, "OK")
    msg = ", ".join(f"'{h[1].strip()}'" for h in hits)
    return CheckResult("tone", False, 0.0, f"childish tone detected: {msg}")


def check_heading_hierarchy(md_text: str | None) -> CheckResult:
    """Ensure h1 -> h2 -> h3 ordering in rendered markdown (skip if md not provided)."""
    if not md_text:
        return CheckResult("heading_hierarchy", True, 1.0, "no markdown to check (skipped)")

    headings = re.findall(r"^(#{1,6})\s+(.+)$", md_text, re.MULTILINE)
    if not headings:
        return CheckResult("heading_hierarchy", False, 0.0, "no headings found")

    prev_level = 0
    violations = []
    h1_count = 0
    for marks, text in headings:
        level = len(marks)
        if level == 1:
            h1_count += 1
        if prev_level and level > prev_level + 1:
            violations.append(f"jump from h{prev_level} to h{level} at '{text[:30]}'")
        prev_level = level
    score = 1.0
    msg = []
    if h1_count != 1:
        score -= 0.3
        msg.append(f"h1 count={h1_count} (expected 1)")
    if violations:
        score -= 0.1 * len(violations)
        msg.append(violations[0])
    score = max(0.0, score)
    return CheckResult("heading_hierarchy", score >= 0.7, score, "; ".join(msg) or "OK")


def check_body_word_count(md_text: str | None) -> CheckResult:
    if not md_text:
        return CheckResult("body_word_count", True, 1.0, "no markdown (skipped)")
    plain = re.sub(r"```.*?```", "", md_text, flags=re.DOTALL)
    plain = re.sub(r"<[^>]+>", "", plain)
    plain = re.sub(r"[\#\*\-\|\>\[\]\(\)`]", "", plain)
    chars = len(re.sub(r"\s+", "", plain))
    score = min(chars / 2000.0, 1.0)
    msg = f"{chars} chars (target>=2000)"
    return CheckResult("body_word_count", chars >= 1600, score, msg)


# ---------------------------------------------------------------------------
# #2686 / #4964: 「どこで買える/在庫」記事型の検証ゲート
#
# where_to_buy_format.py (build_post.py が使う決定的レンダリング層) の出力を
# 検証する。タイトルで「在庫を毎日チェック」と約束しても本文に答えが無い、
# という事故が B0H4PQ29JS で実際に発生し、Google のインデックスから記事が
# 消えた実績があるため、build 後の rendered markdown をここで機械的に検査する。
# ---------------------------------------------------------------------------

_STOCK_TITLE_KEYWORDS = ("どこで買える", "在庫", "取扱店")

# 「YYYY-MM-DD 時点」形式の取得日時。where_to_buy_format.build_conclusion が
# 必ずこの形式で日付を書く前提 (単独の在庫断定を防ぐ安全装置)。
_STOCK_DATE_STAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}\s*時点")

# 在庫状況に触れている語 (取得日時の近傍にこれが無いと「日付はあるが在庫の
# 話をしていない」ケースを見逃すため、日付とは別に存在確認する)。
_STOCK_MENTION_RE = re.compile(r"(在庫あり|在庫切れ|残り\d+点|取扱|発送)")

# 実店舗の在庫を断定する表現。チェーン名 + 断定語がセットで出たら fail する。
_PHYSICAL_STORE_NAMES = (
    "トイザらス", "トイザらす", "イオン", "西松屋", "ヨドバシ", "ビックカメラ",
    "ヤマダ電機", "イトーヨーカドー", "babiesＲus", "babiesRus", "ベビーザらス",
)
_PHYSICAL_STORE_CLAIM_RE = re.compile(
    r"(" + "|".join(re.escape(n) for n in _PHYSICAL_STORE_NAMES) + r")"
    r"[^\n。]{0,8}(で買えます|に在庫あり|で購入できます|で買える|在庫があります|で取り扱っています|に在庫があります)"
)

_STOCK_ASIN_FROM_URL_RE = re.compile(r"/products/([A-Za-z0-9]{10})/")


def check_stock_title_has_dated_conclusion(title: str, body_md: str) -> list[str]:
    """タイトルが在庫系キーワードを含むのに、本文に取得日時付きの在庫記述が
    無ければ違反を返す。"""
    violations: list[str] = []
    if not any(k in (title or "") for k in _STOCK_TITLE_KEYWORDS):
        return violations

    body = body_md or ""
    if not _STOCK_DATE_STAMP_RE.search(body):
        violations.append(
            "stock-title-missing-dated-conclusion: タイトルが在庫系キーワードを含むが、"
            "本文に「YYYY-MM-DD 時点」形式の取得日時付き記述が見つからない"
        )
        return violations
    if not _STOCK_MENTION_RE.search(body):
        violations.append(
            "stock-title-missing-stock-mention: タイトルが在庫系キーワードを含むが、"
            "本文に在庫状況の記述が見つからない"
        )
    return violations


def check_no_physical_store_claims(body_md: str) -> list[str]:
    """実店舗の在庫を断定する表現を検出する。"""
    violations: list[str] = []
    m = _PHYSICAL_STORE_CLAIM_RE.search(body_md or "")
    if m:
        violations.append(
            f"physical-store-claim: 実店舗の在庫を断定する表現を検出しました: {m.group(0)!r}"
        )
    return violations


def check_no_unknown_state_stock_title(title: str, state: str | None) -> list[str]:
    """在庫状態が unknown なのに在庫系タイトルが付いていたら違反にする。"""
    violations: list[str] = []
    if state == stock_status.STATE_UNKNOWN and any(
        k in (title or "") for k in _STOCK_TITLE_KEYWORDS
    ):
        violations.append(
            "unknown-state-with-stock-title: 在庫状態が unknown なのに在庫系タイトルが"
            "付与されています"
        )
    return violations


def _resolve_rendered_title_and_body(
    data: dict, md_text: str | None,
) -> tuple[str, str, str | None]:
    """rendered markdown があれば frontmatter からタイトル/asin を取る
    (build_post の stock_title_override は JSON には書き戻らず、frontmatter
    にしか出ないため)。md が無ければ JSON の title に fallback する。"""
    if md_text and frontmatter is not None:
        try:
            post = frontmatter.loads(md_text)
        except Exception:
            post = None
        if post is not None:
            title = post.get("title") or data.get("title", "")
            body = post.content or ""
            url = post.get("url") or ""
            m = _STOCK_ASIN_FROM_URL_RE.search(url)
            asin = m.group(1) if m else None
            return title, body, asin

    title = data.get("title", "")
    body = md_text or ""
    product = data.get("product") or {}
    asin = product.get("asin") if isinstance(product, dict) else None
    return title, body, asin


def check_stock_where_to_buy(
    data: dict,
    md_text: str | None,
    stock_index: "stock_status.StockIndex | None" = None,
) -> CheckResult:
    """#2686 / #4964 の 3 検証をまとめて実行する。

    1. タイトルが在庫系キーワードを含む記事は、本文に取得日時付きの在庫
       記述を持たなければならない。
    2. 実店舗の在庫を断定する表現を検出したら fail する。
    3. stock_index が渡されているとき、``state`` が unknown の記事に新型
       タイトルが付いていたら fail する
       (stock_status.can_use_stock_title のゲート漏れを検出する最終防波堤)。
    """
    title, body, asin = _resolve_rendered_title_and_body(data, md_text)

    violations: list[str] = []
    violations += check_stock_title_has_dated_conclusion(title, body)
    violations += check_no_physical_store_claims(body)

    if stock_index is not None and asin:
        obs = stock_status.resolve_stock(asin, stock_index)
        violations += check_no_unknown_state_stock_title(title, obs.state)

    if violations:
        return CheckResult("stock_where_to_buy", False, 0.0, "; ".join(violations))
    return CheckResult("stock_where_to_buy", True, 1.0, "OK")


def _derive_verified_status(
    data: dict,
    rakuten_idx: dict[str, Any] | None,
    yahoo_idx: dict[str, Any] | None,
) -> None:
    """In-place: mirror ``build_post._attach_market_prices()`` for the
    ``verified`` flag so ``check_prices_verified`` can be called on raw Jules
    JSON before build_post has rewritten it. No-op when matched indexes are
    not available or the build_post helpers are not importable."""
    if _bp_matched_passes_quality is None or rakuten_idx is None or yahoo_idx is None:
        return
    product = data.get("product") if isinstance(data.get("product"), dict) else None
    if not product:
        return
    asin = product.get("asin")
    if not asin:
        return
    prices = product.get("prices") or {}
    try:
        amazon_price = int((prices.get("amazon") or {}).get("price") or 0)
    except (TypeError, ValueError):
        amazon_price = 0
    for key, idx in (("rakuten", rakuten_idx), ("yahoo", yahoo_idx)):
        entry = prices.get(key)
        if not isinstance(entry, dict):
            continue
        matched = (idx or {}).get(asin)
        matched_ok = bool(matched) and _bp_matched_passes_quality(matched, amazon_price)
        if matched_ok:
            entry["verified"] = True
            continue
        if entry.get("price") or entry.get("url"):
            entry["verified"] = False


def evaluate_article(
    json_path: pathlib.Path,
    schema: dict,
    md_path: pathlib.Path | None,
    *,
    rakuten_idx: dict[str, Any] | None = None,
    yahoo_idx: dict[str, Any] | None = None,
    cert_fetch: bool = True,
    stock_index: "stock_status.StockIndex | None" = None,
    tag_corpus: collections.Counter | None = None,
) -> ArticleReport:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    slug = data.get("slug", json_path.stem)
    md_text: str | None = None
    if md_path and md_path.exists():
        md_text = md_path.read_text(encoding="utf-8")

    _derive_verified_status(data, rakuten_idx, yahoo_idx)

    product = data.get("product", {})
    product_name = product.get("name", "")
    brand = product.get("brand", "")

    report = ArticleReport(slug=slug, path=str(json_path))
    report.checks.append(check_schema(data, schema))
    report.checks.append(check_title_seo(data, product_name))
    report.checks.append(check_title_serp_fit(data, product_name))
    report.checks.append(check_meta_description(data, product_name))
    report.checks.append(check_keywords(data, product_name, brand))
    report.checks.append(check_narrative(data, product_name))
    report.checks.append(check_how_to_choose(data))
    report.checks.append(check_faq(data, product_name))
    report.checks.append(check_score_rationale(data))
    report.checks.append(check_target_age(data))
    report.checks.append(check_certifications(data))
    report.checks.append(check_cert_claims_declared(data))
    report.checks.append(check_source_uniqueness(data))
    report.checks.append(check_sources_v5(data))
    report.checks.append(check_cert_sources_content(data, fetch_enabled=cert_fetch))
    report.checks.append(check_edu_domains(data))
    report.checks.append(check_tag_granularity(data, tag_corpus))
    report.checks.append(check_prices_verified(data))
    report.checks.append(check_no_reseller_pricing(data))
    report.checks.append(check_lead_hook(data))
    report.checks.append(check_tone(data))
    report.checks.append(check_heading_hierarchy(md_text))
    report.checks.append(check_body_word_count(md_text))
    report.checks.append(check_stock_where_to_buy(data, md_text, stock_index=stock_index))
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", default="data/articles/")
    parser.add_argument("--posts", default="hugo/content/posts/")
    parser.add_argument("--schema", default="data/schema/article.schema.json")
    parser.add_argument("--min-score", type=int, default=60, help="minimum total score (0-100) to pass")
    parser.add_argument("--strict", action="store_true", help="exit non-zero if any article fails any check")
    # #4826 項目 4: <slug>.quality.json sidecar は廃止した。
    # 旧 --write-reports は action="store_true" かつ default=True で **無効化する手段が
    # 無く**、--src を指した先に必ず 1 記事 1 ファイルの派生物を書いていた
    # (04-validate は mktemp に書いて捨てており commit 経路も無かった)。
    # main 全量の品質は 48-quality-census.yml (集計 JSON 1 本) が観測する。
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--no-cert-fetch", action="store_true",
        help="cert HTML content check の HTTP fetch を無効化 (local dryrun 用)",
    )
    parser.add_argument(
        "--tag-corpus", default="data/articles/",
        help="#5088: タグの束ね能力を測る母集団 (--src が mktemp の PR 検証でも "
             "リポジトリ全記事を見るため独立指定。存在しなければ判定を skip)",
    )
    parser.add_argument(
        "--price-watch", default=None,
        help="#2686: data/price_watch/latest.json への override path (テスト用)",
    )
    args = parser.parse_args()

    src = pathlib.Path(args.src)
    posts = pathlib.Path(args.posts)
    schema_path = pathlib.Path(args.schema)

    if not src.exists():
        print(f"[quality_gate] src not found: {src}")
        return 0
    if not schema_path.exists():
        print(f"[quality_gate] schema not found: {schema_path}")
        return 1

    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    rakuten_idx = None
    yahoo_idx = None
    if _bp_load_matched_index is not None:
        rakuten_matched_path = pathlib.Path("data/raw/rakuten_matched.json")
        yahoo_matched_path = pathlib.Path("data/raw/yahoo_matched.json")
        if rakuten_matched_path.exists():
            rakuten_idx = _bp_load_matched_index(rakuten_matched_path)
        if yahoo_matched_path.exists():
            yahoo_idx = _bp_load_matched_index(yahoo_matched_path)

    # #2686 / #4964: 「どこで買える/在庫」記事型のゲート用 (state=unknown で
    # 新型タイトルが付いている記事を検出する)。latest.json が無い/壊れている
    # 場合 load_stock_index は空 index を返し fail-soft で扱う。
    if args.price_watch:
        stock_index = stock_status.load_stock_index(args.price_watch)
    else:
        stock_index = stock_status.load_stock_index()

    json_files = sorted(p for p in src.glob("*.json") if not p.stem.endswith(".enrichment") and not p.stem.endswith(".seo") and not p.stem.endswith(".quality"))

    if not json_files:
        print("[quality_gate] no articles to check")
        return 0

    tag_corpus = load_tag_corpus(pathlib.Path(args.tag_corpus)) if args.tag_corpus else None
    if tag_corpus is None:
        print(f"[quality_gate] tag corpus not available at {args.tag_corpus}; tag_granularity partially skipped")

    failures: list[ArticleReport] = []
    below_threshold: list[ArticleReport] = []
    all_reports: list[ArticleReport] = []

    for jp in json_files:
        md_candidate = posts / f"{jp.stem}.md"
        report = evaluate_article(
            jp, schema, md_candidate if md_candidate.exists() else None,
            rakuten_idx=rakuten_idx, yahoo_idx=yahoo_idx,
            cert_fetch=not args.no_cert_fetch,
            tag_corpus=tag_corpus,
        )
        all_reports.append(report)
        if not args.quiet:
            status = "OK" if report.passed and report.total_score >= args.min_score else "NG"
            print(f"[{status}] {report.slug} score={report.total_score} ({sum(1 for c in report.checks if c.passed)}/{len(report.checks)} checks)")
            for c in report.checks:
                if not c.passed:
                    print(f"    - {c.name}: {c.message}")
                elif c.score < 1.0:
                    # warn-only の check (tag_granularity #5088 等) は passed のまま
                    # 減点だけする。ここで出さないと PR ログから完全に消え、
                    # 「黙って死ぬゲート」になる。
                    print(f"    ! {c.name}: {c.message}")
        if not report.passed:
            failures.append(report)
        if report.total_score < args.min_score:
            below_threshold.append(report)

    print()
    print(f"[quality_gate] {len(all_reports)} articles, {len(failures)} with failures, {len(below_threshold)} below min-score {args.min_score}")

    if args.strict and failures:
        return 2
    if below_threshold:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
