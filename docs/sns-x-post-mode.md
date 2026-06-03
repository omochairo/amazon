# X 投稿モード切替 (queue ↔ now) 手順

session 95 (2026-06-04) — engagement 投稿の X 経路を「Buffer キュー積み」から
「Buffer 即時配信 (shareNow)」へ切替できる足回り。

## 背景

- 現状 X 投稿は **Buffer の queue に積む** (`mode: addToQueue`) → Buffer が後で X に流す 2 段。
- news 系は即時性が欲しい。また Buffer キューが 10 件以上滞留すると配信タイミングが読めない。
- **X 公式 API は使わない** (2026 pay-per-usage $0.20/req 回避)。Buffer の `mode: shareNow` は
  検証済み (X live shareNow 配信 OK) なので、Buffer 経由のまま即時化する。

## 仕組み

`scripts/notify_engagement.py` の env `X_POST_MODE`:

| X_POST_MODE | Buffer mode  | 挙動                                    |
|-------------|--------------|-----------------------------------------|
| `queue` (default) | `addToQueue` | 従来。Buffer のキューに積む            |
| `now`       | `shareNow`   | キューを介さず即時配信 (news 即時性確保) |

- 未設定 / 不正値は `queue` に fallback (既存挙動を壊さない)。
- X の weighted char (CJK=2) で 280 超過分は配信前に切る。shareNow は即時で後修正
  不能なので weight guard 必須 (`scripts/_x_text.py` 共有)。
- Threads 経路 (Meta Graph) は本フラグの影響を受けない。

## ワークフロー配線

`.github/workflows/30-sns-engagement.yml` の Publish step に:

```yaml
X_POST_MODE: ${{ vars.X_POST_MODE || 'queue' }}
```

→ **repo variable `X_POST_MODE` を `now` にするだけ**で全 slot が即時配信に切替わる。
変数未設定なら `queue` のまま (現状維持)。

## 切替手順

1. **事前確認 (切替 gate)** — session 94 findings の通り、1 週間 (2026-06-04〜06-10) の
   publish 成功率を確認:
   - daily slot 21 件中 18 件以上成功 (穴開け週 3 件以下)
   - news slot は 33-yml 稼働日に 12 件以上成功
   - Buffer X / Threads とも 4279xxx / GraphQL error なし
2. **dry-run 確認** (ローカル or workflow_dispatch dry_run):
   ```bash
   X_POST_MODE=now python scripts/notify_engagement.py --dry-run --channels x
   ```
   出力に `X_POST_MODE=now -> Buffer mode=shareNow` と weighted char が出れば OK。
3. **1 投稿だけ手動確認** — repo variable は触らず、workflow_dispatch で 1 slot だけ
   即時化したい場合は当面 dry_run=false + 一時的に変数 ON → 投稿確認 → 必要なら戻す。
4. **本切替** — repo variable を設定:
   ```bash
   gh variable set X_POST_MODE -R omochairo/amazon --body now
   ```
   以降の cron は全 X slot が shareNow 即時配信。
5. **戻す**場合:
   ```bash
   gh variable set X_POST_MODE -R omochairo/amazon --body queue
   # または gh variable delete X_POST_MODE -R omochairo/amazon
   ```

## 関連

- `scripts/notify_engagement.py` — X_POST_MODE 実装本体
- `scripts/_x_text.py` — weighted char ヘルパ (notify_buffer.py と共有)
- reference: Buffer `mode: ShareMode!` = addToQueue | shareNow | shareNext | customScheduled | recommendedTime
- 記事投稿 (`scripts/notify_buffer.py`, 20-sns-publish.yml) は別経路。本フラグ対象外。
