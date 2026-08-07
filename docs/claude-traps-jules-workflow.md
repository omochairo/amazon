# Jules 連携 トラップ集

Jules（記事生成AIエージェント）のセッション運用・workflow配線まわりのトラップ。
`03-invoke-jules.yml` / `14-brand-narrative.yml` を触る前に確認する。

## Create Jules Session が FAILED_PRECONDITION を返す場合
最有力の真因は **Jules APIキーがstale/スコープ低下し、`sessions.create`権限を失った**こと。

症状の特徴: `sources.list`はHTTP 200で正常（認証自体は通る）／`sessions.create`のみ
HTTP 400/FAILED_PRECONDITION／レスポンスヘッダに`X-RateLimit-*`等の手がかりが一切無い／
payload・source形式・promptを変えても結果不変／Web UI (jules.google.com) では手動セッション
は正常動作する。

**対応手順（時間効率の良い順）:**
1. **最優先: Jules Web UIでAPIキーを再発行** → GitHub Secrets `JULES_API_KEY`を更新 →
   再実行。これでHTTP 200が返れば確定（所要5分）
2. それでもダメならpayload切り分け: `automationMode`/`title`を削除して最小payloadに、
   source形式を変える（**スラッシュ形式が正解** — 公式curl例の`sources/github-myorg-myrepo`
   ハイフン形式は誤りか別フォーマット。実際はList APIが返す`sources/github/owner/repo`を
   使う）、prompt内の`{ }`を排除
3. 最終手段: `error.message: "Precondition check failed."`をJulesサポートに問合せ

**やってはいけないこと**: payload修正だけを延々試す（4本の切り分けPRを出してAPIキーが
原因と判明した実例あり — 最初にAPIキー再発行を試せば5分で終わった）。「数分前は動いていた」
を「直前のマージが原因」と早合点しない（AGENTS.md等のマージはAPIキーには無関係、原因は
Jules側の運用変更のことが多い）。

## brand-narrative の連続dispatchは重複生成を招く
`14-brand-narrative.yml`の「Wait for completion」ステップは最大5分でポーリング打ち切り、
Jules本体の生成は非同期継続する。session作成からPR作成→merge完了までは実際8分前後
かかる。selector (`select_brand_narrative_target.py`)はcheckout時点の
`hugo/content/brands/*/_index.md`を見て「未生成」の最古ブランドを選ぶため、前回dispatch
のPRがまだmergeされていない状態で次をdispatchすると**同一ブランドが再選択され重複生成
される**（実害は軽微だがJules quotaの無駄）。

**How to apply**: 手動連続dispatchする場合は`gh pr list -R omochairo/amazon --state all
--limit N`で前回dispatch分のPRがMERGEDになったことを確認してから次をdispatchする。目安は
session作成から8〜10分（まれに14分程度まで伸びる）。selector側に「in-flight Jules
sessionのブランドを除外する」ガードは無い。

**効率的な待ち方**: dispatch→run完了待ち→ログからbrand/session_id抽出→session_idを
headRefNameに含むPRのMERGEDをポーリング、を1本のスクリプトにまとめてラウンドあたり
dispatch+監視の2ステップに収める。PRのheadRefNameはJulesが独自ロジックでローマ字化した
ブランド名になる（例: 「サンスター文具」→`sansutaabungu`）ため、ブランド名文字列でなく
**必ずJules session id**を`headRefName`のcontainsで照合すること。

## 「実装完了」と「本番で効いている」は別物 — 呼び出し元まで確認する
新しいロジックを既存スクリプト/関数に実装しテストが通っても、そのスクリプトを実際に
呼んでいる本番workflowがどれかを確認するまで完了と見なさない。

実例: `build_jules_prompt.py`に監査ノート注入機能を実装しテストも通したが、この関数を
呼ぶのはGitLab移行用の別経路だけで、実際に稼働している主力workflow
`03-invoke-jules.yml`は同名ロジックをYAML内でシェル+jqにより**独自にインライン実装**して
おり、新機能を一切呼んでいなかった。「実装・テスト・PRマージまで完了」した機能が、1日
経っても実記事に反映されない状態で放置されるところだった。

**How to apply**:
- 新機能/修正を「実装→テスト→PRマージ」で完了扱いにしない。そのコードが実際にどの
  workflow/cron/エントリポイントから呼ばれるかを
  `grep -rn <function_or_script_name> .github/workflows/` 等で確認するのを最終ステップに
  必ず含める
- 同じ目的のロジックが複数経路に存在する設計（「GitHub native」と「GitLab repoless」等の
  並行経路）では、変更が意図した経路に届いているか特に疑う
- 実装が正しく機能しているかは、可能な限り実際にdispatch/cron実行させて生成物（実記事
  JSON）の中身を見て確認する。テストのグリーンだけでは配線ミスは検出できない
- 既存メモリ/docsに「要確認」「検討する価値がある」と書かれている懸念は、後続作業で
  積み残しにせず、関連作業に着手する前に読み返してケリをつける
