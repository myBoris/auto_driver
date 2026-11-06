import asyncio
import json
import multiprocessing
import os
import struct
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

from terminal.capture import LatestCapture, MockCapture, WindowCapture, _SharedFrameSlot
from terminal.client import TerminalClient, TerminalConfig
from terminal.control import Controller, MockGamepad, Xbox360Gamepad, normalize_action
from terminal.discovery import DiscoveryProtocol, validate_center_url


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.pad = MockGamepad()
        self.controller = Controller(self.pad)
        self.addCleanup(self.controller.close)

    def apply(self, action):
        return self.controller.apply(action, time.time() + 10)

    def test_watchdog_releases_without_network_loop(self):
        released = threading.Event()
        original = self.pad.write

        def observe(action):
            original(action)
            if action["throttle"] == 0:
                released.set()

        self.pad.write = observe
        self.apply({"throttle": 0.8, "duration_ms": 30})
        self.assertEqual(self.pad.state["throttle"], 0.8)
        self.assertTrue(released.wait(1), "watchdog must release even when no asyncio loop runs")
        self.assertEqual(self.pad.state["throttle"], 0)

    def test_new_action_replaces_all_old_inputs(self):
        self.apply({"steering": 1, "throttle": 1, "buttons": ["A"], "duration_ms": 5000})
        state, _ = self.apply({"brake": 0.5, "duration_ms": 5000})
        self.assertEqual(state["steering"], 0)
        self.assertEqual(state["throttle"], 0)
        self.assertEqual(state["buttons"], [])
        self.assertEqual(state["brake"], 0.5)

    def test_obsolete_watchdog_does_not_reset_new_command(self):
        self.apply({"throttle": 1, "duration_ms": 5000})
        old_generation = self.controller._generation
        self.apply({"steering": -0.5, "duration_ms": 5000})
        self.controller._expire(old_generation)
        self.assertEqual(self.pad.state["steering"], -0.5)

    def test_expired_command_never_reaches_device(self):
        with self.assertRaisesRegex(ValueError, "expired"):
            self.controller.apply({"throttle": 1}, time.time() - 1)
        self.assertEqual(self.pad.state["throttle"], 0)

    def test_zero_duration_and_disconnect_release(self):
        self.apply({"throttle": 1, "duration_ms": 5000})
        self.controller.release()
        self.assertEqual(self.pad.state["throttle"], 0)
        state, _ = self.apply({"throttle": 1, "duration_ms": 0})
        self.assertEqual(state["throttle"], 0)
        self.assertEqual(self.pad.state["throttle"], 0)

    def test_invalid_actions_do_not_partially_apply(self):
        for action in ({"throttle": float("nan")}, {"throttle": 10**500}, {"steering": True}, {"buttons": ["INVALID"]},
                       {"duration_ms": 5001}, {"duration_ms": 1.5}, {"throttle": 2}, {"other": 1}):
            with self.subTest(action=action), self.assertRaises(ValueError):
                normalize_action(action)

    def test_closed_controller_cannot_be_rearmed(self):
        self.controller.close()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            self.apply({"throttle": 1})


class XboxMappingTests(unittest.TestCase):
    def backend(self):
        # Use the published XUSB masks, with no vgamepad import or device creation.
        constants = {
            "XUSB_GAMEPAD_DPAD_UP": 0x0001, "XUSB_GAMEPAD_DPAD_DOWN": 0x0002,
            "XUSB_GAMEPAD_DPAD_LEFT": 0x0004, "XUSB_GAMEPAD_DPAD_RIGHT": 0x0008,
            "XUSB_GAMEPAD_START": 0x0010, "XUSB_GAMEPAD_BACK": 0x0020,
            "XUSB_GAMEPAD_LEFT_THUMB": 0x0040, "XUSB_GAMEPAD_RIGHT_THUMB": 0x0080,
            "XUSB_GAMEPAD_LEFT_SHOULDER": 0x0100, "XUSB_GAMEPAD_RIGHT_SHOULDER": 0x0200,
            "XUSB_GAMEPAD_GUIDE": 0x0400, "XUSB_GAMEPAD_A": 0x1000,
            "XUSB_GAMEPAD_B": 0x2000, "XUSB_GAMEPAD_X": 0x4000, "XUSB_GAMEPAD_Y": 0x8000,
        }
        backend = object.__new__(Xbox360Gamepad)
        backend._module = SimpleNamespace(XUSB_BUTTON=SimpleNamespace(**constants))
        backend._pad = Mock()
        return backend

    def test_all_fifteen_button_names_reach_correct_xusb_mask(self):
        backend = self.backend()
        expected = {
            "A": 0x1000, "B": 0x2000, "X": 0x4000, "Y": 0x8000,
            "LB": 0x0100, "RB": 0x0200, "BACK": 0x0020, "START": 0x0010,
            "GUIDE": 0x0400, "LTHUMB": 0x0040, "RTHUMB": 0x0080,
            "DPAD_UP": 0x0001, "DPAD_DOWN": 0x0002,
            "DPAD_LEFT": 0x0004, "DPAD_RIGHT": 0x0008,
        }
        for name, mask in expected.items():
            with self.subTest(button=name):
                backend._pad.reset_mock()
                backend.write(normalize_action({"buttons": [name]}))
                backend._pad.press_button.assert_called_once_with(button=mask)
                self.assertEqual(backend._pad.mock_calls[0], call.reset())
                self.assertEqual(backend._pad.mock_calls[-1], call.update())

    def test_steering_and_pedals_use_expected_xinput_axes(self):
        backend = self.backend()
        backend.write(normalize_action({"steering": -0.4, "throttle": 0.7, "brake": 0.2}))
        backend._pad.left_joystick_float.assert_called_once_with(x_value_float=-0.4, y_value_float=0.0)
        backend._pad.right_trigger_float.assert_called_once_with(value_float=0.7)
        backend._pad.left_trigger_float.assert_called_once_with(value_float=0.2)
        backend._pad.press_button.assert_not_called()
        backend._pad.reset.assert_called_once()
        backend._pad.update.assert_called_once()


