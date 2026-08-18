"""audit_query_entailment.py

Issue #2995 消費側②「GSC 回答性 entailment 監査」の計算スクリプト。

scripts/detect_low_ctr_pages.py が検出する低 CTR ページ (A-1) について、記事本文が
実際に検索クエリへ回答しているかを K8 LLM ワーカーの gemma judge
(``gemma4:26b-a4b-it-qat``、Ollama `/api/generate`) で判定し、
``data/analytics/answerability_audit.json`` に書き出す read-only スクリプト。

入力:
  ``data/analytics/gsc_history/<年>-W<週>.json`` (18-analytics-daily.yml が
  committed する週次履歴。``by_combo``/``by_page``/``totals`` を含み、
  scripts.detect_low_ctr_pages.detect() の入力形式と同一)。ファイル名が最も新しい
  ものを自動選択する (--history-file で明示指定も可)。GSC API 認証情報は不要。

処理:
  1. detect_low_ctr_pages.detect() をライブラリ流用して低 CTR ページ + top queries
     を検出する (閾値ロジックは再実装しない)。
  2. ページ URL から ASIN を抽出し、data/articles/*.json (compute_semantic_related の
     discover_articles と同じ選定規則) から記事本文を解決する。
  3. 記事本文 (title/meta_description/narrative/product/technical_specs/faq) から
     判定用テキストを組み立て、クエリ毎に gemma judge へ 1 リクエストずつ投げる。
  4. 判定結果 (answered/coverage/missing_aspects) をページ毎に集約して出力する。

このフェーズの位置づけ (#2995 案11-13):
  judge の出力は **参考値**。#2928/#2929 のようなサンプル数の少ない検出に対して
  自動リライト要否を判断する段階ではなく、まず gemma の回答性判定が妥当かを
  owner が目視評価する (scripts/comment_answerability_audit.py が既存 low-CTR
  Issue に診断コメントを 1 回だけ追記する)。

Issue: https://github.com/omochairo/amazon/issues/2995 (消費側②) / #2928 / #2929
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import re
import time
from datetime import datetime, timezone
from typing import Any

import requests

from scripts.detect_low_ctr_pages import detect

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("audit_query_entailment")

DEFAULT_HISTORY_DIR = "data/analytics/gsc_history"
DEFAULT_ARTICLES_DIR = "data/articles"
DEFAULT_OUT = "data/analytics/answerability_audit.json"
DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_MODEL = "gemma4:26b-a4b-it-qat"
DEFAULT_MAX_QUERIES_PER_PAGE = 5
DEFAULT_LIMIT = 0

REQUEST_TIMEOUT = 180
MAX_JUDGE_TEXT_LEN = 8000
_MAX_EXTRA_RETRIES = 2  # 初回 + 2 リトライ = 最大 3 回試行
_RETRY_SLEEP_SECONDS = 2.0

# #4826 項目6: 除外していたのは .quality.json だけだった。あの sidecar は
# 廃止されて実体が 0 件になった一方、実在するのは .seo.json / .enrichment.json で、
# それらが素通りしないのは下の _ASIN_RE が `<stem>.seo` を弾くから **だけ** だった。
# 偶然の防波堤 1 枚に頼る形なので、除外そのものを実体に合わせる。
_SIDECAR_SUFFIXES = (".quality.json", ".seo.json", ".enrichment.json")
_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
_PAGE_ASIN_RE = re.compile(r"/products/([a-zA-Z0-9]+)/?(?:$|[?#])")

JUDGE_PROMPT_TEMPLATE = """あなたは記事が検索クエリに十分に回答しているかを判定するアシスタントです。

# 記事本文
{article_text}

# 検索クエリ
{query}

上記の記事本文が、この検索クエリを検索したユーザーの疑問に十分答えているかを判定してください。
次の JSON スキーマだけを出力してください (他の説明文は一切含めない):
{{"answered": true または false, "coverage": 0.0から1.0の数値 (どの程度網羅しているか), "missing_aspects": [記事に不足している観点を短い日本語で列挙した配列 (無ければ空配列)], "note": "1文の補足コメント"}}
"""


class JudgeError(Exception):
    """gemma judge 呼び出しがリトライ上限まで失敗した。"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# 入力ファイル選定
# --------------------------------------------------------------------------

def find_latest_history_file(history_dir: pathlib.Path) -> pathlib.Path | None:
    """data/analytics/gsc_history/*.json からファイル名が最も新しいものを選ぶ。

    ファイル名は ``<年>-W<週2桁>.json`` (例: 2026-W28.json) で zero-padded の
    ため文字列比較の max がそのまま最新週になる。
    """
    if not history_dir.exists():
        return None
    files = sorted(history_dir.glob("*.json"))
    return files[-1] if files else None


