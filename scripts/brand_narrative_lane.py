#!/usr/bin/env python3
"""K8 のブランド紹介 (brand narrative) レーン (amazon-navi-brain#170 B4): Jules の代わりに agy / ollama で書き、PR にする。

14-brand-narrative.yml は Jules の終了で止まった。このスクリプトは K8 の定時タスクから呼ばれ、
sns_copy_lane.py と同じ作りで動く (engagement_lane / writer_lane の部品を流用)。

    対象: select_brand_narrative_target.select (または --brand) が選ぶ 1 ブランド
     → データ欄 (brand_hub.json の統計) と GSC の検索語をプロンプトに貼り、書き手に ## 見出しから 3 段落目までを書かせる
     → 検査 (見出し・段落数・字数・太字のシリーズ・最安値・年齢・禁止語・データに無い数字)
     → 落ちたら理由を付けて 1 回だけ書き直し
     → hugo/content/brands/<slug>/_index.md 1 ファイルの PR (frontmatter はこのスクリプトが付ける)
     → 05-jules-auto-merge.yml が brands/*/_index.md だけの PR を自動マージする (このスクリプトは merge しない・
       open の PR は閉じも作り直しもしない)

プロンプト (private の navi-brain) と書き手の出力・GSC の検索語は --state-dir にだけ置く。
GSC の数字 (impressions) はプロンプトにだけ入れ、PR 本文・コミット・標準出力・台帳には出さない (amazon は public)。

使い方:
    python scripts/brand_narrative_lane.py run --repo-dir C:\\k8jobs\\amazon-brand \\
        --brain-dir C:\\k8jobs\\navi-brain-writer --state-dir C:\\k8jobs\\navi-brand [--dry-run] [--brand NAME]

--repo-dir は専用の作業ツリー。live の実行では毎回 origin/main に揃える。Python 3.8 で import できる書き方。
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import re
import sys
import traceback
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import writer_lane as wl  # noqa: E402
import engagement_lane as el  # noqa: E402

JST = wl.JST
BRANCH_PREFIX = "k8-brand/"
PROMPT_FILE = "BRAND_NARRATIVE_PROMPT.md"
HUB_REL = "hugo/data/brand_hub.json"
BRANDS_REL = "hugo/content/brands"
ROUNDS_PER_DAY = 1
DEFAULT_NUM_CTX = 16384
OLLAMA_DEFAULT_MODEL = el.OLLAMA_DEFAULT_MODEL
KEEP_OUT_KEYS = ("title", "auto_generated", "last_generated_at")
MIN_COUNT = 3
BODY_MIN, BODY_MAX = 200, 400
KIND = "brand_narrative"

WRITER_INSTRUCTION = (
    "write the brand narrative it asks for. You cannot create files, run commands or use the web. "
    'Output exactly one ```json code block containing a single JSON object {"markdown": "..."} and nothing else '
    "(no preface, no explanation, no frontmatter). Use only the facts in that file."
)

FORBIDDEN_RE = re.compile(
    r"創業|設立|創立|本社|受賞|認証|取得|No\.?\s*1|ナンバーワン|世界中|世界的|万個|万台|販売実績|売上|老舗"
    r"|tier|ティア|ivs|知育スコア平均|avg_|top_3|count\s*=", re.I)
HEADING_TMPL = r"^##\s*%sはなぜ「(.{2,30})」なのか\s*$"
NUM_RE = re.compile(r"\d+(?:\.\d+)?")


# ---------------------------------------------------------------- 対象の選び方

@contextlib.contextmanager
def repointed(repo_dir):
    """select_brand_narrative_target が使う TermSlugMap を --repo-dir の data に向ける。"""
    import select_brand_narrative_target as sbnt
    from term_slug import TermSlugMap
    saved = sbnt._TERM_SLUGS
    sbnt._TERM_SLUGS = slug_map(repo_dir)
    try:
        yield sbnt
    finally:
        sbnt._TERM_SLUGS = saved


def slug_map(repo_dir):
    from term_slug import TermSlugMap
    root = Path(repo_dir)
    return TermSlugMap(path=root / "data" / "term_slugs.yaml",
                       brand_taxonomy_path=root / "data" / "brand_taxonomy.yaml")


def info_from_stats(brand, stats, is_refresh, previous):
    """selector の CLI 出力と同じキー (representative_* は載せない)。"""
    top3 = [str(s) for s in (stats.get("top_3_series") or [])]
    return {"brand": brand, "count": stats["count"], "tier": stats.get("tier", "?"),
            "avg_ivs_100": stats.get("avg_ivs_100", 0), "avg_age_min_months": stats.get("avg_age_min_months", 0),
            "lowest_price": stats.get("lowest_price", 0), "top_3_series_csv": ", ".join(top3),
            "is_refresh": bool(is_refresh), "previous_last_generated_at": previous}


def pick_target(repo_dir, hub, forced):
    """戻り値 (info か None, 理由)。理由: "" / no_targets / brand_not_eligible。"""
    brands_dir = Path(repo_dir) / BRANDS_REL
    with repointed(repo_dir) as sbnt:
        if forced:
            stats = (hub.get("brands") or {}).get(forced)
            if (not isinstance(stats, dict) or stats.get("count", 0) < MIN_COUNT
                    or not all(f in stats for f in sbnt.REQUIRED_FIELDS)
                    or not isinstance(stats.get("top_3_series"), list)
                    or len(stats.get("top_3_series")) < 3):
                return None, "brand_not_eligible"
            slug = sbnt._TERM_SLUGS.get(forced) or forced
            fm = sbnt._read_frontmatter(brands_dir / slug / "_index.md")
            return info_from_stats(forced, stats, bool(fm), fm.get("last_generated_at") if fm else None), ""
        cand = sbnt.select(hub, brands_dir)
        if cand is None:
            return None, "no_targets"
        return info_from_stats(cand.name, cand.stats, cand.has_index, cand.last_generated_at), ""


def top3_of(info):
    return [s for s in str(info.get("top_3_series_csv") or "").split(", ") if s.strip()]


# ---------------------------------------------------------------- 既存ファイルの frontmatter

def split_frontmatter(text):
    """(frontmatter のブロック (--- の間の文字列), 本文) 。frontmatter が無ければ (None, text)。"""
    if not text.startswith("---"):
        return None, text
    end = text.find("\n---", 3)
    if end == -1:
        return None, text
    return text[3:end].strip("\n"), text[end + 4:]


def kept_lines(text):
    """既存の frontmatter のうち title / auto_generated / last_generated_at 以外の行 (aliases とその下のリスト・コメント)。"""
    block, _ = split_frontmatter(text or "")
    if block is None:
        return []
    out, keep = [], False
    for line in block.splitlines():
        if not line.strip():
            continue
        if line.lstrip().startswith("#"):
            out.append(line)
            continue
        if line[0] in " \t-":            # 直前のキーの続き (リストなど)
            if keep:
                out.append(line)
            continue
        key = line.split(":", 1)[0].strip()
        keep = key not in KEEP_OUT_KEYS
        if keep:
            out.append(line)
    return out


def build_file(brand, today_iso, md, keep):
    head = ["---", "title: %s" % json.dumps(brand, ensure_ascii=False), "auto_generated: true",
            'last_generated_at: "%s"' % today_iso] + list(keep) + ["---"]
    text = "\n".join(head) + "\n" + md.strip() + "\n"
    import strip_brand_narrative_cta as sbc
    new, _changed = sbc.strip_cta(text)
    return new


# ---------------------------------------------------------------- GSC の検索語 (workflow の inline python の移植)

def is_generic_brand(repo_dir, brand):
    try:
        import yaml
        with open(str(Path(repo_dir) / "data" / "brand_taxonomy.yaml"), encoding="utf-8") as f:
            tax = yaml.safe_load(f)
        for b in tax.get("brands", []):
            if b.get("canonical") == brand:
                return bool(b.get("exclude_from_taxonomy"))
    except Exception:  # noqa: BLE001
        pass
    return False


def query_section(qc, is_generic):
    """読者が実際に検索している語のセクション。impressions を含むのでプロンプトにだけ入れる。"""
    if not isinstance(qc, dict):
        return ""
    brand = qc.get("brand", "")
    lines = []
    queries = qc.get("queries") or []
    if queries and not qc.get("fallback") and not is_generic:
        lines.append("- %s 指名で検索されている語 (直近%s週, GSC実データ):" % (brand, qc.get("weeks_covered", 0)))
        for q in queries:
            lines.append("  - 「%s」(impressions計 %s)" % (q["query"], q["impressions_sum"]))
    hints = qc.get("global_seo_hints") or {}
    age = hints.get("age_keywords_seen") or []
    intent = hints.get("intent_keywords_seen") or []
    if age:
        lines.append("- サイト全体で観測される年齢層キーワード: %s" % ", ".join(age))
    if intent:
        lines.append("- サイト全体で観測される購入意図キーワード: %s" % ", ".join(intent))
    if not lines:
        return ""
    section = "\n【読者が実際に検索している語 (GSC実データ、言い回しの参考のみ)】\n"
    section += "\n".join(lines)
    section += ("\n※ 上記は検索データの参考情報。データに無い年齢層・数値・事実を新たに創作してはならない(捏造禁止)。"
                "自然に盛り込めそうな箇所があれば言い回しの参考にする程度に留める。\n")
    return section


def fetch_query_section(env, repo_dir, brand, run_dir):
    out = Path(run_dir) / "query_context.json"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        r = env.run([env.python, "scripts/build_brand_query_context.py", "--brand", brand, "--out", str(out)])
        if r.returncode != 0 or not out.exists():
            return ""
        qc = json.loads(out.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return ""
    try:
        return query_section(qc, is_generic_brand(repo_dir, brand))
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------- プロンプト

def build_prompt(original, info, slug, today_iso, qsection):
    brand = info["brand"]
    lines = [
        "# K8 の自動レーンからの実行 (この節は、後ろに続く元のプロンプトより優先する)",
        "",
        "- あなたは自動レーンの書き手。後ろに続く元のプロンプトの本文構成・文体・hallucination 防止のルールに従って、"
        "下のブランドの narrative を書く。",
        "- ファイル作成・コマンド実行・Web 参照・PR 作成は行わない (使えない)。必要な入力は下に貼ってある。",
        "- frontmatter は書かない (スクリプトが付ける)。",
        '- 出力は ```json のコードブロック 1 個だけ。中身は {"markdown": "<## 見出しから 3 段落目まで>"} だけ。',
        "- 本サイトの CTA (「本サイトでは、…独自の知育スコア…」の固定文) は書かない。",
        "- データ欄に無い事実 (創業年・国・受賞・認証・販売数・製品の型番など) は書かない。",
        "- 「tier」「avg_ivs_100」などデータ欄の項目名や「tier D」のような記号は本文に書かない。"
        "tier は元のプロンプトの「tier の意味」の言い回しに直して書く。",
        "- 数字は商品数・最安値・年齢 (avg_age_min_months を 12 で割って切り捨てた歳) だけ使う。",
        "",
        "【今回生成する brand】: %s" % brand,
        "【出力先 (参考。ファイルは作らない)】: hugo/content/brands/%s/_index.md" % slug,
        "【本日の日付】: %s" % today_iso,
        "【データ (これ以外の事実は書かない)】:",
        "  - count = %s" % info["count"],
        "  - tier = %s" % info["tier"],
        "  - avg_ivs_100 = %s" % info["avg_ivs_100"],
        "  - avg_age_min_months = %s" % info["avg_age_min_months"],
        "  - lowest_price = %s" % info["lowest_price"],
        "  - top_3_series = %s" % info["top_3_series_csv"],
        "【既存ファイル状態】: is_refresh=%s" % info["is_refresh"],
        "  - false (新規) なら作成、true (再生成) なら置き換え",
    ]
    return "\n".join(lines) + "\n" + (qsection or "") + "\n=== 以下、元のプロンプト ===\n\n" + original


# ---------------------------------------------------------------- 検査

def _nfkc(t):
    return unicodedata.normalize("NFKC", t or "")


def _digits(t):
    return set(NUM_RE.findall(re.sub(r"(?<=\d),(?=\d)", "", _nfkc(t))))


def check_markdown(md, info):
    """戻り値 [(理由, 日本語の直し方)]。空なら合格。"""
    brand = info["brand"]
    fails = []
    if not isinstance(md, str) or not md.strip():
        return [("heading", "markdown が空。## 見出しから 3 段落目までを書く")]
    lines = md.strip().splitlines()
    first = lines[0].strip()
    if not re.match(HEADING_TMPL % re.escape(_nfkc(brand)), _nfkc(first)):
        fails.append(("heading", "最初の行は「## %sはなぜ「(2〜30字のキャッチコピー)」なのか」の形にする" % brand))
    if any(ln.lstrip().startswith("#") for ln in lines[1:]):
        fails.append(("heading_extra", "見出しは最初の 1 行だけ。他の # の行は書かない"))
    body = "\n".join(lines[1:]).strip()
    paras = [p for p in re.split(r"\n\s*\n", body) if p.strip()]
    if len(paras) != 3:
        fails.append(("paragraphs", "見出しの後の段落は空行区切りでちょうど 3 つ (今 %d)" % len(paras)))
    n = len(re.sub(r"[\s*]", "", body))
    if not BODY_MIN <= n <= BODY_MAX:
        fails.append(("length", "本文 (見出しを除く) は %d〜%d 字 (今 %d 字)" % (BODY_MIN, BODY_MAX, n)))
    series = set(top3_of(info))
    hit = [s for s in series if "**%s**" % _nfkc(s) in _nfkc(md)]
    if len(hit) < 2:
        fails.append(("series", "top_3_series のうち 2 つ以上を **名前** の太字で書く (今 %d)" % len(hit)))
    price = int(float(info.get("lowest_price") or 0))
    text = _nfkc(md)
    if price > 0 and not re.search(r"¥(?:%d|%s)(?!\d)" % (price, "{:,}".format(price)), text):
        fails.append(("price", "最安値を「最安 約 ¥%d から」の形で書く" % price))
    age = int(float(info.get("avg_age_min_months") or 0)) // 12
    if age >= 1 and not re.search(r"(?<!\d)%d\s*歳ごろから" % age, text):
        fails.append(("age", "「%d歳ごろから」と書く" % age))
    m = FORBIDDEN_RE.search(text)
    if m:
        fails.append(("forbidden", "「%s」は書かない (データ欄に無い事実・断定・誇大)" % m.group(0)))
    import strip_brand_narrative_cta as sbc
    if sbc._CTA_RE.search(md):
        fails.append(("cta", "本サイトの CTA の固定文は書かない"))
    if el.URL_RE.search(md):
        fails.append(("url", "URL・ドメインは書かない"))
    allowed = {str(info.get("count"))}
    if price > 0:
        allowed.add(str(price))
    if age >= 1:
        allowed.add(str(age))
    allowed |= _digits(brand)
    for s in series:
        allowed |= _digits(s)
    extra = sorted(d for d in _digits(md) if d not in allowed)
    if extra:
        fails.append(("number", "データ欄に無い数字を書かない: %s (使えるのは商品数・最安値・年齢・ブランド名とシリーズ名の数字だけ)" % "・".join(extra)))
    return fails


def feedback_text(fails, parse_error=""):
    if parse_error:
        body = "- " + parse_error
    else:
        body = "\n".join("- %s: %s" % f for f in fails)
    head = ('前回の出力は次の自動検査で不合格になった。直して {"markdown": "..."} の ```json ブロック 1 個だけを'
            "出し直す。frontmatter・CTA・データに無い事実は書かない。\n")
    return (head + body)[:wl.FEEDBACK_MAX]


# ---------------------------------------------------------------- 書き手

def save(dirpath, name, text):
    wl.save(dirpath, name, text)


def rounds_today(rows, today, dry_run):
    return sum(1 for r in rows if r.get("event") == "round" and r.get("jst_date") == today
               and bool(r.get("dry_run")) == bool(dry_run))


def write_once(env, ledger, prompt, attempt, args, state_dir, previous=None, feedback=None):
    wr = env.write(prompt, workdir=str(Path(state_dir) / "ws"), backend=args.backend, model=args.model,
                   timeout_s=args.timeout, previous_text=previous, feedback=feedback,
                   instruction=WRITER_INSTRUCTION, num_ctx=args.num_ctx)
    ledger.add("write", kind=KIND, attempt=attempt, ok=wr.ok, error=wr.error,
               tokens=wl.tokens_of(wr.usage), secs=round(wr.secs, 1), web_calls=wr.web_calls,
               model=wr.model, conversation_id=wr.conversation_id)
    return wr


def run_round(env, ledger, args, state, run_dir, prompt, info, caps, stats):
    """1 回の生成 (書き直しを含めて最大 2 回)。戻り値 (合格した markdown か None, "stop" か "")。"""
    save(run_dir, "prompt.txt", prompt)
    ledger.add("round", kind=KIND, brand=info["brand"], dry_run=args.dry_run)
    previous = feedback = None
    for attempt in (1, 2):
        if caps["calls"] + stats["calls"] >= args.max_calls or caps["tokens"] + stats["tokens"] >= args.max_tokens:
            ledger.add("gate", reason="daily cap reached during run")
            stats["gate"] = stats["gate"] or "daily cap reached during run"
            return None, "stop" if attempt == 1 else ""
        stats["calls"] += 1
        wr = write_once(env, ledger, prompt, attempt, args, state, previous, feedback)
        stats["tokens"] += wl.tokens_of(wr.usage)
        stats["secs"] += wr.secs
        save(run_dir, "response-%d.txt" % attempt, wr.text or "")
        if wr.error == "quota":
            until = wl.mark_quota(state, env.now(), wr.reset_s)
            stats["quota_stop"] = True
            ledger.add("quota", kind=KIND, reset_s=wr.reset_s,
                       until=wl.iso(dt.datetime.fromtimestamp(until, dt.timezone.utc)))
            return None, "stop"
        if wr.web_calls:
            stats["web_discard"] += 1
            ledger.add("web_discard", kind=KIND, reason="web_calls", web_calls=wr.web_calls)
            return None, ""
        parse_error, fails, md = "", [], None
        if wr.error in ("empty", "parse"):
            parse_error = "JSON として読めなかった。```json のコードブロック 1 個だけを出す"
        elif wr.error:
            stats["errors"] += 1
            ledger.add("error", stage="write", error=wr.error, kind=KIND, detail=(wr.error_text or "")[-300:])
            return None, "stop" if wr.error == "auth" else ""
        else:
            raw = wr.article.get("markdown") if isinstance(wr.article, dict) else None
            if not isinstance(raw, str) or not raw.strip():
                parse_error = '出力に markdown (文字列) が無い。{"markdown": "..."} の形で出す'
            else:
                md = raw.replace("\r\n", "\n").strip()
                fails = check_markdown(md, info)
        if parse_error:
            reasons = {wr.error or "format": 1}
            stats["failed"] += 1
        else:
            reasons = {}
            for n, _m in fails:
                reasons[n] = reasons.get(n, 0) + 1
            if fails:
                stats["failed"] += 1
            else:
                stats["passed"] += 1
        ledger.add("check", kind=KIND, attempt=attempt, passed=not parse_error and not fails, reasons=reasons)
        if not parse_error and not fails:
            return md, ""
        if attempt == 1:
            previous = wr.text or ""
            feedback = feedback_text(fails, parse_error)
            save(run_dir, "feedback.txt", feedback)
    return None, ""


# ---------------------------------------------------------------- PR

def pr_body(brand, slug, replaced):
    return "\n".join([
        "K8 のブランド紹介レーン (k8-brand) が書いたブランド紹介です。amazon-navi-brain#170 B4。",
        "",
        "- ブランド: %s" % brand,
        "- slug: %s" % slug,
        "- 種別: %s" % ("置き換え (既存の _index.md を更新)" if replaced else "新規"),
        "",
        "変更は hugo/content/brands/%s/_index.md の 1 ファイルだけ (05 の自動マージの対象)。" % slug,
    ]) + "\n"


def push_and_pr(env, state, repo_dir, branch, rel, content, brand, slug, replaced, today):
    pushed = False
    created = False
    try:
        wl.fresh_branch(env, branch)
        path = Path(repo_dir) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(path), "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        for a in (("add", "--", rel), ("commit", "-m", "data(brand): k8 ブランド紹介 %s" % brand)):
            r = wl.git(env, *a)
            if r.returncode != 0:
                raise RuntimeError("git %s failed: %s" % (a[0], (r.stderr or r.stdout or "").strip()[:300]))
        r = wl.git(env, "push", "-u", "origin", branch)
        if r.returncode != 0:
            raise RuntimeError("git push failed: %s" % (r.stderr or r.stdout or "").strip()[:300])
        pushed = True
        body = Path(state) / "pr_body.md"
        body.write_text(pr_body(brand, slug, replaced), encoding="utf-8")
        r = env.gh(["pr", "create", "-R", env.repo_slug, "--base", "main", "--head", branch,
                    "--title", "[k8] ブランド紹介: %s" % brand, "--body-file", str(body)])
        if r.returncode != 0:
            raise RuntimeError("gh pr create failed: %s" % (r.stderr or "").strip()[:300])
        created = True
    finally:
        if pushed and not created:
            wl.git(env, "push", "origin", "--delete", branch)
        wl.back_to_main(env, branch, [rel])
    out = (r.stdout or "").strip().splitlines()
    return out[-1] if out else ""


def list_own_prs(env):
    rows = env.gh_json(["pr", "list", "-R", env.repo_slug, "--state", "open", "--limit", "100",
                        "--json", "number,url,headRefName"])
    if rows is None:
        raise RuntimeError("gh pr list (open) failed: cannot see in-flight brand PRs")
    return [r for r in rows if str(r.get("headRefName") or "").startswith(BRANCH_PREFIX)]


# ---------------------------------------------------------------- run

def cmd_run(args, env):
    state = Path(args.state_dir)
    state.mkdir(parents=True, exist_ok=True)
    now = env.now()
    run_id = args.run_id or now.astimezone(JST).strftime("%Y%m%d-%H%M%S")
    ledger = wl.Ledger(state, run_id, env)
    today = wl.jst_day(now)
    today_iso = today + "T10:00:00+09:00"
    stats = {"calls": 0, "tokens": 0, "secs": 0.0, "passed": 0, "failed": 0, "web_discard": 0, "prs": 0,
             "errors": 0, "quota_stop": False, "gate": "", "decision": "", "brand": ""}
    lock = el.RunLock(state, env)
    if not lock.acquire():
        ledger.add("locked", dry_run=args.dry_run)
        print(json.dumps({"run_id": run_id, "locked": True}, ensure_ascii=False))
        return 0
    ledger.add("run_start", dry_run=args.dry_run, backend=args.backend, model=args.model, brand=args.brand or None)
    code = 0
    try:
        rows = wl.read_ledger(state)
        gate = wl.quota_gate(state, args.model, now) if args.backend == "agy" else ""
        calls, tokens, _ = wl.day_usage(rows, today, args.dry_run)
        if not gate and calls >= args.max_calls:
            gate = "daily calls %d >= %d" % (calls, args.max_calls)
        if not gate and tokens >= args.max_tokens:
            gate = "daily tokens %d >= %d" % (tokens, args.max_tokens)
        if not gate and rounds_today(rows, today, args.dry_run) >= ROUNDS_PER_DAY:
            gate = "rounds today >= %d" % ROUNDS_PER_DAY
        live = not args.dry_run
        prs = []
        if live and not gate:
            r = wl.git(env, "fetch", "origin", "main")
            if r.returncode != 0:
                raise RuntimeError("git fetch failed: %s" % (r.stderr or "").strip()[:300])
            prs = list_own_prs(env)      # 人がレビューする PR なので、閉じも作り直しもしない
            r = wl.git(env, "switch", "--discard-changes", "--detach", "origin/main")
            if r.returncode != 0:
                raise RuntimeError("git switch origin/main failed: %s" % (r.stderr or "").strip()[:300])
            wl.git(env, "clean", "-fdq", "--", BRANDS_REL)   # 異常終了で残った書きかけを既存ページと誤認しない
        if prs:
            stats["decision"] = "skip: open PR exists"
            ledger.add("decision", action="skip", reason="open_pr", count=len(prs))
        elif gate:
            stats["gate"] = gate
            stats["decision"] = "skip: gate"
            ledger.add("gate", reason=gate)
        else:
            code = generate(args, env, state, ledger, stats, today, today_iso, calls, tokens)
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


def generate(args, env, state, ledger, stats, today, today_iso, calls, tokens):
    """対象を選び、書いて、PR (dry-run なら保存) まで。戻り値は終了コード。"""
    repo = Path(args.repo_dir)
    run_dir = state / "runs" / (ledger.run_id)
    r = env.run([env.python, "scripts/build_brand_hub_stats.py"])
    if r.returncode != 0:
        raise RuntimeError("build_brand_hub_stats failed: %s" % (r.stderr or r.stdout or "").strip()[-300:])
    hub = wl.read_json(repo / HUB_REL, None)
    if not isinstance(hub, dict):
        raise RuntimeError("cannot read %s" % HUB_REL)
    info, why = pick_target(args.repo_dir, hub, args.brand)
    if info is None:
        if why == "no_targets":
            stats["decision"] = "skip: no targets"
            ledger.add("decision", action="skip", reason="no_targets")
            return 0
        stats["errors"] += 1
        stats["decision"] = "error: brand not eligible"
        ledger.add("error", stage="select", error=why, brand=args.brand)
        return 1
    brand = info["brand"]
    stats["brand"] = brand
    slug = slug_map(args.repo_dir).get(brand)
    if not slug:
        stats["errors"] += 1
        stats["decision"] = "error: no slug"
        ledger.add("error", stage="slug", error="slug_missing", brand=brand)
        return 1
    rel = "%s/%s/_index.md" % (BRANDS_REL, slug)
    existing = None
    with contextlib.suppress(OSError):
        existing = (repo / rel).read_text(encoding="utf-8")
    if existing is not None:
        import select_brand_narrative_target as sbnt
        fm = sbnt._read_frontmatter(repo / rel)
        if fm.get("auto_generated") is False:
            stats["decision"] = "skip: manual"
            ledger.add("decision", action="skip", reason="manual", brand=brand)
            return 0
    keep = kept_lines(existing) if existing is not None else []
    stats["decision"] = "generate"
    ledger.add("decision", action="generate", reason="target", brand=brand, slug=slug, replaced=existing is not None)
    prompt_path = Path(args.brain_dir) / PROMPT_FILE
    try:
        original = prompt_path.read_text(encoding="utf-8")
    except OSError:
        stats["errors"] += 1
        ledger.add("error", stage="prompt", error="prompt_missing", detail=prompt_path.name)
        return 0
    qsection = fetch_query_section(env, args.repo_dir, brand, run_dir)
    prompt = build_prompt(original, info, slug, today_iso, qsection)
    caps = {"calls": calls, "tokens": tokens}
    md, _flag = run_round(env, ledger, args, state, run_dir, prompt, info, caps, stats)
    if md is None:
        ledger.add("short", passed=0, brand=brand)
        return 0
    content = build_file(brand, today_iso, md, keep)
    if args.dry_run:
        save(run_dir, "_index.md", content)
        ledger.add("pass", dry_run=True, brand=brand, slug=slug)
        return 0
    branch = "%s%s-%s" % (BRANCH_PREFIX, today, slug)
    try:
        url = push_and_pr(env, state, args.repo_dir, branch, rel, content, brand, slug, existing is not None, today)
    except Exception as e:  # noqa: BLE001
        stats["errors"] += 1
        ledger.add("error", stage="pr", error="pr", detail=str(e)[:300])
    else:
        stats["prs"] += 1
        ledger.add("pr", url=url, branch=branch, brand=brand, slug=slug)
    return 0


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="K8 brand narrative lane (navi-brain#170 B4)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--repo-dir", default=".", help="専用の作業ツリー (他のレーンと共用しない)")
    r.add_argument("--brain-dir", required=True, help="%s があるディレクトリ" % PROMPT_FILE)
    r.add_argument("--state-dir", required=True)
    r.add_argument("--brand", default="", help="選定を飛ばして指定する (brand_hub に count>=3 で居ること)")
    r.add_argument("--backend", choices=["agy", "ollama"], default="agy")
    r.add_argument("--model", default=None)
    r.add_argument("--num-ctx", type=int, default=DEFAULT_NUM_CTX)
    r.add_argument("--timeout", type=int, default=900)
    r.add_argument("--dry-run", action="store_true", help="PR を出さず、_index.md を state-dir に保存するだけ (git / gh を呼ばない)")
    r.add_argument("--max-calls", type=int, default=2)
    r.add_argument("--max-tokens", type=int, default=120000)
    r.add_argument("--repo-slug", default="omochairo/amazon")
    r.add_argument("--run-id", default="")
    args = ap.parse_args(argv)
    if not args.model:
        args.model = wl.DEFAULT_MODEL if args.backend == "agy" else OLLAMA_DEFAULT_MODEL
    env = el.LaneEnv(args.repo_dir, repo_slug=args.repo_slug)
    return cmd_run(args, env)


if __name__ == "__main__":
    sys.exit(main())
