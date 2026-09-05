#!/usr/bin/env python3
"""SNS の「返信を読む」経路が実在するかを実測する probe (Issue: SNS reply inbox)。

投稿レーン (notify_buffer / notify_threads / notify_bluesky) は既にあるが、
**届いた返信を読む**経路は一本も無い。着手前に、各チャネルで返信取得が
実際に可能かを 1 回の dispatch で確定させるための read-only probe。

推測で設計に入らないための道具であって、恒久レーンではない。

チャネルごとに見るもの:

  buffer   : GraphQL schema introspection。Query root と全 type 名から
             reply / comment / mention / engage / inbox / conversation を
             含む名前を抽出する。Buffer の GraphQL は公開ドキュメントに
             無いフィールドを持ちうるので、「docs に無い」ではなく
             「schema に無い」を根拠にするための確認。
  threads  : GET /me/threads → 先頭 1 件の GET /{id}/replies。
             現行 THREADS_ACCESS_TOKEN に threads_read_replies 相当の
             scope が付いているかを HTTP status で判定する。
  bluesky  : createSession → app.bsky.notification.listNotifications。
             reason (reply / mention / quote / like / follow) の**件数**のみ数える。

🚨 出力の秘匿性 — このリポジトリは public で、Actions のログも public。
   本文・ユーザー名・URL・トークンは **一切出さない**。出すのは
   「フィールド名」「HTTP status」「件数」だけ。API の生レスポンスを
   そのまま print しないこと (CLAUDE.md「貼り付けが事故る」)。

使い方:
    python scripts/probe_sns_inbox.py --channel all
    python scripts/probe_sns_inbox.py --channel buffer

env (無いチャネルは skip 報告して続行する。probe が落ちて他が見えなくなる方が損):
    BUFFER_ACCESS_TOKEN
    THREADS_ACCESS_TOKEN / THREADS_USER_ID
    BLUESKY_IDENTIFIER / BLUESKY_APP_PASSWORD / BLUESKY_PDS

exit code:
    0 = probe が最後まで走った (「取れない」と分かるのも成功)
    1 = probe 自体が想定外で落ちた
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001 — 3.6 以前や非 TTY での失敗は無視してよい
    pass

BUFFER_GRAPHQL_URL = "https://api.buffer.com/"
THREADS_BASE = "https://graph.threads.net/v1.0"
DEFAULT_BLUESKY_PDS = "https://bsky.social"
TIMEOUT = 30

# 「返信を読む」能力を示唆する語。schema 名の部分一致で拾う。
INBOX_HINT = re.compile(
    r"repl|comment|mention|engage|inbox|conversation|notification|activity",
    re.IGNORECASE,
)


def _post_json(url: str, payload: dict, headers: dict) -> tuple[int, dict | None, str]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    return _send(req)


def _get_json(url: str, headers: dict | None = None) -> tuple[int, dict | None, str]:
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    return _send(req)


def _send(req: urllib.request.Request) -> tuple[int, dict | None, str]:
    """(status, parsed_json_or_None, short_error_kind) を返す。

    本文はここから外へ出さない。エラーは「種別」に丸めて返す。
    """
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace") if e.fp else ""
        status = e.code
    except urllib.error.URLError as e:
        return 0, None, f"network:{type(e.reason).__name__}"
    except Exception as e:  # noqa: BLE001
        return 0, None, f"exception:{type(e).__name__}"

    try:
        return status, json.loads(raw), ""
    except ValueError:
        return status, None, "non-json-response"


# --------------------------------------------------------------------------
# Buffer
# --------------------------------------------------------------------------

INTROSPECT_QUERY = """
query ProbeSchema {
  __schema {
    queryType { fields { name } }
    types { name kind }
  }
}
""".strip()


def probe_buffer() -> dict:
    token = (os.environ.get("BUFFER_ACCESS_TOKEN") or "").strip()
    if not token:
        return {"channel": "buffer", "status": "skip", "reason": "BUFFER_ACCESS_TOKEN 未設定"}

    status, payload, err = _post_json(
        BUFFER_GRAPHQL_URL,
        {"query": INTROSPECT_QUERY},
        {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    if err:
        return {"channel": "buffer", "status": "error", "http": status, "reason": err}
    if not isinstance(payload, dict):
        return {"channel": "buffer", "status": "error", "http": status, "reason": "payload not dict"}

    if payload.get("errors"):
        # introspection 自体を止めている可能性 (Apollo の本番設定でよくある)。
        # message は Buffer 側の定型文なので kind だけに丸める。
        return {
            "channel": "buffer",
            "status": "introspection-blocked",
            "http": status,
            "error_count": len(payload["errors"]),
        }

    schema = ((payload.get("data") or {}).get("__schema") or {})
    query_fields = [
        f.get("name") for f in ((schema.get("queryType") or {}).get("fields") or [])
        if isinstance(f, dict) and isinstance(f.get("name"), str)
    ]
    type_names = [
        t.get("name") for t in (schema.get("types") or [])
        if isinstance(t, dict) and isinstance(t.get("name"), str)
        and not t["name"].startswith("__")
    ]

    return {
        "channel": "buffer",
        "status": "ok",
        "http": status,
        "query_field_count": len(query_fields),
        "query_fields": sorted(query_fields),
        "type_count": len(type_names),
        "inbox_hint_types": sorted(n for n in type_names if INBOX_HINT.search(n)),
        "inbox_hint_query_fields": sorted(n for n in query_fields if INBOX_HINT.search(n)),
    }


# --------------------------------------------------------------------------
# Threads
# --------------------------------------------------------------------------

def probe_threads() -> dict:
    token = (os.environ.get("THREADS_ACCESS_TOKEN") or "").strip()
    user_id = (os.environ.get("THREADS_USER_ID") or "").strip()
    if not token or not user_id:
        return {
            "channel": "threads",
            "status": "skip",
            "reason": "THREADS_ACCESS_TOKEN / THREADS_USER_ID 未設定",
        }

    out: dict = {"channel": "threads"}

    q = urllib.parse.urlencode({"fields": "id,timestamp", "limit": "5", "access_token": token})
    status, payload, err = _get_json(f"{THREADS_BASE}/{user_id}/threads?{q}")
    out["own_threads_http"] = status
    if err or not isinstance(payload, dict):
        out["status"] = "error"
        out["reason"] = err or "payload not dict"
        return out
    if status != 200:
        out["status"] = "error"
        out["reason"] = _meta_error_kind(payload)
        return out

    items = [i for i in (payload.get("data") or []) if isinstance(i, dict) and i.get("id")]
    out["own_thread_count"] = len(items)
    if not items:
        out["status"] = "ok-but-empty"
        out["reason"] = "自投稿が 0 件 — replies を試せない"
        return out

    media_id = str(items[0]["id"])
    q2 = urllib.parse.urlencode(
        {"fields": "id,timestamp,is_reply", "limit": "5", "access_token": token},
    )
    r_status, r_payload, r_err = _get_json(f"{THREADS_BASE}/{media_id}/replies?{q2}")
    out["replies_http"] = r_status
    if r_err or not isinstance(r_payload, dict):
        out["status"] = "error"
        out["reason"] = r_err or "payload not dict"
        return out
    if r_status != 200:
        # ここが本命の判定点: scope 不足なら 400/403 が返る。
        out["status"] = "replies-denied"
        out["reason"] = _meta_error_kind(r_payload)
        return out

    out["status"] = "replies-ok"
    out["reply_count_on_latest"] = len(r_payload.get("data") or [])
    return out


def _meta_error_kind(payload: dict) -> str:
    """Meta のエラー JSON から「種別」だけ取り出す (本文は出さない)。"""
    err = payload.get("error")
    if not isinstance(err, dict):
        return "unknown"
    return "type={} code={} subcode={}".format(
        err.get("type"), err.get("code"), err.get("error_subcode"),
    )


# --------------------------------------------------------------------------
# Bluesky
# --------------------------------------------------------------------------

def probe_bluesky() -> dict:
    ident = (
        os.environ.get("BLUESKY_IDENTIFIER") or os.environ.get("BLUESKY_HANDLE") or ""
    ).strip()
    pw = (os.environ.get("BLUESKY_APP_PASSWORD") or "").strip()
    if not ident or not pw:
        return {
            "channel": "bluesky",
            "status": "skip",
            "reason": "BLUESKY_IDENTIFIER / BLUESKY_APP_PASSWORD 未設定",
        }

    pds = (os.environ.get("BLUESKY_PDS") or DEFAULT_BLUESKY_PDS).rstrip("/")
    status, payload, err = _post_json(
        f"{pds}/xrpc/com.atproto.server.createSession",
        {"identifier": ident, "password": pw},
        {"Content-Type": "application/json"},
    )
    if err or not isinstance(payload, dict) or status != 200:
        return {
            "channel": "bluesky",
            "status": "auth-error",
            "http": status,
            "reason": err or "createSession failed",
        }

    jwt = payload.get("accessJwt")
    if not isinstance(jwt, str) or not jwt:
        return {"channel": "bluesky", "status": "auth-error", "reason": "no accessJwt"}

    n_status, n_payload, n_err = _get_json(
        f"{pds}/xrpc/app.bsky.notification.listNotifications?limit=50",
        {"Authorization": f"Bearer {jwt}"},
    )
    if n_err or not isinstance(n_payload, dict):
        return {
            "channel": "bluesky",
            "status": "error",
            "http": n_status,
            "reason": n_err or "payload not dict",
        }
    if n_status != 200:
        return {"channel": "bluesky", "status": "error", "http": n_status}

    reasons: dict[str, int] = {}
    unread = 0
    for n in n_payload.get("notifications") or []:
        if not isinstance(n, dict):
            continue
        reasons[str(n.get("reason"))] = reasons.get(str(n.get("reason")), 0) + 1
        if n.get("isRead") is False:
            unread += 1

    return {
        "channel": "bluesky",
        "status": "ok",
        "http": n_status,
        "notification_count": sum(reasons.values()),
        "unread_count": unread,
        "by_reason": dict(sorted(reasons.items())),
    }


PROBES = {"buffer": probe_buffer, "threads": probe_threads, "bluesky": probe_bluesky}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--channel", default="all", choices=["all", *PROBES])
    args = ap.parse_args(argv)

    names = list(PROBES) if args.channel == "all" else [args.channel]
    results = [PROBES[n]() for n in names]

    print(json.dumps({"probe": "sns_inbox", "results": results}, ensure_ascii=False, indent=2))

    print("\n--- 判定 ---")
    for r in results:
        print(f"{r['channel']:8s} {r.get('status')}", end="")
        if r.get("reason"):
            print(f"  ({r['reason']})", end="")
        print()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001 — probe 自体の失敗は 1 で返す
        print(f"probe failed: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
