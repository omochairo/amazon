"""sns_copy_lane (navi-brain#170 B2: SNS 商品投稿コピーレーン) のテスト。

git / gh / 書き手はすべて偽物を注入する (実物の agy・ollama・git・GitHub は呼ばない)。
"""
from __future__ import annotations

import datetime as dt
import json
import random
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import article_writer  # noqa: E402
import engagement_lane as el  # noqa: E402
import sns_copy_lane as sl  # noqa: E402
import writer_lane as wl  # noqa: E402
from _x_text import x_weight  # noqa: E402

TODAY = "2026-11-19"
NOW = dt.datetime(2026, 11, 19, 1, 0, tzinfo=dt.timezone.utc)  # 2026-11-19 10:00 JST
ASINS = ["B0AAAAAAA1", "B0AAAAAAA2", "B0AAAAAAA3"]
KANA = [c for c in "あいうえおかきくけさしすせそたちつてとなにぬねのはひふへほまみむめもやゆよらりるれろわをん"]
NARRATIVE_MARK = "本文のしるし"


def uniq(i, n=30):
    """互いに 3 文字単位の重なりがほぼ無い、ひらがなだけの本文 (禁止語に当たらない)。"""
    rnd = random.Random(i * 7919)
    pool = KANA[:]
    rnd.shuffle(pool)
    return "".join(pool[:n])


def proc(rc=0, out="", err=""):
    return subprocess.CompletedProcess([], rc, out, err)


def article(asin, extra_text=""):
    return {"title": "ブロック遊びの比較 " + asin, "meta_description": "対象 3 歳以上の木製ブロック。12 ピース入り。" + extra_text,
            "narrative": {"intro": NARRATIVE_MARK},
            "product": {"asin": asin, "name": "木製ブロック", "name_full": "木製ブロック 12ピース", "brand": "テスト社",
                        "target_age": "3歳以上", "edu_domains": ["空間認識", "創造力"], "certifications": ["ST"],
                        "features": ["12ピース入り", "天然木"], "pros": ["丈夫"], "cons": ["やや重い"], "best_price": 3980}}


def target(asin, i=0):
    return {"asin": asin, "filename": "2026-11-18-%s.json" % asin, "edu_axis": 4.1, "best_price": 3980,
            "score": 0.7 - i * 0.1, "title": "t"}


def crow(asin, i, **over):
    d = {"asin": asin, "x": uniq(i * 2 + 1), "threads": uniq(i * 2 + 2), "rationale": "知育が高い"}
    d.update(over)
    return d


class FakeWorld:
    """git / gh の呼び出しを記録し、決めた結果を返す。"""

    def __init__(self):
        self.calls = []
        self.files = {}          # git show origin/main:<rel> が返す中身
        self.prs = []
        self.pr_count = 0
        self.fail_pr_create = False

    def __call__(self, argv, input=None, **kw):
        self.calls.append((list(argv), kw.get("cwd")))
        if argv[0] == "git":
            if argv[1] == "show":
                rel = argv[2].split(":", 1)[1]
                return proc(0, self.files[rel]) if rel in self.files else proc(128, "", "not found")
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


def wresult(copies=None, error="", web_calls=0, reset_s=None, usage=None):
    art = None if copies is None else {"copies": copies}
    return article_writer.WriteResult(
        ok=art is not None and not error, article=art, text="```json {} ```", error=error,
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


def parse(argv):
    captured = {}

    def fake_run(args, env, select_fn=None):
        captured["args"] = args
        return 0

    orig = sl.cmd_run
    sl.cmd_run = fake_run
    try:
        sl.main(argv)
    finally:
        sl.cmd_run = orig
    return captured["args"]


@pytest.fixture()
def lane(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "data" / "articles").mkdir(parents=True)
    brain = tmp_path / "brain"
    brain.mkdir()
    (brain / "PROMPT_SNS_COPY.md").write_text("ORIGINAL SNS COPY PROMPT\n", encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "nolocal"))
    state = tmp_path / "state"
    world = FakeWorld()
    targets = [target(a, i) for i, a in enumerate(ASINS)]
    for t in targets:
        text = json.dumps(article(t["asin"]), ensure_ascii=False)
        world.files["data/articles/" + t["filename"]] = text
        (repo / "data" / "articles" / t["filename"]).write_text(text, encoding="utf-8")
    holder = SimpleNamespace(targets=targets, asked=[])

    def select_fn(repo_dir, n):
        holder.asked.append(n)
        return list(holder.targets)[:n]

    def run(writer, *extra, clock=NOW):
        env = el.LaneEnv(repo, runner=world, writer=writer, python="py",
                         clock=clock if callable(clock) else (lambda: clock))
        args = parse(["run", "--repo-dir", str(repo), "--brain-dir", str(brain), "--state-dir", str(state)]
                     + list(extra))
        return sl.cmd_run(args, env, select_fn=select_fn)

    return SimpleNamespace(repo=repo, brain=brain, state=state, world=world, run=run, holder=holder)


