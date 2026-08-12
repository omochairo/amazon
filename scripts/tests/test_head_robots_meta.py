"""hugo/layouts/partials/head.html の robots meta 出力テスト (#4964)。

navi.omcha.jp は max-image-preview を全ページで未指定のため Google の
既定値 (standard) が適用されていた。index,follow ページにのみ
max-image-preview:large / max-snippet:-1 / max-video-preview:-1 を付与する。

テンプレートの直接ユニットテストは本リポジトリに前例が無いため、実際に
`hugo build` した出力 HTML を検証する (テンプレートロジックそのものへの
回帰テストとして最も確実)。hugo バイナリが無い環境ではスキップする。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HUGO_DIR = REPO_ROOT / "hugo"

ROBOTS_RE = re.compile(r'<meta name=robots content="([^"]*)">')


@pytest.fixture(scope="module")
def hugo_build_dir():
    if shutil.which("hugo") is None:
        pytest.skip("hugo binary not found")
    if not (HUGO_DIR / "themes" / "PaperMod" / "layouts").exists():
        pytest.skip("PaperMod theme submodule not initialized")

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
                timeout=300,
            )
        except subprocess.TimeoutExpired:
            pytest.skip("hugo build timed out (300s)")
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
