"""scripts/ci_cf_purge.sh の判定 (#7386)。

API は叩かない (CF_PURGE_DRY_RUN=1)。navi-switch の出力を模したファイルを渡し、
「消去しない / 全消去」のどちらに倒れるかを見る。
**読めない入力は必ず全消去に倒れること**が一番大事な性質。
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "ci_cf_purge.sh"
SH = shutil.which("sh")

pytestmark = pytest.mark.skipif(SH is None, reason="sh が無い環境")


def _diff(paths: list[str], declared: int | None = None) -> str:
    n = len(paths) if declared is None else declared
    body = "".join(f"navi-diff\t{p}\n" for p in paths)
    return f"navi-diff-begin\n{body}navi-diff-end\t{n}\nswitched to abc (free=1GB, pruned=0)\n"


def _run(tmp_path: pathlib.Path, content: str | None, **extra_env: str) -> subprocess.CompletedProcess:
    target = tmp_path / "navi-switch.out"
    if content is not None:
        target.write_bytes(content.encode("utf-8"))
    env = {**os.environ, "CF_PURGE_DRY_RUN": "1", **extra_env}
    return subprocess.run(
        [SH, str(SCRIPT), str(target)],
        capture_output=True, text=True, encoding="utf-8", env=env, check=True,
    )


def test_no_effective_change_skips(tmp_path):
    r = _run(tmp_path, _diff(["build.json"]))
    assert "消去しない" in r.stderr
    assert "purge_everything" not in r.stderr


def test_empty_diff_skips(tmp_path):
    r = _run(tmp_path, _diff([]))
    assert "消去しない" in r.stderr


def _files_bodies(stderr: str) -> list[list[str]]:
    out = []
    for line in stderr.splitlines():
        if "dry-run: files[" in line:
            out.append(json.loads(line[line.index("{"):])["files"])
    return out


def test_changed_paths_are_purged_by_url(tmp_path):
    r = _run(tmp_path, _diff([
        "build.json",
        "index.html",
        "products/b000000000/index.html",
        "og/b000000000.jpg",
        "sitemap.xml",
    ]))
    assert "purge_everything" not in r.stderr
    assert _files_bodies(r.stderr) == [[
        "https://navi.omcha.jp/",
        "https://navi.omcha.jp/products/b000000000/",
        "https://navi.omcha.jp/og/b000000000.jpg",
        "https://navi.omcha.jp/sitemap.xml",
    ]]


def test_deleted_page_is_included(tmp_path):
    # navi-switch は削除も同じ navi-diff 行で出す。消さないと古い 200 がエッジに残る
    r = _run(tmp_path, _diff(["products/bgone00000/index.html"]))
    assert _files_bodies(r.stderr) == [["https://navi.omcha.jp/products/bgone00000/"]]


def test_urls_are_chunked(tmp_path):
    paths = [f"products/b{i:09d}/index.html" for i in range(65)]
    r = _run(tmp_path, _diff(paths))
    bodies = _files_bodies(r.stderr)
    assert [len(b) for b in bodies] == [30, 30, 5]
    assert sum(bodies, []) == [f"https://navi.omcha.jp/products/b{i:09d}/" for i in range(65)]


def test_too_many_paths_fall_back_to_purge_everything(tmp_path):
    paths = [f"tags/t{i}/index.html" for i in range(11)]
    r = _run(tmp_path, _diff(paths), CF_PURGE_URL_MAX="10")
    assert "dry-run: purge_everything" in r.stderr
    assert _files_bodies(r.stderr) == []


@pytest.mark.parametrize("bad", ["tags/音楽/index.html", 'x"y.html', "a b/index.html"])
def test_path_needing_encoding_falls_back_to_purge_everything(tmp_path, bad):
    r = _run(tmp_path, _diff(["index.html", bad]))
    assert "dry-run: purge_everything" in r.stderr
    assert _files_bodies(r.stderr) == []


@pytest.mark.parametrize(
    "content",
    [
        None,  # ファイルが無い
        "navi-diff-unknown\nswitched to abc\n",  # 前世代が無い
        "switched to abc (free=1GB, pruned=0)\n",  # 古い navi-switch
        _diff(["build.json"], declared=2),  # 件数が合わない (途中で切れた)
    ],
    ids=["missing", "unknown", "old-navi-switch", "truncated"],
)
def test_unreadable_input_falls_back_to_purge_everything(tmp_path, content):
    r = _run(tmp_path, content)
    assert "dry-run: purge_everything" in r.stderr
