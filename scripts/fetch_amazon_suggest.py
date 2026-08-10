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
      &alias=toys&site-variant=desktop&version=3&event=onKeyPress
      &mid=A1VC38T7YXB528&lop=ja_JP&client-info=amazon-search-ui
  alias=toys でおもちゃカテゴリに絞る (alias=aps は全カテゴリ、本スクリプトは
  toys 固定がデフォルト)。mid=A1VC38T7YXB528 は amazon.co.jp のマーケット ID。

  レスポンス形状 (実測、fetch_google_suggest / fetch_suggest_info が使う
  OpenSearch 形式 `[query, [...]]` とは別物):
    {"alias": "toys", "prefix": "...", "suffix": "",
     "suggestions": [
       {"suggType": "KeywordSuggestion", "type": "KEYWORD", "value": "...",
        "refTag": "...", "candidateSources": "local", "strategyId": "...",
        "strategyApiType": "RANK", "prior": 0.0, "ghost": false, "help": false},
       ...
     ]}
  suggestions[].type は "KEYWORD" 以外 (カテゴリ絞り込み等) が混ざりうるため
  type=="KEYWORD" のみ採用する。配列順が表示順 = rank (1始まり)。

シード選定:
  WP GSC に依存しないシードが要る (依存させると結局 WP と同じ空間を掘る
  ことになり、本 PR の目的そのものが崩れる)。data/brand_taxonomy.yaml の
  ブランド canonical 名を採用した (fetch_suggest_info.py が使う年齢×カテゴリ
  hub テーマ (data/suggest_info_seeds.yaml) は不採用)。理由:
    - hub テーマは「英語」「プログラミング」のような情報型の粒度で、
      fetch_suggest_info.py (#3332 N2 Lane A) が既に別目的 (hub 本文拡充の
      需要サブクエリ供給) で専有している。同じ種を別の消費目的で二重に
      使うと役割が曖昧になる。
    - ブランド名は「バンダイ」→「バンダイ ぷにるんず」のような固有名詞
      起点の購買型サジェストを引き出しやすく、Amazon サジェストの強み
      (検索意図が最初から購買型) と最も相性が良い。
    - data/brand_taxonomy.yaml は294ブランド (exclude_from_taxonomy=true の
      「ノーブランド」を除く) を持ち、コード側にシード文字列をハードコード
      せず既存データから引ける。

  seed_key (出力ファイル名) は data/term_slugs.yaml の既存スラッグを
  **読み取り専用**で使う (scripts/term_slug.TermSlugMap.get())。実測で
  294ブランド全件が既に登録済み (ensure_tag_slugs.py が push 毎に
  brand_taxonomy.yaml の全 canonical をスキャンして登録しているため)。
  未登録ブランドがあった場合のみ、ローカルな fallback slug (NFKC+lower+
  非ascii置換、全滅時は sha1 先頭10桁) を使う (term_slugs.yaml への書き込みは
  行わない — 別レーンの永続化責務に副作用を持ち込まない)。

展開:
  各 seed について prefix=<seed> と prefix=<seed>+半角スペース の2リクエスト
  (depth=1、既定でここまで)。--depth を上げると、直前の深さで得た候補語を
  新たな prefix として同様に2リクエストずつ展開する。深さを上げると
  リクエスト数が指数的に増えるため、--max-requests で run 全体の総リクエスト
  数に必ずハード上限を掛ける。

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
    {"seed": "...", "alias": "toys", "fetched_at": "<ISO8601 UTC>",
     "suggestions": [{"query": "...", "rank": 1, "prefix": "...", "depth": 1}, ...]}
  data/raw/suggest/ (Google・ASIN起点) や data/raw/suggest_info/ (テーマ×修飾語)
  とは別ディレクトリ (混ぜない)。同一クエリが複数 prefix / depth から得られた
  場合は最小 rank (=最も上位表示) を残す。

再取得制御:
  data/raw/amazon_suggest/<seed_key>.json が存在しない seed を最優先、存在する
  場合は fetched_at が --min-age-days より古いものだけを対象にして fetched_at
  昇順 (古い/未取得ほど先) に並べ、--limit 件まで処理する
  (fetch_google_suggest.select_targets と同じ考え方)。

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
from scripts.term_slug import TermSlugMap

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("fetch_amazon_suggest")

SUGGEST_URL = "https://completion.amazon.co.jp/api/2017/suggestions"
DEFAULT_TAXONOMY = "data/brand_taxonomy.yaml"
DEFAULT_OUT_DIR = "data/raw/amazon_suggest"
DEFAULT_ALIAS = "toys"
DEFAULT_MID = "A1VC38T7YXB528"
KEYWORD_TYPE = "KEYWORD"

_FALLBACK_SLUG_RE = re.compile(r"[^a-z0-9]+")


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


