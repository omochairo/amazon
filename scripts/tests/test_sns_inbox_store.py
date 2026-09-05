"""sns_inbox_store のテスト。

このレーンで一番怖い事故は「返信済みの相手にもう一度返す」こと。store の
不変条件 (id 一意 / status 後退禁止 / 既知 id は検出側が触らない) をここで
固定する。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sns_inbox_store as store  # noqa: E402


@pytest.fixture()
def d(tmp_path: Path) -> Path:
    return tmp_path / "inbox"


def _rec(native_id: str = "abc", **kw) -> dict:
    base = dict(channel="threads", kind="reply", native_id=native_id, text="こんにちは")
    base.update(kw)
    return store.new_record(**base)


def test_new_record_shape():
    r = _rec()
    assert r["id"] == "threads:abc"
    assert r["status"] == store.STATUS_NEW
    assert r["drafts"] == []
    assert r["detected_at"].endswith("Z")


@pytest.mark.parametrize(
    ("kw", "msg"),
    [({"channel": "mixi"}, "channel"), ({"kind": "dm"}, "kind")],
)
def test_new_record_rejects_unknown_enum(kw, msg):
    with pytest.raises(ValueError, match=msg):
        _rec(**kw)


def test_load_records_missing_dir_is_empty(d: Path):
    assert store.load_records(d) == {}


def test_record_new_items_dedupes(d: Path):
    added = store.record_new_items([_rec("a"), _rec("b")], d)
    assert {r["id"] for r in added} == {"threads:a", "threads:b"}

    again = store.record_new_items([_rec("a"), _rec("c")], d)
    assert [r["id"] for r in again] == ["threads:c"]
    assert len(store.load_records(d)) == 3


def test_record_new_items_does_not_resurrect_answered(d: Path):
    """検出側が既知 id を再投入しても answered を new に戻さない。

    ここが崩れると「返信済みの相手に二度返す」事故になる。
    """
    store.record_new_items([_rec("a")], d)
    store.update_record("threads:a", {"status": store.STATUS_ANSWERED}, d)

    store.record_new_items([_rec("a")], d)

    assert store.load_records(d)["threads:a"]["status"] == store.STATUS_ANSWERED
    assert store.pending(d) == []


def test_load_records_last_write_wins(d: Path):
    store.record_new_items([_rec("a")], d)
    store.update_record("threads:a", {"author": "someone"}, d)
    assert store.load_records(d)["threads:a"]["author"] == "someone"


def test_load_records_skips_broken_lines(d: Path):
    store.record_new_items([_rec("a")], d)
    with (d / store.INBOX_FILENAME).open("a", encoding="utf-8") as f:
        f.write("{ not json\n")
        f.write(json.dumps({"no_id": 1}) + "\n")
    assert set(store.load_records(d)) == {"threads:a"}


def test_update_record_unknown_id_returns_none(d: Path):
    assert store.update_record("threads:nope", {"status": "answered"}, d) is None


def test_update_record_cannot_move_status_backwards(d: Path):
    store.record_new_items([_rec("a")], d)
    store.update_record("threads:a", {"status": store.STATUS_ANSWERED}, d)
    store.update_record("threads:a", {"status": store.STATUS_NEW}, d)
    assert store.load_records(d)["threads:a"]["status"] == store.STATUS_ANSWERED


def test_update_record_cannot_change_identity(d: Path):
    store.record_new_items([_rec("a")], d)
    store.update_record("threads:a", {"id": "x:zzz", "channel": "x", "native_id": "zzz"}, d)
    rec = store.load_records(d)["threads:a"]
    assert (rec["id"], rec["channel"], rec["native_id"]) == ("threads:a", "threads", "a")


def test_add_draft_appends_and_marks_drafted(d: Path):
    store.record_new_items([_rec("a")], d)
    store.add_draft("threads:a", "案1", "claude-sonnet-4-6", d)
    store.add_draft("threads:a", "案2", "claude-sonnet-4-6", d)

    rec = store.load_records(d)["threads:a"]
    assert [x["text"] for x in rec["drafts"]] == ["案1", "案2"]
    assert rec["status"] == store.STATUS_DRAFTED


def test_pending_excludes_answered_and_ignored_and_sorts_by_created_at(d: Path):
    store.record_new_items(
        [
            _rec("late", created_at="2026-09-05T10:00:00Z"),
            _rec("early", created_at="2026-09-01T10:00:00Z"),
            _rec("done", created_at="2026-09-02T10:00:00Z"),
            _rec("skip", created_at="2026-09-03T10:00:00Z"),
        ],
        d,
    )
    store.update_record("threads:done", {"status": store.STATUS_ANSWERED}, d)
    store.update_record("threads:skip", {"status": store.STATUS_IGNORED}, d)

    assert [r["id"] for r in store.pending(d)] == ["threads:early", "threads:late"]


def test_cursor_roundtrip(d: Path):
    assert store.get_cursor("bluesky", d) == ""
    store.set_cursor("bluesky", "2026-09-05T00:00:00Z", d)
    store.set_cursor("threads", "abc", d)
    assert store.get_cursor("bluesky", d) == "2026-09-05T00:00:00Z"
    assert store.get_cursor("threads", d) == "abc"


def test_cursor_broken_file_degrades_to_empty(d: Path):
    d.mkdir(parents=True)
    (d / store.CURSORS_FILENAME).write_text("{ broken", encoding="utf-8")
    assert store.get_cursor("bluesky", d) == ""


def test_inbox_dir_prefers_env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("SNS_INBOX_DIR", str(tmp_path / "elsewhere"))
    assert store.inbox_dir() == tmp_path / "elsewhere"


def test_inbox_dir_default_is_git_ignored(monkeypatch):
    """既定パスが git の追跡対象に入らないこと。

    inbox には第三者が書いた本文とハンドル名が入る。このリポジトリは public
    なので、既定パスがうっかりコミットできる場所だと事故が「起こりうる」まま
    残る。パス名の見た目ではなく `git check-ignore` で実際に確認する。
    """
    monkeypatch.delenv("SNS_INBOX_DIR", raising=False)
    default = store.inbox_dir()
    assert "data" not in default.relative_to(store.REPO_ROOT).parts

    probe = default / store.INBOX_FILENAME
    result = subprocess.run(
        ["git", "check-ignore", "-q", str(probe)],
        cwd=store.REPO_ROOT, capture_output=True,
    )
    if result.returncode == 128:
        pytest.skip("git リポジトリとして読めない環境")
    assert result.returncode == 0, f"{probe} が .gitignore で除外されていない"
