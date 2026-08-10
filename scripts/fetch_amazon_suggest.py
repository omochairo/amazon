"""fetch_amazon_suggest.py

Issue #2686 PR-B「Amazon サジェストによる需要源の収集」の収集スクリプト。

背景 (実測済み):
  navi の需要源は現在 omcha.jp (WP本家) の GSC のみだが、navi.omcha.jp は
  omcha.jp のサブドメインで Google の host crowding 対象。WP が既に上位で
  稼いでいる語を navi でも狙うとカニバる (#4884 PR で順位ガードを入れて対処
  済みだが、根本的には「WP と別空間の需要源」が要る)。

  Amazon サジェスト (completion.amazon.co.jp) が本命な理由:
    1. WP GSC と完全に別空間の需要源なので、そもそもカニバリ問題が起きない
       (順位ガードのような後付けの緩和策が不要)。
    2. サジェストに出る語は Amazon に在庫がある語なので、既存の供給 probe
       (scripts/probe_demand_supply.py で hits==0 を落とす工程) が構造的に
       不要になる。
    3. 検索意図が最初から購買型。WP GSC 由来で必要だった「情報クエリの除去」
       (#4883, 98→90語) の工程が要らない。

  弱点 (要明記): サジェスト API は検索ボリュームの絶対値を返さない。代理
  指標として使えるのは (a) 候補の出現順位 (suggestions[].value の配列順 =
  rank、1始まり)、(b) 複数 seed / 複数 depth から重複して出てくる頻度、の
  2つだけ。「ボリュームが高い」という主張はこのスクリプトの出力からは
  できない — 消費側 (後続 PR) はこの前提で設計すること。

API (実測済み。キー不要):
  GET https://completion.amazon.co.jp/api/2017/suggestions
      ?limit=11&prefix=<query>&suggestion-type=KEYWORD&page-type=Gateway
      &alias=aps&site-variant=desktop&version=3&event=onKeyPress
      &mid=A1VC38T7YXB528&lop=ja_JP&client-info=amazon-search-ui
  mid=A1VC38T7YXB528 は amazon.co.jp のマーケット ID。

  alias パラメータは実測で以下の通り (2026-08-10, 同一 prefix での比較):

    | prefix        | alias=aps (全カテゴリ)             | alias=toys        |
    |---------------|------------------------------------|--------------------|
    | "1歳 おもちゃ" | 10件 (女の子/男の子/ティッシュ/車/ボール落とし…) | 1件 (自身のみ) |
    | "ボーネルンド" | 10件 (おもちゃ/アクアプレイ/ボール/ラッパ/マグフォーマー…) | 3件 |
    | "4M"          | 10件                                | 1件 (4枚組)        |
    | "All Bright"  | 9件                                 | 1件 (ソニッケアー) |

  初期実装では「toys の方がおもちゃカテゴリに絞れて精度が上がる」という
  直感で alias=toys をデフォルトにしていたが、上の実測でこれは誤りだったと
  判明した。toys は候補数が極端に少ない別系列を返すだけで、カテゴリ精度が
  上がるわけではない。**デフォルトは aps (全カテゴリ) に変更**した。
  --alias オプション自体は残す (絞り込みが有効な prefix もありうるため)。

  レスポンス形状 (実測、fetch_google_suggest / fetch_suggest_info が使う
  OpenSearch 形式 `[query, [...]]` とは別物):
    {"alias": "aps", "prefix": "...", "suffix": "",
     "suggestions": [
       {"suggType": "KeywordSuggestion", "type": "KEYWORD", "value": "...",
        "refTag": "...", "candidateSources": "local", "strategyId": "...",
        "strategyApiType": "RANK", "prior": 0.0, "ghost": false, "help": false},
       ...
     ]}
  suggestions[].type は "KEYWORD" 以外 (カテゴリ絞り込み等) が混ざりうるため
  type=="KEYWORD" のみ採用する。配列順が表示順 = rank (1始まり)。

シード選定 (--seeds brands|themes|all、既定 all):
  ブランド名だけを種にすると (a) 供給側リスト (data/brand_taxonomy.yaml) を
  掘り直しているだけで新しい需要発見にならない、(b) "4M" "All Bright" の
  ような英語一般語のブランドは無関係な候補 (後述の関連性フィルタ参照) を
  多く返す、という2つの問題がある。そのため2種類の seed 源を両方使う:

  1. ブランド seed (data/brand_taxonomy.yaml の canonical、
     exclude_from_taxonomy=true の「ノーブランド」を除く294件)。固有名詞
     起点の購買型サジェストを引き出しやすい。
  2. テーマ seed (scripts/fetch_suggest_info.py が使う年齢×カテゴリ hub
     テーマ定義 data/suggest_info_seeds.yaml)。fetch_suggest_info.py の
     出力 (data/raw/suggest_info/) は N2 Lane A の別目的で専有されているが、
     それは「同じテーマ語を別の API (Amazon) にも投げること」を妨げる理由
     にはならない — 本スクリプトは data/suggest_info_seeds.yaml を読むだけ
     で data/raw/suggest_info/ には一切書き込まない (出力は常に
     data/raw/amazon_suggest/)。テーマ label 単体 ("1歳" 等) は短すぎて
     購買意図が弱いため、既存の suggest_info_seeds.yaml の expand_modifier
     ("おもちゃ") と結合し "<label> おもちゃ" の形にする (実測根拠:
     "1歳 おもちゃ" (alias=aps) は10件すべて購買意図の修飾語展開だった、
     上の alias 実測表を参照)。seed_key は theme["key"] をそのまま使う
     (既に ascii、fetch_suggest_info.py の出力ファイル名と同じ命名規則)。

  ブランド seed の seed_key (出力ファイル名) は data/term_slugs.yaml の
  既存スラッグを**読み取り専用**で使う (scripts/term_slug.TermSlugMap.get())。
  実測で294ブランド全件が既に登録済み (ensure_tag_slugs.py が push 毎に
  brand_taxonomy.yaml の全 canonical をスキャンして登録しているため)。
  未登録ブランドがあった場合のみ、ローカルな fallback slug (NFKC+lower+
  非ascii置換、全滅時は sha1 先頭10桁) を使う (term_slugs.yaml への書き込みは
  行わない — 別レーンの永続化責務に副作用を持ち込まない)。

展開:
  各 seed について prefix=<seed> と prefix=<seed>+半角スペース の2リクエスト
  (depth=1、既定でここまで)。ブランド seed に限り、"<seed> おもちゃ" という
  カテゴリ語つき prefix も追加で1リクエスト投げる (テーマ seed は元々
  "<label> おもちゃ" の形なので不要)。これは "All Bright" のような一般語
  ブランドの曖昧性を "All Bright おもちゃ" で解消できないか試すため。
  --depth を上げると、直前の深さで新たに得た候補語を frontier として同様に
  展開する (ブランド用のカテゴリ語つき variant は最初の深さのみ)。深さを
  上げるとリクエスト数が指数的に増えるため、--max-requests で run 全体の
  総リクエスト数に必ずハード上限を掛ける。

関連性フィルタ (実測: "4M" → "4枚組" のような無関係な候補が返るため必須):
  正規化 (NFKC + lower + 空白**除去**、rank マージに使う google_suggest.normalize
  の「空白圧縮」より厳しい — 単語順の揺れではなく空白の有無だけを吸収する
  ためあえて全除去にしている) した候補文字列が、正規化した seed 自身を
  部分文字列として含まない場合は落とす。落とした件数は握り潰さず出力 JSON の
  "dropped_irrelevant" に記録する。

レート制御・エラー処理 (fetch_google_suggest.py の流儀を踏襲):
  リクエスト1件ごとに uniform(--sleep-min, --sleep-max) 秒 sleep する。
  HTTP 非200・接続エラーで収集ループ**全体**を打ち切り、それまでの部分成果は
  保存して exit 0 する。JSON パース失敗 (想定外レスポンス形状) も同様に
  ループ全体を打ち切る扱いとした — PR-B の指示は HTTP エラーのみ明記して
  いるが、パース失敗は「レスポンス形状が実測と食い違っている」異常事態であり
  HTTP エラーと同程度に保守的に倒す方が安全と判断した (fetch_google_suggest.py
  の「3連続で打ち切り」ほど寛容にはしていない、との相違点として明記する)。

出力:
  data/raw/amazon_suggest/<seed_key>.json:
    {"seed": "...", "alias": "aps", "fetched_at": "<ISO8601 UTC>",
     "dropped_irrelevant": 2,
     "suggestions": [{"query": "...", "rank": 1, "prefix": "...", "depth": 1}, ...]}
  data/raw/suggest/ (Google・ASIN起点) や data/raw/suggest_info/ (テーマ×修飾語、
  fetch_suggest_info.py 専用) とは別ディレクトリ (混ぜない・書き込まない)。
  同一クエリが複数 prefix / depth から得られた場合は最小 rank (=最も上位表示)
  を残す。

再取得制御:
  data/raw/amazon_suggest/<seed_key>.json が存在しない seed を最優先、存在する
  場合は fetched_at が --min-age-days より古いものだけを対象にして fetched_at
  昇順 (古い/未取得ほど先) に並べ、--limit 件まで処理する (ブランド seed と
  テーマ seed をまとめた単一の優先度キューとして扱う)。

呼び出し元:
  本 PR は inert — CI / cron からは呼ばない (収集ロジックと unit test のみ)。
  消費側 (需要語としての採用判定) は後続 PR の範囲。

Issue: https://github.com/omochairo/amazon/issues/2686 (PR-B)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import pathlib
import random
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
import yaml

from scripts import fetch_google_suggest as google_suggest
from scripts import fetch_suggest_info as suggest_info
from scripts.term_slug import TermSlugMap

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("fetch_amazon_suggest")

SUGGEST_URL = "https://completion.amazon.co.jp/api/2017/suggestions"
DEFAULT_TAXONOMY = "data/brand_taxonomy.yaml"
DEFAULT_SEEDS_FILE = "data/suggest_info_seeds.yaml"
DEFAULT_OUT_DIR = "data/raw/amazon_suggest"
DEFAULT_ALIAS = "aps"
DEFAULT_MID = "A1VC38T7YXB528"
KEYWORD_TYPE = "KEYWORD"

SEED_KIND_BRAND = "brand"
SEED_KIND_THEME = "theme"
SEEDS_MODES = ("brands", "themes", "all")
DEFAULT_SEEDS_MODE = "all"

# ブランド seed だけに追加するカテゴリ語つき variant (曖昧性解消の実測根拠は
# docstring 参照)。テーマ seed は元々 "<label> おもちゃ" の形なので不要。
BRAND_CATEGORY_HINT = "おもちゃ"

_FALLBACK_SLUG_RE = re.compile(r"[^a-z0-9]+")
_WHITESPACE_RE = re.compile(r"\s+")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso(now: datetime | None = None) -> str:
    return (now or _now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


# --------------------------------------------------------------------------
# シード選定
# --------------------------------------------------------------------------

def _fallback_seed_key(term: str) -> str:
    """term_slugs.yaml に未登録の場合の filename-safe fallback。書き込みはしない。"""
    ascii_only = _FALLBACK_SLUG_RE.sub("-", unicodedata.normalize("NFKC", term).lower()).strip("-")
    if ascii_only:
        return ascii_only
    return "brand-" + hashlib.sha1(term.encode("utf-8")).hexdigest()[:10]


def load_brand_seed_candidates(
    taxonomy_path: pathlib.Path,
    slug_map: TermSlugMap | None = None,
) -> list[tuple[str, str]]:
    """data/brand_taxonomy.yaml から (seed, seed_key) の一覧を作る (canonical 昇順)。

    exclude_from_taxonomy=true (例: 「ノーブランド」) は除外する。seed_key は
    data/term_slugs.yaml の既存スラッグを読み取り専用で参照し、無ければ
    _fallback_seed_key を使う。
    """
    data = yaml.safe_load(taxonomy_path.read_text(encoding="utf-8")) or {}
    canonicals = sorted({
        entry["canonical"]
        for entry in data.get("brands", []) or []
        if entry.get("canonical") and not entry.get("exclude_from_taxonomy")
    })
    slug_map = slug_map or TermSlugMap()
    out: list[tuple[str, str]] = []
    for canonical in canonicals:
        key = slug_map.get(canonical) or _fallback_seed_key(canonical)
        out.append((canonical, key))
    return out


def load_theme_seed_candidates(seeds_file: pathlib.Path) -> list[tuple[str, str]]:
    """data/suggest_info_seeds.yaml の年齢×カテゴリ hub テーマから (seed, seed_key) を作る。

    seed = "<theme label> <expand_modifier>" (例: "1歳 おもちゃ")。
    seed_key = theme["key"] をそのまま使う (既に ascii)。
    """
    config = suggest_info.load_seed_config(seeds_file)
    expand_modifier = config["expand_modifier"]
    out: list[tuple[str, str]] = []
    for theme in config["themes"]:
        key = theme.get("key")
        label = theme.get("label")
        if not key or not label:
            continue
        out.append((f"{label} {expand_modifier}", key))
    return out


def load_seed_candidates(
    taxonomy_path: pathlib.Path,
    seeds_file: pathlib.Path,
    seeds_mode: str = DEFAULT_SEEDS_MODE,
    slug_map: TermSlugMap | None = None,
) -> list[tuple[str, str, str]]:
    """--seeds brands|themes|all に応じて (seed, seed_key, kind) の一覧を作る。"""
    if seeds_mode not in SEEDS_MODES:
        raise ValueError(f"seeds_mode must be one of {SEEDS_MODES}, got {seeds_mode!r}")
    out: list[tuple[str, str, str]] = []
    if seeds_mode in ("brands", "all"):
        for seed, key in load_brand_seed_candidates(taxonomy_path, slug_map):
            out.append((seed, key, SEED_KIND_BRAND))
    if seeds_mode in ("themes", "all"):
        for seed, key in load_theme_seed_candidates(seeds_file):
            out.append((seed, key, SEED_KIND_THEME))
    return out


def select_targets(
    seeds: list[tuple[str, str, str]],
    out_dir: pathlib.Path,
    limit: int,
    min_age_days: int,
    now: datetime | None = None,
) -> list[tuple[str, str, str]]:
    """この run で収集すべき (seed, seed_key, kind) を返す。

    順序: data/raw/amazon_suggest/<seed_key>.json が無いものを最優先 (epoch 0
    扱い)、存在するものは fetched_at 昇順。fetched_at が --min-age-days より
    新しい (= まだ新鮮) seed は対象から除外する。--limit 件で cap
    (ブランド seed とテーマ seed をまとめた単一の優先度キューとして扱う)。
    """
    now = now or _now()
    cutoff = now - timedelta(days=min_age_days)
    epoch = datetime.fromtimestamp(0, tz=timezone.utc)

    candidates: list[tuple[datetime, str, str, str]] = []
    for seed, seed_key, kind in seeds:
        out_path = out_dir / f"{seed_key}.json"
        if not out_path.exists():
            candidates.append((epoch, seed, seed_key, kind))
            continue
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            candidates.append((epoch, seed, seed_key, kind))
            continue
        fetched_at = _parse_iso(existing.get("fetched_at")) if isinstance(existing, dict) else None
        if fetched_at is None:
            candidates.append((epoch, seed, seed_key, kind))
            continue
        if fetched_at > cutoff:
            continue  # まだ新鮮なのでスキップ
        candidates.append((fetched_at, seed, seed_key, kind))

    candidates.sort(key=lambda x: x[0])
    return [(seed, key, kind) for _, seed, key, kind in candidates[:limit]]


# --------------------------------------------------------------------------
# 収集元フェッチ
# --------------------------------------------------------------------------

def _build_params(prefix: str, alias: str) -> dict[str, Any]:
    return {
        "limit": 11,
        "prefix": prefix,
        "suggestion-type": KEYWORD_TYPE,
        "page-type": "Gateway",
        "alias": alias,
        "site-variant": "desktop",
        "version": 3,
        "event": "onKeyPress",
        "mid": DEFAULT_MID,
        "lop": "ja_JP",
        "client-info": "amazon-search-ui",
    }


def _extract_keyword_values(payload: Any) -> list[str]:
    """completion.amazon.co.jp/api/2017/suggestions のレスポンスから

    type=="KEYWORD" の value だけを配列順 (=表示順) のまま抽出する。
    """
    if not isinstance(payload, dict):
        raise google_suggest.SuggestParseError("unexpected amazon suggest response shape (not an object)")
    suggestions = payload.get("suggestions")
    if not isinstance(suggestions, list):
        raise google_suggest.SuggestParseError("unexpected amazon suggest response shape (no suggestions list)")
    out: list[str] = []
    for item in suggestions:
        if not isinstance(item, dict) or item.get("type") != KEYWORD_TYPE:
            continue
        value = item.get("value")
        if isinstance(value, str) and value:
            out.append(value)
    return out


def fetch_suggestions_for_prefix(
    prefix: str, session: requests.Session, alias: str = DEFAULT_ALIAS,
) -> list[str]:
    """prefix 1 件分の Amazon サジェストを取得する。

    HTTP 非200は google_suggest.NonOkStatusError、接続エラーは
    requests.RequestException、JSON パース失敗/想定外形状は
    google_suggest.SuggestParseError を送出する (fetch_google_suggest.py /
    fetch_suggest_info.py と同じ例外分岐に揃える)。
    """
    resp = session.get(
        SUGGEST_URL,
        params=_build_params(prefix, alias),
        headers={"User-Agent": google_suggest.USER_AGENT},
        timeout=google_suggest.REQUEST_TIMEOUT,
    )
    if resp.status_code != 200:
        raise google_suggest.NonOkStatusError(resp.status_code)
    resp.encoding = "utf-8"
    try:
        payload = json.loads(resp.text)
    except json.JSONDecodeError as e:
        raise google_suggest.SuggestParseError(str(e)) from e
    return _extract_keyword_values(payload)


# --------------------------------------------------------------------------
# rank マージ・関連性フィルタ
# --------------------------------------------------------------------------

def _rank_and_merge(fetches: list[tuple[str, list[str]]], depth: int) -> list[dict]:
    """同一 depth 内の複数 prefix (seed / seed+空白 等) の結果をマージする。

    正規化 (NFKC+lower+空白圧縮、google_suggest.normalize) が同じ query が
    複数 prefix から得られた場合、最小 rank (=最も上位表示) を残す。
    戻り値は rank 昇順。
    """
    best: dict[str, dict] = {}
    for prefix, values in fetches:
        for i, value in enumerate(values, start=1):
            norm = google_suggest.normalize(value)
            if not norm:
                continue
            entry = {"query": value, "rank": i, "prefix": prefix, "depth": depth}
            existing = best.get(norm)
            if existing is None or entry["rank"] < existing["rank"]:
                best[norm] = entry
    return sorted(best.values(), key=lambda e: e["rank"])


def _relevance_key(s: str) -> str:
    """関連性フィルタ用の正規化: NFKC + lower + 空白**除去** (rank マージの

    google_suggest.normalize は空白を1個に圧縮するだけだが、こちらは単語順の
    揺れは見ず「空白の有無」だけを吸収したいので全除去する。
    """
    return _WHITESPACE_RE.sub("", unicodedata.normalize("NFKC", s).lower())


def _is_relevant(seed: str, candidate: str) -> bool:
    """候補が seed を部分文字列として含むか (空白揺れ・大小文字違いは無視)。"""
    seed_key = _relevance_key(seed)
    if not seed_key:
        return True
    return seed_key in _relevance_key(candidate)


# --------------------------------------------------------------------------
# 収集本体
# --------------------------------------------------------------------------

class _Budget:
    """run 全体で共有する総リクエスト数の残数カウンタ (--max-requests)。"""

    def __init__(self, max_requests: int):
        self.remaining = max_requests

    def take(self) -> bool:
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True


def _do_fetch(
    prefix: str, session: requests.Session, alias: str, state: dict[str, Any],
) -> list[str]:
    """1 request 分を取得する。失敗時は state['aborted']=True にして空リストを返す。"""
    if state["aborted"]:
        return []
    try:
        return fetch_suggestions_for_prefix(prefix, session, alias)
    except (requests.RequestException, google_suggest.NonOkStatusError) as e:
        logger.warning("HTTP error fetching amazon suggest for prefix=%r: %s; aborting collection loop", prefix, e)
        state["aborted"] = True
    except google_suggest.SuggestParseError as e:
        logger.warning("parse failure for prefix=%r: %s; aborting collection loop", prefix, e)
        state["aborted"] = True
    return []


def _prefix_variants(prefix_base: str, current_depth: int, seed: str, seed_kind: str) -> list[str]:
    """1つの frontier prefix から作る variant 一覧。

    常に (base, base+空白) の2つ。ブランド seed の最初の深さ (元の seed
    そのものを展開する時) に限り、"<seed> おもちゃ" というカテゴリ語つき
    variant も追加する (テーマ seed は元々 "<label> おもちゃ" の形なので不要)。
    """
    variants = [prefix_base, prefix_base + " "]
    if current_depth == 1 and prefix_base == seed and seed_kind == SEED_KIND_BRAND:
        variants.append(f"{prefix_base} {BRAND_CATEGORY_HINT}")
    return variants


def collect_seed(
    seed: str,
    seed_kind: str,
    session: requests.Session,
    alias: str,
    depth: int,
    budget: _Budget,
    state: dict[str, Any],
    sleep_min: float,
    sleep_max: float,
    sleeper=time.sleep,
) -> tuple[list[dict], int]:
    """1 seed 分 (--depth まで) を収集する。

    戻り値は (関連性フィルタ通過後の suggestion 一覧 (rank 昇順), 関連性
    フィルタで落とした件数)。budget 枯渇・state['aborted'] のいずれかで
    即座に打ち切る (呼び出し元の run() が run 全体の続行可否を判定する)。
    次の深さの frontier には「その深さで新たに見つかった候補語」だけを使う
    (蓄積済みの過去の深さの候補を毎回再展開すると無駄にリクエストが膨らむ
    ため)。関連性フィルタは最終的にマージし終えた候補に対して、元の seed
    (frontier 展開後の中間 prefix ではなく) を基準に適用する。
    """
    merged: dict[str, dict] = {}
    seed_norm = google_suggest.normalize(seed)
    frontier = [seed]
    current_depth = 1

    while current_depth <= depth and frontier and not state["aborted"] and budget.remaining > 0:
        depth_fetches: list[tuple[str, list[str]]] = []
        for prefix_base in frontier:
            if state["aborted"] or budget.remaining <= 0:
                break
            for prefix in _prefix_variants(prefix_base, current_depth, seed, seed_kind):
                if state["aborted"] or not budget.take():
                    break
                values = _do_fetch(prefix, session, alias, state)
                sleeper(random.uniform(sleep_min, sleep_max))
                if state["aborted"]:
                    break
                depth_fetches.append((prefix, values))

        new_this_depth = _rank_and_merge(depth_fetches, current_depth)
        for entry in new_this_depth:
            norm = google_suggest.normalize(entry["query"])
            existing = merged.get(norm)
            if existing is None or entry["rank"] < existing["rank"]:
                merged[norm] = entry

        frontier = [e["query"] for e in new_this_depth if google_suggest.normalize(e["query"]) != seed_norm]
        current_depth += 1

    relevant: list[dict] = []
    dropped = 0
    for entry in sorted(merged.values(), key=lambda e: e["rank"]):
        if _is_relevant(seed, entry["query"]):
            relevant.append(entry)
        else:
            dropped += 1
    return relevant, dropped


def write_result(
    out_dir: pathlib.Path,
    seed: str,
    seed_key: str,
    alias: str,
    suggestions: list[dict],
    dropped_irrelevant: int = 0,
    now: datetime | None = None,
) -> pathlib.Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "seed": seed,
        "alias": alias,
        "fetched_at": _now_iso(now),
        "dropped_irrelevant": dropped_irrelevant,
        "suggestions": suggestions,
    }
    out_path = out_dir / f"{seed_key}.json"
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return out_path


def run(
    taxonomy_path: pathlib.Path,
    out_dir: pathlib.Path,
    limit: int,
    min_age_days: int,
    sleep_min: float,
    sleep_max: float,
    depth: int = 1,
    max_requests: int = 200,
    alias: str = DEFAULT_ALIAS,
    seeds_file: pathlib.Path | None = None,
    seeds_mode: str = DEFAULT_SEEDS_MODE,
    dry_run: bool = False,
    session: requests.Session | None = None,
    sleeper=time.sleep,
    slug_map: TermSlugMap | None = None,
) -> dict:
    """収集を実行し、件数サマリを dict で返す (テスト・main 双方から呼べるように分離)。"""
    seeds_file = seeds_file or pathlib.Path(DEFAULT_SEEDS_FILE)
    seeds = load_seed_candidates(taxonomy_path, seeds_file, seeds_mode, slug_map)
    targets = select_targets(seeds, out_dir, limit, min_age_days)
    logger.info(
        "amazon suggest targets selected: %d (limit=%d, min_age_days=%d, depth=%d, max_requests=%d, seeds=%s)",
        len(targets), limit, min_age_days, depth, max_requests, seeds_mode,
    )

    summary = {
        "selected": len(targets), "written": 0, "aborted": False,
        "requests_used": 0, "dropped_irrelevant_total": 0,
    }

    if dry_run:
        for seed, seed_key, kind in targets:
            logger.info("[dry-run] would fetch amazon suggest for %s (%s, %s)", seed_key, seed, kind)
        return summary

    session = session or requests.Session()
    budget = _Budget(max_requests)
    state: dict[str, Any] = {"aborted": False}

    for seed, seed_key, kind in targets:
        if state["aborted"] or budget.remaining <= 0:
            summary["aborted"] = True
            break
        suggestions, dropped = collect_seed(
            seed, kind, session, alias, depth, budget, state, sleep_min, sleep_max, sleeper,
        )
        if suggestions or dropped:
            write_result(out_dir, seed, seed_key, alias, suggestions, dropped)
            summary["written"] += 1
            summary["dropped_irrelevant_total"] += dropped
            logger.info(
                "%s (%s, %s): %d suggestion(s) saved, %d dropped as irrelevant",
                seed_key, seed, kind, len(suggestions), dropped,
            )
        elif state["aborted"]:
            logger.warning("%s (%s, %s): aborted before any suggestion obtained; not writing", seed_key, seed, kind)

    summary["requests_used"] = max_requests - budget.remaining
    summary["aborted"] = summary["aborted"] or state["aborted"]

    logger.info(
        "amazon suggest mining done: selected=%d written=%d aborted=%s requests_used=%d dropped_irrelevant_total=%d",
        summary["selected"], summary["written"], summary["aborted"],
        summary["requests_used"], summary["dropped_irrelevant_total"],
    )
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Amazon サジェストを WP GSC に依存しない需要源として収集する (#2686 PR-B, inert)",
    )
    ap.add_argument("--limit", type=int, default=20, help="1 run あたりの最大 seed 数")
    ap.add_argument("--min-age-days", type=int, default=30, help="既存 data/raw/amazon_suggest/<seed_key>.json をこの日数より新しければ再取得しない")
    ap.add_argument("--sleep-min", type=float, default=2.0, help="リクエスト間 sleep の下限 (秒)")
    ap.add_argument("--sleep-max", type=float, default=4.0, help="リクエスト間 sleep の上限 (秒)")
    ap.add_argument("--depth", type=int, default=1, help="展開の深さ (既定 1 = 展開なし。上げると候補語をさらに prefix として展開する)")
    ap.add_argument("--max-requests", type=int, default=200, help="1 run あたりの総リクエスト数のハード上限")
    ap.add_argument("--alias", default=DEFAULT_ALIAS, help="Amazon サジェストの検索カテゴリ絞り込み (既定 aps=全カテゴリ。実測で toys は候補数が極端に減るだけと判明)")
    ap.add_argument("--seeds", choices=SEEDS_MODES, default=DEFAULT_SEEDS_MODE, help="seed 源: brands (ブランド名のみ) / themes (年齢×カテゴリ hub テーマのみ) / all (既定)")
    ap.add_argument("--taxonomy", default=DEFAULT_TAXONOMY, help="ブランド taxonomy yaml のパス (brand seed 抽出元)")
    ap.add_argument("--seeds-file", default=DEFAULT_SEEDS_FILE, help="hub テーマ定義 yaml のパス (theme seed 抽出元)")
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="収集結果の出力先ディレクトリ")
    ap.add_argument("--dry-run", action="store_true", help="対象選定だけ行い、実際の取得/書き込みは行わない")
    args = ap.parse_args()

    if args.sleep_min < 0 or args.sleep_max < args.sleep_min:
        raise SystemExit("--sleep-min must be >= 0 and --sleep-max must be >= --sleep-min")
    if args.depth < 1:
        raise SystemExit("--depth must be >= 1")
    if args.max_requests < 1:
        raise SystemExit("--max-requests must be >= 1")

    run(
        taxonomy_path=pathlib.Path(args.taxonomy),
        out_dir=pathlib.Path(args.out_dir),
        limit=args.limit,
        min_age_days=args.min_age_days,
        sleep_min=args.sleep_min,
        sleep_max=args.sleep_max,
        depth=args.depth,
        max_requests=args.max_requests,
        alias=args.alias,
        seeds_file=pathlib.Path(args.seeds_file),
        seeds_mode=args.seeds,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
