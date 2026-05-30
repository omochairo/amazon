# data/ ディレクトリ構造とキャッシュ境界に関する開発者ガイド

このディレクトリには、アフィリエイト記事の生成、商品マスタの構築、外部APIの取得データなど、本プロジェクトで利用されるすべてのデータファイルおよびキャッシュファイルが配置されています。

本ドキュメントでは、データの全体構造、Gitで追跡（Git-tracked）すべきかどうかの判定基準、および新しい外部APIをシステムに統合する際の手順について詳細に記述します。

---

## 1. ディレクトリの全体構造

`data/` 配下は、データの性質（ライフサイクル、永続性、Git管理ポリシー）ごとに整理されています。

```
data/
├── articles/               # Jules（記事生成エージェント）が生成する記事JSONおよび付随するサイドカーデータ
│   ├── [YYYY-MM-DD]-[ASIN].json              # 本文・レビュー・スコア等が格納された記事のマスターデータ (Git-tracked)
│   ├── [YYYY-MM-DD]-[ASIN].enrichment.json   # 記事の補強情報サイドカー (Git-tracked)
│   ├── [YYYY-MM-DD]-[ASIN].seo.json          # SEOの最適化情報サイドカー (Git-tracked)
│   └── [YYYY-MM-DD]-[ASIN].quality.json      # quality_gate による自動検証の実行結果 (Git-ignored)
│
├── raw/                    # 外部APIから取得した商品の生データおよび一時キャッシュ
│   ├── amazon.json                   # 直近のクロール結果（短寿命、Git-tracked）
│   ├── rakuten_matched.json          # 楽天アフィリエイトリンクの対応表（短寿命、Git-tracked）
│   ├── yahoo_matched.json            # Yahooアフィリエイトリンクの対応表（短寿命、Git-tracked）
│   ├── ranking_pool.json             # 未記事化の優良ASINプール。Julesの選択対象 (Git-tracked)
│   ├── _fetch_state.json             # クローラーの stales-first 管理情報 (Git-tracked)
│   │
│   └── per_asin/                     # ASINごとに個別に取得した詳細データのキャッシュディレクトリ
│       └── <ASIN>/                   # 各ASIN専用 of-truth 
│           ├── amazon.json           # クローラーが保存する長寿命なAmazon商品情報スナップショット (Git-tracked)
│           ├── competitors.json      # 競合他社・類似製品との比較結果 (Git-tracked)
│           ├── youtube.json          # YouTubeレビュー動画情報 (7日 stale cycle、Git-tracked)
│           ├── news.json             # ニュースリリースや最新情報 (7日 stale cycle、Git-tracked)
│           ├── books.json            # 関連書籍データ (7日 stale cycle、Git-tracked)
│           └── omcha_related.json    # 関連記事や玩具の対応表 (7日 stale cycle、Git-tracked)
│
├── features/               # 商品特徴やカテゴリ別のデータセット (Git-tracked)
├── products/               # クリーニング済みの共通商品情報マスタ (Git-tracked)
└── schema/                 # データの整合性を検証するための JSON Schema (Git-tracked)
```

---

## 2. Git追跡（Tracked）と非追跡（Untracked / Git-ignored）の判定基準

本プロジェクトでは、データの再現性とCI/CDパイプラインの独立性を確保するため、以下の基準に従ってGitの管理可否を決定します。

### ✅ Gitで追跡（Tracked）にすべきもの
* **特徴**: **「決定論的に自動再生成できないもの」**、または**「APIトークンの消費やレートリミットがあり、ビルドの都度取得するのが現実的でない外部データ」**。
* **具体例**:
  * **Julesが生成した記事JSON (`data/articles/*.json`)**: エージェントが思考・作成した成果物であり、自動的に再生成することはできません。
  * **クローラーが取得した永続的な外部キャッシュ (`data/raw/per_asin/**/*.json`)**: APIのコール制限（Amazon PA-API / 楽天APIなど）があり、HugoでのHTMLビルド（`build_post.py`）のたびにリアルタイムAPIリクエストを行うと、クォータ制限を超過したりビルド速度が著しく低下するため、Gitにキャッシュとしてコミットして再利用します。これらはクローラー（`fetch_*.py`）によって定期的に安全に更新されます。

