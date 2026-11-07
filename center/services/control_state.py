"""已确认的驾驶输入读模型；按中心单调时钟过期，并唤醒所有监看者。"""

import asyncio
import math
import time


def neutral_state():
    return {"steering": 0.0, "throttle": 0.0, "brake": 0.0,
            "buttons": [], "duration_ms": 0}


class ControlState:
    def __init__(self):
        self.revision = 0
        self.status = "idle"
        self.state = neutral_state()
        self.command_id = None
        self.applied_at = None
        self.deadline = 0.0
        self.changed = asyncio.Event()
        self.timer = None

    def update(self, status, state=None, *, command_id=None, applied_at=None):
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        self.revision += 1
        self.status = status
        self.state = ({**state, "buttons": list(state["buttons"])}
                      if state is not None else neutral_state())
        self.command_id = command_id
        self.applied_at = applied_at
        duration = self.state["duration_ms"] / 1000 if status == "active" else 0
        self.deadline = time.monotonic() + duration if duration else 0.0
        if duration:
            self.timer = asyncio.get_running_loop().call_later(
                duration, self.expire, self.revision,
            )
        # 轮换 Event 使同步 ACK 回调能立即唤醒任意数量的长轮询，无需通知任务。
        previous = self.changed
        self.changed = asyncio.Event()
        previous.set()

    def expire(self, revision):
        if revision == self.revision and self.status == "active":
            self.update("expired", command_id=self.command_id,
                        applied_at=self.applied_at)

    def describe(self):
        if self.deadline and time.monotonic() >= self.deadline:
            self.expire(self.revision)
        remaining = max(0.0, self.deadline - time.monotonic()) if self.deadline else 0
        return {
            "revision": self.revision, "status": self.status,
            "state": {**self.state, "buttons": list(self.state["buttons"])},
            "command_id": self.command_id, "applied_at": self.applied_at,
            "remaining_ms": math.ceil(remaining * 1000),
        }

    async def read(self, *, after=None, timeout=0, same_session=True):
        deadline = time.monotonic() + timeout
        while True:
            value = self.describe()
            remaining = deadline - time.monotonic()
            if (after is None or value["revision"] != after or not same_session
                    or self.status == "disconnected" or remaining <= 0):
                return value
            changed = self.changed
            try:
                async with asyncio.timeout(remaining):
                    await changed.wait()
            except TimeoutError:
                return self.describe()
