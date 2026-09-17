"""build_first_party_eval_packet.py

#7569 型6 の評価セット作りの準備: 題名を伏せたレビュー冊子を生成する。

なぜ必要か:
  probe 3 本 (#7584 / #7592 / #7601) は、いずれも母艦が ``post_title`` を読んで
  付けたラベルで評価していた。(A) で信号が出た瞬間に「ラベルの付け方」と
  「測っている特徴量」が同じ操作になっているリークが表面化した (#7569 コメント6)。
  `lcs_ratio` で題名ラベルの書籍 5 件が 0.242〜1.000 (書名が題名にそのまま入っている)、
  本文ラベルの 7 件が 0.051〜0.227。評価セットを本文由来に作り直さない限り、
  (A) の測り直しも次の候補も判断材料にならない。

  **判定そのものはやらない。母艦が判定できる形の冊子を作るところまで。**
  ラベルを推測して埋めない。判定欄は空で出す。

設計判断:
  - 対象は data/raw/first_party_pool.json の B0 形式 159 件から無作為抽出した 40 件 +
    #7568 で母艦が本文レビューして採用済みの既知正例 7 件 (較正用)。既に題名ラベルを
    付けた 52 件 (NEGATIVE_ASINS) を除外しない (除外すると母集団が歪む)
  - 抽出は再現可能にする (random.Random(seed))。seed は出力キーに記録する
  - 冊子の中では item-01〜item-47 の不透明な連番だけを使い、順序はシャッフルする
    (どれが較正用かを母艦に分からせない)
  - post_title / post_url / 既存の題名ラベルは冊子に載せない。本文中に post_title と
    完全一致する文字列 (｜や【】で切った前半のみの一致を含む) が出たら削除し、
    件数を記録する (完全な遮断は不可能なので、どれだけ残りうるかを分かるようにする)
  - 商品名候補の抽出は probe_first_party_title_match.extract_product_name_candidates を
    import して再利用する (複製しない)。伏せない (「この記事はこの商品をレビューして
    いるか」を訊くのに必要)
  - 本文取得は probe_first_party_link_placement.fetch_contents_cached と同じ作法。
    既存の /tmp キャッシュ (#7592/#7601 が作ったもの) を使い、omcha.jp を再取得しない
  - collect_first_party_sources.py / 既存 probe 3 本は変更しない (import するだけ)
  - data/ には何も書かない・コミットしない。既定の出力先は /tmp 配下
  - LLM を使わない。判定欄は常に空文字列で出す

使い方:
    python -m scripts.build_first_party_eval_packet --limit 5      # スモーク
    python -m scripts.build_first_party_eval_packet                # フル実行 (47件)
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import random
import time
from typing import Any

import requests

from scripts.build_wp_navi_link_candidates import (
    DEFAULT_SLEEP_SECONDS,
    DEFAULT_WP_BASE_URL,
    strip_html,
)
from scripts.collect_first_party_sources import POOL_ASIN_RE, fetch_post_content
from scripts.probe_first_party_link_placement import (
    DEFAULT_CACHE,
    fetch_contents_cached,
    load_cache,
    save_cache,
)
from scripts.probe_first_party_link_proximity import (
    NEGATIVE_ASINS,
    POSITIVE_ASINS,
    build_link_to_post_id,
    find_asin_link_spans,
)
from scripts.probe_first_party_title_match import extract_product_name_candidates

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("build_first_party_eval_packet")

DEFAULT_SOURCES_PATH = "data/analytics/first_party_sources.json"
DEFAULT_POOL_PATH = "data/raw/first_party_pool.json"
DEFAULT_OUT_PACKET = "/tmp/first_party_eval_packet.md"
DEFAULT_OUT_KEY = "/tmp/first_party_eval_key.json"
# #7592/#7601 が残した /tmp キャッシュをそのまま使う (再取得しない)
DEFAULT_CACHE_PATH = DEFAULT_CACHE

DEFAULT_SEED = 7569
DEFAULT_SAMPLE_SIZE = 40

MAX_BODY_CHARS = 2000
HEAD_CHARS = 1200
WINDOW_CHARS = 800
ELLIPSIS = "…（中略）…"
REDACTION_MARKER = "［伏字］"

# 較正用正例 (#7568 で母艦が本文レビューして採用済み。POSITIVE_ASINS のうち B0 形式のみ = 7件)
CALIBRATION_ASINS: tuple[str, ...] = tuple(a for a in POSITIVE_ASINS if POOL_ASIN_RE.match(a))
# 既に題名ラベル (ii) を付けた 52 件 (NEGATIVE_ASINS そのもの。定義を複製しない)
TITLE_LABELED_ASINS: frozenset[str] = frozenset(NEGATIVE_ASINS)


# --------------------------------------------------------------------------
# 抽出母集団・順序 (pure functions。ネットワークを叩かない)
# --------------------------------------------------------------------------

def load_b0_pool(pool_path: pathlib.Path) -> list[str]:
    """first_party_pool.json から B0 形式の ASIN だけを重複除去・ソートして返す。"""
    data = json.loads(pool_path.read_text(encoding="utf-8"))
    asins = data.get("asins") or []
    return sorted({a for a in asins if isinstance(a, str) and POOL_ASIN_RE.match(a)})


def sample_pool_asins(pool_asins: list[str], sample_size: int, rng: random.Random) -> list[str]:
    """母集団から無作為抽出する (母集団より大きい要求は母集団数に丸める)。"""
    n = min(sample_size, len(pool_asins))
    return rng.sample(pool_asins, n)


def build_item_order(
    pool_sample: list[str], calibration_asins: tuple[str, ...], rng: random.Random
) -> list[str]:
    """無作為抽出分 + 較正用正例を結合し、シャッフルした順序を返す。"""
    combined = list(pool_sample) + list(calibration_asins)
    rng.shuffle(combined)
    return combined


def classify_prior_label(asin: str) -> str:
    """asin -> 事前ラベルの種別 (較正用正例 / 既存の題名ラベル (ii) / 未ラベル)。"""
    if asin in CALIBRATION_ASINS:
        return "calibration_positive"
    if asin in TITLE_LABELED_ASINS:
        return "title_labeled_ii"
    return "unlabeled"


# --------------------------------------------------------------------------
# post の引き当て (asin -> role=primary の post。post_title/post_url は伏せる側で使う)
# --------------------------------------------------------------------------

def index_sources_by_asin(sources: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for row in sources:
        asin = row.get("asin")
        if asin:
            out.setdefault(asin, []).append(row)
    return out


def pick_primary_post(
    sources_by_asin: dict[str, list[dict[str, Any]]], asin: str
) -> dict[str, Any] | None:
    """role=primary の post のうち post_url 昇順で最初の 1 件を返す (複数ある場合の決定的な選び方)。"""
    rows = [
        r for r in sources_by_asin.get(asin, [])
        if r.get("role") == "primary" and r.get("post_url")
    ]
    if not rows:
        return None
    return sorted(rows, key=lambda r: r["post_url"])[0]


# --------------------------------------------------------------------------
# post_title の伏せ字化 (pure functions)
# --------------------------------------------------------------------------

def title_redaction_candidates(post_title: str | None) -> list[str]:
    """post_title 本体 + ｜/【】 で切った前半を、長い順・重複除去で返す。"""
    t = (post_title or "").strip()
    if not t:
        return []
    candidates = [t]
    if "｜" in t:
        prefix = t.split("｜", 1)[0].strip()
        if prefix and prefix != t:
            candidates.append(prefix)
    if "【" in t and "】" in t:
        open_idx, close_idx = t.index("【"), t.index("】")
        if open_idx < close_idx:
            bracket = t[open_idx:close_idx + 1]
            if bracket and bracket != t:
                candidates.append(bracket)
    seen: set[str] = set()
    out: list[str] = []
    for c in sorted(set(candidates), key=len, reverse=True):
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def redact_title_occurrences(
    text: str, candidates: list[str], marker: str = REDACTION_MARKER
) -> tuple[str, int]:
    """text から candidates (長い順) に完全一致する箇所を marker に置き換え、削除件数を返す。"""
    total = 0
    for cand in candidates:
        if not cand:
            continue
        count = text.count(cand)
        if count:
            total += count
            text = text.replace(cand, marker)
    return text, total


# --------------------------------------------------------------------------
# 本文の切り出し (先頭1200文字 + 対象ASINリンク最初の出現位置の前後800文字)
# --------------------------------------------------------------------------

def first_link_offset_in_plain(content_html: str, asin: str) -> int | None:
    """対象 ASIN リンクが最初に出る位置を、strip_html 後のプレーンテキスト上の近似オフセットで返す。"""
    spans = find_asin_link_spans(content_html, asin)
    if not spans:
        return None
    start = spans[0][0]
    return len(strip_html(content_html[:start]))


def truncate_body(
    full_text: str,
    link_offset: int | None,
    *,
    cap: int = MAX_BODY_CHARS,
    head: int = HEAD_CHARS,
    window: int = WINDOW_CHARS,
    ellipsis: str = ELLIPSIS,
) -> tuple[str, bool]:
    """2,000 文字を超える本文を、先頭 1,200 文字 + リンク周辺 800 文字に切り出す。"""
    if len(full_text) <= cap:
        return full_text, False
    if link_offset is None:
        return full_text[:cap], False
    window_start = max(0, link_offset - window)
    window_end = min(len(full_text), link_offset + window)
    if window_start <= head:
        # 窓が先頭ブロックと重なる/隣接する場合は中略を挟まず連続させる
        return full_text[:max(head, window_end)], False
    return full_text[:head] + ellipsis + full_text[window_start:window_end], True


def pick_product_name(content_html: str, asin: str) -> str | None:
    """#7601 の抽出規則で商品名候補を取り、最初の 1 件を返す。"""
    candidates = extract_product_name_candidates(content_html, asin)
    return candidates[0] if candidates else None


