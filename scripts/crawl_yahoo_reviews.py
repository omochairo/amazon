"""crawl_yahoo_reviews.py

Issue #3203 Phase 2 Lane 2: Yahoo!ショッピングのレビューを未ログイン・低速蓄積で
収集し、`EXPERIENCE_RAW_DIR` (既定 `~/.omochairo/yahoo_reviews_raw`) にローカル保存
専用で書き出す。**リポジトリ内パスには絶対に書かない** (原文はサイト非公開・
gemma で言い換えた集合傾向のみを experience.json 経由でリポジトリに出す設計、
docs/article-quality-overhaul-design.md §5.2/§5.3)。

設計条件 (すべて必須):
  1. 未ログイン・自宅回線 (K8/NAS レーン)・夜間スケジュール
  2. 1 リクエスト 15〜30 秒間隔 + ジッター、robots.txt 尊重、正直な UA
     (連絡先 URL 入り)。bot 検知の回避策は実装しない
  3. 対象は監査対象/生成予定 ASIN に限定 (mine_experience.select_targets を流用)、
     日次上限あり (--max-requests, 既定 60)
  4. 原文はランナーのローカル保管のみ。既取得 ASIN は --refresh-days (既定 30) 以内
     なら skip (冪等・蓄積型)

レビューページ URL 導出方式 (2026-07-15 改訂):
  当初の推測規約 (`store.shopping.yahoo.co.jp/<store>/review/<item>.html`) は
  実 URL 検証で 404 だったため廃止。**公式 itemSearch v3 API** (scripts/fetch_yahoo.py
  と同じ https://shopping.yahooapis.jp/ShoppingWebService/V3/itemSearch、env
  `YAHOO_CLIENT_ID`) に `jan_code` (data/raw/per_asin/<ASIN>/amazon.json の
  jan_code フィールド、backfill_jan_codes.py がカバレッジ約 96% で埋めている) を
  渡し、レスポンス `hits[].review` の `{rate (平均), count (件数), url (レビュー
  ページ URL)}` を取得する — 推測ゼロの正規導出。
  - JAN が無い ASIN は warning + skip
  - `YAHOO_CLIENT_ID` 未設定なら lane 全体を warning + skip
  - API 呼び出しは 1 秒間隔 (公式 API なので低速クロール規律の対象外だが節度は守る)
  - review.count == 0 の ASIN はレビューページを crawl しない (無駄リクエスト削減)。
    api_review 自体は保存する (rate/count は API の確定値・refresh-days の冪等にも使う)

レビュー本文の抽出 (2026-09-06 改訂・#6641):
  当初は schema.org の JSON-LD だけを見ていた。「CSS セレクタより markup 変更に
  強い」という理由だったが、**JSON-LD 自体が廃止された**。2026-07-15 〜 09-05 の
  65 ASIN すべてで本文 0 件 (API 側は 402 件あると報告) になっていたのに、
  レーンは success で回り続けていた。

  当時の docstring に書いてあった「JS レンダリング依存で取れない可能性がある」
  という保険が、2 ヶ月ぶん誰も確かめない理由になっていた。実際には **JS 依存では
  なく、本文は最初のレスポンスの HTML に入っている**。

  現在は JSON-LD と DOM の両方を試して取れた方を採る。DOM 側の足場は
  `data-review-id` / `data-review-createtime` (ハッシュ化されないデータ属性) と、
  class 名の**部分一致** — `style_ReviewDetail__reviewBody__uzkwW` の末尾ハッシュは
  Yahoo のビルドごとに変わるので完全一致で書かない。

  それでも将来また置いていかれるので、**「api_review.count > 0 なのに本文 0 件」の
  割合**を summary に出し、EMPTY_BODY_ALERT_RATE を超えたら error を出す。
  セレクタを直すだけでは同じことがまた起きる。

  取れなくてもクラッシュせず api_review だけでも保存するのは変わらない
  (mine_experience 側の rating_stats は api_review 優先で集計)。ただし 0 件の
  ファイルは EMPTY_RETRY_DAYS で早めに取り直す (取得成功ではないため)。
  ページネーションは review.url のページ構造が不明のため 1 ページのみ。

出力 (ローカル raw ファイル):
  {asin, jan_code, fetched_at, api_review: {rate, count, url}, reviews: [...]}

Usage:
    python scripts/crawl_yahoo_reviews.py --limit 20
    python scripts/crawl_yahoo_reviews.py --asins B0XXXXXXXX --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import random
import re
import sys
import time
import urllib.parse
import urllib.robotparser
from datetime import datetime, timezone
from typing import Any

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from mine_experience import select_targets  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("crawl_yahoo_reviews")

PER_ASIN_DIR = pathlib.Path("data/raw/per_asin")
DEFAULT_RAW_DIR = pathlib.Path.home() / ".omochairo" / "yahoo_reviews_raw"
DEFAULT_MAX_REQUESTS = 60
# レビューがあるはずの ASIN のうち、本文が 1 件も取れなかった割合がこれを
# 超えたら「抽出器が壊れている」とみなして error を出す (#6641)。個々の ASIN で
# 0 件になるのは普通に起こる (robots 拒否・ページ構造の例外) ので、1 件では
# 鳴らさず割合で見る。2 ヶ月気づかなかったのは、この比較を誰もしていなかったから。
EMPTY_BODY_ALERT_RATE = 0.8
# 本文 0 件だったファイルを再取得するまでの日数。取得成功と同じ 30 日は待たない
# (抽出器を直しても既存ファイルがふさがるため)。ただし毎晩叩くのは低速クロール
# 規律に反するので間隔は空ける。
EMPTY_RETRY_DAYS = 7
DEFAULT_REFRESH_DAYS = 30
REQUEST_TIMEOUT = 20

# 公式 itemSearch v3 (scripts/fetch_yahoo.py と同じエンドポイント・env 名)。
YAHOO_API_URL = "https://shopping.yahooapis.jp/ShoppingWebService/V3/itemSearch"
API_SLEEP_SECONDS = 1.0  # 公式 API のレート節度 (1 query/sec)

# 連絡先 URL 入りの正直な UA (bot 検知回避策は実装しない設計要件)。
HONEST_UA = "omochairo-experience-bot/1.0 (+https://navi.omcha.jp/)"

_BOT_WALL_MARKERS = (
    "captcha", "recaptcha", "are you a human", "unusual traffic",
    "アクセスが集中", "自動的なアクセス", "自動アクセス",
)


class BotWallDetected(Exception):
    """bot 検知/CAPTCHA に遭遇した。回避策は実装せず、その ASIN を諦める。"""


class RequestBudget:
    """日次上限 (--max-requests) を横断的に消費する簡易カウンタ。

    公式 API (itemSearch v3) の呼び出しは対象外 — レビューページ crawl のみ数える。
    """

    def __init__(self, max_requests: int) -> None:
        self.max_requests = max_requests
        self.consumed = 0

    def exhausted(self) -> bool:
        return self.max_requests > 0 and self.consumed >= self.max_requests

    def try_consume(self) -> bool:
        if self.exhausted():
            return False
        self.consumed += 1
        return True


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load(path: pathlib.Path) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def default_raw_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("EXPERIENCE_RAW_DIR", str(DEFAULT_RAW_DIR)))


# --------------------------------------------------------------------------
# JAN 取得 + itemSearch v3 での review.url 導出 (推測ゼロの正規導出)
# --------------------------------------------------------------------------

def get_jan_code(asin: str, base: pathlib.Path = PER_ASIN_DIR) -> str | None:
    """data/raw/per_asin/<ASIN>/amazon.json の item から jan_code を取り出す。
    無い/空なら None (backfill_jan_codes.py のカバレッジ約 96%)。"""
    data = _load(base / asin / "amazon.json")
    if not isinstance(data, dict):
        return None
    item = data.get("item") if isinstance(data.get("item"), dict) else data
    jan = item.get("jan_code") if isinstance(item, dict) else None
    if isinstance(jan, str) and jan.strip():
        return jan.strip()
    return None


def lookup_yahoo_review(
    jan_code: str, client_id: str,
    session: requests.Session, sleeper=time.sleep,
) -> dict | None:
    """itemSearch v3 を jan_code で叩き、最初の hit の review
    {rate (平均), count (件数), url (レビューページURL)} を返す。
    エラー/hit なし/review なしは None (warning + skip、lane を止めない)。"""
    sleeper(API_SLEEP_SECONDS)  # 公式 API への節度 (1 query/sec)
    try:
        resp = session.get(
            YAHOO_API_URL,
            params={"appid": client_id, "jan_code": jan_code, "results": 1},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as e:
        logger.warning("itemSearch v3 failed for jan=%s: %s — skip", jan_code, e)
        return None
    if resp.status_code != 200:
        snippet = resp.text[:200] if resp.text else ""
        logger.warning("itemSearch v3 error for jan=%s (HTTP %s): %s — skip",
                       jan_code, resp.status_code, snippet)
        return None
    try:
        data = resp.json()
    except ValueError as e:
        logger.warning("itemSearch v3 response not JSON for jan=%s: %s — skip", jan_code, e)
        return None

    hits = data.get("hits") if isinstance(data, dict) else None
    if not isinstance(hits, list) or not hits or not isinstance(hits[0], dict):
        logger.info("itemSearch v3: no hits for jan=%s — skip", jan_code)
        return None
    review = hits[0].get("review")
    if not isinstance(review, dict):
        logger.info("itemSearch v3: hit has no review field for jan=%s — skip", jan_code)
        return None

    try:
        count = int(review.get("count") or 0)
    except (TypeError, ValueError):
        count = 0
    try:
        rate = float(review.get("rate") or 0.0)
    except (TypeError, ValueError):
        rate = 0.0
    url = review.get("url") if isinstance(review.get("url"), str) else ""
    return {"rate": rate, "count": count, "url": url}


# --------------------------------------------------------------------------
# robots.txt
# --------------------------------------------------------------------------

def robots_allowed(url: str, ua: str = HONEST_UA) -> bool:
    """robots.txt を確認する。取得自体に失敗した場合は安全側 (disallow) に倒す。"""
    try:
        parsed = urllib.parse.urlparse(url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(robots_url)
        rp.read()
        return rp.can_fetch(ua, url)
    except Exception as e:  # noqa: BLE001 — robots.txt 取得失敗は安全側 (disallow)
        logger.warning("robots.txt check failed for %s: %s — treating as disallow", url, e)
        return False


# --------------------------------------------------------------------------
# fetch (レート制御・リトライ 1 回まで・bot wall 検知)
# --------------------------------------------------------------------------

def _looks_like_bot_wall(status_code: int, text: str) -> bool:
    if status_code in (403, 429):
        return True
    low = (text or "")[:4000].lower()
    return any(marker in low for marker in _BOT_WALL_MARKERS)


def fetch_with_retry(
    url: str, session: requests.Session, budget: RequestBudget,
    sleeper=time.sleep, rng: random.Random | Any = random,
) -> str | None:
    """1 URL を取得する。リクエスト前に必ず 15〜30 秒 + ジッター sleep する。
    リトライは 1 回まで。bot wall 検知時は BotWallDetected を送出する
    (呼び出し側でその ASIN を諦めて次へ進む)。"""
    for attempt in (1, 2):
        if not budget.try_consume():
            logger.info("request budget exhausted — stopping")
            return None
        sleeper(rng.uniform(15, 30))
        try:
            resp = session.get(url, headers={"User-Agent": HONEST_UA}, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            if attempt == 2:
                logger.warning("fetch failed for %s: %s", url, e)
                return None
            continue
        if _looks_like_bot_wall(resp.status_code, resp.text):
            raise BotWallDetected(url)
        if resp.status_code == 404:
            return None
        if resp.status_code == 200:
            return resp.text
        if attempt == 2:
            logger.warning("fetch failed for %s: HTTP %s", url, resp.status_code)
            return None
    return None


# --------------------------------------------------------------------------
# レビュー抽出 (schema.org JSON-LD, best-effort)
# --------------------------------------------------------------------------

def _parse_review_node(node: dict) -> dict | None:
    body = node.get("reviewBody")
    if not isinstance(body, str) or not body.strip():
        return None
    rating = None
    rr = node.get("reviewRating")
    if isinstance(rr, dict):
        try:
            rating = float(rr.get("ratingValue"))
        except (TypeError, ValueError):
            rating = None
    title = node.get("name") if isinstance(node.get("name"), str) else ""
    posted_at = node.get("datePublished") if isinstance(node.get("datePublished"), str) else ""
    return {"rating": rating, "title": title, "body": body.strip(), "posted_at": posted_at}


def _reviews_from_ldjson_node(node: Any) -> list[dict]:
    out: list[dict] = []
    if isinstance(node, list):
        for n in node:
            out.extend(_reviews_from_ldjson_node(n))
        return out
    if not isinstance(node, dict):
        return out
    graph = node.get("@graph")
    if isinstance(graph, list):
        out.extend(_reviews_from_ldjson_node(graph))
    node_type = node.get("@type")
    if node_type == "Review" or (isinstance(node_type, list) and "Review" in node_type):
        r = _parse_review_node(node)
        if r:
            out.append(r)
    review_field = node.get("review")
    if isinstance(review_field, (list, dict)):
        out.extend(_reviews_from_ldjson_node(review_field))
    return out

def _reviews_from_ldjson(html: str) -> list[dict]:
    """schema.org の JSON-LD から Review を拾う (当初からの経路)。

    2026-09 時点の Yahoo レビューページには **JSON-LD が 1 ブロックも無い**
    (#6641 で実測)。戻る可能性はあるので経路は残す。
    """
    try:
        from bs4 import BeautifulSoup  # type: ignore

        soup = BeautifulSoup(html, "html.parser")
        blocks = [s.string or s.get_text()
                  for s in soup.find_all("script", attrs={"type": "application/ld+json"})]
    except ImportError:
        blocks = re.findall(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html, flags=re.S | re.I,
        )

    reviews: list[dict] = []
    for block in blocks:
        if not block or not block.strip():
            continue
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        reviews.extend(_reviews_from_ldjson_node(data))
    return reviews


# レビュー 1 件のカードは data 属性で括られている。**class 名を足場にしない** —
# `style_ReviewDetail__reviewBody__uzkwW` の末尾ハッシュは Yahoo のビルドごとに
# 変わるので、完全一致で書くと次のデプロイでまた黙って 0 件になる (#6641)。
_REVIEW_CARD_ATTR = "data-review-id"
_CREATETIME_ATTR = "data-review-createtime"
# class を使うところは必ず部分一致 (CSS Modules のハッシュを避ける)
_BODY_CLASS = "ReviewDetail__reviewBody"
_TITLE_CLASS = "ReviewDetail__reviewTitle"
_TIME_CLASS = "ReviewDetail__postedTime"
# aria-label は「5点中4点の評価」の形。星 svg を数えるより読みやすく壊れにくい
_STAR_RE = re.compile(r"(\d+(?:\.\d+)?)\s*点中\s*(\d+(?:\.\d+)?)\s*点")


def _rating_from_aria(text: str) -> float | None:
    m = _STAR_RE.search(text or "")
    if not m:
        return None
    try:
        return float(m.group(2))
    except (TypeError, ValueError):
        return None


def _has_class(tag: Any, name: str, needle: str) -> bool:
    return tag.name == name and needle in " ".join(tag.get("class") or [])


def _reviews_from_dom(html: str) -> list[dict]:
    """サーバレンダリング済み HTML の DOM から Review を拾う (#6641 で追加)。

    #6641 の実測: レビュー本文は JS で後から描かれているのではなく、**最初の
    HTTP レスポンスの HTML に入っている**。JSON-LD が廃止されただけだった。
    元の docstring にあった「JS レンダリング依存で取れない可能性」という保険が、
    2 ヶ月ぶん誰も確かめない理由になっていた。
    """
    try:
        from bs4 import BeautifulSoup  # type: ignore
    except ImportError:
        logger.warning("bs4 が無いため DOM 経路をスキップします")
        return []

    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    for card in soup.find_all(attrs={_REVIEW_CARD_ATTR: True}):
        body_el = card.find(lambda t: _has_class(t, "p", _BODY_CLASS))
        if body_el is None:
            continue
        body = body_el.get_text(" ", strip=True)
        if not body:
            continue

        title_el = card.find(lambda t: _has_class(t, "p", _TITLE_CLASS))
        title = title_el.get_text(" ", strip=True) if title_el else ""

        # 日付は data 属性 (ISO8601) を優先。表示用の "2026/3/20" は
        # 表記が変わりやすい
        posted_at = card.get(_CREATETIME_ATTR) or ""
        if not posted_at:
            time_el = card.find(lambda t: _has_class(t, "p", _TIME_CLASS))
            posted_at = time_el.get_text(strip=True) if time_el else ""

        rating = None
        star = card.find(attrs={"aria-label": _STAR_RE})
        if star is not None:
            rating = _rating_from_aria(star.get("aria-label", ""))

        out.append({"rating": rating, "title": title, "body": body,
                    "posted_at": posted_at})
    return out


def _dedupe_reviews(reviews: list[dict]) -> list[dict]:
    """本文が同じものを 1 件にまとめる (ピックアップと一覧で二重に出る)。"""
    seen: set[str] = set()
    out: list[dict] = []
    for r in reviews:
        key = (r.get("body") or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def extract_reviews(html: str) -> list[dict]:
    """JSON-LD と DOM の両方を試し、**取れた方**を返す。

    片方が Yahoo 側の変更で 0 件になっても、もう片方が生きていれば拾える。
    どちらも取れたら件数の多い方 (#6641 の再発防止)。
    """
    ld = _dedupe_reviews(_reviews_from_ldjson(html))
    dom = _dedupe_reviews(_reviews_from_dom(html))
    if len(dom) > len(ld):
        logger.debug("DOM 経路を採用 (dom=%d > ldjson=%d)", len(dom), len(ld))
        return dom
    return ld


# --------------------------------------------------------------------------
# 冪等・蓄積 (refresh-days)
# --------------------------------------------------------------------------

def is_fresh(path: pathlib.Path, max_age_days: int) -> bool:
    data = _load(path)
    if not isinstance(data, dict):
        return False
    ts = data.get("fetched_at")
    if not isinstance(ts, str):
        return False
    try:
        when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return False
    now = datetime.now(timezone.utc)
    age = (now - when).days

    # 「レビューがあるはずなのに本文 0 件」は取得成功ではない。これを
    # max_age_days (既定 30 日) キャッシュすると、抽出器を直しても既存ファイルが
    # 1 ヶ月間ふさがったままになる (#6641 の 65 件がまさにこれ)。**短い間隔で
    # 取り直す。** ページ側に本当に本文が無い場合もあるので毎晩は叩かない
    api = data.get("api_review")
    api_count = api.get("count") if isinstance(api, dict) else None
    reviews = data.get("reviews")
    if isinstance(api_count, int) and api_count > 0 and not reviews:
        return age < EMPTY_RETRY_DAYS

    return age < max_age_days


# --------------------------------------------------------------------------
# 実行本体
# --------------------------------------------------------------------------

def crawl_asin(
    asin: str, *,
    client_id: str,
    base: pathlib.Path = PER_ASIN_DIR,
    session: requests.Session, budget: RequestBudget,
    sleeper=time.sleep, rng: random.Random | Any = random,
) -> dict | None:
    """1 ASIN 分を収集する。JAN 無し・API 導出不能は None。
    api_review が取れれば、レビュー本文が 0 件でも payload を返す (保存対象)。"""
    jan_code = get_jan_code(asin, base)
    if not jan_code:
        logger.warning("%s: no jan_code in per_asin amazon.json — skip", asin)
        return None

    api_review = lookup_yahoo_review(jan_code, client_id, session, sleeper=sleeper)
    if api_review is None:
        return None

    payload = {
        "asin": asin,
        "jan_code": jan_code,
        "fetched_at": _now_iso(),
        "api_review": api_review,
        "reviews": [],
    }

    # count == 0 は crawl せず skip (無駄リクエスト削減)。api_review は保存する。
    if api_review["count"] <= 0 or not api_review["url"]:
        logger.info("%s: review count=%d url=%r — api_review only, no crawl",
                    asin, api_review["count"], api_review["url"])
        return payload

    review_url = api_review["url"]
    if not robots_allowed(review_url):
        logger.info("%s: robots.txt disallows %s — api_review only", asin, review_url)
        return payload
    if budget.exhausted():
        return payload

    # ページ構造が不明のため 1 ページのみ・本文抽出は best-effort
    # (JS レンダリング依存で取れない可能性あり — 取れなくても api_review は成果)。
    try:
        html = fetch_with_retry(review_url, session, budget, sleeper=sleeper, rng=rng)
    except BotWallDetected:
        logger.warning("%s: bot wall detected at %s — abandoning crawl (no workaround), keeping api_review",
                       asin, review_url)
        return payload
    if html:
        payload["reviews"] = extract_reviews(html)
    return payload


def run(
    targets: list[str], *,
    base: pathlib.Path = PER_ASIN_DIR,
    raw_dir: pathlib.Path | None = None,
    client_id: str | None = None,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    refresh_days: int = DEFAULT_REFRESH_DAYS,
    session: requests.Session | None = None,
    sleeper=time.sleep, rng: random.Random | Any = random,
    dry_run: bool = False,
) -> dict:
    raw_dir = raw_dir or default_raw_dir()
    client_id = client_id if client_id is not None else os.environ.get("YAHOO_CLIENT_ID", "").strip()
    if not client_id and not dry_run:
        # 公式 API キー無しでは正規導出ができない — lane 全体を warning + skip
        logger.warning("YAHOO_CLIENT_ID 未設定 — Yahoo レビュー lane 全体を skip")
        return {"targets": len(targets), "written": 0, "skipped": len(targets), "requests_made": 0}

    session = session or requests.Session()
    budget = RequestBudget(max_requests)
    written = 0
    skipped = 0
    crawled = 0   # api_review.count > 0 だった ASIN 数 (本文が取れるはずだった数)
    empty = 0     # そのうち本文が 1 件も取れなかった数

    for asin in targets:
        if budget.exhausted():
            logger.info("request budget (%d) reached — stopping", max_requests)
            break
        out_path = raw_dir / f"{asin}.json"
        if is_fresh(out_path, refresh_days):
            logger.info("%s: fresh (<%dd) skip", asin, refresh_days)
            continue
        if dry_run:
            logger.info("[dry-run] would crawl %s", asin)
            continue
        payload = crawl_asin(
            asin, client_id=client_id, base=base, session=session, budget=budget,
            sleeper=sleeper, rng=rng,
        )
        if payload is None:
            skipped += 1
            continue
        raw_dir.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        written += 1
        api_count = payload["api_review"]["count"]
        n_bodies = len(payload["reviews"])
        # 「API はあると言っているのに 1 件も取れなかった」を数える。
        # #6641: 抽出器が Yahoo の markup 変更に置いていかれて 2 ヶ月ぶん
        # 0 件だったのに、レーンは緑のままだった。**セレクタを直すだけでは
        # 同じことがまた起きる**ので、比較そのものを残す
        if api_count > 0:
            crawled += 1
            if n_bodies == 0:
                empty += 1
        logger.info("%s: wrote %s (api count=%d, %d review bodies)",
                    asin, out_path, api_count, n_bodies)

    summary = {
        "targets": len(targets), "written": written, "skipped": skipped,
        "requests_made": budget.consumed,
        # crawled = レビューがあるはずの ASIN 数 / empty = そのうち 0 件だった数
        "with_api_reviews": crawled, "empty_bodies": empty,
    }
    if crawled:
        rate = empty / crawled
        summary["empty_body_rate"] = round(rate, 3)
        if rate >= EMPTY_BODY_ALERT_RATE:
            # 個々の ASIN では普通に起こりうる (robots 拒否・ページ構造の例外) ので
            # 1 件では鳴らさない。**割合**で見る
            logger.error(
                "レビューがあるはずの %d ASIN のうち %d 件 (%.0f%%) で本文が 0 件でした。"
                "抽出器が Yahoo の markup 変更に置いていかれている可能性があります (#6641)。",
                crawled, empty, rate * 100)
            summary["extractor_suspect"] = True
    logger.info("done: %s", json.dumps(summary, ensure_ascii=False))
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--asins", default="", help="対象 ASIN をカンマ区切りで明示指定")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--max-requests", type=int, default=DEFAULT_MAX_REQUESTS,
                    help="日次リクエスト上限 (レビューページ crawl のみ・公式 API は対象外)")
    ap.add_argument("--refresh-days", type=int, default=DEFAULT_REFRESH_DAYS)
    ap.add_argument("--dry-run", action="store_true", help="取得せず対象/計画のみ表示")
    args = ap.parse_args()

    asins = [a.strip() for a in args.asins.split(",") if a.strip()] or None
    targets = select_targets(limit=args.limit, asins=asins)
    logger.info("対象 %d ASIN: %s", len(targets), targets)

    run(
        targets,
        max_requests=args.max_requests,
        refresh_days=args.refresh_days,
        dry_run=args.dry_run,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
