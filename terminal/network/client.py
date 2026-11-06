"""网络会话：发现与认证、观察上传、动作确认、心跳和断线停推。

这里不访问采集 SDK 或 FFmpeg；中心下发视频地址后委托采集管理器处理。
JPEG 观察、视频发布和控制命令分别运行，单路背压不会累积历史动作。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections import deque
from typing import Any

import aiohttp

from ..capture import LatestCapture
from ..control import Controller
from .discovery import discover_center
from ..config.connection import TerminalConfig

LOG = logging.getLogger(__name__)


class TerminalClient:
    """一次连接对应一次发布授权；结束连接必停推并释放手柄，再进入重连。"""
    def __init__(self, config: TerminalConfig, capture: LatestCapture, controller: Controller) -> None:
        self.config = config
        self.capture = capture
        self.controller = controller
        self.stop_event = asyncio.Event()

    def status(self) -> dict[str, Any]:
        return {"capture": self.capture.status(), "control": self.controller.status(),
                "video": self.capture.video_status(), "mock": self.config.mock}

    async def _watch_stop_file(self) -> None:
        while not self.stop_event.is_set():
            if self.config.stop_file is not None and self.config.stop_file.exists():
                self.stop_event.set()
                return
            await asyncio.sleep(0.2)

    async def _frames(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        # 采集线程通知事件循环，不使用低精度定时轮询；通知可合并，帧也只读最新。
        loop = asyncio.get_running_loop()
        available = asyncio.Event()

        def notify() -> None:
            try:
                loop.call_soon_threadsafe(available.set)
            except RuntimeError:
                # 退出时最后一帧发布可能与移除监听并发；已关闭事件循环无需再次唤醒。
                pass

        previous_sequence = -1
        self.capture.add_frame_listener(notify)
        try:
            while True:
                available.clear()
                frame = self.capture.latest()
                if frame is not None and frame.sequence != previous_sequence and self.capture.status()["ready"]:
                    await ws.send_bytes(frame.packet())
                    previous_sequence = frame.sequence
                    continue
                await available.wait()
        finally:
            self.capture.remove_frame_listener(notify)

    async def _heartbeat(self, ws: aiohttp.ClientWebSocketResponse, interval: float) -> None:
        while True:
            status = self.status()
            await ws.send_json({"type": "heartbeat", "status": status, "capture": status["capture"]})
            await asyncio.sleep(interval)

    async def _commands(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        seen: set[str] = set()
        order: deque[str] = deque()
        async for message in ws:
            if message.type == aiohttp.WSMsgType.ERROR:
                raise ConnectionError("center WebSocket failed") from ws.exception()
            if message.type != aiohttp.WSMsgType.TEXT:
                continue
            try:
                value = json.loads(message.data)
                if not isinstance(value, dict):
                    raise ValueError("command must be a JSON object")
                if value.get("type") != "action":
                    continue
                command_id = value.get("command_id")
                if not isinstance(command_id, str) or not 1 <= len(command_id) <= 128:
                    raise ValueError("invalid command_id")
                ack: dict[str, Any] = {"type": "ack", "command_id": command_id}
                try:
                    if command_id in seen:
                        raise ValueError("duplicate command_id")
                    seen.add(command_id)
                    order.append(command_id)
                    if len(order) > 256:
                        seen.discard(order.popleft())
                    # ACK 表示驱动已提交，而非仅收到网络消息；失败不伪造成功执行时间。
                    state, applied_at = self.controller.apply(value.get("action"), value.get("expires_at"))
                    ack.update(ok=True, state=state, applied_at=applied_at)
                except Exception as exc:
                    ack.update(ok=False, error=str(exc))
                    LOG.warning("Command %s rejected: %s", command_id, exc)
                await ws.send_json(ack)
            except (ValueError, TypeError) as exc:
                LOG.warning("Ignoring malformed center message: %s", exc)
        raise ConnectionError("center connection closed")

    async def _connected(self, session: aiohttp.ClientSession, center: str) -> None:
        headers = {"Authorization": f"Bearer {self.config.token}"}
        async with session.ws_connect(f"{center}/ws/terminal", headers=headers,
                                      heartbeat=10, max_msg_size=64 * 1024) as ws:
            await ws.send_json({"type": "hello", "protocol": 1, "terminal_id": self.config.terminal_id,
                                "name": self.config.name, "capabilities": ["capture", "gamepad"],
                                "capture": self.capture.status(), "status": self.status()})
            message = await ws.receive(timeout=10)
            if message.type != aiohttp.WSMsgType.TEXT:
                raise ConnectionError("center did not send welcome")
            welcome = json.loads(message.data)
            if not isinstance(welcome, dict) or welcome.get("type") != "welcome" or welcome.get("protocol") != 1:
                raise ConnectionError("center protocol handshake failed")
            video = welcome.get("video", {})
            if not isinstance(video, dict):
                raise ValueError("welcome.video must be an object")
            self.capture.configure_video(video)
            interval = welcome.get("heartbeat_interval", 5)
            if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not 0.5 <= interval <= 30:
                interval = 5
            LOG.info("Connected to %s as %s (session %s)", center, self.config.terminal_id, welcome.get("session_id"))
            tasks = [asyncio.create_task(self._frames(ws)), asyncio.create_task(self._heartbeat(ws, interval)),
                     asyncio.create_task(self._commands(ws)), asyncio.create_task(self.stop_event.wait())]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                self.capture.configure_video({"enabled": False})
                self.controller.release()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def run(self) -> None:
        self.capture.start()
        watcher = asyncio.create_task(self._watch_stop_file())
        try:
            timeout = aiohttp.ClientTimeout(total=None, connect=5, sock_connect=5)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                failures = 0
                while not self.stop_event.is_set():
                    try:
                        center = self.config.center or await discover_center(
                            port=self.config.discovery_port, address=self.config.discovery_address)
                        await self._connected(session, center)
                        failures = 0
                    except (aiohttp.ClientError, OSError, TimeoutError, ValueError, ConnectionError) as exc:
                        self.controller.release()
                        failures += 1
                        LOG.warning("Center unavailable (%s: %s); reconnecting", type(exc).__name__, exc)
                    if not self.stop_event.is_set():
                        delay = min(10, 0.5 * 2 ** min(failures, 4)) + random.uniform(0, 0.5)
                        try:
                            await asyncio.wait_for(self.stop_event.wait(), timeout=delay)
                        except TimeoutError:
                            pass
        finally:
            self.capture.configure_video({"enabled": False})
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            self.controller.close()
            await asyncio.to_thread(self.capture.close)
            LOG.info("Terminal stopped; controller inputs released")