# --------------------------------------------------------------------------
# 冊子・キーの組み立て
# --------------------------------------------------------------------------

def render_packet(items: list[dict[str, Any]]) -> str:
    lines = [
        "# 型6 評価セット冊子 (#7569 / #7601)",
        "",
        "## 判定の書き方",
        "",
        "各項目の **判定** 欄に次のいずれかを記入してください。",
        "- `review`: この記事はこの商品をレビュー・体験している",
        "- `mention`: 引用・言及されているだけ",
        "- `unclear`: 本文からは判断できない",
        "",
    ]
    for item in items:
        lines.append(f"## {item['item_id']}")
        lines.append("")
        lines.append(f"**対象商品**: {item['product_name'] or '(商品名候補なし)'}")
        lines.append("")
        lines.append(item["body_text"])
        lines.append("")
        lines.append("**判定**: ")
        lines.append("")
    redaction_items = sum(1 for i in items if i["redaction_count"] > 0)
    redaction_total = sum(i["redaction_count"] for i in items)
    lines.append("## 集計")
    lines.append("")
    lines.append(f"- 項目数: {len(items)}")
    lines.append(
        f"- post_title と完全一致する文字列の削除: {redaction_items} 項目 / 合計 {redaction_total} 箇所"
    )
    return "\n".join(lines) + "\n"


