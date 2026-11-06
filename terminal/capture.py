"""Keep one recent JPEG in memory; native capture never queues work on asyncio."""

from __future__ import annotations

import io
import logging
import multiprocessing
import os
import queue
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

LOG = logging.getLogger(__name__)
MAX_FRAME_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class CapturedFrame:
    sequence: int
    captured_at: float
    jpeg: bytes
    width: int
    height: int

    def packet(self) -> bytes:
        return b"ADF1" + struct.pack("!d", self.captured_at) + self.jpeg


class LatestCapture:
    def __init__(self, fps: int, width: int, quality: int) -> None:
        self.fps = fps
        self.width = width
        self.quality = quality
        self._lock = threading.Lock()
        self._latest: CapturedFrame | None = None
        self._error: str | None = "waiting for first frame"
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._frame_listeners: set[Callable[[], None]] = set()

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


class MockCapture(LatestCapture):
    def _run(self) -> None:
        from PIL import Image, ImageDraw

        width = min(self.width, 1280)
        height = max(1, width * 9 // 16)
        while not self._stop.is_set():
            captured_at = time.time()
            image = Image.new("RGB", (width, height), (20, 30, 45))
            draw = ImageDraw.Draw(image)
            x = int(captured_at * 80) % width
            draw.rectangle((x, height // 3, min(width, x + 80), 2 * height // 3), fill=(40, 180, 160))
            draw.text((20, 20), f"AUTO DRIVER / MOCK TERMINAL\n{captured_at:.3f}", fill="white")
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=self.quality)
            self._publish(output.getvalue(), captured_at, width, height)
            self._stop.wait(1 / self.fps)


class _SharedFrameSlot:
    """A single JPEG slot shared with the disposable native capture process."""

    def __init__(self, context) -> None:
        self.buffer = context.RawArray("B", MAX_FRAME_BYTES)
        # sequence, JPEG length, capture timestamp, width, height
        self.metadata = context.RawArray("d", 5)
        self.lock = context.Lock()

    def publish(self, jpeg: bytes, captured_at: float, width: int, height: int) -> None:
        if len(jpeg) + 12 > MAX_FRAME_BYTES:
            raise ValueError("JPEG exceeds 8 MiB; lower --width or --jpeg-quality")
        with self.lock:
            memoryview(self.buffer).cast("B")[:len(jpeg)] = jpeg
            self.metadata[1:] = (len(jpeg), captured_at, width, height)
            self.metadata[0] += 1

    def read_after(self, sequence: int):
        # A crashed SDK must not leave the supervisor waiting on its abandoned lock.
        if not self.lock.acquire(timeout=0.02):
            return None
        try:
            current = int(self.metadata[0])
            if current <= sequence:
                return None
            length = int(self.metadata[1])
            return (current, memoryview(self.buffer).cast("B")[:length].tobytes(),
                    self.metadata[2], int(self.metadata[3]), int(self.metadata[4]))
        finally:
            self.lock.release()


def _send_capture_status(channel, **message) -> None:
    message["timestamp"] = time.time()
    try:
        channel.put_nowait(message)
    except queue.Full:
        try:
            channel.get_nowait()
        except queue.Empty:
            pass
        try:
            channel.put_nowait(message)
        except queue.Full:
            pass


def _find_window(window_title: str) -> int | None:
    """Resolve a visible HWND before invoking the native capture library."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    matches: list[tuple[bool, int]] = []
    desired = window_title.casefold()

    @callback_type
    def visit(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length:
            title_buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, title_buffer, length + 1)
            title = title_buffer.value.casefold()
            if desired in title:
                # Steam's streaming launcher is a separate window, not the local game.
                if "[streaming]" in title and "[streaming]" not in desired:
                    return True
                matches.append((title != desired, int(hwnd)))
        return True

    user32.EnumWindows(visit, 0)
    return min(matches)[1] if matches else None


class _NativeWindowCapture(LatestCapture):
    """Runs exclusively inside the supervised child, never beside the gamepad."""

    def __init__(self, config: dict, frames: _SharedFrameSlot, status, stop) -> None:
        super().__init__(config["fps"], config["width"], config["quality"])
        self.window_title = config["window_title"]
        self._shared_frames = frames
        self._status_channel = status
        self._stop = stop

    def _publish(self, jpeg: bytes, captured_at: float, width: int, height: int) -> None:
        self._shared_frames.publish(jpeg, captured_at, width, height)

    def _set_error(self, message: str) -> None:
        _send_capture_status(self._status_channel, error=message)

    def _run(self) -> None:
        try:
            import cv2
            from windows_capture import WindowsCapture
        except Exception as exc:
            self._set_error(f"capture dependency failed: {exc}")
            return
        while not self._stop.is_set():
            control = None
            session_closed = threading.Event()
            try:
                hwnd = _find_window(self.window_title)
                if hwnd is None:
                    self._set_error(f"waiting for visible window {self.window_title!r}")
                    self._stop.wait(2)
                    continue
                self._set_error("opening native window capture")
                capture = WindowsCapture(cursor_capture=False, draw_border=False,
                                         minimum_update_interval=max(1, 1000 // self.fps),
                                         window_hwnd=hwnd)

                @capture.event
                def on_frame_arrived(frame, capture_control) -> None:
                    if self._stop.is_set():
                        capture_control.stop()
                        return
                    # The native minimum_update_interval already limits capture rate.
                    # A second elapsed-time gate drops valid frames near the interval.
                    captured_at = time.time()
                    try:
                        pixels = frame.frame_buffer[:, :, :3]
                        height, width = pixels.shape[:2]
                        if width > self.width:
                            height = max(1, round(height * self.width / width))
                            width = self.width
                            pixels = cv2.resize(pixels, (width, height), interpolation=cv2.INTER_AREA)
                        ok, encoded = cv2.imencode(".jpg", pixels, [cv2.IMWRITE_JPEG_QUALITY, self.quality])
                        if not ok:
                            raise RuntimeError("JPEG encoding failed")
                        self._publish(encoded.tobytes(), captured_at, width, height)
                    except Exception as exc:
                        self._set_error(f"frame capture failed: {exc}")

                @capture.event
                def on_closed() -> None:
                    session_closed.set()
                    self._set_error("game window closed; waiting to reconnect")

                control = capture.start_free_threaded()
                while not self._stop.wait(0.25):
                    if session_closed.is_set() or control.is_finished():
                        break
                    _send_capture_status(self._status_channel, alive=True)
            except Exception as exc:
                self._set_error(f"window capture unavailable: {exc}")
            finally:
                if control is not None:
                    try:
                        control.stop()
                        control.wait()
                    except Exception as exc:
                        self._set_error(f"failed stopping native capture: {exc}")
            self._stop.wait(2)


def _capture_process_main(config: dict, frames: _SharedFrameSlot, status, stop) -> None:
    if os.name == "nt":
        import ctypes
        # A native SDK failure is handled by the supervisor, without a modal crash box.
        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
    # Do not block process shutdown waiting for the disposable status queue feeder.
    status.cancel_join_thread()
    _send_capture_status(status, error="initializing capture worker")
    _NativeWindowCapture(config, frames, status, stop)._run()


def _stop_capture_process(process, stop) -> None:
    stop.set()
    process.join(timeout=0.5)
    if process.is_alive():
        # Windows virtualenv launchers may own a second interpreter process.
        if os.name == "nt":
            try:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=2, creationflags=subprocess.CREATE_NO_WINDOW)
            except (OSError, subprocess.TimeoutExpired):
                process.terminate()
        else:
            process.terminate()
        process.join(timeout=1)
    if process.is_alive():
        process.kill()
        process.join(timeout=1)
    process.close()


class WindowCapture(LatestCapture):
    def __init__(self, window_title: str, fps: int, width: int, quality: int) -> None:
        if sys.platform != "win32":
            raise RuntimeError("real game window capture requires Windows; use --mock for tests")
        super().__init__(fps, width, quality)
        self.window_title = window_title
        self._worker_target = _capture_process_main
        self._worker_timeout = 30.0
        self._restart_delay = 2.0

    def _run(self) -> None:
        context = multiprocessing.get_context("spawn")
        # Read often enough for 120 FPS without imposing a second frame-rate cap.
        poll_interval = min(0.005, 0.5 / self.fps)
        failures = 0
        while not self._stop.is_set():
            frames = _SharedFrameSlot(context)
            channel = context.Queue(maxsize=1)
            stop = context.Event()
            process = context.Process(target=self._worker_target,
                                      args=({"window_title": self.window_title, "fps": self.fps,
                                             "width": self.width, "quality": self.quality},
                                            frames, channel, stop),
                                      daemon=True, name="native-window-capture")
            try:
                process.start()
                LOG.info("Native capture worker started (PID %s)", process.pid)
                sequence = 0
                last_activity = time.monotonic()
                previous_error = None
                while not self._stop.is_set():
                    # Event.wait() rounds short timeouts to ~15.6 ms on Windows.
                    # Python's high-resolution sleep keeps 120 FPS forwarding viable.
                    time.sleep(poll_interval)
                    if self._stop.is_set():
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
                    if message is not None:
                        last_activity = time.monotonic()
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
                LOG.warning("Native capture unavailable: %s", exc)
            finally:
                if process.pid is not None:
                    _stop_capture_process(process, stop)
                channel.close()
                channel.cancel_join_thread()
            self._stop.wait(min(10, self._restart_delay * max(1, failures)))

    def close(self) -> None:
        self._stop.set()
        if self._worker is not None:
            self._worker.join(timeout=6)
