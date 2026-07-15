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
| 2 | 体験談供給レーン (K8 gemma + YouTube 字幕 + third-party 本文マイニング) | amazon + amazon-home-ops | 設計のみ (§5) |
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

### 5.1 データソース (規約準拠のもののみ)

| ソース | 取得方法 | 含まれる体験情報 |
|---|---|---|
| YouTube レビュー/開封動画の字幕 | YouTube Data API (search, 無料 10k units/日) + 字幕取得 | 実使用の観察・不満・遊び方シーン (最有力) |
| third_party_sources の本文 | 既存 #1600 Tavily URL を K8 側で本文 fetch | ブログ/メディアの使用レポート |
| per_asin/news.json の本文 | 同上 | 受賞・イベント・体験会の記述 |

Amazon/楽天レビューのスクレイピングは ToS 違反のため**採用しない**。

### 5.2 パイプライン (amazon-home-ops に `23-experience-mining.yml`)

1. 対象 ASIN 選定: 翌日の生成予定 (新規 + rewrite_queue) + answerability_audit 対象
2. YouTube 検索 (`{商品名} レビュー|開封|遊んでみた`) → 上位動画の字幕取得
3. gemma で抽出: 字幕/本文 → `{aspect (体験談/比較/安全/シーン), text (60-120字要約),
   source_url, source_type, confidence}`。**商品一致検証** (字幕中に商品名/ブランドが
   実在するか entailment 判定) を通過したものだけ採用
4. `data/raw/per_asin/<ASIN>/experience.json` に出力 → data PR + auto-merge
   (04-validate paths と .gitignore whitelist の登録を忘れない —
   [[04-validate-paths-inverse-trap]])

### 5.3 プロンプト側の接続 (v7.1)

- build_jules_prompt は per_asin/*.json を全同梱するため experience.json は自動同梱
- PROMPT_TEMPLATE §6.5.4 のレビュー引用規則を改訂: 引用素材の優先順位を
  experience.json > 組み込み検索 とし、「experience.json の text は出典 URL 付きで
  『〜という使用レポートがあります (出典)』形式で使える」と明文化
- 体験談要求の強化 (「daily_use に experience 由来の具体記述を最低 2 件」等の件数規律)
  は experience.json の充足率を 1〜2 週間観測してから決める (供給が無いのに規律だけ
  強化すると v6 の轍を踏む)

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
