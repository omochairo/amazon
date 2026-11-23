"""source_status.py — 商品ごとの出典の状態を覚える (候補供給 P1b, 仕様 navi-candidate-supply §4.1〜4.3)。

記事レーンの事前チェック (出典を開いて、要るサイト数に届くか) の結果を ASIN ごとに 1 行持つ。
出典の無い商品は待っても探しても出てこない (2026-11-22 の計測: 外した 113 件のうち後で書けたのは 0)。
3 日ごとに同じ商品を開き直すのをやめ、きっかけがあるときだけ見直す。

状態:
- writable      届いた。合格の URL は持たない (書く直前に必ず取り直す)
- insufficient  届かなかった。90 日後か、新しい候補 URL が増えたら見直す
- waiting_new   新発売で届かなかった。初観測から 14・30・60 日目に見直し、60 日を過ぎたら insufficient
- article_short 記事はあるが、今の判定で引用できる出典が要るサイト数に届かない (2026-11-23 の点検)。
                見直して届いたら書き直しの待ちに入れる (writable_article に変える)
- written       記事レーンが記事を書いた (PR を出した)。行を消すとリポジトリの写しとの合わせで前の状態が
                戻るので、消さずにこの状態にする
- writable_article  article_short が届いた。書き直しの待ちに入れた (記事の点検では見直さない。
                順番が来たら記事レーンが書き、記事ができたら行を消す)

行が無い = 未検査。書き手は K8 の記事レーンだけ。実行中は state_dir/source_status.json が正で、
回の終わりに data/source_status.json として出自台帳と同じ PR に入れる (Actions 側の参照用)。
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

FILE_NAME = "source_status.json"
REPO_PATH = "data/source_status.json"
RECHECK_DAYS = 90
ARTICLE_RECHECK_DAYS = 30
NEW_RECHECK_DAYS = (14, 30, 60)
WAIT_STATES = ("insufficient", "waiting_new", "article_short")


def load(path) -> dict:
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def save(path, status: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(dict(sorted(status.items())), ensure_ascii=False, indent=1) + "\n",
                   encoding="utf-8")
    os.replace(tmp, p)


def merge(base: dict, ours: dict) -> dict:
    """ASIN ごとに checked_at が新しい方を採る (同じなら ours)。"""
    out = dict(base)
    for a, e in ours.items():
        b = out.get(a)
        if not isinstance(b, dict) or (e.get("checked_at") or "") >= (b.get("checked_at") or ""):
            out[a] = e
    return out


def _date(s):
    try:
        return dt.date.fromisoformat((s or "")[:10])
    except ValueError:
        return None


def first_seen(asin: str, history_dir: str = "data/price_watch/history") -> str | None:
    """初観測日 (price_watch の最古の観測)。無ければ None (状態を書いた日では代用しない)。"""
    try:
        with open(os.path.join(history_dir, f"{asin}.jsonl"), encoding="utf-8") as f:
            for line in f:
                try:
                    ts = json.loads(line).get("ts")
                except ValueError:
                    continue
                if ts:
                    return ts[:10]
    except FileNotFoundError:
        pass
    return None


def _new_recheck(first: str | None, today: dt.date):
    """新発売の次の見直し日。60 日を過ぎていれば None (insufficient にする)。"""
    f = _date(first) or today
    for d in NEW_RECHECK_DAYS:
        day = f + dt.timedelta(days=d)
        if day > today:
            return day
    return None


def record(status: dict, asin: str, *, sites: int, need: int, checked_urls, today: dt.date,
           new_product: bool, has_article: bool = False, first: str | None = None) -> dict:
    """事前チェックの結果を 1 行に書く。戻り値はその行。"""
    prev = status.get(asin) if isinstance(status.get(asin), dict) else {}
    ok = sites >= need
    e = {
        "sites": int(sites), "need": int(need), "checked_at": today.isoformat(),
        "checked_urls": sorted(set(checked_urls)), "checks": int(prev.get("checks") or 0) + 1,
    }
    fs = prev.get("first_seen") or first
    if not fs and new_product:
        # 初観測 (price_watch) が無い新発売は、最初に調べた日を起点にする。毎回の今日を起点にすると
        # 見直し日が 14 日先に延び続けて 60 日に届かない。古い商品は新発売にならない (ASIN で判定) ので
        # 「古い商品が新しく見える」心配は無い
        fs = today.isoformat()
        e["first_seen_approx"] = True
    elif prev.get("first_seen_approx") and fs == prev.get("first_seen"):
        e["first_seen_approx"] = True
    if fs:
        e["first_seen"] = fs
    if has_article:
        e["state"] = "writable_article" if ok else "article_short"
        if not ok:
            e["recheck_after"] = (today + dt.timedelta(days=ARTICLE_RECHECK_DAYS)).isoformat()
    elif ok:
        e["state"] = "writable"
    elif new_product and (nxt := _new_recheck(fs, today)):
        e["state"] = "waiting_new"
        e["recheck_after"] = nxt.isoformat()
    else:
        e["state"] = "insufficient"
        e["recheck_after"] = (today + dt.timedelta(days=RECHECK_DAYS)).isoformat()
    status[asin] = e
    return e


def due(entry, current_urls, today: dt.date) -> tuple[bool, str]:
    """見直す (事前チェックで本文を開く) か。(見直すか, 理由)。

    見直すのは: 行が無い・writable・期限が来た・今の候補 URL に前回開いていない URL がある
    (増えたときだけ。リンク切れで減っただけでは見直さない)。
    """
    if not isinstance(entry, dict) or not entry.get("state"):
        return True, "unchecked"
    state = entry["state"]
    if state == "writable":
        return True, "writable"
    if state == "writable_article":
        # 書き直しの待ちに入れた。順番が来たら書く (記事の点検 recheck_articles は article_short だけを見る)
        return True, "rewrite_queued"
    if state not in WAIT_STATES:
        return True, "unknown_state"
    new = set(current_urls) - set(entry.get("checked_urls") or ())
    if new:
        return True, "new_urls"
    ra = _date(entry.get("recheck_after"))
    if ra is None or today >= ra:
        return True, "recheck_due"
    return False, state