def ledger(lane):
    return wl.read_ledger(lane.state)


def events(lane, name):
    return [r for r in ledger(lane) if r["event"] == name]


def sns_files(lane):
    d = lane.repo / "data" / "sns_copy"
    return sorted(p.name for p in d.glob("*.json")) if d.exists() else []


def all_good():
    return [crow(a, i) for i, a in enumerate(ASINS)]


# ---------------------------------------------------------------- 対象の選び方

class _Src:
    def __init__(self, existing=()):
        self.existing = set(existing)

    def read(self, rel, required=False):
        asin = rel.rsplit("/", 1)[-1][:-5]
        return "{}" if asin in self.existing else None


def test_choose_targets_excludes_pending_and_existing_and_limits_top():
    cands = [target("B0AAAAAAA%d" % i, i) for i in range(1, 9)]
    asked = []

    def sel(repo, n):
        asked.append(n)
        return cands[:n]

    got = sl.choose_targets("r", 3, {"B0AAAAAAA1"}, _Src(existing=["B0AAAAAAA3"]), sel)
    # 本物の select は main にあるコピーを先に除くので、ここで除かれるのは保険の分 (A3) だけ
    assert [t["asin"] for t in got] == ["B0AAAAAAA2", "B0AAAAAAA4"]
    assert asked == [4]                      # top + pending の件数だけ多めに取る
    got = sl.choose_targets("r", 3, set(), _Src(), sel)
    assert [t["asin"] for t in got] == ["B0AAAAAAA1", "B0AAAAAAA2", "B0AAAAAAA3"]
    assert sl.choose_targets("r", 3, set(), _Src(), lambda r, n: []) == []


def test_choose_targets_ignores_bad_asin():
    got = sl.choose_targets("r", 6, set(), _Src(), lambda r, n: [{"asin": "bad"}, {"asin": None}, target("B0AAAAAAA1")])
    assert [t["asin"] for t in got] == ["B0AAAAAAA1"]


def test_repointed_swaps_and_restores_module_paths(tmp_path):
    import pick_sns_target as pst
    import sns_copy_store as store
    before = (pst.ARTICLES_DIR, pst.STATE_FILE, store.STORE_DIR)
    with sl.repointed(tmp_path):
        assert pst.ARTICLES_DIR == tmp_path / "data" / "articles"
        assert pst.STATE_FILE == tmp_path / "data" / "sns_published.json"
        assert store.STORE_DIR == tmp_path / "data" / "sns_copy"
    assert (pst.ARTICLES_DIR, pst.STATE_FILE, store.STORE_DIR) == before


def test_default_select_reads_the_repo_dir(tmp_path):
    (tmp_path / "data" / "articles").mkdir(parents=True)
    (tmp_path / "data" / "sns_copy").mkdir(parents=True)
    # 記事が 1 本もない作業ツリーでは対象 0 件 (自分のチェックアウトのデータを読まない)
    assert sl.default_select(tmp_path, 6) == []


def test_no_targets_does_nothing(lane):
    lane.holder.targets = []
    w = Writer([])
    assert lane.run(w) == 0
    assert w.calls == [] and events(lane, "pr") == [] and events(lane, "round") == []
    assert events(lane, "decision")[0]["reason"] == "no_targets"


# ---------------------------------------------------------------- 入力とプロンプト

def test_prompt_is_original_with_preamble_and_selected_keys(lane):
    w = Writer([wresult(all_good())])
    assert lane.run(w, "--dry-run") == 0
    p = w.prompts[0]
    assert p.index("# K8 の自動レーンからの実行") < p.index("## 入力") < p.index("# 元のプロンプト") < p.index("ORIGINAL SNS")
    assert p.endswith("ORIGINAL SNS COPY PROMPT\n")                  # 元のプロンプトは書き換えない
    assert "PR やファイルは作らない" in p and "{\"copies\"" in p
    for a in ASINS:
        assert "### " + a in p
    assert "edu_axis" in p and "木製ブロック 12ピース" in p and "3歳以上" in p and "空間認識" in p
    assert "やや重い" in p and "テスト社" in p and "対象 3 歳以上" in p
    assert NARRATIVE_MARK not in p                                    # 本文全体は貼らない
    assert w.calls[0]["backend"] == "agy" and w.calls[0]["instruction"] == sl.WRITER_INSTRUCTION
    assert w.calls[0]["num_ctx"] == sl.DEFAULT_NUM_CTX


