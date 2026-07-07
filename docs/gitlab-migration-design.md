# GitLab 移行設計: Jules Repoless 記事生成パイプライン

2026-07-07 起草。GitHub アカウント凍結 (2026-06-25) に伴い、運用を
gitlab.com/omocha/navi (project id 84175362) へ移行する。サイト配信
(GitLab Pages + navi.omcha.jp) は移行済み。本書は自動化パイプライン、
特に記事生成 (旧 03-invoke-jules.yml) の移植設計。

## 0. 前提と決定事項

- Jules は GitLab をソースとして接続できない (GitHub 限定)。Jules の MCP は
  承認済み 6 サービスのみでカスタム MCP 追加不可。よって **Repoless セッション**
  (sourceContext 省略 + file outputs 取得) で GitHub 依存を切る。
- 別名義 GitHub アカウントは ban evasion のため作らない (appeal 継続中)。
- 生成部分の次善策は gemini CLI (agy) 置換だが、プロンプト資産と品質検収の
  再実施コストから Repoless を第一候補とする。

## 1. 仕様検証で見つけた穴と解消方針

| # | 穴 | 深刻度 | 解消方針 |
|---|---|---|---|
| H1 | 公式 API リファレンスは `sourceContext` を **Required** と記載。changelog (2026-01-26) は「省略で repoless」と記載し矛盾 | 致命 (前提崩壊の可能性) | **PoC で実 API 検証** (フェーズ0)。省略で 400 が返るなら repoless は UI 限定であり、gemini CLI 置換に方針転換 |
| H2 | file outputs の取得エンドポイントが未確定 (changelog は「change set を git patch 形式で取得」とのみ記載。session.outputs フィールドの構造不明) | 高 | PoC で `GET /sessions/{id}` の outputs と activities を実地ダンプして確定 |
| H3 | プロンプトサイズ上限が不明。現行 prompt は約 45KB (INTRO + PROMPT_TEMPLATE.md 43KB)。repoless では repo が読めないため per-ASIN データ同梱で **70-90KB** に増える | 高 | PoC で実測。同梱データは対象 ASIN スライスに限定 (§4.3)。超過時は (a) データを activities 経由で分割投入 (b) 圧縮要約 の順に検討 |
| H4 | repoless 環境に google_search / URL fetch ツールがあるか不明。PROMPT_TEMPLATE は検索による裏取りを前提とする | 高 | PoC の生成物で sources の実在性を検収。検索不可なら third_party_sources.json の事前収集 URL だけで書かせる設計に縮退 |
| H5 | 現行プロンプトは「AGENTS.md を最初に必ず読み」と repo 内ファイル参照を指示。repoless では読めない | 中 | AGENTS.md の STAGE1 関連節をプロンプトに **インライン化**。テンプレート組立スクリプト (§4.3) で機械的に結合 |
| H6 | **GitLab.com 無料枠は 400 compute 分/月** (shared runner)。публиш 1 回 5-10 分 × 記事 merge 最大 24/日で数日で枯渇する | 致命 (運用不能) | **セルフホストランナー必須** (§4.1)。自前ランナーは無制限・無料。shared runner は使わない設計にする |
| H7 | GitLab の pipeline schedule は「所有ユーザー」で実行される。project access token の bot ユーザーが schedule を所有できるかは要検証 | 中 | 実装時に API で作成して検証。不可ならユーザー本人の PAT で schedule だけ作成 (1 回きりの操作) |
| H8 | 現行 .gitlab-ci.yml の pages job は main への **全 push** で走る。schedule 起動 (invoke-jules) でも走ってしまい二重実行になる | 中 | 全 job に `rules` で pipeline source を明示分離: pages=push のみ / invoke=schedule のみ (§4.2) |
| H9 | GitLab の auto-merge (MWPS: merge when pipeline succeeds) は「パイプライン成功」が条件。04 相当の validate を **MR パイプライン**として走らせ、かつプロジェクト設定 "Pipelines must succeed" を有効化しないと素通りする | 中 | validate job を `merge_request_event` rule で定義し、プロジェクト設定を API で有効化 |
| H10 | GitHub 固有ハックの残骸: 05 の「GITHUB_TOKEN merge は push cascade を発火しない → merge を polling して publish を workflow_dispatch」は GitLab に該当制約が無い | 低 (簡素化) | **丸ごと不要**。MR merge の main push が pages パイプラインを普通に起動する。lock 即時解放だけ移植する |
| H11 | Jules クォータゲート (jules_quota_gate.py) の移植性 | なし (確認済) | Jules API 直叩きで GitHub 依存ゼロ。環境変数 JULES_API_KEY だけでそのまま動く |
| H12 | GitLab のブランチ作成の原子性 (jules-lock の排他) | なし (確認済) | `POST /projects/:id/repository/branches` は既存ブランチ名で 400 を返す。GitHub の ref 作成 422 と同等の atomic 排他が成立 |

