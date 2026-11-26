"""hugo/static/llms.txt と、その案内 (robots.txt・head の link) の形を守るテスト。

llms.txt は AI アシスタント向けのサイト案内 (https://llmstxt.org/)。
`.gitignore` の `/hugo/*` に飲まれる位置にあるので、消えても気づきにくい。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LLMS = ROOT / "hugo" / "static" / "llms.txt"
ROBOTS = ROOT / "hugo" / "layouts" / "robots.txt"
EXTEND_HEAD = ROOT / "hugo" / "layouts" / "partials" / "extend_head.html"


def _text() -> str:
    return LLMS.read_text(encoding="utf-8")


def test_llms_txt_has_title_and_summary():
    lines = _text().splitlines()
    # llmstxt.org の形: 1 行目が H1、空行のあとに > の要約
    assert lines[0].startswith("# ")
    assert any(line.startswith("> ") for line in lines[:4])


def test_llms_txt_links_stay_on_navi_or_omcha():
    # 出典は navi の商品ページに寄せる方針。ショップ (Amazon など) の URL は載せない
    urls = re.findall(r"https?://[^\s)`]+", _text())
    assert urls
    for url in urls:
        host = re.match(r"https?://([^/]+)", url).group(1)
        assert host in {"navi.omcha.jp", "omcha.jp", "llmstxt.org"}, url


def test_llms_txt_age_hubs_exist():
    # 年齢別ランキングは age-5 を持たない (age-4 が 4・5 歳。scripts/build_age_concerns.py)
    linked = set(re.findall(r"/toys-age-(\d)/", _text()))
    assert linked == {"0", "1", "2", "3", "4", "6"}


def test_llms_txt_is_announced():
    robots = ROBOTS.read_text(encoding="utf-8")
    assert re.search(r"^# llms\.txt: ", robots, re.M)
    assert not re.search(r"^Disallow:.*llms", robots, re.M)
    assert 'href="{{ "llms.txt" | absURL }}"' in EXTEND_HEAD.read_text(encoding="utf-8")
