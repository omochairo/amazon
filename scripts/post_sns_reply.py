#!/usr/bin/env python3
"""承認済みの返信を SNS へ送信する (inbox の最終段)。

fetch_sns_replies → draft_sns_reply の後段。**自動では絶対に走らせない**。
起草した案をそのまま自動投稿すると、誤爆したときに取り返しがつかない
(相手のいるやり取りで、削除しても相手の通知には残る)。人が本文を確定させ、
workflow_dispatch か手元実行で 1 件ずつ送る。

送信経路:
  threads : notify_threads の create_container(reply_to_id=...) → publish を再利用
            (32s settle 待ち + transient retry の実績ある経路をそのまま使う)
  bluesky : com.atproto.repo.createRecord。reply には parent と root の
            strongRef (uri + cid) が要るので getPosts で cid を引く
  x       : **未配線**。POST /2/tweets は user-context 認証 (OAuth 1.0a か
            OAuth 2.0 PKCE) が要り、bearer だけでは投げられない。X を
            使う判断が出たときに配線する

使い方:
    python scripts/post_sns_reply.py --id "threads:1789..." --draft 1
    python scripts/post_sns_reply.py --id "bluesky:at://..." --body "本文" --dry-run

exit code:
    0 = 送信成功 (または --dry-run)
    1 = 送信失敗
    2 = 引数・状態が不正 (対象が無い / 既に返信済み / 本文が空)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

import sns_inbox_store as store  # noqa: E402

DEFAULT_BLUESKY_PDS = "https://bsky.social"
TIMEOUT = 30


class PostError(RuntimeError):
    pass


def _xrpc(url: str, *, headers: dict | None = None, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data, headers=headers or {}, method="POST" if data else "GET",
    )
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        detail = (e.read().decode("utf-8", "replace") if e.fp else "")[:300]
        raise PostError(f"HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise PostError(f"network error: {e.reason}") from e
    except ValueError as e:
        raise PostError("response is not JSON") from e


# --------------------------------------------------------------------------

def post_threads(rec: dict, body: str) -> str:
    import notify_threads  # 遅延 import — bluesky だけ使う環境で巻き込まない

    import fetch_sns_replies  # 同じ identity 解決を二重に書かない

    token = (os.environ.get("THREADS_ACCESS_TOKEN") or "").strip()
    if not token:
        raise PostError("THREADS_ACCESS_TOKEN 未設定")
    try:
        # THREADS_USER_ID は任意 (未設定なら /me から引く)。
        user_id, _ = fetch_sns_replies.resolve_threads_identity(token)
    except fetch_sns_replies.ChannelError as e:
        raise PostError(str(e)) from e

    container = notify_threads.create_container(
        user_id, token, body, reply_to_id=rec["native_id"],
    )
    creation_id = container.get("id")
    if not creation_id:
        raise PostError(f"container 作成失敗: {json.dumps(container, ensure_ascii=False)[:300]}")

    published = notify_threads.publish_container(user_id, token, str(creation_id))
    reply_id = published.get("id")
    if not reply_id:
        raise PostError(f"publish 失敗: {json.dumps(published, ensure_ascii=False)[:300]}")
    return str(reply_id)


def post_bluesky(rec: dict, body: str) -> str:
    ident = (
        os.environ.get("BLUESKY_IDENTIFIER") or os.environ.get("BLUESKY_HANDLE") or ""
    ).strip()
    pw = (os.environ.get("BLUESKY_APP_PASSWORD") or "").strip()
    if not ident or not pw:
        raise PostError("BLUESKY_IDENTIFIER / BLUESKY_APP_PASSWORD 未設定")

    pds = (os.environ.get("BLUESKY_PDS") or DEFAULT_BLUESKY_PDS).rstrip("/")
    session = _xrpc(
        f"{pds}/xrpc/com.atproto.server.createSession",
        payload={"identifier": ident, "password": pw},
    )
    jwt, did = session.get("accessJwt"), session.get("did")
    if not jwt or not did:
        raise PostError("createSession に accessJwt / did が無い")
    auth = {"Authorization": f"Bearer {jwt}"}

    target_uri = rec["native_id"]
    posts = _xrpc(
        f"{pds}/xrpc/app.bsky.feed.getPosts?"
        + urllib.parse.urlencode({"uris": target_uri}),
        headers=auth,
    )
    found = (posts.get("posts") or [])
    if not found or not isinstance(found[0], dict) or not found[0].get("cid"):
        raise PostError(f"返信先の cid を引けなかった: {target_uri}")

    parent_ref = {"uri": target_uri, "cid": found[0]["cid"]}
    root_ref = _bluesky_root_ref(found[0], parent_ref)

    created = _xrpc(
        f"{pds}/xrpc/com.atproto.repo.createRecord",
        headers=auth,
        payload={
            "repo": did,
            "collection": "app.bsky.feed.post",
            "record": {
                "$type": "app.bsky.feed.post",
                "text": body,
                "createdAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "reply": {"root": root_ref, "parent": parent_ref},
            },
        },
    )
    uri = created.get("uri")
    if not uri:
        raise PostError(f"createRecord に uri が無い: {json.dumps(created)[:200]}")
    return str(uri)


def _bluesky_root_ref(post: dict, parent_ref: dict) -> dict:
    """スレッドの根を返す。相手の投稿自体が根なら parent と同じものになる。

    root を parent で代用すると、3 手目以降の返信がスレッドから外れて
    相手に見えなくなる。
    """
    record = post.get("record") if isinstance(post.get("record"), dict) else {}
    reply = record.get("reply") if isinstance(record.get("reply"), dict) else {}
    root = reply.get("root")
    if isinstance(root, dict) and root.get("uri") and root.get("cid"):
        return {"uri": root["uri"], "cid": root["cid"]}
    return parent_ref


def post_x(rec: dict, body: str) -> str:
    raise PostError(
        "x への返信送信は未配線。POST /2/tweets は user-context 認証 (OAuth 1.0a / "
        "OAuth 2.0 PKCE) が要り、bearer だけでは投げられない",
    )


POSTERS = {"threads": post_threads, "bluesky": post_bluesky, "x": post_x}


# --------------------------------------------------------------------------

def resolve_body(rec: dict, args: argparse.Namespace) -> str:
    if args.body:
        return args.body.strip()
    drafts = rec.get("drafts") or []
    if args.draft is None:
        raise ValueError("--body か --draft のどちらかを指定する")
    idx = args.draft - 1
    if idx < 0 or idx >= len(drafts):
        raise ValueError(f"--draft {args.draft} は範囲外 (案は {len(drafts)} 件)")
    return str(drafts[idx].get("text") or "").strip()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--id", required=True, help="inbox id (例 threads:178...)")
    ap.add_argument("--body", default="", help="送信する本文 (最優先)")
    ap.add_argument("--draft", type=int, default=None, help="保存済み案の番号 (1 始まり)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--force", action="store_true",
        help="既に answered のものにも送る (通常は使わない)",
    )
    args = ap.parse_args(argv)

    directory = store.inbox_dir()
    rec = store.load_records(directory).get(args.id)
    if rec is None:
        print(f"inbox に {args.id} が無い", file=sys.stderr)
        return 2

    if rec.get("status") == store.STATUS_ANSWERED and not args.force:
        print(
            f"{args.id} は既に返信済み ({rec.get('answered_at')})。二度目を送らない。"
            "本当に送るなら --force",
            file=sys.stderr,
        )
        return 2

    try:
        body = resolve_body(rec, args)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    if not body:
        print("本文が空", file=sys.stderr)
        return 2

    print(f"channel : {rec['channel']}")
    print(f"相手    : @{rec.get('author') or '不明'}")
    print(f"本文    : {body}")

    if args.dry_run:
        print("(dry-run: 送信していない)")
        return 0

    poster = POSTERS.get(rec["channel"])
    if poster is None:
        print(f"未対応 channel: {rec['channel']}", file=sys.stderr)
        return 2

    try:
        reply_id = poster(rec, body)
    except PostError as e:
        print(f"送信失敗: {e}", file=sys.stderr)
        return 1

    store.update_record(
        args.id,
        {
            "status": store.STATUS_ANSWERED,
            "answered_at": store.utcnow(),
            "reply_native_id": reply_id,
            "answered_body": body,
        },
        directory,
    )
    print(f"送信完了: {reply_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
