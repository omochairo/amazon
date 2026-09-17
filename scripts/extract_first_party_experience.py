"""extract_first_party_experience.py

omcha-ops#264「一次情報の供給経路」設計の C (素材化)。A+D は #7551、B は #7511 で
それぞれマージ済み。このスクリプトは K8 の gemma (Ollama) を使い、
``data/analytics/first_party_sources.json`` (A の出力) を入力に、omcha.jp の
記事本文から運営者自身の一次情報を data/raw/per_asin/<ASIN>/experience.json
(``mine_experience.py`` と同形式) として書き出す。

対象: ``has_article=true`` かつ ``has_experience=false`` の ASIN (A の出力の
フィールドをそのまま使う)。書き込み直前にファイルシステムも二重チェックし、
既に experience.json があれば **スキップする** (上書きしない。第三者素材が
消えるのを防ぐ)。

mine_experience.py の抽出プロンプトは 60〜160字の**要約**を作る設計であり、
そのまま流用すると下記の捏造ゲート (本文への部分一致) が原理的に全部落ちる。
このため抽出プロンプトは C 専用に用意し、「原文から一字一句そのまま抜き出す」
ことを明示する (``C_EXTRACTION_PROMPT_TEMPLATE``)。

捏造ゲート (``snippet_is_grounded``): 抽出された各 snippet の text が記事本文に
実在するかを、正規化した上での部分一致で判定する。通らない snippet は捨て、
捨てた件数を ``fabrication_discarded`` として出力に残す。

役割による出し分け (omcha.jp の記事内での ASIN の役割 = ``first_party_sources.json``
の ``role``。手作業で守ってきた規律のコード化):
  - ``role=primary`` (その記事の主役の商品) — 体験談・比較・安全・シーン・不満の
    どの aspect も許可する
  - ``role=compared`` (比較対象として出てくるだけ) — ``比較`` aspect の snippet
    しか採らない (体験談 aspect 等は役割ミスマッチとして捨てる)

触らないもの: ``scripts/collect_first_party_sources.py`` / ``scripts/mine_experience.py``
/ ``scripts/self_domain.py`` の挙動。

Usage:
    python scripts/extract_first_party_experience.py --limit 5
    python scripts/extract_first_party_experience.py --asins B0XXXXXXXX --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import time
import unicodedata
from typing import Any

import requests

from scripts.build_wp_navi_link_candidates import DEFAULT_WP_BASE_URL, strip_html
from scripts.collect_first_party_sources import fetch_post_content
from scripts.mine_experience import (
    DEFAULT_EXPERIENCE_MODEL,
    DEFAULT_NUM_CTX,
    DEFAULT_OLLAMA_URL,
    GEMMA_REQUEST_TIMEOUT,
    OUT_NAME,
    PER_ASIN_DIR,
    _MAX_EXTRA_RETRIES,
    _RETRY_SLEEP_SECONDS,
    _load,
    _now_iso,
    resolve_product_identity,
    write_experience,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("extract_first_party_experience")

DEFAULT_SOURCES_PATH = pathlib.Path("data/analytics/first_party_sources.json")
DEFAULT_LIMIT = 20
SOURCE_TYPE = "first_party"
USABLE_AS = "quote"
COMPARED_ONLY_ASPECT = "比較"

C_EXTRACTION_PROMPT_TEMPLATE = """あなたは一次情報 (ブログ運営者自身の実体験) の抜き出し専門アシスタントです。
要約・言い換え・補筆は禁止です。

# 商品名
{product_name}

# ブランド
{brand}

# 記事本文
{text}

上記の記事本文が、商品『{product_name}』(ブランド: {brand}) への言及を実際に含むかを判定してください。
含む場合、次の条件を全て満たす一節だけを **本文中の文字列と一字一句完全に一致する形で** 抜き出してください:

- ブログ運営者自身が実際に使った・買った・試したことが分かる記述であること
  (「うちの子」「我が家」「実際に使って」のような一人称の実体験。読者コメントや口コミの引用、
  一般的な商品説明・スペック紹介、他人の感想の伝聞は対象外)
