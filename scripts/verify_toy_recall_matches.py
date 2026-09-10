"""verify_toy_recall_matches.py

#4320 follow-up: `fetch_toy_recalls.py` が抽出したブランド一致の候補について、
当サイトが実際に掲載している ASIN のどれと同一商品かを agy (Antigravity CLI,
このホスト限定で aisys 認証済み) に判定させ、確信度の高いものだけ
`matched_asins` として書き戻す。

設計判断:
- ブランド一致だけでは「そのブランドの全記事」が対象になり誤検出が大きすぎる
  (#4320 の実測: 24 ブランドで記事の24%)。人力で全件 bigram 突合するのは
  非現実的なので、判定そのものを agy (機械的・大容量入力向き、gemini-3.8-flash-high)
  に委ねる。ただし GitHub Actions のクラウド実行からは agy を呼べない
  (認証がこの K8 ホストの aisys ユーザーのファイルベース認証に紐づく) ため、
  本スクリプトは K8 側で `fetch_toy_recalls.py` の出力 (CI artifact) を読んで
  動かす専用の後続ステップとして分離してある。
- 出力は「一致した ASIN だけ」。自信が無ければ空を返すよう agy に明示指示し、
  応答が想定形式から外れた場合も **必ず「一致なし」に倒す**
  (scripts/draft_sns_reply.py の parse_response と同じ fail-closed 方針。
  誤って「一致」と報告する方が実害が大きい)。
- agy が返す ASIN は、プロンプトに列挙した当該ブランドの ASIN 一覧に含まれる
  ものだけを採用する (幻覚防止のホワイトリスト検証)。
- プロンプトで recall.caa.go.jp の詳細URLを「開いて確認して」と誘導しないこと。
  ヘッドレス実行では `read_url` 等のツール許可を対話プロンプトできず自動 deny
  になり、**エラーにもならず stdout が完全に空になる** (2026-09-10 実測:
  `jetski: no output produced — a tool required the "read_url" permission
  that headless mode cannot prompt for, so it was auto-denied`)。判定材料は
  一覧ページのタイトル文字列と当サイトの商品名だけに絞り、agy にツール実行を
  一切要求しないテキスト分類に限定する。
- **1ブランドの商品一覧をそのまま全部プロンプトに詰めない** (2026-09-10 実測:
  タカラトミー179件/くもん出版67件のような大きいブランドで実験したところ、
  40件程度までは10〜15秒で正しく応答するが、60件を超えたあたりから agy が
  勝手に `command` ツール(一覧の集計をコードで済まそうとする挙動と見られる)を
  呼ぼうとして headless では許可できず失敗したり、応答なしのまま固まったりする。
  ヘッドレスの権限ゲートを緩める (`--dangerously-skip-permissions`) のは
  無人実行では危険すぎるので取らない。代わりに `select_relevant_articles` で
  文字bigram一致度の高い上位 `ARTICLE_LIST_LIMIT` 件だけに絞ってから渡す
  (最終判定は agy に委ねるが、入力サイズだけは機械的に抑える)。
- brand_recalls への書き込みは行わない。人間が Issue で確認し、既存の
  backfill 手順 (open_toy_recall_issue.py の説明) で手動 PR する運用は変えない。
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import pathlib
import re
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from brand_normalizer import normalize as normalize_brand  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("verify_toy_recall_matches")

DEFAULT_INPUT = "data/analytics/toy_recall_candidates.json"
DEFAULT_ARTICLES_GLOB = "data/articles/*.json"
# mine_experience.py / draft_sns_reply.py と同じ既定タイムアウト。
ANTIGRAVITY_TIMEOUT_S = 120
DEFAULT_MODEL = "gemini-3.8-flash-high"
MAX_EXTRA_RETRIES = 2
RETRY_SLEEP_SECONDS = 5
# 40件は10〜15秒で安定して応答するが60件超は失敗し始めた実測 (2026-09-10) を受け、
# 十分な安全マージンを取った値。
ARTICLE_LIST_LIMIT = 25

ARTICLE_EXCLUDE_SUFFIXES = (".enrichment.json", ".quality.json", ".seo.json")
BRACKET_RE = re.compile(r"[「『](.+?)[」』]")
BIGRAM_CLEAN_RE = re.compile(r"[^ぁ-んァ-ヶー一-龯A-Za-z0-9]")

MATCH_RE = re.compile(r"^一致[:：]\s*(あり|なし)", re.MULTILINE)
ASIN_RE = re.compile(r"^ASIN[:：]\s*(.+)$", re.MULTILINE)
REASON_RE = re.compile(r"^理由[:：]\s*(.+)$", re.MULTILINE)
ASIN_TOKEN_RE = re.compile(r"^[A-Z0-9]{10}$")


def build_article_index(articles_glob: str = DEFAULT_ARTICLES_GLOB) -> dict[str, list[dict]]:
    """canonical ブランド名 -> [{asin, name, name_full}] のインデックスを作る。

    brand_normalizer.py の --dump-articles と同じ走査・除外パターンを流用。
    """
    index: dict[str, list[dict]] = {}
    files = sorted(glob.glob(articles_glob))
    files = [f for f in files if not f.endswith(ARTICLE_EXCLUDE_SUFFIXES)]
    for f in files:
        try:
            d = json.loads(pathlib.Path(f).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("skip unreadable article %s: %s", f, exc)
            continue
        product = d.get("product") or {}
        asin = product.get("asin")
        if not asin:
            continue
        raw_brand = product.get("brand") or ""
        canonical = normalize_brand(raw_brand).canonical
        index.setdefault(canonical, []).append({
            "asin": asin,
            "name": product.get("name") or "",
            "name_full": product.get("name_full") or "",
        })
    return index


def _recall_product_name(title: str) -> str:
    """公示タイトルから会社名/対応区分を除いた商品名相当部分を取り出す。

    例: `くもん出版「玩具：くるくるチャイム」 - 交換／返金` -> `玩具：くるくるチャイム`
    括弧が無い(古い公示等)場合はタイトル全体を使う。
    """
    m = BRACKET_RE.search(title or "")
    return m.group(1) if m else (title or "")


def _bigrams(text: str) -> set[str]:
    cleaned = BIGRAM_CLEAN_RE.sub("", text or "")
    if len(cleaned) < 2:
        return {cleaned} if cleaned else set()
    return {cleaned[i : i + 2] for i in range(len(cleaned) - 1)}


def select_relevant_articles(
    candidate: dict, articles: list[dict], limit: int = ARTICLE_LIST_LIMIT,
) -> list[dict]:
    """agy に渡す記事数を上限 `limit` に絞る (件数超過による agy 側の失敗回避)。

    最終判定は agy に委ねるので、ここでは絞り込みが粗くてよい。文字bigramの
    重なり率で当該ブランド記事をソートし上位だけ残す。件数が既に上限以下なら
    そのまま返す (小ブランドは無条件で全件渡す)。
    """
    if len(articles) <= limit:
        return articles
    target = _bigrams(_recall_product_name(candidate.get("title", "")))
    if not target:
        return articles[:limit]

    def score(article: dict) -> float:
        text = article.get("name_full") or article.get("name") or ""
        bg = _bigrams(text)
        return len(target & bg) / len(target) if bg else 0.0

    return sorted(articles, key=score, reverse=True)[:limit]


def build_prompt(candidate: dict, articles: list[dict]) -> str:
    lines = [f"- {a['asin']}: {a['name_full'] or a['name']}" for a in articles]
    article_block = "\n".join(lines)
    return f"""あなたは消費者庁のリコール公示と、あるECサイトが実際に掲載している商品を照合するアシスタントです。

