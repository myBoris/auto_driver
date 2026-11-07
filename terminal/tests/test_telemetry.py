"""FH5 小端二进制包和真实 UDP/最新槽验证，不读取或控制真实游戏。"""

import asyncio
import socket
import struct
import unittest

from terminal.network.telemetry import FH5Telemetry, parse_fh5_packet
from terminal.network.client import TerminalClient
from terminal.config.connection import TerminalConfig
from terminal.capture import LatestCapture
from terminal.control import Controller, MockGamepad


def packet(*, clock=1234, speed=12.5, steer=-128):
    value = bytearray(324)
    struct.pack_into("<iI", value, 0, 1, clock)
    struct.pack_into("<f", value, 16, 3500)
    struct.pack_into("<fff", value, 20, .5, 1, 2)
    struct.pack_into("<fff", value, 32, 1, 2, 12)
    struct.pack_into("<fff", value, 44, .1, .2, .3)
    struct.pack_into("<fff", value, 56, .4, .5, .6)
    # Horizon 的 12 字节扩展不能当作世界位置。
    struct.pack_into("<fff", value, 232, 999, 888, 777)
    struct.pack_into("<ffff", value, 244, 100, 200, 300, speed)
    struct.pack_into("<BBBBBb", value, 315, 255, 128, 64, 0, 3, steer)
    return bytes(value)


class FH5PacketTests(unittest.TestCase):
    def test_exact_offsets_units_local_axes_and_signed_steering(self):
        data = parse_fh5_packet(packet())
        self.assertEqual(data["position"], [100, 200, 300])
        self.assertEqual(data["velocity"], [1, 2, 12])
        self.assertEqual(data["speed_mps"], 12.5)
        self.assertEqual(data["engine_rpm"], 3500)
        self.assertEqual(data["steering"], -1)
        self.assertEqual(data["throttle"], 1)
        self.assertAlmostEqual(data["brake"], 128 / 255)
        self.assertAlmostEqual(data["clutch"], 64 / 255)
        self.assertEqual(data["gear"], 3)
        self.assertEqual(data["timestamp_ms"], 1234)
        self.assertTrue(data["is_race_on"])
        self.assertNotIn("collision", data)
        self.assertNotIn("road_offset", data)
        self.assertEqual(parse_fh5_packet(packet(steer=127))["steering"], 1)

    def test_other_forza_packet_lengths_and_nonfinite_fields_are_rejected(self):
        for size in (0, 232, 311, 323, 325):
            with self.subTest(size=size), self.assertRaises(ValueError):
                parse_fh5_packet(bytes(size))
        for offset in (16, 20, 32, 44, 56, 244, 256):
            bad = bytearray(packet())
            struct.pack_into("<f", bad, offset, float("nan"))
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                parse_fh5_packet(bad)
        bad = bytearray(packet())
        struct.pack_into("<i", bad, 0, 9)
        with self.assertRaises(ValueError):
            parse_fh5_packet(bad)


class FH5UDPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.receiver = FH5Telemetry(port=0)
        await self.receiver.start()
        self.addCleanup(self.receiver.close)
        self.sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(self.sender.close)

    def send(self, value):
        self.sender.sendto(value, ("127.0.0.1", self.receiver.port))

    async def test_real_udp_invalid_packet_is_ignored_and_newest_sample_wins(self):
        self.send(b"not FH5")
        self.send(packet(clock=100))
        sequence, sample = await asyncio.wait_for(self.receiver.next_sample(0), 1)
        self.assertEqual(sample["data"]["timestamp_ms"], 100)
        self.assertEqual(sequence, 1)
        self.assertEqual(self.receiver.invalid_packets, 1)
        self.send(packet(clock=101))
        await asyncio.wait_for(self.receiver.next_sample(1), 1)
        self.send(packet(clock=102))
        await asyncio.wait_for(self.receiver.next_sample(2), 1)
        sequence, latest = await self.receiver.next_sample(0)
        self.assertEqual(sequence, 3)
        self.assertEqual(latest["data"]["timestamp_ms"], 102)
        self.assertTrue(self.receiver.status()["ready"])
        self.receiver.received_mono -= 2
        self.assertFalse(self.receiver.status()["ready"])
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(self.receiver.next_sample(0), .02)

    async def test_disabled_and_occupied_listener_do_not_raise(self):
        disabled = FH5Telemetry(enabled=False, port=self.receiver.port)
        await disabled.start()
        self.assertFalse(disabled.status()["listening"])
        unavailable = FH5Telemetry(port=self.receiver.port)
        self.addCleanup(unavailable.close)
        with self.assertLogs("terminal.network.telemetry", level="WARNING"):
            await unavailable.start()
        self.assertIsNotNone(unavailable.error)
        self.assertFalse(unavailable.status()["listening"])

    async def test_blocked_ws_upload_coalesces_and_reconnect_does_not_replay(self):
        controller = Controller(MockGamepad())
        self.addCleanup(controller.close)
        client = TerminalClient(TerminalConfig(token="test", terminal_id="test", name="test"),
                                LatestCapture(fps=10, width=320, quality=75, video_enabled=False), controller)
        client.telemetry = self.receiver
        entered, finished = asyncio.Queue(), asyncio.Queue()
        release = asyncio.Event()

        class Socket:
            async def send_json(self, value):
                entered.put_nowait(value)
                await release.wait()
                finished.put_nowait(value)

        # 旧会话/未连接时的包不得重放。
        self.send(packet(clock=99))
        await asyncio.wait_for(self.receiver.next_sample(0), 1)
        upload = asyncio.create_task(client._telemetry(Socket()))
        try:
            await asyncio.sleep(0)
            self.assertTrue(entered.empty())
            self.send(packet(clock=100))
            first = await asyncio.wait_for(entered.get(), 1)
            self.assertEqual(first["data"]["timestamp_ms"], 100)
            self.send(packet(clock=101))
            await asyncio.wait_for(self.receiver.next_sample(2), 1)
            self.send(packet(clock=102))
            await asyncio.wait_for(self.receiver.next_sample(3), 1)
            release.set()
            await asyncio.wait_for(finished.get(), 1)
            latest = await asyncio.wait_for(finished.get(), 1)
            self.assertEqual(latest["data"]["timestamp_ms"], 102)
        finally:
            upload.cancel()
            await asyncio.gather(upload, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
