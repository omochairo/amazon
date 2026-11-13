"""audit_score_claims.py — 全記事の「点数と本文の食い違い」を数える (navi-brain#165 T1)。

build が描画する記事 (ASIN ごとに最新の本文) について、build と同じ計算の点と
narrative を score_claims で照らし、食い違いの件数と一覧を出す。
build 時の closing の置き換え (score_claims.replace_low_score_closing) で消える分と、
置き換えたあとも残る分を分けて数える。

読み取りだけで、記事は書き換えない。

CLI:
    python scripts/audit_score_claims.py                       # 件数と一覧を stdout
    python scripts/audit_score_claims.py --json out.json       # 一覧を JSON でも書く
    python scripts/audit_score_claims.py --summary-only        # 件数だけ
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import score_claims  # noqa: E402

_PRIMARY_RE = re.compile(r"-(B0[A-Z0-9]{8})$")
_SIDECAR = (".enrichment", ".seo", ".quality")


def newest_articles(src: pathlib.Path) -> list[pathlib.Path]:
    """build_post._winning_stems と同じ規則で、ASIN ごとに最新の本文を選ぶ。"""
    newest: dict[str, pathlib.Path] = {}
    passthrough: list[pathlib.Path] = []
    for f in src.glob("*.json"):
        if f.stem.endswith(_SIDECAR):
            continue
        m = _PRIMARY_RE.search(f.stem)
        if not m:
            passthrough.append(f)
            continue
        asin = m.group(1)
        if asin not in newest or f.stem > newest[asin].stem:
            newest[asin] = f
    return sorted(list(newest.values()) + passthrough)


def audit(src: pathlib.Path) -> dict:
    rows = []
    totals = collections.Counter()
    labels = collections.Counter()
    for path in newest_articles(src):
        try:
            article = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            totals["unreadable"] += 1
            continue
        totals["articles"] += 1
        try:
            view = score_claims.compute_view(article)
        except Exception:  # noqa: BLE001 — 1 記事の失敗で集計を止めない
            totals["score_error"] += 1
            continue
        if view is None:
            totals["no_score"] += 1
            continue
        if view.safety <= score_claims.SAFETY_CLAIM_MAX_AXIS:
            totals["low_safety"] += 1
        if view.total_100 <= score_claims.LOW_TOTAL_MAX:
            totals["low_total"] += 1

        before = score_claims.find_claims(article, view)
        replaced = score_claims.replace_low_score_closing(article, view)
        after = score_claims.find_claims(article, view)
        if not before:
            continue
        kinds_before = {h.kind for h in before}
        kinds_after = {h.kind for h in after}
        for k in kinds_before:
            totals[f"{k}:before"] += 1
        for k in kinds_after:
            totals[f"{k}:after"] += 1
        totals["any:before"] += 1
        if after:
            totals["any:after"] += 1
        if replaced:
            totals["closing_replaced_articles"] += 1
        for h in after:
            labels[(h.kind, h.label, h.section)] += 1
        rows.append({
            "slug": article.get("slug") or path.stem,
            "total_100": view.total_100,
            "safety": view.safety,
            "replaced": replaced,
            "remaining": [
                {"kind": h.kind, "label": h.label, "section": h.section,
                 "phrase": h.phrase, "sentence": h.sentence}
                for h in after
            ],
        })
    return {"totals": dict(totals), "labels": labels, "rows": rows}


def _print_summary(result: dict) -> None:
    t = collections.Counter(result["totals"])
    print(f"articles (ASIN ごとに最新): {t['articles']}  点なし (ブランド無し): {t['no_score']}"
          f"  計算失敗: {t['score_error']}  読めない: {t['unreadable']}")
    print(f"安全性 <= {score_claims.SAFETY_CLAIM_MAX_AXIS}: {t['low_safety']} 件"
          f" / 総合 <= {score_claims.LOW_TOTAL_MAX}: {t['low_total']} 件")
    print("食い違いのある記事 (置き換え前 -> build 時の置き換え後):")
    for k, name in (("safety_claim", "(a) 安全性の言い切り"),
                    ("recommend_claim", "(b) 強い推奨の定型句"),
                    ("any", "合計 (重複を除く)")):
        print(f"  {name}: {t[k + ':before']} -> {t[k + ':after']}")
    print(f"  closing を置き換えた記事: {t['closing_replaced_articles']}")
    print("残る言い切り (種類, ラベル, セクション): 件数")
    for (kind, label, section), n in sorted(result["labels"].items(), key=lambda x: -x[1]):
        print(f"  {kind} {label} {section}: {n}")


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--src", default="data/articles/")
    ap.add_argument("--json", dest="json_out", help="一覧を書き出す JSON のパス")
    ap.add_argument("--summary-only", action="store_true", help="件数だけ出す")
    args = ap.parse_args(argv)

    result = audit(pathlib.Path(args.src))
    _print_summary(result)
    if not args.summary_only:
        print()
        for r in result["rows"]:
            if not r["remaining"]:
                continue
            for h in r["remaining"]:
                print(f"{r['slug']}\t総合{r['total_100']}\t安全性{r['safety']}\t"
                      f"{h['kind']}\t{h['section']}\t{h['phrase']}\t{h['sentence']}")
    if args.json_out:
        out = {
            "totals": result["totals"],
            "labels": [
                {"kind": k, "label": label, "section": s, "count": n}
                for (k, label, s), n in sorted(result["labels"].items(), key=lambda x: -x[1])
            ],
            "rows": result["rows"],
        }
        pathlib.Path(args.json_out).write_text(
            json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
