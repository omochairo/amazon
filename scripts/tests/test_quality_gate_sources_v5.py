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
