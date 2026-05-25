"""Unit tests for build_feature_lists.py (Issue #590 PR-1).

Coverage:
1. load_articles  - parses article json, skips .quality.json / .enrichment.json / .seo.json,
                    skips records missing asin / ivs_score / best_price.
2. attach_amazon_meta - fills savings_percentage + fetched_at from per_asin/<ASIN>/amazon.json,
                        silently skips missing files.
3. build_cospa    - low_ivs / price band / sort order / top_n / dedupe by asin.
4. build_deals    - stale guard / missing savings data / sort by savings then ivs / top_n.
5. serialize_*    - url_internal is lowercased; cospa includes score_cospa; deals includes
                    savings_percentage + fetched_at.
6. run (end-to-end on a tmp fixture) - writes both hugo outputs + manifest.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import build_feature_lists as bfl  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _make_article(
    asin: str,
    *,
    slug: str | None = None,
    name: str = "テスト商品",
    ivs_score: float = 4.5,
    ivs_100: int = 90,
    best_price: int = 2000,
    best_platform: str = "Amazon",
) -> dict:
    return {
        "slug": slug or f"2026-05-13-{asin.lower()}",
        "title": f"{name} 徹底レビュー",
        "product": {
            "asin": asin,
            "name": name,
            "image": f"https://example.invalid/{asin}.jpg",
            "ivs_score": ivs_score,
            "ivs_detail": {"total_100": ivs_100},
            "best_price": best_price,
            "best_platform": best_platform,
            "prices": {
                "amazon": {
                    "price": best_price,
                    "url": f"https://www.amazon.co.jp/dp/{asin}/?tag=test-22",
                }
            },
        },
    }


def _write_article(articles_dir: pathlib.Path, payload: dict) -> pathlib.Path:
    path = articles_dir / f"{payload['slug']}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _write_per_asin(
    per_asin_dir: pathlib.Path,
    asin: str,
    *,
    savings: int | None,
    fetched_at: str | None,
) -> None:
    dir_ = per_asin_dir / asin
    dir_.mkdir(parents=True, exist_ok=True)
    body: dict = {
        "asin": asin,
        "fetched_at": fetched_at,
        "item": {"asin": asin, "savings_percentage": savings},
    }
    (dir_ / "amazon.json").write_text(
        json.dumps(body, ensure_ascii=False), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# load_articles
# ---------------------------------------------------------------------------

class LoadArticlesTest(unittest.TestCase):
    def test_loads_only_article_json(self):
        with tempfile.TemporaryDirectory() as td:
            d = pathlib.Path(td)
            _write_article(d, _make_article("B00000001"))
            # sidecar files that should be ignored:
            (d / "2026-05-13-b00000001.quality.json").write_text("{}", encoding="utf-8")
            (d / "2026-05-13-b00000001.enrichment.json").write_text("{}", encoding="utf-8")
            (d / "2026-05-13-b00000001.seo.json").write_text("{}", encoding="utf-8")

            records = bfl.load_articles(d)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].asin, "B00000001")

    def test_skips_missing_required_fields(self):
        with tempfile.TemporaryDirectory() as td:
            d = pathlib.Path(td)
            # missing best_price
            bad = _make_article("B00000002")
            bad["product"].pop("best_price")
            _write_article(d, bad)
            # missing asin
            bad2 = _make_article("B00000003")
            bad2["product"].pop("asin")
            _write_article(d, bad2)
            # good
            _write_article(d, _make_article("B00000004"))

            records = bfl.load_articles(d)
            self.assertEqual([r.asin for r in records], ["B00000004"])

    def test_unreadable_json_is_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            d = pathlib.Path(td)
            (d / "broken.json").write_text("{not json", encoding="utf-8")
            _write_article(d, _make_article("B00000005"))
            records = bfl.load_articles(d)
            self.assertEqual([r.asin for r in records], ["B00000005"])

    def test_missing_dir_returns_empty(self):
        records = bfl.load_articles(pathlib.Path("/does/not/exist/articles"))
        self.assertEqual(records, [])


# ---------------------------------------------------------------------------
# attach_amazon_meta
# ---------------------------------------------------------------------------

class AttachAmazonMetaTest(unittest.TestCase):
    def test_fills_savings_and_fetched_at(self):
        with tempfile.TemporaryDirectory() as td:
            d = pathlib.Path(td)
            arts = d / "articles"
            arts.mkdir()
            per = d / "per_asin"
            _write_article(arts, _make_article("B00000010"))
            _write_per_asin(
                per, "B00000010", savings=30, fetched_at="2026-05-24T10:00:00+00:00"
            )

            records = bfl.load_articles(arts)
            bfl.attach_amazon_meta(records, per)
            self.assertEqual(records[0].savings_percentage, 30)
            self.assertEqual(records[0].fetched_at, "2026-05-24T10:00:00+00:00")

    def test_missing_per_asin_is_silent(self):
        with tempfile.TemporaryDirectory() as td:
            d = pathlib.Path(td)
            arts = d / "articles"
            arts.mkdir()
            per = d / "per_asin"  # never created
            _write_article(arts, _make_article("B00000011"))
            records = bfl.load_articles(arts)
            bfl.attach_amazon_meta(records, per)
            self.assertIsNone(records[0].savings_percentage)
            self.assertIsNone(records[0].fetched_at)


# ---------------------------------------------------------------------------
# build_cospa
# ---------------------------------------------------------------------------

class BuildCospaTest(unittest.TestCase):
    def test_drops_low_ivs(self):
        recs = [
            bfl.ArticleRecord(
                asin="A1", slug="a1", name="lo", image=None,
                ivs_score=3.9, ivs_100=78, best_price=2000,
                best_platform="Amazon", amazon_url=None,
            ),
            bfl.ArticleRecord(
                asin="A2", slug="a2", name="ok", image=None,
                ivs_score=4.0, ivs_100=80, best_price=2000,
                best_platform="Amazon", amazon_url=None,
            ),
        ]
        items, drops = bfl.build_cospa(recs)
        self.assertEqual([r.asin for r in items], ["A2"])
        self.assertEqual(drops["low_ivs"], 1)

    def test_price_band(self):
        def rec(asin, price):
            return bfl.ArticleRecord(
                asin=asin, slug=asin.lower(), name="x", image=None,
                ivs_score=4.5, ivs_100=90, best_price=price,
                best_platform="Amazon", amazon_url=None,
            )

        items, drops = bfl.build_cospa(
            [rec("A1", 400), rec("A2", 500), rec("A3", 5000), rec("A4", 5001)],
            price_min=500, price_max=5000,
        )
        self.assertEqual({r.asin for r in items}, {"A2", "A3"})
        self.assertEqual(drops["price_out_of_band"], 2)

    def test_sort_order_by_cospa_score(self):
        # Higher IVS / cheaper price ranks first.
        recs = [
            bfl.ArticleRecord(
                asin="HI", slug="hi", name="hi", image=None,
                ivs_score=5.0, ivs_100=100, best_price=800,
                best_platform="Amazon", amazon_url=None,
            ),
            bfl.ArticleRecord(
                asin="MID", slug="mid", name="mid", image=None,
                ivs_score=4.5, ivs_100=90, best_price=2000,
                best_platform="Amazon", amazon_url=None,
            ),
            bfl.ArticleRecord(
                asin="LO", slug="lo", name="lo", image=None,
                ivs_score=4.0, ivs_100=80, best_price=4500,
                best_platform="Amazon", amazon_url=None,
            ),
        ]
        items, _ = bfl.build_cospa(recs)
        self.assertEqual([r.asin for r in items], ["HI", "MID", "LO"])

    def test_top_n_truncates(self):
        recs = [
            bfl.ArticleRecord(
                asin=f"A{i}", slug=f"a{i}", name="x", image=None,
                ivs_score=4.5, ivs_100=90 - i, best_price=2000,
                best_platform="Amazon", amazon_url=None,
            )
            for i in range(10)
        ]
        items, _ = bfl.build_cospa(recs, top_n=3)
        self.assertEqual(len(items), 3)

    def test_dedupe_by_asin_keeps_higher_ivs(self):
        recs = [
            bfl.ArticleRecord(
                asin="DUP", slug="old", name="old", image=None,
                ivs_score=4.0, ivs_100=80, best_price=2000,
                best_platform="Amazon", amazon_url=None,
            ),
            bfl.ArticleRecord(
                asin="DUP", slug="new", name="new", image=None,
                ivs_score=4.8, ivs_100=96, best_price=2000,
                best_platform="Amazon", amazon_url=None,
            ),
        ]
        items, _ = bfl.build_cospa(recs)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].slug, "new")


# ---------------------------------------------------------------------------
# build_deals
# ---------------------------------------------------------------------------

class BuildDealsTest(unittest.TestCase):
    def _rec(self, asin, savings, fetched_at, *, ivs_score=4.5, ivs_100=90):
        return bfl.ArticleRecord(
            asin=asin, slug=asin.lower(), name="x", image=None,
            ivs_score=ivs_score, ivs_100=ivs_100, best_price=2000,
            best_platform="Amazon", amazon_url=None,
            savings_percentage=savings, fetched_at=fetched_at,
        )

    def test_drops_low_ivs(self):
        now = datetime(2026, 5, 25, tzinfo=timezone.utc)
        fresh = (now - timedelta(days=1)).isoformat()
        items, drops = bfl.build_deals(
            [self._rec("A1", 40, fresh, ivs_score=3.9)],
            now=now,
        )
        self.assertEqual(items, [])
        self.assertEqual(drops["low_ivs"], 1)

    def test_drops_missing_savings_data(self):
        now = datetime(2026, 5, 25, tzinfo=timezone.utc)
        fresh = (now - timedelta(days=1)).isoformat()
        items, drops = bfl.build_deals(
            [self._rec("A1", None, fresh)],
            now=now,
        )
        self.assertEqual(items, [])
        self.assertEqual(drops["no_savings_data"], 1)

    def test_drops_below_threshold(self):
        now = datetime(2026, 5, 25, tzinfo=timezone.utc)
        fresh = (now - timedelta(days=1)).isoformat()
        items, drops = bfl.build_deals(
            [self._rec("A1", 19, fresh)],
            now=now,
            min_savings=20,
        )
        self.assertEqual(items, [])
        self.assertEqual(drops["savings_below_threshold"], 1)

    def test_stale_guard(self):
        now = datetime(2026, 5, 25, tzinfo=timezone.utc)
        stale = (now - timedelta(days=15)).isoformat()
        fresh = (now - timedelta(days=1)).isoformat()
        items, drops = bfl.build_deals(
            [
                self._rec("STALE", 50, stale),
                self._rec("FRESH", 30, fresh),
            ],
            now=now,
            stale_days=14,
        )
        self.assertEqual([r.asin for r in items], ["FRESH"])
        self.assertEqual(drops["stale_or_unknown_fetch"], 1)

    def test_none_fetched_at_drops(self):
        now = datetime(2026, 5, 25, tzinfo=timezone.utc)
        items, drops = bfl.build_deals(
            [self._rec("A1", 40, None)],
            now=now,
        )
        self.assertEqual(items, [])
        self.assertEqual(drops["stale_or_unknown_fetch"], 1)

    def test_sort_savings_then_ivs(self):
        now = datetime(2026, 5, 25, tzinfo=timezone.utc)
        fresh = (now - timedelta(days=1)).isoformat()
        recs = [
            self._rec("A_TIE_LO", 40, fresh, ivs_100=80),
            self._rec("HIGH", 50, fresh, ivs_100=82),
            self._rec("A_TIE_HI", 40, fresh, ivs_100=95),
        ]
        items, _ = bfl.build_deals(recs, now=now)
        self.assertEqual([r.asin for r in items], ["HIGH", "A_TIE_HI", "A_TIE_LO"])

    def test_top_n(self):
        now = datetime(2026, 5, 25, tzinfo=timezone.utc)
        fresh = (now - timedelta(days=1)).isoformat()
        recs = [
            self._rec(f"A{i}", 30 + i, fresh) for i in range(10)
        ]
        items, _ = bfl.build_deals(recs, now=now, top_n=2)
        self.assertEqual(len(items), 2)


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

class SerializeTest(unittest.TestCase):
    def test_cospa_url_lowercased_and_score_present(self):
        rec = bfl.ArticleRecord(
            asin="B00ABCXYZ", slug="2026-05-13-B00ABCXYZ", name="x", image=None,
            ivs_score=4.6, ivs_100=92, best_price=1500,
            best_platform="Amazon", amazon_url="https://example/dp/B00ABCXYZ",
        )
        rec.score_cospa = 0.333333
        payload = bfl.serialize_cospa(
            [rec], filter_params={}, generated_at="2026-05-25T00:00:00Z"
        )
        self.assertEqual(payload["type"], "cospa")
        self.assertEqual(payload["count"], 1)
        entry = payload["items"][0]
        # Hugo lowercases URLs - slug.lower() must be applied (memory trap).
        self.assertEqual(entry["url_internal"], "/posts/2026-05-13-b00abcxyz/")
        self.assertEqual(entry["score_cospa"], 0.3333)  # rounded to 4dp
        self.assertNotIn("savings_percentage", entry)

    def test_deals_includes_savings_and_fetched_at(self):
        rec = bfl.ArticleRecord(
            asin="B00DEFGHI", slug="2026-05-13-B00DEFGHI", name="x", image=None,
            ivs_score=4.5, ivs_100=90, best_price=2000,
            best_platform="Amazon", amazon_url=None,
            savings_percentage=35, fetched_at="2026-05-24T10:00:00+00:00",
        )
        payload = bfl.serialize_deals(
            [rec], filter_params={}, generated_at="2026-05-25T00:00:00Z"
        )
        entry = payload["items"][0]
        self.assertEqual(entry["savings_percentage"], 35)
        self.assertEqual(entry["fetched_at"], "2026-05-24T10:00:00+00:00")
        self.assertNotIn("score_cospa", entry)


# ---------------------------------------------------------------------------
# End-to-end run()
# ---------------------------------------------------------------------------

class RunEndToEndTest(unittest.TestCase):
    def test_writes_all_three_outputs(self):
        with tempfile.TemporaryDirectory() as td:
            d = pathlib.Path(td)
            arts = d / "articles"
            arts.mkdir()
            per = d / "per_asin"
            out_hugo = d / "hugo_features"
            out_manifest = d / "manifest" / "_build_manifest.json"

            # Article 1: cospa win (high IVS, cheap), no savings
            _write_article(arts, _make_article(
                "B0AAA1", ivs_score=4.8, ivs_100=96, best_price=1200,
            ))
            # Article 2: deals win (moderate IVS, big discount, fresh)
            _write_article(arts, _make_article(
                "B0BBB2", ivs_score=4.2, ivs_100=84, best_price=3000,
            ))
            now = datetime(2026, 5, 25, tzinfo=timezone.utc)
            _write_per_asin(
                per, "B0BBB2",
                savings=40, fetched_at=(now - timedelta(days=2)).isoformat(),
            )
            # Article 3: stale discount (should be dropped from deals)
            _write_article(arts, _make_article(
                "B0CCC3", ivs_score=4.4, ivs_100=88, best_price=2500,
            ))
            _write_per_asin(
                per, "B0CCC3",
                savings=50, fetched_at=(now - timedelta(days=60)).isoformat(),
            )

            manifest = bfl.run(
                articles_dir=arts,
                per_asin_dir=per,
                out_hugo=out_hugo,
                out_manifest=out_manifest,
                now=now,
            )

            cospa = json.loads((out_hugo / "cospa.json").read_text(encoding="utf-8"))
            deals = json.loads((out_hugo / "deals.json").read_text(encoding="utf-8"))
            saved_manifest = json.loads(out_manifest.read_text(encoding="utf-8"))

            # cospa: all 3 articles qualify (ivs>=4.0, price in band), TOP1 = B0AAA1
            self.assertEqual(cospa["count"], 3)
            self.assertEqual(cospa["items"][0]["asin"], "B0AAA1")

            # deals: only B0BBB2 (B0CCC3 stale, B0AAA1 no savings data)
            self.assertEqual(deals["count"], 1)
            self.assertEqual(deals["items"][0]["asin"], "B0BBB2")
            self.assertEqual(deals["items"][0]["savings_percentage"], 40)

            # manifest accounting
            self.assertEqual(saved_manifest["articles_loaded"], 3)
            self.assertEqual(saved_manifest["deals"]["drops"]["no_savings_data"], 1)
            self.assertEqual(
                saved_manifest["deals"]["drops"]["stale_or_unknown_fetch"], 1
            )
            self.assertEqual(manifest["cospa"]["count"], 3)


if __name__ == "__main__":
    unittest.main()
