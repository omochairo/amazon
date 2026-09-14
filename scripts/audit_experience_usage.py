"""audit_experience_usage.py

Issue #4841 T1「体験談の実使用率を測る」の計測スクリプト。

問い:
  `data/raw/per_asin/<ASIN>/experience.json` の snippet (#6654 由来の体験談レーン)
  は、Jules が書いた記事の narrative / editorial_comment に実際に反映されているか。
  aspect 別、特に「不満」はどうか。

母集団:
  対象は「experience.json の generated_at より後に生成された記事」に限る
  (それより前の記事は素材を使いようがなかった)。同一 ASIN に複数記事がある
  場合は compute_semantic_related.discover_articles と同じ規則 (stem 最新採用)
  で選んだ記事について、この日付条件を判定する。除外した ASIN は全件、
  理由付きで出力に残す (zero_snippets / no_article / invalid_generated_at /
  invalid_article_date / article_older_than_material / no_paragraphs)。

判定方法:
  snippet は usable_as:"paraphrase" が大半で文字列一致では測れないため、
  K8/Ruri (canonical embedding レーン、audit_uniqueness.py / build_wp_wp_h2_link_
  candidates.py と同じ経路) で snippet (kind="query") と記事の各段落
  (narrative の全キー + editorial_comment, kind="document") のコサイン類似度を
  取り、段落側の最大値を snippet の「使用スコア」とする。

  閾値は勘で決めない。同じ記事に **別 ASIN・同じ aspect のランダムな snippet**
  (固定 seed) を当てた類似度分布 (負の対照) を作り、その p95 を「使われた」の
  閾値にする。正例分布との重なり (histogram overlap) も出す — 重なりが大きい
  場合、この方法では測れないというのも有効な結果として報告する。

  この別ASIN負の対照は「同じ商品の話をしている」ことと「実際に使った」こと
  を分離できない (母艦レビュー R1)。そのため `article_older_than_material`
  で除外される記事 (素材より前に書かれていて、時系列的に使いようがない) を
  **同一 ASIN の対照**として追加で採点し、included の閾値超過率から引いた差
  (`diff`) を見出しの数字として使う。この差は「Jules が素材を使った効果」の
  上限にすぎない — 対照記事 (5月生成、v7以前) と included (8〜9月生成) は
  生成時期もプロンプトも異なるため (R3、`[推]` で扱う交絡)。

出力:
  data/analytics/experience_usage.json に、母集団の内訳・閾値の根拠・分布・
  記事単位/snippet 単位の使用率・同一ASIN対照の分布と超過率・included との
  差 (`diff`)・閾値直上直下のサンプルを書き出す。

U1 (#4841 「不満の使用率を、注意点の枠まで含めて測り直す」):
  `--include-caveat-fields` を付けると、採点対象の段落に `product.cons`
  (リストの各要素を `cons[i]` として1段落) と `verdict.headline`
  (`verdict_headline`) を追加する。既定 (フラグ無し) は T1 と同じ挙動。
  閾値は同じ方法 (別ASIN負の対照のp95) で採点対象範囲込みで較正し直され、
  同一ASIN対照も同じ範囲で採点される。出力は既定と別のファイル
  (`data/analytics/experience_usage_caveat.json`) にする。
  `matched_key_breakdown` (aspect別・cons/verdict/safety_note/その他の内訳)
  も出力に追加される (フラグの有無に関わらず常に出す)。

Issue: https://github.com/omochairo/amazon/issues/4841 (T1 / U1)
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import math
import os
import pathlib
import random
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any

import requests

from scripts.compute_semantic_related import DEFAULT_RURI_URL, discover_articles, resolve_embed_model

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("audit_experience_usage")

DEFAULT_EXPERIENCE_GLOB = "data/raw/per_asin/*/experience.json"
DEFAULT_ARTICLES_DIR = "data/articles"
DEFAULT_OUT = "data/analytics/experience_usage.json"
# #4841 U1: 既存の experience_usage.json を上書きしないための別出力先。
DEFAULT_OUT_CAVEAT = "data/analytics/experience_usage_caveat.json"
DEFAULT_MODEL_RURI = "cl-nagoya/ruri-v3-310m"
DEFAULT_BATCH_SIZE = 32
REQUEST_TIMEOUT = 120
_MAX_EXTRA_RETRIES = 2
_RETRY_SLEEP_SECONDS = 2.0

# 負の対照サンプリングの固定 seed。較正の再現性のため変更しない
# (変えると閾値そのものが動き、過去の計測結果と比較できなくなる)。
NEGATIVE_CONTROL_SEED = 20260914

# audit_uniqueness.py の _NARRATIVE_KEYS と同じ列挙 (how_to_choose 込み)。
NARRATIVE_KEYS = (
    "lead", "why_this_product", "gift_appeal", "daily_use", "safety_note", "closing", "how_to_choose",
)

# #4841 U1: 注意点の専用枠 (product.cons の各要素・verdict.headline)。
# --include-caveat-fields でのみ採点対象に加える (既定挙動は変えない)。
CAVEAT_VERDICT_KEY = "verdict_headline"

THRESHOLD_PERCENTILE = 95.0
DISTRIBUTION_PERCENTILES = (50, 90, 95)
SAMPLE_COUNT = 5
SAMPLE_TEXT_MAX_LEN = 200


class EmbeddingBatchError(Exception):
    """Ruri embed バッチがリトライ上限まで失敗した。"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _truncate(text: str, n: int) -> str:
    text = text or ""
    return text if len(text) <= n else text[: n - 1] + "…"


