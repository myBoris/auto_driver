"""实际 HTTP 观察/遥测→PPO 更新→检查点驾驶；只使用本地模拟游戏。"""

from pathlib import Path
import tempfile
import unittest

import torch

from src.environments.center_rl import CenterRLConfig, CenterRLEnvironment, EnvironmentSafetyError
from src.integrations.center import CenterClient
from src.model.rl import RLPolicy
from src.services.rl_driving import run_rl_policy
from src.training.rl import ReinforcementLearningConfig, ReinforcementLearningTrainer
from tests.test_center_rl import FakeRLCenter, TOKEN


class RLIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.previous_threads = torch.get_num_threads()
        torch.set_num_threads(1)
        self.addCleanup(torch.set_num_threads, self.previous_threads)
        self.center = await FakeRLCenter().start()
        self.addAsyncCleanup(self.center.close)
        self.client = await CenterClient(self.center.url, TOKEN).__aenter__()
        self.addAsyncCleanup(self.client.__aexit__)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name) / "ppo"
        self.terminal = {"terminal_id": "game-pc", "session_id": "session-1", "connected": True}

    def environment(self):
        return CenterRLEnvironment(self.client, self.terminal, CenterRLConfig(
            image_size=64, history_frames=1, max_episode_steps=3,
            reset_mode="none", use_lane_reward=False))

    def config(self):
        return ReinforcementLearningConfig(self.output, total_steps=8, rollout_steps=4,
            batch_size=4, update_epochs=1, device="cpu", image_size=64, history_frames=1)

    async def test_http_training_saves_updated_model_and_reload_can_drive_without_updating(self):
        result = await ReinforcementLearningTrainer(self.config()).train(self.environment())
        self.assertEqual(result["steps"], 8)
        self.assertEqual(result["updates"], 2)
        self.assertTrue((self.output / "best.pt").is_file())
        checkpoint = self.output / "latest.pt"
        original = checkpoint.read_bytes()
        policy = RLPolicy(checkpoint, device="cpu")
        driving = await run_rl_policy(self.environment(), policy, steps=3)
        self.assertEqual(driving["steps"], 3)
        self.assertEqual(checkpoint.read_bytes(), original)
        posts = [request for request in self.center.requests if request["method"] == "POST"]
        self.assertEqual(len([r for r in posts if r["path"].endswith("/step")]), 11)
        self.assertTrue(all(r["query"].get("session_id") == "session-1" for r in posts))

    async def test_preflight_without_telemetry_never_sends_input_or_saves_random_policy(self):
        self.center.telemetry_status = "waiting"
        with self.assertRaises(EnvironmentSafetyError):
            await ReinforcementLearningTrainer(self.config()).train(self.environment())
        self.assertFalse(any(r["method"] == "POST" for r in self.center.requests))
        self.assertFalse((self.output / "latest.pt").exists())
