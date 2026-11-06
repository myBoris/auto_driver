"""最小 Center 交互示例：读取观察、执行中立动作、获取后续图像并释放输入。

在 AI 目录执行 ``uv run python -m examples.center_roundtrip``。本示例展示真实
API 调用顺序，不训练模型；修改 Action 后会通过 Center 操作所选终端。
"""

import asyncio

from src.config import Settings
from src.domain import Action
from src.integrations.center import CenterClient
from src.services import resolve_terminal


async def main() -> None:
    settings = Settings.from_env()
    async with CenterClient(settings.base_url, settings.token) as client:
        terminal = await resolve_terminal(client, settings.terminal_id)
        terminal_id = terminal["terminal_id"]
        try:
            observation = await client.observe(terminal_id)
            print(f"当前帧：{observation.sequence}，图像字节数：{len(observation.jpeg)}")
            # 在这里将 observation 交给你的模型；返回归一化轴值及动作保持时间。
            action = Action(steering=0.0, throttle=0.0, brake=0.0, duration_ms=100)
            result = await client.step(terminal_id, action)
            print(f"动作确认：{result.command_id}，后续帧：{result.observation.sequence}")
            # 如果只需要发送操作而不等待图像，可用 await client.action(terminal_id, action)。
        finally:
            await client.release(terminal_id)


if __name__ == "__main__":
    asyncio.run(main())
