#!/bin/sh
# ビルド成果物が参照している指紋付きアセットが、本番オリジンから実際に返るように
# なるまで待つ (#5260)。cf-purge の直前に挟む。
#
# なぜ purge の前に待つ必要があるか:
#   purge は「エッジの古い HTML を捨てて、全クライアントに新しい HTML を配る」操作。
#   オリジンのファイル入れ替えが終わる前に purge すると、新しい HTML を受け取った
#   ブラウザが **まだ存在しない指紋付き CSS を一斉に取りに行く**。
#   /assets/** の 404 は長期キャッシュされうるため (2026-08-15 実測: max-age=31536000)、
#   その一瞬が読者のブラウザに焼き付く。purge を「アセットが実在してから」に
#   限定すれば、この経路は塞げる。
#
# 何を待つか:
#   直前の pages ジョブが吐いた public/index.html が参照している指紋付き
#   CSS/JS が、本番 URL で 200 を返すこと。**キャッシュバスター付きで叩く**ので
#   エッジのヒットではなくオリジンの実体を見ている。
#
# 塞げない経路 (承知のうえ):
#   デプロイ前に配られた古い HTML は、ブラウザ側の max-age (実測 600 秒) の間
#   生き残り、入れ替えで消えた指紋 URL を引いて 404 になる。GitLab Pages は
#   世代を保持しないので、これは構成上避けられない。だからこそ
#   **404 を長期キャッシュさせないこと**が本丸で、こちらは補助 (#5260)。
#
# 使いかた:
#   sh scripts/ci_wait_for_assets.sh [public/index.html] [https://navi.omcha.jp]
set -eu

INDEX="${1:-public/index.html}"
BASE="${2:-https://navi.omcha.jp}"
TIMEOUT="${ASSET_WAIT_TIMEOUT:-300}"   # 全体の待ち時間上限 (秒)
INTERVAL="${ASSET_WAIT_INTERVAL:-10}"

if [ ! -f "$INDEX" ]; then
  echo "[wait-for-assets] $INDEX が無い (pages ジョブの artifacts を取れていない)" >&2
  exit 1
fi

# Hugo の --minify は属性のクォートを落とすので、href=/... と href="/..." の
# 両方を拾う。指紋 (hex 32 桁以上) が付いた同一オリジンの CSS/JS だけが対象。
# BRE の \| は移植性が無い (busybox grep) ので ERE を使う。
ASSETS=$(
  grep -oE '/[a-zA-Z0-9_/.-]*\.[0-9a-f]{32,64}\.(css|js)' "$INDEX" \
    | sort -u
)

if [ -z "$ASSETS" ]; then
  echo "[wait-for-assets] $INDEX に指紋付きアセットの参照が無い" >&2
  exit 1
fi

echo "[wait-for-assets] 対象:"
echo "$ASSETS" | sed 's/^/  /'

DEADLINE=$(( $(date +%s) + TIMEOUT ))
for path in $ASSETS; do
  url="${BASE}${path}"
  while : ; do
    # キャッシュバスターでエッジを迂回し、オリジンの実体を見る。
    code=$(curl -s -o /dev/null -w '%{http_code}' "${url}?cb=$(date +%s)-$$" || echo 000)
    if [ "$code" = "200" ]; then
      echo "[wait-for-assets] ok  $path"
      break
    fi
    if [ "$(date +%s)" -ge "$DEADLINE" ]; then
      echo "[wait-for-assets] 待ち時間 ${TIMEOUT}s を超えても $path が $code のまま。" >&2
      echo "[wait-for-assets] purge せずに中断する (新しい HTML を配ると 404 が焼き付く)" >&2
      exit 1
    fi
    echo "[wait-for-assets] $path -> $code, ${INTERVAL}s 待つ"
    sleep "$INTERVAL"
  done
done

echo "[wait-for-assets] 参照アセットは全て配信済み。purge に進む"
