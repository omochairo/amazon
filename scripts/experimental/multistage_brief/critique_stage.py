"""群 C の後段: ⑤ 凡庸な段落の批評 → ⑥ 指摘された段落だけ書き直す。"""
from __future__ import annotations

from typing import Any

from scripts.experimental.multistage_brief.narrative_stage import STYLE_GUIDE
from scripts.experimental.multistage_brief.ollama_client import (
    DEFAULT_MODEL,
    DEFAULT_NUM_CTX,
    DEFAULT_OLLAMA_URL,
    call_gemma,
    parse_json_response,
)

CRITIQUE_PROMPT_TEMPLATE = """あなたは知育玩具比較サイト「おもちゃいろ」の校閲担当です。以下の narrative の各キーについて、
「既存の他記事との最大類似度」(1.0に近いほど、他の記事と似た凡庸な言い回しである可能性が高い) を
参考情報として渡します。類似度が高く、かつ実際に読んで具体性が乏しい・定型句的だと判断したキーだけを
指摘してください。全部を指摘する必要はありません。

# narrative (キー: 本文 [既存コーパスとの最大類似度])
{narrative_with_scores}

次の JSON だけを出力してください (他の説明文は一切含めない。指摘が無ければ空配列):
{{"flagged": [{{"key": "キー名", "reason": "指摘理由 (1文)"}}, ...]}}
"""

REWRITE_PROMPT_TEMPLATE = """あなたは知育玩具比較サイト「おもちゃいろ」の書き手です。

{style_guide}

# 商品情報・素材
{material_text}

# 校閲担当からの指摘
{flagged_text}

# 元の文章 (指摘されたキーのみ)
{original_text}

指摘された理由を踏まえて、指摘されたキーの文章だけを書き直してください。他のキーは対象外です。
次の JSON だけを出力してください (他の説明文は一切含めない):
{{{schema_lines}}}
"""


def build_critique_input(narrative: dict[str, str], key_scores: dict[str, float]) -> str:
    lines = []
    for key, text in narrative.items():
        score = key_scores.get(key)
        score_str = f"{score:.4f}" if isinstance(score, (int, float)) else "N/A"
        lines.append(f"## {key} [max_sim_vs_corpus={score_str}]\n{text}")
    return "\n\n".join(lines)


def critique_paragraphs(
    narrative: dict[str, str],
    key_scores: dict[str, float],
    *,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    model: str = DEFAULT_MODEL,
    num_ctx: int = DEFAULT_NUM_CTX,
    session=None,
) -> dict[str, Any]:
    prompt = CRITIQUE_PROMPT_TEMPLATE.format(
        narrative_with_scores=build_critique_input(narrative, key_scores),
    )
    call = call_gemma(
        prompt, ollama_url=ollama_url, model=model, num_ctx=num_ctx, temperature=0.3, format_json=True,
        session=session,
    )
    parsed = parse_json_response(call["text"])
    flagged = parsed.get("flagged") if isinstance(parsed, dict) else None
    flagged = flagged if isinstance(flagged, list) else []
    flagged = [
        {"key": str(f.get("key")), "reason": str(f.get("reason", ""))}
        for f in flagged if isinstance(f, dict) and f.get("key") in narrative
    ]
    return {"flagged": flagged, "call_meta": {k: v for k, v in call.items() if k != "text"}}


def rewrite_flagged(
    material_text: str,
    narrative: dict[str, str],
    flagged: list[dict[str, str]],
    *,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    model: str = DEFAULT_MODEL,
    num_ctx: int = DEFAULT_NUM_CTX,
    session=None,
) -> dict[str, Any]:
    """flagged にあるキーだけを書き直す。それ以外は narrative の値を一字も変えず引き継ぐ。

    flagged が空なら gemma を呼ばず、narrative をそのまま返す (C=B と等しくなる)。
    """
    if not flagged:
        return {"narrative": dict(narrative), "rewritten_keys": [], "call_meta": None}

    flagged_keys = [f["key"] for f in flagged]
    flagged_text = "\n".join(f"- {f['key']}: {f['reason']}" for f in flagged)
    original_text = "\n\n".join(f"## {k}\n{narrative[k]}" for k in flagged_keys if k in narrative)
    schema_lines = ", ".join(f'"{k}": "書き直した本文"' for k in flagged_keys)

    prompt = REWRITE_PROMPT_TEMPLATE.format(
        style_guide=STYLE_GUIDE,
        material_text=material_text,
        flagged_text=flagged_text,
        original_text=original_text,
        schema_lines=schema_lines,
    )
    call = call_gemma(
        prompt, ollama_url=ollama_url, model=model, num_ctx=num_ctx, temperature=0.6, format_json=True,
        session=session,
    )
    parsed = parse_json_response(call["text"])
    parsed = parsed if isinstance(parsed, dict) else {}

    new_narrative = dict(narrative)
    rewritten_keys = []
    for key in flagged_keys:
        val = parsed.get(key)
        if isinstance(val, str) and val.strip():
            new_narrative[key] = val.strip()
            rewritten_keys.append(key)

    return {
        "narrative": new_narrative,
        "rewritten_keys": rewritten_keys,
        "call_meta": {k: v for k, v in call.items() if k != "text"},
    }
