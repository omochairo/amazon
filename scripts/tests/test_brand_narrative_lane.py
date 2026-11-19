"""brand_narrative_lane (navi-brain#170 B4: ブランド紹介レーン) のテスト。

git / gh / python スクリプト / 書き手はすべて偽物を注入する (実物の agy・ollama・git・GitHub は呼ばない)。
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
import brand_narrative_lane as bl  # noqa: E402
import engagement_lane as el  # noqa: E402
import writer_lane as wl  # noqa: E402

TODAY = "2026-11-19"
TODAY_ISO = TODAY + "T10:00:00+09:00"
NOW = dt.datetime(2026, 11, 19, 1, 0, tzinfo=dt.timezone.utc)
BRAND = "テスト社"
SLUG = "tesuto"
IMPRESSIONS = "98765"
PAD = "やさしい手ざわりで、じっくり向き合える遊びがそろっています。"


def proc(rc=0, out="", err=""):
    return subprocess.CompletedProcess([], rc, out, err)


def stats(**over):
    d = {"count": 5, "avg_age_min_months": 30.0, "avg_ivs_100": 80.0, "lowest_price": 3980, "tier": "A",
         "top_3_series": ["ブロックA", "ブロックB", "ブロックC"], "representative_asin": "B0TEST00001",
         "representative_image": "https://x/i.jpg"}
    d.update(over)
    return d


INFO = {"brand": BRAND, "count": 5, "tier": "A", "avg_ivs_100": 80.0, "avg_age_min_months": 30.0,
        "lowest_price": 3980, "top_3_series_csv": "ブロックA, ブロックB, ブロックC", "is_refresh": False,
        "previous_last_generated_at": None}


def good_md(heading="## テスト社はなぜ「木のぬくもり系の入門」なのか", p1=None, p2=None, p3=None):
    p1 = p1 or ("テスト社は、**ブロックA**や**ブロックB**など、手で組み立てて遊ぶ商品を展開するブランドです。"
                "本サイトでも5点を扱っています。" + PAD)
    p2 = p2 or ("遊びを通じて、空間認識や巧緻性が育ちます。2歳ごろからの挑戦に向いています。" + PAD)
    p3 = p3 or ("最安 約 ¥3,980 から手に入ります。コスト対比で堅実なポジションです。" + PAD)
    return "%s\n\n%s\n\n%s\n\n%s" % (heading, p1, p2, p3)


def reasons(md, info=None):
    return [n for n, _ in bl.check_markdown(md, info or INFO)]


class FakeWorld:
    """git / gh / python の呼び出しを記録し、決めた結果を返す。"""

    def __init__(self, repo):
        self.repo = repo
        self.calls = []
        self.prs = []
        self.pr_count = 0
        self.fail_pr_create = False
        self.hub = {"brands": {BRAND: stats()}}
        self.qc = None
        self.hub_rc = 0

    def __call__(self, argv, input=None, **kw):
        self.calls.append((list(argv), kw.get("cwd")))
        if argv[0] == "py":
            if argv[1] == "scripts/build_brand_hub_stats.py":
                if self.hub_rc:
                    return proc(self.hub_rc, "", "boom")
                out = self.repo / "hugo" / "data" / "brand_hub.json"
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(json.dumps(self.hub, ensure_ascii=False), encoding="utf-8")
                return proc()
            if argv[1] == "scripts/build_brand_query_context.py":
                if self.qc is None:
                    return proc(1, "", "no data")
                Path(argv[argv.index("--out") + 1]).write_text(json.dumps(self.qc, ensure_ascii=False), encoding="utf-8")
                return proc()
            raise AssertionError("unexpected script %s" % argv)
        if argv[0] == "git":
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
            raise AssertionError("unexpected gh %s" % a)
        raise AssertionError("unexpected command %s" % argv)

    def cmds(self, tool, *prefix):
        return [c for c, _ in self.calls if c[0] == tool and c[1:1 + len(prefix)] == list(prefix)]


def wresult(md=None, error="", web_calls=0, reset_s=None, article="auto"):
    if article == "auto":
        article = None if md is None else {"markdown": md}
    return article_writer.WriteResult(
        ok=article is not None and not error, article=article, text="```json {} ```", error=error,
        error_text=error, usage={"total_tokens": 10000}, secs=30.0, backend="agy",
        model="gemini-4-hydrogen-high", reset_s=reset_s, conversation_id="cid",
        tool_calls={}, web_calls=web_calls)


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

    def fake_run(args, env):
        captured["args"] = args
        return 0

    orig = bl.cmd_run
    bl.cmd_run = fake_run
    try:
        bl.main(argv)
    finally:
        bl.cmd_run = orig
    return captured["args"]


@pytest.fixture()
def lane(tmp_path, monkeypatch):
    import yaml
    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    (repo / "data" / "term_slugs.yaml").write_text(yaml.safe_dump({BRAND: SLUG}, allow_unicode=True), encoding="utf-8")
    (repo / "hugo" / "content" / "brands").mkdir(parents=True)
    brain = tmp_path / "brain"
    brain.mkdir()
    (brain / "BRAND_NARRATIVE_PROMPT.md").write_text("ORIGINAL BRAND PROMPT\n", encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "nolocal"))
    state = tmp_path / "state"
    world = FakeWorld(repo)

    def run(writer, *extra, clock=NOW):
        env = el.LaneEnv(repo, runner=world, writer=writer, python="py", clock=lambda: clock)
        args = parse(["run", "--repo-dir", str(repo), "--brain-dir", str(brain), "--state-dir", str(state)]
                     + list(extra))
        return bl.cmd_run(args, env)

    return SimpleNamespace(repo=repo, brain=brain, state=state, world=world, run=run)


def ledger(lane):
    return wl.read_ledger(lane.state)


def events(lane, name):
    return [r for r in ledger(lane) if r["event"] == name]


def index_path(lane, slug=SLUG):
    return lane.repo / "hugo" / "content" / "brands" / slug / "_index.md"


def write_index(lane, text, slug=SLUG):
    p = index_path(lane, slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------- 検査

def test_good_markdown_passes():
    assert reasons(good_md()) == []
    body = good_md().split("\n", 1)[1]
    assert bl.BODY_MIN <= len(body.replace("\n", "").replace(" ", "").replace("*", "")) <= bl.BODY_MAX


def test_price_accepts_with_or_without_comma():
    assert reasons(good_md(p3="最安 約 ¥3980 から手に入ります。" + PAD)) == []


@pytest.mark.parametrize("kw,reason", [
    ({"heading": "## テスト社の紹介"}, "heading"),
    ({"heading": "## テスト社はなぜ「あ」なのか"}, "heading"),
    ({"heading": "## テスト社はなぜ「" + "あ" * 31 + "」なのか"}, "heading"),
    ({"heading": "# テスト社はなぜ「木のぬくもり」なのか"}, "heading"),
    ({"p2": "## 見出し\n遊びが育ちます。2歳ごろからです。" + PAD}, "heading_extra"),
    ({"p3": "最安 約 ¥3,980 から。" + PAD + "\n\n四つ目の段落です。"}, "paragraphs"),
    ({"p1": "短い。**ブロックA**と**ブロックB**と5点。"}, "length"),
    ({"p1": "テスト社は**ブロックA**だけ。5点。" + PAD}, "series"),
    ({"p3": "最安は手頃です。" + PAD}, "price"),
    ({"p3": "最安 約 ¥39,800 から。" + PAD}, "price"),
    ({"p2": "空間認識が育ちます。" + PAD}, "age"),
    ({"p2": "空間認識が育ちます。12歳ごろからです。" + PAD}, "age"),
    ({"p1": "テスト社は**ブロックA**や**ブロックB**の老舗。5点。" + PAD}, "forbidden"),
    ({"p1": "テスト社は**ブロックA**や**ブロックB**。創業は古い。5点。" + PAD}, "forbidden"),
    ({"p1": "テスト社は**ブロックA**や**ブロックB**。受賞歴あり。5点。" + PAD}, "forbidden"),
    ({"p1": "テスト社は**ブロックA**や**ブロックB**。No.1です。5点。" + PAD}, "forbidden"),
    ({"p3": "最安 約 ¥3,980 から。詳細は https://example.com へ。" + PAD}, "url"),
    ({"p3": "最安 約 ¥3,980 から。本サイトでは、おもちゃロボ独自の知育スコア (教育性 / 長寿命性 / 安全性 / "
            "コストパフォーマンスの 4 軸) で全ブランド商品を横断比較。最安値で 見つけられます。"}, "cta"),
    ({"p1": "テスト社は**ブロックA**や**ブロックB**。7点を扱います。" + PAD}, "number"),
    ({"p2": "空間認識が育ちます。2歳ごろからです。1歳半でも遊べます。" + PAD}, "number"),
])
def test_rule_violations(kw, reason):
    assert reason in reasons(good_md(**kw))


def test_digits_from_brand_and_series_are_allowed():
    info = dict(INFO, brand="4M", top_3_series_csv="STEAM5, ブロックB, ブロックC")
    md = good_md(heading="## 4Mはなぜ「実験キットの本命」なのか",
                 p1="4Mは**STEAM5**や**ブロックB**など、実験を楽しめる商品のブランドです。本サイトでも5点。" + PAD)
    assert reasons(md, info) == []
    bad = good_md(heading="## 4Mはなぜ「実験キットの本命」なのか",
                  p1="4Mは**STEAM5**や**ブロックB**など、実験を楽しめる商品のブランドです。本サイトでも7点。" + PAD)
    assert "number" in reasons(bad, info)


def test_missing_price_and_age_do_not_allow_zero():
    info = dict(INFO, lowest_price=None, avg_age_min_months=None)
    p2 = "遊びを通じて、空間認識や巧緻性が育ちます。" + PAD
    p3 = "手に取りやすい価格帯です。コスト対比で堅実なポジションです。" + PAD
    assert reasons(good_md(p2=p2, p3=p3), info) == []
    assert "number" in reasons(good_md(p2="0歳ごろから遊べます。" + PAD, p3=p3), info)
    assert "number" in reasons(good_md(p2=p2, p3="最安 約 ¥0 から。" + PAD), info)


def test_internal_terms_are_forbidden():
    md = good_md(p3="最安 約 ¥3,980 から。独自評価ではtier Aに位置します。" + PAD)
    assert "forbidden" in reasons(md, INFO)


def test_age_not_required_under_one_year():
    info = dict(INFO, avg_age_min_months=8)
    md = good_md(p2="遊びを通じて、空間認識や巧緻性が育ちます。" + PAD)
    assert reasons(md, info) == []


def test_fullwidth_digits_are_normalized():
    md = good_md(p3="最安 約 ¥３，９８０ から手に入ります。" + PAD)
    assert reasons(md) == []


# ---------------------------------------------------------------- GSC の検索語

def qc(**over):
    d = {"brand": BRAND, "weeks_covered": 8, "fallback": False,
         "queries": [{"query": "テスト社 積み木", "impressions_sum": int(IMPRESSIONS)}],
         "global_seo_hints": {"age_keywords_seen": ["2歳"], "intent_keywords_seen": ["おすすめ"]}}
    d.update(over)
    return d


def test_query_section_rules():
    s = bl.query_section(qc(), False)
    assert "「テスト社 積み木」(impressions計 %s)" % IMPRESSIONS in s and "直近8週" in s
    assert "年齢層キーワード: 2歳" in s and "購入意図キーワード: おすすめ" in s and "捏造禁止" in s
    g = bl.query_section(qc(), True)                       # 汎用ブランドは個別クエリを入れない
    assert IMPRESSIONS not in g and "年齢層キーワード" in g
    f = bl.query_section(qc(fallback=True), False)
    assert IMPRESSIONS not in f
    assert bl.query_section(qc(queries=[], global_seo_hints={}), False) == ""
    assert bl.query_section({}, False) == "" and bl.query_section(None, False) == ""


# ---------------------------------------------------------------- frontmatter

def test_kept_lines_keeps_aliases_list_and_comments_only():
    text = ('---\ntitle: "X"\n# 2026-09-27: 改名\naliases:\n  - "/brands/old/"\n  - "/brands/older/"\n'
            'auto_generated: true\nlast_generated_at: "2026-07-14T10:00:00+09:00"\n---\n本文\n')
    assert bl.kept_lines(text) == ["# 2026-09-27: 改名", "aliases:", '  - "/brands/old/"', '  - "/brands/older/"']
    assert bl.kept_lines('---\ntitle: "X"\naliases: ["/brands/a/"]\n---\n') == ['aliases: ["/brands/a/"]']
    assert bl.kept_lines('---\ntitle: "X"\n---\n') == [] and bl.kept_lines("本文だけ") == []


def test_build_file_layout():
    out = bl.build_file(BRAND, TODAY_ISO, good_md() + "\n", ['aliases: ["/brands/a/"]'])
    assert out.startswith('---\ntitle: "テスト社"\nauto_generated: true\nlast_generated_at: "%s"\n'
                          'aliases: ["/brands/a/"]\n---\n## テスト社はなぜ' % TODAY_ISO)
    assert out.endswith("\n") and not out.endswith("\n\n")


# ---------------------------------------------------------------- 対象の選び方

def test_forced_brand_must_be_eligible(lane):
    for hub in ({"brands": {}}, {"brands": {BRAND: stats(count=2)}},
                {"brands": {BRAND: {k: v for k, v in stats().items() if k != "tier"}}}):
        lane.world.hub = hub
        w = Writer([])
        assert lane.run(w, "--dry-run", "--brand", BRAND, "--run-id", "x%d" % len(w.calls) + str(id(hub))) == 1
        assert w.calls == []
    assert [e["error"] for e in events(lane, "error")] == ["brand_not_eligible"] * 3


def test_auto_select_prefers_brand_without_index(lane):
    import yaml
    (lane.repo / "data" / "term_slugs.yaml").write_text(
        yaml.safe_dump({BRAND: SLUG, "古い社": "furui"}, allow_unicode=True), encoding="utf-8")
    write_index(lane, '---\ntitle: "古い社"\nauto_generated: true\nlast_generated_at: "2026-01-01T10:00:00+09:00"\n---\n本文\n',
                slug="furui")
    lane.world.hub = {"brands": {"古い社": stats(), BRAND: stats()}}
    w = Writer([wresult(good_md())])
    assert lane.run(w, "--dry-run") == 0
    assert "【今回生成する brand】: %s" % BRAND in w.prompts[0]
    assert "is_refresh=False" in w.prompts[0]


def test_no_targets_does_nothing(lane):
    lane.world.hub = {"brands": {BRAND: stats(count=1)}}
    w = Writer([])
    assert lane.run(w, "--dry-run") == 0
    assert w.calls == [] and events(lane, "decision")[0]["reason"] == "no_targets"


def test_manual_narrative_is_skipped(lane):
    write_index(lane, '---\ntitle: "テスト社"\nauto_generated: false\n---\n手書き\n')
    w = Writer([])
    assert lane.run(w, "--dry-run", "--brand", BRAND) == 0
    assert w.calls == [] and events(lane, "decision")[0]["reason"] == "manual"
    assert "手書き" in index_path(lane).read_text(encoding="utf-8")


def test_missing_slug_is_an_error_and_never_creates_one(lane):
    (lane.repo / "data" / "term_slugs.yaml").write_text("{}\n", encoding="utf-8")
    w = Writer([])
    assert lane.run(w, "--dry-run", "--brand", BRAND) == 1
    assert w.calls == [] and events(lane, "error")[0]["error"] == "slug_missing"
    assert (lane.repo / "data" / "term_slugs.yaml").read_text(encoding="utf-8") == "{}\n"


def test_selector_output_keys_match_cli():
    info = bl.info_from_stats(BRAND, stats(), True, "2026-07-14T10:00:00+09:00")
    assert sorted(info) == sorted(["brand", "count", "tier", "avg_ivs_100", "avg_age_min_months", "lowest_price",
                                   "top_3_series_csv", "is_refresh", "previous_last_generated_at"])
    assert info["top_3_series_csv"] == "ブロックA, ブロックB, ブロックC"


def test_hub_build_failure_is_a_crash(lane):
    lane.world.hub_rc = 1
    w = Writer([])
    assert lane.run(w, "--dry-run") == 1 and w.calls == []


# ---------------------------------------------------------------- プロンプト

def test_prompt_layout_and_hub_built_in_repo_dir(lane):
    lane.world.qc = qc()
    w = Writer([wresult(good_md())])
    assert lane.run(w, "--dry-run") == 0
    p = w.prompts[0]
    assert p.index("# K8 の自動レーンからの実行") < p.index("【今回生成する brand】") < p.index("【読者が実際に検索") \
        < p.index("=== 以下、元のプロンプト ===") < p.index("ORIGINAL BRAND PROMPT")
    assert p.endswith("ORIGINAL BRAND PROMPT\n")
    assert "hugo/content/brands/%s/_index.md" % SLUG in p and TODAY_ISO in p
    for frag in ("count = 5", "tier = A", "avg_age_min_months = 30.0", "lowest_price = 3980",
                 "top_3_series = ブロックA, ブロックB, ブロックC", "is_refresh=False"):
        assert frag in p
    assert "frontmatter は書かない" in p and '{"markdown"' in p and "CTA" in p
    assert IMPRESSIONS in p                                   # 検索語はプロンプトにだけ入る
    assert w.calls[0]["instruction"] == bl.WRITER_INSTRUCTION and w.calls[0]["num_ctx"] == bl.DEFAULT_NUM_CTX
    assert ["py", "scripts/build_brand_hub_stats.py"] in lane.world.cmds("py")
    assert all(cwd == str(lane.repo) for _, cwd in lane.world.calls)


def test_query_context_failure_gives_empty_section(lane):
    lane.world.qc = None
    w = Writer([wresult(good_md())])
    assert lane.run(w, "--dry-run") == 0
    assert "読者が実際に検索している語" not in w.prompts[0]


# ---------------------------------------------------------------- 書き直し・破棄・枠

def test_rewrite_once_with_feedback(lane):
    bad = good_md(p3="最安は手頃です。" + PAD)
    seen = {}

    def second(kw):
        seen.update(kw)
        return wresult(good_md())

    w = Writer([wresult(bad), second])
    assert lane.run(w) == 0
    assert seen["previous_text"] and "price" in seen["feedback"] and "¥3980" in seen["feedback"]
    assert [(r["attempt"], r["passed"]) for r in events(lane, "check")] == [(1, False), (2, True)]
    assert events(lane, "pr")


def test_second_failure_means_no_pr_and_no_third_call(lane):
    bad = good_md(p3="最安は手頃です。" + PAD)
    w = Writer([wresult(bad), wresult(bad)])
    assert lane.run(w) == 0
    assert len(w.calls) == 2 and events(lane, "pr") == [] and events(lane, "short")
    assert lane.world.cmds("gh", "pr", "create") == []


def test_parse_failure_and_missing_markdown_get_one_rewrite(lane):
    w = Writer([wresult(None, error="parse"), wresult(article={"other": 1})])
    assert lane.run(w) == 0
    assert "JSON として読めなかった" in w.calls[1]["feedback"]
    assert len(w.calls) == 2 and events(lane, "pr") == []
    w2 = Writer([wresult(article={"other": 1}), wresult(good_md())])
    assert lane.run(w2, "--run-id", "b", "--dry-run", "--max-calls", "99") == 0
    assert "markdown (文字列) が無い" in w2.calls[1]["feedback"]


def test_web_calls_discards(lane):
    w = Writer([wresult(good_md(), web_calls=2)])
    assert lane.run(w) == 0
    assert events(lane, "pr") == [] and events(lane, "web_discard")[0]["web_calls"] == 2 and len(w.calls) == 1
    assert json.loads((lane.state / "ledger.jsonl").read_text(encoding="utf-8").splitlines()[-1])["web_discard"] == 1


def test_quota_exit_3_then_gated(lane):
    w = Writer([wresult(None, error="quota", reset_s=3600)])
    assert lane.run(w) == 3
    w2 = Writer([])
    assert lane.run(w2, "--run-id", "next") == 0
    assert w2.calls == [] and "quota wait" in events(lane, "gate")[-1]["reason"]


def test_one_round_per_day_and_dry_run_counted_separately(lane):
    bad = good_md(p3="最安は手頃です。" + PAD)
    assert lane.run(Writer([wresult(bad)] * 2), "--run-id", "r1", "--max-calls", "99") == 0
    w2 = Writer([])
    assert lane.run(w2, "--run-id", "r2", "--max-calls", "99") == 0
    assert w2.calls == [] and "rounds today" in events(lane, "gate")[-1]["reason"]
    w3 = Writer([wresult(good_md())])
    assert lane.run(w3, "--run-id", "r3", "--dry-run", "--max-calls", "99") == 0 and len(w3.calls) == 1


def test_daily_call_cap(lane):
    env = el.LaneEnv(lane.repo, clock=lambda: NOW)
    led = wl.Ledger(lane.state, "earlier", env)
    for _ in range(2):
        led.add("write", kind="brand_narrative", attempt=1, tokens=100)
    w = Writer([])
    assert lane.run(w) == 0
    assert w.calls == [] and "daily calls" in events(lane, "gate")[0]["reason"]


# ---------------------------------------------------------------- live の PR

def test_live_run_creates_pr_and_leaks_no_query_numbers(lane, capsys):
    lane.world.qc = qc()
    w = Writer([wresult(good_md())])
    assert lane.run(w) == 0
    git = lane.world.cmds("git")
    branch = "k8-brand/%s-%s" % (TODAY, SLUG)
    assert ["git", "fetch", "origin", "main"] in git
    assert any(c[:4] == ["git", "switch", "-C", branch] and c[4] == "origin/main" for c in git)
    assert ["git", "add", "--", "hugo/content/brands/%s/_index.md" % SLUG] in git
    assert any(c[:2] == ["git", "commit"] and c[3] == "data(brand): k8 ブランド紹介 %s" % BRAND for c in git)
    assert ["git", "push", "-u", "origin", branch] in git
    assert any(c[1] == "switch" and "--detach" in c for c in git[-4:])      # 作業ツリーは戻す
    create = lane.world.cmds("gh", "pr", "create")[0]
    assert create[create.index("--title") + 1] == "[k8] ブランド紹介: %s" % BRAND
    assert create[create.index("--head") + 1] == branch
    body = Path(create[create.index("--body-file") + 1]).read_text(encoding="utf-8")
    assert BRAND in body and SLUG in body and "新規" in body and "amazon-navi-brain#170 B4" in body
    assert "05 の自動マージの対象" in body and "ORIGINAL" not in body
    out = capsys.readouterr().out
    led_text = (lane.state / "ledger.jsonl").read_text(encoding="utf-8")
    for leak in (IMPRESSIONS, "テスト社 積み木", "ORIGINAL BRAND", "木のぬくもり系の入門"):
        assert leak not in out and leak not in led_text and leak not in body
    for c in lane.world.calls:
        assert IMPRESSIONS not in " ".join(c[0])
    result = json.loads(out.strip().splitlines()[-1])
    assert result["brand"] == BRAND and result["prs"] == 1 and result["passed"] == 1
    for k in ("calls", "tokens", "secs", "failed", "web_discard", "errors", "quota_stop", "gate", "decision", "run_id"):
        assert k in result
    assert not lane.world.cmds("gh", "pr", "merge")
    assert not (lane.state / "engagement.lock").exists()
    text = index_path(lane).read_text(encoding="utf-8")
    assert text.startswith('---\ntitle: "テスト社"\nauto_generated: true\nlast_generated_at: "%s"\n---\n## テスト社はなぜ' % TODAY_ISO)


def test_existing_aliases_are_kept_and_pr_says_replace(lane):
    write_index(lane, '---\ntitle: "テスト社"\n# 2026-09-27: 改名\naliases: ["/brands/old/"]\n---\n')
    w = Writer([wresult(good_md())])
    assert lane.run(w) == 0
    text = index_path(lane).read_text(encoding="utf-8")
    assert text.startswith('---\ntitle: "テスト社"\nauto_generated: true\nlast_generated_at: "%s"\n'
                           '# 2026-09-27: 改名\naliases: ["/brands/old/"]\n---\n## ' % TODAY_ISO)
    create = lane.world.cmds("gh", "pr", "create")[0]
    body = Path(create[create.index("--body-file") + 1]).read_text(encoding="utf-8")
    assert "置き換え" in body
    assert "is_refresh=True" in w.prompts[0]


def test_stub_with_only_title_is_replaced(lane):
    write_index(lane, '---\ntitle: "テスト社"\n---\n')
    assert lane.run(Writer([wresult(good_md())])) == 0
    text = index_path(lane).read_text(encoding="utf-8")
    assert text.count("title:") == 1 and "auto_generated: true" in text and "aliases" not in text


def test_open_pr_blocks_and_is_never_closed(lane):
    lane.world.prs = [{"number": 7, "url": "u", "headRefName": "k8-brand/%s-%s" % (TODAY, SLUG)},
                      {"number": 8, "url": "u", "headRefName": "k8-engagement/daily-" + TODAY}]
    w = Writer([])
    assert lane.run(w) == 0
    assert w.calls == [] and lane.world.cmds("gh", "pr", "create") == []
    assert lane.world.cmds("gh", "pr", "close") == [] and not any("--delete" in c for c in lane.world.cmds("git", "push"))
    assert events(lane, "decision")[0]["reason"] == "open_pr"
    assert lane.world.cmds("py") == []                         # 選定までも行かない


def test_other_lane_pr_does_not_block(lane):
    lane.world.prs = [{"number": 8, "url": "u", "headRefName": "k8-engagement/daily-" + TODAY}]
    assert lane.run(Writer([wresult(good_md())])) == 0 and events(lane, "pr")


def test_pr_create_failure_deletes_remote_branch(lane):
    lane.world.fail_pr_create = True
    assert lane.run(Writer([wresult(good_md())])) == 0
    assert events(lane, "error")[0]["stage"] == "pr" and events(lane, "pr") == []
    assert ["git", "push", "origin", "--delete", "k8-brand/%s-%s" % (TODAY, SLUG)] in lane.world.cmds("git")


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


# ---------------------------------------------------------------- dry-run

def test_dry_run_calls_no_git_or_gh_and_saves_index(lane):
    lane.world.qc = qc()
    w = Writer([wresult(good_md())])
    assert lane.run(w, "--dry-run") == 0
    assert lane.world.cmds("git") == [] and lane.world.cmds("gh") == []
    saved = next((lane.state / "runs").rglob("_index.md")).read_text(encoding="utf-8")
    assert saved.startswith('---\ntitle: "テスト社"') and events(lane, "pass")[0]["dry_run"] is True
    assert not index_path(lane).exists() and events(lane, "pr") == []


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
    assert a.max_calls == 2 and a.max_tokens == 120000 and a.backend == "agy" and a.brand == ""
    assert a.dry_run is False and a.num_ctx == bl.DEFAULT_NUM_CTX and a.model == wl.DEFAULT_MODEL
    a = parse(["run", "--brain-dir", "b", "--state-dir", "s", "--backend", "ollama", "--brand", "X", "--dry-run"])
    assert a.model == bl.OLLAMA_DEFAULT_MODEL and a.brand == "X" and a.dry_run
