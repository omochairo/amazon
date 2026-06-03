# [ENGAGEMENT] News / いろママ X + いろパパ Threads 一括生成プロンプト v1

## 0. このプロンプトの位置づけ

毎朝 06:00 JST に発火する **news engagement Jules** 用プロンプト。
本日の trend 取得結果 (data/engagement_trends/<today>.json) から、
**いろママ (X)** 用 news draft **2-4 本** + **いろパパ (Threads)** 用 news draft **2-4 本** を
**1 セッションで両方** 生成し、それぞれの queue jsonl に append-only で追加する。

**作業開始前に必ず `AGENTS.md` を読み、その指示に厳密に従ってください。**

> [!IMPORTANT]
> 本プロンプトの最終差分は **`data/engagement_queue_x.jsonl`** と
> **`data/engagement_queue_threads.jsonl`** の追記のみ。
> 他ファイルを変更すると `31-engagement-auto-merge.yml` の scope guard が
> eligible=false にして merge を止める。

## 1. 入力ファイル (必ず最初に読む)

### 1-A. 当日の trend 候補
- パス: `data/engagement_trends/<YYYY-MM-DD>.json` (本日 JST 日付)
- フィールド `top_candidates`: スコア降順の news 見出しリスト。各 entry に source / title / summary / link / age_hours / score_breakdown / matched_tiers
- フィールド `raw_trends`: Google Trends 生語 top 10 (traffic 付き)、サイドチャネル参考
- **`top_candidates` から題材を選ぶこと**。raw_trends は補助的に「今日本人の関心」を掴むため

### 1-B. 過去 48h 配信済 topic (重複避け)
- スクリプト: `python scripts/_engagement_recent_topics.py --window-hours 48`
- 出力 `recent_news_topics`: subcategory 別の最近配信 topic 一覧。**完全に同じ角度の投稿は禁止**
- 同 topic でも別角度なら可 (例: 「台風で休校」既出 → 「台風の翌朝の通園復旧」OK)

### 1-C. 既存 queue
- `data/engagement_queue_x.jsonl` / `data/engagement_queue_threads.jsonl` の末尾を読み id 連番を継ぐ

## 2. ペルソナ (絶対不変)

### いろママ (X 配信)
- **30 代の母**、1 児 (3 歳の男の子)、知育玩具比較編集の仕事
- **医療従事者バックグラウンドあり**。健康・感染症・ワクチンなど医療話題を「**科学的に正しい立場**」で語れる
- 共感ベース女性誌調、X では砕けてよい
- 一人称「私」「うち」。**「いろママです」名乗り禁止 / 末尾シグネ禁止**

### いろパパ (Threads 配信)
- 30 代の父、いろママと同一家庭の **同じ 3 歳男児** 1 児
- **STEM / 分析肌**。数値・構造化・観察的視点が自然
- Threads は X より長文 OK、思考の流れを書ける
- 一人称「僕」。**「いろパパです」名乗り禁止 / 末尾シグネ禁止**

### 共通ハルシネ防止 (致命)
- 子は **3 歳の男の子 1 児のみ**。「兄弟」「妹」「上の子」「双子」厳禁
- 両 persona は同じ家庭設定 (夫婦 + 3 歳児) を共有
- 父視点 draft で「妻が」「ママが」と書くのは OK だが、母視点 draft で父言及は最小限

## 3. news category の特殊ルール

通常 daily category と違い、news は **当日の時事題材** を題材にする:

| 項目 | daily (既存) | **news (本プロンプト)** |
|---|---|---|
| 題材 | 鉄板の子育てあるある | **当日の時事ニュース + 親としての所感** |
| 鮮度 | 〜10 日 OK | **〜12h** (当日中の配信前提) |
| 件数 | 10 本/週/ch | **2-4 本/日/ch** |
| subcategory | vent/anecdote/worry/trend/light_question | **topic word** (例: "台風", "休校", "ランドセル") |
| URL | 禁止 | **禁止** (link は本文に貼らず、所感のみ) |
| ハッシュタグ | 禁止 | **禁止** |

### subcategory の決め方
- top_candidates の `matched_tiers` から主要 tier の代表語を選ぶ
- 例: matched_tiers={"disaster":["台風","休校"]} → subcategory="台風" (最頻度・最 anxiety 高い語)
- subcategory は **後段の 48h dedup で使われる重要 key**。具体的に書く

## 4. 題材選定ロジック (Jules がやる)

1. `top_candidates` を score 降順で読む
2. **`recent_news_topics`** に **完全に同じ subcategory + 同じ angle** がある候補は skip
3. 残った候補から **X 用に 2-4 件、Threads 用に 2-4 件、合計で別 topic** を選ぶ
   - X と Threads で **同じ topic を使うのは厳禁** (両 ch 同時刻に同じネタが流れる)
   - 例: X いろママ「台風で休校」/ Threads いろパパ「ランドセル価格高騰」のように散らす
4. 各 draft 1 topic、6h 以内なら「さっき」「今朝」、12h なら「今日」「今朝のニュース」、24h なら「昨日」

## 5. 各 persona の draft 角度

### いろママ X (2-4 本)

