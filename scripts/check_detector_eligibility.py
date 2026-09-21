"""check_detector_eligibility.py

A レーン検出器 (epic #1356) の **母数** を記録し、母数が枯れたら鳴らす観察レーン。
#5941 の副次項目 / amazon-navi-brain#18 の (b) 軽い版。

なぜ必要か (2026-09-01):
  A-2 / A-3 / A-6 は 4 週連続で 0 件だった。調べると「該当が無い」のではなく
  **「閾値に届く母数が存在しない」**状態で、閾値を下げると即座に候補が出た。
  つまり検出器は正常に走って正しく 0 を返しており、**0 が正しいのかを誰も
  確認していなかった**。#5941 の health check は「落ちたか」しか見ないので、
  この状態はずっと緑のままだった。

  同じ「0 件」でも処方が逆になる 2 つの状態がある:

    eligible == 0                  … 母数が無い。**閾値側**の問題 (A-2/A-3/A-6)
    eligible > 0 かつ detected == 0 … 母数はあるが選別条件で落ちている。
                                      **サイト側**の問題 (A-4 がこれ)

  `detected` だけ見ていると両者が区別できない。そこで各検出器が
  `eligible` (= 量のしきい値を通った母数) を `detected` と別に出し、
  ここでその推移を残して判定する。

なぜ「2 週連続」で鳴らすか:
  週次の母数は小さく、1 週だけ 0 になることは正常系としてありうる
  (brain#18 のスイープでも週によって 0 が混ざる)。1 週で鳴らすと
  「正常な揺れ」を毎月拾うことになり、また誰も見なくなる。

なぜ jsonl に残すか (この設計の前提):
  **検出器の出力 JSON (data/analytics/*.json) は git に入っていない。**
  17-analytics-report が commit-back するのは gsc_history/ と query_intent.json
  だけなので、次回 run の runner には前回のファイルが存在しない。
  「前回の eligible」を出力ファイルから読む実装は動かない (2026-09-01 に確認)。
  ここで tracked な jsonl に 1 run 1 行だけ残し、それを前回値の唯一の出どころにする。

amazon-navi-brain#56 (2026-09-20) での再訪:
  導入から 3 週たっても A-4 (engagement_drop) / A-5 (orphan_pages) / A-3
  (cannibalization) が eligible=0 の警告を鳴らし続けていた。調べ直すと 2 つの
  型が混ざっていた:

    - A-3 (cannibalization) … サイトのトラフィックがさらに減り、閾値が再び
      大きすぎた (#18 と同じ「閾値側」の型)。detect_cannibalization.py の
      既定値を #56 で再較正した
    - A-5 (orphan_pages) … 同じ型に見えたが、#56 のレビューで「前提の GA4 PV
      急落を GSC 側が裏付けていない」「min_pv を下げると entrance_ratio が
      二値に縮退する」と指摘され、閾値の変更は一旦保留にした
      (detect_orphan_pages.py のコメント参照)
    - A-4 … #18 で「下げてもゲートが選別しなくなる」と診断済みで、据え置く
      と決めた検出器だった。据え置いた以上 eligible=0 は定常的に鳴り続ける
      構造で、鳴らし続けても閾値側の合図にならない (#18 の最終コメントで
      「eligible==0 warning の対象から外す」と決めていたが、実装が漏れていた)。
      NO_ELIGIBILITY_WARNING で恒久的に対象から外す。

  amazon-navi-brain#57 (2026-09-21) での再訪:
    #56 で保留にした A-5 の前提 (GA4 PV 急落) の原因が #57 で判明した:
    Lighthouse レーンの代表 11 URL への自己ヒットが navi の GA4 PV を
    60〜70% 水増ししていて、2026-09-02 の除外ガード修正が配信キャッシュの
    入れ替わりで 09-04 から反映され、水増しが消えたのが「急落」の正体
    だった (GSC が動じなかったのは元々クロールベースでこの水増しを
    数えていなかったため)。

    この解消を受けて #56 保留の残り 2 点 (entrance_ratio の丸め粒度 / 既知の
    真陽性確認) を post-fix の実データ (2026-09-13 週・2026-09-20 週、
    どちらも 09-04 以降のみで構成される完全にクリーンな週) で再検証したが、
    **どちらも解消しなかった**:
      - 粒度が保たれる PV>=20 (#18 提案の下限) でスイープしても、両週とも
        eligible=0 のまま。水増し除去後の実トラフィックはさらに小さく、
        粒度を保つ範囲では下げても検出は 1 件も増えない
      - 唯一の既知の真陽性 (brain#22, `/products/b0gc4mql8n/`, 観測週
        08-23〜08-30) が、まさに #57 が特定した Lighthouse 自己ヒット対象
        11 URL の 1 つだったと判明。entrance_ratio=100% はサイト側の内部
        リンク不足ではなく Lighthouse の直接ナビゲーションで説明がつき、
        較正の根拠として使えなくなった

    結論: min_pv は 50 のまま据え置き確定 (detect_orphan_pages.py のコメント
    参照)。A-5 の eligible=0 は A-4 と同じ「据え置くと決めた以上、構造的に
    鳴り続ける」型に確定したので、NO_ELIGIBILITY_WARNING に orphan_pages を
    追加した。次に見直すのは navi のトラフィックが回復し、PV>=20 のスイープで
    eligible が非ゼロの週が実際に出るようになってから。

  再較正した A-3 をいつ「妥当だった」と判断するか:
    次回見直しは jsonl に **4 行 (4 週分) 溜まった時点**
    (目安 2026-10-18 の週次 run 以降)。判断基準は「その 4 週の中で
    eligible==0 が 2 週連続する回数」— 0 回なら妥当、1 回でも起きたら
    「サイトの実寸がさらに縮んだ」として再スイープする。1〜2 週で判断
    すると #18 で踏んだ「週による正常な揺れ」を較正ミスと誤読する
    (このファイルの「なぜ2週連続」と同じ理由)。実測 (#56) では 8 週分の
    スナップショットで 0 件週が出ない値まで下げてあるので、トラフィックが
    さらに減らない限り 4 週内に鳴らないはずというのが検証可能な予測
    (A-5 は保留のままなので対象外)。

出力:
  - data/analytics/history/detector_eligibility.jsonl  1 run 1 行 (append)

副作用: 上記 1 ファイルの追記のみ。ネットワーク・Issue 操作は行わない。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import pathlib
import sys
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("check_detector_eligibility")

DEFAULT_HISTORY = "data/analytics/history/detector_eligibility.jsonl"

# (検出器名, 出力 JSON, 検出結果を持つキー)。
# 検出結果のキーが detected と candidates で割れているのは既存の出力形式に合わせたため。
DETECTORS: tuple[tuple[str, str, str], ...] = (
    ("low_ctr", "data/analytics/low_ctr_pages.json", "detected"),               # A-1
    ("opportunity", "data/analytics/opportunity_pages.json", "detected"),       # A-2
    ("cannibalization", "data/analytics/cannibalization.json", "detected"),     # A-3
    ("engagement_drop", "data/analytics/engagement_drop.json", "detected"),     # A-4
    ("orphan_pages", "data/analytics/orphan_pages.json", "detected"),           # A-5
    ("brand_suggest", "data/analytics/brand_taxonomy_suggestions.json",
     "candidates"),                                                             # A-6
)

# A-4 は amazon-navi-brain#18 で「min_pv を下げるとゲートが選別しなくなる」と
# 確認済みで、閾値を動かさないと決めた検出器 (detect_engagement_drop.py 参照)。
# A-5 は #56 で保留 → #57 (GA4/GSC 乖離の原因) 解消後に再検証したが、粒度を
# 保てる範囲では下げても検出が増えず、唯一の既知真陽性も無効化されたため
# 50 のまま据え置き確定 (detect_orphan_pages.py 参照)。
# どちらも動かさないと決めた以上 eligible==0 は構造的に鳴り続けるので、ここで
# 鳴らしても「閾値側の合図」として機能しない。恒久的に警告対象から外す
# (amazon-navi-brain#56)。collect() / history には引き続き記録する
# (観察はする。鳴らさないだけ)。
NO_ELIGIBILITY_WARNING: frozenset[str] = frozenset({"engagement_drop", "orphan_pages"})


def read_one(path: pathlib.Path, results_key: str) -> dict[str, Any] | None:
    """検出器の出力から eligible / detected を取り出す。読めなければ None。

    None は「この run では判定しない」を意味する。**欠落を 0 と混ぜない。**
    ファイルが無いのは検出器が skip / 失敗した場合で、それは #5941 の
    health check の担当。ここで 0 として記録すると、失敗が「母数が枯れた」に
    化けて処方を間違える。
    """
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("could not read %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        return None
    eligible = data.get("eligible")
    results = data.get(results_key)
    return {
        # eligible を出していない旧版の出力を 0 と読まない (同上の理由)。
        "eligible": eligible if isinstance(eligible, int) else None,
        "detected": len(results) if isinstance(results, list) else None,
    }


def collect(root: pathlib.Path) -> dict[str, Any]:
    return {
        name: read_one(root / path, key) for name, path, key in DETECTORS
    }


def load_history(path: pathlib.Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def find_starved(
    current: dict[str, Any], history: list[dict[str, Any]], date: str
) -> list[str]:
    """eligible == 0 が今回と前回で続いている検出器名を返す。

    前回 = jsonl の末尾行。ただし同じ date の行 (再実行) は前回として扱わない。
    """
    previous = None
    for row in reversed(history):
        if row.get("date") != date:
            previous = row.get("detectors") or {}
            break
    if previous is None:
        return []
    starved = []
    for name, cur in current.items():
        if name in NO_ELIGIBILITY_WARNING:
            continue
        prev = previous.get(name)
        if not isinstance(cur, dict) or not isinstance(prev, dict):
            continue
        if cur.get("eligible") == 0 and prev.get("eligible") == 0:
            starved.append(name)
    return starved


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", default=".")
    p.add_argument("--history", default=DEFAULT_HISTORY)
    p.add_argument("--date", help="既定は今日の UTC の日付 (YYYY-MM-DD)")
    p.add_argument("--force", action="store_true",
                   help="同じ date の行があっても追記する")
    args = p.parse_args()

    root = pathlib.Path(args.root)
    date = args.date or dt.datetime.now(dt.timezone.utc).date().isoformat()

    current = collect(root)
    hist_path = root / args.history
    history = load_history(hist_path)

    for name, val in current.items():
        if val is None:
            logger.info("%s: 出力が読めない (この run では判定しない)", name)
        else:
            logger.info("%s: eligible=%s detected=%s",
                        name, val["eligible"], val["detected"])

    starved = find_starved(current, history, date)
    for name in starved:
        # 閾値に届く母数が 2 週連続で 0。detected が 0 なのは正しいが、
        # 「該当が無い」ではなく「見る対象が存在しない」ので閾値側を疑う。
        print(f"::warning::検出器 {name} は eligible=0 が 2 回続いています。"
              f"閾値に届く母数が存在しないので、0 件を「該当なし」と読まないこと "
              f"(#5941 / amazon-navi-brain#18 / #56)")

    if any(row.get("date") == date for row in history) and not args.force:
        logger.info("history already has %s — skip append (--force で上書き追記)", date)
        return 0

    hist_path.parent.mkdir(parents=True, exist_ok=True)
    with hist_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"date": date, "detectors": current},
                            ensure_ascii=False) + "\n")
    logger.info("appended %s to %s", date, hist_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
