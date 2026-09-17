"""probe_first_party_title_match.py

#7569 型6 の probe 第3弾 (A): 「記事タイトル」と「その ASIN の (記事内での) 商品名」の
一致に分離能があるかを **1 回測る**。

なぜ必要か:
  (C) ASIN リンク近傍の一人称マーカーは却下 (#7584)。(B) ASIN リンクの構造的な置き場所も
  却下 (#7592、理由: リンクの 99.5% が Cocoon 商品ボックス / pochipp の定型出力で、
  レビュー対象も引用もリンクの形が同じ)。#7592 で分かった「ほぼ全リンクが商品ボックスの
  中にある」という事実を逆手に取り、商品ボックスが**表示している商品名**を post_title と
  比べれば、「その記事が主役として名乗っている商品」と「引用しただけの商品」が
  分離できるのではという仮説を測る (#7569 コメント4・5)。

設計判断:
  - **これは計測であって実装ではない**。本番の選別ロジック (`determine_roles` /
    `PRIMARY_SHARE_FLOOR` / `build_first_party_pool`) には一切配線しない
  - ラベル付き集合 (POSITIVE_ASINS / NEGATIVE_ASINS) は
    `probe_first_party_link_proximity` から import する (定義を複製しない)
  - 商品名の取得元は `build_asin_title_catalog` を使わない (負例 52 件中 46 件が
    どこにも商品名を持たず、測定対象の 9 割が欠測になるため。#7569 コメント5)。
    代わりに記事 HTML の ASIN リンクを包む商品ボックス (Cocoon / pochipp) が
    表示している商品名を使う
  - Amazon Creators API を叩かない。外部クォータを使わない
  - `collect_first_party_sources.py` / 既存 probe 2 本は変更しない (import するだけ)
  - 本文取得は `probe_first_party_link_placement` の `fetch_contents_cached` /
    `load_cache` / `save_cache` をそのまま使う。`--cache` の既定値は #7592 が
    残した `/tmp/first_party_link_placement_probe_cache.json` を指す
    (66 post 分が既にキャッシュ済みで、再取得が要らない)
  - 日本語の語分割をしない (カタカナ長音符 "ー" が \\p{Katakana} で割れる事故が
    既知のため)。文字 n-gram (LCS長 / LCS比 / bigram Jaccard) で測る
  - しきい値は提案しない。分布と「全正例を残すときに負例を何件落とせるか」
    「全負例を落とすときに正例が何件残るか」の 2 点だけを出す
  - `data/` には何も書かない・コミットしない。既定の出力先は `/tmp` 配下

商品名候補の抽出規則 (実測して決めた。詳細は EXTRACTION_RULE_NOTES を参照):
  ASIN リンク <a> のうち #7592 の祖先チェーン判定で "block" とされたものだけを対象にする。
  各 <a> について、自分自身を含む祖先を近い順にたどり、タグが <figure>/<table>、または
  class に Cocoon/pochipp の商品ボックス marker
  (amazon-item / product-item / pochipp / shoplinkamazon / swatchimages) を含む
  **最も近いノード** を取る。そのノードの (1) title 属性、(2) 可視テキスト、の非空なものを
  候補とし、CTA 文言 (「Amazonで価格を見る」「楽天で最安値をチェック」「口コミを見る」等) を
  含むものは候補から除く。
  実測 (66 記事) では、この規則だけで商品名候補 (Cocoon の場合は thumb-link/title-link の
  title 属性 or アンカーテキスト、pochipp の場合は `pochipp-box__title` のアンカーテキスト)
  が自然に残り、サムネイル・スウォッチ・購入ボタン・レビューリンクの出現は空文字列
  または CTA 文言として自動的に除外された (特別な "title クラスを探す" 処理は不要だった)。

使い方:
    python -m scripts.probe_first_party_title_match --limit 5      # スモーク
    python -m scripts.probe_first_party_title_match                # フル実行
    python -m scripts.probe_first_party_title_match --dump-names /tmp/title_match_names.json
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import statistics
import unicodedata
from typing import Any

import requests

from scripts.probe_first_party_link_placement import (
    DEFAULT_CACHE,
    _BLOCK_CLASS_MARKERS,
    _BLOCK_TAGS,
    ancestor_chain,
    classify_chain,
    fetch_contents_cached,
    find_asin_link_tags,
    load_cache,
    save_cache,
)
from scripts.probe_first_party_link_proximity import (
    NEGATIVE_ASINS,
    POSITIVE_ASINS,
    _now_iso,
    build_asin_posts,
    build_link_to_post_id,
    collect_post_ids,
    distribution_stats,
    full_drop_specificity,
    full_recall_specificity,
)
from scripts.build_wp_navi_link_candidates import DEFAULT_SLEEP_SECONDS, DEFAULT_WP_BASE_URL
from scripts.collect_first_party_sources import fetch_post_content

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("probe_first_party_title_match")

DEFAULT_SOURCES_PATH = "data/analytics/first_party_sources.json"
DEFAULT_OUT = "/tmp/first_party_title_match_probe.json"
# #7592 のキャッシュをそのまま再利用する (66 post 分、再取得不要)
DEFAULT_TITLE_MATCH_CACHE = DEFAULT_CACHE

METRICS: tuple[str, ...] = ("lcs_len", "lcs_ratio", "bigram_jaccard")

# 実測 (66記事) で確認した CTA 文言。商品名ではないので候補から除く。
CTA_MARKERS: tuple[str, ...] = (
    "で価格を見る", "で最安値をチェック", "口コミを見る", "確認する", "詳細をチェック",
    "Amazonで見る", "楽天で見る",
)

EXTRACTION_RULE_NOTES = (
    "ASIN リンク <a> のうち #7592 の祖先チェーン判定で block と分類されたものだけを対象にする。"
    "自分自身を含む祖先を近い順にたどり、タグが figure/table、または class に "
    "amazon-item/product-item/pochipp/shoplinkamazon/swatchimages のいずれかを含む"
    "最も近いノードを取る。そのノードの title 属性と可視テキストのうち、CTA 文言を含まない"
    "非空の文字列を候補にする。"
)


# --------------------------------------------------------------------------
# 商品名候補の抽出 (pure functions。ネットワークを叩かない)
# --------------------------------------------------------------------------

def find_block_ancestor(a_tag: Any) -> Any | None:
    """<a> 自身を含む祖先を近い順にたどり、#7592 の block 判定条件に一致する最も近いノードを返す。"""
    node = a_tag
    while node is not None and getattr(node, "name", None) not in (None, "[document]"):
        if node.name in _BLOCK_TAGS:
            return node
        classes = node.get("class") if hasattr(node, "get") else None
        classes = classes or []
        if any(marker in c for c in classes for marker in _BLOCK_CLASS_MARKERS):
            return node
        node = node.parent
    return None


