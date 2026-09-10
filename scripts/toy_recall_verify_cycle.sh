#!/usr/bin/env bash
# #4320 follow-up: 消費者庁リコール候補 (37-toy-recall-monitor.yml が月次で作る
# GitHub Actions artifact) を、この K8 ホストでしか動かせない agy による
# ASIN 突合 (scripts/verify_toy_recall_matches.py) にかけ、一致が見つかった分
# だけ既存の toy-recall Issue にコメントする (scripts/open_toy_recall_issue.py
# --matches)。systemd timer (toy-recall-verify.timer) から日次で叩かれる想定。
#
# なぜ GitHub Actions 側でやらないか: agy はこのホストの aisys ユーザーの
# ファイルベース認証に紐づいており、GitHub Actions のクラウド実行からは
# 呼べない。fetch (recall.caa.go.jp のスクレイピング) はクラウド側のままにして
# 二重に外部サイトを叩かない — ここでは fetch 済みの artifact をダウンロード
# するだけ。
#
# なぜ専用の checkout を使うか: 共有の作業ツリー (/root/k8work/amazon) は
# 人間やエージェントが数十分おきにブランチを切り替える (docs/CLAUDE.md の
# 実測: 7.5時間で13回)。無人の定期実行がそこに相乗りすると、途中でブランチが
# 変わって違う data/articles を読んだり、作業中のファイルが消えたりする事故が
# 起きうる。読み取り専用の完全に別 clone を持ち、毎回 origin/main に
# fast-forward してから使う。
set -euo pipefail

REPO="omochairo/amazon"
WORKFLOW="37-toy-recall-monitor.yml"
ARTIFACT_NAME="toy-recall-candidates"
CHECKOUT_DIR="${TOY_RECALL_CHECKOUT_DIR:-/root/k8work/toy-recall-verify-checkout}"
STATE_FILE="${TOY_RECALL_STATE_FILE:-/root/.omochairo/toy_recall_last_verified_run}"
MODEL="${ANTIGRAVITY_MODEL:-gemini-3.8-flash-high}"

log() { echo "[toy_recall_verify_cycle] $*" >&2; }

mkdir -p "$(dirname "$STATE_FILE")"

if [ ! -d "$CHECKOUT_DIR/.git" ]; then
    log "cloning dedicated checkout -> $CHECKOUT_DIR"
    git clone --quiet "https://github.com/${REPO}.git" "$CHECKOUT_DIR"
fi
git -C "$CHECKOUT_DIR" fetch --quiet origin main
git -C "$CHECKOUT_DIR" checkout --quiet --detach origin/main

RUN_ID=$(gh run list -R "$REPO" --workflow="$WORKFLOW" --status=success --limit 1 \
    --json databaseId --jq '.[0].databaseId')
if [ -z "${RUN_ID:-}" ] || [ "$RUN_ID" = "null" ]; then
    log "no successful $WORKFLOW run found — nothing to do"
    exit 0
fi

LAST_RUN_ID=""
[ -f "$STATE_FILE" ] && LAST_RUN_ID="$(cat "$STATE_FILE")"
if [ "$RUN_ID" = "$LAST_RUN_ID" ]; then
    log "run $RUN_ID already processed — nothing to do"
    exit 0
fi

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

if ! gh run download "$RUN_ID" -R "$REPO" -n "$ARTIFACT_NAME" -D "$WORKDIR"; then
    log "no '$ARTIFACT_NAME' artifact on run $RUN_ID (likely 0 candidates that month) — marking processed"
    echo "$RUN_ID" > "$STATE_FILE"
    exit 0
fi

CANDIDATES_JSON="$WORKDIR/toy_recall_candidates.json"
if [ ! -f "$CANDIDATES_JSON" ]; then
    log "downloaded artifact missing expected file — marking processed without verifying"
    echo "$RUN_ID" > "$STATE_FILE"
    exit 0
fi

cd "$CHECKOUT_DIR"
log "verifying candidates from run $RUN_ID with agy ($MODEL)"
python3 scripts/verify_toy_recall_matches.py \
    --input "$CANDIDATES_JSON" \
    --out "$WORKDIR/toy_recall_matches.json" \
    --model "$MODEL"

log "posting confirmed matches (if any) to the open toy-recall Issue"
python3 scripts/open_toy_recall_issue.py \
    --matches "$WORKDIR/toy_recall_matches.json" \
    --repo "$REPO" \
    --model "$MODEL"

echo "$RUN_ID" > "$STATE_FILE"
log "done (run $RUN_ID marked processed)"
