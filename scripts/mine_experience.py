"""mine_experience.py

Issue #3203 Phase 2: 体験談供給レーン。K8 LLM ワーカー上で実行され、
Web 横断 (Antigravity CLI / agy) / third-party 本文 / Threads / YouTube (opportunistic) /
Yahoo レビュー低速蓄積 (crawl_yahoo_reviews.py の出力) から体験談 snippet を抽出し、
data/raw/per_asin/<ASIN>/experience.json に書き出す read-mostly スクリプト。

設計: docs/article-quality-overhaul-design.md §5 (Phase 2)

対象 ASIN 選定 (select_targets):
  ① data/analytics/answerability_audit.json の pages[].asin
  ② data/rewrite_queue/*.json の asin
  ③ (あれば) 引数 --asins 明示指定
  上記を順に合成・重複除去し --limit (既定 20) で切る。

ソースアダプタ (各アダプタは candidate テキスト群 [{text, source_type, source_url}]
を返す。**必要な env/secret が無い・API がエラーのときは stderr warning + 空リストで
skip し、他レーンを止めない**):
  - gather_antigravity     : agy (Antigravity CLI, K8 WSL2 ホスト側でファイルベース認証
                              済み) のヘッドレス実行による Web 検索要約。当初は
                              GEMINI_API_KEY + Google Search grounding だったが、無料枠
                              クォータをすぐ使い切る問題が判明したため置換した
                              (owner 判断、2026-07-15)
  - gather_third_party     : per_asin/third_party_sources.json + news.json の URL 本文 fetch
  - gather_threads         : THREADS_ACCESS_TOKEN + keyword_search API
  - gather_youtube_opportunistic : per_asin/youtube.json に既存エントリがある場合のみ字幕取得
  - gather_yahoo_aggregate : EXPERIENCE_RAW_DIR (crawl_yahoo_reviews.py の出力) のローカル生データ

gemma 抽出・検証 (extract_snippets):
  各 candidate テキストを gemma (Ollama /api/generate, format=json, think=false) に渡し、
  商品名/ブランドへの言及 (entailment) を判定した上で、体験談/比較/安全/シーン/不満の
  観点 (aspect) ごとに 60〜160 字の日本語要約 snippet を抽出させる。商品一致しない
  candidate は破棄する。

usable_as の割当はコード側で固定 (gemma に任せない):
  yahoo_review_aggregate / antigravity -> "paraphrase"
  blog / news / threads / youtube      -> "quote"

出力: data/raw/per_asin/<ASIN>/experience.json
  snippets 0 件の ASIN はファイルを書かない (空ファイルで per_asin を汚さない)。

Usage:
    python scripts/mine_experience.py --asins B0XXXXXXXX,B0YYYYYYYY
    python scripts/mine_experience.py --limit 20
    python scripts/mine_experience.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import brand_normalizer  # noqa: E402
from fetch_cross_search import extract_search_keyword  # noqa: E402
from score_per_asin_info import is_search_result_url  # noqa: E402
from self_domain import SELF_DOMAIN_SUFFIXES, is_self_domain  # noqa: E402,F401

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("mine_experience")

PER_ASIN_DIR = pathlib.Path("data/raw/per_asin")
DEFAULT_AUDIT_PATH = pathlib.Path("data/analytics/answerability_audit.json")
DEFAULT_REWRITE_QUEUE_DIR = pathlib.Path("data/rewrite_queue")
DEFAULT_AMAZON_JSON = pathlib.Path("data/raw/amazon.json")
DEFAULT_LIMIT = 20
OUT_NAME = "experience.json"

DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_EXPERIENCE_MODEL = "gemma4:26b-a4b-it-qat"
DEFAULT_EXPERIENCE_RAW_DIR = pathlib.Path.home() / ".omochairo" / "yahoo_reviews_raw"

THREADS_ENDPOINT = "https://graph.threads.net/v1.0/keyword_search"

ANTIGRAVITY_TIMEOUT_S = 120
# agy に --model を渡さないと CLI 側の既定モデルに乗る。既定は agy のバージョン
# 更新で黙って動く (実測: `agy --output-format json` の応答にモデル名は入らない
# ため、production からは何に乗っているか観測できない) ので明示ピンする。
# 値の根拠は scripts/bench_agy_model.py の実測 (docs/ANTIGRAVITY_MODEL_BENCH.md)。
DEFAULT_ANTIGRAVITY_MODEL = "gemini-3.8-flash-low"

# 出典 URL の収集 (#6588 の probe を受けて)。
# 自社ドメインは **必ず除く**。probe で navi.omcha.jp の当該 ASIN 記事そのものと
# omcha.jp が「購入者の口コミ」の出典として返ってきた。自分の書いた記事を自分の
# 記事の根拠に取り込む循環になる。判定は self_domain に共通化してある (#6593)。
# Gemini の検索グラウンディングは実 URL でなく不透明なリダイレクト URL を返す。
# 302 を辿らないと実 URL が分からないので、収集時に解決して保存する。
GROUNDING_REDIRECT_HOST = "vertexaisearch.cloud.google.com"
GROUNDING_REDIRECT_PATH = "/grounding-api-redirect/"
ANTIGRAVITY_MAX_SOURCE_URLS = 6
_URL_RE = re.compile(r"https?://[^\s<>\"'）\]\[|、。]+")

_ASIN_RE = re.compile(r"^B0[A-Z0-9]{8}$")
_YT_ID_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{11})")

REQUEST_TIMEOUT = 20
GEMMA_REQUEST_TIMEOUT = 180
MAX_CANDIDATE_TEXT_LEN = 4000
_MAX_EXTRA_RETRIES = 2  # 初回 + 2 リトライ
_RETRY_SLEEP_SECONDS = 2.0

HONEST_UA = "omochairo-experience-bot/1.0 (+https://navi.omcha.jp/)"

# usable_as のコード側固定割当 (gemma には判定させない)
_USABLE_AS_MAP = {
    "yahoo_review_aggregate": "paraphrase",
    "antigravity": "paraphrase",
    "blog": "quote",
    "news": "quote",
    "threads": "quote",
    "youtube": "quote",
}

EXTRACTION_PROMPT_TEMPLATE = """あなたは商品レビュー記事の素材抽出アシスタントです。

