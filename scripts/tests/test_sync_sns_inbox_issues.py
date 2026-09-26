"""inbox -> issue 同期のテスト (#7589)。

gh は叩かない。呼び出し列を記録する fake に差し替えて、
「いつ立てるか / いつ立てないか / いつ閉じるか」だけを固定する。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sns_inbox_store as store  # noqa: E402
import sync_sns_inbox_issues as sync  # noqa: E402

REPO = "omochairo/amazon-home-ops"


class FakeGh:
    """gh api の代役。issues 一覧は listing で与え、書き込みは calls に貯める。"""

    def __init__(self, listing: list[dict] | None = None, comments: dict[int, list[dict]] | None = None):
        self.listing = listing or []
        self.comments = comments or {}
        self.calls: list[tuple[list[str], dict | None]] = []
        self._next_number = 100

    def __call__(self, args: list[str], payload: dict | None = None):
        self.calls.append((args, payload))
        if payload is None:  # 一覧の GET
            page = next(
                (a.split("=", 1)[1] for a in args if a.startswith("page=")), "1",
            )
            if args[0].endswith("/comments"):
                number = int(args[0].split("/")[-2])
                return self.comments.get(number, []) if page == "1" else []
            return self.listing if page == "1" else []
        if args[0].endswith("/issues") and "POST" in args:
            self._next_number += 1
            return {"number": self._next_number}
        return {}

    def posts_to(self, suffix: str) -> list[dict]:
        return [p for a, p in self.calls if p is not None and a[0].endswith(suffix)]


@pytest.fixture()
def d(tmp_path: Path) -> Path:
    return tmp_path / "inbox"


def _add(d: Path, rid: str = "threads:1", *, text: str = "こんにちは", drafts=None) -> dict:
    rec = store.new_record(
        channel="threads", kind="reply", native_id=rid.split(":", 1)[1],
        text=text, author="someone", created_at="2026-09-01T00:00:00Z",
    )
    store.append_record(rec, d)
    if drafts:
        for text_ in drafts:
            store.add_draft(rec["id"], text_, "claude-sonnet-4-6", d)
    return store.load_records(d)[rec["id"]]


# --------------------------------------------------------------------------
# 起票
# --------------------------------------------------------------------------

def test_creates_one_issue_per_pending_record(d: Path):
    _add(d, "threads:1")
    _add(d, "threads:2")
    gh = FakeGh()

    stats = sync.sync(REPO, directory=d, gh=gh)

    assert stats["created"] == 2
    bodies = gh.posts_to("/issues")
    assert len(bodies) == 2
    assert all(b["labels"] == [sync.DEFAULT_LABEL] for b in bodies)


def test_issue_number_is_written_back_to_the_record(d: Path):
    _add(d, "threads:1")
    sync.sync(REPO, directory=d, gh=FakeGh())
    assert isinstance(store.load_records(d)["threads:1"]["issue_number"], int)


def test_existing_issue_number_is_not_reissued(d: Path):
    _add(d, "threads:1")
    gh = FakeGh()
    sync.sync(REPO, directory=d, gh=gh)
    again = FakeGh()
    stats = sync.sync(REPO, directory=d, gh=again)
    assert stats["created"] == 0
    assert again.posts_to("/issues") == []


def test_marker_in_existing_issue_prevents_a_second_issue(d: Path):
    """commit されずに落ちた前 run の起票を拾う (二重起票の防波堤)。

    レコードに issue_number が無くても、リポジトリ側に同じ id のマーカーが
    在れば立て直さない。
    """
    _add(d, "threads:1")
    gh = FakeGh([
        {"number": 42, "body": sync.marker("threads:1") + "\n本文"},
    ])

    stats = sync.sync(REPO, directory=d, gh=gh)

    assert stats["created"] == 0
    assert stats["adopted"] == 1
    assert store.load_records(d)["threads:1"]["issue_number"] == 42


def test_closed_issue_with_marker_also_counts_as_known(d: Path):
    """close 済みも読む。読まないと answered のものを立て直す。"""
    _add(d, "threads:1")
    gh = FakeGh([{"number": 7, "state": "closed", "body": sync.marker("threads:1")}])
    assert sync.sync(REPO, directory=d, gh=gh)["created"] == 0


def test_listing_asks_for_state_all(d: Path):
    _add(d, "threads:1")
    gh = FakeGh()
    sync.sync(REPO, directory=d, gh=gh)
    get_args = gh.calls[0][0]
    assert "state=all" in get_args
    assert f"labels={sync.DEFAULT_LABEL}" in get_args


def test_pull_requests_in_the_listing_are_ignored(d: Path):
    """issues エンドポイントは PR も返す。マーカーを PR から拾わない。"""
    _add(d, "threads:1")
    gh = FakeGh([
        {"number": 9, "body": sync.marker("threads:1"), "pull_request": {"url": "x"}},
    ])
    assert sync.sync(REPO, directory=d, gh=gh)["created"] == 1


# --------------------------------------------------------------------------
# バースト上限
# --------------------------------------------------------------------------

def test_limit_caps_new_issues_per_run(d: Path):
    for i in range(7):
        _add(d, f"threads:{i}")
    gh = FakeGh()

    stats = sync.sync(REPO, directory=d, gh=gh, limit=5)

    assert stats["created"] == 5
    assert stats["deferred"] == 2


def test_limit_keeps_the_oldest_first(d: Path):
    old = store.new_record(
        channel="threads", kind="reply", native_id="old", text="古い",
        created_at="2026-01-01T00:00:00Z",
    )
    new = store.new_record(
        channel="threads", kind="reply", native_id="new", text="新しい",
        created_at="2026-09-01T00:00:00Z",
    )
    store.append_record(new, d)
    store.append_record(old, d)
    gh = FakeGh()

    sync.sync(REPO, directory=d, gh=gh, limit=1)

    assert "threads:old" in gh.posts_to("/issues")[0]["body"]


def test_dry_run_writes_nothing(d: Path):
    _add(d, "threads:1")
    gh = FakeGh()
    stats = sync.sync(REPO, directory=d, gh=gh, dry_run=True)
    assert stats["created"] == 1
    assert gh.posts_to("/issues") == []
    assert "issue_number" not in store.load_records(d)["threads:1"]


# --------------------------------------------------------------------------
# 案の追記
# --------------------------------------------------------------------------

def test_new_drafts_are_appended_as_a_comment(d: Path):
    _add(d, "threads:1")
    sync.sync(REPO, directory=d, gh=FakeGh())  # 案 0 件で起票
    store.add_draft("threads:1", "案です", "claude-sonnet-4-6", d)

    gh = FakeGh()
    stats = sync.sync(REPO, directory=d, gh=gh)

    assert stats["commented"] == 1
    assert "案です" in gh.posts_to("/comments")[0]["body"]


def test_drafts_already_in_the_body_are_not_re_commented(d: Path):
    _add(d, "threads:1", drafts=["案A"])
    sync.sync(REPO, directory=d, gh=FakeGh())  # 案 1 件を載せて起票

    gh = FakeGh()
    assert sync.sync(REPO, directory=d, gh=gh)["commented"] == 0


def test_adopted_issue_counts_drafts_from_its_body(d: Path):
    """採用した issue の body に既に載っている案を数え直す。

    0 と決め打ちすると全案をもう一度コメントし、件数を決め打ちすると
    本当に増えた案を落とす。
    """
    _add(d, "threads:1", drafts=["案A"])
    body = sync.issue_body(store.load_records(d)["threads:1"])
    gh = FakeGh([{"number": 42, "body": body}])

    stats = sync.sync(REPO, directory=d, gh=gh)

    assert stats["adopted"] == 1
    assert stats["commented"] == 0
    assert store.load_records(d)["threads:1"]["issue_synced_drafts"] == 1


def test_adopted_issue_counts_drafts_already_added_as_comments(d: Path):
    """起票後にコメントで足した案も数える。本文だけ見ると状態を失ったあとの
    再同期で案 2 をもう一度コメントする (#8287)。"""
    _add(d, "threads:1", drafts=["案A"])
    body = sync.issue_body(store.load_records(d)["threads:1"])
    store.add_draft("threads:1", "案B", "claude-sonnet-4-6", d)
    comment = sync.draft_comment(store.load_records(d)["threads:1"], 1)
    gh = FakeGh([{"number": 42, "body": body}], comments={42: [{"body": comment}]})

    stats = sync.sync(REPO, directory=d, gh=gh)

    assert stats["adopted"] == 1
    assert stats["commented"] == 0
    assert gh.posts_to("/comments") == []
    assert store.load_records(d)["threads:1"]["issue_synced_drafts"] == 2


def test_adopted_issue_still_comments_really_new_drafts(d: Path):
    _add(d, "threads:1", drafts=["案A", "案B"])
    rec = store.load_records(d)["threads:1"]
    body = sync.issue_body({**rec, "drafts": rec["drafts"][:1]})
    gh = FakeGh([{"number": 42, "body": body}])

    stats = sync.sync(REPO, directory=d, gh=gh)

    assert stats["commented"] == 1
    assert "**案 2**" in gh.posts_to("/comments")[0]["body"]


def test_count_drafts_ignores_lookalike_lines_inside_a_draft():
    """案の本文 (``` の中) に同じ書式の行があっても数えない (#8287)。"""
    rec = {
        "id": "threads:1", "channel": "threads", "kind": "reply", "author": "a",
        "text": "**案 9** (x)", "created_at": "2026-09-01T00:00:00Z", "permalink": "",
        "drafts": [{"text": "前置き\n**案 7** (m)\n後ろ", "model": "m"}],
    }
    assert sync.count_drafts_in_body(sync.issue_body(rec)) == 1


def test_count_drafts_in_body_matches_render_record():
    rec = {
        "id": "threads:1", "channel": "threads", "kind": "reply", "author": "a",
        "text": "本文", "created_at": "2026-09-01T00:00:00Z", "permalink": "",
        "drafts": [{"text": "1", "model": "m"}, {"text": "2", "model": "m"}],
    }
    assert sync.count_drafts_in_body(sync.issue_body(rec)) == 2


# --------------------------------------------------------------------------
# close
# --------------------------------------------------------------------------

def test_answered_record_closes_its_issue(d: Path):
    _add(d, "threads:1")
    sync.sync(REPO, directory=d, gh=FakeGh())
    store.update_record(
        "threads:1", {"status": store.STATUS_ANSWERED, "answered_at": "2026-09-10T00:00:00Z"}, d,
    )

    gh = FakeGh()
    stats = sync.sync(REPO, directory=d, gh=gh)

    assert stats["closed"] == 1
    patch = [p for a, p in gh.calls if p and "PATCH" in a][0]
    assert patch["state"] == "closed"


def test_ignored_record_closes_its_issue(d: Path):
    _add(d, "threads:1")
    sync.sync(REPO, directory=d, gh=FakeGh())
    store.update_record("threads:1", {"status": store.STATUS_IGNORED}, d)
    assert sync.sync(REPO, directory=d, gh=FakeGh())["closed"] == 1


def test_close_happens_once(d: Path):
    _add(d, "threads:1")
    sync.sync(REPO, directory=d, gh=FakeGh())
    store.update_record("threads:1", {"status": store.STATUS_IGNORED}, d)
    sync.sync(REPO, directory=d, gh=FakeGh())

    gh = FakeGh()
    assert sync.sync(REPO, directory=d, gh=gh)["closed"] == 0


def test_answered_without_an_issue_is_not_opened_just_to_close_it(d: Path):
    rec = _add(d, "threads:1")
    store.update_record(rec["id"], {"status": store.STATUS_ANSWERED}, d)
    gh = FakeGh()
    stats = sync.sync(REPO, directory=d, gh=gh)
    assert stats == {"created": 0, "commented": 0, "closed": 0, "adopted": 0,
                     "deferred": 0, "retired": 0}


# --------------------------------------------------------------------------
# 置き場所のガード
# --------------------------------------------------------------------------

def test_public_repo_is_refused(capsys):
    assert sync.main(["--repo", "omochairo/amazon"]) == 2


def test_repo_is_required(monkeypatch, capsys):
    monkeypatch.delenv("SNS_ISSUE_REPO", raising=False)
    assert sync.main([]) == 2


def test_body_carries_the_marker_and_the_send_command(d: Path):
    _add(d, "threads:1", drafts=["案A"])
    body = sync.issue_body(store.load_records(d)["threads:1"])
    assert sync.marker("threads:1") in body
    assert "--id threads:1 --draft 1" in body


def test_title_is_truncated_and_single_line(d: Path):
    rec = _add(d, "threads:1", text="とても長い本文" * 10 + "\n改行あり")
    title = sync.issue_title(rec)
    assert "\n" not in title
    assert len(title) < 80
    assert title.startswith("[sns-reply] threads / @someone")


def test_sync_prints_no_third_party_text(d: Path, capsys, monkeypatch):
    """Actions のログに相手の本文を残さない (public repo のログ方針と同じ)。"""
    _add(d, "threads:1", text="ひみつの本文", drafts=["ひみつの案"])
    monkeypatch.setenv("SNS_INBOX_DIR", str(d))
    monkeypatch.setattr(sync, "gh_json", FakeGh())
    assert sync.main(["--repo", REPO]) == 0
    out = capsys.readouterr()
    assert "ひみつ" not in out.out + out.err


def test_gh_failure_exits_one(d: Path, monkeypatch, capsys):
    _add(d, "threads:1")
    monkeypatch.setenv("SNS_INBOX_DIR", str(d))

    def boom(args, payload=None):
        raise sync.GhError("403")

    monkeypatch.setattr(sync, "gh_json", boom)
    assert sync.main(["--repo", REPO]) == 1


def test_json_round_trip_of_the_new_fields(d: Path):
    """issue_number / issue_synced_drafts が JSONL に載ること。"""
    _add(d, "threads:1")
    sync.sync(REPO, directory=d, gh=FakeGh())
    last = [json.loads(x) for x in (d / store.INBOX_FILENAME).read_text(encoding="utf-8").splitlines() if x.strip()][-1]
    assert last["issue_number"] > 0
    assert last["issue_synced_drafts"] == 0


def test_limit_zero_closes_without_creating(d: Path):
    """送信レーン (29-sns-reply-send.yml) は --limit 0 で呼ぶ。

    送った 1 件の issue を閉じたいだけで、そこで新規起票はしない。
    """
    _add(d, "threads:1")
    _add(d, "threads:2")
    sync.sync(REPO, directory=d, gh=FakeGh())  # 両方とも起票済みにする
    store.update_record("threads:1", {"status": store.STATUS_ANSWERED}, d)

    gh = FakeGh()
    stats = sync.sync(REPO, directory=d, gh=gh, limit=0)

    assert stats["created"] == 0
    assert stats["closed"] == 1
    assert gh.posts_to("/issues") == []


def test_limit_zero_does_not_create_for_unissued_records(d: Path):
    _add(d, "threads:1")
    gh = FakeGh()
    stats = sync.sync(REPO, directory=d, gh=gh, limit=0)
    assert stats["created"] == 0
    assert stats["deferred"] == 1
    assert gh.posts_to("/issues") == []


# --------------------------------------------------------------------------
# close は「返さない」の意思表示
# --------------------------------------------------------------------------

def test_human_closing_the_issue_retires_the_record(d: Path):
    """close しただけで未対応から外れる。

    ここが無いと status は drafted のまま残り、PENDING.md の「未対応 n 件」が
    永久に減らない (人が ignored を付ける手段は他に無い)。
    """
    _add(d, "threads:1")
    gh = FakeGh()
    sync.sync(REPO, directory=d, gh=gh)
    number = store.load_records(d)["threads:1"]["issue_number"]

    closed = FakeGh([{"number": number, "state": "closed", "body": sync.marker("threads:1")}])
    stats = sync.sync(REPO, directory=d, gh=closed)

    assert stats["retired"] == 1
    rec = store.load_records(d)["threads:1"]
    assert rec["status"] == store.STATUS_IGNORED
    assert rec["issue_closed"] is True


def test_retired_record_is_not_re_issued(d: Path):
    _add(d, "threads:1")
    sync.sync(REPO, directory=d, gh=FakeGh())
    listing = [{"number": 101, "state": "closed", "body": sync.marker("threads:1")}]
    sync.sync(REPO, directory=d, gh=FakeGh(listing))

    gh = FakeGh(listing)
    stats = sync.sync(REPO, directory=d, gh=gh)
    assert stats == {"created": 0, "commented": 0, "closed": 0, "adopted": 0,
                     "deferred": 0, "retired": 0}
    assert gh.posts_to("/issues") == []


def test_retiring_does_not_re_close_the_issue(d: Path):
    """既に閉じている issue へ close コメントを二度打たない。"""
    _add(d, "threads:1")
    sync.sync(REPO, directory=d, gh=FakeGh())
    gh = FakeGh([{"number": 101, "state": "closed", "body": sync.marker("threads:1")}])
    sync.sync(REPO, directory=d, gh=gh)
    assert gh.posts_to("/comments") == []


def test_open_issue_does_not_retire_the_record(d: Path):
    _add(d, "threads:1")
    sync.sync(REPO, directory=d, gh=FakeGh())
    gh = FakeGh([{"number": 101, "state": "open", "body": sync.marker("threads:1")}])
    assert sync.sync(REPO, directory=d, gh=gh)["retired"] == 0


def test_dry_run_does_not_retire(d: Path):
    _add(d, "threads:1")
    sync.sync(REPO, directory=d, gh=FakeGh())
    gh = FakeGh([{"number": 101, "state": "closed", "body": sync.marker("threads:1")}])
    sync.sync(REPO, directory=d, gh=gh, dry_run=True)
    assert store.load_records(d)["threads:1"]["status"] == store.STATUS_NEW


def test_answered_record_is_not_retired_by_a_closed_issue(d: Path):
    """送信済みを ignored に落とさない (status は後退させない)。"""
    _add(d, "threads:1")
    sync.sync(REPO, directory=d, gh=FakeGh())
    store.update_record("threads:1", {"status": store.STATUS_ANSWERED}, d)
    gh = FakeGh([{"number": 101, "state": "closed", "body": sync.marker("threads:1")}])
    sync.sync(REPO, directory=d, gh=gh)
    assert store.load_records(d)["threads:1"]["status"] == store.STATUS_ANSWERED
