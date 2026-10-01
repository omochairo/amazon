"""scripts/prune_standby_noindex.py unit tests (#6415 の 2)."""
from __future__ import annotations

import pathlib

from scripts.prune_standby_noindex import find_noindex_terms, is_noindex_page, main

INDEX = '<html><head><meta name=robots content="index, follow, max-image-preview:large"></head></html>'
NOINDEX = '<html><head><meta name=robots content="noindex, follow"></head></html>'
ALIAS = ('<!doctype html><html><head><title>x</title><link rel=canonical href=https://navi.omcha.jp/tags/x/>'
         '<meta name=robots content="noindex"><meta charset=utf-8>'
         '<meta http-equiv=refresh content="0; url=https://navi.omcha.jp/tags/x/"></head></html>')


def _write(root: pathlib.Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _site(root: pathlib.Path) -> None:
    _write(root, "index.html", INDEX)
    # index される term: 2 ページ目以降は noindex でも残す
    _write(root, "tags/keep/index.html", INDEX)
    _write(root, "tags/keep/page/2/index.html", NOINDEX)
    # noindex term: ページ送りと feed ごと落とす
    _write(root, "tags/thin/index.html", NOINDEX)
    _write(root, "tags/thin/index.xml", "<rss/>")
    _write(root, "brands/nobrand/index.html", NOINDEX)
    # taxonomy 一覧のページ送りは term ではない
    _write(root, "tags/page/2/index.html", NOINDEX)
    # alias のリダイレクトページは残す
    _write(root, "tags/old-slug/index.html", ALIAS)
    # 対象外の taxonomy・通常ページ
    _write(root, "products/a/index.html", NOINDEX)


def test_is_noindex_page_handles_quoted_and_minified_attributes():
    assert is_noindex_page(NOINDEX)
    assert is_noindex_page('<meta name="robots" content="noindex, nofollow">')
    assert not is_noindex_page(INDEX)
    assert not is_noindex_page("<html><head></head></html>")


def test_alias_redirect_page_is_not_pruned():
    assert not is_noindex_page(ALIAS)


def test_find_noindex_terms_looks_only_at_the_first_page_of_terms(tmp_path):
    _site(tmp_path)
    result = find_noindex_terms(tmp_path)
    got = sorted(p.relative_to(tmp_path).as_posix() for p in result.pruned)
    assert got == ["brands/nobrand", "tags/thin"]
    assert result.kept == 2  # tags/keep, tags/old-slug
    assert result.pruned_bytes > 0


def test_main_removes_noindex_terms_and_keeps_the_rest(tmp_path, capsys):
    _site(tmp_path)
    assert main(["--root", str(tmp_path)]) == 0
    assert not (tmp_path / "tags/thin").exists()
    assert not (tmp_path / "brands/nobrand").exists()
    assert (tmp_path / "tags/keep/page/2/index.html").exists()
    assert (tmp_path / "tags/page/2/index.html").exists()
    assert (tmp_path / "tags/old-slug/index.html").exists()
    assert (tmp_path / "products/a/index.html").exists()
    assert "PRUNED_STANDBY_BYTES=" in capsys.readouterr().out


def test_dry_run_removes_nothing(tmp_path):
    _site(tmp_path)
    assert main(["--root", str(tmp_path), "--dry-run"]) == 0
    assert (tmp_path / "tags/thin/index.html").exists()


def test_refuses_when_almost_every_term_would_be_pruned(tmp_path, capsys):
    _write(tmp_path, "index.html", INDEX)
    for i in range(10):
        _write(tmp_path, f"tags/t{i}/index.html", NOINDEX)
    assert main(["--root", str(tmp_path)]) == 1
    assert (tmp_path / "tags/t0/index.html").exists()
    assert "何もしない" in capsys.readouterr().err


def test_fails_without_build_output(tmp_path):
    assert main(["--root", str(tmp_path / "public")]) == 1


def test_fails_when_there_are_no_terms(tmp_path, capsys):
    _write(tmp_path, "index.html", INDEX)
    assert main(["--root", str(tmp_path)]) == 1
    assert "1 件も無い" in capsys.readouterr().err
