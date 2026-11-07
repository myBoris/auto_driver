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


def add_auto_lane_arguments(parser):
    parser.add_argument("--lane-confidence", type=float, default=.75, help="自动预标注的最低启发式评分，并非校准概率")
    parser.add_argument("--roi-top", type=float, default=.4, help="检测区域顶边，归一化 y")
    parser.add_argument("--roi-bottom", type=float, default=.7, help="检测区域底边；驾驶舱视角应排除仪表台")


def auto_lane_config(args):
    from src.data.auto_lanes import AutoLaneConfig
    return AutoLaneConfig(min_confidence=args.lane_confidence, roi_top=args.roi_top, roi_bottom=args.roi_bottom)


def add_rl_environment_arguments(parser):
    parser.add_argument("--telemetry-timeout", type=float, default=5, help="等待新遥测的秒数；无遥测时不操作")
    parser.add_argument("--telemetry-max-age", type=float, default=1, help="允许的最大遥测年龄，秒")
    parser.add_argument("--max-episode-steps", type=positive_int, default=500)
    parser.add_argument("--stuck-seconds", type=float, default=5)
    parser.add_argument("--reset-mode", choices=["rewind", "none"], default="rewind",
                        help="卡住/回合结束后尝试 FH5 倒带；none 只清空回合记录")
    parser.add_argument("--reset-script", type=Path, help="自定义倒带/恢复动作 JSON，见 docs/rl-training.md")
    parser.add_argument("--max-reset-attempts", type=positive_int, default=3)
    parser.add_argument("--no-lane-reward", action="store_true", help="关闭 OpenCV 检出时的车道居中辅助奖励")
    add_auto_lane_arguments(parser)


