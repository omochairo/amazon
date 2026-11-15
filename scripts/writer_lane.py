#!/usr/bin/env python3
"""K8 の記事レーン (navi-brain#170): Jules の代わりに agy に新記事を書かせ、合格分だけ PR にする。

Jules (03-invoke-jules.yml) は 2026-11-11 で終わった。このスクリプトは K8 の定時タスク
(\\k8\\navi_writer・正本は amazon-home-ops scripts/navi_writer.cmd) から 1 日 1 回呼ばれる。

    商品を選ぶ (article_pick.pick_from_repo = 03 と同じ規則)
     → 出典の事前確認 (A4): 候補ページを開いて抜粋を取り、商品名の語が強く一致した
       候補が quality_gate の数え方で別サイト 2 件以上あるか。無ければ書かない
     → jules-lock/<ASIN> を取る (A6: 03 と同じ方法。03 / 12 もこのロックを見て除外する)
     → プロンプト (build_jules_prompt.build_prompt(no_web=True)・抜粋は事前確認の結果を使い回す)
     → 書き手 (article_writer.write・agy の道具は read_file だけ = Web を使わせない, A3)
     → K8 の中で検査: スキーマ・quality_gate --strict --no-cert-fetch・URL の照合・道具の監査
     → 不合格なら検査の結果を渡して 1 回だけ書き直し (A5)
     → 合格分だけ PR (タイトルに ASIN)。あとは 04 の検証 → 05 の自動マージ (下流は変えない, Q6)

枠切れ (A8): agy が 429 を返したらその回はそこでやめ、`Resets in` まで (最大 24 時間) 次の回を
飛ばす。記事の側の 1 日の上限 (本数・呼び出し回数・トークン) で、Claude の調査で使う agy の
枠 (Gemini は共通の枠) を食い潰さない。

記録 (C1): <state>/ledger.jsonl に 1 行 1 イベント。`report` が直近の集計を private の
navi-brain の記録用 issue にコメントする。public の amazon (PR の本文・コミット) には
プロンプトの中身・抜粋・数値を書かない。プロンプト・書き手の出力は <state> (リポジトリの外) にだけ置く。

使い方 (cwd はリポジトリ直下。jules/ に navi-brain が要る):
    python scripts/writer_lane.py run --state-dir C:\\Users\\AIsys\\k8jobs\\navi-writer [--dry-run]
    python scripts/writer_lane.py report --state-dir ... --issue N [--post]

K8 の Python は 3.11 だが、runner (3.8) と同じく 3.8 で import できる書き方にしている。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from source_rules import required_non_sales_sites
except ModuleNotFoundError:  # package 形式
    from scripts.source_rules import required_non_sales_sites

JST = dt.timezone(dt.timedelta(hours=9))
ASIN_RE = re.compile(r"B0[A-Z0-9]{8}")
URL_RE = re.compile(r"https?://[^\s\"'<>`\\]+")
URL_TRAIL = ".,)]」』、。）"
LOCK_PREFIX = "jules-lock/"
PR_TITLE = "[k8-writer] 記事生成: {asin}"
FAILED_TITLE = "[k8-writer] 不合格: {asin}"
DEFAULT_MODEL = "gemini-4-hydrogen-high"
# 出典不足で外した ASIN を選び直さない日数。素材 (34 / 57 の事前収集) は日次で増えるので短めにする
SKIP_SOURCES_DAYS = 3
# 枠切れの待ちの上限 (Resets in が長くても、小さな依頼は通ることがある: agy MCP の 2026-10-16 実測)
QUOTA_WAIT_DEFAULT_S = 6 * 3600
QUOTA_WAIT_MAX_S = 24 * 3600
FEEDBACK_MAX = 4000
GATE_LINE_MAX = 300
SIDECAR_SUFFIXES = (".enrichment.json", ".quality.json", ".seo.json")


# ---------------------------------------------------------------- 外の世界 (テストで差し替える)

class Env:
    """gh / git / python のサブプロセス・Web 取得・書き手・時計をまとめる。テストは偽物を渡す。"""

    def __init__(self, repo_slug="omochairo/amazon", runner=None, fetch=None, writer=None,
                 clock=None, python=None):
        self.repo_slug = repo_slug
        self._runner = runner or subprocess.run
        self.fetch = fetch  # None なら source_pages.fetch_page (build_jules_prompt の既定)
        self._writer = writer
        self._clock = clock
        self.python = python or sys.executable

    def now(self):
        return self._clock() if self._clock else dt.datetime.now(dt.timezone.utc)

    def run(self, argv, input_text=None, timeout=600):
        return self._runner(argv, input=input_text, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=timeout)

    def gh(self, args, input_text=None):
        return self.run(["gh"] + list(args), input_text=input_text)

    def gh_json(self, args):
        r = self.gh(args)
        if r.returncode != 0:
            return None
        try:
            return json.loads(r.stdout or "null")
        except ValueError:
            return None

    def write(self, prompt, **kw):
        if self._writer:
            return self._writer(prompt, **kw)
        import article_writer
        return article_writer.write(prompt, **kw)


# ---------------------------------------------------------------- 台帳

def iso(t):
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(s):
    try:
        t = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def jst_day(t):
    return t.astimezone(JST).strftime("%Y-%m-%d")


class Ledger:
    def __init__(self, state_dir, run_id, env):
        self.path = Path(state_dir) / "ledger.jsonl"
        self.run_id = run_id
        self.env = env

    def add(self, event, **fields):
        now = self.env.now()
        row = {"ts": iso(now), "jst_date": jst_day(now), "run_id": self.run_id, "event": event}
        row.update(fields)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row


def read_ledger(state_dir):
    path = Path(state_dir) / "ledger.jsonl"
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return rows


def tokens_of(usage):
    usage = usage or {}
    total = usage.get("total_tokens")
    if isinstance(total, (int, float)):
        return int(total)
    return int((usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0))


def day_usage(rows, day, dry_run):
    """その JST 日の書き手の呼び出し回数・トークン・出した本数 (dry-run は合格数)。"""
    calls = tokens = done = 0
    for r in rows:
        if r.get("jst_date") != day:
            continue
        ev = r.get("event")
        if ev == "write":
            calls += 1
            tokens += int(r.get("tokens") or 0)
        elif ev == "pr" and not dry_run:
            done += 1
        elif ev == "pass" and dry_run:
            done += 1
    return calls, tokens, done


# ---------------------------------------------------------------- 関門 (枠切れ・1 日の上限)

def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def quota_gate(state_dir, model, now, mcp_quota_path=None):
    """書いてはいけない理由 (枠切れの待ち) を返す。無ければ ""。"""
    until = (read_json(Path(state_dir) / "quota_until.json", {}) or {}).get("until") or 0
    if until > now.timestamp():
        return f"lane quota wait until {iso(dt.datetime.fromtimestamp(until, dt.timezone.utc))}"
    if mcp_quota_path is None:
        mcp_quota_path = Path(os.environ.get("LOCALAPPDATA", "")) / "agy" / "quota.json"
    mcp = read_json(mcp_quota_path, {}) or {}
    blocked = mcp.get(model) if isinstance(mcp, dict) else None
    if isinstance(blocked, (int, float)) and blocked > now.timestamp():
        return f"agy MCP recorded quota for {model} until {iso(dt.datetime.fromtimestamp(blocked, dt.timezone.utc))}"
    return ""


def mark_quota(state_dir, now, reset_s):
    wait = min(int(reset_s or QUOTA_WAIT_DEFAULT_S), QUOTA_WAIT_MAX_S)
    until = now.timestamp() + wait
    path = Path(state_dir) / "quota_until.json"
    path.write_text(json.dumps({"until": until, "set_at": iso(now), "reset_s": reset_s}), encoding="utf-8")
    return until


# ---------------------------------------------------------------- 候補の選び方 (03 と同じ規則)

def article_asins(articles_dir="data/articles"):
    out = set()
    for p in Path(articles_dir).glob("*.json"):
        if p.name.endswith(SIDECAR_SUFFIXES):
            continue
        m = ASIN_RE.search(p.name)
        if m:
            out.add(m.group(0))
    return out


def open_pr_asins(env):
    rows = env.gh_json(["pr", "list", "-R", env.repo_slug, "--state", "open", "--limit", "300",
                        "--json", "title"])
    if rows is None:
        raise RuntimeError("gh pr list (open) failed: cannot exclude in-flight ASINs")
    return {a for r in rows for a in ASIN_RE.findall(str(r.get("title") or ""))}


def locked_asins(env):
    r = env.gh(["api", f"repos/{env.repo_slug}/git/matching-refs/heads/{LOCK_PREFIX}",
                "--jq", ".[].ref"])
    if r.returncode != 0:
        raise RuntimeError("gh api matching-refs failed: cannot see jules-lock/")
    return {a for a in ASIN_RE.findall(r.stdout or "")}


def recent_skips(rows, now, days=SKIP_SOURCES_DAYS):
    since = now - dt.timedelta(days=days)
    out = set()
    for r in rows:
        if r.get("event") == "skip_sources" and r.get("asin"):
            t = parse_ts(r.get("ts"))
            if t and t >= since:
                out.add(r["asin"])
    return out


def write_closed_rows(env, rows, state_dir):
    """#9239 の冷却に、台帳の不合格 (書き直しても落ちた) を「マージされずに閉じた PR」として足す。"""
    closed = env.gh_json(["pr", "list", "-R", env.repo_slug, "--state", "closed",
                          "--search", "is:unmerged sort:updated-desc", "--limit", "300",
                          "--json", "title,closedAt"])
    out = list(closed) if isinstance(closed, list) else []
    for r in rows:
        if r.get("event") == "failed" and r.get("asin"):
            out.append({"title": FAILED_TITLE.format(asin=r["asin"]), "closedAt": r.get("ts")})
    path = Path(state_dir) / "closed_article_prs.json"
    path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return path, closed is not None


