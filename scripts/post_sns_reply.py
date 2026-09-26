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
    2 = 引数・状態が不正 (対象が無い / 既に返信済み / 返さないと判断済み /
        前回の送信が完了していない / 本文が空)。返信済み・判断済み・未完了は
        --force で越えられる

二重送信の防止 (#8285):
    送信の直前に send_started_at を書き、記録まで済んだら消す。印が残って
    いる = 前回が途中で終わった (送れたかは分からない) ので、次の実行は止まる。
    読み直し → ガード → 印を書く → 送る → 記録、は id ごとのロックファイルの中で
    やるので、同じ id を同時に 2 本走らせても (--force 付きでも) 片方は止まる (#8323)。
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


class PostNotSent(PostError):
    """相手側に何も出ていないことが確かな失敗 (最後の送信呼び出しより前で落ちた)。

    これ以外の PostError は「届いたか分からない」扱い — タイムアウトや 5xx は
    相手側で投稿が済んでいることがある。
    """


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
    except OSError as e:
        # 応答の読み取り中のタイムアウト・切断 (socket.timeout 等) は URLError に包まれない
        raise PostError(f"network error: {e}") from e
    except ValueError as e:
        raise PostError("response is not JSON") from e


# --------------------------------------------------------------------------

def post_threads(rec: dict, body: str) -> str:
    import notify_threads  # 遅延 import — bluesky だけ使う環境で巻き込まない

    import fetch_sns_replies  # 同じ identity 解決を二重に書かない

    token = (os.environ.get("THREADS_ACCESS_TOKEN") or "").strip()
    if not token:
        raise PostNotSent("THREADS_ACCESS_TOKEN 未設定")
    try:
        # THREADS_USER_ID は任意 (未設定なら /me から引く)。
        user_id, _ = fetch_sns_replies.resolve_threads_identity(token)
    except fetch_sns_replies.ChannelError as e:
        raise PostNotSent(str(e)) from e

    # container は publish するまで相手に見えない。ここまでの失敗は PostNotSent。
    # notify_threads._post は HTTPError しか捕まえないので、ネットワーク例外も
    # ここで PostError 系に包む (素通りすると main の except を抜けて落ちる)
    try:
        container = notify_threads.create_container(
            user_id, token, body, reply_to_id=rec["native_id"],
        )
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise PostNotSent(f"container 作成失敗: {e}") from e
    creation_id = container.get("id")
    if not creation_id:
        raise PostNotSent(f"container 作成失敗: {json.dumps(container, ensure_ascii=False)[:300]}")

    try:
        published = notify_threads.publish_container(user_id, token, str(creation_id))
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise PostError(f"publish で例外 (届いたか不明): {e}") from e
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
        raise PostNotSent("BLUESKY_IDENTIFIER / BLUESKY_APP_PASSWORD 未設定")

    pds = (os.environ.get("BLUESKY_PDS") or DEFAULT_BLUESKY_PDS).rstrip("/")
    # createRecord より前は何も書かない。ここまでの失敗は PostNotSent
    try:
        session = _xrpc(
            f"{pds}/xrpc/com.atproto.server.createSession",
            payload={"identifier": ident, "password": pw},
        )
    except PostError as e:
        raise PostNotSent(str(e)) from e
    jwt, did = session.get("accessJwt"), session.get("did")
    if not jwt or not did:
        raise PostNotSent("createSession に accessJwt / did が無い")
    auth = {"Authorization": f"Bearer {jwt}"}

    target_uri = rec["native_id"]
    try:
        posts = _xrpc(
            f"{pds}/xrpc/app.bsky.feed.getPosts?"
            + urllib.parse.urlencode({"uris": target_uri}),
            headers=auth,
        )
    except PostError as e:
        raise PostNotSent(str(e)) from e
    found = (posts.get("posts") or [])
    if not found or not isinstance(found[0], dict) or not found[0].get("cid"):
        raise PostNotSent(f"返信先の cid を引けなかった: {target_uri}")

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
    raise PostNotSent(
        "x への返信送信は未配線。POST /2/tweets は user-context 認証 (OAuth 1.0a / "
        "OAuth 2.0 PKCE) が要り、bearer だけでは投げられない",
    )


POSTERS = {"threads": post_threads, "bluesky": post_bluesky, "x": post_x}
# POSTERS にはあるが送れない channel。dry-run の段階で弾く
UNWIRED_CHANNELS = {"x"}


# --------------------------------------------------------------------------

def resolve_body(rec: dict, args: argparse.Namespace) -> str:
    if args.body:
        return args.body.strip()
    if args.draft is None:
        raise ValueError("--body か --draft のどちらかを指定する")
    # 案番号で引く (並び順ではない)。作り直しで消えた番号は別の案に振り直さ
    # れないので、issue に残る古い案の番号を打つとここで止まる (#8323)
    numbered = store.numbered_drafts(rec)
    for no, draft in numbered:
        if no == args.draft:
            return str(draft.get("text") or "").strip()
    if 0 < args.draft <= store.draft_seq(rec):
        live = ", ".join(str(no) for no, _ in numbered) or "なし"
        raise ValueError(
            f"案 {args.draft} は作り直しで破棄済み (今ある案: {live})。"
            "issue の最新のコメントを見て番号を選び直す",
        )
    raise ValueError(f"--draft {args.draft} は範囲外 (案は {len(numbered)} 件)")


def refuse_reason(rec: dict, force: bool) -> str:
    """送ってはいけない状態なら理由を返す (送ってよければ "")。"""
    if force:
        return ""
    rid = rec.get("id")
    if rec.get("status") == store.STATUS_ANSWERED:
        return (
            f"{rid} は既に返信済み ({rec.get('answered_at')})。二度目を送らない。"
            "本当に送るなら --force"
        )
    # ignored は「返さない」と決めたもの (起草側の判断か、人が issue を close した)。
    # 古いコマンド履歴から id を打ち直しただけで送れてしまわないよう、answered と
    # 同じく --force を要求する。人が考え直して送る余地は --force で残す (#8285)
    if rec.get("status") == store.STATUS_IGNORED:
        return (
            f"{rid} は返信しないと判断済み ({rec.get('ignore_reason') or '理由不明'})。"
            "考え直して送るなら --force"
        )
    # 前回の送信が「送信開始」を書いたまま終わっていない。送れたのに記録する前に
    # 落ちたのか、送る前に落ちたのかは inbox からは区別できないので、相手側を
    # 目で確かめてから --force で送る (#8285)
    if rec.get("send_started_at"):
        return (
            f"{rid} は前回の送信が完了していない ({rec['send_started_at']} 開始)。"
            "相手の投稿に返信が付いていないことを確かめてから --force"
        )
    return ""


def _send_state(rec: dict) -> tuple:
    return (
        rec.get("status"), rec.get("send_started_at") or "", rec.get("answered_at") or "",
    )


def recheck_under_lock(
    args: argparse.Namespace, body: str, directory, seen: dict,
) -> str:
    """ロックを取ったあとに読み直し、送ってよいかをもう一度確かめる。

    最初の読み込みからロックを取るまでの間に、同じ id の別の実行が送り終えて
    いたり (同時実行。#8323)、案が作り直されていたりしたら止める。止めるときは
    理由を返す (送ってよければ "")。

    seen は最初に読んだときのレコード。--force はガードを越えるが、読んだあとに
    送信の状態が変わっていたら (= 別の実行が送信を始めた・終えた) --force でも
    止める。--force は「前回の結果を人が確かめた」という意味で、いま走っている
    別の実行まで越える意味ではない。
    """
    rec = store.load_records(directory).get(args.id)
    if rec is None:
        return f"inbox に {args.id} が無い"
    if _send_state(rec) != _send_state(seen):
        return (
            f"{args.id} は読んだあとに別の実行が送信を始めたか終えた "
            f"(status={rec.get('status')} "
            f"send_started_at={rec.get('send_started_at') or '-'})。"
            "二重送信を避けて止めた"
        )
    reason = refuse_reason(rec, args.force)
    if reason:
        return reason
    try:
        again = resolve_body(rec, args)
    except ValueError as e:
        return str(e)
    if again != body:
        return f"{args.id} の案が表示のあとに変わった。もう一度実行して本文を確かめる"
    return ""


def send_and_record(args, rec: dict, body: str, poster, directory) -> int:
    # 外へ出す前に「送信開始」を残す。送信後・記録前に落ちても、次の実行は
    # 上のガードで止まる (drafted のままだと黙ってもう一度送る)
    if store.update_record(args.id, {"send_started_at": store.utcnow()}, directory) is None:
        print(f"inbox に {args.id} を書けない", file=sys.stderr)
        return 2

    try:
        reply_id = poster(rec, body)
    except PostNotSent as e:
        # 相手側に何も出ていないことが確かな失敗だけ、印を外して再送を許す
        store.update_record(args.id, {"send_started_at": ""}, directory)
        print(f"送信失敗 (未送信): {e}", file=sys.stderr)
        return 1
    except PostError as e:
        # 届いたか分からない。印を残し、次の実行は相手側の確認と --force を要求する
        print(f"送信失敗 (届いたか不明。相手側を確認すること): {e}", file=sys.stderr)
        return 1

    done = store.update_record(
        args.id,
        {
            "status": store.STATUS_ANSWERED,
            "answered_at": store.utcnow(),
            "reply_native_id": reply_id,
            "answered_body": body,
            "send_started_at": "",
        },
        directory,
    )
    if done is None:
        print(
            f"送信はできたが inbox に記録できなかった: {reply_id}。再送しないこと",
            file=sys.stderr,
        )
        return 1
    print(f"送信完了: {reply_id}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--id", required=True, help="inbox id (例 threads:178...)")
    ap.add_argument("--body", default="", help="送信する本文 (最優先)")
    ap.add_argument(
        "--draft", type=int, default=None,
        help="保存済み案の番号 (issue / PENDING.md に出ている「案 N」。作り直しても再利用しない)",
    )
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

    reason = refuse_reason(rec, args.force)
    if reason:
        print(reason, file=sys.stderr)
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

    # dry-run でも未配線 channel は弾く (dry-run が通ったのに本番で落ちる食い違いを無くす)
    poster = POSTERS.get(rec["channel"])
    if poster is None or rec["channel"] in UNWIRED_CHANNELS:
        print(f"未対応 channel: {rec['channel']}", file=sys.stderr)
        return 2

    if args.dry_run:
        print("(dry-run: 送信していない)")
        return 0

    # ロックは送信と記録が終わるまで持つ。印を書いた時点で外すと、そのあとに
    # 起動した別の実行が「残った印」を読み、--force でそれを越えて送ってしまう
    # (印が生きた送信中のものか、落ちた実行の残りかを区別できない。#8323)
    try:
        with store.record_lock(args.id, directory):
            reason = recheck_under_lock(args, body, directory, rec)
            if reason:
                print(reason, file=sys.stderr)
                return 2
            return send_and_record(args, rec, body, poster, directory)
    except store.LockBusy as e:
        print(
            f"{args.id} は別の実行が送信中 (ロック {e})。二重送信を避けて止めた。"
            "その実行が落ちてロックだけが残っているなら、相手の投稿に返信が付いて"
            "いないことを確かめてからロックファイルを消し、--force で送る",
            file=sys.stderr,
        )
        return 2
