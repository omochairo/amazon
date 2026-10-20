"""scripts/open_query_intent_issues.py unit tests (A-7, epic #1356 / brain#111)。

外部 gh CLI 呼び出しはせず、起票の要否と本文だけを検証する。
主眼は「cta_layout が自動で当たる商品ページを毎週起票し直さない」こと。
"""
from __future__ import annotations

from scripts.open_query_intent_issues import is_auto_applied, render_body, should_file

PRODUCT = "https://navi.omcha.jp/products/b0h86rpr71/"
HUB = "https://navi.omcha.jp/english-toys/"
RANGE = {"start": "2026-10-01", "end": "2026-10-08"}


def _row(page, intent="informational", first_detected="2026-10-08"):
    return {
        "page": page,
        "dominant_intent": intent,
        "dominant_share": 0.95,
        "total_impressions": 56,
        "intent_breakdown": {intent: 53},
        "top_queries": [],
        "cta_recommendation": "推奨",
        "first_detected": first_detected,
    }


def test_product_page_with_layout_intent_is_auto_applied():
    assert is_auto_applied(_row(PRODUCT, "informational"))
    assert is_auto_applied(_row(PRODUCT, "commercial"))


def test_hub_and_navigational_are_not_auto_applied():
    # build_post が照合するのは /products/ 配下だけ。navigational は map に入らない
    assert not is_auto_applied(_row(HUB, "commercial"))
    assert not is_auto_applied(_row(PRODUCT, "navigational"))


def test_product_page_is_filed_only_in_its_first_week():
    assert should_file(_row(PRODUCT, first_detected="2026-10-08"), src_range=RANGE)
    assert not should_file(_row(PRODUCT, first_detected="2026-09-03"), src_range=RANGE)


def test_product_page_without_provenance_stays_quiet():
    assert not should_file(_row(PRODUCT, first_detected=None), src_range=RANGE)
    assert not should_file(_row(PRODUCT), src_range={})


def test_pages_outside_auto_apply_are_filed_every_week():
    assert should_file(_row(HUB, "commercial", first_detected="2026-09-10"), src_range=RANGE)
    assert should_file(_row(PRODUCT, "navigational", first_detected="2026-09-10"),
                       src_range=RANGE)


def test_carried_forward_rows_are_never_filed():
    # brain#127: 持ち越しの数字は last_confirmed の週のもの。今週の観察期間として
    # 起票すると古い数字を今週の実測と読ませる。hub でも初検出の週でも黙る
    hub = {**_row(HUB, "commercial", first_detected="2026-08-13"), "carried_forward": True}
    assert not should_file(hub, src_range=RANGE)
    product = {**_row(PRODUCT), "carried_forward": True}
    assert not should_file(product, src_range=RANGE)
    assert should_file({**hub, "carried_forward": False}, src_range=RANGE)


def test_body_says_auto_applied_only_for_product_pages():
    note = "自動で当たる"
    assert note in render_body(_row(PRODUCT), src_range=RANGE)
    assert note not in render_body(_row(HUB, "commercial"), src_range=RANGE)


def test_body_does_not_promise_refiling_for_product_pages():
    # 商品ページは初検出の週しか起票しないので「次週の run が立て直す」は嘘になる
    refile = "次週の run が"
    product = render_body(_row(PRODUCT), src_range=RANGE)
    assert refile not in product
    assert "有効期限" in product
    assert refile in render_body(_row(HUB, "commercial"), src_range=RANGE)


def test_intent_change_keeps_original_first_detected_and_stays_quiet():
    # detect は意図が変わっても first_detected を引き継ぐ。新しい意図も
    # build_post が自動で当てるので、起票し直さなくても取りこぼしにはならない
    row = _row(PRODUCT, "commercial", first_detected="2026-09-10")
    assert is_auto_applied(row)
    assert not should_file(row, src_range=RANGE)