def load_seed_candidates(
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


def select_targets(
    seeds: list[tuple[str, str]],
    out_dir: pathlib.Path,
    limit: int,
    min_age_days: int,
    now: datetime | None = None,
) -> list[tuple[str, str]]:
    """この run で収集すべき (seed, seed_key) を返す。

    順序: data/raw/amazon_suggest/<seed_key>.json が無いものを最優先 (epoch 0
    扱い)、存在するものは fetched_at 昇順。fetched_at が --min-age-days より
    新しい (= まだ新鮮) seed は対象から除外する。--limit 件で cap。
    """
    now = now or _now()
    cutoff = now - timedelta(days=min_age_days)
    epoch = datetime.fromtimestamp(0, tz=timezone.utc)

    candidates: list[tuple[datetime, str, str]] = []
    for seed, seed_key in seeds:
        out_path = out_dir / f"{seed_key}.json"
        if not out_path.exists():
            candidates.append((epoch, seed, seed_key))
            continue
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            candidates.append((epoch, seed, seed_key))
            continue
        fetched_at = _parse_iso(existing.get("fetched_at")) if isinstance(existing, dict) else None
        if fetched_at is None:
            candidates.append((epoch, seed, seed_key))
            continue
        if fetched_at > cutoff:
            continue  # まだ新鮮なのでスキップ
        candidates.append((fetched_at, seed, seed_key))

    candidates.sort(key=lambda x: x[0])
    return [(seed, key) for _, seed, key in candidates[:limit]]


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
# rank マージ
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


def collect_seed(
    seed: str,
    session: requests.Session,
    alias: str,
    depth: int,
    budget: _Budget,
    state: dict[str, Any],
    sleep_min: float,
    sleep_max: float,
    sleeper=time.sleep,
) -> list[dict]:
    """1 seed 分 (--depth まで) を収集し、rank 最小優先でマージした suggestion 一覧を返す。

    budget 枯渇・state['aborted'] のいずれかで即座に打ち切る (呼び出し元の
    run() が run 全体の続行可否を判定する)。次の深さの frontier には
    「その深さで新たに見つかった候補語」だけを使う (蓄積済みの過去の深さの
    候補を毎回再展開すると無駄にリクエストが膨らむため)。
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
            for prefix in (prefix_base, prefix_base + " "):
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

    return sorted(merged.values(), key=lambda e: e["rank"])


def write_result(
    out_dir: pathlib.Path,
    seed: str,
    seed_key: str,
    alias: str,
    suggestions: list[dict],
    now: datetime | None = None,
) -> pathlib.Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "seed": seed,
        "alias": alias,
        "fetched_at": _now_iso(now),
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
    dry_run: bool = False,
    session: requests.Session | None = None,
    sleeper=time.sleep,
    slug_map: TermSlugMap | None = None,
) -> dict:
    """収集を実行し、件数サマリを dict で返す (テスト・main 双方から呼べるように分離)。"""
    seeds = load_seed_candidates(taxonomy_path, slug_map)
    targets = select_targets(seeds, out_dir, limit, min_age_days)
    logger.info(
        "amazon suggest targets selected: %d (limit=%d, min_age_days=%d, depth=%d, max_requests=%d)",
        len(targets), limit, min_age_days, depth, max_requests,
    )

    summary = {"selected": len(targets), "written": 0, "aborted": False, "requests_used": 0}

    if dry_run:
        for seed, seed_key in targets:
            logger.info("[dry-run] would fetch amazon suggest for %s (%s)", seed_key, seed)
        return summary

    session = session or requests.Session()
    budget = _Budget(max_requests)
    state: dict[str, Any] = {"aborted": False}

    for seed, seed_key in targets:
        if state["aborted"] or budget.remaining <= 0:
            summary["aborted"] = True
            break
        suggestions = collect_seed(seed, session, alias, depth, budget, state, sleep_min, sleep_max, sleeper)
        if suggestions:
            write_result(out_dir, seed, seed_key, alias, suggestions)
            summary["written"] += 1
            logger.info("%s (%s): %d suggestion(s) saved", seed_key, seed, len(suggestions))
        elif state["aborted"]:
            logger.warning("%s (%s): aborted before any suggestion obtained; not writing", seed_key, seed)

    summary["requests_used"] = max_requests - budget.remaining
    summary["aborted"] = summary["aborted"] or state["aborted"]

    logger.info(
        "amazon suggest mining done: selected=%d written=%d aborted=%s requests_used=%d",
        summary["selected"], summary["written"], summary["aborted"], summary["requests_used"],
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
    ap.add_argument("--alias", default=DEFAULT_ALIAS, help="Amazon サジェストの検索カテゴリ絞り込み (既定 toys)")
    ap.add_argument("--taxonomy", default=DEFAULT_TAXONOMY, help="ブランド taxonomy yaml のパス (seed 抽出元)")
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
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
