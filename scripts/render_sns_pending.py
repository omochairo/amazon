#!/usr/bin/env python3
"""inbox の未対応分を 1 枚の Markdown にする (スマホで開く用)。

通知が来たときに開く先。「何が来ていて、どう返す案があって、送るには何を
叩けばいいか」がこの 1 ファイルで完結するようにする。ファイルを跨いで
探させると、結局あとで見る = 放置になる。

🚨 出力先は private リポジトリに限る。第三者の本文とハンドル名が入る。

使い方:
    python scripts/render_sns_pending.py --out ops/sns_inbox/PENDING.md

exit code:
    0 = 書き出した (未対応 0 件でも書く。0 件だと分かることに価値がある)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

import sns_inbox_store as store  # noqa: E402


def render(records: list[dict]) -> str:
    lines = [
        "# 未対応の SNS 返信",
        "",
        f"更新: {store.utcnow()} / 未対応 **{len(records)} 件**",
        "",
    ]
    if not records:
        lines += ["未対応はありません。", ""]
        return "\n".join(lines)

    lines += [
        "送信は自動化していない。本文を確定してから 1 件ずつ dispatch する",
        "(`29-sns-reply-inbox.yml` の `send` か、手元で `post_sns_reply.py`)。",
        "",
        "---",
        "",
    ]

    for rec in records:
        title = f"## {rec['channel']} / {rec['kind']}"
        if rec.get("author"):
            title += f" — @{rec['author']}"
        lines.append(title)
        lines.append("")
        lines.append(f"- 受信: {rec.get('created_at') or '不明'}")
        if rec.get("permalink"):
            lines.append(f"- 元投稿: {rec['permalink']}")
        lines.append(f"- id: `{rec['id']}`")
        lines.append("")
        lines.append("**相手の本文**")
        lines.append("")
        lines.append("> " + str(rec.get("text") or "").replace("\n", "\n> "))
        lines.append("")

        drafts = rec.get("drafts") or []
        if not drafts:
            lines += ["返信案はまだありません (起草レーン待ち)。", ""]
        for i, draft in enumerate(drafts, start=1):
            lines.append(f"**案 {i}** ({draft.get('model') or '不明'})")
            lines.append("")
            lines.append("```")
            lines.append(str(draft.get("text") or ""))
            lines.append("```")
            lines.append("")
            lines.append(
                f"送信: `--id {rec['id']} --draft {i}`  "
                "(本文を直したいときは `--body \"...\"`)",
            )
            lines.append("")
        lines += ["---", ""]

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    records = store.pending(store.inbox_dir())
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(records), encoding="utf-8")
    print(f"{out}: 未対応 {len(records)} 件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
