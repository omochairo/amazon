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

## close は「返さない」の意思表示

人が issue を close したら、次の同期で inbox 側を `ignored` にする。これが無いと
close しても status は `drafted` のままで、PENDING.md の「未対応 n 件」に残り続ける。
しかも **人が `ignored` を付ける手段は他に無い** (付けられるのは起草側の LLM だけ)。

誤って閉じても取り返せる — store は `ignored -> answered` を許している。

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
import re
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
        "**返さないと決めたら、この issue を close するだけでよい。** 次の同期で inbox 側が",
        "`ignored` になり、未対応から外れる。同じ返信で立て直されることはない。",
    ]
    return "\n".join(lines)


# 見出しは `**案 N** (model)` まで照合する。人のコメント「**案 2** が良さそう」を数えない
_DRAFT_HEADING = re.compile(r"^\*\*案 (\d+)\*\* \([^)]*\)\s*$")
_FENCE = re.compile(r"^(`{3,})")
DRAFT_COMMENT_LEAD = "返信案が "


def count_drafts_in_body(body: str) -> int:
    """本文 (issue 本文かコメント) に載っている案の最大番号を返す (採用時の起点決めに使う)。

    render_record / draft_comment が案を `**案 N** (model)` の行で出すことに依存する。
    書式を変えるならここも変える (テストで固定してある)。

    行数ではなく番号の最大値を取る。案の本文はフェンスの中に入るので、相手や LLM の
    文面に同じ書式の行が混ざっても、フェンスの内側は数えない (#8287)。フェンスは
    開いたときと同じ長さ以上のバッククォートでしか閉じない (renderer.code_fence が
    本文より長いフェンスを使う)。
    """
    top = 0
    fence = ""
    for line in body.splitlines():
        fm = _FENCE.match(line)
        if fence:
            if fm and len(fm.group(1)) >= len(fence) and not line[len(fm.group(1)):].strip():
                fence = ""
            continue
        if fm:
            fence = fm.group(1)
            continue
        m = _DRAFT_HEADING.match(line)
        if m:
            top = max(top, int(m.group(1)))
    return top


def count_synced_drafts(repo: str, number: int, body: str, gh=gh_json) -> int:
    """issue に既に載っている案の番号の最大値 (本文 + 追記コメント)。

    本文だけを見ると、起票後にコメントで足した案を「未同期」と読み、状態を
    失ったあとの再同期でもう一度コメントする (#8287)。
    """
    top = count_drafts_in_body(body)
    for page in range(1, MAX_PAGES + 1):
        items = gh([
            f"repos/{repo}/issues/{number}/comments", "--method", "GET",
            "-f", f"per_page={PER_PAGE}", "-f", f"page={page}",
        ])
        if not isinstance(items, list):
            break
        for it in items:
            # 数えるのは draft_comment が書いたコメントだけ (人の書き込みは見ない)
            body_ = (it.get("body") or "") if isinstance(it, dict) else ""
            if body_.startswith(DRAFT_COMMENT_LEAD):
                top = max(top, count_drafts_in_body(body_))
        if len(items) < PER_PAGE:
            break
    return top


def draft_comment(rec: dict, synced: int) -> str:
    """案番号が synced より大きい案 (まだ issue に出していない案) をコメントにする。

    作り直しで synced 以下の案が消えていれば、それが送れないことも書く。
    番号は作り直しても再利用しないので、issue に残る古い「案 1」を --draft 1 で
    送ろうとしても、別の文面ではなくエラーになる (#8323)。
    """
    numbered = store.numbered_drafts(rec)
    drafts = [(i, dr) for i, dr in numbered if i > synced]
    lines = [f"{DRAFT_COMMENT_LEAD}{len(drafts)} 件増えました。", ""]
    live = {i for i, _ in numbered}
    gone = [i for i in range(1, synced + 1) if i not in live]
    # issue に出した案が 1 件も残っていない = 今回のコメントが作り直しの報告。
    # 作り直しのあとに案が足されただけなら、破棄の警告は前のコメントで済んでいる
    if gone and not any(i <= synced for i in live):
        span = f"案 {gone[0]}" if len(gone) == 1 else f"案 {gone[0]}〜{gone[-1]}"
        lines += [
            f"⚠️ 作り直したため、これより前に載っている{span}は破棄済みです"
            "（`--draft` で指定するとエラーになります）。",
            "",
        ]
    for i, draft in drafts:
        text = str(draft.get("text") or "")
        fence = renderer.code_fence(text)
        lines += [
            f"**案 {i}** ({draft.get('model') or '不明'})",
            "",
            fence,
            text,
            fence,
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
    stats = {"created": 0, "commented": 0, "closed": 0, "adopted": 0, "deferred": 0, "retired": 0}

    # 起票は古い順。溜まっている分を上限で切るとき、落とすのは新しい側にする
    # (古い返信ほど放置期間が長く、返す価値が先に消える)
    pending = sorted(
        (r for r in records.values() if r.get("status") in (store.STATUS_NEW, store.STATUS_DRAFTED)),
        key=lambda r: (r.get("created_at") or "", r.get("id") or ""),
    )

    for rec in pending:
        rid = rec["id"]
        number = rec.get("issue_number")

        # 人が issue を close した = 「返さない」と決めた、と読む。
        # ここが無いと、close しても inbox は drafted のままで PENDING.md の
        # 「未対応 n 件」に残り続ける。かつ人が ignored にする手段は他に無い
        # (ignored を付けられるのは起草側の LLM だけ)。
        # 誤って閉じた場合は取り返せる — store は ignored -> answered を許す。
        closed_issue = marked.get(rid)
        if (
            closed_issue is not None
            and closed_issue.get("state") == "closed"
            and rec.get("status") in (store.STATUS_NEW, store.STATUS_DRAFTED)
        ):
            if not dry_run:
                store.update_record(
                    rid,
                    {
                        "status": store.STATUS_IGNORED,
                        "ignore_reason": "issue を人が close した",
                        "issue_number": closed_issue.get("number"),
                        "issue_closed": True,
                    },
                    d,
                )
            stats["retired"] += 1
            continue

        if not isinstance(number, int):
            existing = marked.get(rid)
            if existing is not None:
                # commit されなかった前 run の起票を拾う。ここが二重起票の防波堤
                number = existing.get("number")
                if isinstance(number, int) and not dry_run:
                    # 何件の案が既に issue に載っているかは本文と追記コメントから数える。
                    # 0 に置くと下の追記で全案をもう一度コメントすることになり、
                    # 1 に置くと本当に増えた案を取りこぼす
                    store.update_record(
                        rid,
                        {
                            "issue_number": number,
                            "issue_synced_drafts": count_synced_drafts(
                                repo, number, existing.get("body") or "", gh=gh,
                            ),
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
                    {"issue_number": number, "issue_synced_drafts": store.draft_seq(rec)},
                    d,
                )
                stats["created"] += 1
                continue

        if not isinstance(number, int):
            continue

        # 起票のあとに増えた案をコメントで追う。比べるのは件数ではなく案番号。
        # 件数だと、作り直しで 2 件を 2 件に置き換えたときに何も出ない (#8323)
        synced = rec.get("issue_synced_drafts")
        synced = synced if isinstance(synced, int) else 0
        have = max((i for i, _ in store.numbered_drafts(rec)), default=0)
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
        f"上限で見送り {stats['deferred']} / close 済みを ignored に {stats['retired']}",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
