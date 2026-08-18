#!/usr/bin/env python3
"""Rewrite-request queue: decouple "covered" from "needs rewrite" (Issue #2711).

The original idle-fill (#812) requested a rewrite by *deleting* the article body
so 03-invoke-jules pick-asin (which excludes ASINs that already have a body)
would re-pick it. That coupling made deletion the only way to request a rewrite,
so any silently-failed regeneration (Jules quota, amazon.json daily overwrite,
zero-defer #1600, shuffle starvation) left the page permanently 404 -- ~338
bodies were lost this way.

This module is the single source of truth for the decoupled design:

* A rewrite request is a marker file ``data/rewrite_queue/<ASIN>.json`` holding
  ``{asin, old_slug, requested_at}``. The body is NOT touched when the marker is
  written, so a failed regeneration never orphans the page.
* ``eligible_rewrite_asins`` tells pick-asin which *body-present* ASINs are still
  awaiting a fresh body (marker present AND no body newer than ``old_slug``).
  Once a newer-dated body lands the ASIN drops out automatically, so a lingering
  marker can never cause an infinite re-pick loop (self-healing invariant).
* ``cleanup_completed`` removes the stale old body + marker, but ONLY for markers
  whose rewrite already produced a newer body. The old body is therefore deleted
  strictly *after* the replacement exists -- "replace, don't pre-delete".

build_post.py separately renders only the newest body per ASIN, so the transient
"old + new" window never produces a duplicate ``/products/<asin>/`` URL and a
not-yet-regenerated ASIN keeps rendering its old body (no 404).
"""
from __future__ import annotations

import glob
import json
import os
import re
from datetime import datetime, timezone

QUEUE_DIR = "data/rewrite_queue"
_ASIN_RE = re.compile(r"^B0[A-Z0-9]{8}$")
_SLUG_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-(B0[A-Z0-9]{8})$")
_SIDECAR_SUFFIXES = (".quality.json", ".enrichment.json", ".seo.json")


def marker_path(asin: str, queue_dir: str = QUEUE_DIR) -> str:
    return os.path.join(queue_dir, f"{asin}.json")


def write_marker(
    asin: str,
    old_slug: str,
    queue_dir: str = QUEUE_DIR,
    requested_at: str | None = None,
) -> str:
    """Write a rewrite-request marker for ``asin``. Returns the marker path.

    Idempotent: re-writing keeps the original ``requested_at`` if a marker
    already exists (so retries don't reset the age of the request).
    """
    if not _ASIN_RE.match(asin):
        raise ValueError(f"Invalid ASIN: {asin!r}")
    os.makedirs(queue_dir, exist_ok=True)
    path = marker_path(asin, queue_dir)
    existing = _load_json(path)
    if requested_at is None:
        requested_at = (
            existing.get("requested_at")
            if isinstance(existing, dict) and existing.get("requested_at")
            else datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        )
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {"asin": asin, "old_slug": old_slug, "requested_at": requested_at},
            f,
            ensure_ascii=False,
            indent=2,
        )
    return path


def load_markers(queue_dir: str = QUEUE_DIR) -> dict[str, dict]:
    """Return ``{asin: marker_dict}`` for every valid marker in the queue."""
    out: dict[str, dict] = {}
    for path in glob.glob(os.path.join(queue_dir, "*.json")):
        asin = os.path.basename(path)[:-5]
        if not _ASIN_RE.match(asin):
            continue
        d = _load_json(path)
        if isinstance(d, dict):
            out[asin] = d
    return out


def _body_dates_for_asin(asin: str, articles_dir: str) -> list[str]:
    """Return sorted YYYY-MM-DD dates of primary (non-sidecar) bodies for asin."""
    dates: list[str] = []
    for path in glob.glob(os.path.join(articles_dir, f"*-{asin}.json")):
        base = os.path.basename(path)
        if base.endswith(_SIDECAR_SUFFIXES):
            continue
        m = _SLUG_RE.match(base[:-5])
        if m and m.group(2) == asin:
            dates.append(m.group(1))
    return sorted(dates)


def newest_body_slug(asin: str, articles_dir: str = "data/articles") -> str | None:
    """Return the slug of the newest-dated primary body for asin, or None."""
    dates = _body_dates_for_asin(asin, articles_dir)
    return f"{dates[-1]}-{asin}" if dates else None


def _old_date(old_slug: str) -> str:
    m = _SLUG_RE.match(old_slug)
    return m.group(1) if m else ""


def has_newer_body(asin: str, old_slug: str, articles_dir: str) -> bool:
    """True if a body dated strictly after ``old_slug``'s date exists for asin."""
    old = _old_date(old_slug)
    return any(d > old for d in _body_dates_for_asin(asin, articles_dir))


def eligible_rewrite_asins(
    articles_dir: str = "data/articles", queue_dir: str = QUEUE_DIR
) -> set[str]:
    """ASINs that pick-asin should keep eligible despite an existing body.

    = markers whose replacement body has NOT yet landed (still pending). Once a
    newer-dated body exists the ASIN is dropped, so a stale marker cannot cause
    repeated re-picks.
    """
    out: set[str] = set()
    for asin, marker in load_markers(queue_dir).items():
        old_slug = marker.get("old_slug", "") if isinstance(marker, dict) else ""
        if not has_newer_body(asin, old_slug, articles_dir):
            out.add(asin)
    return out


