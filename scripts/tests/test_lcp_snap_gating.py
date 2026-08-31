"""#5081 — scroll-snap は :root.snap-ready の内側にしか書かない。

**なぜ**: `.carousel-wrapper` / `.age-timeline-track` は overflow-x:auto の
スナップコンテナで、読み込み中に中身が入るたび再スナップして scrollLeft が動く。
Chrome はそのスクロールで LCP 候補の記録を打ち切るため、**商品ページは LCP 候補が
1 件も記録されない**状態だった (実測 2026-08-31: trace の
`largestContentfulPaint::Candidate` 0 件 / `Invalidate` 9 件。PerformanceObserver で
LCP エントリ 0 件。`scroll-snap-type` を切ると同じページで LCP が記録される)。

素の CSS に書き戻すと同じ穴が開き、**症状は「lighthouse の lcp_element が
None のまま」という気づきにくい形**でしか出ない (LCP の数値自体は lighthouse が
最後の Invalidate から合成するので出続ける)。ここで機械的に止める。
"""
from __future__ import annotations

import pathlib
import re
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
CSS = REPO_ROOT / "hugo" / "assets" / "css" / "extended" / "custom.css"
JS = REPO_ROOT / "hugo" / "assets" / "js" / "carousel_swipe.js"

_RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)


def _without_comments(css: str) -> str:
    """コメント内の言及を宣言と誤認しないように落とす (このファイル自身が
    `scroll-snap-type` を説明する日本語コメントを各所に置いているため)。"""
    return _COMMENT_RE.sub("", css)


class SnapGatingTests(unittest.TestCase):
    def test_every_scroll_snap_declaration_is_gated(self):
        css = _without_comments(CSS.read_text(encoding="utf-8"))
        offenders = []
        for m in _RULE_RE.finditer(css):
            selector, body = m.group(1), m.group(2)
            if "scroll-snap-type" not in body:
                continue
            if ":root.snap-ready" not in selector:
                offenders.append(selector.strip().splitlines()[-1].strip()[:80])
        self.assertEqual(
            offenders, [],
            "scroll-snap-type は :root.snap-ready の内側だけに書くこと (#5081)。"
            f" 素で書かれているセレクタ: {offenders}",
        )

    def test_gated_rules_exist(self):
        # ゲートごと消えたら「スナップが無い」だけでなく、この検査も空振りする。
        css = _without_comments(CSS.read_text(encoding="utf-8"))
        self.assertIn(":root.snap-ready", css)
        self.assertRegex(css, r":root\.snap-ready[^{}]*\{[^{}]*scroll-snap-type")

    def test_snap_is_enabled_on_first_interaction_not_on_load(self):
        js = JS.read_text(encoding="utf-8")
        self.assertIn("snap-ready", js)
        for kind in ("pointerdown", "touchstart", "wheel", "keydown"):
            self.assertIn(kind, js)
        # `load` を境にすると、load が最初の描画より前に来る条件で同じ穴に戻る。
        self.assertNotRegex(js, r"addEventListener\(\s*'load'\s*,\s*enableSnap")


if __name__ == "__main__":
    unittest.main()
