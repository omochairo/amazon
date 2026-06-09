"""fetch_toy_recalls.py

#1358 B-5 follow-up (#2112): 消費者庁リコール情報サイト「こども向け商品一覧」
(screenkbn=05) を走査し、当サイト掲載ブランド (data/brand_taxonomy.yaml) に
該当するリコール事例を **候補** として抽出する。

設計判断:
- 出力は候補 (candidate) のみ。`hugo/data/toy_safety.json` の brand_recalls には
  **書き込まない**。誤検出=偽『この商品がリコール』は E-E-A-T を破壊するため、
  実投入は人手検証 (open_toy_recall_issue.py が Issue dump → 人間が PR) を必須とする。
- リコール一覧は CSV/API/絞り込みパラメータ無し。POST で viewCountdden を大きくし
  全件 (約485) を 1 リクエスト取得 → カテゴリ + ブランド一致でフィルタ。
- empty-overwrite guard ([[reseller-drop]]/[[rakuten-empty-overwrite]] の教訓):
  fetch 失敗や 0 行パースのときは既存 JSON を握り潰さない。
- ASCII alias は単語境界一致 (SIS-in-BASIS 等の偽陽性回避)。company prefix
  (商品名の「より前) のみを照合対象にし、本文中の偶発一致を避ける。

food (食品) カテゴリは玩具ブランド名がまず出現しないため、ブランド一致 +
company-prefix 限定で自然に 0 件になる (実測)。
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import re
import sys
import time
import urllib.parse
import urllib.request

import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("fetch_toy_recalls")

LIST_URL = "https://www.recall.caa.go.jp/result/index.php"
DETAIL_BASE = "https://www.recall.caa.go.jp/result/detail.php?rcl={rcl}&screenkbn=05"
SCREEN_KBN = "05"  # こども向け商品一覧
DEFAULT_TAXONOMY = "data/brand_taxonomy.yaml"
DEFAULT_TOY_SAFETY = "hugo/data/toy_safety.json"
DEFAULT_OUT = "data/analytics/toy_recall_candidates.json"

# 玩具に無関係な食品アレルギー回収は除外。残すカテゴリ (人手検証前提でやや広めに)。
KEEP_CATEGORIES = {"文具・娯楽用品", "車両・乗り物", "住居品", "保健衛生品", "家電製品", "被服品"}

ROW_RE = re.compile(
    r'result_list_category category_\d+">([^<]+)<'
    r'.*?detail\.php\?rcl=(\d+)[^>]*>([^<]+)</a>'
    r'.*?result_list_post_date">([\d/]+)<',
    re.S,
)

# 会社種別の接頭辞 (ブランド名はこの後に来る) / 責任企業の区切り
CORP_PREFIX_RE = re.compile(r"^(株式会社|有限会社|合同会社|合資会社|（株）|\(株\))")
TOKEN_SEP_RE = re.compile(r"[/／、,\s]+")


def fetch_listing(view_count: int = 1000, retries: int = 3) -> str:
    data = urllib.parse.urlencode({
        "search": "", "screenkbn": SCREEN_KBN, "category": "",
        "viewCountdden": str(view_count), "portarorder": "0",
        "actionorder": "0", "pagingHidden": "",
    }).encode()
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                LIST_URL, data=data,
                headers={"User-Agent": "omochairo-recall-monitor/1.0 (+navi.omcha.jp)"},
            )
            return urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            last = exc
            logger.warning("fetch attempt %d failed: %s", attempt + 1, exc)
            time.sleep(5)
    raise RuntimeError(f"listing fetch failed after {retries} attempts: {last}")


def load_brand_forms(taxonomy_path: pathlib.Path) -> list[tuple[str, str, bool]]:
    tax = yaml.safe_load(taxonomy_path.read_text(encoding="utf-8"))
    forms: list[tuple[str, str, bool]] = []
    for brand in tax.get("brands", []):
        if brand.get("exclude_from_taxonomy"):
            continue
        canonical = brand["canonical"]
        for form in [canonical] + list(brand.get("aliases") or []):
            form = form.strip()
            if not form:
                continue
            if form.isascii() and len(form) < 3:
                continue  # 短すぎる ASCII alias はノイズ源
            forms.append((form, canonical, form.isascii()))
    return forms


def match_company(company: str, forms) -> tuple[str, str] | None:
    """company prefix を区切りで分割し、各トークンの **先頭** で照合する。

    日本語は単語境界が無いため部分一致だと イチジク製薬⊃ジク / パナソニック⊃ニック の
    ような偽陽性が出る。ブランド名は企業名の先頭に来るので token-start に固定する。
    `/` 区切りの責任企業並記 (例 "○○ダイレクト/HASBRO,INC.") も拾えるよう各トークンを見る。
    """
    tokens = [
        CORP_PREFIX_RE.sub("", t).strip()
        for t in TOKEN_SEP_RE.split(CORP_PREFIX_RE.sub("", company).strip())
    ]
    for form, canonical, is_ascii in forms:
        for tok in tokens:
            if not tok:
                continue
            if is_ascii:
                if re.match(rf"{re.escape(form)}(?![A-Za-z])", tok, re.I):
                    return canonical, form
            elif tok.startswith(form):
                return canonical, form
    return None


def load_verified_ids(toy_safety_path: pathlib.Path) -> set[str]:
    """brand_recalls に既登録 (検証済) の rcl id を集める → 候補から除外する。"""
    if not toy_safety_path.exists():
        return set()
    data = json.loads(toy_safety_path.read_text(encoding="utf-8"))
    ids: set[str] = set()
    for entries in (data.get("brand_recalls") or {}).values():
        for e in entries:
            url = e.get("url", "")
            m = re.search(r"rcl=(\d+)", url)
            if m:
                ids.add(m.group(1))
    return ids


def parse_candidates(html: str, forms, verified_ids: set[str]) -> list[dict]:
    candidates: list[dict] = []
    for cat, rcl, title, pdate in ROW_RE.findall(html):
        if cat not in KEEP_CATEGORIES:
            continue
        if rcl in verified_ids:
            continue
        company = re.split(r"[「（(]", title)[0].strip()
        hit = match_company(company, forms)
        if not hit:
            continue
        canonical, alias = hit
        candidates.append({
            "brand": canonical,
            "matched_alias": alias,
            "category": cat,
            "rcl": rcl,
            "post_date": pdate.replace("/", "-"),
            "title": title.strip(),
            "url": DETAIL_BASE.format(rcl=rcl),
        })
    candidates.sort(key=lambda c: c["post_date"], reverse=True)
    return candidates


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--taxonomy", default=DEFAULT_TAXONOMY)
    p.add_argument("--toy-safety", default=DEFAULT_TOY_SAFETY)
    p.add_argument("--out", default=DEFAULT_OUT)
    p.add_argument("--view-count", type=int, default=1000)
    args = p.parse_args()

    forms = load_brand_forms(pathlib.Path(args.taxonomy))
    verified = load_verified_ids(pathlib.Path(args.toy_safety))
    logger.info("loaded %d brand forms, %d verified rcl ids", len(forms), len(verified))

    html = fetch_listing(args.view_count)
    rows = ROW_RE.findall(html)
    if not rows:
        logger.error("0 rows parsed — refusing to overwrite (empty-overwrite guard)")
        return 1
    logger.info("parsed %d recall rows", len(rows))

    candidates = parse_candidates(html, forms, verified)
    logger.info("%d brand-matched candidates (excluding %d verified)", len(candidates), len(verified))

    out_path = pathlib.Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": "消費者庁 リコール情報サイト (こども向け商品一覧, screenkbn=05)",
        "source_url": f"{LIST_URL}?screenkbn={SCREEN_KBN}",
        "rows_scanned": len(rows),
        "verified_excluded": len(verified),
        "candidates": candidates,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("wrote %s", out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