def is_cta_text(text: str) -> bool:
    return any(marker in text for marker in CTA_MARKERS)


def candidate_texts_from_node(node: Any) -> list[str]:
    """1 ノードから商品名候補になりうる文字列 (title 属性・可視テキスト) を、CTA を除いて返す。"""
    texts: list[str] = []
    title_attr = node.get("title") if hasattr(node, "get") else None
    if isinstance(title_attr, str) and title_attr.strip():
        texts.append(title_attr.strip())
    own_text = node.get_text(" ", strip=True)
    if own_text:
        texts.append(own_text)
    seen: set[str] = set()
    out: list[str] = []
    for t in texts:
        if t and not is_cta_text(t) and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def extract_product_name_candidates(content_html: str, asin: str) -> list[str]:
    """1 (post, asin) について、商品名候補のリスト (重複除去・出現順) を返す。"""
    candidates: list[str] = []
    seen: set[str] = set()
    for a_tag in find_asin_link_tags(content_html, asin):
        if classify_chain(ancestor_chain(a_tag)) != "block":
            continue
        node = find_block_ancestor(a_tag)
        if node is None:
            continue
        for text in candidate_texts_from_node(node):
            if text not in seen:
                seen.add(text)
                candidates.append(text)
    return candidates


# --------------------------------------------------------------------------
# 正規化・文字 n-gram 指標 (pure functions)
# --------------------------------------------------------------------------

