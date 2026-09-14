"""T3 のガードレール2種 (#4841 T3 評価表): ASIN の封じ込め / 根拠の無い記述。

どちらも「凡庸度が改善していても、これが悪化した群は無効」という事前判定に使う。
"""
from __future__ import annotations

import re
from typing import Any

from scripts.experimental.multistage_brief.ollama_client import (
    DEFAULT_MODEL,
    DEFAULT_NUM_CTX,
    DEFAULT_OLLAMA_URL,
    call_gemma,
    parse_json_response,
)

_ASIN_TOKEN_RE = re.compile(r"\b[A-Z0-9]{10}\b")

NARRATIVE_KEYS = (
    "lead", "why_this_product", "gift_appeal", "daily_use", "safety_note", "closing", "how_to_choose",
)


def check_asin_containment(
    how_to_choose_text: str, allowed_asins: set[str], own_asin: str, foreign_product_names: list[str] | None = None,
) -> dict[str, Any]:
    """how_to_choose に許可外の ASIN / 商品名が出ていないかを調べる。

    ASIN トークン (10桁の英数字) は正規表現で厳密に検出できる。商品名の混入は
    厳密な NER が無いので、``foreign_product_names`` (無関係な ASIN の商品名の
    サンプル) を文字列一致でチェックするヒューリスティックに留める
    (見つからない=安全ではなく、この方法で検出できる範囲、という限定付き)。
    """
    text = how_to_choose_text or ""
    found_asins = set(_ASIN_TOKEN_RE.findall(text))
    allowed = allowed_asins | {own_asin}
    disallowed_asins = sorted(found_asins - allowed)

    disallowed_names = []
    for name in foreign_product_names or []:
        name = name.strip()
        if name and name in text:
            disallowed_names.append(name)

    return {
        "ok": not disallowed_asins and not disallowed_names,
        "disallowed_asins": disallowed_asins,
        "disallowed_product_names": disallowed_names,
    }


ENTAILMENT_PROMPT_TEMPLATE = """あなたは記事の事実確認担当です。以下の「素材」に書かれている内容だけを根拠として、
「生成された文章」の各キーの文が、素材から導けるかどうかを判定してください。

# 素材
{material_text}

# 生成された文章 (キーごと)
{narrative_text}

各キーについて、素材から導けない文 (根拠の無い記述) があれば、その文をそのまま抜き出してください。
無ければ空配列にしてください。次の JSON だけを出力してください (他の説明文は一切含めない):
{{"findings": [{{"key": "キー名", "unsupported_sentences": ["文1", "文2"]}}, ...]}}
"""


def build_narrative_text(narrative: dict[str, str]) -> str:
    lines = []
    for key in NARRATIVE_KEYS:
        val = narrative.get(key)
        if val:
            lines.append(f"## {key}\n{val}")
    return "\n\n".join(lines)


def run_entailment_check(
    material_text: str,
    narrative: dict[str, str],
    *,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    model: str = DEFAULT_MODEL,
    num_ctx: int = DEFAULT_NUM_CTX,
    session=None,
) -> dict[str, Any]:
    """narrative の全キーを1回の gemma 呼び出しでまとめて entailment 判定する。

    audit_query_entailment.py と同じ「judge が JSON で answered/coverage を返す」
    方式を、対象を「検索クエリへの回答性」から「素材からの導出可能性」に変えて
    流用する。1 キーずつ呼ぶと呼び出し回数が narrative キー数 x 3群 x 10 ASIN で
    数百回に膨らむため、1 narrative = 1 呼び出しにまとめている。
    """
    prompt = ENTAILMENT_PROMPT_TEMPLATE.format(
        material_text=material_text, narrative_text=build_narrative_text(narrative),
    )
    call = call_gemma(
        prompt, ollama_url=ollama_url, model=model, num_ctx=num_ctx, temperature=0.0, format_json=True,
        session=session,
    )
    parsed = parse_json_response(call["text"])
    findings = parsed.get("findings") if isinstance(parsed, dict) else None
    findings = findings if isinstance(findings, list) else []

    total_unsupported = 0
    by_key: dict[str, list[str]] = {}
    for f in findings:
        if not isinstance(f, dict):
            continue
        key = f.get("key")
        sentences = f.get("unsupported_sentences")
        sentences = [str(s) for s in sentences] if isinstance(sentences, list) else []
        if key:
            by_key[str(key)] = sentences
            total_unsupported += len(sentences)

    return {
        "total_unsupported": total_unsupported,
        "by_key": by_key,
        "call_meta": {k: v for k, v in call.items() if k != "text"},
    }
