# GSC / GA4 分析データ・記事データ パイプライン トラップ集

計測データの読み方・記事JSONの取り扱いで実際に誤診断・データ破損に繋がった罠。
数字を根拠に判断する前にこのファイルを確認する。

## GSC `totals.*_sum` は上位100ページ/クエリの合計。サイト全体ではない
`fetch_gsc.py`の`totals.clicks_sum`/`impressions_sum`は**`by_page`上位100件の合計**で
サイト全体の値ではない。実測差: impressions 23,585 vs 44,704 = **47%の取りこぼし**
（規模の大きいプロパティで顕在化。navi単体では実害が薄かったが、property追加で前提が
壊れた。フィールド名が`_sum`のままなので気づけない）。

**How to apply:**
- サイト全体の数字が要るときは**`*_sitewide`を読む**（dimensionlessクエリで別途取得。
  `_sum`は上位N合計のまま意味を変えていない）
- `_sum`の完全性は`truncated_*`フラグで判定する（フラグ導入前の過去行には無い）
- データ無しの日は`*_sitewide`が`None`（0は「本当にゼロ」、position=0は無意味と区別）
- 打ち切りの並び順は**クリック数順**（impressions順ではない）。低CTRクエリはクリックが
  少なく窓から落ちるため、CTR異常分析にこのjsonlを使うと系統的に過小サンプルされ観測CTRが
  上振れする
- GA4側の同種罠: `screenPageViews_sum`もtop-N capped で、行数上限が期間途中で変わることが
  ある。日を跨いだ絶対値比較は無効（トレンドの形は使えるが水準比較は不可）。週次レポートの
  クエリと日次jsonlは別クエリなので**絶対値が一致しない前提で扱う**（混ぜて計算しない）

## GSCの`position`はSERP上の面を区別しない
画像パック内のサムネイル表示も、通常の青リンクと同じくposition~1のweb検索impressionとして
計上される。「pos 1.4 / CTR 1%」という通常結果ではありえない組み合わせが出ることがある。
実例: 213,797 imp / CTR 0.94% / 94日フラットで「lost clicks最優先」に見えたページが、
SERP実見の結果、最上部画像ブロック内サムネイル1枚のみで通常の青リンクは1ページ目に
不在 — 回収余地はゼロだった。

**How to apply:**
- ページ次元の`position`を順位の代理に使わない（複数クエリのimpression加重平均なので
  position→CTR曲線が非単調になりうる。曲線が非単調ならベースラインが壊れている合図）
- 「順位は高いがCTRが低い」を一括りにしない。実測では3型に分かれた: 画像パック占有
  （フラットに低CTR、回収不能）/ 一過性バースト（数週間だけpos~1で急上昇後消滅、対象が
  既に存在しない）/ 通常結果（青リンク、ここだけが施策で動く）
- **判定はSERP実見でしか付かない**。GSC UIはAI Overviewを分離表示しないため、UI上の
  手クリックは徒労になりやすい
- SERP実見はブラウザ自動操作で1クエリ2コール、Google側へのアクセスなので自サイトへの
  負荷はゼロ。判定基準は通常結果（テキストリンク）/画像タイル（innerText空）/不在。
  アプリ内ヘッドレスブラウザはGoogleのボット検出に弾かれるため実ブラウザを使う
- AIOの引用元判定はセレクタ非依存（`document.body.innerHTML`全文検索等）で行う。
  折りたたみ内の引用リンクは`getBoundingClientRect().height === 0`になる

## GSC URL Inspection APIでindex実態を測る（Coverageレポート代替）
集計版Coverageレポートに相当するAPIは無いが、**URL Inspection APIはURL単位でindex実態を
返す**ので、母集団を自前で用意すれば同じ問いに答えられる。「APIが無い」で止まる前に
粒度を変えられないか疑う。

- 認証は`fetch_gsc.py`と同一（`searchconsole` v1・スコープ`webmasters.readonly`）、
  再同意・新規secret不要
- クォータ**2000 URL/日/プロパティ・600/分**。母数が枠に収まるか先に数えると設計が
  「推定」から「実測」に変わる
- **1URLあたり6〜7秒と遅い**。1,500件を逐次だと約2.8時間かかりjob timeoutに載らない。
  律速はAPIレイテンシなのでスレッド並列化で短縮できる（`googleapiclient`のserviceは
  thread-safeでないため`threading.local()`でスレッドごとに構築）
- **クォータ2000/日は失敗したrunも消費する**。落ちたrunを再実行すると同日分が枯れて
  429が混ざる
- Googleの日次クォータリセットはPacific深夜（UTC 07:00前後）。「UTC日付が変わったから
  大丈夫」ではなく**同一クォータウィンドウ内かを見る**。スクリプト側に連続429を検知する
  circuit breakerが無いと、手動再実行の判断ミスが数十分の無駄なAPI連打に直結する
