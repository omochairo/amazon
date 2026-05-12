"""Article quality gate for おもちゃいろ v4.

Checks article JSON (and rendered Markdown if available) against:
- Schema (data/schema/article.schema.json)
- Word/char count thresholds
- Heading hierarchy (h1 -> h2 -> h3)
- Required sections in narrative
- SEO: product-name occurrence in title/meta/h1/h2/body
- Forbidden childish tone tokens
- FAQ completeness

Outputs a per-article quality score JSON next to each input file:
  data/articles/{slug}.quality.json

Exits non-zero if ANY article fails the configured minimum.

Usage:
    python scripts/quality_gate.py
    python scripts/quality_gate.py --src data/articles/ --posts hugo/content/posts/
    python scripts/quality_gate.py --min-score 70 --strict
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from dataclasses import dataclass, field
from typing import Any

try:
    from jsonschema import Draft7Validator
except ImportError:
    Draft7Validator = None  # type: ignore


# 幼児口調・子ども向け演出は禁止（女性誌調をキープするため）。
# 「おもちゃロボ」はサイト公式キャラ（AI編集ロボの名称）として narrative や
# editorial_comment に登場可。ただし「おもちゃロボがしらべたよ」のように
# 幼児口調と組み合わせると下記パターンに引っかかるので注意。
FORBIDDEN_TONE_PATTERNS = [
    r"だよ[。！\s]",
    r"なんだ[。！\s]",
    r"みてね",
    r"しらべたよ",
    r"ぼく[はが、]",
    r"だね[。！\s]",
]

REQUIRED_NARRATIVE_KEYS = [
    "lead",
    "why_this_product",
    "gift_appeal",
    "daily_use",
    "safety_note",
    "closing",
]

NARRATIVE_MIN_CHARS = {
    "lead": 120,
    "why_this_product": 150,
    "gift_appeal": 120,
    "daily_use": 150,
    "safety_note": 120,
    "closing": 120,
}


@dataclass
class CheckResult:
    name: str
    passed: bool
    score: float  # 0.0 - 1.0
    message: str = ""


@dataclass
class ArticleReport:
    slug: str
    path: str
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def total_score(self) -> int:
        if not self.checks:
            return 0
        return int(round(sum(c.score for c in self.checks) / len(self.checks) * 100))

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "path": self.path,
            "total_score": self.total_score,
            "passed": self.passed,
            "checks": [
                {"name": c.name, "passed": c.passed, "score": round(c.score, 2), "message": c.message}
                for c in self.checks
            ],
        }


def _count_chars(text: str) -> int:
    return len(text or "")


def check_schema(data: dict, schema: dict) -> CheckResult:
    if Draft7Validator is None:
        return CheckResult("schema", True, 1.0, "jsonschema not installed; skipped")
    validator = Draft7Validator(schema)
    errors = sorted(validator.iter_errors(data), key=lambda e: e.path)
    if not errors:
        return CheckResult("schema", True, 1.0, "OK")
    msgs = [f"{'/'.join(map(str, e.path))}: {e.message}" for e in errors[:5]]
    return CheckResult("schema", False, 0.0, "; ".join(msgs))


def check_title_seo(data: dict, product_name: str) -> CheckResult:
    title = data.get("title", "")
    if not title:
        return CheckResult("title_seo", False, 0.0, "empty title")
    head = title[:60]
    has_name = product_name and product_name in head
    length_ok = 20 <= len(title) <= 80
    score = (1.0 if has_name else 0.0) * 0.7 + (1.0 if length_ok else 0.0) * 0.3
    msg = []
    if not has_name:
        msg.append(f"product name '{product_name}' missing in first 60 chars")
    if not length_ok:
        msg.append(f"length {len(title)} not in 20-80")
    return CheckResult("title_seo", score >= 0.7, score, "; ".join(msg) or "OK")


def check_meta_description(data: dict, product_name: str) -> CheckResult:
    meta = data.get("meta_description", "")
    head = meta[:40]
    has_name = product_name and product_name in head
    length_ok = 100 <= len(meta) <= 160
    score = (1.0 if has_name else 0.0) * 0.6 + (1.0 if length_ok else 0.0) * 0.4
    msg = []
    if not has_name:
        msg.append("product name missing in first 40 chars")
    if not length_ok:
        msg.append(f"length {len(meta)} not in 100-160")
    return CheckResult("meta_description", score >= 0.7, score, "; ".join(msg) or "OK")


def check_keywords(data: dict, product_name: str, brand: str) -> CheckResult:
    kws = data.get("keywords", [])
    if not isinstance(kws, list):
        return CheckResult("keywords", False, 0.0, "keywords not a list")
    count_ok = 5 <= len(kws) <= 15
    has_product = any(product_name and product_name in k for k in kws)
    has_brand = bool(brand) and any(brand in k for k in kws)
    score = sum([count_ok, has_product, has_brand]) / 3.0
    msg = []
    if not count_ok:
        msg.append(f"count {len(kws)} not in 5-15")
    if not has_product:
        msg.append("product name not in any keyword")
    if not has_brand and brand:
        msg.append(f"brand '{brand}' not in any keyword")
    return CheckResult("keywords", score >= 0.66, score, "; ".join(msg) or "OK")


def check_narrative(data: dict, product_name: str) -> CheckResult:
    narrative = data.get("narrative", {})
    missing = [k for k in REQUIRED_NARRATIVE_KEYS if k not in narrative]
    if missing:
        return CheckResult("narrative", False, 0.0, f"missing keys: {missing}")

    issues = []
    name_occurrences = 0
    char_score_sum = 0.0
    for key in REQUIRED_NARRATIVE_KEYS:
        text = narrative.get(key, "")
        min_chars = NARRATIVE_MIN_CHARS.get(key, 100)
        actual = _count_chars(text)
        if actual < min_chars:
            issues.append(f"{key} {actual}<{min_chars}")
            char_score_sum += actual / min_chars
        else:
            char_score_sum += 1.0
        if product_name and product_name in text:
            name_occurrences += text.count(product_name)

    char_score = char_score_sum / len(REQUIRED_NARRATIVE_KEYS)
    seo_score = min(name_occurrences / 6.0, 1.0)  # 各セクション最低1回想定
    total = char_score * 0.7 + seo_score * 0.3
    if name_occurrences < 3:
        issues.append(f"product name only appears {name_occurrences} times in narrative (need >=3)")
    msg = "; ".join(issues) if issues else "OK"
    return CheckResult("narrative", total >= 0.7, total, msg)


def check_faq(data: dict, product_name: str) -> CheckResult:
    faq = data.get("faq", [])
    if not isinstance(faq, list):
        return CheckResult("faq", False, 0.0, "faq not a list")
    count_ok = len(faq) >= 3
    name_in_q = sum(1 for f in faq if product_name and product_name in f.get("question", ""))
    answers_ok = all(_count_chars(f.get("answer", "")) >= 30 for f in faq)
    score = (1.0 if count_ok else len(faq) / 3.0) * 0.4 + (min(name_in_q / 2, 1.0)) * 0.3 + (1.0 if answers_ok else 0.5) * 0.3
    msg = []
    if not count_ok:
        msg.append(f"only {len(faq)} FAQ items (need >=3)")
    if name_in_q < 2:
        msg.append(f"only {name_in_q} questions contain product name (recommend >=2)")
    if not answers_ok:
        msg.append("some answers too short (<30 chars)")
    return CheckResult("faq", score >= 0.7, score, "; ".join(msg) or "OK")


def check_score_rationale(data: dict) -> CheckResult:
    rationale = data.get("product", {}).get("ivs_detail", {}).get("score_rationale", [])
    if not isinstance(rationale, list):
        return CheckResult("score_rationale", False, 0.0, "score_rationale not a list")
    count = len(rationale)
    well_formed = sum(
        1 for r in rationale
        if isinstance(r, dict) and r.get("factor") and r.get("delta") and len(r.get("reason", "")) >= 10
    )
    score = min(well_formed / 3.0, 1.0)
    msg = []
    if count < 3:
        msg.append(f"only {count} rationale entries (need >=3)")
    if well_formed < count:
        msg.append(f"{count - well_formed} entries malformed")
    return CheckResult("score_rationale", score >= 0.7, score, "; ".join(msg) or "OK")


def check_tone(data: dict) -> CheckResult:
    """Scan all narrative + faq text for forbidden childish tone patterns."""
    texts = []
    narrative = data.get("narrative", {})
    texts.extend(narrative.values())
    for f in data.get("faq", []):
        texts.append(f.get("question", ""))
        texts.append(f.get("answer", ""))
    texts.append(data.get("editorial_comment", ""))
    blob = "\n".join(t for t in texts if isinstance(t, str))

    hits = []
    for pat in FORBIDDEN_TONE_PATTERNS:
        for m in re.finditer(pat, blob):
            hits.append((pat, m.group(0)))
            if len(hits) >= 5:
                break
        if len(hits) >= 5:
            break

    if not hits:
        return CheckResult("tone", True, 1.0, "OK")
    msg = ", ".join(f"'{h[1].strip()}'" for h in hits)
    return CheckResult("tone", False, 0.0, f"childish tone detected: {msg}")


def check_heading_hierarchy(md_text: str | None) -> CheckResult:
    """Ensure h1 -> h2 -> h3 ordering in rendered markdown (skip if md not provided)."""
    if not md_text:
        return CheckResult("heading_hierarchy", True, 1.0, "no markdown to check (skipped)")

    headings = re.findall(r"^(#{1,6})\s+(.+)$", md_text, re.MULTILINE)
    if not headings:
        return CheckResult("heading_hierarchy", False, 0.0, "no headings found")

    prev_level = 0
    violations = []
    h1_count = 0
    for marks, text in headings:
        level = len(marks)
        if level == 1:
            h1_count += 1
        if prev_level and level > prev_level + 1:
            violations.append(f"jump from h{prev_level} to h{level} at '{text[:30]}'")
        prev_level = level
    score = 1.0
    msg = []
    if h1_count != 1:
        score -= 0.3
        msg.append(f"h1 count={h1_count} (expected 1)")
    if violations:
        score -= 0.1 * len(violations)
        msg.append(violations[0])
    score = max(0.0, score)
    return CheckResult("heading_hierarchy", score >= 0.7, score, "; ".join(msg) or "OK")


def check_body_word_count(md_text: str | None) -> CheckResult:
    if not md_text:
        return CheckResult("body_word_count", True, 1.0, "no markdown (skipped)")
    plain = re.sub(r"```.*?```", "", md_text, flags=re.DOTALL)
    plain = re.sub(r"<[^>]+>", "", plain)
    plain = re.sub(r"[\#\*\-\|\>\[\]\(\)`]", "", plain)
    chars = len(re.sub(r"\s+", "", plain))
    score = min(chars / 2000.0, 1.0)
    msg = f"{chars} chars (target>=2000)"
    return CheckResult("body_word_count", chars >= 1600, score, msg)


def check_product_name_in_body(md_text: str | None, product_name: str) -> CheckResult:
    if not md_text or not product_name:
        return CheckResult("product_name_density", True, 1.0, "skipped")
    n = md_text.count(product_name)
    score = min(n / 5.0, 1.0)
    return CheckResult(
        "product_name_density",
        n >= 3,
        score,
        f"appears {n} times (target>=5 for strong SEO, min 3)",
    )


def evaluate_article(json_path: pathlib.Path, schema: dict, md_path: pathlib.Path | None) -> ArticleReport:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    slug = data.get("slug", json_path.stem)
    md_text: str | None = None
    if md_path and md_path.exists():
        md_text = md_path.read_text(encoding="utf-8")

    product = data.get("product", {})
    product_name = product.get("name", "")
    brand = product.get("brand", "")

    report = ArticleReport(slug=slug, path=str(json_path))
    report.checks.append(check_schema(data, schema))
    report.checks.append(check_title_seo(data, product_name))
    report.checks.append(check_meta_description(data, product_name))
    report.checks.append(check_keywords(data, product_name, brand))
    report.checks.append(check_narrative(data, product_name))
    report.checks.append(check_faq(data, product_name))
    report.checks.append(check_score_rationale(data))
    report.checks.append(check_tone(data))
    report.checks.append(check_heading_hierarchy(md_text))
    report.checks.append(check_body_word_count(md_text))
    report.checks.append(check_product_name_in_body(md_text, product_name))
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", default="data/articles/")
    parser.add_argument("--posts", default="hugo/content/posts/")
    parser.add_argument("--schema", default="data/schema/article.schema.json")
    parser.add_argument("--min-score", type=int, default=60, help="minimum total score (0-100) to pass")
    parser.add_argument("--strict", action="store_true", help="exit non-zero if any article fails any check")
    parser.add_argument("--write-reports", action="store_true", default=True, help="write {slug}.quality.json")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    src = pathlib.Path(args.src)
    posts = pathlib.Path(args.posts)
    schema_path = pathlib.Path(args.schema)

    if not src.exists():
        print(f"[quality_gate] src not found: {src}")
        return 0
    if not schema_path.exists():
        print(f"[quality_gate] schema not found: {schema_path}")
        return 1

    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    json_files = sorted(p for p in src.glob("*.json") if not p.stem.endswith(".enrichment") and not p.stem.endswith(".seo") and not p.stem.endswith(".quality"))

    if not json_files:
        print("[quality_gate] no articles to check")
        return 0

    failures: list[ArticleReport] = []
    below_threshold: list[ArticleReport] = []
    all_reports: list[ArticleReport] = []

    for jp in json_files:
        md_candidate = posts / f"{jp.stem}.md"
        report = evaluate_article(jp, schema, md_candidate if md_candidate.exists() else None)
        all_reports.append(report)
        if args.write_reports:
            out = jp.with_suffix(".quality.json")
            out.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        if not args.quiet:
            status = "OK" if report.passed and report.total_score >= args.min_score else "NG"
            print(f"[{status}] {report.slug} score={report.total_score} ({sum(1 for c in report.checks if c.passed)}/{len(report.checks)} checks)")
            for c in report.checks:
                if not c.passed:
                    print(f"    - {c.name}: {c.message}")
        if not report.passed:
            failures.append(report)
        if report.total_score < args.min_score:
            below_threshold.append(report)

    print()
    print(f"[quality_gate] {len(all_reports)} articles, {len(failures)} with failures, {len(below_threshold)} below min-score {args.min_score}")

    if args.strict and failures:
        return 2
    if below_threshold:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
