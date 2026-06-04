#!/usr/bin/env python3
"""SNS 配信対象 ASIN を 1 件選んで stdout に出す (Issue #1420 path α / #1580)。

data/articles/ の未投稿 ASIN を「知育 × 価格」加重スコアでランキングし、
最上位の ASIN を 1 行で stdout に印字。何も無い場合は exit 1 + stderr に理由出力。

スコア設計 (#1580 Part 1):
    score = EDU_WEIGHT * edu_norm + PRICE_WEIGHT * price_norm   (default 0.5 / 0.5)

    edu_norm   : 知育 (education 軸 2.0-5.0) を 0-1 に正規化 = (edu - 2.0) / 3.0
                 education 軸は score_calculator.compute_ivs_axes() で算出。
                 旧 Jules パイプラインの product.ivs_detail.education は #1124 で
                 生成廃止されたため、build_post と同じく brand_normalizer +
                 score_calculator で on-the-fly 計算する。cache が残る旧記事は
                 product.ivs_detail.education を graceful fallback に使う。
    price_norm : best_price を log スケールで 0-1 に正規化 (高単価ほど高得点 /
                 アフィ報酬 = 価格 × 料率 のため "高い順")。log なので右に裾を
                 引く価格分布でも 5:5 の重みが実効的に効く (linear だと中央値が
                 0.17 付近に潰れ知育に支配される)。P_MIN/P_MAX で saturate。

    ハード足切りは無し。スコアが低くても全件ランキングに残し最上位を返すため
    未投稿在庫が枯渇しない (#1580 設計判断: 質は相対順位で担保)。
    同点は date 降順 (新しい記事優先) → ASIN で決定的に解決。

state file:  data/sns_published.json
    {"published": ["B0DBTLH8ZM", "B0XXXXXXXX", ...], "updated": "2026-06-03T..."}

呼び出し例:
    python scripts/pick_sns_target.py                       # 選定のみ (state 未更新)
    python scripts/pick_sns_target.py --debug               # 上位ランキングを stderr 表示
    python scripts/pick_sns_target.py --mark B0DBTLH8ZM     # state に追記

workflow では先に「選定 → notify_buffer.py 成功 → --mark で commit」の順で
失敗時に同じ ASIN を再試行できる作りにする。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# score_calculator / brand_normalizer は同じ scripts/ にあり sys.path[0] で解決。
# import 失敗時 (依存欠落) は date FIFO に degrade して配信を止めない。
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from brand_normalizer import normalize as normalize_brand
    from score_calculator import calculate as calculate_score, compute_ivs_axes
    _SCORING_OK = True
except Exception as _imp_err:  # pragma: no cover - 依存欠落は通常起きない
    print(f"[warn] scoring deps unavailable ({_imp_err}); falling back to date FIFO",
          file=sys.stderr)
    _SCORING_OK = False

REPO_ROOT = Path(__file__).resolve().parent.parent
ARTICLES_DIR = REPO_ROOT / "data" / "articles"
STATE_FILE = REPO_ROOT / "data" / "sns_published.json"
ASIN_RE = re.compile(r"-([A-Z0-9]{10})\.json$")
# 記事本体ではないサイドカー (quality-gate レポート / enrichment / seo)。
# これらは product を持たず、かつファイル名が本体 *.json より lexically 大きく
# (例 ".quality.json" > ".json")、素朴な dedup/glob[-1] で本体を shadow して
# しまう。選定・配信の両方で除外する (build_post.SUFFIX_SKIP と同方針)。
SIDECAR_SUFFIXES = (".enrichment.json", ".quality.json", ".seo.json")

# --- スコア重みと正規化定数 (#1580) -------------------------------------
EDU_WEIGHT = float(os.environ.get("SNS_EDU_WEIGHT", "0.5"))
PRICE_WEIGHT = float(os.environ.get("SNS_PRICE_WEIGHT", "0.5"))
# 価格 log 正規化の下端/上端。実データ分布 (median ¥2,609 / p95 ¥10,783 /
# max ¥28,050) を踏まえ 500-20000 で saturate。
PRICE_MIN = 500.0
PRICE_MAX = 20000.0
_EDU_FLOOR = 2.0  # compute_ivs_axes は education を 2.0-5.0 に clamp
_EDU_CEIL = 5.0


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"published": [], "updated": None}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("published"), list):
            print(f"[warn] {STATE_FILE.name} malformed, resetting", file=sys.stderr)
            return {"published": [], "updated": None}
        return data
    except json.JSONDecodeError as e:
        print(f"[warn] {STATE_FILE.name} JSON error: {e}, resetting", file=sys.stderr)
        return {"published": [], "updated": None}


def save_state(state: dict) -> None:
    state["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def list_articles_desc() -> list[tuple[str, str]]:
    """(asin, filename) を新しい順で返す。同一 ASIN の duplicate は最新側を残す。"""
    seen: dict[str, str] = {}
    for path in ARTICLES_DIR.glob("*.json"):
        if path.name.endswith(SIDECAR_SUFFIXES):
            continue
        m = ASIN_RE.search(path.name)
        if not m:
            continue
        asin = m.group(1)
        # filename prefix が日付なので文字列比較で降順 OK
        if asin not in seen or path.name > seen[asin]:
            seen[asin] = path.name
    return sorted(seen.items(), key=lambda kv: kv[1], reverse=True)


@dataclass
class Candidate:
    asin: str
    filename: str
    score: float
    edu: float | None       # 0-1 正規化後 (None=不明)
    price: float | None     # best_price 生値 (None=不明)
    edu_axis: float | None  # 2.0-5.0 の education 軸生値 (debug 用)


def _education_axis(article: dict, asin: str) -> float | None:
    """education 軸 (2.0-5.0) を返す。算出不能なら None。

    build_post._build_article_index と同じく score_calculator で再計算し、
    失敗時は旧 Jules 記事に残る product.ivs_detail.education を再利用する。
    """
    product = article.get("product") if isinstance(article.get("product"), dict) else {}
    if _SCORING_OK:
        raw_brand = product.get("brand")
        if raw_brand:
            try:
                sr = calculate_score(article, normalize_brand(raw_brand), asin=asin)
                e = compute_ivs_axes(sr.breakdown).get("education")
                if isinstance(e, (int, float)):
                    return float(e)
            except Exception:
                pass  # fall through to cached value
    ivs = product.get("ivs_detail")
    if isinstance(ivs, dict):
        e = ivs.get("education")
        if isinstance(e, (int, float)) and e > 0:
            return float(e)
    return None


def _edu_norm(edu_axis: float | None) -> float:
    if edu_axis is None:
        return 0.0
    clamped = max(_EDU_FLOOR, min(_EDU_CEIL, edu_axis))
    return (clamped - _EDU_FLOOR) / (_EDU_CEIL - _EDU_FLOOR)


def _price_norm(price: float | None) -> float:
    if not isinstance(price, (int, float)) or price <= 0:
        return 0.0
    lo, hi = math.log(PRICE_MIN), math.log(PRICE_MAX)
    return max(0.0, min(1.0, (math.log(price) - lo) / (hi - lo)))


def _date_int(filename: str) -> int:
    """ファイル名先頭の日付を int 化 (YYYY-MM-DD → 20260604)。

    同点 tie-break で新しい記事を優先するため、ソートでは符号反転して使う。
    日付として読めない場合は 0 (最古扱い)。
    """
    try:
        return int(filename[:10].replace("-", ""))
    except ValueError:
        return 0


def rank_candidates(published: set[str]) -> list[Candidate]:
    """未投稿 ASIN を score 降順 (同点は date 降順 → ASIN) で返す。"""
    out: list[Candidate] = []
    for asin, filename in list_articles_desc():
        if asin in published:
            continue
        try:
            article = json.loads((ARTICLES_DIR / filename).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"[warn] {filename} unreadable: {e}", file=sys.stderr)
            continue
        product = article.get("product") if isinstance(article.get("product"), dict) else {}
        edu_axis = _education_axis(article, asin)
        price = product.get("best_price")
        if not isinstance(price, (int, float)):
            price = None
        edu_n = _edu_norm(edu_axis)
        price_n = _price_norm(price)
        score = EDU_WEIGHT * edu_n + PRICE_WEIGHT * price_n
        out.append(Candidate(asin, filename, score, edu_n, price, edu_axis))
    # score 降順 → date 降順 → ASIN 昇順 (決定的)
    out.sort(key=lambda c: (-c.score, -_date_int(c.filename), c.asin))
    return out


def pick_next(published: set[str]) -> str | None:
    ranked = rank_candidates(published)
    return ranked[0].asin if ranked else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mark", metavar="ASIN", help="この ASIN を published に追記して終了")
    parser.add_argument("--limit", type=int, default=int(os.environ.get("SNS_HISTORY_LIMIT", "500")),
                        help="published 履歴の保持上限 (default 500)")
    parser.add_argument("--debug", action="store_true",
                        help="上位ランキングを stderr に表示 (選定 ASIN は通常通り stdout)")
    parser.add_argument("--top", type=int, default=15, help="--debug 表示件数 (default 15)")
    args = parser.parse_args()

    state = load_state()
    published = set(state["published"])

    if args.mark:
        asin = args.mark.strip().upper()
        if asin in published:
            print(f"[skip] {asin} already in published", file=sys.stderr)
            return 0
        state["published"].append(asin)
        # 上限を超えたら古い側 (先頭) を削る
        if len(state["published"]) > args.limit:
            state["published"] = state["published"][-args.limit:]
        save_state(state)
        print(f"[mark] {asin} added (total={len(state['published'])})", file=sys.stderr)
        return 0

    ranked = rank_candidates(published)
    if args.debug:
        mode = "scoring" if _SCORING_OK else "FIFO-fallback"
        print(f"[debug] mode={mode} edu_w={EDU_WEIGHT} price_w={PRICE_WEIGHT} "
              f"candidates={len(ranked)}", file=sys.stderr)
        print(f"[debug] {'rank':>4} {'asin':12} {'score':>6} {'edu_n':>6} "
              f"{'edu/5':>6} {'price':>7} {'date':>10}", file=sys.stderr)
        for i, c in enumerate(ranked[: args.top], 1):
            ea = f"{c.edu_axis:.1f}" if c.edu_axis is not None else "  -"
            pr = f"{int(c.price):,}" if c.price is not None else "-"
            print(f"[debug] {i:>4} {c.asin:12} {c.score:6.3f} {c.edu:6.3f} "
                  f"{ea:>6} {pr:>7} {_date_int(c.filename):>10}", file=sys.stderr)

    if not ranked:
        print("[skip] no unpublished article found", file=sys.stderr)
        return 1
    print(ranked[0].asin)
    return 0


if __name__ == "__main__":
    sys.exit(main())
