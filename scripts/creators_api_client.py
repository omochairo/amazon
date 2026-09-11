#!/usr/bin/env python3
"""Amazon Creators API Client

🚨 重要 — これは Amazon **Creator API (creatorsapi.amazon)** のクライアント。
  - **PA-API 5 (webservices.amazon.co.jp/paapi5/...) ではない**。Legacy 名は
    PAAPIClient だったが、現行 omochairo は Creator API のみ使用する。
  - PA-API 5 公式仕様 (resource 名 / response schema / TPS / signing) は **流用不可**。

エンドポイント (FE / 日本マーケットプレイス, www.amazon.co.jp):
  - OAuth Token: ``https://creatorsapi.auth.us-west-2.amazoncognito.com/oauth2/token``
                 (v2.3, legacy Cognito) または ``https://api.amazon.co.jp/auth/o2/token``
                 (v3.3, LwA: credential_id が ``amzn1.`` で始まる場合)
  - getItems:    ``POST /catalog/v1/getItems``    → 応答 ``itemsResult.items``
  - searchItems: ``POST /catalog/v1/searchItems`` → 応答 ``searchResult.items``

認証フロー:
  OAuth 2.0 Client Credentials。``Authorization: Bearer <token>, Version <2.3|3.3>``
  と ``x-marketplace`` / ``x-amz-application-id`` ヘッダを併用する。

環境変数:
    AMAZON_CREATORS_APPLICATION_ID: アプリケーション ID (x-amz-application-id ヘッダ)
    AMAZON_CREATORS_CREDENTIAL_ID:  クレデンシャル ID (OAuth client_id)
    AMAZON_CREATORS_CREDENTIAL_SECRET: クレデンシャルシークレット (OAuth client_secret)
    AMAZON_PARTNER_TAG: アソシエイト ID (partnerTag フィールド)

エラー応答 (session 60 PR #786):
  200/404 以外のレスポンスは ``[CreatorsAPI] HTTP <code> <url> :: <body[:300]>``
  形式で stdout に出す。PR #761 (deliveryInfo invalid resource) の真因究明が
  「API request failed after 5 attempts」しか残らず不能だった事案の再発防止。

関連 trap:
  [[feedback-omochairo-creators-api-deliveryinfo-trap]] —
  resource 名は PA-API 5 と互換しない箇所がある。本クライアントを使う側
  (``fetch_amazon.py`` 等) の SEARCH_ITEM_RESOURCES 定数を変更する PR は
  ``scripts/fetch_amazon_dry_run.py`` (validate workflow の gate) で実 API
  検証されることを前提に設計すること。
"""
# NAS runner (amazon-home-ops, Python 3.8) から import されるため、3.9+ の
# 組み込みジェネリック注釈 (tuple[...] / list[...]) を遅延評価にする (#3046)。
from __future__ import annotations

import os
import json
import time
import base64
import hashlib
import pathlib
import tempfile
import requests
from typing import Any, Optional

# --- トークンのディスクキャッシュ ---------------------------------------------
#
# **アクセストークンは 1 時間有効なのに、プロセスが終わると捨てていた。**
# token エンドポイントには発行数の上限があり、超えると 429 で
#
#   "This usually indicates a missing token cache — access tokens are valid
#    for 1 hour and should be reused."
#
# が返る。2026-09-10 に実測で踏んだ (getItems / searchItems 以前に、
# トークン取得の時点で落ちる)。**API 側の枠は資格情報ごと**なので、
# 1 プロセス 1 トークンで済ませていても、プロセスを何度も起こす使い方
# (CLI をキーワードごとに叩く / 1 ジョブで複数スクリプトを回す) をすると枯れる。
#
# ディスクに置いて使い回す。**置くのはアクセストークンだけ**で、
# credential_secret は書かない。ファイル名にも credential_id をそのまま使わず
# ハッシュにする (資格情報を切り替えたときに別エントリになればよい)。
_ENV_CACHE_PATH = "CREATORS_TOKEN_CACHE"


