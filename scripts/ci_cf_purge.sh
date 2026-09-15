#!/bin/sh
# デプロイ後の Cloudflare エッジキャッシュ消去 (#7386)。.gitlab-ci.yml の cf-purge から呼ぶ。
#
# なぜ要るか:
#   以前は main への push ごとに無条件で purge_everything を撃っていた。push は
#   1 日 50〜80 本あり、2026-09-14 は purge が 15 回・間隔の中央値 89 分だった。
#   Cache Rule の Edge TTL (HTML 4h) が実質「次の purge まで」になり、訪問者の
#   HTML の MISS の 43% が「前回リクエストとの間に purge が挟まった」ものだった。
#   実際に変わるファイルは多くのデプロイで 20〜35 件 / 約 21,000 件。
#
# 入力:
#   deploy-nas が保存した `switch` の stdout (navi-switch が切替前の差分を出す。
#   omochairo/amazon-home-ops の docker/navi/bin/navi-switch)。
#     navi-diff-begin
#     navi-diff<TAB><release からの相対パス>     変更・追加・削除のいずれか
#     navi-diff-end<TAB><件数>
#   または navi-diff-unknown (前世代が無い)。
#
# 判定:
#   - 差分が読めない (ファイルが無い / 古い navi-switch / 件数が合わない) → 全消去
#   - 配信に効くファイルの差分が 0 件                                     → 消去しない
#   - URL にできないパスがある / 件数が CF_PURGE_URL_MAX を超える          → 全消去
#   - それ以外                                             → 変わった URL だけ消去
#   - URL 消去の API が 1 回でも失敗した                                   → 全消去
#   **迷ったら必ず全消去に倒す。** 消し損ねると古いページがエッジに残るが、
#   消しすぎても MISS が増えるだけで表示は壊れない。
#
# 削除されたファイルも消去対象に入る (消さないと TTL の間 200 の古いページが残る)。
# 指紋付きの /js/ /assets/ は内容が変わると URL ごと変わるので、差分には新しい URL
# (まだキャッシュに無い) と消えた URL しか出ない。巻き込み消去は起きない。
#
# 使いかた:
#   sh scripts/ci_cf_purge.sh navi-switch.out
#   CF_PURGE_DRY_RUN=1 で API を叩かずに判定と URL だけ出す (テスト用)。
set -eu

DIFF="${1:-navi-switch.out}"
BASE="${CF_PURGE_BASE:-https://navi.omcha.jp}"
# これを超えたら URL 指定をやめて全消去する。テンプレート変更などで全ページが
# 変わるデプロイは 7,000 件を超える (2026-09-15 実測)。その規模なら全消去で差が無い。
# 当初 1,000 にしていたが、取り消されたパイプラインの分がまとまったデプロイ
# (記事追加 5 本 + タグ登録 4 回) が 1,003 件で全消去に倒れた。1,000 件を URL で消しても
# 全体 (約 21,000) の 5% だが、全消去は 100% を捨てるので、全ページ規模の手前まで上げる。
# 5,000 件 = 30 件ずつ 167 回。途中で API が失敗したら全消去に倒れる。
URL_MAX="${CF_PURGE_URL_MAX:-5000}"
# 1 リクエストあたりの URL 数。Free プランの上限を公式ドキュメントで確認できな
# かったので、以前から Free で通っていた 30 に固定する。超えて弾かれた場合も全消去に倒れる。
CHUNK="${CF_PURGE_CHUNK:-30}"
TAB=$(printf '\t')

# 配信に効かないので差分から外すパス。
#   build.json: 毎ビルド sha が変わる。Cache Rule で cache bypass 済み (#6205 T9)
IGNORE_RE='^(build\.json)$'

log() { echo "[cf-purge] $*" >&2; }

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# 判定結果を stdout に 1 語で返す (skip / all / urls)。urls のときは $WORK/paths に対象を置く。
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
  grep "^navi-diff${TAB}" "$DIFF" | cut -f2- | grep -Ev "$IGNORE_RE" > "$WORK/paths" || true
  effective=$(wc -l < "$WORK/paths" | tr -d ' ')
  log "差分 ${actual} 件 (配信に効くもの ${effective} 件)"
  if [ "$effective" -eq 0 ]; then
    echo skip; return
  fi
  if grep -q '[^A-Za-z0-9._~/-]' "$WORK/paths"; then
    log "URL にそのまま使えない文字を含むパスがある"; echo all; return
  fi
  if [ "$effective" -gt "$URL_MAX" ]; then
    log "${effective} 件は上限 ${URL_MAX} を超える"; echo all; return
  fi
  echo urls
}

# パス → 読者が叩く URL。ディレクトリの index.html はディレクトリ URL にする。
to_urls() {
  sed -e 's#^index\.html$##' -e 's#/index\.html$#/#' -e "s#^#${BASE}/#" "$WORK/paths"
}

post_purge() {  # $1 = JSON body
  curl -sS -X POST "https://api.cloudflare.com/client/v4/zones/${CF_ZONE_ID}/purge_cache" \
    -H "Authorization: Bearer ${CF_PURGE_TOKEN}" \
    -H "Content-Type: application/json" \
    --data "$1"
}

purge_everything() {
  if [ "${CF_PURGE_DRY_RUN:-}" = "1" ]; then
    log "dry-run: purge_everything"; return
  fi
  res=$(post_purge '{"purge_everything":true}') || { log "purge_everything の API 呼び出しに失敗: $res"; exit 1; }
  echo "$res"
  echo "$res" | grep -q '"success": *true' || { log "purge_everything が success を返さなかった"; exit 1; }
}

purge_urls() {
  to_urls > "$WORK/urls"
  total=$(wc -l < "$WORK/urls" | tr -d ' ')
  awk -v dir="$WORK" -v c="$CHUNK" '{ f = sprintf("%s/chunk.%05d", dir, int((NR - 1) / c)); print > f }' "$WORK/urls"
  n=0
  for chunk in "$WORK"/chunk.*; do
    body=$(awk 'BEGIN{printf "{\"files\":["} NR>1{printf ","} {printf "\"%s\"", $0} END{printf "]}"}' "$chunk")
    n=$((n + 1))
    if [ "${CF_PURGE_DRY_RUN:-}" = "1" ]; then
      log "dry-run: files[$n] $body"; continue
    fi
    if ! res=$(post_purge "$body") || ! echo "$res" | grep -q '"success": *true'; then
      log "URL 消去の ${n} 回目が失敗したので全消去に切り替える: ${res:-no response}"
      purge_everything
      return
    fi
  done
  if [ "${CF_PURGE_DRY_RUN:-}" = "1" ]; then
    log "dry-run: URL ${total} 件 / ${n} 回"
  else
    log "URL ${total} 件を ${n} 回に分けて消去した"
  fi
}

mode=$(decide)
case "$mode" in
  skip) log "配信物に変化が無いので消去しない" ;;
  urls) purge_urls ;;
  *)    log "全消去する"; purge_everything ;;
esac