# #5490: 03-invoke-jules が生成を見送る band。invoke_jules_repoless.pick_candidates の
# `deferred_bands` と同じ値で、こちらが SSOT。
#
# band=="zero" は「非販売ソース 2 件必須 (v5 §6.5.1) を満たす素材が fetch 済みで
# 真にゼロ」= 生成しても品質ゲートに構造的不合格、という信号 (#1600 Phase 1)。
# "unfetched" は第三者収集そのものが未実行。どちらも生成側が defer する。
DEFERRED_BANDS = ("zero", "unfetched")


def is_generatable(asin: str) -> bool:
    """`03-invoke-jules` がこの ASIN の生成に進むか (#5490)。

    リライトの選定 (select_rewrite_targets) と生成の選定
    (invoke_jules_repoless.pick_candidates) が別々の適格性ルールを持っていたため、
    **生成できない ASIN をリライト対象に選び続ける**ループが起きていた:

      1. select_rewrite_targets が pre-v7 の最古から 12 件選ぶ
      2. idle-fill が prepend + マーカーを書き PR を作る
      3. pick_candidates が band=zero で defer → 生成されない
      4. マーカーが消えない → 1 に戻る (同じ 12 件)

    実測 (2026-08-18): キュー先頭の 12 件は band=zero が 11 件で、最古のマーカーは
    56 日・中央値 30 日ぶん滞留していた。判定をここに集約して両側から呼ぶ。

    スコアリングが失敗したら **True を返す** (判定できないことを理由に候補を
    減らさない)。生成側の defer は独立に効くので、取りこぼしても二重に落ちるだけ。
    """
    try:
        import score_per_asin_info as sc
        return sc.score_asin(asin).get("band") not in DEFERRED_BANDS
    except Exception:
        return True


def deferred_markers(queue_dir: str = QUEUE_DIR) -> list[str]:
    """マーカーはあるが生成側が defer する ASIN (= 消化されようがない) を返す。"""
    return sorted(a for a in load_markers(queue_dir) if not is_generatable(a))


def withdraw_deferred(queue_dir: str = QUEUE_DIR) -> list[str]:
    """defer 対象のマーカーを取り下げる (#5490 対処C)。

    `cleanup_completed` は「新しい本体が着地したとき」だけ消すので、生成されない
    ASIN のマーカーは永久に残る。取り下げても**本体は消さない** — リライトを
    諦めるだけで、記事は今のまま配信され続ける (`cleanup_completed` が旧本体を
    消すのは置き換えが着地した後だけ、という #2711 の不変条件は保つ)。

    素材が埋まって band が上がれば、次の選定で普通に選ばれ直す。
    """
    withdrawn = deferred_markers(queue_dir)
    for asin in withdrawn:
        path = marker_path(asin, queue_dir)
        if os.path.exists(path):
            os.remove(path)
    return withdrawn


def cleanup_completed(
    articles_dir: str = "data/articles", queue_dir: str = QUEUE_DIR
) -> tuple[int, int]:
    """Remove stale old body + marker for rewrites whose new body has landed.

    Returns ``(files_removed, markers_cleared)``. Deletion fires ONLY when a
    newer body exists, so the old body is never removed before its replacement.
    """
    files_removed = 0
    markers_cleared = 0
    for asin, marker in load_markers(queue_dir).items():
        old_slug = marker.get("old_slug", "") if isinstance(marker, dict) else ""
        if not old_slug or not has_newer_body(asin, old_slug, articles_dir):
            continue
        for suffix in (".json",) + _SIDECAR_SUFFIXES:
            stale = os.path.join(articles_dir, f"{old_slug}{suffix}")
            if os.path.exists(stale):
                os.remove(stale)
                files_removed += 1
        os.remove(marker_path(asin, queue_dir))
        markers_cleared += 1
    return files_removed, markers_cleared


def _load_json(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Rewrite-queue maintenance (#2711).")
    ap.add_argument("--articles-dir", default="data/articles")
    ap.add_argument("--queue-dir", default=QUEUE_DIR)
    ap.add_argument(
        "--cleanup",
        action="store_true",
        help="Remove stale old body + marker for rewrites whose new body landed. "
        "Safe: deletes only when a newer body already exists.",
    )
    ap.add_argument(
        "--withdraw-deferred",
        action="store_true",
        help="#5490: 生成側が defer する band (zero/unfetched) のマーカーを取り下げる。"
        "本体は消さない (リライトを諦めるだけで記事は今のまま配信される)。",
    )
    ap.add_argument(
        "--list-eligible",
        action="store_true",
        help="Print ASINs still awaiting a fresh body (one per line).",
    )
    args = ap.parse_args()
    if args.cleanup:
        files, markers = cleanup_completed(args.articles_dir, args.queue_dir)
        print(f"[rewrite_queue] cleanup removed_files={files} cleared_markers={markers}")
    if args.withdraw_deferred:
        withdrawn = withdraw_deferred(args.queue_dir)
        print(f"[rewrite_queue] withdrew_deferred={len(withdrawn)}")
        if withdrawn:
            # 黙って消さない。ここに出る ASIN は「素材が無くてリライトできない記事」で、
            # 放置すると古いまま配信され続ける (#5490 案B の収集レーンの対象)。
            print(f"  withdrawn: {', '.join(withdrawn)}")
    if args.list_eligible:
        for asin in sorted(eligible_rewrite_asins(args.articles_dir, args.queue_dir)):
            print(asin)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