def pick(env, rows, state_dir, run_id, forced):
    if forced:
        return [{"asin": a, "pool": "", "source_keyword": None} for a in forced]
    existing = article_asins() | open_pr_asins(env) | locked_asins(env) | recent_skips(rows, env.now())
    # 03 は書き直し待ち (rewrite_queue) を除外から外して候補に戻すが、このレーンはまだ新記事だけ
    path, _ = write_closed_rows(env, rows, state_dir)
    os.environ["CLOSED_ARTICLE_PRS"] = str(path)
    os.environ["REWRITE_PICKS_PER_RUN"] = "0"
    os.environ.pop("JULES_RECENT_SESSIONS", None)
    import article_pick
    return article_pick.pick_from_repo(existing, run_id)


# ---------------------------------------------------------------- 出典の事前確認 (A4)

def precheck(asin, fetch):
    import build_jules_prompt as bjp
    import quality_gate
    cand = bjp.source_candidates(asin, no_web=True)
    targets = bjp.excerpt_targets(cand)
    excerpts = bjp._fetch_excerpts(asin, targets, fetch=fetch) if targets else {}
    citable = bjp.citable_urls(cand, excerpts)
    sites = quality_gate._non_sales_sites([{"url": u} for u in citable])
    return cand, excerpts, citable, sites


