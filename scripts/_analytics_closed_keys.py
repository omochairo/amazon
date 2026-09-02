"""_analytics_closed_keys.py

`close_expired_analytics_issues.py` が「この run で閉じた Issue の dedup キー」を
opener 群へ渡すための受け渡し。

## なぜ必要か

17-analytics-report.yml は同じ run の中で **掃除 → 起票** の順に走る。狙いは、
期限切れを閉じたうえで、まだ検出され続けている URL をその run で最新の数値の
Issue に立て直すこと。

ところが opener 側の重複判定は GitHub の `search/issues` に `is:open` で問い合わせて
いる。**検索索引は非同期更新**で、close 直後に旧状態 (open) を返しうる。そうなると
opener は「まだ open だから起票しない」と判断し、その URL の立て直しが 1 週ずれる。
壊れはしないが、掃除を起票の前に置いた意味が消える。

索引のラグは非公開・可変なので、待ち時間を見積もるのではなく**依存そのものを外す**。
closer が閉じたキーをファイルに落とし、opener が検索結果から差し引く。索引が既に
追いついていれば差し引きは no-op なので、二重に安全。

## キーは検出器ごとに分ける

キー空間を混ぜてはいけない。同じ URL が A-1 と A-5 の両方で検出されることがあり、
A-5 の Issue だけが期限切れで閉じられた状況で URL を一括で差し引くと、**まだ open
な A-1 の Issue と重複した Issue を立ててしまう**。そのため
`{"a5-orphan:": ["<url>", ...]}` のように marker prefix ごとに分けて持ち、opener は
自分の `MARKER_PREFIX` のぶんだけを引く。

## 置き場所

環境変数 `ANALYTICS_CLOSED_KEYS` にパスが入っているときだけ読み書きする。未設定なら
何もしない = 従来どおり検索結果をそのまま使う (手元で opener を単体実行したときに
古いファイルを踏まないようにするため、既定パスは持たせない)。
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import re

logger = logging.getLogger("analytics_closed_keys")

ENV_VAR = "ANALYTICS_CLOSED_KEYS"

# opener 群が本文先頭に埋める重複防止マーカー。`a<数字>-<名前>:` の形。
# `analytics-expires:` は `a` の次が数字でないので誤ってマッチしない。
_DEDUP_MARKER_RE = re.compile(r"<!--\s*(a\d-[a-z0-9-]+:)\s*(.+?)\s*-->")


def extract_dedup_keys(body: str | None) -> dict[str, list[str]]:
    """Issue 本文から `{marker prefix: [キー]}` を取り出す。"""
    out: dict[str, list[str]] = {}
    for prefix, key in _DEDUP_MARKER_RE.findall(body or ""):
        key = key.strip()
        if not key:
            continue
        bucket = out.setdefault(prefix, [])
        if key not in bucket:
            bucket.append(key)
    return out


def path_from_env() -> pathlib.Path | None:
    raw = os.environ.get(ENV_VAR, "").strip()
    return pathlib.Path(raw) if raw else None


def write_closed_keys(keys: dict[str, list[str]], path: pathlib.Path | None = None) -> bool:
    """closer から呼ぶ。書けたら True。パス未設定なら何もせず False。"""
    path = path or path_from_env()
    if path is None:
        logger.info("%s unset — skipping the closed-key handoff", ENV_VAR)
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(keys, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("wrote closed keys to %s (%d detector(s))", path, len(keys))
    return True


def read_closed_keys(marker_prefix: str, path: pathlib.Path | None = None) -> set[str]:
    """opener から呼ぶ。自分の検出器のぶんだけ返す。

    読めない・無い・壊れている場合は空集合。**ここで落とさない** — 受け渡しは
    起票を速く正しくするための最適化であって、前提ではないため。落とすと掃除の
    副作用で本来の起票まで巻き添えになる。
    """
    path = path or path_from_env()
    if path is None or not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        logger.warning("closed-key handoff unreadable (%s): %s", path, exc)
        return set()
    if not isinstance(data, dict):
        logger.warning("closed-key handoff has unexpected shape: %s", path)
        return set()
    keys = data.get(marker_prefix) or []
    if not isinstance(keys, list):
        logger.warning("closed-key handoff bucket %r is not a list", marker_prefix)
        return set()
    return {k for k in keys if isinstance(k, str) and k}
