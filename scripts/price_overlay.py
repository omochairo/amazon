"""price_overlay.py

#4007 — 「現在の Amazon 価格」解決の単一 source of truth。

背景・設計 WHY:
  商品記事の Amazon 価格は ``data/articles/*.json`` の
  ``product.prices.amazon.price`` に**記事生成時のまま凍結**していた。
  build_post.py の ``_backfill_amazon_badges`` は ``BADGE_FIELDS``
  (availability / loyalty_points / savings_percentage / free_shipping) しか
  埋めず、しかも「既存値が勝つ」ため、価格は一度書かれたら更新されない。
  2026-07-26 の実測では比較可能 1499 件のうち **45% (669 件) が現在価格と
  不一致**、20% 以上ズレたものが 107 件あった (最悪 +215%)。

  一方で新鮮な価格はすでにリポジトリ内に 2 系統ある:

    - ``data/price_watch/latest.json`` (日次 / NAS レーン。fetch_price_watch.py)
        ``{"generated_at": ISO,
           "items": {"<ASIN>": {"p": 3149, "off": 26, "ts": ISO,
                                "avail": "在庫あり。"}}}``
        ``off`` (割引率%) は 0 のときキー自体が無い。``avail`` も欠けうる。
        公開 ASIN の 95% をカバーし、観測は中央値 0.6 日。

    - ``data/raw/per_asin/<ASIN>/amazon.json`` (週次 / 記事巡回レーン)
        ``{"asin": ..., "fetched_at": ISO,
           "item": {"price": 3149, "savings_percentage": 0,
                    "availability": "在庫あり。", ...}}``
        旧スナップショットは item 相当の dict がルート直下にあることがある
        (build_post._load_per_asin_amazon と同じく両形式を許容する)。

  「price_watch 優先 → per_asin フォールバック」の優先順は build_discounts.py
  (#3332 OFF バッジ) で既に実装・稼働していた。本モジュールはそれを価格・在庫・
  割引率の解決として一般化し、build_post.py / build_feature_lists.py /
  build_category_hubs.py から共用する (= 記事ページ・/deals/・/cospa/・
  年齢/テーマ hub がすべて同じ観測を出す)。

  鮮度ガードは 2 レーンで別値を持つ (build_discounts と同値):
  price_watch は日次なので 3 日、per_asin は週次巡回なので 21 日。

Pure aggregation only — 外部 API 呼び出しなし・CLI なし。
"""
from __future__ import annotations

import json
import logging
import pathlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger("price_overlay")

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_DEFAULT_LATEST = _REPO_ROOT / "data" / "price_watch" / "latest.json"
_DEFAULT_PER_ASIN_ROOT = _REPO_ROOT / "data" / "raw" / "per_asin"

# ------------------------------------------------------------------------
# 較正可能なしきい値 (build_discounts._WATCH_STALE_DAYS / _STALE_DAYS と同値)
# ------------------------------------------------------------------------
DEFAULT_WATCH_STALE_DAYS = 3       # price_watch: 日次レーンなので短いガード
DEFAULT_PER_ASIN_STALE_DAYS = 21   # per_asin: 週次巡回なので緩いガード


# ------------------------------------------------------------------------
# 在庫状態の判定 (SSOT)
# ------------------------------------------------------------------------
# 「明確に購入不可」と読める avail 文字列の部分一致マーカー。
# 「在庫あり。」「残りN点 ご注文はお早めに」「残りN点（入荷予定あり）」
# 「通常N〜N日以内に発送します。」等はここに該当しないので購入可のまま残る。
#
# build_price_dashboard (/price/) が持っていたものをここへ移した (#5130 残件3)。
# 同じ判定を build_feature_lists (/deals/ /cospa/) でも使うため、在庫の意味を
# 決める場所は 1 つにする。availability は PriceObservation が既に運んでいるので、
# 観測を読む側であるこのモジュールが持つのが素直。
UNAVAILABLE_MARKERS = (
    "在庫切れ",
    "入荷未定",
    "取り扱いできません",
)


