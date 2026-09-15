"""#4841 M2: 素材から「その商品固有の事実カード」を多段で作る。

1. 抽出: 素材のアトミック項目 (amazon.item.features[i] 等、決定的に列挙) を
   番号付きで gemma に渡し、「この商品について具体的に言える」候補文を
   抜き出させる。出典 (ファイル名・フィールド・番号) は gemma の申告に頼らず、
   gemma が答える番号 (source_index) からこちらの一覧に逆引きする。
2. 固有性で絞る: M1-b③ (sentence_metrics.compute_sentence_uniqueness) と同じ
   方法 (Ruri、負の対照=別カテゴリの文の分布のp95)。
3. 裏付けの確認: M1-b② (sentence_metrics.run_sentence_entailment) と同じ
   根拠判定 (文単位、gemma) で、その ASIN の素材全体から導けるかを判定する。
4. カード化: 固有かつ裏付けありの候補から上位 5〜8 件。不満 (aspect=不満) は
   最大1件まで (U1 の方針と揃える)。
"""
from __future__ import annotations

import glob
import json
import pathlib
from typing import Any

import requests

from scripts.experimental.multistage_brief.ollama_client import (
    DEFAULT_MODEL,
    DEFAULT_NUM_CTX,
    DEFAULT_OLLAMA_URL,
    call_gemma,
    parse_json_response,
)
from scripts.experimental.multistage_brief.sentence_metrics import (
    compute_sentence_uniqueness,
    run_sentence_entailment,
)

DEFAULT_RAW_GLOB = "data/raw/per_asin/*"
MAX_CARDS = 8
MIN_CARDS_NOT_THIN = 3
MAX_COMPLAINT_CARDS = 1
COMPLAINT_ASPECT = "不満"

# youtube.json / news.json のアイテムに「本文・字幕」が入っているかの判定に使う
# フィールド名の候補。title/url/thumbnail/published/_relevance_score しか無ければ
# 「タイトルのみ」= 本文カバレッジ0とみなす (#4841 M2 抽出①の事前確認)。
BODY_FIELD_CANDIDATES = ("description", "body", "caption", "captions", "transcript", "subtitle", "content", "text")


# --------------------------------------------------------------------------
# 事前確認: youtube.json / news.json の本文カバレッジ
# --------------------------------------------------------------------------

def _has_body_content(item: dict[str, Any]) -> bool:
    for field in BODY_FIELD_CANDIDATES:
        val = item.get(field)
        if isinstance(val, str) and val.strip():
            return True
    return False


