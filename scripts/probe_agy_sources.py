#!/usr/bin/env python3
"""agy から「出典 URL」を取れるか測る probe (#3203 / #2699 柱0)。

## 何を確かめたいか

`gather_antigravity` は今 `source_url: ""` を返している。**このレーンだけ出所が
辿れない**。体験談の素材としては使えても、E-E-A-T の裏付けにも、後からの検証にも
使えない。ここに実 URL を入れられるかを、実装に入る前に実測で確かめる。

## 先に判明した制約 (2026-09-06 実測)

1. **`read_url` は headless では使えない。** 「原文のまま抜粋しろ」と頼むと agy は
   個別ページを開こうとし、`read_url` 権限が headless で auto-deny されて
   `status: CANCELED` + 空応答になる。**検索結果の範囲で完結させる必要がある。**
   (権限を開ける手はあるが、agy の信頼境界を広げるので owner 判断が要る)
2. **`--json-schema` は Web 検索と併用できない。** ツールを使わないプロンプトでは
   `structured_output` が返るが、検索グラウンディングが走ると無視されて散文が
   返る。**構造化出力に頼れないので、URL は本文からパースする。**
3. **URL は Vertex AI の grounding redirect で返る。**
   `vertexaisearch.cloud.google.com/grounding-api-redirect/...` という不透明な
   トークン URL。ただし **302 を辿れば実 URL に解決できる**ことは実測済み
   (楽天の商品ページに解決した)。収集時に解決して保存すれば実用になる。

## 測る指標

  urls/call     1 コールあたり何本の URL が返るか
  resolve_rate  grounding redirect を実 URL に解決できた割合
  http_ok       解決先が 200 で返る割合 (ハルシネーション URL の検出)
  domains       解決先の異なるドメイン数 (1 サイトに偏っていないか)
  そのほか本文の品質は bench_agy_model の採点器を流用する

使い方:
  python3 -m scripts.probe_agy_sources --trials 3 --products 3 \\
      --out data/analytics/agy_sources_probe.json
  python3 -m scripts.probe_agy_sources --report data/analytics/agy_sources_probe.json
"""

from __future__ import annotations

import argparse
import json
import logging
import pathlib
import re
import statistics
import sys
import time
import urllib.parse

import requests

try:
    from scripts import bench_agy_model, mine_experience
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    from scripts import bench_agy_model, mine_experience

logger = logging.getLogger("probe_agy_sources")

DEFAULT_OUT = pathlib.Path("data/analytics/agy_sources_probe.json")
DEFAULT_MODEL = mine_experience.DEFAULT_ANTIGRAVITY_MODEL
FETCH_TIMEOUT = 20
FETCH_SLEEP_S = 1.0  # 解決のための外部アクセス。礼儀として間隔を空ける

_GROUNDING_HOST = "vertexaisearch.cloud.google.com"
_GROUNDING_PATH = "/grounding-api-redirect/"
# 散文から URL を拾う。markdown リンクの中にあることが多いので括弧・記号で切る
_URL_RE = re.compile(r"https?://[^\s<>\"'））\]\[|、。]+")


# --------------------------------------------------------------------------
# プロンプト variant
# --------------------------------------------------------------------------

def _summary_legacy_prompt(product_name: str, brand: str) -> str:
    """#6588 以前の gather_antigravity プロンプト (対照群として凍結)。

    production 側は `summary_url_balanced` に移行したので
    `mine_experience.build_antigravity_prompt` を参照すると対照にならない。
    **ここを production に追従させないこと** — 過去の実測値と比較できなくなる。
    """
    return (
        f"Web検索ツールを使って『{product_name} ({brand})』という商品の購入者の"
        "口コミ・評判・使用感を調べ、事実に基づき3〜5行の日本語箇条書きで要約して"
        "ください。ファイル操作・コード編集は一切不要です。テキストで直接回答して"
        "ください。"
    )


