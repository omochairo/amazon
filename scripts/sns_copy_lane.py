#!/usr/bin/env python3
"""K8 の SNS 商品投稿コピーレーン (amazon-navi-brain#170 B2): Jules の代わりに agy / ollama でコピーを書き、PR にする。

35-jules-sns-copy.yml は Jules の終了で止まった。止まったままだと、20-sns-publish.yml の商品投稿の本文が
meta_description の切り詰めに落ちる。このスクリプトは K8 の定時タスクから呼ばれ、
engagement_lane.py (エンゲージ下書きレーン) の部品を流用して同じ作りで動く。

    対象: select_sns_copy_targets の上位 --top 件のうち、コピーが main にも自分の open PR にも無い ASIN
     → 記事 JSON の要るキーだけをプロンプトに貼り、書き手 (agy / ollama) に 1 回で全 ASIN 分を書かせる
     → 検査 (36 より厳しめ。URL・ハッシュタグ・文字数・プロンプトの「守ること」のうち機械で見られるもの)
     → 落ちたコピーは理由を付けて 1 回だけ書き直し → それでも落ちた ASIN は今回は出さない
     → data/sns_copy/<ASIN>.json の新規作成だけの PR (既にあるファイルは上書きしない)
     → 36 が検査して自動マージ (下流は変えない。このスクリプトは merge しない)

プロンプト (private の navi-brain) と書き手の出力・コピーの本文は --state-dir にだけ置く。
PR・標準出力・例外メッセージ・台帳には本文を出さない (件数と ASIN だけ)。

使い方 (cwd は何処でもよい):
    python scripts/sns_copy_lane.py run --repo-dir C:\\k8jobs\\amazon-sns-copy \\
        --brain-dir C:\\k8jobs\\navi-brain-writer --state-dir C:\\k8jobs\\navi-sns-copy [--dry-run]

--repo-dir は専用の作業ツリー (記事レーン・エンゲージレーンと共用しない)。live の実行では毎回
origin/main に揃える (fresh_branch / back_to_main が作業ツリーを強制的に切り替えるため)。
K8 の Python は 3.11 だが、runner (3.8) と同じく 3.8 で import できる書き方にしている。
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
from _x_text import weighted_len  # noqa: E402

JST = wl.JST
BRANCH_PREFIX = "k8-sns-copy/"
PR_TITLE = "[k8-sns-copy] {date} {n} 件"
PROMPT_FILE = "PROMPT_SNS_COPY.md"
ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
PR_MAX_AGE_S = el.PR_MAX_AGE_S
TOP_DEFAULT = 6
ROUNDS_PER_DAY = 2             # 1 日 (JST) の生成回数
X_WEIGHT_LIMIT = 220           # プロンプト: 日本語 110 字 = weighted 220 (36 の上限 240 より手前)
THREADS_CHAR_LIMIT = 180       # プロンプト §2「180 文字以内 (厳守)」 (36 の上限 480 より手前)
EMOJI_LIMIT = 3                # プロンプト: 絵文字 0〜2 個。3 個以上で不合格
HASHTAG_LIMIT = 3              # プロンプト: ハッシュタグは 1〜2 個まで。3 個以上で不合格
RATIONALE_MAX = 300
DEFAULT_NUM_CTX = 16384
OLLAMA_DEFAULT_MODEL = el.OLLAMA_DEFAULT_MODEL
FEATURES_MAX = 6
PROS_MAX = 4
CONS_MAX = 3

# 書き手に渡す「ファイルを読ませる文」の続き (article_writer.write の instruction)
WRITER_INSTRUCTION = (
    "write the social media copy it asks for. You cannot create files, run commands or use the web. "
    "Output exactly one ```json code block containing a single JSON object of the form "
    '{"copies": [...]} and nothing else (no preface, no explanation). '
    "Use only the facts in that file and never invent URLs."
)

# ---------------------------------------------------------------- 検査 (プロンプト §2 の「守ること」から機械で見られるもの)

URL_RE = el.URL_RE
HASHTAG_RE = el.HASHTAG_RE
HASHTAG_TOKEN_RE = re.compile(r"[#＃][^\s#＃]+")
# 作り手都合 (「ランキングで上位だから」「スコアが高いから」)
MAKER_RE = re.compile(r"ランキング|スコア|上位|edu_?axis|知育軸|ivs|総合評価\s*[0-9０-９]|[0-9０-９]\s*点満点|点数", re.I)
# 価格・在庫 (具体額を書かない。変動する情報・最安の断定も書かない)
PRICE_RE = re.compile(
    r"[¥￥]|[0-9０-９][0-9０-９,，.]*\s*万?円|[一二三四五六七八九十百千万]+円|円台|税込|最安|値下げ|セール|在庫|売り切れ|残りわずか"
    r"|送料無料|激安|格安")
# 誇大・断定の医療/発達効果・断定
CLAIM_RE = re.compile(
    r"頭(?:が)?(?:良|よ)くなる|賢くなる|天才|IQ|偏差値|発達(?:障害|を促|が促|を改善|が改善|が早)|治(?:る|療|す)"
    r"|必ず|絶対|100\s*[%％]|確実に|誰でも|間違いなし|日本一|世界一|ナンバーワン|No\.?\s*1", re.I)
# 本文末の導線 (配信側が商品 URL を自動付与するので不要)
CTA_RE = re.compile(r"詳しくは|こちら(?:から|を|へ|で|は)|リンク(?:から|先|は|を|へ)|プロフ|👇|⬇|👉|↓|チェック(?:して|してね|してみて)|ぜひ(?:チェック|ご覧|クリック)")
# 特定の人物としての名乗り・署名
SELF_INTRO_RE = re.compile(el.SELF_INTRO_RE.pattern + r"|編集部(?:です|より|から)")
SIGNATURE_RE = el.SIGNATURE_RE
# 数字+単位は記事 JSON に出てくるものだけ (嘘・未確認のスペックを書かない)
FACT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(歳|才|か月|ヶ月|ケ月|カ月|ピース|種類|枚|cm|mm|センチ|ミリ|冊|曲|色|セット|個)", re.I)

CHECK_REASONS = ("missing", "asin", "length", "url", "hashtag", "emoji", "self_intro", "signature", "maker_view",
                 "price", "claim", "cta", "fact", "same_text", "dup_text")
JACCARD_LIMIT = 0.6


def _norm_fact_unit(u):
    u = u.lower()
    if u == "才":
        return "歳"
    return "か月" if u in ("ヶ月", "ケ月", "カ月") else u


def _facts(text):
    t = unicodedata.normalize("NFKC", text or "")
    return {m.group(1) + _norm_fact_unit(m.group(2)) for m in FACT_RE.finditer(t)}


def article_pool(article):
    """記事 JSON 全体を 1 本の文字列にしたもの (数字+単位の裏取り用)。"""
    return unicodedata.normalize("NFKC", json.dumps(article, ensure_ascii=False)).replace(" ", "")


def _pool_facts(pool):
    return {m.group(1) + _norm_fact_unit(m.group(2)) for m in FACT_RE.finditer(pool)}


def text_checks(label, text, limit_kind, pool_facts):
    """x / threads どちらか 1 本の本文の検査。戻り値は [(理由名, 文)]。"""
    fails = []
    if not isinstance(text, str) or not text.strip():
        return [("missing", "%s が空 (x と threads は必ず両方書く)" % label)]
    t = text.strip()
    if limit_kind == "x":
        w = weighted_len(t)
        if w > X_WEIGHT_LIMIT:
            fails.append(("length", "x は日本語 110 字以内 (weighted %d 以内)。今 weighted %d" % (X_WEIGHT_LIMIT, w)))
    elif len(t) > THREADS_CHAR_LIMIT:
        fails.append(("length", "threads は %d 字以内。今 %d 字" % (THREADS_CHAR_LIMIT, len(t))))
    if URL_RE.search(t):
        fails.append(("url", "%s に URL・ドメインを入れない" % label))
    tags = len(HASHTAG_TOKEN_RE.findall(t))
    if tags >= HASHTAG_LIMIT or (HASHTAG_RE.search(t) and not tags):
        fails.append(("hashtag", "%s のハッシュタグは 2 個まで (今 %d 個)" % (label, tags)))
    if el.count_emoji(t) >= EMOJI_LIMIT:
        fails.append(("emoji", "%s の絵文字は 0〜2 個 (今 %d 個)" % (label, el.count_emoji(t))))
    if SELF_INTRO_RE.search(t):
        fails.append(("self_intro", "%s で特定の人物や編集部として名乗らない" % label))
    if SIGNATURE_RE.search(t):
        fails.append(("signature", "%s の末尾に署名を付けない" % label))
    if MAKER_RE.search(t):
        fails.append(("maker_view", "%s に作り手都合 (ランキング・スコア・上位など) を書かない。読者の得を言う" % label))
    if PRICE_RE.search(t):
        fails.append(("price", "%s に価格の具体額・最安・在庫・セールを書かない" % label))
    if CLAIM_RE.search(t):
        fails.append(("claim", "%s に誇大・断定 (頭が良くなる・必ず・絶対・発達への効果など) を書かない" % label))
    if CTA_RE.search(t):
        fails.append(("cta", "%s の本文に「詳しくはこちら」「👇」などの導線を入れない" % label))
    extra = sorted(_facts(t) - pool_facts)
    if extra:
        fails.append(("fact", "%s の数字+単位が記事 JSON に無い: %s" % (label, "・".join(extra))))
    return fails


def individual_checks(copy, target_asins, pool_facts):
    """1 コピーずつの検査。戻り値は [(理由名, 文)]。空なら合格。"""
    if not isinstance(copy, dict):
        return [("missing", "copies の要素が JSON オブジェクトでない")]
    asin = copy.get("asin")
    if not isinstance(asin, str) or asin not in target_asins:
        return [("asin", "asin は今回の対象のものを大文字でそのまま入れる")]
    fails = text_checks("x", copy.get("x"), "x", pool_facts) + text_checks("threads", copy.get("threads"), "threads", pool_facts)
    if isinstance(copy.get("x"), str) and isinstance(copy.get("threads"), str) \
            and copy["x"].strip() and copy["x"].strip() == copy["threads"].strip():
        fails.append(("same_text", "x と threads は別の文にする (それぞれの文体で書く)"))
    return fails


def normalize_copies(raw):
    """書き手の出力 (dict) から copies のリストを取る。取れなければ None。"""
    copies = raw.get("copies") if isinstance(raw, dict) else None
    if not isinstance(copies, list):
        return None
    out = []
    for c in copies:
        if not isinstance(c, dict):
            out.append(c)
            continue
        d = dict(c)
        if isinstance(d.get("asin"), str):
            d["asin"] = d["asin"].strip().upper()
        for k in ("x", "threads"):
            if isinstance(d.get(k), str):
                d[k] = d[k].replace("\r\n", "\n").strip()
        out.append(d)
    return out


def check_batch(copies, targets, pools, taken):
    """copies を検査。戻り値 (合格 {asin: record}, [(asin, [(理由, 文)])], 対象外・重複で無視した数)。

    taken = これまでに合格した {asin: record} (書き直しで同じ ASIN を二重に採らない)。
    対象なのに出力に無い ASIN は ("missing") として bad に入れる。
    """
    asins = [t["asin"] for t in targets]
    accepted, bad, ignored = {}, [], 0
    seen = set(taken)
    answered = set()
    for c in copies:
        asin = c.get("asin") if isinstance(c, dict) else None
        if isinstance(asin, str) and asin in asins:
            answered.add(asin)
        if isinstance(asin, str) and asin in seen:
            ignored += 1
            continue
        fails = individual_checks(c, set(asins), _pool_facts(pools.get(asin, "")))
        if any(n == "asin" for n, _ in fails):
            ignored += 1
            continue
        if not fails:
            for other in list(taken.values()) + list(accepted.values()):
                for k in ("x", "threads"):
                    if el.jaccard(c[k], other[k]) >= JACCARD_LIMIT:
                        fails.append(("dup_text", "同じ回の別の ASIN のコピーとほぼ同じ文 (%s)" % k))
                        break
                if fails:
                    break
        if fails:
            bad.append((asin, fails))
        else:
            rationale = c.get("rationale")
            rationale = rationale.strip() if isinstance(rationale, str) else ""
            if URL_RE.search(rationale):
                rationale = ""
            accepted[asin] = {"x": c["x"], "threads": c["threads"], "rationale": rationale[:RATIONALE_MAX]}
            seen.add(asin)
    bad = [(a, f) for a, f in bad if a not in accepted]   # 同じ回答の中で後から合格した ASIN は落とさない
    for asin in asins:
        if asin not in answered and asin not in taken:
            bad.append((asin, [("missing", "出力に無い。この asin の x と threads を書く")]))
    return accepted, bad, ignored


def feedback_text(bad, parse_error=""):
    if parse_error:
        body = "- " + parse_error
    else:
        body = "\n".join("- %s: %s" % (a, " / ".join("%s: %s" % f for f in fails)) for a, fails in bad)
    head = ("前回の出力は次の自動検査で不合格になった。落ちた ASIN を直し、合格していたものも含めて"
            "{\"copies\": [...]} の全体を出し直す。URL・名乗り・署名・価格の数字は書かない。ハッシュタグは 2 個まで。\n")
    return (head + body)[:wl.FEEDBACK_MAX]


# ---------------------------------------------------------------- 記事 JSON からプロンプトの入力を作る

def _flat(text, n=200):
    return re.sub(r"\s+", " ", str(text if text is not None else "")).strip()[:n]


def _items(v, limit, n):
    if isinstance(v, str):
        v = [v]
    if isinstance(v, dict):
        v = list(v.values())
    if not isinstance(v, list):
        return []
    return [_flat(x if isinstance(x, str) else json.dumps(x, ensure_ascii=False), n) for x in v[:limit] if x]


def article_block(target, article):
    """1 ASIN 分の入力。本文 (narrative) は貼らず、プロンプト §1 が読む指定のキーだけを短く貼る。"""
    p = article.get("product") if isinstance(article.get("product"), dict) else {}
    lines = ["### %s" % target["asin"],
             "- edu_axis (知育軸 2.0-5.0): %s / best_price: %s (価格は変動する。本文に金額は書かない)" % (
                 target.get("edu_axis"), target.get("best_price")),
             "- title: %s" % _flat(article.get("title") or target.get("title"), 160),
             "- meta_description: %s" % _flat(article.get("meta_description"), 200),
             "- 商品名: %s" % _flat(p.get("name_full") or p.get("name"), 120),
             "- ブランド: %s" % _flat(p.get("brand"), 60),
             "- 対象年齢: %s" % _flat(p.get("target_age"), 60)]
    for key, label, limit, n in (("edu_domains", "知育の軸", 6, 40), ("certifications", "認証", 4, 40),
                                 ("features", "特徴", FEATURES_MAX, 100), ("pros", "pros", PROS_MAX, 80),
                                 ("cons", "cons", CONS_MAX, 80)):
        vals = _items(p.get(key), limit, n)
        if vals:
            lines.append("- %s: %s" % (label, " / ".join(vals)))
    return "\n".join(lines)


def build_prompt(original, today, blocks):
    """元のプロンプトの前に K8 用の前置き (入力を貼る) を付ける。プロンプト本文は書き換えない。"""
    lines = [
        "# K8 の自動レーンからの実行 (この節は、後ろに続く元のプロンプトより優先する)",
        "",
        "- あなたは自動レーンの書き手。後ろに続く元のプロンプトの仕様 (文体・守ること・文字数) に従って、"
        "下の各 ASIN の X / Threads の訴求文を書く。",
        "- 元のプロンプトにある「data/articles を読む」「ファイルを書き出す」「PR を作る」は、ここでは行わない。"
        "ファイル・コマンド・Web は使えない。必要な入力は下に貼ってある (記事 JSON の要点)。PR やファイルは作らない。",
        "- 出力は ```json のコードブロック 1 個だけ (前置き・説明なし)。中身は {\"copies\": [ ... ]}。"
        "下の ASIN 1 件につき要素 1 個 (asin・x・threads・rationale の 4 キーだけ)。",
        "- source・generated_at は書かない (スクリプトが付ける)。asin は下の見出しのとおり大文字で書く。",
        "- 本文に書いてよい事実は下の入力にあるものだけ。数字+単位 (「3歳」「12ピース」など) も入力にあるものだけ使う。",
        "- 今日 (JST) は %s。" % today,
        "",
        "## 入力 (元のプロンプトの §1 で読む指定のものを、ここに貼った)",
        "",
    ]
    return "\n".join(lines) + "\n".join(blocks) + "\n\n---\n\n# 元のプロンプト\n\n" + original


# ---------------------------------------------------------------- 対象の選び方

@contextlib.contextmanager
def repointed(repo_dir):
    """select_sns_copy_targets / pick_sns_target / sns_copy_store が読む場所を --repo-dir の中に向ける。"""
    import pick_sns_target as pst
    import sns_copy_store as store
    root = Path(repo_dir)
    saved = (pst.ARTICLES_DIR, pst.STATE_FILE, store.STORE_DIR)
    pst.ARTICLES_DIR = root / "data" / "articles"
    pst.STATE_FILE = root / "data" / "sns_published.json"
    store.STORE_DIR = root / "data" / "sns_copy"
    try:
        yield
    finally:
        pst.ARTICLES_DIR, pst.STATE_FILE, store.STORE_DIR = saved


def default_select(repo_dir, n):
    import select_sns_copy_targets as sel
    with repointed(repo_dir):
        return sel.select_targets(n)


def choose_targets(repo_dir, top, pending_asins, source, select_fn=None):
    """上位 top 件のうち、コピーが main にも open PR (pending) にも無い ASIN。

    select_targets は「コピーが無い ASIN から top 件」を返すので、pending で除かれる分だけ多めに取る。
    """
    select_fn = select_fn or default_select
    got = select_fn(repo_dir, top + len(pending_asins))
    out = []
    for t in got:
        asin = t.get("asin")
        if asin in pending_asins or not isinstance(asin, str) or not ASIN_RE.match(asin):
            continue
        if source.read("data/sns_copy/%s.json" % asin) is not None:
            continue    # main に既にある (作業ツリーが古いときの保険)
        out.append(t)
        if len(out) >= top:
            break
    return out


# ---------------------------------------------------------------- 状態 (自分の open PR)

def load_pending(state, name):
    return wl.read_json(Path(state) / "pending" / (name + ".json"), None)


def pending_files(state, branch):
    """自分の open PR に載せたコピー (台帳側に保存したもの)。{asin: record}"""
    pend = load_pending(state, branch.split("/", 1)[-1])
    if not isinstance(pend, dict):
        return {}
    files = pend.get("files")
    return dict(files) if isinstance(files, dict) else {}


def prune_pending(state, prs):
    """open でなくなった PR (マージ・クローズ済み) の pending を消す。"""
    alive = {pr["headRefName"].split("/", 1)[-1] for pr in prs}
    pdir = Path(state) / "pending"
    if not pdir.exists():
        return
    for p in pdir.glob("*.json"):
        if p.stem not in alive:
            with contextlib.suppress(OSError):
                p.unlink()


def rounds_today(rows, today, dry_run):
    return sum(1 for r in rows if r.get("event") == "round" and r.get("jst_date") == today
               and bool(r.get("dry_run")) == bool(dry_run))


def generator_name(backend, model):
    return el.generator_name(backend, model)


def build_record(asin, rec, now, args):
    """data/sns_copy/<ASIN>.json の中身。キー順は既存ファイルに合わせる。"""
    return {"asin": asin, "x": rec["x"], "threads": rec["threads"], "rationale": rec.get("rationale", ""),
            "source": generator_name(args.backend, args.model),
            "generated_at": now.astimezone(dt.timezone.utc).isoformat(timespec="seconds")}


# ---------------------------------------------------------------- 書き手 1 回分

def save(dirpath, name, text):
    wl.save(dirpath, name, text)


def write_once(env, ledger, prompt, attempt, args, state_dir, previous=None, feedback=None):
    wr = env.write(prompt, workdir=str(Path(state_dir) / "ws"), backend=args.backend, model=args.model,
                   timeout_s=args.timeout, previous_text=previous, feedback=feedback,
                   instruction=WRITER_INSTRUCTION, num_ctx=args.num_ctx)
    ledger.add("write", kind="sns_copy", attempt=attempt, ok=wr.ok, error=wr.error,
               tokens=wl.tokens_of(wr.usage), secs=round(wr.secs, 1), web_calls=wr.web_calls,
               model=wr.model, conversation_id=wr.conversation_id)
    return wr


def run_round(env, ledger, args, state, run_dir, prompt, targets, pools, caps, stats):
    """1 回の生成 (書き直しを含めて最大 2 回の呼び出し)。戻り値は ({asin: record}, "stop" か "")。"""
    save(run_dir, "prompt.txt", prompt)
    ledger.add("round", kind="sns_copy", asins=[t["asin"] for t in targets], dry_run=args.dry_run)
    accepted, previous, feedback = {}, None, None
    for attempt in (1, 2):
        if caps["calls"] + stats["calls"] >= args.max_calls or caps["tokens"] + stats["tokens"] >= args.max_tokens:
            ledger.add("gate", reason="daily cap reached during run")
            stats["gate"] = stats["gate"] or "daily cap reached during run"
            return accepted, "stop" if attempt == 1 else ""
        stats["calls"] += 1
        wr = write_once(env, ledger, prompt, attempt, args, state, previous, feedback)
        stats["tokens"] += wl.tokens_of(wr.usage)
        stats["secs"] += wr.secs
        save(run_dir, "response-%d.txt" % attempt, wr.text or "")
        if wr.error == "quota":
            until = wl.mark_quota(state, env.now(), wr.reset_s)
            stats["quota_stop"] = True
            ledger.add("quota", kind="sns_copy", reset_s=wr.reset_s,
                       until=wl.iso(dt.datetime.fromtimestamp(until, dt.timezone.utc)))
            return accepted, "stop"
        if wr.web_calls:
            stats["web_discard"] += 1
            ledger.add("discard", kind="sns_copy", reason="web_calls", web_calls=wr.web_calls)
            return {}, ""
        parse_error, bad, ignored = "", [], 0
        if wr.error in ("empty", "parse"):
            parse_error = "JSON として読めなかった。```json のコードブロック 1 個だけを出す"
        elif wr.error:
            stats["errors"] += 1
            ledger.add("error", stage="write", error=wr.error, kind="sns_copy", detail=(wr.error_text or "")[-300:])
            return accepted, "stop" if wr.error == "auth" else ""
        else:
            copies = normalize_copies(wr.article)
            if copies is None:
                parse_error = "出力に copies (配列) が無い。{\"copies\": [...]} の形で出す"
            else:
                got, bad, ignored = check_batch(copies, targets, pools, accepted)
                accepted.update(got)
                reasons = {}
                for _, fails in bad:
                    for n, _m in fails:
                        reasons[n] = reasons.get(n, 0) + 1
                ledger.add("check", kind="sns_copy", attempt=attempt, copies=len(copies), passed=len(got),
                           failed=len(bad), ignored=ignored, reasons=reasons)
                stats["passed"] += len(got)
                stats["failed"] += len(bad)
        if parse_error:
            ledger.add("check", kind="sns_copy", attempt=attempt, copies=0, passed=0, failed=0, ignored=0,
                       reasons={wr.error or "format": 1})
        if not parse_error and not bad:
            break
        if attempt == 1:
            previous = wr.text or ""
            feedback = feedback_text(bad, parse_error)
            save(run_dir, "feedback.txt", feedback)
    return accepted, ""


# ---------------------------------------------------------------- PR

def write_new_files(repo_dir, records):
    """data/sns_copy/<ASIN>.json の新規作成だけ ('x' モード)。既にあるファイルは上書きしない。作ったパスを返す。"""
    made = []
    d = Path(repo_dir) / "data" / "sns_copy"
    d.mkdir(parents=True, exist_ok=True)
    for asin, rec in records.items():
        path = d / ("%s.json" % asin)
        try:
            with open(str(path), "x", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(rec, ensure_ascii=False, indent=2) + "\n")
        except FileExistsError:
            continue
        made.append("data/sns_copy/%s.json" % asin)
    return made


class NothingToCommit(RuntimeError):
    """載せるコピーが全部 main に既にある (作り直す意味が無い)。"""


def push_branch(env, repo_dir, branch, records, force):
    """origin/main から branch を切り直し、新規ファイルを作って push する。force なら --force-with-lease。"""
    paths = []
    try:
        wl.fresh_branch(env, branch)
        paths = write_new_files(repo_dir, records)
        if not paths:
            raise NothingToCommit("no new file to commit (all copies already exist on main)")
        for args in (("add", "--") + tuple(paths),
                     ("commit", "-m", "data(sns_copy): k8 コピーを %d 件追加 (%s)" % (len(paths), branch.split("/", 1)[-1]))):
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


def pr_body(asins):
    lines = ["K8 の SNS コピーレーン (k8-sns-copy) が追加した商品投稿コピーです。amazon-navi-brain#170 B2。", ""]
    lines += ["- " + a for a in asins]
    lines += ["", "変更は data/sns_copy/<ASIN>.json の新規作成だけ。検査と自動マージは 36 が行う。"]
    return "\n".join(lines) + "\n"


def next_branch(state, ledger_rows, today):
    """k8-sns-copy/<日付>-<n>。n は同じ日に作った PR (台帳 + pending) の最大の続き。"""
    best = 0
    pat = re.compile(r"-(\d+)$")
    for r in ledger_rows:
        if r.get("event") == "pr" and r.get("jst_date") == today:
            m = pat.search(str(r.get("branch") or ""))
            best = max(best, int(m.group(1)) if m else 0)
    pdir = Path(state) / "pending"
    if pdir.exists():
        for p in pdir.glob("%s-*.json" % today):
            m = pat.search(p.stem)
            best = max(best, int(m.group(1)) if m else 0)
    return "%s%s-%d" % (BRANCH_PREFIX, today, best + 1)


def create_pr(env, state, repo_dir, ledger_rows, today, records):
    branch = next_branch(state, ledger_rows, today)
    # 衝突時の作り直し用に、載せたコピーを台帳側に保存する (PR の本文には書かない)
    pend = Path(state) / "pending" / ("%s.json" % branch.split("/", 1)[-1])
    pend.parent.mkdir(parents=True, exist_ok=True)
    pend.write_text(json.dumps({"date": today, "branch": branch, "files": records}, ensure_ascii=False),
                    encoding="utf-8")
    try:
        push_branch(env, repo_dir, branch, records, force=False)
        body = Path(state) / "pr_body.md"
        body.write_text(pr_body(list(records)), encoding="utf-8")
        r = env.gh(["pr", "create", "-R", env.repo_slug, "--base", "main", "--head", branch,
                    "--title", PR_TITLE.format(date=today, n=len(records)), "--body-file", str(body)])
        if r.returncode != 0:
            wl.git(env, "push", "origin", "--delete", branch)
            raise RuntimeError("gh pr create failed: %s" % (r.stderr or "").strip()[:300])
    except Exception:
        with contextlib.suppress(OSError):
            pend.unlink()
        raise
    out = (r.stdout or "").strip().splitlines()
    return (out[-1] if out else ""), branch


def list_own_prs(env):
    rows = env.gh_json(["pr", "list", "-R", env.repo_slug, "--state", "open", "--limit", "100",
                        "--json", "number,url,headRefName,mergeable,createdAt"])
    if rows is None:
        raise RuntimeError("gh pr list (open) failed: cannot see in-flight sns copy PRs")
    return [r for r in rows if str(r.get("headRefName") or "").startswith(BRANCH_PREFIX)]


def housekeeping(env, ledger, args, state, prs, stats):
    """24 時間以上マージされない PR を閉じ、衝突した PR を最新の main から作り直す。残った PR を返す。"""
    alive = []
    now = env.now()
    for pr in prs:
        branch = pr["headRefName"]
        created = wl.parse_ts(pr.get("createdAt"))
        if created and (now - created).total_seconds() >= PR_MAX_AGE_S:
            r = env.gh(["pr", "close", str(pr["number"]), "-R", env.repo_slug, "--delete-branch", "--comment",
                        "24 時間以上マージされなかったため閉じる。"])
            ledger.add("closed", number=pr["number"], branch=branch, ok=r.returncode == 0, reason="stale_24h")
            stats["closed"] += 1
            if r.returncode == 0:
                continue
        if str(pr.get("mergeable") or "").upper() == "CONFLICTING":
            files = pending_files(state, branch)
            if not files:
                stats["errors"] += 1
                ledger.add("error", stage="rebuild", error="pending_missing", branch=branch)
            else:
                try:
                    push_branch(env, args.repo_dir, branch, files, force=True)
                    stats["rebuilt"] += 1
                    ledger.add("rebuild", number=pr["number"], branch=branch, url=pr.get("url"), asins=sorted(files))
                except NothingToCommit:
                    # 全部 main に入った (別の経路で同じ ASIN が足された)。残しても 24 時間ふさぐだけなので閉じる
                    r = env.gh(["pr", "close", str(pr["number"]), "-R", env.repo_slug, "--delete-branch", "--comment",
                                "載せたコピーが全部 main に入ったため閉じる。"])
                    ledger.add("closed", number=pr["number"], branch=branch, ok=r.returncode == 0, reason="all_on_main")
                    stats["closed"] += 1
                    if r.returncode == 0:
                        continue
                except Exception as e:  # noqa: BLE001
                    stats["errors"] += 1
                    ledger.add("error", stage="rebuild", error="rebuild", branch=branch, detail=str(e)[:300])
        alive.append(pr)
    return alive


# ---------------------------------------------------------------- run

def cmd_run(args, env, select_fn=None):
    state = Path(args.state_dir)
    state.mkdir(parents=True, exist_ok=True)
    now = env.now()
    run_id = args.run_id or now.astimezone(JST).strftime("%Y%m%d-%H%M%S")
    ledger = wl.Ledger(state, run_id, env)
    today = wl.jst_day(now)
    stats = {"calls": 0, "tokens": 0, "secs": 0.0, "passed": 0, "failed": 0, "web_discard": 0, "prs": 0,
             "rebuilt": 0, "closed": 0, "emitted": 0, "errors": 0, "quota_stop": False, "gate": "",
             "decision": ""}
    lock = el.RunLock(state, env)
    if not lock.acquire():
        ledger.add("locked", dry_run=args.dry_run)
        print(json.dumps({"run_id": run_id, "locked": True}, ensure_ascii=False))
        return 0
    ledger.add("run_start", dry_run=args.dry_run, backend=args.backend, model=args.model, top=args.top)
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
        source = el.Source(env, args.repo_dir, live)
        prs = []
        if live:
            r = wl.git(env, "fetch", "origin", "main")
            if r.returncode != 0:
                raise RuntimeError("git fetch failed: %s" % (r.stderr or "").strip()[:300])
            prs = housekeeping(env, ledger, args, state, list_own_prs(env), stats)
            r = wl.git(env, "switch", "--discard-changes", "--detach", "origin/main")
            if r.returncode != 0:
                raise RuntimeError("git switch origin/main failed: %s" % (r.stderr or "").strip()[:300])
            wl.git(env, "clean", "-fq", "--", "data/sns_copy")   # 異常終了で残った書きかけを拾わない
            prune_pending(state, prs)
        pending = set()
        for pr in prs:
            pending.update(pending_files(state, pr["headRefName"]))
        if prs:
            stats["decision"] = "skip: open PR exists"
            ledger.add("decision", action="skip", reason="open_pr", count=len(prs))
        elif gate:
            stats["gate"] = gate
            stats["decision"] = "skip: gate"
            ledger.add("gate", reason=gate)
        else:
            targets = choose_targets(args.repo_dir, args.top, pending, source, select_fn)
            ledger.add("decision", action="generate" if targets else "skip", reason="targets" if targets else "no_targets",
                       asins=[t["asin"] for t in targets])
            stats["decision"] = "generate %d" % len(targets) if targets else "skip: no targets"
            blocks, usable, pools = [], [], {}
            for t in targets:
                text = source.read("data/articles/%s" % t["filename"])
                try:
                    article = json.loads(text) if text else None
                except ValueError:
                    article = None
                if not isinstance(article, dict):
                    ledger.add("skip_target", asin=t["asin"], reason="article_unreadable")
                    continue
                usable.append(t)
                blocks.append(article_block(t, article))
                pools[t["asin"]] = article_pool(article)
            if usable:
                prompt_path = Path(args.brain_dir) / PROMPT_FILE
                try:
                    original = prompt_path.read_text(encoding="utf-8")
                except OSError:
                    original = None
                    stats["errors"] += 1
                    ledger.add("error", stage="prompt", error="prompt_missing", detail=prompt_path.name)
                if original is not None:
                    prompt = build_prompt(original, today, blocks)
                    run_dir = state / "runs" / run_id
                    caps = {"calls": calls, "tokens": tokens}
                    got, _flag = run_round(env, ledger, args, state, run_dir, prompt, usable, pools, caps, stats)
                    if not got:
                        ledger.add("short", passed=0, targets=len(usable))
                    else:
                        records = {a: build_record(a, got[a], env.now(), args) for a in sorted(got)}
                        stats["emitted"] = len(records)
                        save(run_dir, "files.json", json.dumps(records, ensure_ascii=False, indent=2))
                        if args.dry_run:
                            ledger.add("pass", dry_run=True, asins=sorted(records))
                        else:
                            try:
                                url, branch = create_pr(env, state, args.repo_dir, wl.read_ledger(state), today, records)
                            except Exception as e:  # noqa: BLE001
                                stats["errors"] += 1
                                ledger.add("error", stage="pr", error="pr", detail=str(e)[:300])
                            else:
                                stats["prs"] += 1
                                ledger.add("pr", url=url, branch=branch, asins=sorted(records))
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
    ap = argparse.ArgumentParser(description="K8 SNS copy lane (navi-brain#170 B2)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--repo-dir", default=".", help="専用の作業ツリー (他のレーンと共用しない)")
    r.add_argument("--brain-dir", required=True, help="%s があるディレクトリ" % PROMPT_FILE)
    r.add_argument("--state-dir", required=True)
    r.add_argument("--backend", choices=["agy", "ollama"], default="agy")
    r.add_argument("--model", default=None, help="省略時は backend の既定 (agy=%s / ollama=%s)" % (wl.DEFAULT_MODEL, OLLAMA_DEFAULT_MODEL))
    r.add_argument("--num-ctx", type=int, default=DEFAULT_NUM_CTX, help="ollama の num_ctx (agy では無視)")
    r.add_argument("--timeout", type=int, default=900)
    r.add_argument("--dry-run", action="store_true", help="PR を出さず、コピーを state-dir に保存するだけ (git / gh を呼ばない)")
    r.add_argument("--top", type=int, default=TOP_DEFAULT, help="対象にする上位の件数")
    r.add_argument("--max-calls", type=int, default=4)
    r.add_argument("--max-tokens", type=int, default=200000)
    r.add_argument("--repo-slug", default="omochairo/amazon")
    r.add_argument("--run-id", default="")
    args = ap.parse_args(argv)
    if not args.model:
        # agy 用の既定を ollama に渡すと HTTP 404 になる (engagement_lane と同じ)
        args.model = wl.DEFAULT_MODEL if args.backend == "agy" else OLLAMA_DEFAULT_MODEL
    env = el.LaneEnv(args.repo_dir, repo_slug=args.repo_slug)
    return cmd_run(args, env)


if __name__ == "__main__":
    sys.exit(main())
