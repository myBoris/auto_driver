"""真实认证 WS/HTTP 遥测读取，过期和会话替换不泄漏旧样本。"""

import asyncio
import importlib.util
import socket
import struct
import time
import unittest

from aiohttp.test_utils import TestClient, TestServer

from center.app import create_app
from center.config import CenterConfig
from center.dependencies import SESSIONS


TOKEN = "telemetry-read-test"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
PATH = "/api/terminals/game-pc/telemetry"


def sample(clock=100):
    return {"type": "telemetry", "captured_at": time.time(), "data": {
        "source": "fh5_data_out", "is_race_on": True, "timestamp_ms": clock,
        "speed_mps": 12, "position": [100, 200, 300], "velocity": [1, 2, 12], "yaw": .5,
    }}


class TelemetryAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = TestClient(TestServer(create_app(CenterConfig(
            token=TOKEN, enable_discovery=False, media_enabled=False,
        ))))
        await self.client.start_server()
        self.ws = await self.connect()

    async def asyncTearDown(self):
        await self.client.close()

    async def connect(self):
        ws = await self.client.ws_connect("/ws/terminal", headers=AUTH)
        await ws.send_json({"type": "hello", "protocol": 1, "terminal_id": "game-pc"})
        await ws.receive_json(timeout=1)
        return ws

    async def read(self, **params):
        response = await self.client.get(PATH, headers=AUTH, params=params)
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        return await response.json()

    async def publish(self, value=None):
        task = asyncio.create_task(self.read(after=self.client.server.app[SESSIONS]["game-pc"].telemetry.sequence, timeout=1))
        await self.ws.send_json(value or sample())
        return await asyncio.wait_for(task, 2)

    async def test_waiting_auth_validation_and_reads_do_not_send_actions(self):
        initial = await self.read()
        self.assertEqual(initial["status"], "waiting")
        self.assertEqual(initial["sequence"], 0)
        self.assertIsNone(initial["data"])
        self.assertEqual((await self.client.get(PATH)).status, 401)
        self.assertEqual((await self.client.get("/api/terminals/missing/telemetry", headers=AUTH)).status, 404)
        for params in ({"after": "-1"}, {"after": "bad"}, {"timeout": "nan"}, {"timeout": "21"}):
            self.assertEqual((await self.client.get(PATH, headers=AUTH, params=params)).status, 400)
        terminal = self.client.server.app[SESSIONS]["game-pc"]
        self.assertEqual(terminal.pending, {})
        self.assertEqual(terminal.control.revision, 0)

    async def test_longpoll_multiple_readers_strict_after_and_freshness(self):
        polls = [asyncio.create_task(self.read(after=0, timeout=1)) for _ in range(2)]
        sent = sample()
        await self.ws.send_json(sent)
        values = await asyncio.wait_for(asyncio.gather(*polls), 2)
        for value in values:
            self.assertEqual(value["status"], "ready")
            self.assertEqual(value["sequence"], 1)
            self.assertEqual(value["data"], sent["data"])
            self.assertEqual(value["captured_at"], sent["captured_at"])
            self.assertGreaterEqual(value["age_ms"], 0)
            self.assertLess(value["age_ms"], 1000)
        for after in (1, 9999):
            waiting = await self.read(after=after, timeout=.02)
            self.assertEqual(waiting["status"], "waiting")
            self.assertIsNone(waiting["data"])
        state = self.client.server.app[SESSIONS]["game-pc"].telemetry
        state.received_mono -= 2
        stale = await self.read()
        self.assertEqual(stale["status"], "stale")
        self.assertIsNone(stale["data"])
        fresh = await self.publish(sample(101))
        self.assertEqual(fresh["sequence"], 2)
        self.assertEqual(fresh["data"]["timestamp_ms"], 101)

    async def test_disconnect_and_replacement_wake_poll_without_reusing_data(self):
        original = await self.publish()
        polling = asyncio.create_task(self.read(after=1, timeout=2))
        await asyncio.sleep(.02)
        replacement = await self.connect()
        disconnected = await asyncio.wait_for(polling, 1)
        self.assertEqual(disconnected["status"], "disconnected")
        self.assertFalse(disconnected["connected"])
        self.assertIsNone(disconnected["data"])
        current = await self.read(session_id=original["session_id"], after=1, timeout=2)
        self.assertNotEqual(current["session_id"], original["session_id"])
        self.assertEqual(current["sequence"], 0)
        self.assertEqual(current["status"], "waiting")
        await replacement.close()

    async def test_malformed_payload_closes_publisher_without_exposing_data(self):
        bad = sample()
        bad["data"]["velocity"] = [0, float("nan"), 1]
        await self.ws.send_json(bad)
        await self.ws.receive(timeout=1)
        result = await self.read()
        self.assertEqual(result["status"], "disconnected")
        self.assertIsNone(result["data"])


@unittest.skipUnless(importlib.util.find_spec("terminal"), "cross-service test requires workspace terminal package")
class TelemetryEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_udp_terminal_ws_and_center_http_without_game_input(self):
        from terminal.capture import LatestCapture
        from terminal.config.connection import TerminalConfig
        from terminal.control import Controller, MockGamepad
        from terminal.network.client import TerminalClient

        app = create_app(CenterConfig(token=TOKEN, enable_discovery=False, media_enabled=False))
        browser = TestClient(TestServer(app))
        await browser.start_server()
        self.addAsyncCleanup(browser.close)
        controller = Controller(MockGamepad())
        self.addCleanup(controller.close)

        class IdleCapture(LatestCapture):
            def start(self):
                pass

        capture = IdleCapture(fps=10, width=320, quality=75, video_enabled=False)
        terminal = TerminalClient(TerminalConfig(
            token=TOKEN, terminal_id="game-pc", name="UDP test", center=str(browser.make_url("/")).rstrip("/"),
            telemetry_port=0, mock=True,
        ), capture, controller)
        running = asyncio.create_task(terminal.run())
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(sender.close)
        try:
            async with asyncio.timeout(2):
                while "game-pc" not in app[SESSIONS]:
                    await asyncio.sleep(.005)
            # welcome 后 _telemetry 已安装，只传入测试生成的二进制数据。
            await asyncio.sleep(.02)
            value = bytearray(324)
            struct.pack_into("<iI", value, 0, 1, 555)
            struct.pack_into("<fff", value, 32, 1, 2, 10)
            struct.pack_into("<ffff", value, 244, 100, 200, 300, 10)
            sender.sendto(value, ("127.0.0.1", terminal.telemetry.port))
            response = await browser.get(PATH, headers=AUTH, params={"after": "0", "timeout": "1"})
            self.assertEqual(response.status, 200)
            result = await response.json()
            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["data"]["timestamp_ms"], 555)
            self.assertEqual(result["data"]["position"], [100, 200, 300])
            self.assertEqual(result["data"]["velocity"], [1, 2, 10])
            self.assertEqual(app[SESSIONS]["game-pc"].control.revision, 0)
            self.assertEqual(controller.status()["state"]["throttle"], 0)
        finally:
            terminal.stop_event.set()
            await asyncio.wait_for(running, 2)
        self.assertIsNone(terminal.telemetry.transport)


if __name__ == "__main__":
    unittest.main()
