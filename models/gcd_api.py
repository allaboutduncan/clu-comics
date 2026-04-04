"""
GCD REST API client for comics.org.

Provides HTTP client for the Grand Comics Database REST API,
separate from the MySQL-based GCD integration in gcd.py.
"""
import requests
from requests.auth import HTTPBasicAuth
from urllib.parse import urlparse, parse_qs, quote
from typing import Optional, Dict, Any, List
from core.app_logging import app_logger


BASE_URL = "https://www.comics.org/api"


class GCDApiClient:
    """HTTP client for the GCD REST API at comics.org."""

    def __init__(self, username: str, password: str):
        self.session = requests.Session()
        self.session.auth = HTTPBasicAuth(username, password)
        self.session.headers.update({"Accept": "application/json"})

    def _get(self, path: str, params: dict = None) -> Optional[Dict]:
        """Make authenticated GET request to the GCD API."""
        url = f"{BASE_URL}{path}"
        try:
            resp = self.session.get(url, params=params or {}, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            app_logger.error(f"GCD API HTTP error for {path}: {e}")
            raise
        except requests.exceptions.Timeout:
            app_logger.error(f"GCD API timeout for {path}")
            raise
        except requests.exceptions.RequestException as e:
            app_logger.error(f"GCD API request error for {path}: {e}")
            raise

    def search_series(self, name: str, year: int = None) -> List[Dict]:
        """Search series by name, optionally filtered by year."""
        encoded_name = quote(name, safe="")
        if year:
            path = f"/series/name/{encoded_name}/year/{year}/"
        else:
            path = f"/series/name/{encoded_name}/"
        return self._get_all_pages(path)

    def get_series(self, series_id: int) -> Optional[Dict]:
        """Get series details by ID, including issue list."""
        return self._get(f"/series/{series_id}/")

    def get_issue(self, issue_id: int) -> Optional[Dict]:
        """Get full issue details including stories and credits."""
        return self._get(f"/issue/{issue_id}/")

    def search_issue(self, series_name: str, issue_number: str, year: int = None) -> List[Dict]:
        """Search for an issue by series name and issue number."""
        encoded_name = quote(series_name, safe="")
        encoded_number = quote(str(issue_number), safe="")
        if year:
            path = f"/series/name/{encoded_name}/issue/{encoded_number}/year/{year}/"
        else:
            path = f"/series/name/{encoded_name}/issue/{encoded_number}/"
        return self._get_all_pages(path)

    def get_publisher(self, publisher_id: int) -> Optional[Dict]:
        """Get publisher details by ID."""
        return self._get(f"/publisher/{publisher_id}/")

    def _get_all_pages(self, path: str, max_pages: int = 5) -> List[Dict]:
        """Fetch paginated results up to max_pages."""
        results = []
        params = {}
        for _ in range(max_pages):
            data = self._get(path, params)
            if not data:
                break
            results.extend(data.get("results", []))
            next_url = data.get("next")
            if not next_url:
                break
            parsed = parse_qs(urlparse(next_url).query)
            page = parsed.get("page", [None])[0]
            if page:
                params["page"] = page
            else:
                break
        return results
