# GitHub ⇄ GitLab 運用構成とフェイルオーバー手順

2026-07-08 制定 (GitHub アカウント凍結 2026-06-25〜07-07 の教訓)。

## 恒久アーキテクチャ

| 役割 | 場所 |
|---|---|
| リポジトリ本体・Issues・cron 自動化・Jules・SNS | GitHub `omochairo/amazon` (public — private だと Actions 無料枠 2000分/月では不足) |
| サイトビルド | GitLab `omocha/navi` (project id 84175362) の CI。**GitHub Pages は無効** |
| **配信 (本番)** | **NAS のセルフホスト** (nginx + cloudflared)。`deploy-nas` ジョブが `pages` の成果物を rsync し symlink で原子的に切替 (2026-08-30 移管 / #6205) |
| **配信 (待機系)** | GitLab Pages。`pages` ジョブが push ごとに走るので**本番とバイト同一・同 sha** |
| 同期 | `.github/workflows/40-mirror-to-gitlab.yml` が main push を GitLab main へ転送 → GitLab の `pages` → `deploy-nas` |
| DNS | navi.omcha.jp CNAME。本番は `<uuid>.cfargotunnel.com` / 待機系は `navi-92dc61.gitlab.io`。**GitHub 障害のフェイルオーバー時は切替不要**（配信は GitHub に依存していない）。`navi` の TXT `google-site-verification=...` は GSC 所有権なので絶対に消さない |

**この文書が扱う「フェイルオーバー」は 2 種類ある。混同しないこと。**

| | 何が壊れたとき | 何が止まるか | 手順 |
|---|---|---|---|
| **GitHub 障害** | GitHub が使えない | 新規生成だけ。**配信は継続** | 下記「フェイルオーバー手順」 |
| **オリジン障害** | NAS / 自宅回線 / トンネル | **配信そのもの** | 下記「オリジン障害時の自動フェイルオーバー」 |

- GitLab 側スケジュール5本は **inactive で温存** (フェイルオーバー時に active 化するだけで生成が再開する):
  - 4326910 invoke-jules (6h毎) / 4327609・4327610 fetch-data (01:00/10:00 UTC) / 4327611 rakuten-ranking (月 21:00 UTC) / 4327612 third-party-sources (21:30 UTC)
- GitLab の CI/CD 変数 18個・NAS runner (UGREEN DXP4800Plus, runner id 54145618) は登録・常駐のまま維持
  - **LAN の実 IP はこの public repo に置かない。** 実体は private の `omochairo/amazon-home-ops`
    (`docker/navi/README.md` と `.github/workflows/90-heartbeat-check.yml`) にある
- GITLAB_TOKEN (omocha/navi の project access token `clodecode` id 25434570, Maintainer/api, **2027-07-07 期限**) は GitHub Secrets と `amazon-main/.env` の両方に保持。期限前に更新すること
  - 旧 `clode` (id 25434376, 2027-06-30 期限) は既に inactive。参照しないこと
  - 別系統として `vscode/.env` の GITLAB_TOKEN は **omocha/writer** (project id 84200456) の project access token `clodecode` id 25455442 (api, 2027-07-07 期限)。navi のフェイルオーバーには無関係
  - 手元の glab CLI は K 本人アカウントの personal access token `claude` id 26140733 (api, **2027-07-31 期限**、2026-07-31 に rotate 済) を `AppData\Local\glab-cli\config.yml` に保持。CI とは無関係だが、フェイルオーバー時の GitLab 側調査に使うので切らさないこと

## フェイルオーバー手順 (GitHub が再び使えなくなったとき)

**配信は何もしなくても継続する** (GitLab Pages は独立)。止まるのは新規生成のみ。

1. GitLab スケジュール5本を active 化:
   ```
   for id in 4326910 4327609 4327610 4327611 4327612; do
     curl -X PUT -H "PRIVATE-TOKEN: $GITLAB_TOKEN" \
       "https://gitlab.com/api/v4/projects/84175362/pipeline_schedules/$id?active=true"; done
   ```
2. 以後、repoless Jules パイプライン (scripts/invoke_jules_repoless.py + scripts/build_jules_prompt.py) と fetch 系 (scripts/ci_fetch_data.sh + scripts/create_data_mr.py) が GitLab MR + MWPS で無人運転する (2026-07-07 に E2E 実証済)
   - **⚠️ 2026-07-22 以降の前提**: `jules/` プロンプトは private repo `omochairo/amazon-navi-brain` に分離済み (public repo では .gitignore・非追跡)。GitLab/NAS 側で生成を再開する場合、`build_jules_prompt.py` が `jules/PROMPT_TEMPLATE.md` を要求するため、**NAS runner に amazon-navi-brain を `jules/` へ取得させる必要がある** (GitLab CI 変数に github read PAT を置き、invoke-jules ジョブの before_script で clone するか、NAS 上に常設 clone を bind mount)。GitHub 側の overlay checkout に相当する処理を GitLab 側にも用意しないと生成が空プロンプトで失敗する。詳細: docs/navi-brain-split.md
3. ミラー workflow は GitHub 側が止まるので自然停止。手動で止める必要なし

## 復帰手順 (GitHub が回復したとき)

1. GitLab スケジュール5本を inactive 化 (上記 curl の `active=false`)
2. reconcile: `git fetch origin gitlab` → GitLab 期の main を正として `git merge origin/main` (GitHub 期の有効な差分だけ取り込む) → gitlab main と origin main の両方へ push
3. GitHub workflows を **段階再開** (下記 wave 順)。一斉再開しない
4. ミラー workflow の成功を確認

## オリジン障害時の自動フェイルオーバー (#6205 B-1)

本番オリジン (NAS + cloudflared) が落ちたとき、`navi.omcha.jp` の CNAME を
**待機系 (GitLab Pages) へ自動で倒す**。実装は
`scripts/failover_dns.py` + `.github/workflows/53-origin-failover.yml`。

### なぜ自動で倒してよいか

**待機系は劣化した代替ではない。** `pages` ジョブは push ごとに走るので、
待機系は本番とバイト同一・同 sha で、301 リダイレクト 512 本も 404 も同じに返る
(2026-08-31 実測)。倒すことのコストがほぼゼロなので自動化に見合う。

裏返すと **戻すことは急がない。** だから戻しは手動にしてある。この非対称は意図的で、
「自動で戻す」を成立させるには待機系を向いている間もトンネルの生死を見る経路
(常設の `navi-origin.omcha.jp` + cloudflared の ingress 追加 = NAS 側の手作業) が
必要になる。得られるものに見合わない。

### 何を「オリジン障害」と呼ぶか

`https://navi.omcha.jp/build.json` に **`cf-ray` があって 5xx** —— つまり
Cloudflare までは届いているのにオリジンが応えない場合だけ。
(このパスは Cache Rule `navi-build-json-nostore` で cache bypass なので、
エッジのキャッシュにも Always Online にも化けない。)

`cf-ray` が無い失敗 (接続不能 / DNS 不能 / runner 側の一時障害) では
**CNAME を触らない。書き換えても直らない障害で DNS を殴らない。**
Cloudflare 自体が落ちているとき待機系に倒しても、待機系も CF の裏にいる。

### 倒さないための 5 つの防壁

| 防壁 | 内容 |
|---|---|
| 連続性 | 1 run の中で `--attempts` (既定 3) 回連続で失敗したときだけ。1 回でも応答したら倒さない |
| 待機系の健全性 | 倒す前に待機系の `build.json` を確認。死んでいる / 24h 超に古いなら `blocked` で鳴らすだけ。**倒すと部分障害が全面障害になる** |
| クールダウン | レコードの `modified_on` が 2h 以内なら倒さない (フラップ抑止)。DNS レコード自身を状態に使うのでこちら側に状態を持たない |
| kill switch | リポジトリ変数 `FAILOVER_ENABLED` が真でない限り書き換えない |
| 向き先の検査 | CNAME が本番でも待機系でもない値なら自動で触らない |

### 段階投入

`FAILOVER_ENABLED` 未設定の間は判定と起票だけで DNS を書き換えない。
数日回して誤検知が無いことを確かめてから武装する。

```bash
gh variable set FAILOVER_ENABLED --body true -R omochairo/amazon
```

### 戻しかた (手動)

**先に NAS 側 (cloudflared / nginx / navi-switch) の復旧を確認すること。**

```bash
gh workflow run 53-origin-failover.yml -R omochairo/amazon -f direction=nas
```

戻し先の CNAME は、倒したときに issue 本文へ機械可読で埋めてある
(`<!-- failover-prev-cname: ... -->`)。トンネル UUID を人が控えておく必要はない。
issue を閉じてしまった場合は `-f target=<uuid>.cfargotunnel.com` を明示する。

### 倒れている間に起きること

- GitLab パイプラインの `deploy-nas` が失敗し続ける (NAS に届かない) → パイプラインは赤くなるが、
  `pages` は前のステージなので**待機系の更新は続く**
- `cf-purge` は `needs: deploy-nas` なので走らない → エッジの HTML が最大 `edge_ttl` (4h) 古くなる
- **GitLab Pages の 1 GiB 上限が生きた制約に戻る** (#6206)

### 関連する監視との分担

- `51-delivery-freshness-monitor` — 「更新され続けているか」。閾値 8h・原因非依存。**落ちた瞬間には鳴らないし、直さない**
- `53-origin-failover` — 「いま応答しているか」。直せる障害のときだけ手を動かす

両方要る。

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
