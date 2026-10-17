"""事前収集した第三者候補 URL のリンク切れを記録する (navi-brain#117 段階 1)。

``data/raw/per_asin/<ASIN>/third_party_sources.json`` の各候補 (非販売の第三者と
判定されたもの) を 1 回ずつ開き、行に ``link`` を書き足す::

    {"status": "ok" | "dead" | "unknown", "http": 404, "checked_at": "2026-10-17"}

- dead は HTTP 404 / 410 と DNS で引けないときだけ (source_pages.fetch_page)。
  生成前の判定 (score_per_asin_info) は dead の候補を数えないので、候補が 2 サイトを
  割った ASIN は待ちに回り、34 の収集レーンが取り直す (_is_fresh も dead を数えない)
- 本文・抜粋は書かない (public リポジトリ)
- 03 の pick 母集合 (fetch_third_party_sources._pickable_pool) の ASIN を先に見る。
  記事になっている ASIN は書き直しで使われるときだけ効くので後回し
- 同じホストには ``--host-interval`` 秒あけ、1 run で開くのは ``--max-urls`` まで

usage::

    python scripts/check_third_party_links.py                  # 既定 (400 URL)
    python scripts/check_third_party_links.py --dry-run        # 開かずに対象だけ数える
    python scripts/check_third_party_links.py --asin B0XXXXXXXX
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as _dt
import json
import pathlib
import sys
import threading
import time
from collections import defaultdict
from urllib.parse import urlparse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import score_per_asin_info as sc  # noqa: E402
import source_pages  # noqa: E402

OUT_NAME = "third_party_sources.json"


def _load(path: pathlib.Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def is_target_source(src) -> bool:
    """リンクを確かめる対象か (採点が非販売の第三者として数える行だけ)。"""
    if not isinstance(src, dict):
        return False
    url = src.get("url") or ""
    if not url or sc.is_search_result_url(url):
        return False
    host = src.get("host") or urlparse(url).netloc
    return sc.host_kind(host, url) == "third_party"


def needs_check(src: dict, today: _dt.date, max_age_days: int) -> bool:
    link = src.get("link")
    if not isinstance(link, dict):
        return True
    try:
        when = _dt.date.fromisoformat(str(link.get("checked_at")))
    except ValueError:
        return True
    return (today - when).days >= max_age_days


def _pool() -> set[str]:
    try:
        import fetch_third_party_sources as ftp
        return set(ftp._pickable_pool())
    except Exception as e:  # noqa: BLE001 — 優先度づけだけなので落とさない
        print(f"warning: pick pool unavailable ({e}); checking in path order", file=sys.stderr)
        return set()


def collect_targets(base: pathlib.Path, today: _dt.date, max_age_days: int,
                    asins: list[str] | None = None, pool: set[str] | None = None):
    """[(優先度, asin, index, url)] を優先度順に返す。"""
    pool = pool or set()
    paths = ([base / a / OUT_NAME for a in asins] if asins
             else sorted(base.glob(f"*/{OUT_NAME}")))
    out = []
    for p in paths:
        data = _load(p)
        if not isinstance(data, dict):
            continue
        asin = p.parent.name
        for i, src in enumerate(data.get("sources") or []):
            if is_target_source(src) and needs_check(src, today, max_age_days):
                # 0: pick 母集合で未確認 / 1: pick 母集合で期限切れ / 2,3: それ以外
                unchecked = not isinstance(src.get("link"), dict)
                prio = (0 if asin in pool else 2) + (0 if unchecked else 1)
                out.append((prio, asin, i, src["url"]))
    out.sort(key=lambda t: (t[0], t[1], t[2]))
    return out


def check_urls(urls: list[str], workers: int, host_interval: float, timeout: float,
               fetch=source_pages.fetch_page) -> dict[str, source_pages.Page]:
    """URL を開く。同じホストは 1 本の列で順に、host_interval 秒あける。"""
    by_host: dict[str, list[str]] = defaultdict(list)
    for u in dict.fromkeys(urls):
        by_host[urlparse(u).netloc.lower()].append(u)
    results: dict[str, source_pages.Page] = {}
    lock = threading.Lock()

    def run_host(host_urls: list[str]) -> None:
        for n, u in enumerate(host_urls):
            if n:
                time.sleep(host_interval)
            page = fetch(u, timeout=timeout)
            with lock:
                results[u] = page

    with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        list(ex.map(run_host, by_host.values()))
    return results


def suppress_dns_outage(results: dict, min_total: int = 20, ratio: float = 0.3) -> int:
    """DNS で引けない dead が多すぎる run は、DNS 自体の障害とみなして unknown に戻す。

    1 回の障害で候補が一斉にリンク切れ扱いになり、生成が止まるのを防ぐ。戻した件数を返す。
    """
    dns_dead = [u for u, p in results.items() if p.status == "dead" and p.http is None]
    if len(results) >= min_total and len(dns_dead) > len(results) * ratio:
        for u in dns_dead:
            results[u] = source_pages.Page("unknown", None, reason="dns-outage-suspected")
        return len(dns_dead)
    return 0


def link_record(page: source_pages.Page, today: _dt.date) -> dict:
    link = {"status": page.status, "checked_at": today.isoformat()}
    if page.http is not None:
        link["http"] = page.http
    return link


def apply_results(base: pathlib.Path, targets, results, today: _dt.date) -> int:
    """結果を JSON に書き戻す。書き換えたファイル数を返す。

    行は URL で引き当てる (読んだ後に収集レーンが書き換えて順序が変わっても、同じ URL の
    行に書く。消えた URL には書かない)。
    """
    by_asin: dict[str, set] = defaultdict(set)
    for _prio, asin, _idx, url in targets:
        if url in results:
            by_asin[asin].add(url)
    changed = 0
    for asin, urls in by_asin.items():
        path = base / asin / OUT_NAME
        data = _load(path)
        if not isinstance(data, dict):
            continue
        srcs = data.get("sources") or []
        touched = False
        for src in srcs:
            if not isinstance(src, dict) or src.get("url") not in urls:
                continue
            link = link_record(results[src["url"]], today)
            if src.get("link") != link:
                src["link"] = link
                touched = True
        if touched:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            changed += 1
    return changed


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--base", default="data/raw/per_asin")
    ap.add_argument("--asin", nargs="*", help="この ASIN だけ見る")
    ap.add_argument("--max-urls", type=int, default=400)
    ap.add_argument("--max-age-days", type=int, default=30, help="これより古い確認は取り直す")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--host-interval", type=float, default=1.5)
    ap.add_argument("--timeout", type=float, default=8.0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    base = pathlib.Path(args.base)
    today = _dt.datetime.now(_dt.timezone.utc).date()
    pool = set() if args.asin else _pool()
    targets = collect_targets(base, today, args.max_age_days, args.asin, pool)
    picked = targets[: max(0, args.max_urls)]
    print(f"targets: {len(targets)} (pick pool {sum(1 for t in targets if t[0] < 2)}), "
          f"this run: {len(picked)}")
    if args.dry_run or not picked:
        return 0

    started = time.monotonic()
    results = check_urls([t[3] for t in picked], args.workers, args.host_interval, args.timeout)
    reverted = suppress_dns_outage(results)
    if reverted:
        print(f"::warning::DNS で引けない URL が {reverted}/{len(results)} 件あり、DNS の障害とみなして"
              f"リンク切れにしなかった (unknown として記録)")
    changed = apply_results(base, picked, results, today)
    counts = defaultdict(int)
    for page in results.values():
        counts[page.status] += 1
    total = sum(counts.values())
    print(f"checked {total} URL in {time.monotonic() - started:.0f}s: "
          f"ok={counts['ok']} dead={counts['dead']} unknown={counts['unknown']} / files changed={changed}")
    # #4793: 緑のまま黙って空振りしない。unknown が大半なら取得元 IP が弾かれている疑い
    if total >= 20 and counts["unknown"] / total > 0.5:
        print(f"::warning::unknown が {counts['unknown']}/{total} と多い (取得元が弾かれている疑い)。"
              f"dead の判定は 404/410/DNS だけなので候補は減らないが、確認が進んでいない")
    return 0


if __name__ == "__main__":
    sys.exit(main())