def normalize_for_match(text: str) -> str:
    """NFKC -> 小文字化 -> 英数字・かな・カナ・漢字以外を除去。語分割はしない。"""
    if not isinstance(text, str) or not text:
        return ""
    nfkc = unicodedata.normalize("NFKC", text).lower()
    out = []
    for ch in nfkc:
        code = ord(ch)
        if code == 0x30FB:  # ・ (中黒) はカタカナブロック内だが記号なので除去する
            continue
        if (
            "0" <= ch <= "9" or "a" <= ch <= "z"
            or 0x3040 <= code <= 0x309F  # ひらがな
            or 0x30A0 <= code <= 0x30FF  # カタカナ (長音符 U+30FC を含む)
            or 0x4E00 <= code <= 0x9FFF  # 漢字
        ):
            out.append(ch)
    return "".join(out)


def longest_common_substring_len(a: str, b: str) -> int:
    """2 文字列の最長共通部分文字列 (連続) の長さ。"""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        curr = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                curr[j] = prev[j - 1] + 1
                if curr[j] > best:
                    best = curr[j]
        prev = curr
    return best


def char_bigrams(text: str) -> set[str]:
    if len(text) < 2:
        return {text} if text else set()
    return {text[i:i + 2] for i in range(len(text) - 1)}


def bigram_jaccard(a: str, b: str) -> float:
    set_a, set_b = char_bigrams(a), char_bigrams(b)
    if not set_a and not set_b:
        return 0.0
    union = set_a | set_b
    if not union:
        return 0.0
    return len(set_a & set_b) / len(union)


def match_scores(candidate: str, post_title: str) -> dict[str, float]:
    """1 商品名候補と post_title の一致指標 3 種。"""
    norm_candidate = normalize_for_match(candidate)
    norm_title = normalize_for_match(post_title)
    lcs_len = longest_common_substring_len(norm_candidate, norm_title)
    lcs_ratio = (lcs_len / len(norm_candidate)) if norm_candidate else 0.0
    jaccard = bigram_jaccard(norm_candidate, norm_title)
    return {"lcs_len": lcs_len, "lcs_ratio": lcs_ratio, "bigram_jaccard": jaccard}


def best_scores_for_asin(candidate_scores: list[dict[str, float]]) -> dict[str, float] | None:
    """複数候補があるとき、指標ごとの最大値を返す (#7569 コメント4 の指示どおり)。"""
    if not candidate_scores:
        return None
    return {m: max(s[m] for s in candidate_scores) for m in METRICS}


# --------------------------------------------------------------------------
# post_title の引き当て
# --------------------------------------------------------------------------

def build_post_titles(sources: list[dict[str, Any]]) -> dict[tuple[str, str], str]:
    """(asin, post_url) -> post_title。"""
    out: dict[tuple[str, str], str] = {}
    for row in sources:
        asin = row.get("asin")
        url = row.get("post_url")
        title = row.get("post_title")
        if asin and url and isinstance(title, str) and title:
            out[(asin, url)] = title
    return out


