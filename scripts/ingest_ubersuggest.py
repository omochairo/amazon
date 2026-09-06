#!/usr/bin/env python3
"""ingest_ubersuggest.py

Ubersuggest CSV (監視している競合おもちゃサイトの検索語エクスポート) を
第三の需要源として取り込む (#2686 PR-C)。

なぜ第三の需要源が要るか:
  navi (navi.omcha.jp) の需要源はこれまで omcha.jp (WP 本家) の GSC だったが、
  Google 検索は同一サイト扱いの host crowding をかけるため、WP が既に取っている
  語で navi の商品ページを出すと「露出が増える」のでなく「WP の枠が置き換わる」
  おそれがある (#4889 で順位ガード導入済み)。実測では WP と重なるのは
  Ubersuggest 需要語 2,532 語のうち 40 語 (1.6%) しかなく、Ubersuggest は
  WP のカニバリ対象になりにくい独立した需要源として使える。

CSV の実測 (2026-08-10, 再調査不要):
  data/demand/ubersuggest/ に投入されるファイルは 2 種類ある。
    - キーワードレポート: 列 Keywords,Volume,Position,Est. Visits,
      Seo Difficulty,Ranking Url (Position は競合サイトの当該語での順位)
    - Top Pages レポート: 列 Title,URL,Est. Visits,Backlinks (Keywords 列が
      無い)。owner が誤って投入することがあるので、Keywords 相当の列が無い
      ファイルは例外にせず skipped_files に理由つきで記録してスキップする
      (握り潰さない)。
  文字コードは UTF-8 (BOM 有無どちらもありうる)、改行は CRLF。列名はエクス
  ポート元のバージョン差で揺れうるので COLUMN_ALIASES で吸収する。

  重複排除キーは build_demand_keywords.normalize_key (NFKC + 小文字化 +
  空白完全除去) を再利用する。空白を残すと GSC 由来クエリと同様に分かち書き
  揺れで同一語が別集計になる (実測: 空白を残すと 3,939 語、除去すると
  2,532 語)。同一語が複数ファイル/複数行に現れたら Volume 最大の行を採用し、
  出現した全サイト名を sites に集約する。

語のゲート (data/demand_query_rules.yaml、ハードコードしない):
  2 階層。
    (a) subject_exclusions — 主題そのものが商品でない語 (キャラ一覧・図鑑・
        作り方・攻略・無料コンテンツ・施設・教育役務・とは系定義)。
        語ごと落とし、落ちた理由 (カテゴリ) を dropped_subject に残す。
    (b) trailing_modifiers — 主題は商品で末尾の修飾語だけ非商品 (評判・
        価格・販路・懸念)。**落とさず末尾だけ剥いで残す**。
        「口コミ」は商品ページで受けられるので (a) ではなく (b)。
        「キャラクター一覧」は主題が商品でないので (a) であって、剥いでも
        意味がない。この2つを混同しないこと。

  L1 の誤りは非対称 (2026-08-10 owner レビューで確定):
    - 誤除外 (本当は商品なのに subject_exclusions で落とす) は L2 実査で
      回復できない — 二度と候補に上がらず、しかも記録が dropped_subject に
      埋もれて気付けない。
    - 誤通過 (非商品を subject_exclusions で落とさず残す) は後続 L2 実査
      (Amazon SearchItems + genre_gate.py) で落とせる。
    したがって subject_exclusions は**保守的 (緩め) に倒す**。判断に迷う語・
    短い断片トークンで無関係な語を巻き込みうる語は入れず、L2 に送る。
    data/demand_query_rules.yaml のコメントに実例 (「図鑑」「ランド」「パーク」
    「幼稚園」「保育園」の誤除外) を記録してある。

異常値・Volume 0 は落とさない:
  「知育 村」の Volume が 1,000,000 と桁がおかしく、Ubersuggest 側のノイズの
  可能性が高い。閾値で黙って落とさず suspect_volume:true を立てて出力に残し、
  owner が後で判断できるようにする (data/demand_query_rules.yaml の
  suspect_volume_threshold)。
  Volume 0 も同様に落とさない。Ubersuggest の Volume 0 は「需要ゼロ」では
  なく「測定閾値未満」であり (2026-08-10 の実データ検証で確認済み)、0 を
  「需要が無い」ことの反証に使ってはいけない。

WP (omcha.jp) とのカニバリ計測 (レポートのみ、ここでは除外しない):
  data/analytics/history/gsc_wp_by_query.jsonl と突き合わせ、omcha.jp が
  既に露出を持つ語に WP 実績 (impressions/clicks/position) を付ける。
  position は build_demand_keywords.load_wp_rank_stats を再利用し、
  impression 加重平均で出す (日次 position の単純平均は誤り。閑散日の
  順位に引きずられる)。除外は共通の順位ガード (#4889 と同じしきい値
  pos<=3.0 かつ clicks>=100) を後続 PR で configure する想定なので、
  ここでは wp_rank_guard フラグを立てるだけにとどめ、語は落とさない。

サイト単位の採否 (#2686 PR-D, 2026-08-10 追加):
  owner が新たに9サイト分の CSV を投入し、サイト別の質を実測した結果、
  p-bandai.jp / bandai-hobby.net の2サイトを owner 判断で除外した (大人向け
  キャラクターグッズ・プラモデル中心で navi の対象外。Volume上位100語中64語を
  占め、良質な語を押し出す実害があるため)。data/demand_query_rules.yaml の
  excluded_sites にサイト名と理由を列挙する。CSV ファイルは owner の資産
  なので削除しない (読み飛ばすだけ)。

  1つの語が複数サイトに出ることがある (「ガンプラ」は bandai-hobby と
  p-bandai の両方、「ベイブレードx」は takaratomymall と toysrus の両方)。
  除外は **行 (site, keyword) 単位**で dedupe_rows に渡す前に行うので、
  除外サイト由来の出典だけが落ち、採用サイトにも出ている語はそちらの行で
  残る。全出典が除外サイトだった語だけが grouped から消える。
  除外サイトごとの (reason / keyword_rows / volume_sum) は握り潰さず
  出力 JSON の excluded_sites に記録する (フィルタ前の raw_rows から集計)。

本 PR のスコープ外 (L1 だけでは商品抽出できない):
  実測で、この語彙ルールを通しても残り約1,878語の上位には非商品が残る
  (「知育 村」「みみっち」「すいちゃん みいつけた」「こども新聞」
  「たまごっち 種類」等)。**語彙ルールだけで商品抽出はできない。**
  Amazon 実査 (SearchItems + scripts/genre_gate.py のジャンル判定 + 返り
  商品タイトルとクエリ語の重なり) が必須だが、それは後続 PR-D で行う。
  本 PR は外部 API を一切呼ばず、出力もまだ商品化候補ではなく L2 実査の
  入力である。data/demand_keywords.json への合流・fetch_amazon.py への
  配線も行わない (後続 PR)。cron / GitHub Actions workflow は一切変更
  しない (inert)。

使い方:
    python scripts/ingest_ubersuggest.py
    python scripts/ingest_ubersuggest.py --csv-dir data/demand/ubersuggest --dry-run
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import logging
import pathlib
import re
from datetime import datetime, timezone
from typing import Any

import yaml

# build_demand_keywords の正規化・WP順位集計ロジックを再利用する (重複実装しない)。
import sys

_SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import build_demand_keywords as bdk  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ingest_ubersuggest")

DEFAULT_CSV_DIR = "data/demand/ubersuggest"
DEFAULT_RULES_PATH = "data/demand_query_rules.yaml"
DEFAULT_OUT = "data/analytics/ubersuggest_demand.json"
# 供給元は omcha-ops → amazon-navi-brain の派生物に変わった (omcha-ops#97 P2)。
# 単一のパス定数ではなく bdk.resolve_wp_history_path() で候補から解決する。
DEFAULT_WP_HISTORY_PATH = None
DEFAULT_GUARD_POS_MAX = bdk.DEFAULT_GUARD_POS_MAX
DEFAULT_GUARD_MIN_CLICKS = bdk.DEFAULT_GUARD_MIN_CLICKS

# 列名エイリアス。将来のエクスポート差異に耐えるため小文字・空白正規化した
# キーで引く。値は候補の優先順 (先に見つかったものを使う)。
COLUMN_ALIASES: dict[str, list[str]] = {
    "keywords": ["keywords", "keyword"],
    "volume": ["volume", "search volume"],
    "position": ["position", "pos"],
    "seo_difficulty": ["seo difficulty", "sd", "difficulty"],
    "est_visits": ["est. visits", "est visits", "estimated visits"],
    "ranking_url": ["ranking url", "url"],
}

_FILENAME_PREFIX_RE = re.compile(r"^ubersuggest\s+", re.IGNORECASE)
_FILENAME_SCHEME_RE = re.compile(r"^https?_+", re.IGNORECASE)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def site_name_from_filename(path: pathlib.Path) -> str:
    """ファイル名からサイト名を取り出す。

    Ubersuggest のエクスポートファイル名は "ubersuggest https_<domain>.csv"
    の形 (URL の "://" がアンダースコアに置換されている)。"ubersuggest "
    prefix と "https_"/"http_" scheme 残骸を落とし、末尾のアンダースコアも
    落とす (URL 末尾がスラッシュだったファイルは "..._" になるため)。
    """
    stem = path.stem
    stem = _FILENAME_PREFIX_RE.sub("", stem)
    stem = _FILENAME_SCHEME_RE.sub("", stem)
    return stem.strip("_ ").strip()


def _normalize_header(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def resolve_columns(fieldnames: list[str]) -> dict[str, str | None]:
    """CSV ヘッダをエイリアス解決する。値は元のヘッダ名 (無ければ None)。"""
    normalized_to_actual = {_normalize_header(f): f for f in (fieldnames or [])}
    resolved: dict[str, str | None] = {}
    for canonical, aliases in COLUMN_ALIASES.items():
        actual = None
        for alias in aliases:
            if alias in normalized_to_actual:
                actual = normalized_to_actual[alias]
                break
        resolved[canonical] = actual
    return resolved


def _to_number(value: Any) -> float | None:
    if value is None:
        return None
    s = str(value).strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def read_csv_file(path: pathlib.Path) -> tuple[list[dict[str, Any]], str | None, list[str]]:
    """1 CSV を読む。

    戻り値: (rows, skip_reason, fieldnames)。skip_reason が None でなければ
    rows は空で、呼び出し側は skipped_files に理由つきで記録する。

    encoding="utf-8-sig" で BOM 有無どちらも吸収する。newline="" で開くのは
    CRLF を csv モジュールに正しく扱わせるため (Python 公式推奨)。
    """
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        cols = resolve_columns(fieldnames)
        if not cols.get("keywords"):
            return [], f"Keywords 相当の列が無い (Top Pages レポート等)", fieldnames
        rows = []
        for row in reader:
            rows.append(row)
    return rows, None, fieldnames


def load_rules(rules_path: pathlib.Path) -> dict[str, Any]:
    data = yaml.safe_load(rules_path.read_text(encoding="utf-8")) or {}
    return data


def _flatten_gate_terms(section: dict[str, Any], mode_key: str) -> list[tuple[str, str, str]]:
    """(term, category_key, category_label) のリストを返す。mode_key は "contains" か "suffix"。"""
    out: list[tuple[str, str, str]] = []
    for cat_key, cat in (section or {}).items():
        if not isinstance(cat, dict):
            continue
        label = cat.get("label") or cat_key
        for term in cat.get(mode_key) or []:
            t = bdk.normalize_key(term)
            if t:
                out.append((t, cat_key, label))
    return out


def match_subject_exclusions(
    key: str, contains_terms: list[tuple[str, str, str]], suffix_terms: list[tuple[str, str, str]]
) -> list[dict[str, str]]:
    """主題除外にマッチしたカテゴリの一覧を返す (複数カテゴリに同時該当しうる)。"""
    matched = []
    for term, cat_key, label in contains_terms:
        if term and term in key:
            matched.append({"category": cat_key, "label": label, "term": term})
    for term, cat_key, label in suffix_terms:
        if term and key.endswith(term):
            matched.append({"category": cat_key, "label": label, "term": term})
    return matched


def _flatten_brand_composite(
    section: dict[str, Any],
) -> list[tuple[str, str, list[str], list[str]]]:
    """(category_key, label, brands, modifiers) のリストを返す。

    subject_exclusions のカテゴリのうち "brand_composite" キーを持つものだけ
    拾う (store_navigational 用)。通常の contains/suffix とは別扱い。
    """
    out: list[tuple[str, str, list[str], list[str]]] = []
    for cat_key, cat in (section or {}).items():
        if not isinstance(cat, dict) or "brand_composite" not in cat:
            continue
        label = cat.get("label") or cat_key
        bc = cat["brand_composite"] or {}
        brands = [bdk.normalize_key(b) for b in (bc.get("brands") or [])]
        brands = [b for b in brands if b]
        modifiers = [bdk.normalize_key(m) for m in (bc.get("modifiers") or [])]
        modifiers = [m for m in modifiers if m]
        if brands:
            out.append((cat_key, label, brands, modifiers))
    return out


def match_brand_composite(
    key: str, brand_composite_defs: list[tuple[str, str, list[str], list[str]]]
) -> list[dict[str, str]]:
    """店名トークンの複合条件マッチ (2026-08-10 owner レビューで追加)。

    店名は「店名である前に玩具ブランド」であることがあり (ボーネルンド・
    タカラトミー等)、単純 contains で落とすと「ボーネルンド おもちゃ」
    「ボーネルンドルーピング」のような実在商品需要語まで誤除外する
    (data/demand_keywords.json に「ボーネルンドルーピング」が WP GSC 由来の
    正規需要語として既に存在しており、単純 contains は既存の正規語と衝突する)。

    複合条件 (誤除外を避けるため厳しめに倒す):
      1. クエリ全体が店名そのもの (key == brand) → 落とす
      2. 店名 + modifiers のどれか (実データにある店舗運営語) → 落とす
      3. それ以外の「店名 + 何か」(地名・商品語等) → **マッチさせない**
         (呼び出し側で dropped_subject に入れない = L2 実査に送る)
    """
    matched: list[dict[str, str]] = []
    for cat_key, label, brands, modifiers in brand_composite_defs:
        for brand in brands:
            if brand not in key:
                continue
            if key == brand:
                matched.append({"category": cat_key, "label": label, "term": brand})
                break
            if any(m in key for m in modifiers):
                matched.append({"category": cat_key, "label": label, "term": brand})
                break
            # brand はあるが exact でも modifier 付きでもない
            # (例:「ボーネルンド 大阪」「ボーネルンド おもちゃ」) → L2 に送る。
    return matched


def strip_trailing_modifiers(key: str, suffix_terms: list[tuple[str, str, str]]) -> tuple[str, list[str]]:
    """末尾の修飾語を繰り返し剥ぐ。

    長い語から先に試す (「どこで買える」を先に剥がないと「買える」が
    残らず不整合になるケースを避ける、build_demand_keywords.to_search_keyword
    と同じ考え方)。1 周で複数マッチする可能性があるため、変化が無くなるまで
    ループする。
    """
    terms_sorted = sorted({t for t, _, _ in suffix_terms}, key=len, reverse=True)
    stripped: list[str] = []
    changed = True
    while changed:
        changed = False
        for term in terms_sorted:
            if term and key.endswith(term):
                key = key[: -len(term)]
                stripped.append(term)
                changed = True
                break
    return key, stripped


def collect_csv_rows(csv_dir: pathlib.Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """CSV 群を読み、(raw_rows, skipped_files) を返す。

    raw_rows の各要素: {"site": ..., "raw_query": ..., "volume": ...,
    "position": ..., "seo_difficulty": ...}
    """
    raw_rows: list[dict[str, Any]] = []
    skipped_files: list[dict[str, Any]] = []
    for path_str in sorted(glob.glob(str(csv_dir / "*.csv"))):
        path = pathlib.Path(path_str)
        rows, skip_reason, fieldnames = read_csv_file(path)
        if skip_reason:
            skipped_files.append({
                "file": path.name,
                "reason": skip_reason,
                "columns": fieldnames,
            })
            logger.warning("skip %s: %s (columns=%s)", path.name, skip_reason, fieldnames)
            continue
        site = site_name_from_filename(path)
        cols = resolve_columns(fieldnames)
        for row in rows:
            kw = (row.get(cols["keywords"]) or "").strip() if cols.get("keywords") else ""
            if not kw:
                continue
            raw_rows.append({
                "site": site,
                "raw_query": kw,
                "volume": _to_number(row.get(cols["volume"])) if cols.get("volume") else None,
                "position": _to_number(row.get(cols["position"])) if cols.get("position") else None,
                "seo_difficulty": _to_number(row.get(cols["seo_difficulty"]))
                if cols.get("seo_difficulty") else None,
            })
    return raw_rows, skipped_files


def compute_excluded_sites_report(
    raw_rows: list[dict[str, Any]], rules: dict[str, Any]
) -> list[dict[str, Any]]:
    """excluded_sites 設定にあるサイトの記述統計を返す (握り潰さない)。

    フィルタ**前**の raw_rows (全 CSV 行) から、そのサイト自身の生の行数と
    Volume 合計を集計する。「そのサイトがフィルタ前どんな量だったか」の記録
    であり、他サイトとの重複排除後に実際に落ちた語数ではない (重複除外は
    dedupe_rows 後の summary.total_unique_queries 等で別途分かる)。
    """
    excluded_cfg = rules.get("excluded_sites") or {}
    out: list[dict[str, Any]] = []
    for site, cfg in excluded_cfg.items():
        reason = cfg.get("reason") if isinstance(cfg, dict) else str(cfg)
        site_rows = [r for r in raw_rows if r["site"] == site]
        out.append({
            "site": site,
            "reason": reason,
            "keyword_rows": len(site_rows),
            "volume_sum": sum(r["volume"] or 0 for r in site_rows),
        })
    out.sort(key=lambda d: d["site"])
    return out


def filter_excluded_site_rows(
    raw_rows: list[dict[str, Any]], excluded_site_names: set[str]
) -> list[dict[str, Any]]:
    """excluded_site_names に属する行を落とす。

    同じ語が他の採用サイトにも出ていれば、その行はここでは削られないので
    dedupe_rows でそのまま拾われる (「全出典が除外サイトだった語だけが落ちる」
    という要件はこの前段フィルタだけで満たされる。dedupe 側の変更は不要)。
    """
    if not excluded_site_names:
        return raw_rows
    return [r for r in raw_rows if r["site"] not in excluded_site_names]


def dedupe_rows(raw_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """空白除去キーで重複排除する。Volume 最大の行の position/seo_difficulty/
    raw_query を採用し、出現した全サイト名を集約する。

    重複排除キーは build_demand_keywords.normalize_key (NFKC + 小文字化 +
    空白完全除去) を使う。空白を残すと GSC 由来クエリと同様に「たまごっち
    みみっち」と「たまごっちみみっち」が別集計になる (2026-08-10 実データ
    検証: 空白を残すと 3,939 語、除去すると 2,532 語)。
    """
    grouped: dict[str, dict[str, Any]] = {}
    for row in raw_rows:
        key = bdk.normalize_key(row["raw_query"])
        if not key:
            continue
        entry = grouped.get(key)
        if entry is None:
            entry = {
                "key": key,
                "raw_query": row["raw_query"],
                "volume": row["volume"] or 0,
                "position": row["position"],
                "seo_difficulty": row["seo_difficulty"],
                "sites": set(),
            }
            grouped[key] = entry
        entry["sites"].add(row["site"])
        vol = row["volume"] or 0
        if vol > (entry["volume"] or 0):
            entry["volume"] = vol
            entry["raw_query"] = row["raw_query"]
            entry["position"] = row["position"]
            entry["seo_difficulty"] = row["seo_difficulty"]
    return grouped


def build(
    raw_rows: list[dict[str, Any]],
    rules: dict[str, Any],
    wp_rank_stats: dict[str, dict[str, float]] | None = None,
    guard_pos_max: float = DEFAULT_GUARD_POS_MAX,
    guard_min_clicks: float = DEFAULT_GUARD_MIN_CLICKS,
) -> dict[str, Any]:
    wp_rank_stats = wp_rank_stats or {}
    suspect_threshold = rules.get("suspect_volume_threshold", 500000)

    subject_contains = _flatten_gate_terms(rules.get("subject_exclusions"), "contains")
    subject_suffix = _flatten_gate_terms(rules.get("subject_exclusions"), "suffix")
    brand_composite_defs = _flatten_brand_composite(rules.get("subject_exclusions"))
    modifier_suffix = _flatten_gate_terms(rules.get("trailing_modifiers"), "suffix")

    grouped = dedupe_rows(raw_rows)

    dropped_subject: list[dict[str, Any]] = []
    dropped_empty_after_strip = 0
    keywords: list[dict[str, Any]] = []

    wp_dup_count = 0
    wp_guard_count = 0
    modifier_stripped_count = 0
    suspect_count = 0

    for key, entry in grouped.items():
        matched = match_subject_exclusions(key, subject_contains, subject_suffix)
        matched += match_brand_composite(key, brand_composite_defs)
        if matched:
            dropped_subject.append({
                "query": entry["raw_query"],
                "volume": entry["volume"],
                "categories": sorted({m["category"] for m in matched}),
                "matched_terms": [m["term"] for m in matched],
            })
            continue

        stripped_key, stripped_mods = strip_trailing_modifiers(key, modifier_suffix)
        if not stripped_key:
            dropped_empty_after_strip += 1
            continue
        if stripped_mods:
            modifier_stripped_count += 1

        volume = entry["volume"] or 0
        suspect_volume = volume >= suspect_threshold
        if suspect_volume:
            suspect_count += 1

        wp_key = bdk.normalize_key(entry["raw_query"])
        wp_stat = wp_rank_stats.get(wp_key)
        wp_impressions = wp_clicks = wp_position = None
        wp_rank_guard = False
        if wp_stat and wp_stat.get("imp"):
            wp_dup_count += 1
            wp_impressions = round(wp_stat["imp"], 1)
            wp_clicks = round(wp_stat["clicks"], 1)
            wp_position = round(wp_stat["pos"], 1)
            if wp_position <= guard_pos_max and wp_clicks >= guard_min_clicks:
                wp_rank_guard = True
                wp_guard_count += 1

        keywords.append({
            "query": stripped_key,
            "raw_query": entry["raw_query"],
            "volume": volume,
            "sites": sorted(entry["sites"]),
            "competitor_position": entry["position"],
            "seo_difficulty": entry["seo_difficulty"],
            "stripped_modifiers": stripped_mods,
            "suspect_volume": suspect_volume,
            "wp_impressions": wp_impressions,
            "wp_clicks": wp_clicks,
            "wp_position": wp_position,
            "wp_rank_guard": wp_rank_guard,
        })

    keywords.sort(key=lambda k: (-(k["volume"] or 0), k["query"]))
    dropped_subject.sort(key=lambda d: -(d["volume"] or 0))

    return {
        "generated_at": _now_iso(),
        "summary": {
            "total_unique_queries": len(grouped),
            "kept_keywords": len(keywords),
            "dropped_subject": len(dropped_subject),
            "dropped_subject_volume": sum(d["volume"] or 0 for d in dropped_subject),
            "dropped_empty_after_strip": dropped_empty_after_strip,
            "modifier_stripped": modifier_stripped_count,
            "wp_duplicate": wp_dup_count,
            "wp_rank_guard_flagged": wp_guard_count,
            "suspect_volume": suspect_count,
            "unique_volume_sum": sum(k["volume"] or 0 for k in keywords) + sum(
                d["volume"] or 0 for d in dropped_subject),
        },
        "skipped_files": [],  # run() が差し込む
        "dropped_subject": dropped_subject,
        "keywords": keywords,
    }


def run(
    csv_dir: pathlib.Path,
    rules_path: pathlib.Path,
    out_path: pathlib.Path,
    wp_history_path: pathlib.Path | None,
    guard_pos_max: float = DEFAULT_GUARD_POS_MAX,
    guard_min_clicks: float = DEFAULT_GUARD_MIN_CLICKS,
    dry_run: bool = False,
) -> dict[str, Any]:
    raw_rows, skipped_files = collect_csv_rows(csv_dir)
    rules = load_rules(rules_path)
    wp_rank_stats = bdk.load_wp_rank_stats(wp_history_path) if wp_history_path else {}

    excluded_sites_report = compute_excluded_sites_report(raw_rows, rules)
    excluded_site_names = set((rules.get("excluded_sites") or {}).keys())
    filtered_raw_rows = filter_excluded_site_rows(raw_rows, excluded_site_names)

    result = build(filtered_raw_rows, rules, wp_rank_stats, guard_pos_max, guard_min_clicks)
    result["skipped_files"] = skipped_files
    result["excluded_sites"] = excluded_sites_report

    s = result["summary"]
    logger.info("ユニーク需要語 %d 件 / 採用 %d 件 (skipped_files=%d)",
                s["total_unique_queries"], s["kept_keywords"], len(skipped_files))
    logger.info("  主題除外 %d 語 / Volume %d", s["dropped_subject"], s["dropped_subject_volume"])
    logger.info("  修飾語を剥いだ語 %d 件 / 剥いだ結果が空で除外 %d 件",
                s["modifier_stripped"], s["dropped_empty_after_strip"])
    logger.info("  WP 重複 %d 語 (うち順位ガード該当 %d 語)", s["wp_duplicate"], s["wp_rank_guard_flagged"])
    logger.info("  suspect_volume %d 語", s["suspect_volume"])
    if excluded_sites_report:
        logger.info("  除外サイト %d 件: %s", len(excluded_sites_report),
                    ", ".join(f"{e['site']}(rows={e['keyword_rows']})" for e in excluded_sites_report))

    if not dry_run:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        logger.info("wrote %s", out_path)
    return result


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Ubersuggest CSV を需要源として取り込む (inert, #2686 PR-C)")
    ap.add_argument("--csv-dir", default=DEFAULT_CSV_DIR)
    ap.add_argument("--rules", default=DEFAULT_RULES_PATH)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--wp-history", default=DEFAULT_WP_HISTORY_PATH,
                    help="WP (omcha.jp) の日次クエリ実績 JSONL。カニバリ計測の入力")
    ap.add_argument("--no-wp-crossmatch", action="store_true",
                    help="WP 突き合わせを無効化する")
    ap.add_argument("--guard-pos-max", type=float, default=DEFAULT_GUARD_POS_MAX)
    ap.add_argument("--guard-min-clicks", type=float, default=DEFAULT_GUARD_MIN_CLICKS)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    run(
        pathlib.Path(args.csv_dir),
        pathlib.Path(args.rules),
        pathlib.Path(args.out),
        None if args.no_wp_crossmatch else bdk.resolve_wp_history_path(args.wp_history),
        guard_pos_max=args.guard_pos_max,
        guard_min_clicks=args.guard_min_clicks,
        dry_run=args.dry_run,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
