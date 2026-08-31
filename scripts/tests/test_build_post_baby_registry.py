"""omcha-ops#19 P2 — 乳児帯の記事にだけ「らくらくベビー」導線を出す。

Bounty (登録 1 件の固定報酬) は商品紹介料と桁が違う一方、対象年齢の合わない
記事に出すと「クリックはあるが登録に至らない」導線が増えるだけになる。
判定の境界がここのテスト対象:

  - 「0ヶ月〜」と「対象年齢の記載なし」は _parse_age_min_months が
    どちらも 0 を返す。前者だけを通し、後者は落とす
  - 3 歳以上は出さない
  - URL は hugo/config.toml の [params].babyRegistryUrl (未設定は既定値)
  - tag= はここでは付けない (P1 の _force_amazon_partner_tag が 1 点で付ける)
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

THIS_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent.parent  # amazon-clone/
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from build_post import (  # type: ignore[import-not-found]
    _BABY_REGISTRY_URL_DEFAULT,
    _build_baby_registry_context,
    _force_amazon_partner_tag,
    _is_baby_targeted_age,
    _load_baby_registry_url,
)


class IsBabyTargetedAgeTests(unittest.TestCase):
    def test_infant_ranges_are_eligible(self):
        for raw in ("0ヶ月〜", "0歳〜", "0歳〜2歳", "3ヶ月〜", "6ヶ月以上",
                    "10ヶ月〜", "1歳〜", "1歳以上", "1歳半〜", "1.5歳以上",
                    "18ヶ月〜", "1歳6ヶ月〜"):
            with self.subTest(raw=raw):
                self.assertTrue(_is_baby_targeted_age(raw))

    def test_older_ages_are_not_eligible(self):
        for raw in ("2歳〜", "3歳以上", "3歳〜7歳", "6歳以上", "12歳〜"):
            with self.subTest(raw=raw):
                self.assertFalse(_is_baby_targeted_age(raw))

    def test_unknown_age_is_not_eligible(self):
        # ここが本丸: 月齢だけ見ると「対象年齢の記載なし」も 0 になり、
        # 乳児向けと同じ扱いで CTA が出てしまう。
        for raw in ("対象年齢の記載なし", "指定なし", "全年齢", "大人向け",
                    "幼児（受験期）", "", None):
            with self.subTest(raw=raw):
                self.assertFalse(_is_baby_targeted_age(raw))

    def test_boundary_is_18_months(self):
        self.assertTrue(_is_baby_targeted_age("18ヶ月〜"))
        self.assertFalse(_is_baby_targeted_age("19ヶ月〜"))


class BuildBabyRegistryContextTests(unittest.TestCase):
    def test_eligible_returns_url(self):
        ctx = _build_baby_registry_context("6ヶ月〜", _BABY_REGISTRY_URL_DEFAULT)
        self.assertEqual(ctx, {"url": _BABY_REGISTRY_URL_DEFAULT})

    def test_not_eligible_returns_none(self):
        self.assertIsNone(_build_baby_registry_context("3歳〜", _BABY_REGISTRY_URL_DEFAULT))

    def test_empty_url_disables_the_cta(self):
        self.assertIsNone(_build_baby_registry_context("6ヶ月〜", ""))


class LoadBabyRegistryUrlTests(unittest.TestCase):
    def _config(self, body: str) -> pathlib.Path:
        d = pathlib.Path(tempfile.mkdtemp())
        p = d / "config.toml"
        p.write_text(body, encoding="utf-8")
        return p

    def test_reads_param(self):
        p = self._config('[params]\nbabyRegistryUrl = "https://www.amazon.co.jp/baby-reg/x"\n')
        self.assertEqual(_load_baby_registry_url(p), "https://www.amazon.co.jp/baby-reg/x")

    def test_missing_param_falls_back_to_default(self):
        p = self._config('[params]\namazonPartnerTag = "x-22"\n')
        self.assertEqual(_load_baby_registry_url(p), _BABY_REGISTRY_URL_DEFAULT)

    def test_missing_file_falls_back_to_default(self):
        self.assertEqual(
            _load_baby_registry_url(pathlib.Path("does/not/exist.toml")),
            _BABY_REGISTRY_URL_DEFAULT,
        )

    def test_malformed_toml_falls_back_to_default(self):
        p = self._config("[params\nbroken")
        self.assertEqual(_load_baby_registry_url(p), _BABY_REGISTRY_URL_DEFAULT)


class RegistryUrlGetsTaggedTests(unittest.TestCase):
    """導線の URL も P1 の強制を通る (tag 無しで配信されない)。"""

    def test_tag_is_appended_by_the_single_enforcement_point(self):
        html = f'<a href="{_BABY_REGISTRY_URL_DEFAULT}" rel="sponsored">x</a>'
        out = _force_amazon_partner_tag(html, "chk01-22")
        self.assertIn("https://www.amazon.co.jp/baby-reg/?tag=chk01-22", out)


class ConfiguredUrlIsAmazonTests(unittest.TestCase):
    """config.toml の値が amazon.co.jp でなくなると tag 強制も計測も効かない。"""

    def test_committed_config_points_at_amazon(self):
        url = _load_baby_registry_url(REPO_ROOT / "hugo" / "config.toml")
        self.assertTrue(url.startswith("https://www.amazon.co.jp/"), url)


if __name__ == "__main__":
    unittest.main()