def is_explicitly_unavailable(avail: Any) -> bool:
    """avail が「購入不可」と**明示している**なら True。

    None・空文字は False (= 判定しない)。「観測が無い」ことと「在庫が無いと
    観測した」ことは別で、前者を購入不可に倒すと観測の無い記事が一覧から
    まとめて消える。観測を持たない ASIN が 183 件ある (2026-08-18 実測) ので
    実害が出る。

    /price/ (build_price_dashboard) だけは None も購入不可に倒している。
    あちらの入力は latest.json の観測レコードそのもので、「観測はあるが avail が
    無い」= 取得に失敗した観測、と読めるため。入力の意味が違うので判定も分ける。
    """
    if not isinstance(avail, str) or not avail.strip():
        return False
    return any(marker in avail for marker in UNAVAILABLE_MARKERS)


@dataclass(frozen=True)
class PriceObservation:
    """1 ASIN 分の「現在価格」観測点。

    ``price`` は int > 0 のみ (取れなければ None)。``savings_percentage`` は
    0..100 で、**0 は「割引なし」の有効値**として保持し None (不明) と区別する。
    ``observed_at`` は採用した観測の生 ISO 文字列 (price_watch の ``ts`` /
    per_asin の ``fetched_at``) で、表示上の「価格情報取得日」に使う。
    """

    asin: str
    price: int | None
    savings_percentage: int | None
    availability: str | None
    observed_at: str | None
    source: str  # "price_watch" | "per_asin"


