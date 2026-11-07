# 道路检测与 AI 方向显示接口

这组接口只发布可视化结果，不执行转向、油门或刹车。模型在 AI 进程中运行；Center 保存每个终端当前会话的最新结果，浏览器在 WebRTC 视频上叠加道路区域和预测方向。

页面显示道路区域和金色预测方向箭头；`road.centerline` 仅用于定位箭头，不绘制中间线。

页面默认读取真实结果。“演示”开关使用浏览器本地示意数据，并明确显示“演示数据”，无需训练模型。接口也接受 `prediction.source="demo"`，方便用脚本联调。

## 发布结果

`POST /api/terminals/{terminal_id}/perception`，请求头使用 `Authorization: Bearer <CENTER_TOKEN>` 和 `Content-Type: application/json`。

先从 `GET /api/terminals` 或 `GET /api/terminals/{terminal_id}` 获取当前 `session_id`。终端重连后会话会改变，旧会话的数据不能再发布。

```json
{
  "session_id": "替换为当前终端的session_id",
  "frame_sequence": 42,
  "captured_at": 1794000000.123,
  "status": "ready",
  "image": {"width": 1280, "height": 720},
  "road": {
    "polygon": [[0.32, 0.63], [0.40, 0.53], [0.48, 0.48], [0.59, 0.48], [0.72, 0.54], [0.87, 0.63]],
    "centerline": [[0.59, 0.63], [0.56, 0.56], [0.535, 0.48]],
    "confidence": 0.92
  },
  "prediction": {"steering": -0.2, "confidence": 0.86, "source": "model"},
  "model": {"id": "my-driving-v1", "label": "我的驾驶模型"},
  "inference_ms": 25
}
```

| 字段 | 约定 |
| --- | --- |
| `session_id` | 必填，匹配当前在线终端会话。 |
| `status` | `ready` 或 `no_road`；也可用 `waiting` / `unavailable` 主动清除旧结果，这两种状态的 `road` 和 `prediction` 都必须为 `null`。 |
| `image` | 模型对应的完整原始画面尺寸，正整数宽高。 |
| `road.polygon` | 道路区域的有序边界点，至少 3 个点。 |
| `road.centerline` | 路线中心的有序点，至少 2 个点；建议从近到远排列。 |
| 坐标 | `[x, y]`，均在 `[0, 1]`；左上角为 `[0, 0]`，右下角为 `[1, 1]`。以原始完整图像为基准，不包含播放器黑边。 |
| `prediction.steering` | `[-1, 1]`，负数左转、0 直行、正数右转；仅供显示。 |
| `confidence` | `[0, 1]`，由你的模型提供，Center 不代算置信度。 |
| `prediction.source` | 默认 `model`；还支持 `external`、`road_geometry` 和 `demo`。演示数据必须使用 `demo`。 |
| `model` | 必填 `id`；`label` 可选，用于展示来源。 |
| `frame_sequence` | 可选正整数，对应观察帧号；同会话不能倒序，可更新同一帧。 |
| `captured_at` | 可选，原图采集的 UNIX 秒时间戳，仅作为元数据。 |
| `inference_ms` | 可选，实际推理耗时，未提供时页面不显示虚构耗时。 |

只发布方向时可将 `road` 设为 `null`；只发布道路时可将 `prediction` 设为 `null`。`ready` 至少要包含其中一项。未检测到道路时发布 `status="no_road"`、`road=null`，并按需要清除或保留独立模型的 `prediction`。

每条结果在 Center 接收后有效 **2 秒**，建议持续以 5～10 Hz 发布最新结果，不积压旧帧。采集时间不参与跨机器时钟比较。未知终端返回 404，离线或会话不匹配及帧号倒退返回 409，字段非法返回 400。此接口沿用现有 Bearer 和浏览器同源认证。

## 读取与页面显示

`GET /api/terminals/{terminal_id}/perception` 返回最新结果，并附带 `terminal_id`、`session_id`、`received_at`、`age_ms`、`ttl_ms` 和 `frame` 元信息。未提供的帧号、采集时间和推理耗时为 `null`。

没有上报时为 `waiting`；超过有效期时为 `stale`，旧道路和方向被清除；离线时不继续显示旧结果。终端切换、会话替换、视频断开和页面隐藏都会清空叠加。道路检测与方向显示可分别开关，视频黑边与窗口缩放不会改变图像坐标。

## Python 接入

现有两个 SDK 均提供发布方法：

```python
# Center 轻量 SDK：from center.client import CenterClient
async with CenterClient(center_url, token) as client:
    terminals = await client.terminals()
    terminal = next(item for item in terminals if item["connected"])
    result = {
        "session_id": terminal["session_id"],
        "status": "ready",
        "image": {"width": 1280, "height": 720},
        "road": None,
        "prediction": {"steering": 0.25, "confidence": 0.9, "source": "demo"},
        "model": {"id": "example", "label": "演示数据"},
    }
    await client.publish_perception(terminal["terminal_id"], result)
    latest = await client.perception(terminal["terminal_id"])
```

AI 项目 SDK 使用 `list_terminals()` / `get_terminal()` / `get_perception()` / `publish_perception()`。将你训练后的道路点和预测方向写入同一结构，并把 `source` 改为 `model` 即可。在 `AI` 目录运行 `uv run python -m examples.publish_perception` 可上报 30 次、5 Hz 的演示数据；不会发出驾驶操作。
