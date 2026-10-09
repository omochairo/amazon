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

--print-note {audit,experience} モード (#3203 未配線修正・2026-07-16):
    03-invoke-jules.yml (GitHub 接続ありの主力ワークフロー・6時間毎 cron) から
    高頻度に呼ばれる軽量 CLI。_audit_note(asin) / _experience_note(asin) の
    戻り値だけを stdout に出力して即終了する (build_prompt() のフルパイプライン・
    AGENTS.md / jules/PROMPT_TEMPLATE.md 読み込みは実行しない)。
    例: python scripts/build_jules_prompt.py --asin B0XXXXXXXX --print-note audit
"""
import argparse
import glob
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from filter_raw_per_asin import exclude_title_only  # noqa: E402

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
- usable_as が "quote" の snippet は出典付き短引用可、"paraphrase" は集合表現への言い換えのみ可 (原文再現禁止)。
- note が空でない snippet は、実使用対象が本記事の商品と異なるモデル・型番であることを示す注記です。本商品固有の体験として書かず、note の内容 (例: 旧モデルでの使用感) を明記するか、この snippet は使わないでください。"""


def _first_party_note(asin):
    """#9199 案(b): omcha.jp の実使用記事を出典に使ってよいことの注記。

    この ASIN を主役にした omcha.jp の記事 (first_party_sources.json の role=primary) が
    あるときだけ返す。待ちの判定 (score_per_asin_info.non_sales_material) はこれを
    非販売ソース 1 件として数えているので、ここで渡さないと待ちが解けても sources_v5 で落ちる。
    03-invoke-jules.yml は --print-note first_party で同じ文面を受け取る。
    """
    try:
        import score_per_asin_info as sc
        posts = sc.first_party_posts(asin)
    except Exception as e:
        print(f"warning: first-party posts read failed, skipping note: {e}", file=sys.stderr)
        return ""
    if not posts:
        return ""
    urls = "\n".join(f"- {u}" for u in posts[:3])
    return f"""【おもちゃいろ (omcha.jp) の実使用記事 (出典に使ってよい)】
{urls}
- サイト運営者がこの商品を実際に使って書いた omcha.jp (おもちゃいろ本家) の記事です。販売ページではない一次情報なので、内容を閲覧ツールで確認したうえで **sources に 1 件まで採用してよい** (title / notes は記事の中身から書く)。
- これは第三者ソースではありません。sources の非販売ソースのうち**少なくとも 1 件は外部のサイト**から確保してください (同梱 third_party_sources.json / news.json 等)。
- navi.omcha.jp (この比較サイト自身) の URL は出典にしないでください。自分の記事を自分の根拠にする循環になります。"""


def _unwrap_affiliate(url):
    """楽天 (hb.afl ?pc=) / Yahoo (valuecommerce ?vc_url=) のアフィリエイト URL から商品ページを取り出す。"""
    try:
        q = parse_qs(urlparse(url or "").query)
    except ValueError:
        return ""
    for key in ("pc", "vc_url"):
        if q.get(key):
            return q[key][0]
    return url or ""


def _sources_note(asin):
    """sources_v5 を満たす組み立て方の注記 (#9239 → navi-brain#92 案 C)。

    gate の条件は「本文 (claims / review_signals) の supporting_source_ids から参照された
    非販売の出典が、別々のサイトで 2 件以上」。合計件数の条件は無い。使ってよい URL を
    具体的に並べ、数え方 (サイト単位・SNS は 1・通販は数えない) を渡す。
    03-invoke-jules.yml は --print-note sources で同じ文面を受け取る。
    """
    import score_per_asin_info as sc
    mall = [f"- Amazon 商品ページ: https://www.amazon.co.jp/dp/{asin}/"]
    for label, path in (("楽天", "data/raw/rakuten_matched.json"),
                        ("Yahoo!ショッピング", "data/raw/yahoo_matched.json")):
        for row in _matched(path, asin)[:1]:
            url = _unwrap_affiliate(row.get("url"))
            # 照合の誤り: 出品ページの URL に別の ASIN が埋まっている (s-b0xxxxxxxx-...)。
            # 別商品のページを「対象商品の販売ページ」として渡さない
            if url and sc.other_asin_in_url(url, asin):
                continue
            if url:
                mall.append(f"- {label} 商品ページ (照合済み): {url}")
    third = []
    try:
        data = _jload(f"data/raw/per_asin/{asin}/third_party_sources.json")
    except (FileNotFoundError, ValueError):
        data = {}
    for src in (data.get("sources") or []) if isinstance(data, dict) else []:
        url = src.get("url") if isinstance(src, dict) else None
        if not url or sc.is_search_result_url(url):
            continue
        # 通販サイトは非販売に数えないので候補に出さない (古い JSON に残っている分)
        if sc.host_kind(src.get("host") or urlparse(url).netloc) != "third_party":
            continue
        title = (src.get("title") or "").strip()
        third.append(f"- {url}" + (f" ({title[:60]})" if title else ""))
    blocks = [
        "【sources の組み立て (品質ゲート sources_v5: 本文から参照された非販売の出典が、"
        "別々のサイトで 2 件以上)】",
        "1. 非販売 (第三者) の出典を、別々のサイトから 2 件以上確保する。まず下の候補を"
        "閲覧ツールで開き、対象商品について書かれているものを採用する。足りなければ検索で探す。",
    ]
    if third:
        blocks.append("   第三者の候補 (システムが事前収集。商品が違う・読めないものは採用しない):")
        blocks.extend("   " + t for t in third[:8])
    # navi-brain#92: 書き直しでは、前の記事が見つけた非販売の出典も材料になる。
    # 前の記事は「本文から参照していない」だけで非販売を 2 サイト以上持っていることが多い
    listed = {t.split(" ", 2)[1] for t in third}
    prior = [u for u in sc.prior_article_urls(asin) if u not in listed]
    if prior:
        blocks.append("   前の記事が出典にしていた非販売のページ (同じく開いて確認してから採用する):")
        blocks.extend(f"   - {u}" for u in prior[:8])
    blocks.extend([
        "2. 採用した出典は、それが裏付けた claims / review_signals の supporting_source_ids に"
        "必ず id を書く。**どこからも参照されない出典は数えられない**ので、参照しない出典は載せない。",
        "3. 数え方: サイトはドメイン単位 (ja.wikipedia.org と en.wikipedia.org、"
        "toy.example.co.jp と example.co.jp は 1 サイト)。SNS (YouTube・X・Instagram・TikTok 等) は"
        "合わせて 1 サイトまで。販売ページ・通販サイト (ヨドバシ・ビックカメラ等) は非販売に数えない。"
        "omcha.jp の記事は 1 サイトなので、もう 1 サイトは外部が要る。"
        "news.json の Google ニュースの URL (news.google.com) と Google Books は、媒体が違っても"
        "google.com の 1 サイトになる。ニュースを 2 件目に数えたいときは、記事を開いて配信元の URL を載せる。",
        "4. 価格・仕様の主張には販売ページを出典にしてよい (非販売の 2 サイトの代わりにはならない)。"
        "使ってよい販売ページ:",
    ])
    blocks.extend("   " + m for m in mall)
    blocks.append(
        "5. 保存する前に、参照された非販売の出典のサイト数を数える。2 未満なら 1 に戻って足す。"
        "件数を埋めるための出典・架空の URL・別の商品のページは引き続き禁止。")
    return "\n".join(blocks)


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
            data = _jload(p)
        except Exception as e:
            print(f"warning: skip unreadable {p}: {e}", file=sys.stderr)
            continue
        if base == "youtube.json":
            data = exclude_title_only(data)
        per_asin[base] = data

    info_note = _info_note(asin)
    gsc_note = _gsc_note(asin)
    audit_note = _audit_note(asin)
    experience_note = _experience_note(asin)
    first_party_note = _first_party_note(asin)
    try:
        sources_note = _sources_note(asin)
    except Exception as e:  # best-effort: 注記が作れなくてもプロンプトは出す
        print(f"warning: sources note failed: {e}", file=sys.stderr)
        sources_note = ""

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

{first_party_note}

{sources_note}

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
- ただしこの環境では両ツールが失敗することがあります (検索結果なし・サイト取得失敗)。**失敗しても、非販売の 2 サイトを販売ページで代用しないでください** (販売ページは価格・仕様の主張の根拠)。
- ツールで裏取りできなかった場合のフォールバック: 同梱の per_asin データ (news.json / books.json / youtube.json / competitors.json) に含まれる URL は、システム側が実在する API (ニュース検索・Google Books・YouTube Data API) から事前収集した検証済み URL です。**これらを sources に採用して構いません** (タイトル・出典名も同梱データのものを使う)。
- 同梱の per_asin/third_party_sources.json は、システムが事前収集した非販売の第三者候補 URL (レビュー・解説・メディア) です。**sources 候補として優先的に内容を確認し**、裏取りに使えた URL を採用してください。候補に過ぎないので、内容を読めなかった・商品が違う URL は採用しない (#9199。03-invoke-jules と同じ規則)。
- 販売ページ (Amazon / 楽天 / Yahoo の対象商品ページ) は価格・仕様の出典にしてよい。ただし非販売の 2 サイトの代わりにはならない (上の【sources の組み立て】)。
- **claims / review_signals の supporting_source_ids から参照された非販売の出典が、別々のサイトで 2 件以上必須** (品質ゲートで機械検査されます。合計件数の下限は無いので、参照しない出典で件数を埋めない)。

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
    # Windows (cp932 既定) で --print-note / --out 未指定の stdout 出力が壊れるのを防ぐ
    # (03-invoke-jules.yml が動く GitHub Actions ubuntu-latest は元々 UTF-8 だが、
    # ローカル Windows での手動デバッグ実行でも文字化けしないよう明示的に固定する)。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    ap = argparse.ArgumentParser()
    ap.add_argument("--asin", required=True)
    ap.add_argument("--out", help="出力先ファイル (省略時 stdout)")
    ap.add_argument(
        "--print-note",
        choices=["audit", "experience", "first_party", "sources"],
        help=(
            "指定時は _audit_note/_experience_note/_first_party_note/_sources_note の戻り値のみを stdout に出力して終了する"
            " (03-invoke-jules.yml から高頻度に呼ばれる軽量モード。--out は無視される)。"
        ),
    )
    args = ap.parse_args()

    if args.print_note:
        # best-effort: 03 系と同じく失敗時も空文字・exit 0 (呼び出し側の || echo '' と二重の安全網)
        try:
            if args.print_note == "audit":
                note = _audit_note(args.asin)
            elif args.print_note == "first_party":
                note = _first_party_note(args.asin)
            elif args.print_note == "sources":
                note = _sources_note(args.asin)
            else:
                note = _experience_note(args.asin)
        except Exception as e:
            print(f"warning: --print-note {args.print_note} failed: {e}", file=sys.stderr)
            note = ""
        sys.stdout.write(note)
        return 0

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
