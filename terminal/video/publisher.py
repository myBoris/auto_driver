"""硬件 H.264 发布：原始帧只保留最新一张，独立写入线程负责 FFmpeg 和 RTSP。"""

from __future__ import annotations

from collections import deque
import re
import subprocess
import threading
import time
from urllib.parse import urlparse

from .hardware import bitrate_bits, encoder_options, resolve_ffmpeg, select_hardware_encoder, subprocess_flags
from .processes import close_encoder_job, own_encoder_process


class HardwareVideoPublisher:
    """实际编码/连接成功才 ready；采集回调只替换待处理槽，不等待编码器或网络。"""
    def __init__(self, publish_url: str, *, fps: int, bitrate: str = "8M",
                 encoder: str = "auto", ffmpeg_binary: str | None = None) -> None:
        parsed = urlparse(publish_url)
        if parsed.scheme not in {"rtsp", "rtsps"} or not parsed.hostname:
            raise ValueError("video publish URL must be RTSP")
        self.publish_url = publish_url
        self.fps = fps
        self.bitrate = bitrate
        self._bitrate_bits = bitrate_bits(bitrate)
        self.requested_encoder = encoder
        self.ffmpeg_binary = ffmpeg_binary
        self._lock = threading.Lock()
        self._process_lock = threading.RLock()
        self._available = threading.Event()
        self._stop = threading.Event()
        self._latest = None
        self._process: subprocess.Popen | None = None
        self._process_job = None
        self._thread: threading.Thread | None = None
        self._watchdog_thread: threading.Thread | None = None
        self._last_submit_at = 0.0
        self._last_output_at = 0.0
        self._output_frames = 0
        self._process_started_at = 0.0
        self._status = {"ready": False, "error": "waiting for raw video frames", "encoder": None,
                        "codec": "H264", "hardware": True, "fps": fps, "bitrate": bitrate,
                        "frames": 0}

    def _safe_error(self, value: str) -> str:
        # FFmpeg 会把输出 URL 写到 stderr，必须同时覆盖完整地址和任意 RTSP userinfo。
        return re.sub(r"(rtsps?://)[^@\s]+@", r"\1***@", value.replace(self.publish_url, "<video-publisher>"))[:1000]

    def _set_status(self, **fields) -> None:
        with self._lock:
            self._status.update(fields)

    def status(self) -> dict:
        with self._lock:
            result = dict(self._status)
        process = self._process
        result["running"] = process is not None and process.poll() is None and not self._stop.is_set()
        return result

    def submit(self, pixels, captured_at: float) -> None:
        # 调用者提供拥有内存的连续 BGR 缓冲，绝不是 SDK 回调结束后会失效的映射。
        height, width = pixels.shape[:2]
        frame = (pixels.tobytes(), width, height, captured_at)
        with self._lock:
            self._latest = frame
            self._last_submit_at = time.monotonic()
        self._available.set()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="hardware-video-writer")
        self._thread.start()
        self._watchdog_thread = threading.Thread(target=self._watchdog, daemon=True, name="hardware-video-progress-watchdog")
        self._watchdog_thread.start()

    def _command(self, binary: str, encoder: str, width: int, height: int) -> list[str]:
        return [binary, "-hide_banner", "-loglevel", "warning", "-nostats", "-stats_period", "0.25",
                "-progress", "pipe:1", "-f", "rawvideo", "-pixel_format", "bgr24",
                "-video_size", f"{width}x{height}", "-framerate", str(self.fps), "-use_wallclock_as_timestamps", "1", "-i", "pipe:0",
                "-an", "-c:v", encoder, *encoder_options(encoder), "-profile:v", "baseline",
                "-pix_fmt", "yuv420p", "-bf", "0", "-g", str(self.fps),
                "-b:v", str(self._bitrate_bits), "-maxrate", str(self._bitrate_bits),
                "-bufsize", str(max(100000, self._bitrate_bits // 4)),
                "-fps_mode", "passthrough", "-f", "rtsp", "-rtsp_transport", "tcp", "-muxdelay", "0", self.publish_url]

    def _read_progress(self, process) -> None:
        for raw_line in self._pipe_lines(process.stdout):
            if self._stop.is_set() or process is not self._process:
                return
            line = raw_line.decode("ascii", errors="replace").strip()
            if line.startswith("frame="):
                try:
                    frames = int(line.partition("=")[2])
                except ValueError:
                    continue
                if frames > 0:
                    with self._lock:
                        if frames > self._output_frames:
                            self._output_frames = frames
                            self._last_output_at = time.monotonic()
                    self._set_status(ready=True, error=None, frames=frames)

    def _read_errors(self, process, errors) -> None:
        for raw_line in self._pipe_lines(process.stderr):
            errors.append(self._safe_error(raw_line.decode("utf-8", errors="replace").strip()))

    @staticmethod
    def _pipe_lines(stream):
        try:
            yield from iter(stream.readline, b"")
        except (OSError, ValueError):
            # 清理线程可能关闭阻塞中的管道；这是正常关闭路径，不输出线程异常。
            return

    def _stop_process(self, expected=None) -> None:
        # close() 与写入线程 finally 都可能调用清理；锁与先摘除引用保证只回收一次。
        with self._process_lock:
            process = self._process
            if expected is not None and process is not expected:
                return
            self._process = None
            job = self._process_job
            self._process_job = None
        if process is None:
            return
        # 关闭 Job 同时回收 FFmpeg 自己可能创建的后代；不会影响终端的其他进程。
        close_encoder_job(job)
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    # 默认异常包含完整命令行；只留下不含发布凭据的通用错误。
                    self._set_status(ready=False, error="hardware publisher did not exit after termination")
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    def _check_stalled(self, now=None) -> bool:
        """输入仍活跃但输出不前进时回收编码器；独立于可能卡住的 stdin 写线程。"""
        if self._stop.is_set():
            return False
        now = time.monotonic() if now is None else now
        with self._process_lock:
            process = self._process
            started = self._process_started_at
        if process is None or process.poll() is not None:
            return False
        with self._lock:
            last_submit = self._last_submit_at
            last_output = self._last_output_at
        source_active = now - last_submit < max(2.0, 3 / self.fps)
        limit = 8.0 if not last_output else 5.0
        if source_active and now - (last_output or started) >= limit:
            self._set_status(ready=False, error="hardware publisher output stalled; reconnecting")
            self._stop_process(expected=process)
            return True
        return False

    def _watchdog(self) -> None:
        while not self._stop.wait(0.25):
            self._check_stalled()

    def _run(self) -> None:
        try:
            binary = resolve_ffmpeg(self.ffmpeg_binary)
            self._set_status(error="probing H.264 hardware encoder")
            encoder = select_hardware_encoder(binary, self.requested_encoder)
            self._set_status(encoder=encoder, error="waiting for raw video frames")
        except Exception as exc:
            self._set_status(ready=False, error=self._safe_error(str(exc)))
            return
        while not self._stop.is_set():
            if not self._available.wait(timeout=0.2):
                continue
            errors = deque(maxlen=8)
            try:
                with self._lock:
                    first = self._latest
                if first is None:
                    continue
                if self._stop.is_set():
                    break
                _, width, height, _ = first
                process = subprocess.Popen(self._command(binary, encoder, width, height),
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           bufsize=0, **subprocess_flags())
                with self._process_lock:
                    self._process = process
                    self._process_job = own_encoder_process(process)
                    self._process_started_at = time.monotonic()
                with self._lock:
                    self._last_output_at = 0.0
                    self._output_frames = 0
                self._set_status(ready=False, error="connecting H.264 hardware publisher", frames=0)
                threading.Thread(target=self._read_progress, args=(process,), daemon=True).start()
                threading.Thread(target=self._read_errors, args=(process, errors), daemon=True).start()
                previous_timestamp = None
                while not self._stop.is_set():
                    if process.poll() is not None:
                        raise RuntimeError("hardware publisher exited: " + ("; ".join(errors) or str(process.returncode)))
                    self._available.wait(timeout=0.2)
                    self._available.clear()
                    with self._lock:
                        frame = self._latest
                    if frame is None or frame[3] == previous_timestamp:
                        continue
                    pixels, current_width, current_height, captured_at = frame
                    if (current_width, current_height) != (width, height):
                        break
                    view = memoryview(pixels)
                    while view and not self._stop.is_set():
                        written = process.stdin.write(view)
                        if not written:
                            raise BrokenPipeError("hardware video input closed")
                        view = view[written:]
                    previous_timestamp = captured_at
            except Exception as exc:
                detail = "; ".join(errors)
                self._set_status(ready=False, error=self._safe_error(detail or str(exc)))
            finally:
                self._stop_process()
            self._stop.wait(2)

    def close(self) -> None:
        self._stop.set()
        self._available.set()
        self._stop_process()
        if self._thread is not None:
            self._thread.join(timeout=3)
        if self._watchdog_thread is not None:
            self._watchdog_thread.join(timeout=1)
        self._set_status(ready=False, error="video publisher stopped")
