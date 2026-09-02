"""掃除 step → opener 群への「閉じたキー」受け渡しの unit test。

GitHub の検索索引は非同期なので、close 直後の `is:open` 検索は旧状態を返しうる。
その依存を外すための受け渡し (scripts/_analytics_closed_keys.py)。
"""
from __future__ import annotations

import datetime as dt
import json
import os
from unittest.mock import patch

from scripts._analytics_closed_keys import (
    ENV_VAR,
    extract_dedup_keys,
    path_from_env,
    read_closed_keys,
    write_closed_keys,
)
from scripts.close_expired_analytics_issues import select_expired

A5 = "a5-orphan:"
A1 = "a1-low-ctr:"
URL = "https://navi.omcha.jp/products/b0gc4mql8n/"


def test_extract_dedup_keys_reads_marker():
    got = extract_dedup_keys(f"<!-- {A5}{URL} -->\n親 epic: #1356")
    assert got == {A5: [URL]}


def test_extract_dedup_keys_ignores_the_expiry_marker():
    # `analytics-expires:` を検出器マーカーと取り違えない (`a` の次が数字でない)
    body = f"<!-- {A5}{URL} -->\n<!-- analytics-expires:2026-09-13 -->"
    assert extract_dedup_keys(body) == {A5: [URL]}


def test_extract_dedup_keys_handles_hyphenated_and_multiple():
    body = "<!-- a4-engagement-drop:/u/ -->\n<!-- a3-cannibal:知育 玩具 -->"
    assert extract_dedup_keys(body) == {"a4-engagement-drop:": ["/u/"],
                                        "a3-cannibal:": ["知育 玩具"]}


def test_extract_dedup_keys_empty_for_unmarked():
    assert extract_dedup_keys(None) == {}
    assert extract_dedup_keys("マーカーの無い本文") == {}


def test_roundtrip_is_scoped_per_detector(tmp_path):
    # 同じ URL が A-1 でも検出されていることがある。A-5 を閉じたからといって
    # A-1 側の重複防止を外すと、まだ open な A-1 と重複した Issue を立ててしまう
    path = tmp_path / "closed.json"
    write_closed_keys({A5: [URL]}, path)
    assert read_closed_keys(A5, path) == {URL}
    assert read_closed_keys(A1, path) == set()


def test_read_closed_keys_tolerates_missing_and_broken(tmp_path):
    # 受け渡しは最適化であって前提ではない。壊れていても起票は続けさせる
    assert read_closed_keys(A5, tmp_path / "nope.json") == set()
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert read_closed_keys(A5, broken) == set()
    wrong_shape = tmp_path / "list.json"
    wrong_shape.write_text('["x"]', encoding="utf-8")
    assert read_closed_keys(A5, wrong_shape) == set()
    wrong_bucket = tmp_path / "bucket.json"
    wrong_bucket.write_text(json.dumps({A5: "not-a-list"}), encoding="utf-8")
    assert read_closed_keys(A5, wrong_bucket) == set()


def test_env_var_drives_the_default_path(tmp_path):
    path = tmp_path / "from_env.json"
    with patch.dict(os.environ, {ENV_VAR: str(path)}):
        assert path_from_env() == path
        assert write_closed_keys({A5: [URL]}) is True
        assert read_closed_keys(A5) == {URL}


def test_no_env_var_means_no_handoff(tmp_path):
    # 手元で opener を単体実行したとき、既定パスの古いファイルを踏まないこと
    with patch.dict(os.environ, {ENV_VAR: ""}):
        assert path_from_env() is None
        assert write_closed_keys({A5: [URL]}) is False
        assert read_closed_keys(A5) == set()


def _item(number: int, expiry: str, body_extra: str = "") -> dict:
    return {"number": number, "title": f"#{number}",
            "body": f"<!-- analytics-expires:{expiry} -->\n{body_extra}"}


def test_select_expired_carries_the_body_through():
    # closer は閉じた Issue の本文から dedup キーを拾うので body が要る
    items = [_item(1, "2026-09-01", f"<!-- {A5}{URL} -->")]
    got = select_expired(items, today=dt.date(2026, 9, 10))
    assert len(got) == 1
    assert got[0]["number"] == 1
    assert got[0]["expiry"] == dt.date(2026, 9, 1)
    assert extract_dedup_keys(got[0]["body"]) == {A5: [URL]}
