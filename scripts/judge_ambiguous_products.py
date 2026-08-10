#!/usr/bin/env python3
"""judge_ambiguous_products.py

Issue #2686 案2「ambiguous 語のローカル LLM 判定レーン」の計算スクリプト。

なぜ必要か:
  scripts/probe_ubersuggest_products.py の実査 (workflow 50) を 200 語で
  2 回実行した結果、以下が確定値として残った (最新 run 31392220870):
    product 126 語 / non_product 18 語 / ambiguous 56 語 (Volume 2,081,300)
  non_product 18 語は精度が高い (施設「uniqlo park」・鉄道模型「tomix」・
  キャラ名「ふらわっち」等が正しく落ちている)。だが ambiguous 56 語は
  機械判定 (compute_title_overlap の形態素解析ベース集合一致) では
  これ以上分離できない。商品クエリと情報クエリが混在している:
    商品側: 「リカちゃん人形」「ピカチュー ぬいぐるみ」
             「パウパトロール おもちゃ」「プリキュア おもちゃ」
             「ポケモングッズ」
    非商品側: 「知育 村」「たまごっち 種類」「トイストーリー キャラ」
              「かっこいい ポケモン」「すいちゃん みい つけた」
  「リカちゃん人形」が product にならないのは、実タイトルが「ドール」表記
  で「人形」という語自体が存在しないため = 同義語問題であり、表層一致の
  compute_title_overlap では原理的に解けない。ここが LLM の担当領域。

  owner 方針: セッションコストを抑えたいので、LLM で落とせるなら機械側
  (probe_ubersuggest_products.judge_verdict) で無理に減らさなくてよい。
  まず LLM の性能を見る。

プロンプト改訂 (2026-08-10、run 31394640877 の実測に基づく):
  初版プロンプトで 56 語を判定させ、事前に確定させた正解ラベルと突き合わせた
  結果は **正答率 43/56 = 76.8% / precision 72.0% / recall 75.0%**
  (LLM 側エラー 0)。同義語問題は解けており「リカちゃん人形」は confidence
  1.0 で product、「たまごっち 種類」「知育 村」も正しく落ちた。
  誤りは 13 件で、機構は 2 つだけだった:

  (1) 誤通過 7 件 = 循環論法。LLM の理由がほぼ全て「Amazon で関連商品が
      複数ヒットするため」だった (「すいちゃん みい つけた」「スティッチ
      可愛い」「トランスフォーマー 新着」「新製品プラレール」等)。だが
      **この判定に回ってくる語は定義上すべて商品が返っている**ので、
      「商品がヒットする」は全語共通の定数であって証拠にならない。
      初版はタイトルを渡すだけで、それを根拠にしてよいかを書いていな
      かった。→ 判断材料にしてはいけない旨を明示した。
  (2) 取りこぼし 6 件のうち 3 件 = 「キャラクター名単体」ルールの過剰適用。
      「ポケモングッズ」(conf 1.0)「トイストーリー ウッディー」「dレックス」
      を「キャラクター名単体」として落としていたが、いずれも商品種別を
      伴うか実在商品名である。→ 「商品種別を伴わない」場合に限定し、
      逆に商品種別を伴う語・実在商品名は product とみなす旨を足した。

  残り 4 件 (「2歳の誕生日プレゼント」×2・「たまごっちパラダイス定価」・
  「デュエマ 新弾」) は正解ラベル側に議論の余地がある境界例で、LLM の
  理由付けも筋が通っていた。これらを正解扱いすると 47/56 = 84%。

  **confidence は使えない**: 56 語中 55 語が 0.9〜1.0 に張り付き、その帯の
  正答率が 76%。confidence と正誤が相関していないので「低 confidence だけ
  人手確認」という運用は成立しない。閾値による選別を入れないこと
  (しきい値が分布の中央付近にあるゲートは何も選別していない)。

処理の流れ:
  1. data/analytics/ubersuggest_product_probe.json の results から
     verdict == "ambiguous" の語だけを対象にする (product/non_product は
     対象外)。
  2. 語ごとに raw_query と実査で既に保存済みの sample_titles (最大10件、
     #4899 で全件保存に変更済み) を K8 のローカル LLM (gemma) に渡し、
     「Amazon の商品ページ1本で受けられる商品を探すクエリか、それとも
     商品では受けられない情報を探すクエリか」を判定させる。
     **Amazon API を再度叩かない** (タイトルは実査で既に保存済み)。
  3. 判定結果を data/analytics/ubersuggest_llm_judge.json に書き出す。

既存資産の再利用 (再実装しない):
  scripts/audit_query_entailment.py が同じ経路 (Ollama `/api/generate`、
  think: False、format: "json"、keep_alive、options.temperature 0、
  HTTP/JSON 失敗時のリトライ、「1件の失敗で全体を止めない」設計) を
  実装済み。呼び出し方・リトライ・エラー畳み込みの定数
  (REQUEST_TIMEOUT / _MAX_EXTRA_RETRIES / _RETRY_SLEEP_SECONDS /
  JudgeError) をそのまま import して使う。

制約 (probe_ubersuggest_products.py / audit_query_entailment.py の流儀を踏襲):
  - LLM の失敗は語単位で error に畳んで次へ進む (1語の失敗で全体を止めない)
  - **data/raw/amazon.json を書かない** = 記事は1本も生成されない
    (観測専用、workflow 50 と同じ規律)
  - --dry-run で LLM を呼ばず対象語の確認だけできる
  - --limit で件数を絞れる (既定 0 = 全 ambiguous)

使い方:
    python scripts/judge_ambiguous_products.py --dry-run
    python scripts/judge_ambiguous_products.py --limit 20
    python scripts/judge_ambiguous_products.py   # secrets 不要 (K8 ローカル LLM)

Issue: https://github.com/omochairo/amazon/issues/2686 (案2)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import time
from datetime import datetime, timezone
from typing import Any

import requests

from scripts.audit_query_entailment import (
    JudgeError,
    REQUEST_TIMEOUT,
    _MAX_EXTRA_RETRIES,
    _RETRY_SLEEP_SECONDS,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("judge_ambiguous_products")

DEFAULT_PROBE_PATH = "data/analytics/ubersuggest_product_probe.json"
DEFAULT_OUT = "data/analytics/ubersuggest_llm_judge.json"
DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_MODEL = "gemma4:26b-a4b-it-qat"
DEFAULT_LIMIT = 0
MAX_SAMPLE_TITLES_IN_PROMPT = 10

# プロンプト設計の規律 (プロジェクト既存の教訓): 短い絶対規則で書く。
# 失敗例の文字列を大量に埋め込んで長くしない。判断に使ってよい材料
# (クエリと商品タイトル) を明示する。「リカちゃん人形」のような同義語
# ケース (タイトルが「ドール」表記) を正しく商品と判定できるよう、
# 表記が違っても同じ物を指すなら商品とみなす旨を1行入れる (#2686 案2)。
JUDGE_PROMPT_TEMPLATE = """あなたは検索クエリが Amazon の商品ページ1本で受けられる「商品を探すクエリ」か、
商品ページでは受けられない「情報を探すクエリ」かを判定するアシスタントです。
判定対象のサイトは知育玩具メディアで、記事の型は常に Amazon 商品ページ1本です。

