"""
UAPI HTTP Client - Async HTTP client for calling uapis.cn APIs
"""
import aiohttp
import asyncio
from typing import Optional, Any
from astrbot.api import logger


class UAPIClient:
    """Async HTTP client for UAPI (uapis.cn) APIs."""

    def __init__(self, base_url: str = "https://uapis.cn", api_key: str = "", timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            )
        return self._session

    def _get_headers(self) -> dict:
        headers = {
            "User-Agent": "AstrBot-ManyAPIs/1.0",
            "Accept": "application/json",
        }
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    async def call(
        self,
        path: str,
        method: str = "GET",
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
        form_data: Optional[dict] = None,
    ) -> dict:
        """
        Call a UAPI endpoint.

        Args:
            path: API path, e.g. "/api/v1/misc/weather"
            method: HTTP method (GET, POST)
            params: Query parameters
            json_body: JSON request body (for POST)
            form_data: Multipart form data (for file uploads)

        Returns:
            dict with keys: success (bool), data (dict/bytes), error (str)
        """
        url = f"{self.base_url}{path}"
        headers = self._get_headers()

        try:
            session = await self._get_session()

            if method == "GET":
                async with session.get(url, params=params, headers=headers) as resp:
                    return await self._handle_response(resp)
            elif method == "POST":
                if form_data:
                    # Multipart form data
                    form = aiohttp.FormData()
                    for key, value in form_data.items():
                        if hasattr(value, "read"):
                            form.add_field(key, value, filename=getattr(value, "name", key))
                        else:
                            form.add_field(key, str(value))
                    async with session.post(url, data=form, headers=headers) as resp:
                        return await self._handle_response(resp)
                else:
                    headers["Content-Type"] = "application/json"
                    async with session.post(
                        url, params=params, json=json_body, headers=headers
                    ) as resp:
                        return await self._handle_response(resp)
            else:
                return {"success": False, "error": f"Unsupported method: {method}"}

        except asyncio.TimeoutError:
            logger.error(f"UAPI timeout: {method} {url}")
            return {"success": False, "error": "请求超时，请稍后重试"}
        except aiohttp.ClientError as e:
            logger.error(f"UAPI network error: {e}")
            return {"success": False, "error": f"网络请求失败: {str(e)}"}
        except Exception as e:
            logger.error(f"UAPI unexpected error: {e}")
            return {"success": False, "error": f"未知错误: {str(e)}"}

    async def _handle_response(self, resp: aiohttp.ClientResponse) -> dict:
        """Handle API response, supporting both JSON and binary."""
        content_type = resp.headers.get("Content-Type", "")

        # Check for binary responses: image, audio, octet-stream
        if any(ct in content_type for ct in ["image/", "audio/", "application/octet-stream"]):
            data = await resp.read()
            return {
                "success": True,
                "data": data,
                "content_type": content_type,
                "is_binary": True,
            }
        else:
            try:
                data = await resp.json()
                return {"success": resp.status < 400, "data": data, "status_code": resp.status}
            except Exception:
                text = await resp.text()
                return {"success": resp.status < 400, "data": text, "status_code": resp.status}

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
