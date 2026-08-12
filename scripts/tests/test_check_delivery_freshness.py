"""check_delivery_freshness.py の unit tests (#5042).

成り立つべき条件:
  1. 導入時点の実測 (2026-08-12 14:00 UTC に最新 lastmod が同日 13:48 UTC) で
     **鳴らない** = 鳴りっぱなしゲートにしない。
  2. 止まったら必ず鳴る。GitLab 側の pages が落ちても GitHub の run は緑のままで、
     症状は「lastmod が進まない」1 つに落ちるので、原因を問わずこの網で拾える。
  3. 判定不能 (取得失敗 / lastmod ゼロ件) を ok に潰さない。
"""
from __future__ import annotations

import datetime as dt

import pytest

from scripts.check_delivery_freshness import (
    DEFAULT_MAX_AGE_HOURS,
    FetchError,
    check,
    child_sitemaps,
    is_sitemap_index,
    latest_lastmod,
    parse_lastmods,
    render_body,
    title_for,
)

T = dt.datetime.fromisoformat


def _urlset(*lastmods: str) -> str:
    entries = "".join(
        f"<url><loc>https://navi.omcha.jp/p/{i}/</loc><lastmod>{lm}</lastmod></url>"
        for i, lm in enumerate(lastmods)
    )
    return (
        '<?xml version="1.0" encoding="utf-8" standalone="yes"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{entries}</urlset>"
    )


def _index(*children: str) -> str:
    entries = "".join(f"<sitemap><loc>{c}</loc></sitemap>" for c in children)
    return (
        '<?xml version="1.0" encoding="utf-8" standalone="yes"?>'
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{entries}</sitemapindex>"
    )


# --- parse_lastmods ---------------------------------------------------------

def test_parses_hugo_offset_format():
    got = parse_lastmods(_urlset("2026-08-12T13:48:12+00:00"))
    assert got == [T("2026-08-12T13:48:12+00:00")]


def test_parses_z_terminated_and_normalizes_to_utc():
    # Hugo は +00:00 で出すが、Z 終端も sitemap 仕様上あり得る。
    got = parse_lastmods(_urlset("2026-08-12T13:48:12Z"))
    assert got == [T("2026-08-12T13:48:12+00:00")]


def test_non_utc_offset_is_converted():
    got = parse_lastmods(_urlset("2026-08-12T22:48:12+09:00"))
    assert got == [T("2026-08-12T13:48:12+00:00")]


def test_naive_lastmod_is_treated_as_utc():
    # tz を落とす実装に変わっても「lastmod ゼロ件」で誤報しないこと。
    got = parse_lastmods(_urlset("2026-08-12T13:48:12"))
    assert got == [T("2026-08-12T13:48:12+00:00")]


def test_unparseable_entries_are_skipped_not_fatal():
    xml = _urlset("2026-08-12T13:48:12+00:00", "not-a-date", "")
    assert parse_lastmods(xml) == [T("2026-08-12T13:48:12+00:00")]


def test_no_lastmod_yields_empty():
    assert parse_lastmods("<urlset><url><loc>https://x/</loc></url></urlset>") == []


# --- sitemapindex -----------------------------------------------------------

def test_detects_index_and_lists_children():
    xml = _index("https://navi.omcha.jp/a.xml", "https://navi.omcha.jp/b.xml")
    assert is_sitemap_index(xml)
    assert child_sitemaps(xml) == [
        "https://navi.omcha.jp/a.xml", "https://navi.omcha.jp/b.xml"]


def test_urlset_is_not_index():
    assert not is_sitemap_index(_urlset("2026-08-12T13:48:12+00:00"))


def test_latest_across_children_of_an_index():
    pages = {
        "root": _index("a", "b"),
        "a": _urlset("2026-08-10T00:00:00+00:00"),
        "b": _urlset("2026-08-12T13:48:12+00:00"),
    }
    got = latest_lastmod("root", lambda u: pages[u])
    assert got == T("2026-08-12T13:48:12+00:00")


def test_one_broken_child_does_not_void_the_whole_index():
    pages = {"root": _index("a", "b"), "b": _urlset("2026-08-12T13:48:12+00:00")}

    def fetch(url):
        if url not in pages:
            raise FetchError("HTTP 500")
        return pages[url]

    assert latest_lastmod("root", fetch) == T("2026-08-12T13:48:12+00:00")


def test_self_referencing_index_terminates():
    # 循環しても止まること (無限再帰でジョブを固めない)。
    got = latest_lastmod("root", lambda u: _index("root"))
    assert got is None


# --- check ------------------------------------------------------------------

NOW = T("2026-08-12T14:00:00+00:00")


def test_measured_reality_at_introduction_does_not_fire():
    """導入時の実測: 14:00 UTC 時点で最新 lastmod は同日 13:48 UTC (= 12 分前)。"""
    row = check("u", NOW, lambda _: _urlset("2026-08-12T13:48:12+00:00"))
    assert row["status"] == "ok"
    assert row["age_hours"] == 0.2


def test_stale_when_older_than_threshold():
    row = check("u", NOW, lambda _: _urlset("2026-08-10T00:00:00+00:00"))
    assert row["status"] == "stale"
    assert row["age_hours"] > DEFAULT_MAX_AGE_HOURS


def test_boundary_just_inside_threshold_is_ok():
    latest = NOW - dt.timedelta(hours=DEFAULT_MAX_AGE_HOURS) + dt.timedelta(minutes=1)
    row = check("u", NOW, lambda _: _urlset(latest.isoformat()))
    assert row["status"] == "ok"


def test_boundary_just_outside_threshold_is_stale():
    latest = NOW - dt.timedelta(hours=DEFAULT_MAX_AGE_HOURS) - dt.timedelta(minutes=1)
    row = check("u", NOW, lambda _: _urlset(latest.isoformat()))
    assert row["status"] == "stale"


def test_custom_threshold_is_honoured():
    row = check("u", NOW, lambda _: _urlset("2026-08-12T00:00:00+00:00"),
                max_age_hours=6)
    assert row["status"] == "stale"


def test_fetch_failure_is_unreachable_not_ok():
    def fetch(_):
        raise FetchError("HTTP 503")

    row = check("u", NOW, fetch)
    assert row["status"] == "unreachable"
    assert "503" in row["detail"]


def test_readable_sitemap_without_lastmod_is_unknown_not_ok():
    row = check("u", NOW, lambda _: "<urlset><url><loc>https://x/</loc></url></urlset>")
    assert row["status"] == "unknown"


@pytest.mark.parametrize("status", ["stale", "unreachable", "unknown"])
def test_every_abnormal_status_has_its_own_title(status):
    assert title_for({"status": status}).startswith("[delivery]")
    assert title_for({"status": status}) != title_for({"status": "other"})


# --- render_body ------------------------------------------------------------

def test_body_carries_marker_and_numbers():
    row = check("https://navi.omcha.jp/sitemap.xml", NOW,
                lambda _: _urlset("2026-08-09T00:00:00+00:00"))
    body = render_body(row, NOW)
    assert "<!-- delivery-freshness-monitor -->" in body
    assert "2026-08-09T00:00:00+00:00" in body
    assert f"{DEFAULT_MAX_AGE_HOURS}h" in body
    # 見た人が次に何を見ればいいかが本文だけで分かること。
    assert "40-mirror-to-gitlab.yml" in body


def test_body_of_unreachable_shows_detail():
    def fetch(_):
        raise FetchError("HTTP 404")

    body = render_body(check("u", NOW, fetch), NOW)
    assert "unreachable" in body
    assert "HTTP 404" in body
