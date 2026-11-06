"""网络消息约束，避免把无效或过期的输入发送给游戏。"""

import math
import re
import struct

SERVICE = "auto-driver-center"
PROTOCOL = 1
FRAME_HEADER = struct.Struct("!4sd")
BUTTONS = frozenset({
    "A", "B", "X", "Y", "LB", "RB", "BACK", "START", "GUIDE",
    "LTHUMB", "RTHUMB", "DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT",
})
TERMINAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def number(value, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    if not low <= value <= high or not math.isfinite(value):
        raise ValueError(f"{name} must be finite and in [{low}, {high}]")
    return float(value)


def normalize_action(data) -> dict:
    if not isinstance(data, dict):
        raise ValueError("action must be an object")
    unknown = data.keys() - {"steering", "throttle", "brake", "buttons", "duration_ms"}
    if unknown:
        raise ValueError(f"unknown action fields: {', '.join(sorted(unknown))}")
    duration = data.get("duration_ms", 200)
    if isinstance(duration, bool) or not isinstance(duration, int) or not 0 <= duration <= 5000:
        raise ValueError("duration_ms must be an integer in [0, 5000]")
    buttons = data.get("buttons", [])
    if not isinstance(buttons, list) or len(buttons) > len(BUTTONS):
        raise ValueError("buttons must be a list of Xbox button names")
    if any(not isinstance(button, str) or button not in BUTTONS for button in buttons):
        raise ValueError(f"buttons must be one of: {', '.join(sorted(BUTTONS))}")
    return {
        "steering": number(data.get("steering", 0), "steering", -1, 1),
        "throttle": number(data.get("throttle", 0), "throttle", 0, 1),
        "brake": number(data.get("brake", 0), "brake", 0, 1),
        "buttons": list(dict.fromkeys(buttons)),
        "duration_ms": duration,
    }


def unpack_frame(data: bytes) -> tuple[float, bytes]:
    if len(data) < FRAME_HEADER.size + 4:
        raise ValueError("frame is too short")
    magic, captured_at = FRAME_HEADER.unpack_from(data)
    if magic != b"ADF1" or not math.isfinite(captured_at) or captured_at <= 0:
        raise ValueError("invalid frame header")
    jpeg = data[FRAME_HEADER.size:]
    if not jpeg.startswith(b"\xff\xd8") or not jpeg.endswith(b"\xff\xd9"):
        raise ValueError("frame must contain JPEG data")
    return captured_at, jpeg