以下は消費者庁が公示したリコール情報です:
- ブランド: {candidate.get("brand", "")}
- 公示日: {candidate.get("post_date", "")}
- カテゴリ: {candidate.get("category", "")}
- 商品名(公示の表記): {candidate.get("title", "")}
- 詳細ページ (参考、開けなくてよい): {candidate.get("url", "")}

このブランドで当サイトが掲載している商品一覧です (ASIN: 商品名):
{article_block}

上記のリコール対象商品と**同一商品(型番違い・色違いを含む同一シリーズ)**とみなせる
ASIN が一覧の中にあれば、それだけを挙げてください。ブランド名やキャラクター名
(例: アンパンマン) が共通しているだけの別商品は対象に含めないでください。
公示の商品名(表記)が「玩具」「パズル」のように一般的すぎて、具体的な型番や
固有のシリーズ名が書かれておらず、一覧の中の複数商品が同じくらい当てはまり
得る場合は、**特定できたことにせず**必ず「一致なし」としてください。
少しでも自信が持てない場合も「一致なし」としてください
(誤って「一致」と報告する方が実害が大きいです)。

出力は次のどちらかの形式のみとし、余計な説明を追加しないでください:

一致: あり
ASIN: <一致するASINをカンマ区切りで>
理由: <1〜2文>

