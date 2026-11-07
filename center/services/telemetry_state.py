"""经过协议校验的 FH5 最新遥测；新鲜度只使用 Center 的单调时钟。"""

import asyncio
import math
import time


TTL_MS = 1000
REQUIRED = {"source", "is_race_on", "timestamp_ms", "speed_mps", "position", "velocity", "yaw"}
VECTORS = {"position", "velocity", "acceleration", "angular_velocity"}
FLOATS = {"speed_mps", "yaw", "pitch", "roll", "engine_rpm", "throttle", "brake",
          "clutch", "handbrake", "steering"}
ALLOWED = REQUIRED | VECTORS | FLOATS | {"gear"}


def finite(value, name, low=-1e12, high=1e12):
    if (isinstance(value, bool) or not isinstance(value, (float, int))
            or not low <= value <= high or not math.isfinite(value)):
        raise ValueError(f"invalid telemetry {name}")
    return float(value)


def normalize_telemetry(value):
    if not isinstance(value, dict) or not REQUIRED <= value.keys() or value.keys() - ALLOWED:
        raise ValueError("telemetry data requires verified FH5 Data Out fields only")
    if value["source"] != "fh5_data_out" or not isinstance(value["is_race_on"], bool):
        raise ValueError("invalid telemetry source or race flag")
    clock = value["timestamp_ms"]
    if isinstance(clock, bool) or not isinstance(clock, int) or not 0 <= clock <= 2**32 - 1:
        raise ValueError("invalid telemetry timestamp_ms")
    result = {"source": value["source"], "is_race_on": value["is_race_on"], "timestamp_ms": clock}
    for name in VECTORS & value.keys():
        vector = value[name]
        if not isinstance(vector, list) or len(vector) != 3:
            raise ValueError(f"telemetry {name} must contain three coordinates")
        result[name] = [finite(item, name) for item in vector]
    for name in FLOATS & value.keys():
        low, high = -1e12, 1e12
        if name in {"throttle", "brake", "clutch", "handbrake"}:
            low, high = 0, 1
        elif name == "steering":
            low, high = -1, 1
        elif name in {"speed_mps", "engine_rpm"}:
            low = 0
        result[name] = finite(value[name], name, low, high)
    if "gear" in value:
        gear = value["gear"]
        if isinstance(gear, bool) or not isinstance(gear, int) or not 0 <= gear <= 255:
            raise ValueError("invalid telemetry gear")
        result["gear"] = gear
    return result


class TelemetryState:
    def __init__(self):
        self.sequence = 0
        self.data = None
        self.captured_at = None
        self.received_at = None
        self.received_mono = None
        self.connected = True
        self.changed = asyncio.Event()

    def notify(self):
        previous, self.changed = self.changed, asyncio.Event()
        previous.set()

    def publish(self, message):
        captured_at = finite(message.get("captured_at"), "captured_at", 1e-12, 1e20)
        data = normalize_telemetry(message.get("data"))
        if not self.connected:
            return
        if self.captured_at is not None and captured_at < self.captured_at:
            raise ValueError("telemetry captured_at must not move backwards within a session")
        self.sequence += 1
        self.data, self.captured_at = data, captured_at
        self.received_at, self.received_mono = time.time(), time.monotonic()
        self.notify()

    def disconnect(self):
        self.connected = False
        self.data = None
        self.notify()

    def describe(self, after=None):
        age = max(0.0, time.monotonic() - self.received_mono) * 1000 if self.received_mono is not None else None
        if not self.connected:
            status = "disconnected"
        elif self.data is None:
            status = "waiting"
        elif age > TTL_MS:
            status = "stale"
        elif after is not None and self.sequence <= after:
            status = "waiting"
        else:
            status = "ready"
        return {"sequence": self.sequence, "status": status, "ttl_ms": TTL_MS,
                "captured_at": self.captured_at, "received_at": self.received_at,
                "age_ms": age, "data": self.data if status == "ready" else None}

    async def read(self, *, after=None, timeout=0, same_session=True):
        deadline = time.monotonic() + timeout
        while True:
            value = self.describe(after if same_session else None)
            remaining = deadline - time.monotonic()
            if value["status"] in {"ready", "disconnected"} or remaining <= 0 or not same_session:
                return value
            try:
                async with asyncio.timeout(remaining):
                    await self.changed.wait()
            except TimeoutError:
                return self.describe(after)