def build_post_id_to_url(posts_cache: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for pid, entry in posts_cache.items():
        if isinstance(entry, dict) and isinstance(entry.get("link"), str):
            out[pid] = entry["link"]
    return out


# --------------------------------------------------------------------------
# 測定・集計
# --------------------------------------------------------------------------

def build_pairs(
    asin_post_ids: dict[str, list[str]],
    contents: dict[str, str],
    post_id_to_url: dict[str, str],
    post_titles: dict[tuple[str, str], str],
) -> list[dict[str, Any]]:
    """(asin, post_id) ごとに、商品名候補・post_title・指標を持つペアのリスト。"""
    pairs = []
    for asin, pids in asin_post_ids.items():
        for pid in pids:
            content = contents.get(pid)
            if content is None:
                continue
            url = post_id_to_url.get(pid)
            post_title = post_titles.get((asin, url)) if url else None
            candidates = extract_product_name_candidates(content, asin)
            candidate_scores = (
                [match_scores(c, post_title) for c in candidates] if post_title else []
            )
            pairs.append({
                "asin": asin,
                "post_id": pid,
                "post_title": post_title,
                "candidates": candidates,
                "candidate_scores": candidate_scores,
            })
    return pairs


def aggregate_by_asin(pairs: list[dict[str, Any]], asins: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    """asin -> 指標ごとの最大値 (複数 post にまたがる場合も全候補の最大を取る)。"""
    out: dict[str, dict[str, Any]] = {}
    for asin in asins:
        rows = [p for p in pairs if p["asin"] == asin]
        all_scores = [s for row in rows for s in row["candidate_scores"]]
        best = best_scores_for_asin(all_scores)
        out[asin] = {
            "pair_count": len(rows),
            "no_candidate_pairs": sum(1 for row in rows if row["post_title"] and not row["candidates"]),
            "missing_post_title_pairs": sum(1 for row in rows if not row["post_title"]),
            "scores": best,
        }
    return out


def example_rows(pairs: list[dict[str, Any]], asins: tuple[str, ...], limit: int = 5) -> list[dict[str, Any]]:
    """報告用の実例: ASIN / 商品名候補 / post_title / 各指標の値 (候補があるものから)。"""
    examples = []
    for asin in asins:
        rows = [p for p in pairs if p["asin"] == asin and p["candidates"] and p["post_title"]]
        if not rows:
            continue
        row = rows[0]
        best_idx = max(
            range(len(row["candidate_scores"])),
            key=lambda i: row["candidate_scores"][i]["lcs_len"],
        )
        examples.append({
            "asin": asin,
            "post_id": row["post_id"],
            "product_name_candidate": row["candidates"][best_idx],
            "post_title": row["post_title"],
            "scores": row["candidate_scores"][best_idx],
        })
        if len(examples) >= limit:
            break
    return examples


def build_report(
    agg: dict[str, dict[str, Any]],
    pairs: list[dict[str, Any]],
    failed_post_ids: list[str],
    total_pairs: int,
) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for metric in METRICS:
        pos_values = [
            agg[a]["scores"][metric] for a in POSITIVE_ASINS
            if agg[a]["scores"] is not None
        ]
        neg_values = [
            agg[a]["scores"][metric] for a in NEGATIVE_ASINS
            if agg[a]["scores"] is not None
        ]
        metrics[metric] = {
            "positive": distribution_stats(pos_values),
            "negative": distribution_stats(neg_values),
            "full_recall_specificity": full_recall_specificity(pos_values, neg_values),
            "full_drop_specificity": full_drop_specificity(pos_values, neg_values),
            "positive_missing": len(POSITIVE_ASINS) - len(pos_values),
            "negative_missing": len(NEGATIVE_ASINS) - len(neg_values),
        }

    no_candidate_pairs = sum(1 for p in pairs if p["post_title"] and not p["candidates"])
    missing_post_title_pairs = sum(1 for p in pairs if not p["post_title"])

    return {
        "generated_at": _now_iso(),
        "extraction_rule": EXTRACTION_RULE_NOTES,
        "cta_markers": list(CTA_MARKERS),
        "positive_asins": list(POSITIVE_ASINS),
        "negative_asins": list(NEGATIVE_ASINS),
        "fetch_failed_posts": failed_post_ids,
        "total_pairs": total_pairs,
        "no_candidate_pairs": no_candidate_pairs,
        "missing_post_title_pairs": missing_post_title_pairs,
        "metrics": metrics,
        "positive_examples": example_rows(pairs, POSITIVE_ASINS),
        "negative_examples": example_rows(pairs, NEGATIVE_ASINS),
        "per_asin": agg,
    }


def build_names_dump(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    """--dump-names 用: (asin, post_id) ごとの商品名候補一覧。"""
    return {
        "pairs": [
            {
                "asin": p["asin"],
                "post_id": p["post_id"],
                "post_title": p["post_title"],
                "candidates": p["candidates"],
            }
            for p in pairs
        ],
    }


def write_json(path: pathlib.Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run(
    *,
    sources_path: pathlib.Path,
    out_path: pathlib.Path,
    cache_path: pathlib.Path,
    dump_names_path: pathlib.Path | None = None,
    wp_base_url: str = DEFAULT_WP_BASE_URL,
    sleep_seconds: float = DEFAULT_SLEEP_SECONDS,
    limit: int = 0,
    session: requests.Session | None = None,
    sleeper=None,
    fetch_fn=fetch_post_content,
) -> dict[str, Any]:
    import time as _time
    sleeper = sleeper or _time.sleep
    session = session or requests.Session()
    payload = json.loads(sources_path.read_text(encoding="utf-8"))
    sources = payload.get("sources") or []
    posts_cache = payload.get("posts_cache") or {}

    all_asins = set(POSITIVE_ASINS) | set(NEGATIVE_ASINS)
    asin_posts = build_asin_posts(sources, all_asins)
    link_to_id = build_link_to_post_id(posts_cache)
    asin_post_ids = collect_post_ids(asin_posts, link_to_id)
    post_id_to_url = build_post_id_to_url(posts_cache)
    post_titles = build_post_titles(sources)

    unique_ids = sorted({pid for ids in asin_post_ids.values() for pid in ids}, key=int)
    logger.info("対象 post: %d 件 (正例%d + 負例%d ASIN)", len(unique_ids),
                len(POSITIVE_ASINS), len(NEGATIVE_ASINS))

    cache = load_cache(cache_path)
    contents, failed_ids = fetch_contents_cached(
        unique_ids, wp_base_url, session, sleep_seconds, limit, cache,
        sleeper=sleeper, fetch_fn=fetch_fn,
    )
    save_cache(cache_path, cache)
    if failed_ids:
        logger.warning("本文取得に失敗した post: %d 件 %s", len(failed_ids), failed_ids)

    pairs = build_pairs(asin_post_ids, contents, post_id_to_url, post_titles)
    agg = aggregate_by_asin(pairs, POSITIVE_ASINS + NEGATIVE_ASINS)
    report = build_report(agg, pairs, failed_ids, len(pairs))

    write_json(out_path, report)
    logger.info("wrote %s (pairs=%d, no_candidate=%d, missing_title=%d, fetch_failed=%d)",
                out_path, len(pairs), report["no_candidate_pairs"],
                report["missing_post_title_pairs"], len(failed_ids))

    if dump_names_path is not None:
        write_json(dump_names_path, build_names_dump(pairs))
        logger.info("wrote %s (pairs=%d)", dump_names_path, len(pairs))

    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sources", default=DEFAULT_SOURCES_PATH)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--cache", default=DEFAULT_TITLE_MATCH_CACHE, help="本文取得結果の /tmp キャッシュ")
    ap.add_argument("--dump-names", default=None, help="ASIN ごとの商品名候補一覧の出力先 (省略時は出さない)")
    ap.add_argument("--wp-base-url", default=DEFAULT_WP_BASE_URL)
    ap.add_argument("--sleep-seconds", type=float, default=DEFAULT_SLEEP_SECONDS)
    ap.add_argument("--limit", type=int, default=0, help="取得する post 数の上限 (スモーク用, 0=無制限)")
    args = ap.parse_args()
    run(
        sources_path=pathlib.Path(args.sources),
        out_path=pathlib.Path(args.out),
        cache_path=pathlib.Path(args.cache),
        dump_names_path=pathlib.Path(args.dump_names) if args.dump_names else None,
        wp_base_url=args.wp_base_url,
        sleep_seconds=args.sleep_seconds,
        limit=args.limit,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
