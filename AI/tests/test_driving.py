"""用小型真实网络和本地 HTTP 中心验证训练权重推理、显示及驾驶生命周期。"""

import asyncio
import base64
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from aiohttp import web
from PIL import Image
import torch

from src.data.preprocessing import ObservationPreprocessor
from src.domain import Observation
from src.integrations.center import CenterClient
from src.model import DrivingModel, DrivingModelConfig, TransformerConfig
from src.policies.driving import DrivingPolicy
from src.services.driving import run_driving
from tests.fake_center import FakeCenter, TOKEN


def image_bytes():
    output = io.BytesIO()
    Image.new("RGB", (80, 48), (35, 70, 110)).save(output, format="JPEG")
    return output.getvalue()


def observation(sequence=1):
    return Observation(image_bytes(), sequence, 1000 + sequence, 2000 + sequence)


def save_checkpoint(path, *, history=1, **overrides):
    config = DrivingModelConfig(encoder=TransformerConfig(
        image_size=16, patch_size=8, embed_dim=16, num_heads=4,
        depth=1, mlp_dim=32, dropout=0, max_images=history,
    ))
    model = DrivingModel(config)
    with torch.no_grad():
        for parameter in model.parameters():
            parameter.zero_()
        model.control_head.bias.copy_(torch.tensor([torch.atanh(torch.tensor(-.4)),
                                                   torch.logit(torch.tensor(.6)),
                                                   torch.logit(torch.tensor(.2))]))
        model.button_head.bias.fill_(-8)
        for name in ("LB", "B"):
            model.button_head.bias[config.button_names.index(name)] = 8
        model.lane_presence_head.bias.fill_(torch.logit(torch.tensor(.9)))
        model.direction_head.bias.copy_(torch.tensor([2., 0., 1.]))
    payload = {"schema_version": 1, "model_config": config.to_dict(),
               "preprocessing": ObservationPreprocessor(16, history).to_dict(),
               "model_state": model.state_dict(), "trained_tasks": ["lane", "control", "direction"],
               "data_source": "real", "model_id": "test-driving-model", "epoch": 1,
               "metrics": {"loss": .2}, **overrides}
    torch.save(payload, path)
    return payload


class InferenceCenter(FakeCenter):
    def __init__(self):
        super().__init__()
        self.frame_body = image_bytes()
        self.auto_frame_sequence = True

    def observation(self, sequence=None):
        return {**super().observation(sequence),
                "jpeg_base64": base64.b64encode(self.frame_body).decode("ascii")}

    async def handle(self, request):
        if not request.path.endswith("/perception"):
            return await super().handle(request)
        body = await request.json()
        self.requests.append({"method": request.method, "path": request.path,
                              "raw_path": request.raw_path, "query": dict(request.query),
                              "authorization": request.headers.get("Authorization"), "body": body})
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return web.json_response({"error": "invalid credential"}, status=401)
        status = self.response_status.get("perception", 200)
        if status != 200:
            return web.json_response({"error": "perception unavailable"}, status=status)
        return web.json_response({**body, "terminal_id": "game-pc", "age_ms": 0,
                                  "received_at": time.time(), "ttl_ms": 2000,
                                  "frame": {"sequence": body["frame_sequence"],
                                            "captured_at": body["captured_at"]}})


class DrivingPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.threads = torch.get_num_threads()
        torch.set_num_threads(1)

    @classmethod
    def tearDownClass(cls):
        torch.set_num_threads(cls.threads)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "trained.pt"
        save_checkpoint(self.path)

    def test_weights_determine_geometry_controls_probabilities_and_original_image_coordinates(self):
        policy = DrivingPolicy(self.path, device="cpu", max_throttle=.5)
        result = policy.predict_result(observation())
        self.assertAlmostEqual(result.action.steering, -.4, places=5)
        self.assertEqual(result.action.throttle, .5)
        self.assertAlmostEqual(result.action.brake, .2, places=5)
        self.assertEqual(set(result.action.buttons), {"LB", "B"})
        self.assertEqual(result.action.duration_ms, 100)
        self.assertEqual((result.image_width, result.image_height), (80, 48))
        self.assertGreater(result.inference_ms, 0)
        self.assertEqual(len(result.road["polygon"]), 12)
        self.assertAlmostEqual(result.road["confidence"], .9, places=5)
        expected_probability = float(torch.softmax(torch.tensor([2., 0., 1.]), dim=0)[0])
        self.assertAlmostEqual(result.prediction["confidence"], expected_probability, places=5)
        payload = result.to_perception(observation(), session_id="session", model_id=policy.model_id)
        self.assertEqual(payload["prediction"]["source"], "model")
        self.assertEqual(payload["frame_sequence"], 1)
        self.assertEqual(payload["image"], {"width": 80, "height": 48})
        # 完整图像的归一化车道坐标可以直接用于 Center 的叠加。
        self.assertTrue(all(0 <= x <= 1 and 0 <= y <= 1 for x, y in payload["road"]["polygon"]))

    def test_confidence_uses_steering_category_instead_of_unrelated_maximum(self):
        payload = torch.load(self.path, weights_only=True)
        payload["model_state"]["direction_head.bias"] = torch.tensor([0., 1., 3.])
        torch.save(payload, self.path)
        result = DrivingPolicy(self.path, "cpu").predict_result(observation())
        self.assertLess(result.prediction["confidence"], .1, "right probability cannot be reported for left steering")

    def test_partially_supervised_road_never_draws_untrained_anchors_over_the_dashboard(self):
        save_checkpoint(self.path, lane_supervised_y=[.45, .55])
        result = DrivingPolicy(self.path, "cpu").predict_result(observation())
        self.assertEqual([point[1] for point in result.road["centerline"]], [.55, .45])
        self.assertEqual(len(result.road["polygon"]), 4)
        self.assertEqual({point[1] for point in result.road["polygon"]}, {.45, .55})

    def test_unknown_duplicate_or_insufficient_supervised_lane_anchors_are_rejected(self):
        for anchors in ([.5, .55], [.55], [.55, .55], [True, .55], "all"):
            with self.subTest(anchors=anchors):
                save_checkpoint(self.path, lane_supervised_y=anchors)
                with self.assertRaisesRegex(ValueError, "监督锚点"):
                    DrivingPolicy(self.path, "cpu")

    def test_untrained_tasks_are_not_fabricated_and_lane_threshold_can_hide_geometry(self):
        save_checkpoint(self.path, trained_tasks=["lane"])
        policy = DrivingPolicy(self.path, "cpu", lane_threshold=.95)
        result = policy.predict_result(observation())
        self.assertIsNone(result.action)
        self.assertIsNone(result.prediction)
        self.assertIsNone(result.road)
        self.assertEqual(result.to_perception(observation(), session_id="s", model_id="m")["status"], "no_road")
        with self.assertRaisesRegex(ValueError, "操作标签"):
            policy.predict(observation(2))

    def test_synthetic_checkpoint_can_preview_but_never_operate_real_terminal(self):
        save_checkpoint(self.path, data_source="synthetic")
        policy = DrivingPolicy(self.path, "cpu")
        self.assertIsNotNone(policy.predict_result(observation()).road)
        self.assertEqual(policy.source, "demo")
        with self.assertRaisesRegex(ValueError, "合成"):
            policy.require_executable()

    def test_synthetic_lane_only_result_carries_demo_model_identity_without_a_fake_direction(self):
        save_checkpoint(self.path, data_source="synthetic", trained_tasks=["lane"])
        policy = DrivingPolicy(self.path, "cpu")
        result = policy.predict_result(observation())
        payload = result.to_perception(observation(), session_id="s", model_id=policy.model_id, source=policy.source)
        self.assertIsNone(payload["prediction"])
        self.assertIsNotNone(payload["road"])
        self.assertEqual(payload["model"]["id"], "demo-test-driving-model")

    def test_history_padding_is_masked_and_regressing_frames_cannot_reenter_sequence(self):
        save_checkpoint(self.path, history=3)
        policy = DrivingPolicy(self.path, "cpu")
        images, mask = policy.preprocess(observation(1))
        self.assertEqual(tuple(images.shape), (1, 3, 3, 16, 16))
        self.assertEqual(mask.tolist(), [[False, False, True]])
        self.assertEqual(images[0, :2].count_nonzero().item(), 0)
        _, mask = policy.preprocess(observation(2))
        self.assertEqual(mask.tolist(), [[False, True, True]])
        with self.assertRaisesRegex(ValueError, "回退"):
            policy.preprocess(observation(1))
        policy.reset_state()
        self.assertEqual(policy.preprocess(observation(1))[1].tolist(), [[False, False, True]])

    def test_incomplete_unknown_and_incompatible_checkpoint_metadata_is_rejected(self):
        for overrides in ({"schema_version": 2}, {"trained_tasks": []},
                          {"trained_tasks": ["direction"]}, {"data_source": "unknown"},
                          {"epoch": 0}, {"preprocessing": {"image_size": 32, "history_frames": 1}}):
            with self.subTest(overrides=overrides):
                save_checkpoint(self.path, **overrides)
                with self.assertRaises((ValueError, RuntimeError)):
                    DrivingPolicy(self.path, "cpu")

    def test_nonfinite_network_outputs_never_become_a_driving_action(self):
        payload = torch.load(self.path, weights_only=True)
        payload["model_state"]["control_head.bias"].fill_(float("nan"))
        torch.save(payload, self.path)
        policy = DrivingPolicy(self.path, "cpu")
        with self.assertRaisesRegex(RuntimeError, "非有限"):
            policy.predict(observation())

    def test_missing_checkpoint_weights_fail_with_actionable_validation(self):
        payload = torch.load(self.path, weights_only=True)
        del payload["model_state"]
        torch.save(payload, self.path)
        with self.assertRaisesRegex(ValueError, "缺少模型权重"):
            DrivingPolicy(self.path, "cpu")


class DrivingServiceTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.threads = torch.get_num_threads()
        torch.set_num_threads(1)

    @classmethod
    def tearDownClass(cls):
        torch.set_num_threads(cls.threads)

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "trained.pt"
        save_checkpoint(self.path)
        self.policy = DrivingPolicy(self.path, "cpu", max_inference_seconds=10)
        self.server = await InferenceCenter().start()
        self.client = await CenterClient(self.server.url, TOKEN).__aenter__()
        self.terminal = {"terminal_id": "game-pc", "connected": True, "session_id": "session-1", "frame_sequence": 1}

    async def asyncTearDown(self):
        await self.client.__aexit__()
        await self.server.close()

    async def run_driving(self, **options):
        with redirect_stdout(io.StringIO()):
            return await run_driving(self.client, self.terminal, self.policy, **options)

    def posted(self, endpoint):
        return [request for request in self.server.requests
                if request["method"] == "POST" and request["path"].endswith("/" + endpoint)]

    async def test_preview_only_publishes_predictions_with_fresh_cursors_and_preserves_human_control(self):
        count = await self.run_driving(frames=3, output_dir=self.root / "predictions")
        self.assertEqual(count, 3)
        self.assertEqual(len(self.posted("perception")), 3)
        self.assertEqual(self.posted("actions") + self.posted("step") + self.posted("release"), [])
        requests = [request for request in self.server.requests if request["path"].endswith("/frame")]
        self.assertEqual([request["query"]["after"] for request in requests], ["1", "2", "3"])
        records = list((self.root / "predictions").glob("episode-*/predictions.jsonl"))
        self.assertEqual(len(records), 1)
        values = [json.loads(line) for line in records[0].read_text(encoding="utf-8").splitlines()]
        self.assertEqual([value["sequence"] for value in values], [2, 3, 4])
        self.assertTrue(all(not value["execute"] for value in values))

    async def test_execute_reuses_step_observation_records_causality_and_releases_on_completion(self):
        count = await self.run_driving(frames=2, execute=True, output_dir=self.root / "drive")
        self.assertEqual(count, 2)
        self.assertEqual(len(self.posted("step")), 2)
        self.assertEqual(len(self.posted("release")), 1)
        self.assertEqual(len([r for r in self.server.requests if r["path"].endswith("/frame")]), 1)
        self.assertAlmostEqual(self.posted("step")[0]["body"]["action"]["steering"], -.4, places=5)
        records = next((self.root / "drive").glob("episode-*/records.jsonl"))
        values = [json.loads(line) for line in records.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["observation"]["sequence"] for row in values], [2, 3])
        self.assertEqual([row["next_observation"]["sequence"] for row in values], [3, 4])

    async def test_action_failure_is_not_replayed_and_one_release_is_attempted(self):
        self.server.response_status["step"] = 502
        with self.assertRaisesRegex(RuntimeError, "502"):
            await self.run_driving(frames=3, execute=True)
        self.assertEqual(len(self.posted("step")), 1)
        self.assertEqual(len(self.posted("release")), 1)

    async def test_preview_failure_does_not_release_external_driving(self):
        self.server.response_status["perception"] = 503
        with self.assertRaisesRegex(RuntimeError, "503"):
            await self.run_driving(frames=2)
        self.assertEqual(self.posted("step") + self.posted("release"), [])

    async def test_synthetic_preview_is_marked_demo_and_execution_is_rejected_before_any_request(self):
        save_checkpoint(self.path, data_source="synthetic")
        self.policy = DrivingPolicy(self.path, "cpu")
        await self.run_driving(frames=1)
        self.assertEqual(self.posted("perception")[0]["body"]["prediction"]["source"], "demo")
        count = len(self.server.requests)
        with self.assertRaisesRegex(ValueError, "合成"):
            await self.run_driving(frames=1, execute=True)
        self.assertEqual(len(self.server.requests), count)

    async def test_slow_inference_never_sends_stale_actions(self):
        original = self.policy.predict_result
        self.policy.max_inference_seconds = .01

        def slow_predict(value):
            time.sleep(.04)
            return original(value)

        with patch.object(self.policy, "predict_result", side_effect=slow_predict):
            with self.assertRaisesRegex(RuntimeError, "耗时过长"):
                await self.run_driving(frames=1, execute=True)
        self.assertEqual(self.posted("perception") + self.posted("step") + self.posted("release"), [])

    async def test_session_replacement_stops_predictions_and_does_not_release_new_session(self):
        def reconnect(server):
            server.response_overrides["game-pc"] = {**self.terminal, "session_id": "new-session"}

        self.server.after_step = reconnect
        with self.assertRaisesRegex(RuntimeError, "连接已改变"):
            await self.run_driving(frames=2, execute=True)
        self.assertEqual(len(self.posted("step")), 1)
        self.assertEqual(self.posted("release"), [])

    async def test_cancelled_step_releases_original_session_without_retrying_action(self):
        self.server.step_gate = asyncio.Event()
        with redirect_stdout(io.StringIO()):
            task = asyncio.create_task(run_driving(self.client, self.terminal, self.policy, frames=2, execute=True))
            await asyncio.wait_for(self.server.step_started.wait(), 5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(len(self.posted("step")), 1)
        self.assertEqual(len(self.posted("release")), 1)


if __name__ == "__main__":
    unittest.main()
