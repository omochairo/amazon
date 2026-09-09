"""probe_llm_citations.py

LLM 検索エンジンおよびモデルによる検索クエリ回答内のドメイン引用 (Citation) を
プローブし、時系列データとして蓄積する CLI スクリプト。

## 目的

Google Search Console (GSC) の検索クエリ履歴から上位クエリを抽出し、
LLM エンジン (Antigravity CLI / Perplexity 等) に対してプロンプトを送信。
得られた回答および引用 URL から自社ドメイン (navi.omcha.jp / omcha.jp) への
言及・引用状況を検知・記録する。

## 設計判断

1. レート制御と実行規律 (Rate discipline - 必須要件):
   - 完全逐次実行 (Strictly sequential, never concurrent):
     LLM API や CLI に対する過剰な負荷やレートリミットを避けるため、
     非同期・並列実行は行わず、必ず 1 クエリずつ逐次的に実行する。
   - 呼び出し間隔の sleep (time.sleep):
     エンジン呼び出しの間には既定で 5.0 秒の待機時間 (`--sleep`) を挟む。
     単体テスト等で待機を不要化できるよう、`sleeper=time.sleep` として
     外部から注入可能な設計とする。
   - 自動リトライの禁止 (NO automatic retry):
     エンジン呼び出しが失敗 (タイムアウト、APIエラー、異常終了等) した場合、
     WARNING ログを記録して該当クエリをスキップする。同一実行内で同じクエリを
     再試行することはしない (リトライ嵐による障害拡大の防止)。

2. GSC プロパティの系列分離 (Site separation):
   `navi` (navi.omcha.jp) と `omcha` (omcha.jp) は独立した GSC プロパティであり、
   クエリの抽出元および引用判定の対象ホストは完全に分離して処理する。

3. 冪等性とサイドカーファイル (Idempotency & Sidecar):
   同一日付 (`date`)、対象サイト (`site`)、エンジン (`engine`) の組み合わせについて、
   多重実行による重複レコード蓄積を防ぐため、実行完了状態をサイドカー
   `<history-dir>/llm_citations_seen.json` に記録する。
   すでに sidecar に記録されている組み合わせは、`--force` が指定されない限り
   INFO ログを出力してスキップする。
   また、JSONL (`llm_citations.jsonl`) への追記が正常に完了した後にのみ
   サイドカーを更新することでアトミック性を担保する。

4. エンジン層の抽象化 (Multi-engine architecture):
   - `agy`: ローカルの Antigravity CLI ヘッドレス実行。`--model` を `--print=` より
     前に配置することで引数パースの不具合を回避。
   - `perplexity`: Perplexity API (Sonar 等) への HTTPS POST 通信。
   - `fixture`: 外部通信・サブプロセスなしの決定論的モックエンジン。`--dry-run` および
     テストで使用。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import logging
import os
import pathlib
import subprocess
import sys
import time
from typing import Any, Callable
import urllib.request

# 親ディレクトリおよびスクリプト配置ディレクトリを sys.path に追加して
# scripts._llm_citation および _llm_citation の双方から安全にインポート可能にする
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

try:
    from scripts._llm_citation import SITES, build_record, select_queries
except ImportError:
    from _llm_citation import SITES, build_record, select_queries

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("probe_llm_citations")


class EngineError(RuntimeError):
    """LLM エンジンの呼び出しまたは応答解析失敗を表す例外。"""


def build_prompt(query: str) -> str:
    """検索クエリに対する消費者の疑問に答え、参照元URLの提示を求めるプロンプトを構築する。"""
    return (
        f"ユーザーが「{query}」について調べています。"
        "一般の消費者に分かりやすく回答し、参照したWebサイトのURLを末尾に箇条書きで示してください。"
    )


def build_agy_argv(prompt: str, model: str | None) -> list[str]:
    """Antigravity CLI (agy) のコマンドライン引数を構築する。

    --model は必ず --print の前に配置し、プロンプトは --print=<prompt> の形式で渡す。
    そうしないと agy が後続の引数をプロンプトの一部として消費してしまう。
    model が falsy (None や空文字) の場合は --model を省略する。
    """
    argv = ["agy"]
    if model:
        argv.extend(["--model", model])
    argv.append(f"--print={prompt}")
    return argv


def engine_agy(query: str, *, timeout: int) -> dict[str, Any]:
    """ローカルの Antigravity CLI (agy) をサブプロセスとして実行する。"""
    env_model = os.environ.get("ANTIGRAVITY_MODEL") or None
    prompt = build_prompt(query)
    argv = build_agy_argv(prompt, env_model)
    resolved_model = env_model if env_model else "agy-default"

    try:
        res = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            check=False,
        )
    except Exception as exc:
        raise EngineError(f"agy process execution error: {exc}") from exc

    if res.returncode != 0:
        raise EngineError(f"agy process exited with code {res.returncode}: {res.stderr}")

    stdout = res.stdout if res.stdout is not None else ""
    if not stdout.strip():
        raise EngineError(f"agy process returned empty stdout: {res.stderr}")

    return {
        "answer_text": stdout,
        "citation_urls": [],
        "model": resolved_model,
    }


def engine_perplexity(query: str, *, timeout: int) -> dict[str, Any]:
    """Perplexity API に POST リクエストを送信し、回答テキストと引用 URL を取得する。"""
    api_key = os.environ.get("PERPLEXITY_API_KEY")
    if not api_key:
        raise EngineError("PERPLEXITY_API_KEY environment variable is unset")

    model = os.environ.get("PERPLEXITY_MODEL") or "sonar"
    prompt = build_prompt(query)
    url = "https://api.perplexity.ai/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")

    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp_bytes = resp.read()
            body_text = resp_bytes.decode("utf-8")
    except Exception as exc:
        raise EngineError(f"Perplexity API request failed: {exc}") from exc

    try:
        data = json.loads(body_text)
        if not isinstance(data, dict):
            raise EngineError(f"Perplexity response is not a dict: {type(data)}")
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise EngineError(f"Perplexity response missing valid choices: {data}")
        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            raise EngineError(f"Perplexity choice is not a dict: {first_choice}")
        message = first_choice.get("message")
        if not isinstance(message, dict):
            raise EngineError(f"Perplexity message is not a dict: {message}")
        content = message.get("content")
        if not isinstance(content, str):
            raise EngineError(f"Perplexity message content is not a string: {content}")
        citations = data.get("citations")
        citation_urls = [str(u) for u in citations] if isinstance(citations, list) else []
    except EngineError:
        raise
    except Exception as exc:
        raise EngineError(f"Failed to parse Perplexity response: {exc}") from exc

    return {
        "answer_text": content,
        "citation_urls": citation_urls,
        "model": model,
    }


def engine_fixture(query: str, *, timeout: int = 120) -> dict[str, Any]:
    """テストおよび dry-run 用の決定論的モックエンジン。"""
    if "navi" in query:
        text = "知育玩具ナビ (https://navi.omcha.jp/example/) の解説を参照してください。"
    elif "omcha" in query:
        text = "おもちゃレビュー (https://omcha.jp/example/) の記事を参照してください。"
    else:
        text = "他社サイトのレビュー情報を参考にしてください。"

    return {
        "answer_text": text,
        "citation_urls": [],
        "model": "fixture",
    }


ENGINES: dict[str, Callable[..., dict[str, Any]]] = {
    "agy": engine_agy,
    "perplexity": engine_perplexity,
    "fixture": engine_fixture,
}


def load_gsc_rows(path: pathlib.Path | str) -> list[dict[str, Any]]:
    """GSC の JSONL ファイルを読み込み、空行やパース失敗行をスキップして行辞書のリストを返す。"""
    p = pathlib.Path(path)
    if not p.is_file():
        logger.warning("GSC history file not found: %s", p)
        return []

    rows: list[dict[str, Any]] = []
    with p.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line_str = line.strip()
            if not line_str:
                continue
            try:
                data = json.loads(line_str)
                if isinstance(data, dict):
                    rows.append(data)
                else:
                    logger.warning("Line %d in %s is not a JSON object", lineno, p)
            except json.JSONDecodeError as exc:
                logger.warning("Line %d in %s failed to parse: %s", lineno, p, exc)
    return rows


def seen_key(date: str, site: str, engine: str) -> str:
    """サイドカー管理用の識別キー文字列を返す。"""
    return f"{date}|{site}|{engine}"


def _load_seen(path: pathlib.Path) -> dict[str, Any]:
    if not path.is_file():
        return {"seen": {}}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("seen"), dict):
                return data
    except Exception as exc:
        logger.warning("Failed to load seen sidecar %s: %s", path, exc)
    return {"seen": {}}


def _save_seen(path: pathlib.Path, seen_data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(".tmp")
    with temp_path.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(seen_data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    temp_path.replace(path)


def main(argv: list[str] | None = None, *, sleeper: Callable[[float], None] = time.sleep) -> int:
    parser = argparse.ArgumentParser(description="Probe LLM engines and append citation time series.")
    parser.add_argument("--site", choices=["navi", "omcha", "all"], default="all", help="Target site (default: all)")
    parser.add_argument("--engine", action="append", default=None, help="Engine to probe (default: ['agy'])")
    parser.add_argument("--top-n", type=int, default=10, help="Top N queries per site (default: 10)")
    parser.add_argument("--days", type=int, default=28, help="Distinct GSC days window (default: 28)")
    parser.add_argument("--min-impressions", type=int, default=1, help="Min cumulative impressions filter (default: 1)")
    parser.add_argument("--date", default=None, help="Target date in YYYY-MM-DD (default: today UTC)")
    parser.add_argument("--sleep", type=float, default=5.0, help="Sleep seconds between engine calls (default: 5.0)")
    parser.add_argument("--root", default=".", help="Repository root directory (default: '.')")
    parser.add_argument("--history-dir", default="data/analytics/history", help="History directory (default: 'data/analytics/history')")
    parser.add_argument("--dry-run", action="store_true", help="Force fixture engine and make no external calls")
    parser.add_argument("--force", action="store_true", help="Re-probe even if already recorded in seen sidecar")
    parser.add_argument("--limit", type=int, default=None, help="Cap the number of queries actually probed")
    parser.add_argument("--timeout", type=int, default=120, help="Timeout in seconds per engine call (default: 120)")

    args = parser.parse_args(argv)

    target_date = args.date if args.date else datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if args.dry_run:
        engines = ["fixture"]
        logger.info("Dry-run mode: forcing 'fixture' engine, no external calls will be made.")
    else:
        engines = args.engine if args.engine is not None else ["agy"]

    for eng in engines:
        if eng not in ENGINES:
            logger.error("Unknown engine %r. Registered: %s", eng, list(ENGINES.keys()))
            return 1

    sites = ["navi", "omcha"] if args.site == "all" else [args.site]

    root_path = pathlib.Path(args.root)
    history_dir = pathlib.Path(args.history_dir)
    if not history_dir.is_absolute():
        history_dir = root_path / history_dir
    history_dir.mkdir(parents=True, exist_ok=True)

    citations_file = history_dir / "llm_citations.jsonl"
    seen_file = history_dir / "llm_citations_seen.json"

    seen_data = _load_seen(seen_file)
    seen_dict = seen_data.setdefault("seen", {})

    combinations = [(site, engine) for site in sites for engine in engines]
    all_combinations_already_seen = all(
        seen_key(target_date, s, e) in seen_dict and not args.force
        for s, e in combinations
    )

    total_records_written = 0
    is_first_call = True

    for site in sites:
        gsc_rel = SITES[site]["gsc_history"]
        gsc_path = root_path / gsc_rel
        rows = load_gsc_rows(gsc_path)
        selected = select_queries(
            rows,
            days=args.days,
            top_n=args.top_n,
            min_impressions=args.min_impressions,
        )
        if args.limit is not None and args.limit > 0:
            selected = selected[:args.limit]

        for engine in engines:
            key = seen_key(target_date, site, engine)
            if key in seen_dict and not args.force:
                logger.info("Skipping %s: already recorded in sidecar (use --force to override)", key)
                continue

            engine_fn = ENGINES[engine]
            records: list[dict[str, Any]] = []
            errors = 0
            probed_count = 0
            cited_count = 0

            for q_item in selected:
                query_str = q_item["query"]
                if not is_first_call and args.sleep > 0:
                    sleeper(args.sleep)
                is_first_call = False

                t0 = time.perf_counter()
                try:
                    res = engine_fn(query_str, timeout=args.timeout)
                except EngineError as exc:
                    logger.warning("Engine %s failed for query %r: %s", engine, query_str, exc)
                    errors += 1
                    continue
                except Exception as exc:
                    logger.warning("Unexpected error from engine %s for query %r: %s", engine, query_str, exc)
                    errors += 1
                    continue

                latency_ms = round((time.perf_counter() - t0) * 1000)
                probed_count += 1

                rec = build_record(
                    date=target_date,
                    site=site,
                    engine=engine,
                    model=res["model"],
                    query=query_str,
                    answer_text=res.get("answer_text"),
                    citation_urls=res.get("citation_urls"),
                    latency_ms=latency_ms,
                )
                if rec["cited"]:
                    cited_count += 1
                records.append(rec)

            if records:
                with citations_file.open("a", encoding="utf-8", newline="\n") as f:
                    for r in records:
                        f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
                total_records_written += len(records)

                seen_dict[key] = {
                    "queries": len(records),
                    "cited": cited_count,
                    "written_at": datetime.now(timezone.utc).isoformat(),
                }
                _save_seen(seen_file, seen_data)

            logger.info(
                "[%s | %s] queries probed: %d, cited count: %d, errors: %d",
                site,
                engine,
                probed_count,
                cited_count,
                errors,
            )

    if total_records_written > 0 or all_combinations_already_seen:
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
