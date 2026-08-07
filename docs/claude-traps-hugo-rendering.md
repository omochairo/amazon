# Hugo レンダリング・パフォーマンス トラップ集

Hugo ビルド・テーマ (PaperMod)・パフォーマンス計測まわりで実際に踏んだ、再発しやすいトラップ。
新規実装・レビュー時にこのファイルを検索してから着手する。

## URL / ルーティング

### taxonomy リンクは urlize でなく `.Page.RelPermalink` を使う
`(printf "tags/%s/" (urlize .Name))` は罠。`urlize` と Hugo の term ページ URL 生成 (anchorize) は
別アルゴリズムで、特殊文字を含む語 (`Pretend & Play` 等) で不一致になり 404 を生む
(session 135, PR #2125: 213 ページが 404)。

taxonomy エントリ (`.Data.Terms.ByCount` / `site.Taxonomies.X.ByCount`) からリンクするときは
必ず `{{ .Page.RelPermalink }}` を使う。urlize で URL を「組み立てない」。

関連トラップ (同PR):
- `len .Params.brands` は param が未設定 (nil) のとき `error calling len: reflect: call of
  reflect.Value.Type on zero Value` でビルド失敗。nil 可能性のある Params は `len` でなく
  `and` の真偽判定 (`{{ if and X .Params.brands ... }}`) で扱う
- `.Params.brand`（canonical文字列・常時付与）と `.Params.brands`（taxonomy list・
  `exclude_from_taxonomy=false` のみ付与）は別物。taxonomy 実在判定は `.Params.brands` で
- 未公開 ASIN（`.quality.json` サイドカーのみ・content `.md` 無し）への link は 404。
  描画前に `$.Pages` 実在照合でガードする

検証: `hugo --minify --quiet 2>&1 | tail -5; echo EXIT=$?` の EXIT は **tail のもの**で hugo の
失敗を隠す（パイプの罠）。`hugo --minify 2>err.log; echo $?` で hugo 自身の exit を見て
`grep -i '^ERROR' err.log`。

### URL は小文字化される（記事 slug は大文字 ASIN のまま）
記事 slug は `2026-05-19-B0DY75DS1S` のように ASIN 部分が大文字だが、配信 URL は
`/amazon/posts/2026-05-19-b0dy75ds1s/`。`disablePathToLower` の既定は false のため URL は
自動小文字化される（PR #175 で大文字 URL 404 を実際に踏んだ）。

Python 側で Hugo 向け記事 URL を組み立てるときは必ず `slug.lower()` を通す。slug 自身
(`data/articles/*.json` のファイル名・slugフィールド) は大文字 ASIN のままでよい —
URL 文字列を作るときだけ小文字化する。

### `_index.md` の title / slug 罠
**空 frontmatter で `.Title` が空文字列になる**: taxonomy term ページ用 `_index.md` を空
frontmatter（コメントのみ等）で置くと `.Title` が空になり、ディレクトリ名にフォールバック
**しない**（PR #1048）。`head.html` の brand SEO override・JSON-LD の ItemList・hero block が
連鎖的に消える。`title: "<term>"` を term名と完全一致で明示するのが必須仕様。

**`slug:`/`url:` override は一覧ページで別の title バグを踏む**: `_index.md` に `slug:`/`url:`
で URL を上書きすると、個別 term ページ (`_default/single.html`) は正しい title を出すが、
一覧ページ (`.Data.Terms.Alphabetical` 等) 側の `.Page` オブジェクトは別解決になり、
humanize された生スラッグが表示される（Hugo v0.160.1 実証）。回避策: override を使わず
**物理ディレクトリ名を直接最終スラッグにする**。既存 JP 名ディレクトリのリネームコストを
払ってでもこちらを選ぶ。

## データ型・テンプレート

### JSON 数値は float64 —比較前に `int` キャスト必須
`hugo/data/**.json` の数値を `{{ eq .rank 1 }}` で比較すると常に false（Hugo は JSON数値を
float64 でアンマーシャルし `eq` は型厳密）。`/ranking/` メダル 🥇🥈🥉 が出ない不具合の真因
だった（Issue #600 PR4）。

```go
{{- $rank := int (.rank | default 0) }}
{{- if eq $rank 1 }}<span>🥇</span>{{- end }}
```

`printf "%d"` で印字する場合も先に int 化しないと `1.000000` になる。

### `.Params` はキーが小文字化される — JSON-LD は jsonify を経由しない
`.Params` map は内部でキーを小文字化する。frontmatter の camelCase (`aggregateRating` 等) が
`{{ .Params.jsonld.product | jsonify }}` 経由だと `aggregaterating` になり、schema.org の
case-sensitive 判定に落ちて Google Rich Results に届かない（PR #1298: 642商品ページの
AggregateRating/FAQPage が長期未達だった実例）。

修正パターン: build 側で **JSON文字列として** frontmatter に保存 → template は `| safeJS` で
素通し（jsonify を経由しない）。`<`/`>`/`&` は `\uXXXX` escape して `</script>` 衝突を防ぐ。
PyYAML の長い文字列の 80桁折り返しも Hugo yaml.v3 側で literal `\n` 化しparse errorになるため
`frontmatter.dumps(post, width=10000)` で抑止する。

### Jinja2 dict の key に `items`/`keys`/`values` を使わない
build_post.py で Jinja2 に渡す dict の key 名が `items`/`keys`/`values`/`get`/`pop`/`update` 等の
dict builtin と衝突すると、Jinja2 の属性アクセス優先 (`obj.attr` を先に試す) により
**メソッドオブジェクト本体**が返る。`|length` でクラッシュ、`{% for x in items %}` は
"iterating over method object" エラー。

PR #1085 実例: `same_price_band.items` が dict.items method を返し、build_post.py が**同じ
価格帯テンプレを含む487記事を黙殺**（404大量発生）。`items` → `cards` にリネームで根治。
dict 渡しテンプレで `x.items|length` / `for y in x.items` を見たら key 名を疑う。subscript
(`x['items']`) も同じ問題が起きるため key 名を変えるしかない。

### 同一ページの複数 output format は `.Scratch` を共有する
Hugo は同一ページ (例: home) を複数 output format (JSON/SearchIndex等) でレンダリングする際、
**同じ Page オブジェクトの `.Scratch` を共有**する。2つのテンプレートが同じキーで
`$.Scratch.Add` すると後段レンダリングの出力に前段が混ざり二重化する（PR #3056: 3記事で
index.json=3のところsearch.json=6）。記事0件のローカル検証では異常が見えず素通りする。

一覧を蓄積するときは Scratch でなく**ローカル変数**を使う:
`$idx := slice` → `$idx = $idx | append ...` → `$idx | jsonify`。検証はダミー記事を数件作り
両formatのエントリ数一致を数える。

## CSS / テーマ (PaperMod)

### モバイル H1 font-size はページ種別ごとにクラスが違う
- `single.html`（記事/商品ページ）→ `<h1 class="post-title">`
- `list.html` / `_default/feature.html` / `_default/term.html` / `_default/ranking.html` 等の
  リスト系 → `<h1>`（PaperMod既定の `.page-header h1` が `font-size: 40px`）

`.post-title` だけ縮小すると `/cospa/` 等リスト系ページのH1が40pxのまま残る（PR #1437 →
follow-up #1439）。mobile font-size を H1 で触るときは3つまとめて書く:

```css
@media (max-width: 720px) {
  .post-title,
  .page-header h1,
  main.main h1 {
    font-size: 1.2rem !important;
  }
}
```

検証は `single.html` 系1ページ + リスト系（`/cospa/` 等）1ページの両方で確認する。

### line-clamp + gradient背景の要素はテキストが枠外にはみ出す
`-webkit-line-clamp: N` を `background: linear-gradient(...)` + `border-left` + `padding` を
持つ要素にかけると、テキストの一部が背景塗りつぶし枠外に飛び出す。**N行未満の短いタイトルでも
発生**する（`-webkit-box` の box height計算とグラデBGの塗り範囲の不一致。PR #1432→#1439）。

対処: 長文タイトルの切り詰めは line-clamp でなく **font-size縮小 + `word-break: break-word`**
を第一手にする。どうしても line-clamp が必要なら、当てる要素をBG・border-leftを持たない
インナー span/div に分離するか、親要素のBGを `background: none !important` で解除してから
かける。

### PaperMod の絶対配置レイヤーがクリックを吸う
`themes/PaperMod/assets/css/common/404.css` の `.not-found` は
`position:absolute; left:0; right:0; height:80%` でページ上部8割を覆う透明レイヤーになる。
404ページに新規ボタンを追加すると「表示されるがクリックできない」バグになる（PR #3076→#3078）。

既存レイアウト（404/archive/search等）に新規インタラクティブ要素を追加するときは、テーマ側
CSSで `position:absolute` な要素がないか grep してから作業する。検証は DOM/computed style
だけでなく `document.elementFromPoint(x,y) === target` や実クリックまで行う。

## Amazon CTA リンクのCSSスコープ

`hugo/assets/css/extended/custom.css` には `.post-content p a[href*="amazon.co.jp"]` の
セレクタでAmazonリンクを橙CTAボタン化+`🛒 `プレフィックスを付与するルールがある（通常/
hover/::before/モバイル/印刷の5箇所）。**セレクタは`<p>`内限定 (`p a[...]`) のまま**にする
こと — `a[href*="amazon.co.jp"]`のような無限定セレクタに緩めると、価格比較グリッドの
ボタンに🛒がさらに付いて折り返す、参考ソース欄`<ul><li><a>`のリンクがボタン化されて
不整合になる、といった再発を招く（過去に実際に踏んで修正済み）。

新しくexplicitなamazonボタンをテンプレに追加する場合は、その親が`<p>`でないこと（`<div>`
直下など）を確認する。参考ソース欄のようにamazonリンクをプレーン表示したい場所は
`<ul>`内なので影響を受けない。narrative本文中（Jinjaテンプレ出力の`<p>`）のamazonインライン
リンクは自動でCTA化されるのが意図通りの挙動。

## ビルド出力の所在 / gitignore

### `hugo/` 配下の新規ファイルは `git add -f` 必須
`.gitignore` に `/hugo` がある（早期の generated artifacts 誤コミット対策の名残）。既存
tracked ファイル（custom.css, partials/*.html等）は影響しないが、**新規追加ファイル**は
ignore される。hugo/配下に新規ファイルを足すときは最初から `git add -f <path>` を使う。

CI レーンでの危険な変種: `git status --porcelain <path>` による差分検出は ignored な新規
ファイルを**エラーなしで空を返す** → data PR がスキップされ「run success なのにデータが
還流しない」（amazon-home-ops 20-semantic-related.yml で発現）。今後 hugo/配下にCIが新規
ファイルを書くレーンを作るときは `.gitignore` の除外連鎖（`/hugo` + `!/hugo/` + `/hugo/*` +
`!/hugo/data` + `/hugo/data/*` + `!/hugo/data/<file>`）に1行足す。

ローカルの孤児 `.md`（`hugo/content/posts/` に対応する `data/articles/*.json` が消えた後も
残る）は本番ビルドに無関係。GitLab CI の `pages` job はキャッシュなしで毎回クリーン
checkoutから `build_post.py` が posts/ を再生成するため、ローカル孤児ファイルは手元
`hugo build` プレビューの精度を落とすだけ。

### 記事存在判定は `data/articles/*.json` を読む。`hugo/content/posts/*.md` は禁止
ランキング/リンク系で「その ASIN の記事が存在するか」判定するときは、必ず commit済みの
`data/articles/<YYYY-MM-DD-ASIN>.json` を読む（サイドカー `.enrichment`/`.seo`/`.quality` は
除外、末尾10文字がASIN）。`hugo/content/posts/*.md` を読んではいけない。

理由: `hugo/` 全体が git 管理外（gitignore）で、`hugo/content/posts/*.md` は
`build_post.py` がビルド時に生成する成果物。build-and-deploy 以外の workflow のランナー
checkout には存在しない。そこを読むと記事インデックスが**常に空集合**になる。

実害（Issue #600 follow-up）: `fetch_rakuten._build_article_asins` が
`hugo/content/posts/*.md` を読んでいたため CI で `articles=0` → 楽天ランキングの
`matched_asin` が本番で全滅（ローカルは hugo/ が存在するので再現せず発見が遅れた）。正準は
`resolve_ranking_asins._load_article_asins` / `fetch_amazon._load_existing_article_asins`
（どちらも `data/articles` 規約）。「ローカルで動くが本番で空」の症状はこの罠を最初に疑う。

## その他の細かいトラップ

- Hugo partialの戻り値は`template.HTML`型。`site.GetPage`/`index`/`where`のような文字列
  厳格な関数に渡す前に`| string`が必須
- Grep (ripgrep) はgitignoreされた`hugo/layouts`をスキップする。layout検索はbash `grep`
  を使う

## Windows ローカル開発

`hugo server` は Windows で末尾ピリオドを含むブランド名（例: `enne.`）のタグページの
ページネーション alias 書き出しで即死することがある:

```
ERROR Alias "\tags\enne.\page\1\index.html" contains component with a trailing space or
period, problematic on Windows
```

CI (Linux) では問題なくビルドできる — 本番には影響しない。「絶対不可」ではなく「踏むことが
ある」程度（該当タグの有無やalias書き出しタイミングに依存し、起動できることもある）。

対処:
- ローカル目視確認は諦めて PR マージ後の本番デプロイで確認する
- どうしても見たいなら該当ブランドタグを一時的にピリオド抜きにして build、WSL/Linux で
  `hugo server` を起動、または config に `disableAliases = true` を追加
- `python scripts/build_post.py` で生成された `hugo/content/posts/*.md` を直接 grep/cat すれば
  Markdownレンダリングは見えないがHTML出力は確認できる

フロントエンド (JS/CSS) の検証はブラウザ DOM を直接操作・状態確認するのが確実。クリックは
要素未hydrateのタイミングで空振りすることがあるので `el.click()` を直接呼ぶと安定する。
実商品ページのスクリーンショットは外部画像（Amazon/Keepa多数）+ backdrop blur でタイムアウト
しやすい — スクショに頼らずDOM評価ベースで確認する。

## パフォーマンス計測 (Lighthouse / PSI / preconnect)

### preconnect の crossorigin は fetch モードと一致させる
`<link rel="preconnect" crossorigin>` は、その origin から実際に取る fetch のモードと
一致していないと効かない。LCP要素の hero画像 (`<img>` に crossorigin属性なし = 非CORS fetch)
に対して crossorigin付き preconnect を張ると、匿名CORSソケットが別プールになり
TCP+TLSを再利用できない（PR #3313）。

**罠である理由**: 完全無効ではなく DNS解決だけは効く（DNSはCORSモードを問わない）ため、
「preconnectなし」より速く、設定は入っているし多少速いので誰も疑わない。実測 (Chrome cold
profile x9 interleaved, median): crossorigin付き 107.5ms / 属性なし 38.2ms / preconnectなし
168.8ms。

適用: 画像 origin（`<img>` にcrossorigin属性なし）には crossorigin を付けない。フォント
(`fonts.gstatic.com`) は常にCORS fetchなので crossorigin必須。cross-originリソースの計測は
`Timing-Allow-Origin` が無いと dns/tcp/tls/ttfbが全部0でマスクされるため `duration`
(responseEnd-startTime) だけが使える信号。

### PSI の `NO_LCP` はPSI側の計測失敗のことがある
PSIが `LCP = Error! NO_LCP` を返してもサイト側バグとは限らない。同じ Lighthouse メジャー版を
ローカルで回すと正常値が出ることがある（PSIはLightrider上で動き実行が劣化することがある）。

見分け方: **エラーになった audit の散らばり**を見る。LCP/TBTだけでなくCSS最小化・JS最小化・
未使用CSS等**互いに無関係な audit まで同時にエラー**なら基盤側。サイト起因なら特定audit
だけが落ちる。`NO_LCP` でLCPが採点から除外されるとスコアはむしろ上がるため、「スコアが良い」
を健全性の根拠にしない。

切り分け: `npx lighthouse <url> --only-categories=performance --form-factor=mobile
--output=json --output-path=./lh.json --chrome-flags="--headless=new --no-sandbox"`。teardown
で非ゼロ終了してもJSONは書けていることがあるため returncode でなく **JSONが読めたか**で
成否判断する。出力JSONをPythonで読むときは `PYTHONIOENCODING=utf-8` 必須
(displayValueの `\xa0` がWindows cp932で落ちる)。

### Lighthouse `lcp` はLantern推定値。`observed_lcp` と別物
自宅Lighthouseレーン (`scripts/run_lighthouse_lane.py`) の劣化検出issueは、サイト回帰では
なく計測・判定側のアーティファクトのことが多い。レーンは `throttlingMethod=simulate`
（Lighthouse mobile既定）で回るため、JSONLの `lcp` は**Lanternシミュレーションの出力**。
実際に描画された時刻は同じJSONの
`audits.metrics.details.items[0].observedLargestContentfulPaint`。**この2つは3倍以上ずれる
ことがある**（Issue #4160実例: 5015ms(sim) vs 1474ms(observed)、描画は無劣化だった）。

**反証されたヒューリスティック**: 「特定ページ・特定metricだけ悪化＝コード回帰の可能性が
高い」は誤り。LCPだけ動きFCP/SI/CLSが動かないのは、むしろ**Lantern推定値アーティファクトの
典型**（実描画が動けばFCP/SIも一緒に動く）。

適用:
- `[lighthouse][regression]` issueが来たら**最初にobservedとsimulatedを比較**する。
  `observed_lcp ≈ observed_fcp` なら描画は健全でサイトを触らない
- `lcp_element` は当てにできない（product ページはほぼ全滅で取れない）。LCP要素の特定は
  `audits['lcp-breakdown-insight'].details.items` の `type == "node"` の `selector` を見る
  （Lighthouse 13で `largest-contentful-paint-element` audit は廃止）
- 全URL・全metricが同時に悪化するパターンはbaseline不足/throttling由来。日次cron単発なら
  基本出ないが、出たらK8の他ジョブとの資源競合を疑う
- ゲートのしきい値は分布の標準偏差を見ずに固定値で設定すると、通常のばらつきで誤発火する
  （`REGRESSION_RATIO=1.25` が実質1.2σ相当だった実例、Issue #4386で修正）。window/
  MIN_BASELINE_SAMPLESを十分に取り、MADベースの閾値をAND条件に加える
- **本番のLighthouse版は amazon-home-ops の `41-lighthouse-lane.yml` が決める**
  (`LIGHTHOUSE_CMD` 上書き)。script側の既定を変えても本番には効かない。版を変えたいときは
  home-ops側を見る。両リポジトリの版がずれたまま別々にマージされるとbaselineが静かに汚染
  されるため、片側だけ触ったら必ずもう片側も確認する

## Service Worker / キャッシュ

### デプロイ伝播窓でSWが古いHTMLをアセットURLとして固定してしまう
GitLab Pagesのデプロイ伝播窓では、新しい指紋付きアセットURL（例: `article_actions.min.*.js`）
への初回リクエストにHTML（404/offlineページ）が返ることがあり、SWの素のcache-firstが
それを恒久固定する（PR #3579で発生・PR #3609で修正）。指紋付きURLは「内容不変」前提で
再検証しないため、一度汚染されるとSWのCACHE_VERSION bump以外で回復しない。`nosniff` により
scriptはコンソールエラーも出さず無音でブロックされる。

診断: curl（cache-buster付き）でoriginが健全かまず確認 → 健全なのにブラウザで機能が死んで
いるなら、ページ内 `fetch(scriptSrc)` のContent-Type/長さと
`fetch(src+'?bust=', {cache:'no-store'})` を比較。前者がHTMLならSW汚染確定。

デプロイ反映タイミング: mainマージ後、navi反映まで15-30分かかりうる（GitLab runnerは
パイプラインを直列処理）。「壊れた」と誤警報する前に (a) commitのpipelineが
`pages`→`pages:deploy`まで到達したか、(b) postdeployの`cf-purge`が走ったかを確認する。
plain `/sw.js` はCloudflareがmax-age=14400でエッジキャッシュするため、`cf-cache-status`が
MISSかつ最新versionになってからbrowser検証する。