# 商品名
{product_name}

# ブランド
{brand}

# 対象テキスト
{text}

上記テキストが、商品『{product_name}』(ブランド: {brand}) への言及を実際に含むかを判定してください。
含む場合、体験談・比較・安全・シーン・不満のいずれかの観点 (aspect) ごとに、60〜160字の日本語要約
snippet を抽出してください (該当する観点が無ければ省略してよい)。
次の JSON スキーマだけを出力してください (他の説明文は一切含めない):
{{"entailed": true または false, "snippets": [{{"aspect": "体験談|比較|安全|シーン|不満", "text": "60〜160字の日本語要約", "confidence": "high|medium|low"}}]}}
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load(path: pathlib.Path) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _items(data: Any) -> list:
    if isinstance(data, dict):
        v = data.get("items")
        return v if isinstance(v, list) else []
    return data if isinstance(data, list) else []


def _html_to_text(html: str) -> str:
    """本文 HTML → プレーンテキスト。bs4 があれば使い、無ければ正規表現で最小ストリップ。"""
    try:
        from bs4 import BeautifulSoup  # type: ignore

        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = soup.get_text(separator="\n")
    except ImportError:
        text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


# --------------------------------------------------------------------------
# 対象 ASIN 選定
# --------------------------------------------------------------------------

def select_targets(
    limit: int = DEFAULT_LIMIT,
    asins: list[str] | None = None,
    audit_path: pathlib.Path = DEFAULT_AUDIT_PATH,
    rewrite_queue_dir: pathlib.Path = DEFAULT_REWRITE_QUEUE_DIR,
) -> list[str]:
    """① answerability_audit.json ② rewrite_queue/*.json ③ 明示 --asins を合成し
    重複除去したうえで limit (0以下なら無制限) で切る。"""
    ordered: list[str] = []
    seen: set[str] = set()

    def _add(a: Any) -> None:
        if isinstance(a, str) and _ASIN_RE.match(a) and a not in seen:
            seen.add(a)
            ordered.append(a)

    audit = _load(audit_path)
    if isinstance(audit, dict):
        for p in audit.get("pages", []) or []:
            if isinstance(p, dict):
                _add(p.get("asin"))

    if rewrite_queue_dir.is_dir():
        for f in sorted(rewrite_queue_dir.glob("*.json")):
            data = _load(f)
            if isinstance(data, dict):
                _add(data.get("asin"))

    for a in (asins or []):
        _add(a)

    if limit and limit > 0:
        ordered = ordered[:limit]
    return ordered


