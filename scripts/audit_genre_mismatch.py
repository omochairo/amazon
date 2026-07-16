"""既掲載記事のジャンル不一致棚卸しスクリプト (#2823)。

data/articles/ の記事 ASIN を列挙し、data/raw/per_asin/{ASIN}/amazon.json の
browse_nodes を genre_gate.classify_genre で判定してレポートを stdout に
markdown で出力する。CI ゲートではなく人間レビュー用のレポートなので、
判定結果に関わらず終了コードは常に 0。

実行方法 (repo root を cwd として):
    python scripts/audit_genre_mismatch.py

再実行可能。browse_nodes 蓄積が進むたびに再実行して FLAG を確認する運用を
想定 (次回想定: 2026-07-15、記事全件の browse_nodes 蓄積完了後)。
"""
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from genre_gate import classify_genre

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
ARTICLES_DIR = REPO_ROOT / "data" / "articles"
PER_ASIN_DIR = REPO_ROOT / "data" / "raw" / "per_asin"
BLOCKLIST_PATH = REPO_ROOT / "data" / "asin_blocklist.json"

ARTICLE_FILENAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-(B[A-Z0-9]{9})\.json$")


def load_blocklist_asins() -> set:
    if not BLOCKLIST_PATH.exists():
        return set()
    try:
        data = json.loads(BLOCKLIST_PATH.read_text(encoding="utf-8"))
    except Exception:
        return set()
    return {entry.get("asin") for entry in data.get("blocked", []) if entry.get("asin")}


def collect_article_asins() -> dict:
    """{asin: filename} を返す (記事本体 JSON のみ、複数あれば最初の 1 件)。"""
    out = {}
    if not ARTICLES_DIR.exists():
        return out
    for p in sorted(ARTICLES_DIR.iterdir()):
        m = ARTICLE_FILENAME_RE.match(p.name)
        if not m:
            continue
        asin = m.group(1)
        out.setdefault(asin, p.name)
    return out


def load_snapshot_item(asin: str):
    snap_path = PER_ASIN_DIR / asin / "amazon.json"
    if not snap_path.exists():
        return None
    try:
        data = json.loads(snap_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    item = data.get("item") if isinstance(data, dict) else None
    return item if isinstance(item, dict) else None


def truncate(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


def main() -> int:
    blocklist_asins = load_blocklist_asins()
    article_asins = collect_article_asins()

    total = len(article_asins)
    skipped_blocklist = 0
    pass_n = 0
    flag_n = 0
    indeterminate_n = 0
    no_data_n = 0
    flag_rows = []

    for asin, filename in article_asins.items():
        if asin in blocklist_asins:
            skipped_blocklist += 1
            continue
        item = load_snapshot_item(asin)
        if item is None or "browse_nodes" not in item:
            no_data_n += 1
            continue
        verdict, cat_nodes = classify_genre(item.get("browse_nodes"), asin)
        if verdict == "pass":
            pass_n += 1
        elif verdict == "indeterminate":
            indeterminate_n += 1
        else:  # flag
            flag_n += 1
            title = truncate(item.get("title") or "", 60)
            cats = ", ".join(f"{nd.get('name')}({nd.get('root')})" for nd in cat_nodes)
            url = f"https://navi.omcha.jp/products/{asin.lower()}/"
            flag_rows.append((asin, title, url, cats, filename))

    judged = pass_n + flag_n + indeterminate_n
    coverage_pct = (judged / total * 100) if total else 0.0

    lines = []
    lines.append("# ジャンル不一致棚卸しレポート (#2823)")
    lines.append("")
    lines.append("## サマリ")
    lines.append("")
    lines.append(f"- 記事総数: {total}")
    lines.append(f"- blocklist 済 (対象外スキップ): {skipped_blocklist}")
    lines.append(f"- browse_nodes カバレッジ (判定可能): {judged} / {total} ({coverage_pct:.1f}%)")
    lines.append(f"  - pass: {pass_n}")
    lines.append(f"  - flag: {flag_n}")
    lines.append(f"  - indeterminate: {indeterminate_n}")
    lines.append(f"- no-data (snapshot 無し / browse_nodes 未蓄積): {no_data_n}")
    lines.append("")
    lines.append("## FLAG (ジャンル不一致の疑いあり — 要人間レビュー)")
    lines.append("")
    if flag_rows:
        lines.append("| ASIN | タイトル | URL | 実カテゴリ | 記事ファイル |")
        lines.append("|---|---|---|---|---|")
        for asin, title, url, cats, filename in flag_rows:
            lines.append(f"| {asin} | {title} | {url} | {cats} | {filename} |")
    else:
        lines.append("(該当なし)")
    lines.append("")

    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
