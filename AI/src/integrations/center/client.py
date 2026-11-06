"""独立中心客户端：严格解析观察和动作确认，保持 POST 的执行结果语义。

网络失败可能发生在终端已执行动作之后，因此本适配器不自动重试 POST。
训练策略应根据异常决定停止、观察或显式释放，不能假设断线等于未执行。
"""

from __future__ import annotations

import base64
import binascii
from typing import Any
from urllib.parse import quote

import aiohttp

from ...config.settings import validate_base_url
from ...domain import Action, Observation, StepResult
from ...domain.types import finite_number, timestamp


class CenterAPIError(RuntimeError):
    """中心明确返回的 HTTP 失败；status 保留给训练服务判断离线、超时或参数错误。"""

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        super().__init__(f"HTTP {status}: {message}")


class CenterProtocolError(RuntimeError):
    """响应格式或执行确认不符合协议；不可把它解释成一次成功采样。"""


class CenterTransportError(RuntimeError):
    """请求未取得完整响应；POST 的 may_have_executed 为真，不应直接重放。"""

    def __init__(self, method: str) -> None:
        self.may_have_executed = method != "GET"
        suffix = "; the action may already have executed; POST was not retried" if self.may_have_executed else ""
        super().__init__(f"center connection failed or timed out{suffix}")


