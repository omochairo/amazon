"""hugo/layouts/partials/ のテンプレート出力テスト (実 hugo build)。

主眼は head.html の robots meta (#4964): navi.omcha.jp は max-image-preview を
全ページで未指定のため Google の既定値 (standard) が適用されていた。
index,follow ページにのみ max-image-preview:large / max-snippet:-1 /
max-video-preview:-1 を付与する。

extend_head.html の GA4 ラボ/自動化トラフィック除外ガード (brain#57) もここで
一緒に押さえる — テンプレートロジックの回帰テストという性質が同じなため。

テンプレートの直接ユニットテストは本リポジトリに前例が無いため、実際に
`hugo build` した出力 HTML を検証する (テンプレートロジックそのものへの
回帰テストとして最も確実)。hugo バイナリが無い環境ではスキップする。
"""
from __future__ import annotations

import os
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from scripts.run_lighthouse_lane import LAB_UA_MARKER

REPO_ROOT = Path(__file__).resolve().parents[2]
HUGO_DIR = REPO_ROOT / "hugo"
# テーマの submodule を持たない作業ツリーでは、環境変数でテーマの置き場を渡せる (任意)
THEMES_DIR_ENV = "HUGO_THEMES_DIR"

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
    themes_dir = HUGO_DIR / "themes"
    if not (themes_dir / "PaperMod" / "layouts").exists() and os.environ.get(THEMES_DIR_ENV):
        themes_dir = Path(os.environ[THEMES_DIR_ENV])
    if not (themes_dir / "PaperMod" / "layouts").exists():
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
                    "--themesDir",
                    str(themes_dir),
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


def _html(out_dir: Path, rel_path: str) -> str:
    path = out_dir / rel_path / "index.html"
    assert path.exists(), f"missing build output: {path}"
    return path.read_text(encoding="utf-8")


def _breadcrumb_schema(html: str) -> dict:
    for raw in re.findall(
        r'<script\s+type=["\']?application/ld\+json["\']?>(.*?)</script>', html, re.S
    ):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if value.get("@type") == "BreadcrumbList":
            return value
    raise AssertionError("BreadcrumbList JSON-LD not found")


def test_age_article_breadcrumb_and_schema(hugo_build_dir):
    html = _html(hugo_build_dir, "products/b0f99n8ctd")
    assert re.search(
        r'href=["\']?/toys-age-1/["\']?\s+data-nav=["\']?breadcrumb-age["\']?\s+'
        r'data-age-hub=["\']?age-1', html
    )
    assert "1歳のおもちゃ" in html
    items = _breadcrumb_schema(html)["itemListElement"]
    assert [(i["position"], i["name"], i["item"]) for i in items] == [
        (1, "1歳のおもちゃ", "https://navi.omcha.jp/toys-age-1/"),
        (2, "Vanplay モンテッソーリ 知育玩具の最安値・対象年齢を横断比較！1歳からの指先訓練に",
         "https://navi.omcha.jp/products/b0f99n8ctd/"),
    ]


def test_age_four_five_breadcrumb_uses_combined_hub_name(hugo_build_dir):
    html = _html(hugo_build_dir, "products/b0f1tqblld")
    assert re.search(r'data-nav=["\']?breadcrumb-age["\']?\s+data-age-hub=["\']?age-4', html)
    assert "4歳・5歳のおもちゃ" in html
    first = _breadcrumb_schema(html)["itemListElement"][0]
    assert first["position"] == 1
    assert first["name"] == "4歳・5歳のおもちゃ"
    assert first["item"] == "https://navi.omcha.jp/toys-age-4/"


def test_article_without_age_keeps_section_breadcrumb(hugo_build_dir):
    html = _html(hugo_build_dir, "products/b008dc0gss")
    assert 'data-nav="breadcrumb-age"' not in html
    assert "📚 記事一覧" in html
    items = _breadcrumb_schema(html)["itemListElement"]
    assert items[0] == {
        "@type": "ListItem", "position": 1, "name": "📚 記事一覧",
        "item": "https://navi.omcha.jp/posts/",
    }
    assert items[1]["position"] == 2


@pytest.mark.parametrize("path", ["toys-age-1", "ranking"])
def test_non_article_breadcrumb_schema_remains_section_based(hugo_build_dir, path):
    html = _html(hugo_build_dir, path)
    items = _breadcrumb_schema(html)["itemListElement"]
    assert len(items) == 1
    assert items[0]["position"] == 1
    assert f"https://navi.omcha.jp/{path}/" == items[0]["item"]


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


def test_ga4_excludes_lab_and_automation_traffic(hugo_build_dir):
    """extend_head.html の GA4 ラボ/自動化除外ガードが出力に残っていること (brain#57)。

    2026-09-02 に追加されたこの 2 本のガードが無いと、Lighthouse lab レーン
    (毎日 11 URL x mobile/desktop) の自己ヒットが navi.omcha.jp の GA4 PV に
    混入する。brain#57 の調査で、この除外が効き始めた 2026-09-04 に navi の
    日次 PV が 60〜70% 落ちたことを確認済み (需要減ではなく計測の補正)。
    リファクタで静かに消えると同じ規模の水増しが再発するので、ビルド出力に
    両方のガードが残っていることを不変条件として押さえる。
    """
    html = (hugo_build_dir / "index.html").read_text(encoding="utf-8")

    assert "ga-disable-G-D8ZQX1BT20" in html
    # UA マーカーと navigator.webdriver の併記ガード。マーカーは送り側
    # (run_lighthouse_lane.py) の定数から取る — 直書きすると値を変えても緑のまま
    # 送り側とずれる (#7896)。
    assert "/%s/.test(navigator.userAgent)" % LAB_UA_MARKER in html
    assert "navigator.webdriver" in html
    # 正規ホスト (config.toml の baseURL) 以外を弾くガード。
    assert "location.hostname" in html
    assert "navi.omcha.jp" in html
    # 2 本とも `window['ga-disable-G-D8ZQX1BT20']=true` を立てる作りなので、
    # 出力上も ga-disable の代入が最低 2 箇所残っているはず。
    assert html.count("ga-disable-G-D8ZQX1BT20") >= 2


def test_cf_rum_excludes_lab_and_automation_traffic(hugo_build_dir):
    """Cloudflare Web Analytics (RUM) も GA4 と同じ条件で送信を止めること (#7895)。

    CF の beacon には公式のオプトアウトが無いので、beacon.min.js の二重読み込み
    防止 (`window.__cfBeacon.load === "single"` なら return) を流用している。
    GA4 のガード 2 本がそれぞれこのフラグも立てるので、出力上も 2 箇所残る。
    ガードは defer の beacon より前に実行されなければ意味が無いので順序も押さえる。
    """
    html = (hugo_build_dir / "index.html").read_text(encoding="utf-8")

    beacon = html.find("static.cloudflareinsights.com/beacon.min.js")
    assert beacon != -1, "CF Web Analytics の beacon が出力に無い"
    guards = [
        m.start()
        for m in re.finditer(
            r"__cfBeacon\s*=\s*\{\s*load\s*:\s*[\"']single[\"']\s*\}", html
        )
    ]
    assert len(guards) >= 2
    assert all(g < beacon for g in guards)


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
