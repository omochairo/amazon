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
  - レゴ: 商品名から 4〜6 桁の品番 (ピース数・年式等を除外) を抜き、
    building-instructions ページの応答本文に非空の ``buildingInstructions``
    配列があるときだけ記録する。存在しない品番でも常に HTTP 200 かつ
    ページ内にその数字自体を含む (URL エコー) ため、ステータスと数字の
    文字列一致だけでは判定できない (実測 2026-09-21、レビュー指摘 2026-09-22)
  - たまごっち: ``data/brand_official.yaml`` の系列キーワードが商品名に
    部分一致し、**ブランドがバンダイ** かつ **アクセサリー語 (ケース/カバー/
    フィルム等) を含まない** ときだけ、対応する URL が 200 を返すことを
    確認して記録する (レビュー指摘: GOKEI 等のサードパーティ保護ケースや
    バンダイ純正キャリーケースが系列キーワードだけでは誤って一致していた)
  - 「無し」も ``{"asin", "status": "not_found", "checked_at"}`` で記録し、
    30 日は再問い合わせしない (見つかった記録も同様に 30 日は据え置く)。
    **「見つからなかった」と「取得できなかった (ネットワーク断・想定外の
    ステータス)」は区別する** — 後者は ``AdapterFetchError`` として
    ファイルに一切書き込まず、既存レコードを守る (レビュー指摘: 一時障害で
    found を not_found に格下げしてはいけない)
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
import urllib.error
import urllib.request
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
# レビュー指摘 (2026-09-22, #7958): ピース数・年式・対象年齢などの数字が品番と
# 同じ 4〜6 桁の範囲に収まり、誤って品番候補として拾われる
# (例: 「レゴ クラシック 11717 1000ピース」の 1000)。直後 (空白 0〜1 個を挟んでも
# よい) にこれらの単位が続く数字は候補から除外する。年式 (2000-2099年) も同じ
# 仕組みで弾ける (「年」がこのリストに入っている)。
_LEGO_NON_SET_NUMBER_SUFFIX_RE = re.compile(
    r"^\s?(ピース|pcs|PCS|個|年|歳|才|枚|点|cm|mm|g)", re.IGNORECASE,
)
# building-instructions ページ (SSR で埋め込まれる Apollo cache state の JSON)
# が実際に持つ「結果あり/結果なし」のマーカー文字列。レビュー指摘 (2026-09-22):
# 存在しない品番 (99999 等) でも HTTP 200 を返し、ページ内にその数字自体
# (URL エコー) も含まれるため、``status==200 and set_number in resp.text`` だけ
# では常に真になる false positive だった。実測 (2026-09-21、charCodeAt で
# バックスラッシュの有無まで確認済み) で、実在する品番 (60304) は応答本文に
# ``buildingInstructions":[{`` (中身のある配列) を、存在しない品番 (99999) は
# ``buildingInstructions":[]`` (空配列) を含むことを確認済み
# (scripts/tests/fixtures/lego_building_instructions_{60304,99999}.html)。
_LEGO_MATCH_MARKER = 'buildingInstructions":[{'
_LEGO_NOMATCH_MARKER = 'buildingInstructions":[]'

TAMAGOTCHI_HOST = "tamagotchi-official.com"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _strip_tags(text: str) -> str:
    return _BANDAI_TAG_RE.sub("", text).strip()


class AdapterFetchError(RuntimeError):
    """アダプタが「見つからなかった」のか「取得できなかった」のかを区別するための例外。

    レビュー指摘 (2026-09-22, #7958): ネットワーク断・想定外のステータスコード
    (5xx/403/429 等) を「見つからなかった」(not_found) として書いてしまうと、
    一時的な障害が既存の found レコードを 30 日据え置きの not_found で
    **上書きしてしまう** (steps/reviewed_by は build_record が引き継ぐが、url は
    消える。#7957 のタイトル・FAQ ゲートがこの url の有無を見るため、記事側の
    公開範囲が一時障害だけで縮む)。アダプタはこの例外を投げ、process_asin は
    捕捉して action="error" とし、ファイルには一切書き込まない
    (既存レコードをそのまま残す)。
    """


