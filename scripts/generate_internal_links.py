"""generate_internal_links.py

Issue #3332 N3 供給側「内部リンク提案生成」の生成スクリプト。

GSC index census の実測で「検出-未登録 243 + Google 未認識 59」の記事が一度も
クロールされていないことが判明しており、内部リンクはクロール誘導の唯一の無料の
打ち手である。本スクリプトは記事本文に contextual な内部リンクを張るための提案を
生成し、``data/articles/<slug>.seo.json`` sidecar の ``internal_link_suggestions``
に書く。消費側は既に実装済みで (``scripts/build_post.py`` の
``_inject_internal_links``)、sidecar の提案を決定的な文字列置換で本文へ注入する。
本スクリプトは sidecar (と監査用 manifest) を書くだけで build_post.py には触らない。

設計の核 — 逐語アンカー契約 (絶対に変えない):
gemma には「既存本文からアンカーにする部分文字列を逐語で抜き出す」ことしかさせない。
本文の改稿は一切させない。記事 JSON 本体にも書き戻さない (sidecar のみ)。

入力:
  - ``hugo/data/semantic_related.json`` (Ruri v3 近傍。ASIN は小文字。``_meta`` は
    近傍データではないので除外する)
  - ``data/analytics/gsc_index_census.json`` (``not_indexed_urls[].coverage_state``。
    URL の ASIN は小文字)
  - ``data/analytics/gsc_history/<年>-W<週>.json`` (``by_page`` をソース優先度に使う。
    最新週を自動選択。scripts.audit_query_entailment.find_latest_history_file と
    同じ規則)
  - ``data/articles/<stem>.json`` (記事本文。アンカー抽出元)

優先度とグローバル割当 (§4 = 設計の肝):
  リンクは「未クロールのページから」ではなく「未クロールのページへ」張る。

  - ソース (リンク元) 優先度: GSC ``by_page`` で impressions のあるページを
    impressions 降順で最優先。次に census ``not_indexed_urls`` に載っていない
    記事 (= index 済みと推定) を stem 順。最後に残り (未クロール記事同士の
    リンクは効果が薄いので最後)。
  - ターゲット (リンク先) 優先度: census ``not_indexed_urls`` のうち
    ``coverage_state`` が「検出 - インデックス未登録」または「URL が Google に
    認識されていません」のもの (= 一度もクロールされていない)。
    「クロール済み - インデックス未登録」(#3203 の領分) と「見つかりませんでした
    （404）」(#3331 の領分) はターゲットにしない。
  - ターゲット被リンク上限: 1 ターゲットあたり最大 ``--max-inbound`` (既定 20)。
  - 記事単位の貪欲法ではなくコーパス全体のグローバル割当として解く
    (:func:`allocate_link_targets` 参照)。各ソース記事の候補 =
    「semantic 近傍 top-8」∩「優先ターゲット集合」。候補が複数あるとき、現時点の
    被リンク数が少ないターゲットを優先して配分する (決定的に。同点は score 降順
    → asin 昇順で tie-break)。
  - 関連性閾値: semantic score >= ``--min-score`` (既定 0.85)。
  - 割当は gemma 呼び出しの前に純粋関数として完結させ、記事ごとに候補ターゲット
    (最大 3) を絞ってから gemma に投げる。

鮮度チェック:
  ``semantic_related.json`` の ``_meta.generated_at`` が ``--max-age-hours``
  (既定 24) より古ければ abort する (前日分の近傍で走らせない)。

生成:
  gemma4:26b-a4b-it-qat (Ollama ``/api/generate``、記事 1 本につき 1 リクエスト)。
  対象セクション (allowlist) の段落を index 付きで列挙し、候補ターゲットを渡す。

validator (:func:`validate_link_suggestions`):
  消費側 (``build_post._inject_internal_links``) と**同じ規則**を実装する
  (drop = fail-closed)。消費側が最終的な砦なのでここで漏れても安全側に倒れるが、
  無駄な sidecar を書かないため揃える。両ファイルで規則が重複するのは設計どおり
  (この行が本ファイル側の相互参照コメント。消費側は
  ``scripts/build_post.py`` の ``_inject_internal_links`` / 定数群
  ``_INTERNAL_LINK_*`` を参照)。build_post は import しない
  (jinja2/frontmatter 依存を増やし K8 レーンの依存を肥大化させるため)。

  規則 12 (販売終了先の除外) のみ消費側にはまだ無い供給側独自の追加防御。
  ``build_post._apply_amazon_discontinued`` が per-ASIN snapshot
  (``data/raw/per_asin/<ASIN>/amazon.json``) の ``status=="gone"`` かつ
  楽天/Yahoo 直リンクも無いケースを ``purchase_unavailable`` と判定しているのを
  こちらでも独立に評価する (:func:`is_purchase_unavailable`)。

shadow/dry-run:
  ``--dry-run`` で sidecar を書かず、提案を manifest JSON
  (``--manifest`` 既定 ``data/analytics/internal_links_manifest.json``) に
  出力するのみ。dry-run でなくても manifest は常に書く (監査可能性)。

backfill:
  ``--limit`` (既定 30)。``--force`` 無しなら sidecar に既に
  ``internal_link_suggestions`` がある記事はスキップ (冪等)。処理はソース優先度順
  なので途中で止めても効果の大きい順に積み上がる。

Issue: https://github.com/omochairo/amazon/issues/3332 (N3 設計コメント)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import re
import time
from datetime import datetime, timezone
from typing import Any

import requests

from scripts._seo_sidecar import load_sidecar, sidecar_path, update_sidecar
from scripts.audit_query_entailment import (
    discover_articles,
    extract_asin_from_page,
    find_latest_history_file,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("generate_internal_links")

DEFAULT_ARTICLES_DIR = "data/articles"
DEFAULT_SEMANTIC_RELATED = "hugo/data/semantic_related.json"
DEFAULT_CENSUS = "data/analytics/gsc_index_census.json"
DEFAULT_HISTORY_DIR = "data/analytics/gsc_history"
DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_MODEL = "gemma4:26b-a4b-it-qat"
DEFAULT_LIMIT = 30
DEFAULT_MIN_SCORE = 0.85
DEFAULT_MAX_INBOUND = 20
DEFAULT_MAX_AGE_HOURS = 24.0
DEFAULT_MANIFEST = "data/analytics/internal_links_manifest.json"
# per-ASIN snapshot ルート (規則12 の purchase_unavailable 判定用)。CLI では
# 露出しない (#3332 N3 設計コメントの CLI 引数リストに含まれない内部既定値)。
# scripts/build_post.py の --per-asin-root 既定値と揃える。
DEFAULT_PER_ASIN_ROOT = "data/raw/per_asin"

REQUEST_TIMEOUT = 180
_MAX_EXTRA_RETRIES = 2  # 初回 + 2 リトライ = 最大 3 回試行
_RETRY_SLEEP_SECONDS = 2.0

MAX_CANDIDATES_PER_SOURCE = 3  # 消費側 _INTERNAL_LINK_MAX_PER_ARTICLE と同じ上限

# --------------------------------------------------------------------------
# validator の閾値・許可セクション (build_post._inject_internal_links と同一。
# 相互参照は本ファイル冒頭の docstring 参照)
# --------------------------------------------------------------------------
ALLOWED_SECTIONS: tuple[str, ...] = (
    "how_to_choose", "why_this_product", "daily_use", "gift_appeal",
)
_DENYLIST: frozenset[str] = frozenset({"この商品", "こちら", "詳しくは", "関連記事"})
_DYNAMIC_WORDS: tuple[str, ...] = ("価格", "最安", "在庫", "セール", "%オフ", "割引")
# 「円」「ポイント」は素の語で弾くと「楕円形のピース」(図形パズル/形合わせ商品の
# 形状説明) や「着目ポイント」(= 要点) まで落ちるので、金額 (数字/漢数詞 + 円) と
# ポイント還元の文脈でのみ弾く。generate_faq_seo.py / build_post.py の禁止規則と
# 同じ意図 (動的事実 = 時間で変化する事実だけを禁止する)。
_PRICE_RE = re.compile(r"[\d０-９一二三四五六七八九十百千万数][\d,，]*\s*円")
_POINT_RE = re.compile(
    r"ポイント(還元|付与|進呈|アップ|バック|倍)|ポイントが?(貯ま|付く|つく)|ポイ活"
)
_ANCHOR_MIN = 4
_ANCHOR_MAX = 30
MAX_LINKS_PER_ARTICLE = 3
_ANCHOR_FORBIDDEN_CHARS: tuple[str, ...] = ("[", "]", "(", ")", "\n", "\r", "`")
# 文末の句読点で終わるアンカーは「短いフレーズ」ではなく「一文まるごと」に見え、
# リンクとして不自然。gemma がプロンプト指示を守らず文末込みで抜き出した場合に
# fail-closed で弾く (プロンプト側の指示だけでは完全には防げないため二重の防御)
_SENTENCE_FINAL_PUNCTUATION: tuple[str, ...] = ("。", "！", "？", ".", "!", "?")
_MD_RE = re.compile(r"\[[^\]\n]*\]\([^)\n]*\)")
_TAG_RE = re.compile(r"<[^>\n]+>")

PRIORITY_TARGET_STATES: frozenset[str] = frozenset({
    "検出 - インデックス未登録",
    "URL が Google に認識されていません",
})

GENERATION_PROMPT_TEMPLATE = """あなたは記事本文の中から、他記事への内部リンクに使えるアンカーテキストを見つけるアシスタントです。

