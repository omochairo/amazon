# 外部 API 連携 トラップ集 (Amazon / Rakuten / GA4 / GSC 認証 / SNS)

商品データ取得・分析データ取得・SNS配信で使う各外部APIの、公式ドキュメントだけでは
分からない実挙動。新規にresourceやendpointを追加する前に確認する。

## GA4 / GSC 認証

### 2026-04-20以降作成のService AccountはGA4/GSC UIに追加できない
Google公式の既知バグ（2026-05-01報道）。`@gserviceaccount.com` の email lookupが
「Googleアカウントとして登録なし」と誤判定し「Email not found」で必ず弾かれる。設定ミス
ではないのでUIで粘っても通らない。

**GA4は Admin API v1alpha 経由で回避できる**（実証済）:
```
POST https://analyticsadmin.googleapis.com/v1alpha/properties/{PROPERTY_ID}/accessBindings
{ "user": "<sa-email>", "roles": ["predefinedRoles/viewer"] }
```
v1betaでは`accessBindings`未実装で404。OAuth Playgroundでスコープ
`analytics.manage.users` を使い本人アカウントのaccess tokenで叩く。

**GSCは回避策なし**（APIがuser management機能を公開していない）。SA方式を諦め、
**OAuth refresh token方式**を使う: GCPでOAuth 2.0 **Web application** Client を作成
（Desktopは`redirect_uri_mismatch`で不可）、Authorized redirect URIsに
`https://developers.google.com/oauthplayground`（末尾スラなし）を追加、scope
`webmasters.readonly` でrefresh token取得。**Consent screenのPublishing statusは
`Production`に変更必須** — Testingのままだとrefresh tokenが7日で失効する
（`refresh_token_expires_in: 604799`）。Production化は verification 不要で即可能。
Production化後はrefresh tokenを取り直す必要がある。

### GA4に`entrances` metricは存在しない
`Metric(name="entrances")` は `400 INVALID_ARGUMENT` で request全体が落ちる（旧Universal
Analytics由来のmetric名でGA4には無い）。着地数が欲しいときは別query で
`dims=["hostName","landingPagePlusQueryString"]`, `metrics=["sessions"]` を取り、query
stringをstripして `(hostName, pagePath)` に合算しby_pageへjoinする。landing queryは
try/exceptでwarningに留め、落ちても本体fetchを止めない設計にする。

## Amazon Creator API

商品データ取得は **PA-API ではなく Amazon Creator API**（`CreatorsAPIClient`）。スクリプト
名やコメントに「PA-API」とあっても実体はCreator API。エンドポイント:
`https://creatorsapi.amazon/catalog/v1/{searchItems,getItems}`、認証はOAuth2
client_credentials via AWS Cognito。resource名はPA-API風camelCaseだが**独自resource
セット**なので、PA-API 5公式仕様で valid/invalid を判断しても無意味。

**Invalid resourceを足すとsearchItems全体が400でreject**され、`fetch_amazon.py`が
`Search returned zero items across all keywords; aborting` でexit 1、cron全停止に
つながる（実例: `offersV2.listings.deliveryInfo`）。

**How to apply:**
- 新resourceを`SEARCH_ITEM_RESOURCES`に足すPRは、`04-validate-article-pr.yml`のdry-run
  gateが自動検証する（1 keyword×1 itemを実APIに投げてHTTP 200 + items[]非空を確認）
- ただしgateはresourceの**schema valid性のみ**確認する。実際に値を返すかは別問題
  （例: `itemInfo.productInfo`はvalidだが`parentAsin`フィールド自体が空で返る可能性）。
  マージ後は`data/raw/per_asin/*/amazon.json`をgrepして新フィールドが実際に書き込まれて
  いるか必ず確認する
- `getItems`と`searchItems`でvalid setが異なる可能性がある。新resourceは必ず
  searchItemsでdry-run gateを通す
- Python/templateのみを変更するPRは`04-validate-article-pr.yml`が
  `data/articles/*.json`のschema検証しか見ておらず、対象ファイルが空だと全stepがskip
  されてvacuous SUCCESSになる。「required check通過＝実fetch成功」ではない

**reseller filterの空seller扱い**: `is_trusted_seller(seller)`は空sellerもFalse
（信頼不能）扱いのため、Creator APIがsellerを埋めないASINが大量にdropされうる
（実測: unique 201中154=77%がreseller-drop）。dropの内訳（seller-empty /
not-trusted-pattern / low-stock）をロギングしてから根因判断する。空sellerを即drop
でなくwarningに降格する案・Creator APIにmerchantInfoを明示要求する案がある。

## Rakuten API

