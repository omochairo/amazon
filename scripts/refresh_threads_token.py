#!/usr/bin/env python3
"""Refresh THREADS_ACCESS_TOKEN via Meta Graph `th_refresh_token`.

Issue #1486 follow-up (session 87).

long-lived token (60 日寿命) を期限前に renew する。`th_refresh_token` は
**24 時間以上経過した long-lived token** が対象 ([[feedback-omochairo-threads-token-diagnostic]])。

Usage:
    THREADS_ACCESS_TOKEN=... python scripts/refresh_threads_token.py

Output: 新 token を stdout の最終行に出力 (workflow 側で capture して
`gh secret set THREADS_ACCESS_TOKEN` に渡す)。expires_in / 発行時刻は
stderr に diagnostic として出力。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request

ENDPOINT = "https://graph.threads.net/refresh_access_token"


def refresh(token: str) -> dict:
    qs = urllib.parse.urlencode(
        {"grant_type": "th_refresh_token", "access_token": token}
    )
    req = urllib.request.Request(f"{ENDPOINT}?{qs}", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"[error] HTTP {e.code}: {body}", file=sys.stderr)
        raise SystemExit(1)
    data = json.loads(body)
    if "access_token" not in data:
        print(f"[error] response missing access_token: {body}", file=sys.stderr)
        raise SystemExit(1)
    return data


def main() -> int:
    token = os.environ.get("THREADS_ACCESS_TOKEN", "").strip()
    if not token:
        print("[error] THREADS_ACCESS_TOKEN env not set", file=sys.stderr)
        return 1
    data = refresh(token)
    expires_in = int(data.get("expires_in", 0))
    days = expires_in // 86400
    print(
        f"[ok] refreshed; expires_in={expires_in}s (~{days}d), "
        f"token_type={data.get('token_type', '?')}",
        file=sys.stderr,
    )
    # 最終行に新 token のみ出力 (workflow capture 用)
    print(data["access_token"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
