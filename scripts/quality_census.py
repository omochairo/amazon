"""quality_census.py

#4826 項目 3: main 全量に quality_gate を当てて「後から腐ったゲート」を検出する観察レーン。

なぜ必要か (2026-08-10 実測):
  ``quality_gate`` は 04-validate-article-pr.yml から **PR で変更されたファイルにしか**
  当たらない (mktemp した一時ディレクトリに変更分だけ cp して --src に渡す)。
  ところが ``check_how_to_choose`` が参照する ``data/raw/per_asin/<ASIN>/competitors.json``
  は日次で更新されるため、**マージ時に合格した記事が後から不合格に転じる**。
  main を再評価する経路が無いので、この腐りは誰にも見えない。

  実測: main 全量 1903 記事に当てると 10 件が不合格で、10/10 すべて how_to_choose。
  参照先はいずれも実在 ASIN で捏造ではなく、競合セットの入れ替わりで落ちたもの。

このレーンの位置づけ:
  **観察のみ。CI を落とさないし記事も直さない。** 週次で全量評価し、前回との差分
  (新規不合格 / 回復 / 継続) を tracker issue にコメントするところまで。
  「作業リスト」ではなく「進捗追跡」なので、件数は相対閾値で切らず絶対値で出す
  (feedback-metric-gate-calibration: 出力を上限で切ると機能不全が不可視になる)。

cert fetch を無効にしている理由 (owner 判断・2026-08-10):
  ``check_cert_sources_content`` は証明書ページへ実 HTTP fetch する。週次で 1903 記事分を
  舐めると外部サイトへの負荷が大きく、タイムアウト由来のフレークがそのまま「不合格」として
  観測に乗る (誤検出)。census は ``--no-cert-fetch`` 相当で走らせ、レポートに
  ``cert_fetch: false`` を明記して PR 時 CI との 1 チェック分の乖離を可視化する。

sidecar を書かない理由 (owner 判断・2026-08-10):
  ``<slug>.quality.json`` sidecar は廃止方針。本スクリプトは記事ディレクトリに
  派生ファイルを 1 つも作らず、集計 JSON 1 本 + history jsonl 1 行だけを出力する。

直近コホート (2026-08-20 追加):
  ``by_deduction`` は全量の集計しか出さないため、**施行日つきの soft→hard 昇格を
  判定できない**。昇格判定は #4826 項目2 の前例に従って「施行日以降の新規記事で
  発火 0」で行うが、施行直後は該当記事がコーパスの数 % しか無い。
  実測 (2026-08-20): #5083 の規約適用後コホートは 2109 本中 42 本 = 2.0% で、
  仮にコホートが全滅しても全量の発火率は 1pt しか動かず、効果と誤差が区別できない。

  そこで slug 順 (= 生成順) の直近 N 本を切ったコホート集計を併せて出す。
  詳細は ``COHORT_SIZES`` と ``cohort_summary`` を参照。

出力:
  - data/analytics/quality_census.json           単一スナップショット (最新 run のみ)
  - data/analytics/history/quality_census.jsonl  1 run 1 行の時系列 (append)

idempotency:
  append_census_history.py と同じく共有サイドカーを使わず、jsonl 自体を毎回スキャンして
  対象 date の行が既にあるかで判定する (年間 ~52 行なので全件スキャンで足りる)。
  既存行があれば既定では上書きせず skip する (--force で置換)。

副作用: 上記 2 ファイルの書き込みのみ。ネットワーク・Issue 操作は行わない
        (Issue へのサーフェシングは comment_quality_census.py の担当)。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
from typing import Any

_HERE = pathlib.Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import quality_gate as qg  # noqa: E402

DEFAULT_SRC = pathlib.Path("data/articles")
DEFAULT_SCHEMA = pathlib.Path("data/schema/article.schema.json")
DEFAULT_SNAPSHOT = pathlib.Path("data/analytics/quality_census.json")
DEFAULT_HISTORY = pathlib.Path("data/analytics/history/quality_census.jsonl")

# quality_gate.main と同じ除外。sidecar の種別追加時はここも更新すること
# (claude-traps-analytics-data.md: sidecar 除外は正準 3 種)。
SIDECAR_SUFFIXES = (".enrichment", ".seo", ".quality")


def iter_article_paths(src: pathlib.Path) -> list[pathlib.Path]:
    """記事本体 JSON のみを列挙する (sidecar 除外)。"""
    return sorted(
        p for p in src.glob("*.json")
        if not any(p.stem.endswith(s) for s in SIDECAR_SUFFIXES)
    )


def load_matched_indexes(
    rakuten_path: pathlib.Path = pathlib.Path("data/raw/rakuten_matched.json"),
    yahoo_path: pathlib.Path = pathlib.Path("data/raw/yahoo_matched.json"),
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """quality_gate.main と同じ楽天/Yahoo の matched index を読む。

    ``_derive_verified_status`` がこれを使って ``check_prices_verified`` の判定を
    変えるため、読まずに評価すると PR 時 CI と別条件の census になる
    (cert_fetch 以外の乖離を作らないこと)。
    """
    loader = getattr(qg, "_bp_load_matched_index", None)
    if loader is None:
        return None, None
    rakuten = loader(rakuten_path) if rakuten_path.exists() else None
    yahoo = loader(yahoo_path) if yahoo_path.exists() else None
    return rakuten, yahoo


def evaluate_corpus(
    src: pathlib.Path,
    schema: dict,
    *,
    posts: pathlib.Path | None = None,
    cert_fetch: bool = False,
    rakuten_idx: dict[str, Any] | None = None,
    yahoo_idx: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """全記事を評価し、不合格・減点チェック名つきの軽量レコード列を返す。

    レポート全文 (checks 20 件分) は 1903 記事ぶん保持すると数 MB になるため、
    集計に要る最小限 (slug / total_score / passed / 落ちた check / 減点された
    check) だけを残す。

    ``deducted_checks`` を持つ理由 (#4826・2026-08-10 実測):
      20 check 中 **15 は全コーパスで一度も減点も不合格も出していない**。
      信号があるのは keywords / narrative / faq / prices_verified の 4 つで、
      これらは **passed=True のまま score だけ下がる**「減点のみ」の check。
      total_score に丸められた時点で個別の理由は失われ、誰も見ていなかった。

      実測の中身は当初設計 (2026-05-12 e96a74d8b0「指名検索SEO適合 —
      title/meta/keywords/narrative 全箇所に商品名」) が未達な記事そのもの:
        keywords 512 件 (うち 258 件は商品名がどのキーワードにも無い)
        faq 595 件 (商品名を含む質問が推奨数に足りない)
        narrative 363 件 (closing 等の字数不足)
        prices_verified 573 件 (楽天/Yahoo が未検証・warn-only)

      合否だけ見ていると全件 OK に見えるので、ここで拾って census に残す。
    """
    out: list[dict[str, Any]] = []
    for jp in iter_article_paths(src):
        md = None
        if posts is not None:
            cand = posts / f"{jp.stem}.md"
            md = cand if cand.exists() else None
        report = qg.evaluate_article(
            jp, schema, md,
            rakuten_idx=rakuten_idx, yahoo_idx=yahoo_idx,
            cert_fetch=cert_fetch,
        )
        out.append({
            "slug": report.slug,
            "total_score": report.total_score,
            "passed": report.passed,
            "md": md is not None,
            "failed_checks": sorted(
                {c.name: c.message for c in report.checks if not c.passed}.items()
            ),
            # passed=True だが満点でない check (減点のみ)。合否には出ない soft signal。
            "deducted_checks": sorted(
                {c.name: c.message for c in report.checks
                 if c.passed and c.score < 1.0}.items()
            ),
        })
    return out


# 直近コホートの既定サイズ (slug 順の末尾から N 本)。
#
# なぜ「施行日以降」ではなく「直近 N 本」で切るか (2026-08-20 実測):
#   soft check を hard へ昇格する判断は #4826 項目2 の前例に従って
#   「施行日以降の新規記事で発火 0」で行う。ところが施行当日のコホートは
#   n=8 程度にしかならない (Jules は 1 日 16 本ペース・cron 6 時間毎) ため、
#   発火 0 でも rule of three の 95% 上限が 37% になり、何も言えない。
#   slug 順の直近 N 本で取れば分母が固定され、施行日を跨いだ時点から
#   上限が単調に締まっていくので、昇格の可否をはるかに早く判定できる。
#
# 3 段階を同時に出す理由: 直近 100 は施行直後の変化に敏感だが上限が緩く
# (3/100 = 3%)、直近 300 は上限が締まる (1%) が古い記事を含む。どちらか一方
# では「効いているが n 不足」と「n は足りるが薄まっている」を区別できない。
COHORT_SIZES = (100, 200, 300)


# 減点理由メッセージから件数を数えるとき、埋め込まれた数値
# ("closing 92<120" / "only 1 questions ...") で無限に分岐するのを防ぐ。
_DIGITS = str.maketrans("0123456789", "N" * 10)


def normalize_reason(message: str) -> str:
    """減点メッセージを集計キーに落とす (先頭の 1 理由・数値は N に潰す)。"""
    head = (message or "").split(";")[0].strip()
    return head.translate(_DIGITS)


def cohort_summary(records: list[dict[str, Any]], n: int) -> dict[str, Any] | None:
    """slug 順の直近 n 本だけで減点・不合格を数える。

    slug は ``YYYY-MM-DD-ASIN`` なので辞書順 = 生成順。末尾 n 本が最新コホート。

    ``zero_firing_95_upper`` は rule of three (発火 0 のとき真の発火率の 95%
    信頼上限 = 3/n)。**発火 0 の check にしか意味が無い**数値なので、各 check の
    件数と一緒に読むこと。#4826 項目2 の昇格判定はこの上限が 1.8% 以下
    (= n>=163 で発火 0) を目安にしている。

    コーパスが n に満たないときは None を返す (母集団が全体と同じになり、
    コホートとして意味を成さないため)。
    """
    if n <= 0 or len(records) < n:
        return None
    cohort = sorted(records, key=lambda r: r["slug"])[-n:]
    by_deduction: dict[str, int] = {}
    for r in cohort:
        for name, _msg in r.get("deducted_checks") or []:
            by_deduction[name] = by_deduction.get(name, 0) + 1
    failing = [r for r in cohort if not r["passed"]]
    return {
        "n": n,
        "from": cohort[0]["slug"],
        "to": cohort[-1]["slug"],
        "failing": len(failing),
        "failing_rate": round(len(failing) / n, 5),
        "by_deduction": dict(sorted(by_deduction.items(), key=lambda kv: (-kv[1], kv[0]))),
        "zero_firing_95_upper": round(3 / n, 5),
    }


def summarize(records: list[dict[str, Any]], *, cert_fetch: bool, date: str,
              cohort_sizes: tuple[int, ...] = COHORT_SIZES) -> dict[str, Any]:
    """census スナップショットを組み立てる。"""
    failing = [r for r in records if not r["passed"]]
    by_check: dict[str, int] = {}
    for r in failing:
        for name, _msg in r["failed_checks"]:
            by_check[name] = by_check.get(name, 0) + 1

    # 減点のみ (passed=True かつ score<1.0) の集計。合否には現れない soft signal。
    by_deduction: dict[str, int] = {}
    reasons: dict[str, dict[str, int]] = {}
    for r in records:
        for name, msg in r.get("deducted_checks") or []:
            by_deduction[name] = by_deduction.get(name, 0) + 1
            key = normalize_reason(msg)
            reasons.setdefault(name, {})
            reasons[name][key] = reasons[name].get(key, 0) + 1

    scores = sorted(r["total_score"] for r in records)
    return {
        "date": date,
        "articles": len(records),
        "failing": len(failing),
        "failing_rate": round(len(failing) / len(records), 5) if records else 0.0,
        "by_check": dict(sorted(by_check.items(), key=lambda kv: (-kv[1], kv[0]))),
        "by_deduction": dict(sorted(by_deduction.items(), key=lambda kv: (-kv[1], kv[0]))),
        # 減点の内訳。各 check につき上位理由を件数つきで残す (全件は出さないが、
        # by_deduction 側に総数があるので切り詰めても機能不全は隠れない)。
        "deduction_reasons": {
            name: dict(sorted(rs.items(), key=lambda kv: (-kv[1], kv[0]))[:5])
            for name, rs in sorted(reasons.items())
        },
        "score_min": scores[0] if scores else None,
        "score_median": scores[len(scores) // 2] if scores else None,
        "score_max": scores[-1] if scores else None,
        # PR 時 CI との乖離を明示する。false のとき cert_sources_content は
        # fetch 無しで評価されており、CI と 1 チェック分条件が違う。
        "cert_fetch": cert_fetch,
        # heading_hierarchy / body_word_count は MD が無いと passed=True score=1.0 を
        # 返す (unknown を pass に潰す既存挙動)。hugo/content/posts/* は gitignore で
        # CI では常に MD 無しだが、**ローカルにはビルド済み MD が残っている**ため、
        # 件数を記録しないと環境差で時系列が無言でずれる (実測: MD 有ならスコア
        # 中央値 -1 点)。合否は変わらないが、記録しないと差の出所が追えない。
        "md_evaluated": sum(1 for r in records if r.get("md")),
        # 直近コホート別の減点集計。全量の by_deduction だけでは、施行日以降の
        # 記事が全体の数 % しか無い段階で規約やプロンプト改訂の効果が原理的に
        # 見えない (実測 2026-08-20: #5083 の規約適用後コホートは 2109 本中
        # 42 本 = 2.0%。コホートが全滅しても全体値は 1pt しか動かない)。
        "cohorts": {
            f"recent_{n}": c
            for n in cohort_sizes
            if (c := cohort_summary(records, n)) is not None
        },
        "failing_slugs": sorted(
            (
                {"slug": r["slug"], "total_score": r["total_score"],
                 "failed_checks": [{"name": n, "message": m} for n, m in r["failed_checks"]]}
                for r in failing
            ),
            key=lambda d: d["slug"],
        ),
    }


def diff_against(previous: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, Any]:
    """前回スナップショットとの差分 (新規不合格 / 回復 / 継続) を返す。

    前回が無い初回は previous_date=None を返し、このレーンを fail させない
    (append_census_history.py の cohort と同じ扱い)。
    """
    cur = {d["slug"] for d in current.get("failing_slugs", [])}
    if previous is None:
        return {"previous_date": None, "new": sorted(cur), "recovered": [], "persisting": []}
    prev = {d["slug"] for d in previous.get("failing_slugs", [])}
    return {
        "previous_date": previous.get("date"),
        "new": sorted(cur - prev),
        "recovered": sorted(prev - cur),
        "persisting": sorted(cur & prev),
    }


def history_has_date(history_path: pathlib.Path, date: str) -> bool:
    if not history_path.exists():
        return False
    for line in history_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            if json.loads(line).get("date") == date:
                return True
        except json.JSONDecodeError:
            # 壊れた行は無視する (履歴全体を落とさない)
            continue
    return False


def append_history(history_path: pathlib.Path, row: dict[str, Any], *, force: bool) -> bool:
    """jsonl に 1 行 append する。同 date が既にあれば force 指定時のみ置換。"""
    date = row["date"]
    exists = history_has_date(history_path, date)
    if exists and not force:
        return False
    history_path.parent.mkdir(parents=True, exist_ok=True)
    if exists:
        kept = [
            ln for ln in history_path.read_text(encoding="utf-8").splitlines()
            if ln.strip() and _row_date(ln) != date
        ]
        kept.append(json.dumps(row, ensure_ascii=False))
        history_path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    else:
        with history_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return True


def _row_date(line: str) -> str | None:
    try:
        return json.loads(line).get("date")
    except json.JSONDecodeError:
        return None


def history_row(snapshot: dict[str, Any], diff: dict[str, Any]) -> dict[str, Any]:
    """時系列として列集合を安定させた 1 行を作る (append_census_history と同方針)。"""
    return {
        "date": snapshot["date"],
        "articles": snapshot["articles"],
        "failing": snapshot["failing"],
        "failing_rate": snapshot["failing_rate"],
        "by_check": snapshot["by_check"],
        "by_deduction": snapshot["by_deduction"],
        "score_min": snapshot["score_min"],
        "score_median": snapshot["score_median"],
        "score_max": snapshot["score_max"],
        "cert_fetch": snapshot["cert_fetch"],
        "md_evaluated": snapshot["md_evaluated"],
        # by_deduction と同じくネスト dict。列集合は cohort_sizes が変わらない
        # 限り安定する (サイズを変えるときは時系列が繋がらなくなる点に注意)。
        "cohorts": snapshot.get("cohorts") or {},
        "new": len(diff["new"]),
        "recovered": len(diff["recovered"]),
        "persisting": len(diff["persisting"]),
        "previous_date": diff["previous_date"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", type=pathlib.Path, default=DEFAULT_SRC)
    parser.add_argument("--posts", type=pathlib.Path, default=None,
                        help="レンダリング済み Markdown の場所 (省略時は MD 依存 check を skip)")
    parser.add_argument("--schema", type=pathlib.Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--snapshot", type=pathlib.Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--history", type=pathlib.Path, default=DEFAULT_HISTORY)
    parser.add_argument("--date", default=None, help="既定は UTC の当日 (YYYY-MM-DD)")
    parser.add_argument("--cert-fetch", action="store_true",
                        help="証明書ページへの実 HTTP fetch を有効にする (既定 無効)")
    parser.add_argument("--force", action="store_true",
                        help="同 date の history 行が既にあっても置換する")
    parser.add_argument("--cohorts", default=None,
                        help="直近コホートのサイズをカンマ区切りで指定 "
                             f"(既定 {','.join(str(x) for x in COHORT_SIZES)})。"
                             "空文字を渡すとコホート集計を行わない")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    if args.cohorts is None:
        cohort_sizes = COHORT_SIZES
    else:
        cohort_sizes = tuple(
            int(x) for x in (t.strip() for t in args.cohorts.split(",")) if x
        )

    if not args.src.exists():
        print(f"[quality_census] src not found: {args.src}")
        return 1
    if not args.schema.exists():
        print(f"[quality_census] schema not found: {args.schema}")
        return 1

    schema = json.loads(args.schema.read_text(encoding="utf-8"))
    date = args.date or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")

    previous: dict[str, Any] | None = None
    if args.snapshot.exists():
        try:
            previous = json.loads(args.snapshot.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"[quality_census] ! previous snapshot unreadable, treating as first run")

    rakuten_idx, yahoo_idx = load_matched_indexes()
    records = evaluate_corpus(
        args.src, schema, posts=args.posts, cert_fetch=args.cert_fetch,
        rakuten_idx=rakuten_idx, yahoo_idx=yahoo_idx,
    )
    if not records:
        print("[quality_census] no articles to check")
        return 1

    snapshot = summarize(records, cert_fetch=args.cert_fetch, date=date,
                         cohort_sizes=cohort_sizes)
    diff = diff_against(previous, snapshot)
    snapshot["diff"] = diff

    args.snapshot.parent.mkdir(parents=True, exist_ok=True)
    args.snapshot.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    appended = append_history(args.history, history_row(snapshot, diff), force=args.force)

    if not args.quiet:
        print(f"[quality_census] {date} articles={snapshot['articles']} "
              f"failing={snapshot['failing']} ({snapshot['failing_rate']:.2%}) "
              f"cert_fetch={snapshot['cert_fetch']} md={snapshot['md_evaluated']}")
        for name, n in snapshot["by_check"].items():
            print(f"    NG {name}: {n}")
        for name, n in snapshot["by_deduction"].items():
            print(f"    減点 {name}: {n}")
        for key, c in (snapshot.get("cohorts") or {}).items():
            hits = ", ".join(f"{k}={v}" for k, v in c["by_deduction"].items()) or "減点なし"
            print(f"    [{key}] {c['from']}..{c['to']} "
                  f"発火0なら95%上限 {c['zero_firing_95_upper']:.2%} — {hits}")
        print(f"    diff vs {diff['previous_date']}: "
              f"new={len(diff['new'])} recovered={len(diff['recovered'])} "
              f"persisting={len(diff['persisting'])}")
        if not appended:
            print(f"    history: {date} は既に存在するため skip (--force で置換)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