### ❌ Git非追跡（Untracked / `.gitignore`）にすべきもの
* **特徴**: **「Gitにある別の入力データから、ローカルのスクリプト実行だけでいつでも決定論的に（同じ内容で）自動再生成できるもの」**。
* **具体例**:
  * **`data/articles/*.quality.json`**: これは `quality_gate.py` が記事JSONをバリデーションした結果を出力するレポートファイルです。入力である記事JSONがあればいつでも再生成できるため、Gitにコミットする必要はありません（コミット差分を肥大化させる原因になります）。
  * **Hugoがビルド時に生成する中間生成物・HTML群**。

---

## 3. 新規外部API / データソースの統合手順

Trends API、Wikipedia API、GitHub APIなど、新しい外部データソースをシステムに組み込む場合は、以下の手順に厳密に従ってください。

### Step 1: クローラー（`fetch_<source>.py`）の作成
* `scripts/fetch_<source>.py` を新規作成します（既存の `fetch_youtube.py` や `fetch_omcha_related.py` が最も参考になります）。
* **原則**: 外部APIへのライブ接続（ネットワーク通信）を行うのは、この **`fetch_*.py` スクリプトの実行フェーズに限定** します。
* データの取得効率を高めるため、`scripts/_fetch_targets.py` の `pick_target_asins` や `mark_queried` を利用して、古いキャッシュ（staleサイクルが切れたもの、通常は7日間）から優先的に取得する「stales-first」サイクルを実装します。

### Step 2: データの出力先設計
* 取得したデータは必ず `data/raw/per_asin/<ASIN>/<source>.json` に出力し、Gitで追跡可能（Git-tracked）とします。
* これにより、後続の静的サイトビルドがネットワークアクセスなしに安全にデータを読み込めるようになります。

### Step 3: CI/CDワークフロー（GitHub Actions）への組み込み
* クローラーを自動稼働させるために、以下の設定を更新します。
  1. `.github/workflows/01-fetch-products.yml` に新規ステップを追加し、定期的にデータを取得して自動でコミット・プッシュされるようにします。
  2. `.github/workflows/04-validate-article-pr.yml` の `paths` に、作成したクローラーのスクリプトパスを追加し、スクリプトの修正時にバリデーションCIが動くようにします。

### Step 4: ビルド・スコアリング層からの読み込み
* `build_post.py` や `score_calculator.py` などのスコアリング・ビルド層は、上記の `data/raw/per_asin/<ASIN>/<source>.json` を **ファイルとしてローカルで読み込むだけ** にします。
* ビルド中にリアルタイムにHTTPリクエストを送ることは**厳禁**です。

---

## 4. トラブルシューティングガイド

### ❓ `per_asin` 配下の特定のファイルが Git 差分として残らない（untracked のままである）
* **原因**: そのファイルが `.gitignore` に登録されているか、あるいはクローラー（fetch）レイヤーで生成されておらず、ビルド（build_post）レイヤーで一時ファイルとして作成されてしまっている可能性があります（以前発生した `omcha_related.json` の設計バグと同様のケース）。
* **対策**: ファイルの生成が `scripts/fetch_*.py` で行われており、そこから Git への追加（`git add`）対象に含まれていることを確認してください。

### ❓ ビルド時に「API制限」や「タイムアウトエラー」が発生する
* **原因**: `build_post.py` または `score_calculator.py` の中で、キャッシュファイルを介さずに直接外部APIにリアルタイムHTTPリクエストを行っています。
* **対策**: APIリクエストを行うコードを `scripts/fetch_*.py` に完全に切り離し、ビルド処理はローカルの `data/` 配下のJSONデータをパースするだけの設計に修正してください。

### ❓ 特定のASINのスコアが更新されない、または古いデータが使われ続ける
* **原因**: クローラーの stales-first クエリ処理（`_fetch_state.json` の日付管理）が正しく機能していない、またはワークフローでのフェッチジョブが失敗しています。
* **対策**: `data/raw/_fetch_state.json` の中に該当ASINのエントリとタイムスタンプが記録されているか、またワークフローの実行ログでエラーが発生していないかを確認してください。
