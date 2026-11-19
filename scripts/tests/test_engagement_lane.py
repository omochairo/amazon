"""engagement_lane (navi-brain#170 B3: エンゲージ下書きレーン) のテスト。

git / gh / 書き手はすべて偽物を注入する (実物の agy・ollama・git・GitHub は呼ばない)。
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import article_writer  # noqa: E402
import engagement_lane as el  # noqa: E402
import writer_lane as wl  # noqa: E402

TODAY = "2026-11-19"
NOW = dt.datetime(2026, 11, 19, 1, 0, tzinfo=dt.timezone.utc)  # 2026-11-19 10:00 JST
LINK_A = "https://news.example.jp/articles/aaa?source=rss"
LINK_B = "https://news.example.jp/articles/bbb?source=rss"
LINK_C = "https://news.example.jp/articles/ccc?source=rss"
TREND = {"date_jst": TODAY, "top_candidates": [
    {"title": "見出しA", "link": LINK_A, "summary": "概要A"},
    {"title": "見出しB", "link": LINK_B, "summary": "概要B"},
    {"title": "見出しC", "link": LINK_C, "summary": "概要C"}]}
BODY_MARK = "本文のしるし"


def uniq(i, n=30):
    """3 文字単位の重なりが無い、互いに別の本文 (CJK の連番)。"""
    return "".join(chr(0x4E00 + i * 97 + k * 3) for k in range(n))


def proc(rc=0, out="", err=""):
    return subprocess.CompletedProcess([], rc, out, err)


def qrow(rid, ch, cat, sub, text, created, published=None, url=None):
    r = {"id": rid, "channel": ch, "persona": "iromama" if ch == "x" else "iropapa", "category": cat,
         "subcategory": sub, "text": text, "generator": "jules_v1", "created_at": created,
         "earliest_publish_at": None, "published_at": published, "post_id": None}
    if url:
        r["source_url"] = url
        r["source_title"] = "t"
    return r


def jsonl(rows):
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)


class FakeWorld:
    """git / gh の呼び出しを記録し、決めた結果を返す。"""

    def __init__(self):
        self.calls = []
        self.files = {}          # git show origin/main:<rel> が返す中身
        self.prs = []            # gh pr list --state open の結果
        self.pr_count = 0
        self.fail_pr_create = False
        self.push_rc = 0

    def __call__(self, argv, input=None, **kw):
        self.calls.append((list(argv), kw.get("cwd")))
        if argv[0] == "git":
            if argv[1] == "show":
                rel = argv[2].split(":", 1)[1]
                return proc(0, self.files[rel]) if rel in self.files else proc(128, "", "not found")
            if argv[1] == "push" and "--delete" not in argv:
                return proc(self.push_rc, "", "push rejected" if self.push_rc else "")
            return proc()
        if argv[0] == "gh":
            a = argv[1:]
            if a[:2] == ["pr", "list"]:
                return proc(0, json.dumps(self.prs))
            if a[:2] == ["pr", "create"]:
                if self.fail_pr_create:
                    return proc(1, "", "gh failed")
                self.pr_count += 1
                return proc(0, "https://github.com/omochairo/amazon/pull/%d\n" % (9000 + self.pr_count))
            if a[:2] == ["pr", "close"]:
                return proc(0)
            raise AssertionError("unexpected gh %s" % a)
        raise AssertionError("unexpected command %s" % argv)

    def cmds(self, tool, *prefix):
        return [c for c, _ in self.calls if c[0] == tool and c[1:1 + len(prefix)] == list(prefix)]


def wresult(posts=None, error="", web_calls=0, reset_s=None, usage=None):
    article = None if posts is None else {"posts": posts}
    return article_writer.WriteResult(
        ok=article is not None and not error, article=article, text="```json {} ```", error=error,
        error_text=error, usage=usage or {"total_tokens": 10000}, secs=30.0, backend="agy",
        model="gemini-4-hydrogen-high", reset_s=reset_s, conversation_id="cid",
        tool_calls={"read_file": 1}, web_calls=web_calls)


class Writer:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []
        self.prompts = []

    def __call__(self, prompt, **kw):
        self.calls.append(kw)
        self.prompts.append(prompt)
        r = self.results.pop(0)
        return r(kw) if callable(r) else r


def np(ch, sub, i, url=None):
    """news の post"""
    d = {"channel": ch, "subcategory": sub, "text": uniq(i)}
    d["source_url"] = url or LINK_A
    d["source_title"] = "writer title"
    return d


def dp(sub, i):
    return {"channel": "x", "subcategory": sub, "text": uniq(i)}


@pytest.fixture()
def lane(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    brain = tmp_path / "brain"
    brain.mkdir()
    (brain / "PROMPT_ENGAGEMENT_NEWS_DUAL.md").write_text("ORIGINAL NEWS PROMPT\n", encoding="utf-8")
    (brain / "PROMPT_ENGAGEMENT_X_IROMAMA_DAILY.md").write_text("ORIGINAL X DAILY PROMPT\n", encoding="utf-8")
    (brain / "PROMPT_ENGAGEMENT_THREADS_IROPAPA_DAILY.md").write_text("ORIGINAL TH DAILY PROMPT\n", encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "nolocal"))
    state = tmp_path / "state"
    world = FakeWorld()
    # 既定: 空のキュー + 今日のトレンド (X は 6 本の未投稿 daily を持たせず、各テストで上書き)
    world.files["data/engagement_queue_x.jsonl"] = ""
    world.files["data/engagement_queue_threads.jsonl"] = ""
    world.files["data/engagement_trends/%s.json" % TODAY] = json.dumps(TREND, ensure_ascii=False)
    (repo / "data/engagement_queue_x.jsonl").write_text("", encoding="utf-8")
    (repo / "data/engagement_trends").mkdir()
    (repo / ("data/engagement_trends/%s.json" % TODAY)).write_text(json.dumps(TREND, ensure_ascii=False), encoding="utf-8")

    def set_queue(ch, rows):
        rel = "data/engagement_queue_%s.jsonl" % ch
        world.files[rel] = jsonl(rows)
        (repo / rel).write_text(jsonl(rows), encoding="utf-8")

    # 既定では Threads の daily は足りている (未投稿 6 本)。X の補充だけを見るテストが多いため
    set_queue("threads", [qrow("eng-th-2026-11-18-%03d" % i, "threads", "daily", "observation", uniq(2000 + i),
                               "2026-11-18T10:00:00+09:00") for i in range(1, 7)])

    def run(writer, *extra, clock=NOW, backend_args=()):
        env = el.LaneEnv(repo, runner=world, writer=writer, python="py",
                         clock=clock if callable(clock) else (lambda: clock))
        args = parse(["run", "--repo-dir", str(repo), "--brain-dir", str(brain), "--state-dir", str(state)]
                     + list(extra))
        return el.cmd_run(args, env)

    return SimpleNamespace(repo=repo, brain=brain, state=state, world=world, run=run, set_queue=set_queue)


def parse(argv):
    captured = {}

    def fake_run(args, env):
        captured["args"] = args
        return 0

    orig = el.cmd_run
    el.cmd_run = fake_run
    try:
        el.main(argv)
    finally:
        el.cmd_run = orig
    return captured["args"]


def ledger(lane):
    return wl.read_ledger(lane.state)


def events(lane, name):
    return [r for r in ledger(lane) if r["event"] == name]


def queue_lines(lane, ch):
    return (lane.repo / ("data/engagement_queue_%s.jsonl" % ch)).read_text(encoding="utf-8").splitlines()


# ---------------------------------------------------------------- id の採番

def test_next_seq_continues_from_main_and_open_pr_per_prefix():
    rows = {"x": [qrow("news-x-2026-11-19-003", "x", "news", "a", "t", "2026-11-19T06:00:00+09:00"),
                  qrow("news-x-2026-11-18-009", "x", "news", "a", "t", "2026-11-18T06:00:00+09:00"),
                  qrow("eng-x-2026-11-19-012", "x", "daily", "vent", "t", "2026-11-19T06:00:00+09:00")],
            "threads": [qrow("news-th-2026-11-19-001", "threads", "news", "a", "t", "2026-11-19T06:00:00+09:00")]}
    assigned = {}
    assert el.next_seq(rows, "news-x", TODAY, assigned) == 4       # main の同日最大 3 の続き
    assert el.next_seq(rows, "news-th", TODAY, assigned) == 2      # チャネルごとに別の接頭辞
    assert el.next_seq(rows, "eng-x", TODAY, assigned) == 13
    assert el.next_seq(rows, "eng-th", TODAY, assigned) == 1       # 無ければ 001
    assert el.next_seq(rows, "news-x", "2026-11-20", assigned) == 1  # 日付が変われば 001 に戻る
    assigned[("news-x", TODAY)] = 7                                 # この回で割り当て済みの続き
    assert el.next_seq(rows, "news-x", TODAY, assigned) == 8


def test_ids_include_open_pr_rows(lane):
    lane.set_queue("x", [qrow("news-x-2026-11-19-001", "x", "news", "a", uniq(900), "2026-11-19T06:00:00+09:00",
                              url=LINK_C)])
    pend = lane.state / "pending"
    pend.mkdir(parents=True)
    (pend / ("news-%s.json" % TODAY)).write_text(json.dumps(
        {"rows": {"x": [qrow("news-x-2026-11-19-002", "x", "news", "b", uniq(901), "2026-11-19T08:00:00+09:00")],
                  "threads": []}}), encoding="utf-8")
    # open PR (news) があると news は作らない: id 採番は daily で見る
    lane.world.prs = [{"number": 7, "url": "u", "headRefName": "k8-engagement/news-" + TODAY,
                       "mergeable": "MERGEABLE", "createdAt": "2026-11-19T00:00:00Z"}]
    w = Writer([wresult([dp("vent", i) for i in range(10)])])
    assert lane.run(w, "--only", "daily") == 0
    ids = [json.loads(line)["id"] for line in queue_lines(lane, "x")]
    assert ids[-1] == "eng-x-2026-11-19-010" and ids[0] == "news-x-2026-11-19-001"


# ---------------------------------------------------------------- 行の組み立て

def test_assemble_rows_shape_keys_and_created_at():
    ctx = {"rows": {"x": [], "threads": []}}
    args = SimpleNamespace(backend="agy", model="gemini-4-hydrogen-high")
    clock = {"n": 0}
    drafts = [{"channel": "x", "subcategory": "台風", "text": "あ" * 10, "source_url": LINK_A, "source_title": "見出しA"},
              {"channel": "x", "subcategory": "休校", "text": "abc", "source_url": LINK_B, "source_title": "見出しB"},
              {"channel": "threads", "subcategory": "受験", "text": "い" * 10, "source_url": LINK_C,
               "source_title": "見出しC"}]
    out = el.assemble_rows(drafts, "news", ctx, NOW, TODAY, args, clock)
    x, th = out["x"], out["threads"]
    assert list(x[0].keys()) == ["id", "channel", "persona", "category", "subcategory", "text", "weighted_chars",
                                 "generator", "source_title", "source_url", "created_at", "earliest_publish_at",
                                 "published_at", "post_id"]
    assert list(th[0].keys()) == ["id", "channel", "persona", "category", "subcategory", "text", "char_count",
                                  "generator", "source_title", "source_url", "created_at", "earliest_publish_at",
                                  "published_at", "post_id"]
    assert [r["id"] for r in x] == ["news-x-2026-11-19-001", "news-x-2026-11-19-002"]
    assert th[0]["id"] == "news-th-2026-11-19-001"
    assert x[0]["persona"] == "iromama" and th[0]["persona"] == "iropapa"
    assert x[0]["category"] == "news" and x[0]["generator"] == "k8_agy_v1"
    assert x[0]["weighted_chars"] == 20 and x[1]["weighted_chars"] == 3 and th[0]["char_count"] == 10
    assert isinstance(x[0]["weighted_chars"], int) and isinstance(th[0]["char_count"], int)
    assert x[0]["published_at"] is None and x[0]["post_id"] is None and x[0]["earliest_publish_at"] is None
    # created_at は +09:00 付きで、同じ回の行は 1 秒ずつずれる (news は created_at の降順で選ばれるため)
    stamps = [x[0]["created_at"], x[1]["created_at"], th[0]["created_at"]]
    assert stamps == ["2026-11-19T10:00:00+09:00", "2026-11-19T10:00:01+09:00", "2026-11-19T10:00:02+09:00"]


def test_assemble_daily_has_no_source_and_category_is_fixed():
    ctx = {"rows": {"x": [], "threads": []}}
    args = SimpleNamespace(backend="ollama", model="gemma4:26b-a4b-it-qat")
    out = el.assemble_rows([{"channel": "threads", "subcategory": "observation", "text": "いろは"}],
                           "daily", ctx, NOW, TODAY, args, {"n": 0})
    row = out["threads"][0]
    assert row["id"] == "eng-th-2026-11-19-001" and row["category"] == "daily"
    assert "source_url" not in row and "source_title" not in row
    assert row["generator"] == "k8_gemma_v1" and row["char_count"] == 3
    assert el.generator_name("ollama", "other-model") == "k8_ollama_v1"


# ---------------------------------------------------------------- 検査

def checks(post, kind="daily", channels=("x",), cands=()):
    return [n for n, _ in el.individual_checks(post, kind, list(channels), list(cands))]


def good(ch="x", text=None):
    return {"channel": ch, "subcategory": "vent", "text": text or "今日は雨で一日家にいた。うちの子はずっと積木を並べていた"}


def test_required_keys():
    assert checks(good()) == []
    assert "missing" in checks({**good(), "text": "  \n "})
    assert "missing" in checks({**good(), "subcategory": ""})
    assert "missing" in checks({"channel": "x", "text": "あ"})
    assert checks("not a dict") == ["format"]
    assert "channel" in checks({**good(), "channel": "x"}, kind="news", channels=("threads",),
                               cands=[{"link": LINK_A}])


def test_length_limits():
    assert checks(good(text="あ" * 140)) == []                       # weighted 280・字数 140
    assert checks(good(text="あ" * 141)) == ["length"]               # weighted 282
    assert checks(good(text="a" * 281)) == ["length"]                # ASCII でも weighted 281
    assert checks(good("threads", "あ" * 300), channels=("threads",)) == []
    assert checks(good("threads", "あ" * 301), channels=("threads",)) == ["length"]
    # X は weighted 280 以内でも 300 字を超えない (Bluesky にも流れるため): 280 以内なら 300 字は超えないので別経路で確認
    assert checks(good(text="a" * 280)) == []


def test_url_hashtag_emoji_signature_self_intro():
    assert "url" in checks(good(text="詳しくは https://example.com を見てね"))
    assert "url" in checks(good(text="www.example.org に書いてあった"))
    assert "url" in checks(good(text="navi.omcha.jp の記事を読んだ"))
    assert "url" in checks(good(text="example.com が便利"))
    assert checks(good(text="3.5倍になった。Wi-Fi が遅い")) == []
    assert "hashtag" in checks(good(text="今日の育児 #育児"))
    assert "hashtag" in checks(good(text="今日の育児 ＃育児"))
    assert "emoji" in checks(good(text="嬉しい✨😊🎉"))
    assert checks(good(text="嬉しい✨😊")) == []
    assert checks(good(text="ありがとう♡♪★")) == []                 # 記号は絵文字に数えない
    assert "self_intro" in checks(good(text="いろママです。今日は雨"))
    assert "self_intro" in checks(good("threads", "いろパパです。今日は雨"), channels=("threads",))
    assert checks(good(text="いろママの友だちに会った")) == []
    assert "signature" in checks(good(text="今日もおつかれさま\n— ママより"))
    assert "signature" in checks(good(text="今日もおつかれさま\n―パパより"))
    assert "signature" in checks(good(text="今日もおつかれさま ママより"))
    assert checks(good(text="母より先に子が起きた")) == []


def test_negative_bank_words_are_not_applied_to_body():
    # 育児の普通の語 (着替え・母乳・健康診断) で落とさない (NEGATIVE_BANK は本文に当てない)
    assert checks(good(text="着替えを嫌がる朝。母乳の頃が懐かしい。健康診断の予約をした")) == []


def test_news_source_must_be_in_top_candidates_and_row_is_dropped():
    cands = TREND["top_candidates"]
    assert checks(np("x", "台風", 1), "news", ("x", "threads"), cands) == []
    # クエリ (?source=rss) が付いていなくても、候補の link に合わせて直す
    posts = [np("x", "台風", 1, url="https://news.example.jp/articles/aaa")]
    ctx = el.build_context({"x": [], "threads": []}, {"x": [], "threads": []}, NOW, TREND)
    ok, bad, dropped = el.check_batch(posts, "news", ["x", "threads"], cands, ctx, [])
    assert len(ok) == 1 and ok[0]["source_url"] == LINK_A and ok[0]["source_title"] == "見出しA"
    # 候補に無い URL は書き直しではなく捨てる (feedback にも出さない)
    fake = [np("x", "台風", 2, url="https://made-up.example.jp/news/1")]
    ok, bad, dropped = el.check_batch(fake, "news", ["x", "threads"], cands, ctx, [])
    assert ok == [] and bad == [] and dropped == 1


def test_dedup_source_topic_cross_channel_and_text():
    recent = {"x": [qrow("news-x-2026-11-18-001", "x", "news", "台風", uniq(50), "2026-11-18T06:00:00+09:00",
                         url=LINK_A)],
              "threads": []}
    ctx = el.build_context(recent, {"x": [], "threads": []}, NOW, TREND)
    cands = TREND["top_candidates"]

    def run(posts, taken=()):
        return el.check_batch(posts, "news", ["x", "threads"], cands, ctx, list(taken))

    # 直近 72 時間と同じ source_url
    ok, bad, _ = run([np("x", "別の話題", 1, LINK_A)])
    assert ok == [] and [n for n, _m in bad[0][1]] == ["dup_source"]
    # 直近 48 時間と同じ話題 (subcategory)
    ok, bad, _ = run([np("threads", "台風", 2, LINK_B)])
    assert ok == [] and [n for n, _m in bad[0][1]] == ["dup_topic"]
    # X と Threads で同じ話題 (2 本目を落とす)
    ok, bad, _ = run([np("x", "休校", 3, LINK_B), np("threads", "休校", 4, LINK_C)])
    assert len(ok) == 1 and ok[0]["channel"] == "x" and [n for n, _m in bad[0][1]] == ["dup_topic_cross"]
    # 直近 60 行・同じ回の中でほぼ同じ本文 (Jaccard 0.6 以上)
    ok, bad, _ = run([{**np("x", "運動会", 50, LINK_B)}])
    assert ok == [] and [n for n, _m in bad[0][1]] == ["dup_text"]
    near = uniq(5)[:28] + "ああ"
    ok, bad, _ = run([np("x", "遠足", 5, LINK_B), {**np("x", "遠足2", 6, LINK_C), "text": near}])
    assert len(ok) == 1 and [n for n, _m in bad[0][1]] == ["dup_text"]
    # 日本語 3 文字単位の Jaccard (別の本文は通る)
    assert el.jaccard(uniq(1), uniq(2)) == 0.0 and el.jaccard(uniq(1), uniq(1)) == 1.0


def test_old_source_is_not_deduped_after_72_hours():
    old = {"x": [qrow("news-x-2026-11-15-001", "x", "news", "台風", uniq(60), "2026-11-15T06:00:00+09:00",
                      url=LINK_A)], "threads": []}
    ctx = el.build_context(old, {"x": [], "threads": []}, NOW, TREND)
    ok, bad, _ = el.check_batch([np("x", "台風", 7, LINK_A)], "news", ["x", "threads"], TREND["top_candidates"], ctx, [])
    assert len(ok) == 1 and bad == []


# ---------------------------------------------------------------- 判定 (news / daily の要否)

def trend_rows(lane):
    return json.loads(lane.world.files["data/engagement_trends/%s.json" % TODAY])


def news_row(i, ch, created="2026-11-19T07:00:00+09:00"):
    rid = "news-%s-%s-%03d" % ("x" if ch == "x" else "th", TODAY, i)
    return qrow(rid, ch, "news", "話題%d" % i, uniq(700 + i), created, url="https://news.example.jp/a/%d" % i)


def test_news_generates_only_channels_below_two_today(lane):
    # X は今日の news が 2 本ある。Threads は 1 本 → Threads だけ作る
    lane.set_queue("x", [news_row(1, "x"), news_row(2, "x")])
    lane.set_queue("threads", [news_row(1, "threads")])
    w = Writer([wresult([np("threads", "新話題", 1, LINK_B)])])
    assert lane.run(w, "--only", "news") == 0
    dec = [r for r in events(lane, "decision") if r["kind"] == "news"][0]
    assert dec["action"] == "generate" and dec["channels"] == ["threads"]
    head = w.prompts[0].split("## 入力")[0]
    assert "threads (iropapa) 1〜2 本" in head and "x (iromama)" not in head
    pr = events(lane, "pr")[0]
    assert pr["counts"] == {"threads": 1} and pr["ids"] == ["news-th-2026-11-19-002"]
    # X の queue は変わらない (追記は Threads の queue だけ)
    assert len(queue_lines(lane, "x")) == 2 and len(queue_lines(lane, "threads")) == 2


def test_news_yesterday_rows_do_not_count_and_without_trend_nothing_runs(lane):
    lane.set_queue("x", [news_row(1, "x", "2026-11-18T07:00:00+09:00"), news_row(2, "x", "2026-11-18T07:01:00+09:00")])
    del lane.world.files["data/engagement_trends/%s.json" % TODAY]
    w = Writer([])
    assert lane.run(w, "--only", "news") == 0
    dec = events(lane, "decision")[0]
    assert dec["action"] == "skip" and "no trend" in dec["reason"] and w.calls == []
    # トレンドが出たら昨日の行は数えず、両チャネルを作る
    lane.world.files["data/engagement_trends/%s.json" % TODAY] = json.dumps(TREND)
    w = Writer([wresult([np("x", "話A", 1, LINK_A), np("threads", "話B", 2, LINK_B)])])
    assert lane.run(w, "--only", "news", "--run-id", "second") == 0
    dec = [r for r in events(lane, "decision") if r["run_id"] == "second"][0]
    assert dec["channels"] == ["x", "threads"]


def test_news_open_pr_blocks_the_kind(lane):
    lane.world.prs = [{"number": 7, "url": "u", "headRefName": "k8-engagement/news-" + TODAY,
                       "mergeable": "MERGEABLE", "createdAt": "2026-11-19T00:30:00Z"}]
    w = Writer([])
    assert lane.run(w, "--only", "news") == 0
    dec = events(lane, "decision")[0]
    assert dec["action"] == "skip" and dec["reason"] == "open_pr" and w.calls == []
    assert lane.world.cmds("gh", "pr", "create") == []


def test_round_limit_per_channel_per_day(lane):
    env = el.LaneEnv(lane.repo, clock=lambda: NOW)
    led = wl.Ledger(lane.state, "earlier", env)
    for _ in range(2):
        led.add("round", kind="news", channels=["x", "threads"], dry_run=False)
    w = Writer([])
    assert lane.run(w, "--only", "news") == 0
    dec = events(lane, "decision")[0]
    assert dec["action"] == "skip" and "rounds exhausted" in dec["reason"] and w.calls == []
    # X だけ 1 回使った日は、X はもう 1 回まで・Threads は 2 回使い切り
    state2 = lane.state.parent / "state2"
    led2 = wl.Ledger(state2, "e", env)
    led2.add("round", kind="news", channels=["threads"], dry_run=False)
    led2.add("round", kind="news", channels=["threads"], dry_run=False)
    rows = wl.read_ledger(state2)
    ctx = el.build_context({"x": [], "threads": []}, {"x": [], "threads": []}, NOW, TREND)
    chans, _ = el.plan_news(ctx, True, rows, TODAY, False)
    assert chans == ["x"]
    # dry-run の回は本番の回数に数えない
    assert el.rounds_today(rows, TODAY, True, "news", "threads") == 0


def test_daily_generates_below_six_pending_and_skips_enough(lane):
    pending = [qrow("eng-x-2026-11-18-%03d" % i, "x", "daily", "vent", uniq(800 + i), "2026-11-18T10:00:00+09:00")
               for i in range(1, 7)]
    published = [qrow("eng-th-2026-11-10-001", "threads", "daily", "vent", uniq(850), "2026-11-10T10:00:00+09:00",
                      published="2026-11-11T00:00:00+00:00")]
    lane.set_queue("x", pending)          # X は未投稿 6 本 → 足りている
    lane.set_queue("threads", published)  # Threads は未投稿 0 本 → 補充
    w = Writer([wresult([{"channel": "threads", "subcategory": "observation", "text": uniq(i)} for i in range(10)])])
    assert lane.run(w, "--only", "daily") == 0
    dec = events(lane, "decision")[0]
    assert dec["channels"] == ["threads"] and "x enough (6)" in dec["reason"]
    assert "ORIGINAL TH DAILY PROMPT" in w.prompts[0] and "ORIGINAL X DAILY" not in w.prompts[0]
    pr = events(lane, "pr")[0]
    assert pr["counts"] == {"threads": 10}
    ids = [json.loads(x)["id"] for x in queue_lines(lane, "threads")[1:]]
    assert ids == ["eng-th-2026-11-19-%03d" % i for i in range(1, 11)]
    title = [c for c in lane.world.cmds("gh", "pr", "create")][0]
    assert title[title.index("--title") + 1] == "[k8-engagement] daily (Threads) " + TODAY
    assert title[title.index("--head") + 1] == "k8-engagement/daily-" + TODAY


def test_daily_below_four_is_not_emitted_and_per_channel_round_cap(lane):
    w = Writer([wresult([dp("vent", i) for i in range(3)])])
    # X だけ作る (Threads は 2 回使い切りにしておく)
    env = el.LaneEnv(lane.repo, clock=lambda: NOW)
    led = wl.Ledger(lane.state, "earlier", env)
    led.add("round", kind="daily", channels=["threads"], dry_run=False)
    led.add("round", kind="daily", channels=["threads"], dry_run=False)
    assert lane.run(w, "--only", "daily") == 0
    assert events(lane, "pr") == [] and events(lane, "short")[0]["passed"] == 3
    assert lane.world.cmds("gh", "pr", "create") == []
    assert queue_lines(lane, "x") == []


def test_news_channel_with_no_passing_row_is_not_emitted(lane):
    w = Writer([wresult([np("x", "話A", 1, LINK_A)])])   # Threads は 0 本
    assert lane.run(w, "--only", "news") == 0
    assert events(lane, "pr")[0]["counts"] == {"x": 1}
    assert [r["channel"] for r in events(lane, "short")] == ["threads"]


# ---------------------------------------------------------------- 書き直し

def test_rewrite_gets_feedback_and_pass_rows_are_combined(lane):
    bad_url = dp("vent", 2)
    bad_url["text"] = "詳しくは https://example.com を見て " + uniq(2, 10)
    first = [dp("vent", 1), bad_url, dp("anecdote", 3), dp("worry", 4), dp("trend", 5), dp("vent", 6)]
    # 書き直し: 落ちた行を直した 1 本 + 1 回目の合格行とほぼ同じ本文 (これは合わせた検査で落とす)
    fixed = dp("vent", 7)
    dup_of_first = dp("vent", 99)
    dup_of_first["text"] = uniq(1)
    second = [fixed, dup_of_first]
    seen = {}

    def second_writer(kw):
        seen.update(kw)
        return wresult(second)

    w = Writer([wresult(first), second_writer])
    assert lane.run(w, "--only", "daily") == 0
    assert w.calls[0]["previous_text"] is None
    assert seen["previous_text"] and "posts[2]" in seen["feedback"] and "url" in seen["feedback"]
    assert seen["instruction"] == el.WRITER_INSTRUCTION and seen["num_ctx"] == el.DEFAULT_NUM_CTX
    pr = events(lane, "pr")[0]
    assert pr["counts"]["x"] == 6                               # 1 回目の合格 5 本 + 書き直しの合格 1 本
    checks_ev = [r for r in events(lane, "check") if r["kind"] == "daily"]
    assert [(r["attempt"], r["passed"], r["failed"]) for r in checks_ev] == [(1, 5, 1), (2, 1, 1)]
    assert checks_ev[1]["reasons"] == {"dup_text": 1}
    # 2 回目も落ちたら捨てるだけで、3 回目は書かせない
    assert len(w.calls) == 2


def test_no_rewrite_when_everything_passes(lane):
    w = Writer([wresult([dp("vent", i) for i in range(10)])])
    assert lane.run(w, "--only", "daily") == 0
    assert len(w.calls) == 1 and events(lane, "pr")[0]["counts"] == {"x": 10}


def test_parse_failure_gets_one_rewrite(lane):
    w = Writer([wresult(None, error="parse"), wresult([dp("vent", i) for i in range(5)])])
    assert lane.run(w, "--only", "daily") == 0
    assert len(w.calls) == 2 and "JSON として読めなかった" in w.calls[1]["feedback"]
    assert events(lane, "pr")[0]["counts"] == {"x": 5}


def test_web_calls_discards_the_round(lane):
    w = Writer([wresult([dp("vent", i) for i in range(10)], web_calls=2)])
    assert lane.run(w, "--only", "daily") == 0
    assert events(lane, "pr") == [] and events(lane, "discard")[0]["reason"] == "web_calls"
    assert len(w.calls) == 1 and queue_lines(lane, "x") == []


def test_quota_stops_run_and_next_run_is_gated(lane):
    w = Writer([wresult(None, error="quota", reset_s=3600)])
    assert lane.run(w, "--only", "daily") == 3
    assert events(lane, "quota")[0]["reset_s"] == 3600
    w2 = Writer([])
    assert lane.run(w2, "--only", "daily", "--run-id", "next") == 0
    assert w2.calls == [] and "quota wait" in [r for r in events(lane, "gate")][-1]["reason"]


def test_daily_caps_calls_and_tokens(lane):
    env = el.LaneEnv(lane.repo, clock=lambda: NOW)
    led = wl.Ledger(lane.state, "earlier", env)
    for _ in range(8):
        led.add("write", kind="daily", attempt=1, tokens=100)
    w = Writer([])
    assert lane.run(w, "--only", "daily") == 0
    assert w.calls == [] and "daily calls" in events(lane, "gate")[0]["reason"]
    state2 = lane.state.parent / "s2"
    led2 = wl.Ledger(state2, "earlier", env)
    led2.add("write", kind="daily", attempt=1, tokens=400000)
    rows = wl.read_ledger(state2)
    assert wl.day_usage(rows, TODAY, False)[1] == 400000


# ---------------------------------------------------------------- PR・衝突・dry-run

def test_live_run_creates_pr_with_append_only_change_and_no_bodies(lane, capsys):
    base = qrow("eng-x-2026-11-18-001", "x", "daily", "vent", uniq(500), "2026-11-18T10:00:00+09:00",
                published="2026-11-19T00:00:00+00:00")
    lane.set_queue("x", [base])
    before = queue_lines(lane, "x")
    w = Writer([wresult([dp("vent", i + 1) for i in range(10)])])
    assert lane.run(w, "--only", "daily") == 0
    after = queue_lines(lane, "x")
    assert after[:1] == before and len(after) == 11            # 末尾追記だけ。既存行は 1 文字も変えない
    assert all(json.loads(x)["created_at"].endswith("+09:00") for x in after[1:])
    git = lane.world.cmds("git")
    assert ["git", "fetch", "origin", "main"] in git
    assert any(c[:3] == ["git", "switch", "-C"] and c[3] == "k8-engagement/daily-" + TODAY and c[4] == "origin/main"
               for c in git)
    assert ["git", "add", "--", "data/engagement_queue_x.jsonl"] in git     # 触るのは X の queue だけ
    assert ["git", "push", "-u", "origin", "k8-engagement/daily-" + TODAY] in git
    assert not lane.world.cmds("gh", "pr", "merge")                          # merge は 31 に任せる
    assert all(cwd == str(lane.repo) for _, cwd in lane.world.calls)
    create = lane.world.cmds("gh", "pr", "create")[0]
    body = Path(create[create.index("--body-file") + 1]).read_text(encoding="utf-8")
    assert "eng-x-2026-11-19-001" in body and uniq(1) not in body and "ORIGINAL" not in body
    # 本文・プロンプトは標準出力・台帳に出さない
    out = capsys.readouterr().out
    led_text = (lane.state / "ledger.jsonl").read_text(encoding="utf-8")
    for leak in (uniq(1), uniq(2), "ORIGINAL X DAILY"):
        assert leak not in out and leak not in led_text
    # 本文は state の下にだけある
    assert uniq(1) in (lane.state / "runs").rglob("rows-daily.json").__next__().read_text(encoding="utf-8")
    assert (lane.state / "pending" / ("daily-%s.json" % TODAY)).exists()
    assert not (lane.state / "engagement.lock").exists()                     # ロックは解放


def test_prompt_is_original_with_preamble_and_inputs(lane):
    lane.set_queue("x", [qrow("news-x-2026-11-18-001", "x", "news", "運動会", "昨日の本文" + uniq(3),
                              "2026-11-18T06:00:00+09:00", url="https://news.example.jp/old")])
    w = Writer([wresult([np("x", "話A", 1, LINK_A), np("threads", "話B", 2, LINK_B)])])
    lane.run(w, "--only", "news", "--dry-run")
    p = w.prompts[0]
    assert p.index("# K8 の自動レーンからの実行") < p.index("# 元のプロンプト") < p.index("ORIGINAL NEWS PROMPT")
    assert p.endswith("ORIGINAL NEWS PROMPT\n")                     # 元のプロンプトは書き換えない
    assert "見出しA" in p and LINK_B in p and "概要C" in p            # top_candidates (title・link・summary)
    assert "運動会" in p                                              # 直近 48 時間の news の話題
    assert "昨日の本文" in p                                           # 直近の本文
    assert "PR は作らない" in p or "PR を作る」は、ここでは行わない" in p
    assert "id・created_at" in p and "{\"posts\"" in p
    assert w.calls[0]["backend"] == "agy" and w.calls[0]["instruction"] == el.WRITER_INSTRUCTION


def test_dry_run_calls_no_git_or_gh_and_saves_rows(lane):
    w = Writer([wresult([np("x", "話A", 1, LINK_A), np("threads", "話B", 2, LINK_B)])])
    assert lane.run(w, "--only", "news", "--dry-run") == 0
    assert lane.world.calls == []
    saved = json.loads(next((lane.state / "runs").rglob("rows-news.json")).read_text(encoding="utf-8"))
    assert saved["x"][0]["id"] == "news-x-2026-11-19-001" and saved["threads"][0]["id"] == "news-th-2026-11-19-001"
    assert queue_lines(lane, "x") == [] and events(lane, "pass")[0]["dry_run"] is True
    assert events(lane, "pr") == []


def test_dry_run_does_not_count_against_live_rounds(lane):
    w = Writer([wresult([np("x", "話A", 1, LINK_A), np("threads", "話B", 2, LINK_B)])])
    lane.run(w, "--only", "news", "--dry-run")
    w2 = Writer([wresult([np("x", "話A", 1, LINK_A), np("threads", "話B", 2, LINK_B)])])
    assert lane.run(w2, "--only", "news", "--run-id", "live") == 0
    assert events(lane, "pr")[0]["run_id"] == "live"


def test_conflicting_pr_is_rebuilt_with_same_branch_and_no_new_pr(lane):
    branch = "k8-engagement/news-" + TODAY
    rows = {"x": [qrow("news-x-2026-11-19-001", "x", "news", "a", uniq(1), "2026-11-19T09:00:00+09:00", url=LINK_A)],
            "threads": []}
    (lane.state / "pending").mkdir(parents=True)
    (lane.state / "pending" / ("news-%s.json" % TODAY)).write_text(
        json.dumps({"kind": "news", "date": TODAY, "branch": branch, "rows": rows}, ensure_ascii=False),
        encoding="utf-8")
    lane.world.prs = [{"number": 7, "url": "https://github.com/omochairo/amazon/pull/7", "headRefName": branch,
                       "mergeable": "CONFLICTING", "createdAt": "2026-11-19T00:30:00Z"}]
    w = Writer([])
    assert lane.run(w, "--only", "news") == 0
    git = lane.world.cmds("git")
    assert any(c[:2] == ["git", "push"] and "--force-with-lease" in c and c[-1] == branch for c in git)
    assert any(c[:3] == ["git", "switch", "-C"] and c[3] == branch and c[4] == "origin/main" for c in git)
    assert lane.world.cmds("gh", "pr", "create") == [] and w.calls == []      # 新しい PR は作らず、書き手も呼ばない
    assert events(lane, "rebuild")[0]["ids"] == ["news-x-2026-11-19-001"]
    # 保存しておいた同じ行がそのまま付け直される
    lines = queue_lines(lane, "x")
    assert json.loads(lines[-1])["id"] == "news-x-2026-11-19-001" and json.loads(lines[-1])["text"] == uniq(1)


def test_mergeable_pr_is_left_alone(lane):
    branch = "k8-engagement/news-" + TODAY
    lane.world.prs = [{"number": 7, "url": "u", "headRefName": branch, "mergeable": "MERGEABLE",
                       "createdAt": "2026-11-19T00:30:00Z"}]
    assert lane.run(Writer([]), "--only", "news") == 0
    assert not any(c[:2] == ["git", "push"] for c in lane.world.cmds("git"))
    assert events(lane, "rebuild") == []


def test_stale_pr_is_closed_and_recorded(lane):
    branch = "k8-engagement/news-" + TODAY
    lane.world.prs = [{"number": 7, "url": "u", "headRefName": branch, "mergeable": "MERGEABLE",
                       "createdAt": "2026-11-18T00:30:00Z"},      # 25.5 時間前
                      {"number": 8, "url": "u2", "headRefName": "some-other-branch", "mergeable": "MERGEABLE",
                       "createdAt": "2026-11-01T00:00:00Z"}]      # 自分の PR ではない
    w = Writer([wresult([np("x", "話A", 1, LINK_A), np("threads", "話B", 2, LINK_B)])])
    assert lane.run(w, "--only", "news") == 0
    closes = lane.world.cmds("gh", "pr", "close")
    assert len(closes) == 1 and closes[0][3] == "7"
    assert events(lane, "closed")[0]["number"] == 7 and events(lane, "closed")[0]["reason"] == "stale_24h"
    # 閉じたあとは news を作り直せる
    assert events(lane, "pr")[0]["kind"] == "news"


def test_pr_create_failure_deletes_remote_branch_and_records_error(lane):
    lane.world.fail_pr_create = True
    w = Writer([wresult([dp("vent", i) for i in range(10)])])
    assert lane.run(w, "--only", "daily") == 0
    assert events(lane, "error")[0]["stage"] == "pr" and events(lane, "pr") == []
    assert ["git", "push", "origin", "--delete", "k8-engagement/daily-" + TODAY] in lane.world.cmds("git")


def test_crash_is_recorded_and_lock_released(lane):
    lane.world.files.pop("data/engagement_queue_x.jsonl")
    assert lane.run(Writer([]), "--only", "daily") == 1
    assert events(lane, "error")[0]["stage"] == "crash"
    assert not (lane.state / "engagement.lock").exists()


def test_open_pr_list_failure_crashes_without_writing(lane):
    class Fail(FakeWorld):
        def __call__(self, argv, input=None, **kw):
            if argv[:3] == ["gh", "pr", "list"]:
                return proc(1, "", "boom")
            return super().__call__(argv, input, **kw)

    lane.world.__class__ = Fail
    w = Writer([])
    assert lane.run(w, "--only", "daily") == 1 and w.calls == []


# ---------------------------------------------------------------- ロック

def test_lock_blocks_second_run_and_stale_lock_expires(lane):
    env = el.LaneEnv(lane.repo, clock=lambda: NOW)
    a = el.RunLock(lane.state, env)
    assert a.acquire() is True
    b = el.RunLock(lane.state, env)
    assert b.acquire() is False
    w = Writer([])
    assert lane.run(w, "--only", "daily") == 0
    assert events(lane, "locked") and events(lane, "run_start") == [] and w.calls == []
    a.release()
    # 古いロック (2 時間超) は無効
    old = el.LaneEnv(lane.repo, clock=lambda: NOW - dt.timedelta(hours=2, minutes=1))
    assert el.RunLock(lane.state, old).acquire() is True
    assert el.RunLock(lane.state, env).acquire() is True
    # 2 時間以内は有効
    state2 = lane.state.parent / "s2"
    recent = el.LaneEnv(lane.repo, clock=lambda: NOW - dt.timedelta(hours=1, minutes=59))
    assert el.RunLock(state2, recent).acquire() is True
    assert el.RunLock(state2, env).acquire() is False


# ---------------------------------------------------------------- 引数

def test_cli_defaults():
    a = parse(["run", "--brain-dir", "b", "--state-dir", "s"])
    assert a.max_calls == 8 and a.max_tokens == 400000 and a.backend == "agy" and a.only is None
    assert a.dry_run is False and a.num_ctx == el.DEFAULT_NUM_CTX
    assert a.model == el.wl.DEFAULT_MODEL


def test_cli_model_default_follows_backend():
    # agy 用の既定を ollama に渡すと HTTP 404 になった (2026-11-19 の試運転)
    a = parse(["run", "--brain-dir", "b", "--state-dir", "s", "--backend", "ollama"])
    assert a.model == el.OLLAMA_DEFAULT_MODEL
    assert el.generator_name(a.backend, a.model) == "k8_gemma_v1"
    a = parse(["run", "--brain-dir", "b", "--state-dir", "s", "--backend", "ollama", "--model", "gemma4:26b-a4b-it-qat",
               "--num-ctx", "16384", "--only", "news", "--dry-run", "--repo-dir", "r"])
    assert a.backend == "ollama" and a.num_ctx == 16384 and a.only == "news" and a.dry_run and a.repo_dir == "r"
