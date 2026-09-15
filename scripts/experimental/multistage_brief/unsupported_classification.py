"""#4841 M2 追加要件 (「M1 完了」issue コメント):

M1-b の情報利得指標で「裏付けなし (unsupported)」と判定された narrative の文を、
「修辞・時点依存」(rhetorical_or_time_dependent) と「事実の主張」(factual_claim)
に分けた件数を報告する。M1-c の目視サンプルで、時点依存の価格言明や修辞的な
導入文が一律 unsupported 判定になっていたため (判定器が実害の無い文と事実誤認を
区別できない)、この分類は「どれだけ実害の可能性がある不支持か」を見るための
追加のレポート用分類であり、M2 の Go/No-Go 判定そのものは変えない
(判定は bootstrap.py の条件のまま)。
"""
from __future__ import annotations

from typing import Any

from scripts.experimental.multistage_brief.ollama_client import (
    DEFAULT_MODEL,
    DEFAULT_NUM_CTX,
    DEFAULT_OLLAMA_URL,
    call_gemma,
    parse_json_response,
)

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
    """gemma の classifications 配列を index -> category にマップする (pure)。

    未知のカテゴリ文字列・型不正・index 不整合は None (未解決) として扱う
    (parse_entailment_response と同じ方針: 黙ってどちらかに倒さない)。
    """
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
    call = call_gemma(
        prompt, ollama_url=ollama_url, model=model, num_ctx=num_ctx, temperature=0.0, format_json=True,
        session=session,
    )
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
