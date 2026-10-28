#!/usr/bin/env python3
"""配信済み engagement 投稿の反応 (表示回数・いいね・返信など) を取得して記録用 Markdown を作る (#10628)。

amazon-navi-brain#139 の案 0'。X と Threads のどちらで何が効いているかを判定するための
計測で、値は private リポジトリ (amazon-navi-brain#140) にだけ書く。

対象の選び方:
  JST の日付で「N 日前 (既定 3)」に配信した row を測る。時刻の窓 (48〜72h 前) にしないのは、
  GitHub の schedule が数時間遅れて起動するため (2026-10 の実測で 4〜6h)。時刻の窓だと
  run ごとに窓がずれて取りこぼし・二重計測が出るが、日付で区切れば毎日 1 回の run で
  各投稿をちょうど 1 回ずつ測れる。測る時点の経過は約 2.5〜3.5 日。

取得元:
  X       : Buffer GraphQL `post(input:{id}).metrics` (個人用 API キー・insightsRead が要る)。
            Buffer の取り込みは 1 日 1 回なので値は最大 24h 前のもの (metricsUpdatedAt を記録)。
  Threads : Graph API `GET /{media-id}/insights`

🚨 出力の秘匿性 — このスクリプトは public リポジトリの Actions で動く。値は --out の
   ファイルにだけ書き、標準出力には件数と HTTP の結果しか出さない。

使い方:
    python scripts/fetch_engagement_metrics.py --out /tmp/metrics.md
    python scripts/fetch_engagement_metrics.py --days-ago 3 --out /tmp/metrics.md
    python scripts/fetch_engagement_metrics.py --days-ago 10 --source article --out /tmp/metrics.md

記事投稿 (20-sns-publish.yml のリンク付き X 投稿) は data/sns_published.json の x_posts
から測る (navi-brain#139)。種別は article/<ASIN>。Threads の記事投稿は測らない。

env:
    BUFFER_ACCESS_TOKEN                    X の取得に必要
    THREADS_ACCESS_TOKEN                   Threads の取得に必要

exit code:
    0 = 全件取得できた (対象 0 件も含む)
    1 = 取得に失敗した投稿がある (Markdown は失敗行込みで書き出す)
    2 = 必要な token が未設定 (黙って緑にしない: secret 失効でレーンが黙って死ぬのを防ぐ)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

REPO_ROOT = Path(__file__).resolve().parent.parent
QUEUE_X = REPO_ROOT / "data" / "engagement_queue_x.jsonl"
QUEUE_THREADS = REPO_ROOT / "data" / "engagement_queue_threads.jsonl"
SNS_PUBLISHED = REPO_ROOT / "data" / "sns_published.json"

BUFFER_GRAPHQL_URL = "https://api.buffer.com/"
THREADS_BASE = "https://graph.threads.net/v1.0"
THREADS_METRICS = ("views", "likes", "replies", "reposts", "quotes", "shares")
# 表の列。X と Threads で呼び名が違うものを同じ列に揃える (Buffer の正規化名 → 列名)。
X_COLUMNS = {
    "impressions": "表示", "reactions": "いいね", "comments": "返信",
    "reposts": "リポスト", "clicks": "クリック", "engagementRate": "反応率%",
}
THREADS_COLUMNS = {
    "views": "表示", "likes": "いいね", "replies": "返信",
    "reposts": "リポスト", "quotes": "引用", "shares": "シェア",
}
COLUMNS = ("表示", "いいね", "返信", "リポスト", "引用", "シェア", "クリック", "反応率%")

JST = timezone(timedelta(hours=9))
TIMEOUT = 30
TEXT_HEAD = 40


def _parse_ts(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        d = datetime.fromisoformat(value)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def load_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def select_rows(rows: list[dict], target: date) -> list[dict]:
    """JST の日付が target の日に配信され、その SNS の post_id を持つ row を配信順に返す。

    post_id が null の row (X を休む枠で Bluesky にだけ流した row, #10628) は X に
    出ていないので測らない。
    """
    picked = []
    for r in rows:
        at = _parse_ts(r.get("published_at"))
        pid = r.get("post_id")
        if at is None or not isinstance(pid, str) or not pid:
            continue
        if at.astimezone(JST).date() == target:
            picked.append((at, r))
    picked.sort(key=lambda t: t[0])
    return [r for _, r in picked]


def _http(req: urllib.request.Request) -> tuple[int, dict | None]:
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace") if e.fp else ""
        status = e.code
    except Exception:  # noqa: BLE001 — 種別は呼び出し側で「取得失敗」に丸める
        return 0, None
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, None


def fetch_buffer_metrics(post_id: str, token: str) -> tuple[dict | None, str]:
    """(列名 → 値, metricsUpdatedAt) を返す。失敗は (None, 失敗の種別)。"""
    query = (
        "query { post(input: { id: %s }) { metrics { type value } metricsUpdatedAt } }"
        % json.dumps(post_id)
    )
    req = urllib.request.Request(
        BUFFER_GRAPHQL_URL,
        data=json.dumps({"query": query}).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    status, payload = _http(req)
    if status != 200 or not isinstance(payload, dict):
        return None, f"http={status}"
    if payload.get("errors"):
        codes = sorted({
            str(((e or {}).get("extensions") or {}).get("code"))
            for e in payload["errors"] if isinstance(e, dict)
        })
        return None, "errors=" + ",".join(codes)
    post = (payload.get("data") or {}).get("post") or {}
    values = {}
    for m in post.get("metrics") or []:
        if isinstance(m, dict) and m.get("type") in X_COLUMNS:
            values[X_COLUMNS[m["type"]]] = m.get("value")
    return values, str(post.get("metricsUpdatedAt") or "")


def fetch_threads_insights(media_id: str, token: str) -> tuple[dict | None, str]:
    q = urllib.parse.urlencode({"metric": ",".join(THREADS_METRICS), "access_token": token})
    req = urllib.request.Request(f"{THREADS_BASE}/{media_id}/insights?{q}", method="GET")
    status, payload = _http(req)
    if status != 200 or not isinstance(payload, dict):
        code = ""
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            code = f" code={payload['error'].get('code')}"
        return None, f"http={status}{code}"
    values = {}
    for m in payload.get("data") or []:
        if not isinstance(m, dict) or m.get("name") not in THREADS_COLUMNS:
            continue
        v = None
        if isinstance(m.get("total_value"), dict):
            v = m["total_value"].get("value")
        elif m.get("values"):
            v = (m["values"][0] or {}).get("value")
        values[THREADS_COLUMNS[m["name"]]] = v
    return values, ""


def _fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, float) and not v.is_integer():
        return f"{v:.1f}"
    return str(int(v)) if isinstance(v, (int, float)) else str(v)


def _head(text: str) -> str:
    s = " ".join((text or "").split()).replace("|", "｜")
    return s if len(s) <= TEXT_HEAD else s[: TEXT_HEAD - 1] + "…"


def build_markdown(target: date, records: list[dict], fetched_at: datetime, source: str = "all") -> str:
    scope = {"article": "（記事投稿のみ）", "engagement": "（記事投稿を除く）"}.get(source, "")
    lines = [
        f"## {target.isoformat()}（JST）に配信した分{scope}",
        "",
        f"取得: {fetched_at.astimezone(JST).strftime('%Y-%m-%d %H:%M')} JST",
        "",
        "| SNS | 配信 | 種別 | 本文 | " + " | ".join(COLUMNS) + " |",
        "|---|---|---|---|" + "---:|" * len(COLUMNS),
    ]
    for rec in records:
        cat = f"{rec['category'] or '-'}/{rec['subcategory'] or '-'}"
        if rec["metrics"] is None:
            cells = ["取得失敗"] + [""] * (len(COLUMNS) - 1)
        else:
            cells = [_fmt(rec["metrics"].get(c)) for c in COLUMNS]
        lines.append(
            f"| {rec['channel']} | {rec['published_jst']} | {cat} | {_head(rec['text'])} | "
            + " | ".join(cells) + " |"
        )
    if not records:
        lines.append("| - | - | - | 対象の投稿なし | " + " | ".join([""] * len(COLUMNS)) + " |")
    lines += [
        "",
        "X の値は Buffer の取り込み時点（1 日 1 回）のもの。Threads の表示・シェアは Meta 側で「開発中」扱いの指標。",
        "",
        "<!-- engagement-metrics-json",
        json.dumps({"target_date": target.isoformat(), "records": records}, ensure_ascii=False),
        "-->",
        "",
    ]
    return "\n".join(lines)


def load_article_rows(path: Path) -> list[dict]:
    """sns_published.json の x_posts を engagement の row と同じ形にして返す (navi-brain#139)。

    記事投稿 (20-sns-publish.yml) は engagement のキューに載らないので、
    pick_sns_target.py --x-post-file が記録した Buffer の投稿 ID から測る。
    """
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    posts = state.get("x_posts") if isinstance(state, dict) else None
    if not isinstance(posts, dict):
        return []
    rows = []
    for asin, rec in posts.items():
        if not isinstance(rec, dict):
            continue
        rows.append({
            "id": f"article-x-{asin}",
            "post_id": rec.get("post_id"),
            "published_at": rec.get("published_at"),
            "category": "article",
            "subcategory": asin,
            "text": rec.get("text") or "",
        })
    return rows


SOURCES = ("all", "engagement", "article")


def collect(target: date, x_token: str, threads_token: str, source: str = "all") -> list[dict]:
    """source="article" は記事投稿だけ、"engagement" は従来の engagement 投稿だけを測る。

    過去の日付を記事投稿だけ取り直すときに、engagement の行を #140 に二重に載せないため。
    """
    x_rows: list[dict] = []
    threads_rows: list[dict] = []
    if source in ("all", "engagement"):
        x_rows += load_rows(QUEUE_X)
        threads_rows += load_rows(QUEUE_THREADS)
    if source in ("all", "article"):
        x_rows += load_article_rows(SNS_PUBLISHED)
    records = []
    for channel, rows, token, fetch in (
        ("X", x_rows, x_token, fetch_buffer_metrics),
        ("Threads", threads_rows, threads_token, fetch_threads_insights),
    ):
        for row in select_rows(rows, target):
            metrics, note = fetch(row["post_id"], token)
            at = _parse_ts(row["published_at"])
            records.append({
                "channel": channel,
                "id": row.get("id"),
                "post_id": row["post_id"],
                "published_jst": at.astimezone(JST).strftime("%H:%M") if at else "",
                "category": row.get("category"),
                "subcategory": row.get("subcategory"),
                "text": row.get("text") or "",
                "metrics": metrics,
                "note": note,
            })
    return records


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, type=Path, help="記録用 Markdown の書き出し先 (値はここにだけ書く)")
    ap.add_argument("--days-ago", type=int, default=3, help="JST で何日前に配信した分を測るか (既定 3)")
    ap.add_argument("--source", choices=SOURCES, default="all",
                    help="測る投稿: all (既定) / engagement / article (記事投稿の X だけ)")
    args = ap.parse_args(argv)

    x_token = (os.environ.get("BUFFER_ACCESS_TOKEN") or "").strip()
    threads_token = (os.environ.get("THREADS_ACCESS_TOKEN") or "").strip()
    # 記事投稿は X だけなので、--source article のときは Threads の token を要求しない。
    needed = [("BUFFER_ACCESS_TOKEN", x_token)]
    if args.source != "article":
        needed.append(("THREADS_ACCESS_TOKEN", threads_token))
    missing = [n for n, v in needed if not v]
    if missing:
        print(f"::error::{', '.join(missing)} が未設定 — 反応を取得できない")
        return 2

    now = datetime.now(timezone.utc)
    target = now.astimezone(JST).date() - timedelta(days=args.days_ago)
    records = collect(target, x_token, threads_token, args.source)
    args.out.write_text(build_markdown(target, records, now, args.source), encoding="utf-8")

    failed = [r for r in records if r["metrics"] is None]
    by_channel = {}
    for r in records:
        by_channel[r["channel"]] = by_channel.get(r["channel"], 0) + 1
    print(f"target={target.isoformat()} posts={len(records)} by_channel={by_channel} failed={len(failed)}")
    for r in failed:
        # 値は出さない。どの SNS がなぜ落ちたかだけ。
        print(f"::warning::[{r['channel']}] 取得失敗 ({r['note']})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
