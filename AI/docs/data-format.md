# 采集数据格式

`EpisodeWriter` 使用标准库将真实 JPEG 和 JSONL 写到数据集目录。每次运行创建唯一的 `episode-时间-随机ID/`，不覆盖已有 episode；每个 episode 固定一个 `terminal_id` 和中心连接 `session_id`。中心重启或终端重新注册后，应结束当前 episode 并新建记录器。

```text
data/
└── episode-.../
    ├── meta.json
    ├── frames/
    │   ├── 101.jpg
    │   └── 102.jpg
    └── records.jsonl
```

`meta.json` 和每条 JSONL 均包含 `schema_version: 1`、`episode_id`、`terminal_id`、`session_id`。元数据还记录采集模式、创建/关闭时间、记录数和正常关闭或中断状态。JPEG 先写入，JSONL 后追加；异常可留下未被引用的 JPEG，读取端应只消费完整 JSONL 记录并验证引用文件存在。

## 无标签图像

`mode="observations"` 用于被动采集。`write_observation(obs)` 保存 `frames/<sequence>.jpg` 并追加 `type="observation"` 记录，内含帧序号、终端采集时间 `captured_at`、中心接收时间 `received_at`、相对路径及 `labeled: false`。返回值是相对 episode 的 JPEG 路径。

```json
{"schema_version":1,"episode_id":"episode-...","terminal_id":"game-pc","session_id":"connection-1","type":"observation","observation":{"sequence":101,"captured_at":1700000000.1,"received_at":1700000000.2,"terminal_id":"game-pc","session_id":"connection-1","path":"frames/101.jpg"},"labeled":false}
```

`collect` 保持无标签语义。新的 `collect-training` 读取 Center `/control` 即时确认状态，并生成下面的监督样本清单；它不会修改原有 `records.jsonl` 格式。

## 联合训练样本

```text
data/driving/
├── samples.jsonl
├── episode-.../frames/101.jpg
└── episode-.../records.jsonl
```

`collect-training` 每次运行产生独立 episode，同时向数据集根目录的 `samples.jsonl` 追加图像引用。采集与标注保存通过 `.collection.lock` 互斥；采集停止后才保存标注。异常停止会保留完整 JSONL 行和对应图像，正常取消会移除锁。

```json
{"schema_version":1,"id":"episode-abc:101","episode_id":"episode-abc","image":"episode-abc/frames/101.jpg","sequence":101,"captured_at":1700000000.2,"received_at":1700000000.3,"source":"center","terminal_id":"game-pc","session_id":"connection-1","action_source":"center_confirmed","action":{"steering":-0.2,"throttle":0.4,"brake":0,"buttons":[],"duration_ms":200},"lane_source":"human","lane":{"visible":true,"left":[[0.2,0.95],[0.42,0.45]],"right":[[0.8,0.95],[0.58,0.45]]}}
```

`id` 在整个清单中唯一；`sequence` 在同一 episode 内唯一。图片路径必须位于数据集内部，读取器校验版本、身份、会话、来源、文件和标签范围。同一 episode 不能混入真实及合成图像；历史帧只来自本片段的当前和过去画面。

`action` 和 `lane` 均可省略或为 `null`。缺失标签对应训练掩码为假，不自动替换为“直行”“松开踏板”或“没有道路”。只有至少一个被接受的监督任务才会进入训练样本索引；未标注的早期画面仍可作为同片段历史。

操作标签明确包含 `steering`、`throttle`、`brake`、`buttons`，可带 `duration_ms`，范围与 Center 动作一致。`action_source` 为 `center_confirmed`、`human`、`synthetic` 或 `policy_proposed`。策略标签默认不参与训练，需显式 `--allow-policy-labels`；合成数据需 `--allow-synthetic` 并将权重标记为演示模型。

Center 标签只在当前会话在线、状态为 `active`，且 `applied_at <= captured_at < applied_at + duration_ms/1000` 时保存。两个时间均来自终端，不进行跨电脑时钟相减。标签记录的是该画面采集时正在执行的输入，不能证明它是最佳专家决策；短按可能在状态读取前结束而漏标，此时保留无标签。独立设备直接送到游戏的输入不属于 Center 状态；应通过 Center 网页演示或人工补充标签。