def _default_token_cache_path() -> pathlib.Path:
    """既定の置き場。`CREATORS_TOKEN_CACHE` で差し替えられる。

    CI の runner は 1 ジョブごとに使い捨てなので、**ジョブをまたいだ再利用は
    しない**（トークンを actions/cache に置くと、そのリポジトリの他の
    workflow から読めてしまう）。効くのは同じジョブ・同じ端末の中だけで、
    それでも「キーワードごとに CLI を起こす」使い方は救われる。
    """
    override = os.environ.get(_ENV_CACHE_PATH)
    if override:
        return pathlib.Path(override)
    return pathlib.Path.home() / ".cache" / "omochairo" / "creators_token.json"

# Load .env file if available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv not installed, rely on system env vars

class CreatorsAPIError(Exception):
    """Base exception class for Creators API client."""
    pass

class CreatorsAPITokenError(CreatorsAPIError):
    """Exception raised when failing to get OAuth access token."""
    pass

class CreatorsAPIRequestError(CreatorsAPIError):
    """Exception raised when an API request fails after retries or with specific error status."""
    pass

class CreatorsAPIMaxRetriesError(CreatorsAPIRequestError):
    """Exception raised when maximum retry attempts are exceeded."""
    pass

class CreatorsAPIClient:
    """Amazon Creators API Client with OAuth 2.0 authentication."""

    # OAuth Token Endpoint for Japan (FE region - Cognito)
    OAUTH_TOKEN_URL = "https://creatorsapi.auth.us-west-2.amazoncognito.com/oauth2/token"

    # Creators API Base URL
    API_BASE_URL = "https://creatorsapi.amazon"

    # Endpoints
    GET_ITEMS_ENDPOINT = "/catalog/v1/getItems"
    SEARCH_ITEMS_ENDPOINT = "/catalog/v1/searchItems"

    # Marketplace for Japan
    MARKETPLACE = "www.amazon.co.jp"

    # Credential version for Japan (FE region)
    CREDENTIAL_VERSION = "2.3"

    def __init__(
        self,
        application_id: str = None,
        credential_id: str = None,
        credential_secret: str = None,
        partner_tag: str = None,
        max_retries: int = 5
    ):
        self.application_id = application_id or os.environ.get('AMAZON_CREATORS_APPLICATION_ID', '')
        self.credential_id = credential_id or os.environ.get('AMAZON_CREATORS_CREDENTIAL_ID', '')
        self.credential_secret = credential_secret or os.environ.get('AMAZON_CREATORS_CREDENTIAL_SECRET', '')
        self.partner_tag = partner_tag or os.environ.get('AMAZON_PARTNER_TAG', '')
        self.max_retries = max_retries

        self._access_token = None
        self._token_expires_at = 0

        # Quota and error tracking
        self.total_requests = 0
        self.throttle_count = 0

    # --- ディスクキャッシュ ---
    #
    # 壊れたファイル・読めないファイルで**本処理を止めない**。
    # キャッシュはあくまで 429 を避けるための最適化で、無くても動く。

    def _cache_key(self) -> str:
        return hashlib.sha256(self.credential_id.encode()).hexdigest()[:16]

    def _load_cached_token(self) -> Optional[tuple[str, float, str]]:
        """`(token, expires_at, version)`。使えなければ None。"""
        path = _default_token_cache_path()
        try:
            entry = json.loads(path.read_text(encoding="utf-8")).get(self._cache_key())
        except (OSError, ValueError, AttributeError):
            return None
        if not entry or not entry.get("access_token"):
            return None
        # 60 秒の余裕。**期限ちょうどのトークンを配ると、使う側で 401 になる**
        if time.time() >= float(entry.get("expires_at", 0)) - 60:
            return None
        return entry["access_token"], float(entry["expires_at"]), entry.get("version", "2.3")

    def _store_cached_token(self, token: str, expires_at: float, version: str) -> None:
        path = _default_token_cache_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    data = {}
            except (OSError, ValueError):
                data = {}
            data[self._cache_key()] = {
                "access_token": token, "expires_at": expires_at, "version": version}
            # 同じ端末で 2 プロセスが同時に書いても壊さない (書いてから置き換える)
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".creators_token.")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, path)
            os.chmod(path, 0o600)
        except OSError:
            pass  # 書けなくても動く。**ここで落とさない**

    def _invalidate_cached_token(self) -> None:
        """401 を食らったとき用。ディスク側も捨てないと次のプロセスが同じ死体を拾う。"""
        path = _default_token_cache_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.pop(self._cache_key(), None) is not None:
                path.write_text(json.dumps(data), encoding="utf-8")
        except (OSError, ValueError, AttributeError):
            pass

    def _get_access_token(self) -> str:
        """Get OAuth 2.0 access token using client credentials flow."""
        # Return cached token if still valid (60 seconds buffer)
        if self._access_token and time.time() < self._token_expires_at - 60:
            return self._access_token

        cached = self._load_cached_token()
        if cached:
            self._access_token, self._token_expires_at, self.CREDENTIAL_VERSION = cached
            return self._access_token

        # Check if using LwA (v3.x) or legacy Cognito (v2.x)
        is_lwa = self.credential_id.startswith("amzn1.")

        if is_lwa:
            self.CREDENTIAL_VERSION = "3.3"
            token_url = "https://api.amazon.co.jp/auth/o2/token"
            headers = {"Content-Type": "application/json"}
            payload = {
                "grant_type": "client_credentials",
                "client_id": self.credential_id,
                "client_secret": self.credential_secret,
                "scope": "creatorsapi::default"
            }
            response = requests.post(token_url, headers=headers, json=payload, timeout=30)
        else:
            self.CREDENTIAL_VERSION = "2.3"
            token_url = self.OAUTH_TOKEN_URL
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
            data = "grant_type=client_credentials&scope=creatorsapi/default"
            response = requests.post(
                token_url,
                auth=(self.credential_id, self.credential_secret),
                headers=headers,
                data=data,
                timeout=30
            )

        if response.status_code != 200:
            raise CreatorsAPITokenError(f"Failed to get access token: {response.status_code} - {response.text}")

        token_data = response.json()
        self._access_token = token_data.get("access_token")
        expires_in = token_data.get("expires_in", 3600)
        self._token_expires_at = time.time() + expires_in
        self._store_cached_token(
            self._access_token, self._token_expires_at, self.CREDENTIAL_VERSION)

        return self._access_token

    def _get_auth_headers(self) -> dict:
        """Generate common headers and Bearer token for API requests."""
        access_token = self._get_access_token()
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}, Version {self.CREDENTIAL_VERSION}",
            "x-marketplace": self.MARKETPLACE,
            "x-amz-application-id": self.application_id
        }

    def _sleep_with_backoff(self, attempt: int, message: str = "") -> None:
        """Sleep with exponential backoff."""
        delay = min(2 ** attempt, 30)
        if message:
            print(f"{message} (attempt {attempt}/{self.max_retries}), retrying in {delay}s...")
        else:
            print(f"Waiting {delay}s...")
        time.sleep(delay)

    def _handle_retryable_error(self, response: requests.Response, headers: dict) -> bool:
        """Handle retryable API errors (401, 429). Returns True if retry should proceed."""
        if response.status_code == 429:
            return True

        if response.status_code == 401:
            # Token expired, refresh for next attempt.
            # **ディスク側も捨てる。** 残すと次のプロセスが同じ死んだトークンを拾い、
            # 401 -> 再取得 を毎回やることになる (キャッシュを入れた意味が消える)
            self._access_token = None
            self._invalidate_cached_token()
            headers.update(self._get_auth_headers())
            return True

        return False

    def _attempt_request(self, url: str, headers: dict, payload: dict) -> tuple[Optional[dict], bool]:
        """Perform a single API request attempt. Returns (response_json, should_retry)."""
        self.total_requests += 1
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=30)

            if response.status_code == 200:
                return response.json(), False

            if response.status_code == 404:
                return response.json(), False

            if response.status_code == 429:
                self.throttle_count += 1

            # session 60: 元々ここで 400/500/422 等の本文を捨てており、retry 後に
            # 「API request failed after 5 attempts」だけが残って原因究明不能だった。
            # PR #761 で resource 名が invalid だったケース (PR #783 hotfix) を二度と
            # 「真因不明」にしないため、status code と response body 冒頭を必ず出す。
            body_preview = (response.text or "")[:300].replace("\n", " ")
            print(
                f"[CreatorsAPI] HTTP {response.status_code} {url} :: {body_preview}",
                flush=True,
            )

            if self._handle_retryable_error(response, headers):
                msg = "Rate limited" if response.status_code == 429 else ""
                return None, (msg or True)

            # #2744: 400/403/422/5xx 等の非リトライエラーはリトライしても結果が
            # 変わらない (resource 名 invalid / param 不正など)。従来は True を返して
            # 指数バックオフを挟みつつ max_retries まで同一リクエストを投げ続け、
            # API を無駄に叩いた上で原因も埋もれていた。即座に中断する。
            return None, False

        except requests.exceptions.RequestException as e:
            print(
                f"[CreatorsAPI] RequestException {url} :: {type(e).__name__}: {e}",
                flush=True,
            )
            return None, f"Request failed: {e}"

    def _make_request(self, endpoint: str, payload: dict) -> dict:
        """Make authenticated request to Creators API."""
        headers = self._get_auth_headers()
        url = f"{self.API_BASE_URL}{endpoint}"

        for attempt in range(1, self.max_retries + 1):
            result, retry_info = self._attempt_request(url, headers, payload)

            if result is not None:
                return result

            if retry_info and attempt < self.max_retries:
                msg = retry_info if isinstance(retry_info, str) else ""
                self._sleep_with_backoff(attempt, msg)
                continue

            # If we reached here without returning, it's an error on the last attempt
            # or a non-retryable error we didn't catch (though _attempt_request handles most)
            raise CreatorsAPIRequestError(f"API request failed after {attempt} attempts")

        raise CreatorsAPIMaxRetriesError("Max retries exceeded")

    def get_items(self, asins: list[str], resources: list[str] = None) -> dict:
        """Get item details by ASINs.

        Args:
            asins: List of ASINs to retrieve
            resources: List of resources to include
        """
        if resources is None:
            resources = [
                "images.primary.small",
                "images.primary.large",
                "images.variants.medium",
                "itemInfo.title",
                "itemInfo.features",
                "itemInfo.productInfo",
                "itemInfo.byLineInfo",
                "itemInfo.technicalInfo",
                "offersV2.listings.price",
                "offersV2.listings.availability",
                "browseNodeInfo.browseNodes"
            ]

        payload = {
            "itemIds": asins,
            "itemIdType": "ASIN",
            "marketplace": self.MARKETPLACE,
            "partnerTag": self.partner_tag,
            "resources": resources
        }

        return self._make_request(self.GET_ITEMS_ENDPOINT, payload)

    def search_items(
        self,
        keywords: str = None,
        search_index: str = "All",
        item_count: int = 10,
        item_page: int = 1,
        resources: list[str] = None
    ) -> dict:
        """Search for items.

        Args:
            keywords: Search keywords
            search_index: Category to search (e.g., 'Electronics', 'All')
            item_count: Number of results (1-10)
            item_page: Page number for pagination (1-10)
            resources: List of resources to include
        """
        if resources is None:
            resources = [
                "images.primary.small",
                "itemInfo.title",
                "itemInfo.features",
                "offersV2.listings.price",
                "browseNodeInfo.browseNodes"
            ]

        payload = {
            "keywords": keywords,
            "searchIndex": search_index,
            "itemCount": item_count,
            "itemPage": item_page,
            "marketplace": self.MARKETPLACE,
            "partnerTag": self.partner_tag,
            "resources": resources
        }

        return self._make_request(self.SEARCH_ITEMS_ENDPOINT, payload)

if __name__ == "__main__":
    import sys

    client = CreatorsAPIClient()

    if len(sys.argv) > 1:
        asin = sys.argv[1]
        print(f"Fetching item: {asin}")
        try:
            result = client.get_items([asin])
            print(json.dumps(result, indent=2, ensure_ascii=False))
        except Exception as e:
            print(f"Error: {e}")
    else:
        print("Usage: python creators_api_client.py <ASIN>")
        print("\nTesting SearchItems API...")
        try:
            result = client.search_items(keywords="PlayStation 5", search_index="VideoGames", item_count=1)
            print(json.dumps(result, indent=2, ensure_ascii=False))
        except Exception as e:
            print(f"Error: {e}")
