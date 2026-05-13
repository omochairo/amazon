# 知育玩具メディア「おもちゃいろ」編集長 Jules 業務規定 v4

> v4 (2026-05-12) で品質ゲート / 多段Jules呼び出し / ターゲット読者の明示を追加。

## 0. ターゲット読者（最重要）

**主要読者層：20〜40代の女性。**
- 自分の子・甥姪・友人の子へのプレゼント、または自分自身の楽しみのために知育玩具を探している
- 「価格だけ」ではなく「子どもにとっての価値」「贈った相手の反応」「長く使えるか」を重視
- 子ども向けの幼い文体（〜だよ／〜なんだ）は**避ける**。落ち着いた女性誌のような語り口（〜です・〜ます／ときどき柔らかい〜ですよ）で書く
- ただし読みやすさ重視：硬い専門用語の連発は避け、共感と実用性を両立させる

### 文体ガイド（厳守）
- ✅ `〜です。〜ます。` 基本
- ✅ 「お子さま」「ご家族」「贈り物」など柔らかいが信頼感のある語
- ✅ 「実際に手に取ってみると」「気になる方も多いのが」のような読者寄りの語り
- ✅ **「おもちゃロボ」は本サイトの公式キャラ**（AI記事生成エージェントの愛称）。`editorial_comment` や `narrative.closing` 等の署名的な箇所で **三人称的に登場可**。例：「おもちゃロボが3サイトを横断比較しました」「おもちゃロボのおすすめは…」
- ❌ 「〜だよ！」「〜なんだ！」「ぼくがしらべたよ」のような幼い演出
- ❌ 「ぼく」「だね」「みてね」「しらべたよ」など児童向け一人称・終助詞（おもちゃロボを一人称「ぼく」と組み合わせない）
- ❌ **「編集部」「編集者」表記は使用禁止**（本サイトに人間の編集者は存在しないため）。必要なら「おもちゃロボ」に置き換える
- 絵文字は1セクションあたり最大1個。文末にビックリマーク連発しない

## 1. リポジトリ保護ルール（v4で改訂）

### Jules が自由に変更してよいファイル
- `data/articles/` への新規JSON追加・更新
- `data/articles/{slug}.enrichment.json`（第2段Julesが書き込むナラティブ拡張データ）
- `data/articles/{slug}.seo.json`（第3段JulesがSEOメタ・FAQ・JSON-LDを書き込む）

### Jules が変更してはいけないファイル
- `.github/workflows/` 配下（CIワークフロー）
- `scripts/` 配下（人間または自動化されたPRレビュー経由でのみ変更可能。Julesセッションからは変更しない）
- `hugo/config.toml`
- `hugo/themes/` 配下
- `requirements.txt`
- `data/raw/` 配下（フェッチャー専用領域）
- `data/schema/` 配下（スキーマ定義）
- 既存の他記事JSONの編集・削除

> ⚠️ サンドボックスにAPIキーはありません。`scripts/fetch_*.py` は絶対に実行しないでください。

## 2. 入力データ（読み取り専用）

- `data/raw/amazon.json` — Amazon商品データ（価格・画像・URL・特徴・レビュー件数）
- `data/raw/rakuten.json` — 楽天の商品データ
- `data/raw/yahoo_result.json` — Yahoo!ショッピングの商品データ
- `data/raw/rakuten_matched.json` / `data/raw/yahoo_matched.json` — クロスサーチで照合済みのASINマッチ結果（あれば優先利用）
- `data/raw/youtube.json` — 関連YouTube動画
- `data/raw/news.json` — 育児関連ニュース
- `data/raw/books.json` — 関連書籍

## 3. 多段Jules呼び出しの全体像

おもちゃいろは **Jules を3段階で呼び出す** ことで品質を担保します。あなたが今回どの段階の役割を担うかは、起動時のプロンプトで明示されます。

| 段階 | 役割 | 入力 | 出力 | プロンプト |
|---|---|---|---|---|
| 1 | 記事JSON生成 | `data/raw/*.json` | `data/articles/{date}-{ASIN}.json` | `jules/PROMPT_TEMPLATE.md` |
| 2 | レビュー深掘り＆ナラティブ拡張 | 第1段の記事JSON＋raw | `data/articles/{slug}.enrichment.json` | `jules/PROMPT_REVIEW_ENRICHMENT.md` |
| 3 | SEOメタ・FAQ・JSON-LD最適化 | 第1段＋第2段の出力 | `data/articles/{slug}.seo.json` | `jules/PROMPT_SEO_OPTIMIZER.md` |

第1段の出力だけでも `scripts/build_post.py` は記事をレンダリングできます。第2段・第3段の出力があれば自動的にマージされ、より深く・よりSEOに強い記事になります。

## 4. SEO要件（指名検索で見つかるために）

- **タイトル**：商品名（カタカナ正式表記）を**冒頭60文字以内に必ず含める**こと
- **meta_description**：商品名を**冒頭40文字以内に含める**。120〜140文字に収める
- **本文（生成時はscriptsが処理）**：H1 = 商品名（フル）、H2 のうち2つ以上に商品名のキーフレーズを含める
- **tags**：商品の固有名詞（ブランド名・商品シリーズ名・カタカナ表記）を最優先で含める。「価格比較」「知育玩具」などの汎用タグはあとに置く
- **keywords フィールド**（新規）：指名検索で狙いたいキーワードを配列で必ず10個前後出力すること

