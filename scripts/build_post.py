"""Build Hugo post markdown from article JSON + enrichment + seo (おもちゃいろ v4).

For each ``data/articles/{slug}.json`` (excluding ``.enrichment.json`` /
``.seo.json`` / ``.quality.json`` siblings), merge the optional STAGE-2 and
STAGE-3 outputs and render ``hugo/content/posts/{slug}.md``.

Frontmatter is populated from STAGE-3 SEO data when available (optimized title /
meta description / jsonld / breadcrumbs), falling back to STAGE-1 fields.

Usage:
    python scripts/build_post.py
    python scripts/build_post.py --src data/articles/ --dst hugo/content/posts/
    python scripts/build_post.py --min-score 70   # mark below-threshold as draft
"""

from __future__ import annotations

import argparse
import json
import pathlib
from datetime import datetime
from typing import Any

import frontmatter
import jinja2


SUFFIX_SKIP = (".enrichment", ".seo", ".quality")

ENRICHMENT_KEYS = (
    "review_summary",
    "use_case_scenarios",
    "competitive_position",
    "gift_reaction_prediction",
    "expert_take",
)

SEO_KEYS = (
    "title_variants",
    "meta_description_optimized",
    "h1_recommendation",
    "h2_recommendations",
    "faq_extended",
    "breadcrumbs",
    "jsonld",
    "internal_link_suggestions",
)


def _load_optional_json(path: pathlib.Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"  ! skip malformed {path.name}: {e}")
        return {}


def _merge(data: dict[str, Any], extra: dict[str, Any], keys: tuple[str, ...]) -> None:
    for key in keys:
        if key in extra and key not in data:
            data[key] = extra[key]


def _fill_jsonld(data: dict[str, Any]) -> None:
    jsonld = data.get("jsonld")
    if not isinstance(jsonld, dict):
        return
    faq = data.get("faq_extended") or data.get("faq") or []
    if isinstance(jsonld.get("faq"), dict):
        jsonld["faq"]["mainEntity"] = [
            {
                "@type": "Question",
                "name": q.get("question", ""),
                "acceptedAnswer": {"@type": "Answer", "text": q.get("answer", "")},
            }
            for q in faq
        ]
    breadcrumbs = data.get("breadcrumbs") or []
    if isinstance(jsonld.get("breadcrumb"), dict):
        jsonld["breadcrumb"]["itemListElement"] = [
            {
                "@type": "ListItem",
                "position": i + 1,
                "name": b.get("name", ""),
                "item": b.get("url", ""),
            }
            for i, b in enumerate(breadcrumbs)
        ]


def _frontmatter_meta(data: dict[str, Any], slug: str, draft: bool) -> dict[str, Any]:
    title = data.get("title", "No Title")
    variants = data.get("title_variants") or []
    if variants and isinstance(variants[0], dict) and variants[0].get("title"):
        title = variants[0]["title"]
    description = data.get("meta_description_optimized") or data.get("meta_description", "")
    meta: dict[str, Any] = {
        "title": title,
        "date": data.get("date", datetime.now().isoformat()),
        "tags": data.get("tags", []),
        "slug": slug,
        "draft": draft,
        "description": description,
        "keywords": data.get("keywords", []),
    }
    if data.get("jsonld"):
        meta["jsonld"] = data["jsonld"]
    if data.get("breadcrumbs"):
        meta["breadcrumbs"] = data["breadcrumbs"]
    return meta


def _quality_draft(slug: str, src_path: pathlib.Path, min_score: int) -> bool:
    if min_score <= 0:
        return False
    qpath = src_path / f"{slug}.quality.json"
    if not qpath.exists():
        return False
    try:
        q = json.loads(qpath.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    score = q.get("total_score", q.get("score", 0))
    return int(score) < min_score


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", default="data/articles/")
    parser.add_argument("--dst", default="hugo/content/posts/")
    parser.add_argument("--min-score", type=int, default=0,
                        help="If >0, set draft=true when quality total_score < value")
    parser.add_argument("--gate", action="store_true",
                        help="Run quality_gate after each render and write {slug}.quality.json")
    parser.add_argument("--schema", default="data/schema/article.schema.json",
                        help="Schema path used when --gate is set")
    args = parser.parse_args()

    evaluate_article = None
    schema: dict[str, Any] = {}
    if args.gate:
        try:
            from quality_gate import evaluate_article  # type: ignore
        except ImportError as e:
            print(f"--gate requested but quality_gate import failed: {e}")
            return
        schema_path = pathlib.Path(args.schema)
        if schema_path.exists():
            try:
                schema = json.loads(schema_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                print(f"--gate: schema parse error ({schema_path}): {e}")
                schema = {}

    src_path = pathlib.Path(args.src)
    dst_path = pathlib.Path(args.dst)
    dst_path.mkdir(parents=True, exist_ok=True)

    template_file = pathlib.Path("scripts/templates/post.md.j2")
    if not template_file.exists():
        print("Template not found")
        return

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(template_file.parent)),
        autoescape=False,
        keep_trailing_newline=True,
    )
    template = env.get_template(template_file.name)

    rendered = 0
    skipped_legacy = 0
    for f in sorted(src_path.glob("*.json")):
        if f.stem.endswith(SUFFIX_SKIP):
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            slug = data.get("slug", f.stem)
            legacy_v3 = "narrative" not in data
            if legacy_v3:
                data.setdefault("narrative", {k: "" for k in (
                    "lead", "why_this_product", "gift_appeal",
                    "daily_use", "safety_note", "closing")})
                data.setdefault("persona_fit", {})
                data.setdefault("faq", [])
                data.setdefault("keywords", [])
                skipped_legacy += 1
            _merge(data, _load_optional_json(src_path / f"{slug}.enrichment.json"), ENRICHMENT_KEYS)
            _merge(data, _load_optional_json(src_path / f"{slug}.seo.json"), SEO_KEYS)
            _fill_jsonld(data)

            md_body = template.render(**data)
            draft = _quality_draft(slug, src_path, args.min_score)
            post = frontmatter.Post(md_body, **_frontmatter_meta(data, slug, draft))

            out_file = dst_path / f"{slug}.md"
            out_file.write_text(frontmatter.dumps(post), encoding="utf-8")
            tag = "  [DRAFT: low quality]" if draft else ""

            if args.gate and evaluate_article is not None:
                report = evaluate_article(f, schema, out_file)
                qpath = src_path / f"{slug}.quality.json"
                qpath.write_text(
                    json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                if args.min_score > 0 and report.total_score < args.min_score and not draft:
                    post.metadata["draft"] = True
                    out_file.write_text(frontmatter.dumps(post), encoding="utf-8")
                    tag = f"  [DRAFT: score {report.total_score} < {args.min_score}]"
                else:
                    tag += f"  [score {report.total_score}]"

            legacy_tag = "  [legacy v3 fallback]" if legacy_v3 else ""
            print(f"Rendered: {out_file}{tag}{legacy_tag}")
            rendered += 1
        except Exception as e:
            print(f"Error processing {f}: {e}")
    msg = f"\nDone. {rendered} post(s) rendered."
    if skipped_legacy:
        msg += f" {skipped_legacy} rendered as legacy v3 fallback (regenerate via Jules)."
    print(msg)


if __name__ == "__main__":
    main()
