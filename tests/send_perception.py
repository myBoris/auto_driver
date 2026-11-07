"""持续发送随机道路和方向演示数据；仅依赖 Python 标准库。

在仓库根目录运行：python tests/send_perception.py
默认读取 center/.env，自动选择唯一在线终端，5 Hz 发送，Ctrl+C 停止。
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import random
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[1]


class NoRedirect(HTTPRedirectHandler):
    """不将配对口令随重定向转发到其他地址。"""

    def redirect_request(self, request, *args, **kwargs):
        return None


class APIError(RuntimeError):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(f"HTTP {status}: {message}")


class Client:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.opener = build_opener(NoRedirect())

    def request(self, method: str, path: str, data: dict | None = None):
        headers = {"Authorization": f"Bearer {self.token}"}
        body = None
        if data is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(data, allow_nan=False).encode("utf-8")
        request = Request(self.base_url + path, data=body, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=3) as response:
                return json.load(response)
        except HTTPError as error:
            try:
                message = json.loads(error.read(4096)).get("error", error.reason)
            except (ValueError, AttributeError):
                message = error.reason
            raise APIError(error.code, str(message).replace(self.token, "<redacted>")) from None


def read_env(path: Path) -> dict[str, str]:
    """读取本地配置中的简单 KEY=VALUE；不改变进程环境或输出口令。"""
    if not path.is_file():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if value.startswith(("'", '"')):
            end = value.find(value[0], 1)
            if end < 0:
                raise ValueError(f"配置项 {key.strip()} 的引号未闭合")
            value = value[1:end]
        else:
            value = value.split(" #", 1)[0].rstrip()
        values[key.strip()] = value
    return values


class RandomRoad:
    def __init__(self, seed: int | None):
        self.rng = random.Random(seed)
        self.steering = 0.0
        self.target = 0.0
        self.offset = 0.0
        self.next_turn = 0.0
        self.last_update = time.monotonic()

    def payload(self, terminal: dict) -> dict:
        now = time.monotonic()
        dt = max(0, now - self.last_update)
        self.last_update = now
        if now >= self.next_turn:
            self.target = self.rng.uniform(-.85, .85)
            self.offset = self.rng.uniform(-.035, .035)
            self.next_turn = now + self.rng.uniform(1.5, 3.0)
        self.steering += (self.target - self.steering) * (1 - math.exp(-dt * 3))
        centerline, left, right = [], [], []
        for index in range(12):
            t = index / 11
            x = .52 + self.offset + self.steering * .20 * t * t
            y = .64 - .17 * t
            half_width = .24 - .205 * t
            centerline.append([round(x, 5), round(y, 5)])
            left.append([round(x - half_width, 5), round(y, 5)])
            right.append([round(x + half_width, 5), round(y, 5)])
        capture = terminal.get("capture") or {}

        def dimension(name, fallback):
            value = terminal.get(name) or capture.get(name)
            return value if type(value) is int and 1 <= value <= 16384 else fallback

        return {
            "session_id": terminal["session_id"],
            "status": "ready",
            "image": {"width": dimension("width", 1280), "height": dimension("height", 720)},
            "road": {"polygon": left + right[::-1], "centerline": centerline,
                     "confidence": round(self.rng.uniform(.80, .98), 3)},
            "prediction": {"steering": round(self.steering, 4),
                           "confidence": round(self.rng.uniform(.75, .97), 3), "source": "demo"},
            "model": {"id": "random-perception-test", "label": "随机演示数据"},
        }


def run(args, client: Client) -> int:
    generator = RandomRoad(args.seed)
    started = time.monotonic()
    next_metadata = 0.0
    next_log = 0.0
    terminal = None
    selected = args.terminal_id
    sent = 0
    last_message = ""
    print("发送随机演示数据，仅用于画面叠加。Ctrl+C 停止。", flush=True)
    try:
        while not args.duration or time.monotonic() - started < args.duration:
            tick = time.monotonic()
            retry_delay = 0.0
            try:
                if terminal is None or tick >= next_metadata:
                    listing = client.request("GET", "/api/terminals")
                    online = [item for item in listing["terminals"] if item.get("connected")]
                    if selected is None and len(online) > 1:
                        names = ", ".join(item["terminal_id"] for item in online)
                        raise ValueError(f"有多个在线终端：{names}。请用 --terminal-id 指定")
                    terminal = next((item for item in online if selected is None or item["terminal_id"] == selected), None)
                    next_metadata = tick + 1
                    if terminal is None:
                        raise APIError(409, "等待指定终端上线" if selected else "等待终端上线")
                    selected = terminal["terminal_id"]
                    if not terminal.get("session_id"):
                        raise ValueError("终端没有 session_id，请先更新 Center 服务")
                payload = generator.payload(terminal)
                client.request("POST", f"/api/terminals/{quote(selected, safe='')}/perception", payload)
                sent += 1
                last_message = ""
                if tick >= next_log:
                    steering = payload["prediction"]["steering"]
                    direction = "左转" if steering < -.08 else "右转" if steering > .08 else "直行"
                    print(f"已发送 {sent} 条 | {selected} | {direction} {abs(steering):.0%} | 演示数据", flush=True)
                    next_log = tick + 1
            except APIError as error:
                if error.status not in {404, 409, 429} and error.status < 500:
                    raise
                terminal = None
                message = str(error)
                if message != last_message:
                    print(f"{message}，正在重试……", flush=True)
                    last_message = message
                retry_delay = 1
            except (URLError, TimeoutError, ConnectionError) as error:
                terminal = None
                message = f"连接中心失败：{type(error).__name__}"
                if message != last_message:
                    print(f"{message}，正在重试……", flush=True)
                    last_message = message
                retry_delay = 1
            delay = max(0, 1 / args.fps - (time.monotonic() - tick), retry_delay)
            if args.duration:
                delay = min(delay, max(0, args.duration - (time.monotonic() - started)))
            time.sleep(delay)
    except KeyboardInterrupt:
        pass
    print(f"已停止，共发送 {sent} 条；页面叠加将在 2 秒内自动过期。", flush=True)
    return sent


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--center", help="中心地址，默认 CENTER_URL 或 http://127.0.0.1:8000")
    parser.add_argument("--env-file", type=Path, default=ROOT / "center" / ".env", help="配置文件，默认 center/.env")
    parser.add_argument("--token", help="配对口令，默认读取 CENTER_TOKEN")
    parser.add_argument("--terminal-id", help="指定终端，默认 AI_TERMINAL_ID 或唯一在线终端")
    parser.add_argument("--fps", type=float, default=5, help="发送频率 1～20，默认5")
    parser.add_argument("--duration", type=float, default=0, help="运行秒数，0 表示持续运行（默认）")
    parser.add_argument("--seed", type=int, help="随机种子，可选")
    args = parser.parse_args()
    try:
        values = read_env(args.env_file)
        values.update(os.environ)
        args.center = args.center or values.get("CENTER_URL") or "http://127.0.0.1:8000"
        token = args.token or values.get("CENTER_TOKEN", "")
        args.terminal_id = args.terminal_id or values.get("AI_TERMINAL_ID") or None
        parsed = urlsplit(args.center)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                or any(char.isspace() for char in args.center) or parsed.port == 0):
            raise ValueError("--center 必须是 http(s)://主机[:端口]")
        if not token.strip() or "\r" in token or "\n" in token:
            raise ValueError("请在 center/.env 设置 CENTER_TOKEN，或通过环境变量/--token 提供")
        if not math.isfinite(args.fps) or not 1 <= args.fps <= 20:
            raise ValueError("--fps 必须在 1～20 之间")
        if not math.isfinite(args.duration) or args.duration < 0:
            raise ValueError("--duration 必须是非负有限秒数")
        run(args, Client(args.center, token))
    except (ValueError, KeyError, OSError, APIError) as error:
        parser.exit(1, f"错误：{error}\n")


if __name__ == "__main__":
    main()
