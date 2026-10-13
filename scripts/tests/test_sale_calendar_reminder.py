"""#9449: sale_calendar_reminder.py (セールの日程登録を促す issue) の単体テスト。

固定するのは:
1. 時期の判定: 目安の N 日前から鳴る / 過ぎても猶予の間は鳴る / 年またぎ
2. 登録済み (sale_events.yaml に <id>-<年> がある) なら鳴らない
3. 重複させない: 既存 issue (open / closed) があれば立てない。マーカーに触れただけの
   issue は既存とみなさない。1 run で立てるのは 1 件まで
4. 検索に失敗したら立てない (例外で落ちる)
5. data/sale_calendar.yaml の実物が書き方どおりで、登録済みのイベント id と対応する
6. 公式フィードで発表を見つけたとき: 候補つきで立てる / 開いている issue には 1 回だけ
   コメントする / 閉じた issue には何もしない / フィードを読めなければ非ゼロで終わる
   (題名の読み取りそのものは test_sale_announcements.py)
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import sale_announcements as sa  # noqa: E402
from scripts import sale_calendar_reminder as scr  # noqa: E402


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """既定では「発表なし」。外へ取りに行かせない (検知のテストは個別に差し替える)。"""
    def refuse(url):
        raise AssertionError("テストから外部へ取りに行こうとした: {}".format(url))
    monkeypatch.setattr(sa, "fetch", refuse)
    monkeypatch.setattr(sa, "collect", lambda calendar, registered, today: ({}, []))

CAL = [
    {"id": "prime-day", "name": "Amazon プライムデー", "typical_start": "07-08",
     "remind_days_before": 21, "note": "例年 7 月中旬"},
    {"id": "hatsuuri", "name": "Amazon 初売り", "typical_start": "01-03",
     "remind_days_before": 21},
]


def _ids(rows):
    return [r["event_id"] for r in rows]


# ---- 1. 時期の判定 ----

@pytest.mark.parametrize("today, expected", [
    ("2027-06-16", []),                     # 22 日前: まだ
    ("2027-06-17", ["prime-day-2027"]),     # 21 日前: 鳴りはじめ
    ("2027-07-08", ["prime-day-2027"]),     # 目安の当日
    ("2027-08-05", ["prime-day-2027"]),     # 目安 + 28 日: 猶予の最終日
    ("2027-08-06", []),                     # 猶予を過ぎたら黙る
])
def test_window_boundaries(today, expected):
    assert _ids(scr.due_reminders(CAL, [], dt.date.fromisoformat(today))) == expected


def test_year_boundary_reminds_for_next_year():
    # 1/3 のセールは前年の 12/13 から知らせる。id の年は開催年
    assert _ids(scr.due_reminders(CAL, [], dt.date(2026, 12, 12))) == []
    assert _ids(scr.due_reminders(CAL, [], dt.date(2026, 12, 13))) == ["hatsuuri-2027"]
    assert _ids(scr.due_reminders(CAL, [], dt.date(2027, 1, 20))) == ["hatsuuri-2027"]


def test_default_remind_days():
    cal = [{"id": "x", "name": "X", "typical_start": "07-08"}]
    assert scr.due_reminders(cal, [], dt.date(2027, 6, 16)) == []
    assert _ids(scr.due_reminders(cal, [], dt.date(2027, 6, 17))) == ["x-2027"]


def test_overlapping_sales_are_sorted_by_typical_start():
    cal = [
        {"id": "b", "name": "B", "typical_start": "07-20"},
        {"id": "a", "name": "A", "typical_start": "07-08"},
    ]
    assert _ids(scr.due_reminders(cal, [], dt.date(2027, 7, 1))) == ["a-2027", "b-2027"]


# ---- 2. 登録済みなら鳴らない ----

def test_registered_event_is_not_reminded():
    events = [{"id": "prime-day-2027", "phases": []}]
    assert scr.due_reminders(CAL, events, dt.date(2027, 7, 1)) == []


def test_other_years_registration_does_not_silence_this_year():
    events = [{"id": "prime-day-2026", "phases": []}]
    assert _ids(scr.due_reminders(CAL, events, dt.date(2027, 7, 1))) == ["prime-day-2027"]


# ---- 書き方の検証 ----

@pytest.mark.parametrize("entry", [
    {"id": "Prime_Day", "name": "X", "typical_start": "07-08"},
    {"id": "x", "name": "", "typical_start": "07-08"},
    {"id": "x", "name": "X", "typical_start": "7-8"},
    {"id": "x", "name": "X", "typical_start": "13-01"},
    {"id": "x", "name": "X", "typical_start": "02-29"},
    {"id": "x", "name": "X", "typical_start": 708},
    {"id": "x", "name": "X", "typical_start": "07-08", "announce_names": "X セール"},
    {"id": "x", "name": "X", "typical_start": "07-08", "announce_names": [""]},
    {"id": "x", "name": "X", "typical_start": "07-08", "remind_days_before": 0},
    {"id": "x", "name": "X", "typical_start": "07-08", "remind_days_before": 91},
    {"id": "x", "name": "X", "typical_start": "07-08", "remind_days_before": True},
    "prime-day",
])
def test_invalid_calendar_entry_is_rejected(entry):
    with pytest.raises(scr.CalendarError):
        scr.due_reminders([entry], [], dt.date(2027, 7, 1))


def test_duplicate_calendar_id_is_rejected():
    with pytest.raises(scr.CalendarError):
        scr.validate_calendar([CAL[0], dict(CAL[0])])


# ---- 本文 ----

def test_body_has_marker_event_id_and_no_date_claim():
    row = scr.due_reminders(CAL, [], dt.date(2027, 7, 1))[0]
    body = scr.render_body(row, dt.date(2027, 7, 1))
    assert body.startswith("<!-- sale-calendar-reminder:prime-day-2027 -->")
    assert "`prime-day-2027`" in body
    assert "hugo/data/sale_events.yaml" in body
    # 目安であって今年の日程ではない、と書いてあること
    assert "目安" in body and "公式の告知で日程を確認" in body
    assert scr.title_for(row) == "[sale] Amazon プライムデー 2027 の日程を確認して告知に登録する"


# ---- 3. 重複させない / 4. 検索に失敗したら立てない ----

class FakeGh:
    """_gh の差し替え。search の応答を決め、issue create の呼び出しを記録する。"""

    def __init__(self, existing=None, search_error=None):
        self.existing = existing or {}   # marker -> items
        self.search_error = search_error
        self.created = []
        self.searched = []

    def __call__(self, args):
        if args[:2] == ["api", "-X"]:
            if self.search_error:
                raise self.search_error
            query = next(a for a in args if a.startswith("q="))
            self.searched.append(query)
            for marker, items in self.existing.items():
                if '"{}"'.format(marker) in query:
                    return json.dumps({"items": items})
            return json.dumps({"items": []})
        if args[:2] == ["issue", "create"]:
            self.created.append(args)
            return "https://github.com/o/r/issues/1\n"
        raise AssertionError("想定外の gh 呼び出し: {}".format(args))


@pytest.fixture
def files(tmp_path, monkeypatch):
    cal = tmp_path / "sale_calendar.yaml"
    events = tmp_path / "sale_events.yaml"
    cal.write_text(
        '- id: a\n  name: A\n  typical_start: "07-08"\n'
        '- id: b\n  name: B\n  typical_start: "07-20"\n', encoding="utf-8")
    events.write_text("[]\n", encoding="utf-8")
    monkeypatch.setattr(scr, "CALENDAR", cal)
    monkeypatch.setattr(scr, "SALE_EVENTS", events)
    return events


def _run(monkeypatch, gh, *argv):
    monkeypatch.setattr(scr, "_gh", gh)
    return scr.main(["--repo", "o/r", "--today", "2027-07-01", *argv])


def test_creates_only_one_issue_per_run(files, monkeypatch):
    gh = FakeGh()
    assert _run(monkeypatch, gh) == 0
    assert len(gh.created) == 1
    args = gh.created[0]
    assert args[args.index("--title") + 1].startswith("[sale] A 2027")
    assert args[args.index("--label") + 1] == "todo"
    assert "<!-- sale-calendar-reminder:a-2027 -->" in args[args.index("--body") + 1]


def test_search_covers_closed_issues(files, monkeypatch):
    gh = FakeGh()
    _run(monkeypatch, gh)
    assert gh.searched and all("is:open" not in q for q in gh.searched)


def test_existing_issue_suppresses_and_next_sale_is_created(files, monkeypatch):
    marker = "sale-calendar-reminder:a-2027"
    gh = FakeGh(existing={marker: [
        {"number": 10, "state": "closed", "body": "<!-- {} -->\n本文".format(marker)}]})
    assert _run(monkeypatch, gh) == 0
    assert len(gh.created) == 1
    assert gh.created[0][gh.created[0].index("--title") + 1].startswith("[sale] B 2027")


def test_issue_that_only_mentions_marker_is_not_treated_as_existing(files, monkeypatch):
    marker = "sale-calendar-reminder:a-2027"
    gh = FakeGh(existing={marker: [
        {"number": 11, "body": "{} の件について".format(marker)}]})
    _run(monkeypatch, gh)
    assert gh.created[0][gh.created[0].index("--title") + 1].startswith("[sale] A 2027")


def test_registered_and_out_of_window_create_nothing(files, monkeypatch):
    files.write_text("- id: a-2027\n- id: b-2027\n", encoding="utf-8")
    gh = FakeGh()
    assert _run(monkeypatch, gh) == 0
    assert gh.created == [] and gh.searched == []


def test_dry_run_searches_but_does_not_create(files, monkeypatch):
    gh = FakeGh()
    assert _run(monkeypatch, gh, "--dry-run") == 0
    assert gh.searched and gh.created == []


def test_search_failure_raises_and_creates_nothing(files, monkeypatch):
    gh = FakeGh(search_error=subprocess.CalledProcessError(1, ["gh"]))
    with pytest.raises(subprocess.CalledProcessError):
        _run(monkeypatch, gh)
    assert gh.created == []


def test_without_repo_refuses_to_create(files, monkeypatch):
    gh = FakeGh()
    monkeypatch.setattr(scr, "_gh", gh)
    monkeypatch.delenv("REPO", raising=False)
    assert scr.main(["--today", "2027-07-01"]) == 2
    assert gh.created == []


# ---- 5. 実物のファイル ----

def test_real_calendar_is_valid():
    calendar = scr.load_yaml(scr.CALENDAR)
    scr.validate_calendar(calendar)
    assert calendar, "data/sale_calendar.yaml が空"


def test_real_calendar_matches_registered_event_ids():
    # 登録済みのプライム感謝祭 2026 を「未登録」と誤判定しないこと
    # (id の対応が崩れると、登録済みのセールに毎年 issue が立つ)
    calendar = scr.load_yaml(scr.CALENDAR)
    events = scr.load_yaml(scr.SALE_EVENTS)
    assert "prime-kanshasai-2026" in scr.registered_ids(events)
    assert scr.due_reminders(calendar, events, dt.date(2026, 10, 13)) == []


def test_real_calendar_never_reminds_two_years_of_the_same_sale():
    calendar = scr.load_yaml(scr.CALENDAR)
    day = dt.date(2027, 1, 1)
    while day.year == 2027:
        due = [r["calendar_id"] for r in scr.due_reminders(calendar, [], day)]
        assert len(due) == len(set(due)), day
        day += dt.timedelta(days=1)


# ---- 6. 公式フィードで発表を見つけたとき ----

class FakeGhWithComments(FakeGh):
    """FakeGh に、issue のコメント一覧とコメント投稿を足したもの。"""

    def __init__(self, comments=None, **kwargs):
        super().__init__(**kwargs)
        self.comments = comments or []
        self.commented = []

    def __call__(self, args):
        if args[:3] == ["api", "-X", "GET"] and "/comments" in args[3]:
            return json.dumps(self.comments)
        if args[:2] == ["issue", "comment"]:
            self.commented.append(args)
            return "https://github.com/o/r/issues/7#issuecomment-1\n"
        return super().__call__(args)


def _detected(event_id="a-2027", feed_errors=()):
    cal_id = event_id.rsplit("-", 1)[0]
    cand = {
        "event_id": event_id, "calendar_id": cal_id, "name": cal_id.upper(), "year": 2027,
        "periods": [{"start": dt.date(2027, 7, 10), "end": dt.date(2027, 7, 13),
                     "start_time": (0, 0), "end_time": (23, 59)}],
        "sources": [{"feed": "PR TIMES", "title": "「A」を7月10日（土）から開催",
                     "link": "https://prtimes.jp/main/html/rd/p/1.html",
                     "published": dt.date(2027, 6, 15)}],
    }
    return lambda calendar, registered, today: ({event_id: cand}, list(feed_errors))


def test_detected_announcement_creates_issue_with_candidate(files, monkeypatch):
    # 目安の窓 (7/8 の 21 日前 = 6/17) より前でも、発表が出ていれば立てる
    monkeypatch.setattr(sa, "collect", _detected())
    gh = FakeGhWithComments()
    monkeypatch.setattr(scr, "_gh", gh)
    assert scr.main(["--repo", "o/r", "--today", "2027-06-15"]) == 0
    assert len(gh.created) == 1 and gh.commented == []
    args = gh.created[0]
    assert "開催が発表された" in args[args.index("--title") + 1]
    body = args[args.index("--body") + 1]
    assert body.startswith("<!-- sale-calendar-reminder:a-2027 -->\n"
                           "<!-- sale-announcement:a-2027 -->")
    assert "未確認" in body and "2027-07-10T00:00:00+09:00" in body
    assert "https://prtimes.jp/main/html/rd/p/1.html" in body


def test_detected_sale_goes_before_calendar_only_sale(files, monkeypatch):
    # a は目安の窓にも入っている。発表のあった b を先に書き、a は次の run に回す
    monkeypatch.setattr(sa, "collect", _detected("b-2027"))
    gh = FakeGhWithComments()
    assert _run(monkeypatch, gh) == 0
    assert len(gh.created) == 1
    assert "sale-announcement:b-2027" in gh.created[0][gh.created[0].index("--body") + 1]


def _open_reminder(event_id="a-2027", state="open", extra=""):
    return {"sale-calendar-reminder:{}".format(event_id): [{
        "number": 7, "state": state,
        "body": "<!-- sale-calendar-reminder:{} -->\n{}".format(event_id, extra)}]}


def test_open_reminder_issue_gets_one_comment(files, monkeypatch):
    monkeypatch.setattr(sa, "collect", _detected())
    gh = FakeGhWithComments(existing=_open_reminder())
    monkeypatch.setattr(scr, "_gh", gh)
    assert scr.main(["--repo", "o/r", "--today", "2027-06-15"]) == 0
    assert gh.created == [] and len(gh.commented) == 1
    args = gh.commented[0]
    assert args[2] == "7"
    body = args[args.index("--body") + 1]
    assert body.startswith("<!-- sale-announcement:a-2027 -->")
    assert "2027-07-10T00:00:00+09:00" in body


@pytest.mark.parametrize("gh", [
    # コメントに載せ済み
    FakeGhWithComments(existing=_open_reminder(),
                       comments=[{"body": "<!-- sale-announcement:a-2027 -->\n..."}]),
    # 本文に載せ済み (発表を見つけて立てた issue)
    FakeGhWithComments(existing=_open_reminder(extra="<!-- sale-announcement:a-2027 -->")),
    # 閉じた issue: 「今年は登録しない」を覆さない
    FakeGhWithComments(existing=_open_reminder(state="closed")),
])
def test_announcement_is_not_posted_twice_or_to_closed_issue(files, monkeypatch, gh):
    monkeypatch.setattr(sa, "collect", _detected())
    monkeypatch.setattr(scr, "_gh", gh)
    assert scr.main(["--repo", "o/r", "--today", "2027-06-15"]) == 0
    assert gh.created == [] and gh.commented == []


def test_comment_that_only_mentions_marker_does_not_count(files, monkeypatch):
    monkeypatch.setattr(sa, "collect", _detected())
    gh = FakeGhWithComments(existing=_open_reminder(),
                            comments=[{"body": "sale-announcement:a-2027 の件"}])
    monkeypatch.setattr(scr, "_gh", gh)
    assert scr.main(["--repo", "o/r", "--today", "2027-06-15"]) == 0
    assert len(gh.commented) == 1


def test_dry_run_with_announcement_writes_nothing(files, monkeypatch):
    monkeypatch.setattr(sa, "collect", _detected())
    gh = FakeGhWithComments(existing=_open_reminder())
    monkeypatch.setattr(scr, "_gh", gh)
    assert scr.main(["--repo", "o/r", "--today", "2027-06-15", "--dry-run"]) == 0
    assert gh.created == [] and gh.commented == []


def test_feed_failure_still_reminds_then_exits_nonzero(files, monkeypatch):
    # 検知が止まっても目安のリマインドは出す。ただし緑では終わらない
    monkeypatch.setattr(
        sa, "collect", lambda calendar, registered, today: ({}, ["About Amazon Japan: 403"]))
    gh = FakeGhWithComments()
    assert _run(monkeypatch, gh) == 1
    assert len(gh.created) == 1


def test_main_passes_registered_ids_to_detection(files, monkeypatch):
    files.write_text("- id: a-2027\n", encoding="utf-8")
    seen = {}

    def collect(calendar, registered, today):
        seen.update(registered=registered, today=today, ids=[e["id"] for e in calendar])
        return {}, []
    monkeypatch.setattr(sa, "collect", collect)
    assert _run(monkeypatch, FakeGhWithComments()) == 0
    assert seen == {"registered": {"a-2027"}, "today": dt.date(2027, 7, 1), "ids": ["a", "b"]}
