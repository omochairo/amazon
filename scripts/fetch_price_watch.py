"""fetch_price_watch.py

NAS 日次価格観測レーン (#3046 / #2995 案8 P1) — chimolog 型「値下げ発見
ダッシュボード」(navi の /price/ ページ、P2 以降で構築) の土台となる、
公開 ASIN 全件を日次で薄く観測するスクリプト。

背景・設計 WHY:
  既存の週1価格観測 (#2953 C案 = fetch_amazon.refresh_article_snapshots が
  write_per_asin_snapshot 経由で price_history.append_price_point を呼ぶ
  経路) は「記事巡回のついで」に発生する観測点で、1 ASIN あたり週1点程度
  にしかならない。日次ダッシュボードには密度が足りないため、本スクリプトは
  独立して全公開 ASIN を毎日 1 周し、観測点を増やす。

  **書き込み先を data/price_watch/ に分離する** (data/price_history/ には
  書かない): data/price_history/ は amazon 側週次レーンの単独書き込み先。
  2 レーンが同一 JSONL に daily/weekly でバラバラに追記すると、home-ops 側
  の日次 data PR と amazon 側週次レーンの書き込みタイミングが競合して
  auto-merge が git conflict で落ちるリスクがある。single-writer 原則で
  ディレクトリを分け、両者のマージ・dedupe は P2 のビルド時派生
  (build_price_dashboard.py 予定) に任せる。

  価格変化点の永続化ロジック自体は data/price_history/ 用に実装済みの
  ``price_history.append_price_point`` (dedupe: 同一価格が6日未満続く場合は
  追記しない) をそのまま再利用する。出力先ディレクトリを
  ``<out_dir>/history`` に変えるだけで、重複抑制の挙動・レコード形式は
  週次レーンと完全に同一にできる。

対象 ASIN の選定:
  data/articles/*.json (STAGE-2/3 サイドカー `*.enrichment.json` /
  `*.seo.json` / `*.quality.json` を除く。build_post.SUFFIX_SKIP /
  fetch_google_suggest.SIDECAR_SUFFIXES と同一の流儀) から
  ``product.asin`` フィールドを読む。ファイル名からの逆算はしない
  (rewrite で新旧本文が一時併存していても、各ファイルの product.asin は
  常に正なので stem の新旧判定は不要)。大文字化して重複排除・ソート。

API 呼び出し:
  Creator API GetItems を 10 件/バッチ、バッチ間 1.1 秒 sleep (TPS=1
  安全マージン。fetch_amazon.refresh_article_snapshots と同じレート)。
  resources は価格・在庫の 2 つだけの最小セット (PRICE_WATCH_RESOURCES)。
  ``offersV2.listings.{price,availability}`` は fetch_amazon.py の
  REFRESH_ITEM_RESOURCES で getItems 実用検証済み (2026-05-26 時点 ✅ 有効
  確認済) のサブセットなので、新規に 04-validate の dry-run gate
  (Issue #785) を通す必要はない。画像・タイトル・browseNodes 等は取得せず
  quota と応答サイズを絞る (~147 call/日、既存 quota 比 +2% 未満の想定)。

欠落 ASIN の扱い:
  GetItems 応答に現れなかった ASIN はカウントのみ (stats.missing)。
  「Amazon から出品が消えた」の確定判定 (status="gone") は
  fetch_amazon.refresh_article_snapshots (週次レーン) の責務であり、本
  スクリプトは判定を持たない (二重判定を避ける)。

エラー処理:
  バッチ単位の例外・想定外応答は warning を出して次バッチへ continue する
  (1 バッチ失敗で全体を止めない)。全バッチが失敗した場合のみ、latest.json
  は書いた上で exit code 1 を返す (呼び出し元 workflow が異常を検知できる
  ように)。

呼び出し元:
  omochairo/amazon-home-ops リポジトリの `40-price-watch.yml` workflow から
  `python3 -m scripts.fetch_price_watch ...` として実行される。

Issue: https://github.com/omochairo/amazon/issues/3046
Issue: https://github.com/omochairo/amazon/issues/2995 (案8)
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import sys
import time
from datetime import datetime, timezone
from typing import Any, Optional

# `python -m scripts.fetch_price_watch` (home-ops workflow の呼び出し形) では
# scripts/ 自体が sys.path に入らないため、同ディレクトリの price_history を
# bare import できない。他の script (build_feature_lists.py 等) と同じ流儀で
# 自ディレクトリを sys.path に足しておく (直接実行 `python scripts/fetch_price_watch.py`
# でも -m 実行でもどちらでも動くようにする)。
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import price_history

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("fetch_price_watch")

DEFAULT_ARTICLES_DIR = "data/articles"
DEFAULT_OUT_DIR = "data/price_watch"

BATCH_SIZE = 10
BATCH_SLEEP_SECONDS = 1.1

# build_post.SUFFIX_SKIP / fetch_google_suggest.SIDECAR_SUFFIXES と同一の流儀。
SIDECAR_SUFFIXES = (".enrichment", ".seo", ".quality")

# 価格・在庫だけが必要なので REFRESH_ITEM_RESOURCES (画像/タイトル/browseNodes
# 込みのフルセット) は使わず、既に有効性確認済みの2つに絞る (docstring 参照)。
PRICE_WATCH_RESOURCES = [
    "offersV2.listings.price",
    "offersV2.listings.availability",
]


def _safe_get(obj: Any, *attrs: str, default: Any = None) -> Any:
    cur = obj
    for a in attrs:
        if cur is None:
            return default
        if isinstance(cur, dict):
            cur = cur.get(a)
        else:
            cur = getattr(cur, a, None)
    return cur if cur is not None else default


def extract_price(item: dict) -> Optional[int]:
    """offersV2.listings[0].price.money.amount を int で返す。取得不能なら None。"""
    listings = _safe_get(item, "offersV2", "listings", default=[])
    if listings:
        money = _safe_get(listings[0], "price", "money", default={})
        amount = money.get("amount") if isinstance(money, dict) else None
        if isinstance(amount, (int, float)) and not isinstance(amount, bool):
            return int(amount)
    return None


def extract_availability(item: dict) -> Optional[str]:
    """offersV2.listings[0].availability.message を返す。無ければ None。"""
    listings = _safe_get(item, "offersV2", "listings", default=[])
    if listings:
        msg = _safe_get(listings[0], "availability", "message")
        if isinstance(msg, str) and msg.strip():
            return msg.strip()
    return None


def collect_published_asins(articles_dir: pathlib.Path) -> list[str]:
    """data/articles/*.json (STAGE-2/3 サイドカー除く) から公開 ASIN を列挙する。

    各記事 JSON の ``product.asin`` フィールドを読む (ファイル名からの逆算は
    しない)。大文字化して重複排除し、ソート済みで返す。
    """
    asins: set[str] = set()
    if not articles_dir.exists():
        return []
    for f in sorted(articles_dir.glob("*.json")):
        if f.stem.endswith(SIDECAR_SUFFIXES):
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"fetch_price_watch: failed to read {f}: {e}")
            continue
        if not isinstance(data, dict):
            continue
        product = data.get("product")
        asin = product.get("asin") if isinstance(product, dict) else None
        if isinstance(asin, str) and asin.strip():
            asins.add(asin.strip().upper())
    return sorted(asins)


def _write_latest(out_dir: pathlib.Path, stats: dict, items: dict) -> dict:
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "creators-api",
        "stats": stats,
        "items": items,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "latest.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1, sort_keys=True),
        encoding="utf-8",
    )
    return result


def run(
    api,
    articles_dir: pathlib.Path,
    out_dir: pathlib.Path,
    max_asins: int = 0,
    dry_run: bool = False,
    sleeper=time.sleep,
) -> dict:
    """全公開 ASIN を巡回して価格観測を行い、latest.json を書く (テスト・main 双方から呼べるように分離)。

    Returns:
        latest.json と同じ shape の dict。dry_run 時はファイルを書かず、
        items は空・history_appended は 0 のまま返す。
    """
    asins = collect_published_asins(articles_dir)
    if max_asins > 0:
        asins = asins[:max_asins]
    logger.info(f"price watch targets: {len(asins)} ASIN(s) (max_asins={max_asins})")

    stats = {"requested": len(asins), "found": 0, "missing": 0, "history_appended": 0}

    if dry_run:
        logger.info(f"[dry-run] would query {len(asins)} ASIN(s) via GetItems; no API calls / writes")
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source": "creators-api",
            "stats": stats,
            "items": {},
        }

    history_dir = out_dir / "history"
    items: dict = {}
    batches_total = 0
    batches_failed = 0

    for i in range(0, len(asins), BATCH_SIZE):
        chunk = asins[i:i + BATCH_SIZE]
        batches_total += 1
        if i:
            sleeper(BATCH_SLEEP_SECONDS)
        try:
            res = api.get_items(chunk, resources=PRICE_WATCH_RESOURCES)
        except Exception as e:
            logger.warning(f"fetch_price_watch: GetItems failed for {chunk}: {e}")
            batches_failed += 1
            continue

        found_items = _safe_get(res, "itemsResult", "items")
        if not isinstance(found_items, list):
            logger.warning(f"fetch_price_watch: unexpected response shape for {chunk}; skipped")
            batches_failed += 1
            continue

        found = {it.get("asin"): it for it in found_items if isinstance(it, dict)}
        for asin in chunk:
            it = found.get(asin)
            if it is None:
                continue  # missing は最後に requested - found から算出する
            stats["found"] += 1
            price = extract_price(it)
            availability = extract_availability(it)
            ts = datetime.now(timezone.utc)
            if price_history.append_price_point(str(history_dir), asin, "amazon", price, availability):
                stats["history_appended"] += 1
            items[asin] = {"p": price, "avail": availability, "ts": ts.isoformat()}

    stats["missing"] = stats["requested"] - stats["found"]

    result = _write_latest(out_dir, stats, items)

    if batches_total > 0 and batches_failed == batches_total:
        logger.error(
            f"fetch_price_watch: all {batches_total} batch(es) failed; "
            "wrote latest.json with zero observations"
        )
        raise SystemExit(1)

    logger.info(
        f"price watch done: requested={stats['requested']} found={stats['found']} "
        f"missing={stats['missing']} history_appended={stats['history_appended']}"
    )
    return result


def main() -> None:
    ap = argparse.ArgumentParser(
        description="NAS 日次価格観測レーン: 公開 ASIN 全件の価格/在庫を Creator API GetItems で巡回する (#3046 / #2995 案8 P1)"
    )
    ap.add_argument("--articles-dir", default=DEFAULT_ARTICLES_DIR, help="記事 JSON のディレクトリ")
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="価格観測の出力先ディレクトリ")
    ap.add_argument("--max-asins", type=int, default=0, help="0=無制限。>0 でスモークテスト用に対象 ASIN 数を制限")
    ap.add_argument("--dry-run", action="store_true", help="対象 ASIN 選定だけ行い、実際の API 呼び出し/書き込みは行わない")
    args = ap.parse_args()

    api = None
    if not args.dry_run:
        from creators_api_client import CreatorsAPIClient

        api = CreatorsAPIClient()

    run(
        api=api,
        articles_dir=pathlib.Path(args.articles_dir),
        out_dir=pathlib.Path(args.out_dir),
        max_asins=args.max_asins,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
