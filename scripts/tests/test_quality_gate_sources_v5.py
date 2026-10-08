"""check_sources_v5: sources を消して合格させる穴を塞ぐ (#8934)."""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quality_gate import check_sources_v5  # noqa: E402


def _src(i, url_host="example.org"):
    return {"id": f"s{i}", "name": f"src {i}", "url": f"https://{url_host}/{i}"}


def test_absent_sources_fails_for_new_article():
    r = check_sources_v5({"date": "2026-09-26"})
    assert r.passed is False
    assert "field 無し" in r.message


def test_absent_sources_skipped_for_legacy_article():
    r = check_sources_v5({"date": "2026-05-17"})
    assert r.passed is True


def test_too_few_sources_fails():
    r = check_sources_v5({"date": "2026-09-26", "sources": [_src(1)]})
    assert r.passed is False


def test_five_sources_with_third_party_passes():
    srcs = [_src(i) for i in range(5)]
    r = check_sources_v5({"date": "2026-09-26", "sources": srcs})
    assert r.passed is True


def test_navi_self_citation_fails_even_with_enough_sources():
    # #9199: navi.omcha.jp はこのサイト自身。件数・非販売を満たしても通さない
    srcs = [_src(i) for i in range(5)] + [_src(9, "navi.omcha.jp")]
    r = check_sources_v5({"date": "2026-09-26", "sources": srcs})
    assert r.passed is False
    assert "navi.omcha.jp" in r.message


def test_omcha_first_party_post_is_allowed():
    # 本家 omcha.jp の実使用記事は一次情報として使ってよい (#9199 案b)
    srcs = [_src(i) for i in range(4)] + [_src(9, "omcha.jp")]
    r = check_sources_v5({"date": "2026-09-26", "sources": srcs})
    assert r.passed is True


def test_omcha_posts_count_as_one_non_sales_at_most():
    # 本家の記事 2 件 + 販売 3 件 → 非販売は 1 件扱い (2 件目は外部の第三者が要る)
    srcs = ([_src(i, "www.amazon.co.jp") for i in range(3)]
            + [_src(7, "omcha.jp"), _src(8, "omcha.jp")])
    r = check_sources_v5({"date": "2026-09-26", "sources": srcs})
    assert r.passed is False
    srcs[-1] = _src(8, "example.org")
    assert check_sources_v5({"date": "2026-09-26", "sources": srcs}).passed is True
