"""媒体 API 集成验证：假上游代替编码器，不访问游戏或真实媒体服务。"""

import asyncio
import base64
import json
import unittest
from urllib.parse import unquote, urlsplit

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from center.app import MEDIA, create_app
from center.config import CenterConfig
from center.domain.media import publisher_url, rewrite_ice_candidates, terminal_from_media_path


TOKEN = "media-test-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
OFFER = "v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\na=rtpmap:96 H264/90000\r\n"
ANSWER = (OFFER + "a=ice-ufrag:keep-ufrag\r\na=ice-pwd:keep-password\r\n"
          "a=candidate:1 1 udp 2130706431 172.18.0.2 8189 typ host\r\n"
          "a=candidate:2 1 tcp 2130706430 172.18.0.2 8189 typ host tcptype passive\r\n"
          "a=end-of-candidates\r\n")


class ProductionDisconnectServer(TestServer):
    """沿用 web.run_app 的默认行为，断线不自动取消尚在处理的协商请求。

    aiohttp TestServer 默认开启 handler_cancellation，与实际服务默认值不同；
    此测试必须覆盖请求断开后上游仍完成并返回 Location 的清理分支。
    """

    async def _make_runner(self, **kwargs):
        kwargs["handler_cancellation"] = False
        return await super()._make_runner(**kwargs)


class MediaAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        self.upstream_status = 201
        self.upstream_answer = ANSWER
        self.upstream_location = None
        self.delete_status = 204
        self.upstream_chunked = False
        self.upstream_gate = None
        upstream = web.Application()
        upstream.router.add_post("/{path:.+}/whep", self.upstream_post)
        upstream.router.add_delete("/{path:.+}", self.upstream_delete)
        self.upstream = TestServer(upstream)
        await self.upstream.start_server()
        config = CenterConfig(token=TOKEN, enable_discovery=False,
                              media_http_url=str(self.upstream.make_url("/")).rstrip("/"))
        app = create_app(config)
        self.active_whep_request = None

        @web.middleware
        async def observe_whep(request, handler):
            if request.path.endswith("/video/whep"):
                self.active_whep_request = request
            return await handler(request)

        app.middlewares.insert(0, observe_whep)
        self.client = TestClient(ProductionDisconnectServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.upstream.close()

    async def upstream_post(self, request):
        self.calls.append(("POST", request.path, request.headers.get("Authorization"), await request.text()))
        if self.upstream_gate is not None:
            await self.upstream_gate.wait()
        location = self.upstream_location or request.path + "/upstream-session"
        if self.upstream_chunked:
            response = web.StreamResponse(status=201, headers={
                "Content-Type": "application/sdp", "Location": location,
            })
            await response.prepare(request)
            await response.write(self.upstream_answer[:25].encode())
            await asyncio.sleep(0.02)
            await response.write(self.upstream_answer[25:].encode())
            return response
        return web.Response(status=self.upstream_status, text=self.upstream_answer,
                            content_type="application/sdp", headers={"Location": location, "ETag": '"etag-1"'})

    async def upstream_delete(self, request):
        self.calls.append(("DELETE", request.path, request.headers.get("Authorization"), ""))
        return web.Response(status=self.delete_status)

    async def connect(self, terminal_id="game-pc", *, host=None, ready=True):
        headers = dict(AUTH)
        if host:
            headers["Host"] = host
        ws = await self.client.ws_connect("/ws/terminal", headers=headers)
        await ws.send_json({"type": "hello", "protocol": 1, "terminal_id": terminal_id,
                            "name": "Media test terminal", "status": {"video": {"ready": ready}}})
        return ws, await ws.receive_json(timeout=1)

    async def negotiate(self, *, headers=None, offer=OFFER):
        return await self.client.post("/api/terminals/game-pc/video/whep", data=offer,
                                      headers=headers or {**AUTH, "Content-Type": "application/sdp"})

    async def test_welcome_uses_actual_connection_host_without_browser_secret_leak(self):
        _, welcome = await self.connect(host="192.168.1.23:8000")
        publish = urlsplit(welcome["video"]["publish_url"])
        self.assertEqual(publish.hostname, "192.168.1.23")
        self.assertEqual(publish.port, 8554)
        self.assertEqual(publish.username, "publisher")
        self.assertEqual(publish.path, "/terminal/game-pc")
        response = await self.client.get("/api/terminals/game-pc/video", headers=AUTH)
        metadata = await response.json()
        self.assertTrue(metadata["enabled"])
        self.assertTrue(metadata["available"])
        self.assertEqual(metadata["codec"], "H264")
        self.assertNotIn(TOKEN, json.dumps(metadata))
        self.assertNotIn("publish_url", metadata)

    async def test_unavailable_metadata_explains_waiting_state(self):
        await self.connect(ready=False)
        response = await self.client.get("/api/terminals/game-pc/video", headers=AUTH)
        metadata = await response.json()
        self.assertFalse(metadata["available"])
        self.assertEqual(metadata["reason"], "publisher_starting")
        self.assertTrue(metadata["message"])

    async def test_media_callback_checks_identity_action_credentials_and_live_path(self):
        ws, _ = await self.connect()
        valid = {"user": "publisher", "password": TOKEN, "action": "publish", "path": "terminal/game-pc"}
        for user, action in (("publisher", "publish"), ("reader", "read")):
            response = await self.client.post("/internal/media/auth", json={**valid, "user": user, "action": action})
            self.assertEqual(response.status, 200)
        for field, value in (("password", "wrong"), ("user", "reader"), ("user", []), ("action", "read"),
                             ("path", "terminal/game-pc/extra"), ("path", "terminal/missing"),
                             ("path", "../terminal/game-pc"), ("path", None)):
            response = await self.client.post("/internal/media/auth", json={**valid, field: value})
            self.assertEqual(response.status, 401)
        await ws.close()
        response = await self.client.post("/internal/media/auth", json=valid)
        self.assertEqual(response.status, 401)

    async def test_cookie_whep_proxy_rewrites_candidates_and_hides_upstream_location(self):
        await self.connect()
        await self.client.get("/dashboard-session")
        response = await self.negotiate(headers={"Content-Type": "application/sdp",
                                                "Origin": str(self.client.make_url("/").origin())})
        self.assertEqual(response.status, 201, await response.text())
        answer = await response.text()
        self.assertIn("127.0.0.1 8189 typ host", answer)
        self.assertNotIn("172.18.0.2", answer)
        self.assertIn("a=ice-pwd:keep-password", answer)
        self.assertNotIn(TOKEN, answer)
        location = response.headers["Location"]
        self.assertTrue(location.startswith("/api/terminals/game-pc/video/sessions/"))
        self.assertNotIn("upstream-session", location)
        self.assertEqual(response.headers["ETag"], '"etag-1"')
        method, path, authorization, body = self.calls[0]
        self.assertEqual((method, path, body), ("POST", "/terminal/game-pc/whep", OFFER))
        self.assertEqual(base64.b64decode(authorization.split()[1]).decode(), "reader:" + TOKEN)
        response = await self.client.delete(location)
        self.assertEqual(response.status, 204)
        self.assertEqual(self.calls[-1][0:2], ("DELETE", "/terminal/game-pc/whep/upstream-session"))
        self.assertFalse(self.client.server.app[MEDIA].sessions)

    async def test_cookie_sdp_post_has_origin_protection(self):
        await self.connect()
        await self.client.get("/dashboard-session")
        response = await self.negotiate(headers={"Content-Type": "application/sdp", "Origin": "http://foreign.example"})
        self.assertEqual(response.status, 403)
        self.assertFalse(self.calls)
        response = await self.negotiate(headers={**AUTH, "Content-Type": "application/sdp", "Origin": "http://foreign.example"})
        self.assertEqual(response.status, 201)

    async def test_chunked_sdp_answer_is_read_to_eof(self):
        await self.connect()
        self.upstream_chunked = True
        response = await self.negotiate()
        self.assertEqual(response.status, 201)
        answer = await response.text()
        self.assertIn("a=ice-ufrag:keep-ufrag", answer)
        self.assertIn("a=ice-pwd:keep-password", answer)
        self.assertTrue(answer.endswith("a=end-of-candidates\r\n"))

    async def test_browser_disconnect_during_post_reclaims_created_session(self):
        await self.connect()
        self.upstream_gate = asyncio.Event()
        request = asyncio.create_task(self.negotiate())
        deadline = asyncio.get_running_loop().time() + 2
        while not self.calls:
            if asyncio.get_running_loop().time() > deadline:
                self.fail("Negotiation did not reach upstream")
            await asyncio.sleep(0.01)
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)
        # 确认 TCP 关闭事件已传到服务端，再让上游应答，避免依赖调度顺序。
        while (self.active_whep_request.transport is not None
               and not self.active_whep_request.transport.is_closing()):
            if asyncio.get_running_loop().time() > deadline:
                self.fail("Cancelled client did not close the HTTP request transport")
            await asyncio.sleep(0.01)
        self.upstream_gate.set()
        while not any(call[0] == "DELETE" for call in self.calls):
            if asyncio.get_running_loop().time() > deadline:
                self.fail("Abandoned negotiation did not delete upstream session")
            await asyncio.sleep(0.01)
        self.assertFalse(self.client.server.app[MEDIA].sessions)

    async def test_invalid_offer_or_content_type_does_not_reach_upstream(self):
        await self.connect()
        response = await self.negotiate(offer="not SDP")
        self.assertEqual(response.status, 400)
        response = await self.negotiate(headers={**AUTH, "Content-Type": "application/json"})
        self.assertEqual(response.status, 415)
        self.assertFalse(self.calls)

    async def test_missing_publisher_and_upstream_errors_are_clear(self):
        await self.connect()
        for upstream, expected in ((404, 503), (503, 503), (500, 502), (401, 502)):
            self.upstream_status = upstream
            response = await self.negotiate()
            self.assertEqual(response.status, expected)
            self.assertNotIn(TOKEN, await response.text())

    async def test_upstream_location_cannot_escape_media_origin_or_terminal_path(self):
        await self.connect()
        for location in ("http://other.invalid/terminal/game-pc/whep/session",
                         "/terminal/other-pc/whep/session", "/terminal/game-pc/whep/../session"):
            self.upstream_location = location
            response = await self.negotiate()
            self.assertEqual(response.status, 502)
            self.assertFalse(self.client.server.app[MEDIA].sessions)

    async def test_delete_session_requires_matching_terminal(self):
        await self.connect()
        response = await self.negotiate()
        location = response.headers["Location"]
        response = await self.client.delete(location.replace("game-pc", "other-pc"), headers=AUTH)
        self.assertEqual(response.status, 404)
        self.assertEqual(len(self.calls), 1)
        self.delete_status = 404
        response = await self.client.delete(location, headers=AUTH)
        self.assertEqual(response.status, 204)

    async def test_terminal_disconnect_reclaims_server_owned_video_session(self):
        ws, _ = await self.connect()
        response = await self.negotiate()
        self.assertEqual(response.status, 201)
        await ws.close()
        await self.client.server.app[MEDIA].cleanup()
        self.assertFalse(self.client.server.app[MEDIA].sessions)
        self.assertEqual(self.calls[-1][0], "DELETE")


