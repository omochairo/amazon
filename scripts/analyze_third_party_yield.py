"""
analyze_third_party_yield.py (#4841 V1)

third_party_sources.json のホスト種別ごとの歩留まりを測る。

体験談の検索経路 (fetch_third_party_sources.py) は既にあり、商品名だけの検索語で
2,003 ASIN・8,940 件の URL を集めている。中身の大半は通販・SNS で、ブログ由来の
snippet (experience.json の source_url 付き) は少ない。「検索語を変えればブログが
増えるか」(V2) を試す前に、まず手持ちのデータで「ホスト種別ごとに歩留まりが違うか」
を実測する (外部への新規リクエストはしない。ただし §V1-2 の JS シェル判定のみ
最小限の再取得を行う)。

分母の作り方が肝心: experience.json は snippet が 0 件だと書かれない (生存者
バイアス) ので、「third_party_sources.json に URL がある」だけでは分母にならない。
分母は「体験談マイニングが実際に fetch を試みた URL」に絞る。それは 2 つの記録の
突き合わせで再構成する:

  1. K8 の named volume `experience-raw` 上の `mining_ledger.json` (ASIN 単位で
     last_attempt を記録)。**volume は読み取り専用**。`docker cp` 相当でコピーを
     取ってから、このスクリプトにはローカルパスとして渡す (--ledger)。
  2. home-ops の Experience Mining run ログの warning 行
     (`third_party fetch failed for <url>: ... — skip`, mine_experience.py の
     gather_third_party が出す)。ログファイルをそのままこのスクリプトに渡す
     (--run-log, 複数可)。

ledger に載っている ASIN の third_party_sources.json 由来 URL (検索結果ページを
除く。gather_third_party と同じフィルタ) が「試した URL」。そのうち run ログに
fetch failed が出ているものが「失敗」。experience.json の snippet で source_url が
一致するものが「snippet が出た」。

Usage:
  python scripts/analyze_third_party_yield.py \\
      --ledger /tmp/mining_ledger.json \\
      --run-log /tmp/run1.txt --run-log /tmp/run2.txt \\
      --out docs/experience-source-yield/v1_results.json

  # JS シェル判定 (blog 種別から 30 URL だけ再取得。1 秒 1 リクエスト・HONEST_UA)
  python scripts/analyze_third_party_yield.py --ledger ... --run-log ... \\
      --js-shell-sample 30 --out ...
"""

from __future__ import annotations

import argparse
import collections
import json
import logging
import pathlib
import re
import sys
import time
import urllib.parse
from typing import Optional

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import score_per_asin_info as _sc  # noqa: E402  (is_search_result_url の SSOT)
from mine_experience import HONEST_UA, REQUEST_TIMEOUT, _html_to_text  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("analyze_third_party_yield")

PER_ASIN_DIR = pathlib.Path("data/raw/per_asin")
THIRD_PARTY_NAME = "third_party_sources.json"
EXPERIENCE_NAME = "experience.json"

_FETCH_FAILED_RE = re.compile(r"third_party fetch failed for (\S+):")

