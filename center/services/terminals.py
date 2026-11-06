"""终端业务服务：注册、完整动作执行与训练观察。

HTTP 和 WebSocket 适配器只负责读写消息，本服务处理连接替换、确认匹配、
动作串行化及释放取消。step 使用终端自身的执行时间和采集时间比较，只有
动作保持时长结束后采集的帧才可作为下一观察，避免跨电脑时钟差影响训练。
"""

import base64
import math
import time

from ..domain.errors import APIError
from ..domain.protocol import PROTOCOL, TERMINAL_ID, normalize_action, number, unpack_frame
from .terminal_session import TerminalSession


class TerminalService:
    def __init__(self, config, sessions):
        self.config = config
        self.sessions = sessions

    def get(self, terminal_id: str) -> TerminalSession:
        terminal = self.sessions.get(terminal_id)
        if terminal is None:
            raise APIError(404, "terminal not found")
        return terminal

    def list(self) -> list[dict]:
        return [terminal.describe() for terminal in self.sessions.values()]

    async def register(self, hello, ws) -> TerminalSession:
        if not isinstance(hello, dict) or hello.get("type") != "hello" or hello.get("protocol") != PROTOCOL:
            raise ValueError("expected hello with protocol=1")
        terminal_id = hello.get("terminal_id", "")
        if not isinstance(terminal_id, str) or not TERMINAL_ID.fullmatch(terminal_id):
            raise ValueError("terminal_id must contain 1..64 ASCII letters, digits, _, . or -")
        name = hello.get("name", terminal_id)
        capture = hello.get("capture", {})
        status = hello.get("status", {})
        capabilities = hello.get("capabilities", [])
        if not isinstance(name, str) or not 1 <= len(name) <= 128:
            raise ValueError("name must contain 1..128 characters")
        if not isinstance(capture, dict) or not isinstance(status, dict) or not isinstance(capabilities, list):
            raise ValueError("invalid terminal metadata")
        previous = self.sessions.get(terminal_id)
        terminal = TerminalSession(terminal_id, name, ws, self.config, capture=capture,
                                   status=status, capabilities=capabilities,
                                   sequence=previous.sequence if previous else 0)
        self.sessions[terminal_id] = terminal
        if previous and previous.connected:
            await previous.disconnect("terminal session replaced")
            await previous.ws.close(code=1000, message=b"session replaced")
        return terminal

    async def receive_frame(self, terminal: TerminalSession, packet: bytes) -> None:
        captured_at, jpeg = unpack_frame(packet)
        await terminal.receive_frame(captured_at, jpeg)

    def receive_message(self, terminal: TerminalSession, data) -> None:
        if not isinstance(data, dict):
            raise ValueError("terminal message must be an object")
        terminal.last_seen = time.time()
        if data.get("type") in {"heartbeat", "status"}:
            terminal.last_heartbeat = time.monotonic()
            if isinstance(data.get("status"), dict):
                terminal.status = data["status"]
            if isinstance(data.get("capture"), dict):
                terminal.capture = data["capture"]
        elif data.get("type") == "ack":
            command_id = data.get("command_id")
            future = terminal.pending.get(command_id) if isinstance(command_id, str) else None
            if future is not None and not future.done():
                if not isinstance(data.get("ok"), bool):
                    raise ValueError("ack.ok must be boolean")
                if data["ok"]:
                    applied_at = data.get("applied_at")
                    if (isinstance(applied_at, bool) or not isinstance(applied_at, (int, float))
                            or applied_at <= 0 or applied_at > 1e20 or not math.isfinite(applied_at)):
                        raise ValueError("ack.applied_at must be a positive finite timestamp")
                future.set_result(data)

    async def action(self, terminal_id: str, data) -> dict:
        terminal = self.get(terminal_id)
        action = normalize_action(data)
        epoch = terminal.control_epoch
        async with terminal.operation_lock:
            terminal.check_operation(epoch)
            return await terminal.command(action)

    async def release(self, terminal_id: str) -> dict:
        terminal = self.get(terminal_id)
        # 释放不等待训练的观察锁，并取消释放前已排队或等待确认的操作。
        await terminal.cancel_operations()
        return await terminal.command(normalize_action({"duration_ms": 0}))

    async def step(self, terminal_id: str, data: dict) -> dict:
        if data.keys() - {"action", "observation_timeout"}:
            raise APIError(400, "step accepts action and observation_timeout only")
        if "action" not in data:
            raise APIError(400, "step requires action")
        action = normalize_action(data["action"])
        timeout = number(data.get("observation_timeout", 5), "observation_timeout", 0.1, 30)
        terminal = self.get(terminal_id)
        epoch = terminal.control_epoch
        async with terminal.operation_lock:
            terminal.check_operation(epoch)
            result = await terminal.command(action)
            image = await terminal.get_frame(
                captured_after=result["ack"]["applied_at"] + action["duration_ms"] / 1000,
                timeout=timeout, control_epoch=epoch,
            )
            result["observation"] = {
                "sequence": image.sequence, "captured_at": image.captured_at,
                "received_at": image.received_at, "content_type": "image/jpeg",
                "jpeg_base64": base64.b64encode(image.jpeg).decode("ascii"),
            }
        return result
