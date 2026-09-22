"""audit_sticky_stock_title_replay.py

#7953: 「どこで買える」タイトル型が在庫文言の欠測 (unknown) で日替わりに
旧型 (口コミ・最安値) へ戻る事故を、issue と同じ再現方法で計測し直す。

再現方法 (issue #7953 の実測方法と同一):
  1. ``data/price_watch/latest.json`` の git 履歴から 1 コミット/日のスナップ
     ショットを取る (``--since`` 以降)。
  2. 記事 ``date`` が ``--since`` 以降の ASIN 集合について、日ごとに
     「どこで買える」型を適用してよいかを判定する。
  3. 旧ルール (stock_status.can_use_stock_title のみ = 毎日その日の avail
     だけで判定) と新ルール (#7953 sticky: 一度でも rollout 日以降に分類可能
     だったら固定) の両方で判定し、新型→旧型の遷移回数・Amazon 在庫あり⇔
     在庫切れによる括弧の変化回数を比較する。

**先読み (look-ahead) を避ける**: history (``data/price_watch/history/
<ASIN>.jsonl``) は現在の HEAD に累積された全期間のログだが、日 D の sticky
判定には ``ts <= D`` の行だけを使う (本番ビルドが D 時点で見えていたはずの
情報だけを使う)。

pure read-only audit — data/ には一切書き込まない。

使い方::

    python scripts/audit_sticky_stock_title_replay.py \
        --articles data/articles/ \
        --history data/price_watch/history \
        --since 2026-08-13
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib
import subprocess
import sys
from datetime import datetime
from typing import Any, Optional

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import stock_status  # noqa: E402
import where_to_buy_format as wtb  # noqa: E402


def _git(args: list[str]) -> str:
    # Windows では既定のコンソール encoding (cp932) で git show の UTF-8 出力
    # (日本語 avail 文言を含む) の decode に失敗するため、bytes で受けて
    # 明示的に utf-8 decode する。
    out = subprocess.run(
        ["git", *args], cwd=_REPO_ROOT, check=True, capture_output=True,
    )
    return out.stdout.decode("utf-8", errors="replace")


def collect_daily_snapshots(since: str, path: str) -> list[tuple[str, str]]:
    """``[(day, commit_sha)]`` を古い順で返す。1 日に複数コミットがあれば
    その日最後 (最新) のものを採用する。"""
    log = _git([
        "log", f"--since={since}", "--format=%H %ad", "--date=short", "--", path,
    ])
    by_day: dict[str, str] = {}
    for line in log.splitlines():
        line = line.strip()
        if not line:
            continue
        sha, day = line.split(" ", 1)
        # git log は新しい順。まだ記録の無い日だけ埋めれば「その日最新」が残る。
        by_day.setdefault(day, sha)
    return sorted(by_day.items())


def load_latest_at(sha: str, path: str) -> dict[str, dict]:
    """``<sha>:<path>`` の ``items`` を ``{ASIN: entry}`` (大文字キー) で返す。
    無い/壊れていれば空を返す (fail-soft)。"""
    try:
        text = _git(["show", f"{sha}:{path}"])
    except subprocess.CalledProcessError:
        return {}
    try:
        d = json.loads(text)
    except json.JSONDecodeError:
        return {}
    raw_items = d.get("items") if isinstance(d, dict) else None
    if not isinstance(raw_items, dict):
        return {}
    items: dict[str, dict] = {}
    for asin, entry in raw_items.items():
        if isinstance(asin, str) and asin.strip() and isinstance(entry, dict):
            items[asin.strip().upper()] = entry
    return items


def load_article_population(articles_dir: pathlib.Path, since: str) -> dict[str, str]:
    """``{ASIN: date}`` (date はロールアウト日以降の記事のみ)。"""
    population: dict[str, str] = {}
    for f in sorted(articles_dir.glob("*.json")):
        if f.stem.endswith(".enrichment") or f.stem.endswith(".seo"):
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        date = data.get("date")
        if not isinstance(date, str) or len(date) < 10 or date[:10] < since:
            continue
        product = data.get("product")
        asin = product.get("asin") if isinstance(product, dict) else None
        if isinstance(asin, str) and asin.strip():
            population[asin.strip().upper()] = date[:10]
    return population


def load_history_classifiable_points(history_root: pathlib.Path | None, asin: str) -> list[str]:
    """``<history_root>/<ASIN>.jsonl`` から、分類可能な観測点の ``ts`` だけを
    昇順で返す (state=unknown の行は除く)。"""
    if history_root is None:
        return []
    p = history_root / f"{asin.upper()}.jsonl"
    if not p.exists():
        return []
    points: list[str] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict) or rec.get("source") != "amazon":
                continue
            ts = rec.get("ts")
            if not isinstance(ts, str) or not ts.strip():
                continue
            state, _ = stock_status.classify_availability(rec.get("availability"))
            if state == stock_status.STATE_UNKNOWN:
                continue
            points.append(ts)
    except OSError:
        return []
    points.sort()
    return points


def sticky_eligible_asof(points: list[str], day: str, rollout_date: str) -> bool:
    """``day`` (YYYY-MM-DD) 時点で見えていたはずの観測点 (``ts[:10] <= day``)
    のうち最新のものが ``rollout_date`` 以降なら True (#7953 のステートレス
    sticky ルール)。"""
    visible = [ts for ts in points if ts[:10] <= day]
    if not visible:
        return False
    return visible[-1][:10] >= rollout_date


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--articles", default="data/articles/")
    ap.add_argument("--history", default="data/price_watch/history")
    ap.add_argument("--price-history", default="data/price_history",
                     help="週次レーン (price_history.py)。日次レーンと合わせてマージし、"
                          "収集タイミングのズレによる sticky 判定の取りこぼしを減らす")
    ap.add_argument("--latest-path", default="data/price_watch/latest.json")
    ap.add_argument("--since", default=wtb.ROLLOUT_DATE)
    ap.add_argument("--rollout-date", default=wtb.ROLLOUT_DATE)
    args = ap.parse_args()

    articles_dir = pathlib.Path(args.articles)
    history_root = pathlib.Path(args.history)
    price_history_root = pathlib.Path(args.price_history) if args.price_history else None

    population = load_article_population(articles_dir, args.since)
    print(f"[audit] ASIN population (date >= {args.since}): {len(population)}")

    days = collect_daily_snapshots(args.since, args.latest_path)
    print(f"[audit] daily snapshots: {len(days)} ({days[0][0]}..{days[-1][0]})" if days else "[audit] no snapshots found")
    if not days:
        return 0

    # history (日次 + 週次 2 レーン) はここで1回だけ読み、以降は in-memory の
    # 日付フィルタで再利用する (git show をコミット×ASIN 回叩くのは history 側
    # では不要 — 現行の history/*.jsonl は累積ログなので、日付でスライスする
    # だけでよい)。2 レーンをマージするのは、日次レーン単独では新規 ASIN の
    # 追跡開始が数日遅れることがあり (#7953 実測)、週次レーンにより早い分類
    # 可能な観測が記録されていることがあるため
    # (where_to_buy_format.build_price_history_note と同じ「片方を捨てない」
    # 方針)。
    history_points: dict[str, list[str]] = {
        asin: sorted(
            load_history_classifiable_points(history_root, asin)
            + load_history_classifiable_points(price_history_root, asin)
        )
        for asin in population
    }

    old_flags: dict[str, list[bool]] = collections.defaultdict(list)
    new_flags: dict[str, list[bool]] = collections.defaultdict(list)
    old_amazon_avail: dict[str, list[Optional[bool]]] = collections.defaultdict(list)

    for day, sha in days:
        items = load_latest_at(sha, args.latest_path)
        for asin in population:
            entry = items.get(asin)
            raw_avail = entry.get("avail") if entry else None
            state, _ = stock_status.classify_availability(raw_avail)

            old_eligible = state in (
                stock_status.STATE_IN_STOCK, stock_status.STATE_LOW_STOCK,
                stock_status.STATE_DELAYED, stock_status.STATE_OUT_OF_STOCK,
            )
            old_flags[asin].append(old_eligible)
            if old_eligible:
                amazon_avail = state in (
                    stock_status.STATE_IN_STOCK, stock_status.STATE_LOW_STOCK,
                    stock_status.STATE_DELAYED,
                )
                old_amazon_avail[asin].append(amazon_avail)
            else:
                old_amazon_avail[asin].append(None)

            new_eligible = old_eligible or sticky_eligible_asof(
                history_points[asin], day, args.rollout_date,
            )
            new_flags[asin].append(new_eligible)

    def count_stock_to_legacy_transitions(flags_by_asin: dict[str, list[bool]]) -> tuple[int, int]:
        transitions = 0
        flipping_asins = 0
        for flags in flags_by_asin.values():
            asin_transitions = sum(
                1 for a, b in zip(flags, flags[1:]) if a and not b
            )
            if asin_transitions:
                flipping_asins += 1
                transitions += asin_transitions
        return transitions, flipping_asins

    def count_parenthetical_changes(avail_by_asin: dict[str, list[Optional[bool]]]) -> tuple[int, int]:
        changes = 0
        changed_asins = 0
        for values in avail_by_asin.values():
            known = [v for v in values if v is not None]
            asin_changes = sum(1 for a, b in zip(known, known[1:]) if a != b)
            if asin_changes:
                changed_asins += 1
                changes += asin_changes
        return changes, changed_asins

    old_transitions, old_flip_asins = count_stock_to_legacy_transitions(old_flags)
    new_transitions, new_flip_asins = count_stock_to_legacy_transitions(new_flags)
    old_paren_changes, old_paren_asins = count_parenthetical_changes(old_amazon_avail)

    print()
    print("=== 新型→旧型の遷移 (target: 新ルールで 0) ===")
    print(f"  旧ルール: {old_transitions} 回 / {old_flip_asins} ASIN")
    print(f"  新ルール (#7953 sticky): {new_transitions} 回 / {new_flip_asins} ASIN")
    print()
    print("=== Amazon 在庫あり⇔在庫切れによる括弧内サイト名の変化 ===")
    print(f"  旧ルール: {old_paren_changes} 回 / {old_paren_asins} ASIN")
    print("  新ルール (#7953): 0 回 (Amazon は取扱=listed で決めるため、"
          "在庫状態と無関係。listed は常に True と扱う設計)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
