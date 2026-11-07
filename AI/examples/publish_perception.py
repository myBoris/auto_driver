"""仅向画面发布演示道路和方向，不运行模型，也不发送游戏控制。

在 AI 目录执行 ``uv run python -m examples.publish_perception``。
默认发布 30 次、每秒 5 次，停止后叠加结果由 Center 自动过期；不调用 release。
训练完成后可直接使用 CenterClient.publish_perception 发布自己的推理结果。
"""

from __future__ import annotations

import argparse
import asyncio
import math
import random
import sys

from src.config import Settings
from src.integrations.center import CenterClient
from src.services import resolve_terminal


def demo_payload(terminal: dict, steering: float) -> dict:
    """固定梯形仅用于看清叠加效果，坐标不是从当前游戏画面检测得到的。"""
    def dimension(name: str, fallback: int) -> int:
        value = terminal.get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 8192 else fallback

    return {
        "session_id": terminal["session_id"],
        "status": "ready",
        "image": {"width": dimension("width", 1280), "height": dimension("height", 720)},
        "road": {
            "polygon": [[.40, .48], [.66, .48], [.85, .64], [.21, .64]],
            "centerline": [[.53, .63], [.53, .58], [.53, .53], [.53, .48]],
            "confidence": 1.0,
        },
        "prediction": {"steering": steering, "confidence": 1.0, "source": "demo"},
        "model": {"id": "demo-road-overlay", "label": "演示数据"},
    }


async def publish_demo(client: CenterClient, terminal: dict, *, frames: int = 30,
                       fps: float = 5.0, seed: int = 7) -> int:
    """有限演示循环；连接换代后明确停止，防止把旧会话结果发布到新画面。"""
    if isinstance(frames, bool) or not isinstance(frames, int) or not 1 <= frames <= 1000:
        raise ValueError("frames must be an integer between 1 and 1000")
    if isinstance(fps, bool) or not isinstance(fps, (int, float)) or not math.isfinite(fps) or not .5 <= fps <= 10:
        raise ValueError("fps must be finite and between 0.5 and 10")
    session_id = terminal.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise RuntimeError("终端缺少当前 session_id，无法发布演示数据")
    rng = random.Random(seed)
    steering = 0.0
    terminal_id = terminal["terminal_id"]
    for index in range(frames):
        current = await client.get_terminal(terminal_id)
        if not current.get("connected") or current.get("session_id") != session_id:
            raise RuntimeError("终端连接已改变，演示已停止；请重新运行并选择当前终端")
        steering = .8 * steering + .2 * rng.uniform(-.6, .6)
        await client.publish_perception(terminal_id, demo_payload(current, steering))
        direction = "左" if steering < 0 else "右"
        print(f"演示数据 {index + 1}/{frames}：{direction} {abs(steering):.0%}，仅画面叠加")
        if index + 1 < frames:
            await asyncio.sleep(1 / fps)
    return frames


async def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="发布固定道路和缓变方向演示，不控制游戏")
    parser.add_argument("--terminal-id", help="覆盖 AI_TERMINAL_ID")
    parser.add_argument("--frames", type=int, default=30, help="发布次数，1–1000，默认30")
    parser.add_argument("--fps", type=float, default=5.0, help="发布频率，0.5–10，默认5")
    parser.add_argument("--seed", type=int, default=7, help="演示随机种子")
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    async with CenterClient(settings.base_url, settings.token) as client:
        terminal = await resolve_terminal(client, args.terminal_id or settings.terminal_id)
        print("正在发布演示数据；道路与方向均为模拟，非当前画面检测或模型预测。")
        await publish_demo(client, terminal, frames=args.frames, fps=args.fps, seed=args.seed)
        print("演示结束，叠加结果将自动过期。")


if __name__ == "__main__":
    asyncio.run(main())