# --------------------------------------------------------------------------
# 商品名/ブランド解決
# --------------------------------------------------------------------------

def resolve_product_identity(asin: str, base: pathlib.Path = PER_ASIN_DIR) -> tuple[str, str, str]:
    """(title, product_name_query, brand_canonical) を返す。見つからなければ空文字。"""
    item = None
    raw = _load(DEFAULT_AMAZON_JSON)
    if isinstance(raw, dict):
        for i in raw.get("items", []) or []:
            if isinstance(i, dict) and i.get("asin") == asin:
                item = i
                break
    if item is None:
        per = _load(base / asin / "amazon.json")
        if isinstance(per, dict):
            item = per.get("item") if isinstance(per.get("item"), dict) else per

    title = ""
    if isinstance(item, dict) and isinstance(item.get("title"), str):
        title = item["title"]
    if not title:
        return "", "", ""
    try:
        product_name = extract_search_keyword(title)
    except Exception:  # noqa: BLE001 — keyword 抽出失敗は best-effort
        product_name = title
    brand = brand_normalizer.normalize(title).canonical
    return title, product_name, brand


# --------------------------------------------------------------------------
# ソースアダプタ
# --------------------------------------------------------------------------

def build_antigravity_prompt(product_name: str, brand: str) -> str:
    """gather_antigravity が agy に渡すプロンプト。

    bench (scripts/bench_agy_model.py) から同じ文字列を使うために切り出してある。
    ここを変えるとモデル選定の実測前提も変わるので、変更したら bench を回し直す。

    #6588 の probe で決めた形。2 つの指示がどちらも要る:

    - **出典 URL**: 旧プロンプトは出典を求めておらず、このレーンだけ
      `source_url` が空で出所を辿れなかった。
    - **注意点を必ず 1 行**: 出典 URL だけを足すと注意点が押し出される
      (balance 0.56 -> 0.11 と実測)。体験談マイニングは注意点こそが素材の価値
      なので (#3203)、明示して取り戻す。両方入れると現行を上回った
      (balance 1.00 / 注意点 0.67 -> 2.00 / score 0.824 -> 0.912)。

    「原文のまま抜粋しろ」は **書かないこと**。agy が個別ページを開こうとして
    `read_url` が headless で auto-deny され、空応答になる (#6588 実測)。
    """
    return (
        f"Web検索ツールを使って『{product_name} ({brand})』という商品の購入者の"
        "口コミ・評判・使用感を調べ、事実に基づき3〜5行の日本語箇条書きで要約して"
        "ください。**良い点だけでなく、不満・注意点・難点にも必ず1行以上使うこと。**"
        "**各行の末尾に、その内容の出典URLを1つ必ず `出典: <URL>` の形で"
        "付けてください。** 検索結果に出たURLをそのまま書き、URLを作文しないこと。"
        "ファイル操作・コード編集は一切不要です。テキストで直接回答してください。"
    )


def extract_urls(text: str) -> list[str]:
    """本文から URL を重複なく拾う。

    `agy --json-schema` は Web 検索と併用すると無視される (#6588 実測) ので、
    構造化出力には頼れず本文パースになる。markdown リンクの閉じ括弧や日本語の
    句読点を落とす。
    """
    out: list[str] = []
    for raw in _URL_RE.findall(text or ""):
        url = raw.rstrip(").,、。」』>")
        if url and url not in out:
            out.append(url)
    return out


def strip_urls(text: str) -> str:
    """本文から URL を落とす。

    gemma の抽出に渡すのは日本語の本文だけでよい。grounding redirect の URL は
    1 本 300 字超あり、そのまま渡すと入力の大半が URL になる。
    """
    return re.sub(r"[ 	]*(?:出典[:：]\s*)?" + _URL_RE.pattern, "", text or "")


