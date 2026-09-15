"""#4841 M1-b: 情報利得の指標 (「その商品にしか無い、素材で裏付けられた事実がいくつ入ったか」)。

T3 の反省 (「narrative 全体を1回で判定」は分解能が無かった) を踏まえ、判定の単位を
文にする。手順:
  1. narrative を文に分割する (LLM を使わず句点ベースで決定的に、split_sentences)
  2. 各文が ASIN の素材から導けるかを gemma で判定する (根拠判定、文単位で結果を持つ。
     呼び出しはバッチにまとめてよい)
  3. 裏付けありの各文について、同じカテゴリの既存記事の文との Ruri 最大類似度を取り、
     別カテゴリの文の分布 (負の対照) の p95 未満なら「固有」とする
  4. 指標: 固有かつ裏付けありの文の数 (主) / 裏付けの無い文の数 (ガードレール)
"""
from __future__ import annotations

import re
from typing import Any

import requests

from scripts.audit_experience_usage import build_paragraph_map, cosine_similarity, percentile
from scripts.compute_semantic_related import embed_batch_ruri
from scripts.experimental.multistage_brief.narrative_stage import NARRATIVE_KEYS
from scripts.experimental.multistage_brief.ollama_client import (
    DEFAULT_MODEL,
    DEFAULT_NUM_CTX,
    DEFAULT_OLLAMA_URL,
    call_gemma,
    parse_json_response,
)

THRESHOLD_PERCENTILE = 95.0

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？])")


def split_sentences(text: str) -> list[str]:
    """句点 (。！？) ベースで決定的に文へ分割する (LLM を使わない)。

    分割記号自体は残す (「文の意味の単位」を壊さないため)。空白のみの断片・
    空文字列は含めない。改行は文区切りとしては扱わない (narrative の各キーは
    地の文なので、句点の無い改行は文の途中とみなす)。
    """
    if not isinstance(text, str) or not text.strip():
        return []
    parts = _SENTENCE_SPLIT_RE.split(text)
    return [p.strip() for p in parts if p and p.strip()]


def flatten_narrative_sentences(
    narrative: dict[str, Any], keys: tuple[str, ...] = NARRATIVE_KEYS,
) -> list[dict[str, Any]]:
    """narrative の各キーを文に分割し、{key, sentence} のフラットなリストにする。

    本番記事の narrativeSection は string (旧形式) と array (現行形式、要素は
    既に文単位のことが多い) の両方があり得る (audit_experience_usage.
    build_paragraph_map と同じ理由)。ここでも同じ正規化 (array は空白区切りで
    1つの文字列に結合) を経由してから句点で再分割することで、array 形式の
    キーが丸ごと読み飛ばされることを防ぐ (#4841 M1 実データ検証で発覚: array
    形式のキーを黙ってスキップすると、新しい記事ほど文がほぼ0件になる)。
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


def build_sentence_pool(articles: list[dict[str, Any]], max_sentences: int = 300) -> list[str]:
    """記事一覧の narrative を文に分割し、プールにする (#4841 M1-b: 固有性判定のコーパス)。

    Ruri 埋め込みの呼び出し回数を抑えるため ``max_sentences`` で打ち切る
    (順序はそのまま、記事の並び順に依存するが呼び出し元がサンプリング済み)。
    """
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
    (「未判定」も有効な結果として扱う。黙って supported/unsupported どちらかに
    倒さない)。
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
    """文単位の根拠判定を1回の gemma 呼び出しでまとめて行う (#4841 M1-b)。"""
    if not sentences:
        return {"supported_flags": [], "unresolved_indices": [], "call_meta": None}
    prompt = build_entailment_prompt(material_text, sentences)
    call = call_gemma(
        prompt, ollama_url=ollama_url, model=model, num_ctx=num_ctx, temperature=0.0, format_json=True,
        session=session,
    )
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
    """文ごとの固有性を判定する (#4841 M1-b③)。

    each sentence の「同じカテゴリの既存記事の文」との最大類似度 (same_category_max_sim)
    を出す。閾値は「別カテゴリの文」に対する同じ sentences の最大類似度分布
    (負の対照: 別カテゴリの文とたまたま似てしまう度合いの目安) の p95 とする。
    same_category_max_sim が閾値未満なら固有 (unique=True)。

    プールが空 (コーパスが無い) 場合は判定不能として unique=None を返す
    (固有=Falseに倒さない。判定できないことを明示する)。
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
    """narrative 1件ぶんの情報利得指標をまとめて計算する (#4841 M1-b④)。

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