- クォータ超過は429だけでなく**403 (reason=quotaExceeded)** でも返る。両方をリトライ
  対象にする
- workflowが作るdata PRの出力先は**validateのpathsに登録が必要**（scriptだけ登録して
  出力先を忘れるとrequired check不発でBLOCKEDになる → [claude-traps-github-actions.md](claude-traps-github-actions.md)）
- **限界**: サイト全体Coverage集計の代替ではない。答えられるのは「送信済みsitemap URLの
  実態」だけ。live sitemapを母集団にするのが要点（GSCの「すべての既知のページ」は旧
  noindex/404が永久に混ざる）

## GSC by_query / by_page は別々に蓄積される。query×pageのcomboは無い
`gsc_by_query.jsonl`と`gsc_by_page.jsonl`は別々に日次appendされ、query×pageのcombo
（`by_combo`）は**committed historyに存在しない**（transientな`gsc_weekly.json`にしか
無く、これはcommitされない）。query→page紐付けが必要な機能（brand名のquery文字列
substring matchでpage単位のnarrativeへ注入等）は、専用のsnapshot infra
（週ISO単位で`data/analytics/gsc_history/<YYYY-Www>.json`）を別途用意する必要がある。

新しい`data/analytics/`サブディレクトリをauto-mergeでcommitするときは
**04-validate-article-pr.ymlのpaths登録が必須**（登録漏れでinverse trapに陥る実例あり）。

## 内部被リンク数は `data/site_audit/inbound_links.json` を見る
`scripts/audit_site_health.py`の週次site-auditが構築する`inbound_links.json`が正。
**サイドカーの`internal_link_suggestions`（`*.seo.json`）を代理に使ってはいけない** —
これは`generate_internal_links.py`が出した**リンク候補**であって描画済みリンクではない。
カバレッジが本記事の5%未満しかないため、代理に使うと「73/73件が被リンク0件」のような
誤った孤立判定を出す（0は「被リンクが無い」ではなく「未測定」）。

被リンク数を扱う前に必ず**供給源のカバレッジ（分母と実データ保有率）を先に測る**。
カバレッジが低い供給源での0はunknownとして扱う。`inbound_links.json`は週次site-audit
（火曜UTC）が回るまで生成されないため、参照側は不在を通常運用として扱う。

`scripts/internal_links.py`は紛らわしいが無関係 — 中身はomcha.jp(WP)の関連記事を
REST APIから取るクライアントで、naviの内部リンクとは別物。

## 記事データのサイドカーは本体を shadow しうる
`data/articles/`には本体記事`YYYY-MM-DD-ASIN.json`と並んでサイドカー
`*.quality.json` / `*.enrichment.json` / `*.seo.json`が同居する。サイドカーは
`product`を持たない別物（例: quality.jsonはquality-gateレポート）。

ファイル名で`".quality.json" > ".json"`（文字コード順）なので、
`sorted(glob("*-ASIN.json"))[-1]`のような「同ASINは名前が大きい方を残す」dedupは
**サイドカーを掴み本体をshadowする**。本体を読まないコード（ASIN列挙のみ）では
顕在化しないが、本体JSONを読んで処理するコード（スコアリング等）では本体が消えて壊れる。

`data/articles/*.json`を列挙・解決するときは必ず
`SIDECAR = (".enrichment.json", ".quality.json", ".seo.json")`で`endswith`除外する
（基準は`score_calculator._cli` / `build_post.SUFFIX_SKIP`と同じ）。新規にarticlesを
globするスクリプトを書くたびにこの除外を入れること。

**除外箇所はコードベース各所に散在しており、新sidecar種別を足すと除外漏れが起きる。**
2026-07-18、26-faq-seo-laneが作ったdata PRの`validate`が全`.seo.json`sidecarで
`'slug' is a required property`エラーになりBLOCKEDした。原因は生成物ではなく
`04-validate-article-pr.yml`のarticle列挙が`.quality.json`しか除外していなかったこと
（正準3種のうち1種のみ）。`scripts/notify_{threads,buffer,bluesky}.py` /
`scripts/analyze_low_confidence_links.py` / `scripts/brand_normalizer.py` /
`03-invoke-jules.yml` / `invoke_jules_repoless.py` は3種すべてを除外している中で、
04-validateだけがこの集合から外れていた。

**How to apply**: 新しいsidecar種別（`.<type>.json`）を導入するときは、上記の全除外箇所を
`grep -rn "quality\.json" scripts/ .github/workflows/` で網羅的に洗って更新する。
`.quality.json`はgit管理外（生成物・ローカルのみ）だが`.seo.json`/`.enrichment.json`は
data PRでcommitされるためdiffに乗る＝CI検証対象に入る点に注意。

記事存在判定（`hugo/content/posts/*.md`を読んではいけない理由）は
[claude-traps-hugo-rendering.md](claude-traps-hugo-rendering.md) 参照。
