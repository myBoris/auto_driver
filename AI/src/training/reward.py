"""FH5 自由驾驶的遥测奖励；进度/冲击均为代理量，不冒充碰撞或离路真值。"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from src.domain import Action
from src.domain.types import finite_number


def _vector(value: Any, name: str) -> tuple[float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f"{name} requires three finite coordinates")
    return tuple(finite_number(x, name, -1e9, 1e9) for x in value)


@dataclass(frozen=True)
class TelemetryState:
    """position 是世界坐标；velocity 是本地坐标，Z 正方向为车头向前。"""

    sequence: int
    captured_at: float
    received_at: float
    timestamp_ms: int
    speed_mps: float
    position: tuple[float, float, float]
    velocity: tuple[float, float, float]
    yaw: float
    acceleration: tuple[float, float, float] | None = None

    @classmethod
    def from_payload(cls, payload: dict) -> "TelemetryState":
        data = payload.get("data")
        if not isinstance(data, dict) or data.get("is_race_on") is not True:
            raise ValueError("FH5 telemetry must be active (is_race_on=true)")
        sequence, stamp = payload.get("sequence"), data.get("timestamp_ms")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            raise ValueError("telemetry sequence must be positive")
        if isinstance(stamp, bool) or not isinstance(stamp, int) or not 0 <= stamp <= 2**32 - 1:
            raise ValueError("timestamp_ms must be the FH5 uint32 game clock")
        return cls(sequence, finite_number(payload.get("captured_at"), "captured_at", 1e-6, 1e12),
                   finite_number(payload.get("received_at"), "received_at", 1e-6, 1e12), stamp,
                   finite_number(data.get("speed_mps"), "speed_mps", 0, 500),
                   _vector(data.get("position"), "position"), _vector(data.get("velocity"), "velocity"),
                   finite_number(data.get("yaw"), "yaw", -100, 100),
                   _vector(data["acceleration"], "acceleration") if data.get("acceleration") is not None else None)


@dataclass(frozen=True)
class RewardResult:
    reward: float
    components: dict[str, float]
    dt: float
    forward_progress_m: float
    yaw_rate: float
    lateral_acceleration: float
    impact_proxy_mps2: float
    discontinuity: bool = False


def driving_reward(previous: TelemetryState, current: TelemetryState, action: Action,
                   previous_action: Action | None = None, *, lane_offset: float | None = None) -> RewardResult:
    """只奖励真实位置改变且本地 Z 速度向前的进度，ACK/油门不会产生正奖励。

    自由驾驶没有既定路线终点；向前位移不能证明车辆在公路上。可选 lane_offset
    来自当前图像 CV 候选；没有候选时完全不加此项。
    """
    dt = current.captured_at - previous.captured_at
    game_delta = (current.timestamp_ms - previous.timestamp_ms) % 2**32 / 1000
    displacement = math.dist(previous.position, current.position)
    distance = math.hypot(current.position[0] - previous.position[0],
                          current.position[2] - previous.position[2])
    max_speed = max(previous.speed_mps, current.speed_mps,
                    math.sqrt(sum(x*x for x in previous.velocity)), math.sqrt(sum(x*x for x in current.velocity)))
    discontinuity = (not 0 < dt <= 3 or not 0 < game_delta <= 3 or abs(game_delta - dt) > .75
                     or displacement > max(5., max_speed * max(dt, 0) * 2 + 2))
    if discontinuity:
        return RewardResult(-1., {"discontinuity_penalty": -1.}, max(dt, 0), 0., 0., 0., 0., True)
    yaw_delta = math.atan2(math.sin(current.yaw-previous.yaw), math.cos(current.yaw-previous.yaw))
    yaw_rate = yaw_delta / dt
    lateral = (current.acceleration[0] if current.acceleration is not None
               else (current.velocity[0] - previous.velocity[0]) / dt)
    forward = (previous.velocity[2] + current.velocity[2]) / 2
    ratio = max(-1., min(1., forward / max(1., max_speed)))
    progress = min(distance, max_speed * dt * 1.25) * ratio
    impact = max(0., (previous.speed_mps - current.speed_mps) / dt - 8.)
    movement = min(1., distance / max(.05, max_speed * dt))
    parts = {"forward_progress": .1 * progress,
             "forward_speed": .01 * forward * dt * movement,
             "lateral_slip_penalty": -.03 * abs(current.velocity[0]) / max(current.speed_mps, 1.) * dt,
             "yaw_rate_penalty": -.02 * abs(yaw_rate) * dt,
             "steering_change_penalty": -.02 * abs(action.steering - (previous_action.steering if previous_action else 0.)),
             "impact_proxy_penalty": -.005 * min(impact, 100.) * dt,
             "time_penalty": -.01 * dt}
    if lane_offset is not None:
        offset = finite_number(lane_offset, "lane_offset", -1e6, 1e6)
        parts["cv_lane_center"] = .03 * (1 - min(2., abs(offset))) * dt * movement
    return RewardResult(sum(parts.values()), parts, dt, progress, yaw_rate, lateral, impact)


class RewardFunction:
    """Pure calculation adapter; never reads or writes game state."""

    def __call__(self, previous: TelemetryState, current: TelemetryState, action: Action,
                 previous_action: Action | None = None, **kwargs) -> RewardResult:
        return driving_reward(previous, current, action, previous_action, **kwargs)
