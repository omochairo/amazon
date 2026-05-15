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
import re
from datetime import datetime
from typing import Any

import frontmatter
import jinja2


SUFFIX_SKIP = (".enrichment", ".seo", ".quality")

BADGE_FIELDS = ("availability", "loyalty_points", "savings_percentage")

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


def _load_raw_amazon_index(raw_path: pathlib.Path) -> dict[str, dict[str, Any]]:
    """Return {asin: item} from data/raw/amazon.json, or {} if absent/malformed.

    Used to back-fill availability / loyalty_points / savings_percentage into
    article JSON when the upstream generator (Jules) did not include them.
    """
    if not raw_path.exists():
        return {}
    try:
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return {it.get("asin"): it for it in raw.get("items", []) if it.get("asin")}


def _load_per_asin_competitors(per_asin_root: pathlib.Path, asin: str) -> list[dict[str, Any]]:
    """Return the API-fetched competitor list for ``asin`` (possibly empty).

    Written by fetch_amazon.py as
    ``data/raw/per_asin/<ASIN>/competitors.json`` with shape
    ``{asin, fetched_at, competitors: [{asin, name, image, price, url, features}]}``.
    Every entry has a verified ASIN, image, and affiliate URL — so they can
    replace Jules-hallucinated ``competitive_analysis[]`` items wholesale.
    """
    p = per_asin_root / asin / "competitors.json"
    if not p.exists():
        return []
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    items = payload.get("competitors") if isinstance(payload, dict) else None
    return [c for c in items if isinstance(c, dict) and c.get("asin")] if isinstance(items, list) else []


def _price_comparison_label(target_price: int, competitor_price: int) -> str:
    if not target_price or not competitor_price:
        return ""
    diff = competitor_price - target_price
    if abs(diff) < 100:
        return "本品とほぼ同価格"
    yen = f"{abs(diff):,}円"
    return f"本品より{yen}安い" if diff < 0 else f"本品より{yen}高い"


def _build_asin_to_slug_map(src_path: pathlib.Path) -> dict[str, str]:
    """Return {asin: slug} for every article JSON under ``src_path``.

    Used so competitor cards can deep-link to an existing internal article
    when the competitor ASIN already has dedicated coverage on the site.
    """
    mapping: dict[str, str] = {}
    if not src_path.exists():
        return mapping
    for f in src_path.glob("*.json"):
        if f.stem.endswith(SUFFIX_SKIP):
            continue
        try:
            meta = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        slug = meta.get("slug") or f.stem
        product = meta.get("product") if isinstance(meta.get("product"), dict) else None
        asin = product.get("asin") if product else None
        if not asin:
            m = re.search(r"-(B0[A-Z0-9]{8})$", f.stem)
            if m:
                asin = m.group(1)
        if not asin:
            continue
        mapping.setdefault(asin, slug)
    return mapping


def _site_base_path(config_path: pathlib.Path) -> str:
    """Extract the path component of ``baseURL`` from hugo/config.toml.

    Returns e.g. ``/amazon`` for ``https://omochairo.github.io/amazon/``,
    or ``""`` when the site is served at the host root. Hugo does not
    rewrite raw <a href> URLs emitted from our jinja template, so the
    GitHub-Pages subpath has to be baked in here.
    """
    if not config_path.exists():
        return ""
    try:
        for line in config_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped.lower().startswith("baseurl"):
                continue
            m = re.search(r"['\"]([^'\"]+)['\"]", stripped)
            if not m:
                return ""
            from urllib.parse import urlparse
            return urlparse(m.group(1)).path.rstrip("/")
    except OSError:
        return ""
    return ""


def _attach_internal_links(
    data: dict[str, Any],
    asin_to_slug: dict[str, str],
    site_base_path: str,
) -> None:
    """Mark each competitor entry with ``internal_url`` when we already
    publish an article for that ASIN. The current article's own ASIN is
    excluded so a card never self-links."""
    ca = data.get("competitive_analysis")
    if not isinstance(ca, list) or not asin_to_slug:
        return
    product = data.get("product") if isinstance(data.get("product"), dict) else None
    self_asin = product.get("asin") if product else None
    for c in ca:
        if not isinstance(c, dict):
            continue
        asin = c.get("asin")
        if not asin or asin == self_asin:
            continue
        slug = asin_to_slug.get(asin)
        if slug:
            # Hugo / PaperMod serves post URLs lower-cased (disablePathToLower
            # default is false), so the ASIN segment in the slug has to be
            # lowered to avoid 404s on GitHub Pages.
            c["internal_url"] = f"{site_base_path}/posts/{slug.lower()}/"


