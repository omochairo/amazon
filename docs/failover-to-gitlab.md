# GitHub ⇄ GitLab 運用構成とフェイルオーバー手順

2026-07-08 制定 (GitHub アカウント凍結 2026-06-25〜07-07 の教訓)。

## 恒久アーキテクチャ

| 役割 | 場所 |
|---|---|
| リポジトリ本体・Issues・cron 自動化・Jules・SNS | GitHub `omochairo/amazon` (public — private だと Actions 無料枠 2000分/月では不足) |
| サイトビルド・配信 (navi.omcha.jp) | GitLab `omocha/navi` (project id 84175362) の Pages。**GitHub Pages は無効** |
| 同期 | `.github/workflows/40-mirror-to-gitlab.yml` が main push を GitLab main へ転送 → GitLab `pages` ジョブが再ビルド |
| DNS | navi.omcha.jp → omocha.gitlab.io (CNAME)。**フェイルオーバー時も切替不要**。`navi` の TXT `google-site-verification=...` は GSC 所有権なので絶対に消さない |

- GitLab 側スケジュール5本は **inactive で温存** (フェイルオーバー時に active 化するだけで生成が再開する):
  - 4326910 invoke-jules (6h毎) / 4327609・4327610 fetch-data (01:00/10:00 UTC) / 4327611 rakuten-ranking (月 21:00 UTC) / 4327612 third-party-sources (21:30 UTC)
- GitLab の CI/CD 変数 18個・NAS runner (UGREEN DXP4800Plus, 192.168.68.62, runner id 54145618) は登録・常駐のまま維持
- GITLAB_TOKEN (project access token, Maintainer/api, **2027-06-30 期限**) は GitHub Secrets と `amazon-main/.env` の両方に保持。期限前に更新すること

## フェイルオーバー手順 (GitHub が再び使えなくなったとき)

**配信は何もしなくても継続する** (GitLab Pages は独立)。止まるのは新規生成のみ。

1. GitLab スケジュール5本を active 化:
   ```
   for id in 4326910 4327609 4327610 4327611 4327612; do
     curl -X PUT -H "PRIVATE-TOKEN: $GITLAB_TOKEN" \
       "https://gitlab.com/api/v4/projects/84175362/pipeline_schedules/$id?active=true"; done
   ```
2. 以後、repoless Jules パイプライン (scripts/invoke_jules_repoless.py + scripts/build_jules_prompt.py) と fetch 系 (scripts/ci_fetch_data.sh + scripts/create_data_mr.py) が GitLab MR + MWPS で無人運転する (2026-07-07 に E2E 実証済)
3. ミラー workflow は GitHub 側が止まるので自然停止。手動で止める必要なし

## 復帰手順 (GitHub が回復したとき)

1. GitLab スケジュール5本を inactive 化 (上記 curl の `active=false`)
2. reconcile: `git fetch origin gitlab` → GitLab 期の main を正として `git merge origin/main` (GitHub 期の有効な差分だけ取り込む) → gitlab main と origin main の両方へ push
3. GitHub workflows を **段階再開** (下記 wave 順)。一斉再開しない
4. ミラー workflow の成功を確認

## GitHub workflow の wave 構成 (再開順序)

- **wave 1 (データ)**: 01 fetch-products / 04 validate (required check 提供元、fetch PR のマージに必須) / 07 rakuten-ranking / 08 wiki-cache / 34 third-party-sources / 38 wikidata-refresh / refresh_threads_token / 40 mirror
- **wave 2 (生成・分析、wave 1 安定後 2日目安)**: 03 invoke-jules / 05 auto-merge / 06 lock-cleanup / 09-16 (feature-lists, brand-narrative, rewrite-idle 等) / 17-19 analytics / 37 toy-recall / 39 quarantine
- **wave 3 (SNS、さらに2日目安)**: 20 sns-publish / 29 29b 30 31 32 33 35 36 (engagement 系)
- **恒久 disable**: 02 publish (GitHub Pages デプロイは廃止。配信は GitLab。サイトビルドの workflow_run 連鎖 (02→03) は wave 2 で要再設計)

## BAN 再発防止 (トリガー規律)

2026-06-25 の凍結は **Antigravity による短時間の大量 issue 一括起票**のタイミングと一致 (サポート回答は「自動検知→手動レビューで解除」で理由非開示)。

- **issue の一括起票禁止**: 自動化・エージェントからの issue 作成は 1 バーストにつき数件まで。複数項目は 1 issue にまとめる
- Antigravity には gh / GitHub API を直接触らせない (Claude がオーケストレータとして代行)
- 新しい自動化を GitHub API に対して追加するときはレート・バースト特性を必ず確認する
