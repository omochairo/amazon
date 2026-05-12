# [STAGE 3] SEOメタ最適化・FAQ・JSON-LD プロンプト v4

## 0. このプロンプトの位置づけ

これは **第3段Jules呼び出し** です。第1段＋第2段の出力を読み、検索エンジンと読者の両方に響く最終メタ情報を `data/articles/{slug}.seo.json` に書き出します。

**作業開始前に必ず `AGENTS.md` を読んでください。**

## 1. あなたの役割

指名検索（商品名検索）で「おもちゃいろ」が見つかるよう、最終的なSEO武装を担当します。

- タイトル案を3パターン提案（A/Bテスト準備）
- meta description を最適化
- FAQ を「People Also Ask」想定で増補
- JSON-LD（schema.org）を組み立てる
- パンくず情報を整理

## 2. 入力

- `data/articles/{slug}.json` — 第1段の出力
- `data/articles/{slug}.enrichment.json` — 第2段の出力（あれば）

## 3. 出力ファイル

`data/articles/{slug}.seo.json`

```json
{
  "slug": "2026-05-12-B073W9V2WB",
  "optimized_at": "2026-05-12T12:00:00+09:00",
  "title_variants": [
    {
      "variant": "primary",
      "title": "ジスター 天才のはじまりは知育に効く？口コミ・最安値・遊び方を編集部が徹底比較",
      "char_count": 38,
      "rationale": "指名検索『ジスター 天才のはじまり』完全一致＋疑問形でCTR向上"
    },
    {
      "variant": "alt_a",
      "title": "ジスター 天才のはじまり徹底レビュー｜何歳から遊べる？口コミと最安値を解説",
      "char_count": 38,
      "rationale": "ロングテール『何歳から』を含めた構成"
    },
    {
      "variant": "alt_b",
      "title": "【2026年最新】ジスター 天才のはじまりの口コミ・評判は？Amazon楽天Yahoo価格比較",
      "char_count": 43,
      "rationale": "年度表記＋口コミ・評判の組み合わせ"
    }
  ],
  "meta_description_optimized": "ジスター 天才のはじまりの口コミ・最安値・対象年齢を3サイト横断で徹底比較。3歳からのプレゼント選びに迷う方へ、編集部が遊び方や安全性まで丁寧に解説します。",
  "h1_recommendation": "ジスター 天才のはじまり 完全ガイド｜口コミ・最安値・遊び方",
  "h2_recommendations": [
    "ジスター 天才のはじまりとは？特徴と対象年齢",
    "ジスター 天才のはじまりの最安値は？Amazon・楽天・Yahooを比較",
    "ジスター 天才のはじまりの口コミ・評判まとめ",
    "プレゼントに選ぶ前に知りたい安全性のポイント",
    "実際の遊び方と長く楽しむためのコツ",
    "よくある質問（FAQ）"
  ],
  "faq_extended": [
    {"question": "ジスター 天才のはじまりは何歳から遊べますか？", "answer": "対象年齢は3歳以上です。細かなピースを誤飲しないことが前提となりますので、3歳未満のお子さまにはおすすめしません。…（100〜200字）"},
    {"question": "ジスター 天才のはじまりはどこで買うのが一番安いですか？", "answer": "…"},
    {"question": "ジスター 天才のはじまりはプレゼントに向いていますか？", "answer": "…"},
    {"question": "ジスター 天才のはじまりは何個のピースが入っていますか？", "answer": "…"},
    {"question": "ジスター 天才のはじまりは収納に困りますか？", "answer": "…"},
    {"question": "ジスター 天才のはじまりは長く遊べますか？", "answer": "…"}
  ],
  "breadcrumbs": [
    {"name": "おもちゃいろ", "url": "/"},
    {"name": "知育玩具", "url": "/categories/知育玩具/"},
    {"name": "ブロック・パズル", "url": "/categories/ブロック/"},
    {"name": "ジスター 天才のはじまり", "url": "/posts/2026-05-12-B073W9V2WB/"}
  ],
  "jsonld": {
    "product": {
      "@context": "https://schema.org/",
      "@type": "Product",
      "name": "ジスター 天才のはじまり",
      "image": "https://...",
      "description": "ジスター 天才のはじまりの…",
      "brand": {"@type": "Brand", "name": "ジスター"},
      "aggregateRating": {
        "@type": "AggregateRating",
        "ratingValue": "4.5",
        "reviewCount": "547"
      },
      "offers": {
        "@type": "AggregateOffer",
        "lowPrice": "3200",
        "highPrice": "3500",
        "priceCurrency": "JPY",
        "offerCount": "3"
      }
    },
    "faq": {
      "@context": "https://schema.org/",
      "@type": "FAQPage",
      "mainEntity": []
    },
    "breadcrumb": {
      "@context": "https://schema.org/",
      "@type": "BreadcrumbList",
      "itemListElement": []
    }
  },
  "internal_link_suggestions": [
    {"anchor": "知育ブロックの選び方", "target_keyword": "知育ブロック おすすめ", "reason": "本記事の親トピックへ誘導"},
    {"anchor": "3歳の誕生日プレゼント特集", "target_keyword": "3歳 プレゼント", "reason": "ペルソナ親和性が高い回遊先"}
  ]
}
```

## 4. ルール

- `title_variants` は文字数を必ず申告（日本語の文字単位）。Google検索結果のSP表示は約30〜35文字で切れる点に注意
- `faq_extended` は **6項目以上**。「People Also Ask」型の指名検索クエリを意識
- `jsonld.faq.mainEntity` と `jsonld.breadcrumb.itemListElement` は build時にPythonが自動で埋めるので空配列でOK（ただしルート構造は出すこと）
- `aggregateRating` は raw データに件数があるときだけ埋める。なければそのフィールドを省略
- 文体は STAGE 1 / 2 と同じ女性誌調
- 第1段・第2段のJSONは**絶対に変更しない**

## 5. 出力前セルフチェック

1. [ ] `title_variants` が3案あり、いずれも指名検索クエリを含む
2. [ ] `meta_description_optimized` 冒頭40字以内に商品名がある
3. [ ] `h2_recommendations` が5項目以上で、半数以上が商品名を含む
4. [ ] `faq_extended` が6項目以上
5. [ ] `jsonld.product` が `aggregateRating` 除き必須フィールドを満たす
6. [ ] 既存の `.json` / `.enrichment.json` は変更していない