一致: なし
理由: <1文>
"""


def parse_verify_response(text: str, valid_asins: set[str]) -> tuple[list[str], str]:
    """応答テキストを (一致ASINのリスト, 理由) に分解する。

    形式が崩れている・ASIN欄が無い・一致ASINが候補一覧に無い (幻覚)場合は
    すべて「一致なし」に倒す (fail-closed)。
    """
    m = MATCH_RE.search(text or "")
    if not m or m.group(1) != "あり":
        reason_m = REASON_RE.search(text or "")
        return [], (reason_m.group(1).strip() if reason_m else "")

    asin_m = ASIN_RE.search(text)
    if not asin_m:
        return [], "応答形式が不正 (ASIN欄が無い) — 一致なしとして扱う"

    raw_asins = re.split(r"[,、\s]+", asin_m.group(1).strip())
    asins = []
    for a in raw_asins:
        a = a.strip().upper()
        if not a:
            continue
        if not ASIN_TOKEN_RE.match(a):
            continue
        if a not in valid_asins:
            logger.warning("agy が候補一覧に無い ASIN %s を返した — 破棄", a)
            continue
        if a not in asins:
            asins.append(a)

    reason_m = REASON_RE.search(text)
    reason = reason_m.group(1).strip() if reason_m else ""
    return asins, reason


def build_agy_argv(prompt: str, model: str) -> list[str]:
    """agy の argv を組む (mine_experience.build_antigravity_argv と同じ形)。

    `--print` は次のトークンを食うため、--model を先に置き prompt は
    --print= に添付する (omochairo/amazon#6539 の実測)。
    """
    return ["agy", "--model", model, f"--print={prompt}"]


def call_agy(
    prompt: str, *, model: str = DEFAULT_MODEL, timeout_s: int = ANTIGRAVITY_TIMEOUT_S,
    sleeper=time.sleep,
) -> str:
    """agy をヘッドレス実行して応答テキストを返す。空応答・timeout はリトライする。

    mine_experience.gather_antigravity は空応答だけをリトライし timeout は
    1回で諦める (大量ASINのスループット優先、timeout retryはcostlyという判断)。
    本スクリプトは目的が逆で、**見逃し (真陽性を拾えない) の方がリトライの
    コストより実害が大きい** (#4320 の検知漏れそのものを塞ぐための仕組みなので)。
    そのため timeout も空応答と同じ扱いでリトライする
    (2026-09-10 実測: 同じ25件プロンプトが13秒で終わることもあれば
    120秒timeoutすることもあり、単純な入力サイズだけでは説明できない揺らぎがある)。
    """
    argv = build_agy_argv(prompt, model)
    cmds = [["dbus-run-session", "--", *argv], argv]

    attempts = MAX_EXTRA_RETRIES + 1
    for attempt in range(1, attempts + 1):
        result = None
        try:
            for cmd in cmds:
                try:
                    result = subprocess.run(
                        cmd, capture_output=True, text=True, timeout=timeout_s, encoding="utf-8",
                    )
                    break
                except FileNotFoundError:
                    continue
        except subprocess.TimeoutExpired:
            if attempt < attempts:
                logger.warning(
                    "agy 呼び出しが timeout (%ds, attempt %d/%d) — リトライ",
                    timeout_s, attempt, attempts,
                )
                sleeper(RETRY_SLEEP_SECONDS)
                continue
            logger.warning("agy が %d 回とも timeout (%ds) — skip", attempts, timeout_s)
            return ""
        if result is None:
            logger.warning("agy (Antigravity CLI) が見つかりません — skip")
            return ""
        if result.returncode != 0:
            logger.warning(
                "agy が非ゼロ終了 (code %s): %s — skip",
                result.returncode, (result.stderr or "")[:200],
            )
            return ""
        text = (result.stdout or "").strip()
        if text:
            return text
        if attempt < attempts:
            logger.warning("agy から空応答 (attempt %d/%d) — リトライ", attempt, attempts)
            sleeper(RETRY_SLEEP_SECONDS)

    logger.warning("agy が %d 回とも空応答 — skip", attempts)
    return ""


def verify_candidates(
    candidates: list[dict], article_index: dict[str, list[dict]], *,
    model: str = DEFAULT_MODEL, timeout_s: int = ANTIGRAVITY_TIMEOUT_S,
    caller=call_agy,
) -> list[dict]:
    """candidates を破壊せず、matched_asins/match_reason を足したコピーを返す。"""
    out = []
    for c in candidates:
        c = dict(c)
        articles = article_index.get(c.get("brand", ""), [])
        if not articles:
            c["matched_asins"] = []
            c["match_reason"] = ""
            out.append(c)
            continue
        articles = select_relevant_articles(c, articles)
        valid_asins = {a["asin"] for a in articles}
        prompt = build_prompt(c, articles)
        text = caller(prompt, model=model, timeout_s=timeout_s)
        asins, reason = parse_verify_response(text, valid_asins)
        c["matched_asins"] = asins
        c["match_reason"] = reason
        if asins:
            logger.info("MATCH: %s (%s) -> %s", c.get("title", ""), c.get("brand", ""), asins)
        out.append(c)
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", default=DEFAULT_INPUT)
    p.add_argument("--out", default=None, help="既定は --input を上書き")
    p.add_argument("--articles-glob", default=DEFAULT_ARTICLES_GLOB)
    p.add_argument("--model", default=os.environ.get("ANTIGRAVITY_MODEL", DEFAULT_MODEL))
    p.add_argument("--timeout", type=int, default=ANTIGRAVITY_TIMEOUT_S)
    p.add_argument("--brand", default=None, help="このブランドの候補だけ処理する (検証用)")
    p.add_argument("--limit", type=int, default=None, help="先頭N件だけ処理する (検証用)")
    args = p.parse_args()

    in_path = pathlib.Path(args.input)
    if not in_path.exists():
        logger.error("input not found: %s", in_path)
        return 2
    data = json.loads(in_path.read_text(encoding="utf-8"))
    candidates = data.get("candidates") or []
    if args.brand:
        candidates = [c for c in candidates if c.get("brand") == args.brand]
    if args.limit is not None:
        candidates = candidates[: args.limit]
    if not candidates:
        logger.info("no candidates to verify — exiting")
        return 0

    article_index = build_article_index(args.articles_glob)
    logger.info("article index: %d brands", len(article_index))

    verified = verify_candidates(candidates, article_index, model=args.model, timeout_s=args.timeout)
    matched = [c for c in verified if c.get("matched_asins")]
    logger.info("%d/%d candidates matched an ASIN", len(matched), len(verified))

    data["candidates"] = verified
    out_path = pathlib.Path(args.out) if args.out else in_path
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("wrote %s", out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
