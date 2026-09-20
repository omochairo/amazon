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
捨てた件数を ``fabrication_discarded`` として experience.json の
``extraction_stats`` に残す。gemma に渡すのは記事全文ではなく、一人称マーカーを
含む文の周辺だけ (``select_first_party_passages``。実測で本文の 6〜14%)。

役割による出し分け (omcha.jp の記事内での ASIN の役割 = ``first_party_sources.json``
の ``role``。手作業で守ってきた規律のコード化):
  - ``role=primary`` (その記事の主役の商品) — 体験談・比較・安全・シーン・不満の
    どの aspect も許可する
  - ``role=compared`` (比較対象として出てくるだけ) — ``比較`` aspect の snippet
    しか採らない (体験談 aspect 等は役割ミスマッチとして捨てる)

触らないもの: ``scripts/collect_first_party_sources.py`` / ``scripts/mine_experience.py``
/ ``scripts/self_domain.py`` の挙動。

#7569 (初回本実行 (#7568) のレビューで判明した、捏造ゲートを通るが素材にならない型) への
手当て:
  - 型1 (実使用が別SKU なのに note が無い) / 型2 (予測・仮定を実体験として抜く) —
    ``C_EXTRACTION_PROMPT_TEMPLATE`` に禁止事項と ``note`` フィールドを追加 (プロンプト側の
    手当てなので確率的。レビューを外す根拠にはならない)
  - 型3 (見出し・目次が本文として抽出される) — ``strip_heading_tags`` で見出し要素を
    strip_html の前に落とす
  - 型5 (文の断片) — ``snippet_is_sentence_fragment`` で句点・感嘆符・疑問符で
    閉じていない抜粋を捨てる
  - 型4 (定型リード文) は機械判定の設計が未確定 (aspect別の採択率を見てから決める、
    #7569 の申し送り) のため見送り

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
import re
import time
import unicodedata
from typing import Any

import requests

from scripts.build_wp_navi_link_candidates import DEFAULT_WP_BASE_URL, strip_html
from scripts.collect_first_party_sources import FP_MARKERS, fetch_post_content
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

# 一人称マーカーを含む文の前後何文まで一緒に残すか (select_first_party_passages)。
PASSAGE_WINDOW = 1
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？])")

# 捏造ゲートを通すのに必要な、正規化後の最短長。
# 部分一致だけだと「子」「。」のような 1 文字の断片が素通りする (母艦で実証)。
# 引用として使えない長さなので、ここで弾く。
MIN_GROUNDED_LENGTH = 20

# 見出し要素をタグごと落とす (#7569 型3)。非貪欲マッチなので開閉のタグ番号が
# 食い違っても (壊れた HTML でも) 直近の閉じタグまでで止まる。
_HEADING_TAG_RE = re.compile(r"<h[1-6][^>]*>.*?</h[1-6]>", re.IGNORECASE | re.DOTALL)

# 文として閉じているかの判定 (#7569 型5)。閉じ括弧・引用符は末尾から無視する。
_SENTENCE_TERMINATORS = "。！？"
_TRAILING_CLOSERS = "」』”'）) 　"

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
- 既に実際に体験したことだけを対象とし、「〜だろう」「〜はず」「〜かもしれない」「〜に違いない」
  のような予測・推測・仮定の表現は対象外とすること (まだ使っていない・買っていない記述も対象外)
- 抜き出す文字列を要約したり書き換えたりしないこと (本文の連続した一部をそのまま抜粋する)
- aspect は 体験談・比較・安全・シーン・不満 のいずれか一つ
- 抜き出した記述の実使用対象が商品『{product_name}』そのものではなく、別モデル・型番・旧型/新型で
  あると分かる場合は、note にその旨を一言で書くこと (例:「実使用は旧モデル」)。該当しなければ
  note は空文字列にすること

