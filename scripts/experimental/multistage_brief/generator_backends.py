"""#4841 P1 (model-ceiling probe): 群 A の「生成器」だけを差し替え可能にする層。

問い (2026-09-23 issue コメント): 素材・プロンプトを固定したまま生成モデルだけを
大きくすると、情報利得の指標は動くか。判定器 (sentence_metrics の裏付け判定・
unsupported_classification・fact_cards の抽出) は引き続き gemma のまま変えない —
ここで差し替えるのは narrative_stage.generate_narrative_baseline が担っている
「narrative を書く」呼び出しだけ。

- ``ollama``: 従来どおり narrative_stage.generate_narrative_baseline (gemma)。
- ``agy``: K8 ホストの agy (Antigravity CLI) をヘッドレス実行し、同じプロンプト
  (BASELINE_PROMPT_TEMPLATE) をクラウド上位モデルに渡す。呼び方は
  scripts/mine_experience.py::gather_antigravity と同じ
  ``dbus-run-session -- agy --print=...`` (omochairo/amazon#6539: --model は
  --print より前に置く)。

agy の Web 検索を止める手段: ``agy --help`` を確認したが、tool 単位で無効化する
フラグは無い (--sandbox は「terminal 操作」の制限であり web 検索ツールとは無関係、
--dangerously-skip-permissions は逆に全 tool を自動承認する方向)。プロンプト側は
group A (gemma) と一字一句同じ BASELINE_PROMPT_TEMPLATE を使う (素材だけを根拠に
書けという指示はあるが「検索するな」の指示はない — 生成器比較の対象プロンプトを
歪めないため、gemma 側にも無い指示をここだけに足さない)。そのため検索の混入は
「止める」のではなく「数えて結果に記録する」方針にする:
  - 出力 (narrative の各キー) に URL がいくつ混ざったか (mine_experience.extract_urls
    と同じ正規表現)
  - agy の --output-format json が返す ``num_turns`` (1 = ツール呼び出しの往復無しで
    直接応答。2 以上は tool call が挟まった=検索等を使った可能性の代理指標)
"""
from __future__ import annotations

import hashlib
import logging
import re
import subprocess
import time
from typing import Any, Callable

from scripts.experimental.multistage_brief import narrative_stage
from scripts.experimental.multistage_brief.ollama_client import DEFAULT_SEED, parse_json_response

logger = logging.getLogger("multistage_brief.generator_backends")

# `agy models` (2026-09-23 実測) の一覧で Pro 系のうち最上位。issue の指示
# 「Pro系の上位モデル」に対応する明示ピン。CLI 側の「既定モデル」に乗せない
# (ollama_client.call_gemma と同じ理由: バージョン更新で黙って変わると
# 比較にならない)。
DEFAULT_AGY_MODEL = "gemini-3.1-pro-high"
AGY_TIMEOUT_S = 300
AGY_MAX_EXTRA_RETRIES = 1  # 呼び出し回数の予算 (40回) を圧迫しないため gather_antigravity より控えめ
AGY_RETRY_SLEEP_S = 2.0

# mine_experience.py の _URL_RE と同じパターン (agy の応答から出典 URL / 一般 URL を拾う)。
_URL_RE = re.compile(r"https?://[^\s<>\"'）\]\[|、。]+")


class AgyGenerationError(Exception):
    """agy 呼び出しが (リトライ後も) 失敗した、または narrative を抽出できなかった。"""


def build_agy_prompt(material_text: str) -> str:
    """group A (gemma) と一字一句同じプロンプトを組む (生成器だけの差にするため)。"""
    return narrative_stage.BASELINE_PROMPT_TEMPLATE.format(
        style_guide=narrative_stage.STYLE_GUIDE, material_text=material_text,
        schema=narrative_stage.NARRATIVE_OUTPUT_SCHEMA,
    )


def build_agy_argv(prompt: str, model: str) -> list[str]:
    """agy の argv を組む (mine_experience.build_antigravity_argv と同じ形)。

    ``agy --print <prompt> --model X`` だと --model 以降が prompt に食われる
    (omochairo/amazon#6539 で実測)。--model を先に置き、prompt は --print= に
    付ける。--output-format json で num_turns 等のメタデータも受け取る。
    """
    argv = ["agy"]
    if model:
        argv += ["--model", model]
    argv += ["--output-format", "json", f"--print={prompt}"]
    return argv


def count_urls(narrative: dict[str, str]) -> int:
    """narrative の各キーに混ざった URL の総数 (検索混入の代理指標)。"""
    total = 0
    for v in narrative.values():
        if isinstance(v, str):
            total += len(_URL_RE.findall(v))
    return total


