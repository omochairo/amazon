"""scripts/open_catalog_brand_issue.py の重複起票判定 (#6622 の照合を通すこと)。"""
from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

from scripts.open_catalog_brand_issue import MARKER, has_open_issue


def _run_returning(items):
    return subprocess.CompletedProcess(
        args=[], returncode=0, stdout=json.dumps({"items": items}), stderr="")


def test_has_open_issue_true_when_body_has_marker():
    with patch("scripts.open_catalog_brand_issue.subprocess.run",
               return_value=_run_returning([{"number": 5, "body": f"<!-- {MARKER} -->\n..."}])):
        assert has_open_issue("repo") is True


def test_has_open_issue_false_when_empty():
    with patch("scripts.open_catalog_brand_issue.subprocess.run",
               return_value=_run_returning([])):
        assert has_open_issue("repo") is False


def test_has_open_issue_ignores_mention_only():
    # 言及しただけの issue で「起票済み」にすると、新しい候補の issue が黙って出なくなる
    items = [{"number": 1, "body": f"{MARKER} の候補が少ない"}, {"number": 2}]
    with patch("scripts.open_catalog_brand_issue.subprocess.run",
               return_value=_run_returning(items)):
        assert has_open_issue("repo") is False
