# SNS 返信 inbox レーン

X / Threads / Bluesky に投稿しても、返ってきた返信に気付けず放置になる。
それを検出 → 起草 → 人手承認 → 送信 の 4 段で潰すレーン。

## 実測 (2026-09-05, run 33966085054)

`scripts/probe_sns_inbox.py` を 1 回 dispatch して確定させた事実。

| チャネル | 返信を読めるか | 根拠 |
|---|---|---|
| Threads | **読める** | 現行 `THREADS_ACCESS_TOKEN` のまま `GET /{media-id}/replies` が HTTP 200。再認可不要 |
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
| 通知 | 上記 2 段が ntfy へ push | 同上 |
| 送信 | `scripts/post_sns_reply.py` | **人手承認後に手動 dispatch のみ** |

状態は `scripts/sns_inbox_store.py` の inbox (JSONL) が唯一の受け渡し面。

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

## 二重返信を防ぐ不変条件

inbox の `status` は `new → drafted → answered / ignored` と一方向にしか
進まない。検出側は**既知 id を一切触らない**。ここが崩れると「返信済みの
相手にもう一度返す」事故になる (SNS 配信で `published_at` の bookkeeping が
遅れて二重投稿になった #4782 と同じ形)。

`ignored` からの `answered` は許す。起草側が「返信しない」と判断したものを、
人が見て返したくなるケースは正当。
