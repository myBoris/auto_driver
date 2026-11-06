"""训练与中心交互使用的不可变数据。

完整动作状态使调用者明确每一步保持哪些输入，省略轴归零、按钮释放。
观察的 captured_at 来自终端采集，received_at 来自中心接收，二者不能互换。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any

BUTTONS = frozenset({
    "A", "B", "X", "Y", "LB", "RB", "BACK", "START", "GUIDE", "LTHUMB", "RTHUMB",
    "DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT",
})


def finite_number(value: Any, name: str, low: float, high: float) -> float:
    """先比较范围再转浮点，巨大整数也只产生清楚的参数错误，不溢出。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    if not low <= value <= high or not math.isfinite(value):
        raise ValueError(f"{name} must be finite and between {low} and {high}")
    return float(value)


def timestamp(value: Any, name: str) -> float:
    return finite_number(value, name, 0.000001, 1000000000000)


@dataclass(frozen=True)
class Action:
    """一次完整手柄动作；轴采用归一化值，duration_ms 采用毫秒，0 表示释放。"""

    steering: float = 0.0
    throttle: float = 0.0
    brake: float = 0.0
    buttons: tuple[str, ...] = ()
    duration_ms: int = 100

    def __post_init__(self) -> None:
        for name, low, high in (("steering", -1, 1), ("throttle", 0, 1), ("brake", 0, 1)):
            object.__setattr__(self, name, finite_number(getattr(self, name), name, low, high))
        if not isinstance(self.buttons, tuple) or any(not isinstance(item, str) or item not in BUTTONS for item in self.buttons):
            raise ValueError("buttons must be a tuple of supported Xbox 360 button names")
        if len(self.buttons) > len(BUTTONS):
            raise ValueError("too many buttons")
        object.__setattr__(self, "buttons", tuple(dict.fromkeys(self.buttons)))
        if isinstance(self.duration_ms, bool) or not isinstance(self.duration_ms, int) or not 0 <= self.duration_ms <= 5000:
            raise ValueError("duration_ms must be an integer between 0 and 5000")

    def to_payload(self) -> dict[str, Any]:
        """每次返回独立字典和列表，调用者修改 HTTP 载荷不会改变领域对象。"""
        return {"steering": self.steering, "throttle": self.throttle, "brake": self.brake,
                "buttons": list(self.buttons), "duration_ms": self.duration_ms}


@dataclass(frozen=True)
class Observation:
    """中心返回的压缩观察；保留序号及两个时钟，不在传输层执行图像预处理。"""

    jpeg: bytes = field(repr=False)
    sequence: int
    captured_at: float
    received_at: float

    def __post_init__(self) -> None:
        if not isinstance(self.jpeg, bytes) or len(self.jpeg) < 4 or not self.jpeg.startswith(b"\xff\xd8") or not self.jpeg.endswith(b"\xff\xd9"):
            raise ValueError("observation must contain JPEG bytes")
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 1:
            raise ValueError("observation sequence must be a positive integer")
        object.__setattr__(self, "captured_at", timestamp(self.captured_at, "captured_at"))
        object.__setattr__(self, "received_at", timestamp(self.received_at, "received_at"))


@dataclass(frozen=True)
class StepResult:
    """一次执行后的新观察及原始确认详情；奖励、终止条件由训练代码自行定义。"""

    observation: Observation
    command_id: str
    ack: dict[str, Any]
