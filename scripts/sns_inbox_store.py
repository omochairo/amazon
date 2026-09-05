#!/usr/bin/env python3
"""SNS に届いた返信・メンションの inbox store。

X / Threads / Bluesky に投稿しても、返ってきた返信に気付けず放置になる。
その取りこぼしを潰すレーンの状態管理層。検出 (fetch_sns_replies) →
起草 (draft_sns_reply) → 送信 (post_sns_reply) の 3 段が、この store を
唯一の受け渡し面として共有する。

🚨 置き場所 — **omochairo/amazon (public) にコミットしてはならない**。
   inbox の中身は第三者が書いた本文とハンドル名であり、public リポジトリに
   置くのは CLAUDE.md「個人情報・第三者の生ログを置かない」に真正面から
   反する。Yahoo レビュー原文 (docs/article-quality-overhaul-design.md §5.2
   条件4) と同じ扱いで、**ランナーローカル or private リポジトリにのみ置く**。
   既定パスも意図的に repo 外を向けてある。

   SNS_INBOX_DIR を明示指定するのが正。未指定時は <repo>/tmp/sns_inbox に
   落ちるが、tmp/ は .gitignore 済みであることを前提にしている。

ファイル構成:
    <SNS_INBOX_DIR>/inbox.jsonl    1 行 1 レコード。同じ id の行は **後勝ち**
                                    (append-only で更新を表現する。途中で
                                    プロセスが死んでも既存行を壊さない)
    <SNS_INBOX_DIR>/cursors.json   channel 別の「ここまで見た」印

レコード schema:
    id            "<channel>:<native_id>"  — 重複検出の唯一のキー
    channel       "threads" | "bluesky" | "x"
    kind          "reply" | "mention" | "quote"
    native_id     チャネル側の ID (Threads の media id / Bluesky の at-uri 等)
    parent_id     返信元になった自分の投稿の ID (取れない場合は "")
    author        相手のハンドル (表示用。照合には使わない)
    text          相手の本文
    permalink     相手の投稿への URL (取れない場合は "")
    created_at    相手が投稿した時刻 (ISO8601 UTC)
    detected_at   こちらが検出した時刻 (ISO8601 UTC)
    status        "new" | "drafted" | "answered" | "ignored"
    drafts        [{"text": ..., "model": ..., "generated_at": ...}, ...]
    answered_at   送信した時刻 (ISO8601 UTC / 未送信は "")
    reply_native_id  送信した自分の返信の ID (未送信は "")

status は前に戻さない。answered/ignored に落ちたものを検出側が new に
書き戻すと、既に返信済みの相手へ二度目を投げる事故になる (SNS 配信レーンで
published_at の bookkeeping が遅れて二重投稿になった #4782 と同じ形)。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

CHANNELS = ("threads", "bluesky", "x")
KINDS = ("reply", "mention", "quote")

STATUS_NEW = "new"
STATUS_DRAFTED = "drafted"
STATUS_ANSWERED = "answered"
STATUS_IGNORED = "ignored"

# 進行方向。左のものへは戻さない。
_STATUS_RANK = {
    STATUS_NEW: 0,
    STATUS_DRAFTED: 1,
    STATUS_ANSWERED: 2,
    STATUS_IGNORED: 2,
}

INBOX_FILENAME = "inbox.jsonl"
CURSORS_FILENAME = "cursors.json"


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def inbox_dir() -> Path:
    """inbox の置き場所。SNS_INBOX_DIR が正、未指定なら repo 外を指す tmp/。"""
    raw = (os.environ.get("SNS_INBOX_DIR") or "").strip()
    return Path(raw) if raw else REPO_ROOT / "tmp" / "sns_inbox"


def make_id(channel: str, native_id: str) -> str:
    return f"{channel}:{native_id}"


def load_records(directory: Path | None = None) -> dict[str, dict]:
    """inbox.jsonl を読み、id -> レコード の dict にする (同 id は後勝ち)。

    壊れた行は捨てて続行する。1 行の破損で inbox 全体が読めなくなる方が損。
    """
    path = (directory or inbox_dir()) / INBOX_FILENAME
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and isinstance(rec.get("id"), str):
                out[rec["id"]] = rec
    return out


def append_record(rec: dict, directory: Path | None = None) -> None:
    d = directory or inbox_dir()
    d.mkdir(parents=True, exist_ok=True)
    with (d / INBOX_FILENAME).open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def new_record(
    *,
    channel: str,
    kind: str,
    native_id: str,
    text: str,
    author: str = "",
    parent_id: str = "",
    permalink: str = "",
    created_at: str = "",
) -> dict:
    if channel not in CHANNELS:
        raise ValueError(f"unknown channel: {channel}")
    if kind not in KINDS:
        raise ValueError(f"unknown kind: {kind}")
    return {
        "id": make_id(channel, native_id),
        "channel": channel,
        "kind": kind,
        "native_id": native_id,
        "parent_id": parent_id,
        "author": author,
        "text": text,
        "permalink": permalink,
        "created_at": created_at,
        "detected_at": utcnow(),
        "status": STATUS_NEW,
        "drafts": [],
        "answered_at": "",
        "reply_native_id": "",
    }


def record_new_items(items: list[dict], directory: Path | None = None) -> list[dict]:
    """未知の id のものだけ append し、実際に追加されたレコードを返す。

    既知の id は status を問わず触らない。answered まで進んだものを
    検出側が上書きして new に戻す事故を、そもそも起こせなくする。
    """
    d = directory or inbox_dir()
    known = load_records(d)
    added: list[dict] = []
    for rec in items:
        if not isinstance(rec, dict) or not isinstance(rec.get("id"), str):
            continue
        if rec["id"] in known:
            continue
        append_record(rec, d)
        known[rec["id"]] = rec
        added.append(rec)
    return added


def update_record(
    record_id: str, changes: dict, directory: Path | None = None,
) -> dict | None:
    """既存レコードに changes をマージした行を append する。

    status は後退させない (rank が下がる指定は黙って無視する)。未知の id は
    None を返す — 検出を経ていない ID への更新は事故か typo なので作らない。
    """
    d = directory or inbox_dir()
    records = load_records(d)
    cur = records.get(record_id)
    if cur is None:
        return None

    merged = dict(cur)
    for k, v in changes.items():
        if k in ("id", "channel", "native_id"):
            continue  # 同一性を決めるキーは動かさない
        if k == "status":
            if _STATUS_RANK.get(str(v), -1) < _STATUS_RANK.get(str(cur.get("status")), 0):
                continue
        merged[k] = v
    append_record(merged, d)
    return merged


def add_draft(
    record_id: str, text: str, model: str, directory: Path | None = None,
) -> dict | None:
    d = directory or inbox_dir()
    cur = load_records(d).get(record_id)
    if cur is None:
        return None
    drafts = list(cur.get("drafts") or [])
    drafts.append({"text": text, "model": model, "generated_at": utcnow()})
    return update_record(
        record_id, {"drafts": drafts, "status": STATUS_DRAFTED}, d,
    )


def pending(directory: Path | None = None) -> list[dict]:
    """まだ返していないもの (new / drafted) を古い順に返す。"""
    recs = [
        r for r in load_records(directory).values()
        if r.get("status") in (STATUS_NEW, STATUS_DRAFTED)
    ]
    return sorted(recs, key=lambda r: (r.get("created_at") or "", r.get("id") or ""))


# --------------------------------------------------------------------------
# cursors
# --------------------------------------------------------------------------

def load_cursors(directory: Path | None = None) -> dict:
    path = (directory or inbox_dir()) / CURSORS_FILENAME
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def set_cursor(channel: str, value: str, directory: Path | None = None) -> None:
    d = directory or inbox_dir()
    d.mkdir(parents=True, exist_ok=True)
    cursors = load_cursors(d)
    cursors[channel] = value
    (d / CURSORS_FILENAME).write_text(
        json.dumps(cursors, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def get_cursor(channel: str, directory: Path | None = None) -> str:
    val = load_cursors(directory).get(channel)
    return val if isinstance(val, str) else ""