def _summary_with_url_prompt(product_name: str, brand: str) -> str:
    """現行の要約に出典 URL を足させるだけ。traceability の最小追加。"""
    return (
        f"Web検索ツールを使って『{product_name} ({brand})』という商品の購入者の"
        "口コミ・評判・使用感を調べ、事実に基づき3〜5行の日本語箇条書きで要約して"
        "ください。**各行の末尾に、その内容の出典URLを1つ必ず `出典: <URL>` の形で"
        "付けてください。** 検索結果に出たURLをそのまま書き、URLを作文しないこと。"
        "ファイル操作・コード編集は一切不要です。テキストで直接回答してください。"
    )


def _excerpt_with_url_prompt(product_name: str, brand: str) -> str:
    """抜粋 + URL。ページを開かせない (read_url が headless で auto-deny のため)。"""
    return (
        f"Web検索ツールを使って『{product_name} ({brand})』という商品の購入者の"
        "口コミを調べてください。**検索結果に表示された内容だけを使い、個別の"
        "ページを開かない(URLを読み込まない)こと。** 検索結果に出てきた口コミの"
        "文言と、その出典URL・サイト名を3〜5件、`- 「<口コミ>」 出典: <URL>` の"
        "形式で返してください。URLを作文しないこと。ファイル操作・コード編集は"
        "一切不要です。"
    )


PROMPTS = {
    "summary_legacy": _summary_legacy_prompt,       # #6588 以前 (対照群・凍結)
    "summary_url": _summary_with_url_prompt,
    # production の現行。ここは追従させる — 本番と同じ文字列で測るため
    "summary_url_balanced": mine_experience.build_antigravity_prompt,
    "excerpt_url": _excerpt_with_url_prompt,
}


# --------------------------------------------------------------------------
# URL の抽出と解決
# --------------------------------------------------------------------------

def extract_urls(text: str) -> list[str]:
    """散文から URL を重複なく拾う。

    `--json-schema` が Web 検索と併用できないので、構造化出力には頼れない。
    markdown リンク `[表示](url)` の閉じ括弧や全角括弧・句読点で切る。
    """
    out: list[str] = []
    for raw in _URL_RE.findall(text or ""):
        url = raw.rstrip(").,、。」』>")
        if url and url not in out:
            out.append(url)
    return out


def is_grounding_redirect(url: str) -> bool:
    """Vertex AI の grounding redirect か。

    Gemini の検索グラウンディングは実 URL ではなく不透明なリダイレクト URL を
    返す。これをそのまま source_url に保存しても後から辿れる保証がないので、
    収集時に解決する必要がある。
    """
    try:
        p = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return p.netloc == _GROUNDING_HOST and p.path.startswith(_GROUNDING_PATH)


def resolve_source_url(url: str, session: requests.Session) -> dict:
    """URL を辿って最終 URL と HTTP status を返す。

    grounding redirect は 302 で実 URL に飛ぶ。直リンクならそのまま到達性の確認。
    ここで 200 が返らない URL は **ハルシネーションか失効**なので、source_url に
    入れてはいけない。
    """
    rec = {
        "url": url,
        "is_redirect": is_grounding_redirect(url),
        "final_url": "",
        "status": 0,
        "domain": "",
        "error": "",
    }
    try:
        resp = session.get(
            url, timeout=FETCH_TIMEOUT, allow_redirects=True,
            headers={"User-Agent": mine_experience.HONEST_UA},
        )
        rec["status"] = resp.status_code
        rec["final_url"] = resp.url
        rec["domain"] = urllib.parse.urlsplit(resp.url).netloc
    except requests.RequestException as e:
        rec["error"] = f"{type(e).__name__}: {e}"[:200]
    return rec


# --------------------------------------------------------------------------
# 1 ケース
# --------------------------------------------------------------------------

def run_case(
    variant: str, product: dict, model: str, session: requests.Session,
    retries: int = 2, sleeper=time.sleep,
) -> dict:
    prompt = PROMPTS[variant](product["product_name"], product["brand"])
    res = bench_agy_model.call_agy_json(prompt, model, retries=retries)

    rec = {
        "variant": variant,
        "asin": product["asin"],
        "product_name": product["product_name"],
        "brand": product["brand"],
        "ok": res["ok"],
        "error": res.get("error"),
        "attempts": res.get("attempts", 1),
        "latency_s": round(res.get("latency_s") or 0.0, 2),
        "text": res.get("text", ""),
        "urls": [],
    }
    if not res["ok"]:
        return rec

    rec.update(bench_agy_model.score_text(
        res["text"], product["product_name"], product["brand"]))
    rec["text"] = res["text"]  # score_text は text を返さないので入れ直す

    for url in extract_urls(res["text"]):
        rec["urls"].append(resolve_source_url(url, session))
        sleeper(FETCH_SLEEP_S)
    return rec


