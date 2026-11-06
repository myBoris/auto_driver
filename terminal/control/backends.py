"""手柄后端：模拟输入与 ViGEmBus Xbox 360 映射；导入模块不会连接驱动。"""

import sys
from typing import Any, Protocol

from .actions import BUTTONS, neutral_action

class GamepadBackend(Protocol):
    def write(self, action: dict[str, Any]) -> None: ...


class MockGamepad:
    """仅显式 --mock 使用的内存后端，不连接总线、不向真实游戏写入输入。"""

    def __init__(self) -> None:
        self.state = neutral_action()

    def write(self, action: dict[str, Any]) -> None:
        self.state = {**action, "buttons": list(action["buttons"])}


class Xbox360Gamepad:
    """唯一的真实驱动适配层：转向映射左摇杆 X，油门/刹车映射 RT/LT。"""
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
