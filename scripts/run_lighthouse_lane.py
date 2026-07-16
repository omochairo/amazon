"""run_lighthouse_lane.py

#2995 案6「セルフホスト Lighthouse — CWV 全ページ実測」の計測スクリプト
(#1357 E2 / 19-cwv-monitor.yml の CrUX レーンと対になるラボ計測側)。

navi.omcha.jp の代表 URL 群に対して Lighthouse をローカル実行し、
`data/analytics/history/lighthouse_history.jsonl` に append する。前回までの
履歴と比較して劣化を検出し、劣化があったときだけ Markdown レポートを出して
呼び出し元 workflow に「issue を立てるべきか」を伝える。

なぜ CrUX (19-cwv-monitor) と別に要るか:
  CrUX は実ユーザ 28 日 rolling の p75 なので、①低トラフィック URL はデータ
  なし ②劣化が見えるまで数週間かかる ③ページ単位の原因 (どの audit が落ちたか)
  が分からない。ラボ計測は毎日・全ページ級・原因つきで取れるので、回帰を
  「リリース当日」に捕まえる早期警戒レーンとして相補的に効く。

なぜ PSI API でなくセルフホストか:
  PSI API はクォータ (匿名だと即 429、key ありでも 25k/day・400/100s) があり
  全ページ級の毎日計測には足りない。self-hosted runner なら Actions 分数も
  API 課金もゼロで、Lighthouse を無制限に回せる (#2995 の自宅レーン資産④)。

なぜこのリポジトリにスクリプトだけ置くか:
  omochairo/amazon は public リポジトリで self-hosted runner を繋げない
  (fork PR が workflow 定義ごと差し替えて自宅サーバーで任意コード実行できる)。
  実行は private な omochairo/amazon-home-ops の workflow から行い、本リポジトリ
  には計測ロジックと unit test のみを置く (CI からは呼ばない)。
  → fetch_google_suggest.py と同じ流儀。

設計判断:
- **median-of-N**: Lighthouse は 1 回の実行でも数百 ms 単位で振れる。単発値で
  閾値判定すると誤検出で issue が湧き、GitHub API バースト禁止規律 (CLAUDE.md)
  にも反する。既定 3 回実行の median を採用する。
- **error 状態を数値と分けて記録する**: 2026-07-16 の PSI 手動測定で LCP が
  `NO_LCP` エラーになり「LCP が壊れている」ように見えたが、同版の Lighthouse を
  ローカルで回すと LCP=5.0s と正常に出た (= 計測基盤側の失敗)。metric 値だけを
  JSONL に持つと、この 2 つが区別できず追跡不能になる。よって各 metric に
  `*_error` 列を持たせ、error 行は劣化判定から除外して別枠で報告する。
- **回帰判定は「直近 baseline の median」対比**: 絶対閾値 (CWV good) だけだと
  元から悪い指標が毎回鳴り続けてノイズになるので、①CWV 閾値をまたいだ悪化
  ②baseline 比の相対悪化、の 2 条件で鳴らす。
- 出力は data PR + auto-merge、報告は劣化時のみ 1 issue に集約 (バースト禁止規律)。

runner 側の前提 (amazon-home-ops の 41-lighthouse-lane.yml):
- 実行先は **K8 Plus (label `home,llm`)** で NAS (`home,ollama`) ではない。
  Chrome headless は 1GB 級を食うが、NAS (DXP4800) は RAM 7.7GB を ollama と
  gitlab-runner で分け合い runner の mem_limit は 4g しかない。K8 (Ryzen 7
  8845HS / 64GB, mem_limit 8g) が適地。#2995 は K8 稼働 (2026-07-14) より前に
  書かれた「NAS レーン拡張」issue だが、案6 の実体は LLM ではなくブラウザ計測。
- Node.js + `lighthouse` CLI と Chrome が必要。`--lighthouse-cmd` /
  `LIGHTHOUSE_CMD`、`CHROME_PATH` で差し替えできる。
- Lighthouse は PSI と同じメジャー版に固定する (audit id / errorMessage の
  互換性のため。版が飛ぶと baseline が不連続になり誤検出の温床になる)。
- `from __future__ import annotations` は維持する。NAS (python3.8) に載せ替えても
  壊れないようにするための保険 ([[reference-omochairo-home-ops-repo]] の実績: 3.9+ の
  組み込みジェネリック注釈は 3.8 の import 時に落ちる)。

副作用:
- data/analytics/history/lighthouse_history.jsonl への append
- data/analytics/history/seen_dates.json の `lighthouse` key 更新
- --report-out 指定時、劣化レポート Markdown の書き出し
- 記事生成 / score / narrative には触れない

Issue: https://github.com/omochairo/amazon/issues/2995 (案6) / #1357 (epic E2)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import statistics
import subprocess
import sys
import tempfile
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("run_lighthouse_lane")

DEFAULT_HISTORY_DIR = "data/analytics/history"
LIGHTHOUSE_HISTORY_FILENAME = "lighthouse_history.jsonl"
SEEN_DATES_FILENAME = "seen_dates.json"
# GSC 上位 URL の入力元。main に実在するのは週次の
# data/analytics/gsc_history/<ISO週>.json (by_page 付き) の方で、
# gsc_weekly.json は存在しない。初回 run 29523140030 はこれを踏んで
# --top-urls 10 を渡しているのに origin 1 件しか計測していなかった。
# 明示パスが無ければ gsc_history の最新週に自動フォールバックする。
DEFAULT_GSC_INPUT = "data/analytics/gsc_weekly.json"
GSC_HISTORY_DIR = "data/analytics/gsc_history"
DEFAULT_ORIGIN = "https://navi.omcha.jp"
DEFAULT_RUNS = 3
DEFAULT_TOP_URLS = 5
LIGHTHOUSE_TIMEOUT = 300

# Lighthouse audit id → JSONL 列名 prefix。numericValue (ms / 無次元) を採る。
METRIC_MAP = (
    ("largest-contentful-paint", "lcp"),
    ("first-contentful-paint", "fcp"),
    ("cumulative-layout-shift", "cls"),
    ("total-blocking-time", "tbt"),
    ("speed-index", "si"),
)

# CWV / Lighthouse の "good" 閾値。これをまたぐ悪化を第一級の劣化として扱う。
GOOD_THRESHOLDS = {
    "lcp": 2500.0,
    "fcp": 1800.0,
    "cls": 0.1,
    "tbt": 200.0,
    "si": 3400.0,
}

# baseline 比でこれ以上悪化したら鳴らす (相対)。
REGRESSION_RATIO = 1.25
# 相対悪化でも、この絶対差未満ならノイズとして無視する (metric 単位)。
MIN_ABS_DELTA = {
    "lcp": 300.0,
    "fcp": 200.0,
    "cls": 0.02,
    "tbt": 50.0,
    "si": 300.0,
}
# perf score がこれ以上落ちたら鳴らす (0-100 換算)。
SCORE_DROP_POINTS = 5.0


def build_lighthouse_argv(
    cmd: str, url: str, out_path: str, form_factor: str
) -> List[str]:
    """Lighthouse CLI の argv を組み立てる。

    `cmd` は "npx lighthouse" のように空白区切りでも渡せる (runner 側の都合で
    npx / グローバル install / 絶対パスが混在しうるため)。
    """
    argv = cmd.split()
    argv += [
        url,
        "--only-categories=performance",
        "--output=json",
        "--output-path=" + out_path,
        "--quiet",
        "--chrome-flags=--headless=new --no-sandbox --disable-dev-shm-usage",
    ]
    if form_factor == "mobile":
        # Lighthouse の既定 (Moto G Power / 低速4G) = PSI mobile と揃う
        argv += ["--form-factor=mobile", "--screenEmulation.mobile"]
    else:
        argv += [
            "--form-factor=desktop",
            "--screenEmulation.disabled",
            "--throttling.rttMs=40",
            "--throttling.throughputKbps=10240",
            "--throttling.cpuSlowdownMultiplier=1",
        ]
    return argv


def extract_metrics(lh: Dict[str, Any]) -> Dict[str, Any]:
    """Lighthouse JSON から metric 値と *_error を flat dict に抽出する。

    値と error を明確に分けるのが肝 (module docstring の設計判断を参照)。
    audit が errorMessage を持つ場合 (例: NO_LCP) は値を None にして
    `<short>_error` にメッセージを入れる。
    """
    out: Dict[str, Any] = {}

    runtime_error = (lh.get("runtimeError") or {}).get("code")
    if runtime_error and runtime_error != "NO_ERROR":
        out["runtime_error"] = runtime_error

    categories = lh.get("categories") or {}
    perf = (categories.get("performance") or {}).get("score")
    # Lighthouse の score は 0-1 (取得不能なら null)。0-100 に正規化して持つ。
    out["perf_score"] = round(perf * 100, 1) if isinstance(perf, (int, float)) else None

    audits = lh.get("audits") or {}
    for audit_id, short in METRIC_MAP:
        audit = audits.get(audit_id) or {}
        err = audit.get("errorMessage")
        value = audit.get("numericValue")
        if err:
            out[short] = None
            out[short + "_error"] = err
        elif isinstance(value, (int, float)):
            # ms 系は整数化しても十分 (CLS だけ小数を保つ)
            out[short] = round(value, 3) if short == "cls" else round(value, 1)
        else:
            out[short] = None
    return out


def run_lighthouse_once(
    url: str, cmd: str, form_factor: str, timeout: int = LIGHTHOUSE_TIMEOUT
) -> Optional[Dict[str, Any]]:
    """Lighthouse を 1 回実行して metric dict を返す。失敗したら None。

    Lighthouse は teardown で非ゼロを返すことがある一方で JSON は書けている、
    という挙動があるので returncode ではなく「JSON が読めたか」で成否を決める。
    """
    tmpdir = tempfile.mkdtemp(prefix="lh-")
    out_path = os.path.join(tmpdir, "lh.json")
    argv = build_lighthouse_argv(cmd, url, out_path, form_factor)
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout
        )
        if not os.path.exists(out_path):
            logger.warning(
                "lighthouse produced no report for %s (rc=%s): %s",
                url, proc.returncode, (proc.stderr or "")[-300:],
            )
            return None
        with open(out_path, encoding="utf-8") as f:
            lh = json.load(f)
        return extract_metrics(lh)
    except subprocess.TimeoutExpired:
        logger.warning("lighthouse timed out (%ss) for %s", timeout, url)
        return None
    except (OSError, ValueError) as e:
        logger.warning("lighthouse failed for %s: %s", url, e)
        return None
    finally:
        try:
            if os.path.exists(out_path):
                os.remove(out_path)
            os.rmdir(tmpdir)
        except OSError:
            pass


def aggregate_runs(runs: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """複数 run を median に畳む。

    - 数値がある run だけを median の母数にする (error/None は除外)。
    - error は「1 回でも出たら」記録する。全 run が error なら値は None のまま。
    - `runs` が空なら空 dict。
    """
    if not runs:
        return {}
    out: Dict[str, Any] = {"runs": len(runs)}

    scores = [r["perf_score"] for r in runs if isinstance(r.get("perf_score"), (int, float))]
    out["perf_score"] = round(statistics.median(scores), 1) if scores else None

    runtime_errors = [r["runtime_error"] for r in runs if r.get("runtime_error")]
    if runtime_errors:
        out["runtime_error"] = runtime_errors[0]

    for _, short in METRIC_MAP:
        values = [r[short] for r in runs if isinstance(r.get(short), (int, float))]
        if values:
            med = statistics.median(values)
            out[short] = round(med, 3) if short == "cls" else round(med, 1)
        else:
            out[short] = None
        errors = [r[short + "_error"] for r in runs if r.get(short + "_error")]
        if errors:
            # 何回中何回 error だったかは誤検出の切り分けに効くので残す
            out[short + "_error"] = errors[0]
            out[short + "_error_runs"] = len(errors)
    return out


def load_history(history_path: pathlib.Path) -> List[Dict[str, Any]]:
    if not history_path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    with history_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                logger.warning("skipping malformed history line")
    return rows


def baseline_for(
    history: List[Dict[str, Any]], url: str, form_factor: str, short: str, window: int
) -> Optional[float]:
    """直近 `window` 件の履歴から baseline (median) を作る。値のない行は無視。"""
    values: List[float] = []
    for row in reversed(history):
        if row.get("url") != url or row.get("form_factor") != form_factor:
            continue
        v = row.get(short)
        if isinstance(v, (int, float)):
            values.append(float(v))
        if len(values) >= window:
            break
    if not values:
        return None
    return statistics.median(values)


def detect_regressions(
    history: List[Dict[str, Any]], current: List[Dict[str, Any]], window: int = 5
) -> List[Dict[str, Any]]:
    """current の各行を履歴 baseline と比べて劣化を列挙する。

    鳴らす条件:
      1. audit error (NO_LCP など) が出た                     → kind="error"
      2. CWV good 閾値を「跨いで」悪化した                     → kind="threshold"
      3. baseline 比 REGRESSION_RATIO 以上 かつ 絶対差が十分   → kind="relative"
      4. perf score が SCORE_DROP_POINTS 以上落ちた            → kind="score"
    """
    alerts: List[Dict[str, Any]] = []
    for row in current:
        url = row.get("url")
        ff = row.get("form_factor")

        if row.get("runtime_error"):
            alerts.append({
                "kind": "error", "url": url, "form_factor": ff, "metric": "runtime",
                "detail": "runtime error: {}".format(row["runtime_error"]),
            })

        for _, short in METRIC_MAP:
            err = row.get(short + "_error")
            if err:
                alerts.append({
                    "kind": "error", "url": url, "form_factor": ff, "metric": short,
                    "detail": "audit error: {} ({}/{} runs)".format(
                        err, row.get(short + "_error_runs", 1), row.get("runs", 1)
                    ),
                })
                continue

            value = row.get(short)
            if not isinstance(value, (int, float)):
                continue
            base = baseline_for(history, url, ff, short, window)
            good = GOOD_THRESHOLDS.get(short)

            if base is None:
                # 初回計測: baseline が無いので閾値超えだけ拾う
                if good is not None and value > good:
                    alerts.append({
                        "kind": "threshold", "url": url, "form_factor": ff,
                        "metric": short, "value": value, "baseline": None,
                        "detail": "{} = {} (CWV good 閾値 {} 超・初回計測)".format(short, value, good),
                    })
                continue

            min_delta = MIN_ABS_DELTA.get(short, 0.0)
            crossed = good is not None and base <= good < value
            worse = value > base * REGRESSION_RATIO and (value - base) >= min_delta

            if crossed:
                alerts.append({
                    "kind": "threshold", "url": url, "form_factor": ff, "metric": short,
                    "value": value, "baseline": base,
                    "detail": "{} が good 閾値 {} を跨いで悪化: {} → {}".format(short, good, base, value),
                })
            elif worse:
                alerts.append({
                    "kind": "relative", "url": url, "form_factor": ff, "metric": short,
                    "value": value, "baseline": base,
                    "detail": "{} が baseline 比 {:.2f}x 悪化: {} → {}".format(
                        short, value / base if base else 0, base, value
                    ),
                })

        score = row.get("perf_score")
        if isinstance(score, (int, float)):
            base_score = baseline_for(history, url, ff, "perf_score", window)
            if base_score is not None and (base_score - score) >= SCORE_DROP_POINTS:
                alerts.append({
                    "kind": "score", "url": url, "form_factor": ff, "metric": "perf_score",
                    "value": score, "baseline": base_score,
                    "detail": "performance score が {} → {} に低下".format(base_score, score),
                })
    return alerts


def render_report(alerts: List[Dict[str, Any]], target_date: str) -> str:
    """劣化レポート Markdown。1 issue に集約する前提で全 URL 分を 1 本にまとめる。"""
    lines = [
        "## Lighthouse ラボ計測 劣化検出 ({})".format(target_date),
        "",
        "self-hosted Lighthouse レーン (#2995 案6) が回帰を検出しました。",
        "各行は {} 回実行の median です。".format(DEFAULT_RUNS),
        "",
    ]
    errors = [a for a in alerts if a["kind"] == "error"]
    perf = [a for a in alerts if a["kind"] != "error"]

    if errors:
        lines += [
            "### 計測エラー (値が取れていない)",
            "",
            "計測基盤側の失敗の可能性があります (2026-07-16 の PSI `NO_LCP` 相当)。",
            "サイトの劣化と即断せず、まず再計測してください。",
            "",
            "| URL | form factor | metric | 詳細 |",
            "| --- | --- | --- | --- |",
        ]
        for a in errors:
            lines.append("| {} | {} | {} | {} |".format(
                a["url"], a["form_factor"], a["metric"], a["detail"]))
        lines.append("")

    if perf:
        lines += [
            "### パフォーマンス劣化",
            "",
            "| URL | form factor | metric | baseline | 今回 | 判定 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for a in perf:
            base = a.get("baseline")
            lines.append("| {} | {} | {} | {} | {} | {} |".format(
                a["url"], a["form_factor"], a["metric"],
                "-" if base is None else base, a.get("value", "-"), a["kind"]))
        lines.append("")

    lines += ["Refs #2995 (案6) / #1357 (epic E2)"]
    return "\n".join(lines)


def latest_gsc_history() -> Optional[pathlib.Path]:
    """gsc_history/ の最新週の JSON を返す (無ければ None)。

    ファイル名が ISO 週 (2026-W28.json) なので、名前順 = 時系列順になる。
    """
    d = pathlib.Path(GSC_HISTORY_DIR)
    if not d.is_dir():
        return None
    files = sorted(d.glob("*.json"))
    return files[-1] if files else None


def get_targets(
    origin: str, gsc_path: pathlib.Path, top_urls: int, extra: Sequence[str]
) -> List[str]:
    """計測対象 URL を決める。

    origin (ホーム) + GSC 上位 URL + 明示指定分。ページ種別の代表性より
    「実際に流入がある URL の劣化を早く知る」ことを優先する。
    """
    targets: List[str] = [origin.rstrip("/") + "/"]

    if not gsc_path.exists():
        latest = latest_gsc_history()
        if latest is not None:
            logger.info("gsc input %s not found — falling back to %s", gsc_path, latest)
            gsc_path = latest

    if gsc_path.exists():
        try:
            data = json.loads(gsc_path.read_text(encoding="utf-8"))
            pages = data.get("by_page", []) or []
            pages = sorted(pages, key=lambda r: r.get("impressions", 0), reverse=True)
            for p in pages:
                path = p.get("page")
                if not path:
                    continue
                url = path if path.startswith("http") else origin.rstrip("/") + "/" + path.lstrip("/")
                if url not in targets:
                    targets.append(url)
                if len(targets) > top_urls:
                    break
        except (OSError, ValueError) as e:
            logger.warning("failed to read %s: %s — origin only", gsc_path, e)
    else:
        logger.info("gsc input %s not found — origin + --url only", gsc_path)

    for url in extra:
        if url not in targets:
            targets.append(url)
    return targets


def append_records(
    history_path: pathlib.Path, records: List[Dict[str, Any]]
) -> int:
    if not records:
        return 0
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with history_path.open("a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False, sort_keys=True))
            f.write("\n")
    return len(records)


def load_seen(seen_path: pathlib.Path) -> Dict[str, Any]:
    if not seen_path.exists():
        return {}
    try:
        return json.loads(seen_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_seen(seen_path: pathlib.Path, seen: Dict[str, Any]) -> None:
    seen_path.parent.mkdir(parents=True, exist_ok=True)
    seen_path.write_text(
        json.dumps(seen, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description="self-hosted Lighthouse CWV lane (#2995 案6)")
    p.add_argument("--origin", default=os.environ.get("LH_ORIGIN", DEFAULT_ORIGIN))
    p.add_argument("--url", action="append", default=[],
                   help="追加で計測する URL (複数可)")
    p.add_argument("--gsc-input", default=DEFAULT_GSC_INPUT)
    p.add_argument("--top-urls", type=int, default=DEFAULT_TOP_URLS)
    p.add_argument("--runs", type=int, default=DEFAULT_RUNS,
                   help="URL ごとの実行回数 (median を採る)")
    p.add_argument("--form-factors", nargs="+", default=["mobile"],
                   choices=["mobile", "desktop"])
    p.add_argument("--lighthouse-cmd",
                   default=os.environ.get("LIGHTHOUSE_CMD", "npx --yes lighthouse"))
    p.add_argument("--history-dir", default=DEFAULT_HISTORY_DIR)
    p.add_argument("--target-date", default=date.today().isoformat())
    p.add_argument("--baseline-window", type=int, default=5)
    p.add_argument("--report-out", default=None,
                   help="劣化があったとき Markdown を書き出すパス")
    p.add_argument("--dry-run", action="store_true",
                   help="JSONL に書かず結果を stdout に出すだけ")
    args = p.parse_args(argv)

    history_dir = pathlib.Path(args.history_dir)
    history_path = history_dir / LIGHTHOUSE_HISTORY_FILENAME
    seen_path = history_dir / SEEN_DATES_FILENAME

    targets = get_targets(
        args.origin, pathlib.Path(args.gsc_input), args.top_urls, args.url
    )
    logger.info("targets: %d URL x %s x %d runs",
                len(targets), args.form_factors, args.runs)

    history = load_history(history_path)
    current: List[Dict[str, Any]] = []

    for url in targets:
        for ff in args.form_factors:
            runs: List[Dict[str, Any]] = []
            for i in range(args.runs):
                logger.info("lighthouse %s (%s) run %d/%d", url, ff, i + 1, args.runs)
                r = run_lighthouse_once(url, args.lighthouse_cmd, ff)
                if r is not None:
                    runs.append(r)
            agg = aggregate_runs(runs)
            if not agg:
                logger.warning("all runs failed for %s (%s) — skipping row", url, ff)
                continue
            current.append({
                "date": args.target_date, "url": url, "form_factor": ff, **agg,
            })

    if not current:
        logger.error("no successful measurements — leaving history untouched")
        return 1

    alerts = detect_regressions(history, current, window=args.baseline_window)

    if args.dry_run:
        for row in current:
            print(json.dumps(row, ensure_ascii=False, sort_keys=True))
        for a in alerts:
            print("ALERT: {}".format(a["detail"]))
        return 0

    n = append_records(history_path, current)
    logger.info("appended %d rows to %s", n, history_path)

    seen = load_seen(seen_path)
    seen.setdefault("lighthouse", {})[args.target_date] = True
    meta = seen.get("_meta") or {}
    meta["lighthouse_last_run_utc"] = datetime.now(timezone.utc).isoformat()
    seen["_meta"] = meta
    save_seen(seen_path, seen)

    if alerts:
        logger.warning("%d regression alert(s) detected", len(alerts))
        report = render_report(alerts, args.target_date)
        if args.report_out:
            pathlib.Path(args.report_out).write_text(report, encoding="utf-8")
            logger.info("wrote report to %s", args.report_out)
        else:
            print(report)
    else:
        logger.info("no regressions detected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
