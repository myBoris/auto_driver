"""真实 HTTP 读取采集：确认动作时间对齐、会话边界与无驾驶写请求。"""

import asyncio
from io import BytesIO
import json
from pathlib import Path
import tempfile
import time
import unittest

from PIL import Image

from src.data.dataset import load_samples
from src.domain import Observation
from src.integrations.center import CenterClient, CenterProtocolError
from src.services import resolve_terminal
from src.services.training_collection import aligned_action, collect_training_samples
from fake_center import FakeCenter, TOKEN


def snapshot(captured_at, **changes):
    return {"terminal_id": "game-pc", "session_id": "session-1", "connected": True,
            "revision": 2, "status": "active", "command_id": "human-command",
            "applied_at": captured_at - .05, "remaining_ms": 900,
            "state": {"steering": -.4, "throttle": .35, "brake": .1, "buttons": ["LB"], "duration_ms": 1000}, **changes}


class TrainingCollectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = await FakeCenter().start()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        data = BytesIO()
        Image.new("RGB", (40, 24), (25, 80, 110)).save(data, format="JPEG")
        self.server.frame_body = data.getvalue()
        self.server.auto_frame_sequence = True

    async def asyncTearDown(self):
        await self.server.close()

    async def test_collects_center_frames_and_only_aligned_confirmed_inputs_without_control_posts(self):
        self.server.response_overrides["control"] = snapshot(self.server.captured_at)
        async with CenterClient(self.server.url, TOKEN) as client:
            terminal = await resolve_terminal(client)
            summary = await collect_training_samples(client, terminal, self.root, frames=3, interval=0)
        records = load_samples(self.root)
        self.assertEqual(summary["control_labels"], 3)
        self.assertEqual(len(records), 3)
        self.assertTrue(all(item["action_source"] == "center_confirmed" for item in records))
        self.assertTrue(all(item["action"]["steering"] == -.4 for item in records))
        self.assertTrue(all("lane" not in item for item in records))
        self.assertFalse(any(item["method"] == "POST" for item in self.server.requests))
        self.assertFalse((self.root / ".collection.lock").exists())
        after = [item["query"].get("after") for item in self.server.requests if item["path"].endswith("/frame")]
        self.assertEqual(after, [None, "2", "3"])

    async def test_outside_active_capture_interval_remains_unlabeled(self):
        self.server.response_overrides["control"] = snapshot(self.server.captured_at, applied_at=self.server.captured_at + 1)
        async with CenterClient(self.server.url, TOKEN) as client:
            summary = await collect_training_samples(client, await resolve_terminal(client), self.root, frames=1, interval=0)
        self.assertEqual(summary["control_labels"], 0)
        self.assertNotIn("action", load_samples(self.root)[0])

    async def test_images_only_works_without_control_endpoint_and_separate_runs_create_episodes(self):
        async with CenterClient(self.server.url, TOKEN) as client:
            terminal = await resolve_terminal(client)
            for _ in range(2):
                await collect_training_samples(client, terminal, self.root, frames=1, interval=0, include_controls=False)
        records = load_samples(self.root)
        self.assertEqual(len({item["episode_id"] for item in records}), 2)
        self.assertFalse(any(item["path"].endswith("/control") for item in self.server.requests))

    async def test_changed_session_and_wrong_control_snapshot_stop_without_appending_or_writing(self):
        self.server.response_overrides["control"] = snapshot(self.server.captured_at, session_id="new-session")
        async with CenterClient(self.server.url, TOKEN) as client:
            with self.assertRaises(CenterProtocolError):
                await collect_training_samples(client, await resolve_terminal(client), self.root, frames=1, interval=0)
        self.assertEqual((self.root / "samples.jsonl").read_text(), "")
        self.assertFalse((self.root / ".collection.lock").exists())
        self.assertFalse(any(item["method"] == "POST" for item in self.server.requests))

    async def test_locked_dataset_cannot_be_collected_and_cancellation_removes_own_lock(self):
        lock = self.root / ".collection.lock"
        lock.write_text("another collector")
        async with CenterClient(self.server.url, TOKEN) as client:
            terminal = await resolve_terminal(client)
            with self.assertRaisesRegex(RuntimeError, "正在采集"):
                await collect_training_samples(client, terminal, self.root, frames=1)
            self.assertEqual(lock.read_text(), "another collector")
            lock.unlink()
            self.server.response_overrides["control"] = snapshot(self.server.captured_at)
            task = asyncio.create_task(collect_training_samples(client, terminal, self.root, frames=5, interval=60))
            for _ in range(100):
                if (self.root / "samples.jsonl").exists() and (self.root / "samples.jsonl").stat().st_size:
                    break
                await asyncio.sleep(.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(lock.exists())
        self.assertFalse(any(item["method"] == "POST" for item in self.server.requests))


class AlignmentTests(unittest.TestCase):
    def test_only_same_session_and_terminal_clock_interval_is_labeled(self):
        observation = Observation(b"\xff\xd8test\xff\xd9", 1, time.time(), time.time())
        active = snapshot(observation.captured_at)
        self.assertEqual(aligned_action(observation, active, "session-1")["throttle"], .35)
        for changes in ({"status": "idle"}, {"status": "expired"}, {"session_id": "old"},
                        {"connected": False}, {"applied_at": observation.captured_at - 2}):
            self.assertIsNone(aligned_action(observation, {**active, **changes}, "session-1"))


if __name__ == "__main__":
    unittest.main()
