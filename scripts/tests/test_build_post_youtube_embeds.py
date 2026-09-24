"""youtube_embeds の要素が必ず dict であることを保証する回帰テスト。

背景: 2026-07-14-B097BMF5DN.md の「🎬 関連動画」に

    - [<built-in method title of str object at 0x000001DF4B44E550>]()

が焼き込まれていた。原因は data/articles の ``youtube_embeds`` に dict では
なく **裸の URL 文字列** が1件入っていたこと。Jinja2 の ``a.b`` は「まず
getattr、無ければ getitem」の順で解決するため、要素が str だと
``vid.title`` が ``str.title`` (束縛メソッド) に解決されてそのまま str() され、
``vid.url`` は str に属性が無く getitem も TypeError なので Undefined (空文字)
になる。テンプレ側は例外を出さないので、テストが無いと生成物まで素通りする。

ここでは (1) _normalize_youtube_embeds の単体挙動と、(2) build_post.py を
実走させたエンドツーエンドの生成 markdown の両方を見る。単体だけだと
「テンプレが dict 以外をどう描くか」を踏めないため。
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

THIS_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent.parent  # amazon-clone/
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from build_post import _normalize_youtube_embeds  # type: ignore[import-not-found]

_ASIN = "B0TESTYT01"
_URL = "https://www.youtube.com/watch?v=0eNXLHTnkIU"


class NormalizeYoutubeEmbedsTests(unittest.TestCase):
    def test_bare_youtube_url_string_is_dropped_not_rescued(self):
        # B097BMF5DN の実データそのもの。title が無いので関連度判定
        # (title で見る) を受けていない。救済せずに落とす。
        data = {"youtube_embeds": [_URL]}
        dropped = _normalize_youtube_embeds(data)
        self.assertEqual(dropped, 1)
        self.assertEqual(data["youtube_embeds"], [])

    def test_every_item_is_a_dict_after_normalize(self):
        data = {"youtube_embeds": [_URL, {"title": "t", "url": _URL}, None, 42, ["x"]]}
        _normalize_youtube_embeds(data)
        for item in data["youtube_embeds"]:
            self.assertIsInstance(item, dict)

    def test_every_string_form_is_dropped(self):
        data = {"youtube_embeds": [
            "https://youtu.be/abc123", "HTTPS://WWW.YOUTUBE.COM/watch?v=z",
            "https://example.com/watch?v=1", "ただの文字列",
        ]}
        dropped = _normalize_youtube_embeds(data)
        self.assertEqual(dropped, 4)
        self.assertEqual(data["youtube_embeds"], [])

    def test_bare_string_is_dropped_but_dict_neighbour_is_kept(self):
        good = {"title": "レビュー動画", "url": _URL}
        data = {"youtube_embeds": [_URL, dict(good)]}
        _normalize_youtube_embeds(data)
        self.assertEqual(data["youtube_embeds"], [good])

    def test_dict_without_url_or_embed_html_is_dropped(self):
        data = {"youtube_embeds": [{"title": "タイトルだけ"}]}
        dropped = _normalize_youtube_embeds(data)
        self.assertEqual(dropped, 1)
        self.assertEqual(data["youtube_embeds"], [])

    def test_dict_with_embed_html_only_is_kept(self):
        data = {"youtube_embeds": [{"title": "t", "embed_html": "<iframe></iframe>"}]}
        _normalize_youtube_embeds(data)
        self.assertEqual(len(data["youtube_embeds"]), 1)

    def test_non_string_title_is_coerced_not_left_as_object(self):
        # dict ではあるが title が None / 数値のケース。テンプレは str を期待する。
        data = {"youtube_embeds": [{"title": None, "url": _URL}, {"title": 7, "url": _URL}]}
        _normalize_youtube_embeds(data)
        self.assertEqual(data["youtube_embeds"][0]["title"], "")
        self.assertEqual(data["youtube_embeds"][1]["title"], "7")

    def test_good_payload_is_untouched(self):
        good = [{"title": "レビュー動画", "url": _URL, "thumbnail": "https://i/y.jpg"}]
        data = {"youtube_embeds": [dict(g) for g in good]}
        dropped = _normalize_youtube_embeds(data)
        self.assertEqual(dropped, 0)
        self.assertEqual(data["youtube_embeds"], good)

    def test_non_list_value_is_reset_to_empty_list(self):
        data = {"youtube_embeds": "まるごと文字列"}
        _normalize_youtube_embeds(data)
        self.assertEqual(data["youtube_embeds"], [])

    def test_missing_key_is_a_noop(self):
        data: dict = {}
        self.assertEqual(_normalize_youtube_embeds(data), 0)
        self.assertNotIn("youtube_embeds", data)


def _make_article(youtube_embeds: list) -> dict:
    return {
        "slug": f"2026-01-01-{_ASIN}",
        "title": f"テスト商品 ({_ASIN})",
        "product": {
            "asin": _ASIN,
            "brand": "Test",
            "name": "テスト商品",
            "best_price": 1900,
            "best_platform": "amazon",
            "prices": {
                "amazon": {
                    "price": 1900,
                    "url": f"https://www.amazon.co.jp/dp/{_ASIN}",
                    "availability": "在庫あり。",
                    "discontinued": False,
                    "savings_percentage": 0,
                    "loyalty_points": 0,
                    "free_shipping": False,
                    "search_url": None,
                },
            },
        },
        "narrative": {"lead": "", "why_this_product": "", "gift_appeal": "",
                      "daily_use": "", "safety_note": "", "closing": ""},
        "persona_fit": {},
        "faq": [],
        "keywords": [],
        "youtube_embeds": youtube_embeds,
    }


class YoutubeEmbedsRenderTest(unittest.TestCase):
    """build_post.py をサブプロセスで実走し、生成 markdown を直に見る。"""

    def _render(self, youtube_embeds: list, per_asin_youtube: dict | None = None) -> str:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp_path = pathlib.Path(tmp.name)
        src = tmp_path / "articles"
        dst = tmp_path / "posts"
        src.mkdir()
        dst.mkdir()
        data_dir = tmp_path / "data"
        per_asin_root = data_dir / "raw" / "per_asin"
        (per_asin_root / _ASIN).mkdir(parents=True)
        (data_dir / "raw" / "amazon.json").write_text('{"items": []}', encoding="utf-8")
        if per_asin_youtube is not None:
            (per_asin_root / _ASIN / "youtube.json").write_text(
                json.dumps(per_asin_youtube, ensure_ascii=False), encoding="utf-8"
            )
        (src / f"2026-01-01-{_ASIN}.json").write_text(
            json.dumps(_make_article(youtube_embeds), ensure_ascii=False), encoding="utf-8"
        )

        cfg_dir = tmp_path / "hugo"
        cfg_dir.mkdir()
        (cfg_dir / "config.toml").write_text(
            'baseURL = "https://example.com/"\n'
            "[params]\n"
            '  amazonPartnerTag = "chk01-22"\n',
            encoding="utf-8",
        )
        (tmp_path / "scripts").mkdir(exist_ok=True)
        shutil.copytree(SCRIPTS_DIR / "templates", tmp_path / "scripts" / "templates")

        env = os.environ.copy()
        env.pop("GITHUB_STEP_SUMMARY", None)
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "build_post.py"),
             "--src", str(src) + os.sep,
             "--dst", str(dst) + os.sep,
             "--raw-amazon", str(data_dir / "raw" / "amazon.json"),
             "--per-asin-root", str(per_asin_root)],
            cwd=str(tmp_path), env=env,
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(proc.returncode, 0, msg=f"stdout={proc.stdout}\nstderr={proc.stderr}")
        out_files = list(dst.glob("*.md"))
        self.assertEqual(len(out_files), 1, msg=f"dst listing: {list(dst.iterdir())}")
        return out_files[0].read_text(encoding="utf-8")

    def _assert_no_python_repr(self, content: str) -> None:
        # 束縛メソッド / 生オブジェクトの repr が本文に漏れていないこと。
        for marker in ("built-in method", "<method ", " object at 0x", "<bound method"):
            self.assertNotIn(marker, content, msg=f"{marker!r} leaked into rendered markdown")

    def test_bare_url_string_is_not_rendered_as_a_bound_method(self):
        # B097BMF5DN の再現。per_asin 側も空 = 補完されないので節ごと消える。
        content = self._render([_URL], per_asin_youtube={"items": []})
        self._assert_no_python_repr(content)
        self.assertNotIn("]()", content)
        self.assertNotIn("0eNXLHTnkIU", content)
        self.assertNotIn("関連動画", content)

    def test_bare_url_string_falls_through_to_per_asin_fallback(self):
        # 全要素が落ちたら「Jules 由来は無い」扱いで per_asin から補完される。
        content = self._render(
            [_URL],
            per_asin_youtube={"items": [
                {"title": "判定済みの動画", "url": "https://www.youtube.com/watch?v=abcDEF12345"},
            ]},
        )
        self._assert_no_python_repr(content)
        self.assertIn("youtube-nocookie.com/embed/abcDEF12345", content)
        self.assertIn('<p class="yt-caption">判定済みの動画</p>', content)
        self.assertNotIn("0eNXLHTnkIU", content)

    def test_dict_without_title_renders_iframe_without_empty_caption(self):
        content = self._render([{"url": _URL}])
        self._assert_no_python_repr(content)
        self.assertIn("youtube-nocookie.com/embed/0eNXLHTnkIU", content)
        self.assertNotIn('<p class="yt-caption"></p>', content)

    def test_junk_only_list_drops_the_whole_section(self):
        content = self._render([None, 42, "ただの文字列"], per_asin_youtube={"items": []})
        self._assert_no_python_repr(content)
        self.assertNotIn("関連動画", content)

    def test_normal_payload_still_renders_title_and_iframe(self):
        content = self._render([{"title": "レビュー動画", "url": _URL}])
        self._assert_no_python_repr(content)
        self.assertIn("youtube-nocookie.com/embed/0eNXLHTnkIU", content)
        self.assertIn('<p class="yt-caption">レビュー動画</p>', content)

    def test_title_only_fallback_item_is_excluded_from_embeds(self):
        # #8162 案 A: title_only タグの付いた per_asin フォールバック項目は
        # 公開記事に自動埋め込みしない。裏付け無しの項目と裏付けありの項目が
        # 混ざっていても、裏付けありだけが載る。
        content = self._render(
            [],
            per_asin_youtube={"items": [
                {"title": "一般語の別商品", "url": "https://www.youtube.com/watch?v=titleonly01",
                 "_match": "title_only"},
                {"title": "判定済みの動画", "url": "https://www.youtube.com/watch?v=abcDEF12345"},
            ]},
        )
        self._assert_no_python_repr(content)
        self.assertIn("youtube-nocookie.com/embed/abcDEF12345", content)
        self.assertNotIn("titleonly01", content)
        self.assertNotIn("一般語の別商品", content)

    def test_all_title_only_fallback_items_drop_the_section(self):
        content = self._render(
            [],
            per_asin_youtube={"items": [
                {"title": "一般語の別商品", "url": "https://www.youtube.com/watch?v=titleonly01",
                 "_match": "title_only"},
            ]},
        )
        self._assert_no_python_repr(content)
        self.assertNotIn("titleonly01", content)
        self.assertNotIn("関連動画", content)


if __name__ == "__main__":
    unittest.main()


class ArticleSchemaMediaItemTests(unittest.TestCase):
    """真因側の防御: Jules の記事 PR は quality_gate の schema check を通って
    自動マージされる。youtube_embeds / news が ``{"type": "array"}`` だけだった
    ため、下の4形はすべて validate SUCCESS で main に入っていた (いずれも
    2026-07 の Jules PR で実際に書かれた形)。
    """

    @classmethod
    def setUpClass(cls):
        from jsonschema import Draft7Validator
        schema = json.loads((REPO_ROOT / "data" / "schema" / "article.schema.json")
                            .read_text(encoding="utf-8"))
        cls.validator = Draft7Validator(schema)

    def _errors(self, field: str, items: list) -> list:
        return [e for e in self.validator.iter_errors({field: items})
                if e.path and e.path[0] == field]

    def test_historical_bad_youtube_shapes_are_rejected(self):
        for bad in (
            _URL,                                                    # B097BMF5DN
            {"video_id": "L1ZtdcRkO5A", "title": "t"},               # B08WRF3PGC
            {"id": "8v1WxlJFq7o", "title": "t", "thumbnailUrl": "x"},  # B0894MVK59
            {"title": "t", "url": "https://example.com/watch?v=1"},
            {"title": "", "url": _URL},
        ):
            with self.subTest(bad=bad):
                self.assertTrue(self._errors("youtube_embeds", [bad]))

    def test_canonical_youtube_shapes_are_accepted(self):
        for good in (
            {"title": "t", "url": _URL, "thumbnail": "https://i.ytimg.com/vi/x/hqdefault.jpg"},
            {"title": "t", "url": "https://youtu.be/0eNXLHTnkIU"},
            {"title": "t", "url": "https://youtube.com/watch?v=0eNXLHTnkIU&t=3"},
        ):
            with self.subTest(good=good):
                self.assertEqual(self._errors("youtube_embeds", [good]), [])

    def test_news_items_must_be_objects_with_title_and_url(self):
        self.assertTrue(self._errors("news", ["https://example.com/a"]))
        self.assertTrue(self._errors("news", [{"title": "t"}]))
        self.assertEqual(self._errors("news", [{"title": "t", "url": "https://example.com/a"}]), [])

    def test_every_committed_article_passes_the_media_item_schema(self):
        # 既存記事の後退防止。形を崩した記事が入れば、ここで落ちる。
        failures = []
        for path in sorted((REPO_ROOT / "data" / "articles").glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            for field in ("youtube_embeds", "news"):
                if isinstance(data, dict) and self._errors(field, data.get(field) or []):
                    failures.append(f"{path.name}:{field}")
        self.assertEqual(failures, [])