def resolve_source_urls(
    text: str, *, session: requests.Session | None = None,
    max_urls: int = ANTIGRAVITY_MAX_SOURCE_URLS,
) -> list[str]:
    """本文中の URL を実 URL に解決し、使えるものだけ返す。

    落とすもの:
      - 到達しない URL (200 以外)。probe では 13〜19% が **作文**だった。
        実在しそうな形をしているので、叩いて確かめる以外に見分ける手が無い。
      - 自社ドメイン (循環の防止)
      - 検索結果ページ (#5490 案B と同じ理由: 体験談が載っていない)

    ネットワークが死んでいても素材そのものは残したいので、例外は握って
    「その URL を捨てる」だけにする。
    """
    urls = extract_urls(text)
    if not urls:
        return []
    session = session or requests.Session()
    out: list[str] = []
    for url in urls:
        if len(out) >= max_urls:
            break
        try:
            resp = session.get(
                url, timeout=REQUEST_TIMEOUT, allow_redirects=True,
                headers={"User-Agent": HONEST_UA},
            )
        except requests.RequestException as e:
            logger.warning("出典 URL の解決に失敗 (%s): %s — 捨てる", url[:80], e)
            continue
        if resp.status_code != 200:
            logger.warning(
                "出典 URL が %s (%s) — 作文か失効とみなして捨てる",
                resp.status_code, resp.url[:80],
            )
            continue
        final = resp.url
        if is_self_domain(final):
            logger.warning("出典 URL が自社ドメイン (%s) — 循環になるので捨てる", final[:80])
            continue
        if is_search_result_url(final):
            continue
        if final not in out:
            out.append(final)
    return out


def build_antigravity_argv(prompt: str, model: str | None) -> list[str]:
    """agy の argv を組む。

    `agy --print <prompt> --model X` と書くと --model 以降が prompt に食われる
    (omochairo/amazon#6539 で実測)。draft_sns_reply.build_agy_argv と同じく
    **--model を先に置き prompt は --print= に添付する**形にそろえる。
    model が None/空なら --model を付けない (= CLI 既定モデル)。
    """
    argv = ["agy"]
    if model:
        argv += ["--model", model]
    argv.append(f"--print={prompt}")
    return argv


def gather_antigravity(
    product_name: str, brand: str, *, timeout_s: int = ANTIGRAVITY_TIMEOUT_S,
    model: str | None = None, sleeper=time.sleep,
    session: requests.Session | None = None,
) -> list[dict]:
    """Antigravity CLI (`agy`) をヘッドレス実行し、Web 検索に基づく口コミ要約を取得する。

    認証はファイルベース (K8 の WSL2 ホスト側で owner が事前に手動ブラウザ認証を
    完了済み) で完結するため api_key 引数は無い。`agy` が PATH に無い・timeout・
    非ゼロ終了はいずれも warning ログ + 空リストで skip し、他レーンを止めない
    (gather_threads / gather_third_party と同じ graceful-skip 設計)。

    **空応答だけはリトライする** (#6578 の実測):
    agy は exit 0 かつ `status: SUCCESS` のまま最終テキストを返さないことがある。
    エージェント CLI なのでターンは「ツールを呼ばなくなったら終了」で、検索の
    往復が伸びた回に最終メッセージを出さずに終わる経路があるらしい。実行時間は
    その variant の成功時平均より一貫して長かった。
    これを skip で握ると **レーンは緑のまま体験談だけが入って来ない**。失敗が
    集中した商品では既定モデルで 8 回中 6 回この経路に落ちていた。

    リトライを空応答に限るのは、それが実測した失敗モードだから。timeout は
    1 回 120s を積み増すだけで costly、非ゼロ終了は認証・PATH 等リトライで
    直らない類が主なので、どちらも従来どおり 1 回で諦める。
    """
    prompt = build_antigravity_prompt(product_name, brand)
    if model is None:
        model = os.environ.get("ANTIGRAVITY_MODEL", DEFAULT_ANTIGRAVITY_MODEL)
    argv = build_antigravity_argv(prompt, model)

    # 本番 (K8 の Linux ワーカー) では dbus-run-session 経由で叩く。手元検証用の
    # Windows には dbus-run-session が無いので直接叩きにフォールバックする
    # (draft_sns_reply.call_agy と同じ形)。
    cmds = [["dbus-run-session", "--", *argv], argv]

    attempts = _MAX_EXTRA_RETRIES + 1
    for attempt in range(1, attempts + 1):
        result = None
        try:
            for cmd in cmds:
                try:
                    result = subprocess.run(
                        cmd, capture_output=True, text=True,
                        timeout=timeout_s, encoding="utf-8",
                    )
                    break
                except FileNotFoundError:
                    continue
            if result is None:
                logger.warning("agy (Antigravity CLI) が見つかりません — antigravity skip")
                return []
        except subprocess.TimeoutExpired:
            logger.warning("agy 呼び出しが timeout (%ds) — antigravity skip", timeout_s)
            return []

        if result.returncode != 0:
            detail = (result.stderr or "")[:200]
            logger.warning(
                "agy 呼び出しが非ゼロ終了 (code %s): %s — antigravity skip",
                result.returncode, detail,
            )
            return []

        text = (result.stdout or "").strip()
        if text:
            if attempt > 1:
                logger.info("agy が %d 回目の試行で応答 (model=%s)", attempt, model)
            source_urls = resolve_source_urls(text, session=session)
            return [{
                # gemma に渡すのは日本語の本文だけでよい。grounding redirect の
                # URL は 1 本 300 字超あり、残すと入力の大半が URL になる。
                "text": strip_urls(text).strip()[:MAX_CANDIDATE_TEXT_LEN],
                "source_type": "antigravity",
                # このレーンは複数の出典から合成した要約なので、行ごとの帰属を
                # 名乗れない。**偽の精度を持たせない**ために source_url は空のまま
                # にし、出典は集合として source_urls に持つ (usable_as は
                # paraphrase なので短引用の出典表示には使われない)。
                "source_url": "",
                "source_urls": source_urls,
            }]

        if attempt < attempts:
            logger.warning(
                "agy から空応答 (attempt %d/%d, model=%s) — リトライ",
                attempt, attempts, model,
            )
            sleeper(_RETRY_SLEEP_SECONDS)

    logger.warning(
        "agy が %d 回とも空応答 (model=%s) — antigravity skip", attempts, model,
    )
    return []


