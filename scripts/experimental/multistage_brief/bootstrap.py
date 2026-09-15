"""#4841 M1-c R1: 平均差の判定をブートストラップ信頼区間でやり直す。

母艦レビュー (PR #7354) の指摘: 「対の平均差 > 1件あたりのノイズの床」は
比べる対象を間違えている (平均のばらつきは1件のばらつきよりずっと小さい)。
平均そのもののばらつきを、対象のサンプルからブートストラップで直接見積もる。

M2 でも同じ判定基準 (数・率の両方で95%信頼区間が0を含まない) を使う想定のため、
M1c 固有のロジック (run_m1.py) から独立させてある。
"""
from __future__ import annotations

import random

DEFAULT_N_RESAMPLES = 10_000
DEFAULT_CONFIDENCE = 0.95


def bootstrap_mean_ci(
    values: list[float],
    *,
    seed: int,
    n_resamples: int = DEFAULT_N_RESAMPLES,
    confidence: float = DEFAULT_CONFIDENCE,
) -> dict[str, float | int | None]:
    """``values`` の平均について、パーセンタイル法でブートストラップ信頼区間を出す (pure)。

    件数が2未満だと標本から分布を作れないため、信頼区間は None (計算不能を
    明示する。0や過度に狭い区間に倒さない)。
    """
    n = len(values)
    if n < 2:
        return {
            "mean": round(values[0], 4) if n == 1 else None,
            "lower": None,
            "upper": None,
            "n": n,
            "n_resamples": n_resamples,
        }

    rng = random.Random(seed)
    resample_means = []
    for _ in range(n_resamples):
        total = sum(values[rng.randrange(n)] for _ in range(n))
        resample_means.append(total / n)
    resample_means.sort()

    alpha = 1 - confidence
    lower_idx = max(0, min(n_resamples - 1, round((alpha / 2) * (n_resamples - 1))))
    upper_idx = max(0, min(n_resamples - 1, round((1 - alpha / 2) * (n_resamples - 1))))

    return {
        "mean": round(sum(values) / n, 4),
        "lower": round(resample_means[lower_idx], 4),
        "upper": round(resample_means[upper_idx], 4),
        "n": n,
        "n_resamples": n_resamples,
    }


def ci_excludes_zero_on_positive_side(ci: dict[str, float | int | None]) -> bool:
    """信頼区間の下限が0より大きいか (#4841 M1-c 判定基準: 0を含まず正の側)。"""
    lower = ci.get("lower")
    return isinstance(lower, (int, float)) and lower > 0