# ホスト種別分類。判定は host の完全一致 or サフィックス一致 (サブドメイン許容)。
# **網羅はしない。** third_party_sources.json は 1,900+ distinct host あり、うち
# 8 割以上は 1〜2 件しか出現しない零細な輸入代理店・個店 (実測 2026-09-15)。
# 上位で確認できたホストだけを明示分類し、それ以外は "other" に落ちる。
# "other" の上位ホストは実行結果 (top_other_hosts) に出して透明化する。
HOST_CATEGORIES: dict[str, tuple[str, ...]] = {
    # 個人ブログサービス (依頼コメントの指定リストが SSOT)
    "blog": (
        "ameblo.jp", "note.com", "hatenablog.com", "hatenablog.jp", "hateblo.jp",
        "fc2.com", "livedoor.blog", "livedoor.jp", "blog.jp", "goo.ne.jp",
        "seesaa.net", "jugem.jp", "cocolog-nifty.com", "exblog.jp",
        "blogspot.com", "substack.com",
    ),
    # 依頼コメントの指定リストが SSOT (facebook/reddit/pinterest 等はここに
    # 入れず other に落として上位ホスト報告で可視化する — 指定を勝手に広げない)
    "sns": (
        "youtube.com", "youtu.be", "instagram.com", "x.com", "twitter.com",
        "threads.com", "threads.net", "tiktok.com",
    ),
    # ニュース・専門メディア・比較メディア・(準公式の) ファン百科
    "media": (
        "prtimes.jp", "atpress.ne.jp", "newscast.jp",
        "watch.impress.co.jp", "dengeki.com",
        "ja.wikipedia.org", "en.wikipedia.org", "dtimes.jp",
        "my-best.com", "goodtoy-guide.com", "thetoyinsider.com",
        "fandom.com",  # tamagotchi/nerf/beyblade/tomica 等のファン wiki (非公式)
        "boardgamegeek.com", "hoobby.net",
    ),
    # メーカー公式 (直販 EC サブドメインも同一ドメイン配下なら maker に含める)
    "maker": (
        "takaratomy.co.jp", "takaratomy-arts.co.jp", "lego.com", "briojapan.com",
        "kawada-toys.com", "agatsuma.co.jp", "bandai.co.jp", "toyroyal.co.jp",
        "tamagotchi-official.com", "monpoke.jp", "laq.co.jp", "epoch.jp",
        "gakkensf.co.jp", "corp-gakken.co.jp", "hon.gakken.jp",
        "kumonshuppan.com", "kumon.ne.jp", "gentosha-edu.co.jp",
        "saywoodwork.com", "mattel.co.jp", "sylvanianfamilies.com",
        "playmobil.com", "ravensburger.us", "ravensburger.org",
        "picassotiles.com", "learningresources.com",
        "pokemoncenter-online.com", "pokemonfrienda.com", "megahouse.co.jp",
        "hasbro.com", "artec-kk.co.jp", "segatoys.co.jp", "elenco.com",
        "shachihata.jp", "ensky.co.jp", "hanayamatoys.co.jp", "pilot-toy.com",
        "quartett.co.jp", "joypalette.co.jp", "woodymonkey.com",
    ),
    # 通販・価格比較・フリマ・ふるさと納税返礼品など「売り場」全般
    "ec": (
        "yodobashi.com", "biccamera.com", "kakaku.com", "yamada-denkiweb.com",
        "askul.co.jp", "ebay.com", "ganguoroshi.jp", "toysrus.co.jp",
        "eurobus.jp", "joshinweb.jp", "walmart.com", "dreamblossom.jp",
        "chiikugangu.jp", "etsy.com", "monotaro.com", "hyakuchomori.co.jp",
        "aeonretail.com", "paypayfleamarket.yahoo.co.jp", "edute.jp",
        "edion.com", "target.com", "kojima.net", "happinetonline.com",
        "iroya.online", "irisplaza.co.jp", "ed-inter.co.jp",
        "goodsmiley.base.ec", "mocco.jp", "creatoys.jp", "rangs.jp",
        "rangsjapan.co.jp", "amiami.jp", "giftmall.co.jp", "aliexpress.com",
        "alibaba.com", "superdelivery.com", "liebam.co.jp", "gincho.co.jp",
        "stds.jp", "payid.jp", "goodtoy.jp", "ripka2007.com", "cainz.com",
        "auctions.yahoo.co.jp", "minne.com", "creema.jp", "tanomail.com",
        "furusato-tax.jp", "gakken-mall.jp", "nafco-online.com",
        "dcm-ekurashi.com", "kohls.com", "bestbuy.com", "schoolspecialty.com",
        "malloftoys.com", "staples.com", "babiesrus.co.jp", "toynes.jp",
        "satofull.jp", "lohaco.yahoo.co.jp", "ymall.jp", "zurutoys.com",
        "happyhentoys.com", "maruka.jp", "fatbraintoys.com", "hand2mind.com",
        "hands.net", "depot-net.com", "orange-baby.com", "greenbeans.com",
        "online.nojima.co.jp",
    ),
}

_CATEGORY_ORDER = ("blog", "sns", "media", "maker", "ec")


def _host(url: str) -> str:
    """URL から host を取り出す (先頭の www. だけ落とす)。

    fetch_third_party_sources._host と同じ正規化 (lstrip ではなく
    removeprefix — walmart.com のような w 始まりの host を壊さない)。
    """
    try:
        return urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")
    except ValueError:
        return ""