# --------------------------------------------------------------------------
# 段落テキストの組み立て (pure function)
# --------------------------------------------------------------------------

def _flatten_section(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        items = [str(x).strip() for x in value if isinstance(x, str) and x.strip()]
        return " ".join(items)
    return ""


def build_paragraph_map(article: dict[str, Any], include_caveat_fields: bool = False) -> dict[str, str]:
    """記事 JSON dict から {narrative キー / editorial_comment: 段落テキスト} を作る。

    narrativeSection は string/array いずれの形式にも対応する
    (audit_uniqueness.build_uniqueness_text と同じ考え方)。空の段落は含めない。

    include_caveat_fields=True のとき (#4841 U1)、注意点の専用枠として
    `product.cons` (リストの各要素を `cons[i]` という 1 段落) と
    `verdict.headline` (`verdict_headline` 段落) を追加する。既定 (False) では
    T1 と同じ挙動 (narrative キー + editorial_comment のみ)。
    """
    if not isinstance(article, dict):
        return {}
    out: dict[str, str] = {}
    narrative = article.get("narrative")
    narrative = narrative if isinstance(narrative, dict) else {}
    for key in NARRATIVE_KEYS:
        text = _flatten_section(narrative.get(key))
        if text:
            out[key] = text
    ec = article.get("editorial_comment")
    if isinstance(ec, str) and ec.strip():
        out["editorial_comment"] = ec.strip()
    if include_caveat_fields:
        product = article.get("product")
        cons = product.get("cons") if isinstance(product, dict) else None
        if isinstance(cons, list):
            for i, item in enumerate(cons):
                if isinstance(item, str) and item.strip():
                    out[f"cons[{i}]"] = item.strip()
        verdict = article.get("verdict")
        headline = verdict.get("headline") if isinstance(verdict, dict) else None
        if isinstance(headline, str) and headline.strip():
            out[CAVEAT_VERDICT_KEY] = headline.strip()
    return out


# --------------------------------------------------------------------------
# experience.json 読み込み・母集団選定
# --------------------------------------------------------------------------

def load_experience_records(pattern: str = DEFAULT_EXPERIENCE_GLOB) -> list[dict[str, Any]]:
    """data/raw/per_asin/*/experience.json を asin 昇順で読み込む。

    壊れたファイルは警告して読み飛ばす (他の監査スクリプトと同じ graceful な扱い)。
    """
    records: list[dict[str, Any]] = []
    for path in sorted(glob.glob(pattern)):
        try:
            data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("skip %s: failed to read/parse: %s", path, e)
            continue
        if not isinstance(data, dict):
            logger.warning("skip %s: not a JSON object", path)
            continue
        asin = data.get("asin")
        if not isinstance(asin, str) or not asin:
            asin = pathlib.Path(path).parent.name
        snippets = data.get("snippets")
        records.append({
            "asin": asin,
            "path": path,
            "generated_at": data.get("generated_at"),
            "snippets": snippets if isinstance(snippets, list) else [],
        })
    return records


def _parse_iso(ts: Any) -> datetime | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def select_population(
    experience_records: list[dict[str, Any]],
    articles_dir: str | os.PathLike[str],
    include_caveat_fields: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """母集団 (included) と除外 (excluded, 理由付き) を作る。

    included の各要素: {asin, generated_at, article_path, article_date,
    snippets, paragraphs}
    excluded の各要素: {asin, reason}

    理由は zero_snippets / no_article / invalid_generated_at /
    invalid_article_date / article_older_than_material / no_paragraphs のいずれか。
    """
    article_paths = discover_articles(pathlib.Path(articles_dir))
    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []

    for rec in experience_records:
        asin = rec["asin"]
        if not rec["snippets"]:
            excluded.append({"asin": asin, "reason": "zero_snippets"})
            continue

        gen_dt = _parse_iso(rec["generated_at"])
        if gen_dt is None:
            excluded.append({"asin": asin, "reason": "invalid_generated_at"})
            continue

        path = article_paths.get(asin)
        if path is None:
            excluded.append({"asin": asin, "reason": "no_article"})
            continue

        try:
            article = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("skip %s: failed to read/parse article %s: %s", asin, path, e)
            excluded.append({"asin": asin, "reason": "no_article"})
            continue
        if not isinstance(article, dict):
            excluded.append({"asin": asin, "reason": "no_article"})
            continue

        article_dt = _parse_iso(article.get("date"))
        if article_dt is None:
            excluded.append({"asin": asin, "reason": "invalid_article_date"})
            continue
        if article_dt <= gen_dt:
            excluded.append({"asin": asin, "reason": "article_older_than_material"})
            continue

        paragraphs = build_paragraph_map(article, include_caveat_fields=include_caveat_fields)
        if not paragraphs:
            excluded.append({"asin": asin, "reason": "no_paragraphs"})
            continue

        included.append({
            "asin": asin,
            "generated_at": rec["generated_at"],
            "article_path": str(path),
            "article_date": article.get("date"),
            "snippets": rec["snippets"],
            "paragraphs": paragraphs,
        })

    return included, excluded


def select_same_asin_control(
    experience_records: list[dict[str, Any]],
    articles_dir: str | os.PathLike[str],
    include_caveat_fields: bool = False,
) -> list[dict[str, Any]]:
    """R1: 別 ASIN の負の対照では「同じ商品の話」と「使った」を分離できない問題への対照群。

    `select_population` が `article_older_than_material` で除外する記事
    (素材の generated_at より前に書かれた記事) を対照として使う。この記事は
    時系列的に素材を使いようがないので、この記事の段落と同じ ASIN の
    snippet の類似度が高くても「同じ商品について書けば自然に似る」度合いの
    目安にしかならない。included と同じ形の dict を返す
    (`boundary_hours` は R4 の日付境界チェック用: generated_at と
    article_date の差 (時間、正値))。
    """
    article_paths = discover_articles(pathlib.Path(articles_dir))
    control: list[dict[str, Any]] = []
    for rec in experience_records:
        asin = rec["asin"]
        if not rec["snippets"]:
            continue
        gen_dt = _parse_iso(rec["generated_at"])
        if gen_dt is None:
            continue
        path = article_paths.get(asin)
        if path is None:
            continue
        try:
            article = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("skip %s: failed to read/parse article %s: %s", asin, path, e)
            continue
        if not isinstance(article, dict):
            continue
        article_dt = _parse_iso(article.get("date"))
        if article_dt is None:
            continue
        if article_dt > gen_dt:
            continue  # included 側 (対照ではない)
        paragraphs = build_paragraph_map(article, include_caveat_fields=include_caveat_fields)
        if not paragraphs:
            continue
        control.append({
            "asin": asin,
            "generated_at": rec["generated_at"],
            "article_path": str(path),
            "article_date": article.get("date"),
            "snippets": rec["snippets"],
            "paragraphs": paragraphs,
            "boundary_hours": round((gen_dt - article_dt).total_seconds() / 3600.0, 2),
        })
    return control


def date_boundary_within_24h(
    included: list[dict[str, Any]], control: list[dict[str, Any]],
) -> dict[str, Any]:
    """R4: 記事の `date` は生成時刻ではなく10:00 JST固定の公開日時のため、

    素材の generated_at との日付境界の新旧判定が実際の生成順序と食い違い
    うる。`article_date` と `generated_at` の差が24時間未満の件数を出す
    (新旧の境界に近く、included/control の振り分けが逆転しうる候補)。
    """
    items: list[dict[str, Any]] = []
    for item in included:
        gen_dt = _parse_iso(item["generated_at"])
        art_dt = _parse_iso(item["article_date"])
        if gen_dt is None or art_dt is None:
            continue
        hours = abs((art_dt - gen_dt).total_seconds()) / 3600.0
        if hours < 24:
            items.append({"asin": item["asin"], "group": "included", "hours": round(hours, 2)})
    for item in control:
        if item["boundary_hours"] < 24:
            items.append({"asin": item["asin"], "group": "same_asin_control", "hours": item["boundary_hours"]})
    return {"count": len(items), "items": items}


# --------------------------------------------------------------------------
# 負の対照サンプリング (pure, 固定 seed で再現可能)
# --------------------------------------------------------------------------

def build_snippet_pool(experience_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """全 experience.json (母集団に入らなかった ASIN も含む) から snippet プールを作る。

    負の対照は「別 ASIN の snippet」を要求するだけで、その ASIN 自身が母集団に
    入っているかは問わない (候補が多いほど aspect 別のサンプリングが安定する)。
    """
    pool: list[dict[str, Any]] = []
    for rec in experience_records:
        for idx, sn in enumerate(rec["snippets"]):
            if not isinstance(sn, dict):
                continue
            text = sn.get("text")
            aspect = sn.get("aspect")
            if not isinstance(text, str) or not text.strip() or not isinstance(aspect, str) or not aspect:
                continue
            pool.append({
                "asin": rec["asin"],
                "index": idx,
                "aspect": aspect,
                "text": text.strip(),
                "source_type": sn.get("source_type"),
            })
    return pool


def sample_negative_snippet(
    pool: list[dict[str, Any]], aspect: str, exclude_asin: str, rng: random.Random,
) -> dict[str, Any] | None:
    """同じ aspect・別 ASIN の snippet を固定 seed の rng でランダムに1件選ぶ。

    候補が無ければ None (この snippet は負の対照から除外される)。候補は
    rng.choice の前に (asin, index) で安定ソートしておく — dict/glob の順序に
    依存すると同じ seed でも別の結果になり得るため。
    """
    candidates = [p for p in pool if p["aspect"] == aspect and p["asin"] != exclude_asin]
    if not candidates:
        return None
    candidates.sort(key=lambda p: (p["asin"], p["index"]))
    return rng.choice(candidates)


# --------------------------------------------------------------------------
# Ruri embedding (kind 可変。build_wp_wp_h2_link_candidates.py と同じ独立実装)
# --------------------------------------------------------------------------

def embed_batch_ruri(
    texts: list[str], kind: str, ruri_url: str, session: requests.Session, sleeper=time.sleep,
) -> list[list[float]]:
    """1 バッチ分のテキストを Ruri v3 API (``/embed``) でベクトル化する。"""
    url = f"{ruri_url.rstrip('/')}/embed"
    last_err: Exception | None = None
    attempts = _MAX_EXTRA_RETRIES + 1
    for attempt in range(1, attempts + 1):
        try:
            resp = session.post(url, json={"texts": texts, "kind": kind}, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            payload = resp.json()
            vectors = payload.get("vectors") if isinstance(payload, dict) else None
            if not isinstance(vectors, list) or len(vectors) != len(texts):
                raise EmbeddingBatchError(
                    f"unexpected /embed response shape (expected {len(texts)} vectors)"
                )
            return vectors
        except (requests.RequestException, EmbeddingBatchError, ValueError) as e:
            last_err = e
            if attempt < attempts:
                logger.warning("ruri embed batch failed (attempt %d/%d): %s; retrying", attempt, attempts, e)
                sleeper(_RETRY_SLEEP_SECONDS)
            else:
                logger.error("ruri embed batch failed after %d attempt(s): %s", attempts, e)
    raise EmbeddingBatchError(str(last_err)) from last_err


def embed_texts_ruri(
    texts: list[str],
    kind: str,
    ruri_url: str,
    session: requests.Session,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    sleeper=time.sleep,
) -> list[list[float]]:
    if not texts:
        return []
    vectors: list[list[float]] = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        vectors.extend(embed_batch_ruri(batch, kind, ruri_url, session, sleeper=sleeper))
    return vectors


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# --------------------------------------------------------------------------
# 集計 (pure function)
# --------------------------------------------------------------------------

def percentile(values: list[float], pct: float) -> float | None:
    """線形補間による百分位数 (audit_uniqueness._percentile と同じ方式)。"""
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
    return round(s[f] * (c - k) + s[c] * (k - f), 4)


def distribution_stats(values: list[float]) -> dict[str, Any]:
    out: dict[str, Any] = {"count": len(values)}
    for pct in DISTRIBUTION_PERCENTILES:
        out[f"p{pct}"] = percentile(values, pct)
    return out


def histogram_overlap(a: list[float], b: list[float], bins: int = 20) -> float | None:
    """正例分布 a と負例分布 b の重なり具合 (0=重ならない, 1=同じ形)。

    固定ビンのヒストグラム (各群を面積1に正規化) の overlapping coefficient。
    重なりが大きい (1 に近い) ほど「この方法では正例と負例を区別できない」。
    """
    if not a or not b:
        return None
    lo = min(min(a), min(b))
    hi = max(max(a), max(b))
    if hi <= lo:
        return 1.0

    width = (hi - lo) / bins

    def hist(values: list[float]) -> list[float]:
        counts = [0] * bins
        for v in values:
            idx = int((v - lo) / width)
            idx = max(0, min(bins - 1, idx))
            counts[idx] += 1
        total = len(values)
        return [c / total for c in counts]

    ha, hb = hist(a), hist(b)
    return round(sum(min(x, y) for x, y in zip(ha, hb)), 4)


def _rate_stats(items: list[dict[str, Any]], key: str = "used") -> dict[str, Any]:
    total = len(items)
    used = sum(1 for r in items if r.get(key))
    return {"total": total, "used": used, "rate": round(used / total, 4) if total else None}


def _diff_rate(a: dict[str, Any] | None, b: dict[str, Any] | None) -> float | None:
    """a (included) の rate から b (対照) の rate を引く (R1: 見出しは差で出す)。

    どちらかの group が存在しない/rate が計算不能なら None
    (母数0や、対照側に対応する aspect/source_type が無い場合)。
    """
    if not a or not b or a.get("rate") is None or b.get("rate") is None:
        return None
    return round(a["rate"] - b["rate"], 4)


def _group_by(items: list[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        k = item.get(key) or "(unknown)"
        groups.setdefault(k, []).append(item)
    return groups


def _bucket_matched_key(matched_key: str | None) -> str:
    """#4841 U1: matched_key を報告用の粗い枠 (cons/verdict/safety_note/その他) に丸める。"""
    if not matched_key:
        return "(none)"
    if matched_key.startswith("cons["):
        return "cons"
    if matched_key == CAVEAT_VERDICT_KEY:
        return "verdict"
    if matched_key == "safety_note":
        return "safety_note"
    return "other"


def matched_key_breakdown_by_aspect(positive_results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """aspect別に、matched_key の粗い枠の内訳を出す (#4841 U1)。

    all: 全 snippet (used に関わらず) の内訳。used_only: 閾値を超えた
    (used=True) snippet のみの内訳。不満が cons/verdict/safety_note/その他の
    どこに実際に着地しているかを見るための集計。
    """
    out: dict[str, dict[str, Any]] = {}
    for aspect, items in sorted(_group_by(positive_results, "aspect").items()):
        all_counts = Counter(_bucket_matched_key(r.get("matched_key")) for r in items)
        used_counts = Counter(_bucket_matched_key(r.get("matched_key")) for r in items if r.get("used"))
        out[aspect] = {"all": dict(all_counts), "used_only": dict(used_counts)}
    return out


# --------------------------------------------------------------------------
# 実行
# --------------------------------------------------------------------------

def run(
    *,
    experience_glob: str = DEFAULT_EXPERIENCE_GLOB,
    articles_dir: str = DEFAULT_ARTICLES_DIR,
    out_path: pathlib.Path,
    ruri_url: str = DEFAULT_RURI_URL,
    model: str = DEFAULT_MODEL_RURI,
    batch_size: int = DEFAULT_BATCH_SIZE,
    seed: int = NEGATIVE_CONTROL_SEED,
    threshold_percentile: float = THRESHOLD_PERCENTILE,
    include_caveat_fields: bool = False,
    session: requests.Session | None = None,
    sleeper=time.sleep,
) -> dict[str, Any]:
    started = time.monotonic()
    session = session or requests.Session()
    resolved_model = resolve_embed_model("ruri", model=model, ruri_url=ruri_url, session=session) or model

    experience_records = load_experience_records(experience_glob)
    included, excluded = select_population(experience_records, articles_dir, include_caveat_fields)
    # R1: article_older_than_material (素材より前に書かれた記事) を同一 ASIN
    # の対照として使う。別 ASIN の負の対照 (下の pool/rng) では「同じ商品の
    # 話」と「使った」を分離できないため。
    control = select_same_asin_control(experience_records, articles_dir, include_caveat_fields)

    population = {
        "total_experience_files": len(experience_records),
        "included": len(included),
        "included_asins": sorted(item["asin"] for item in included),
        "excluded_total": len(excluded),
        "excluded_by_reason": dict(Counter(e["reason"] for e in excluded)),
        "excluded": excluded,
        "same_asin_control_total": len(control),
        "same_asin_control_asins": sorted(item["asin"] for item in control),
        # R4: date が10:00 JST固定の公開日時なので、generated_at との日付
        # だけの比較では新旧判定を誤りうる境界ケースの件数。
        "date_boundary_within_24h": date_boundary_within_24h(included, control),
    }

    if not included:
        payload = {
            "generated_at": _now_iso(),
            "embed_model": resolved_model,
            "elapsed_seconds": round(time.monotonic() - started, 1),
            "include_caveat_fields": include_caveat_fields,
            "population": population,
            "threshold": None,
            "article_level": None,
            "snippet_level": None,
            "matched_key_breakdown": None,
            "same_asin_control": None,
            "diff": None,
            "alt_threshold_from_control": None,
            "samples": None,
        }
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("no included articles; wrote empty payload to %s", out_path)
        return {"included": 0, "written": True, "payload": payload}

    pool = build_snippet_pool(experience_records)

    # 文書側 (段落) テキスト
    doc_texts: list[str] = []
    doc_index: list[tuple[str, str]] = []
    for item in included:
        for key, text in item["paragraphs"].items():
            doc_texts.append(text)
            doc_index.append((item["asin"], key))

    # クエリ側 (正例 snippet + 負の対照 snippet)
    rng = random.Random(seed)
    query_texts: list[str] = []
    query_meta: list[dict[str, Any]] = []
    skipped_negative = 0

    for item in included:
        asin = item["asin"]
        for idx, sn in enumerate(item["snippets"]):
            if not isinstance(sn, dict):
                continue
            text = sn.get("text")
            aspect = sn.get("aspect")
            if not isinstance(text, str) or not text.strip() or not isinstance(aspect, str) or not aspect:
                continue
            text = text.strip()

            query_texts.append(text)
            query_meta.append({
                "role": "positive", "asin": asin, "snippet_index": idx, "aspect": aspect,
                "source_type": sn.get("source_type"), "text": text,
            })

            neg = sample_negative_snippet(pool, aspect, asin, rng)
            if neg is None:
                skipped_negative += 1
                continue
            query_texts.append(neg["text"])
            query_meta.append({
                "role": "negative", "asin": asin, "aspect": aspect,
                "negative_source_asin": neg["asin"], "text": neg["text"],
            })

    logger.info(
        "embedding %d document paragraph(s), %d query snippet(s) (%d negative skipped: no cross-ASIN candidate)",
        len(doc_texts), len(query_texts), skipped_negative,
    )
    doc_vectors = embed_texts_ruri(doc_texts, "document", ruri_url, session, batch_size=batch_size, sleeper=sleeper)
    query_vectors = embed_texts_ruri(query_texts, "query", ruri_url, session, batch_size=batch_size, sleeper=sleeper)

    doc_by_asin: dict[str, dict[str, list[float]]] = {}
    for (asin, key), vec in zip(doc_index, doc_vectors):
        doc_by_asin.setdefault(asin, {})[key] = vec

    positive_results: list[dict[str, Any]] = []
    negative_sims: list[float] = []

    for meta, qv in zip(query_meta, query_vectors):
        paragraphs = doc_by_asin.get(meta["asin"], {})
        if not paragraphs:
            continue
        best_key, best_sim = None, -1.0
        for key, dv in paragraphs.items():
            s = cosine_similarity(qv, dv)
            if s > best_sim:
                best_sim, best_key = s, key
        if meta["role"] == "positive":
            rec = dict(meta)
            rec["max_sim"] = round(best_sim, 4)
            rec["matched_key"] = best_key
            positive_results.append(rec)
        else:
            negative_sims.append(best_sim)

    # matched_text は表示用にオリジナルの段落文字列 (paragraphs dict) から引く
    paragraphs_text_by_asin = {item["asin"]: item["paragraphs"] for item in included}
    for rec in positive_results:
        text = paragraphs_text_by_asin.get(rec["asin"], {}).get(rec["matched_key"], "")
        rec["matched_text"] = _truncate(text, SAMPLE_TEXT_MAX_LEN)
        rec["text"] = _truncate(rec["text"], SAMPLE_TEXT_MAX_LEN)

    threshold = percentile(negative_sims, threshold_percentile)
    for rec in positive_results:
        rec["used"] = (threshold is not None) and (rec["max_sim"] > threshold)

    # 記事単位
    used_by_asin: dict[str, bool] = {}
    for rec in positive_results:
        used_by_asin[rec["asin"]] = used_by_asin.get(rec["asin"], False) or bool(rec["used"])
    articles_with_used = sum(1 for v in used_by_asin.values() if v)

    article_level = {
        "total_articles": len(included),
        "articles_with_used_snippet": articles_with_used,
        "fraction": round(articles_with_used / len(included), 4) if included else None,
    }

    snippet_level = {
        "overall": _rate_stats(positive_results),
        "by_aspect": {k: _rate_stats(v) for k, v in sorted(_group_by(positive_results, "aspect").items())},
        "by_source_type": {k: _rate_stats(v) for k, v in sorted(_group_by(positive_results, "source_type").items())},
    }

    positive_sims = [r["max_sim"] for r in positive_results]
    threshold_info = {
        "value": threshold,
        "method": f"negative_control_p{threshold_percentile:g}",
        "seed": seed,
        "negative_skipped_no_candidate": skipped_negative,
        "negative_distribution": distribution_stats(negative_sims),
        "positive_distribution": distribution_stats(positive_sims),
        "overlap_coefficient": histogram_overlap(positive_sims, negative_sims),
    }

    # --------------------------------------------------------------------
    # R1: 同一 ASIN 対照 (素材より前に書かれた記事) の採点
    # --------------------------------------------------------------------
    control_doc_texts: list[str] = []
    control_doc_index: list[tuple[str, str]] = []
    for item in control:
        for key, text in item["paragraphs"].items():
            control_doc_texts.append(text)
            control_doc_index.append((item["asin"], key))

    control_query_texts: list[str] = []
    control_query_meta: list[dict[str, Any]] = []
    for item in control:
        asin = item["asin"]
        for idx, sn in enumerate(item["snippets"]):
            if not isinstance(sn, dict):
                continue
            text = sn.get("text")
            aspect = sn.get("aspect")
            if not isinstance(text, str) or not text.strip() or not isinstance(aspect, str) or not aspect:
                continue
            text = text.strip()
            control_query_texts.append(text)
            control_query_meta.append({
                "asin": asin, "snippet_index": idx, "aspect": aspect, "source_type": sn.get("source_type"),
            })

    logger.info(
        "embedding %d control document paragraph(s), %d control query snippet(s)",
        len(control_doc_texts), len(control_query_texts),
    )
    control_doc_vectors = embed_texts_ruri(
        control_doc_texts, "document", ruri_url, session, batch_size=batch_size, sleeper=sleeper,
    )
    control_query_vectors = embed_texts_ruri(
        control_query_texts, "query", ruri_url, session, batch_size=batch_size, sleeper=sleeper,
    )

    control_doc_by_asin: dict[str, dict[str, list[float]]] = {}
    for (asin, key), vec in zip(control_doc_index, control_doc_vectors):
        control_doc_by_asin.setdefault(asin, {})[key] = vec

    control_results: list[dict[str, Any]] = []
    for meta, qv in zip(control_query_meta, control_query_vectors):
        paragraphs = control_doc_by_asin.get(meta["asin"], {})
        if not paragraphs:
            continue
        best_key, best_sim = None, -1.0
        for key, dv in paragraphs.items():
            s = cosine_similarity(qv, dv)
            if s > best_sim:
                best_sim, best_key = s, key
        rec = dict(meta)
        rec["max_sim"] = round(best_sim, 4)
        rec["matched_key"] = best_key
        # 元の (別ASIN負の対照由来の) 閾値を超えるかどうか。この記事は素材
        # より前に書かれているので、超えても「同じ商品の話をしている」こと
        # の証拠にしかならない (「使った」との分離ができない部分、R1)。
        rec["used"] = (threshold is not None) and (rec["max_sim"] > threshold)
        control_results.append(rec)

    control_sims = [r["max_sim"] for r in control_results]
    same_asin_control_snippet_level = {
        "overall": _rate_stats(control_results),
        "by_aspect": {k: _rate_stats(v) for k, v in sorted(_group_by(control_results, "aspect").items())},
        "by_source_type": {k: _rate_stats(v) for k, v in sorted(_group_by(control_results, "source_type").items())},
    }
    same_asin_control = {
        "total_articles": len(control),
        "asins": sorted(item["asin"] for item in control),
        "snippet_level": same_asin_control_snippet_level,
        "distribution": distribution_stats(control_sims),
    }

    # R1: 見出しの数字はこの差にする。included (元の閾値超過率) から同一ASIN
    # 対照 (同じ閾値の超過率) を引くと、「同じ商品の話をしている」ぶんが
    # 相殺され、素材が実際に使われた分の上限に近づく (交絡は R3 参照)。
    diff = {
        "overall": _diff_rate(snippet_level["overall"], same_asin_control_snippet_level["overall"]),
        "by_aspect": {
            k: _diff_rate(v, same_asin_control_snippet_level["by_aspect"].get(k))
            for k, v in snippet_level["by_aspect"].items()
        },
        "by_source_type": {
            k: _diff_rate(v, same_asin_control_snippet_level["by_source_type"].get(k))
            for k, v in snippet_level["by_source_type"].items()
        },
    }

    # R1: 対照自身の p95 を閾値にした場合、included の使用率がどう動くかも
    # 出す (別ASIN負の対照由来の閾値より厳しい/緩いかもしれないため感度として)。
    control_threshold = percentile(control_sims, threshold_percentile)
    for rec in positive_results:
        rec["used_alt"] = (control_threshold is not None) and (rec["max_sim"] > control_threshold)
    used_by_asin_alt: dict[str, bool] = {}
    for rec in positive_results:
        used_by_asin_alt[rec["asin"]] = used_by_asin_alt.get(rec["asin"], False) or bool(rec["used_alt"])
    articles_with_used_alt = sum(1 for v in used_by_asin_alt.values() if v)
    alt_threshold_from_control = {
        "value": control_threshold,
        "method": f"same_asin_control_p{threshold_percentile:g}",
        "article_level": {
            "total_articles": len(included),
            "articles_with_used_snippet": articles_with_used_alt,
            "fraction": round(articles_with_used_alt / len(included), 4) if included else None,
        },
        "snippet_level": {
            "overall": _rate_stats(positive_results, key="used_alt"),
            "by_aspect": {
                k: _rate_stats(v, key="used_alt") for k, v in sorted(_group_by(positive_results, "aspect").items())
            },
            "by_source_type": {
                k: _rate_stats(v, key="used_alt")
                for k, v in sorted(_group_by(positive_results, "source_type").items())
            },
        },
    }

    sorted_by_sim = sorted(positive_results, key=lambda r: r["max_sim"])
    if threshold is None:
        above_samples, below_samples = [], []
    else:
        above = [r for r in sorted_by_sim if r["max_sim"] >= threshold]
        below = [r for r in sorted_by_sim if r["max_sim"] < threshold]
        above_samples = above[:SAMPLE_COUNT]
        below_samples = below[-SAMPLE_COUNT:][::-1] if below else []

    def _sample_view(r: dict[str, Any]) -> dict[str, Any]:
        return {
            "asin": r["asin"], "aspect": r["aspect"], "source_type": r.get("source_type"),
            "snippet_text": r["text"], "max_sim": r["max_sim"],
            "matched_key": r["matched_key"], "matched_text": r["matched_text"],
        }

    payload = {
        "generated_at": _now_iso(),
        "embed_model": resolved_model,
        "elapsed_seconds": round(time.monotonic() - started, 1),
        "include_caveat_fields": include_caveat_fields,
        "population": population,
        "threshold": threshold_info,
        "article_level": article_level,
        "snippet_level": snippet_level,
        # #4841 U1: matched_key の粗い枠 (cons/verdict/safety_note/その他) の
        # aspect別内訳。不満が実際にどの枠に着地しているかを見る。
        "matched_key_breakdown": matched_key_breakdown_by_aspect(positive_results),
        "same_asin_control": same_asin_control,
        "diff": diff,
        "alt_threshold_from_control": alt_threshold_from_control,
        "samples": {
            "above_threshold": [_sample_view(r) for r in above_samples],
            "below_threshold": [_sample_view(r) for r in below_samples],
        },
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(
        "wrote %s: included=%d threshold=%s article_fraction=%s snippet_rate=%s "
        "control_rate=%s diff=%s",
        out_path, len(included), threshold, article_level["fraction"], snippet_level["overall"]["rate"],
        same_asin_control_snippet_level["overall"]["rate"], diff["overall"],
    )
    return {"included": len(included), "written": True, "payload": payload}


def main() -> int:
    ap = argparse.ArgumentParser(
        description="experience.json の snippet が記事本文で実際に使われているかを Ruri 類似度で測る (#4841 T1)"
    )
    ap.add_argument("--experience-glob", default=DEFAULT_EXPERIENCE_GLOB)
    ap.add_argument("--articles-dir", default=DEFAULT_ARTICLES_DIR)
    ap.add_argument("--out", default=None, help=f"default: {DEFAULT_OUT} ({DEFAULT_OUT_CAVEAT} with --include-caveat-fields)")
    ap.add_argument("--ruri-url", default=os.environ.get("RURI_URL", DEFAULT_RURI_URL))
    ap.add_argument("--model", default=os.environ.get("EMBED_MODEL", DEFAULT_MODEL_RURI))
    ap.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    ap.add_argument("--seed", type=int, default=NEGATIVE_CONTROL_SEED)
    ap.add_argument("--threshold-percentile", type=float, default=THRESHOLD_PERCENTILE)
    ap.add_argument(
        "--include-caveat-fields", action="store_true",
        help="product.cons の各要素と verdict.headline も採点対象に加える (#4841 U1)。既定は narrative + editorial_comment のみ",
    )
    args = ap.parse_args()
    out_path = args.out or (DEFAULT_OUT_CAVEAT if args.include_caveat_fields else DEFAULT_OUT)

    try:
        run(
            experience_glob=args.experience_glob,
            articles_dir=args.articles_dir,
            out_path=pathlib.Path(out_path),
            ruri_url=args.ruri_url,
            model=args.model,
            batch_size=args.batch_size,
            seed=args.seed,
            threshold_percentile=args.threshold_percentile,
            include_caveat_fields=args.include_caveat_fields,
        )
    except EmbeddingBatchError as e:
        logger.error("embedding failed: %s", e)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
