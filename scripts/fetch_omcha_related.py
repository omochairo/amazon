"""fetch_omcha_related.py

Per-ASIN omcha.jp 関連記事フェッチ。`data/articles/*.json` の tags をキーワードに
`internal_links.get_related_articles()` を叩き、結果を
`data/raw/per_asin/<ASIN>/omcha_related.json` に保存する。

旧設計では `build_post._attach_omcha_related()` が描画ループ内で in-band に
omcha.jp API を叩いて 24h TTL の per-ASIN キャッシュを書いていた。それを fetch
層に分離して:

- 描画と I/O を分離 (build_post は読むだけ)
- 出力ファイルを tracked に統一 (untracked 汚染解消、Issue #674)
- _fetch_targets の stale-first cycle (50/run × 7日) に統合 (PR #486 と同型)
- score_calculator が依存する omcha_related.json の存在を保証 (race 解消)

Issue: https://github.com/omochairo/amazon/issues/674
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import _fetch_targets
from internal_links import get_related_articles

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("fetch_omcha_related")

SOURCE = "omcha"
_ASIN_FROM_FILENAME = re.compile(r"(B0[A-Z0-9]{8}|[0-9]{9}[0-9X])")
_SIDECAR_SUFFIXES = (".enrichment", ".seo", ".quality")


def _keyword_from_tags(tags: Any) -> str:
    """記事の top-3 tags をスペース連結して omcha 検索キーワードを作る。

    旧 `build_post._omcha_keyword_from_tags` と完全同一ロジック (回帰防止のため
    test_fetch_omcha_related で同値性を検証している)。
    """
    if not isinstance(tags, list):
        return ""
    picked: list[str] = []
    for t in tags:
        if not isinstance(t, str):
            continue
        s = t.strip()
        if not s:
            continue
        picked.append(s)
        if len(picked) >= 3:
            break
    return " ".join(picked)


def _collect_keyword_pairs(articles_dir: pathlib.Path) -> dict[str, str]:
    """`data/articles/*.json` から `{asin: keyword}` を構築。

    tags が無い記事 (空 / 欠損) はスキップ。omcha 検索は tag-based なので、
    タイトル等での代替はせず「キーワード抽出不能 → fetch 対象から除外」とする。
    """
    out: dict[str, str] = {}
    if not articles_dir.exists():
        return out
    for art_path in sorted(articles_dir.glob("*.json")):
        if art_path.stem.endswith(_SIDECAR_SUFFIXES):
            continue
        m = _ASIN_FROM_FILENAME.search(art_path.stem)
        if not m:
            continue
        asin = m.group(1)
        if asin in out:
            continue
        try:
            art = json.loads(art_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        keyword = _keyword_from_tags(art.get("tags"))
        if keyword:
            out[asin] = keyword
    return out


def _pick_stale_targets(
    out_dir: pathlib.Path,
    keyword_pairs: dict[str, str],
    max_per_run: int,
    stale_after_days: int,
    now: datetime | None = None,
) -> list[tuple[str, str]]:
    """`_fetch_targets.pick_target_asins` の omcha 用ローカル版。

    pick_target_asins は (asin, title) を返すが、omcha は keyword (tags 由来)
    を使うため targets 構築段階で keyword 抽出が必要。本体を流用できないので
    stale-first アルゴリズムだけ local fork している (15 行)。
    """
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=stale_after_days)
    state = _fetch_targets.load_state(out_dir).get(SOURCE, {})

    candidates: list[tuple[datetime, str, str]] = []
    fresh = 0
    for asin, keyword in keyword_pairs.items():
        ts_str = state.get(asin)
        if ts_str:
            ts = _fetch_targets._parse_iso(ts_str)
            if ts > cutoff:
                fresh += 1
                continue
        else:
            ts = datetime.fromtimestamp(0, tz=timezone.utc)
        candidates.append((ts, asin, keyword))

    candidates.sort(key=lambda x: x[0])
    picked = candidates[:max_per_run]
    logger.info(
        f"[{SOURCE}] targets={len(keyword_pairs)} fresh<={stale_after_days}d={fresh} "
        f"stale={len(candidates)} picked={len(picked)}/{max_per_run}"
    )
    return [(asin, keyword) for _, asin, keyword in picked]


def _write_per_asin_cache(
    per_asin_root: pathlib.Path, asin: str, keyword: str, items: list[dict]
) -> None:
    """`per_asin/<ASIN>/omcha_related.json` に書く。

    スキーマは旧 build_post._attach_omcha_related の出力と完全互換:
    `{"keyword": str, "items": [{"title", "url", "score", "thumbnail"}]}`
    """
    path = per_asin_root / asin / "omcha_related.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"keyword": keyword, "items": items}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main():
    ap = argparse.ArgumentParser(description="Per-ASIN omcha.jp related article fetcher")
    ap.add_argument("--out", default="data/raw", help="data/raw ルート")
    ap.add_argument("--articles-dir", default="data/articles", help="記事 JSON dir")
    ap.add_argument(
        "--max-per-run", type=int, default=50,
        help="1 run あたりの最大 ASIN 数 (stale-first cap)",
    )
    ap.add_argument(
        "--stale-after-days", type=int, default=7,
        help="N 日以内に query 済みの ASIN はスキップ (TTL)",
    )
    ap.add_argument("--count", type=int, default=3, help="各 ASIN の取得件数 (top N)")
    ap.add_argument("--min-score", type=int, default=20, help="omcha 側 score の下限")
    ap.add_argument("--sleep", type=float, default=0.3, help="API 呼び出し間 sleep (秒)")
    args = ap.parse_args()

    out_dir = pathlib.Path(args.out)
    per_asin_root = out_dir / "per_asin"
    articles_dir = pathlib.Path(args.articles_dir)

    keyword_pairs = _collect_keyword_pairs(articles_dir)
    if not keyword_pairs:
        logger.warning("No (asin, keyword) pairs found under %s; nothing to fetch", articles_dir)
        return

    targets = _pick_stale_targets(
        out_dir, keyword_pairs, args.max_per_run, args.stale_after_days,
    )
    if not targets:
        logger.info("No stale ASINs to refresh this run")
        return

    queried: list[str] = []
    hits = 0
    for asin, keyword in targets:
        try:
            items = get_related_articles(keyword, count=args.count, min_score=args.min_score)
        except Exception as e:
            logger.error(f"  {asin} ({keyword[:30]}): omcha fetch raised {e}")
            items = []
        _write_per_asin_cache(per_asin_root, asin, keyword, items)
        queried.append(asin)
        if items:
            hits += 1
        logger.info(f"  {asin} ({keyword[:30]}) → {len(items)} item(s)")
        time.sleep(args.sleep)

    _fetch_targets.mark_queried(out_dir, SOURCE, queried)
    logger.info(
        f"omcha_related saved: queried={len(queried)}, with_hits={hits}, "
        f"no_hits={len(queried) - hits}"
    )


if __name__ == "__main__":
    main()
