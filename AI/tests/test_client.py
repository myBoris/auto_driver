"""使用真实 aiohttp 请求验证中心协议、错误语义和动作不能自动重放。"""

import base64
import time
import unittest

from aiohttp import web

from src.domain import Action
from src.integrations.center import CenterAPIError, CenterClient, CenterProtocolError, CenterTransportError

from fake_center import FakeCenter, JPEG, TOKEN


class CenterClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = await FakeCenter().start()

    async def asyncTearDown(self):
        await self.server.close()

    async def test_bearer_auth_list_and_frame_metadata(self):
        async with CenterClient(self.server.url, TOKEN) as client:
            terminals = await client.list_terminals()
            terminal = await client.get_terminal("game-pc")
            image = await client.observe("game-pc", after=0, timeout=2)
        self.assertEqual(terminals[0]["terminal_id"], "game-pc")
        self.assertTrue(terminal["connected"])
        self.assertEqual(image.jpeg, JPEG)
        self.assertEqual(image.sequence, 1)
        self.assertEqual(image.captured_at, self.server.captured_at)
        self.assertEqual(image.received_at, self.server.received_at)
        self.assertTrue(all(request["authorization"] == f"Bearer {TOKEN}" for request in self.server.requests))
        self.assertEqual(self.server.requests[-1]["query"], {"after": "0", "timeout": "2.0"})
        self.assertNotIn(TOKEN, repr(client))

    async def test_step_preserves_full_action_ack_and_next_observation(self):
        action = Action(steering=-0.25, throttle=0.4, buttons=("A",), duration_ms=120)
        async with CenterClient(self.server.url, TOKEN) as client:
            result = await client.step("game-pc", action, observation_timeout=2)
            await client.release("game-pc")
        request = self.server.requests[0]
        self.assertEqual(request["body"], {"action": action.to_payload(), "observation_timeout": 2.0})
        self.assertEqual(result.command_id, "command-2")
        self.assertEqual(result.ack["command_id"], result.command_id)
        self.assertEqual(result.ack["state"], action.to_payload())
        self.assertEqual(result.observation.jpeg, JPEG)
        self.assertGreaterEqual(result.observation.captured_at,
                                result.ack["applied_at"] + action.duration_ms / 1000)
        self.assertEqual(self.server.requests[-1]["body"], {})

    async def test_explicit_http_action_failure_is_not_retried(self):
        self.server.response_status["actions"] = 504
        async with CenterClient(self.server.url, TOKEN) as client:
            with self.assertRaises(CenterAPIError) as caught:
                await client.action("game-pc", Action(throttle=0.2))
        self.assertEqual(caught.exception.status, 504)
        self.assertEqual(len(self.server.requests), 1)

    async def test_connection_loss_after_action_request_is_uncertain_and_not_retried(self):
        self.server.disconnect_endpoints.add("actions")
        async with CenterClient(self.server.url, TOKEN) as client:
            with self.assertRaises(CenterTransportError) as caught:
                await client.action("game-pc", Action(throttle=0.3))
        self.assertTrue(caught.exception.may_have_executed)
        self.assertEqual(len(self.server.requests), 1)

    async def test_malformed_list_and_non_json_response_fail_explicitly(self):
        for response in ({"terminals": "not-a-list"}, {"terminals": [{"terminal_id": "pc", "connected": "yes"}]},
                         web.Response(text="bad-json", content_type="application/json")):
            self.server.response_overrides["terminals"] = response
            async with CenterClient(self.server.url, TOKEN) as client:
                with self.assertRaises(CenterProtocolError):
                    await client.list_terminals()

    async def test_observe_rejects_stale_or_invalid_image_metadata(self):
        async with CenterClient(self.server.url, TOKEN) as client:
            with self.assertRaises(CenterProtocolError):
                await client.observe("game-pc", after=1)
            self.server.frame_headers["X-Frame-Timestamp"] = "nan"
            with self.assertRaises(CenterProtocolError):
                await client.observe("game-pc")
            self.server.frame_headers["X-Frame-Timestamp"] = str(time.time())
            self.server.frame_body = b"not-a-jpeg"
            with self.assertRaises(CenterProtocolError):
                await client.observe("game-pc")

    async def test_step_rejects_mismatched_ack_or_observation_before_action_end(self):
        action = Action(duration_ms=100)
        now = time.time()
        response = {"terminal_id": "game-pc", "command_id": "command-1", "ack": {
            "type": "ack", "command_id": "wrong-command", "ok": True,
            "applied_at": now, "state": action.to_payload(),
        }, "observation": {"sequence": 2, "captured_at": now + 0.01, "received_at": now + 0.02,
                            "content_type": "image/jpeg", "jpeg_base64": base64.b64encode(JPEG).decode()}}
        self.server.response_overrides["step"] = response
        async with CenterClient(self.server.url, TOKEN) as client:
            with self.assertRaises(CenterProtocolError):
                await client.step("game-pc", action)
            response["ack"]["command_id"] = "command-1"
            with self.assertRaises(CenterProtocolError):
                await client.step("game-pc", action)
            response["observation"]["captured_at"] = now + 0.2
            response["observation"]["jpeg_base64"] = "not valid base64?!"
            with self.assertRaises(CenterProtocolError):
                await client.step("game-pc", action)

    async def test_auth_failure_does_not_leak_pairing_token(self):
        self.server.response_overrides["terminals"] = web.json_response(
            {"error": "bad credential " + TOKEN}, status=401)
        async with CenterClient(self.server.url, TOKEN) as client:
            with self.assertRaises(CenterAPIError) as caught:
                await client.list_terminals()
        self.assertEqual(caught.exception.status, 401)
        self.assertNotIn(TOKEN, str(caught.exception))


class ActionBoundaryTests(unittest.TestCase):
    def test_invalid_policy_output_cannot_become_an_action(self):
        for values in ({"throttle": True}, {"steering": float("nan")}, {"brake": 2},
                       {"steering": 10 ** 1000}, {"buttons": ("INVALID",)},
                       {"duration_ms": -1}, {"duration_ms": 5001}):
            with self.subTest(values=values), self.assertRaises((TypeError, ValueError)):
                Action(**values)


if __name__ == "__main__":
    unittest.main()
