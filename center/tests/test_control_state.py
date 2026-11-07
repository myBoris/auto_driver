"""真实 HTTP/终端 WebSocket 验证已执行输入的实时读取与生命周期。"""

import asyncio
import struct
import time
import unittest

from aiohttp.test_utils import TestClient, TestServer

from center.app import create_app
from center.config import CenterConfig
from center.dependencies import SESSIONS


TOKEN = "control-read-test"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
PATH = "/api/terminals/game-pc/control"
NEUTRAL = {"steering": 0, "throttle": 0, "brake": 0,
           "buttons": [], "duration_ms": 0}


class ControlStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = TestClient(TestServer(create_app(CenterConfig(
            token=TOKEN, enable_discovery=False, media_enabled=False,
            command_timeout=.3,
        ))))
        await self.client.start_server()
        self.ws = await self.connect()

    async def asyncTearDown(self):
        await self.client.close()

    async def connect(self, terminal_id="game-pc"):
        ws = await self.client.ws_connect("/ws/terminal", headers=AUTH)
        await ws.send_json({"type": "hello", "protocol": 1,
                            "terminal_id": terminal_id, "name": "Control test"})
        await ws.receive_json(timeout=1)
        return ws

    async def read(self, *, path=PATH, **params):
        response = await self.client.get(path, headers=AUTH, params=params)
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        return await response.json()

    def post(self, action=None, endpoint="actions"):
        return asyncio.create_task(self.client.post(
            f"/api/terminals/game-pc/{endpoint}", headers=AUTH,
            json=action if action is not None else {},
        ))

    async def ack(self, command, *, ok=True, state=None, applied_at=None, ws=None):
        value = {"type": "ack", "command_id": command["command_id"], "ok": ok,
                 "applied_at": time.time() if applied_at is None else applied_at,
                 "state": command["action"] if state is None else state}
        if not ok:
            value["error"] = "controller failed"
        await (ws or self.ws).send_json(value)

    async def apply(self, action):
        request = self.post(action)
        command = await self.ws.receive_json(timeout=1)
        await self.ack(command)
        response = await request
        self.assertEqual(response.status, 200, await response.text())
        return command

    async def test_initial_snapshot_and_authentication_without_sending_commands(self):
        terminal = self.client.server.app[SESSIONS]["game-pc"]
        seen = terminal.last_seen
        initial = await self.read()
        self.assertEqual(initial["status"], "idle")
        self.assertEqual(initial["state"], NEUTRAL)
        self.assertEqual(initial["remaining_ms"], 0)
        self.assertEqual(initial["revision"], 0)
        self.assertTrue(initial["connected"])
        self.assertEqual(initial["session_id"], terminal.session_id)
        self.assertIsNone(initial["command_id"])
        self.assertEqual(terminal.last_seen, seen)
        self.assertEqual(terminal.pending, {})
        response = await self.client.get(PATH)
        self.assertEqual(response.status, 401)
        await self.client.get("/dashboard-session")
        self.assertEqual((await self.client.get(PATH)).status, 200)
        self.assertEqual((await self.client.get(PATH, headers={"Authorization": "Bearer bad"})).status, 401)
        self.assertEqual((await self.client.get("/api/terminals/missing/control", headers=AUTH)).status, 404)

    async def test_ack_wakes_multiple_readers_and_uses_actual_state_and_monotonic_lifetime(self):
        initial = await self.read()
        polls = [asyncio.create_task(self.read(after=initial["revision"], timeout=1))
                 for _ in range(2)]
        request = self.post({"throttle": 1, "duration_ms": 5000})
        command = await self.ws.receive_json(timeout=1)
        self.assertEqual((await self.read())["status"], "idle", "sending is not execution")
        actual = {"steering": -.42, "throttle": .65, "brake": .18,
                  "buttons": ["LB", "X"], "duration_ms": 5000}
        await self.ack(command, state=actual, applied_at=1)
        self.assertEqual((await request).status, 200)
        for result in await asyncio.wait_for(asyncio.gather(*polls), timeout=1):
            self.assertEqual(result["status"], "active")
            self.assertEqual(result["state"], actual)
            self.assertEqual(result["command_id"], command["command_id"])
            self.assertEqual(result["applied_at"], 1)
            self.assertGreater(result["remaining_ms"], 4500)
            self.assertLessEqual(result["remaining_ms"], 5000)
            self.assertEqual(result["revision"], 1)
        # 重复 ACK 和未知指令不能续期或覆盖已确认状态。
        await self.ack(command, state={"throttle": 1, "duration_ms": 5000})
        await self.ws.send_json({"type": "ack", "command_id": "unknown", "ok": True})
        same = await self.read(after=1, timeout=.02)
        self.assertEqual(same["revision"], 1)
        self.assertEqual(same["state"], actual)

    async def test_legacy_ack_without_state_uses_normalized_command(self):
        request = self.post({"steering": .3, "duration_ms": 500})
        command = await self.ws.receive_json(timeout=1)
        await self.ws.send_json({"type": "ack", "command_id": command["command_id"],
                                 "ok": True, "applied_at": time.time()})
        self.assertEqual((await request).status, 200)
        self.assertEqual((await self.read())["state"], command["action"])

    async def test_expiry_wakes_reader_and_replacement_cancels_old_timer(self):
        await self.apply({"throttle": .6, "duration_ms": 80})
        active = await self.read()
        expired = await asyncio.wait_for(self.read(after=active["revision"], timeout=1), 1)
        self.assertEqual(expired["status"], "expired")
        self.assertEqual(expired["state"], NEUTRAL)
        self.assertEqual(expired["remaining_ms"], 0)
        await self.apply({"throttle": .4, "duration_ms": 100})
        terminal = self.client.server.app[SESSIONS]["game-pc"]
        old_timer = terminal.control.timer
        await self.apply({"brake": .8, "duration_ms": 5000})
        self.assertTrue(old_timer.cancelled())
        new = await self.read()
        after_old_deadline = await self.read(after=new["revision"], timeout=.12)
        self.assertEqual(after_old_deadline["status"], "active")
        self.assertEqual(after_old_deadline["revision"], new["revision"])
        self.assertEqual(after_old_deadline["state"]["brake"], .8)

    async def test_release_cancels_pending_ack_without_reviving_old_input(self):
        await self.apply({"throttle": .7, "duration_ms": 5000})
        pending = self.post({"steering": -.8, "duration_ms": 5000})
        old_command = await self.ws.receive_json(timeout=1)
        release = self.post(endpoint="release")
        release_command = await self.ws.receive_json(timeout=1)
        self.assertEqual((await pending).status, 409)
        before_ack = await self.read()
        self.assertEqual(before_ack["status"], "unconfirmed")
        self.assertEqual(before_ack["state"], NEUTRAL)
        await self.ack(release_command)
        self.assertEqual((await release).status, 200)
        released = await self.read()
        self.assertEqual(released["status"], "released")
        self.assertEqual(released["state"], NEUTRAL)
        await self.ack(old_command)
        later = await self.read(after=released["revision"], timeout=.02)
        self.assertEqual(later["revision"], released["revision"])
        self.assertEqual(later["status"], "released")

    async def test_rejected_and_unacknowledged_commands_clear_display(self):
        await self.apply({"throttle": .7, "duration_ms": 5000})
        failed = self.post({"brake": .3, "duration_ms": 5000})
        command = await self.ws.receive_json(timeout=1)
        await self.ack(command, ok=False)
        self.assertEqual((await failed).status, 502)
        rejected = await self.read()
        self.assertEqual(rejected["status"], "unconfirmed")
        self.assertEqual(rejected["state"], NEUTRAL)
        await self.apply({"throttle": .2, "duration_ms": 5000})
        pending = self.post({"brake": .5, "duration_ms": 5000})
        command = await self.ws.receive_json(timeout=1)
        self.assertEqual((await pending).status, 504)
        timed_out = await self.read()
        self.assertEqual(timed_out["status"], "unconfirmed")
        self.assertEqual(timed_out["state"], NEUTRAL)
        await self.ack(command)
        self.assertEqual((await self.read(after=timed_out["revision"], timeout=.02))["revision"],
                         timed_out["revision"])

    async def test_step_displays_ack_before_waiting_for_observation(self):
        request = self.post({"action": {"steering": .5, "throttle": .4, "duration_ms": 80},
                             "observation_timeout": 1}, endpoint="step")
        command = await self.ws.receive_json(timeout=1)
        applied_at = time.time()
        await self.ack(command, applied_at=applied_at)
        active = await self.read(after=0, timeout=1)
        self.assertEqual(active["status"], "active")
        self.assertEqual(active["state"]["steering"], .5)
        self.assertFalse(request.done(), "step should still be awaiting a fresh observation")
        expired = await self.read(after=active["revision"], timeout=1)
        self.assertEqual(expired["status"], "expired")
        await self.ws.send_bytes(b"ADF1" + struct.pack("!d", applied_at + .1)
                                 + b"\xff\xd8test\xff\xd9")
        self.assertEqual((await request).status, 200)

    async def test_disconnect_wakes_reader_cancels_timer_and_new_session_is_idle(self):
        await self.apply({"throttle": .6, "duration_ms": 5000})
        active = await self.read()
        old = self.client.server.app[SESSIONS]["game-pc"]
        timer = old.control.timer
        poll = asyncio.create_task(self.read(after=active["revision"], timeout=1))
        self.ws = await self.connect()
        disconnected = await asyncio.wait_for(poll, 1)
        self.assertFalse(disconnected["connected"])
        self.assertEqual(disconnected["status"], "disconnected")
        self.assertEqual(disconnected["state"], NEUTRAL)
        self.assertTrue(timer.cancelled())
        current = await asyncio.wait_for(self.read(after=active["revision"], timeout=1,
                                                   session_id=active["session_id"]), .2)
        self.assertNotEqual(current["session_id"], active["session_id"])
        self.assertEqual(current["status"], "idle")
        self.assertEqual(current["revision"], 0)
        self.assertTrue(current["connected"])
        self.assertEqual(current["state"], NEUTRAL)

    async def test_validation_terminal_isolation_and_zero_duration_actions(self):
        for params in ({"after": "-1"}, {"after": "1.1"}, {"after": "oops"},
                       {"timeout": "-1"}, {"timeout": "20.1"},
                       {"timeout": "nan"}, {"timeout": "inf"}):
            with self.subTest(params=params):
                response = await self.client.get(PATH, headers=AUTH, params=params)
                self.assertEqual(response.status, 400, await response.text())
        second = await self.connect("second")
        await self.apply({"throttle": .8, "buttons": ["B"], "duration_ms": 5000})
        other = await self.read(path="/api/terminals/second/control")
        self.assertEqual(other["status"], "idle")
        self.assertEqual(other["state"], NEUTRAL)
        await self.apply({"throttle": 1, "buttons": ["A"], "duration_ms": 0})
        released = await self.read()
        self.assertEqual(released["status"], "released")
        self.assertEqual(released["state"], NEUTRAL)
        await second.close()

    async def test_malformed_actual_state_is_rejected_and_disconnects(self):
        request = self.post({"throttle": .5, "duration_ms": 5000})
        command = await self.ws.receive_json(timeout=1)
        await self.ack(command, state={"throttle": 2, "duration_ms": 5000})
        # 接收 CLOSE 完成握手，让服务立即清理该协议错误连接和等待中的操作。
        await self.ws.receive(timeout=1)
        self.assertEqual((await request).status, 409)
        state = await self.read()
        self.assertFalse(state["connected"])
        self.assertEqual(state["status"], "disconnected")
        self.assertEqual(state["state"], NEUTRAL)


if __name__ == "__main__":
    unittest.main()
