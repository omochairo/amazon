"""writer_lane (navi-brain#170 の K8 記事レーン) のテスト。

gh・git・agy・Web は一切呼ばない (Env に偽物を入れる)。
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
import build_jules_prompt as bjp  # noqa: E402
import writer_lane as wl  # noqa: E402

TODAY = "2026-11-15"
NOW = dt.datetime(2026, 11, 14, 17, 30, tzinfo=dt.timezone.utc)  # 2026-11-15 02:30 JST
A1, A2, A3 = "B0AAAAAAA1", "B0BBBBBBB2", "B0CCCCCCC3"
CITABLE = ["https://review.example.jp/a", "https://blog.example.com/b"]


def proc(rc=0, out="", err=""):
    return subprocess.CompletedProcess([], rc, out, err)


class FakeWorld:
    """gh / git / python の呼び出しを記録し、決めた結果を返す。"""

    def __init__(self):
        self.calls = []
        self.locks = set()
        self.gate_rc = 0
        self.gate_out = "[OK] x score=97 (40/40 checks)\n"
        self.schema_rc = 0
        self.fail_lock = set()
        self.pr_count = 0
        self.closed = []
        self.open_titles = []

    def __call__(self, argv, input=None, **kw):
        self.calls.append((list(argv), input))
        if argv[0] == "gh":
            return self.gh(argv[1:], input)
        if argv[0] == "git":
            return proc()
        if "validate_json.py" in " ".join(argv):
            return proc(self.schema_rc, "", "schema error: missing slug")
        if "quality_gate.py" in " ".join(argv):
            return proc(self.gate_rc, self.gate_out)
        if "record_asin_origin.py" in " ".join(argv):
            return proc()
        raise AssertionError(f"unexpected command {argv}")

    def gh(self, a, input_text):
        if a[:2] == ["pr", "list"]:
            if "open" in a:
                return proc(0, json.dumps([{"title": t} for t in self.open_titles]))
            if "closed" in a:
                return proc(0, json.dumps(self.closed))
            return proc(0, "[]")
        if a[0] == "api" and "matching-refs" in a[1]:
            return proc(0, "".join(f"refs/heads/jules-lock/{x}\n" for x in self.locks))
        if a[0] == "api" and a[1].endswith("git/refs/heads/main"):
            return proc(0, "mainsha\n")
        if a[0] == "api" and "/git/commits/" in a[1]:
            return proc(0, "treesha\n")
        if a[:3] == ["api", "-X", "POST"] and a[3].endswith("git/commits"):
            return proc(0, "lockcommit\n")
        if a[:3] == ["api", "-X", "POST"] and a[3].endswith("git/refs"):
            ref = [x for x in a if x.startswith("ref=")][0]
            asin = ref.rsplit("/", 1)[-1]
            if asin in self.fail_lock or asin in self.locks:
                return proc(1, "", "Reference already exists")
            self.locks.add(asin)
            return proc(0, "{}")
        if a[:3] == ["api", "-X", "DELETE"]:
            self.locks.discard(a[3].rsplit("/", 1)[-1])
            return proc(0)
        if a[:2] == ["pr", "create"]:
            self.pr_count += 1
            return proc(0, f"https://github.com/omochairo/amazon/pull/{9000 + self.pr_count}\n")
        if a[:2] == ["pr", "merge"]:
            return proc(0)
        if a[:2] == ["issue", "comment"]:
            return proc(0, "https://github.com/omochairo/amazon-navi-brain/issues/1#c")
        raise AssertionError(f"unexpected gh {a}")

    def gh_cmds(self, *prefix):
        return [c for c, _ in self.calls if c[0] == "gh" and c[1:1 + len(prefix)] == list(prefix)]


def article(asin, sources=None, extra_url=None):
    a = {"slug": f"{TODAY}-{asin}", "date": f"{TODAY}T10:00:00+09:00", "title": "t",
         "product": {"prices": {"amazon": {"url": f"https://www.amazon.co.jp/dp/{asin}/?tag=x-22"}}},
         "sources": sources if sources is not None else [
             {"id": "src-1", "url": CITABLE[0]}, {"id": "src-2", "url": CITABLE[1]},
             {"id": "src-3", "url": f"https://www.amazon.co.jp/dp/{asin}/"}]}
    if extra_url:
        a["narrative"] = {"intro": f"詳しくは {extra_url} を参照"}
    return a


def wresult(art=None, error="", web_calls=0, reset_s=None, text="```json {} ```"):
    return article_writer.WriteResult(
        ok=art is not None and not error, article=art, text=text, error=error, error_text=error,
        usage={"total_tokens": 100000}, secs=200.0, backend="agy", model="gemini-4-hydrogen-high",
        reset_s=reset_s, conversation_id="cid", tool_calls={"read_file": 2}, web_calls=web_calls)


class Writer:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, prompt, **kw):
        self.calls.append(kw)
        r = self.results.pop(0)
        return r(kw) if callable(r) else r


@pytest.fixture()
def lane(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "data" / "articles").mkdir(parents=True)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "nolocal"))
    state = tmp_path / "state"
    world = FakeWorld()
    sites = {A1: 2, A2: 2, A3: 2}

    def fake_precheck(asin, fetch, cand=None):
        n = sites.get(asin, 2)
        cit = CITABLE[:n]
        return {"third": [], "prior": []}, {u: ("ok", "x", ["33506"], 200) for u in cit}, cit, \
            [f"site{i}" for i in range(n)]

    monkeypatch.setattr(wl, "precheck", fake_precheck)
    # 出典の状態 (P1b) が読む候補 URL。事前チェックは上の偽物なので、候補は空でよい
    monkeypatch.setattr(bjp, "source_candidates", lambda asin, no_web=False: {
        "mall": [], "third": [], "prior": [], "dead": set(), "news": set(), "first_party": set(), "asin": asin})
    monkeypatch.setattr(bjp, "build_prompt", lambda asin, **kw: (
        f"PROMPT {asin} https://www.amazon.co.jp/dp/{asin}/ {CITABLE[0]} {CITABLE[1]} "
        f"https://m.media-amazon.com/images/I/x.jpg"))
    picked = {"origins": [{"asin": A1, "pool": "ranking", "source_keyword": None},
                          {"asin": A2, "pool": "keyword", "source_keyword": "kw"},
                          {"asin": A3, "pool": "ranking", "source_keyword": None}]}
    import article_pick
    seen = {}

    def fake_pick(existing, seed):
        seen["existing"] = set(existing)
        seen["closed_path"] = __import__("os").environ.get("CLOSED_ARTICLE_PRS")
        seen["rewrite_cap"] = __import__("os").environ.get("REWRITE_PICKS_PER_RUN")
        return list(picked["origins"])

    monkeypatch.setattr(article_pick, "pick_from_repo", fake_pick)

    def run(writer, *extra, clock=NOW):
        env = wl.Env(runner=world, writer=writer, python="py",
                     clock=clock if callable(clock) else (lambda: clock))
        ap = ["run", "--state-dir", str(state)] + list(extra)
        args = parse(ap)
        return wl.cmd_run(args, env)

    return SimpleNamespace(repo=repo, state=state, world=world, sites=sites, run=run,
                           picked=picked, seen=seen)


def parse(argv):
    import argparse
    # main() の引数定義をそのまま使う
    captured = {}

    def fake_run(args, env):
        captured["args"] = args
        return 0

    orig = wl.cmd_run
    wl.cmd_run = fake_run
    try:
        wl.main(argv)
    finally:
        wl.cmd_run = orig
    return captured["args"]


def ledger(lane):
    return wl.read_ledger(lane.state)


def events(lane, name):
    return [r for r in ledger(lane) if r["event"] == name]


def test_pass_first_try_makes_pr_and_keeps_lock(lane):
    w = Writer([wresult(article(A1))])
    assert lane.run(w, "--max-articles", "1") == 0
    prs = events(lane, "pr")
    assert len(prs) == 1 and prs[0]["asin"] == A1 and prs[0]["attempts"] == 1 and prs[0]["score"] == 97
    create = lane.world.gh_cmds("pr", "create")
    title = create[0][create[0].index("--title") + 1]
    assert title == f"[k8-writer] 記事生成: {A1}"
    body = Path(create[0][create[0].index("--body-file") + 1]).read_text(encoding="utf-8")
    assert "PROMPT" not in body and "review.example.jp" not in body and "navi-brain#170" in body
    # ロックは 05 が外すので残す
    assert A1 in lane.world.locks
    gits = [c for c, _ in lane.world.calls if c[0] == "git"]
    assert gits[0] == ["git", "fetch", "origin", "main"] and gits[1][:3] == ["git", "switch", "-C"]
    assert ["git", "switch", "--discard-changes", "--detach", "origin/main"] in gits
    # 記事ファイルは LF・末尾改行なし
    raw = (lane.repo / "data" / "articles" / f"{TODAY}-{A1}.json").read_bytes()
    assert b"\r\n" not in raw and not raw.endswith(b"\n")
    # 出自台帳の PR (pool があるもの)
    assert len(lane.world.gh_cmds("pr", "merge")) == 1
    end = events(lane, "run_end")[-1]
    assert end["prs"] == 1 and end["calls"] == 1


def test_rewrite_with_feedback_then_pass(lane):
    bad = article(A1, extra_url="https://made-up.example.org/x")
    w = Writer([wresult(bad), wresult(article(A1))])
    lane.run(w, "--max-articles", "1")
    assert len(w.calls) == 2
    assert w.calls[0]["feedback"] is None and "url_not_in_prompt" in w.calls[1]["feedback"]
    assert w.calls[1]["previous_text"]
    assert events(lane, "pr")[0]["attempts"] == 2


def test_failed_twice_releases_lock_and_records(lane):
    lane.world.gate_rc = 2
    lane.world.gate_out = "[NG] x score=88 (38/40 checks)\n    - sources_v5: 非販売 1 サイト\n"
    w = Writer([wresult(article(A1)), wresult(article(A1))])
    lane.run(w, "--max-articles", "1", "--max-prechecks", "1")
    f = events(lane, "failed")
    assert len(f) == 1 and f[0]["reasons"] == ["quality_gate:sources_v5"]
    assert A1 not in lane.world.locks
    assert lane.world.gh_cmds("pr", "create") == []
    assert "sources_v5" in w.calls[1]["feedback"]


def test_url_rules(lane):
    allowed = wl.urls_in_text(f"x https://www.amazon.co.jp/dp/{A1}/ {CITABLE[0]}/ {CITABLE[1]}")
    ok = article(A1)
    assert wl.check_urls(ok, A1, allowed, CITABLE) == []
    # Amazon の dp はタグ違いでも通る
    tagged = article(A1, sources=[{"id": "s", "url": f"https://amazon.co.jp/dp/{A1}?tag=other-22"}])
    assert wl.check_urls(tagged, A1, allowed, CITABLE) == []
    made_up = article(A1, extra_url="https://made-up.example.org/x。")
    names = [n for n, _ in wl.check_urls(made_up, A1, allowed, CITABLE)]
    assert names == ["url_not_in_prompt"]
    # プロンプトにはあるが抜粋で確かめていない非販売の出典
    allowed2 = allowed | {wl.normalize_url("https://weak.example.net/c")}
    weak = article(A1, sources=[{"id": "s", "url": "https://weak.example.net/c"}])
    assert [n for n, _ in wl.check_urls(weak, A1, allowed2, CITABLE)] == ["source_not_citable"]
    # 販売ページ (楽天) はプロンプトにあれば通る
    rk = "https://item.rakuten.co.jp/shop/x/"
    sale = article(A1, sources=[{"id": "s", "url": rk}])
    assert wl.check_urls(sale, A1, allowed | {wl.normalize_url(rk)}, CITABLE) == []


def test_web_calls_fail(lane):
    w = Writer([wresult(article(A1), web_calls=1), wresult(article(A1), web_calls=1)])
    lane.run(w, "--max-articles", "1", "--max-prechecks", "1")
    assert events(lane, "failed")[0]["reasons"] == ["web_calls"]


def test_skip_when_sources_short(lane):
    lane.sites[A1] = 1
    w = Writer([wresult(article(A2))])
    lane.run(w, "--max-articles", "1")
    assert [r["asin"] for r in events(lane, "skip_sources")] == [A1]
    assert events(lane, "pr")[0]["asin"] == A2
    assert len(w.calls) == 1


def test_lock_busy_skips(lane):
    lane.world.fail_lock.add(A1)
    w = Writer([wresult(article(A2))])
    lane.run(w, "--max-articles", "1")
    assert [r["asin"] for r in events(lane, "lock_busy")] == [A1]
    assert events(lane, "pr")[0]["asin"] == A2


def test_quota_stops_run_and_next_run_is_gated(lane):
    w = Writer([wresult(error="quota", reset_s=3600)])
    assert lane.run(w, "--max-articles", "3") == 3
    assert len(w.calls) == 1 and A1 not in lane.world.locks
    q = json.loads((lane.state / "quota_until.json").read_text(encoding="utf-8"))
    assert q["until"] == pytest.approx(NOW.timestamp() + 3600)
    w2 = Writer([])
    lane.run(w2, clock=NOW + dt.timedelta(minutes=30))
    assert events(lane, "gate")[-1]["reason"].startswith("lane quota wait")
    assert w2.calls == []
    # 待ちが過ぎれば書く
    w3 = Writer([wresult(article(A1))])
    lane.run(w3, "--max-articles", "1", clock=NOW + dt.timedelta(hours=2))
    assert len(w3.calls) == 1


def test_quota_wait_is_capped(lane):
    w = Writer([wresult(error="quota", reset_s=173021)])
    lane.run(w)
    q = json.loads((lane.state / "quota_until.json").read_text(encoding="utf-8"))
    assert q["until"] == pytest.approx(NOW.timestamp() + 24 * 3600)


def test_mcp_quota_file_gates(lane, tmp_path, monkeypatch):
    d = tmp_path / "local" / "agy"
    d.mkdir(parents=True)
    (d / "quota.json").write_text(json.dumps({"gemini-4-hydrogen-high": NOW.timestamp() + 60}))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    w = Writer([])
    lane.run(w)
    assert "agy MCP recorded quota" in events(lane, "gate")[-1]["reason"]


def test_daily_caps(lane):
    w = Writer([wresult(article(A1)), wresult(article(A2)), wresult(article(A3))])
    lane.run(w, "--max-articles", "2")
    assert len(events(lane, "pr")) == 2
    w2 = Writer([])
    lane.run(w2, "--max-articles", "2", clock=NOW + dt.timedelta(hours=1))
    assert events(lane, "gate")[-1]["reason"].startswith("daily articles")
    # 呼び出し回数の上限 (同じ日)
    w3 = Writer([])
    lane.run(w3, "--max-articles", "9", "--max-calls", "2", clock=NOW + dt.timedelta(hours=2))
    assert events(lane, "gate")[-1]["reason"].startswith("daily calls")
    w4 = Writer([])
    lane.run(w4, "--max-articles", "9", "--max-tokens", "150000", clock=NOW + dt.timedelta(hours=3))
    assert events(lane, "gate")[-1]["reason"].startswith("daily tokens")


def test_failed_asins_feed_cooldown_and_exclusions(lane):
    lane.world.gate_rc = 2
    lane.world.gate_out = "    - sources_v5: x\n"
    lane.run(Writer([wresult(article(A1)), wresult(article(A1))]), "--max-articles", "1", "--max-prechecks", "1")
    lane.world.gate_rc = 0
    lane.world.closed = [{"title": "Add article JSON for B0ZZZZZZZ9", "closedAt": "2026-11-10T00:00:00Z"}]
    lane.world.open_titles = [f"feat: add JSON for {A3}"]
    lane.world.locks.add("B0LOCKED01")
    # 直近に出典不足で外した ASIN (台帳の skip_sources) も除外する
    with open(lane.state / "ledger.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": wl.iso(NOW), "jst_date": TODAY, "run_id": "x",
                            "event": "skip_sources", "asin": A2}) + "\n")
    (lane.repo / "data" / "articles" / "2026-01-01-B0EXIST001.json").write_text("{}")
    (lane.repo / "data" / "articles" / "2026-01-01-B0SIDE0001.seo.json").write_text("{}")
    lane.run(Writer([wresult(article(A1))]), "--max-articles", "1", clock=NOW + dt.timedelta(hours=1))
    rows = json.loads(Path(lane.seen["closed_path"]).read_text(encoding="utf-8"))
    assert {"title": f"[k8-writer] 不合格: {A1}", "closedAt": events(lane, "failed")[0]["ts"]} in rows
    assert any("B0ZZZZZZZ9" in r["title"] for r in rows)
    ex = lane.seen["existing"]
    assert {"B0EXIST001", A3, "B0LOCKED01", A2} <= ex  # A2 は出典不足で直近に外した
    assert "B0SIDE0001" not in ex
    assert lane.seen["rewrite_cap"] == "0"


def test_dry_run_no_lock_no_pr(lane):
    w = Writer([wresult(article(A1)), wresult(article(A2))])
    lane.run(w, "--dry-run", "--max-articles", "2")
    assert lane.world.locks == set()
    assert lane.world.gh_cmds("pr", "create") == []
    assert [r["asin"] for r in events(lane, "pass")] == [A1, A2]
    assert (lane.state / "runs").exists()
    saved = list((lane.state / "runs").glob(f"*/{A1}/article.json"))
    assert saved and json.loads(saved[0].read_text(encoding="utf-8"))["slug"] == f"{TODAY}-{A1}"


def test_crash_releases_lock(lane, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(wl, "check_article", boom)
    w = Writer([wresult(article(A1))])
    assert lane.run(w) == 1
    assert A1 not in lane.world.locks
    err = events(lane, "error")[-1]
    assert err["stage"] == "crash" and "boom" in err["detail"]
    assert events(lane, "run_end")[-1]["errors"] == 1


def test_writer_errors(lane):
    w = Writer([wresult(error="timeout"), wresult(article(A2))])
    lane.run(w, "--max-articles", "1")
    assert events(lane, "error")[0]["error"] == "timeout" and A1 not in lane.world.locks
    assert events(lane, "pr")[0]["asin"] == A2
    w2 = Writer([wresult(error="auth")])
    lane.run(w2, "--max-articles", "5", clock=NOW + dt.timedelta(days=1))
    assert len(w2.calls) == 1  # 認証切れはその回をやめる


def test_refusal_on_rewrite_is_recorded_as_refused(lane):
    """navi-brain#170 R4: 1 回目は記事として読めて出典不足、2 回目は書き手が断って JSON を出さない。
    parse でなく、本当の原因 (1 回目の理由) と refused を残す。"""
    lane.world.gate_rc = 2
    lane.world.gate_out = "[NG] x score=88 (38/40 checks)\n    - sources_v5: 非販売 0 サイト\n"
    w = Writer([wresult(article(A1)), wresult(error="parse", text="この材料では直せません。")])
    lane.run(w, "--max-articles", "1", "--max-prechecks", "1")
    f = events(lane, "failed")
    assert len(f) == 1 and f[0]["reasons"] == ["quality_gate:sources_v5", "refused"]
    assert "この材料では直せません" in f[0]["details"]


def test_parse_twice_stays_parse(lane):
    w = Writer([wresult(error="parse"), wresult(error="parse")])
    lane.run(w, "--max-articles", "1", "--max-prechecks", "1")
    assert events(lane, "failed")[0]["reasons"] == ["parse"]


def test_parse_error_gets_one_rewrite(lane):
    w = Writer([wresult(error="parse"), wresult(article(A1))])
    lane.run(w, "--max-articles", "1")
    assert "記事 JSON として読めなかった" in w.calls[1]["feedback"]
    assert len(events(lane, "pr")) == 1


def test_open_pr_list_failure_crashes_without_writing(lane):
    orig = lane.world.gh

    def gh(a, inp):
        if a[:2] == ["pr", "list"] and "open" in a:
            return proc(1, "", "network")
        return orig(a, inp)

    lane.world.gh = gh
    w = Writer([])
    assert lane.run(w) == 1
    assert w.calls == []


def test_report_warnings_and_table(lane):
    lane.run(Writer([wresult(article(A1))]), "--max-articles", "1")
    report = wl.build_report(ledger(lane), {"https://github.com/omochairo/amazon/pull/9001": "MERGED"},
                             NOW + dt.timedelta(hours=1))
    assert report.startswith("## 記事レーン（k8-writer）")
    assert "| 2026-11-15 | 1 | 1 | 1 | 1 | 1 | 0 |" in report
    # 3 日後: 直近 2 日 PR 0 本で ⚠
    later = NOW + dt.timedelta(days=3)
    lane.run(Writer([wresult(error="quota", reset_s=60)]), clock=later)
    r2 = wl.build_report(ledger(lane), {}, later)
    assert r2.startswith("## ⚠ ") and "枠切れ" in r2 and "直近 2 日" in r2
    # gh が取れないとき
    r3 = wl.build_report(ledger(lane), None, later)
    assert "取得できなかった" in r3


def test_report_crash_warning(lane, monkeypatch):
    monkeypatch.setattr(wl, "check_article", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    lane.run(Writer([wresult(article(A1))]))
    assert "例外で落ちた" in wl.build_report(ledger(lane), {}, NOW)


def test_report_post(lane):
    lane.run(Writer([wresult(article(A1))]), "--max-articles", "1")
    env = wl.Env(runner=lane.world, clock=lambda: NOW)
    args = SimpleNamespace(state_dir=str(lane.state), issue=7, brain_slug="omochairo/amazon-navi-brain",
                           repo_slug="omochairo/amazon", days=7, post=True)
    assert wl.cmd_report(args, env) == 0
    c = lane.world.gh_cmds("issue", "comment")[0]
    assert c[3] == "7" and c[c.index("-R") + 1] == "omochairo/amazon-navi-brain"


def test_forced_asin_skips_pick(lane):
    w = Writer([wresult(article(A3))])
    lane.run(w, "--asin", A3, "--max-articles", "1")
    assert "existing" not in lane.seen
    assert events(lane, "pr")[0]["asin"] == A3
    assert lane.world.gh_cmds("pr", "merge") == []  # pool が空なら出自台帳は出さない


def test_is_sale_source_by_host_not_substring():
    # クエリに販売サイト名を含む第三者の URL は販売扱いにしない (抜粋の照合をすり抜けない)
    assert not wl.is_sale_source({"url": "https://blog.example.com/a?from=amazon.co.jp"})
    assert wl.is_sale_source({"url": "https://item.rakuten.co.jp/shop/x/"})
    assert wl.is_sale_source({"url": "https://www.amazon.co.jp/s?k=x"})
    assert not wl.is_sale_source({"url": ""})


def test_run_stops_when_jst_date_changes(lane):
    # 23:50 JST に始まり、1 本目を書いている間に日付が変わった
    t = {"now": dt.datetime(2026, 11, 15, 14, 50, tzinfo=dt.timezone.utc)}

    def first(kw):
        t["now"] = t["now"] + dt.timedelta(minutes=20)
        return wresult(article(A1))  # 23:50 JST の日付 (TODAY) で書いた記事

    w = Writer([first, wresult(article(A2))])
    lane.run(w, "--max-articles", "5", "--dry-run", clock=lambda: t["now"])
    assert len(w.calls) == 1
    assert any(e.get("reason") == "JST date changed during run" for e in events(lane, "gate"))


def test_rewrites_are_picked_first_when_enabled(lane):
    """navi-brain#170 B1: --max-rewrites で書き直し待ちを除外から外し、先頭に回す。"""
    rq_dir = lane.repo / "data" / "rewrite_queue"
    rq_dir.mkdir(parents=True)
    (rq_dir / "B0REWRITE1.json").write_text(json.dumps(
        {"asin": "B0REWRITE1", "old_slug": "2026-06-01-B0REWRITE1", "requested_at": "2026-11-10T00:00:00Z"}))
    (lane.repo / "data" / "articles" / "2026-06-01-B0REWRITE1.json").write_text("{}")
    # 書き直し済み (新しい日付の本文がある) の印は候補に戻さない
    (rq_dir / "B0DONE0001.json").write_text(json.dumps(
        {"asin": "B0DONE0001", "old_slug": "2026-06-01-B0DONE0001", "requested_at": "2026-11-10T00:00:00Z"}))
    (lane.repo / "data" / "articles" / "2026-06-01-B0DONE0001.json").write_text("{}")
    (lane.repo / "data" / "articles" / "2026-11-12-B0DONE0001.json").write_text("{}")
    (rq_dir / "B0REWRITE2.json").write_text(json.dumps(
        {"asin": "B0REWRITE2", "old_slug": "2026-06-01-B0REWRITE2", "requested_at": "2026-11-10T00:00:00Z"}))
    (lane.repo / "data" / "articles" / "2026-06-01-B0REWRITE2.json").write_text("{}")
    # 進行中 (ロック中) の書き直し待ちは除外のまま
    (rq_dir / "B0LOCKED01.json").write_text(json.dumps(
        {"asin": "B0LOCKED01", "old_slug": "2026-06-01-B0LOCKED01", "requested_at": "2026-11-10T00:00:00Z"}))
    (lane.repo / "data" / "articles" / "2026-06-01-B0LOCKED01.json").write_text("{}")
    lane.world.locks.add("B0LOCKED01")
    # B0REWRITE2 は上限を超えた書き直し待ちが別の枠 (pool 無し) で残ったもの → 外す
    lane.picked["origins"] = [{"asin": A1, "pool": "ranking", "source_keyword": None},
                              {"asin": "B0REWRITE2", "pool": "", "source_keyword": None},
                              {"asin": "B0REWRITE1", "pool": "rewrite-queue", "source_keyword": None}]
    lane.sites["B0REWRITE1"] = 2
    w = Writer([wresult(article("B0REWRITE1"))])
    lane.run(w, "--max-articles", "1", "--max-rewrites", "2", "--dry-run")
    assert lane.seen["rewrite_cap"] == "2"
    assert "B0REWRITE1" not in lane.seen["existing"] and "B0DONE0001" in lane.seen["existing"]
    assert "B0LOCKED01" in lane.seen["existing"]
    assert [e["asin"] for e in events(lane, "write")] == ["B0REWRITE1"]
    # 上限に届かなくても、別の枠で残った書き直し待ちは書かない
    lane.run(Writer([wresult(article(A1))]), "--max-articles", "5", "--max-rewrites", "1", "--dry-run",
             clock=NOW + dt.timedelta(hours=1))
    assert "B0REWRITE2" not in [e["asin"] for e in events(lane, "write")]