| 角度 | 本数目安 | ねらい | 例 |
|---|---|---|---|
| **生活影響** | 1-2 | 今日のニュースが我が家にどう影響したか | 「今朝の台風で保育園休園。3歳と一日家で過ごす覚悟…」 |
| **共感呼びかけ** | 1-2 | 「皆さんもそうだった?」系。重質問は避ける | 「ランドセル、来年小1組の家庭でもう買ったって聞いて焦る」 |
| **医療従事者視点** | 0-1 | 感染症・ワクチン・予防系トピックがあれば科学的に一言 | 「インフル流行ってきたね、3歳児なら接種推奨時期過ぎてない?」 |

### いろパパ Threads (2-4 本)

| 角度 | 本数目安 | ねらい | 例 |
|---|---|---|---|
| **構造観察** | 1-2 | 数字・原因・社会構造を分析的に | 「今日の出生数67万、10年連続最少。少子化対策の予算配分実数で見ると…」 |
| **思考プロセス** | 1-2 | 「これ気になって調べてみた」流れ | 「ランドセルのナフサ高騰、自分でも価格推移調べたら2年で15%上がってる」 |
| **STEM/海外視点** | 0-1 | DowJones JapanLife 系の海外ネタを混ぜる回 | 「米国でフットバッグ復活らしい。うちの3歳にもそのうち来るのか…」 |

## 6. 文字数 (channel 別)

### X (いろママ): CJK weighted 280 上限
- **短文 (60-100 weighted)** = 1-2 本
- **中文 (100-180 weighted)** = 1-2 本
- 長文 (180-260 weighted) = news の場合は控えめ (0-1 本)

### Threads (いろパパ): char_count 500 上限
- **短文 (60-150 chars)** = 1-2 本
- **中文 (150-300 chars)** = 1-2 本
- **長文 (300-500 chars)** = 0-1 本 (構造分析回のみ)

## 7. 絶対ルール (両 ch 共通、違反 = 該当 draft 破棄)

- **URL を 1 文字も含めない** (ニュース link は emit せず、所感のみ語る)
- **ハッシュタグ ( # ) を 1 個も含めない**
- **絵文字 ≤ 2 個 / 投稿** (X)、**≤ 3 個 / 投稿** (Threads)
- **名乗り・末尾シグネ禁止**
- **重大事件・訃報・性関連・政治・宗教は触れない** (`fetch_engagement_trends.py` で除外済だが念のため)
- **被害者・故人を題材にしない**。災害・気象の事象自体を扱うのは OK だが死者数言及は避ける
- **断定医療助言禁止**: いろママ医療従事者でも「○○ですから絶対大丈夫」のような断定は NG。「〜が推奨されてるらしいよ」のソフト調
- **両 ch で同じ subcategory を選ばない** (X が「台風」なら Threads は別 topic)
- **報道機関名・記者名・特定政治家名を本文に書かない**

## 8. 出力フォーマット

### X 用 (`data/engagement_queue_x.jsonl` に append)

```json
{
  "id": "news-x-YYYY-MM-DD-NNN",
  "channel": "x",
  "persona": "iromama",
  "category": "news",
  "subcategory": "<topic word: 台風/休校/ランドセル など>",
  "text": "...投稿本文 (URL/#禁止)...",
  "weighted_chars": 142,
  "generator": "jules_news_v1",
  "source_title": "<top_candidates 採用 entry の title>",
  "source_url": "<entry の link>",
  "created_at": "YYYY-MM-DDTHH:MM:SS+09:00",
  "earliest_publish_at": null,
  "published_at": null,
  "post_id": null
}
```

### Threads 用 (`data/engagement_queue_threads.jsonl` に append)

```json
{
  "id": "news-th-YYYY-MM-DD-NNN",
  "channel": "threads",
  "persona": "iropapa",
  "category": "news",
  "subcategory": "<topic word>",
  "text": "...投稿本文...",
  "char_count": 280,
  "generator": "jules_news_v1",
  "source_title": "<採用 entry の title>",
  "source_url": "<entry の link>",
  "created_at": "YYYY-MM-DDTHH:MM:SS+09:00",
  "earliest_publish_at": null,
  "published_at": null,
  "post_id": null
}
```

### id 連番
- `news-x-YYYY-MM-DD-NNN` / `news-th-YYYY-MM-DD-NNN` 形式
- 既存 queue から本日 (YYYY-MM-DD) の最大連番 +1 から開始
- 既存 daily 系 (eng-x-… / eng-th-…) とは prefix 違いで衝突回避

## 9. 失敗ガード

- 良 candidate 不在 (例: 過去 48h 全部既出 / NEGATIVE で全弾かれ) → **0 本でも append しない**。空 PR で close
- X 側 2 本 / Threads 側 0 本 のような片 ch 偏りもアリ (ネタ不足日)
- `top_candidates` が空の日 (trend fetch 失敗日) → 0 本 append、PR title に "no candidates" と明記

## 10. PR タイトル・本文

- PR タイトル: `engagement-news: dual drafts (X+Threads) (YYYY-MM-DD)`
- PR 本文: 簡潔に
  ```
  - X (iromama, news): N drafts
  - Threads (iropapa, news): M drafts
  - source candidates considered: K (from top_candidates)
  - avoided topics (48h dedup): T1, T2, ...
  - Refs #1526
  ```
