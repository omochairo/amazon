# GitHub Actions / git 運用 トラップ集

このリポジトリの CI・ブランチ保護・自動化パイプラインまわりで実際に踏んだトラップ。
新規 workflow を書く・既存の validate/required check を触る前に確認する。

## 作業開始前の git ルール

**作業を開始する前に、必ず `git fetch origin main` で最新を確認する。**

```bash
git -C amazon-clone fetch origin main
git -C amazon-clone log --oneline origin/main -10
git -C amazon-clone checkout main && git -C amazon-clone pull
git -C amazon-clone checkout -b <branch> origin/main   # ブランチは origin/main から切る
```

**Why:** `.github/workflows/03-invoke-jules.yml`（`workflow_run` トリガー）により、main への
マージ → Jules Publish Pipeline 成功 → Jules workflow 起動 → 新記事自動生成 → PR化 →
マージ、という**自動連鎖**が動く。ユーザーの直接操作なしに main がどんどん前進する
（放置すると数十コミット遅れることもある）。

**診断シグナル**: 「PR #xxxx マージ済み・配線済み」のはずのコードが手元に無い、
`git merge-base --is-ancestor <mergeCommit> HEAD` が NOT-ancestor、`git show <oid>` が
"Not a valid commit name" を返す — revert を疑う前に**まず fetch 遅れを疑う**。
`git rev-list --count HEAD..origin/main` が 0 でなければ `git merge --ff-only origin/main`。

## main 保護 / push 系トラップ

### 自分のトークンでの push は main protection をすり抜けることがある
main protection の安全弁 (GH006 / silent_push_reject) は `git-auto-commit-action` のような
bot push には効くが、**対話的な gh CLI（自分のトークン）経由では効かずに main へ直接
push が通ってしまう**ことがある。

対策: add/commit/push 直前に必ず `git branch --show-current` を確認する。`git checkout main`
（pull/log確認のため）を実行した後は、作業branchに戻ったか必ず確認する。push後に気づいたら
害がなければ報告して残す、害があればrevert PRで打ち消す（force pushは禁忌）。

### `continue-on-error: true` 付きの直push actionはGH006を隠す
main branch protection が有効な repo で `stefanzweifel/git-auto-commit-action` 等の**直push
action**を `continue-on-error: true` 付きで使うと、GH006 (`required status check "validate" is
expected`) で拒否されるが**workflow全体はSUCCESSで返る**。生成物が捨てられていることに
気付けない。

実例: `02-publish.yml` の "Commit Generated Data" step が per_asin JSON を main に直push
しようとして毎回silently reject。新ASINの `per_asin/news.json` 等が永久に作られず、直近20記事
中17件で `youtube_embeds: []` になっていた（真因発見まで数時間）。

診断: 「stepは通っているのに生成物がmainに居ない」を見たら、まず
`gh run view <run-id> --log | grep -E "(GH006|protected branch|rejected|denied)"` を確認する。

修正パターン: **App token + PR + auto-merge**（`01-fetch-products.yml` がリファレンス）。
`continue-on-error: true` は新規workflowには付けない（失敗を表に出して検知できるようにする）。
同じ罠を踏みそうな他action: `git push origin main` を生bashで書いたstep。

### cron workflow が hugo/ を main に反映するには3点セットが要る
1. **App token**（`actions/create-github-app-token@v1`）— GITHUB_TOKEN だとPR作成不可
2. **`git add -f`** — `hugo/` は `.gitignore` の `/hugo` でignoredなのでforce addしないと
   stageされない
3. **`git diff --cached --quiet`** — staging後の判定は必ず `--cached`。working tree比較
   (`git diff --quiet path/`) はignoredファイルが見えず"No changes"に誤判定する

1つでも欠けるとsilent failする（08-wiki-cache.yml が3段階で順に踏んだ実例、PR #688/#689/#690）。

## required check の設計

### required check を `paths` フィルタ付きworkflowで提供してはいけない
発火条件を workflow の `on.pull_request.paths` で絞ると、**未登録ファイルだけを触るPRは
check自体が起動せず永久pending = マージ不能**になる。判定は「起動するか」ではなく
「起動した上で何をするか」に置く。