# --------------------------------------------------------------------------
# 記事本文の解決 (compute_semantic_related.discover_articles と同じ選定規則)
# --------------------------------------------------------------------------

def discover_articles(articles_dir: pathlib.Path) -> dict[str, pathlib.Path]:
    """data/articles/*.json から {asin: article_path} を作る (quality sidecar 除外)。

    同一 ASIN の記事が複数ある場合は stem が最も新しい (文字列として大きい) ものを
    採用する (rewrite で新旧併存, #2711 と同じ流儀)。
    """
    if not articles_dir.exists():
        return {}
    winners: dict[str, str] = {}
    paths: dict[str, pathlib.Path] = {}
    for f in sorted(articles_dir.glob("*.json")):
        if f.name.endswith(_SIDECAR_SUFFIXES):
            continue
        stem = f.stem
        parts = stem.split("-")
        asin = parts[-1] if parts else ""
        if not _ASIN_RE.match(asin):
            continue
        if asin not in winners or stem > winners[asin]:
            winners[asin] = stem
            paths[asin] = f
    return paths


def extract_asin_from_page(page: str) -> str | None:
    """ページ URL (https://navi.omcha.jp/products/<asin>/) から ASIN を抽出する。"""
    if not page:
        return None
    m = _PAGE_ASIN_RE.search(page)
    if not m:
        return None
    asin = m.group(1).upper()
    return asin if _ASIN_RE.match(asin) else None


# --------------------------------------------------------------------------
# 判定用テキスト組み立て (pure function)
# --------------------------------------------------------------------------

def build_judge_text(article: dict[str, Any]) -> str:
    """記事 JSON dict から gemma judge 用の本文テキストを組み立てる。

    title / meta_description / narrative.* / product.name+features /
    technical_specs.other / faq (Q&A) を連結する。どのフィールドが欠けていても
    クラッシュしない。
    """
    if not isinstance(article, dict):
        return ""

    parts: list[str] = []

    title = article.get("title")
    if isinstance(title, str) and title.strip():
        parts.append(title.strip())

    meta = article.get("meta_description")
    if isinstance(meta, str) and meta.strip():
        parts.append(meta.strip())

    narrative = article.get("narrative")
    narrative = narrative if isinstance(narrative, dict) else {}
    for key in ("lead", "why_this_product", "gift_appeal", "daily_use", "safety_note", "closing", "how_to_choose"):
        v = narrative.get(key)
        if isinstance(v, str) and v.strip():
            parts.append(v.strip())

    product = article.get("product")
    product = product if isinstance(product, dict) else {}
    name = product.get("name")
    if isinstance(name, str) and name.strip():
        parts.append(name.strip())
    features = product.get("features")
    if isinstance(features, list):
        f_strs = [str(x).strip() for x in features if isinstance(x, str) and x.strip()]
        if f_strs:
            parts.append("特徴: " + " / ".join(f_strs))

    tech = article.get("technical_specs")
    tech = tech if isinstance(tech, dict) else {}
    other = tech.get("other")
    if isinstance(other, list):
        o_strs = [str(x).strip() for x in other if isinstance(x, str) and x.strip()]
        if o_strs:
            parts.append("仕様: " + " / ".join(o_strs))

    faq = article.get("faq")
    if isinstance(faq, list):
        for item in faq:
            if not isinstance(item, dict):
                continue
            q = item.get("question")
            a = item.get("answer")
            if isinstance(q, str) and isinstance(a, str) and q.strip() and a.strip():
                parts.append(f"Q: {q.strip()}\nA: {a.strip()}")

    text = "\n\n".join(parts)
    return text[:MAX_JUDGE_TEXT_LEN]


# --------------------------------------------------------------------------
# gemma judge 呼び出し
# --------------------------------------------------------------------------

def judge_query(
    article_text: str,
    query: str,
    ollama_url: str,
    model: str,
    session: requests.Session,
    sleeper=time.sleep,
) -> dict[str, Any]:
    """1 クエリ分を gemma judge (`/api/generate`) に問い合わせる。

    HTTP エラー / 接続エラー / JSON パース失敗は最大 ``_MAX_EXTRA_RETRIES`` 回まで
    再試行する。それでも失敗したら例外を送出せず、``error`` フィールド付きの
    dict を返す (1 クエリの失敗で監査全体を止めない)。
    """
    url = f"{ollama_url.rstrip('/')}/api/generate"
    prompt = JUDGE_PROMPT_TEMPLATE.format(article_text=article_text, query=query)
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
            missing = parsed.get("missing_aspects")
            return {
                "answered": bool(parsed.get("answered")),
                "coverage": float(parsed.get("coverage")) if isinstance(parsed.get("coverage"), (int, float)) else None,
                "missing_aspects": [str(m) for m in missing] if isinstance(missing, list) else [],
                "note": str(parsed.get("note")) if parsed.get("note") is not None else "",
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
        "answered": None,
        "coverage": None,
        "missing_aspects": [],
        "note": "",
        "error": str(last_err),
    }