def test_article_block_truncates_lists():
    a = article("B0AAAAAAA1")
    a["product"]["features"] = ["f%d" % i for i in range(20)]
    blk = sl.article_block(target("B0AAAAAAA1"), a)
    assert "f5" in blk and "f6" not in blk


def test_live_reads_articles_from_origin_main(lane):
    # 作業ツリーの記事を壊しても、live は git show origin/main の中身を使う
    for p in (lane.repo / "data" / "articles").glob("*.json"):
        p.write_text("{broken", encoding="utf-8")
    w = Writer([wresult(all_good())])
    assert lane.run(w) == 0
    assert "木製ブロック 12ピース" in w.prompts[0]
    assert ["git", "show", "origin/main:data/articles/2026-11-18-B0AAAAAAA1.json"] in lane.world.cmds("git")


def test_unreadable_article_target_is_skipped(lane):
    lane.world.files.pop("data/articles/2026-11-18-B0AAAAAAA2.json")
    w = Writer([wresult([crow(ASINS[0], 0), crow(ASINS[2], 2)])])
    assert lane.run(w) == 0
    assert "### B0AAAAAAA2" not in w.prompts[0]
    assert events(lane, "skip_target")[0]["asin"] == "B0AAAAAAA2"
    assert events(lane, "pr")[0]["asins"] == [ASINS[0], ASINS[2]]


# ---------------------------------------------------------------- 検査

POOL = sl._pool_facts(sl.article_pool(article("B0AAAAAAA1")))


def fails(copy):
    return [n for n, _ in sl.individual_checks(copy, set(ASINS), POOL)]


def good_copy(**over):
    d = {"asin": ASINS[0], "x": "木のブロックで、3歳のお子さんが夢中で積み上げます", "threads": "お? って顔で積み始めるところが好きです。12ピースで遊びが広がります",
         "rationale": "r"}
    d.update(over)
    return d


def test_good_copy_passes():
    assert fails(good_copy()) == []


def test_asin_and_missing_checks():
    assert fails(good_copy(asin="B0ZZZZZZZZ")) == ["asin"]
    assert fails(good_copy(asin=None)) == ["asin"]
    assert fails("not a dict") == ["missing"]
    assert "missing" in fails(good_copy(x=""))
    assert "missing" in fails(good_copy(threads="  "))
    assert "missing" in fails(good_copy(x=None))


def test_length_limits_x_weighted_and_threads_chars():
    assert fails(good_copy(x="あ" * 110)) == []                    # weighted 220
    assert fails(good_copy(x="あ" * 111)) == ["length"]            # weighted 222 (36 の上限 240 より手前)
    assert fails(good_copy(x="a" * 221)) == ["length"]
    assert fails(good_copy(threads="あ" * 180)) == []
    assert fails(good_copy(threads="あ" * 181)) == ["length"]      # プロンプトの 180 字 (36 の上限 480 より手前)
    assert sum(x_weight(c) for c in "あ" * 110) == 220


@pytest.mark.parametrize("text,reason", [
    ("詳細は https://example.com です", "url"),
    ("navi.omcha.jp に載っています", "url"),
    ("今日の積み木 #知育 #積み木 #おもちゃ", "hashtag"),
    ("今日の積み木 ＃知育＃a ＃b", "hashtag"),
    ("嬉しい✨😊🎉", "emoji"),
    ("いろママです。積み木が好き", "self_intro"),
    ("おもちゃいろ編集部です。積み木が好き", "self_intro"),
    ("積み木が好き\n— ママより", "signature"),
    ("ランキング上位の積み木です", "maker_view"),
    ("スコアが高い積み木です", "maker_view"),
    ("3,980円で買える積み木", "price"),
    ("¥3980 の積み木", "price"),
    ("今なら最安の積み木", "price"),
    ("在庫わずかの積み木", "price"),
    ("遊ぶだけで頭が良くなる積み木", "claim"),
    ("絶対に飽きない積み木", "claim"),
    ("発達を促進する積み木", "claim"),
    ("詳しくはこちらから", "cta"),
    ("気になる方はチェックしてね", "cta"),
    ("積み木が好き 👇", "cta"),
    ("0歳から遊べる積み木", "fact"),
    ("100ピースもある積み木", "fact"),
])
def test_rule_violations_are_caught_in_both_fields(text, reason):
    assert reason in fails(good_copy(x=text))
    assert reason in fails(good_copy(threads=text))


def test_ok_things_are_not_flagged():
    assert fails(good_copy(x="嬉しい✨😊 3 歳のお祝いに")) == []         # 絵文字 2 個・全角/半角の数字
    assert fails(good_copy(x="3歳")) == []
    assert fails(good_copy(x="３歳のお祝いに")) == []                    # 全角数字は NFKC で揃える
    assert fails(good_copy(x="12 ピース")) == []                         # 数字と単位の間の空白
    assert fails(good_copy(x="ありがとう♡♪★")) == []
    assert fails(good_copy(x="コスパの良い、長く遊べる積み木")) == []


