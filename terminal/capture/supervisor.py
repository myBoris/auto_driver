"""父进程采集管理：检测 SDK 崩溃、配置代次变化，并传递最新观察与视频状态。"""

from __future__ import annotations

import logging
import multiprocessing
import queue
import sys
import time

from .base import LatestCapture
from .processes import _stop_capture_process
from .shared import _SharedFrameSlot
from .worker import _capture_process_main

LOG = logging.getLogger(__name__)


class SupervisedCapture(LatestCapture):
    """父进程不导入原生采集 SDK，也不执行编码器写入，避免影响手柄安全计时器。"""

    def __init__(self, fps: int, width: int, quality: int, *, window_title="", mock=False, **options):
        super().__init__(fps, width, quality, **options)
        self.window_title = window_title
        self.mock = mock
        self._worker_target = _capture_process_main
        self._worker_timeout = 45.0
        self._restart_delay = 2.0

    def _run(self) -> None:
        context = multiprocessing.get_context("spawn")
        failures = 0
        while not self._stop.is_set():
            with self._lock:
                generation = self._config_generation
                video_config = dict(self._video_config)
            config = {"window_title": self.window_title, "mock": self.mock, "fps": self.fps,
                      "width": self.width, "quality": self.quality, "observation_fps": self.observation_fps,
                      "video_bitrate": self.video_bitrate, "video_encoder": self.video_encoder,
                      "ffmpeg_binary": self.ffmpeg_binary, "video": video_config}
            frames = _SharedFrameSlot(context)
            channel = context.Queue(maxsize=1)
            stop = context.Event()
            process = context.Process(target=self._worker_target, args=(config, frames, channel, stop),
                                      daemon=True, name="game-capture-and-hardware-video")
            changed = False
            try:
                process.start()
                LOG.info("Capture worker started (PID %s, video %s)", process.pid, video_config["enabled"])
                sequence = 0
                last_activity = time.monotonic()
                previous_error = None
                while not self._stop.is_set():
                    time.sleep(min(0.005, 0.5 / self.observation_fps))
                    with self._lock:
                        changed = self._config_generation != generation
                    if changed:
                        break
                    if not process.is_alive():
                        raise RuntimeError(f"native capture worker exited ({process.exitcode})")
                    packet = frames.read_after(sequence)
                    if packet is not None:
                        sequence, jpeg, captured_at, width, height = packet
                        self._publish(jpeg, captured_at, width, height)
                        last_activity = time.monotonic()
                        failures = 0
                    try:
                        message = channel.get_nowait()
                    except queue.Empty:
                        message = None
                    if message:
                        last_activity = time.monotonic()
                        if isinstance(message.get("video"), dict):
                            with self._lock:
                                self._video_status.update(message["video"])
                        error = message.get("error")
                        latest = self.latest()
                        if error and (latest is None or message["timestamp"] >= latest.captured_at):
                            self._set_error(error)
                            if error != previous_error:
                                LOG.info("Capture status: %s", error)
                                previous_error = error
                    if time.monotonic() - last_activity > self._worker_timeout:
                        raise RuntimeError("native capture worker is unresponsive; restarting")
            except Exception as exc:
                failures += 1
                self._set_error(str(exc))
                with self._lock:
                    self._video_status.update(ready=False, error="capture worker stopped; restarting")
                LOG.warning("Capture unavailable: %s", exc)
            finally:
                if process.pid is not None:
                    _stop_capture_process(process, stop)
                channel.close()
                channel.cancel_join_thread()
            # 连接/发布地址变化立即重建；原生故障则退避，避免反复占用驱动资源。
            if not changed:
                self._stop.wait(min(10, self._restart_delay * max(1, failures)))

    def close(self):
        self._stop.set()
        if self._worker is not None:
            self._worker.join(timeout=8)
        with self._lock:
            self._video_status.update(ready=False, error="terminal capture stopped")


class WindowCapture(SupervisedCapture):
    def __init__(self, window_title: str, fps: int, width: int, quality: int, **options):
        if sys.platform != "win32":
            raise RuntimeError("real game capture requires Windows; use --mock for synthetic frames")
        super().__init__(fps, width, quality, window_title=window_title, **options)


class MockCapture(SupervisedCapture):
    """模拟图像也使用相同隔离、采样和硬件编码链路；只把游戏输入替换为内存后端。"""

    def __init__(self, fps: int, width: int, quality: int, **options):
        super().__init__(fps, width, quality, mock=True, **options)
