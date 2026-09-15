"""audit_information_gain.py

Issue #4841 S3「物差し (M1) を本番の週次監査に載せる」の計算スクリプト。

M1 (#4841) で採用した物差し「固有かつ裏付けありの文」
(``scripts/experimental/multistage_brief/sentence_metrics.py`` /
``unsupported_classification.py``) を本番の週次レーンとして移したもの。
狙いは素材の供給 (体験談レーン等) の変化が、記事の情報利得に効いているかを
定点観測すること。**観測専用。自動リライト等には連動しない。**

指標 (文単位):
  1. narrative を句点で文に分割する (LLM を使わず決定的に)
  2. 各文が ASIN の素材 (data/raw/per_asin/<ASIN>/) から導けるかを gemma で判定する
  3. 裏付けありの各文について、同じカテゴリの既存記事の文との Ruri 最大類似度を取り、
     別カテゴリの文の分布 (負の対照) の p95 未満なら「固有」とする
  4. 主指標: 固有かつ裏付けありの文の数と率。ガードレール: 裏付けの無い文の数
  5. 裏付けの無い文はさらに「修辞・時点依存」/「事実の主張」に分類する

対象の選び方 (全 2,500 記事は約49時間かかり週次では回せない、M1-c実測 約70秒/記事):
  その週に `data/articles/` へ追加・更新コミットがあった記事から、固定 seed で
  最大 ``--limit`` 件 (既定40) を抽出する。各記事について
  `experience.json` が生成時点で存在したか (``has_experience_material``) を記録し、
  素材供給の有無別の群比較に使う。

堅牢性:
  - gemma の出力 JSON が壊れた記事は、その記事だけ失敗として数えて続行する
    (#4841 M2 で 1 件の破損が run 全体を落とした反省)
  - num_ctx を明示し、切り詰め (ollama が無言で入力を約半分に切り詰める挙動、
    #4528 実測) を検出した試行は失敗として扱う
  - 失敗が対象の 20% を超えたら run を失敗として報告する (呼び出し元が exit code で判定)

キャッシュ:
  結果は narrative のハッシュ + 素材テキストのハッシュ + モデル ID をキーに
  キャッシュする (K8 の named volume 等、**リポジトリには置かない**)。同じ週を
  2 回 dispatch した場合の再計算を避ける。

出力:
  - data/analytics/information_gain_audit.json (その週のスナップショット)
  - data/analytics/information_gain_history.jsonl への追記は
    scripts/append_information_gain_history.py が担当 (append_uniqueness_audit_history.py
    と同型の分離)

呼び出し元:
  omochairo/amazon-home-ops リポジトリの週次 workflow から
  `python -m scripts.audit_information_gain ...` として実行される想定。

Issue: https://github.com/omochairo/amazon/issues/4841 (S3)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import pathlib
import random
import re
import statistics
import subprocess
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from scripts.audit_experience_usage import NARRATIVE_KEYS, build_paragraph_map, cosine_similarity, percentile
from scripts.compute_semantic_related import DEFAULT_RURI_URL, discover_articles, embed_batch_ruri

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("audit_information_gain")

DEFAULT_ARTICLES_DIR = "data/articles"
DEFAULT_RAW_DIR = "data/raw/per_asin"
DEFAULT_OUT = "data/analytics/information_gain_audit.json"
DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_MODEL = "gemma4:26b-a4b-it-qat"
DEFAULT_NUM_CTX = 8192
DEFAULT_SEED = 20260914
DEFAULT_LIMIT = 40
DEFAULT_POOL_SAMPLE_SIZE = 30
DEFAULT_MAX_POOL_SENTENCES = 300
THRESHOLD_PERCENTILE = 95.0
# #4841 実装依頼 S3: 失敗がこの割合を超えたら run を失敗として報告する。
MAX_FAILURE_RATIO = 0.20
REQUEST_TIMEOUT = 300
_MAX_EXTRA_RETRIES = 2
_RETRY_SLEEP_SECONDS = 2.0

# ollama が num_ctx 超過時に入力を無言で切り詰める挙動の検出しきい値
# (scripts/experimental/multistage_brief/ollama_client.py と同じ、#4841 実装依頼)。
TRUNCATION_RATIO_THRESHOLD = 0.7
NUM_CTX_HEADROOM_THRESHOLD = 0.9

_SIDECAR_SUFFIXES = (".quality.json", ".seo.json", ".enrichment.json")
_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？])")


# --------------------------------------------------------------------------
# gemma (ollama) 呼び出し共通ラッパー
# --------------------------------------------------------------------------
# scripts/experimental/multistage_brief/ollama_client.py の call_gemma を本番へ
# 移したもの (#4841 実装依頼 S3「LLM 呼び出しの共通要件」)。


class TruncationError(Exception):
    """ollama が num_ctx を超えて入力を無言で切り詰めた疑いがある。"""


class GemmaCallError(Exception):
    """gemma 呼び出しがリトライ上限まで失敗した。"""


def estimate_tokens(text: str) -> int:
    """日本語混じりテキストの雑なトークン数見積もり (prompt_eval_count との比較専用)。"""
    return max(1, len(text) // 2)


def call_gemma(
    prompt: str,
    *,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    model: str = DEFAULT_MODEL,
    num_ctx: int = DEFAULT_NUM_CTX,
    temperature: float = 0.0,
    seed: int = DEFAULT_SEED,
    format_json: bool = True,
    session: requests.Session | None = None,
    sleeper=time.sleep,
    timeout: int = REQUEST_TIMEOUT,
) -> dict[str, Any]:
    """gemma に1回問い合わせ、テキストと呼び出しメタデータを返す。

    切り詰めを検出した場合は ``TruncationError`` を送出する (呼び出し元は
    その試行を失敗として扱うこと。黙って続行しない)。
    """
    session = session or requests.Session()
    url = f"{ollama_url.rstrip('/')}/api/generate"
    body: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "think": False,
        "keep_alive": "30m",
        "options": {"temperature": temperature, "num_ctx": num_ctx, "seed": seed},
    }
    if format_json:
        body["format"] = "json"

    last_err: Exception | None = None
    attempts = _MAX_EXTRA_RETRIES + 1
    for attempt in range(1, attempts + 1):
        try:
            resp = session.post(url, json=body, timeout=timeout)
            resp.raise_for_status()
            payload = resp.json()
            text = payload.get("response") if isinstance(payload, dict) else None
            if not isinstance(text, str) or not text.strip():
                raise GemmaCallError("empty /api/generate response")

            prompt_eval_count = payload.get("prompt_eval_count") or 0
            estimated = estimate_tokens(prompt)
            truncation_reasons: list[str] = []
            if prompt_eval_count and prompt_eval_count < TRUNCATION_RATIO_THRESHOLD * estimated:
                truncation_reasons.append(
                    f"prompt_eval_count({prompt_eval_count}) < "
                    f"{TRUNCATION_RATIO_THRESHOLD}*estimated({estimated})"
                )
            if prompt_eval_count >= NUM_CTX_HEADROOM_THRESHOLD * num_ctx:
                truncation_reasons.append(
                    f"prompt_eval_count({prompt_eval_count}) >= "
                    f"{NUM_CTX_HEADROOM_THRESHOLD}*num_ctx({num_ctx})"
                )

            result = {
                "text": text,
                "model_id": payload.get("model") or model,
                "prompt_eval_count": prompt_eval_count,
                "estimated_prompt_tokens": estimated,
                "num_ctx": num_ctx,
                "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                "truncated": bool(truncation_reasons),
                "truncation_reason": "; ".join(truncation_reasons) or None,
            }
            if result["truncated"]:
                logger.error("gemma call truncated: %s", result["truncation_reason"])
                raise TruncationError(result["truncation_reason"])
            return result
        except (requests.RequestException, GemmaCallError, ValueError, json.JSONDecodeError) as e:
            last_err = e
            if attempt < attempts:
                logger.warning("gemma call failed (attempt %d/%d): %s; retrying", attempt, attempts, e)
                sleeper(_RETRY_SLEEP_SECONDS)
            else:
                logger.error("gemma call failed after %d attempt(s): %s", attempts, e)

    raise GemmaCallError(str(last_err)) from last_err


_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_json_response(text: str) -> Any:
    """gemma の応答テキストから JSON を取り出す (コードフェンス混入に耐える)。"""
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    cleaned = _CODE_FENCE_RE.sub("", stripped).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(f"failed to parse JSON from gemma response: {e}\n---\n{text[:500]}") from e


# --------------------------------------------------------------------------
# 文分割・文プール構築 (#4841 M1-b。LLM を使わない決定的な分割)
# --------------------------------------------------------------------------

def split_sentences(text: str) -> list[str]:
    """句点 (。！？) ベースで決定的に文へ分割する (LLM を使わない)。"""
    if not isinstance(text, str) or not text.strip():
        return []
    parts = _SENTENCE_SPLIT_RE.split(text)
    return [p.strip() for p in parts if p and p.strip()]


def flatten_narrative_sentences(
    narrative: dict[str, Any], keys: tuple[str, ...] = NARRATIVE_KEYS,
) -> list[dict[str, Any]]:
    """narrative の各キーを文に分割し、{key, sentence} のフラットなリストにする。

    本番記事の narrative セクションは string (旧形式) と array (現行形式) の
    両方があり得るため、build_paragraph_map と同じ正規化を経由してから句点で
    再分割する (array 形式のキーを黙って読み飛ばすと文がほぼ0件に潰れる、
    #4841 M1 実データ検証で発覚した実バグ)。
    """
    paragraphs = build_paragraph_map({"narrative": narrative})
    out: list[dict[str, Any]] = []
    for key in keys:
        text = paragraphs.get(key)
        if not text:
            continue
        for sentence in split_sentences(text):
            out.append({"key": key, "sentence": sentence})
    return out


def build_sentence_pool(articles: list[dict[str, Any]], max_sentences: int = DEFAULT_MAX_POOL_SENTENCES) -> list[str]:
    """記事一覧の narrative を文に分割し、プールにする (固有性判定のコーパス)。"""
    out: list[str] = []
    for article in articles:
        narrative = article.get("narrative") if isinstance(article, dict) else None
        if not isinstance(narrative, dict):
            continue
        for item in flatten_narrative_sentences(narrative):
            out.append(item["sentence"])
            if len(out) >= max_sentences:
                return out
    return out


# --------------------------------------------------------------------------
# 根拠判定 (文単位、バッチ)
# --------------------------------------------------------------------------

ENTAILMENT_SENTENCE_PROMPT_TEMPLATE = """あなたは記事の事実確認担当です。以下の「素材」に書かれている内容だけを根拠として、
「文一覧」の各文が、素材から導けるかどうかを番号ごとに判定してください。

# 素材
{material_text}

# 文一覧 (番号付き)
{numbered_sentences}

各番号について、素材から導けるなら true、導けないなら false としてください。
文一覧と同じ個数・同じ順序で、次の JSON だけを出力してください (他の説明文は一切含めない):
{{"judgments": [{{"index": 1, "supported": true}}, {{"index": 2, "supported": false}}, ...]}}
"""


def build_entailment_prompt(material_text: str, sentences: list[str]) -> str:
    numbered = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(sentences))
    return ENTAILMENT_SENTENCE_PROMPT_TEMPLATE.format(material_text=material_text, numbered_sentences=numbered)


def parse_entailment_response(parsed: Any, sentence_count: int) -> dict[str, Any]:
    """gemma の judgments 配列を index -> supported にマップする (pure)。

    index が範囲外・重複・欠落している場合は無視/記録するだけで例外にしない
    (「未判定」も有効な結果として扱う。黙って supported/unsupported どちらかに倒さない)。
    """
    judgments = parsed.get("judgments") if isinstance(parsed, dict) else None
    judgments = judgments if isinstance(judgments, list) else []

    by_index: dict[int, bool] = {}
    for j in judgments:
        if not isinstance(j, dict):
            continue
        idx = j.get("index")
        supported = j.get("supported")
        if not isinstance(idx, int) or not isinstance(supported, bool):
            continue
        if 1 <= idx <= sentence_count:
            by_index[idx] = supported

    supported_flags: list[bool | None] = [by_index.get(i + 1) for i in range(sentence_count)]
    unresolved_indices = [i + 1 for i, v in enumerate(supported_flags) if v is None]
    return {"supported_flags": supported_flags, "unresolved_indices": unresolved_indices}


def run_sentence_entailment(
    material_text: str,
    sentences: list[str],
    *,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    model: str = DEFAULT_MODEL,
    num_ctx: int = DEFAULT_NUM_CTX,
    session=None,
) -> dict[str, Any]:
    """文単位の根拠判定を1回の gemma 呼び出しでまとめて行う。"""
    if not sentences:
        return {"supported_flags": [], "unresolved_indices": [], "call_meta": None}
    prompt = build_entailment_prompt(material_text, sentences)
    call = call_gemma(prompt, ollama_url=ollama_url, model=model, num_ctx=num_ctx, format_json=True, session=session)
    parsed = parse_json_response(call["text"])
    result = parse_entailment_response(parsed, len(sentences))
    result["call_meta"] = {k: v for k, v in call.items() if k != "text"}
    return result


# --------------------------------------------------------------------------
# 固有性判定 (Ruri、負の対照で較正)
# --------------------------------------------------------------------------

def _embed(texts: list[str], ruri_url: str, session: requests.Session) -> list[list[float]]:
    if not texts:
        return []
    return embed_batch_ruri(texts, ruri_url, session)


def compute_sentence_uniqueness(
    sentences: list[str],
    same_category_pool: list[str],
    cross_category_pool: list[str],
    *,
    ruri_url: str,
    session: requests.Session,
    threshold_percentile: float = THRESHOLD_PERCENTILE,
) -> dict[str, Any]:
    """文ごとの固有性を判定する。

    each sentence の「同じカテゴリの既存記事の文」との最大類似度 (same_category_max_sim)
    を出す。閾値は「別カテゴリの文」に対する同じ sentences の最大類似度分布
    (負の対照) の p95 とする。same_category_max_sim が閾値未満なら固有 (unique=True)。

    プールが空 (コーパスが無い) 場合は判定不能として unique=None を返す
    (固有=False に倒さない。判定できないことを明示する)。
    """
    if not sentences:
        return {"per_sentence": [], "threshold": None}

    sentence_vectors = _embed(sentences, ruri_url, session)

    if not same_category_pool or not cross_category_pool:
        per_sentence = [
            {"sentence": s, "same_category_max_sim": None, "unique": None} for s in sentences
        ]
        return {"per_sentence": per_sentence, "threshold": None}

    same_vectors = _embed(same_category_pool, ruri_url, session)
    cross_vectors = _embed(cross_category_pool, ruri_url, session)

    cross_sims = [
        max(cosine_similarity(sv, cv) for cv in cross_vectors) for sv in sentence_vectors
    ]
    threshold = percentile(cross_sims, threshold_percentile)

    per_sentence = []
    for sentence, sv, cross_sim in zip(sentences, sentence_vectors, cross_sims):
        same_max = max(cosine_similarity(sv, dv) for dv in same_vectors)
        unique = (threshold is not None) and (same_max < threshold)
        per_sentence.append({
            "sentence": sentence,
            "same_category_max_sim": round(same_max, 4),
            "cross_category_max_sim": round(cross_sim, 4),
            "unique": unique,
        })

    return {
        "per_sentence": per_sentence,
        "threshold": threshold,
        "threshold_method": f"cross_category_p{threshold_percentile:g}",
        "cross_category_distribution_count": len(cross_sims),
    }


# --------------------------------------------------------------------------
# 指標の合成
# --------------------------------------------------------------------------

def compute_information_gain(
    narrative: dict[str, str],
    material_text: str,
    same_category_pool: list[str],
    cross_category_pool: list[str],
    *,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    ruri_url: str,
    model: str = DEFAULT_MODEL,
    num_ctx: int = DEFAULT_NUM_CTX,
    session=None,
) -> dict[str, Any]:
    """narrative 1件ぶんの情報利得指標をまとめて計算する。

    指標: 固有かつ裏付けありの文の数 (主) / 裏付けの無い文の数 (ガードレール) /
    未判定の文の数 (根拠判定が index 不整合で解決できなかった件数)。
    """
    flat = flatten_narrative_sentences(narrative)
    sentences = [f["sentence"] for f in flat]

    entail = run_sentence_entailment(
        material_text, sentences, ollama_url=ollama_url, model=model, num_ctx=num_ctx, session=session,
    )
    uniqueness = compute_sentence_uniqueness(
        sentences, same_category_pool, cross_category_pool, ruri_url=ruri_url, session=session,
    )

    per_sentence = []
    unique_and_supported = 0
    unsupported = 0
    unresolved = 0
    uniq_by_sentence = {u["sentence"]: u for u in uniqueness["per_sentence"]}
    for i, item in enumerate(flat):
        supported = entail["supported_flags"][i] if i < len(entail["supported_flags"]) else None
        uniq = uniq_by_sentence.get(item["sentence"], {})
        unique = uniq.get("unique")
        row = {
            "key": item["key"],
            "sentence": item["sentence"],
            "supported": supported,
            "unique": unique,
            "same_category_max_sim": uniq.get("same_category_max_sim"),
        }
        per_sentence.append(row)
        if supported is None:
            unresolved += 1
        elif supported is False:
            unsupported += 1
        elif supported is True and unique is True:
            unique_and_supported += 1

    return {
        "sentence_count": len(flat),
        "unique_and_supported_count": unique_and_supported,
        "unsupported_count": unsupported,
        "unresolved_count": unresolved,
        "threshold": uniqueness.get("threshold"),
        "per_sentence": per_sentence,
        "entailment_call_meta": entail.get("call_meta"),
    }


# --------------------------------------------------------------------------
# 裏付けの無い文の分類 (修辞・時点依存 / 事実の主張)
# --------------------------------------------------------------------------

RHETORICAL_OR_TIME_DEPENDENT = "rhetorical_or_time_dependent"
FACTUAL_CLAIM = "factual_claim"
VALID_CATEGORIES = (RHETORICAL_OR_TIME_DEPENDENT, FACTUAL_CLAIM)

CLASSIFY_PROMPT_TEMPLATE = """以下は、素材から根拠を確認できなかった(「裏付けなし」と判定された) 文の一覧です。
各文を次の2種類のどちらかに分類してください。

- {rhetorical}: 修辞的な言い回し (煽り文句・一般的な導入・感想的な表現) か、
  価格・在庫・セール・「今」「現在」のような時点に依存して変わりうる言明
- {factual}: 商品の仕様・機能・素材・安全性など、時点に依存しない具体的な事実の主張
  (誤っていれば読者に実害がある可能性がある)

# 文一覧 (番号付き)
{numbered_sentences}

各番号について分類してください。文一覧と同じ個数・同じ順序で、次の JSON だけを
出力してください (他の説明文は一切含めない):
{{"classifications": [{{"index": 1, "category": "{rhetorical}"}}, {{"index": 2, "category": "{factual}"}}, ...]}}
"""


def build_classify_prompt(sentences: list[str]) -> str:
    numbered = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(sentences))
    return CLASSIFY_PROMPT_TEMPLATE.format(
        rhetorical=RHETORICAL_OR_TIME_DEPENDENT, factual=FACTUAL_CLAIM, numbered_sentences=numbered,
    )


def parse_classify_response(parsed: Any, sentence_count: int) -> list[str | None]:
    """gemma の classifications 配列を index -> category にマップする (pure)。"""
    classifications = parsed.get("classifications") if isinstance(parsed, dict) else None
    classifications = classifications if isinstance(classifications, list) else []

    by_index: dict[int, str] = {}
    for c in classifications:
        if not isinstance(c, dict):
            continue
        idx = c.get("index")
        category = c.get("category")
        if not isinstance(idx, int) or category not in VALID_CATEGORIES:
            continue
        if 1 <= idx <= sentence_count:
            by_index[idx] = category

    return [by_index.get(i + 1) for i in range(sentence_count)]


def classify_unsupported_sentences(
    sentences: list[str],
    *,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    model: str = DEFAULT_MODEL,
    num_ctx: int = DEFAULT_NUM_CTX,
    session=None,
) -> dict[str, Any]:
    if not sentences:
        return {"categories": [], "call_meta": None}
    prompt = build_classify_prompt(sentences)
    call = call_gemma(prompt, ollama_url=ollama_url, model=model, num_ctx=num_ctx, format_json=True, session=session)
    parsed = parse_json_response(call["text"])
    categories = parse_classify_response(parsed, len(sentences))
    return {"categories": categories, "call_meta": {k: v for k, v in call.items() if k != "text"}}


def summarize_categories(categories: list[str | None]) -> dict[str, int]:
    """カテゴリ一覧を件数の内訳にまとめる (pure)。"""
    return {
        RHETORICAL_OR_TIME_DEPENDENT: sum(1 for c in categories if c == RHETORICAL_OR_TIME_DEPENDENT),
        FACTUAL_CLAIM: sum(1 for c in categories if c == FACTUAL_CLAIM),
        "unresolved": sum(1 for c in categories if c is None),
    }


# --------------------------------------------------------------------------
# 素材読み込み (data/raw/per_asin/<ASIN>/、読み取り専用)
# --------------------------------------------------------------------------

MAX_MATERIAL_TEXT_LEN = 4000
MAX_FEATURES = 6
MAX_COMPETITORS = 5
MAX_SNIPPETS_PER_ASPECT = 3
MAX_YOUTUBE_TITLES = 5
MAX_NEWS_HEADLINES = 5


def _load_json(path: pathlib.Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def load_raw_material(asin: str, raw_dir: str | pathlib.Path = DEFAULT_RAW_DIR) -> dict[str, Any]:
    """data/raw/per_asin/<ASIN>/ の全ファイルを dict にまとめて読み込む。"""
    base = pathlib.Path(raw_dir) / asin
    return {
        "asin": asin,
        "amazon": _load_json(base / "amazon.json"),
        "competitors": _load_json(base / "competitors.json"),
        "experience": _load_json(base / "experience.json"),
        "youtube": _load_json(base / "youtube.json"),
        "news": _load_json(base / "news.json"),
    }


def _item_field(item: Any, *names: str) -> Any:
    if not isinstance(item, dict):
        return None
    for n in names:
        if n in item and item[n] not in (None, ""):
            return item[n]
    return None


def experience_snippets_by_aspect(raw_material: dict[str, Any]) -> dict[str, list[str]]:
    exp = raw_material.get("experience")
    if not isinstance(exp, dict):
        return {}
    snippets = exp.get("snippets")
    if not isinstance(snippets, list):
        return {}
    out: dict[str, list[str]] = {}
    for s in snippets:
        if not isinstance(s, dict):
            continue
        aspect = s.get("aspect")
        text = s.get("text")
        if isinstance(aspect, str) and isinstance(text, str) and text.strip():
            out.setdefault(aspect, []).append(text.strip())
    return out


def build_material_text(raw_material: dict[str, Any], max_len: int = MAX_MATERIAL_TEXT_LEN) -> str:
    """gemma プロンプトに埋め込む「素材」テキストを組み立てる。どのソースが欠けても落ちない。"""
    parts: list[str] = []

    amazon = raw_material.get("amazon")
    item = amazon.get("item") if isinstance(amazon, dict) else None
    if isinstance(item, dict):
        name = _item_field(item, "title", "name_full", "name")
        price = _item_field(item, "price")
        features = _item_field(item, "features")
        lines = ["## 商品データ (Amazon)"]
        if name:
            lines.append(f"商品名: {name}")
        if price:
            lines.append(f"価格: {price}円")
        if isinstance(features, list) and features:
            f_strs = [str(f).strip() for f in features if str(f).strip()][:MAX_FEATURES]
            if f_strs:
                lines.append("特徴: " + " / ".join(f_strs))
        if len(lines) > 1:
            parts.append("\n".join(lines))

    comp = raw_material.get("competitors")
    comp_items = comp.get("competitors") if isinstance(comp, dict) else None
    if isinstance(comp_items, list) and comp_items:
        lines = ["## 競合商品"]
        for c in comp_items[:MAX_COMPETITORS]:
            if not isinstance(c, dict):
                continue
            c_asin = c.get("asin", "")
            c_name = c.get("name", "")
            c_price = c.get("price")
            price_str = f"{c_price}円" if c_price else "価格不明"
            lines.append(f"- [{c_asin}] {c_name} ({price_str})")
        if len(lines) > 1:
            parts.append("\n".join(lines))

    snippets_by_aspect = experience_snippets_by_aspect(raw_material)
    if snippets_by_aspect:
        lines = ["## 体験談・レビューの要約 (aspect 別)"]
        for aspect, texts in snippets_by_aspect.items():
            for t in texts[:MAX_SNIPPETS_PER_ASPECT]:
                lines.append(f"- [{aspect}] {t}")
        parts.append("\n".join(lines))

    youtube = raw_material.get("youtube")
    yt_items = youtube.get("items") if isinstance(youtube, dict) else None
    if isinstance(yt_items, list) and yt_items:
        titles = [str(_item_field(v, "title") or "").strip() for v in yt_items if isinstance(v, dict)]
        titles = [t for t in titles if t][:MAX_YOUTUBE_TITLES]
        if titles:
            parts.append("## 関連動画タイトル\n" + "\n".join(f"- {t}" for t in titles))

    news = raw_material.get("news")
    news_items = news.get("items") if isinstance(news, dict) else None
    if isinstance(news_items, list) and news_items:
        heads = [str(_item_field(n, "title") or "").strip() for n in news_items if isinstance(n, dict)]
        heads = [h for h in heads if h][:MAX_NEWS_HEADLINES]
        if heads:
            parts.append("## 関連ニュース見出し\n" + "\n".join(f"- {h}" for h in heads))

    text = "\n\n".join(parts)
    if len(text) > max_len:
        text = text[: max_len - 1] + "…"
    return text


def _parse_iso(ts: Any) -> datetime | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def has_experience_material_at_generation(raw_material: dict[str, Any], article: dict[str, Any]) -> bool | None:
    """experience.json が記事の生成時点で main に入っていたか (群比較の軸)。

    audit_experience_usage.select_population の population 判定 (T1) と
    同じ考え方: experience.json の generated_at が記事の date より前であれば
    「生成時点で使えた」とみなす。experience.json が無い/日付が読めない場合は
    None (「無かった」と「判定不能」を区別する)。
    """
    exp = raw_material.get("experience")
    if not isinstance(exp, dict):
        return False
    gen_dt = _parse_iso(exp.get("generated_at"))
    article_dt = _parse_iso(article.get("date"))
    if gen_dt is None or article_dt is None:
        return None
    return gen_dt <= article_dt


# --------------------------------------------------------------------------
# カテゴリ (product.edu_domains の先頭要素)・同/別カテゴリのコーパスサンプリング
# --------------------------------------------------------------------------

def article_category(article: dict[str, Any]) -> str:
    """product.edu_domains の先頭要素をカテゴリとして使う (STEM/言語/運動/想像)。"""
    product = article.get("product")
    domains = product.get("edu_domains") if isinstance(product, dict) else None
    if isinstance(domains, list) and domains:
        return str(domains[0])
    return "unknown"


def _sample_articles_by_category(
    article_paths: dict[str, pathlib.Path],
    category: str,
    exclude_asin: str,
    *,
    same_category: bool,
    sample_size: int,
    seed: int,
) -> list[dict[str, Any]]:
    matched: list[dict[str, Any]] = []
    for asin, path in article_paths.items():
        if asin == exclude_asin:
            continue
        article = _load_json(path)
        if not isinstance(article, dict):
            continue
        is_same = article_category(article) == category
        if is_same != same_category:
            continue
        matched.append(article)

    rng = random.Random(seed)
    rng.shuffle(matched)
    return matched[:sample_size]


def sample_category_articles(
    article_paths: dict[str, pathlib.Path], category: str, exclude_asin: str,
    *, sample_size: int = DEFAULT_POOL_SAMPLE_SIZE, seed: int = DEFAULT_SEED,
) -> list[dict[str, Any]]:
    """同じカテゴリの既存記事から最大 ``sample_size`` 件をサンプリングする。"""
    return _sample_articles_by_category(
        article_paths, category, exclude_asin, same_category=True, sample_size=sample_size, seed=seed,
    )


def sample_other_category_articles(
    article_paths: dict[str, pathlib.Path], category: str, exclude_asin: str,
    *, sample_size: int = DEFAULT_POOL_SAMPLE_SIZE, seed: int = DEFAULT_SEED,
) -> list[dict[str, Any]]:
    """``category`` 以外のカテゴリの既存記事から最大 ``sample_size`` 件をサンプリングする
    (固有性判定の負の対照「別カテゴリの文」のプール用)。
    """
    return _sample_articles_by_category(
        article_paths, category, exclude_asin, same_category=False, sample_size=sample_size, seed=seed,
    )


# --------------------------------------------------------------------------
# 対象記事の選定 (その週に data/articles/ へコミットがあったもの、固定 seed で --limit 件)
# --------------------------------------------------------------------------

def select_recent_article_paths(repo_dir: pathlib.Path, since: str, until: str) -> set[str]:
    """``data/articles/`` 配下で、``since``〜``until`` の間に追加・更新コミットがあった
    ファイルの相対パス集合を返す (git 履歴が要る。浅い clone では検出できない)。

    git 呼び出しに失敗した場合は空集合を返し、呼び出し元に委ねる (このレーンを
    import エラー等と混同させない、freshness 監視とは別の失敗経路として扱う)。
    """
    try:
        proc = subprocess.run(
            ["git", "log", f"--since={since}", f"--until={until}",
             "--name-only", "--diff-filter=AM", "--pretty=format:",
             "--", "data/articles/"],
            cwd=repo_dir, capture_output=True, text=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError) as e:
        logger.warning("git log failed (%s) — treating as empty selection", e)
        return set()

    paths: set[str] = set()
    for line in proc.stdout.splitlines():
        line = line.strip().replace("\\", "/")
        if not line or not line.startswith("data/articles/") or not line.endswith(".json"):
            continue
        if any(line.endswith(sfx) for sfx in _SIDECAR_SUFFIXES):
            continue
        paths.add(line)
    return paths


def select_target_asins(
    articles_dir: pathlib.Path,
    repo_dir: pathlib.Path,
    *,
    since: str,
    until: str,
    limit: int,
    seed: int = DEFAULT_SEED,
) -> tuple[list[str], dict[str, pathlib.Path]]:
    """今週の対象 ASIN を選ぶ。戻り値は (選ばれた asin のリスト, discover_articles の結果)。

    「その週にコミットされた記事」を discover_articles (rewrite 新旧併存時は stem
    最新を採用) と突き合わせ、固定 seed でシャッフルしてから ``limit`` 件を切る。
    """
    article_paths = discover_articles(articles_dir)
    touched = select_recent_article_paths(repo_dir, since, until)

    def _relative_to_repo(path: pathlib.Path) -> str:
        # discover_articles は呼び出し元が渡した articles_dir (絶対/相対どちらもあり得る)
        # をそのまま glob するため、git log の相対パスと突き合わせるには
        # repo_dir 基準の相対パスへ正規化する必要がある。
        try:
            return str(path.resolve().relative_to(repo_dir.resolve())).replace("\\", "/")
        except ValueError:
            return str(path).replace("\\", "/")

    candidates = sorted(
        asin for asin, path in article_paths.items()
        if _relative_to_repo(path) in touched
    )
    rng = random.Random(seed)
    rng.shuffle(candidates)
    if limit and limit > 0:
        candidates = candidates[:limit]
    return candidates, article_paths


# --------------------------------------------------------------------------
# 結果キャッシュ (K8 named volume 想定、リポジトリには置かない)
# --------------------------------------------------------------------------

CACHE_FORMAT = 1


def cache_key(model_id: str, narrative: dict[str, Any], material_text: str) -> str:
    """narrative のハッシュ + 素材テキストのハッシュ + モデル ID をキーにする
    (#4841 実装依頼 S3)。narrative の JSON 化は sort_keys でキー順の揺れを吸収する。
    """
    narrative_hash = hashlib.sha256(
        json.dumps(narrative, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    material_hash = hashlib.sha256(material_text.encode("utf-8")).hexdigest()
    return hashlib.sha256(f"{model_id}\x00{narrative_hash}\x00{material_hash}".encode("utf-8")).hexdigest()


class ResultCache:
    """narrative+素材+モデル をキーに、計測結果の要約をキャッシュする。

    埋め込みキャッシュ (compute_semantic_related.EmbeddingCache, #6602 N3a) と
    同じ設計判断: 壊れても落とさない・原子的置換で保存する。**未参照キーは捨てない**
    (embed cache と違い対象は週40件程度で肥大の心配が無く、同じ週の再 dispatch
    以外にも「後日の目視確認で同じ記事を見直す」用途がありうるため)。
    """

    def __init__(self, path: pathlib.Path | None, model_id: str) -> None:
        self.path = path
        self.model_id = model_id
        self.entries: dict[str, dict[str, Any]] = {}
        self.hits = 0
        self.misses = 0

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def load(self) -> None:
        if not self.enabled:
            return
        assert self.path is not None
        if not self.path.exists():
            logger.info("結果キャッシュ %s は未作成 — コールドで開始します", self.path)
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("format") != CACHE_FORMAT:
                logger.warning("結果キャッシュの format が違う — 捨てて作り直します")
                return
            entries = data.get("entries")
            self.entries = entries if isinstance(entries, dict) else {}
            logger.info("結果キャッシュを読み込み: %d 件 (%s)", len(self.entries), self.path)
        except (OSError, ValueError) as e:
            logger.warning("結果キャッシュの読み込みに失敗 (%s) — コールドで続行します", e)
            self.entries = {}

    def get(self, key: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        hit = self.entries.get(key)
        if hit is None:
            self.misses += 1
            return None
        self.hits += 1
        return hit

    def put(self, key: str, result: dict[str, Any]) -> None:
        if not self.enabled:
            return
        self.entries[key] = result

    def save(self) -> None:
        if not self.enabled:
            return
        assert self.path is not None
        payload = {
            "format": CACHE_FORMAT,
            "model": self.model_id,
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "entries": self.entries,
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
            logger.info("結果キャッシュを保存: %d 件 -> %s", len(self.entries), self.path)
        except OSError as e:
            logger.warning("結果キャッシュの保存に失敗 (%s) — 続行します", e)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def stats(self) -> dict[str, int]:
        total = self.hits + self.misses
        return {
            "hits": self.hits, "misses": self.misses, "total": total,
            "hit_rate_pct": round(100.0 * self.hits / total) if total else 0,
        }


# --------------------------------------------------------------------------
# 週ラベル
# --------------------------------------------------------------------------

def iso_week_label(dt: datetime) -> str:
    year, week, _ = dt.isocalendar()
    return f"{year}-W{week:02d}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# 実行本体
# --------------------------------------------------------------------------

def _summarize_run(
    processed: list[dict[str, Any]], failed: list[dict[str, Any]], target_count: int,
) -> dict[str, Any]:
    counts = [p["unique_and_supported_count"] for p in processed]
    rates = [
        p["unique_and_supported_count"] / p["sentence_count"]
        for p in processed if p["sentence_count"] > 0
    ]

    def _group_stats(items: list[dict[str, Any]]) -> dict[str, Any]:
        c = [p["unique_and_supported_count"] for p in items]
        r = [p["unique_and_supported_count"] / p["sentence_count"] for p in items if p["sentence_count"] > 0]
        return {
            "count": len(items),
            "median_unique_and_supported_count": statistics.median(c) if c else None,
            "median_unique_and_supported_rate": round(statistics.median(r), 4) if r else None,
        }

    with_material = [p for p in processed if p.get("has_experience_material") is True]
    without_material = [p for p in processed if p.get("has_experience_material") is False]
    unknown_material = [p for p in processed if p.get("has_experience_material") is None]

    classification_totals = {RHETORICAL_OR_TIME_DEPENDENT: 0, FACTUAL_CLAIM: 0, "unresolved": 0}
    for p in processed:
        for k, v in (p.get("unsupported_classification") or {}).items():
            classification_totals[k] = classification_totals.get(k, 0) + v

    elapsed = [p["elapsed_seconds"] for p in processed if isinstance(p.get("elapsed_seconds"), (int, float))]

    return {
        "target_count": target_count,
        "processed_count": len(processed),
        "failed_count": len(failed),
        "failure_ratio": round(len(failed) / target_count, 4) if target_count else 0.0,
        "median_unique_and_supported_count": statistics.median(counts) if counts else None,
        "median_unique_and_supported_rate": round(statistics.median(rates), 4) if rates else None,
        "by_experience_material": {
            "with_material": _group_stats(with_material),
            "without_material": _group_stats(without_material),
            "unknown": _group_stats(unknown_material),
        },
        "unsupported_classification_totals": classification_totals,
        "median_elapsed_seconds": round(statistics.median(elapsed), 2) if elapsed else None,
        "total_elapsed_seconds": round(sum(elapsed), 2) if elapsed else None,
    }


def process_article(
    asin: str,
    article_path: pathlib.Path,
    article_paths: dict[str, pathlib.Path],
    *,
    articles_dir: pathlib.Path,
    raw_dir: pathlib.Path,
    ollama_url: str,
    ruri_url: str,
    model: str,
    num_ctx: int,
    seed: int,
    cache: ResultCache,
    session: requests.Session,
) -> dict[str, Any]:
    """1 記事ぶんを計測する。呼び出し元が例外を捕まえて失敗として数える。"""
    start = time.monotonic()
    article = json.loads(article_path.read_text(encoding="utf-8"))
    narrative = article.get("narrative") if isinstance(article.get("narrative"), dict) else {}
    category = article_category(article)

    raw_material = load_raw_material(asin, raw_dir)
    material_text = build_material_text(raw_material)
    has_material = has_experience_material_at_generation(raw_material, article)

    key = cache_key(model, narrative, material_text)
    cached = cache.get(key)
    if cached is not None:
        result = dict(cached)
        result["asin"] = asin
        result["cache_hit"] = True
        result["elapsed_seconds"] = round(time.monotonic() - start, 3)
        result["has_experience_material"] = has_material
        return result

    same_pool_articles = sample_category_articles(article_paths, category, asin, seed=seed)
    cross_pool_articles = sample_other_category_articles(article_paths, category, asin, seed=seed)
    same_pool = build_sentence_pool(same_pool_articles)
    cross_pool = build_sentence_pool(cross_pool_articles)

    gain = compute_information_gain(
        narrative, material_text, same_pool, cross_pool,
        ollama_url=ollama_url, ruri_url=ruri_url, model=model, num_ctx=num_ctx, session=session,
    )

    unsupported_sentences = [row["sentence"] for row in gain["per_sentence"] if row["supported"] is False]
    classify = classify_unsupported_sentences(
        unsupported_sentences, ollama_url=ollama_url, model=model, num_ctx=num_ctx, session=session,
    )
    classification_summary = summarize_categories(classify["categories"])

    result = {
        "asin": asin,
        "category": category,
        "sentence_count": gain["sentence_count"],
        "unique_and_supported_count": gain["unique_and_supported_count"],
        "unsupported_count": gain["unsupported_count"],
        "unresolved_count": gain["unresolved_count"],
        "unsupported_classification": classification_summary,
        "cache_hit": False,
    }
    cache.put(key, {k: v for k, v in result.items() if k != "cache_hit"})

    result["has_experience_material"] = has_material
    result["elapsed_seconds"] = round(time.monotonic() - start, 3)
    return result


def run(
    *,
    articles_dir: pathlib.Path,
    raw_dir: pathlib.Path,
    repo_dir: pathlib.Path,
    out_path: pathlib.Path,
    cache_path: pathlib.Path | None,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    ruri_url: str = DEFAULT_RURI_URL,
    model: str = DEFAULT_MODEL,
    num_ctx: int = DEFAULT_NUM_CTX,
    seed: int = DEFAULT_SEED,
    limit: int = DEFAULT_LIMIT,
    since: str | None = None,
    until: str | None = None,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """全体を実行し、書き込んだ payload を返す (テスト・main 双方から呼べるように分離)。"""
    now = datetime.now(timezone.utc)
    week_label = iso_week_label(now)
    since = since or (now - timedelta(days=7)).strftime("%Y-%m-%d")
    until = until or now.strftime("%Y-%m-%dT%H:%M:%S")

    session = session or requests.Session()
    cache = ResultCache(cache_path, model)
    cache.load()

    target_asins, article_paths = select_target_asins(
        articles_dir, repo_dir, since=since, until=until, limit=limit, seed=seed,
    )

    processed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    for asin in target_asins:
        path = article_paths.get(asin)
        if path is None or not path.exists():
            failed.append({"asin": asin, "reason": "article_path_missing"})
            continue
        try:
            result = process_article(
                asin, path, article_paths,
                articles_dir=articles_dir, raw_dir=raw_dir,
                ollama_url=ollama_url, ruri_url=ruri_url, model=model, num_ctx=num_ctx, seed=seed,
                cache=cache, session=session,
            )
        except (TruncationError, GemmaCallError, ValueError, requests.RequestException, json.JSONDecodeError) as e:
            logger.warning("skip %s: %s: %s", asin, type(e).__name__, e)
            failed.append({"asin": asin, "reason": f"{type(e).__name__}: {e}"})
            continue
        processed.append(result)

    cache.save()

    summary = _summarize_run(processed, failed, len(target_asins))
    run_ok = summary["failure_ratio"] <= MAX_FAILURE_RATIO if target_asins else True

    payload = {
        "generated_at": _now_iso(),
        "source_week": week_label,
        "model": model,
        "num_ctx": num_ctx,
        "seed": seed,
        "limit": limit,
        "since": since,
        "until": until,
        "target_asins": target_asins,
        "articles": processed,
        "failed": failed,
        "summary": summary,
        "run_ok": run_ok,
        "cache": cache.stats(),
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(
        "wrote %s: %d processed / %d failed (target=%d, run_ok=%s)",
        out_path, len(processed), len(failed), len(target_asins), run_ok,
    )
    return payload


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--articles-dir", default=DEFAULT_ARTICLES_DIR)
    ap.add_argument("--raw-dir", default=DEFAULT_RAW_DIR)
    ap.add_argument("--repo-dir", default=".", help="git log 実行の基準ディレクトリ")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--cache", default=os.environ.get("INFORMATION_GAIN_CACHE"),
                     help="結果キャッシュの永続領域 (K8 の named volume 等)。未指定ならキャッシュ無効")
    ap.add_argument("--ollama-url", default=os.environ.get("OLLAMA_URL", DEFAULT_OLLAMA_URL))
    ap.add_argument("--ruri-url", default=os.environ.get("RURI_URL", DEFAULT_RURI_URL))
    ap.add_argument("--model", default=os.environ.get("JUDGE_MODEL", DEFAULT_MODEL))
    ap.add_argument("--num-ctx", type=int, default=int(os.environ.get("OLLAMA_NUM_CTX", DEFAULT_NUM_CTX)),
                     help="ollama num_ctx (未設定時は超過分を無言で切り詰める罠がある)")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="対象記事数の上限 (0=無制限)")
    ap.add_argument("--since", default=None, help="対象コミットの下限 (既定: 7日前)")
    ap.add_argument("--until", default=None, help="対象コミットの上限 (既定: 現在時刻)")
    args = ap.parse_args()

    payload = run(
        articles_dir=pathlib.Path(args.articles_dir),
        raw_dir=pathlib.Path(args.raw_dir),
        repo_dir=pathlib.Path(args.repo_dir),
        out_path=pathlib.Path(args.out),
        cache_path=pathlib.Path(args.cache) if args.cache else None,
        ollama_url=args.ollama_url,
        ruri_url=args.ruri_url,
        model=args.model,
        num_ctx=args.num_ctx,
        seed=args.seed,
        limit=args.limit,
        since=args.since,
        until=args.until,
    )
    if not payload.get("run_ok", True):
        logger.error(
            "failure ratio %.1f%% exceeds %.0f%% — failing the run (#4841 S3 堅牢性要件)",
            payload["summary"]["failure_ratio"] * 100, MAX_FAILURE_RATIO * 100,
        )
        return 1
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
