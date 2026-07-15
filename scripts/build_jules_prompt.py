#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""repoless Jules セッション用プロンプトの組み立て (GitLab 移行 フェーズ2)。

旧 03-invoke-jules.yml は Jules が GitHub リポジトリを clone して data/raw/* を
直接読む前提だった。repoless (リポジトリ非接続) では読めないため、対象 ASIN の
データスライスをプロンプトに同梱する方式に置換する。

ベースは PoC v3 プロンプト (quality_gate 99/100 で合格した構成)。
設計: docs/gitlab-migration-design.md §4.3

同梱スライス:
- data/raw/amazon.json の対象 ASIN エントリ (無ければ per_asin/<ASIN>/amazon.json
  の item — 楽天ランキング由来 ASIN, #810 Phase 1.5)
- rakuten_matched / yahoo_matched の matched_asin 一致エントリ
- data/raw/per_asin/<ASIN>/*.json (*.raw.json は除く)
- AGENTS.md 全文 (リポジトリ操作系の §1/2/5 は INTRO で置き換えを宣言)
- jules/PROMPT_TEMPLATE.md 全文
- GSC 実流入クエリ注記 (#2953 B案 / #1329 記事版, build_asin_query_context.py 経由。
  03-invoke-jules.yml とのパリティ維持。取得失敗/データ無しは best-effort で空文字)
- 監査不足観点注記 (#2995 消費側② Phase 3 / #3203 Phase 1-C, data/analytics/
  answerability_audit.json 経由。取得失敗/該当 ASIN 無しは best-effort で空文字)
- 体験談供給レーン注記 (#3203 Phase 2, per_asin/<ASIN>/experience.json 経由。
  snippets 0 件/ファイル無しは best-effort で空文字)

注意: グローバル data/raw/youtube.json は ASIN 紐付けが無い (title/url のみ) ため
同梱しない。per_asin/<ASIN>/youtube.json 側を使う。

使い方:
    python scripts/build_jules_prompt.py --asin B0XXXXXXXX [--out prompt.txt]
"""
import argparse
import glob
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

JST = timezone(timedelta(hours=9))


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _jload(path):
    return json.loads(_read(path))


def _jdump(obj):
    return json.dumps(obj, ensure_ascii=False, indent=1)


def _info_note(asin):
    """#1600 Phase 1: 第三者情報量に応じた水増し禁止の動的注記 (03 の INFO_NOTE 移植)。"""
    try:
        import score_per_asin_info as sc
        info = sc.score_asin(asin)
    except Exception as e:  # スコア計算失敗は生成を止めない (03 と同じ best-effort)
        print(f"warning: info scoring failed, using sparse note: {e}", file=sys.stderr)
        info = {}
    band = info.get("band", "unknown")
    news = info.get("news_sources", 0)
    yt = info.get("youtube", 0)
    bk = info.get("books", 0)
    if band == "ok":
        return (f"【事前収集ソースの量】: 十分 (媒体{news} / 動画{yt} / 書籍{bk})。"
                "同梱の per_asin データ (news/youtube/books/competitors) の一次ソースを優先して根拠を固めてください。")
    return f"""【事前収集ソースの量】: 乏しい (媒体{news} / 動画{yt} / 書籍{bk})。この商品は第三者情報が少ない品です。
- 確認できない仕様・効果・受賞歴・口コミを創作して字数を埋めること (水増し) を**禁止**します。裏取りできない記述は書かないでください。
- 出典の弱い一般論で分量を稼ぐより、確認できた事実だけで簡潔にまとめる方を優先してください (本文 minLength は安全網であって目標値ではありません)。
- sources には実在し検証可能な URL のみ記載。検索エンジンの結果ページ URL は出典にしないでください。"""


def _gsc_note(asin):
    """#2953 B案 (#1329 記事版): GSC 実流入クエリ注記 (03 の GSC_NOTE 移植・パリティ維持)。
    取得失敗/データ無しは best-effort で空文字を返し、生成は止めない。"""
    try:
        import build_asin_query_context as bq
        ctx = bq.build_context(asin)
    except Exception as e:  # スコア計算失敗は生成を止めない (03 と同じ best-effort)
        print(f"warning: gsc query context failed, skipping note: {e}", file=sys.stderr)
        return ""
    if ctx.get("fallback", True):
        return ""
    lines = "\n".join(
        f"- 「{q['query']}」 (表示 {q['impressions']} 回 / 平均掲載順位 {q['position']})"
        for q in ctx.get("queries", [])
    )
    return f"""【このページに実際に流入している検索クエリ (GSC 直近4週)】
{lines}
- これは読者が実際に検索窓に打った語です。title / meta_description の検索意図選定 (テンプレート §4 の意図差別化) では、このクエリ群を最優先のシグナルとして使ってください
- faq のうち最低 1 問は、上記クエリが示す疑問への直接回答にしてください (クエリの語をそのまま質問文に活かす)
- クエリが示す疑問 (例: 「○○ 対象年齢」「○○ 違い」) に本文が答えていない場合、narrative の該当セクションで必ず回答してください"""


def _audit_note(asin):
    """#2995 消費側② Phase 3: answerability_audit の不足観点をリライトに注入する
    (03 系と同じ best-effort 規律: 失敗/データ無しは stderr warning + 空文字)。"""
    try:
        audit = _jload("data/analytics/answerability_audit.json")
    except Exception as e:
        print(f"warning: answerability audit read failed, skipping note: {e}", file=sys.stderr)
        return ""
    page = None
    for p in audit.get("pages", []):
        if isinstance(p, dict) and p.get("asin") == asin:
            page = p
            break
    if not page:
        return ""
    lines = []
    for q in page.get("queries", []):
        aspects = q.get("missing_aspects") or []
        if not aspects:
            continue
        lines.append(f"- クエリ「{q.get('query', '')}」: {'、'.join(aspects)}")
    if not lines:
        return ""
    joined = "\n".join(lines)
    return f"""【前回記事に不足していた観点 (読者の実検索クエリ監査・必読)】
この商品の既存記事は、実際に検索流入しているクエリに対し以下の観点が不足していると監査されました:
{joined}
- 同梱データ (competitors / news / youtube / books / third_party_sources) で裏取りできる範囲で、これらの観点を本文・FAQ で必ず埋めてください
- 裏取りできない観点は創作せず省いて構いません (創作禁止が優先)"""


def _experience_note(asin):
    """#3203 Phase 2: 体験談供給レーン (experience.json) の使い方注記。
    同梱 per_asin/<ASIN>/experience.json が存在し snippets が 1 件以上のときのみ
    ブロックを返す (03 系と同じ best-effort 規律: 読めない/0件は空文字)。"""
    path = f"data/raw/per_asin/{asin}/experience.json"
    if not os.path.exists(path):
        return ""
    try:
        data = _jload(path)
    except Exception as e:
        print(f"warning: experience.json read failed, skipping note: {e}", file=sys.stderr)
        return ""
    snippets = data.get("snippets") if isinstance(data, dict) else None
    if not isinstance(snippets, list) or not snippets:
        return ""
    return """【体験談素材 (experience.json) の使い方】
- 同梱の per_asin/experience.json はシステムが Web/SNS/レビューから事前収集・検証した体験談素材です。narrative の根拠にはまずこれを使ってください (テンプレート §6.5.4)。
- usable_as が "quote" の snippet は出典付き短引用可、"paraphrase" は集合表現への言い換えのみ可 (原文再現禁止)。"""


def _amazon_item(asin):
    raw = _jload("data/raw/amazon.json")
    for item in raw.get("items", []):
        if isinstance(item, dict) and item.get("asin") == asin:
            return item
    # 楽天ランキング由来の新規 ASIN (#810 Phase 1.5)
    per = f"data/raw/per_asin/{asin}/amazon.json"
    if os.path.exists(per):
        d = _jload(per)
        item = d.get("item") or d
        if isinstance(item, dict):
            return item
    raise SystemExit(f"ASIN {asin}: amazon.json にも per_asin にも商品データが無い")


def _matched(path, asin):
    try:
        return [i for i in _jload(path).get("items", [])
                if i.get("matched_asin") == asin]
    except FileNotFoundError:
        return []


def build_prompt(asin, today=None):
    today = today or datetime.now(JST).strftime("%Y-%m-%d")
    today_iso = f"{today}T10:00:00+09:00"

    amazon_item = _amazon_item(asin)
    rakuten = _matched("data/raw/rakuten_matched.json", asin)
    yahoo = _matched("data/raw/yahoo_matched.json", asin)
    per_asin = {}
    for p in sorted(glob.glob(f"data/raw/per_asin/{asin}/*.json")):
        base = os.path.basename(p)
        if base.endswith(".raw.json"):
            continue
        try:
            per_asin[base] = _jload(p)
        except Exception as e:
            print(f"warning: skip unreadable {p}: {e}", file=sys.stderr)

    info_note = _info_note(asin)
    gsc_note = _gsc_note(asin)
    audit_note = _audit_note(asin)
    experience_note = _experience_note(asin)

    prompt = f"""あなたは知育玩具メディア「おもちゃいろ」の記事生成エージェントです。
このセッションはリポジトリ非接続 (repoless) です。必要な入力データは本プロンプト末尾に全て同梱しています。

【最重要・成果物ルール (リポジトリ規定の置き換え)】
- 後述の「業務規定 (AGENTS.md)」の §1 リポジトリ保護ルール・§2 入力データ・§5 提出フローは、リポジトリ非接続のため以下で置き換えます:
  - 成果物はワークスペース直下に data/articles/{today}-{asin}.json の 1 ファイルのみ作成する
  - git 操作・PR 作成・ブランチ作成・検証スクリプト実行は不要 (システム側で実施する)
  - 入力データはファイルシステムではなく本プロンプト同梱の「入力データ」セクションを使う
- 一時ファイルを作った場合は完了前に必ず削除し、最終的なファイル追加が上記 1 ファイルだけになるようにすること

【今回生成する記事の対象 ASIN】: {asin}
このセッションで生成する記事は必ずこの ASIN を対象としてください。

{info_note}

{gsc_note}

{audit_note}

{experience_note}

【本日の日付 (必ず使用)】: {today}
- 出力ファイル名: data/articles/{today}-{asin}.json
- slug フィールド: "{today}-{asin}"
- date フィールド: "{today_iso}"
テンプレートのスキーマ例にある日付は例示なので絶対に流用しないでください。未来日付・過去日付の生成は禁止です。

【価格データの扱い】
- 楽天/Yahoo の価格・URL は同梱の rakuten_matched / yahoo_matched (対象 ASIN 抽出済み) を使い、product.prices.rakuten / product.prices.yahoo に {{ price, url }} を埋める。空配列ならば price=0 / url="" / is_search=true とする。

【比較・選び分け (narrative.how_to_choose) の素材】
- 同梱の per_asin/competitors.json は API 由来の検証済み競合データです。narrative.how_to_choose ではこのデータ内の商品に限り名前・価格・特徴に言及して比較して構いません (テンプレート §5.F)。
- competitors.json に無い商品名・ASIN・価格を比較に持ち出すことは禁止します (品質ゲートで機械検査されます)。

【sources のルール (リポジトリ非接続環境向けの明確化・必読)】
- あなたの環境には google_search と view_text_website ツールがあります。まず対象商品について**必ず検索・URL閲覧で裏取りを試みてください**。
- ただしこの環境では両ツールが失敗することがあります (検索結果なし・サイト取得失敗)。**失敗しても諦めて販売ページで埋めないでください。**
- ツールで裏取りできなかった場合のフォールバック: 同梱の per_asin データ (news.json / books.json / youtube.json / competitors.json) に含まれる URL は、システム側が実在する API (ニュース検索・Google Books・YouTube Data API) から事前収集した検証済み URL です。**これらを sources に採用して構いません** (タイトル・出典名も同梱データのものを使う)。
- Amazon の販売ページ URL は sources に 1 件だけ含めてよい (慣例)。楽天・Yahoo の販売ページは sources に入れない。
- **sources は最低 5 件必須** (品質ゲートで機械検査されます)。

【品質ゲートで機械検査される項目】
product.name の通称ルール (最大 40 字) と機械検査 11 項目は、下記テンプレート §4 / §4.5 に明文化されています (不合格になると公開されません・全項目厳守)。

=== 業務規定 (AGENTS.md 全文) ===
{_read("AGENTS.md")}

=== 記事生成プロンプト (jules/PROMPT_TEMPLATE.md 全文) ===
{_read("jules/PROMPT_TEMPLATE.md")}

=== 入力データ ===
--- amazon.json (対象 ASIN エントリ) ---
{_jdump(amazon_item)}
--- rakuten_matched (対象 ASIN 抽出) ---
{_jdump(rakuten)}
--- yahoo_matched (対象 ASIN 抽出) ---
{_jdump(yahoo)}
"""
    for name, data in per_asin.items():
        prompt += f"--- per_asin/{name} ---\n{_jdump(data)}\n"
    return prompt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asin", required=True)
    ap.add_argument("--out", help="出力先ファイル (省略時 stdout)")
    args = ap.parse_args()
    prompt = build_prompt(args.asin)
    size = len(prompt.encode("utf-8"))
    print(f"prompt size: {size} bytes", file=sys.stderr)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(prompt)
    else:
        sys.stdout.write(prompt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