# ---------------------------------------------------------------- ロック (03 と同じ)

class LockSession:
    def __init__(self, env):
        self.env = env
        self._base = None
        self.held = set()

    def _main(self):
        if self._base is None:
            slug = self.env.repo_slug
            sha = self.env.gh(["api", f"repos/{slug}/git/refs/heads/main", "--jq", ".object.sha"])
            tree = self.env.gh(["api", f"repos/{slug}/git/commits/{(sha.stdout or '').strip()}",
                                "--jq", ".tree.sha"]) if sha.returncode == 0 else sha
            if sha.returncode != 0 or tree.returncode != 0:
                raise RuntimeError("could not read main for jules-lock")
            self._base = ((sha.stdout or "").strip(), (tree.stdout or "").strip())
        return self._base

    def acquire(self, asin):
        sha, tree = self._main()
        ts = iso(self.env.now())
        body = json.dumps({"message": f"jules-lock {asin}", "tree": tree, "parents": [sha],
                           "author": {"name": "github-actions", "email": "actions@github.com", "date": ts},
                           "committer": {"name": "github-actions", "email": "actions@github.com", "date": ts}})
        c = self.env.gh(["api", "-X", "POST", f"repos/{self.env.repo_slug}/git/commits",
                         "--input", "-", "--jq", ".sha"], input_text=body)
        commit = (c.stdout or "").strip()
        if c.returncode != 0 or not commit:
            return False
        r = self.env.gh(["api", "-X", "POST", f"repos/{self.env.repo_slug}/git/refs",
                         "-f", f"ref=refs/heads/{LOCK_PREFIX}{asin}", "-f", f"sha={commit}"])
        if r.returncode != 0:
            return False
        self.held.add(asin)
        return True

    def release(self, asin):
        if asin not in self.held:
            return
        self.env.gh(["api", "-X", "DELETE", f"repos/{self.env.repo_slug}/git/refs/heads/{LOCK_PREFIX}{asin}"])
        self.held.discard(asin)

    def keep(self, asin):
        """PR を出した: ロックは 05 が自動マージを掛けるときに外す (06 も 1 時間で回収する)。"""
        self.held.discard(asin)

    def release_all(self):
        for a in list(self.held):
            self.release(a)


# ---------------------------------------------------------------- 検査

def normalize_url(u):
    u = u.strip()
    while u and u[-1] in URL_TRAIL:
        u = u[:-1]
    try:
        p = urlparse(u)
        host = (p.hostname or "").lower()
        port = f":{p.port}" if p.port else ""
    except ValueError:
        return u
    q = f"?{p.query}" if p.query else ""
    return f"{p.scheme.lower()}://{host}{port}{p.path.rstrip('/')}{q}"


def urls_in_text(text):
    return {normalize_url(m) for m in URL_RE.findall(text or "")}


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            for s in _strings(v):
                yield s
    elif isinstance(obj, list):
        for v in obj:
            for s in _strings(v):
                yield s


def is_amazon_dp(url, asin):
    try:
        p = urlparse(url)
    except ValueError:
        return False
    host = (p.hostname or "").lower()
    return (host == "amazon.co.jp" or host.endswith(".amazon.co.jp")) and f"/dp/{asin}" in p.path


