"""采集子进程：原始帧分流到硬件视频发布和低频 AI 观察编码。

WGC/OpenCV/FFmpeg 都限定在本进程内；即使驱动崩溃或编码管道阻塞，父进程
的中心连接和手柄超时计时器仍继续工作。回调只复制像素并替换有界槽，不等待网络。
"""

from __future__ import annotations

import os
import threading
import time

from ..video import HardwareVideoPublisher
from .observations import ObservationSampler
from .shared import _send_capture_status
from .windows import _find_window


class FramePipeline:
    """共享的原始 BGR 分流器；两条输出分别限速，绝不逐视频帧压缩 JPEG。"""

    def __init__(self, config: dict, frames, channel, stop) -> None:
        self.config = config
        self.channel = channel
        self.stop = stop
        self.video = None
        self._last_status = 0.0
        self._next_video = 0.0
        video_config = config.get("video", {})
        if video_config.get("enabled"):
            self.video = HardwareVideoPublisher(
                video_config["publish_url"], fps=config["fps"], bitrate=config["video_bitrate"],
                encoder=config["video_encoder"], ffmpeg_binary=config.get("ffmpeg_binary"))
            self.video.start()
        self.observations = ObservationSampler(frames, fps=config["observation_fps"],
                                               quality=config["quality"], on_error=self.capture_error)

    def capture_error(self, error: str) -> None:
        _send_capture_status(self.channel, error=error, video=self.video_status())

    def video_status(self) -> dict:
        if self.video is not None:
            return {**self.video.status(), "enabled": True}
        return {"ready": False, "enabled": False, "error": None, "encoder": None,
                "codec": "H264", "hardware": True, "fps": self.config["fps"],
                "bitrate": self.config["video_bitrate"]}

    def submit(self, pixels, captured_at: float) -> None:
        import cv2
        import numpy as np

        height, width = pixels.shape[:2]
        target_width = min(width, self.config["width"])
        # H.264 yuv420p 要求偶数宽高；观察帧与视频保持相同画面比例。
        target_width -= target_width % 2
        target_height = max(2, round(height * target_width / width))
        target_height -= target_height % 2
        if (width, height) != (target_width, target_height):
            pixels = cv2.resize(pixels, (target_width, target_height), interpolation=cv2.INTER_AREA)
        else:
            # 即使 SDK 某版本返回已连续 BGR，也必须复制；后台观察线程会跨越回调生命周期。
            pixels = np.array(pixels, copy=True, order="C")
        if self.video is not None:
            now = time.monotonic()
            # 只有观察请求比视频更高频时才需额外分流限速，常规路径不重复限制 WGC。
            if self.config["observation_fps"] <= self.config["fps"] or now >= self._next_video:
                self.video.submit(pixels, captured_at)
                self._next_video = now + 1 / self.config["fps"]
        self.observations.submit(pixels, captured_at)
        self.heartbeat()

    def heartbeat(self, force=False) -> None:
        now = time.monotonic()
        if force or now - self._last_status >= 0.5:
            self._last_status = now
            _send_capture_status(self.channel, alive=True, video=self.video_status())

    def close(self) -> None:
        self.observations.close()
        if self.video is not None:
            self.video.close()


def _run_mock(config: dict, pipeline: FramePipeline, stop) -> None:
    """只替换图像来源；启用视频时仍走真实 NVENC/QSV/AMF 编码器，便于端到端验收。"""
    import cv2
    import numpy as np
    from PIL import Image, ImageDraw

    width = min(config["width"], 1280)
    width -= width % 2
    height = max(2, width * 9 // 16)
    height -= height % 2
    fps = max(config["fps"], config["observation_fps"])
    deadline = time.perf_counter()
    counter = 0
    while not stop.is_set():
        captured_at = time.time()
        counter += 1
        image = Image.new("RGB", (width, height), (20, 30, 45))
        draw = ImageDraw.Draw(image)
        x = int(captured_at * 160) % width
        draw.rectangle((x, height // 3, min(width, x + 120), 2 * height // 3), fill=(40, 180, 160))
        draw.text((20, 20), f"AUTO DRIVER / SYNTHETIC SOURCE\nframe={counter} time={captured_at:.3f}", fill="white")
        pipeline.submit(cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR), captured_at)
        deadline = max(deadline + 1 / fps, time.perf_counter())
        time.sleep(max(0, deadline - time.perf_counter()))


def _run_windows(config: dict, pipeline: FramePipeline, stop) -> None:
    from windows_capture import WindowsCapture

    while not stop.is_set():
        control = None
        closed = threading.Event()
        try:
            hwnd = _find_window(config["window_title"])
            if hwnd is None:
                pipeline.capture_error(f"waiting for visible window {config['window_title']!r}")
                stop.wait(2)
                continue
            pipeline.capture_error("opening native window capture")
            capture_fps = max(config["fps"], config["observation_fps"])
            capture = WindowsCapture(cursor_capture=False, draw_border=False,
                                     minimum_update_interval=max(1, 1000 // capture_fps), window_hwnd=hwnd)

            @capture.event
            def on_frame_arrived(frame, capture_control):
                if stop.is_set():
                    capture_control.stop()
                    return
                try:
                    # native 映射只在回调内有效；submit 在返回前生成拥有内存的 BGR 副本。
                    pipeline.submit(frame.frame_buffer[:, :, :3], time.time())
                except Exception as exc:
                    pipeline.capture_error(f"raw frame processing failed: {exc}")

            @capture.event
            def on_closed():
                closed.set()
                pipeline.capture_error("game window closed; waiting to reconnect")

            control = capture.start_free_threaded()
            while not stop.wait(0.25):
                if closed.is_set() or control.is_finished():
                    break
                pipeline.heartbeat()
        except Exception as exc:
            pipeline.capture_error(f"window capture unavailable: {exc}")
        finally:
            if control is not None:
                try:
                    control.stop()
                    control.wait()
                except Exception:
                    pass
        stop.wait(2)


def _capture_process_main(config: dict, frames, channel, stop) -> None:
    if os.name == "nt":
        import ctypes
        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
    channel.cancel_join_thread()
    _send_capture_status(channel, error="initializing capture worker")
    pipeline = None
    try:
        pipeline = FramePipeline(config, frames, channel, stop)
        if config.get("mock"):
            _run_mock(config, pipeline, stop)
        else:
            _run_windows(config, pipeline, stop)
    except Exception as exc:
        _send_capture_status(channel, error=f"capture worker failed: {exc}")
        raise
    finally:
        if pipeline is not None:
            pipeline.close()
