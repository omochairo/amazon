"""#4841 M1-c: 「素材投入前後の版が git 履歴に両方ある記事」のペアを探す。

指標の検証に使う「既知の差がある組」の第一候補。同じ ASIN で、記事ファイルが
別の日付プレフィックスで作り直されている (= リライトで新しいファイル名になり、
古いファイルは削除された) ケースを git 履歴から見つける。

git 呼び出しは薄いラッパーに閉じ込め、パース・判定ロジックは pure function に
分離してテストする。
"""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from typing import Any

ADD_LOG_COMMAND = [
    "git", "log", "--all", "--diff-filter=A", "--name-only", "--format=C %H %aI", "--", "data/articles/*.json",
]

_FILENAME_RE = re.compile(r"^data/articles/(\d{4}-\d{2}-\d{2})-([A-Z0-9]{10})\.json$")


def _parse_iso(ts: Any) -> datetime | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fetch_add_log(cwd: str | None = None) -> str:
    """`git log` で data/articles/*.json の追加イベントを1回で取得する (IO)。"""
    proc = subprocess.run(ADD_LOG_COMMAND, cwd=cwd, capture_output=True, text=True, check=True)
    return proc.stdout


def parse_add_events(log_text: str) -> dict[str, list[dict[str, Any]]]:
    """`fetch_add_log` の出力を ASIN ごとの追加イベント一覧にパースする (pure)。

    各イベント: {date_prefix, filename, sha, commit_at}
    """
    events_by_asin: dict[str, list[dict[str, Any]]] = {}
    sha = None
    commit_at = None
    for line in log_text.splitlines():
        if line.startswith("C "):
            _, sha, commit_at = line.split(" ", 2)
            continue
        line = line.strip()
        if not line:
            continue
        m = _FILENAME_RE.match(line)
        if not m:
            continue
        date_prefix, asin = m.group(1), m.group(2)
        events_by_asin.setdefault(asin, []).append({
            "date_prefix": date_prefix, "filename": line, "sha": sha, "commit_at": commit_at,
        })
    return events_by_asin


def find_rewrite_candidates(
    events_by_asin: dict[str, list[dict[str, Any]]], target_asins: set[str],
) -> dict[str, dict[str, Any]]:
    """対象 ASIN のうち、2つ以上の異なる日付プレフィックスでファイルが追加された

    もの (= リライトで作り直された候補) を返す。各値は最も古い版
    (``earliest``: date_prefix が最小のもの。同じ date_prefix に複数コミットが
    ある場合は commit_at が最も古いもの) と最新版のファイル名 (``latest_filename``、
    現在のディスク上のファイルと突き合わせる用) を持つ。
    """
    out: dict[str, dict[str, Any]] = {}
    for asin, events in events_by_asin.items():
        if asin not in target_asins:
            continue
        distinct_dates = sorted({e["date_prefix"] for e in events})
        if len(distinct_dates) < 2:
            continue
        earliest_date = distinct_dates[0]
        latest_date = distinct_dates[-1]
        earliest_events = sorted(
            (e for e in events if e["date_prefix"] == earliest_date), key=lambda e: e["commit_at"],
        )
        earliest = earliest_events[0]
        latest_events = [e for e in events if e["date_prefix"] == latest_date]
        out[asin] = {
            "earliest": earliest,
            "latest_filename": latest_events[0]["filename"],
            "distinct_version_count": len(distinct_dates),
        }
    return out


def evaluate_pair(
    asin: str, old_article: dict[str, Any] | None, new_article: dict[str, Any] | None, generated_at: str | None,
) -> dict[str, Any] | None:
    """old (素材投入前) / new (素材投入後) の版が本当にその条件を満たすかを判定する (pure)。

    audit_experience_usage.select_population / select_same_asin_control と同じ
    「article date と experience.json の generated_at」の比較を、1 ASIN の新旧
    両方に適用する。old.date < generated_at <= new.date を満たせば有効なペア。
    満たさなければ None (呼び出し元は候補から除外する)。
    """
    if not isinstance(old_article, dict) or not isinstance(new_article, dict):
        return None
    gen_dt = _parse_iso(generated_at)
    old_dt = _parse_iso(old_article.get("date"))
    new_dt = _parse_iso(new_article.get("date"))
    if gen_dt is None or old_dt is None or new_dt is None:
        return None
    if not (old_dt < gen_dt <= new_dt):
        return None
    return {
        "asin": asin,
        "old_date": old_article.get("date"),
        "new_date": new_article.get("date"),
        "generated_at": generated_at,
        "old_narrative": old_article.get("narrative") if isinstance(old_article.get("narrative"), dict) else {},
        "new_narrative": new_article.get("narrative") if isinstance(new_article.get("narrative"), dict) else {},
    }


def fetch_file_at_commit(sha: str, filename: str, cwd: str | None = None) -> dict[str, Any] | None:
    """`git show <sha>:<filename>` で過去の記事 JSON を読む (IO)。取得できなければ None。"""
    proc = subprocess.run(
        ["git", "show", f"{sha}:{filename}"], cwd=cwd, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None