def is_sale_source(src):
    """販売ページか。ホストで見る (quality_gate._is_sales_source は URL の部分一致なので、
    クエリに amazon.co.jp を含む第三者の URL まで販売扱いになり、抜粋の照合をすり抜ける)。"""
    import quality_gate
    import source_sites
    host = source_sites.host_of(str(src.get("url") or ""))
    if not host:
        return False
    return source_sites.is_retail(host) or any(host == h or host.endswith("." + h)
                                               for h in quality_gate._SALES_PAGE_HOSTS)


def check_urls(article, asin, allowed, citable):
    """プロンプトに無い URL・抜粋で確かめていない非販売の出典を拾う (A3)。"""
    fails = []
    bad = []
    for s in _strings(article):
        for raw in URL_RE.findall(s):
            if is_amazon_dp(raw, asin):
                continue
            if normalize_url(raw) not in allowed:
                bad.append(raw)
    if bad:
        uniq = list(dict.fromkeys(bad))
        fails.append(("url_not_in_prompt", "プロンプトに無い URL がある (使えるのはプロンプトにある URL だけ): "
                      + ", ".join(uniq[:8])))
    citable_n = {normalize_url(u) for u in citable}
    not_citable = []
    for src in article.get("sources") or []:
        if not isinstance(src, dict):
            continue
        url = str(src.get("url") or "")
        if not url or is_amazon_dp(url, asin) or is_sale_source(src):
            continue
        if normalize_url(url) not in citable_n:
            not_citable.append(url)
    if not_citable:
        fails.append(("source_not_citable", "非販売の出典は「抜粋」が付いた候補からだけ選ぶ。抜粋の無い出典: "
                      + ", ".join(not_citable[:8])))
    return fails


def run_gate(env, article, asin, today):
    """スキーマと quality_gate を 04 と同じ条件 (cert の出典本文の取得も含む) で K8 の中で回す。"""
    fails = []
    score = None
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / f"{today}-{asin}.json"
        f.write_text(json.dumps(article, ensure_ascii=False, indent=2), encoding="utf-8")
        r = env.run([env.python, "scripts/validate_json.py", "--json", str(f),
                     "--schema", "data/schema/article.schema.json"])
        if r.returncode != 0:
            tail = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()[-6:]
            fails.append(("schema", " / ".join(line[:GATE_LINE_MAX] for line in tail) or "schema failed"))
        g = env.run([env.python, "scripts/quality_gate.py", "--src", tmp + os.sep,
                     "--posts", "hugo/content/posts/", "--schema", "data/schema/article.schema.json",
                     "--strict"])
        out = (g.stdout or "") + (g.stderr or "")
        m = re.search(r"score=(\d+)", out)
        if m:
            score = int(m.group(1))
        if g.returncode != 0:
            reasons = [line.strip()[2:] for line in out.splitlines() if line.startswith("    - ")]
            if not reasons:
                reasons = [out.strip()[-GATE_LINE_MAX:] or f"exit {g.returncode}"]
            for line in reasons:
                name = line.split(":", 1)[0].strip()
                fails.append((f"quality_gate:{name}", line[:GATE_LINE_MAX]))
    return fails, score


def check_article(env, wr, asin, today, allowed, citable):
    fails = []
    art = wr.article
    if wr.web_calls:
        fails.append(("web_calls", f"書き手が Web を使った ({wr.web_calls} 回)。Web は使えない"))
    if art.get("slug") != f"{today}-{asin}" or not str(art.get("date") or "").startswith(today):
        fails.append(("slug_date", f"slug は {today}-{asin}、date は {today} で始める"))
    fails.extend(check_urls(art, asin, allowed, citable))
    gate_fails, score = run_gate(env, art, asin, today)
    fails.extend(gate_fails)
    return fails, score


def feedback_text(fails):
    body = "\n".join(f"- {name}: {msg}" for name, msg in fails)
    head = ("前回の出力は次の自動検査で不合格になった。全部直して、記事 JSON の全体を出し直す。"
            "プロンプトに無い URL・抜粋の無い出典は足さない。\n")
    return (head + body)[:FEEDBACK_MAX]


# ---------------------------------------------------------------- PR

PR_BODY = """K8 の記事レーン（k8-writer）が生成した記事です。navi-brain#170。

- 書き手: {backend} / {model}（Web は使わせていない）
- K8 での検査: スキーマ・quality_gate --strict（score {score}）・出典 URL の照合 合格
- 書き直し: {rewrites} 回
"""


def git(env, *args):
    return env.run(["git"] + list(args))


def fresh_branch(env, branch):
    """最新の origin/main からブランチを切る (run は 1 時間を超えうるので毎回 fetch する)。"""
    r = git(env, "fetch", "origin", "main")
    if r.returncode != 0:
        raise RuntimeError(f"git fetch failed: {(r.stderr or '').strip()[:300]}")
    r = git(env, "switch", "-C", branch, "origin/main")
    if r.returncode != 0:
        raise RuntimeError(f"git switch failed: {(r.stderr or '').strip()[:300]}")


