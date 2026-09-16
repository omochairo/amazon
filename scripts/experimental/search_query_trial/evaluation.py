"""V2 の評価 (#4841 V2 owner 上書き点3)。

評価は「取得成功URLあたりのsnippet数」を主に見る (取得失敗は失敗として数え、
成功URLを分母にする)。群の差は ASIN 単位で対にしたブートストラップ
(2,000回・seed固定) の95%信頼区間。ホスト種別内訳は V1 と同じ分類
(`analyze_third_party_yield.classify_host`) を再利用する。
"""
from __future__ import annotations

from typing import Any

from scripts.analyze_third_party_yield import _host, classify_host

from scripts.experimental.multistage_brief.bootstrap import (
    bootstrap_mean_ci,
    ci_excludes_zero_on_positive_side,
)

# #4841 owner 上書き点3: 群の差はASIN単位で対にしたブートストラップ
# (2,000回・seed固定)。V1 (analyze_third_party_yield.bootstrap_category_ratio)
# と同じ seed 4841 を使う (このレーンの慣例)。
BOOTSTRAP_N_RESAMPLES = 2000
BOOTSTRAP_SEED = 4841


def has_product_or_brand_keyword(text: str, product_name: str, brand: str) -> bool | None:
    """本文に商品名/ブランド名のトークンが含まれるか (別商品のページを数えないため)。

    `analyze_third_party_yield.sample_js_shell_check` と同じトークン単位判定
    (product_name はフレーズのまま本文に出現するとは限らないため、空白区切りの
    トークンのいずれかが含まれれば拾う)。
    """
    keywords = [t for t in (product_name.split() + [brand]) if len(t) >= 2]
    if not keywords:
        return None
    return any(k in text for k in keywords)


def compute_group_stats(
    *, asin: str, group: str, tried_urls: list[str],
    fetch_log: list[dict[str, Any]], snippets: list[dict[str, Any]],
    product_name: str, brand: str, url_texts: dict[str, str],
) -> dict[str, Any]:
    """1 ASIN・1群分の評価指標。"""
    failed = {row["url"] for row in fetch_log if row.get("status") not in ("ok",)}
    success_urls = [u for u in tried_urls if u not in failed]

    host_categories = {u: classify_host(_host(u)) for u in tried_urls}
    category_counts: dict[str, int] = {}
    for u in tried_urls:
        cat = host_categories[u]
        category_counts[cat] = category_counts.get(cat, 0) + 1

    brand_hits = 0
    checked = 0
    for u in success_urls:
        text = url_texts.get(u)
        if text is None:
            continue
        hit = has_product_or_brand_keyword(text, product_name, brand)
        if hit is None:
            continue
        checked += 1
        if hit:
            brand_hits += 1

    aspect_breakdown: dict[str, int] = {}
    for s in snippets:
        aspect = s.get("aspect")
        if isinstance(aspect, str):
            aspect_breakdown[aspect] = aspect_breakdown.get(aspect, 0) + 1

    snippet_count = len(snippets)
    success_count = len(success_urls)
    return {
        "asin": asin, "group": group,
        "urls_tried": len(tried_urls),
        "urls_fetch_failed": len(tried_urls) - success_count,
        "urls_fetch_success": success_count,
        "urls_checked_for_keyword": checked,
        "urls_with_product_or_brand_keyword": brand_hits,
        "host_category_breakdown": category_counts,
        "snippet_count": snippet_count,
        "snippet_per_success_url": (snippet_count / success_count) if success_count else 0.0,
        "aspect_breakdown": aspect_breakdown,
    }


def paired_diffs(
    per_asin_group_stats: dict[str, dict[str, dict[str, Any]]],
    *, treatment: str, control: str = "Q0",
    metric: str = "snippet_per_success_url",
) -> list[float]:
    """ASIN 単位で treatment - control の差を並べる (両群とも成功URLがあるASINのみ)。"""
    diffs: list[float] = []
    for asin, groups in per_asin_group_stats.items():
        t = groups.get(treatment)
        c = groups.get(control)
        if not t or not c:
            continue
        if t.get("urls_fetch_success", 0) == 0 and c.get("urls_fetch_success", 0) == 0:
            continue
        diffs.append(t.get(metric, 0.0) - c.get(metric, 0.0))
    return diffs


def evaluate_group(
    per_asin_group_stats: dict[str, dict[str, dict[str, Any]]],
    *, treatment: str, control: str = "Q0",
    n_resamples: int = BOOTSTRAP_N_RESAMPLES, seed: int = BOOTSTRAP_SEED,
) -> dict[str, Any]:
    diffs = paired_diffs(per_asin_group_stats, treatment=treatment, control=control)
    ci = bootstrap_mean_ci(diffs, seed=seed, n_resamples=n_resamples)
    return {
        "treatment": treatment, "control": control,
        "n_asin_pairs": len(diffs),
        "ci": ci,
        "valid": ci_excludes_zero_on_positive_side(ci),
    }


def build_report(per_asin_group_stats: dict[str, dict[str, dict[str, Any]]]) -> dict[str, Any]:
    q1 = evaluate_group(per_asin_group_stats, treatment="Q1")
    q2 = evaluate_group(per_asin_group_stats, treatment="Q2")
    return {
        "per_asin_group_stats": per_asin_group_stats,
        "q1_vs_q0": q1,
        "q2_vs_q0": q2,
        "decision": "q1_or_q2_valid" if (q1["valid"] or q2["valid"]) else "no_go",
    }