該当する記述が無ければ snippets は空配列にしてください。
次の JSON スキーマだけを出力してください (他の説明文は一切含めない):
{{"entailed": true または false, "snippets": [{{"aspect": "体験談|比較|安全|シーン|不満", "text": "本文からの一字一句そのままの抜粋", "confidence": "high|medium|low", "note": "実使用が別モデル等の場合のみ一言。無ければ空文字列"}}]}}
"""


def select_first_party_passages(plain_text: str, *, window: int = PASSAGE_WINDOW) -> str:
    """本文から一人称マーカーを含む文とその前後だけを抜き出して連結する。

    gemma に記事全文を渡すと 2 つ困ることが実測で分かった (2026-09-17, K8):

    1. **黙って切られる。** 実測のトークン比は 0.59 tok/char で、omcha.jp の記事は
       7,000〜35,600 字 = 4,300〜21,000 トークン。``DEFAULT_NUM_CTX`` (8192) を
       probe した 11 記事のうち 5 件が超えており、Ollama が prompt を切り詰める。
    2. **収量が出ない。** 11 回の呼び出しで snippet 2 件、77 秒/記事。

    ``FP_MARKERS`` (A の収集側と同じリスト) を含む文だけに絞ると、本文は実測で
    全体の 6〜14% (277〜2,938 トークン) になる。全記事が num_ctx に余裕で収まり、
    モデルが見るのが候補箇所だけになる。

    前後 ``window`` 文を足すのは、実体験の記述が「うちの子は〜」の次の文に続く
    ことがあるため。絞った結果が空なら空文字を返す (呼び出し側が skip する)。
    """
    if not plain_text:
        return ""
    sentences = [s for s in _SENTENCE_SPLIT_RE.split(plain_text) if s]
    if not sentences:
        return ""
    keep: set[int] = set()
    for i, sentence in enumerate(sentences):
        if any(marker in sentence for marker in FP_MARKERS):
            for j in range(max(0, i - window), min(len(sentences), i + window + 1)):
                keep.add(j)
    if not keep:
        return ""
    return "".join(sentences[i] for i in sorted(keep))


def strip_heading_tags(content_html: str) -> str:
    """``<h1>``〜``<h6>`` をタグごと除去する (#7569 型3)。

    ``strip_html`` はタグを空白に置換するだけで見出しの文字列自体は残すため、
    目次段落や見出しテキストが地の文と区別なく連結され、本文の断片として
    抽出されていた (母艦の実測: 目次ブロックが1文になる・見出しそのものが
    snippet になる、の2件)。strip_html に渡す前に見出し要素ごと落とすことで、
    構造が消える前に区別を付ける。
    """
    if not content_html:
        return ""
    return _HEADING_TAG_RE.sub(" ", content_html)


def snippet_is_sentence_fragment(text: str) -> bool:
    """text が文として閉じていない断片か (#7569 型5)。

    捏造ゲート (``snippet_is_grounded``) は本文への実在チェックしかしないため、
    「…遊び倒した 我が家が、」のように文の途中で切れた抜粋も素通りしていた
    (母艦の実測)。閉じ括弧・引用符を無視した末尾が句点・感嘆符・疑問符の
    いずれでもなければ断片とみなす。
    """
    trimmed = (text or "").rstrip()
    while trimmed and trimmed[-1] in _TRAILING_CLOSERS:
        trimmed = trimmed[:-1]
    return not trimmed or trimmed[-1] not in _SENTENCE_TERMINATORS


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
    """snippet_text が source_plain_text に (正規化後) 実在するか。

    ``MIN_GROUNDED_LENGTH`` 未満の断片は、本文に在っても通さない。部分一致だけだと
    「子」「。」のような 1 文字が必ず当たってゲートが空回りする (母艦で実証)。
    """
    needle = _normalize_for_match(snippet_text)
    if len(needle) < MIN_GROUNDED_LENGTH:
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
        note = s.get("note")
        out.append({
            "aspect": aspect,
            "text": text_out.strip(),
            "confidence": s.get("confidence") if s.get("confidence") in ("high", "medium", "low") else "medium",
            "note": note.strip() if isinstance(note, str) else "",
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
    stats = {
        "posts": 0, "no_passage": 0, "checked": 0,
        "fabrication_discarded": 0, "fragment_discarded": 0, "role_filtered": 0, "kept": 0,
    }
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
            # 見出し要素はタグごと落としてから strip_html する (#7569 型3)。
            # strip_html だけだと見出し文字列が地の文と区別なく連結される。
            content_cache[post_id] = strip_html(strip_heading_tags(content_html)) if content_html else ""
        plain_text = content_cache[post_id]
        if not plain_text:
            continue
        # gemma に渡すのは一人称文の周辺だけ (実測で本文の 6〜14%)。
        # 接地判定は **絞る前の本文全体** に対して行う: 前後 window の切り方で
        # 正当な引用を落とさないため。
        passages = select_first_party_passages(plain_text)
        if not passages:
            stats["no_passage"] += 1
            continue
        stats["posts"] += 1

        for s in extract_first_party_snippets(
            passages, product_name, brand, ollama_url, model, session, sleeper, num_ctx,
        ):
            stats["checked"] += 1
            # 捏造ゲートを **役割フィルタより先に** 通す。逆順だと compared の
            # 非「比較」snippet が接地判定を受けないまま捨てられ、品質指標である
            # fabrication_discarded が実態より小さく出る。
            if not snippet_is_grounded(s["text"], plain_text):
                stats["fabrication_discarded"] += 1
                continue
            # 文の断片チェックも役割フィルタより先に通す (同じ理由: 逆順だと
            # compared の非「比較」snippet が判定を受けないまま捨てられる)。
            if snippet_is_sentence_fragment(s["text"]):
                stats["fragment_discarded"] += 1
                continue
            if role == "compared" and s["aspect"] != COMPARED_ONLY_ASPECT:
                stats["role_filtered"] += 1
                continue
            snippets.append({
                "aspect": s["aspect"],
                "text": s["text"],
                "source_type": SOURCE_TYPE,
                "source_url": post_url,
                "usable_as": USABLE_AS,
                "confidence": s["confidence"],
                "note": s["note"],
            })

    stats["kept"] = len(snippets)
    if not snippets:
        return None, stats

    payload = {
        "asin": asin,
        "generated_at": _now_iso(),
        "model": model,
        "snippets": snippets,
        # 設計 (#264) の「捨てた件数を出力に残す」。ログと run() の戻り値だけだと
        # 運用に乗せたあと捏造ゲートの通過率を追えない。
        "extraction_stats": dict(stats),
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

    # primary を持つ ASIN を先に並べる。実測 2026-09-17 では対象 272 件のうち
    # 251 件 (92%) が compared のみで、ASIN 昇順で --limit を切ると gemma 予算の
    # ほとんどが compared に流れる。compared から採れるのは「比較」aspect だけで、
    # 一次情報としての価値は primary より低い。
    def _priority(asin: str) -> tuple[int, str]:
        has_primary = any(r.get("role") == "primary" for r in targets[asin])
        return (0 if has_primary else 1, asin)

    asin_list = sorted(targets.keys(), key=_priority)
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
            "%s: posts=%d no_passage=%d checked=%d fabrication_discarded=%d "
            "fragment_discarded=%d role_filtered=%d kept=%d",
            asin, stats["posts"], stats["no_passage"], stats["checked"],
            stats["fabrication_discarded"], stats["fragment_discarded"],
            stats["role_filtered"], stats["kept"],
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