def test_same_text_for_x_and_threads_fails():
    assert fails(good_copy(x="おなじ文", threads="おなじ文")) == ["same_text"]


def test_month_units_are_normalized():
    pool = sl._pool_facts(sl.article_pool({"a": "対象は18ヶ月から"}))
    assert sl._facts("18か月ごろ") <= pool and sl._facts("19カ月") - pool == {"19か月"}


def test_check_batch_ignores_unknown_asin_dups_and_reports_missing():
    targets = [target(a) for a in ASINS]
    pools = {a: sl.article_pool(article(a)) for a in ASINS}
    copies = [crow(ASINS[0], 0), crow(ASINS[0], 5), crow("B0ZZZZZZZZ", 6), crow(ASINS[1], 1, x="いろママです")]
    acc, bad, ignored = sl.check_batch(copies, targets, pools, {})
    assert list(acc) == [ASINS[0]] and ignored == 2
    reasons = {a: [n for n, _ in f] for a, f in bad}
    assert reasons == {ASINS[1]: ["self_intro"], ASINS[2]: ["missing"]}


def test_check_batch_rejects_near_duplicate_across_asins():
    targets = [target(a) for a in ASINS[:2]]
    pools = {a: sl.article_pool(article(a)) for a in ASINS}
    copies = [crow(ASINS[0], 0), crow(ASINS[1], 1, x=uniq(1))]       # 2 本目の x が 1 本目の x と同じ
    acc, bad, _ = sl.check_batch(copies, targets, pools, {})
    assert list(acc) == [ASINS[0]] and bad[0][1][0][0] == "dup_text"


def test_rationale_with_url_is_blanked_and_trimmed():
    targets = [target(ASINS[0])]
    pools = {ASINS[0]: sl.article_pool(article(ASINS[0]))}
    acc, _, _ = sl.check_batch([crow(ASINS[0], 0, rationale="見て https://x.example/a")], targets, pools, {})
    assert acc[ASINS[0]]["rationale"] == ""
    acc, _, _ = sl.check_batch([crow(ASINS[0], 0, rationale="あ" * 999)], targets, pools, {})
    assert len(acc[ASINS[0]]["rationale"]) == sl.RATIONALE_MAX


# ---------------------------------------------------------------- 書き直し

def test_rewrite_gets_feedback_and_pass_rows_are_combined(lane):
    first = [crow(ASINS[0], 0), crow(ASINS[1], 1, x="詳しくは https://example.com へ"), crow(ASINS[2], 2)]
    second = [crow(ASINS[1], 11), crow(ASINS[0], 12, x="別の文に差し替え")]      # 合格済みの ASIN は採らない
    seen = {}

    def second_writer(kw):
        seen.update(kw)
        return wresult(second)

    w = Writer([wresult(first), second_writer])
    assert lane.run(w) == 0
    assert w.calls[0]["previous_text"] is None
    assert seen["previous_text"] and "B0AAAAAAA2" in seen["feedback"] and "url" in seen["feedback"]
    assert "B0AAAAAAA1" not in seen["feedback"]
    pr = events(lane, "pr")[0]
    assert pr["asins"] == ASINS
    chk = events(lane, "check")
    assert [(r["attempt"], r["passed"], r["failed"], r["ignored"]) for r in chk] == [(1, 2, 1, 0), (2, 1, 0, 1)]
    saved = json.loads((lane.repo / "data/sns_copy/B0AAAAAAA1.json").read_text(encoding="utf-8"))
    assert saved["x"] == uniq(1)                     # 1 回目の合格コピーのまま


def test_second_failure_drops_only_that_asin_and_no_third_call(lane):
    bad = crow(ASINS[1], 1, threads="詳しくは https://example.com")
    w = Writer([wresult([crow(ASINS[0], 0), bad, crow(ASINS[2], 2)]), wresult([bad])])
    assert lane.run(w) == 0
    assert len(w.calls) == 2
    assert events(lane, "pr")[0]["asins"] == [ASINS[0], ASINS[2]]


def test_missing_asin_is_asked_for_in_rewrite(lane):
    w = Writer([wresult([crow(ASINS[0], 0), crow(ASINS[1], 1)]), wresult([crow(ASINS[2], 2)])])
    assert lane.run(w) == 0
    assert "B0AAAAAAA3" in w.calls[1]["feedback"] and "出力に無い" in w.calls[1]["feedback"]
    assert events(lane, "pr")[0]["asins"] == ASINS