def _parse_iso8601(value: Any) -> Optional[datetime]:
    """build_discounts._parse_iso8601 と同一挙動 (naive は UTC とみなす)。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _load_json(path: pathlib.Path | str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"skip unreadable/invalid json {path}: {e}")
        return None
    return d if isinstance(d, dict) else None


def _int_or_none(value: Any, *, minimum: int = 1, maximum: int | None = None) -> int | None:
    """int > 0 系の値を取り出す。bool は int のサブクラスなので明示的に弾く。

    ``True`` が価格 1 円 / 割引 1% として採択されてはならない
    (build_discounts と同じ流儀)。
    """
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    if value < minimum:
        return None
    if maximum is not None and value > maximum:
        return None
    return value


def _str_or_none(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    v = value.strip()
    return v or None


def _fresh(observed_raw: Any, *, stale_days: int, now: datetime) -> bool:
    """観測時刻が鮮度ガード内かを返す。

    **unknown を pass に潰さない**: 欠落・非 str・パース不能はすべて False。
    「日付が読めないから新鮮扱い」は事故のもとなので採択しない。
    """
    observed = _parse_iso8601(observed_raw)
    if observed is None:
        return False
    return observed >= now - timedelta(days=stale_days)


def _now(now: Optional[datetime]) -> datetime:
    return now if now is not None else datetime.now(timezone.utc)


def load_watch_index(
    latest_path: pathlib.Path | str | None = None,
    *,
    stale_days: int = DEFAULT_WATCH_STALE_DAYS,
    now: Optional[datetime] = None,
) -> dict[str, PriceObservation]:
    """``data/price_watch/latest.json`` を 1 回読んで ASIN -> 観測の index を返す。

    呼び出し側 (build_post.py など) は記事を 1 件ずつ回すので、index を作って
    使い回す設計。latest.json が無い/壊れている場合は空 dict を返し、
    per_asin フォールバックのみで graceful に動作させる。
    """
    path = _DEFAULT_LATEST if latest_path is None else latest_path
    now = _now(now)

    d = _load_json(path)
    if d is None:
        return {}
    raw_items = d.get("items")
    if not isinstance(raw_items, dict):
        return {}

    index: dict[str, PriceObservation] = {}
    for asin, entry in raw_items.items():
        if not isinstance(asin, str) or not asin.strip():
            continue
        if not isinstance(entry, dict):
            continue

        price = _int_or_none(entry.get("p"))
        if price is None:
            # 価格が取れない観測は使わない (在庫だけ入れても表示に使えない)。
            continue

        ts_raw = entry.get("ts")
        if not _fresh(ts_raw, stale_days=stale_days, now=now):
            continue

        index[asin.strip().upper()] = PriceObservation(
            asin=asin.strip().upper(),
            price=price,
            # off は 0 のときキー自体が無い = 「割引なし」。範囲外は不明扱い。
            savings_percentage=_int_or_none(entry.get("off"), minimum=0, maximum=100),
            availability=_str_or_none(entry.get("avail")),
            observed_at=ts_raw if isinstance(ts_raw, str) else None,
            source="price_watch",
        )

    return index


def load_per_asin_observation(
    per_asin_root: pathlib.Path | str | None,
    asin: str,
    *,
    stale_days: int = DEFAULT_PER_ASIN_STALE_DAYS,
    now: Optional[datetime] = None,
) -> PriceObservation | None:
    """``data/raw/per_asin/<ASIN>/amazon.json`` から 1 件の観測を読む。

    週次レーン由来。price が取れない / 鮮度ガード外 / 読めない場合は None。
    """
    if not isinstance(asin, str) or not asin.strip():
        return None
    asin = asin.strip().upper()
    root = _DEFAULT_PER_ASIN_ROOT if per_asin_root is None else pathlib.Path(per_asin_root)
    now = _now(now)

    d = _load_json(root / asin / "amazon.json")
    if d is None:
        return None

    fetched_raw = d.get("fetched_at")
    if not _fresh(fetched_raw, stale_days=stale_days, now=now):
        return None

    # 旧スナップショットは item 相当が root 直下にある (build_post と同じ許容)。
    item = d.get("item") if isinstance(d.get("item"), dict) else d
    if not isinstance(item, dict):
        return None

    price = _int_or_none(item.get("price"))
    if price is None:
        return None

    return PriceObservation(
        asin=asin,
        price=price,
        savings_percentage=_int_or_none(item.get("savings_percentage"), minimum=0, maximum=100),
        availability=_str_or_none(item.get("availability")),
        observed_at=fetched_raw if isinstance(fetched_raw, str) else None,
        source="per_asin",
    )


def resolve(
    asin: str,
    *,
    watch_index: dict[str, PriceObservation] | None = None,
    per_asin_root: pathlib.Path | str | None = None,
    stale_days: int = DEFAULT_PER_ASIN_STALE_DAYS,
    now: Optional[datetime] = None,
) -> PriceObservation | None:
    """price_watch (日次) 優先 → per_asin (週次) フォールバックで観測を決める。

    どちらも採択できなければ None を返す (呼び出し側は記事 JSON の値を
    そのまま使う = 現行挙動のまま fail-soft)。
    """
    if not isinstance(asin, str) or not asin.strip():
        return None
    key = asin.strip().upper()

    if watch_index:
        hit = watch_index.get(key)
        if hit is not None:
            return hit

    return load_per_asin_observation(per_asin_root, key, stale_days=stale_days, now=now)


def apply_to_amazon_entry(amazon: dict, obs: PriceObservation | None) -> bool:
    """``product["prices"]["amazon"]`` 相当の dict に観測を反映する。

    現行の build_post._backfill_amazon_badges は「既存値が勝つ」ため価格が
    凍結していた (#4007 の根因)。ここでは **新しい観測が勝つ**。ただし観測側が
    None (不明) のフィールドで既存値を消すことはしない。

    併せて ``price_source`` / ``price_observed_at`` を残し、後段が「価格情報
    取得日」を実際に採用したソースの時刻で表示できるようにする
    (新鮮な日付の隣に凍結価格を出す矛盾表示を防ぐ)。

    値が実際に変わったら True を返す。
    """
    if obs is None or not isinstance(amazon, dict):
        return False

    changed = False

    def _set(key: str, value: Any) -> None:
        nonlocal changed
        if amazon.get(key) != value:
            amazon[key] = value
            changed = True

    if obs.price is not None:
        _set("price", obs.price)
    if obs.availability is not None:
        _set("availability", obs.availability)
    if obs.savings_percentage is not None:
        _set("savings_percentage", obs.savings_percentage)

    _set("price_source", obs.source)
    _set("price_observed_at", obs.observed_at)

    return changed
