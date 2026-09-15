"""narrative 生成: 群 A (対照、1パス) と 群 B (前段メモ付き)。"""
from __future__ import annotations

from typing import Any

from scripts.experimental.multistage_brief.ollama_client import (
    DEFAULT_MODEL,
    DEFAULT_NUM_CTX,
    DEFAULT_OLLAMA_URL,
    DEFAULT_SEED,
    call_gemma,
    parse_json_response,
)

STYLE_GUIDE = """# 文体ガイド (厳守)
- 主要読者層は20〜40代の女性。子ども・甥姪へのプレゼントや自分の子育てのために知育玩具を探している
- 「〜です。〜ます。」を基本にした落ち着いた女性誌調で書く
- 幼児口調 (だよ/なんだ/みてね/しらべたよ/ぼく/だね) は禁止
- 「編集部」「編集者」という表記は使わない
- 素材に無い受賞歴・レビュー文・エピソードを創作しない (架空エピソード厳禁)
- how_to_choose の比較対象は、素材に挙がっている競合商品のみに限定する"""

NARRATIVE_OUTPUT_SCHEMA = """{
  "lead": "導入 (2-3文)",
  "why_this_product": "この商品を薦める理由 (2-4文)",
  "gift_appeal": "贈り物としての魅力 (2-3文)",
  "daily_use": "日常での使い方 (2-3文)",
  "safety_note": "安全面の注意点 (1-2文)",
  "closing": "締めの一言 (1-2文)",
  "how_to_choose": "比較軸に基づいた選び方の解説 (3-5文)",
  "editorial_comment": "1文の短いコメント"
}"""

BASELINE_PROMPT_TEMPLATE = """あなたは知育玩具比較サイト「おもちゃいろ」の書き手です。

{style_guide}

# 商品情報・素材
{material_text}

# 出力
上記の素材だけを根拠に、以下のキーを持つ narrative を JSON で出力してください。
他の説明文は一切含めない。
{schema}
"""

WITH_MEMO_PROMPT_TEMPLATE = """あなたは知育玩具比較サイト「おもちゃいろ」の書き手です。

{style_guide}

# 商品情報・素材
{material_text}

# 採用する記事の切り口 (angle)
{angle}

# 設計メモ (各キーで使う素材・比較軸)
{memo_text}

# 出力
上記の切り口と設計メモに沿って、以下のキーを持つ narrative を JSON で出力してください。
メモに挙げられていない素材を新たに創作しないこと。他の説明文は一切含めない。
{schema}
"""

NARRATIVE_KEYS = (
    "lead", "why_this_product", "gift_appeal", "daily_use", "safety_note", "closing", "how_to_choose",
)


def _extract_narrative(parsed: Any) -> dict[str, str]:
    if not isinstance(parsed, dict):
        return {}
    out: dict[str, str] = {}
    for key in (*NARRATIVE_KEYS, "editorial_comment"):
        val = parsed.get(key)
        if isinstance(val, str) and val.strip():
            out[key] = val.strip()
    return out


def generate_narrative_baseline(
    material_text: str,
    *,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    model: str = DEFAULT_MODEL,
    num_ctx: int = DEFAULT_NUM_CTX,
    seed: int = DEFAULT_SEED,
    session=None,
) -> dict[str, Any]:
    """群 A: 素材から narrative を 1 パスで書く (対照)。

    seed (#4841 M1-a): ノイズの床を測るため、同じ ASIN・同じプロンプトで
    seed だけ変えて複数回呼べるようにする。既定は T3 と同じ DEFAULT_SEED。
    """
    prompt = BASELINE_PROMPT_TEMPLATE.format(
        style_guide=STYLE_GUIDE, material_text=material_text, schema=NARRATIVE_OUTPUT_SCHEMA,
    )
    call = call_gemma(
        prompt, ollama_url=ollama_url, model=model, num_ctx=num_ctx, temperature=0.6, seed=seed,
        format_json=True, session=session,
    )
    parsed = parse_json_response(call["text"])
    narrative = _extract_narrative(parsed)
    return {"narrative": narrative, "call_meta": {k: v for k, v in call.items() if k != "text"}}


FACT_CARD_PROMPT_TEMPLATE = """あなたは知育玩具比較サイト「おもちゃいろ」の書き手です。

{style_guide}

# 商品情報・素材
{material_text}

# 事実カード (この商品について確認済みの具体的な事実)
{fact_cards_text}

# 出力
上記の素材だけを根拠に、以下のキーを持つ narrative を JSON で出力してください。
他の説明文は一切含めない。
{schema}
"""


def generate_narrative_with_fact_cards(
    material_text: str,
    fact_cards_text: str,
    *,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    model: str = DEFAULT_MODEL,
    num_ctx: int = DEFAULT_NUM_CTX,
    seed: int = DEFAULT_SEED,
    session=None,
) -> dict[str, Any]:
    """群 D (#4841 M2): A と同じ素材 + 事実カードで narrative を書く。

    プロンプトの差は BASELINE_PROMPT_TEMPLATE に対して「事実カードの節が
    あるか」だけ (style_guide・素材節・出力指示・schema は一字一句同じ)。
    """
    prompt = FACT_CARD_PROMPT_TEMPLATE.format(
        style_guide=STYLE_GUIDE, material_text=material_text, fact_cards_text=fact_cards_text,
        schema=NARRATIVE_OUTPUT_SCHEMA,
    )
    call = call_gemma(
        prompt, ollama_url=ollama_url, model=model, num_ctx=num_ctx, temperature=0.6, seed=seed,
        format_json=True, session=session,
    )
    parsed = parse_json_response(call["text"])
    narrative = _extract_narrative(parsed)
    return {"narrative": narrative, "call_meta": {k: v for k, v in call.items() if k != "text"}}


def _format_memo(memo: dict[str, Any]) -> str:
    lines = []
    key_map = memo.get("key_snippet_map")
    if isinstance(key_map, dict):
        for k, v in key_map.items():
            lines.append(f"- {k}: {v}")
    axis = memo.get("how_to_choose_axis")
    if axis:
        lines.append(f"- how_to_choose_axis: {axis}")
    return "\n".join(lines) if lines else "(メモなし)"


def generate_narrative_with_memo(
    material_text: str,
    angle: str,
    memo: dict[str, Any],
    *,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    model: str = DEFAULT_MODEL,
    num_ctx: int = DEFAULT_NUM_CTX,
    session=None,
) -> dict[str, Any]:
    """群 B: 選んだ angle + 設計メモに沿って narrative を書く。"""
    prompt = WITH_MEMO_PROMPT_TEMPLATE.format(
        style_guide=STYLE_GUIDE,
        material_text=material_text,
        angle=angle,
        memo_text=_format_memo(memo),
        schema=NARRATIVE_OUTPUT_SCHEMA,
    )
    call = call_gemma(
        prompt, ollama_url=ollama_url, model=model, num_ctx=num_ctx, temperature=0.6, format_json=True,
        session=session,
    )
    parsed = parse_json_response(call["text"])
    narrative = _extract_narrative(parsed)
    return {"narrative": narrative, "call_meta": {k: v for k, v in call.items() if k != "text"}}
