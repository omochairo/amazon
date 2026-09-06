#!/usr/bin/env python3
"""agy 出力 → gemma snippet の **下流歩留まり** を測る。

`bench_agy_model.py` が測れなかった残りの半分。あちらは agy の応答本文だけを
採点していて、「その本文が最終的に何本の snippet になったか」は測っていない
(docs/ANTIGRAVITY_MODEL_BENCH.md 「測っていないもの」)。理由は gemma
(`extract_snippets`) が K8 ワーカーにしか無かったから。

**これは経路の穴であって、ハード制約ではなかった。** K8 の Ollama は WSL2 の
NAT 越しで LAN には出ていないが、K8 への ssh は通る。ssh のローカル
ポートフォワードを 1 本張れば母艦から /api/generate に届く
(--ollama-url に渡すだけ。mine_experience 側の変更は不要):

    ssh -N -L 21434:localhost:11434 -i <key> <user>@<k8-host>
    python3 -m scripts.bench_snippet_yield --bench /tmp/bench.json \
        --ollama-url http://localhost:21434

なぜ variant 別に測るのか:
  上流スコア (grounding/balance/...) は「素材として良さそうか」の代理指標で
  しかない。記事に載るのは snippet なので、**代理指標が高い variant が本当に
  snippet を多く通すかは別の問題**。上流1位が下流で負ける可能性を潰す。

測る指標 (記録側に決定的に出る量だけ):
  entail_rate   gemma が entailed=true を返した割合 (通らないと 0 本)
  snippets_mean 1 応答あたりの採用 snippet 本数 (上流の落ちも 0 本で平均に入れる)
  yield_per_ok  agy が応答した回だけで割った本数 (上流の落ちを除いた変換率)
  aspects       aspect の分布 (素材の偏り。注意点が落ちていないかを見る)

入力は bench_agy_model.py の --out がそのまま使える (records[].text を読む)。
本文は保存済みなので **agy を叩き直さずに** 何度でも採り直せる。
gemma 側の応答も --out に残すので、集計を直したら --report で読み直せる。
"""

from __future__ import annotations

import argparse
import collections
import json
import logging
import pathlib
import statistics
import sys
import time

import requests

try:  # package 実行と素実行の両対応 (bench_agy_model.py と同じ流儀)
    from scripts import mine_experience
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    from scripts import mine_experience

logger = logging.getLogger("bench_snippet_yield")

DEFAULT_OLLAMA_URL = "http://localhost:21434"
DEFAULT_MODEL = "gemma4:26b-a4b-it-qat"
DEFAULT_OUT = pathlib.Path("/tmp/snippet_yield.json")


def build_candidate(text: str) -> dict:
    """gather_antigravity が gemma に渡すのと同じ形の candidate を作る。

    ここを本番と揃えないと歩留まりの数字が本番と別物になる
    (URL 除去と長さ切り詰めは入力量そのものを変えるので特に効く)。
    """
    return {
        "text": mine_experience.strip_urls(text).strip()[: mine_experience.MAX_CANDIDATE_TEXT_LEN],
        "source_type": "antigravity",
        "source_url": "",
        "source_urls": [],
    }


