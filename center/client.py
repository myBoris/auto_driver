"""供训练循环使用的轻量异步客户端，不依赖 PyTorch。"""

import base64
from dataclasses import dataclass
from urllib.parse import quote

import aiohttp


class CenterAPIError(RuntimeError):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(f"HTTP {status}: {message}")


@dataclass(frozen=True)
class Observation:
    jpeg: bytes
    sequence: int
    captured_at: float
    received_at: float


class CenterClient:
    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self.token = token
        self.session: aiohttp.ClientSession | None = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=aiohttp.ClientTimeout(total=65),
        )
        return self

    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()
            self.session = None

    def _session(self):
        if self.session is None:
            raise RuntimeError("use 'async with CenterClient(...) as client'")
        return self.session

    async def _json(self, method, path, **kwargs):
        async with self._session().request(method, self.url + path, **kwargs) as response:
            data = await response.json()
            if response.status >= 400:
                raise CenterAPIError(response.status, data.get("error", response.reason))
            return data

    @staticmethod
    def _path(terminal_id: str):
        return "/api/terminals/" + quote(terminal_id, safe="")

    async def terminals(self) -> list[dict]:
        return (await self._json("GET", "/api/terminals"))["terminals"]

    async def perception(self, terminal_id: str) -> dict:
        """读取道路检测和预测显示结果，不操作终端手柄。"""
        return await self._json("GET", self._path(terminal_id) + "/perception")

    async def publish_perception(self, terminal_id: str, result: dict) -> dict:
        """发布模型的可视化结果；result.session_id 必须匹配当前终端会话。"""
        return await self._json("POST", self._path(terminal_id) + "/perception", json=result)

    async def action(self, terminal_id: str, **action) -> dict:
        return await self._json("POST", self._path(terminal_id) + "/actions", json=action)

    async def release(self, terminal_id: str) -> dict:
        return await self._json("POST", self._path(terminal_id) + "/release", json={})

    async def observe(self, terminal_id: str, *, after: int | None = None,
                      timeout: float = 5) -> Observation:
        params = {"timeout": str(timeout)}
        if after is not None:
            params["after"] = str(after)
        async with self._session().get(self.url + self._path(terminal_id) + "/frame", params=params) as response:
            if response.status >= 400:
                data = await response.json()
                raise CenterAPIError(response.status, data.get("error", response.reason))
            return Observation(
                await response.read(), int(response.headers["X-Frame-Sequence"]),
                float(response.headers["X-Frame-Timestamp"]),
                float(response.headers["X-Frame-Received-At"]),
            )

    async def step(self, terminal_id: str, action: dict, *, observation_timeout: float = 5
                   ) -> tuple[Observation, dict]:
        result = await self._json("POST", self._path(terminal_id) + "/step", json={
            "action": action, "observation_timeout": observation_timeout,
        })
        data = result["observation"]
        observation = Observation(base64.b64decode(data["jpeg_base64"]), data["sequence"],
                                  data["captured_at"], data["received_at"])
        return observation, result["ack"]
