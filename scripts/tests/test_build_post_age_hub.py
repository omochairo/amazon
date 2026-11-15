"""build_post front matter uses the shared age hub classifier."""

from pathlib import Path

from build_post import _frontmatter_meta


def _meta(target_age):
    data = {
        "title": "対象年齢テスト",
        "date": "2026-01-01T00:00:00+09:00",
        "product": {"name": "テスト玩具", "asin": "B0AGE000001", "target_age": target_age},
    }
    return _frontmatter_meta(data, "2026-01-01-B0AGE000001", False, {}, Path("test.json"))


def test_age_hub_boundaries():
    for month, expected in [(11, "age-0"), (12, "age-1"), (47, "age-3"),
                            (48, "age-4"), (71, "age-4"), (72, "age-6")]:
        assert _meta(f"{month}ヶ月")["age_hub"] == expected


def test_unknown_age_omits_key():
    meta = _meta("")
    assert "age_hub" not in meta
