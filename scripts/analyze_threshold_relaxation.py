#!/usr/bin/env python3
"""Issue #1072 Phase 3 Option A: 低確度マッチ閾値緩和の dry-run 分析。

`_matched_passes_quality` (scripts/build_post.py) の現状閾値で fail している
未検証リンクを、緩和した閾値プリセットで再評価し、「どれが救えるか / その
代償としてどんな誤マッチを許容することになるか」を一覧化する。

対象 ASIN は ``--jan-unknown-json`` で与える (analyze_low_confidence_links.py の
``--json`` 出力)。Option A の意思決定材料に使う。API は一切叩かない (in-memory
analysis only)。

Output:
    - --report-md: 人間レビュー用の markdown table (推奨)
    - --report-json: machine-readable な集計 (summary + per-ASIN details)

Usage::

    python scripts/analyze_low_confidence_links.py \
        --json scratch/low_confidence_links.json
    python scripts/analyze_threshold_relaxation.py \
        --jan-unknown-json scratch/low_confidence_links.json \
        --report-md scratch/threshold_relaxation.md \
        --report-json scratch/threshold_relaxation.json
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
from dataclasses import dataclass, field
from typing import Any

# build_post の閾値定義を流用 (drift を防ぐため import で参照)
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if THIS_DIR not in sys.path:
    sys.path.insert(0, THIS_DIR)

import build_post  # noqa: E402  (defines _MARKET_* constants & quality gate)


DEFAULT_RAKUTEN_MATCHED = pathlib.Path("data/raw/rakuten_matched.json")
DEFAULT_YAHOO_MATCHED = pathlib.Path("data/raw/yahoo_matched.json")
DEFAULT_PER_ASIN_DIR = pathlib.Path("data/raw/per_asin")
DEFAULT_AMAZON_RAW = pathlib.Path("data/raw/amazon.json")


@dataclass
class ThresholdPreset:
    """`_matched_passes_quality` の閾値セット。"""

    name: str
    price_low: float
    price_high: float
    coverage_ratio: float
    hits_threshold_when_multi: int  # meaningful >= 2 のときの hits 下限
    description: str = ""

    @classmethod
    def current(cls) -> "ThresholdPreset":
        return cls(
            name="current",
            price_low=build_post._MARKET_PRICE_BAND_LOW,
            price_high=build_post._MARKET_PRICE_BAND_HIGH,
            coverage_ratio=build_post._MARKET_COVERAGE_RATIO,
            hits_threshold_when_multi=2,
            description="本番現行 (build_post の constants)",
        )


def _passes(matched: dict[str, Any], amazon_price: int, preset: ThresholdPreset) -> bool:
    """`_matched_passes_quality` を preset 化したコピー。

    build_post 側のロジックを 1:1 で trace するため、変更時は両者同期必須。
    """
    title = matched.get("title") or ""
    try:
        price = int(matched.get("price") or 0)
    except (TypeError, ValueError):
        price = 0
    if not title or price <= 0:
        return False

    if amazon_price > 0:
        if price < amazon_price * preset.price_low:
            return False
        if price > amazon_price * preset.price_high:
            return False

    kw = matched.get("search_keyword") or ""
    kw_tokens = [t for t in re.split(r"\s+", kw) if len(t) >= 2]
    meaningful = [t for t in kw_tokens if t not in build_post._MARKET_GENERIC_TOKENS]
    if not meaningful:
        return True
    hits = sum(1 for t in meaningful if t in title)
    threshold = preset.hits_threshold_when_multi if len(meaningful) >= 2 else 1
    if hits < threshold:
        return False
    if hits / len(meaningful) < preset.coverage_ratio:
        return False
    return True


def _why_fail(matched: dict[str, Any], amazon_price: int, preset: ThresholdPreset) -> str:
    """fail した理由を 1 行で返す (人間レビュー用)。"""
    title = matched.get("title") or ""
    try:
        price = int(matched.get("price") or 0)
    except (TypeError, ValueError):
        price = 0
    if not title:
        return "no_title"
    if price <= 0:
        return "no_price"
    if amazon_price > 0:
        ratio = price / amazon_price
        if price < amazon_price * preset.price_low:
            return f"price_too_low (ratio={ratio:.2f})"
        if price > amazon_price * preset.price_high:
            return f"price_too_high (ratio={ratio:.2f})"
    kw = matched.get("search_keyword") or ""
    kw_tokens = [t for t in re.split(r"\s+", kw) if len(t) >= 2]
    meaningful = [t for t in kw_tokens if t not in build_post._MARKET_GENERIC_TOKENS]
    if not meaningful:
        return "no_meaningful_tokens (passed)"
    hits = sum(1 for t in meaningful if t in title)
    threshold = preset.hits_threshold_when_multi if len(meaningful) >= 2 else 1
    if hits < threshold:
        return f"hits_low (hits={hits}/{len(meaningful)}, thr={threshold})"
    cov = hits / len(meaningful)
    if cov < preset.coverage_ratio:
        return f"coverage_low (cov={cov:.2f}, thr={preset.coverage_ratio})"
    return "passed"


def _build_presets() -> list[ThresholdPreset]:
    """評価対象プリセット一覧 (current + 段階緩和)。"""
    return [
        ThresholdPreset.current(),
        ThresholdPreset(
            name="relax_price",
            price_low=0.4, price_high=2.5,
            coverage_ratio=build_post._MARKET_COVERAGE_RATIO,
            hits_threshold_when_multi=2,
            description="価格帯のみ緩め (0.4x-2.5x)",
        ),
        ThresholdPreset(
            name="relax_coverage",
            price_low=build_post._MARKET_PRICE_BAND_LOW,
            price_high=build_post._MARKET_PRICE_BAND_HIGH,
            coverage_ratio=0.5,
            hits_threshold_when_multi=2,
            description="coverage のみ緩め (0.7→0.5)",
        ),
        ThresholdPreset(
            name="relax_both",
            price_low=0.4, price_high=2.5,
            coverage_ratio=0.5,
            hits_threshold_when_multi=2,
            description="価格 + coverage 緩め",
        ),
        ThresholdPreset(
            name="relax_aggressive",
            price_low=0.3, price_high=3.0,
            coverage_ratio=0.4,
            hits_threshold_when_multi=1,
            description="aggressive (FP リスク大、上限見極め用)",
        ),
    ]


def _load_json(path: pathlib.Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _load_amazon_price(asin: str, amazon_raw: dict, per_asin_dir: pathlib.Path) -> int:
    """amazon.json 1 次、per_asin snapshot 2 次で price を引く。"""
    for item in (amazon_raw or {}).get("items", []) or []:
        if item.get("asin") == asin:
            try:
                return int(item.get("price") or 0)
            except (TypeError, ValueError):
                return 0
    snap = _load_json(per_asin_dir / asin / "amazon.json") or {}
    inner = snap.get("item") if isinstance(snap.get("item"), dict) else snap
    try:
        return int((inner or {}).get("price") or 0)
    except (TypeError, ValueError):
        return 0


@dataclass
class AsinReport:
    asin: str
    amazon_price: int
    rakuten: dict[str, Any] = field(default_factory=dict)  # preset_name -> status info
    yahoo: dict[str, Any] = field(default_factory=dict)


def _eval_site(
    asin: str,
    amazon_price: int,
    matched_entry: dict | None,
    presets: list[ThresholdPreset],
) -> dict[str, Any]:
    """1 サイト分の preset 別評価結果。"""
    if not matched_entry:
        return {"has_candidate": False}
    base = {
        "has_candidate": True,
        "title": matched_entry.get("title", ""),
        "price": matched_entry.get("price", 0),
        "search_keyword": matched_entry.get("search_keyword", ""),
        "match_method": matched_entry.get("_match_method", ""),
        "by_preset": {},
    }
    for p in presets:
        passed = _passes(matched_entry, amazon_price, p)
        base["by_preset"][p.name] = {
            "passed": passed,
            "reason": _why_fail(matched_entry, amazon_price, p),
        }
    return base


def analyze(
    jan_unknown_asins: list[str],
    rakuten_index: dict[str, dict],
    yahoo_index: dict[str, dict],
    amazon_raw: dict,
    per_asin_dir: pathlib.Path,
    presets: list[ThresholdPreset],
) -> tuple[list[AsinReport], dict[str, Any]]:
    """ASIN ごとの評価 + preset サマリ。"""
    reports: list[AsinReport] = []
    summary: dict[str, dict[str, int]] = {p.name: {
        "rakuten_pass": 0, "yahoo_pass": 0,
        "rakuten_rescued": 0, "yahoo_rescued": 0,
    } for p in presets}

    current_name = presets[0].name  # convention: first is current
    for asin in jan_unknown_asins:
        amazon_price = _load_amazon_price(asin, amazon_raw, per_asin_dir)
        r = _eval_site(asin, amazon_price, rakuten_index.get(asin), presets)
        y = _eval_site(asin, amazon_price, yahoo_index.get(asin), presets)
        reports.append(AsinReport(asin=asin, amazon_price=amazon_price, rakuten=r, yahoo=y))

        for p in presets:
            if r.get("has_candidate") and r["by_preset"][p.name]["passed"]:
                summary[p.name]["rakuten_pass"] += 1
                if not r["by_preset"][current_name]["passed"]:
                    summary[p.name]["rakuten_rescued"] += 1
            if y.get("has_candidate") and y["by_preset"][p.name]["passed"]:
                summary[p.name]["yahoo_pass"] += 1
                if not y["by_preset"][current_name]["passed"]:
                    summary[p.name]["yahoo_rescued"] += 1
    return reports, summary


def render_markdown(
    reports: list[AsinReport],
    summary: dict[str, dict[str, int]],
    presets: list[ThresholdPreset],
) -> str:
    lines: list[str] = []
    lines.append("# 低確度マッチ閾値緩和 dry-run (#1072 Option A)\n")
    lines.append(f"対象 jan_unknown ASIN: {len(reports)} 件\n")

    lines.append("## Preset サマリ\n")
    lines.append("| preset | 説明 | Rakuten pass | (うち救済) | Yahoo pass | (うち救済) |")
    lines.append("| --- | --- | ---: | ---: | ---: | ---: |")
    for p in presets:
        s = summary[p.name]
        lines.append(
            f"| `{p.name}` | {p.description} | {s['rakuten_pass']} | +{s['rakuten_rescued']} "
            f"| {s['yahoo_pass']} | +{s['yahoo_rescued']} |"
        )

    lines.append("\n## 救済される候補の人間レビュー\n")
    lines.append("以下は `current` で fail だが、いずれかの relax preset で pass する ASIN。")
    lines.append("title / price / search_keyword を見て **誤マッチかどうか目視判定**する。\n")
    for site_key, site_label in (("rakuten", "Rakuten"), ("yahoo", "Yahoo")):
        lines.append(f"### {site_label}\n")
        lines.append("| ASIN | Amazon¥ | 候補 title | 候補¥ | search_keyword | current 理由 | pass する preset |")
        lines.append("| --- | ---: | --- | ---: | --- | --- | --- |")
        for r in reports:
            site = getattr(r, site_key)
            if not site.get("has_candidate"):
                continue
            cur = site["by_preset"][presets[0].name]
            if cur["passed"]:
                continue
            rescuing = [p.name for p in presets[1:] if site["by_preset"][p.name]["passed"]]
            if not rescuing:
                continue
            title = (site["title"] or "")[:50]
            kw = (site["search_keyword"] or "")[:40]
            lines.append(
                f"| `{r.asin}` | {r.amazon_price} | {title} | {site['price']} | {kw} "
                f"| {cur['reason']} | {', '.join(rescuing)} |"
            )
        lines.append("")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--jan-unknown-json", type=pathlib.Path, required=True,
                   help="analyze_low_confidence_links.py --json 出力。jan_unknown 配列を読む")
    p.add_argument("--rakuten-matched", type=pathlib.Path, default=DEFAULT_RAKUTEN_MATCHED)
    p.add_argument("--yahoo-matched", type=pathlib.Path, default=DEFAULT_YAHOO_MATCHED)
    p.add_argument("--per-asin-dir", type=pathlib.Path, default=DEFAULT_PER_ASIN_DIR)
    p.add_argument("--amazon-raw", type=pathlib.Path, default=DEFAULT_AMAZON_RAW)
    p.add_argument("--report-md", type=pathlib.Path, default=None)
    p.add_argument("--report-json", type=pathlib.Path, default=None)
    args = p.parse_args(argv)

    raw = _load_json(args.jan_unknown_json) or {}
    jan_unknown_asins = raw.get("jan_unknown") or []
    if not jan_unknown_asins:
        print(f"No jan_unknown ASINs in {args.jan_unknown_json}", file=sys.stderr)
        return 1

    rakuten_index: dict[str, dict] = {}
    for it in ((_load_json(args.rakuten_matched) or {}).get("items") or []):
        a = it.get("matched_asin") or it.get("asin")
        if a:
            rakuten_index[a] = it
    yahoo_index: dict[str, dict] = {}
    for it in ((_load_json(args.yahoo_matched) or {}).get("items") or []):
        a = it.get("matched_asin") or it.get("asin")
        if a:
            yahoo_index[a] = it
    amazon_raw = _load_json(args.amazon_raw) or {}

    presets = _build_presets()
    reports, summary = analyze(
        jan_unknown_asins, rakuten_index, yahoo_index, amazon_raw, args.per_asin_dir, presets,
    )

    if args.report_md:
        args.report_md.parent.mkdir(parents=True, exist_ok=True)
        args.report_md.write_text(render_markdown(reports, summary, presets), encoding="utf-8")
        print(f"wrote {args.report_md}")
    if args.report_json:
        args.report_json.parent.mkdir(parents=True, exist_ok=True)
        out = {
            "target_asin_count": len(reports),
            "presets": [
                {"name": p.name, "description": p.description,
                 "price_low": p.price_low, "price_high": p.price_high,
                 "coverage_ratio": p.coverage_ratio,
                 "hits_threshold_when_multi": p.hits_threshold_when_multi}
                for p in presets
            ],
            "summary": summary,
            "items": [
                {"asin": r.asin, "amazon_price": r.amazon_price,
                 "rakuten": r.rakuten, "yahoo": r.yahoo}
                for r in reports
            ],
        }
        args.report_json.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {args.report_json}")
    if not args.report_md and not args.report_json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
