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
    drafts        [{"no": ..., "text": ..., "model": ..., "generated_at": ...}, ...]
                  有効な案だけ。作り直し (discard_drafts) で置き換わる
    draft_seq     これまでに振った案番号の最大値。作り直しても戻さない (#8323)
    answered_at   送信した時刻 (ISO8601 UTC / 未送信は "")
    reply_native_id  送信した自分の返信の ID (未送信は "")

status は前に戻さない。answered/ignored に落ちたものを検出側が new に
書き戻すと、既に返信済みの相手へ二度目を投げる事故になる (SNS 配信レーンで
published_at の bookkeeping が遅れて二重投稿になった #4782 と同じ形)。
"""
from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
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


def numbered_drafts(rec: dict) -> list[tuple[int, dict]]:
    """(案番号, 案) の一覧。番号の無い古い案は並び順 (1 始まり) を番号とみなす。

    案番号は作り直しても再利用しない (#8323)。人が issue で読んだ「案 1」と、
    作り直したあとの別の文面が同じ番号で送られる事故を防ぐため。
    """
    out: list[tuple[int, dict]] = []
    for i, dr in enumerate(rec.get("drafts") or [], start=1):
        if not isinstance(dr, dict):
            continue
        no = dr.get("no")
        out.append((no if isinstance(no, int) and no > 0 else i, dr))
    return out


def draft_seq(rec: dict) -> int:
    """これまでに振った案番号の最大値 (破棄した案も含む)。"""
    seq = rec.get("draft_seq")
    top = seq if isinstance(seq, int) else 0
    for no, _ in numbered_drafts(rec):
        top = max(top, no)
    return top


def add_draft(
    record_id: str, text: str, model: str, directory: Path | None = None,
) -> dict | None:
    d = directory or inbox_dir()
    cur = load_records(d).get(record_id)
    if cur is None:
        return None
    no = draft_seq(cur) + 1
    drafts = list(cur.get("drafts") or [])
    drafts.append({"no": no, "text": text, "model": model, "generated_at": utcnow()})
    return update_record(
        record_id, {"drafts": drafts, "draft_seq": no, "status": STATUS_DRAFTED}, d,
    )


def discard_drafts(record_id: str, directory: Path | None = None) -> dict | None:
    """案を全部捨てる (作り直しの前段)。番号は draft_seq に残して再利用させない。"""
    d = directory or inbox_dir()
    cur = load_records(d).get(record_id)
    if cur is None:
        return None
    return update_record(record_id, {"drafts": [], "draft_seq": draft_seq(cur)}, d)


class LockBusy(RuntimeError):
    pass


def _lock_path(record_id: str, directory: Path) -> Path:
    # id には ":" や "/" (Bluesky の at-uri) が入るのでファイル名にはハッシュを使う
    digest = hashlib.sha256(record_id.encode("utf-8")).hexdigest()[:24]
    return directory / "locks" / f"{digest}.lock"


@contextmanager
def record_lock(record_id: str, directory: Path | None = None):
    """同じ id の送信を同時に 1 本に絞る排他 (O_CREAT|O_EXCL のロックファイル)。

    取れなければ待たずに LockBusy を投げる。送信側は「読み直す → ガード →
    印を書く → 送る → 記録」の間ずっと持つ。ファイルが残るのはその間に
    プロセスが強制終了されたときで、そのときは send_started_at も残っている。
    """
    d = directory or inbox_dir()
    path = _lock_path(record_id, d)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise LockBusy(str(path)) from None
    except PermissionError as e:
        # Windows では消している途中のファイルに O_EXCL を当てると PermissionError
        # (そのとき exists() は False を返しうるので見分けられない)。本当の権限不足も
        # 含めて「送らない」側に倒す
        raise LockBusy(f"{path} ({e})") from None
    try:
        try:
            os.write(fd, f"{os.getpid()} {utcnow()} {record_id}\n".encode("utf-8"))
        finally:
            os.close(fd)
        yield path
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _sort_key(r: dict) -> tuple[str, str]:
    # created_at (相手の投稿時刻) が API 仕様の穴等で空文字のことがある (#8287)。
    # 空文字は最小値として扱われるため、常に最優先に並んで --limit の枠を占有
    # する。detected_at (こちらの検出時刻。new_record で必ず入る) にフォール
    # バックし、日時取得に失敗したレコードが不当に優先されないようにする
    return (r.get("created_at") or r.get("detected_at") or "", r.get("id") or "")


def pending(directory: Path | None = None) -> list[dict]:
    """まだ返していないもの (new / drafted) を古い順に返す。"""
    recs = [
        r for r in load_records(directory).values()
        if r.get("status") in (STATUS_NEW, STATUS_DRAFTED)
    ]
    return sorted(recs, key=_sort_key)


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
