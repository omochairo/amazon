"""scripts/ci_cf_purge.sh の判定 (#7386)。

API は叩かない (CF_PURGE_DRY_RUN=1)。navi-switch の出力を模したファイルを渡し、
「消去しない / 全消去」のどちらに倒れるかを見る。
**読めない入力は必ず全消去に倒れること**が一番大事な性質。
"""
from __future__ import annotations

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


def _run(tmp_path: pathlib.Path, content: str | None) -> subprocess.CompletedProcess:
    target = tmp_path / "navi-switch.out"
    if content is not None:
        target.write_bytes(content.encode("utf-8"))
    env = {**os.environ, "CF_PURGE_DRY_RUN": "1"}
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


def test_real_change_purges(tmp_path):
    r = _run(tmp_path, _diff(["build.json", "products/b000000000/index.html"]))
    assert "dry-run: purge_everything" in r.stderr


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
