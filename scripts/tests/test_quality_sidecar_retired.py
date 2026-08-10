"""<slug>.quality.json sidecar 廃止の回帰テスト (#4826 項目 4)。

sidecar は「1 記事 1 ファイルの派生物を data/articles/ に撒く」経路で、
quality_gate 側は無効化する手段が無かった (--write-reports が
action="store_true" かつ default=True)。main 全量の品質観測は
48-quality-census.yml (集計 JSON 1 本) が担うため、生成経路ごと除去した。

ここでは「もう誰も書かない」ことを実行で確かめる。
"""
from __future__ import annotations

import json
import pathlib
import shutil
import sys

import pytest

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1]
_ROOT = _SCRIPTS.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import build_post  # noqa: E402
import quality_gate as qg  # noqa: E402

_SCHEMA = _ROOT / "data" / "schema" / "article.schema.json"


def _one_real_article() -> pathlib.Path:
    arts = sorted(
        p for p in (_ROOT / "data" / "articles").glob("*.json")
        if p.name.count(".") == 1
    )
    if not arts:
        pytest.skip("data/articles に記事が無い環境")
    return arts[-1]


def test_quality_gate_writes_no_sidecar(tmp_path, monkeypatch):
    """quality_gate を走らせても --src に派生ファイルが 1 つも増えない。"""
    src = tmp_path / "articles"
    src.mkdir()
    shutil.copy(_one_real_article(), src)
    before = {p.name for p in src.iterdir()}

    monkeypatch.chdir(_ROOT)
    monkeypatch.setattr(sys, "argv", [
        "quality_gate.py",
        "--src", str(src) + "/",
        "--posts", str(tmp_path / "nonexistent") + "/",
        "--schema", str(_SCHEMA),
        "--no-cert-fetch",
        "--quiet",
    ])
    rc = qg.main()

    assert rc in (0, 1, 2)  # 合否そのものはここでは問わない
    after = {p.name for p in src.iterdir()}
    assert after == before, f"sidecar が書かれた: {sorted(after - before)}"
    assert not list(src.glob("*.quality.json"))


def _code_only(fn) -> str:
    """コメント行を落とした関数ソース。廃止の経緯を書いたコメントに引っかからないため。"""
    import inspect
    out = []
    for line in inspect.getsource(fn).splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        out.append(line.split("  #")[0])
    return "\n".join(out)


def test_quality_gate_has_no_write_reports_flag():
    """無効化できない書き込みフラグが復活していないこと。"""
    code = _code_only(qg.main)
    assert "--write-reports" not in code
    assert ".quality.json" not in code


def test_build_post_has_no_sidecar_read_path():
    """sidecar を読んで draft を決める経路 (_quality_draft) が消えていること。"""
    assert not hasattr(build_post, "_quality_draft")


def test_build_post_gate_does_not_write_sidecar():
    assert ".quality.json" not in _code_only(build_post.main)


def test_quality_sidecar_is_still_gitignored():
    """旧 checkout で作られた取りこぼしが誤って commit されないための保険。"""
    text = (_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "data/articles/*.quality.json" in text
