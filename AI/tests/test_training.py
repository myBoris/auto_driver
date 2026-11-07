"""使用小型离线样本验证联合训练、任务掩码、片段划分及检查点恢复。"""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

import torch
from PIL import Image

from src.data.dataset import DrivingDataset, write_samples
from src.data.preprocessing import ObservationPreprocessor
from src.data.synthetic import generate_synthetic_dataset
from src.model import DrivingModel, DrivingModelConfig
from src.training.bc import (BehaviorCloningConfig, BehaviorCloningTrainer,
                             multitask_loss, split_episodes)


def tiny_config(dataset, output, **options):
    return BehaviorCloningConfig(**{
        "dataset_dir": Path(dataset), "output_dir": Path(output), "epochs": 1,
        "batch_size": 4, "learning_rate": .01, "device": "cpu", "image_size": 32,
        "patch_size": 8, "history_frames": 1, "embed_dim": 16, "num_heads": 2,
        "depth": 1, "mlp_dim": 32, "dropout": 0, "validation_split": .5,
        **options,
    })


def make_labels(root, *, episodes=2, frames=6, lane_only=False, sparse=False):
    """图片和人工标签均为测试夹具，不会访问任何 Center 或真实数据集。"""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    samples = []
    for episode in range(episodes):
        episode_id = f"test-episode-{episode}"
        for frame in range(frames):
            filename = f"{episode_id}-{frame}.jpg"
            Image.new("RGB", (48, 32), (110, 140, 80)).save(root / filename, "JPEG")
            sample = {
                "schema_version": 1, "id": filename, "episode_id": episode_id,
                "image": filename, "sequence": frame + 1, "source": "human",
                "session_id": f"session-{episode}", "terminal_id": "fixture",
                "lane_source": "human",
                "lane": {"visible": True, "left": [[.2, .95], [.4, .45]],
                         "right": [[.8, .95], [.6, .45]]},
            }
            if not lane_only and (not sparse or frame == 0):
                sample.update(action_source="human", action={
                    "steering": -.4, "throttle": .7, "brake": 0.,
                    "buttons": [], "duration_ms": 100,
                })
            if sparse and frame == 0:
                sample.pop("lane")
            samples.append(sample)
    write_samples(root, samples)
    return samples


