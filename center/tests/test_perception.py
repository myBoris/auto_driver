"""外部 AI 结果发布与浏览器读取；不运行推理、不操作真实终端。"""

import math
import unittest
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from center.app import create_app
from center.client import CenterClient
from center.config import CenterConfig
from center.dependencies import PERCEPTION, SESSIONS


TOKEN = "perception-test-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
PATH = "/api/terminals/game-pc/perception"


def example(session_id, **updates):
    value = {
        "session_id": session_id, "frame_sequence": 42, "captured_at": 1234.5,
        "status": "ready", "image": {"width": 1920, "height": 1080},
        "road": {"polygon": [[.3, .75], [.45, .4], [.55, .4], [.7, .75]],
                 "centerline": [[.5, .75], [.52, .4]], "confidence": .92},
        "prediction": {"steering": -.2, "confidence": .86, "source": "model"},
        "model": {"id": "test-model", "label": "Test model"}, "inference_ms": 25,
    }
    value.update(updates)
    return value


class PerceptionAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = TestClient(TestServer(create_app(CenterConfig(
            token=TOKEN, enable_discovery=False,
        ))))
        await self.client.start_server()
        self.ws, self.session_id = await self.connect()

    async def asyncTearDown(self):
        await self.client.close()

    async def connect(self, terminal_id="game-pc"):
        ws = await self.client.ws_connect("/ws/terminal", headers=AUTH)
        await ws.send_json({"type": "hello", "protocol": 1, "terminal_id": terminal_id,
                            "name": "Perception test terminal"})
        await ws.receive_json(timeout=1)
        return ws, self.client.server.app[SESSIONS][terminal_id].session_id

    async def publish(self, value=None, *, headers=None, path=PATH):
        return await self.client.post(path, headers=AUTH if headers is None else headers,
                                      json=value if value is not None else example(self.session_id))

    async def read(self, *, headers=None, path=PATH):
        return await self.client.get(path, headers=AUTH if headers is None else headers)

    async def test_publish_and_read_without_jpeg_is_read_only_and_labels_timing(self):
        before = await (await self.read()).json()
        self.assertEqual(before["status"], "waiting")
        self.assertIsNone(before["road"])
        self.assertIsNone(before["prediction"])
        self.assertIsNone(before["received_at"])
        with patch("center.services.perception.monotonic", return_value=100):
            response = await self.publish()
            self.assertEqual(response.status, 200, await response.text())
            published = await response.json()
        with patch("center.services.perception.monotonic", return_value=100.25):
            response = await self.read()
            data = await response.json()
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(data["terminal_id"], "game-pc")
        self.assertEqual(data["session_id"], self.session_id)
        self.assertEqual(data["status"], "ready")
        self.assertEqual(data["road"], example(self.session_id)["road"])
        self.assertEqual(data["prediction"]["steering"], -.2)
        self.assertEqual(data["frame"], {"sequence": 42, "captured_at": 1234.5, "received_at": None})
        self.assertEqual(data["age_ms"], 250)
        self.assertEqual(data["ttl_ms"], 2000)
        self.assertEqual(data["received_at"], published["received_at"])
        terminal = self.client.server.app[SESSIONS]["game-pc"]
        self.assertIsNone(terminal.frame)
        self.assertEqual(terminal.pending, {})
        self.assertEqual(terminal.control_epoch, 0)

    async def test_lightweight_sdk_publishes_and_reads_via_real_http_without_game_commands(self):
        terminal = self.client.server.app[SESSIONS]["game-pc"]
        with (patch.object(terminal, "command", side_effect=AssertionError("perception must not control the game")) as command,
              patch.object(terminal, "cancel_operations", side_effect=AssertionError("perception must not release inputs")) as release):
            async with CenterClient(str(self.client.make_url("/")), TOKEN) as sdk:
                listed = await sdk.terminals()
                self.assertEqual(listed[0]["session_id"], self.session_id)
                empty = await sdk.perception("game-pc")
                self.assertEqual(empty["status"], "waiting")
                payload = example(self.session_id)
                payload["prediction"]["source"] = "demo"
                published = await sdk.publish_perception("game-pc", payload)
                latest = await sdk.perception("game-pc")
                self.assertEqual(latest["status"], "ready")
                self.assertEqual(latest["terminal_id"], "game-pc")
                self.assertEqual(latest["session_id"], self.session_id)
                self.assertEqual(latest["road"], payload["road"])
                self.assertEqual(latest["prediction"], payload["prediction"])
                self.assertEqual(latest["received_at"], published["received_at"])
                self.assertEqual(latest["frame"]["sequence"], payload["frame_sequence"])
                self.assertEqual(latest["ttl_ms"], 2000)
                self.assertGreaterEqual(latest["age_ms"], published["age_ms"])
            command.assert_not_called()
            release.assert_not_called()
        self.assertEqual(terminal.pending, {})
        self.assertEqual(terminal.control_epoch, 0)

    async def test_freshness_uses_server_monotonic_time_and_stale_geometry_is_cleared(self):
        with patch("center.services.perception.monotonic", return_value=100):
            response = await self.publish(example(self.session_id, captured_at=1e19))
            self.assertEqual(response.status, 200)
        with patch("center.services.perception.monotonic", return_value=102.001):
            data = await (await self.read()).json()
        self.assertEqual(data["status"], "stale")
        self.assertGreater(data["age_ms"], data["ttl_ms"])
        self.assertIsNone(data["road"])
        self.assertIsNone(data["prediction"])
        self.assertNotIn("image", data)

    async def test_optional_frame_metadata_and_inference_time_remain_unknown(self):
        value = example(self.session_id)
        for key in ("frame_sequence", "captured_at", "inference_ms"):
            value.pop(key)
        value["model"].pop("label")
        value["prediction"].pop("source")
        data = await (await self.publish(value)).json()
        self.assertIsNone(data["frame"]["sequence"])
        self.assertIsNone(data["frame"]["captured_at"])
        self.assertIsNone(data["inference_ms"])
        self.assertEqual(data["model"]["label"], "test-model")
        self.assertEqual(data["prediction"]["source"], "model")

    async def test_optional_sequence_never_erases_the_reordering_watermark(self):
        self.assertEqual((await self.publish()).status, 200)
        no_sequence = example(self.session_id)
        no_sequence.pop("frame_sequence")
        self.assertEqual((await self.publish(no_sequence)).status, 200)
        response = await self.publish(example(self.session_id, frame_sequence=41))
        self.assertEqual(response.status, 409)
        self.assertIn("older", (await response.json())["error"])
        self.assertEqual((await self.publish(example(self.session_id, frame_sequence=42))).status, 200)
        self.assertEqual((await self.publish(example(self.session_id, frame_sequence=43))).status, 200)
        self.assertEqual((await (await self.read()).json())["frame"]["sequence"], 43)

    async def test_terminal_reconnect_clears_old_result_and_rejects_old_publisher(self):
        self.assertEqual((await self.publish()).status, 200)
        _, new_session = await self.connect()
        self.assertNotEqual(new_session, self.session_id)
        data = await (await self.read()).json()
        self.assertEqual(data["session_id"], new_session)
        self.assertEqual(data["status"], "waiting")
        self.assertIsNone(data["road"])
        self.assertEqual((await self.publish()).status, 409)
        self.assertEqual((await self.publish(example(new_session, frame_sequence=1))).status, 200)

    async def test_terminals_are_isolated(self):
        _, second_session = await self.connect("second")
        self.assertEqual((await self.publish()).status, 200)
        path = "/api/terminals/second/perception"
        self.assertEqual((await (await self.read(path=path)).json())["status"], "waiting")
        self.assertEqual((await self.publish(example(second_session, frame_sequence=1), path=path)).status, 200)
        self.assertEqual((await (await self.read()).json())["frame"]["sequence"], 42)
        self.assertEqual((await (await self.read(path=path)).json())["frame"]["sequence"], 1)

    async def test_road_and_prediction_can_be_published_independently(self):
        for change in ({"road": None}, {"prediction": None}, {"road": None, "status": "no_road"}):
            with self.subTest(change=change):
                response = await self.publish(example(self.session_id, **change))
                self.assertEqual(response.status, 200, await response.text())
        value = example(self.session_id, road=None, prediction=None, status="no_road")
        self.assertEqual((await self.publish(value)).status, 200)
        value["status"] = "unavailable"
        self.assertEqual((await self.publish(value)).status, 200)

    async def test_demo_and_geometric_sources_are_preserved_without_model_claim(self):
        for source in ("demo", "road_geometry", "external", "model"):
            with self.subTest(source=source):
                value = example(self.session_id)
                value["prediction"]["source"] = source
                data = await (await self.publish(value)).json()
                self.assertEqual(data["prediction"]["source"], source)

    async def test_unknown_or_disconnected_terminal_cannot_publish(self):
        self.assertEqual((await self.read(path="/api/terminals/missing/perception")).status, 404)
        self.assertEqual((await self.publish(path="/api/terminals/missing/perception")).status, 404)
        terminal = self.client.server.app[SESSIONS]["game-pc"]
        await terminal.disconnect()
        self.assertEqual((await self.publish()).status, 409)

    async def test_existing_result_becomes_stale_after_disconnect(self):
        self.assertEqual((await self.publish()).status, 200)
        await self.client.server.app[SESSIONS]["game-pc"].disconnect()
        data = await (await self.read()).json()
        self.assertEqual(data["status"], "stale")
        self.assertIsNone(data["road"])
        self.assertIsNone(data["prediction"])

    async def test_authentication_and_browser_origin_rules_cover_new_endpoint(self):
        self.assertEqual((await self.read(headers={})).status, 401)
        self.assertEqual((await self.publish(headers={})).status, 401)
        self.assertEqual((await self.publish(headers={"Authorization": "Bearer wrong"})).status, 401)
        await self.client.get("/dashboard-session")
        self.assertEqual((await self.read(headers={})).status, 200)
        self.assertEqual((await self.publish(headers={})).status, 200)
        self.assertEqual((await self.publish(headers={"Origin": "http://foreign.example"})).status, 403)
        self.assertEqual((await self.publish(headers={**AUTH, "Origin": "http://foreign.example"})).status, 200)

    async def test_invalid_metadata_geometry_and_predictions_never_replace_valid_result(self):
        self.assertEqual((await self.publish()).status, 200)
        invalid = [
            ("frame_sequence", True), ("frame_sequence", 0), ("frame_sequence", 1.1),
            ("frame_sequence", 2**53), ("captured_at", -1), ("captured_at", math.inf),
            ("captured_at", 10**1000), ("inference_ms", 10**1000),
            ("inference_ms", math.nan), ("status", "invented"), ("model", {}),
            ("model", {"id": "m" * 257}), ("image", {"width": 0, "height": 1080}),
            ("image", {"width": True, "height": 1080}), ("extra", 1),
            ("road", {"polygon": [[.1, .1]] * 257, "centerline": [[.5, .7], [.5, .4]], "confidence": .8}),
            ("road", {"polygon": [[0, 0], [1, 1], [0, 0]], "centerline": [[.5, .7], [.5, .4]], "confidence": .8}),
            ("prediction", {"steering": 1.1, "confidence": .8}),
            ("prediction", {"steering": .3, "confidence": -.1}),
            ("prediction", {"steering": .3, "confidence": .8, "source": "unknown"}),
        ]
        for key, value in invalid:
            with self.subTest(key=key, value=value):
                response = await self.publish(example(self.session_id, **{key: value}))
                self.assertEqual(response.status, 400, await response.text())
        value = example(self.session_id)
        value["road"]["polygon"][0][0] = math.nan
        self.assertEqual((await self.publish(value)).status, 400)
        value = example(self.session_id)
        value["road"]["centerline"][0][1] = 1.1
        self.assertEqual((await self.publish(value)).status, 400)
        data = await (await self.read()).json()
        self.assertEqual(data["road"], example(self.session_id)["road"])

    async def test_missing_session_invalid_json_and_oversized_body_are_rejected(self):
        value = example(self.session_id)
        value.pop("session_id")
        self.assertEqual((await self.publish(value)).status, 409)
        for payload in ("{", "[]"):
            response = await self.client.post(PATH, headers={**AUTH, "Content-Type": "application/json"}, data=payload)
            self.assertEqual(response.status, 400)
        response = await self.client.post(PATH, headers={**AUTH, "Content-Type": "application/json"}, data="x" * (65 * 1024))
        self.assertEqual(response.status, 413)

    async def test_inactive_status_cannot_retain_old_geometry(self):
        for update in ({"status": "no_road"}, {"status": "waiting"}, {"status": "unavailable"}, {"road": None, "prediction": None}):
            with self.subTest(update=update):
                self.assertEqual((await self.publish(example(self.session_id, **update))).status, 400)


if __name__ == "__main__":
    unittest.main()
