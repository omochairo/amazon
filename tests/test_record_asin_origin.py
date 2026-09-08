"""scripts/record_asin_origin.py の純関数の検査 (#4964 観察項目3 台帳)。"""
from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1] / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import record_asin_origin as R  # noqa: E402


def test_make_record_shape():
    rec = R.make_record(ts="2026-09-08T12:34:56Z", run_id="123", workflow="03-invoke-jules",
                         asin="B0XXXXXXXX", pool="demand", source_keyword="トミカ 収納")
    assert rec == {
        "ts": "2026-09-08T12:34:56Z",
        "run_id": "123",
        "workflow": "03-invoke-jules",
        "asin": "B0XXXXXXXX",
        "pool": "demand",
        "source_keyword": "トミカ 収納",
    }


def test_make_record_rejects_unknown_pool():
    with pytest.raises(ValueError):
        R.make_record(ts="t", run_id="1", workflow="w", asin="B0X",
                       pool="supply", source_keyword=None)


def test_make_record_rejects_empty_asin():
    with pytest.raises(ValueError):
        R.make_record(ts="t", run_id="1", workflow="w", asin="",
                       pool="demand", source_keyword=None)


def test_make_record_normalizes_empty_source_keyword_to_none():
    rec = R.make_record(ts="t", run_id="1", workflow="w", asin="B0X",
                         pool="ranking-sniper", source_keyword="")
    assert rec["source_keyword"] is None


@pytest.mark.parametrize("pool", sorted(R.POOLS))
def test_all_four_pools_accepted(pool):
    rec = R.make_record(ts="t", run_id="1", workflow="w", asin="B0X",
                         pool=pool, source_keyword=None)
    assert rec["pool"] == pool


def test_dedupe_new_drops_existing_run_asin_pair():
    existing = {("42", "B0AAAAAAAA")}
    records = [
        R.make_record(ts="t", run_id="42", workflow="w", asin="B0AAAAAAAA",
                       pool="demand", source_keyword="x"),
        R.make_record(ts="t", run_id="42", workflow="w", asin="B0BBBBBBBB",
                       pool="supply-random", source_keyword="y"),
    ]
    out = R.dedupe_new(existing, records)
    assert [r["asin"] for r in out] == ["B0BBBBBBBB"]


def test_dedupe_new_drops_duplicates_within_the_same_batch():
    records = [
        R.make_record(ts="t", run_id="42", workflow="w", asin="B0AAAAAAAA",
                       pool="demand", source_keyword="x"),
        R.make_record(ts="t", run_id="42", workflow="w", asin="B0AAAAAAAA",
                       pool="demand", source_keyword="x"),
    ]
    out = R.dedupe_new(set(), records)
    assert len(out) == 1


def test_dedupe_new_same_asin_different_run_id_is_not_a_duplicate():
    existing = {("41", "B0AAAAAAAA")}
    records = [
        R.make_record(ts="t", run_id="42", workflow="w", asin="B0AAAAAAAA",
                       pool="demand", source_keyword="x"),
    ]
    out = R.dedupe_new(existing, records)
    assert len(out) == 1


def test_read_existing_keys_missing_file_returns_empty_set(tmp_path):
    assert R.read_existing_keys(str(tmp_path / "nope.jsonl")) == set()


def test_read_existing_keys_ignores_malformed_lines(tmp_path):
    p = tmp_path / "ledger.jsonl"
    p.write_text('{"run_id": "1", "asin": "B0AAAAAAAA"}\nnot json\n\n', encoding="utf-8")
    assert R.read_existing_keys(str(p)) == {("1", "B0AAAAAAAA")}


def test_append_records_creates_file_and_parent_dir(tmp_path):
    out = tmp_path / "nested" / "asin_origin.jsonl"
    records = [R.make_record(ts="t", run_id="1", workflow="w", asin="B0AAAAAAAA",
                              pool="demand", source_keyword="x")]
    written = R.append_records(str(out), records)
    assert written == 1
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1


def test_append_records_is_idempotent_across_calls(tmp_path):
    out = tmp_path / "asin_origin.jsonl"
    records = [R.make_record(ts="t", run_id="1", workflow="w", asin="B0AAAAAAAA",
                              pool="demand", source_keyword="x")]
    first = R.append_records(str(out), records)
    second = R.append_records(str(out), records)
    assert (first, second) == (1, 0)
    assert len(out.read_text(encoding="utf-8").splitlines()) == 1


def test_append_records_appends_to_existing_content(tmp_path):
    out = tmp_path / "asin_origin.jsonl"
    R.append_records(str(out), [R.make_record(ts="t", run_id="1", workflow="w",
                                                asin="B0AAAAAAAA", pool="demand",
                                                source_keyword="x")])
    written = R.append_records(str(out), [R.make_record(ts="t", run_id="2", workflow="w",
                                                          asin="B0BBBBBBBB", pool="rewrite-queue",
                                                          source_keyword=None)])
    assert written == 1
    assert len(out.read_text(encoding="utf-8").splitlines()) == 2
