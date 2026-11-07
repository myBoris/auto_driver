# FH5 自动驾驶与 PPO 强化学习

`train-rl` 会实际操作所选终端，用游戏反馈计算奖励并更新策略。它不需要人工驾驶标签；`train-live` 则是监督式模仿学习，仍使用 Center 的驾驶示范，两者是独立入口和检查点格式。

## 1. 游戏电脑与 Center

游戏电脑运行更新后的 `terminal/`，Center 运行更新后的服务。终端默认在本机 `127.0.0.1:5300` 被动接收 FH5 Data Out，再通过现有认证 WebSocket 转发到 Center；AI 无需直接连接游戏电脑的 UDP 端口，也无需虚拟手柄驱动。

在 FH5 的设置 → HUD 和游戏性中设置：

- 数据输出 / Data Out：开启。
- 数据输出 IP：`127.0.0.1`。
- 数据输出端口：`5300`。
- 使用 Dashboard 数据格式（若界面提供该选项）。

进入自由驾驶/练习路段，启用倒带，并使用自动换挡。终端按游戏窗口配置持续上传 JPEG 观察。先检查链路，在 `AI` 目录运行：

```powershell
uv run auto-driver-ai --env-file ../center/.env telemetry
```

应返回 `status=ready`、`data.is_race_on=true` 及实时变化的速度/位置。`waiting` 表示未取得新的遥测，`stale` 表示过期，`disconnected` 表示终端断线。检查 IP/端口、游戏是否暂停及终端是否更新并重启；没有真实有效遥测时，训练前置检查会退出，不发送探索动作。

## 2. 自动操作并训练

```powershell
cd G:\work\auto-driver\AI

# 首次有限运行，观察终端控制、奖励和恢复逻辑。
uv run auto-driver-ai --env-file ../center/.env train-rl --output runs/rl-test --total-steps 256 --rollout-steps 64

# 持续采样 10000 步，每批进行 PPO 更新。
uv run auto-driver-ai --env-file ../center/.env train-rl --output runs/rl --total-steps 10000

# 加载已有 PPO 检查点，训练到累计 20000 步。
uv run auto-driver-ai --env-file ../center/.env train-rl --output runs/rl --resume runs/rl/latest.pt --total-steps 20000
```

多终端时在子命令前增加 `--terminal-id 游戏电脑ID`。启动后由模型控制车辆，训练期间不要同时使用 Center 手动驾驶或其他 API 控制脚本；`Ctrl+C` 停止，已开始的动作在原会话释放，并保留可保存的训练状态。游戏比赛、菜单或数据流切换不被当成正常驾驶样本。

初始模型从随机策略开始，动作是探索行为，需要大量交互才能改善。默认转向上限 0.7、油门上限 0.35、刹车上限 0.7，可分别用 `--max-steering`、`--max-throttle`、`--max-brake` 调整。模型输出转向和有符号踏板值，映射为互斥的油门/刹车。手动离合和换挡不在本次动作空间内，由游戏自动挡处理。

模型使用最近 4 张 84×84 RGB 画面及遥测特征；PPO 对采样策略计算优势、截断策略比率、价值损失和熵项。每批更新前释放已持有输入，更新结束后重新获取画面和遥测，避免用训练计算前的旧画面操作持续运行的游戏。

## 3. 奖励与训练目标

默认目标是自由驾驶中的持续前进与稳定操作：使用真实世界坐标变化、速度和本地前向速度，配合急转/输入变化、卡住及异常运动惩罚。位移突变（例如倒带、传送）会截断样本，不成为高额进度奖励。

OpenCV 能可靠检出双侧车道时，额外提供居中辅助奖励；未检出不等于没有道路。`--no-lane-reward` 可关闭该项，`--roi-top`、`--roi-bottom`、`--lane-confidence` 可调整检测范围和评分门槛。

FH5 Data Out 提供速度、位置和车辆状态，**不提供真实车道边界、道路偏移或碰撞事件**。碰撞/道路保持相关项是可观测数据推导的估计，不是游戏真值。当前奖励也不包含目的地导航、路线完成率或比赛名次目标；学会更稳定地前进不等于已学会按指定路线完成比赛。后续可在 `training/reward.py` 接入更可靠的赛道进度、独立道路分割模型或额外游戏信号。

## 4. 卡住后的恢复

默认卡住超过 5 秒或达到回合步数上限后，尝试 FH5 倒带恢复，随后等待新鲜且处于驾驶状态的遥测再开始下一回合。`--stuck-seconds`、`--max-episode-steps` 和 `--max-reset-attempts` 可调整；恢复后仍反复卡住达到次数上限时停止训练，不无限点击菜单。

默认使用 Xbox `Y` 倒带、`A` 继续。实际游戏按键或界面不同时，可用 `--reset-script configs/fh5-reset.json` 配置恢复脚本。脚本只允许按钮和等待，示例：

```json
{"version":1,"steps":[{"buttons":["Y"],"duration_ms":200},{"wait_seconds":2.0},{"buttons":["A"],"duration_ms":200},{"wait_seconds":0.5}]}
```

该脚本是游戏按键序列，不是 Center 的原生游戏重置接口。`--reset-mode none` 不发送倒带按钮，只清空回合统计，适合你另有游戏恢复机制的情况。

Center 的 POST 网络失败不会自动重试，避免按键重复执行。遥测过期、观察断流、终端会话变化或恢复失败会停止当前闭环；输入释放仅针对本次训练操作过的原连接。

## 5. 输出与独立驾驶

输出目录保存模型、优化器、训练步数、PPO 参数及训练指标。`latest.pt` 用于继续训练，`best.pt` 基于采样批次的平均训练奖励选择，后者不是独立评估成绩。`--total-steps` 是包括恢复前步数的累计目标。写入采用临时文件加原子替换，避免推理进程读取半个检查点；仅交互但尚未完成一次策略更新的应急检查点只能用于恢复训练，不能当作已训练策略运行。

```powershell
# 加载 PPO 模型自动驾驶 300 步，保持模型权重不更新。
uv run auto-driver-ai --env-file ../center/.env predict-rl --checkpoint runs/rl/latest.pt --steps 300
```

PPO 检查点用 `train-rl --resume` / `predict-rl`；监督模型用 `train-bc --resume` / `predict`，两者不能混用。RL 学习的是驾驶策略；车道显示/辅助奖励来自独立视觉检测器，并不表示 PPO 自动获得了车道标注模型。