def classify_host(host: str) -> str:
    """host を HOST_CATEGORIES で分類する。一致しなければ "other"。"""
    if not host:
        return "other"
    for category in _CATEGORY_ORDER:
        for domain in HOST_CATEGORIES[category]:
            if host == domain or host.endswith("." + domain):
                return category
    return "other"


def _load(path: pathlib.Path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def load_third_party_sources(base: pathlib.Path = PER_ASIN_DIR) -> dict[str, list[dict]]:
    """asin -> [{"url", "host", "title"}] (third_party_sources.json の sources)。"""
    out: dict[str, list[dict]] = {}
    for path in sorted(base.glob("*/" + THIRD_PARTY_NAME)):
        asin = path.parent.name
        data = _load(path)
        if not isinstance(data, dict):
            continue
        sources = data.get("sources")
        if not isinstance(sources, list):
            continue
        rows = []
        for s in sources:
            if not isinstance(s, dict):
                continue
            url = s.get("url")
            if not isinstance(url, str) or not url:
                continue
            rows.append({
                "url": url,
                "host": s.get("host") or _host(url),
                "title": s.get("title", ""),
            })
        if rows:
            out[asin] = rows
    return out


def load_experience_snippets(base: pathlib.Path = PER_ASIN_DIR) -> dict[str, list[dict]]:
    """asin -> [{"aspect", "source_type", "source_url"}] (experience.json の snippets)。"""
    out: dict[str, list[dict]] = {}
    for path in sorted(base.glob("*/" + EXPERIENCE_NAME)):
        asin = path.parent.name
        data = _load(path)
        if not isinstance(data, dict):
            continue
        snippets = data.get("snippets")
        if not isinstance(snippets, list):
            continue
        out[asin] = [s for s in snippets if isinstance(s, dict)]
    return out


def load_ledger_tried_asins(path: pathlib.Path) -> set[str]:
    """mining_ledger.json のコピーから、マイニングが実際に試みた ASIN 集合を返す。

    last_attempt を持つエントリはすべて mine_asin が呼ばれた = gather_third_party
    も呼ばれた (amazon.json に title が無く即 skip した場合を除くが、判別できる
    記録がないのでここでは区別しない。該当 ASIN は third_party_sources.json も
    無いことが多く、"試した URL 0 件" として自然に効いてくる)。
    """
    data = _load(path)
    if not isinstance(data, dict):
        logger.warning("ledger %s が読めません — 分母は空集合", path)
        return set()
    asins = data.get("asins")
    if not isinstance(asins, dict):
        return set()
    return {a for a in asins if isinstance(a, str)}


def parse_fetch_failures(paths: list[pathlib.Path]) -> set[str]:
    """run ログから "third_party fetch failed for <url>: ... — skip" 行の URL を集める。

    ログの保持期間の分だけしか遡れない (GitHub Actions のログ retention)。
    網羅ではなく、観測できた範囲のサンプルとして扱う。
    """
    failed: set[str] = set()
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            logger.warning("run log %s が読めません: %s", path, e)
            continue
        for m in _FETCH_FAILED_RE.finditer(text):
            failed.add(m.group(1))
    return failed


def tried_urls_for_asin(asin: str, sources_by_asin: dict[str, list[dict]]) -> list[dict]:
    """gather_third_party と同じフィルタ (検索結果ページを除外) で「試した URL」を返す。"""
    rows = sources_by_asin.get(asin, [])
    return [r for r in rows if not _sc.is_search_result_url(r["url"])]


def compute_corpus_distribution(sources_by_asin: dict[str, list[dict]]) -> dict:
    """third_party_sources.json 全体 (母集合を絞らない) のホスト種別分布。"""
    host_counts: collections.Counter = collections.Counter()
    category_counts: collections.Counter = collections.Counter()
    other_hosts: collections.Counter = collections.Counter()
    total_urls = 0
    for rows in sources_by_asin.values():
        for r in rows:
            total_urls += 1
            host = r["host"]
            host_counts[host] += 1
            cat = classify_host(host)
            category_counts[cat] += 1
            if cat == "other":
                other_hosts[host] += 1
    return {
        "total_urls": total_urls,
        "distinct_hosts": len(host_counts),
        "by_category": dict(category_counts),
        "top_other_hosts": other_hosts.most_common(30),
    }


def compute_yield(
    tried_asins: set[str],
    sources_by_asin: dict[str, list[dict]],
    failed_urls: set[str],
    snippets_by_asin: dict[str, list[dict]],
) -> dict:
    """ホスト種別ごとの歩留まり (試した URL 数・失敗数・snippet が出た URL 数・snippet
    数・aspect 内訳) を、ledger の「試した ASIN」に絞って集計する。"""
    # source_url -> host (「試した」母集合に限定。snippet の source_url 突き合わせに使う)
    url_to_host: dict[str, str] = {}
    tried_by_category: collections.Counter = collections.Counter()
    failed_by_category: collections.Counter = collections.Counter()
    tried_urls_total: set[str] = set()
    failed_urls_matched: set[str] = set()

    for asin in tried_asins:
        for r in tried_urls_for_asin(asin, sources_by_asin):
            url = r["url"]
            host = r["host"]
            cat = classify_host(host)
            url_to_host[url] = cat
            if url not in tried_urls_total:
                tried_urls_total.add(url)
                tried_by_category[cat] += 1
            if url in failed_urls and url not in failed_urls_matched:
                failed_urls_matched.add(url)
                failed_by_category[cat] += 1

    snippet_urls_by_category: dict[str, set[str]] = collections.defaultdict(set)
    snippet_count_by_category: collections.Counter = collections.Counter()
    aspect_by_category: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)

    for asin in tried_asins:
        for s in snippets_by_asin.get(asin, []):
            source_url = s.get("source_url")
            if not isinstance(source_url, str) or not source_url:
                continue
            cat = url_to_host.get(source_url)
            if cat is None:
                # third_party_sources.json に無い source_url (news.json 由来など)。
                # ホスト種別分類の対象外 (V1 は third_party_sources.json 限定)。
                continue
            aspect = s.get("aspect")
            snippet_urls_by_category[cat].add(source_url)
            snippet_count_by_category[cat] += 1
            if isinstance(aspect, str) and aspect:
                aspect_by_category[cat][aspect] += 1

    categories = sorted(set(tried_by_category) | set(failed_by_category)
                         | set(snippet_urls_by_category) | set(snippet_count_by_category))
    out = {}
    for cat in categories:
        tried = tried_by_category.get(cat, 0)
        failed = failed_by_category.get(cat, 0)
        out[cat] = {
            "tried_urls": tried,
            "fetch_failed": failed,
            "urls_with_snippet": len(snippet_urls_by_category.get(cat, ())),
            "snippet_count": snippet_count_by_category.get(cat, 0),
            "snippet_per_tried_url": (
                snippet_count_by_category.get(cat, 0) / tried if tried else 0.0
            ),
            "aspect_breakdown": dict(aspect_by_category.get(cat, {})),
        }
    return out


