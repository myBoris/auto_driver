"""运行编排层：按配置组装采集、控制和网络服务，统一管理启动与异常退出。"""

import asyncio
import logging
import sys

from ..capture import MockCapture, WindowCapture
from ..config import parse_args
from ..config.connection import TerminalConfig
from ..control import Controller, MockGamepad, Xbox360Gamepad
from ..network.client import TerminalClient


def main():
    args = parse_args()
    handlers: list[logging.Handler] = []
    if args.log_file:
        args.log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(args.log_file, encoding="utf-8"))
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    controller = None
    try:
        backend = MockGamepad() if args.mock else Xbox360Gamepad()
        controller = Controller(backend)
        capture_options = dict(observation_fps=args.observation_fps, video_enabled=args.video_enabled,
                               video_bitrate=args.video_bitrate, video_encoder=args.video_encoder,
                               ffmpeg_binary=args.ffmpeg_binary)
        capture = (MockCapture(args.fps, args.width, args.jpeg_quality, **capture_options) if args.mock else
                   WindowCapture(args.window_title, args.fps, args.width, args.jpeg_quality, **capture_options))
        config = TerminalConfig(token=args.token, terminal_id=args.terminal_id, name=args.name,
                                center=args.center, discovery_port=args.discovery_port,
                                discovery_address=args.discovery_address, stop_file=args.stop_file, mock=args.mock)
        asyncio.run(TerminalClient(config, capture, controller).run())
    except KeyboardInterrupt:
        pass
    except Exception:
        logging.exception("Terminal startup or execution failed")
        raise SystemExit(1)
    finally:
        # 网络服务正常退出也会 close；重复释放是幂等的，覆盖构造中途失败路径。
        if controller is not None:
            controller.close()
