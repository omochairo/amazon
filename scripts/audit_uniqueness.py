"""audit_uniqueness.py

Issue #3203 Phase 3「凡庸度の定量監査」の計算スクリプト。

owner の根本診断 (#2928/#2995 消費側②の評価): 「各記事が同じような内容で凡庸」
「型の完全固定」。v7 プロンプト (#3204, Phase 1) は narrative.how_to_choose の解禁と
unique angle の必須化でこれを崩そうとしているが、効果を定量観測する手段が無かった。
本スクリプトは K8/Ruri (canonical 埋め込みレーン, #2995 案1) で narrative 全文の
embedding を取り、記事同士の類似度から「凡庸さ」を 2 軸で定量化する。

読むデータ:
  data/articles/*.json (compute_semantic_related.discover_articles と同じ選定規則:
  `*.quality.json` sidecar 除外、rewrite 新旧併存時は stem 最新を採用)。

埋め込みテキスト (build_uniqueness_text):
  title + narrative の全 7 セクション (lead/why_this_product/gift_appeal/daily_use/
  safety_note/closing/how_to_choose、audit_query_entailment.build_judge_text と同じ
  キー列挙) + tags を連結する。compute_semantic_related.build_embedding_text
  (site 側の「意味的に近い記事」レーン、lead のみ・1200字) とは別の text builder
  であり、そちらは変更しない。narrative 全文を対象にすることで v7 で追加された
  how_to_choose セクションの差別化効果を測定に反映できる。

メトリクス:
  - max_sim: 自分以外の全記事との cosine 類似度の最大値。高い ⇒ 他記事の
    near-duplicate (= 冗長・凡庸)
  - centroid_sim: 全記事の正規化ベクトルの重心とのコサイン類似度。高い ⇒
    「最も平均的な記事」(= owner の言う凡庸さそのもの)
  - mean_top5_sim: 上位 5 件近傍の平均 (単一 max_sim より頑健な補助指標)

cohort 比較:
  記事 slug (YYYY-MM-DD-ASIN) の先頭 10 文字を quality_gate.HOW_TO_CHOOSE_ENFORCE_FROM
  (v7 施行日, 2026-07-16) と比較し pre_v7 / post_v7 に分類する。施行日を判定できない
  slug は安全側 (quality_gate._how_to_choose_enforced と同じ方針) で post_v7 扱い。
  各 cohort の max_sim / centroid_sim の p50・p90 を集計し、v7 施行前後で分布が
  下がるか (= 個性化が効いているか) を観測できるようにする (Phase 4 効果測定と地続き)。

埋め込みバックエンド:
  compute_semantic_related.py の embed_batch / embed_batch_ruri をそのまま import
  して再利用する (計算ロジックの重複を避ける)。既定は backend="ruri"
  (#2995 案1 の K8 LLM ワーカー経路、canonical)。

しきい値 (2026-07-26 に固定値 → 分布ベースへ変更):
  flagged 判定は既定で **コーパス分布の百分位** (--threshold-mode percentile,
  既定 p95) を実効しきい値にする。旧来の固定値 (max_sim>0.95 / centroid_sim>0.90)
  は 2026-W30 実測でコーパス中央値 (0.9416 / 0.9241) に対して較正が合っておらず、
  centroid 側は中央値より下にあって過半数が常時該当していた。詳細は
  DEFAULT_THRESHOLD_MODE のコメントを参照。

出力:
  data/analytics/uniqueness_audit.json に thresholds (実効値 + absolute_reference の
  超過件数) + cohort_stats (pre_v7 / post_v7 / all の p25・p50・p75・p90) +
  flagged_total (切り詰め前件数) + flagged (max_sim 降順で --top-flagged 件に
  切ったもの) を書き出す。全記事の生データは出力しない (ファイル肥大・コメント肥大を
  避けるため)。

呼び出し元:
  omochairo/amazon-home-ops リポジトリの週次 workflow
  (`24-uniqueness-audit.yml`) から `python -m scripts.audit_uniqueness ...` として
  実行される想定。

Issue: https://github.com/omochairo/amazon/issues/3203 (Phase 3) / #2995 (Ruri 埋め込みレーン)
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import pathlib
import re
import time
from datetime import datetime, timezone
from typing import Any

import requests

from scripts.compute_semantic_related import (
    DEFAULT_ARTICLES_DIR,
    DEFAULT_BATCH_SIZE,
    DEFAULT_MODEL as DEFAULT_MODEL_OLLAMA,
    DEFAULT_MODEL_RURI,
    DEFAULT_OLLAMA_URL,
    DEFAULT_RURI_URL,
    EmbeddingBatchError,
    compute_similarity_matrix,
    discover_articles,
    embed_batch,
    embed_batch_ruri,
    embed_texts,
    open_embedding_cache,
)
from scripts.quality_gate import HOW_TO_CHOOSE_ENFORCE_FROM

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("audit_uniqueness")

DEFAULT_OUT = "data/analytics/uniqueness_audit.json"
DEFAULT_BACKEND = "ruri"

# flagged 判定のしきい値モード。
#
# 既定を "percentile" にしている理由 (2026-07-26 の週次分析で判明した較正ずれ):
# 2026-W30 実測でコーパス (1,602 記事) の max_sim 中央値は 0.9416、centroid_sim
# 中央値は 0.9241 だった。旧固定しきい値 (max_sim>0.95 / centroid_sim>0.90) は
#   - max_sim 側: 中央値 0.9416 のすぐ上に張り付いており、コーパス全体が近似重複に
#     寄っているという事実そのものを検出できない
#   - centroid_sim 側: 中央値 0.9241 より **下** にあるため過半数が常に該当し、
#     ゲートとして機能していない
# という状態だった。固定値だと「コーパスが改善したか」も「今どれが最悪か」も
# 読み取れないため、以下のように 2 つの関心を分離する:
#   - flagged (リライト候補の作業リスト) = percentile ベースの相対しきい値。
#     コーパスが良くなればしきい値の絶対値自体が下がり、それが進捗シグナルになる。
#   - cohort_stats の百分位 + absolute_reference の超過件数 = 絶対基準の進捗追跡。
# Issue #3203 Phase 3 / #3300
DEFAULT_THRESHOLD_MODE = "percentile"
DEFAULT_MAX_SIM_PERCENTILE = 95.0
DEFAULT_CENTROID_PERCENTILE = 95.0

# 旧固定しきい値。--threshold-mode absolute の既定値であり、percentile モードでも
# 「絶対基準では何件超えているか」を absolute_reference として併記するために残す
# (相対しきい値だけだと改善しても flagged 件数が一定になり進捗が見えないため)。
DEFAULT_MAX_SIM_THRESHOLD = 0.95
DEFAULT_CENTROID_THRESHOLD = 0.90

DEFAULT_TOP_FLAGGED = 30
DEFAULT_TOP_NEIGHBORS_FOR_MEAN = 5

# cohort_stats / corpus_stats で出す百分位。p25/p75 は分布の広がり (個性化が
# 進むと裾が下に伸びる) を見るために p50/p90 に加えて記録する。
STAT_PERCENTILES = (25, 50, 75, 90)

MAX_UNIQUENESS_TEXT_LEN = 3000
_SLUG_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")

_NARRATIVE_KEYS = (
    "lead", "why_this_product", "gift_appeal", "daily_use", "safety_note", "closing", "how_to_choose",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_week_label(dt: datetime) -> str:
    year, week, _ = dt.isocalendar()
    return f"{year}-W{week:02d}"


# --------------------------------------------------------------------------
# 埋め込みテキスト組み立て (pure function)
# --------------------------------------------------------------------------

def build_uniqueness_text(article: dict[str, Any]) -> str:
    """記事 JSON dict から凡庸度監査用の embedding テキストを組み立てる。

    title + narrative 全 7 セクション (string/array いずれの narrativeSection 形式
    にも対応、#3203 how_to_choose 込み) + tags を連結する。
    compute_semantic_related.build_embedding_text (site 側「意味的に近い記事」
    レーン、lead のみ) とは異なる text builder であり、記事の言い回し・軸そのものの
    近さを測るために narrative 全文を対象にする。どのフィールドが欠けていても
    クラッシュしない。
    """
    if not isinstance(article, dict):
        return ""

    parts: list[str] = []

    title = article.get("title")
    if isinstance(title, str) and title.strip():
        parts.append(title.strip())

    narrative = article.get("narrative")
    narrative = narrative if isinstance(narrative, dict) else {}
    for key in _NARRATIVE_KEYS:
        v = narrative.get(key)
        if isinstance(v, str) and v.strip():
            parts.append(v.strip())
        elif isinstance(v, list):
            joined = " ".join(str(x).strip() for x in v if isinstance(x, str) and x.strip())
            if joined:
                parts.append(joined)

    tags = article.get("tags")
    if isinstance(tags, list):
        tag_strs = [str(t).strip() for t in tags if isinstance(t, str) and t.strip()]
        if tag_strs:
            parts.append("タグ: " + ", ".join(tag_strs))

    text = "\n\n".join(parts)
    return text[:MAX_UNIQUENESS_TEXT_LEN]


# --------------------------------------------------------------------------
# 記事レコード読み込み (discover_articles は compute_semantic_related から再利用)
# --------------------------------------------------------------------------

def load_article_records(articles_dir: pathlib.Path, limit: int = 0) -> list[dict[str, Any]]:
    """discover_articles() の結果を読み込み、{asin, slug, text} のリストを返す。

    ASIN 昇順で安定ソートしてから --limit (0=全件) で切り詰める (compute_semantic_related
    の load_article_index と同じ流儀)。slug は記事 JSON の `slug` フィールドを優先し、
    欠けていればファイル stem にフォールバックする。
    """
    paths = discover_articles(articles_dir)
    asins = sorted(paths.keys())
    if limit and limit > 0:
        asins = asins[:limit]

    records: list[dict[str, Any]] = []
    for asin in asins:
        path = paths[asin]
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("skip %s: failed to read/parse %s: %s", asin, path, e)
            continue
        if not isinstance(data, dict):
            logger.warning("skip %s: article JSON at %s is not an object", asin, path)
            continue
        slug = data.get("slug")
        slug = slug if isinstance(slug, str) and slug else path.stem
        records.append({"asin": asin, "slug": slug, "text": build_uniqueness_text(data)})
    return records


# --------------------------------------------------------------------------
# cohort 判定 (v7 施行日ゲート, quality_gate.HOW_TO_CHOOSE_ENFORCE_FROM を単一情報源とする)
# --------------------------------------------------------------------------

def slug_date(slug: str) -> str:
    """slug (YYYY-MM-DD-ASIN 等) の先頭 10 文字 (YYYY-MM-DD) を返す。判定できなければ空文字。"""
    if isinstance(slug, str) and _SLUG_DATE_RE.match(slug):
        return slug[:10]
    return ""


def cohort_for_slug(slug: str) -> str:
    """slug の日付を v7 施行日と比較し pre_v7 / post_v7 に分類する。

    施行日を判定できない slug は安全側 (quality_gate._how_to_choose_enforced と同じ
    方針: 判定不能なら施行済み扱い) で post_v7 とする。
    """
    d = slug_date(slug)
    if not d:
        return "post_v7"
    return "post_v7" if d >= HOW_TO_CHOOSE_ENFORCE_FROM else "pre_v7"


# --------------------------------------------------------------------------
# メトリクス計算 (pure function、単体テスト可能なように分離)
# --------------------------------------------------------------------------

def compute_uniqueness_metrics(
    asins: list[str], similarity: list[list[float]], top_n: int = DEFAULT_TOP_NEIGHBORS_FOR_MEAN
) -> dict[str, dict[str, Any]]:
    """記事ごとの max_sim / nearest_asin / mean_top5_sim を計算する (自己除外)。

    近傍が 1 件も無い (コーパスサイズ 1) 場合は max_sim=0.0 / nearest_asin=None /
    mean_top5_sim=0.0 を返す。
    """
    n = len(asins)
    result: dict[str, dict[str, Any]] = {}
    for i in range(n):
        candidates = [(asins[j], similarity[i][j]) for j in range(n) if j != i]
        candidates.sort(key=lambda t: (-t[1], t[0]))
        if not candidates:
            result[asins[i]] = {"max_sim": 0.0, "nearest_asin": None, "mean_top5_sim": 0.0}
            continue
        top = candidates[:top_n]
        result[asins[i]] = {
            "max_sim": round(candidates[0][1], 4),
            "nearest_asin": candidates[0][0],
            "mean_top5_sim": round(sum(s for _, s in top) / len(top), 4),
        }
    return result


def _centroid_similarities_pure(vectors: list[list[float]]) -> list[float]:
    normed: list[list[float]] = []
    for v in vectors:
        norm = math.sqrt(sum(x * x for x in v))
        normed.append([x / norm for x in v] if norm else [0.0] * len(v))

    n = len(normed)
    if n == 0:
        return []
    dim = len(normed[0])
    centroid = [0.0] * dim
    for v in normed:
        for i, x in enumerate(v):
            centroid[i] += x
    centroid = [x / n for x in centroid]
    centroid_norm = math.sqrt(sum(x * x for x in centroid))
    if centroid_norm == 0:
        return [0.0] * n
    return [sum(a * b for a, b in zip(v, centroid)) / centroid_norm for v in normed]


def _centroid_similarities_numpy(vectors: list[list[float]]) -> list[float]:
    import numpy as np

    arr = np.asarray(vectors, dtype=float)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    normed = arr / norms
    centroid = normed.mean(axis=0)
    centroid_norm = np.linalg.norm(centroid)
    if centroid_norm == 0:
        return [0.0] * len(vectors)
    return ((normed @ centroid) / centroid_norm).tolist()


def compute_centroid_similarities(vectors: list[list[float]]) -> list[float]:
    """全ベクトルを正規化したうえで、その重心とのコサイン類似度を計算する。

    「最も平均的な記事」= 凡庸さの指標。numpy が import できればベクトル化した
    計算を使い、できなければ純 Python フォールバックを使う
    (compute_similarity_matrix と同じ二経路の流儀)。
    """
    if not vectors:
        return []
    try:
        return _centroid_similarities_numpy(vectors)
    except ImportError:
        return _centroid_similarities_pure(vectors)


def _percentile(values: list[float], pct: float) -> float | None:
    """線形補間による百分位数 (numpy の既定 'linear' 法と同じ)。"""
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    if n == 1:
        return round(s[0], 4)
    k = (pct / 100) * (n - 1)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return round(s[int(k)], 4)
    d0 = s[f] * (c - k)
    d1 = s[c] * (k - f)
    return round(d0 + d1, 4)


def _stats_for_subset(subset: list[dict[str, Any]]) -> dict[str, Any]:
    """1 グループ分の count + max_sim/centroid_sim の百分位を組み立てる。

    空グループでも同じキー集合を None 埋めで返す (下流のレンダラが
    キー存在を前提にできるようにするため)。
    """
    out: dict[str, Any] = {"count": len(subset)}
    max_sims = [e["max_sim"] for e in subset]
    centroid_sims = [e["centroid_sim"] for e in subset]
    for pct in STAT_PERCENTILES:
        out[f"max_sim_p{pct}"] = _percentile(max_sims, pct) if subset else None
        out[f"centroid_sim_p{pct}"] = _percentile(centroid_sims, pct) if subset else None
    return out


def compute_cohort_stats(entries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """cohort ごと + コーパス全体の max_sim / centroid_sim 百分位を集計する。

    ``all`` はコーパス全体 (pre_v7 + post_v7)。cohort 別だけだと「サイト全体が
    どれだけ近似重複に寄っているか」という単一の追跡可能な数値が無く、
    週次で進捗を追えないため追加している (#3300)。
    """
    stats: dict[str, dict[str, Any]] = {}
    for cohort in ("pre_v7", "post_v7"):
        stats[cohort] = _stats_for_subset([e for e in entries if e["cohort"] == cohort])
    stats["all"] = _stats_for_subset(list(entries))
    return stats


def resolve_thresholds(
    entries: list[dict[str, Any]],
    *,
    mode: str = DEFAULT_THRESHOLD_MODE,
    max_sim_percentile: float = DEFAULT_MAX_SIM_PERCENTILE,
    centroid_percentile: float = DEFAULT_CENTROID_PERCENTILE,
    max_sim_absolute: float = DEFAULT_MAX_SIM_THRESHOLD,
    centroid_absolute: float = DEFAULT_CENTROID_THRESHOLD,
) -> dict[str, Any]:
    """flagged 判定に使う実効しきい値を決め、メタ情報込みの dict で返す。

    mode="percentile" では今回のコーパス分布の百分位を実効しきい値にする
    (相対評価)。mode="absolute" では固定値をそのまま使う。

    どちらのモードでも ``absolute_reference`` に固定基準での超過件数を入れる。
    percentile モードは定義上ほぼ一定件数が flagged されるため、絶対基準での
    超過件数を併記しないと改善したかどうかが判定できない。

    ``max_sim`` / ``centroid_sim`` キーは実効しきい値 (float) であり、旧スキーマと
    同じ意味を保つ (下流の comment_uniqueness_audit がそのまま読める)。
    """
    if mode not in ("absolute", "percentile"):
        raise ValueError(f"unknown threshold mode: {mode!r} (expected 'absolute' or 'percentile')")

    max_sims = [e["max_sim"] for e in entries]
    centroid_sims = [e["centroid_sim"] for e in entries]

    if mode == "percentile" and entries:
        eff_max = _percentile(max_sims, max_sim_percentile)
        eff_centroid = _percentile(centroid_sims, centroid_percentile)
    else:
        # entries が空なら百分位を取れないので固定値にフォールバックする
        eff_max = max_sim_absolute
        eff_centroid = centroid_absolute

    resolved: dict[str, Any] = {
        "mode": mode,
        "max_sim": eff_max,
        "centroid_sim": eff_centroid,
        "absolute_reference": {
            "max_sim": max_sim_absolute,
            "centroid_sim": centroid_absolute,
            "max_sim_exceeded": sum(1 for v in max_sims if v > max_sim_absolute),
            "centroid_sim_exceeded": sum(1 for v in centroid_sims if v > centroid_absolute),
        },
    }
    if mode == "percentile":
        resolved["max_sim_percentile"] = max_sim_percentile
        resolved["centroid_sim_percentile"] = centroid_percentile
    return resolved


def flag_entries(
    entries: list[dict[str, Any]],
    max_sim_threshold: float,
    centroid_threshold: float,
) -> list[dict[str, Any]]:
    """閾値超えのエントリ **全件** を max_sim 降順 (同点は asin 昇順) で返す。

    切り詰めはしない。出力の切り詰め前件数 (flagged_total) を記録するために
    select_flagged から分離してある — 旧実装は上限 30 件で切った結果しか
    残さなかったため、「何件が閾値を超えているか」が復元できなかった。
    """
    flagged: list[dict[str, Any]] = []
    for e in entries:
        reasons = []
        if e["max_sim"] > max_sim_threshold:
            reasons.append(f"max_sim>{max_sim_threshold}")
        if e["centroid_sim"] > centroid_threshold:
            reasons.append(f"centroid_sim>{centroid_threshold}")
        if reasons:
            flagged.append({**e, "reasons": reasons})
    flagged.sort(key=lambda e: (-e["max_sim"], e["asin"]))
    return flagged


def select_flagged(
    entries: list[dict[str, Any]],
    max_sim_threshold: float,
    centroid_threshold: float,
    top_flagged: int,
) -> list[dict[str, Any]]:
    """閾値超えのエントリを max_sim 降順 (同点は asin 昇順) で --top-flagged 件に切る。

    週次コメントの肥大・GitHub API 負荷を避けるための上限 (バースト回避)。
    """
    flagged = flag_entries(entries, max_sim_threshold, centroid_threshold)
    return flagged[:top_flagged] if top_flagged and top_flagged > 0 else flagged


# --------------------------------------------------------------------------
# 実行本体
# --------------------------------------------------------------------------

def run(
    articles_dir: pathlib.Path,
    out_path: pathlib.Path,
    *,
    backend: str = DEFAULT_BACKEND,
    ruri_url: str = DEFAULT_RURI_URL,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    model: str | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    limit: int = 0,
    threshold_mode: str = DEFAULT_THRESHOLD_MODE,
    max_sim_percentile: float = DEFAULT_MAX_SIM_PERCENTILE,
    centroid_percentile: float = DEFAULT_CENTROID_PERCENTILE,
    max_sim_threshold: float = DEFAULT_MAX_SIM_THRESHOLD,
    centroid_threshold: float = DEFAULT_CENTROID_THRESHOLD,
    top_flagged: int = DEFAULT_TOP_FLAGGED,
    session: requests.Session | None = None,
    sleeper=time.sleep,
    embed_cache: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """全体を実行し、件数サマリ (+ payload) を dict で返す (テスト・main 双方から呼べるように分離)。

    埋め込みバッチが最終的に失敗した場合は ``EmbeddingBatchError`` を送出し、出力
    ファイルへの書き込みは一切行わない (compute_semantic_related と同じ all-or-nothing 規律)。
    """
    if backend not in ("ollama", "ruri"):
        raise ValueError(f"unknown backend: {backend!r} (expected 'ollama' or 'ruri')")

    if not model:
        model = DEFAULT_MODEL_RURI if backend == "ruri" else DEFAULT_MODEL_OLLAMA

    records = load_article_records(articles_dir, limit)
    logger.info("articles after dedupe: %d (limit=%d)", len(records), limit)

    if not records:
        logger.info("no articles to process; nothing to write")
        return {"articles": 0, "written": False}

    asins = [r["asin"] for r in records]
    texts = [r["text"] for r in records]

    session = session or requests.Session()
    cache = open_embedding_cache(
        embed_cache, backend=backend, model=model, ruri_url=ruri_url, session=session,
    )
    embeddings = embed_texts(
        texts, backend=backend, batch_size=batch_size, ollama_url=ollama_url,
        model=model, ruri_url=ruri_url, session=session, sleeper=sleeper, cache=cache,
    )
    logger.info("embedding complete: %d article(s)", len(embeddings))
    if cache is not None:
        # limit 付きは部分実行。prune すると warm キャッシュを削ってしまう
        cache.save(prune=not limit)

    similarity = compute_similarity_matrix(embeddings)
    metrics = compute_uniqueness_metrics(asins, similarity)
    centroid_sims = compute_centroid_similarities(embeddings)

    entries: list[dict[str, Any]] = []
    for idx, r in enumerate(records):
        asin = r["asin"]
        m = metrics[asin]
        entries.append({
            "asin": asin,
            "slug_date": slug_date(r["slug"]),
            "cohort": cohort_for_slug(r["slug"]),
            "page": f"https://navi.omcha.jp/products/{asin.lower()}/",
            "max_sim": m["max_sim"],
            "nearest_asin": m["nearest_asin"],
            "mean_top5_sim": m["mean_top5_sim"],
            "centroid_sim": round(centroid_sims[idx], 4),
        })

    cohort_stats = compute_cohort_stats(entries)
    thresholds = resolve_thresholds(
        entries,
        mode=threshold_mode,
        max_sim_percentile=max_sim_percentile,
        centroid_percentile=centroid_percentile,
        max_sim_absolute=max_sim_threshold,
        centroid_absolute=centroid_threshold,
    )
    all_flagged = flag_entries(entries, thresholds["max_sim"], thresholds["centroid_sim"])
    flagged = all_flagged[:top_flagged] if top_flagged and top_flagged > 0 else all_flagged

    payload: dict[str, Any] = {
        "generated_at": _now_iso(),
        "model": model,
        "source_week": _iso_week_label(datetime.now(timezone.utc)),
        "corpus_size": len(entries),
        "thresholds": thresholds,
        # 切り詰め前の該当件数。flagged は表示用に top_flagged で切られるため、
        # 「実際に何件が閾値を超えたか」はこちらを見る。
        "flagged_total": len(all_flagged),
        "flagged_truncated": len(all_flagged) > len(flagged),
        "cohort_stats": cohort_stats,
        "flagged": flagged,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(
        "wrote %s: corpus=%d mode=%s thresholds(max_sim=%s, centroid_sim=%s) "
        "flagged_total=%d (shown=%d) abs_exceeded(max_sim=%d, centroid_sim=%d)",
        out_path, len(entries), thresholds["mode"],
        thresholds["max_sim"], thresholds["centroid_sim"],
        len(all_flagged), len(flagged),
        thresholds["absolute_reference"]["max_sim_exceeded"],
        thresholds["absolute_reference"]["centroid_sim_exceeded"],
    )
    return {
        "articles": len(entries),
        "written": True,
        "flagged": len(flagged),
        "flagged_total": len(all_flagged),
        "payload": payload,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="記事間の narrative 全文の意味的近さから凡庸度を定量監査する (#3203 Phase 3)"
    )
    ap.add_argument("--articles-dir", default=DEFAULT_ARTICLES_DIR, help="記事 JSON のディレクトリ")
    ap.add_argument("--out", default=DEFAULT_OUT, help="出力先 JSON パス")
    ap.add_argument("--limit", type=int, default=0, help="処理する記事数の上限 (0=全件、スモークテスト用)")
    ap.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="embed API へのバッチサイズ")
    ap.add_argument(
        "--threshold-mode",
        choices=("percentile", "absolute"),
        default=DEFAULT_THRESHOLD_MODE,
        help="flagged 判定のしきい値モード: percentile (既定, コーパス分布の百分位) / absolute (固定値)",
    )
    ap.add_argument("--max-sim-percentile", type=float, default=DEFAULT_MAX_SIM_PERCENTILE,
                    help="percentile モードでの max_sim しきい値の百分位")
    ap.add_argument("--centroid-percentile", type=float, default=DEFAULT_CENTROID_PERCENTILE,
                    help="percentile モードでの centroid_sim しきい値の百分位")
    ap.add_argument("--max-sim-threshold", type=float, default=DEFAULT_MAX_SIM_THRESHOLD,
                    help="absolute モードの max_sim 閾値 / percentile モードでは absolute_reference の基準値")
    ap.add_argument("--centroid-threshold", type=float, default=DEFAULT_CENTROID_THRESHOLD,
                    help="absolute モードの centroid_sim 閾値 / percentile モードでは absolute_reference の基準値")
    ap.add_argument("--top-flagged", type=int, default=DEFAULT_TOP_FLAGGED, help="flagged に残す最大件数 (max_sim 降順)")
    ap.add_argument(
        "--backend",
        choices=("ollama", "ruri"),
        default=os.environ.get("EMBED_BACKEND", DEFAULT_BACKEND),
        help="埋め込みバックエンド: ruri (既定, #2995 案1 K8 LLM ワーカー) または ollama",
    )
    ap.add_argument("--ollama-url", default=os.environ.get("OLLAMA_URL", DEFAULT_OLLAMA_URL))
    ap.add_argument("--ruri-url", default=os.environ.get("RURI_URL", DEFAULT_RURI_URL))
    ap.add_argument(
        "--model",
        default=os.environ.get("EMBED_MODEL"),
        help="embeddings に使うモデル名 (未指定ならバックエンド別既定値)",
    )
    ap.add_argument(
        "--embed-cache",
        default=os.environ.get("EMBED_CACHE"),
        help=("埋め込みキャッシュの JSON パス (未指定ならキャッシュしない)。"
              "**リポジトリ内を指さないこと** — 数十 MB になる"),
    )
    args = ap.parse_args()

    try:
        run(
            articles_dir=pathlib.Path(args.articles_dir),
            out_path=pathlib.Path(args.out),
            backend=args.backend,
            ruri_url=args.ruri_url,
            ollama_url=args.ollama_url,
            model=args.model,
            embed_cache=args.embed_cache,
            batch_size=args.batch_size,
            limit=args.limit,
            threshold_mode=args.threshold_mode,
            max_sim_percentile=args.max_sim_percentile,
            centroid_percentile=args.centroid_percentile,
            max_sim_threshold=args.max_sim_threshold,
            centroid_threshold=args.centroid_threshold,
            top_flagged=args.top_flagged,
        )
    except EmbeddingBatchError as e:
        logger.error("embedding computation failed: %s; aborting without writing output", e)
        return 1
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
