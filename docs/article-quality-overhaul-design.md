# 記事の脱・凡庸化 設計書 (プロンプト v7 + 体験談供給レーン)

作成: 2026-07-15 / 起点: #2928 の gemma 回答性監査 (#2995 消費側②) の owner 評価

## 0. 背景と問題定義

#2995 消費側② (GSC 回答性 entailment 監査) の初回実機診断 (#2928) で、gemma は
低 CTR ページの不足観点として次の 2 点を挙げ、owner がこれを「全記事に共通する妥当な
指摘」と確認した:

1. 具体的なユーザー体験談 (良い点・悪い点の詳細、実使用シーン)
2. 他の同ジャンル玩具との比較による評価

さらに owner の評価: **各記事が同じような内容で凡庸**。記事単位のリライトでは直らず、
Jules の記事設計仕様そのものに原因がある。

### 0.1 根因分析 (2026-07-15 調査で確定)

| 症状 | 根因 | 種別 |
|---|---|---|
| 体験談が薄い | `data/raw/reviews.json` = 空 `{}`。per_asin の news/youtube/books も 0 件の商品が多数 (例 B0FNM4Y35G は全て 0)。テンプレ (v6 §6.5.4) は「レビュー直接引用必須・創作厳禁」を要求するが、**引用すべき素材が供給されていない**。repoless の Jules 組み込み検索は頻繁に失敗する前提が v6 自身に明記されている。結果、商品ページの言い換えに退化する | データ供給の欠落 |
| 比較が無い | ASIN ハルシネーション事故対策で Jules の競合記述は全面禁止 (v6 §6.5)。`competitive_analysis` は `build_post._override_competitive_analysis` がカードを自動入稿するのみで、「どう選び分けるか」の編集文はどの記事にも構造的に存在しない | 仕様による意図的欠落 |
| 凡庸・同型 | 全記事が同一の 6 narrative セクション + 4-step 構造 + hook 3 パターン + title 公式。品質ゲートは「型への準拠」を検査し「情報利得」を検査しない。リライトレーン (12-rewrite-idle-fill) も同じ型で再生成するだけ | 型の完全固定 |

**設計原則**: LLM 記事の品質は「プロンプトの言い回し」ではなく「同梱する商品固有情報の
量と質」で決まる。凡庸さは固有情報ゼロの状態で型を埋めさせている帰結であり、改善は
(a) 固有素材の供給、(b) 禁止の安全な解除、(c) 記事の軸の個性化、の 3 方向を同時に行う。

## 1. 全体構成 (4 フェーズ)

| Phase | 内容 | 対象リポジトリ | 状態 |
|---|---|---|---|
| 1 | プロンプト v7: 比較・選び分けセクション解禁 + unique angle + 監査不足観点のリライト注入 | amazon | 本設計で実装 |
| 2 | 体験談供給レーン (K8 gemma + Antigravity CLI (agy) + Threads + third-party 本文 + Yahoo レビュー低速蓄積) | amazon + amazon-home-ops | 2026-07-15 方針確定 (§5) |
| 3 | 凡庸度の定量監査 (Ruri embedding 類似度) | amazon-home-ops | 設計のみ (§6) |
| 4 | 効果測定 (answerability coverage / CTR の前後比較) | amazon | 観察 |

Phase 1 だけでも gemma 指摘の「比較欠落」と「リライトが同じ記事を再生産する」問題は
解消する。「体験談」は Phase 2 のデータ供給が入って初めて本質解決する (Phase 1 の
時点では創作禁止を優先し、無い素材を書かせない方針を維持)。

## 2. Phase 1-A: 比較・選び分けセクション (`narrative.how_to_choose`)

### 2.1 仕様

- `narrative` に新キー `how_to_choose` (narrativeSection: array of string) を追加。
  **v7 以降の新規生成記事では必須**。既存記事には遡及しない。
- 内容要件 (PROMPT_TEMPLATE v7 に §5.F として明文化):
  - 読者の問い「似た商品が多い中で、なぜ/どういう場合にこれを選ぶべきか」に答える
  - 同梱 `per_asin/<ASIN>/competitors.json` の競合 (名前・価格・features) と自商品を、
    読者の選択軸 (年齢適合・価格帯・遊びの性質・安全性・拡張性など、その商品で意味の
    ある軸を 2 つ以上) で対比する
  - 「〜な家庭はこの商品、〜を重視するなら競合 X」の**条件分岐型の推奨**を最低 1 つ含む
  - 2〜4 要素、合計 150 字以上 (他セクションと同じ array-of-string 規律)