def _is_bandai_branded(brand: str, amazon_title: str) -> bool:
    """product.brand か、Amazon 商品タイトルの ``[バンダイ(BANDAI)]`` 接頭辞で判定する。

    #7958 レビュー: たまごっちアダプタがサードパーティのアクセサリー
    (GOKEI 保護ケース等) にも系列キーワードで一致してしまう問題の一部。
    ブランドが実際にバンダイであることを要求する (アクセサリー語の除外と併用)。
    """
    if isinstance(brand, str) and ("バンダイ" in brand or "BANDAI" in brand.upper()):
        return True
    if isinstance(amazon_title, str) and amazon_title.strip().startswith("[バンダイ(BANDAI)]"):
        return True
    return False


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
    amazon_title = ""
    amazon_path = per_asin_root / asin / "amazon.json"
    try:
        amazon_data = json.loads(amazon_path.read_text(encoding="utf-8"))
        item = amazon_data.get("item") or {}
        if isinstance(item, dict):
            jan = (item.get("jan_code") or "").strip()
            amazon_title = item.get("title") or ""
        if not name:
            name = amazon_title
    except (OSError, ValueError):
        pass

    # amazon_title (Amazon 商品タイトル、``[バンダイ(BANDAI)]`` のような接頭辞を含む
    # ことがある) はブランド判定の補助に使う (#7958 レビュー: たまごっちアダプタの
    # ブランド確認)。記事側 product.name_full には通常この接頭辞が付かない。
    return {"asin": asin, "name": name, "brand": brand, "jan": jan, "amazon_title": amazon_title}


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

    ネットワーク断・想定外のステータスコード・想定した DOM 構造が見つからない
    (``"unknown"``: サイト変更の可能性) は ``AdapterFetchError`` を投げる。
    これらは「バンダイ公式に取説が無い」ことの確認にならないため、
    not_found として書いてはいけない (#7958 レビュー)。
    """
    jan = (jan or "").strip()
    if not jan:
        return None
    limiter.wait(BANDAI_HOST)
    try:
        resp = session.get(BANDAI_MANUALS_URL, params={"jan_code": jan}, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise AdapterFetchError(f"bandai: request failed for jan={jan}: {e}") from e
    parsed = parse_bandai_manual_list(resp.text)
    if parsed["status"] == "unknown":
        raise AdapterFetchError(f"bandai: unrecognized manualListContent shape for jan={jan}")
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
    末尾側を先に試す順で返す。ピース数・年式・対象年齢など、品番と同じ桁数の
    数字は除外する (「レゴ クラシック 11717 1000ピース」の 1000 は候補にしない。
    #7958 レビュー)。
    """
    if not isinstance(name, str):
        return []
    candidates: list[str] = []
    for m in _LEGO_SET_NUMBER_RE.finditer(name):
        tail = name[m.end():]
        if _LEGO_NON_SET_NUMBER_SUFFIX_RE.match(tail):
            continue
        num = m.group(1)
        if num not in candidates:
            candidates.append(num)
    # 出現順を保ったまま重複を除きつつ、末尾側を先に試す並びにする。
    return list(reversed(candidates))


class _UrllibResponse:
    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text


class UrllibSession:
    """``requests.Session`` の ``get`` と同じ形で urllib を使う最小の代替。

    lego.com は Python ``requests`` からの接続を UA やヘッダに関係なく 403 で
    弾く (2026-09-22 実測: requests は 403、同じ UA の curl / urllib は 200)。
    TLS / 接続層の特徴で判定されているとみられ、ヘッダ調整では回避できなかった。
    レゴのアダプタだけこれを使う (テストは従来どおり偽 session を渡せる)。
    """

    def __init__(self, user_agent: str) -> None:
        self.user_agent = user_agent

    def get(self, url: str, timeout: float = REQUEST_TIMEOUT) -> _UrllibResponse:
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read().decode("utf-8", errors="replace")
                return _UrllibResponse(r.status, body)
        except urllib.error.HTTPError as e:
            return _UrllibResponse(e.code, "")
        except (urllib.error.URLError, OSError) as e:
            raise requests.RequestException(str(e)) from e


def fetch_lego_howto(
    name: str, session: requests.Session, limiter: HostRateLimiter,
) -> Optional[dict[str, Any]]:
    """商品名から品番を抜き、building-instructions ページの応答本文に

    非空の ``buildingInstructions`` 配列があるときだけ記録する (#7958)。
    候補が複数あれば末尾側から順に試す。存在しない品番でも常に HTTP 200 かつ
    ページ内にその数字自体 (URL エコー) を含むため、ステータスと数字の
    文字列一致だけでは判定できない (実測 2026-09-21、レビュー指摘 2026-09-22)。

    ネットワーク断・200/404 以外の応答は ``AdapterFetchError`` を投げる
    (「品番が無い」ことの確認にならないため not_found として書かない)。
    """
    for set_number in extract_lego_set_numbers(name):
        url = LEGO_INSTRUCTIONS_URL.format(set_number=set_number)
        limiter.wait(LEGO_HOST)
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            raise AdapterFetchError(f"lego: request failed for set_number={set_number}: {e}") from e
        if resp.status_code == 404:
            continue  # この品番は存在しない (次の候補を試す)
        if resp.status_code != 200:
            raise AdapterFetchError(
                f"lego: unexpected status {resp.status_code} for set_number={set_number}"
            )
        if _LEGO_MATCH_MARKER in resp.text:
            return {
                "publisher": "lego",
                "kind": "building_instructions",
                "url": url,
                "official_name": None,
                "matched_by": "set_number",
                "matched_key": set_number,
            }
        # NOMATCH マーカーが無い (想定外のページ形状) 場合も、この候補について
        # 確証が持てないだけなので次の候補へ進む (単一候補の不明瞭さは全体の
        # エラーにしない。ネットワーク層の失敗とは区別する)。
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


def _tamagotchi_accessory_words(config: dict[str, Any]) -> list[str]:
    words = ((config.get("tamagotchi") or {}).get("accessory_exclude_words")) or []
    return [w for w in words if isinstance(w, str) and w]


def match_tamagotchi_series(
    name: str, brand: str, amazon_title: str, config: dict[str, Any],
) -> Optional[dict[str, str]]:
    """商品名に系列キーワードが部分一致すれば ``{"keyword":..., "url":...}`` を返す。

    対応表に無ければ None (#7958: 対応表に無い商品名は記録しない)。

    レビュー指摘 (2026-09-22): 系列キーワードだけで判定すると、GOKEI/ミヤビックス/
    PDA工房 の保護ケース・保護フィルムや、バンダイ純正の「おでかけキャリー
    おこじょっち」(B0H1KQQ7J4、キャリーケースでブランドはバンダイ) にも一致して
    しまい、本体の公式あそびかたページを付けてしまう。以下の両方を満たすときだけ
    系列一致とみなす:
      - ブランドがバンダイ (``_is_bandai_branded``) — GOKEI 等の非純正を除外
      - 商品名に ``data/brand_official.yaml`` の ``accessory_exclude_words``
        (ケース/カバー/フィルム/キャリー等) が含まれない — バンダイ純正の
        キャリーケースはこちらで除外する
    """
    if not isinstance(name, str) or not name:
        return None
    if not _is_bandai_branded(brand, amazon_title):
        return None
    accessory_words = _tamagotchi_accessory_words(config)
    if any(w in name for w in accessory_words):
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
    product: dict[str, Any], config: dict[str, Any], session: requests.Session, limiter: HostRateLimiter,
) -> Optional[dict[str, Any]]:
    """系列一致 (ブランド・アクセサリー除外込み) すれば、あそびかたページが
    200 を返すときだけ記録する (#7958)。

    match_tamagotchi_series が None を返す場合は「対応表に無い/対象外」の
    確定 not_found。ページ取得のネットワーク断・非 200 は ``AdapterFetchError``
    (系列一致済みの URL なので、取得できないのは一時障害の可能性が高い)。
    """
    match = match_tamagotchi_series(
        product.get("name", ""), product.get("brand", ""), product.get("amazon_title", ""), config,
    )
    if match is None or not match.get("url"):
        return None
    limiter.wait(TAMAGOTCHI_HOST)
    try:
        resp = session.get(match["url"], timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        raise AdapterFetchError(f"tamagotchi: request failed for url={match['url']}: {e}") from e
    if resp.status_code != 200:
        raise AdapterFetchError(f"tamagotchi: unexpected status {resp.status_code} for url={match['url']}")
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
    brand_raw = product.get("brand") or ""
    brand = brand_raw.upper()
    name_upper = name.upper()

    if match_tamagotchi_series(name, brand_raw, product.get("amazon_title") or "", config) is not None:
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
    lego_session: Any = None,
) -> dict[str, Any]:
    """1 ASIN 分の判定・取得・書き込みを行い、サマリ dict を返す (テスト容易化)。

    ``lego_session`` を渡さなければ ``session`` を使う (UrllibSession の理由は同クラス参照)。
    """
    now = now or datetime.now(timezone.utc)
    existing = official_howto.load(asin, per_asin_root)
    if not _is_stale(existing, recheck_days, now):
        return {"asin": asin, "action": "skip_recent"}

    adapter = route_adapter(product, config)
    if adapter is None:
        return {"asin": asin, "action": "skip_no_adapter"}

    if dry_run:
        return {"asin": asin, "action": "dry_run", "adapter": adapter}

    try:
        if adapter == "bandai":
            found = fetch_bandai_howto(product.get("jan", ""), session, limiter)
        elif adapter == "lego":
            found = fetch_lego_howto(product.get("name", ""), lego_session or session, limiter)
        elif adapter == "tamagotchi":
            found = fetch_tamagotchi_howto(product, config, session, limiter)
        else:  # pragma: no cover - route_adapter が保証する
            found = None
    except (AdapterFetchError, requests.RequestException) as e:
        # レビュー指摘 (2026-09-22, #7958): 一時的な取得失敗は「見つからなかった」
        # ではない。ファイルには一切書き込まず (既存の found レコードを
        # not_found で上書きしない)、呼び出し側 (main) がエラーとして数える。
        return {"asin": asin, "action": "error", "adapter": adapter, "error": str(e)}

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
    lego_session = UrllibSession(USER_AGENT)
    limiter = HostRateLimiter()

    counts = {"found": 0, "not_found": 0, "skip_recent": 0, "skip_no_adapter": 0, "dry_run": 0, "error": 0}
    for asin in asins:
        product = load_product_info(asin, targets[asin], args.per_asin_root)
        result = process_asin(
            asin, product, config, session, limiter, args.per_asin_root,
            recheck_days=args.recheck_days, dry_run=args.dry_run,
            lego_session=lego_session,
        )
        counts[result["action"]] = counts.get(result["action"], 0) + 1
        suffix = f" ({result['adapter']})" if "adapter" in result else ""
        if result["action"] == "error":
            suffix += f": {result.get('error', '')}"
        print(f"{asin}: {result['action']}{suffix}")

    print(
        f"official_howto (#7958): {len(asins)} ASIN(s) checked — "
        f"found={counts['found']} not_found={counts['not_found']} "
        f"skip_recent={counts['skip_recent']} skip_no_adapter={counts['skip_no_adapter']} "
        f"dry_run={counts['dry_run']} error={counts['error']}"
    )

    exit_code = _exit_code_for_counts(counts)
    if exit_code != 0:
        attempted = counts["found"] + counts["not_found"] + counts["error"]
        print(
            f"::error::official_howto (#7958): error rate {counts['error']}/{attempted} "
            "exceeds 50% — treating as systemic failure"
        )
    return exit_code


# エラー率の閾値 (レビュー指摘 2026-09-22, #7958)。「実際に取得を試みた」件数
# (found + not_found + error) のうち error がこの割合を超えたときだけ非ゼロ
# 終了する。単発の一時エラー (1〜2 件) は週次 cron を赤くしない — サイト側の
# 瞬断で毎週アラートが飛ぶのは無駄で、次回の 30 日据え置きガード外の ASIN で
# 自然に再試行される。一方、バンダイ/レゴ/たまごっちのホストが軒並み
# ブロック/ダウンしているような系統的な障害は赤くして気付けるようにする。
_ERROR_RATE_FAILURE_THRESHOLD = 0.5


def _exit_code_for_counts(counts: dict[str, int]) -> int:
    """attempted (found+not_found+error) が無ければ 0。error 比率が閾値超なら 1。"""
    attempted = counts.get("found", 0) + counts.get("not_found", 0) + counts.get("error", 0)
    if attempted > 0 and counts.get("error", 0) / attempted > _ERROR_RATE_FAILURE_THRESHOLD:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
