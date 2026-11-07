"""独立中心客户端：严格解析观察、感知结果和动作确认，保持 POST 的结果语义。

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
        suffix = "; the request may already have been processed; POST was not retried" if self.may_have_executed else ""
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

    async def get_control(self, terminal_id: str, *, session_id: str | None = None,
                          after: int | None = None, timeout: float = 0) -> dict[str, Any]:
        """读取已确认的手柄输入供示范采集使用；不发送、续期或释放任何驾驶操作。"""
        params = {"timeout": str(finite_number(timeout, "timeout", 0, 20))}
        if after is not None:
            if isinstance(after, bool) or not isinstance(after, int) or after < 0:
                raise ValueError("after must be a nonnegative integer")
            params["after"] = str(after)
        if session_id is not None:
            if not isinstance(session_id, str) or not session_id:
                raise ValueError("session_id must be a non-empty string")
            params["session_id"] = session_id
        data = await self._json("GET", self._path(terminal_id) + "/control", params=params)
        try:
            if (data.get("terminal_id") != terminal_id or not isinstance(data.get("session_id"), str)
                    or not data["session_id"] or (session_id is not None and data["session_id"] != session_id)
                    or not isinstance(data.get("connected"), bool)
                    or data.get("status") not in {"idle", "active", "released", "expired", "unconfirmed", "disconnected"}
                    or isinstance(data.get("revision"), bool) or not isinstance(data.get("revision"), int) or data["revision"] < 0):
                raise ValueError("invalid control identity or revision")
            finite_number(data.get("remaining_ms"), "remaining_ms", 0, 5000)
            state = data.get("state")
            if not isinstance(state, dict) or set(state) != {"steering", "throttle", "brake", "buttons", "duration_ms"} or not isinstance(state["buttons"], list):
                raise ValueError("control state requires all action fields")
            state = Action(**{**state, "buttons": tuple(state["buttons"])}).to_payload()
            if data["status"] == "active":
                timestamp(data.get("applied_at"), "applied_at")
                if not isinstance(data.get("command_id"), str) or not data["command_id"] or state["duration_ms"] <= 0:
                    raise ValueError("active state requires command identity and positive duration")
        except (ValueError, TypeError) as exc:
            raise CenterProtocolError("invalid or mismatched control snapshot") from exc
        return {**data, "state": state}

    async def get_telemetry(self, terminal_id: str, *, session_id: str | None = None,
                            after: int | None = None, timeout: float = 0) -> dict[str, Any]:
        """读取 FH5 实测遥测；after 只返回更大的有效序号，超时状态不携带旧数据。"""
        params = {"timeout": str(finite_number(timeout, "timeout", 0, 20))}
        if after is not None:
            if isinstance(after, bool) or not isinstance(after, int) or after < 0:
                raise ValueError("after must be a nonnegative integer")
            params["after"] = str(after)
        if session_id is not None:
            if not isinstance(session_id, str) or not session_id:
                raise ValueError("session_id must be a non-empty string")
            params["session_id"] = session_id
        result = await self._json("GET", self._path(terminal_id) + "/telemetry", params=params)
        try:
            if (result.get("terminal_id") != terminal_id
                    or not isinstance(result.get("session_id"), str) or not result["session_id"]
                    or (session_id is not None and result["session_id"] != session_id)
                    or not isinstance(result.get("connected"), bool)
                    or result.get("status") not in {"ready", "waiting", "stale", "disconnected"}
                    or isinstance(result.get("sequence"), bool)
                    or not isinstance(result.get("sequence"), int) or result["sequence"] < 0):
                raise ValueError("invalid telemetry identity, status or sequence")
            ttl = finite_number(result.get("ttl_ms"), "ttl_ms", 1, 60000)
            if result.get("age_ms") is not None:
                finite_number(result["age_ms"], "age_ms", 0, 1e15)
            for name in ("captured_at", "received_at"):
                if result.get(name) is not None:
                    timestamp(result[name], name)
            if result["status"] == "disconnected" and result["connected"]:
                raise ValueError("disconnected telemetry requires disconnected terminal")
            value = result.get("data")
            if result["status"] != "ready":
                if value is not None:
                    raise ValueError("inactive telemetry must not contain data")
                return result
            if (not result["connected"] or result["sequence"] <= 0
                    or (after is not None and result["sequence"] <= after)
                    or result.get("age_ms") is None or result["age_ms"] > ttl
                    or result.get("captured_at") is None or result.get("received_at") is None):
                raise ValueError("ready telemetry requires fresh metadata and increasing sequence")
            required = {"source", "is_race_on", "timestamp_ms", "speed_mps", "position", "velocity", "yaw"}
            vectors = {"position", "velocity", "acceleration", "angular_velocity"}
            floats = {"speed_mps", "yaw", "pitch", "roll", "engine_rpm", "throttle", "brake",
                      "clutch", "handbrake", "steering"}
            if (not isinstance(value, dict) or not required <= value.keys()
                    or value.keys() - (required | vectors | floats | {"gear"})
                    or value["source"] != "fh5_data_out" or not isinstance(value["is_race_on"], bool)):
                raise ValueError("telemetry requires verified FH5 Data Out fields")
            clock = value["timestamp_ms"]
            if isinstance(clock, bool) or not isinstance(clock, int) or not 0 <= clock < 2**32:
                raise ValueError("invalid telemetry game timestamp")
            for name in vectors & value.keys():
                if not isinstance(value[name], list) or len(value[name]) != 3:
                    raise ValueError("invalid telemetry vector")
                for item in value[name]:
                    finite_number(item, name, -1e12, 1e12)
            for name in floats & value.keys():
                low, high = -1e12, 1e12
                if name in {"throttle", "brake", "clutch", "handbrake"}:
                    low, high = 0, 1
                elif name == "steering":
                    low, high = -1, 1
                elif name in {"speed_mps", "engine_rpm"}:
                    low = 0
                finite_number(value[name], name, low, high)
            if "gear" in value and (isinstance(value["gear"], bool) or not isinstance(value["gear"], int)
                                    or not 0 <= value["gear"] <= 255):
                raise ValueError("invalid telemetry gear")
        except (ValueError, TypeError) as exc:
            raise CenterProtocolError("invalid or mismatched telemetry snapshot") from exc
        return result

    @staticmethod
    def _perception(data: dict[str, Any], terminal_id: str,
                    expected_session: str | None = None) -> dict[str, Any]:
        """校验结果所属终端、会话和新鲜度元数据；不将感知值转换成驾驶动作。"""
        if (data.get("terminal_id") != terminal_id
                or not isinstance(data.get("session_id"), str) or not data["session_id"]
                or (expected_session is not None and data["session_id"] != expected_session)
                or data.get("status") not in ("ready", "no_road", "waiting", "stale", "unavailable")):
            raise CenterProtocolError("perception does not match requested terminal/session or status")
        try:
            finite_number(data.get("ttl_ms"), "ttl_ms", 1, 60000)
            if data.get("age_ms") is not None:
                finite_number(data["age_ms"], "age_ms", 0, 1e15)
            if data.get("received_at") is not None:
                timestamp(data["received_at"], "received_at")
            if data.get("inference_ms") is not None:
                finite_number(data["inference_ms"], "inference_ms", 0, 60000)
            frame = data.get("frame")
            if frame is not None:
                if not isinstance(frame, dict):
                    raise ValueError("frame must be an object")
                sequence = frame.get("sequence")
                if sequence is not None and (isinstance(sequence, bool) or not isinstance(sequence, int)
                                             or not 1 <= sequence <= 2**53 - 1):
                    raise ValueError("invalid frame sequence")
                if frame.get("captured_at") is not None:
                    timestamp(frame["captured_at"], "captured_at")
            road, prediction = data.get("road"), data.get("prediction")
            if road is not None and not isinstance(road, dict):
                raise ValueError("road must be an object or null")
            if prediction is not None:
                if (not isinstance(prediction, dict)
                        or prediction.get("source") not in {"model", "road_geometry", "external", "demo"}):
                    raise ValueError("invalid prediction source")
                finite_number(prediction.get("steering"), "steering", -1, 1)
                finite_number(prediction.get("confidence"), "confidence", 0, 1)
            if data["status"] in {"waiting", "stale", "unavailable"} and (road is not None or prediction is not None):
                raise ValueError("inactive perception must not retain road or prediction")
            if data["status"] in {"ready", "no_road"}:
                image, model = data.get("image"), data.get("model")
                if not isinstance(image, dict) or not isinstance(model, dict) or not isinstance(model.get("id"), str) or not model["id"]:
                    raise ValueError("missing perception image/model metadata")
                for name in ("width", "height"):
                    value = image.get(name)
                    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 16384:
                        raise ValueError("invalid image size")
                if data.get("received_at") is None or data.get("age_ms") is None:
                    raise ValueError("published perception requires freshness metadata")
                if data["status"] == "ready" and road is None and prediction is None:
                    raise ValueError("ready perception requires road or prediction")
                if data["status"] == "no_road" and road is not None:
                    raise ValueError("no_road perception must not contain road geometry")
        except (TypeError, ValueError) as exc:
            raise CenterProtocolError("invalid perception response metadata") from exc
        return data

    async def get_perception(self, terminal_id: str) -> dict[str, Any]:
        """读取最新道路/方向叠加结果，不取图、不发送游戏输入或释放操作。"""
        data = await self._json("GET", self._path(terminal_id) + "/perception")
        return self._perception(data, terminal_id)

    async def publish_perception(self, terminal_id: str, result: dict[str, Any]) -> dict[str, Any]:
        """发布当前会话的可视化数据；网络异常不自动重放 POST。"""
        if not isinstance(result, dict) or not isinstance(result.get("session_id"), str) or not result["session_id"]:
            raise ValueError("perception result requires the current terminal session_id")
        data = await self._json("POST", self._path(terminal_id) + "/perception", json=result)
        return self._perception(data, terminal_id, result["session_id"])

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

    @staticmethod
    def _command_session(session_id):
        if session_id is None:
            return {}
        if not isinstance(session_id, str) or not 1 <= len(session_id) <= 128:
            raise ValueError("session_id must be a nonempty string of at most 128 characters")
        return {"session_id": session_id}

    async def action(self, terminal_id: str, action: Action, *, session_id: str | None = None) -> dict[str, Any]:
        if not isinstance(action, Action):
            raise TypeError("action must be an Action")
        return self._ack(await self._json("POST", self._path(terminal_id) + "/actions", json=action.to_payload(),
                                          params=self._command_session(session_id)), terminal_id)

    async def release(self, terminal_id: str, *, session_id: str | None = None) -> dict[str, Any]:
        return self._ack(await self._json("POST", self._path(terminal_id) + "/release", json={},
                                          params=self._command_session(session_id)), terminal_id)

    async def step(self, terminal_id: str, action: Action, observation_timeout: float = 5, *,
                   session_id: str | None = None) -> StepResult:
        if not isinstance(action, Action):
            raise TypeError("action must be an Action")
        timeout = finite_number(observation_timeout, "observation_timeout", 0.1, 30)
        data = self._ack(await self._json("POST", self._path(terminal_id) + "/step",
                                        json={"action": action.to_payload(), "observation_timeout": timeout},
                                        params=self._command_session(session_id)), terminal_id)
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
