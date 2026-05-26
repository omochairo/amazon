"""Article quality gate for おもちゃいろ v4.

Checks article JSON (and rendered Markdown if available) against:
- Schema (data/schema/article.schema.json)
- Word/char count thresholds
- Heading hierarchy (h1 -> h2 -> h3)
- Required sections in narrative
- SEO: product-name occurrence in title/meta/h1/h2/body
- Forbidden childish tone tokens
- FAQ completeness

Outputs a per-article quality score JSON next to each input file:
  data/articles/{slug}.quality.json

Exits non-zero if ANY article fails the configured minimum.

Usage:
    python scripts/quality_gate.py
    python scripts/quality_gate.py --src data/articles/ --posts hugo/content/posts/
    python scripts/quality_gate.py --min-score 70 --strict
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from dataclasses import dataclass, field
from typing import Any

try:
    # Reuse build_post's verification logic so quality_gate and the actual
    # rendered template agree on what counts as "verified". Loaded lazily so
    # quality_gate still works when run in isolation without build_post on
    # the import path.
    from build_post import _load_matched_index as _bp_load_matched_index
    from build_post import _matched_passes_quality as _bp_matched_passes_quality
except ImportError:  # pragma: no cover - best-effort fallback
    _bp_load_matched_index = None
    _bp_matched_passes_quality = None
from typing import Any

try:
    from jsonschema import Draft7Validator
except ImportError:
    Draft7Validator = None  # type: ignore


# 幼児口調・子ども向け演出は禁止（女性誌調をキープするため）。
# 「おもちゃロボ」はサイト公式キャラ（AI編集ロボの名称）として narrative や
# editorial_comment に登場可。ただし「おもちゃロボがしらべたよ」のように
# 幼児口調と組み合わせると下記パターンに引っかかるので注意。
FORBIDDEN_TONE_PATTERNS = [
    r"だよ[。！\s]",
    r"なんだ[。！\s]",
    r"みてね",
    r"しらべたよ",
    r"ぼく[はが、]",
    r"だね[。！\s]",
]

REQUIRED_NARRATIVE_KEYS = [
    "lead",
    "why_this_product",
    "gift_appeal",
    "daily_use",
    "safety_note",
    "closing",
]

NARRATIVE_MIN_CHARS = {
    "lead": 120,
    "why_this_product": 150,
    "gift_appeal": 120,
    "daily_use": 150,
    "safety_note": 120,
    "closing": 120,
}


@dataclass
class CheckResult:
    name: str
    passed: bool
    score: float  # 0.0 - 1.0
    message: str = ""


@dataclass
class ArticleReport:
    slug: str
    path: str
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def total_score(self) -> int:
        if not self.checks:
            return 0
        return int(round(sum(c.score for c in self.checks) / len(self.checks) * 100))

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "path": self.path,
            "total_score": self.total_score,
            "passed": self.passed,
            "checks": [
                {"name": c.name, "passed": c.passed, "score": round(c.score, 2), "message": c.message}
                for c in self.checks
            ],
        }


def _is_legacy_article(data: dict) -> bool:
    """Check if the article date is on or before 2026-05-17 to treat it as legacy."""
    date_str = data.get("date")
    if not date_str:
        return True
    try:
        dt = date_str.split("T")[0]
        if dt <= "2026-05-17":
            return True
    except Exception:
        pass
    return False


def _count_chars(text) -> int:
    if text is None:
        return 0
    if isinstance(text, str):
        return len(text)
    if isinstance(text, list):
        return sum(len(s) for s in text if isinstance(s, str))
    return 0


def check_schema(data: dict, schema: dict) -> CheckResult:
    if Draft7Validator is None:
        return CheckResult("schema", True, 1.0, "jsonschema not installed; skipped")
    validator = Draft7Validator(schema)
    errors = sorted(validator.iter_errors(data), key=lambda e: e.path)
    if not errors:
        return CheckResult("schema", True, 1.0, "OK")
    msgs = [f"{'/'.join(map(str, e.path))}: {e.message}" for e in errors[:5]]
    return CheckResult("schema", False, 0.0, "; ".join(msgs))


def check_title_seo(data: dict, product_name: str) -> CheckResult:
    title = data.get("title", "")
    if not title:
        return CheckResult("title_seo", False, 0.0, "empty title")
    head = title[:60]
    has_name = product_name and product_name in head
    length_ok = 20 <= len(title) <= 80
    score = (1.0 if has_name else 0.0) * 0.7 + (1.0 if length_ok else 0.0) * 0.3
    msg = []
    if not has_name:
        msg.append(f"product name '{product_name}' missing in first 60 chars")
    if not length_ok:
        msg.append(f"length {len(title)} not in 20-80")
    return CheckResult("title_seo", score >= 0.7, score, "; ".join(msg) or "OK")


def check_meta_description(data: dict, product_name: str) -> CheckResult:
    meta = data.get("meta_description", "")
    head = meta[:40]
    has_name = product_name and product_name in head
    length_ok = 100 <= len(meta) <= 160
    score = (1.0 if has_name else 0.0) * 0.6 + (1.0 if length_ok else 0.0) * 0.4
    msg = []
    if not has_name:
        msg.append("product name missing in first 40 chars")
    if not length_ok:
        msg.append(f"length {len(meta)} not in 100-160")
    return CheckResult("meta_description", score >= 0.7, score, "; ".join(msg) or "OK")


def check_keywords(data: dict, product_name: str, brand: str) -> CheckResult:
    kws = data.get("keywords", [])
    if not isinstance(kws, list):
        return CheckResult("keywords", False, 0.0, "keywords not a list")
    count_ok = 5 <= len(kws) <= 15
    has_product = any(product_name and product_name in k for k in kws)
    has_brand = bool(brand) and any(brand in k for k in kws)
    score = sum([count_ok, has_product, has_brand]) / 3.0
    msg = []
    if not count_ok:
        msg.append(f"count {len(kws)} not in 5-15")
    if not has_product:
        msg.append("product name not in any keyword")
    if not has_brand and brand:
        msg.append(f"brand '{brand}' not in any keyword")
    return CheckResult("keywords", score >= 0.66, score, "; ".join(msg) or "OK")


def check_narrative(data: dict, product_name: str) -> CheckResult:
    narrative = data.get("narrative", {})
    missing = [k for k in REQUIRED_NARRATIVE_KEYS if k not in narrative]
    if missing:
        return CheckResult("narrative", False, 0.0, f"missing keys: {missing}")

    issues = []
    char_score_sum = 0.0
    for key in REQUIRED_NARRATIVE_KEYS:
        text = narrative.get(key, "")
        min_chars = NARRATIVE_MIN_CHARS.get(key, 100)
        actual = _count_chars(text)
        if actual < min_chars:
            issues.append(f"{key} {actual}<{min_chars}")
            char_score_sum += actual / min_chars
        else:
            char_score_sum += 1.0

    total = char_score_sum / len(REQUIRED_NARRATIVE_KEYS)
    msg = "; ".join(issues) if issues else "OK"
    return CheckResult("narrative", total >= 0.7, total, msg)


def check_faq(data: dict, product_name: str) -> CheckResult:
    faq = data.get("faq", [])
    if not isinstance(faq, list):
        return CheckResult("faq", False, 0.0, "faq not a list")
    count_ok = len(faq) >= 3
    name_in_q = sum(1 for f in faq if product_name and product_name in f.get("question", ""))
    answers_ok = all(_count_chars(f.get("answer", "")) >= 30 for f in faq)
    score = (1.0 if count_ok else len(faq) / 3.0) * 0.4 + (min(name_in_q / 2, 1.0)) * 0.3 + (1.0 if answers_ok else 0.5) * 0.3
    msg = []
    if not count_ok:
        msg.append(f"only {len(faq)} FAQ items (need >=3)")
    if name_in_q < 2:
        msg.append(f"only {name_in_q} questions contain product name (recommend >=2)")
    if not answers_ok:
        msg.append("some answers too short (<30 chars)")
    return CheckResult("faq", score >= 0.7, score, "; ".join(msg) or "OK")


def check_score_rationale(data: dict) -> CheckResult:
    rationale = data.get("product", {}).get("ivs_detail", {}).get("score_rationale", [])
    if not isinstance(rationale, list):
        return CheckResult("score_rationale", False, 0.0, "score_rationale not a list")
    count = len(rationale)
    well_formed = sum(
        1 for r in rationale
        if isinstance(r, dict) and r.get("factor") and r.get("delta") and len(r.get("reason", "")) >= 10
    )
    score = min(well_formed / 3.0, 1.0)
    msg = []
    if count < 3:
        msg.append(f"only {count} rationale entries (need >=3)")
    if well_formed < count:
        msg.append(f"{count - well_formed} entries malformed")
    return CheckResult("score_rationale", score >= 0.7, score, "; ".join(msg) or "OK")


_V5_VALID_EDU_DOMAINS = {"STEM", "言語", "運動", "想像"}
_V5_TARGET_AGE_RE = re.compile(r"\d+\s*(歳|才|ヶ月)")


def check_target_age(data: dict) -> CheckResult:
    """v5: product.target_age が正規化表記である。フィールド無しは skip (score 1.0)."""
    product = data.get("product") or {}
    if "target_age" not in product:
        return CheckResult("target_age_v5", True, 1.0, "field absent (legacy article, skipped)")
    raw = product.get("target_age")
    if not isinstance(raw, str) or not raw.strip():
        return CheckResult("target_age_v5", False, 0.0, "target_age must be non-empty string")
    if not _V5_TARGET_AGE_RE.search(raw):
        return CheckResult("target_age_v5", False, 0.0, f"target_age '{raw[:30]}' lacks digits+歳/才/ヶ月")
    return CheckResult("target_age_v5", True, 1.0, "OK")


_SALES_PAGE_HOSTS = (
    "amazon.co.jp", "amazon.com",
    "rakuten.co.jp", "hb.afl.rakuten.co.jp", "search.rakuten.co.jp",
    "shopping.yahoo.co.jp", "ck.jp.ap.valuecommerce.com", "store.shopping.yahoo.co.jp",
)


def _is_sales_source(src: dict) -> bool:
    url = (src.get("url") or "").lower()
    return any(h in url for h in _SALES_PAGE_HOSTS)


# 検索エンジン結果ページ自体は独立した第三者ソースになり得ない (PR #325 後継)
# Jules が「検索した URL をそのまま src に貼る」手抜きを禁止する
_SEARCH_ENGINE_URL_PATTERNS = (
    "html.duckduckgo.com",
    "duckduckgo.com/?q=", "duckduckgo.com/html",
    "www.google.com/search", "google.com/search?",
    "www.bing.com/search", "bing.com/search?",
    "search.yahoo.co.jp", "search.yahoo.com",
    "search.brave.com",
    "www.baidu.com/s?", "baidu.com/s?",
)


def _is_search_engine_url(url: str) -> bool:
    u = (url or "").lower()
    return any(p in u for p in _SEARCH_ENGINE_URL_PATTERNS)


def _normalize_url(url: str) -> str:
    """URL 正規化: 同一 URL を src 間で重複検出するための比較キー。
    末尾スラッシュ・大小文字・affiliate tag を吸収するが、path や query 構造は保持。
    """
    if not url:
        return ""
    u = url.strip().lower()
    # affiliate tag を剥がす (Amazon の ?tag= や &tag= が代表)
    u = re.sub(r"[?&]tag=[^&]*", "", u)
    u = re.sub(r"[?&]utm_[a-z_]+=[^&]*", "", u)
    # 残った "?" や "&" の連結を整える
    u = re.sub(r"\?&", "?", u)
    u = re.sub(r"&+", "&", u)
    u = u.rstrip("?&/")
    return u


def check_certifications(data: dict) -> CheckResult:
    """v5: product.certifications が list。空配列も可。フィールド無しは skip.

    追加 (P2): certifications が非空のとき、その cert 名を本文で主張している
    claim を探し、その claim の supporting_source_ids が空または全販売ページ
    だけだった場合は failure とする (§6.5.6 ハルシネーション防止)。
    """
    product = data.get("product") or {}
    if "certifications" not in product:
        return CheckResult("certifications_v5", True, 1.0, "field absent (legacy article, skipped)")
    val = product.get("certifications")
    if not isinstance(val, list):
        return CheckResult("certifications_v5", False, 0.0, "certifications must be a list")
    non_str = [c for c in val if not isinstance(c, str)]
    if non_str:
        return CheckResult("certifications_v5", False, 0.0, f"non-string entries: {non_str[:3]}")
    certs = [c for c in val if c]
    if not certs:
        return CheckResult("certifications_v5", True, 1.0, "OK (empty)")

    claims = data.get("claims") or []
    sources_by_id = {s.get("id"): s for s in (data.get("sources") or []) if isinstance(s, dict) and s.get("id")}

    # cert 名を含む claim を抽出
    mentioning = [
        c for c in claims
        if isinstance(c, dict) and isinstance(c.get("claim"), str)
        and any(cert in c["claim"] for cert in certs)
    ]
    if not mentioning:
        return CheckResult(
            "certifications_v5", False, 0.0,
            f"certifications {certs} 主張だが本文 claim 内に cert 名を含む裏付け記述が無い",
        )

    bad = []
    for cl in mentioning:
        sids = cl.get("supporting_source_ids") or []
        srcs = [sources_by_id.get(sid) for sid in sids if sources_by_id.get(sid)]
        if not srcs:
            bad.append((cl.get("claim", "")[:60], "sources=空"))
            continue
        if all(_is_sales_source(s) for s in srcs):
            bad.append((cl.get("claim", "")[:60], f"全販売ページ ({len(srcs)}件)"))
    if bad:
        first = bad[0]
        return CheckResult(
            "certifications_v5", False, 0.0,
            f"certifications {certs} の裏付け不足: claim='{first[0]}' → {first[1]}",
        )
    return CheckResult("certifications_v5", True, 1.0, "OK")


def check_source_uniqueness(data: dict) -> CheckResult:
    """sources 構造健全性チェック (v5 §6.5.1 補強):
    1. 検索エンジン結果ページ URL (DuckDuckGo/Google 等) は src に不可
    2. 同一 URL (正規化後) が 2 つ以上の src id に分割されていない (1 URL multi-source 化禁止)
    """
    if "sources" not in data or _is_legacy_article(data):
        return CheckResult("source_uniqueness", True, 1.0, "field absent or legacy article (skipped)")
    srcs = data.get("sources") or []
    if not isinstance(srcs, list) or not srcs:
        return CheckResult("source_uniqueness", True, 1.0, "OK (empty)")

    # 1. 検索エンジン URL
    bad_search = []
    for s in srcs:
        if not isinstance(s, dict):
            continue
        url = s.get("url") or ""
        if _is_search_engine_url(url):
            bad_search.append((s.get("id", "?"), url[:100]))
    if bad_search:
        sid, u = bad_search[0]
        return CheckResult(
            "source_uniqueness", False, 0.0,
            f"検索エンジン結果ページが src に含まれる: {sid}={u}",
        )

    # 2. URL 重複 (正規化後)
    seen: dict[str, list[str]] = {}
    for s in srcs:
        if not isinstance(s, dict):
            continue
        norm = _normalize_url(s.get("url") or "")
        if not norm:
            continue
        seen.setdefault(norm, []).append(s.get("id") or "?")
    dups = {u: ids for u, ids in seen.items() if len(ids) >= 2}
    if dups:
        u0 = next(iter(dups))
        return CheckResult(
            "source_uniqueness", False, 0.0,
            f"同一 URL が複数 src に分割: {dups[u0]} → {u0[:100]}",
        )

    return CheckResult("source_uniqueness", True, 1.0, "OK")


def check_sources_v5(data: dict) -> CheckResult:
    """v5 §6.5.1 件数規律: sources は最低 5 件、うち非販売 (第三者) が 2 件以上。

    セッション 19 で「certs=[] にすれば cert 系 check が全 skip → sources を
    緩めても通過する」盲点が判明 (例: B0GFVV4YG9 は販売 5 件のみ / B0C8HM1F94
    は sources=3)。プロンプトには明記されているが gate 未強制だったため追加。

    判定:
    - sources field 無し → skip (legacy article)
    - sources が list でない → fail
    - len(sources) < 5 → fail
    - 非販売 (= _is_sales_source False) が 2 件未満 → fail
    """
    if "sources" not in data or _is_legacy_article(data):
        return CheckResult("sources_v5", True, 1.0, "field absent or legacy article (skipped)")
    srcs = data.get("sources") or []
    if not isinstance(srcs, list):
        return CheckResult("sources_v5", False, 0.0, "sources must be a list")

    valid = [s for s in srcs if isinstance(s, dict) and s.get("url")]
    total = len(valid)
    if total < 5:
        return CheckResult(
            "sources_v5", False, 0.0,
            f"sources={total} 件 (v5 §6.5.1 最低 5 件必須)",
        )

    non_sales = [s for s in valid if not _is_sales_source(s)]
    if len(non_sales) < 2:
        sales_count = total - len(non_sales)
        return CheckResult(
            "sources_v5", False, 0.0,
            f"非販売 source={len(non_sales)} 件 / 販売={sales_count} 件 "
            f"(v5 §6.5.1 第三者 2 件以上必須)",
        )
    return CheckResult("sources_v5", True, 1.0, f"OK (total={total}, non_sales={len(non_sales)})")


# cert 名と HTML 本文内で許容するトークン (alias) の対応
# 個別 cert 主張時に、第三者 source HTML がこれらのいずれかを含めば OK
_CERT_HTML_TOKENS: dict[str, tuple[str, ...]] = {
    "食品衛生法": ("食品衛生法", "食品衛生", "食品級", "食用級"),
    "EN71": ("EN71", "EN 71", "EN-71"),
    "ASTM": ("ASTM",),
    "CE": ("CE承認", "CE認証", "CEマーク", "CE基準", "CE適合", "CE標準", "EN71"),
    "ST": ("STマーク", "ST基準", "ST規格", "ST認証", "日本玩具協会", "玩具安全基準"),
    "PSC": ("PSCマーク", "PSC認証", "PSC基準", "PSC適合", "消費生活用製品安全法"),
    "KC": ("KCマーク", "KC認証", "KC基準"),
}


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_SCRIPT_RE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)


def _fetch_url_text(url: str, *, cache: dict[str, str | None], timeout: float = 8.0) -> str | None:
    """source URL を fetch して本文テキストを返す。失敗時 None。簡易 cache 付き。
    bs4 を避け、regex で <script>/<style>/<noscript> と HTML タグを剥がす軽量版。
    cert トークンは plain string なので tag stripping が雑でも捕捉できる。
    """
    if url in cache:
        return cache[url]
    try:
        import requests  # type: ignore
    except ImportError:
        cache[url] = None
        return None
    try:
        r = requests.get(
            url,
            timeout=timeout,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (compatible; omochairo-quality-gate/1.0; "
                    "+https://omochairo.github.io/amazon/)"
                ),
                "Accept-Language": "ja,en;q=0.5",
            },
            allow_redirects=True,
        )
        if r.status_code >= 400:
            cache[url] = None
            return None
        ct = r.headers.get("Content-Type", "").lower()
        if "html" not in ct and "text" not in ct:
            cache[url] = None
            return None
        raw = r.text
        raw = _HTML_SCRIPT_RE.sub(" ", raw)
        text = _HTML_TAG_RE.sub(" ", raw)
        cache[url] = text
        return text
    except Exception:
        cache[url] = None
        return None


def check_cert_sources_content(data: dict, *, fetch_enabled: bool = True) -> CheckResult:
    """v5 §6.5.6 補強 (P0.5): cert claim の supporting 第三者 source HTML を
    fetch して cert 名 (or alias) トークンが本文に出現するか検証する。

    PR #313 ゲートは「non-sales URL を 1 件 supporting に挙げる」だけで通過する
    構造だったため、B07GCYY4M6 の kanamiblog のように cert を一切言及しない
    blog を Jules が「形だけ」埋めて gate 突破する事例が確認された (セッション18)。

    判定:
    - certifications 空 → skip
    - fetch_enabled=False → skip (local dryrun 用)
    - cert ごとに claim を抽出 → 非販売 supporting source HTML を順次 fetch
    - 1 source でも cert token を含めば、その cert は OK
    - 全 fetch 成功かつ cert token 0 件 → fail
    - 全 fetch 失敗 (network/4xx/5xx) → warn (score 0.7, passed=True) — flake 耐性
    """
    product = data.get("product") or {}
    if "certifications" not in product:
        return CheckResult("cert_sources_content", True, 1.0, "field absent (skipped)")
    certs = [c for c in (product.get("certifications") or []) if isinstance(c, str) and c]
    if not certs:
        return CheckResult("cert_sources_content", True, 1.0, "no certifications (skipped)")
    if not fetch_enabled:
        return CheckResult("cert_sources_content", True, 1.0, "network disabled (skipped)")

    claims = data.get("claims") or []
    sources_by_id = {
        s.get("id"): s for s in (data.get("sources") or [])
        if isinstance(s, dict) and s.get("id")
    }
    cache: dict[str, str | None] = {}

    indeterminate_certs: list[str] = []
    failed_certs: list[tuple[str, str]] = []  # (cert, reason)

    for cert in certs:
        tokens = _CERT_HTML_TOKENS.get(cert, (cert,))
        mentioning = [
            c for c in claims
            if isinstance(c, dict) and isinstance(c.get("claim"), str) and cert in c["claim"]
        ]
        if not mentioning:
            # cert listed but no claim mentions it — caught by check_certifications already
            continue

        # この cert を主張する全 claim の非販売 supporting を集約
        non_sales_srcs: list[dict] = []
        for cl in mentioning:
            for sid in cl.get("supporting_source_ids") or []:
                s = sources_by_id.get(sid)
                if s and not _is_sales_source(s) and s.get("url"):
                    non_sales_srcs.append(s)
        if not non_sales_srcs:
            # 全販売 supporting — check_certifications 側で既に fail されるはず
            continue

        # 重複 URL は 1 回だけ fetch
        seen_urls = []
        for s in non_sales_srcs:
            u = s.get("url") or ""
            if u and u not in seen_urls:
                seen_urls.append(u)

        any_fetch_success = False
        token_found = False
        for url in seen_urls:
            text = _fetch_url_text(url, cache=cache)
            if text is None:
                continue
            any_fetch_success = True
            if any(tok in text for tok in tokens):
                token_found = True
                break

        if token_found:
            continue
        if not any_fetch_success:
            indeterminate_certs.append(cert)
            continue
        failed_certs.append((cert, f"非販売 source {len(seen_urls)} 件 fetch 成功 / token {tokens} 言及無し"))

    if failed_certs:
        cert, reason = failed_certs[0]
        return CheckResult(
            "cert_sources_content", False, 0.0,
            f"certifications '{cert}' の第三者 source 本文に cert 言及無し: {reason}",
        )
    if indeterminate_certs:
        return CheckResult(
            "cert_sources_content", True, 0.7,
            f"network 失敗で未検証: {indeterminate_certs}",
        )
    return CheckResult("cert_sources_content", True, 1.0, "OK")


def check_edu_domains(data: dict) -> CheckResult:
    """v5: product.edu_domains が {STEM,言語,運動,想像} の部分集合。空配列も可。フィールド無しは skip."""
    product = data.get("product") or {}
    if "edu_domains" not in product:
        return CheckResult("edu_domains_v5", True, 1.0, "field absent (legacy article, skipped)")
    val = product.get("edu_domains")
    if not isinstance(val, list):
        return CheckResult("edu_domains_v5", False, 0.0, "edu_domains must be a list")
    invalid = [d for d in val if d not in _V5_VALID_EDU_DOMAINS]
    if invalid:
        return CheckResult(
            "edu_domains_v5", False, 0.0,
            f"invalid domains: {invalid[:3]} (allowed: STEM, 言語, 運動, 想像)"
        )
    return CheckResult("edu_domains_v5", True, 1.0, "OK")


def check_prices_verified(data: dict) -> CheckResult:
    """Warn-only: count rakuten/yahoo price entries that have a clickable URL
    but lack deterministic cross_search verification. Build_post tags entries
    with ``verified=True`` only when ``data/raw/{rakuten,yahoo}_matched.json``
    confirms an ASIN-level match. Jules-supplied URLs without that match are
    legitimate (often correct) but carry a higher chance of pointing to a
    look-alike product, especially for no-brand listings. We surface the count
    in the report (and modestly nudge the score) but never strict-fail —
    blocking every Jules PR with an unverified Yahoo link would be too harsh.
    """
    product = data.get("product") or {}
    prices = product.get("prices") or {}
    unverified: list[str] = []
    verified: list[str] = []
    for key in ("rakuten", "yahoo"):
        entry = prices.get(key)
        if not isinstance(entry, dict):
            continue
        try:
            p = int(entry.get("price") or 0)
        except (TypeError, ValueError):
            p = 0
        if p <= 0 or not entry.get("url"):
            continue  # no clickable link rendered to readers
        if entry.get("verified") is True:
            verified.append(key)
        else:
            unverified.append(key)
    total = len(verified) + len(unverified)
    if total == 0:
        return CheckResult("prices_verified", True, 1.0, "no rakuten/yahoo links present")
    if not unverified:
        return CheckResult("prices_verified", True, 1.0, f"all {total} link(s) verified via cross_search")
    score = max(0.5, 1.0 - 0.1 * len(unverified))
    msg = (
        f"unverified={','.join(unverified)} verified={','.join(verified) or 'none'} "
        f"(warn-only; template renders ※確度低 badge + search fallback)"
    )
    return CheckResult("prices_verified", True, score, msg)


def check_no_reseller_pricing(data: dict) -> CheckResult:
    """Detect ASINs whose Amazon Buy Box price is dramatically higher than
    the same product on Rakuten / Yahoo — a strong indicator that the ASIN
    is a third-party reseller scam (e.g. B0G398BYV6 Switch Mario Wonder
    listed at ¥8480 when official ASIN B0C8Y9THVS is ¥5980).

    Hard fail when amazon_price > 1.3x min(rakuten, yahoo). 1.3x balances
    legitimate Amazon markup (1.0-1.15x typical) against clear reseller
    premiums (1.3x+). Only fires when at least one of rakuten/yahoo has a
    real price so the gate doesn't false-trip on niche products.
    """
    product = data.get("product") or {}
    prices = product.get("prices") or {}
    amazon = prices.get("amazon") or {}
    try:
        a_price = int(amazon.get("price") or 0)
    except (TypeError, ValueError):
        a_price = 0
    if a_price <= 0:
        return CheckResult("no_reseller_pricing", True, 1.0, "no amazon price")
    cross_prices: list[tuple[str, int]] = []
    for src in ("rakuten", "yahoo"):
        entry = prices.get(src) or {}
        try:
            p = int(entry.get("price") or 0)
        except (TypeError, ValueError):
            p = 0
        if p > 0:
            cross_prices.append((src, p))
    if not cross_prices:
        return CheckResult("no_reseller_pricing", True, 1.0, "no rakuten/yahoo price to compare")
    best_src, best_p = min(cross_prices, key=lambda x: x[1])
    ratio = a_price / best_p
    if ratio > 1.30:
        asin = product.get("asin", "?")
        msg = (
            f"reseller-pricing suspect: Amazon ¥{a_price} vs {best_src} ¥{best_p} "
            f"(ratio {ratio:.2f}x > 1.30). ASIN {asin} may be a third-party "
            f"reseller scam. Investigate canonical ASIN and add to "
            f"data/asin_blocklist.json if confirmed."
        )
        return CheckResult("no_reseller_pricing", False, 0.0, msg)
    return CheckResult("no_reseller_pricing", True, 1.0, f"OK (ratio {ratio:.2f}x vs {best_src})")


def check_tone(data: dict) -> CheckResult:
    """Scan all narrative + faq text for forbidden childish tone patterns."""
    texts = []
    narrative = data.get("narrative", {})
    texts.extend(narrative.values())
    for f in data.get("faq", []):
        texts.append(f.get("question", ""))
        texts.append(f.get("answer", ""))
    texts.append(data.get("editorial_comment", ""))
    blob = "\n".join(t for t in texts if isinstance(t, str))

    hits = []
    for pat in FORBIDDEN_TONE_PATTERNS:
        for m in re.finditer(pat, blob):
            hits.append((pat, m.group(0)))
            if len(hits) >= 5:
                break
        if len(hits) >= 5:
            break

    if not hits:
        return CheckResult("tone", True, 1.0, "OK")
    msg = ", ".join(f"'{h[1].strip()}'" for h in hits)
    return CheckResult("tone", False, 0.0, f"childish tone detected: {msg}")


def check_heading_hierarchy(md_text: str | None) -> CheckResult:
    """Ensure h1 -> h2 -> h3 ordering in rendered markdown (skip if md not provided)."""
    if not md_text:
        return CheckResult("heading_hierarchy", True, 1.0, "no markdown to check (skipped)")

    headings = re.findall(r"^(#{1,6})\s+(.+)$", md_text, re.MULTILINE)
    if not headings:
        return CheckResult("heading_hierarchy", False, 0.0, "no headings found")

    prev_level = 0
    violations = []
    h1_count = 0
    for marks, text in headings:
        level = len(marks)
        if level == 1:
            h1_count += 1
        if prev_level and level > prev_level + 1:
            violations.append(f"jump from h{prev_level} to h{level} at '{text[:30]}'")
        prev_level = level
    score = 1.0
    msg = []
    if h1_count != 1:
        score -= 0.3
        msg.append(f"h1 count={h1_count} (expected 1)")
    if violations:
        score -= 0.1 * len(violations)
        msg.append(violations[0])
    score = max(0.0, score)
    return CheckResult("heading_hierarchy", score >= 0.7, score, "; ".join(msg) or "OK")


def check_body_word_count(md_text: str | None) -> CheckResult:
    if not md_text:
        return CheckResult("body_word_count", True, 1.0, "no markdown (skipped)")
    plain = re.sub(r"```.*?```", "", md_text, flags=re.DOTALL)
    plain = re.sub(r"<[^>]+>", "", plain)
    plain = re.sub(r"[\#\*\-\|\>\[\]\(\)`]", "", plain)
    chars = len(re.sub(r"\s+", "", plain))
    score = min(chars / 2000.0, 1.0)
    msg = f"{chars} chars (target>=2000)"
    return CheckResult("body_word_count", chars >= 1600, score, msg)


def _derive_verified_status(
    data: dict,
    rakuten_idx: dict[str, Any] | None,
    yahoo_idx: dict[str, Any] | None,
) -> None:
    """In-place: mirror ``build_post._attach_market_prices()`` for the
    ``verified`` flag so ``check_prices_verified`` can be called on raw Jules
    JSON before build_post has rewritten it. No-op when matched indexes are
    not available or the build_post helpers are not importable."""
    if _bp_matched_passes_quality is None or rakuten_idx is None or yahoo_idx is None:
        return
    product = data.get("product") if isinstance(data.get("product"), dict) else None
    if not product:
        return
    asin = product.get("asin")
    if not asin:
        return
    prices = product.get("prices") or {}
    try:
        amazon_price = int((prices.get("amazon") or {}).get("price") or 0)
    except (TypeError, ValueError):
        amazon_price = 0
    for key, idx in (("rakuten", rakuten_idx), ("yahoo", yahoo_idx)):
        entry = prices.get(key)
        if not isinstance(entry, dict):
            continue
        matched = (idx or {}).get(asin)
        matched_ok = bool(matched) and _bp_matched_passes_quality(matched, amazon_price)
        if matched_ok:
            entry["verified"] = True
            continue
        if entry.get("price") or entry.get("url"):
            entry["verified"] = False


def evaluate_article(
    json_path: pathlib.Path,
    schema: dict,
    md_path: pathlib.Path | None,
    *,
    rakuten_idx: dict[str, Any] | None = None,
    yahoo_idx: dict[str, Any] | None = None,
    cert_fetch: bool = True,
) -> ArticleReport:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    slug = data.get("slug", json_path.stem)
    md_text: str | None = None
    if md_path and md_path.exists():
        md_text = md_path.read_text(encoding="utf-8")

    _derive_verified_status(data, rakuten_idx, yahoo_idx)

    product = data.get("product", {})
    product_name = product.get("name", "")
    brand = product.get("brand", "")

    report = ArticleReport(slug=slug, path=str(json_path))
    report.checks.append(check_schema(data, schema))
    report.checks.append(check_title_seo(data, product_name))
    report.checks.append(check_meta_description(data, product_name))
    report.checks.append(check_keywords(data, product_name, brand))
    report.checks.append(check_narrative(data, product_name))
    report.checks.append(check_faq(data, product_name))
    report.checks.append(check_score_rationale(data))
    report.checks.append(check_target_age(data))
    report.checks.append(check_certifications(data))
    report.checks.append(check_source_uniqueness(data))
    report.checks.append(check_sources_v5(data))
    report.checks.append(check_cert_sources_content(data, fetch_enabled=cert_fetch))
    report.checks.append(check_edu_domains(data))
    report.checks.append(check_prices_verified(data))
    report.checks.append(check_no_reseller_pricing(data))
    report.checks.append(check_tone(data))
    report.checks.append(check_heading_hierarchy(md_text))
    report.checks.append(check_body_word_count(md_text))
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", default="data/articles/")
    parser.add_argument("--posts", default="hugo/content/posts/")
    parser.add_argument("--schema", default="data/schema/article.schema.json")
    parser.add_argument("--min-score", type=int, default=60, help="minimum total score (0-100) to pass")
    parser.add_argument("--strict", action="store_true", help="exit non-zero if any article fails any check")
    parser.add_argument("--write-reports", action="store_true", default=True, help="write {slug}.quality.json")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--no-cert-fetch", action="store_true",
        help="cert HTML content check の HTTP fetch を無効化 (local dryrun 用)",
    )
    args = parser.parse_args()

    src = pathlib.Path(args.src)
    posts = pathlib.Path(args.posts)
    schema_path = pathlib.Path(args.schema)

    if not src.exists():
        print(f"[quality_gate] src not found: {src}")
        return 0
    if not schema_path.exists():
        print(f"[quality_gate] schema not found: {schema_path}")
        return 1

    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    rakuten_idx = None
    yahoo_idx = None
    if _bp_load_matched_index is not None:
        rakuten_matched_path = pathlib.Path("data/raw/rakuten_matched.json")
        yahoo_matched_path = pathlib.Path("data/raw/yahoo_matched.json")
        if rakuten_matched_path.exists():
            rakuten_idx = _bp_load_matched_index(rakuten_matched_path)
        if yahoo_matched_path.exists():
            yahoo_idx = _bp_load_matched_index(yahoo_matched_path)

    json_files = sorted(p for p in src.glob("*.json") if not p.stem.endswith(".enrichment") and not p.stem.endswith(".seo") and not p.stem.endswith(".quality"))

    if not json_files:
        print("[quality_gate] no articles to check")
        return 0

    failures: list[ArticleReport] = []
    below_threshold: list[ArticleReport] = []
    all_reports: list[ArticleReport] = []

    for jp in json_files:
        md_candidate = posts / f"{jp.stem}.md"
        report = evaluate_article(
            jp, schema, md_candidate if md_candidate.exists() else None,
            rakuten_idx=rakuten_idx, yahoo_idx=yahoo_idx,
            cert_fetch=not args.no_cert_fetch,
        )
        all_reports.append(report)
        if args.write_reports:
            out = jp.with_suffix(".quality.json")
            out.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        if not args.quiet:
            status = "OK" if report.passed and report.total_score >= args.min_score else "NG"
            print(f"[{status}] {report.slug} score={report.total_score} ({sum(1 for c in report.checks if c.passed)}/{len(report.checks)} checks)")
            for c in report.checks:
                if not c.passed:
                    print(f"    - {c.name}: {c.message}")
        if not report.passed:
            failures.append(report)
        if report.total_score < args.min_score:
            below_threshold.append(report)

    print()
    print(f"[quality_gate] {len(all_reports)} articles, {len(failures)} with failures, {len(below_threshold)} below min-score {args.min_score}")

    if args.strict and failures:
        return 2
    if below_threshold:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
