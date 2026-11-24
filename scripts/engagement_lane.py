#!/usr/bin/env python3
"""K8 のエンゲージ投稿レーン (amazon-navi-brain#170 B3): Jules の代わりに agy / ollama で下書きを書き、PR にする。

下書きを作っていた 29-jules-engagement-refill.yml / 33-jules-engagement-news.yml は Jules の終了で
止まり、navi-brain#139 で削除した (29b の補充見張りも)。このスクリプトは K8 の定時タスク (\\k8\\navi_engagement) から 1 時間おきに呼ばれる。

    news : 今日 (JST) のトレンドが origin/main にあり、今日作成の news 行が 2 本未満のチャネルだけ生成
    daily: 未投稿の daily が 6 本未満のチャネルだけ 10 本生成
     → 検査 (31 は形しか見ない。禁止事項・文字数・重複・出典はここで見る)
     → 落ちた行は理由を付けて 1 回だけ書き直し → それでも落ちた行は捨てる
     → data/engagement_queue_{x,threads}.jsonl への末尾追記だけの PR
     → 31 が自動マージ → 30 が配信 (下流は変えない。このスクリプトは merge しない)

記事レーン (writer_lane.py) の Env / Ledger / 枠切れの扱い / git の部品を流用する。作業ツリーは
記事レーンと別 (--repo-dir): fresh_branch / back_to_main は作業ツリーを強制的に切り替えるので、
同じ作業ツリーで動かすと互いに壊す。

プロンプト (private の navi-brain) と書き手の出力・下書きの本文は --state-dir にだけ置く。
PR・標準出力・例外メッセージ・台帳には本文を出さない (件数と id だけ)。

使い方 (cwd は何処でもよい):
    python scripts/engagement_lane.py run --repo-dir C:\\k8jobs\\amazon-engagement \\
        --brain-dir C:\\k8jobs\\navi-brain-writer --state-dir C:\\k8jobs\\navi-engagement [--dry-run]

K8 の Python は 3.11 だが、runner (3.8) と同じく 3.8 で import できる書き方にしている。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import writer_lane as wl  # noqa: E402
import _engagement_recent_topics as ert  # noqa: E402
from _x_text import weighted_len  # noqa: E402

JST = wl.JST
BRANCH_PREFIX = "k8-engagement/"
PR_TITLE = "[k8-engagement] {kind} ({label}) {date}"
LOCK_STALE_S = 2 * 3600
PR_MAX_AGE_S = 24 * 3600
X_WEIGHT_LIMIT = 280
POST_CHAR_LIMIT = 300
NEWS_TARGET = 2            # チャネルごとの 1 日の news 行
DAILY_LOW_WATER = 6        # 未投稿 daily がこれ未満なら補充
DAILY_BATCH = 10
DAILY_MIN_EMIT = 4         # これ未満なら出さない
NEWS_MAX_PER_CHANNEL = 4
ROUNDS_PER_DAY = 2         # news / daily それぞれ、チャネルごとの 1 日の生成回数
RECENT_ROWS = 60           # 本文の重複を見る直近行数 (チャネルごと)
RECENT_PROMPT_ROWS = 30    # プロンプトに貼る直近行数
SOURCE_DUP_HOURS = 72
TOPIC_DUP_HOURS = 48
JACCARD_LIMIT = 0.6
EMOJI_LIMIT = 3            # この個数以上で不合格 (プロンプトの上限は X 2 / Threads 2〜3)
TREND_PROMPT_MAX = 30
DEFAULT_NUM_CTX = 16384    # news のプロンプト (元 約 12KB + 入力) は 8192 では収まらない [推]
OLLAMA_DEFAULT_MODEL = "gemma4:26b-a4b-it-qat"

CHANNELS = {
    "x": {"label": "X", "persona": "iromama", "file": "data/engagement_queue_x.jsonl",
          "len_key": "weighted_chars", "prefix": {"daily": "eng-x", "news": "news-x"}},
    "threads": {"label": "Threads", "persona": "iropapa", "file": "data/engagement_queue_threads.jsonl",
                "len_key": "char_count", "prefix": {"daily": "eng-th", "news": "news-th"}},
}
PROMPT_FILES = {
    ("news", "x"): "PROMPT_ENGAGEMENT_NEWS_DUAL.md",
    ("news", "threads"): "PROMPT_ENGAGEMENT_NEWS_DUAL.md",
    ("daily", "x"): "PROMPT_ENGAGEMENT_X_IROMAMA_DAILY.md",
    ("daily", "threads"): "PROMPT_ENGAGEMENT_THREADS_IROPAPA_DAILY.md",
}
TREND_REL = "data/engagement_trends/{date}.json"

# 書き手に渡す「ファイルを読ませる文」の続き (article_writer.write の instruction)
WRITER_INSTRUCTION = (
    "write the social media drafts it asks for. You cannot create files, run commands or use the web. "
    "Output exactly one ```json code block containing a single JSON object of the form "
    '{"posts": [...]} and nothing else (no preface, no explanation). '
    "Use only the material in that file and never invent URLs."
)

# ---------------------------------------------------------------- 検査 (31 が見ないもの)

URL_RE = re.compile(
    r"https?://|www\.|"
    r"\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.(?:com|net|org|jp|io|me|info|biz|dev|app|tv|xyz|site|shop|blog|"
    r"co|ne|or|ac|go|edu|gov)\b",
    re.I)
HASHTAG_RE = re.compile(r"[#＃]")
SELF_INTRO_RE = re.compile(r"いろ(?:ママ|パパ)(?:です|より|と申します|だよ|でした)")
SIGNATURE_RE = re.compile(r"(?:^|\n)[ \t\u3000]*[—―–－\-]{1,3}[ \t\u3000]*[^\n]{0,12}より[ \t\u3000]*$"
                          r"|(?:ママ|パパ)より[ \t\u3000]*$")
# 絵文字 (1F000 台の全部 + BMP の主な絵文字。♡♪★ などの記号は数えない。肌色の修飾子は数えない)
EMOJI_RE = re.compile(
    "[\U0001F000-\U0001F3FA\U0001F400-\U0001FAFF"
    "\u2600-\u2604\u260e\u2614\u2615\u2639\u263a\u2648-\u2653\u26a0\u26a1\u26bd\u26be\u26c4\u26c5"
    "\u2702\u2705\u2708-\u270d\u2728\u2733\u2734\u2744\u274c\u2753-\u2755\u2757\u2763\u2764\u27a1"
    "\u2b50\u2b55]")

# 落ちたら書き直しではなく、その行を捨てるもの (作り話のニュースを止める)
DROP_REASONS = frozenset(["source_not_in_trend"])


def count_emoji(text):
    return len(EMOJI_RE.findall(text or ""))


def shingles(text):
    s = re.sub(r"\s+", "", text or "")
    if len(s) < 3:
        return {s} if s else set()
    return {s[i:i + 3] for i in range(len(s) - 2)}


def jaccard(a, b):
    sa, sb = shingles(a), shingles(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / float(len(sa | sb))


def _url_keys(url):
    n = wl.normalize_url(str(url or ""))
    return n, n.split("?", 1)[0]


def match_candidate(url, candidates):
    """source_url に当たるトレンド候補。クエリ (?source=rss) の有無は問わない。"""
    n, nq = _url_keys(url)
    for c in candidates:
        cn, cq = _url_keys(c.get("link"))
        if n == cn or nq == cq:
            return c
    return None


def individual_checks(draft, kind, channels, candidates):
    """1 行ずつの検査。戻り値は [(理由名, 文)]。空なら合格。"""
    fails = []
    if not isinstance(draft, dict):
        return [("format", "posts の要素が JSON オブジェクトでない")]
    text = draft.get("text")
    sub = draft.get("subcategory")
    if not isinstance(text, str) or not text.strip():
        fails.append(("missing", "text が空 (空白だけ) 。"))
    if not isinstance(sub, str) or not sub.strip():
        fails.append(("missing", "subcategory が空"))
    ch = draft.get("channel") if kind == "news" else channels[0]
    if not isinstance(ch, str) or ch not in channels:
        fails.append(("channel", "channel は " + " / ".join(channels) + " のどれかにする"))
    if kind == "news":
        url = draft.get("source_url")
        if not isinstance(url, str) or not url.strip():
            fails.append(("missing", "source_url が空 (top_candidates の link をそのまま入れる)"))
        elif match_candidate(url, candidates) is None:
            fails.append(("source_not_in_trend", "source_url が今日の top_candidates に無い"))
    if not isinstance(text, str) or not text.strip() or ch not in CHANNELS:
        return fails
    t = text.strip()
    n = len(t)
    if ch == "x":
        w = weighted_len(t)
        if w > X_WEIGHT_LIMIT or n > POST_CHAR_LIMIT:
            fails.append(("length", "X は weighted %d 以内かつ %d 字以内 (今 weighted %d / %d 字)"
                          % (X_WEIGHT_LIMIT, POST_CHAR_LIMIT, w, n)))
    elif n > POST_CHAR_LIMIT:
        fails.append(("length", "Threads は %d 字以内 (今 %d 字)" % (POST_CHAR_LIMIT, n)))
    if URL_RE.search(t):
        fails.append(("url", "URL・ドメインを本文に含めない"))
    if HASHTAG_RE.search(t):
        fails.append(("hashtag", "ハッシュタグ (# / ＃) を含めない"))
    if count_emoji(t) >= EMOJI_LIMIT:
        fails.append(("emoji", "絵文字は %d 個未満にする (今 %d 個)" % (EMOJI_LIMIT, count_emoji(t))))
    if SELF_INTRO_RE.search(t):
        fails.append(("self_intro", "「いろママです」のような名乗りを書かない"))
    if SIGNATURE_RE.search(t):
        fails.append(("signature", "末尾に「— ママより」のような署名を付けない"))
    return fails


def dedup_checks(draft, kind, ctx, taken):
    """重複・話題の検査。taken = 同じ回で先に合格した行 (draft の dict)。"""
    fails = []
    ch = draft["channel"]
    text = draft["text"]
    if kind == "news":
        n, nq = _url_keys(draft["source_url"])
        if n in ctx["recent_urls"] or nq in ctx["recent_urls_q"] or any(
                _url_keys(t["source_url"])[1] == nq for t in taken if t.get("source_url")):
            fails.append(("dup_source", "同じ出典 (source_url) を直近 %d 時間か同じ回で使っている" % SOURCE_DUP_HOURS))
        sub = draft["subcategory"].strip()
        if sub in ctx["recent_topics"]:
            fails.append(("dup_topic", "話題 (subcategory) が直近 %d 時間と同じ: %s" % (TOPIC_DUP_HOURS, sub)))
        if any(t["channel"] != ch and t["subcategory"].strip() == sub for t in taken):
            fails.append(("dup_topic_cross", "X と Threads で同じ話題 (subcategory) を使わない: " + sub))
    pool = list(ctx["recent_texts"].get(ch, [])[-RECENT_ROWS:]) + [t["text"] for t in taken if t["channel"] == ch]
    for other in pool:
        if jaccard(text, other) >= JACCARD_LIMIT:
            fails.append(("dup_text", "既存の下書きとほぼ同じ本文 (3 文字単位の Jaccard %.1f 以上)" % JACCARD_LIMIT))
            break
    return fails


def normalize_posts(raw, kind, channels):
    """書き手の出力 (dict) から posts のリストを取る。取れなければ None。"""
    posts = raw.get("posts") if isinstance(raw, dict) else None
    if not isinstance(posts, list):
        return None
    out = []
    for p in posts:
        if isinstance(p, dict):
            d = dict(p)
            if isinstance(d.get("text"), str):
                d["text"] = d["text"].replace("\r\n", "\n").strip()
            if kind == "daily":
                d["channel"] = channels[0]
            out.append(d)
        else:
            out.append(p)
    return out


def check_batch(posts, kind, channels, candidates, ctx, taken):
    """posts を検査。戻り値 (合格した draft のリスト, [(番号, [(理由, 文)])], 捨てた行数)。"""
    ok_ind, bad, dropped = [], [], 0
    for i, p in enumerate(posts, 1):
        fails = individual_checks(p, kind, channels, candidates)
        if any(n in DROP_REASONS for n, _ in fails):
            dropped += 1
            continue
        if fails:
            bad.append((i, fails))
        else:
            ok_ind.append((i, p))
    accepted = []
    for i, p in ok_ind:
        d = {"channel": p["channel"], "subcategory": p["subcategory"].strip(), "text": p["text"].strip()}
        if kind == "news":
            cand = match_candidate(p["source_url"], candidates)
            d["source_url"] = cand["link"]
            d["source_title"] = cand.get("title") or str(p.get("source_title") or "")
        fails = dedup_checks(d, kind, ctx, list(taken) + accepted)
        if fails:
            bad.append((i, fails))
        else:
            accepted.append(d)
    bad.sort(key=lambda x: x[0])
    return accepted, bad, dropped


def feedback_text(bad, parse_error=""):
    if parse_error:
        body = "- " + parse_error
    else:
        body = "\n".join("- posts[%d]: %s" % (i, " / ".join("%s: %s" % f for f in fails)) for i, fails in bad)
    head = ("前回の出力は次の自動検査で不合格になった。落ちた行を直し、{\"posts\": [...]} の全体を出し直す。"
            "URL・ハッシュタグ・名乗り・署名は書かない。出典は top_candidates の link だけ使う。\n")
    return (head + body)[:wl.FEEDBACK_MAX]


# ---------------------------------------------------------------- 外の世界

class LaneEnv(wl.Env):
    """作業ツリー (--repo-dir) を cwd にして git / gh を呼ぶ Env。"""

    def __init__(self, repo_dir, **kw):
        super().__init__(**kw)
        self.repo_dir = str(repo_dir)

    def run(self, argv, input_text=None, timeout=600):
        return self._runner(argv, input=input_text, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=timeout, cwd=self.repo_dir)


class Source:
    """main の中身の読み方。live は origin/main (git show)、dry-run は作業ツリーのファイル (git / gh を呼ばない)。"""

    def __init__(self, env, repo_dir, live):
        self.env, self.repo_dir, self.live = env, Path(repo_dir), live

    def read(self, rel, required=False):
        if self.live:
            r = wl.git(self.env, "show", "origin/main:" + rel)
            if r.returncode == 0:
                return r.stdout or ""
            if required:
                raise RuntimeError("git show origin/main:%s failed" % rel)
            return None
        p = self.repo_dir / rel
        try:
            return p.read_text(encoding="utf-8")
        except OSError:
            if required:
                raise RuntimeError("cannot read %s" % rel)
            return None


def parse_rows(text):
    rows = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict):
            rows.append(r)
    return rows


class RunLock:
    """多重起動防止。古いロック (2 時間超) は無効。"""

    def __init__(self, state_dir, env):
        self.path = Path(state_dir) / "engagement.lock"
        self.env = env
        self.held = False

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        now = self.env.now().timestamp()
        for _ in range(2):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                held = wl.read_json(self.path, {}) or {}
                ts = held.get("ts") if isinstance(held, dict) else None
                if isinstance(ts, (int, float)) and now - ts <= LOCK_STALE_S:
                    return False
                try:
                    self.path.unlink()
                except OSError:
                    return False
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps({"ts": now, "pid": os.getpid()}))
            self.held = True
            return True
        return False

    def release(self):
        if self.held:
            try:
                self.path.unlink()
            except OSError:
                pass
            self.held = False


# ---------------------------------------------------------------- 状態 (main + 自分の open PR)

def row_day(row):
    t = wl.parse_ts(row.get("created_at"))
    return wl.jst_day(t) if t else ""


def load_pending(state, name):
    return wl.read_json(Path(state) / "pending" / (name + ".json"), None)


def pending_rows(state, branch):
    """自分の open PR の行 (台帳側に保存した「PR に載せた行」)。{channel: [row...]}"""
    pend = load_pending(state, branch.split("/", 1)[-1])
    if not isinstance(pend, dict):
        return {}
    return {ch: list(rows) for ch, rows in (pend.get("rows") or {}).items() if ch in CHANNELS}


def build_context(main_rows, pr_rows, now, trend):
    """main と open PR を合わせた状態から、判定・検査・プロンプトに使う値を作る。"""
    allrows = {ch: list(main_rows.get(ch, [])) + list(pr_rows.get(ch, [])) for ch in CHANNELS}
    since72 = now - dt.timedelta(hours=SOURCE_DUP_HOURS)
    urls, urls_q = set(), set()
    for ch in CHANNELS:
        for r in allrows[ch]:
            if r.get("category") != "news" or not r.get("source_url"):
                continue
            t = ert.parse_dt(r.get("created_at"))
            if t is not None and t >= since72:
                n, nq = _url_keys(r["source_url"])
                urls.add(n)
                urls_q.add(nq)
    news48 = []
    since48 = now - dt.timedelta(hours=TOPIC_DUP_HOURS)
    for ch in CHANNELS:
        for r in allrows[ch]:
            if r.get("category") == "news":
                t = ert.parse_dt(r.get("created_at")) or ert.parse_dt(r.get("published_at"))
                if t is not None and t >= since48:
                    news48.append(r)
    topics = ert.summarize(news48)
    cands = []
    if isinstance(trend, dict):
        for c in trend.get("top_candidates") or []:
            if isinstance(c, dict) and c.get("link") and c.get("title"):
                cands.append(c)
    return {
        "rows": allrows,
        "recent_urls": urls, "recent_urls_q": urls_q,
        "recent_topics": {t["topic"] for t in topics},
        "recent_topic_summary": topics,
        "recent_texts": {ch: [str(r.get("text") or "") for r in allrows[ch]] for ch in CHANNELS},
        "candidates": cands,
    }


def next_seq(rows_by_channel, prefix, date, assigned):
    """同じ日・同じ接頭辞の最大の続き番号 (main + open PR + この回で割り当て済み)。"""
    pat = re.compile(r"^%s-%s-(\d+)$" % (re.escape(prefix), re.escape(date)))
    best = 0
    for rows in rows_by_channel.values():
        for r in rows:
            m = pat.match(str(r.get("id") or ""))
            if m:
                best = max(best, int(m.group(1)))
    return max(best, assigned.get((prefix, date), 0)) + 1


def generator_name(backend, model):
    if backend == "agy":
        return "k8_agy_v1"
    return "k8_gemma_v1" if "gemma" in (model or "").lower() else "k8_ollama_v1"


def assemble_rows(drafts, kind, ctx, now, today, args, clock):
    """合格した draft をキュー行にする。キー順は既存行に合わせる。clock は同じ回で共有する秒のずらし。"""
    gen = generator_name(args.backend, args.model)
    assigned = ctx.setdefault("assigned", {})
    base = now.astimezone(JST).replace(microsecond=0)
    out = {}
    for d in drafts:
        ch = d["channel"]
        meta = CHANNELS[ch]
        prefix = meta["prefix"][kind]
        seq = next_seq(ctx["rows"], prefix, today, assigned)
        assigned[(prefix, today)] = seq
        created = (base + dt.timedelta(seconds=clock["n"])).isoformat()
        clock["n"] += 1
        text = d["text"]
        row = {"id": "%s-%s-%03d" % (prefix, today, seq), "channel": ch, "persona": meta["persona"],
               "category": kind, "subcategory": d["subcategory"], "text": text}
        row[meta["len_key"]] = weighted_len(text) if ch == "x" else len(text)
        row["generator"] = gen
        if kind == "news":
            row["source_title"] = d["source_title"]
            row["source_url"] = d["source_url"]
        row.update({"created_at": created, "earliest_publish_at": None, "published_at": None, "post_id": None})
        out.setdefault(ch, []).append(row)
    return out


# ---------------------------------------------------------------- プロンプト

def _flat(text, n=200):
    return re.sub(r"\s+", " ", str(text or "")).strip()[:n]


def build_prompt(original, kind, channels, need, today, ctx):
    """元のプロンプトの前に K8 用の前置き (入力を貼る) を付ける。プロンプト本文は書き換えない。"""
    lines = [
        "# K8 の自動レーンからの実行 (この節は、後ろに続く元のプロンプトより優先する)",
        "",
        "- あなたは自動レーンの書き手。後ろに続く元のプロンプトの仕様 (ペルソナ・題材・文体・絶対ルール) に従って下書きを書く。",
        "- 元のプロンプトにある「入力ファイルを読む」「スクリプトを実行する」「queue に追記する」「PR を作る」は、ここでは行わない。"
        "ファイル・コマンド・Web は使えない。必要な入力は下に貼ってある。",
        "- 出力は ```json のコードブロック 1 個だけ (前置き・説明なし)。中身は {\"posts\": [ ... ]}。",
        "- id・created_at・generator・persona・category・weighted_chars・char_count・earliest_publish_at・published_at・post_id は書かない"
        " (スクリプトが付ける)。",
    ]
    if kind == "news":
        lines += [
            "- posts の各要素のキーは channel (\"x\" か \"threads\")・subcategory・text・source_title・source_url の 5 つだけ。",
            "- source_url は下の top_candidates の link をそのまま (クエリも含めて) 入れる。top_candidates に無い題材は使わない。",
            "- 今日 (JST) は %s。書くチャネルと本数: %s" % (today, "、".join(
                "%s (%s) %d〜%d 本" % (c, CHANNELS[c]["persona"], need[c][0], need[c][1]) for c in channels)),
            "- 良い題材が無いチャネルは、そのチャネルの posts を 0 本にしてよい。",
        ]
    else:
        ch = channels[0]
        lines += [
            "- posts の各要素のキーは channel (\"%s\")・subcategory・text の 3 つだけ。" % ch,
            "- 今日 (JST) は %s。%s (%s) の daily を %d 本書く (元のプロンプトの 7〜10 本の指定より、ここの本数を優先)。"
            % (today, CHANNELS[ch]["label"], CHANNELS[ch]["persona"], DAILY_BATCH),
        ]
    lines += ["", "## 入力 (元のプロンプトの §1 / §6 で読む指定のものを、ここに貼った)", ""]
    if kind == "news":
        lines.append("### top_candidates (今日のトレンド。スコア降順)")
        for i, c in enumerate(ctx["candidates"][:TREND_PROMPT_MAX], 1):
            lines.append("%d. title: %s" % (i, _flat(c.get("title"), 160)))
            lines.append("   link: %s" % c.get("link"))
            if c.get("summary"):
                lines.append("   summary: %s" % _flat(c.get("summary"), 120))
        lines += ["", "### 直近 48 時間の news の話題 (recent_news_topics。同じ角度は禁止)"]
        lines.append(json.dumps(ctx["recent_topic_summary"], ensure_ascii=False) if ctx["recent_topic_summary"] else "(なし)")
        lines.append("")
    for ch in channels:
        recent = ctx["recent_texts"].get(ch, [])[-RECENT_PROMPT_ROWS:]
        lines.append("### %s の直近 %d 行の本文 (モチーフ・言い回しを繰り返さない)" % (CHANNELS[ch]["label"], RECENT_PROMPT_ROWS))
        lines += ["- " + _flat(t, 140) for t in recent] or ["(なし)"]
        lines.append("")
    return "\n".join(lines) + "\n\n---\n\n# 元のプロンプト\n\n" + original


# ---------------------------------------------------------------- 回の判定

def rounds_today(rows, today, dry_run, kind, ch):
    return sum(1 for r in rows if r.get("event") == "round" and r.get("jst_date") == today
               and bool(r.get("dry_run")) == bool(dry_run) and r.get("kind") == kind and ch in (r.get("channels") or []))


def plan_news(ctx, trend_ok, ledger_rows, today, dry_run):
    if not trend_ok:
        return [], "no trend file for today"
    if not ctx["candidates"]:
        return [], "trend has no candidates"
    chans, reasons = [], []
    for ch in CHANNELS:
        have = sum(1 for r in ctx["rows"][ch] if r.get("category") == "news" and row_day(r) == today)
        if have >= NEWS_TARGET:
            reasons.append("%s enough (%d)" % (ch, have))
        elif rounds_today(ledger_rows, today, dry_run, "news", ch) >= ROUNDS_PER_DAY:
            reasons.append("%s rounds exhausted" % ch)
        else:
            chans.append(ch)
    return chans, "; ".join(reasons)


def plan_daily(ctx, ledger_rows, today, dry_run):
    chans, reasons = [], []
    for ch in CHANNELS:
        pending = sum(1 for r in ctx["rows"][ch] if r.get("category") == "daily" and not r.get("published_at"))
        if pending >= DAILY_LOW_WATER:
            reasons.append("%s enough (%d)" % (ch, pending))
        elif rounds_today(ledger_rows, today, dry_run, "daily", ch) >= ROUNDS_PER_DAY:
            reasons.append("%s rounds exhausted" % ch)
        else:
            chans.append(ch)
    return chans, "; ".join(reasons)


# ---------------------------------------------------------------- 書き手 1 回分

def save(dirpath, name, text):
    wl.save(dirpath, name, text)


def write_once(env, ledger, prompt, kind, attempt, args, state_dir, previous=None, feedback=None):
    wr = env.write(prompt, workdir=str(Path(state_dir) / "ws"), backend=args.backend, model=args.model,
                   timeout_s=args.timeout, previous_text=previous, feedback=feedback,
                   instruction=WRITER_INSTRUCTION, num_ctx=args.num_ctx)
    ledger.add("write", kind=kind, attempt=attempt, ok=wr.ok, error=wr.error,
               tokens=wl.tokens_of(wr.usage), secs=round(wr.secs, 1), web_calls=wr.web_calls,
               model=wr.model, conversation_id=wr.conversation_id)
    return wr


def run_unit(env, ledger, args, state, run_dir, unit, ctx, today, caps, stats):
    """1 つの生成単位 (news の 1 回 / daily の 1 チャネル) を書いて検査する。

    戻り値 (rows を作る前の合格 draft のリスト, "stop" か "")。"stop" ならその回をやめる。
    """
    kind, channels = unit["kind"], unit["channels"]
    udir = run_dir / unit["name"]
    prompt_file = Path(args.brain_dir) / PROMPT_FILES[(kind, channels[0])]
    try:
        original = prompt_file.read_text(encoding="utf-8")
    except OSError:
        stats["errors"] += 1
        ledger.add("error", stage="prompt", error="prompt_missing", kind=kind, detail=prompt_file.name)
        return [], ""
    prompt = build_prompt(original, kind, channels, unit["need"], today, ctx)
    save(udir, "prompt.txt", prompt)
    ledger.add("round", kind=kind, channels=channels, dry_run=args.dry_run)
    accepted, previous, feedback = [], None, None
    for attempt in (1, 2):
        if caps["calls"] + stats["calls"] >= args.max_calls or caps["tokens"] + stats["tokens"] >= args.max_tokens:
            ledger.add("gate", reason="daily cap reached during run")
            stats["gate"] = stats["gate"] or "daily cap reached during run"
            return accepted, "stop" if attempt == 1 else ""
        stats["calls"] += 1
        wr = write_once(env, ledger, prompt, kind, attempt, args, state, previous, feedback)
        stats["tokens"] += wl.tokens_of(wr.usage)
        stats["secs"] += wr.secs
        save(udir, "response-%d.txt" % attempt, wr.text or "")
        if wr.error == "quota":
            until = wl.mark_quota(state, env.now(), wr.reset_s)
            stats["quota_stop"] = True
            ledger.add("quota", kind=kind, reset_s=wr.reset_s,
                       until=wl.iso(dt.datetime.fromtimestamp(until, dt.timezone.utc)))
            return accepted, "stop"
        if wr.web_calls:
            stats["web_discard"] += 1
            ledger.add("discard", kind=kind, reason="web_calls", web_calls=wr.web_calls)
            return [], ""
        parse_error = ""
        bad, dropped = [], 0
        if wr.error in ("empty", "parse"):
            parse_error = "JSON として読めなかった。```json のコードブロック 1 個だけを出す"
        elif wr.error:
            stats["errors"] += 1
            ledger.add("error", stage="write", error=wr.error, kind=kind, detail=(wr.error_text or "")[-300:])
            return accepted, "stop" if wr.error == "auth" else ""
        else:
            posts = normalize_posts(wr.article, kind, channels)
            if posts is None:
                parse_error = "出力に posts (配列) が無い。{\"posts\": [...]} の形で出す"
            else:
                got, bad, dropped = check_batch(posts, kind, channels, ctx["candidates"], ctx, accepted)
                accepted = accepted + got
                reasons = {}
                for _, fails in bad:
                    for n, _m in fails:
                        reasons[n] = reasons.get(n, 0) + 1
                ledger.add("check", kind=kind, attempt=attempt, posts=len(posts), passed=len(got),
                           failed=len(bad), dropped=dropped, reasons=reasons)
                stats["passed"] += len(got)
                stats["failed"] += len(bad)
                stats["dropped"] += dropped
        if parse_error:
            ledger.add("check", kind=kind, attempt=attempt, posts=0, passed=0, failed=0, dropped=0,
                       reasons={wr.error or "format": 1})
        if not parse_error and not bad:
            break
        if attempt == 1:
            previous = wr.text or ""
            feedback = feedback_text(bad, parse_error)
            save(udir, "feedback.txt", feedback)
    return accepted, ""


def trim(drafts, kind, unit):
    out, counts = [], {}
    limit = {c: unit["need"][c][1] for c in unit["channels"]}
    for d in drafts:
        ch = d["channel"]
        counts[ch] = counts.get(ch, 0) + 1
        if counts[ch] <= limit[ch]:
            out.append(d)
    return out


# ---------------------------------------------------------------- PR

def append_rows(repo_dir, rows_by_channel):
    """2 つの jsonl への末尾追記だけ。既存行は触らない。"""
    changed = []
    for ch, rows in rows_by_channel.items():
        if not rows:
            continue
        rel = CHANNELS[ch]["file"]
        path = Path(repo_dir) / rel
        prefix = ""
        if path.exists() and path.stat().st_size:
            with open(path, "rb") as f:
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":
                    prefix = "\n"
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(prefix + "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
        changed.append(rel)
    return changed


def push_branch(env, repo_dir, branch, rows_by_channel, force):
    """origin/main から branch を切り直し、行を追記して push する。force なら --force-with-lease。"""
    paths = []
    try:
        wl.fresh_branch(env, branch)
        paths = append_rows(repo_dir, rows_by_channel)
        n = sum(len(v) for v in rows_by_channel.values())
        for args in (("add", "--") + tuple(paths),
                     ("commit", "-m", "data(engagement): k8 下書きを %d 本追加 (%s)" % (n, branch.split("/", 1)[-1]))):
            r = wl.git(env, *args)
            if r.returncode != 0:
                raise RuntimeError("git %s failed: %s" % (args[0], (r.stderr or r.stdout or "").strip()[:300]))
        if force:
            wl.git(env, "fetch", "origin", branch)
            push = ("push", "--force-with-lease", "-u", "origin", branch)
        else:
            push = ("push", "-u", "origin", branch)
        r = wl.git(env, *push)
        if r.returncode != 0:
            raise RuntimeError("git push failed: %s" % (r.stderr or r.stdout or "").strip()[:300])
    finally:
        wl.back_to_main(env, branch, paths)


def pr_body(kind, rows_by_channel):
    lines = ["K8 のエンゲージ下書きレーン (k8-engagement) が追加した行です。amazon-navi-brain#170 B3。", ""]
    for ch, rows in rows_by_channel.items():
        if rows:
            lines.append("- %s (%s %s): %d 本 (%s)" % (CHANNELS[ch]["label"], kind, CHANNELS[ch]["persona"], len(rows),
                                                      ", ".join(r["id"] for r in rows)))
    lines += ["", "変更は data/engagement_queue_{x,threads}.jsonl の末尾追記だけ。自動マージは 31 が行う。"]
    return "\n".join(lines) + "\n"


def create_pr(env, state, repo_dir, kind, today, rows_by_channel):
    branch = "%s%s-%s" % (BRANCH_PREFIX, kind, today)
    label = "+".join(CHANNELS[c]["label"] for c in CHANNELS if rows_by_channel.get(c))
    # 衝突時の作り直し用に、載せた行を台帳側に保存する (PR の本文には書かない)
    pend = Path(state) / "pending" / ("%s-%s.json" % (kind, today))
    pend.parent.mkdir(parents=True, exist_ok=True)
    pend.write_text(json.dumps({"kind": kind, "date": today, "branch": branch, "rows": rows_by_channel},
                               ensure_ascii=False), encoding="utf-8")
    push_branch(env, repo_dir, branch, rows_by_channel, force=False)
    body = Path(state) / "pr_body.md"
    body.write_text(pr_body(kind, rows_by_channel), encoding="utf-8")
    r = env.gh(["pr", "create", "-R", env.repo_slug, "--base", "main", "--head", branch,
                "--title", PR_TITLE.format(kind=kind, label=label, date=today), "--body-file", str(body)])
    if r.returncode != 0:
        wl.git(env, "push", "origin", "--delete", branch)
        raise RuntimeError("gh pr create failed: %s" % (r.stderr or "").strip()[:300])
    out = (r.stdout or "").strip().splitlines()
    return (out[-1] if out else ""), branch


def list_own_prs(env):
    rows = env.gh_json(["pr", "list", "-R", env.repo_slug, "--state", "open", "--limit", "100",
                        "--json", "number,url,headRefName,mergeable,createdAt"])
    if rows is None:
        raise RuntimeError("gh pr list (open) failed: cannot see in-flight engagement PRs")
    return [r for r in rows if str(r.get("headRefName") or "").startswith(BRANCH_PREFIX)]


def pr_kind(pr):
    m = re.match(r"^%s(news|daily)-" % re.escape(BRANCH_PREFIX), str(pr.get("headRefName") or ""))
    return m.group(1) if m else ""


def housekeeping(env, ledger, args, state, prs, stats):
    """24 時間以上マージされない PR を閉じ、衝突した PR を最新の main から作り直す。残った PR を返す。"""
    alive = []
    now = env.now()
    for pr in prs:
        branch = pr["headRefName"]
        created = wl.parse_ts(pr.get("createdAt"))
        if created and (now - created).total_seconds() >= PR_MAX_AGE_S:
            r = env.gh(["pr", "close", str(pr["number"]), "-R", env.repo_slug, "--delete-branch", "--comment",
                        "24 時間以上マージされなかったため閉じる (news は鮮度切れになる)。"])
            ledger.add("closed", number=pr["number"], branch=branch, ok=r.returncode == 0, reason="stale_24h")
            stats["closed"] += 1
            if r.returncode == 0:
                continue
        if str(pr.get("mergeable") or "").upper() == "CONFLICTING":
            pend = load_pending(state, branch.split("/", 1)[-1])
            if not isinstance(pend, dict) or not pend.get("rows"):
                stats["errors"] += 1
                ledger.add("error", stage="rebuild", error="pending_missing", branch=branch)
            else:
                try:
                    push_branch(env, args.repo_dir, branch, pend["rows"], force=True)
                    stats["rebuilt"] += 1
                    ledger.add("rebuild", number=pr["number"], branch=branch, url=pr.get("url"),
                               ids=[r["id"] for rs in pend["rows"].values() for r in rs])
                except Exception as e:  # noqa: BLE001
                    stats["errors"] += 1
                    ledger.add("error", stage="rebuild", error="rebuild", branch=branch, detail=str(e)[:300])
        alive.append(pr)
    return alive


# ---------------------------------------------------------------- run

def load_main_rows(source):
    out = {}
    for ch, meta in CHANNELS.items():
        out[ch] = parse_rows(source.read(meta["file"], required=True))
    return out


def cmd_run(args, env):
    state = Path(args.state_dir)
    state.mkdir(parents=True, exist_ok=True)
    now = env.now()
    run_id = args.run_id or now.astimezone(JST).strftime("%Y%m%d-%H%M%S")
    ledger = wl.Ledger(state, run_id, env)
    today = wl.jst_day(now)
    stats = {"calls": 0, "tokens": 0, "secs": 0.0, "passed": 0, "failed": 0, "dropped": 0,
             "web_discard": 0, "prs": 0, "rebuilt": 0, "closed": 0, "emitted": 0, "errors": 0,
             "quota_stop": False, "gate": "", "decisions": ""}
    lock = RunLock(state, env)
    if not lock.acquire():
        ledger.add("locked", dry_run=args.dry_run)
        print(json.dumps({"run_id": run_id, "locked": True}, ensure_ascii=False))
        return 0
    ledger.add("run_start", dry_run=args.dry_run, backend=args.backend, model=args.model, only=args.only)
    code = 0
    try:
        rows = wl.read_ledger(state)
        gate = wl.quota_gate(state, args.model, now) if args.backend == "agy" else ""
        calls, tokens, _ = wl.day_usage(rows, today, args.dry_run)
        if not gate and calls >= args.max_calls:
            gate = "daily calls %d >= %d" % (calls, args.max_calls)
        if not gate and tokens >= args.max_tokens:
            gate = "daily tokens %d >= %d" % (tokens, args.max_tokens)
        live = not args.dry_run
        source = Source(env, args.repo_dir, live)
        prs = []
        if live:
            r = wl.git(env, "fetch", "origin", "main")
            if r.returncode != 0:
                raise RuntimeError("git fetch failed: %s" % (r.stderr or "").strip()[:300])
            prs = housekeeping(env, ledger, args, state, list_own_prs(env), stats)
        main_rows = load_main_rows(source)
        pr_rows = {ch: [] for ch in CHANNELS}
        for pr in prs:
            for ch, rs in pending_rows(state, pr["headRefName"]).items():
                pr_rows[ch].extend(rs)
        trend_text = source.read(TREND_REL.format(date=today))
        trend = None
        if trend_text:
            try:
                trend = json.loads(trend_text)
            except ValueError:
                trend = None
        ctx = build_context(main_rows, pr_rows, now, trend)
        open_kinds = {pr_kind(pr) for pr in prs}
        kinds = [k for k in ("news", "daily") if args.only in (None, k)]
        decisions = []
        plans = []
        for kind in kinds:
            if kind in open_kinds:
                decisions.append("%s: open PR exists" % kind)
                ledger.add("decision", kind=kind, action="skip", reason="open_pr")
                continue
            if kind == "news":
                chans, why = plan_news(ctx, trend is not None, rows, today, args.dry_run)
            else:
                chans, why = plan_daily(ctx, rows, today, args.dry_run)
            ledger.add("decision", kind=kind, action="generate" if chans else "skip", channels=chans, reason=why)
            decisions.append("%s: %s" % (kind, ",".join(chans) if chans else "skip"))
            if chans:
                plans.append((kind, chans))
        stats["decisions"] = " | ".join(decisions)
        if gate:
            stats["gate"] = gate
            ledger.add("gate", reason=gate)
        elif plans:
            run_dir = state / "runs" / run_id
            caps = {"calls": calls, "tokens": tokens}
            clock = {"n": 0}
            stop = False
            for kind, chans in plans:
                if stop:
                    break
                units = []
                if kind == "news":
                    need = {}
                    for c in chans:
                        have = sum(1 for r in ctx["rows"][c] if r.get("category") == "news" and row_day(r) == today)
                        lo = max(1, NEWS_TARGET - have)
                        need[c] = (lo, min(NEWS_MAX_PER_CHANNEL, lo + 1))
                    units.append({"kind": kind, "channels": chans, "need": need, "name": "news"})
                else:
                    for c in chans:
                        units.append({"kind": kind, "channels": [c], "need": {c: (DAILY_MIN_EMIT, DAILY_BATCH)},
                                      "name": "daily-" + c})
                drafts = []
                for unit in units:
                    if wl.jst_day(env.now()) != today:
                        ledger.add("gate", reason="JST date changed during run")
                        stop = True
                        break
                    got, flag = run_unit(env, ledger, args, state, run_dir, unit, ctx, today, caps, stats)
                    got = trim(got, kind, unit)
                    by_ch = {}
                    for d in got:
                        by_ch.setdefault(d["channel"], []).append(d)
                    for ch in unit["channels"]:
                        n = len(by_ch.get(ch, []))
                        if kind == "daily" and n < DAILY_MIN_EMIT:
                            if n:
                                ledger.add("short", kind=kind, channel=ch, passed=n)
                            continue
                        if n < 1:
                            ledger.add("short", kind=kind, channel=ch, passed=0)
                            continue
                        drafts.extend(by_ch[ch])
                    if flag == "stop":
                        stop = True
                        break
                if not drafts:
                    continue
                rows_by_ch = assemble_rows(drafts, kind, ctx, env.now(), today, args, clock)
                # 同じ回で次の種類 (news の後の daily) を書くとき、今出した行も重複検査・直近行に入れる
                for ch, rs in rows_by_ch.items():
                    ctx["rows"][ch].extend(rs)
                    ctx["recent_texts"][ch].extend(str(r.get("text") or "") for r in rs)
                ids = [r["id"] for rs in rows_by_ch.values() for r in rs]
                stats["emitted"] += len(ids)
                save(run_dir, "rows-%s.json" % kind, json.dumps(rows_by_ch, ensure_ascii=False, indent=2))
                if args.dry_run:
                    ledger.add("pass", kind=kind, dry_run=True, counts={c: len(v) for c, v in rows_by_ch.items()}, ids=ids)
                    continue
                try:
                    url, branch = create_pr(env, state, args.repo_dir, kind, today, rows_by_ch)
                except Exception as e:  # noqa: BLE001
                    stats["errors"] += 1
                    ledger.add("error", stage="pr", error="pr", kind=kind, detail=str(e)[:300])
                    continue
                stats["prs"] += 1
                ledger.add("pr", kind=kind, url=url, branch=branch, ids=ids,
                           counts={c: len(v) for c, v in rows_by_ch.items()})
        if stats["quota_stop"]:
            code = 3
    except Exception as e:  # noqa: BLE001
        code = 1
        stats["errors"] += 1
        ledger.add("error", stage="crash", error=type(e).__name__, detail=traceback.format_exc()[-1500:])
    finally:
        lock.release()
    stats["secs"] = round(stats["secs"], 1)
    ledger.add("run_end", **stats)
    print(json.dumps(dict(stats, run_id=run_id), ensure_ascii=False))
    return code


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="K8 engagement lane (navi-brain#170 B3)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--repo-dir", default=".", help="専用の作業ツリー (記事レーンと共用しない)")
    r.add_argument("--brain-dir", required=True, help="PROMPT_ENGAGEMENT_*.md があるディレクトリ")
    r.add_argument("--state-dir", required=True)
    r.add_argument("--backend", choices=["agy", "ollama"], default="agy")
    r.add_argument("--model", default=None, help="省略時は backend の既定 (agy=%s / ollama=%s)" % (wl.DEFAULT_MODEL, OLLAMA_DEFAULT_MODEL))
    r.add_argument("--num-ctx", type=int, default=DEFAULT_NUM_CTX, help="ollama の num_ctx (agy では無視)")
    r.add_argument("--timeout", type=int, default=900)
    r.add_argument("--dry-run", action="store_true", help="PR を出さず、行を state-dir に保存するだけ (git / gh を呼ばない)")
    r.add_argument("--only", choices=["news", "daily"], default=None)
    r.add_argument("--max-calls", type=int, default=8)
    r.add_argument("--max-tokens", type=int, default=400000)
    r.add_argument("--repo-slug", default="omochairo/amazon")
    r.add_argument("--run-id", default="")
    args = ap.parse_args(argv)
    if not args.model:
        # agy 用の既定を ollama に渡すと HTTP 404 になる (2026-11-19 の試運転で実測)
        args.model = wl.DEFAULT_MODEL if args.backend == "agy" else OLLAMA_DEFAULT_MODEL
    env = LaneEnv(args.repo_dir, repo_slug=args.repo_slug)
    return cmd_run(args, env)


if __name__ == "__main__":
    sys.exit(main())
