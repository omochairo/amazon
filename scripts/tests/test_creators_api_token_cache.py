"""Unit tests for the on-disk OAuth token cache in creators_api_client.

Why this exists: the token endpoint rate-limits per credential, and Amazon's own
429 body says so —

    "This usually indicates a missing token cache — access tokens are valid for
     1 hour and should be reused."

Before this cache the client only kept the token in a process attribute, so any
usage pattern that starts a fresh process per call (a CLI invoked per keyword, a
job running several scripts) burned one token issuance each time and eventually
got 429 *before* reaching getItems/searchItems.

Every test points CREATORS_TOKEN_CACHE at a temp file. **Never let these tests
touch the developer's real cache** — a test that deletes a live token would push
the next real run straight back into the rate limit this change exists to avoid.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(THIS_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import creators_api_client as cac  # noqa: E402


def _token_response(token: str = "tok-abc", expires_in: int = 3600):
    r = mock.Mock()
    r.status_code = 200
    r.json.return_value = {"access_token": token, "expires_in": expires_in}
    return r


class TokenCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "creators_token.json")
        self.env = mock.patch.dict(
            os.environ, {"CREATORS_TOKEN_CACHE": self.path}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)

    def _client(self, credential_id: str = "cred-1") -> cac.CreatorsAPIClient:
        return cac.CreatorsAPIClient(
            application_id="app", credential_id=credential_id,
            credential_secret="secret", partner_tag="tag-22")

    def test_second_process_reuses_token_without_hitting_endpoint(self) -> None:
        with mock.patch.object(cac.requests, "post",
                               return_value=_token_response()) as post:
            self.assertEqual(self._client()._get_access_token(), "tok-abc")
            self.assertEqual(post.call_count, 1)

        # A brand new client stands in for a brand new process.
        with mock.patch.object(cac.requests, "post") as post:
            self.assertEqual(self._client()._get_access_token(), "tok-abc")
            post.assert_not_called()

    def test_secret_is_never_written_to_disk(self) -> None:
        with mock.patch.object(cac.requests, "post", return_value=_token_response()):
            self._client()._get_access_token()
        raw = open(self.path, encoding="utf-8").read()
        self.assertNotIn("secret", raw)
        self.assertNotIn("cred-1", raw)  # keyed by hash, not by the credential id

    def test_expired_entry_is_refetched(self) -> None:
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({self._client()._cache_key(): {
                "access_token": "stale", "expires_at": time.time() + 10,
                "version": "2.3"}}, fh)
        # 60s of headroom means "expires in 10s" is already unusable.
        with mock.patch.object(cac.requests, "post",
                               return_value=_token_response("fresh")) as post:
            self.assertEqual(self._client()._get_access_token(), "fresh")
            self.assertEqual(post.call_count, 1)

    def test_different_credentials_get_separate_entries(self) -> None:
        with mock.patch.object(cac.requests, "post", return_value=_token_response("a")):
            self._client("cred-a")._get_access_token()
        with mock.patch.object(cac.requests, "post", return_value=_token_response("b")):
            self._client("cred-b")._get_access_token()
        data = json.load(open(self.path, encoding="utf-8"))
        self.assertEqual(len(data), 2)

    def test_401_drops_the_cached_entry(self) -> None:
        with mock.patch.object(cac.requests, "post", return_value=_token_response()):
            client = self._client()
            client._get_access_token()
        self.assertIsNotNone(client._load_cached_token())

        resp = mock.Mock()
        resp.status_code = 401
        with mock.patch.object(cac.requests, "post",
                               return_value=_token_response("tok-2")):
            client._handle_retryable_error(resp, {})
        # The dead token must not survive for the next process to pick up.
        cached = client._load_cached_token()
        self.assertIsNotNone(cached)
        self.assertEqual(cached[0], "tok-2")

    def test_unreadable_cache_does_not_break_the_client(self) -> None:
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("not json at all")
        with mock.patch.object(cac.requests, "post", return_value=_token_response()):
            self.assertEqual(self._client()._get_access_token(), "tok-abc")


if __name__ == "__main__":
    unittest.main()
