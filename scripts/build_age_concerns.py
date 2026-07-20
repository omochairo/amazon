"""Build age-hub "よくある悩み" (common concerns) blocks from demand-gap data
(Issue #3332 N2 / #2687 柱1).

demand-gap レーン (scripts/detect_demand_gaps.py, amazon-home-ops shadow lane)
が出す data/analytics/demand_gaps.json の age テーマ別ギャップ (未充足の育児の
悩み検索) を、age hub ページ (/toys-age-N/) の「よくある悩み」ブロック用データ
へ変換する。各悩みには demand-gap 側が既に求めた最も近い商品 (nearest_asin) を
そのまま内部リンク先として添える (本 script は類似度計算をしない・外部 API
呼び出しなし)。

本 PR は amazon 側の配線のみ。demand_gaps.json を commit する home-ops 42
workflow の graduate は別 PR (オーナー対応) のため、demand_gaps.json が未 commit
でもビルドが壊れず空出力になる graceful degradation を必須とする。

Reads:
    data/analytics/demand_gaps.json   (無ければ warn して空出力・exit 0)

Writes:
    hugo/data/concerns/<age_hub_key>.json   (feature.html の age hub が読む。
                                              age_hub_key ∈ age-0,1,2,3,4,6)

Issue: https://github.com/omochairo/amazon/issues/3332 (N2)
       https://github.com/omochairo/amazon/issues/2687 (柱1)
"""
from __future__ import annotations

import argparse
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("build_age_concerns")

DEFAULT_DEMAND_GAPS = "data/analytics/demand_gaps.json"
DEFAULT_OUT_HUGO = "hugo/data/concerns"

# age-5 → age-4 写像 (#3332 N2 spec)。age-4 hub は「4〜5歳」なので age-5 の
# gap を統合する。他は恒等写像。age-0 は現状 gap 0 だが、将来 gap が来た場合に
# concerns/age-0.json を出せるよう _hub_key_for_theme() で age-N 形式は
# このマップに無くても恒等写像として通す。
AGE_HUB_MAP: dict[str, str] = {
    "age-1": "age-1",
    "age-2": "age-2",
    "age-3": "age-3",
    "age-4": "age-4",
    "age-5": "age-4",
    "age-6": "age-6",
}

_AGE_THEME_RE = re.compile(r"^age-\d+$")

# age-2 (58件と最多) のみ intent 別 sub-cluster する。他 hub は単一グループ
# ("困りごと") で十分。固定順: 困りごと → 安全 → 選び方・量。
INTENT_ORDER = ["困りごと", "安全", "選び方・量"]
INTENT_DEFAULT = "困りごと"
INTENT_SAFETY = "安全"
INTENT_CHOICE = "選び方・量"

