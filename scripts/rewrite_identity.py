#!/usr/bin/env python3
"""書き直しの前の題・商品名・公開日を残す台帳 (#165 T16)。

書き直し (12-rewrite-idle-fill が印を付け、Jules が記事 JSON を
``data/articles/{今日}-{ASIN}.json`` として丸ごと作り直す) では、次の 3 つが同時に変わる:

1. Jules が ``title`` と ``product.name`` (H2 にも入る) を書き直す
2. ``date`` が新しくなるので ``where_to_buy_format.is_stock_format_eligible`` を満たし、
   H1 / og:title が「…はどこで買える？在庫と価格を毎日チェック」に差し替わる
3. 旧 slug の ``/posts/<old_slug>/`` の転送が消える

この台帳は書き直しの印を付けた時点の値を残し、build_post.py が表示に使う。

**置き場が marker や旧 JSON ではない理由**: ``rewrite_queue.cleanup_completed`` が
新しい本体の着地後に旧 JSON と marker を消すため。台帳は ``rewrite_ledger.jsonl``
(``rewrite_queue.LEDGER_PATH``) と同じ置き場・同じ流儀の追記専用で、書き換えない。

**読むときは ASIN ごとに最も古い行を使う。** 2 回目の書き直しの印でも行は足すが
(同じ slug の行は足さない)、表示に使うのは初回の値。

Jules の JSON 自体は書き換えない (元の ``product.name`` が 40 字を超えると品質ゲートで
落ちるので、Jules に元の名前を出させない)。上書きは build の中のメモリ上だけ。
"""
from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import rewrite_queue
import seo_title

LEDGER_PATH = "data/analytics/rewrite_identity.jsonl"

_ASIN_RE = re.compile(r"^B0[A-Z0-9]{8}$")
_SLUG_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-(B0[A-Z0-9]{8})$")
# rewrite_ledger の old_slug には日付しか無い。記事 JSON の date (JST の ISO 時刻) と
# 書式を揃えるため、その日の 0 時 (JST) として扱う。
_SLUG_DATE_TIME = "T00:00:00+09:00"


@dataclass
class Identity:
    """build が使う、その ASIN の書き直し前の値。"""

    asin: str
    title: str = ""
    product_name: str = ""
    # 台帳の最古の行の date (初回の公開日)
    recorded_date: str = ""
    # 初回の公開日。台帳の date と rewrite_ledger の old_slug の日付のうち古いほう
    first_date: str = ""
    # 印を付けた時点の <title> 構成条件。古い台帳行では None のまま。
    has_review_signals: bool | None = None
    has_target_age: bool | None = None
    # 台帳に出てくる slug (転送に足す)
    slugs: list[str] = field(default_factory=list)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_jsonl(path: str) -> list[dict]:
    rows: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(r, dict):
                    rows.append(r)
    except OSError:
        return []
    return rows


def displayed_title(data: dict) -> str:
    """build_post._frontmatter_meta が在庫型でないときに出す題 (title_variants[0] が優先)。"""
    variants = data.get("title_variants") or []
    if variants and isinstance(variants[0], dict) and variants[0].get("title"):
        return str(variants[0]["title"])
    return str(data.get("title") or "")


def identity_row(asin: str, slug: str, data: dict, recorded_at: str | None = None) -> dict | None:
    """記事 JSON (と seo サイドカーを合わせた dict) から台帳の 1 行を作る。題も商品名も無ければ None。"""
    product = data.get("product") if isinstance(data.get("product"), dict) else {}
    title = displayed_title(data)
    product_name = str(product.get("name") or "")
    if not title and not product_name:
        return None
    return {
        "asin": asin,
        "slug": slug,
        "date": str(data.get("date") or ""),
        "title": title,
        "product_name": product_name,
        "has_review_signals": seo_title._has_review_signals(data),
        "has_target_age": seo_title._has_target_age(data),
        "recorded_at": recorded_at or _now(),
    }


