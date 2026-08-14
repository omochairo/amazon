#!/usr/bin/env python3
"""build_demand_keywords.py

WP の実需要クエリを Amazon SearchItems 用の検索キーワードに変換する (#2686)。

なぜ必要か (2026-08-10 実測):
  fetch_amazon.py の DEFAULT_KEYWORDS は 231 件の**供給側**リスト (ブランド名・
  年齢 × カテゴリ・素材) で、「どんなおもちゃが存在するか」から引いている。
  実測では navi は 554 記事中 417 (75%) が平均 10 位以内で**順位は取れている**
  のに、サイト全体で 376 impressions/日 しかない。誰も検索しない語で 1 位を
  取っている状態で、供給駆動の構造的な帰結。

  omcha.jp (WP 本家) の GSC には同領域で 51 倍の実需要があり、toy bucket だけで
  237,885 impressions ある (#4862 の分類レポート)。本スクリプトはそれを
  fetch_amazon.py --keywords に渡せる形に変換する。

変換の要点:
  需要クエリは情報型 (「スクイーズ どこで買える」「トミカ 収納 100均」) なので、
  そのまま SearchItems に投げても商品が返らない。data/demand_topic_terms.yaml の
  search_modifiers (買い方・評判・出来事・販売店を表す語) を落とし、商品の実体を
  指す語だけ残す。**商品カテゴリ語は落とさない** —「トミカ 収納」の「収納」を
  落とすとミニカー本体が返り、検索意図と違う商品を拾う。

供給ゲート (excluded_keywords):
  需要があっても Amazon に商品が無ければ navi の記事型 (商品ページ) にならない。
  実例としてメロジョイ (スクイーズ玩具・WP 需要 約 110,000 imp = 最大クラスタ)
  は Amazon で販売されておらず、SearchItems に投げても 0 件で API 呼び出しを
  捨てるだけになる。除外は理由つきで YAML に書き、握り潰さない。

  ここで落とせるのは「事前に分かっている」分だけである点に注意。残りの供給有無は
  実際に SearchItems を叩くまで分からない。ヒット 0 件の記録は配線側 (後続) の仕事。

情報クエリの排除 (owner 指示 2026-08-10):
  「説明が分からない情報クエリは入れず、商品名なら入れる」。判定は人手のラベルでは
  なく **供給 probe の実測** (data/analytics/demand_supply_probe.json) で行う。
  Amazon が商品を 1 件も返さなかった語を落とす。実測では 0 件 8 語がすべて変換の
  残骸 (「食玩法律」「はじめてずかん1000と1500」等) で、商品名の語は 1 つも
  0 件になっていない。--no-supply-filter で無効化できる。

本スクリプトは **inert** (外部 API を呼ばず cron にも繋がない)。出力を見てから
fetch_amazon.py への配線を決める。

WP順位ガード (dropped_wp_ranked, #2686):
  需要語の元クエリを omcha.jp の実 GSC 順位 (data/analytics/history/gsc_wp_by_query.jsonl)
  と突き合わせると、需要語90件・imp合計103,845・clicks合計13,321 (CTR 12.8%・
  imp加重平均 pos 6.2) のうち imp の 25% (26,083) が既に 1-3 位で WP が獲得済みだった。
  fetch_amazon.py の load_demand_keywords は imp 上位から機械的に20件取るため、需要枠20の
  うち約10件が WP で pos<=3・CTR 40%超の語に向いていた
  (アンパンマンシール pos 1.1/CTR 44.4%、ディズニーシートミカ pos 2.6/43.8%、
  ぷにるんずサンリオ pos 2.1/41.5%、どうぶつしょうぎ pos 1.9、ダイヤブロック pos 2.1 など)。

  Google は同一サイトの host crowding をかけるため、ここで navi の商品ページが出ると
  「露出が増える」のでなく「WP の枠が置き換わる」おそれがある。置き換わった先が商品
  ページなら CTR は落ちる公算が高い。一方 スクイーズ (18,290 imp @ pos 11.4) のような
  「需要はあるが WP が取れていない」語は純増余地で全くカニバらない。

  そこで各需要語の元クエリ (source_queries) を WP 実績と突き合わせ、
  pos<=--guard-pos-max (既定 3.0) **かつ** clicks>=--guard-min-clicks (既定 100) の語を
  除外する (dropped_wp_ranked)。pos だけで切ると低 imp の語まで巻き込むので AND にする。
  --no-rank-guard で無効化できる。gsc_wp_by_query.jsonl が無い/壊れている場合は
  fail-open (ガード無効のまま従来動作) で進み、summary に記録する。

  ただし **古いだけ** のファイルは fail-open に入らない (#5107)。2026-08-07 の #4654 で
  WP の GSC 収集は private の omochairo/omcha-ops へ移設され、public 側のこのファイルは
  2026-08-04 で凍結している。壊れていないので正常なガードとして通り、古い順位で
  カニバリ判定を続けてしまう。assert_wp_history_fresh が最終計測日を見て
  --wp-history-max-age-days (既定 8) を超えたら **中断する** (fail-closed)。

Ubersuggest 由来の需要語の合流 (#2686 PR1・2026-08-10 実測):
  WP (omcha.jp GSC) だけでなく、競合サイトの Ubersuggest 由来語も需要側の入力に
  合流する。実測パイプライン (競合9サイト CSV → L1 語彙ゲート → Amazon 実査
  → ambiguous のローカル LLM 判定) の結果:
    - ubersuggest_product_probe.json: 200 語実査、product 126 / non_product 18 /
      ambiguous 56
    - ubersuggest_llm_judge.json: ambiguous 56 語を gemma で判定、is_product 21 /
      is_not_product 35。正解ラベルとの突き合わせで正答率 83.9% / precision 85.7% /
      recall 75.0%
    - 使える語は最大 126 + 21 = 147 語
  confidence は選別に使わない: ambiguous 56 語中 54 語が confidence 0.9〜1.0 に
  張り付き、その帯の正答率は 87% で confidence の値そのものが判別に効いていない
  (2026-08-10 実測)。閾値ゲートを入れても高 confidence の誤判定は残り、低
  confidence の正しい判定を捨てるだけなので入れない。

  WP順位ガードは Ubersuggest 由来の語にも適用する。omcha.jp が既に上位で取って
  いる語なら出所に関わらずカニバるため。実測では 2,532 語中 omcha.jp と重なるのは
  44 語・うちガード該当は 9 語で影響は小さいが、原理として全ソースに通す。

  WP と Ubersuggest は単位が違う (WP: 90 日間表示回数 wp_impressions /
  Ubersuggest: 月間検索数 volume) ため、数値を直接比較して 1 本のリストに
  ソートしてはいけない。各ソース内でそれぞれ降順に並べ、1 件ずつ交互に取り出す
  「ランクのラウンドロビン」で合流する (round_robin_merge)。

  入力ファイル (ubersuggest_product_probe.json / ubersuggest_llm_judge.json) は
  どちらか欠けていても fail-open で従来動作 (WP のみ) のまま進み、summary の
  ubersuggest_missing_inputs に記録する。

使い方:
    python scripts/build_demand_keywords.py
    python scripts/build_demand_keywords.py --buckets toy,baby_goods --limit 100
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import re
import unicodedata
from datetime import date, datetime, timezone
from typing import Any

import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("build_demand_keywords")

DEFAULT_TERMS_PATH = "data/demand_topic_terms.yaml"
DEFAULT_TOPICS_PATH = "data/analytics/demand_topics.json"
DEFAULT_OUT = "data/demand_keywords.json"
DEFAULT_SUPPLY_PROBE_PATH = "data/analytics/demand_supply_probe.json"
DEFAULT_WP_QUERY_HISTORY_PATH = "data/analytics/history/gsc_wp_by_query.jsonl"
DEFAULT_UBERSUGGEST_PROBE_PATH = "data/analytics/ubersuggest_product_probe.json"
DEFAULT_UBERSUGGEST_LLM_JUDGE_PATH = "data/analytics/ubersuggest_llm_judge.json"
DEFAULT_BUCKETS = ("toy",)
# SearchItems に投げる意味のある最短長。1 文字の残骸を弾く。
MIN_KEYWORD_CHARS = 2
# WP順位ガードの既定閾値 (#2686 の docstring 参照)。
DEFAULT_GUARD_POS_MAX = 3.0
DEFAULT_GUARD_MIN_CLICKS = 100
# WP順位履歴の許容鮮度 (日)。0 で無効化。ガードは「今 WP が取れている語」を守るための
# ものなので、古い順位で判定すると守る対象がずれる (#5107 の docstring 参照)。
# 8d は check_history_freshness.py が退役まで gsc_wp_* に使っていた上限と同じ
# (日次 cadence + GSC の確定遅延 3d に余裕を足した値)。
DEFAULT_WP_HISTORY_MAX_AGE_DAYS = 8

_SPACE_RE = re.compile(r"[\s　]+")
# 末尾に残る助詞 1 文字 (修飾語を落とした残骸)。「…1000と1500の」→「…1000と1500」
_TRAILING_PARTICLE_RE = re.compile(r"[のとがはをにでもへや]+$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize(text: Any) -> str:
    if not text:
        return ""
    return _SPACE_RE.sub(" ", unicodedata.normalize("NFKC", str(text)).lower()).strip()


def normalize_key(text: Any) -> str:
    """空白を**完全に除去**した正規化キー。

    日本語クエリで空白は意味を持たないうえ、GSC のクエリは Google 側の分かち書きが
    そのまま入っていて揺れる (「はじめて ず かん 1000」「ぷにる ん ず」「危険 性」)。
    空白を残したまま扱うと (1) 修飾語が分断されて落ちない (「危険 性」に「危険性」が
    当たらない)、(2) 同一語が別集計になる (「トミカ 収納」21,365 と「トミカ収納」
    8,056) の 2 つを同時に踏む。2026-08-10 に実データで両方踏んで空白除去に変えた。
    """
    return _SPACE_RE.sub("", normalize(text))


def load_vocab(terms_path: pathlib.Path) -> tuple[list[str], list[dict[str, Any]]]:
    """(search_modifiers, excluded_keywords) を返す。

    modifiers は**長い順**に並べて返す。「どこで売ってる」を先に落とさないと
    「売ってる」だけ消えて「どこで」が残るため。
    """
    data = yaml.safe_load(terms_path.read_text(encoding="utf-8")) or {}
    mods = [normalize_key(m) for m in (data.get("search_modifiers") or [])]
    mods = sorted({m for m in mods if m}, key=len, reverse=True)
    excluded = data.get("excluded_keywords") or []
    return mods, excluded


def build_excluded_terms(excluded: list[dict[str, Any]]) -> dict[str, str]:
    """正規化済みの除外語 -> 理由 の辞書。keyword 本体と aliases の両方を張る。"""
    out: dict[str, str] = {}
    for e in excluded:
        if not isinstance(e, dict):
            continue
        reason = (e.get("reason") or "").strip()
        for name in [e.get("keyword")] + list(e.get("aliases") or []):
            n = normalize_key(name)
            if n:
                out[n] = reason
    return out


def to_search_keyword(query: str, modifiers: list[str]) -> str:
    """需要クエリから修飾語を落として検索語にする。

    空白を除去した正規化キー上で処理する (normalize_key の docstring 参照)。
    modifiers は長い順に適用される前提 (load_vocab がそう並べる)。記号と、修飾語を
    落とした残骸の末尾助詞を掃除し、短すぎるものは空文字を返して呼び出し側で捨てる。
    """
    s = normalize_key(query)
    for m in modifiers:
        if m in s:
            s = s.replace(m, "")
    s = re.sub(r"[)(（）・:：\-–—…!！?？'\"”“'’+&/／,、]+", "", s)
    s = _TRAILING_PARTICLE_RE.sub("", s).strip()
    if len(s) < MIN_KEYWORD_CHARS:
        return ""
    return s


def load_zero_supply_keywords(probe_path: pathlib.Path | None) -> set[str]:
    """供給 probe で hits=0 だった検索語 (= 商品名として通らなかった語) を返す。

    「情報クエリを入れず、商品名だけ入れる」(owner 指示 2026-08-10) の機械的な判定に
    probe の実測を使う。人手のラベルではなく Amazon が商品を返すかどうかで決める。

    2026-08-10 の実測 (98 語) では 0 件が 8 語あり、すべて変換の残骸だった:
      はじめてずかん1000と1500 / ジョブロイド名前 / 食玩法律 / 食玩にするメリット /
      食玩メリット / 中古絵本買わない / 点つなぎ難しい300 / はじめてずかん15001000
    いずれも情報クエリで商品名ではない。逆に商品名の語は 1 つも 0 件になっていない。

    **API エラーだった語は落とさない** (供給が無いのではなく測れていない)。
    probe に載っていない語も落とさない (新しい需要は未測定なので通す)。
    probe が無ければ空集合を返し、フィルタは一切かからない。
    """
    if probe_path is None or not probe_path.exists():
        return set()
    try:
        data = json.loads(probe_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("supply probe を読めないのでフィルタしない: %s", e)
        return set()
    out: set[str] = set()
    for r in data.get("results") or []:
        if not isinstance(r, dict):
            continue
        if r.get("error"):
            continue
        if r.get("hits") == 0 and isinstance(r.get("keyword"), str):
            out.add(normalize_key(r["keyword"]))
    return out


def load_wp_rank_stats(history_path: pathlib.Path | None) -> dict[str, dict[str, float]]:
    """omcha.jp (WP) の日次クエリ実績を集計する。

    gsc_wp_by_query.jsonl は 1 行 1 レコード (1 クエリ×1日) の JSONL。
    クエリ単位 (normalize_key) で全期間の impressions・clicks を合算し、
    position は **impression 加重平均**で出す (日次 position の単純平均は誤り。
    日によって imp が大きく偏るため、単純平均だと閑散日の順位に引きずられる)。

    ファイルが無い/壊れている場合は空 dict を返し、呼び出し側は fail-open する。
    """
    if history_path is None:
        return {}
    if not history_path.exists():
        logger.warning("WP順位履歴 %s が無いのでガードを適用しない (fail-open)", history_path)
        return {}
    agg: dict[str, dict[str, float]] = {}
    try:
        with history_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                q = normalize_key(row.get("query"))
                if not q:
                    continue
                imp = row.get("impressions") or 0
                clicks = row.get("clicks") or 0
                pos = row.get("position") or 0
                e = agg.setdefault(q, {"imp": 0.0, "clicks": 0.0, "pos_weighted_sum": 0.0})
                e["imp"] += imp
                e["clicks"] += clicks
                e["pos_weighted_sum"] += pos * imp
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("WP順位履歴を読めないのでガードを適用しない (fail-open): %s", e)
        return {}

    out: dict[str, dict[str, float]] = {}
    for q, e in agg.items():
        out[q] = {
            "imp": e["imp"],
            "clicks": e["clicks"],
            "pos": (e["pos_weighted_sum"] / e["imp"]) if e["imp"] else 0.0,
        }
    return out


def wp_history_last_date(history_path: pathlib.Path | None) -> str | None:
    """WP順位履歴の最終計測日 (YYYY-MM-DD) を返す。読めなければ None。

    load_wp_rank_stats とは別パスにしてある。あちらの戻り値の型は
    ingest_ubersuggest.py が再利用しているので変えない (#2686 PR1)。
    """
    if history_path is None or not history_path.exists():
        return None
    last: str | None = None
    try:
        with history_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                value = json.loads(line).get("date")
                if isinstance(value, str) and len(value) >= 10:
                    day = value[:10]
                    if last is None or day > last:
                        last = day
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("WP順位履歴の日付を読めない: %s", e)
        return None
    return last


def assert_wp_history_fresh(history_path: pathlib.Path | None, max_age_days: int,
                            today: date | None = None) -> str | None:
    """WP順位履歴が古すぎたら **止める** (fail-closed)。最終計測日を返す。

    なぜ fail-open にしないか (#5107):
      2026-08-07 の #4654 で WP (omcha.jp) の GSC 収集は private の omochairo/omcha-ops
      へ移設され、public 側の gsc_wp_by_query.jsonl は 2026-08-04 で凍結した。
      ファイルは壊れておらず**ただ古いだけ**なので、load_wp_rank_stats の
      「無い / 壊れている」fail-open のどちらにも入らず、正常なガードとして通ってしまう。

      ガードは host crowding によるカニバリ回避が目的なので、順位が古いと
      両方向に静かに外れる: WP が既に落とした語を除外し続ける (機会損失) 一方、
      WP が新たに取った語は除外できない (カニバリ)。どちらも出力を見ても分からない。
      ここは「黙って間違った答えを出す」より「止まって人に決めさせる」が正しい。

    ガード無効時 (--no-rank-guard) と max_age_days<=0 では何も見ない。
    """
    if max_age_days <= 0:
        return None
    last = wp_history_last_date(history_path)
    if last is None:
        # 無い / 壊れている は従来どおり load_wp_rank_stats 側の fail-open に委ねる。
        return None
    age = ((today or datetime.now(timezone.utc).date()) - date.fromisoformat(last)).days
    if age > max_age_days:
        raise SystemExit(
            f"WP順位履歴 {history_path} の最終計測日が {last} ({age}d 前) で "
            f"上限 {max_age_days}d を超えている。古い順位でガードすると "
            f"カニバリ回避が静かに外れるので中断する (#5107)。\n"
            f"  - 収集は omochairo/omcha-ops (private) の data/gsc/ に移設済み。"
            f"そこから最新の gsc_wp_by_query.jsonl を持ってきて --wp-history に渡すか、\n"
            f"  - ガードの是非を判断した上で --no-rank-guard "
            f"(または --wp-history-max-age-days 0) を明示すること")
    return last


def load_ubersuggest_keywords(
    probe_path: pathlib.Path | None,
    llm_judge_path: pathlib.Path | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Ubersuggest 由来の需要語のうち商品語だけを集める (#2686 PR1)。

    2 つの入力ソースを統合する:
      - ubersuggest_product_probe.json: verdict == "product" (L1 実査で商品と
        確定した語)
      - ubersuggest_llm_judge.json: is_product_query == true (ambiguous 56 語を
        gemma で判定した結果)
    2026-08-10 実測: probe 200 語中 product 126 / non_product 18 / ambiguous 56。
    ambiguous 56 語の LLM 判定は正答率 83.9% / precision 85.7% / recall 75.0%。
    confidence は使わない (モジュール docstring 参照。選別に使える精度が無い)。

    重複 (両ソースに同じ語が出るケース) は normalize_key で排除し 1 件にまとめる。
    Amazon に投げる keyword は **raw_query** (空白を保持した元表記) を使う。
    実査でヒットを確認したのはこの表記であり、query は重複排除用に空白を
    完全除去した別物なので検索語には使わない。

    入力ファイルが無い/壊れている場合はそのソースを fail-open でスキップし、
    2 つ目の戻り値 (missing) にラベルを積む。両方欠けていれば空リストを返し、
    呼び出し側は WP のみの従来動作のまま進む。
    """
    out: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    sources = (("probe", probe_path), ("llm_judge", llm_judge_path))
    for label, path in sources:
        if path is None or not path.exists():
            missing.append(label)
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("ubersuggest %s を読めないのでスキップ (fail-open): %s", label, e)
            missing.append(label)
            continue
        for r in data.get("results") or []:
            if not isinstance(r, dict):
                continue
            if label == "probe":
                is_product = r.get("verdict") == "product"
            else:
                is_product = r.get("is_product_query") is True
            if not is_product:
                continue
            query = r.get("query") or ""
            raw_query = (r.get("raw_query") or query or "").strip()
            key = normalize_key(query)
            if not key or not raw_query:
                continue
            entry = out.setdefault(key, {
                "keyword": raw_query,
                "volume": 0,
                "sites": [],
                "source_queries": [],
                "source": "ubersuggest",
            })
            volume = r.get("volume") or 0
            entry["volume"] = max(entry["volume"], volume)
            for s in (r.get("sites") or []):
                if s not in entry["sites"]:
                    entry["sites"].append(s)
            if query not in entry["source_queries"]:
                entry["source_queries"].append(query)
    return list(out.values()), missing


def round_robin_merge(a: list[dict[str, Any]], b: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """2 つの降順ソート済みリストを 1 件ずつ交互に取り出して合流する (#2686 PR1)。

    WP (wp_impressions = 90 日間表示回数) と Ubersuggest (volume = 月間検索数) は
    単位が違うので、数値の大小を直接比較して 1 本のリストにソートしてはいけない
    (imp 数千〜数万 vs volume は月間検索数のオーダーが違い、比較すれば常に片方の
    ソースに支配される)。各ソース内の「良い順」だけを信用し、出所を均等に混ぜる
    ラウンドロビンで合流する。a・b はどちらも呼び出し側で既に降順ソート済みの前提。
    """
    merged: list[dict[str, Any]] = []
    for i in range(max(len(a), len(b))):
        if i < len(a):
            merged.append(a[i])
        if i < len(b):
            merged.append(b[i])
    return merged


def apply_rank_guard(
    entries: list[dict[str, Any]],
    wp_rank_stats: dict[str, dict[str, float]],
    guard_pos_max: float,
    guard_min_clicks: float,
    rank_guard_enabled: bool,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """WPランクガードを 1 組の需要語エントリ群に適用する (#2686 PR1 で共通化)。

    元は WP 由来の語にしか効いていなかったが、omcha.jp が既にその語の元クエリで
    上位表示を得ているなら、需要語の出所 (WP/Ubersuggest) を問わずカニバる。
    entries は {"keyword", "source_queries", ...} を持つ辞書のリスト。各 entry の
    source_queries を WP 実績 (wp_rank_stats) と突き合わせ、
    pos<=guard_pos_max **かつ** clicks>=guard_min_clicks なら落とす。
    """
    if not rank_guard_enabled or not wp_rank_stats:
        return entries, {}
    kept: list[dict[str, Any]] = []
    dropped: dict[str, dict[str, Any]] = {}
    for entry in entries:
        imp_sum = 0.0
        clicks_sum = 0.0
        pos_weighted_sum = 0.0
        seen_norm: set[str] = set()
        for q in entry["source_queries"]:
            nk = normalize_key(q)
            # 空白揺れ違いの重複 source_query を二重加算しない (build() の既存ロジックと同じ)。
            if nk in seen_norm:
                continue
            seen_norm.add(nk)
            stat = wp_rank_stats.get(nk)
            if not stat:
                continue
            imp_sum += stat["imp"]
            clicks_sum += stat["clicks"]
            pos_weighted_sum += stat["pos"] * stat["imp"]
        if imp_sum <= 0:
            kept.append(entry)
            continue
        pos = pos_weighted_sum / imp_sum
        if pos <= guard_pos_max and clicks_sum >= guard_min_clicks:
            dropped[entry["keyword"]] = {
                "keyword": entry["keyword"],
                "source": entry.get("source", "unknown"),
                "wp_impressions": round(imp_sum, 1),
                "wp_clicks": round(clicks_sum, 1),
                "wp_position": round(pos, 1),
                "reason": (
                    f"omcha.jp が既にこの語の元クエリで pos {round(pos, 1)}"
                    f" (clicks {round(clicks_sum)}) を獲得済み。navi の商品ページを"
                    f"出すと host crowding で WP の枠を食い合う恐れがあるため除外"
                    f" (guard_pos_max={guard_pos_max}, guard_min_clicks={guard_min_clicks})。"
                ),
            }
        else:
            kept.append(entry)
    return kept, dropped


def build(
    topics: dict[str, Any],
    modifiers: list[str],
    excluded_terms: dict[str, str],
    buckets: tuple[str, ...],
    limit: int = 0,
    zero_supply: set[str] | None = None,
    wp_rank_stats: dict[str, dict[str, float]] | None = None,
    guard_pos_max: float = DEFAULT_GUARD_POS_MAX,
    guard_min_clicks: float = DEFAULT_GUARD_MIN_CLICKS,
    rank_guard_enabled: bool = True,
    ubersuggest_keywords: list[dict[str, Any]] | None = None,
    ubersuggest_missing_inputs: list[str] | None = None,
) -> dict[str, Any]:
    """検索語ごとに需要 impressions/volume を合算したランキングを返す。

    同じ検索語に落ちる需要クエリ (「メロジョイ 偽物」「メロジョイ 定価」) は
    1 件にまとめ、impressions を合算する。合算するのは**同一サイト内の同一語**
    なので意味が通る (別サイトの合算をしないのとは別の話)。

    WP (wp_impressions) と Ubersuggest (volume) の 2 ソースは round_robin_merge で
    合流する (#2686 PR1、理由はモジュール docstring・round_robin_merge 参照)。
    """
    agg: dict[str, dict[str, Any]] = {}
    skipped_excluded: dict[str, dict[str, Any]] = {}
    dropped_no_supply: dict[str, dict[str, Any]] = {}
    zero_supply = zero_supply or set()
    skipped_empty = 0

    for row in topics.get("rows") or []:
        if row.get("bucket") not in buckets:
            continue
        query = row.get("query") or ""
        imp = row.get("wp_impressions") or 0

        hit = next((t for t in excluded_terms if t in normalize_key(query)), None)
        if hit:
            e = skipped_excluded.setdefault(
                hit, {"term": hit, "reason": excluded_terms[hit], "queries": 0, "wp_impressions": 0})
            e["queries"] += 1
            e["wp_impressions"] += imp
            continue

        kw = to_search_keyword(query, modifiers)
        if not kw:
            skipped_empty += 1
            continue

        if normalize_key(kw) in zero_supply:
            d = dropped_no_supply.setdefault(
                kw, {"keyword": kw, "wp_impressions": 0, "source_queries": []})
            d["wp_impressions"] += imp
            d["source_queries"].append(query)
            continue

        entry = agg.setdefault(
            kw, {"keyword": kw, "wp_impressions": 0, "source_queries": [], "source": "wp_gsc"})
        entry["wp_impressions"] += imp
        entry["source_queries"].append(query)

    wp_rank_stats = wp_rank_stats or {}
    ubersuggest_keywords = ubersuggest_keywords or []
    ubersuggest_missing_inputs = ubersuggest_missing_inputs or []

    # WPランクガード (#2686): 需要語の元クエリ (source_queries) を WP 実績と突き合わせ、
    # pos<=guard_pos_max かつ clicks>=guard_min_clicks の語を落とす。
    # WP由来・Ubersuggest由来の両方に同じガードを適用する (apply_rank_guard で共通化)。
    wp_entries_sorted = sorted(agg.values(), key=lambda e: (-e["wp_impressions"], e["keyword"]))
    wp_kept, dropped_wp_ranked = apply_rank_guard(
        wp_entries_sorted, wp_rank_stats, guard_pos_max, guard_min_clicks, rank_guard_enabled)

    uber_entries_sorted = sorted(
        ubersuggest_keywords, key=lambda e: (-e["volume"], e["keyword"]))
    uber_kept, dropped_uber_ranked = apply_rank_guard(
        uber_entries_sorted, wp_rank_stats, guard_pos_max, guard_min_clicks, rank_guard_enabled)

    dropped_wp_ranked_all: dict[str, dict[str, Any]] = {**dropped_wp_ranked, **dropped_uber_ranked}

    # 単位が違う (imp vs volume) ので数値比較で1本のリストにソートせず、ラウンドロビンで合流する。
    keywords = round_robin_merge(wp_kept, uber_kept)
    if limit > 0:
        keywords = keywords[:limit]

    return {
        "generated_at": _now_iso(),
        "params": {"buckets": list(buckets), "limit": limit},
        "summary": {
            "keywords": len(keywords),
            "wp_impressions": sum(k.get("wp_impressions", 0) for k in keywords),
            "excluded_terms": len(skipped_excluded),
            "excluded_wp_impressions": sum(e["wp_impressions"] for e in skipped_excluded.values()),
            "dropped_empty_after_strip": skipped_empty,
            "dropped_no_supply": len(dropped_no_supply),
            "dropped_no_supply_wp_impressions": sum(
                d["wp_impressions"] for d in dropped_no_supply.values()),
            "dropped_wp_ranked": len(dropped_wp_ranked_all),
            "dropped_wp_ranked_clicks": round(
                sum(d["wp_clicks"] for d in dropped_wp_ranked_all.values()), 1),
            "wp_keywords": len(wp_kept),
            "ubersuggest_keywords": len(uber_kept),
            "ubersuggest_volume": sum(k.get("volume", 0) for k in uber_kept),
            "ubersuggest_dropped_wp_ranked": len(dropped_uber_ranked),
            "ubersuggest_missing_inputs": ubersuggest_missing_inputs,
        },
        "excluded": sorted(skipped_excluded.values(), key=lambda e: -e["wp_impressions"]),
        "dropped_no_supply": sorted(dropped_no_supply.values(),
                                    key=lambda d: -d["wp_impressions"]),
        "dropped_wp_ranked": sorted(dropped_wp_ranked_all.values(),
                                    key=lambda d: -d["wp_impressions"]),
        "keywords": keywords,
    }


def run(terms_path: pathlib.Path, topics_path: pathlib.Path, out_path: pathlib.Path,
        buckets: tuple[str, ...], limit: int, dry_run: bool = False,
        supply_probe_path: pathlib.Path | None = None,
        wp_history_path: pathlib.Path | None = None,
        guard_pos_max: float = DEFAULT_GUARD_POS_MAX,
        guard_min_clicks: float = DEFAULT_GUARD_MIN_CLICKS,
        rank_guard_enabled: bool = True,
        ubersuggest_probe_path: pathlib.Path | None = None,
        ubersuggest_llm_judge_path: pathlib.Path | None = None,
        wp_history_max_age_days: int = DEFAULT_WP_HISTORY_MAX_AGE_DAYS) -> dict[str, Any]:
    modifiers, excluded = load_vocab(terms_path)
    topics = json.loads(topics_path.read_text(encoding="utf-8"))
    zero_supply = load_zero_supply_keywords(supply_probe_path)
    wp_history_last = (assert_wp_history_fresh(wp_history_path, wp_history_max_age_days)
                       if rank_guard_enabled else None)
    wp_rank_stats = load_wp_rank_stats(wp_history_path) if rank_guard_enabled else {}
    ubersuggest_keywords, ubersuggest_missing = load_ubersuggest_keywords(
        ubersuggest_probe_path, ubersuggest_llm_judge_path)
    result = build(topics, modifiers, build_excluded_terms(excluded), buckets, limit,
                   zero_supply=zero_supply, wp_rank_stats=wp_rank_stats,
                   guard_pos_max=guard_pos_max, guard_min_clicks=guard_min_clicks,
                   rank_guard_enabled=rank_guard_enabled,
                   ubersuggest_keywords=ubersuggest_keywords,
                   ubersuggest_missing_inputs=ubersuggest_missing)

    s = result["summary"]
    logger.info("検索キーワード %d 件 / 需要 %d imp (buckets=%s)",
                s["keywords"], s["wp_impressions"], ",".join(buckets))
    logger.info("  除外 %d 語 / %d imp (Amazon 非販売など)",
                s["excluded_terms"], s["excluded_wp_impressions"])
    logger.info("  修飾語を落として空になった需要クエリ: %d 件", s["dropped_empty_after_strip"])
    logger.info("  供給 probe で 0 件だった語 (商品名として通らない): %d 語 / %d imp",
                s["dropped_no_supply"], s["dropped_no_supply_wp_impressions"])
    if rank_guard_enabled and not wp_rank_stats:
        logger.warning("  WP順位ガード: 履歴データが無いため無効 (fail-open)")
    else:
        logger.info("  WP順位ガードで除外 %d 語 (WP保護クリック %d, WP順位履歴 最終 %s)",
                    s["dropped_wp_ranked"], s["dropped_wp_ranked_clicks"],
                    wp_history_last or "-")
    logger.info("  Ubersuggest 由来 %d 語 (volume 合計 %d, ガード除外 %d)",
                s["ubersuggest_keywords"], s["ubersuggest_volume"],
                s["ubersuggest_dropped_wp_ranked"])
    if s["ubersuggest_missing_inputs"]:
        logger.warning("  Ubersuggest 入力欠損 (fail-open): %s",
                       ",".join(s["ubersuggest_missing_inputs"]))

    if not dry_run:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
        logger.info("wrote %s", out_path)
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="需要クエリを Amazon 検索キーワードに変換する (#2686)")
    ap.add_argument("--terms", default=DEFAULT_TERMS_PATH)
    ap.add_argument("--topics", default=DEFAULT_TOPICS_PATH)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--buckets", default=",".join(DEFAULT_BUCKETS),
                    help="対象 bucket の CSV (既定 toy)。範囲決定が変わったらここを変える")
    ap.add_argument("--limit", type=int, default=0, help="出力する検索語の上限 (0=全件)")
    ap.add_argument("--supply-probe", default=DEFAULT_SUPPLY_PROBE_PATH,
                    help="供給 probe レポート。hits=0 の語 (商品名として通らない情報クエリ) を落とす")
    ap.add_argument("--no-supply-filter", action="store_true",
                    help="供給 probe によるフィルタを無効化する")
    ap.add_argument("--wp-history", default=DEFAULT_WP_QUERY_HISTORY_PATH,
                    help="WP (omcha.jp) の日次クエリ実績 JSONL。WP順位ガードの入力")
    ap.add_argument("--guard-pos-max", type=float, default=DEFAULT_GUARD_POS_MAX,
                    help="WP順位ガードの順位しきい値 (これ以下で保護対象、既定 3.0)")
    ap.add_argument("--guard-min-clicks", type=float, default=DEFAULT_GUARD_MIN_CLICKS,
                    help="WP順位ガードのクリック数しきい値 (これ以上で保護対象、既定 100)")
    ap.add_argument("--no-rank-guard", action="store_true",
                    help="WP順位ガードを無効化する")
    ap.add_argument("--wp-history-max-age-days", type=int,
                    default=DEFAULT_WP_HISTORY_MAX_AGE_DAYS,
                    help="WP順位履歴の許容鮮度 (日, 既定 8)。超えたら中断する (0=検査しない)")
    ap.add_argument("--ubersuggest-probe", default=DEFAULT_UBERSUGGEST_PROBE_PATH,
                    help="Ubersuggest 語の Amazon 実査 probe (#2686 PR1)。verdict==product を合流")
    ap.add_argument("--ubersuggest-llm-judge", default=DEFAULT_UBERSUGGEST_LLM_JUDGE_PATH,
                    help="Ubersuggest ambiguous 語の LLM 判定。is_product_query==true を合流")
    ap.add_argument("--no-ubersuggest", action="store_true",
                    help="Ubersuggest 由来の需要語合流を無効化する (WP のみの従来動作)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    buckets = tuple(b.strip() for b in args.buckets.split(",") if b.strip())
    run(pathlib.Path(args.terms), pathlib.Path(args.topics), pathlib.Path(args.out),
        buckets, args.limit, args.dry_run,
        supply_probe_path=(None if args.no_supply_filter else pathlib.Path(args.supply_probe)),
        wp_history_path=pathlib.Path(args.wp_history),
        guard_pos_max=args.guard_pos_max,
        guard_min_clicks=args.guard_min_clicks,
        rank_guard_enabled=not args.no_rank_guard,
        ubersuggest_probe_path=(
            None if args.no_ubersuggest else pathlib.Path(args.ubersuggest_probe)),
        ubersuggest_llm_judge_path=(
            None if args.no_ubersuggest else pathlib.Path(args.ubersuggest_llm_judge)),
        wp_history_max_age_days=args.wp_history_max_age_days)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
