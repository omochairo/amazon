#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""記事生成オーケストレータ (GitLab 移行 フェーズ2, 旧 03-invoke-jules.yml の移植)。

repoless Jules セッションで記事 JSON を生成し、GitLab に MR を作る。
旧フローとの対応:
- pick-asin (候補選定 + jules-lock 排他)      → GitHub API を GitLab API に置換
- Create Jules Sessions (AUTO_CREATE_PR)      → repoless 作成 + 完了 poll + patch 取得
- PR 作成は Jules 任せ                         → 本スクリプトが検証後に MR を作成
- 検証は 04 (PR パイプライン) 任せ             → フェーズ3 まではここでインライン実行
  (schema + quality_gate 不合格の成果物は MR を作らず捨てる)

env:
  JULES_API_KEY      (必須)
  GITLAB_API_TOKEN   (必須, Maintainer の project access token)
  CI_PROJECT_ID      (既定 84175362)
  INPUT_ASIN         (任意, 強制 ASIN。quota gate をバイパスし 1 件のみ)
  INVOKE_MAX         (任意, 1 run の生成上限。既定は quota gate 予算, 上限 6)
  CI_PIPELINE_ID     (shuffle の seed。ローカル実行時は未設定で可)
"""
import glob
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_jules_prompt import build_prompt  # noqa: E402

JST = timezone(timedelta(hours=9))
ASIN_RE = re.compile(r"^B0[A-Z0-9]{8}$")
JULES_API = "https://jules.googleapis.com/v1alpha"
POLL_INTERVAL_SEC = 30
POLL_TIMEOUT_SEC = 45 * 60  # 全セッション合計の待ち上限
TERMINAL_OK = {"COMPLETED"}
TERMINAL_NG = {"FAILED", "AWAITING_USER_FEEDBACK", "AWAITING_PLAN_APPROVAL", "PAUSED"}


def _http(url, token_header, data=None, method=None, timeout=60):
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json", **token_header},
        method=method or ("POST" if data is not None else "GET"),
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode()
        return json.loads(body) if body else {}


class GitLab:
    def __init__(self):
        self.token = os.environ["GITLAB_API_TOKEN"]
        pid = os.environ.get("CI_PROJECT_ID", "84175362")
        self.base = f"https://gitlab.com/api/v4/projects/{pid}"

    def _hdr(self):
        return {"PRIVATE-TOKEN": self.token}

    def call(self, path, data=None, method=None):
        return _http(self.base + path, self._hdr(), data, method)

    def open_mr_titles(self):
        mrs = self.call("/merge_requests?state=opened&per_page=100")
        return [m.get("title", "") + " " + m.get("source_branch", "") for m in mrs]

    def lock_branches(self):
        bs = self.call("/repository/branches?regex=" +
                       urllib.parse.quote("^jules-lock/") + "&per_page=100")
        return [b["name"].split("/", 1)[1] for b in bs if b["name"].startswith("jules-lock/")]

    def try_lock(self, asin):
        """jules-lock/<ASIN> ブランチ作成。既存なら False (atomic 排他, 設計 H12)。"""
        try:
            self.call("/repository/branches?branch=" +
                      urllib.parse.quote(f"jules-lock/{asin}", safe="") + "&ref=main", data={})
            return True
        except urllib.error.HTTPError as e:
            if e.code == 400:
                return False
            raise

    def release_lock(self, asin):
        try:
            self.call("/repository/branches/" +
                      urllib.parse.quote(f"jules-lock/{asin}", safe=""), method="DELETE")
        except urllib.error.HTTPError as e:
            print(f"warning: lock release failed for {asin}: HTTP {e.code}", file=sys.stderr)

    def cleanup_stale_locks(self, ttl_hours=1):
        """旧 06-jules-lock-cleanup.yml の移植 (フェーズ3)。run 冒頭で TTL 超過 lock を回収。

        独立 schedule にしない理由: project bot はスケジュール変数を追加できず (API 403)、
        schedule 2 本を CI 側で見分けられないため、invoke-jules に相乗りさせる。
        6h 毎 run 冒頭の掃除で TTL 1h は十分守れる (旧 06 は 30 分毎だったが、
        lock は MR 作成/失敗時に即時解放されるようになったので残るのは異常系のみ)。"""
        from datetime import datetime as _dt
        try:
            branches = self.call("/repository/branches?regex=" +
                                 urllib.parse.quote("^jules-lock/") + "&per_page=100")
        except urllib.error.HTTPError as e:
            print(f"warning: lock cleanup list failed: HTTP {e.code}", file=sys.stderr)
            return
        now = _dt.now(timezone.utc)
        for b in branches:
            ts = (b.get("commit") or {}).get("committed_date")
            if not ts:
                continue
            try:
                age = (now - _dt.fromisoformat(ts)).total_seconds()
            except ValueError:
                continue
            if age > ttl_hours * 3600:
                asin = b["name"].split("/", 1)[1]
                print(f"stale lock ({age / 3600:.1f}h > TTL {ttl_hours}h): releasing {b['name']}")
                self.release_lock(asin)

    def enable_auto_merge(self, mr_iid):
        """MWPS (merge when pipeline succeeds) を設定 (旧 05 の gh pr merge --auto 相当)。

        MR 直後は pipeline 未作成で 405/406 が返るため、リトライで待つ。
        validate-article (MR パイプライン) 成功時に squash マージされる。"""
        for attempt in range(12):  # 最大 ~2 分
            try:
                self.call(f"/merge_requests/{mr_iid}/merge",
                          data={"merge_when_pipeline_succeeds": True,
                                "squash": True, "should_remove_source_branch": True},
                          method="PUT")
                return True
            except urllib.error.HTTPError as e:
                # 405/406: pipeline 未作成。422: mergeability チェック中 (実測 MR !7)。
                if e.code in (405, 406, 422):
                    time.sleep(10)
                    continue
                print(f"warning: auto-merge setup failed for !{mr_iid}: HTTP {e.code}",
                      file=sys.stderr)
                return False
        print(f"warning: auto-merge setup timed out for !{mr_iid} (要手動 merge)",
              file=sys.stderr)
        return False

    def create_article_mr(self, asin, today, content, session_url, score_line):
        branch = f"add-article-{asin}"
        try:
            self.call("/repository/branches?branch=" +
                      urllib.parse.quote(branch, safe="") + "&ref=main", data={})
        except urllib.error.HTTPError as e:
            if e.code != 400:
                raise
            branch = f"add-article-{asin}-{os.environ.get('CI_PIPELINE_ID', 'local')}"
            self.call("/repository/branches?branch=" +
                      urllib.parse.quote(branch, safe="") + "&ref=main", data={})
        path = urllib.parse.quote(f"data/articles/{today}-{asin}.json", safe="")
        self.call(f"/repository/files/{path}", data={
            "branch": branch,
            "content": content,
            "commit_message": f"feat: 記事追加 {asin} (repoless Jules)",
        })
        mr = self.call("/merge_requests", data={
            "source_branch": branch,
            "target_branch": "main",
            "title": f"[wf03] 記事生成: {asin}",
            "description": (f"repoless Jules セッションによる自動生成。\n\n"
                            f"- session: {session_url}\n- {score_line}\n\n"
                            f"生成フロー: scripts/invoke_jules_repoless.py "
                            f"(docs/gitlab-migration-design.md §4.4)"),
            "squash": True,
            "remove_source_branch": True,
        })
        return mr["web_url"], mr["iid"]


class Jules:
    def __init__(self):
        self.key = os.environ["JULES_API_KEY"]

    def _hdr(self):
        return {"X-Goog-Api-Key": self.key}

    def create_session(self, prompt, title):
        d = _http(f"{JULES_API}/sessions", self._hdr(),
                  {"prompt": prompt, "title": title})
        return d["id"]

    def get_session(self, sid):
        return _http(f"{JULES_API}/sessions/{sid}", self._hdr())


def quota_budget(max_per_run):
    """jules_quota_gate.py (H11: GitHub 非依存) で rolling-24h 残予算を取得。"""
    try:
        # Jules の sessions 一覧 API は履歴が増えると遅い (実測 4 分超) ため長めに取る
        out = subprocess.run(
            [sys.executable, "scripts/jules_quota_gate.py", "--cap", "80",
             "--max", str(max_per_run)],
            capture_output=True, text=True, timeout=600, check=True,
            env=os.environ.copy())
        return int(out.stdout.strip().splitlines()[-1])
    except Exception as e:
        print(f"warning: quota gate failed ({e}); fail-closed budget=0", file=sys.stderr)
        return 0


def existing_article_asins():
    existing = set()
    for path in glob.glob("data/articles/*.json"):
        base = os.path.basename(path)
        if base.endswith((".enrichment.json", ".quality.json", ".seo.json")):
            continue
        m = re.search(r"(B0[A-Z0-9]{8})", base)
        if m:
            existing.add(m.group(1))
    return existing


def pick_candidates(gl, budget):
    """旧 03 pick-asin の移植: ranking_pool 優先 + keyword pool、既存/オープンMR/lock/info-zero を除外。"""
    raw = json.load(open("data/raw/amazon.json", encoding="utf-8"))
    candidates = [i["asin"] for i in raw.get("items", [])
                  if isinstance(i, dict) and isinstance(i.get("asin"), str)
                  and ASIN_RE.match(i["asin"])]

    existing = existing_article_asins()

    # #2711: rewrite_queue マーカーのある ASIN は再生成対象として復帰
    try:
        import rewrite_queue as rq
        pending = rq.eligible_rewrite_asins("data/articles")
        if pending:
            print(f"rewrite-pending (#2711): {len(pending)}")
        existing -= pending
    except Exception as e:
        print(f"warning: rewrite_queue eligibility skipped: {e}", file=sys.stderr)

    # open MR と lock ブランチの ASIN を除外
    for text in gl.open_mr_titles():
        for m in re.finditer(r"(B0[A-Z0-9]{8})", text.upper()):
            existing.add(m.group(1))
    for asin in gl.lock_branches():
        if ASIN_RE.match(asin):
            existing.add(asin)

    # #810 Phase 1.5: ranking_pool を先頭に (shuffle は pool 内でのみ)
    ranking_pool = []
    try:
        rp = json.load(open("data/raw/ranking_pool.json", encoding="utf-8"))
        ranking_pool = [a for a in rp.get("asins", [])
                        if isinstance(a, str) and ASIN_RE.match(a)]
    except FileNotFoundError:
        pass
    ranking_set = set(ranking_pool)
    remaining_ranking = [a for a in ranking_pool if a not in existing]
    remaining_kw = [a for a in candidates
                    if a not in existing and a not in ranking_set]
    # #5490: リライト待ちを候補として **足す**。マーカーは existing から除外を
    # 外すだけで、候補列そのものは amazon.json の items[] から作られるため、
    # 日次 fetch に prepend を消された対象は永久に選ばれなかった
    # (実測 2026-08-31: マーカー 172 件・消化 0 件・候補プール内 0 件)。
    # 03-invoke-jules.yml の inline pick-asin と同じ規則。
    rewrite_first: list[str] = []
    try:
        import rewrite_queue as _rq2
        cap = int(os.environ.get("REWRITE_PICKS_PER_RUN", "2"))
        pool = [a for a in _rq2.pending_rewrite_candidates("data/articles")
                if a not in existing and a not in ranking_set]
        rewrite_first = pool[:max(cap, 0)]
        if pool:
            print(f"rewrite-ready (#5490): {len(pool)} -> picking {len(rewrite_first)}")
    except Exception as e:
        print(f"warning: rewrite candidate injection skipped: {e}", file=sys.stderr)
    rng = random.Random(os.environ.get("CI_PIPELINE_ID", ""))
    rng.shuffle(remaining_ranking)
    rng.shuffle(remaining_kw)
    remaining = remaining_ranking + rewrite_first + [
        a for a in remaining_kw if a not in rewrite_first
    ]

    # #1600 Phase 1: band=zero (真ゼロ素材) を defer (全滅時は無効化)。
    # repoless 移行で "unfetched" (第三者収集が未実行) も defer 対象に追加:
    # 非販売ソース 2 件必須 (v5 §6.5.1) を満たす素材が存在せず、生成しても
    # 品質ゲートに構造的不合格 (実測: B0GYCJC6DC が 5 session 空費)。
    # フェーズ4 でデータ収集系が復旧しフェッチされれば自然に候補へ戻る。
    try:
        import score_per_asin_info as sc
        deferred_bands = ("zero", "unfetched")
        kept = [a for a in remaining
                if sc.score_asin(a).get("band") not in deferred_bands]
        if kept:
            if len(kept) < len(remaining):
                print(f"info-zero/unfetched deferred (#1600): {len(remaining) - len(kept)}")
            remaining = kept
        elif remaining:
            print("warning: all candidates deferred band; proceeding without defer",
                  file=sys.stderr)
    except Exception as e:
        print(f"warning: info scoring skipped, no defer applied: {e}", file=sys.stderr)

    print(f"candidates={len(candidates)} ranking_first={len(remaining_ranking)} "
          f"excluded={len(existing)} remaining={len(remaining)} budget={budget}")
    return remaining


def validate_article(asin, today, content):
    """schema + quality_gate をインライン実行 (フェーズ3 で MR パイプラインに移す)。"""
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, f"{today}-{asin}.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        r1 = subprocess.run(
            [sys.executable, "scripts/validate_json.py", "--json", p,
             "--schema", "data/schema/article.schema.json"],
            capture_output=True, text=True, timeout=300)
        r2 = subprocess.run(
            [sys.executable, "scripts/quality_gate.py", "--src", tmp + os.sep,
             "--posts", "hugo/content/posts/",
             "--schema", "data/schema/article.schema.json", "--strict"],
            capture_output=True, text=True, timeout=600)
    ok = r1.returncode == 0 and r2.returncode == 0
    tail = (r2.stdout or "").strip().splitlines()
    score_line = next((l.strip() for l in tail if l.strip().startswith(("[OK]", "[NG]"))),
                      "quality_gate: no output")
    if not ok:
        print(f"validation NG for {asin}:\n{r1.stdout}{r1.stderr}\n{r2.stdout[-1500:]}",
              file=sys.stderr)
    return ok, score_line


def extract_article(patch, asin, today):
    """patch から記事 JSON を抽出。追加ファイルが記事 1 点のみであることを強制 (scope guard)。"""
    files = re.findall(r"^diff --git a/(\S+) b/", patch, re.M)
    expect = f"data/articles/{today}-{asin}.json"
    if files != [expect]:
        print(f"scope guard NG for {asin}: patch files={files} expected=[{expect}]",
              file=sys.stderr)
        return None
    body_lines = []
    in_hunk = False
    for line in patch.splitlines():
        if line.startswith("@@"):
            in_hunk = True
            continue
        if in_hunk and line.startswith("+"):
            body_lines.append(line[1:])
    body = "\n".join(body_lines)
    try:
        json.loads(body)
    except Exception as e:
        print(f"patch JSON parse NG for {asin}: {e}", file=sys.stderr)
        return None
    return body


def main():
    gl = GitLab()
    jules = Jules()
    today = datetime.now(JST).strftime("%Y-%m-%d")
    gl.cleanup_stale_locks()  # 旧 06 の TTL 掃除 (run 冒頭に相乗り)

    input_asin = os.environ.get("INPUT_ASIN", "").strip()
    max_per_run = int(os.environ.get("INVOKE_MAX", "6"))

    if input_asin:
        if not ASIN_RE.match(input_asin):
            raise SystemExit(f"INPUT_ASIN '{input_asin}' does not match B0[A-Z0-9]{{8}}")
        targets, budget = [input_asin], 1  # 手動 override は gate バイパス (旧 03 同様)
    else:
        budget = quota_budget(max_per_run)
        if budget <= 0:
            print("quota gate: budget 0; skip this run")
            return 0
        targets = pick_candidates(gl, budget)
        if not targets:
            print("warning: no candidate ASINs available")
            return 0

    # lock 取得 (budget 件まで)
    picked = []
    for asin in targets:
        if len(picked) >= budget:
            break
        if gl.try_lock(asin):
            print(f"lock acquired: jules-lock/{asin}")
            picked.append(asin)
        else:
            print(f"lock contention on {asin}, trying next")
    if not picked:
        print("warning: no locks acquired")
        return 0

    # セッション作成
    sessions = {}  # asin -> session id
    for asin in picked:
        try:
            prompt = build_prompt(asin, today)
            sid = jules.create_session(prompt, f"[wf03] 記事生成: {asin}")
            sessions[asin] = sid
            print(f"session created: {asin} -> {sid}")
        except urllib.error.HTTPError as e:
            err = e.read().decode()[:300]
            print(f"session create NG for {asin}: HTTP {e.code} {err}", file=sys.stderr)
            gl.release_lock(asin)
            # #1353: quota/precondition はグローバル要因なので残りを打ち切る
            if e.code == 429 or "FAILED_PRECONDITION" in err or "RESOURCE_EXHAUSTED" in err:
                print("quota/precondition exhausted — skip remaining ASINs (#1353)",
                      file=sys.stderr)
                for rest in picked[picked.index(asin) + 1:]:
                    gl.release_lock(rest)
                break
        except Exception as e:
            print(f"session create NG for {asin}: {e}", file=sys.stderr)
            gl.release_lock(asin)

    if not sessions:
        print("::error::no Jules sessions were created", file=sys.stderr)
        return 1

    # 完了 poll
    deadline = time.time() + POLL_TIMEOUT_SEC
    states = {a: None for a in sessions}
    while time.time() < deadline:
        pending = [a for a, s in states.items() if s is None]
        if not pending:
            break
        for asin in pending:
            try:
                st = jules.get_session(sessions[asin]).get("state", "?")
            except Exception as e:
                print(f"poll error {asin}: {e}", file=sys.stderr)
                continue
            if st in TERMINAL_OK | TERMINAL_NG:
                states[asin] = st
                print(f"{asin}: {st}")
        if any(s is None for s in states.values()):
            time.sleep(POLL_INTERVAL_SEC)

    # 成果物回収 → 検証 → MR
    made = 0
    for asin, sid in sessions.items():
        state = states.get(asin)
        if state not in TERMINAL_OK:
            print(f"{asin}: state={state or 'TIMEOUT'} — lock 解放して次回に回す",
                  file=sys.stderr)
            gl.release_lock(asin)
            continue
        sess = jules.get_session(sid)
        outs = sess.get("outputs", [])
        patch = (outs[0].get("changeSet", {}).get("gitPatch", {})
                 .get("unidiffPatch", "")) if outs else ""
        if not patch:
            # 実測: activities 1 件だけで成果物ゼロのまま COMPLETED になる「空振り」
            # セッションが存在する (2026-07-07, session 18046904765687026255)。
            # lock を解放して次回 run の候補に戻す。
            print(f"{asin}: COMPLETED but no outputs (空振りセッション) — skip",
                  file=sys.stderr)
            gl.release_lock(asin)
            continue
        body = extract_article(patch, asin, today)
        if body is None:
            gl.release_lock(asin)
            continue
        ok, score_line = validate_article(asin, today, body)
        if not ok:
            gl.release_lock(asin)
            continue
        url, iid = gl.create_article_mr(asin, today, body,
                                        f"https://jules.google.com/session/{sid}", score_line)
        print(f"MR created: {asin} -> {url} ({score_line})")
        if gl.enable_auto_merge(iid):
            print(f"auto-merge (MWPS) enabled on !{iid}")
        gl.release_lock(asin)
        made += 1

    print(f"done: sessions={len(sessions)} MRs={made}")
    return 0 if made > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