## 2. 全体アーキテクチャ

```
[schedule 6h毎] invoke-jules (GitLab CI, self-hosted runner)
  ├─ jules_quota_gate.py で作成枠 (0..6) を算出          … H11 そのまま移植
  ├─ ASIN 選定: 既存記事 + open MR + jules-lock/* を除外   … gh → GitLab API 置換
  ├─ jules-lock/<ASIN> ブランチ作成 (atomic 排他)          … H12
  ├─ プロンプト組立 (scripts/build_jules_prompt.py 新規)   … H3/H5
  │    INTRO + AGENTS.md抜粋 + PROMPT_TEMPLATE.md
  │    + per-ASIN データスライス (amazon/rakuten/yahoo/youtube/per_asin/*)
  ├─ Jules repoless セッション作成 (sourceContext なし)     … H1
  └─ 完了 poll → outputs 取得 (git patch)                  … H2
       └─ 記事 JSON 抽出 → branch + commit + MR 作成 (GitLab API)
            └─ MWPS (squash + source branch 削除) を即時セット … H9

[MR event] validate (04 移植, self-hosted runner)
  └─ スコープガード + スキーマ/品質検証 → 成功で MWPS が自動 merge
       └─ merge → main push → pages パイプライン (既存) → サイト反映 … H10
       └─ merge 時に jules-lock/<ASIN> 解放 (validate 成功後 or cleanup で回収)

[schedule 30分毎] lock-cleanup (06 移植)
  └─ TTL 超過 (1h) の jules-lock/* ブランチ削除
```

## 3. フェーズ計画

- **フェーズ0 (PoC)**: H1〜H4 の実地検証。ローカルから 1 ASIN で
  repoless セッション → 記事 JSON 取得 → 手動で GitLab MR 作成まで通す。
  **JULES_API_KEY が必要 (要ユーザー提供)**。ここで前提が崩れたら gemini CLI 案へ転換
- **フェーズ1**: self-hosted runner 立ち上げ (H6) + CI/CD 変数登録
- **フェーズ2**: 03 移植 (invoke-jules job + build_jules_prompt.py + MR 作成スクリプト)
- **フェーズ3**: 04/05/06 移植 (validate MR パイプライン + MWPS + lock cleanup)
- **フェーズ4**: 01-fetch / 07-rakuten 等のデータ更新系 (別設計、Jules 非依存なので機械移植)
- 対象外 (当面停止のまま): SNS 系 (20/30/35/36)、分析系 (17/18/19)、engagement 系 (29-33)

## 4. コンポーネント詳細

### 4.1 Runner 戦略 (H6)

- gitlab-runner を **自宅 Windows 機の WSL2/Docker** または **VPS** に常駐。
  project runner として登録し、shared runner はプロジェクト設定で無効化
  (誤課金・枠消費の予防)。
- 既存 pages job も同 runner で実行 (image: python:3.11-bookworm を pull できる
  Docker executor)。
- タグ設計: `omocha-docker` を全 job の `tags:` に指定。

### 4.2 .gitlab-ci.yml の rules 分離 (H8)

```yaml
pages:
  rules:
    - if: '$CI_PIPELINE_SOURCE == "push" && $CI_COMMIT_BRANCH == "main"'
invoke-jules:
  rules:
    - if: '$CI_PIPELINE_SOURCE == "schedule" && $SCHEDULE_JOB == "invoke-jules"'
validate-article:
  rules:
    - if: '$CI_PIPELINE_SOURCE == "merge_request_event"'
lock-cleanup:
  rules:
    - if: '$CI_PIPELINE_SOURCE == "schedule" && $SCHEDULE_JOB == "lock-cleanup"'
```

