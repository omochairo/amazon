#!/bin/sh
# デプロイ後の Cloudflare エッジキャッシュ消去 (#7386)。.gitlab-ci.yml の cf-purge から呼ぶ。
#
# なぜ要るか:
#   以前は main への push ごとに無条件で purge_everything を撃っていた。push は
#   1 日 50〜80 本あり、2026-09-14 は purge が 15 回・間隔の中央値 89 分だった。
#   Cache Rule の Edge TTL (HTML 4h) が実質「次の purge まで」になり、訪問者の
#   HTML の MISS の 43% が「前回リクエストとの間に purge が挟まった」ものだった。
#
# 入力:
#   deploy-nas が保存した `switch` の stdout (navi-switch が切替前の差分を出す。
#   omochairo/amazon-home-ops の docker/navi/bin/navi-switch)。
#     navi-diff-begin
#     navi-diff<TAB><release からの相対パス>
#     navi-diff-end<TAB><件数>
#   または navi-diff-unknown (前世代が無い)。
#
# 判定:
#   - 差分が読めない (ファイルが無い / 古い navi-switch / 件数が合わない) → 全消去
#   - 配信に効くファイルの差分が 0 件                                     → 消去しない
#   - それ以外                                                              → 全消去
#   **読めないときは必ず全消去に倒す。** 消し損ねると古いページがエッジに残るが、
#   消しすぎても MISS が増えるだけで表示は壊れない。
#
# 使いかた:
#   sh scripts/ci_cf_purge.sh navi-switch.out
#   CF_PURGE_DRY_RUN=1 で API を叩かずに判定だけ出す (テスト用)。
set -eu

DIFF="${1:-navi-switch.out}"
TAB=$(printf '\t')

# 配信に効かないので差分から外すパス。
#   build.json: 毎ビルド sha が変わる。Cache Rule で cache bypass 済み (#6205 T9)
IGNORE_RE='^(build\.json)$'

log() { echo "[cf-purge] $*" >&2; }

decide() {
  if [ ! -f "$DIFF" ]; then
    log "差分ファイル $DIFF が無い"; echo all; return
  fi
  if grep -q '^navi-diff-unknown$' "$DIFF"; then
    log "前世代が無い (navi-diff-unknown)"; echo all; return
  fi
  if ! grep -q '^navi-diff-begin$' "$DIFF" || ! grep -q "^navi-diff-end${TAB}" "$DIFF"; then
    log "差分の区切りが無い (NAS の navi-switch が古い可能性)"; echo all; return
  fi
  declared=$(grep "^navi-diff-end${TAB}" "$DIFF" | tail -1 | cut -f2)
  actual=$(grep -c "^navi-diff${TAB}" "$DIFF" || true)
  if [ "$declared" != "$actual" ]; then
    log "差分の件数が合わない (宣言 ${declared} / 実際 ${actual})"; echo all; return
  fi
  effective=$(grep "^navi-diff${TAB}" "$DIFF" | cut -f2- | grep -Evc "$IGNORE_RE" || true)
  log "差分 ${actual} 件 (配信に効くもの ${effective} 件)"
  if [ "$effective" -eq 0 ]; then
    echo skip; return
  fi
  echo all
}

purge_everything() {
  if [ "${CF_PURGE_DRY_RUN:-}" = "1" ]; then
    log "dry-run: purge_everything"; return
  fi
  curl -fsS -X POST "https://api.cloudflare.com/client/v4/zones/${CF_ZONE_ID}/purge_cache" \
    -H "Authorization: Bearer ${CF_PURGE_TOKEN}" \
    -H "Content-Type: application/json" \
    --data '{"purge_everything":true}'
  echo
}

mode=$(decide)
case "$mode" in
  skip) log "配信物に変化が無いので消去しない" ;;
  *)    log "全消去する"; purge_everything ;;
esac
