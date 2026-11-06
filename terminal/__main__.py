"""Run with python -m terminal; use start.ps1 for a hidden Windows process."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path
import re
import socket
import sys

from dotenv import load_dotenv

from .capture import MockCapture, WindowCapture
from .client import TerminalClient, TerminalConfig
from .control import Controller, MockGamepad, Xbox360Gamepad
from .discovery import validate_center_url


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Forza Horizon 5 capture and virtual Xbox 360 terminal")
    parser.add_argument("--token", default=os.environ.get("CENTER_TOKEN"), help="shared center token; defaults to CENTER_TOKEN")
    parser.add_argument("--center", default=os.environ.get("CENTER_URL"), help="center URL; omit to discover on LAN")
    default_id = re.sub(r"[^A-Za-z0-9_.-]", "-", socket.gethostname())[:64] or "game-pc"
    parser.add_argument("--terminal-id", default=os.environ.get("TERMINAL_ID", default_id))
    parser.add_argument("--name", default=socket.gethostname())
    parser.add_argument("--window-title", default="Forza Horizon 5", help="case-insensitive game window title substring")
    parser.add_argument("--fps", type=int, default=os.environ.get("TERMINAL_FPS", "30"),
                        help="capture FPS; defaults to TERMINAL_FPS or 30")
    parser.add_argument("--width", type=int, default=1280, help="maximum uploaded frame width")
    parser.add_argument("--jpeg-quality", type=int, default=75)
    parser.add_argument("--discovery-port", type=int, default=9999)
    parser.add_argument("--discovery-address", default="255.255.255.255")
    parser.add_argument("--mock", action="store_true", help="synthetic video and gamepad; no game input")
    parser.add_argument("--log-file", type=Path)
    parser.add_argument("--stop-file", type=Path, help="exit gracefully when this file appears")
    return parser


def main() -> None:
    load_dotenv(Path(__file__).with_name(".env"), override=False)
    parser = build_parser()
    args = parser.parse_args()
    if not args.token or not args.token.strip() or "\n" in args.token or "\r" in args.token:
        parser.error("set CENTER_TOKEN or --token to the center's shared token")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", args.terminal_id):
        parser.error("terminal-id must contain 1..64 ASCII letters, digits, dots, underscores, or hyphens and start with a letter or digit")
    if not 1 <= len(args.name) <= 128:
        parser.error("name must be 1..128 characters")
    if not args.window_title:
        parser.error("window-title must not be empty")
    if not 1 <= args.fps <= 60 or not 160 <= args.width <= 3840 or not 1 <= args.jpeg_quality <= 95:
        parser.error("fps must be 1..60, width 160..3840, jpeg-quality 1..95")
    if not 1 <= args.discovery_port <= 65535:
        parser.error("discovery-port must be 1..65535")
    if args.center:
        try:
            args.center = validate_center_url(args.center)
        except ValueError as exc:
            parser.error(str(exc))
    handlers: list[logging.Handler] = []
    if args.log_file:
        args.log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(args.log_file, encoding="utf-8"))
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        backend = MockGamepad() if args.mock else Xbox360Gamepad()
        controller = Controller(backend)
        capture = (MockCapture(args.fps, args.width, args.jpeg_quality) if args.mock else
                   WindowCapture(args.window_title, args.fps, args.width, args.jpeg_quality))
        config = TerminalConfig(token=args.token, terminal_id=args.terminal_id, name=args.name,
                                center=args.center, discovery_port=args.discovery_port,
                                discovery_address=args.discovery_address, stop_file=args.stop_file, mock=args.mock)
        asyncio.run(TerminalClient(config, capture, controller).run())
    except KeyboardInterrupt:
        pass
    except Exception:
        logging.exception("Terminal startup or execution failed")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
