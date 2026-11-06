"""每个连接独立保存最新画面、待确认操作与生命周期。"""

import asyncio
from dataclasses import dataclass, field
import time
import uuid

from aiohttp import web

from .config import CenterConfig


class APIError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)


@dataclass
class Frame:
    sequence: int
    captured_at: float
    received_at: float
    jpeg: bytes


@dataclass
class TerminalSession:
    terminal_id: str
    name: str
    ws: web.WebSocketResponse
    config: CenterConfig
    capture: dict = field(default_factory=dict)
    status: dict = field(default_factory=dict)
    capabilities: list = field(default_factory=list)
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    connected_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    last_heartbeat: float = field(default_factory=time.monotonic)
    connected: bool = True
    sequence: int = 0
    control_epoch: int = 0
    frame: Frame | None = None
    pending: dict = field(default_factory=dict)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    operation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def describe(self) -> dict:
        return {
            "terminal_id": self.terminal_id, "name": self.name,
            "session_id": self.session_id, "connected": self.connected,
            "connected_at": self.connected_at, "last_seen": self.last_seen,
            "frame_sequence": self.sequence,
            "frame_timestamp": self.frame.captured_at if self.frame else None,
            "width": self.capture.get("width"), "height": self.capture.get("height"),
            "capture": self.capture, "status": self.status, "capabilities": self.capabilities,
        }

    async def receive_frame(self, captured_at: float, jpeg: bytes) -> None:
        async with self.condition:
            self.sequence += 1
            self.frame = Frame(self.sequence, captured_at, time.time(), jpeg)
            self.last_seen = time.time()
            self.condition.notify_all()

    async def disconnect(self, reason: str = "terminal disconnected") -> None:
        async with self.condition:
            self.connected = False
            self.frame = None
            self.condition.notify_all()
        for future in list(self.pending.values()):
            if not future.done():
                future.set_exception(APIError(409, reason))
        self.pending.clear()

    async def cancel_operations(self) -> None:
        async with self.condition:
            self.control_epoch += 1
            self.condition.notify_all()
        for future in list(self.pending.values()):
            if not future.done():
                future.set_exception(APIError(409, "operation cancelled by release"))

    def check_operation(self, epoch: int) -> None:
        if epoch != self.control_epoch:
            raise APIError(409, "operation cancelled by release")

    async def get_frame(self, *, after: int | None = None,
                        captured_after: float | None = None, timeout: float = 0,
                        control_epoch: int | None = None) -> Frame:
        def ready():
            return (self.frame is not None
                    and time.time() - self.frame.received_at <= self.config.max_frame_age
                    and (after is None or self.frame.sequence > after)
                    and (captured_after is None or self.frame.captured_at >= captured_after))

        async def wait_for_frame():
            async with self.condition:
                while True:
                    if control_epoch is not None:
                        self.check_operation(control_epoch)
                    if not self.connected:
                        raise APIError(409, "terminal disconnected")
                    if ready():
                        return self.frame
                    await self.condition.wait()

        if not self.connected:
            raise APIError(409, "terminal disconnected")
        if control_epoch is not None:
            self.check_operation(control_epoch)
        if ready():
            return self.frame
        if timeout <= 0:
            raise APIError(503, "no matching frame available yet")
        try:
            async with asyncio.timeout(timeout):
                return await wait_for_frame()
        except TimeoutError as exc:
            raise APIError(504, "timed out waiting for a fresh frame") from exc

    async def command(self, action: dict) -> dict:
        if not self.connected or self.ws.closed:
            raise APIError(409, "terminal disconnected")
        command_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[command_id] = future
        try:
            async with asyncio.timeout(self.config.command_timeout):
                await self.ws.send_json({
                    "type": "action", "command_id": command_id, "action": action,
                    "expires_at": time.time() + self.config.command_timeout,
                })
                ack = await future
            if not ack["ok"]:
                raise APIError(502, str(ack.get("error", "terminal rejected action")))
            return {"terminal_id": self.terminal_id, "command_id": command_id, "ack": ack}
        except TimeoutError as exc:
            raise APIError(504, "terminal did not acknowledge action before deadline") from exc
        except (ConnectionError, RuntimeError) as exc:
            raise APIError(409, "terminal connection closed") from exc
        finally:
            self.pending.pop(command_id, None)
            if not future.done():
                future.cancel()
