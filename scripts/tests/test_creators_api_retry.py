"""Unit tests for creators_api_client retry gating (Issue #2744).

Non-retryable HTTP errors (400/403/422/5xx) must abort immediately instead of
exhausting max_retries with exponential backoff. Retryable errors (429/401)
must still retry.
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import creators_api_client as cac  # noqa: E402


def _resp(status: int, body: str = "{}", json_obj=None):
    r = mock.Mock()
    r.status_code = status
    r.text = body
    r.json.return_value = json_obj if json_obj is not None else {}
    return r


class _Client(cac.CreatorsAPIClient):
    """Test client that skips real OAuth."""

    def _get_auth_headers(self) -> dict:  # type: ignore[override]
        return {"Authorization": "Bearer test"}


class RetryGatingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = _Client(max_retries=5)

    def test_400_aborts_without_retry(self) -> None:
        with mock.patch.object(cac.requests, "post", return_value=_resp(400, '{"err":"bad"}')) as post, \
             mock.patch.object(self.client, "_sleep_with_backoff") as sleep:
            with self.assertRaises(cac.CreatorsAPIRequestError):
                self.client._make_request("/get", {"x": 1})
        self.assertEqual(post.call_count, 1, "400 must not retry")
        sleep.assert_not_called()

    def test_500_aborts_without_retry(self) -> None:
        with mock.patch.object(cac.requests, "post", return_value=_resp(500, "oops")) as post, \
             mock.patch.object(self.client, "_sleep_with_backoff") as sleep:
            with self.assertRaises(cac.CreatorsAPIRequestError):
                self.client._make_request("/get", {"x": 1})
        self.assertEqual(post.call_count, 1, "500 must not retry")
        sleep.assert_not_called()

    def test_429_retries_until_max(self) -> None:
        with mock.patch.object(cac.requests, "post", return_value=_resp(429, "throttled")) as post, \
             mock.patch.object(self.client, "_sleep_with_backoff"):
            with self.assertRaises(cac.CreatorsAPIRequestError):
                self.client._make_request("/get", {"x": 1})
        self.assertEqual(post.call_count, 5, "429 must retry up to max_retries")

    def test_200_returns_immediately(self) -> None:
        with mock.patch.object(cac.requests, "post", return_value=_resp(200, json_obj={"ok": True})) as post:
            out = self.client._make_request("/get", {"x": 1})
        self.assertEqual(out, {"ok": True})
        self.assertEqual(post.call_count, 1)


if __name__ == "__main__":
    unittest.main()
