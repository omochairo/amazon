#!/usr/bin/env python3
"""国立国会図書館サーチ (NDL Search) API から、知育・発達・食育・安全の参考文献を取得する。

epic #1358 B-9。著者プロフィール (/author/iromama, /author/iropapa) に「編集部が参考に
している公開書籍」を出典 link 付きで掲載し、E-E-A-T (専門性・権威性) を補強する。

設計:
- build_post から呼ばない (描画専用原則)。事前生成スクリプトとして単独実行し
  hugo/data/author_references.json を出力する。
- NDL OpenSearch は関連度ソートでないため「topic クエリ → 最新の実書籍を1冊」方式で
  選定する (定期刊行物・紀要等は除外)。リンクは NDL の書誌詳細 permalink。
- 認証不要。stdlib のみ (urllib + xml)。

Usage: python scripts/fetch_ndl_references.py
"""
from __future__ import annotations

import json
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

API = "https://ndlsearch.ndl.go.jp/api/opensearch"
OUT = Path(__file__).resolve().parents[1] / "hugo" / "data" / "author_references.json"

# persona 別に「編集部が参照する古典・標準的書籍」を (query, match, label) で curate する。
# query=NDL 検索語 / match=結果タイトルに必須の漢字部分列 (論叢・紀要・ローマ字読み
# 書誌を自動排除する品質ゲート) / label=表示用の領域ラベル。
TOPICS = {
    "iropapa": [
        ("知能の心理学", "知能の心理学", "認知発達の理論（J. ピアジェ）"),
        ("モンテッソーリ教育", "モンテッソーリ", "モンテッソーリ教育"),
        ("非認知能力", "非認知", "非認知能力（社会情動的スキル）の育ち"),
    ],
    "iromama": [
        ("子どもの食と栄養", "子どもの食と栄養", "子どもの食と栄養"),
        ("保育所保育指針解説", "保育所保育指針", "保育所保育指針（発達の道すじ）"),
        ("よくわかる発達心理学", "発達心理学", "乳幼児の発達心理学"),
    ],
}

# 定期刊行物・雑誌・紀要・論文 (古典書籍の「再読/分析/特集」等) を除外する語。
# E-E-A-T 上、解説書・古典そのものを優先し、二次的な論考は外す。
SKIP_RE = re.compile(
    r"(年報|紀要|だより|会報|月報|学会誌|ニュース|雑誌|広報|目録|論叢|論集"
    r"|再読|言説|特集|における|に於ける|於ける|序説|試論|書評)")


def fetch(query: str, cnt: int = 80) -> list[dict]:
    url = f"{API}?{urllib.parse.urlencode({'title': query, 'cnt': cnt})}"
    req = urllib.request.Request(url, headers={"User-Agent": "omochairo-ndl/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    root = ET.fromstring(raw)
    ns = {"dc": "http://purl.org/dc/elements/1.1/",
          "dcterms": "http://purl.org/dc/terms/"}
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        creator = (item.findtext("dc:creator", namespaces=ns) or "").strip()
        issued = (item.findtext("dcterms:issued", namespaces=ns) or "").strip()
        if not title or "/books/" not in link:
            continue
        if SKIP_RE.search(title):
            continue
        year = None
        m = re.search(r"(\d{4})", issued)
        if m:
            year = int(m.group(1))
        out.append({"title": title, "creator": creator, "year": year, "ndl_url": link})
    return out


def pick_recent(cands: list[dict]) -> dict | None:
    """書籍を1冊選ぶ。NDL 蔵書 (図書) の書誌を CiNii 論文等より優先し、
    creator・発行年があるもののうち最新を採る。"""
    if not cands:
        return None
    # NDL 図書カタログ (R100000001 / R100000002) を CiNii 論文 (R100000136) より優先
    books = [c for c in cands if re.search(r"/R10000000[12]-", c["ndl_url"])]
    pool = books or cands
    scored = [c for c in pool if c["creator"] and c["year"]]
    if not scored:
        scored = [c for c in pool if c["year"]] or pool
    return max(scored, key=lambda c: c["year"] or 0)


def main() -> int:
    result: dict = {
        "_meta": {
            "source_name": "国立国会図書館サーチ (NDL Search)",
            "source_url": "https://ndlsearch.ndl.go.jp/",
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "note": "編集部が知育・発達・食育・安全の記述で参考にしている公開書籍。各リンクは国立国会図書館サーチの書誌詳細です。",
        }
    }
    for persona, topics in TOPICS.items():
        refs = []
        for query, match, label in topics:
            try:
                cands = fetch(query)
            except Exception as exc:  # noqa: BLE001
                print(f"WARN {persona} '{query}': {exc}", file=sys.stderr)
                continue
            cands = [c for c in cands if match in c["title"]]
            best = pick_recent(cands)
            if not best:
                print(f"WARN {persona} '{query}': no candidate", file=sys.stderr)
                continue
            refs.append({
                "label": label,
                "title": best["title"],
                "creator": best["creator"],
                "year": best["year"],
                "ndl_url": best["ndl_url"],
            })
            print(f"OK {persona} '{query}' -> {best['title']} ({best['year']})")
        result[persona] = refs
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
