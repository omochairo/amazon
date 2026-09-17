# SNS 返信 inbox レーン

X / Threads / Bluesky に投稿しても、返ってきた返信に気付けず放置になる。
それを検出 → 起草 → 人手承認 → 送信 の 4 段で潰すレーン。

## 実測 (2026-09-05, run 33966085054)

`scripts/probe_sns_inbox.py` を 1 回 dispatch して確定させた事実。

| チャネル | 返信を読めるか | 根拠 |
|---|---|---|
| Threads | **読める** | 現行 `THREADS_ACCESS_TOKEN` のまま `GET /{media-id}/replies` が HTTP 200。再認可不要 |
| Threads (会話の続き) | `/replies` では**読めない** | `/replies` は top-level の返信しか返さない。実際に叩くのは `/conversation` (深さに関係なく flat に全部返す) |
| Bluesky | **読める** | `app.bsky.notification.listNotifications` が 200。reply / mention / quote を取得できる |
| X | **Buffer からは読めない** | Buffer GraphQL の Query root は 13 個 (`account` / `channel(s)` / `post(s)` / `contentItem(s)` / `idea*` / `postTemplate*` / `aggregatedPostMetrics` / `dailyPostingLimits`) だけ。281 型を走査しても reply / comment / mention / conversation は**型ごと存在しない** |

X の返信を取る経路は X 公式 API (`GET /2/users/{id}/mentions`) しかない。
2026-02 に従量課金へ移行し、Basic ($200/月) は廃止。自分のメンション取得は
owned read 扱いで **$0.001/件**。`X_BEARER_TOKEN` + `X_USER_ID` を設定した
ときだけアダプタが有効化される (未設定なら skip)。

なお X への**返信送信**は別問題で、`POST /2/tweets` が user-context 認証
(OAuth 1.0a / OAuth 2.0 PKCE) を要求するため bearer だけでは投げられない。
`post_sns_reply.py` は明示的に「未配線」として失敗する。

## 4 段

| 段 | スクリプト | 実行場所 |
|---|---|---|
| 検出 | `scripts/fetch_sns_replies.py` | GitHub hosted (`amazon-home-ops` の cron) |
| 起草 | `scripts/draft_sns_reply.py` | **K8 LLM ワーカー** (agy の Claude を owner 定額クォータで使う) |
| 通知 | `scripts/sync_sns_inbox_issues.py` が `amazon-home-ops` に issue を立てる (+ ntfy) | GitHub hosted |
| 送信 | `scripts/post_sns_reply.py` | **人手承認後に手動 dispatch のみ** |

状態は `scripts/sns_inbox_store.py` の inbox (JSONL) が唯一の受け渡し面。

## 通知を issue にしている理由 (#7589)

ntfy push と `PENDING.md` だけだった頃、**どちらも「開かないと何も起きない」**ので
放置された。2026-09-17 の実測で、未対応 4 件はいずれも案が出来ているのに、送信まで至ったのは
通算 1 件だけだった。

未対応 1 件につき `amazon-home-ops` に issue を 1 本立てる。送信 (`answered`) /
見送り (`ignored`) で close されるので、**open 件数がそのまま未対応件数**になる。
ntfy は残してあるが、気付く主経路は GitHub の issue 通知。

- 起票先は private 限定。`--repo` / `SNS_ISSUE_REPO` の明示が要り、
  `omochairo/amazon` (public) を指すと exit 2 で拒否する
- 1 run で立てるのは既定 5 本まで。現在の流量 (数件) では当たらないが、
  取りこぼしを後から一括で流すときに効く。GitHub API のバースト起票は禁止
  (2026-06-25 のアカウント凍結)
- **二重起票の防波堤は 2 重**: レコードの `issue_number` と、issue 本文の
  `<!-- sns-inbox-id: … -->` マーカー。前者は commit → push が要るので、
  「起票は成功したが commit 前に落ちた」窓をマーカー照合で塞ぐ
- **返さないと決めたら issue を close するだけでよい。** 次の同期で inbox 側が
  `ignored` になり、未対応から外れる。同じ返信で立て直されることもない
  (`issue_number` とマーカーの両方で既知として扱う)。
  人が `ignored` を付ける手段は他に無い — 起草側の LLM が「返信しない」と
  判断した経路しか無かったので、close をその意思表示として読む。
  誤って閉じても取り返せる (store は `ignored -> answered` を許す)

