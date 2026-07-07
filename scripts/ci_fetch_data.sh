#!/bin/sh
# データ更新ジョブ本体 (GitLab 移行 フェーズ4, 旧 01-fetch-products.yml の移植)。
# .gitlab-ci.yml の fetch-data job から呼ばれる。
#
# 方針:
# - 各 fetch step は旧 workflow の `if: always()` と同じく、失敗しても後続を止めない
#   (`step` ヘルパーが exit code を握りつぶして警告だけ出す)
# - secret が必要な step は未設定なら skip (GitHub Secrets から移せていないキーが
#   揃うまでの間も、秘密鍵不要の trends / yahoo_news / signal_detector /
#   filter_raw_per_asin だけで安全に回る)
# - 空データでの上書き guard は各 script 側に実装済み (fetch_rakuten ほか,
#   omochairo-rakuten-empty-overwrite-trap 参照)。ここでは制御しない
#
# 最後の filter_raw_per_asin だけは失敗を fail とする (per_asin は記事生成の
# 品質ゲート素材で、壊れたまま MR を作ってはいけないため)。

step() {
  name="$1"; shift
  echo "=== $name ==="
  if "$@"; then :; else echo "WARNING: $name failed (exit $?) — continuing"; fi
}

skip_unless() {
  # $1: 表示名, $2: 必須 env 変数名。未設定なら 1 を返す (呼び出し側で skip)
  eval v="\${$2:-}"
  if [ -z "$v" ]; then
    echo "=== $1: skip ($2 未設定) ==="
    return 1
  fi
  return 0
}

if skip_unless "fetch_amazon" AMAZON_CREATORS_APPLICATION_ID; then
  # #810 follow-up の discovery sweep 拡大パラメータ (旧 01 と同値)
  step "fetch_amazon" python scripts/fetch_amazon.py \
    --keyword-sample-size 60 --pages 3 --min-new 80 --out data/raw/
fi

step "fetch_trends" python scripts/fetch_trends.py --out data/raw/

if skip_unless "fetch_rakuten" RAKUTEN_APP_ID; then
  step "fetch_rakuten" python scripts/fetch_rakuten.py --out data/raw/
fi

if skip_unless "fetch_youtube" YOUTUBE_API_KEY; then
  step "fetch_youtube" python scripts/fetch_youtube.py --out data/raw/
fi

step "fetch_yahoo_news" python scripts/fetch_yahoo_news.py --out data/raw/

if skip_unless "fetch_yahoo" YAHOO_CLIENT_ID; then
  step "fetch_yahoo" python scripts/fetch_yahoo.py
fi

if skip_unless "fetch_books" GOOGLEBOOKS_API_KEY; then
  step "fetch_books" python scripts/fetch_books.py
fi

if skip_unless "fetch_cross_search" RAKUTEN_APP_ID; then
  step "fetch_cross_search" python scripts/fetch_cross_search.py
fi

if skip_unless "fetch_reviews" RAKUTEN_APP_ID; then
  step "fetch_reviews" python scripts/fetch_reviews.py
fi

# OMCHA_API_KEY は任意 (internal_links.py: 設定時のみ api_key query param を付与)。
# omcha.jp API はキー無しでも動くため guard しない (2026-07-07 ユーザー確認済)
step "fetch_omcha_related" python scripts/fetch_omcha_related.py --out data/raw/

step "signal_detector" python scripts/signal_detector.py

echo "=== filter_raw_per_asin ==="
python scripts/filter_raw_per_asin.py || exit 1

python scripts/create_data_mr.py \
  --prefix fetch-data \
  --title "data: automated fetch update" \
  --body "Automated data refresh (旧 01-fetch-products.yml の GitLab 移植). data/ 配下のみの更新で記事変更なし。" \
  --paths data/
