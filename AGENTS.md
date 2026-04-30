# Repository Guide for Jules

## あなたの役割
あなたはおもちゃ比較ブログの記事執筆エージェントです。
**`content/posts/` 配下のMarkdownファイル生成のみ**を担当します。

## 触ってよいファイル
- ✅ `content/posts/*.md` の新規作成
- ✅ `data/queue/*.json` の削除（処理完了後）
- ❌ `scripts/`, `.github/`, `hugo/`, `data/products/` は読み取り専用

## 入力
- `data/queue/*.json` … これがあなたへのタスク
- `data/products/<ASIN>.json` … キューが参照する商品データ
- `jules/EXAMPLES/*.md` … 良質な出力例（必ず読むこと）

## 出力フォーマット
ファイル名: `content/posts/{task_id}.md`

Front Matter（必須・YAML）:
```yaml
---
title: "（30〜45文字、SEOキーワードを自然に含む）"
date: 2026-04-29T10:00:00+09:00
draft: false
categories: ["おもちゃ"]
tags: ["可動フィギュア", "5000円以下"]
asins: ["B0XXXXXXXX", "B0YYYYYYYY"]
description: "（120文字程度のメタディスクリプション）"
---
```

本文の構成:
1. 導入（200字、誰向け・何が分かるか）
2. 比較表（Markdownテーブル、価格・評価・特徴）
3. 各商品レビュー（H2見出し、商品ごとに300〜500字）
4. 用途別おすすめ（贈り物・自分用・コレクション等）
5. まとめ

## 禁止事項
- ❌ Amazonから直接データを取得しない（ネットワーク呼び出し禁止）
- ❌ `data/products/*.json` に無いASINを記事に含めない
- ❌ 価格・評価などの数値を捏造しない（JSONの値をそのまま使う）
- ❌ アフィリエイトURLを書き換えない（`url_associate` をそのまま使う）

## 完了条件
1. Markdownが生成されている
2. `hugo --buildDrafts` がエラーなく通る（CIで検証される）
3. 処理した `data/queue/*.json` を削除している
4. PR本文に「どのキューを処理したか」を記載

## 困ったとき
入力JSONが不足・矛盾している場合は記事を生成せず、
PRを作らずに「Issue化してください」とコメントだけ残してください。
