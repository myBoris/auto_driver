"""提供真实 HTTP 边界的假中心，记录动作和采样顺序而不创建游戏输入。

测试使用独立的本地随机端口，允许模拟 HTTP 错误、错误图像元数据和等待中
的训练步骤，以验证请求不能重复执行以及取消后的释放路径。
"""

import asyncio
import base64
import time

from aiohttp import web
from aiohttp.test_utils import TestServer


TOKEN = "ai-integration-test-token"
JPEG = b"\xff\xd8\xff\xe0fake-observation\xff\xd9"


class FakeCenter:
    def __init__(self):
        self.requests = []
        self.response_status = {}
        self.response_overrides = {}
        self.step_started = asyncio.Event()
        self.step_gate = None
        self.sequence = 1
        self.captured_at = time.time()
        self.received_at = self.captured_at + 0.01
        self.frame_headers = {
            "X-Frame-Sequence": str(self.sequence),
            "X-Frame-Timestamp": str(self.captured_at),
            "X-Frame-Received-At": str(self.received_at),
        }
        self.frame_body = JPEG
        self.auto_frame_sequence = False
        self.after_step = None
        self.disconnect_endpoints = set()
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", self.handle)
        self.server = TestServer(app)

    async def start(self):
        await self.server.start_server()
        return self

    @property
    def url(self):
        return str(self.server.make_url("/")).rstrip("/")

    async def close(self):
        if self.step_gate:
            self.step_gate.set()
        await self.server.close()

    def observation(self, sequence=None):
        return {
            "sequence": self.sequence if sequence is None else sequence,
            "captured_at": self.captured_at,
            "received_at": self.received_at,
            "jpeg_base64": base64.b64encode(JPEG).decode("ascii"),
            "content_type": "image/jpeg",
        }

    async def handle(self, request):
        endpoint = request.path.rsplit("/", 1)[-1]
        body = await request.json() if request.can_read_body else None
        self.requests.append({
            "method": request.method, "path": request.path,
            "raw_path": request.raw_path, "query": dict(request.query),
            "authorization": request.headers.get("Authorization"), "body": body,
        })
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return web.json_response({"error": "bad token"}, status=401)
        if endpoint in self.disconnect_endpoints:
            request.transport.abort()
            return web.Response()
        status = self.response_status.get(endpoint, 200)
        if status >= 400:
            return web.json_response({"error": f"simulated {endpoint} failure"}, status=status)
        if endpoint in self.response_overrides:
            override = self.response_overrides[endpoint]
            if isinstance(override, web.StreamResponse):
                return override
            return web.json_response(override)
        if endpoint == "terminals":
            return web.json_response({"terminals": [{"terminal_id": "game-pc", "connected": True,
                                                    "name": "Test game PC", "session_id": "session-1"}]})
        if endpoint == "frame":
            if self.auto_frame_sequence:
                self.sequence += 1
                self.frame_headers["X-Frame-Sequence"] = str(self.sequence)
            return web.Response(body=self.frame_body, headers=self.frame_headers, content_type="image/jpeg")
        if endpoint == "step":
            self.step_started.set()
            if self.step_gate is not None:
                await self.step_gate.wait()
            self.sequence += 1
            action = body["action"]
            applied_at = self.captured_at
            self.captured_at = applied_at + action.get("duration_ms", 200) / 1000 + 0.01
            self.received_at = self.captured_at + 0.01
            if self.after_step:
                self.after_step(self)
            return web.json_response({"terminal_id": "game-pc", "command_id": f"command-{self.sequence}",
                                      "ack": {"type": "ack", "command_id": f"command-{self.sequence}",
                                              "ok": True, "applied_at": applied_at, "state": action},
                                      "observation": self.observation()})
        if endpoint in {"actions", "release"}:
            state = {"steering": 0, "throttle": 0, "brake": 0, "buttons": [], "duration_ms": 0}
            state.update(body or {})
            return web.json_response({"terminal_id": "game-pc", "command_id": "action-command",
                                      "ack": {"type": "ack", "command_id": "action-command", "ok": True,
                                              "applied_at": time.time(), "state": state}})
        return web.json_response({"terminal_id": "game-pc", "name": "Test game PC",
                                  "connected": True, "session_id": "session-1"})