旧endpointを触るときは**3段の互換性trapを順番に踏む**前提で計画する。1段ずつPRを出し
manual dispatchするとtrapが判別できる。

**Ichiba (Ranking/Search)**:
1. version date `20171001`等は deprecated。`20220601`（RMS移行版）のみ受け付ける
   endpointが多い
2. v20220601は`applicationId` + `accessKey` 両方必須（RMS license紐付き、
   applicationId単独は弾かれる）
3. 旧`app.rakuten.co.jp/services/api/...`hostはv20220601 + RMS appIdを拒否する場合が
   ある。新`openapi.rakuten.co.jp/<service>/api/...`hostを使う

**BooksTotal/Search**（versionは`20170404`のまま、host/認証は同じ罠）:
1. RMS紐付きapplicationIdは旧hostに`specify valid applicationId`(400)で拒否される
   （IchibaItem/Search/20220601は旧ホストでも通るため、拒否はendpoint依存）
2. 新hostは`applicationId` + `accessKey`両方必須
3. **Referer/Originヘッダが無いと`403 REQUEST_CONTEXT_BODY_HTTP_REFERRER_MISSING`**。
   値はアプリ登録URL

**Product Search (商品価格ナビ)**（`app.rakuten.co.jp`廃止後も後継が存在する。「旧host
廃止＝このAPIは死んだ」と早合点しない）:
- endpoint: `openapi.rakuten.co.jp/ichibaproduct/api/Product/Search/20250801`
  （Ichiba系の20220601とは別のversion番号）
- 既存`RAKUTEN_APP_ID`/`RAKUTEN_ACCESS_KEY`がそのまま通る（追加secret不要）
- `accessKey`は**paramsに入れる**（headersでは通らない）。Referer/Origin必須は同様
- `keyword`は短くする。長い文字列/記号は400 `keyword parameter value is not valid`。
  記号除去+先頭5トークン/32文字程度に詰める
- 1req/secでも429を観測。バッチは間隔を空ける
- 短いブランド名クエリは**ブランドの別商品**を返す（誤マッチ防止に別途照合ガードが要る）

**cron出力の空上書きガード**: `fetch_rakuten.py`系のcron output（`weekly.json`等）は、
Rakuten APIが失敗（429等）しても**空配列で無条件上書き**し`/ranking/`が長期間空になる
（PR #1454実例）。外部API出力をフルリプレースするcronは全て「空→上書きしない」ガード
（前回スナップショット温存）を入れる。TPS=1厳守（Search間1.0s以上、別endpoint間1.5s以上）。
429は1回リトライ（3s backoff）で大抵通る。

**同時dispatch衝突**: `data/raw/{rakuten,yahoo}_matched.json`を毎回fresh rebuildする
workflow（re-search-low-confidence等）は、先行PRがmerge される前に2回目をdispatchすると
後発PRがCONFLICTINGになる。連続dispatchは1回目のmerge完了を待つ。既にconflictしたPRは
`gh pr close <num> -d`→main最新で再dispatchすれば解消する。

## キーワード抽出（Rakuten/Yahoo クロスサーチ）

Amazonタイトルからキーワード抽出する際の3つの罠:

1. **カギ括弧`『 』`の中身は商品名** — タカラトミー系（TOMICA/プラレール/BEYBLADE等）は
   `『 トミカワールド カーゴジェット ANA 』` のように固有商品名をカギ括弧で囲む。
   カギ括弧`「」『』`/山括弧`〈〉《》`は**記号のみ**スペースに置換し中身保持する
   （丸括弧`()`/角括弧`[]`/隅つき`【】`は中身ごと除去してよい）
2. **モデル番号は英大文字と数字を両方含む必須** — 大文字始まりだけの判定
   (`^[A-Z][A-Z0-9\-]{3,11}$`)だと`TOMICA`/`BEYBLADE`のような英単語ブランド名を型番と
   誤検出する。lookaheadで英字+数字両方必須にする:
   `(?=.*[A-Z])(?=.*[0-9])[A-Z0-9\-]{4,12}`
3. **ブランド+メガジャンルの短縮形は暴投する** — 汎用ブランド語のみの短縮形
   （`タカラトミー トミカ`等）はAPI側で何千件もヒットし関連性0の商品を返す。
   `_GENERIC_BRAND_TOKENS` allow-listを持ち、短縮候補が全部このセットに含まれる場合は
   該当Stageをskipする（「マッチしない」方が「誤マッチ」より良い設計判断）

補足: 末尾の数字トークン（`3 4`等）・波ダッシュ・漢数字単独はRakuten Ichibaに
`keyword is not valid`(400)を起こすため除去が要る（`_is_age_token`）。

## live URL

