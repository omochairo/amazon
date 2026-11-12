"""外した商品ページの旧 URL を、代わりのページへ転送する（navi-brain#164 R1・#11647）。

付属品の ASIN に本体の説明が載っていた、のように「そのページ自体を出すのが誤り」と
分かった記事は、記事 JSON を消して ``data/asin_blocklist.json`` に入れる（取り込み・記事化で
二度と拾わない）。ただし検索で読まれていた URL を 404 にすると、探しに来た人が行き止まりになる。

そこで ``data/asin_redirects.json`` に「外した ASIN → 代わりに見せる ASIN」を書くと、
``build_post.py`` が代わりのページの front matter の ``aliases`` に旧 URL
（``/products/<外した asin>/``）を足す。Hugo はそこに転送のページ（canonical 付き）を置くので、
配信先（GitLab Pages / NAS の nginx）を問わず効く。

書式::

    {"redirects": [{"from": "B0H1KNTHWZ", "to": "B0H1KQ5CMD", "reason": "...", "added_at": "YYYY-MM-DD"}]}
"""
from __future__ import annotations

import json
import pathlib

DEFAULT_PATH = pathlib.Path("data/asin_redirects.json")


def load(path: pathlib.Path = DEFAULT_PATH) -> dict[str, list[str]]:
    """``{代わりの ASIN: [外した ASIN, ...]}`` を返す。ファイルが無ければ空。"""
    try:
        raw = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    out: dict[str, list[str]] = {}
    for r in raw.get("redirects", []):
        src = str(r.get("from", "")).strip().upper()
        dst = str(r.get("to", "")).strip().upper()
        if not src or not dst or src == dst:
            continue
        out.setdefault(dst, []).append(src)
    return out


def aliases_for(asin: str, redirects: dict[str, list[str]]) -> list[str]:
    """代わりのページに足す旧 URL の一覧。"""
    return [f"/products/{src.lower()}/" for src in redirects.get(str(asin).upper(), [])]


def conflicts(redirects: dict[str, list[str]], live_asins: set[str]) -> list[str]:
    """外したはずの ASIN の記事がまだ残っている／転送先の記事が無い、を返す。

    外した ASIN の記事が残っていると、同じ URL にページと転送が重なる。
    転送先の記事が無いと、旧 URL がどこにも転送されない。
    """
    live = {a.upper() for a in live_asins}
    errs = []
    for dst, srcs in redirects.items():
        if dst not in live:
            errs.append(f"転送先 {dst} の記事が無い（{', '.join(srcs)} の旧 URL が行き止まりになる）")
        for s in srcs:
            if s in live:
                errs.append(f"外した {s} の記事がまだ残っている（転送と重なる）")
    return errs
