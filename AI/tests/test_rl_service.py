"""PPO 独立驾驶入口在完成、出错与取消时统一关闭环境。"""

import asyncio
import unittest

from src.domain import Action
from src.services.rl_driving import run_rl_policy


class EvaluationEnvironment:
    def __init__(self):
        self.resets = self.steps = self.closes = 0
        self.failure = None
        self.started = asyncio.Event()
        self.gate = None

    async def reset(self):
        self.resets += 1
        return {"sample": self.steps}, {"status": "ready"}

    async def step(self, action):
        self.steps += 1
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.failure:
            raise self.failure
        return {"sample": self.steps}, .5, False, self.steps % 2 == 0, {}

    async def close(self):
        self.closes += 1


class EvaluationPolicy:
    def __init__(self):
        self.observations = []

    def predict(self, observation, deterministic=True):
        if deterministic is not True:
            raise AssertionError("evaluation must be deterministic")
        self.observations.append(observation)
        return Action(steering=.1, throttle=.2)


class RLServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_finite_deterministic_driving_resets_episodes_and_closes(self):
        environment, policy = EvaluationEnvironment(), EvaluationPolicy()
        result = await run_rl_policy(environment, policy, steps=5)
        self.assertEqual(result, {"steps": 5, "episodes": 2, "total_reward": 2.5})
        self.assertEqual(environment.resets, 3)
        self.assertEqual(environment.closes, 1)
        self.assertEqual(len(policy.observations), 5)

    async def test_environment_errors_preserve_exception_and_close(self):
        environment = EvaluationEnvironment()
        environment.failure = RuntimeError("telemetry became stale")
        with self.assertRaisesRegex(RuntimeError, "telemetry became stale"):
            await run_rl_policy(environment, EvaluationPolicy())
        self.assertEqual(environment.steps, 1)
        self.assertEqual(environment.closes, 1)

    async def test_cancelled_inflight_action_closes_environment_before_returning(self):
        environment = EvaluationEnvironment()
        environment.gate = asyncio.Event()
        task = asyncio.create_task(run_rl_policy(environment, EvaluationPolicy()))
        await environment.started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(environment.closes, 1)
