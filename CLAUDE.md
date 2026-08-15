# CLAUDE.md

Claude Code / 人間の開発者がこのリポジトリで作業するときの入口。

**記事の中身（文体・スキーマ・SEO 方針）を触るなら [AGENTS.md](./AGENTS.md) と [instructions.md](./instructions.md) を読むこと。** そちらが記事生成 AI (Jules) の業務規定であり、文体や JSON スキーマの SSOT。本ファイルはリポジトリ側（scripts / workflows / テーマ）を触るときの手引き。

## このリポジトリの位置づけ

`navi.omcha.jp`（知育玩具比較サイト）のソース。市場データ収集 → Jules が記事生成 → Hugo でビルド → 配信、までを自動化している。**public** なのは Actions の無料枠を使うため。

**配信は GitLab Pages。GitHub Pages は使っていない**（リポジトリ設定でも無効化済み）。`main` への push を `40-mirror-to-gitlab.yml` が GitLab へ転送し、GitLab 側の `pages` ジョブ（`.gitlab-ci.yml`）がビルドして配信する。経緯と復帰手順は [docs/failover-to-gitlab.md](./docs/failover-to-gitlab.md)。

```
GitHub Actions（開発・自動化・データ収集・記事生成）
        │ main へ push
        ▼
40-mirror-to-gitlab.yml ──► GitLab main ──► .gitlab-ci.yml の pages ジョブ ──► navi.omcha.jp
```

この分離のせいで **GitHub 側の run が全部緑でもサイトが凍りうる**（GitHub の責務は `git push` の成功まで）。外形監視は #5042。

関連リポジトリ:

| リポジトリ | 可視性 | 役割 |
|---|---|---|
| `omochairo/amazon`（本リポジトリ） | public | navi のソース + 自動化 + 共通スクリプト |
| `omochairo/amazon-navi-brain` | private | Jules の生成プロンプト（`jules/` に overlay checkout される） |
| `omochairo/omcha-ops` | private | omcha.jp（WordPress 本家）の分析・改善 + ファイル受け渡し用 `exchange/` |

## 共同開発の会話は private 側でやる

このリポジトリは複数人がそれぞれの Claude Code から触る。**相談・判断・数値のやりとりは `omochairo/amazon-navi-brain` の Issues で行う。**

```bash
gh issue view 2 -R omochairo/amazon-navi-brain --comments   # レーンと作法の定義
```

本リポジトリは public で、Issue / PR コメント / コミットメッセージは全世界に公開され、第三者ミラーに永久保存される（後から消しても消えない）。ここに書かないもの:

- GSC / GA4 の実数値、収益、順位、狙っているキーワード、これから打つ施策
- 個人情報・生活情報
- 生ログ、API レスポンス、環境変数、スクリーンショット

**特に事故りやすいのは「貼り付け」。** 診断のためにログを貼るのは自然な動作なので、そもそも会話レーンを private 側に置くこと自体が防御になる。secret scanning は自分のキーは守るが、数値や第三者の個人情報は検知しない。

複数人が同時に触るときは、ブランチを `<担当者>/*` で prefix 分離する（`add-article-*` 等の自動ブランチと混ざらないように）。

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

**依存は 2 ファイルで役割が分かれている（#5043 項目3）。** 直接依存を書くのは `requirements.in` で、`requirements.txt` は `pip-compile` が生成した推移的依存まで全部ピン留め済みのファイル。**`requirements.txt` を手で編集しない。** 依存を足す・上げるときは `requirements.in` を編集して再生成する:

```bash
docker run --rm -v "$(pwd):/w" -w /w python:3.11-slim sh -c "pip install pip-tools && pip-compile --output-file=requirements.txt requirements.in"
```

**Linux の Python 3.11 で生成すること。** Windows で生成すると win32 限定の依存や環境マーカーが混ざり、`ubuntu-latest` の CI と食い違う。インストール側（CI・手元）の手順は変わらず `pip install -r requirements.txt` のまま。

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

Hugo は **Extended v0.146.0**（本番ビルドの SSOT は `.gitlab-ci.yml` の `HUGO_VERSION`。上げるときはそちらに合わせる）。`http://localhost:1313/`。

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
