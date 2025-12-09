from __future__ import annotations

import os
import httpx
from typing import Any, Dict, Optional

try:
    from dotenv import load_dotenv, find_dotenv
    load_dotenv(find_dotenv(), override=False)
except Exception:
    pass


class SerpApiClient:
    """
    Thin client cho SerpAPI (engine=google_maps).
    - Tiêm api_key từ tham số hoặc env SERPAPI_KEY/SERP_API_KEY.
    - Thống nhất timeout/limits/header.
    """
    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        http2: bool = False,
        connect_timeout: float = 8.0,
        read_timeout: float = 12.0,
        pool_timeout: float = 12.0,
        max_connections: int = 8,
        max_keepalive: int = 8,
    ):
        self.api_key = api_key or os.getenv("SERPAPI_KEY") or os.getenv("SERP_API_KEY")
        self.base_url = "https://serpapi.com/search.json"
        self._client = httpx.AsyncClient(
            http2=http2,
            timeout=httpx.Timeout(connect=connect_timeout, read=read_timeout, write=12.0, pool=pool_timeout),
            limits=httpx.Limits(max_connections=max_connections, max_keepalive_connections=max_keepalive),
            headers={"User-Agent": "nestfeast-serpapi/0.3"},
        )

    async def aclose(self):
        await self._client.aclose()

    async def __aenter__(self):  # type: ignore
        return self

    async def __aexit__(self, exc_type, exc, tb):  # type: ignore
        await self.aclose()

    async def get_json(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if self.api_key and "api_key" not in params:
            params = {**params, "api_key": self.api_key}
        try:
            r = await self._client.get(self.base_url, params=params)
            r.raise_for_status()
            return r.json()
        except Exception:
            return {}