def back_to_main(env, branch, paths=()):
    """作業ツリーを origin/main に戻す。途中で落ちたときの書きかけ・追跡外のファイルも消す。"""
    git(env, "switch", "--discard-changes", "--detach", "origin/main")
    for rel in paths:
        git(env, "clean", "-fq", "--", rel)
    git(env, "branch", "-D", branch)


def create_pr(env, asin, today, article, meta, state_dir):
    stamp = env.now().astimezone(JST).strftime("%Y%m%d%H%M")
    branch = f"add-article-{asin}-k8w-{stamp}"
    rel = f"data/articles/{today}-{asin}.json"
    try:
        fresh_branch(env, branch)
        with open(rel, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(article, ensure_ascii=False, indent=2))
        for args in (("add", "--", rel),
                     ("commit", "-m", f"data(article): {asin} の記事を追加 (k8-writer)"),
                     ("push", "-u", "origin", branch)):
            r = git(env, *args)
            if r.returncode != 0:
                raise RuntimeError(f"git {args[0]} failed: {(r.stderr or r.stdout or '').strip()[:300]}")
        body = Path(state_dir) / "pr_body.md"
        body.write_text(PR_BODY.format(**meta), encoding="utf-8")
        r = env.gh(["pr", "create", "-R", env.repo_slug, "--base", "main", "--head", branch,
                    "--title", PR_TITLE.format(asin=asin), "--body-file", str(body)])
        if r.returncode != 0:
            raise RuntimeError(f"gh pr create failed: {(r.stderr or '').strip()[:300]}")
        url = (r.stdout or "").strip().splitlines()[-1] if (r.stdout or "").strip() else ""
        return url, branch
    finally:
        back_to_main(env, branch, (rel,))


def record_origin(env, prs, run_id, state_dir):
    """#4964 の出自台帳 (03 と同じ data/analytics/asin_origin.jsonl) を 1 本の PR で足す。"""
    rows = [{"asin": p["asin"], "pool": p["pool"], "source_keyword": p.get("source_keyword")}
            for p in prs if p.get("pool")]
    if not rows:
        return ""
    inp = Path(state_dir) / "origin_input.jsonl"
    inp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    branch = f"data/asin-origin-k8w-{run_id}"
    try:
        fresh_branch(env, branch)
        r = env.run([env.python, "scripts/record_asin_origin.py", "--input", str(inp),
                     "--workflow", "k8-article-writer", "--run-id", f"k8w-{run_id}",
                     "--out", "data/analytics/asin_origin.jsonl"])
        if r.returncode != 0:
            raise RuntimeError(f"record_asin_origin failed: {(r.stderr or '').strip()[:300]}")
        # 題に k8-writer を入れない (63 の見張りと report は「k8-writer in:title」で記事 PR を探す)
        title = f"data(analytics): ASIN origin ledger (k8w-{run_id})"
        for args in (("add", "--", "data/analytics/asin_origin.jsonl"), ("commit", "-m", title),
                     ("push", "-u", "origin", branch)):
            r = git(env, *args)
            if r.returncode != 0:
                raise RuntimeError(f"git {args[0]} failed")
        r = env.gh(["pr", "create", "-R", env.repo_slug, "--base", "main", "--head", branch,
                    "--title", title, "--body",
                    "#4964 観察項目3 用の出自台帳 append (K8 の記事レーン, navi-brain#170)。記録するだけで、比率計算はしない。"])
        if r.returncode != 0:
            raise RuntimeError("gh pr create failed")
        url = (r.stdout or "").strip().splitlines()[-1]
        env.gh(["pr", "merge", "--auto", "--squash", "--delete-branch", url, "-R", env.repo_slug])
        return url
    finally:
        back_to_main(env, branch)


# ---------------------------------------------------------------- run

def save(dirpath, name, text):
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / name).write_text(text, encoding="utf-8")


def write_once(env, ledger, prompt, asin, attempt, args, state_dir, previous=None, feedback=None):
    wr = env.write(prompt, workdir=str(Path(state_dir) / "ws"), backend=args.backend,
                   model=args.model, timeout_s=args.timeout, previous_text=previous, feedback=feedback)
    ledger.add("write", asin=asin, attempt=attempt, ok=wr.ok, error=wr.error,
               tokens=tokens_of(wr.usage), secs=round(wr.secs, 1), web_calls=wr.web_calls,
               model=wr.model, conversation_id=wr.conversation_id)
    return wr