- **言及可能な商品の範囲 (ハルシネーション対策の要)**:
  - 同梱 competitors.json / omcha_related.json に実在するエントリのみ。ASIN・商品名・
    価格の創作は禁止
  - competitors が空の商品では、固有商品名を出さずに「このジャンルの選択軸」
    (素材・ピース数・対象年齢帯などの一般軸) として書く
- `competitive_analysis` フィールドの扱いは v6 と不変 (Jules 出力不要・ビルド側自動入稿)。
  how_to_choose は「自動カードの下に載る編集文」という位置づけ

### 2.2 機械検査 (quality_gate.py)

新チェック `check_how_to_choose`:

1. **施行日ゲート**: slug 日付 >= `2026-07-16` の記事のみ必須 (定数
   `HOW_TO_CHOOSE_ENFORCE_FROM`)。それ以前の slug は skip (既存記事の軽微修正 PR を
   落とさない)
2. 存在 + 合計 150 字以上 (既存 `NARRATIVE_MIN_CHARS` の枠組みに準拠)
3. **ASIN 封じ込め**: 本文中の `B0[A-Z0-9]{8}` パターンは
   `data/raw/per_asin/<ASIN>/competitors.json` の asin 集合の部分集合であること。
   competitors.json が読めない実行環境ではASIN 言及ゼロを要求 (安全側フォールバック)
4. `REQUIRED_NARRATIVE_KEYS` には**追加しない** (check_narrative は全年代の記事に走る
   ため)。独立チェックとして registrer する

### 2.3 レンダリング (post.md.j2 / build_post.py)

- `post.md.j2`: `competitive_analysis` セクションの直後・`same_price_band` の前に追加:
  - H2: `<small>{{ product.name }}の</small>🧭 選び方：似た商品とどう選び分ける？`
  - `{{ narrative_section(narrative.how_to_choose) }}`
  - competitive_analysis が無くても独立レンダリング (ジャンル一般軸の文でも単独で成立)
- `build_post.py`: メタ語り scrub ループ (`_meta_re` 適用箇所) の対象キーに
  `how_to_choose` を追加。legacy v3 setdefault (6 キー) は変更しない (optional のため)
- `scripts/audit_query_entailment.py` の本文抽出キー列挙に `how_to_choose` を追加
  (監査が新セクションを読めるように)
- `data/schema/article.schema.json`: `narrative.properties.how_to_choose` を
  narrativeSection 参照で追加 (**required には入れない** — 施行日ゲートは gate 側の責務)

## 3. Phase 1-B: 記事の軸 (unique angle) の必須化

同型化の直接対策。PROMPT_TEMPLATE v7 に §1.E として追加:

- 記事を書き始める前に、同梱データ (features / competitors との差分 / 認証 / 価格
  ポジション / GSC クエリ) から「**この商品が同ジャンルの他商品と違う 1 点**」を特定する
