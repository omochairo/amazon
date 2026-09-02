"""hugo/layouts/partials/head.html の robots meta 出力テスト (#4964)。

navi.omcha.jp は max-image-preview を全ページで未指定のため Google の
既定値 (standard) が適用されていた。index,follow ページにのみ
max-image-preview:large / max-snippet:-1 / max-video-preview:-1 を付与する。

テンプレートの直接ユニットテストは本リポジトリに前例が無いため、実際に
`hugo build` した出力 HTML を検証する (テンプレートロジックそのものへの
回帰テストとして最も確実)。hugo バイナリが無い環境ではスキップする。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HUGO_DIR = REPO_ROOT / "hugo"

ROBOTS_RE = re.compile(r'<meta name=robots content="([^"]*)">')
LOC_RE = re.compile(r"<loc>(.*?)</loc>")


# CI では skip を許さない (#4793 と同じ形: 前提が欠けたまま緑で通ると、レーンが
# 黙って死ぬ)。45-template-tests.yml が HUGO_TEMPLATE_TESTS=required を渡す。
_REQUIRED = os.environ.get("HUGO_TEMPLATE_TESTS") == "required"
# ビルドは手元実測で 100 秒前後 (本番の GitLab `pages` ジョブは前処理込みで約 9 分)。
# 手元は 300 秒で足りるが、CI では余裕を持たせる。
_BUILD_TIMEOUT = int(os.environ.get("HUGO_TEST_BUILD_TIMEOUT", "300"))


def _unavailable(reason: str):
    """前提が無いときの扱い。手元では skip、CI (required) では fail。"""
    if _REQUIRED:
        pytest.fail(
            "HUGO_TEMPLATE_TESTS=required なのにテンプレートテストを実行できない: "
            + reason
        )
    pytest.skip(reason)


@pytest.fixture(scope="module")
def hugo_build_dir():
    if shutil.which("hugo") is None:
        _unavailable("hugo binary not found")
    if not (HUGO_DIR / "themes" / "PaperMod" / "layouts").exists():
        _unavailable("PaperMod theme submodule not initialized")

    with tempfile.TemporaryDirectory(prefix="hugo_robots_test_") as tmp:
        out_dir = Path(tmp) / "public"
        try:
            proc = subprocess.run(
                [
                    "hugo",
                    "--environment",
                    "production",
                    "--minify",
                    "-d",
                    str(out_dir),
                ],
                cwd=HUGO_DIR,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_BUILD_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            _unavailable("hugo build timed out (%ds)" % _BUILD_TIMEOUT)
        if proc.returncode != 0:
            pytest.fail(f"hugo build failed:\n{proc.stdout}\n{proc.stderr}")
        yield out_dir


def _robots_content(out_dir: Path, rel_path: str) -> str:
    html_path = out_dir / rel_path / "index.html"
    assert html_path.exists(), f"missing build output: {html_path}"
    text = html_path.read_text(encoding="utf-8")
    m = ROBOTS_RE.search(text)
    assert m, f"no <meta name=robots> found in {html_path}"
    return m.group(1)


def test_indexed_product_page_has_max_image_preview(hugo_build_dir):
    """通常の商品記事 (index,follow) に max-image-preview:large 等が付与される。"""
    content = _robots_content(hugo_build_dir, "products/b0c8hk543g")
    assert content.startswith("index, follow")
    assert "max-image-preview:large" in content
    assert "max-snippet:-1" in content
    assert "max-video-preview:-1" in content


def test_noindex_page_has_no_max_image_preview(hugo_build_dir):
    """noindex ページ (例: tags のページネーション 2 ページ目) は変更しない。"""
    content = _robots_content(hugo_build_dir, "tags/page/2")
    assert content.startswith("noindex")
    assert "max-image-preview" not in content


def test_sitemap_and_robots_agree(hugo_build_dir):
    """sitemap に載る URL は 1 つ残らず index,follow であること (#6206 A)。

    head.html の meta robots と _default/sitemap.xml は同じ noindex 条件を
    それぞれ手書きしており、**片方だけ条件が増えるとずれる**。ずれると
    「noindex なのに sitemap で index を要求している」ページが出て、
    クロールバジェットを噛む (#2701 でこの sitemap を作った動機そのもの)。

    #6206 A で判定を partials/is_noindex_term.html に寄せたので、両者が同じ
    述語を見ていることをビルド出力で押さえる。条件の列挙をテストに書き写すと
    3 箇所目の mirror ができるだけなので、**不変条件だけを見る**。

    paginate 2 ページ目以降は sitemap に Page として現れないため対象外。
    """
    sitemap = (hugo_build_dir / "sitemap.xml").read_text(encoding="utf-8")
    locs = LOC_RE.findall(sitemap)
    assert len(locs) > 100, f"sitemap が小さすぎる ({len(locs)} 件) — ビルド不全を疑う"

    offenders = []
    for url in locs:
        rel = re.sub(r"^https?://[^/]+/", "", url)
        html_path = hugo_build_dir / rel / "index.html" if rel else hugo_build_dir / "index.html"
        assert html_path.exists(), f"sitemap の URL に対応する出力が無い: {url}"
        m = ROBOTS_RE.search(html_path.read_text(encoding="utf-8", errors="replace"))
        content = m.group(1) if m else "(none)"
        if not content.startswith("index, follow"):
            offenders.append((url, content))

    assert not offenders, (
        "sitemap に noindex ページが載っている "
        f"({len(offenders)}/{len(locs)} 件): {offenders[:5]}"
    )