# 検索クエリ
{query}

# Amazon 実査で見つかった商品タイトル (該当する商品が無ければ空)
{titles}

判定ルール:
- 商品タイトルが見つかること自体は判断材料にしてはいけない。この判定に
  回ってくる語は全て何らかの商品が返っており、全語で共通なので証拠に
  ならない。判断は**クエリの語そのものが商品を指しているか**で行う。
  タイトルは、クエリの語が実在の商品名や商品種別かを確かめるためだけに
  使う。
- クエリと表記が違っても同じ物を指すなら商品を探すクエリとみなす
  (例: クエリが「人形」でもタイトルが「ドール」表記なら商品とみなす)。
- 商品種別 (ぬいぐるみ・フィギュア・グッズ・おもちゃ 等) を伴う語や、
  実在する商品名・シリーズ名は商品を探すクエリとみなす。
- 施設名・作品名、商品種別を伴わないキャラクター名だけの語、
  「種類」「一覧」「キャラ」「進化」のような商品を指さない末尾語、
  「新作」「新製品」「新着」「新弾」のような一覧を求める語、
  「かわいい」「かっこいい」のような鑑賞を求める形容詞は
  情報を探すクエリとみなす。

次の JSON スキーマだけを出力してください (他の説明文は一切含めない):
{{"is_product_query": true または false, "confidence": 0.0から1.0の数値, "reason": "1文の日本語の理由"}}
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_ambiguous_targets(probe_path: pathlib.Path, limit: int) -> list[dict[str, Any]]:
    """probe JSON から verdict == "ambiguous" の語だけを取り出す (product/
    non_product は対象外)。sample_titles は最大 MAX_SAMPLE_TITLES_IN_PROMPT
    件に切り詰めてプロンプトに渡す (実査で保存されているのは既に最大10件
    なので通常は無変化)。"""
    payload = json.loads(probe_path.read_text(encoding="utf-8"))
    results = payload.get("results") or []
    targets = [r for r in results if r.get("verdict") == "ambiguous"]
    targets = sorted(targets, key=lambda r: -(r.get("volume") or 0))
    if limit and limit > 0:
        targets = targets[:limit]
    return targets


