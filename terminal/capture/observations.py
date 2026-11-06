"""AI 观察采样：独立于视频帧率，在有界工作槽中按需编码 JPEG。"""

import threading
import time


class ObservationSampler:
    """一张待编码图像可被新图像覆盖；模型不会被迫消费累积的历史观察。"""

    def __init__(self, frames, *, fps: int, quality: int, on_error) -> None:
        self.frames = frames
        self.fps = fps
        self.quality = quality
        self.on_error = on_error
        self._next_sample = 0.0
        self._lock = threading.Lock()
        self._latest = None
        self._available = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="ai-observation-encoder")
        self._thread.start()

    def submit(self, pixels, captured_at: float) -> None:
        now = time.monotonic()
        if now < self._next_sample:
            return
        self._next_sample = now + 1 / self.fps if not self._next_sample else max(self._next_sample + 1 / self.fps, now)
        with self._lock:
            # pixels 已在采集回调中复制，SDK 释放原生映射不会使该引用失效。
            self._latest = (pixels, captured_at)
        self._available.set()

    def _run(self) -> None:
        import cv2

        while not self._stop.is_set():
            if not self._available.wait(timeout=0.2):
                continue
            self._available.clear()
            with self._lock:
                item = self._latest
                self._latest = None
            if item is None:
                continue
            pixels, captured_at = item
            try:
                ok, encoded = cv2.imencode(".jpg", pixels, [cv2.IMWRITE_JPEG_QUALITY, self.quality])
                if not ok:
                    raise RuntimeError("JPEG observation encoding failed")
                height, width = pixels.shape[:2]
                self.frames.publish(encoded.tobytes(), captured_at, width, height)
            except Exception as exc:
                self.on_error(f"observation encoding failed: {exc}")

    def close(self) -> None:
        self._stop.set()
        self._available.set()
        self._thread.join(timeout=2)