class JointTrainingTests(unittest.TestCase):
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
        self.dataset = self.root / "dataset"
        self.output = self.root / "run"

    def fit(self, **options):
        with redirect_stdout(io.StringIO()):
            return BehaviorCloningTrainer(tiny_config(self.dataset, self.output, **options)).fit()

    def test_training_decreases_loss_splits_episodes_and_reloads_exact_weights(self):
        make_labels(self.dataset)
        result = self.fit(epochs=10)
        history = [json.loads(line) for line in (self.output / "history.jsonl").read_text().splitlines()]
        self.assertLess(history[-1]["training"]["loss"], history[0]["training"]["loss"])
        self.assertEqual(set(result["trained_tasks"]), {"lane", "control", "direction"})
        self.assertEqual(result["data_source"], "real")
        split = json.loads((self.output / "split.json").read_text())
        self.assertTrue(split["training"])
        self.assertTrue(split["validation"])
        self.assertFalse(set(split["training"]) & set(split["validation"]))
        checkpoint = torch.load(result["last_checkpoint"], weights_only=True)
        self.assertEqual(checkpoint["epoch"], 10)
        self.assertEqual(checkpoint["split"], split)
        model = DrivingModel(DrivingModelConfig.from_dict(checkpoint["model_config"]))
        model.load_state_dict(checkpoint["model_state"], strict=True)
        outputs = model.eval()(torch.rand(2, 1, 3, 32, 32))
        self.assertTrue(all(torch.isfinite(value).all() for value in outputs.values()))
        self.assertEqual(result["training_samples"], 6)
        self.assertEqual(result["validation_samples"], 6)

    def test_resume_keeps_model_identity_and_advances_epochs_without_overwriting_history(self):
        make_labels(self.dataset)
        original = self.fit(epochs=2)
        previous = torch.load(original["last_checkpoint"], weights_only=True)
        resumed = self.fit(epochs=2, resume=Path(original["last_checkpoint"]))
        current = torch.load(resumed["last_checkpoint"], weights_only=True)
        self.assertEqual(current["epoch"], 4)
        self.assertEqual(current["model_id"], previous["model_id"])
        self.assertEqual(current["split"], previous["split"])
        history = [json.loads(line)["epoch"] for line in (self.output / "history.jsonl").read_text().splitlines()]
        self.assertEqual(history, [1, 2, 3, 4])
        self.assertTrue(any(not torch.equal(value, current["model_state"][name])
                            for name, value in previous["model_state"].items()))
        with self.assertRaises(ValueError):
            self.fit(resume=Path(resumed["last_checkpoint"]), image_size=16)
        with self.assertRaises(ValueError):
            self.fit()

    def test_lane_only_training_does_not_claim_controls_or_report_fabricated_driving_metrics(self):
        make_labels(self.dataset, lane_only=True)
        result = self.fit(epochs=2)
        self.assertEqual(result["trained_tasks"], ["lane"])
        for metrics in (result["metrics"]["training"], result["metrics"]["validation"]):
            for name in ("steering_mae", "throttle_mae", "brake_mae", "direction_accuracy"):
                self.assertIsNone(metrics[name])
            self.assertEqual(metrics["control"], 0)
            self.assertEqual(metrics["buttons"], 0)
            self.assertEqual(metrics["direction"], 0)
            self.assertIsNotNone(metrics["lane_mae"])

    def test_resume_rejects_changed_samples_and_changed_episode_split(self):
        samples = make_labels(self.dataset, episodes=3)
        original = self.fit()
        resume = Path(original["last_checkpoint"])
        with self.assertRaises(ValueError):
            self.fit(resume=resume, validation_split=0)
        samples[0]["action"]["steering"] = .4
        write_samples(self.dataset, samples)
        with self.assertRaises(ValueError):
            self.fit(resume=resume)

    def test_resume_into_new_output_directory_always_produces_a_best_checkpoint(self):
        make_labels(self.dataset)
        original = self.fit()
        checkpoint = torch.load(original["last_checkpoint"], weights_only=True)
        # 原 run 的极低 best_score 不能令新目录永远不产生 best.pt。
        checkpoint["best_score"] = 0.
        source = self.root / "resume-source.pt"
        torch.save(checkpoint, source)
        self.output = self.root / "new-run"
        result = self.fit(resume=source)
        self.assertTrue(Path(result["checkpoint"]).is_file())
        self.assertTrue(Path(result["last_checkpoint"]).is_file())

    def test_empty_unlabeled_and_implicit_synthetic_data_are_rejected(self):
        self.dataset.mkdir()
        write_samples(self.dataset, [])
        with self.assertRaises(ValueError):
            self.fit()
        samples = make_labels(self.dataset)
        for sample in samples:
            sample.pop("action", None)
            sample.pop("lane", None)
        write_samples(self.dataset, samples)
        with self.assertRaises(ValueError):
            self.fit()
        synthetic = self.root / "synthetic"
        generate_synthetic_dataset(synthetic, episodes=2, frames_per_episode=3)
        self.dataset = synthetic
        with self.assertRaises(ValueError):
            self.fit()
        result = self.fit(allow_synthetic=True)
        self.assertEqual(result["data_source"], "synthetic")
        self.assertEqual(set(result["trained_tasks"]), {"lane", "control", "direction"})

    def test_only_negative_lane_labels_cannot_claim_a_trained_lane_geometry_head(self):
        samples = make_labels(self.dataset, lane_only=True)
        for sample in samples:
            sample["lane"] = {"visible": False}
        write_samples(self.dataset, samples)
        with self.assertRaises(ValueError):
            self.fit()
        # 有真实操作监督时仍可训练控制，但不能宣称获得车道边界预测能力。
        samples[0].update(action_source="human", action={
            "steering": 0., "throttle": .4, "brake": 0., "buttons": [], "duration_ms": 100,
        })
        samples[-1].update(action_source="human", action={
            "steering": 0., "throttle": .4, "brake": 0., "buttons": [], "duration_ms": 100,
        })
        write_samples(self.dataset, samples)
        result = self.fit()
        self.assertEqual(set(result["trained_tasks"]), {"control", "direction"})

    def test_missing_labels_are_excluded_from_each_loss_and_receive_no_gradient(self):
        config = tiny_config(self.dataset, self.output)
        outputs = {
            "controls": torch.tensor([[.1, .2, .3], [.3, .4, .5]], requires_grad=True),
            "button_logits": torch.zeros(2, 15, requires_grad=True),
            "direction_logits": torch.zeros(2, 3, requires_grad=True),
            "lane_points": torch.full((2, 6, 2), .5, requires_grad=True),
            "lane_presence_logit": torch.zeros(2, requires_grad=True),
        }
        batch = {
            "control_mask": torch.tensor([True, False]),
            "controls": torch.tensor([[0., 0., 0.], [float("nan")] * 3]),
            "buttons": torch.cat((torch.zeros(1, 15), torch.full((1, 15), float("nan")))),
            "direction": torch.tensor([0, -10000]),
            "lane_points": torch.cat((torch.full((1, 6, 2), float("nan")), torch.full((1, 6, 2), .4))),
            "lane_mask": torch.tensor([[False] * 6, [True] * 6]),
            "lane_presence": torch.tensor([float("nan"), 1.]),
            "lane_label_mask": torch.tensor([False, True]),
        }
        loss, components = multitask_loss(outputs, batch, config)
        self.assertTrue(torch.isfinite(loss))
        self.assertTrue(all(torch.isfinite(value) for value in components.values()))
        loss.backward()
        for name in ("controls", "button_logits", "direction_logits"):
            self.assertTrue(torch.equal(outputs[name].grad[1], torch.zeros_like(outputs[name].grad[1])))
        self.assertTrue(torch.equal(outputs["lane_points"].grad[0], torch.zeros_like(outputs["lane_points"].grad[0])))
        self.assertEqual(outputs["lane_presence_logit"].grad[0], 0)

    def test_validation_losses_match_full_labeled_population_with_sparse_tasks(self):
        make_labels(self.dataset, frames=7, sparse=True)
        result = self.fit(batch_size=3)
        checkpoint = torch.load(result["last_checkpoint"], weights_only=True)
        model = DrivingModel(DrivingModelConfig.from_dict(checkpoint["model_config"]))
        model.load_state_dict(checkpoint["model_state"])
        model.eval()
        dataset = DrivingDataset(self.dataset, preprocessor=ObservationPreprocessor(32))
        indices = [i for i, episode in enumerate(dataset.episode_ids) if episode in checkpoint["split"]["validation"]]
        batch = torch.utils.data.default_collate([dataset[i] for i in indices])
        with torch.inference_mode():
            expected_loss, expected_components = multitask_loss(model(batch["images"], batch["image_mask"]), batch,
                                                               tiny_config(self.dataset, self.output))
        actual = result["metrics"]["validation"]
        for name, expected in expected_components.items():
            self.assertAlmostEqual(actual[name], float(expected), places=5, msg=name)
        self.assertAlmostEqual(actual["loss"], float(expected_loss), places=5)

    def test_episode_split_never_leaks_neighboring_samples_or_fakes_a_single_episode_holdout(self):
        identities = ["a"] * 5 + ["b"] * 4 + ["c"] * 2
        training, validation = split_episodes(identities, .2, 7)
        self.assertEqual(set(training) | set(validation), set(range(len(identities))))
        self.assertFalse({identities[i] for i in training} & {identities[i] for i in validation})
        with self.assertRaises(ValueError):
            split_episodes(["a"] * 5, .2)
        self.assertEqual(split_episodes(["a"] * 5, 0), (list(range(5)), []))


if __name__ == "__main__":
    unittest.main()
