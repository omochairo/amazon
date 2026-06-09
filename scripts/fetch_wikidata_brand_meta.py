#!/usr/bin/env python3
"""fetch_wikidata_brand_meta.py — #1358 B-6

ブランドハブ (/brands/<brand>/) の Organization JSON-LD と可視メタパネルを
充填するため、Wikidata から各ブランドの構造化メタを取得しサイドカー
`hugo/data/brand_wikidata.json` に保存する。

取得経路は **MediaWiki Action API** (www.wikidata.org/w/api.php) を使う。
SPARQL (WDQS) は障害時に 1req/min へ絞られ不安定なため不採用。Action API は
認証不要・障害の影響を受けにくく rate limit も緩い。

  1. wbsearchentities でブランド名 → 候補エンティティ (ja→en の順で照合)
  2. wbgetentities(claims) で P31/P571/P856/P154/P112/P159/P17 を取得
  3. 組織型 (P31 ∩ allowlist もしくは公式サイト/本社を持つ) のみ採用、人物等は除外
  4. 参照 QID (創業者/本社/国) のラベルを wbgetentities(labels) で一括解決

取得項目 (取れたものだけ): qid / inception(年) / website / logo / founder / hq / country

build_post 同様 in-band IO は禁止 ([[feedback-omochairo-build-post-no-inband-io]])。
描画前の事前生成専用で cron / 手動で回す。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRAND_HUB = ROOT / "hugo" / "data" / "brand_hub.json"
OUT = ROOT / "hugo" / "data" / "brand_wikidata.json"
API = "https://www.wikidata.org/w/api.php"
UA = "omochairo-brand-meta/1.0 (https://navi.omcha.jp; brand E-E-A-T enrichment)"

# P31 (instance of) として「企業/ブランド」と見なす QID 集合。これに **厳格一致**
# したエンティティだけ採用する。website を持つだけの雑誌 (Q12340140) / 新聞
# (Q11032) / 自治体 / 町などの誤マッチを構造的に弾くため (実測で People 誌・
# Marca 紙・Romanian municipality・UK town が混入したのを受けて厳格化)。
ORG_ALLOW = {
    "Q4830453",   # business / 企業
    "Q6881511",   # enterprise / 事業者
    "Q891723",    # public company / 公開会社
    "Q5621421",   # privately held company
    "Q43229",     # organization
    "Q431289",    # brand
    "Q18388277",  # technology company
    "Q210167",    # video game developer
    "Q1137109",   # video game publisher
    "Q2085381",   # publisher / 出版社
    "Q167037",    # corporation
    "Q161726",    # multinational corporation
    "Q1002954",   # company
    "Q783794",    # company (汎用)
}
HUMAN = "Q5"

# 同名・別業種の誤マッチを人手で除外する curated 上書き。auto-match が「企業型」
# に一致しても業種が異なる (例: ゴリアテ=独自動車 / Osmo=仏レコードレーベル /
# ジーピー=ソニー・ミュージック) ものは None で skip する。
# 正しいエンティティが Wikidata に確認できれば QID 文字列で pin も可能。
BRAND_OVERRIDES: dict[str, str | None] = {
    "Goliath": None,   # Q565128 はドイツの自動車メーカー Goliath-Werke
    "ジーピー": None,   # Q732503 はソニー・ミュージック (音楽会社)
    # session 138: 104 ブランドへの refresh で auto-match が拾った同名別業種を是正。
    "グリムス": "Q5609278",  # 正: Grimm's Spiel und Holz Design (独・木製玩具)。
                              # auto-match は同名の gremz.co.jp (日本の環境/IT 企業) に誤マッチしていた。
    "VILAC": None,     # Q7930050 は韓国の乳業会社。仏 Vilac (木製玩具) は Wikidata に企業 entity 無し。
    "キューブ": None,   # Q442816 は独 Cube (自転車メーカー)。日本のキューブ (オタマトーン) は entity 無し。
    "トーヨー": None,   # Q53268 はトヨタ自動車 (founder=豊田喜一郎)。折り紙メーカー トーヨー は別。
}


def api_get(params: dict) -> dict:
    params = {**params, "format": "json"}
    url = API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                return json.load(resp)
        except Exception as e:  # noqa: BLE001
            if attempt == 3:
                raise
            time.sleep(2 * (attempt + 1))
    return {}


def search_candidates(brand: str) -> list[str]:
    """ja→en の順で候補 QID を検索順のまま重複排除して返す。"""
    seen: list[str] = []
    for lang in ("ja", "en"):
        d = api_get({"action": "wbsearchentities", "search": brand,
                     "language": lang, "uselang": lang, "type": "item", "limit": 7})
        for h in d.get("search", []):
            if h["id"] not in seen:
                seen.append(h["id"])
    return seen


def _claim_value(claims: dict, pid: str) -> dict | None:
    arr = claims.get(pid)
    if not arr:
        return None
    snak = arr[0].get("mainsnak", {})
    return snak.get("datavalue", {}).get("value")


def _claim_qid(claims: dict, pid: str) -> str | None:
    v = _claim_value(claims, pid)
    if isinstance(v, dict) and "id" in v:
        return v["id"]
    return None


def fetch_labels(qids: list[str]) -> dict:
    if not qids:
        return {}
    d = api_get({"action": "wbgetentities", "ids": "|".join(qids),
                 "props": "labels", "languages": "ja|en"})
    out = {}
    for qid, ent in d.get("entities", {}).items():
        labels = ent.get("labels", {})
        lab = labels.get("ja") or labels.get("en")
        if lab:
            out[qid] = lab["value"]
    return out


def _p31_set(claims: dict) -> set:
    return {c.get("mainsnak", {}).get("datavalue", {}).get("value", {}).get("id")
            for c in claims.get("P31", [])}


def build_record(brand: str) -> dict | None:
    if brand in BRAND_OVERRIDES:
        pinned = BRAND_OVERRIDES[brand]
        if pinned is None:
            return None  # curated skip (同名別業種)
        candidates = [pinned]
    else:
        candidates = search_candidates(brand)
    if not candidates:
        return None
    # 候補を一括取得し、検索順で最初に「企業/ブランド型」へ厳格一致したものを採用。
    d = api_get({"action": "wbgetentities", "ids": "|".join(candidates[:10]), "props": "claims"})
    entities = d.get("entities", {})
    qid = None
    claims: dict = {}
    for cand in candidates:
        ent = entities.get(cand)
        if not ent:
            continue
        c = ent.get("claims", {})
        p31 = _p31_set(c)
        if HUMAN in p31:
            continue
        if p31 & ORG_ALLOW:
            qid, claims = cand, c
            break
    if not qid:
        return None

    website = _claim_value(claims, "P856")
    hq_qid = _claim_qid(claims, "P159")
    rec: dict = {"qid": qid}
    inception = _claim_value(claims, "P571")
    if isinstance(inception, dict) and inception.get("time"):
        # 形式 "+1924-00-00T00:00:00Z" → 年
        yr = inception["time"].lstrip("+")[:4]
        if yr.isdigit():
            rec["inception"] = int(yr)
    if isinstance(website, str):
        rec["website"] = website
    logo = _claim_value(claims, "P154")
    if isinstance(logo, str):
        fname = logo.replace(" ", "_")
        rec["logo"] = "https://commons.wikimedia.org/wiki/Special:FilePath/" + urllib.parse.quote(fname)

    ref_qids = [q for q in (_claim_qid(claims, "P112"), hq_qid, _claim_qid(claims, "P17")) if q]
    labels = fetch_labels(ref_qids)
    if _claim_qid(claims, "P112") and labels.get(_claim_qid(claims, "P112")):
        rec["founder"] = labels[_claim_qid(claims, "P112")]
    if hq_qid and labels.get(hq_qid):
        rec["hq"] = labels[hq_qid]
    cq = _claim_qid(claims, "P17")
    if cq and labels.get(cq):
        rec["country"] = labels[cq]
    return rec


def main() -> int:
    hub = json.loads(BRAND_HUB.read_text(encoding="utf-8"))
    brands = sorted(hub.get("brands", {}).keys())
    if not brands:
        print("no brands in brand_hub.json", file=sys.stderr)
        return 1

    result: dict = {}
    matched = 0
    for i, brand in enumerate(brands, 1):
        try:
            rec = build_record(brand)
        except Exception as e:  # noqa: BLE001 — 1 ブランド失敗で全体を落とさない
            print(f"[{i}/{len(brands)}] {brand}: ERROR {e}", file=sys.stderr, flush=True)
            time.sleep(1)
            continue
        if rec:
            result[brand] = rec
            matched += 1
            print(f"[{i}/{len(brands)}] {brand}: {rec['qid']} inception={rec.get('inception')} "
                  f"web={'Y' if rec.get('website') else '-'} logo={'Y' if rec.get('logo') else '-'} "
                  f"hq={rec.get('hq','-')}", flush=True)
        else:
            print(f"[{i}/{len(brands)}] {brand}: no match", flush=True)
        time.sleep(0.4)

    payload = {
        "_source": "Wikidata (MediaWiki Action API, www.wikidata.org)",
        "_generated_at": datetime.now(timezone.utc).isoformat(),
        "_matched": matched,
        "_total": len(brands),
        "brands": result,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {OUT.relative_to(ROOT)}: {matched}/{len(brands)} brands matched", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
