"""article_writer (navi-brain#170 A1: 書き手を呼ぶ 1 つの口) のテスト。

agy・Ollama は呼ばない (runner / urlopen を偽物に差し替える)。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import article_writer as aw  # noqa: E402

ARTICLE = {"slug": "2026-11-15-B0TEST0001", "title": "t"}


class Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def agy_json(response, cid="cid", usage=None):
    return json.dumps({"response": response, "conversation_id": cid, "status": "SUCCESS",
                       "usage": usage or {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}})


def make_transcript(brain, cid, calls):
    """実物の形 (2026-11-14 K8 の agy 1.5.9): 各行の tool_calls に {name, args}。Path は引用符付き。"""
    d = brain / cid / ".system_generated" / "logs"
    d.mkdir(parents=True)
    lines = [json.dumps({"step_index": i, "type": "PLANNER_RESPONSE", "tool_calls": [c]})
             for i, c in enumerate(calls)]
    lines.append(json.dumps({"step_index": 99, "type": "PLANNER_RESPONSE", "content": "done"}))
    (d / "transcript.jsonl").write_text("\n".join(lines), encoding="utf-8")


def local_read(path):
    return {"name": "read_file", "args": {"Path": '"' + path + '"'}}


def setup(tmp_path, monkeypatch, calls=None, cid="cid"):
    monkeypatch.setenv("AGY_EXE", "agy-fake")
    brain = tmp_path / "brain"
    make_transcript(brain, cid, calls if calls is not None else [local_read("C:\\ws\\prompt.txt")])
    return brain, tmp_path / "ws"


def test_extract_forms_and_reset():
    assert aw._extract('```json\n{"x":1}\n```') == {"x": 1}
    assert aw._extract('前置き\n```json\n{"x":1}\n```\n本体\n```json\n{"x": 1, "y": 2}\n```') == {"x": 1, "y": 2}
    assert aw._extract('prefix {"x": 2} suffix') == {"x": 2}
    assert aw._extract("no json") is None
    assert aw._extract("```json\n[1, 2]\n```") is None
    assert aw._reset_seconds("Individual quota reached. Resets in 48h3m41s.") == 173021
    assert aw._reset_seconds("Resets in 5m") == 300
    assert aw._reset_seconds("nothing") is None
    assert aw._reset_seconds("Resets in soon") is None
    assert aw._reset_seconds("Resets in 1d2h") == 93600


def test_success_argv_agent_and_settings(tmp_path, monkeypatch):
    brain, ws = setup(tmp_path, monkeypatch)
    seen = {}

    def runner(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        return Proc(stdout=agy_json("```json\n" + json.dumps(ARTICLE) + "\n```"))

    r = aw.write("PROMPT本文", workdir=str(ws), runner=runner, brain_dir=brain)
    assert r.ok and r.article == ARTICLE and r.error == ""
    assert r.web_calls == 0 and r.tool_calls == {"read_file": 1}
    assert r.usage["total_tokens"] == 15 and r.conversation_id == "cid"
    argv = seen["argv"]
    # --print の直後が指示文 (間に他のフラグを挟むと agy はそれを指示文として扱う)
    assert argv[0] == "agy-fake" and argv[1] == "--print" and "prompt.txt" in argv[2]
    assert argv[argv.index("--agent") + 1] == "navi-writer"
    assert argv[argv.index("--model") + 1] == "gemini-4-hydrogen-high"
    assert argv[argv.index("--mode") + 1] == "plan"
    settings = Path(argv[argv.index("--settings") + 1])
    deny = json.loads(settings.read_text(encoding="utf-8"))["permissions"]["deny"]
    assert "read_url(*)" in deny
    agent = (ws / ".agents" / "agents" / "navi-writer" / "agent.md").read_text(encoding="utf-8")
    assert "tools: [read_file]" in agent and "mainAgent: true" in agent
    assert (ws / "prompt.txt").read_text(encoding="utf-8") == "PROMPT本文"
    assert seen["kw"]["cwd"] == str(ws.resolve())
    assert seen["kw"]["stdin"] == subprocess.DEVNULL


def test_web_calls_are_counted_from_transcript(tmp_path, monkeypatch):
    calls = [{"name": "search_web", "args": {"query": '"x"'}},
             {"name": "search_web", "args": {"query": '"y"'}},
             local_read("https://example.org/x"),
             local_read("C:\\ws\\prompt.txt")]
    brain, ws = setup(tmp_path, monkeypatch, calls)
    r = aw.write("p", workdir=str(ws), brain_dir=brain,
                 runner=lambda *a, **k: Proc(stdout=agy_json('```json\n{"a":1}\n```')))
    assert r.ok
    assert r.web_calls == 3
    assert r.tool_calls == {"search_web": 2, "read_file": 2}


def test_missing_transcript_or_conversation_id_is_failure(tmp_path, monkeypatch):
    brain, ws = setup(tmp_path, monkeypatch)
    r = aw.write("p", workdir=str(ws), brain_dir=brain,
                 runner=lambda *a, **k: Proc(stdout=agy_json('```json\n{"a":1}\n```', cid="other")))
    assert not r.ok and r.error == "audit_unavailable" and r.article is None
    r = aw.write("p", workdir=str(ws), brain_dir=brain,
                 runner=lambda *a, **k: Proc(stdout=json.dumps({"response": '```json\n{"a":1}\n```'})))
    assert not r.ok and r.error == "audit_unavailable"


def test_quota(tmp_path, monkeypatch):
    brain, ws = setup(tmp_path, monkeypatch)
    err = ("E1112 agent executor error: RESOURCE_EXHAUSTED (code 429): Individual quota reached. "
           "Please upgrade your subscription to increase your limits. Resets in 48h3m41s.")
    r = aw.write("p", workdir=str(ws), brain_dir=brain, runner=lambda *a, **k: Proc(1, "", err))
    assert r.error == "quota" and r.reset_s == 173021 and not r.ok
    # 終了コード 0 で応答が空でも、文面が枠切れなら枠切れ
    r = aw.write("p", workdir=str(ws), brain_dir=brain, runner=lambda *a, **k: Proc(0, agy_json(""), err))
    assert r.error == "quota"


def test_transient_503_retries_once(tmp_path, monkeypatch):
    brain, ws = setup(tmp_path, monkeypatch)
    procs = [Proc(1, "", "Eligibility check failed: UNAVAILABLE"),
             Proc(0, agy_json('```json\n{"a":1}\n```'))]
    slept = []
    r = aw.write("p", workdir=str(ws), brain_dir=brain, runner=lambda *a, **k: procs.pop(0),
                 sleep=slept.append)
    assert r.ok and slept == [20] and procs == []


def test_transient_twice_is_exit(tmp_path, monkeypatch):
    brain, ws = setup(tmp_path, monkeypatch)
    r = aw.write("p", workdir=str(ws), brain_dir=brain, sleep=lambda s: None,
                 runner=lambda *a, **k: Proc(1, "", "503 UNAVAILABLE"))
    assert r.error == "exit" and not r.ok


def test_auth_timeout_empty_parse(tmp_path, monkeypatch):
    brain, ws = setup(tmp_path, monkeypatch)

    def run(proc):
        return aw.write("p", workdir=str(ws), brain_dir=brain, runner=lambda *a, **k: proc)

    assert run(Proc(1, "", "Authentication required")).error == "auth"
    assert run(Proc(0, agy_json(""), "Authentication required")).error == "auth"
    # 時間切れの途中までの応答は使わない (壊れた形の記事を出さない)
    assert run(Proc(0, agy_json('```json\n{"a":1}\n```'), "print timeout after 900s")).error == "timeout"
    assert run(Proc(0, agy_json("   "))).error == "empty"
    assert run(Proc(0, agy_json("記事は書けませんでした"))).error == "parse"
    assert run(Proc(2, "", "boom")).error == "exit"

    def raise_timeout(*a, **k):
        raise subprocess.TimeoutExpired("agy", 1)

    r = aw.write("p", workdir=str(ws), brain_dir=brain, runner=raise_timeout)
    assert r.error == "timeout"


def test_not_found(tmp_path, monkeypatch):
    monkeypatch.delenv("AGY_EXE", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "nowhere"))
    monkeypatch.setattr(aw.shutil, "which", lambda name: None)
    r = aw.write("p", workdir=str(tmp_path / "ws"))
    assert r.error == "not_found"


def test_rewrite_writes_previous_and_feedback(tmp_path, monkeypatch):
    brain, ws = setup(tmp_path, monkeypatch)
    seen = {}

    def runner(argv, **kw):
        seen["argv"] = argv
        return Proc(stdout=agy_json('```json\n{"a":1}\n```'))

    aw.write("p", workdir=str(ws), brain_dir=brain, runner=runner,
             previous_text="前の出力", feedback="- quality_gate: sources_v5 ...")
    assert (ws / "previous.txt").read_text(encoding="utf-8") == "前の出力"
    assert "sources_v5" in (ws / "feedback.txt").read_text(encoding="utf-8")
    assert "previous.txt" in seen["argv"][2] and "feedback.txt" in seen["argv"][2]
    # 通常の回では前回の書き直し用ファイルを消す
    aw.write("p", workdir=str(ws), brain_dir=brain, runner=runner)
    assert not (ws / "previous.txt").exists() and not (ws / "feedback.txt").exists()
    assert "previous.txt" not in seen["argv"][2]


def test_ollama_success_and_failure(monkeypatch):
    sent = {}

    class Response:
        def read(self):
            return json.dumps({"response": '```json\n{"ok":true}\n```',
                               "prompt_eval_count": 4, "eval_count": 2}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def urlopen(req, timeout=None):
        sent["body"] = json.loads(req.data.decode("utf-8"))
        return Response()

    monkeypatch.setattr(aw.urllib.request, "urlopen", urlopen)
    r = aw.write("材料", workdir=".", backend="ollama")
    assert r.ok and r.article == {"ok": True} and r.usage == {"input_tokens": 4, "output_tokens": 2}
    assert r.web_calls == 0 and sent["body"]["stream"] is False and "材料" in sent["body"]["prompt"]

    def refuse(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(aw.urllib.request, "urlopen", refuse)
    r = aw.write("p", workdir=".", backend="ollama")
    assert not r.ok and r.error == "exit" and "refused" in r.error_text
