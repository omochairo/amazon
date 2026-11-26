#!/usr/bin/env python3
"""デプロイ後に、本文が更新されたページを IndexNow で検索エンジンへ知らせる。

.gitlab-ci.yml の indexnow ジョブ (cf-purge の後) から呼ぶ。標準ライブラリだけで動く。

なぜ要るか:
  ChatGPT の検索などは Bing の索引を使う。IndexNow (https://www.indexnow.org/) は
  「この URL が変わった」を Bing などに直接知らせる仕組みで、クロールを待たずに
  新しい記事・書き直した記事が拾われる。

送る URL (3 つを全部満たすもの):
  1. 今回の配信で中身が変わったページ (deploy-nas が保存した navi-switch.out の差分)
  2. 新しい public/sitemap.xml に載っているページ (= インデックスしてほしいページ。
     noindex・canonical を他へ向けたページは sitemap.xml の側で除かれている)
  3. その lastmod が直近 INDEXNOW_LASTMOD_HOURS 時間 (既定 72) 以内
     → 価格だけ変わった日のページは lastmod が古いので送らない。毎日数千件を
       送り直さないための条件

  差分が読めない (前世代が無い・件数が合わない等) ときは 1 を外し、2 と 3 だけで選ぶ。
  削除されたページは送らない (v1)。

送る前に、本番の鍵ファイル (https://navi.omcha.jp/<鍵>.txt) が鍵と同じ中身を返すかを
確かめる。返さなければ送らずに失敗する (検索エンジン側で鍵の確認に落ちるため)。

鍵は hugo/static/<32 桁の 16 進>.txt に置く (中身はファイル名と同じ)。IndexNow の鍵は
公開して使うものなので、public リポジトリに置いてよい。

使いかた:
  python3 scripts/ci_indexnow.py --diff navi-switch.out --public public
  INDEXNOW_DRY_RUN=1 で、本番の鍵の確認と送信をせずに、選んだ URL の件数だけ出す。

終了コード: 0 = 送った / 送るものが無かった、1 = 失敗 (鍵が無い・本番の鍵が違う・送信の失敗)。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable

BASE = os.environ.get("INDEXNOW_BASE", "https://navi.omcha.jp").rstrip("/")
ENDPOINT = os.environ.get("INDEXNOW_ENDPOINT", "https://api.indexnow.org/indexnow")
# IndexNow の 1 回の上限は 10,000 URL
MAX_URLS = 10_000
KEY_RE = re.compile(r"^[0-9a-f]{32}$")
SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
TAB = "\t"


def log(msg: str) -> None:
    print(f"[indexnow] {msg}", file=sys.stderr)


def find_key(public: Path) -> str:
    """public/ 直下の <32 桁の 16 進>.txt で、中身がファイル名と同じものを鍵とする。"""
    keys = []
    for p in public.glob("*.txt"):
        if KEY_RE.match(p.stem) and p.read_text(encoding="utf-8").strip() == p.stem:
            keys.append(p.stem)
    if len(keys) != 1:
        raise SystemExit(f"[indexnow] 鍵ファイルが 1 つに決まらない ({len(keys)} 件): {public}/<鍵>.txt")
    return keys[0]


def read_changed_paths(diff: Path) -> list[str] | None:
    """navi-switch.out から変わったパスを返す。読めないときは None。

    形式は scripts/ci_cf_purge.sh の説明と同じ:
      navi-diff-begin / navi-diff<TAB><パス> ... / navi-diff-end<TAB><件数>、
      または navi-diff-unknown。
    """
    if not diff.is_file():
        log(f"差分ファイル {diff} が無い")
        return None
    lines = diff.read_text(encoding="utf-8", errors="replace").splitlines()
    if "navi-diff-unknown" in lines:
        log("前世代が無い (navi-diff-unknown)")
        return None
    ends = [ln for ln in lines if ln.startswith("navi-diff-end" + TAB)]
    if "navi-diff-begin" not in lines or not ends:
        log("差分の区切りが無い")
        return None
    paths = [ln.split(TAB, 1)[1] for ln in lines if ln.startswith("navi-diff" + TAB)]
    declared = ends[-1].split(TAB, 1)[1].strip()
    if declared != str(len(paths)):
        log(f"差分の件数が合わない (宣言 {declared} / 実際 {len(paths)})")
        return None
    return paths


def path_to_url(path: str) -> str | None:
    """index.html のパスだけを、読者が開く URL にする (それ以外のファイルは送らない)。"""
    if path == "index.html":
        return BASE + "/"
    if path.endswith("/index.html"):
        return f"{BASE}/{path[: -len('index.html')]}"
    return None


def read_sitemap(sitemap: Path) -> dict[str, dt.datetime | None]:
    """sitemap.xml の URL → lastmod (無ければ None)。"""
    root = ET.parse(sitemap).getroot()
    out: dict[str, dt.datetime | None] = {}
    for url in root.iter(f"{SITEMAP_NS}url"):
        loc = (url.findtext(f"{SITEMAP_NS}loc") or "").strip()
        if not loc:
            continue
        lm = (url.findtext(f"{SITEMAP_NS}lastmod") or "").strip()
        try:
            out[loc] = dt.datetime.fromisoformat(lm) if lm else None
        except ValueError:
            out[loc] = None
        if out[loc] is not None and out[loc].tzinfo is None:
            out[loc] = out[loc].replace(tzinfo=dt.timezone.utc)
    return out


def select_urls(
    changed: list[str] | None,
    sitemap: dict[str, dt.datetime | None],
    now: dt.datetime,
    hours: float,
) -> list[str]:
    cutoff = now - dt.timedelta(hours=hours)
    recent = {u for u, lm in sitemap.items() if lm is not None and lm >= cutoff}
    if changed is None:
        picked = recent
    else:
        changed_urls = {u for u in (path_to_url(p) for p in changed) if u}
        picked = changed_urls & recent
    # 念のため自サイト以外は送らない (sitemap の baseURL が変わったときの事故防止)
    return sorted(u for u in picked if u.startswith(BASE + "/"))


def http_get(url: str) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "navi-indexnow/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return res.status, res.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, ""


def http_post_json(url: str, body: dict) -> tuple[int, str]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": "navi-indexnow/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            return res.status, res.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


def run(
    diff: Path,
    public: Path,
    now: dt.datetime,
    hours: float,
    dry_run: bool,
    get: Callable[[str], tuple[int, str]] = http_get,
    post: Callable[[str, dict], tuple[int, str]] = http_post_json,
) -> int:
    key = find_key(public)
    urls = select_urls(read_changed_paths(diff), read_sitemap(public / "sitemap.xml"), now, hours)
    log(f"送る URL {len(urls)} 件 (lastmod が直近 {hours:g} 時間以内)")
    if not urls:
        return 0
    if dry_run:
        log("dry-run: 本番の鍵の確認と送信をしない")
        for u in urls[:20]:
            log(f"  {u}")
        return 0

    key_url = f"{BASE}/{key}.txt"
    status, text = get(key_url)
    if status != 200 or text.strip() != key:
        log(f"本番の鍵ファイル {key_url} が鍵を返さない (HTTP {status})。送らずに止める")
        return 1

    host = BASE.split("://", 1)[1]
    # 1 回の上限 (10,000 件) ごとに分けて全部送る。1 回でも失敗したら失敗にする
    for start in range(0, len(urls), MAX_URLS):
        chunk = urls[start : start + MAX_URLS]
        body = {"host": host, "key": key, "keyLocation": key_url, "urlList": chunk}
        status, text = post(ENDPOINT, body)
        # 200 = 受け付けた、202 = 受け付けたが鍵の確認待ち
        if status not in (200, 202):
            log(f"送信に失敗した (HTTP {status}, {start + 1}〜{start + len(chunk)} 件目): {text[:300]}")
            return 1
        log(f"送信した (HTTP {status}, {start + 1}〜{start + len(chunk)} 件目)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--diff", type=Path, default=Path("navi-switch.out"))
    ap.add_argument("--public", type=Path, default=Path("public"))
    args = ap.parse_args(argv)
    hours = float(os.environ.get("INDEXNOW_LASTMOD_HOURS", "72"))
    dry = os.environ.get("INDEXNOW_DRY_RUN") == "1"
    return run(args.diff, args.public, dt.datetime.now(dt.timezone.utc), hours, dry)


if __name__ == "__main__":
    sys.exit(main())