def test_rewrites_off_by_default(lane):
    rq_dir = lane.repo / "data" / "rewrite_queue"
    rq_dir.mkdir(parents=True)
    (rq_dir / "B0REWRITE1.json").write_text(json.dumps(
        {"asin": "B0REWRITE1", "old_slug": "2026-06-01-B0REWRITE1", "requested_at": "2026-11-10T00:00:00Z"}))
    (lane.repo / "data" / "articles" / "2026-06-01-B0REWRITE1.json").write_text("{}")
    lane.run(Writer([wresult(article(A1))]), "--max-articles", "1", "--dry-run")
    assert lane.seen["rewrite_cap"] == "0" and "B0REWRITE1" in lane.seen["existing"]


# ---------------------------------------------------------------- 出典の状態 (候補供給 P1b)

def _status(lane):
    import source_status
    return source_status.load(lane.state / "source_status.json")


def test_status_skips_not_due_and_records_results(lane):
    import source_status
    lane.state.mkdir(parents=True, exist_ok=True)
    source_status.save(lane.state / "source_status.json", {
        A1: {"state": "insufficient", "checked_at": "2026-11-10", "checked_urls": [],
             "recheck_after": "2027-02-08"}})
    lane.sites[A3] = 1
    w = Writer([wresult(article(A2))])
    assert lane.run(w, "--max-articles", "2") == 0
    # 候補選びの除外に入る (キーワード枠・ランキング枠・first-party 枠で同じ判定)
    assert A1 in lane.seen["existing"]
    # 候補に来ても開かない
    assert [r["asin"] for r in events(lane, "skip_status")] == [A1]
    st = _status(lane)
    assert st[A2]["state"] == "written"  # 記事ができた (行を消すとリポジトリの写しで戻る)
    assert st[A3]["state"] == "insufficient" and st[A3]["sites"] == 1 and st[A3]["need"] == 2
    assert st[A1]["checked_at"] == "2026-11-10"  # 開いていないので変えない


