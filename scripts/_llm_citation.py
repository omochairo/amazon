"""_llm_citation.py

LLM による回答内での引用 (Citation) および言及状況を検知・記録するための純粋関数群。

## 目的

知育玩具ナビ (navi.omcha.jp) および本家ブログ (omcha.jp) に対する
LLM 検索エンジン・モデルの引用状況 (Cited / Matched URLs / Mention without link) を
継続的に追跡・プローブするための集計・正規化・検知ロジックを提供する。
ファイル I/O、ネットワーク通信、argparse などの副作用を持たない純粋関数のみで構成される。

## 設計判断

1. GSC プロパティの系列分離 (SITES):
   `navi` (navi.omcha.jp) と `omcha` (omcha.jp) は Google Search Console (GSC) の
   プロパティが完全に異なっており、別個の時系列データとして扱わなければならない。
   ホスト判定において親ドメインとサブドメインの相互マッチ（例: omcha.jp 判定時に
   navi.omcha.jp や home.omcha.jp をマッチさせること、およびその逆）は厳格に禁止する。

2. カレンダー窓ではなく実績日窓 (select_queries):
   GSC の履歴データ (`gsc_history`) には日付の欠損や遅延 (gap) が存在しうる。
   そのため「今日から 28 日前」というカレンダー日数ではなく、データ中に実際に存在する
   最新の N 個の異なる日付 (distinct dates) を抽出窓として採用する。

3. URL 抽出と末尾記号の安全な除去 (extract_urls / normalize_host):
   Markdown のリンク形式 `[label](url)` や自由記述テキスト内の URL を抽出する際、
   ASCII の句読点・閉じ括弧 (.,!?;:'\")>]) および日本語の句読点・括弧類 (。、）】」』・…)
   を確実に除去する。ホスト名は小文字化し、ポート番号および先頭の "www." を除去して
   ドメインの完全一致で判定する。
"""
from __future__ import annotations

import re
from typing import Any, Iterable
from urllib.parse import urlsplit

# GSC プロパティごとの設定。navi と omcha は異なるプロパティであり、独立した時系列として管理する。
SITES: dict[str, dict[str, str]] = {
    "navi":  {"host": "navi.omcha.jp", "gsc_history": "data/analytics/history/gsc_by_query.jsonl"},
    "omcha": {"host": "omcha.jp",      "gsc_history": "data/analytics/history/gsc_wp_by_query.jsonl"},
}

# URL 末尾に付着しやすい ASCII / 日本語の句読点・閉じ括弧
_TRAILING_PUNCT = ".,!?;:'\")>]" + "。、）】」』・…" + "}>”’〉》〕｝"

# Markdown リンク [label](url) とプレーンテキスト内の http(s) URL を抽出する正規表現
_URL_RE = re.compile(
    r'\[(?:[^\]]*)\]\(\s*(https?://[^\s)]+?)(?:\s+["\'][^"\']*["\'])?\s*\)'
    r"|"
    r"(https?://[a-zA-Z0-9-._~:/?#\[\]@!$&'()*+,;=%]+[.,!?;:'\")>\]。、）】」』・…]*)"
)


def select_queries(
    rows: Iterable[dict[str, Any]],
    *,
    days: int = 28,
    top_n: int = 20,
    min_impressions: int = 1,
) -> list[dict[str, Any]]:
    """GSC の履歴行から最新 N 日分をクエリ単位で集計し、上位クエリを抽出する。

    - 日付窓: rows 内に実際に現れる最新の `days` 個の個別日付 (distinct dates) を対象とする。
    - 集計値: clicks (合算), impressions (合算), days_seen (出現日数), position (imp 加重平均; imp 0 の場合は None)。
    - フィルタ: 合算 impressions < min_impressions のクエリを除外。
    - ソート: (-clicks, -impressions, query) でソートし、上位 top_n 件を返す。
    - 耐障害性: query が空・欠損の行は無視し、clicks/impressions キーの欠損は 0 として扱う。
    """
    if days <= 0 or top_n <= 0:
        return []

    row_list = list(rows)
    if not row_list:
        return []

    distinct_dates = {
        r["date"]
        for r in row_list
        if isinstance(r, dict) and isinstance(r.get("date"), str) and r["date"].strip()
    }
    if not distinct_dates:
        return []

    target_dates = set(sorted(distinct_dates, reverse=True)[:days])

    clicks_map: dict[str, int] = {}
    impressions_map: dict[str, int] = {}
    dates_seen_map: dict[str, set[str]] = {}
    weighted_pos_map: dict[str, float] = {}
    pos_weight_map: dict[str, int] = {}

    for r in row_list:
        if not isinstance(r, dict):
            continue
        d = r.get("date")
        if not isinstance(d, str) or d not in target_dates:
            continue

        q_raw = r.get("query")
        if not isinstance(q_raw, str) or not q_raw.strip():
            continue
        q = q_raw.strip()

        try:
            c = int(r.get("clicks") or 0)
        except (ValueError, TypeError):
            c = 0

        try:
            imp = int(r.get("impressions") or 0)
        except (ValueError, TypeError):
            imp = 0

        clicks_map[q] = clicks_map.get(q, 0) + c
        impressions_map[q] = impressions_map.get(q, 0) + imp
        dates_seen_map.setdefault(q, set()).add(d)

        pos_raw = r.get("position")
        if pos_raw is not None and imp > 0:
            try:
                pos = float(pos_raw)
                weighted_pos_map[q] = weighted_pos_map.get(q, 0.0) + (pos * imp)
                pos_weight_map[q] = pos_weight_map.get(q, 0) + imp
            except (ValueError, TypeError):
                pass

    out: list[dict[str, Any]] = []
    for q, total_imp in impressions_map.items():
        if total_imp < min_impressions:
            continue

        total_clicks = clicks_map.get(q, 0)
        days_seen = len(dates_seen_map.get(q, set()))

        if total_imp == 0:
            position = None
        elif pos_weight_map.get(q, 0) > 0:
            position = weighted_pos_map[q] / pos_weight_map[q]
        else:
            position = None

        out.append({
            "query": q,
            "clicks": total_clicks,
            "impressions": total_imp,
            "days_seen": days_seen,
            "position": position,
        })

    out.sort(key=lambda x: (-x["clicks"], -x["impressions"], x["query"]))
    return out[:top_n]


