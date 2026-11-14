"""記事の書き手を呼ぶ 1 つの口 (navi-brain#170 A1)。

「プロンプトを渡すと記事 JSON が返る」。書き手を替えるときはここだけを直す
(Jules の終了で、書き手の呼び出しがあちこちにあると全部止まることが分かった)。

backend:
- agy (既定): Google Antigravity CLI を K8 でヘッドレスに呼ぶ。モデルは既定で
  gemini-4-hydrogen-high (--model を省くと agy の既定が黙って変わるので必ず付ける)。
  **書き手に Web を使わせない** (A3): 作業フォルダに道具を read_file だけにした
  エージェント定義 (.agents/agents/navi-writer/agent.md) を置いて --agent で呼ぶ。
  --settings の deny では search_web が止まらなかった (2026-11-14 K8 で実測)。
  念のため会話ログ (transcript) の道具の呼び出しを数え、Web を使った回数を返す。
  ~/.gemini/antigravity-cli/settings.json (agy の MCP と共用) は触らない。
- ollama: K8 のローカル AI。今の長いプロンプトのままでは書けない (#170 2-3) ので予備。

プロンプト本体 (約 11 万バイト) は Windows のコマンドラインの上限を超えるので、作業フォルダに
書いて読ませる。書き手の出力・プロンプトはリポジトリの外 (workdir) にだけ置く。

使い方 (単発の試験):
    python scripts/article_writer.py --prompt-file p.txt --workdir <リポジトリの外> --out a.json
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


@dataclasses.dataclass
class WriteResult:
    ok: bool
    article: dict | None
    text: str
    error: str
    error_text: str
    usage: dict
    secs: float
    backend: str
    model: str
    reset_s: int | None = None
    conversation_id: str | None = None
    tool_calls: dict | None = None
    web_calls: int = 0

    def __post_init__(self):
        if self.tool_calls is None:
            self.tool_calls = {}


def _extract(text):
    blocks = re.findall(r"```json\s*(.*?)```", text, re.I | re.S)
    candidate = max(blocks, key=len) if blocks else ""
    if not candidate:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            candidate = text[start:end + 1]
    try:
        value = json.loads(candidate)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _reset_seconds(text):
    m = re.search(r"Resets in (?:(\d+)\s*d[a-z]*)?\s*(?:(\d+)\s*h[a-z]*)?\s*(?:(\d+)\s*m[a-z]*)?"
                  r"\s*(?:(\d+)\s*s[a-z]*)?", text, re.I)
    if not m or not any(m.groups()):
        return None  # 「Resets in soon」など読めない表記は不明 (呼び出し側の既定の待ちに任せる)
    d, h, minute, sec = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + minute * 60 + sec


def _error(text):
    low = text.lower()
    if "authentication required" in low:
        return "auth"
    if "resource_exhausted" in low or "quota reached" in low:
        return "quota"
    return "exit"


def _iter_tool_calls(obj):
    """transcript の 1 行から {"name", "args"} の道具の呼び出しを全部拾う (入れ子も見る)。"""
    if isinstance(obj, dict):
        calls = obj.get("tool_calls")
        if isinstance(calls, list):
            for c in calls:
                if isinstance(c, dict) and isinstance(c.get("name"), str):
                    yield c
        for k, v in obj.items():
            if k != "tool_calls" and isinstance(v, (dict, list)):
                for c in _iter_tool_calls(v):
                    yield c
    elif isinstance(obj, list):
        for v in obj:
            for c in _iter_tool_calls(v):
                yield c


def _audit(conversation_id, brain_dir=None):
    """会話ログの道具の呼び出しを数える。戻り値 (呼び出し回数の dict, Web を使った回数)。

    transcript.jsonl の各行は {"type": "PLANNER_RESPONSE", "tool_calls": [{"name", "args"}], ...}
    (2026-11-14 K8 の agy 1.5.9 で実測)。read_file の Path は値の両端に引用符が付いた形
    ("\\"C:\\\\...\\"") で入っているので、引用符を外してから http(s):// かを見る。
    読めなければ (None, 0): 監査できない記事は出さない (呼び出し側で audit_unavailable)。
    """
    root = Path(brain_dir) if brain_dir else Path.home() / ".gemini" / "antigravity-cli" / "brain"
    path = root / conversation_id / ".system_generated" / "logs" / "transcript.jsonl"
    try:
        data = path.read_text(encoding="utf-8")
    except OSError:
        return None, 0
    calls = {}
    web = 0
    for line in data.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        for c in _iter_tool_calls(obj):
            name = c["name"]
            calls[name] = calls.get(name, 0) + 1
            if name in ("search_web", "read_url", "read_url_content"):
                web += 1
            elif name == "read_file":
                args = c.get("args") if isinstance(c.get("args"), dict) else {}
                p = str(args.get("Path") or args.get("path") or "").strip().strip('"').strip()
                if p.lower().startswith(("http://", "https://")):
                    web += 1
    return calls, web


def write(prompt_text: str, *, workdir: str, backend: str = "agy", model: str | None = None,
          timeout_s: int = 900, previous_text: str | None = None, feedback: str | None = None,
          runner=None, brain_dir=None, sleep=None) -> WriteResult:
    started = time.monotonic()
    chosen_model = model or ("gemini-4-hydrogen-high" if backend == "agy" else "gemma4:26b-a4b-it-qat")
    def result(ok=False, article=None, text="", error="", error_text="", usage=None, reset_s=None,
               conversation_id=None, tool_calls=None, web_calls=0):
        return WriteResult(ok, article, text, error, error_text, usage or {}, time.monotonic()-started,
                           backend, chosen_model, reset_s, conversation_id, tool_calls or {}, web_calls)
    if backend == "ollama":
        prompt = "Write exactly one article JSON using only the supplied material. Output one ```json code block and nothing else. Never invent URLs.\n\n" + prompt_text
        if previous_text is not None or feedback is not None:
            prompt += "\n\nPrevious output:\n" + (previous_text or "") + "\n\nFailed checks:\n" + (feedback or "")
        payload = json.dumps({"model": chosen_model, "prompt": prompt, "stream": False,
                              "options": {"num_ctx": 65536}}).encode("utf-8")
        try:
            req = urllib.request.Request(os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434") + "/api/generate",
                                         data=payload, headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=timeout_s) as response:
                data = json.loads(response.read().decode("utf-8"))
            text = data.get("response", "")
            article = _extract(text)
            if not text: return result(text=text, error="empty")
            if article is None: return result(text=text, error="parse")
            return result(True, article, text, usage={"input_tokens": data.get("prompt_eval_count", 0), "output_tokens": data.get("eval_count", 0)})
        except Exception as exc:
            return result(error="exit", error_text=str(exc))
    if backend != "agy":
        return result(error="exit", error_text="unknown backend")
    wd = Path(workdir).resolve()
    agents = wd / ".agents/agents/navi-writer"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "agent.md").write_text("---\nname: navi-writer\ndescription: Writes one article JSON from a prompt file using only the material in that file. No web access.\nmainAgent: true\nsubagent: false\ntools: [read_file]\n---\n# Navi writer\n\nYou write exactly what the prompt file asks, using only the material inside it. You have no web access and must not try to obtain any. Never invent URLs.\n", encoding="utf-8")
    settings = wd / "settings.json"
    settings.write_text(json.dumps({"permissions": {"deny": ["read_url(*)", "command(*)", "write_file(*)"]}}), encoding="utf-8")
    prompt_file = wd / "prompt.txt"
    prompt_file.write_text(prompt_text, encoding="utf-8")
    previous_file, feedback_file = wd / "previous.txt", wd / "feedback.txt"
    if previous_text is not None and feedback is not None:
        previous_file.write_text(previous_text, encoding="utf-8")
        feedback_file.write_text(feedback, encoding="utf-8")
    else:
        for p in (previous_file, feedback_file):
            try: p.unlink()
            except FileNotFoundError: pass
    exe = os.environ.get("AGY_EXE")
    if not exe:
        candidate = Path(os.environ.get("LOCALAPPDATA", "")) / "agy/bin/agy.exe"
        exe = str(candidate) if candidate.is_file() else shutil.which("agy")
    if not exe:
        return result(error="not_found", error_text="agy executable not found")
    instruction = "Read the file " + str(prompt_file) + " completely (it is long: keep reading until the end) and follow its instructions to write exactly one article JSON. You cannot create files: output the article JSON as a single ```json code block and nothing else (no preface, no explanation). You have no web access. Use only the material in that file and never invent URLs."
    if previous_text is not None and feedback is not None:
        instruction += " Your previous attempt failed automated checks. Read " + str(previous_file) + " (your previous output) and " + str(feedback_file) + " (the failed checks), fix every problem, and output the complete corrected article JSON in the same format."
    argv = [exe, "--print", instruction, "--agent", "navi-writer", "--model", chosen_model, "--mode", "plan",
            "--print-timeout", str(timeout_s) + "s", "--output-format", "json", "--settings", str(settings)]
    run = runner or subprocess.run
    for attempt in range(2):
        try:
            proc = run(argv, cwd=str(wd), stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace",
                       capture_output=True, timeout=timeout_s + 60)
        except subprocess.TimeoutExpired as exc:
            return result(error="timeout", error_text=str(exc))
        stdout, stderr = proc.stdout or "", proc.stderr or ""
        combined = stdout + "\n" + stderr
        outer = {}
        if stdout.startswith("{"):
            try: outer = json.loads(stdout)
            except ValueError: pass
        text = outer.get("response", stdout) if isinstance(outer, dict) else stdout
        usage = outer.get("usage", {}) if isinstance(outer, dict) else {}
        cid = outer.get("conversation_id") if isinstance(outer, dict) else None
        if proc.returncode and re.search(r"\bUNAVAILABLE\b|\b503\b", combined) and attempt == 0:
            (sleep or time.sleep)(20)
            continue
        # 枠切れは終了コード 0 のまま空の応答で返ることもありうるので、応答が空なら文面でも見る
        if proc.returncode or (not text.strip() and _error(combined) in ("quota", "auth")):
            err = _error(combined)
            return result(text=text, error=err, error_text=combined, usage=usage, reset_s=_reset_seconds(combined) if err == "quota" else None, conversation_id=cid)
        if "print timeout after" in combined.lower(): return result(text=text, error="timeout", error_text=combined, usage=usage, conversation_id=cid)
        if not text.strip(): return result(text=text, error="empty", usage=usage, conversation_id=cid)
        article = _extract(text)
        if article is None: return result(text=text, error="parse", usage=usage, conversation_id=cid)
        # 道具の監査 (navi-brain#170 A3)。会話 ID が無い・ログが読めないときは、Web を使ったか
        # 確かめられないので記事を返さない
        calls, web = _audit(cid, brain_dir) if cid else (None, 0)
        if calls is None:
            return result(text=text, error="audit_unavailable", error_text="transcript not found",
                          usage=usage, conversation_id=cid)
        return result(True, article, text, usage=usage, conversation_id=cid, tool_calls=calls, web_calls=web)
    return result(error="exit", error_text="UNAVAILABLE")


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt-file", required=True); ap.add_argument("--workdir", required=True)
    ap.add_argument("--backend", choices=["agy", "ollama"], default="agy"); ap.add_argument("--model")
    ap.add_argument("--timeout", type=int, default=900); ap.add_argument("--out")
    ap.add_argument("--previous-file"); ap.add_argument("--feedback-file")
    args = ap.parse_args(argv)
    prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    previous = Path(args.previous_file).read_text(encoding="utf-8") if args.previous_file else None
    feedback = Path(args.feedback_file).read_text(encoding="utf-8") if args.feedback_file else None
    res = write(prompt, workdir=args.workdir, backend=args.backend, model=args.model, timeout_s=args.timeout,
                previous_text=previous, feedback=feedback)
    summary = dataclasses.asdict(res); summary.pop("article", None); summary.pop("text", None)
    print(json.dumps(summary, ensure_ascii=False))
    if res.ok and args.out:
        Path(args.out).write_text(json.dumps(res.article, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if res.ok else 3 if res.error == "quota" else 1


if __name__ == "__main__":
    sys.exit(main())