## なぜ自動送信しないか

誤爆したとき取り返しがつかない。相手のいるやり取りで、投稿を削除しても
相手の通知には残る。ペルソナ (いろママ / いろパパ) を名乗って別人格の返事を
送るのは、放置よりダメージが大きい。起草までを自動化し、送信は人が本文を
確定させてから 1 件ずつ行う。

## 置き場所の制約 (重要)

inbox の中身は**第三者が書いた本文とハンドル名**。`omochairo/amazon` は
public なので、**このリポジトリにコミットしてはならない**。

- 保存先は `SNS_INBOX_DIR`。本番は private リポジトリ (`amazon-home-ops`) の
  checkout 内を指す
- 未指定時の既定は `tmp/sns_inbox/` で、`.gitignore` 済み。
  `scripts/tests/test_sns_inbox_store.py` が `git check-ignore` で実際に
  確認しているので、除外が外れるとテストが落ちる
- ペルソナ定義 (`jules/PROMPT_ENGAGEMENT_*`) は `amazon-navi-brain` の資産で、
  public 側に複製しない。overlay が無い環境では起草を**拒否**する
  (適当なペルソナで書くより書かない方が良い)

## 会話が 1 往復で切れないようにする (#7589)

Threads で「こちらが返信 → 相手がさらに返信」を拾うには `/conversation` が要る。

- `GET /{media-id}/replies` は **top-level の返信しか返さない**
  (Threads API docs: "only returns the top-level replies")。こちらの返信への
  返信は depth 2 なので、この経路では永久に見えない
- `GET /{user-id}/threads` は **自分の返信を含まない** (別エッジが要る)。
  つまり「自分の返信を起点に辿る」形にも逃げられない
- したがって自分の投稿を根として `/conversation` を読むのが唯一の経路。
  深さに関係なく flat に全部返るので、何往復しても拾える

Bluesky は `listNotifications` が深さに関係なく通知を返すので元から影響なし。

## lookback を名目どおり効かせる / 第三者同士の会話を拾わない (#7609)

#7589 の配線後の最終チェックで見つかった、Threads 固有の穴 2 件。

**lookback 14 日が実質 約 2.8 日しか効いていなかった。** `GET /{user-id}/threads`
は 1 ページ最大 25 件で、ページングせずに取っていたため、投稿量 (engagement +
記事レーンで 1 日あたり約 9 media) からすると 25 件 ≒ 2.8 日分で頭打ちになり、
それより前の投稿への返信は検出できていなかった。`_fetch_own_posts` が
`paging.next` を cutoff に届くまで辿るようにして、14 日分を正しく見る。
`MAX_OWN_POSTS_TOTAL` (300) は暴走防止の安全弁で、通常の投稿量なら当たらない。

**`/conversation` は第三者同士の返信も返す。** 深さ関係なく flat に返す代償として、
「自分のスレッド内で A さんが B さんに返した発言」も混ざる。返信の `replied_to.id`
が、根の投稿 (自分の投稿) か自分自身の返信を指しているものだけに絞り、
第三者同士の会話を除外する。`replied_to` が返らない (フィールド欠落) 場合は
フィルタせず従来通り拾う (誤って捨てるより多少ノイズが混じる方を選ぶ)。
**返信先がページに載っていない場合も同じく拾う** — `/conversation` は
`MAX_REPLIES_PER_POST` で切れる (ページングしていない) ため、返信先が
載らないことがある。「知らない = 自分宛でない」と判定すると、自分の返信への
返信を無言で捨てることになり、#7589 で塞いだ穴が別の形で開く。

## 二重返信を防ぐ不変条件

inbox の `status` は `new → drafted → answered / ignored` と一方向にしか
進まない。検出側は**既知 id を一切触らない**。ここが崩れると「返信済みの
相手にもう一度返す」事故になる (SNS 配信で `published_at` の bookkeeping が
遅れて二重投稿になった #4782 と同じ形)。

`ignored` からの `answered` は許す。起草側が「返信しない」と判断したものを、
人が見て返したくなるケースは正当。