schedule は変数 `SCHEDULE_JOB` で多重化 (schedule 2 本: 6h 毎 / 30 分毎)。

### 4.3 プロンプト組立 (H3/H5) — scripts/build_jules_prompt.py (新規)

repoless は repo を読めないため、現行プロンプトの「repo 内ファイルを読め」系
指示を全てデータ同梱に置換する。同梱するのは対象 ASIN のスライスのみ:

| データ | 元ファイル | スライス方法 | 想定サイズ |
|---|---|---|---|
| 商品本体 | data/raw/amazon.json (184KB) | items[] から対象 ASIN の 1 エントリ | 2-4KB |
| 楽天価格 | data/raw/rakuten_matched.json (2.6MB) | matched_asin == ASIN | <2KB |
| Yahoo価格 | data/raw/yahoo_matched.json (2.2MB) | matched_asin == ASIN | <2KB |
| 動画 | data/raw/youtube.json (36KB) | ASIN 関連のみ | <5KB |
| per-ASIN 素材 | data/raw/per_asin/<ASIN>/ | 全ファイル (実測 ~15KB) | ~15KB |
| ルール | AGENTS.md (15KB) | STAGE1 関連節を抽出 | ~8KB |
| テンプレート | jules/PROMPT_TEMPLATE.md | 全文 (43KB, 現行同様) | 43KB |

合計 ~80KB 見込み。PROMPT_TEMPLATE 内の「data/raw/... を読む」という記述は
「以下に同梱したデータを使う」に読み替える前置きを INTRO に追加する
(テンプレート本体は GitHub 復帰時のために書き換えない)。

### 4.4 セッション作成〜MR 作成 (H1/H2) — scripts/jules_repoless_session.py (新規)

1. `POST /v1alpha/sessions` — prompt のみ、sourceContext なし、
   automationMode 指定なし (AUTO_CREATE_PR は GitHub 専用のため)
2. `GET /sessions/{id}` を 30s 間隔で poll (上限 30 分)。
   terminal: COMPLETED / FAILED。AWAITING_USER_FEEDBACK は失敗扱いで lock 解放
3. COMPLETED 後、outputs (git patch) を取得し `data/articles/<date>-<ASIN>.json`
   に該当する追加ファイルを抽出。**patch 全体は適用しない** (旧 05 の scope guard
   と同じ思想: 記事 JSON 1 ファイル以外は捨てて MR 差分を最小化)
4. GitLab API で branch `add-article-<ASIN>` 作成 → commit (file create) →
   MR 作成 (squash=true, remove_source_branch=true) → MWPS セット

### 4.5 validate (04 移植)

- 発火: merge_request_event、対象パス data/articles/**
- 内容: 現行 04 の検証スクリプト群 (スキーマ/品質/価格整合) をそのまま実行。
  GitHub 依存は PR 番号参照程度なので CI_MERGE_REQUEST_IID に置換
- スコープガード (旧 05): `git diff $CI_MERGE_REQUEST_DIFF_BASE_SHA...HEAD` が
  data/articles/ 以外に触れていたら fail → MWPS が発火せず auto-merge 停止
- プロジェクト設定 `only_allow_merge_if_pipeline_succeeds=true` を API で有効化

### 4.6 secrets (CI/CD 変数)

フェーズ2 時点で必要なのは `JULES_API_KEY` のみ (masked)。
フェーズ4 で AMAZON_CREATORS_* / RAKUTEN_* 等を追加。GITHUB_TOKEN /
APP_ID / APP_PRIVATE_KEY は GitLab では不要 (CI_JOB_TOKEN + project token で代替)。

## 5. 未解決事項 (ユーザー判断待ち)

1. **JULES_API_KEY の提供** — jules.google.com → Settings → API keys で確認/再発行
   (PoC と CI 変数登録に必要)
2. **ランナーの設置場所** — 自宅 Windows 機 (WSL2+Docker, 電源常時ON前提) か
   VPS (月数百円〜) か
3. Jules の稼働前提が PoC で崩れた場合、gemini CLI (agy) 置換案へ転換してよいか