def render_key(
    items: list[dict[str, Any]], seed: int, sample_size: int, pool_total: int
) -> dict[str, Any]:
    return {
        "seed": seed,
        "sample_size": sample_size,
        "pool_total": pool_total,
        "post_title_redaction_total_occurrences": sum(i["redaction_count"] for i in items),
        "items": {
            i["item_id"]: {
                "asin": i["asin"],
                "post_url": i["post_url"],
                "post_title": i["post_title"],
                "prior_title_label": i["prior_title_label"],
            }
            for i in items
        },
    }


def write_text(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_json(path: pathlib.Path, data: Any) -> None:
    write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


# --------------------------------------------------------------------------
# 実行
# --------------------------------------------------------------------------

def run(
    *,
    sources_path: pathlib.Path,
    pool_path: pathlib.Path,
    out_packet_path: pathlib.Path,
    out_key_path: pathlib.Path,
    cache_path: pathlib.Path,
    seed: int = DEFAULT_SEED,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    wp_base_url: str = DEFAULT_WP_BASE_URL,
    sleep_seconds: float = DEFAULT_SLEEP_SECONDS,
    limit: int = 0,
    session: requests.Session | None = None,
    sleeper=None,
    fetch_fn=fetch_post_content,
) -> dict[str, Any]:
    sleeper = sleeper or time.sleep
    session = session or requests.Session()

    payload = json.loads(sources_path.read_text(encoding="utf-8"))
    sources = payload.get("sources") or []
    posts_cache = payload.get("posts_cache") or {}

    pool_asins = load_b0_pool(pool_path)
    rng = random.Random(seed)
    pool_sample = sample_pool_asins(pool_asins, sample_size, rng)
    order = build_item_order(pool_sample, CALIBRATION_ASINS, rng)
    if limit and limit > 0:
        order = order[:limit]

    sources_by_asin = index_sources_by_asin(sources)
    link_to_id = build_link_to_post_id(posts_cache)

    resolved: dict[str, dict[str, Any]] = {}
    missing_asins: list[str] = []
    for asin in order:
        row = pick_primary_post(sources_by_asin, asin)
        if row is None or row.get("post_url") not in link_to_id:
            missing_asins.append(asin)
            continue
        resolved[asin] = row

    post_ids = sorted({link_to_id[row["post_url"]] for row in resolved.values()}, key=int)
    cache = load_cache(cache_path)
    contents, failed_ids = fetch_contents_cached(
        post_ids, wp_base_url, session, sleep_seconds, 0, cache, sleeper=sleeper, fetch_fn=fetch_fn,
    )
    save_cache(cache_path, cache)
    if missing_asins:
        logger.warning("post を引き当てられなかった ASIN: %d 件 %s", len(missing_asins), missing_asins)
    if failed_ids:
        logger.warning("本文取得に失敗した post: %d 件 %s", len(failed_ids), failed_ids)

    items: list[dict[str, Any]] = []
    for idx, asin in enumerate(order, start=1):
        item_id = f"item-{idx:02d}"
        row = resolved.get(asin)
        content_html = contents.get(link_to_id[row["post_url"]]) if row else None
        if row is None or content_html is None:
            items.append({
                "item_id": item_id, "asin": asin,
                "post_url": row["post_url"] if row else None,
                "post_title": row.get("post_title") if row else None,
                "product_name": None,
                "body_text": "(本文を取得できなかった: post_url の引き当て、または fetch に失敗)",
                "redaction_count": 0, "truncated": False, "link_found": False,
                "prior_title_label": classify_prior_label(asin),
            })
            continue

        product_name = pick_product_name(content_html, asin)
        full_plain = strip_html(content_html)
        link_offset = first_link_offset_in_plain(content_html, asin)
        body, truncated = truncate_body(full_plain, link_offset)
        title_candidates = title_redaction_candidates(row.get("post_title"))
        body, redaction_count = redact_title_occurrences(body, title_candidates)

        items.append({
            "item_id": item_id, "asin": asin,
            "post_url": row["post_url"], "post_title": row.get("post_title"),
            "product_name": product_name, "body_text": body,
            "redaction_count": redaction_count, "truncated": truncated,
            "link_found": link_offset is not None,
            "prior_title_label": classify_prior_label(asin),
        })

    packet_md = render_packet(items)
    key = render_key(items, seed, sample_size, len(pool_asins))

    write_text(out_packet_path, packet_md)
    write_json(out_key_path, key)

    logger.info(
        "wrote %s / %s (items=%d, missing_asins=%d, fetch_failed=%d, truncated=%d, link_not_found=%d)",
        out_packet_path, out_key_path, len(items), len(missing_asins), len(failed_ids),
        sum(1 for i in items if i["truncated"]),
        sum(1 for i in items if not i["link_found"]),
    )
    return {"items": items, "key": key, "missing_asins": missing_asins, "fetch_failed": failed_ids}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sources", default=DEFAULT_SOURCES_PATH)
    ap.add_argument("--pool", default=DEFAULT_POOL_PATH)
    ap.add_argument("--out-packet", default=DEFAULT_OUT_PACKET)
    ap.add_argument("--out-key", default=DEFAULT_OUT_KEY)
    ap.add_argument("--cache", default=DEFAULT_CACHE_PATH, help="本文取得結果の /tmp キャッシュ")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--sample-size", type=int, default=DEFAULT_SAMPLE_SIZE)
    ap.add_argument("--wp-base-url", default=DEFAULT_WP_BASE_URL)
    ap.add_argument("--sleep-seconds", type=float, default=DEFAULT_SLEEP_SECONDS)
    ap.add_argument(
        "--limit", type=int, default=0,
        help="冊子に載せる項目数の上限 (スモーク用。シャッフル後の先頭から切る。0=無制限)",
    )
    args = ap.parse_args()
    run(
        sources_path=pathlib.Path(args.sources),
        pool_path=pathlib.Path(args.pool),
        out_packet_path=pathlib.Path(args.out_packet),
        out_key_path=pathlib.Path(args.out_key),
        cache_path=pathlib.Path(args.cache),
        seed=args.seed,
        sample_size=args.sample_size,
        wp_base_url=args.wp_base_url,
        sleep_seconds=args.sleep_seconds,
        limit=args.limit,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
