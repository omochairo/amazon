"""fetch_suggest_info.py

Issue #3332 N2 Lane A「情報型シードのサジェスト収集」の収集スクリプト。

背景 (#2690 / #2687 / #3332 設計コメント):
  #2690 (closed) は「クエリ空白マイニングは不要」と結論済み。真因は空白ではなく
  「勝てる構造 (情報型 age/カテゴリ hub) に薄いページしか無い」こと。N2 はこれを
  受けて「未知の需要発見」ではなく **既存 hub (#2687 の年齢 hub / 英語・
  プログラミング・算数のテーマ別 category hub) を厚くするための需要サブクエリ
  供給** と再定義されている。

  本スクリプトはその Lane A (収集のみ)。既存の D 案レーン
  (fetch_google_suggest.py、ASIN=商品名を種にした収集) と同じ低レート・jitter・
  graceful abort のパターンを踏襲しつつ、種を「商品名」ではなく
  「hub テーマ (年齢/カテゴリ) × 修飾語」に変える。加えて a-z / 五十音 suffix を
  付与した再帰展開で Google の上位10件カットオフを超えて候補を掘り、
  completion.amazon.co.jp (購買意図クエリ) も収集する。

  収集と消費を分離し、Lane A (本スクリプト) だけを先行起動する。消費側
  (hub 拡充への供給・カバレッジ判定) は Lane B (N1/N3 実装後) の範囲であり、
  本スクリプトはデータを data/raw/suggest_info/ に蓄積するところまでが責務。

シードの定義:
  data/suggest_info_seeds.yaml にある themes (年齢 0〜6歳 / 英語・プログラミング・
  算数 / モンテッソーリ) と modifiers (おもちゃ / おもちゃ ランキング / おもちゃ
  おすすめ) の直積から base query を機械生成する (コード側にシード文字列を
  ハードコードしない)。由来は yaml 内コメント参照。

再帰展開 (a-z / 五十音 suffix):
  Google/Amazon のサジェストは概ね上位10件で打ち切られるため、`expand_modifier`
  (既定 "おもちゃ") で作った query に対してのみ、a-z (26) + 五十音 (46) の
  suffix を1文字ずつ付与し (`"<label> <expand_modifier> <suffix>"`)、
  再帰的に候補を掘る。全 modifier に展開すると request 数が modifiers 倍に
  膨らむため、展開対象は expand_modifier 1本に絞ってコストを抑える。

収集元:
  1. Google サジェスト (suggestqueries.google.com/complete/search) —
     fetch_google_suggest.py の実装 (fetch_suggestions_for_seed /
     NonOkStatusError / SuggestParseError / normalize) をそのまま再利用する。
  2. completion.amazon.co.jp/search/complete (購買意図クエリ) — Amazon 検索窓の
     オートコンプリート。レスポンス形状は OpenSearch suggestions 形式で Google と
     同じ (`[query, [suggestions...], ...]`) のため `_extract_suggestions` を
     共用する。**mkt パラメータの値は未検証** (既定 6)。初回のシャドー収集で
     実データの件数・形を見て調整する想定 (--amazon-mkt / --no-amazon で
     無効化も可能)。

エラー処理 (D 案からの変更点):
  D 案は「HTTP エラー1回で収集ループ全体を打ち切り」だが、本スクリプトは
  Google と Amazon の 2 系統を独立に収集元停止する: 一方の収集元が
  HTTP 非200/接続エラー、または JSON パース失敗が3連続すると **その収集元だけ**
  を以降のテーマで無効化し、もう一方は継続する (2 つの独立した外部依存を
  1つの壊れやすさに束ねない)。両方が無効化された時点で run 全体を打ち切る。

対象選定・レート制御:
  data/raw/suggest_info/<theme_key>.json が存在しない theme を最優先、存在
  する場合は fetched_at が --min-age-days より古いものだけを対象にして
  fetched_at 昇順 (古い/未取得ほど先) に並べ、--limit 件まで処理する
  (fetch_google_suggest.select_targets と同じ考え方)。リクエスト1件ごとに
  uniform(--sleep-min, --sleep-max) 秒 sleep する。

出力:
  data/raw/suggest_info/<theme_key>.json に `lane="A"` / `seed_type=
  "informational"` のタグを付けて蓄積する (D 案の data/raw/suggest/<ASIN>.json
  と区別するため、出力ディレクトリ自体も分けている)。

呼び出し元:
  omochairo/amazon-home-ops リポジトリの workflow (NAS/K8 いずれか、ネットワーク
  I/O のみで LLM 不要のため配置は運用側で決める) から
  `python -m scripts.fetch_suggest_info ...` として実行される想定
  (CI からは呼ばない)。

消費側 (未実装・スコープ外):
  hub 本文の拡充への還流は Lane B (N1/N3 実装後) の範囲。本スクリプトは
  蓄積するところまでで、`data/raw/suggest_info/*.json` を読む側は現時点で
  存在しない。

Issue: https://github.com/omochairo/amazon/issues/3332 (N2 設計コメント)
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import random
import string
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
import yaml

from scripts import fetch_google_suggest as google_suggest

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("fetch_suggest_info")

DEFAULT_SEEDS_FILE = "data/suggest_info_seeds.yaml"
DEFAULT_OUT_DIR = "data/raw/suggest_info"

LANE = "A"
SEED_TYPE = "informational"

GOOGLE_SOURCE = "google"
AMAZON_SOURCE = "amazon_completion"
_ALL_SOURCES = (GOOGLE_SOURCE, AMAZON_SOURCE)

# completion.amazon.co.jp: 公開の (認証不要) Amazon 検索窓オートコンプリート
# エンドポイント。レスポンス形状は Google 同様の OpenSearch suggestions 形式。
# mkt (マーケットID) の正確な値は未検証 — 初回シャドー収集で実データを見て
# 確定する (--amazon-mkt で上書き可能)。
AMAZON_COMPLETION_URL = "https://completion.amazon.co.jp/search/complete"
DEFAULT_AMAZON_MKT = 6

_MAX_CONSECUTIVE_PARSE_FAILURES = 3

ALPHA_SUFFIXES: tuple[str, ...] = tuple(string.ascii_lowercase)
# 五十音 (46字。ゐ/ゑ等の歴史的仮名は含めない現代仮名遣い版)。
KANA_SUFFIXES: tuple[str, ...] = tuple(
    "あいうえお" "かきくけこ" "さしすせそ" "たちつてと" "なにぬねの"
    "はひふへほ" "まみむめも" "やゆよ" "らりるれろ" "わをん"
)
EXPANSION_SUFFIXES: tuple[str, ...] = ALPHA_SUFFIXES + KANA_SUFFIXES


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
# シード定義の読み込み・クエリ生成
# --------------------------------------------------------------------------

def load_seed_config(path: pathlib.Path) -> dict[str, Any]:
    """data/suggest_info_seeds.yaml を読み込む。

    最小限の形状検証 (themes/modifiers/expand_modifier の存在) のみ行い、
    詳細な値検証は呼び出し元 (build_theme_queries) に委ねる。
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"invalid seed config shape in {path}")
    themes = raw.get("themes")
    modifiers = raw.get("modifiers")
    expand_modifier = raw.get("expand_modifier")
    if not isinstance(themes, list) or not themes:
        raise ValueError(f"{path}: 'themes' must be a non-empty list")
    if not isinstance(modifiers, list) or not modifiers:
        raise ValueError(f"{path}: 'modifiers' must be a non-empty list")
    if not isinstance(expand_modifier, str) or not expand_modifier.strip():
        raise ValueError(f"{path}: 'expand_modifier' must be a non-empty string")
    return {"themes": themes, "modifiers": modifiers, "expand_modifier": expand_modifier}