def run_yield(
    records: list[dict], ollama_url: str, model: str, out_path: pathlib.Path,
) -> list[dict]:
    session = requests.Session()
    out: list[dict] = []
    n_ok = sum(1 for r in records if r.get("ok") and r.get("text"))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info(
        "%d records 中 %d 件が上流 ok (落ちた %d 件は 0 本として集計に残す)",
        len(records), n_ok, len(records) - n_ok,
    )

    for i, r in enumerate(records, 1):
        base = {
            "variant": r.get("variant"),
            "trial": r.get("trial"),
            "asin": r.get("asin"),
            "product_name": r.get("product_name"),
            "brand": r.get("brand"),
            "upstream_ok": bool(r.get("ok")),
            "upstream_score": r.get("score"),
        }
        if not (r.get("ok") and r.get("text")):
            # 上流が落ちた回。gemma は呼ばないが、歩留まりの分母には残す
            out.append({**base, "snippets": [], "n_snippets": 0, "latency_s": 0.0})
            continue

        cand = build_candidate(r["text"])
        t0 = time.time()
        snippets = mine_experience.extract_snippets(
            cand, r.get("product_name", ""), r.get("brand", ""),
            ollama_url=ollama_url, model=model, session=session,
        )
        dt = round(time.time() - t0, 2)
        logger.info(
            "[%d/%d] variant=%s asin=%s -> %d snippets (%.1fs)",
            i, len(records), r.get("variant"), r.get("asin"), len(snippets), dt,
        )
        out.append({
            **base,
            "candidate_chars": len(cand["text"]),
            "snippets": snippets,
            "n_snippets": len(snippets),
            "latency_s": dt,
        })
        # 逐次書き出し: 1 件あたり数十秒かかるので中断しても集計できるようにする
        out_path.write_text(
            json.dumps({"records": out, "model": model}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
    return out


def summarize(records: list[dict]) -> list[dict]:
    by: dict[str, list[dict]] = {}
    for r in records:
        by.setdefault(r.get("variant") or "(default)", []).append(r)

    rows = []
    for variant, rs in by.items():
        oks = [r for r in rs if r.get("upstream_ok")]
        counts = [r["n_snippets"] for r in rs]
        aspects: collections.Counter = collections.Counter()
        for r in rs:
            for s in r.get("snippets", []):
                aspects[s.get("aspect", "?")] += 1
        lat = [r["latency_s"] for r in oks if r.get("latency_s")]
        rows.append({
            "variant": variant,
            "n": len(rs),
            "upstream_ok_rate": round(len(oks) / len(rs), 3) if rs else 0.0,
            # entailed を通った (= 1 本以上出た) 割合。gemma 側の門の狭さ
            "entail_rate": round(
                sum(1 for r in oks if r["n_snippets"]) / len(oks), 3) if oks else 0.0,
            # 落ちた回も 0 本で入れた、レーン全体としての本数
            "snippets_mean": round(statistics.fmean(counts), 3) if counts else 0.0,
            "snippets_sd": round(statistics.pstdev(counts), 3) if len(counts) > 1 else 0.0,
            # 上流の落ちを除いた純粋な変換率
            "yield_per_ok": round(
                statistics.fmean([r["n_snippets"] for r in oks]), 3) if oks else 0.0,
            "chars_mean": round(statistics.fmean(
                [r.get("candidate_chars", 0) for r in oks]), 1) if oks else 0.0,
            "gemma_p50_s": round(statistics.median(lat), 1) if lat else 0.0,
            "aspects": dict(aspects.most_common()),
        })
    # レーン全体の本数で並べる (上流の落ちを含めた実力)
    rows.sort(key=lambda r: -r["snippets_mean"])
    return rows


def format_report(rows: list[dict]) -> str:
    head = (
        f"{'variant':24} {'n':>3} {'up_ok':>6} {'entail':>7} "
        f"{'snip/all':>9} {'sd':>6} {'snip/ok':>8} {'chars':>6} {'p50s':>6}"
    )
    lines = [head, "-" * len(head)]
    for r in rows:
        lines.append(
            f"{r['variant']:24} {r['n']:>3} {r['upstream_ok_rate']:>6.2f} "
            f"{r['entail_rate']:>7.2f} {r['snippets_mean']:>9.2f} {r['snippets_sd']:>6.2f} "
            f"{r['yield_per_ok']:>8.2f} {r['chars_mean']:>6.0f} {r['gemma_p50_s']:>6.1f}"
        )
    lines.append("")
    lines.append("aspect 分布 (素材の偏り — 注意点が落ちていないか):")
    for r in rows:
        lines.append(f"  {r['variant']:24} {r['aspects']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bench", type=pathlib.Path,
                    help="bench_agy_model.py の --out (records[].text を読む)")
    ap.add_argument("--report", type=pathlib.Path, default=None,
                    help="このスクリプトの --out を読み直して集計だけする")
    ap.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL,
                    help="ssh -L で母艦に転送した K8 の Ollama (既定: %(default)s)")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--out", type=pathlib.Path, default=DEFAULT_OUT)
    ap.add_argument("--limit", type=int, default=0, help="先頭 N 件だけ (疎通確認用)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # 母艦は Windows で、既定の標準出力は cp932。レポートに全角ダッシュや
    # snippet 本文が乗ると UnicodeEncodeError で落ちる (実測)。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover — 非 Windows / 非 TTY
            pass

    if args.report:
        data = json.loads(args.report.read_text(encoding="utf-8"))
        print(format_report(summarize(data["records"])))
        return 0

    if not args.bench:
        ap.error("--bench か --report のどちらかが要る")

    data = json.loads(args.bench.read_text(encoding="utf-8"))
    records = data["records"]
    if args.limit:
        records = records[: args.limit]

    out = run_yield(records, args.ollama_url, args.model, args.out)
    print()
    print(format_report(summarize(out)))
    print(f"\n生応答: {args.out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