# 本文セクション ([section:paragraph_index] 形式でラベル付けした段落)
{sections_text}

# リンク候補 (ASIN・商品名・要旨)
{candidates_text}

## 指示
- anchor_text は必ず指定した段落からの逐語コピーにすること。1 文字も変えてはいけない。要約・言い換えは禁止。
- anchor_text は文末の句点 (。！？) を含めないこと。1 文まるごとではなく、名詞句など
  短く自然なフレーズを選ぶこと (例: 「空間認識力が育まれます」ではなく「空間認識力」
  のように、文の主要部だけを切り出す)。
- リンク先の商品に自然につながる語句だけを選ぶこと。関連が薄いなら links を空配列にしてよい。
- 価格・在庫・ポイント還元など時間で変化する事実を含む語句はアンカーにしないこと。
- 1 記事につき最大 3 件、1 段落につき 1 件、同じ target_asin の重複は不可。

次の JSON スキーマだけを出力してください (他の説明文は一切含めない):
{{"links": [{{"section": "why_this_product", "paragraph_index": 1, "anchor_text": "...", "target_asin": "...", "reason": "..."}}]}}
"""


class GenerationError(Exception):
    """gemma 生成呼び出しがリトライ上限まで失敗した。"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_json_or_none(path: pathlib.Path) -> Any | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# --------------------------------------------------------------------------