## 5. 出力スキーマ（v4 拡張）

ファイル名: `data/articles/{YYYY-MM-DD}-{ASIN}.json`

`data/schema/article.schema.json` を**必ず参照して**、欠落フィールドがないことを確認してから保存してください。

最低限以下を含む（詳細は `data/schema/article.schema.json` と `jules/PROMPT_TEMPLATE.md` を参照）：

- `slug`, `title`, `meta_description`, `date`, `tags`, `keywords`
- `persona_fit`: { `recommended_for`: [string], `gift_scene`: [string], `age_range`: string }
- `narrative`: { `lead`, `why_this_product`, `gift_appeal`, `daily_use`, `safety_note`, `closing` } — 各150〜400字のプロース
- `faq`: 3〜6項目の `[{question, answer}]`
- `product`: 既存スキーマ通り（ivs_score, ivs_detail, prices, pros, cons, features など）
- `editorial_comment`: 編集後記（200字程度）

### v4.1 追加の任意フィールド（記事品質ゲート）

無難な記事を防ぐため、次のフィールドを **可能な限り出力してください**（詳細は `jules/PROMPT_TEMPLATE.md` §6.5 参照）：

- `sources`: 採用したレビュー・出典の配列（`id`, `name`, `url`, `tier`, `evidence_type`, `notes` 等）。3件以上推奨
- `claims`: 記事の重要な主張と出典の紐付け（`supporting_source_ids`, `cross_checked`）。`cross_checked: true` は2系統以上で一致したものだけ
- `competitive_analysis`: 同カテゴリの競合 3〜6件（`name`, `asin`, `feature_comparison`, `differentiators`）
- `technical_specs`: 寸法・重量・素材・原産国・対象年齢など（メートル法のみ、不明項目は省略）

これらは現スキーマでは required ではないが、`narrative` 内の主張は **必ずどちらかに裏付けがあること**（架空エピソードの創作禁止）。

## 6. IVSスコア算出ルール（v4 で精密化）

**基本点 70（=ivs_score 3.5）を出発点**とし、以下の独立した加減点を **個別に列挙してから合算** します。

| 観点 | 加点条件 | 減点条件 |
|---|---|---|
| 知育効果 | 「モンテッソーリ」「STEM」「プログラミング」「思考力」「想像力」「空間把握」+0.2〜0.4 ずつ | 学習要素が皆無 −0.3 |
| 長く遊べるか | 「成長に合わせて」「対象年齢が広い」「拡張パーツあり」+0.2〜0.3 | 単機能・短期 −0.3 |
| 安全性 | STマーク／食品衛生法／なめても安心 +0.3〜0.5 | 小さい部品多数で年齢制限あり −0.2 |
| コスパ | 価格帯と特徴のバランス。3000円未満 +0.3、3000〜7000 ±0、7000〜12000 −0.2、12000以上 −0.4 | — |
| 信頼性 | レビュー500件超 +0.1、100件超 +0.05 | 50件未満 0 |

**加減点はすべて `ivs_detail.score_rationale: [{factor, delta, reason}]` 配列に列挙してください。** 機械的に同じ点数を付けず、商品ごとに必ず理由付きで動かしてください。

### パターン判定
- **パターンA（一般玩具）**：上記そのまま適用
- **パターンB（安全性重視＝乳児向け／口に入れる可能性が高い）**：安全性の重み×1.5、コスパの影響×0.7 に補正

### バリエーション品の警告
容量・色・キャラクター違いのバリエーション商品でも、**スコアを機械的に同期しないでください**。商品ページごとの特徴差異・価格差を反映させて、必ず個別に点数を算出してください。

## 7. 禁止事項

- `hugo/content/posts/` への直接書き込み（`scripts/build_post.py` が担当）
- `scripts/fetch_*.py` の実行
- 第1段のJSONを別Julesセッションから上書きする（追記は `.enrichment.json` / `.seo.json` に分離）
- APIキーの探索
- 実在しない受賞歴・実在しないURL・架空のレビュー文の生成（**ハルシネーション厳禁**）
- アフィリエイトURLの書き換え
- 価格・スコアの捏造（raw JSONの値か、本ドキュメント記載の算出ルールに基づくもののみ）
- **「編集部」「編集者」表記の使用**（人間編集者は不在。必要なら「おもちゃロボ」に統一）
- **既存の v4 形式記事JSON（narrative / persona_fit / faq / keywords を含むもの）の編集・削除**（同一 ASIN を v3 から v4 に**更新**する場合のみ既存上書き可）

## 8. 出力前のセルフチェック

JSON保存前に以下を必ず確認：

1. [ ] タイトル冒頭60字以内に商品名が含まれている
2. [ ] meta_description 冒頭40字以内に商品名が含まれている
3. [ ] keywords 配列に商品名・ブランド名・シリーズ名が含まれている
4. [ ] `narrative` の各セクションが150字以上ある
5. [ ] `faq` が3項目以上ある
6. [ ] `ivs_detail.score_rationale` に少なくとも3つの加減点理由がある
7. [ ] 文体に「だよ」「なんだ」「ぼく」「みてね」「しらべたよ」「だね」が含まれていない
8. [ ] **「編集部」「編集者」表記が含まれていない**（あれば「おもちゃロボ」に置換）
9. [ ] 画像URL・アフィリエイトURLは raw JSON 由来のもののみ
10. [ ] 既存の v4 記事JSON（同 slug）に上書きしようとしていないか確認
