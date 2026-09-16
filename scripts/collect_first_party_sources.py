"""collect_first_party_sources.py

omcha-ops#264「一次情報の供給経路」設計の A (収集) + D (未収載の報告)。
B (03-invoke-jules への前置) は #7511 で先行実装済み、C (素材化) は別レーン。

omcha.jp (WP REST, read-only GET のみ) の記事本文から、アフィリエイトリンクに
埋め込まれた ASIN を機械的に抽出する。商品名の文字列照合は使わない (誤りが
多いことが手作業検証で判明済み — グリム積み木・ラーニングリソーシズが本文に
一度も出てこないのに一致した)。

処理の流れ:
  1. WP REST から記事一覧 (id/link/title/modified) を取得
  2. 前回 (``--out`` の既存ファイル) と ``modified`` が変わった記事だけ本文を
     個別取得する (毎回 823 記事を引かない)。1 秒 1 リクエスト
  3. 本文から ASIN (``/dp/``・``/gp/product/``・``asin=`` のリンク)・一人称
     マーカー数・自前画像数を抽出する
  4. ASIN ごとの役割 (primary/compared) を post 単位で判定する:
       - navi 側カタログの商品名が記事タイトルに含まれる → title_match
       - それ以外で本文中の出現回数が post 内最多 かつ その記事の商品リンクに
         占める割合が ``PRIMARY_SHARE_FLOOR`` 以上 → mention_share
       - どちらでもない → compared (role_reason は mention_share)
  5. ``data/analytics/first_party_sources.json`` に asin ごとの役割・navi 側の
     状態 (in_corpus/has_article/has_experience) を書き出す。D (未収載の報告:
     primary かつ ``has_article=false`` の一覧) は同ファイル内の
     ``uncatalogued`` セクション
  6. ``role=primary`` かつ ``has_article=false`` の
     ASIN を ``data/raw/first_party_pool.json`` (``{"asins": [...]}`` 形式、
     ``data/raw/ranking_pool.json`` と同形) に書き出す。03-invoke-jules 側の
     前置 (#7511) がこれを消費する

Issue: https://github.com/omochairo/omcha-ops/issues/264 (設計コメント)
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import re
import time
from datetime import datetime, timezone
from typing import Any

import requests

from scripts.audit_query_entailment import discover_articles
from scripts.build_wp_navi_link_candidates import (
    DEFAULT_MAX_PAGES,
    DEFAULT_PER_PAGE,
    DEFAULT_SLEEP_SECONDS,
    DEFAULT_WP_BASE_URL,
    REQUEST_TIMEOUT,
    WP_USER_AGENT,
    _fetch_wp_page,
    strip_html,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("collect_first_party_sources")

DEFAULT_OUT = "data/analytics/first_party_sources.json"
DEFAULT_POOL_OUT = "data/raw/first_party_pool.json"
DEFAULT_ARTICLES_DIR = "data/articles"
DEFAULT_AMAZON_JSON = "data/raw/amazon.json"
DEFAULT_PER_ASIN_DIR = "data/raw/per_asin"

_CONTENT_MAX_EXTRA_RETRIES = 2
_CONTENT_RETRY_SLEEP_SECONDS = 2.0

# primary と認めるのに必要な「その記事の商品リンクに占める割合」の下限。
# 閾値の意味は「同数で並ぶ商品を何個まで primary と認めるか」で、n 商品が同数なら
# share は 1/n (2 商品=0.50 / 3 商品=0.33 / 6 商品=0.17)。0.40 は「同数なら最大
# 2 商品まで」を、正解の真上に乗らない余裕付きで表す。
#
# 実測 2026-09-17 の較正 (母艦が手作業で作った 8 ASIN を正解とする):
#   floor  primary ASIN  8 件中 primary を得た数
#   1.00      1447            6/8   ← 設計どおり「最多なら全部」。772 記事全部が primary を出す
#   0.33       461            6/8
#   0.40       219            6/8   ← 到達可能な上限を満たす最も狭い点
#   0.50       159            5/8   ← B0DT15VQF8 (最大 share 0.49) を落とす
# 8/8 にはどの閾値でもならない。B09WRH12JL は omcha.jp のどの記事にも出てこず、
# B01N2PLTVL はどの記事でも最多にならないため、出現回数ベースでは到達できない。
PRIMARY_SHARE_FLOOR = 0.4

# 記事一覧が前回の何割を下回ったら「途中で切れた」とみなすか (assert_not_truncated)。
# omcha.jp の記事は積み上がる一方で、週次で 1 割減ることは通常ありえない。
TRUNCATION_FLOOR_RATIO = 0.9

# 一人称マーカー (運営者自身の実体験の兆候)。設計コメントの固定リスト。
FP_MARKERS: tuple[str, ...] = ("うちの子", "我が家", "息子", "娘", "買って", "使って", "実際に")
OWN_IMAGE_MARKER = "omcha.jp/wp-content/uploads"

_ASIN_LINK_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"/dp/([A-Za-z0-9]{10})", re.IGNORECASE),
    re.compile(r"/gp/product/([A-Za-z0-9]{10})", re.IGNORECASE),
    re.compile(r"[?&]asin=([A-Za-z0-9]{10})", re.IGNORECASE),
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_json(path: pathlib.Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# --------------------------------------------------------------------------
# 本文からの抽出 (pure functions)
# --------------------------------------------------------------------------

def extract_asin_mentions(content_html: str) -> dict[str, int]:
    """本文 HTML からアフィリエイトリンクの ASIN と出現回数を抽出する。"""
    counts: dict[str, int] = {}
    if not isinstance(content_html, str) or not content_html:
        return counts
    for pattern in _ASIN_LINK_PATTERNS:
        for m in pattern.finditer(content_html):
            asin = m.group(1).upper()
            counts[asin] = counts.get(asin, 0) + 1
    return counts


def count_fp_markers(content_html: str) -> int:
    """一人称マーカーの出現回数の合計 (可視テキストで数える)。"""
    text = strip_html(content_html)
    return sum(text.count(marker) for marker in FP_MARKERS)


def count_own_images(content_html: str) -> int:
    """自前画像 (omcha.jp/wp-content/uploads) の出現回数。"""
    if not isinstance(content_html, str):
        return 0
    return content_html.count(OWN_IMAGE_MARKER)


def determine_roles(
    asin_counts: dict[str, int], post_title: str, catalog_titles: dict[str, str]
) -> dict[str, tuple[str, str]]:
    """post 内の各 ASIN について (role, role_reason) を判定する。

    - catalog (navi 側の商品名) が post タイトルに含まれる → primary/title_match
    - それ以外で post 内の出現回数が最多 **かつ** その記事の商品リンクに占める割合が
      ``PRIMARY_SHARE_FLOOR`` 以上 → primary/mention_share
    - どちらでもない → compared/mention_share

    設計 (#264) は「出現回数が最多」だけだったが、実測 2026-09-17 で 772 記事
    **すべて**が primary を出した。商品紹介ですらないテーマパークの記事で 6 商品が
    各 4 回 (カードリンクの定型) 並び、6 件とも primary になっていた。
    share の下限を足すと、レビュー対象 1 件と 2 商品の同数比較だけが残る。
    較正は ``PRIMARY_SHARE_FLOOR`` のコメントを参照。
    """
    if not asin_counts:
        return {}
    max_count = max(asin_counts.values())
    total = sum(asin_counts.values())
    normalized_title = (post_title or "").strip()
    result: dict[str, tuple[str, str]] = {}
    for asin, count in asin_counts.items():
        catalog_title = (catalog_titles.get(asin) or "").strip()
        share = count / total if total else 0.0
        if catalog_title and normalized_title and catalog_title in normalized_title:
            result[asin] = ("primary", "title_match")
        elif count == max_count and share >= PRIMARY_SHARE_FLOOR:
            result[asin] = ("primary", "mention_share")
        else:
            result[asin] = ("compared", "mention_share")
    return result


def _catalog_short_name(title: str, product_name: str) -> str:
    """タイトル一致判定用の短い商品名を作る (product.name 優先、無ければ title の｜前半)。"""
    if product_name and product_name.strip():
        return product_name.strip()
    if not title:
        return ""
    return title.split("｜", 1)[0].strip()


def build_asin_title_catalog(articles_dir: pathlib.Path, amazon_json_path: pathlib.Path) -> dict[str, str]:
    """asin -> タイトル一致判定用の短い商品名。amazon.json (title) を articles (product.name) で上書きする。"""
    catalog: dict[str, str] = {}
    for item in load_amazon_items(amazon_json_path):
        asin = item.get("asin")
        title = item.get("title")
        if isinstance(asin, str) and isinstance(title, str) and title.strip():
            catalog[asin.upper()] = _catalog_short_name(title, "")

    article_index = discover_articles(articles_dir)
    for asin, path in article_index.items():
        data = _load_json(path)
        if not isinstance(data, dict):
            continue
        product = data.get("product") if isinstance(data.get("product"), dict) else {}
        product_name = product.get("name") if isinstance(product.get("name"), str) else ""
        title = data.get("title") if isinstance(data.get("title"), str) else ""
        name = _catalog_short_name(title, product_name)
        if name:
            catalog[asin.upper()] = name
    return catalog


def load_amazon_items(amazon_json_path: pathlib.Path) -> list[dict[str, Any]]:
    data = _load_json(amazon_json_path)
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return [i for i in data["items"] if isinstance(i, dict)]
    return []


def load_in_corpus_asins(amazon_json_path: pathlib.Path) -> set[str]:
    """navi の商品プール (data/raw/amazon.json の items[].asin)。"""
    asins: set[str] = set()
    for item in load_amazon_items(amazon_json_path):
        asin = item.get("asin")
        if isinstance(asin, str) and asin.strip():
            asins.add(asin.upper())
    return asins


def load_has_experience_asins(per_asin_dir: pathlib.Path) -> set[str]:
    if not per_asin_dir.exists():
        return set()
    return {p.parent.name for p in per_asin_dir.glob("*/experience.json")}


# --------------------------------------------------------------------------
# WP REST 取得 (read-only. GET のみ)
# --------------------------------------------------------------------------

def parse_post_list_entry(raw: Any) -> dict[str, Any] | None:
    """WP REST の 1 記事 raw dict を ``{id, link, title, modified}`` に正規化する。"""
    if not isinstance(raw, dict):
        return None
    post_id = raw.get("id")
    link = raw.get("link")
    title_field = raw.get("title")
    modified = raw.get("modified")
    title = strip_html(title_field.get("rendered") if isinstance(title_field, dict) else "")
    if (
        not isinstance(post_id, int)
        or not isinstance(link, str)
        or not link.strip()
        or not title
        or not isinstance(modified, str)
        or not modified.strip()
    ):
        return None
    return {"id": post_id, "link": link.strip(), "title": title, "modified": modified}


def fetch_post_list(
    wp_base_url: str,
    session: requests.Session,
    *,
    per_page: int = DEFAULT_PER_PAGE,
    sleep_seconds: float = DEFAULT_SLEEP_SECONDS,
    max_pages: int = DEFAULT_MAX_PAGES,
    limit: int = 0,
    sleeper=time.sleep,
) -> list[dict[str, Any]]:
    """omcha.jp の記事一覧を id/link/title/modified で全件取得する (低レート)。"""
    url = f"{wp_base_url.rstrip('/')}/wp-json/wp/v2/posts"
    headers = {"User-Agent": WP_USER_AGENT}
    posts: list[dict[str, Any]] = []
    page = 1
    while page <= max_pages:
        params = {
            "status": "publish",
            "per_page": per_page,
            "page": page,
            "_fields": "id,link,title,modified",
        }
        raw_list = _fetch_wp_page(url, params, session, headers, sleeper=sleeper)
        if not raw_list:
            break

        for raw in raw_list:
            parsed = parse_post_list_entry(raw)
            if parsed is not None:
                posts.append(parsed)
            if limit and limit > 0 and len(posts) >= limit:
                return posts[:limit]

        if len(raw_list) < per_page:
            break
        page += 1
        if sleep_seconds > 0:
            sleeper(sleep_seconds)
    return posts


def fetch_post_content(
    post_id: int, wp_base_url: str, session: requests.Session, sleeper=time.sleep
) -> str | None:
    """1 記事の本文 (content.rendered) を取得する。失敗時は None。"""
    url = f"{wp_base_url.rstrip('/')}/wp-json/wp/v2/posts/{post_id}"
    headers = {"User-Agent": WP_USER_AGENT}
    params = {"_fields": "id,content"}
    attempts = _CONTENT_MAX_EXTRA_RETRIES + 1
    last_err: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            resp = session.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            payload = resp.json()
            content_field = payload.get("content") if isinstance(payload, dict) else None
            rendered = content_field.get("rendered") if isinstance(content_field, dict) else None
            return rendered if isinstance(rendered, str) else ""
        except (requests.RequestException, ValueError) as e:
            last_err = e
            if attempt < attempts:
                logger.warning(
                    "WP post %s content fetch failed (attempt %d/%d): %s; retrying",
                    post_id, attempt, attempts, e,
                )
                sleeper(_CONTENT_RETRY_SLEEP_SECONDS)
            else:
                logger.error("WP post %s content fetch failed after %d attempt(s): %s", post_id, attempts, e)
    return None


# --------------------------------------------------------------------------
# 収集本体
# --------------------------------------------------------------------------

def load_previous_output(out_path: pathlib.Path) -> dict[str, Any]:
    data = _load_json(out_path)
    return data if isinstance(data, dict) else {}


def collect(
    wp_base_url: str,
    session: requests.Session,
    previous: dict[str, Any],
    *,
    articles_dir: pathlib.Path,
    amazon_json_path: pathlib.Path,
    per_asin_dir: pathlib.Path,
    sleep_seconds: float = DEFAULT_SLEEP_SECONDS,
    limit: int = 0,
    sleeper=time.sleep,
) -> dict[str, Any]:
    posts = fetch_post_list(wp_base_url, session, sleep_seconds=sleep_seconds, limit=limit, sleeper=sleeper)
    prev_cache = previous.get("posts_cache") if isinstance(previous.get("posts_cache"), dict) else {}

    posts_cache: dict[str, Any] = {}
    for post in posts:
        pid = str(post["id"])
        cached = prev_cache.get(pid) if isinstance(prev_cache.get(pid), dict) else None
        if cached is not None and cached.get("modified") == post["modified"] and isinstance(cached.get("asin_counts"), dict):
            entry = dict(cached)
        else:
            content = fetch_post_content(post["id"], wp_base_url, session, sleeper=sleeper)
            if content is None and cached is not None:
                # フェッチ失敗: 前回キャッシュを維持する (modified は古いまま残し次回再取得を狙う)
                entry = dict(cached)
            else:
                content = content or ""
                entry = {
                    "modified": post["modified"],
                    "asin_counts": extract_asin_mentions(content),
                    "fp_markers": count_fp_markers(content),
                    "own_images": count_own_images(content),
                }
            if sleep_seconds > 0:
                sleeper(sleep_seconds)
        entry["link"] = post["link"]
        entry["title"] = post["title"]
        posts_cache[pid] = entry

    catalog_titles = build_asin_title_catalog(articles_dir, amazon_json_path)
    in_corpus_set = load_in_corpus_asins(amazon_json_path)
    has_article_set = set(discover_articles(articles_dir).keys())
    has_experience_set = load_has_experience_asins(per_asin_dir)

    sources: list[dict[str, Any]] = []
    for entry in posts_cache.values():
        asin_counts = entry.get("asin_counts") or {}
        if not asin_counts:
            continue
        roles = determine_roles(asin_counts, entry.get("title", ""), catalog_titles)
        for asin, (role, reason) in roles.items():
            sources.append({
                "asin": asin,
                "role": role,
                "role_reason": reason,
                "post_url": entry.get("link", ""),
                "post_title": entry.get("title", ""),
                "fp_markers": entry.get("fp_markers", 0),
                "own_images": entry.get("own_images", 0),
                "in_corpus": asin in in_corpus_set,
                "has_article": asin in has_article_set,
                "has_experience": asin in has_experience_set,
            })
    sources.sort(key=lambda r: (r["asin"], r["post_url"]))

    return {
        "generated_at": _now_iso(),
        "posts_cache": posts_cache,
        "sources": sources,
        "uncatalogued": build_uncatalogued(sources),
    }


def build_uncatalogued(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """D: navi に記事が無く、どこかの記事で primary の ASIN 一覧 (asin ごとに 1 件)。

    設計 (#264) は ``in_corpus=false`` (navi の商品プールに無い) で絞る想定だったが、
    その出所 ``amazon.json`` は日次 fetch の作業セットなので、実測 2026-09-17 では
    3292 ASIN 中 3280 件が該当してしまい報告として機能しない
    (``build_first_party_pool`` の docstring を参照)。
    owner が見たいのは「実体験があるのに navi に無い」商品なので ``has_article``
    で絞る。さらに ``compared`` (比較対象として並んでいるだけ) を落とす:
    落とさないと実測 2026-09-17 で 2983 件になり報告として読めない。primary だけなら
    140 件。プール (B) と同じ母集団の、post URL 付きの読める版になる。
    全量は ``sources`` にあるので情報は失われない。
    """
    picked: dict[str, dict[str, Any]] = {}
    for record in sources:
        if record["has_article"] or record["role"] != "primary":
            continue
        asin = record["asin"]
        existing = picked.get(asin)
        if existing is None or (record["role"] == "primary" and existing["role"] != "primary"):
            picked[asin] = record
    return [
        {
            "asin": r["asin"],
            "role": r["role"],
            "role_reason": r["role_reason"],
            "post_url": r["post_url"],
            "post_title": r["post_title"],
        }
        for r in sorted(picked.values(), key=lambda r: r["asin"])
    ]


def build_first_party_pool(sources: list[dict[str, Any]]) -> list[str]:
    """B が消費する対象: role=primary かつ 未記事化。

    設計 (#264) は ``in_corpus=true`` も条件に入れていたが、``in_corpus`` の出所
    ``data/raw/amazon.json`` は日次 fetch の作業セット (実測 2026-09-17 で 82 件)
    であって navi の恒久カタログ (記事 3272 件) ではない。この条件を課すとプールは
    「今日たまたま fetch に居た 5 件」になり、週ごとに揺れて意図を果たさない。

    消費側 (#7511) は first_party_pool を候補列に **直接前置** するので、
    amazon.json の items[] に居る必要はない (``rewrite_first`` に同じ前例がある)。
    ``in_corpus`` は情報として出力には残す。
    """
    asins = {r["asin"] for r in sources if r["role"] == "primary" and not r["has_article"]}
    return sorted(asins)


class TruncatedCollectionError(RuntimeError):
    """記事一覧が前回より大幅に減った (= 途中で取得が切れた疑い)。"""


def assert_not_truncated(
    previous: dict[str, Any], result: dict[str, Any], *, limit: int = 0,
) -> None:
    """途中で切れた収集結果を書き出さない。

    ``_fetch_wp_page`` はリトライ上限に達しても ``None`` を返す (400 の終端と
    区別が付かない) ので、ページングが途中で静かに打ち切られうる。出力は
    ``posts_cache`` ごと丸ごと置き換わるため、そのまま書くと (a) 欠けた記事の
    キャッシュが消えて次回 823 件を引き直す、(b) ``first_party_pool.json`` が
    縮んだまま auto-merge される。どちらも静かに起きるので、ここで落とす。

    ``--limit`` 指定時 (スモーク) は件数が減って当然なので判定しない。
    """
    if limit and limit > 0:
        return
    prev_count = len(previous.get("posts_cache") or {})
    new_count = len(result.get("posts_cache") or {})
    if prev_count <= 0:
        return
    floor = int(prev_count * TRUNCATION_FLOOR_RATIO)
    if new_count < floor:
        raise TruncatedCollectionError(
            f"post list shrank from {prev_count} to {new_count} "
            f"(floor {floor} = {TRUNCATION_FLOOR_RATIO:.0%}); refusing to overwrite the cache"
        )


def write_json(path: pathlib.Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run(
    *,
    wp_base_url: str = DEFAULT_WP_BASE_URL,
    out_path: pathlib.Path,
    pool_path: pathlib.Path,
    articles_dir: pathlib.Path,
    amazon_json_path: pathlib.Path,
    per_asin_dir: pathlib.Path,
    sleep_seconds: float = DEFAULT_SLEEP_SECONDS,
    limit: int = 0,
    session: requests.Session | None = None,
    sleeper=time.sleep,
) -> dict[str, Any]:
    session = session or requests.Session()
    previous = load_previous_output(out_path)
    result = collect(
        wp_base_url,
        session,
        previous,
        articles_dir=articles_dir,
        amazon_json_path=amazon_json_path,
        per_asin_dir=per_asin_dir,
        sleep_seconds=sleep_seconds,
        limit=limit,
        sleeper=sleeper,
    )
    assert_not_truncated(previous, result, limit=limit)
    write_json(out_path, result)

    pool = build_first_party_pool(result["sources"])
    write_json(pool_path, {"asins": pool, "generated_at": result["generated_at"]})

    logger.info(
        "collected: %d posts, %d source records, %d uncatalogued, pool=%d",
        len(result["posts_cache"]), len(result["sources"]), len(result["uncatalogued"]), len(pool),
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wp-base-url", default=DEFAULT_WP_BASE_URL)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--pool-out", default=DEFAULT_POOL_OUT)
    parser.add_argument("--articles-dir", default=DEFAULT_ARTICLES_DIR)
    parser.add_argument("--amazon-json", default=DEFAULT_AMAZON_JSON)
    parser.add_argument("--per-asin-dir", default=DEFAULT_PER_ASIN_DIR)
    parser.add_argument("--sleep-seconds", type=float, default=DEFAULT_SLEEP_SECONDS)
    parser.add_argument("--limit", type=int, default=0, help="記事一覧の取得件数上限 (スモーク用, 0=無制限)")
    args = parser.parse_args()

    run(
        wp_base_url=args.wp_base_url,
        out_path=pathlib.Path(args.out),
        pool_path=pathlib.Path(args.pool_out),
        articles_dir=pathlib.Path(args.articles_dir),
        amazon_json_path=pathlib.Path(args.amazon_json),
        per_asin_dir=pathlib.Path(args.per_asin_dir),
        sleep_seconds=args.sleep_seconds,
        limit=args.limit,
    )


if __name__ == "__main__":
    main()