class CenterClient:
    """通过 async with 管理单个 aiohttp 会话；令牌只进入认证头，repr 不暴露它。"""

    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = validate_base_url(base_url)
        if not isinstance(token, str) or not token.strip() or "\r" in token or "\n" in token:
            raise ValueError("CENTER_TOKEN is required and must be a single-line bearer token")
        self._token = token
        self._session: aiohttp.ClientSession | None = None

    def __repr__(self) -> str:
        return f"CenterClient(base_url={self.base_url!r})"

    async def __aenter__(self) -> "CenterClient":
        if self._session is not None:
            raise RuntimeError("CenterClient context is already open")
        self._session = aiohttp.ClientSession(headers={"Authorization": f"Bearer {self._token}"},
                                              timeout=aiohttp.ClientTimeout(total=65, connect=5))
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _active_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("use 'async with CenterClient(...) as client' before requesting data")
        return self._session

    @staticmethod
    def _path(terminal_id: str) -> str:
        if not isinstance(terminal_id, str) or not terminal_id or len(terminal_id) > 64:
            raise ValueError("terminal_id must be a non-empty string of at most 64 characters")
        return "/api/terminals/" + quote(terminal_id, safe="")

    def _safe_message(self, value: Any) -> str:
        return str(value).replace(self._token, "<redacted>")[:500]

    async def _response_object(self, response: aiohttp.ClientResponse) -> dict[str, Any]:
        try:
            data = await response.json()
        except (ValueError, aiohttp.ContentTypeError) as exc:
            if response.status >= 400:
                raise CenterAPIError(response.status, "center returned a non-JSON error response") from None
            raise CenterProtocolError("center response must be a JSON object") from exc
        if response.status >= 400:
            message = data.get("error", response.reason) if isinstance(data, dict) else response.reason
            raise CenterAPIError(response.status, self._safe_message(message))
        if not isinstance(data, dict):
            raise CenterProtocolError("center response must be a JSON object")
        return data

    async def _json(self, method: str, path: str, **options: Any) -> dict[str, Any]:
        try:
            async with self._active_session().request(method, self.base_url + path, **options) as response:
                return await self._response_object(response)
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise CenterTransportError(method) from exc

    @staticmethod
    def _terminal(data: Any) -> dict[str, Any]:
        if (not isinstance(data, dict) or not isinstance(data.get("terminal_id"), str)
                or not isinstance(data.get("connected"), bool)):
            raise CenterProtocolError("terminal metadata requires terminal_id and boolean connected")
        return data

    async def list_terminals(self) -> list[dict[str, Any]]:
        data = await self._json("GET", "/api/terminals")
        if not isinstance(data.get("terminals"), list):
            raise CenterProtocolError("terminal list response requires a terminals array")
        return [self._terminal(item) for item in data["terminals"]]

    async def get_terminal(self, terminal_id: str) -> dict[str, Any]:
        data = self._terminal(await self._json("GET", self._path(terminal_id)))
        if data["terminal_id"] != terminal_id:
            raise CenterProtocolError("terminal metadata does not match requested terminal")
        return data

    async def observe(self, terminal_id: str, after: int | None = None, timeout: float = 5) -> Observation:
        timeout = finite_number(timeout, "timeout", 0.1, 30)
        if after is not None and (isinstance(after, bool) or not isinstance(after, int) or after < 0):
            raise ValueError("after must be a nonnegative integer")
        params = {"timeout": str(timeout)}
        if after is not None:
            params["after"] = str(after)
        try:
            async with self._active_session().get(self.base_url + self._path(terminal_id) + "/frame", params=params) as response:
                if response.status >= 400:
                    await self._response_object(response)
                if response.content_type != "image/jpeg":
                    raise CenterProtocolError("observation response must be image/jpeg")
                try:
                    observation = Observation(await response.read(), int(response.headers["X-Frame-Sequence"]),
                                              float(response.headers["X-Frame-Timestamp"]),
                                              float(response.headers["X-Frame-Received-At"]))
                except (KeyError, ValueError, TypeError, OverflowError) as exc:
                    raise CenterProtocolError("invalid observation JPEG or frame metadata") from exc
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise CenterTransportError("GET") from exc
        if after is not None and observation.sequence <= after:
            raise CenterProtocolError("center returned an observation older than the requested cursor")
        return observation

    @staticmethod
    def _ack(data: dict[str, Any], terminal_id: str) -> dict[str, Any]:
        ack = data.get("ack")
        command_id = data.get("command_id")
        if (data.get("terminal_id") != terminal_id or not isinstance(command_id, str) or not command_id
                or not isinstance(ack, dict) or ack.get("command_id") != command_id
                or ack.get("type") != "ack" or ack.get("ok") is not True):
            raise CenterProtocolError("invalid or unsuccessful action acknowledgement")
        try:
            timestamp(ack.get("applied_at"), "ack.applied_at")
            state = ack.get("state")
            if not isinstance(state, dict) or set(state) != {"steering", "throttle", "brake", "buttons", "duration_ms"}:
                raise ValueError("ack state must contain all action fields")
            if not isinstance(state["buttons"], list):
                raise ValueError("ack buttons must be a list")
            normalized = Action(steering=state["steering"], throttle=state["throttle"], brake=state["brake"],
                                buttons=tuple(state["buttons"]), duration_ms=state["duration_ms"]).to_payload()
        except (ValueError, TypeError) as exc:
            raise CenterProtocolError("invalid acknowledgement time or gamepad state") from exc
        return {**data, "ack": {**ack, "applied_at": float(ack["applied_at"]), "state": normalized}}

    async def action(self, terminal_id: str, action: Action) -> dict[str, Any]:
        if not isinstance(action, Action):
            raise TypeError("action must be an Action")
        return self._ack(await self._json("POST", self._path(terminal_id) + "/actions", json=action.to_payload()), terminal_id)

    async def release(self, terminal_id: str) -> dict[str, Any]:
        return self._ack(await self._json("POST", self._path(terminal_id) + "/release", json={}), terminal_id)

    async def step(self, terminal_id: str, action: Action, observation_timeout: float = 5) -> StepResult:
        if not isinstance(action, Action):
            raise TypeError("action must be an Action")
        timeout = finite_number(observation_timeout, "observation_timeout", 0.1, 30)
        data = self._ack(await self._json("POST", self._path(terminal_id) + "/step",
                                        json={"action": action.to_payload(), "observation_timeout": timeout}), terminal_id)
        value = data.get("observation")
        if not isinstance(value, dict) or value.get("content_type") != "image/jpeg":
            raise CenterProtocolError("step requires a JPEG observation")
        try:
            encoded = value["jpeg_base64"]
            if not isinstance(encoded, str):
                raise ValueError("JPEG base64 must be a string")
            observation = Observation(base64.b64decode(encoded, validate=True), value["sequence"],
                                      value["captured_at"], value["received_at"])
        except (KeyError, ValueError, TypeError, binascii.Error) as exc:
            raise CenterProtocolError("invalid step observation data") from exc
        if observation.captured_at < data["ack"]["applied_at"] + action.duration_ms / 1000:
            raise CenterProtocolError("step observation was captured before the action holding period finished")
        return StepResult(observation=observation, command_id=data["command_id"], ack=data["ack"])