def sample_js_shell_check(
    sources_by_asin: dict[str, list[dict]],
    asin_pool: Optional[set[str]] = None,
    *,
    base: pathlib.Path = PER_ASIN_DIR,
    category: str = "blog",
    sample_size: int = 30,
    seed: int = 4841,
    session=None,
    sleeper=time.sleep,
) -> dict:
    """blog 種別から sample_size URL を再取得し、本文が JS の枠だけ (商品名/ブランド名を
    含まない) かどうかを確認する。HONEST_UA・1 秒 1 リクエスト・検索結果ページ除外。

    判定は「本文テキストに ASIN の商品名またはブランドの語のいずれかが含まれるか」。
    含まれなければ「本文が薄い (JS 枠の疑いを含む)」として数える。

    `asin_pool` は候補を絞る ASIN 集合 (省略時は third_party_sources.json 全体)。
    歩留まり計算 (compute_yield) と違い、この測定は「mining が実際に試したか」に
    縛られない — blog ホストの本文品質そのものを見るのが目的で、分母を tried_asins
    (実測では数件しかない) に絞ると標本が小さすぎて測れない。
    """
    import random

    import requests

    from mine_experience import resolve_product_identity

    pool = sorted(asin_pool) if asin_pool is not None else sorted(sources_by_asin)
    candidates: list[tuple[str, str]] = []  # (asin, url)
    for asin in pool:
        for r in tried_urls_for_asin(asin, sources_by_asin):
            if classify_host(r["host"]) == category:
                candidates.append((asin, r["url"]))
    rng = random.Random(seed)
    rng.shuffle(candidates)
    picked = candidates[:sample_size]

    session = session or requests.Session()
    results = []
    for asin, url in picked:
        title, product_name, brand = resolve_product_identity(asin, base)
        try:
            resp = session.get(url, headers={"User-Agent": HONEST_UA}, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            text = _html_to_text(resp.text)
        except requests.RequestException as e:
            results.append({"asin": asin, "url": url, "status": "fetch_error", "error": str(e)})
            sleeper(1)
            continue
        # product_name は extract_search_keyword の出力 (語順そのまま) で、記事本文に
        # 一続きのフレーズとして出現するとは限らない。トークン単位 (空白区切り) で
        # 判定する — 「BRIO 木製レール 直線レール」なら「BRIO」だけ本文にあっても拾う。
        keywords = [t for t in (product_name.split() + [brand]) if len(t) >= 2]
        has_keyword = any(k in text for k in keywords) if keywords else None
        results.append({
            "asin": asin, "url": url, "status": "ok",
            "text_len": len(text), "has_product_or_brand_keyword": has_keyword,
        })
        sleeper(1)

    checked = [r for r in results if r["status"] == "ok" and r["has_product_or_brand_keyword"] is not None]
    thin = [r for r in checked if not r["has_product_or_brand_keyword"]]
    return {
        "category": category,
        "sample_size": len(picked),
        "checked": len(checked),
        "thin_body_count": len(thin),
        "thin_body_rate": (len(thin) / len(checked)) if checked else None,
        "samples": results,
    }


def build_report(
    *,
    base: pathlib.Path = PER_ASIN_DIR,
    ledger_path: pathlib.Path,
    run_log_paths: list[pathlib.Path],
    js_shell_sample: int = 0,
) -> dict:
    sources_by_asin = load_third_party_sources(base)
    snippets_by_asin = load_experience_snippets(base)
    tried_asins = load_ledger_tried_asins(ledger_path)
    failed_urls = parse_fetch_failures(run_log_paths)

    corpus = compute_corpus_distribution(sources_by_asin)
    yield_by_category = compute_yield(tried_asins, sources_by_asin, failed_urls, snippets_by_asin)

    report = {
        "denominator": {
            "tried_asins": len(tried_asins),
            "tried_asins_list": sorted(tried_asins),
            "run_log_files": [str(p) for p in run_log_paths],
            "fetch_failed_urls_observed": len(failed_urls),
        },
        "corpus_distribution": corpus,
        "yield_by_host_category": yield_by_category,
    }
    if js_shell_sample > 0:
        # 分母を tried_asins (実測では数件) に絞らない — corpus 全体の blog URL から
        # 抽出する (理由は sample_js_shell_check の docstring 参照)
        report["js_shell_check"] = sample_js_shell_check(
            sources_by_asin, base=base, sample_size=js_shell_sample,
        )
    return report


def _cli() -> int:
    ap = argparse.ArgumentParser(description="第三者ソースのホスト種別ごとの歩留まり分析 (#4841 V1)")
    ap.add_argument("--base", default=str(PER_ASIN_DIR))
    ap.add_argument("--ledger", required=True, help="mining_ledger.json のコピー (volume 外)")
    ap.add_argument("--run-log", action="append", default=[], dest="run_logs",
                    help="Experience Mining run ログ (複数指定可)")
    ap.add_argument("--js-shell-sample", type=int, default=0,
                    help="blog 種別から N 件再取得して本文の薄さを確認する (既定 0 = 実行しない)")
    ap.add_argument("--out", default=None, help="結果 JSON の書き出し先")
    args = ap.parse_args()

    report = build_report(
        base=pathlib.Path(args.base),
        ledger_path=pathlib.Path(args.ledger),
        run_log_paths=[pathlib.Path(p) for p in args.run_logs],
        js_shell_sample=args.js_shell_sample,
    )
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        out_path = pathlib.Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        logger.info("書き出し: %s", out_path)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
