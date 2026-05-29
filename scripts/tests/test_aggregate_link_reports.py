"""Tests for scripts/aggregate_link_reports.py (Issue #722).

Issue Forms から来る body 形式
  ### 商品 ASIN

  B0083AHXT0

  ### 対象 EC サイト

  楽天市場
を parse → (asin, ec_site) で aggregate → 閾値超え検出 → low_confidence
ファイル append-unique までを unit test。
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aggregate_link_reports import (  # noqa: E402
    EC_SITE_NORMALIZE,
    RESEARCHABLE_SITES,
    aggregate,
    extract_report,
    parse_issue_body,
    update_low_confidence_file,
)


SAMPLE_BODY = (
    "### 記事 URL\n\n"
    "https://navi.omcha.jp/posts/2026-05-16-b0083ahxt0/\n\n"
    "### 商品 ASIN\n\n"
    "B0083AHXT0\n\n"
    "### 対象 EC サイト\n\n"
    "楽天市場\n\n"
    "### 問題のあるリンク URL (任意)\n\n"
    "https://example.invalid/\n\n"
    "### 問題の種類\n\n"
    "別商品にリンクされている\n\n"
    "### 詳細 (任意)\n\n"
    "_No response_"
)


def _issue(num: int, body: str, labels: list[str] | None = None) -> dict:
    return {
        "number": num,
        "body": body,
        "labels": [{"name": n} for n in (labels or ["link-report"])],
    }


def test_parse_issue_body_extracts_all_form_fields():
    fields = parse_issue_body(SAMPLE_BODY)
    assert fields["商品 ASIN"] == "B0083AHXT0"
    assert fields["対象 EC サイト"] == "楽天市場"
    assert fields["問題の種類"] == "別商品にリンクされている"


def test_extract_report_normalizes_ec_site():
    report = extract_report(_issue(777, SAMPLE_BODY))
    assert report == {"asin": "B0083AHXT0", "ec_site": "rakuten", "issue_number": 777}


def test_extract_report_handles_yahoo_and_amazon():
    yahoo_body = SAMPLE_BODY.replace("楽天市場", "Yahoo!ショッピング")
    amazon_body = SAMPLE_BODY.replace("楽天市場", "Amazon")
    assert extract_report(_issue(1, yahoo_body))["ec_site"] == "yahoo"
    assert extract_report(_issue(2, amazon_body))["ec_site"] == "amazon"


def test_extract_report_returns_none_for_unknown_ec_site():
    bad = SAMPLE_BODY.replace("楽天市場", "メルカリ")
    assert extract_report(_issue(3, bad)) is None


def test_extract_report_returns_none_when_asin_missing():
    bad = SAMPLE_BODY.replace("B0083AHXT0", "_No response_")
    assert extract_report(_issue(4, bad)) is None


def test_ec_site_mapping_matches_form_dropdown_options():
    # Issue Forms の dropdown options と同じキーが揃っていること (退化検知)
    assert set(EC_SITE_NORMALIZE.keys()) == {"Amazon", "楽天市場", "Yahoo!ショッピング"}


def test_researchable_sites_excludes_amazon():
    # Amazon は ASIN 自体由来なので auto re-search 対象外
    assert "amazon" not in RESEARCHABLE_SITES
    assert RESEARCHABLE_SITES == {"rakuten", "yahoo"}


def test_aggregate_groups_and_flags_above_threshold():
    issues = [
        _issue(101, SAMPLE_BODY),
        _issue(102, SAMPLE_BODY),
        _issue(103, SAMPLE_BODY.replace("B0083AHXT0", "B0CWH43WP4")),
    ]
    result = aggregate(issues, threshold=2)
    assert result["total_issues_scanned"] == 3
    assert result["threshold"] == 2
    assert len(result["flagged"]) == 1
    flag = result["flagged"][0]
    assert flag["asin"] == "B0083AHXT0"
    assert flag["ec_site"] == "rakuten"
    assert flag["count"] == 2
    assert sorted(flag["issue_numbers"]) == [101, 102]
    assert flag["researchable"] is True


def test_aggregate_flags_amazon_but_marks_not_researchable():
    amazon_body = SAMPLE_BODY.replace("楽天市場", "Amazon")
    result = aggregate([_issue(1, amazon_body), _issue(2, amazon_body)], threshold=2)
    assert len(result["flagged"]) == 1
    flag = result["flagged"][0]
    assert flag["ec_site"] == "amazon"
    assert flag["researchable"] is False  # Amazon は自動 re-search 対象外


def test_aggregate_records_unparseable_issue_numbers():
    issues = [
        _issue(201, SAMPLE_BODY),
        _issue(202, "garbage body without form headers"),
    ]
    result = aggregate(issues, threshold=2)
    assert result["skipped_unparseable_issue_numbers"] == [202]


def test_update_low_confidence_file_append_unique(tmp_path: pathlib.Path):
    path = tmp_path / "low.txt"
    path.write_text("B0AAAAAAAA\nB0BBBBBBBB\n", encoding="utf-8")

    flagged = [
        {"asin": "B0BBBBBBBB", "researchable": True},   # 既存
        {"asin": "B0CCCCCCCC", "researchable": True},   # 新規
        {"asin": "B0DDDDDDDD", "researchable": False},  # 非 researchable は無視
    ]
    added = update_low_confidence_file(flagged, path)
    assert added == 1
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines == ["B0AAAAAAAA", "B0BBBBBBBB", "B0CCCCCCCC"]


def test_update_low_confidence_file_creates_parent(tmp_path: pathlib.Path):
    path = tmp_path / "raw" / "nested" / "low.txt"
    flagged = [{"asin": "B0XXXXXXXX", "researchable": True}]
    added = update_low_confidence_file(flagged, path)
    assert added == 1
    assert path.read_text(encoding="utf-8").strip() == "B0XXXXXXXX"


def test_aggregate_output_json_roundtrip(tmp_path: pathlib.Path):
    result = aggregate(
        [_issue(301, SAMPLE_BODY), _issue(302, SAMPLE_BODY)],
        threshold=2,
    )
    out = tmp_path / "counts.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert loaded["flagged"][0]["asin"] == "B0083AHXT0"
    assert "B0083AHXT0|rakuten" in loaded["counts_by_asin_ec"]
