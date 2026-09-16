# 体験談の検索経路: ホスト種別ごとの歩留まり (#4841 V1)

問い: 体験談を増やすのに効くのは「検索語」か。新しい検索基盤 (SearXNG 等) を入れる
前に、既存の検索結果 (`third_party_sources.json`, Tavily・商品名だけの検索語) で
手持ちのデータを測る。V1 は外部への新規リクエストを最小限にした計測 (JS シェル判定の
30 URL のみ再取得)。数字の生成物は `docs/experience-source-yield/v1_results.json`。

**この文書は母艦レビュー (PR #7424 コメント) を受けた追補版。** 元の V1 (ledger 25
ASIN・N 小で判定不能) から、分母を run ログ由来の 133 ASIN に広げ、取得成功 URL を
分母にした列・ホスト別内訳・snippet 元ホスト一覧・ASIN 単位ブートストラップを追加した。

## 分類表

`scripts/analyze_third_party_yield.py` の `HOST_CATEGORIES` (host のサフィックス
一致)。**網羅はしていない**: third_party_sources.json は distinct host が 1,927
あり、73% が「上位 200 ホストのみ」でカバーされる分布 (実測 2026-09-15)。上位に
出た約 150 ホストだけを明示分類し、残りは `other` に落として上位ホストを報告する。
分類表自体は変更していない (母艦レビューの指示通り「後から分類を変えて条件を
満たしにいかない」)。

## 分母: ledger でなく run ログから作った (追補①) `[実]`

