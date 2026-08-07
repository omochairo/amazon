# 記事生成・スコアリング 設計判断集

`build_post.py` / スコアリング / フィルタ・ゲート周りで確定した設計判断とその理由（WHY）。
この設計を変更する提案が出たら、まずここを読んで過去に同じ理由で却下されていないか確認する。

## build_post.py は外部APIへlive HTTPを書かない
描画（Jinja2テンプレ+データ整形）に専念させ、データ取得は必ず
`scripts/fetch_*.py` + `_fetch_targets` パターンに分離する。

**Why**: Issue #674で`_attach_omcha_related()`が描画ループ内で218ASIN分live HTTPし、
24h TTLキャッシュをtracked dirに書いていた。結果: (1) untracked汚染（.gitignore漏れで
134件の常時untracked）、(2) 描画レイヤI/O（外部API依存でCI失敗のattack surface拡大）、
(3) score_calculatorのrace（ファイル無→0点のsilent fail）が同時発生。PR #675で
`fetch_omcha_related.py`に切り出して一括解消した。

**How to apply**: 将来の外部API統合は必ず以下の順で実装する:
1. `scripts/fetch_<source>.py` 新規作成（既存`fetch_youtube.py`/`fetch_omcha_related.py`
   をテンプレに）
2. `_fetch_targets.load_state` / `mark_queried` でstale-first 50/run × 7d cycle実装
3. 出力は`data/raw/per_asin/<ASIN>/<source>.json`（tracked、`_fetch_state.json`の
   `<source>`entryで管理）
4. `.github/workflows/01-fetch-products.yml`に`if: always()`step追加
5. `build_post.py`/`score_calculator.py`は**読むだけ**（live HTTP禁止）

build_postにin-band呼び出しを足したくなったら、一旦止まってfetch層への分離が可能か検討する。

## cross-search誤マッチの品質評価はgate後の最終出力を見る
`data/raw/{rakuten,yahoo}_matched.json`は**gate前の生候補**で、無関係な高額商品が
`_match_method:"text"`で混ざっていることがあるが、`build_post._matched_passes_quality`
（実amazon価格でのトークン被覆+価格帯+型番ガード）が**load-bearingな防御**で大半を弾き、
`is_search:true`（検索リンク・価格非表示）にfallbackさせる。

「誤マッチが表示されている」系の指摘を検証するときは matched.json ではなく実レンダー
`data/articles/*.json`の`product.prices`を読み、`prices.{rakuten,yahoo}`が実価格か
`is_search:true`かを確認してから判断する（実例: 指摘された誤マッチのうち大半は既にgateで
除外済みで実害が再現しなかった）。

gate（catch側）を盛る前に、まずsource側（キーワード抽出の質、JAN直引き率）を疑う —
誤マッチの上流原因はほぼキーワード抽出のゴミ化。gateロジックを触るdry-runツールは
`_matched_passes_quality`を閾値引数付きで委譲呼びし、手コピーによるロジックdriftを
構造的に排除する。

## 知育スコア v2 の設計原則
配点式を変更するときはこのルールを尊重する:

1. **tier floorは下げない** — 「玩具メーカーである以上、知育・安全が0はあり得ない」という
   確定要望。`safety_cert`はS=10/A=9/B=8/C=6/D=0、`edu_value`はS=5/A=4/B=3/C=2/D=0が
   floor。具体的な認証（ST/CE）は加点ボーナス+1のみ
2. **海外ブランドにはmedia/marketのfloor 5を保証** — 海外有名ブランドは日本語YouTube/
   news/books・楽天/Yahooマッチが少ないのが普通で、これをpenaltyにすると不当に下がる。
   `is_overseas = brand.region not in ("JP", "unknown", "")` で判定し、region新tier
   （SE/DE/FR等）が増えてもJP/unknown以外はoverseas扱い
3. **4軸表示(/5)は2.0-5.0スケール（中央3.5）** — 「玩具で0.0はあり得ない」「3.5を基準に
   上下に振る」という要望。`axis = 2.0 + (raw / max) * 3.0`
4. **最終マップ係数は0.4（raw max 100）** — 旧0.5+raw max110ではcap100が頻発し上位解像度
   が潰れた。`final = max(50, min(100, round(50 + raw * 0.4)))`。raw maxを再び増やすなら
   係数を再調整する。配点項目を増減する際はraw max合計が100を維持できるか確認する
5. `product.ivs_score`（Jules元値）はfrontmatter `ivs_score_jules`に保持（デバッグ用）。
   `build_post.py`の`_sync_ivs_for_render()`は必ず`template.render()`の直前に呼ぶ
   （順序を変えると本文が古いJules値で出る）

## コピーライティング指針（機能/特集ページ）
`/cospa/` `/deals/` `/ranking/` 等のtitle/description/本文ledeは以下に従う:

1. **読者の悩みから始める** — ターゲット読者（20-40代女性、ギフト購入者多い）の生の声や
   問いから書き出す。「機能説明→効能」の順はNG
2. **作り手都合の理由は出さない** — 「データが偏るから分けた」等は内部README/PR本文に
   書く。ユーザー向けには「あなたの予算/シーンに合うものが見つかる」とだけ言う
3. **購入動機ロングテール語を構造化マークアップで網羅** — 誕生日/出産祝い/入園祝い等を
   `<table>`や`<ul>`で含める。本文散文より構造化マークアップの方が検索意図マッチに強い
