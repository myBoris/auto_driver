"""采集源公共接口：只缓存最新 AI 观察 JPEG，并在发布后通知网络层。"""

from __future__ import annotations

import struct
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

MAX_FRAME_BYTES = 8 * 1024 * 1024

@dataclass(frozen=True)
class CapturedFrame:
    """训练观察帧：采集时间在终端产生，不能用上传时间替代其因果顺序。"""
    sequence: int
    captured_at: float
    jpeg: bytes
    width: int
    height: int

    def packet(self) -> bytes:
        return b"ADF1" + struct.pack("!d", self.captured_at) + self.jpeg


class LatestCapture:
    """只保存最新 JPEG；视频推流状态独立保存，网络层无需知道 SDK/编码器细节。"""

    def __init__(self, fps: int, width: int, quality: int, *, observation_fps: int = 10,
                 video_enabled: bool = True, video_bitrate: str = "8M",
                 video_encoder: str = "auto", ffmpeg_binary: str | None = None) -> None:
        self.fps = fps
        self.width = width
        self.quality = quality
        self.observation_fps = observation_fps
        self.video_enabled = video_enabled
        self.video_bitrate = video_bitrate
        self.video_encoder = video_encoder
        self.ffmpeg_binary = ffmpeg_binary
        self._lock = threading.Lock()
        self._latest: CapturedFrame | None = None
        self._error: str | None = "waiting for first frame"
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._frame_listeners: set[Callable[[], None]] = set()
        self._video_config: dict = {"enabled": False}
        self._config_generation = 0
        self._video_status = {"ready": False, "error": "waiting for center video configuration" if video_enabled else None,
                              "encoder": None, "codec": "H264", "hardware": True,
                              "enabled": video_enabled, "fps": fps, "bitrate": video_bitrate}

    def configure_video(self, video: dict) -> None:
        """中心每次握手下发地址；变化时递增代次，由采集管理线程停止旧发布进程。

        地址可能含发布口令，因此只在内存配置中保存，绝不能进入状态或日志。
        CLI --no-video 的优先级高于中心配置，适用于纯观察或无硬件环境。
        """
        enabled = self.video_enabled and video.get("enabled") is True
        publish_url = video.get("publish_url", "") if enabled else ""
        if not isinstance(publish_url, str) or not publish_url:
            enabled = False
        config = {"enabled": enabled, "publish_url": publish_url if enabled else ""}
        with self._lock:
            if config != self._video_config:
                self._video_config = config
                self._config_generation += 1
            self._video_status.update(ready=False, enabled=enabled,
                                      error="starting hardware video publisher" if enabled else None)

    def video_status(self) -> dict:
        with self._lock:
            return dict(self._video_status)

    def _publish(self, jpeg: bytes, captured_at: float, width: int, height: int) -> None:
        if len(jpeg) + 12 > MAX_FRAME_BYTES:
            raise ValueError("JPEG exceeds 8 MiB; lower --width or --jpeg-quality")
        with self._lock:
            sequence = 1 if self._latest is None else self._latest.sequence + 1
            self._latest = CapturedFrame(sequence, captured_at, jpeg, width, height)
            self._error = None
            listeners = tuple(self._frame_listeners)
        for listener in listeners:
            listener()

    def add_frame_listener(self, listener: Callable[[], None]) -> None:
        with self._lock:
            self._frame_listeners.add(listener)

    def remove_frame_listener(self, listener: Callable[[], None]) -> None:
        with self._lock:
            self._frame_listeners.discard(listener)

    def _set_error(self, message: str) -> None:
        with self._lock:
            self._error = message

    def latest(self) -> CapturedFrame | None:
        with self._lock:
            return self._latest

    def status(self) -> dict[str, Any]:
        with self._lock:
            frame = self._latest
            age = None if frame is None else max(0.0, time.time() - frame.captured_at)
            return {"ready": frame is not None and self._error is None and age < 3,
                    "width": frame.width if frame else 0, "height": frame.height if frame else 0,
                    "fps": self.fps, "frame_sequence": frame.sequence if frame else 0,
                    "observation_fps": self.observation_fps,
                    "frame_age_seconds": age, "error": self._error}

    def start(self) -> None:
        if self._worker is not None:
            raise RuntimeError("capture already started")
        self._worker = threading.Thread(target=self._run, daemon=True, name="game-capture")
        self._worker.start()

    def _run(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        self._stop.set()
        if self._worker is not None:
            self._worker.join(timeout=4)
