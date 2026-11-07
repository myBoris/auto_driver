"""真实 HTTP FakeCenter 验证遥测驱动环境，绝不向游戏终端发送动作。"""

import asyncio
import base64
from dataclasses import replace
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

from src.domain import Action
from src.environments.center_rl import CenterRLConfig, CenterRLEnvironment, EnvironmentSafetyError, load_reset_script
from src.integrations.center import CenterClient
from src.training.reward import TelemetryState, driving_reward
from tests.fake_center import FakeCenter, TOKEN


class FakeRLCenter(FakeCenter):
    """Importable local HTTP fixture for PPO integration; positions are fabricated only here."""

    def __init__(self):
        super().__init__()
        out = io.BytesIO()
        Image.new("RGB", (128, 96), (30, 60, 90)).save(out, format="JPEG")
        self.frame_body = out.getvalue()
        self.origin = self.captured_at
        self.session_id = "session-1"
        self.telemetry_sequence = 1
        self.telemetry_status = "ready"
        self.telemetry_age_ms = 0
        self.active = True
        self.speed = 5.
        self.forward_speed = 5.
        self.position = [0., 0., 0.]
        self.moving = True
        self.telemetry_capture_offset = 0.
        self.step_error = None
        self.swap_on_step = False
        self.swap_on_post = False
        self.executed_controls = []
        self.teleport_on_step = False
        self.freeze_telemetry = False
        self.game_time_offset = 0

    def _record(self, request, body=None):
        self.requests.append({"method": request.method, "path": request.path,
            "query": dict(request.query), "body": body})

    def observation(self, sequence=None):
        return {**super().observation(sequence),
                "jpeg_base64": base64.b64encode(self.frame_body).decode("ascii")}

    def _advance(self, dt, move=False):
        self.captured_at += dt
        self.received_at = self.captured_at + .01
        if move and self.moving:
            self.position[2] += self.forward_speed * dt

    async def handle(self, request):
        endpoint = request.path.rsplit("/", 1)[-1]
        if request.method == "POST" and endpoint in {"step", "actions", "release"}:
            if self.swap_on_post:
                self.session_id = "replacement-session"
            if request.query.get("session_id", self.session_id) != self.session_id:
                self._record(request, await request.json())
                return web.json_response({"error": "terminal session changed"}, status=409)
        if endpoint == "telemetry":
            self._record(request)
            after = request.query.get("after")
            if after is not None and not self.freeze_telemetry:
                self.telemetry_sequence = max(self.telemetry_sequence+1, int(after)+1)
            data = {"source": "fh5_data_out", "is_race_on": self.active,
                    "timestamp_ms": max(0, round((self.captured_at-self.origin)*1000) + 1000 + self.game_time_offset),
                    "speed_mps": self.speed, "position": self.position.copy(),
                    "velocity": [0., 0., self.forward_speed], "acceleration": [0., 0., 0.], "yaw": 0.}
            return web.json_response({"terminal_id": "game-pc", "session_id": self.session_id,
                "connected": True, "sequence": self.telemetry_sequence, "status": self.telemetry_status,
                "captured_at": self.captured_at + self.telemetry_capture_offset,
                "received_at": self.received_at, "age_ms": self.telemetry_age_ms, "ttl_ms": 1000,
                "data": data if self.telemetry_status == "ready" else None})
        if endpoint == "frame":
            self._record(request)
            self.sequence += 1
            self._advance(.01)
            return web.Response(body=self.frame_body, content_type="image/jpeg", headers={
                "X-Frame-Sequence": str(self.sequence), "X-Frame-Timestamp": str(self.captured_at),
                "X-Frame-Received-At": str(self.received_at)})
        if endpoint == "step":
            body = await request.json()
            self._record(request, body)
            self.step_started.set()
            if self.step_gate is not None:
                await self.step_gate.wait()
            if self.step_error:
                return web.json_response({"error": "step failed after possible execution"}, status=self.step_error)
            self.executed_controls.append("step")
            action = body["action"]
            applied = self.captured_at
            self._advance(action["duration_ms"]/1000 + .01, move=True)
            self.sequence += 1
            if self.swap_on_step:
                self.session_id = "replacement-session"
            if self.teleport_on_step:
                self.position[2] += 1000
            return web.json_response({"terminal_id": "game-pc", "command_id": f"c{self.sequence}",
                "ack": {"type": "ack", "command_id": f"c{self.sequence}", "ok": True,
                        "applied_at": applied, "state": action}, "observation": self.observation()})
        if endpoint in {"actions", "release"}:
            body = await request.json()
            self._record(request, body)
            self.executed_controls.append(endpoint)
            action = {"steering": 0, "throttle": 0, "brake": 0, "buttons": [], "duration_ms": 0, **body}
            applied = self.captured_at
            self._advance(action["duration_ms"]/1000 + .01)
            return web.json_response({"terminal_id": "game-pc", "command_id": "ui-button",
                "ack": {"type": "ack", "command_id": "ui-button", "ok": True,
                        "applied_at": applied, "state": action}})
        if endpoint == "game-pc":
            self._record(request)
            return web.json_response({"terminal_id": "game-pc", "session_id": self.session_id, "connected": True})
        return await super().handle(request)