def call_agy(
    prompt: str,
    *,
    model: str = DEFAULT_AGY_MODEL,
    timeout_s: int = AGY_TIMEOUT_S,
    sleeper: Callable[[float], None] = time.sleep,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """agy をヘッドレス実行し、応答テキストと呼び出しメタデータを返す。

    mine_experience.gather_antigravity と同じく dbus-run-session 経由 (本番 K8) /
    直接実行 (Windows フォールバック) の順に試す。空応答だけリトライする
    (gather_antigravity の実測どおり、agy が exit 0 のまま最終テキストを返さない
    ことがあるため)。呼び出し予算 (issue 制約: 最大40回) を圧迫しないよう、
    追加リトライは1回のみ (gather_antigravity の2回より少ない)。
    """
    argv = build_agy_argv(prompt, model)
    cmds = [["dbus-run-session", "--", *argv], argv]

    attempts = AGY_MAX_EXTRA_RETRIES + 1
    last_stderr = ""
    attempts_used = 0
    for attempt in range(1, attempts + 1):
        attempts_used = attempt
        t0 = time.time()
        result = None
        for cmd in cmds:
            try:
                result = runner(cmd, capture_output=True, text=True, timeout=timeout_s, encoding="utf-8")
                break
            except FileNotFoundError:
                continue
        elapsed = time.time() - t0

        if result is None:
            raise AgyGenerationError("agy (Antigravity CLI) not found in PATH")
        if result.returncode != 0:
            last_stderr = (result.stderr or "")[:300]
            raise AgyGenerationError(f"agy exited {result.returncode}: {last_stderr}")

        stdout = (result.stdout or "").strip()
        if stdout:
            parsed_envelope = parse_json_response(stdout)
            if not isinstance(parsed_envelope, dict):
                raise AgyGenerationError(f"agy --output-format json did not return an object: {stdout[:200]}")
            response_text = parsed_envelope.get("response")
            if not isinstance(response_text, str) or not response_text.strip():
                raise AgyGenerationError(f"agy response missing 'response' text: {stdout[:200]}")
            return {
                "text": response_text,
                "model": model,
                "status": parsed_envelope.get("status"),
                "num_turns": parsed_envelope.get("num_turns"),
                "usage": parsed_envelope.get("usage"),
                "duration_seconds": parsed_envelope.get("duration_seconds"),
                "elapsed_s": round(elapsed, 3),
                "attempts_used": attempts_used,
            }

        last_stderr = (result.stderr or "").strip()[:300]
        if attempt < attempts:
            logger.warning("agy 空応答 (attempt %d/%d, model=%s) — リトライ", attempt, attempts, model)
            sleeper(AGY_RETRY_SLEEP_S)

    raise AgyGenerationError(f"agy empty response after {attempts_used} attempt(s): {last_stderr}")


def generate_narrative_agy(
    material_text: str,
    *,
    model: str = DEFAULT_AGY_MODEL,
    timeout_s: int = AGY_TIMEOUT_S,
    seed: int | None = None,  # 未使用 (agy はseed制御が無い)。ollama系と呼び出し面を揃えるためだけの引数
    session=None,  # 未使用。narrative_stage.generate_narrative_baseline と同じキーワードを受けられるようにするためだけの引数
    sleeper: Callable[[float], None] = time.sleep,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """群 A の生成器を agy (クラウド上位モデル) に差し替えたバージョン。

    戻り値の形は narrative_stage.generate_narrative_baseline と揃える
    ({"narrative": ..., "call_meta": ...}) — 呼び出し側 (run_p1_generator_ceiling.py)
    が生成器を切り替えても後段の判定コード (sentence_metrics 等、gemma固定) を
    変えずに済むようにするため。
    """
    prompt = build_agy_prompt(material_text)
    call = call_agy(prompt, model=model, timeout_s=timeout_s, sleeper=sleeper, runner=runner)
    parsed = parse_json_response(call["text"])
    narrative = narrative_stage._extract_narrative(parsed)  # noqa: SLF001 — 同一パッケージ内の抽出ロジックを再利用
    call_meta = {
        "generator": "agy",
        "model_id": model,
        "status": call.get("status"),
        "num_turns": call.get("num_turns"),
        "usage": call.get("usage"),
        "duration_seconds": call.get("duration_seconds"),
        "elapsed_s": call.get("elapsed_s"),
        "attempts_used": call.get("attempts_used"),
        "seed": seed,
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "url_count_in_narrative": count_urls(narrative),
    }
    return {"narrative": narrative, "call_meta": call_meta}


def generate_narrative_ollama(
    material_text: str,
    *,
    ollama_url: str,
    model: str,
    num_ctx: int,
    seed: int = DEFAULT_SEED,
    session=None,
) -> dict[str, Any]:
    """群 A の従来の生成器 (gemma/Ollama)。narrative_stage.generate_narrative_baseline そのもの。"""
    return narrative_stage.generate_narrative_baseline(
        material_text, ollama_url=ollama_url, model=model, num_ctx=num_ctx, seed=seed, session=session,
    )


GENERATORS: dict[str, Callable[..., dict[str, Any]]] = {
    "ollama": generate_narrative_ollama,
    "agy": generate_narrative_agy,
}
