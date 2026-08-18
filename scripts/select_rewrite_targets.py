#!/usr/bin/env python3
"""Select articles to rewrite, ordered by prompt generation + slug date asc.

Used by .github/workflows/12-rewrite-idle-fill.yml to fill idle Jules slots
with rewrites of low-quality / old-prompt articles when new-ASIN fetch supply
is low (Issue #812).

Priority key (lower = higher priority):
1. pre-v7 (slug date < ``quality_gate.HOW_TO_CHOOSE_ENFORCE_FROM``) before post-v7.
   「古いプロンプトで書かれた記事ほどリライトの価値が高い」という #812 の意図を、
   実在するシグナル (施行日) で表す。
2. slug date ascending (older first) within each generation.

2026-08-09 に ``total_score`` 昇順をやめた理由 (実データ 1,903 記事の実測):

  - quality_gate の total_score は min=92 / p50=97 / max=99 で、20 check 中 19 が
    コーパス全体で一度も失敗していない。既定の ``--min-score 60`` は分布の最小値より
    32 点低く、閾値として何も選別していない。順序付けの材料にできる分散が無い。
  - ``<slug>.quality.json`` sidecar は 2026-05-28 を最後に 1 件も生成されていない
    (1,903 記事中 233 件のみ保持。04-validate は mktemp した一時ディレクトリに書いて
    捨てており、commit される経路が無い)。
  - 結果として「sidecar 欠落 → -1 で最優先」の規則が実際に並べていたのは品質ではなく
    **sidecar 生成が止まった日**だった。sidecar を持つ 233 件 (2026-05-14〜05-28 =
    サイト最古の pre-v7 記事) が、より新しい 1,669 件 (post-v7 を含む) の**後ろ**に
    回されていた。実測ペース 12 件/日 では約 140 日後にしか着手されない。

``total_score`` / ``passed`` は観察用に引き続き読み、stderr のログに出す。

Excludes ASINs that are already being processed: jules-lock branches + open PR
titles. The exclude-file is produced by the workflow with a `gh api` call and
passed in here as a flat text file.
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import sys
from typing import Iterable

# v7 施行日は quality_gate を単一情報源とする (audit_uniqueness.cohort_for_slug と
# 同じ定数を見ることで pre/post v7 の線引きが 2 箇所でずれないようにする)。
from quality_gate import HOW_TO_CHOOSE_ENFORCE_FROM
# #5490: 生成側 (03-invoke-jules) が defer する ASIN を選ばないための適格性判定。
# 判定は rewrite_queue が SSOT (詳細は rewrite_queue.is_generatable の docstring)。
import rewrite_queue
from rewrite_queue import is_generatable, load_markers

_ASIN_RE = re.compile(r"(B0[A-Z0-9]{8})")
_SLUG_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-(B0[A-Z0-9]{8})$")
_SIDECAR_SUFFIXES = (".quality.json", ".enrichment.json", ".seo.json")


def collect_candidates(articles_dir: str) -> list[dict]:
    """Return primary article records as {slug, asin, date, score, passed}.

    Skips sidecar JSONs and any file whose slug doesn't match YYYY-MM-DD-ASIN.
    """
    out: list[dict] = []
    seen_asin: set[str] = set()
    for path in sorted(glob.glob(os.path.join(articles_dir, "*.json"))):
        base = os.path.basename(path)
        if base.endswith(_SIDECAR_SUFFIXES):
            continue
        slug = base[:-5]
        m = _SLUG_RE.match(slug)
        if not m:
            continue
        date, asin = m.group(1), m.group(2)
        # If two article files reference the same ASIN (shouldn't happen but
        # be defensive: a stale rewrite + fresh one could overlap), keep the
        # older one — the rewrite is meant to replace it.
        if asin in seen_asin:
            continue
        seen_asin.add(asin)
        # #4826 項目4: 旧 <slug>.quality.json sidecar の読み取りを外した。
        # sidecar の生成は quality_gate 側で廃止済み (main 全量の品質は
        # 48-quality-census.yml が集計 JSON 1 本で観測する) で、リポジトリに
        # 実体も 1 件も残っていない。読んでも必ず (None, None) が返るだけの
        # 経路だったので、選定ロジックからも消す。リライト優先度が sidecar に
        # 依存しないことは #4822 で既に確認済み。
        out.append({
            "slug": slug,
            "asin": asin,
            "date": date,
        })
    return out


def _read_exclude(path: str) -> set[str]:
    if not path or not os.path.exists(path):
        return set()
    out: set[str] = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            for m in _ASIN_RE.finditer(line):
                out.add(m.group(1))
    return out


def select(
    candidates: Iterable[dict],
    excluded: set[str],
    limit: int,
    generatable=None,
) -> tuple[list[dict], list[str]]:
    """Sort by (pre-v7 first, date asc) and return ``(picked, deferred)``.

    ``total_score`` は順序付けに使わない (理由はモジュール docstring)。日付が読めない
    候補は安全側で post-v7 扱いにする (quality_gate._how_to_choose_enforced と同じ方針)。

    #5490: **生成側が defer する ASIN は選ばない。** `03-invoke-jules` は
    band=zero / unfetched を生成対象から外すので、それを選ぶとマーカーだけが
    永久に残り、次の選定でも同じ ASIN が先頭に来る (実測で最古 56 日・中央値
    30 日の滞留)。判定は rewrite_queue.is_generatable が SSOT。

    除外した ASIN は捨てずに第 2 戻り値で返す。**黙って落とすと「選ばれないから
    気付かない」状態になる**ので、呼び出し側がログに出せるようにしておく
    (#4789 の「鳴っていない = 健全とは読めない」と同じ)。
    """
    def key(c: dict) -> tuple[int, str]:
        date = c.get("date") or ""
        generation = 0 if date and date < HOW_TO_CHOOSE_ENFORCE_FROM else 1
        return (generation, date)

    available = [c for c in candidates if c["asin"] not in excluded]
    available.sort(key=key)

    # 既定は実データの band 判定。テストは per_asin ディレクトリを作らずに
    # 順序だけを確かめたいので差し替えられるようにしておく (per_asin が無い ASIN は
    # band=unfetched = defer 対象になり、順序のテストが書けなくなるため)。
    check = generatable if generatable is not None else is_generatable

    picked: list[dict] = []
    deferred: list[str] = []
    for c in available:
        if len(picked) >= max(limit, 0):
            break
        if check(c["asin"]):
            picked.append(c)
        else:
            deferred.append(c["asin"])
    return picked, deferred


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--articles-dir", default="data/articles")
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument(
        "--exclude-file",
        default="",
        help=(
            "Path to a text file with excluded ASINs (one per line, or any text "
            "containing B0[A-Z0-9]{8}). Caller populates this from open PR titles "
            "+ jules-lock/<ASIN> branches."
        ),
    )
    ap.add_argument(
        "--out",
        default="",
        help="Write CSV (one ASIN per line) to this path. Default: stdout.",
    )
    ap.add_argument(
        "--queue-dir",
        default=rewrite_queue.QUEUE_DIR,
        help="#5490: 既に依頼済み (マーカーあり) の ASIN を選び直さないために読む。",
    )
    args = ap.parse_args()

    candidates = collect_candidates(args.articles_dir)
    excluded = _read_exclude(args.exclude_file)
    # #5490 対処D: 既にマーカーがある ASIN は「依頼済みで生成待ち」なので選び直さない。
    #
    # 呼び出し側が渡す exclude は open PR タイトル + lock ブランチだけで、idle-fill の
    # PR がマージされた瞬間に外れる。一方マーカーは生成が着地するまで残るので、
    # 従来は **同じ ASIN を半日ごとに prepend し直す** 形になっていた。実際 #5358 と
    # #5428 は対象 12 件が完全一致し、後者は日次 fetch の書き換えとコンフリクトして
    # 止まっていた。
    pending = set(load_markers(args.queue_dir))
    if pending:
        excluded = excluded | pending
    picked, deferred = select(candidates, excluded, args.limit)

    body = "\n".join(c["asin"] for c in picked)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(body + ("\n" if body else ""))
    else:
        if body:
            print(body)

    print(
        f"[select_rewrite_targets] candidates={len(candidates)} "
        f"excluded={len(excluded)} pending_markers={len(pending)} "
        f"deferred={len(deferred)} "
        f"picked={len(picked)} limit={args.limit}",
        file=sys.stderr,
    )
    for c in picked:
        print(f"  -> {c['asin']} slug={c['slug']}", file=sys.stderr)
    if deferred:
        # #5490: 黙って落とさない。ここに出続ける ASIN は「素材が無くてリライト
        # できない記事」であり、放置すると古いまま配信され続ける (対処は #5490 案B の
        # 収集レーン)。件数が増え続けるなら、それ自体が別の問題の信号になる。
        print(
            f"  deferred (band=zero/unfetched, 生成側が見送るので選ばない): "
            f"{', '.join(deferred)}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