def process(env, ledger, locks, args, state_dir, run_dir, origin, today, stats, prs):
    """候補 1 件を書いて検査し、合格なら PR。戻り値 "stop" ならその回をやめる。"""
    asin = origin["asin"]
    stats["prechecked"] += 1
    cand, excerpts, citable, sites = precheck(asin, env.fetch)
    if len(sites) < required_non_sales_sites(asin):
        stats["skip_sources"] += 1
        ledger.add("skip_sources", asin=asin, sites=len(sites), n_citable=len(citable),
                   n_candidates=len(excerpts))
        return ""
    if not args.dry_run and not locks.acquire(asin):
        stats["lock_busy"] += 1
        ledger.add("lock_busy", asin=asin)
        return ""
    import build_jules_prompt as bjp
    prompt = bjp.build_prompt(asin, today=today, no_web=True, excerpts=excerpts)
    adir = run_dir / asin
    save(adir, "prompt.txt", prompt)
    allowed = urls_in_text(prompt)
    previous = feedback = None
    fails = []
    for attempt in (1, 2):
        stats["calls"] += 1
        wr = write_once(env, ledger, prompt, asin, attempt, args, state_dir, previous, feedback)
        stats["tokens"] += tokens_of(wr.usage)
        stats["secs"] += wr.secs
        save(adir, f"response-{attempt}.txt", wr.text or "")
        if wr.error == "quota":
            until = mark_quota(state_dir, env.now(), wr.reset_s)
            locks.release(asin)
            stats["quota_stop"] = True
            ledger.add("quota", asin=asin, reset_s=wr.reset_s,
                       until=iso(dt.datetime.fromtimestamp(until, dt.timezone.utc)))
            return "stop"
        if wr.error in ("empty", "parse"):
            fails = [(wr.error, "記事 JSON として読めなかった。```json のコードブロック 1 個だけを出す")]
        elif wr.error:
            locks.release(asin)
            stats["errors"] += 1
            ledger.add("error", asin=asin, stage="write", error=wr.error,
                       detail=(wr.error_text or "")[-500:])
            return "stop" if wr.error == "auth" else ""
        else:
            fails, score = check_article(env, wr, asin, today, allowed, citable)
            if not fails:
                stats["passed"] += 1
                rewrites = attempt - 1
                save(adir, "article.json", json.dumps(wr.article, ensure_ascii=False, indent=2))
                if args.dry_run:
                    ledger.add("pass", asin=asin, dry_run=True, score=score, attempts=attempt)
                    return ""
                meta = {"backend": args.backend, "model": wr.model, "score": score, "rewrites": rewrites}
                try:
                    url, branch = create_pr(env, asin, today, wr.article, meta, state_dir)
                except Exception as e:  # noqa: BLE001
                    locks.release(asin)
                    stats["errors"] += 1
                    ledger.add("error", asin=asin, stage="pr", error="pr", detail=str(e)[:500])
                    return ""
                locks.keep(asin)
                stats["prs"] += 1
                prs.append({"asin": asin, "pool": origin.get("pool") or "",
                            "source_keyword": origin.get("source_keyword")})
                ledger.add("pr", asin=asin, url=url, branch=branch, score=score, attempts=attempt,
                           pool=origin.get("pool") or "", source_keyword=origin.get("source_keyword"))
                return ""
        if attempt == 1:
            previous = wr.text or ""
            feedback = feedback_text(fails)
            save(adir, "feedback.txt", feedback)
    locks.release(asin)
    stats["failed"] += 1
    details = feedback_text(fails)
    save(adir, "failed.txt", details)
    ledger.add("failed", asin=asin, reasons=sorted({n for n, _ in fails}), details=details[:1500])
    return ""


def cmd_run(args, env):
    state = Path(args.state_dir)
    state.mkdir(parents=True, exist_ok=True)
    now = env.now()
    run_id = args.run_id or now.astimezone(JST).strftime("%Y%m%d-%H%M%S")
    ledger = Ledger(state, run_id, env)
    today = jst_day(now)
    stats = {"candidates": 0, "prechecked": 0, "skip_sources": 0, "lock_busy": 0, "calls": 0,
             "tokens": 0, "secs": 0.0, "passed": 0, "prs": 0, "failed": 0, "errors": 0,
             "quota_stop": False, "gate": ""}
    ledger.add("run_start", dry_run=args.dry_run, backend=args.backend, model=args.model,
               max_articles=args.max_articles)
    locks = LockSession(env)
    prs = []
    code = 0
    try:
        rows = read_ledger(state)
        gate = quota_gate(state, args.model, now) if args.backend == "agy" else ""
        calls, tokens, done = day_usage(rows, today, args.dry_run)
        if not gate and calls >= args.max_calls:
            gate = f"daily calls {calls} >= {args.max_calls}"
        if not gate and tokens >= args.max_tokens:
            gate = f"daily tokens {tokens} >= {args.max_tokens}"
        if not gate and done >= args.max_articles:
            gate = f"daily articles {done} >= {args.max_articles}"
        if gate:
            stats["gate"] = gate
            ledger.add("gate", reason=gate)
        else:
            run_dir = state / "runs" / run_id
            origins = pick(env, rows, state, run_id, args.asin)
            stats["candidates"] = len(origins)
            for origin in origins:
                if done + stats["prs" if not args.dry_run else "passed"] >= args.max_articles:
                    break
                if calls + stats["calls"] >= args.max_calls or tokens + stats["tokens"] >= args.max_tokens:
                    ledger.add("gate", reason="daily cap reached during run")
                    break
                if stats["prechecked"] >= args.max_prechecks:
                    break
                if jst_day(env.now()) != today:
                    # 日をまたいだら止める (slug の日付と 1 日の上限の数え方がずれる)。続きは次の回
                    ledger.add("gate", reason="JST date changed during run")
                    break
                if process(env, ledger, locks, args, state, run_dir, origin, today, stats, prs) == "stop":
                    break
            if prs and not args.dry_run:
                try:
                    url = record_origin(env, prs, run_id, state)
                    if url:
                        ledger.add("origin", url=url)
                except Exception as e:  # noqa: BLE001 — 台帳の PR が出なくても記事は出ている
                    ledger.add("error", stage="origin", error="origin", detail=str(e)[:500])
        if stats["quota_stop"]:
            code = 3
    except Exception as e:  # noqa: BLE001
        code = 1
        stats["errors"] += 1
        ledger.add("error", stage="crash", error=type(e).__name__,
                   detail=traceback.format_exc()[-1500:])
    finally:
        locks.release_all()
    stats["secs"] = round(stats["secs"], 1)
    ledger.add("run_end", **stats)
    print(json.dumps(dict(stats, run_id=run_id), ensure_ascii=False))
    return code


