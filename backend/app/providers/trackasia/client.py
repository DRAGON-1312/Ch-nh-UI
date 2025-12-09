# Mục tiêu:  đảm bảo gọi API an toàn, có retry, timeout rõ ràng, 
# và không gây lỗi nghiêm trọng nếu API trả về lỗi.
"""Tổng quan chức năng:
- Gọi API TrackAsia qua HTTP GET với token xác thực.
- Xử lý lỗi mềm mại: không raise exception khi gặp lỗi HTTP.
- Tự động retry nếu gặp lỗi mạng hoặc lỗi server (5xx).
- Tự động timeout cứng để tránh treo bất tận.
- Hỗ trợ dùng như context manager (async with).
"""
from __future__ import annotations

import asyncio
import os
import random
from typing import Any, Dict, Optional
from urllib.parse import urljoin

import httpx

try:
    from dotenv import load_dotenv, find_dotenv
    load_dotenv(find_dotenv(), override=False)
except Exception:
    pass


# Đọc URL và token từ biến môi trường .env
DEFAULT_BASE_URL = os.getenv("TRACKASIA_BASE_URL", "https://maps.track-asia.com").rstrip("/") + "/"
DEFAULT_TOKEN = os.getenv("TRACKASIA_TOKEN", "").strip()


class TrackAsiaClient:
    """Tạo client HTTP bất đồng bộ với các thông số:
    - Timeout rõ ràng: connect, read, write, pool.
    - Retry có giới hạn: max_retries, backoff_base_s, backoff_cap_s.
    - Tùy chọn bật HTTP/2 qua biến môi trường.
    - Tự động thêm token vào query param nếu chưa có.
    """
    def __init__(
        self,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        *,
        # timeouts
        connect_timeout_s: float = 6.0,
        read_timeout_s: float = 8.0,
        write_timeout_s: float = 8.0,
        pool_timeout_s: float = 8.0,
        # retries (5xx + network) – giới hạn tổng để không “đứng hoài”
        max_retries: int = int(os.getenv("TRACKASIA_MAX_RETRIES", "1")),  # 1 retry = 2 lần gọi
        backoff_base_s: float = 0.4,
        backoff_cap_s: float = 1.2,
        # transport
        http2: Optional[bool] = None,
        client: Optional[httpx.AsyncClient] = None,
        query_param_name: str = "key",
        # hard deadline cho *mỗi lần* gọi (bao gồm retry)
        hard_timeout_s: float = float(os.getenv("TRACKASIA_HARD_TIMEOUT_S", "12.0")),
    ) -> None:
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/") + "/"
        self.token = DEFAULT_TOKEN if token is None else token
        self.query_param_name = query_param_name
        self.max_retries = max(0, max_retries)
        self.backoff_base_s = backoff_base_s
        self.backoff_cap_s = backoff_cap_s
        self.hard_timeout_s = hard_timeout_s

        # httpx client
        self._owns_client = client is None
        if client is None:
            http2_flag = http2
            if http2_flag is None:
                http2_flag = bool(int(os.getenv("TRACKASIA_HTTP2", "0")))  # mặc định tắt
            limits = httpx.Limits(max_connections=8, max_keepalive_connections=8)
            timeout = httpx.Timeout(
                connect=connect_timeout_s,
                read=read_timeout_s,
                write=write_timeout_s,
                pool=pool_timeout_s,
            )
            self.client = httpx.AsyncClient(
                http2=http2_flag,
                timeout=timeout,
                limits=limits,
                headers={"User-Agent": "nestfeast-trackasia/0.1"},
            )
        else:
            self.client = client

    # helpers
    
    # Ghép đường dẫn tương đối path với base_url để tạo URL đầy đủ.
    def _url(self, path: str) -> str:
        return urljoin(self.base_url, path.lstrip("/"))

    # Trộn các tham số query. Tự động thêm ?key=<TOKEN> nếu chưa có.
    def _merge_params(self, params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        p = dict(params or {})
        # chèn ?key=... nếu có token
        if self.token and self.query_param_name not in p:
            p[self.query_param_name] = self.token
        return p

    # Gọi HTTP GET một lần duy nhất.
    async def _one_try(self, method: str, path: str, params: Optional[Dict[str, Any]]) -> httpx.Response:
        url = self._url(path)
        if method.upper() != "GET":
            raise NotImplementedError("Only GET is implemented for now.")
        return await self.client.get(url, params=self._merge_params(params))

    """Đây là hàm chính để gọi API:
    - Nếu lỗi mạng, server, timeout → trả dict chứa "error": ... thay vì raise exception.
    - Nếu thành công → trả JSON (dict).
    - Nếu lỗi JSON → trả {"error": "bad_json"}.
    - Có timeout cứng bằng asyncio.wait_for().
    """
    async def get_json(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not self.token:
            return {"error": "missing_token"}

        async def _run_once() -> Dict[str, Any]:
            attempts = self.max_retries + 1
            for i in range(attempts):
                try:
                    resp = await self._one_try("GET", path, params)
                    # Không raise_for_status — tự xử lý
                    if 200 <= resp.status_code < 300:
                        # một số endpoint trả text/plain khi lỗi — try/except
                        try:
                            return resp.json()
                        except Exception:
                            return {"error": "bad_json"}
                    else:
                        # đọc body lỗi (nếu có) để tiện debug
                        try:
                            body = resp.json()
                        except Exception:
                            body = {"message": resp.text[:200]}
                        # 4xx không retry, 5xx sẽ retry
                        if 400 <= resp.status_code < 500:
                            return {"error": "http_client", "status": resp.status_code, "body": body}
                        # 5xx → có thể retry
                        err = {"error": "http_server", "status": resp.status_code, "body": body}
                        if i < attempts - 1:
                            # backoff có jitter, nhưng trần thấp để không “đứng”
                            sleep_s = min(self.backoff_cap_s, self.backoff_base_s * (2 ** i)) * (0.75 + random.random() * 0.5)
                            await asyncio.sleep(sleep_s)
                            continue
                        return err
                except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout):
                    if i < attempts - 1:
                        await asyncio.sleep(0.3)
                        continue
                    return {"error": "timeout"}
                except httpx.HTTPError as e:
                    if i < attempts - 1:
                        await asyncio.sleep(0.3)
                        continue
                    return {"error": "network", "detail": str(e)[:200]}
            return {"error": "unknown"}

        try:
            return await asyncio.wait_for(_run_once(), timeout=self.hard_timeout_s)
        except asyncio.TimeoutError:
            return {"error": "timeout_hard"}

    # Đóng client HTTP nếu client được tạo nội bộ.
    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    # Cho phép dùng async with TrackAsiaClient() as client: để tự động đóng sau khi dùng.
    async def __aenter__(self) -> "TrackAsiaClient":
        return self
    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()