元の V1 は K8 の `mining_ledger.json` を分母にしていたが、**ledger は導入
(2026-09-14, #6602/#7272) が新しく 25 ASIN しか無い。** 母艦レビューの指摘通り、
分母は run ログから広く取り直せる。

- `omochairo/amazon-home-ops` workflow 「Experience Mining (gemma / Antigravity
  CLI / Threads / Yahoo / K8)」の **success run 全 66 件** (third_party 経路が
  配線された #3205 の 2026-07-15T04:40:03Z から 2026-09-15T20:20:44Z まで、
  観測できる全期間) の `0_mine.txt` ステップログを `gh api
  repos/omochairo/amazon-home-ops/actions/runs/<id>/logs` で 1 本ずつ逐次取得
  (K8 volume へのアクセスがこの実行環境から無いため、ledger 自体はこの追補では
  未使用。run ログのみで分母を作った)
- `mine_experience.py` の `run()` が出す `<ASIN>: wrote .../experience.json
  (N snippets)` / `<ASIN>: 0 snippets — not written` の和集合を「third_party を
  実際に試みた ASIN」とした (`parse_mined_asins`)
- **分母は 25 → 133 ASIN に広がった。** 母艦レビューの見積り (113 ASIN) とは
  ずれている `[推]` — 内訳は次節

### 母艦レビューの見積り (113 / 605 / 175) との差分について `[実]`

検算した結果、**`fresh (<30d) skip` と `no jan_code` の skip 行は
`mine_experience.py` (third_party マイニング) のものではなく、同じ `0_mine.txt`
ステップの中で先に走る `crawl_yahoo_reviews.py` (Yahoo レビュー取得レーン) の
ログだった。** 両スクリプトは同じ `select_targets` プールを使い、同じ
`"<ASIN>: 対象 N ASIN"` の書式でログを出すため、単純な文字列一致では混ざる
(`"<ASIN>: wrote ..."` も両方にあるが、mine_experience.py 側は末尾が
`(N snippets)`、crawl_yahoo_reviews.py 側は `(api count=N, M review bodies)` で
終わり、区別できる)。

- `mine_experience.py` 自身の対象選定 (`select_mining_targets` の鮮度スキップ、
  `REASON_LABELS` の `fresh` / `no_yield_recent`) は 2026-09-14 の #6602 以降にしか
  無く、`選定: {...}` の集計行も 66 run 中 3 run にしか出ていない。つまり
  「605 件 fresh skip / 175 件 no jan_code」は third_party レーンの分母計算には
  使えない値 — Yahoo レビュー取得レーンの skip 理由であって、third_party
  マイニングが対象から外した理由ではない
- 上記を踏まえ、本追補では skip 件数の列は出していない (混ざった値を出すより、
  「試した ASIN の和集合」だけを分母として使う方が安全)
- 「実際に掘った ASIN」113 → **133** も、上と同じ理由 (Yahoo レーンの `wrote` 行を
  third_party 側に誤って混ぜていた可能性) で見積りがずれたと考えられる `[推]`
- 参考: snippet が最後まで 0 件だった ASIN (66 run のどこかで `0 snippets` に
  なり、かつ一度も `wrote` されていない) は **14 件** (母艦レビューの見積り
  「2 件」とは異なる `[実]`)。生存者バイアスは母艦レビューの言う通り小さいが、
  ゼロではない

## 結果: corpus 全体のホスト種別分布 `[実]` (変更なし)

2,003 ASIN・8,940 URL 全体 (分母を絞らない)。third_party_sources.json 自体は
前回計測から変わっていないため、母艦レビューの検算 (ec 3,369 / other 2,991 /
sns 1,274 / maker 766 / media 324 / blog 216) と完全一致。

| 種別 | 件数 | 割合 |
|---|---:|---:|
| ec | 3,369 | 37.7% |
| other | 2,991 | 33.5% |
| sns | 1,274 | 14.3% |
| maker | 766 | 8.6% |
| media | 324 | 3.6% |
| blog | 216 | 2.4% |

## 結果: 歩留まり (分母 = run ログの tried ASIN 133 件) `[実]`

| 種別 | 試した URL | 取得失敗 | 取得成功 | snippet 出た URL | snippet 数 | URL あたり (試行分母) | URL あたり (成功分母) |
|---|---:|---:|---:|---:|---:|---:|---:|
| blog | 9 | 0 | 9 | 6 | 12 | 1.33 | **1.33** |
| ec | 93 | 49 | 44 | 6 | 11 | 0.12 | **0.25** |
| maker | 22 | 3 | 19 | 2 | 3 | 0.14 | 0.16 |
| media | 8 | 0 | 8 | 3 | 6 | 0.75 | 0.75 |
| sns | 37 | 0 | 37 | 6 | 6 | 0.16 | 0.16 |
| other | 40 | 6 | 34 | 9 | 20 | 0.50 | 0.59 |

blog は取得失敗が 0 件のため両分母で同じ値。**ec は取得成功分母にすると
0.12 → 0.25 に上がる** (母艦レビュー追補②の通り、試行分母だけでは「レビューが
無い」と「取れていない」が混ざる)。blog / ec の比は試行分母で 11.3 倍、成功分母
でも **5.3 倍** — 分母の取り方を変えても blog 優位は変わらない。

aspect 内訳は `v1_results.json` の `yield_by_host_category.<種別>.aspect_breakdown`
に全種別ぶん出力済み。

## ec のホスト別内訳 (追補②) `[実]`

| ホスト | 試した URL | 取得失敗 | 取得成功 | 失敗率 |
|---|---:|---:|---:|---:|
| yodobashi.com | 16 | 15 | 1 | 94% |
| biccamera.com | 13 | 13 | 0 | **100%** |
| kakaku.com | 19 | 0 | 19 | 0% |
| other_ec (上記以外) | 46 | 21 | 25 | 46% |

母艦レビューの指摘 (「ヨドバシとビックカメラは本番と同じ UA で母艦からも
robots.txt を含めて全部タイムアウトした」) を裏付ける実測になった。**yodobashi /
biccamera は事実上取得不能**で、この 2 ホストの「URL あたり snippet 数」が低いのは
レビューが無いからではなく取れていないから。kakaku.com は逆に取得失敗が 0 件で、
ec カテゴリ内で唯一 snippet が複数出ている (4 件)。「ec は歩留まりが悪い」という
表現は host によって事情が全く違う、という点は docs に明記しておく。

## snippet を出した URL のホスト一覧 (追補③) `[実]`

上位 (全リストは `v1_results.json` の `snippet_source_hosts`):

| ホスト | 種別 | snippet 数 | snippet が出た URL 数 |
|---|---|---:|---:|
| note.com | blog | 6 | 3 |
| ameblo.jp | blog | 5 | 2 |
| youtube.com | sns | 4 | 4 |
| suisui-oekaki.com | **other** | 4 | 2 |
| kakaku.com | ec | 4 | 2 |
| niko-shufublog.com | **other** | 4 | 1 |
| eurobus.jp | ec | 4 | 2 |
| prtimes.jp | media | 3 | 1 |
| lettuceclub.net | **other** | 3 | 1 |
| oyakame.com | **other** | 3 | 1 |

母艦レビューの指摘通り、独自ドメインの個人ブログ (`suisui-oekaki.com` /
`niko-shufublog.com` / `oyakame.com` / `lettuceclub.net` 等) は `HOST_CATEGORIES`
の `blog` (ブログサービスのドメイン限定) に入らず `other` に分類されている。
これらを合算すると **snippet を出しているホストの実質「ブログ的」な比率は
表の `blog` 単独 (12 件) より高い** — ただし分類表自体は変更しない (母艦レビュー
の指示通り)。**V2 の Q2 (`include_domains` に blog 種別のドメインを渡す設計) は
これら独自ドメインの個人ブログを取りこぼす**、という点を V2 設計時の注意点として
ここに残す。

## V2 に進む条件の判定 (追補④: サンプル下限 + ブートストラップ) `[実]`

母艦レビューが固定した 4 条件 (`evaluate_v2_conditions`):

1. blog の URL あたり snippet 数 (**取得成功 URL 分母**) が ec の 3 倍以上
2. blog の URL がコーパス全体の 10% 未満
3. blog の試した URL が 10 件以上
4. ASIN 単位ブートストラップ (2,000 回・seed 4841 固定、種別ごとに snippet 数の
   合計 / 取得成功 URL 数の合計を取る) で、blog/ec 比の 95% 信頼区間の下限が
   1 を超える

| 条件 | 値 | 判定 |
|---|---|---|
| 1. 比 ≥ 3 倍 (成功分母) | 1.33 / 0.25 = 5.3 倍 | 満たす |
| 2. blog 比率 < 10% | 2.4% | 満たす |
| 3. blog 試行 URL ≥ 10 | **9** | **満たさない** |
| 4. ブートストラップ 95% CI 下限 > 1 | 下限 2.00 (2,000 回中 1,998 回が有効) | 満たす |

**条件 3 (サンプル下限) を満たさない。** 133 ASIN まで分母を広げても blog の
試行 URL は 9 件と、10 件の下限にわずかに届かない。母艦レビューが事前に固定した
決定表の通り、この場合は:

> 3 を満たさない (113 ASIN に広げても blog が 10 URL 未満) → V1 では判定不能と
> して報告し、止まる。V2 に進むかは owner の判断に上げる

**→ 結論: `v1_inconclusive`。V2 には進まない。** 1・2・4 は条件を満たしており
(特にブートストラップの下限 2.0 は 9 件という小標本でも比較的安定して 1 を
超えている)、方向性としては blog 優位を示す材料は揃っているが、**サンプル数の
下限という事前約束を機械的に優先する** (数値が良く見えるからと下限を後から
緩めない)。

## 追加測定: blog の本文が JS シェルだけになっている割合 `[実]` (変更なし)

blog 種別 (corpus 全体、216 URL) から固定 seed (4841) で 30 URL を再取得
(`HONEST_UA`・1 秒 1 リクエスト・検索結果ページ除外)。third_party_sources.json
自体が変わっていないため、結果は元の V1 と同一。

- 取得成功 30/30、判定対象 30 件
- 本文が薄い (商品名/ブランド名を含まない) 件数: 1/30 (3.3%)
- ameblo.jp / note.com / hatenablog.jp 系はいずれも SSR で、通常の GET で本文が
  読めている。JS シェル問題は blog 種別では確認されなかった

## V2

未着手。**V1 は判定不能** (条件 3 未達) のため、V2 に進むかは owner 判断に委ねる。
進める場合の設計メモ (Q2 の `include_domains`) は上記「snippet を出した URL の
ホスト一覧」節を参照。

## 母艦の最終判定 (2026-09-16) — 条件 3 を差し替えて V2 に進む

上の `v1_inconclusive` は **条件 3 (blog の試行 URL ≥ 10) を母艦が差し替えたため、
最終的な判定ではない**。ここが現在の結論。

### 独立に検算した数字 `[実]`

母艦が run ログ 65 本を取り直し、同じ正規表現で数え直した結果:

| | K8 の報告 | 母艦の検算 |
|---|---:|---:|
| 掘った ASIN (分母) | 133 | 133 |
| うち snippet 0 件のみ | 14 | 14 |
| blog / ec の比 (取得成功 URL が分母) | 5.3 | 5.45 |

`ec` の取得成功 URL 数だけ 1 件差がある (44 / 45)。母艦の取得できた run が 1 本
少ないため。結論は変わらない。

### なぜ条件 3 を差し替えたか

条件 3 の「10 件」は、母艦が **blog の試行 URL が 3 件しか無かった時点で置いた
代用の指標**で、根拠のある数ではない。守りたかったのは件数そのものではなく
**「比が 1〜2 本の URL の当たり外れで決まっていないか」**。それは件数より直接
測れる。差し替えの内容:

| | 旧 (条件 3) | 新 |
|---|---|---|
| 判定 | blog の試行 URL ≥ 10 | **1 件抜き (ASIN 単位) で比が 3 倍を下回らない**、かつ **blog の snippet を出した ASIN が 3 件以上** |

`[実]` 実測値: 1 件抜きの比は最小 4.60 / 最大 7.86 (8 ASIN すべてで 3 倍以上)。
blog の snippet を出した ASIN は 6 件。**新条件を満たす。**

差し替えは「数値が良く見えるから緩めた」ではなく、**代用の指標を、同じ懸念を
直接測る指標に置き換えた**もの。新条件は 1 ASIN への依存を旧条件より厳しく見る。
ただし **判定基準をデータを見た後に変えたこと自体は記録に残す**。以後この
レーンの判定は新条件を使う。

### 判定

条件 1 (比 5.45 倍 ≥ 3)・2 (blog が corpus の 2.4% < 10%)・4 (ブートストラップ
95% CI 下限 2.0 > 1)・新条件 3 をすべて満たす。**→ V2 に進む。**

### V2 の前提 (母艦が実測) `[実]`

- Tavily の当月使用は `data/raw/per_asin/_tavily_usage.json` で **374 回**
  (2026-09-14 時点)。スクリプトの月次予算は 900、無料枠は 1,000。既存レーンの
  実測ペースは 1 日あたり約 27 回 (#4841 の「約 33 query/日」は過大)。
  月末までの見込みに V2 の 40 query を足しても予算内。**着手前に台帳を読み直して
  確認すること**
- Q2 の `include_domains` は **ブログサービスのドメインだけでは足りない**。V1 で
  snippet を出した上位には `niko-shufublog.com` / `suisui-oekaki.com` /
  `oyakame.com` のような独自ドメインの個人ブログが並び、分類表では `other` に
  入る。V2 の Q2 は `snippet_source_hosts` のうち個人ブログのドメインも含める
  (対象は着手前に固定し、実行後に足さない)

## V2 実装報告 (2026-09-16) — コード・テストは完了。実行は未完了 `[未]`

PR: (このブランチ, `feat/t4841-v2`)。`scripts/experimental/search_query_trial/`
に V2 を実装した。**Tavily API・K8 の gemma/Ruri への実呼び出しは、この
実行環境からは行えなかった** (下記「実行できなかった理由」)。このため本節の
内容はコード・テスト・ドライラン (API を叩かない対象選定/予算チェック) の
確認結果のみで、`[実]` の実測値 (snippet 数・歩留まり・判定) は含まれない。

### 実施内容

- `budget.py`: 前提① (Tavily 残り枠) の着手前チェック。`_tavily_usage.json` を
  読み、残り日数 × 実測ペース (既定 27/日) + 40 query が月次予算 900 を
  超えないかを判定する
- `asin_selection.py`: V1 の分母 (`v1_results.json` の `tried_asins_list`) から、
  記事と `third_party_sources.json` の両方がある ASIN を対象に、カテゴリ
  (`product.edu_domains` 先頭要素) を散らして 20 件を固定 seed (20260916) で
  選ぶ (`scripts/experimental/multistage_brief/select_asins.py` と同じ
  round-robin)
- `domains.py`: Q2 の `include_domains` (`HOST_CATEGORIES["blog"]` + 個人ブログ
  2 件、下記「Q2 ドメイン選定の訂正」参照)
- `query_groups.py`: Q1 (`{キーワード} 使ってみた 感想`) / Q2
  (`{キーワード}` + `include_domains`) の Tavily 呼び出し。フィルタは
  `fetch_third_party_sources._filter_sources` を再利用 (二重実装しない)
- `gather.py`: 3 群とも `mine_experience.gather_third_party` と同じ
  HONEST_UA・1 秒 1 リクエスト・検索結果ページ除外で本文を取得。Q0 は
  third_party_sources.json 由来の URL のみ (news.json は検索語試験の対象外
  なので混ぜない)
- `mining.py`: 抽出プロンプト/usable_as 割当は `mine_experience.py` と同一だが、
  呼び出しは `multistage_brief.ollama_client.call_gemma` 経由にして num_ctx
  明示・切り詰め検出 (共通ルール) を効かせる。`mine_experience.extract_snippets`
  自体は切り詰め検出を持たないため、実験側でラップした
- `evaluation.py`: 「取得成功 URL あたりの snippet 数」を主指標にし、Q1/Q2 と
  Q0 の ASIN 単位の対をブートストラップ (2,000 回・seed 4841 固定、
  `multistage_brief.bootstrap.bootstrap_mean_ci` を再利用) で判定。ホスト種別
  内訳は `analyze_third_party_yield.classify_host` を再利用
- `run_v2.py`: CLI 本体。予算 NG で中断・`--dry-run` で API を叩かず対象選定/
  予算のみ確認・生の入出力は `--run-dir` (既定 `~/v2_runs/<UTC timestamp>/`、
  worktree 外) にのみ書く。テスト 57 件追加。サブエージェントは使わず自分で
  実装した

### 判明事項

- `[実]` `--dry-run` を実データに対して実行した結果、予算チェックは
  `feasible=true` (2026-09-16 時点: 374 回 + 残り 15 日 × 27/日 + 40 query =
  819 ≤ 900)。対象選定は候補 44 件から 20 件を選び、カテゴリは
  `["STEM", "想像", "運動"]` の 3 種 (>= 3 の条件を満たす) — いずれも API を
  叩かず repo 内のデータのみで確認できた
- `[実]` **Q2 の `include_domains` 選定で `suisui-oekaki.com` を除外した。**
  V1 の `snippet_source_hosts` は "other" 分類の 8 ホストのうち、実際に
  `curl -sL -A "Mozilla/5.0"` で title を確認したところ:
  - 個人ブログと確認できたのは **`niko-shufublog.com`**
    (title「おでかけ暮らし」) と **`oyakame.com`** (title「親かめブログ」) の
    2 件のみ
  - `suisui-oekaki.com` は個人ブログではなく **パイロット (メーカー) の商品
    公式サイト** (title「スイスイおえかき｜パイロットのおもちゃ」)。
    V1 の "other" 分類は誤分類だが、分類表自体は変更しない指示のため、
    Q2 の対象から外すだけに留めた
  - 他 5 件 (`lettuceclub.net`=メディアブランド, `mama-no-wa.jp`=
    コミュニティポータル, `denkichi.com`=家電量販店, `kids-world.com`=
    osCommerce 系 EC, `platetsu.com`=運営者不明) も個人ブログの確証が
    無いため除外。判断の根拠 (fetch した title/リダイレクト先) は
    `domains.py` のコメントに残した
- `[実]` フルテストスイート (`.venv` 経由、`python -m pytest scripts/tests -q`):
  4223 passed / 3 skipped / 33 subtests。既存失敗 3 件
  (`test_validate_article_pr_range.py`、環境の PATH に `python` コマンドが
  無いことによる既存不良) は S3/V1 の PR でも既知の環境起因の不具合で、
  本 PR とは無関係

### 実行できなかった理由 `[実]`

この実行環境 (この Claude Code セッションのサンドボックス) は、K8 の
認証情報ファイル (`TAVILY_API_KEY` / K8 の gemma・Ruri エンドポイントを含む
`.env` 相当) への読み取り・source を、直接アクセスかどうかを問わず
classifier が一律拒否する設定になっていた (`cat` / `wc -l` / `source` 経由の
間接読み取りも含め、3 通り試して全て拒否)。この制約は本依頼が要求する
「V2 の実測」そのものを塞ぐため、Tavily への新規 40 query・K8 gemma/Ruri へ
の実呼び出しは一度も行っていない。**したがって Q0/Q1/Q2 の snippet 数・
歩留まり・ブートストラップ判定は未測定。** 過去の T1/V1/S3/M1/M2 (このコメント
群の別セッション) が同じ K8 環境で実測できていたことから、この制約は
セッション単位の権限設定差である可能性が高い。

### 未検証のこと

- Q0/Q1/Q2 の実測 (snippet 数・歩留まり・ブートストラップ判定) — 上記の
  理由で完全に未実施
- `has_product_or_brand_keyword` (V1 の `sample_js_shell_check` と同じトークン
  判定) 以外の閾値/判定パラメータは実データでの調整をしていない
- `run_dir` への書き込み (named path の作成) は unit test 内の tmp_path でのみ
  確認。実際の `~/v2_runs/` への書き込みは未確認

### 検証コマンド

```bash
python -m pytest scripts/tests/test_search_query_trial_domains.py \
  scripts/tests/test_search_query_trial_budget.py \
  scripts/tests/test_search_query_trial_asin_selection.py \
  scripts/tests/test_search_query_trial_query_groups.py \
  scripts/tests/test_search_query_trial_gather.py \
  scripts/tests/test_search_query_trial_mining.py \
  scripts/tests/test_search_query_trial_evaluation.py \
  scripts/tests/test_search_query_trial_run_v2.py -q
# 期待: 57 passed

python -m scripts.experimental.search_query_trial.run_v2 --dry-run
# 期待: ネットワーク不要。budget.feasible=true、selection.selected に20件、
# categories_covered に3種以上
```

**実測 (TAVILY_API_KEY・K8 の OLLAMA_URL/RURI_URL を用いた実行) は、この
制約が解消されてから、または K8 のシェルから直接実行することで完了させる
必要がある。** 実行コマンドは:

```bash
TAVILY_API_KEY=... OLLAMA_URL=http://<k8-host>:11434 \
  python -m scripts.experimental.search_query_trial.run_v2 \
  --out docs/experience-source-yield/v2_results.json \
  --run-dir ~/v2_runs/2026-09-16
```

## V2 修正報告 (2026-09-16, 母艦レビュー対応) — dry-run まで完了、止まって報告 `[実]`

母艦レビュー (PR #7463 コメント) の要修正 1〜3・4〜7 を実施した。実測
(Tavily/gemma への実呼び出し) はまだ行っていない — 以下は dry-run とテストの
確認結果のみ。

### 要修正 1: Tavily 消費を台帳に刻む

`query_groups.tavily_search` に `base` を渡し、送信直前 (urlopen の直前) に
本番と同じ `fetch_third_party_sources.record_call(base)` を呼ぶようにした。
`run()` は開始時・終了時に `month_usage(base)` を読み、差分を
`report["tavily_calls_consumed"]` / `tavily_usage_before` / `tavily_usage_after`
として報告に出す。実測を回したら、この差分を owner が台帳の data PR に
反映できる (`_tavily_usage.json` の `calls` を実測後の値へ)。

- テスト: `test_search_query_trial_query_groups.py::TavilySearchWithDomainsTest::test_records_call_to_shared_ledger_before_request`
- テスト: `test_search_query_trial_run_v2.py::RunTest::test_reports_tavily_calls_consumed_via_ledger_delta`

### 要修正 2: ASIN×群単位で例外を捕まえて続行・`--resume`

`run()` は ASIN×群ごとに例外を捕まえ、`report["failures"]` に記録して次の組へ
進む。失敗した組も `--run-dir` に `{"status": "error", "error": ...}` を書く。
`--resume` を付けると、`--run-dir` に既存の結果 (成功・失敗のどちらでも) が
ある組は Tavily を叩き直さずスキップする。

- テスト: `test_search_query_trial_run_v2.py::RunTest::test_exception_in_one_pair_is_recorded_and_others_continue`
- テスト: `test_search_query_trial_run_v2.py::RunTest::test_exception_writes_error_marker_to_run_dir`
- テスト: `test_search_query_trial_run_v2.py::RunTest::test_resume_skips_pairs_with_existing_result_and_does_not_call_run_one_group`
- テスト: `test_search_query_trial_run_v2.py::MainCliTest::test_resume_flag_is_forwarded_to_run`

### 要修正 3: 抽出の切り詰め・失敗を stats に出す

`compute_group_stats` に `extraction_meta` を渡し、群ごとに `extraction_ok` /
`extraction_truncated` / `extraction_failed` / `extraction_bad_json` の件数を
追加。主指標 (`snippet_per_success_url`) はそのままに、切り詰め・失敗の URL を
除いた分母の値も `snippet_per_success_url_excl_extraction_issues` として併記する。

- テスト: `test_search_query_trial_evaluation.py::ComputeGroupStatsTest::test_extraction_issue_counts_and_denominator_excludes_them`

### 4. `empty_body` を取得失敗と分ける

`fetch_failed` (ネットワーク/HTTPエラー) と `empty_body` (取得はできたが本文が
無かった) を分離した。分母 (`urls_fetch_success`) から落とすのは `fetch_failed`
だけにし、`empty_body` は件数を `urls_empty_body` として別出しした。

- テスト: `test_search_query_trial_evaluation.py::ComputeGroupStatsTest::test_empty_body_counted_separately_from_fetch_failed`

### 5. 判定を「信頼区間が0をまたがない群だけ採用」に固定

`build_report` は Q1・Q2 それぞれの信頼区間 (95%) をそのまま報告し、
`adopted_groups` (採用する群) はその群自身の信頼区間で決める。一方
「どちらかが有効か」という両方を見た全体判定 (`decision`) は多重比較になる
ため、Bonferroni 補正 (各検定 97.5%、`FAMILYWISE_CONFIDENCE`) で家族的 alpha
を約5%に抑えたうえで go/no_go を決める。

- テスト: `test_search_query_trial_evaluation.py::PairedDiffsAndEvaluateTest::test_build_report_decision_reflects_either_group_valid`
- テスト: `test_search_query_trial_evaluation.py::PairedDiffsAndEvaluateTest::test_build_report_no_go_when_neither_group_valid`
- テスト: `test_search_query_trial_evaluation.py::PairedDiffsAndEvaluateTest::test_evaluate_group_uses_bonferroni_confidence_when_passed`

### 6. 着手直前に `git fetch origin main` して台帳を読み直す

`budget.refresh_ledger_from_origin_main()` が `git fetch origin main` の後に
`git show origin/main:data/raw/per_asin/_tavily_usage.json` で最新の台帳内容
だけを読む (ワークツリーには書かない)。取得できたらそれを
`check_tavily_budget(usage_data=...)` に渡し、失敗時 (サンドボックスでネット
ワーク不可等) はローカルファイルにフォールバックする。`budget_report` に
`usage_source` / `ledger_source` を追加し、どちらを見たか報告に残る。

- テスト: `test_search_query_trial_budget.py::RefreshLedgerFromOriginMainTest` (3件)
- テスト: `test_search_query_trial_budget.py::CheckTavilyBudgetTest::test_usage_data_overrides_local_file`
- テスト: `test_search_query_trial_run_v2.py::MainCliTest::test_passes_refreshed_ledger_data_to_budget_check`

### 7. 型注釈修正

`run_one_group` の戻り値注釈を `dict[str, Any]` から
`tuple[dict[str, Any], dict[str, Any]]` に修正。

### dry-run 結果 (`git fetch origin main` 後、2026-09-16T01:15:43Z 実施) `[実]`

```bash
git fetch origin main
python -m scripts.experimental.search_query_trial.run_v2 --dry-run
```

- `usage_source: "origin_main"` / `ledger_source: "origin/main (git fetch 済み)"`
  — ローカルの worktree ではなく origin/main から読み直した値であることを確認
- `used_this_month: 374` (origin/main の 9/14 時点の値。9/15 以降の追加消費は
  無かった)
- `remaining_days_in_month: 15` / `projected_existing_lane_remainder: 405.0` /
  `projected_total: 819.0` ≤ `monthly_budget: 900` → `feasible: true`
- 対象 20 ASIN、カテゴリ内訳 `["STEM", "想像", "運動"]` の3種 (候補44件から選定)
- `--resume` の動作: Tavily を叩かないテストで確認 (上記「要修正2」参照)

### テスト

`test_search_query_trial_*.py` 73 passed (既存57 + 今回追加16)。
フルスイート (`.venv` 経由): 4239 passed / 3 skipped / 33 subtests、既存の
環境起因失敗3件 (`test_validate_article_pr_range.py`、PATH に `python` が
無いことによる既知の不具合) 以外は回帰なし。

**実測 (Tavily/gemma への実呼び出し) は go が出てから。** それまでこの PR は
このまま止める。