def _override_competitive_analysis(
    data: dict[str, Any],
    per_asin_root: pathlib.Path,
) -> None:
    """If we have API-fetched competitors for this article's ASIN, replace
    ``competitive_analysis`` with entries that always resolve to a real listing
    (verified ASIN, real image URL, real affiliate URL).

    The Jules-authored ``feature_comparison`` / ``differentiators`` text is
    dropped — those routinely paired with fabricated ASINs, so trying to
    fuzzy-match them onto real competitors is worse than starting clean.
    """
    product = data.get("product") if isinstance(data.get("product"), dict) else None
    if not product:
        return
    asin = product.get("asin")
    if not asin:
        return
    competitors = _load_per_asin_competitors(per_asin_root, asin)
    if not competitors:
        # Jules routinely pairs a real-sounding ``name`` with a fabricated
        # ``asin`` (sometimes one that happens to belong to a different
        # article on this very site, which would otherwise be turned into a
        # misleading internal link). Strip the ASIN entirely when we have
        # no API-verified replacement — the card still renders its text
        # block, but the thumbnail / Amazon CTA / internal-link button all
        # disappear via the ``c.asin AND (c.image OR c.internal_url)`` gate.
        ca = data.get("competitive_analysis")
        if isinstance(ca, list):
            for c in ca:
                if isinstance(c, dict):
                    c.pop("asin", None)
        return
    target_price = 0
    amazon_price = (product.get("prices") or {}).get("amazon") or {}
    if isinstance(amazon_price, dict):
        try:
            target_price = int(amazon_price.get("price") or 0)
        except (TypeError, ValueError):
            target_price = 0

    new_entries: list[dict[str, Any]] = []
    for c in competitors:
        try:
            cp = int(c.get("price") or 0)
        except (TypeError, ValueError):
            cp = 0
        new_entries.append({
            "asin": c["asin"],
            "name": c.get("name") or "",
            "image": c.get("image") or "",
            "url": c.get("url") or f"https://www.amazon.co.jp/dp/{c['asin']}/",
            "price": cp,
            "price_comparison": _price_comparison_label(target_price, cp),
            "feature_comparison": [f"特徴：{f}" for f in (c.get("features") or [])[:2]],
            "differentiators": [],
        })
    data["competitive_analysis"] = new_entries


