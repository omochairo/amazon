"""build_price_sparks.py — 全 ASIN の価格推移ミニチャートを1ファイルにまとめる (#5225)。

``hugo/data/price_sparks.json`` を生成する。中身は ASIN をキーにしたチャート
データの辞書で、Hugo 側は ``site.Data.price_sparks.sparks`` から引く
(``partial "price_spark_for.html" <ASIN>``)。

## なぜ ASIN キーの共有ファイルにするか

#5143 / #5167 では ``/price/`` と ``/deals/`` の一覧 JSON にチャートを直接
埋め込んでいた。この方式は「一覧を作る Python スクリプトごとに enrich を足す」
必要があり、水平展開しようとすると同じコードが増える。

一方、カードを描くテンプレートは以下の4種類しかなく、**すべて ASIN を持っている**:

  product_card.html      … home / /posts/ / /brands/ / /tags/ / /categories/
  feature-item.html      … /cospa/ とテーマ・年齢 hub / /deals/
  price_dashboard_item.html … /price/
  _default/ranking.html  … /ranking/ (matched_asin)

そこで「ASIN → チャート」の辞書を1つ作れば、どのテンプレートからも同じ
partial で引ける。データの持ち方が1箇所になるので、/price/ と /deals/ で
条件がズレるといった事故も起きない。

## 出力

    {"generated_at": ISO8601, "count": int, "sparks": {"<ASIN>": {...}}}

採択条件は price_spark.build_card_spark に集約 (観測3点以上・価格の種類2種類
以上)。条件を満たさない ASIN はキーごと入らないので、テンプレート側は
「引けたら描く」だけでよい。

商品ページ (/products/<asin>/) のグラフはこのファイルを使わない。あちらは
build_post.py が ARTICLE_GEOM で別に描く (Keepa の隣に置く観測の証跡であり、
目的も必要な情報量も違う。#5167 参照)。

CLI:
    python scripts/build_price_sparks.py
    python scripts/build_price_sparks.py --out hugo/data/price_sparks.json
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from build_price_dashboard import load_merged_history  # noqa: E402
from price_spark import build_card_spark  # noqa: E402

logger = logging.getLogger("build_price_sparks")

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_DEFAULT_PRICE_WATCH_DIR = _REPO_ROOT / "data" / "price_watch"
_DEFAULT_PRICE_HISTORY_DIR = _REPO_ROOT / "data" / "price_history"
_DEFAULT_OUT = _REPO_ROOT / "hugo" / "data" / "price_sparks.json"


def collect_asins(price_watch_dir: pathlib.Path,
                  price_history_dir: pathlib.Path) -> list[str]:
    """履歴ファイルが存在する ASIN を大文字で重複なく返す。

    2 系統のディレクトリに分かれているのは #2953 / #3046 の single-writer 設計。
    マージ自体は build_price_dashboard.load_merged_history が唯一の実装。
    """
    asins: set[str] = set()
    for directory in (price_history_dir, price_watch_dir / "history"):
        if directory.exists():
            asins |= {p.stem.upper() for p in directory.glob("*.jsonl")}
    return sorted(asins)


def build_sparks(price_watch_dir: pathlib.Path,
                 price_history_dir: pathlib.Path) -> dict[str, dict]:
    sparks: dict[str, dict] = {}
    for asin in collect_asins(price_watch_dir, price_history_dir):
        try:
            history = load_merged_history(asin, price_watch_dir, price_history_dir)
            spark = build_card_spark(history)
        except Exception as e:  # noqa: BLE001 - fail-soft, 1 ASIN の失敗で全体を落とさない
            logger.warning(f"build_sparks: skip {asin}: {e}")
            continue
        if spark is not None:
            sparks[asin] = spark
    return sparks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--price-watch-dir", type=pathlib.Path, default=_DEFAULT_PRICE_WATCH_DIR)
    parser.add_argument("--price-history-dir", type=pathlib.Path, default=_DEFAULT_PRICE_HISTORY_DIR)
    parser.add_argument("--out", type=pathlib.Path, default=_DEFAULT_OUT)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    sparks = build_sparks(args.price_watch_dir, args.price_history_dir)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(sparks),
        "sparks": sparks,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    size_mb = args.out.stat().st_size / 1024 / 1024
    print(f"[price_sparks] count={len(sparks)} size={size_mb:.2f}MB out={args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
