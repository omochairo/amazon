#!/usr/bin/env python3
"""未対応の SNS 返信を private リポジトリの issue に 1 件 1 本で立てる (#7589)。

## なぜ要るか

返信レーンは検出 → 起草まで自動で回っているのに、人が気付く経路が ntfy push と
PENDING.md しかなく、どちらも「開かないと何も起きない」。2026-09-17 の実測で、
未対応 4 件はいずれも案が出来ているのに送信は通算 1 件だけだった。通知の種類ではなく、
**1 件ごとに閉じるべき箱になっていない**のが原因。

issue にすると open 件数がそのまま未対応件数になり、送信 (answered) / 見送り
(ignored) で close されて消える。

🚨 立てる先は **private リポジトリに限る**。issue の本文には第三者の本文と
   ハンドル名が入る。`omochairo/amazon` は public なのでここへは立てない
   (既定は無く、--repo か SNS_ISSUE_REPO の明示が要る)。

## 二重起票を防ぐ不変条件

status の一方向性 (new -> drafted -> answered/ignored) と同じ格の要件。

1. レコードに `issue_number` を持たせ、inbox.jsonl を commit する
2. ただし「issue 作成は成功したが commit / push 前に落ちた」窓がある。
   commit だけに完了マーカーを預けると、次の run が同じ返信をもう一度起票する。
   そこで **起票前に、リポジトリ側の既存 issue から `<!-- sns-inbox-id: … -->`
   マーカーを引いて突き合わせる**。外部副作用の完了判定を、失敗しうる非同期
   経路だけに依存させない

マーカーの照合に search API を使わないのは、書き込み直後の索引ラグ
(_analytics_issue_search.py の注記) が、ここではそのまま二重起票になるため。
label で絞った issue 一覧を直接読む方が件数も小さく、ラグも無い。

## バースト上限

1 run で作る issue は既定 5 本まで (draft 側の --limit と同じ)。現在の流量 (数件) では
当たらないが、取りこぼしを後から一括で流すときに効く。GitHub API のバースト起票は 2026-06-25 のアカウント凍結と
タイミングが一致しており、.claude/CLAUDE.md で禁止されている。

使い方:
    python scripts/sync_sns_inbox_issues.py --repo omochairo/amazon-home-ops
    python scripts/sync_sns_inbox_issues.py --repo … --dry-run

exit code:
    0 = 同期した (0 件でも 0)
    1 = gh 呼び出しに失敗した
    2 = 引数が不正 (--repo 未指定など)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

import render_sns_pending as renderer  # noqa: E402
import sns_inbox_store as store  # noqa: E402

DEFAULT_LABEL = "sns-reply"
DEFAULT_LIMIT = 5
TITLE_EXCERPT_CHARS = 24
PER_PAGE = 100
MAX_PAGES = 10


class GhError(RuntimeError):
    pass


def marker(record_id: str) -> str:
    return f"<!-- sns-inbox-id: {record_id} -->"


# --------------------------------------------------------------------------
# gh 呼び出し (テストはここだけ差し替える)
# --------------------------------------------------------------------------

def gh_json(args: list[str], payload: dict | None = None) -> object:
    """gh api を叩いて JSON を返す。payload があれば --input - で送る。"""
    cmd = ["gh", "api", *args]
    if payload is not None:
        cmd += ["--input", "-"]
    try:
        res = subprocess.run(
            cmd,
            input=json.dumps(payload, ensure_ascii=False) if payload is not None else None,
            check=True, capture_output=True, text=True, encoding="utf-8",
        )
    except subprocess.CalledProcessError as exc:
        # stderr に相手の本文が混ざることは無い (送るのは payload 側) が、
        # 念のため先頭 200 字だけに絞る
        raise GhError((exc.stderr or "")[:200].strip() or "gh api failed") from exc
    return json.loads(res.stdout) if res.stdout.strip() else {}


def fetch_marked_issues(repo: str, label: str, gh=gh_json) -> dict[str, dict]:
    """label 付き issue を全件読み、マーカー -> issue の dict にする。

    state=all。close 済みも読むのは、answered/ignored で閉じたものを
    「マーカーが無い = 未起票」と誤判定して立て直さないため。
    """
    found: dict[str, dict] = {}
    for page in range(1, MAX_PAGES + 1):
        items = gh([
            f"repos/{repo}/issues", "--method", "GET",
            "-f", f"labels={label}", "-f", "state=all",
            "-f", f"per_page={PER_PAGE}", "-f", f"page={page}",
        ])
        if not isinstance(items, list):
            break
        for it in items:
            if not isinstance(it, dict) or "pull_request" in it:
                continue  # issues エンドポイントは PR も返す
            body = it.get("body") or ""
            for line in body.splitlines():
                line = line.strip()
                if line.startswith("<!-- sns-inbox-id:") and line.endswith("-->"):
                    key = line[len("<!-- sns-inbox-id:"):-len("-->")].strip()
                    found.setdefault(key, it)
                    break
        if len(items) < PER_PAGE:
            break
    return found


# --------------------------------------------------------------------------
# 本文の組み立て
# --------------------------------------------------------------------------

def issue_title(rec: dict) -> str:
    head = " ".join(str(rec.get("text") or "").split())
    if len(head) > TITLE_EXCERPT_CHARS:
        head = head[:TITLE_EXCERPT_CHARS] + "…"
    who = f" / @{rec['author']}" if rec.get("author") else ""
    base = f"[sns-reply] {rec.get('channel')}{who}"
    return f"{base} — {head}" if head else base


def issue_body(rec: dict) -> str:
    lines = [marker(rec["id"]), ""]
    lines += renderer.render_record(rec, heading=False)
    lines += [
        "---",
        "",
        "送信は自動化していない。本文を確定してから `29-sns-reply-send.yml` を",
        "1 件ずつ dispatch する (手元なら `post_sns_reply.py`)。",
        "",
        "返さないと決めたら、この issue を close せずに inbox 側を `ignored` にする",
        "(close だけすると次の run で未対応として扱われ続ける)。",
    ]
    return "\n".join(lines)


def count_drafts_in_body(body: str) -> int:
    """issue 本文に何件の案が載っているかを数える (採用時の起点決めに使う)。

    render_record が案を `**案 N** (model)` の行で出すことに依存する。書式を
    変えるならここも変える (テストで固定してある)。
    """
    return sum(1 for line in body.splitlines() if line.startswith("**案 "))


def draft_comment(rec: dict, start_index: int) -> str:
    """start_index (0 始まり) 以降の案だけをコメントにする。"""
    drafts = (rec.get("drafts") or [])[start_index:]
    lines = [f"返信案が {len(drafts)} 件増えました。", ""]
    for offset, draft in enumerate(drafts):
        i = start_index + offset + 1
        lines += [
            f"**案 {i}** ({draft.get('model') or '不明'})",
            "",
            "```",
            str(draft.get("text") or ""),
            "```",
            "",
            f"送信: `--id {rec['id']} --draft {i}`",
            "",
        ]
    return "\n".join(lines)


def close_comment(rec: dict) -> str:
    if rec.get("status") == store.STATUS_ANSWERED:
        at = rec.get("answered_at") or "不明"
        return f"送信済み ({at})。inbox 側は `answered`。"
    return "返信しないと判断したもの。inbox 側は `ignored`。"


# --------------------------------------------------------------------------
# 同期
# --------------------------------------------------------------------------

def sync(
    repo: str,
    *,
    label: str = DEFAULT_LABEL,
    limit: int = DEFAULT_LIMIT,
    dry_run: bool = False,
    directory=None,
    gh=None,
) -> dict[str, int]:
    """inbox と issue を突き合わせて、作る / 追記する / 閉じる。

    返すのは件数だけ。**本文・ハンドル名は一切 print しない** (このスクリプトは
    public リポジトリにあり、Actions のログにも第三者の本文を残さない)。
    """
    # 既定は呼び出し時に引く (def 時に束縛すると gh_json の差し替えが効かない)
    gh = gh if gh is not None else gh_json
    d = directory if directory is not None else store.inbox_dir()
    records = store.load_records(d)
    marked = fetch_marked_issues(repo, label, gh=gh)
    stats = {"created": 0, "commented": 0, "closed": 0, "adopted": 0, "deferred": 0}

    # 起票は古い順。溜まっている分を上限で切るとき、落とすのは新しい側にする
    # (古い返信ほど放置期間が長く、返す価値が先に消える)
    pending = sorted(
        (r for r in records.values() if r.get("status") in (store.STATUS_NEW, store.STATUS_DRAFTED)),
        key=lambda r: (r.get("created_at") or "", r.get("id") or ""),
    )

    for rec in pending:
        rid = rec["id"]
        number = rec.get("issue_number")
        if not isinstance(number, int):
            existing = marked.get(rid)
            if existing is not None:
                # commit されなかった前 run の起票を拾う。ここが二重起票の防波堤
                number = existing.get("number")
                if isinstance(number, int) and not dry_run:
                    # 何件の案が既に body に載っているかは issue 本文から数える。
                    # 0 に置くと下の追記で全案をもう一度コメントすることになり、
                    # 1 に置くと本当に増えた案を取りこぼす
                    store.update_record(
                        rid,
                        {
                            "issue_number": number,
                            "issue_synced_drafts": count_drafts_in_body(existing.get("body") or ""),
                        },
                        d,
                    )
                    rec = store.load_records(d).get(rid, rec)
                stats["adopted"] += 1
            else:
                if stats["created"] >= limit:
                    stats["deferred"] += 1
                    continue
                if dry_run:
                    stats["created"] += 1
                    continue
                created = gh(
                    [f"repos/{repo}/issues", "--method", "POST"],
                    {"title": issue_title(rec), "body": issue_body(rec), "labels": [label]},
                )
                number = created.get("number") if isinstance(created, dict) else None
                if not isinstance(number, int):
                    raise GhError("issue 作成の応答に number が無い")
                store.update_record(
                    rid,
                    {"issue_number": number, "issue_synced_drafts": len(rec.get("drafts") or [])},
                    d,
                )
                stats["created"] += 1
                continue

        if not isinstance(number, int):
            continue

        # 起票のあとに増えた案をコメントで追う
        synced = rec.get("issue_synced_drafts")
        synced = synced if isinstance(synced, int) else 0
        have = len(rec.get("drafts") or [])
        if have > synced:
            if not dry_run:
                gh(
                    [f"repos/{repo}/issues/{number}/comments", "--method", "POST"],
                    {"body": draft_comment(rec, synced)},
                )
                store.update_record(rid, {"issue_synced_drafts": have}, d)
            stats["commented"] += 1

    # 決着したものを閉じる
    for rec in records.values():
        if rec.get("status") not in (store.STATUS_ANSWERED, store.STATUS_IGNORED):
            continue
        number = rec.get("issue_number")
        if not isinstance(number, int) or rec.get("issue_closed") is True:
            continue
        if not dry_run:
            gh(
                [f"repos/{repo}/issues/{number}/comments", "--method", "POST"],
                {"body": close_comment(rec)},
            )
            gh(
                [f"repos/{repo}/issues/{number}", "--method", "PATCH"],
                {"state": "closed", "state_reason": "completed"},
            )
            store.update_record(rec["id"], {"issue_closed": True}, d)
        stats["closed"] += 1

    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--repo",
        help="起票先 (private リポジトリに限る)。既定は環境変数 SNS_ISSUE_REPO",
    )
    ap.add_argument("--label", default=DEFAULT_LABEL)
    ap.add_argument(
        "--limit", type=int, default=DEFAULT_LIMIT,
        help=f"1 run で新規に立てる issue の上限 (既定 {DEFAULT_LIMIT})",
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    repo = (args.repo or os.environ.get("SNS_ISSUE_REPO") or "").strip()
    if not repo:
        print("--repo か SNS_ISSUE_REPO が要る (private リポジトリを指定する)", file=sys.stderr)
        return 2
    if repo == "omochairo/amazon":
        print("omochairo/amazon は public。第三者の本文を含む issue は立てない", file=sys.stderr)
        return 2

    try:
        stats = sync(repo, label=args.label, limit=args.limit, dry_run=args.dry_run)
    except GhError as exc:
        print(f"gh api に失敗: {exc}", file=sys.stderr)
        return 1

    prefix = "[dry-run] " if args.dry_run else ""
    print(
        f"{prefix}起票 {stats['created']} / 追記 {stats['commented']} / "
        f"close {stats['closed']} / 既存を採用 {stats['adopted']} / "
        f"上限で見送り {stats['deferred']}",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
