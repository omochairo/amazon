"""score_claims / audit_score_claims / quality_gate.check_score_claim_consistency /
brand_normalizer.display_name のテスト (navi-brain#165 T1)。"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

THIS_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR.parent))

import audit_score_claims  # noqa: E402
import brand_normalizer  # noqa: E402
import quality_gate  # noqa: E402
import score_claims as sc  # noqa: E402
from score_calculator import calculate  # noqa: E402

LOW_SAFE = sc.ScoreView(total_100=70, axes={"safety": 2.0, "education": 3.0,
                                             "longevity": 3.0, "cost_performance": 3.0})
LOW_TOTAL = sc.ScoreView(total_100=58, axes={"safety": 4.7, "education": 3.0,
                                              "longevity": 3.0, "cost_performance": 3.0})
HIGH = sc.ScoreView(total_100=85, axes={"safety": 4.7, "education": 4.0,
                                         "longevity": 4.0, "cost_performance": 4.0})


def _art(**narrative):
    return {"slug": "2026-11-13-B0TESTCLM1", "narrative": narrative,
            "product": {"asin": "B0TESTCLM1", "brand": "Hueyaus"}}


class SafetyClaimTest(unittest.TestCase):
    def kinds(self, text, view=LOW_SAFE):
        return [(h.kind, h.label) for h in sc.find_claims(_art(safety_note=text), view)]

    def test_strong_claims_detected(self):
        for text in ("天然木を使っており安全性の高さも魅力です。",
                     "安全性が非常に高い設計です。",
                     "高い安全性を誇ります。",
                     "安全面でも安心な作りです。",
                     "安心・安全な素材です。",
                     "安心かつ安全な素材です。",
                     "赤ちゃんが口に入れても安心な素材です。",
                     "舐めても大丈夫な塗料を使っています。"):
            with self.subTest(text=text):
                self.assertTrue(self.kinds(text), text)

    def test_weak_or_generic_not_detected(self):
        for text in ("角が丸く安心して遊べます。",
                     "安全に配慮した設計です。",
                     "デザイン性と安全性を兼ね備えています。",
                     "安全性の高さを重視するご家庭には向いています。",
                     "安全性が高いものを選びたい方へ。",
                     "安全性が高いとは言えません。",
                     "安全性が高くないので注意が必要です。",
                     "安全性は高くはありません。",
                     "安全面で安心できません。",
                     "「口に入れても安全か」という点が気になります。",
                     "赤ちゃんが口に入れても安全な素材か？",
                     "口に入れても大丈夫か、すぐ飽きないかと悩む方も多いでしょう。"):
            with self.subTest(text=text):
                self.assertEqual(self.kinds(text), [], text)

    def test_threshold_boundary(self):
        text = "安全性の高さも魅力です。"
        at = sc.ScoreView(total_100=80, axes={"safety": 2.6})
        above = sc.ScoreView(total_100=80, axes={"safety": 2.7})
        self.assertTrue(sc.find_claims(_art(lead=text), at))
        self.assertEqual(sc.find_claims(_art(lead=text), above), [])

    def test_no_view_no_hits(self):
        self.assertEqual(sc.find_claims(_art(lead="安全性の高さも魅力です。"), None), [])


class RecommendClaimTest(unittest.TestCase):
    def test_detected_only_when_total_low(self):
        art = _art(closing=["総合的に、保護者の方には自信を持って推せる一品です。"])
        self.assertEqual([h.kind for h in sc.find_claims(art, LOW_TOTAL)], ["recommend_claim"])
        self.assertEqual(sc.find_claims(art, HIGH), [])
        at = sc.ScoreView(total_100=60, axes={"safety": 4.0})
        above = sc.ScoreView(total_100=61, axes={"safety": 4.0})
        self.assertTrue(sc.find_claims(art, at))
        self.assertEqual(sc.find_claims(art, above), [])

    def test_variants(self):
        for text in ("迷わずおすすめできる一品です。", "太鼓判を押します。",
                     "間違いなく買って損のない一品です。", "自信をもっておすすめできます。",
                     "文句なしにおすすめです。"):
            with self.subTest(text=text):
                self.assertTrue(sc.find_claims(_art(gift_appeal=text), LOW_TOTAL))

    def test_weak_not_detected(self):
        for text in ("おすすめの一品です。", "迷わず候補に入れたい商品です。",
                     "喜ぶこと間違いなしです。", "最高の状態で楽しめます。"):
            with self.subTest(text=text):
                self.assertEqual(sc.find_claims(_art(lead=text), LOW_TOTAL), [])


class ClosingReplacementTest(unittest.TestCase):
    def test_table_is_fixed(self):
        # 置き換えの対象と置換文を変えるときは、集計し直してこの表も直す
        self.assertEqual(sc.CLOSING_REPLACEMENTS, (
            ("自信を持って推せる一品です。", "検討する価値のある一品です。"),
            ("自信を持って強く推せる一品です。", "検討する価値のある一品です。"),
            ("自信を持っておすすめできる一品です。", "検討する価値のある一品です。"),
            ("自信を持って推せるアイテムです。", "検討する価値のあるアイテムです。"),
            ("自信を持っておすすめできます。", "候補の一つとして検討できます。"),
            ("迷わず推せる一品です。", "検討する価値のある一品です。"),
        ))

    def test_replacements_are_neutral(self):
        for old, new in sc.CLOSING_REPLACEMENTS:
            with self.subTest(old=old):
                self.assertTrue(sc.find_claims(_art(closing=old), LOW_TOTAL))
                self.assertEqual(sc.find_claims(_art(closing=new), LOW_TOTAL), [])

    def test_replaces_list_closing_when_low(self):
        art = _art(closing=["総合的に、初めての方には自信を持って推せる一品です。",
                            "最安は楽天です。"])
        n = sc.replace_low_score_closing(art, LOW_TOTAL)
        self.assertEqual(n, 1)
        self.assertEqual(art["narrative"]["closing"],
                         ["総合的に、初めての方には検討する価値のある一品です。", "最安は楽天です。"])

    def test_replaces_str_closing(self):
        art = _art(closing="プレゼントとして自信を持っておすすめできます。")
        self.assertEqual(sc.replace_low_score_closing(art, LOW_TOTAL), 1)
        self.assertEqual(art["narrative"]["closing"], "プレゼントとして候補の一つとして検討できます。")

    def test_untouched_when_high_or_other_section(self):
        art = _art(closing=["自信を持って推せる一品です。"],
                   gift_appeal=["自信を持って推せる一品です。"])
        self.assertEqual(sc.replace_low_score_closing(art, HIGH), 0)
        self.assertEqual(art["narrative"]["closing"], ["自信を持って推せる一品です。"])
        self.assertEqual(sc.replace_low_score_closing(art, LOW_TOTAL), 1)
        # closing 以外は書き換えない
        self.assertEqual(art["narrative"]["gift_appeal"], ["自信を持って推せる一品です。"])

    def test_non_exact_variant_untouched(self):
        art = _art(closing=["自信を持って推せる有力な候補です。"])
        self.assertEqual(sc.replace_low_score_closing(art, LOW_TOTAL), 0)


class ComputeViewTest(unittest.TestCase):
    def test_matches_score_calculator(self):
        art = {"product": {"asin": "B0TESTCLM1", "brand": "レゴ(LEGO)", "certifications": ["ST"],
                           "best_price": 3000, "target_age": "3歳以上"}}
        sr = calculate(art, brand_normalizer.normalize("レゴ(LEGO)"), asin="B0TESTCLM1")
        view = sc.compute_view(art)
        self.assertEqual(view.total_100, sr.total_100)
        self.assertEqual(view.safety, sc.view_from_result(sr).safety)

    def test_no_brand_no_score(self):
        self.assertIsNone(sc.compute_view({"product": {"asin": "B0TESTCLM1"}}))
        self.assertIsNone(sc.compute_view({}))


class QualityGateCheckTest(unittest.TestCase):
    def _data(self, closing, slug="2026-11-13-B0TESTCLM1"):
        return {"slug": slug, "narrative": {"closing": closing},
                "product": {"asin": "B0TESTCLM1", "brand": "Hueyaus"}}

    def test_warn_only_by_default(self):
        with mock.patch.object(sc, "compute_view", return_value=LOW_SAFE):
            r = quality_gate.check_score_claim_consistency(self._data(["安全性の高さも魅力です。"]))
        self.assertTrue(r.passed)
        self.assertTrue(r.warn)
        self.assertFalse(r.scored)
        self.assertIn("safety_claim", r.message)

    def test_closing_phrase_replaced_is_ok(self):
        with mock.patch.object(sc, "compute_view", return_value=LOW_TOTAL):
            r = quality_gate.check_score_claim_consistency(self._data(["自信を持って推せる一品です。"]))
        self.assertTrue(r.passed)
        self.assertFalse(r.warn)
        self.assertIn("置き換わる", r.message)

    def test_enforce_date_fails_new_slug_only(self):
        with mock.patch.object(sc, "compute_view", return_value=LOW_SAFE), \
                mock.patch.object(quality_gate, "SCORE_CLAIM_ENFORCE_FROM", "2026-11-13"):
            new = quality_gate.check_score_claim_consistency(self._data(["安全性の高さも魅力です。"]))
            old = quality_gate.check_score_claim_consistency(
                self._data(["安全性の高さも魅力です。"], slug="2026-11-12-B0TESTCLM1"))
        self.assertFalse(new.passed)
        self.assertTrue(old.passed)

    def test_uncomputable_is_warned(self):
        with mock.patch.object(sc, "compute_view", side_effect=RuntimeError("x")):
            r = quality_gate.check_score_claim_consistency(self._data(["x"]))
        self.assertTrue(r.passed)
        self.assertTrue(r.warn)
        self.assertIn("計算不能", r.message)

    def test_no_brand_skipped(self):
        r = quality_gate.check_score_claim_consistency({"product": {"asin": "B0TESTCLM1"}})
        self.assertTrue(r.passed)
        self.assertFalse(r.warn)

    def test_unscored_check_does_not_move_total(self):
        rep = quality_gate.ArticleReport(slug="s", path="p")
        rep.checks.append(quality_gate.CheckResult("a", True, 0.5))
        before = rep.total_score
        rep.checks.append(quality_gate.CheckResult("b", True, 1.0, scored=False))
        self.assertEqual(rep.total_score, before)


class DisplayNameTest(unittest.TestCase):
    def test_known_brand_is_canonical(self):
        self.assertEqual(brand_normalizer.display_name("レゴ(LEGO)"), "レゴ")

    def test_unknown_brand_keeps_raw(self):
        self.assertEqual(brand_normalizer.display_name(" Hueyaus "), "Hueyaus")

    def test_non_brand_words(self):
        for raw in ("", None, "ノーブランド", "ノーブランド品", "Generic", "generic",
                    "不明", "なし", "-", "—", "Unknown", "N/A", "不明 / 不明"):
            with self.subTest(raw=raw):
                self.assertEqual(brand_normalizer.display_name(raw), "ノーブランド")

    def test_classification_unchanged(self):
        nb = brand_normalizer.normalize("Hueyaus")
        self.assertEqual((nb.canonical, nb.tier, nb.exclude_from_taxonomy),
                         ("ノーブランド", "D", True))


class AuditTest(unittest.TestCase):
    def test_counts_before_and_after_replacement(self):
        with tempfile.TemporaryDirectory() as td:
            src = pathlib.Path(td)
            arts = {
                "2026-11-01-B0TESTAUD1": {"closing": ["自信を持って推せる一品です。"]},
                "2026-11-01-B0TESTAUD2": {"safety_note": ["安全性の高さも魅力です。"]},
                # 古い本文は数えない (build は ASIN ごとに最新だけ描画する)
                "2026-10-01-B0TESTAUD2": {"safety_note": ["安全性の高さも魅力です。"] * 3},
            }
            for slug, nar in arts.items():
                (src / f"{slug}.json").write_text(json.dumps(
                    {"slug": slug, "narrative": nar,
                     "product": {"asin": slug[-10:], "brand": "Hueyaus"}},
                    ensure_ascii=False), encoding="utf-8")
            (src / "2026-11-01-B0TESTAUD1.seo.json").write_text("{}", encoding="utf-8")
            with mock.patch.object(sc, "compute_view",
                                   return_value=sc.ScoreView(58, {"safety": 2.0})):
                res = audit_score_claims.audit(src)
        t = res["totals"]
        self.assertEqual(t["articles"], 2)
        self.assertEqual(t["recommend_claim:before"], 1)
        self.assertEqual(t.get("recommend_claim:after", 0), 0)
        self.assertEqual(t["safety_claim:after"], 1)
        self.assertEqual(t["closing_replaced_articles"], 1)
        self.assertEqual(t["any:after"], 1)


if __name__ == "__main__":
    unittest.main()