def test_parse_failure_gets_one_rewrite(lane):
    w = Writer([wresult(None, error="parse"), wresult(all_good())])
    assert lane.run(w) == 0
    assert len(w.calls) == 2 and "JSON として読めなかった" in w.calls[1]["feedback"]


def test_everything_fails_means_no_pr(lane):
    junk = [crow(a, i, x="https://example.com") for i, a in enumerate(ASINS)]
    w = Writer([wresult(junk), wresult(junk)])
    assert lane.run(w) == 0
    assert events(lane, "pr") == [] and events(lane, "short")[0]["passed"] == 0
    assert lane.world.cmds("gh", "pr", "create") == []


def test_web_calls_discards_the_round(lane):
    w = Writer([wresult(all_good(), web_calls=2)])
    assert lane.run(w) == 0
    assert events(lane, "pr") == [] and events(lane, "discard")[0]["reason"] == "web_calls" and len(w.calls) == 1


def test_quota_stops_run_and_next_run_is_gated(lane):
    w = Writer([wresult(None, error="quota", reset_s=3600)])
    assert lane.run(w) == 3
    w2 = Writer([])
    assert lane.run(w2, "--run-id", "next") == 0
    assert w2.calls == [] and "quota wait" in events(lane, "gate")[-1]["reason"]


# ---------------------------------------------------------------- ファイルの新規作成だけ・36 の検査

def test_write_new_files_never_overwrites(tmp_path):
    d = tmp_path / "data" / "sns_copy"
    d.mkdir(parents=True)
    (d / "B0AAAAAAA1.json").write_text("ORIGINAL", encoding="utf-8")
    made = sl.write_new_files(tmp_path, {"B0AAAAAAA1": {"x": "n", "threads": "n"}, "B0AAAAAAA2": {"x": "n", "threads": "n"}})
    assert made == ["data/sns_copy/B0AAAAAAA2.json"]
    assert (d / "B0AAAAAAA1.json").read_text(encoding="utf-8") == "ORIGINAL"


def test_push_branch_refuses_when_every_file_already_exists(lane):
    d = lane.repo / "data" / "sns_copy"
    d.mkdir(parents=True)
    (d / "B0AAAAAAA1.json").write_text("ORIGINAL", encoding="utf-8")
    env = el.LaneEnv(lane.repo, runner=lane.world, clock=lambda: NOW)
    rec = {"asin": "B0AAAAAAA1", "x": "a", "threads": "b"}
    with pytest.raises(RuntimeError):
        sl.push_branch(env, lane.repo, "k8-sns-copy/x-1", {"B0AAAAAAA1": rec}, force=False)
    assert not [c for c in lane.world.cmds("git") if c[1] in ("commit", "push")]
    assert (d / "B0AAAAAAA1.json").read_text(encoding="utf-8") == "ORIGINAL"
    assert any(c[1] == "switch" and "--detach" in c for c in lane.world.cmds("git"))   # 作業ツリーは戻す


def sns_copy_check_36(path):
    """36-sns-copy-auto-merge.yml の Validate sns_copy JSON shape の検査をそのまま再現する。"""
    from _x_text import x_weight as xw, X_LIMIT
    asin_re = re.compile(r"^[A-Z0-9]{10}$")
    x_max, th_max = X_LIMIT - 40, 480
    errs = []
    p = Path(path)
    if not asin_re.match(p.stem):
        errs.append("asin")
    try:
        row = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return ["json"]
    if not isinstance(row, dict):
        return ["object"]
    x, th = row.get("x"), row.get("threads")
    has_x = isinstance(x, str) and x.strip()
    has_th = isinstance(th, str) and th.strip()
    if not (has_x or has_th):
        errs.append("neither")
    if has_x:
        if sum(xw(c) for c in x) > x_max:
            errs.append("x_len")
        if "http" in x:
            errs.append("x_http")
    if has_th:
        if len(th) > th_max:
            errs.append("th_len")
        if "http" in th:
            errs.append("th_http")
    return errs


def test_written_files_pass_the_36_checks_and_match_existing_shape(lane):
    w = Writer([wresult(all_good())])
    assert lane.run(w) == 0
    assert sns_files(lane) == ["%s.json" % a for a in ASINS]
    for a in ASINS:
        path = lane.repo / "data/sns_copy" / ("%s.json" % a)
        assert sns_copy_check_36(path) == []
        data = json.loads(path.read_text(encoding="utf-8"))
        assert list(data) == ["asin", "x", "threads", "rationale", "source", "generated_at"]
        assert data["asin"] == a and data["source"] == "k8_agy_v1"
        assert re.match(r"^2026-11-19T01:00:00\+00:00$", data["generated_at"])
        import sns_copy_store
        assert sns_copy_store.PLATFORMS == ("x", "threads")
    # 作ったファイルは 36 の範囲 (data/sns_copy/<ASIN>.json だけ) に収まる
    adds = [c for c in lane.world.cmds("git", "add")]
    assert adds == [["git", "add", "--"] + ["data/sns_copy/%s.json" % a for a in ASINS]]


