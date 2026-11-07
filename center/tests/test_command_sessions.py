"""动作会话约束在服务器取连接时校验，避免客户端检查后的重连竞态。"""

import asyncio
import time
import unittest

from aiohttp.test_utils import TestClient, TestServer

from center.app import create_app
from center.config import CenterConfig


TOKEN = "command-session-test"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
BASE = "/api/terminals/game-pc"


class BoundCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = TestClient(TestServer(create_app(CenterConfig(
            token=TOKEN, enable_discovery=False, media_enabled=False))))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def connect(self):
        ws = await self.client.ws_connect("/ws/terminal", headers=AUTH)
        await ws.send_json({"type": "hello", "protocol": 1, "terminal_id": "game-pc"})
        welcome = await ws.receive_json(timeout=1)
        return ws, welcome["session_id"]

    async def test_reconnect_rejects_old_session_for_actions_step_and_release_without_changing_input(self):
        old, previous = await self.connect()
        current, session = await self.connect()
        self.assertNotEqual(previous, session)
        for endpoint, payload in (("actions", {"throttle": .2}),
                                  ("step", {"action": {"throttle": .2}}), ("release", {})):
            response = await self.client.post(BASE + "/" + endpoint,
                params={"session_id": previous}, json=payload, headers=AUTH)
            self.assertEqual(response.status, 409)
            self.assertIn("command was not sent", (await response.json())["error"])
        response = await self.client.get(BASE + "/control", headers=AUTH)
        snapshot = await response.json()
        self.assertEqual(snapshot["session_id"], session)
        self.assertEqual(snapshot["revision"], 0)
        self.assertEqual(snapshot["status"], "idle")
        await current.close()
        await old.close()

    async def test_matching_session_reaches_only_that_connection_and_empty_binding_is_rejected(self):
        ws, session = await self.connect()
        response = await self.client.post(BASE + "/release", params={"session_id": ""}, json={}, headers=AUTH)
        self.assertEqual(response.status, 400)
        pending = asyncio.create_task(self.client.post(BASE + "/actions",
            params={"session_id": session}, json={"steering": .25, "duration_ms": 100}, headers=AUTH))
        command = await ws.receive_json(timeout=1)
        self.assertEqual(command["action"]["steering"], .25)
        await ws.send_json({"type": "ack", "command_id": command["command_id"], "ok": True,
                            "applied_at": time.time(), "state": command["action"]})
        response = await pending
        self.assertEqual(response.status, 200)
        await ws.close()
