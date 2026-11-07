"""实时采集的 HTTP 只读性、清单一致性、后台训练及取消清理。"""

import asyncio
from io import BytesIO
from pathlib import Path
import tempfile
import threading
import unittest

from PIL import Image

from fake_center import FakeCenter, TOKEN
from src.data.dataset import load_samples
from src.integrations.center import CenterClient, CenterProtocolError
from src.services import resolve_terminal
from src.services.live_training import run_live_training
from src.services.training_collection import collect_training_samples
from test_training_collection import snapshot


class RecordingTrainer:
    def __init__(self):
        self.snapshots = []
        self.started = threading.Event()
        self.proceed = threading.Event()
        self.proceed.set()

    def update(self, root):
        self.started.set()
        if not self.proceed.wait(5):
            raise RuntimeError("test worker did not finish")
        rows = load_samples(root)
        self.snapshots.append(rows)
        return {"status": "updated", "samples": len(rows), "version": len(self.snapshots)}


class LiveTrainingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = await FakeCenter().start()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        data = BytesIO()
        Image.new("RGB", (40, 24), (25, 80, 110)).save(data, format="JPEG")
        self.server.frame_body = data.getvalue()
        self.server.auto_frame_sequence = True
        self.server.response_overrides["control"] = snapshot(self.server.captured_at)
        self.trainer = RecordingTrainer()

    async def asyncTearDown(self):
        self.trainer.proceed.set()
        await self.server.close()

    def assert_read_only_clean(self):
        self.assertFalse(any(row["method"] == "POST" for row in self.server.requests))
        self.assertFalse((self.root / ".live.lock").exists())
        self.assertFalse((self.root / ".collection.lock").exists())

    async def test_chunks_share_episode_and_final_manifest_is_trained_without_post_requests(self):
        async with CenterClient(self.server.url, TOKEN) as client:
            result = await run_live_training(client, await resolve_terminal(client), self.root,
                                             self.trainer, frames=7, chunk_frames=2, interval=0)
        rows = load_samples(self.root)
        self.assertEqual(result["frames"], 7)
        self.assertEqual(result["control_labels"], 7)
        self.assertEqual(len(rows), 7)
        self.assertEqual(len({row["episode_id"] for row in rows}), 1)
        self.assertEqual(len({row["split_group"] for row in rows}), 1)
        self.assertEqual(len(self.trainer.snapshots[-1]), 7)
        self.assertTrue(all(row["action_source"] == "center_confirmed" for row in rows))
        self.assert_read_only_clean()

    async def test_slow_training_does_not_block_collection_and_updates_are_coalesced(self):
        self.trainer.proceed.clear()
        async with CenterClient(self.server.url, TOKEN) as client:
            task = asyncio.create_task(run_live_training(client, await resolve_terminal(client), self.root,
                                                         self.trainer, frames=8, chunk_frames=2, interval=.01))
            for _ in range(300):
                if (self.root / "samples.jsonl").exists() and len(load_samples(self.root)) == 8:
                    break
                await asyncio.sleep(.01)
            self.assertTrue(self.trainer.started.is_set())
            self.assertEqual(len(load_samples(self.root)), 8)
            self.assertFalse(task.done())
            self.trainer.proceed.set()
            await task
        self.assertLessEqual(len(self.trainer.snapshots), 2)
        self.assertEqual(len(self.trainer.snapshots[-1]), 8)
        self.assert_read_only_clean()

    async def test_cancel_saves_partial_chunk_and_waits_for_current_worker(self):
        self.trainer.proceed.clear()
        async with CenterClient(self.server.url, TOKEN) as client:
            task = asyncio.create_task(run_live_training(client, await resolve_terminal(client), self.root,
                                                         self.trainer, chunk_frames=2, interval=.02))
            for _ in range(300):
                if self.trainer.started.is_set():
                    break
                await asyncio.sleep(.01)
            task.cancel()
            await asyncio.sleep(.02)
            self.assertFalse(task.done())
            self.trainer.proceed.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertGreaterEqual(len(load_samples(self.root)), 2)
        self.assert_read_only_clean()

    async def test_session_change_does_not_label_or_train_wrong_session(self):
        self.server.response_overrides["control"] = snapshot(self.server.captured_at, session_id="other-session")
        async with CenterClient(self.server.url, TOKEN) as client:
            with self.assertRaises(CenterProtocolError):
                await run_live_training(client, await resolve_terminal(client), self.root,
                                        self.trainer, frames=1, interval=0)
        self.assertFalse((self.root / "samples.jsonl").exists())
        self.assertEqual(self.trainer.snapshots, [])
        self.assert_read_only_clean()

    async def test_auto_candidates_are_saved_with_source_and_do_not_fabricate_controls(self):
        candidate = {"lane": {"visible": True, "left": [[.3, .45], [.1, .65]],
                               "right": [[.7, .45], [.9, .65]]}, "lane_source": "auto",
                     "lane_annotation": {"method": "opencv-test", "confidence": .9, "reviewed": False}}

        class Labeler:
            def annotate(self, jpeg):
                return candidate

        self.server.response_overrides["control"] = snapshot(self.server.captured_at, status="idle")
        async with CenterClient(self.server.url, TOKEN) as client:
            result = await run_live_training(client, await resolve_terminal(client), self.root,
                                             self.trainer, frames=3, chunk_frames=2, interval=0,
                                             auto_labeler=Labeler())
        self.assertEqual(result["auto_lane_labels"], 3)
        self.assertEqual(result["control_labels"], 0)
        self.assertTrue(all(row["lane_source"] == "auto" and "action" not in row for row in load_samples(self.root)))
        self.assert_read_only_clean()

    async def test_existing_live_lock_is_preserved_and_finite_collector_refuses_it(self):
        lock = self.root / ".live.lock"
        lock.write_text("another process")
        async with CenterClient(self.server.url, TOKEN) as client:
            terminal = await resolve_terminal(client)
            with self.assertRaisesRegex(RuntimeError, "已有实时"):
                await run_live_training(client, terminal, self.root, self.trainer, frames=1)
            with self.assertRaisesRegex(RuntimeError, "实时"):
                await collect_training_samples(client, terminal, self.root, frames=1)
        self.assertEqual(lock.read_text(), "another process")

    async def test_actual_incremental_model_trains_from_http_controls_and_auto_lanes(self):
        import torch
        from src.data.auto_lanes import AutoLaneLabeler
        from src.policies.driving import DrivingPolicy
        from src.training.online import OnlineTrainer, OnlineTrainingConfig
        from test_auto_lanes import road_image
        from test_training import tiny_config

        previous_threads = torch.get_num_threads()
        torch.set_num_threads(1)
        self.addCleanup(torch.set_num_threads, previous_threads)
        self.server.frame_body = road_image()
        output = self.root / "model"
        trainer = OnlineTrainer(OnlineTrainingConfig(
            base=tiny_config(self.root, output, validation_split=0, allow_auto_lanes=True),
            min_samples=4, steps_per_update=2, replay_size=8))
        async with CenterClient(self.server.url, TOKEN) as client:
            result = await run_live_training(client, await resolve_terminal(client), self.root,
                                             trainer, frames=6, chunk_frames=2, interval=0,
                                             auto_labeler=AutoLaneLabeler())
        self.assertEqual(result["training"]["status"], "updated")
        self.assertTrue((output / "latest.pt").is_file())
        self.assertFalse((output / ".live-training.lock").exists())
        policy = DrivingPolicy(output / "latest.pt", device="cpu")
        self.assertEqual(set(policy.trained_tasks), {"control", "direction", "lane"})
        checkpoint = torch.load(output / "latest.pt", weights_only=True)
        self.assertTrue(checkpoint["auto_lane_labels"])
        self.assertEqual(checkpoint["lane_label_sources"], ["auto"])
        self.assert_read_only_clean()


if __name__ == "__main__":
    unittest.main()