def rl_environment(client, terminal, args, settings, *, image_size, history_frames):
    from src.environments.center_rl import CenterRLConfig, CenterRLEnvironment
    return CenterRLEnvironment(client, terminal, config=CenterRLConfig(
        image_size=image_size, history_frames=history_frames,
        observation_timeout=settings.observation_timeout, telemetry_timeout=args.telemetry_timeout,
        telemetry_max_age=args.telemetry_max_age, max_episode_steps=args.max_episode_steps,
        stuck_seconds=args.stuck_seconds, reset_mode=args.reset_mode, reset_script=args.reset_script,
        max_reset_attempts=args.max_reset_attempts, use_lane_reward=not args.no_lane_reward,
        lane_config=auto_lane_config(args)))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="通过 Center 采集、标注、训练和运行车道/方向/驾驶模型")
    parser.add_argument("--env-file", type=Path, help="连接配置文件，默认使用 AI/.env")
    parser.add_argument("--center", help="覆盖 CENTER_URL")
    parser.add_argument("--terminal-id", help="覆盖 AI_TERMINAL_ID；未配置时要求只有一个在线终端")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("terminals", help="查看在线终端，不发送动作")
    telemetry = commands.add_parser("telemetry", help="检查 FH5 Data Out 遥测链路，只读不操作")
    telemetry.add_argument("--timeout", type=float, default=5)
    observe = commands.add_parser("observe", help="获取一张训练观察图像，不发送动作")
    observe.add_argument("--output", type=Path, required=True, help="JPEG 保存路径，必须尚不存在")
    collect = commands.add_parser("collect", help="采集无标签图片序列，不发送动作")
    collect.add_argument("--frames", type=positive_int, default=100)
    collect.add_argument("--output", type=Path, help="数据输出目录，每次在其中创建独立 episode；默认使用 AI_DATA_DIR")
    training_data = commands.add_parser("collect-training", help="通过 Center 采集画面和已确认驾驶操作，不主动控制车辆")
    training_data.add_argument("--frames", type=positive_int, default=200)
    training_data.add_argument("--output", type=Path, default=Path("data/driving"))
    training_data.add_argument("--interval", type=float, default=0.1, help="两次采集的最小间隔，秒")
    training_data.add_argument("--images-only", action="store_true", help="只采集画面，不读取 Center 操作标签")
    training_data.add_argument("--auto-lanes", action="store_true", help="采集结束后自动预标注未标注车道，保留未复核来源")
    add_auto_lane_arguments(training_data)
    annotate = commands.add_parser("annotate", help="打开本机标注服务，给 Center 画面标记左右车道和驾驶操作")
    annotate.add_argument("--dataset", type=Path, required=True)
    annotate.add_argument("--port", type=positive_int, default=8766)
    auto_lanes = commands.add_parser("auto-label-lanes", help="对已有图片自动预标注车道；不覆盖已有标签，不生成驾驶操作")
    auto_lanes.add_argument("--dataset", type=Path, required=True)
    auto_lanes.add_argument("--limit", type=positive_int, help="最多处理的未标注图片数")
    add_auto_lane_arguments(auto_lanes)
    demo = commands.add_parser("make-demo-data", help="生成明确标记的合成道路样本，仅用于训练流程验证")
    demo.add_argument("--output", type=Path, default=Path("data/demo"))
    demo.add_argument("--episodes", type=positive_int, default=4)
    demo.add_argument("--frames", type=positive_int, default=16, help="每个片段的图片数")
    demo.add_argument("--seed", type=int, default=7)
    rollout = commands.add_parser("rollout", help="运行中立策略并保存动作和后续观察，验证训练数据链路")
    rollout.add_argument("--steps", type=positive_int, default=5)
    rollout.add_argument("--policy", choices=["neutral", "driving"], default="neutral")
    rollout.add_argument("--checkpoint", type=Path, help="driving 策略的训练权重")
    rollout.add_argument("--device", default="auto")
    rollout.add_argument("--output", type=Path, help="数据输出目录，每次在其中创建独立 episode；默认使用 AI_DATA_DIR")
    bc = commands.add_parser("train-bc", help="联合训练车道边界、转向类别、转向力度和驾驶操作")
    bc.add_argument("--dataset", type=Path, required=True)
    bc.add_argument("--output", type=Path, default=Path("runs/bc"))
    bc.add_argument("--epochs", type=positive_int, default=10)
    bc.add_argument("--batch-size", type=positive_int, default=16)
    bc.add_argument("--learning-rate", type=float, default=0.0001)
    bc.add_argument("--device", default="auto")
    bc.add_argument("--image-size", type=positive_int, default=128)
    bc.add_argument("--patch-size", type=positive_int, default=16)
    bc.add_argument("--history-frames", type=positive_int, default=1)
    bc.add_argument("--embed-dim", type=positive_int, default=128)
    bc.add_argument("--num-heads", type=positive_int, default=4)
    bc.add_argument("--depth", type=positive_int, default=3)
    bc.add_argument("--mlp-dim", type=positive_int, default=256)
    bc.add_argument("--dropout", type=float, default=0.1)
    bc.add_argument("--validation-split", type=float, default=0.2)
    bc.add_argument("--seed", type=int, default=7)
    bc.add_argument("--num-workers", type=int, default=0)
    bc.add_argument("--allow-synthetic", action="store_true", help="允许训练合成样本；权重会标记为演示模型")
    bc.add_argument("--allow-policy-labels", action="store_true", help="显式接受已有策略产生的标签")
    bc.add_argument("--allow-auto-lanes", action="store_true", help="显式接受尚未人工复核的自动车道标签")
    bc.add_argument("--amp", action="store_true", help="CUDA 混合精度训练")
    bc.add_argument("--resume", type=Path, help="继续同样配置的 last.pt；epochs 表示再训练的轮数")
    live = commands.add_parser("train-live", help="在 Center 人工驾驶时持续采集并后台增量训练，Ctrl+C 停止")
    live.add_argument("--dataset", type=Path, default=Path("data/driving"))
    live.add_argument("--output", type=Path, default=Path("runs/live"))
    live.add_argument("--frames", type=positive_int, help="可选：采集指定帧数后退出；默认持续运行")
    live.add_argument("--chunk-frames", type=positive_int, default=16, help="每批发布多少新帧供后台训练")
    live.add_argument("--interval", type=float, default=.1)
    live.add_argument("--min-samples", type=positive_int, default=16)
    live.add_argument("--steps-per-update", type=positive_int, default=10)
    live.add_argument("--replay-size", type=positive_int, default=512)
    live.add_argument("--warm-start", type=Path, help="从已有兼容检查点继续学习新数据；与离线严格 resume 分开")
    live.add_argument("--auto-lanes", action="store_true", help="新采集图片同步生成自动车道候选")
    add_auto_lane_arguments(live)
    # 与离线训练使用相同的模型参数；实时初次采集默认不宣称独立验证。
    for name, default in (("batch-size", 16), ("image-size", 128), ("patch-size", 16),
                          ("history-frames", 1), ("embed-dim", 128), ("num-heads", 4),
                          ("depth", 3), ("mlp-dim", 256)):
        live.add_argument("--" + name, type=positive_int, default=default)
    live.add_argument("--learning-rate", type=float, default=.0001)
    live.add_argument("--device", default="auto")
    live.add_argument("--dropout", type=float, default=.1)
    live.add_argument("--validation-split", type=float, default=0, help="默认 0 无独立验证；大于 0 时需至少两次独立采集")
    live.add_argument("--seed", type=int, default=7)
    live.add_argument("--allow-auto-lanes", action="store_true", help="允许用未复核自动车道参与训练；默认只存候选")
    live.add_argument("--amp", action="store_true")
    predict = commands.add_parser("predict", help="读取 Center 新画面并显示模型预测；--execute 才操作驾驶")
    predict.add_argument("--checkpoint", type=Path, required=True)
    predict.add_argument("--frames", type=positive_int, default=100)
    predict.add_argument("--device", default="auto")
    predict.add_argument("--execute", action="store_true", help="将模型输出的操作发送到所选终端")
    predict.add_argument("--lane-threshold", type=float, default=0.5)
    predict.add_argument("--button-threshold", type=float, default=0.5)
    predict.add_argument("--max-throttle", type=float, default=1.0)
    predict.add_argument("--max-inference-seconds", type=float, default=1.0)
    predict.add_argument("--output", type=Path, help="可选：预测 JSONL 或执行轨迹保存目录")
    rl = commands.add_parser("train-rl", help="FH5 自由驾驶 PPO：自动操作、遥测奖励、分批更新和卡住后倒带")
    rl.add_argument("--output", type=Path, default=Path("runs/rl"))
    rl.add_argument("--total-steps", type=positive_int, default=10000)
    for name, default in (("rollout-steps", 128), ("batch-size", 32), ("update-epochs", 4),
                          ("image-size", 84), ("history-frames", 4), ("action-duration-ms", 100),
                          ("checkpoint-interval", 1000)):
        rl.add_argument("--" + name, type=positive_int, default=default)
    for name, default in (("learning-rate", .0003), ("gamma", .99), ("gae-lambda", .95),
                          ("clip-ratio", .2), ("entropy-coef", .01), ("value-coef", .5),
                          ("max-grad-norm", .5), ("target-kl", .03), ("max-steering", .7),
                          ("max-throttle", .35), ("max-brake", .7)):
        rl.add_argument("--" + name, type=float, default=default)
    rl.add_argument("--device", default="auto")
    rl.add_argument("--seed", type=int, default=0)
    rl.add_argument("--resume", type=Path, help="恢复 PPO 模型、优化器和训练步数")
    add_rl_environment_arguments(rl)
    rl_predict = commands.add_parser("predict-rl", help="加载 PPO 检查点自动驾驶，保持模型权重不更新")
    rl_predict.add_argument("--checkpoint", type=Path, required=True)
    rl_predict.add_argument("--steps", type=positive_int, default=300)
    rl_predict.add_argument("--device", default="auto")
    add_rl_environment_arguments(rl_predict)
    return parser