def normalize_host(url: str) -> str | None:
    """http/https URL から小文字化・ポート除去・先頭 'www.' 除去済みのホスト名を返す。

    パース不能な値や非 http(s) スキームの場合は None を返す。
    """
    if not isinstance(url, str):
        return None
    url = url.strip()
    if not url:
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme.lower() not in ("http", "https"):
        return None
    hostname = parsed.hostname
    if not hostname:
        return None
    host = hostname.lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return None
    return host


def extract_urls(text: str | None) -> list[str]:
    """自由記述テキストから http/https URL を出現順・重複排除で抽出する。

    Markdown リンク [label](url) を処理し、末尾の ASCII および日本語の句読点・括弧を除去する。
    空文字列または None の場合は空リストを返す。
    """
    if not text or not isinstance(text, str):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for m in _URL_RE.finditer(text):
        raw = m.group(1) or m.group(2)
        if not raw:
            continue
        url = raw.rstrip(_TRAILING_PUNCT)
        if url and url.startswith(("http://", "https://")) and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _mentions_host(text: str | None, host: str) -> bool:
    """host が素のテキストとして text 中に現れるか。部分文字列一致は使わない。

    単純な `in` 判定では host "omcha.jp" が "navi.omcha.jp" / "home.omcha.jp" の
    部分文字列として一致してしまい、URL 側で厳密に分離した系列が言及判定で混ざる。
    前後に単語構成文字・`.`・`-` が来ないことを境界条件として要求する。
    """
    if not text or not isinstance(text, str):
        return False
    pattern = r"(?<![\w.-])" + re.escape(host) + r"(?![\w-])"
    return re.search(pattern, text, re.IGNORECASE) is not None


def detect_citation(
    *,
    answer_text: str | None,
    citation_urls: list[str] | None,
    host: str,
) -> dict[str, Any]:
    """回答テキストおよび明示的引用 URL から、対象ホストへの引用・言及を検知する。

    - 候補 URL: (citation_urls or []) + extract_urls(answer_text or "")
    - ホスト一致: normalize_host(url) == host の完全一致。
      navi.omcha.jp と omcha.jp は別系列の GSC プロパティであるため、
      サブドメインと親ドメインを相互にマッチさせてはならない (厳密な完全一致)。
    - matched_urls: 一致した URL の重複排除済み出現順リスト。
    - cited: bool(matched_urls)
    - mention_without_link: 未引用 (not cited) かつ host 文字列が answer_text 中に
      大文字小文字を問わず出現している場合に True。
    """
    candidate_urls = list(citation_urls or []) + extract_urls(answer_text or "")

    # サブドメインと親ドメインは双方いずれの方向にもマッチさせてはならない:
    # host "omcha.jp" に対し navi.omcha.jp や home.omcha.jp の URL はカウントせず、
    # host "navi.omcha.jp" に対し omcha.jp の URL はカウントしない。
    # これにより 2 つの独立した GSC 系列を厳密に分離する。
    matched_urls: list[str] = []
    seen: set[str] = set()
    for u in candidate_urls:
        if normalize_host(u) == host:
            if u not in seen:
                seen.add(u)
                matched_urls.append(u)

    cited = bool(matched_urls)
    mention_without_link = (not cited) and _mentions_host(answer_text, host)

    return {
        "cited": cited,
        "matched_urls": matched_urls,
        "mention_without_link": mention_without_link,
    }


def build_record(
    *,
    date: str,
    site: str,
    engine: str,
    model: str,
    query: str,
    answer_text: str | None,
    citation_urls: list[str] | None,
    latency_ms: int | float | None = None,
) -> dict[str, Any]:
    """1 回のプローブ結果を表す JSON シリアライズ可能な平坦 dict を構築する。"""
    if site not in SITES:
        raise KeyError(f"Unknown site {site!r}; expected one of {list(SITES.keys())}")

    host = SITES[site]["host"]
    det = detect_citation(
        answer_text=answer_text,
        citation_urls=citation_urls,
        host=host,
    )

    return {
        "date": date,
        "site": site,
        "engine": engine,
        "model": model,
        "query": query,
        "cited": det["cited"],
        "matched_urls": det["matched_urls"],
        "mention_without_link": det["mention_without_link"],
        "answer_chars": len(answer_text or ""),
        "citation_count": len(citation_urls or []),
        "latency_ms": latency_ms,
    }