4. **タブ/UIと情報を重複させない** — UI自明な情報は本文から落として情報密度を上げる
5. **信頼シグナルを1行で** — 件数・スコア軸数・更新頻度・ロジック公開等を1文に圧縮
6. **「こんな方におすすめ」セクションを置く** — 3-4項目、各項目は具体シーン+動機ワード入り

## article.schema.json の本文系minLengthは「下限の安全網」であって「目標値」ではない
narrative/faq.answer/editorial_comment等の本文系`minLength`をJules prompt側で文章の
最低字数として強制すると、文体が単調化する。Jules自然生成は大半40-80字に収まり、たまに
15-30字が混ざるのは決め台詞・問いかけとして**むしろ良い**（ユーザーfeedback:
「毎回短いなら問題ですが、時々短いのが紛れるくらいが自然でちょうどいい」）。

**How to apply**: 本文系schema minLengthを厳しくする提案には反対する（現行floor 15を
維持）。Jules promptに「最低N字以上」系のuniform強制ルールを足さない。重要部
（title=20/lead=120/meta_description=100/editorial_comment）は別物 — これらはprompt強制
&schema floor両方で守る対象のまま。

## 許可リストにキーを足すときは過去の除外対象と照合する
フィルタの誤検知を「許可リストに分類キー（カテゴリ/ノード/タグ）を追加」して救うときは、
追加するキーを**過去に除外した対象が持っていないか必ず照合する**。共有していた場合、
その追加は誤検知を救うと同時に過去のcleanupを静かに巻き戻す。テストが無ければ誰も
気づかない。

実例: ジャンルゲートの誤検知12件を救うためbrowse node単位の許可リストを追加しようとした
ところ、そのノードは過去に「実カテゴリ=手芸キット」を理由に削除した別ASINと同一ノード
だった。ノード許可していれば過去のcleanupを無効化し、削除済み商品がsearch sweepから
再流入していた。

**How to apply**:
- 許可リストにキーを足す前に、そのキーで過去の除外対象を検索する（`asin_blocklist.json`
  の`reason`にカテゴリ名が書いてあるのでgrep可能。削除済み記事はper_asin snapshotも
  消えるためこの記録が唯一の証拠 — だからblocklistのreasonに実カテゴリを書き残す運用に
  意味がある）
- 救済対象と除外対象が同じキーを共有していたら、キー単位の許可は諦めて**個別
  (ASIN/ID)単位の例外**に落とす
- 許可リスト追加時は全カタログへの影響範囲（何件が新たにpassするか）を実測してから
  commitする。意図した件数と一致しなければ粒度が粗い
- 分類ロジックにテストが無いまま許可リストを足さない

## 画像類似度による商品同一性判定は変種SKUを分離できない
「商品画像の類似度で同一商品かを判定する」案が出たら、**変種SKUを分離できるかを最初に
確かめる**。ほぼ確実にできないので、画像ゲートを主たる同一性判定に据えてはいけない。

CLIP ViT-B/32埋め込みで実測したところ、「別商品」のはずのペア（英検2級版 vs 準2級版）が
cosine 0.9911を記録した。級・色・容量・サイズ等のvariant SKUはパッケージ写真が意図的に
同一デザインなので、画像埋め込みでは**原理的に**分離できない。これは閾値較正で解決しない
— 同一商品クラスタと別商品クラスタが重なっている以上、閾値をどこに置いてもvariantは通る。

**How to apply**:
- 商品同一性の判定はexactな識別子（JAN/ISBN/型番）を取りにいく方向で設計する。画像は
  補助にしかならない
- どうしても画像を使うなら、同一商品(別カット)と別商品(同ブランド・変種含む)を明示的に
  分けたデータセットで分離性を先に検証する。「同一商品の最小sim > 別商品の最大sim」が
  成り立たなければ不採用にする
- 「ブランド+型番トークン+価格帯」のガードレールは画像で置き換えられない

採用した代替方向: 楽天リスティング→楽天カタログ（型番照合）→exact JAN→Amazon。
あいまい照合を同一エコシステム内・正規化済み商品名に閉じ込め、最終リンクはexactにする。

## 動的事実の禁止語は文脈を絞って狭める
「動的事実（時間で変化する事実）の言及は恒久禁止」を実装するとき、**素の「円」
「ポイント」を禁止語にしてはいけない**。「楕円形、円形…」は図形説明、「着目ポイント」は
要点の意味であり、素の語で弾くと図形パズル・分数教材のような「形状Q&Aがまさに価値を持つ
商品」で系統的に誤検出する（実測: 既存FAQ4,627件中12件=0.26%）。

```python
DYNAMIC_FACT_WORDS = ("価格", "最安", "在庫", "セール", "割引", "%オフ", "送料", "キャンペーン", "タイムセール")
_PRICE_NUMBER_RE = re.compile(r"[\d０-９一二三四五六七八九十百千万数][\d,，]*\s*円")
_POINT_REWARD_RE = re.compile(r"ポイント(還元|付与|進呈|アップ|バック|倍)|ポイントが?(貯ま|付く|つく)|ポイ活")
```

**How to apply**: 禁止語リストを書くときは実データに当てて誤検出率と誤検出の中身を必ず
測る。日本語は1〜2字の語が別語の部分文字列になりやすい（円/点/在/品）。測定は
`data/articles/*.json`の`faq`を全走査すれば数十秒で終わる。
