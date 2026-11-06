"""配置层：读取终端自身 .env 并校验 CLI；解析参数时不初始化任何硬件。

视频 FPS 和 AI 观察 FPS 独立，码率以 bit/s 表示并支持 K/M 后缀。
CLI 优先于环境，已有进程环境优先于 .env，便于同一终端临时运行不同配置。
"""

import argparse
import os
from pathlib import Path
import re
import socket

from dotenv import load_dotenv
from ..network.discovery import validate_center_url

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Forza Horizon 5 capture and virtual Xbox 360 terminal")
    parser.add_argument("--token", default=os.environ.get("CENTER_TOKEN"), help="shared center token; defaults to CENTER_TOKEN")
    parser.add_argument("--center", default=os.environ.get("CENTER_URL"), help="center URL; omit to discover on LAN")
    default_id = re.sub(r"[^A-Za-z0-9_.-]", "-", socket.gethostname())[:64] or "game-pc"
    parser.add_argument("--terminal-id", default=os.environ.get("TERMINAL_ID", default_id))
    parser.add_argument("--name", default=socket.gethostname())
    parser.add_argument("--window-title", default="Forza Horizon 5", help="case-insensitive game window title substring")
    parser.add_argument("--fps", type=int, default=os.environ.get("TERMINAL_FPS", "60"),
                        help="video target FPS (1..120); defaults to TERMINAL_FPS or 60")
    parser.add_argument("--observation-fps", type=int, default=os.environ.get("TERMINAL_OBSERVATION_FPS", "10"),
                        help="JPEG training observation FPS (1..120); independent of video FPS")
    parser.add_argument("--no-video", action="store_false", dest="video_enabled",
                        default=os.environ.get("TERMINAL_VIDEO_ENABLED", "true").lower() not in {"0", "false", "no", "off"},
                        help="disable hardware H.264 publishing; retain JPEG observations")
    parser.add_argument("--video-bitrate", default=os.environ.get("TERMINAL_VIDEO_BITRATE", "8M"))
    parser.add_argument("--video-encoder", choices=("auto", "h264_nvenc", "h264_qsv", "h264_amf"),
                        default=os.environ.get("TERMINAL_VIDEO_ENCODER", "auto"))
    parser.add_argument("--ffmpeg-binary", default=os.environ.get("FFMPEG_BINARY"),
                        help="optional FFmpeg executable override; otherwise use bundled FFmpeg")
    parser.add_argument("--width", type=int, default=1280, help="maximum uploaded frame width")
    parser.add_argument("--jpeg-quality", type=int, default=75)
    parser.add_argument("--discovery-port", type=int, default=9999)
    parser.add_argument("--discovery-address", default="255.255.255.255")
    parser.add_argument("--mock", action="store_true", help="synthetic video and gamepad; no game input")
    parser.add_argument("--log-file", type=Path)
    parser.add_argument("--stop-file", type=Path, help="exit gracefully when this file appears")
    return parser


def parse_args(argv=None):
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.token or not args.token.strip() or "\n" in args.token or "\r" in args.token:
        parser.error("set CENTER_TOKEN or --token to the center's shared token")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", args.terminal_id):
        parser.error("terminal-id must contain 1..64 ASCII letters, digits, dots, underscores, or hyphens and start with a letter or digit")
    if not 1 <= len(args.name) <= 128:
        parser.error("name must be 1..128 characters")
    if not args.window_title:
        parser.error("window-title must not be empty")
    if not 1 <= args.fps <= 120 or not 160 <= args.width <= 3840 or not 1 <= args.jpeg_quality <= 95:
        parser.error("fps must be 1..120, width 160..3840, jpeg-quality 1..95")
    if not 1 <= args.observation_fps <= 120:
        parser.error("observation-fps must be 1..120")
    if not re.fullmatch(r"[1-9][0-9]*(?:[kKmM])?", args.video_bitrate):
        parser.error("video-bitrate must be a positive integer with optional k/M suffix, e.g. 8M")
    if args.video_encoder not in {"auto", "h264_nvenc", "h264_qsv", "h264_amf"}:
        parser.error("video-encoder must be auto or a supported hardware H.264 encoder")
    if not 1 <= args.discovery_port <= 65535:
        parser.error("discovery-port must be 1..65535")
    if args.center:
        try:
            args.center = validate_center_url(args.center)
        except ValueError as exc:
            parser.error(str(exc))
    return args