def _load_per_asin_items(
    per_asin_root: pathlib.Path,
    asin: str,
    filename: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Return up to ``limit`` items from ``data/raw/per_asin/<ASIN>/<filename>``.

    Used as a fallback for the Jules-authored ``news``/``books`` fields when
    they come back empty. File shape: ``{"items": [...]}``.
    """
    p = per_asin_root / asin / filename
    if not p.exists():
        return []
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []
    return [
        it for it in items
        if isinstance(it, dict) and it.get("title") and it.get("url")
    ][:limit]


def _fallback_news_books(data: dict[str, Any], per_asin_root: pathlib.Path) -> None:
    """When the article JSON's ``news``/``books`` field is missing or empty,
    populate it from the per-ASIN snapshots written by fetch_yahoo_news.py and
    fetch_books.py. Non-empty Jules-authored values are preserved."""
    product = data.get("product") if isinstance(data.get("product"), dict) else None
    asin = product.get("asin") if product else None
    if not asin:
        return
    for key, filename in (("news", "news.json"), ("books", "books.json")):
        if data.get(key):
            continue
        items = _load_per_asin_items(per_asin_root, asin, filename)
        if items:
            data[key] = items


def _load_per_asin_amazon(per_asin_root: pathlib.Path, asin: str) -> dict[str, Any] | None:
    """Return the per-ASIN amazon snapshot dict, or None if absent/malformed.

    Snapshots are written by fetch_amazon.py as
    ``data/raw/per_asin/<ASIN>/amazon.json`` with shape ``{asin, fetched_at, item}``.
    Older snapshots may store the item dict at the root — both shapes are tolerated.
    """
    p = per_asin_root / asin / "amazon.json"
    if not p.exists():
        return None
    try:
        snap = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(snap, dict):
        return None
    item = snap.get("item") if isinstance(snap.get("item"), dict) else snap
    return item if isinstance(item, dict) else None


def _backfill_amazon_badges(
    data: dict[str, Any],
    raw_index: dict[str, dict[str, Any]],
    per_asin_root: pathlib.Path,
) -> None:
    """If prices.amazon is missing badge fields, copy them from the latest
    raw/amazon.json first, then fall back to the per-ASIN snapshot under
    data/raw/per_asin/<ASIN>/amazon.json. Existing values in the article win."""
    product = data.get("product") if isinstance(data.get("product"), dict) else None
    if not product:
        return
    asin = product.get("asin")
    if not asin:
        return

    sources: list[dict[str, Any]] = []
    raw_item = raw_index.get(asin)
    if raw_item:
        sources.append(raw_item)
    snap_item = _load_per_asin_amazon(per_asin_root, asin)
    if snap_item:
        sources.append(snap_item)
    if not sources:
        return

    prices = product.setdefault("prices", {})
    amazon = prices.setdefault("amazon", {})
    for field in BADGE_FIELDS:
        if amazon.get(field):
            continue
        for src in sources:
            val = src.get(field)
            if val:
                amazon[field] = val
                break


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
    product = data.get("product") or {}
    image_url = product.get("image") or ""
    if image_url:
        meta["product_image"] = image_url
    if product.get("brand"):
        meta["brand"] = product["brand"]
        # brands taxonomy 用: ノーブランド / 不明系はタクソノミーに含めない
        # (UI 側ブラックリストと同期。根本対策は @J で別途)
        _brand_blacklist = {
            "不明", "不明 / 不明", "Unknown", "N/A", "なし", "—", "-",
            "ノーブランド", "ノーブランド品", "NoBrand", "No Brand", "Generic",
        }
        if product["brand"] not in _brand_blacklist:
            meta["brands"] = [product["brand"]]
    if product.get("name"):
        meta["product_name"] = product["name"]
    ivs_score = product.get("ivs_score")
    if isinstance(ivs_score, (int, float)):
        meta["ivs_score"] = float(ivs_score)
    ivs_detail = product.get("ivs_detail") or {}
    total_100 = ivs_detail.get("total_100")
    if isinstance(total_100, (int, float)):
        meta["ivs_score_100"] = int(total_100)
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
    parser.add_argument("--raw-amazon", default="data/raw/amazon.json",
                        help="Raw amazon.json used to back-fill badge fields (availability/loyalty_points/savings_percentage)")
    parser.add_argument("--per-asin-root", default="data/raw/per_asin",
                        help="Directory holding per-ASIN amazon snapshots used as a back-fill fallback when raw/amazon.json no longer contains the ASIN")
    parser.add_argument("--hugo-config", default="hugo/config.toml",
                        help="Hugo config used to derive site base path for internal links on competitor cards")
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
    raw_amazon_index = _load_raw_amazon_index(pathlib.Path(args.raw_amazon))
    per_asin_root = pathlib.Path(args.per_asin_root)
    asin_to_slug = _build_asin_to_slug_map(src_path)
    site_base_path = _site_base_path(pathlib.Path(args.hugo_config))

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
            _backfill_amazon_badges(data, raw_amazon_index, per_asin_root)
            _override_competitive_analysis(data, per_asin_root)
            _fallback_news_books(data, per_asin_root)
            _attach_internal_links(data, asin_to_slug, site_base_path)
            _fill_jsonld(data)

            _meta_re = re.compile(r"\s*[(（]\s*\d+\s*字\s*[)）]\s*$")
            if isinstance(data.get("narrative"), dict):
                for k in ("lead", "why_this_product", "gift_appeal", "daily_use", "safety_note", "closing"):
                    v = data["narrative"].get(k)
                    if isinstance(v, str):
                        data["narrative"][k] = _meta_re.sub("", v).strip()
            for top_key in ("editorial_comment", "expert_take"):
                v = data.get(top_key)
                if isinstance(v, str):
                    data[top_key] = _meta_re.sub("", v).strip()

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