def _coverage_for_source(paths: list[str]) -> dict[str, Any]:
    total_asin = 0
    total_items = 0
    items_with_body = 0
    for p in paths:
        try:
            data = json.loads(pathlib.Path(p).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        items = data.get("items") if isinstance(data, dict) else None
        items = items if isinstance(items, list) else []
        if items:
            total_asin += 1
        for it in items:
            if not isinstance(it, dict):
                continue
            total_items += 1
            if _has_body_content(it):
                items_with_body += 1
    return {
        "asin_with_items": total_asin,
        "item_count": total_items,
        "items_with_body_count": items_with_body,
        "body_coverage": round(items_with_body / total_items, 4) if total_items else None,
    }


def count_body_content_coverage(raw_glob: str = DEFAULT_RAW_GLOB) -> dict[str, Any]:
    """youtube.json / news.json に本文・字幕が実際に入っている割合を数える。

    (#4841 M2 抽出①: 「先に数え、取れた割合を報告する」)。本文フィールドが
    一度も見つからなければ 0.0 を返す (存在しないことを 0 件として明示する。
    None にして「計測不能」と混同しない)。
    """
    youtube_paths = sorted(glob.glob(str(pathlib.Path(raw_glob) / "youtube.json")))
    news_paths = sorted(glob.glob(str(pathlib.Path(raw_glob) / "news.json")))
    return {
        "youtube": _coverage_for_source(youtube_paths),
        "news": _coverage_for_source(news_paths),
    }


# --------------------------------------------------------------------------
# アトミック項目の決定的な列挙 (gemma を使わない)
# --------------------------------------------------------------------------

def build_atomic_material_items(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """素材を「1件=1事実候補の種」に決定的に分解する。

    競合商品 (competitors.json) は「この商品について」の事実ではないため対象外。
    youtube/news はタイトルのみ (本文カバレッジ0、count_body_content_coverage で
    確認済み) で事実の抽出元として弱いため、こちらも対象外にする。
    """
    items: list[dict[str, Any]] = []

    amazon = raw.get("amazon")
    amazon_item = amazon.get("item") if isinstance(amazon, dict) else None
    if isinstance(amazon_item, dict):
        price = amazon_item.get("price")
        if isinstance(price, (int, float)) and price:
            items.append({
                "source_file": "amazon.json", "source_field": "item.price",
                "aspect": None, "text": f"価格は{price}円",
            })
        features = amazon_item.get("features")
        if isinstance(features, list):
            for i, f in enumerate(features):
                text = str(f).strip() if f else ""
                if text:
                    items.append({
                        "source_file": "amazon.json", "source_field": f"item.features[{i}]",
                        "aspect": None, "text": text,
                    })

    experience = raw.get("experience")
    snippets = experience.get("snippets") if isinstance(experience, dict) else None
    if isinstance(snippets, list):
        for i, s in enumerate(snippets):
            if not isinstance(s, dict):
                continue
            text = str(s.get("text", "")).strip()
            if text:
                items.append({
                    "source_file": "experience.json", "source_field": f"snippets[{i}]",
                    "aspect": s.get("aspect"), "text": text,
                })

    for i, it in enumerate(items, start=1):
        it["index"] = i
    return items


# --------------------------------------------------------------------------
# ① 抽出 (gemma)
# --------------------------------------------------------------------------

EXTRACT_PROMPT_TEMPLATE = """あなたは商品カタログの編集者です。以下は、ある知育玩具の商品データの断片を
番号付きで列挙したものです。各項目について、「この商品について具体的に言える」独自の
事実として使えるものを、短い一文の候補にしてください。

# 商品データ断片 (番号付き)
{numbered_items}

# 条件
- 同じ種類の商品なら大抵当てはまる一般的すぎる情報 (「対象年齢は3歳から」の類) は候補にしない
- 各候補には、元にした番号を1つだけ添える (複数の番号を合成した候補は作らない)
- 短い一文 (60文字程度まで) にし、元の文言から大きく言い換えない

次の JSON だけを出力してください (他の説明文は一切含めない):
{{"candidates": [{{"source_index": 1, "fact_text": "..."}}, ...]}}
"""


def build_extract_prompt(atomic_items: list[dict[str, Any]]) -> str:
    numbered = "\n".join(f"{it['index']}. [{it['source_file']}:{it['source_field']}] {it['text']}" for it in atomic_items)
    return EXTRACT_PROMPT_TEMPLATE.format(numbered_items=numbered)


def parse_extract_response(parsed: Any, atomic_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """gemma の候補一覧を、決定的に構築した atomic_items へ逆引きして出典を確定する (pure)。

    source_index が範囲外・型不正の候補は捨てる (出典を確定できない候補を残さない)。
    """
    by_index = {it["index"]: it for it in atomic_items}
    candidates_raw = parsed.get("candidates") if isinstance(parsed, dict) else None
    candidates_raw = candidates_raw if isinstance(candidates_raw, list) else []

    out: list[dict[str, Any]] = []
    for c in candidates_raw:
        if not isinstance(c, dict):
            continue
        idx = c.get("source_index")
        fact_text = c.get("fact_text")
        if not isinstance(idx, int) or not isinstance(fact_text, str) or not fact_text.strip():
            continue
        source = by_index.get(idx)
        if source is None:
            continue
        out.append({
            "source_index": idx,
            "source_file": source["source_file"],
            "source_field": source["source_field"],
            "aspect": source.get("aspect"),
            "text": fact_text.strip(),
        })
    return out


def extract_fact_candidates(
    atomic_items: list[dict[str, Any]],
    *,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    model: str = DEFAULT_MODEL,
    num_ctx: int = DEFAULT_NUM_CTX,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    if not atomic_items:
        return {"candidates": [], "call_meta": None}
    prompt = build_extract_prompt(atomic_items)
    call = call_gemma(
        prompt, ollama_url=ollama_url, model=model, num_ctx=num_ctx, temperature=0.2, format_json=True,
        session=session,
    )
    parsed = parse_json_response(call["text"])
    candidates = parse_extract_response(parsed, atomic_items)
    return {"candidates": candidates, "call_meta": {k: v for k, v in call.items() if k != "text"}}


# --------------------------------------------------------------------------
# ② 固有性 / ③ 裏付け の付与 (既存の sentence_metrics を再利用)
# --------------------------------------------------------------------------

def annotate_uniqueness(
    candidates: list[dict[str, Any]],
    same_category_pool: list[str],
    cross_category_pool: list[str],
    *,
    ruri_url: str,
    session: requests.Session,
) -> list[dict[str, Any]]:
    if not candidates:
        return []
    texts = [c["text"] for c in candidates]
    uniqueness = compute_sentence_uniqueness(texts, same_category_pool, cross_category_pool, ruri_url=ruri_url, session=session)
    out = []
    for candidate, u in zip(candidates, uniqueness["per_sentence"]):
        out.append({**candidate, "unique": u["unique"], "same_category_max_sim": u.get("same_category_max_sim")})
    return out


def annotate_support(
    candidates: list[dict[str, Any]],
    material_text: str,
    *,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    model: str = DEFAULT_MODEL,
    num_ctx: int = DEFAULT_NUM_CTX,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    if not candidates:
        return {"candidates": [], "call_meta": None}
    texts = [c["text"] for c in candidates]
    entail = run_sentence_entailment(material_text, texts, ollama_url=ollama_url, model=model, num_ctx=num_ctx, session=session)
    out = []
    for candidate, supported in zip(candidates, entail["supported_flags"]):
        out.append({**candidate, "supported": supported})
    return {"candidates": out, "call_meta": entail.get("call_meta")}


# --------------------------------------------------------------------------
# ④ カード化
# --------------------------------------------------------------------------

def select_fact_cards(
    candidates: list[dict[str, Any]],
    *,
    max_cards: int = MAX_CARDS,
    max_complaint_cards: int = MAX_COMPLAINT_CARDS,
    complaint_aspect: str = COMPLAINT_ASPECT,
) -> list[dict[str, Any]]:
    """固有 (unique=True) かつ裏付けあり (supported=True) の候補から、抽出順を

    保った上で不満 (aspect=不満) を max_complaint_cards 件まで、全体を max_cards
    件までに絞る (pure function)。
    """
    passed = [c for c in candidates if c.get("unique") is True and c.get("supported") is True]
    selected: list[dict[str, Any]] = []
    complaint_count = 0
    for c in passed:
        if c.get("aspect") == complaint_aspect:
            if complaint_count >= max_complaint_cards:
                continue
            complaint_count += 1
        selected.append(c)
        if len(selected) >= max_cards:
            break
    return selected


def format_fact_cards(cards: list[dict[str, Any]]) -> str:
    """群D プロンプトに埋め込む「事実カード」節のテキストを組み立てる (pure)。"""
    if not cards:
        return "(なし)"
    lines = []
    for c in cards:
        source = f"{c['source_file']}:{c['source_field']}"
        lines.append(f"- [出典: {source}] {c['text']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 1 ASIN ぶんのオーケストレーション (IO: gemma + Ruri を叩く)
# --------------------------------------------------------------------------

def build_fact_cards_for_asin(
    raw: dict[str, Any],
    same_category_pool: list[str],
    cross_category_pool: list[str],
    material_text: str,
    *,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    ruri_url: str,
    model: str = DEFAULT_MODEL,
    num_ctx: int = DEFAULT_NUM_CTX,
    session: requests.Session,
) -> dict[str, Any]:
    atomic_items = build_atomic_material_items(raw)
    extracted = extract_fact_candidates(atomic_items, ollama_url=ollama_url, model=model, num_ctx=num_ctx, session=session)
    candidates = extracted["candidates"]

    candidates = annotate_uniqueness(candidates, same_category_pool, cross_category_pool, ruri_url=ruri_url, session=session)
    support = annotate_support(candidates, material_text, ollama_url=ollama_url, model=model, num_ctx=num_ctx, session=session)
    candidates = support["candidates"]

    selected = select_fact_cards(candidates)

    return {
        "atomic_item_count": len(atomic_items),
        "extraction_candidate_count": len(extracted["candidates"]),
        "extraction_call_meta": extracted["call_meta"],
        "support_call_meta": support["call_meta"],
        "candidates": candidates,
        "selected_cards": selected,
        "selected_card_count": len(selected),
        "is_thin": len(selected) < MIN_CARDS_NOT_THIN,
    }