def gather_third_party(
    asin: str, *, base: pathlib.Path = PER_ASIN_DIR,
    session: requests.Session | None = None,
) -> list[dict]:
    session = session or requests.Session()
    urls: list[tuple[str, str]] = []  # (url, source_type)

    tp = _load(base / asin / "third_party_sources.json")
    if isinstance(tp, dict):
        for s in tp.get("sources", []) or []:
            if isinstance(s, dict) and isinstance(s.get("url"), str) and s["url"]:
                # #5490 案B: 検索結果ページを fetch しても体験談は取れない。収集側は
                # 塞いだが、それ以前の行が store に残っている (2026-08-20 実測 498 行)。
                # 外部への無駄なリクエストにもなるのでここで落とす。
                if is_search_result_url(s["url"]):
                    continue
                urls.append((s["url"], "blog"))

    news = _load(base / asin / "news.json")
    for it in _items(news):
        if isinstance(it, dict) and isinstance(it.get("url"), str) and it["url"]:
            urls.append((it["url"], "news"))

    out: list[dict] = []
    for url, source_type in urls:
        try:
            resp = session.get(url, headers={"User-Agent": HONEST_UA}, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.warning("third_party fetch failed for %s: %s — skip", url, e)
            continue
        text = _html_to_text(resp.text)
        if not text:
            continue
        out.append({
            "text": text[:MAX_CANDIDATE_TEXT_LEN],
            "source_type": source_type,
            "source_url": url,
        })
    return out


def gather_threads(
    product_name: str, *, token: str | None = None,
    session: requests.Session | None = None,
) -> list[dict]:
    token = token if token is not None else os.environ.get("THREADS_ACCESS_TOKEN", "").strip()
    if not token:
        logger.warning("THREADS_ACCESS_TOKEN 未設定 — threads skip")
        return []
    session = session or requests.Session()
    try:
        resp = session.get(
            THREADS_ENDPOINT,
            params={
                "q": product_name,
                "media_type": "TEXT",
                "fields": "id,text,permalink,timestamp",
                "access_token": token,
            },
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code in (400, 401, 403):
            # レスポンス本文 (Meta のエラー JSON は {"error": {"message": ...}} 形式で
            # token 自体は含まない) を先頭 200 字だけログに出し、権限不足の原因
            # (threads_keyword_search 未承認 / scope 不足等) を切り分けやすくする。
            detail = (resp.text or "")[:200]
            logger.warning(
                "Threads keyword_search permission/auth error (HTTP %s): %s — skip",
                resp.status_code, detail,
            )
            return []
        resp.raise_for_status()
        payload = resp.json()
    except requests.RequestException as e:
        logger.warning("Threads keyword_search call failed: %s — skip", e)
        return []
    except ValueError as e:
        logger.warning("Threads keyword_search response not JSON: %s — skip", e)
        return []

    out: list[dict] = []
    for item in (payload.get("data") or []) if isinstance(payload, dict) else []:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        out.append({
            "text": text.strip()[:MAX_CANDIDATE_TEXT_LEN],
            "source_type": "threads",
            "source_url": item.get("permalink") or "",
        })
    return out


def gather_youtube_opportunistic(asin: str, *, base: pathlib.Path = PER_ASIN_DIR) -> list[dict]:
    yt = _load(base / asin / "youtube.json")
    items = _items(yt)
    if not items:
        return []
    try:
        from youtube_transcript_api import YouTubeTranscriptApi  # type: ignore
    except ImportError:
        logger.warning("youtube-transcript-api 未インストール — youtube skip")
        return []

    out: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        video_url = it.get("url")
        if not isinstance(video_url, str):
            continue
        m = _YT_ID_RE.search(video_url)
        if not m:
            continue
        video_id = m.group(1)
        try:
            segments = YouTubeTranscriptApi.get_transcript(video_id, languages=["ja", "en"])
        except Exception as e:  # noqa: BLE001 — 字幕無し等は 1 件失敗として skip
            logger.warning("youtube transcript unavailable for %s: %s — skip", video_id, e)
            continue
        text = " ".join(seg.get("text", "") for seg in segments if isinstance(seg, dict))
        if not text.strip():
            continue
        out.append({
            "text": text.strip()[:MAX_CANDIDATE_TEXT_LEN],
            "source_type": "youtube",
            "source_url": video_url,
        })
    return out


def _yahoo_rating_stats(reviews: list[dict]) -> dict:
    ratings = [
        r.get("rating") for r in reviews
        if isinstance(r, dict) and isinstance(r.get("rating"), (int, float))
    ]
    if not ratings:
        return {"count": 0, "average": 0.0, "distribution": {}}
    distribution: dict[str, int] = {}
    for r in ratings:
        key = str(int(r))
        distribution[key] = distribution.get(key, 0) + 1
    return {
        "count": len(ratings),
        "average": round(sum(ratings) / len(ratings), 2),
        "distribution": distribution,
    }


def gather_yahoo_aggregate(
    asin: str, *, raw_dir: pathlib.Path | None = None, max_reviews: int = 10,
) -> tuple[list[dict], dict]:
    """EXPERIENCE_RAW_DIR/<ASIN>.json (crawl_yahoo_reviews.py の出力) を読む。
    無ければ ([], 空 rating_stats) を返す。生レビュー本文は gemma への入力にのみ使う。

    返す candidate は **最大 1 個** (max_reviews 件のレビューをまとめたもの)。
    1 レビュー 1 candidate ではない — 理由は下のコメント (#6602 P1)。

    rating_stats は **api_review (itemSearch v3 の rate/count 確定値) 優先**。
    distribution はクロールで取れたレビュー本文がある場合のみ機械集計で補う
    (API は分布を返さない)。api_review が無い旧形式は本文集計にフォールバック。"""
    raw_dir = raw_dir or pathlib.Path(
        os.environ.get("EXPERIENCE_RAW_DIR", str(DEFAULT_EXPERIENCE_RAW_DIR))
    )
    data = _load(raw_dir / f"{asin}.json")
    if not isinstance(data, dict):
        return [], {"count": 0, "average": 0.0, "distribution": {}}
    reviews = data.get("reviews")
    reviews = reviews if isinstance(reviews, list) else []

    api_review = data.get("api_review")
    if isinstance(api_review, dict) and isinstance(api_review.get("count"), (int, float)):
        rating_stats = {
            "count": int(api_review.get("count") or 0),
            "average": round(float(api_review.get("rate") or 0.0), 2),
            "distribution": _yahoo_rating_stats(reviews)["distribution"],
        }
    else:
        rating_stats = _yahoo_rating_stats(reviews)

    # **レビューは 1 つの candidate にまとめて gemma に渡す** (#6602 P1)。
    #
    # 以前は 1 レビュー = 1 candidate = gemma 1 コールだった。source_type が
    # `yahoo_review_aggregate`、usable_as が `paraphrase` と「集合として扱う」
    # 宣言をしているのに、実装だけ per-review だった。
    #
    # 2026-09-06 の実測 (4 ASIN x 10 レビュー、gemma 実機):
    #
    #   variant       calls  秒/ASIN  snip/ASIN  不満の割合
    #   per_review     10.0    111.3       13.5   7% (4/54)
    #   batched         1.0     29.0        3.8  20% (3/15)
    #
    # 速さだけでなく **素材の質が変わる**。Yahoo のレビューは 97% が 4 星以上
    # (実測 141 件: 5 星 107 / 4 星 30 / 3 星 3 / 2 星 1) なので、1 件ずつ見せると
    # 賞賛レビューからは賞賛しか出ない。per_review は 4 ASIN 中 3 ASIN で不満を
    # 1 件も拾えなかった。まとめて見せると gemma が少数派の不満を拾える
    # (batched は 4 ASIN すべてで拾った)。#3203 の凡庸化対策として、
    # まとめる方が正しい向き。
    #
    # 副次的に 23-experience-mining の timeout も解ける (5.6 分/ASIN ->
    # 約 1.9 分/ASIN)。ただし**速さは選定理由ではなく結果**。
    picked: list[str] = []
    for r in reviews[:max_reviews]:
        if not isinstance(r, dict):
            continue
        parts = [str(r.get(k, "")).strip() for k in ("title", "body") if r.get(k)]
        text = "\n".join(p for p in parts if p)
        if text:
            picked.append(text)
    if not picked:
        return [], rating_stats

    # 長いレビューが数件あるだけで後続が切り捨てられると、**まとめた意味が
    # 消える** (賞賛の長文だけ残って少数派の不満が落ちる)。1 件あたりの枠を
    # 決めてから連結する。
    per_review_budget = max(200, MAX_CANDIDATE_TEXT_LEN // len(picked))
    joined = "\n\n".join("- " + t[:per_review_budget] for t in picked)

    return [{
        "text": joined[:MAX_CANDIDATE_TEXT_LEN],
        "source_type": "yahoo_review_aggregate",
        "source_url": "",
    }], rating_stats


# --------------------------------------------------------------------------
# gemma 抽出・検証
# --------------------------------------------------------------------------

def extract_snippets(
    candidate: dict, product_name: str, brand: str,
    ollama_url: str, model: str, session: requests.Session,
    sleeper=time.sleep,
) -> list[dict]:
    """1 candidate 分を gemma に投げ、entailment 通過分の snippet 群を返す。
    失敗時は空リスト (1 件の失敗で全体を止めない)。"""
    text = candidate.get("text", "")
    if not text.strip():
        return []
    url = f"{ollama_url.rstrip('/')}/api/generate"
    prompt = EXTRACTION_PROMPT_TEMPLATE.format(product_name=product_name, brand=brand, text=text)
    body = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "think": False,
        "format": "json",
        "keep_alive": "30m",
        "options": {"temperature": 0},
    }

    attempts = _MAX_EXTRA_RETRIES + 1
    for attempt in range(1, attempts + 1):
        try:
            resp = session.post(url, json=body, timeout=GEMMA_REQUEST_TIMEOUT)
            resp.raise_for_status()
            payload = resp.json()
            raw = payload.get("response") if isinstance(payload, dict) else None
            if not isinstance(raw, str) or not raw.strip():
                raise ValueError("empty /api/generate response")
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError("extraction response is not a JSON object")
            break
        except (requests.RequestException, ValueError, json.JSONDecodeError) as e:
            if attempt < attempts:
                logger.warning("extraction call failed (attempt %d/%d): %s", attempt, attempts, e)
                sleeper(_RETRY_SLEEP_SECONDS)
            else:
                logger.error("extraction call failed after %d attempt(s): %s", attempts, e)
                return []
    else:  # pragma: no cover — for/else 到達しない防御
        return []

    if not parsed.get("entailed"):
        return []
    raw_snippets = parsed.get("snippets")
    if not isinstance(raw_snippets, list):
        return []

    usable_as = _USABLE_AS_MAP.get(candidate.get("source_type", ""), "quote")
    out: list[dict] = []
    for s in raw_snippets:
        if not isinstance(s, dict):
            continue
        aspect = s.get("aspect")
        text_out = s.get("text")
        if not isinstance(aspect, str) or not isinstance(text_out, str) or not text_out.strip():
            continue
        out.append({
            "aspect": aspect,
            "text": text_out.strip(),
            "source_type": candidate.get("source_type", ""),
            "source_url": candidate.get("source_url", ""),
            # 集合としての出典 (antigravity レーンのみ非空)。監査と、自社記事を
            # 引いていないかの検出に使う (#6588)
            "source_urls": candidate.get("source_urls", []),
            "usable_as": usable_as,
            "confidence": s.get("confidence") if s.get("confidence") in ("high", "medium", "low") else "medium",
        })
    return out


# --------------------------------------------------------------------------
# 実行本体
# --------------------------------------------------------------------------

def mine_asin(
    asin: str, *,
    base: pathlib.Path = PER_ASIN_DIR,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    model: str = DEFAULT_EXPERIENCE_MODEL,
    session: requests.Session | None = None,
    sleeper=time.sleep,
) -> dict | None:
    """1 ASIN 分の候補収集 + gemma 抽出を行い、experience.json payload を返す
    (snippets 0 件なら None)。"""
    session = session or requests.Session()
    title, product_name, brand = resolve_product_identity(asin, base)
    if not title:
        logger.warning("%s: amazon item not found — skip", asin)
        return None

    candidates: list[dict] = []
    candidates += gather_antigravity(product_name, brand, session=session)
    candidates += gather_third_party(asin, base=base, session=session)
    candidates += gather_threads(product_name, session=session)
    candidates += gather_youtube_opportunistic(asin, base=base)
    yahoo_candidates, rating_stats = gather_yahoo_aggregate(asin)
    candidates += yahoo_candidates

    snippets: list[dict] = []
    for c in candidates:
        snippets += extract_snippets(c, product_name, brand, ollama_url, model, session, sleeper)

    if not snippets:
        return None

    return {
        "asin": asin,
        "generated_at": _now_iso(),
        "model": model,
        "snippets": snippets,
        "rating_stats": {"yahoo": rating_stats},
    }


def write_experience(asin: str, payload: dict, base: pathlib.Path = PER_ASIN_DIR) -> pathlib.Path:
    out_path = base / asin / OUT_NAME
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path


def run(
    targets: list[str], *,
    base: pathlib.Path = PER_ASIN_DIR,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    model: str = DEFAULT_EXPERIENCE_MODEL,
    dry_run: bool = False,
) -> dict:
    session = requests.Session()
    written = 0
    skipped = 0
    for asin in targets:
        if dry_run:
            logger.info("[dry-run] would mine %s", asin)
            continue
        payload = mine_asin(asin, base=base, ollama_url=ollama_url, model=model, session=session)
        if payload is None:
            skipped += 1
            logger.info("%s: 0 snippets — not written", asin)
            continue
        out_path = write_experience(asin, payload, base=base)
        written += 1
        logger.info("%s: wrote %s (%d snippets)", asin, out_path, len(payload["snippets"]))
    summary = {"targets": len(targets), "written": written, "skipped": skipped}
    logger.info("done: %s", json.dumps(summary, ensure_ascii=False))
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--asins", default="", help="対象 ASIN をカンマ区切りで明示指定")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    ap.add_argument("--base", default=str(PER_ASIN_DIR))
    ap.add_argument("--ollama-url", default=os.environ.get("OLLAMA_HOST", DEFAULT_OLLAMA_URL))
    ap.add_argument("--model", default=os.environ.get("EXPERIENCE_MODEL", DEFAULT_EXPERIENCE_MODEL))
    ap.add_argument("--dry-run", action="store_true", help="出力せず stdout にサマリ")
    args = ap.parse_args()

    asins = [a.strip() for a in args.asins.split(",") if a.strip()] or None
    targets = select_targets(limit=args.limit, asins=asins)
    logger.info("対象 %d ASIN: %s", len(targets), targets)

    run(
        targets,
        base=pathlib.Path(args.base),
        ollama_url=args.ollama_url,
        model=args.model,
        dry_run=args.dry_run,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
