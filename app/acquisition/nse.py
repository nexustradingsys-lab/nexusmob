import logging
import time
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)


class NseRawClient:
    BASE_URL = "https://www.nseindia.com"

    def __init__(self, timeout_seconds: int = 10, retries: int = 2, session=None):
        self.timeout = timeout_seconds
        self.retries = retries
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
            "Referer": f"{self.BASE_URL}/",
        })
        self.cookies_initialized = False

    def _initialize_session(self) -> None:
        response = self.session.get(self.BASE_URL, timeout=self.timeout)
        response.raise_for_status()
        self.cookies_initialized = True
        time.sleep(1.0)

    def fetch_endpoint(self, path: str, retries: Optional[int] = None) -> Dict[str, Any]:
        url = path if path.startswith("http") else f"{self.BASE_URL}{path if path.startswith('/') else '/' + path}"
        path_only = urlparse(url).path
        last_error = None
        retry_count = self.retries if retries is None else retries
        for attempt in range(retry_count + 1):
            try:
                if not self.cookies_initialized:
                    self._initialize_session()
                if "oi-spurts" in path_only:
                    self.session.headers["Referer"] = f"{self.BASE_URL}/market-data/oi-spurts"
                elif "snapshot" in path_only or "most-active" in path_only:
                    self.session.headers["Referer"] = f"{self.BASE_URL}/market-data/most-active-contracts"
                else:
                    self.session.headers["Referer"] = f"{self.BASE_URL}/"
                response = self.session.get(url, timeout=self.timeout)
                if response.status_code in (401, 403):
                    self.session.cookies.clear()
                    self.cookies_initialized = False
                    if attempt == retry_count:
                        response.raise_for_status()
                    time.sleep(1.5)
                    continue
                if response.status_code != 200:
                    response.raise_for_status()
                    raise requests.exceptions.HTTPError(
                        f"HTTP Error {response.status_code}", response=response
                    )
                response.raise_for_status()
                return response.json()
            except Exception as exc:
                last_error = exc
                self.cookies_initialized = False
                if attempt < retry_count:
                    time.sleep(1.5)
        raise RuntimeError(f"NSE request failed for {path_only}: {last_error}") from last_error

    def get_all_indices(self) -> Dict[str, Any]:
        return self.fetch_endpoint("/api/allIndices", retries=3)

    def fetch_datasets(self) -> Dict[str, Dict[str, Any]]:
        endpoints = {
            "oi_spurts_underlying": "/api/live-analysis-oi-spurts-underlyings",
            "oi_spurts_contracts": "/api/live-analysis-oi-spurts-contracts",
            "most_active_stock_calls": "/api/snapshot-derivatives-equity?index=calls-stocks-vol",
            "most_active_stock_puts": "/api/snapshot-derivatives-equity?index=puts-stocks-vol",
            "most_active_contracts_by_oi": "/api/snapshot-derivatives-equity?index=oi",
            "most_active_contracts": "/api/snapshot-derivatives-equity?index=contracts&limit=50",
            "most_active_futures": "/api/snapshot-derivatives-equity?index=futures",
            "most_active_underlyings": "/api/live-analysis-most-active-underlying",
        }
        results = {}
        for name, path in endpoints.items():
            results[name] = self.fetch_endpoint(path)
        return results
