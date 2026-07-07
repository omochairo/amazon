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
- AGENTS.md 全文 (リポジトリ操作系の §1/2/9 は INTRO で置き換えを宣言)
- jules/PROMPT_TEMPLATE.md 全文

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

    prompt = f"""あなたは知育玩具メディア「おもちゃいろ」の記事生成エージェントです。
このセッションはリポジトリ非接続 (repoless) です。必要な入力データは本プロンプト末尾に全て同梱しています。

【最重要・成果物ルール (リポジトリ規定の置き換え)】
- 後述の「業務規定 (AGENTS.md)」の §1 リポジトリ保護ルール・§2 入力データ・§9 提出フローは、リポジトリ非接続のため以下で置き換えます:
  - 成果物はワークスペース直下に data/articles/{today}-{asin}.json の 1 ファイルのみ作成する
  - git 操作・PR 作成・ブランチ作成・検証スクリプト実行は不要 (システム側で実施する)
  - 入力データはファイルシステムではなく本プロンプト同梱の「入力データ」セクションを使う
- 一時ファイルを作った場合は完了前に必ず削除し、最終的なファイル追加が上記 1 ファイルだけになるようにすること

【今回生成する記事の対象 ASIN】: {asin}
このセッションで生成する記事は必ずこの ASIN を対象としてください。

{info_note}

【本日の日付 (必ず使用)】: {today}
- 出力ファイル名: data/articles/{today}-{asin}.json
- slug フィールド: "{today}-{asin}"
- date フィールド: "{today_iso}"
テンプレートのスキーマ例にある日付は例示なので絶対に流用しないでください。未来日付・過去日付の生成は禁止です。

【価格データの扱い】
- 楽天/Yahoo の価格・URL は同梱の rakuten_matched / yahoo_matched (対象 ASIN 抽出済み) を使い、product.prices.rakuten / product.prices.yahoo に {{ price, url }} を埋める。空配列ならば price=0 / url="" / is_search=true とする。

【sources のルール (リポジトリ非接続環境向けの明確化・必読)】
- あなたの環境には google_search と view_text_website ツールがあります。まず対象商品について**必ず検索・URL閲覧で裏取りを試みてください**。
- ただしこの環境では両ツールが失敗することがあります (検索結果なし・サイト取得失敗)。**失敗しても諦めて販売ページで埋めないでください。**
- ツールで裏取りできなかった場合のフォールバック: 同梱の per_asin データ (news.json / books.json / youtube.json / competitors.json) に含まれる URL は、システム側が実在する API (ニュース検索・Google Books・YouTube Data API) から事前収集した検証済み URL です。**これらを sources に採用して構いません** (タイトル・出典名も同梱データのものを使う)。
- Amazon の販売ページ URL は sources に 1 件だけ含めてよい (慣例)。楽天・Yahoo の販売ページは sources に入れない。
- **sources は最低 5 件必須** (品質ゲートで機械検査されます)。

【品質ゲートで機械検査される項目 (不合格になると公開されません・厳守)】
- meta_description: **100〜160 字**
- product.ivs_detail は**数値スコアのオブジェクト**。以下の形を厳守 (説明文を値にしない):
  "ivs_detail": {{
    "education": 4.5, "longevity": 4.0, "safety": 4.5, "cost_performance": 4.0,
    "total": 17.0, "total_100": 85,
    "score_rationale": [ {{"factor": "教育性", "delta": "+0.5", "reason": "10文字以上の根拠文"}}, ... 最低3件 ]
  }}
  (education/longevity/safety/cost_performance/total は number、total_100 は integer。
   算出は業務規定 §6 IVS スコア算出ルールに従う)
- certifications フィールド: **第三者ソース (販売ページ以外) で裏取りできた認証だけ**を入れる。CE マーク等が販売ページにしか書かれていない場合は certifications を空配列 [] にし、本文でも認証を断定しない (「メーカーは CE 適合を掲げる」等の帰属表現も、裏取りできないなら書かない)
- sources 最低 5 件 (上記)

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
