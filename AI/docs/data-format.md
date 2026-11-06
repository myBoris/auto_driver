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

中心没有提供人类手柄输入标签 API。仅保存游戏画面无法得到行为克隆所需的动作标签，记录器不会把缺失标签自动补成零动作。

## 策略动作转移

`mode="rollout"` 接受 `write_transition(obs, action, result, reward=None, terminated=False, truncated=False)`。一条 `type="transition"` 记录包含前后观察引用、完整动作、命令 ID、原始 ACK、可选奖励及终止标记。两个 JPEG 均成功保存后才追加转移记录。

`action_source="policy_proposed"`、`labeled: true` 表示动作标签来自本次调用的策略提出值，**不表示人类示范、游戏遥测或驾驶已成功**。`ack` 保存执行确认和 `applied_at`，但虚拟手柄调用成功不能证明车辆按预期移动。`NeutralPolicy` 的零动作只用于验证链路，不能当作合格驾驶示范。

`reward` 默认 `null`，`terminated`/`truncated` 默认 `false`；中心未提供奖励、比赛重置、碰撞/车速遥测或终止判断。任务实现需要自行提供这些信号。元数据 `reward_available` 仅在显式写入非空奖励后变为 `true`。

## 会话与训练边界

同一 episode 内重复保存同序号、同时间、同内容的观察会复用文件。若序号相同而时间或内容不同，记录器报错而不覆盖；显式携带不同终端/会话的观察也会被拒绝。不同 episode 可以从相同序号开始。

`src.data.EpisodeDataset` 只定义离线样本接口，尚未实现 PyTorch DataLoader。后续需要补齐版本校验、坏帧处理、时间窗口、图像预处理和标签筛选，并按 episode 划分训练/验证集，避免相邻帧泄漏。模型推理和训练应共享同一预处理。

CLI 被动采集入口为 `collect --frames 100 --output data/...`，策略转移入口为 `rollout --policy neutral --steps 5 --output data/...`。它们记录真实 API 返回内容；训练算法、奖励和驾驶策略仍需自行实现。
