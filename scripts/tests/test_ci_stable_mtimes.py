import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ci_stable_mtimes as m  # noqa: E402


def _mtime(p: Path) -> int:
    return int(os.stat(p).st_mtime)


def test_same_content_gets_same_mtime_across_builds(tmp_path):
    a = tmp_path / "a" / "index.html"
    b = tmp_path / "b" / "index.html"
    a.parent.mkdir()
    b.parent.mkdir()
    a.write_bytes(b"<html>same</html>")
    b.write_bytes(b"<html>same</html>")
    os.utime(a, (1_700_000_000, 1_700_000_000))
    os.utime(b, (1_800_000_000, 1_800_000_000))

    m.apply(tmp_path / "a")
    m.apply(tmp_path / "b")

    assert _mtime(a) == _mtime(b)


def test_changed_content_changes_mtime(tmp_path):
    p = tmp_path / "x.css"
    p.write_bytes(b"body{color:red}")
    m.apply(tmp_path)
    before = _mtime(p)

    p.write_bytes(b"body{color:blu}")  # 大きさは同じ
    m.apply(tmp_path)

    assert _mtime(p) != before


def test_mtime_is_in_sane_range(tmp_path):
    (tmp_path / "sub").mkdir()
    for i in range(20):
        (tmp_path / "sub" / f"{i}.txt").write_text(str(i))
    assert m.apply(tmp_path) == 20
    for p in (tmp_path / "sub").iterdir():
        assert m._BASE <= _mtime(p) < m._BASE + m._SPAN


def test_main_rejects_missing_dir(tmp_path):
    assert m.main([str(tmp_path / "nope")]) == 2
