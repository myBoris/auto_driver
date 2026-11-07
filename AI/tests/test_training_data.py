"""验证标签缺失掩码、历史边界、在线离线一致性和本地标注的真实保存。"""

import copy
import json
from pathlib import Path
import tempfile
import unittest

from aiohttp.test_utils import TestClient, TestServer

from src.data.annotations import create_annotation_app
from src.data.dataset import DrivingDataset, load_samples, write_samples
from src.data.preprocessing import ObservationPreprocessor
from src.data.synthetic import generate_synthetic_dataset
from src.domain import Observation


class TrainingDataTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        generate_synthetic_dataset(self.root, episodes=2, frames_per_episode=3)
        self.samples = load_samples(self.root)

    def test_synthetic_requires_explicit_selection_and_never_overwrites(self):
        self.assertEqual(len(DrivingDataset(self.root)), 0)
        dataset = DrivingDataset(self.root, allow_synthetic=True)
        self.assertEqual(len(dataset), 6)
        self.assertEqual(dataset.label_counts, {"control": 6, "direction": 6, "lane": 6})
        with self.assertRaisesRegex(ValueError, "必须为空"):
            generate_synthetic_dataset(self.root)
        self.assertEqual(load_samples(self.root), self.samples)

    def test_shared_preprocessing_is_exact_for_online_jpeg_and_offline_paths(self):
        import torch

        preprocessor = ObservationPreprocessor(image_size=32, history_frames=2)
        first, second = self.samples[:2]
        paths = [self.root / sample["image"] for sample in (first, second)]
        observations = [Observation(path.read_bytes(), index + 1, 100 + index, 100.1 + index)
                        for index, path in enumerate(paths)]
        online = preprocessor.transform(observations)
        self.assertEqual(tuple(online.shape), (2, 3, 32, 32))
        self.assertTrue(torch.equal(online, preprocessor.transform_paths(paths)))
        self.assertEqual(online.dtype, torch.float32)
        self.assertGreaterEqual(online.min().item(), 0)
        self.assertLessEqual(online.max().item(), 1)
        self.assertEqual(ObservationPreprocessor.from_dict(preprocessor.to_dict()).to_dict(), preprocessor.to_dict())
        with self.assertRaises(ValueError):
            preprocessor.transform(observations[::-1])
        with self.assertRaises(ValueError):
            preprocessor.transform([])
        with self.assertRaises(ValueError):
            ObservationPreprocessor(history_frames=True)

    def test_missing_labels_are_masked_and_history_never_crosses_episodes(self):
        import torch

        self.samples[0].pop("action")
        self.samples[1].pop("lane")
        self.samples[2].pop("action")
        self.samples[2].pop("lane")
        write_samples(self.root, self.samples)
        dataset = DrivingDataset(self.root, history_frames=3, allow_synthetic=True)
        self.assertEqual(len(dataset), 5, "unlabelled images supply history but are not invented training targets")
        first = dataset[0]
        self.assertFalse(first["control_mask"].item())
        self.assertTrue(first["lane_label_mask"].item())
        self.assertEqual(first["image_mask"].tolist(), [False, False, True])
        self.assertEqual(torch.count_nonzero(first["images"][:2]).item(), 0)
        second_episode_index = dataset.episode_ids.index("synthetic-0002")
        self.assertEqual(dataset[second_episode_index]["image_mask"].tolist(), [False, False, True])
        self.assertFalse(dataset[1]["lane_label_mask"].item())
        self.assertEqual(dataset.label_counts, {"control": 4, "direction": 4, "lane": 4})

    def test_lane_interpolation_only_supervises_annotated_height_and_no_road_is_explicit(self):
        self.samples[0]["lane"] = {"visible": True,
                                    "left": [[.2, .4], [.3, .9]],
                                    "right": [[.8, .4], [.7, .9]]}
        self.samples[1]["lane"] = {"visible": False}
        write_samples(self.root, self.samples)
        dataset = DrivingDataset(self.root, lane_y=(.95, .65, .45), allow_synthetic=True)
        item = dataset[0]
        self.assertEqual(item["lane_mask"].tolist(), [False, True, True])
        self.assertAlmostEqual(item["lane_points"][1, 0].item(), .25)
        self.assertAlmostEqual(item["lane_points"][1, 1].item(), .75)
        absent = dataset[1]
        self.assertTrue(absent["lane_label_mask"].item())
        self.assertEqual(absent["lane_presence"].item(), 0)
        self.assertFalse(absent["lane_mask"].any().item())

    def test_ambiguous_or_partial_actions_and_bad_lane_geometry_fail_clearly(self):
        cases = [
            {"action": {"steering": 0}},
            {"action_source": "unknown"},
            {"lane": {"visible": True, "left": [[.8, .4], [.8, .9]], "right": [[.2, .4], [.2, .9]]}},
            {"lane": {"visible": False, "left": [[.2, .4], [.2, .9]]}},
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides):
                rows = copy.deepcopy(self.samples)
                rows[0].update(overrides)
                write_samples(self.root, rows)
                with self.assertRaisesRegex(ValueError, "第 1 行"):
                    load_samples(self.root)

    def test_manifest_rejects_duplicate_identity_cross_session_and_path_escape(self):
        cases = [
            (1, {"id": self.samples[0]["id"]}),
            (1, {"sequence": self.samples[0]["sequence"]}),
            (1, {"session_id": "different-session"}),
            (0, {"image": "../outside.jpg"}),
            (0, {"image": str((self.root / self.samples[0]["image"]).resolve())}),
        ]
        for index, overrides in cases:
            with self.subTest(overrides=overrides):
                rows = copy.deepcopy(self.samples)
                rows[index].update(overrides)
                write_samples(self.root, rows)
                with self.assertRaises(ValueError):
                    load_samples(self.root)

    def test_policy_proposed_labels_require_explicit_opt_in(self):
        rows = copy.deepcopy(self.samples)
        for row in rows:
            row["source"] = "center"
            row["action_source"] = "policy_proposed"
            row.pop("lane")
        write_samples(self.root, rows)
        self.assertEqual(len(DrivingDataset(self.root)), 0)
        self.assertEqual(len(DrivingDataset(self.root, allow_policy_labels=True)), 6)

    def test_narrow_lane_labels_and_mixed_synthetic_history_are_rejected(self):
        rows = copy.deepcopy(self.samples)
        rows[0]["lane"] = {"visible": True, "left": [[.2, .11], [.3, .21]],
                           "right": [[.8, .11], [.7, .21]]}
        write_samples(self.root, rows)
        with self.assertRaisesRegex(ValueError, "两个 lane_y"):
            DrivingDataset(self.root, allow_synthetic=True)
        rows = copy.deepcopy(self.samples)
        rows[1]["source"] = "center"
        write_samples(self.root, rows)
        with self.assertRaisesRegex(ValueError, "真实和合成"):
            DrivingDataset(self.root, allow_synthetic=True)


class AnnotationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        generate_synthetic_dataset(self.root, episodes=2, frames_per_episode=2)
        self.samples = load_samples(self.root)
        self.client = TestClient(TestServer(create_annotation_app(self.root)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_page_lists_samples_and_serves_only_manifest_images(self):
        response = await self.client.get("/")
        self.assertEqual(response.status, 200)
        self.assertIn("驾驶训练数据标注", await response.text())
        listing = await (await self.client.get("/api/samples")).json()
        self.assertEqual(len(listing["samples"]), 4)
        image = await self.client.get("/api/image/0")
        self.assertEqual(await image.read(), (self.root / self.samples[0]["image"]).read_bytes())
        self.assertEqual((await self.client.get("/api/image/-1")).status, 404)
        self.assertEqual((await self.client.get("/api/image/99")).status, 404)

    async def test_saved_human_labels_are_atomic_and_do_not_modify_images_or_identity(self):
        before = (self.root / self.samples[0]["image"]).read_bytes()
        payload = {"action": {"steering": -.4, "throttle": .2, "brake": 0, "buttons": ["LB", "X"], "duration_ms": 100},
                   "lane": {"visible": False}}
        response = await self.client.post("/api/samples/0", json=payload)
        self.assertEqual(response.status, 200, await response.text())
        saved = load_samples(self.root)[0]
        self.assertEqual(saved["action_source"], "human")
        self.assertEqual(saved["lane_source"], "human")
        self.assertEqual(saved["source"], "synthetic", "manual edits cannot hide original synthetic provenance")
        self.assertEqual(saved["action"]["buttons"], ["LB", "X"])
        self.assertEqual(saved["id"], self.samples[0]["id"])
        self.assertEqual((self.root / saved["image"]).read_bytes(), before)
        self.assertFalse((self.root / ".collection.lock").exists())
        self.assertFalse(list(self.root.glob("samples-*.tmp")))

    async def test_active_collection_bad_labels_and_foreign_origin_cannot_modify_dataset(self):
        before = (self.root / "samples.jsonl").read_bytes()
        lock = self.root / ".collection.lock"
        lock.touch()
        self.assertEqual((await self.client.post("/api/samples/0", json={"lane": {"visible": False}})).status, 409)
        lock.unlink()
        response = await self.client.post("/api/samples/0", json={"action": {"throttle": .5}})
        self.assertEqual(response.status, 400)
        response = await self.client.post("/api/samples/0", json={"lane": {"visible": False}}, headers={"Origin": "http://foreign.invalid"})
        self.assertEqual(response.status, 403)
        self.assertEqual((self.root / "samples.jsonl").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
