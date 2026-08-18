"""build_price_dashboard.py

N4-P2 (#3332 / #3046 P1 follow-up) — 「値下げ発見ダッシュボード」/price/ 用の
集計を出力する。chimolog.co の価格ページ型。再訪動機 + information gain が
戦略目的 (#3332 N4)。

P1 (#3046, CLOSED) の日次価格観測レーンが蓄積した以下 2 系統の価格観測点を
読んで値下げ商品を検出し、ビルド時に ``hugo/data/price_dashboard.json`` を
生成する (commit しない。/hugo gitignore 配下の派生物として毎ビルド再生成)。

読み込むデータ (すべて既に main に存在・読み取り専用):
  - ``data/price_watch/latest.json``:
      各 ASIN の最新価格・在庫スナップショット。
      ``{"generated_at": ISO, "items": {"<ASIN>": {"avail": str|None,
      "p": int円, "ts": ISO}}}``
  - ``data/price_watch/history/<ASIN>.jsonl`` / ``data/price_history/<ASIN>.jsonl``:
      価格変化点のみを記録した append-only 履歴 (#2953 C案 / #3046 P1 の
      single-writer 設計で 2 ディレクトリに分かれている)。本モジュールが
      唯一のマージ地点。1 行 = ``{"ts": ISO, "source": "amazon",
      "price": int, "availability": str|None}``。
  - ``data/articles/*.json`` (サイドカー ``*.enrichment.json`` /
      ``*.seo.json`` / ``*.quality.json`` を除く):
      商品メタ (name/brand/image)。公開記事が存在しない ASIN は
      ダッシュボードに載せない (公開ページが無いため)。

値下げ算出仕様は #3332 issue 本文で確定済み。しきい値は下の定数
(_DROP_PCT_THRESHOLD / _DROP_YEN_THRESHOLD / _WINDOW_DAYS / _TOP_N_CAP) に
まとめてあり、較正可能 (後日 observation を見て調整する想定)。

CLI:
    python scripts/build_price_dashboard.py
    python scripts/build_price_dashboard.py --articles-dir X --price-watch-dir Y ...
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import pathlib
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

# 知育スコア (ivs_100/ivs_axes) は build_feature_lists.py / build_post.py と同じ
# score_calculator で再計算する (#3563 カード情報統一。/price/ にもスコア・年齢・
# Amazon リンクを表示するため)。採択後の最大 _TOP_N_CAP 件のみ enrich する
# (全 1580 記事でスコア計算しないための fail-soft 2 段構え、enrich_items 参照)。
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from brand_normalizer import normalize as normalize_brand  # noqa: E402
from build_feature_lists import age_min_months_from_article  # noqa: E402
import price_overlay  # noqa: E402
from score_calculator import calculate as calculate_score, compute_ivs_axes  # noqa: E402

logger = logging.getLogger("build_price_dashboard")

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_DEFAULT_ARTICLES_DIR = _REPO_ROOT / "data" / "articles"
_DEFAULT_PRICE_WATCH_DIR = _REPO_ROOT / "data" / "price_watch"
_DEFAULT_PRICE_HISTORY_DIR = _REPO_ROOT / "data" / "price_history"
_DEFAULT_OUT = _REPO_ROOT / "hugo" / "data" / "price_dashboard.json"

# build_post.SUFFIX_SKIP / fetch_price_watch.SIDECAR_SUFFIXES と同一の流儀。
SIDECAR_SUFFIXES = (".enrichment", ".seo", ".quality")

# ------------------------------------------------------------------------
# 較正可能なしきい値 (#3332 仕様確定値。observation を見て後日調整する)
# ------------------------------------------------------------------------
_DROP_PCT_THRESHOLD = 0.05       # 値下げ率 5% 未満は採択しない
_DROP_YEN_THRESHOLD = 100        # 値下げ幅 100円 未満は採択しない (端数ノイズ除け)
_WINDOW_DAYS = 90                # 「以前の価格」を探す直近日数
_TOP_N_CAP = 150                 # JSON / ページ肥大回避の上限件数

# 在庫: 「明確に購入不可」と読めるマーカーの一覧は price_overlay に移した
# (#5130 残件3)。/deals/ /cospa/ 側 (build_feature_lists) でも同じ判定を使うので、
# 在庫の意味を決める場所を 1 つにする。ここでの再エクスポートは既存の
# import 元 (テスト含む) を壊さないため。
_UNAVAILABLE_MARKERS = price_overlay.UNAVAILABLE_MARKERS


# ------------------------------------------------------------------------
# 記事メタ (ASIN -> name/brand/image/url) の読み込み
# ------------------------------------------------------------------------

def _is_primary_article(path: str) -> bool:
    stem = pathlib.Path(path).stem
    return not stem.endswith(SIDECAR_SUFFIXES)


def load_article_meta(articles_dir: pathlib.Path | str) -> dict[str, dict[str, Any]]:
    """``data/articles/*.json`` (サイドカー除く) から ASIN(大文字) -> meta を返す。

    meta = {"name", "brand", "image", "url", "path"}。``path`` は #3563 の
    enrich_items が採択後の最大 _TOP_N_CAP 件だけ記事 json を再読込して
    age_min_months / amazon_url / ivs_100 / ivs_axes を計算するために使う
    (全 1580 記事でスコア計算しないための遅延評価)。同一 ASIN の記事が複数
    あった場合は後勝ち (glob のソート順で最後に読んだものが残る。再生成で
    新しいファイル名が辞書順で後ろに来るケースが多いため build_feature_lists
    等と同様「最後に見た記事を優先」で実用上問題ない)。
    """
    meta: dict[str, dict[str, Any]] = {}
    files = sorted(
        f for f in glob.glob(str(pathlib.Path(articles_dir) / "*.json"))
        if _is_primary_article(f)
    )
    for f in files:
        try:
            with open(f, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"skip unreadable article {f}: {e}")
            continue
        if not isinstance(d, dict):
            continue
        product = d.get("product") or {}
        asin = product.get("asin")
        if not isinstance(asin, str) or not asin.strip():
            continue
        asin = asin.strip().upper()
        meta[asin] = {
            "name": product.get("name"),
            "brand": product.get("brand"),
            "image": product.get("image"),
            "url": f"/products/{asin.lower()}/",
            "path": f,
        }
    return meta


# ------------------------------------------------------------------------
# 価格履歴の読み込み・マージ
# ------------------------------------------------------------------------

def _parse_iso8601(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _read_history_file(path: pathlib.Path) -> list[tuple[datetime, int]]:
    """1 jsonl ファイルを読んで (ts, price) のリストを返す。壊れた行は無視する。"""
    points: list[tuple[datetime, int]] = []
    if not path.exists():
        return points
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                ts = _parse_iso8601(rec.get("ts"))
                price = rec.get("price")
                if ts is None or not isinstance(price, int) or isinstance(price, bool) or price <= 0:
                    continue
                points.append((ts, price))
    except OSError as e:
        logger.warning(f"failed to read history file {path}: {e}")
    return points


def load_merged_history(
    asin: str,
    price_watch_dir: pathlib.Path,
    price_history_dir: pathlib.Path,
) -> list[tuple[datetime, int]]:
    """P1 の 2 系統ディレクトリ (single-writer) をマージ・dedupe して ts 昇順で返す。

    dedupe は (ts, price) の完全一致ペアのみを対象にする (同一観測点が両
    ディレクトリに独立に書かれることは無いはずだが、将来の運用変更に備えた
    保険)。
    """
    points = (
        _read_history_file(price_watch_dir / "history" / f"{asin}.jsonl")
        + _read_history_file(price_history_dir / f"{asin}.jsonl")
    )
    dedup = sorted(set(points), key=lambda p: p[0])
    return dedup


# ------------------------------------------------------------------------
# 値下げ判定
# ------------------------------------------------------------------------

def evaluate_drop(
    current_price: int,
    current_ts: datetime,
    history_points: list[tuple[datetime, int]],
    *,
    window_days: int = _WINDOW_DAYS,
    drop_pct_threshold: float = _DROP_PCT_THRESHOLD,
    drop_yen_threshold: int = _DROP_YEN_THRESHOLD,
) -> Optional[dict[str, Any]]:
    """1 ASIN 分の値下げ判定を行う。採択されなければ None を返す。

    Returns (採択時):
        {"reference": int, "drop_yen": int, "drop_pct": float, "all_time_low": bool}
    """
    # current を含めた全観測点プール ("全履歴" = 履歴 + current)。
    all_points = history_points + [(current_ts, current_price)]

    window_start = current_ts - timedelta(days=window_days)
    window_points = [
        p for (t, p) in all_points if window_start <= t <= current_ts
    ]
    if len(window_points) < 2:
        # current 以外に窓内観測点が無い = 「以前の価格」と比較できない
        return None

    reference = max(window_points)
    drop_yen = reference - current_price
    drop_pct = (drop_yen / reference) if reference > 0 else 0.0

    if drop_pct < drop_pct_threshold or drop_yen < drop_yen_threshold:
        return None

    all_time_low = current_price <= min(p for (_, p) in all_points)

    return {
        "reference": reference,
        "drop_yen": drop_yen,
        "drop_pct": round(drop_pct, 3),
        "all_time_low": all_time_low,
    }


def enrich_items(
    items: list[dict[str, Any]],
    article_meta: dict[str, dict[str, Any]],
    price_watch_dir: pathlib.Path | None = None,
    price_history_dir: pathlib.Path | None = None,
) -> None:
    """採択済み ``items`` (最大 _TOP_N_CAP 件) を in-place で enrich する。

    追加するフィールド: age_min_months / amazon_url / ivs_100 / ivs_axes。
    build_feature_lists.py と同一の式で計算する (score_calculator 再計算)。
    articles_meta に ``path`` が無い・記事 json が読めない・計算失敗のいずれ
    でも fail-soft で None のまま残す (#3563: nil ガード必須)。全記事 (最大
    1580件) でスコアを計算しないよう、呼出し側は top_n_cap 適用後にのみ
    このフィールドを呼ぶこと。
    """
    for item in items:
        item.setdefault("age_min_months", None)
        item.setdefault("amazon_url", None)
        item.setdefault("ivs_100", None)
        item.setdefault("ivs_axes", None)

        # #5225: 価格推移チャートはここで埋め込まない。build_price_sparks.py が
        # ASIN キーの hugo/data/price_sparks.json を作り、Hugo 側が partial
        # "price_spark_for.html" で引く。一覧を作るスクリプトごとに enrich を
        # 増やさないための SSOT 化 (/price/ /deals/ /cospa/ /brands/ /tags/ ...
        # で採択条件がズレる事故も同時に防げる)。
        meta = article_meta.get(item["asin"]) or {}
        path = meta.get("path")
        if not path:
            continue
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"enrich_items: skip unreadable article {path}: {e}")
            continue
        if not isinstance(raw, dict):
            continue

        item["age_min_months"] = age_min_months_from_article(raw)

        product = raw.get("product") or {}
        prices = product.get("prices") or {}
        amazon_block = prices.get("amazon") or {}
        item["amazon_url"] = amazon_block.get("url")

        try:
            brand = normalize_brand(product.get("brand") or "")
            sr = calculate_score(raw, brand, asin=item["asin"])
            item["ivs_100"] = sr.total_100
            item["ivs_axes"] = compute_ivs_axes(sr.breakdown)
        except Exception as e:  # noqa: BLE001 - fail-soft, スコア計算失敗で全体を落とさない
            logger.warning(f"enrich_items: score calc failed for {item['asin']}: {e}")


def is_unavailable(avail: Any) -> bool:
    """avail が「明確に購入不可」なら True。None・空文字も購入不可扱い。

    None を購入不可に倒すのはここだけ。入力が latest.json の観測レコードそのもの
    なので、「観測はあるのに avail が無い」= その観測の取得に失敗した、と読める
    ため。観測を持たない ASIN が入りうる /deals/ 側は
    ``price_overlay.is_explicitly_unavailable`` (明示だけを見る) を使う。
    """
    if not isinstance(avail, str) or not avail.strip():
        return True
    return price_overlay.is_explicitly_unavailable(avail)


# ------------------------------------------------------------------------
# 集計本体
# ------------------------------------------------------------------------

def build_items(
    latest: dict[str, Any],
    article_meta: dict[str, dict[str, Any]],
    price_watch_dir: pathlib.Path,
    price_history_dir: pathlib.Path,
    *,
    top_n_cap: int = _TOP_N_CAP,
) -> list[dict[str, Any]]:
    items_in = latest.get("items") or {}
    if not isinstance(items_in, dict):
        return []

    out: list[dict[str, Any]] = []
    for asin_raw, rec in items_in.items():
        if not isinstance(rec, dict):
            continue
        asin = str(asin_raw).strip().upper()

        meta = article_meta.get(asin)
        if meta is None:
            continue  # 公開記事が無い ASIN はダッシュボードに載せない

        if is_unavailable(rec.get("avail")):
            continue

        current_price = rec.get("p")
        if not isinstance(current_price, int) or isinstance(current_price, bool) or current_price <= 0:
            continue

        current_ts = _parse_iso8601(rec.get("ts"))
        if current_ts is None:
            continue

        history_points = load_merged_history(asin, price_watch_dir, price_history_dir)
        drop = evaluate_drop(current_price, current_ts, history_points)
        if drop is None:
            continue

        out.append({
            "asin": asin,
            "name": meta.get("name"),
            "brand": meta.get("brand"),
            "image": meta.get("image"),
            "url": meta.get("url"),
            "current": current_price,
            "reference": drop["reference"],
            "drop_yen": drop["drop_yen"],
            "drop_pct": drop["drop_pct"],
            "all_time_low": drop["all_time_low"],
            "last_updated": rec.get("ts"),
        })

    out.sort(key=lambda e: e["drop_pct"], reverse=True)
    selected = out[:top_n_cap]
    # #3563: スコア計算 (enrich_items) は採択後の最大 top_n_cap 件だけに適用する
    # (全 1580 記事で毎回スコア再計算しないため top_n_cap 適用後に呼ぶ)。
    enrich_items(selected, article_meta, price_watch_dir, price_history_dir)
    return selected


def load_latest(price_watch_dir: pathlib.Path | str) -> dict[str, Any]:
    path = pathlib.Path(price_watch_dir) / "latest.json"
    if not path.exists():
        logger.warning(f"latest.json not found at {path}; treating as empty (graceful)")
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"failed to read {path}: {e}; treating as empty (graceful)")
        return {}
    return data if isinstance(data, dict) else {}


def write_output(payload: dict[str, Any], out_path: pathlib.Path | str) -> None:
    out_path = pathlib.Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def run(
    *,
    articles_dir: pathlib.Path,
    price_watch_dir: pathlib.Path,
    price_history_dir: pathlib.Path,
    out_path: pathlib.Path,
) -> dict[str, Any]:
    latest = load_latest(price_watch_dir)
    article_meta = load_article_meta(articles_dir)
    items = build_items(latest, article_meta, price_watch_dir, price_history_dir)

    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_updated": latest.get("generated_at"),
        "count": len(items),
        "items": items,
    }
    write_output(payload, out_path)
    return payload


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--articles-dir", default=str(_DEFAULT_ARTICLES_DIR))
    p.add_argument("--price-watch-dir", default=str(_DEFAULT_PRICE_WATCH_DIR))
    p.add_argument("--price-history-dir", default=str(_DEFAULT_PRICE_HISTORY_DIR))
    p.add_argument("--out", default=str(_DEFAULT_OUT))
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    payload = run(
        articles_dir=pathlib.Path(args.articles_dir),
        price_watch_dir=pathlib.Path(args.price_watch_dir),
        price_history_dir=pathlib.Path(args.price_history_dir),
        out_path=pathlib.Path(args.out),
    )
    print(
        f"[price_dashboard] count={payload['count']} "
        f"source_updated={payload['source_updated']} out={args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
