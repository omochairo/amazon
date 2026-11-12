"""navi-brain#164 R1: 外した商品ページの旧 URL を代わりのページへ転送する。"""
import json
import pathlib
import re

import asin_redirects

REPO = pathlib.Path(__file__).resolve().parents[2]


def _write(tmp_path, rows):
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"redirects": rows}), encoding="utf-8")
    return p


def test_load_groups_by_destination_and_normalizes(tmp_path):
    p = _write(tmp_path, [
        {"from": "b0aaaaaaaa", "to": "B0CCCCCCCC"},
        {"from": "B0BBBBBBBB", "to": "b0cccccccc"},
        {"from": "B0DDDDDDDD", "to": "B0DDDDDDDD"},   # 自分自身への転送は捨てる
        {"from": "", "to": "B0CCCCCCCC"},
    ])
    m = asin_redirects.load(p)
    assert m == {"B0CCCCCCCC": ["B0AAAAAAAA", "B0BBBBBBBB"]}
    assert asin_redirects.aliases_for("b0cccccccc", m) == ["/products/b0aaaaaaaa/", "/products/b0bbbbbbbb/"]
    assert asin_redirects.aliases_for("B0ZZZZZZZZ", m) == []


def test_missing_file_is_empty(tmp_path):
    assert asin_redirects.load(tmp_path / "none.json") == {}


def test_conflicts():
    m = {"B0CCCCCCCC": ["B0AAAAAAAA"]}
    assert asin_redirects.conflicts(m, {"B0CCCCCCCC"}) == []
    assert any("まだ残っている" in e for e in asin_redirects.conflicts(m, {"B0CCCCCCCC", "B0AAAAAAAA"}))
    assert any("転送先" in e for e in asin_redirects.conflicts(m, set()))


def test_frontmatter_adds_alias_to_replacement_page():
    from build_post import _frontmatter_meta
    data = {"title": "t", "product": {"asin": "B0CCCCCCCC"}, "date": "2026-01-01T00:00:00+09:00"}
    meta = _frontmatter_meta(data, "2026-01-01-B0CCCCCCCC", False, {}, pathlib.Path("x.json"),
                             asin_redirect_map={"B0CCCCCCCC": ["B0AAAAAAAA"]})
    assert meta["aliases"] == ["/posts/2026-01-01-b0cccccccc/", "/products/b0aaaaaaaa/"]
    other = _frontmatter_meta(data, "2026-01-01-B0CCCCCCCC", False, {}, pathlib.Path("x.json"))
    assert other["aliases"] == ["/posts/2026-01-01-b0cccccccc/"]


def test_repo_redirects_are_consistent():
    """リポジトリの転送表: 外した記事が残っていない・転送先の記事がある・ブロックリストに入っている。"""
    m = asin_redirects.load(REPO / "data" / "asin_redirects.json")
    live = set()
    for f in (REPO / "data" / "articles").glob("*.json"):
        mm = re.search(r"-(B0[A-Z0-9]{8})$", f.stem)
        if mm:
            live.add(mm.group(1))
    assert asin_redirects.conflicts(m, live) == []
    blocked = {b["asin"] for b in json.loads((REPO / "data" / "asin_blocklist.json").read_text(encoding="utf-8"))["blocked"]}
    retired = {s for srcs in m.values() for s in srcs}
    assert retired <= blocked, f"ブロックリストに無い: {sorted(retired - blocked)}"
