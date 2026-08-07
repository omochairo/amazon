# CLAUDE.md — omochairo/amazon (おもちゃいろ 比較ナビ)

このリポジトリは [navi.omcha.jp](https://navi.omcha.jp) を Hugo で構築する。外部APIからの
商品データ収集 → Jules (AIエージェント) による記事生成 → Hugo ビルド → GitHub Pages / GitLab
Pages への配信を GitHub Actions ベースの自動化パイプラインで運営している。概要は
[README.md](README.md)、記事生成の文体・編集ルールは Jules 向け [AGENTS.md](AGENTS.md)。

## 作業開始前に必ず

```bash
git fetch origin main && git log --oneline origin/main -10
```

`03-invoke-jules.yml` の自動連鎖 (main マージ→記事生成→PR→マージ) により main が
ユーザーの直接操作なしに頻繁に進む。詳細は
[docs/claude-traps-github-actions.md](docs/claude-traps-github-actions.md) 冒頭。

## エンジニアリング・トラップ集

このリポジトリで実際に踏んだ既知の罠。新規実装・レビュー前に該当するものを確認する
(症状から探すか、ファイル内 grep で当たりを付ける):

| ファイル | 対象領域 |
|---|---|
| [docs/claude-traps-hugo-rendering.md](docs/claude-traps-hugo-rendering.md) | Hugo ビルド・URL/taxonomy・PaperMod テーマCSS・Windows開発環境・パフォーマンス計測(Lighthouse/PSI)・Service Worker |
| [docs/claude-traps-github-actions.md](docs/claude-traps-github-actions.md) | CI・ブランチ保護・required check設計・git運用・エージェント委譲時の注意・Windows文字化け |
| [docs/claude-traps-external-apis.md](docs/claude-traps-external-apis.md) | Amazon Creator API・Rakuten API・GA4/GSC認証・SNS(X/Threads/Buffer) |
| [docs/claude-traps-analytics-data.md](docs/claude-traps-analytics-data.md) | GSC/GA4データの読み方・記事データ(サイドカー)の扱い |
| [docs/claude-traps-content-design.md](docs/claude-traps-content-design.md) | スコアリング・フィルタ・ゲート・コピーライティングの設計判断(WHY) |
| [docs/claude-traps-jules-workflow.md](docs/claude-traps-jules-workflow.md) | Jules セッション運用・workflow配線 |

## その他の設計ドキュメント

- [docs/failover-to-gitlab.md](docs/failover-to-gitlab.md) — GitHub↔GitLab フェイルオーバー手順
- [docs/navi-brain-split.md](docs/navi-brain-split.md) — 生成プロンプト(jules/)のprivate repo分離
- [docs/gitlab-migration-design.md](docs/gitlab-migration-design.md)
- [docs/article-quality-overhaul-design.md](docs/article-quality-overhaul-design.md)
- [docs/sns-x-post-mode.md](docs/sns-x-post-mode.md)

## TODO・課題管理

このリポジトリの GitHub Issues が一次管理 (`gh issue list -R omochairo/amazon --state open`)。
