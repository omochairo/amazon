#!/usr/bin/env python3
"""
Amazon Creators API Client

Creators API用クライアント。OAuth 2.0 Client Credentials認証を使用。
(Legacy: PAAPIClient)

環境変数:
    AMAZON_CREATORS_CREDENTIAL_ID: クレデンシャルID (OAuth client_id)
    AMAZON_CREATORS_CREDENTIAL_SECRET: クレデンシャルシークレット (OAuth client_secret)
    AMAZON_PARTNER_TAG: アソシエイトID
"""
import os
import json
import time
import base64
import requests
from typing import Any, Optional

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

    def _get_access_token(self) -> str:
        """Get OAuth 2.0 access token using client credentials flow."""
        # Return cached token if still valid (60 seconds buffer)
        if self._access_token and time.time() < self._token_expires_at - 60:
            return self._access_token

        # Check if using LwA (v3.x) or legacy Cognito (v2.x)
        is_lwa = self.credential_id.startswith("amzn1.application-oa2-client.")

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
            # Token expired, refresh for next attempt
            self._access_token = None
            headers.update(self._get_auth_headers())
            return True

        return False

    def _attempt_request(self, url: str, headers: dict, payload: dict) -> tuple[Optional[dict], bool]:
        """Perform a single API request attempt. Returns (response_json, should_retry)."""
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=30)

            if response.status_code == 200:
                return response.json(), False

            if response.status_code == 404:
                return response.json(), False

            if self._handle_retryable_error(response, headers):
                msg = "Rate limited" if response.status_code == 429 else ""
                return None, (msg or True)

            return None, True

        except requests.exceptions.RequestException as e:
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
        resources: list[str] = None
    ) -> dict:
        """Search for items.

        Args:
            keywords: Search keywords
            search_index: Category to search (e.g., 'Electronics', 'All')
            item_count: Number of results (1-10)
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
