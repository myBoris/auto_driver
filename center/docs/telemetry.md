# FH5 Data Out 遥测

游戏电脑的 Terminal 默认在 `127.0.0.1:5300` 接收 FH5 的 324 字节 Dashboard UDP 包，将最新有效数据通过已认证的终端 WebSocket 转发。Center 不监听游戏 UDP，也不合成遥测。终端参数见 [Terminal README](../../terminal/README.md)。

## 读取

`GET /api/terminals/{terminal_id}/telemetry` 使用现有 Bearer 或管理页认证，支持：

- `after`：非负整数。只有序号严格大于此值才返回 ready 数据。
- `timeout`：等待新数据的秒数，0–20，默认 0。
- `session_id`：期望的终端会话。AI 客户端会拒绝不同会话的响应。

```json
{"terminal_id":"game-pc","session_id":"connection-id","connected":true,"sequence":20,"status":"ready","ttl_ms":1000,"captured_at":1700000000.12,"received_at":1700000000.13,"age_ms":15,"data":{"source":"fh5_data_out","is_race_on":true,"timestamp_ms":10000,"speed_mps":12.5,"position":[100,20,300],"velocity":[0,0,12.5],"yaw":0.2}}
```

`captured_at` 是终端收到 UDP 的 UNIX 时间，`received_at` 是 Center 接收时间；二者来自不同机器，不能相减来估算网络延迟。`age_ms` 由 Center 的单调时钟计算，超过 1000ms 后过期。游戏自己的 `timestamp_ms` 是 uint32 毫秒时钟，可回绕，倒带/暂停/游戏切换可能导致不连续。

状态为 `ready` 时有有效 `data`。尚未收到数据或 after 未推进时为 `waiting`，过期为 `stale`，断线为 `disconnected`；后三者 `data=null`。重连创建新会话，不继承旧遥测。HTTP 响应禁止缓存。

位置是世界坐标（米）；`velocity`、`acceleration` 为车体本地轴 X右/Y上/Z前。速度单位 m/s；角度采用弧度。可选字段有 acceleration、angular_velocity、pitch、roll、engine_rpm、throttle、brake、clutch、handbrake、gear、steering，均来自已解析的游戏字段。

Data Out 不提供真实车道边界、道路偏移或碰撞事件；这些任务需要独立视觉/游戏信号，不能把输入 ACK 或加油量当作成功驾驶反馈。

## 将驾驶指令绑定到原连接

`POST /api/terminals/{id}/actions`、`/step`、`/release` 支持可选 query `session_id`。Center 在取得目标终端连接时校验，连接已替换则返回 HTTP 409 且不发送指令；空/过长会话参数返回 400。旧客户端省略该参数时仍使用当前连接。

RL 对全部动作、恢复按钮和释放请求传入固定会话，避免“客户端检查连接后、POST 到达前发生重连”的竞态。连接替换后的新会话不会收到旧训练的控制或释放。
