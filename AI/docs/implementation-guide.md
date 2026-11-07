# 采集、联合训练与在线驾驶

本工程已实现从 Center 取图和确认输入、本地标注、视觉 Transformer、车道/方向/操作联合监督训练、检查点加载，以及在线显示和驾驶。FH5 自由驾驶也可通过 Data Out 遥测和 PPO 自动交互训练；比赛导航、真实碰撞事件和完整游戏重置仍需额外信号。

## 一、先确定训练样本是什么

先考虑行为克隆：输入当前画面或一段连续画面，预测转向、油门与刹车。每条记录的正确关系是 `observation → action`，后续观察 `next_observation` 用于分析动作结果；不能把动作执行后的画面当作该动作决策时已知的输入。

`collect-training --frames 500 --output data/driving` 从 Center 获取更新帧，读取 `/control` 的即时确认输入，仅在终端采集时间处于确认动作有效区间时保存标签。应通过 Center 网页驾驶车辆；游戏直接接收的独立设备输入不在 Center 状态中。短动作可能在读取状态前已结束，从而漏标；程序保留未标注状态，不把它填成零动作。

每次采集独立生成 episode，根目录的 `samples.jsonl` 记录真实图像及可选操作标签。采集两段或更多路况后，用 `annotate --dataset data/driving` 打开本机标注页，为左右道路边界加点，核对驾驶操作。未标注的任务使用独立掩码；明确“没有道路”和尚未标注是两种不同标签。

`rollout` 保存的是你指定策略通过 Center 发出的请求动作及 ACK，可以用于交互学习或管道验证。中立策略只验证协议和数据格式，不构成有驾驶能力的训练集。ACK 表示虚拟手柄提交成功，不代表车辆已转弯、未碰撞或获得了奖励。

## 二、实现数据读取和预处理

`src/data/dataset.py` 已实现清单版本与身份检查、图片路径约束、来源筛选、车道插值和历史采样。按完整 episode 划分训练集与验证集，避免同一片段的邻近帧泄漏。策略记录默认不当作专家示范；合成样本需显式允许并写入检查点来源。

`src/data/preprocessing.py` 在离线和在线使用完全相同的 RGB 转换、完整画面缩放及 `[0,1]` 像素值；不裁剪，不改变归一化车道坐标。历史窗口不足时前补零并提供有效帧掩码，不跨片段和会话。模型输入为 `[B,N,3,H,W]`。

## 三、实现驾驶策略

`src/model/driving.py` 在共享 Transformer 特征上提供五个输出头：连续操作、15 个按钮、固定 y 锚点左右车道边界、道路可见性，以及左/直/右类别。转向采用 tanh，踏板和有序车道坐标采用 sigmoid 约束。

`src/policies/driving.py` 使用 `torch.load(weights_only=True)` 重建检查点中的模型和预处理。`predict_result` 返回实际预测的道路区域、中心点、转向和操作；只显示训练过的任务，不输出随机占位驾驶。方向置信度使用与连续转向类别对应的分类概率，道路置信度是可见性概率，并不等同于道路几何误差估计。

模型推理不要放在 Center 内部。AI 通过 `src/integrations/center/` 获取 JPEG 和发送动作；WebRTC 用于人看的视频预览，其解码和显示缓冲不作为训练图像时间来源。`run_rollout` 已在线程执行同步 `predict`，由网络事件循环负责响应和取消。

## 四、实现离线训练

`src/training/bc.py` 已实现 DataLoader、AdamW、任务掩码、回归和分类损失、梯度裁剪、可选 CUDA AMP、按片段验证、指标统计及原子保存。可先只标注车道，或只收集操作示范，再逐步补齐多任务标签。全负车道样本不足以宣称已学习左右边界。

`train-bc --dataset data/driving --output runs/driving --epochs 30` 训练，`--resume runs/driving/last.pt` 追加训练。输出 `best.pt`、`last.pt`、逐轮 `history.jsonl` 及 `split.json`，包括转向/油门/刹车 MAE、方向准确率和有效车道坐标 MAE；没有监督的任务指标为 null。恢复时校验模型、任务、预处理、真实/合成来源、数据指纹和划分，避免无声更改验证集。

## 五、实时增量训练与自动预标注

