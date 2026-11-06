"""连接中心的最小训练/推理循环；策略与奖励函数由具体任务提供。"""

import argparse
import asyncio
from io import BytesIO
import os

from PIL import Image

from center.client import CenterClient


async def run(args):
    async with CenterClient(args.center, args.token) as client:
        terminals = await client.terminals()
        online = [item for item in terminals if item["connected"]]
        terminal_id = args.terminal_id or (online[0]["terminal_id"] if online else None)
        if terminal_id is None:
            raise RuntimeError("没有在线终端；先启动 terminal（可用 --mock 联调）")
        try:
            observation = await client.observe(terminal_id)
            for index in range(args.steps):
                with Image.open(BytesIO(observation.jpeg)) as image:
                    rgb = image.convert("RGB")
                    # 在此转换张量并调用你的模型，例如 action = policy(rgb)。
                    # 示例使用中立动作，运行示例不会主动给游戏加油。
                    action = {"steering": 0, "throttle": 0, "brake": 0, "duration_ms": 100}
                    print(f"step={index} frame={observation.sequence} image={rgb.size}")
                observation, ack = await client.step(terminal_id, action)
                # 在此计算 reward/done，并保存 (observation, action, reward, next_observation)。
                # 中心提供控制与观测，不推断赛道奖励，也不自动重置比赛。
                assert ack["ok"]
        finally:
            await client.release(terminal_id)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--center", default=os.environ.get("CENTER_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--token", default=os.environ.get("CENTER_TOKEN", ""))
    parser.add_argument("--terminal-id")
    parser.add_argument("--steps", type=int, default=10)
    args = parser.parse_args()
    if not args.token:
        parser.error("请设置 CENTER_TOKEN 或 --token")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
