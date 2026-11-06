"""控制生命周期：独立计时器在命令到期、断线或退出时释放所有输入。"""

import math
import threading
import time
from typing import Any

from .actions import neutral_action, normalize_action
from .backends import GamepadBackend

class Controller:
    """输入整体替换；使用代次防止旧计时器错误释放新命令，不依赖网络事件循环。"""

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
            # 只有驱动提交成功之后才记录执行时刻，训练 step 依此等待后续观察。
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