def summarize(records: list[dict]) -> list[dict]:
    by: dict[str, list[dict]] = {}
    for r in records:
        by.setdefault(r["variant"], []).append(r)

    rows = []
    for variant, rs in by.items():
        oks = [r for r in rs if r["ok"]]
        urls = [u for r in oks for u in r["urls"]]
        resolved = [u for u in urls if u["status"] == 200]
        domains = {u["domain"] for u in resolved if u["domain"]}
        redirects = [u for u in urls if u["is_redirect"]]
        rows.append({
            "variant": variant,
            "n": len(rs),
            "success_rate": round(len(oks) / len(rs), 2) if rs else 0.0,
            "score": round(statistics.fmean([r.get("score", 0.0) for r in rs]), 3) if rs else 0.0,
            "urls_per_call": round(len(urls) / len(oks), 2) if oks else 0.0,
            "calls_with_url": round(
                sum(1 for r in oks if r["urls"]) / len(oks), 2) if oks else 0.0,
            "redirect_share": round(len(redirects) / len(urls), 2) if urls else 0.0,
            "http_ok": round(len(resolved) / len(urls), 2) if urls else 0.0,
            "domains": len(domains),
            "domain_list": sorted(domains)[:8],
            "latency_p50": sorted([r["latency_s"] for r in oks])[len(oks) // 2] if oks else 0.0,
        })
    rows.sort(key=lambda r: (-r["http_ok"] * r["urls_per_call"], -r["score"]))
    return rows


def format_report(rows: list[dict]) -> str:
    head = (
        f"{'variant':14} {'n':>3} {'ok':>5} {'score':>6} {'url/call':>9} "
        f"{'有URL率':>8} {'redirect':>9} {'http200':>8} {'domains':>8} {'p50s':>6}"
    )
    lines = [head, "-" * len(head)]
    for r in rows:
        lines.append(
            f"{r['variant']:14} {r['n']:>3} {r['success_rate']:>5.2f} {r['score']:>6.3f} "
            f"{r['urls_per_call']:>9.2f} {r['calls_with_url']:>8.2f} "
            f"{r['redirect_share']:>9.2f} {r['http_ok']:>8.2f} {r['domains']:>8} "
            f"{r['latency_p50']:>6.1f}"
        )
        if r["domain_list"]:
            lines.append(f"{'':14} → {', '.join(r['domain_list'])}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--variants", nargs="*", default=list(PROMPTS))
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--products", type=int, default=3)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--out", type=pathlib.Path, default=DEFAULT_OUT)
    ap.add_argument("--report", type=pathlib.Path, default=None)
    args = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover
            pass

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.report:
        records = json.loads(args.report.read_text(encoding="utf-8"))["records"]
        # 採点器を直したら採り直さずに採点し直す (bench_agy_model と同じ方針)
        records = bench_agy_model.rescore(records)
    else:
        products = bench_agy_model.load_products(args.products)
        if not products:
            logger.error("対象商品が 0 件")
            return 1
        session = requests.Session()
        records = []
        args.out.parent.mkdir(parents=True, exist_ok=True)
        total = len(args.variants) * len(products) * args.trials
        i = 0
        for trial in range(args.trials):
            for prod in products:
                for variant in args.variants:
                    i += 1
                    logger.info("[%d/%d] trial=%d variant=%s asin=%s",
                                i, total, trial, variant, prod["asin"])
                    rec = run_case(variant, prod, args.model, session, args.retries)
                    rec["trial"] = trial
                    records.append(rec)
                    args.out.write_text(
                        json.dumps({"records": records}, ensure_ascii=False, indent=1),
                        encoding="utf-8")

    print(format_report(summarize(records)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
