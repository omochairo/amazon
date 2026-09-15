# 体験談の検索経路: ホスト種別ごとの歩留まり (#4841 V1)

問い: 体験談を増やすのに効くのは「検索語」か。新しい検索基盤 (SearXNG 等) を入れる
前に、既存の検索結果 (`third_party_sources.json`, Tavily・商品名だけの検索語) で
手持ちのデータを測る。V1 は外部への新規リクエストを最小限にした計測 (JS シェル判定の
30 URL のみ再取得)。実施内容・判明事項・未検証のことは PR / #4841 コメントの 4 項目
報告を参照。数字の生成物は `docs/experience-source-yield/v1_results.json`。

## V1. ホスト種別ごとの歩留まり

### 分類表

`scripts/analyze_third_party_yield.py` の `HOST_CATEGORIES` (host のサフィックス
一致)。**網羅はしていない**: third_party_sources.json は distinct host が 1,927
あり、73% が「上位 200 ホストのみ」でカバーされる分布 (実測 2026-09-15)。上位に
出た約 150 ホストだけを明示分類し、残りは `other` に落として上位ホストを報告する
(下表)。

### 分母 (生存者バイアスを避ける)

`experience.json` は snippet が 0 件だと書かれないため、「third_party_sources.json
に URL がある」だけでは分母にならない。分母は「体験談マイニングが実際に fetch を
試みた ASIN」に絞った:

- K8 の named volume `docker_experience-raw` 上の `mining_ledger.json` を
  読み取り専用でコピー (`docker cp` 相当。volume には書き込んでいない)
- home-ops workflow 「Experience Mining (gemma / Antigravity CLI / Threads /
  Yahoo / K8)」の run 34788591470 (2026-09-13T23:03Z) と 34896203769
  (2026-09-14T20:59Z) の `0_mine.txt` ステップログ (`third_party fetch failed for
  <url>: ... — skip` の warning 行を fetch 失敗として拾う)

`[実]` **ledger には現在 25 ASIN しか無い。** 上記 2 run の「ASIN: wrote ...
(N snippets)」ログ行と 1:1 で一致し、ledger がこの 2 run 分の記録しか持っていない
ことを確認した (それより古い記録は無い — 機構導入から日が浅いか、volume が最近
作り直された可能性があるが特定はしていない `[未]`)。**V1 の歩留まり集計は
この 25 ASIN が母数**であり、特に blog 種別の「試した URL」はわずか 3 件しかない。
比率は出るが統計的な重みはほぼ無い。数値は下表の通り報告するが、**この N で本番判断
はできない** (V2 が必要な理由そのもの)。

### 結果: corpus 全体のホスト種別分布 `[実]`

2,003 ASIN・8,940 URL 全体 (分母を絞らない、V1 タスク①)。

| 種別 | 件数 | 割合 |
|---|---:|---:|
| ec | 3,369 | 37.7% |
| other | 2,991 | 33.5% |
| sns | 1,274 | 14.3% |
| maker | 766 | 8.6% |
| media | 324 | 3.6% |
| blog | 216 | 2.4% |

`other` の上位ホスト (抜粋。全リストは `v1_results.json` の `top_other_hosts`):
facebook.com (56) / reddit.com (52) / educe-web.craypas.co.jp (37) /
play.google.com (33) / jp.pinterest.com (28) / pinterest.com (24) /
anpanman.jp (18) / apps.apple.com (15) / detail.chiebukuro.yahoo.co.jp (15) /
family-games.blog (13) / saiyasune.com (13)。facebook / reddit / pinterest は
実質 SNS だが、依頼コメントの sns 定義 (youtube / instagram / x / threads /
tiktok の 5 host) を勝手に広げず `other` のまま報告する。

### 結果: 歩留まり (分母 = ledger の tried ASIN 25 件) `[実]` (N 小、参考値)

| 種別 | 試した URL | 取得失敗 | snippet 出た URL | snippet 数 | URL あたり snippet | 不満 |
|---|---:|---:|---:|---:|---:|---:|
| blog | 3 | 0 | 2 | 4 | **1.33** | 1 |
| ec | 25 | 13 | 2 | 4 | 0.16 | 0 |
| maker | 4 | 1 | 0 | 0 | 0.00 | 0 |
| media | 2 | 0 | 1 | 2 | 1.00 | 0 |
| sns | 11 | 0 | 1 | 1 | 0.09 | 0 |
| other | 8 | 1 | 3 | 6 | 0.75 | 0 |

aspect 内訳は `v1_results.json` の `yield_by_host_category.<種別>.aspect_breakdown`
に全種別ぶん出力済み (不満以外も含む)。

### 追加測定: blog の本文が JS シェルだけになっている割合 `[実]`

blog 種別 (corpus 全体、216 URL) から固定 seed で 30 URL を再取得
(`HONEST_UA`・1 秒 1 リクエスト・検索結果ページ除外)。判定は「本文テキストに
商品名 (トークン単位) かブランド名が含まれるか」。

- 取得成功 30/30、判定対象 30 件
- **本文が薄い (商品名/ブランド名を含まない) 件数: 1/30 (3.3%)**
- ameblo.jp / note.com / hatenablog.jp 系はいずれも SSR で、通常の GET で本文が
  読めている。**JS シェル問題は blog 種別では確認されなかった** (EC 系サイトで
  よく見る症状とは違う)

この計測だけは分母を「体験談マイニングが実際に試した ASIN」(25 件、うち blog は
3 URL) に絞らず、third_party_sources.json 全体の blog URL (216 件) から抽出した。
歩留まり計算 (上表) は生存者バイアスを避けるため ledger に縛る設計だが、この測定は
「blog ホストの本文品質そのもの」を見るのが目的で、母数を 3 件に絞ると測定不能に
なるため。

### V2 に進む条件の判定

依頼コメントに事前固定された条件:

- blog 種別の「URL あたり snippet 数」が ec 種別の 3 倍以上
  → 実測 1.33 / 0.16 = **8.3 倍** (条件を満たす)
- blog 種別の URL が全体の 10% 未満
  → 実測 216 / 8,940 = **2.4%** (条件を満たす)

**数値上は「V2 に進む」の条件を両方満たす。** ただし上記の通り、この判定の根拠は
blog の「試した URL」がわずか 3 件・snippet 4 件という極小サンプルであり
(ec は 25 件で相対的にまだ厚い)、**条件式が数値上成立していることと、結論に
統計的な重みがあることは別**。母艦レビューで、この N でも V2 (Tavily 20 query ×
2 群) に進める判断が妥当か確認してほしい。

参考として、JS シェル判定 (N=30、corpus 全体から抽出) は blog が「取れば読める」
ことを裏付けており、歩留まり自体(snippet 化率)が低いことの原因が「取得の失敗」
ではなさそうだという傍証にはなる。

## V2

未着手。V1 の母艦レビュー通過後、着手可否を改めて判断する。