`04-validate-article-pr.yml` の allowlist は224エントリ/552行まで肥大し、7回この罠を踏んだ
（新script追加のたびの登録漏れ）。**2026-08-02 に `paths:` を丸ごと撤廃して解決** — validate
jobは元々base..head差分で対象ファイルを探し、無ければ`exit 0`する設計なので、`paths`が
無くても無関係PRは同じvacuous SUCCESS経路を通る（実測42〜45秒、repoはpublicでActions無料）。

**`paths:` を足し戻さないこと。** 新しくrequired checkを作るときも同様に、paths で絞らず
job内で「対象ファイルが変わったか」を見てearly returnする。

**`workflow_dispatch` はrequired checkの代わりにならない。** check-runs APIにはsuccessが
出るが、combined commit status APIでは空のままでbranch protectionは満たされない。

required contexts確認: `gh api repos/omochairo/amazon/branches/main/protection --jq
'.required_status_checks.contexts'`

### `gh pr merge --auto` 有効化後の追加pushは反映されない
`gh pr merge --auto` を有効化してから新しいコミットをpushすると、最初のrequired checkが
successになった瞬間にPRがmergeされ、その後のpushは反映されない（branchだけに残る）。
GitHubのauto-mergeは「最初にrequired checkがgreenになったHEAD SHA」をmergeする仕様。

対策: 複数コミットで構成するPRは (1) **全コミットをpushしてから** `--auto` を有効化する
のが基本、(2) 既に有効なPRに追加commitが必要なら `gh pr merge <N> --disable-auto` で解除
→push→再度有効化、(3) 諦めてfollow-up PRにする。validateが~30秒で終わるためrace windowは
極めて狭い。

### `peter-evans/create-pull-request` はGITHUB_TOKEN由来PRでvalidateが発火しない
GitHub Actionsの仕様（recursive workflow trigger防止）で、GITHUB_TOKENで作成されたPR/pushは
ダウンストリームworkflowをトリガしない。`peter-evans/create-pull-request@v6` で自動起票した
PRは起票直後に `pull_request` トリガのworkflowが一切走らず、auto-mergeがBLOCKEDのまま
停止する。`gh pr close && gh pr reopen` も無効（reopenイベントはGITHUB_TOKEN由来の最終commit
には発火しない）。

**恒久対策**: 新規にpeter-evans/create-pull-requestを導入するworkflowは最初から**App
token**（`actions/create-github-app-token@v1` + `APP_ID`/`APP_PRIVATE_KEY` secrets）を使う。
チェック手順: `gh -R <repo> run list --branch <pr-branch> --limit 5` が空配列を返したら罠を
踏んでいる。

対症療法（App token化するまでの間）:
```bash
G="git -C <repo>"
$G fetch origin <pr-branch>:<local-ref>
TREE=$($G rev-parse <local-ref>^{tree})
PARENT=$($G rev-parse <local-ref>)
NEW=$(echo "chore: re-trigger validate" | $G -c commit.gpgsign=false commit-tree $TREE -p $PARENT)
$G update-ref refs/heads/<local-ref> $NEW
$G push origin <local-ref>:<pr-branch>
```

### 新規workflowはdefault branchに乗るまでdispatchできない
新規追加したworkflowファイルは、**main に存在するまで** `gh workflow run <file> --ref
<branch>` が `HTTP 404: workflow not found on the default branch` で失敗する
（workflow_dispatchの対象はdefault branch登録済みworkflowに限られるGitHub仕様）。

「merge前にActionsで動作確認」は原則できない。検証はmerge後にmainでdispatchする。先merge
の可否は失敗時のblast radiusで判断する（no-opで安全なら merge-then-verify、破壊的・不可逆
なら別経路を検討）。

## workflow YAML / 運用

### workflow YAMLに絵文字を入れると全push event workflowがfailureになる
`.github/workflows/*.yml` に4-byte UTF-8絵文字（U+10000以上）を入れると GitHub Actions の
YAML parserがplain scalarとして拒否し、**全push eventのworkflow triggerがfailure**になる
（対象workflowだけでなく無関係workflowまで巻き込む）。

装飾したいなら `:rocket:` のようなASCII shortcodeか、必ずdouble-quoteした scalar
（`"🚀"`）を使う。事前検証: `python -c "import yaml; yaml.safe_load(open('.github/workflows/
foo.yml'))"`。「pushしたら無関係workflowまで全部failure」を見たらこれを疑う。