def test_status_new_urls_trigger_recheck(lane, monkeypatch):
    import source_status
    lane.state.mkdir(parents=True, exist_ok=True)
    source_status.save(lane.state / "source_status.json", {
        A1: {"state": "insufficient", "checked_at": "2026-11-10", "checked_urls": ["https://old.example.jp/"],
             "recheck_after": "2027-02-08"}})
    monkeypatch.setattr(bjp, "source_candidates", lambda asin, no_web=False: {
        "mall": [], "third": [("https://new.example.jp/", "")], "prior": [], "dead": set(),
        "news": set(), "first_party": set(), "asin": asin})
    w = Writer([wresult(article(A1))])
    assert lane.run(w, "--max-articles", "1") == 0
    assert A1 not in lane.seen["existing"]
    assert events(lane, "pr")[0]["asin"] == A1


def test_second_stage_fills_with_rewrites(lane, monkeypatch):
    import article_pick
    calls = []

    def fake_pick(existing, seed):
        calls.append(__import__("os").environ.get("REWRITE_PICKS_PER_RUN"))
        if len(calls) == 1:
            return [{"asin": A1, "pool": "ranking", "source_keyword": None}]
        return [{"asin": A1, "pool": "ranking", "source_keyword": None},
                {"asin": "B0REWRITE1", "pool": "rewrite-queue", "source_keyword": None}]

    monkeypatch.setattr(article_pick, "pick_from_repo", fake_pick)
    lane.sites[A1] = 1  # 新規は出典不足
    w = Writer([wresult(article("B0REWRITE1"))])
    assert lane.run(w, "--max-articles", "2", "--max-rewrites", "1") == 0
    assert calls == ["1", "2"]  # 2 段目は残りの本数 (2) だけ書き直しを拾う
    assert [r["asin"] for r in events(lane, "pr")] == ["B0REWRITE1"]
    assert ledger(lane)[-1]["second_stage"] == 1