def build_theme_queries(theme_label: str, modifiers: list[str]) -> list[str]:
    """テーマ label × modifiers から base query を機械生成する (順序保持・重複除去)。"""
    seen: set[str] = set()
    out: list[str] = []
    for m in modifiers:
        q = f"{theme_label} {m}"
        if q in seen:
            continue
        seen.add(q)
        out.append(q)
    return out


def build_expansion_queries(theme_label: str, expand_modifier: str) -> list[str]:
    """再帰展開 (a-z / 五十音 suffix) 用の query 一覧を作る。"""
    base = f"{theme_label} {expand_modifier}"
    return [f"{base} {suffix}" for suffix in EXPANSION_SUFFIXES]


# --------------------------------------------------------------------------
# 対象選定
# --------------------------------------------------------------------------

def select_theme_targets(
    themes: list[dict[str, Any]],
    out_dir: pathlib.Path,
    limit: int,
    min_age_days: int,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """この run で収集すべき theme dict のリストを返す。

    順序: data/raw/suggest_info/<theme_key>.json が無いものを最優先 (epoch 0
    扱い)、存在するものは fetched_at 昇順。fetched_at が --min-age-days より
    新しい (= まだ新鮮) theme は対象から除外する。--limit 件で cap
    (fetch_google_suggest.select_targets と同じ考え方)。
    """
    now = now or _now()
    cutoff = now - timedelta(days=min_age_days)
    epoch = datetime.fromtimestamp(0, tz=timezone.utc)

    candidates: list[tuple[datetime, dict[str, Any]]] = []
    for theme in themes:
        key = theme.get("key")
        if not key:
            continue
        out_path = out_dir / f"{key}.json"
        if not out_path.exists():
            candidates.append((epoch, theme))
            continue
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            candidates.append((epoch, theme))
            continue
        fetched_at = _parse_iso(existing.get("fetched_at")) if isinstance(existing, dict) else None
        if fetched_at is None:
            candidates.append((epoch, theme))
            continue
        if fetched_at > cutoff:
            continue  # まだ新鮮なのでスキップ
        candidates.append((fetched_at, theme))

    candidates.sort(key=lambda x: x[0])
    return [theme for _, theme in candidates[:limit]]


# --------------------------------------------------------------------------
# 収集元フェッチ
# --------------------------------------------------------------------------

def fetch_amazon_completions_for_query(
    query: str, session: requests.Session, mkt: int = DEFAULT_AMAZON_MKT,
) -> list[str]:
    """completion.amazon.co.jp から購買意図クエリのサジェストを取得する。

    レスポンス形状は Google と同じ OpenSearch suggestions 形式なので
    fetch_google_suggest._extract_suggestions をそのまま再利用する。
    HTTP 非 200 は google_suggest.NonOkStatusError、JSON パース失敗は
    google_suggest.SuggestParseError を送出する (呼び出し元の分岐を統一するため)。
    """
    params = {
        "method": "completion",
        "mkt": mkt,
        "search-alias": "aps",
        "q": query,
    }
    resp = session.get(
        AMAZON_COMPLETION_URL,
        params=params,
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
    return google_suggest._extract_suggestions(payload)


_SOURCE_FETCHERS = {
    GOOGLE_SOURCE: lambda query, session, amazon_mkt: google_suggest.fetch_suggestions_for_seed(query, session),
    AMAZON_SOURCE: lambda query, session, amazon_mkt: fetch_amazon_completions_for_query(query, session, amazon_mkt),
}


def dedupe_info_suggestions(raw: list[tuple[str, str, str]], seeds: list[str]) -> list[dict]:
    """seed 横断で正規化重複除去する (source 情報も保持)。

    正規化 (NFKC + lowercase + 空白圧縮) は fetch_google_suggest.normalize を
    再利用する。同一 query が複数 source から得られた場合は先に見つかった方
    (source 収集順 = google → amazon_completion) を採用する。
    """
    seed_norms = {google_suggest.normalize(s) for s in seeds}
    seen: set[str] = set()
    out: list[dict] = []
    for query, seed, source in raw:
        norm = google_suggest.normalize(query)
        if not norm or norm in seed_norms or norm in seen:
            continue
        seen.add(norm)
        out.append({"query": query, "seed": seed, "source": source})
    return out


def write_result(
    out_dir: pathlib.Path,
    theme: dict[str, Any],
    seeds_used: list[str],
    suggestions: list[dict],
    now: datetime | None = None,
) -> pathlib.Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "theme_key": theme["key"],
        "theme_label": theme["label"],
        "theme_kind": theme.get("kind", ""),
        "lane": LANE,
        "seed_type": SEED_TYPE,
        "seeds": seeds_used,
        "suggestions": suggestions,
        "fetched_at": _now_iso(now),
    }
    out_path = out_dir / f"{theme['key']}.json"
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return out_path


# --------------------------------------------------------------------------
# 収集本体
# --------------------------------------------------------------------------

def _fetch_one(
    source: str,
    query: str,
    session: requests.Session,
    amazon_mkt: int,
    source_state: dict[str, dict[str, Any]],
    raw: list[tuple[str, str, str]],
) -> None:
    """1 query x 1 source を取得し raw に積む。source_state を破壊的に更新する。

    NonOkStatusError / RequestException: その source を以降の run 全体で無効化。
    SuggestParseError: 連続失敗カウントを進め、閾値到達で source を無効化。
    成功時は連続失敗カウントをリセットする。
    """
    state = source_state[source]
    if not state["enabled"]:
        return
    fetcher = _SOURCE_FETCHERS[source]
    try:
        suggestions = fetcher(query, session, amazon_mkt)
    except (requests.RequestException, google_suggest.NonOkStatusError) as e:
        logger.warning("source=%s HTTP error for %r: %s; disabling this source for the rest of the run", source, query, e)
        state["enabled"] = False
        return
    except google_suggest.SuggestParseError as e:
        state["consecutive_parse_failures"] += 1
        logger.warning(
            "source=%s parse failure for %r (%d/%d consecutive): %s",
            source, query, state["consecutive_parse_failures"], _MAX_CONSECUTIVE_PARSE_FAILURES, e,
        )
        if state["consecutive_parse_failures"] >= _MAX_CONSECUTIVE_PARSE_FAILURES:
            logger.warning("source=%s %d consecutive parse failures; disabling this source", source, state["consecutive_parse_failures"])
            state["enabled"] = False
        return
    else:
        state["consecutive_parse_failures"] = 0
        for s in suggestions:
            raw.append((s, query, source))


def collect_theme(
    theme: dict[str, Any],
    modifiers: list[str],
    expand_modifier: str,
    session: requests.Session,
    amazon_mkt: int,
    source_state: dict[str, dict[str, Any]],
    sleep_min: float,
    sleep_max: float,
    sleeper=time.sleep,
) -> tuple[list[str], list[dict]]:
    """1 theme 分の base query + 再帰展開 query を両 source から収集する。

    どちらの source も enabled でなくなった時点でこの theme の残り処理は
    打ち切る (呼び出し元の run() が run 全体の打ち切り判定を行う)。
    戻り値は (使用した seed 一覧, dedupe 済み suggestion 一覧)。
    """
    label = theme["label"]
    base_queries = build_theme_queries(label, modifiers)
    expansion_queries = build_expansion_queries(label, expand_modifier)
    all_queries = base_queries + expansion_queries

    raw: list[tuple[str, str, str]] = []
    for query in all_queries:
        if not any(source_state[s]["enabled"] for s in _ALL_SOURCES):
            break
        for source in _ALL_SOURCES:
            if not source_state[source]["enabled"]:
                continue
            _fetch_one(source, query, session, amazon_mkt, source_state, raw)
            sleeper(random.uniform(sleep_min, sleep_max))

    deduped = dedupe_info_suggestions(raw, all_queries)
    return base_queries, deduped


def run(
    seeds_file: pathlib.Path,
    out_dir: pathlib.Path,
    limit: int,
    min_age_days: int,
    sleep_min: float,
    sleep_max: float,
    amazon_mkt: int = DEFAULT_AMAZON_MKT,
    use_amazon: bool = True,
    dry_run: bool = False,
    session: requests.Session | None = None,
    sleeper=time.sleep,
) -> dict:
    """収集を実行し、件数サマリを dict で返す (テスト・main 双方から呼べるように分離)。"""
    config = load_seed_config(seeds_file)
    themes = config["themes"]
    modifiers = config["modifiers"]
    expand_modifier = config["expand_modifier"]

    targets = select_theme_targets(themes, out_dir, limit, min_age_days)
    logger.info("suggest_info targets selected: %d (limit=%d, min_age_days=%d)", len(targets), limit, min_age_days)

    summary = {
        "selected": len(targets), "written": 0,
        "google_aborted": False, "amazon_aborted": False, "aborted": False,
    }

    if dry_run:
        for theme in targets:
            logger.info("[dry-run] would fetch suggest_info for %s (%s)", theme["key"], theme["label"])
        return summary

    session = session or requests.Session()
    source_state = {
        GOOGLE_SOURCE: {"enabled": True, "consecutive_parse_failures": 0},
        AMAZON_SOURCE: {"enabled": use_amazon, "consecutive_parse_failures": 0},
    }

    for theme in targets:
        if not any(source_state[s]["enabled"] for s in _ALL_SOURCES):
            summary["aborted"] = True
            break
        seeds_used, deduped = collect_theme(
            theme, modifiers, expand_modifier, session, amazon_mkt,
            source_state, sleep_min, sleep_max, sleeper,
        )
        write_result(out_dir, theme, seeds_used, deduped)
        summary["written"] += 1
        logger.info("%s (%s): %d suggestion(s) saved", theme["key"], theme["label"], len(deduped))

    summary["google_aborted"] = not source_state[GOOGLE_SOURCE]["enabled"]
    summary["amazon_aborted"] = use_amazon and not source_state[AMAZON_SOURCE]["enabled"]
    summary["aborted"] = summary["aborted"] or (summary["google_aborted"] and summary["amazon_aborted"])

    logger.info(
        "suggest_info mining done: selected=%d written=%d google_aborted=%s amazon_aborted=%s",
        summary["selected"], summary["written"], summary["google_aborted"], summary["amazon_aborted"],
    )
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description="情報型シード (hub テーマ x 修飾語) のサジェストを収集する (#3332 N2 Lane A)")
    ap.add_argument("--limit", type=int, default=3, help="1 run あたりの最大 theme 数 (再帰展開込みで theme あたり request 数が多いため既定は控えめ)")
    ap.add_argument("--min-age-days", type=int, default=30, help="既存 data/raw/suggest_info/<theme>.json をこの日数より新しければ再取得しない")
    ap.add_argument("--sleep-min", type=float, default=2.0, help="リクエスト間 sleep の下限 (秒)")
    ap.add_argument("--sleep-max", type=float, default=4.0, help="リクエスト間 sleep の上限 (秒)")
    ap.add_argument("--seeds-file", default=DEFAULT_SEEDS_FILE, help="シード定義 yaml のパス")
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="収集結果の出力先ディレクトリ")
    ap.add_argument("--amazon-mkt", type=int, default=DEFAULT_AMAZON_MKT, help="completion.amazon.co.jp の mkt パラメータ (未検証の既定値)")
    ap.add_argument("--no-amazon", action="store_true", help="completion.amazon.co.jp 収集を無効化し Google サジェストのみ収集する")
    ap.add_argument("--dry-run", action="store_true", help="対象選定だけ行い、実際の取得/書き込みは行わない")
    args = ap.parse_args()

    if args.sleep_min < 0 or args.sleep_max < args.sleep_min:
        raise SystemExit("--sleep-min must be >= 0 and --sleep-max must be >= --sleep-min")

    run(
        seeds_file=pathlib.Path(args.seeds_file),
        out_dir=pathlib.Path(args.out_dir),
        limit=args.limit,
        min_age_days=args.min_age_days,
        sleep_min=args.sleep_min,
        sleep_max=args.sleep_max,
        amazon_mkt=args.amazon_mkt,
        use_amazon=not args.no_amazon,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
