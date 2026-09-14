"""群 B の前段: ① angle 候補生成 → ② Ruri で選択 → ③ 設計メモ。"""
from __future__ import annotations

from typing import Any

from scripts.audit_experience_usage import cosine_similarity
from scripts.experimental.multistage_brief.corpus import embed_texts_document
from scripts.experimental.multistage_brief.ollama_client import (
    DEFAULT_MODEL,
    DEFAULT_NUM_CTX,
    DEFAULT_OLLAMA_URL,
    call_gemma,
    parse_json_response,
)

ANGLE_COUNT = 5

ANGLE_PROMPT_TEMPLATE = """あなたは知育玩具比較サイト「おもちゃいろ」の企画担当です。以下の商品素材から、
記事の切り口 (angle) を{count}件考えてください。

# 商品素材
{material_text}

各候補には根拠 (素材中のどの記述・snippet・商品データのフィールドに基づくか) を明記してください。
根拠が無い候補 (素材に無い想像) は出さないでください。次の JSON だけを出力してください
(他の説明文は一切含めない。"candidates" の配列にちょうど{count}件入れること):
{{"candidates": [{{"angle": "切り口の一文説明", "evidence": "根拠となる素材の該当箇所"}}, ...]}}
"""

DESIGN_MEMO_PROMPT_TEMPLATE = """あなたは知育玩具比較サイト「おもちゃいろ」の企画担当です。以下の商品素材と、
採用が決まった記事の切り口 (angle) をもとに、執筆者向けの設計メモを作ってください。

# 商品素材
{material_text}

# 採用した切り口
angle: {angle}
根拠: {evidence}

# narrative の各キー
lead / why_this_product / gift_appeal / daily_use / safety_note / closing / how_to_choose

次の JSON だけを出力してください (他の説明文は一切含めない):
{{
  "angle": "{angle}",
  "key_snippet_map": {{"lead": "このキーで使う素材の要約", "why_this_product": "...", "gift_appeal": "...", "daily_use": "...", "safety_note": "...", "closing": "...", "how_to_choose": "..."}},
  "how_to_choose_axis": "比較軸の説明。比較対象は次の競合ASINのみに限定すること: {allowed_asins}"
}}
"""


def _coerce_candidate_list(parsed: Any) -> list[Any]:
    """gemma は ``format: json`` でも配列ではなく1個のオブジェクト、または

    ``{"angles": [...]}`` のようにラップしたオブジェクトを返すことがある
    (実測、#4841 T3)。トップレベルが配列ならそのまま使い、dict ならその中の
    最初のリスト値を候補として使い、単一の候補オブジェクトならそれ1件を
    候補として扱う (呼び出しが完全に無駄になるよりは1件でも拾う)。
    """
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        for v in parsed.values():
            if isinstance(v, list):
                return v
        if parsed.get("angle"):
            return [parsed]
    return []


def generate_angle_candidates(
    material_text: str,
    *,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    model: str = DEFAULT_MODEL,
    num_ctx: int = DEFAULT_NUM_CTX,
    session=None,
) -> dict[str, Any]:
    prompt = ANGLE_PROMPT_TEMPLATE.format(material_text=material_text, count=ANGLE_COUNT)
    call = call_gemma(
        prompt, ollama_url=ollama_url, model=model, num_ctx=num_ctx, temperature=0.7, format_json=True,
        session=session,
    )
    parsed = parse_json_response(call["text"])
    candidates = _coerce_candidate_list(parsed)
    candidates = [
        {"angle": str(c.get("angle", "")), "evidence": str(c.get("evidence", ""))}
        for c in candidates if isinstance(c, dict) and c.get("angle") and c.get("evidence")
    ]
    return {"candidates": candidates, "call_meta": {k: v for k, v in call.items() if k != "text"}}


def select_angle(
    candidates: list[dict[str, str]],
    corpus_vectors: list[list[float]],
    *,
    ruri_url: str,
    session=None,
) -> dict[str, Any]:
    """候補の angle 文を embed し、既存コーパスとの最大類似度が一番低いものを選ぶ。

    コーパスが空 (同カテゴリの既存記事が無い) 場合は最大類似度を 0.0 として扱い、
    最初の候補を選ぶ (選択不能で落とさない。その旨は呼び出し元がログに残す)。
    """
    if not candidates:
        return {"selected": None, "scored": []}

    angle_texts = [c["angle"] for c in candidates]
    angle_vectors = embed_texts_document(angle_texts, ruri_url=ruri_url, session=session)

    scored: list[dict[str, Any]] = []
    for c, vec in zip(candidates, angle_vectors):
        if corpus_vectors:
            max_sim = max(cosine_similarity(vec, cv) for cv in corpus_vectors)
        else:
            max_sim = 0.0
        scored.append({**c, "max_sim_vs_corpus": round(max_sim, 4)})

    scored.sort(key=lambda x: x["max_sim_vs_corpus"])
    return {"selected": scored[0], "scored": scored}


def build_design_memo(
    material_text: str,
    selected_angle: dict[str, str],
    allowed_asins: set[str],
    *,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    model: str = DEFAULT_MODEL,
    num_ctx: int = DEFAULT_NUM_CTX,
    session=None,
) -> dict[str, Any]:
    prompt = DESIGN_MEMO_PROMPT_TEMPLATE.format(
        material_text=material_text,
        angle=selected_angle.get("angle", ""),
        evidence=selected_angle.get("evidence", ""),
        allowed_asins=", ".join(sorted(allowed_asins)) or "(なし)",
    )
    call = call_gemma(
        prompt, ollama_url=ollama_url, model=model, num_ctx=num_ctx, temperature=0.4, format_json=True,
        session=session,
    )
    parsed = parse_json_response(call["text"])
    memo = parsed if isinstance(parsed, dict) else {}
    return {"memo": memo, "call_meta": {k: v for k, v in call.items() if k != "text"}}