# ---------------------------------------------------------------- report

def _runs(rows):
    runs = {}
    for r in rows:
        runs.setdefault(r.get("run_id"), []).append(r)
    return runs


def build_report(rows, pr_states, now, days=7):
    """台帳から直近の集計を Markdown (日本語) にする。pr_states は {url: state} か None。"""
    runs = _runs(rows)
    ends = [r for r in rows if r.get("event") == "run_end"]
    if not ends:
        return "## 記事レーン（k8-writer）\n\n台帳にまだ回の記録が無い。\n"
    last_end = ends[-1]
    last_id = last_end.get("run_id")
    last_rows = runs.get(last_id, [])
    last_ts = parse_ts(last_end.get("ts")) or now
    warns = []
    if any(r.get("event") == "error" and r.get("stage") == "crash" for r in last_rows):
        warns.append("最新の回が例外で落ちた")
    if any(r.get("event") == "error" and r.get("error") == "auth" for r in last_rows):
        warns.append("agy の認証切れ (K8 で agy を起動して再ログインが要る)")
    if last_end.get("quota_stop"):
        warns.append("最新の回が agy の枠切れで止まった")
    real_days = sorted({r.get("jst_date") for r in rows
                        if r.get("event") == "run_start" and not r.get("dry_run")})
    today = jst_day(now)
    yesterday = jst_day(now - dt.timedelta(days=1))
    if real_days and real_days[0] <= jst_day(now - dt.timedelta(days=2)):
        recent_prs = [r for r in rows if r.get("event") == "pr" and r.get("jst_date") in (today, yesterday)]
        if not recent_prs:
            warns.append("直近 2 日 (今日と昨日) の記事 PR が 0 本")
    head = f"## {'⚠ ' if warns else ''}記事レーン（k8-writer）{last_ts.astimezone(JST).strftime('%Y-%m-%d %H:%M')} の回"
    lines = [head, ""]
    if warns:
        lines += ["**" + "・".join(warns) + "**", ""]
    start = next((r for r in last_rows if r.get("event") == "run_start"), {})
    fails = [r for r in last_rows if r.get("event") == "failed"]
    reason_count = {}
    for r in fails:
        for n in r.get("reasons") or []:
            reason_count[n] = reason_count.get(n, 0) + 1
    gate = last_end.get("gate")
    lines += [
        f"- 種類: {'試運転 (PR を作らない)' if start.get('dry_run') else '本番'}・書き手 {start.get('backend')} / {start.get('model')}",
        f"- 候補 {last_end.get('candidates', 0)}・出典を確かめた {last_end.get('prechecked', 0)}・"
        f"出典不足で外した {last_end.get('skip_sources', 0)}・ロック中で飛ばした {last_end.get('lock_busy', 0)}",
        f"- 書き手の呼び出し {last_end.get('calls', 0)} 回・合格 {last_end.get('passed', 0)}・PR {last_end.get('prs', 0)}・"
        f"不合格 {last_end.get('failed', 0)}・エラー {last_end.get('errors', 0)}・枠切れ {'あり' if last_end.get('quota_stop') else 'なし'}",
        f"- 所要 {round((last_end.get('secs') or 0) / 60, 1)} 分（書き手の時間の合計）・トークン {round((last_end.get('tokens') or 0) / 10000, 1)} 万",
    ]
    if gate:
        lines.append(f"- 書かずに終えた理由: {gate}")
    if reason_count:
        lines.append("- 不合格の理由: " + "、".join(f"{k} {v}" for k, v in sorted(reason_count.items(), key=lambda x: -x[1])))
    problems = [r for r in last_rows if r.get("event") in ("failed", "error")][:10]
    if problems:
        lines += ["", "不合格・エラーの記事:"]
        for r in problems:
            what = ", ".join(r.get("reasons") or []) if r.get("event") == "failed" else f"{r.get('stage')}: {r.get('error')}"
            lines.append(f"- {r.get('asin') or '-'}: {what}")
    lines += ["", f"直近 {days} 日:", "",
              "| 日付 | 回 | 書いた | 合格 | PR | マージ | 閉じた | 出典不足 | 不合格の主な理由 | 枠切れ | 1 本の平均所要(分) | トークン(万) |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for i in range(days - 1, -1, -1):
        day = jst_day(now - dt.timedelta(days=i))
        drows = [r for r in rows if r.get("jst_date") == day]
        if not drows:
            lines.append(f"| {day} | 0 | - | - | - | - | - | - | - | - | - | - |")
            continue
        n_runs = sum(1 for r in drows if r.get("event") == "run_start")
        writes = [r for r in drows if r.get("event") == "write"]
        passed = sum(1 for r in drows if r.get("event") == "pass") + sum(1 for r in drows if r.get("event") == "pr")
        prs = [r for r in drows if r.get("event") == "pr"]
        if pr_states is None:
            merged = closed = "?"
        else:
            merged = sum(1 for r in prs if pr_states.get(r.get("url")) == "MERGED")
            closed = sum(1 for r in prs if pr_states.get(r.get("url")) == "CLOSED")
        skips = sum(1 for r in drows if r.get("event") == "skip_sources")
        rc = {}
        for r in drows:
            if r.get("event") == "failed":
                for n in r.get("reasons") or []:
                    rc[n] = rc.get(n, 0) + 1
        top = "、".join(k for k, _ in sorted(rc.items(), key=lambda x: -x[1])[:2]) or "-"
        quota = sum(1 for r in drows if r.get("event") == "quota")
        articles = {r.get("asin") for r in writes}
        secs = sum(float(r.get("secs") or 0) for r in writes)
        avg = round(secs / 60 / len(articles), 1) if articles else "-"
        toks = round(sum(int(r.get("tokens") or 0) for r in writes) / 10000, 1)
        lines.append(f"| {day} | {n_runs} | {len(articles)} | {passed} | {len(prs)} | {merged} | {closed} | "
                     f"{skips} | {top} | {quota} | {avg} | {toks} |")
    if pr_states is None:
        lines += ["", "（PR のその後は gh で取得できなかった）"]
    lines += ["", "台帳: K8 の C:\\Users\\AIsys\\k8jobs\\navi-writer\\ledger.jsonl ・ navi-brain#170"]
    return "\n".join(lines) + "\n"


def cmd_report(args, env):
    rows = read_ledger(args.state_dir)
    data = env.gh_json(["pr", "list", "-R", args.repo_slug, "--state", "all",
                        "--search", "\"記事生成\" k8-writer in:title", "--limit", "100",
                        "--json", "number,url,state,createdAt,title"])
    prefix = PR_TITLE.split("{", 1)[0]
    pr_states = ({r.get("url"): r.get("state") for r in data if str(r.get("title") or "").startswith(prefix)}
                 if isinstance(data, list) else None)
    text = build_report(rows, pr_states, env.now(), days=args.days)
    if not args.post:
        sys.stdout.write(text)
        return 0
    f = Path(args.state_dir) / "report.md"
    f.write_text(text, encoding="utf-8")
    r = env.gh(["issue", "comment", str(args.issue), "-R", args.brain_slug, "--body-file", str(f)])
    print((r.stdout or r.stderr or "").strip())
    return 0 if r.returncode == 0 else 1


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="K8 article lane (navi-brain#170)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--state-dir", required=True)
    r.add_argument("--max-articles", type=int, default=5)
    r.add_argument("--max-calls", type=int, default=10)
    r.add_argument("--max-tokens", type=int, default=1500000)
    r.add_argument("--max-prechecks", type=int, default=40)
    r.add_argument("--backend", choices=["agy", "ollama"], default="agy")
    r.add_argument("--model", default=DEFAULT_MODEL)
    r.add_argument("--timeout", type=int, default=900)
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--asin", nargs="*", default=None)
    r.add_argument("--repo-slug", default="omochairo/amazon")
    r.add_argument("--run-id", default="")
    p = sub.add_parser("report")
    p.add_argument("--state-dir", required=True)
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--brain-slug", default="omochairo/amazon-navi-brain")
    p.add_argument("--repo-slug", default="omochairo/amazon")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--post", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "run":
        if args.asin:
            bad = [a for a in args.asin if not re.fullmatch(r"B0[A-Z0-9]{8}", a)]
            if bad:
                ap.error(f"bad ASIN: {bad}")
        return cmd_run(args, Env(repo_slug=args.repo_slug))
    return cmd_report(args, Env(repo_slug=args.repo_slug))


if __name__ == "__main__":
    sys.exit(main())
