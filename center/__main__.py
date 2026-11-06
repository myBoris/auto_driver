import argparse
import logging
import sys
from pathlib import Path

from aiohttp import web
from dotenv import load_dotenv

from .app import create_app
from .config import CenterConfig


def main():
    load_dotenv(Path(__file__).resolve().parent / ".env", override=False)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="自动驾驶服务中心")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--token")
    parser.add_argument("--discovery-port", type=int)
    parser.add_argument("--advertise-url", help="广播返回的外部 HTTP URL（端口映射或反向代理时设置）")
    parser.add_argument("--no-discovery", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        config = CenterConfig.from_env(
            host=args.host, port=args.port, token=args.token,
            discovery_port=args.discovery_port, advertise_url=args.advertise_url,
            enable_discovery=False if args.no_discovery else None,
        )
    except ValueError as exc:
        parser.error(str(exc))
    web.run_app(create_app(config), host=config.host, port=config.port)


if __name__ == "__main__":
    main()