class MediaDomainTests(unittest.TestCase):
    def test_publisher_address_quotes_credentials_and_brackets_ipv6(self):
        url = urlsplit(publisher_url("[2001:db8::1]:8000", "game-pc", "private:/@value", 8554))
        self.assertEqual(url.hostname, "2001:db8::1")
        self.assertEqual(url.port, 8554)
        self.assertEqual(unquote(url.password), "private:/@value")

    def test_media_path_is_exactly_one_valid_terminal_id(self):
        self.assertEqual(terminal_from_media_path("terminal/game-pc"), "game-pc")
        for value in ("terminal/a/b", "terminal/../pc", "terminal/", "other/pc", None):
            self.assertIsNone(terminal_from_media_path(value))

    def test_ice_rewrite_preserves_relay_credentials_and_transport(self):
        answer = ANSWER + "a=candidate:3 1 udp 100 203.0.113.2 40000 typ srflx raddr 0.0.0.0 rport 0\r\n"
        rewritten = rewrite_ice_candidates(answer, "192.168.0.42", 8189)
        self.assertNotIn("172.18.0.2", rewritten)
        self.assertIn("udp 2130706431 192.168.0.42 8189 typ host", rewritten)
        self.assertIn("tcp 2130706430 192.168.0.42 8189 typ host tcptype passive", rewritten)
        self.assertIn("203.0.113.2 40000 typ srflx", rewritten)
        self.assertIn("a=ice-pwd:keep-password", rewritten)
        self.assertIn("2001:db8::9 8189 typ host", rewrite_ice_candidates(ANSWER, "2001:db8::9", 8189))

    def test_missing_host_candidates_get_published_udp_and_tcp_addresses(self):
        rewritten = rewrite_ice_candidates(OFFER + "a=end-of-candidates\r\n", "localhost", 8189)
        self.assertIn("127.0.0.1 8189 typ host", rewritten)
        self.assertIn("tcptype passive", rewritten)
        self.assertLess(rewritten.index("a=candidate:"), rewritten.index("a=end-of-candidates"))

    def test_ice_host_must_be_a_reachable_address_or_hostname(self):
        for host in ("0.0.0.0", "::", "bad host", "host/path"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                rewrite_ice_candidates(ANSWER, host, 8189)


if __name__ == "__main__":
    unittest.main()
