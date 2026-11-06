"""硬件选择：实际打开编码器并编码测试帧，不能凭 FFmpeg 支持列表宣称硬件可用。"""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess

ENCODERS = ("h264_nvenc", "h264_qsv", "h264_amf")


class HardwareEncoderUnavailable(RuntimeError):
    pass


def subprocess_flags() -> dict:
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def resolve_ffmpeg(override: str | None = None) -> str:
    import imageio_ffmpeg

    binary = override or os.environ.get("FFMPEG_BINARY") or imageio_ffmpeg.get_ffmpeg_exe()
    if not Path(binary).is_file():
        raise HardwareEncoderUnavailable("FFMPEG_BINARY does not point to an executable file")
    return str(binary)


def encoder_options(encoder: str) -> list[str]:
    if encoder == "h264_nvenc":
        return ["-preset", "p1", "-tune", "ull", "-zerolatency", "1"]
    if encoder == "h264_qsv":
        return ["-preset", "veryfast", "-look_ahead", "0", "-async_depth", "1"]
    if encoder == "h264_amf":
        return ["-quality", "speed", "-usage", "ultralowlatency"]
    raise ValueError("only h264_nvenc, h264_qsv, and h264_amf are supported; software encoding is disabled")


def bitrate_bits(value: str) -> int:
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([kKmMgG]?)", str(value))
    if not match:
        raise ValueError("video bitrate must be a positive number, optionally suffixed K, M, or G")
    bits = int(float(match[1]) * {"": 1, "k": 1000, "m": 1000000, "g": 1000000000}[match[2].lower()])
    if not 100000 <= bits <= 1000000000:
        raise ValueError("video bitrate must be between 100K and 1G")
    return bits


def select_hardware_encoder(binary: str, requested: str = "auto") -> str:
    """依次试 NVENC/QSV/AMF；全部失败显式报错，绝不改用 libx264 等软件实现。"""
    if requested != "auto" and requested not in ENCODERS:
        raise ValueError("video encoder must be auto, h264_nvenc, h264_qsv, or h264_amf")
    failures = []
    for encoder in ENCODERS if requested == "auto" else (requested,):
        command = [binary, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                   "color=c=black:s=640x360:r=30", "-frames:v", "3", "-an", "-c:v", encoder,
                   *encoder_options(encoder), "-bf", "0", "-profile:v", "baseline",
                   "-pix_fmt", "yuv420p", "-f", "null", "-"]
        try:
            result = subprocess.run(command, capture_output=True, timeout=15, **subprocess_flags())
            if result.returncode == 0:
                return encoder
            detail = result.stderr.decode("utf-8", errors="replace")
            first_line = next((line for line in detail.splitlines() if line.strip()), "encoder open failed")
            failures.append(f"{encoder}: {first_line[:220]}")
        except (OSError, subprocess.TimeoutExpired) as exc:
            failures.append(f"{encoder}: {type(exc).__name__}")
    raise HardwareEncoderUnavailable("No working H.264 hardware encoder. " + "; ".join(failures))