### Issue Formsのlabelsは存在しないlabelを指定するとsubmit失敗する
`.github/ISSUE_TEMPLATE/*.yml` の `labels:` 配列に repo に存在しないlabelを指定すると、
ユーザーがsubmit時にエラーになり起票失敗する。GitHubはlabelを自動作成しない
（`link-report` label未作成のまま template だけ merge され、実報告ゼロで長期間気づかれ
なかった実例）。

新規Issue Forms template追加PRでは必ず: (1) `gh label list -R omochairo/amazon | grep
<label>` で存在確認、(2) 未作成なら `gh label create <label> -R omochairo/amazon --color
XXXXXX --description "..."`、(3) PR説明文に作成済みと明記。

### 長期disable/凍結明けのworkflowはYAML破損とschedule非再開に注意
- **disabled中のworkflowはYAML評価対象外**。`gh workflow enable` した瞬間に初めて評価され、
  中の構文エラー（`run: |` ブロックのインデント欠落等）が push 毎の即failとして表面化する。
  enable前後に必ず妥当性を確認する:
  ```bash
  python -c "import yaml; yaml.safe_load(open('.github/workflows/<file>.yml', encoding='utf-8'))"
  ```
  （※ YAMLとして読めるかしか分からない。bash/python埋め込みブロックのインデントずれ等の
  意味的破損は別途目視が要る）
- **長期凍結解除だけではschedule cronが自動再開しない。** GitHubはdefault branchへのpush
  （特にworkflowファイル自体の変更）を契機にscheduleを再インデックスする挙動があり、凍結
  解除だけでは内部cron登録が復元されない。復活手順: (1) scheduleトリガを持つ全workflow
  にダミーコメント行を追加してpush、(2) 次のcron予定時刻を2つチェックポイントとして
  `event: schedule` での発火を実測確認、(3) 確認できてから初めてcron復活とみなし以降の
  enableフェーズに進む（確認前に大量enableしない）。

### `scripts.xxx` を相互importするscriptは `python -m` で実行する
`from scripts.xxx import ...` のように同ディレクトリの他スクリプトを絶対importする
scriptを `python scripts/foo.py` で直接実行すると `ModuleNotFoundError: No module named
'scripts'` になる（`python <path>` 実行時は `sys.path[0]` がスクリプト自身のディレクトリ
になり repo root が乗らないため）。

`scripts/` 配下の他モジュールをimportするscriptは、CI/workflowからの呼び出しを必ず
`python -m scripts.<module名>`（repo rootがcwd）にする。`python scripts/<module名>.py`
直接実行は禁止。ローカル動作確認も同様に `python -m scripts.<module名> --help` で行う。

## エージェント委譲時の git 運用

### サブエージェントに stash させると無関係な変更が混入しうる
サブエージェントに実装を委譲し、既存の作業ブランチ（別タスクの未マージ変更あり）から
新規ブランチを切らせる場合、`git stash`/`stash pop`の手順を踏むと**無関係な作業ディレクト
リの変更が新ブランチに混入する**ことがある。実例: 「対象3ファイルのHEADがorigin/mainと
一致することを確認した上でstash→checkout -b→stash popした」という報告だったが、実際には
作業ディレクトリに残っていた無関係な未コミット変更（日次cronが書き込むデータファイル、
巻き戻り方向）がstashに巻き込まれてPRにコミットされ、マージされればSNS重複投稿リスクが
あった。サブエージェントの「確認済み」報告（`git show HEAD --stat`）はファイル名一覧
レベルの確認で、中身の意味的妥当性までは見ていなかった。

**How to apply**:
- サブエージェントに「ブランチを切って作業」を指示するときは、可能なら**まず
  `git status`/`git diff`が空であることを確認してから`checkout -b`させる**よう指示する
  （stashが必要なほど汚れた作業ディレクトリを引き継がせない）
- 委譲先が「意図しないファイルが紛れていないか事前に確認した」と報告してきても、レビュー
  側で改めて`gh pr diff --stat`の全ファイルに目を通し、**特にデータファイル（jsonl/json
  等、他の自動化が定期更新するもの）は中身のdiffまで開いて意味を確認する**（ファイル名
  一致だけでは巻き戻りか前進か分からない）
