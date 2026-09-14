"""gemma (Ollama) 呼び出しの共通ラッパー。

#4841 実装依頼「LLM 呼び出しの共通要件」を満たす:
  - num_ctx を必ず明示する
  - 切り詰めを検出して落とす (黙って続行しない)
  - temperature / seed を固定し、結果に記録する
  - モデル ID・プロンプトの sha256・所要秒を記録する
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from typing import Any

import requests

logger = logging.getLogger("multistage_brief.ollama_client")

DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_MODEL = "gemma4:26b-a4b-it-qat"
DEFAULT_NUM_CTX = 8192
DEFAULT_SEED = 20260914
DEFAULT_TEMPERATURE = 0.4
REQUEST_TIMEOUT = 300
_MAX_EXTRA_RETRIES = 2
_RETRY_SLEEP_SECONDS = 2.0

# ``prompt_eval_count`` がこの割合を割ったら切り詰め疑いとする (推定トークン数比)。
TRUNCATION_RATIO_THRESHOLD = 0.7
# num_ctx のこの割合を超えたら「上限に接近している」ことも切り詰め扱いにする。
NUM_CTX_HEADROOM_THRESHOLD = 0.9


class TruncationError(Exception):
    """ollama が num_ctx を超えて入力を無言で切り詰めた疑いがある。"""


class GemmaCallError(Exception):
    """gemma 呼び出しがリトライ上限まで失敗した。"""


def estimate_tokens(text: str) -> int:
    """日本語混じりテキストの雑なトークン数見積もり。

    実トークナイザは呼ばず、``prompt_eval_count`` との比較にのみ使う粗い目安
    (CJK は概ね1〜2文字/トークンなので、保守的に2文字/トークンとする)。
    """
    return max(1, len(text) // 2)


def call_gemma(
    prompt: str,
    *,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    model: str = DEFAULT_MODEL,
    num_ctx: int = DEFAULT_NUM_CTX,
    temperature: float = DEFAULT_TEMPERATURE,
    seed: int = DEFAULT_SEED,
    format_json: bool = True,
    session: requests.Session | None = None,
    sleeper=time.sleep,
    timeout: int = REQUEST_TIMEOUT,
) -> dict[str, Any]:
    """gemma に 1 回問い合わせ、テキストと呼び出しメタデータを返す。

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
                "eval_count": payload.get("eval_count"),
                "total_duration_s": round((payload.get("total_duration") or 0) / 1e9, 3),
                "num_ctx": num_ctx,
                "temperature": temperature,
                "seed": seed,
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
    """gemma の応答テキストから JSON を取り出す。

    ``format: json`` を指定していてもコードフェンスが混ざることがあるため、
    まず素の json.loads を試し、失敗したらフェンスを剥がして再試行する。
    それでも壊れていれば ``ValueError`` を送出する (壊れた JSON を握りつぶさない)。
    """
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
