"""有真实 FH5 遥测的有限回合环境；只在有效原会话中执行短时输入。"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
import io
import json
import logging
from pathlib import Path
import time

from src.data.auto_lanes import AutoLaneConfig, AutoLaneLabeler
from src.domain import Action, Observation
from src.domain.types import finite_number
from src.services.sessions import ensure_session
from src.training.reward import TelemetryState, driving_reward

log = logging.getLogger(__name__)


class EnvironmentSafetyError(RuntimeError):
    """遥测/画面/会话不可靠时停止，不得继续采样或盲目重试。"""


DEFAULT_RESET_STEPS = (
    {"buttons": ["Y"], "duration_ms": 200}, {"wait_seconds": 2.},
    {"buttons": ["A"], "duration_ms": 200}, {"wait_seconds": .5},
)


def load_reset_script(path: Path | None = None) -> tuple[dict, ...]:
    """显式按钮和等待脚本，不执行代码，不重放失败的 POST。"""
    raw = ({"version": 1, "steps": DEFAULT_RESET_STEPS} if path is None
           else json.loads(Path(path).read_text(encoding="utf-8")))
    if not isinstance(raw, dict) or set(raw) != {"version", "steps"} or raw["version"] != 1:
        raise ValueError("reset script requires version=1 and steps")
    steps = raw["steps"]
    if not isinstance(steps, (list, tuple)) or not 1 <= len(steps) <= 32:
        raise ValueError("reset script requires 1-32 steps")
    result, total = [], 0.
    for item in steps:
        if not isinstance(item, dict):
            raise ValueError("reset step must be an object")
        if set(item) == {"wait_seconds"}:
            seconds = finite_number(item["wait_seconds"], "wait_seconds", 0, 10)
            result.append({"wait_seconds": seconds})
            total += seconds
        elif set(item) == {"buttons", "duration_ms"} and isinstance(item["buttons"], list):
            action = Action(buttons=tuple(item["buttons"]), duration_ms=item["duration_ms"])
            if not action.buttons or not 50 <= action.duration_ms <= 1000:
                raise ValueError("reset buttons must be nonempty and held for 50-1000 ms")
            result.append({"buttons": list(action.buttons), "duration_ms": action.duration_ms})
            total += action.duration_ms / 1000
        else:
            raise ValueError("reset step supports only wait_seconds or buttons+duration_ms")
    if total > 30:
        raise ValueError("reset script must complete within 30 seconds")
    return tuple(result)


@dataclass(frozen=True)
class CenterRLConfig:
    image_size: int = 84
    history_frames: int = 4
    observation_timeout: float = 5.
    telemetry_timeout: float = 5.
    telemetry_max_age: float = 1.
    max_episode_steps: int = 500
    stuck_seconds: float = 5.
    reset_mode: str = "rewind"
    reset_script: Path | None = None
    max_reset_attempts: int = 3
    use_lane_reward: bool = True
    lane_config: AutoLaneConfig = field(default_factory=AutoLaneConfig)
    alignment_tolerance: float = .35
    max_action_ms: int = 1000

    def __post_init__(self):
        for name, low, high in (("image_size", 32, 512), ("history_frames", 1, 16),
                                ("max_episode_steps", 1, 100000), ("max_reset_attempts", 1, 10),
                                ("max_action_ms", 50, 1000)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"{name} must be an integer in {low}..{high}")
        for name, low, high in (("observation_timeout", .1, 30), ("telemetry_timeout", .1, 20),
                                ("telemetry_max_age", .05, 2), ("stuck_seconds", .5, 60),
                                ("alignment_tolerance", .02, 1)):
            finite_number(getattr(self, name), name, low, high)
        if self.reset_mode not in {"rewind", "none"}:
            raise ValueError("reset_mode must be rewind or none")
        if not isinstance(self.use_lane_reward, bool) or not isinstance(self.lane_config, AutoLaneConfig):
            raise ValueError("use_lane_reward/lane_config are invalid")


class CenterRLEnvironment:
    """异步 reset/step 接口，不依赖 Gym，不在构造时执行网络调用。

    images float32[history,3,H,W] (0..1)；telemetry float32[6] 分别为
    speed/80, local vx/80,vy/80,vz/80,yaw_rate/3,lateral_accel/20。
    """

    telemetry_dim = 6

    def __init__(self, client, terminal: dict, config: CenterRLConfig | None = None):
        if (not isinstance(terminal, dict) or terminal.get("connected") is not True
                or not terminal.get("terminal_id") or not terminal.get("session_id")):
            raise ValueError("RL requires an online terminal with a fixed session_id")
        self.client, self.terminal = client, dict(terminal)
        self.config = config or CenterRLConfig()
        self.terminal_id = terminal["terminal_id"]
        self._reset_steps = load_reset_script(self.config.reset_script)
        self._labeler = AutoLaneLabeler(self.config.lane_config) if self.config.use_lane_reward else None
        self._images = deque(maxlen=self.config.history_frames)
        self._telemetry: TelemetryState | None = None
        self._frame: Observation | None = None
        self._owned_action = False
        self._closed = self._failed = self._started = False
        self._episode_done = True
        self._episode_steps = 0
        self._stuck_elapsed = 0.
        self._recovery_attempts = 0
        self._recovery_progress = 0.
        self._previous_action: Action | None = None
        self._snapshot_time = 0.
        self._aligned_at = 0.
        self._paused = False
        self._telemetry_age_ms = None
        self._telemetry_read_at = 0.
        self._last_reward_terms = {}
        self._episode = 0

    async def _session(self):
        await asyncio.wait_for(ensure_session(self.client, self.terminal),
                               self.config.observation_timeout + 2)

    def _available(self):
        if self._closed or self._failed:
            raise EnvironmentSafetyError("environment is closed or stopped; inspect state before creating another")

    async def _get_telemetry(self, after: int | None = None) -> TelemetryState:
        requested_at = time.monotonic()
        payload = await asyncio.wait_for(self.client.get_telemetry(
            self.terminal_id, session_id=self.terminal["session_id"], after=after,
            timeout=self.config.telemetry_timeout), self.config.telemetry_timeout + 2)
        if (payload.get("terminal_id") != self.terminal_id
                or payload.get("session_id") != self.terminal["session_id"]
                or payload.get("connected") is not True or payload.get("status") != "ready"):
            raise EnvironmentSafetyError("fresh FH5 telemetry unavailable; driving input prohibited")
        self._telemetry_age_ms = finite_number(payload.get("age_ms"), "telemetry age_ms", 0,
                                                self.config.telemetry_max_age * 1000)
        # Conservatively include the GET round trip; never renew a server age
        # when a delayed HTTP response eventually arrives at the AI machine.
        self._telemetry_read_at = requested_at
        state = TelemetryState.from_payload(payload)
        if after is not None and state.sequence <= after:
            raise EnvironmentSafetyError("telemetry sequence did not advance")
        return state

    async def _observe(self, after: int | None = None) -> Observation:
        return await asyncio.wait_for(self.client.observe(self.terminal_id, after=after,
            timeout=self.config.observation_timeout), self.config.observation_timeout + 2)

    async def _pair(self, *, frame: Observation | None = None, fresh_after: int | None = None,
                    not_before: float | None = None) -> tuple[Observation, TelemetryState]:
        await self._session()
        state = await self._get_telemetry(after=fresh_after)
        if frame is None:
            frame = await self._observe(after=self._frame.sequence if self._frame else None)
        for _ in range(8):
            # Compare clocks only within their originating machine. Center's age
            # uses its monotonic clock; AI wall-clock skew does not affect it.
            delta = frame.captured_at - state.captured_at
            received_delta = frame.received_at - state.received_at
            if (self._telemetry_age_ms / 1000 + time.monotonic() - self._telemetry_read_at
                    > self.config.telemetry_max_age):
                state = await self._get_telemetry(after=state.sequence)
                continue
            if ((not_before is None or min(frame.captured_at, state.captured_at) >= not_before)
                    and abs(delta) <= self.config.alignment_tolerance
                    and abs(received_delta) <= self.config.alignment_tolerance + .5):
                if self._frame and frame.sequence <= self._frame.sequence:
                    raise EnvironmentSafetyError("observation sequence did not advance")
                await self._session()
                if (self._telemetry_age_ms / 1000 + time.monotonic() - self._telemetry_read_at
                        > self.config.telemetry_max_age):
                    state = await self._get_telemetry(after=state.sequence)
                    continue
                # Keep a separate baseline clock. A later preflight GET may
                # refresh its own age fields but must not renew policy pixels.
                self._aligned_at = (self._telemetry_read_at - self._telemetry_age_ms / 1000
                                    - max(0., state.captured_at - frame.captured_at))
                if time.monotonic() - self._aligned_at > self.config.telemetry_max_age:
                    frame = await self._observe(after=frame.sequence)
                    continue
                return frame, state
            if frame.captured_at < state.captured_at or (not_before is not None and frame.captured_at < not_before):
                frame = await self._observe(after=frame.sequence)
            else:
                state = await self._get_telemetry(after=state.sequence)
        raise EnvironmentSafetyError("frame and FH5 telemetry could not be time aligned")

    def _obs(self, frame: Observation, state: TelemetryState, *, yaw_rate=0., lateral_acceleration=None):
        import numpy as np
        from PIL import Image
        import torch
        with Image.open(io.BytesIO(frame.jpeg)) as image:
            array = np.asarray(image.convert("RGB").resize((self.config.image_size, self.config.image_size)),
                               dtype=np.float32).copy()
        self._images.append(torch.from_numpy(array).permute(2, 0, 1) / 255.)
        images = torch.zeros((self.config.history_frames, 3, self.config.image_size, self.config.image_size))
        images[-len(self._images):] = torch.stack(tuple(self._images))
        lateral = ((state.acceleration[0] if state.acceleration else 0.)
                   if lateral_acceleration is None else lateral_acceleration)
        telemetry = torch.tensor([state.speed_mps/80, *(value/80 for value in state.velocity),
                                  yaw_rate/3, lateral/20], dtype=torch.float32).clamp(-1, 1)
        self._frame, self._telemetry = frame, state
        self._snapshot_time = self._aligned_at
        return {"images": images, "telemetry": telemetry}

    def _info(self, **extra):
        return {"telemetry_ready": True, "session_id": self.terminal["session_id"],
                "terminal_id": self.terminal_id, "frame_sequence": self._frame.sequence if self._frame else None,
                "telemetry_sequence": self._telemetry.sequence if self._telemetry else None,
                "episode": self._episode, "episode_steps": self._episode_steps,
                "stuck_seconds": self._stuck_elapsed, "reset_attempts": self._recovery_attempts,
                "telemetry_age_ms": self._telemetry_age_ms, "reward_terms": self._last_reward_terms,
                "needs_manual_reset": False, "reward_source": "fh5_free_drive_telemetry_proxy", **extra}

    async def _fail(self, exc: BaseException):
        self._failed = True
        await self._release_owned()
        if isinstance(exc, (asyncio.CancelledError, EnvironmentSafetyError)):
            raise exc
        raise EnvironmentSafetyError(f"RL stopped: {exc}") from exc

    async def _release_owned(self):
        if not self._owned_action:
            return
        self._owned_action = False
        try:
            await asyncio.wait_for(self._session(), 3)
            await asyncio.wait_for(self.client.release(self.terminal_id,
                session_id=self.terminal["session_id"]), 3)
        except Exception:
            log.warning("RL original-session release failed or session changed; input expires by watchdog")

    async def _run_reset(self):
        last_completed = None
        for item in self._reset_steps:
            await self._session()
            if "wait_seconds" in item:
                await asyncio.sleep(item["wait_seconds"])
                continue
            action = Action(buttons=tuple(item["buttons"]), duration_ms=item["duration_ms"])
            self._owned_action = True
            ack = await asyncio.wait_for(self.client.action(self.terminal_id, action,
                session_id=self.terminal["session_id"]), 5)
            last_completed = ack["ack"]["applied_at"] + action.duration_ms / 1000
            await asyncio.sleep(action.duration_ms / 1000)
            await self._release_owned()
        return last_completed

    async def reset(self):
        self._available()
        try:
            # Initial reset is strictly read only. Later resets require active,
            # fresh data before the explicitly configured rewind UI sequence.
            frame, state = await self._pair(fresh_after=self._telemetry.sequence if self._telemetry else None)
            if self._started:
                await self._release_owned()
                if self.config.reset_mode == "rewind":
                    if self._recovery_attempts >= self.config.max_reset_attempts:
                        raise EnvironmentSafetyError("FH5 reset limit reached without verified movement; inspect rewind/menu settings")
                    if self._recovery_attempts == 0:
                        self._recovery_progress = 0.
                    self._recovery_attempts += 1
                    reset_completed = await self._run_reset()
                    frame, state = await self._pair(fresh_after=state.sequence,
                        not_before=max(frame.captured_at + .001, reset_completed or 0))
            self._images.clear()
            self._episode_steps, self._stuck_elapsed = 0, 0.
            self._previous_action = None
            self._started, self._episode_done, self._paused = True, False, False
            self._episode += 1
            obs = self._obs(frame, state)
            return obs, self._info(reset_mode="initial" if self._recovery_attempts == 0 else self.config.reset_mode)
        except BaseException as exc:
            await self._fail(exc)

    async def step(self, action: Action):
        self._available()
        if not self._started or self._episode_done or self._paused:
            raise EnvironmentSafetyError("call reset/refresh before driving this environment")
        if not isinstance(action, Action) or action.buttons or not 50 <= action.duration_ms <= self.config.max_action_ms:
            raise ValueError("RL actions require steering/pedals only and a bounded 50..max_action_ms hold")
        try:
            await self._session()
            if time.monotonic() - self._snapshot_time > self.config.telemetry_max_age:
                raise EnvironmentSafetyError("policy observation is stale; pause and refresh before another action")
            latest = await self._get_telemetry()
            await self._session()
            previous = self._telemetry
            if (time.monotonic() - self._snapshot_time > self.config.telemetry_max_age
                    or latest.captured_at < previous.captured_at or latest.sequence < previous.sequence):
                raise EnvironmentSafetyError("observation expired or telemetry clock regressed during preflight")
            self._owned_action = True
            result = await asyncio.wait_for(self.client.step(self.terminal_id, action,
                observation_timeout=self.config.observation_timeout, session_id=self.terminal["session_id"]),
                self.config.observation_timeout + action.duration_ms/1000 + 3)
            minimum = result.ack["applied_at"] + action.duration_ms / 1000
            frame, state = await self._pair(frame=result.observation, fresh_after=previous.sequence, not_before=minimum)
            lane_offset, lane_candidate = None, None
            if self._labeler:
                lane_candidate = await asyncio.to_thread(self._labeler.annotate, frame.jpeg)
                if lane_candidate:
                    lane = lane_candidate["lane"]
                    left, right = max(lane["left"], key=lambda p:p[1]), max(lane["right"], key=lambda p:p[1])
                    lane_offset = (.5-(left[0]+right[0])/2) / max((right[0]-left[0])/2, .01)
            reward = driving_reward(previous, state, action, self._previous_action, lane_offset=lane_offset)
            self._last_reward_terms = reward.components
            self._previous_action = action
            self._episode_steps += 1
            if self._recovery_attempts:
                self._recovery_progress += max(0, reward.forward_progress_m)
                if self._recovery_progress >= 2:
                    self._recovery_attempts, self._recovery_progress = 0, 0.
            self._stuck_elapsed = (self._stuck_elapsed + reward.dt
                if abs(reward.forward_progress_m) < .05 and state.speed_mps < 1 else 0.)
            reason = ("telemetry_discontinuity" if reward.discontinuity else
                      "stuck" if self._stuck_elapsed >= self.config.stuck_seconds else
                      "time_limit" if self._episode_steps >= self.config.max_episode_steps else None)
            self._episode_done = reason is not None
            obs = self._obs(frame, state, yaw_rate=reward.yaw_rate, lateral_acceleration=reward.lateral_acceleration)
            info = self._info(reward_components=reward.components, forward_progress_m=reward.forward_progress_m,
                impact_proxy_mps2=reward.impact_proxy_mps2, cv_lane_detected=lane_candidate is not None,
                cv_lane_offset=lane_offset, truncation_reason=reason,
                bootstrap_allowed=not reward.discontinuity)
            if self._episode_done:
                await self._release_owned()
            return obs, reward.reward, False, self._episode_done, info
        except BaseException as exc:
            await self._fail(exc)

    async def pause(self):
        """PPO 更新期间释放输入，不打开菜单或重置游戏。"""
        self._available()
        self._paused = True
        await self._release_owned()

    async def refresh(self):
        """更新后重取当前状态；漂移时间不归因给下一次策略动作。"""
        self._available()
        if not self._started or self._episode_done:
            raise EnvironmentSafetyError("refresh requires an active episode")
        try:
            frame, state = await self._pair(fresh_after=self._telemetry.sequence)
            self._images.clear()
            self._previous_action = None
            self._paused = False
            obs = self._obs(frame, state)
            return obs, self._info(refreshed=True)
        except BaseException as exc:
            await self._fail(exc)

    async def close(self):
        if not self._closed:
            self._closed = True
            await self._release_owned()