`train-live --dataset data/driving --output runs/live --auto-lanes --allow-auto-lanes --amp` 在 Center 人工驾驶时持续获取画面和确认输入。`src/services/live_training.py` 分批原子发布清单，后台线程执行 `src/training/online.py`；模型、优化器与 AMP 状态跨批保留。每轮只进行有限梯度更新，混合新样本和有界旧样本回放；没有可靠标签时返回 waiting，不能宣称训练成功或生成可用模型。

一次采集run始终同一 episode / `split_group`。实时默认没有独立验证集；设置 `--validation-split 0.2` 时须有两个独立采集组，并保持组划分稳定，验证样本不能进入训练回放。在线 `--warm-start` 允许新数据，校验模型与预处理兼容；离线 `--resume` 的严格指纹检查继续保留。原子发布 `latest.pt` 及版本检查点，供独立 `predict` 进程预览；采集训练不会自动发送驾驶动作。

`src/data/auto_lanes.py` 用 OpenCV 标线颜色、边缘与透视一致性生成车道候选，不需要初始训练数据，评分不足则不标注。检测区域可排除驾驶舱，真实弯道/遮挡可能无法识别；后续可将 `annotate(jpeg)` 替换为独立的预训练教师模型。来源 `auto` 默认不进入车道损失，须人工确认或显式启用 `--allow-auto-lanes`。学生模型预测不回填为自己的真值；车道候选不会自动变成合格驾驶操作。

实时监督训练仍需要通过 Center 的人工示范。不要同时运行 AI 驾驶或 API 操作演示：`center_confirmed` 只能证明输入被执行，不能证明来自人类或是正确决策。游戏直连外设的操作不会自动成为 Center 标签。

## 六、PPO 自动交互训练

`src/training/rl.py` 实现连续控制 PPO，`src/model/rl.py` 使用图像序列和遥测特征生成有界转向、互斥油门/刹车，以及状态价值。`src/environments/center_rl.py` 对接 Center 的观察、动作和遥测。`src/training/reward.py` 使用实际位移/运动状态及可选车道检测构造奖励；不把油门大小、ACK 或模型自己输出当作成功驾驶奖励。全流程和配置见 [rl-training.md](rl-training.md)。

游戏电脑的 Terminal 被动接收 FH5 Data Out，再经现有认证 WebSocket 转发到 Center 的当前会话。AI 先检查遥测新鲜度、游戏驾驶状态及画面对齐，再允许探索。瞬移、时间回退、异常位移不能获得正向进度奖励；PPO 的 terminated 不 bootstrap，truncated 使用最终观察 bootstrap 但不跨回合传递 GAE。检查点保留模型、优化器、步数和随机状态，独立于监督模型格式。

Center 的 `step` 只保证：动作 ACK 的执行时间加保持时长之后，才返回新采集的观察。游戏持续运行，网络、采样与调度都会耗时，因此它不是确定性的游戏物理帧推进接口。PPO 更新前释放持有输入，更新后重新获取观察并重置奖励基准，避免旧状态与训练计算期间车辆移动混入下一步归因。

游戏卡住/回合结束时可用配置的 Xbox 按键脚本尝试 FH5 倒带恢复，失败超过上限停止。该流程不是游戏原生 reset，受游戏按键和菜单影响。Data Out 不提供真实车道、道路偏移或碰撞事件；自由驾驶奖励目前也不包含比赛路线完成率，相关目标需继续扩展。断线、遥测失效和人为取消会结束闭环并释放本次操作过的原会话，不自动重放驾驶 POST。

## 七、实现评估和部署

先用 `predict --checkpoint runs/driving/best.pt` 从 Center 读取新画面，只将车道与方向通过 `perception` 发布到管理页。添加 `--execute` 后才调用 `step` 操作驾驶；可用 `--max-throttle` 限制油门。原会话出错或取消后释放输入；连接被替换时不释放新会话。推理与网络耗时超过上限时不发送旧画面的动作。

在线流程记录预测及可选执行转移，不会自动计算车速、碰撞或任务完成率。固定赛道的任务评估可扩展在 `src/evaluation/`。合成检查点只用于训练与显示联调，不能执行真实驾驶。

观察上传频率由游戏电脑 `TERMINAL_OBSERVATION_FPS` 控制，默认 10 FPS；视频默认 60 FPS，两者独立。提高视觉控制频率前，先实测采集、推理和网络耗时，再调整观察频率及 `duration_ms`，不要把视频播放 FPS 当成 AI 的可用观察频率。
