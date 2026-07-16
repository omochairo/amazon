"""generate_faq_seo.py

Issue #3332 N1 供給側「FAQ 実需要拡張 + meta_description 刷新」の生成スクリプト。

記事の FAQ を実需要クエリ (GSC 実クエリ・Google suggest・回答性監査の
missing_aspects) で拡張し、meta_description を実クエリを織り込んで刷新して
``data/articles/<slug>.seo.json`` sidecar に書く。消費側は既に実装済みで
(``scripts/build_post.py`` の ``_merge`` / ``SEO_KEYS``)、``faq_extended`` を
JSON-LD + 本文の FAQ セクションへ、``meta_description_optimized`` を meta タグへ
反映する。本スクリプトは sidecar を書くだけで build_post.py には触らない。

入力:
  - ``data/analytics/gsc_history/<年>-W<週>.json`` (最新週を自動選択。
    scripts.audit_query_entailment.find_latest_history_file と同じ規則)
  - ``data/analytics/answerability_audit.json`` (pages[].queries[].missing_aspects。
    不在でも動く)
  - ``data/raw/suggest/<ASIN>.json`` (実需要クエリ。不在でも動く)
  - ``data/articles/<stem>.json`` (記事本文。グラウンディング源)

対象選定 (優先度キュー):
  1. GSC で impressions があり CTR が低いページを優先 (imp 降順 x (1-ctr) 降順の
     スコア。scripts.detect_low_ctr_pages の閾値ロジックそのものは使わず、単純な
     スコアリングに留める)
  2. その後、未処理の記事を stem 順で埋める (全量 backfill)
  3. --force 無しなら sidecar に faq_extended と meta_description_optimized の
     両方が既にある記事はスキップ (冪等・日次で少しずつ backfill する設計)
  4. --limit (既定 30) 件で打ち切り

生成は gemma4:26b-a4b-it-qat (Ollama /api/generate) へ記事 1 本につき 1 リクエスト。
記事本文には scripts.audit_query_entailment.build_judge_text をそのまま流用する
(再実装しない)。

グラウンディング制約 (owner 確定・agy/Web 検索は使わない):
  - 記事本文と商品データから回答できる質問のみを作る
  - 価格・最安値・在庫・ポイント・セール・割引など時間で変化する事実には
    一切言及しない (validate_faq/validate_meta_description が fail-closed で
    禁止語を弾く。既存 FAQ に価格の腐敗が混入していた実績があるため)

Issue: https://github.com/omochairo/amazon/issues/3332 (N1 設計コメント)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import re
import time
from typing import Any

import requests

from scripts._seo_sidecar import load_sidecar, sidecar_path, update_sidecar
from scripts.audit_query_entailment import (
    build_judge_text,
    discover_articles,
    extract_asin_from_page,
    find_latest_history_file,
)
from scripts.detect_low_ctr_pages import collect_top_queries_by_page

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("generate_faq_seo")

DEFAULT_ARTICLES_DIR = "data/articles"
DEFAULT_HISTORY_DIR = "data/analytics/gsc_history"
DEFAULT_SUGGEST_DIR = "data/raw/suggest"
DEFAULT_ANSWERABILITY = "data/analytics/answerability_audit.json"
DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_MODEL = "gemma4:26b-a4b-it-qat"
DEFAULT_LIMIT = 30

GSC_QUERIES_PER_PAGE = 5
MAX_DEMAND_QUERIES = 15

REQUEST_TIMEOUT = 180
_MAX_EXTRA_RETRIES = 2  # 初回 + 2 リトライ = 最大 3 回試行
_RETRY_SLEEP_SECONDS = 2.0

# --------------------------------------------------------------------------
# validator の閾値 (動的事実 = 時間で変化する事実の禁止語)
# --------------------------------------------------------------------------
MIN_QUESTION_LEN = 5
MAX_QUESTION_LEN = 100
MIN_ANSWER_LEN = 20
MAX_ANSWER_LEN = 400
MAX_FAQ_ITEMS = 6

MIN_META_LEN = 60
MAX_META_LEN = 140

DYNAMIC_FACT_WORDS: tuple[str, ...] = (
    "価格", "最安", "円", "ポイント", "在庫", "セール", "割引", "%オフ",
    "送料", "キャンペーン", "タイムセール",
)
_PRICE_NUMBER_RE = re.compile(r"\d[\d,]*\s*円")

GENERATION_PROMPT_TEMPLATE = """あなたは記事の FAQ を実需要クエリで拡張し、meta_description を刷新するアシスタントです。

