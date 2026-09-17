"""fetch / draft / post の 3 段のテスト。

外部 API は叩かない。パース・フィルタ・ガードだけを固定する。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import draft_sns_reply as drafter  # noqa: E402
import fetch_sns_replies as fetch  # noqa: E402
import post_sns_reply as poster  # noqa: E402
import sns_inbox_store as store  # noqa: E402


# --------------------------------------------------------------------------
# fetch: Bluesky の整形
# --------------------------------------------------------------------------

def test_bluesky_permalink_from_at_uri():
    uri = "at://did:plc:abc123/app.bsky.feed.post/3kxyz"
    assert fetch._bluesky_permalink("omochairo.bsky.social", uri) == (
        "https://bsky.app/profile/omochairo.bsky.social/post/3kxyz"
    )


@pytest.mark.parametrize(
    ("handle", "uri"),
    [("", "at://did/app.bsky.feed.post/3k"), ("h", "https://example.com/x")],
)
def test_bluesky_permalink_degrades_to_empty(handle, uri):
    assert fetch._bluesky_permalink(handle, uri) == ""


def test_bluesky_parent_uri():
    rec = {"reply": {"parent": {"uri": "at://did/app.bsky.feed.post/parent"}}}
    assert fetch._bluesky_parent(rec) == "at://did/app.bsky.feed.post/parent"
    assert fetch._bluesky_parent({}) == ""
    assert fetch._bluesky_parent({"reply": {"parent": {}}}) == ""


@pytest.mark.parametrize(
    ("value", "expected_year"),
    [("2026-09-05T10:00:00Z", 2026), ("2026-09-05T10:00:00+00:00", 2026)],
)
def test_parse_iso_accepts_both_forms(value, expected_year):
    parsed = fetch._parse_iso(value)
    assert parsed is not None
    assert parsed.year == expected_year
    assert parsed.tzinfo is not None


@pytest.mark.parametrize("value", ["", "not-a-date", None])
def test_parse_iso_rejects_garbage(value):
    assert fetch._parse_iso(value) is None


# --------------------------------------------------------------------------
# fetch: Threads の own-posts ページング (#7609)
# --------------------------------------------------------------------------

def test_fetch_own_posts_pages_until_cutoff(monkeypatch):
    """25 件で頭打ちにせず、cutoff に届くまで paging.next を辿る。"""
    responses = [
        {
            "data": [
                {"id": "p1", "timestamp": "2026-09-15T00:00:00+00:00"},
                {"id": "p2", "timestamp": "2026-09-14T00:00:00+00:00"},
            ],
            "paging": {"next": "page2"},
        },
        {
            "data": [
                {"id": "p3", "timestamp": "2026-09-01T00:00:00+00:00"},  # cutoff より前
                {"id": "p4", "timestamp": "2026-08-01T00:00:00+00:00"},
            ],
            "paging": {"next": "page3"},
        },
        {"data": [{"id": "p5", "timestamp": "2026-01-01T00:00:00+00:00"}]},
    ]
    calls = []

    def fake_request(url, **kw):
        calls.append(url)
        return responses[len(calls) - 1]

    monkeypatch.setattr(fetch, "_request", fake_request)
    cutoff = fetch._parse_iso("2026-09-10T00:00:00+00:00")

    posts = fetch._fetch_own_posts("u1", "tok", cutoff)

    assert [p["id"] for p in posts] == ["p1", "p2"]
    assert calls == [calls[0], "page2"]  # p3 で cutoff を割った時点で page3 は取りに行かない
    assert len(calls) == 2


def test_fetch_own_posts_stops_at_hard_cap(monkeypatch):
    """cutoff に届かなくても MAX_OWN_POSTS_TOTAL で必ず止まる (暴走防止)。"""
    call_count = 0

    def fake_request(url, **kw):
        nonlocal call_count
        call_count += 1
        return {
            "data": [
                {"id": f"p{call_count}-{i}", "timestamp": "2026-09-15T00:00:00+00:00"}
                for i in range(fetch.MAX_OWN_POSTS_PAGE)
            ],
            "paging": {"next": f"page{call_count + 1}"},
        }

    monkeypatch.setattr(fetch, "_request", fake_request)
    cutoff = fetch._parse_iso("2020-01-01T00:00:00+00:00")  # 絶対に届かない

    posts = fetch._fetch_own_posts("u1", "tok", cutoff)

    assert len(posts) <= fetch.MAX_OWN_POSTS_TOTAL
    assert len(posts) >= fetch.MAX_OWN_POSTS_TOTAL - fetch.MAX_OWN_POSTS_PAGE + 1


# --------------------------------------------------------------------------
# fetch: Threads の第三者同士の返信を除外する (#7609)
# --------------------------------------------------------------------------

def test_fetch_thread_replies_keeps_reply_addressed_to_self(monkeypatch):
    payload = {
        "data": [
            {
                "id": "r1", "text": "何歳から使えますか", "username": "someone",
                "timestamp": "2026-09-15T00:00:00+00:00",
                "replied_to": {"id": "media1"},
            },
        ],
    }
    monkeypatch.setattr(fetch, "_request", lambda url, **kw: payload)
    cutoff = fetch._parse_iso("2026-01-01T00:00:00+00:00")

    out = fetch._fetch_thread_replies("media1", "tok", "iromama", cutoff)

    assert [r["text"] for r in out] == ["何歳から使えますか"]


def test_fetch_thread_replies_keeps_reply_addressed_to_own_reply(monkeypatch):
    """自分の返信 (depth 2 の根) への返信も拾う (#7589 のスコープ)。"""
    payload = {
        "data": [
            {
                "id": "own_reply", "text": "うちも同じです", "username": "iromama",
                "timestamp": "2026-09-14T00:00:00+00:00",
                "replied_to": {"id": "media1"},
            },
            {
                "id": "r2", "text": "詳しく教えて", "username": "someone",
                "timestamp": "2026-09-15T00:00:00+00:00",
                "replied_to": {"id": "own_reply"},
            },
        ],
    }
    monkeypatch.setattr(fetch, "_request", lambda url, **kw: payload)
    cutoff = fetch._parse_iso("2026-01-01T00:00:00+00:00")

    out = fetch._fetch_thread_replies("media1", "tok", "iromama", cutoff)

    assert [r["text"] for r in out] == ["詳しく教えて"]


def test_fetch_thread_replies_drops_reply_between_third_parties(monkeypatch):
    """A さんが B さんに返した発言 (自分宛でない) は拾わない (#7609)。"""
    payload = {
        "data": [
            {
                "id": "a1", "text": "私はこう思う", "username": "personA",
                "timestamp": "2026-09-14T00:00:00+00:00",
                "replied_to": {"id": "media1"},
            },
            {
                "id": "b1", "text": "それは違うと思う", "username": "personB",
                "timestamp": "2026-09-15T00:00:00+00:00",
                "replied_to": {"id": "a1"},
            },
        ],
    }
    monkeypatch.setattr(fetch, "_request", lambda url, **kw: payload)
    cutoff = fetch._parse_iso("2026-01-01T00:00:00+00:00")

    out = fetch._fetch_thread_replies("media1", "tok", "iromama", cutoff)

    assert [r["text"] for r in out] == ["私はこう思う"]  # b1 (A宛) は除外


def test_fetch_thread_replies_keeps_reply_when_replied_to_field_missing(monkeypatch):
    """`replied_to` を返さない (旧仕様/フィールド欠落) 場合はフィルタせず従来通り拾う。"""
    payload = {
        "data": [
            {
                "id": "r1", "text": "本文", "username": "someone",
                "timestamp": "2026-09-15T00:00:00+00:00",
            },
        ],
    }
    monkeypatch.setattr(fetch, "_request", lambda url, **kw: payload)
    cutoff = fetch._parse_iso("2026-01-01T00:00:00+00:00")

    out = fetch._fetch_thread_replies("media1", "tok", "iromama", cutoff)

    assert [r["text"] for r in out] == ["本文"]


def test_bluesky_interesting_excludes_like_and_follow():
    """like / follow は返信対象ではない。混ぜると inbox がノイズで埋まる。"""
    assert set(fetch.BLUESKY_INTERESTING) == {"reply", "mention", "quote"}


def test_render_digest_empty():
    assert "ありません" in fetch.render_digest([])


def test_render_digest_includes_id_and_body():
    rec = store.new_record(
        channel="bluesky", kind="reply", native_id="at://x/1",
        text="これ気になります", author="someone.bsky.social",
    )
    out = fetch.render_digest([rec])
    assert "bluesky:at://x/1" in out
    assert "> これ気になります" in out
    assert "@someone.bsky.social" in out


# --------------------------------------------------------------------------
# draft: 応答パース
# --------------------------------------------------------------------------

def test_parse_response_two_drafts():
    should, reason, drafts = drafter.parse_response(
        "判定: 返信する\n理由: 具体的な質問\n案1: 我が家では2歳から使えました\n案2: 対象年齢より少し早めでも大丈夫でした",
    )
    assert should is True
    assert reason == "具体的な質問"
    assert drafts == ["我が家では2歳から使えました", "対象年齢より少し早めでも大丈夫でした"]


def test_parse_response_multiline_draft():
    should, _, drafts = drafter.parse_response(
        "判定: 返信する\n理由: x\n案1: 1行目\n続き\n案2: 別案",
    )
    assert should is True
    assert drafts == ["1行目\n続き", "別案"]


def test_parse_response_declines():
    should, reason, drafts = drafter.parse_response("判定: 返信しない\n理由: スパム")
    assert (should, reason, drafts) == (False, "スパム", [])


def test_parse_response_full_width_colon():
    should, _, drafts = drafter.parse_response("判定：返信する\n理由：ok\n案1：本文")
    assert should is True
    assert drafts == ["本文"]


@pytest.mark.parametrize(
    "raw",
    [
        "こんにちは！返信案です。",              # 判定行が無い
        "判定: 返信する\n理由: ok",              # 案が無い
        "",
    ],
)
def test_parse_response_broken_output_falls_back_to_no_reply(raw):
    """形式が崩れたら「返信しない」に倒す。

    壊れた出力から本文らしきものを拾って人に見せると、そのまま送信される。
    """
    should, _, drafts = drafter.parse_response(raw)
    assert should is False
    assert drafts == []


def test_build_prompt_carries_channel_limit_and_body():
    rec = store.new_record(channel="x", kind="mention", native_id="1", text="質問です")
    prompt = drafter.build_prompt(rec, "PERSONA-BLOCK")
    assert "PERSONA-BLOCK" in prompt
    assert "質問です" in prompt
    assert f"{drafter.CHANNEL_LIMITS['x']} 文字以内" in prompt


def test_load_persona_missing_overlay_raises(monkeypatch, tmp_path: Path):
    """jules/ overlay が無いなら起草しない (別人格で返信する方が有害)。"""
    monkeypatch.setattr(drafter, "JULES_DIR", tmp_path / "nope")
    with pytest.raises(drafter.DraftError, match="checkout されていない"):
        drafter.load_persona("x")


def test_load_persona_unknown_channel(monkeypatch):
    with pytest.raises(drafter.DraftError, match="未割当"):
        drafter.load_persona("mixi")


def test_persona_files_cover_every_channel():
    assert set(drafter.PERSONA_FILES) == set(store.CHANNELS)


# --------------------------------------------------------------------------
# post: 本文解決と二重送信ガード
# --------------------------------------------------------------------------

@pytest.fixture()
def d(tmp_path: Path) -> Path:
    return tmp_path / "inbox"


def _seed(d: Path, **kw) -> dict:
    rec = store.new_record(
        channel=kw.pop("channel", "threads"), kind="reply",
        native_id=kw.pop("native_id", "n1"), text="やあ",
    )
    store.record_new_items([rec], d)
    return rec


def test_resolve_body_prefers_explicit_body():
    rec = {"drafts": [{"text": "案1"}]}
    args = type("A", (), {"body": "  手書き  ", "draft": 1})()
    assert poster.resolve_body(rec, args) == "手書き"


def test_resolve_body_from_draft_index():
    rec = {"drafts": [{"text": "案1"}, {"text": "案2"}]}
    args = type("A", (), {"body": "", "draft": 2})()
    assert poster.resolve_body(rec, args) == "案2"


@pytest.mark.parametrize("draft", [0, 3])
def test_resolve_body_rejects_out_of_range(draft):
    rec = {"drafts": [{"text": "案1"}, {"text": "案2"}]}
    args = type("A", (), {"body": "", "draft": draft})()
    with pytest.raises(ValueError, match="範囲外"):
        poster.resolve_body(rec, args)


def test_resolve_body_requires_a_choice():
    args = type("A", (), {"body": "", "draft": None})()
    with pytest.raises(ValueError, match="--body か --draft"):
        poster.resolve_body({"drafts": []}, args)


def test_post_refuses_already_answered(monkeypatch, d: Path):
    monkeypatch.setenv("SNS_INBOX_DIR", str(d))
    rec = _seed(d)
    store.update_record(rec["id"], {"status": store.STATUS_ANSWERED}, d)

    assert poster.main(["--id", rec["id"], "--body", "もう一度", "--dry-run"]) == 2


def test_post_unknown_id_is_rejected(monkeypatch, d: Path):
    monkeypatch.setenv("SNS_INBOX_DIR", str(d))
    _seed(d)
    assert poster.main(["--id", "threads:nope", "--body", "x", "--dry-run"]) == 2


def test_post_empty_body_is_rejected(monkeypatch, d: Path):
    monkeypatch.setenv("SNS_INBOX_DIR", str(d))
    rec = _seed(d)
    assert poster.main(["--id", rec["id"], "--body", "   ", "--dry-run"]) == 2


def test_post_dry_run_does_not_change_status(monkeypatch, d: Path):
    monkeypatch.setenv("SNS_INBOX_DIR", str(d))
    rec = _seed(d)
    assert poster.main(["--id", rec["id"], "--body", "ありがとうございます", "--dry-run"]) == 0
    assert store.load_records(d)[rec["id"]]["status"] == store.STATUS_NEW


def test_post_ignored_record_can_still_be_answered_by_a_human(monkeypatch, d: Path):
    """起草側が「返信しない」と判断しても、人の判断で送れる余地を残す。"""
    monkeypatch.setenv("SNS_INBOX_DIR", str(d))
    rec = _seed(d)
    store.update_record(rec["id"], {"status": store.STATUS_IGNORED}, d)
    assert poster.main(["--id", rec["id"], "--body", "やっぱり返す", "--dry-run"]) == 0


def test_bluesky_root_ref_uses_thread_root():
    post = {
        "record": {"reply": {"root": {"uri": "at://root", "cid": "cid-root"}}},
    }
    parent = {"uri": "at://parent", "cid": "cid-parent"}
    assert poster._bluesky_root_ref(post, parent) == {"uri": "at://root", "cid": "cid-root"}


def test_bluesky_root_ref_falls_back_to_parent_when_top_level():
    parent = {"uri": "at://parent", "cid": "cid-parent"}
    assert poster._bluesky_root_ref({"record": {}}, parent) == parent


def test_post_x_is_explicitly_unwired():
    with pytest.raises(poster.PostError, match="user-context"):
        poster.post_x({"native_id": "1"}, "本文")


# --------------------------------------------------------------------------
# render_sns_pending
# --------------------------------------------------------------------------

import render_sns_pending as digest  # noqa: E402


def test_render_pending_empty_says_so():
    assert "未対応はありません" in digest.render([])


def test_render_pending_includes_send_command_per_draft():
    rec = store.new_record(
        channel="threads", kind="reply", native_id="177", text="何歳から使えますか",
        author="someone", permalink="https://www.threads.net/p/abc",
    )
    rec["drafts"] = [
        {"text": "案A", "model": "claude-sonnet-4-6"},
        {"text": "案B", "model": "claude-sonnet-4-6"},
    ]
    out = digest.render([rec])

    assert "何歳から使えますか" in out
    assert "https://www.threads.net/p/abc" in out
    assert "--id threads:177 --draft 1" in out
    assert "--id threads:177 --draft 2" in out


def test_render_pending_marks_undrafted():
    rec = store.new_record(channel="bluesky", kind="mention", native_id="at://1", text="やあ")
    assert "起草レーン待ち" in digest.render([rec])


def test_agy_argv_attaches_prompt_to_print_flag():
    """`--print` は次のトークンを食う。裸の --print の直後に別フラグを置かない。

    2026-09-05 の初回 run は `agy --print --model X "本文"` と書いていたため
    --model が prompt として解釈され、本文が無視されたまま exit 2 になった。
    """
    argv = drafter.build_agy_argv("本文プロンプト", "claude-sonnet-4-6")

    assert argv[0] == "agy"
    assert "--print" not in argv, "裸の --print は次のトークンを食う"
    assert argv[-1] == "--print=本文プロンプト"
    assert argv.index("--model") < len(argv) - 1
    assert argv[argv.index("--model") + 1] == "claude-sonnet-4-6"


def test_bluesky_uses_the_same_persona_as_x():
    """Bluesky の中身は X のミラー = いろママ。

    notify_engagement.py は channel == "x" のときだけ Bluesky にミラーし、
    直近 100 投稿の一人称も 私 10 / 僕 0、夫 3 / 妻 0 だった。

    2026-09-05 に displayName (当時「いろパパ＠おもちゃいろ」) だけを見て
    いろパパへ倒し、誤った人格の案を出した。表示名は設定の取り違えであって
    本文の実態ではない。人格は**何が投稿されているか**で決める。
    """
    assert drafter.PERSONA_FILES["bluesky"] == drafter.PERSONA_FILES["x"]
    assert drafter.PERSONA_FILES["threads"] != drafter.PERSONA_FILES["x"]


def test_redraft_replaces_existing_drafts_instead_of_appending(monkeypatch, tmp_path: Path):
    """作り直しは置き換え。没案と新案が PENDING.md に並ぶと没案を送る事故になる。"""
    d = tmp_path / "inbox"
    monkeypatch.setenv("SNS_INBOX_DIR", str(d))
    rec = store.new_record(channel="threads", kind="reply", native_id="r1", text="質問です")
    store.record_new_items([rec], d)
    store.add_draft(rec["id"], "古い案", "old-model", d)

    monkeypatch.setattr(drafter, "load_persona", lambda ch: "PERSONA")
    monkeypatch.setattr(
        drafter, "call_agy",
        lambda prompt, model, timeout_s: "判定: 返信する\n理由: ok\n案1: 新案A\n案2: 新案B",
    )

    assert drafter.main(["--redraft", "--limit", "5"]) == 0

    drafts = [x["text"] for x in store.load_records(d)[rec["id"]]["drafts"]]
    assert drafts == ["新案A", "新案B"]
    assert "古い案" not in drafts


def test_without_redraft_already_drafted_records_are_skipped(monkeypatch, tmp_path: Path):
    d = tmp_path / "inbox"
    monkeypatch.setenv("SNS_INBOX_DIR", str(d))
    rec = store.new_record(channel="threads", kind="reply", native_id="r1", text="質問です")
    store.record_new_items([rec], d)
    store.add_draft(rec["id"], "既存案", "old-model", d)

    def _boom(*a, **k):
        raise AssertionError("既に案があるものを呼んではいけない")

    monkeypatch.setattr(drafter, "call_agy", _boom)
    assert drafter.main(["--limit", "5"]) == 0
    assert [x["text"] for x in store.load_records(d)[rec["id"]]["drafts"]] == ["既存案"]