_SAFETY_KEYWORDS = [
    "口に入れ", "なめ", "舐め", "かじ", "噛", "飲み込", "誤飲", "舐める",
]
_CHOICE_KEYWORDS = [
    "選び方", "少ない", "与えすぎ", "手作り", "飽き", "おすすめ", "何個", "いくつ",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hub_key_for_theme(theme_key: Any) -> str | None:
    """gap の theme_key を age hub key へ写像する。

    AGE_HUB_MAP にあればそれを使う (age-5 → age-4 統合)。無くても "age-N" 形式
    ならば恒等写像として通す (age-0 対応)。programming/english/None 等
    age-* でないテーマは None を返し除外する。
    """
    if not isinstance(theme_key, str) or not theme_key:
        return None
    if theme_key in AGE_HUB_MAP:
        return AGE_HUB_MAP[theme_key]
    if _AGE_THEME_RE.match(theme_key):
        return theme_key
    return None


def classify_intent(query: str) -> str:
    """query 文字列から intent バケットを判定する (最初にマッチしたもの優先)。"""
    text = query or ""
    if any(kw in text for kw in _SAFETY_KEYWORDS):
        return INTENT_SAFETY
    if any(kw in text for kw in _CHOICE_KEYWORDS):
        return INTENT_CHOICE
    return INTENT_DEFAULT


def load_gaps(demand_gaps_path: Path) -> list[dict[str, Any]]:
    """demand_gaps.json の gaps 配列を読む。無い/壊れていれば空リスト (graceful)。"""
    if not demand_gaps_path.exists():
        logger.warning(
            "demand gaps file not found: %s; writing empty output (graceful degradation)",
            demand_gaps_path,
        )
        return []
    try:
        raw = json.loads(demand_gaps_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(
            "failed to read/parse %s: %s; writing empty output (graceful degradation)",
            demand_gaps_path, e,
        )
        return []
    if not isinstance(raw, dict):
        logger.warning("%s is not a JSON object; writing empty output", demand_gaps_path)
        return []
    gaps = raw.get("gaps")
    if not isinstance(gaps, list):
        return []
    return [g for g in gaps if isinstance(g, dict)]


def _concern_entry(gap: dict[str, Any]) -> dict[str, Any]:
    impressions = gap.get("impressions")
    if not isinstance(impressions, (int, float)) or isinstance(impressions, bool):
        impressions = 0
    return {
        "query": gap.get("query") or "",
        "nearest_asin": gap.get("nearest_asin") or "",
        "nearest_title": gap.get("nearest_title") or "",
        "impressions": impressions,
        "source": "gsc" if impressions > 0 else "suggest",
    }


def bucket_by_hub(gaps: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """theme_key が age-* の gap だけを hub key ごとにバケット分けする。

    programming/english/None theme はここで除外される (_hub_key_for_theme が
    None を返すため)。gaps の元の並び順 (suggest 出現優先 → gsc impressions
    降順 → query 昇順、detect_demand_gaps.py._demand_rank_key 由来) は
    そのまま保持する (同点タイブレークの "gap 順維持" に使う)。
    """
    buckets: dict[str, list[dict[str, Any]]] = {}
    for gap in gaps:
        hub_key = _hub_key_for_theme(gap.get("theme_key"))
        if hub_key is None:
            continue
        buckets.setdefault(hub_key, []).append(_concern_entry(gap))
    return buckets


def rank_concerns(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """impressions (gsc 実証需要) 降順にランクする。同点は元の並び順を維持する

    (Python の sort は stable なので、key に impressions のみ使えば同点内の
    相対順序は自動的に入力順 = gap 順のまま保たれる)。
    """
    return sorted(entries, key=lambda e: -(e["impressions"] or 0))


def group_concerns(hub_key: str, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """entries を intent 別 group にまとめる。age-2 のみ intent 分類し、他は単一

    "困りごと" グループにまとめる。groups は 困りごと→安全→選び方・量 の固定順で、
    空 intent は出さない。
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    if hub_key == "age-2":
        for entry in entries:
            intent = classify_intent(entry["query"])
            grouped.setdefault(intent, []).append(entry)
    else:
        grouped[INTENT_DEFAULT] = list(entries)

    groups: list[dict[str, Any]] = []
    for intent in INTENT_ORDER:
        concerns = grouped.get(intent)
        if concerns:
            groups.append({"intent": intent, "concerns": concerns})
    return groups


def serialize_hub(hub_key: str, entries: list[dict[str, Any]], generated_at: str) -> dict[str, Any]:
    return {
        "generated_at": generated_at,
        "type": hub_key,
        "count": len(entries),
        "groups": group_concerns(hub_key, entries),
    }


def run(demand_gaps_path: Path, out_hugo: Path, *, limit: int = 0) -> dict[str, int]:
    """全体を実行し、hub key ごとの悩み件数を dict で返す。

    gap が1件も無い hub は concerns/<key>.json を書かない (partial 側は存在
    しなければ描画しない)。demand_gaps.json 不在・壊れ・gap 0件でも例外を
    送出せず正常終了する。
    """
    gaps = load_gaps(demand_gaps_path)
    buckets = bucket_by_hub(gaps)
    generated_at = _now_iso()
    out_hugo.mkdir(parents=True, exist_ok=True)

    counts: dict[str, int] = {}
    for hub_key, entries in buckets.items():
        ranked = rank_concerns(entries)
        if limit and limit > 0:
            ranked = ranked[:limit]
        if not ranked:
            continue
        payload = serialize_hub(hub_key, ranked, generated_at)
        (out_hugo / f"{hub_key}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        counts[hub_key] = len(ranked)
        logger.info("%s: %d concern(s)", hub_key, len(ranked))
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demand-gaps", default=DEFAULT_DEMAND_GAPS, type=Path)
    parser.add_argument("--out-hugo", default=DEFAULT_OUT_HUGO, type=Path)
    parser.add_argument("--limit", type=int, default=0,
                        help="hub あたりの悩み件数上限 (0=無制限)。")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(levelname)s %(name)s: %(message)s")
    counts = run(args.demand_gaps, args.out_hugo, limit=args.limit)
    print(json.dumps(counts, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