async def run(args, settings: Settings) -> None:
    # 离线任务不创建 Center 连接；训练和推理在执行时才导入 PyTorch。
    if args.command == "make-demo-data":
        from src.data.synthetic import generate_synthetic_dataset
        count = generate_synthetic_dataset(args.output, episodes=args.episodes, frames_per_episode=args.frames, seed=args.seed)
        print(f"已生成 {count} 张合成演示样本：{args.output}；这些样本用于验证流程")
        return
    if args.command == "auto-label-lanes":
        from src.data.auto_lanes import annotate_dataset
        summary = await asyncio.to_thread(annotate_dataset, args.dataset, auto_lane_config(args), limit=args.limit)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return
    if args.command == "train-bc":
        from src.training import BehaviorCloningConfig, BehaviorCloningTrainer
        summary = BehaviorCloningTrainer(BehaviorCloningConfig(
            dataset_dir=args.dataset, output_dir=args.output, epochs=args.epochs, batch_size=args.batch_size,
            learning_rate=args.learning_rate, device=args.device, image_size=args.image_size,
            patch_size=args.patch_size, history_frames=args.history_frames, embed_dim=args.embed_dim,
            num_heads=args.num_heads, depth=args.depth, mlp_dim=args.mlp_dim, dropout=args.dropout,
            validation_split=args.validation_split, seed=args.seed, num_workers=args.num_workers,
            allow_synthetic=args.allow_synthetic, allow_policy_labels=args.allow_policy_labels,
            allow_auto_lanes=args.allow_auto_lanes,
            amp=args.amp, resume=args.resume,
        )).fit()
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return
    live_trainer = None
    labeler = None
    if args.command == "train-live":
        from src.training.bc import BehaviorCloningConfig
        from src.training.online import OnlineTrainer, OnlineTrainingConfig
        base = BehaviorCloningConfig(
            dataset_dir=args.dataset, output_dir=args.output, batch_size=args.batch_size,
            learning_rate=args.learning_rate, device=args.device, image_size=args.image_size,
            patch_size=args.patch_size, history_frames=args.history_frames, embed_dim=args.embed_dim,
            num_heads=args.num_heads, depth=args.depth, mlp_dim=args.mlp_dim, dropout=args.dropout,
            validation_split=args.validation_split, seed=args.seed, allow_auto_lanes=args.allow_auto_lanes, amp=args.amp,
        )
        live_trainer = OnlineTrainer(OnlineTrainingConfig(
            base=base, min_samples=args.min_samples, steps_per_update=args.steps_per_update,
            replay_size=args.replay_size, warm_start=args.warm_start))
        if args.auto_lanes:
            from src.data.auto_lanes import AutoLaneLabeler
            labeler = AutoLaneLabeler(auto_lane_config(args))
        print("请在 Center 网页人工驾驶；实时采集只读取已确认输入，不会执行模型动作。", flush=True)
        if not args.validation_split:
            print("本次未设置独立验证集；训练损失不代表实际驾驶效果。", flush=True)
    if args.command == "train-rl":
        from src.training import ReinforcementLearningConfig, ReinforcementLearningTrainer
        rl_trainer = ReinforcementLearningTrainer(ReinforcementLearningConfig(
            output_dir=args.output, total_steps=args.total_steps, device=args.device,
            rollout_steps=args.rollout_steps, batch_size=args.batch_size, update_epochs=args.update_epochs,
            learning_rate=args.learning_rate, gamma=args.gamma, gae_lambda=args.gae_lambda,
            clip_ratio=args.clip_ratio, entropy_coef=args.entropy_coef, value_coef=args.value_coef,
            max_grad_norm=args.max_grad_norm, target_kl=args.target_kl, image_size=args.image_size,
            history_frames=args.history_frames, max_steering=args.max_steering, max_throttle=args.max_throttle,
            max_brake=args.max_brake, action_duration_ms=args.action_duration_ms,
            checkpoint_interval=args.checkpoint_interval, seed=args.seed, resume=args.resume))
    policy = None
    if args.command == "predict" or (args.command == "rollout" and args.policy == "driving"):
        from src.policies import DrivingPolicy
        if args.checkpoint is None:
            raise ValueError("driving 策略需要 --checkpoint 指定训练权重")
        policy = DrivingPolicy(checkpoint=args.checkpoint, device=args.device, duration_ms=settings.action_duration_ms,
                               lane_threshold=getattr(args, "lane_threshold", .5),
                               button_threshold=getattr(args, "button_threshold", .5),
                               max_throttle=getattr(args, "max_throttle", 1.0),
                               max_inference_seconds=getattr(args, "max_inference_seconds", 1.0))
    async with CenterClient(args.center or settings.base_url, settings.token) as client:
        if args.command == "terminals":
            # 不打印配对口令、请求头或发布地址，只展示与选取终端相关的状态。
            fields = ("terminal_id", "name", "connected", "session_id")
            entries = [{key: item.get(key) for key in fields} for item in await client.list_terminals()]
            print(json.dumps(entries, ensure_ascii=False, indent=2))
            return
        terminal = await resolve_terminal(client, args.terminal_id or settings.terminal_id)
        if args.command == "telemetry":
            snapshot = await client.get_telemetry(terminal["terminal_id"], session_id=terminal["session_id"],
                                                  timeout=args.timeout)
            print(json.dumps(snapshot, ensure_ascii=False, indent=2))
        elif args.command == "train-rl":
            environment = rl_environment(client, terminal, args, settings,
                                         image_size=args.image_size, history_frames=args.history_frames)
            print("PPO 将自动操作此终端并训练。请进入 FH5 自由驾驶，开启 Data Out、自动换挡和倒带；Ctrl+C 停止。", flush=True)
            summary = await rl_trainer.train(environment)
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        elif args.command == "predict-rl":
            from src.services.rl_driving import run_rl_policy
            from src.model.rl import RLPolicy
            policy = await asyncio.to_thread(RLPolicy, args.checkpoint, device=args.device)
            environment = rl_environment(client, terminal, args, settings,
                                         image_size=policy.model_config.image_size,
                                         history_frames=policy.model_config.history_frames)
            summary = await run_rl_policy(environment, policy, steps=args.steps)
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        elif args.command == "observe":
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
        elif args.command == "collect-training":
            from src.services.training_collection import collect_training_samples
            result = await collect_training_samples(client, terminal, args.output, frames=args.frames,
                                                     timeout=settings.observation_timeout, interval=args.interval,
                                                     include_controls=not args.images_only)
            if args.auto_lanes:
                from src.data.auto_lanes import annotate_dataset
                result["auto_lanes"] = await asyncio.to_thread(annotate_dataset, args.output, auto_lane_config(args))
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "train-live":
            from src.services.live_training import run_live_training
            result = await run_live_training(client, terminal, args.dataset, live_trainer,
                                             frames=args.frames, chunk_frames=args.chunk_frames,
                                             interval=args.interval, timeout=settings.observation_timeout,
                                             auto_labeler=labeler)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "predict" or (args.command == "rollout" and args.policy == "driving"):
            from src.services.driving import run_driving
            count = await run_driving(client, terminal, policy,
                                      frames=args.frames if args.command == "predict" else args.steps,
                                      execute=args.execute if args.command == "predict" else True,
                                      timeout=settings.observation_timeout, output_dir=args.output)
            print(f"已完成 {count} 帧模型预测")
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
        if args.command == "annotate":
            from src.data.annotations import run_annotation_server
            run_annotation_server(args.dataset, port=args.port)
            return
        settings = Settings.from_env(args.env_file)
        asyncio.run(run(args, settings))
    except KeyboardInterrupt:
        print("运行已停止")
    except (ValueError, RuntimeError, OSError) as exc:
        parser.exit(1, f"错误：{exc}\n")


if __name__ == "__main__":
    main()
