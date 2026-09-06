"""run_lighthouse_lane.py

#2995 案6「セルフホスト Lighthouse — CWV 全ページ実測」の計測スクリプト
(#1357 E2 / 19-cwv-monitor.yml の CrUX レーンと対になるラボ計測側)。

navi.omcha.jp の代表 URL 群に対して Lighthouse をローカル実行し、
`data/analytics/history/lighthouse_history.jsonl` に append する。前回までの
履歴と比較して劣化を検出し、劣化があったときだけ Markdown レポートを出して
呼び出し元 workflow に「issue を立てるべきか」を伝える。

なぜ CrUX (19-cwv-monitor) と別に要るか:
  CrUX は実ユーザ 28 日 rolling の p75 なので、①低トラフィック URL はデータ
  なし ②劣化が見えるまで数週間かかる ③ページ単位の原因 (どの audit が落ちたか)
  が分からない。ラボ計測は毎日・全ページ級・原因つきで取れるので、回帰を
  「リリース当日」に捕まえる早期警戒レーンとして相補的に効く。

なぜ PSI API でなくセルフホストか:
  PSI API はクォータ (匿名だと即 429、key ありでも 25k/day・400/100s) があり
  全ページ級の毎日計測には足りない。self-hosted runner なら Actions 分数も
  API 課金もゼロで、Lighthouse を無制限に回せる (#2995 の自宅レーン資産④)。

なぜこのリポジトリにスクリプトだけ置くか:
  omochairo/amazon は public リポジトリで self-hosted runner を繋げない
  (fork PR が workflow 定義ごと差し替えて自宅サーバーで任意コード実行できる)。
  実行は private な omochairo/amazon-home-ops の workflow から行い、本リポジトリ
  には計測ロジックと unit test のみを置く (CI からは呼ばない)。
  → fetch_google_suggest.py と同じ流儀。

設計判断:
- **median-of-N**: Lighthouse は 1 回の実行でも数百 ms 単位で振れる。単発値で
  閾値判定すると誤検出で issue が湧き、GitHub API バースト禁止規律 (CLAUDE.md)
  にも反する。既定 3 回実行の median を採用する。
- **error 状態を数値と分けて記録する**: 2026-07-16 の PSI 手動測定で LCP が
  `NO_LCP` エラーになり「LCP が壊れている」ように見えたが、同版の Lighthouse を
  ローカルで回すと LCP=5.0s と正常に出た (= 計測基盤側の失敗)。metric 値だけを
  JSONL に持つと、この 2 つが区別できず追跡不能になる。よって各 metric に
  `*_error` 列を持たせ、error 行は劣化判定から除外して別枠で報告する。
- **分散の見かたは MAD と分位の 2 本立て** (#4160 / #5264): MAD は単発の外れ値に
  頑健だが、外れ値が**再発する第 2 のクラスタ**である系列 (ホーム mobile の TBT)
  では、多数派クラスタの幅しか測らずゲートが許容すべき幅を見落とす。baseline 窓の
  Tukey fence (Q3 + 1.5*IQR) を超えることを必須条件に足して塞ぐ。
- **run 単位の汚染は run 単位でしか見えない** (#5320): 上の 2 本はどちらも
  「その URL の履歴」の中で外れ値を測るので、self-hosted runner が重かった日の
  ように **run 全体が一様に沈む** 汚染は原理的に見抜けない (各 URL から見れば
  全員が正しく外れている)。run 全体の中央比で別途判定して metric ごとに外す。
- **回帰判定は「直近 baseline の median」対比**: 絶対閾値 (CWV good) だけだと
  元から悪い指標が毎回鳴り続けてノイズになるので、①CWV 閾値をまたいだ悪化
  ②baseline 比の相対悪化、の 2 条件で鳴らす。**baseline が育っていない URL は
  何も鳴らさない** (2026-07-27 修正): 計測対象は GSC 週次上位なので週替わりで
  URL が入れ替わり、旧実装はその新規 URL に対して絶対閾値だけで判定していたため
  「商品ページは元から LCP 5.4s」という既知の遅さを毎週報告し続けていた。
- 出力は data PR + auto-merge、報告は劣化時のみ 1 issue に集約 (バースト禁止規律)。

runner 側の前提 (amazon-home-ops の 41-lighthouse-lane.yml):
- 実行先は **K8 Plus (label `home,llm`)** で NAS (`home,ollama`) ではない。
  Chrome headless は 1GB 級を食うが、NAS (DXP4800) は RAM 7.7GB を ollama と
  gitlab-runner で分け合い runner の mem_limit は 4g しかない。K8 (Ryzen 7
  8845HS / 64GB, mem_limit 8g) が適地。#2995 は K8 稼働 (2026-07-14) より前に
  書かれた「NAS レーン拡張」issue だが、案6 の実体は LLM ではなくブラウザ計測。
- Node.js + `lighthouse` CLI と Chrome が必要。`--lighthouse-cmd` /
  `LIGHTHOUSE_CMD`、`CHROME_PATH` で差し替えできる。
- Lighthouse は PSI と同じメジャー版に固定する (audit id / errorMessage の
  互換性のため。版が飛ぶと baseline が不連続になり誤検出の温床になる)。
- `from __future__ import annotations` は維持する。NAS (python3.8) に載せ替えても
  壊れないようにするための保険 ([[reference-omochairo-home-ops-repo]] の実績: 3.9+ の
  組み込みジェネリック注釈は 3.8 の import 時に落ちる)。

副作用:
- data/analytics/history/lighthouse_history.jsonl への append
- data/analytics/history/seen_dates.json の `lighthouse` key 更新、および
  計測が飛んだ日を見つけたときの `_meta.lighthouse_missing_dates` 追記 (#4785)
- --report-out 指定時、劣化レポート Markdown の書き出し
- 記事生成 / score / narrative には触れない

Issue: https://github.com/omochairo/amazon/issues/2995 (案6) / #1357 (epic E2)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import re
import statistics
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("run_lighthouse_lane")

DEFAULT_HISTORY_DIR = "data/analytics/history"
LIGHTHOUSE_HISTORY_FILENAME = "lighthouse_history.jsonl"
SEEN_DATES_FILENAME = "seen_dates.json"
# 計測日を UTC の壁時計から取らず、offset だけ戻した「論理日」で決める (#4785)。
#
# lane の cron は home-ops の 41-lighthouse-lane.yml で `40 21 * * *` (21:40 UTC)。
# ところが GitHub の schedule dispatch は恒常的に 50-60 分遅れており (実測:
# 07-28〜08-08 の起動は 22:32-22:42 UTC)、真夜中 UTC までの実質余裕は 1 時間強
# しかない。2026-08-07 の run は 01:04 UTC に起動し、date.today() が翌日を返した
# 結果 **2026-08-06 の 11 行が丸ごと欠測し、2026-08-07 が 2 バッチ**になった。
# (#4772 は後者の重複を後勝ちで潰したが、欠測する側は手当てされていなかった)
#
# 6 時間戻すと「その日の 06:00 UTC 〜 翌 06:00 UTC に起動した run」が同じ論理日に
# なる。21:40 の cron に対して 8 時間 20 分の遅延耐性ができる。
DEFAULT_DAY_OFFSET_HOURS = float(os.environ.get("LH_DAY_OFFSET_HOURS", "6"))
# GSC 上位 URL の入力元。main に実在するのは週次の
# data/analytics/gsc_history/<ISO週>.json (by_page 付き) の方で、
# gsc_weekly.json は存在しない。初回 run 29523140030 はこれを踏んで
# --top-urls 10 を渡しているのに origin 1 件しか計測していなかった。
# 明示パスが無ければ gsc_history の最新週に自動フォールバックする。
DEFAULT_GSC_INPUT = "data/analytics/gsc_weekly.json"
GSC_HISTORY_DIR = "data/analytics/gsc_history"
DEFAULT_ORIGIN = "https://navi.omcha.jp"
DEFAULT_RUNS = 3
DEFAULT_TOP_URLS = 5
LIGHTHOUSE_TIMEOUT = 300
# PSI と同じメジャー版に固定する (module docstring の設計判断)。バージョンを
# 固定せず npx --yes lighthouse のままにしていたため実装が伴っていなかった
# (#4160 で追加)。環境変数 LIGHTHOUSE_CMD での差し替えは従来どおり効く。
#
# 値は cron が実際に使う版と一致させること (#4384)。cron は
# omochairo/amazon-home-ops の 41-lighthouse-lane.yml で global CLI を
# LIGHTHOUSE_VERSION=13.4.0 に exact 固定し、`LIGHTHOUSE_CMD: lighthouse` を
# 渡してくるので、この既定値が効くのは手元/手動実行のときだけ。ここが
# ずれていると手動実行の値だけ別版で測られ、同じ JSONL に混ざって baseline を
# 静かに汚す。上げるときは home-ops 側と同時に、履歴の断点を承知の上で。
DEFAULT_LIGHTHOUSE_VERSION = "13.4.0"
DEFAULT_LIGHTHOUSE_CMD = "npx --yes lighthouse@{}".format(DEFAULT_LIGHTHOUSE_VERSION)

# ラボ計測を GA4 から外すための UA マーカー (#6398)。
#
# なぜ UA か:
#   - `navigator.webdriver` は使えない。chrome-launcher の既定フラグに
#     `--enable-automation` が無く、Lighthouse 実行時も false になる
#     (2026-09-02 に `--headless=new --no-sandbox --disable-dev-shm-usage` で実測)。
#   - URL に `?lab=1` を足す案は却下。Cloudflare のキャッシュキーが変わって毎回
#     MISS になり、NAS オリジンに毎 run 到達する。504 が 14〜31%/日 出ている
#     オリジンの応答を測ることになり、測定そのものが歪む。
#   - Cookie を `--extra-headers` で送る案も却下。CDP の追加ヘッダは cookie jar に
#     入らないので `document.cookie` からは見えない。配信は静的ホストなので
#     サーバ側で読むこともできない。
#
# 文字列は Lighthouse の既定 (core/config/constants.js の MOTOG4_USERAGENT /
# DESKTOP_USERAGENT) に `omcha-lab` を足しただけのもの。Lighthouse が既定 UA の
# Chrome バージョンを上げると差が出るが、**配信側に UA 分岐は 1 箇所も無く**
# (hugo/assets/js・layouts を grep して 0 件)、GA4 は本マーカーで無効化するので
# 実害は無い。受け側は hugo/layouts/partials/extend_head.html。
LAB_UA_MARKER = "omcha-lab"
LAB_USER_AGENTS = {
    "mobile": (
        "Mozilla/5.0 (Linux; Android 11; moto g power (2022)) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/136.0.0.0 Mobile Safari/537.36 " + LAB_UA_MARKER
    ),
    "desktop": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36 " + LAB_UA_MARKER
    ),
}

# Lighthouse audit id → JSONL 列名 prefix。numericValue (ms / 無次元) を採る。
METRIC_MAP = (
    ("largest-contentful-paint", "lcp"),
    ("first-contentful-paint", "fcp"),
    ("cumulative-layout-shift", "cls"),
    ("total-blocking-time", "tbt"),
    ("speed-index", "si"),
)

# CWV / Lighthouse の "good" 閾値。これをまたぐ悪化を第一級の劣化として扱う。
GOOD_THRESHOLDS = {
    "lcp": 2500.0,
    "fcp": 1800.0,
    "cls": 0.1,
    "tbt": 200.0,
    "si": 3400.0,
}

# baseline 比でこれ以上悪化したら鳴らす (相対)。
REGRESSION_RATIO = 1.25
# 相対悪化でも、この絶対差未満ならノイズとして無視する (metric 単位)。
MIN_ABS_DELTA = {
    "lcp": 300.0,
    "fcp": 200.0,
    "cls": 0.02,
    "tbt": 50.0,
    "si": 300.0,
}
# perf score がこれ以上落ちたら鳴らす (0-100 換算)。
SCORE_DROP_POINTS = 5.0
# baseline としてこの件数未満しか履歴が無い URL は、まだ比較しない (= 鳴らさない)。
#
# #4160 (2026-08-02) で MIN_BASELINE_SAMPLES=3 / window=5 のまま実履歴を replay
# したところ誤検出だった: navi ホーム (mobile) の LCP 系列は n=20 で
# median=4674 / min=2425 / max=5748 / **stdev=957**。REGRESSION_RATIO=1.25 が
# 効くのは「baseline の 1.25 倍」であり、この分散下ではだいたい 1.2σ 相当しか
# ない (= 実行ごとの揺れの範囲内で簡単に踏む)。3 回 median でも収束しないほど
# 元々振れる指標なので、母数を増やして安定させる: window 5→10, 最小サンプル数
# 3→7。あわせて後述の MAD ゲートで「分散に対して十分外れているか」も見る。
MIN_BASELINE_SAMPLES = 7
# 分散対応ゲート (#4160): baseline の MAD (median absolute deviation) に対して
# K 倍以上外れていることを relative/score 判定の必須条件にする。ratio 条件
# だけだと分散の大きい指標 (ホーム mobile LCP など) を毎回誤検出するため。
REGRESSION_MAD_K = 3.0

# 二峰性対応ゲート (#5264): baseline 窓の Tukey fence (Q3 + K*IQR) を超えることを
# metric 判定の必須条件にする。
#
# なぜ MAD だけでは足りないか (2026-08-14 の #5259 が実例):
#   ホーム mobile の TBT の baseline 窓は 85 / 51 / 44.5 / 48 / 231.5 / 46.5 / 272.5 で、
#   median=51.0 に対し **MAD=6.5** だった。MAD は外れ値に頑健なので多数派クラスタ
#   (44〜85) の幅しか測らず、窓の中にある 231.5 と 272.5 を無視する。その結果ゲートは
#   「51 ± 6.5」だと信じ、窓の実測レンジ (44.5〜272.5) の内側である 150 で鳴った。
#
#   MAD が「外れ値を無視する」道具であること自体は #4160 の意図どおりで、単発の
#   外れ値には正しく効いている。問題は、この系列では高い側が**単発ではなく再発する
#   第 2 のクラスタ**である点。ゲートが許容すべき幅そのものを MAD が見落とす。
#
#   分位なら再発クラスタが Q3 に乗るので幅に反映される。分布が締まっている系列では
#   IQR が小さく fence も締まるため、感度は落ちない (CLS のようにほぼ定数の系列では
#   fence ≒ Q3 ≒ baseline)。実履歴 28 日の replay では、現行が鳴らす 3 件のうち
#   誤検出寄りの 2 件 (2026-07-27 ホーム 105 / baseline 50.8、2026-08-14 ホーム 150 /
#   baseline 51.0) が消え、大きい 1 件 (2026-08-05 商品 398 / baseline 130.5・
#   fence 141.6) は残った。
REGRESSION_FENCE_K = 1.5

# run 全体汚染ガード (#5320): その run の全 URL がまとめて同じ向きに沈んでいるときは、
# サイトの劣化ではなく計測環境 (self-hosted runner) の劣化として扱い、その metric の
# 判定を見送る。
#
# なぜ要るか (2026-08-15 の #5320 が実例):
#   その日の mobile 11 URL の SI 中央値は 2856 → 4734 (+66%)、11 本中 10 本が
#   4000ms を超えた。ホーム・cospa・商品ページが**同時に同じ幅で**沈み、翌 8/16 には
#   11 本とも元の水準へ完全復帰している (中央値 2779)。Chrome major も lighthouse 版も
#   同一。同じ形は 7/16 と 7/19 にも出ていて、いずれも翌日に復帰している。
#   デプロイでこの形にはならない (共通アセットの劣化なら翌日も続く)。runner 側が
#   その run のあいだだけ重かった、と読むのが素直。
#
# なぜ URL 単位のゲートでは防げないか:
#   MAD も Tukey fence も「その URL の履歴」の中でしか外れ値を測らない。run 全体が
#   一様に沈むと、各 URL から見れば単独で大きく外れた値なので、全 URL が正しく鳴る。
#   汚染は run 単位の量なので、run 単位で測らないと見えない。
#
# なぜ #5264 の「同日 URL 中央値で正規化」とは別物か:
#   #5264 が扱ったのは TBT の (URL × 日) 単位でランダムに乗る裾で、日効果ではないので
#   正規化が効かなかった。こちらは日効果そのもの (fcp / si は run 単位で動く)。
#   metric ごとに独立して判定するので、#5264 の TBT の裾はここでは抑制されない。
#
# 較正 (実履歴 31 日の replay):
#   baseline が育っている 17 日ぶんの run 中央比は lcp/fcp/cls/si が p90 ≤ 1.04、
#   最も裾の重い tbt でも最大 1.20 (2026-08-05 = #5264 が残すべきとした本物)。
#   1.25 を超えるのは 2026-08-15 の fcp 1.35 / si 1.59 だけ。さらに「1.25 を超えた
#   URL が半数以上」を AND で課すと、tbt の裾 (2026-07-24 は中央比 1.01 なのに 2/5 が
#   1.25 超) を確実に外せる。両条件を満たすのは 2026-08-15 の fcp (3/6) と si (5/6)
#   のみで、2026-08-05 の本物 (中央比 1.20 / 1件) は残る。
RUN_SHIFT_RATIO = 1.25
RUN_SHIFT_FRACTION = 0.5
# run 単位の統計なので、比較できる URL がこれ未満のときは判定しない
# (Chrome major 更新直後は baseline を持つ URL が数本まで減る)。
MIN_RUN_SHIFT_SAMPLES = 4

# `lcp_element_reason` がこれらのときは、その行の LCP は「計測できていない」。
#
# 2026-08-06 の probe (#4441) で確定した機構: product ページの trace には
# `largestContentfulPaint::Candidate` が 1 件も無い。この状態で lighthouse 13.4.0 は
# NO_LCP で落とさず、**最後の `NavStartToLargestContentfulPaint::Invalidate::AllFrames::UKM`
# の ts で偽の Candidate を合成する** (core/lib/tracehouse/trace-processor.js、
# コード内コメントいわく "Not ideal since they are 1 paint behind"、`size` は 1 のモック)。
# その結果 `lcp` (Lantern 推定) も `observed_lcp` も値は入るが、中身は「最後に LCP が
# 無効化された時刻」であって LCP ではない。
#
# 実測 (2026-08-04 の 11 行) では 9 行が product = 9/11 がこの状態。ここを素通しすると
# ゲートの lcp アームは毎日 9/11 の URL で「LCP でない数字」を LCP として比べ続ける。
# → [[feedback-omochairo-psi-no-lcp-vs-real-lcp]] の「値だけ持つと『計測失敗』と
#   『本当に遅い』が区別できず追跡不能になる」の再来なので、lcp 判定から外す。
#
# 外すのは lcp の relative/threshold 判定だけ。`lcp_error` (NO_LCP など) の
# kind="error" は計測基盤の失敗検出なので従来どおり無条件で鳴らす。
# fcp/cls/tbt/si は実 Candidate の有無と無関係なので影響しない。
#
# `no-node-in-details` は details が存在する = 実 Candidate はあったので除外しない。
# `audit-missing` (LH12 以前) は判断材料が無いので除外しない (degrade して従来判定)。
# #4577 マージ前の行には `lcp_element_reason` キー自体が無く None になるため、
# 過去行も自動的に degrade 側に落ちる。
LCP_UNMEASURED_REASONS = frozenset({"notApplicable", "no-details"})

# 単日連続判定ゲート (#6426): kind="error" 以外の劣化は、直近 N 日連続で条件を
# 満たしたときだけ報告する。#6120 の実例 (2026-08-27 の TBT 単発スパイク。翌日には
# baseline へ戻り、その後 5 日間も戻ったまま) が示すとおり、単日の値だけで起票すると
# ワーカー側の負荷変動を回帰として誤検出する。N=2 なら検出日だけの単発は弾け、
# 実際に連日続く劣化は取りこぼさない。
# kind="error" (audit error / runtime error) は計測基盤の失敗検出であり、上の
# LCP_UNMEASURED_REASONS 付近のコメントにある設計判断 (無条件で鳴らす) と同じ理由
# でこのゲートの対象外にする。
REGRESSION_CONSECUTIVE_DAYS = int(os.environ.get("LH_REGRESSION_CONSECUTIVE_DAYS", "2"))


def build_lighthouse_argv(
    cmd: str, url: str, out_path: str, form_factor: str
) -> List[str]:
    """Lighthouse CLI の argv を組み立てる。

    `cmd` は "npx lighthouse" のように空白区切りでも渡せる (runner 側の都合で
    npx / グローバル install / 絶対パスが混在しうるため)。
    """
    argv = cmd.split()
    argv += [
        url,
        "--only-categories=performance",
        "--output=json",
        "--output-path=" + out_path,
        "--quiet",
        "--chrome-flags=--headless=new --no-sandbox --disable-dev-shm-usage",
    ]
    argv.append("--emulated-user-agent=" + LAB_USER_AGENTS[form_factor])
    if form_factor == "mobile":
        # Lighthouse の既定 (Moto G Power / 低速4G) = PSI mobile と揃う
        argv += ["--form-factor=mobile", "--screenEmulation.mobile"]
    else:
        argv += [
            "--form-factor=desktop",
            "--screenEmulation.disabled",
            "--throttling.rttMs=40",
            "--throttling.throughputKbps=10240",
            "--throttling.cpuSlowdownMultiplier=1",
        ]
    return argv


def _find_node_selector(node: Any) -> Optional[str]:
    """Lighthouse details ツリーを再帰的に走査して最初の node selector を返す。

    LH13 (`lcp-breakdown-insight`) は details.items に直接
    `{"type": "node", "selector": ...}` を持つが、旧版
    (`largest-contentful-paint-element`) は details.items[].node や
    details.items[].items[] にネストされた table 形式で持つ。両対応するため
    dict/list を再帰的に潜って最初に見つかった node.selector を返す。
    """
    if isinstance(node, dict):
        if node.get("type") == "node" and node.get("selector"):
            return node.get("selector")
        for key in ("node", "items"):
            found = _find_node_selector(node.get(key))
            if found:
                return found
        return None
    if isinstance(node, list):
        for item in node:
            found = _find_node_selector(item)
            if found:
                return found
    return None


def _lcp_element_reason(audits: Dict[str, Any]) -> str:
    """`lcp_element` が None のとき、その理由を短い文字列で返す (#4441)。

    2026-08-06 の probe (lighthouse@13.4.0 をローカル実測) で確定した機構:
    product ページの trace には `largestContentfulPaint::Candidate` が **1 件も無く**
    (Invalidate の UKM だけ)、trace engine が NO_LCP → `subparts` 未定義 →
    audit が `notApplicable` かつ `details: null` になる。つまり selector が取れない
    のは parser 側の問題ではなく **ページ側で LCP 候補が確定していない**ため。
    行にこの区別を残さないと、次に見た人がまた `_find_node_selector` を疑う。

    戻り値は集計しやすいよう短い固定語彙にする:
      - `audit-missing`      … lcp-breakdown-insight 自体が無い (LH12 以前)
      - `notApplicable` 等   … audit はあるが details が null (scoreDisplayMode をそのまま)
      - `no-node-in-details` … details はあるが type=node が見つからない (真の parser 案件)
    """
    audit = audits.get("lcp-breakdown-insight")
    if not audit:
        return "audit-missing"
    if audit.get("details") is None:
        return audit.get("scoreDisplayMode") or "no-details"
    return "no-node-in-details"


def _lcp_subparts(audits: Dict[str, Any]) -> Tuple[Optional[Dict[str, float]], Optional[str]]:
    """LCP の内訳 (subpart 別の所要 ms) と、取れないときの理由を返す (#5081 やること2)。

    出どころは `lcp_element` と **同じ** `lcp-breakdown-insight` の details。
    LH13 では details.type == "list" で、items に
      - `{"type": "table", ...}`  … subpart 別の内訳
      - `{"type": "node", ...}`   … LCP 要素そのもの (`_find_node_selector` が拾う)
    が並ぶ (2026-08-20 に lighthouse@13.4.0 で実測)。

    **subpart の数は LCP 要素の種類で変わる。** テキストが LCP なら
    `timeToFirstByte` / `elementRenderDelay` の 2 つしか出ない。4 分割
    (resourceLoadDelay / resourceLoadDuration が加わる) になるのは画像が LCP の
    ときだけなので、固定 4 キーを期待せず出てきたぶんだけ入れる。

    理由の語彙は `_lcp_element_reason` と揃える。**同じ audit を見ているので、
    details が無い行では element も subparts も同時に落ちる** (別々の障害では
    ない)。details はあるのに table だけ無い場合だけ独自の語彙を返す。
    """
    details = (audits.get("lcp-breakdown-insight") or {}).get("details")
    if details is None:
        return None, _lcp_element_reason(audits)
    out: Dict[str, float] = {}
    for item in (details.get("items") or []) if isinstance(details, dict) else []:
        if not isinstance(item, dict) or item.get("type") != "table":
            continue
        for row in item.get("items") or []:
            if not isinstance(row, dict):
                continue
            name = row.get("subpart")
            value = row.get("duration")
            if isinstance(name, str) and isinstance(value, (int, float)):
                out[name] = round(float(value), 1)
    if not out:
        return None, "no-subparts-in-details"
    return out, None


_CHROME_UA_RE = re.compile(r"(?:Headless)?Chrome/([\d.]+)")


def _chrome_version(lh: Dict[str, Any]) -> Optional[str]:
    """計測に使われた Chrome の版を `environment.hostUserAgent` から拾う (#4583)。

    なぜ要るか: 2026-08-05 の run で b0fjw7gq8x mobile の TBT が 131 → 398 に跳ねた
    (perf_score 76 → 66)。LCP/FCP/SI/CLS は動いておらず observed_lcp はむしろ改善して
    いたため描画は健全で、同 run の他 URL も軽く上振れしていた。原因調査で唯一動いて
    いた変数が Chrome 150.0.7871.128 → 151.0.7922.75 だったが、**これは JSONL には
    残っておらず home-ops の run ログを 22 本さかのぼって初めて分かった**。
    lh_version と違って Chrome 版は行に無かったのが調査コストの正体なので、残す。

    UA が持つのは major だけ (`HeadlessChrome/151.0.0.0`) だが、baseline を不連続に
    するのは major 更新なので、この粒度で用は足りる。
    """
    ua = ((lh.get("environment") or {}).get("hostUserAgent")) or ""
    m = _CHROME_UA_RE.search(ua)
    return m.group(1) if m else None


def extract_metrics(lh: Dict[str, Any]) -> Dict[str, Any]:
    """Lighthouse JSON から metric 値と *_error を flat dict に抽出する。

    値と error を明確に分けるのが肝 (module docstring の設計判断を参照)。
    audit が errorMessage を持つ場合 (例: NO_LCP) は値を None にして
    `<short>_error` にメッセージを入れる。

    #4160 で observed 値 / LCP 要素 / throttling 方式 / LH 版も追加で拾う。
    JSONL に記録している `lcp` は `throttlingMethod=simulate` (Lantern) の
    推定値であり、実描画の遅れとは限らない (simulated LCP が simulated TTI に
    引っ張られるケースを実測で確認済み)。observed 値と要素を残しておけば、
    次回以降はライブ再計測なしに「本当に描画が遅いのか」を JSONL だけで
    判別できる。
    """
    out: Dict[str, Any] = {}

    runtime_error = (lh.get("runtimeError") or {}).get("code")
    if runtime_error and runtime_error != "NO_ERROR":
        out["runtime_error"] = runtime_error

    categories = lh.get("categories") or {}
    perf = (categories.get("performance") or {}).get("score")
    # Lighthouse の score は 0-1 (取得不能なら null)。0-100 に正規化して持つ。
    out["perf_score"] = round(perf * 100, 1) if isinstance(perf, (int, float)) else None

    audits = lh.get("audits") or {}
    for audit_id, short in METRIC_MAP:
        audit = audits.get(audit_id) or {}
        err = audit.get("errorMessage")
        value = audit.get("numericValue")
        if err:
            out[short] = None
            out[short + "_error"] = err
        elif isinstance(value, (int, float)):
            # ms 系は整数化しても十分 (CLS だけ小数を保つ)
            out[short] = round(value, 3) if short == "cls" else round(value, 1)
        else:
            out[short] = None

    # observed 値 (simulate/lantern 推定でなく実描画タイムスタンプ)。
    # `metrics` audit の details.items[0] に observedXxx 系がまとまっている。
    metrics_items = (((audits.get("metrics") or {}).get("details") or {}).get("items") or [])
    first_item = metrics_items[0] if metrics_items else {}
    for key, short in (
        ("observedLargestContentfulPaint", "observed_lcp"),
        ("observedFirstContentfulPaint", "observed_fcp"),
    ):
        v = first_item.get(key)
        out[short] = round(v, 1) if isinstance(v, (int, float)) else None

    # LCP 要素の selector。LH13 は lcp-breakdown-insight、旧版は
    # largest-contentful-paint-element にある。前者が無ければ後者にフォール
    # バックする。
    lcp_element = _find_node_selector((audits.get("lcp-breakdown-insight") or {}).get("details"))
    if lcp_element is None:
        # LH13 の lcp-breakdown-insight は meta に replacesAudits:
        # ['largest-contentful-paint-element'] を持ち、13.x の JSON にこの audit は
        # 存在しない (2026-08-06 実測)。過去に採取した 12.x 以前の JSON を再解析する
        # ときのためだけに残している。
        lcp_element = _find_node_selector(
            (audits.get("largest-contentful-paint-element") or {}).get("details")
        )
    out["lcp_element"] = lcp_element
    out["lcp_element_reason"] = None if lcp_element else _lcp_element_reason(audits)

    # LCP の内訳 (#5081 やること2)。element と同じ audit なので、商品ページのように
    # details が null の行では両方 None になる (details が無い = LCP 候補が確定して
    # いない、という 1 つの事象の別の面であって、独立した 2 つの欠測ではない)。
    out["lcp_subparts"], out["lcp_subparts_reason"] = _lcp_subparts(audits)

    out["throttling_method"] = (lh.get("configSettings") or {}).get("throttlingMethod")
    out["lh_version"] = lh.get("lighthouseVersion")
    out["chrome_version"] = _chrome_version(lh)
    return out


def run_lighthouse_once(
    url: str, cmd: str, form_factor: str, timeout: int = LIGHTHOUSE_TIMEOUT
) -> Optional[Dict[str, Any]]:
    """Lighthouse を 1 回実行して metric dict を返す。失敗したら None。

    Lighthouse は teardown で非ゼロを返すことがある一方で JSON は書けている、
    という挙動があるので returncode ではなく「JSON が読めたか」で成否を決める。
    """
    tmpdir = tempfile.mkdtemp(prefix="lh-")
    out_path = os.path.join(tmpdir, "lh.json")
    argv = build_lighthouse_argv(cmd, url, out_path, form_factor)
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout
        )
        if not os.path.exists(out_path):
            logger.warning(
                "lighthouse produced no report for %s (rc=%s): %s",
                url, proc.returncode, (proc.stderr or "")[-300:],
            )
            return None
        with open(out_path, encoding="utf-8") as f:
            lh = json.load(f)
        return extract_metrics(lh)
    except subprocess.TimeoutExpired:
        logger.warning("lighthouse timed out (%ss) for %s", timeout, url)
        return None
    except (OSError, ValueError) as e:
        logger.warning("lighthouse failed for %s: %s", url, e)
        return None
    finally:
        try:
            if os.path.exists(out_path):
                os.remove(out_path)
            os.rmdir(tmpdir)
        except OSError:
            pass


def aggregate_runs(runs: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """複数 run を median に畳む。

    - 数値がある run だけを median の母数にする (error/None は除外)。
    - error は「1 回でも出たら」記録する。全 run が error なら値は None のまま。
    - `runs` が空なら空 dict。
    """
    if not runs:
        return {}
    out: Dict[str, Any] = {"runs": len(runs)}

    scores = [r["perf_score"] for r in runs if isinstance(r.get("perf_score"), (int, float))]
    out["perf_score"] = round(statistics.median(scores), 1) if scores else None

    runtime_errors = [r["runtime_error"] for r in runs if r.get("runtime_error")]
    if runtime_errors:
        out["runtime_error"] = runtime_errors[0]

    for _, short in METRIC_MAP:
        values = [r[short] for r in runs if isinstance(r.get(short), (int, float))]
        if values:
            med = statistics.median(values)
            out[short] = round(med, 3) if short == "cls" else round(med, 1)
            # #5264: run 間のばらつき (max-min) を残す。median だけだと「3 回とも
            # 遅い」と「1 回だけ暴れた」を後から区別できず、誤検出の切り分けに
            # 使える材料が JSONL 側に何も残らない。ゲートでの利用は履歴が
            # 溜まってから (今ある行は全部このキーを持たないため)。
            spread = max(values) - min(values)
            out[short + "_spread"] = round(spread, 3) if short == "cls" else round(spread, 1)
        else:
            out[short] = None
        errors = [r[short + "_error"] for r in runs if r.get(short + "_error")]
        if errors:
            # 何回中何回 error だったかは誤検出の切り分けに効くので残す
            out[short + "_error"] = errors[0]
            out[short + "_error_runs"] = len(errors)

    # observed 系も数値 metric と同じく median を採る。
    for short in ("observed_lcp", "observed_fcp"):
        values = [r[short] for r in runs if isinstance(r.get(short), (int, float))]
        out[short] = round(statistics.median(values), 1) if values else None

    # lcp_element と lcp_element_reason は per-run では排他 (extract_metrics が
    # element を取れなかったときだけ reason を入れる)。この排他は集約でも保た
    # ないといけないので、2 キーを独立に畳まず 1 組で扱う (#5081 項目2)。
    # 商品ページの LCP 候補は間欠取得なので、run ごとに element / reason が割れ
    # る。片方ずつ拾うと両方入った行ができ、lcp_is_unmeasured() が reason だけ
    # を見るせいで「LCP が取れていた行」まで無音でゲートから外れていた。
    # 「1 回でも実 Candidate があれば計測できている」に倒す。
    elements = [r.get("lcp_element") for r in runs if r.get("lcp_element") is not None]
    if elements:
        out["lcp_element"] = elements[0]
        out["lcp_element_reason"] = None
        # 何回中何回で取れたかを残し、間欠であること自体を JSONL から追える
        # ようにする (`<metric>_error_runs` / `<metric>_spread` と同じ方針)。
        out["lcp_element_runs"] = len(elements)
        if any(v != elements[0] for v in elements):
            logger.warning("lcp_element differs across runs: %s", elements)
    else:
        out["lcp_element"] = None
        reasons = [r.get("lcp_element_reason") for r in runs
                   if r.get("lcp_element_reason") is not None]
        out["lcp_element_reason"] = reasons[0] if reasons else None
        if reasons and any(v != reasons[0] for v in reasons):
            logger.warning("lcp_element_reason differs across runs: %s", reasons)

    # lcp_subparts も element と同じく「1 回でも取れたら取れている」に倒す。
    # 値は subpart ごとに median を採る (run 間で内訳が割れるため)。取れた run が
    # 1 つも無いときだけ reason を入れる (element/reason と同じ排他)。
    subparts_runs = [r.get("lcp_subparts") for r in runs if isinstance(r.get("lcp_subparts"), dict)]
    if subparts_runs:
        merged: Dict[str, float] = {}
        for name in sorted({k for sp in subparts_runs for k in sp}):
            values = [sp[name] for sp in subparts_runs
                      if isinstance(sp.get(name), (int, float))]
            if values:
                merged[name] = round(statistics.median(values), 1)
        out["lcp_subparts"] = merged
        out["lcp_subparts_reason"] = None
        out["lcp_subparts_runs"] = len(subparts_runs)
    else:
        out["lcp_subparts"] = None
        reasons = [r.get("lcp_subparts_reason") for r in runs
                   if r.get("lcp_subparts_reason") is not None]
        out["lcp_subparts_reason"] = reasons[0] if reasons else None

    # throttling_method / lh_version / chrome_version は run 間で変わらない前提
    # (同一 URL・同一 form_factor を同一コマンドで N 回叩くだけなので)。最初の
    # run の値を代表として持つ。もし run 間で割れていたら、集計せず追跡だけ
    # できるよう警告に残す (原因調査の手がかりを消さない)。
    for key in ("throttling_method", "lh_version", "chrome_version"):
        values = [r.get(key) for r in runs if r.get(key) is not None]
        if values:
            out[key] = values[0]
            if any(v != values[0] for v in values):
                logger.warning("%s differs across runs: %s", key, values)
        else:
            out[key] = None
    return out


def load_history(history_path: pathlib.Path) -> List[Dict[str, Any]]:
    if not history_path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    with history_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                logger.warning("skipping malformed history line")
    return rows


def _collect_baseline_values(
    history: List[Dict[str, Any]],
    url: str,
    form_factor: str,
    short: str,
    window: int,
    chrome_version: Optional[str] = None,
) -> List[float]:
    """直近 `window` 件の履歴から対象 URL/form_factor/metric の値列を集める。

    `chrome_version` を渡すと、同じ Chrome major で測られた行だけを使う (#4765)。
    module docstring が「版が飛ぶと baseline が不連続になり誤検出の温床になる」と
    書いているとおりで、実際 2026-08-05 の #4583 は Chrome 150→151 が原因だった
    (それが chrome_version を JSONL に残すようにした理由)。しかし記録するだけで
    baseline 側が版を跨いだままだったため、2026-08-07 に mobile SI が全 11 URL
    同時に 2849→4421 と跳ね、サイト側の資産変更が無いのに #4652 が鳴った。

    版が変わった直後は同版のサンプルが MIN_BASELINE_SAMPLES に届かないので
    baseline は None になり、育つまで鳴らない (baseline_for 参照)。これは
    「URL が入れ替わった直後は鳴らさない」既存の degrade と同じ挙動。
    """
    values: List[float] = []
    for row in reversed(history):
        if row.get("url") != url or row.get("form_factor") != form_factor:
            continue
        if chrome_version is not None and row.get("chrome_version") != chrome_version:
            continue
        v = row.get(short)
        if isinstance(v, (int, float)):
            values.append(float(v))
        if len(values) >= window:
            break
    return values


def baseline_for(
    history: List[Dict[str, Any]],
    url: str,
    form_factor: str,
    short: str,
    window: int,
    min_samples: int = MIN_BASELINE_SAMPLES,
    chrome_version: Optional[str] = None,
) -> Optional[float]:
    """直近 `window` 件の履歴から baseline (median) を作る。値のない行は無視。

    `min_samples` 未満しか集まらなければ None を返す (= 判定を見送る)。計測対象は
    GSC 週次上位から採るので週替わりで URL が入れ替わり、入れ替わった直後は
    必ず履歴が薄い。薄い baseline で比較を始めると、劣化ではなく計測揺れを
    拾ってしまう。Chrome major が変わった直後も同じ扱いになる (#4765)。
    """
    values = _collect_baseline_values(history, url, form_factor, short, window,
                                      chrome_version=chrome_version)
    if len(values) < min_samples:
        return None
    return statistics.median(values)


def mad_for(
    history: List[Dict[str, Any]],
    url: str,
    form_factor: str,
    short: str,
    window: int,
    min_samples: int = MIN_BASELINE_SAMPLES,
    chrome_version: Optional[str] = None,
) -> Optional[float]:
    """baseline と同じ値列から MAD (median absolute deviation) を作る (#4160)。

    baseline_for と同じ min_samples / chrome_version ガードを使う (baseline が
    None なのに MAD だけ出るのは筋が悪い)。
    """
    values = _collect_baseline_values(history, url, form_factor, short, window,
                                      chrome_version=chrome_version)
    if len(values) < min_samples:
        return None
    med = statistics.median(values)
    return statistics.median([abs(v - med) for v in values])


def upper_fence_for(
    history: List[Dict[str, Any]],
    url: str,
    form_factor: str,
    short: str,
    window: int,
    min_samples: int = MIN_BASELINE_SAMPLES,
    chrome_version: Optional[str] = None,
) -> Optional[float]:
    """baseline と同じ値列から Tukey の上側 fence (Q3 + K*IQR) を作る (#5264)。

    baseline_for / mad_for と同じ min_samples / chrome_version ガードを使う。
    値が 2 件未満で四分位が計算できないときは None (= この条件を課さない)。
    """
    values = _collect_baseline_values(history, url, form_factor, short, window,
                                      chrome_version=chrome_version)
    if len(values) < min_samples or len(values) < 2:
        return None
    q1, _, q3 = statistics.quantiles(values, n=4)
    return q3 + REGRESSION_FENCE_K * (q3 - q1)


def warmup_gates(
    history: List[Dict[str, Any]], current: List[Dict[str, Any]], window: int = 10
) -> List[Dict[str, Any]]:
    """baseline が育っておらず**判定を見送っている** (url, form_factor, metric) を返す。

    なぜ要るか (#5264):
      baseline は同一 Chrome major の行だけで作る (#4765) ので、Chrome の major が
      上がるたびに `MIN_BASELINE_SAMPLES` 日ぶんゲートが沈黙する。2026-08-07 の
      major 更新では 8/14 まで沈黙し、系列で最大の 2 スパイク (231.5 / 272.5) は
      一度も鳴らないまま、それより小さい 150 が「ウォームアップ明けの初日」だから
      鳴った。Chrome major は約 4 週ごとに上がるので、この盲点は定常的に出る。

      沈黙そのものは設計どおり (薄い baseline で比べると計測揺れを拾う) なので
      変えない。**沈黙していることを見えるようにする**のがここの役割で、
      「鳴っていない = 健全」と読まれるのを防ぐ (#4789 と同じ思想)。
    """
    out: List[Dict[str, Any]] = []
    for row in current:
        url, ff = row.get("url"), row.get("form_factor")
        for _, short in METRIC_MAP:
            if row.get(short + "_error") or not isinstance(row.get(short), (int, float)):
                continue
            values = _collect_baseline_values(history, url, ff, short, window,
                                              chrome_version=row.get("chrome_version"))
            if len(values) < MIN_BASELINE_SAMPLES:
                out.append({
                    "url": url, "form_factor": ff, "metric": short,
                    "samples": len(values), "needed": MIN_BASELINE_SAMPLES,
                    "chrome_version": row.get("chrome_version"),
                })
    return out


def run_wide_shift(
    history: List[Dict[str, Any]], current: List[Dict[str, Any]], window: int = 10
) -> Dict[str, Dict[str, Any]]:
    """その run 全体が一様に悪化している metric を返す (#5320)。

    metric ごとに、baseline を持つ全 (url, form_factor) の `value / baseline` を集め、

      - 中央比 >= RUN_SHIFT_RATIO   (run 全体が沈んでいる)
      - かつ 比 >= RUN_SHIFT_RATIO の URL が RUN_SHIFT_FRACTION 以上 (一様である)

    の両方を満たすものを「計測環境由来」と判定する。中央比だけだと tbt のように
    裾の重い metric で 1 本の外れ値に引かれ、割合だけだと (URL × 日) 単位で飛ぶ
    #5264 の裾を拾う。両方を課してはじめて「run 全体が同じ幅で沈んだ」だけが残る。

    戻り値は metric → {median_ratio, fraction, samples}。**空 dict = 汚染なし**。
    値は 1 方向 (悪化側) だけ見る — 全 URL が一斉に速くなった run を疑う理由は無い。
    """
    shifted: Dict[str, Dict[str, Any]] = {}
    for _, short in METRIC_MAP:
        ratios: List[float] = []
        for row in current:
            if row.get(short + "_error"):
                continue
            value = row.get(short)
            if not isinstance(value, (int, float)):
                continue
            base = baseline_for(history, row.get("url"), row.get("form_factor"),
                                short, window,
                                chrome_version=row.get("chrome_version"))
            # base が 0 (CLS が常時 0 の URL など) は比を作れないので除く。
            if not base:
                continue
            ratios.append(value / base)
        if len(ratios) < MIN_RUN_SHIFT_SAMPLES:
            continue
        median_ratio = statistics.median(ratios)
        over = sum(1 for r in ratios if r >= RUN_SHIFT_RATIO)
        fraction = over / len(ratios)
        if median_ratio >= RUN_SHIFT_RATIO and fraction >= RUN_SHIFT_FRACTION:
            shifted[short] = {
                "median_ratio": round(median_ratio, 3),
                "fraction": round(fraction, 3),
                "samples": len(ratios),
                "over": over,
            }
    return shifted


def _ratio_regression(value: float, base: float, min_delta: float) -> bool:
    """REGRESSION_RATIO 条件 + 絶対差条件 (MAD ゲートより手前の素朴な判定)。"""
    return value > base * REGRESSION_RATIO and (value - base) >= min_delta


def lcp_is_unmeasured(row: Dict[str, Any]) -> bool:
    """その行の `lcp` / `observed_lcp` が合成値 (= LCP を計測できていない) か (#4441)。

    判定は `lcp_element_reason` を見る (LCP_UNMEASURED_REASONS の定義を参照)。
    ただし `lcp_element` が入っている行は、実 LCP 候補が取れている以上「計測でき
    ていない」ではない。2026-08-07〜08-15 の集約バグ (#5081 項目2) で両方が入っ
    た行が JSONL に残っているので、element 優先で判定して過去行もその場で救う。
    """
    if row.get("lcp_element"):
        return False
    return row.get("lcp_element_reason") in LCP_UNMEASURED_REASONS


def _observed_lcp_confirms(
    history: List[Dict[str, Any]], row: Dict[str, Any], url: Any, ff: Any, window: int
) -> bool:
    """simulated LCP の悪化を observed LCP でも裏付けられるか判定する (#4160)。

    実測で判明した問題: JSONL に記録している `lcp` は throttlingMethod=simulate
    (Lantern) の推定値で、navi ホームでは simulated LCP が simulated TTI と
    ほぼ一致していた (= LCP 要素でなく JS/ネットワークの末端を追っていた)。一方
    `observedLargestContentfulPaint` (実描画) は 1424〜1497ms で observedFCP と
    同値のまま安定していた。simulated だけが動いた場合に鳴らすと、この種の
    「計測アーティファクト」を回帰として誤検出する。

    observed 側の履歴・当日値が両方揃っている場合だけ「observed 側も baseline
    比で悪化しているか」を追加条件にする。observed の記録は #4160 以降の行にしか
    無いため、揃うまでは判定できない → その間は degrade して simulated だけの
    従来判定を維持する (揃っていない = False を返さない = 呼び出し側で無条件許可)。
    """
    cur_observed = row.get("observed_lcp")
    if not isinstance(cur_observed, (int, float)):
        return True  # degrade: observed が無ければ simulated だけで判定
    base_observed = baseline_for(history, url, ff, "observed_lcp", window,
                                 chrome_version=row.get("chrome_version"))
    if base_observed is None:
        return True  # degrade: observed の履歴がまだ育っていない
    min_delta = MIN_ABS_DELTA.get("lcp", 0.0)
    return _ratio_regression(float(cur_observed), base_observed, min_delta)


def detect_regressions(
    history: List[Dict[str, Any]], current: List[Dict[str, Any]], window: int = 10,
    shifted: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """current の各行を履歴 baseline と比べて劣化を列挙する。

    鳴らす条件:
      1. audit error (NO_LCP など) が出た                     → kind="error"
      2. CWV good 閾値を「跨いで」悪化した                     → kind="threshold"
      3. baseline 比 REGRESSION_RATIO 以上 かつ 絶対差/MAD 差が十分 → kind="relative"
      4. perf score が SCORE_DROP_POINTS (or MAD 由来の閾値) 以上落ちた → kind="score"

    2〜4 はいずれも baseline が MIN_BASELINE_SAMPLES 件以上ある URL だけが対象。
    履歴が足りない URL は「絶対値が悪い」だけでは鳴らさない (baseline_for 参照)。

    #4160 (2026-08-02) の分散対応:
      実履歴を replay したところ、baseline (median) と REGRESSION_RATIO=1.25 だけ
      では誤検出だった。ホーム (mobile) の LCP 系列は n=20 で stdev=957
      (min=2425 / max=5748) と実行ごとの揺れが大きく、1.25x はこの分散に対して
      1.2σ 相当にしかならない。そこで baseline の MAD (median absolute
      deviation) を計算し、`value > baseline + REGRESSION_MAD_K * MAD` を
      relative/score 判定の追加の必須条件にする (AND)。MAD が小さい/0 の指標
      まで緩めないよう、下限として既存の MIN_ABS_DELTA / SCORE_DROP_POINTS を
      使う (`max(固定値, MAD由来)`)。
      さらに `lcp` については simulated (Lantern 推定) 単独の悪化では鳴らさず、
      observed 値 (実描画) が揃っていればそちらも悪化していることを要求する
      (_observed_lcp_confirms 参照)。observed の履歴が無い期間は degrade して
      従来どおり simulated だけで判定する。
      `kind="error"` (audit error / runtime error) は計測基盤の失敗検出であり
      分散の話とは無関係なので、これらのゲートを通さず無条件で鳴らし続ける。

    #5320 (2026-08-15) の run 全体汚染ガード:
      run 全体が一様に沈んだ metric (run_wide_shift 参照) は、その run では判定を
      見送る。metric ごとに独立して外すので、汚染されていない metric のアラートは
      そのまま残る。`shifted` を渡さなければここで計算する (呼び出し側が report に
      出すために先に計算しているときは渡す)。
    """
    if shifted is None:
        shifted = run_wide_shift(history, current, window=window)
    alerts: List[Dict[str, Any]] = []
    for row in current:
        url = row.get("url")
        ff = row.get("form_factor")
        # baseline は同じ Chrome major で測った行だけから作る (#4765)。
        # None (抽出失敗 / chrome_version 導入前の行) のときは版で絞らず従来
        # 挙動に degrade する — 抽出が一時的にコケただけでゲートが恒久的に
        # 黙るほうが、版跨ぎの誤検出より悪い。
        cur_chrome = row.get("chrome_version")

        if row.get("runtime_error"):
            alerts.append({
                "kind": "error", "url": url, "form_factor": ff, "metric": "runtime",
                "detail": "runtime error: {}".format(row["runtime_error"]),
            })

        for _, short in METRIC_MAP:
            err = row.get(short + "_error")
            if err:
                alerts.append({
                    "kind": "error", "url": url, "form_factor": ff, "metric": short,
                    "detail": "audit error: {} ({}/{} runs)".format(
                        err, row.get(short + "_error_runs", 1), row.get("runs", 1)
                    ),
                })
                continue

            value = row.get(short)
            if not isinstance(value, (int, float)):
                continue
            base = baseline_for(history, url, ff, short, window,
                                chrome_version=cur_chrome)
            good = GOOD_THRESHOLDS.get(short)

            if base is None:
                # baseline が育つまでは鳴らさない (記録だけ残す)。
                # 旧実装はここで「閾値超え」を鳴らしていたが、navi の商品ページは
                # mobile LCP 5.4s / FCP 2.0s が常態で good 閾値を恒常的に超えている。
                # 計測対象は GSC 週次上位なので週替わりで URL が入れ替わり、その
                # たびに全新規 URL 分の閾値アラートが一斉に出ていた (2026-07-26 の
                # 10 件がこれ)。これは「回帰」ではなく既知の遅さなので、このレーン
                # ではなく #1301 (CWV 改善) の領分。
                continue

            min_delta = MIN_ABS_DELTA.get(short, 0.0)
            mad = mad_for(history, url, ff, short, window,
                          chrome_version=cur_chrome)
            # MAD が拾えない/0 のときは MIN_ABS_DELTA を下限にする
            # (MAD=0 のまま K 倍しても 0 になり、ゲートが機能しなくなるのを防ぐ)。
            required_delta = max(min_delta, REGRESSION_MAD_K * mad) if mad else min_delta

            crossed = good is not None and base <= good < value
            worse = value > base * REGRESSION_RATIO and (value - base) >= required_delta

            # #5320: run 全体が一様に沈んでいる metric は、この run では判定しない。
            # URL 単位のゲート (MAD / fence) は run 単位の汚染を原理的に見抜けない
            # ので、その手前で外す。無音で消すと「鳴らないから健全」に見えるので
            # 必ずログに残す (report にも run_shift セクションとして出る)。
            if (crossed or worse) and short in shifted:
                info = shifted[short]
                logger.warning(
                    "gate suppressed (run-wide shift, median=%.2fx on %d/%d URLs): "
                    "%s [%s] %s baseline=%s value=%s",
                    info["median_ratio"], info["over"], info["samples"],
                    url, ff, short, base, value,
                )
                crossed = False
                worse = False

            # #5264: baseline 窓の Tukey fence を超えないなら、その値は「窓の中で
            # 既に起きている振れ幅の内側」であって劣化の証拠にならない。threshold
            # (good 閾値跨ぎ) にも同じ条件を課す — 二峰性の系列では高い側のクラスタが
            # 再発するたびに閾値を跨ぐので、relative だけ塞いでも同じ日に threshold で
            # 鳴り直すだけになる。
            fence = upper_fence_for(history, url, ff, short, window,
                                    chrome_version=cur_chrome)
            if (crossed or worse) and fence is not None and value <= fence:
                # 無音で消すと「鳴らないから健全」に見えるので必ずログに残す。
                logger.warning(
                    "gate suppressed (within baseline spread, fence=%.1f): %s [%s] "
                    "%s baseline=%s value=%s",
                    fence, url, ff, short, base, value,
                )
                crossed = False
                worse = False

            if (crossed or worse) and short == "lcp":
                if lcp_is_unmeasured(row):
                    # LCP 候補が確定していない行 (#4441)。値は入っているが LCP では
                    # ないので比較しない。無音で消すと「鳴らないから健全」に見えるので
                    # 必ずログに残す。
                    logger.warning(
                        "lcp gate skipped (LCP unmeasured, reason=%s): %s [%s] "
                        "lcp=%s observed_lcp=%s",
                        row.get("lcp_element_reason"), url, ff,
                        value, row.get("observed_lcp"),
                    )
                    crossed = False
                    worse = False
                # simulated だけの悪化は鳴らさない (揃っていなければ degrade)。
                elif not _observed_lcp_confirms(history, row, url, ff, window):
                    crossed = False
                    worse = False

            if crossed:
                alerts.append({
                    "kind": "threshold", "url": url, "form_factor": ff, "metric": short,
                    "value": value, "baseline": base,
                    "detail": "{} が good 閾値 {} を跨いで悪化: {} → {}".format(short, good, base, value),
                })
            elif worse:
                alerts.append({
                    "kind": "relative", "url": url, "form_factor": ff, "metric": short,
                    "value": value, "baseline": base,
                    "detail": "{} が baseline 比 {:.2f}x 悪化: {} → {}".format(
                        short, value / base if base else 0, base, value
                    ),
                })

        score = row.get("perf_score")
        if isinstance(score, (int, float)):
            base_score = baseline_for(history, url, ff, "perf_score", window,
                                      chrome_version=cur_chrome)
            if base_score is not None:
                score_mad = mad_for(history, url, ff, "perf_score", window,
                                    chrome_version=cur_chrome)
                required_drop = max(SCORE_DROP_POINTS, REGRESSION_MAD_K * score_mad) if score_mad else SCORE_DROP_POINTS
                # perf_score は fcp/si/lcp/tbt/cls の加重合成なので、素の metric が
                # run 全体で汚染されていればスコアも必ず巻き添えで落ちる (#5320)。
                # 2026-08-15 の score 3 件は fcp/si の汚染の写像だった。
                if (base_score - score) >= required_drop and shifted:
                    logger.warning(
                        "score gate suppressed (run-wide shift on %s): %s [%s] "
                        "baseline=%s value=%s",
                        ", ".join(sorted(shifted)), url, ff, base_score, score,
                    )
                elif (base_score - score) >= required_drop:
                    alerts.append({
                        "kind": "score", "url": url, "form_factor": ff, "metric": "perf_score",
                        "value": score, "baseline": base_score,
                        "detail": "performance score が {} → {} に低下".format(base_score, score),
                    })
    return alerts


def _alert_key(alert: Dict[str, Any]) -> Tuple[Any, Any, Any, Any]:
    return (alert.get("url"), alert.get("form_factor"), alert.get("metric"), alert.get("kind"))


def _replay_alerts_for_date(
    history: List[Dict[str, Any]], day: str, window: int,
) -> List[Dict[str, Any]]:
    """`day` 時点で detect_regressions が何を鳴らしていたかを履歴から再現する (#6426)。

    baseline は `day` より前の履歴だけで作る (当時実際に走った判定と同じ土俵にする)。
    `day` の計測行が無ければ (欠測日) 空を返す。
    """
    hist_before = [r for r in history if r.get("date") < day]
    rows_on_day = [r for r in history if r.get("date") == day]
    if not rows_on_day:
        return []
    shifted = run_wide_shift(hist_before, rows_on_day, window=window)
    return detect_regressions(hist_before, rows_on_day, window=window, shifted=shifted)


def filter_consecutive_regressions(
    history: List[Dict[str, Any]],
    alerts: List[Dict[str, Any]],
    target_date: str,
    window: int,
    days_required: int = REGRESSION_CONSECUTIVE_DAYS,
) -> List[Dict[str, Any]]:
    """`days_required` 日連続で鳴っていない劣化アラートを削る (#6426)。

    kind="error" は無条件で通す (REGRESSION_CONSECUTIVE_DAYS のコメント参照)。
    それ以外 (threshold/relative/score) は、前日以前の履歴を replay して同じ
    (url, form_factor, metric, kind) が続けて鳴っていたかを確認する。履歴が
    無い日 (計測が飛んだ日、#4785) は「続いていない」扱いにする — 連続性を
    確認できない以上、鳴らさない側に倒す。
    """
    if days_required <= 1 or not alerts:
        return alerts
    cache: Dict[str, List[Dict[str, Any]]] = {}

    def replay(day: str) -> List[Dict[str, Any]]:
        if day not in cache:
            cache[day] = _replay_alerts_for_date(history, day, window)
        return cache[day]

    confirmed: List[Dict[str, Any]] = []
    for alert in alerts:
        if alert.get("kind") == "error":
            confirmed.append(alert)
            continue
        key = _alert_key(alert)
        day = target_date
        ok = True
        for _ in range(days_required - 1):
            day = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
            if key not in {_alert_key(a) for a in replay(day)}:
                ok = False
                break
        if ok:
            confirmed.append(alert)
    return confirmed


def render_report(
    alerts: List[Dict[str, Any]],
    target_date: str,
    lcp_unmeasured: Optional[List[Dict[str, Any]]] = None,
    warmup: Optional[List[Dict[str, Any]]] = None,
    run_shift: Optional[Dict[str, Dict[str, Any]]] = None,
) -> str:
    """劣化レポート Markdown。1 issue に集約する前提で全 URL 分を 1 本にまとめる。

    `lcp_unmeasured` を渡すと、その run で LCP ゲートを外した URL を脚注に出す
    (#4441)。issue を読む人が「この URL は LCP を見ていない」を知らずに
    「LCP は鳴っていない = LCP は健全」と読むのを防ぐため。

    `run_shift` を渡すと、run 全体の汚染で判定を見送った metric を脚注に出す
    (#5320)。同じく「鳴っていない = 健全」と読まれるのを防ぐため。
    """
    lines = [
        "## Lighthouse ラボ計測 劣化検出 ({})".format(target_date),
        "",
        "self-hosted Lighthouse レーン (#2995 案6) が回帰を検出しました。",
        "各行は {} 回実行の median です。".format(DEFAULT_RUNS),
        "",
    ]
    errors = [a for a in alerts if a["kind"] == "error"]
    perf = [a for a in alerts if a["kind"] != "error"]

    if errors:
        lines += [
            "### 計測エラー (値が取れていない)",
            "",
            "計測基盤側の失敗の可能性があります (2026-07-16 の PSI `NO_LCP` 相当)。",
            "サイトの劣化と即断せず、まず再計測してください。",
            "",
            "| URL | form factor | metric | 詳細 |",
            "| --- | --- | --- | --- |",
        ]
        for a in errors:
            lines.append("| {} | {} | {} | {} |".format(
                a["url"], a["form_factor"], a["metric"], a["detail"]))
        lines.append("")

    if perf:
        lines += [
            "### パフォーマンス劣化",
            "",
            "| URL | form factor | metric | baseline | 今回 | 判定 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for a in perf:
            base = a.get("baseline")
            lines.append("| {} | {} | {} | {} | {} | {} |".format(
                a["url"], a["form_factor"], a["metric"],
                "-" if base is None else base, a.get("value", "-"), a["kind"]))
        lines.append("")

    if lcp_unmeasured:
        lines += [
            "### LCP を判定していない URL ({} 件)".format(len(lcp_unmeasured)),
            "",
            "これらの URL は trace に LCP 候補が 1 件も無く、`lcp` / `observed_lcp` は",
            "lighthouse が UKM の Invalidate から合成した値です (#4441)。LCP としては",
            "比較できないのでゲートから外しています。**「LCP が鳴っていない = LCP が健全」",
            "とは読めません。**",
            "",
            "| URL | form factor | reason |",
            "| --- | --- | --- |",
        ]
        for r in lcp_unmeasured:
            lines.append("| {} | {} | {} |".format(
                r.get("url"), r.get("form_factor"), r.get("lcp_element_reason")))
        lines.append("")

    if run_shift:
        lines += [
            "### run 全体が沈んでいて判定を見送った metric ({} 件)".format(len(run_shift)),
            "",
            "計測対象の**大半の URL が同時に同じ幅で**悪化しています。デプロイでこの形には",
            "ならない (共通アセットの劣化なら翌日も続く) ので、self-hosted runner 側が",
            "この run のあいだだけ重かったと判断し、これらの metric は判定から外しました",
            "(#5320)。**「鳴っていない = 健全」とは読めません。**",
            "",
            "翌日の run で元の水準に戻らない場合は、本当にサイト側が劣化しています。",
            "",
            "| metric | run 中央比 | 1.25x 超の URL |",
            "| --- | --- | --- |",
        ]
        for m in sorted(run_shift):
            info = run_shift[m]
            lines.append("| {} | {:.2f}x | {}/{} |".format(
                m, info["median_ratio"], info["over"], info["samples"]))
        lines.append("")

    if warmup:
        by_metric: Dict[str, int] = {}
        for w in warmup:
            by_metric[w["metric"]] = by_metric.get(w["metric"], 0) + 1
        lines += [
            "### baseline が育っておらず判定を見送っている項目 ({} 件)".format(len(warmup)),
            "",
            "baseline は**同一 Chrome major の行だけ**から作る (#4765) ため、Chrome の",
            "major が上がるたびに {} 日ぶんゲートが沈黙します。**「鳴っていない = 健全」".format(MIN_BASELINE_SAMPLES),
            "とは読めません** (#5264)。",
            "",
            "| metric | 件数 |",
            "| --- | --- |",
        ]
        for metric in sorted(by_metric):
            lines.append("| {} | {} |".format(metric, by_metric[metric]))
        lines.append("")

    lines += ["Refs #2995 (案6) / #1357 (epic E2)"]
    return "\n".join(lines)


def latest_gsc_history() -> Optional[pathlib.Path]:
    """gsc_history/ の最新週の JSON を返す (無ければ None)。

    ファイル名が ISO 週 (2026-W28.json) なので、名前順 = 時系列順になる。
    """
    d = pathlib.Path(GSC_HISTORY_DIR)
    if not d.is_dir():
        return None
    files = sorted(d.glob("*.json"))
    return files[-1] if files else None


def get_targets(
    origin: str, gsc_path: pathlib.Path, top_urls: int, extra: Sequence[str]
) -> List[str]:
    """計測対象 URL を決める。

    origin (ホーム) + GSC 上位 URL + 明示指定分。ページ種別の代表性より
    「実際に流入がある URL の劣化を早く知る」ことを優先する。
    """
    targets: List[str] = [origin.rstrip("/") + "/"]

    if not gsc_path.exists():
        latest = latest_gsc_history()
        if latest is not None:
            logger.info("gsc input %s not found — falling back to %s", gsc_path, latest)
            gsc_path = latest

    if gsc_path.exists():
        try:
            data = json.loads(gsc_path.read_text(encoding="utf-8"))
            pages = data.get("by_page", []) or []
            pages = sorted(pages, key=lambda r: r.get("impressions", 0), reverse=True)
            for p in pages:
                path = p.get("page")
                if not path:
                    continue
                url = path if path.startswith("http") else origin.rstrip("/") + "/" + path.lstrip("/")
                if url not in targets:
                    targets.append(url)
                if len(targets) > top_urls:
                    break
        except (OSError, ValueError) as e:
            logger.warning("failed to read %s: %s — origin only", gsc_path, e)
    else:
        logger.info("gsc input %s not found — origin + --url only", gsc_path)

    for url in extra:
        if url not in targets:
            targets.append(url)
    return targets


def _record_key(row: Dict[str, Any]) -> Tuple[Any, Any, Any]:
    return (row.get("date"), row.get("url"), row.get("form_factor"))


def append_records(
    history_path: pathlib.Path, records: List[Dict[str, Any]]
) -> int:
    """`(date, url, form_factor)` につき 1 行だけを保つように append する (#4765)。

    素の append だと同じ日にレーンが 2 回走った分がそのまま二重に積まれる。
    2026-08-07 が実際にそれで、通常 11 行のところ 22 行入っていた。害は 2 つ:

      1. `detect_regressions` は current の全行を回すので、遅かった方のバッチが
         そのまま劣化として鳴る。#4652 の 8 件はこれ (翌 08-08 は Chrome 151 の
         まま si 平均 2825 と平常値に戻っており、サイト側の劣化ではない)。
      2. baseline の window は「直近 N 件 ≒ N 日」を前提にしているのに、1 日 2 行
         入ると期間が半分になり、しかも悪いバッチが median を汚す。

    再計測は「やり直し」なので後勝ちにする (同じキーの古い行を落として append)。
    """
    if not records:
        return 0
    history_path.parent.mkdir(parents=True, exist_ok=True)

    incoming = {_record_key(r) for r in records}
    if history_path.exists():
        kept = []
        dropped = 0
        for line in history_path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s:
                continue
            try:
                row = json.loads(s)
            except ValueError:
                kept.append(s)  # 壊れた行は判断材料が無いので触らない
                continue
            if _record_key(row) in incoming:
                dropped += 1
                continue
            kept.append(s)
        if dropped:
            logger.warning(
                "同じ (date, url, form_factor) の既存行 %d 件を再計測で置き換えます", dropped
            )
            history_path.write_text(
                "".join(s + "\n" for s in kept), encoding="utf-8"
            )

    with history_path.open("a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False, sort_keys=True))
            f.write("\n")
    return len(records)


def logical_date(now: datetime, offset_hours: float) -> str:
    """壁時計から offset だけ戻した「計測日」を ISO 文字列で返す (#4785)。

    now は tz-aware を想定 (naive なら UTC とみなす)。cron の予定時刻が真夜中の
    手前にあると、schedule dispatch の遅延がそのまま日付のズレになるので、
    境界を実行時刻から十分離れたところへ動かす。
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (now.astimezone(timezone.utc) - timedelta(hours=offset_hours)).date().isoformat()


def missing_dates(history: Sequence[Dict[str, Any]], target_date: str,
                  cap: int = 30) -> List[str]:
    """履歴の最終計測日と target_date の間で計測が飛んだ日を返す (#4785)。

    lane が 1 日走らなかったこと自体は run が存在しないので気づけず、
    lighthouse_history.jsonl にも「無い」という痕跡が残らない (実際に 2026-08-06 が
    欠測していたが誰も気づかなかった)。ここで検出して _meta に残す。

    履歴が空 / 日付が壊れている / target が最終日以前 (再計測) のときは空を返す。
    cap は初回投入や長期停止で戻り値が暴れないための上限。
    """
    seen: List[date] = []
    for row in history:
        raw = row.get("date")
        if not isinstance(raw, str):
            continue
        try:
            seen.append(date.fromisoformat(raw))
        except ValueError:
            continue
    if not seen:
        return []
    try:
        target = date.fromisoformat(target_date)
    except (TypeError, ValueError):
        return []
    last = max(seen)
    if target <= last:
        return []
    gap = [(last + timedelta(days=i)).isoformat() for i in range(1, (target - last).days)]
    return gap[:cap]


def load_seen(seen_path: pathlib.Path) -> Dict[str, Any]:
    if not seen_path.exists():
        return {}
    try:
        return json.loads(seen_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_seen(seen_path: pathlib.Path, seen: Dict[str, Any]) -> None:
    seen_path.parent.mkdir(parents=True, exist_ok=True)
    seen_path.write_text(
        json.dumps(seen, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description="self-hosted Lighthouse CWV lane (#2995 案6)")
    p.add_argument("--origin", default=os.environ.get("LH_ORIGIN", DEFAULT_ORIGIN))
    p.add_argument("--url", action="append", default=[],
                   help="追加で計測する URL (複数可)")
    p.add_argument("--gsc-input", default=DEFAULT_GSC_INPUT)
    p.add_argument("--top-urls", type=int, default=DEFAULT_TOP_URLS)
    p.add_argument("--runs", type=int, default=DEFAULT_RUNS,
                   help="URL ごとの実行回数 (median を採る)")
    p.add_argument("--form-factors", nargs="+", default=["mobile"],
                   choices=["mobile", "desktop"])
    p.add_argument("--lighthouse-cmd",
                   default=os.environ.get("LIGHTHOUSE_CMD", DEFAULT_LIGHTHOUSE_CMD))
    p.add_argument("--history-dir", default=DEFAULT_HISTORY_DIR)
    # 既定は None。実際の値は論理日 (#4785) から引数解析後に決める。
    # module import 時に date.today() を固定していた旧既定は、長寿命プロセスで
    # 日付が固まる潜在バグでもあった。
    p.add_argument("--target-date", default=None,
                   help="計測日を明示する (既定: 論理日 = UTC 現在時刻 - --day-offset-hours)")
    p.add_argument("--day-offset-hours", type=float, default=DEFAULT_DAY_OFFSET_HOURS,
                   help="論理日の境界を壁時計から何時間戻すか "
                        f"(既定 {DEFAULT_DAY_OFFSET_HOURS}, env LH_DAY_OFFSET_HOURS)")
    p.add_argument("--baseline-window", type=int, default=10)
    p.add_argument("--consecutive-days", type=int, default=REGRESSION_CONSECUTIVE_DAYS,
                   help="劣化を報告するのに何日連続で条件を満たす必要があるか "
                        f"(既定 {REGRESSION_CONSECUTIVE_DAYS}, env "
                        "LH_REGRESSION_CONSECUTIVE_DAYS, #6426)")
    p.add_argument("--report-out", default=None,
                   help="劣化があったとき Markdown を書き出すパス")
    p.add_argument("--dry-run", action="store_true",
                   help="JSONL に書かず結果を stdout に出すだけ")
    args = p.parse_args(argv)

    target_date = args.target_date or logical_date(
        datetime.now(timezone.utc), args.day_offset_hours)
    logger.info("target date: %s (offset %.1fh)", target_date, args.day_offset_hours)

    history_dir = pathlib.Path(args.history_dir)
    history_path = history_dir / LIGHTHOUSE_HISTORY_FILENAME
    seen_path = history_dir / SEEN_DATES_FILENAME

    targets = get_targets(
        args.origin, pathlib.Path(args.gsc_input), args.top_urls, args.url
    )
    logger.info("targets: %d URL x %s x %d runs",
                len(targets), args.form_factors, args.runs)

    history = load_history(history_path)
    # 論理日にしても runner 停止などで日が飛ぶことはある。飛んだこと自体は
    # 「run が存在しない」ので後から気づけないため、痕跡を seen_dates.json に残す
    # (#4785)。ここでは報告だけで、計測は通常どおり続ける。
    gap = missing_dates(history, target_date)
    if gap:
        logger.warning("::warning::lighthouse history gap: %d day(s) with no "
                       "measurement before %s: %s",
                       len(gap), target_date, ", ".join(gap))
    current: List[Dict[str, Any]] = []

    for url in targets:
        for ff in args.form_factors:
            runs: List[Dict[str, Any]] = []
            for i in range(args.runs):
                logger.info("lighthouse %s (%s) run %d/%d", url, ff, i + 1, args.runs)
                r = run_lighthouse_once(url, args.lighthouse_cmd, ff)
                if r is not None:
                    runs.append(r)
            agg = aggregate_runs(runs)
            if not agg:
                logger.warning("all runs failed for %s (%s) — skipping row", url, ff)
                continue
            current.append({
                "date": target_date, "url": url, "form_factor": ff, **agg,
            })

    if not current:
        logger.error("no successful measurements — leaving history untouched")
        return 1

    # run 全体の汚染は先に測る — detect_regressions に渡すのと、alerts が 0 件に
    # なって report が出ない場合でも痕跡を残すのと、両方に要る (#5320)。
    run_shift = run_wide_shift(history, current, window=args.baseline_window)
    if run_shift:
        logger.warning(
            "::warning::lighthouse run-wide shift on %s — these metrics were not "
            "judged this run (suspected runner-side slowdown, #5320): %s",
            target_date,
            "; ".join("{} median={:.2f}x on {}/{} URLs".format(
                m, run_shift[m]["median_ratio"], run_shift[m]["over"],
                run_shift[m]["samples"]) for m in sorted(run_shift)),
        )
    raw_alerts = detect_regressions(history, current, window=args.baseline_window,
                                    shifted=run_shift)
    alerts = filter_consecutive_regressions(
        history, raw_alerts, target_date, args.baseline_window,
        days_required=args.consecutive_days,
    )
    if len(raw_alerts) != len(alerts):
        confirmed_keys = {_alert_key(a) for a in alerts}
        suppressed = [a for a in raw_alerts if _alert_key(a) not in confirmed_keys]
        logger.warning(
            "::warning::%d alert(s) suppressed as single-day spike (need %d "
            "consecutive days, #6426): %s",
            len(suppressed), args.consecutive_days,
            "; ".join(a["detail"] for a in suppressed),
        )
    warmup = warmup_gates(history, current, window=args.baseline_window)
    if warmup:
        # 沈黙を可視化する (#5264)。Chrome major が上がるたびに必ず出るので
        # ::warning:: にはせず info で残す。
        logger.info("gate warm-up: %d (url, form_factor, metric) not compared yet "
                    "(need %d samples on the same Chrome major)",
                    len(warmup), MIN_BASELINE_SAMPLES)

    if args.dry_run:
        for row in current:
            print(json.dumps(row, ensure_ascii=False, sort_keys=True))
        for a in alerts:
            print("ALERT: {}".format(a["detail"]))
        return 0

    n = append_records(history_path, current)
    logger.info("appended %d rows to %s", n, history_path)

    seen = load_seen(seen_path)
    seen.setdefault("lighthouse", {})[target_date] = True
    meta = seen.get("_meta") or {}
    meta["lighthouse_last_run_utc"] = datetime.now(timezone.utc).isoformat()
    # 欠測日を累積で持つ (#4785)。これが空でない = 計測が飛んだ日がある、を
    # 独立レーンから監査できるようにするための痕跡。
    if gap:
        known = meta.get("lighthouse_missing_dates")
        known = known if isinstance(known, list) else []
        meta["lighthouse_missing_dates"] = sorted(set(known) | set(gap))
    # run 全体の汚染も痕跡を残す (#5320)。alerts が全部抑制されると report も issue も
    # 出ないので、ここに残さないと「その日は健全だった」と区別が付かなくなる。
    # 汚染が特定曜日や特定時間帯に偏っていないかを後から監査するための材料でもある。
    if run_shift:
        known_shift = meta.get("lighthouse_run_shifts")
        known_shift = known_shift if isinstance(known_shift, dict) else {}
        known_shift[target_date] = run_shift
        meta["lighthouse_run_shifts"] = known_shift
    seen["_meta"] = meta
    save_seen(seen_path, seen)

    unmeasured = [r for r in current if lcp_is_unmeasured(r)]
    if unmeasured:
        logger.warning(
            "LCP unmeasured on %d/%d rows (gate skipped): %s",
            len(unmeasured), len(current),
            ", ".join(sorted({str(r.get("url")) for r in unmeasured})),
        )

    if alerts:
        logger.warning("%d regression alert(s) detected", len(alerts))
        report = render_report(alerts, target_date, unmeasured, warmup, run_shift)
        if args.report_out:
            pathlib.Path(args.report_out).write_text(report, encoding="utf-8")
            logger.info("wrote report to %s", args.report_out)
        else:
            print(report)
    else:
        logger.info("no regressions detected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
