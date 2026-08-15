"""audit_title_prefix.py

Issue #5083 項目3「SERP 表示長の観点で、タイトル前半に主要語が収まっている割合を計測する」。

#5083 項目1 (PR #5094) で商品ページのサフィックスは 31 文字 → 「| 比較ナビ」に短縮済み。
残る項目2 は「記事タイトル本体の生成規約を見直す (前半 30 文字に商品名 + 主要検索意図語
1 つが収まる形を AGENTS.md の規約に落とす)」だが、**規約を書く前に現行コーパスが実際に
どうなっているかの実数が無い**。本スクリプトはその判断材料を作る read-only 監査であり、
記事生成パイプラインにも Hugo 側にも一切触れない (副作用ゼロ)。

読むデータ:
  data/articles/*.json。記事の選定は compute_semantic_related.discover_articles を
  そのまま再利用する (sidecar 除外・rewrite 新旧併存時は stem 最新を採用、
  audit_uniqueness.py と同じ規則)。

測るもの (記事 1 件につき):
  1. レンダリング後の <title> = "{title} | {suffix}"。suffix は hugo/config.toml の
     params.productTitleSuffix (= 商品ページの実際のサフィックス) を読む。記事本文の
     title だけを見ると、短いタイトルでサフィックスが表示枠に食い込む分を見落とす。
  2. その先頭を 2 通りの数え方で切る:
     - chars     : 素の文字数 32 (Issue #5083 の文面どおり)
     - fullwidth : 全角換算幅 32.0 (East Asian Width が W/F/A なら 1.0、他は 0.5)
     日本語 SERP の打ち切りは字数でなく表示幅で起きるため、ラテン文字の多い商品名
     (例: "Tamagotchi ネックストラップ Vivid Yellow") は chars だけで測ると不当に
     不利になる。どちらか一方を正としないで両方出し、判断は人間に残す。
  3. 前半に **商品名** が収まっているか: product.name を janome で分かち書きし、
     内容語トークンが正規化済み prefix に何割現れるかを coverage として持つ。
     coverage == 1.0 を「収まっている」とする。
     - 非対称にしている理由: prefix は途中で切った文字列なので、prefix 側を
       形態素解析すると切断境界のトークンを誤って割る。product.name 側だけを
       分割し、prefix には正規化後の部分文字列一致をかける (= 収まっている側に
       倒す) ほうが、誤って「収まっていない」と数えるより安全。
  4. 前半に **検索意図語** が 1 つ以上あるか: build_brand_query_context の
     INTENT_KEYWORDS (ランキング/比較/レビュー/おすすめ/口コミ/評価/評判/人気/
     選び方/最安/安い/コスパ) と _AGE_RE (「6歳」「小学生」等) を再利用する。
     ここで新しい語彙表を作ると、既存の需要駆動レーンと意図語の定義がずれる。

決定に効く補助指標:
  - product_name_length: **商品名そのものが既に上限を超えている記事の件数**。
    項目2 の規約は「前半に商品名 + 意図語」を求めるが、商品名だけで枠を使い切る
    記事が多いなら、その規約はそもそも達成不能であり、規約側 (商品名の短縮規則)
    を先に決める必要がある。
  - intent_first_index: 最初の意図語が現れる位置の分布。現行タイトルで意図語を
    どれだけ前に動かす必要があるかの量。

出力:
  data/analytics/title_prefix_audit.json
  切り詰めるのは samples だけで、件数 (samples_total) は必ず切り詰め前を残す。

使い方:
  python -m scripts.audit_title_prefix
  python -m scripts.audit_title_prefix --limit 30 --out /tmp/x.json
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import statistics
import sys
import unicodedata
from datetime import datetime, timezone
from typing import Any

from scripts.build_brand_query_context import INTENT_KEYWORDS, _AGE_RE, normalize
from scripts.compute_semantic_related import discover_articles

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("audit_title_prefix")

DEFAULT_ARTICLES_DIR = "data/articles"
DEFAULT_OUT = "data/analytics/title_prefix_audit.json"
DEFAULT_HUGO_CONFIG = "hugo/config.toml"
DEFAULT_LIMIT = 32
DEFAULT_MAX_SAMPLES = 20

# hugo/config.toml を読めなかったときだけ使う。head.html の
# `<title>{{ $pageTitle }} | {{ $titleSuffix }}</title>` と同じ組み立て。
FALLBACK_SUFFIX = "比較ナビ"
TITLE_SEPARATOR = " | "

# product.name の分かち書きから落とす品詞 (大分類)。商品名の同定に効かない語だけを
# 落とす。数字は型番・容量として意味を持つので残す。
_DROP_POS = {"助詞", "助動詞", "記号", "接続詞", "フィラー", "その他"}
_KANA = "ぁあぃいぅうぇえぉおかがきぎくぐけげこごさざしじすずせぜそぞただちぢっつづてでとどなにぬねのはばぱひびぴふぶぷへべぺほぼぽまみむめもゃやゅゆょよらりるれろゎわゐゑをんァアィイゥウェエォオカガキギクグケゲコゴサザシジスズセゼソゾタダチヂッツヅテデトドナニヌネノハバパヒビピフブプヘベペホボポマミムメモャヤュユョヨラリルレロヮワヰヱヲンヴー"


# --------------------------------------------------------------------------
# 幅と切り出し (pure)
# --------------------------------------------------------------------------

def fullwidth_width(text: str) -> float:
    """全角換算の表示幅。East Asian Width が W/F/A なら 1.0、他は 0.5。

    'A' (Ambiguous) を全角側に寄せるのは、日本語ロケールの SERP では
    ambiguous 文字 (§ や ± 等) が全角で描画されるため。
    """
    total = 0.0
    for ch in text or "":
        total += 1.0 if unicodedata.east_asian_width(ch) in ("W", "F", "A") else 0.5
    return total


def take_prefix(text: str, limit: float, mode: str) -> str:
    """先頭を limit まで切る。mode='chars' は文字数、'fullwidth' は全角換算幅。"""
    text = text or ""
    if mode == "chars":
        return text[: int(limit)]
    if mode != "fullwidth":
        raise ValueError(f"unknown mode: {mode}")
    out: list[str] = []
    used = 0.0
    for ch in text:
        w = 1.0 if unicodedata.east_asian_width(ch) in ("W", "F", "A") else 0.5
        if used + w > limit:
            break
        out.append(ch)
        used += w
    return "".join(out)


def render_title(title: str, suffix: str) -> str:
    """商品ページの <title> を再現する。

    head.html は `{{ $pageTitle }} | {{ $titleSuffix }}` なので、本文タイトルが
    空でない限り必ずサフィックスが付く。
    """
    title = (title or "").strip()
    suffix = (suffix or "").strip()
    if not title:
        return suffix
    if not suffix:
        return title
    return f"{title}{TITLE_SEPARATOR}{suffix}"


# --------------------------------------------------------------------------
# 商品名の照合 (pure + tokenizer 注入)
# --------------------------------------------------------------------------

def product_tokens(name: str, tokenizer: Any) -> list[str]:
    """product.name を分かち書きし、同定に効く内容語だけを正規化して返す。

    落とすもの: _DROP_POS の品詞、空白、1 文字のかな (「の」「ー」等が
    独立トークンとして残ると、ほぼ全ての prefix にヒットして coverage が
    無意味に上がるため)。
    """
    norm = normalize(name)
    if not norm:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for tok in tokenizer.tokenize(norm):
        surface = (tok.surface or "").strip()
        if not surface:
            continue
        pos = (tok.part_of_speech or "").split(",")[0]
        if pos in _DROP_POS:
            continue
        if len(surface) == 1 and surface in _KANA:
            continue
        if surface in seen:
            continue
        seen.add(surface)
        out.append(surface)
    return out


def product_coverage(name: str, prefix: str, tokenizer: Any) -> tuple[float, list[str]]:
    """商品名トークンのうち prefix に現れる割合と、現れなかったトークンを返す。

    prefix 側は形態素解析せず正規化後の部分文字列一致で判定する (docstring 冒頭の
    非対称性の理由を参照)。商品名が空 = 判定不能なので coverage は 0.0 とし、
    呼び出し側で `product_name_missing` として別に数える。
    """
    tokens = product_tokens(name, tokenizer)
    if not tokens:
        return 0.0, []
    norm_prefix = normalize(prefix)
    missing = [t for t in tokens if t not in norm_prefix]
    return (len(tokens) - len(missing)) / len(tokens), missing


# --------------------------------------------------------------------------
# 意図語 (pure)
# --------------------------------------------------------------------------

def intent_hits(text: str) -> list[str]:
    """text に現れる検索意図語 (INTENT_KEYWORDS + 年齢表現) を出現順で返す。"""
    text = text or ""
    found: list[tuple[int, str]] = []
    for kw in INTENT_KEYWORDS:
        i = text.find(kw)
        if i >= 0:
            found.append((i, kw))
    m = _AGE_RE.search(text)
    if m:
        found.append((m.start(), m.group(0)))
    found.sort()
    return [kw for _, kw in found]


def intent_first_index(text: str) -> int | None:
    """最初の意図語の開始位置。無ければ None。"""
    text = text or ""
    positions: list[int] = []
    for kw in INTENT_KEYWORDS:
        i = text.find(kw)
        if i >= 0:
            positions.append(i)
    m = _AGE_RE.search(text)
    if m:
        positions.append(m.start())
    return min(positions) if positions else None


# --------------------------------------------------------------------------
# 記事 1 件の監査
# --------------------------------------------------------------------------

def audit_article(article: dict[str, Any], *, suffix: str, limit: float,
                  tokenizer: Any) -> dict[str, Any]:
    """記事 JSON 1 件を監査して 1 行分の dict を返す。欠損でクラッシュしない。"""
    title = (article.get("title") or "").strip()
    product = article.get("product") if isinstance(article.get("product"), dict) else {}
    name = (product.get("name") or product.get("name_full") or "").strip()

    rendered = render_title(title, suffix)
    row: dict[str, Any] = {
        "slug": article.get("slug") or "",
        "title": title,
        "product_name": name,
        "rendered_len_chars": len(rendered),
        "rendered_len_fullwidth": fullwidth_width(rendered),
        "product_name_len_chars": len(name),
        "product_name_len_fullwidth": fullwidth_width(name),
        "product_name_missing": not name,
        "intent_first_index_chars": intent_first_index(rendered),
        "modes": {},
    }
    for mode in ("chars", "fullwidth"):
        prefix = take_prefix(rendered, limit, mode)
        coverage, missing = product_coverage(name, prefix, tokenizer)
        hits = intent_hits(prefix)
        row["modes"][mode] = {
            "prefix": prefix,
            "product_coverage": round(coverage, 4),
            "product_missing_tokens": missing,
            "product_full": bool(name) and coverage >= 1.0,
            "intent_hits": hits,
            "has_intent": bool(hits),
            "both": bool(name) and coverage >= 1.0 and bool(hits),
        }
    return row


# --------------------------------------------------------------------------
# 集計
# --------------------------------------------------------------------------

def _pcts(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    s = sorted(values)

    def q(p: float) -> float:
        # 線形補間なしの nearest-rank。件数が 2,000 程度あるので十分。
        i = min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))
        return round(float(s[i]), 4)

    return {
        "p10": q(0.10), "p25": q(0.25), "p50": q(0.50),
        "p75": q(0.75), "p90": q(0.90),
        "mean": round(statistics.fmean(s), 4),
        "min": round(float(s[0]), 4), "max": round(float(s[-1]), 4),
    }


def summarize(rows: list[dict[str, Any]], *, limit: float,
              max_samples: int = DEFAULT_MAX_SAMPLES) -> dict[str, Any]:
    """監査行から判断材料のサマリを組み立てる。"""
    total = len(rows)
    out: dict[str, Any] = {
        "total_articles": total,
        "product_name_missing": sum(1 for r in rows if r["product_name_missing"]),
        "modes": {},
    }
    if not total:
        return out

    def ratio(n: int) -> float:
        return round(n / total, 4)

    for mode in ("chars", "fullwidth"):
        ms = [r["modes"][mode] for r in rows]
        both = sum(1 for m in ms if m["both"])
        prod = sum(1 for m in ms if m["product_full"])
        intent = sum(1 for m in ms if m["has_intent"])
        neither = sum(1 for m in ms if not m["product_full"] and not m["has_intent"])
        # 切り詰めるのは samples だけ。件数は必ず切り詰め前を残す。
        failing = [r for r in rows if not r["modes"][mode]["both"]]
        out["modes"][mode] = {
            "both": both, "both_ratio": ratio(both),
            "product_full": prod, "product_full_ratio": ratio(prod),
            "has_intent": intent, "has_intent_ratio": ratio(intent),
            "neither": neither, "neither_ratio": ratio(neither),
            "product_coverage_pcts": _pcts([m["product_coverage"] for m in ms]),
            "samples_total": len(failing),
            "samples": [
                {
                    "slug": r["slug"],
                    "title": r["title"],
                    "product_name": r["product_name"],
                    "prefix": r["modes"][mode]["prefix"],
                    "product_coverage": r["modes"][mode]["product_coverage"],
                    "product_missing_tokens": r["modes"][mode]["product_missing_tokens"],
                    "intent_hits": r["modes"][mode]["intent_hits"],
                }
                for r in failing[:max_samples]
            ],
        }

    # 商品名だけで枠を使い切る記事 = 項目2 の規約が原理的に達成できない記事。
    named = [r for r in rows if not r["product_name_missing"]]
    over_chars = sum(1 for r in named if r["product_name_len_chars"] > limit)
    over_fw = sum(1 for r in named if r["product_name_len_fullwidth"] > limit)
    out["product_name_length"] = {
        "counted": len(named),
        "chars_pcts": _pcts([float(r["product_name_len_chars"]) for r in named]),
        "fullwidth_pcts": _pcts([r["product_name_len_fullwidth"] for r in named]),
        "over_limit_chars": over_chars,
        "over_limit_chars_ratio": round(over_chars / len(named), 4) if named else 0.0,
        "over_limit_fullwidth": over_fw,
        "over_limit_fullwidth_ratio": round(over_fw / len(named), 4) if named else 0.0,
    }

    out["rendered_length"] = {
        "chars_pcts": _pcts([float(r["rendered_len_chars"]) for r in rows]),
        "fullwidth_pcts": _pcts([r["rendered_len_fullwidth"] for r in rows]),
    }

    idxs = [float(r["intent_first_index_chars"]) for r in rows
            if r["intent_first_index_chars"] is not None]
    out["intent_first_index"] = {
        "with_intent_anywhere": len(idxs),
        "without_intent_anywhere": total - len(idxs),
        "pcts": _pcts(idxs),
    }
    return out


# --------------------------------------------------------------------------
# I/O
# --------------------------------------------------------------------------

def load_suffix(config_path: pathlib.Path) -> str:
    """hugo/config.toml の params.productTitleSuffix を読む。読めなければ既定値。"""
    try:
        import tomllib
        with config_path.open("rb") as fh:
            cfg = tomllib.load(fh)
        suffix = (cfg.get("params") or {}).get("productTitleSuffix")
        if isinstance(suffix, str) and suffix.strip():
            return suffix.strip()
        logger.warning("productTitleSuffix not found in %s; using fallback", config_path)
    except Exception as exc:  # noqa: BLE001 - 監査は config が読めなくても続行する
        logger.warning("failed to read %s (%s); using fallback suffix", config_path, exc)
    return FALLBACK_SUFFIX


def run(articles_dir: pathlib.Path, *, suffix: str, limit: float,
        max_samples: int, max_articles: int | None = None) -> dict[str, Any]:
    from janome.tokenizer import Tokenizer

    tokenizer = Tokenizer()
    paths = discover_articles(articles_dir)
    logger.info("discovered %d articles under %s", len(paths), articles_dir)

    rows: list[dict[str, Any]] = []
    skipped = 0
    for asin in sorted(paths):
        if max_articles is not None and len(rows) >= max_articles:
            break
        try:
            article = json.loads(paths[asin].read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("skip unreadable article %s (%s)", paths[asin], exc)
            skipped += 1
            continue
        if not isinstance(article, dict):
            skipped += 1
            continue
        rows.append(audit_article(article, suffix=suffix, limit=limit,
                                 tokenizer=tokenizer))

    summary = summarize(rows, limit=limit, max_samples=max_samples)
    summary.update({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "articles_dir": str(articles_dir).replace("\\", "/"),
        "title_suffix": suffix,
        "prefix_limit": limit,
        "unreadable_skipped": skipped,
        "intent_keywords": list(INTENT_KEYWORDS),
        "age_pattern": _AGE_RE.pattern,
    })
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--articles-dir", default=DEFAULT_ARTICLES_DIR)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--hugo-config", default=DEFAULT_HUGO_CONFIG)
    ap.add_argument("--limit", type=float, default=DEFAULT_LIMIT,
                    help="前半とみなす長さ (chars モードは文字数、fullwidth モードは全角換算幅)")
    ap.add_argument("--suffix", default=None,
                    help="<title> サフィックス。既定は hugo/config.toml から読む")
    ap.add_argument("--max-samples", type=int, default=DEFAULT_MAX_SAMPLES)
    ap.add_argument("--limit-articles", type=int, default=None,
                    help="デバッグ用: 先頭 N 件だけ処理する")
    args = ap.parse_args(argv)

    suffix = args.suffix if args.suffix is not None else load_suffix(pathlib.Path(args.hugo_config))
    summary = run(pathlib.Path(args.articles_dir), suffix=suffix, limit=args.limit,
                  max_samples=args.max_samples, max_articles=args.limit_articles)

    out_path = pathlib.Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    logger.info("wrote %s", out_path)

    for mode in ("chars", "fullwidth"):
        m = summary.get("modes", {}).get(mode)
        if not m:
            continue
        logger.info(
            "[%s] both=%d (%.1f%%) product_full=%d (%.1f%%) intent=%d (%.1f%%) neither=%d (%.1f%%)",
            mode, m["both"], m["both_ratio"] * 100,
            m["product_full"], m["product_full_ratio"] * 100,
            m["has_intent"], m["has_intent_ratio"] * 100,
            m["neither"], m["neither_ratio"] * 100,
        )
    pn = summary.get("product_name_length", {})
    if pn:
        logger.info("product name over limit: chars=%d (%.1f%%) fullwidth=%d (%.1f%%)",
                    pn["over_limit_chars"], pn["over_limit_chars_ratio"] * 100,
                    pn["over_limit_fullwidth"], pn["over_limit_fullwidth_ratio"] * 100)
    return 0


if __name__ == "__main__":
    sys.exit(main())
