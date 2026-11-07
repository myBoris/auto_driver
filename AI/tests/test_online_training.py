"""增量更新验证：持续优化器状态、固定片段隔离、有界回放及原子权重。"""

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

import torch
from PIL import Image

from src.data.dataset import load_samples, write_samples
from src.data.synthetic import generate_synthetic_dataset
from src.training import OnlineTrainer, OnlineTrainingConfig
from tests.test_training import make_labels, tiny_config


class OnlineTrainingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.threads = torch.get_num_threads()
        torch.set_num_threads(1)

    @classmethod
    def tearDownClass(cls):
        torch.set_num_threads(cls.threads)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.dataset = self.root / "data"
        self.output = self.root / "online"

    def trainer(self, *, min_samples=2, steps=3, replay=5, warm_start=None, **options):
        base = tiny_config(self.dataset, self.output, **{"validation_split": 0, **options})
        return OnlineTrainer(OnlineTrainingConfig(base, min_samples=min_samples,
                                                 steps_per_update=steps, replay_size=replay,
                                                 warm_start=warm_start))

    def append(self, samples, *, episode=None, count=3):
        template = deepcopy(next(sample for sample in samples if episode is None or sample["episode_id"] == episode))
        episode = template["episode_id"]
        sequence = max(sample["sequence"] for sample in samples if sample["episode_id"] == episode)
        added = []
        for number in range(count):
            sample = deepcopy(template)
            sample["sequence"] = sequence + number + 1
            sample["id"] = f"{episode}:new-{sample['sequence']}"
            sample["image"] = f"new-{episode}-{sample['sequence']}.jpg"
            Image.new("RGB", (48, 32), (120 + number, 130, 90)).save(self.dataset / sample["image"], "JPEG")
            added.append(sample)
        write_samples(self.dataset, samples + added)
        return samples + added

    def test_waits_for_labels_minimum_and_independent_holdout_without_loading_a_model(self):
        trainer = self.trainer(min_samples=5)
        self.assertEqual(trainer.update(self.dataset)["reason"], "no_manifest")
        samples = make_labels(self.dataset, episodes=1, frames=3)
        self.assertEqual(trainer.update(self.dataset)["reason"], "min_samples")
        self.assertIsNone(trainer.model)
        for sample in samples:
            sample.pop("action", None); sample.pop("lane", None)
        write_samples(self.dataset, samples)
        self.assertEqual(trainer.update(self.dataset)["reason"], "no_labels")
        make_labels(self.dataset, episodes=2, frames=3)
        samples = load_samples(self.dataset)
        for sample in samples:
            sample["split_group"] = "same-continuous-capture"
        write_samples(self.dataset, samples)
        grouped = self.trainer(validation_split=.5)
        self.assertEqual(grouped.update(self.dataset)["reason"], "need_independent_validation_episode")
        self.assertIsNone(grouped.model)

    def test_updates_persist_optimizer_and_weights_with_bounded_replay_and_unchanged_detection(self):
        samples = make_labels(self.dataset, episodes=1, frames=4)
        trainer = self.trainer(steps=3, replay=3)
        result = trainer.update(self.dataset)
        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["steps"], 3)
        model_id, optimizer_id = id(trainer.model), id(trainer.optimizer)
        first = torch.load(result["checkpoint"], weights_only=True)
        samples = self.append(samples, count=4)
        result = trainer.update(self.dataset)
        current = torch.load(result["checkpoint"], weights_only=True)
        self.assertEqual((id(trainer.model), id(trainer.optimizer)), (model_id, optimizer_id))
        self.assertEqual(result["optimizer_steps"], 6)
        self.assertEqual(current["model_id"], first["model_id"])
        self.assertEqual(current["online_version"], 2)
        self.assertTrue(any(not torch.equal(value, current["model_state"][name]) for name, value in first["model_state"].items()))
        self.assertLessEqual(result["replay_samples"], 3)
        for _ in range(20):
            result = trainer.update(self.dataset)
            if result["status"] == "unchanged":
                break
        self.assertEqual(result["status"], "unchanged")
        version = trainer.version
        self.assertEqual(trainer.update(self.dataset)["version"], version)
        self.assertEqual(json.loads((self.output / "status.json").read_text())["status"], "unchanged")
        self.assertTrue((self.output / "versions" / f"v{version:06d}.pt").is_file())
        self.assertFalse(list(self.output.rglob("*.pending")))

    def test_fixed_validation_groups_never_enter_replay_as_training_data_grows(self):
        samples = make_labels(self.dataset, episodes=3, frames=5)
        trainer = self.trainer(validation_split=.34)
        result = trainer.update(self.dataset)
        first_split = deepcopy(result["split"])
        self.assertTrue(first_split["validation"])
        self.assertTrue(first_split["training"])
        for cached in trainer._replay.values():
            self.assertIn(cached["group"], first_split["training"])
            self.assertNotIn(cached["group"], first_split["validation"])
        samples = self.append(samples, episode=first_split["training"][0], count=4)
        result = trainer.update(self.dataset)
        self.assertEqual(result["split"], first_split)
        self.assertIsNotNone(result["metrics"]["validation"])
        self.assertLessEqual(result["metrics"]["validation"]["samples"], trainer.config.replay_size)
        self.assertTrue(all(cached["group"] not in first_split["validation"] for cached in trainer._replay.values()))

    def test_auto_lane_labels_are_opt_in_and_checkpoint_keeps_unreviewed_provenance(self):
        samples = make_labels(self.dataset, episodes=1, frames=4, lane_only=True)
        for sample in samples:
            sample.update(lane_source="auto", lane_annotation={"method": "fixture-auto", "confidence": .9, "reviewed": False})
        write_samples(self.dataset, samples)
        trainer = self.trainer()
        self.assertEqual(trainer.update(self.dataset)["reason"], "no_labels")
        enabled = self.trainer(allow_auto_lanes=True)
        result = enabled.update(self.dataset)
        self.assertEqual(result["trained_tasks"], ["lane"])
        checkpoint = torch.load(result["checkpoint"], weights_only=True)
        self.assertEqual(checkpoint["data_source"], "real")
        self.assertTrue(checkpoint["auto_lane_labels"])
        self.assertEqual(checkpoint["lane_label_sources"], ["auto"])
        self.assertTrue(checkpoint["lane_supervised_y"])

    def test_synthetic_requires_explicit_permission_and_cannot_become_a_real_warm_start(self):
        generate_synthetic_dataset(self.dataset, episodes=1, frames_per_episode=4)
        self.assertEqual(self.trainer().update(self.dataset)["reason"], "no_labels")
        trainer = self.trainer(allow_synthetic=True)
        result = trainer.update(self.dataset)
        source = Path(result["checkpoint"])
        self.dataset = self.root / "real-data"
        make_labels(self.dataset, episodes=1, frames=4)
        self.output = self.root / "new-output"
        warm = self.trainer(warm_start=source)
        with self.assertRaises(ValueError):
            warm.update(self.dataset)

    def test_warm_start_keeps_progress_accepts_new_data_and_checks_model_and_preprocessing(self):
        make_labels(self.dataset, episodes=1, frames=4)
        trained = self.trainer(steps=2)
        original = trained.update(self.dataset)
        source = Path(original["checkpoint"])
        self.dataset = self.root / "new-data"
        make_labels(self.dataset, episodes=1, frames=4)
        self.output = self.root / "warm-output"
        warm = self.trainer(warm_start=source, steps=2)
        result = warm.update(self.dataset)
        self.assertEqual(result["version"], original["version"] + 1)
        self.assertEqual(result["optimizer_steps"], original["optimizer_steps"] + 2)
        self.assertEqual(warm.model_id, trained.model_id)
        self.output = self.root / "incompatible"
        wrong = self.trainer(warm_start=source, image_size=16)
        with self.assertRaises(ValueError):
            wrong.update(self.dataset)

    def test_stop_requests_do_not_train_and_prior_versions_cannot_be_overwritten_implicitly(self):
        make_labels(self.dataset, episodes=1, frames=4)
        trainer = self.trainer()
        trainer.request_stop()
        self.assertEqual(trainer.update(self.dataset)["status"], "stopped")
        self.assertIsNone(trainer.model)
        trainer = self.trainer()
        result = trainer.update(self.dataset)
        self.assertTrue(Path(result["checkpoint"]).is_file())
        with self.assertRaises(ValueError):
            self.trainer()
        with self.assertRaises(ValueError):
            OnlineTrainingConfig(replace(trainer.config.base, resume=Path(result["checkpoint"])))

    def test_deleted_labels_leave_replay_and_changed_history_retrains_affected_samples(self):
        samples = make_labels(self.dataset, episodes=1, frames=3)
        write_samples(self.dataset, [samples[0], samples[2]])
        trainer = self.trainer(history_frames=2, steps=3)
        trainer.update(self.dataset)
        identifier = samples[2]["id"]
        for _ in range(10):
            if identifier in trainer._seen:
                break
            trainer.update(self.dataset)
        old_signature = trainer._seen[identifier]
        write_samples(self.dataset, samples)
        for _ in range(10):
            result = trainer.update(self.dataset)
            if trainer._seen.get(identifier) != old_signature:
                break
        self.assertEqual(result["status"], "updated")
        self.assertNotEqual(trainer._seen[identifier], old_signature)
        write_samples(self.dataset, samples[:2])
        trainer.update(self.dataset)
        self.assertNotIn(identifier, trainer._replay)
        self.assertNotIn(identifier, trainer._seen)
        write_samples(self.dataset, [])
        self.assertEqual(trainer.update(self.dataset)["status"], "waiting")
        self.assertFalse(trainer._replay)
        self.assertFalse(trainer._seen)


if __name__ == "__main__":
    unittest.main()