# 鮮度チェック (pure function; now は注入可能)
# --------------------------------------------------------------------------

def is_semantic_data_stale(
    meta: dict[str, Any], max_age_hours: float, *, now: datetime | None = None,
) -> bool:
    """``_meta.generated_at`` が ``max_age_hours`` より古い (または欠落/壊れている)かを判定する。"""
    generated_at = meta.get("generated_at") if isinstance(meta, dict) else None
    if not isinstance(generated_at, str) or not generated_at.strip():
        return True
    try:
        gen_dt = datetime.strptime(generated_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    now = now or datetime.now(timezone.utc)
    age_hours = (now - gen_dt).total_seconds() / 3600.0
    return age_hours > max_age_hours


# --------------------------------------------------------------------------
# 入力の読み込み・正規化 (asin は全て大文字に正規化して扱う)
# --------------------------------------------------------------------------

def load_semantic_neighbors(semantic_data: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """``semantic_related.json`` (``_meta`` 込み) から asin(大文字) -> 近傍 list を作る。

    近傍データの asin も大文字に正規化する (元データは小文字)。壊れた要素は無視する。
    """
    out: dict[str, list[dict[str, Any]]] = {}
    if not isinstance(semantic_data, dict):
        return out
    for asin, neighbors in semantic_data.items():
        if asin == "_meta" or not isinstance(neighbors, list):
            continue
        cleaned: list[dict[str, Any]] = []
        for n in neighbors:
            if not isinstance(n, dict):
                continue
            n_asin = n.get("asin")
            score = n.get("score")
            if not isinstance(n_asin, str) or not n_asin.strip():
                continue
            if not isinstance(score, (int, float)):
                continue
            cleaned.append({"asin": n_asin.strip().upper(), "score": float(score)})
        out[asin.strip().upper()] = cleaned
    return out


def extract_priority_targets(
    not_indexed_urls: list[dict[str, Any]], article_index: dict[str, pathlib.Path],
) -> set[str]:
    """census ``not_indexed_urls`` のうち優先ターゲット state かつ公開記事が実在する asin 集合。"""
    out: set[str] = set()
    for row in not_indexed_urls or []:
        if not isinstance(row, dict):
            continue
        if row.get("coverage_state") not in PRIORITY_TARGET_STATES:
            continue
        asin = extract_asin_from_page(row.get("url") or "")
        if asin and asin in article_index:
            out.add(asin)
    return out


def extract_not_indexed_asins(not_indexed_urls: list[dict[str, Any]]) -> set[str]:
    """census ``not_indexed_urls`` に (state を問わず) 登場する全 asin 集合。

    ソース優先度②「census に載っていない記事 = index 済みと推定」の判定に使う。
    """
    out: set[str] = set()
    for row in not_indexed_urls or []:
        if not isinstance(row, dict):
            continue
        asin = extract_asin_from_page(row.get("url") or "")
        if asin:
            out.add(asin)
    return out


# --------------------------------------------------------------------------
# ソース優先度 (pure function)
# --------------------------------------------------------------------------

def build_source_priority(
    article_index: dict[str, pathlib.Path],
    by_page: list[dict[str, Any]],
    not_indexed_asins: set[str],
) -> list[str]:
    """ソース (リンク元) を優先度順に並べる。

    1. GSC impressions > 0 のページを impressions 降順 (同点は asin 昇順)
    2. census not_indexed_urls に載っていない記事 (= index 済みと推定) を stem 順
    3. 残り (未クロール記事同士) を stem 順
    """
    scored: list[tuple[float, str]] = []
    seen: set[str] = set()
    for row in by_page or []:
        if not isinstance(row, dict):
            continue
        page = row.get("page")
        impressions = row.get("impressions", 0) or 0
        if not page or impressions <= 0:
            continue
        asin = extract_asin_from_page(page)
        if not asin or asin not in article_index or asin in seen:
            continue
        scored.append((float(impressions), asin))
        seen.add(asin)
    scored.sort(key=lambda t: (-t[0], t[1]))
    ordered = [asin for _, asin in scored]

    indexed_rest = sorted(
        (asin for asin in article_index if asin not in seen and asin not in not_indexed_asins),
        key=lambda a: article_index[a].stem,
    )
    ordered.extend(indexed_rest)
    seen.update(indexed_rest)

    remaining = sorted(
        (asin for asin in article_index if asin not in seen),
        key=lambda a: article_index[a].stem,
    )
    ordered.extend(remaining)
    return ordered


# --------------------------------------------------------------------------
# グローバル割当 (pure function; 設計の肝 §4)
# --------------------------------------------------------------------------

def allocate_link_targets(
    source_order: list[str],
    semantic_neighbors: dict[str, list[dict[str, Any]]],
    priority_targets: set[str],
    *,
    min_score: float = DEFAULT_MIN_SCORE,
    max_inbound: int = DEFAULT_MAX_INBOUND,
    max_candidates_per_source: int = MAX_CANDIDATES_PER_SOURCE,
) -> dict[str, list[dict[str, Any]]]:
    """ソース優先度順に走査し、各ソースへ最大 ``max_candidates_per_source`` 件の
    候補ターゲットを決定的に割り当てる (source_asin -> [{"asin","score"}, ...])。

    候補 = semantic 近傍 top-8 ∩ 優先ターゲット集合 (自己リンク除外・
    score >= min_score・現時点の被リンク数 < max_inbound)。複数候補があるとき、
    現時点の被リンク数が少ないターゲットを優先する (同点は score 降順 →
    asin 昇順で tie-break)。ソース優先度順に処理するため、被リンク数は
    処理が進むごとに更新される (ストリーミング貪欲法)。候補が 0 件のソースは
    戻り値に含めない。
    """
    inbound_count: dict[str, int] = {t: 0 for t in priority_targets}
    allocated: dict[str, list[dict[str, Any]]] = {}

    for source in source_order:
        neighbors = semantic_neighbors.get(source) or []
        candidates = [
            n for n in neighbors
            if n["asin"] in priority_targets
            and n["asin"] != source
            and n["score"] >= min_score
            and inbound_count.get(n["asin"], 0) < max_inbound
        ]
        if not candidates:
            continue
        candidates.sort(key=lambda n: (inbound_count.get(n["asin"], 0), -n["score"], n["asin"]))
        chosen = candidates[:max_candidates_per_source]
        for c in chosen:
            inbound_count[c["asin"]] = inbound_count.get(c["asin"], 0) + 1
        allocated[source] = chosen

    return allocated


# --------------------------------------------------------------------------
# 販売終了先の除外 (規則12; build_post._apply_amazon_discontinued と同じ判定規則)
# --------------------------------------------------------------------------

def _has_direct_link(entry: Any) -> bool:
    return isinstance(entry, dict) and bool(entry.get("url")) and not entry.get("is_search")


def is_purchase_unavailable(
    target_article: dict[str, Any], per_asin_root: pathlib.Path, asin: str,
) -> bool:
    """target がAmazon取り扱い終了 (snapshot status=="gone") かつ楽天/Yahoo 直リンクも
    無いか (= build_post が付ける ``purchase_unavailable``) を独立に評価する。

    ``scripts/build_post.py`` の ``_amazon_snapshot_status`` /
    ``_apply_amazon_discontinued`` と同じ判定規則 (build_post は import しない)。
    """
    snap_path = per_asin_root / asin / "amazon.json"
    if not snap_path.exists():
        return False
    try:
        snap = json.loads(snap_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(snap, dict) or (snap.get("status") or "") != "gone":
        return False
    product = target_article.get("product") if isinstance(target_article.get("product"), dict) else {}
    prices = product.get("prices") if isinstance(product.get("prices"), dict) else {}
    if _has_direct_link(prices.get("rakuten")) or _has_direct_link(prices.get("yahoo")):
        return False
    return True


def compute_unavailable_targets(
    candidate_asins: set[str], article_index: dict[str, pathlib.Path], per_asin_root: pathlib.Path,
) -> set[str]:
    """優先ターゲット候補のうち販売終了 (purchase_unavailable) の asin 集合を返す。"""
    out: set[str] = set()
    for asin in candidate_asins:
        path = article_index.get(asin)
        if not path:
            continue
        article = _load_json_or_none(path)
        if not isinstance(article, dict):
            continue
        if is_purchase_unavailable(article, per_asin_root, asin):
            out.add(asin)
    return out


# --------------------------------------------------------------------------
# セクション/段落の組み立て (pure function)
# --------------------------------------------------------------------------

def build_section_paragraphs(narrative: dict[str, Any]) -> dict[str, list[str]]:
    """narrative から allowlist セクションだけを抽出する。

    str セクションは単一要素 [text] の list に正規化する (paragraph_index=0)。
    list セクションは元の順序・要素数をそのまま保持する (欠番/空文字列も含む) —
    gemma に渡す paragraph_index が消費側の検証規則 (元 list の実インデックス) と
    ずれないようにするため。
    """
    out: dict[str, list[str]] = {}
    if not isinstance(narrative, dict):
        return out
    for section in ALLOWED_SECTIONS:
        value = narrative.get(section)
        if isinstance(value, str):
            if value.strip():
                out[section] = [value]
        elif isinstance(value, list):
            paras = [v if isinstance(v, str) else "" for v in value]
            if any(p.strip() for p in paras):
                out[section] = paras
    return out


def _format_sections_for_prompt(section_paragraphs: dict[str, list[str]]) -> str:
    lines: list[str] = []
    for section, paras in section_paragraphs.items():
        for idx, para in enumerate(paras):
            if not para.strip():
                continue
            lines.append(f"[{section}:{idx}] {para}")
    return "\n".join(lines) if lines else "(対象セクションなし)"


def _target_summary(target_article: dict[str, Any]) -> str:
    """gemma プロンプト用にターゲット記事の商品名+要旨を短く組み立てる。"""
    product = target_article.get("product") if isinstance(target_article.get("product"), dict) else {}
    name = product.get("name") if isinstance(product.get("name"), str) else ""
    meta = target_article.get("meta_description") if isinstance(target_article.get("meta_description"), str) else ""
    parts = [p.strip() for p in (name, meta) if isinstance(p, str) and p.strip()]
    return " / ".join(parts)[:200]


def _format_candidates_for_prompt(
    candidates: list[dict[str, Any]], target_summaries: dict[str, str],
) -> str:
    lines: list[str] = []
    for c in candidates:
        asin = c["asin"]
        summary = target_summaries.get(asin, "")
        lines.append(f"- ASIN={asin}: {summary}" if summary else f"- ASIN={asin}")
    return "\n".join(lines) if lines else "(候補なし)"


# --------------------------------------------------------------------------
# gemma 呼び出し
# --------------------------------------------------------------------------

def call_gemma_internal_links(
    sections_text: str,
    candidates_text: str,
    ollama_url: str,
    model: str,
    session: requests.Session,
    sleeper=time.sleep,
) -> dict[str, Any]:
    """gemma (`/api/generate`) に内部リンク提案 (逐語アンカー) を問い合わせる。

    HTTP エラー / 接続エラー / JSON パース失敗は最大 ``_MAX_EXTRA_RETRIES`` 回まで
    再試行する。それでも失敗したら例外を送出せず、``error`` フィールド付きの
    dict を返す (1 記事の失敗で他記事の処理を止めない)。
    """
    url = f"{ollama_url.rstrip('/')}/api/generate"
    prompt = GENERATION_PROMPT_TEMPLATE.format(sections_text=sections_text, candidates_text=candidates_text)
    body = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "think": False,
        "format": "json",
        "keep_alive": "30m",
        "options": {"temperature": 0},
    }

    last_err: Exception | None = None
    attempts = _MAX_EXTRA_RETRIES + 1
    for attempt in range(1, attempts + 1):
        try:
            resp = session.post(url, json=body, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            payload = resp.json()
            raw = payload.get("response") if isinstance(payload, dict) else None
            if not isinstance(raw, str) or not raw.strip():
                raise GenerationError("empty /api/generate response")
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise GenerationError("generation response is not a JSON object")
            links = parsed.get("links")
            return {"links": links if isinstance(links, list) else [], "error": None}
        except (requests.RequestException, GenerationError, ValueError, json.JSONDecodeError) as e:
            last_err = e
            if attempt < attempts:
                logger.warning("generation call failed (attempt %d/%d): %s", attempt, attempts, e)
                sleeper(_RETRY_SLEEP_SECONDS)
            else:
                logger.error("generation call failed after %d attempt(s): %s", attempts, e)

    return {"links": [], "error": str(last_err)}


# --------------------------------------------------------------------------
# validator (pure function; build_post._inject_internal_links と同じ規則。
# 本ファイル冒頭の docstring 参照)
# --------------------------------------------------------------------------

def _overlaps_markup(text: str, start: int, end: int) -> bool:
    """anchor の出現区間が既存の markdown リンク/HTML タグの区間と重なるか。"""
    for pattern in (_MD_RE, _TAG_RE):
        for m in pattern.finditer(text):
            if start < m.end() and end > m.start():
                return True
    return False


def validate_link_suggestions(
    suggestions: Any,
    narrative: dict[str, Any],
    self_asin: str,
    valid_target_asins: set[str],
    unavailable_target_asins: set[str],
    *,
    max_per_article: int = MAX_LINKS_PER_ARTICLE,
) -> tuple[list[dict[str, Any]], int]:
    """gemma が生成した内部リンク提案を検証し、``(kept, dropped_count)`` を返す。

    検証規則 (全て drop 判定。1 件の drop が他の判定を妨げない):
        1. anchor_text が対象段落の完全な部分文字列であること (逐語一致)
        2. section が how_to_choose/why_this_product/daily_use/gift_appeal のみ
        3. paragraph_index は対象セクションの範囲内 (str セクションは 0 のみ)
        4. target_asin が公開記事 (valid_target_asins) に実在すること
        5. target_asin が自記事 ASIN でないこと (自己リンク禁止)
        6. anchor の出現位置が既存の markdown リンク/HTML タグと重ならないこと
        7. anchor 長が 4〜30 字であること
        8. anchor が汎用語 denylist と完全一致しないこと
        9. anchor に動的事実 (価格・在庫等) の禁止語を含まないこと
        10. anchor に markdown 破壊文字 ([ ] ( ) 改行 バッククォート) を含まないこと
        11. 1 記事 3 本まで / 1 段落 1 本 / 1 セクション 1 本 / 同一 target 重複禁止
        12. target がpurchase_unavailable (販売終了) でないこと (供給側独自の追加防御)
        13. anchor が文末句読点 (。！？.!?) で終わらないこと (一文まるごとの
            リンク化を防ぐ。短いフレーズであることの目安)
    """
    if not isinstance(suggestions, list) or not suggestions:
        return [], 0
    if not isinstance(narrative, dict):
        return [], len(suggestions)

    self_asin_u = (self_asin or "").strip().upper()
    kept: list[dict[str, Any]] = []
    dropped = 0
    used_sections: set[str] = set()
    used_paragraphs: set[tuple[str, int]] = set()
    used_targets: set[str] = set()

    for sugg in suggestions:
        if len(kept) >= max_per_article:
            dropped += 1
            continue
        if not isinstance(sugg, dict):
            dropped += 1
            continue

        section = sugg.get("section")
        anchor = sugg.get("anchor_text")
        target_asin_raw = sugg.get("target_asin")
        paragraph_index_raw = sugg.get("paragraph_index")
        reason = sugg.get("reason")

        if section not in ALLOWED_SECTIONS or section in used_sections:
            dropped += 1
            continue
        if not isinstance(paragraph_index_raw, int):
            dropped += 1
            continue
        paragraph_index = paragraph_index_raw

        section_value = narrative.get(section)
        if isinstance(section_value, str):
            if paragraph_index != 0:
                dropped += 1
                continue
            paragraph_text = section_value
        elif isinstance(section_value, list):
            if paragraph_index < 0 or paragraph_index >= len(section_value):
                dropped += 1
                continue
            paragraph_text = section_value[paragraph_index]
            if not isinstance(paragraph_text, str):
                dropped += 1
                continue
        else:
            dropped += 1
            continue

        para_key = (section, paragraph_index)
        if para_key in used_paragraphs:
            dropped += 1
            continue

        if not isinstance(anchor, str) or not anchor:
            dropped += 1
            continue
        if not (_ANCHOR_MIN <= len(anchor) <= _ANCHOR_MAX):
            dropped += 1
            continue
        if any(ch in anchor for ch in _ANCHOR_FORBIDDEN_CHARS):
            dropped += 1
            continue
        if anchor.endswith(_SENTENCE_FINAL_PUNCTUATION):
            dropped += 1
            continue
        if anchor in _DENYLIST:
            dropped += 1
            continue
        if (any(w in anchor for w in _DYNAMIC_WORDS)
                or _PRICE_RE.search(anchor)
                or _POINT_RE.search(anchor)):
            dropped += 1
            continue

        pos = paragraph_text.find(anchor)
        if pos < 0:
            dropped += 1
            continue
        if _overlaps_markup(paragraph_text, pos, pos + len(anchor)):
            dropped += 1
            continue

        if not isinstance(target_asin_raw, str) or not target_asin_raw.strip():
            dropped += 1
            continue
        target_asin = target_asin_raw.strip().upper()
        if not target_asin or target_asin == self_asin_u:
            dropped += 1
            continue
        if target_asin in used_targets:
            dropped += 1
            continue
        if target_asin not in valid_target_asins:
            dropped += 1
            continue
        if target_asin in unavailable_target_asins:
            dropped += 1
            continue

        used_sections.add(section)
        used_paragraphs.add(para_key)
        used_targets.add(target_asin)
        kept.append({
            "section": section,
            "paragraph_index": paragraph_index,
            "anchor_text": anchor,
            "target_asin": target_asin,
            "reason": reason if isinstance(reason, str) else "",
        })

    return kept, dropped


# --------------------------------------------------------------------------
# 実行本体
# --------------------------------------------------------------------------

def run(
    articles_dir: pathlib.Path,
    *,
    semantic_related_path: pathlib.Path,
    census_path: pathlib.Path,
    history_dir: pathlib.Path,
    history_file: pathlib.Path | None = None,
    per_asin_root: pathlib.Path | None = None,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    model: str = DEFAULT_MODEL,
    limit: int = DEFAULT_LIMIT,
    min_score: float = DEFAULT_MIN_SCORE,
    max_inbound: int = DEFAULT_MAX_INBOUND,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    manifest_path: pathlib.Path,
    force: bool = False,
    dry_run: bool = False,
    session: requests.Session | None = None,
    sleeper=time.sleep,
) -> dict[str, Any]:
    """全記事を走査し、グローバル割当 → gemma 生成 → 検証 → 書き込みを行う。"""
    summary: dict[str, Any] = {
        "processed": 0, "links_written": 0, "links_dropped": 0,
        "articles_with_links": 0, "errors": 0, "aborted": False,
    }
    per_asin_root = per_asin_root or pathlib.Path(DEFAULT_PER_ASIN_ROOT)

    # 鮮度チェック (最優先。stale なら他の入力すら読まず abort)
    semantic_data = _load_json_or_none(semantic_related_path)
    if not isinstance(semantic_data, dict):
        logger.error("failed to read semantic-related file: %s", semantic_related_path)
        summary["aborted"] = True
        return summary
    meta = semantic_data.get("_meta")
    meta = meta if isinstance(meta, dict) else {}
    if is_semantic_data_stale(meta, max_age_hours):
        logger.error(
            "semantic-related data is stale (generated_at=%s, max_age_hours=%s); aborting",
            meta.get("generated_at"), max_age_hours,
        )
        summary["aborted"] = True
        return summary

    article_index = discover_articles(articles_dir)
    if not article_index:
        logger.info("no articles found under %s", articles_dir)
        return summary

    census = _load_json_or_none(census_path) or {}
    not_indexed_urls = census.get("not_indexed_urls") if isinstance(census, dict) else None
    not_indexed_urls = not_indexed_urls if isinstance(not_indexed_urls, list) else []

    src = history_file or find_latest_history_file(history_dir)
    history: dict[str, Any] = {}
    if src is not None and src.exists():
        loaded = _load_json_or_none(src)
        if isinstance(loaded, dict):
            history = loaded
        else:
            logger.warning("failed to read gsc history %s; proceeding without impressions", src)
    else:
        logger.info("no gsc_history file found under %s; proceeding without impressions", history_dir)
    by_page = history.get("by_page") or []

    semantic_neighbors = load_semantic_neighbors(semantic_data)
    priority_targets = extract_priority_targets(not_indexed_urls, article_index)
    not_indexed_asins = extract_not_indexed_asins(not_indexed_urls)

    if not priority_targets:
        logger.info("no priority targets found (census empty or no matching articles); nothing to do")
        return summary

    unavailable_targets = compute_unavailable_targets(priority_targets, article_index, per_asin_root)
    if unavailable_targets:
        logger.info("excluding %d purchase_unavailable target(s) from allocation", len(unavailable_targets))
        priority_targets = priority_targets - unavailable_targets

    source_order = build_source_priority(article_index, by_page, not_indexed_asins)
    allocated = allocate_link_targets(
        source_order, semantic_neighbors, priority_targets,
        min_score=min_score, max_inbound=max_inbound,
    )

    valid_target_asins = set(article_index.keys())
    session = session or requests.Session()
    manifest_entries: list[dict[str, Any]] = []
    target_article_cache: dict[str, dict[str, Any] | None] = {}

    def _get_target_article(asin: str) -> dict[str, Any] | None:
        if asin not in target_article_cache:
            path = article_index.get(asin)
            target_article_cache[asin] = _load_json_or_none(path) if path else None
        return target_article_cache[asin]

    processed_count = 0
    for source_asin in source_order:
        if processed_count >= limit:
            break
        candidates = allocated.get(source_asin)
        if not candidates:
            continue

        article_path = article_index[source_asin]
        sc_path = sidecar_path(articles_dir, article_path.stem)
        if not force:
            existing = load_sidecar(sc_path)
            if existing.get("internal_link_suggestions"):
                continue

        article = _load_json_or_none(article_path)
        if not isinstance(article, dict):
            logger.warning("skip %s: failed to read/parse", article_path)
            processed_count += 1
            summary["processed"] += 1
            summary["errors"] += 1
            continue

        narrative = article.get("narrative") if isinstance(article.get("narrative"), dict) else {}
        section_paragraphs = build_section_paragraphs(narrative)
        processed_count += 1
        summary["processed"] += 1

        if not section_paragraphs:
            continue

        target_summaries: dict[str, str] = {}
        for c in candidates:
            t_article = _get_target_article(c["asin"])
            if t_article is not None:
                target_summaries[c["asin"]] = _target_summary(t_article)

        sections_text = _format_sections_for_prompt(section_paragraphs)
        candidates_text = _format_candidates_for_prompt(candidates, target_summaries)

        result = call_gemma_internal_links(sections_text, candidates_text, ollama_url, model, session, sleeper)
        if result.get("error"):
            summary["errors"] += 1
            logger.error("generation failed for %s: %s", article_path.stem, result["error"])
            continue

        candidate_score_by_asin = {c["asin"]: c["score"] for c in candidates}
        validated, dropped_count = validate_link_suggestions(
            result.get("links") or [], narrative, source_asin, valid_target_asins, unavailable_targets,
        )
        summary["links_dropped"] += dropped_count

        if not validated:
            continue

        summary["links_written"] += len(validated)
        summary["articles_with_links"] += 1

        for link in validated:
            manifest_entries.append({
                "source_asin": source_asin,
                "target_asin": link["target_asin"],
                "section": link["section"],
                "paragraph_index": link["paragraph_index"],
                "anchor_text": link["anchor_text"],
                "semantic_score": candidate_score_by_asin.get(link["target_asin"]),
                "reason": link["reason"],
            })

        if not dry_run:
            update_sidecar(sc_path, {"internal_link_suggestions": validated})

    manifest_payload = {
        "generated_at": _now_iso(),
        "processed": summary["processed"],
        "dry_run": dry_run,
        "links": manifest_entries,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    logger.info(
        "done: processed=%d links_written=%d links_dropped=%d articles_with_links=%d errors=%d",
        summary["processed"], summary["links_written"], summary["links_dropped"],
        summary["articles_with_links"], summary["errors"],
    )
    print(json.dumps(summary, ensure_ascii=False))
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--articles-dir", default=DEFAULT_ARTICLES_DIR, help="記事 JSON のディレクトリ")
    ap.add_argument("--semantic-related", default=DEFAULT_SEMANTIC_RELATED, help="Ruri v3 近傍 JSON のパス")
    ap.add_argument("--census", default=DEFAULT_CENSUS, help="GSC index census JSON のパス")
    ap.add_argument("--history-dir", default=DEFAULT_HISTORY_DIR, help="GSC 週次履歴のディレクトリ")
    ap.add_argument("--history-file", default=None, help="明示的な履歴ファイルパス (未指定なら最新週を自動選択)")
    ap.add_argument("--ollama-url", default=os.environ.get("OLLAMA_URL", DEFAULT_OLLAMA_URL))
    ap.add_argument("--model", default=os.environ.get("SEO_GEN_MODEL", DEFAULT_MODEL))
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="1 回の実行で処理する記事数の上限")
    ap.add_argument("--min-score", type=float, default=DEFAULT_MIN_SCORE, help="semantic score のこの値未満は候補から除外")
    ap.add_argument("--max-inbound", type=int, default=DEFAULT_MAX_INBOUND, help="1 ターゲットあたりの被リンク上限")
    ap.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE_HOURS, help="semantic_related.json の許容鮮度 (時間)")
    ap.add_argument("--manifest", default=DEFAULT_MANIFEST, help="監査用 manifest JSON の出力先")
    ap.add_argument("--force", action="store_true", help="既に internal_link_suggestions がある記事も再生成する")
    ap.add_argument("--dry-run", action="store_true", help="sidecar を書かず、manifest のみ出力する")
    args = ap.parse_args()

    summary = run(
        articles_dir=pathlib.Path(args.articles_dir),
        semantic_related_path=pathlib.Path(args.semantic_related),
        census_path=pathlib.Path(args.census),
        history_dir=pathlib.Path(args.history_dir),
        history_file=pathlib.Path(args.history_file) if args.history_file else None,
        ollama_url=args.ollama_url,
        model=args.model,
        limit=args.limit,
        min_score=args.min_score,
        max_inbound=args.max_inbound,
        max_age_hours=args.max_age_hours,
        manifest_path=pathlib.Path(args.manifest),
        force=args.force,
        dry_run=args.dry_run,
    )
    return 1 if summary.get("aborted") else 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
