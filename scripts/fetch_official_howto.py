"""fetch_official_howto.py

#7958 (#7955 設計2 改訂版 B) — 記事のある ASIN について、メーカー公式の
取扱説明書・あそびかたの URL を取得し ``data/raw/per_asin/<ASIN>/official_howto.json``
に書く。

なぜ要るか:
  #7955/#2686 の実測で、298 本のタイトルが「遊び方」「使い方」を約束しながら
  手順ゼロだった (B0H4PQ29JS はタイトルで約束して手順ゼロのまま順位を落とした)。
  #7957 (A) はタイトル・FAQ で手順を約束してよい条件を
  ``official_howto.json`` の ``url`` の有無に固定した (``scripts/official_howto.py``
  が判定に使う)。このスクリプトはその ``url`` を**実際に取得できたものだけ**書く側。
  ``steps`` (手順の要約) は #7960 (D) が AGY による書き起こしを経て後から足す。
  このスクリプトは ``steps`` / ``reviewed_by`` を絶対に消さない (書き込み時に必ず
  マージする)。

規律 (#7958 本文。すべて必須):
  - **取れた URL だけを記録する**。パターンから推定した URL を書かない
  - バンダイ: ``toy.bandai.co.jp/manuals/?jan_code=<JAN>`` の結果が
    ちょうど 1 件のときだけ記録する。href に含まれる ``id`` を抜いて
    常に安定形 ``pdf.php?id=<id>`` に正規化して保存する
    (実測 2026-09-21: 同じ「ちょうど 1 件」でも商品によって
    ``pdf.php?id=N`` と ``manual.php?id=N&time=..&sig=..`` の**両方**が
    返ってくる。``manual.php`` の署名は期限切れになるため保存せず、
    id だけ引き継いで安定 URL を組み立てる。JAN が空/不一致だと直近 20 件の
    既定一覧が返ってくるため、件数でも既定一覧と区別する)
  - レゴ: 商品名から 4〜6 桁の品番を抜き、
    ``lego.com/ja-jp/service/building-instructions/<品番>`` が 200 かつ
    ページ内に品番の文字列があるときだけ記録する
  - たまごっち: ``data/brand_official.yaml`` の系列キーワードが商品名に
    部分一致し、対応する URL が 200 を返すときだけ記録する
  - 「無し」も ``{"asin", "status": "not_found", "checked_at"}`` で記録し、
    30 日は再問い合わせしない (見つかった記録も同様に 30 日は据え置く)
  - 公式サイトへは 1 ホストあたり 1 秒 1 リクエスト
  - 対象は記事 (``data/articles/*.json``) のある ASIN のみ

Usage:
    python -m scripts.fetch_official_howto --dry-run
    python -m scripts.fetch_official_howto --limit 50
    python -m scripts.fetch_official_howto --asin B0H4PQ29JS,B0HDB8KG6G

Issue: https://github.com/omochairo/amazon/issues/7958 (#7955 #2686 #7960)
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import requests
import yaml

# `python -m scripts.<mod>` (package 形式) と `python scripts/<mod>.py`
# (scripts/ が sys.path に乗る形式) の両方で import できるようにする。
# scripts/ に __init__.py が無く両形式が混在しているため、素の兄弟 import は
# package 形式で ModuleNotFoundError になる (scripts/quality_gate.py と同じ穴、
# #5003)。official_howto.load() を使ってマージ前の既存 steps/reviewed_by を
# 読むため、この import が壊れると本スクリプトが -m 形式で全く起動しなくなる。
try:
    import official_howto
except ModuleNotFoundError:  # package 形式
    from scripts import official_howto  # type: ignore[no-redef]


DEFAULT_ARTICLES_DIR = pathlib.Path("data/articles")
DEFAULT_PER_ASIN_ROOT = pathlib.Path("data/raw/per_asin")
DEFAULT_BRAND_OFFICIAL_YAML = pathlib.Path("data/brand_official.yaml")

# 記事 slug の末尾 ASIN 抽出 (data/articles/2026-05-14-B0F2T9PFS9.json -> B0F2T9PFS9)。
# discover_articles (scripts/audit_query_entailment.py) と同じ規則だが、依存を
# 増やさないためここに複製する (audit_query_entailment は gemma judge 呼び出しの
# 重い import 連鎖を持ち込むため sibling import しない)。
_ASIN_RE = re.compile(r"^B0[A-Z0-9]{8}$")
_SIDECAR_SUFFIXES = (".seo.json", ".enrichment.json", ".quality.json")

# 公式サイトへは「普通のブラウザ」の User-Agent で行く (#7958 本文)。bot 名乗りの
# UA だと一部サイトが弾く実測があるため、他スクリプト (*-bot/1.0 名乗り) とは
# あえて流儀を変える。
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 20
# 1 ホストあたり 1 秒 1 リクエスト (#7958 本文)。
RATE_LIMIT_SECONDS = 1.0
# 見つかった/見つからなかった、どちらの記録も 30 日は再問い合わせしない (#7958)。
RECHECK_DAYS = 30

BANDAI_MANUALS_URL = "https://toy.bandai.co.jp/manuals/"
BANDAI_HOST = "toy.bandai.co.jp"
# manualListContent の 1 件分。head 行 (<li class="head"><dl>...) は <a> を
# 持たないため、このパターンには最初からマッチしない。
_BANDAI_ENTRY_RE = re.compile(
    r'<li[^>]*>\s*<a\s+href="(?P<href>[^"]+)"[^>]*>\s*'
    r"<dl>\s*<dt>(?P<dt>.*?)</dt>\s*<dd>(?P<dd>.*?)</dd>\s*</dl>\s*</a>\s*</li>",
    re.S,
)
# 見出し行・お知らせ行の <dd> テキスト (#7958 本文が明示的に除外を求めている)。
_BANDAI_DENYLIST_DD = {"商品名", "取り扱い説明書一覧を公開しました"}
_BANDAI_TAG_RE = re.compile(r"<[^>]+>")
# エントリの href から id を抜く。実測 (2026-09-21): 同じ「ちょうど 1 件」の filtered
# 結果でも、商品によって pdf.php?id=N (署名なし) と manual.php?id=N&time=..&sig=..
# (署名つき・期限切れ) の**両方**が返ってくる (B0H4PQ29JS は pdf.php、
# B0HDB8KG6G/B0HDB6X1CN は manual.php で返った)。href の形式では判定できないため、
# id だけを抜いて常に安定 URL (pdf.php?id=<id>、#7958 本文が「stable」と明言する形)
# に正規化して保存する。manual.php の time/sig は破棄するので期限切れの心配が無い。
_BANDAI_ID_RE = re.compile(r"[?&]id=(\d+)")
_BANDAI_PDF_URL_FMT = "https://toy.bandai.co.jp/manuals/pdf.php?id={id}"
# 既定 (jan_code 空/不一致) の「最近公開・更新された取扱説明書」一覧は実測で毎回
# 20 件。事故的な複数マッチ (同一 JAN が複数商品に振られる等) はこの規模にまで
# 達しないと考えられるため、件数でも既定一覧と genuine multi-match を区別する
# (#7958: 「デフォルト一覧と誤認しない」ための result-count indicator)。
_DEFAULT_LISTING_MIN_ENTRIES = 10

LEGO_HOST = "www.lego.com"
LEGO_INSTRUCTIONS_URL = "https://www.lego.com/ja-jp/service/building-instructions/{set_number}"
# 4〜6 桁の独立した数字 (前後が数字でない)。レゴの品番はこの範囲に収まる。
_LEGO_SET_NUMBER_RE = re.compile(r"(?<!\d)(\d{4,6})(?!\d)")

TAMAGOTCHI_HOST = "tamagotchi-official.com"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _strip_tags(text: str) -> str:
    return _BANDAI_TAG_RE.sub("", text).strip()


# ---------------------------------------------------------------------------
# 対象 ASIN の収集
# ---------------------------------------------------------------------------

def discover_target_asins(articles_dir: pathlib.Path) -> dict[str, pathlib.Path]:
    """data/articles/*.json から {asin: article_path} を作る (sidecar 除外)。

    同一 ASIN が複数記事に跨る場合は stem が最も新しい (文字列として大きい) もの
    を採用する (rewrite で新旧併存するときの規則。#2711 と同じ流儀)。
    """
    if not articles_dir.exists():
        return {}
    winners: dict[str, str] = {}
    paths: dict[str, pathlib.Path] = {}
    for f in sorted(articles_dir.glob("*.json")):
        if f.name.endswith(_SIDECAR_SUFFIXES):
            continue
        stem = f.stem
        parts = stem.split("-")
        asin = parts[-1] if parts else ""
        if not _ASIN_RE.match(asin):
            continue
        if asin not in winners or stem > winners[asin]:
            winners[asin] = stem
            paths[asin] = f
    return paths


def load_product_info(asin: str, article_path: pathlib.Path, per_asin_root: pathlib.Path) -> dict[str, Any]:
    """ルーティングに要る最小限の商品情報 (name/brand/jan) を集める。

    JAN は記事 JSON ではなく ``data/raw/per_asin/<ASIN>/amazon.json`` の
    ``item.jan_code`` にしかない (#2747 backfill_jan_codes と同じ置き場所)。
    """
    name = ""
    brand = ""
    try:
        data = json.loads(article_path.read_text(encoding="utf-8"))
        product = data.get("product") or {}
        if isinstance(product, dict):
            name = product.get("name_full") or product.get("name") or ""
            brand = product.get("brand") or ""
    except (OSError, ValueError):
        pass

    jan = ""
    amazon_path = per_asin_root / asin / "amazon.json"
    try:
        amazon_data = json.loads(amazon_path.read_text(encoding="utf-8"))
        item = amazon_data.get("item") or {}
        if isinstance(item, dict):
            jan = (item.get("jan_code") or "").strip()
        if not name:
            name = item.get("title") or ""
    except (OSError, ValueError):
        pass

    return {"asin": asin, "name": name, "brand": brand, "jan": jan}


# ---------------------------------------------------------------------------
# レート制限 (1 ホスト 1 秒 1 リクエスト)
# ---------------------------------------------------------------------------

class HostRateLimiter:
    """ホストごとに直近リクエスト時刻を覚え、必要な分だけ待つ。

    テストでは ``sleeper`` に no-op を注入して実待機を避ける
    (scripts/build_wp_navi_link_candidates.py と同じ DI の流儀)。
    """

    def __init__(self, min_interval: float = RATE_LIMIT_SECONDS, sleeper=time.sleep, clock=time.monotonic):
        self._min_interval = min_interval
        self._sleeper = sleeper
        self._clock = clock
        self._last_at: dict[str, float] = {}

    def wait(self, host: str) -> None:
        now = self._clock()
        last = self._last_at.get(host)
        if last is not None:
            elapsed = now - last
            if elapsed < self._min_interval:
                self._sleeper(self._min_interval - elapsed)
        self._last_at[host] = self._clock()


# ---------------------------------------------------------------------------
# バンダイ (JAN)
# ---------------------------------------------------------------------------

def parse_bandai_manual_list(html: str) -> dict[str, Any]:
    """``toy.bandai.co.jp/manuals/?jan_code=`` の応答 HTML を解析する。

    戻り値: ``{"status": ..., "results": [{"url":..., "name":...}, ...]}``
    ``url`` は常に署名なしの安定形 (``pdf.php?id=<id>``) に正規化済み。

    status:
      - ``"zero"``: 「条件に一致する取扱説明書が見つかりませんでした。」のみ
      - ``"single"``: 結果がちょうど 1 件 (記録してよい)
      - ``"multi"``: 結果が複数件、既定一覧とみなせる規模ではない (#7958: 記録しない)
      - ``"default_listing"``: 件数が ``_DEFAULT_LISTING_MIN_ENTRIES`` 以上
        (jan_code が空/不一致のときの既定の直近一覧。**記録しない**)
      - ``"unknown"``: 想定した DOM 構造が見つからない (サイト変更の可能性。
        安全側で記録しない)
    """
    m = re.search(r'class="manualListContent"[^>]*>(.*?)<div class="btnBack"', html, re.S)
    block = m.group(1) if m else html

    entries: list[dict[str, str]] = []
    for em in _BANDAI_ENTRY_RE.finditer(block):
        dd = _strip_tags(em.group("dd"))
        if not dd or dd in _BANDAI_DENYLIST_DD:
            continue
        id_match = _BANDAI_ID_RE.search(em.group("href"))
        if not id_match:
            continue  # 想定外の href 形式は安全側で捨てる (推測 URL を作らない)
        entries.append({"url": _BANDAI_PDF_URL_FMT.format(id=id_match.group(1)), "name": dd})

    if not entries:
        if "err_msg" in block or "見つかりませんでした" in block:
            return {"status": "zero", "results": []}
        return {"status": "unknown", "results": []}

    if len(entries) == 1:
        return {"status": "single", "results": entries}
    if len(entries) >= _DEFAULT_LISTING_MIN_ENTRIES:
        return {"status": "default_listing", "results": entries}
    return {"status": "multi", "results": entries}


def fetch_bandai_howto(
    jan: str, session: requests.Session, limiter: HostRateLimiter,
) -> Optional[dict[str, Any]]:
    """JAN から公式取説を引く。見つからなければ None (呼び出し側が not_found を書く)。

    JAN が空のときは問い合わせない (空 jan は既定の一覧を返し誤一致になる。#7958)。
    """
    jan = (jan or "").strip()
    if not jan:
        return None
    limiter.wait(BANDAI_HOST)
    resp = session.get(BANDAI_MANUALS_URL, params={"jan_code": jan}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    parsed = parse_bandai_manual_list(resp.text)
    if parsed["status"] != "single":
        return None
    result = parsed["results"][0]
    return {
        "publisher": "bandai",
        "kind": "manual_pdf",
        "url": result["url"],
        "official_name": result["name"],
        "matched_by": "jan",
        "matched_key": jan,
    }


# ---------------------------------------------------------------------------
# レゴ (品番)
# ---------------------------------------------------------------------------

def extract_lego_set_numbers(name: str) -> list[str]:
    """商品名から 4〜6 桁の品番候補を抜く。末尾に近いものほど品番らしい実測

    (「レゴ(LEGO) シティ ... 交差点 60304」のように末尾に置かれる) ため、
    末尾側を先に試す順で返す。
    """
    if not isinstance(name, str):
        return []
    candidates = _LEGO_SET_NUMBER_RE.findall(name)
    # 出現順を保ったまま重複を除きつつ、末尾側を先に試す並びにする。
    seen: list[str] = []
    for c in candidates:
        if c not in seen:
            seen.append(c)
    return list(reversed(seen))


def fetch_lego_howto(
    name: str, session: requests.Session, limiter: HostRateLimiter,
) -> Optional[dict[str, Any]]:
    """商品名から品番を抜き、building-instructions ページが 200 かつ品番を
    含むときだけ記録する (#7958)。候補が複数あれば末尾側から順に試す。
    """
    for set_number in extract_lego_set_numbers(name):
        url = LEGO_INSTRUCTIONS_URL.format(set_number=set_number)
        limiter.wait(LEGO_HOST)
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
        except requests.RequestException:
            continue
        if resp.status_code == 200 and set_number in resp.text:
            return {
                "publisher": "lego",
                "kind": "building_instructions",
                "url": url,
                "official_name": None,
                "matched_by": "set_number",
                "matched_key": set_number,
            }
    return None


# ---------------------------------------------------------------------------
# たまごっち (系列キーワード)
# ---------------------------------------------------------------------------

def load_brand_official_config(path: pathlib.Path) -> dict[str, Any]:
    try:
        obj = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return obj if isinstance(obj, dict) else {}


def match_tamagotchi_series(name: str, config: dict[str, Any]) -> Optional[dict[str, str]]:
    """商品名に系列キーワードが部分一致すれば ``{"keyword":..., "url":...}`` を返す。

    対応表に無ければ None (#7958: 対応表に無い商品名は記録しない)。
    """
    if not isinstance(name, str) or not name:
        return None
    series_list = ((config.get("tamagotchi") or {}).get("series")) or []
    for series in series_list:
        if not isinstance(series, dict):
            continue
        url = series.get("url")
        for kw in series.get("keywords") or []:
            if isinstance(kw, str) and kw and kw in name:
                return {"keyword": kw, "url": url}
    return None


def fetch_tamagotchi_howto(
    name: str, config: dict[str, Any], session: requests.Session, limiter: HostRateLimiter,
) -> Optional[dict[str, Any]]:
    match = match_tamagotchi_series(name, config)
    if match is None or not match.get("url"):
        return None
    limiter.wait(TAMAGOTCHI_HOST)
    try:
        resp = session.get(match["url"], timeout=REQUEST_TIMEOUT)
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    return {
        "publisher": "tamagotchi",
        "kind": "howto_page",
        "url": match["url"],
        "official_name": None,
        "matched_by": "series_keyword",
        "matched_key": match["keyword"],
    }


# ---------------------------------------------------------------------------
# ルーティング
# ---------------------------------------------------------------------------

def route_adapter(product: dict[str, Any], config: dict[str, Any]) -> Optional[str]:
    """product (name/brand/jan) から使うアダプタ名を決める。無ければ None。

    たまごっち系列一致をバンダイ判定より先に見る。たまごっちの商品名には
    「バンダイ(BANDAI)」ブランドタグが付くが、あそびかたは
    tamagotchi-official.com にしか無い (#7958 本文の実測: たまごっち
    パラダイスの JAN はバンダイの取説一覧では 0 件)。
    """
    name = product.get("name") or ""
    brand = (product.get("brand") or "").upper()
    name_upper = name.upper()

    if match_tamagotchi_series(name, config) is not None:
        return "tamagotchi"
    if "レゴ" in brand or "LEGO" in brand or "LEGO" in name_upper:
        return "lego"
    if ("バンダイ" in brand or "BANDAI" in brand or "バンダイ" in name or "BANDAI" in name_upper) and product.get("jan"):
        return "bandai"
    return None


# ---------------------------------------------------------------------------
# 書き込み (steps/reviewed_by を絶対に消さない)
# ---------------------------------------------------------------------------

def _is_stale(existing: Optional[dict[str, Any]], recheck_days: int, now: datetime) -> bool:
    """既存レコードが無い、または recheck_days 以上前なら再問い合わせしてよい。"""
    if not isinstance(existing, dict):
        return True
    ts = existing.get("fetched_at") or existing.get("checked_at")
    if not isinstance(ts, str):
        return True
    try:
        checked = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return now - checked >= timedelta(days=recheck_days)


def build_record(
    asin: str, found: Optional[dict[str, Any]], existing: Optional[dict[str, Any]], now_iso: str,
) -> dict[str, Any]:
    """found (アダプタの結果) と existing (旧ファイル) から新しい JSON を組み立てる。

    ``steps`` / ``reviewed_by`` は #7960 (D) がこの後で足す/読むフィールドなので、
    見つかった/見つからなかったに関わらず既存の値を必ず引き継ぐ (#7958 の規律)。
    """
    if found is not None:
        record: dict[str, Any] = {"asin": asin, **found, "fetched_at": now_iso}
    else:
        record = {"asin": asin, "status": "not_found", "checked_at": now_iso}

    if isinstance(existing, dict):
        for key in ("steps", "reviewed_by"):
            if key in existing:
                record[key] = existing[key]
    return record


def write_official_howto(
    asin: str, record: dict[str, Any], per_asin_root: pathlib.Path,
) -> pathlib.Path:
    out_dir = per_asin_root / asin
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "official_howto.json"
    out_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out_path


# ---------------------------------------------------------------------------
# メイン処理
# ---------------------------------------------------------------------------

def process_asin(
    asin: str,
    product: dict[str, Any],
    config: dict[str, Any],
    session: requests.Session,
    limiter: HostRateLimiter,
    per_asin_root: pathlib.Path,
    *,
    recheck_days: int = RECHECK_DAYS,
    now: Optional[datetime] = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """1 ASIN 分の判定・取得・書き込みを行い、サマリ dict を返す (テスト容易化)。"""
    now = now or datetime.now(timezone.utc)
    existing = official_howto.load(asin, per_asin_root)
    if not _is_stale(existing, recheck_days, now):
        return {"asin": asin, "action": "skip_recent"}

    adapter = route_adapter(product, config)
    if adapter is None:
        return {"asin": asin, "action": "skip_no_adapter"}

    if dry_run:
        return {"asin": asin, "action": "dry_run", "adapter": adapter}

    if adapter == "bandai":
        found = fetch_bandai_howto(product.get("jan", ""), session, limiter)
    elif adapter == "lego":
        found = fetch_lego_howto(product.get("name", ""), session, limiter)
    elif adapter == "tamagotchi":
        found = fetch_tamagotchi_howto(product.get("name", ""), config, session, limiter)
    else:  # pragma: no cover - route_adapter が保証する
        found = None

    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    record = build_record(asin, found, existing, now_iso)
    write_official_howto(asin, record, per_asin_root)
    return {"asin": asin, "action": "found" if found else "not_found", "adapter": adapter}


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--articles-dir", type=pathlib.Path, default=DEFAULT_ARTICLES_DIR)
    ap.add_argument("--per-asin-root", type=pathlib.Path, default=DEFAULT_PER_ASIN_ROOT)
    ap.add_argument("--brand-official-yaml", type=pathlib.Path, default=DEFAULT_BRAND_OFFICIAL_YAML)
    ap.add_argument("--limit", type=int, default=0, help="処理する ASIN 数の上限 (0=無制限)")
    ap.add_argument("--asin", default="", help="対象 ASIN をカンマ区切りで明示指定 (省略時は記事のある全 ASIN)")
    ap.add_argument("--recheck-days", type=int, default=RECHECK_DAYS)
    ap.add_argument("--dry-run", action="store_true", help="取得・書き込みをせず計画のみ表示")
    args = ap.parse_args(argv)

    config = load_brand_official_config(args.brand_official_yaml)
    targets = discover_target_asins(args.articles_dir)

    if args.asin:
        wanted = {a.strip().upper() for a in args.asin.split(",") if a.strip()}
        targets = {a: p for a, p in targets.items() if a in wanted}

    asins = sorted(targets)
    if args.limit > 0:
        asins = asins[: args.limit]

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    limiter = HostRateLimiter()

    counts = {"found": 0, "not_found": 0, "skip_recent": 0, "skip_no_adapter": 0, "dry_run": 0}
    for asin in asins:
        product = load_product_info(asin, targets[asin], args.per_asin_root)
        result = process_asin(
            asin, product, config, session, limiter, args.per_asin_root,
            recheck_days=args.recheck_days, dry_run=args.dry_run,
        )
        counts[result["action"]] = counts.get(result["action"], 0) + 1
        print(f"{asin}: {result['action']}" + (f" ({result['adapter']})" if "adapter" in result else ""))

    print(
        f"official_howto (#7958): {len(asins)} ASIN(s) checked — "
        f"found={counts['found']} not_found={counts['not_found']} "
        f"skip_recent={counts['skip_recent']} skip_no_adapter={counts['skip_no_adapter']} "
        f"dry_run={counts['dry_run']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
