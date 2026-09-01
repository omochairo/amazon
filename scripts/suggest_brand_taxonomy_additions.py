"""suggest_brand_taxonomy_additions.py

A-6 (epic #1356): GSC weekly の by_query から brand_taxonomy.yaml に
未登録のブランド候補トークンを抽出し、`data/analytics/brand_taxonomy_suggestions.json`
に書き出す read-only スクリプト。

アルゴリズム:
1. brand_taxonomy.yaml 全 canonical + aliases を lowercase で taxonomy set に
2. by_query をループ。impressions >= MIN_IMPRESSIONS の行のみ評価
3. クエリを whitespace + 一部記号で tokenize
4. 各 token について:
   - 長さ >= MIN_TOKEN_LEN (default 3) でない → skip
   - brand-like pattern (katakana / 半角ｶﾅ / alphabet / 数字 / ・ . - _ 以外を含まない、
     かつ最低 1 文字は katakana / alphabet) に合わない → skip
   - GENERIC_TOKENS_STOPWORDS に含まれる → skip
   - 既知 taxonomy term との substring 関係 (token in term OR term in token) で
     カバーされている → skip
5. 残った token を集計 (impressions sum + 代表クエリ Top 3)、上位 N 件を出力

出力は「人間がレビューする候補リスト」であり、auto-PR は安全のため作らない。
open_brand_suggestion_issue.py が単一の weekly Issue として提案を集約する。

Issue: https://github.com/omochairo/amazon/issues/1356 (epic E1 / A-6)
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import re
import sys
from typing import Any

import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("suggest_brand_taxonomy_additions")

DEFAULT_GSC = "data/analytics/gsc_weekly.json"
DEFAULT_TAXONOMY = "data/brand_taxonomy.yaml"
DEFAULT_OUT = "data/analytics/brand_taxonomy_suggestions.json"
DEFAULT_MIN_IMPRESSIONS = 20
DEFAULT_MIN_TOKEN_LEN = 3
DEFAULT_TOP_N = 10
DEFAULT_QUERY_EXAMPLES = 3

# クエリトークン分割: whitespace と一部の括弧 / 引用符 / 区切り記号
# (ドット / 中黒 / ハイフンはトークン内に残す。例: "デュエル・マスターズ", "Fisher-Price")
_SPLIT_RE = re.compile(
    r"[\s　\(\)\[\]\{\}「」『』【】\"'"
    r"“”‘’"
    r"!\?,\.。、;:|&+\\/]+"
)

# brand-like: 許容文字のみ
# - 全角カタカナ (U+30A0..U+30FF, 長音符 ー / 中黒 ・ を含む)
# - 半角カタカナ (U+FF65..U+FF9F)
# - 英字 / 数字 / - _
_ALLOWED_RE = re.compile(
    r"^[゠-ヿ･-ﾟa-zA-Z0-9\-_]+$"
)
# 少なくとも 1 文字は katakana or alphabet (長音符・中黒・数字だけのトークンを除く)
_HAS_BRAND_CHAR_RE = re.compile(
    r"[ァ-ヶｦ-ﾝa-zA-Z]"
)

# 非ブランドのカタカナ / 英字 generic 語 (反復で育てる前提の初期セット)
GENERIC_TOKENS_STOPWORDS: set[str] = {
    # 玩具カテゴリ
    "パズル", "ブロック", "ゲーム", "タブレット", "ロボット", "ピアノ",
    "ドラム", "アルファベット", "アクション", "シリーズ", "セット",
    "カード", "ボックス", "バランス", "テーブル", "スマホ", "ヘッドホン",
    "オモチャ",
    # 2026-07-11 (#2644): ベイブレードX の商品シリーズ名 (例: "BX-50 ランダムブースターVol.11")。
    # ブランドではないため reject。tokenizer は "." で分割するので "vol" は小文字で登録する。
    "ランダムブースターvol", "ランダムブースター",
    # イベント / シーズン
    "ハーフ", "バースデー", "クリスマス", "ハロウィン", "プレゼント",
    "イベント", "セール", "キャンペーン",
    # ターゲット
    "ベビー", "キッズ", "ボーイ", "ガール", "ファミリー",
    # メディア / 商業
    "レビュー", "ランキング", "オリジナル", "クチコミ", "クーポン",
    "メーカー", "ブランド", "サイト", "メディア", "アプリ", "アンケート",
    "ジャンル", "カテゴリ", "リスト",
    # 教育法 / コンセプト
    "プログラミング", "モンテッソーリ", "シュタイナー", "アクティブ",
    # 文房具
    "シール", "ステッカー", "ノート", "クレヨン", "ペン", "リング",
    # 抽象
    "リアル", "デジタル", "アナログ", "ホーム", "アウトドア",
    "スポーツ", "ファッション", "コスメ", "ジュエリー", "スマート",
    # alphabet
    "amazon", "rakuten", "google", "yahoo", "youtube",
    "stem", "diy", "kids", "baby", "toys", "toy",
    "vs", "the", "for", "and", "with",
    "best", "top", "new", "buy", "shop",
}


def load_taxonomy_terms(path: pathlib.Path) -> set[str]:
    """brand_taxonomy.yaml から canonical + aliases を lowercase set として取得。"""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    terms: set[str] = set()
    for brand in data.get("brands", []):
        canonical = brand.get("canonical")
        if canonical:
            terms.add(str(canonical).lower())
        for alias in brand.get("aliases", []) or []:
            terms.add(str(alias).lower())
    return terms


def is_brand_like(token: str, min_len: int) -> bool:
    if len(token) < min_len:
        return False
    if not _ALLOWED_RE.match(token):
        return False
    if not _HAS_BRAND_CHAR_RE.search(token):
        return False
    return True


def covered_by_taxonomy(token_lower: str, taxonomy_terms: set[str]) -> bool:
    """token がいずれかの taxonomy term と substring 関係でカバーされていれば True。"""
    for term in taxonomy_terms:
        if not term:
            continue
        if token_lower == term:
            return True
        if token_lower in term or term in token_lower:
            return True
    return False


def tokenize(query: str) -> list[str]:
    return [t for t in _SPLIT_RE.split(query) if t]


def suggest(gsc: dict[str, Any], taxonomy_terms: set[str], *,
            min_impressions: int = DEFAULT_MIN_IMPRESSIONS,
            min_token_len: int = DEFAULT_MIN_TOKEN_LEN,
            top_n: int = DEFAULT_TOP_N,
            query_examples: int = DEFAULT_QUERY_EXAMPLES) -> dict[str, Any]:
    by_query = gsc.get("by_query", []) or []
    candidates: dict[str, dict[str, Any]] = {}

    eligible = 0
    for row in by_query:
        impressions = row.get("impressions", 0)
        if impressions < min_impressions:
            continue
        eligible += 1
        query = row.get("query", "")
        tokens = tokenize(query)
        seen_in_query: set[str] = set()
        for token in tokens:
            if not is_brand_like(token, min_token_len):
                continue
            token_lower = token.lower()
            if token_lower in seen_in_query:
                continue
            if token_lower in GENERIC_TOKENS_STOPWORDS:
                continue
            if token_lower in (s.lower() for s in GENERIC_TOKENS_STOPWORDS):
                # already covered above; kept for safety re uppercase
                continue
            if covered_by_taxonomy(token_lower, taxonomy_terms):
                continue
            seen_in_query.add(token_lower)
            entry = candidates.setdefault(token, {
                "token": token,
                "impressions_sum": 0,
                "query_count": 0,
                "queries": [],
            })
            entry["impressions_sum"] += impressions
            entry["query_count"] += 1
            entry["queries"].append({
                "query": query,
                "impressions": impressions,
                "clicks": row.get("clicks", 0),
                "ctr": row.get("ctr", 0.0),
                "position": row.get("position", 0.0),
            })

    # トリミング + ソート
    for entry in candidates.values():
        entry["queries"].sort(key=lambda r: r["impressions"], reverse=True)
        entry["queries"] = entry["queries"][:query_examples]

    sorted_candidates = sorted(
        candidates.values(), key=lambda e: e["impressions_sum"], reverse=True,
    )
    sorted_candidates = sorted_candidates[:top_n]

    return {
        "source_range": gsc.get("range"),
        "params": {
            "min_impressions": min_impressions,
            "min_token_len": min_token_len,
            "top_n": top_n,
            "taxonomy_term_count": len(taxonomy_terms),
        },
        "eligible": eligible,
        "candidates": sorted_candidates,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--gsc", default=DEFAULT_GSC)
    p.add_argument("--taxonomy", default=DEFAULT_TAXONOMY)
    p.add_argument("--out", default=DEFAULT_OUT)
    p.add_argument("--min-impressions", type=int, default=DEFAULT_MIN_IMPRESSIONS)
    p.add_argument("--min-token-len", type=int, default=DEFAULT_MIN_TOKEN_LEN)
    p.add_argument("--top-n", type=int, default=DEFAULT_TOP_N)
    args = p.parse_args()

    gsc_path = pathlib.Path(args.gsc)
    tax_path = pathlib.Path(args.taxonomy)
    if not gsc_path.exists():
        logger.error("GSC input not found: %s", gsc_path)
        return 2
    if not tax_path.exists():
        logger.error("taxonomy not found: %s", tax_path)
        return 2

    gsc = json.loads(gsc_path.read_text(encoding="utf-8"))
    taxonomy_terms = load_taxonomy_terms(tax_path)
    logger.info("loaded taxonomy: %d terms", len(taxonomy_terms))

    result = suggest(
        gsc, taxonomy_terms,
        min_impressions=args.min_impressions,
        min_token_len=args.min_token_len,
        top_n=args.top_n,
    )

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("wrote %s (%d candidates)", out, len(result["candidates"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