class CaptureTests(unittest.TestCase):
    def test_native_worker_publishes_only_latest_jpeg(self):
        from PIL import Image, ImageDraw  # Warm dependency imports before timing frame delivery.

        source = MockCapture(fps=30, width=320, quality=60)
        source.start()
        self.addCleanup(source.close)
        deadline = time.monotonic() + 2
        while (source.latest() is None or source.latest().sequence < 2) and time.monotonic() < deadline:
            time.sleep(0.01)
        frame = source.latest()
        self.assertIsNotNone(frame)
        self.assertGreaterEqual(frame.sequence, 2)
        packet = frame.packet()
        self.assertEqual(packet[:4], b"ADF1")
        self.assertEqual(struct.unpack("!d", packet[4:12])[0], frame.captured_at)
        self.assertTrue(packet[12:].startswith(b"\xff\xd8"))
        self.assertTrue(packet.endswith(b"\xff\xd9"))
        self.assertTrue(source.status()["ready"])
        self.assertEqual((frame.width, frame.height), (320, 180))


class _RecordingFrameSocket:
    def __init__(self, *, block_first=False):
        self.entered = asyncio.Queue()
        self.completed = asyncio.Queue()
        self.packets = []
        self.release_first = asyncio.Event()
        if not block_first:
            self.release_first.set()

    async def send_bytes(self, packet):
        first = not self.packets
        self.packets.append(packet)
        self.entered.put_nowait(packet)
        if first:
            await self.release_first.wait()
        self.completed.put_nowait(packet)


class FrameUploadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.capture = LatestCapture(fps=120, width=320, quality=75)
        self.controller = Controller(MockGamepad())
        self.addCleanup(self.controller.close)
        self.client = TerminalClient(
            TerminalConfig(token="upload-test", terminal_id="upload-test", name="upload-test", mock=True),
            self.capture, self.controller,
        )

    def publish(self, marker):
        self.capture._publish(b"\xff\xd8" + marker + b"\xff\xd9", time.time(), 320, 180)
        return self.capture.latest().packet()

    async def next_packet(self, channel):
        # This is a deadlock guard; delivery is synchronized by queue events, not FPS.
        return await asyncio.wait_for(channel.get(), timeout=2)

    async def cancel_upload(self, task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def flush_loop_callbacks(self):
        completed = asyncio.get_running_loop().create_future()
        asyncio.get_running_loop().call_soon(completed.set_result, None)
        await completed

    async def test_existing_and_thread_published_frames_send_and_cancellation_unsubscribes(self):
        initial = self.publish(b"existing")
        socket = _RecordingFrameSocket()
        with patch.object(self.capture, "add_frame_listener", wraps=self.capture.add_frame_listener) as added, \
                patch.object(self.capture, "remove_frame_listener", wraps=self.capture.remove_frame_listener) as removed:
            task = asyncio.create_task(self.client._frames(socket))
            self.addAsyncCleanup(self.cancel_upload, task)
            self.assertEqual(await self.next_packet(socket.completed), initial)

            def produce_from_thread():
                return threading.get_ident(), self.publish(b"from-thread")

            producer_thread, newest = await asyncio.to_thread(produce_from_thread)
            self.assertNotEqual(producer_thread, threading.get_ident())
            self.assertEqual(await self.next_packet(socket.completed), newest)
            await self.cancel_upload(task)
            self.assertTrue(task.cancelled())
            added.assert_called_once()
            removed.assert_called_once_with(added.call_args.args[0])

            await asyncio.to_thread(self.publish, b"after-cancellation")
            await self.flush_loop_callbacks()
            self.assertEqual(socket.packets, [initial, newest])
            self.assertTrue(socket.completed.empty())

    async def test_blocked_upload_keeps_only_newest_frame_and_cleans_listener(self):
        initial = self.publish(b"initial")
        socket = _RecordingFrameSocket(block_first=True)
        with patch.object(self.capture, "add_frame_listener", wraps=self.capture.add_frame_listener) as added, \
                patch.object(self.capture, "remove_frame_listener", wraps=self.capture.remove_frame_listener) as removed:
            task = asyncio.create_task(self.client._frames(socket))
            self.addAsyncCleanup(self.cancel_upload, task)
            self.assertEqual(await self.next_packet(socket.entered), initial)

            def publish_while_blocked():
                intermediate = self.publish(b"superseded")
                newest = self.publish(b"newest")
                return intermediate, newest

            intermediate, newest = await asyncio.to_thread(publish_while_blocked)
            self.assertEqual(socket.packets, [initial])
            socket.release_first.set()
            self.assertEqual(await self.next_packet(socket.completed), initial)
            self.assertEqual(await self.next_packet(socket.completed), newest)
            await self.flush_loop_callbacks()
            self.assertEqual(socket.packets, [initial, newest])
            self.assertNotIn(intermediate, socket.packets)

            await self.cancel_upload(task)
            removed.assert_called_once_with(added.call_args.args[0])


def _crashing_capture_worker(config, frames, channel, stop):
    frames.publish(b"\xff\xd8\xff\xd9", time.time(), 320, 180)
    time.sleep(0.3)
    os._exit(17)


def _hung_capture_worker(config, frames, channel, stop):
    time.sleep(60)


@unittest.skipUnless(os.name == "nt", "Windows native worker supervisor")
class CaptureIsolationTests(unittest.TestCase):
    def test_shared_slot_overwrites_instead_of_queuing(self):
        slot = _SharedFrameSlot(multiprocessing.get_context("spawn"))
        slot.publish(b"first", 1, 10, 20)
        slot.publish(b"latest", 2, 30, 40)
        sequence, jpeg, timestamp, width, height = slot.read_after(0)
        self.assertEqual((sequence, jpeg, timestamp, width, height), (2, b"latest", 2, 30, 40))
        self.assertIsNone(slot.read_after(sequence))

    def test_native_crash_restarts_without_killing_parent(self):
        source = WindowCapture("unused", fps=15, width=320, quality=75)
        source._worker_target = _crashing_capture_worker
        source._restart_delay = 0.05
        source.start()
        self.addCleanup(source.close)
        deadline = time.monotonic() + 10
        while (source.latest() is None or source.latest().sequence < 2) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIsNotNone(source.latest())
        self.assertGreaterEqual(source.latest().sequence, 2, "a new child must publish after the first child crashes")

    def test_hung_native_worker_keeps_gamepad_watchdog_alive(self):
        source = WindowCapture("unused", fps=15, width=320, quality=75)
        source._worker_target = _hung_capture_worker
        source._worker_timeout = 0.4
        source._restart_delay = 0.05
        source.start()
        self.addCleanup(source.close)
        pad = MockGamepad()
        controller = Controller(pad)
        self.addCleanup(controller.close)
        controller.apply({"throttle": 1, "duration_ms": 30}, time.time() + 10)
        deadline = time.monotonic() + 2
        while pad.state["throttle"] and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(pad.state["throttle"], 0)
        deadline = time.monotonic() + 4
        while "unresponsive" not in (source.status()["error"] or "") and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIn("unresponsive", source.status()["error"])


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_nonce_and_service_prevent_unrelated_offer(self):
        result = asyncio.get_running_loop().create_future()
        protocol = DiscoveryProtocol("expected", result)
        offer = {"service": "auto-driver-center", "type": "offer", "protocol": 1, "nonce": "wrong", "port": 8000}
        protocol.datagram_received(json.dumps(offer).encode(), ("192.168.1.3", 9999))
        self.assertFalse(result.done())
        offer["nonce"] = "expected"
        protocol.datagram_received(json.dumps(offer).encode(), ("192.168.1.3", 9999))
        self.assertEqual(result.result(), "http://192.168.1.3:8000")

    async def test_invalid_advertised_address_is_ignored(self):
        result = asyncio.get_running_loop().create_future()
        protocol = DiscoveryProtocol("nonce", result)
        offer = {"service": "auto-driver-center", "type": "offer", "protocol": 1, "nonce": "nonce", "port": 8000, "url": "http://user:secret@host"}
        protocol.datagram_received(json.dumps(offer).encode(), ("127.0.0.1", 9999))
        self.assertFalse(result.done())

    def test_center_url_does_not_accept_embedded_secrets_or_paths(self):
        for value in ("file:///etc/passwd", "http://user:pass@host", "http://host/path", "http://host?token=secret"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_center_url(value)
        self.assertEqual(validate_center_url("https://localhost:8000/"), "https://localhost:8000")


if __name__ == "__main__":
    unittest.main()
