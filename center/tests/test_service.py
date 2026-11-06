"""Exercise the HTTP/WebSocket boundary without a game or virtual controller."""

import asyncio
import base64
import struct
import time
import unittest

from aiohttp import MultipartReader, WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from center.app import SESSIONS, create_app
from center.config import CenterConfig


TOKEN = "test-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
# The relay deliberately treats the JPEG as opaque compressed image bytes.
JPEG = b"\xff\xd8\xff\xe0test-image\xff\xd9"


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = TestClient(TestServer(create_app(CenterConfig(
            token=TOKEN, enable_discovery=False, command_timeout=0.2,
        ))))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def connect(self, terminal_id="game-pc"):
        ws = await self.client.ws_connect("/ws/terminal", headers=AUTH)
        await ws.send_json({
            "type": "hello", "protocol": 1, "terminal_id": terminal_id,
            "name": "Game PC", "capabilities": ["capture", "gamepad"],
            "capture": {"width": 640, "height": 360, "fps": 15},
        })
        welcome = await ws.receive_json(timeout=1)
        self.assertEqual(welcome["type"], "welcome")
        self.assertEqual(welcome["protocol"], 1)
        return ws

    async def send_frame(self, ws, captured_at=None):
        captured_at = time.time() if captured_at is None else captured_at
        await ws.send_bytes(b"ADF1" + struct.pack("!d", captured_at) + JPEG)
        return captured_at

    async def wait_metadata(self, predicate, terminal_id="game-pc"):
        deadline = asyncio.get_running_loop().time() + 1
        while True:
            response = await self.client.get(
                f"/api/terminals/{terminal_id}", headers=AUTH,
            )
            data = await response.json()
            if predicate(data):
                return data
            if asyncio.get_running_loop().time() >= deadline:
                self.fail(f"Terminal state did not update: {data}")
            await asyncio.sleep(0.01)

    async def ack(self, ws, command, *, ok=True, applied_at=None):
        value = {
            "type": "ack", "command_id": command["command_id"], "ok": ok,
            "applied_at": time.time() if applied_at is None else applied_at,
            "state": command["action"],
        }
        if not ok:
            value["error"] = "Virtual controller unavailable"
        await ws.send_json(value)

    async def test_health_public_but_api_and_socket_reject_anonymous_requests(self):
        response = await self.client.get("/health")
        self.assertEqual(response.status, 200)
        for headers in ({}, {"Authorization": "Bearer wrong"}):
            response = await self.client.get("/api/terminals", headers=headers)
            self.assertEqual(response.status, 401)
            self.assertIn("error", await response.json())
            response = await self.client.get("/ws/terminal", headers=headers)
            self.assertEqual(response.status, 401)

    def assert_dashboard_cookie(self, response):
        cookie = response.cookies["auto_driver_dashboard"]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"].lower(), "strict")
        self.assertEqual(cookie["path"], "/")
        self.assertNotEqual(cookie.value, TOKEN)

    async def test_index_auto_authenticates_dashboard_without_exposing_pairing_token(self):
        response = await self.client.get("/")
        self.assertEqual(response.status, 200)
        self.assert_dashboard_cookie(response)
        self.assertNotIn(TOKEN, await response.text())
        response = await self.client.get("/api/terminals")
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["terminals"], [])

    async def test_dashboard_bootstrap_cookie_cannot_pair_terminal(self):
        response = await self.client.get("/dashboard-session")
        self.assertEqual(response.status, 200)
        self.assert_dashboard_cookie(response)
        body = await response.text()
        self.assertNotIn(TOKEN, body)
        self.assertIsInstance(await response.json(), dict)
        response = await self.client.get("/api/terminals")
        self.assertEqual(response.status, 200)
        response = await self.client.get("/ws/terminal")
        self.assertEqual(response.status, 401)

    async def test_dashboard_cookie_authorizes_same_origin_actions(self):
        ws = await self.connect()
        response = await self.client.get("/dashboard-session")
        self.assertEqual(response.status, 200)
        request = asyncio.create_task(self.client.post(
            "/api/terminals/game-pc/actions",
            headers={"Origin": str(self.client.make_url("/").origin())},
            json={"steering": 0.25, "duration_ms": 100},
        ))
        command = await ws.receive_json(timeout=1)
        self.assertEqual(command["action"]["steering"], 0.25)
        await self.ack(ws, command)
        self.assertEqual((await request).status, 200)

    async def test_foreign_origin_cookie_writes_rejected_but_bearer_still_works(self):
        ws = await self.connect()
        response = await self.client.get("/dashboard-session")
        self.assertEqual(response.status, 200)
        foreign = {"Origin": "http://untrusted.example"}
        for endpoint, action in (("actions", {"throttle": 1}), ("release", {})):
            response = await self.client.post(
                f"/api/terminals/game-pc/{endpoint}", headers=foreign, json=action,
            )
            self.assertEqual(response.status, 403, await response.text())
            self.assertIn("error", await response.json())
        request = asyncio.create_task(self.client.post(
            "/api/terminals/game-pc/actions", headers={**AUTH, **foreign},
            json={"brake": 0.5},
        ))
        command = await ws.receive_json(timeout=1)
        self.assertEqual(command["action"]["brake"], 0.5)
        self.assertEqual(command["action"]["throttle"], 0)
        await self.ack(ws, command)
        self.assertEqual((await request).status, 200)

    async def test_registration_and_frame_relay(self):
        ws = await self.connect()
        response = await self.client.get("/api/terminals", headers=AUTH)
        terminal = (await response.json())["terminals"][0]
        self.assertEqual(terminal["terminal_id"], "game-pc")
        self.assertTrue(terminal["connected"])
        response = await self.client.get("/api/terminals/game-pc/frame", headers=AUTH)
        self.assertEqual(response.status, 503)
        timestamp = await self.send_frame(ws)
        await self.wait_metadata(lambda data: data["frame_sequence"] == 1)
        response = await self.client.get("/api/terminals/game-pc/frame", headers=AUTH)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, "image/jpeg")
        self.assertEqual(await response.read(), JPEG)
        self.assertEqual(int(response.headers["X-Frame-Sequence"]), 1)
        self.assertAlmostEqual(float(response.headers["X-Frame-Timestamp"]), timestamp, places=3)

    async def test_action_waits_for_matching_ack_and_returns_result(self):
        ws = await self.connect()
        request = asyncio.create_task(self.client.post(
            "/api/terminals/game-pc/actions", headers=AUTH,
            json={"steering": -0.5, "throttle": 0.4, "buttons": ["A"]},
        ))
        command = await ws.receive_json(timeout=1)
        self.assertEqual(command["type"], "action")
        self.assertEqual(command["action"]["steering"], -0.5)
        self.assertEqual(command["action"]["throttle"], 0.4)
        self.assertEqual(command["action"]["brake"], 0)
        self.assertEqual(command["action"]["duration_ms"], 200)
        self.assertGreater(command["expires_at"], time.time())
        await ws.send_json({"type": "ack", "command_id": "wrong-id", "ok": True})
        await asyncio.sleep(0.02)
        self.assertFalse(request.done(), "An unrelated ACK completed the command")
        await self.ack(ws, command)
        response = await request
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual(body["command_id"], command["command_id"])
        self.assertTrue(body["ack"]["ok"])

    async def test_invalid_actions_never_reach_terminal(self):
        ws = await self.connect()
        invalid_actions = [
            {"throttle": 1.01}, {"steering": -1.01}, {"brake": -0.1},
            {"throttle": True}, {"brake": "1"}, {"steering": float("nan")},
            {"throttle": float("inf")}, {"buttons": ["UNKNOWN"]},
            {"buttons": "A"}, {"duration_ms": -1}, {"duration_ms": 5001},
            {"duration_ms": True}, {"duration_ms": 0.5}, {"typo": 1}, [],
        ]
        for value in invalid_actions:
            with self.subTest(action=value):
                response = await self.client.post(
                    "/api/terminals/game-pc/actions", headers=AUTH, json=value,
                )
                self.assertEqual(response.status, 400, await response.text())
                self.assertIn("error", await response.json())
        with self.assertRaises(asyncio.TimeoutError):
            await ws.receive_json(timeout=0.05)

    async def test_malformed_json_is_client_error(self):
        await self.connect()
        response = await self.client.post(
            "/api/terminals/game-pc/actions", headers=AUTH,
            data="{bad-json",
        )
        self.assertEqual(response.status, 400)
        self.assertIn("error", await response.json())

    async def test_huge_integer_action_is_client_error(self):
        ws = await self.connect()
        for action in ({"throttle": 10 ** 1000}, {"steering": -(10 ** 1000)}):
            response = await self.client.post(
                "/api/terminals/game-pc/actions", headers=AUTH, json=action,
            )
            self.assertEqual(response.status, 400, await response.text())
            self.assertIn("error", await response.json())
        with self.assertRaises(asyncio.TimeoutError):
            await ws.receive_json(timeout=0.05)

    async def test_unacknowledged_action_times_out(self):
        ws = await self.connect()
        request = asyncio.create_task(self.client.post(
            "/api/terminals/game-pc/actions", headers=AUTH, json={"throttle": 1},
        ))
        await ws.receive_json(timeout=1)
        response = await request
        self.assertEqual(response.status, 504)
        self.assertIn("error", await response.json())

    async def test_terminal_execution_error_reaches_caller(self):
        ws = await self.connect()
        request = asyncio.create_task(self.client.post(
            "/api/terminals/game-pc/actions", headers=AUTH, json={"throttle": 1},
        ))
        command = await ws.receive_json(timeout=1)
        await self.ack(ws, command, ok=False)
        response = await request
        self.assertEqual(response.status, 502)
        self.assertIn("Virtual controller", (await response.json())["error"])

    async def test_release_sends_complete_neutral_state(self):
        ws = await self.connect()
        request = asyncio.create_task(self.client.post(
            "/api/terminals/game-pc/release", headers=AUTH, json={},
        ))
        command = await ws.receive_json(timeout=1)
        self.assertEqual(command["action"], {
            "steering": 0, "throttle": 0, "brake": 0,
            "buttons": [], "duration_ms": 0,
        })
        await self.ack(ws, command)
        self.assertEqual((await request).status, 200)

    async def test_release_cancels_active_step_and_discards_queued_action(self):
        ws = await self.connect()
        step_request = asyncio.create_task(self.client.post(
            "/api/terminals/game-pc/step", headers=AUTH,
            json={"action": {"throttle": 0.5}, "observation_timeout": 5},
        ))
        command = await ws.receive_json(timeout=1)
        await self.ack(ws, command)
        queued_request = asyncio.create_task(self.client.post(
            "/api/terminals/game-pc/actions", headers=AUTH, json={"throttle": 1},
        ))
        # Wait until the second request reaches the server and queues behind
        # the step, rather than depending on HTTP scheduling or a fixed delay.
        lock = self.client.server.app[SESSIONS]["game-pc"].operation_lock
        deadline = asyncio.get_running_loop().time() + 1
        while not lock._waiters:
            if asyncio.get_running_loop().time() >= deadline:
                self.fail("Action did not queue behind the active step")
            await asyncio.sleep(0.01)
        release_request = asyncio.create_task(self.client.post(
            "/api/terminals/game-pc/release", headers=AUTH, json={},
        ))
        release_command = await ws.receive_json(timeout=1)
        self.assertEqual(release_command["action"], {
            "steering": 0, "throttle": 0, "brake": 0,
            "buttons": [], "duration_ms": 0,
        })
        await self.ack(ws, release_command)
        self.assertEqual((await release_request).status, 200)
        for request in (step_request, queued_request):
            response = await request
            self.assertEqual(response.status, 409, await response.text())
            self.assertIn("release", (await response.json())["error"])
        with self.assertRaises(asyncio.TimeoutError):
            await ws.receive_json(timeout=0.05)

    async def test_stale_frame_unavailable_until_new_frame_arrives(self):
        await self.client.close()
        self.client = TestClient(TestServer(create_app(CenterConfig(
            token=TOKEN, enable_discovery=False, command_timeout=0.2,
            max_frame_age=0.1,
        ))))
        await self.client.start_server()
        ws = await self.connect()
        await self.send_frame(ws)
        await self.wait_metadata(lambda data: data["frame_sequence"] == 1)
        response = await self.client.get("/api/terminals/game-pc/frame", headers=AUTH)
        self.assertEqual(response.status, 200)
        await asyncio.sleep(0.13)
        response = await self.client.get("/api/terminals/game-pc/frame", headers=AUTH)
        self.assertEqual(response.status, 503)
        self.assertIn("error", await response.json())
        await self.send_frame(ws)
        await self.wait_metadata(lambda data: data["frame_sequence"] == 2)
        response = await self.client.get("/api/terminals/game-pc/frame", headers=AUTH)
        self.assertEqual(response.status, 200)
        self.assertEqual(int(response.headers["X-Frame-Sequence"]), 2)

    async def test_step_waits_for_capture_after_action_duration(self):
        ws = await self.connect()
        await self.send_frame(ws, captured_at=time.time() - 10)
        await self.wait_metadata(lambda data: data["frame_sequence"] == 1)
        request = asyncio.create_task(self.client.post(
            "/api/terminals/game-pc/step", headers=AUTH,
            json={"action": {"throttle": 0.5, "duration_ms": 100}, "observation_timeout": 1},
        ))
        command = await ws.receive_json(timeout=1)
        applied_at = time.time()
        await self.ack(ws, command, applied_at=applied_at)
        await self.send_frame(ws, captured_at=applied_at + 0.02)
        await self.wait_metadata(lambda data: data["frame_sequence"] == 2)
        self.assertFalse(request.done(), "Step returned an image captured before the action duration elapsed")
        await self.send_frame(ws, captured_at=applied_at + 0.12)
        response = await request
        self.assertEqual(response.status, 200, await response.text())
        observation = (await response.json())["observation"]
        self.assertEqual(observation["sequence"], 3)
        self.assertGreaterEqual(observation["captured_at"], applied_at + 0.1)
        self.assertEqual(base64.b64decode(observation["jpeg_base64"]), JPEG)

    async def test_step_observation_timeout_is_explicit(self):
        ws = await self.connect()
        request = asyncio.create_task(self.client.post(
            "/api/terminals/game-pc/step", headers=AUTH,
            json={"action": {"duration_ms": 0}, "observation_timeout": 0.1},
        ))
        command = await ws.receive_json(timeout=1)
        await self.ack(ws, command)
        response = await request
        self.assertEqual(response.status, 504)
        self.assertIn("error", await response.json())

    async def test_disconnect_clears_frame_and_fails_pending_action(self):
        ws = await self.connect()
        await self.send_frame(ws)
        await self.wait_metadata(lambda data: data["frame_sequence"] == 1)
        request = asyncio.create_task(self.client.post(
            "/api/terminals/game-pc/actions", headers=AUTH, json={"throttle": 0.5},
        ))
        await ws.receive_json(timeout=1)
        await ws.close()
        response = await request
        self.assertEqual(response.status, 409)
        await self.wait_metadata(lambda data: not data["connected"])
        response = await self.client.get("/api/terminals/game-pc/frame", headers=AUTH)
        self.assertEqual(response.status, 409)

    async def test_duplicate_identity_replaces_old_connection(self):
        old_ws = await self.connect()
        new_ws = await self.connect()
        message = await old_ws.receive(timeout=1)
        self.assertIn(message.type, {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING})
        await self.send_frame(new_ws)
        metadata = await self.wait_metadata(lambda data: data["frame_sequence"] >= 1)
        self.assertTrue(metadata["connected"])

    async def test_unknown_terminal_is_not_found(self):
        for method, path, body in (
            ("get", "/api/terminals/missing", None),
            ("get", "/api/terminals/missing/frame", None),
            ("post", "/api/terminals/missing/actions", {"throttle": 1}),
        ):
            response = await getattr(self.client, method)(path, headers=AUTH, json=body)
            self.assertEqual(response.status, 404)
            self.assertIn("error", await response.json())

    async def test_diagnostic_stream_uses_cookie_and_ends_when_terminal_disconnects(self):
        ws = await self.connect()
        await self.send_frame(ws)
        await self.wait_metadata(lambda data: data["frame_sequence"] == 1)
        await self.client.get("/dashboard-session")
        response = await self.client.get("/api/terminals/game-pc/stream")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["X-Accel-Buffering"], "no")
        reader = MultipartReader.from_response(response)
        first = await reader.next()
        self.assertEqual(first.headers["X-Frame-Sequence"], "1")
        self.assertEqual(await first.read(), JPEG)
        await self.send_frame(ws)
        second = await reader.next()
        self.assertEqual(second.headers["X-Frame-Sequence"], "2")
        self.assertEqual(await second.read(), JPEG)
        await ws.close()
        await asyncio.wait_for(response.content.read(), timeout=1)
        response.close()


if __name__ == "__main__":
    unittest.main()
