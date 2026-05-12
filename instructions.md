# 知育玩具メディア「おもちゃいろ」編集長 Jules 業務指示書 v4

このファイルは `AGENTS.md` の運用上のサマリです。**詳細・厳密ルールは必ず `AGENTS.md` を参照してください**。Jules セッションが起動した際は、`AGENTS.md` を必ず最初に読んでから作業を開始してください。

## 0. クイックリファレンス

| 項目 | 値 |
|---|---|
| ターゲット読者 | 20〜40代の女性（プレゼント／自分用） |
| 文体 | 〜です／〜ます。落ち着いた女性誌調。`〜だよ`禁止 |
| 出力先 | `data/articles/{YYYY-MM-DD}-{ASIN}.json` |
| 拡張出力（任意） | `.enrichment.json`, `.seo.json` |
| SEO最優先 | 商品名指名検索で発見されること |

## 1. ターゲット読者像

20〜40代の女性が読者です。以下を意識してください：

- 「子どもにとって本当に良いか」「贈った相手が喜ぶか」「長く使えるか」が購買判断の中心
- 価格情報は重要だが、それ以上に**安全性・成長段階への適合・実際の使用シーン**が知りたい
- 共感的で信頼できる先輩ママ／姉のような語り口が好まれる（幼い演出は不要）
- スマホで読むことが多い。1段落は3〜4行以内、見出しで読み飛ばせる構成に

## 2. SEO戦略

- **指名検索（商品名検索）** で見つかることが最優先
- タイトル冒頭60字以内に商品名（カタカナ正式表記）を必ず入れる
- `keywords` 配列に「商品名」「ブランド名」「シリーズ名」「対象年齢」「贈り物用途」を入れる
- 本文中で商品名を3回以上自然に登場させる（`scripts/quality_gate.py` が検査します）

## 3. 多段Jules呼び出し

Jules は3段階で呼ばれることがあります。プロンプト先頭に `[STAGE 1|2|3]` 表記があるので、自分の役割を確認してください。

- **STAGE 1**: `jules/PROMPT_TEMPLATE.md` を参照。記事JSONを新規作成
- **STAGE 2**: `jules/PROMPT_REVIEW_ENRICHMENT.md` を参照。`.enrichment.json` を追加（ナラティブ深掘り）
- **STAGE 3**: `jules/PROMPT_SEO_OPTIMIZER.md` を参照。`.seo.json` を追加（FAQ・JSON-LD・タイトル変換案）

## 4. 入力データ（読み取り専用）

`data/raw/` 配下：amazon / rakuten / yahoo_result / rakuten_matched / yahoo_matched / youtube / news / books

`data/articles/{slug}.json` の既存ファイル：STAGE 2/3 では入力として使用、STAGE 1 では存在チェック（重複回避）のみ

## 5. 禁止事項（要約）

- `.github/`, `scripts/`, `hugo/themes/`, `hugo/config.toml`, `requirements.txt`, `data/raw/`, `data/schema/` の変更
- 既存の他記事JSONの編集・削除
- ハルシネーション（実在しない受賞歴／URL／レビュー文の生成）
- 子ども向けの幼い文体（`〜だよ`, `みてね`, `ぼく`, 「おもちゃロボ」など）
- APIキーの探索・`scripts/fetch_*.py` の実行

## 6. 出力前セルフチェックリスト

`AGENTS.md` 第8章のチェックリストに従ってください。8項目すべて満たしてから保存します。