def build_prompt(query: str, sample_titles: list[str]) -> str:
    titles = sample_titles[:MAX_SAMPLE_TITLES_IN_PROMPT]
    titles_block = "\n".join(f"- {t}" for t in titles) if titles else "(該当商品なし)"
    return JUDGE_PROMPT_TEMPLATE.format(query=query, titles=titles_block)


def judge_ambiguous_query(
    query: str,
    sample_titles: list[str],
    ollama_url: str,
    model: str,
    session: requests.Session,
    sleeper=time.sleep,
) -> dict[str, Any]:
    """1 語分を gemma judge (`/api/generate`) に問い合わせる。

    audit_query_entailment.judge_query と同じリトライ・エラー畳み込みの
    流儀 (最大 _MAX_EXTRA_RETRIES+1 回試行、失敗しても例外を送出せず
    error フィールド付き dict を返す)。
    """
    url = f"{ollama_url.rstrip('/')}/api/generate"
    prompt = build_prompt(query, sample_titles)
    body = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "think": False,
        "format": "json",
        "keep_alive": "30m",
        "options": {"temperature": 0},
    }

    last_err: Exception | None = None
    attempts = _MAX_EXTRA_RETRIES + 1
    for attempt in range(1, attempts + 1):
        try:
            resp = session.post(url, json=body, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            payload = resp.json()
            raw = payload.get("response") if isinstance(payload, dict) else None
            if not isinstance(raw, str) or not raw.strip():
                raise JudgeError("empty /api/generate response")
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise JudgeError("judge response is not a JSON object")
            confidence = parsed.get("confidence")
            return {
                "is_product_query": bool(parsed.get("is_product_query")),
                "confidence": float(confidence) if isinstance(confidence, (int, float)) else None,
                "reason": str(parsed.get("reason")) if parsed.get("reason") is not None else "",
                "error": None,
            }
        except (requests.RequestException, JudgeError, ValueError, json.JSONDecodeError) as e:
            last_err = e
            if attempt < attempts:
                logger.warning("judge call failed (attempt %d/%d) for query=%r: %s", attempt, attempts, query, e)
                sleeper(_RETRY_SLEEP_SECONDS)
            else:
                logger.error("judge call failed after %d attempt(s) for query=%r: %s", attempts, query, e)

    return {
        "is_product_query": None,
        "confidence": None,
        "reason": "",
        "error": str(last_err),
    }


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    judged = [r for r in results if r["error"] is None]
    is_product = [r for r in judged if r["is_product_query"] is True]
    is_not_product = [r for r in judged if r["is_product_query"] is False]
    errors = [r for r in results if r["error"]]
    return {
        "judged": len(judged),
        "is_product": len(is_product),
        "is_not_product": len(is_not_product),
        "error_count": len(errors),
    }


def run(
    probe_path: pathlib.Path,
    out_path: pathlib.Path,
    limit: int,
    dry_run: bool,
    *,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    model: str = DEFAULT_MODEL,
    session: requests.Session | None = None,
    sleeper=time.sleep,
) -> dict[str, Any]:
    targets = load_ambiguous_targets(probe_path, limit)
    params = {"limit": limit, "ollama_url": ollama_url, "model": model}

    if dry_run:
        logger.info("[dry-run] LLM を呼ばずに終了する。ambiguous 対象語 %d 件", len(targets))
        for t in targets[:20]:
            logger.info("  %-24s volume=%s reason=%s", t.get("query", "")[:24], t.get("volume"),
                        t.get("verdict_reason"))
        return {
            "generated_at": _now_iso(),
            "params": params,
            "summary": {"judged": 0},
            "results": [],
            "targets": [
                {"query": t.get("query"), "raw_query": t.get("raw_query"), "volume": t.get("volume"),
                 "verdict_reason": t.get("verdict_reason")}
                for t in targets
            ],
        }

    session = session or requests.Session()
    results: list[dict[str, Any]] = []
    for i, t in enumerate(targets):
        sample_titles = t.get("sample_titles") or []
        verdict = judge_ambiguous_query(t.get("raw_query") or t.get("query", ""), sample_titles,
                                        ollama_url, model, session, sleeper)
        results.append({
            "query": t.get("query"),
            "raw_query": t.get("raw_query"),
            "volume": t.get("volume"),
            "sites": t.get("sites") or [],
            "probe_verdict": t.get("verdict"),
            "probe_reason": t.get("verdict_reason"),
            "sample_titles": sample_titles,
            **verdict,
        })
        logger.info("  [%3d/%3d] %-24s is_product=%s confidence=%s %s",
                    i + 1, len(targets), (t.get("query") or "")[:24],
                    verdict["is_product_query"], verdict["confidence"], verdict["error"] or "")
        # K8 ローカル LLM (同一マシン内) のため probe_ubersuggest_products.py
        # と異なり呼び出し間隔のスリープは不要 (レート制限の対象は外部 API)

    summary = summarize(results)
    payload = {"generated_at": _now_iso(), "params": params, "summary": summary, "results": results}

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    logger.info("wrote %s: judged=%d is_product=%d is_not_product=%d error=%d",
                out_path, summary["judged"], summary["is_product"], summary["is_not_product"],
                summary["error_count"])
    return payload


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--probe", default=DEFAULT_PROBE_PATH, help="probe_ubersuggest_products.py の出力 JSON")
    ap.add_argument("--out", default=DEFAULT_OUT, help="出力先 JSON パス")
    ap.add_argument("--ollama-url", default=os.environ.get("OLLAMA_URL", DEFAULT_OLLAMA_URL))
    ap.add_argument("--model", default=os.environ.get("JUDGE_MODEL", DEFAULT_MODEL))
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="判定する ambiguous 語数の上限 (0=全件)")
    ap.add_argument("--dry-run", action="store_true", help="LLM を一切呼ばず対象語の確認だけ")
    args = ap.parse_args()

    run(
        probe_path=pathlib.Path(args.probe),
        out_path=pathlib.Path(args.out),
        limit=args.limit,
        dry_run=args.dry_run,
        ollama_url=args.ollama_url,
        model=args.model,
    )
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