- 発見したら`git checkout origin/main -- <file>`で復元し追加コミット。ただしGitHubのPR
  files APIは「PRのmerge-baseからの累積diff」を表示するため、復元後も
  `additions/deletions`が0にならないことがある（実質差分ゼロは
  `git diff origin/main..HEAD`で確認すべきで、PR statの数字だけで判断しない）

## Windows ローカル実行の注意

### `sys.stdout.write()` の日本語がcp932で化ける/クラッシュする
Windowsローカル環境ではPythonの`sys.stdout.encoding`が既定で`cp932`になっており、日本語
文字列を`sys.stdout.write()`に渡すと意図せずcp932エンコードされたバイト列が出力される
（GitHub Actions ubuntu-latestはUTF-8既定のため問題にならない）。別の顕在化として、
`print()`が`UnicodeEncodeError: 'cp932' codec can't encode character '—'`で**即クラッシュ**
することもある（em dash等がcp932非対応のため）。

**How to apply**:
- Windowsローカルで日本語出力する既存スクリプトを動かすときは、まず
  `PYTHONIOENCODING=utf-8` を前置する（スクリプト修正不要）
- 日本語を扱うPython CLIで`sys.stdout.write()`を直接使う箇所は、`main()`冒頭で
  `if hasattr(sys.stdout, "reconfigure"): sys.stdout.reconfigure(encoding="utf-8")` を
  入れておくとWindowsローカル実行でも安全になる（GitHub Actions側への影響なし）
- ローカルで日本語出力を検証するときは、ターミナル表示の文字化け（cp932端末がUTF-8
  テキストを正しく表示できないだけ）と実際にバイト列が壊れているバグを区別する。
  `repr(stdout_bytes[:20])`や`stdout_bytes.decode('utf-8')`の成否で判定し、「表示が変」
  だけで即バグと判断しない

## GitHub API の特性

### Search API はApp token経由だと新規issueの可視性が著しく遅れる
`gh api search/issues` は書き込み系操作の直後に叩くと、実在するissueが一時的に0件で
ヒットしないことがある。既存issueへの直前の書き込み（コメント/PR作成）直後のラグは
数秒〜数十秒だが、**issueそのものを新規作成した直後**は、App installation token 経由の
検索で **30分〜数日**（実質見えない）0件を返し続けることを実測した。個人/PATトークン経由
では数分後に正しくヒットする。

**How to apply:**
- 既存issueへの直前書き込みラグには短いリトライ（2-3秒×3回）が効くが、新規issue自体の
  可視性ラグには無力（数十分〜数日リトライするのは非現実的）
- 新しく作ったissueを同一job/直後のworkflow runでApp token searchから見つけようとする
  設計は避ける。「見つからなければ安全にskipして次回に委ねる」設計にする
- **固定の既知issue/PRをSearchで引く**箇所は番号直指定に倒す（`DEFAULT_TRACKER_ISSUE`の
  ように既定値化し、`--issue-number`で上書き可能にする）。Searchが必要なのは対象が可変
  （per-URL issue等・かつ別botが前日以前に作成済み）のときだけ

### App tokenでの issue 書き込みは `gh` サブコマンドでなく REST に統一する
`gh issue comment N` は GraphQL `addComment` を叩くが、App installation token 経由だと
理由不明のまま exit 1 で落ちることがある（同一run・同一App token・1秒差で `gh issue create`
は成功し `gh issue comment` だけ失敗した実例。GraphQL全落ち説・権限不足説とも矛盾し
真因は未確定のまま調査を打ち切った）。`gh issue create --label` のlabel付与が0件になる
事象も併発した。

**How to apply:**
1. App installation tokenで回す自動化は、issueへの書き込みを `gh` サブコマンドではなく
   **REST** (`gh api --method POST repos/{repo}/issues[/N/comments]`) に統一する。本文は
   argv でなく **stdin から JSON** で渡し、labelsはREST bodyの配列に同梱する
   （実装例: `scripts/gh_rest.py` の `run_gh`/`post_issue_comment`）
2. subprocess/gh呼び出しの失敗時は必ずstderrをログに出す。`capture_output` の握り潰しが
   原因調査を長期化させた
3. App token周りの疑いが出たら `amazon-home-ops/.github/workflows/91-gh-token-probe.yml`
   （workflow_dispatchのみ・読み取り専用の診断workflow）をまず回す。同じ調査を二度しない
