"""用本地 HTTP 假中心验证只读感知发布、结果身份与演示的控制隔离。"""

import asyncio
import copy
from contextlib import redirect_stdout
import io
import time
import unittest
from urllib.parse import quote

from aiohttp import web
from aiohttp.test_utils import TestServer

from examples.publish_perception import demo_payload, publish_demo
from src.integrations.center import CenterAPIError, CenterClient, CenterProtocolError, CenterTransportError


TOKEN = "perception-test-token"


class PerceptionServer:
    def __init__(self, terminal_id="game-pc"):
        self.terminal_id = terminal_id
        self.session_id = "session-1"
        self.connected = True
        self.requests = []
        self.result = None
        self.override = None
        self.error_status = None
        self.disconnect = False
        self.next_terminal_session = None
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", self.handle)
        self.server = TestServer(app)

    async def start(self):
        await self.server.start_server()
        return self

    @property
    def url(self):
        return str(self.server.make_url("/")).rstrip("/")

    def terminal(self):
        return {"terminal_id": self.terminal_id, "session_id": self.session_id,
                "connected": self.connected, "width": 1280, "height": 720}

    def envelope(self):
        common = {"terminal_id": self.terminal_id, "session_id": self.session_id, "ttl_ms": 2000}
        if self.result is None:
            return {**common, "status": "waiting", "received_at": None, "frame": None,
                    "age_ms": None, "road": None, "prediction": None}
        return {**copy.deepcopy(self.result), **common, "received_at": time.time(), "age_ms": 0,
                "frame": {"sequence": self.result.get("frame_sequence"),
                          "captured_at": self.result.get("captured_at"), "received_at": None}}

    async def handle(self, request):
        body = await request.json() if request.can_read_body else None
        self.requests.append({"method": request.method, "raw_path": request.raw_path,
                              "authorization": request.headers.get("Authorization"), "body": body})
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return web.json_response({"error": "bad credential " + request.headers.get("Authorization", "")}, status=401)
        if request.path.endswith("/perception"):
            if self.disconnect:
                request.transport.abort()
                return web.Response()
            if self.error_status is not None:
                return web.json_response({"error": "simulated failure"}, status=self.error_status)
            if self.override is not None:
                return self.override if isinstance(self.override, web.StreamResponse) else web.json_response(self.override)
            if request.method == "POST":
                if body.get("session_id") != self.session_id:
                    return web.json_response({"error": "session changed"}, status=409)
                self.result = copy.deepcopy(body)
            return web.json_response(self.envelope())
        if request.method != "GET":
            return web.json_response({"error": "unexpected control request"}, status=500)
        if request.path.endswith("/terminals"):
            return web.json_response({"terminals": [self.terminal()]})
        if self.result is not None and self.next_terminal_session is not None:
            self.session_id = self.next_terminal_session
        return web.json_response(self.terminal())


class PerceptionClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.upstream = await PerceptionServer().start()

    async def asyncTearDown(self):
        await self.upstream.server.close()

    async def test_publish_and_read_preserve_payload_auth_and_frame_metadata(self):
        payload = demo_payload(self.upstream.terminal(), -.23)
        payload.update(frame_sequence=42, captured_at=time.time(), inference_ms=12.5)
        original = copy.deepcopy(payload)
        async with CenterClient(self.upstream.url, TOKEN) as client:
            published = await client.publish_perception("game-pc", payload)
            latest = await client.get_perception("game-pc")
        self.assertEqual(payload, original)
        self.assertEqual(published["road"], payload["road"])
        self.assertEqual(latest["prediction"], payload["prediction"])
        self.assertEqual(latest["frame"]["sequence"], 42)
        self.assertEqual(latest["frame"]["captured_at"], payload["captured_at"])
        self.assertEqual(latest["model"]["label"], "演示数据")
        self.assertEqual(latest["ttl_ms"], 2000)
        self.assertEqual([item["method"] for item in self.upstream.requests], ["POST", "GET"])
        self.assertTrue(all(item["authorization"] == f"Bearer {TOKEN}" for item in self.upstream.requests))
        self.assertTrue(all(item["raw_path"].endswith("/perception") for item in self.upstream.requests))

    async def test_terminal_id_is_url_escaped_for_both_endpoints(self):
        terminal_id = "车/库 #?%"
        self.upstream.terminal_id = terminal_id
        async with CenterClient(self.upstream.url, TOKEN) as client:
            await client.publish_perception(terminal_id, demo_payload(self.upstream.terminal(), .1))
            await client.get_perception(terminal_id)
        expected = "/api/terminals/" + quote(terminal_id, safe="") + "/perception"
        self.assertEqual([item["raw_path"] for item in self.upstream.requests], [expected, expected])

    async def test_waiting_stale_no_road_and_direction_only_are_valid_results(self):
        async with CenterClient(self.upstream.url, TOKEN) as client:
            waiting = await client.get_perception("game-pc")
            self.assertEqual(waiting["status"], "waiting")
            payload = demo_payload(self.upstream.terminal(), .2)
            payload["road"] = None
            result = await client.publish_perception("game-pc", payload)
            self.assertEqual(result["prediction"]["steering"], .2)
            payload.update(status="no_road", prediction=None)
            result = await client.publish_perception("game-pc", payload)
            self.assertEqual(result["status"], "no_road")
            self.upstream.override = {**self.upstream.envelope(), "status": "stale", "road": None,
                                      "prediction": None, "age_ms": 2100}
            self.assertEqual((await client.get_perception("game-pc"))["status"], "stale")

    async def test_invalid_terminal_and_missing_session_never_make_requests(self):
        async with CenterClient(self.upstream.url, TOKEN) as client:
            for terminal_id in ("", "x" * 65, None):
                with self.subTest(terminal_id=terminal_id), self.assertRaises(ValueError):
                    await client.get_perception(terminal_id)
            for payload in (None, [], {}, {"session_id": ""}, {"session_id": True}):
                with self.subTest(payload=payload), self.assertRaises(ValueError):
                    await client.publish_perception("game-pc", payload)
        self.assertEqual(self.upstream.requests, [])

    async def test_http_errors_propagate_without_replaying_the_publish(self):
        async with CenterClient(self.upstream.url, TOKEN) as client:
            for status in (400, 404, 409, 503):
                self.upstream.error_status = status
                before = len(self.upstream.requests)
                with self.subTest(status=status), self.assertRaises(CenterAPIError) as caught:
                    await client.publish_perception("game-pc", demo_payload(self.upstream.terminal(), .1))
                self.assertEqual(caught.exception.status, status)
                self.assertEqual(len(self.upstream.requests), before + 1)

    async def test_connection_loss_is_reported_once_and_does_not_release_or_control(self):
        self.upstream.disconnect = True
        async with CenterClient(self.upstream.url, TOKEN) as client:
            with self.assertRaises(CenterTransportError) as caught:
                await client.publish_perception("game-pc", demo_payload(self.upstream.terminal(), .1))
        self.assertTrue(caught.exception.may_have_executed)
        self.assertNotIn("action", str(caught.exception))
        self.assertEqual(len(self.upstream.requests), 1)
        self.assertTrue(self.upstream.requests[0]["raw_path"].endswith("/perception"))

    async def test_auth_error_redacts_token(self):
        async with CenterClient(self.upstream.url, "incorrect") as client:
            with self.assertRaises(CenterAPIError) as caught:
                await client.get_perception("game-pc")
        self.assertEqual(caught.exception.status, 401)
        self.assertNotIn("incorrect", str(caught.exception))

    async def test_wrong_terminal_session_or_malformed_metadata_is_not_accepted(self):
        self.upstream.result = demo_payload(self.upstream.terminal(), .1)
        base = self.upstream.envelope()
        cases = [{**base, "terminal_id": "other"}, {**base, "session_id": ""},
                 {**base, "status": []}, {**base, "ttl_ms": float("nan")},
                 {**base, "age_ms": -1}, {**base, "received_at": "bad"},
                 {**base, "frame": {"sequence": True}}, {**base, "image": {"width": True, "height": 720}},
                 {**base, "prediction": {"steering": 2, "confidence": .5, "source": "model"}},
                 {**base, "status": "stale"}, {**base, "road": None, "prediction": None}]
        async with CenterClient(self.upstream.url, TOKEN) as client:
            for override in cases:
                self.upstream.override = override
                with self.subTest(override=override), self.assertRaises(CenterProtocolError):
                    await client.get_perception("game-pc")
            self.upstream.override = {**base, "session_id": "other-session"}
            with self.assertRaises(CenterProtocolError):
                await client.publish_perception("game-pc", self.upstream.result)
            self.upstream.override = web.Response(text="not json", content_type="application/json")
            with self.assertRaises(CenterProtocolError):
                await client.get_perception("game-pc")


class PerceptionDemoTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.upstream = await PerceptionServer().start()

    async def asyncTearDown(self):
        await self.upstream.server.close()

    async def test_demo_publishes_finite_explicit_simulation_and_never_controls(self):
        async with CenterClient(self.upstream.url, TOKEN) as client, asyncio.timeout(3):
            with redirect_stdout(io.StringIO()):
                count = await publish_demo(client, self.upstream.terminal(), frames=3, fps=10)
        self.assertEqual(count, 3)
        posts = [item for item in self.upstream.requests if item["method"] == "POST"]
        self.assertEqual(len(posts), 3)
        self.assertTrue(all(item["raw_path"].endswith("/perception") for item in posts))
        self.assertTrue(all(item["body"]["prediction"]["source"] == "demo" for item in posts))
        self.assertTrue(all(item["body"]["model"]["label"] == "演示数据" for item in posts))
        strengths = [item["body"]["prediction"]["steering"] for item in posts]
        self.assertTrue(all(-1 <= value <= 1 for value in strengths))
        self.assertTrue(all(abs(b - a) <= .24 for a, b in zip(strengths, strengths[1:])))
        self.assertEqual(posts[0]["body"]["road"], posts[-1]["body"]["road"])

    async def test_session_change_stops_demo_without_publishing_into_new_session(self):
        self.upstream.next_terminal_session = "session-2"
        async with CenterClient(self.upstream.url, TOKEN) as client:
            with redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, "连接已改变"):
                await publish_demo(client, self.upstream.terminal(), frames=3, fps=10)
        posts = [item for item in self.upstream.requests if item["method"] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertTrue(all(item["raw_path"].endswith("/perception") for item in posts))

    async def test_invalid_demo_limits_make_no_requests(self):
        async with CenterClient(self.upstream.url, TOKEN) as client:
            for arguments in ({"frames": 0}, {"frames": True}, {"fps": float("nan")}, {"fps": 20}):
                with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                    await publish_demo(client, self.upstream.terminal(), **arguments)
        self.assertEqual(self.upstream.requests, [])


if __name__ == "__main__":
    unittest.main()