def test_record_source_follows_backend():
    a = SimpleNamespace(backend="ollama", model="gemma4:26b-a4b-it-qat")
    assert sl.build_record("B0AAAAAAA1", {"x": "a", "threads": "b"}, NOW, a)["source"] == "k8_gemma_v1"
    a = SimpleNamespace(backend="agy", model="gemini-4-hydrogen-high")
    assert sl.build_record("B0AAAAAAA1", {"x": "a", "threads": "b"}, NOW, a)["source"] == "k8_agy_v1"


# ---------------------------------------------------------------- PR・上限・衝突・dry-run

def test_live_run_creates_pr_and_leaks_no_bodies(lane, capsys):
    w = Writer([wresult(all_good())])
    assert lane.run(w) == 0
    git = lane.world.cmds("git")
    branch = "k8-sns-copy/%s-1" % TODAY
    assert ["git", "fetch", "origin", "main"] in git
    assert any(c[:4] == ["git", "switch", "-C", branch] and c[4] == "origin/main" for c in git)
    assert ["git", "push", "-u", "origin", branch] in git
    assert not lane.world.cmds("gh", "pr", "merge")                         # merge は 36 に任せる
    assert all(cwd == str(lane.repo) for _, cwd in lane.world.calls)
    create = lane.world.cmds("gh", "pr", "create")[0]
    assert create[create.index("--title") + 1] == "[k8-sns-copy] %s 3 件" % TODAY
    assert create[create.index("--head") + 1] == branch
    body = Path(create[create.index("--body-file") + 1]).read_text(encoding="utf-8")
    assert all(a in body for a in ASINS) and uniq(1) not in body and "ORIGINAL" not in body
    out = capsys.readouterr().out
    led_text = (lane.state / "ledger.jsonl").read_text(encoding="utf-8")
    for leak in (uniq(1), uniq(2), "ORIGINAL SNS", "知育が高い"):
        assert leak not in out and leak not in led_text
    assert uniq(1) in next((lane.state / "runs").rglob("files.json")).read_text(encoding="utf-8")
    pend = json.loads((lane.state / "pending" / ("%s-1.json" % TODAY)).read_text(encoding="utf-8"))
    assert sorted(pend["files"]) == ASINS
    assert not (lane.state / "engagement.lock").exists()


def test_open_pr_blocks_generation(lane):
    lane.world.prs = [{"number": 7, "url": "u", "headRefName": "k8-sns-copy/%s-1" % TODAY, "mergeable": "MERGEABLE",
                       "createdAt": "2026-11-19T00:30:00Z"},
                      {"number": 8, "url": "u", "headRefName": "k8-engagement/daily-" + TODAY, "mergeable": "MERGEABLE",
                       "createdAt": "2026-11-19T00:30:00Z"}]
    w = Writer([])
    assert lane.run(w) == 0
    assert w.calls == [] and lane.world.cmds("gh", "pr", "create") == []
    assert events(lane, "decision")[0]["reason"] == "open_pr"


def test_other_lane_pr_does_not_block(lane):
    lane.world.prs = [{"number": 8, "url": "u", "headRefName": "k8-engagement/daily-" + TODAY,
                       "mergeable": "MERGEABLE", "createdAt": "2026-11-19T00:30:00Z"}]
    w = Writer([wresult(all_good())])
    assert lane.run(w) == 0 and len(w.calls) == 1 and events(lane, "pr")


def test_rounds_per_day_limit_is_two(lane):
    for n in (1, 2):
        w = Writer([wresult([crow(a, i, x="https://example.com") for i, a in enumerate(ASINS)])] * 2)    # 全部落ちる回
        assert lane.run(w, "--run-id", "r%d" % n, "--max-calls", "99") == 0
        assert len(w.calls) == 2
    w3 = Writer([])
    assert lane.run(w3, "--run-id", "r3", "--max-calls", "99") == 0
    assert w3.calls == [] and "rounds today" in events(lane, "gate")[-1]["reason"]
    assert len(events(lane, "round")) == 2


