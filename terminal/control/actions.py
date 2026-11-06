"""动作协议校验：命令完整替换手柄状态，轴范围和毫秒持续时间集中定义。"""

from __future__ import annotations

import math
from typing import Any

BUTTONS = {
    "A": "A", "B": "B", "X": "X", "Y": "Y",
    "LB": "LEFT_SHOULDER", "RB": "RIGHT_SHOULDER",
    "BACK": "BACK", "START": "START", "GUIDE": "GUIDE",
    "LTHUMB": "LEFT_THUMB", "RTHUMB": "RIGHT_THUMB",
    "DPAD_UP": "DPAD_UP", "DPAD_DOWN": "DPAD_DOWN",
    "DPAD_LEFT": "DPAD_LEFT", "DPAD_RIGHT": "DPAD_RIGHT",
}


def normalize_action(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("action must be an object")
    unknown = set(value) - {"steering", "throttle", "brake", "buttons", "duration_ms"}
    if unknown:
        raise ValueError(f"unknown action fields: {', '.join(sorted(unknown))}")
    action: dict[str, Any] = {}
    for key, low, high in (("steering", -1, 1), ("throttle", 0, 1), ("brake", 0, 1)):
        number = value.get(key, 0)
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            raise ValueError(f"{key} must be a number")
        if not low <= number <= high or not math.isfinite(number):
            raise ValueError(f"{key} must be between {low} and {high}")
        action[key] = float(number)
    duration = value.get("duration_ms", 200)
    if isinstance(duration, bool) or not isinstance(duration, int) or not 0 <= duration <= 5000:
        raise ValueError("duration_ms must be an integer between 0 and 5000")
    buttons = value.get("buttons", [])
    if not isinstance(buttons, list) or len(buttons) > len(BUTTONS):
        raise ValueError("buttons must be a list of supported button names")
    if any(not isinstance(button, str) or button not in BUTTONS for button in buttons):
        raise ValueError("unsupported button name")
    action.update(buttons=list(dict.fromkeys(buttons)), duration_ms=duration)
    return action


def neutral_action() -> dict[str, Any]:
    return {"steering": 0.0, "throttle": 0.0, "brake": 0.0, "buttons": [], "duration_ms": 0}