- 抜き出す文字列を要約したり書き換えたりしないこと (本文の連続した一部をそのまま抜粋する)
- aspect は 体験談・比較・安全・シーン・不満 のいずれか一つ

該当する記述が無ければ snippets は空配列にしてください。
次の JSON スキーマだけを出力してください (他の説明文は一切含めない):
{{"entailed": true または false, "snippets": [{{"aspect": "体験談|比較|安全|シーン|不満", "text": "本文からの一字一句そのままの抜粋", "confidence": "high|medium|low"}}]}}
"""


def _normalize_for_match(text: str) -> str:
    """捏造ゲートの正規化。

    - Unicode NFKC 正規化で全角/半角・互換文字を統一する (gemma が全角スペースや
      互換englishを混ぜて出力することがある)
    - 空白・改行を全て除去する (strip_html は連続空白を1個に畳むだけなので、
      本文側の改行の入り方と gemma 出力の空白の入り方が食い違いうる)
    - casefold で英数字の大小文字差を吸収する (日本語には影響しない)

    HTML タグの除去はここでは行わない。呼び出し側は既に strip_html 済みの
    本文を渡す前提。
    """
    normalized = unicodedata.normalize("NFKC", text or "")
    return "".join(normalized.split()).casefold()


def snippet_is_grounded(snippet_text: str, source_plain_text: str) -> bool:
    """snippet_text が source_plain_text に (正規化後) 実在するか。"""
    needle = _normalize_for_match(snippet_text)
    if not needle:
        return False
    return needle in _normalize_for_match(source_plain_text)


def build_targets(sources_data: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """asin -> 対象の source レコード群 (has_article=true かつ has_experience=false)。"""
    targets: dict[str, list[dict[str, Any]]] = {}
    for rec in sources_data.get("sources", []) or []:
        if not isinstance(rec, dict):
            continue
        if not rec.get("has_article") or rec.get("has_experience"):
            continue
        asin = rec.get("asin")
        if not isinstance(asin, str) or not asin:
            continue
        targets.setdefault(asin, []).append(rec)
    return targets


def build_post_id_by_url(sources_data: dict[str, Any]) -> dict[str, int]:
    """post_url -> WP post id。posts_cache (A の出力) から逆引きする。

    first_party_sources.json の sources[] は post_url/post_title しか持たず
    post_id を持たないため、本文の再取得には posts_cache 側の対応が要る。
    """
    out: dict[str, int] = {}
    posts_cache = sources_data.get("posts_cache")
    if not isinstance(posts_cache, dict):
        return out
    for pid_str, entry in posts_cache.items():
        if not isinstance(entry, dict):
            continue
        link = entry.get("link")
        if not isinstance(link, str) or not link:
            continue
        try:
            out[link] = int(pid_str)
        except (TypeError, ValueError):
            continue
    return out


def extract_first_party_snippets(
    text: str, product_name: str, brand: str,
    ollama_url: str, model: str, session: requests.Session,
    sleeper=time.sleep, num_ctx: int = DEFAULT_NUM_CTX,
) -> list[dict[str, Any]]:
    """1 記事本文分を gemma に投げ、entailment 通過分の生 snippet 群 (aspect/text/confidence)
    を返す。役割フィルタ・捏造ゲートはここではかけない (呼び出し側の責務)。"""
    if not text.strip():
        return []
    url = f"{ollama_url.rstrip('/')}/api/generate"
    prompt = C_EXTRACTION_PROMPT_TEMPLATE.format(product_name=product_name, brand=brand, text=text)
    body = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "think": False,
        "format": "json",
        "keep_alive": "30m",
        "options": {"temperature": 0, "num_ctx": num_ctx},
    }

    attempts = _MAX_EXTRA_RETRIES + 1
    parsed: dict[str, Any] | None = None
    for attempt in range(1, attempts + 1):
        try:
            resp = session.post(url, json=body, timeout=GEMMA_REQUEST_TIMEOUT)
            resp.raise_for_status()
            payload = resp.json()
            raw = payload.get("response") if isinstance(payload, dict) else None
            if not isinstance(raw, str) or not raw.strip():
                raise ValueError("empty /api/generate response")
            candidate = json.loads(raw)
            if not isinstance(candidate, dict):
                raise ValueError("extraction response is not a JSON object")
            parsed = candidate
            break
        except (requests.RequestException, ValueError, json.JSONDecodeError) as e:
            if attempt < attempts:
                logger.warning("extraction call failed (attempt %d/%d): %s", attempt, attempts, e)
                sleeper(_RETRY_SLEEP_SECONDS)
            else:
                logger.error("extraction call failed after %d attempt(s): %s", attempts, e)
                return []

    if parsed is None or not parsed.get("entailed"):
        return []
    raw_snippets = parsed.get("snippets")
    if not isinstance(raw_snippets, list):
        return []

    out: list[dict[str, Any]] = []
    for s in raw_snippets:
        if not isinstance(s, dict):
            continue
        aspect = s.get("aspect")
        text_out = s.get("text")
        if not isinstance(aspect, str) or not isinstance(text_out, str) or not text_out.strip():
            continue
        out.append({
            "aspect": aspect,
            "text": text_out.strip(),
            "confidence": s.get("confidence") if s.get("confidence") in ("high", "medium", "low") else "medium",
        })
    return out


def extract_asin_experience(
    asin: str, role_records: list[dict[str, Any]], post_id_by_url: dict[str, int],
    *, wp_base_url: str, per_asin_dir: pathlib.Path,
    ollama_url: str, model: str, session: requests.Session,
    sleeper=time.sleep, num_ctx: int = DEFAULT_NUM_CTX,
    content_cache: dict[int, str] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, int]]:
    """1 ASIN 分。対象記事群を取得・抽出し、捏造ゲートと役割フィルタを通した
    payload (mine_experience.write_experience が書ける形) と統計を返す。
    snippet が 1 件も残らなければ payload は None。"""
    content_cache = content_cache if content_cache is not None else {}
    title, product_name, brand = resolve_product_identity(asin, per_asin_dir)
    stats = {"posts": 0, "checked": 0, "role_filtered": 0, "fabrication_discarded": 0, "kept": 0}
    if not title:
        logger.warning("%s: amazon item not found — skip", asin)
        return None, stats

    snippets: list[dict[str, Any]] = []
    seen_posts: set[str] = set()
    for rec in role_records:
        post_url = rec.get("post_url", "")
        role = rec.get("role", "")
        if not post_url or post_url in seen_posts:
            continue
        seen_posts.add(post_url)
        post_id = post_id_by_url.get(post_url)
        if post_id is None:
            logger.warning("%s: %s の post id が posts_cache に無い — skip", asin, post_url[:80])
            continue

        if post_id not in content_cache:
            content_html = fetch_post_content(post_id, wp_base_url, session, sleeper=sleeper)
            content_cache[post_id] = strip_html(content_html) if content_html else ""
        plain_text = content_cache[post_id]
        if not plain_text:
            continue
        stats["posts"] += 1

        for s in extract_first_party_snippets(
            plain_text, product_name, brand, ollama_url, model, session, sleeper, num_ctx,
        ):
            stats["checked"] += 1
            if role == "compared" and s["aspect"] != COMPARED_ONLY_ASPECT:
                stats["role_filtered"] += 1
                continue
            if not snippet_is_grounded(s["text"], plain_text):
                stats["fabrication_discarded"] += 1
                continue
            snippets.append({
                "aspect": s["aspect"],
                "text": s["text"],
                "source_type": SOURCE_TYPE,
                "source_url": post_url,
                "usable_as": USABLE_AS,
                "confidence": s["confidence"],
            })

    stats["kept"] = len(snippets)
    if not snippets:
        return None, stats

    payload = {
        "asin": asin,
        "generated_at": _now_iso(),
        "model": model,
        "snippets": snippets,
    }
    return payload, stats


def run(
    *,
    sources_path: pathlib.Path = DEFAULT_SOURCES_PATH,
    asins: list[str] | None = None,
    limit: int = DEFAULT_LIMIT,
    wp_base_url: str = DEFAULT_WP_BASE_URL,
    per_asin_dir: pathlib.Path = PER_ASIN_DIR,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    model: str = DEFAULT_EXPERIENCE_MODEL,
    num_ctx: int = DEFAULT_NUM_CTX,
    dry_run: bool = False,
    session: requests.Session | None = None,
    sleeper=time.sleep,
) -> dict[str, Any]:
    session = session or requests.Session()
    data = _load(sources_path)
    if not isinstance(data, dict):
        logger.error("%s が読めない (未生成か壊れている) — 何もしない", sources_path)
        return {"processed": [], "written": 0}

    targets = build_targets(data)
    post_id_by_url = build_post_id_by_url(data)

    asin_list = sorted(targets.keys())
    if asins:
        wanted = set(asins)
        asin_list = [a for a in asin_list if a in wanted]
    if limit and limit > 0:
        asin_list = asin_list[:limit]

    content_cache: dict[int, str] = {}
    processed: list[dict[str, Any]] = []
    written = 0
    for asin in asin_list:
        out_path = per_asin_dir / asin / OUT_NAME
        if out_path.exists():
            logger.info("%s: 既に experience.json がある — skip (上書きしない)", asin)
            processed.append({"asin": asin, "skipped": "already_has_experience"})
            continue

        payload, stats = extract_asin_experience(
            asin, targets[asin], post_id_by_url,
            wp_base_url=wp_base_url, per_asin_dir=per_asin_dir,
            ollama_url=ollama_url, model=model, session=session,
            sleeper=sleeper, num_ctx=num_ctx, content_cache=content_cache,
        )
        processed.append({"asin": asin, **stats})
        logger.info(
            "%s: posts=%d checked=%d role_filtered=%d fabrication_discarded=%d kept=%d",
            asin, stats["posts"], stats["checked"], stats["role_filtered"],
            stats["fabrication_discarded"], stats["kept"],
        )
        if payload is None:
            continue
        if dry_run:
            logger.info("[dry-run] %s: %d snippets を書く予定 (書かない)", asin, stats["kept"])
            continue
        write_experience(asin, payload, base=per_asin_dir)
        written += 1

    summary = {"processed": processed, "written": written}
    logger.info("done: %d 件処理 / %d 件書いた", len(processed), written)
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sources", default=str(DEFAULT_SOURCES_PATH))
    ap.add_argument("--asins", default="", help="対象 ASIN をカンマ区切りで明示指定")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    ap.add_argument("--wp-base-url", default=DEFAULT_WP_BASE_URL)
    ap.add_argument("--per-asin-dir", default=str(PER_ASIN_DIR))
    ap.add_argument("--ollama-url", default=os.environ.get("OLLAMA_HOST", DEFAULT_OLLAMA_URL))
    ap.add_argument("--model", default=os.environ.get("EXPERIENCE_MODEL", DEFAULT_EXPERIENCE_MODEL))
    ap.add_argument("--num-ctx", type=int, default=int(os.environ.get("OLLAMA_NUM_CTX", DEFAULT_NUM_CTX)))
    ap.add_argument("--dry-run", action="store_true", help="出力せず stdout にサマリ")
    args = ap.parse_args()

    asins = [a.strip() for a in args.asins.split(",") if a.strip()]
    run(
        sources_path=pathlib.Path(args.sources),
        asins=asins or None,
        limit=args.limit,
        wp_base_url=args.wp_base_url,
        per_asin_dir=pathlib.Path(args.per_asin_dir),
        ollama_url=args.ollama_url,
        model=args.model,
        num_ctx=args.num_ctx,
        dry_run=args.dry_run,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