def test_second_round_of_the_day_uses_next_branch_number(lane):
    w = Writer([wresult(all_good())])
    assert lane.run(w, "--run-id", "a") == 0
    for p in (lane.repo / "data" / "sns_copy").glob("*.json"):
        p.unlink()                                             # 本物の git は back_to_main で消す (FakeWorld は消さない)
    w2 = Writer([wresult(all_good())])
    assert lane.run(w2, "--run-id", "b") == 0                 # PR は FakeWorld の open 一覧に出ていない (マージ済み扱い)
    assert [e["branch"] for e in events(lane, "pr")] == ["k8-sns-copy/%s-1" % TODAY, "k8-sns-copy/%s-2" % TODAY]


def test_daily_caps_calls_and_tokens(lane):
    env = el.LaneEnv(lane.repo, clock=lambda: NOW)
    led = wl.Ledger(lane.state, "earlier", env)
    for _ in range(4):
        led.add("write", kind="sns_copy", attempt=1, tokens=100)
    w = Writer([])
    assert lane.run(w) == 0
    assert w.calls == [] and "daily calls" in events(lane, "gate")[0]["reason"]
    state2 = lane.state.parent / "s2"
    wl.Ledger(state2, "earlier", env).add("write", kind="sns_copy", attempt=1, tokens=200000)
    assert wl.day_usage(wl.read_ledger(state2), TODAY, False)[1] == 200000


def test_conflicting_pr_is_rebuilt_with_same_branch_and_no_new_pr(lane):
    branch = "k8-sns-copy/%s-1" % TODAY
    rec = {"asin": ASINS[0], "x": uniq(1), "threads": uniq(2), "rationale": "r", "source": "k8_agy_v1",
           "generated_at": "2026-11-19T00:30:00+00:00"}
    (lane.state / "pending").mkdir(parents=True)
    (lane.state / "pending" / ("%s-1.json" % TODAY)).write_text(
        json.dumps({"date": TODAY, "branch": branch, "files": {ASINS[0]: rec}}, ensure_ascii=False), encoding="utf-8")
    lane.world.prs = [{"number": 7, "url": "https://github.com/omochairo/amazon/pull/7", "headRefName": branch,
                       "mergeable": "CONFLICTING", "createdAt": "2026-11-19T00:30:00Z"}]
    w = Writer([])
    assert lane.run(w) == 0
    git = lane.world.cmds("git")
    assert any(c[:2] == ["git", "push"] and "--force-with-lease" in c and c[-1] == branch for c in git)
    assert any(c[:4] == ["git", "switch", "-C", branch] and c[4] == "origin/main" for c in git)
    assert lane.world.cmds("gh", "pr", "create") == [] and w.calls == []
    assert events(lane, "rebuild")[0]["asins"] == [ASINS[0]]
    saved = json.loads((lane.repo / "data/sns_copy/B0AAAAAAA1.json").read_text(encoding="utf-8"))
    assert saved["x"] == uniq(1) and saved["generated_at"] == rec["generated_at"]    # 保存しておいた中身がそのまま


def test_conflicting_pr_without_pending_is_recorded_not_recreated(lane):
    branch = "k8-sns-copy/%s-1" % TODAY
    lane.world.prs = [{"number": 7, "url": "u", "headRefName": branch, "mergeable": "CONFLICTING",
                       "createdAt": "2026-11-19T00:30:00Z"}]
    assert lane.run(Writer([])) == 0
    assert events(lane, "error")[0]["error"] == "pending_missing" and lane.world.cmds("gh", "pr", "create") == []


def test_stale_pr_is_closed_then_generation_resumes(lane):
    lane.world.prs = [{"number": 7, "url": "u", "headRefName": "k8-sns-copy/%s-1" % TODAY, "mergeable": "MERGEABLE",
                       "createdAt": "2026-11-18T00:30:00Z"}]
    w = Writer([wresult(all_good())])
    assert lane.run(w) == 0
    closes = lane.world.cmds("gh", "pr", "close")
    assert len(closes) == 1 and closes[0][3] == "7" and events(lane, "closed")[0]["reason"] == "stale_24h"
    assert events(lane, "pr")


def test_pr_create_failure_deletes_remote_branch_and_records_error(lane):
    lane.world.fail_pr_create = True
    w = Writer([wresult(all_good())])
    assert lane.run(w) == 0
    assert events(lane, "error")[0]["stage"] == "pr" and events(lane, "pr") == []
    assert ["git", "push", "origin", "--delete", "k8-sns-copy/%s-1" % TODAY] in lane.world.cmds("git")
    assert not (lane.state / "pending" / ("%s-1.json" % TODAY)).exists()


def test_open_pr_list_failure_crashes_without_writing(lane):
    class Fail(FakeWorld):
        def __call__(self, argv, input=None, **kw):
            if argv[:3] == ["gh", "pr", "list"]:
                return proc(1, "", "boom")
            return super().__call__(argv, input, **kw)

    lane.world.__class__ = Fail
    w = Writer([])
    assert lane.run(w) == 1 and w.calls == []
    assert not (lane.state / "engagement.lock").exists()


