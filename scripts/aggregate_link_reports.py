"""Aggregate user-submitted link-report Issues into a re-search dispatch plan.

Issue #722: omochairo の記事カードに配備された 🚩 link-report ボタン (PR #720) で
集まる GitHub Issues (label=link-report) を週次で集計し、同一 (ASIN, EC site) の
通報が閾値 (default 2 件) を超えたら 10-re-search-low-confidence.yml を auto
dispatch するための入力ファイルを生成する。

出力:
  - data/link_report_counts.json (全体カウント + 閾値超え一覧 + Issue 番号)
  - data/raw/low_confidence_asins.txt (閾値超え ASIN を改行区切りで append-unique)

呼び出し形態:
  python scripts/aggregate_link_reports.py [--threshold N] [--issues-json FILE]

`--issues-json` 未指定時は `gh api repos/{REPO}/issues?labels=link-report&state=open`
を shell out する。CI では gh が App token で認証済前提。

EC site の正規化:
  Amazon            → amazon
  楽天市場          → rakuten
  Yahoo!ショッピング → yahoo

自動 re-search 対象は rakuten / yahoo のみ (10-re-search-low-confidence.yml が
fetch_cross_search のみを叩くため)。Amazon リンク誤りは ASIN 自体由来なので
別軸 (#722 Phase 4 検討) で扱う — 閾値超えとして flagged には残るが、
auto dispatch & auto close はしない。
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
LOW_CONF_TXT = DATA_DIR / "raw" / "low_confidence_asins.txt"
COUNTS_JSON = DATA_DIR / "link_report_counts.json"

DEFAULT_REPO = "omochairo/amazon"
DEFAULT_THRESHOLD = 2

EC_SITE_NORMALIZE = {
    "Amazon": "amazon",
    "楽天市場": "rakuten",
    "Yahoo!ショッピング": "yahoo",
}
RESEARCHABLE_SITES = {"rakuten", "yahoo"}

# Issue Forms はフィールド値を `### <Label>\n\n<value>` 形式で body に書く。
_HEADER_VALUE_RE = re.compile(
    r"^###\s+(?P<label>.+?)\s*\n+(?P<value>.+?)(?=\n###\s|\Z)",
    re.MULTILINE | re.DOTALL,
)
_ASIN_RE = re.compile(r"\b([A-Z0-9]{10})\b")


def parse_issue_body(body: str) -> dict:
    """Parse `### Label\n\nValue` blocks from Issue Forms output."""
    fields = {}
    for m in _HEADER_VALUE_RE.finditer(body or ""):
        fields[m.group("label").strip()] = m.group("value").strip()
    return fields


def extract_report(issue: dict) -> dict | None:
    """Return {asin, ec_site, issue_number} or None if unparseable."""
    fields = parse_issue_body(issue.get("body") or "")
    asin_raw = fields.get("商品 ASIN", "")
    ec_raw = fields.get("対象 EC サイト", "")
    if not asin_raw or not ec_raw:
        return None
    asin_match = _ASIN_RE.search(asin_raw.upper())
    if not asin_match:
        return None
    ec_norm = EC_SITE_NORMALIZE.get(ec_raw.strip())
    if not ec_norm:
        return None
    return {
        "asin": asin_match.group(1),
        "ec_site": ec_norm,
        "issue_number": issue.get("number"),
    }


def fetch_issues_via_gh(repo: str) -> list[dict]:
    """gh api でlink-report open issues を全件取得 (paginate).

    `gh api` の `-f key=value` は **body フィールド** として解釈され、
    GET ではなく POST にメソッドが切り替わってしまう (Issue 作成 API として
    `Invalid request. For 'properties/labels', "link-report" is not an array.`
    が返る)。query string はそのまま URL に embed するのが安全。
    """
    url = (
        f"repos/{repo}/issues"
        "?labels=link-report&state=open&per_page=100"
    )
    cmd = ["gh", "api", url, "--paginate"]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"gh api failed (exit {proc.returncode}): {proc.stderr.strip()[:500]}"
        )
    raw = proc.stdout.strip()
    if not raw:
        return []
    # `--paginate` は配列を連結して返すので、改行で分割せず JSON ストリームとして読む。
    decoder = json.JSONDecoder()
    issues: list[dict] = []
    idx = 0
    while idx < len(raw):
        # skip whitespace
        while idx < len(raw) and raw[idx].isspace():
            idx += 1
        if idx >= len(raw):
            break
        obj, end = decoder.raw_decode(raw, idx)
        if isinstance(obj, list):
            issues.extend(obj)
        idx = end
    # link-report-processed が付いている Issue は処理済として除外。
    return [
        i for i in issues
        if "pull_request" not in i
        and not any(
            (lbl.get("name") if isinstance(lbl, dict) else lbl) == "link-report-processed"
            for lbl in (i.get("labels") or [])
        )
    ]


def aggregate(issues: list[dict], threshold: int) -> dict:
    counts: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"count": 0, "issue_numbers": []}
    )
    skipped: list[int] = []
    for issue in issues:
        report = extract_report(issue)
        if report is None:
            n = issue.get("number")
            if isinstance(n, int):
                skipped.append(n)
            continue
        key = (report["asin"], report["ec_site"])
        counts[key]["count"] += 1
        counts[key]["issue_numbers"].append(report["issue_number"])

    flagged = [
        {
            "asin": asin,
            "ec_site": ec_site,
            "count": entry["count"],
            "issue_numbers": entry["issue_numbers"],
            "researchable": ec_site in RESEARCHABLE_SITES,
        }
        for (asin, ec_site), entry in counts.items()
        if entry["count"] >= threshold
    ]
    flagged.sort(key=lambda f: (-f["count"], f["asin"], f["ec_site"]))

    counts_serializable = {
        f"{asin}|{ec_site}": entry for (asin, ec_site), entry in counts.items()
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "threshold": threshold,
        "researchable_sites": sorted(RESEARCHABLE_SITES),
        "total_issues_scanned": len(issues),
        "skipped_unparseable_issue_numbers": sorted(skipped),
        "counts_by_asin_ec": counts_serializable,
        "flagged": flagged,
    }


def update_low_confidence_file(flagged: list[dict], path: pathlib.Path) -> int:
    """Append-unique flagged ASINs (researchable のみ) to a sorted line-delimited file.

    Returns the number of newly added ASINs.
    """
    asins = {f["asin"] for f in flagged if f.get("researchable")}
    if not asins:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: set[str] = set()
    if path.exists():
        existing = {
            line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
        }
    merged = existing | asins
    new_count = len(merged) - len(existing)
    path.write_text("\n".join(sorted(merged)) + "\n", encoding="utf-8")
    return new_count


def emit_github_output(result: dict) -> None:
    """Set workflow outputs (researchable_flag, processed_issue_numbers, summary)."""
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if not gh_out:
        return
    researchable = [f for f in result["flagged"] if f.get("researchable")]
    processed_numbers: list[int] = []
    for f in researchable:
        processed_numbers.extend(int(n) for n in f["issue_numbers"] if isinstance(n, int))
    with open(gh_out, "a", encoding="utf-8") as fh:
        fh.write(f"flagged_count={len(result['flagged'])}\n")
        fh.write(f"researchable_count={len(researchable)}\n")
        fh.write(f"processed_issue_numbers={','.join(str(n) for n in sorted(set(processed_numbers)))}\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD,
                    help="同一 (ASIN, EC site) で flagged 扱いにする最小通報数 (default: 2)")
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--issues-json", type=pathlib.Path, default=None,
                    help="ローカル/テスト用: gh API レスポンス JSON ファイルから読み込む")
    ap.add_argument("--counts-out", type=pathlib.Path, default=COUNTS_JSON)
    ap.add_argument("--low-confidence-out", type=pathlib.Path, default=LOW_CONF_TXT)
    args = ap.parse_args()

    if args.issues_json:
        issues = json.loads(args.issues_json.read_text(encoding="utf-8"))
    else:
        issues = fetch_issues_via_gh(args.repo)

    result = aggregate(issues, threshold=args.threshold)

    args.counts_out.parent.mkdir(parents=True, exist_ok=True)
    args.counts_out.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    added = update_low_confidence_file(result["flagged"], args.low_confidence_out)

    print(f"scanned={result['total_issues_scanned']} "
          f"flagged={len(result['flagged'])} "
          f"researchable={sum(1 for f in result['flagged'] if f.get('researchable'))} "
          f"low_confidence_appended={added}")
    emit_github_output(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
