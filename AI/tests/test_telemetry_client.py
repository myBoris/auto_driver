"""CenterClient 遥测契约校验；只有 GET，不发送任何游戏操作。"""

import copy
import time
import unittest

from src.integrations.center import CenterClient, CenterProtocolError
from fake_center import FakeCenter, TOKEN


def snapshot():
    return {"terminal_id": "game-pc", "session_id": "session-1", "connected": True,
            "sequence": 4, "status": "ready", "ttl_ms": 1000, "age_ms": 10,
            "captured_at": time.time() - .01, "received_at": time.time(),
            "data": {"source": "fh5_data_out", "is_race_on": True, "timestamp_ms": 1234,
                     "speed_mps": 12.5, "position": [1, 2, 3], "velocity": [0, 0, 12.5], "yaw": .5}}


class TelemetryClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = await FakeCenter().start()
        self.server.response_overrides["telemetry"] = snapshot()

    async def asyncTearDown(self):
        await self.server.close()

    async def test_get_sends_cursor_auth_and_returns_center_age(self):
        async with CenterClient(self.server.url, TOKEN) as client:
            result = await client.get_telemetry("game-pc", session_id="session-1", after=3, timeout=1)
        self.assertEqual(result["data"]["velocity"][2], 12.5)
        self.assertEqual(result["age_ms"], 10)
        self.assertEqual(self.server.requests[0]["query"], {"session_id": "session-1", "after": "3", "timeout": "1.0"})
        self.assertEqual(self.server.requests[0]["authorization"], f"Bearer {TOKEN}")
        self.assertEqual(self.server.requests[0]["method"], "GET")

    async def test_waiting_stale_and_disconnected_never_retain_old_data(self):
        async with CenterClient(self.server.url, TOKEN) as client:
            for status in ("waiting", "stale", "disconnected"):
                value = snapshot()
                value.update(status=status, data=None, connected=status != "disconnected")
                self.server.response_overrides["telemetry"] = value
                self.assertIsNone((await client.get_telemetry("game-pc"))["data"])
                value["data"] = snapshot()["data"]
                with self.assertRaises(CenterProtocolError):
                    await client.get_telemetry("game-pc")

    async def test_mismatch_stale_ready_bad_units_and_nonfinite_are_rejected(self):
        bad_values = []
        for updates in ({"session_id": "new"}, {"terminal_id": "other"}, {"sequence": 3},
                        {"age_ms": 1001}, {"connected": False}, {"sequence": True}, {"age_ms": None}):
            value = snapshot()
            value.update(updates)
            bad_values.append(value)
        for updates in ({"velocity": [1, 2]}, {"speed_mps": -1}, {"yaw": float("nan")},
                        {"throttle": 2}, {"timestamp_ms": True}, {"collision": False}):
            value = copy.deepcopy(snapshot())
            value["data"].update(updates)
            bad_values.append(value)
        async with CenterClient(self.server.url, TOKEN) as client:
            for value in bad_values:
                self.server.response_overrides["telemetry"] = value
                with self.subTest(value=value), self.assertRaises(CenterProtocolError):
                    await client.get_telemetry("game-pc", session_id="session-1", after=3)

    async def test_argument_validation_happens_before_request(self):
        async with CenterClient(self.server.url, TOKEN) as client:
            for kwargs in ({"after": -1}, {"after": True}, {"timeout": 21}, {"session_id": ""}):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    await client.get_telemetry("game-pc", **kwargs)
        self.assertEqual(self.server.requests, [])


if __name__ == "__main__":
    unittest.main()