def test_article_short_becomes_writable_article(lane):
    import source_status
    lane.state.mkdir(parents=True, exist_ok=True)
    source_status.save(lane.state / "source_status.json", {
        "B0ARTICLE1": {"state": "article_short", "checked_at": "2026-10-01", "checked_urls": [],
                       "recheck_after": "2026-10-31"},
        "B0ARTICLE2": {"state": "article_short", "checked_at": "2026-11-14", "checked_urls": [],
                       "recheck_after": "2026-12-14"}})
    lane.picked["origins"] = []
    lane.sites["B0ARTICLE1"] = 2
    assert lane.run(Writer([]), "--max-articles", "1") == 0
    st = _status(lane)
    assert st["B0ARTICLE1"]["state"] == "writable_article"
    assert st["B0ARTICLE2"]["state"] == "article_short"  # 期限前・候補が増えていない
    assert [r["asin"] for r in events(lane, "article_recheck")] == ["B0ARTICLE1"]


def test_dry_run_does_not_save_status(lane):
    lane.sites.update({A1: 1, A2: 1, A3: 1})
    assert lane.run(Writer([]), "--max-articles", "1", "--dry-run") == 0
    assert not (lane.state / "source_status.json").exists()


class StatusWorld(FakeWorld):
    def __init__(self, old_heads=()):
        super().__init__()
        self.old_heads = list(old_heads)

    def __call__(self, argv, input=None, **kw):
        if argv[:2] == ["git", "diff"]:
            self.calls.append((list(argv), input))
            return proc(1)  # 変更あり
        return super().__call__(argv, input, **kw)

    def gh(self, a, input_text):
        if a[:2] == ["pr", "list"] and "headRefName" in " ".join(a):
            return proc(0, json.dumps([{"number": 77, "headRefName": h} for h in self.old_heads]))
        if a[:2] == ["pr", "close"]:
            return proc(0)
        return super().gh(a, input_text)


