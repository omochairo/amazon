"""check_history_freshness.py

#4789: 各計測レーンが**無言で止まっていないか**を検査する。

対象は 2 種類:
  - `LANES` … `data/analytics/history/<name>.jsonl` (1 ファイル = 1 レーン)
  - `DIR_LANES` … ディレクトリ配下にエンティティ単位で分かれる履歴。現状は価格観測の
    2 レーン (`data/price_watch/history/` / `data/price_history/`。#5015)。
    ここは `data/analytics/history/` の外にあるため走査対象から漏れており、
    **止まっても検出できない状態が続いていた**。

なぜ必要か:
  計測レーンは止まっても run が緑のままになる経路が 3 つある。
    1. secret 未設定の無言 no-op (`::notice::... not set — skipping` → 緑)。
       実測: 19-cwv-monitor の 2026-08-01 run は CRUX_API_KEY が空のまま緑終了し、
       データ 0 件だった。
    2. commit-back ステップの握りつぶし。`continue-on-error: true` が
       `git push` + `gh pr create` + auto-merge を含むステップ**全体**に付いており
       (13 workflow)、push や PR 作成が落ちるとその run のデータ更新が丸ごと消える。
    3. lane がそもそも起動しない。run が存在しないので run 一覧にも現れない
       (#4785 の Lighthouse 2026-08-06 欠測がこれ)。
  いずれも JSONL 側に「無い」という痕跡が残らないので、遡って気づくこともできない。

設計判断:
- **原因非依存**。各 lane に警告を足すのではなく「データが進んでいるか」だけを見る。
  #4469 (stale-pr-sweeper) と同じ思想で、これは logging ではなく monitoring の欠落。
  上の 3 経路はどれも「最終日が進まない」という同じ症状に落ちるので 1 つの網で拾える。
- **しきい値は cadence + 実測の遅延に余裕を足した絶対値**。導入時点 (2026-08-09) の
  実経過は ga4=1d / gsc=4d / gsc_wp=5d / lighthouse=1d / census=7d / crux=3d で、
  どれも閾値の下にある = **入れた日に鳴らない**こと。鳴りっぱなしのゲートは何も
  選別しないので、鳴ったら必ず異常であるように振る。
- **未知のファイルを pass に潰さない**。LANES にも UNMONITORED にも無い履歴ファイルは
  `unknown` として報告する。新レーンを足したときに黙って監視外へ落ちるのを防ぐ。
- 判定は「最終計測日」だけを見て、途中の歯抜けは見ない。歯抜けの検出は各 lane 側
  (#4785 の missing_dates) の役割で、こちらは lane が動いているかどうかの網。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import pathlib
import sys
from typing import Any, Dict, List, Optional, Sequence

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("check_history_freshness")

MARKER = "history-freshness-monitor"
LABELS = "tech-debt,todo,analytics"
DEFAULT_HISTORY_DIR = "data/analytics/history"


class Lane:
    """1 履歴ファイルの監視条件。

    max_age_days は「最終計測日が今日から何日離れていたら異常とみなすか」。
    cadence の間隔そのものではなく、**そのレーン固有の報告遅延を足した上限**である
    ことに注意 (GSC は API 側が 3 日遅れで確定するので日次でも 4 日前が正常)。
    """

    def __init__(self, filename: str, cadence: str, max_age_days: int,
                 lane: str, note: str = "") -> None:
        self.filename = filename
        self.cadence = cadence
        self.max_age_days = max_age_days
        self.lane = lane
        self.note = note


# 2026-08-09 実測の経過日数を括弧内に添える (閾値に余裕があることの記録)。
LANES: Sequence[Lane] = (
    Lane("ga4_totals.jsonl", "daily", 3, "18-analytics-daily.yml",
         "GA4 は前日 day-1 確定 (実測 1d)"),
    Lane("ga4_pages.jsonl", "daily", 3, "18-analytics-daily.yml",
         "GA4 は前日 day-1 確定 (実測 1d)"),
    Lane("gsc_totals.jsonl", "daily", 7, "18-analytics-daily.yml",
         "GSC は delay=3 で確定 (実測 4d)"),
    Lane("gsc_by_page.jsonl", "daily", 7, "18-analytics-daily.yml",
         "GSC は delay=3 で確定 (実測 4d)"),
    Lane("gsc_by_query.jsonl", "daily", 7, "18-analytics-daily.yml",
         "GSC は delay=3 で確定 (実測 4d)"),
    Lane("gsc_wp_totals.jsonl", "daily", 8, "18-analytics-daily.yml",
         "omcha.jp (WP) 側。実測 5d"),
    Lane("gsc_wp_by_page.jsonl", "daily", 8, "18-analytics-daily.yml",
         "omcha.jp (WP) 側。実測 5d"),
    Lane("gsc_wp_by_query.jsonl", "daily", 8, "18-analytics-daily.yml",
         "omcha.jp (WP) 側。実測 5d"),
    Lane("lighthouse_history.jsonl", "daily", 3,
         "amazon-home-ops/41-lighthouse-lane.yml",
         "self-hosted (K8)。#4785 で論理日に是正済 (実測 1d)"),
    Lane("gsc_index_census.jsonl", "weekly", 12, "22-gsc-index-census.yml",
         "毎週日曜 22:00 UTC (実測 7d)"),
    Lane("crux_history.jsonl", "monthly", 45, "19-cwv-monitor.yml",
         "CrUX は 28 日 rolling。月初のみ (実測 3d)"),
    Lane("quality_census.jsonl", "weekly", 12, "48-quality-census.yml",
         "毎週月曜 02:00 UTC (#4826 項目3。初回計測 2026-08-10)"),
)

class DirLane:
    """1 ディレクトリ配下に **エンティティ単位で分かれた** 履歴 jsonl を持つレーン。

    ``Lane`` が「1 ファイル = 1 レーン」なのに対し、価格観測は 1 ASIN 1 ファイルで
    数千本になるため、ディレクトリ配下の **最新観測日** をレーンの代表値にする。

    個別 ASIN は dedupe (同一価格が 6 日未満なら書かない) のせいで何週間も行が
    増えないことがあるが、ディレクトリ全体では毎日数百行が積まれる (実測:
    price_watch 357 行/日 / price_history 604 行/日) ので、代表値は日次で進む。
    """

    def __init__(self, path: str, cadence: str, max_age_days: int,
                 lane: str, note: str = "") -> None:
        self.path = path
        self.filename = path  # 表示・レンダリングは Lane と同じ扱いにする
        self.cadence = cadence
        self.max_age_days = max_age_days
        self.lane = lane
        self.note = note


# 価格観測の 2 レーン (#5015)。``data/analytics/history/`` の外にあるため、
# DEFAULT_HISTORY_DIR の走査では拾えず、これまで **止まっても検出されなかった**。
#
# しきい値のキャリブレーション (2026-08-12 に観測窓全域を replay):
#   price_watch   窓 2026-07-13〜08-11 (30日) の age 分布 = {0:29, 1:1}、最大 1
#   price_history 窓 2026-05-30〜08-12 (75日) の age 分布 = 0 が 58 日で、
#                 2〜13 は 2026-06-24〜07-06 の 13 日欠測 (GitHub 凍結期間) のみ
# 監視は 02:00 UTC で、price_watch の cron は 20:23 UTC (= 通常 age 1)、
# price_history は 00:00/09:00 UTC (= 通常 age 0〜1)。max_age=3 なら通常運転では
# 鳴らず、1 run の取りこぼしも吸収し、凍結相当の実障害では 3 日目に鳴る。
DIR_LANES: Sequence[DirLane] = (
    DirLane("data/price_watch/history", "daily", 3,
            "amazon-home-ops/40-price-watch.yml",
            "自宅 NAS runner・20:23 UTC 日次 (実測 age 1)"),
    DirLane("data/price_history", "daily", 3,
            "01-fetch-products.yml",
            "write_per_asin_snapshot の hook。1日2run (実測 age 0〜1)"),
)

# 監視対象外。**理由つきで明示する** (未知として鳴らさないための逃げ道ではなく、
# 「見ないと決めた」ことを記録に残すため)。
UNMONITORED: Dict[str, str] = {
    "uniqueness_audit_history.jsonl":
        "日付フィールドを持たない集計スナップショット (#3300)。cadence 判定の対象外",
}


def _date_of(row: Dict[str, Any]) -> Optional[dt.date]:
    # 価格レーンは日付を ``ts`` (ISO datetime) に持つ。``date`` を優先し、
    # 無ければ ``ts`` を見る (どちらも先頭 10 文字が YYYY-MM-DD)。
    value = row.get("date")
    if not isinstance(value, str) or len(value) < 10:
        value = row.get("ts")
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return dt.date.fromisoformat(value[:10])
    except ValueError:
        return None


def last_date(path: pathlib.Path) -> Optional[dt.date]:
    """JSONL の最終計測日を返す (読めない / 日付が 1 つも無ければ None)。

    末尾行が最新とは限らない (#4772 の後勝ち append で行順が入れ替わる) ため
    全行を走査して max を採る。壊れた行は黙って飛ばす — ここは freshness の網で
    あってスキーマ検証ではない。
    """
    if not path.exists():
        return None
    found: List[dt.date] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            d = _date_of(row)
            if d is not None:
                found.append(d)
    return max(found) if found else None


def last_date_in_dir(root: pathlib.Path) -> Optional[dt.date]:
    """ディレクトリ配下の ``*.jsonl`` 全体で最新の観測日を返す。

    1 本でも読めれば代表値になるので、壊れたファイルは飛ばす (freshness の網で
    あってスキーマ検証ではない)。
    """
    if not root.is_dir():
        return None
    found: Optional[dt.date] = None
    for path in root.glob("*.jsonl"):
        d = last_date(path)
        if d is not None and (found is None or d > found):
            found = d
    return found


def check_dirs(repo_root: pathlib.Path, today: dt.date,
               dir_lanes: Sequence[DirLane] = DIR_LANES) -> List[Dict[str, Any]]:
    """ディレクトリ単位レーンの状態を返す。行の形は ``check`` と揃える。"""
    rows: List[Dict[str, Any]] = []
    for lane in dir_lanes:
        root = repo_root / lane.path
        if not root.is_dir():
            rows.append({"filename": lane.filename, "status": "missing",
                         "last": None, "age_days": None, "lane": lane})
            continue
        last = last_date_in_dir(root)
        if last is None:
            # ディレクトリはあるが日付を 1 つも読めない (空 / 全部壊れている)。
            rows.append({"filename": lane.filename, "status": "unknown",
                         "last": None, "age_days": None, "lane": lane})
            continue
        age = (today - last).days
        rows.append({"filename": lane.filename,
                     "status": "stale" if age > lane.max_age_days else "ok",
                     "last": last.isoformat(), "age_days": age, "lane": lane})
    return rows


def check(history_dir: pathlib.Path, today: dt.date,
          lanes: Sequence[Lane] = LANES) -> List[Dict[str, Any]]:
    """各レーンの状態を返す。status は ok / stale / missing / unknown。"""
    rows: List[Dict[str, Any]] = []
    for lane in lanes:
        path = history_dir / lane.filename
        if not path.exists():
            rows.append({"filename": lane.filename, "status": "missing",
                         "last": None, "age_days": None, "lane": lane})
            continue
        last = last_date(path)
        if last is None:
            # ファイルはあるが日付が 1 つも読めない = 中身が空か壊れている。
            # 「日付が無いから判定不能」を ok に潰さない。
            rows.append({"filename": lane.filename, "status": "unknown",
                         "last": None, "age_days": None, "lane": lane})
            continue
        age = (today - last).days
        status = "stale" if age > lane.max_age_days else "ok"
        rows.append({"filename": lane.filename, "status": status,
                     "last": last.isoformat(), "age_days": age, "lane": lane})
    return rows


def unregistered_files(history_dir: pathlib.Path,
                       lanes: Sequence[Lane] = LANES) -> List[str]:
    """LANES にも UNMONITORED にも無い *.jsonl を返す (監視の取りこぼし検出)。"""
    known = {l.filename for l in lanes} | set(UNMONITORED)
    if not history_dir.is_dir():
        return []
    return sorted(p.name for p in history_dir.glob("*.jsonl") if p.name not in known)


def problems(rows: Sequence[Dict[str, Any]], unregistered: Sequence[str]) -> List[str]:
    """報告すべき事象を 1 行ずつ返す (空なら全部健全)。"""
    out = [r["filename"] for r in rows if r["status"] != "ok"]
    out += list(unregistered)
    return out


def render_body(rows: Sequence[Dict[str, Any]], unregistered: Sequence[str],
                today: dt.date) -> str:
    bad = [r for r in rows if r["status"] != "ok"]
    # 見出しの件数は「止まっているレーン + 監視表に無いファイル」の合計にする。
    # stale が 0 でも未登録ファイルだけで起票されることがあり、そこで "0 件" と
    # 書くと本文と矛盾する。
    total = len(bad) + len(unregistered)
    headline = []
    if bad:
        headline.append(f"止まっているレーン **{len(bad)} 件**")
    if unregistered:
        headline.append(f"監視表に無い履歴ファイル **{len(unregistered)} 件**")
    parts = [
        f"<!-- {MARKER} -->",
        "",
        f"計測レーンに{'、'.join(headline)}があります "
        f"(計 {total} 件・{today.isoformat()} 時点)。",
        "",
        "計測レーンは止まっても run が緑のままになる経路が 3 つあり "
        "(secret 未設定の無言 no-op / commit-back ステップの `continue-on-error` / "
        "lane がそもそも起動しない)、JSONL 側にも痕跡が残りません。"
        "この issue はデータが進んでいるかだけを見る原因非依存の網です (#4789)。",
        "",
    ]
    if bad:
        parts += [
            "## 止まっているレーン",
            "",
            "| ファイル | 状態 | 最終計測日 | 経過 | 上限 | 供給元 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for r in bad:
            lane: Lane = r["lane"]
            age = "-" if r["age_days"] is None else f"{r['age_days']}d"
            parts.append("| `{}` | {} | {} | {} | {}d | `{}` |".format(
                r["filename"], r["status"], r["last"] or "-", age,
                lane.max_age_days, lane.lane))
        parts.append("")

    if unregistered:
        parts += [
            "## 監視表に無い履歴ファイル",
            "",
            "`LANES` にも `UNMONITORED` にも登録されていないため、止まっても検出できません。"
            "`scripts/check_history_freshness.py` に cadence を足してください "
            "(ディレクトリ配下にエンティティ単位で分かれる履歴なら `DIR_LANES` 側)。",
            "",
        ]
        parts += [f"- `{name}`" for name in unregistered]
        parts.append("")

    ok = [r for r in rows if r["status"] == "ok"]
    if ok:
        parts += ["<details><summary>健全なレーン ({} 件)</summary>".format(len(ok)), ""]
        parts += ["| ファイル | 最終計測日 | 経過 | 上限 |", "| --- | --- | --- | --- |"]
        for r in ok:
            parts.append("| `{}` | {} | {}d | {}d |".format(
                r["filename"], r["last"], r["age_days"], r["lane"].max_age_days))
        parts += ["", "</details>", ""]

    parts += [
        "## 見かた",
        "",
        "- `stale` — ファイルはあるが最終計測日が上限を超えた。供給元 workflow の直近 run を "
        "**annotations まで**見ること。`continue-on-error` のステップは REST の "
        "`steps[].conclusion` が `success` を返すので、run 一覧では失敗が見えない",
        "- `missing` — ファイルごと無い。初回投入前か、消えた",
        "- `unknown` — ファイルはあるが日付を 1 つも読めない (空 / 壊れている)",
        "",
        f"- マーカー `<!-- {MARKER} -->` で同一 open Issue を特定し、body を毎回更新します",
        "- 全レーンが健全になったら自動 close します",
        "",
        "Refs #4789, #4785, #4469, #1357",
    ]
    return "\n".join(parts)


# --- GitHub 側 (#4469 open_stale_pr_issue.py と同じ作法) --------------------

def _gh(args: List[str]) -> str:
    import subprocess
    res = subprocess.run(["gh", *args], check=True, capture_output=True, text=True)
    return res.stdout


def get_open_issue(repo: str) -> Optional[int]:
    query = f'repo:{repo} is:issue is:open in:body "{MARKER}"'
    out = _gh(["api", "-X", "GET", "search/issues", "-f", f"q={query}", "-f", "per_page=10"])
    items = json.loads(out).get("items", [])
    return items[0]["number"] if items else None


def create_issue(repo: str, title: str, body: str) -> str:
    return _gh(["issue", "create", "-R", repo, "--title", title,
                "--label", LABELS, "--body", body]).strip()


def update_issue(repo: str, number: int, title: str, body: str) -> str:
    return _gh(["issue", "edit", str(number), "-R", repo,
                "--title", title, "--body", body]).strip()


def close_issue(repo: str, number: int) -> None:
    _gh(["issue", "close", str(number), "-R", repo,
         "--comment", "全計測レーンが想定 cadence 内に戻ったため自動 close します (#4789)。"])


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", default=os.environ.get("REPO"))
    p.add_argument("--history-dir", type=pathlib.Path,
                   default=pathlib.Path(DEFAULT_HISTORY_DIR))
    p.add_argument("--repo-root", type=pathlib.Path, default=pathlib.Path("."),
                   help="DIR_LANES (価格観測レーン等) の解決基準。既定はカレント")
    p.add_argument("--today", default=None,
                   help="判定基準日 (既定: UTC 今日)。replay 検証用")
    p.add_argument("--dry-run", action="store_true",
                   help="issue を触らず判定結果と body だけ出す")
    args = p.parse_args(argv)

    today = (dt.date.fromisoformat(args.today) if args.today
             else dt.datetime.now(dt.timezone.utc).date())
    rows = check(args.history_dir, today) + check_dirs(args.repo_root, today)
    unregistered = unregistered_files(args.history_dir)

    for r in rows:
        logger.info("%-32s %-8s last=%s age=%s max=%dd",
                    r["filename"], r["status"], r["last"],
                    "-" if r["age_days"] is None else f"{r['age_days']}d",
                    r["lane"].max_age_days)
    for name in unregistered:
        logger.warning("%-32s unregistered (not in LANES/UNMONITORED)", name)

    bad = problems(rows, unregistered)
    if args.dry_run:
        if bad:
            print(render_body(rows, unregistered, today))
        else:
            print("all lanes fresh")
        return 0

    if not args.repo:
        logger.error("missing --repo or $REPO")
        return 2

    number = get_open_issue(args.repo)
    if not bad:
        if number is not None:
            close_issue(args.repo, number)
            logger.info("closed #%s (all lanes fresh)", number)
        else:
            logger.info("all lanes fresh; nothing to do")
        return 0

    title = "[計測] 履歴レーンが {} 件停止しています ({})".format(
        len(bad), today.isoformat())
    body = render_body(rows, unregistered, today)
    if number is None:
        logger.info("created %s", create_issue(args.repo, title, body))
    else:
        logger.info("updated %s", update_issue(args.repo, number, title, body))
    return 0


if __name__ == "__main__":
    sys.exit(main())
