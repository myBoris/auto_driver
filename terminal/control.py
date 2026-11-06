"""Full-state gamepad input, with a watchdog independent of the network loop."""

from __future__ import annotations

import math
import sys
import threading
import time
from typing import Any, Protocol


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


class GamepadBackend(Protocol):
    def write(self, action: dict[str, Any]) -> None: ...


class MockGamepad:
    """Hardware-free controller used only with explicit --mock."""

    def __init__(self) -> None:
        self.state = neutral_action()

    def write(self, action: dict[str, Any]) -> None:
        self.state = {**action, "buttons": list(action["buttons"])}


class Xbox360Gamepad:
    def __init__(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("real gamepad control requires Windows and ViGEmBus; use --mock for tests")
        try:
            import vgamepad
            self._module = vgamepad
            self._pad = vgamepad.VX360Gamepad()
            self.write(neutral_action())
        except Exception as exc:
            raise RuntimeError("cannot connect virtual Xbox 360 controller; install ViGEmBus and vgamepad") from exc

    def write(self, action: dict[str, Any]) -> None:
        self._pad.reset()
        self._pad.left_joystick_float(x_value_float=action["steering"], y_value_float=0.0)
        self._pad.right_trigger_float(value_float=action["throttle"])
        self._pad.left_trigger_float(value_float=action["brake"])
        for button in action["buttons"]:
            self._pad.press_button(button=getattr(self._module.XUSB_BUTTON, f"XUSB_GAMEPAD_{BUTTONS[button]}"))
        self._pad.update()


class Controller:
    """A command replaces every input; a timer always returns the device to neutral."""

    def __init__(self, backend: GamepadBackend) -> None:
        self.backend = backend
        self._lock = threading.RLock()
        self._timer: threading.Timer | None = None
        self._generation = 0
        self._state = neutral_action()
        self._error: str | None = None
        self._closed = False
        self.backend.write(self._state)

    def apply(self, value: Any, expires_at: Any) -> tuple[dict[str, Any], float]:
        action = normalize_action(value)
        if (isinstance(expires_at, bool) or not isinstance(expires_at, (int, float))
                or not 0 < expires_at < 1e12 or not math.isfinite(expires_at)):
            raise ValueError("expires_at must be a finite UNIX timestamp")
        with self._lock:
            if self._closed:
                raise RuntimeError("controller is closed")
            if expires_at <= time.time():
                raise ValueError("command expired before execution")
            self._generation += 1
            generation = self._generation
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            actual = action if action["duration_ms"] else neutral_action()
            try:
                self.backend.write(actual)
                self._state = actual
                self._error = None
            except Exception:
                self._reset_locked()
                raise
            applied_at = time.time()
            if action["duration_ms"]:
                self._timer = threading.Timer(action["duration_ms"] / 1000, self._expire, (generation,))
                self._timer.daemon = True
                self._timer.start()
            return {**actual, "buttons": list(actual["buttons"])}, applied_at

    def _reset_locked(self) -> None:
        try:
            self.backend.write(neutral_action())
            self._state = neutral_action()
            self._error = None
        except Exception as exc:
            self._error = f"gamepad reset failed: {exc}"

    def _expire(self, generation: int) -> None:
        with self._lock:
            if generation == self._generation:
                self._reset_locked()
                self._timer = None

    def release(self) -> None:
        with self._lock:
            self._generation += 1
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._reset_locked()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"ready": not self._closed and self._error is None, "state": {**self._state, "buttons": list(self._state["buttons"])}, "error": self._error}

    def close(self) -> None:
        with self._lock:
            self.release()
            self._closed = True