def test_publish_status_merges_marks_and_replaces_old_pr(tmp_path, monkeypatch):
    import source_status
    repo = tmp_path / "repo"
    (repo / "data" / "articles").mkdir(parents=True)
    (repo / "data" / "articles" / "2026-11-19-B0ARTICLE1.json").write_text(
        json.dumps({"title": "題", "product_name": "商品"}), encoding="utf-8")
    monkeypatch.chdir(repo)
    source_status.save(source_status.REPO_PATH, {
        "B0OLD00001": {"state": "insufficient", "checked_at": "2026-11-01"},
        "B0ARTICLE1": {"state": "article_short", "checked_at": "2026-11-01"}})
    world = StatusWorld(old_heads=["data/source-status-k8w-20261122-023000"])
    env = wl.Env(runner=world, python="py", clock=lambda: NOW)
    ours = {"B0ARTICLE1": {"state": "writable_article", "checked_at": "2026-11-15"},
            "B0NEW00001": {"state": "insufficient", "checked_at": "2026-11-15"}}
    url = wl.publish_status(env, "20261115-023000", tmp_path, ours)
    assert url.endswith("/pull/9001")
    merged = source_status.load(source_status.REPO_PATH)
    assert set(merged) == {"B0OLD00001", "B0ARTICLE1", "B0NEW00001"}
    assert merged["B0ARTICLE1"]["state"] == "writable_article"
    marker = json.loads((repo / "data" / "rewrite_queue" / "B0ARTICLE1.json").read_text(encoding="utf-8"))
    assert marker["old_slug"] == "2026-11-19-B0ARTICLE1"
    assert world.gh_cmds("pr", "close")[0][3] == "77"
    title = world.gh_cmds("pr", "create")[0]
    assert "k8-writer" not in " ".join(title)  # 記事 PR の見張りに拾わせない
    assert world.gh_cmds("pr", "merge")