# 記事本文
{article_text}

# 実需要クエリ (検索ユーザーが実際に検索している/知りたいこと)
{demand_queries}

# 既存 FAQ
{existing_faq}

## 指示
- 記事本文と商品データから回答できる質問のみを作ってください。答えが本文に無い質問は作らないでください。
- 価格・最安値・在庫・ポイント・セール・割引など時間で変化する事実には一切言及しないでください。
- FAQ は最大6問。既存FAQのうち有用なものは残してよいですが、動的事実に触れているものは必ず捨ててください。
- meta_descriptionは80〜120字の日本語で、検索クエリを自然に織り込んでください。

次の JSON スキーマだけを出力してください (他の説明文は一切含めない):
{{"faq": [{{"question": "...", "answer": "..."}}], "meta_description": "..."}}
"""


class GenerationError(Exception):
    """gemma 生成呼び出しがリトライ上限まで失敗した。"""


# --------------------------------------------------------------------------
# validator (pure function)
# --------------------------------------------------------------------------

def _contains_dynamic_fact(text: str) -> bool:
    """時間で変化する事実 (価格・在庫・セール等) の禁止語を含むかを判定する。"""
    if any(word in text for word in DYNAMIC_FACT_WORDS):
        return True
    return bool(_PRICE_NUMBER_RE.search(text))


def validate_faq(faq: Any, *, max_items: int = MAX_FAQ_ITEMS) -> list[dict[str, str]]:
    """gemma が生成した FAQ 候補を検証し、有効な Q&A だけを返す (fail-closed)。

    1 件の drop が他の Q&A の判定を妨げない。同一 question は先勝ちで重複除去し、
    最終的に ``max_items`` 件を超えたら先頭から切り詰める。
    """
    if not isinstance(faq, list):
        return []

    kept: list[dict[str, str]] = []
    seen_questions: set[str] = set()
    for item in faq:
        if not isinstance(item, dict):
            continue
        question = item.get("question")
        answer = item.get("answer")
        if not isinstance(question, str) or not isinstance(answer, str):
            continue
        question = question.strip()
        answer = answer.strip()
        if not question or not answer:
            continue
        if len(question) < MIN_QUESTION_LEN or len(question) > MAX_QUESTION_LEN:
            continue
        if len(answer) < MIN_ANSWER_LEN or len(answer) > MAX_ANSWER_LEN:
            continue
        if _contains_dynamic_fact(question) or _contains_dynamic_fact(answer):
            continue
        if question in seen_questions:
            continue
        seen_questions.add(question)
        kept.append({"question": question, "answer": answer})

    return kept[:max_items]


def validate_meta_description(text: Any) -> str | None:
    """gemma が生成した meta_description を検証する。不採用なら None を返す。

    生成指示は 80〜120 字だが、validator は 60〜140 字まで許容幅を持たせる。
    """
    if not isinstance(text, str):
        return None
    if "\n" in text or "\r" in text:
        return None
    stripped = text.strip()
    if len(stripped) < MIN_META_LEN or len(stripped) > MAX_META_LEN:
        return None
    if _contains_dynamic_fact(stripped):
        return None
    return stripped


# --------------------------------------------------------------------------
# 対象選定 (優先度キュー)
# --------------------------------------------------------------------------

def build_asin_page_map(by_page: list[dict[str, Any]]) -> dict[str, str]:
    """GSC by_page の各行から asin -> page URL のマップを作る (先勝ち)。"""
    out: dict[str, str] = {}
    for row in by_page or []:
        page = row.get("page")
        if not page:
            continue
        asin = extract_asin_from_page(page)
        if asin and asin not in out:
            out[asin] = page
    return out


def build_priority_queue(article_index: dict[str, pathlib.Path], by_page: list[dict[str, Any]]) -> list[str]:
    """asin を優先度順 (GSC 高 imp x 低 CTR 優先 → 未処理 backfill) に並べる。

    スコア = impressions * (1 - ctr) (impressions 降順 x ctr 昇順に相当)。
    同点は asin 昇順で決定的に tie-break する。GSC に出てこない残りの記事は
    stem 昇順で末尾に追加する (全量 backfill)。
    """
    scored: list[tuple[float, str]] = []
    seen: set[str] = set()
    for row in by_page or []:
        page = row.get("page")
        impressions = row.get("impressions", 0) or 0
        ctr = row.get("ctr", 0.0) or 0.0
        if not page or impressions <= 0:
            continue
        asin = extract_asin_from_page(page)
        if not asin or asin not in article_index or asin in seen:
            continue
        score = impressions * (1.0 - ctr)
        scored.append((score, asin))
        seen.add(asin)

    scored.sort(key=lambda t: (-t[0], t[1]))
    ordered = [asin for _, asin in scored]

    remaining = sorted(
        (asin for asin in article_index if asin not in seen),
        key=lambda a: article_index[a].stem,
    )
    ordered.extend(remaining)
    return ordered


def select_targets(
    article_index: dict[str, pathlib.Path],
    by_page: list[dict[str, Any]],
    articles_dir: pathlib.Path,
    *,
    force: bool = False,
    limit: int = DEFAULT_LIMIT,
) -> list[tuple[str, pathlib.Path, pathlib.Path]]:
    """優先度順に並べ、skip 判定・limit を適用した処理対象リストを返す。

    戻り値は ``(asin, article_path, sidecar_path)`` のリスト。
    """
    ordered = build_priority_queue(article_index, by_page)
    targets: list[tuple[str, pathlib.Path, pathlib.Path]] = []
    for asin in ordered:
        article_path = article_index[asin]
        sc_path = sidecar_path(articles_dir, article_path.stem)
        if not force:
            existing = load_sidecar(sc_path)
            if existing.get("faq_extended") and existing.get("meta_description_optimized"):
                continue
        targets.append((asin, article_path, sc_path))
        if len(targets) >= limit:
            break
    return targets


# --------------------------------------------------------------------------
# 実需要クエリの収集
# --------------------------------------------------------------------------

def load_suggest_queries(suggest_dir: pathlib.Path, asin: str) -> list[str]:
    """data/raw/suggest/<ASIN>.json から suggestions[].query の一覧を読む。"""
    path = suggest_dir / f"{asin}.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    out: list[str] = []
    for s in data.get("suggestions") or []:
        if isinstance(s, dict):
            q = s.get("query")
            if isinstance(q, str) and q.strip():
                out.append(q.strip())
    return out


def load_answerability_missing_aspects(answerability_path: pathlib.Path) -> dict[str, list[str]]:
    """answerability_audit.json から asin -> missing_aspects (フラット化) を作る。"""
    if not answerability_path.exists():
        return {}
    try:
        data = json.loads(answerability_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}

    out: dict[str, list[str]] = {}
    for page in data.get("pages") or []:
        if not isinstance(page, dict):
            continue
        asin = page.get("asin")
        if not isinstance(asin, str) or not asin:
            continue
        aspects: list[str] = []
        for q in page.get("queries") or []:
            if not isinstance(q, dict):
                continue
            for m in q.get("missing_aspects") or []:
                if isinstance(m, str) and m.strip():
                    aspects.append(m.strip())
        if aspects:
            out.setdefault(asin, []).extend(aspects)
    return out


def merge_demand_queries(
    suggest_queries: list[str],
    gsc_queries: list[str],
    missing_aspects: list[str],
    *,
    limit: int = MAX_DEMAND_QUERIES,
) -> list[str]:
    """suggest + GSC 上位クエリ + missing_aspects をマージし、重複除去 + 上限で切り詰める。"""
    merged: list[str] = []
    seen: set[str] = set()
    for q in list(suggest_queries) + list(gsc_queries) + list(missing_aspects):
        if not isinstance(q, str):
            continue
        q = q.strip()
        if q and q not in seen:
            seen.add(q)
            merged.append(q)
    return merged[:limit]


# --------------------------------------------------------------------------
# gemma 呼び出し
# --------------------------------------------------------------------------

def call_gemma_faq_seo(
    article_text: str,
    demand_queries: list[str],
    existing_faq: list[dict[str, Any]],
    ollama_url: str,
    model: str,
    session: requests.Session,
    sleeper=time.sleep,
) -> dict[str, Any]:
    """gemma (`/api/generate`) に FAQ 拡張 + meta_description 刷新を問い合わせる。

    HTTP エラー / 接続エラー / JSON パース失敗は最大 ``_MAX_EXTRA_RETRIES`` 回まで
    再試行する。それでも失敗したら例外を送出せず、``error`` フィールド付きの
    dict を返す (1 記事の失敗で他記事の処理を止めない)。
    """
    url = f"{ollama_url.rstrip('/')}/api/generate"
    demand_text = "\n".join(f"- {q}" for q in demand_queries) if demand_queries else "(なし)"
    existing_lines = [
        f"Q: {item.get('question')}\nA: {item.get('answer')}"
        for item in existing_faq
        if isinstance(item, dict) and isinstance(item.get("question"), str) and isinstance(item.get("answer"), str)
    ]
    existing_text = "\n".join(existing_lines) if existing_lines else "(なし)"
    prompt = GENERATION_PROMPT_TEMPLATE.format(
        article_text=article_text, demand_queries=demand_text, existing_faq=existing_text,
    )
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
                raise GenerationError("empty /api/generate response")
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise GenerationError("generation response is not a JSON object")
            return {
                "faq": parsed.get("faq") if isinstance(parsed.get("faq"), list) else [],
                "meta_description": parsed.get("meta_description"),
                "error": None,
            }
        except (requests.RequestException, GenerationError, ValueError, json.JSONDecodeError) as e:
            last_err = e
            if attempt < attempts:
                logger.warning("generation call failed (attempt %d/%d): %s", attempt, attempts, e)
                sleeper(_RETRY_SLEEP_SECONDS)
            else:
                logger.error("generation call failed after %d attempt(s): %s", attempts, e)

    return {"faq": [], "meta_description": None, "error": str(last_err)}


# --------------------------------------------------------------------------
# 実行本体
# --------------------------------------------------------------------------

def run(
    articles_dir: pathlib.Path,
    *,
    history_dir: pathlib.Path,
    history_file: pathlib.Path | None = None,
    suggest_dir: pathlib.Path,
    answerability_path: pathlib.Path,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    model: str = DEFAULT_MODEL,
    limit: int = DEFAULT_LIMIT,
    force: bool = False,
    dry_run: bool = False,
    session: requests.Session | None = None,
    sleeper=time.sleep,
) -> dict[str, Any]:
    """全記事を走査し、対象を選定して sidecar を書く (テスト・main 双方から呼べるように分離)。"""
    summary: dict[str, Any] = {
        "processed": 0, "faq_written": 0, "meta_written": 0, "faq_dropped": 0, "errors": 0,
    }

    article_index = discover_articles(articles_dir)
    if not article_index:
        logger.info("no articles found under %s", articles_dir)
        return summary

    src = history_file or find_latest_history_file(history_dir)
    gsc: dict[str, Any] = {}
    if src is not None and src.exists():
        try:
            gsc = json.loads(src.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("failed to read gsc history %s: %s; proceeding backfill-only", src, e)
    else:
        logger.info("no gsc_history file found under %s; proceeding backfill-only", history_dir)

    by_page = gsc.get("by_page") or []
    queries_by_page = collect_top_queries_by_page(gsc.get("by_combo") or [], GSC_QUERIES_PER_PAGE)
    asin_to_page = build_asin_page_map(by_page)
    missing_by_asin = load_answerability_missing_aspects(answerability_path)

    targets = select_targets(article_index, by_page, articles_dir, force=force, limit=limit)
    if not targets:
        logger.info("no target articles to process (all up to date, or none discovered)")
        return summary

    session = session or requests.Session()
    dry_run_results: list[dict[str, Any]] = []

    for asin, article_path, sc_path in targets:
        try:
            article = json.loads(article_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("skip %s: failed to read/parse: %s", article_path, e)
            summary["processed"] += 1
            summary["errors"] += 1
            continue

        article_text = build_judge_text(article)
        suggest_queries = load_suggest_queries(suggest_dir, asin)
        gsc_queries = [q.get("query", "") for q in queries_by_page.get(asin_to_page.get(asin, ""), [])]
        missing_aspects = missing_by_asin.get(asin, [])
        demand_queries = merge_demand_queries(suggest_queries, gsc_queries, missing_aspects)
        existing_faq = article.get("faq") if isinstance(article.get("faq"), list) else []

        result = call_gemma_faq_seo(article_text, demand_queries, existing_faq, ollama_url, model, session, sleeper)
        summary["processed"] += 1

        if result.get("error"):
            summary["errors"] += 1
            logger.error("generation failed for %s: %s", article_path.stem, result["error"])
            continue

        raw_faq = result.get("faq") or []
        validated_faq = validate_faq(raw_faq)
        summary["faq_dropped"] += max(0, len(raw_faq) - len(validated_faq))
        validated_meta = validate_meta_description(result.get("meta_description"))

        updates: dict[str, Any] = {}
        if validated_faq:
            updates["faq_extended"] = validated_faq
            summary["faq_written"] += 1
        if validated_meta:
            updates["meta_description_optimized"] = validated_meta
            summary["meta_written"] += 1

        if dry_run:
            entry = {"asin": asin, "stem": article_path.stem}
            entry.update(updates)
            dry_run_results.append(entry)
            continue

        if updates:
            update_sidecar(sc_path, updates)

    logger.info(
        "done: processed=%d faq_written=%d meta_written=%d faq_dropped=%d errors=%d",
        summary["processed"], summary["faq_written"], summary["meta_written"],
        summary["faq_dropped"], summary["errors"],
    )
    print(json.dumps(summary, ensure_ascii=False))
    if dry_run:
        print(json.dumps(dry_run_results, ensure_ascii=False, indent=2))
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--articles-dir", default=DEFAULT_ARTICLES_DIR, help="記事 JSON のディレクトリ")
    ap.add_argument("--history-dir", default=DEFAULT_HISTORY_DIR, help="GSC 週次履歴のディレクトリ")
    ap.add_argument("--history-file", default=None, help="明示的な履歴ファイルパス (未指定なら最新週を自動選択)")
    ap.add_argument("--suggest-dir", default=DEFAULT_SUGGEST_DIR, help="Google suggest 実需要クエリのディレクトリ")
    ap.add_argument("--answerability", default=DEFAULT_ANSWERABILITY, help="回答性監査 JSON のパス")
    ap.add_argument("--ollama-url", default=os.environ.get("OLLAMA_URL", DEFAULT_OLLAMA_URL))
    ap.add_argument("--model", default=os.environ.get("SEO_GEN_MODEL", DEFAULT_MODEL))
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="1 回の実行で処理する記事数の上限")
    ap.add_argument("--force", action="store_true", help="既に faq_extended/meta_description_optimized がある記事も再生成する")
    ap.add_argument("--dry-run", action="store_true", help="sidecar を書かず、生成結果を stdout に出す")
    args = ap.parse_args()

    run(
        articles_dir=pathlib.Path(args.articles_dir),
        history_dir=pathlib.Path(args.history_dir),
        history_file=pathlib.Path(args.history_file) if args.history_file else None,
        suggest_dir=pathlib.Path(args.suggest_dir),
        answerability_path=pathlib.Path(args.answerability),
        ollama_url=args.ollama_url,
        model=args.model,
        limit=args.limit,
        force=args.force,
        dry_run=args.dry_run,
    )
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
