"""离线假环境验证 PPO 数学、参数更新、恢复与释放；绝不向真实游戏发控制。"""

import asyncio
from dataclasses import replace
import json
import math
from pathlib import Path
import tempfile
import threading
import time
import unittest

import torch

from src.model.rl import ActorCriticConfig, RLActorCritic, RLPolicy, latent_to_action, observation_tensors
from src.training.rl import ReinforcementLearningConfig, ReinforcementLearningTrainer, compute_gae


class FakeEnvironment:
    def __init__(self, *, episode_steps=1000, fault_at=None, missing_telemetry=False,
                 no_bootstrap=False, fail_reset=False):
        self.episode_steps = episode_steps
        self.fault_at = fault_at
        self.missing_telemetry = missing_telemetry
        self.no_bootstrap = no_bootstrap
        self.fail_reset = fail_reset
        self.actions = []
        self.events = []
        self.episode_step = 0
        self.paused = False
        self.closed = False
        self.marker = .1

    def observation(self):
        result = {"images": torch.full((1, 3, 64, 64), self.marker),
                  "telemetry": torch.tensor([self.marker, 0., 0., .2, 0., 0.])}
        if self.missing_telemetry:
            result.pop("telemetry")
        return result

    async def reset(self):
        self.events.append("reset")
        if self.fail_reset:
            raise RuntimeError("real telemetry unavailable")
        self.episode_step = 0
        return self.observation(), {}

    async def step(self, action):
        self.events.append("step")
        self.actions.append(action)
        if self.fault_at is not None and len(self.actions) == self.fault_at:
            raise RuntimeError("stale telemetry")
        self.paused = False
        self.episode_step += 1
        reward = 1. - abs(action.steering - .25) - abs(action.throttle - .15)
        truncated = self.episode_step >= self.episode_steps
        return self.observation(), reward, False, truncated, {
            "needs_manual_reset": False, "bootstrap_allowed": not self.no_bootstrap}

    async def pause(self):
        self.events.append("pause")
        self.paused = True

    async def refresh(self):
        self.events.append("refresh")
        self.marker = .8
        return self.observation(), {}

    async def close(self):
        self.events.append("close")
        self.closed = True
        self.paused = True


class ObservedTrainer(ReinforcementLearningTrainer):
    def __init__(self, config, env):
        super().__init__(config)
        self.env = env
        self.observed_markers = []
        self.rollouts = []

    def _initialize(self):
        super()._initialize()
        self.initial_actor = self.model.actor.weight.detach().clone()
        self.initial_critic = self.model.critic.weight.detach().clone()

    def _sample(self, observation):
        self.observed_markers.append(float(observation["telemetry"][0]))
        return super()._sample(observation)

    def _update(self, rollout):
        assert self.env.paused, "inputs must be released before optimizer work"
        self.env.events.append("update")
        self.rollouts.append(rollout)
        return super()._update(rollout)


class RLMathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.threads = torch.get_num_threads()
        torch.set_num_threads(1)

    @classmethod
    def tearDownClass(cls):
        torch.set_num_threads(cls.threads)

    def test_gae_bootstraps_truncation_and_cuts_both_episode_boundaries(self):
        advantages, returns = compute_gae([1, 2, 3, 4], [.5, .6, .7, .8], [.6, 10, .8, 20],
                                         [False, False, True, False], [False, True, False, False], .9, .8)
        self.assertTrue(torch.allclose(advantages, torch.tensor([8.528, 10.4, 2.3, 21.2]), atol=1e-5))
        self.assertTrue(torch.allclose(returns, advantages + torch.tensor([.5, .6, .7, .8])))

    def test_squashed_policy_evaluation_matches_sampling_probability(self):
        model = RLActorCritic(ActorCriticConfig(image_size=64, history_frames=1))
        images, telemetry = torch.rand(3, 3, 64, 64), torch.zeros(3, 6)
        latent, log_prob, value = model.sample(images, telemetry)
        evaluated, entropy, predicted_value = model.evaluate(images, telemetry, latent)
        self.assertTrue(torch.allclose(log_prob, evaluated))
        self.assertTrue(torch.allclose(value, predicted_value))
        self.assertTrue(torch.isfinite(entropy).all())
        # 即使 tanh 近饱和，稳定 Jacobian 也不会 log(0)。
        log_extreme = model.evaluate(images, telemetry, torch.full((3, 2), 100.))[0]
        self.assertTrue(torch.isfinite(log_extreme).all())

    def test_action_limits_and_exclusive_pedals(self):
        for latent in ([100., 100.], [-100., -100.], [0., 0.], [.1, -.2]):
            action = latent_to_action(latent)
            self.assertLessEqual(abs(action.steering), .7)
            self.assertLessEqual(action.throttle, .35)
            self.assertLessEqual(action.brake, .7)
            self.assertEqual(action.throttle * action.brake, 0.)
        with self.assertRaises(ValueError):
            latent_to_action([float("nan"), 0.])
        with self.assertRaises(ValueError):
            latent_to_action([1., 1.], max_throttle=-1)

    def test_missing_or_invalid_observation_never_gets_fake_zero_telemetry(self):
        config = ActorCriticConfig(image_size=64, history_frames=1)
        with self.assertRaises(ValueError):
            observation_tensors({"images": torch.zeros(1, 3, 64, 64)}, config)
        with self.assertRaises(ValueError):
            observation_tensors({"images": torch.ones(1, 3, 64, 64) * 255,
                                 "telemetry": torch.zeros(6)}, config)
        with self.assertRaises(ValueError):
            observation_tensors({"images": torch.zeros(1, 3, 64, 64),
                                 "telemetry": torch.tensor([0., 0., 0., 0., 0., float("nan")])}, config)

    def test_invalid_training_configuration_is_rejected(self):
        for options in ({"max_throttle": 1.1}, {"total_steps": True}, {"gamma": 0.},
                        {"target_kl": float("nan")}, {"image_size": 32}, {"action_duration_ms": 5001}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                ReinforcementLearningConfig(Path("unused"), **options)


class RLTrainingTests(unittest.IsolatedAsyncioTestCase):
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
        self.output = Path(self.temporary.name) / "ppo"
        self.config = ReinforcementLearningConfig(self.output, total_steps=8, device="cpu",
            rollout_steps=4, batch_size=2, update_epochs=2, image_size=64, history_frames=1,
            checkpoint_interval=4)

    async def test_ppo_changes_weights_saves_and_reloads_policy_and_refreshes_after_update(self):
        env = FakeEnvironment()
        trainer = ObservedTrainer(self.config, env)
        result = await trainer.train(env)
        self.assertEqual((result["steps"], result["updates"], result["status"]), (8, 2, "complete"))
        self.assertFalse(torch.equal(trainer.initial_actor, trainer.model.actor.weight))
        self.assertFalse(torch.equal(trainer.initial_critic, trainer.model.critic.weight))
        self.assertTrue(env.closed)
        self.assertEqual(env.events.count("pause"), 2)
        self.assertEqual(env.events.count("refresh"), 1)
        self.assertAlmostEqual(trainer.observed_markers[3], .1)
        self.assertAlmostEqual(trainer.observed_markers[4], .8)
        self.assertEqual(env.events[env.events.index("update") - 1], "pause")
        for action in env.actions:
            self.assertLessEqual(abs(action.steering), self.config.max_steering)
            self.assertEqual(action.throttle * action.brake, 0)
        status = json.loads((self.output / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["status"], "complete")
        self.assertEqual(status["metrics"]["score_source"], "training_rollout_not_independent_evaluation")
        for name in ("policy_loss", "value_loss", "approx_kl", "entropy", "grad_norm"):
            self.assertTrue(math.isfinite(status["metrics"][name]))
        self.assertTrue((self.output / "best.pt").is_file())
        self.assertEqual(len(list(self.output.glob("checkpoint-*.pt"))), 2)
        self.assertEqual(len((self.output / "metrics.jsonl").read_text().splitlines()), 2)
        self.assertFalse(list(self.output.glob("*.tmp")))
        payload = torch.load(self.output / "latest.pt", weights_only=True)
        self.assertTrue(payload["optimizer_state"]["state"])
        policy = RLPolicy(self.output / "latest.pt", device="cpu")
        self.assertEqual(policy.model_config.image_size, 64)
        self.assertEqual(policy.action_config["max_throttle"], .35)
        self.assertEqual(policy.predict(env.observation()), policy.predict(env.observation()))

    async def test_resume_preserves_optimizer_and_uses_cumulative_step_target(self):
        first = ReinforcementLearningTrainer(replace(self.config, total_steps=4))
        await first.train(FakeEnvironment())
        checkpoint = self.output / "latest.pt"
        before = torch.load(checkpoint, weights_only=True)
        second = ReinforcementLearningTrainer(replace(self.config, resume=checkpoint))
        env = FakeEnvironment()
        result = await second.train(env)
        self.assertEqual(len(env.actions), 4)
        self.assertEqual((result["steps"], result["updates"]), (8, 2))
        self.assertEqual(result["model_id"], first.model_id)
        after = torch.load(checkpoint, weights_only=True)
        first_key = next(iter(before["optimizer_state"]["state"]))
        self.assertGreater(after["optimizer_state"]["state"][first_key]["step"],
                           before["optimizer_state"]["state"][first_key]["step"])

    async def test_resume_rejects_different_action_bounds_without_overwriting_checkpoint(self):
        await ReinforcementLearningTrainer(self.config).train(FakeEnvironment())
        checkpoint = self.output / "latest.pt"
        original = checkpoint.read_bytes()
        env = FakeEnvironment()
        trainer = ReinforcementLearningTrainer(replace(self.config, resume=checkpoint, max_throttle=.8))
        with self.assertRaises(ValueError):
            await trainer.train(env)
        self.assertTrue(env.closed)
        self.assertFalse(env.actions)
        self.assertEqual(checkpoint.read_bytes(), original)

    async def test_checkpoint_action_configuration_is_validated_before_prediction(self):
        await ReinforcementLearningTrainer(replace(self.config, total_steps=4)).train(FakeEnvironment())
        payload = torch.load(self.output / "latest.pt", weights_only=True)
        for key, invalid in (("max_throttle", 5.), ("max_brake", -.5), ("max_steering", float("nan")),
                             ("max_throttle", True), ("action_duration_ms", 10), ("action_duration_ms", True)):
            with self.subTest(key=key, invalid=invalid):
                modified = {**payload, "action_config": {**payload["action_config"], key: invalid}}
                target = self.output / "tampered.pt"
                torch.save(modified, target)
                with self.assertRaises(ValueError):
                    RLPolicy(target, "cpu")

    async def test_target_kl_stops_additional_optimizer_steps(self):
        env = FakeEnvironment()
        trainer = ObservedTrainer(replace(self.config, total_steps=4, target_kl=1e-10,
                                          update_epochs=10, learning_rate=.01), env)
        await trainer.train(env)
        self.assertTrue(trainer.last_metrics["early_stop_kl"])
        self.assertLess(trainer.last_metrics["minibatches"], 20)
        self.assertGreaterEqual(trainer.last_metrics["minibatches"], 1)

    async def test_normal_time_limit_bootstraps_final_observation_before_reset(self):
        env = FakeEnvironment(episode_steps=2)
        trainer = ObservedTrainer(replace(self.config, total_steps=4), env)
        await trainer.train(env)
        for transition in (trainer.rollouts[0][1], trainer.rollouts[0][3]):
            self.assertTrue(transition["truncated"])
            self.assertNotEqual(transition["next_value"], 0.)

    async def test_missing_telemetry_prevents_first_action_and_checkpoint(self):
        env = FakeEnvironment(missing_telemetry=True)
        with self.assertRaises(ValueError):
            await ReinforcementLearningTrainer(self.config).train(env)
        self.assertTrue(env.closed)
        self.assertFalse(env.actions)
        self.assertFalse((self.output / "latest.pt").exists())

    async def test_reset_preflight_failure_closes_without_an_untrained_checkpoint(self):
        env = FakeEnvironment(fail_reset=True)
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            await ReinforcementLearningTrainer(self.config).train(env)
        self.assertTrue(env.closed)
        self.assertFalse(env.actions)
        self.assertFalse((self.output / "latest.pt").exists())

    async def test_safety_fault_closes_and_keeps_last_valid_update(self):
        env = FakeEnvironment(fault_at=6)
        trainer = ReinforcementLearningTrainer(self.config)
        with self.assertRaisesRegex(RuntimeError, "stale telemetry"):
            await trainer.train(env)
        self.assertTrue(env.closed)
        payload = torch.load(self.output / "latest.pt", weights_only=True)
        self.assertEqual(payload["steps"], 5)
        self.assertEqual(payload["updates"], 1)
        self.assertEqual(payload["status"], "error")

    async def test_discontinuity_does_not_bootstrap_and_boundaries_reset_environment(self):
        env = FakeEnvironment(episode_steps=2, no_bootstrap=True)
        trainer = ObservedTrainer(replace(self.config, total_steps=4), env)
        await trainer.train(env)
        self.assertEqual(trainer.episodes, 2)
        self.assertEqual(env.events.count("reset"), 2)
        self.assertEqual(trainer.rollouts[0][1]["next_value"], 0.)
        self.assertEqual(trainer.rollouts[0][3]["next_value"], 0.)

    async def test_cancellation_releases_inputs_and_saves_completed_update(self):
        blocked = asyncio.Event()

        class BlockingEnvironment(FakeEnvironment):
            async def step(inner, action):
                if len(inner.actions) == 4:
                    blocked.set()
                    await asyncio.Event().wait()
                return await super().step(action)

        env = BlockingEnvironment()
        trainer = ReinforcementLearningTrainer(self.config)
        task = asyncio.create_task(trainer.train(env))
        await asyncio.wait_for(blocked.wait(), timeout=15)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(env.closed)
        payload = torch.load(self.output / "latest.pt", weights_only=True)
        self.assertEqual((payload["steps"], payload["updates"], payload["status"]), (4, 1, "interrupted"))

    async def test_cancellation_waits_for_bounded_worker_before_atomic_save(self):
        started = threading.Event()

        class BlockingUpdate(ReinforcementLearningTrainer):
            def _update(inner, rollout):
                started.set()
                while not inner.stop_event.is_set():
                    time.sleep(.005)
                inner.worker_finished = True
                return super()._update(rollout)

            def _save(inner, *args):
                if inner.steps:
                    assert inner.worker_finished
                return super()._save(*args)

        env = FakeEnvironment()
        trainer = BlockingUpdate(self.config)
        task = asyncio.create_task(trainer.train(env))
        for _ in range(300):
            if started.is_set():
                break
            await asyncio.sleep(.01)
        self.assertTrue(started.is_set())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(env.closed)
        self.assertTrue(trainer.worker_finished)
        self.assertFalse(list(self.output.glob("*.tmp")))
        # 只有采样、尚未优化的应急权重可恢复训练，但不能直接当成训练好的策略。
        with self.assertRaises(ValueError):
            RLPolicy(self.output / "latest.pt", "cpu")

    async def test_http_center_environment_ppo_checkpoint_integration(self):
        from src.environments.center_rl import CenterRLConfig, CenterRLEnvironment
        from src.integrations.center import CenterClient
        from tests.fake_center import TOKEN
        from tests.test_center_rl import FakeRLCenter

        center = await FakeRLCenter().start()
        try:
            async with CenterClient(center.url, TOKEN) as client:
                env = CenterRLEnvironment(client,
                    {"terminal_id": "game-pc", "session_id": "session-1", "connected": True},
                    CenterRLConfig(image_size=64, history_frames=1, use_lane_reward=False))
                trainer = ReinforcementLearningTrainer(replace(self.config, total_steps=4, rollout_steps=2))
                result = await trainer.train(env)
            self.assertEqual((result["steps"], result["updates"]), (4, 2))
            self.assertEqual(center.executed_controls.count("step"), 4)
            self.assertGreaterEqual(center.executed_controls.count("release"), 2)
            self.assertEqual(center.executed_controls[-1], "release")
            payload = torch.load(self.output / "latest.pt", weights_only=True)
            self.assertEqual(payload["status"], "complete")
            self.assertTrue(math.isfinite(payload["metrics"]["reward_mean"]))
        finally:
            await center.close()


if __name__ == "__main__":
    unittest.main()