def test_queued_rewrite_is_written_when_its_turn_comes(lane):
    """writable_article (記事の点検で届いて書き直しの待ちに入れた) は、候補に来たら書く。"""
    import source_status
    lane.state.mkdir(parents=True, exist_ok=True)
    source_status.save(lane.state / "source_status.json", {
        A1: {"state": "writable_article", "checked_at": "2026-11-14", "checked_urls": []}})
    w = Writer([wresult(article(A1))])
    assert lane.run(w, "--max-articles", "1") == 0
    assert events(lane, "pr")[0]["asin"] == A1
    assert _status(lane)[A1]["state"] == "written"


def test_written_is_not_revived_by_repo_copy(lane):
    """記事を書いた商品の状態が、リポジトリの写し (古い insufficient) との合わせで戻らない。"""
    import source_status
    (lane.repo / "data").mkdir(exist_ok=True)
    source_status.save(lane.repo / source_status.REPO_PATH, {
        A1: {"state": "writable_article", "checked_at": "2026-11-10", "checked_urls": []}})
    w = Writer([wresult(article(A1))])
    assert lane.run(w, "--max-articles", "1") == 0
    st = source_status.merge(source_status.load(lane.repo / source_status.REPO_PATH), _status(lane))
    assert st[A1]["state"] == "written"


def test_forced_asin_is_not_skipped_by_status(lane):
    import source_status
    lane.state.mkdir(parents=True, exist_ok=True)
    source_status.save(lane.state / "source_status.json", {
        A1: {"state": "insufficient", "checked_at": "2026-11-10", "checked_urls": [],
             "recheck_after": "2027-02-08"}})
    w = Writer([wresult(article(A1))])
    assert lane.run(w, "--max-articles", "1", "--asin", A1) == 0
    assert not events(lane, "skip_status") and events(lane, "pr")[0]["asin"] == A1


def test_rewrite_candidate_short_becomes_article_short(lane):
    lane.picked["origins"] = [{"asin": "B0REWRITE1", "pool": "rewrite-queue", "source_keyword": None}]
    lane.sites["B0REWRITE1"] = 0  # B0R… は新発売の扱い (要るのは 1 サイト)
    assert lane.run(Writer([]), "--max-articles", "1") == 0
    assert _status(lane)["B0REWRITE1"]["state"] == "article_short"
