"""FH5 Data Out 被动接收器；只保留最新有效 Dash 包，不产生游戏输入。"""

from __future__ import annotations

import asyncio
import logging
import math
import struct
import time
from typing import Any


LOG = logging.getLogger(__name__)
MAX_AGE = 1.0


def parse_fh5_packet(packet: bytes) -> dict[str, Any]:
    """解析 FH5 的 324 字节小端 Dash 包（Sled 后有 12 字节 Horizon 扩展）。

    Position 是世界坐标；Velocity/Acceleration 是车辆局部轴 X右/Y上/Z前。
    最后一个 FH5 扩展字节和未核实字段不输出；其它 Forza 包长明确拒绝。
    """
    if len(packet) != 324:
        raise ValueError("FH5 Dash packet must contain exactly 324 bytes")
    race, clock = struct.unpack_from("<iI", packet)
    if race not in (0, 1):
        raise ValueError("invalid FH5 IsRaceOn flag")

    def floats(offset: int, count: int = 1):
        values = struct.unpack_from("<" + "f" * count, packet, offset)
        if any(not math.isfinite(value) or abs(value) > 1e12 for value in values):
            raise ValueError("non-finite or invalid FH5 float")
        return values[0] if count == 1 else list(values)

    speed = floats(256)
    rpm = floats(16)
    if speed < 0 or rpm < 0:
        raise ValueError("FH5 speed and RPM must be nonnegative")
    throttle, brake, clutch, handbrake, gear, steering = struct.unpack_from("<BBBBBb", packet, 315)
    return {
        "source": "fh5_data_out", "is_race_on": bool(race), "timestamp_ms": clock,
        "speed_mps": speed, "position": floats(244, 3),
        "velocity": floats(32, 3), "acceleration": floats(20, 3),
        "angular_velocity": floats(44, 3), "yaw": floats(56),
        "pitch": floats(60), "roll": floats(64), "engine_rpm": rpm,
        "throttle": throttle / 255, "brake": brake / 255,
        "clutch": clutch / 255, "handbrake": handbrake / 255,
        "gear": gear, "steering": max(-1.0, steering / 127),
    }


class FH5Telemetry(asyncio.DatagramProtocol):
    """UDP 与 WebSocket 之间用单个槽传递，网络变慢时覆盖旧包。"""

    def __init__(self, *, enabled: bool = True, host: str = "127.0.0.1", port: int = 5300):
        self.enabled, self.host, self.port = enabled, host, port
        self.transport: asyncio.DatagramTransport | None = None
        self.sequence = 0
        self.sample: dict[str, Any] | None = None
        self.received_mono = 0.0
        self.changed = asyncio.Event()
        self.error: str | None = None
        self.invalid_packets = 0

    async def start(self) -> None:
        if not self.enabled or self.transport is not None:
            return
        try:
            transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                lambda: self, local_addr=(self.host, self.port),
            )
            self.transport = transport
            self.port = transport.get_extra_info("sockname")[1]
            LOG.info("FH5 Data Out listening on %s:%s", self.host, self.port)
        except OSError as exc:
            self.error = f"FH5 Data Out listener unavailable: {exc}"
            LOG.warning("%s; capture and control remain available", self.error)

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            value = parse_fh5_packet(data)
        except (ValueError, struct.error):
            self.invalid_packets += 1
            return
        self.sequence += 1
        self.received_mono = time.monotonic()
        self.sample = {"captured_at": time.time(), "data": value}
        self.error = None
        previous, self.changed = self.changed, asyncio.Event()
        previous.set()

    def error_received(self, exc) -> None:
        self.error = f"FH5 Data Out UDP error: {exc}"

    def status(self) -> dict[str, Any]:
        age = max(0.0, time.monotonic() - self.received_mono) if self.sample else None
        return {"enabled": self.enabled, "listening": self.transport is not None,
                "host": self.host, "port": self.port,
                "ready": age is not None and age <= MAX_AGE,
                "age_ms": round(age * 1000, 3) if age is not None else None,
                "sequence": self.sequence, "invalid_packets": self.invalid_packets,
                "error": self.error}

    async def next_sample(self, after: int) -> tuple[int, dict[str, Any]]:
        while True:
            if self.sequence > after and self.sample and time.monotonic() - self.received_mono <= MAX_AGE:
                return self.sequence, self.sample
            await self.changed.wait()

    def close(self) -> None:
        if self.transport is not None:
            self.transport.close()
            self.transport = None
        self.sample = None
