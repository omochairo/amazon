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

# owner 修正5: Q1・Q2 それぞれ独立に95%信頼区間でORを取ると、家族的な偽陽性率は
# 1-0.95**2 ≈ 10%まで増える。全体の go/no-go 判定 (両方を見る判定) だけは
# Bonferroni補正 (各検定を 97.5%) にして家族的 alpha を 5% に抑える。
# 個々の群の採否 (adopted_groups) は各群自身の信頼区間 (95%) で決める。
FAMILYWISE_CONFIDENCE = 0.975
_STANDARD_CONFIDENCE = 0.95


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
    extraction_meta: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """1 ASIN・1群分の評価指標。

    owner 修正4: `empty_body` (取得はできたが本文が無かった) は取得失敗
    (`fetch_failed`) と区別する。取得自体はできているので「成功URLあたり」の
    分母から落とさない (empty_body の URL は snippet 0 件として分母に残る)。
    """
    fetch_failed = {row["url"] for row in fetch_log if row.get("status") == "fetch_failed"}
    empty_body = {row["url"] for row in fetch_log if row.get("status") == "empty_body"}
    success_urls = [u for u in tried_urls if u not in fetch_failed]

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

    # owner 修正3: 抽出の切り詰め・失敗は「snippet 0件」と区別できないと、
    # 長文が多い群 (ブログ) だけが理由不明で低く出る。件数を出したうえで、
    # 分母から切り詰め・失敗のURLを除いた値も併記する (主指標は変えない)。
    extraction_ok = extraction_truncated = extraction_failed = extraction_bad_json = 0
    extraction_issue_urls: set[str] = set()
    for m in extraction_meta or []:
        status = m.get("status")
        if status == "ok":
            extraction_ok += 1
        elif status == "truncated":
            extraction_truncated += 1
            extraction_issue_urls.add(m.get("source_url"))
        elif status == "failed":
            extraction_failed += 1
            extraction_issue_urls.add(m.get("source_url"))
        elif status == "bad_json":
            extraction_bad_json += 1
            extraction_issue_urls.add(m.get("source_url"))

    success_urls_excl_extraction_issues = [
        u for u in success_urls if u not in extraction_issue_urls
    ]
    success_count_excl_issues = len(success_urls_excl_extraction_issues)

    snippet_count = len(snippets)
    success_count = len(success_urls)
    return {
        "asin": asin, "group": group,
        "urls_tried": len(tried_urls),
        "urls_fetch_failed": len(fetch_failed),
        "urls_empty_body": len(empty_body),
        "urls_fetch_success": success_count,
        "urls_checked_for_keyword": checked,
        "urls_with_product_or_brand_keyword": brand_hits,
        "host_category_breakdown": category_counts,
        "snippet_count": snippet_count,
        "snippet_per_success_url": (snippet_count / success_count) if success_count else 0.0,
        "snippet_per_success_url_excl_extraction_issues": (
            snippet_count / success_count_excl_issues if success_count_excl_issues else 0.0
        ),
        "aspect_breakdown": aspect_breakdown,
        "extraction_ok": extraction_ok,
        "extraction_truncated": extraction_truncated,
        "extraction_failed": extraction_failed,
        "extraction_bad_json": extraction_bad_json,
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
    confidence: float = _STANDARD_CONFIDENCE,
) -> dict[str, Any]:
    diffs = paired_diffs(per_asin_group_stats, treatment=treatment, control=control)
    ci = bootstrap_mean_ci(diffs, seed=seed, n_resamples=n_resamples, confidence=confidence)
    return {
        "treatment": treatment, "control": control,
        "n_asin_pairs": len(diffs),
        "confidence": confidence,
        "ci": ci,
        "valid": ci_excludes_zero_on_positive_side(ci),
    }


def build_report(per_asin_group_stats: dict[str, dict[str, dict[str, Any]]]) -> dict[str, Any]:
    """owner 修正5: Q1・Q2 それぞれの信頼区間を報告し、採用する群はその群自身の
    信頼区間 (95%) で決める (adopted_groups)。一方、「どちらかが有効」という
    両方を見た全体判定 (decision) は多重比較になるため、Bonferroni補正
    (各検定 97.5%) で家族的 alpha を 5% に抑えたうえで判定する。"""
    q1 = evaluate_group(per_asin_group_stats, treatment="Q1")
    q2 = evaluate_group(per_asin_group_stats, treatment="Q2")
    q1_familywise = evaluate_group(
        per_asin_group_stats, treatment="Q1", confidence=FAMILYWISE_CONFIDENCE,
    )
    q2_familywise = evaluate_group(
        per_asin_group_stats, treatment="Q2", confidence=FAMILYWISE_CONFIDENCE,
    )
    adopted_groups = [g for g, ev in (("Q1", q1), ("Q2", q2)) if ev["valid"]]
    decision = "go" if (q1_familywise["valid"] or q2_familywise["valid"]) else "no_go"
    return {
        "per_asin_group_stats": per_asin_group_stats,
        "q1_vs_q0": q1,
        "q2_vs_q0": q2,
        "adopted_groups": adopted_groups,
        "decision": decision,
        "decision_confidence": FAMILYWISE_CONFIDENCE,
    }