def test_dry_run_calls_no_git_or_gh_and_saves_files(lane):
    w = Writer([wresult(all_good())])
    assert lane.run(w, "--dry-run") == 0
    assert lane.world.calls == []
    saved = json.loads(next((lane.state / "runs").rglob("files.json")).read_text(encoding="utf-8"))
    assert sorted(saved) == ASINS and events(lane, "pass")[0]["dry_run"] is True
    assert sns_files(lane) == [] and events(lane, "pr") == []


def test_dry_run_does_not_count_against_live_rounds(lane):
    lane.run(Writer([wresult(all_good())]), "--dry-run")
    assert lane.run(Writer([wresult(all_good())]), "--run-id", "live") == 0
    assert events(lane, "pr")[0]["run_id"] == "live"


def test_lock_blocks_second_run(lane):
    env = el.LaneEnv(lane.repo, clock=lambda: NOW)
    a = el.RunLock(lane.state, env)
    assert a.acquire() is True
    w = Writer([])
    assert lane.run(w) == 0
    assert events(lane, "locked") and events(lane, "run_start") == [] and w.calls == []
    a.release()


# ---------------------------------------------------------------- 引数

def test_cli_defaults():
    a = parse(["run", "--brain-dir", "b", "--state-dir", "s"])
    assert a.top == 6 and a.max_calls == 4 and a.max_tokens == 200000 and a.backend == "agy"
    assert a.dry_run is False and a.num_ctx == sl.DEFAULT_NUM_CTX and a.timeout == 900
    assert a.model == wl.DEFAULT_MODEL and a.repo_slug == "omochairo/amazon"


def test_cli_model_default_follows_backend():
    # agy 用の既定を ollama に渡すと HTTP 404 になった (engagement_lane の 2026-11-19 の試運転)
    a = parse(["run", "--brain-dir", "b", "--state-dir", "s", "--backend", "ollama"])
    assert a.model == sl.OLLAMA_DEFAULT_MODEL
    a = parse(["run", "--brain-dir", "b", "--state-dir", "s", "--backend", "ollama", "--model", "m", "--top", "3",
               "--dry-run", "--repo-dir", "r", "--max-calls", "2", "--max-tokens", "10"])
    assert a.model == "m" and a.top == 3 and a.dry_run and a.repo_dir == "r" and a.max_calls == 2 and a.max_tokens == 10


# ---------------------------------------------------------------- レビュー指摘 (agy) の回帰

def test_up_to_two_hashtags_are_ok():
    assert fails(good_copy(x="積み木で遊ぼう #知育 #積み木")) == []          # プロンプト: 1〜2 個まで
    assert fails(good_copy(threads="積み木で遊ぼう ＃知育")) == []
    assert fails(good_copy(x="記号だけ # ")) == ["hashtag"]


def test_sai_and_upper_units_are_checked_as_facts():
    pool = sl._pool_facts(sl.article_pool({"a": "対象年齢 3歳以上・幅20cm"}))
    assert sl._facts("3才から") <= pool
    assert sl._facts("20CM") <= pool
    assert sl._facts("4才から") - pool == {"4歳"}


def test_check_batch_keeps_asin_that_passes_later_in_same_answer():
    targets = [target(ASINS[0])]
    pools = {ASINS[0]: sl.article_pool(article(ASINS[0]))}
    copies = [crow(ASINS[0], 0, x="いろママです"), crow(ASINS[0], 1)]
    acc, bad, _ = sl.check_batch(copies, targets, pools, {})
    assert list(acc) == [ASINS[0]] and bad == []


def test_prune_pending_removes_closed_or_merged(tmp_path):
    pdir = tmp_path / "pending"
    pdir.mkdir()
    (pdir / "2026-11-19-1.json").write_text("{}", encoding="utf-8")
    (pdir / "2026-11-19-2.json").write_text("{}", encoding="utf-8")
    sl.prune_pending(tmp_path, [{"headRefName": "k8-sns-copy/2026-11-19-2"}])
    assert sorted(p.name for p in pdir.iterdir()) == ["2026-11-19-2.json"]


def test_push_branch_raises_nothing_to_commit(lane):
    d = lane.repo / "data" / "sns_copy"
    d.mkdir(parents=True)
    (d / "B0AAAAAAA1.json").write_text("ORIGINAL", encoding="utf-8")
    env = el.LaneEnv(lane.repo, runner=lane.world, clock=lambda: NOW)
    with pytest.raises(sl.NothingToCommit):
        sl.push_branch(env, lane.repo, "k8-sns-copy/x-1", {"B0AAAAAAA1": {"asin": "B0AAAAAAA1"}}, force=False)