- その 1 点を title の差別化シグナル (#2717 の既存要件)・lead hook・how_to_choose の
  選択軸に**一貫して**使う (= 記事の背骨)
- 禁止: どの商品にも言える汎用の軸 (「知育に良い」「カラフル」「プレゼントに人気」)
- unique angle は内部処理でありフィールドとして出力しない (persona シナリオと同じ扱い)

機械検査はしない (定性要件)。効果は Phase 3 の類似度監査と answerability coverage で
定量観測する。

## 4. Phase 1-C: 監査不足観点のリライト注入 (#2995 消費側② Phase 3)

監査 → リライトのループを閉じる。

- `build_jules_prompt.py` に `_audit_note(asin)` を追加:
  - `data/analytics/answerability_audit.json` の `pages[]` から `asin` 一致エントリを
    探し、あれば各クエリの `missing_aspects` を列挙した
    【前回記事に不足していた観点 (読者の実検索クエリ監査)】ブロックを生成
  - 指示文: 「これらは実際に検索流入している読者クエリに対し前回記事が答えられて
    いなかった観点。**同梱データで裏取りできる範囲で必ず埋める**。裏取りできない観点は
    創作せず省いてよい (創作禁止が優先)」
  - rewrite_queue マーカーの有無に依存しない (audit にエントリがあれば常に注入 —
    低 CTR で監査対象になった ASIN の再生成は事実上すべてリライト)
  - 取得失敗・データ無しは best-effort で空文字 (既存 `_gsc_note` と同じ規律)
- 監査 JSON は日次で数件ペースのため、当面はプロンプト肥大の懸念なし (1 ASIN あたり
  数百字)

## 5. Phase 2 設計: 体験談供給レーン (experience.json)

「具体的なユーザー体験談」はデータが無い限り書けない (創作禁止)。K8 LLM ワーカー
(#2995) の新消費タスクとして供給側を作る。

**2026-07-15 owner 判断で確定した方針** (初版から変更):

- YouTube 広域検索レーンは**不採用** (玩具ジャンルはキッズ遊び動画が支配的でレビュー
  動画のヒット率が低く、コストメリットが薄い — owner 指摘)。per_asin/youtube.json に
  関連度一致済みの動画が既にある場合のみ字幕を拾う opportunistic 処理に降格
- Amazon のレビュー情報 (「お客様のご意見」含む) は**当面見送り**。Conditions of Use /
  Associates 規約が自動取得を禁止しており、収益根幹のアソシエイトアカウントを賭ける
  リスクに見合わない。Lane 1+2 の充足率を見て不足が残る場合に有償代行 API
  (Rainforest 等 $66/月〜) でのリスク外部化を再検討する
- 楽天レビューは**不採用**。楽天はレビューを「テキストマイニング防止のため」API 非提供
  とし、スクレイピング非推奨を明示回答した実績がある (グレー黙認ではなく明示的拒否)
- Yahoo!ショッピングのレビューは owner 判断で**低速蓄積を採用** (Lane 2)。公式レビュー
  API は 2021 年提供終了のため未ログイン・低負荷クロールで代替。法的整理: 未ログイン・
  低頻度・情報解析目的は著作権法 30 条の 4 の範囲。**原文はサイトに出さない** (§5.3)

### 5.1 Lane 1 (無料・規約準拠) — 主軸

| ソース | 取得方法 | コスト | 含まれる体験情報 |
|---|---|---|---|
| Web 横断の評判要約 | **Antigravity CLI (`agy`)** をヘッドレス実行 (`dbus-run-session -- agy --print "..."`)。`{商品名} ({ブランド})` の口コミ・評判・使用感を Web 検索ツール付きで要約させる | Google AI Pro 定額契約 (個人契約を自動化バッチに利用、owner 判断) | AI Overview 相当の Web 集合知の要約 (旧 Gemini API grounding は無料枠 5,000 prompts/月 をすぐ使い切るため 2026-07-15 に置換) |
| third_party_sources の本文 | 既存 #1600 Tavily URL を K8 側で本文 fetch | 無料 (fetch は自前) | ブログ/メディアの使用レポート (引用可) |
| SNS の生の声 | **Threads keyword_search API** (公式・threads_keyword_search 権限・2,200 クエリ/日) | 無料 | 購入報告・使用感の生投稿 (#496 の答え) |
| per_asin/news.json の本文 | K8 側で本文 fetch | 無料 | 受賞・イベント・体験会の記述 |
| YouTube 字幕 (降格) | per_asin/youtube.json に既存エントリがある場合のみ字幕取得 | 無料 | レビュー動画の実使用観察 (0 件なら skip) |

#### agy 運用上の注記

- **owner 判断の記録**: 個人の Google AI Pro 定額契約を自動化バッチ (K8 LLM ワーカーの
  夜間 cron) に利用することを owner が判断済み (2026-07-15)。Gemini API grounding の
  無料枠クォータをすぐ使い切る問題を解消する目的
- **初回セットアップ**: K8 の WSL2 Ubuntu ホスト側 (`/home/aisys`) で 1 回限りの手動
  ブラウザ認証が必要。`dbus-run-session -- agy --print "..."` を実行すると表示される
  OAuth URL をブラウザで開いて認可コードを貼り付ける。認証状態は
  `/home/aisys/.gemini/antigravity-cli` に保存される
- **コンテナへの認証共有**: `23-experience-mining.yml` の GitHub Actions job は使い捨て
  Docker コンテナ内で実行されるため、上記の認証ディレクトリを bind mount で明示的に
  共有する (amazon-home-ops リポジトリの `docker/docker-compose.k8.yml` 参照)。この
  bind mount はコンテナ侵害時に Google アカウントへの永続アクセス経路になるリスクが
  あるが、K8 が個人所有の自宅マシンで外部到達性が低いことを踏まえ owner が承認済み
- **Jules API との比較**: 同種タスク (口コミ検索・要約) を invest-content-studio の
  Jules API キーで試したところ 9 分17秒かかり不採用と判断した。agy は実測で約1分以内
  に完了することを確認済み

### 5.2 Lane 2 (owner 承認済み) — Yahoo レビュー低速蓄積

設計条件 (すべて必須):

1. 未ログイン・自宅回線 (K8/NAS レーン)・夜間スケジュール
2. 1 リクエスト 15〜30 秒間隔 + ジッター、robots.txt 尊重、正直な UA
   (連絡先 URL 入り)。bot 検知の回避策は実装しない
3. 対象は監査対象/生成予定 ASIN に限定 (全カタログ巡回しない)、日次上限あり
4. **原文はランナーのローカル保管のみ (リポジトリにコミットしない)**。リポジトリと
   サイトに出るのは gemma で言い換えた集合傾向 (`usable_as: paraphrase`) と
   評点分布の統計のみ。§5.D 集合声ルール・著作権の両面をクリアする

### 5.3 パイプライン (amazon-home-ops に `23-experience-mining.yml`)

1. 対象 ASIN 選定: answerability_audit 対象 + rewrite_queue + 新規生成予定。日次上限
   (初期 20 ASIN)
2. Lane 1 各アダプタでソース取得 (アダプタは環境変数の secret 有無で個別に有効化、
   無い lane は warning + skip で止めない)
3. gemma で抽出: 本文/投稿 → `{aspect (体験談/比較/安全/シーン/不満), text (60〜160 字),
   source_type, source_url, usable_as (quote|paraphrase), confidence}`。**商品一致検証**
   (本文中に商品名/ブランドが実在するか entailment 判定) を通過したものだけ採用
4. `data/raw/per_asin/<ASIN>/experience.json` に出力 → data PR + auto-merge
   (04-validate paths と .gitignore whitelist の登録を忘れない —
   04-validate-paths-inverse-trap)
5. usable_as の割当: EC レビュー由来 (yahoo_review_aggregate)・antigravity (agy) 要約 =
   `paraphrase` 固定。Tavily/news の公開記事・Threads 公開投稿 = 出典明記の短引用可
   (`quote`)

必要 secret (ランナー .env / repo secret): `THREADS_ACCESS_TOKEN` (既存流用・
threads_keyword_search 権限の付与状況は初回実行で要確認)。Tavily は既存 URL の再利用
のため新規キー不要。agy (Antigravity CLI) の認証は secret ではなくファイルベース
(K8 WSL2 ホスト側で owner が事前に手動ブラウザ認証済み) のため新規 GitHub Secret は
不要 (詳細は §5.1「agy 運用上の注記」)。

### 5.4 プロンプト側の接続 (v7.1)

- build_jules_prompt は per_asin/*.json を全同梱するため experience.json は自動同梱
- PROMPT_TEMPLATE §6.5.4 のレビュー引用規則を改訂: 引用素材の優先順位を
  experience.json > 組み込み検索 とし、`usable_as: quote` は「〜という使用レポートが
  あります (出典)」形式の短引用可、`usable_as: paraphrase` は「〜という声が複数
  見られます」型の集合表現のみ許可 (原文再現・「実際の購入者から」の断定は不可) と
  明文化
- 体験談要求の強化 (「daily_use に experience 由来の具体記述を最低 2 件」等の件数規律)
  は experience.json の充足率を 1〜2 週間観測してから決める (供給が無いのに規律だけ
  強化すると v6 の轍を踏む)
- experience.json の `source_type: "antigravity"` (旧 `"gemini_grounded"`) の扱いは他の
  quote/paraphrase 分類と同じ (`paraphrase` 扱い) であり、本節の規律に変更なし

## 6. Phase 3 設計: 凡庸度の定量監査

- K8/Ruri (canonical 埋め込みレーン) で新記事 narrative 全文の embedding を取り、
  既存全記事との max cosine 類似度 + コーパス重心との類似度を算出
- `data/analytics/uniqueness_audit.json` に週次出力、閾値超え (例 max-sim > 0.95) は
  analytics issue にコメント (二重起票しない・#2995 の監査コメント方式に準拠)
- 目的: v7 施行の効果測定 (施行前後で分布が下がるか) と、リライト対象選定の新シグナル

## 7. 実施順序とリスク

1. Phase 1 (本 PR): テンプレ v7 + gate + prompt builder + レンダリング + テスト
2. 施行後 1 週間: 新規生成記事の how_to_choose 品質を owner 目視 + gemma 監査で確認
3. Phase 2 実装 (amazon-home-ops 側が主) → 供給率観測 → v7.1 で引用規律強化
4. Phase 3 実装 → v7 効果の定量確認

リスクと対策:

- **比較解禁によるハルシネーション再発** → 言及可能商品を同梱データに限定 + gate の
  ASIN 封じ込め検査 + competitors 空時は固有名詞禁止
- **既存記事 PR が新 gate で落ちる** → 施行日ゲート (slug 日付) で遮断
- **プロンプト肥大** → 追加は注記 2 ブロック (比較許可・監査不足観点) のみ。
  competitors.json は元々同梱済みで増分ゼロ