# --------------------------------------------------------------------------
# 実行本体
# --------------------------------------------------------------------------

def run(
    history_dir: pathlib.Path,
    articles_dir: pathlib.Path,
    out_path: pathlib.Path,
    *,
    history_file: pathlib.Path | None = None,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    model: str = DEFAULT_MODEL,
    max_queries_per_page: int = DEFAULT_MAX_QUERIES_PER_PAGE,
    limit: int = DEFAULT_LIMIT,
    session: requests.Session | None = None,
    sleeper=time.sleep,
) -> dict[str, Any]:
    """全体を実行し、書き込んだ payload を返す (テスト・main 双方から呼べるように分離)。"""
    src = history_file or find_latest_history_file(history_dir)
    if src is None or not src.exists():
        logger.error("no gsc_history file found under %s", history_dir)
        return {"written": False, "pages": 0}

    gsc = json.loads(src.read_text(encoding="utf-8"))
    result = detect(gsc)
    detected = result.get("detected") or []
    if limit and limit > 0:
        detected = detected[:limit]

    if not detected:
        logger.info("no low-CTR pages detected in %s; nothing to audit", src.name)
        return {"written": False, "pages": 0}

    article_index = discover_articles(articles_dir)
    session = session or requests.Session()

    pages_out: list[dict[str, Any]] = []
    for d in detected:
        page = d["page"]
        asin = extract_asin_from_page(page)
        article_path = article_index.get(asin) if asin else None
        if not article_path:
            logger.warning("skip %s: could not resolve article for asin=%s", page, asin)
            continue
        try:
            article = json.loads(article_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("skip %s: failed to read/parse %s: %s", page, article_path, e)
            continue

        article_text = build_judge_text(article)
        top_queries = (d.get("top_queries") or [])[:max_queries_per_page]

        queries_out: list[dict[str, Any]] = []
        coverages: list[float] = []
        for q in top_queries:
            query = q.get("query", "")
            if not query:
                continue
            verdict = judge_query(article_text, query, ollama_url, model, session, sleeper)
            if isinstance(verdict.get("coverage"), (int, float)):
                coverages.append(float(verdict["coverage"]))
            queries_out.append({
                "query": query,
                "impressions": q.get("impressions", 0),
                "position": q.get("position", 0.0),
                **verdict,
            })

        pages_out.append({
            "page": page,
            "asin": asin,
            "page_coverage_avg": round(sum(coverages) / len(coverages), 3) if coverages else None,
            "queries": queries_out,
        })

    payload = {
        "generated_at": _now_iso(),
        "model": model,
        "source_week": src.stem,
        "baseline_ctr": result.get("baseline_ctr"),
        "pages": pages_out,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("wrote %s: %d page(s) audited (source=%s)", out_path, len(pages_out), src.name)
    return {"written": True, "pages": len(pages_out), "payload": payload}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--history-dir", default=DEFAULT_HISTORY_DIR, help="GSC 週次履歴のディレクトリ")
    ap.add_argument("--history-file", default=None, help="明示的な履歴ファイルパス (未指定なら最新週を自動選択)")
    ap.add_argument("--articles-dir", default=DEFAULT_ARTICLES_DIR, help="記事 JSON のディレクトリ")
    ap.add_argument("--out", default=DEFAULT_OUT, help="出力先 JSON パス")
    ap.add_argument("--ollama-url", default=os.environ.get("OLLAMA_URL", DEFAULT_OLLAMA_URL))
    ap.add_argument("--model", default=os.environ.get("JUDGE_MODEL", DEFAULT_MODEL))
    ap.add_argument("--max-queries-per-page", type=int, default=DEFAULT_MAX_QUERIES_PER_PAGE)
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="監査するページ数の上限 (0=全件、スモーク用)")
    args = ap.parse_args()

    run(
        history_dir=pathlib.Path(args.history_dir),
        articles_dir=pathlib.Path(args.articles_dir),
        out_path=pathlib.Path(args.out),
        history_file=pathlib.Path(args.history_file) if args.history_file else None,
        ollama_url=args.ollama_url,
        model=args.model,
        max_queries_per_page=args.max_queries_per_page,
        limit=args.limit,
    )
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
