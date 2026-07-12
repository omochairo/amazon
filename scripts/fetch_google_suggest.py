"""fetch_google_suggest.py

#2953 D案「Google サジェストマイニング」の収集スクリプト。

data/articles/*.json (ASIN ごとの記事) に対応する商品名を種に、Google の
サジェスト候補 (suggestqueries.google.com/complete/search、API キー不要) を
低レートで収集し、data/raw/suggest/<ASIN>.json に蓄積する。蓄積結果は将来
FAQ / keywords 生成素材として使う想定 (本スクリプトは収集のみ、消費側は別途)。

なぜこのリポジトリにスクリプトだけ置くか:
  GitHub Actions のホスト IP は suggestqueries.google.com から早期にブロック
  されやすい。実際の収集は自宅サーバーの self-hosted runner (別リポジトリ
  omochairo/amazon-home-ops の `10-suggest-mining.yml` workflow) から低頻度で
  実行し、このリポジトリには収集ロジックとその unit test のみを置く
  (CI からは呼ばない)。

対象選定:
  data/articles/*.json を列挙し、sidecar (`*.enrichment.json` / `*.seo.json` /
  `*.quality.json`) は除外する (build_post.py の SUFFIX_SKIP / fetch_omcha_related.py
  の _SIDECAR_SUFFIXES と同じ流儀)。ファイル名 `YYYY-MM-DD-<ASIN>.json` の末尾
  ハイフン区切りトークンを ASIN として扱う (_fetch_targets._collect_article_targets
  と同じ切り出し方)。同一 ASIN に複数の記事ファイルがある場合 (rewrite で新旧
  併存、Issue #2711) は stem が最も新しいものを採用する。

  data/raw/suggest/<ASIN>.json が存在しない ASIN を最優先、存在する場合は
  `fetched_at` が --min-age-days より古いものだけを対象にして fetched_at 昇順
  (古い/未取得ほど先) に並べ、--limit 件まで処理する。

レート制御:
  リクエスト 1 件ごとに uniform(--sleep-min, --sleep-max) 秒 sleep する。

エラー処理:
  HTTP 非 200 応答または接続エラーが発生したら、その時点で収集ループ全体を
  打ち切り (以降の ASIN は処理しない)、それまでに得た結果は保存して exit 0
  する (部分成果 OK)。個別 ASIN の JSON パース失敗はその ASIN だけをスキップ
  して続行するが、連続 3 件失敗したらループ全体を打ち切る。

呼び出し元:
  omochairo/amazon-home-ops リポジトリの `10-suggest-mining.yml` workflow から
  `python -m scripts.fetch_google_suggest ...` として実行される。

Issue: https://github.com/omochairo/amazon/issues/2953
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import random
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("fetch_google_suggest")

# build_post.SUFFIX_SKIP / fetch_omcha_related._SIDECAR_SUFFIXES と同一の流儀。
SIDECAR_SUFFIXES = (".enrichment", ".seo", ".quality")

SUGGEST_URL = "https://suggestqueries.google.com/complete/search"
DEFAULT_ARTICLES_DIR = "data/articles"
DEFAULT_OUT_DIR = "data/raw/suggest"
REQUEST_TIMEOUT = 15
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_MAX_CONSECUTIVE_PARSE_FAILURES = 3

_SPACE_RE = re.compile(r"\s+")


class NonOkStatusError(Exception):
    """suggest エンドポイントが HTTP 200 以外を返した (403/429 含む)。"""

    def __init__(self, status_code: int):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class SuggestParseError(Exception):
    """suggest レスポンスの JSON パース、または想定外の形状。"""


def normalize(text: str) -> str:
    """NFKC + lowercase + 空白圧縮。suggestion の重複判定キー。"""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text)).lower()
    return _SPACE_RE.sub(" ", t).strip()


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


def discover_target_asins(articles_dir: pathlib.Path) -> dict[str, pathlib.Path]:
    """data/articles/*.json から {asin: article_path} を作る。

    sidecar は除外。同一 ASIN の記事が複数ある場合 (rewrite で新旧併存) は
    stem が最も新しい (= 文字列として大きい) ものを採用する
    (build_post._winning_stems と同種の判定)。
    """
    if not articles_dir.exists():
        return {}
    winners: dict[str, str] = {}
    paths: dict[str, pathlib.Path] = {}
    for f in sorted(articles_dir.glob("*.json")):
        if f.stem.endswith(SIDECAR_SUFFIXES):
            continue
        parts = f.stem.split("-")
        if len(parts) < 4:
            continue
        asin = parts[-1]
        if not asin:
            continue
        if asin not in winners or f.stem > winners[asin]:
            winners[asin] = f.stem
            paths[asin] = f
    return paths


def _load_product_name(article_path: pathlib.Path) -> str:
    try:
        data = json.loads(article_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    product = data.get("product")
    if not isinstance(product, dict):
        return ""
    name = product.get("name")
    return name.strip() if isinstance(name, str) else ""


def select_targets(
    articles_dir: pathlib.Path,
    out_dir: pathlib.Path,
    limit: int,
    min_age_days: int,
    now: datetime | None = None,
) -> list[tuple[str, pathlib.Path]]:
    """この run で収集すべき (asin, article_path) を返す。

    順序: data/raw/suggest/<ASIN>.json が無いものを最優先 (epoch 0 扱い)、
    存在するものは fetched_at 昇順。fetched_at が --min-age-days より新しい
    (= まだ新鮮) ASIN は対象から除外する。--limit 件で cap。
    """
    now = now or _now()
    cutoff = now - timedelta(days=min_age_days)
    epoch = datetime.fromtimestamp(0, tz=timezone.utc)

    candidates: list[tuple[datetime, str, pathlib.Path]] = []
    for asin, article_path in discover_target_asins(articles_dir).items():
        out_path = out_dir / f"{asin}.json"
        if not out_path.exists():
            candidates.append((epoch, asin, article_path))
            continue
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            candidates.append((epoch, asin, article_path))
            continue
        fetched_at = _parse_iso(existing.get("fetched_at")) if isinstance(existing, dict) else None
        if fetched_at is None:
            candidates.append((epoch, asin, article_path))
            continue
        if fetched_at > cutoff:
            continue  # まだ新鮮なのでスキップ
        candidates.append((fetched_at, asin, article_path))

    candidates.sort(key=lambda x: x[0])
    return [(asin, path) for _, asin, path in candidates[:limit]]


def _extract_suggestions(payload: Any) -> list[str]:
    if not isinstance(payload, list) or len(payload) < 2 or not isinstance(payload[1], list):
        raise SuggestParseError("unexpected suggest response shape")
    out: list[str] = []
    for item in payload[1]:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, list) and item and isinstance(item[0], str):
            out.append(item[0])
    return out


def fetch_suggestions_for_seed(seed: str, session: requests.Session) -> list[str]:
    """seed 1 件分のサジェストを取得する。

    HTTP 非 200 は NonOkStatusError、接続エラーは requests.RequestException、
    JSON パース失敗 / 想定外形状は SuggestParseError を送出する。
    """
    params = {
        "client": "firefox",
        "hl": "ja",
        "ie": "utf-8",
        "oe": "utf-8",
        "q": seed,
    }
    resp = session.get(
        SUGGEST_URL,
        params=params,
        headers={"User-Agent": USER_AGENT},
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code != 200:
        raise NonOkStatusError(resp.status_code)
    resp.encoding = "utf-8"
    try:
        payload = json.loads(resp.text)
    except json.JSONDecodeError as e:
        raise SuggestParseError(str(e)) from e
    return _extract_suggestions(payload)


def dedupe_suggestions(raw: list[tuple[str, str]], seeds: list[str]) -> list[dict]:
    """seed 横断で正規化重複除去。元表記・出現順は保持し、seed 自身と一致するものは除外する。"""
    seed_norms = {normalize(s) for s in seeds}
    seen: set[str] = set()
    out: list[dict] = []
    for query, seed in raw:
        norm = normalize(query)
        if not norm or norm in seed_norms or norm in seen:
            continue
        seen.add(norm)
        out.append({"query": query, "seed": seed})
    return out


def write_result(
    out_dir: pathlib.Path,
    asin: str,
    product_name: str,
    seeds: list[str],
    suggestions: list[dict],
    now: datetime | None = None,
) -> pathlib.Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "asin": asin,
        "product_name": product_name,
        "seeds": seeds,
        "suggestions": suggestions,
        "fetched_at": _now_iso(now),
    }
    out_path = out_dir / f"{asin}.json"
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return out_path


def run(
    articles_dir: pathlib.Path,
    out_dir: pathlib.Path,
    limit: int,
    min_age_days: int,
    sleep_min: float,
    sleep_max: float,
    dry_run: bool = False,
    session: requests.Session | None = None,
    sleeper=time.sleep,
) -> dict:
    """収集を実行し、件数サマリを dict で返す (テスト・main 双方から呼べるように分離)。"""
    targets = select_targets(articles_dir, out_dir, limit, min_age_days)
    logger.info("suggest targets selected: %d (limit=%d, min_age_days=%d)", len(targets), limit, min_age_days)

    summary = {"selected": len(targets), "written": 0, "skipped_no_name": 0, "aborted": False}

    if dry_run:
        for asin, _ in targets:
            logger.info("[dry-run] would fetch suggest for %s", asin)
        return summary

    session = session or requests.Session()
    consecutive_parse_failures = 0

    for asin, article_path in targets:
        product_name = _load_product_name(article_path)
        if not product_name:
            logger.warning("skip %s: product.name missing/empty in %s", asin, article_path)
            summary["skipped_no_name"] += 1
            continue

        seeds = [product_name, product_name + " "]
        raw: list[tuple[str, str]] = []
        try:
            for seed in seeds:
                suggestions = fetch_suggestions_for_seed(seed, session)
                for s in suggestions:
                    raw.append((s, seed))
                sleeper(random.uniform(sleep_min, sleep_max))
        except (requests.RequestException, NonOkStatusError) as e:
            logger.warning("HTTP error fetching suggest for %s: %s; aborting collection loop", asin, e)
            summary["aborted"] = True
            break
        except SuggestParseError as e:
            consecutive_parse_failures += 1
            logger.warning(
                "parse failure for %s (%d/%d consecutive): %s",
                asin, consecutive_parse_failures, _MAX_CONSECUTIVE_PARSE_FAILURES, e,
            )
            if consecutive_parse_failures >= _MAX_CONSECUTIVE_PARSE_FAILURES:
                logger.warning("%d consecutive parse failures; aborting collection loop", consecutive_parse_failures)
                summary["aborted"] = True
                break
            continue
        else:
            consecutive_parse_failures = 0

        deduped = dedupe_suggestions(raw, seeds)
        write_result(out_dir, asin, product_name, seeds, deduped)
        summary["written"] += 1
        logger.info("%s: %d suggestion(s) saved", asin, len(deduped))

    logger.info(
        "suggest mining done: selected=%d written=%d skipped_no_name=%d aborted=%s",
        summary["selected"], summary["written"], summary["skipped_no_name"], summary["aborted"],
    )
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description="Google サジェストを ASIN 単位で収集する (#2953 D案)")
    ap.add_argument("--limit", type=int, default=40, help="1 run あたりの最大 ASIN 数")
    ap.add_argument("--min-age-days", type=int, default=30, help="既存 data/raw/suggest/<ASIN>.json をこの日数より新しければ再取得しない")
    ap.add_argument("--sleep-min", type=float, default=2.0, help="リクエスト間 sleep の下限 (秒)")
    ap.add_argument("--sleep-max", type=float, default=4.0, help="リクエスト間 sleep の上限 (秒)")
    ap.add_argument("--articles-dir", default=DEFAULT_ARTICLES_DIR, help="記事 JSON のディレクトリ")
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="収集結果の出力先ディレクトリ")
    ap.add_argument("--dry-run", action="store_true", help="対象選定だけ行い、実際の取得/書き込みは行わない")
    args = ap.parse_args()

    if args.sleep_min < 0 or args.sleep_max < args.sleep_min:
        raise SystemExit("--sleep-min must be >= 0 and --sleep-max must be >= --sleep-min")

    run(
        articles_dir=pathlib.Path(args.articles_dir),
        out_dir=pathlib.Path(args.out_dir),
        limit=args.limit,
        min_age_days=args.min_age_days,
        sleep_min=args.sleep_min,
        sleep_max=args.sleep_max,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