def _load_article(articles_dir: str, slug: str) -> dict | None:
    path = os.path.join(articles_dir, f"{slug}.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    # build_post は seo サイドカーの title_variants も合わせて使う (JSON 側に無いときだけ)
    try:
        with open(os.path.join(articles_dir, f"{slug}.seo.json"), encoding="utf-8") as f:
            seo = json.load(f)
        if isinstance(seo, dict) and "title_variants" in seo and "title_variants" not in data:
            data["title_variants"] = seo["title_variants"]
    except (OSError, json.JSONDecodeError):
        pass
    return data


def record(
    asin: str,
    slug: str,
    articles_dir: str = "data/articles",
    path: str = LEDGER_PATH,
    recorded_at: str | None = None,
) -> bool:
    """``articles_dir/<slug>.json`` の今の値を台帳に 1 行足す。足したら True。

    同じ (asin, slug) の行が既にあれば足さない (印の付け直しで行を増やさない)。
    記事が読めない・題も商品名も無いときも足さない。
    """
    if not slug or not _ASIN_RE.match(asin or ""):
        return False
    for r in _read_jsonl(path):
        if r.get("asin") == asin and r.get("slug") == slug:
            return False
    data = _load_article(articles_dir, slug)
    if data is None:
        return False
    row = identity_row(asin, slug, data, recorded_at)
    if row is None:
        return False
    out_dir = os.path.dirname(path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return True


def ensure_pending(
    queue_dir: str = rewrite_queue.QUEUE_DIR,
    articles_dir: str = "data/articles",
    path: str = LEDGER_PATH,
) -> int:
    """印の付いた ASIN のうち、台帳にまだ無いものを旧本体から記録する。足した行数を返す。

    この仕組みより前に付いた印 (台帳の行が無い) を、旧本体が cleanup で消える前に拾う。
    12-rewrite-idle-fill の cleanup の前に回す。
    """
    added = 0
    for asin, marker in sorted(rewrite_queue.load_markers(queue_dir).items()):
        old_slug = marker.get("old_slug") if isinstance(marker, dict) else ""
        if old_slug and record(asin, old_slug, articles_dir, path):
            added += 1
    return added


def _slug_date(slug: str) -> str:
    m = _SLUG_RE.match(slug or "")
    return m.group(1) if m else ""


def load_old_slugs(rewrite_ledger_path: str = rewrite_queue.LEDGER_PATH) -> dict[str, list[str]]:
    """``rewrite_ledger.jsonl`` から ASIN ごとの過去の old_slug を出てきた順に (重複なし) 返す。"""
    out: dict[str, list[str]] = {}
    for r in _read_jsonl(rewrite_ledger_path):
        asin = r.get("asin")
        slug = r.get("old_slug")
        if not isinstance(asin, str) or not isinstance(slug, str) or not _slug_date(slug):
            continue
        lst = out.setdefault(asin, [])
        if slug not in lst:
            lst.append(slug)
    return out


def load(
    path: str = LEDGER_PATH,
    old_slugs: dict[str, list[str]] | None = None,
) -> dict[str, Identity]:
    """台帳を ``{asin: Identity}`` に読む。台帳に行がある ASIN だけが入る。

    題・商品名・在庫型の判定に使う日付は ASIN ごとに最初に追記された行。
    初回の公開日は、台帳の date と ``old_slugs`` (rewrite_ledger の
    old_slug) の日付のうち古いほう。
    """
    by_asin: dict[str, list[tuple[int, dict]]] = {}
    for i, r in enumerate(_read_jsonl(path)):
        asin = r.get("asin")
        if not isinstance(asin, str) or not _ASIN_RE.match(asin):
            continue
        by_asin.setdefault(asin, []).append((i, r))
    out: dict[str, Identity] = {}
    for asin, rows in by_asin.items():
        first = rows[0][1]
        ident = Identity(
            asin=asin,
            title=str(first.get("title") or ""),
            product_name=str(first.get("product_name") or ""),
            recorded_date=str(first.get("date") or ""),
            has_review_signals=(first.get("has_review_signals")
                                if isinstance(first.get("has_review_signals"), bool) else None),
            has_target_age=(first.get("has_target_age")
                            if isinstance(first.get("has_target_age"), bool) else None),
        )
        # 初回の公開日: 日付部分 (先頭 10 字) で比べ、最も古いものの元の書式を使う
        cands: list[str] = [str(r.get("date") or "") for _, r in rows]
        cands += [d + _SLUG_DATE_TIME for d in map(_slug_date, (old_slugs or {}).get(asin, [])) if d]
        cands = [c for c in cands if len(c) >= 10]
        if cands:
            ident.first_date = min(cands, key=lambda c: c[:10])
        for _, r in rows:
            s = r.get("slug")
            if isinstance(s, str) and s and s not in ident.slugs:
                ident.slugs.append(s)
        out[asin] = ident
    return out


def apply(data: dict, ident: Identity) -> None:
    """記事 dict (メモリ上) の題・商品名・公開日を書き直し前の値にする。ファイルは書き換えない。

    title_variants / h1_recommendation は落とす (_frontmatter_meta で title_variants[0] が
    title を上書きするため。h1_recommendation は今は読まれていないが、同じ経路を塞いでおく)。
    """
    if ident.title:
        data["title"] = ident.title
        data.pop("title_variants", None)
        data.pop("h1_recommendation", None)
    product = data.get("product")
    if ident.product_name and isinstance(product, dict):
        product["name"] = ident.product_name
        jsonld = data.get("jsonld")
        if isinstance(jsonld, dict) and isinstance(jsonld.get("product"), dict):
            jsonld["product"]["name"] = ident.product_name
    if ident.first_date:
        data["date"] = ident.first_date


def old_slug_aliases(
    asin: str,
    current_slug: str,
    old_slugs: dict[str, list[str]],
    ident: Identity | None = None,
) -> list[str]:
    """その ASIN の過去の slug の ``/posts/<slug>/`` (今の slug と重複は除く)。

    rewrite_ledger の old_slug に加えて、この台帳の slug も足す。新しい本体が着地してから
    cleanup が rewrite_ledger に書くまでの間 (旧本体は描画されない) も転送を残すため。
    """
    slugs = list(old_slugs.get(asin, []))
    if ident is not None:
        slugs += ident.slugs
    out: list[str] = []
    cur = (current_slug or "").lower()
    for s in slugs:
        s = s.lower()
        if s == cur:
            continue
        a = f"/posts/{s}/"
        if a not in out:
            out.append(a)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ensure-pending", action="store_true",
                    help="印の付いた ASIN のうち台帳に無いものを旧本体から記録する (cleanup の前に回す)")
    ap.add_argument("--queue-dir", default=rewrite_queue.QUEUE_DIR)
    ap.add_argument("--articles-dir", default="data/articles")
    ap.add_argument("--path", default=LEDGER_PATH)
    args = ap.parse_args()
    if args.ensure_pending:
        n = ensure_pending(args.queue_dir, args.articles_dir, args.path)
        print(f"[rewrite_identity] ensure_pending recorded={n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
