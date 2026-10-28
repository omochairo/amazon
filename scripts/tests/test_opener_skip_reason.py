"""opener の抑止ログが、開いている Issue と wontfix で閉じたキーを言い分けるか。

以前は wontfix のキーも open の集合に足してから `skip (already open)` と出しており、
実際には開いている Issue が無いのにログが「開いている」と読めた (amazon-navi-brain#138)。
6 本の opener は同じ形なので A-3 で代表して確かめる。gh CLI は叩かない。
"""
from __future__ import annotations

import json
import logging
import sys
from unittest.mock import patch

from scripts import open_cannibalization_issues as m


def test_skip_reason_separates_open_and_dismissed(tmp_path, caplog):
    src = tmp_path / "cannibalization.json"
    src.write_text(json.dumps({
        "source_range": {"start": "2026-10-15", "end": "2026-10-22"},
        "detected": [
            {"query": q, "competing_page_count": 2, "total_impressions": 10,
             "total_clicks": 0, "best_position": 5.0, "top_share": 0.5, "pages": []}
            for q in ("open-q", "both-q", "dismissed-q", "new-q")
        ],
    }), encoding="utf-8")
    argv = ["x", "--input", str(src), "--repo", "o/r", "--dry-run"]
    with patch.object(sys, "argv", argv), \
            patch.object(m, "find_existing_taken", return_value={"open-q", "both-q"}), \
            patch.object(m, "read_closed_keys", return_value=set()), \
            patch.object(m, "find_dismissed_keys", return_value={"both-q", "dismissed-q"}), \
            caplog.at_level(logging.INFO, logger=m.logger.name):
        assert m.main() == 0

    msgs = [r.getMessage() for r in caplog.records]
    assert "skip (already open): open-q" in msgs
    # open と wontfix の両方にあるキーは、開いている方を言う
    assert "skip (already open): both-q" in msgs
    assert "skip (dismissed as wontfix): dismissed-q" in msgs
    assert any(s.startswith("would create:") and "new-q" in s for s in msgs)
    assert "existing cannibalization open Issues: 2 (+1 dismissed as wontfix)" in msgs