live確認URLは**`https://navi.omcha.jp/`**（Hugo baseURL）。`omcha.jp`直下は別ホスティング
（WordPress）で、Hugo buildの配信先ではない。`curl -sI https://omcha.jp/ranking/`が404を
返すのは「/ranking/が無い」のではなく「そもそも別ドメイン」。Hugo buildのdeploy先は
GitHub Pages→`navi.omcha.jp`（CNAME）。

## SNS (Buffer / X / Threads)

### Buffer GraphQL APIはX/Threadsどちらもthreadを作れない
Buffer内部DBはthreadを格納するが、配信時に`type: "post"`でX公式APIへ**親のみ送信し子は
破棄**される。`TwitterPostMetadataInput`/`EditPostInput`のどちらにも`type`フィールドが
無く入力経路から`type: "thread"`を渡せない。Threads側は`type: "thread"`を強行すると
`UnexpectedError`で配信時拒否される。thread投稿はX公式API（tweepy v2）とThreads公式API
（Meta Graph API）直叩きが本線。Bufferは単発投稿の配信エンジンとしてのみ使う。

### Threads access tokenのlong-lived判定は`th_refresh_token`で行う
`debug_token`はgraph.threads.netでは使えない（app_token形式を弾く実装）。`/me`で200が
返るvalid tokenでも既にlong-livedだと`th_exchange_token`が400
（subcode 4279019 `Session key invalid`）を返す。判定手順:
1. `GET /v1.0/me?fields=id,username&access_token=$T` でtoken自体のvalidity確認
2. `GET /refresh_access_token?grant_type=th_refresh_token&access_token=$T` で種別判定
   （decisive）— 成功+`expires_in≒5,184,000`なら既にlong-lived、
   `"not a long-lived"`エラーならshort-lived（再OAuth要）
3. `th_exchange_token`の400(subcode 4279019)は「既にlong-lived」のサイン。
   エラー扱いせず`th_refresh_token`に切り替える

PowerShellの`Invoke-RestMethod`は4xxでerror bodyを捨てるため、
`$_.Exception.Response.GetResponseStream()`で本文を読む必要がある。

### X (Twitter) はCJK文字をweight 2でカウントする
`len()`ではなく`twitter-text`仕様のweighted countでtruncateする。CJK/emojiはweight 2、
Latinはweight 1。`len(hook) > 220`でtruncateすると実質440weight相当になり280文字制限を
大幅に超えてBufferにrejectされる。

```python
def _x_weight(c: str) -> int:
    cp = ord(c)
    if cp <= 0x10FF or 0x2000 <= cp <= 0x200D or 0x2010 <= cp <= 0x201F or 0x2032 <= cp <= 0x2037:
        return 1
    return 2
```

URLはBufferが**実文字数**でカウントする（t.co短縮23は使われない）。予算計算は
`280 - url_weight - separator_weight - ellipsis_weight - safety`。Threadsは500文字制限で
weightedではない（別budget管理が要る）。

### Threads publish は container 作成後 最低30秒のsettle waitが必須
text-onlyでもeventual consistency windowがある。`/{user_id}/threads`（container作成）→
`/{user_id}/threads_publish`の間、最低30秒（推奨31秒以上）待たないと
`HTTP 400 Media Not Found, error_subcode=4279009`が出る。reply container
（`reply_to_id`付き）は特に踏みやすい。`time.sleep(32)`等の固定sleepを入れ、transient
error判定は`media not found / cannot be found / does not exist / 4279009 / 4279019 /
media id is not available`を広くmatchする。max retry 3回+10秒backoff。

### X card は画像下部にtitleをoverlay描画する
`twitter:card=summary_large_image`はX側で画像下部80px程度に半透明黒帯でtitleを
overlay描画する（多くの解説記事の「別行で出る」は不正確）。OG画像下端80-100pxにwatermark
等の重要要素を置くとoverlayに潰される。watermark/ロゴは右上か左上に配置する。Threads/
Facebook/LinkedInはtitle overlayしない。

### link preview のcarouselはX/Threadsとも標準仕様に無い
「同じURLのlink preview cardが複数og:imageをswipeable carouselとして描画する」挙動は
標準実装されていない。複数og:imageを emit しても最初の1枚しか採用されない（Xは
`twitter:image`、Threads/Facebookは`og:image`の最初の1つ）。**per-page動的化**（記事ごと
に異なるOG画像を持つ）は可能で実装済みだが、**per-render動的化（同一URLのlink preview内
でswipe）**はできない。post本体をcarouselにする別ルート（Threads APIの
`media_type=CAROUSEL`で複数IMAGEを1postに添付）はあるが、URL link previewとは別物。
