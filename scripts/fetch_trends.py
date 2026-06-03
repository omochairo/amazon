import requests, os, json, logging, feedparser, sys

logger = logging.getLogger("fetch_trends")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# Issue #1481: API 失敗時の最後の砦。`trends.json` も `prior` も無い完全初回
# だけが踏むハードコード fallback。通常運用では prior 温存 → fallback の順。
HARDCODED_FALLBACK = [{"query": "知育玩具 0歳", "traffic": "50,000+"}]


def _load_prior(out_dir: str) -> list | None:
    """既存 trends.json の top_queries を返す。空 / 不在 / 壊れている場合 None。"""
    path = os.path.join(out_dir, "trends.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    prior = data.get("top_queries") or []
    return prior if prior else None


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="data/raw/")
    args = parser.parse_args()

    url = "https://trends.google.com/trends/trendingsearches/daily/rss?geo=JP"
    headers = {"User-Agent": "Mozilla/5.0"}

    trends: list | None = None
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 200:
            feed = feedparser.parse(resp.content)
            trends = [{"query": e.title, "traffic": e.get("ht_approx_traffic", "不明")} for e in feed.entries]
            if not trends:
                logger.warning("Google Trends RSS returned 0 entries; preserving prior if available.")
                trends = None
        else:
            logger.warning(f"Google Trends RSS HTTP {resp.status_code}; preserving prior if available.")
    except Exception as e:
        logger.warning(f"Google Trends RSS request failed: {e}; preserving prior if available.")

    if trends is None:
        prior = _load_prior(args.out)
        if prior is not None:
            logger.info(f"Preserving previous trends.json ({len(prior)} entries).")
            return  # 上書きせず終了
        logger.warning("No prior trends.json; falling back to hardcoded entry.")
        trends = HARDCODED_FALLBACK

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "trends.json"), "w", encoding="utf-8") as f:
        json.dump({"top_queries": trends}, f, ensure_ascii=False, indent=4)

if __name__ == "__main__":
    main()
