"""封装 MediaMTX 的 WHEP HTTP 访问，业务层不直接操作上游凭据和地址。

中心只把本地会话地址交给浏览器；上游 Location 保存在服务器内，并限制在
配置的 MediaMTX 源和对应终端路径内，防止代理请求越过媒体服务边界。
"""

import base64
from urllib.parse import unquote, urljoin, urlsplit

import aiohttp

from ..config import CenterConfig
from ..domain.errors import APIError


class MediaGateway:
    def __init__(self, config: CenterConfig):
        self.config = config
        self.client: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        self.client = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10, connect=3),
            headers={"Authorization": "Basic " + base64.b64encode(
                ("reader:" + self.config.token).encode("utf-8")).decode("ascii")},
        )

    async def close(self) -> None:
        if self.client:
            await self.client.close()

    def _session(self) -> aiohttp.ClientSession:
        if self.client is None:
            raise RuntimeError("media gateway is not started")
        return self.client

    def _session_url(self, request_url: str, location: str, path: str) -> str:
        target = urljoin(request_url, location)
        expected = urlsplit(self.config.media_http_url)
        parsed = urlsplit(target)
        prefix = f"/{path}/whep/"
        decoded = unquote(parsed.path)
        def origin(value):
            return value.scheme, value.hostname, value.port or (443 if value.scheme == "https" else 80)
        if (origin(parsed) != origin(expected)
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or not decoded.startswith(prefix) or decoded == prefix
                or any(part in {".", ".."} for part in decoded.split("/"))):
            raise APIError(502, "media service returned an invalid session address")
        return target

    async def create(self, path: str, offer: str) -> tuple[str, str, str | None]:
        url = f"{self.config.media_http_url.rstrip('/')}/{path}/whep"
        try:
            async with self._session().post(
                url, data=offer.encode(), headers={"Content-Type": "application/sdp"}, allow_redirects=False,
            ) as response:
                if response.status in {404, 409, 503}:
                    raise APIError(503, "video stream is not ready; retry after the publisher connects")
                if response.status != 201:
                    raise APIError(502, "media service rejected video negotiation")
                # read(n) 只保证至多 n 字节，可能在第一个 HTTP 分块就返回。
                # 逐块读到 EOF 才是完整 SDP，同时在每块追加前限制总容量。
                data = bytearray()
                async for chunk in response.content.iter_chunked(16 * 1024):
                    if len(data) + len(chunk) > 256 * 1024:
                        raise APIError(502, "media service returned an oversized SDP answer")
                    data.extend(chunk)
                answer = data.decode("utf-8")
                location = response.headers.get("Location", "")
                if not location or not answer.startswith("v=0") or "m=video" not in answer:
                    raise APIError(502, "media service returned an invalid SDP answer")
                session_url = self._session_url(url, location, path)
                return answer, session_url, response.headers.get("ETag")
        except (aiohttp.ClientError, TimeoutError, UnicodeError) as exc:
            # 不返回底层异常文本，避免其中包含上游请求详情或认证信息。
            raise APIError(502, "media service is unavailable") from exc

    async def delete(self, session_url: str) -> None:
        try:
            async with self._session().delete(
                session_url, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=3),
            ) as response:
                if response.status not in {200, 204, 404, 410}:
                    raise APIError(502, "media service could not release video session")
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise APIError(502, "media service is unavailable") from exc
