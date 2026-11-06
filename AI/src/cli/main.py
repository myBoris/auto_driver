"""训练项目命令入口；网络、数据保存和策略逻辑交给各自模块。

帮助和离线框架入口不需要导入 PyTorch。只有用户选择在线命令时才创建中心
连接；被动采集与策略交互是不同的业务，前者不会发送驾驶或释放操作。
"""

import argparse
import asyncio
import json
from pathlib import Path
import sys
import uuid

from src.config import Settings
from src.integrations.center import CenterClient
from src.policies import NeutralPolicy
from src.services import collect_observations, resolve_terminal, run_rollout


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("请输入正整数")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="自动驾驶 AI 数据接入与训练框架")
    parser.add_argument("--env-file", type=Path, help="连接配置文件，默认使用 AI/.env")
    parser.add_argument("--center", help="覆盖 CENTER_URL")
    parser.add_argument("--terminal-id", help="覆盖 AI_TERMINAL_ID；未配置时要求只有一个在线终端")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("terminals", help="查看在线终端，不发送动作")
    observe = commands.add_parser("observe", help="获取一张训练观察图像，不发送动作")
    observe.add_argument("--output", type=Path, required=True, help="JPEG 保存路径，必须尚不存在")
    collect = commands.add_parser("collect", help="采集无标签图片序列，不发送动作")
    collect.add_argument("--frames", type=positive_int, default=100)
    collect.add_argument("--output", type=Path, help="数据输出目录，每次在其中创建独立 episode；默认使用 AI_DATA_DIR")
    rollout = commands.add_parser("rollout", help="运行中立策略并保存动作和后续观察，验证训练数据链路")
    rollout.add_argument("--steps", type=positive_int, default=5)
    rollout.add_argument("--policy", choices=["neutral"], default="neutral")
    rollout.add_argument("--output", type=Path, help="数据输出目录，每次在其中创建独立 episode；默认使用 AI_DATA_DIR")
    bc = commands.add_parser("train-bc", help="行为克隆训练入口，具体算法待实现")
    bc.add_argument("--dataset", type=Path, required=True)
    bc.add_argument("--output", type=Path, default=Path("runs/bc"))
    bc.add_argument("--epochs", type=positive_int, default=10)
    rl = commands.add_parser("train-rl", help="强化学习训练入口，具体算法待实现")
    rl.add_argument("--output", type=Path, default=Path("runs/rl"))
    rl.add_argument("--total-steps", type=positive_int, default=10000)
    return parser


async def run(args, settings: Settings) -> None:
    # 训练入口保持离线可导入；占位方法明确说明实现位置，不会假装完成训练。
    if args.command == "train-bc":
        from src.training import BehaviorCloningConfig, BehaviorCloningTrainer
        BehaviorCloningTrainer(BehaviorCloningConfig(
            dataset_dir=args.dataset, output_dir=args.output, epochs=args.epochs,
        )).fit()
        return
    if args.command == "train-rl":
        from src.training import ReinforcementLearningConfig, ReinforcementLearningTrainer
        ReinforcementLearningTrainer(ReinforcementLearningConfig(
            output_dir=args.output, total_steps=args.total_steps,
        )).train()
        return
    async with CenterClient(args.center or settings.base_url, settings.token) as client:
        if args.command == "terminals":
            # 不打印配对口令、请求头或发布地址，只展示与选取终端相关的状态。
            fields = ("terminal_id", "name", "connected", "session_id")
            entries = [{key: item.get(key) for key in fields} for item in await client.list_terminals()]
            print(json.dumps(entries, ensure_ascii=False, indent=2))
            return
        terminal = await resolve_terminal(client, args.terminal_id or settings.terminal_id)
        if args.command == "observe":
            observation = await client.observe(terminal["terminal_id"], timeout=settings.observation_timeout)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            # 独立图像也不覆盖旧文件；正式数据集请使用 collect 记录元数据。
            with args.output.open("xb") as output:
                output.write(observation.jpeg)
            print(f"已保存观察帧 {observation.sequence}：{args.output}")
        elif args.command == "collect":
            output = args.output or settings.data_dir / "episodes" / f"observations-{uuid.uuid4().hex[:12]}"
            count = await collect_observations(client, terminal, output, frames=args.frames,
                                               timeout=settings.observation_timeout)
            print(f"已保存 {count} 张无标签观察图像：{output}")
        elif args.command == "rollout":
            policy = NeutralPolicy(duration_ms=settings.action_duration_ms)
            output = args.output or settings.data_dir / "episodes" / f"rollout-{uuid.uuid4().hex[:12]}"
            count = await run_rollout(client, terminal, policy, output, steps=args.steps,
                                      timeout=settings.observation_timeout)
            print(f"已保存 {count} 条中立动作交互记录：{output}")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args()
    try:
        settings = Settings.from_env(args.env_file)
        asyncio.run(run(args, settings))
    except KeyboardInterrupt:
        print("运行已停止")
    except (ValueError, RuntimeError, OSError) as exc:
        parser.exit(1, f"错误：{exc}\n")


if __name__ == "__main__":
    main()
