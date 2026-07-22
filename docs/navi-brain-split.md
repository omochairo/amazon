# navi-brain split — 編集IP(プロンプト)の private 分離

2026-07-22 制定。public repo `omochairo/amazon` を公開のまま維持しつつ、競合に模倣される
価値の高い「編集知能」(生成プロンプト / few-shot 手本) だけを private repo に隔離する構成。

## 背景・判断

- `omochairo/amazon` は **public**。public repo は GitHub Actions が無制限無料 (private だと
  月2,000分の従量; 実測影メーターで動画無し月 ~$40 / 動画有り月 ~$80 相当)。この無料
  クラウド CPU を手放したくない → repo 全体の private 化 / 全ジョブの NAS 移設は採らない。
- 一方で `jules/*.md` (プロンプト) は **生きたサイトから観測できない唯一の真の秘密**。
  ASIN/キーワードの狙撃先はサイト巡回で既に丸見えなので隠す実益は薄いが、プロンプトは
  出力しか露出しないため隔離価値が高い (シナリオB: 一部抜き取りへの防御)。
- 過去履歴は purge しない (**P1 stale-out**)。プロンプトは既に公開済み=過去版の秘匿価値は
  毀損済み。守るのは「これから育てる最新版」で、それは分離後 private 側に閉じる。履歴 purge は
  GitLab ミラーの fast-forward を壊すコストに見合わない。

## 構成

```
omochairo/amazon (PUBLIC)                       ← 41 workflow は無料 runner で継続
  ├─ jules/          … .gitignore・非追跡 (public tree から消える。履歴は残置)
  └─ .github/workflows/{03,04,14,29,33,35}.yml  … 実行時に brain を jules/ へ overlay checkout

omochairo/amazon-navi-brain (PRIVATE)           ← 旧 jules/ の中身がルート。Actions 走らせず=課金ゼロ
```

### なぜ submodule ではなく gitignore + 実行時 checkout か
- GitLab pages ジョブは `GIT_SUBMODULE_STRATEGY: recursive`。jules を submodule 化すると NAS が
  github private repo を認証なし fetch して **pages ビルドが壊れる**。gitignore 方式なら submodule が
  存在しないので問題自体が消える (`.gitlab-ci.yml` 無改修)。
- submodule 未初期化トラップ (空 HTML 偽陽性) の地雷面を増やさない。
- プロンプト改訂ごとの submodule ポインタ bump が不要 = brain 側で独立進化 (P1 と整合)。
- overlay checkout 用 PAT を brain のみに限定でき最小権限。
- トレードオフ: プロンプト版の SHA ピン止めは無し (プロンプトには独立浮動が望ましいので許容)。

## 消費側 (public repo workflow)

jules/ を読む 6 workflow (03/04/14/29/33/35) は本体 checkout の直後に以下を追加:

```yaml
- name: Checkout prompt brain (private) into jules/
  uses: actions/checkout@v6
  with:
    repository: omochairo/amazon-navi-brain
    token: ${{ secrets.NAVI_BRAIN_PAT }}
    path: jules
```

- 本体 checkout の **後** に置く (先だと本体 checkout が jules/ を消しうる)。
- `cat jules/…` / `build_jules_prompt.py` の `_read("jules/…")` はパス無改修で動く。
- Jules へは workflow が読んだテキストを API 引数で渡すので Jules 自身は brain へアクセス不要。

## secret: NAVI_BRAIN_PAT

- Fine-grained PAT。Resource owner=omochairo、対象リポジトリ=**amazon-navi-brain のみ**、
  権限=**Contents: Read-only** のみ。他は付与しない (最小権限=事故時の被害を markdown に限定)。
- `omochairo/amazon` の Actions secret に `NAVI_BRAIN_PAT` として登録。
- 有効期限を付けた場合は失効前に再発行 (失効すると 6 workflow の checkout が失敗し生成停止)。

## ビルド / GitLab への影響

- pages ビルド系 (build_post.py 等) は jules/ を **一切参照しない** → GitLab/NAS 配信は無影響。
- GitLab スケジュール (invoke-jules 等) は現在 `active=False` (ホットスタンバイ)。
  フェイルオーバーで再開する場合の手当ては docs/failover-to-gitlab.md 参照。

## ローカル開発

public repo を clone しただけでは jules/ が無い。プロンプトを触る場合は brain を clone:

```
git clone https://github.com/omochairo/amazon-navi-brain jules
```

(jules/ は .gitignore 済みなので public repo の作業ツリーを汚さない。)
プロンプト改訂は amazon-navi-brain 側へ commit/push する。
