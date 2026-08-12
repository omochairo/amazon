# CLAUDE.md

Claude Code / 人間の開発者がこのリポジトリで作業するときの入口。

**記事の中身（文体・スキーマ・SEO 方針）を触るなら [AGENTS.md](./AGENTS.md) と [instructions.md](./instructions.md) を読むこと。** そちらが記事生成 AI (Jules) の業務規定であり、文体や JSON スキーマの SSOT。本ファイルはリポジトリ側（scripts / workflows / テーマ）を触るときの手引き。

## このリポジトリの位置づけ

`navi.omcha.jp`（知育玩具比較サイト）のソース。市場データ収集 → Jules が記事生成 → Hugo でビルド → GitHub Pages 配信、までを GitHub Actions で自動化している。**public** なのは Actions の無料枠を使うため。

関連リポジトリ:

| リポジトリ | 可視性 | 役割 |
|---|---|---|
| `omochairo/amazon`（本リポジトリ） | public | navi のソース + 自動化 + 共通スクリプト |
| `omochairo/amazon-navi-brain` | private | Jules の生成プロンプト（`jules/` に overlay checkout される） |
| `omochairo/omcha-ops` | private | omcha.jp（WordPress 本家）の分析・改善 |
| `omochairo/amazon-home-ops` | private | 自宅 self-hosted runner レーン |

## TODO は Issues に置く

**TODO をソースコード内やローカルファイルに持たない。** 課題は全て GitHub Issues で一元管理する。

```bash
gh issue list -R omochairo/amazon --state open
```

## テスト

CI (`.github/workflows/44-unit-tests.yml`) は全 PR で無条件に走る。手元で同じものを回すには:

```bash
pip install -r requirements.txt && pip install pytest
python -m pytest scripts/tests -q          # 2,354 passed / 2 skipped（約2分）
node --test "tests/js/*.test.mjs"          # Service Worker 8 件（0.3秒）
```

`requirements.txt` は本番実行依存だけを管理していて pytest を含まない。テスト依存は個別に入れる（本番環境に不要な依存を持ち込まないため）。

CI の Python は 3.11。3.14 でも全件パスすることは確認済みだが、落ちたときはまずバージョン差を疑う。

`scripts/tests/test_build_jules_prompt.py` は private repo 側の `jules/PROMPT_TEMPLATE.md` を要求する。手元に `jules/` が無い状態でも他のテストは通る。

## 記事の品質ゲート

```bash
python scripts/quality_gate.py --src data/articles/ --schema data/schema/article.schema.json --no-cert-fetch
```

**手元で回すときは `--no-cert-fetch` を付ける。** 付けないと cert HTML content check が外部へ HTTP fetch しに行く。CI と同じ厳密さで見たいときは `--strict`（1件でも落ちれば非ゼロ終了）。

`--src` はディレクトリを丸ごと走査する。特定の記事だけ見たいときは一時ディレクトリにコピーしてから当てる（`04-validate-article-pr.yml` が `mktemp -d` でやっているのと同じ方法）。

PR 時は変更されたファイルにしか当たらない。`competitors.json` が日次更新されるため、**マージ時に合格した記事が後から不合格になりうる**（main を再評価する経路が無い。Issue #4826 項目3）。

## ローカルプレビュー

```bash
pip install -r requirements.txt
python scripts/build_post.py
cd hugo && hugo server -D
```

Hugo は **Extended v0.146.0**（`02-publish.yml` の指定に合わせる）。`http://localhost:1313/`。

## 触ってよい / いけない

`AGENTS.md` の「リポジトリ保護ルール」は Jules 向けの制約だが、そこに挙がっている `hugo/config.toml` / `hugo/themes/` / `data/schema/` / `data/raw/` は自動化パイプラインの前提になっているので、人間が変更する場合も影響範囲を確認してから触ること。

**`scripts/fetch_*.py` を手元で実行しない。** API キーが要るうえ、外部 API のレート制限に当たる。

## workflow を触るときの注意

- `continue-on-error: true` を commit-back ステップ全体に付けない。落ちてよいのは auto-merge の有効化だけ（Issue #4793）
- `paths` allowlist を新設しない。過去に「未登録 → required check 不発 → auto-merge 永久 pending」の inverse-trap を繰り返し踏んでおり、`#4384` で撤廃した経緯がある
- secret ガードで `::notice::skipping` して緑終了させると、secret 失効時にレーンが黙って死ぬ（Issue #4793）

## PR の作法

- ブランチを切って PR を出す。`main` に直接コミットしない
- `add-article-*` / `add-*-brand-narrative-*` のような大量のブランチは cron と Jules が作る正常な状態。掃除しようとしない（`46-prune-stale-branches.yml` が担当）
- ライセンスは All Rights Reserved。OSS ではない
