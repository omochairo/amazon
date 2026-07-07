"""purge_misattributed_media_20260707.py (one-off)

§4.7 (2026-07-07) で filter_raw_per_asin.py の youtube/news 判定を strict=2
(商品同定判定) に改修したが、既公開記事 data/articles/*.json の
youtube_embeds / news は生成時に焼き込まれており、改修前の誤マッチ
(同ブランド別商品の動画/ニュース) が残っている。

本スクリプトは全記事の youtube_embeds / news の各アイテムを、改修後の
filter_items (strict=2, shared_terms=全ターゲット df>=SHARED_TERM_DF) で
再評価し、strong シグナルを満たさないものを記事 JSON から除去する。

判定は filter_raw_per_asin.py の関数をそのまま import して 1 アイテムずつ
filter_items([item], ...) に通すことで、per_asin 生成と完全に同一ロジック
(strong 判定 + SCORE_THRESHOLD) を適用する。順序は元記事のまま保持し、
_relevance_score 等の付加はしない。

空になった youtube_embeds / news は build_post.py の
_fallback_youtube_embeds / _fallback_news_books がビルド時に per_asin
(strict=2 済み) から補完するため、記事側は空で成立する。
schema (article.schema.json) も両フィールドは単なる array で minItems なし。

Usage:
    cd amazon-clone && python scripts/_oneoff/purge_misattributed_media_20260707.py [--dry-run]
"""

import json
import pathlib
import sys

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))

import filter_raw_per_asin as frpa  # noqa: E402

ROOT = SCRIPTS_DIR.parent
ARTICLES_DIR = ROOT / "data" / "articles"
AMAZON_PATH = ROOT / "data" / "raw" / "amazon.json"

# 記事フィールド → filter 種別。youtube/news とも per_asin 生成時と同じく
# title のみで判定する (per_asin では text_keys=["title"])。
FIELDS = [("youtube_embeds", "youtube"), ("news", "news")]


def main() -> None:
    dry_run = "--dry-run" in sys.argv

    amazon_items = []
    if AMAZON_PATH.exists():
        amazon_items = json.loads(
            AMAZON_PATH.read_text(encoding="utf-8")).get("items", [])

    # filter_raw_per_asin.main() と同じターゲット集合 (amazon.json + articles)
    # から shared_terms を算出する。
    targets = frpa.collect_targets(amazon_items, ARTICLES_DIR)
    extracted: dict[str, tuple] = {}
    term_df: dict[str, int] = {}
    for asin, title in targets.items():
        if not asin:
            continue
        brands, series = frpa.extract_brand_series(title)
        model = frpa.extract_model_number(title)
        tokens = frpa.tokenize(title)
        product_terms = frpa.extract_product_terms(title, brands, series)
        extracted[asin] = (brands, series, model, tokens, product_terms)
        for t in product_terms:
            term_df[t] = term_df.get(t, 0) + 1
    shared_terms = {t for t, n in term_df.items() if n >= frpa.SHARED_TERM_DF}
    print(f"targets={len(targets)} shared_terms(df>={frpa.SHARED_TERM_DF})="
          f"{len(shared_terms)}")

    total_removed = {"youtube_embeds": 0, "news": 0}
    total_kept = {"youtube_embeds": 0, "news": 0}
    modified_files = 0
    skipped = 0
    removals: list[dict] = []

    for path in sorted(ARTICLES_DIR.glob("*.json")):
        parts = path.stem.split("-")
        if len(parts) < 4:
            skipped += 1
            continue
        asin = parts[-1]
        if asin not in extracted:
            print(f"WARN no target title for {path.name} - skipped")
            skipped += 1
            continue
        brands, series, model, tokens, product_terms = extracted[asin]

        art = json.loads(path.read_text(encoding="utf-8"))
        changed = False
        for field, _kind in FIELDS:
            items = art.get(field)
            if not isinstance(items, list) or not items:
                continue
            keep = []
            for it in items:
                passed = isinstance(it, dict) and frpa.filter_items(
                    [it], brands, series, model, tokens, product_terms,
                    ["title"], top_n=1, strict=2, shared_terms=shared_terms)
                if passed:
                    keep.append(it)
                else:
                    total_removed[field] += 1
                    removals.append({
                        "file": path.name,
                        "asin": asin,
                        "field": field,
                        "title": (it.get("title", "")
                                  if isinstance(it, dict) else str(it)),
                        "url": (it.get("url", "")
                                if isinstance(it, dict) else ""),
                    })
            total_kept[field] += len(keep)
            if len(keep) != len(items):
                art[field] = keep
                changed = True
        if changed:
            modified_files += 1
            if not dry_run:
                path.write_text(
                    json.dumps(art, ensure_ascii=False, indent=2),
                    encoding="utf-8", newline="\n")

    report = {
        "dry_run": dry_run,
        "articles_scanned": len(list(ARTICLES_DIR.glob("*.json"))) - skipped,
        "articles_skipped": skipped,
        "articles_modified": modified_files,
        "removed": total_removed,
        "kept": total_kept,
        "removals": removals,
    }
    report_path = pathlib.Path(__file__).with_suffix(".report.json")
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
        newline="\n")
    print(f"articles modified: {modified_files} (skipped {skipped})")
    print(f"removed: youtube_embeds={total_removed['youtube_embeds']} "
          f"news={total_removed['news']}")
    print(f"kept:    youtube_embeds={total_kept['youtube_embeds']} "
          f"news={total_kept['news']}")
    print(f"report: {report_path}")
    if dry_run:
        print("(dry-run: no files written)")


if __name__ == "__main__":
    main()