车道用完整原图的归一化 `[x,y]` 坐标，左右各至少两个点，不能交叉。训练在 `.95,.85,.75,.65,.55,.45` 六个 y 锚点线性插值，只监督实际标注覆盖的高度，可见车道至少覆盖两个有效锚点。检查点保存训练中有监督的高度，在线只绘制这些高度的边界，不把未学习的点延伸到车身或仪表盘。`lane={"visible":false}` 表示人工明确确认没有道路；省略 `lane` 表示尚未标注。标注页面保存后的来源为 `human`，原图片不改动。

自动预标注通过 `auto-label-lanes`、`collect-training --auto-lanes`、`train-live --auto-lanes` 或标注页按钮生成。只在双侧边界有足够图像支持时保存下列来源信息；未检出则保持缺失标签，不能填成 `visible:false`。自动程序不改已有标签，不生成驾驶操作。`confidence` 是启发式几何分数而非校准概率。

```json
{"lane_source":"auto","lane":{"visible":true,"left":[[0.4,0.45],[0.25,0.65]],"right":[[0.6,0.45],[0.75,0.65]]},"lane_annotation":{"method":"opencv-color-hough-v1","confidence":0.86,"reviewed":false,"roi_top":0.4,"roi_bottom":0.7,"min_confidence":0.75}}
```

`DrivingDataset` 默认屏蔽来源为 `auto` 的车道标签，但同帧可用操作标签仍参与训练；`--allow-auto-lanes` 才允许使用未经人工复核的候选。人工修正/确认保存后改为 `lane_source=human` 并保留已复核记录，不会将所有自动候选默认为人工真值。

实时采集 `train-live` 使用 `.live.lock` 防止同一数据集重复启动，且阻止普通追加式 `collect-training` 同时运行。整次采集对应同一 episode / `split_group`，每批短暂持有 `.collection.lock`，合并最新人工修正并原子替换完整 `samples.jsonl`。训练线程读取一次完整快照，JPEG 只新增不覆盖；手动标注可在批次间保存。慢训练会合并等待中的批次，不无限排队。退出保留已完成记录并等待本次有界更新结束。

训练输出每个锚点左右 x 边界、可见性、转向/油门/刹车、15 个 Xbox 按钮及左/直/右类别。方向标签由操作转向派生：小于 -0.08 为左、大于 0.08 为右，其余为直。离合通过 LB 开关，换挡对应 X/B。训练与推理都将完整 RGB 画面缩放至配置尺寸，保持归一化坐标含义。

## 策略动作转移

`mode="rollout"` 接受 `write_transition(obs, action, result, reward=None, terminated=False, truncated=False)`。一条 `type="transition"` 记录包含前后观察引用、完整动作、命令 ID、原始 ACK、可选奖励及终止标记。两个 JPEG 均成功保存后才追加转移记录。

`action_source="policy_proposed"`、`labeled: true` 表示动作标签来自本次调用的策略提出值，**不表示人类示范、游戏遥测或驾驶已成功**。`ack` 保存执行确认和 `applied_at`，但虚拟手柄调用成功不能证明车辆按预期移动。`NeutralPolicy` 的零动作只用于验证链路，不能当作合格驾驶示范。

`reward` 默认 `null`，`terminated`/`truncated` 默认 `false`；中心未提供奖励、比赛重置、碰撞/车速遥测或终止判断。任务实现需要自行提供这些信号。元数据 `reward_available` 仅在显式写入非空奖励后变为 `true`。

## 会话与训练边界

同一 episode 内重复保存同序号、同时间、同内容的观察会复用文件。若序号相同而时间或内容不同，记录器报错而不覆盖；显式携带不同终端/会话的观察也会被拒绝。不同 episode 可以从相同序号开始。

`src.data.DrivingDataset` 读取 `samples.jsonl`，返回图像窗口、有效帧掩码和各任务标签掩码，可直接交给 PyTorch DataLoader。`EpisodeDataset` 保留旧的文件引用协议。训练按完整 episode 划分，不将同片段邻近帧随机分进验证集；检查点保存划分和数据指纹，恢复时检查一致性。

CLI 训练采集入口为 `collect-training --frames 100 --output data/driving`，标注入口为 `annotate --dataset data/driving`，训练入口为 `train-bc`。旧的 `collect` 和中立 `rollout` 继续保留原格式；它们不自动成为专家训练数据。奖励、终止和比赛重置仍需额外游戏信号。
