"""check_sources_v5: 本文から参照された非販売の出典をサイトで数える (navi-brain#92 案 C).

sources を消して合格させる穴 (#8934) も引き続き塞ぐ。
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quality_gate import SOURCES_REF_ENFORCE_FROM, check_sources_v5  # noqa: E402
import source_sites  # noqa: E402

# 施行日以降の slug (新条件だけで判定される)
NEW_SLUG = f"{SOURCES_REF_ENFORCE_FROM}-B0TEST0001"
# 施行日より前の slug (旧条件でも合格できる)
OLD_SLUG = "2026-09-26-B0TEST0001"


def _src(i, url_host="example.org"):
    return {"id": f"s{i}", "name": f"src {i}", "url": f"https://{url_host}/{i}"}


def _article(srcs, claim_ids=(), review_ids=(), slug=NEW_SLUG):
    data = {"date": "2026-09-26", "slug": slug, "sources": srcs}
    data["claims"] = [{"text": f"c{i}", "supporting_source_ids": [sid]}
                      for i, sid in enumerate(claim_ids)]
    if review_ids:
        data["review_signals"] = {
            "high_points": [{"text": "h", "supporting_source_ids": list(review_ids)}],
        }
    return data


def test_absent_sources_fails_for_new_article():
    r = check_sources_v5({"date": "2026-09-26"})
    assert r.passed is False
    assert "field 無し" in r.message


def test_absent_sources_skipped_for_legacy_article():
    r = check_sources_v5({"date": "2026-05-17"})
    assert r.passed is True


def test_two_referenced_sites_pass_without_total_count():
    # 合計件数の条件は無い: 参照された非販売 2 サイトだけで合格
    srcs = [_src(1, "a.example.org"), _src(2, "example.net")]
    r = check_sources_v5(_article(srcs, claim_ids=["s1", "s2"]))
    assert r.passed is True, r.message


def test_unreferenced_sources_do_not_count():
    # 5 件あっても本文から参照されていなければ数えない (件数合わせを通さない)
    srcs = [_src(i, f"site{i}.example") for i in range(5)]
    r = check_sources_v5(_article(srcs, claim_ids=["s0"]))
    assert r.passed is False
    assert "1 サイト" in r.message


def test_review_signals_references_count():
    srcs = [_src(1, "example.org"), _src(2, "example.net")]
    r = check_sources_v5(_article(srcs, claim_ids=["s1"], review_ids=["s2"]))
    assert r.passed is True, r.message


def test_all_review_signal_fields_count():
    srcs = [_src(i, f"site{i}.example") for i in range(4)]
    data = _article(srcs)
    data["review_signals"] = {
        "use_scenes": [{"text": "u", "supporting_source_ids": ["s0"]}],
        "concerns": [{"text": "c", "supporting_source_ids": ["s1"]}],
    }
    assert check_sources_v5(data).passed is True
    data["review_signals"] = {
        "segment_voices": [{"segment_label": "x", "summary": "y", "supporting_source_ids": ["s2", "s3"]}],
    }
    assert check_sources_v5(data).passed is True


def test_other_fields_are_not_references():
    # id を文字列で探さない: faq や narrative に "s2" と書いてあっても参照ではない
    srcs = [_src(1, "example.org"), _src(2, "example.net")]
    data = _article(srcs, claim_ids=["s1"])
    data["faq"] = [{"q": "s2", "a": "s2"}]
    assert check_sources_v5(data).passed is False


def test_same_registered_domain_is_one_site():
    # ja/en の wikipedia、サブドメインと親ドメインは 1 サイト
    srcs = [_src(1, "ja.wikipedia.org"), _src(2, "en.wikipedia.org")]
    assert check_sources_v5(_article(srcs, claim_ids=["s1", "s2"])).passed is False
    srcs = [_src(1, "toy.bandai.co.jp"), _src(2, "bandai.co.jp")]
    assert check_sources_v5(_article(srcs, claim_ids=["s1", "s2"])).passed is False


def test_sns_counts_as_one_site_at_most():
    srcs = [_src(1, "www.youtube.com"), _src(2, "youtu.be"), _src(3, "x.com")]
    assert check_sources_v5(_article(srcs, claim_ids=["s1", "s2", "s3"])).passed is False
    srcs.append(_src(4, "example.org"))
    assert check_sources_v5(_article(srcs, claim_ids=["s1", "s4"])).passed is True


def test_sales_and_retail_pages_are_not_non_sales():
    srcs = [_src(1, "www.amazon.co.jp"), _src(2, "www.yodobashi.com"), _src(3, "example.org")]
    r = check_sources_v5(_article(srcs, claim_ids=["s1", "s2", "s3"]))
    assert r.passed is False
    assert "1 サイト (example.org)" in r.message


def test_dangling_reference_is_ignored():
    srcs = [_src(1, "example.org")]
    assert check_sources_v5(_article(srcs, claim_ids=["s1", "s9"])).passed is False


def test_navi_self_citation_fails_even_when_referenced():
    # #9199: navi.omcha.jp はこのサイト自身。条件を満たしても通さない
    srcs = [_src(1, "example.org"), _src(2, "example.net"), _src(9, "navi.omcha.jp")]
    r = check_sources_v5(_article(srcs, claim_ids=["s1", "s2", "s9"]))
    assert r.passed is False
    assert "navi.omcha.jp" in r.message


def test_omcha_first_party_post_is_allowed():
    # 本家 omcha.jp の実使用記事は一次情報として使ってよい (#9199 案b)
    srcs = [_src(1, "example.org"), _src(9, "omcha.jp")]
    assert check_sources_v5(_article(srcs, claim_ids=["s1", "s9"])).passed is True


def test_omcha_posts_count_as_one_site():
    # 本家の記事 2 件は 1 サイト (2 件目は外部の第三者が要る)
    srcs = [_src(7, "omcha.jp"), _src(8, "www.omcha.jp")]
    assert check_sources_v5(_article(srcs, claim_ids=["s7", "s8"])).passed is False


def test_before_enforce_date_old_rule_still_passes():
    # 既存記事を落とさない: 施行日前の slug は旧条件 (合計 5・非販売 2) でも合格
    srcs = [_src(i, "example.org") for i in range(5)]
    data = _article(srcs, slug=OLD_SLUG)
    assert check_sources_v5(data).passed is True
    data["slug"] = NEW_SLUG
    assert check_sources_v5(data).passed is False


def test_before_enforce_date_new_rule_also_passes():
    # 新テンプレートは最低 5 件をやめるので、施行日前の slug でも新条件で通す
    srcs = [_src(1, "example.org"), _src(2, "example.net")]
    assert check_sources_v5(_article(srcs, claim_ids=["s1", "s2"], slug=OLD_SLUG)).passed is True


def test_before_enforce_date_old_rule_omcha_counts_once():
    # 旧条件の omcha.jp 1 件まで (#9199 案b) は施行日前の記事でも維持
    srcs = ([_src(i, "www.amazon.co.jp") for i in range(3)]
            + [_src(7, "omcha.jp"), _src(8, "omcha.jp")])
    assert check_sources_v5(_article(srcs, slug=OLD_SLUG)).passed is False
    srcs[-1] = _src(8, "example.org")
    assert check_sources_v5(_article(srcs, slug=OLD_SLUG)).passed is True


def test_registered_domain():
    rd = source_sites.registered_domain
    assert rd("toy.bandai.co.jp") == "bandai.co.jp"
    assert rd("ja.wikipedia.org") == "wikipedia.org"
    assert rd("example.org") == "example.org"
    assert rd("foo.hatenablog.com") == "foo.hatenablog.com"
    assert rd("www.bbc.co.uk") == "bbc.co.uk"
    assert source_sites.site_key("www.youtube.com") == source_sites.site_key("x.com")
    assert source_sites.host_of("https://www.Example.org:443/a") == "example.org"
