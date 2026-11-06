"""硬件模式的失败语义、凭据保护和配置优先级；不创建实际控制器。"""

import subprocess
import io
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from terminal.capture import LatestCapture
from terminal.video.hardware import HardwareEncoderUnavailable, bitrate_bits, select_hardware_encoder
from terminal.video.publisher import HardwareVideoPublisher


class HardwareSelectionTests(unittest.TestCase):
    def test_auto_uses_a_successfully_opened_hardware_encoder(self):
        results = [subprocess.CompletedProcess([], 1, b"", b"NVENC unavailable"),
                   subprocess.CompletedProcess([], 0, b"", b"")]
        with patch("terminal.video.hardware.subprocess.run", side_effect=results) as run:
            self.assertEqual(select_hardware_encoder("ffmpeg", "auto"), "h264_qsv")
        commands = [entry.args[0] for entry in run.call_args_list]
        self.assertEqual([command[command.index("-c:v") + 1] for command in commands], ["h264_nvenc", "h264_qsv"])
        self.assertTrue(all("-frames:v" in command for command in commands))

    def test_forced_hardware_failure_never_falls_back_to_software(self):
        result = subprocess.CompletedProcess([], 1, b"", b"device cannot initialize")
        with patch("terminal.video.hardware.subprocess.run", return_value=result) as run:
            with self.assertRaises(HardwareEncoderUnavailable):
                select_hardware_encoder("ffmpeg", "h264_nvenc")
        self.assertEqual(run.call_count, 1)
        self.assertNotIn("libx264", run.call_args.args[0])

    def test_software_encoder_is_rejected_before_launch(self):
        with patch("terminal.video.hardware.subprocess.run") as run:
            with self.assertRaises(ValueError):
                select_hardware_encoder("ffmpeg", "libx264")
        run.assert_not_called()


class VideoConfigurationTests(unittest.TestCase):
    def test_cli_disable_has_priority_over_center_configuration(self):
        source = LatestCapture(60, 1280, 75, video_enabled=False)
        source.configure_video({"enabled": True, "publish_url": "rtsp://publisher:secret@example/terminal/test"})
        self.assertFalse(source.video_status()["enabled"])
        self.assertFalse(source.video_status()["ready"])
        self.assertNotIn("secret", str(source.video_status()))

    def test_publisher_errors_redact_all_rtsp_credentials(self):
        publisher = HardwareVideoPublisher("rtsp://publisher:secret%40token@example:8554/terminal/test", fps=60)
        message = publisher._safe_error("output rtsp://publisher:secret%40token@example:8554/terminal/test failed; rtsp://another:second-secret@elsewhere/path")
        self.assertNotIn("secret", message)
        self.assertNotIn("second-secret", message)
        self.assertNotIn("publisher:", message)

    def test_bitrate_units_and_invalid_values(self):
        self.assertEqual(bitrate_bits("8M"), 8000000)
        self.assertEqual(bitrate_bits("500K"), 500000)
        for value in ("0", "junk", "1", "99999999999M"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                bitrate_bits(value)

    def test_stalled_output_terminates_encoder_and_unblocks_writer(self):
        publisher = HardwareVideoPublisher("rtsp://publisher:secret@example:8554/terminal/test", fps=60)
        entered = threading.Event()
        unblocked = threading.Event()

        class BlockedPipe:
            def write(self, data):
                entered.set()
                unblocked.wait()
                raise BrokenPipeError("encoder terminated")

            def close(self):
                pass

        process = SimpleNamespace(stdin=BlockedPipe(), stdout=io.BytesIO(), stderr=io.BytesIO(), returncode=None)
        process.poll = lambda: process.returncode

        def terminate():
            process.returncode = 0
            unblocked.set()

        process.terminate = terminate
        process.wait = lambda timeout: process.returncode
        process.kill = terminate
        publisher._process = process
        publisher._process_started_at = 100.0
        publisher._last_submit_at = 108.0
        publisher._last_output_at = 101.0

        def blocked_writer():
            try:
                process.stdin.write(b"raw frame")
            except BrokenPipeError:
                pass

        writer = threading.Thread(target=blocked_writer, daemon=True)
        writer.start()
        self.assertTrue(entered.wait(1))
        self.assertTrue(publisher._check_stalled(now=108.0))
        writer.join(timeout=1)
        self.assertFalse(writer.is_alive())
        self.assertIsNone(publisher._process)
        self.assertIn("stalled", publisher.status()["error"])
        publisher.close()
        self.assertFalse(publisher._check_stalled(now=200.0))


if __name__ == "__main__":
    unittest.main()
