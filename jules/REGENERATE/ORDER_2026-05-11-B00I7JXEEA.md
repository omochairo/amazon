# 再生成オーダー: 2026-05-11-B00I7JXEEA

> Phase 1 v4パイプライン (`PROMPT_TEMPLATE` → `PROMPT_REVIEW_ENRICHMENT` → `PROMPT_SEO_OPTIMIZER`) で v3 記事を再生成するための、ASIN単位の指示書です。

## 0. 対象

- **slug**: `2026-05-11-B00I7JXEEA`
- **ASIN**: `B00I7JXEEA`
- **商品名**: プラレール S-03 E5系新幹線はやぶさ
- **ブランド**: タカラトミー
- **現在のフォーマット**: v3（narrative・persona_fit・faq・keywords 欠落のため `build_post.py` でスキップされる）

## 1. 全体の流れ（必ずこの順に実行）

1. **STAGE 1** — `jules/PROMPT_TEMPLATE.md` の指示に従って `data/articles/2026-05-11-B00I7JXEEA.json` を **v4スキーマで上書き**
2. **STAGE 2** — `jules/PROMPT_REVIEW_ENRICHMENT.md` の指示に従って `data/articles/2026-05-11-B00I7JXEEA.enrichment.json` を新規作成
3. **STAGE 3** — `jules/PROMPT_SEO_OPTIMIZER.md` の指示に従って `data/articles/2026-05-11-B00I7JXEEA.seo.json` を新規作成

各ステージ完了後にスキーマ検証を実施：

```bash
python scripts/quality_gate.py --slug 2026-05-11-B00I7JXEEA
```

最終的に `python scripts/build_post.py --gate --min-score 60` が緑になることがゴール。

## 2. 入力（真実値の出所）

`data/raw/` に当ASINのレコードは**残っていません**。よって価格・URL・画像URL・特徴量は **既存の v3 JSON** を真実値として扱ってください。

- **既存v3 JSON**: `data/articles/2026-05-11-B00I7JXEEA.json`（読み取り後、STAGE 1で上書き）
- **参考プロダクトJSON**: `data/products/` に当ASINファイルは無し（無視）
- **市場データ**: `data/raw/amazon.json` `data/raw/rakuten.json` `data/raw/news.json` を参考にしてよい（ただし当ASINの直接データは無いため、カテゴリ傾向の把握用途のみ）

### v3 JSON から **そのまま引き継ぐ** フィールド（捏造禁止）

- `product.asin` / `product.name` / `product.name_full` / `product.brand` / `product.image`
- `product.features`
- `product.prices.amazon.price` / `product.prices.amazon.url`
- `product.prices.rakuten` / `product.prices.yahoo`（`is_search: true` を含む）
- `product.best_platform` / `product.best_price`
- `date`

### v3 JSON を **捨てて作り直す** フィールド

- `title` `meta_description` `tags`（v3は装飾過剰・「だよ」調混入）
- `product.intro`（「〜だよ」が含まれているため）
- `product.pros` / `product.cons`（v3は中身が薄い）
- `product.ivs_detail`（`score_rationale` 配列が無いため v4 スキーマ非適合）
- `editorial_comment`（v3は「おもちゃロボ」演出のため禁止）

### 新規に追加するフィールド

- `keywords` / `persona_fit` / `narrative` / `faq`
- `product.ivs_detail.score_rationale` / `product.ivs_detail.pattern`

## 3. このASIN固有の注意

- 商品名は **「プラレール S-03 E5系新幹線はやぶさ」**。タイトル・meta・narrative 各セクションで自然に2回以上登場させること
- ブランドは「タカラトミー」。`tags` 先頭3つは `["プラレール", "タカラトミー", "E5系新幹線はやぶさ"]` のように固有名詞優先
- 価格帯 ￥1,836（〜3000円帯）→ `cost_performance` は **+0.3**
- 対象性別「男女共用」だが、20〜40代女性ペルソナ視点で **「鉄道好きのお子さま・甥姪へのプレゼント」** 文脈に寄せること
- 連結仕様 → `daily_use` セクションで「他の車両と連結して遊びが広がる」点を活かす
- `pattern` は **A（一般玩具）** を指定（乳児向けではない）
- `safety`：プラレールはSTマーク取得が一般的なので **+0.3** 程度。確実な記述が無ければ「STマーク取得の表記が確認できる範囲では」と慎重に表現

## 4. 出力先（厳守）

| Stage | 出力パス | 既存ファイル扱い |
|---|---|---|
| 1 | `data/articles/2026-05-11-B00I7JXEEA.json` | **上書き**（v3を捨てる） |
| 2 | `data/articles/2026-05-11-B00I7JXEEA.enrichment.json` | 新規 |
| 3 | `data/articles/2026-05-11-B00I7JXEEA.seo.json` | 新規 |

`hugo/content/posts/` には直接書き込まないこと（`scripts/build_post.py` が担当）。

## 5. 完了条件（Definition of Done）

- [ ] `python scripts/quality_gate.py --slug 2026-05-11-B00I7JXEEA` がスコア **60以上**
- [ ] `python scripts/build_post.py --gate --min-score 60` で当記事が draft でなく公開可で出力される
- [ ] `narrative.*` 全6セクションが規定字数を満たし、文体禁止表現（「だよ」「おもちゃロボ」等）ゼロ
- [ ] `faq` 3項目以上、`faq_extended`（STAGE 3）6項目以上
- [ ] `score_rationale` 3項目以上、各 `reason` 10字以上

## 6. レビュー観点（人間向けメモ）

- v3→v4 移行で**価格・URLが書き換わっていないか**（捏造チェック）
- 「20〜40代女性」ペルソナ向けの語り口が実現されているか
- 指名検索キーワード「プラレール E5系」「プラレール はやぶさ」が自然に含まれているか
