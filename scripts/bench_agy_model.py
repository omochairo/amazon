#!/usr/bin/env python3
"""agy (Antigravity CLI) のモデル選定ベンチ。

対象は **mine_experience.gather_antigravity** — 現状 agy を使っている処理のうち、
Gemini に乗っている唯一のレーン。

  - 23-experience-mining.yml  gather_antigravity  -> agy 既定モデル (= Gemini)  ← ここ
  - 29-sns-reply-inbox.yml    draft_sns_reply     -> AGY_MODEL=claude-sonnet-4-6 (Claude なので対象外)
  - generate_faq_seo.py       agy は「使わない」と owner 確定済み (対象外)

なぜ要るか:
  gather_antigravity は `agy --print` を --model 無しで叩いていた。既定モデルは
  agy のバージョン更新で黙って動き、`--output-format json` の応答にモデル名は
  入らないので **本番からは何に乗っているか観測できない**。新モデル
  (gemini-3.8-flash) が出た今、明示ピンするならどれが最適かを実測で決める。

測り方:
  variant (= --model の値。空文字は「現状 = --model 無し」) × product × trial で
  実際に agy を叩き、決定的な指標だけでスコアリングする。gemma judge
  (extract_snippets) は K8 側にしか無いので下流歩留まりはここでは測らない
  — 測れないものを測ったことにしない。

スコア (ok を落ちた trial は 0 点):
  grounding  0.35  商品名/ブランドの語がいくつ本文に出るか (Web 検索が効いた証跡)
  balance    0.20  注意点・不満に触れているか (賞賛のみは素材として弱い)
  no_refusal 0.20  「見つかりませんでした」等の逃げ文句が無いか
  format     0.15  箇条書き 3〜5 行 (プロンプトの指示)
  japanese   0.10  日本語で返っているか

  本文は記録に残してあるので、ルーブリックを直したら --report で採り直さずに
  採点し直せる (rescore)。variant 間は必ず同じ物差しで比べること。

使い方:
  python3 -m scripts.bench_agy_model --trials 3 --products 5 \
      --variants "" gemini-3.8-flash-medium gemini-3.8-flash-high gemini-3.1-pro-high \
      --out data/analytics/agy_model_bench.json

  # 途中結果は --out に逐次書くので、中断しても再開せずそのまま集計できる。
  python3 -m scripts.bench_agy_model --report data/analytics/agy_model_bench.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import re
import shutil
import statistics
import subprocess
import sys
import time

try:  # package 実行 (`python3 -m scripts.bench_agy_model`) と素実行の両対応
    from scripts import mine_experience
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    from scripts import mine_experience

logger = logging.getLogger("bench_agy_model")

DEFAULT_VARIANTS = [
    "",  # 現状: --model を渡さない (agy CLI 既定)
    "gemini-3.8-flash-low",
    "gemini-3.8-flash-medium",
    "gemini-3.8-flash-high",
    "gemini-3.1-pro-low",
    "gemini-3.1-pro-high",
]
DEFAULT_OUT = pathlib.Path("data/analytics/agy_model_bench.json")
CALL_TIMEOUT_S = mine_experience.ANTIGRAVITY_TIMEOUT_S

# 「調べたが分からなかった」を丁寧に言い換えただけの応答。Web 検索が効いて
# いない/ハルシネーションを避けて逃げた場合にここに落ちる。
_REFUSAL_MARKERS = [
    "見つかりませんでした",
    "見つかりません",
    "確認できませんでした",
    "確認できません",
    "情報がありません",
    "情報は見つ",
    "検索できません",
    "アクセスできません",
    "特定できませんでした",
    "該当する商品",
    "申し訳あり",
    "一般的な情報",
    "推測",
]
_BULLET_RE = re.compile(r"^\s*(?:[-*・•‣]|\d+[.)]|\*\*)\s*\S")
_URL_RE = re.compile(r"https?://\S+")
_JA_RE = re.compile(r"[ぁ-んァ-ヴ一-龥]")
# 商品名を語に割る。長音符はカタカナ語の一部なので明示的に含める
# (\p{Katakana} 相当のクラスは ー を取りこぼす — feedback-regex-katakana-script-prolonged-mark)。
_TOKEN_RE = re.compile(r"[A-Za-z0-9]{3,}|[ァ-ヴー]{3,}|[一-龥]{2,}")

# balance は **初回実測の後で足した**。当初の 4 指標だとどの variant も 0.90〜0.97 に
# 張り付いて選別できず (ゲートが分布の中央から外れていた)、本文を目視して
# 「3.1-pro は賞賛のみで注意点を落とす」という、このレーンにとって決定的な差が
# 指標に載っていないことが分かったため。体験談マイニングは注意点・不満こそが
# 記事素材の価値なので (#3203 凡庸化)、後付けだが正当な軸として重み付けする。
# 追加前のランキングは docs/ANTIGRAVITY_MODEL_BENCH.md に併記して監査可能にしてある。
WEIGHTS = {
    "grounding": 0.35,
    "balance": 0.20,
    "no_refusal": 0.20,
    "format": 0.15,
    "japanese": 0.10,
}

# --- 以下は重み付けしない「診断」指標 -------------------------------------
# 実測すると 4 つの加点指標はどの variant も天井近くに張り付き、score だけでは
# 選別できない (= ゲートが効いていない)。差が出る軸を別に見るための計器。
# ここを score に混ぜて無理に差をつけない — 差が無いという事実の方が結論。
_HEDGE_MARKERS = [
    "かもしれません", "ようです", "と思われます", "と考えられます",
    "でしょう", "可能性があります", "とされています",
]
# Web 検索が本当に効いていれば出所の媒体名が出る。paraphrase 素材としての
# 追跡可能性に効くので、品質そのものとは別に見る。
_SOURCE_MARKERS = [
    "楽天", "Amazon", "アマゾン", "Yahoo", "ヤフー", "レビュー", "口コミサイト",
    "ブログ", "SNS", "Instagram", "X(", "投稿",
]
# 賞賛だけの要約は記事素材として弱い。navi の記事が凡庸になる主因はここで、
# 「良い点しか書けない」素材しか入って来ないと下流でどうにもならない (#3203)。
# 注意点・不満に触れているかを別軸で数える。
_CAVEAT_MARKERS = [
    "難し", "注意", "デメリット", "欠点", "不満", "惜し", "残念", "破損",
    "壊れ", "苦戦", "サポートが必要", "手助け", "気になる", "という指摘",
    "取り合い", "物足り", "高価", "値段が高", "価格が高",
]


# --------------------------------------------------------------------------
# 1 回分の呼び出し
# --------------------------------------------------------------------------

def call_agy_once(prompt: str, model: str, timeout_s: int = CALL_TIMEOUT_S) -> dict:
    """agy を JSON 出力で 1 回叩く。

    本番 (gather_antigravity) は text 出力だが、bench では status と
    duration_seconds / usage を取りたいので `--output-format json` を足す。
    全 variant に同じ形で足すので比較の公平性は損なわれない。
    """
    argv = mine_experience.build_antigravity_argv(prompt, model or None)
    argv = [argv[0], "--output-format", "json", *argv[1:]]
    if shutil.which("dbus-run-session"):  # Linux (K8 worker) / 無い Windows は素で叩く
        argv = ["dbus-run-session", "--", *argv]

    started = time.monotonic()
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_s, encoding="utf-8",
        )
    except FileNotFoundError:
        return {"ok": False, "error": "agy_not_found", "latency_s": 0.0, "text": ""}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout", "latency_s": float(timeout_s), "text": ""}
    latency = time.monotonic() - started

    if result.returncode != 0:
        return {
            "ok": False, "error": f"rc={result.returncode}",
            "stderr": (result.stderr or "")[:300], "latency_s": latency, "text": "",
        }

    raw = (result.stdout or "").strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {"ok": False, "error": "bad_json", "latency_s": latency, "text": raw[:500]}

    text = (payload.get("response") or "").strip()
    if payload.get("status") != "SUCCESS" or not text:
        return {
            "ok": False, "error": f"status={payload.get('status')}",
            "latency_s": latency, "text": text[:500],
        }

    usage = payload.get("usage") or {}
    return {
        "ok": True,
        "text": text,
        "latency_s": latency,
        "agy_duration_s": payload.get("duration_seconds"),
        "num_turns": payload.get("num_turns"),
        "output_tokens": usage.get("output_tokens"),
        "total_tokens": usage.get("total_tokens"),
    }


def call_agy_json(
    prompt: str, model: str, timeout_s: int = CALL_TIMEOUT_S, retries: int = 0,
) -> dict:
    """空応答だけリトライして叩く (本番 gather_antigravity と同じ方針)。

    retries=0 なら 1 コールぶんの素の成功率が測れる。本番の挙動で順位を出したい
    ときは retries を本番の _MAX_EXTRA_RETRIES に合わせる。**両方測ること** —
    素の成功率だけ見ると「落ちにくいモデル」が勝ち、リトライ後だけ見ると
    「当たれば良いモデル」が勝つので、混ぜると何を選んだのか分からなくなる。

    latency はリトライぶんを積算して返す (それが本番で実際にかかる時間)。
    """
    attempts_used = 0
    total_latency = 0.0
    errors: list[str] = []
    res: dict = {}
    for attempt in range(retries + 1):
        res = call_agy_once(prompt, model, timeout_s)
        attempts_used += 1
        total_latency += res.get("latency_s") or 0.0
        if res["ok"]:
            break
        errors.append(str(res.get("error")))
        # 本番がリトライするのは空応答だけ。ここも合わせる
        if not str(res.get("error", "")).startswith("status="):
            break
    res = dict(res)
    res["latency_s"] = total_latency
    res["attempts"] = attempts_used
    res["attempt_errors"] = errors
    return res


# --------------------------------------------------------------------------
# 採点
# --------------------------------------------------------------------------

def product_tokens(product_name: str, brand: str) -> list[str]:
    seen: list[str] = []
    for tok in _TOKEN_RE.findall(f"{product_name} {brand}"):
        low = tok.lower()
        if low not in [s.lower() for s in seen]:
            seen.append(tok)
    return seen


def score_text(text: str, product_name: str, brand: str) -> dict:
    """決定的な指標だけで採点する。LLM judge は使わない (再現しないので)。"""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    bullets = [ln for ln in lines if _BULLET_RE.match(ln)]
    n = len(bullets)
    if 3 <= n <= 5:
        fmt = 1.0
    elif 2 <= n <= 7:
        fmt = 0.5
    else:
        fmt = 0.0

    # URL を除いてから日本語比率を測る。grounding redirect の URL は 1 本 300 字
    # 超あり、出典つきで返させると本文が日本語でも比率が 0.3 を割って「英語で
    # 返ってきた」と誤判定する (probe_agy_sources で踏んだ)。
    # 測りたいのは本文の言語であって URL の長さではない。
    prose = _URL_RE.sub("", text)
    ja_chars = len(_JA_RE.findall(prose))
    ja = 1.0 if prose.strip() and ja_chars / max(len(prose), 1) >= 0.30 else 0.0

    toks = product_tokens(product_name, brand)
    hits = [t for t in toks if t.lower() in text.lower()]
    grounding = (len(hits) / min(len(toks), 4)) if toks else 0.0
    grounding = min(grounding, 1.0)

    refusals = [m for m in _REFUSAL_MARKERS if m in text]
    no_refusal = 0.0 if refusals else 1.0

    caveats = sum(1 for m in _CAVEAT_MARKERS if m in text)
    balance = 1.0 if caveats else 0.0

    parts = {
        "grounding": grounding, "balance": balance,
        "no_refusal": no_refusal, "format": fmt, "japanese": ja,
    }
    total = sum(WEIGHTS[k] * v for k, v in parts.items())
    return {
        "score": round(total, 4),
        "parts": {k: round(v, 4) for k, v in parts.items()},
        "bullets": n,
        # URL を除いた本文長。出典つき variant と素の要約を同じ土俵で比べる
        "chars": len(prose.strip()),
        "token_hits": hits[:6],
        "refusal_markers": refusals,
        "diagnostics": {
            # 商品語の被覆率 (4 語で頭打ちにしない生の値)
            "grounding_full": round(len(hits) / len(toks), 4) if toks else 0.0,
            # 断定を避けた表現の数。多い = 検索で裏が取れていない疑い
            "hedges": sum(text.count(m) for m in _HEDGE_MARKERS),
            # 出所の媒体名が本文に出た数
            "sources": sum(1 for m in _SOURCE_MARKERS if m in text),
            # 注意点・不満に触れた語の種類数。0 なら賞賛のみ = 素材として弱い
            "caveats": caveats,
        },
    }


# --------------------------------------------------------------------------
# 実行
# --------------------------------------------------------------------------

def load_products(limit: int, asins: list[str] | None = None) -> list[dict]:
    """ベンチ用の商品を limit 件選ぶ。

    ノーブランド/舶来品は日本語 Web に口コミが無く、モデル差ではなく素材差で
    スコアが潰れるので除く (どのモデルでも 0 点になり選別にならない)。

    さらに **1 ブランド 1 件までに絞る**。per_asin は ASIN 昇順で同一ブランドが
    固まって並んでいるので、素直に先頭から取ると 5 件全部が同じシリーズになり、
    「そのシリーズでの強さ」しか測っていないのに全体の結論として書いてしまう。
    """
    src = asins or [d.name for d in sorted(mine_experience.PER_ASIN_DIR.iterdir()) if d.is_dir()]
    if asins:  # 明示指定は言われたとおりに使う (分散フィルタを掛けない)
        out = []
        for asin in src:
            _t, product_name, brand = mine_experience.resolve_product_identity(asin)
            if product_name and brand:
                out.append({"asin": asin, "product_name": product_name, "brand": brand})
        return out[:limit]

    seen_brands: set[str] = set()
    out: list[dict] = []
    for asin in src:
        _title, product_name, brand = mine_experience.resolve_product_identity(asin)
        if not product_name or not brand:
            continue
        if brand == "ノーブランド" or len(product_name) < 5 or brand in seen_brands:
            continue
        seen_brands.add(brand)
        out.append({"asin": asin, "product_name": product_name, "brand": brand})
        if len(out) >= limit:
            break
    return out


def run_bench(
    variants: list[str], products: list[dict], trials: int, out_path: pathlib.Path,
    retries: int = 0,
) -> list[dict]:
    records: list[dict] = []
    out_path.parent.mkdir(parents=True, exist_ok=True)
    total = len(variants) * len(products) * trials
    i = 0
    # trial を外側に回す。variant を外側にすると、途中で打ち切ったとき先頭
    # variant だけ全 trial 揃うという偏った比較になる。
    for trial in range(trials):
        for prod in products:
            prompt = mine_experience.build_antigravity_prompt(prod["product_name"], prod["brand"])
            for variant in variants:
                i += 1
                logger.info(
                    "[%d/%d] trial=%d variant=%s asin=%s",
                    i, total, trial, variant or "(default)", prod["asin"],
                )
                res = call_agy_json(prompt, variant, retries=retries)
                rec = {
                    "variant": variant or "(default)",
                    "model_flag": variant,
                    "trial": trial,
                    "asin": prod["asin"],
                    "product_name": prod["product_name"],
                    "brand": prod["brand"],
                    "ok": res["ok"],
                    "error": res.get("error"),
                    "latency_s": round(res.get("latency_s") or 0.0, 2),
                    "attempts": res.get("attempts", 1),
                    "attempt_errors": res.get("attempt_errors", []),
                    "output_tokens": res.get("output_tokens"),
                    "text": res.get("text", ""),
                }
                if res["ok"]:
                    rec.update(score_text(res["text"], prod["product_name"], prod["brand"]))
                else:
                    rec.update({"score": 0.0, "parts": {}, "bullets": 0, "chars": 0})
                records.append(rec)
                # 逐次書き出し: 長時間 run なので中断しても集計できるようにする
                out_path.write_text(
                    json.dumps({"records": records}, ensure_ascii=False, indent=1),
                    encoding="utf-8",
                )
    return records


def rescore(records: list[dict]) -> list[dict]:
    """保存済みの text を今のルーブリックで採点し直す。

    採点器を触るたびに 90 回 agy を叩き直すのは現実的でないし、
    variant 間の比較は同じ本文に同じ物差しを当てないと成立しない。
    """
    out = []
    for r in records:
        r = dict(r)
        if r.get("ok") and r.get("text"):
            r.update(score_text(r["text"], r["product_name"], r["brand"]))
        out.append(r)
    return out


def summarize(records: list[dict]) -> list[dict]:
    by: dict[str, list[dict]] = {}
    for r in records:
        by.setdefault(r["variant"], []).append(r)

    rows = []
    for variant, rs in by.items():
        oks = [r for r in rs if r["ok"]]
        scores = [r["score"] for r in rs]  # 失敗は 0 点として平均に入れる
        lat = sorted(r["latency_s"] for r in oks) or [0.0]
        rows.append({
            "variant": variant,
            "n": len(rs),
            "success_rate": round(len(oks) / len(rs), 3) if rs else 0.0,
            "score_mean": round(statistics.fmean(scores), 4) if scores else 0.0,
            "score_sd": round(statistics.pstdev(scores), 4) if len(scores) > 1 else 0.0,
            "score_min": round(min(scores), 4) if scores else 0.0,
            "grounding": round(
                statistics.fmean([r["parts"].get("grounding", 0.0) for r in oks]), 3
            ) if oks else 0.0,
            "no_refusal": round(
                statistics.fmean([r["parts"].get("no_refusal", 0.0) for r in oks]), 3
            ) if oks else 0.0,
            "format": round(
                statistics.fmean([r["parts"].get("format", 0.0) for r in oks]), 3
            ) if oks else 0.0,
            "latency_p50": lat[len(lat) // 2],
            "latency_p95": lat[max(0, int(len(lat) * 0.95) - 1)],
            "chars_mean": round(statistics.fmean([r["chars"] for r in oks]), 1) if oks else 0.0,
            "grounding_full": round(statistics.fmean(
                [r.get("diagnostics", {}).get("grounding_full", 0.0) for r in oks]), 3) if oks else 0.0,
            "hedges": round(statistics.fmean(
                [r.get("diagnostics", {}).get("hedges", 0) for r in oks]), 2) if oks else 0.0,
            "sources": round(statistics.fmean(
                [r.get("diagnostics", {}).get("sources", 0) for r in oks]), 2) if oks else 0.0,
            "caveats": round(statistics.fmean(
                [r.get("diagnostics", {}).get("caveats", 0) for r in oks]), 2) if oks else 0.0,
            # 賞賛のみだった応答の割合 (体験談素材としての弱さ)
            "praise_only_rate": round(sum(
                1 for r in oks if not r.get("diagnostics", {}).get("caveats")) / len(oks), 3) if oks else 0.0,
            "attempts": round(statistics.fmean([r.get("attempts", 1) for r in rs]), 2),
            "errors": sorted({r["error"] for r in rs if r.get("error")}),
        })
    # 平均スコア降順、同点はレイテンシ昇順
    rows.sort(key=lambda r: (-r["score_mean"], r["latency_p50"]))
    return rows


def format_report(rows: list[dict]) -> str:
    head = (
        f"{'variant':24} {'n':>3} {'ok':>5} {'score':>7} {'sd':>6} "
        f"{'grnd':>5} {'gfull':>6} {'cavs':>5} {'praise':>7} "
        f"{'p50s':>6} {'p95s':>6} {'chars':>6} {'att':>5}"
    )
    lines = [head, "-" * len(head)]
    for r in rows:
        lines.append(
            f"{r['variant']:24} {r['n']:>3} {r['success_rate']:>5.2f} "
            f"{r['score_mean']:>7.3f} {r['score_sd']:>6.3f} {r['grounding']:>5.2f} "
            f"{r['grounding_full']:>6.2f} {r['caveats']:>5.2f} {r['praise_only_rate']:>7.2f} "
            f"{r['latency_p50']:>6.1f} {r['latency_p95']:>6.1f} {r['chars_mean']:>6.0f} "
            f"{r['attempts']:>5.2f}"
        )
        if r["errors"]:
            lines.append(f"{'':24} errors: {', '.join(r['errors'])}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--variants", nargs="*", default=DEFAULT_VARIANTS,
                    help='--model に渡す値。"" は現状 (--model 無し)')
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--products", type=int, default=5)
    ap.add_argument("--retries", type=int, default=0,
                    help="空応答時の追加試行回数。本番と揃えるなら 2 (既定 0 = 素の成功率)")
    ap.add_argument("--asins", default="", help="カンマ区切りで商品を明示指定")
    ap.add_argument("--out", type=pathlib.Path, default=DEFAULT_OUT)
    ap.add_argument("--report", type=pathlib.Path, default=None,
                    help="既存の結果 JSON を集計するだけ (agy を叩かない)")
    # Windows のコンソールは既定 cp932 で、--help ですら U+2014 で落ちる。
    # parse_args より前に張り替える。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover — 差し替え済みの stream
            pass

    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.report:
        records = json.loads(args.report.read_text(encoding="utf-8"))["records"]
        # 本文を保存してあるので、ルーブリックを変えたら**採り直さずに**採点し直す。
        records = rescore(records)
    else:
        asins = [a.strip() for a in args.asins.split(",") if a.strip()] or None
        products = load_products(args.products, asins)
        if not products:
            logger.error("対象商品が 0 件")
            return 1
        logger.info("products=%s variants=%s trials=%d retries=%d",
                    [p["asin"] for p in products], args.variants, args.trials, args.retries)
        records = run_bench(args.variants, products, args.trials, args.out, args.retries)

    rows = summarize(records)
    print(format_report(rows))
    if rows:
        print(f"\nwinner: {rows[0]['variant']}  (score={rows[0]['score_mean']:.3f}, "
              f"ok={rows[0]['success_rate']:.2f}, p50={rows[0]['latency_p50']:.1f}s)")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    raise SystemExit(main())