class TelemetryRewardTests(unittest.TestCase):
    def state(self, t=1., position=(0., 0., 0.), speed=5., forward=5.):
        return TelemetryState(1, t, t+.01, round(t*1000), speed, position, (0., 0., forward), 0.)

    def test_real_forward_motion_is_rewarded_reverse_and_stationary_are_not(self):
        action = Action(throttle=1)
        previous = self.state()
        forward = driving_reward(previous, self.state(1.2, (0, 0, 1)), action)
        reverse = driving_reward(self.state(forward=-5), self.state(1.2, (0, 0, -1), forward=-5), action)
        parked = driving_reward(previous, self.state(1.2), action, lane_offset=0.)
        falling = driving_reward(previous, self.state(1.2, (0, -1, 0)), action, lane_offset=0.)
        self.assertGreater(forward.reward, 0)
        self.assertLess(reverse.reward, 0)
        self.assertLess(parked.reward, 0, "throttle/speed/CV without position movement cannot earn reward")
        self.assertLess(falling.reward, 0, "vertical movement is not forward progress")
        self.assertNotIn("collision", forward.components)
        self.assertNotIn("cv_lane_center", forward.components)

    def test_jump_rewind_clock_and_game_pause_are_discontinuities_without_progress(self):
        previous = self.state()
        for current in (self.state(1.1, (0, 0, 100)), self.state(.9), self.state(5.)):
            result = driving_reward(previous, current, Action())
            self.assertTrue(result.discontinuity)
            self.assertEqual(result.forward_progress_m, 0)
            self.assertLess(result.reward, 0)

    def test_real_acceleration_slip_yaw_and_steering_variation_have_penalties(self):
        previous = self.state(speed=20, forward=20)
        current = replace(self.state(1.2, (0, 0, 1), speed=3), yaw=.3, velocity=(2, 0, 3), acceleration=(12, 0, 0))
        result = driving_reward(previous, current, Action(steering=.8), Action(steering=-.8))
        self.assertGreater(result.impact_proxy_mps2, 0)
        self.assertEqual(result.lateral_acceleration, 12)
        for name in ("impact_proxy_penalty", "steering_change_penalty", "yaw_rate_penalty", "lateral_slip_penalty"):
            self.assertLess(result.components[name], 0)


class CenterRLEnvironmentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.center = await FakeRLCenter().start()
        self.addAsyncCleanup(self.center.close)
        self.client = await CenterClient(self.center.url, TOKEN).__aenter__()
        self.addAsyncCleanup(self.client.__aexit__)
        self.terminal = {"terminal_id": "game-pc", "session_id": "session-1", "connected": True}
        self.environments = []
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def env(self, **kwargs):
        env = CenterRLEnvironment(self.client, self.terminal, CenterRLConfig(use_lane_reward=False, **kwargs))
        self.environments.append(env)
        self.addAsyncCleanup(env.close)
        return env

    def posts(self, endpoint=None):
        return [r for r in self.center.requests if r["method"] == "POST"
                and (endpoint is None or r["path"].endswith("/"+endpoint))]

    async def test_first_reset_is_read_only_and_returns_real_images_and_fixed_telemetry(self):
        env = self.env()
        obs, info = await env.reset()
        self.assertEqual(self.posts(), [])
        self.assertEqual(obs["images"].shape, (4, 3, 84, 84))
        self.assertEqual(obs["telemetry"].shape, (6,))
        self.assertEqual(obs["images"].dtype, torch.float32)
        self.assertTrue(torch.isfinite(obs["telemetry"]).all())
        self.assertEqual(obs["images"][:3].count_nonzero(), 0)
        self.assertAlmostEqual(float(obs["telemetry"][3]), 5/80)
        self.assertTrue(info["telemetry_ready"])
        await env.close()
        self.assertEqual(self.posts(), [])

    async def test_missing_stale_or_inactive_telemetry_never_sends_any_input(self):
        for status, age, active in (("waiting", 0, True), ("stale", 2000, True), ("ready", 0, False)):
            with self.subTest(status=status, active=active):
                self.center.telemetry_status, self.center.telemetry_age_ms, self.center.active = status, age, active
                env = self.env()
                with self.assertRaises(EnvironmentSafetyError):
                    await env.reset()
                await env.close()
        self.assertEqual(self.posts(), [])

    async def test_step_rewards_telemetry_and_time_limit_releases_original_session(self):
        env = self.env(max_episode_steps=1)
        await env.reset()
        obs, reward, terminated, truncated, info = await env.step(Action(throttle=.4))
        self.assertGreater(reward, 0)
        self.assertFalse(terminated)
        self.assertTrue(truncated)
        self.assertEqual(info["truncation_reason"], "time_limit")
        self.assertTrue(info["bootstrap_allowed"])
        self.assertGreater(obs["images"][-2].count_nonzero(), 0)
        self.assertEqual(len(self.posts("step")), 1)
        self.assertEqual(len(self.posts("release")), 1)
        await env.close()
        self.assertEqual(len(self.posts("release")), 1)

    async def test_udp_loss_before_action_stops_without_a_driving_post(self):
        env = self.env()
        await env.reset()
        self.center.active = False
        with self.assertRaises(EnvironmentSafetyError):
            await env.step(Action(throttle=1))
        self.assertEqual(self.posts(), [])

    async def test_slow_preflight_or_preprocessing_cannot_renew_old_policy_pixels(self):
        for slow_phase in ("preflight", "preprocess"):
            with self.subTest(slow_phase=slow_phase):
                env = self.env(telemetry_max_age=.05)
                if slow_phase == "preprocess":
                    original = env._obs
                    def slow_obs(*args, **kwargs):
                        time.sleep(.06)
                        return original(*args, **kwargs)
                    with patch.object(env, "_obs", side_effect=slow_obs):
                        await env.reset()
                else:
                    await env.reset()
                original_get = self.client.get_telemetry
                async def slow_get(*args, **kwargs):
                    value = await original_get(*args, **kwargs)
                    await asyncio.sleep(.06)
                    return value
                if slow_phase == "preflight":
                    with patch.object(self.client, "get_telemetry", side_effect=slow_get):
                        with self.assertRaises(EnvironmentSafetyError):
                            await env.step(Action(throttle=1))
                else:
                    with self.assertRaises(EnvironmentSafetyError):
                        await env.step(Action(throttle=1))
        self.assertEqual(self.posts(), [])

    async def test_uncertain_step_is_never_replayed_and_releases_once(self):
        env = self.env()
        await env.reset()
        self.center.step_error = 504
        with self.assertRaises(EnvironmentSafetyError):
            await env.step(Action(throttle=.4))
        self.assertEqual(len(self.posts("step")), 1)
        self.assertEqual(len(self.posts("release")), 1)
        with self.assertRaises(EnvironmentSafetyError):
            await env.step(Action(throttle=.4))
        self.assertEqual(len(self.posts("step")), 1)

    async def test_replaced_terminal_is_never_released_or_reset(self):
        env = self.env()
        await env.reset()
        self.center.swap_on_step = True
        with self.assertRaises(EnvironmentSafetyError):
            await env.step(Action(throttle=.4))
        await env.close()
        self.assertEqual(len(self.posts("release")), 0)
        self.assertEqual(len(self.posts("actions")), 0)

    async def test_atomic_session_guard_rejects_reconnect_between_preflight_and_post(self):
        env = self.env()
        await env.reset()
        self.center.swap_on_post = True
        with self.assertRaises(EnvironmentSafetyError):
            await env.step(Action(throttle=.4))
        self.assertEqual(self.center.executed_controls, [])
        self.assertEqual(len(self.posts("step")), 1)
        self.assertEqual(self.posts("step")[0]["query"]["session_id"], "session-1")
        self.assertEqual(len(self.posts("release")), 0)

    async def test_cancelled_sampling_releases_original_session(self):
        env = self.env()
        await env.reset()
        self.center.step_gate = asyncio.Event()
        task = asyncio.create_task(env.step(Action(throttle=.4)))
        await self.center.step_started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(self.posts("release")), 1)

    async def test_time_alignment_failure_is_read_only_and_bounded(self):
        self.center.telemetry_capture_offset = -10
        with self.assertRaises(EnvironmentSafetyError):
            await self.env().reset()
        self.assertEqual(self.posts(), [])
        self.assertLess(len(self.center.requests), 20)

    async def test_pause_refresh_clears_baseline_without_resetting_episode_or_buttons(self):
        env = self.env()
        await env.reset()
        await env.step(Action(throttle=.4))
        await env.pause()
        self.center._advance(20, move=True)
        _, info = await env.refresh()
        self.assertEqual(info["episode_steps"], 1)
        self.assertEqual(len(self.posts("actions")), 0)
        _, reward, _, truncated, _ = await env.step(Action(throttle=.4))
        self.assertFalse(truncated)
        self.assertGreater(reward, 0)
        self.assertLess(reward, 1, "20 seconds of coast during update cannot count as policy progress")

    async def test_small_rollouts_refresh_cannot_erase_stuck_accumulation(self):
        env = self.env(stuck_seconds=.5)
        self.center.speed = self.center.forward_speed = 0
        self.center.moving = False
        await env.reset()
        for _ in range(5):
            _, _, _, truncated, info = await env.step(Action(throttle=1))
            if truncated:
                break
            await env.pause()
            await env.refresh()
        self.assertTrue(truncated)
        self.assertEqual(info["truncation_reason"], "stuck")

    async def test_position_teleport_cannot_farm_progress_and_disallows_bootstrap(self):
        env = self.env()
        await env.reset()
        self.center.teleport_on_step = True
        _, reward, _, truncated, info = await env.step(Action(throttle=1))
        self.assertTrue(truncated)
        self.assertFalse(info["bootstrap_allowed"])
        self.assertLess(reward, 0)
        self.assertEqual(info["forward_progress_m"], 0)

    async def test_stuck_reset_button_script_is_bounded_and_first_reset_has_no_buttons(self):
        script = Path(self.directory.name)/"reset.json"
        script.write_text(json.dumps({"version": 1, "steps": [
            {"buttons": ["Y"], "duration_ms": 50}, {"wait_seconds": 0},
            {"buttons": ["A"], "duration_ms": 50}]}), encoding="utf-8")
        env = self.env(stuck_seconds=.5, max_reset_attempts=1, reset_script=script)
        self.center.speed = self.center.forward_speed = 0.
        self.center.moving = False
        await env.reset()
        self.assertEqual(self.posts(), [])
        for _ in range(5):
            _, _, _, truncated, info = await env.step(Action(throttle=1))
        self.assertTrue(truncated)
        self.assertEqual(info["truncation_reason"], "stuck")
        _, info = await env.reset()
        self.assertEqual(info["reset_attempts"], 1)
        self.assertEqual([p["body"]["buttons"] for p in self.posts("actions")], [["Y"], ["A"]])
        for _ in range(5):
            _, _, _, truncated, _ = await env.step(Action(throttle=1))
        with self.assertRaisesRegex(EnvironmentSafetyError, "reset limit"):
            await env.reset()
        self.assertEqual(len(self.posts("actions")), 2)

    def test_reset_script_rejects_commands_axes_unknown_buttons_and_unbounded_waits(self):
        script = Path(self.directory.name)/"reset.json"
        for steps in ([{"wait_seconds": 60}], [{"buttons": ["DELETE"], "duration_ms": 100}],
                      [{"throttle": 1, "duration_ms": 100}], [{"exec": "anything"}]):
            script.write_text(json.dumps({"version": 1, "steps": steps}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_reset_script(script)


if __name__ == "__main__":
    unittest.main()
